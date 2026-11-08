"""Transport-layer tests for :mod:`uad_console_mcp.client`.

Every test here runs against ``tests.mock_engine.MockEngine`` bound to an ephemeral
port (``port=0``). Nothing in this file may ever connect to 127.0.0.1:4710 -- that is
a real UA Mixer Engine driving real hardware.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import functools
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from tests.mock_engine import MockEngine
from uad_console_mcp.client import (
    UAClient,
    UAConnectError,
    UAError,
    UAPathError,
    UAProperty,
    UATimeoutError,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "apollo_x8_gen2_tree.json"

ROOT = "/"
DEV = "/devices/0"
IN8 = "/devices/0/inputs/8"
IN8_NAME = f"{IN8}/Name"
IN8_FADER = f"{IN8}/FaderLevel"
IN8_MUTE = f"{IN8}/Mute"
IN8_METERS = f"{IN8}/meters/0"
IN8_METER_LEVEL = f"{IN8_METERS}/MeterLevel"
PREAMP = "/devices/0/inputs/0/preamps/0"
MONITOR_OUT = "/devices/0/outputs/22"

METER_PATHS = [f"/devices/0/inputs/{i}/meters/0/MeterLevel" for i in range(8)]

# A spread of scalar properties across the tree, used for interleaving assertions.
SCALAR_PATHS = [
    f"{ROOT}SampleRate",
    f"{ROOT}ClockSource",
    f"{ROOT}ClockLocked",
    f"{DEV}/DeviceName",
    f"{DEV}/CueBusCount",
    IN8_NAME,
    IN8_FADER,
    IN8_MUTE,
    f"{IN8}/IOType",
    f"{IN8}/Pan",
    f"{IN8}/sends/0/Gain",
    f"{PREAMP}/Gain",
    f"{PREAMP}/48V",
    f"{MONITOR_OUT}/CRMonitorLevel",
    IN8_METER_LEVEL,
    "/plugins/0/Name",
]


@functools.lru_cache(maxsize=1)
def _fixture_source() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def fixture() -> dict[str, Any]:
    """The x8 Gen 2 capture. MockEngine deep-copies it, so handing out the cache is safe."""
    return _fixture_source()


def mutable_fixture() -> dict[str, Any]:
    """A private deep copy, for tests that doctor the tree before starting an engine."""
    return copy.deepcopy(_fixture_source())


def fixture_meta(path: str) -> dict[str, Any]:
    """Raw property metadata straight out of the fixture, for expectation building."""
    node, _, prop = path.rpartition("/")
    node = node or "/"
    return _fixture_source()["nodes"][node]["properties"][prop]


@contextlib.asynccontextmanager
async def engine(**kwargs: Any) -> AsyncIterator[tuple[MockEngine, UAClient]]:
    """Start a MockEngine on an ephemeral port with a connected client attached."""
    client_kwargs = {
        key: kwargs.pop(key)
        for key in ("connect_timeout", "scalar_timeout", "tree_timeout")
        if key in kwargs
    }
    mock = MockEngine(fixture(), port=0, **kwargs)
    port = await mock.start()
    assert port != 4710, "MockEngine must never bind the real engine's port"
    client = UAClient("127.0.0.1", port, **client_kwargs)
    try:
        await client.connect()
        yield mock, client
    finally:
        await client.aclose()
        await mock.stop()


async def wait_until(predicate: Callable[[], bool], limit: float = 3.0) -> bool:
    """Poll ``predicate`` until true or ``limit`` seconds elapse (asyncio only, no sleeps)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


def frame_parts(frame: str) -> tuple[str, str]:
    """``"set /a/b/value 1"`` -> ``("set", "/a/b/value")``."""
    bits = frame.split(" ", 2)
    return bits[0], (bits[1] if len(bits) > 1 else "")


# --------------------------------------------------------------------------- reads


async def test_get_node_returns_properties_and_children() -> None:
    async with engine() as (_mock, client):
        properties, children = await client.get_node(IN8)

        assert isinstance(properties["Name"], UAProperty)
        assert properties["Name"].path == IN8_NAME
        assert properties["Name"].type == "string"
        assert properties["Name"].value == "UG 1"
        assert properties["Name"].default == "ADAT 1"

        fader = properties["FaderLevel"]
        assert (fader.type, fader.min, fader.max, fader.default) == ("float", -144.0, 12.0, 0.0)
        assert fader.readonly is False  # absent in the reply -> False

        assert sorted(children) == ["effects", "meters", "sends"]
        # Container nodes describe children by name only, never with values inline.
        assert all(isinstance(child, str) for child in children)


async def test_get_node_on_root_and_leaf() -> None:
    async with engine() as (_mock, client):
        properties, children = await client.get_node(ROOT)
        assert children == ["devices", "inputroutes", "outputroutes", "plugins", "pluginscenes"]
        assert properties["SampleRate"].path == "/SampleRate"

        meter_props, meter_children = await client.get_node(IN8_METERS)
        assert meter_children == []
        assert set(meter_props) == {"Type", "MeterLevel", "MeterPeakLevel", "MeterClip"}


async def test_get_prop_metadata_shapes() -> None:
    """Ground parsing against the real reply shapes seen on the x8 Gen 2."""
    async with engine() as (_mock, client):
        # enum as list of {value, string, enabled} objects, int-typed
        sample_rate = await client.get_prop("/SampleRate")
        assert sample_rate.type == "int"
        assert sample_rate.value == 48000
        assert sample_rate.values is not None
        assert sample_rate.values[0] == {"value": 44100, "string": "44.1 kHz", "enabled": True}

        # enum as a plain list of scalars
        clock = await client.get_prop("/ClockSource")
        assert clock.values == ["Internal", "Word Clock", "ADAT", "S/PDIF"]

        # readonly present + int (not float) min/max preserved verbatim
        cue = await client.get_prop(f"{DEV}/CueBusCount")
        assert cue.readonly is True
        assert (cue.min, cue.max) == (0, 4)
        assert isinstance(cue.min, int)

        # bool with no min/max/values at all
        mute = await client.get_prop(IN8_MUTE)
        assert (mute.type, mute.value, mute.min, mute.max, mute.values) == (
            "bool",
            False,
            None,
            None,
            None,
        )

        # int64 readonly
        hw_id = await client.get_prop(f"{DEV}/DeviceHwID")
        assert (hw_id.type, hw_id.readonly) == ("int64", True)

        # property metadata with no "value" key at all (13 of these on the fixture)
        aux_gain = await client.get_prop("/devices/0/auxs/0/Gain")
        assert aux_gain.value is None
        assert (aux_gain.min, aux_gain.max) == (-144.0, 65.0)


async def test_get_value_shapes() -> None:
    async with engine() as (_mock, client):
        assert await client.get_value(IN8_NAME) == "UG 1"
        assert await client.get_value(IN8_FADER) == 0.0
        assert await client.get_value(IN8_MUTE) is False
        assert await client.get_value("/SampleRate") == 48000
        assert await client.get_value(IN8_METER_LEVEL) == pytest.approx(-77.0, abs=1e-3)


async def test_paths_are_normalized() -> None:
    async with engine() as (mock, client):
        assert await client.get_value(f"{IN8_NAME}/") == "UG 1"
        assert await client.get_value(IN8_NAME.lstrip("/")) == "UG 1"
        assert all(frame_parts(f)[1] == IN8_NAME for f in mock.sent_frames)


# --------------------------------------------------------------------------- errors


async def test_engine_error_reply_raises_path_error() -> None:
    async with engine() as (_mock, client):
        with pytest.raises(UAPathError) as excinfo:
            await client.get_prop("/bogus")
        assert "/bogus" in str(excinfo.value)
        assert "resolve" in str(excinfo.value).lower()

        with pytest.raises(UAPathError):
            await client.get_node("/devices/0/inputs/999")

        # An error reply must not poison the connection.
        assert await client.get_value(IN8_NAME) == "UG 1"
        assert client.connected


async def test_node_property_confusion_raises_ua_error() -> None:
    async with engine() as (_mock, client):
        with pytest.raises(UAError) as excinfo:
            await client.get_prop(IN8)
        assert "not a scalar property" in str(excinfo.value)

        with pytest.raises(UAError) as excinfo:
            await client.get_node(IN8_NAME)
        assert "not a node" in str(excinfo.value)


async def test_timeout_when_engine_never_replies() -> None:
    async with engine(never_reply_on={IN8_NAME}, scalar_timeout=0.25) as (_mock, client):
        with pytest.raises(UATimeoutError) as excinfo:
            await client.get_value(IN8_NAME)
        assert IN8_NAME in str(excinfo.value)

        # The abandoned waiter must be gone: the next read still dispatches correctly.
        assert client.connected
        assert await client.get_value(IN8_FADER) == 0.0
        assert await client.get_value(IN8_MUTE) is False


async def test_set_timeout_does_not_wedge_the_set_lock() -> None:
    value_path = f"{IN8_NAME}/value"
    async with engine(never_reply_on={value_path}, scalar_timeout=0.25) as (_mock, client):
        with pytest.raises(UATimeoutError):
            await client.set_value(IN8_NAME, "Nope")
        # Lock released -> another set still goes through.
        assert await client.set_value(IN8_FADER, -6.0) == -6.0


# --------------------------------------------------------------------------- writes


async def test_set_value_acks_then_verifies_with_a_get() -> None:
    async with engine() as (mock, client):
        assert await client.set_value(IN8_FADER, -6.0) == -6.0
        assert await client.get_value(IN8_FADER) == -6.0
        assert mock.set_count() == 1

        verbs = [frame_parts(frame) for frame in mock.sent_frames]
        assert verbs[0] == ("set", f"{IN8_FADER}/value")
        # ALWAYS verified with a follow-up get of the property itself.
        assert verbs[1] == ("get", IN8_FADER)
        assert mock.sent_frames[0] == f"set {IN8_FADER}/value -6.0"


async def test_set_value_json_encodes_by_type() -> None:
    async with engine() as (mock, client):
        assert await client.set_value(IN8_NAME, "UG 1 vox") == "UG 1 vox"
        assert await client.set_value(IN8_MUTE, True) is True
        assert await client.set_value("/SampleRate", 96000) == 96000

        sent = [frame for frame in mock.sent_frames if frame.startswith("set ")]
        assert sent == [
            f'set {IN8_NAME}/value "UG 1 vox"',
            f"set {IN8_MUTE}/value true",
            "set /SampleRate/value 96000",
        ]


async def test_set_value_on_bad_path_surfaces_error() -> None:
    async with engine(scalar_timeout=0.5) as (_mock, client):
        # SPEC §1 verifies the error shape for `get`; a `set` to an unresolvable path
        # either errors the same way or is dropped. Both are UAError, never a hang.
        with pytest.raises(UAError) as excinfo:
            await client.set_value("/bogus/Nope", 1)
        assert isinstance(excinfo.value, UAPathError | UATimeoutError)
        assert await client.get_value(IN8_NAME) == "UG 1"


async def test_client_does_not_block_readonly_writes() -> None:
    """The transport is NOT a write barrier (safety.py is). Mirrors the engine quirk."""
    async with engine() as (mock, client):
        prop = await client.get_prop(f"{DEV}/CueBusCount")
        assert prop.readonly is True
        assert await client.set_value(f"{DEV}/CueBusCount", 3) == 3
        assert mock.set_count() == 1


async def test_sets_are_serialized_across_concurrent_callers() -> None:
    targets = [f"/devices/0/inputs/{i}/FaderLevel" for i in range(6)]
    async with engine() as (mock, client):
        results = await asyncio.gather(
            *(client.set_value(path, -12.0) for path in targets)
        )
        assert results == [-12.0] * len(targets)
        assert mock.set_count() == len(targets)

        parsed = [frame_parts(frame) for frame in mock.sent_frames]
        set_indexes = [i for i, (verb, _) in enumerate(parsed) if verb == "set"]
        assert len(set_indexes) == len(targets)
        # Each set must be fully done (ack + verifying get) before the next one starts.
        for start, next_start in zip(set_indexes, set_indexes[1:], strict=False):
            prop_path = parsed[start][1].removesuffix("/value")
            verifies = [
                i
                for i, (verb, path) in enumerate(parsed)
                if verb == "get" and path == prop_path and i > start
            ]
            assert verifies, f"no verifying get after {parsed[start]}"
            assert verifies[0] < next_start, "a set interleaved with another set"


# --------------------------------------------------------------------------- framing


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param({}, id="plain"),
        pytest.param({"split_frames": True}, id="split_frames"),
        pytest.param({"batch_frames": True}, id="batch_frames"),
        pytest.param({"meter_noise_hz": 200.0}, id="meter_noise"),
        pytest.param({"reply_delay": 0.01}, id="reply_delay"),
    ],
)
async def test_framing_survives_adversarial_transports(mode: dict[str, Any]) -> None:
    async with engine(scalar_timeout=10.0, **mode) as (_mock, client):
        # Small node so 1-3 byte chunking stays quick.
        properties, children = await client.get_node(IN8_METERS)
        assert children == []
        assert properties["MeterLevel"].min == -77.0

        assert await client.get_value(IN8_NAME) == "UG 1"
        assert await client.get_value(IN8_MUTE) is False

        # Multi-byte UTF-8 must survive being split mid-sequence across reads.
        multibyte = "Vöcal 日本 ✓ mic"
        assert await client.set_value(IN8_NAME, multibyte) == multibyte
        assert await client.get_value(IN8_NAME) == multibyte


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param({}, id="plain"),
        pytest.param({"batch_frames": True}, id="batch_frames"),
        pytest.param({"meter_noise_hz": 300.0}, id="meter_noise"),
    ],
)
async def test_concurrent_gets_are_dispatched_to_the_right_waiter(
    mode: dict[str, Any],
) -> None:
    expected = {path: fixture_meta(path).get("value") for path in SCALAR_PATHS}
    async with engine(scalar_timeout=10.0, **mode) as (_mock, client):
        for _ in range(3):
            props = await asyncio.gather(*(client.get_prop(path) for path in SCALAR_PATHS))
            got = {prop.path: prop.value for prop in props}
            assert got == expected


async def test_meter_noise_between_request_and_reply_is_dropped() -> None:
    """Unsubscribed broadcasts must not satisfy waiters and must not raise."""
    async with engine(meter_noise_hz=400.0, scalar_timeout=10.0) as (_mock, client):
        for _ in range(25):
            assert await client.get_value(IN8_FADER) == 0.0
        assert client.connected


# --------------------------------------------------------------------------- polling


async def test_poll_values_batch_on_the_poll_socket() -> None:
    async with engine() as (_mock, client):
        control_before = client._control.frames_sent
        values = await client.poll_values(METER_PATHS)

        assert set(values) == set(METER_PATHS)
        assert all(v == pytest.approx(-77.0, abs=1e-3) for v in values.values())
        # Bulk reads go on the SECOND socket so they never contend with control.
        assert client._poll.frames_sent == len(METER_PATHS)
        assert client._control.frames_sent == control_before


async def test_poll_values_edges() -> None:
    async with engine(scalar_timeout=1.0) as (_mock, client):
        assert await client.poll_values([]) == {}

        # duplicates collapse to one request each
        values = await client.poll_values([IN8_FADER, IN8_FADER, IN8_MUTE])
        assert values == {IN8_FADER: 0.0, IN8_MUTE: False}
        assert client._poll.frames_sent == 2

        # unresolvable paths are omitted, good ones still returned
        values = await client.poll_values(["/bogus", IN8_NAME])
        assert values == {IN8_NAME: "UG 1"}


async def test_poll_values_raises_when_nothing_answers() -> None:
    async with engine(never_reply_on={IN8_FADER, IN8_MUTE}, scalar_timeout=0.25) as (
        _mock,
        client,
    ):
        with pytest.raises(UATimeoutError):
            await client.poll_values([IN8_FADER, IN8_MUTE])
        # Partial answers are fine: the answered path comes back.
        assert await client.poll_values([IN8_FADER, IN8_NAME]) == {IN8_NAME: "UG 1"}


# --------------------------------------------------------------------- subscriptions


async def test_subscribe_pushes_initial_value_then_changes() -> None:
    async with engine() as (mock, client):
        seen: list[Any] = []
        await client.subscribe(IN8_FADER, seen.append)

        assert await wait_until(lambda: seen == [0.0]), f"no initial push, got {seen}"
        assert mock.sent_frames[0] == f"subscribe {IN8_FADER}"

        # A different client changes the value; the subscriber gets a push.
        other = UAClient("127.0.0.1", client.port)
        try:
            assert await other.set_value(IN8_FADER, -3.0) == -3.0
            assert await wait_until(lambda: -3.0 in seen), f"no change push, got {seen}"

            await client.unsubscribe(IN8_FADER)
            assert await other.set_value(IN8_FADER, -9.0) == -9.0
            await asyncio.sleep(0.2)
            assert -9.0 not in seen
        finally:
            await other.aclose()

        assert f"unsubscribe {IN8_FADER}" in mock.sent_frames


async def test_subscription_callback_exceptions_are_contained() -> None:
    async with engine() as (_mock, client):
        seen: list[Any] = []

        def boom(value: Any) -> None:
            seen.append(value)
            raise RuntimeError("callback blew up")

        await client.subscribe(IN8_MUTE, boom)
        assert await wait_until(lambda: len(seen) == 1)
        # The reader task must survive a bad callback.
        assert client.connected
        assert await client.get_value(IN8_NAME) == "UG 1"


# ------------------------------------------------------------------------ reconnect


async def test_pending_operations_fail_with_connect_error_on_drop() -> None:
    mock = MockEngine(fixture(), port=0, drop_after=1)
    port = await mock.start()
    client = UAClient("127.0.0.1", port, scalar_timeout=2.0)
    try:
        results = await asyncio.gather(
            client.get_value(IN8_NAME),
            client.get_value(IN8_FADER),
            client.get_value(IN8_MUTE),
            return_exceptions=True,
        )
        assert len(results) == 3
        assert all(isinstance(result, UAConnectError) for result in results), results
        assert client.connected is False
    finally:
        await client.aclose()
        await mock.stop()


async def test_next_operation_reconnects_and_fires_callbacks() -> None:
    mock = MockEngine(fixture(), port=0, drop_after=2)
    port = await mock.start()
    client = UAClient("127.0.0.1", port, scalar_timeout=2.0)
    reconnects: list[int] = []

    async def on_reconnect() -> None:
        reconnects.append(1)

    client.on_reconnect(on_reconnect)
    try:
        failure: UAConnectError | None = None
        for _ in range(4):
            try:
                await client.get_value(IN8_NAME)
            except UAConnectError as exc:
                failure = exc
                assert client.connected is False
                break
        assert failure is not None, "the mock never dropped the connection"

        # The next operation transparently reconnects and works.
        assert await client.get_value(IN8_NAME) == "UG 1"
        assert client.connected
        assert await wait_until(lambda: bool(reconnects)), "on_reconnect never fired"
    finally:
        await client.aclose()
        await mock.stop()


async def test_subscriptions_are_rearmed_after_reconnect() -> None:
    mock = MockEngine(fixture(), port=0, drop_after=3)
    port = await mock.start()
    client = UAClient("127.0.0.1", port, scalar_timeout=2.0)
    seen: list[Any] = []
    try:
        await client.subscribe(IN8_FADER, seen.append)
        assert await wait_until(lambda: len(seen) == 1)

        with contextlib.suppress(UAConnectError):
            for _ in range(4):
                await client.get_value(IN8_NAME)
        assert await wait_until(lambda: client.connected, limit=5.0)

        # The re-subscribe replays an initial push on the fresh socket.
        rearmed = await wait_until(lambda: len(seen) >= 2, limit=5.0)
        assert rearmed, f"subscription not re-armed: {seen}"
        assert mock.sent_frames.count(f"subscribe {IN8_FADER}") >= 2
    finally:
        await client.aclose()
        await mock.stop()


# ----------------------------------------------------------------------------- walk


async def test_walk_returns_the_whole_fixture_tree() -> None:
    async with engine(tree_timeout=90.0) as (_mock, client):
        progress: list[int] = []
        nodes = await client.walk(ROOT, progress=progress.append)

        source = _fixture_source()["nodes"]
        assert len(nodes) == _fixture_source()["_meta"]["node_count"] == 1941
        assert set(nodes) == set(source)

        # fixture format: raw metadata dicts + children as a list of names
        node = nodes[IN8]
        assert node["properties"]["FaderLevel"] == {
            "type": "float",
            "min": -144.0,
            "max": 12.0,
            "default": 0.0,
            "value": 0.0,
        }
        assert sorted(node["children"]) == ["effects", "meters", "sends"]
        assert nodes[IN8_METERS]["children"] == []

        assert progress, "progress callback never called"
        assert progress == sorted(progress)
        assert progress[-1] == len(nodes)


async def test_walk_subtree_and_max_nodes_cap() -> None:
    async with engine(tree_timeout=60.0) as (_mock, client):
        subtree = await client.walk(IN8)
        assert IN8 in subtree
        assert all(path.startswith(IN8) for path in subtree)
        assert IN8_METERS in subtree
        assert DEV not in subtree

        capped = await client.walk(ROOT, max_nodes=5)
        assert 0 < len(capped) <= 5


async def test_walk_skips_unresolvable_nodes() -> None:
    doc = mutable_fixture()
    doc["nodes"]["/devices/0"]["children"].append("ghosts")
    mock = MockEngine(doc, port=0)
    port = await mock.start()
    client = UAClient("127.0.0.1", port, tree_timeout=60.0)
    try:
        nodes = await client.walk(DEV)
        assert "/devices/0/ghosts" not in nodes
        assert f"{IN8}/meters/0" in nodes
    finally:
        await client.aclose()
        await mock.stop()


async def test_walk_uses_the_poll_socket() -> None:
    async with engine(tree_timeout=60.0) as (_mock, client):
        before = client._control.frames_sent
        await client.walk(IN8)
        assert client._poll.frames_sent > 0
        assert client._control.frames_sent == before


# ------------------------------------------------------------------------ lifecycle


async def test_connect_is_idempotent_and_lazy() -> None:
    mock = MockEngine(fixture(), port=0)
    port = await mock.start()
    client = UAClient("127.0.0.1", port)
    try:
        assert client.connected is False
        # Lazy: a public op connects on its own.
        assert await client.get_value(IN8_NAME) == "UG 1"
        assert client.connected
        await asyncio.gather(client.connect(), client.connect(), client.connect())
        assert client.connected
        assert mock.sent_frames == [f"get {IN8_NAME}"]
    finally:
        await client.aclose()
        await mock.stop()


async def test_connect_failure_raises_connect_error() -> None:
    mock = MockEngine(fixture(), port=0)
    port = await mock.start()
    await mock.stop()
    client = UAClient("127.0.0.1", port, connect_timeout=0.5)
    try:
        with pytest.raises(UAConnectError) as excinfo:
            await client.get_value(IN8_NAME)
        assert str(port) in str(excinfo.value)
        assert client.connected is False
    finally:
        await client.aclose()


async def test_aclose_is_idempotent_and_terminal() -> None:
    async with engine() as (_mock, client):
        assert await client.get_value(IN8_NAME) == "UG 1"
        await client.aclose()
        await client.aclose()
        assert client.connected is False
        assert client.closed is True
        with pytest.raises(UAConnectError):
            await client.get_value(IN8_NAME)


def client_tasks() -> list[asyncio.Task[Any]]:
    """Live tasks owned by the client (it names every task it spawns ``uad-*``)."""
    return [
        task
        for task in asyncio.all_tasks()
        if not task.done() and task.get_name().startswith("uad-")
    ]


async def test_aclose_leaves_no_tasks_behind() -> None:
    mock = MockEngine(fixture(), port=0, drop_after=2)
    port = await mock.start()
    client = UAClient("127.0.0.1", port, scalar_timeout=1.0)
    with contextlib.suppress(UAError):
        for _ in range(3):
            await client.get_value(IN8_NAME)
    assert client_tasks(), "expected reader/reconnect tasks while running"
    await client.aclose()
    await mock.stop()
    assert await wait_until(lambda: not client_tasks()), (
        f"client left tasks running after aclose(): {client_tasks()}"
    )


async def test_client_writes_nothing_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    async with engine() as (_mock, client):
        await client.get_node(IN8)
        await client.set_value(IN8_FADER, -4.0)
        await client.poll_values(METER_PATHS)
        await client.subscribe(IN8_MUTE, lambda _value: None)
        await client.unsubscribe(IN8_MUTE)
    assert capsys.readouterr().out == ""


def test_a_malformed_frame_is_dropped_without_losing_the_stream() -> None:
    """Audit L1: one bad frame must not tear down the socket and fail every pending request."""
    channel = UAClient()._control
    good = b'{"path": "/Tempo", "data": 120}\0'
    messages = channel._feed(b"{not json\0\xff\xfe\0" + good)
    assert messages == [{"path": "/Tempo", "data": 120}]
