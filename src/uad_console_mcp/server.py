"""The MCP server: tools, resources and prompts (docs/SPEC.md §4.5).

Structure, and why it is shaped this way:

* :class:`ServerContext` owns the whole session -- one :class:`~uad_console_mcp.client.UAClient`,
  a topology cache with a 30 s TTL that is invalidated on reconnect, the
  :class:`~uad_console_mcp.safety.SafetyGate` built from the environment, and the snapshot store.
* Every tool is a plain ``async def tool_x(ctx, ...) -> dict``. They are ordinary functions the
  test suite calls directly, with no MCP plumbing in the way; :func:`build_server` wraps each of
  them in a thin registered closure whose signature is what the JSON schema is generated from.
* Every write goes through :meth:`ServerContext.write`, which reads the property first (so the
  gate sees real metadata and the real current value), asks the gate, writes, and reports
  ``{verified, before, after, warnings}``. Nothing in this module calls ``set_value`` directly.
* Tools never raise at the MCP boundary: the :func:`tool` decorator turns any exception into an
  actionable sentence, because a traceback tells a model nothing it can act on.

``uad_raw_set`` is registered ONLY when the safety mode is ``full``. Nothing here writes to
stdout -- an MCP stdio server must keep stdout for the protocol; logs go to stderr.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import Any

from . import __version__, snapshot
from .client import (
    UAClient,
    UAConnectError,
    UAError,
    UAPathError,
    UAProperty,
    UATimeoutError,
)
from .discovery import (
    ChannelInfo,
    Rig,
    TargetNotFoundError,
    discover,
)
from .safety import (
    AUDIO_FLOWING_DB,
    DIM_ATTENUATION_PROPERTY,
    DIM_ON_PROPERTY,
    MONITOR_LEVEL_PROPERTY,
    MONITOR_RELEASE_PROPERTIES,
    SafetyGate,
    SafetyRefusal,
    WriteContext,
    coerces_to_false,
    gate_from_env,
    property_name,
)
from .snapshot import SnapshotStore, values_equal

logger = logging.getLogger("uad_console_mcp.server")

SERVER_NAME = "uad-console"

RIG_TTL_S = 30.0
RIG_WALK_ATTEMPTS = 3
"""How many times a topology walk may be re-run because it was invalidated while running."""

TALKBACK_AUTO_OFF_S = 30.0
TALKBACK_ON_PATH = "/TalkbackOn"
TALKBACK_MASTER_PATH = "/TalkbackMaster"
TALKBACK_AUTO_OFF_RETRY_DELAYS_S: tuple[float, ...] = (1.0, 2.0, 4.0)
"""Backoff between auto-off attempts. Its length is the number of attempts."""

CLOCK_VERIFY_DELAY_S = 2.5
WATCH_MAX_DURATION_S = 30.0
WATCH_MIN_INTERVAL_S = 0.1
DEFAULT_WATCH_INTERVAL_S = 1.0 / 3.0
"""~3 Hz meter polling: proven cheap against the real engine (SPEC §1)."""

METER_FLOOR_DB = -77.0
METER_FLOOR_TOLERANCE_DB = 0.1
"""Captured floor readings wobble by float noise (-76.99993), so compare with slack."""

PLUGIN_LIST_LIMIT = 50
UNISON_SLOT = "unison"

CONNECT_HELP = (
    "UA Mixer Engine not reachable on {host}:{port} - is the UAD software installed and "
    "running? (It starts with UAD Console and keeps running afterwards.)"
)

LIMITATIONS = """\
# What this server cannot do (and what to do instead)

The UA Mixer Engine's local API is the only channel this server has. These are hard limits of
that API, not missing features.

## Levels and routing
- **Headphone output level cannot be set.** The HP outputs expose no level property at all, only
  their `MixInSource` (which cue bus feeds them). Workaround: pick the cue bus with
  `MixInSource`, then set the *send* levels into that cue bus with `uad_set_sends`, and use the
  physical knob for the final trim.
- **The I/O routing matrix (`/inputroutes`, `/outputroutes`) cannot be re-patched here.** No tool
  writes it, and the property that actually carries the mapping (`ChannelIndex`) is not on the
  safe-mode allowlist. Use `uad_raw_get` to inspect it; re-routing needs UAD Console itself.
- **Channels cannot be created or deleted.** The strip list is fixed by the hardware model.
- **Monitor level is capped** by `UAD_MCP_MAX_MONITOR_DB` (default -6 dB), with at most +6 dB per
  call and +10 dB per 30 s. Unmuting or un-dimming onto an above-ceiling stored level is refused,
  and so is *lowering* `dim_attenuation_db` while dim is on, because that exposes the same level.
  Lower the stored level first. If the stored level cannot be read, the release is refused rather
  than guessed at.

## Metering
- **A meter sitting exactly at the floor (-77 dB) cannot tell silence from a dead link.** On an
  ADAT or S/PDIF input, "all channels pinned at the floor" almost always means no clock lock or
  a bad optical cable, not a quiet source. See the `troubleshoot_clock` prompt.
- Meters are polled (~3 Hz), not streamed sample-accurately. `uad_watch_meters` caps at 30 s.
- There is no audio capture: this server cannot record, play or analyse the audio itself.

## Plugins
- **Plugin parameter nodes only exist after a plugin is loaded into the slot.** `uad_load_plugin`
  polls for up to 5 s; if they never appear, parameter writes for that slot are skipped rather
  than silently lost.
- Parameters are addressed by name and set as `normalized` 0-1 values. There is no dB or Hz unit
  conversion; `StringValue` is the engine's own display text, read-only in practice.
- Per-plugin DSP cost is not exposed. Only the totals (`TotalDSPLoad`, `TotalPGMLoad`,
  `TotalMEMLoad`) are, in `uad_get_status`.
- Unauthorized/demo plugins can be loaded but will not process audio; check `authorized` in
  `uad_list_plugins`.

## Sessions and scenes
- **Console sessions and plugin scenes cannot be saved or recalled.** Writes under
  `/pluginscenes` are permanently denied. Use `uad_snapshot` instead: it captures every property
  this server may write - every strip under `/devices`, plus the engine-wide talkback pair
  (`TalkbackOn`, `TalkbackMaster`) - and restores them in a safe order, talkback last.
- Snapshots do not restore clock source or sample rate unless you ask for it, and never restore
  the monitor rail unless you ask for it. Talkback that was ON when the snapshot was taken is
  never switched back on by a restore either (that would open a live mic with no auto-off timer);
  use `uad_talkback(on=true)`, which arms one.

## Transport
- **Clock source and sample rate changes re-lock the whole chain.** Both refuse while audio is
  flowing (any input above -60 dB) and require `confirm=true`. `uad_set_clock` verifies
  `ClockLocked` and reverts itself if the new source does not lock.
- Firmware, device identity, `AppCommand` and `MixerState` are never writable, in any mode.
- The engine does NOT enforce its own read-only flags, which is exactly why the allowlist in
  `safe` mode exists. `UAD_MCP_MODE=full` bypasses the allowlist; the monitor rail and the write
  rate limit still apply.
"""


# ------------------------------------------------------------------------------ error surface


def error_text(exc: BaseException, *, client: UAClient | None = None) -> str:
    """One actionable sentence for the model. Never a traceback, never a bare type name."""
    if isinstance(exc, UAConnectError):
        host = client.host if client else "127.0.0.1"
        port = client.port if client else 4710
        return f"{CONNECT_HELP.format(host=host, port=port)} (detail: {exc})"
    if isinstance(exc, UATimeoutError):
        return (
            f"{exc} The engine is reachable but did not answer; it may be busy loading plugins "
            "or scanning presets. Retry in a moment."
        )
    if isinstance(exc, UAError | ValueError):
        # Every UAError this package raises (SafetyRefusal, AmbiguousTargetError,
        # TargetNotFoundError, UAPathError, SnapshotError) already carries an actionable
        # sentence naming the flag, env var or alternative that unblocks it.
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


ToolFn = Callable[..., Awaitable[dict[str, Any]]]


def tool(fn: ToolFn) -> ToolFn:
    """Wrap a tool so it always returns a dict and never raises across the MCP boundary."""

    @functools.wraps(fn)
    async def wrapper(ctx: ServerContext, *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            result = await fn(ctx, *args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "%s failed: %s",
                fn.__name__,
                exc,
                exc_info=logger.isEnabledFor(logging.DEBUG),
            )
            return {"ok": False, "error": error_text(exc, client=ctx.client)}
        if "ok" not in result:
            return {"ok": True, **result}
        return result

    return wrapper


# ---------------------------------------------------------------------------------- context


class ServerContext:
    """Everything one server process owns: client, topology cache, safety gate, snapshots."""

    def __init__(
        self,
        client: UAClient,
        gate: SafetyGate | None = None,
        *,
        snapshots: SnapshotStore | None = None,
        rig_ttl_s: float = RIG_TTL_S,
        talkback_auto_off_s: float = TALKBACK_AUTO_OFF_S,
        talkback_retry_delays_s: Sequence[float] = TALKBACK_AUTO_OFF_RETRY_DELAYS_S,
        clock_verify_delay_s: float = CLOCK_VERIFY_DELAY_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.gate = gate if gate is not None else gate_from_env()
        self.snapshots = snapshots if snapshots is not None else SnapshotStore()
        self.rig_ttl_s = rig_ttl_s
        self.talkback_auto_off_s = talkback_auto_off_s
        self.talkback_retry_delays_s = tuple(talkback_retry_delays_s)
        self.clock_verify_delay_s = clock_verify_delay_s
        self._clock = clock
        self._rig: Rig | None = None
        self._rig_at = 0.0
        self._rig_lock = asyncio.Lock()
        #: Bumped by :meth:`invalidate`, so a walk that raced a reconnect can be discarded.
        self._rig_generation = 0
        #: How many times the topology has actually been walked (diagnostics + tests).
        self.discoveries = 0
        #: True when the talkback auto-off timer fired and could not turn talkback off.
        self.talkback_auto_off_failed = False
        self._catalog: list[dict[str, Any]] | None = None
        self._catalog_at = 0.0
        self._talkback_task: asyncio.Task[None] | None = None
        client.on_reconnect(self._on_reconnect)

    # ------------------------------------------------------------------- topology cache

    async def rig(self, *, refresh: bool = False) -> Rig:
        """The cached topology, rediscovered when stale, on request, or after a reconnect.

        A reconnect during the walk invalidates it: the result describes a mix of before and
        after, so it is thrown away and re-walked rather than cached with a stale timestamp.
        """
        async with self._rig_lock:
            fresh = self._rig is not None and (self._clock() - self._rig_at) <= self.rig_ttl_s
            if refresh or not fresh:
                settled = False
                for attempt in range(1, max(1, RIG_WALK_ATTEMPTS) + 1):
                    generation = self._rig_generation
                    walked = await discover(self.client)
                    self.discoveries += 1
                    settled = generation == self._rig_generation
                    if settled:
                        break
                    logger.info(
                        "topology was invalidated during walk %d/%d; discarding it and re-walking",
                        attempt,
                        RIG_WALK_ATTEMPTS,
                    )
                self._rig = walked
                # Stamped AFTER the walk: a long walk must not start out already half-expired.
                # A walk we never managed to take cleanly is served once and then re-walked,
                # never cached for the full TTL.
                self._rig_at = self._clock() if settled else float("-inf")
                if not settled:
                    logger.warning(
                        "topology kept being invalidated across %d walks; serving the last one "
                        "without caching it",
                        RIG_WALK_ATTEMPTS,
                    )
            assert self._rig is not None
            return self._rig

    @property
    def topology_stale(self) -> bool:
        """True when the next call to :meth:`rig` will re-walk the tree."""
        return self._rig is None or (self._clock() - self._rig_at) > self.rig_ttl_s

    def invalidate(self) -> None:
        """Drop the topology and plugin caches (reconnect, or a channel count that changed)."""
        self._rig = None
        self._catalog = None
        self._rig_generation += 1

    async def _on_reconnect(self) -> None:
        logger.info("control socket reconnected: dropping the topology cache")
        self.invalidate()

    async def aclose(self) -> None:
        """Cancel the talkback timer. The client is owned by whoever built it."""
        self.cancel_talkback_auto_off()
        task = self._talkback_task
        self._talkback_task = None
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # -------------------------------------------------------------------------- reads

    async def value(self, path: str, default: Any = None) -> Any:
        """Read one value, returning ``default`` instead of raising for a missing path."""
        try:
            return await self.client.get_value(path)
        except (UAPathError, UATimeoutError) as exc:
            logger.debug("read %s failed: %s", path, exc)
            return default

    async def poll(self, paths: Sequence[str]) -> dict[str, Any]:
        """Batch read on the poll socket, tolerating an empty request."""
        if not paths:
            return {}
        return await self.client.poll_values(list(paths))

    async def audio_flowing(self) -> tuple[bool, float, str | None]:
        """Is anything above -60 dB on any input meter? Returns ``(flowing, peak_db, where)``."""
        rig = await self.rig()
        inputs = [channel for channel in rig.channels if channel.kind == "input"]
        paths: dict[str, ChannelInfo] = {}
        for channel in inputs:
            for meter in (0, 1):
                paths[f"{channel.path}/meters/{meter}/MeterLevel"] = channel
        if not paths:
            return False, METER_FLOOR_DB, None
        values = await self.poll(list(paths))
        peak = METER_FLOOR_DB
        where: str | None = None
        for path, raw in values.items():
            if not isinstance(raw, int | float) or isinstance(raw, bool):
                continue
            level = float(raw)
            if level > peak:
                peak = level
                where = paths[path].name
        return peak > AUDIO_FLOWING_DB, peak, where

    # -------------------------------------------------------------------------- writes

    async def write(
        self,
        path: str,
        value: Any,
        *,
        context: WriteContext | str,
        prop: UAProperty | None = None,
    ) -> dict[str, Any]:
        """Read, gate, write and verify one property. The only write path in this module."""
        current = prop if prop is not None else await self.client.get_prop(path)
        name = property_name(path)
        warnings: list[str] = []
        gated = self.gate.check_write(current, value, context=context)
        if not values_equal(gated, value):
            warnings.append(
                f"safety policy and property limits changed the requested {value!r} to {gated!r}"
            )
        after = await self.client.set_value(path, gated)
        verified = values_equal(after, gated)
        if not verified:
            warnings.append(f"the engine reports {after!r} after writing {gated!r}")
        if name == MONITOR_LEVEL_PROPERTY and isinstance(after, int | float):
            self.gate.note_monitor_event(float(after))
        if path == TALKBACK_ON_PATH and verified and gated is False:
            # Talkback is shut, whoever shut it: a previously failed auto-off is history.
            self.talkback_auto_off_failed = False
        return {
            "path": path,
            "property": name,
            "verified": verified,
            "before": current.value,
            "after": after,
            "warnings": warnings,
        }

    async def write_many(
        self, requests: Sequence[tuple[str, Any, WriteContext | str]]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Apply writes in order, collecting per-write results and per-write error sentences."""
        writes: list[dict[str, Any]] = []
        errors: list[str] = []
        for path, value, context in requests:
            try:
                writes.append(await self.write(path, value, context=context))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                errors.append(error_text(exc, client=self.client))
        return writes, errors

    # ----------------------------------------------------------------------- talkback

    def arm_talkback_auto_off(self, seconds: float) -> None:
        """(Re)start the auto-off timer. A second call replaces the first one's deadline."""
        self.cancel_talkback_auto_off()
        self.talkback_auto_off_failed = False
        self._talkback_task = asyncio.create_task(
            self._talkback_auto_off(seconds), name="uad-talkback-auto-off"
        )

    def cancel_talkback_auto_off(self) -> None:
        task = self._talkback_task
        self._talkback_task = None
        if task is not None and not task.done():
            task.cancel()

    @property
    def talkback_timer_running(self) -> bool:
        task = self._talkback_task
        return task is not None and not task.done()

    async def _talkback_auto_off(self, seconds: float) -> None:
        """Close talkback after ``seconds``, retrying with backoff before giving up loudly.

        An open talkback mic is a live path into the control room, so a single failed write
        (a dropped socket, the rate limiter) must not silently end the attempt. If every retry
        fails, :attr:`talkback_auto_off_failed` is set and surfaced by ``uad_get_status``.
        """
        await asyncio.sleep(max(0.0, seconds))
        delays = self.talkback_retry_delays_s or (0.0,)
        for attempt, delay in enumerate(delays, start=1):
            try:
                await self.write(
                    TALKBACK_ON_PATH,
                    False,
                    context=WriteContext(reason="talkback auto-off timeout"),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "talkback auto-off attempt %d/%d failed: %s", attempt, len(delays), exc
                )
                if attempt < len(delays):
                    await asyncio.sleep(delay)
                continue
            logger.info("talkback auto-off fired after %.1fs (attempt %d)", seconds, attempt)
            self.talkback_auto_off_failed = False
            return
        # Before alarming, check whether the mic is even open: when the original TalkbackOn
        # write never reached the engine, every retry fails too, and flagging that case would
        # be a false alarm on the one flag an operator must be able to trust.
        try:
            still_on = bool(await self.client.get_value(TALKBACK_ON_PATH))
        except Exception:
            still_on = True  # cannot read it either: assume the worst
        self.talkback_auto_off_failed = still_on
        if still_on:
            logger.error(
                "talkback auto-off gave up after %d attempts: talkback may still be ON",
                len(delays),
            )
        else:
            logger.warning(
                "talkback auto-off could not write, but TalkbackOn already reads false; "
                "no alarm raised"
            )

    # ------------------------------------------------------------------ plugin catalog

    async def plugin_catalog(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        """The UAD plugin catalog, cached with the same TTL as the topology."""
        now = self._clock()
        fresh = self._catalog is not None and (now - self._catalog_at) <= self.rig_ttl_s
        if not refresh and fresh:
            assert self._catalog is not None
            return self._catalog
        _, children = await self.client.get_node("/plugins")
        fields = ("Name", "Categories", "Unison", "Authorized", "Status")
        paths = [f"/plugins/{child}/{field}" for child in children for field in fields]
        values = await self.poll(paths)
        catalog: list[dict[str, Any]] = []
        for child in children:
            name = values.get(f"/plugins/{child}/Name")
            if not name:
                continue
            catalog.append(
                {
                    "index": child,
                    "path": f"/plugins/{child}",
                    "name": str(name),
                    "categories": values.get(f"/plugins/{child}/Categories") or "",
                    "unison": bool(values.get(f"/plugins/{child}/Unison")),
                    "authorized": bool(values.get(f"/plugins/{child}/Authorized")),
                    "status": values.get(f"/plugins/{child}/Status") or "",
                }
            )
        catalog.sort(key=lambda item: item["name"].casefold())
        self._catalog = catalog
        self._catalog_at = now
        return catalog


# ------------------------------------------------------------------------- small helpers


def identity(channel: ChannelInfo) -> dict[str, Any]:
    """The resolved ``{device, index, name}`` identity every write result carries."""
    return {
        "device": channel.device,
        "index": channel.index,
        "name": channel.name,
        "kind": channel.kind,
        "path": channel.path,
    }


async def _resolve(
    ctx: ServerContext, target: str | int, device: str | int | None, kind: str = "input"
) -> ChannelInfo:
    rig = await ctx.rig()
    return rig.resolve(target, device=device, kind=kind)


def _channel_pool(
    rig: Rig, targets: Sequence[str | int] | None, kind: str, device: int | None
) -> list[ChannelInfo]:
    if targets:
        return [rig.resolve(target, device=device, kind=kind) for target in targets]
    return rig.channels_of(kind, device)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _item_error(item: Mapping[str, Any], message: str) -> dict[str, Any]:
    return {"target": item.get("target"), "ok": False, "error": message}


async def _monitor_rail_state(
    ctx: ServerContext, strip_path: str
) -> tuple[float | None, bool | None]:
    """``(stored level, DimOn)`` for a monitor strip. ``None`` for either means the read failed.

    A failed read is not the same as "no rail applies": the gate is told the strip IS a monitor
    (``is_monitor=True``) so a missing level refuses the write instead of waving it through.
    """
    level_path = f"{strip_path}/{MONITOR_LEVEL_PROPERTY}"
    dim_path = f"{strip_path}/{DIM_ON_PROPERTY}"
    values = await ctx.poll([level_path, dim_path])
    raw_dim = values.get(dim_path)
    return _number(values.get(level_path)), raw_dim if isinstance(raw_dim, bool) else None


_CHANNEL_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "Name"),
    ("fader_db", "FaderLevel"),
    ("mute", "Mute"),
    ("solo", "Solo"),
    ("solo_safe", "SoloSafe"),
    ("pan", "Pan"),
    ("pan2", "Pan2"),
    ("hidden", "ChannelHidden"),
    ("stereo", "Stereo"),
    ("enabled", "EnabledByUser"),
)
"""Tool-facing name -> engine property, in the order writes are applied."""


# ------------------------------------------------------------------------------- status


@tool
async def tool_get_status(ctx: ServerContext) -> dict[str, Any]:
    """Engine, clock, DSP, every device in the cascade, and the effective safety limits."""
    root, _ = await ctx.client.get_node("/")

    def root_value(name: str) -> Any:
        prop = root.get(name)
        return None if prop is None else prop.value

    rig = await ctx.rig()
    online = await ctx.poll([f"{device.path}/DeviceOnline" for device in rig.devices])
    devices = []
    for device in rig.devices:
        channels = [c for c in rig.channels if c.device == device.index]
        devices.append(
            {
                "device": device.index,
                "name": device.name,
                "type_id": device.type_id,
                "online": bool(online.get(f"{device.path}/DeviceOnline", device.online)),
                "multi_unit_order": device.multi_unit_order,
                "cue_buses": device.cue_bus_count,
                "channels": {
                    kind: len([c for c in channels if c.kind == kind])
                    for kind in ("input", "aux", "output")
                },
            }
        )
    return {
        "engine": {
            "connected": ctx.client.connected,
            "host": ctx.client.host,
            "port": ctx.client.port,
            "version": root_value("Version"),
            "protocol_version": root_value("ProtocolVersion"),
            "audio_streaming": root_value("AudioStreaming"),
        },
        "clock": {
            "source": root_value("ClockSource"),
            "locked": root_value("ClockLocked"),
            "sample_rate": root_value("SampleRate"),
            "max_delay_comp": root_value("MaxDelayComp"),
        },
        "dsp": {
            "total_dsp_load_percent": root_value("TotalDSPLoad"),
            "program_load_percent": root_value("TotalPGMLoad"),
            "memory_load_percent": root_value("TotalMEMLoad"),
        },
        "talkback": {
            "on": root_value("TalkbackOn"),
            "level": root_value("TalkbackMaster"),
            "auto_off_timer_running": ctx.talkback_timer_running,
            "auto_off_failed": ctx.talkback_auto_off_failed,
        },
        "devices": devices,
        "safety": ctx.gate.describe(),
    }


@tool
async def tool_list_channels(
    ctx: ServerContext,
    kind: str = "input",
    device: int | None = None,
    include_hidden: bool = True,
) -> dict[str, Any]:
    """Every strip across every unit, grouped by device, with its live state."""
    rig = await ctx.rig()
    kinds = ("input", "aux", "output") if kind in ("all", "any", "") else (kind,)
    channels = [c for k in kinds for c in rig.channels_of(k, device)]
    fields = (
        ("name", "Name"),
        ("fader_db", "FaderLevel"),
        ("mute", "Mute"),
        ("solo", "Solo"),
        ("solo_safe", "SoloSafe"),
        ("pan", "Pan"),
        ("hidden", "ChannelHidden"),
        ("stereo", "Stereo"),
    )
    monitor_field = ("monitor_level_db", MONITOR_LEVEL_PROPERTY)
    # Only control-room outputs carry CRMonitorLevel; asking every strip for it wastes a whole
    # extra poll entry per channel (56 on an x8) that can never answer.
    values = await ctx.poll(
        [f"{c.path}/{prop}" for c in channels for _, prop in fields]
        + [f"{c.path}/{monitor_field[1]}" for c in channels if c.is_monitor]
    )
    grouped: dict[int, list[dict[str, Any]]] = {}
    for channel in channels:
        wanted = (*fields, monitor_field) if channel.is_monitor else fields
        live = {key: values.get(f"{channel.path}/{prop}") for key, prop in wanted}
        if not include_hidden and live.get("hidden"):
            continue
        entry: dict[str, Any] = {
            **identity(channel),
            "io_type": channel.io_type,
            "has_preamp": channel.has_preamp,
            "is_monitor": channel.is_monitor,
            "is_talkback": channel.is_talkback,
            "active": channel.active,
        }
        for key, value in live.items():
            if value is not None or key in ("fader_db", "mute", "pan"):
                entry[key] = value
        entry["name"] = live.get("name") or channel.name
        grouped.setdefault(channel.device, []).append(entry)

    return {
        "devices": [
            {
                "device": device_info.index,
                "name": device_info.name,
                "type_id": device_info.type_id,
                "online": device_info.online,
                "channels": grouped.get(device_info.index, []),
            }
            for device_info in rig.devices
        ],
        "total": sum(len(items) for items in grouped.values()),
    }


# ------------------------------------------------------------------------------ channels


@tool
async def tool_set_channels(ctx: ServerContext, items: list[dict[str, Any]]) -> dict[str, Any]:
    """Batch channel writes: fader, pan, mute, solo, rename, hide, per item."""
    if not items:
        return {"results": [], "error": "items was empty; nothing to set."}
    rig = await ctx.rig()
    results: list[dict[str, Any]] = []
    for item in items:
        if "target" not in item:
            results.append(
                _item_error(item, "every item needs a 'target' (channel name or index).")
            )
            continue
        try:
            channel = rig.resolve(
                item["target"], device=item.get("device"), kind=str(item.get("kind", "input"))
            )
        except (UAError, ValueError) as exc:
            results.append(_item_error(item, error_text(exc, client=ctx.client)))
            continue
        # A monitor strip reached through this tool must still meet the monitor rail: an
        # unmute here is exactly as loud as an unmute through uad_set_monitor. is_monitor makes
        # a failed level read a refusal instead of a free pass.
        monitor_level = (
            _number(await ctx.value(f"{channel.path}/{MONITOR_LEVEL_PROPERTY}"))
            if channel.is_monitor
            else None
        )
        requests: list[tuple[str, Any, WriteContext | str]] = []
        for key, prop in _CHANNEL_FIELDS:
            if item.get(key) is None:
                continue
            context = WriteContext(
                reason=f"uad_set_channels {key} on {channel.label()}",
                monitor_level_db=monitor_level,
                is_monitor=channel.is_monitor,
            )
            requests.append((f"{channel.path}/{prop}", item[key], context))
        if not requests:
            results.append(
                {
                    "target": item.get("target"),
                    "resolved": identity(channel),
                    "ok": False,
                    "error": (
                        "nothing to change: pass at least one of "
                        + ", ".join(key for key, _ in _CHANNEL_FIELDS)
                        + "."
                    ),
                }
            )
            continue
        writes, errors = await ctx.write_many(requests)
        for write in writes:
            # Keep the cached topology in sync with a verified rename, otherwise the new
            # name is unresolvable until the rig TTL expires.
            if write["property"] == "Name" and write["verified"]:
                try:
                    index = rig.channels.index(channel)
                except ValueError:
                    ctx.invalidate()
                else:
                    rig.channels[index] = dataclasses.replace(
                        channel, name=str(write["after"])
                    )
        results.append(
            {
                "target": item.get("target"),
                "resolved": identity(channel),
                "ok": not errors,
                "writes": writes,
                **({"errors": errors} if errors else {}),
            }
        )
    return {"results": results, "ok": all(result.get("ok") for result in results)}


@tool
async def tool_set_preamp(
    ctx: ServerContext,
    target: str | int,
    device: str | int | None = None,
    gain_db: float | None = None,
    pad: bool | None = None,
    low_cut: bool | None = None,
    phase: bool | None = None,
    hi_z: bool | None = None,
    source: str | None = None,
    phantom_48v: bool | None = None,
    confirm_condenser_mic: bool = False,
) -> dict[str, Any]:
    """Preamp controls for one input. 48V needs confirm_condenser_mic=true."""
    channel = await _resolve(ctx, target, device)
    if not channel.has_preamp:
        raise UAError(
            f"{channel.label()} has no preamp, so gain, pad, 48V and Hi-Z do not exist on it. "
            "Use uad_list_channels and pick a channel with has_preamp true."
        )
    preamp = f"{channel.path}/preamps/0"
    requests: list[tuple[str, Any, WriteContext | str]] = []
    if source is not None:
        requests.append(
            (
                f"{channel.path}/IOType",
                source,
                WriteContext(reason=f"uad_set_preamp source on {channel.label()}"),
            )
        )
    for value, prop in (
        (gain_db, "Gain"),
        (pad, "Pad"),
        (low_cut, "LowCut"),
        (phase, "Phase"),
        (hi_z, "HiZ"),
    ):
        if value is not None:
            requests.append(
                (
                    f"{preamp}/{prop}",
                    value,
                    WriteContext(reason=f"uad_set_preamp {prop} on {channel.label()}"),
                )
            )
    if phantom_48v is not None:
        requests.append(
            (
                f"{preamp}/48V",
                phantom_48v,
                WriteContext(
                    reason=f"uad_set_preamp 48V on {channel.label()}",
                    confirm_condenser_mic=confirm_condenser_mic,
                ),
            )
        )
    if not requests:
        raise ValueError(
            "nothing to change: pass at least one of gain_db, pad, low_cut, phase, hi_z, "
            "source or phantom_48v."
        )
    writes, errors = await ctx.write_many(requests)
    return {
        "ok": not errors,
        "resolved": identity(channel),
        "writes": writes,
        **({"errors": errors} if errors else {}),
    }


async def _send_index(ctx: ServerContext, channel: ChannelInfo, send: str | int) -> tuple[str, str]:
    """Resolve ``send`` ("aux1", "cue2", 0, ...) to ``(send path, its ID)`` on this channel."""
    _, children = await ctx.client.get_node(f"{channel.path}/sends")
    ids = await ctx.poll([f"{channel.path}/sends/{child}/ID" for child in children])
    known = {
        str(ids.get(f"{channel.path}/sends/{child}/ID") or "").casefold(): child
        for child in children
    }
    wanted = str(send).strip().casefold()
    if wanted in known:
        child = known[wanted]
    elif wanted.isdigit() and wanted in {c.casefold() for c in children}:
        child = wanted
    else:
        available = ", ".join(sorted(name for name in known if name)) or "none"
        raise TargetNotFoundError(
            send,
            "send",
            message=(
                f"{channel.label()} has no send {send!r}. Available send IDs: {available} "
                "(or pass the send index)."
            ),
        )
    path = f"{channel.path}/sends/{child}"
    return path, str(ids.get(f"{path}/ID") or child)


@tool
async def tool_set_sends(ctx: ServerContext, items: list[dict[str, Any]]) -> dict[str, Any]:
    """Batch send writes: a channel's level, pan or bypass into an aux or cue bus."""
    if not items:
        return {"results": [], "error": "items was empty; nothing to set."}
    rig = await ctx.rig()
    results: list[dict[str, Any]] = []
    for item in items:
        if "target" not in item or "send" not in item:
            results.append(
                _item_error(item, "every item needs a 'target' and a 'send' (e.g. 'cue1').")
            )
            continue
        try:
            channel = rig.resolve(
                item["target"], device=item.get("device"), kind=str(item.get("kind", "input"))
            )
            send_path, send_id = await _send_index(ctx, channel, item["send"])
        except (UAError, ValueError) as exc:
            results.append(_item_error(item, error_text(exc, client=ctx.client)))
            continue
        requests: list[tuple[str, Any, WriteContext | str]] = []
        for key, prop in (("level_db", "Gain"), ("pan", "Pan"), ("bypass", "Bypass")):
            if item.get(key) is not None:
                requests.append(
                    (
                        f"{send_path}/{prop}",
                        item[key],
                        WriteContext(reason=f"uad_set_sends {key} {send_id} on {channel.label()}"),
                    )
                )
        if not requests:
            results.append(
                _item_error(item, "nothing to change: pass level_db, pan or bypass.")
            )
            continue
        writes, errors = await ctx.write_many(requests)
        # cue3/cue4 send nodes exist in the tree even on devices with fewer cue buses; a
        # write there "succeeds" against a bus the hardware does not have. Warn, loudly.
        phantom_cue = None
        if send_id.startswith("cue") and send_id[3:].isdigit():
            cue_count = rig.device(channel.device).cue_bus_count
            if int(send_id[3:]) > cue_count:
                phantom_cue = (
                    f"{send_id} was written and verified, but this device reports only "
                    f"{cue_count} cue bus(es), so {send_id} does not reach any real output. "
                    "Use cue1"
                    + (f"..cue{cue_count}." if cue_count > 1 else " only.")
                )
        if phantom_cue:
            for write in writes:
                write["warnings"] = [*write["warnings"], phantom_cue]
        results.append(
            {
                "target": item.get("target"),
                "send": send_id,
                "resolved": identity(channel),
                "ok": not errors,
                "writes": writes,
                **({"errors": errors} if errors else {}),
            }
        )
    return {"results": results, "ok": all(result.get("ok") for result in results)}


# ------------------------------------------------------------------------------- monitor


@tool
async def tool_set_monitor(
    ctx: ServerContext,
    device: int | None = None,
    level_db: float | None = None,
    dim: bool | None = None,
    mute: bool | None = None,
    mono: bool | None = None,
    alt_speaker: int | None = None,
    dim_attenuation_db: int | None = None,
) -> dict[str, Any]:
    """Control-room monitor: level (railed), dim, mute, mono fold, alt speaker set."""
    if all(
        value is None
        for value in (level_db, dim, mute, mono, alt_speaker, dim_attenuation_db)
    ):
        raise ValueError(
            "nothing to change: pass level_db, dim, mute, mono, alt_speaker or "
            "dim_attenuation_db."
        )
    rig = await ctx.rig()
    monitor = rig.monitor_output(device)
    stored, dim_on = await _monitor_rail_state(ctx, monitor.path)

    writes: list[dict[str, Any]] = []
    errors: list[str] = []

    # Level FIRST, and on its own: everything below is railed against the level the engine
    # actually confirmed, never against the level that was merely requested.
    applied_level = stored
    release_blocked: str | None = None
    if level_db is not None:
        level_writes, level_errors = await ctx.write_many(
            [
                (
                    f"{monitor.path}/{MONITOR_LEVEL_PROPERTY}",
                    level_db,
                    WriteContext(reason="uad_set_monitor level", is_monitor=True),
                )
            ]
        )
        writes.extend(level_writes)
        errors.extend(level_errors)
        write = level_writes[0] if level_writes else None
        # The level to rail against is the one the ENGINE reports back, not the one that was
        # asked for: that catches an engine which applied something louder, and it does not
        # block a legitimate quiet unmute just because the engine quantized -35.0 to -34.996.
        applied_level = _number(write["after"]) if write is not None else None
        if applied_level is None:
            release_blocked = (
                "the monitor level write did not land"
                if write is None
                else f"the engine reported no usable monitor level ({write['after']!r})"
            )

    requests: list[tuple[str, Any, WriteContext | str]] = []
    if mono is not None:
        requests.append(
            (
                f"{monitor.path}/MixToMono",
                mono,
                WriteContext(reason="uad_set_monitor mono"),
            )
        )
    if dim_attenuation_db is not None:
        # Lowering the attenuation while dim is engaged is an un-dim by another name, so the
        # gate gets the confirmed level and the live DimOn state to rail it against.
        requests.append(
            (
                f"/devices/{monitor.device}/{DIM_ATTENUATION_PROPERTY}",
                dim_attenuation_db,
                WriteContext(
                    reason="uad_set_monitor dim attenuation",
                    monitor_level_db=applied_level,
                    dim_on=dim_on,
                    is_monitor=True,
                ),
            )
        )
    if alt_speaker is not None:
        requests.append(
            (
                f"/devices/{monitor.device}/AltMonSelection",
                alt_speaker,
                WriteContext(reason="uad_set_monitor alt speaker"),
            )
        )
    more_writes, more_errors = await ctx.write_many(requests)
    writes.extend(more_writes)
    errors.extend(more_errors)

    for value, prop in ((dim, DIM_ON_PROPERTY), (mute, "Mute")):
        if value is None:
            continue
        if value is False and release_blocked is not None:
            errors.append(
                f"Skipped releasing {prop} on {monitor.label()}: {release_blocked}, so the level "
                "this release would jump straight to is unknown. Set a level_db that verifies "
                f"first, then release {prop} in a second call."
            )
            continue
        release_writes, release_errors = await ctx.write_many(
            [
                (
                    f"{monitor.path}/{prop}",
                    value,
                    WriteContext(
                        reason=f"uad_set_monitor {prop}",
                        monitor_level_db=applied_level,
                        is_monitor=True,
                    ),
                )
            ]
        )
        writes.extend(release_writes)
        errors.extend(release_errors)

    return {
        "ok": not errors,
        "resolved": identity(monitor),
        "stored_level_db": stored,
        "applied_level_db": applied_level,
        "dim_on": dim_on,
        "ceiling_db": ctx.gate.max_monitor_db,
        "budget_remaining_db": ctx.gate.monitor_budget_remaining_db(),
        "writes": writes,
        **({"errors": errors} if errors else {}),
    }


@tool
async def tool_talkback(
    ctx: ServerContext,
    on: bool | None = None,
    level: int | None = None,
    auto_off_s: float | None = None,
) -> dict[str, Any]:
    """Talkback on/off and level, with an auto-off timer so it cannot be left open."""
    if on is None and level is None:
        raise ValueError("nothing to change: pass on=true/false and/or level (0-100).")
    writes: list[dict[str, Any]] = []
    errors: list[str] = []
    if level is not None:
        level_writes, level_errors = await ctx.write_many(
            [(TALKBACK_MASTER_PATH, level, WriteContext(reason="uad_talkback level"))]
        )
        writes.extend(level_writes)
        errors.extend(level_errors)
    # The timer is armed on the TalkbackOn write's OWN outcome, and it is armed unless the gate
    # REFUSED the write - the only case where talkback provably never opened. A level write that
    # failed, or an ack that never came back, must not leave an open mic with no timer behind it;
    # arming when talkback is in fact off costs one redundant write of False.
    refused_by_policy = False
    if on is not None:
        try:
            writes.append(
                await ctx.write(
                    TALKBACK_ON_PATH, on, context=WriteContext(reason="uad_talkback on/off")
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            refused_by_policy = isinstance(exc, SafetyRefusal)
            errors.append(error_text(exc, client=ctx.client))

    timeout = ctx.talkback_auto_off_s if auto_off_s is None else max(0.0, float(auto_off_s))
    armed = False
    if on is True and not refused_by_policy and timeout > 0:
        ctx.arm_talkback_auto_off(timeout)
        armed = True
    elif on is False:
        ctx.cancel_talkback_auto_off()
    return {
        "ok": not errors,
        "writes": writes,
        "auto_off_s": timeout if armed else None,
        "auto_off_armed": armed,
        **({"errors": errors} if errors else {}),
    }


@tool
async def tool_safe_state(ctx: ServerContext) -> dict[str, Any]:
    """The panic button: mute every monitor output and turn talkback off. No parameters."""
    rig = await ctx.rig()
    monitors = [c for c in rig.channels if c.kind == "output" and c.is_monitor]
    requests: list[tuple[str, Any, WriteContext | str]] = [
        (f"{monitor.path}/Mute", True, WriteContext(reason="uad_safe_state monitor mute"))
        for monitor in monitors
    ]
    requests.append(
        (TALKBACK_ON_PATH, False, WriteContext(reason="uad_safe_state talkback off"))
    )
    ctx.cancel_talkback_auto_off()
    writes, errors = await ctx.write_many(requests)
    return {
        "ok": not errors,
        "monitors_muted": [identity(monitor) for monitor in monitors],
        "writes": writes,
        **({"errors": errors} if errors else {}),
        "next": (
            "Monitors are muted. Find the runaway channel with uad_list_channels, mute it, then "
            "release the monitor mute with uad_set_monitor(mute=false)."
        ),
    }


# --------------------------------------------------------------------------------- clock


@tool
async def tool_set_clock(ctx: ServerContext, source: str, confirm: bool = False) -> dict[str, Any]:
    """Change the clock source, then verify lock and revert automatically if it fails."""
    prop = await ctx.client.get_prop("/ClockSource")
    previous = prop.value
    flowing, peak, where = await ctx.audio_flowing()
    context = WriteContext(
        reason=f"uad_set_clock to {source!r}",
        confirm=confirm,
        audio_flowing=flowing,
    )
    write = await ctx.write("/ClockSource", source, context=context, prop=prop)
    await asyncio.sleep(ctx.clock_verify_delay_s)
    locked = await ctx.value("/ClockLocked")
    result: dict[str, Any] = {
        "resolved": {"path": "/ClockSource", "before": previous, "after": write["after"]},
        "write": write,
        "locked": bool(locked),
        "audio_check": {"flowing": flowing, "peak_db": peak, "loudest_channel": where},
        "reverted": False,
    }
    if locked:
        result["ok"] = True
        return result
    # The revert is best-effort: if IT fails, the populated result must still come back, saying
    # plainly that the rig was left on an unlocked clock source.
    try:
        revert = await ctx.write(
            "/ClockSource",
            previous,
            context=WriteContext(
                reason="auto-revert: the new clock source never locked",
                confirm=True,
                audio_flowing=False,
            ),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        result.update(
            ok=False,
            reverted=False,
            revert_error=error_text(exc, client=ctx.client),
            error=(
                f"ClockLocked was still false {ctx.clock_verify_delay_s:g}s after switching to "
                f"{write['after']!r}, AND the automatic revert to {previous!r} failed, so the "
                f"clock source is left at {write['after']!r} and unlocked. Fix the engine "
                f"connection, then set the clock back with "
                f"uad_set_clock(source={previous!r}, confirm=true)."
            ),
        )
        return result
    # The revert itself landed. Re-reading ClockLocked is a separate, weaker claim: if that read
    # fails we still reverted, so say "unknown" rather than mislabelling the revert as failed.
    locked_after_revert: bool | None = None
    try:
        await asyncio.sleep(ctx.clock_verify_delay_s)
        locked_after_revert = bool(await ctx.value("/ClockLocked"))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("could not re-read ClockLocked after reverting: %s", exc)
    result.update(
        ok=False,
        reverted=True,
        revert=revert,
        locked_after_revert=locked_after_revert,
        error=(
            f"ClockLocked was still false {ctx.clock_verify_delay_s:g}s after switching to "
            f"{write['after']!r}, so the clock source was reverted to {previous!r}. Check that "
            "the external clock device is powered on, connected, and running at the same sample "
            "rate as this Apollo (see the troubleshoot_clock prompt)."
        ),
    )
    return result


@tool
async def tool_set_sample_rate(
    ctx: ServerContext, rate: int, confirm: bool = False
) -> dict[str, Any]:
    """Change the system sample rate. Requires confirm=true and silent inputs."""
    prop = await ctx.client.get_prop("/SampleRate")
    previous = prop.value
    flowing, peak, where = await ctx.audio_flowing()
    write = await ctx.write(
        "/SampleRate",
        rate,
        context=WriteContext(
            reason=f"uad_set_sample_rate to {rate}", confirm=confirm, audio_flowing=flowing
        ),
        prop=prop,
    )
    await asyncio.sleep(ctx.clock_verify_delay_s)
    locked = bool(await ctx.value("/ClockLocked"))
    warnings = list(write["warnings"])
    if not locked:
        warnings.append(
            "ClockLocked is false at the new rate: if this Apollo is slaved to an external "
            "clock, that device must be switched to the same rate too."
        )
    return {
        "ok": True,
        "resolved": {"path": "/SampleRate", "before": previous, "after": write["after"]},
        "write": {**write, "warnings": warnings},
        "locked": locked,
        "audio_check": {"flowing": flowing, "peak_db": peak, "loudest_channel": where},
    }


# ------------------------------------------------------------------------------- plugins


def _matches_plugin(entry: Mapping[str, Any], needle: str) -> bool:
    return needle in entry["name"].casefold() or needle in str(entry["categories"]).casefold()


@tool
async def tool_list_plugins(
    ctx: ServerContext,
    search: str | None = None,
    category: str | None = None,
    unison_only: bool = False,
    authorized_only: bool = False,
    limit: int = PLUGIN_LIST_LIMIT,
) -> dict[str, Any]:
    """The installed UAD plugin catalog, filterable by name, category and authorization."""
    catalog = await ctx.plugin_catalog()
    selected = catalog
    if search:
        needle = search.strip().casefold()
        selected = [entry for entry in selected if _matches_plugin(entry, needle)]
    if category:
        needle = category.strip().casefold()
        selected = [entry for entry in selected if needle in str(entry["categories"]).casefold()]
    if unison_only:
        selected = [entry for entry in selected if entry["unison"]]
    if authorized_only:
        selected = [entry for entry in selected if entry["authorized"]]
    limited = selected[: max(1, limit)]
    categories = sorted(
        {
            part.strip()
            for entry in catalog
            for part in str(entry["categories"]).split(",")
            if part.strip()
        }
    )
    return {
        "plugins": [
            {key: entry[key] for key in ("name", "categories", "unison", "authorized", "status")}
            for entry in limited
        ],
        "shown": len(limited),
        "matched": len(selected),
        "installed": len(catalog),
        "truncated": len(selected) - len(limited),
        "categories": categories,
    }


def _slot_path(channel: ChannelInfo, slot: str | int) -> str:
    text = str(slot).strip().casefold()
    if text in (UNISON_SLOT, "preamp"):
        if not channel.has_preamp:
            raise UAError(
                f"{channel.label()} has no preamp, so it has no Unison slot. Unison lives on "
                "mic/line preamp channels only (has_preamp true in uad_list_channels)."
            )
        return f"{channel.path}/preamps/0/effects/0"
    if not text.lstrip("+-").isdigit():
        raise ValueError(
            f"slot must be an insert slot number or {UNISON_SLOT!r}, got {slot!r}."
        )
    return f"{channel.path}/effects/{int(text)}"


async def _insert_slots(ctx: ServerContext, channel: ChannelInfo) -> list[str]:
    _, children = await ctx.client.get_node(f"{channel.path}/effects")
    return sorted(children, key=lambda name: int(name) if name.isdigit() else 0)


@tool
async def tool_get_inserts(
    ctx: ServerContext,
    target: str | int | None = None,
    device: int | None = None,
    kind: str = "input",
    loaded_only: bool = True,
) -> dict[str, Any]:
    """What is loaded where: every insert slot plus the Unison slot, across channels."""
    rig = await ctx.rig()
    channels = _channel_pool(rig, [target] if target is not None else None, kind, device)
    rows: list[dict[str, Any]] = []
    for channel in channels:
        slots: list[tuple[str, str]] = [
            (name, f"{channel.path}/effects/{name}") for name in await _insert_slots(ctx, channel)
        ]
        if channel.has_preamp:
            slots.append((UNISON_SLOT, f"{channel.path}/preamps/0/effects/0"))
        values = await ctx.poll(
            [f"{path}/{prop}" for _, path in slots for prop in ("EffectName", "Power", "Preset")]
        )
        for name, path in slots:
            effect = values.get(f"{path}/EffectName")
            if loaded_only and not effect:
                continue
            rows.append(
                {
                    **identity(channel),
                    "slot": name,
                    "slot_path": path,
                    "plugin": effect or None,
                    "power": values.get(f"{path}/Power"),
                    "preset": values.get(f"{path}/Preset") or None,
                }
            )
    return {"inserts": rows, "channels_checked": len(channels), "loaded_only": loaded_only}


async def _match_plugin(ctx: ServerContext, plugin: str) -> dict[str, Any]:
    catalog = await ctx.plugin_catalog()
    wanted = plugin.strip().casefold()
    for predicate in (
        lambda name: name == wanted,
        lambda name: name.startswith(wanted),
        lambda name: wanted in name,
    ):
        hits = [entry for entry in catalog if predicate(entry["name"].casefold())]
        if len(hits) == 1:
            return hits[0]
        if hits:
            shown = ", ".join(entry["name"] for entry in hits[:8])
            raise UAError(
                f"{plugin!r} matches {len(hits)} installed plugins: {shown}"
                f"{', ...' if len(hits) > 8 else ''}. Pass the full plugin name."
            )
    raise TargetNotFoundError(
        plugin,
        "plugin",
        message=(
            f"No installed UAD plugin matches {plugin!r}. Use uad_list_plugins(search=...) to "
            "find the exact name."
        ),
    )


@tool
async def tool_load_plugin(
    ctx: ServerContext,
    target: str | int,
    plugin: str,
    slot: str | int = 0,
    device: str | int | None = None,
    preset: str | None = None,
    power: bool | None = None,
) -> dict[str, Any]:
    """Load a UAD plugin into an insert slot or the Unison slot, then wait for its parameters."""
    channel = await _resolve(ctx, target, device)
    path = _slot_path(channel, slot)
    entry = await _match_plugin(ctx, plugin)
    if str(slot).strip().casefold() == UNISON_SLOT and not entry["unison"]:
        raise UAError(
            f"{entry['name']!r} is not a Unison plugin, so it cannot go in the Unison slot. "
            "Use uad_list_plugins(unison_only=true) to see which ones can."
        )
    write = await ctx.write(
        f"{path}/EffectName",
        entry["name"],
        context=WriteContext(reason=f"uad_load_plugin {entry['name']!r} into {path}"),
    )
    parameters = await snapshot.wait_for_parameters(ctx.client, path)
    warnings = list(write["warnings"])
    if not entry["authorized"]:
        warnings.append(
            f"{entry['name']!r} reports authorized=false (status {entry['status']!r}); it will "
            "load but may not process audio."
        )
    if not parameters:
        warnings.append(
            f"parameter nodes did not appear at {path}/parameters within "
            f"{snapshot.MATERIALIZE_TIMEOUT_S:g}s, so uad_set_plugin_params will not find "
            "anything yet. Re-run uad_get_plugin_params in a moment."
        )
    extra: list[tuple[str, Any, WriteContext | str]] = []
    if preset is not None:
        extra.append(
            (f"{path}/Preset", preset, WriteContext(reason="uad_load_plugin preset recall"))
        )
    if power is not None:
        extra.append((f"{path}/Power", power, WriteContext(reason="uad_load_plugin power")))
    writes, errors = await ctx.write_many(extra)
    return {
        "ok": not errors,
        "resolved": identity(channel),
        "slot": str(slot),
        "slot_path": path,
        "plugin": entry["name"],
        "materialized": bool(parameters),
        "parameter_count": len(parameters),
        "writes": [{**write, "warnings": warnings}, *writes],
        **({"errors": errors} if errors else {}),
    }


@tool
async def tool_remove_plugin(
    ctx: ServerContext,
    target: str | int,
    slot: str | int = 0,
    device: str | int | None = None,
) -> dict[str, Any]:
    """Clear an insert or Unison slot by writing an empty EffectName."""
    channel = await _resolve(ctx, target, device)
    path = _slot_path(channel, slot)
    write = await ctx.write(
        f"{path}/EffectName", "", context=WriteContext(reason=f"uad_remove_plugin {path}")
    )
    return {
        "resolved": identity(channel),
        "slot": str(slot),
        "slot_path": path,
        "removed": write["before"] or None,
        "writes": [write],
    }


async def _parameters(ctx: ServerContext, path: str) -> list[dict[str, Any]]:
    try:
        _, children = await ctx.client.get_node(f"{path}/parameters")
    except UAPathError:
        return []
    values = await ctx.poll(
        [
            f"{path}/parameters/{child}/{prop}"
            for child in children
            for prop in ("Name", "StringValue", "NormalizedValue")
        ]
    )
    parameters: list[dict[str, Any]] = []
    for child in sorted(children, key=lambda name: int(name) if name.isdigit() else 0):
        base = f"{path}/parameters/{child}"
        parameters.append(
            {
                "index": child,
                "name": values.get(f"{base}/Name"),
                "display": values.get(f"{base}/StringValue"),
                "normalized": values.get(f"{base}/NormalizedValue"),
                "path": base,
            }
        )
    return parameters


@tool
async def tool_get_plugin_params(
    ctx: ServerContext,
    target: str | int,
    slot: str | int = 0,
    device: str | int | None = None,
) -> dict[str, Any]:
    """List a loaded plugin's parameters with their normalized and displayed values."""
    channel = await _resolve(ctx, target, device)
    path = _slot_path(channel, slot)
    effect = await ctx.value(f"{path}/EffectName")
    parameters = await _parameters(ctx, path)
    result: dict[str, Any] = {
        "resolved": identity(channel),
        "slot": str(slot),
        "slot_path": path,
        "plugin": effect or None,
        "parameters": parameters,
    }
    if not effect:
        result["note"] = (
            f"{path} is empty, so it has no parameters. Load a plugin with uad_load_plugin first."
        )
    elif not parameters:
        result["note"] = (
            "the plugin is loaded but its parameter nodes have not materialized yet; they only "
            "appear once the engine has finished instantiating it."
        )
    return result


@tool
async def tool_set_plugin_params(
    ctx: ServerContext,
    target: str | int,
    params: list[dict[str, Any]],
    slot: str | int = 0,
    device: str | int | None = None,
) -> dict[str, Any]:
    """Batch-set a loaded plugin's parameters by name, using normalized 0-1 values."""
    channel = await _resolve(ctx, target, device)
    path = _slot_path(channel, slot)
    if not params:
        raise ValueError("params was empty; pass [{'name': ..., 'normalized': 0..1}, ...].")
    known = await _parameters(ctx, path)
    if not known:
        raise UAError(
            f"{path} exposes no parameters yet (is a plugin loaded there?). Check with "
            "uad_get_plugin_params."
        )
    by_name = {str(entry["name"] or "").casefold(): entry for entry in known}
    results: list[dict[str, Any]] = []
    for item in params:
        name = str(item.get("name", "")).strip()
        entry = by_name.get(name.casefold())
        if entry is None:
            shown = ", ".join(str(other["name"]) for other in known[:12])
            results.append(
                {
                    "name": item.get("name"),
                    "ok": False,
                    "error": f"{name!r} is not a parameter of this plugin. It has: {shown}.",
                }
            )
            continue
        value = item.get("normalized", item.get("value"))
        if value is None:
            results.append(
                {
                    "name": name,
                    "ok": False,
                    "error": "pass 'normalized' (0-1) for this parameter.",
                }
            )
            continue
        writes, errors = await ctx.write_many(
            [
                (
                    f"{entry['path']}/NormalizedValue",
                    value,
                    WriteContext(reason=f"uad_set_plugin_params {name!r}"),
                )
            ]
        )
        display = await ctx.value(f"{entry['path']}/StringValue")
        results.append(
            {
                "name": name,
                "ok": not errors,
                "display": display,
                "writes": writes,
                **({"errors": errors} if errors else {}),
            }
        )
    return {
        "ok": all(result["ok"] for result in results),
        "resolved": identity(channel),
        "slot": str(slot),
        "slot_path": path,
        "results": results,
    }


def _flatten_presets(options: Iterable[Any]) -> list[str]:
    names: list[str] = []
    for option in options:
        if isinstance(option, dict):
            value = option.get("value")
            if value is not None and not option.get("children"):
                names.append(str(value))
            names.extend(_flatten_presets(option.get("children") or []))
        elif option is not None:
            names.append(str(option))
    return names


@tool
async def tool_plugin_preset(
    ctx: ServerContext,
    target: str | int,
    preset: str,
    slot: str | int = 0,
    device: str | int | None = None,
) -> dict[str, Any]:
    """Recall a factory or user preset by name into a loaded plugin slot."""
    channel = await _resolve(ctx, target, device)
    path = _slot_path(channel, slot)
    effect = await ctx.value(f"{path}/EffectName")
    if not effect:
        raise UAError(
            f"{path} is empty, so there is nothing to recall a preset into. Load a plugin with "
            "uad_load_plugin first."
        )
    warnings: list[str] = []
    catalog = await ctx.plugin_catalog()
    entry = next((item for item in catalog if item["name"] == effect), None)
    if entry is not None:
        try:
            prop = await ctx.client.get_prop(f"{entry['path']}/Preset")
            available = _flatten_presets(prop.values or [])
        except UAError:
            available = []
        if available and preset not in available:
            close = ", ".join(available[:10])
            raise TargetNotFoundError(
                preset,
                "preset",
                message=(
                    f"{effect!r} has no preset named {preset!r}. Its preset browser lists: "
                    f"{close}{' ...' if len(available) > 10 else ''}."
                ),
            )
        if not available:
            warnings.append(
                "the engine did not list this plugin's presets, so the name could not be "
                "checked before writing it."
            )
    write = await ctx.write(
        f"{path}/Preset", preset, context=WriteContext(reason=f"uad_plugin_preset {preset!r}")
    )
    return {
        "resolved": identity(channel),
        "slot": str(slot),
        "slot_path": path,
        "plugin": effect,
        "writes": [{**write, "warnings": [*write["warnings"], *warnings]}],
    }


# -------------------------------------------------------------------------------- meters


def _meter_paths(channels: Sequence[ChannelInfo]) -> list[str]:
    """Both meters of every channel. Mono strips simply do not answer for meter 1."""
    return [
        f"{channel.path}/meters/{meter}/{prop}"
        for channel in channels
        for meter in (0, 1)
        for prop in ("MeterLevel", "MeterPeakLevel", "MeterClip")
    ]


@tool
async def tool_peek_meters(
    ctx: ServerContext,
    targets: list[str] | None = None,
    kind: str = "input",
    device: int | None = None,
    above_db: float | None = None,
) -> dict[str, Any]:
    """One-shot meter read across channels, with the floor called out explicitly."""
    rig = await ctx.rig()
    channels = _channel_pool(rig, targets, kind, device)
    values = await ctx.poll(_meter_paths(channels))
    rows: list[dict[str, Any]] = []
    for channel in channels:
        levels = [
            _number(values.get(f"{channel.path}/meters/{meter}/MeterLevel")) for meter in (0, 1)
        ]
        peaks = [
            _number(values.get(f"{channel.path}/meters/{meter}/MeterPeakLevel"))
            for meter in (0, 1)
        ]
        present = [level for level in levels if level is not None]
        if not present:
            continue
        level = max(present)
        if above_db is not None and level <= above_db:
            continue
        rows.append(
            {
                **identity(channel),
                "io_type": channel.io_type,
                "level_db": level,
                "peak_db": max([peak for peak in peaks if peak is not None], default=None),
                "clip": any(
                    bool(values.get(f"{channel.path}/meters/{meter}/MeterClip"))
                    for meter in (0, 1)
                ),
                "at_floor": level <= METER_FLOOR_DB + METER_FLOOR_TOLERANCE_DB,
            }
        )
    at_floor = [row for row in rows if row["at_floor"]]
    return {
        "meters": rows,
        "floor_db": METER_FLOOR_DB,
        "at_floor": len(at_floor),
        "note": (
            "A meter at the floor cannot distinguish a silent source from a dead digital link. "
            "If every ADAT or S/PDIF channel is at the floor, suspect clock or cabling "
            "(see the troubleshoot_clock prompt)."
            if at_floor
            else None
        ),
    }


@tool
async def tool_watch_meters(
    ctx: ServerContext,
    targets: list[str] | None = None,
    duration_s: float = 5.0,
    threshold_db: float = AUDIO_FLOWING_DB,
    interval_s: float = DEFAULT_WATCH_INTERVAL_S,
    kind: str = "input",
    device: int | None = None,
) -> dict[str, Any]:
    """Poll meters for a while and stop early as soon as a channel crosses the threshold."""
    rig = await ctx.rig()
    channels = _channel_pool(rig, targets, kind, device)
    if not channels:
        raise ValueError("no channels selected to watch.")
    duration = min(max(0.0, float(duration_s)), WATCH_MAX_DURATION_S)
    interval = max(WATCH_MIN_INTERVAL_S, float(interval_s))
    level_paths = {
        f"{channel.path}/meters/{meter}/MeterLevel": channel
        for channel in channels
        for meter in (0, 1)
    }
    peaks: dict[str, float] = {}
    triggered: list[str] = []
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + duration
    polls = 0
    while True:
        values = await ctx.poll(list(level_paths))
        polls += 1
        for path, raw in values.items():
            level = _number(raw)
            if level is None:
                continue
            channel = level_paths[path]
            if level > peaks.get(channel.name, float("-inf")):
                peaks[channel.name] = level
            if level > threshold_db and channel.name not in triggered:
                triggered.append(channel.name)
        if triggered:
            break
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(interval, remaining))
    return {
        "triggered": bool(triggered),
        "triggered_channels": triggered,
        "threshold_db": threshold_db,
        "peaks_db": dict(sorted(peaks.items(), key=lambda item: item[1], reverse=True)),
        "polls": polls,
        "elapsed_s": round(loop.time() - started, 3),
        "duration_s": duration,
        "channels_watched": len(channels),
        "note": (
            None
            if triggered
            else f"nothing rose above {threshold_db:+.1f} dB in {duration:g}s; every watched "
            "meter stayed below it."
        ),
    }


# ------------------------------------------------------------------------------ snapshots


@tool
async def tool_snapshot(
    ctx: ServerContext,
    action: str = "list",
    name: str | None = None,
    dry_run: bool = True,
    ignore: list[str] | None = None,
    include_clock: bool = False,
    include_monitor: bool = False,
    allow_cross_model: bool = False,
) -> dict[str, Any]:
    """Save, list, diff and restore whole-rig snapshots. Restore is a dry run by default."""
    verb = action.strip().casefold()
    store = ctx.snapshots
    if verb in ("list", "ls"):
        return {
            "action": "list",
            "directory": str(store.directory),
            "snapshots": store.list_snapshots(),
        }

    if verb in ("save", "capture"):
        rig = await ctx.rig(refresh=True)
        # UTC, to match the document's own `created` field and snapshot.py's pre-restore names.
        chosen = name or f"snapshot-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"
        doc = await snapshot.capture(ctx.client, rig, name=chosen)
        path = store.save(doc, chosen)
        return {
            "action": "save",
            "name": chosen,
            "path": str(path),
            "entries": len(doc["entries"]),
            "devices": [device["type_id"] for device in doc["devices"]],
            "sample_rate": doc["sample_rate"],
            "clock_source": doc["clock_source"],
        }

    if verb not in ("diff", "restore"):
        raise ValueError(f"action={action!r} is not supported. Use save, list, diff or restore.")
    if not name:
        raise ValueError(f"action={action!r} needs the name of a saved snapshot.")
    doc = store.load(name)

    if verb == "diff":
        rig = await ctx.rig()
        changes = await snapshot.diff(ctx.client, rig, doc, ignore or [])
        return {
            "action": "diff",
            "name": name,
            "changed": len(changes),
            "entries": len(doc.get("entries", [])),
            "ignored": ignore or [],
            "changes": [change.as_dict() for change in changes[:200]],
            "truncated": max(0, len(changes) - 200),
        }

    rig = await ctx.rig(refresh=True)
    flowing: bool | None = None
    if include_clock:
        flowing, _, _ = await ctx.audio_flowing()
    result = await snapshot.restore(
        ctx.client,
        rig,
        doc,
        gate=ctx.gate,
        dry_run=dry_run,
        include_clock=include_clock,
        include_monitor=include_monitor,
        allow_cross_model=allow_cross_model,
        audio_flowing=flowing,
        store=store,
    )
    return {"action": "restore", "name": name, **result}


# ------------------------------------------------------------------------------------ raw


@tool
async def tool_raw_get(ctx: ServerContext, path: str) -> dict[str, Any]:
    """Read any node or property in the engine tree verbatim. Escape hatch, read-only."""
    try:
        prop = await ctx.client.get_prop(path)
    except (UAConnectError, UATimeoutError):
        # Never let a transport failure look like "this is a node, not a property".
        raise
    except UAError:
        properties, children = await ctx.client.get_node(path)
        return {
            "path": path,
            "kind": "node",
            "children": children,
            "properties": {
                name: {
                    "type": item.type,
                    "value": item.value,
                    "min": item.min,
                    "max": item.max,
                    "readonly": item.readonly,
                }
                for name, item in properties.items()
            },
        }
    return {
        "path": path,
        "kind": "property",
        "type": prop.type,
        "value": prop.value,
        "min": prop.min,
        "max": prop.max,
        "values": prop.values,
        "readonly": prop.readonly,
        "default": prop.default,
    }


async def _raw_write_context(ctx: ServerContext, path: str, value: Any) -> WriteContext:
    """The write context for ``uad_raw_set``, which knows nothing about the topology.

    ``uad_raw_set`` can reach the monitor rail directly, so it reconstructs what the gate needs:

    * ``Mute``/``DimOn`` under ``/outputs/``: look for the sibling ``CRMonitorLevel``. If that
      property exists the strip IS a control-room monitor and the rail applies; if the read fails,
      the gate refuses (fail closed). An output without it is an ordinary output.
    * ``DimAttenuation``: find that device's monitor strip and read its level and ``DimOn``, so a
      legitimate decrease is not blocked and a dangerous one still is.
    """
    reason = "uad_raw_set (full mode)"
    name = property_name(path)
    if name == DIM_ATTENUATION_PROPERTY:
        return await _raw_dim_attenuation_context(ctx, path, reason)
    if name not in MONITOR_RELEASE_PROPERTIES or not coerces_to_false(value):
        return WriteContext(reason=reason, confirm=True)
    if "/outputs/" not in path:
        return WriteContext(reason=reason, confirm=True)
    strip = path.rsplit("/", 1)[0]
    try:
        level = await ctx.client.get_prop(f"{strip}/{MONITOR_LEVEL_PROPERTY}")
    except UAPathError:
        # No CRMonitorLevel on this strip: it is an ordinary output, not the control room.
        return WriteContext(reason=reason, confirm=True)
    except UAError as exc:
        logger.warning("could not read %s/%s: %s", strip, MONITOR_LEVEL_PROPERTY, exc)
        return WriteContext(reason=reason, confirm=True, is_monitor=True)
    return WriteContext(
        reason=reason,
        confirm=True,
        is_monitor=True,
        monitor_level_db=_number(level.value),
    )


async def _raw_dim_attenuation_context(
    ctx: ServerContext, path: str, reason: str
) -> WriteContext:
    """Level + DimOn of the monitor strip on the device whose ``DimAttenuation`` is being set."""
    device: int | None = None
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 3 and parts[1].isdigit():
        device = int(parts[1])
    level: float | None = None
    dim_on: bool | None = None
    try:
        monitor = (await ctx.rig()).monitor_output(device)
    except UAError as exc:
        logger.warning("no monitor output for %s: %s", path, exc)
    else:
        level, dim_on = await _monitor_rail_state(ctx, monitor.path)
    return WriteContext(
        reason=reason,
        confirm=True,
        is_monitor=True,
        monitor_level_db=level,
        dim_on=dim_on,
    )


@tool
async def tool_raw_set(ctx: ServerContext, path: str, value: Any) -> dict[str, Any]:
    """Write any property directly. Registered only when UAD_MCP_MODE=full."""
    if ctx.gate.mode != "full":
        raise SafetyRefusal(
            f"uad_raw_set is only available when UAD_MCP_MODE=full (current mode: "
            f"{ctx.gate.mode!r}). Use the specific tool for this property instead."
        )
    write = await ctx.write(path, value, context=await _raw_write_context(ctx, path, value))
    return {"writes": [write]}


# ------------------------------------------------------------------------------- prompts

SETUP_VOCAL_TRACKING = """\
Set up an Apollo input for tracking a lead vocal, end to end.

Work in this order and show me the numbers you read at each step.

1. `uad_get_status` - confirm the interface is online, note the sample rate, the clock source and
   the safety mode. If the safety mode is `readonly`, stop and tell me: nothing can be changed.
2. `uad_list_channels(kind="input")` - pick the input with `has_preamp: true` that the mic is
   plugged into. If you cannot tell which one, ask me. Do not guess.
3. `uad_set_channels` - rename it to something meaningful (e.g. "LEAD VOX"), `mute: false`,
   `fader_db: 0`, `hidden: false`, `solo: false`.
4. `uad_set_preamp` - `low_cut: true` for a close vocal, `pad: false`, `phase: false`,
   `gain_db: 20` as a starting point.
   PHANTOM POWER: only after I confirm the microphone is a condenser, call
   `uad_set_preamp(phantom_48v=true, confirm_condenser_mic=true)`. 48V can permanently destroy a
   ribbon microphone. If I have not said what mic it is, ask, and leave 48V alone.
5. Set the gain properly: ask me for the loudest passage, then
   `uad_watch_meters(targets=["LEAD VOX"], duration_s=10, threshold_db=-6)`. Aim for peaks
   between -12 and -6 dBFS. Adjust `gain_db` in 3 dB steps, re-check, and tell me the peak you
   measured. Never chase a target by more than 6 dB in one step.
6. Character: `uad_list_plugins(unison_only=true, authorized_only=true)` and, if I want one,
   `uad_load_plugin(target="LEAD VOX", plugin=..., slot="unison")`. Then inserts: a compressor in
   slot 1 and an EQ in slot 2 with `uad_load_plugin`. Report `materialized` for each; if it is
   false the parameters are not addressable yet.
7. The singer's headphones: `uad_set_sends` with `send: "cue1"` and `level_db` around -6, plus a
   little of the backing track. Remember the headphone output *level* itself cannot be set from
   here (see `uad://limitations`) - the singer's cue balance is made with send levels.
8. Control room: `uad_set_monitor(level_db=...)`. The monitor ceiling is enforced; if you are
   refused, say so and read the stored level back rather than retrying blindly.
9. `uad_snapshot(action="save", name="vocal-tracking")` so this setup can be recovered.

Safety rules for this session: never engage 48V without my explicit confirmation; never raise
the monitor level to "check" something; if anything squeals, call `uad_safe_state` first and ask
questions afterwards.
"""

ONBOARD_ADAT_EXPANDER = """\
Add an 8-channel ADAT expander (e.g. an ADA8200, or a second interface in ADAT mode) to this
Apollo and get all 8 channels showing signal.

1. `uad_get_status` - write down `clock.source`, `clock.locked` and `clock.sample_rate`. Those
   three facts decide everything below.
2. Cabling, in words I can check: the expander's ADAT OUT goes to the Apollo's ADAT IN. If the
   expander is to be slaved to the Apollo, the Apollo's ADAT OUT must ALSO be patched back to
   the expander's ADAT IN (or use word clock). Optical cables fail silently, so tell me exactly
   which ports to look at.
3. Decide who is the clock master. Exactly one device may be master:
   - **Apollo master** (`ClockSource: Internal`): the expander must be set to slave from ADAT
     (or word clock) on the return cable, and to the same sample rate as the Apollo.
   - **Expander master**: call `uad_set_clock(source="ADAT", confirm=true)`. It will refuse while
     audio is flowing, so stop playback and recording first.
   Never set both to internal, and never set both to slave.
4. Sample rate must match exactly on both boxes. At 88.2 or 96 kHz ADAT runs S/MUX, so the 8
   channels become 4 - channels 5-8 going dead at high rates is expected, not a fault.
5. `uad_list_channels(kind="input")` - find the channels with `io_type: "ADAT"`. With
   `uad_set_channels`, rename each to what is actually patched into the expander, set
   `hidden: false` and `enabled: true`, `fader_db: 0`, `mute: false`.
6. Verify: ask me to send signal through the expander, then
   `uad_peek_meters(targets=[...the ADAT channels...])`. If every ADAT meter sits at exactly the
   floor (-77 dB), that is the signature of no clock lock or a dead optical cable, NOT silence -
   switch to the `troubleshoot_clock` prompt.
7. `uad_snapshot(action="save", name="adat-expander")` once all 8 channels meter correctly.
"""

TROUBLESHOOT_CLOCK = """\
Diagnose digital audio that is not arriving: no signal on ADAT or S/PDIF inputs, clock that will
not lock, or garbled digital audio.

Follow this in order. Report what you read at each step before acting on it.

1. `uad_get_status`. Read `clock.source`, `clock.locked`, `clock.sample_rate`.
2. **If `clock.locked` is false**: the Apollo is chasing a clock it cannot see. Either the
   external device is off, unplugged or at a different rate, or `ClockSource` points at an input
   with no clock on it. To get back to a known-good state, stop all audio and call
   `uad_set_clock(source="Internal", confirm=true)`; it verifies lock and reverts itself if the
   new source does not lock.
3. **If `clock.locked` is true but a digital input reads nothing**: `uad_peek_meters` on the ADAT
   and S/PDIF channels. Every digital meter pinned at exactly the floor (-77 dB) is the classic
   signature of a dead digital link. A meter at the floor CANNOT tell silence from a dead link,
   so do not conclude "the source is just quiet".
4. **Then check who is master.** Exactly one device in the chain may be the clock master:
   - Apollo master (`ClockSource: Internal`): the expander or outboard converter MUST be slaved
     to the Apollo, either from the ADAT return cable (Apollo ADAT OUT into the device's ADAT IN)
     or from word clock out. A device left on its own internal clock will never lock here.
   - External master: the Apollo's `ClockSource` must be `ADAT`, `Word Clock` or `S/PDIF` to
     match, and the external device's sample rate must equal the Apollo's `sample_rate`.
   - Both internal: nothing locks and audio drifts or clicks. Both slaved: nothing locks at all.
5. **Sample rate mismatch is the single most common cause.** 44.1 vs 48 kHz shows up as no lock
   or as garbled, pitched-wrong audio. Compare the device's front panel to `clock.sample_rate`.
   At 88.2/96 kHz, ADAT S/MUX halves the channel count (8 becomes 4) - expected behaviour.
6. **Cabling.** Swap the optical cable; confirm OUT goes to IN in both directions and that you
   are not looking at an S/PDIF optical port. TOSLINK failures are silent.
7. After every change: `uad_get_status` again, confirm `clock.locked` is true, then
   `uad_peek_meters` to confirm signal is actually arriving.

Safety: `uad_set_clock` and `uad_set_sample_rate` refuse while audio is flowing (any input above
-60 dB) and both require `confirm=true`, because re-locking the clock glitches or drops every
running stream. Stop playback and recording first, and tell me before you change either.
"""

PANIC_RESET = """\
Something is wrong right now - feedback, a runaway level, unexpected loud audio. Get the rig
safe first, diagnose second.

1. **Immediately** call `uad_safe_state`. No parameters, no confirmation, no questions: it mutes
   every monitor output and turns talkback off.
2. `uad_get_status` to see where things stand (clock, sample rate, talkback, safety mode).
3. With the monitors still muted, find the cause: `uad_list_channels` and look for channels that
   are unexpectedly unmuted, soloed, or sitting at a high `fader_db`; `uad_peek_meters` to see
   what is actually hot. A microphone open next to its own monitor is the usual feedback path.
4. Fix the cause with `uad_set_channels` (mute the offender, `solo: false`, sensible `fader_db`).
5. Only then bring the monitors back: `uad_set_monitor(mute=false)`. If that is refused, the
   stored monitor level is above the safety ceiling - lower it first with
   `uad_set_monitor(level_db=...)`, then release the mute. Come up in steps; the rail allows at
   most +6 dB per call and +10 dB per 30 s.
6. If the state is scrambled beyond quick repair: `uad_snapshot(action="list")`, then
   `uad_snapshot(action="restore", name=..., dry_run=true)` and show me the ordered write list
   before running it with `dry_run=false`. A real restore captures a `pre-restore-*` snapshot
   first, so this is recoverable.

Do not, while sorting this out: raise the monitor level to hear whether it is fixed; unsolo or
unmute everything at once; change the clock source or sample rate; or engage 48V.
"""


# --------------------------------------------------------------------------- registration


def _server_class() -> Any:
    """The decorator-style MCP server class, whatever the installed ``mcp`` calls it.

    SPEC §4.5 says ``FastMCP``; mcp 2.x renamed the identical decorator API to ``MCPServer``.
    Imported lazily so the tool functions above stay importable (and unit-testable) even if the
    ``mcp`` package is not installed at all.
    """
    try:
        from mcp.server import MCPServer

        return MCPServer
    except ImportError:  # pragma: no cover - mcp < 2.0
        from mcp.server.fastmcp import FastMCP

        return FastMCP


def build_server(ctx: ServerContext) -> Any:
    """Build the MCP server and register every tool, resource and prompt.

    Each registered function is a thin closure over ``ctx``: its signature is what the JSON
    schema is generated from, and its body does nothing but forward to the ``tool_*`` function
    the tests exercise directly.
    """
    server = _server_class()(
        SERVER_NAME,
        version=__version__,
        instructions=(
            "Controls Universal Audio Apollo interfaces through the local UA Mixer Engine. "
            "Resolve channels by name where possible, read before you write, and treat every "
            "refusal as information: the message names the environment variable or confirm flag "
            "that would unblock it. Monitor levels and 48V phantom power can damage ears, "
            "speakers and ribbon microphones."
        ),
    )

    registered: list[str] = []

    def register(**kwargs: Any) -> Callable[[ToolFn], ToolFn]:
        """Register a tool and remember its name, so the count can be logged and asserted."""
        decorate = server.tool(**kwargs)

        def wrap(fn: ToolFn) -> ToolFn:
            registered.append(str(kwargs["name"]))
            return decorate(fn)

        return wrap

    @register(
        name="uad_get_status",
        description=(
            "Report engine, clock, sample rate, DSP load, every Apollo in the cascade and the "
            "effective safety limits.\n"
            "Use when: starting a session, before any write, or when something is not behaving "
            "and you need to know the clock, the sample rate or which safety mode is active.\n"
            "Not for: channel levels or meters - use uad_list_channels and uad_peek_meters."
        ),
    )
    async def uad_get_status() -> dict[str, Any]:
        return await tool_get_status(ctx)

    @register(
        name="uad_list_channels",
        description=(
            "List every strip across every unit, grouped by device, with name, I/O type, fader, "
            "mute, solo, pan, hidden and stereo link.\n"
            "Use when: you need to find a channel by name, or to see the current mix state "
            "before changing it.\n"
            "Not for: meter levels (uad_peek_meters) or plugin contents (uad_get_inserts)."
        ),
    )
    async def uad_list_channels(
        kind: str = "input", device: int | None = None, include_hidden: bool = True
    ) -> dict[str, Any]:
        return await tool_list_channels(
            ctx, kind=kind, device=device, include_hidden=include_hidden
        )

    @register(
        name="uad_set_channels",
        description=(
            "Batch-set channel state. Each item: {target: name or index, device?, kind?, "
            "fader_db?, pan?, pan2?, mute?, solo?, solo_safe?, name?, hidden?, stereo?, "
            "enabled?}. Every item reports resolved {device,index,name} and per-property "
            "{verified, before, after, warnings}.\n"
            "Use when: changing faders, mutes, solos, pans, names or visibility on one or many "
            "channels at once.\n"
            "Not for: the control-room monitor (uad_set_monitor), preamp gain (uad_set_preamp) "
            "or send levels (uad_set_sends)."
        ),
    )
    async def uad_set_channels(items: list[dict[str, Any]]) -> dict[str, Any]:
        return await tool_set_channels(ctx, items)

    @register(
        name="uad_set_preamp",
        description=(
            "Set preamp controls on one mic/line input: gain_db, pad, low_cut, phase, hi_z, "
            "source ('Mic' or 'Line'), and 48V phantom power.\n"
            "Use when: setting recording gain or input options on a channel whose has_preamp is "
            "true. 48V requires confirm_condenser_mic=true because phantom power destroys ribbon "
            "microphones.\n"
            "Not for: fader level (that is uad_set_channels fader_db) or digital inputs, which "
            "have no preamp at all."
        ),
    )
    async def uad_set_preamp(
        target: str | int,
        device: str | int | None = None,
        gain_db: float | None = None,
        pad: bool | None = None,
        low_cut: bool | None = None,
        phase: bool | None = None,
        hi_z: bool | None = None,
        source: str | None = None,
        phantom_48v: bool | None = None,
        confirm_condenser_mic: bool = False,
    ) -> dict[str, Any]:
        return await tool_set_preamp(
            ctx,
            target,
            device=device,
            gain_db=gain_db,
            pad=pad,
            low_cut=low_cut,
            phase=phase,
            hi_z=hi_z,
            source=source,
            phantom_48v=phantom_48v,
            confirm_condenser_mic=confirm_condenser_mic,
        )

    @register(
        name="uad_set_sends",
        description=(
            "Batch-set send levels into aux and cue buses. Each item: {target, send: 'aux1' | "
            "'cue1'... or index, device?, level_db?, pan?, bypass?}.\n"
            "Use when: building a performer's cue/headphone mix or feeding a reverb aux.\n"
            "Not for: the headphone output level itself, which this protocol cannot set - see "
            "uad://limitations."
        ),
    )
    async def uad_set_sends(items: list[dict[str, Any]]) -> dict[str, Any]:
        return await tool_set_sends(ctx, items)

    @register(
        name="uad_set_monitor",
        description=(
            "Set the control-room monitor: level_db (hard-railed), dim, mute, mono fold, alt "
            "speaker selection, dim attenuation. Unmuting, un-dimming and LOWERING "
            "dim_attenuation_db are all treated as level events and can be refused; level_db is "
            "written first and the release is railed against the level the engine reports back.\n"
            "Use when: changing what the control room hears, or recovering after uad_safe_state.\n"
            "Not for: headphone level - the protocol cannot set it; see uad://limitations."
        ),
    )
    async def uad_set_monitor(
        device: int | None = None,
        level_db: float | None = None,
        dim: bool | None = None,
        mute: bool | None = None,
        mono: bool | None = None,
        alt_speaker: int | None = None,
        dim_attenuation_db: int | None = None,
    ) -> dict[str, Any]:
        return await tool_set_monitor(
            ctx,
            device=device,
            level_db=level_db,
            dim=dim,
            mute=mute,
            mono=mono,
            alt_speaker=alt_speaker,
            dim_attenuation_db=dim_attenuation_db,
        )

    @register(
        name="uad_talkback",
        description=(
            "Turn talkback on or off and set its level, with an auto-off timer (default 30 s) so "
            "an open talkback mic cannot be forgotten. Calling it again replaces the timer.\n"
            "Use when: the engineer needs to speak to the live room.\n"
            "Not for: routing the talkback mic or choosing which mic it is - that is set in "
            "Console."
        ),
    )
    async def uad_talkback(
        on: bool | None = None, level: int | None = None, auto_off_s: float | None = None
    ) -> dict[str, Any]:
        return await tool_talkback(ctx, on=on, level=level, auto_off_s=auto_off_s)

    @register(
        name="uad_safe_state",
        description=(
            "Panic button: mute every monitor output and turn talkback off. Takes no arguments "
            "and needs no confirmation.\n"
            "Use when: there is feedback, a runaway level, or anything unexpectedly loud. Call "
            "it first and diagnose afterwards.\n"
            "Not for: normal muting of a single channel - use uad_set_channels."
        ),
    )
    async def uad_safe_state() -> dict[str, Any]:
        return await tool_safe_state(ctx)

    @register(
        name="uad_set_clock",
        description=(
            "Change the clock source (Internal, Word Clock, ADAT, S/PDIF), verify ClockLocked "
            "afterwards, and revert automatically if the new source does not lock.\n"
            "Use when: slaving to or from external digital gear, or recovering from an unlocked "
            "clock. Requires confirm=true and refuses while audio is flowing.\n"
            "Not for: changing the sample rate - that is uad_set_sample_rate."
        ),
    )
    async def uad_set_clock(source: str, confirm: bool = False) -> dict[str, Any]:
        return await tool_set_clock(ctx, source, confirm=confirm)

    @register(
        name="uad_set_sample_rate",
        description=(
            "Change the system sample rate (e.g. 44100, 48000, 96000). Requires confirm=true and "
            "refuses while audio is flowing, because every running stream will glitch.\n"
            "Use when: a session needs a different rate, or to match an external clock master.\n"
            "Not for: fixing a lock failure on its own - check the clock source first with "
            "uad_get_status."
        ),
    )
    async def uad_set_sample_rate(rate: int, confirm: bool = False) -> dict[str, Any]:
        return await tool_set_sample_rate(ctx, rate, confirm=confirm)

    @register(
        name="uad_list_plugins",
        description=(
            "List the installed UAD plugin catalog: name, categories, Unison capability, "
            "authorization status. Filterable and limited by default.\n"
            "Use when: choosing a plugin to load, or checking whether one is authorized.\n"
            "Not for: what is currently loaded on channels - that is uad_get_inserts."
        ),
    )
    async def uad_list_plugins(
        search: str | None = None,
        category: str | None = None,
        unison_only: bool = False,
        authorized_only: bool = False,
        limit: int = PLUGIN_LIST_LIMIT,
    ) -> dict[str, Any]:
        return await tool_list_plugins(
            ctx,
            search=search,
            category=category,
            unison_only=unison_only,
            authorized_only=authorized_only,
            limit=limit,
        )

    @register(
        name="uad_get_inserts",
        description=(
            "Show what is loaded where: every insert slot plus the Unison slot, with plugin name, "
            "power state and preset, across one channel or all of them.\n"
            "Use when: auditing a channel's processing chain or finding a free slot.\n"
            "Not for: parameter values inside a plugin - use uad_get_plugin_params."
        ),
    )
    async def uad_get_inserts(
        target: str | int | None = None,
        device: int | None = None,
        kind: str = "input",
        loaded_only: bool = True,
    ) -> dict[str, Any]:
        return await tool_get_inserts(
            ctx, target=target, device=device, kind=kind, loaded_only=loaded_only
        )

    @register(
        name="uad_load_plugin",
        description=(
            "Load a UAD plugin into an insert slot (slot=0,1,2,3...) or the Unison preamp slot "
            "(slot='unison'), then poll up to 5 s for its parameter nodes to materialize and "
            "report whether they did.\n"
            "Use when: adding processing to a channel. Optionally recall a preset in the same "
            "call.\n"
            "Not for: changing parameters (uad_set_plugin_params) or clearing a slot "
            "(uad_remove_plugin)."
        ),
    )
    async def uad_load_plugin(
        target: str | int,
        plugin: str,
        slot: str | int = 0,
        device: str | int | None = None,
        preset: str | None = None,
        power: bool | None = None,
    ) -> dict[str, Any]:
        return await tool_load_plugin(
            ctx, target, plugin, slot=slot, device=device, preset=preset, power=power
        )

    @register(
        name="uad_remove_plugin",
        description=(
            "Clear an insert or Unison slot by writing an empty EffectName, and report what was "
            "removed.\n"
            "Use when: freeing DSP or removing processing from a channel.\n"
            "Not for: bypassing a plugin while keeping it loaded - set its Power instead via "
            "uad_load_plugin(power=false) on the same slot."
        ),
    )
    async def uad_remove_plugin(
        target: str | int, slot: str | int = 0, device: str | int | None = None
    ) -> dict[str, Any]:
        return await tool_remove_plugin(ctx, target, slot=slot, device=device)

    @register(
        name="uad_get_plugin_params",
        description=(
            "List a loaded plugin's parameters with their names, normalized 0-1 values and the "
            "engine's own display text.\n"
            "Use when: you need the exact parameter names before setting them, or to read back "
            "what a preset did.\n"
            "Not for: an empty slot - parameter nodes only exist once a plugin is loaded."
        ),
    )
    async def uad_get_plugin_params(
        target: str | int, slot: str | int = 0, device: str | int | None = None
    ) -> dict[str, Any]:
        return await tool_get_plugin_params(ctx, target, slot=slot, device=device)

    @register(
        name="uad_set_plugin_params",
        description=(
            "Batch-set a loaded plugin's parameters by name using normalized 0-1 values: "
            "params=[{name: 'Peak Reduction', normalized: 0.4}, ...].\n"
            "Use when: dialling in a plugin after loading it. Read uad_get_plugin_params first "
            "for exact names and current values.\n"
            "Not for: dB or Hz values - the protocol exposes normalized values only; the display "
            "text is read-only."
        ),
    )
    async def uad_set_plugin_params(
        target: str | int,
        params: list[dict[str, Any]],
        slot: str | int = 0,
        device: str | int | None = None,
    ) -> dict[str, Any]:
        return await tool_set_plugin_params(ctx, target, params, slot=slot, device=device)

    @register(
        name="uad_plugin_preset",
        description=(
            "Recall a preset by name into a loaded plugin slot, validated against that plugin's "
            "preset browser when the engine lists it.\n"
            "Use when: a named factory or user preset is a faster starting point than setting "
            "parameters.\n"
            "Not for: channel-strip or Console session presets, which this server does not "
            "manage - see uad://limitations."
        ),
    )
    async def uad_plugin_preset(
        target: str | int, preset: str, slot: str | int = 0, device: str | int | None = None
    ) -> dict[str, Any]:
        return await tool_plugin_preset(ctx, target, preset, slot=slot, device=device)

    @register(
        name="uad_peek_meters",
        description=(
            "One-shot meter read (level, peak, clip) for all channels or a named subset, with "
            "at-floor channels called out.\n"
            "Use when: checking whether signal is arriving, or which channel is hot right now.\n"
            "Not for: waiting for signal to appear - use uad_watch_meters, which polls and exits "
            "early."
        ),
    )
    async def uad_peek_meters(
        targets: list[str] | None = None,
        kind: str = "input",
        device: int | None = None,
        above_db: float | None = None,
    ) -> dict[str, Any]:
        return await tool_peek_meters(
            ctx, targets=targets, kind=kind, device=device, above_db=above_db
        )

    @register(
        name="uad_watch_meters",
        description=(
            "Poll meters for up to duration_s (capped at 30 s) and return as soon as any watched "
            "channel exceeds threshold_db, reporting per-channel peaks.\n"
            "Use when: verifying that a source is live, setting gain against a performance, or "
            "hunting an intermittent signal.\n"
            "Not for: long-term monitoring - it is capped at 30 s per call by design."
        ),
    )
    async def uad_watch_meters(
        targets: list[str] | None = None,
        duration_s: float = 5.0,
        threshold_db: float = AUDIO_FLOWING_DB,
        interval_s: float = DEFAULT_WATCH_INTERVAL_S,
        kind: str = "input",
        device: int | None = None,
    ) -> dict[str, Any]:
        return await tool_watch_meters(
            ctx,
            targets=targets,
            duration_s=duration_s,
            threshold_db=threshold_db,
            interval_s=interval_s,
            kind=kind,
            device=device,
        )

    @register(
        name="uad_snapshot",
        description=(
            "Whole-rig snapshots: action='save' captures every writable property, 'list' shows "
            "what is saved, 'diff' reports what changed since one (ignore accepts glob "
            "patterns), 'restore' writes one back in a safe order. Restore defaults to "
            "dry_run=true and returns the exact ordered write list.\n"
            "Use when: saving a session state before experimenting, seeing what drifted, or "
            "putting a known-good state back. A real restore captures a pre-restore snapshot "
            "first.\n"
            "Not for: Console sessions or plugin scenes, which this server cannot touch."
        ),
    )
    async def uad_snapshot(
        action: str = "list",
        name: str | None = None,
        dry_run: bool = True,
        ignore: list[str] | None = None,
        include_clock: bool = False,
        include_monitor: bool = False,
        allow_cross_model: bool = False,
    ) -> dict[str, Any]:
        return await tool_snapshot(
            ctx,
            action=action,
            name=name,
            dry_run=dry_run,
            ignore=ignore,
            include_clock=include_clock,
            include_monitor=include_monitor,
            allow_cross_model=allow_cross_model,
        )

    @register(
        name="uad_raw_get",
        description=(
            "Read any node or property in the engine tree verbatim, with its metadata.\n"
            "Use when: a tool does not cover something and you need to inspect the tree, or to "
            "confirm a property's type, range or enum values.\n"
            "Not for: writing - that is deliberately separate and only available in full mode."
        ),
    )
    async def uad_raw_get(path: str) -> dict[str, Any]:
        return await tool_raw_get(ctx, path)

    if ctx.gate.mode == "full":

        @register(
            name="uad_raw_set",
            description=(
                "Write any property directly, bypassing the safe-mode allowlist. Registered only "
                "because UAD_MCP_MODE=full. The monitor rail, the denylist and the write rate "
                "limit still apply.\n"
                "Use when: nothing else can reach the property and you know exactly what it "
                "does.\n"
                "Not for: anything a specific tool already covers - those read, gate and verify "
                "properly."
            ),
        )
        async def uad_raw_set(path: str, value: Any) -> dict[str, Any]:
            return await tool_raw_set(ctx, path, value)

    # ------------------------------------------------------------------------- resources

    @server.resource(
        "uad://state",
        name="Apollo rig state",
        description="Live uad_get_status payload: clock, sample rate, DSP, devices, safety limits.",
        mime_type="application/json",
    )
    async def state_resource() -> str:
        return json.dumps(await tool_get_status(ctx), indent=1, default=str)

    @server.resource(
        "uad://channels",
        name="Apollo channel list",
        description="Live uad_list_channels payload for every input, aux and output.",
        mime_type="application/json",
    )
    async def channels_resource() -> str:
        return json.dumps(await tool_list_channels(ctx, kind="all"), indent=1, default=str)

    @server.resource(
        "uad://limitations",
        name="What this server cannot do",
        description="The cannot-do list with the workaround for each item.",
        mime_type="text/markdown",
    )
    async def limitations_resource() -> str:
        return LIMITATIONS

    # --------------------------------------------------------------------------- prompts

    @server.prompt(
        name="setup_vocal_tracking",
        description="Set up an input for vocal tracking: gain, 48V safety, cue mix, plugins.",
    )
    async def setup_vocal_tracking() -> str:
        return SETUP_VOCAL_TRACKING

    @server.prompt(
        name="onboard_adat_expander",
        description="Add an ADAT expander: clock master, sample rate, naming, verification.",
    )
    async def onboard_adat_expander() -> str:
        return ONBOARD_ADAT_EXPANDER

    @server.prompt(
        name="troubleshoot_clock",
        description="Diagnose no-signal digital inputs and clock lock failures.",
    )
    async def troubleshoot_clock() -> str:
        return TROUBLESHOOT_CLOCK

    @server.prompt(
        name="panic_reset",
        description="Feedback or a runaway level: get safe first, diagnose second.",
    )
    async def panic_reset() -> str:
        return PANIC_RESET

    logger.info(
        "registered %d tools in %s mode: %s", len(registered), ctx.gate.mode, ", ".join(registered)
    )
    return server
