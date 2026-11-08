"""The driver I/O map: what macOS apps see as the Apollo's input and output channels.

``/inputroutes`` and ``/outputroutes`` list the channels the driver hands to apps, in the order
apps number them (route 0 is channel 1). Console's I/O Matrix decides that order. Three kinds of
driver input are not hardware at all: ``CtrlRoom`` (``MON L/R``, the Console monitor mix) and
``Aux`` (``AUX1 L/R``, ``AUX2 L/R``, the aux buses). Recording those is how an Apollo does
loopback, and ``Virtual`` outputs are how computer audio gets into a Console strip.

Everything here is pure: the server reads the routes and hands them in. Nothing is written; the
routing matrix is read-only for this server (see ``uad://limitations``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

MONITOR_MIX = "monitor_mix"
CAPTURE_BUSES = (MONITOR_MIX, "aux1", "aux2")


def route_entries(
    children: Sequence[str], values: Mapping[str, Any], root: str
) -> list[dict[str, Any]]:
    """One entry per route, in driver order, from a poll of Name/IOType/ChannelIndex/Active."""
    ordered = sorted((c for c in children if str(c).isdigit()), key=int)
    entries = []
    for position, child in enumerate(ordered, start=1):
        base = f"{root}/{child}"
        index = values.get(f"{base}/ChannelIndex")
        entries.append(
            {
                "channel": position,
                "name": str(values.get(f"{base}/Name") or ""),
                "io_type": str(values.get(f"{base}/IOType") or ""),
                "index": int(index) if isinstance(index, int | float) else None,
                "active": bool(values.get(f"{base}/Active", True)),
            }
        )
    return entries


def _pair(
    entries: Sequence[Mapping[str, Any]], io_type: str, first: int
) -> list[Mapping[str, Any]]:
    """The two routes of ``io_type`` whose per-type index is ``first`` and ``first + 1``."""
    found = {}
    for entry in entries:
        if entry["io_type"] == io_type and entry["index"] in (first, first + 1):
            found.setdefault(entry["index"], entry)
    return [found[i] for i in (first, first + 1) if i in found]


def _describe(pair: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    if not pair:
        return None
    return {
        "channels": [entry["channel"] for entry in pair],
        "names": [entry["name"] for entry in pair],
        "active": all(entry["active"] for entry in pair),
    }


def capture(inputs: Sequence[Mapping[str, Any]], bus: str) -> dict[str, Any] | None:
    """Driver input channels that record ``bus``: ``monitor_mix``, ``aux1`` or ``aux2``."""
    if bus == MONITOR_MIX:
        return _describe(_pair(inputs, "CtrlRoom", 1))
    if bus.startswith("aux") and bus[3:].isdigit():
        n = int(bus[3:])
        return _describe(_pair(inputs, "Aux", 2 * n - 1))
    return None


def virtual_output(outputs: Sequence[Mapping[str, Any]], ordinal: int) -> dict[str, Any] | None:
    """Driver output channels feeding the Virtual strips ``ordinal`` and ``ordinal + 1``."""
    return _describe(_pair(outputs, "Virtual", ordinal))


def cue_output(outputs: Sequence[Mapping[str, Any]], bus: str) -> dict[str, Any] | None:
    """Driver output channels that play straight into cue bus ``bus`` (``cue1`` ...)."""
    if not (bus.startswith("cue") and bus[3:].isdigit()):
        return None
    n = int(bus[3:])
    return _describe(_pair(outputs, "Cue", 2 * n - 1))


def summarize(
    inputs: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
    *,
    preset: Any,
    presets: Sequence[Any] | None,
) -> dict[str, Any]:
    """The routing map plus the facts a livestream or call setup needs, ready to show."""
    names = []
    for option in presets or []:
        if isinstance(option, Mapping):
            option = option.get("value")
        if option:
            names.append(str(option))
    first_two = [entry["name"] for entry in inputs[:2]]
    monitor = capture(inputs, MONITOR_MIX)
    loopback = {bus: capture(inputs, bus) for bus in CAPTURE_BUSES}
    notes = []
    if monitor and monitor["channels"] != [1, 2]:
        notes.append(
            f"Apps that only record inputs 1-2 (Zoom, browsers, most stream apps) get "
            f"{' / '.join(first_two) or 'nothing'}, not the Console mix, which is on inputs "
            f"{monitor['channels'][0]}-{monitor['channels'][-1]}. To change that, move MON L/R "
            "to inputs 1-2 in UAD Console > Settings > I/O Matrix"
            + (" (it lists a 'Livestreaming' preset)" if "Livestreaming" in names else "")
            + ". Your DAW's input numbering moves with it."
        )
    disabled = [
        entry["name"] for entry in outputs if entry["io_type"] == "Virtual" and not entry["active"]
    ]
    if disabled:
        notes.append(
            f"Disabled in the I/O Matrix, so apps cannot play to them: {', '.join(disabled)}. "
            "Enable them in UAD Console > Settings > I/O Matrix if you need them."
        )
    return {
        "io_matrix_preset": preset or None,
        "io_matrix_presets": names,
        "inputs_1_2": first_two,
        "loopback": loopback,
        "virtual_outputs": [entry for entry in outputs if entry["io_type"] == "Virtual"],
        "driver_inputs": inputs,
        "driver_outputs": outputs,
        "notes": notes,
    }
