"""Self-tests for the mock engine and the derived fixtures.

These talk to ``MockEngine`` with a raw asyncio TCP client only, deliberately without
``uad_console_mcp.client`` import, so the wire contract in SPEC §1 is pinned independently
of the real client implementation.
"""

from __future__ import annotations

import asyncio
import copy
import json
import random
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest

from tests.fixtures.derive import (
    BASE_FIXTURE_PATH,
    FIXTURE_DIR,
    cascade_fixture,
    load_base,
    twin_fixture,
)
from tests.mock_engine import MockEngine, _split_bytes, normalise_path

REAL_ENGINE_PORT = 4710
GET_ERROR = "Unable to resolve path for get."
SET_ERROR = "Unable to resolve path for set."
SUBSCRIBE_ERROR = "Unable to resolve path for subscribe."

INPUT_8 = "/devices/0/inputs/8"
NAME_8 = f"{INPUT_8}/Name"
FADER_0 = "/devices/0/inputs/0/FaderLevel"
MUTE_0 = "/devices/0/inputs/0/Mute"
MULTIBYTE_NAME = "Vöcal 日本 🎤 ünïcøde"


# --------------------------------------------------------------------------- raw client


class RawClient:
    """Minimal ``\\0``-framed JSON client. Knows nothing about uad_console_mcp."""

    def __init__(self, host: str, port: int) -> None:
        if port == REAL_ENGINE_PORT:
            raise AssertionError("refusing to connect to the real UA Mixer Engine port")
        self.host = host
        self.port = port
        self.skipped: list[dict[str, Any]] = []
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)

    @property
    def reader(self) -> asyncio.StreamReader:
        assert self._reader is not None
        return self._reader

    async def send(self, text: str) -> None:
        assert self._writer is not None
        self._writer.write(text.encode("utf-8") + b"\0")
        await self._writer.drain()

    async def recv(self, timeout_s: float = 2.0) -> dict[str, Any]:
        raw = await asyncio.wait_for(self.reader.readuntil(b"\0"), timeout_s)
        return json.loads(raw[:-1].decode("utf-8"))

    async def recv_path(self, path: str, timeout_s: float = 2.0) -> dict[str, Any]:
        """Read until a frame whose ``path`` matches; unmatched frames land in ``skipped``."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"no frame for {path!r} (skipped {len(self.skipped)})")
            frame = await self.recv(remaining)
            if frame.get("path") == path:
                return frame
            self.skipped.append(frame)

    async def request(self, text: str, path: str, timeout_s: float = 2.0) -> dict[str, Any]:
        await self.send(text)
        return await self.recv_path(path, timeout_s)

    async def collect(self, duration: float = 0.25) -> list[dict[str, Any]]:
        """Everything that arrives within ``duration`` seconds (may be empty)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + duration
        frames: list[dict[str, Any]] = []
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return frames
            try:
                frames.append(await self.recv(remaining))
            except TimeoutError:
                return frames

    async def close(self) -> None:
        if self._writer is None:
            return
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass


async def _wait_for(predicate: Callable[[], bool], limit: float = 2.0) -> None:
    """Poll ``predicate`` until true (server-side bookkeeping lags the client's close)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")


@asynccontextmanager
async def raw_client(engine: MockEngine) -> AsyncIterator[RawClient]:
    client = RawClient(engine.host, engine.port)
    await client.connect()
    try:
        yield client
    finally:
        await client.close()


# --------------------------------------------------------------------------- lifecycle


async def test_binds_ephemeral_loopback_port(mock: MockEngine) -> None:
    assert mock.host == "127.0.0.1"
    assert mock.port != 0
    assert mock.port != REAL_ENGINE_PORT


def test_refuses_the_real_engine_port(base_fixture: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="real"):
        MockEngine(base_fixture, port=REAL_ENGINE_PORT)


async def test_start_is_idempotent_and_stop_is_repeatable(mock_factory: Any) -> None:
    engine = await mock_factory()
    assert await engine.start() == engine.port
    await engine.stop()
    await engine.stop()


async def test_does_not_mutate_the_session_fixture(
    base_fixture: dict[str, Any], mock: MockEngine
) -> None:
    original = base_fixture["nodes"][NAME_8.rsplit("/", 1)[0]]["properties"]["Name"]["value"]
    async with raw_client(mock) as client:
        await client.request(f'set {NAME_8}/value "mutated"', f"{NAME_8}/value")
    assert mock.value(NAME_8) == "mutated"
    assert base_fixture["nodes"][INPUT_8]["properties"]["Name"]["value"] == original


# --------------------------------------------------------------------------- get


async def test_get_root_container(mock: MockEngine) -> None:
    async with raw_client(mock) as client:
        frame = await client.request("get /", "/")
    data = frame["data"]
    assert set(data["children"]) == {
        "devices",
        "inputroutes",
        "outputroutes",
        "plugins",
        "pluginscenes",
    }
    assert all(stub == {} for stub in data["children"].values())
    assert data["properties"]["SampleRate"]["value"] == 48000
    assert data["properties"]["SampleRate"]["type"] == "int"


async def test_get_container_returns_properties_and_child_stubs(mock: MockEngine) -> None:
    async with raw_client(mock) as client:
        frame = await client.request("get /devices/0/inputs", "/devices/0/inputs")
    data = frame["data"]
    assert data["properties"]["Type"]["value"] == "container"
    assert len(data["children"]) == 28
    assert data["children"]["8"] == {}


async def test_get_property_returns_metadata(mock: MockEngine) -> None:
    async with raw_client(mock) as client:
        frame = await client.request(f"get {NAME_8}", NAME_8)
    assert frame["data"] == {"type": "string", "default": "ADAT 1", "value": "UG 1"}


async def test_get_property_value_returns_scalar(mock: MockEngine) -> None:
    async with raw_client(mock) as client:
        frame = await client.request(f"get {NAME_8}/value", f"{NAME_8}/value")
    assert frame["data"] == "UG 1"


async def test_get_property_without_a_value_is_served_verbatim(mock: MockEngine) -> None:
    """Real-engine quirk: 13 captured properties carry metadata but no `value` at all."""
    path = "/devices/0/auxs/0/Gain"
    async with raw_client(mock) as client:
        frame = await client.request(f"get {path}", path)
    assert frame["data"] == {"type": "float", "min": -144.0, "max": 65.0}
    assert "value" not in frame["data"]
    assert mock.value(path) is None


async def test_get_unknown_path_error_shape(mock: MockEngine) -> None:
    async with raw_client(mock) as client:
        frame = await client.request("get /bogus", "/bogus")
    assert frame == {"path": "/bogus", "error": GET_ERROR}


@pytest.mark.parametrize(
    "path",
    ["/devices/0/inputs/999", "/devices/0/inputs/8/Nope", "/devices/0/inputs/8/Name/value/value"],
)
async def test_get_unresolvable_variants(mock: MockEngine, path: str) -> None:
    async with raw_client(mock) as client:
        frame = await client.request(f"get {path}", path)
    assert frame["error"] == GET_ERROR
    assert "data" not in frame


async def test_recursive_query_returns_top_level_stubs_only(mock: MockEngine) -> None:
    """SPEC §1 quirk: `get /?recursive=1` yields the same shallow answer as `get /`."""
    async with raw_client(mock) as client:
        frame = await client.request("get /?recursive=1", "/?recursive=1")
    assert frame["data"]["children"]["devices"] == {}


# --------------------------------------------------------------------------- set


async def test_set_acks_with_applied_value(mock: MockEngine) -> None:
    async with raw_client(mock) as client:
        frame = await client.request(f"set {FADER_0}/value -12.5", f"{FADER_0}/value")
    assert frame == {"path": f"{FADER_0}/value", "data": -12.5}
    assert mock.value(FADER_0) == -12.5
    assert mock.set_count() == 1


async def test_set_without_value_suffix_acks_on_value_path(mock: MockEngine) -> None:
    """The `set /Sleep false` keepalive form: no `/value`, ack still lands on `/value`."""
    async with raw_client(mock) as client:
        frame = await client.request(f"set {MUTE_0} true", f"{MUTE_0}/value")
    assert frame["data"] is True
    assert mock.value(MUTE_0) is True


async def test_set_is_accepted_on_readonly_property(mock: MockEngine) -> None:
    """The real engine does NOT enforce its own readonly flags (SPEC §1 safety fact)."""
    path = "/devices/0/inputs/8/Active"
    assert mock.nodes[INPUT_8]["properties"]["Active"]["readonly"] is True
    async with raw_client(mock) as client:
        frame = await client.request(f"set {path}/value false", f"{path}/value")
    assert frame == {"path": f"{path}/value", "data": False}
    assert mock.value(path) is False


async def test_set_unknown_path_error_shape(mock: MockEngine) -> None:
    async with raw_client(mock) as client:
        frame = await client.request('set /bogus/value "x"', "/bogus/value")
    assert frame == {"path": "/bogus/value", "error": SET_ERROR}
    assert mock.set_count() == 1


async def test_set_with_unparseable_value_reports_instead_of_hanging(mock: MockEngine) -> None:
    async with raw_client(mock) as client:
        frame = await client.request(f"set {NAME_8}/value not-json", f"{NAME_8}/value")
    assert "error" in frame
    assert mock.value(NAME_8) == "UG 1"


async def test_set_roundtrips_multibyte_strings(mock: MockEngine) -> None:
    payload = json.dumps(MULTIBYTE_NAME, ensure_ascii=False)
    async with raw_client(mock) as client:
        ack = await client.request(f"set {NAME_8}/value {payload}", f"{NAME_8}/value")
        assert ack["data"] == MULTIBYTE_NAME
        echoed = await client.request(f"get {NAME_8}", NAME_8)
    assert echoed["data"]["value"] == MULTIBYTE_NAME


# --------------------------------------------------------------------------- verbs


async def test_unknown_verb_is_silently_dropped(mock: MockEngine) -> None:
    async with raw_client(mock) as client:
        await client.send("frobnicate /devices")
        await client.send("get /devices")
        first = await client.recv()
    assert first["path"] == "/devices"
    assert client.skipped == []
    assert "frobnicate /devices" in mock.sent_frames


async def test_post_and_unsubscribe_produce_no_reply(mock: MockEngine) -> None:
    async with raw_client(mock) as client:
        await client.send("post /devices/0/inputs/0 {}")
        await client.send(f"unsubscribe {FADER_0}")
        await client.send("get /devices")
        first = await client.recv()
    assert first["path"] == "/devices"


async def test_sent_frames_records_every_received_frame(mock: MockEngine) -> None:
    async with raw_client(mock) as client:
        await client.request("get /devices", "/devices")
        await client.request(f"set {MUTE_0}/value true", f"{MUTE_0}/value")
        await client.send("nonsense")
        await asyncio.sleep(0.05)
    assert mock.sent_frames == ["get /devices", f"set {MUTE_0}/value true", "nonsense"]


# --------------------------------------------------------------------------- subscribe


async def test_subscribe_pushes_initial_then_only_on_change(mock: MockEngine) -> None:
    async with raw_client(mock) as watcher, raw_client(mock) as setter:
        initial = await watcher.request(f"subscribe {FADER_0}", f"{FADER_0}/value")
        assert initial["data"] == mock.value(FADER_0)

        await setter.request(f"set {FADER_0}/value -6.0", f"{FADER_0}/value")
        pushed = await watcher.recv_path(f"{FADER_0}/value")
        assert pushed["data"] == -6.0

        # Same value again: no change, therefore no push.
        await setter.request(f"set {FADER_0}/value -6.0", f"{FADER_0}/value")
        assert await watcher.collect(0.2) == []


async def test_subscribe_accepts_value_suffix_and_survives_unsubscribe(mock: MockEngine) -> None:
    async with raw_client(mock) as watcher, raw_client(mock) as setter:
        await watcher.request(f"subscribe {MUTE_0}/value", f"{MUTE_0}/value")
        await setter.request(f"set {MUTE_0}/value true", f"{MUTE_0}/value")
        assert (await watcher.recv_path(f"{MUTE_0}/value"))["data"] is True

        await watcher.send(f"unsubscribe {MUTE_0}")
        await asyncio.sleep(0.05)
        await setter.request(f"set {MUTE_0}/value false", f"{MUTE_0}/value")
        assert await watcher.collect(0.2) == []


async def test_subscribe_on_container_errors(mock: MockEngine) -> None:
    async with raw_client(mock) as client:
        frame = await client.request("subscribe /devices/0/inputs", "/devices/0/inputs")
    assert frame["error"] == SUBSCRIBE_ERROR


async def test_engine_side_push_reaches_subscribers(mock: MockEngine) -> None:
    async with raw_client(mock) as client:
        await client.request(f"subscribe {FADER_0}", f"{FADER_0}/value")
        await mock.push(FADER_0, -3.25)
        frame = await client.recv_path(f"{FADER_0}/value")
    assert frame["data"] == -3.25
    assert mock.value(FADER_0) == -3.25


# --------------------------------------------------------------------------- concurrency


async def test_multiple_clients_share_one_engine_state(mock: MockEngine) -> None:
    async with raw_client(mock) as a, raw_client(mock) as b, raw_client(mock) as c:
        await a.request(f'set {NAME_8}/value "Shared"', f"{NAME_8}/value")
        replies = await asyncio.gather(
            b.request(f"get {NAME_8}", NAME_8), c.request(f"get {NAME_8}", NAME_8)
        )
    assert [reply["data"]["value"] for reply in replies] == ["Shared", "Shared"]
    await _wait_for(lambda: mock.client_count == 0)


async def test_pipelined_requests_reply_in_order(mock: MockEngine) -> None:
    paths = [f"/devices/0/inputs/{index}/Name" for index in range(4)]
    async with raw_client(mock) as client:
        for path in paths:
            await client.send(f"get {path}")
        received = [(await client.recv())["path"] for _ in paths]
    assert received == paths


# --------------------------------------------------------------------------- adversarial


def test_split_bytes_cuts_every_multibyte_character() -> None:
    payload = json.dumps({"path": "/x", "data": MULTIBYTE_NAME}, ensure_ascii=False).encode()
    chunks = _split_bytes(payload, random.Random(1))
    assert b"".join(chunks) == payload
    assert all(1 <= len(chunk) <= 3 for chunk in chunks)
    assert any(byte >= 0x80 for byte in payload), "payload must contain multi-byte UTF-8"
    # No chunk may hold a complete multi-byte character: a lead byte always ends its chunk
    # and every continuation byte starts one, so each character spans >= 2 writes.
    for chunk in chunks:
        for index, byte in enumerate(chunk):
            if byte >= 0xC0:
                assert index == len(chunk) - 1, chunk
            elif byte >= 0x80:
                assert index == 0, chunk


async def test_split_frames_survives_bytewise_delivery(mock_factory: Any) -> None:
    engine = await mock_factory(split_frames=True)
    payload = json.dumps(MULTIBYTE_NAME, ensure_ascii=False)
    async with raw_client(engine) as client:
        ack = await client.request(f"set {NAME_8}/value {payload}", f"{NAME_8}/value")
        assert ack["data"] == MULTIBYTE_NAME
        node = await client.request("get /devices", "/devices")
        assert node["data"]["children"] == {"0": {}}
        prop = await client.request(f"get {NAME_8}", NAME_8)
    assert prop["data"]["value"] == MULTIBYTE_NAME


async def test_batch_frames_coalesces_replies_into_one_write(mock_factory: Any) -> None:
    engine = await mock_factory(batch_frames=True)
    paths = [f"/devices/0/inputs/{index}/Mute" for index in range(3)]
    async with raw_client(engine) as client:
        for path in paths:
            await client.send(f"get {path}")
        buffer = b""
        per_read: list[int] = []
        while buffer.count(b"\0") < len(paths):
            chunk = await asyncio.wait_for(client.reader.read(65536), 2.0)
            assert chunk, "connection closed early"
            per_read.append(chunk.count(b"\0"))
            buffer += chunk
    assert max(per_read) >= 2, f"no coalescing observed: {per_read}"
    decoded = [json.loads(raw) for raw in buffer.split(b"\0") if raw]
    assert [frame["path"] for frame in decoded] == paths


async def test_meter_noise_broadcasts_to_every_client(mock_factory: Any) -> None:
    engine = await mock_factory(meter_noise_hz=200.0)
    async with raw_client(engine) as a, raw_client(engine) as b:
        frames_a = await a.collect(0.3)
        frames_b = await b.collect(0.05)
    assert frames_a, "no meter noise received"
    assert frames_b, "meter noise not broadcast to the second client"
    for frame in frames_a:
        assert frame["path"].endswith("/MeterLevel/value")
        assert -77.0 <= frame["data"] <= 0.0


async def test_meter_noise_interleaves_between_request_and_reply(mock_factory: Any) -> None:
    engine = await mock_factory(meter_noise_hz=200.0, reply_delay=0.15)
    async with raw_client(engine) as client:
        frame = await client.request(f"get {NAME_8}", NAME_8, timeout_s=3.0)
    assert frame["data"]["value"] == "UG 1"
    assert client.skipped, "expected meter broadcasts to arrive before the reply"
    assert all(other["path"].endswith("/MeterLevel/value") for other in client.skipped)


async def test_reply_delay_delays_replies(mock_factory: Any) -> None:
    engine = await mock_factory(reply_delay=0.2)
    loop = asyncio.get_running_loop()
    async with raw_client(engine) as client:
        started = loop.time()
        await client.request("get /devices", "/devices", timeout_s=3.0)
        elapsed = loop.time() - started
    assert elapsed >= 0.2


async def test_drop_after_closes_the_connection_abruptly(mock_factory: Any) -> None:
    engine = await mock_factory(drop_after=2)
    async with raw_client(engine) as client:
        first = await client.request("get /devices", "/devices")
        assert first["data"]["children"] == {"0": {}}
        with pytest.raises((asyncio.IncompleteReadError, ConnectionResetError, TimeoutError)):
            await client.request(f"get {NAME_8}", NAME_8, timeout_s=1.0)
    assert engine.sent_frames == ["get /devices", f"get {NAME_8}"]
    # The engine still serves new connections after dropping one.
    async with raw_client(engine) as reconnected:
        assert (await reconnected.request("get /devices", "/devices"))["data"]["children"]


async def test_never_reply_on_swallows_matching_requests(mock_factory: Any) -> None:
    engine = await mock_factory(never_reply_on={NAME_8})
    async with raw_client(engine) as client:
        await client.send(f"get {NAME_8}")
        assert await client.collect(0.2) == []
        await client.send(f'set {NAME_8}/value "nope"')
        assert await client.collect(0.2) == []
        # Anything else still answers normally.
        assert (await client.request(f"get {MUTE_0}", MUTE_0))["data"]["value"] is False
    assert engine.value(NAME_8) == "UG 1", "swallowed set must not be applied"
    assert engine.set_count() == 1, "the set still reached the wire"


async def test_adversarial_modes_combine(mock_factory: Any) -> None:
    engine = await mock_factory(split_frames=True, batch_frames=True, meter_noise_hz=100.0)
    async with raw_client(engine) as client:
        await client.request(f'set {NAME_8}/value "{MULTIBYTE_NAME}"', f"{NAME_8}/value")
        frame = await client.request(f"get {NAME_8}", NAME_8, timeout_s=3.0)
    assert frame["data"]["value"] == MULTIBYTE_NAME


# --------------------------------------------------------------------------- fixture swap


async def test_load_fixture_swaps_the_served_tree(mock: MockEngine) -> None:
    async with raw_client(mock) as client:
        before = await client.request("get /devices/0/inputs", "/devices/0/inputs")
        assert len(before["data"]["children"]) == 28

        mock.load_fixture(twin_fixture(load_base()))

        after = await client.request("get /devices/0/inputs", "/devices/0/inputs")
        device = await client.request("get /devices/0/DeviceTypeID", "/devices/0/DeviceTypeID")
    assert sorted(after["data"]["children"]) == ["0", "1", "2", "3", "4", "5"]
    assert device["data"]["value"] == "Apollo Twin X"


# --------------------------------------------------------------------------- derive.py


def assert_valid_fixture(fixture: dict[str, Any]) -> None:
    """Every fixture must satisfy the SPEC §3 shape and be internally consistent."""
    assert set(fixture) == {"_meta", "nodes"}
    nodes = fixture["nodes"]
    assert "/" in nodes
    for path, node in nodes.items():
        assert path == "/" or path.startswith("/"), path
        assert set(node) == {"properties", "children"}, path
        assert isinstance(node["properties"], dict), path
        assert isinstance(node["children"], list), path
        for name, meta in node["properties"].items():
            assert isinstance(name, str), path
            assert isinstance(meta, dict), f"{path}/{name}"
            # `value` is NOT guaranteed: the real engine reports a handful of properties
            # (e.g. /devices/0/auxs/0/Gain) as metadata with no value at all.
            assert "type" in meta, f"{path}/{name}"
        for child in node["children"]:
            assert isinstance(child, str), path
            child_path = f"/{child}" if path == "/" else f"{path}/{child}"
            assert child_path in nodes, child_path
        if path != "/":
            parent, _, name = path.rpartition("/")
            parent = parent or "/"
            assert parent in nodes, path
            assert name in nodes[parent]["children"], path
    assert fixture["_meta"].get("node_count", len(nodes)) == len(nodes)


def test_base_fixture_is_valid(base_fixture: dict[str, Any]) -> None:
    assert_valid_fixture(base_fixture)
    assert base_fixture["_meta"]["device"] == "Apollo x8 Gen 2"


def test_cascade_fixture(base_fixture: dict[str, Any]) -> None:
    cascade = cascade_fixture(base_fixture)
    assert_valid_fixture(cascade)
    nodes = cascade["nodes"]

    assert nodes["/devices"]["children"] == ["0", "1"]
    device = nodes["/devices/1"]["properties"]
    assert device["DeviceName"]["value"] == "Apollo x8 Gen 2 #2"
    assert device["MultiUnitOrder"]["value"] == 1
    assert device["DeviceTypeID"]["value"] == "Apollo x8 Gen 2"

    base_names = [
        nodes[f"/devices/0/inputs/{index}"]["properties"]["Name"]["value"]
        for index in nodes["/devices/0/inputs"]["children"]
    ]
    cascade_names = [
        nodes[f"/devices/1/inputs/{index}"]["properties"]["Name"]["value"]
        for index in nodes["/devices/1/inputs"]["children"]
    ]
    assert all(not name.startswith("B ") for name in base_names)
    assert all(name.startswith("B ") for name in cascade_names)
    assert sorted(cascade_names) == sorted(f"B {name}" for name in base_names)

    # Device 0 is untouched, and the whole subtree really was duplicated.
    assert nodes["/devices/0"]["properties"]["MultiUnitOrder"]["value"] == 0
    device_0_paths = {p for p in nodes if p.startswith("/devices/0")}
    device_1_paths = {p for p in nodes if p.startswith("/devices/1")}
    assert len(device_1_paths) == len(device_0_paths)
    assert "/devices/1/inputs/0/preamps/0/effects/0" in nodes


def test_twin_fixture(base_fixture: dict[str, Any]) -> None:
    twin = twin_fixture(base_fixture)
    assert_valid_fixture(twin)
    nodes = twin["nodes"]
    inputs = nodes["/devices/0/inputs"]["children"]
    outputs = nodes["/devices/0/outputs"]["children"]

    assert inputs == ["0", "1", "2", "3", "4", "5"]
    assert outputs == ["0"]
    assert "/devices/0/inputs/6" not in nodes
    assert "/devices/0/outputs/1" not in nodes

    def io_type(index: str) -> str:
        return nodes[f"/devices/0/inputs/{index}"]["properties"]["IOType"]["value"]

    assert all("preamps" in nodes[f"/devices/0/inputs/{i}"]["children"] for i in ("0", "1"))
    assert all("preamps" not in nodes[f"/devices/0/inputs/{i}"]["children"] for i in "2345")
    assert [io_type(index) for index in inputs] == [
        "Mic",
        "Mic",
        "Line",
        "Line",
        "Virtual",
        "Virtual",
    ]

    monitor = nodes["/devices/0/outputs/0"]["properties"]
    assert "CRMonitorLevel" in monitor
    assert "CRMonitorLevelTapered" in monitor
    assert nodes["/devices/0"]["properties"]["DeviceTypeID"]["value"] == "Apollo Twin X"
    # Descendants came along with the re-indexed channels.
    assert "/devices/0/inputs/0/preamps/0/effects/0" in nodes
    assert "/devices/0/inputs/5/meters/0" in nodes
    assert "/devices/0/outputs/0/meters/0" in nodes


def test_twin_reindexing_defeats_hardcoded_indices(base_fixture: dict[str, Any]) -> None:
    """The x8 monitor lives at output 22; on the twin it must not."""
    assert "CRMonitorLevel" in base_fixture["nodes"]["/devices/0/outputs/22"]["properties"]
    twin = twin_fixture(base_fixture)
    assert "/devices/0/outputs/22" not in twin["nodes"]


def test_derivations_do_not_mutate_the_base(base_fixture: dict[str, Any]) -> None:
    before = copy.deepcopy(base_fixture)
    cascade_fixture(base_fixture)
    twin_fixture(base_fixture)
    assert base_fixture == before


def test_derived_fixtures_are_never_written_to_disk(base_fixture: dict[str, Any]) -> None:
    cascade_fixture(base_fixture)
    twin_fixture(base_fixture)
    on_disk = {path.name for path in FIXTURE_DIR.glob("*.json")}
    assert on_disk == {BASE_FIXTURE_PATH.name}


@pytest.mark.parametrize("derive", [cascade_fixture, twin_fixture])
async def test_derived_fixtures_serve_over_the_wire(
    base_fixture: dict[str, Any], mock_factory: Any, derive: Any
) -> None:
    engine = await mock_factory(derive(base_fixture))
    async with raw_client(engine) as client:
        devices = await client.request("get /devices", "/devices")
        name = await client.request("get /devices/0/inputs/0/Name", "/devices/0/inputs/0/Name")
    assert devices["data"]["children"]
    assert isinstance(name["data"]["value"], str)


async def test_cascade_second_device_is_addressable(
    base_fixture: dict[str, Any], mock_factory: Any
) -> None:
    engine = await mock_factory(cascade_fixture(base_fixture))
    async with raw_client(engine) as client:
        devices = await client.request("get /devices", "/devices")
        name = await client.request("get /devices/1/inputs/0/Name", "/devices/1/inputs/0/Name")
        order = await client.request(
            "get /devices/1/MultiUnitOrder", "/devices/1/MultiUnitOrder"
        )
    assert sorted(devices["data"]["children"]) == ["0", "1"]
    assert name["data"]["value"] == "B ANALOG 1"
    assert order["data"]["value"] == 1


# --------------------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/", "/"),
        ("/devices/", "/devices"),
        ("/?recursive=1", "/"),
        ("/devices/0?x=1", "/devices/0"),
        ("devices", "/devices"),
    ],
)
def test_normalise_path(raw: str, expected: str) -> None:
    assert normalise_path(raw) == expected
