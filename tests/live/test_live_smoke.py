"""Live smoke tests against a REAL UA Mixer Engine and Apollo.

Run explicitly with:  UAD_MCP_LIVE=1 uv run pytest tests/live -m live -q

Every write is reverted. Nothing here changes monitor volume, mutes anything,
or touches the clock. Skipped entirely unless UAD_MCP_LIVE=1.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator

import pytest

from uad_console_mcp.client import UAClient
from uad_console_mcp.safety import SafetyRefusal
from uad_console_mcp.server import (
    ServerContext,
    tool_get_inserts,
    tool_get_plugin_params,
    tool_get_status,
    tool_list_channels,
    tool_list_plugins,
    tool_load_plugin,
    tool_peek_meters,
    tool_raw_get,
    tool_remove_plugin,
    tool_set_channels,
    tool_set_monitor,
    tool_set_plugin_params,
    tool_set_preamp,
    tool_snapshot,
    tool_watch_meters,
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("UAD_MCP_LIVE") != "1", reason="UAD_MCP_LIVE!=1"),
]

SCRATCH_CHANNEL = "VIRTUAL 8"  # inactive on the reference rig; safe for plugin cycling


@pytest.fixture
async def ctx(tmp_path, monkeypatch) -> AsyncIterator[ServerContext]:
    monkeypatch.setenv("UAD_MCP_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    from uad_console_mcp.snapshot import SnapshotStore

    client = UAClient()
    context = ServerContext(client, snapshots=SnapshotStore())
    yield context
    await context.aclose()
    await client.aclose()


async def test_status_and_channels(ctx: ServerContext) -> None:
    status = await tool_get_status(ctx)
    assert status["ok"] is True
    assert status["engine"]["connected"] is True
    assert any(d["online"] for d in status["devices"])
    assert status["clock"]["locked"] is True

    everything = await tool_list_channels(ctx, kind="all")
    names = [c["name"] for dev in everything["devices"] for c in dev["channels"]]
    assert any(n.startswith("UG ") for n in names), "expected the UG expander labels"
    monitors = [
        c for dev in everything["devices"] for c in dev["channels"] if c.get("is_monitor")
    ]
    assert len(monitors) >= 1
    assert all(c["monitor_level_db"] is not None for c in monitors)


async def test_rename_and_revert(ctx: ServerContext) -> None:
    original, temp = "UG 8", "UG 8 SMOKE"
    res = await tool_set_channels(ctx, [{"target": original, "name": temp}])
    item = res["results"][0]
    assert item["ok"] is True, item
    assert item["writes"][0]["verified"] is True
    try:
        assert item["writes"][0]["after"] == temp
    finally:
        back = await tool_set_channels(ctx, [{"target": temp, "name": original}])
        assert back["results"][0]["writes"][0]["after"] == original


async def test_preamp_noop_roundtrip(ctx: ServerContext) -> None:
    raw = await tool_raw_get(ctx, "/devices/0/inputs/2/preamps/0/Gain")
    current = raw["value"]
    res = await tool_set_preamp(ctx, target="ANALOG 3", gain_db=current)
    assert res["ok"] is True, res
    assert all(w["verified"] for w in res["writes"])


async def test_monitor_rail_refuses_above_ceiling(ctx: ServerContext) -> None:
    # Ceiling defaults to -6 dB; asking for -3 dB must refuse and change nothing.
    monitor_path = (await ctx.rig()).monitor_output().path
    before = (await tool_raw_get(ctx, f"{monitor_path}/CRMonitorLevel"))["value"]
    refused = False
    try:
        res = await tool_set_monitor(ctx, level_db=-3.0)
        refused = res["ok"] is False and "UAD_MCP_MAX_MONITOR_DB" in json.dumps(
            res.get("errors", [])
        )
    except SafetyRefusal as exc:
        refused = "UAD_MCP_MAX_MONITOR_DB" in str(exc)
    assert refused, "above-ceiling level was not refused"
    after = (await tool_raw_get(ctx, f"{monitor_path}/CRMonitorLevel"))["value"]
    assert after == before


async def test_monitor_noop_write_passes_rail(ctx: ServerContext) -> None:
    monitor_path = (await ctx.rig()).monitor_output().path
    current = float(
        (await tool_raw_get(ctx, f"{monitor_path}/CRMonitorLevel"))["value"]
    )
    res = await tool_set_monitor(ctx, level_db=current)
    assert res["ok"] is True, res
    assert res["stored_level_db"] == pytest.approx(current, abs=0.5)


async def test_plugin_load_params_remove(ctx: ServerContext) -> None:
    catalog = await tool_list_plugins(ctx, limit=250)
    authorized = [p for p in catalog["plugins"] if p["authorized"] and not p["unison"]]
    assert authorized, "no authorized non-Unison plugins on this rig"
    name = authorized[0]["name"]
    try:
        loaded = await tool_load_plugin(ctx, target=SCRATCH_CHANNEL, plugin=name, slot=0)
        assert json.dumps(loaded), loaded  # shape varies; must at least serialize
        params = await tool_get_plugin_params(ctx, target=SCRATCH_CHANNEL, slot=0)
        assert params["plugin"], params
        if params["parameters"]:
            first = params["parameters"][0]
            res = await tool_set_plugin_params(
                ctx,
                target=SCRATCH_CHANNEL,
                slot=0,
                params=[{"name": first["name"], "normalized": first["normalized"]}],
            )
            assert res["ok"] is True, res
    finally:
        removed = await tool_remove_plugin(ctx, target=SCRATCH_CHANNEL, slot=0)
        assert removed["writes"][0]["verified"] is True, removed
    inserts = await tool_get_inserts(ctx, target=SCRATCH_CHANNEL, loaded_only=True)
    assert name not in json.dumps(inserts["inserts"])


async def test_meters_and_snapshot_cycle(ctx: ServerContext) -> None:
    peek = await tool_peek_meters(ctx)
    assert peek["meters"], peek
    watch = await tool_watch_meters(ctx, duration_s=2.0, threshold_db=0.0)
    assert watch["elapsed_s"] <= 4.0
    assert watch["channels_watched"] > 0

    saved = await tool_snapshot(ctx, action="save", name="live-smoke")
    assert saved["entries"] > 100, saved
    diff = await tool_snapshot(ctx, action="diff", name="live-smoke")
    assert diff["changed"] < 5, diff  # racing a human at the desk is allowed
    restore = await tool_snapshot(ctx, action="restore", name="live-smoke", dry_run=True)
    assert restore["action"] == "restore"
