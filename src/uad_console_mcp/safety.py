"""The write barrier (docs/SPEC.md §4.3).

The UA Mixer Engine does **not** enforce its own ``readonly`` flags - a ``set`` on a property
marked read-only was accepted and applied on real hardware. This module is therefore the only real
protection between an LLM and someone's monitors, ears and ribbon microphones.

Everything here is pure, synchronous policy: no sockets, no I/O, no sleeping. The tool layer calls
:meth:`SafetyGate.check_write` before every write and only sends what comes back.

Every :class:`SafetyRefusal` message names the exact environment variable or confirm flag that
unblocks the action - or states plainly that nothing does.
"""

from __future__ import annotations

import logging
import math
import os
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, NoReturn, Protocol, cast

from .client import UAError

logger = logging.getLogger("uad_console_mcp.safety")

Mode = Literal["readonly", "safe", "full"]
MODES: tuple[Mode, ...] = ("readonly", "safe", "full")
DEFAULT_MODE: Mode = "safe"

ENV_MODE = "UAD_MCP_MODE"
ENV_MAX_MONITOR_DB = "UAD_MCP_MAX_MONITOR_DB"
ENV_MONITOR_STEP_DB = "UAD_MCP_MAX_MONITOR_STEP_DB"
ENV_MONITOR_WINDOW_DB = "UAD_MCP_MONITOR_WINDOW_DB"
ENV_MONITOR_WINDOW_SECONDS = "UAD_MCP_MONITOR_WINDOW_SECONDS"
ENV_WRITE_RATE = "UAD_MCP_WRITES_PER_SECOND"
ENV_WRITE_BURST = "UAD_MCP_WRITE_BURST"
ENV_ALLOW_READONLY = "UAD_MCP_ALLOW_READONLY_WRITES"

DEFAULT_MAX_MONITOR_DB = -6.0
DEFAULT_MONITOR_STEP_DB = 6.0
DEFAULT_MONITOR_WINDOW_DB = 10.0
DEFAULT_MONITOR_WINDOW_SECONDS = 30.0
DEFAULT_WRITES_PER_SECOND = 10.0
DEFAULT_WRITE_BURST = 20

AUDIO_FLOWING_DB = -60.0
"""Any input meter above this counts as "audio is flowing" for the clock/sample-rate gates."""

MONITOR_LEVEL_PROPERTY = "CRMonitorLevel"
PHANTOM_POWER_PROPERTY = "48V"
DIM_ATTENUATION_PROPERTY = "DimAttenuation"
DIM_ON_PROPERTY = "DimOn"
MONITOR_RELEASE_PROPERTIES = ("Mute", "DimOn")
"""Setting these to false releases an attenuation, so they are monitor level events."""

WRITABLE_PROPERTIES: frozenset[str] = frozenset(
    {
        "Name",
        "FaderLevel",
        "Pan",
        "Pan2",
        "Mute",
        "Solo",
        "SoloSafe",
        "ChannelHidden",
        "Gain",
        "48V",
        "Pad",
        "Phase",
        "LowCut",
        "HiZ",
        "IOType",
        "Bypass",
        "PostFaderOverride",
        "EffectName",
        "Preset",
        "Power",
        "NormalizedValue",
        "CRMonitorLevel",
        "DimOn",
        "MixToMono",
        "DimAttenuation",
        "TalkbackOn",
        "TalkbackMaster",
        "TalkbackEnabled",
        "AltMonSelection",
        "ClockSource",
        "SampleRate",
        "StereoName",
        "Stereo",
        "EnabledByUser",
        "SRConvert",
    }
)
"""Property names writable in ``safe`` mode. Everything else needs ``UAD_MCP_MODE=full``."""

CONFIRM_REQUIRED_PROPERTIES = frozenset({"ClockSource", "SampleRate"})
"""Allowlisted, but additionally gated on an is-audio-flowing check plus ``confirm=True``."""

DENIED_PROPERTIES = frozenset(
    {
        # SPEC §4.3 denylist
        "AppCommand",
        "DeviceHwID",
        "MixerState",
        "MultiUnitOrder",
        # system/identity descriptors: writing them desyncs the engine from the hardware
        "ActiveSpans",
        "AnalogBoardID",
        "DefaultSpans",
        "DeviceTypeID",
        "MaxSpans",
        "Session",
        "Type",
    }
)
DENIED_PROPERTY_SUBSTRINGS = ("firmware", "bootloader", "serialnumber", "hwid")
DENIED_PATH_PREFIXES = ("/pluginscenes",)

_MODE_REMEDY = f"Set {ENV_MODE}=safe (or full) to allow writes at all."
_FULL_REMEDY = f"Set {ENV_MODE}=full to bypass the safe-mode allowlist."
_NO_UNBLOCK = f"Nothing unblocks this: no {ENV_MODE} value and no confirm flag will allow it."

_TRUTHY = {"1", "true", "t", "yes", "y", "on"}
_FALSY = {"0", "false", "f", "no", "n", "off"}
_HIERARCHICAL_OPTION_TYPES = {"folder", "file"}
_EPSILON = 1e-9


class SafetyRefusal(UAError):
    """A write was refused by policy.

    The message always names the env var or confirm flag that unblocks it, or says outright that
    nothing does.
    """


class PropertyLike(Protocol):
    """Structural view of :class:`uad_console_mcp.client.UAProperty`.

    Duck-typed on purpose: the gate needs nothing but these attributes, which keeps it unit
    testable without a client, a socket or an engine.
    """

    path: str
    type: str
    value: Any
    min: float | None
    max: float | None
    values: list[Any] | None
    readonly: bool


@dataclass(frozen=True)
class WriteContext:
    """What the tool layer knows about a write, including its confirmations.

    :meth:`SafetyGate.check_write` accepts a plain string for ``context`` (treated as ``reason``),
    so callers that need no confirmations stay terse.
    """

    reason: str = ""
    confirm: bool = False
    """Generic "yes, I mean it" for clock and sample-rate changes."""

    confirm_condenser_mic: bool = False
    """Required for 48V: phantom power can destroy ribbon microphones."""

    audio_flowing: bool | None = None
    """Result of the tool-layer meter check. ``None`` means "not checked" and is refused."""

    monitor_level_db: float | None = None
    """Stored monitor level, so unmute/un-dim writes can be checked against the ceiling."""

    is_monitor: bool = False
    """True when the target strip really is a control-room monitor.

    The gate cannot see topology. With this set, a missing :attr:`monitor_level_db` is a FAILED
    level read rather than "not a monitor", so the release is refused instead of allowed.
    """

    dim_on: bool | None = None
    """Live ``DimOn`` state, so a ``DimAttenuation`` decrease can be railed. ``None`` means
    "not read", which fails closed for a decrease."""

    def __str__(self) -> str:
        return self.reason


def as_write_context(context: str | WriteContext) -> WriteContext:
    """Normalise the ``context`` argument of :meth:`SafetyGate.check_write`."""
    if isinstance(context, WriteContext):
        return context
    return WriteContext(reason=str(context))


# ----------------------------------------------------------------------------- env parsing


def _environ(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _raw_env(env: Mapping[str, str] | None, name: str) -> str | None:
    raw = _environ(env).get(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip()


def mode_from_env(env: Mapping[str, str] | None = None) -> Mode:
    """Parse :data:`ENV_MODE`. Unset or empty means ``"safe"``; anything unknown is an error."""
    raw = _raw_env(env, ENV_MODE)
    if raw is None:
        return DEFAULT_MODE
    folded = raw.casefold()
    if folded not in MODES:
        raise ValueError(
            f"{ENV_MODE}={raw!r} is not a valid safety mode. "
            f"Use one of: {', '.join(MODES)} (default {DEFAULT_MODE})."
        )
    return cast(Mode, folded)


def float_from_env(env: Mapping[str, str] | None, name: str, default: float) -> float:
    """Parse a float environment variable, naming ``name`` in every failure message."""
    raw = _raw_env(env, name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"{name}={raw!r} is not a number. Set {name} to a plain decimal "
            f"value (default {default}) or unset it."
        ) from exc
    if not math.isfinite(value):
        raise ValueError(f"{name}={raw!r} must be a finite number (default {default}).")
    return value


def int_from_env(env: Mapping[str, str] | None, name: str, default: int) -> int:
    """Parse an int environment variable, naming ``name`` in every failure message."""
    raw = _raw_env(env, name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(
            f"{name}={raw!r} is not a whole number. Set {name} to an integer "
            f"(default {default}) or unset it."
        ) from exc


def bool_from_env(env: Mapping[str, str] | None, name: str, default: bool = False) -> bool:
    """Parse a boolean environment variable, naming ``name`` in every failure message."""
    raw = _raw_env(env, name)
    if raw is None:
        return default
    folded = raw.casefold()
    if folded in _TRUTHY:
        return True
    if folded in _FALSY:
        return False
    raise ValueError(f"{name}={raw!r} is not a boolean. Use {name}=1 or {name}=0.")


def gate_from_env(env: Mapping[str, str] | None = None) -> SafetyGate:
    """Build a :class:`SafetyGate` entirely from the environment (server entry point)."""
    return SafetyGate(
        mode_from_env(env),
        float_from_env(env, ENV_MAX_MONITOR_DB, DEFAULT_MAX_MONITOR_DB),
        monitor_step_db=float_from_env(env, ENV_MONITOR_STEP_DB, DEFAULT_MONITOR_STEP_DB),
        monitor_window_db=float_from_env(env, ENV_MONITOR_WINDOW_DB, DEFAULT_MONITOR_WINDOW_DB),
        monitor_window_seconds=float_from_env(
            env, ENV_MONITOR_WINDOW_SECONDS, DEFAULT_MONITOR_WINDOW_SECONDS
        ),
        writes_per_second=float_from_env(env, ENV_WRITE_RATE, DEFAULT_WRITES_PER_SECOND),
        write_burst=int_from_env(env, ENV_WRITE_BURST, DEFAULT_WRITE_BURST),
        allow_readonly_writes=bool_from_env(env, ENV_ALLOW_READONLY, False),
    )


# ------------------------------------------------------------------------- small utilities


def property_name(path: str) -> str:
    """Last path segment, tolerating a trailing ``/value``."""
    parts = [part for part in path.split("/") if part]
    if not parts:
        return ""
    if len(parts) > 1 and parts[-1] == "value":
        return parts[-2]
    return parts[-1]


def coerces_to_false(value: Any) -> bool:
    """Would :meth:`SafetyGate.check_write` read ``value`` as ``False``?

    The one definition of "this write is a release". Callers that decide whether a ``Mute`` or
    ``DimOn`` write needs the monitor rail MUST use this rather than ``value is False``: the gate
    coerces ``0``, ``"no"``, ``"n"``, ``"off"`` and friends, so an identity test on ``False``
    lets exactly those spellings past the rail.
    """
    if isinstance(value, bool):
        return value is False
    if _is_number(value):
        return float(value) == 0.0
    return isinstance(value, str) and value.strip().casefold() in _FALSY


def _refuse(reason: str, remedy: str) -> NoReturn:
    message = f"{reason} {remedy}"
    logger.warning("refused: %s", message)
    raise SafetyRefusal(message)


def _denial_reason(name: str, path: str) -> str | None:
    folded = name.casefold()
    for prefix in DENIED_PATH_PREFIXES:
        if path.startswith(prefix):
            return f"{prefix} is never writable through this server."
    if name in DENIED_PROPERTIES:
        return f"{name!r} is on the permanent denylist (device identity / engine internals)."
    for needle in DENIED_PROPERTY_SUBSTRINGS:
        if needle in folded:
            return f"{name!r} looks like a firmware or hardware-identity property."
    return None


def _text(value: Any) -> str:
    return str(value).strip().casefold()


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _same_number(left: Any, right: Any) -> bool:
    if not (_is_number(left) or isinstance(left, str)):
        return False
    if not _is_number(right):
        return False
    try:
        return abs(float(left) - float(right)) < _EPSILON
    except (TypeError, ValueError):
        return False


def _is_hierarchical(options: Sequence[Any]) -> bool:
    """True for preset browser trees, whose ``values`` are folders/files, not selectable enums."""
    for option in options:
        if not isinstance(option, dict):
            continue
        if "children" in option:
            return True
        if _text(option.get("type", "")) in _HIERARCHICAL_OPTION_TYPES:
            return True
    return False


def _positive(name: str, value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number, got {value!r}")
    return value


# ---------------------------------------------------------------------------------- the gate


class SafetyGate:
    """Policy for every write. Construct one per server process and share it.

    ``clock`` is injectable purely so the rate limit and the rolling monitor window are testable
    without sleeping; it must be a monotonic source.
    """

    def __init__(
        self,
        mode: Mode = DEFAULT_MODE,
        max_monitor_db: float = DEFAULT_MAX_MONITOR_DB,
        *,
        monitor_step_db: float = DEFAULT_MONITOR_STEP_DB,
        monitor_window_db: float = DEFAULT_MONITOR_WINDOW_DB,
        monitor_window_seconds: float = DEFAULT_MONITOR_WINDOW_SECONDS,
        writes_per_second: float = DEFAULT_WRITES_PER_SECOND,
        write_burst: int = DEFAULT_WRITE_BURST,
        allow_readonly_writes: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if mode not in MODES:
            raise ValueError(
                f"mode must be one of {', '.join(MODES)}, got {mode!r} "
                f"(this normally comes from {ENV_MODE})."
            )
        if not math.isfinite(max_monitor_db):
            raise ValueError(
                f"{ENV_MAX_MONITOR_DB} must be a finite dB value, got {max_monitor_db!r}"
            )
        if max_monitor_db > 0:
            logger.warning(
                "%s is %+.1f dB: above unity, so the monitor ceiling protects nothing",
                ENV_MAX_MONITOR_DB,
                max_monitor_db,
            )

        self._mode: Mode = mode
        self._max_monitor_db = float(max_monitor_db)
        self._monitor_step_db = _positive(ENV_MONITOR_STEP_DB, float(monitor_step_db))
        self._monitor_window_db = _positive(ENV_MONITOR_WINDOW_DB, float(monitor_window_db))
        self._monitor_window_seconds = _positive(
            ENV_MONITOR_WINDOW_SECONDS, float(monitor_window_seconds)
        )
        self._writes_per_second = _positive(ENV_WRITE_RATE, float(writes_per_second))
        self._write_burst = int(_positive(ENV_WRITE_BURST, float(write_burst)))
        self._allow_readonly_writes = bool(allow_readonly_writes)
        self._clock = clock

        self._tokens = float(self._write_burst)
        self._tokens_updated = clock()
        self._monitor_events: deque[tuple[float, float]] = deque()
        self._last_monitor_db: float | None = None

    # ------------------------------------------------------------------------ introspection

    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def max_monitor_db(self) -> float:
        return self._max_monitor_db

    def describe(self) -> dict[str, Any]:
        """Effective limits, for ``uad://state`` and ``--check``."""
        return {
            "mode": self._mode,
            "mode_env_var": ENV_MODE,
            "max_monitor_db": self._max_monitor_db,
            "max_monitor_step_db": self._monitor_step_db,
            "monitor_window_db": self._monitor_window_db,
            "monitor_window_seconds": self._monitor_window_seconds,
            "writes_per_second": self._writes_per_second,
            "write_burst": self._write_burst,
            "allow_readonly_writes": self._allow_readonly_writes,
            "allowlisted_properties": sorted(WRITABLE_PROPERTIES),
            "confirm_required_properties": sorted(CONFIRM_REQUIRED_PROPERTIES),
        }

    # -------------------------------------------------------------------------- write checks

    def check_write(
        self,
        prop: PropertyLike,
        new_value: Any,
        *,
        context: str | WriteContext,
    ) -> Any:
        """Return the value that may be written, or raise.

        Raises:
            SafetyRefusal: policy says no; the message names the env var or confirm flag.
            ValueError: ``new_value`` cannot be applied to this property's type at all.
        """
        ctx = as_write_context(context)
        path = prop.path
        name = property_name(path)
        because = f" ({ctx.reason})" if ctx.reason else ""

        # These two are permanent, global and free to evaluate. They are answered BEFORE the token
        # bucket so a caller in readonly mode (or hammering the denylist) always gets the message
        # that names its real blocker, instead of "retry in 0.1 s" for something no wait can fix.
        if self._mode == "readonly":
            _refuse(f"Refused write to {path}{because}: safety mode is 'readonly'.", _MODE_REMEDY)

        denial = _denial_reason(name, path)
        if denial is not None:
            _refuse(f"Refused write to {path}{because}: {denial}", _NO_UNBLOCK)

        # Everything below this line costs a token whether it is allowed or refused: the caller
        # already spent a property read to get here, so refusals must not be free.
        self._consume_token(path)

        if getattr(prop, "readonly", False) and not self._allow_readonly_writes:
            _refuse(
                f"Refused write to {path}{because}: the engine reports it read-only, and the "
                "engine does not enforce that itself, so the write would really apply.",
                f"Set {ENV_ALLOW_READONLY}=1 only if you are certain this property is safe.",
            )

        if self._mode == "safe" and name not in WRITABLE_PROPERTIES:
            _refuse(
                f"Refused write to {path}{because}: {name!r} is not in the safe-mode allowlist.",
                _FULL_REMEDY,
            )

        if name == PHANTOM_POWER_PROPERTY:
            self._check_phantom_power(path, new_value, ctx)
        if name in CONFIRM_REQUIRED_PROPERTIES:
            self._check_transport_change(name, path, ctx)

        value = self.validate_value(prop, new_value)

        if name == MONITOR_LEVEL_PROPERTY:
            value = self._check_monitor_level_write(prop, value)
        elif name == DIM_ATTENUATION_PROPERTY:
            self._check_dim_attenuation(prop, value, ctx, because)
        elif name in MONITOR_RELEASE_PROPERTIES and coerces_to_false(value):
            self._check_monitor_release(name, path, ctx, because)

        return value

    def validate_value(self, prop: PropertyLike, new_value: Any) -> Any:
        """Enum-resolve, coerce and clamp ``new_value`` against the property's own metadata."""
        value = self._resolve_enum(prop, new_value)
        value = self._coerce(prop, value)
        return self._clamp(prop, value)

    # ------------------------------------------------------------------------- monitor rail

    def monitor_level_check(self, current_db: float, target_db: float) -> float:
        """Return the monitor level that may be applied, or raise.

        Turning down is always allowed. Turning up is capped by the ceiling, by the per-call step
        and by the rolling window. Call :meth:`note_monitor_event` once the write succeeds.
        """
        current = float(current_db)
        target = float(target_db)
        self._last_monitor_db = current

        if target <= current:
            return target

        if target > self._max_monitor_db:
            _refuse(
                f"Refused monitor level {target:+.1f} dB: above the "
                f"{self._max_monitor_db:+.1f} dB ceiling.",
                f"Ask for {self._max_monitor_db:+.1f} dB or less, or raise "
                f"{ENV_MAX_MONITOR_DB} (hearing and speaker damage risk).",
            )

        step = target - current
        if step > self._monitor_step_db + _EPSILON:
            target = current + self._monitor_step_db
            step = self._monitor_step_db
            logger.warning(
                "clamped monitor increase to +%.1f dB per call (%s=%s); applying %+.1f dB",
                self._monitor_step_db,
                ENV_MONITOR_STEP_DB,
                self._monitor_step_db,
                target,
            )

        remaining = self._monitor_window_db - self._monitor_window_used()
        if step > remaining + _EPSILON:
            _refuse(
                f"Refused monitor increase of +{step:.1f} dB: only +{max(0.0, remaining):.1f} dB "
                f"of the +{self._monitor_window_db:.1f} dB / "
                f"{self._monitor_window_seconds:.0f} s monitor budget is left.",
                f"Wait {self._monitor_window_wait():.0f} s, or raise "
                f"{ENV_MONITOR_WINDOW_DB} / {ENV_MONITOR_WINDOW_SECONDS}.",
            )
        return target

    def note_monitor_event(self, applied_db: float) -> None:
        """Record an applied monitor level so the rolling window sees the increase."""
        applied = float(applied_db)
        previous = self._last_monitor_db
        self._last_monitor_db = applied
        if previous is None:
            logger.debug("seeding monitor level tracking at %+.1f dB", applied)
            return
        increase = applied - previous
        if increase <= 0:
            return
        self._monitor_events.append((self._clock(), increase))
        self._prune_monitor_events()

    def check_monitor_unmute(self, stored_db: float, *, action: str = "unmute") -> None:
        """Refuse an unmute/un-dim that would jump straight to an above-ceiling level."""
        stored = float(stored_db)
        if stored > self._max_monitor_db:
            _refuse(
                f"Refused to {action} the monitor output: its stored level is {stored:+.1f} dB, "
                f"above the {self._max_monitor_db:+.1f} dB ceiling, so releasing it would jump "
                "straight there.",
                f"Set {MONITOR_LEVEL_PROPERTY} to {self._max_monitor_db:+.1f} dB or less first, "
                f"or raise {ENV_MAX_MONITOR_DB}.",
            )

    def monitor_budget_remaining_db(self) -> float:
        """How much of the rolling monitor increase budget is left right now."""
        return max(0.0, self._monitor_window_db - self._monitor_window_used())

    # ----------------------------------------------------------------------------- internals

    def _check_phantom_power(self, path: str, new_value: Any, ctx: WriteContext) -> None:
        engaging = new_value not in (False, 0, "0", "false", "False")
        if engaging and not ctx.confirm_condenser_mic:
            _refuse(
                f"Refused to switch 48V phantom power on at {path}: it can permanently damage "
                "ribbon microphones and some vintage dynamics.",
                "Confirm the connected microphone is a condenser and pass "
                "confirm_condenser_mic=True.",
            )

    def _check_transport_change(self, name: str, path: str, ctx: WriteContext) -> None:
        if not ctx.confirm:
            _refuse(
                f"Refused write to {path}: changing {name} re-locks the whole audio chain and "
                "will glitch or drop every running stream.",
                "Pass confirm=True from the tool layer to proceed.",
            )
        if ctx.audio_flowing is None:
            _refuse(
                f"Refused write to {path}: nobody checked whether audio is flowing.",
                f"Run the meter check (any input above {AUDIO_FLOWING_DB:+.0f} dB) and pass "
                "audio_flowing=False together with confirm=True.",
            )
        if ctx.audio_flowing:
            _refuse(
                f"Refused write to {path}: audio is flowing (an input meter is above "
                f"{AUDIO_FLOWING_DB:+.0f} dB).",
                "Stop playback and recording, then retry with confirm=True and "
                "audio_flowing=False.",
            )

    def _check_monitor_release(
        self, name: str, path: str, ctx: WriteContext, because: str
    ) -> None:
        """Rail an unmute / un-dim, failing CLOSED when the level behind it is unknown."""
        action = "un-dim" if name == DIM_ON_PROPERTY else "unmute"
        if ctx.monitor_level_db is not None:
            self.check_monitor_unmute(ctx.monitor_level_db, action=action)
            return
        if ctx.is_monitor:
            _refuse(
                f"Refused to {action} the monitor output at {path}{because}: reading its stored "
                f"{MONITOR_LEVEL_PROPERTY} failed, so the level this release would jump straight "
                "to is unknown.",
                f"Read {MONITOR_LEVEL_PROPERTY} on that strip successfully and pass it in the "
                f"write context, or lower it explicitly first; no {ENV_MODE} value and no confirm "
                "flag bypasses a failed monitor level read.",
            )
        logger.debug(
            "%s released on a strip the tool layer did not identify as a monitor; "
            "pass is_monitor=True plus monitor_level_db for control-room outputs",
            path,
        )

    def _check_dim_attenuation(
        self, prop: PropertyLike, value: Any, ctx: WriteContext, because: str
    ) -> None:
        """A DECREASE of the dim attenuation while dim is engaged is an un-dim: rail it.

        Increasing the attenuation only ever makes the control room quieter, so it is always
        allowed. Lowering it while ``DimOn`` is true raises the real acoustic level by exactly
        that many dB, which is the same event as releasing the dim.
        """
        if not _is_number(value):
            return
        attenuation = float(value)
        current = float(prop.value) if _is_number(prop.value) else None
        if current is not None and attenuation >= current - _EPSILON:
            return
        if ctx.dim_on is False:
            return
        if ctx.dim_on is None or ctx.monitor_level_db is None:
            failed = " and ".join(
                name
                for name, known in (
                    (DIM_ON_PROPERTY, ctx.dim_on is not None),
                    (MONITOR_LEVEL_PROPERTY, ctx.monitor_level_db is not None),
                )
                if not known
            )
            _refuse(
                f"Refused to lower {prop.path}{because} to {attenuation:g} dB: reading {failed} "
                "failed, so the level this un-dim would expose is unknown.",
                f"Read {DIM_ON_PROPERTY} and {MONITOR_LEVEL_PROPERTY} on the monitor output "
                f"successfully and pass them, or raise the attenuation instead; no {ENV_MODE} "
                "value and no confirm flag bypasses a failed monitor level read.",
            )
        self.check_monitor_unmute(ctx.monitor_level_db - attenuation, action="un-dim")

    def _check_monitor_level_write(self, prop: PropertyLike, target: Any) -> float:
        current = prop.value
        if not _is_number(current):
            raise ValueError(
                f"cannot apply the monitor rail to {prop.path}: its current value is {current!r}, "
                "so the increase cannot be measured. Read the property first."
            )
        return self.monitor_level_check(float(current), float(target))

    def _resolve_enum(self, prop: PropertyLike, value: Any) -> Any:
        options = getattr(prop, "values", None)
        if not options:
            return value
        if _is_hierarchical(options):
            logger.debug("skipping enum validation for %s (hierarchical preset tree)", prop.path)
            return value

        allowed: list[tuple[Any, str | None, bool]] = []
        for option in options:
            if isinstance(option, dict):
                allowed.append(
                    (
                        option.get("value"),
                        option.get("string"),
                        bool(option.get("enabled", True)),
                    )
                )
            else:
                allowed.append((option, None, True))

        wanted = _text(value)
        for option_value, label, enabled in allowed:
            matched = (
                value == option_value
                or wanted == _text(option_value)
                or (label is not None and wanted == _text(label))
                or _same_number(value, option_value)
            )
            if not matched:
                continue
            if not enabled:
                _refuse(
                    f"Refused write to {prop.path}: the engine reports option "
                    f"{option_value!r} as disabled right now.",
                    f"Pick an enabled option instead. {_NO_UNBLOCK}",
                )
            return option_value

        shown = ", ".join(repr(option) for option, _, enabled in allowed if enabled)
        _refuse(
            f"Refused write to {prop.path}: {value!r} is not one of its allowed values.",
            f"Use one of: {shown}. {_NO_UNBLOCK}",
        )

    def _coerce(self, prop: PropertyLike, value: Any) -> Any:
        kind = _text(getattr(prop, "type", "") or "")
        if kind == "bool":
            return _to_bool(prop, value)
        if kind in ("int", "int64"):
            return _to_int(prop, value)
        if kind == "float":
            return _to_float(prop, value)
        if kind == "string":
            return _to_str(prop, value)
        logger.debug(
            "no coercion rule for property type %r at %s", getattr(prop, "type", None), prop.path
        )
        return value

    def _clamp(self, prop: PropertyLike, value: Any) -> Any:
        if not _is_number(value):
            return value
        low = getattr(prop, "min", None)
        high = getattr(prop, "max", None)
        clamped = float(value)
        if low is not None and clamped < float(low):
            clamped = float(low)
        if high is not None and clamped > float(high):
            clamped = float(high)
        if abs(clamped - float(value)) > _EPSILON:
            logger.warning(
                "clamped %s from %s to %s (min=%s max=%s)", prop.path, value, clamped, low, high
            )
        if _text(getattr(prop, "type", "") or "") in ("int", "int64"):
            return int(round(clamped))
        return clamped

    def _consume_token(self, path: str) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._tokens_updated)
        self._tokens_updated = now
        self._tokens = min(
            float(self._write_burst), self._tokens + elapsed * self._writes_per_second
        )
        if self._tokens < 1.0:
            wait = (1.0 - self._tokens) / self._writes_per_second
            _refuse(
                f"Refused write to {path}: write rate limit reached "
                f"({self._writes_per_second:g} writes/s, burst {self._write_burst}).",
                f"Retry in {wait:.2f} s, or raise {ENV_WRITE_RATE} / {ENV_WRITE_BURST}.",
            )
        self._tokens -= 1.0

    def _prune_monitor_events(self) -> None:
        cutoff = self._clock() - self._monitor_window_seconds
        while self._monitor_events and self._monitor_events[0][0] <= cutoff:
            self._monitor_events.popleft()

    def _monitor_window_used(self) -> float:
        self._prune_monitor_events()
        return sum(increase for _, increase in self._monitor_events)

    def _monitor_window_wait(self) -> float:
        self._prune_monitor_events()
        if not self._monitor_events:
            return 0.0
        oldest = self._monitor_events[0][0]
        return max(0.0, oldest + self._monitor_window_seconds - self._clock())


# ------------------------------------------------------------------------------- coercion


def _type_error(prop: PropertyLike, value: Any, expected: str) -> ValueError:
    return ValueError(
        f"{prop.path} is a {getattr(prop, 'type', '?')} property; {value!r} is not {expected}."
    )


def _to_bool(prop: PropertyLike, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if _is_number(value) and float(value) in (0.0, 1.0):
        return bool(value)
    if isinstance(value, str):
        folded = value.strip().casefold()
        if folded in _TRUTHY:
            return True
        if folded in _FALSY:
            return False
    raise _type_error(prop, value, "a true/false value")


def _to_int(prop: PropertyLike, value: Any) -> int:
    if isinstance(value, bool):
        raise _type_error(prop, value, "a number")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    if isinstance(value, str):
        try:
            return int(round(float(value.strip())))
        except ValueError as exc:
            raise _type_error(prop, value, "a number") from exc
    raise _type_error(prop, value, "a number")


def _to_float(prop: PropertyLike, value: Any) -> float:
    if isinstance(value, bool):
        raise _type_error(prop, value, "a number")
    if _is_number(value):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError as exc:
            raise _type_error(prop, value, "a number") from exc
    raise _type_error(prop, value, "a number")


def _to_str(prop: PropertyLike, value: Any) -> str:
    if isinstance(value, str):
        return value
    raise _type_error(prop, value, "a string")
