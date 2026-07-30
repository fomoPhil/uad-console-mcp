"""Snapshot capture, diff and ordered restore tests (docs/SPEC.md §4.4, §4.7).

Every engine here is a ``MockEngine`` on an ephemeral loopback port; nothing in this file may
touch 127.0.0.1:4710, which is the author's real Apollo. Every test also points
``UAD_MCP_SNAPSHOT_DIR`` at a tmp_path, so no test can write into ``~/.uad-console-mcp``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.derive import cascade_fixture, twin_fixture
from tests.mock_engine import MockEngine
from uad_console_mcp import snapshot as snap
from uad_console_mcp.client import UAClient
from uad_console_mcp.discovery import Rig, discover
from uad_console_mcp.safety import SafetyGate

Tree = dict[str, Any]

MONITOR = "/devices/0/outputs/22"
MONITOR_CHANNEL = {
    "path": MONITOR,
    "is_monitor": True,
    "device": 0,
    "kind": "output",
    "index": 22,
    "io_type": "Monitor",
}
MONITOR_LEVEL = f"{MONITOR}/CRMonitorLevel"
IN0 = "/devices/0/inputs/0"
IN18_SLOT1 = "/devices/0/inputs/18/effects/1"
STEREO_INPUT = "/devices/0/inputs/16"


# ----------------------------------------------------------------------------------- helpers


@pytest.fixture(autouse=True)
def snapshot_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the snapshot directory. Autouse so no test can reach a real home directory."""
    directory = tmp_path / "snapshots"
    monkeypatch.setenv(snap.ENV_SNAPSHOT_DIR, str(directory))
    return directory


@asynccontextmanager
async def connected(engine: MockEngine) -> AsyncIterator[tuple[UAClient, Rig]]:
    """A client plus a freshly discovered rig for a started mock engine."""
    assert engine.port not in (0, MockEngine.REAL_ENGINE_PORT), "must be an ephemeral mock port"
    client = UAClient(port=engine.port)
    try:
        yield client, await discover(client)
    finally:
        await client.aclose()


def gate(mode: str = "safe", **kwargs: Any) -> SafetyGate:
    return SafetyGate(mode, **kwargs)  # type: ignore[arg-type]


def entry_for(doc: dict[str, Any], path: str) -> dict[str, Any] | None:
    return next((entry for entry in doc["entries"] if entry["path"] == path), None)


def paths_of(writes: Iterable[dict[str, Any]]) -> list[str]:
    return [write["path"] for write in writes]


def inject_parameters(engine: MockEngine, effect_path: str, names: Iterable[str]) -> None:
    """Materialize plugin parameter nodes engine-side, the way a real load eventually does."""
    listed = list(names)
    node = engine.nodes[effect_path]
    if snap.PARAMETERS_CHILD not in node["children"]:
        node["children"] = [*node["children"], snap.PARAMETERS_CHILD]
    engine.nodes[f"{effect_path}/{snap.PARAMETERS_CHILD}"] = {
        "properties": {"Type": {"type": "string", "value": "container"}},
        "children": [str(index) for index in range(len(listed))],
    }
    for index, name in enumerate(listed):
        engine.nodes[f"{effect_path}/{snap.PARAMETERS_CHILD}/{index}"] = {
            "properties": {
                "Type": {"type": "string", "value": "parameter"},
                "Name": {"type": "string", "value": name},
                "StringValue": {"type": "string", "value": "0.0"},
                "NormalizedValue": {"type": "float", "min": 0.0, "max": 1.0, "value": 0.0},
            },
            "children": [],
        }


def remove_parameters(engine: MockEngine, effect_path: str) -> None:
    """Take a slot's parameter nodes away, as the engine does when a plugin is removed."""
    node = engine.nodes[effect_path]
    node["children"] = [child for child in node["children"] if child != snap.PARAMETERS_CHILD]
    prefix = f"{effect_path}/{snap.PARAMETERS_CHILD}"
    for path in [p for p in engine.nodes if p == prefix or p.startswith(prefix + "/")]:
        del engine.nodes[path]


# ----------------------------------------------------------------------------------- capture


async def test_capture_keeps_only_allowlisted_writable_properties(mock: MockEngine) -> None:
    async with connected(mock) as (client, rig):
        doc = await snap.capture(client, rig, name="base")
    assert doc["schema_version"] == snap.SCHEMA_VERSION
    assert doc["entries"], "the x8 fixture must yield entries"
    names = {snap.property_name(entry["path"]) for entry in doc["entries"]}
    assert names <= snap.CAPTURED_PROPERTIES
    assert not names & snap.CLOCK_PROPERTIES, "clock/rate belong in the header, not the entries"


async def test_capture_puts_clock_and_rate_in_the_header(mock: MockEngine) -> None:
    async with connected(mock) as (client, rig):
        doc = await snap.capture(client, rig)
    assert doc["sample_rate"] == 48000
    assert doc["clock_source"] == "Internal"
    assert doc["engine_version"] == "11.7.1"


async def test_capture_records_devices_and_channels(mock: MockEngine) -> None:
    async with connected(mock) as (client, rig):
        doc = await snap.capture(client, rig)
    assert [device["type_id"] for device in doc["devices"]] == ["Apollo x8 Gen 2"]
    assert len(doc["channels"]) == len(rig.channels)
    monitor = next(channel for channel in doc["channels"] if channel["is_monitor"])
    assert monitor["path"] == MONITOR


async def test_capture_skips_properties_with_no_value(mock: MockEngine) -> None:
    async with connected(mock) as (client, rig):
        doc = await snap.capture(client, rig)
    # SPEC §2: 13 captured properties carry no value at all; they cannot be restored.
    assert entry_for(doc, "/devices/0/auxs/0/Gain") is None
    assert entry_for(doc, "/devices/0/inputs/27/IOType") is None
    assert entry_for(doc, "/devices/0/auxs/0/FaderLevel") is not None


async def test_capture_drops_preset_browser_trees_from_metadata(mock: MockEngine) -> None:
    async with connected(mock) as (client, rig):
        doc = await snap.capture(client, rig)
    channel_preset = entry_for(doc, f"{IN0}/Preset")
    assert channel_preset is not None
    assert "values" not in channel_preset["meta"], "a hierarchical preset tree must not be stored"
    fader = entry_for(doc, f"{IN0}/FaderLevel")
    assert fader is not None
    assert fader["meta"]["min"] == -144.0
    assert fader["meta"]["max"] == 12.0


async def test_capture_keeps_small_enum_domains(mock: MockEngine) -> None:
    async with connected(mock) as (client, rig):
        doc = await snap.capture(client, rig)
    io_type = entry_for(doc, f"{IN0}/IOType")
    assert io_type is not None
    assert io_type["meta"]["values"] == ["Line", "Mic", "Dante"]


# ------------------------------------------------------------------------------------- store


def test_snapshot_dir_honours_the_env_override(tmp_path: Path) -> None:
    override = tmp_path / "elsewhere"
    assert snap.snapshot_dir({snap.ENV_SNAPSHOT_DIR: str(override)}) == override
    assert snap.snapshot_dir({}) == snap.DEFAULT_SNAPSHOT_DIR.expanduser()
    default = snap.DEFAULT_SNAPSHOT_DIR.expanduser()
    assert snap.snapshot_dir({snap.ENV_SNAPSHOT_DIR: "  "}) == default


def test_module_level_store_follows_the_env(snapshot_dir: Path) -> None:
    assert snap.SnapshotStore().directory == snapshot_dir
    assert snap.list_snapshots() == []


def test_save_load_list_round_trip(snapshot_dir: Path) -> None:
    store = snap.SnapshotStore(snapshot_dir)
    doc = {
        "schema_version": snap.SCHEMA_VERSION,
        "created": "2026-07-29T12:00:00Z",
        "devices": [{"type_id": "Apollo x8 Gen 2"}],
        "sample_rate": 48000,
        "entries": [{"path": f"{IN0}/Mute", "value": True, "meta": {"type": "bool"}}],
    }
    path = store.save(doc, "my session 1")
    assert path.exists()
    loaded = store.load("my session 1")
    assert loaded["name"] == "my session 1"
    assert loaded["entries"] == doc["entries"]
    listed = store.list_snapshots()
    assert [item["name"] for item in listed] == ["my session 1"]
    assert listed[0]["entries"] == 1
    assert listed[0]["devices"] == ["Apollo x8 Gen 2"]
    assert store.delete("my session 1") is True
    assert store.delete("my session 1") is False


@pytest.mark.parametrize(
    "name", ["../escape", "a/b", "", " ", ".hidden", "x" * 200, "no\0null"]
)
def test_unusable_snapshot_names_are_refused(snapshot_dir: Path, name: str) -> None:
    store = snap.SnapshotStore(snapshot_dir)
    with pytest.raises(snap.SnapshotError, match="usable snapshot name"):
        store.path_for(name)


def test_missing_snapshot_lists_what_is_available(snapshot_dir: Path) -> None:
    store = snap.SnapshotStore(snapshot_dir)
    store.save({"schema_version": 1, "entries": []}, "present")
    with pytest.raises(snap.SnapshotError, match="Available: present"):
        store.load("absent")


def test_wrong_schema_version_is_refused(snapshot_dir: Path) -> None:
    store = snap.SnapshotStore(snapshot_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / "old.json").write_text(
        json.dumps({"schema_version": 99, "entries": []}), encoding="utf-8"
    )
    with pytest.raises(snap.SnapshotError, match="schema_version"):
        store.load("old")


def test_unreadable_snapshot_is_listed_with_its_error(snapshot_dir: Path) -> None:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / "broken.json").write_text("{not json", encoding="utf-8")
    listed = snap.SnapshotStore(snapshot_dir).list_snapshots()
    assert listed[0]["name"] == "broken"
    assert "unreadable" in listed[0]["error"]


# -------------------------------------------------------------------------------------- diff


async def test_diff_reports_only_what_changed(mock: MockEngine) -> None:
    async with connected(mock) as (client, rig):
        doc = await snap.capture(client, rig)
        assert await snap.diff(client, rig, doc) == []
        await client.set_value(f"{IN0}/FaderLevel", -12.0)
        await client.set_value(f"{IN0}/Name", "RENAMED")
        changes = await snap.diff(client, rig, doc)
    assert {change.path for change in changes} == {f"{IN0}/FaderLevel", f"{IN0}/Name"}
    fader = next(change for change in changes if change.property == "FaderLevel")
    assert fader.current_value == -12.0
    assert fader.snapshot_value != -12.0
    assert set(fader.as_dict()) == {"path", "property", "snapshot_value", "current_value"}


async def test_diff_ignore_globs_exclude_paths(mock: MockEngine) -> None:
    async with connected(mock) as (client, rig):
        doc = await snap.capture(client, rig)
        await client.set_value(f"{IN0}/FaderLevel", -12.0)
        await client.set_value(f"{IN0}/Name", "RENAMED")
        assert len(await snap.diff(client, rig, doc, ["*/FaderLevel"])) == 1
        assert await snap.diff(client, rig, doc, ["/devices/0/inputs/0/*"]) == []


def test_values_equal_tolerates_float_quantization() -> None:
    assert snap.values_equal(-6.0, -6.0000000001)
    assert not snap.values_equal(-6.0, -6.01)
    assert snap.values_equal("UG 1", "UG 1")
    assert not snap.values_equal(True, False)
    assert not snap.values_equal(None, 0)


# -------------------------------------------------------------------------------------- plan


def test_plan_is_ordered_by_phase() -> None:
    doc = {
        "channels": [MONITOR_CHANNEL],
        "entries": [
            {"path": f"{IN0}/FaderLevel", "value": -3.0},
            {"path": f"{IN0}/sends/0/Gain", "value": -10.0},
            {"path": f"{IN0}/effects/1/EffectName", "value": "UAD Teletronix LA-3A"},
            {"path": f"{IN0}/effects/1/Preset", "value": "Default"},
            {"path": f"{IN0}/effects/1/parameters/0/NormalizedValue", "value": 0.5},
            {"path": f"{IN0}/preamps/0/Gain", "value": 20.0},
            {"path": f"{STEREO_INPUT}/Stereo", "value": True},
            {"path": f"{MONITOR}/CRMonitorLevel", "value": -20.0},
        ],
    }
    plan = snap.plan_restore(doc, include_monitor=True, current={f"{STEREO_INPUT}/Stereo": False})
    phases = [write.phase for write in plan.writes]
    assert phases == sorted(phases), "the plan must already be in execution order"
    assert paths_of(write.as_dict() for write in plan.writes) == [
        f"{IN0}/effects/1/EffectName",
        f"{IN0}/effects/1/Preset",
        f"{IN0}/effects/1/parameters/0/NormalizedValue",
        f"{IN0}/preamps/0/Gain",
        f"{IN0}/sends/0/Gain",
        f"{IN0}/FaderLevel",
        f"{STEREO_INPUT}/Stereo",
        f"{MONITOR}/CRMonitorLevel",
    ]


def test_monitor_entries_are_skipped_unless_opted_in() -> None:
    doc = {
        "channels": [MONITOR_CHANNEL],
        "entries": [
            {"path": MONITOR_LEVEL, "value": -20.0},
            {"path": f"{MONITOR}/Mute", "value": False},
            {"path": "/devices/0/DimAttenuation", "value": 17},
            {"path": f"{IN0}/Mute", "value": False},
        ],
    }
    plan = snap.plan_restore(doc)
    assert paths_of(write.as_dict() for write in plan.writes) == [f"{IN0}/Mute"]
    assert {item["path"] for item in plan.skipped} == {
        MONITOR_LEVEL,
        f"{MONITOR}/Mute",
        "/devices/0/DimAttenuation",
    }
    assert all("include_monitor" in item["reason"] for item in plan.skipped)
    opted_in = snap.plan_restore(doc, include_monitor=True)
    assert len(opted_in.writes) == 4
    assert opted_in.skipped == []


def test_stereo_is_unlinked_only_when_it_is_actually_linked() -> None:
    entries = [
        {"path": f"{STEREO_INPUT}/Stereo", "value": True},
        {"path": f"{STEREO_INPUT}/FaderLevel", "value": -3.0},
    ]
    doc: dict[str, Any] = {"channels": [], "entries": entries}

    linked = snap.plan_restore(doc, current={f"{STEREO_INPUT}/Stereo": True})
    assert [(write.phase.name, write.value) for write in linked.writes] == [
        ("UNLINK_STEREO", False),
        ("CHANNEL", -3.0),
        ("RELINK_STEREO", True),
    ]

    unlinked = snap.plan_restore(doc, current={f"{STEREO_INPUT}/Stereo": False})
    assert [(write.phase.name, write.value) for write in unlinked.writes] == [
        ("CHANNEL", -3.0),
        ("RELINK_STEREO", True),
    ]


def test_a_mono_channel_with_nothing_to_change_gets_no_stereo_writes() -> None:
    doc: dict[str, Any] = {"channels": [], "entries": []}
    assert snap.plan_restore(doc, current={f"{IN0}/Stereo": False}).writes == []


def test_include_clock_puts_the_transport_first() -> None:
    doc = {
        "channels": [],
        "sample_rate": 96000,
        "clock_source": "ADAT",
        "entries": [{"path": f"{IN0}/Mute", "value": True}],
    }
    plan = snap.plan_restore(doc, include_clock=True)
    assert paths_of(write.as_dict() for write in plan.writes) == [
        "/SampleRate",
        "/ClockSource",
        f"{IN0}/Mute",
    ]
    assert snap.plan_restore(doc).writes[0].path == f"{IN0}/Mute"


# ----------------------------------------------------------------------------------- restore


async def test_round_trip_on_the_cascade_fixture(
    mock_factory: Any, base_fixture: Tree, snapshot_dir: Path
) -> None:
    engine = await mock_factory(cascade_fixture(base_fixture))
    store = snap.SnapshotStore(snapshot_dir)
    async with connected(engine) as (client, rig):
        assert len(rig.devices) == 2
        doc = await snap.capture(client, rig, name="cascade")
        store.save(doc, "cascade")

        # Scramble state on BOTH units, across every restore phase.
        await client.set_value("/devices/0/inputs/0/FaderLevel", -30.0)
        await client.set_value("/devices/0/inputs/0/Mute", True)
        await client.set_value("/devices/1/inputs/3/Name", "SCRAMBLED")
        await client.set_value("/devices/1/inputs/3/Pan", 0.75)
        await client.set_value("/devices/0/inputs/0/sends/0/Gain", 6.0)
        await client.set_value(f"{STEREO_INPUT}/Stereo", False)
        await client.set_value(f"{IN18_SLOT1}/parameters/1/NormalizedValue", 0.9)
        assert len(await snap.diff(client, rig, doc)) == 7

        result = await snap.restore(
            client, rig, store.load("cascade"), gate=gate(), dry_run=False, store=store
        )
        assert result["counts"]["failed"] == 0
        assert result["counts"]["applied"] >= 7
        assert result["dry_run"] is False
        assert result["cross_model"] is False
        assert result["pre_restore_snapshot"] is not None
        assert await snap.diff(client, rig, doc) == [], "the rig must be exactly as captured"


async def test_restore_dry_run_writes_nothing(mock: MockEngine, snapshot_dir: Path) -> None:
    store = snap.SnapshotStore(snapshot_dir)
    async with connected(mock) as (client, rig):
        doc = await snap.capture(client, rig, name="dry")
        await client.set_value(f"{IN0}/FaderLevel", -30.0)
        await client.set_value(f"{IN0}/Name", "SCRAMBLED")
        await client.set_value(f"{STEREO_INPUT}/Stereo", False)
        before = mock.set_count()

        result = await snap.restore(client, rig, doc, gate=gate(), dry_run=True, store=store)

    assert mock.set_count() == before, "a dry run must not put a single set on the wire"
    assert result["dry_run"] is True
    assert result["applied"] == []
    assert result["failed"] == []
    assert result["pre_restore_snapshot"] is None
    assert store.list_snapshots() == [], "a dry run must not save anything either"
    phases = [write["phase"] for write in result["writes"]]
    assert set(phases) == {"channel", "relink_stereo"}
    assert phases == ["channel", "channel", "relink_stereo"]
    assert set(result["writes"][0]) == {"path", "property", "value", "phase", "note"}
    assert result["counts"]["planned"] == 3


async def test_restore_captures_a_pre_restore_snapshot_first(
    mock: MockEngine, snapshot_dir: Path
) -> None:
    store = snap.SnapshotStore(snapshot_dir)
    async with connected(mock) as (client, rig):
        doc = await snap.capture(client, rig, name="before")
        await client.set_value(f"{IN0}/Name", "SCRAMBLED")
        result = await snap.restore(
            client, rig, doc, gate=gate(), dry_run=False, store=store
        )
    name = result["pre_restore_snapshot"]
    assert name is not None and name.startswith("pre-restore-")
    saved = store.load(name)
    scrambled = entry_for(saved, f"{IN0}/Name")
    assert scrambled is not None
    assert scrambled["value"] == "SCRAMBLED", "the pre-restore snapshot must hold the OLD state"


async def test_restore_can_be_run_without_a_pre_restore_snapshot(
    mock: MockEngine, snapshot_dir: Path
) -> None:
    store = snap.SnapshotStore(snapshot_dir)
    async with connected(mock) as (client, rig):
        doc = await snap.capture(client, rig)
        await client.set_value(f"{IN0}/Name", "SCRAMBLED")
        result = await snap.restore(
            client, rig, doc, gate=gate(), dry_run=False, store=store, pre_restore=False
        )
    assert result["pre_restore_snapshot"] is None
    assert store.list_snapshots() == []


async def test_readonly_gate_fails_every_entry_and_writes_nothing(mock: MockEngine) -> None:
    async with connected(mock) as (client, rig):
        doc = await snap.capture(client, rig)
        await mock.push(f"{IN0}/Name", "SCRAMBLED")
        await mock.push(f"{IN0}/FaderLevel", -30.0)
        assert mock.set_count() == 0
        result = await snap.restore(
            client, rig, doc, gate=gate("readonly"), dry_run=False, pre_restore=False
        )
    assert mock.set_count() == 0
    assert result["counts"]["applied"] == 0
    assert result["counts"]["failed"] == 2
    assert all("readonly" in item["error"] for item in result["failed"])
    assert all("UAD_MCP_MODE" in item["error"] for item in result["failed"])


async def test_restore_runs_the_monitor_level_through_the_rail(mock: MockEngine) -> None:
    async with connected(mock) as (client, rig):
        doc = await snap.capture(client, rig)
        # The engine ends up far below the snapshot: restoring it is a big level increase.
        await mock.push(MONITOR_LEVEL, -60.0)
        result = await snap.restore(
            client,
            rig,
            doc,
            gate=gate(),
            dry_run=False,
            include_monitor=True,
            pre_restore=False,
        )
    applied = [item for item in result["applied"] if item["property"] == "CRMonitorLevel"]
    assert applied, "the monitor level should have been restored"
    assert applied[0]["after"] == -54.0, "clamped to +6 dB per call, not straight to -22 dB"
    assert mock.value(MONITOR_LEVEL) == -54.0


async def test_restore_refuses_the_transport_without_an_audio_check(mock: MockEngine) -> None:
    async with connected(mock) as (client, rig):
        doc = await snap.capture(client, rig)
        doc["clock_source"] = "ADAT"
        result = await snap.restore(
            client,
            rig,
            doc,
            gate=gate(),
            dry_run=False,
            include_clock=True,
            audio_flowing=None,
            pre_restore=False,
        )
    failed = [item for item in result["failed"] if item["property"] == "ClockSource"]
    assert failed, result
    assert "whether audio is flowing" in failed[0]["error"]
    assert mock.value("/ClockSource") == "Internal"


async def test_restore_applies_the_transport_when_the_room_is_silent(mock: MockEngine) -> None:
    async with connected(mock) as (client, rig):
        doc = await snap.capture(client, rig)
        doc["clock_source"] = "ADAT"
        result = await snap.restore(
            client,
            rig,
            doc,
            gate=gate(),
            dry_run=False,
            include_clock=True,
            audio_flowing=False,
            pre_restore=False,
        )
    assert result["counts"]["failed"] == 0
    assert mock.value("/ClockSource") == "ADAT"


async def test_restore_skips_properties_this_rig_does_not_have(
    mock_factory: Any, base_fixture: Tree
) -> None:
    engine = await mock_factory(base_fixture)
    async with connected(engine) as (client, rig):
        doc = await snap.capture(client, rig)
        doc["entries"].append(
            {"path": "/devices/0/inputs/99/Mute", "value": True, "meta": {"type": "bool"}}
        )
        result = await snap.restore(
            client, rig, doc, gate=gate(), dry_run=False, pre_restore=False
        )
    missing = [item for item in result["skipped"] if "inputs/99" in item["path"]]
    assert missing and "no such property" in missing[0]["reason"]
    assert result["counts"]["failed"] == 0


# ------------------------------------------------------------------------------- cross model


async def test_cross_model_restore_is_refused(
    mock_factory: Any, base_fixture: Tree
) -> None:
    x8 = await mock_factory(base_fixture)
    twin = await mock_factory(twin_fixture(base_fixture))
    async with connected(x8) as (client, rig):
        doc = await snap.capture(client, rig, name="x8")
    async with connected(twin) as (twin_client, twin_rig):
        with pytest.raises(snap.CrossModelError) as caught:
            await snap.restore(twin_client, twin_rig, doc, gate=gate(), dry_run=True)
    assert "Apollo x8 Gen 2" in str(caught.value)
    assert "Apollo Twin X" in str(caught.value)
    assert "allow_cross_model=True" in str(caught.value)
    assert twin.set_count() == 0


async def test_cross_model_mapping_uses_io_type_and_ordinal_not_index(
    mock_factory: Any, base_fixture: Tree
) -> None:
    x8 = await mock_factory(base_fixture)
    twin = await mock_factory(twin_fixture(base_fixture))
    async with connected(x8) as (client, rig):
        doc = await snap.capture(client, rig)
    async with connected(twin) as (_, twin_rig):
        mapping = snap.build_channel_map(doc["channels"], twin_rig)
    # The twin keeps 2 mic + 2 line + 2 virtual inputs, RE-INDEXED contiguously: the x8's Line
    # inputs 4 and 5 must land on twin inputs 2 and 3, never on the same raw index.
    assert mapping["/devices/0/inputs/0"] == "/devices/0/inputs/0"
    assert mapping["/devices/0/inputs/4"] == "/devices/0/inputs/2"
    assert mapping["/devices/0/inputs/5"] == "/devices/0/inputs/3"
    assert mapping["/devices/0/inputs/18"] == "/devices/0/inputs/4"
    assert mapping[MONITOR] == "/devices/0/outputs/0"
    # Channels with no counterpart (a 3rd mic input, ADAT, talkback) are simply not mapped.
    assert "/devices/0/inputs/2" not in mapping
    assert "/devices/0/inputs/8" not in mapping


async def test_cross_model_dry_run_writes_to_the_mapped_channel(
    mock_factory: Any, base_fixture: Tree
) -> None:
    x8 = await mock_factory(base_fixture)
    twin = await mock_factory(twin_fixture(base_fixture))
    async with connected(x8) as (client, rig):
        doc = await snap.capture(client, rig)
    async with connected(twin) as (twin_client, twin_rig):
        await twin_client.set_value("/devices/0/inputs/2/Name", "SOMETHING ELSE")
        result = await snap.restore(
            twin_client, twin_rig, doc, gate=gate(), dry_run=True, allow_cross_model=True
        )
    assert result["cross_model"] is True
    rename = [write for write in result["writes"] if write["property"] == "Name"]
    assert rename == [
        {
            "path": "/devices/0/inputs/2/Name",
            "property": "Name",
            "value": "ANALOG 5",
            "phase": "channel",
            "note": "",
        }
    ]
    unmapped = [item for item in result["skipped"] if "ordinal" in item.get("reason", "")]
    assert unmapped, "channels with no counterpart must be reported, not written blindly"


async def test_cross_model_restore_applies_to_the_mapped_channel(
    mock_factory: Any, base_fixture: Tree
) -> None:
    x8 = await mock_factory(base_fixture)
    twin = await mock_factory(twin_fixture(base_fixture))
    async with connected(x8) as (client, rig):
        doc = await snap.capture(client, rig)
    async with connected(twin) as (twin_client, twin_rig):
        await twin_client.set_value("/devices/0/inputs/2/Name", "SOMETHING ELSE")
        result = await snap.restore(
            twin_client,
            twin_rig,
            doc,
            gate=gate(),
            dry_run=False,
            allow_cross_model=True,
            pre_restore=False,
        )
    assert result["counts"]["failed"] == 0
    assert twin.value("/devices/0/inputs/2/Name") == "ANALOG 5"


# -------------------------------------------------------------------------- materialization


async def test_wait_for_parameters_returns_children_that_already_exist(mock: MockEngine) -> None:
    async with connected(mock) as (client, _):
        children = await snap.wait_for_parameters(client, IN18_SLOT1, timeout_s=0.2)
    assert len(children) == 7


async def test_wait_for_parameters_times_out_gracefully(mock: MockEngine) -> None:
    async with connected(mock) as (client, _):
        # The mock mirrors the engine: writing EffectName does NOT create parameter nodes.
        await client.set_value(f"{IN0}/effects/1/EffectName", "UAD Teletronix LA-3A")
        children = await snap.wait_for_parameters(
            client, f"{IN0}/effects/1", timeout_s=0.25, poll_s=0.05
        )
    assert children == [], "a slot that never materializes must return empty, not raise"


async def test_wait_for_parameters_notices_late_materialization(mock: MockEngine) -> None:
    slot = f"{IN0}/effects/2"

    async def materialize_later() -> None:
        await asyncio.sleep(0.1)
        inject_parameters(mock, slot, ["Threshold", "Ratio", "Output"])

    async with connected(mock) as (client, _):
        await client.set_value(f"{slot}/EffectName", "UAD Teletronix LA-3A")
        task = asyncio.create_task(materialize_later())
        children = await snap.wait_for_parameters(client, slot, timeout_s=3.0, poll_s=0.05)
        await task
    assert children == ["0", "1", "2"]


async def test_restore_reloads_a_plugin_and_reports_the_parameter_timeout(
    mock: MockEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writing EffectName does not create parameter nodes, so the poll must time out cleanly."""
    monkeypatch.setattr(snap, "MATERIALIZE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(snap, "MATERIALIZE_POLL_S", 0.05)
    async with connected(mock) as (client, rig):
        doc = await snap.capture(client, rig)
        # Engine-side: the plugin is gone and so are its parameter nodes.
        await mock.push(f"{IN18_SLOT1}/EffectName", "")
        remove_parameters(mock, IN18_SLOT1)
        result = await snap.restore(
            client, rig, doc, gate=gate(), dry_run=False, pre_restore=False
        )
    reloaded = [item for item in result["applied"] if item["property"] == "EffectName"]
    assert reloaded and reloaded[0]["after"] == "UAD Teletronix LA-3A"
    assert "did not appear" in reloaded[0]["warnings"][0]
    assert result["counts"]["failed"] == 0, "a slot that will not materialize is not a failure"
    gone = [
        item
        for item in result["skipped"]
        if item["path"].startswith(f"{IN18_SLOT1}/{snap.PARAMETERS_CHILD}/")
    ]
    # Two captured properties per parameter (Name and NormalizedValue), seven parameters.
    assert len(gone) == 14
    assert all("no such property" in item["reason"] for item in gone)


async def test_restore_skips_parameter_entries_when_a_slot_dies_mid_restore(
    mock: MockEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dead-slot guard: parameters read fine, then the slot never comes back up."""
    monkeypatch.setattr(snap, "MATERIALIZE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(snap, "MATERIALIZE_POLL_S", 0.05)
    read_current = snap._read_current

    async def read_then_tear_down(client: UAClient, paths: Any) -> dict[str, Any]:
        # Simulate the engine tearing the slot down between our read and our write: the
        # parameter entries are already planned, but their nodes are gone by write time.
        values = await read_current(client, paths)
        remove_parameters(mock, IN18_SLOT1)
        return values

    monkeypatch.setattr(snap, "_read_current", read_then_tear_down)
    async with connected(mock) as (client, rig):
        doc = await snap.capture(client, rig)
        await mock.push(f"{IN18_SLOT1}/EffectName", "")
        for index in range(7):
            await mock.push(f"{IN18_SLOT1}/{snap.PARAMETERS_CHILD}/{index}/NormalizedValue", 0.99)
        result = await snap.restore(
            client, rig, doc, gate=gate(), dry_run=False, pre_restore=False
        )
    dead = [
        item
        for item in result["skipped"]
        if item["path"].startswith(f"{IN18_SLOT1}/{snap.PARAMETERS_CHILD}/")
    ]
    assert dead, "parameter entries for the dead slot must be skipped, not blindly written"
    assert all("never materialized" in item["reason"] for item in dead)
    assert result["counts"]["failed"] == 0


async def test_restore_writes_parameters_when_the_slot_is_alive(mock: MockEngine) -> None:
    async with connected(mock) as (client, rig):
        doc = await snap.capture(client, rig)
        await mock.push(f"{IN18_SLOT1}/parameters/0/NormalizedValue", 0.99)
        result = await snap.restore(
            client, rig, doc, gate=gate(), dry_run=False, pre_restore=False
        )
    assert result["counts"]["failed"] == 0
    applied = [item for item in result["applied"] if "parameters/0" in item["path"]]
    assert applied and applied[0]["verified"] is True
    assert mock.value(f"{IN18_SLOT1}/parameters/0/NormalizedValue") != 0.99


# ------------------------------------------------------------------------------- throughput


async def test_a_long_restore_paces_itself_past_the_rate_limit(mock: MockEngine) -> None:
    """The gate allows 10 writes/s with a burst of 20; a 30-write restore must not mass-fail."""
    async with connected(mock) as (client, rig):
        doc = await snap.capture(client, rig)
        inputs = [f"/devices/0/inputs/{index}" for index in range(24)]
        for path in inputs:
            await mock.push(f"{path}/Name", "SCRAMBLED")
            await mock.push(f"{path}/Pan", 0.5)
        result = await snap.restore(
            client,
            rig,
            doc,
            gate=gate(writes_per_second=100.0, write_burst=20),
            dry_run=False,
            pre_restore=False,
        )
    planned = result["counts"]["planned"]
    assert planned > 20, "the point of this test is to exceed the 20-write burst"
    assert result["counts"]["applied"] == planned
    assert result["counts"]["failed"] == 0
