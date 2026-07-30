"""60 s random-call soak (docs/SPEC.md §4.7).

Marked ``slow`` and excluded from the default run (``pyproject.toml`` addopts). Run it with::

    uv run pytest tests/test_soak.py -m slow -q

It hammers a ``MockEngine`` with a seeded random mix of READ and WRITE tool calls for a wall-clock
minute and asserts three things that no single-purpose test can:

* no unhandled exception ever escapes a tool - every call returns a dict, and every failure is an
  actionable sentence rather than a Python type name;
* the monitor rail holds on the WIRE for the whole run: no call may raise what the control room
  hears above the ceiling, even though the "physical knob" keeps being turned up engine-side;
* nothing leaks - no ``uad-*`` asyncio task and no engine-side socket survives the session.

Nothing here touches 127.0.0.1:4710. ``UAD_MCP_SOAK_SECONDS`` shortens the run while iterating.
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
for _path in (REPO_ROOT, SRC_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from tests.mock_engine import MockEngine  # noqa: E402
from uad_console_mcp import server as srv  # noqa: E402
from uad_console_mcp import snapshot as snap  # noqa: E402
from uad_console_mcp.client import UAClient  # noqa: E402
from uad_console_mcp.safety import SafetyGate  # noqa: E402

pytestmark = pytest.mark.slow

SOAK_SECONDS = float(os.environ.get("UAD_MCP_SOAK_SECONDS", "60"))
SOAK_SEED = 20260729

MONITOR = "/devices/0/outputs/22"
MONITOR_LEVEL = f"{MONITOR}/CRMonitorLevel"
MONITOR_MUTE = f"{MONITOR}/Mute"
MONITOR_DIM = f"{MONITOR}/DimOn"
DIM_ATTENUATION = "/devices/0/DimAttenuation"
IN0 = "/devices/0/inputs/0"

#: Error prefixes that mean a Python exception escaped instead of a handled refusal.
BUG_PREFIXES = (
    "AttributeError",
    "KeyError",
    "TypeError",
    "IndexError",
    "RuntimeError",
    "AssertionError",
    "NameError",
    "UnboundLocalError",
    "ZeroDivisionError",
    "StopIteration",
)

Call = Callable[[srv.ServerContext], Awaitable[dict[str, Any]]]


@asynccontextmanager
async def soak_context(engine: MockEngine) -> AsyncIterator[srv.ServerContext]:
    """A server context wired to the mock, with a token bucket wide enough to keep writing.

    The monitor rail itself is left at its defaults - that is the invariant under test. Only the
    write RATE limit is raised, because a soak spent being rate-limited proves nothing.
    """
    assert engine.port not in (0, MockEngine.REAL_ENGINE_PORT), "must be an ephemeral mock port"
    client = UAClient(port=engine.port)
    ctx = srv.ServerContext(
        client,
        SafetyGate("full", writes_per_second=2000.0, write_burst=2000),
        snapshots=snap.SnapshotStore(snap.snapshot_dir()),
        clock_verify_delay_s=0.01,
        talkback_retry_delays_s=(0.01, 0.01, 0.01),
    )
    try:
        yield ctx
    finally:
        await ctx.aclose()
        await client.aclose()


def exposure(engine: MockEngine) -> float | None:
    """The level the control room is actually exposed to, or None while it is muted."""
    if engine.value(MONITOR_MUTE):
        return None
    level = engine.value(MONITOR_LEVEL)
    if not isinstance(level, int | float):
        return None
    attenuation = engine.value(DIM_ATTENUATION) if engine.value(MONITOR_DIM) else 0.0
    return float(level) - float(attenuation or 0.0)


def build_calls(rng: random.Random) -> list[tuple[str, Call]]:
    """The call mix. Reads and writes, weighted by repetition, all bounded in duration."""
    inputs = [0, 1, 2, 3, 18, 19]
    plugins = ["UAD Teletronix LA-3A", "Pultec EQP-1A Legacy", "UA 1176LN Rev E"]

    calls: list[tuple[str, Call]] = [
        # ---------------------------------------------------------------- reads
        ("get_status", lambda ctx: srv.tool_get_status(ctx)),
        (
            "list_channels",
            lambda ctx: srv.tool_list_channels(
                ctx, kind=rng.choice(["input", "aux", "output", "all"])
            ),
        ),
        ("peek_meters", lambda ctx: srv.tool_peek_meters(ctx)),
        (
            "watch_meters",
            lambda ctx: srv.tool_watch_meters(ctx, duration_s=0.2, interval_s=0.1),
        ),
        ("get_inserts", lambda ctx: srv.tool_get_inserts(ctx, target=rng.choice(inputs))),
        ("list_plugins", lambda ctx: srv.tool_list_plugins(ctx, search=rng.choice(["la", "eq"]))),
        (
            "get_plugin_params",
            lambda ctx: srv.tool_get_plugin_params(
                ctx, rng.choice(inputs), slot=rng.choice([0, 1])
            ),
        ),
        (
            "raw_get",
            lambda ctx: srv.tool_raw_get(
                ctx, rng.choice([MONITOR_LEVEL, f"{IN0}/Name", "/ClockSource", f"{IN0}/meters"])
            ),
        ),
        ("snapshot_list", lambda ctx: srv.tool_snapshot(ctx, action="list")),
        # --------------------------------------------------------------- writes
        (
            "set_channels",
            lambda ctx: srv.tool_set_channels(
                ctx,
                [
                    {
                        "target": rng.choice(inputs),
                        "fader_db": rng.uniform(-90.0, 12.0),
                        "mute": rng.choice([True, False]),
                        "pan": rng.uniform(-1.0, 1.0),
                    }
                ],
            ),
        ),
        (
            "set_channels_monitor",
            lambda ctx: srv.tool_set_channels(
                ctx,
                [
                    {
                        "target": "MONITOR",
                        "kind": "output",
                        "mute": rng.choice([True, False]),
                    }
                ],
            ),
        ),
        (
            "set_preamp",
            lambda ctx: srv.tool_set_preamp(
                ctx,
                rng.choice([0, 1, 2, 3]),
                gain_db=rng.uniform(0.0, 65.0),
                pad=rng.choice([True, False]),
                low_cut=rng.choice([True, False]),
                phantom_48v=rng.choice([True, False, None]),
                confirm_condenser_mic=rng.choice([True, False]),
            ),
        ),
        (
            "set_sends",
            lambda ctx: srv.tool_set_sends(
                ctx,
                [
                    {
                        "target": rng.choice(inputs),
                        "send": rng.choice(["cue1", "aux1", "cue4", "nonsense"]),
                        "level_db": rng.uniform(-60.0, 6.0),
                    }
                ],
            ),
        ),
        (
            "set_monitor_level",
            lambda ctx: srv.tool_set_monitor(ctx, level_db=rng.uniform(-90.0, 6.0)),
        ),
        ("set_monitor_unmute", lambda ctx: srv.tool_set_monitor(ctx, mute=False)),
        ("set_monitor_mute", lambda ctx: srv.tool_set_monitor(ctx, mute=True)),
        ("set_monitor_undim", lambda ctx: srv.tool_set_monitor(ctx, dim=False)),
        ("set_monitor_dim", lambda ctx: srv.tool_set_monitor(ctx, dim=True)),
        (
            "set_monitor_dim_attenuation",
            lambda ctx: srv.tool_set_monitor(ctx, dim_attenuation_db=rng.randint(0, 60)),
        ),
        (
            "set_monitor_combo",
            lambda ctx: srv.tool_set_monitor(
                ctx,
                level_db=rng.uniform(-90.0, 6.0),
                mute=rng.choice([True, False]),
                dim=rng.choice([True, False]),
                mono=rng.choice([True, False]),
                dim_attenuation_db=rng.randint(0, 60),
                alt_speaker=rng.randint(0, 2),
            ),
        ),
        (
            "talkback",
            lambda ctx: srv.tool_talkback(
                ctx,
                on=rng.choice([True, False]),
                level=rng.randint(0, 100),
                auto_off_s=rng.choice([0.05, 0.5]),
            ),
        ),
        ("safe_state", lambda ctx: srv.tool_safe_state(ctx)),
        (
            "set_clock",
            lambda ctx: srv.tool_set_clock(
                ctx,
                rng.choice(["Internal", "ADAT", "S/PDIF", "Word Clock"]),
                confirm=rng.choice([True, False]),
            ),
        ),
        (
            "set_sample_rate",
            lambda ctx: srv.tool_set_sample_rate(
                ctx, rng.choice([44100, 48000, 96000, 12345]), confirm=rng.choice([True, False])
            ),
        ),
        (
            "load_plugin",
            lambda ctx: srv.tool_load_plugin(
                ctx,
                rng.choice(inputs),
                rng.choice(plugins),
                slot=rng.choice([0, 1, "unison"]),
            ),
        ),
        (
            "remove_plugin",
            lambda ctx: srv.tool_remove_plugin(ctx, rng.choice(inputs), slot=rng.choice([0, 1])),
        ),
        (
            "set_plugin_params",
            lambda ctx: srv.tool_set_plugin_params(
                ctx,
                rng.choice(inputs),
                [{"name": rng.choice(["Gain", "Peak Reduction", "nope"]), "normalized": 0.5}],
                slot=rng.choice([0, 1]),
            ),
        ),
        (
            "plugin_preset",
            lambda ctx: srv.tool_plugin_preset(
                ctx, rng.choice(inputs), rng.choice(["Default", "nope"]), slot=rng.choice([0, 1])
            ),
        ),
        (
            # Curated paths only: raw_set is the escape hatch, not a fuzzer for the whole tree.
            "raw_set",
            lambda ctx: srv.tool_raw_set(
                ctx,
                *rng.choice(
                    [
                        (MONITOR_MUTE, False),
                        (MONITOR_MUTE, True),
                        (MONITOR_DIM, False),
                        (MONITOR_DIM, True),
                        (MONITOR_LEVEL, rng.uniform(-90.0, 0.0)),
                        (DIM_ATTENUATION, rng.randint(0, 60)),
                        (f"{IN0}/FaderLevel", rng.uniform(-90.0, 12.0)),
                        ("/Tempo", rng.uniform(40.0, 200.0)),
                    ]
                ),
            ),
        ),
    ]
    return calls


async def test_random_call_soak_holds_the_monitor_rail_and_leaks_nothing(
    mock: MockEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(snap.ENV_SNAPSHOT_DIR, str(tmp_path / "snapshots"))
    monkeypatch.setattr(snap, "MATERIALIZE_TIMEOUT_S", 0.1)
    monkeypatch.setattr(snap, "MATERIALIZE_POLL_S", 0.05)

    rng = random.Random(SOAK_SEED)
    calls = build_calls(rng)
    loop = asyncio.get_running_loop()
    tasks_before = {id(task) for task in asyncio.all_tasks()}

    performed = 0
    ceiling = 0.0
    tally: dict[str, int] = {}
    async with soak_context(mock) as ctx:
        ceiling = ctx.gate.max_monitor_db
        # Captured BEFORE the random mix, so the real restore at the end has to undo a whole
        # session's worth of drift rather than writing back a state nothing has touched.
        baseline = await srv.tool_snapshot(ctx, action="save", name="soak-baseline")
        assert baseline["ok"] is True, baseline
        deadline = loop.time() + SOAK_SECONDS
        while loop.time() < deadline:
            if performed % 17 == 0:
                # The physical panel, meddled with behind the server's back: the stored level goes
                # above the ceiling while dim keeps the room quiet. Every release path is now one
                # write away from an above-ceiling control room.
                await mock.push(MONITOR_LEVEL, rng.uniform(-4.0, 0.0))
                await mock.push(MONITOR_MUTE, False)
                await mock.push(MONITOR_DIM, True)
                await mock.push(DIM_ATTENUATION, 17)

            name, call = rng.choice(calls)
            before = exposure(mock)
            result = await call(ctx)
            performed += 1
            tally[name] = tally.get(name, 0) + 1

            assert isinstance(result, dict), f"{name} returned {type(result).__name__}"
            assert "ok" in result, f"{name} returned no ok flag: {result}"
            if result["ok"] is False:
                messages = [str(result.get("error", ""))]
                messages += [str(item) for item in result.get("errors", [])]
                for item in result.get("results", []):
                    if isinstance(item, dict):
                        messages.append(str(item.get("error", "")))
                        messages += [str(error) for error in item.get("errors", [])]
                assert any(message.strip() for message in messages), f"{name}: silent failure"
                for message in messages:
                    assert "Traceback" not in message, f"{name}: {message}"
                    assert not message.startswith(BUG_PREFIXES), f"{name} leaked a bug: {message}"

            after = exposure(mock)
            if after is not None and (before is None or after > before + 1e-9):
                assert after <= ceiling + 1e-9, (
                    f"call {performed} ({name}) raised the control-room exposure from {before} "
                    f"to {after} dB, past the {ceiling} dB ceiling: {result}"
                )

        # Snapshots are slow and stateful, so they run at the end rather than in the mix.
        for index in range(2):
            saved = await srv.tool_snapshot(ctx, action="save", name=f"soak-{index}")
            assert saved["ok"] is True, saved
            assert (await srv.tool_snapshot(ctx, action="diff", name=f"soak-{index}"))["ok"]
            dry = await srv.tool_snapshot(ctx, action="restore", name=f"soak-{index}")
            assert dry["ok"] is True, dry

        drift = await srv.tool_snapshot(ctx, action="diff", name="soak-baseline")
        assert drift["changed"] > 20, f"the soak barely changed anything: {drift['changed']}"
        before_restore = exposure(mock)
        real = await srv.tool_snapshot(
            ctx, action="restore", name="soak-baseline", dry_run=False, include_monitor=True
        )
        assert real["ok"] is True, real
        assert real["counts"]["applied"] > 10, real["counts"]
        after_restore = exposure(mock)
        if after_restore is not None and (
            before_restore is None or after_restore > before_restore + 1e-9
        ):
            assert after_restore <= ceiling + 1e-9, f"the restore opened the room up: {real}"

    assert performed > 200, f"only {performed} calls in {SOAK_SECONDS}s; the soak did nothing"
    missed = sorted({name for name, _ in calls} - set(tally))
    assert not missed, f"never exercised: {missed}"

    # Nothing may outlive the session: no talkback timer, no reader task, no socket.
    leaked = [
        task
        for task in asyncio.all_tasks()
        if id(task) not in tasks_before
        and not task.done()
        and str(task.get_name()).startswith("uad-")
    ]
    assert leaked == [], f"leaked tasks: {[task.get_name() for task in leaked]}"
    for _ in range(100):
        if mock.client_count == 0:
            break
        await asyncio.sleep(0.02)
    assert mock.client_count == 0, "the engine still sees an open client socket"
