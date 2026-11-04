"""Topology discovery for the UA Mixer Engine tree (docs/SPEC.md §4.2).

Turns the engine's node tree into a flat, cacheable :class:`Rig` of devices and channels, and
resolves human/LLM-supplied targets ("ANALOG 1", 8, "monitor") onto exact node paths.

Nothing here is derived from a channel's position in the tree: monitor outputs are found by the
presence of the ``CRMonitorLevel`` property, preamps by the presence of a ``preamps`` child and
talkback by ``IOType``. Indices differ per Apollo model, so they are never hardcoded.

Only topology belongs in a :class:`Rig`. Live values (fader, mute, meters) are read on demand and
must never be cached here.
"""

from __future__ import annotations

import difflib
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from .client import UAClient, UAError, UAProperty

logger = logging.getLogger("uad_console_mcp.discovery")

DEVICES_PATH = "/devices"
MONITOR_LEVEL_PROPERTY = "CRMonitorLevel"
"""Presence of this property is what makes an output the control-room monitor."""

PREAMP_CHILD = "preamps"
TALKBACK_IO_TYPE = "TalkbackMic"

ChannelKind = Literal["input", "aux", "output"]

_CONTAINERS: tuple[tuple[ChannelKind, str], ...] = (
    ("input", "inputs"),
    ("aux", "auxs"),
    ("output", "outputs"),
)
CHANNEL_KINDS: tuple[ChannelKind, ...] = tuple(kind for kind, _ in _CONTAINERS)

_MAX_LISTED = 8


# --------------------------------------------------------------------------------- data model


@dataclass(frozen=True)
class DeviceInfo:
    """One Apollo in the (possibly cascaded) rig."""

    index: int
    name: str
    type_id: str
    online: bool
    multi_unit_order: int
    cue_bus_count: int

    @property
    def path(self) -> str:
        return f"{DEVICES_PATH}/{self.index}"


@dataclass(frozen=True)
class ChannelInfo:
    """One input, aux or output strip, with the capability flags the tool layer needs."""

    device: int
    kind: ChannelKind
    index: int
    name: str
    io_type: str | None
    stereo: bool
    hidden: bool
    active: bool
    has_preamp: bool
    is_monitor: bool
    is_talkback: bool
    path: str

    def label(self) -> str:
        """Short human/LLM-facing identity, e.g. ``'ANALOG 1' (device 0 input 0)``."""
        return f"{self.name!r} (device {self.device} {self.kind} {self.index})"


# ------------------------------------------------------------------------------------- errors


class AmbiguousTargetError(UAError):
    """A target matched more than one channel. ``candidates`` lists every match."""

    def __init__(
        self,
        target: str | int,
        candidates: Sequence[ChannelInfo],
        *,
        hint: str | None = None,
    ) -> None:
        self.target = target
        self.candidates: list[ChannelInfo] = list(candidates)
        kind = self.candidates[0].kind if self.candidates else "channel"
        shown = ", ".join(channel.label() for channel in self.candidates[:_MAX_LISTED])
        if len(self.candidates) > _MAX_LISTED:
            shown += f", ... ({len(self.candidates)} total)"
        remedy = hint or (
            "Use the exact channel name, or pass device=<index or name> and/or the channel index."
        )
        super().__init__(
            f"{target!r} matches {len(self.candidates)} {kind} targets: {shown}. {remedy}"
        )


class TargetNotFoundError(UAError):
    """Nothing matched the requested target. The message lists the closest known names."""

    def __init__(
        self,
        target: str | int,
        kind: str = "input",
        *,
        close: Iterable[str] = (),
        message: str | None = None,
    ) -> None:
        self.target = target
        self.kind = kind
        self.close: list[str] = list(close)
        if message is None:
            if self.close:
                shown = ", ".join(repr(name) for name in self.close[:_MAX_LISTED])
                tail = f" Closest {kind} names: {shown}."
            else:
                tail = f" This rig has no {kind} targets at all."
            message = f"No {kind} matches {target!r}.{tail}"
        super().__init__(message)


# ------------------------------------------------------------------------------------ helpers


def _child_indices(children: Iterable[str]) -> list[int]:
    """Numerically sorted child indices, skipping non-numeric child names."""
    indices: list[int] = []
    for child in children:
        try:
            indices.append(int(child))
        except (TypeError, ValueError):
            logger.debug("ignoring non-numeric child %r", child)
    return sorted(indices)


def _raw(properties: Mapping[str, UAProperty], name: str) -> Any:
    prop = properties.get(name)
    return None if prop is None else prop.value


def _as_str(properties: Mapping[str, UAProperty], name: str, default: str = "") -> str:
    value = _raw(properties, name)
    if value is None:
        return default
    text = str(value)
    return text if text else default


def _as_bool(properties: Mapping[str, UAProperty], name: str, default: bool) -> bool:
    value = _raw(properties, name)
    return default if value is None else bool(value)


def _as_int(properties: Mapping[str, UAProperty], name: str, default: int) -> int:
    value = _raw(properties, name)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.debug("property %s has non-integer value %r", name, value)
        return default


def _close_names(target: str, names: Sequence[str]) -> list[str]:
    matches = difflib.get_close_matches(target, names, n=_MAX_LISTED, cutoff=0.4)
    return matches or list(names[:_MAX_LISTED])


def _fold(text: str) -> str:
    return text.strip().casefold()


def _is_index_text(text: str) -> bool:
    return text.lstrip("+-").isdigit()


# ---------------------------------------------------------------------------------------- rig


@dataclass
class Rig:
    """Cached topology snapshot. Rebuild it on reconnect; never cache live values in it."""

    devices: list[DeviceInfo]
    channels: list[ChannelInfo]
    engine_connected: bool

    # ------------------------------------------------------------------ public resolution

    def resolve(
        self,
        target: str | int,
        *,
        device: str | int | None = None,
        kind: str = "input",
    ) -> ChannelInfo:
        """Resolve ``target`` to exactly one channel.

        ``int`` targets are channel indices; with more than one device in the rig an index that
        exists on several devices is ambiguous and ``device`` is required. ``str`` targets are
        matched case-insensitively: exact name first, then prefix, then substring. A digits-only
        string falls back to an index lookup when no name matches.

        Raises:
            AmbiguousTargetError: several channels matched (``.candidates`` lists them).
            TargetNotFoundError: nothing matched.
            ValueError: ``kind`` or ``target`` is not a usable shape (caller bug).
        """
        channel_kind = _check_kind(kind)
        pool = self._pool(channel_kind, device)

        if isinstance(target, bool):
            raise ValueError(
                f"channel target must be an index or a name, not the boolean {target!r}"
            )
        if isinstance(target, int):
            return self._by_index(target, channel_kind, pool, device)
        if not isinstance(target, str):
            raise ValueError(f"channel target must be an int index or a str name, got {target!r}")

        text = target.strip()
        if not text:
            raise ValueError("channel target must not be empty")
        match = self._by_name(text, pool)
        if match is not None:
            return match
        if _is_index_text(text):
            return self._by_index(int(text), channel_kind, pool, device)
        raise TargetNotFoundError(
            target, channel_kind, close=_close_names(text, [c.name for c in pool])
        )

    def monitor_output(self, device: int | None = None) -> ChannelInfo:
        """The control-room monitor output, found by its ``CRMonitorLevel`` property."""
        monitors = [c for c in self.channels if c.kind == "output" and c.is_monitor]
        scope = ""
        if device is not None:
            self._require_device(device)
            monitors = [m for m in monitors if m.device == device]
            scope = f" on device {device}"
        if not monitors:
            outputs = [c for c in self.channels if c.kind == "output"]
            if device is not None:
                outputs = [c for c in outputs if c.device == device]
            raise TargetNotFoundError(
                "monitor output",
                "output",
                message=(
                    f"No monitor output found{scope}. A monitor output is identified by its "
                    f"{MONITOR_LEVEL_PROPERTY} property and none of the {len(outputs)} output "
                    f"channels{scope} has one."
                ),
            )
        if len(monitors) > 1:
            raise AmbiguousTargetError(
                "monitor output",
                monitors,
                hint="Pass device=<index> to choose which monitor output you mean.",
            )
        return monitors[0]

    # ---------------------------------------------------------------------- small accessors

    def device(self, index: int) -> DeviceInfo:
        """The :class:`DeviceInfo` with ``index``."""
        for info in self.devices:
            if info.index == index:
                return info
        raise TargetNotFoundError(index, "device", close=[str(d.index) for d in self.devices])

    def channels_of(self, kind: str = "input", device: int | None = None) -> list[ChannelInfo]:
        """Every channel of ``kind``, optionally limited to one device."""
        channel_kind = _check_kind(kind)
        return [
            c
            for c in self.channels
            if c.kind == channel_kind and (device is None or c.device == device)
        ]

    # ----------------------------------------------------------------------------- internals

    def _pool(self, kind: ChannelKind, device: str | int | None) -> list[ChannelInfo]:
        channels = [c for c in self.channels if c.kind == kind]
        if device is None:
            if not channels:
                raise TargetNotFoundError(kind, kind, message=f"This rig has no {kind} channels.")
            return channels
        indices = self._device_indices(device)
        scoped = [c for c in channels if c.device in indices]
        if not scoped:
            raise TargetNotFoundError(
                kind,
                kind,
                message=f"Device {device!r} has no {kind} channels.",
            )
        return scoped

    def _require_device(self, index: int) -> None:
        if not any(d.index == index for d in self.devices):
            raise TargetNotFoundError(index, "device", close=[str(d.index) for d in self.devices])

    def _device_indices(self, device: str | int) -> set[int]:
        """Device indices selected by ``device`` (an index, a DeviceName or a DeviceTypeID)."""
        if isinstance(device, bool):
            raise ValueError(f"device selector must be an index or a name, not {device!r}")
        if isinstance(device, int):
            self._require_device(device)
            return {device}
        if not isinstance(device, str):
            raise ValueError(f"device selector must be an int or str, got {device!r}")

        text = device.strip()
        if not text:
            raise ValueError("device selector must not be empty")
        if _is_index_text(text):
            # "2" is device 2, never "the device whose name contains a 2" (Apollo x8 Gen 2).
            index = int(text)
            self._require_device(index)
            return {index}
        wanted = _fold(text)
        haystack = [(info, (_fold(info.name), _fold(info.type_id))) for info in self.devices]
        for predicate in (
            lambda field: field == wanted,
            lambda field: field.startswith(wanted),
            lambda field: wanted in field,
        ):
            hits = {info.index for info, fields in haystack if any(map(predicate, fields))}
            if hits:
                return hits
        raise TargetNotFoundError(
            device,
            "device",
            close=_close_names(text, [info.name for info in self.devices]),
        )

    def _by_index(
        self,
        index: int,
        kind: ChannelKind,
        pool: Sequence[ChannelInfo],
        device: str | int | None,
    ) -> ChannelInfo:
        matches = [c for c in pool if c.index == index]
        if not matches:
            available = sorted({c.index for c in pool})
            raise TargetNotFoundError(
                index,
                kind,
                message=(
                    f"No {kind} with index {index}. Available {kind} indices: "
                    f"{_summarise_indices(available)}."
                ),
            )
        if len(matches) > 1:
            hint = (
                f"{len(self.devices)} devices are cascaded, so {kind} index {index} is ambiguous - "
                "pass device=<index or name>."
                if device is None
                else "Narrow the device selector so it matches a single device."
            )
            raise AmbiguousTargetError(index, matches, hint=hint)
        return matches[0]

    def _by_name(self, text: str, pool: Sequence[ChannelInfo]) -> ChannelInfo | None:
        wanted = _fold(text)
        for predicate in (
            lambda name: name == wanted,
            lambda name: name.startswith(wanted),
            lambda name: wanted in name,
        ):
            matches = [c for c in pool if predicate(_fold(c.name))]
            if len(matches) == 1:
                return matches[0]
            if matches:
                raise AmbiguousTargetError(text, matches)
        return None


def _check_kind(kind: str) -> ChannelKind:
    folded = kind.strip().casefold()
    for candidate in CHANNEL_KINDS:
        if folded == candidate:
            return candidate
    raise ValueError(f"kind must be one of {', '.join(CHANNEL_KINDS)}, got {kind!r}")


def _summarise_indices(indices: Sequence[int]) -> str:
    if not indices:
        return "none"
    if len(indices) <= _MAX_LISTED:
        return ", ".join(str(i) for i in indices)
    return f"{indices[0]}-{indices[-1]}"


# ---------------------------------------------------------------------------------- discovery


def _channel_info(
    device: int,
    kind: ChannelKind,
    index: int,
    path: str,
    properties: Mapping[str, UAProperty],
    children: Sequence[str],
) -> ChannelInfo:
    raw_io = _raw(properties, "IOType")
    io_type = None if raw_io is None else str(raw_io)
    return ChannelInfo(
        device=device,
        kind=kind,
        index=index,
        name=_as_str(properties, "Name", f"{kind} {index}"),
        io_type=io_type,
        stereo=_as_bool(properties, "Stereo", False),
        hidden=_as_bool(properties, "ChannelHidden", False),
        active=_as_bool(properties, "Active", True),
        has_preamp=PREAMP_CHILD in children,
        is_monitor=MONITOR_LEVEL_PROPERTY in properties,
        is_talkback=(io_type or "").casefold() == TALKBACK_IO_TYPE.casefold(),
        path=path,
    )


async def discover(client: UAClient) -> Rig:
    """Walk ``/devices/*`` and build the rig topology.

    One ``get_node`` per device, per container and per channel: the engine returns every scalar
    property of a node inline, so a single read gives us names, IOType, the monitor marker and the
    child list that reveals a preamp.
    """
    devices: list[DeviceInfo] = []
    channels: list[ChannelInfo] = []

    _, device_children = await client.get_node(DEVICES_PATH)
    for device_index in _child_indices(device_children):
        device_path = f"{DEVICES_PATH}/{device_index}"
        properties, containers = await client.get_node(device_path)
        devices.append(
            DeviceInfo(
                index=device_index,
                name=_as_str(properties, "DeviceName", f"Device {device_index}"),
                type_id=_as_str(properties, "DeviceTypeID"),
                online=_as_bool(properties, "DeviceOnline", True),
                multi_unit_order=_as_int(properties, "MultiUnitOrder", device_index),
                cue_bus_count=_as_int(properties, "CueBusCount", 0),
            )
        )

        for kind, container in _CONTAINERS:
            if container not in containers:
                logger.debug("device %d has no %s container", device_index, container)
                continue
            container_path = f"{device_path}/{container}"
            _, strip_children = await client.get_node(container_path)
            for index in _child_indices(strip_children):
                path = f"{container_path}/{index}"
                strip_props, strip_kids = await client.get_node(path)
                channels.append(
                    _channel_info(device_index, kind, index, path, strip_props, strip_kids)
                )

    rig = Rig(devices=devices, channels=channels, engine_connected=client.connected)
    logger.info(
        "discovered %d device(s), %d channel(s): %s",
        len(devices),
        len(channels),
        ", ".join(
            f"{info.name} ({'online' if info.online else 'OFFLINE'})" for info in devices
        )
        or "none",
    )
    return rig
