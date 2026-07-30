"""Derived test fixtures (docs/SPEC.md §3).

The captured Apollo x8 Gen 2 tree is the only fixture on disk. The ``cascade`` and ``twin``
variants are GENERATED IN MEMORY by this module and must never be written to disk.

Fixture format::

    {"_meta": {...},
     "nodes": {"<path>": {"properties": {"<name>": {...metadata}}, "children": ["a", "b"]}}}
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

Node = dict[str, Any]
Fixture = dict[str, Any]

FIXTURE_DIR = Path(__file__).resolve().parent
BASE_FIXTURE_PATH = FIXTURE_DIR / "apollo_x8_gen2_tree.json"

CASCADE_DEVICE_NAME = "Apollo x8 Gen 2 #2"
CASCADE_INPUT_PREFIX = "B "
TWIN_DEVICE_TYPE_ID = "Apollo Twin X"
TWIN_PREAMP_INPUTS = 2
TWIN_LINE_INPUTS = 2
TWIN_VIRTUAL_INPUTS = 2
MONITOR_MARKER_PROPERTY = "CRMonitorLevel"


def load_base() -> Fixture:
    """Load the captured Apollo x8 Gen 2 fixture from disk."""
    with BASE_FIXTURE_PATH.open(encoding="utf-8") as handle:
        fixture: Fixture = json.load(handle)
    return fixture


# ---------------------------------------------------------------------------- helpers


def _child_paths(nodes: dict[str, Node], root: str) -> list[str]:
    """All paths at or below ``root``, parents before children."""
    prefix = root if root.endswith("/") else root + "/"
    return sorted(path for path in nodes if path == root or path.startswith(prefix))


def _set_property_value(properties: dict[str, Any], name: str, value: Any) -> None:
    meta = properties.get(name)
    if isinstance(meta, dict):
        meta["value"] = value
        return
    inferred = "bool" if isinstance(value, bool) else "int" if isinstance(value, int) else "string"
    properties[name] = {"type": inferred, "value": value}


def _graft(
    source: dict[str, Node], target: dict[str, Node], source_root: str, target_root: str
) -> None:
    """Copy ``source_root`` and every descendant into ``target`` under ``target_root``."""
    for path in _child_paths(source, source_root):
        suffix = path[len(source_root) :]
        target[target_root + suffix] = copy.deepcopy(source[path])


def _property_value(nodes: dict[str, Node], path: str, name: str) -> Any:
    meta = nodes.get(path, {}).get("properties", {}).get(name)
    return meta.get("value") if isinstance(meta, dict) else None


def _derived_meta(
    base: Fixture, nodes: dict[str, Node], *, device: str, kind: str
) -> dict[str, Any]:
    meta = dict(base.get("_meta", {}))
    meta.update(
        {
            "device": device,
            "derived": kind,
            "derived_from": base.get("_meta", {}).get("device"),
            "captured_via": "generated in memory by tests/fixtures/derive.py (never committed)",
            "node_count": len(nodes),
            "errors": [],
        }
    )
    return meta


# ---------------------------------------------------------------------------- cascade


def cascade_fixture(base: Fixture) -> Fixture:
    """Two cascaded Apollos: ``/devices/0`` duplicated as ``/devices/1``.

    Device 1 gets ``DeviceName`` "Apollo x8 Gen 2 #2", ``MultiUnitOrder`` 1 and every input
    ``Name`` prefixed with "B " so channel resolution cannot rely on names alone.
    """
    out: Fixture = copy.deepcopy(base)
    nodes: dict[str, Node] = out["nodes"]

    _graft(nodes, nodes, "/devices/0", "/devices/1")

    device = nodes["/devices/1"]["properties"]
    _set_property_value(device, "DeviceName", CASCADE_DEVICE_NAME)
    _set_property_value(device, "MultiUnitOrder", 1)
    if "DeviceHwID" in device:
        _set_property_value(device, "DeviceHwID", 1)

    inputs_root = "/devices/1/inputs"
    for index in nodes[inputs_root]["children"]:
        properties = nodes[f"{inputs_root}/{index}"]["properties"]
        name = properties.get("Name")
        if isinstance(name, dict) and isinstance(name.get("value"), str):
            name["value"] = CASCADE_INPUT_PREFIX + name["value"]

    nodes["/devices"]["children"] = ["0", "1"]
    out["_meta"] = _derived_meta(
        base, nodes, device="Apollo x8 Gen 2 cascade (x2)", kind="cascade"
    )
    return out


# ---------------------------------------------------------------------------- twin


def twin_fixture(base: Fixture) -> Fixture:
    """A single small interface: 2 preamp + 2 line + 2 virtual inputs, 1 monitor output.

    Channels are RE-INDEXED contiguously (inputs 0-5, the monitor output at 0) precisely so
    that anything hardcoding x8 indices (notably monitor output 22) breaks. Named
    ``DeviceTypeID`` "Apollo Twin X" so cross-model checks trip.
    """
    source: Fixture = copy.deepcopy(base)
    src_nodes: dict[str, Node] = source["nodes"]
    inputs_root = "/devices/0/inputs"
    outputs_root = "/devices/0/outputs"

    input_indices = sorted(src_nodes[inputs_root]["children"], key=int)
    preamps = [
        index
        for index in input_indices
        if "preamps" in src_nodes[f"{inputs_root}/{index}"]["children"]
    ][:TWIN_PREAMP_INPUTS]
    lines = [
        index
        for index in input_indices
        if index not in preamps
        and _property_value(src_nodes, f"{inputs_root}/{index}", "IOType") == "Line"
    ][:TWIN_LINE_INPUTS]
    virtuals = [
        index
        for index in input_indices
        if _property_value(src_nodes, f"{inputs_root}/{index}", "IOType") == "Virtual"
    ][:TWIN_VIRTUAL_INPUTS]
    keep_inputs = preamps + lines + virtuals

    monitors = [
        index
        for index in sorted(src_nodes[outputs_root]["children"], key=int)
        if MONITOR_MARKER_PROPERTY in src_nodes[f"{outputs_root}/{index}"]["properties"]
    ][:1]
    if not monitors:  # pragma: no cover - the captured fixture always has a monitor
        raise ValueError(f"no output carries {MONITOR_MARKER_PROPERTY}")

    nodes: dict[str, Node] = {
        path: node
        for path, node in src_nodes.items()
        if not path.startswith(inputs_root + "/") and not path.startswith(outputs_root + "/")
    }
    for new_index, old_index in enumerate(keep_inputs):
        _graft(src_nodes, nodes, f"{inputs_root}/{old_index}", f"{inputs_root}/{new_index}")
    for new_index, old_index in enumerate(monitors):
        _graft(src_nodes, nodes, f"{outputs_root}/{old_index}", f"{outputs_root}/{new_index}")

    nodes[inputs_root]["children"] = [str(i) for i in range(len(keep_inputs))]
    nodes[outputs_root]["children"] = [str(i) for i in range(len(monitors))]

    device = nodes["/devices/0"]["properties"]
    _set_property_value(device, "DeviceTypeID", TWIN_DEVICE_TYPE_ID)
    _set_property_value(device, "DeviceName", TWIN_DEVICE_TYPE_ID)

    out: Fixture = {
        "_meta": _derived_meta(base, nodes, device=TWIN_DEVICE_TYPE_ID, kind="twin"),
        "nodes": nodes,
    }
    return out
