"""Workflow tools: routing map, bus mixes, the rig profile and modes (docs/SPEC.md §4.8, §4.9).

Every engine is a ``MockEngine`` on an ephemeral port. The snapshot directory and the rig
profile are redirected to tmp paths, so nothing touches ``~/.uad-console-mcp``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.derive import twin_fixture
from tests.mock_engine import MockEngine
from uad_console_mcp import modes
from uad_console_mcp import server as srv
from uad_console_mcp import snapshot as snap
from uad_console_mcp.client import UAClient
from uad_console_mcp.profile import ProfileStore
from uad_console_mcp.safety import SafetyGate

IN0 = "/devices/0/inputs/0"  # ANALOG 1, preamp
IN1 = "/devices/0/inputs/1"  # ANALOG 2, preamp
IN2 = "/devices/0/inputs/2"  # ANALOG 3, preamp
VIRTUAL1 = "/devices/0/inputs/18"
VIRTUAL3 = "/devices/0/inputs/20"
AUX2 = "/devices/0/auxs/1"
MONITOR = "/devices/0/outputs/22"


@pytest.fixture(autouse=True)
def snapshot_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "snapshots"
    monkeypatch.setenv(snap.ENV_SNAPSHOT_DIR, str(directory))
    return directory


@asynccontextmanager
async def context(engine: MockEngine, mode: str = "safe") -> AsyncIterator[srv.ServerContext]:
    assert engine.port not in (0, MockEngine.REAL_ENGINE_PORT)
    client = UAClient(port=engine.port)
    ctx = srv.ServerContext(
        client,
        SafetyGate(mode),
        snapshots=snap.SnapshotStore(snap.snapshot_dir()),  # type: ignore[arg-type]
    )
    try:
        yield ctx
    finally:
        await ctx.aclose()
        await client.aclose()


def exists(path: Path) -> bool:
    return path.exists()


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def wire_sets(engine: MockEngine) -> list[str]:
    return [frame for frame in engine.sent_frames if frame.startswith("set ")]


async def save_profile(ctx: srv.ServerContext, sources: list[dict[str, Any]], **kw: Any) -> None:
    result = await srv.tool_rig_profile(ctx, action="save", sources=sources, **kw)
    assert result["ok"] is True, result


VOICE = {"target": "ANALOG 1", "plugged_in": "Shure SM7B", "mic_type": "dynamic", "role": "voice"}


# --------------------------------------------------------------------------------- routing


async def test_routing_finds_loopback_inputs_and_virtual_outputs(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_get_routing(ctx)
    assert result["ok"] is True
    assert result["loopback"]["monitor_mix"] == {
        "channels": [27, 28],
        "names": ["MON L", "MON R"],
        "active": True,
    }
    assert result["loopback"]["aux1"]["channels"] == [29, 30]
    assert result["loopback"]["aux2"]["channels"] == [31, 32]
    assert result["inputs_1_2"] == ["MIC/LINE/HIZ 1", "MIC/LINE/HIZ 2"]
    virtual = {entry["name"]: entry for entry in result["virtual_outputs"]}
    assert virtual["VIRTUAL 1"]["channel"] == 21 and virtual["VIRTUAL 1"]["active"] is True
    assert virtual["VIRTUAL 3"]["active"] is False
    assert "Livestreaming" in result["io_matrix_presets"]
    assert any("inputs 1-2" in note for note in result["notes"])
    assert any("VIRTUAL 3" in note for note in result["notes"])
    assert wire_sets(mock) == []


async def test_routing_note_goes_away_when_mon_is_on_inputs_1_2(mock: MockEngine) -> None:
    for field, value in (("Name", "MON L"), ("IOType", "CtrlRoom"), ("ChannelIndex", 1)):
        await mock.push(f"/inputroutes/0/{field}", value)
    for field, value in (("Name", "MON R"), ("IOType", "CtrlRoom"), ("ChannelIndex", 2)):
        await mock.push(f"/inputroutes/1/{field}", value)
    async with context(mock) as ctx:
        result = await srv.tool_get_routing(ctx)
    assert result["loopback"]["monitor_mix"]["channels"] == [1, 2]
    assert not any("inputs 1-2" in note for note in result["notes"])


# ------------------------------------------------------------------------------------ mixes


async def test_get_mix_cue_lists_sources_and_headphones(mock: MockEngine) -> None:
    await mock.push(f"{IN0}/sends/2/Gain", -8.0)  # ANALOG 1 -> cue1
    async with context(mock) as ctx:
        result = await srv.tool_get_mix(ctx, "cue1")
    names = {entry["name"]: entry for entry in result["sources"]}
    assert names["ANALOG 1"]["level_db"] == -8.0
    assert "ANALOG 2" in result["off"]
    assert result["heard_on"] == ["HP 1"]
    assert wire_sets(mock) == []


async def test_get_mix_aux_reports_return_and_capture(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_get_mix(ctx, "aux2")
    assert result["return"] == {"name": "AUX 2", "fader_db": 0.0, "mute": False}
    assert result["recorded_on"]["channels"] == [31, 32]
    assert result["sources"] == []


async def test_get_mix_warns_about_a_cue_the_device_does_not_have(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_get_mix(ctx, "cue3")
        bad = await srv.tool_get_mix(ctx, "monitor")
    assert "reaches no real output" in result["warnings"][0]
    assert bad["ok"] is False and "uad_list_channels" in bad["error"]


# ------------------------------------------------------------------------------ rig profile


async def test_profile_starts_empty_and_status_says_so(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        profile = await srv.tool_rig_profile(ctx)
        status = await srv.tool_get_status(ctx)
    assert profile["saved"] is False and profile["sources"] == []
    assert "Ask the user" in profile["hint"]
    assert status["rig_profile"]["saved"] is False


async def test_profile_round_trip_by_index_survives_a_rename(
    mock: MockEngine, rig_profile_path: Path
) -> None:
    async with context(mock) as ctx:
        await save_profile(
            ctx, [VOICE, {"target": "VIRTUAL 1", "role": "system_audio"}], notes="OBS"
        )
        await srv.tool_set_channels(ctx, [{"target": "ANALOG 1", "name": "HOST"}])
        result = await srv.tool_rig_profile(ctx)
        notes_only = await srv.tool_rig_profile(ctx, action="save", notes="OBS to Twitch")
        status = await srv.tool_get_status(ctx)
    stored = json.loads(read(rig_profile_path))
    assert stored["sources"][0]["index"] == 0 and stored["sources"][0]["name"] == "ANALOG 1"
    assert result["sources"][0]["current_name"] == "HOST"
    assert notes_only["notes"] == "OBS to Twitch" and len(notes_only["sources"]) == 2
    assert status["rig_profile"]["sources"] == 2


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ({"target": "ANALOG 1", "volume": 3}, "unknown key"),
        ({"target": "ANALOG 1", "mic_type": "tube"}, "not one of"),
        ({"target": "ANALOG 1", "role": "lead"}, "not one of"),
        ({"target": "UG 1", "mic_type": "condenser"}, "has no preamp"),
        ({"plugged_in": "SM7B"}, "needs a 'target'"),
    ],
)
async def test_profile_rejects_bad_sources_and_saves_nothing(
    mock: MockEngine, rig_profile_path: Path, source: dict[str, Any], message: str
) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_rig_profile(ctx, action="save", sources=[source])
    assert result["ok"] is False and message in result["error"]
    assert not exists(rig_profile_path)


async def test_profile_rejects_the_same_input_twice(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_rig_profile(
            ctx, action="save", sources=[{"target": 0}, {"target": "ANALOG 1"}]
        )
    assert result["ok"] is False and "listed twice" in result["error"]


# ------------------------------------------------------------------------------ ribbon guard


async def test_48v_is_refused_on_a_profiled_ribbon_even_when_confirmed(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        await save_profile(
            ctx, [{"target": "ANALOG 2", "plugged_in": "R-121", "mic_type": "ribbon"}]
        )
        before = mock.set_count()
        refused = await srv.tool_set_preamp(
            ctx, "ANALOG 2", phantom_48v=True, confirm_condenser_mic=True
        )
        off = await srv.tool_set_preamp(ctx, "ANALOG 2", phantom_48v=False)
    assert refused["ok"] is False and "ribbon mic (R-121)" in refused["error"]
    assert mock.value(f"{IN1}/preamps/0/48V") is False
    assert off["ok"] is True
    assert mock.set_count() == before + 1  # only the switch-off reached the wire


async def test_48v_still_works_on_a_profiled_condenser(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        await save_profile(ctx, [{"target": "ANALOG 1", "mic_type": "condenser"}])
        result = await srv.tool_set_preamp(
            ctx, "ANALOG 1", phantom_48v=True, confirm_condenser_mic=True
        )
    assert result["ok"] is True and mock.value(f"{IN0}/preamps/0/48V") is True


async def test_48v_fails_closed_on_an_unreadable_profile(
    mock: MockEngine, rig_profile_path: Path
) -> None:
    write(rig_profile_path, "{not json")
    async with context(mock) as ctx:
        result = await srv.tool_set_preamp(
            ctx, "ANALOG 1", phantom_48v=True, confirm_condenser_mic=True
        )
    assert result["ok"] is False and "could not be read" in result["error"]
    assert mock.value(f"{IN0}/preamps/0/48V") is False


# ------------------------------------------------------------------------------------ modes


async def test_mode_list_describes_every_mode(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_mode(ctx, action="list")
    assert [item["mode"] for item in result["modes"]] == list(modes.MODES)
    assert result["never"]


async def test_livestream_asks_for_the_mic_instead_of_guessing(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        plan = await srv.tool_mode(ctx, mode="livestream")
        applied = await srv.tool_mode(ctx, action="apply", mode="livestream")
    assert plan["questions"] and "ANALOG 1" in plan["questions"][0]
    assert applied["ok"] is False and "Cannot apply livestream yet" in applied["error"]
    assert wire_sets(mock) == []


async def test_livestream_opens_what_is_off_and_leaves_balance_alone(
    mock: MockEngine, snapshot_dir: Path
) -> None:
    await mock.push(f"{IN0}/Mute", True)
    await mock.push(f"{IN0}/FaderLevel", -144.0)
    await mock.push(f"{VIRTUAL1}/FaderLevel", -3.0)  # up already: must not move
    await mock.push(f"{IN2}/Solo", True)
    monitor_before = (mock.value(f"{MONITOR}/CRMonitorLevel"), mock.value(f"{MONITOR}/Mute"))
    async with context(mock) as ctx:
        plan = await srv.tool_mode(ctx, mode="livestream", inputs=["ANALOG 1"])
        assert wire_sets(mock) == []
        applied = await srv.tool_mode(ctx, action="apply", mode="livestream", inputs=["ANALOG 1"])
        again = await srv.tool_mode(ctx, mode="livestream", inputs=["ANALOG 1"])

    by_target = {item["target"]: item for item in plan["channels"]}
    assert by_target[0] == {"target": 0, "device": 0, "mute": False, "fader_db": 0.0}
    assert by_target[2] == {"target": 2, "device": 0, "solo": False}
    assert 18 not in by_target
    assert any("VIRTUAL 1" in note for note in plan["notes"])
    assert any("channel 21" in step for step in plan["you_do"])
    assert any("27-28" in step for step in plan["you_do"])

    assert applied["ok"] is True
    assert mock.value(f"{IN0}/Mute") is False and mock.value(f"{IN0}/FaderLevel") == 0.0
    assert mock.value(f"{IN2}/Solo") is False
    assert mock.value(f"{VIRTUAL1}/FaderLevel") == -3.0
    assert (snapshot_dir / f"{applied['snapshot']}.json").exists()
    assert again["writes"] == 0
    assert (
        mock.value(f"{MONITOR}/CRMonitorLevel"),
        mock.value(f"{MONITOR}/Mute"),
    ) == monitor_before
    assert not any("/outputs/" in frame for frame in wire_sets(mock))


async def test_a_mode_is_undone_by_its_snapshot(mock: MockEngine) -> None:
    await mock.push(f"{IN0}/Mute", True)
    async with context(mock) as ctx:
        applied = await srv.tool_mode(ctx, action="apply", mode="livestream", inputs=[0])
        assert mock.value(f"{IN0}/Mute") is False
        restored = await srv.tool_snapshot(
            ctx, action="restore", name=applied["snapshot"], dry_run=False
        )
    assert restored["ok"] is True
    assert mock.value(f"{IN0}/Mute") is True


async def test_livestream_uses_profile_roles_and_builds_a_mix_minus(mock: MockEngine) -> None:
    await mock.push(f"{VIRTUAL3}/sends/1/Gain", -3.0)  # guest already in aux2: must be cut
    await mock.push(f"{IN2}/sends/1/Gain", -12.0)  # an unrelated strip in aux2: warned about
    async with context(mock) as ctx:
        await save_profile(
            ctx,
            [
                VOICE,
                {"target": "VIRTUAL 1", "role": "system_audio"},
                {"target": "VIRTUAL 3", "role": "call_guest", "plugged_in": "Zoom"},
            ],
        )
        plan = await srv.tool_mode(ctx, mode="livestream")
        applied = await srv.tool_mode(ctx, action="apply", mode="livestream")

    sends = {(item["target"], item["send"]): item["level_db"] for item in plan["sends"]}
    assert sends == {(0, "aux2"): 0.0, (18, "aux2"): -10.0, (20, "aux2"): -144.0}
    assert {"target": 1, "device": 0, "kind": "aux", "mute": True} in plan["channels"]
    assert any("ANALOG 3" in warning for warning in plan["warnings"])
    assert any("disabled in Console's I/O Matrix" in warning for warning in plan["warnings"])
    assert any("31-32" in step for step in plan["you_do"])
    assert applied["ok"] is True
    assert mock.value(f"{VIRTUAL3}/sends/1/Gain") == -144.0
    assert mock.value(f"{IN0}/sends/1/Gain") == 0.0
    assert mock.value(f"{AUX2}/Mute") is True


async def test_livestream_refuses_a_guest_that_is_the_system_audio(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        plan = await srv.tool_mode(
            ctx, mode="livestream", inputs=[0], system_audio="VIRTUAL 1", guest="VIRTUAL 1"
        )
    assert any("hear themselves" in question for question in plan["questions"])


async def test_livestream_warns_about_48v_from_the_profile(mock: MockEngine) -> None:
    await mock.push(f"{IN1}/preamps/0/48V", True)
    async with context(mock) as ctx:
        await save_profile(
            ctx,
            [
                {"target": "ANALOG 1", "mic_type": "condenser", "role": "voice"},
                {"target": "ANALOG 2", "mic_type": "ribbon", "role": "voice"},
            ],
        )
        plan = await srv.tool_mode(ctx, mode="livestream")
    assert "Turn it off now" in plan["warnings"][0] and "ANALOG 2" in plan["warnings"][0]
    assert any("confirm_condenser_mic=true" in warning for warning in plan["warnings"])
    assert not any("48V" in json.dumps(item) for item in plan["channels"] + plan["sends"])


async def test_tracking_puts_the_input_in_the_cue(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        plan = await srv.tool_mode(ctx, action="apply", mode="tracking", inputs=["ANALOG 1"])
        bad_bus = await srv.tool_mode(ctx, mode="tracking", inputs=[0], bus="cue3")
        bad_arg = await srv.tool_mode(ctx, mode="tracking", inputs=[0], guest="VIRTUAL 3")
    assert plan["ok"] is True and plan["sends"] == [
        {"target": 0, "device": 0, "send": "cue1", "level_db": -6.0}
    ]
    assert mock.value(f"{IN0}/sends/2/Gain") == -6.0
    assert "HP 1" in plan["summary"]
    assert bad_bus["ok"] is False and "2 cue bus" in bad_bus["error"]
    assert bad_arg["ok"] is False


async def test_mixing_mutes_hardware_inputs_only(mock: MockEngine) -> None:
    await mock.push(f"{IN2}/Solo", True)
    async with context(mock) as ctx:
        applied = await srv.tool_mode(ctx, action="apply", mode="mixing")
        bad = await srv.tool_mode(ctx, mode="mixing", inputs=[0])
    assert applied["ok"] is True
    assert mock.value(f"{IN0}/Mute") is True
    assert mock.value("/devices/0/inputs/8/Mute") is True  # UG 1, ADAT
    assert mock.value(f"{VIRTUAL1}/Mute") is False
    assert mock.value("/devices/0/inputs/26/Mute") is not True  # talkback mic left alone
    assert mock.value(f"{IN2}/Solo") is False
    assert bad["ok"] is False and "takes no inputs" in bad["error"]


async def test_apply_in_readonly_mode_writes_nothing(mock: MockEngine, snapshot_dir: Path) -> None:
    async with context(mock, "readonly") as ctx:
        result = await srv.tool_mode(ctx, action="apply", mode="mixing")
    assert result["ok"] is False and "UAD_MCP_MODE=safe" in result["error"]
    assert wire_sets(mock) == []
    assert not exists(snapshot_dir)


async def test_apply_with_nothing_to_do_takes_no_snapshot(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        await srv.tool_mode(ctx, action="apply", mode="mixing")
        before = mock.set_count()
        again = await srv.tool_mode(ctx, action="apply", mode="mixing")
    assert again["snapshot"] is None and again["writes"] == 0
    assert mock.set_count() == before


async def test_modes_work_on_a_twin(mock_factory: Any, base_fixture: dict[str, Any]) -> None:
    engine = await mock_factory(twin_fixture(base_fixture))
    async with context(engine) as ctx:
        rig = await ctx.rig()
        mic = next(c for c in rig.channels if c.kind == "input" and c.has_preamp)
        plan = await srv.tool_mode(ctx, mode="livestream", inputs=[mic.name])
        mixing = await srv.tool_mode(ctx, mode="mixing")
    assert plan["ok"] is True and plan["questions"] == []
    assert mixing["ok"] is True


def test_profile_store_tolerates_a_missing_file(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "nope.json")
    assert store.exists is False and store.load().sources == []
