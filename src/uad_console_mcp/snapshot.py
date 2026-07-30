"""Session snapshots: capture, diff and ordered restore (docs/SPEC.md §4.4).

A snapshot is a plain JSON document holding only the properties this server is allowed to
write back (the ``safe``-mode allowlist), so restoring one can never reach a firmware or
identity property. Clock source and sample rate are captured for reference but are NOT
restored unless the caller explicitly opts in, because changing them re-locks the whole
audio chain.

Restore is deliberately boring and ordered (SPEC §4.4): stereo pairs are unlinked first so a
linked partner cannot clobber per-channel writes, plugins are loaded before their presets and
parameters (parameter nodes only exist once a plugin is in the slot), sends and channel state
follow, stereo pairs are relinked, and the monitor rail is touched last and only on request.
No single failure aborts the run: every entry reports back as applied, skipped or failed.

Files live in ``~/.uad-console-mcp/snapshots/``; ``UAD_MCP_SNAPSHOT_DIR`` overrides the
directory (the test suite always sets it, so nothing writes to a real home directory).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import fnmatch
import json
import logging
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any

from .client import UAClient, UAError, UAPathError
from .discovery import DEVICES_PATH, ChannelInfo, Rig
from .safety import (
    MONITOR_RELEASE_PROPERTIES,
    WRITABLE_PROPERTIES,
    SafetyGate,
    SafetyRefusal,
    WriteContext,
    property_name,
)

logger = logging.getLogger("uad_console_mcp.snapshot")

SCHEMA_VERSION = 1

ENV_SNAPSHOT_DIR = "UAD_MCP_SNAPSHOT_DIR"
DEFAULT_SNAPSHOT_DIR = Path("~/.uad-console-mcp/snapshots")

CLOCK_PROPERTIES: frozenset[str] = frozenset({"ClockSource", "SampleRate"})
"""Captured into the document header, never restored unless ``include_clock=True``."""

CAPTURED_PROPERTIES: frozenset[str] = WRITABLE_PROPERTIES - CLOCK_PROPERTIES
"""Exactly the ``safe``-mode allowlist minus the two transport-wide properties."""

DEVICE_MONITOR_PROPERTIES: frozenset[str] = frozenset({"DimAttenuation", "AltMonSelection"})
"""Device-level properties that belong to the monitor rail rather than to a channel."""

MATERIALIZE_TIMEOUT_S = 5.0
"""Per-slot budget for plugin parameter nodes to appear after an ``EffectName`` write."""

MATERIALIZE_POLL_S = 0.25

PARAMETERS_CHILD = "parameters"
EFFECT_NAME_PROPERTY = "EffectName"
STEREO_PROPERTY = "Stereo"
MONITOR_LEVEL_PROPERTY = "CRMonitorLevel"

_MAX_ENUM_VALUES = 64
"""Enum domains longer than this (preset browser trees) are dropped from captured metadata."""

_POLL_CHUNK = 400
_MAX_REPORTED = 200
"""Report lists are truncated at this length; the ``counts`` block always has the totals."""

_VALUE_TOLERANCE = 1e-6
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,80}$")
_SNAPSHOT_SUFFIX = ".json"
_PRE_RESTORE_PREFIX = "pre-restore-"


class SnapshotError(UAError):
    """A snapshot could not be captured, read, written or restored."""


class CrossModelError(SnapshotError):
    """The snapshot was taken on different hardware than the rig it is being restored to."""

    def __init__(self, snapshot_models: Sequence[str], rig_models: Sequence[str]) -> None:
        self.snapshot_models = list(snapshot_models)
        self.rig_models = list(rig_models)
        super().__init__(
            f"Refused to restore a snapshot taken on {', '.join(snapshot_models) or 'unknown'} "
            f"onto {', '.join(rig_models) or 'unknown'}: the channel layout differs, so raw "
            "indices would land on the wrong strips. Pass allow_cross_model=True to map "
            "channels by I/O type and ordinal instead (names, plugins and levels may still "
            "land on unexpected channels)."
        )


class Phase(IntEnum):
    """Restore ordering (SPEC §4.4 step 3). Lower runs first."""

    CLOCK = 0
    UNLINK_STEREO = 1
    EFFECT_LOAD = 2
    PLUGIN_PRESET = 3
    PLUGIN_PARAMS = 4
    UNISON = 5
    SENDS = 6
    CHANNEL = 7
    RELINK_STEREO = 8
    MONITOR = 9


_PROPERTY_RANK: dict[str, int] = {
    # CLOCK phase: the rate first, then the source, so the source locks at the final rate.
    "SampleRate": 0,
    "ClockSource": 1,
    EFFECT_NAME_PROPERTY: 0,
    "Preset": 1,
    "NormalizedValue": 2,
    "Power": 3,
    "Bypass": 4,
    MONITOR_LEVEL_PROPERTY: 0,
    "DimAttenuation": 1,
    "Mute": 2,
    "DimOn": 3,
}
_DEFAULT_RANK = 5


@dataclass(frozen=True)
class Change:
    """One property whose live value differs from the snapshot."""

    path: str
    property: str
    snapshot_value: Any
    current_value: Any

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "property": self.property,
            "snapshot_value": self.snapshot_value,
            "current_value": self.current_value,
        }


@dataclass(frozen=True)
class PlannedWrite:
    """One write in the ordered restore plan."""

    path: str
    property: str
    value: Any
    phase: Phase
    note: str = ""

    @property
    def slot(self) -> str | None:
        """The ``.../effects/S`` slot this write belongs to, if any."""
        return _effect_slot(self.path)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "property": self.property,
            "value": self.value,
            "phase": self.phase.name.lower(),
            "note": self.note,
        }


@dataclass
class RestorePlan:
    """The ordered write list plus everything deliberately left out of it."""

    writes: list[PlannedWrite] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "writes": [write.as_dict() for write in self.writes],
            "skipped": list(self.skipped),
        }


# --------------------------------------------------------------------------------- small utils


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def values_equal(left: Any, right: Any, *, tolerance: float = _VALUE_TOLERANCE) -> bool:
    """Compare two property values, tolerating float quantization by the engine."""
    if _is_number(left) and _is_number(right):
        return abs(float(left) - float(right)) <= tolerance
    return bool(left == right)


_CHANNEL_RE = re.compile(rf"^({DEVICES_PATH}/\d+/(?:inputs|auxs|outputs)/\d+)")


def _channel_prefix(path: str) -> str | None:
    """The ``/devices/N/<container>/I`` strip a property path belongs to, if any."""
    match = _CHANNEL_RE.match(path)
    return match.group(1) if match is not None else None


def _effect_slot(path: str) -> str | None:
    marker = "/effects/"
    index = path.rfind(marker)
    if index < 0:
        return None
    tail = path[index + len(marker) :]
    slot = tail.split("/", 1)[0]
    return f"{path[:index]}{marker}{slot}"


def _utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _timestamp_name() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")


# --------------------------------------------------------------------------------- the store


def snapshot_dir(env: Mapping[str, str] | None = None) -> Path:
    """The directory snapshots live in. ``UAD_MCP_SNAPSHOT_DIR`` wins when set."""
    environ = os.environ if env is None else env
    raw = environ.get(ENV_SNAPSHOT_DIR)
    if raw and raw.strip():
        return Path(raw.strip()).expanduser()
    return DEFAULT_SNAPSHOT_DIR.expanduser()


def check_name(name: str) -> str:
    """Validate a snapshot name, refusing anything that could escape the directory."""
    text = (name or "").strip()
    if not _NAME_PATTERN.match(text):
        raise SnapshotError(
            f"{name!r} is not a usable snapshot name. Use 1-81 characters from letters, digits, "
            "space, dot, dash and underscore, starting with a letter or digit "
            "(no slashes, no '..')."
        )
    return text


class SnapshotStore:
    """Snapshot files on disk. One per server; the directory is created on first write."""

    def __init__(self, directory: Path | None = None) -> None:
        self._directory = Path(directory).expanduser() if directory is not None else snapshot_dir()

    @property
    def directory(self) -> Path:
        return self._directory

    def path_for(self, name: str) -> Path:
        return self._directory / f"{check_name(name)}{_SNAPSHOT_SUFFIX}"

    def save(self, doc: Mapping[str, Any], name: str) -> Path:
        """Write ``doc`` as ``<name>.json``, replacing any previous file of that name."""
        target = self.path_for(name)
        payload = dict(doc)
        payload["name"] = check_name(name)
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8"
            )
            temporary.replace(target)
        except OSError as exc:
            raise SnapshotError(f"Could not write snapshot {name!r} to {target}: {exc}") from exc
        entry_count = len(payload.get("entries", []))
        logger.info("saved snapshot %r (%d entries) to %s", name, entry_count, target)
        return target

    def load(self, name: str) -> dict[str, Any]:
        """Read ``<name>.json`` and sanity-check its schema version."""
        target = self.path_for(name)
        try:
            raw = target.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            known = ", ".join(item["name"] for item in self.list_snapshots()) or "none saved yet"
            raise SnapshotError(
                f"No snapshot named {name!r} in {self._directory}. Available: {known}."
            ) from exc
        except OSError as exc:
            raise SnapshotError(f"Could not read snapshot {name!r} from {target}: {exc}") from exc
        try:
            doc = json.loads(raw)
        except ValueError as exc:
            raise SnapshotError(f"Snapshot {name!r} at {target} is not valid JSON: {exc}") from exc
        if not isinstance(doc, dict) or "entries" not in doc:
            raise SnapshotError(
                f"Snapshot {name!r} at {target} is not a snapshot document (no 'entries' list)."
            )
        version = doc.get("schema_version")
        if version != SCHEMA_VERSION:
            raise SnapshotError(
                f"Snapshot {name!r} has schema_version {version!r}, but this server writes and "
                f"reads version {SCHEMA_VERSION}. Capture a fresh snapshot."
            )
        doc.setdefault("name", check_name(name))
        return doc

    def list_snapshots(self) -> list[dict[str, Any]]:
        """Every snapshot in the directory, newest first. Unreadable files are reported too."""
        items: list[dict[str, Any]] = []
        try:
            files = sorted(self._directory.glob(f"*{_SNAPSHOT_SUFFIX}"))
        except OSError as exc:
            logger.warning("cannot list %s: %s", self._directory, exc)
            return items
        for file in files:
            entry: dict[str, Any] = {
                "name": file.stem,
                "path": str(file),
                "created": None,
                "devices": [],
                "entries": 0,
            }
            try:
                doc = json.loads(file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                entry["error"] = f"unreadable: {exc}"
                items.append(entry)
                continue
            if isinstance(doc, dict):
                entry["created"] = doc.get("created")
                entry["schema_version"] = doc.get("schema_version")
                entry["sample_rate"] = doc.get("sample_rate")
                entry["devices"] = [
                    device.get("type_id") or device.get("name")
                    for device in doc.get("devices", [])
                    if isinstance(device, dict)
                ]
                entries = doc.get("entries")
                entry["entries"] = len(entries) if isinstance(entries, list) else 0
            items.append(entry)
        items.sort(key=lambda item: (item.get("created") or "", item["name"]), reverse=True)
        return items

    def delete(self, name: str) -> bool:
        """Remove a snapshot file. Returns False when it was not there."""
        target = self.path_for(name)
        try:
            target.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise SnapshotError(f"Could not delete snapshot {name!r} at {target}: {exc}") from exc
        return True


def list_snapshots() -> list[dict[str, Any]]:
    """Module-level convenience over the default :class:`SnapshotStore`."""
    return SnapshotStore().list_snapshots()


def load(name: str) -> dict[str, Any]:
    """Module-level convenience over the default :class:`SnapshotStore`."""
    return SnapshotStore().load(name)


def save(doc: Mapping[str, Any], name: str) -> Path:
    """Module-level convenience over the default :class:`SnapshotStore`."""
    return SnapshotStore().save(doc, name)


# ----------------------------------------------------------------------------------- capture


def _entry_meta(meta: Mapping[str, Any]) -> dict[str, Any]:
    """The metadata worth keeping: type, range and (small) enum domain."""
    kept: dict[str, Any] = {"type": str(meta.get("type", ""))}
    for key in ("min", "max"):
        if meta.get(key) is not None:
            kept[key] = meta[key]
    options = meta.get("values")
    if isinstance(options, list) and 0 < len(options) <= _MAX_ENUM_VALUES:
        if not any(isinstance(option, dict) and "children" in option for option in options):
            kept["values"] = options
    return kept


def _channel_dict(channel: ChannelInfo) -> dict[str, Any]:
    return {
        "device": channel.device,
        "kind": channel.kind,
        "index": channel.index,
        "name": channel.name,
        "io_type": channel.io_type,
        "stereo": channel.stereo,
        "has_preamp": channel.has_preamp,
        "is_monitor": channel.is_monitor,
        "path": channel.path,
    }


async def capture(client: UAClient, rig: Rig, *, name: str | None = None) -> dict[str, Any]:
    """Walk ``/devices`` and build a snapshot document of everything we may write back.

    Only allowlisted property names are kept, properties the engine reports as read-only or
    with no value at all are dropped, and clock/sample rate land in the header rather than in
    the restorable entries.
    """
    nodes = await client.walk(DEVICES_PATH)
    entries: list[dict[str, Any]] = []
    for path, node in nodes.items():
        properties = node.get("properties") or {}
        for prop_name, meta in properties.items():
            if prop_name not in CAPTURED_PROPERTIES or not isinstance(meta, dict):
                continue
            if "value" not in meta:
                logger.debug("skipping %s/%s: the engine reported no value", path, prop_name)
                continue
            if meta.get("readonly"):
                logger.debug("skipping read-only %s/%s", path, prop_name)
                continue
            entries.append(
                {
                    "path": f"{path}/{prop_name}",
                    "value": meta["value"],
                    "meta": _entry_meta(meta),
                }
            )
    entries.sort(key=lambda entry: entry["path"])

    root_properties, _ = await client.get_node("/")
    sample_rate = _root_value(root_properties, "SampleRate")
    clock_source = _root_value(root_properties, "ClockSource")

    doc: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "created": _utc_now(),
        "name": check_name(name) if name else None,
        "engine_version": _root_value(root_properties, "Version"),
        "sample_rate": sample_rate,
        "clock_source": clock_source,
        "devices": [
            {
                "index": device.index,
                "name": device.name,
                "type_id": device.type_id,
                "online": device.online,
                "multi_unit_order": device.multi_unit_order,
                "cue_bus_count": device.cue_bus_count,
            }
            for device in rig.devices
        ],
        "channels": [_channel_dict(channel) for channel in rig.channels],
        "entries": entries,
    }
    logger.info(
        "captured %d entries across %d device(s) at %s Hz",
        len(entries),
        len(rig.devices),
        sample_rate,
    )
    return doc


def _root_value(properties: Mapping[str, Any], name: str) -> Any:
    prop = properties.get(name)
    return None if prop is None else prop.value


# -------------------------------------------------------------------------------------- diff


def _matches_any(path: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


async def diff(
    client: UAClient,
    rig: Rig,
    doc: Mapping[str, Any],
    ignore: Iterable[str] = (),
) -> list[Change]:
    """Every snapshot entry whose live value differs, minus paths matching ``ignore`` globs."""
    patterns = [str(pattern) for pattern in ignore]
    entries = [
        entry
        for entry in doc.get("entries", [])
        if isinstance(entry, dict) and not _matches_any(str(entry.get("path", "")), patterns)
    ]
    snapshot_models = _model_ids(doc.get("devices", []))
    rig_models = [device.type_id for device in rig.devices]
    if snapshot_models != rig_models:
        logger.warning(
            "diffing a %s snapshot against %s", snapshot_models or "?", rig_models or "?"
        )

    current: dict[str, Any] = {}
    paths = [str(entry["path"]) for entry in entries]
    for start in range(0, len(paths), _POLL_CHUNK):
        current.update(await client.poll_values(paths[start : start + _POLL_CHUNK]))

    changes: list[Change] = []
    for entry in entries:
        path = str(entry["path"])
        if path not in current:
            logger.debug("diff: %s did not answer; treating it as unchanged", path)
            continue
        if not values_equal(entry.get("value"), current[path]):
            changes.append(
                Change(
                    path=path,
                    property=property_name(path),
                    snapshot_value=entry.get("value"),
                    current_value=current[path],
                )
            )
    return changes


def _model_ids(devices: Iterable[Any]) -> list[str]:
    ids: list[str] = []
    for device in devices:
        if isinstance(device, Mapping):
            ids.append(str(device.get("type_id") or ""))
    return ids


# ----------------------------------------------------------------------- cross-model mapping


def _ordinal_key(channel: Mapping[str, Any]) -> tuple[int, str, str]:
    return (
        int(channel.get("device", 0)),
        str(channel.get("kind", "")),
        str(channel.get("io_type") or "").casefold(),
    )


def _ordinals(channels: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Position of each channel within its (device, kind, IOType) group, by path."""
    counters: dict[tuple[int, str, str], int] = {}
    out: dict[str, int] = {}
    ordered = sorted(
        channels,
        key=lambda c: (int(c.get("device", 0)), str(c.get("kind")), int(c.get("index", 0))),
    )
    for channel in ordered:
        key = _ordinal_key(channel)
        out[str(channel["path"])] = counters.get(key, 0)
        counters[key] = counters.get(key, 0) + 1
    return out


def build_channel_map(
    doc_channels: Sequence[Mapping[str, Any]], rig: Rig
) -> dict[str, str]:
    """Map snapshot channel paths to live channel paths by I/O type and ordinal, never index."""
    live = [_channel_dict(channel) for channel in rig.channels]
    doc_ordinals = _ordinals(doc_channels)
    live_ordinals = _ordinals(live)
    live_by_slot: dict[tuple[int, str, str, int], str] = {}
    for channel in live:
        key = (*_ordinal_key(channel), live_ordinals[str(channel["path"])])
        live_by_slot[key] = str(channel["path"])

    device_map = _device_map(doc_channels, live)
    mapping: dict[str, str] = {}
    for channel in doc_channels:
        source = str(channel["path"])
        source_device = int(channel.get("device", 0))
        target_device = device_map.get(source_device)
        if target_device is None:
            continue
        key = (
            target_device,
            str(channel.get("kind", "")),
            str(channel.get("io_type") or "").casefold(),
            doc_ordinals[source],
        )
        target = live_by_slot.get(key)
        if target is not None:
            mapping[source] = target
    return mapping


def _device_map(
    doc_channels: Sequence[Mapping[str, Any]], live_channels: Sequence[Mapping[str, Any]]
) -> dict[int, int]:
    """Map snapshot device indices onto live device indices by position in the chain."""
    doc_devices = sorted({int(c.get("device", 0)) for c in doc_channels})
    live_devices = sorted({int(c.get("device", 0)) for c in live_channels})
    return {
        source: live_devices[position]
        for position, source in enumerate(doc_devices)
        if position < len(live_devices)
    }


def _remap_entries(
    doc: Mapping[str, Any], rig: Rig
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Rewrite entry paths onto the live rig. Returns ``(entries, skipped)``."""
    channels = [c for c in doc.get("channels", []) if isinstance(c, Mapping) and c.get("path")]
    mapping = build_channel_map(channels, rig)
    prefixes = sorted(mapping, key=len, reverse=True)
    device_map = _device_map(channels, [_channel_dict(c) for c in rig.channels])

    entries: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for entry in doc.get("entries", []):
        if not isinstance(entry, Mapping) or not entry.get("path"):
            continue
        path = str(entry["path"])
        target = _remap_path(path, prefixes, mapping, device_map)
        if target is None:
            skipped.append(
                {
                    "path": path,
                    "property": property_name(path),
                    "value": entry.get("value"),
                    "reason": "no channel of the same I/O type and ordinal exists on this rig",
                }
            )
            continue
        remapped = dict(entry)
        remapped["path"] = target
        if target != path:
            remapped["mapped_from"] = path
        entries.append(remapped)
    return entries, skipped


def _remap_path(
    path: str,
    prefixes: Sequence[str],
    mapping: Mapping[str, str],
    device_map: Mapping[int, int],
) -> str | None:
    for prefix in prefixes:
        if path == prefix or path.startswith(prefix + "/"):
            return mapping[prefix] + path[len(prefix) :]
    match = re.match(rf"^{DEVICES_PATH}/(\d+)(/[^/]+)$", path)
    if match is not None:
        target_device = device_map.get(int(match.group(1)))
        if target_device is None:
            return None
        return f"{DEVICES_PATH}/{target_device}{match.group(2)}"
    return None


# -------------------------------------------------------------------------------- the plan


def _classify(path: str, prop: str, *, monitor: bool) -> Phase:
    if "/preamps/" in path:
        return Phase.UNISON
    if "/sends/" in path:
        return Phase.SENDS
    if "/effects/" in path:
        if prop == EFFECT_NAME_PROPERTY:
            return Phase.EFFECT_LOAD
        if prop == "Preset":
            return Phase.PLUGIN_PRESET
        return Phase.PLUGIN_PARAMS
    if monitor:
        return Phase.MONITOR
    return Phase.CHANNEL


def plan_restore(
    doc: Mapping[str, Any],
    *,
    entries: Sequence[Mapping[str, Any]] | None = None,
    current: Mapping[str, Any] | None = None,
    include_clock: bool = False,
    include_monitor: bool = False,
) -> RestorePlan:
    """Build the exact ordered write list a restore would perform.

    Pure and synchronous: this is what ``dry_run=True`` returns, and what the apply loop walks.
    ``entries`` defaults to every entry in ``doc``; the restore path passes only the entries
    that actually differ. ``current`` (live values keyed by path) is what lets the stereo
    unlink/relink pass fire only for pairs that are really linked right now.
    """
    plan = RestorePlan()
    source = doc.get("entries", []) if entries is None else entries
    live: Mapping[str, Any] = current or {}
    monitor_paths = tuple(
        str(channel["path"])
        for channel in doc.get("channels", [])
        if isinstance(channel, Mapping) and channel.get("is_monitor") and channel.get("path")
    )

    if include_clock:
        transport = (
            ("SampleRate", doc.get("sample_rate")),
            ("ClockSource", doc.get("clock_source")),
        )
        for prop, value in transport:
            if value is None:
                continue
            plan.writes.append(
                PlannedWrite(
                    path=f"/{prop}",
                    property=prop,
                    value=value,
                    phase=Phase.CLOCK,
                    note="opt-in transport change: re-locks the whole audio chain",
                )
            )

    stereo_targets: dict[str, tuple[str, Any]] = {}
    touched_channels: set[str] = set()

    for entry in source:
        if not isinstance(entry, Mapping) or not entry.get("path"):
            continue
        path = str(entry["path"])
        prop = property_name(path)
        value = entry.get("value")
        monitor = prop in DEVICE_MONITOR_PROPERTIES or _under_any(path, monitor_paths)
        if monitor and not include_monitor:
            plan.skipped.append(
                {
                    "path": path,
                    "property": prop,
                    "value": value,
                    "reason": "monitor rail entry; pass include_monitor=true to restore it",
                }
            )
            continue
        channel = _channel_prefix(path)
        if prop == STEREO_PROPERTY and channel is not None:
            stereo_targets[channel] = (path, value)
            continue
        if channel is not None:
            touched_channels.add(channel)
        plan.writes.append(
            PlannedWrite(
                path=path,
                property=prop,
                value=value,
                phase=_classify(path, prop, monitor=monitor),
            )
        )

    plan.writes.extend(_stereo_writes(stereo_targets, touched_channels, live))
    plan.writes.sort(
        key=lambda write: (
            int(write.phase),
            _PROPERTY_RANK.get(write.property, _DEFAULT_RANK),
            write.path,
        )
    )
    return plan


def _stereo_writes(
    targets: Mapping[str, tuple[str, Any]],
    touched_channels: Iterable[str],
    live: Mapping[str, Any],
) -> list[PlannedWrite]:
    """Unlink-before / relink-after writes, emitted only where they change something.

    A pair that is linked right now is unlinked first so per-channel writes are not mirrored
    onto its partner, then relinked at the end. A pair that is already unlinked and stays
    unlinked produces no writes at all.
    """
    writes: list[PlannedWrite] = []
    channels = set(targets) | set(touched_channels)
    for channel in sorted(channels):
        stereo_path = f"{channel}/{STEREO_PROPERTY}"
        target_path, target = targets.get(channel, (stereo_path, None))
        currently = live.get(stereo_path)
        linked = currently is True
        wants = bool(target) if target is not None else linked
        if linked:
            writes.append(
                PlannedWrite(
                    path=target_path,
                    property=STEREO_PROPERTY,
                    value=False,
                    phase=Phase.UNLINK_STEREO,
                    note="unlink the stereo pair so per-channel writes are not mirrored",
                )
            )
        if wants and (linked or target is not None):
            writes.append(
                PlannedWrite(
                    path=target_path,
                    property=STEREO_PROPERTY,
                    value=True,
                    phase=Phase.RELINK_STEREO,
                    note="relink the stereo pair",
                )
            )
        elif not wants and not linked and target is not None:
            writes.append(
                PlannedWrite(
                    path=target_path,
                    property=STEREO_PROPERTY,
                    value=False,
                    phase=Phase.CHANNEL,
                    note="the snapshot has this channel unlinked",
                )
            )
    return writes


def _under_any(path: str, prefixes: Sequence[str]) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


# ------------------------------------------------------------------------------- materialize


async def wait_for_parameters(
    client: UAClient,
    effect_path: str,
    *,
    timeout_s: float | None = None,
    poll_s: float | None = None,
) -> list[str]:
    """Poll ``<effect_path>/parameters`` until its children appear, or give up.

    Plugin parameter nodes only materialize AFTER a plugin is actually loaded into the slot
    (SPEC §1), and the engine gives no signal when that finishes. Returns the child names, or
    an empty list on timeout, never raising: a slot that never materializes is a reportable
    outcome, not a crash.
    """
    timeout = MATERIALIZE_TIMEOUT_S if timeout_s is None else float(timeout_s)
    poll = MATERIALIZE_POLL_S if poll_s is None else float(poll_s)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout)
    path = f"{effect_path}/{PARAMETERS_CHILD}"
    while True:
        try:
            _, children = await client.get_node(path)
            if children:
                return list(children)
        except UAPathError:
            pass
        except UAError as exc:
            logger.debug("waiting for %s: %s", path, exc)
        if loop.time() >= deadline:
            logger.info("plugin parameters never appeared at %s within %.1fs", path, timeout)
            return []
        await asyncio.sleep(min(poll, max(0.0, deadline - loop.time())))


# ----------------------------------------------------------------------------------- restore


@dataclass
class _Outcome:
    applied: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)


async def restore(
    client: UAClient,
    rig: Rig,
    doc: Mapping[str, Any],
    *,
    gate: SafetyGate | None = None,
    dry_run: bool = True,
    include_clock: bool = False,
    include_monitor: bool = False,
    allow_cross_model: bool = False,
    audio_flowing: bool | None = None,
    store: SnapshotStore | None = None,
    pre_restore: bool = True,
) -> dict[str, Any]:
    """Apply a snapshot in the SPEC §4.4 order, collecting per-entry outcomes.

    ``dry_run=True`` (the default) writes nothing at all and returns the exact ordered write
    list. A real run first captures a ``pre-restore-<timestamp>`` snapshot so the previous
    state is always recoverable.
    """
    gate = gate or SafetyGate()
    snapshot_models = _model_ids(doc.get("devices", []))
    rig_models = [device.type_id for device in rig.devices]
    cross_model = snapshot_models != rig_models
    if cross_model and not allow_cross_model:
        raise CrossModelError(snapshot_models, rig_models)

    entries: Sequence[Mapping[str, Any]] = [
        entry for entry in doc.get("entries", []) if isinstance(entry, Mapping)
    ]
    mapping_skipped: list[dict[str, Any]] = []
    if cross_model:
        entries, mapping_skipped = _remap_entries(doc, rig)
        logger.warning(
            "cross-model restore: mapping %s onto %s by I/O type and ordinal",
            snapshot_models,
            rig_models,
        )

    live = await _read_current(client, [str(entry["path"]) for entry in entries])
    changed: list[Mapping[str, Any]] = []
    missing: list[dict[str, Any]] = []
    unchanged = 0
    for entry in entries:
        path = str(entry["path"])
        if path not in live:
            missing.append(
                {
                    "path": path,
                    "property": property_name(path),
                    "value": entry.get("value"),
                    "reason": "this rig has no such property (different model or layout)",
                }
            )
        elif values_equal(entry.get("value"), live[path]):
            unchanged += 1
        else:
            changed.append(entry)

    plan = plan_restore(
        doc,
        entries=changed,
        current=live,
        include_clock=include_clock,
        include_monitor=include_monitor,
    )
    outcome = _Outcome(skipped=[*mapping_skipped, *missing, *plan.skipped])

    result: dict[str, Any] = {
        "dry_run": dry_run,
        "cross_model": cross_model,
        "snapshot_devices": snapshot_models,
        "rig_devices": rig_models,
        "pre_restore_snapshot": None,
        "writes": _capped(write.as_dict() for write in plan.writes),
    }

    if dry_run:
        result.update(
            applied=[],
            skipped=_capped(outcome.skipped),
            failed=[],
            counts=_counts(plan, outcome, unchanged),
        )
        return result

    if pre_restore:
        result["pre_restore_snapshot"] = await _capture_pre_restore(client, rig, store)

    await _apply(client, gate, plan, outcome, audio_flowing=audio_flowing)
    result.update(
        applied=_capped(outcome.applied),
        skipped=_capped(outcome.skipped),
        failed=_capped(outcome.failed),
        counts=_counts(plan, outcome, unchanged),
    )
    logger.info(
        "restore finished: %d applied, %d skipped, %d failed, %d already correct",
        len(outcome.applied),
        len(outcome.skipped),
        len(outcome.failed),
        unchanged,
    )
    return result


async def _read_current(client: UAClient, paths: Sequence[str]) -> dict[str, Any]:
    """Live values for every entry path, read in batches on the poll socket."""
    unique = list(dict.fromkeys(paths))
    values: dict[str, Any] = {}
    for start in range(0, len(unique), _POLL_CHUNK):
        values.update(await client.poll_values(unique[start : start + _POLL_CHUNK]))
    return values


def _capped(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Truncate a report list so a whole-rig restore cannot flood the model's context."""
    listed = list(items)
    if len(listed) <= _MAX_REPORTED:
        return listed
    hidden = len(listed) - _MAX_REPORTED
    return [*listed[:_MAX_REPORTED], {"truncated": hidden, "reason": "see counts for the totals"}]


def _counts(plan: RestorePlan, outcome: _Outcome, unchanged: int) -> dict[str, int]:
    return {
        "planned": len(plan.writes),
        "applied": len(outcome.applied),
        "skipped": len(outcome.skipped),
        "failed": len(outcome.failed),
        "unchanged": unchanged,
    }


async def _capture_pre_restore(
    client: UAClient, rig: Rig, store: SnapshotStore | None
) -> str | None:
    """Snapshot the current state before touching anything. Never fatal."""
    name = f"{_PRE_RESTORE_PREFIX}{_timestamp_name()}"
    try:
        doc = await capture(client, rig, name=name)
        (store or SnapshotStore()).save(doc, name)
    except (UAError, SnapshotError, OSError) as exc:
        logger.warning("could not capture %s before restoring: %s", name, exc)
        return None
    return name


async def _apply(
    client: UAClient,
    gate: SafetyGate,
    plan: RestorePlan,
    outcome: _Outcome,
    *,
    audio_flowing: bool | None,
) -> None:
    limits = gate.describe()
    interval = 1.0 / max(1.0, float(limits.get("writes_per_second") or 1.0))
    burst = max(1, int(limits.get("write_burst") or 1))
    dead_slots: set[str] = set()
    written = 0

    for write in plan.writes:
        slot = write.slot
        if slot is not None and slot in dead_slots:
            outcome.skipped.append(
                _record(write, reason=f"{slot} never materialized its parameter nodes")
            )
            continue
        try:
            prop = await client.get_prop(write.path)
        except UAPathError:
            outcome.skipped.append(
                _record(write, reason="this rig has no such property (different model or layout)")
            )
            continue
        except UAError as exc:
            outcome.failed.append(_record(write, error=str(exc)))
            continue

        if values_equal(prop.value, write.value):
            outcome.skipped.append(_record(write, reason="already at the snapshot value"))
            continue

        context = await _context_for(client, write, audio_flowing=audio_flowing)
        try:
            value = gate.check_write(prop, write.value, context=context)
        except (SafetyRefusal, ValueError) as exc:
            outcome.failed.append(_record(write, error=str(exc)))
            continue

        if written >= burst - 1:
            # Stay inside the gate's token bucket instead of mass-failing a long restore.
            await asyncio.sleep(interval)
        try:
            after = await client.set_value(write.path, value)
        except UAError as exc:
            outcome.failed.append(_record(write, error=str(exc)))
            continue
        written += 1
        if write.property == MONITOR_LEVEL_PROPERTY and _is_number(after):
            gate.note_monitor_event(float(after))
        outcome.applied.append(
            {
                **_record(write),
                "before": prop.value,
                "after": after,
                "verified": values_equal(after, value),
            }
        )

        if write.property == EFFECT_NAME_PROPERTY and value and slot is not None:
            children = await wait_for_parameters(client, slot)
            if not children:
                dead_slots.add(slot)
                outcome.applied[-1].setdefault("warnings", []).append(
                    f"parameter nodes did not appear within {MATERIALIZE_TIMEOUT_S:g}s; "
                    "preset and parameter entries for this slot were skipped"
                )


async def _context_for(
    client: UAClient, write: PlannedWrite, *, audio_flowing: bool | None
) -> WriteContext:
    reason = f"snapshot restore ({write.phase.name.lower()})"
    if write.property in CLOCK_PROPERTIES:
        return WriteContext(reason=reason, confirm=True, audio_flowing=audio_flowing)
    if write.property in MONITOR_RELEASE_PROPERTIES and write.value is False:
        level = await _monitor_level(client, write.path)
        return WriteContext(reason=reason, monitor_level_db=level)
    return WriteContext(reason=reason)


async def _monitor_level(client: UAClient, path: str) -> float | None:
    """The stored monitor level next to ``path``, so an unmute/un-dim can be railed."""
    parent = path.rsplit("/", 1)[0]
    with contextlib.suppress(UAError):
        value = await client.get_value(f"{parent}/{MONITOR_LEVEL_PROPERTY}")
        if _is_number(value):
            return float(value)
    return None


def _record(
    write: PlannedWrite, *, reason: str | None = None, error: str | None = None
) -> dict[str, Any]:
    record: dict[str, Any] = write.as_dict()
    if reason is not None:
        record["reason"] = reason
    if error is not None:
        record["error"] = error
    return record
