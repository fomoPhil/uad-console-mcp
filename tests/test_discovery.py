"""Topology discovery tests (docs/SPEC.md §4.2).

Every engine here is a ``MockEngine`` on an ephemeral loopback port - nothing in this file may
touch 127.0.0.1:4710, which is the author's real Apollo.

The x8 Gen 2 capture is the only fixture on disk; the ``cascade`` and ``twin`` trees are derived in
memory (SPEC §3). ``twin`` re-indexes its channels precisely so that any assumption about x8
indices - above all "the monitor output is output 22" - fails loudly.
"""

from __future__ import annotations

import copy
from collections import Counter
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest

from tests.fixtures.derive import cascade_fixture, twin_fixture
from tests.mock_engine import MockEngine
from uad_console_mcp.client import UAClient
from uad_console_mcp.discovery import (
    MONITOR_LEVEL_PROPERTY,
    AmbiguousTargetError,
    ChannelInfo,
    DeviceInfo,
    Rig,
    TargetNotFoundError,
    discover,
)

Tree = dict[str, Any]

# The captured Apollo x8 Gen 2, as facts rather than as indices.
X8_INPUT_COUNT = 28
X8_OUTPUT_COUNT = 26
X8_AUX_COUNT = 2
X8_IO_TYPES = {
    "Mic": 4,
    "Line": 4,
    "ADAT": 8,
    "S/PDIF": 2,
    "Virtual": 8,
    "TalkbackMic": 1,
    None: 1,  # the spare "N/A 1" strip reports IOType with no value at all
}
X8_PREAMP_COUNT = 4


# ------------------------------------------------------------------------------------ helpers


@asynccontextmanager
async def rig_for(engine: MockEngine) -> AsyncIterator[Rig]:
    """Discover against a started :class:`MockEngine`, closing the client afterwards."""
    assert engine.port not in (0, MockEngine.REAL_ENGINE_PORT), "must be an ephemeral mock port"
    client = UAClient(port=engine.port)
    try:
        yield await discover(client)
    finally:
        await client.aclose()


def container_indices(tree: Tree, container: str, device: int = 0) -> list[int]:
    root = f"/devices/{device}/{container}"
    return sorted(int(index) for index in tree["nodes"][root]["children"])


def property_meta(tree: Tree, path: str, name: str) -> dict[str, Any] | None:
    meta = tree["nodes"][path]["properties"].get(name)
    return meta if isinstance(meta, dict) else None


def monitor_indices(tree: Tree, device: int = 0) -> list[int]:
    """Output indices carrying ``CRMonitorLevel`` - the only legitimate monitor test."""
    root = f"/devices/{device}/outputs"
    return [
        index
        for index in container_indices(tree, "outputs", device)
        if property_meta(tree, f"{root}/{index}", MONITOR_LEVEL_PROPERTY) is not None
    ]


def move_monitor_marker(tree: Tree, target: int, device: int = 0) -> Tree:
    """Relocate the monitor marker properties onto output ``target``."""
    moved = copy.deepcopy(tree)
    root = f"/devices/{device}/outputs"
    source = monitor_indices(tree, device)[0]
    source_props = moved["nodes"][f"{root}/{source}"]["properties"]
    target_props = moved["nodes"][f"{root}/{target}"]["properties"]
    for name in (MONITOR_LEVEL_PROPERTY, f"{MONITOR_LEVEL_PROPERTY}Tapered"):
        if name in source_props:
            target_props[name] = source_props.pop(name)
    return moved


def channel(
    name: str,
    *,
    device: int = 0,
    kind: str = "input",
    index: int = 0,
    io_type: str | None = "Mic",
    is_monitor: bool = False,
    is_talkback: bool = False,
    has_preamp: bool = False,
) -> ChannelInfo:
    container = {"input": "inputs", "aux": "auxs", "output": "outputs"}[kind]
    return ChannelInfo(
        device=device,
        kind=kind,  # type: ignore[arg-type]
        index=index,
        name=name,
        io_type=io_type,
        stereo=False,
        hidden=False,
        active=True,
        has_preamp=has_preamp,
        is_monitor=is_monitor,
        is_talkback=is_talkback,
        path=f"/devices/{device}/{container}/{index}",
    )


def synthetic_rig(*channels: ChannelInfo, devices: int = 1) -> Rig:
    infos = [
        DeviceInfo(
            index=index,
            name=f"Apollo {index}",
            type_id="Apollo x8 Gen 2",
            online=True,
            multi_unit_order=index,
            cue_bus_count=2,
        )
        for index in range(devices)
    ]
    return Rig(devices=infos, channels=list(channels), engine_connected=True)


@pytest.fixture(scope="session")
def cascade_tree(base_fixture: Tree) -> Tree:
    return cascade_fixture(base_fixture)


@pytest.fixture(scope="session")
def twin_tree(base_fixture: Tree) -> Tree:
    return twin_fixture(base_fixture)


# ------------------------------------------------------------------------- x8: what is there


async def test_discovers_the_single_x8_with_its_identity(mock: MockEngine) -> None:
    async with rig_for(mock) as rig:
        assert len(rig.devices) == 1
        device = rig.devices[0]
        assert device.index == 0
        assert device.name == "Apollo x8 Gen 2"
        assert device.type_id == "Apollo x8 Gen 2"
        assert device.online is True
        assert device.multi_unit_order == 0
        assert device.cue_bus_count == 2
        assert rig.engine_connected is True


async def test_x8_channel_counts(mock: MockEngine, base_fixture: Tree) -> None:
    async with rig_for(mock) as rig:
        counts = Counter(ch.kind for ch in rig.channels)
        assert counts["input"] == X8_INPUT_COUNT == len(container_indices(base_fixture, "inputs"))
        assert counts["output"] == X8_OUTPUT_COUNT
        assert counts["output"] == len(container_indices(base_fixture, "outputs"))
        assert counts["aux"] == X8_AUX_COUNT == len(container_indices(base_fixture, "auxs"))
        assert len(rig.channels) == X8_INPUT_COUNT + X8_OUTPUT_COUNT + X8_AUX_COUNT


async def test_x8_input_io_types(mock: MockEngine) -> None:
    async with rig_for(mock) as rig:
        assert Counter(ch.io_type for ch in rig.channels_of("input")) == Counter(X8_IO_TYPES)


async def test_talkback_is_flagged_from_io_type_only(mock: MockEngine) -> None:
    async with rig_for(mock) as rig:
        talkback = [ch for ch in rig.channels if ch.is_talkback]
        assert len(talkback) == 1
        assert talkback[0].name == "TALKBACK"
        assert talkback[0].io_type == "TalkbackMic"
        assert talkback[0].path == f"/devices/0/inputs/{talkback[0].index}"
        assert all(ch.io_type == "TalkbackMic" for ch in talkback)
        others = [ch for ch in rig.channels if ch is not talkback[0]]
        assert not any(ch.is_talkback for ch in others)


async def test_preamps_come_from_the_child_node_not_the_index(mock: MockEngine) -> None:
    async with rig_for(mock) as rig:
        with_preamp = [ch for ch in rig.channels if ch.has_preamp]
        assert len(with_preamp) == X8_PREAMP_COUNT
        assert all(ch.kind == "input" for ch in with_preamp)
        assert all(ch.io_type == "Mic" for ch in with_preamp)
        assert not any(ch.has_preamp for ch in rig.channels_of("output"))
        assert not any(ch.has_preamp for ch in rig.channels_of("aux"))


async def test_a_property_with_no_value_becomes_none(mock: MockEngine, base_fixture: Tree) -> None:
    def io_type_meta(index: int) -> dict[str, Any]:
        return property_meta(base_fixture, f"/devices/0/inputs/{index}", "IOType") or {}

    spare = [
        index
        for index in container_indices(base_fixture, "inputs")
        if "value" not in io_type_meta(index)
    ]
    assert spare, "fixture should contain a strip whose IOType has no value"
    async with rig_for(mock) as rig:
        for index in spare:
            assert rig.resolve(index).io_type is None


async def test_flags_match_the_tree(mock: MockEngine, base_fixture: Tree) -> None:
    async with rig_for(mock) as rig:
        for ch in rig.channels_of("input"):
            node = f"/devices/0/inputs/{ch.index}"
            for attribute, name, default in (
                ("stereo", "Stereo", False),
                ("hidden", "ChannelHidden", False),
                ("active", "Active", True),
            ):
                meta = property_meta(base_fixture, node, name) or {}
                assert getattr(ch, attribute) is bool(meta.get("value", default))
        assert any(ch.stereo for ch in rig.channels_of("input"))
        assert any(ch.hidden for ch in rig.channels_of("input"))
        assert any(not ch.active for ch in rig.channels_of("input"))


async def test_auxs_are_discovered_without_io_type(mock: MockEngine) -> None:
    async with rig_for(mock) as rig:
        auxs = rig.channels_of("aux")
        assert [ch.name for ch in auxs] == ["AUX 1", "AUX 2"]
        assert all(ch.io_type is None for ch in auxs)
        assert all(not ch.is_monitor and not ch.is_talkback for ch in auxs)
        assert [ch.path for ch in auxs] == ["/devices/0/auxs/0", "/devices/0/auxs/1"]


# -------------------------------------------------------------------------- monitor discovery


async def test_monitor_output_is_found_by_its_property(
    mock: MockEngine, base_fixture: Tree
) -> None:
    expected = monitor_indices(base_fixture)
    assert len(expected) == 1
    async with rig_for(mock) as rig:
        monitor = rig.monitor_output()
        assert monitor.index == expected[0]
        assert monitor.kind == "output"
        assert monitor.is_monitor is True
        assert monitor.name == "MONITOR"
        # Nothing positional: it is neither the first nor the last output.
        outputs = rig.channels_of("output")
        assert monitor.index not in (outputs[0].index, outputs[-1].index)
        assert [ch.index for ch in outputs if ch.is_monitor] == expected


async def test_monitor_output_follows_the_property_when_it_moves(
    mock_factory: Callable[..., Any], base_fixture: Tree
) -> None:
    original = monitor_indices(base_fixture)[0]
    target = 3
    assert target != original
    engine = await mock_factory(move_monitor_marker(base_fixture, target))
    async with rig_for(engine) as rig:
        monitor = rig.monitor_output()
        assert monitor.index == target
        assert monitor.name != "MONITOR", "the marker moved onto a differently named output"
        assert [ch.index for ch in rig.channels_of("output") if ch.is_monitor] == [target]


async def test_monitor_output_missing_is_an_actionable_error(
    mock_factory: Callable[..., Any], base_fixture: Tree
) -> None:
    stripped = copy.deepcopy(base_fixture)
    root = "/devices/0/outputs"
    for index in monitor_indices(base_fixture):
        stripped["nodes"][f"{root}/{index}"]["properties"].pop(MONITOR_LEVEL_PROPERTY)
    engine = await mock_factory(stripped)
    async with rig_for(engine) as rig:
        with pytest.raises(TargetNotFoundError) as excinfo:
            rig.monitor_output()
        assert MONITOR_LEVEL_PROPERTY in str(excinfo.value)


# ------------------------------------------------------------------------------------ cascade


async def test_cascade_discovers_both_devices(
    mock_factory: Callable[..., Any], cascade_tree: Tree
) -> None:
    engine = await mock_factory(cascade_tree)
    async with rig_for(engine) as rig:
        assert [d.index for d in rig.devices] == [0, 1]
        assert [d.multi_unit_order for d in rig.devices] == [0, 1]
        assert rig.devices[1].name == "Apollo x8 Gen 2 #2"
        per_device = Counter(ch.device for ch in rig.channels)
        assert per_device[0] == per_device[1]
        assert len(rig.channels_of("input", device=1)) == X8_INPUT_COUNT


async def test_cascade_index_target_needs_a_device(
    mock_factory: Callable[..., Any], cascade_tree: Tree
) -> None:
    engine = await mock_factory(cascade_tree)
    async with rig_for(engine) as rig:
        with pytest.raises(AmbiguousTargetError) as excinfo:
            rig.resolve(0)
        assert len(excinfo.value.candidates) == 2
        assert {ch.device for ch in excinfo.value.candidates} == {0, 1}
        assert "device=" in str(excinfo.value)


@pytest.mark.parametrize("selector", [1, "Apollo x8 Gen 2 #2", "#2"])
async def test_cascade_device_selector_scopes_an_index(
    mock_factory: Callable[..., Any], cascade_tree: Tree, selector: str | int
) -> None:
    engine = await mock_factory(cascade_tree)
    async with rig_for(engine) as rig:
        resolved = rig.resolve(0, device=selector)
        assert resolved.device == 1
        assert resolved.path == "/devices/1/inputs/0"
        assert resolved.name == "B ANALOG 1"


async def test_cascade_ambiguous_name_across_devices(
    mock_factory: Callable[..., Any], cascade_tree: Tree
) -> None:
    engine = await mock_factory(cascade_tree)
    async with rig_for(engine) as rig:
        with pytest.raises(AmbiguousTargetError) as excinfo:
            rig.resolve("AUX 1", kind="aux")
        candidates = excinfo.value.candidates
        assert len(candidates) == 2
        assert {ch.device for ch in candidates} == {0, 1}
        assert all(ch.kind == "aux" for ch in candidates)
        assert "AUX 1" in str(excinfo.value)

        assert rig.resolve("AUX 1", kind="aux", device=0).device == 0


async def test_cascade_exact_name_beats_a_prefixed_twin(
    mock_factory: Callable[..., Any], cascade_tree: Tree
) -> None:
    engine = await mock_factory(cascade_tree)
    async with rig_for(engine) as rig:
        assert rig.resolve("ANALOG 1").device == 0
        assert rig.resolve("B ANALOG 1").device == 1


async def test_cascade_monitor_output_needs_a_device(
    mock_factory: Callable[..., Any], cascade_tree: Tree
) -> None:
    engine = await mock_factory(cascade_tree)
    async with rig_for(engine) as rig:
        with pytest.raises(AmbiguousTargetError) as excinfo:
            rig.monitor_output()
        assert len(excinfo.value.candidates) == 2
        assert "device=" in str(excinfo.value)
        assert rig.monitor_output(1).device == 1
        assert rig.monitor_output(0).device == 0


# --------------------------------------------------------------------------------------- twin


async def test_twin_topology_has_no_x8_indices(
    mock_factory: Callable[..., Any], twin_tree: Tree
) -> None:
    engine = await mock_factory(twin_tree)
    async with rig_for(engine) as rig:
        assert rig.devices[0].type_id == "Apollo Twin X"
        inputs = rig.channels_of("input")
        assert [ch.index for ch in inputs] == [0, 1, 2, 3, 4, 5]
        assert [ch.has_preamp for ch in inputs] == [True, True, False, False, False, False]
        assert Counter(ch.io_type for ch in inputs) == {"Mic": 2, "Line": 2, "Virtual": 2}
        assert len(rig.channels_of("output")) == 1


async def test_twin_monitor_output_is_not_the_x8_index(
    mock_factory: Callable[..., Any], twin_tree: Tree, base_fixture: Tree
) -> None:
    engine = await mock_factory(twin_tree)
    async with rig_for(engine) as rig:
        monitor = rig.monitor_output()
        assert monitor.index == monitor_indices(twin_tree)[0]
        assert monitor.index != monitor_indices(base_fixture)[0]
        assert monitor.path == f"/devices/0/outputs/{monitor.index}"


async def test_twin_resolution_still_works_by_name_and_index(
    mock_factory: Callable[..., Any], twin_tree: Tree
) -> None:
    engine = await mock_factory(twin_tree)
    async with rig_for(engine) as rig:
        first = rig.resolve(0)
        assert first.has_preamp is True
        assert rig.resolve(first.name) == first
        with pytest.raises(TargetNotFoundError) as excinfo:
            rig.resolve(22)
        assert "22" in str(excinfo.value)


# ------------------------------------------------------------------------ resolve edge cases


def test_exact_match_beats_prefix_and_substring() -> None:
    rig = synthetic_rig(
        channel("VOX", index=0),
        channel("VOX 2", index=1),
        channel("LEAD VOX", index=2),
    )
    assert rig.resolve("VOX").index == 0
    assert rig.resolve("VOX 2").index == 1


def test_prefix_match_beats_substring() -> None:
    rig = synthetic_rig(channel("LEAD VOX", index=0), channel("VOX GTR", index=1))
    assert rig.resolve("VOX").index == 1
    assert rig.resolve("GTR").index == 1
    assert rig.resolve("LEAD").index == 0


def test_matching_ignores_case_and_surrounding_space() -> None:
    rig = synthetic_rig(channel("ANALOG 1", index=0))
    for spelling in ("analog 1", "  ANALOG 1  ", "AnAlOg 1"):
        assert rig.resolve(spelling).index == 0


def test_ambiguity_carries_every_candidate() -> None:
    rig = synthetic_rig(channel("VOX A", index=3), channel("VOX B", index=4))
    with pytest.raises(AmbiguousTargetError) as excinfo:
        rig.resolve("VOX")
    assert [ch.index for ch in excinfo.value.candidates] == [3, 4]
    assert excinfo.value.target == "VOX"
    message = str(excinfo.value)
    assert "VOX A" in message
    assert "VOX B" in message


def test_missing_name_lists_close_names() -> None:
    rig = synthetic_rig(channel("VOX A", index=0), channel("KICK IN", index=1))
    with pytest.raises(TargetNotFoundError) as excinfo:
        rig.resolve("VOZ A")
    assert "VOX A" in str(excinfo.value)
    assert excinfo.value.close


def test_digit_string_falls_back_to_an_index_but_names_win() -> None:
    rig = synthetic_rig(channel("KICK", index=3), channel("SNARE", index=4))
    assert rig.resolve("3").index == 3
    named = synthetic_rig(channel("3", index=7), channel("KICK", index=3))
    assert named.resolve("3").index == 7


def test_kind_filters_the_pool() -> None:
    rig = synthetic_rig(
        channel("ANALOG 1", kind="input", index=0),
        channel("AUX 1", kind="aux", index=0, io_type=None),
        channel("MONITOR", kind="output", index=5, io_type="Monitor", is_monitor=True),
    )
    assert rig.resolve("AUX 1", kind="aux").kind == "aux"
    assert rig.resolve("MONITOR", kind="output").index == 5
    with pytest.raises(TargetNotFoundError):
        rig.resolve("AUX 1")


@pytest.mark.parametrize("kind", ["inputs", "channel", "", "INPUTZ"])
def test_unknown_kind_is_a_caller_bug(kind: str) -> None:
    rig = synthetic_rig(channel("ANALOG 1"))
    with pytest.raises(ValueError, match="kind must be one of"):
        rig.resolve("ANALOG 1", kind=kind)


@pytest.mark.parametrize("target", [True, False, None, "", "   ", 1.5, b"x"])
def test_unusable_targets_raise_value_error(target: Any) -> None:
    rig = synthetic_rig(channel("ANALOG 1"))
    with pytest.raises(ValueError):
        rig.resolve(target)


def test_unknown_index_lists_the_available_ones() -> None:
    rig = synthetic_rig(channel("ANALOG 1", index=0), channel("ANALOG 2", index=1))
    with pytest.raises(TargetNotFoundError) as excinfo:
        rig.resolve(9)
    message = str(excinfo.value)
    assert "9" in message
    assert "0, 1" in message


def test_unknown_device_lists_the_available_ones() -> None:
    rig = synthetic_rig(channel("ANALOG 1"))
    with pytest.raises(TargetNotFoundError) as excinfo:
        rig.resolve(0, device=7)
    assert "device" in str(excinfo.value)
    with pytest.raises(TargetNotFoundError):
        rig.resolve(0, device="Neve")
    with pytest.raises(TargetNotFoundError):
        rig.device(7)


def test_monitor_output_without_a_monitor_explains_the_marker() -> None:
    rig = synthetic_rig(channel("ANALOG 1", kind="output", index=0, io_type="Line"))
    with pytest.raises(TargetNotFoundError) as excinfo:
        rig.monitor_output()
    assert MONITOR_LEVEL_PROPERTY in str(excinfo.value)


def test_channel_label_is_human_readable() -> None:
    labelled = channel("ANALOG 1", device=1, index=4)
    assert labelled.label() == "'ANALOG 1' (device 1 input 4)"
