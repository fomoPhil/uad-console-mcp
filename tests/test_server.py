"""MCP server, tool and CLI tests (docs/SPEC.md §4.5, §4.7).

Every engine here is a ``MockEngine`` on an ephemeral loopback port; nothing in this file may
touch 127.0.0.1:4710, which is the author's real Apollo. ``UAD_MCP_SNAPSHOT_DIR`` is redirected
to a tmp_path for every test, so nothing can write into ``~/.uad-console-mcp``.

The tools are exercised as plain ``async def tool_x(ctx, ...)`` functions - the MCP registration
layer is verified separately, once, in the registration section at the bottom.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.derive import cascade_fixture, twin_fixture
from tests.mock_engine import MockEngine
from uad_console_mcp import __main__ as cli
from uad_console_mcp import server as srv
from uad_console_mcp import snapshot as snap
from uad_console_mcp.client import UAClient, UAConnectError, UATimeoutError
from uad_console_mcp.safety import SafetyGate

Tree = dict[str, Any]

MONITOR = "/devices/0/outputs/22"
MONITOR_LEVEL = f"{MONITOR}/CRMonitorLevel"
MONITOR_MUTE = f"{MONITOR}/Mute"
MONITOR_DIM = f"{MONITOR}/DimOn"
DIM_ATTENUATION = "/devices/0/DimAttenuation"
IN0 = "/devices/0/inputs/0"
IN0_METER = f"{IN0}/meters/0/MeterLevel"
IN18_SLOT1 = "/devices/0/inputs/18/effects/1"
LA3A = "UAD Teletronix LA-3A"

X8_INPUTS = 28
X8_CHANNELS = 56
TWIN_CHANNELS = 9

WRITE_KEYS = {"path", "property", "verified", "before", "after", "warnings"}
IDENTITY_KEYS = {"device", "index", "name", "kind", "path"}


# ----------------------------------------------------------------------------------- helpers


@pytest.fixture(autouse=True)
def snapshot_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the snapshot directory. Autouse so no test can reach a real home directory."""
    directory = tmp_path / "snapshots"
    monkeypatch.setenv(snap.ENV_SNAPSHOT_DIR, str(directory))
    return directory


@asynccontextmanager
async def context(
    engine: MockEngine, mode: str = "safe", *, gate: SafetyGate | None = None, **kwargs: Any
) -> AsyncIterator[srv.ServerContext]:
    """A :class:`ServerContext` wired to a mock engine, with fast clock verification."""
    assert engine.port not in (0, MockEngine.REAL_ENGINE_PORT), "must be an ephemeral mock port"
    client = UAClient(port=engine.port)
    ctx = srv.ServerContext(
        client,
        gate if gate is not None else SafetyGate(mode),  # type: ignore[arg-type]
        snapshots=snap.SnapshotStore(snap.snapshot_dir()),
        clock_verify_delay_s=kwargs.pop("clock_verify_delay_s", 0.02),
        **kwargs,
    )
    try:
        yield ctx
    finally:
        await ctx.aclose()
        await client.aclose()


async def wait_until(predicate: Any, limit: float = 5.0, step: float = 0.02) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return predicate()


def monitor_exposure(engine: MockEngine) -> float | None:
    """The level the control room is actually exposed to, or None while it is muted."""
    if engine.value(MONITOR_MUTE):
        return None
    level = engine.value(MONITOR_LEVEL)
    assert isinstance(level, int | float)
    dimmed = bool(engine.value(MONITOR_DIM))
    attenuation = engine.value(DIM_ATTENUATION) if dimmed else 0.0
    return float(level) - float(attenuation or 0.0)


def channel_named(payload: dict[str, Any], name: str) -> dict[str, Any]:
    for device in payload["devices"]:
        for channel in device["channels"]:
            if channel["name"] == name:
                return channel
    raise AssertionError(f"{name!r} not in the channel list")


# ------------------------------------------------------------------------------ golden shapes


async def test_get_status_golden_shape(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_get_status(ctx)

    assert set(result) == {"ok", "engine", "clock", "dsp", "talkback", "devices", "safety"}
    assert result["ok"] is True
    assert set(result["engine"]) == {
        "connected",
        "host",
        "port",
        "version",
        "protocol_version",
        "audio_streaming",
    }
    assert result["engine"]["connected"] is True
    assert result["engine"]["version"] == "11.7.1"
    assert set(result["clock"]) == {"source", "locked", "sample_rate", "max_delay_comp"}
    assert result["clock"] == {
        "source": "Internal",
        "locked": True,
        "sample_rate": 48000,
        "max_delay_comp": "Medium",
    }
    assert set(result["dsp"]) == {
        "total_dsp_load_percent",
        "program_load_percent",
        "memory_load_percent",
    }
    assert 0 <= result["dsp"]["total_dsp_load_percent"] <= 100
    assert set(result["talkback"]) == {
        "on",
        "level",
        "auto_off_timer_running",
        "auto_off_failed",
    }
    assert result["talkback"]["auto_off_failed"] is False
    assert result["talkback"] == {
        "on": False,
        "level": 0,
        "auto_off_timer_running": False,
        "auto_off_failed": False,
    }
    assert len(result["devices"]) == 1
    assert set(result["devices"][0]) == {
        "device",
        "name",
        "type_id",
        "online",
        "multi_unit_order",
        "cue_buses",
        "channels",
    }
    assert result["devices"][0]["channels"] == {"input": 28, "aux": 2, "output": 26}
    assert result["safety"]["mode"] == "safe"
    assert result["safety"]["max_monitor_db"] == -6.0


async def test_get_status_reports_every_unit_of_a_cascade(
    mock_factory: Any, base_fixture: Tree
) -> None:
    engine = await mock_factory(cascade_fixture(base_fixture))
    async with context(engine) as ctx:
        result = await srv.tool_get_status(ctx)
    assert [device["device"] for device in result["devices"]] == [0, 1]
    assert [device["multi_unit_order"] for device in result["devices"]] == [0, 1]
    assert all(device["online"] for device in result["devices"])


def same_name_cascade(base: Tree) -> Tree:
    """A real cascade: both units name their strips identically (the derived one prefixes B)."""
    tree = cascade_fixture(base)
    for path, node in tree["nodes"].items():
        name = node["properties"].get("Name")
        if path.startswith("/devices/1/inputs/") and isinstance(name, dict):
            name["value"] = name["value"].removeprefix("B ")
    return tree


async def test_watch_meters_keeps_same_named_cascade_channels_apart(
    mock_factory: Any, base_fixture: Tree
) -> None:
    """Audit M1: two units' 'ANALOG 1' must not merge into one peak."""
    engine = await mock_factory(same_name_cascade(base_fixture))
    engine.nodes["/devices/1/inputs/0/meters/0"]["properties"]["MeterLevel"]["value"] = -10.0
    async with context(engine) as ctx:
        result = await srv.tool_watch_meters(ctx, duration_s=0.1, threshold_db=-20)
    assert result["triggered_channels"] == ["ANALOG 1 (device 1)"]
    assert result["peaks_db"]["ANALOG 1 (device 1)"] == -10.0
    assert result["peaks_db"]["ANALOG 1 (device 0)"] < -70


async def test_a_numeric_device_string_is_an_index(mock: MockEngine) -> None:
    """Audit M2: device='2' must not match 'Apollo x8 Gen 2' by name."""
    async with context(mock) as ctx:
        rig = await ctx.rig()
        assert rig.resolve("ANALOG 1", device="0").device == 0
        with pytest.raises(srv.TargetNotFoundError):
            rig.resolve("ANALOG 1", device="2")


async def test_list_channels_golden_shape(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_list_channels(ctx)

    assert set(result) == {"ok", "devices", "total"}
    assert result["total"] == X8_INPUTS
    assert set(result["devices"][0]) == {"device", "name", "type_id", "online", "channels"}
    analog1 = channel_named(result, "ANALOG 1")
    assert set(analog1) == {
        *IDENTITY_KEYS,
        "io_type",
        "has_preamp",
        "is_monitor",
        "is_talkback",
        "active",
        "fader_db",
        "mute",
        "solo",
        "solo_safe",
        "pan",
        "hidden",
        "stereo",
    }
    assert analog1["io_type"] == "Mic"
    assert analog1["has_preamp"] is True
    assert analog1["mute"] is False
    assert analog1["fader_db"] == pytest.approx(-0.2466, abs=1e-3)


async def test_list_channels_covers_every_kind_and_the_monitor(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_list_channels(ctx, kind="all")
    assert result["total"] == X8_CHANNELS
    monitor = channel_named(result, "MONITOR")
    assert monitor["is_monitor"] is True
    assert monitor["monitor_level_db"] == -22.0
    assert monitor["kind"] == "output"
    aux = channel_named(result, "AUX 1")
    assert aux["kind"] == "aux"


async def test_list_channels_only_polls_the_monitor_for_its_level(mock: MockEngine) -> None:
    """m9: 55 of 56 strips have no CRMonitorLevel; asking them for it is pure wire noise."""
    async with context(mock) as ctx:
        await ctx.rig()
        before = len(mock.sent_frames)
        result = await srv.tool_list_channels(ctx, kind="all")
    asked = [frame for frame in mock.sent_frames[before:] if "CRMonitorLevel" in frame]
    assert asked == [f"get {MONITOR_LEVEL}"]
    assert channel_named(result, "MONITOR")["monitor_level_db"] == -22.0
    assert "monitor_level_db" not in channel_named(result, "ANALOG 1")


async def test_list_channels_can_hide_hidden_strips(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        baseline = await srv.tool_list_channels(ctx, include_hidden=False)
        await mock.push(f"{IN0}/ChannelHidden", True)
        shown = await srv.tool_list_channels(ctx, include_hidden=False)
        everything = await srv.tool_list_channels(ctx, include_hidden=True)
    assert everything["total"] == X8_INPUTS
    assert shown["total"] == baseline["total"] - 1
    with pytest.raises(AssertionError):
        channel_named(shown, "ANALOG 1")


async def test_set_channels_golden_shape(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_set_channels(
            ctx, [{"target": "ANALOG 1", "fader_db": -6.0, "mute": True, "name": "LEAD VOX"}]
        )

    assert set(result) == {"ok", "results"}
    assert result["ok"] is True
    item = result["results"][0]
    assert set(item) == {"target", "resolved", "ok", "writes"}
    assert item["resolved"] == {
        "device": 0,
        "index": 0,
        "name": "ANALOG 1",
        "kind": "input",
        "path": IN0,
    }
    assert set(item["resolved"]) == IDENTITY_KEYS
    assert [write["property"] for write in item["writes"]] == ["Name", "FaderLevel", "Mute"]
    for write in item["writes"]:
        assert set(write) == WRITE_KEYS
        assert write["verified"] is True
        assert write["warnings"] == []
    assert mock.value(f"{IN0}/Name") == "LEAD VOX"
    assert mock.value(f"{IN0}/FaderLevel") == -6.0
    assert mock.value(f"{IN0}/Mute") is True


async def test_set_channels_reports_per_item_failures_without_aborting(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_set_channels(
            ctx,
            [
                {"target": "nonexistent channel"},
                {"target": "ANALOG 2", "fader_db": -3.0},
                {"target": "ANALOG 3"},
                {"fader_db": 0.0},
            ],
        )
    assert result["ok"] is False
    first, second, third, fourth = result["results"]
    assert first["ok"] is False and "No input matches" in first["error"]
    assert second["ok"] is True
    assert third["ok"] is False and "nothing to change" in third["error"]
    assert fourth["ok"] is False and "needs a 'target'" in fourth["error"]
    assert mock.value("/devices/0/inputs/1/FaderLevel") == -3.0


async def test_set_channels_clamps_to_the_property_range(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_set_channels(ctx, [{"target": "ANALOG 1", "fader_db": 99.0}])
    write = result["results"][0]["writes"][0]
    assert write["after"] == 12.0, "FaderLevel maxes out at +12 dB"
    assert write["warnings"] and "changed the requested" in write["warnings"][0]
    assert write["verified"] is True


async def test_set_channels_resolves_by_index_and_device(
    mock_factory: Any, base_fixture: Tree
) -> None:
    engine = await mock_factory(cascade_fixture(base_fixture))
    async with context(engine) as ctx:
        ambiguous = await srv.tool_set_channels(ctx, [{"target": 0, "mute": True}])
        scoped = await srv.tool_set_channels(ctx, [{"target": 0, "device": 1, "mute": True}])
    assert ambiguous["results"][0]["ok"] is False
    assert "ambiguous" in ambiguous["results"][0]["error"]
    assert scoped["results"][0]["resolved"]["device"] == 1
    assert engine.value("/devices/1/inputs/0/Mute") is True
    assert engine.value("/devices/0/inputs/0/Mute") is False


# --------------------------------------------------------------------------------- preamp


async def test_set_preamp_writes_gain_and_switches(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_set_preamp(
            ctx, "ANALOG 1", gain_db=32.0, low_cut=True, pad=True, source="Line"
        )
    assert result["ok"] is True
    assert [write["property"] for write in result["writes"]] == ["IOType", "Gain", "Pad", "LowCut"]
    assert mock.value(f"{IN0}/preamps/0/Gain") == 32.0
    assert mock.value(f"{IN0}/IOType") == "Line"


async def test_set_preamp_refuses_48v_without_confirmation(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        refused = await srv.tool_set_preamp(ctx, "ANALOG 1", phantom_48v=True)
        assert refused["ok"] is False
        assert "confirm_condenser_mic=True" in refused["errors"][0]
        assert "ribbon" in refused["errors"][0]
        assert mock.value(f"{IN0}/preamps/0/48V") is False

        allowed = await srv.tool_set_preamp(
            ctx, "ANALOG 1", phantom_48v=True, confirm_condenser_mic=True
        )
    assert allowed["ok"] is True
    assert mock.value(f"{IN0}/preamps/0/48V") is True


async def test_set_preamp_on_a_channel_without_one_is_actionable(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_set_preamp(ctx, "UG 1", gain_db=10.0)
    assert result["ok"] is False
    assert "has no preamp" in result["error"]
    assert "has_preamp" in result["error"]


async def test_set_preamp_needs_something_to_do(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_set_preamp(ctx, "ANALOG 1")
    assert result["ok"] is False
    assert "nothing to change" in result["error"]


# ----------------------------------------------------------------------------------- sends


async def test_set_sends_resolves_bus_ids(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_set_sends(
            ctx,
            [
                {"target": "ANALOG 1", "send": "cue1", "level_db": -6.0, "pan": -0.5},
                {"target": "ANALOG 1", "send": "aux1", "bypass": True},
            ],
        )
    assert result["ok"] is True
    assert [item["send"] for item in result["results"]] == ["cue1", "aux1"]
    assert mock.value(f"{IN0}/sends/2/Gain") == -6.0
    assert mock.value(f"{IN0}/sends/2/Pan") == -0.5
    assert mock.value(f"{IN0}/sends/0/Bypass") is True


async def test_set_sends_unknown_bus_lists_the_real_ones(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_set_sends(
            ctx, [{"target": "ANALOG 1", "send": "cue9", "level_db": 0.0}]
        )
    error = result["results"][0]["error"]
    assert "has no send 'cue9'" in error
    assert "aux1" in error and "cue1" in error


async def test_set_sends_requires_target_and_send(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_set_sends(ctx, [{"target": "ANALOG 1"}])
    assert "needs a 'target' and a 'send'" in result["results"][0]["error"]


# --------------------------------------------------------------------------------- monitor


async def test_set_monitor_lowering_is_always_allowed(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_set_monitor(ctx, level_db=-40.0, mono=True)
    assert result["ok"] is True
    assert result["resolved"]["name"] == "MONITOR"
    assert result["ceiling_db"] == -6.0
    assert mock.value(MONITOR_LEVEL) == -40.0
    assert mock.value(f"{MONITOR}/MixToMono") is True


async def test_set_monitor_refuses_above_the_ceiling(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_set_monitor(ctx, level_db=0.0)
    assert result["ok"] is False
    assert "ceiling" in result["errors"][0]
    assert "UAD_MCP_MAX_MONITOR_DB" in result["errors"][0]
    assert mock.value(MONITOR_LEVEL) == -22.0, "nothing may reach the wire when refused"


async def test_set_monitor_clamps_the_per_call_step(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_set_monitor(ctx, level_db=-8.0)
    write = result["writes"][0]
    assert write["after"] == -16.0, "+6 dB per call from the stored -22 dB"
    assert write["warnings"]
    assert mock.value(MONITOR_LEVEL) == -16.0


async def test_no_sequence_of_monitor_calls_can_pass_the_ceiling(mock: MockEngine) -> None:
    """SPEC §4.7 monitor rail property: the engine value never exceeds the ceiling."""
    now = [0.0]
    # A generous token bucket: refusals cost tokens too, and a rate-limited call proves nothing
    # about the monitor rail.
    gate = SafetyGate("safe", writes_per_second=500.0, write_burst=500, clock=lambda: now[0])
    ceiling = gate.max_monitor_db
    rng = random.Random(20260729)
    async with context(mock, gate=gate) as ctx:
        for step in range(40):
            requested = rng.choice(
                [rng.uniform(-60.0, 6.0), 0.0, ceiling, ceiling + 0.5, -3.0, 6.0]
            )
            await srv.tool_set_monitor(ctx, level_db=requested)
            if step % 3 == 0:
                await srv.tool_set_monitor(ctx, mute=False)
            if step % 5 == 0:
                await srv.tool_set_monitor(ctx, dim=False)
            if step % 4 == 0:
                await srv.tool_set_monitor(ctx, dim=True, dim_attenuation_db=rng.randint(0, 40))
            if step % 7 == 0:
                await srv.tool_set_monitor(ctx, dim_attenuation_db=rng.randint(0, 60))
            now[0] += rng.choice([0.0, 1.0, 31.0])
            level = mock.value(MONITOR_LEVEL)
            assert level is not None
            assert level <= ceiling + 1e-9, f"step {step}: {level} exceeded the {ceiling} ceiling"


async def test_concurrent_monitor_calls_cannot_beat_the_window_budget(
    mock: MockEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit H3: MCP runs tool calls concurrently; parallel raises must share one budget.

    A slow read-back holds each call between its write and its budget record, and the next call
    starts in that gap, which is the interleaving that used to let every call see an empty window.
    """
    gate = SafetyGate("safe", max_monitor_db=0.0, writes_per_second=500.0, write_burst=500)
    await mock.push(MONITOR_LEVEL, -40.0)
    async with context(mock, gate=gate) as ctx:
        await srv.tool_list_channels(ctx)  # warm the topology cache
        real_set = ctx.client.set_value

        async def slow_verify(path: str, value: Any) -> Any:
            applied = await real_set(path, value)
            await asyncio.sleep(0.05)
            return applied

        monkeypatch.setattr(ctx.client, "set_value", slow_verify)

        async def raise_by_six(delay: float) -> None:
            await asyncio.sleep(delay)
            await srv.tool_set_monitor(ctx, level_db=0.0)

        await asyncio.gather(*(raise_by_six(0.01 * i) for i in range(4)))
    assert mock.value(MONITOR_LEVEL) <= -40.0 + gate.describe()["monitor_window_db"] + 1e-9


async def test_the_server_never_newly_exposes_an_above_ceiling_level(mock: MockEngine) -> None:
    """The real property: no call may RAISE what the room hears past the ceiling.

    The physical monitor knob is modelled with engine-side pushes, so the stored level goes hot
    without the server's help. Every release path (unmute, un-dim, and lowering the dim
    attenuation) must then refuse rather than open that level up.
    """
    now = [0.0]
    gate = SafetyGate("safe", writes_per_second=500.0, write_burst=500, clock=lambda: now[0])
    ceiling = gate.max_monitor_db
    rng = random.Random(4710)
    calls: list[Any] = [
        lambda ctx: srv.tool_set_monitor(ctx, mute=False),
        lambda ctx: srv.tool_set_monitor(ctx, mute=True),
        lambda ctx: srv.tool_set_monitor(ctx, dim=False),
        lambda ctx: srv.tool_set_monitor(ctx, dim=True),
        lambda ctx: srv.tool_set_monitor(ctx, dim_attenuation_db=rng.randint(0, 17)),
        lambda ctx: srv.tool_set_monitor(ctx, dim_attenuation_db=rng.randint(18, 60)),
        lambda ctx: srv.tool_set_monitor(ctx, level_db=rng.uniform(-60.0, 6.0)),
        lambda ctx: srv.tool_set_monitor(ctx, level_db=rng.uniform(-60.0, 6.0), mute=False),
        lambda ctx: srv.tool_set_channels(
            ctx, [{"target": "MONITOR", "kind": "output", "mute": False}]
        ),
        lambda ctx: srv.tool_safe_state(ctx),
    ]
    async with context(mock, gate=gate) as ctx:
        for step in range(60):
            if step % 5 == 0:
                # The physical panel: knob turned up past the ceiling, dim engaged to tame it.
                # Every release path is now one write away from an above-ceiling room.
                await mock.push(MONITOR_LEVEL, rng.uniform(-4.0, 0.0))
                await mock.push(MONITOR_MUTE, False)
                await mock.push(MONITOR_DIM, True)
                await mock.push(DIM_ATTENUATION, 17)
            before = monitor_exposure(mock)
            result = await rng.choice(calls)(ctx)
            assert isinstance(result, dict)
            after = monitor_exposure(mock)
            now[0] += rng.choice([0.0, 1.0, 31.0])
            if after is None:
                continue
            if before is None or after > before + 1e-9:
                assert after <= ceiling + 1e-9, (
                    f"step {step}: exposure rose from {before} to {after}, past {ceiling}: "
                    f"{result}"
                )


async def test_lowering_the_dim_attenuation_cannot_un_dim_past_the_ceiling(
    mock: MockEngine,
) -> None:
    """B2 repro: 0 dB stored, dimmed 17 dB, then ask for 0 dB of dim. That is a +17 dB jump."""
    async with context(mock) as ctx:
        await mock.push(MONITOR_LEVEL, 0.0)
        await mock.push(MONITOR_DIM, True)
        result = await srv.tool_set_monitor(ctx, dim_attenuation_db=0)
    assert result["ok"] is False
    assert "UAD_MCP_MAX_MONITOR_DB" in result["errors"][0]
    assert "un-dim" in result["errors"][0]
    assert mock.value(DIM_ATTENUATION) == 17, "nothing may reach the wire when refused"


async def test_lowering_the_dim_attenuation_is_allowed_when_the_room_stays_quiet(
    mock: MockEngine,
) -> None:
    async with context(mock) as ctx:
        await mock.push(MONITOR_DIM, True)
        result = await srv.tool_set_monitor(ctx, dim_attenuation_db=0)
    assert result["ok"] is True, result
    assert mock.value(DIM_ATTENUATION) == 0, "-22 dB with no dim is still under the ceiling"


async def test_raising_the_dim_attenuation_is_always_allowed(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        await mock.push(MONITOR_LEVEL, 0.0)
        await mock.push(MONITOR_DIM, True)
        result = await srv.tool_set_monitor(ctx, dim_attenuation_db=40)
    assert result["ok"] is True, result
    assert mock.value(DIM_ATTENUATION) == 40


async def test_dim_attenuation_fails_closed_when_the_level_read_fails(
    mock: MockEngine,
) -> None:
    async with context(mock) as ctx:
        await mock.push(MONITOR_DIM, True)
        await mock.push(MONITOR_LEVEL, None)  # the engine answered with no usable value
        result = await srv.tool_set_monitor(ctx, dim_attenuation_db=0)
    assert result["ok"] is False
    assert "CRMonitorLevel" in result["errors"][0]
    assert mock.value(DIM_ATTENUATION) == 17


async def test_set_monitor_rails_the_release_against_the_verified_level(
    mock: MockEngine,
) -> None:
    """M4: the release is checked against what the engine confirmed, not what was asked for."""
    async with context(mock) as ctx:
        await mock.push(MONITOR_LEVEL, -30.0)
        await mock.push(MONITOR_MUTE, True)
        result = await srv.tool_set_monitor(ctx, level_db=-8.0, mute=False)
    assert result["ok"] is True, result
    assert result["applied_level_db"] == -24.0, "+6 dB per call, not the -8 dB requested"
    assert mock.value(MONITOR_LEVEL) == -24.0
    assert mock.value(MONITOR_MUTE) is False


async def test_set_monitor_rails_the_release_on_what_the_engine_reports(
    mock: MockEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4, the dangerous direction: the engine says it applied a level far above the ceiling.

    The request was quiet, so railing on the REQUESTED value would happily unmute into it.
    """
    async with context(mock) as ctx:
        await mock.push(MONITOR_MUTE, True)
        real_set = ctx.client.set_value

        async def lying_set(path: str, value: Any) -> Any:
            applied = await real_set(path, value)
            return -1.0 if path == MONITOR_LEVEL else applied

        monkeypatch.setattr(ctx.client, "set_value", lying_set)
        result = await srv.tool_set_monitor(ctx, level_db=-40.0, mute=False)

    assert result["ok"] is False
    assert result["applied_level_db"] == -1.0, "the reported level is what the rail must see"
    assert "UAD_MCP_MAX_MONITOR_DB" in " ".join(result["errors"])
    assert mock.value(MONITOR_MUTE) is True, "the mute must stay engaged"


async def test_set_monitor_still_releases_when_the_engine_quantizes_the_level(
    mock: MockEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4 must not block a SAFE unmute over float noise: engines quantize (SPEC §1)."""
    async with context(mock) as ctx:
        await mock.push(MONITOR_MUTE, True)
        real_set = ctx.client.set_value

        async def quantizing_set(path: str, value: Any) -> Any:
            applied = await real_set(path, value)
            return -34.996 if path == MONITOR_LEVEL else applied

        monkeypatch.setattr(ctx.client, "set_value", quantizing_set)
        result = await srv.tool_set_monitor(ctx, level_db=-35.0, mute=False)

    assert result["applied_level_db"] == -34.996
    assert mock.value(MONITOR_MUTE) is False, "a level 29 dB under the ceiling must still unmute"
    assert any("-34.996" in warning for write in result["writes"] for warning in write["warnings"])


async def test_set_monitor_skips_the_release_when_the_level_is_unreadable(
    mock: MockEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4: no usable number back from the engine means no railing is possible. Skip the release."""
    async with context(mock) as ctx:
        await mock.push(MONITOR_MUTE, True)
        real_set = ctx.client.set_value

        async def mute_set(path: str, value: Any) -> Any:
            applied = await real_set(path, value)
            return None if path == MONITOR_LEVEL else applied

        monkeypatch.setattr(ctx.client, "set_value", mute_set)
        result = await srv.tool_set_monitor(ctx, level_db=-40.0, mute=False)

    assert result["ok"] is False
    assert result["applied_level_db"] is None
    assert any("Skipped releasing Mute" in error for error in result["errors"])
    assert any("no usable monitor level" in error for error in result["errors"])
    assert mock.value(MONITOR_MUTE) is True, "the mute must stay engaged"


async def test_set_monitor_skips_the_release_when_the_level_write_fails(
    mock: MockEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with context(mock) as ctx:
        await mock.push(MONITOR_MUTE, True)
        real_set = ctx.client.set_value

        async def flaky_set(path: str, value: Any) -> Any:
            if path == MONITOR_LEVEL:
                raise UAConnectError("the control socket went away mid-write")
            return await real_set(path, value)

        monkeypatch.setattr(ctx.client, "set_value", flaky_set)
        result = await srv.tool_set_monitor(ctx, level_db=-40.0, mute=False)

    assert result["ok"] is False
    assert any("Skipped releasing Mute" in error for error in result["errors"])
    assert mock.value(MONITOR_MUTE) is True


async def test_set_monitor_still_engages_a_mute_when_the_level_write_fails(
    mock: MockEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only RELEASES are skipped: getting quieter must never be blocked by a failed read."""
    async with context(mock) as ctx:
        real_set = ctx.client.set_value

        async def flaky_set(path: str, value: Any) -> Any:
            if path == MONITOR_LEVEL:
                raise UAConnectError("the control socket went away mid-write")
            return await real_set(path, value)

        monkeypatch.setattr(ctx.client, "set_value", flaky_set)
        result = await srv.tool_set_monitor(ctx, level_db=-40.0, mute=True)

    assert result["ok"] is False, "the level write still failed"
    assert mock.value(MONITOR_MUTE) is True


async def test_unmuting_the_monitor_fails_closed_when_the_level_cannot_be_read(
    mock: MockEngine,
) -> None:
    """B1: an unreadable CRMonitorLevel must refuse the unmute, not log and allow it."""
    async with context(mock) as ctx:
        await mock.push(MONITOR_MUTE, True)
        await mock.push(MONITOR_LEVEL, None)
        by_monitor = await srv.tool_set_monitor(ctx, mute=False)
        by_channel = await srv.tool_set_channels(
            ctx, [{"target": "MONITOR", "kind": "output", "mute": False}]
        )
    assert by_monitor["ok"] is False
    assert "CRMonitorLevel" in by_monitor["errors"][0]
    assert by_channel["ok"] is False
    assert "CRMonitorLevel" in by_channel["results"][0]["errors"][0]
    assert mock.value(MONITOR_MUTE) is True


async def test_unmuting_onto_an_above_ceiling_level_is_refused(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        # Engine-side change (as if someone turned the physical knob up): the stored level is
        # now hotter than the ceiling, so releasing the mute would jump straight there.
        await mock.push(MONITOR_LEVEL, -2.0)
        await mock.push(f"{MONITOR}/Mute", True)
        result = await srv.tool_set_monitor(ctx, mute=False)
    assert result["ok"] is False
    assert "unmute" in result["errors"][0]
    assert "CRMonitorLevel" in result["errors"][0]
    assert mock.value(f"{MONITOR}/Mute") is True


async def test_unmuting_the_monitor_via_set_channels_is_railed_too(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        await mock.push(MONITOR_LEVEL, -2.0)
        await mock.push(f"{MONITOR}/Mute", True)
        result = await srv.tool_set_channels(
            ctx, [{"target": "MONITOR", "kind": "output", "mute": False}]
        )
    assert result["ok"] is False
    assert "unmute" in result["results"][0]["errors"][0]
    assert mock.value(f"{MONITOR}/Mute") is True


async def test_set_monitor_needs_something_to_do(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_set_monitor(ctx)
    assert result["ok"] is False
    assert "nothing to change" in result["error"]


# -------------------------------------------------------------------------------- talkback


async def test_talkback_auto_off_fires(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_talkback(ctx, on=True, level=60, auto_off_s=0.1)
        assert result["ok"] is True
        assert result["auto_off_armed"] is True
        assert result["auto_off_s"] == pytest.approx(0.1)
        assert mock.value("/TalkbackOn") is True
        assert mock.value("/TalkbackMaster") == 60
        assert await wait_until(lambda: mock.value("/TalkbackOn") is False)


async def test_repeat_talkback_calls_replace_the_timer(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        await srv.tool_talkback(ctx, on=True, auto_off_s=0.05)
        first = ctx._talkback_task
        await srv.tool_talkback(ctx, on=True, auto_off_s=5.0)
        second = ctx._talkback_task
        assert first is not None and second is not None
        assert first is not second
        assert ctx.talkback_timer_running
        # The replacement deadline is 5 s away, so talkback must still be on well past 0.05 s.
        await asyncio.sleep(0.2)
        assert first.cancelled(), "the first timer must be cancelled, not left to fire"
        assert mock.value("/TalkbackOn") is True
        await srv.tool_talkback(ctx, on=False)
        assert ctx.talkback_timer_running is False
        assert mock.value("/TalkbackOn") is False


async def test_shutdown_closes_talkback_this_server_opened(mock: MockEngine) -> None:
    """Audit H1: a client that disconnects inside the auto-off window must not leave a live mic."""
    async with context(mock) as ctx:
        await srv.tool_talkback(ctx, on=True, auto_off_s=30.0)
        assert mock.value("/TalkbackOn") is True
        await ctx.shutdown()
        assert mock.value("/TalkbackOn") is False
        assert ctx.talkback_timer_running is False


async def test_shutdown_leaves_talkback_alone_without_a_timer(mock: MockEngine) -> None:
    """Talkback opened from Console (no timer of ours) is not this server's to close."""
    async with context(mock) as ctx:
        await mock.push("/TalkbackOn", True)
        await ctx.shutdown()
        assert mock.value("/TalkbackOn") is True
        assert mock.set_count() == 0


async def test_serve_runs_shutdown_in_the_same_loop(mock: MockEngine) -> None:
    """Audit H1: cleanup happens before the serving loop ends, so the write still lands."""
    async with context(mock) as ctx:

        class FakeServer:
            async def run_stdio_async(self) -> None:
                await srv.tool_talkback(ctx, on=True, auto_off_s=30.0)

        await cli._serve(FakeServer(), ctx, ctx.client)
    assert mock.value("/TalkbackOn") is False


async def test_talkback_arms_the_timer_even_when_the_level_write_fails(
    mock: MockEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M6: a failed level write must never leave talkback open with nothing to close it."""
    async with context(mock) as ctx:
        real_set = ctx.client.set_value

        async def flaky_set(path: str, value: Any) -> Any:
            if path == "/TalkbackMaster":
                raise UAConnectError("the control socket went away mid-write")
            return await real_set(path, value)

        monkeypatch.setattr(ctx.client, "set_value", flaky_set)
        result = await srv.tool_talkback(ctx, on=True, level=60, auto_off_s=0.1)

        assert result["ok"] is False, "the level failure is still reported"
        assert result["errors"]
        assert result["auto_off_armed"] is True, "talkback is ON: the timer must be armed"
        assert mock.value("/TalkbackOn") is True
        assert await wait_until(lambda: mock.value("/TalkbackOn") is False)


async def test_talkback_does_not_arm_a_timer_when_the_gate_refuses(mock: MockEngine) -> None:
    """A policy refusal is the one case where talkback provably never opened."""
    async with context(mock, "readonly") as ctx:
        result = await srv.tool_talkback(ctx, on=True, level=60, auto_off_s=0.1)
        assert result["ok"] is False
        assert "UAD_MCP_MODE" in " ".join(result["errors"])
        assert result["auto_off_armed"] is False
        assert ctx.talkback_timer_running is False
        assert mock.value("/TalkbackOn") is False
        assert mock.set_count() == 0


async def test_talkback_arms_the_timer_when_the_ack_is_lost(
    mock: MockEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M6: a write that reached the engine but never came back is UNKNOWN, so arm the timer.

    This is the shape that leaves an open mic: the set frame landed, the ack did not.
    """
    async with context(mock) as ctx:
        real_set = ctx.client.set_value
        swallowed: list[str] = []

        async def lost_ack(path: str, value: Any) -> Any:
            applied = await real_set(path, value)
            if path == "/TalkbackOn" and value is True and not swallowed:
                swallowed.append(path)
                raise UATimeoutError("no reply from UA Mixer Engine for set /TalkbackOn within 2s")
            return applied

        monkeypatch.setattr(ctx.client, "set_value", lost_ack)
        result = await srv.tool_talkback(ctx, on=True, auto_off_s=0.05)

        assert result["ok"] is False, "the lost ack is still reported"
        assert mock.value("/TalkbackOn") is True, "talkback really did open"
        assert result["auto_off_armed"] is True, "an open mic must never be left without a timer"
        assert await wait_until(lambda: mock.value("/TalkbackOn") is False)


async def test_talkback_auto_off_retries_before_giving_up(
    mock: MockEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M7: one dropped write must not end the attempt to close an open talkback mic."""
    async with context(mock, talkback_retry_delays_s=(0.01, 0.01, 0.01)) as ctx:
        real_set = ctx.client.set_value
        attempts: list[Any] = []

        async def flaky_set(path: str, value: Any) -> Any:
            if path == "/TalkbackOn" and value is False:
                attempts.append(value)
                if len(attempts) < 2:
                    raise UAConnectError("the control socket went away mid-write")
            return await real_set(path, value)

        monkeypatch.setattr(ctx.client, "set_value", flaky_set)
        await srv.tool_talkback(ctx, on=True, auto_off_s=0.01)
        assert await wait_until(lambda: mock.value("/TalkbackOn") is False)
        assert len(attempts) == 2, "the first attempt failed and the second one closed it"
        assert ctx.talkback_auto_off_failed is False


async def test_talkback_auto_off_failure_is_reported_in_status(
    mock: MockEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M7: when every retry fails, say so - talkback may still be open."""
    async with context(mock, talkback_retry_delays_s=(0.01, 0.01, 0.01)) as ctx:
        real_set = ctx.client.set_value
        attempts: list[Any] = []

        async def always_fails(path: str, value: Any) -> Any:
            if path == "/TalkbackOn" and value is False:
                attempts.append(value)
                raise UAConnectError("the control socket went away mid-write")
            return await real_set(path, value)

        monkeypatch.setattr(ctx.client, "set_value", always_fails)
        await srv.tool_talkback(ctx, on=True, auto_off_s=0.01)
        assert await wait_until(lambda: ctx.talkback_auto_off_failed)
        assert len(attempts) == 3, "3 attempts, then give up loudly"
        monkeypatch.setattr(ctx.client, "set_value", real_set)
        status = await srv.tool_get_status(ctx)

    assert status["talkback"]["auto_off_failed"] is True
    assert status["talkback"]["on"] is True, "and talkback really is still open"


async def test_arming_the_timer_again_clears_a_previous_failure(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        ctx.talkback_auto_off_failed = True
        await srv.tool_talkback(ctx, on=True, auto_off_s=5.0)
        assert ctx.talkback_auto_off_failed is False


@pytest.mark.parametrize("close", ["talkback", "safe_state"])
async def test_closing_talkback_by_hand_clears_the_failure_flag(
    mock: MockEngine, close: str
) -> None:
    async with context(mock) as ctx:
        await mock.push("/TalkbackOn", True)
        ctx.talkback_auto_off_failed = True
        if close == "talkback":
            await srv.tool_talkback(ctx, on=False)
        else:
            await srv.tool_safe_state(ctx)
        assert mock.value("/TalkbackOn") is False
        assert ctx.talkback_auto_off_failed is False, "talkback is shut; stop crying wolf"


async def test_talkback_needs_something_to_do(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_talkback(ctx)
    assert result["ok"] is False
    assert "nothing to change" in result["error"]


async def test_safe_state_mutes_monitors_and_kills_talkback(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        await srv.tool_talkback(ctx, on=True, auto_off_s=30.0)
        result = await srv.tool_safe_state(ctx)
    assert result["ok"] is True
    assert result["monitors_muted"][0]["name"] == "MONITOR"
    assert mock.value(f"{MONITOR}/Mute") is True
    assert mock.value("/TalkbackOn") is False
    assert "uad_set_monitor(mute=false)" in result["next"]


async def test_safe_state_works_with_an_empty_rate_limit_bucket(mock: MockEngine) -> None:
    """Audit H2: a runaway agent empties the bucket; the panic button must still land."""
    gate = SafetyGate("safe", writes_per_second=0.01, write_burst=2)
    async with context(mock, gate=gate) as ctx:
        await mock.push("/TalkbackOn", True)
        await srv.tool_set_channels(ctx, [{"target": i, "fader_db": -1} for i in range(3)])
        result = await srv.tool_safe_state(ctx)
    assert result["ok"] is True, result.get("errors")
    assert mock.value(f"{MONITOR}/Mute") is True
    assert mock.value("/TalkbackOn") is False


# ----------------------------------------------------------------------------------- clock


@pytest.mark.parametrize("missing", ["all", "one"])
async def test_clock_change_fails_closed_when_meters_cannot_be_read(
    mock: MockEngine, missing: str
) -> None:
    """Audit H4: an unread meter is not a silent meter."""
    for path, node in mock.nodes.items():
        if "/meters/" in path and (missing == "all" or path.startswith(f"{IN0}/meters/")):
            node["properties"].pop("MeterLevel", None)
    async with context(mock) as ctx:
        result = await srv.tool_set_clock(ctx, "ADAT", confirm=True)
    assert result["ok"] is False
    assert "no proof that audio is silent" in result["error"]
    assert mock.value("/ClockSource") == "Internal"
    assert mock.set_count() == 0


async def test_set_clock_requires_confirmation(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_set_clock(ctx, "ADAT")
    assert result["ok"] is False
    assert "confirm=True" in result["error"]
    assert mock.value("/ClockSource") == "Internal"


async def test_set_clock_refuses_while_audio_is_flowing(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        await mock.push(IN0_METER, -3.0)
        result = await srv.tool_set_clock(ctx, "ADAT", confirm=True)
    assert result["ok"] is False
    assert "audio is flowing" in result["error"]
    assert mock.value("/ClockSource") == "Internal"


async def test_set_clock_verifies_lock(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_set_clock(ctx, "ADAT", confirm=True)
    assert result["ok"] is True
    assert result["locked"] is True
    assert result["reverted"] is False
    assert result["audio_check"] == {
        "flowing": False,
        "peak_db": pytest.approx(-77.0, abs=0.01),
        "loudest_channel": result["audio_check"]["loudest_channel"],
    }
    assert mock.value("/ClockSource") == "ADAT"


async def test_set_clock_reverts_itself_when_lock_fails(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        await mock.push("/ClockLocked", False)
        result = await srv.tool_set_clock(ctx, "ADAT", confirm=True)
    assert result["ok"] is False
    assert result["locked"] is False
    assert result["reverted"] is True
    assert "reverted to 'Internal'" in result["error"]
    assert "same sample rate" in result["error"]
    assert mock.value("/ClockSource") == "Internal", "the clock source must be back where it was"
    assert result["revert"]["after"] == "Internal"


async def test_set_clock_survives_a_failed_auto_revert(
    mock: MockEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M5: if the revert itself fails, the result must still come back and say so."""
    async with context(mock) as ctx:
        await mock.push("/ClockLocked", False)
        real_set = ctx.client.set_value
        writes: list[Any] = []

        async def flaky_set(path: str, value: Any) -> Any:
            if path == "/ClockSource":
                writes.append(value)
                if len(writes) > 1:
                    raise UAConnectError("the control socket went away mid-revert")
            return await real_set(path, value)

        monkeypatch.setattr(ctx.client, "set_value", flaky_set)
        result = await srv.tool_set_clock(ctx, "ADAT", confirm=True)

    assert result["ok"] is False
    assert result["reverted"] is False
    assert "UA Mixer Engine not reachable" in result["revert_error"]
    assert "left at 'ADAT' and unlocked" in result["error"]
    assert "uad_set_clock(source='Internal', confirm=true)" in result["error"]
    # The populated result survived: the model can still see what happened before the revert.
    assert result["write"]["after"] == "ADAT"
    assert result["locked"] is False
    assert mock.value("/ClockSource") == "ADAT", "the rig really is left on the new source"


async def test_set_clock_reverts_even_when_the_lock_recheck_fails(
    mock: MockEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed re-read of ClockLocked must not be reported as a failed revert."""
    async with context(mock) as ctx:
        await mock.push("/ClockLocked", False)
        real_value = ctx.value
        reads: list[str] = []

        async def flaky_value(path: str, default: Any = None) -> Any:
            reads.append(path)
            if path == "/ClockLocked" and len(reads) > 1:
                raise UAConnectError("the control socket went away mid-read")
            return await real_value(path, default)

        monkeypatch.setattr(ctx, "value", flaky_value)
        result = await srv.tool_set_clock(ctx, "ADAT", confirm=True)

    assert result["reverted"] is True, "the revert write itself succeeded"
    assert result["locked_after_revert"] is None, "unknown, not false"
    assert "revert_error" not in result
    assert "reverted to 'Internal'" in result["error"]
    assert mock.value("/ClockSource") == "Internal"


async def test_set_clock_rejects_an_unknown_source(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_set_clock(ctx, "Bluetooth", confirm=True)
    assert result["ok"] is False
    assert "not one of its allowed values" in result["error"]
    assert "Internal" in result["error"]
    assert mock.value("/ClockSource") == "Internal"


async def test_set_sample_rate_needs_confirmation_and_silence(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        unconfirmed = await srv.tool_set_sample_rate(ctx, 96000)
        assert unconfirmed["ok"] is False
        assert "confirm=True" in unconfirmed["error"]

        await mock.push(IN0_METER, -3.0)
        loud = await srv.tool_set_sample_rate(ctx, 96000, confirm=True)
        assert loud["ok"] is False
        assert "audio is flowing" in loud["error"]
        assert mock.value("/SampleRate") == 48000

        await mock.push(IN0_METER, -77.0)
        applied = await srv.tool_set_sample_rate(ctx, 96000, confirm=True)
    assert applied["ok"] is True
    assert applied["write"]["after"] == 96000
    assert applied["locked"] is True
    assert mock.value("/SampleRate") == 96000


async def test_set_sample_rate_rejects_a_rate_the_engine_does_not_offer(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_set_sample_rate(ctx, 12345, confirm=True)
    assert result["ok"] is False
    assert "not one of its allowed values" in result["error"]
    assert mock.value("/SampleRate") == 48000


# --------------------------------------------------------------------------------- plugins


async def test_list_plugins_filters_and_reports_totals(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        everything = await srv.tool_list_plugins(ctx, limit=5)
        searched = await srv.tool_list_plugins(ctx, search="la-3a")
        unison = await srv.tool_list_plugins(ctx, unison_only=True, limit=500)
    assert everything["installed"] == 215
    assert everything["shown"] == 5
    assert everything["truncated"] == 210
    assert "Compressors & Limiters" in everything["categories"]
    assert [entry["name"] for entry in searched["plugins"]] == [LA3A]
    assert searched["plugins"][0]["authorized"] is True
    assert unison["plugins"] and all(entry["unison"] for entry in unison["plugins"])


async def test_get_inserts_finds_what_is_loaded(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        loaded = await srv.tool_get_inserts(ctx)
        one = await srv.tool_get_inserts(ctx, target="VIRTUAL 1", loaded_only=False)
    plugins = {(row["name"], row["slot"]): row["plugin"] for row in loaded["inserts"]}
    assert plugins[("VIRTUAL 1", "1")] == LA3A
    assert plugins[("ANALOG 5", "3")] == "UAD Ampex ATR-102"
    assert all(row["plugin"] for row in loaded["inserts"])
    assert [row["slot"] for row in one["inserts"]] == ["0", "1", "2", "3", "4", "5", "6", "7"]
    assert one["channels_checked"] == 1


async def test_get_inserts_includes_the_unison_slot(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        await mock.push(f"{IN0}/preamps/0/effects/0/EffectName", "UAD 610-B")
        result = await srv.tool_get_inserts(ctx, target="ANALOG 1")
    unison = [row for row in result["inserts"] if row["slot"] == srv.UNISON_SLOT]
    assert unison and unison[0]["plugin"] == "UAD 610-B"
    assert unison[0]["slot_path"] == f"{IN0}/preamps/0/effects/0"


async def test_load_plugin_reports_materialized_parameters(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_load_plugin(ctx, "VIRTUAL 1", "bx_refinement", slot=1)
    assert result["ok"] is True
    assert result["plugin"] == "UAD bx_refinement"
    assert result["materialized"] is True, "this slot already has parameter nodes"
    assert result["parameter_count"] == 7
    assert mock.value(f"{IN18_SLOT1}/EffectName") == "UAD bx_refinement"


async def test_load_plugin_times_out_gracefully_on_an_empty_slot(
    mock: MockEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mock mirrors the engine: setting EffectName does NOT create parameter nodes."""
    monkeypatch.setattr(snap, "MATERIALIZE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(snap, "MATERIALIZE_POLL_S", 0.05)
    async with context(mock) as ctx:
        result = await srv.tool_load_plugin(ctx, "ANALOG 1", LA3A, slot=1)
    assert result["ok"] is True, "the load itself succeeded; only the parameters are late"
    assert result["materialized"] is False
    assert result["parameter_count"] == 0
    warning = result["writes"][0]["warnings"][0]
    assert "did not appear" in warning
    assert "uad_get_plugin_params" in warning
    assert mock.value(f"{IN0}/effects/1/EffectName") == LA3A


async def test_load_plugin_sees_late_materialization(mock: MockEngine) -> None:
    from tests.test_snapshot import inject_parameters

    slot = f"{IN0}/effects/2"

    async def materialize_later() -> None:
        await asyncio.sleep(0.1)
        inject_parameters(mock, slot, ["Peak Reduction", "Gain"])

    async with context(mock) as ctx:
        task = asyncio.create_task(materialize_later())
        result = await srv.tool_load_plugin(ctx, "ANALOG 1", LA3A, slot=2)
        await task
    assert result["materialized"] is True
    assert result["parameter_count"] == 2
    assert result["writes"][0]["warnings"] == []


async def test_load_plugin_warns_about_an_unauthorized_plugin(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_load_plugin(ctx, "VIRTUAL 1", "UAD 4K Buss Compressor", slot=1)
    assert any("authorized=false" in warning for warning in result["writes"][0]["warnings"])


async def test_load_plugin_rejects_an_unknown_name(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_load_plugin(ctx, "ANALOG 1", "Fairchild Nonexistent", slot=1)
    assert result["ok"] is False
    assert "No installed UAD plugin matches" in result["error"]
    assert "uad_list_plugins" in result["error"]
    assert mock.set_count() == 0


async def test_load_plugin_rejects_an_ambiguous_name(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_load_plugin(ctx, "ANALOG 1", "Teletronix", slot=1)
    assert result["ok"] is False
    assert "matches 5 installed plugins" in result["error"]
    assert "UAD Teletronix LA-3A" in result["error"]
    assert "Pass the full plugin name" in result["error"]
    assert mock.set_count() == 0


async def test_unison_slot_needs_a_unison_plugin_and_a_preamp(
    mock: MockEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(snap, "MATERIALIZE_TIMEOUT_S", 0.1)
    monkeypatch.setattr(snap, "MATERIALIZE_POLL_S", 0.05)
    async with context(mock) as ctx:
        not_unison = await srv.tool_load_plugin(ctx, "ANALOG 1", LA3A, slot="unison")
        no_preamp = await srv.tool_load_plugin(ctx, "UG 1", LA3A, slot=1)
        unison_on_digital = await srv.tool_load_plugin(ctx, "UG 1", LA3A, slot="unison")
    assert not_unison["ok"] is False
    assert "not a Unison plugin" in not_unison["error"]
    assert no_preamp["ok"] is True, "a digital input still has ordinary insert slots"
    assert unison_on_digital["ok"] is False
    assert "no Unison slot" in unison_on_digital["error"]


async def test_remove_plugin_clears_the_slot(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_remove_plugin(ctx, "VIRTUAL 1", slot=1)
    assert result["ok"] is True
    assert result["removed"] == LA3A
    assert mock.value(f"{IN18_SLOT1}/EffectName") == ""


async def test_get_plugin_params_lists_names_and_values(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_get_plugin_params(ctx, "VIRTUAL 1", slot=1)
    assert result["plugin"] == LA3A
    assert [entry["name"] for entry in result["parameters"]][:2] == ["Peak Reduction", "Gain"]
    assert result["parameters"][0]["normalized"] == pytest.approx(0.27008, abs=1e-4)
    assert result["parameters"][0]["display"] == "2.7"


async def test_get_plugin_params_explains_an_empty_slot(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_get_plugin_params(ctx, "ANALOG 1", slot=0)
    assert result["parameters"] == []
    assert "uad_load_plugin" in result["note"]


async def test_set_plugin_params_by_name(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_set_plugin_params(
            ctx,
            "VIRTUAL 1",
            [{"name": "Gain", "normalized": 0.75}, {"name": "Nope", "normalized": 0.5}],
            slot=1,
        )
    assert result["ok"] is False
    good, bad = result["results"]
    assert good["ok"] is True
    assert good["writes"][0]["after"] == 0.75
    assert bad["ok"] is False
    assert "is not a parameter" in bad["error"]
    assert "Peak Reduction" in bad["error"]
    assert mock.value(f"{IN18_SLOT1}/parameters/1/NormalizedValue") == 0.75


async def test_set_plugin_params_needs_a_loaded_plugin(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        empty = await srv.tool_set_plugin_params(
            ctx, "ANALOG 1", [{"name": "Gain", "normalized": 0.5}], slot=0
        )
        nothing = await srv.tool_set_plugin_params(ctx, "VIRTUAL 1", [], slot=1)
    assert empty["ok"] is False and "exposes no parameters" in empty["error"]
    assert nothing["ok"] is False and "params was empty" in nothing["error"]


async def test_plugin_preset_validates_against_the_catalog(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        bad = await srv.tool_plugin_preset(ctx, "VIRTUAL 1", "Not A Preset", slot=1)
        assert bad["ok"] is False
        assert "has no preset named" in bad["error"]

        catalog = await ctx.plugin_catalog()
        entry = next(item for item in catalog if item["name"] == LA3A)
        presets = srv._flatten_presets(
            (await ctx.client.get_prop(f"{entry['path']}/Preset")).values or []
        )
        good = await srv.tool_plugin_preset(ctx, "VIRTUAL 1", presets[0], slot=1)
    assert good["ok"] is True
    assert mock.value(f"{IN18_SLOT1}/Preset") == presets[0]


async def test_plugin_preset_needs_a_loaded_plugin(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_plugin_preset(ctx, "ANALOG 1", "Default", slot=0)
    assert result["ok"] is False
    assert "nothing to recall a preset into" in result["error"]


# ---------------------------------------------------------------------------------- meters


async def test_peek_meters_calls_out_the_floor(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_peek_meters(ctx)
    assert result["floor_db"] == -77.0
    assert result["at_floor"] == len(result["meters"]), "the fixture is captured in silence"
    assert "dead digital link" in result["note"]
    row = next(row for row in result["meters"] if row["name"] == "ANALOG 1")
    assert set(row) == {*IDENTITY_KEYS, "io_type", "level_db", "peak_db", "clip", "at_floor"}


async def test_peek_meters_can_filter_by_level(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        await mock.push(IN0_METER, -12.0)
        result = await srv.tool_peek_meters(ctx, above_db=-60.0)
    assert [row["name"] for row in result["meters"]] == ["ANALOG 1"]
    assert result["meters"][0]["level_db"] == -12.0
    assert result["meters"][0]["at_floor"] is False
    assert result["note"] is None


async def test_watch_meters_exits_early_when_a_meter_rises(mock: MockEngine) -> None:
    async def raise_meter_later() -> None:
        await asyncio.sleep(0.15)
        await mock.push(IN0_METER, -4.0)

    async with context(mock) as ctx:
        task = asyncio.create_task(raise_meter_later())
        result = await srv.tool_watch_meters(
            ctx, targets=["ANALOG 1"], duration_s=20.0, threshold_db=-20.0, interval_s=0.05
        )
        await task
    assert result["triggered"] is True
    assert result["triggered_channels"] == ["ANALOG 1"]
    assert result["peaks_db"]["ANALOG 1"] == -4.0
    assert result["elapsed_s"] < 5.0, "it must not sit out the full 20 s"
    assert result["polls"] > 1
    assert result["note"] is None


async def test_watch_meters_reports_a_quiet_watch(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_watch_meters(
            ctx, targets=["ANALOG 1"], duration_s=0.2, interval_s=0.05
        )
    assert result["triggered"] is False
    assert result["triggered_channels"] == []
    assert "nothing rose above" in result["note"]
    assert result["channels_watched"] == 1


async def test_watch_meters_duration_is_capped(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        # A threshold below the floor triggers on the first poll, so the cap can be asserted
        # without actually waiting 30 s.
        result = await srv.tool_watch_meters(ctx, duration_s=600.0, threshold_db=-200.0)
    assert result["duration_s"] == srv.WATCH_MAX_DURATION_S
    assert result["triggered"] is True
    assert result["polls"] == 1


async def test_watch_meters_needs_channels(mock_factory: Any, base_fixture: Tree) -> None:
    engine = await mock_factory(base_fixture)
    async with context(engine) as ctx:
        result = await srv.tool_watch_meters(ctx, kind="aux", targets=[], duration_s=0.1)
        assert result["ok"] is True
        broken = await srv.tool_watch_meters(ctx, targets=["nonexistent"], duration_s=0.1)
    assert broken["ok"] is False
    assert "No input matches" in broken["error"]


# -------------------------------------------------------------------------------- snapshots


async def test_snapshot_save_list_diff_restore_through_the_tool(
    mock: MockEngine, snapshot_dir: Path
) -> None:
    async with context(mock) as ctx:
        saved = await srv.tool_snapshot(ctx, action="save", name="session-a")
        assert saved["ok"] is True
        assert saved["entries"] > 1000
        assert saved["devices"] == ["Apollo x8 Gen 2"]
        assert (snapshot_dir / "session-a.json").exists()

        listed = await srv.tool_snapshot(ctx, action="list")
        assert [item["name"] for item in listed["snapshots"]] == ["session-a"]

        await srv.tool_set_channels(ctx, [{"target": "ANALOG 1", "fader_db": -30.0}])
        diffed = await srv.tool_snapshot(ctx, action="diff", name="session-a")
        assert diffed["changed"] == 1
        assert diffed["changes"][0]["path"] == f"{IN0}/FaderLevel"

        ignored = await srv.tool_snapshot(
            ctx, action="diff", name="session-a", ignore=["*/FaderLevel"]
        )
        assert ignored["changed"] == 0

        before = mock.set_count()
        dry = await srv.tool_snapshot(ctx, action="restore", name="session-a")
        assert dry["dry_run"] is True
        assert mock.set_count() == before
        assert [write["path"] for write in dry["writes"]] == [f"{IN0}/FaderLevel"]

        applied = await srv.tool_snapshot(
            ctx, action="restore", name="session-a", dry_run=False
        )
    assert applied["counts"]["applied"] == 1
    assert applied["pre_restore_snapshot"] is not None
    assert mock.value(f"{IN0}/FaderLevel") == pytest.approx(-0.2466, abs=1e-3)


async def test_snapshot_cross_model_refusal_is_actionable(
    mock_factory: Any, base_fixture: Tree, snapshot_dir: Path
) -> None:
    x8 = await mock_factory(base_fixture)
    twin = await mock_factory(twin_fixture(base_fixture))
    async with context(x8) as ctx:
        await srv.tool_snapshot(ctx, action="save", name="x8")
    async with context(twin) as twin_ctx:
        result = await srv.tool_snapshot(
            twin_ctx, action="restore", name="x8", dry_run=False
        )
    assert result["ok"] is False
    assert "allow_cross_model=True" in result["error"]
    assert twin.set_count() == 0


async def test_default_snapshot_names_are_utc(
    mock: MockEngine, snapshot_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m8: the auto-generated name must agree with the document's own UTC `created` field.

    The process timezone is forced away from UTC for the duration, so this cannot pass by
    accident on a UTC machine.
    """
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    monkeypatch.setattr(time, "tzset", lambda: None, raising=False)
    try:
        assert time.strftime("%H", time.localtime()) != time.strftime("%H", time.gmtime())
        async with context(mock) as ctx:
            before = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
            saved = await srv.tool_snapshot(ctx, action="save")
            after = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    finally:
        monkeypatch.undo()
        time.tzset()
    stamp = saved["name"].removeprefix("snapshot-")
    assert before <= stamp <= after, f"{saved['name']} is not a UTC timestamp"
    doc = snap.SnapshotStore(snapshot_dir).load(saved["name"])
    created = doc["created"].replace("-", "").replace(":", "").replace("Z", "")
    assert created[:8] == stamp[:8]
    assert created[9:13] == stamp[9:13], f"{doc['created']} and {saved['name']} disagree"


async def test_snapshot_bad_action_and_missing_name(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        bad = await srv.tool_snapshot(ctx, action="destroy")
        nameless = await srv.tool_snapshot(ctx, action="diff")
        missing = await srv.tool_snapshot(ctx, action="restore", name="never-saved")
    assert "Use save, list, diff or restore" in bad["error"]
    assert "needs the name of a saved snapshot" in nameless["error"]
    assert "No snapshot named" in missing["error"]


# --------------------------------------------------------------------------------- raw + full


async def test_raw_get_reads_properties_and_nodes(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        prop = await srv.tool_raw_get(ctx, f"{IN0}/FaderLevel")
        node = await srv.tool_raw_get(ctx, f"{IN0}/meters")
        missing = await srv.tool_raw_get(ctx, "/nope")
    assert prop["kind"] == "property"
    assert prop["min"] == -144.0 and prop["max"] == 12.0
    assert node["kind"] == "node"
    assert node["children"] == ["0", "1"]
    assert missing["ok"] is False
    assert "Unable to resolve path" in missing["error"]


async def test_raw_set_is_refused_outside_full_mode(mock: MockEngine) -> None:
    async with context(mock, "safe") as ctx:
        result = await srv.tool_raw_set(ctx, "/Tempo", 100.0)
    assert result["ok"] is False
    assert "UAD_MCP_MODE=full" in result["error"]
    assert mock.set_count() == 0


async def test_full_mode_raw_set_writes_but_keeps_the_denylist(mock: MockEngine) -> None:
    async with context(mock, "full") as ctx:
        tempo = await srv.tool_raw_set(ctx, "/Tempo", 100.0)
        denied = await srv.tool_raw_set(ctx, "/devices/0/DeviceHwID", 7)
        scenes = await srv.tool_raw_set(ctx, "/pluginscenes/0/Name", "x")
    assert tempo["ok"] is True
    assert mock.value("/Tempo") == 100.0
    assert denied["ok"] is False and "denylist" in denied["error"]
    assert scenes["ok"] is False and "/pluginscenes" in scenes["error"]


async def test_full_mode_still_rails_the_monitor(mock: MockEngine) -> None:
    async with context(mock, "full") as ctx:
        result = await srv.tool_raw_set(ctx, MONITOR_LEVEL, 0.0)
    assert result["ok"] is False
    assert "ceiling" in result["error"]
    assert mock.value(MONITOR_LEVEL) == -22.0


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (f"{MONITOR}/CRMonitorLevelTapered", 1.0),
        (f"{MONITOR}/SurroundTrimLevel", 10.0),
        (f"{MONITOR_MUTE}/value", False),
        (f"/{MONITOR_MUTE}", False),
    ],
)
async def test_full_mode_has_no_way_around_the_monitor_rail(
    mock: MockEngine, path: str, value: Any
) -> None:
    """Audit H5: other spellings and other level properties must not dodge the rail."""
    async with context(mock, "full") as ctx:
        await mock.push(MONITOR_LEVEL, -2.0)
        await mock.push(MONITOR_MUTE, True)
        result = await srv.tool_raw_set(ctx, path, value)
    assert result["ok"] is False, path
    assert mock.value(f"{MONITOR}/CRMonitorLevelTapered") == pytest.approx(0.3541666567325592)
    assert mock.value(f"{MONITOR}/SurroundTrimLevel") == 0.0
    assert mock.value(MONITOR_MUTE) is True


async def test_raw_set_refuses_dot_segments(mock: MockEngine) -> None:
    async with context(mock, "full") as ctx:
        result = await srv.tool_raw_set(ctx, "/devices/../pluginscenes/0/Name", "x")
    assert result["ok"] is False and "'..'" in result["error"]
    assert mock.set_count() == 0


async def test_raw_set_talkback_on_arms_the_auto_off_timer(mock: MockEngine) -> None:
    """Audit H5: raw_set must not be a way to open talkback with no timer behind it."""
    async with context(mock, "full", talkback_auto_off_s=0.05) as ctx:
        result = await srv.tool_raw_set(ctx, "/TalkbackOn", True)
        assert result["ok"] is True
        assert "auto-off armed" in " ".join(result["writes"][0]["warnings"])
        assert await wait_until(lambda: mock.value("/TalkbackOn") is False)


@pytest.mark.parametrize("prop", ["Mute", "DimOn"])
async def test_raw_set_rails_a_monitor_release_against_the_stored_level(
    mock: MockEngine, prop: str
) -> None:
    """B1: uad_raw_set can reach the monitor directly, so it must find the rail too."""
    async with context(mock, "full") as ctx:
        await mock.push(MONITOR_LEVEL, -2.0)
        await mock.push(f"{MONITOR}/{prop}", True)
        result = await srv.tool_raw_set(ctx, f"{MONITOR}/{prop}", False)
    assert result["ok"] is False
    assert "UAD_MCP_MAX_MONITOR_DB" in result["error"]
    assert mock.value(f"{MONITOR}/{prop}") is True


@pytest.mark.parametrize("prop", ["Mute", "DimOn"])
async def test_raw_set_refuses_a_monitor_release_when_the_level_read_fails(
    mock: MockEngine, prop: str
) -> None:
    """B1: an unreadable level is a refusal, not a free pass."""
    async with context(mock, "full") as ctx:
        await mock.push(f"{MONITOR}/{prop}", True)
        await mock.push(MONITOR_LEVEL, None)
        result = await srv.tool_raw_set(ctx, f"{MONITOR}/{prop}", False)
    assert result["ok"] is False
    assert "CRMonitorLevel" in result["error"]
    assert mock.value(f"{MONITOR}/{prop}") is True


async def test_raw_set_refuses_a_monitor_release_when_the_level_never_answers(
    mock_factory: Any, base_fixture: Tree
) -> None:
    """The reviewer's case: the sibling read times out, so the rail cannot be evaluated."""
    engine = await mock_factory(base_fixture, never_reply_on={MONITOR_LEVEL})
    async with context(engine, "full") as ctx:
        await engine.push(MONITOR_MUTE, True)
        result = await srv.tool_raw_set(ctx, MONITOR_MUTE, False)
    assert result["ok"] is False
    assert "CRMonitorLevel" in result["error"]
    assert engine.value(MONITOR_MUTE) is True


@pytest.mark.parametrize("spelling", [False, 0, "no", "n", "off", "false"])
async def test_raw_set_rails_every_spelling_of_a_monitor_release(
    mock: MockEngine, spelling: Any
) -> None:
    """B1: the gate coerces "n" and 0 to False, so the rail must recognise them as releases."""
    async with context(mock, "full") as ctx:
        await mock.push(MONITOR_LEVEL, -2.0)
        await mock.push(MONITOR_MUTE, True)
        result = await srv.tool_raw_set(ctx, MONITOR_MUTE, spelling)
    assert result["ok"] is False, f"{spelling!r} slipped past the monitor rail"
    assert "UAD_MCP_MAX_MONITOR_DB" in result["error"]
    assert mock.value(MONITOR_MUTE) is True


async def test_raw_set_can_lower_the_dim_attenuation_when_it_is_safe(mock: MockEngine) -> None:
    """Full mode must stay usable: with dim off, the attenuation is not in circuit at all."""
    async with context(mock, "full") as ctx:
        allowed = await srv.tool_raw_set(ctx, DIM_ATTENUATION, 0)
        assert allowed["ok"] is True, allowed
        assert mock.value(DIM_ATTENUATION) == 0

        await mock.push(DIM_ATTENUATION, 17)
        await mock.push(MONITOR_DIM, True)
        await mock.push(MONITOR_LEVEL, 0.0)
        refused = await srv.tool_raw_set(ctx, DIM_ATTENUATION, 0)
    assert refused["ok"] is False, "dimmed, with a hot stored level, this is an un-dim"
    assert "UAD_MCP_MAX_MONITOR_DB" in refused["error"]
    assert mock.value(DIM_ATTENUATION) == 17


async def test_raw_set_leaves_ordinary_outputs_alone(mock: MockEngine) -> None:
    """An output with no CRMonitorLevel is not the control room: is_monitor stays False."""
    headphones = "/devices/0/outputs/23"
    mock.nodes[headphones]["properties"]["Mute"] = {"type": "bool", "value": True}
    async with context(mock, "full") as ctx:
        result = await srv.tool_raw_set(ctx, f"{headphones}/Mute", False)
    assert result["ok"] is True, result
    assert mock.value(f"{headphones}/Mute") is False


async def test_raw_set_does_not_hunt_for_a_monitor_level_on_inputs(mock: MockEngine) -> None:
    async with context(mock, "full") as ctx:
        await mock.push(f"{IN0}/Mute", True)
        before = len(mock.sent_frames)
        result = await srv.tool_raw_set(ctx, f"{IN0}/Mute", False)
    assert result["ok"] is True
    frames = mock.sent_frames[before:]
    assert not [frame for frame in frames if "CRMonitorLevel" in frame]


# ------------------------------------------------------------------------------- readonly


async def test_readonly_mode_puts_zero_sets_on_the_wire(
    mock: MockEngine, monkeypatch: pytest.MonkeyPatch, snapshot_dir: Path
) -> None:
    """SPEC §4.7: exercise EVERY write tool in readonly mode; ``set_count()`` must stay 0."""
    monkeypatch.setattr(snap, "MATERIALIZE_TIMEOUT_S", 0.1)
    monkeypatch.setattr(snap, "MATERIALIZE_POLL_S", 0.05)
    # A generous token bucket: refusals spend tokens too, and the point here is that EVERY tool
    # is refused for being in readonly mode, not that some of them ran out of rate limit.
    readonly = SafetyGate("readonly", writes_per_second=500.0, write_burst=500)
    async with context(mock, gate=readonly) as ctx:
        # A snapshot to restore, and engine-side drift so the restore really has work to do.
        await srv.tool_snapshot(ctx, action="save", name="ro")
        await mock.push(f"{IN0}/Name", "SCRAMBLED")
        await mock.push(f"{IN0}/FaderLevel", -30.0)
        await mock.push(MONITOR_LEVEL, -40.0)
        assert mock.set_count() == 0

        results = {
            "set_channels": await srv.tool_set_channels(
                ctx, [{"target": "ANALOG 1", "fader_db": -6.0, "mute": True, "name": "X"}]
            ),
            "set_preamp": await srv.tool_set_preamp(
                ctx, "ANALOG 1", gain_db=30.0, phantom_48v=True, confirm_condenser_mic=True
            ),
            "set_sends": await srv.tool_set_sends(
                ctx, [{"target": "ANALOG 1", "send": "cue1", "level_db": -6.0}]
            ),
            "set_monitor": await srv.tool_set_monitor(
                ctx, level_db=-30.0, dim=False, mute=False, mono=True, alt_speaker=1
            ),
            "talkback": await srv.tool_talkback(ctx, on=True, level=50, auto_off_s=0.05),
            "safe_state": await srv.tool_safe_state(ctx),
            "set_clock": await srv.tool_set_clock(ctx, "ADAT", confirm=True),
            "set_sample_rate": await srv.tool_set_sample_rate(ctx, 96000, confirm=True),
            "load_plugin": await srv.tool_load_plugin(ctx, "ANALOG 1", LA3A, slot=1),
            "remove_plugin": await srv.tool_remove_plugin(ctx, "VIRTUAL 1", slot=1),
            "set_plugin_params": await srv.tool_set_plugin_params(
                ctx, "VIRTUAL 1", [{"name": "Gain", "normalized": 0.9}], slot=1
            ),
            "plugin_preset": await srv.tool_plugin_preset(ctx, "VIRTUAL 1", "Default", slot=1),
            "raw_set": await srv.tool_raw_set(ctx, "/Tempo", 100.0),
            "restore": await srv.tool_snapshot(
                ctx, action="restore", name="ro", dry_run=False, include_monitor=True
            ),
        }
        # Give the talkback auto-off timer a chance to misbehave, if it were ever armed.
        await asyncio.sleep(0.15)

    assert mock.set_count() == 0, "readonly mode must never put a set frame on the wire"
    assert results["talkback"]["auto_off_armed"] is False
    assert results["restore"]["counts"]["applied"] == 0
    assert results["restore"]["counts"]["failed"] >= 3
    refused = [name for name, result in results.items() if result.get("ok") is False]
    assert set(refused) == {
        "set_channels",
        "set_preamp",
        "set_sends",
        "set_monitor",
        "talkback",
        "safe_state",
        "set_clock",
        "set_sample_rate",
        "load_plugin",
        "remove_plugin",
        "set_plugin_params",
        "plugin_preset",
        "raw_set",
    }
    for name in ("set_clock", "remove_plugin", "raw_set"):
        assert "UAD_MCP_MODE" in str(results[name]), name


async def test_readonly_mode_still_reads_everything(mock: MockEngine) -> None:
    async with context(mock, "readonly") as ctx:
        assert (await srv.tool_get_status(ctx))["safety"]["mode"] == "readonly"
        assert (await srv.tool_list_channels(ctx))["total"] == X8_INPUTS
        assert (await srv.tool_peek_meters(ctx))["meters"]
        assert (await srv.tool_get_inserts(ctx))["inserts"]
        assert (await srv.tool_list_plugins(ctx))["installed"] == 215
    assert mock.set_count() == 0


# ------------------------------------------------------------------------- caching + reconnect


async def test_topology_is_cached_until_the_ttl_expires(mock: MockEngine) -> None:
    now = [1000.0]
    async with context(mock, rig_ttl_s=30.0, clock=lambda: now[0]) as ctx:
        await ctx.rig()
        await ctx.rig()
        assert ctx.discoveries == 1
        now[0] += 31.0
        assert ctx.topology_stale is True
        await ctx.rig()
        assert ctx.discoveries == 2
        await ctx.rig(refresh=True)
        assert ctx.discoveries == 3


async def test_a_walk_invalidated_mid_flight_is_discarded_and_redone(
    mock: MockEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m7: a reconnect during the walk must not be papered over by a stale timestamp."""
    now = [1000.0]
    async with context(mock, rig_ttl_s=30.0, clock=lambda: now[0]) as ctx:
        real_discover = srv.discover
        calls: list[int] = []

        async def racing_discover(client: Any) -> Any:
            calls.append(1)
            rig = await real_discover(client)
            if len(calls) == 1:
                # As if the control socket reconnected while we were still walking.
                ctx.invalidate()
            return rig

        monkeypatch.setattr(srv, "discover", racing_discover)
        rig = await ctx.rig()

        assert len(calls) == 2, "the invalidated walk must be thrown away and re-walked"
        assert ctx.discoveries == 2
        assert len(rig.channels) == X8_CHANNELS
        assert ctx.topology_stale is False, "the surviving walk is cached, not left stale"
        # And the cache really is usable: no further walk while inside the TTL.
        await ctx.rig()
        assert len(calls) == 2


async def test_a_walk_that_never_settles_is_served_but_never_cached(
    mock: MockEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m7: if every attempt is invalidated, do not hand out stale topology for a full TTL."""
    now = [1000.0]
    async with context(mock, rig_ttl_s=30.0, clock=lambda: now[0]) as ctx:
        real_discover = srv.discover
        calls: list[int] = []

        async def always_racing(client: Any) -> Any:
            calls.append(1)
            rig = await real_discover(client)
            ctx.invalidate()
            return rig

        monkeypatch.setattr(srv, "discover", always_racing)
        rig = await ctx.rig()
        assert len(calls) == srv.RIG_WALK_ATTEMPTS
        assert len(rig.channels) == X8_CHANNELS, "the caller still gets a usable topology"
        assert ctx.topology_stale is True, "but it must not be served again from cache"
        await ctx.rig()
        assert len(calls) == 2 * srv.RIG_WALK_ATTEMPTS


async def test_the_rig_timestamp_is_stamped_after_the_walk(
    mock: MockEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m7: a slow walk must not come back already half-expired."""
    now = [1000.0]
    async with context(mock, rig_ttl_s=30.0, clock=lambda: now[0]) as ctx:
        real_discover = srv.discover

        async def slow_discover(client: Any) -> Any:
            now[0] += 25.0  # the walk itself took 25 s
            return await real_discover(client)

        monkeypatch.setattr(srv, "discover", slow_discover)
        await ctx.rig()
        assert ctx.topology_stale is False
        now[0] += 20.0
        assert ctx.topology_stale is False, "20 s after the walk finished it is still fresh"
        now[0] += 11.0
        assert ctx.topology_stale is True


async def test_concurrent_callers_share_one_discovery(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        rigs = await asyncio.gather(*(ctx.rig() for _ in range(6)))
    assert ctx.discoveries == 1
    assert all(rig is rigs[0] for rig in rigs)


async def test_reconnect_rediscovers_a_changed_channel_count(
    mock_factory: Any, base_fixture: Tree
) -> None:
    """SPEC §4.7: pending ops fail, the next op reconnects, and discovery re-runs."""
    # 70 frames is enough for a full x8 discovery (about 62 control reads) and then a drop.
    engine = await mock_factory(base_fixture, drop_after=70)
    async with context(engine) as ctx:
        rig = await ctx.rig()
        assert len(rig.channels) == X8_CHANNELS
        assert ctx.discoveries == 1

        failure: dict[str, Any] | None = None
        for _ in range(20):
            result = await srv.tool_raw_get(ctx, f"{IN0}/Name")
            if result["ok"] is False:
                failure = result
                break
        assert failure is not None, "the mock never dropped the connection"
        assert "UA Mixer Engine not reachable" in failure["error"]
        assert "UAD Console" in failure["error"], "the error must say how to fix it"

        # The engine is now a different interface entirely.
        engine.load_fixture(twin_fixture(base_fixture))
        assert await wait_until(lambda: ctx.client.connected)
        assert await wait_until(lambda: ctx.topology_stale), "reconnect must drop the cache"

        status = await srv.tool_get_status(ctx)
        rig = await ctx.rig()
    assert ctx.discoveries == 2, "the topology must be walked again, not served from cache"
    assert status["devices"][0]["type_id"] == "Apollo Twin X"
    assert status["devices"][0]["channels"] == {"input": 6, "aux": 2, "output": 1}
    assert len(rig.channels) == TWIN_CHANNELS
    assert rig.monitor_output().index == 0, "the twin re-indexes its monitor output"


async def test_pending_operations_fail_together_on_a_drop(
    mock_factory: Any, base_fixture: Tree
) -> None:
    engine = await mock_factory(base_fixture, drop_after=1)
    async with context(engine) as ctx:
        results = await asyncio.gather(
            srv.tool_raw_get(ctx, f"{IN0}/Name"),
            srv.tool_raw_get(ctx, f"{IN0}/Mute"),
            srv.tool_list_channels(ctx),
        )
    assert all(result["ok"] is False for result in results)
    assert all("UA Mixer Engine not reachable" in result["error"] for result in results)


async def test_plugin_catalog_is_cached_and_invalidated(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        first = await ctx.plugin_catalog()
        assert await ctx.plugin_catalog() is first
        ctx.invalidate()
        assert await ctx.plugin_catalog() is not first


# ------------------------------------------------------------------------------ error surface


async def test_connection_failure_text_names_the_engine_and_the_fix(
    mock_factory: Any, base_fixture: Tree
) -> None:
    engine = await mock_factory(base_fixture)
    port = engine.port
    await engine.stop()
    client = UAClient(port=port, connect_timeout=0.5)
    ctx = srv.ServerContext(client, SafetyGate("safe"), snapshots=snap.SnapshotStore())
    try:
        result = await srv.tool_get_status(ctx)
    finally:
        await ctx.aclose()
        await client.aclose()
    assert result["ok"] is False
    assert f"not reachable on 127.0.0.1:{port}" in result["error"]
    assert "is the UAD software installed and running?" in result["error"]


def test_error_text_never_leaks_a_type_name_for_known_errors() -> None:
    assert srv.error_text(snap.SnapshotError("no such thing")) == "no such thing"
    assert "Traceback" not in srv.error_text(RuntimeError("boom"))
    assert srv.error_text(RuntimeError("boom")) == "RuntimeError: boom"


async def test_a_tool_that_raises_becomes_a_sentence(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.tool_get_plugin_params(ctx, "ANALOG 1", slot="banana")
    assert result["ok"] is False
    assert "slot must be an insert slot number or 'unison'" in result["error"]


# ------------------------------------------------------------------------------ registration


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("readonly", 21), ("safe", 21), ("full", 22)],
)
async def test_raw_set_is_registered_only_in_full_mode(
    mock: MockEngine, mode: str, expected: int
) -> None:
    async with context(mock, mode) as ctx:
        server = srv.build_server(ctx)
        names = [tool.name for tool in await server.list_tools()]
    assert len(names) == expected
    assert ("uad_raw_set" in names) is (mode == "full")
    assert names[0] == "uad_get_status"
    assert all(name.startswith("uad_") for name in names)


async def test_every_tool_description_says_when_to_use_it(mock: MockEngine) -> None:
    async with context(mock, "full") as ctx:
        tools = await srv.build_server(ctx).list_tools()
    for tool in tools:
        description = tool.description or ""
        first_line = description.splitlines()[0]
        assert first_line and first_line[0].isupper(), tool.name
        assert "Use when:" in description, tool.name
        assert "Not for:" in description, tool.name


async def test_resources_and_prompts_are_registered(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        server = srv.build_server(ctx)
        resources = [str(resource.uri) for resource in await server.list_resources()]
        prompts = [prompt.name for prompt in await server.list_prompts()]
        limitations = await server.read_resource("uad://limitations")
        state = await server.read_resource("uad://state")
    assert resources == ["uad://state", "uad://channels", "uad://limitations"]
    assert prompts == [
        "setup_vocal_tracking",
        "onboard_adat_expander",
        "troubleshoot_clock",
        "panic_reset",
    ]
    assert "Headphone output level cannot be set" in str(limitations[0].content)
    assert '"sample_rate": 48000' in str(state[0].content)


async def test_a_registered_tool_can_be_called_through_mcp(mock: MockEngine) -> None:
    async with context(mock) as ctx:
        result = await srv.build_server(ctx).call_tool("uad_list_channels", {"kind": "aux"})
    payload = getattr(result, "structuredContent", None) or {}
    assert payload.get("total") == 2 or "AUX 1" in str(result)


async def test_the_clock_prompt_encodes_the_real_diagnosis(mock: MockEngine) -> None:
    """SPEC §4.5: the clock prompt must carry the facts, not just a list of tools."""
    body = srv.TROUBLESHOOT_CLOCK
    assert "-77 dB" in body and "dead digital link" in body
    assert "CANNOT tell silence from a dead link" in body
    assert "Exactly one device in the chain may be the clock master" in body
    assert "ADAT return cable" in body and "word clock" in body
    assert "sample rate" in body and "S/MUX" in body
    assert "confirm=true" in body and "-60 dB" in body
    for prompt in (srv.SETUP_VOCAL_TRACKING, srv.ONBOARD_ADAT_EXPANDER, srv.PANIC_RESET):
        assert "uad_get_status" in prompt
    assert "48V" in srv.SETUP_VOCAL_TRACKING and "ribbon" in srv.SETUP_VOCAL_TRACKING
    assert "uad_safe_state" in srv.PANIC_RESET
    assert "ADAT OUT" in srv.ONBOARD_ADAT_EXPANDER


# ------------------------------------------------------------------------------------- cli


def test_build_parser_defaults_to_serving() -> None:
    args = cli.build_parser().parse_args([])
    assert args.check is False
    assert args.dump_tree is None
    assert (args.host, args.port) == ("127.0.0.1", 4710)


def test_dump_tree_takes_an_optional_file() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["--dump-tree"]).dump_tree == cli.DUMP_STDOUT
    assert parser.parse_args(["--dump-tree", "out.json"]).dump_tree == "out.json"


def test_version_exits_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        cli.main(["--version"])
    assert caught.value.code == 0
    assert "uad-console-mcp" in capsys.readouterr().out


def test_check_and_dump_tree_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        cli.main(["--check", "--dump-tree"])


def test_sanitize_scrubs_the_home_directory() -> None:
    payload = {
        "nodes": {
            "/x": {"properties": {"Preset": {"value": "/Users/someone/Music/UAD/My Preset"}}},
            "/y": ["/Users/someone", 42, True, None],
        }
    }
    cleaned = cli.sanitize(payload, "/Users/someone")
    assert cleaned["nodes"]["/x"]["properties"]["Preset"]["value"] == "~/Music/UAD/My Preset"
    assert cleaned["nodes"]["/y"] == ["~", 42, True, None]


def test_sanitize_redacts_hardware_identity_values() -> None:
    """B3: a dumped tree is meant to be shareable, so serials and firmware do not travel."""
    payload = {
        "nodes": {
            "/": {
                "properties": {
                    "SerialNumber": {"type": "string", "value": "24492087001847"},
                    "Version": {"type": "string", "value": "11.7.1"},
                }
            },
            "/devices/0": {
                "properties": {
                    "DeviceHwID": {"type": "int64", "value": 987654321},
                    "FirmwareVersion": {"type": "string", "value": "5.4.3", "default": "0.0.0"},
                    "BootloaderVersion": {"type": "string", "value": "1.2"},
                    "DeviceName": {"type": "string", "value": "Apollo x8 Gen 2"},
                }
            },
        }
    }
    cleaned = cli.sanitize(payload, "/Users/someone")
    root = cleaned["nodes"]["/"]["properties"]
    device = cleaned["nodes"]["/devices/0"]["properties"]
    assert root["SerialNumber"] == {"type": "string", "value": "0"}
    assert device["DeviceHwID"] == {"type": "int64", "value": 0}
    assert device["FirmwareVersion"] == {"type": "string", "value": "0", "default": "0"}
    assert device["BootloaderVersion"]["value"] == "0"
    # Everything that is not hardware identity survives untouched.
    assert root["Version"]["value"] == "11.7.1"
    assert device["DeviceName"]["value"] == "Apollo x8 Gen 2"


def test_the_committed_fixture_carries_no_real_serial_number(base_fixture: Tree) -> None:
    """B3: the fixture is public. Its serial must be blanked, and the property kept."""
    assert base_fixture["nodes"]["/"]["properties"]["SerialNumber"] == {
        "type": "string",
        "value": "0",
    }
    assert "24492087001847" not in json.dumps(base_fixture)


def test_an_unusable_safety_mode_is_a_configuration_error_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """m2: exit code 2 and one sentence on stderr, for every CLI mode."""
    monkeypatch.setenv("UAD_MCP_MODE", "yolo")
    monkeypatch.setattr(cli, "serve", _refuse_to_serve)
    monkeypatch.setattr(cli, "_run_check", _refuse_to_connect)
    for argv in ([], ["--check"], ["--dump-tree", "-"]):
        assert cli.main(argv) == cli.CONFIG_ERROR_EXIT, argv
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "UAD_MCP_MODE='yolo' is not a valid safety mode" in captured.err
    assert "readonly, safe, full" in captured.err


def test_a_bad_numeric_limit_is_also_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("UAD_MCP_MAX_MONITOR_DB", "loud")
    monkeypatch.setattr(cli, "serve", _refuse_to_serve)
    assert cli.main([]) == cli.CONFIG_ERROR_EXIT
    assert "UAD_MCP_MAX_MONITOR_DB='loud' is not a number" in capsys.readouterr().err


def _refuse_to_serve(host: str, port: int) -> int:  # pragma: no cover - guard only
    raise AssertionError("a broken configuration must never reach serve()")


async def test_doctor_reports_a_healthy_rig(mock: MockEngine) -> None:
    client = UAClient(port=mock.port)
    try:
        code, lines = await cli.doctor(client)
    finally:
        await client.aclose()
    report = "\n".join(lines)
    assert code == 0
    assert "engine:        reachable" in report
    assert "UAD version 11.7.1" in report
    assert "clock:         Internal, locked=True" in report
    assert "sample rate:   48000 Hz" in report
    assert "Apollo x8 Gen 2 (Apollo x8 Gen 2) online: 28 inputs, 2 auxs, 26 outputs" in report
    assert "monitor out:   MONITOR at /devices/0/outputs/22" in report
    assert "plugins:       215 installed" in report
    assert "safety mode:   safe (from UAD_MCP_MODE)" in report
    assert "raw set tool:  not registered" in report


async def test_doctor_flags_an_offline_device(mock_factory: Any, base_fixture: Tree) -> None:
    engine = await mock_factory(base_fixture)
    await engine.push("/devices/0/DeviceOnline", False)
    client = UAClient(port=engine.port)
    try:
        code, lines = await cli.doctor(client)
    finally:
        await client.aclose()
    assert code == 1
    assert any("OFFLINE" in line for line in lines)


async def test_doctor_explains_an_unreachable_engine(
    mock_factory: Any, base_fixture: Tree
) -> None:
    engine = await mock_factory(base_fixture)
    port = engine.port
    await engine.stop()
    client = UAClient(port=port, connect_timeout=0.5)
    try:
        code, lines = await cli.doctor(client)
    finally:
        await client.aclose()
    report = "\n".join(lines)
    assert code == 1
    assert "UNREACHABLE" in report
    assert "is the UAD software installed and running?" in report
    assert "safety mode:" in report


async def test_dump_tree_walks_and_sanitizes(
    mock: MockEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli.Path, "home", classmethod(lambda cls: Path("/Users/someone"))  # type: ignore[arg-type]
    )
    await mock.push(f"{IN0}/Name", "/Users/someone/secret")
    await mock.push("/SerialNumber", "24492087001847")  # as a real engine would report it
    client = UAClient(port=mock.port, tree_timeout=30.0)
    try:
        document = await cli.dump_tree(client)
    finally:
        await client.aclose()
    assert document["_meta"]["node_count"] == len(document["nodes"]) == 1941
    assert document["nodes"][IN0]["properties"]["Name"]["value"] == "~/secret"
    assert "children" in document["nodes"]["/devices/0"]
    # B3: a dumped tree is a fixture people paste into issues. No serial travels with it.
    assert document["nodes"]["/"]["properties"]["SerialNumber"]["value"] == "0"
    assert "24492087001847" not in json.dumps(document)


def test_run_dump_writes_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    document = {"_meta": {"node_count": 1}, "nodes": {"/": {"properties": {}, "children": []}}}

    async def fake_walk(host: str, port: int) -> dict[str, Any]:
        assert (host, port) == ("127.0.0.1", 9)
        return document

    monkeypatch.setattr(cli, "_walk_for_dump", fake_walk)
    target = tmp_path / "nested" / "tree.json"
    assert cli.run_dump("127.0.0.1", 9, str(target)) == 0
    assert "wrote 1 nodes" in capsys.readouterr().err
    assert '"nodes"' in target.read_text(encoding="utf-8")


def test_run_dump_reports_an_unreachable_engine(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_walk(host: str, port: int) -> dict[str, Any]:
        raise UAConnectError("nothing listening")

    monkeypatch.setattr(cli, "_walk_for_dump", fake_walk)
    assert cli.run_dump("127.0.0.1", 9, cli.DUMP_STDOUT) == 1
    assert "is the UAD software installed and running?" in capsys.readouterr().err


def test_serve_is_not_invoked_by_the_other_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A safety net: no CLI path may accidentally open a socket to the real engine."""
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(cli, "serve", lambda host, port: calls.append((host, port)) or 0)
    monkeypatch.setattr(cli, "_run_check", _refuse_to_connect)
    monkeypatch.setattr(cli, "run_dump", lambda *args: 0)
    assert cli.main(["--dump-tree", "-"]) == 0
    assert calls == []
    assert cli.main([]) == 0
    assert calls == [("127.0.0.1", 4710)]


async def _refuse_to_connect(host: str, port: int) -> int:  # pragma: no cover - guard only
    raise AssertionError("no test may connect to the real engine")


def test_logging_goes_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    cli._configure_logging("DEBUG")
    with contextlib.suppress(Exception):
        srv.logger.warning("a warning that must not reach stdout")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "must not reach stdout" in captured.err


async def test_rename_updates_cached_topology_immediately(mock: MockEngine) -> None:
    """A verified rename must be resolvable by its new name at once, without waiting
    out the rig TTL or paying for a re-discovery."""
    async with context(mock) as ctx:
        first = await srv.tool_set_channels(ctx, [{"target": "ANALOG 1", "name": "LEAD VOX"}])
        assert first["ok"] is True
        walked = ctx.discoveries
        second = await srv.tool_set_channels(ctx, [{"target": "LEAD VOX", "fader_db": -3.0}])
        assert second["ok"] is True, second
        assert second["results"][0]["resolved"]["name"] == "LEAD VOX"
        assert ctx.discoveries == walked


async def test_set_sends_warns_on_phantom_cue_bus(mock: MockEngine) -> None:
    """The x8 exposes cue3/cue4 send nodes but reports CueBusCount=2: a write there must
    verify AND carry a warning that the bus does not reach any real output."""
    async with context(mock) as ctx:
        result = await srv.tool_set_sends(
            ctx, [{"target": "ANALOG 1", "send": "cue3", "level_db": -20.0}]
        )
    item = result["results"][0]
    assert item["ok"] is True
    assert any("does not reach any real output" in w for w in item["writes"][0]["warnings"])
    async with context(mock) as ctx:
        real_result = await srv.tool_set_sends(
            ctx, [{"target": "ANALOG 1", "send": "cue1", "level_db": -20.0}]
        )
    assert real_result["results"][0]["writes"][0]["warnings"] == []


async def test_auto_off_failure_is_not_alarmed_when_talkback_never_opened(
    mock: MockEngine,
) -> None:
    """If every auto-off write fails but TalkbackOn already reads false (the mic never
    actually opened), the failure flag must stay down: it is the one flag an operator
    has to be able to trust."""
    async with context(mock) as ctx:
        ctx.talkback_retry_delays_s = (0.0,)

        async def failing_write(*args: object, **kwargs: object) -> None:
            raise UAConnectError("socket dropped")

        ctx.write = failing_write  # type: ignore[method-assign]
        assert mock.value("/TalkbackOn") is False
        await ctx._talkback_auto_off(0.0)
        assert ctx.talkback_auto_off_failed is False

        await mock.push("/TalkbackOn", True)
        await ctx._talkback_auto_off(0.0)
        assert ctx.talkback_auto_off_failed is True
