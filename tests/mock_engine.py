"""In-process mock of the UA Mixer Engine TCP API (see docs/SPEC.md §1 and §4.6).

The mock binds an ephemeral loopback port (``port=0``) and speaks the exact wire protocol
described in SPEC §1: UTF-8 JSON frames terminated by a single ``\\0`` byte, no correlation
IDs, replies and broadcasts dispatched purely by ``path``.

It NEVER binds 127.0.0.1:4710, which belongs to the user's real hardware. Passing
``port=4710`` raises ``ValueError``.

Behaviour notes (mirroring the real engine, VERIFIED facts from SPEC §1):

* ``get`` on a container node returns ``{"properties": {...}, "children": {name: {}}}``.
* ``get`` on a property returns its self-describing metadata dict.
* ``get`` on an unresolvable path returns ``{"path": ..., "error": "Unable to resolve path
  for get."}``.
* ``set`` acks by echoing the applied value on ``<prop>/value``.
* ``set`` is ACCEPTED even for properties flagged ``readonly``. The real engine does not
  enforce its own readonly flags, and safety.py is the only real write barrier.
* Unknown verbs are silently dropped: no reply, no state change.
* ``subscribe`` sends an initial ``{"path": "<prop>/value", "data": current}`` and then
  pushes only when the value actually changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import random
from collections.abc import Iterable
from typing import Any

log = logging.getLogger(__name__)

Node = dict[str, Any]
Fixture = dict[str, Any]

FRAME_TERMINATOR = b"\0"
VALUE_SUFFIX = "/value"
VALID_VERBS = frozenset({"get", "set", "subscribe", "unsubscribe", "post"})
UNRESOLVED_ERROR = "Unable to resolve path for {verb}."
UNPARSEABLE_VALUE_ERROR = "Unable to parse value for set."
METER_FLOOR_DB = -77.0
METER_PROPERTY = "MeterLevel"

_READ_CHUNK = 65536
_RNG_SEED = 0xC0FFEE
_BATCH_WINDOW_S = 0.002
# split_frames chunks the first _SPLIT_FINE_BUDGET bytes of every write into 1-3 byte
# pieces; beyond that it uses large blocks so multi-hundred-KB replies stay fast. UTF-8
# continuation-byte boundaries are forced across the WHOLE payload either way.
_SPLIT_FINE_BUDGET = 4096
_SPLIT_COARSE_CHUNK = 8192


def _split_bytes(payload: bytes, rng: random.Random) -> list[bytes]:
    """Split ``payload`` into 1-3 byte chunks, deliberately cutting multi-byte UTF-8.

    Every UTF-8 continuation byte (0x80..0xBF) gets a chunk boundary immediately before
    it, which guarantees that each multi-byte character is delivered across at least two
    TCP writes.
    """
    forced = {i for i, byte in enumerate(payload) if 0x80 <= byte < 0xC0}
    chunks: list[bytes] = []
    pos = 0
    total = len(payload)
    while pos < total:
        size = rng.randint(1, 3) if pos < _SPLIT_FINE_BUDGET else _SPLIT_COARSE_CHUNK
        end = min(pos + size, total)
        for boundary in range(pos + 1, end):
            if boundary in forced:
                end = boundary
                break
        chunks.append(payload[pos:end])
        pos = end
    return chunks


def normalise_path(path: str) -> str:
    """Normalise a request path: drop any query string, collapse a trailing slash."""
    base = path.split("?", 1)[0]
    if not base:
        return "/"
    if not base.startswith("/"):
        base = "/" + base
    if len(base) > 1 and base.endswith("/"):
        base = base.rstrip("/") or "/"
    return base


def _parse_request(text: str) -> tuple[str | None, str | None, str | None]:
    """Split a request frame into ``(verb, path, raw_json_value)``."""
    parts = text.split(" ", 2)
    verb = parts[0] if parts and parts[0] else None
    path = parts[1] if len(parts) > 1 and parts[1] else None
    payload = parts[2] if len(parts) > 2 else None
    return verb, path, payload


class _Connection:
    """Per-client connection state."""

    __slots__ = ("reader", "writer", "outbox", "subscriptions", "frames_received", "abrupt")

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.outbox: asyncio.Queue[bytes] = asyncio.Queue()
        self.subscriptions: set[str] = set()
        self.frames_received = 0
        self.abrupt = False


class MockEngine:
    """Fixture-driven stand-in for the UA Mixer Engine (SPEC §4.6).

    ``sent_frames`` records every frame RECEIVED from clients (decoded, terminator
    stripped) for wire-level assertions. ``set_count()`` counts every ``set`` frame that
    reached the wire, including sets to unresolvable paths and sets swallowed by
    ``never_reply_on``, so ``readonly`` mode can be asserted as ``set_count() == 0``.
    Use :meth:`value` to check what the engine actually applied.
    """

    HOST = "127.0.0.1"
    REAL_ENGINE_PORT = 4710

    def __init__(
        self,
        fixture: Fixture,
        *,
        split_frames: bool = False,
        batch_frames: bool = False,
        meter_noise_hz: float = 0.0,
        reply_delay: float = 0.0,
        drop_after: int | None = None,
        never_reply_on: Iterable[str] = frozenset(),
        port: int = 0,
    ) -> None:
        if port == self.REAL_ENGINE_PORT:
            raise ValueError(
                f"refusing to bind {self.HOST}:{self.REAL_ENGINE_PORT}, which is the real "
                "UA Mixer Engine port; MockEngine binds ephemeral ports only"
            )
        self.host: str = self.HOST
        self.port: int = 0
        self.sent_frames: list[str] = []

        self._requested_port = port
        self._split_frames = split_frames
        self._batch_frames = batch_frames
        self._meter_noise_hz = meter_noise_hz
        self._reply_delay = reply_delay
        self._drop_after = drop_after
        self._never_reply_on = frozenset(never_reply_on)

        self._nodes: dict[str, Node] = {}
        self._meta: dict[str, Any] = {}
        self._meter_paths: list[str] = []
        self._connections: set[_Connection] = set()
        self._handlers: set[asyncio.Task[None]] = set()
        self._server: asyncio.AbstractServer | None = None
        self._noise_task: asyncio.Task[None] | None = None
        self._set_count = 0
        self._rng = random.Random(_RNG_SEED)

        self.load_fixture(fixture)

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> int:
        """Bind an ephemeral loopback port and start serving. Returns the bound port."""
        if self._server is not None:
            return self.port
        self._server = await asyncio.start_server(
            self._handle_client, host=self.host, port=self._requested_port
        )
        sockname = self._server.sockets[0].getsockname()
        self.port = int(sockname[1])
        if self.port == self.REAL_ENGINE_PORT:  # pragma: no cover - paranoia
            await self.stop()
            raise RuntimeError("bound the real UA Mixer Engine port; aborting")
        if self._meter_noise_hz > 0:
            self._noise_task = asyncio.create_task(self._meter_noise_loop())
        log.debug("MockEngine listening on %s:%d", self.host, self.port)
        return self.port

    async def stop(self) -> None:
        """Stop serving, drop every client connection. Idempotent."""
        if self._noise_task is not None:
            self._noise_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._noise_task
            self._noise_task = None
        for conn in list(self._connections):
            self._abort(conn)
        self._connections.clear()
        if self._handlers:
            # Let the aborted connection handlers unwind so no task outlives the engine.
            await asyncio.wait(set(self._handlers), timeout=1.0)
            self._handlers.clear()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        log.debug("MockEngine stopped")

    # ------------------------------------------------------------------ state access

    def load_fixture(self, fixture: Fixture) -> None:
        """Replace the in-memory tree with a deep copy of ``fixture``.

        Used both by ``__init__`` and by reconnect tests that swap the fixture (e.g. x8 to
        twin) while the server keeps running.
        """
        snapshot = copy.deepcopy(fixture)
        self._meta = snapshot.get("_meta", {})
        self._nodes = snapshot.get("nodes", {})
        self._meter_paths = [
            f"{path}/{METER_PROPERTY}"
            for path, node in self._nodes.items()
            if METER_PROPERTY in node.get("properties", {})
        ]

    @property
    def nodes(self) -> dict[str, Node]:
        """Live (mutable) node tree: mutations are what a client's ``get`` will see."""
        return self._nodes

    @property
    def fixture_meta(self) -> dict[str, Any]:
        return self._meta

    @property
    def client_count(self) -> int:
        return len(self._connections)

    def set_count(self) -> int:
        """Number of ``set`` frames received on the wire."""
        return self._set_count

    def value(self, prop_path: str) -> Any:
        """Current value of a property path, or ``None`` if it does not resolve."""
        meta = self._find_property(normalise_path(prop_path))
        return None if meta is None else meta.get("value")

    async def push(self, prop_path: str, value: Any) -> None:
        """Apply a value engine-side and push it to subscribers (unsolicited broadcast)."""
        path = normalise_path(prop_path)
        meta = self._find_property(path)
        if meta is None:
            raise KeyError(prop_path)
        meta["value"] = value
        self._push_to_subscribers(path, value)

    # ------------------------------------------------------------------ resolution

    def _find_node(self, path: str) -> Node | None:
        return self._nodes.get(path)

    def _find_property(self, path: str) -> dict[str, Any] | None:
        if path == "/" or "/" not in path:
            return None
        parent, _, name = path.rpartition("/")
        node = self._nodes.get(parent or "/")
        if node is None:
            return None
        meta = node.get("properties", {}).get(name)
        return meta if isinstance(meta, dict) else None

    def _resolve(self, path: str) -> tuple[str, Any] | None:
        """Resolve to ``("node", node)``, ``("property", meta)``, ``("value", value)`` or None."""
        node = self._find_node(path)
        if node is not None:
            return "node", node
        meta = self._find_property(path)
        if meta is not None:
            return "property", meta
        if path.endswith(VALUE_SUFFIX):
            meta = self._find_property(path[: -len(VALUE_SUFFIX)])
            if meta is not None:
                return "value", meta.get("value")
        return None

    @staticmethod
    def _property_path(path: str) -> str:
        return path[: -len(VALUE_SUFFIX)] if path.endswith(VALUE_SUFFIX) else path

    # ------------------------------------------------------------------ request handling

    def _reply_get(self, requested: str) -> dict[str, Any]:
        path = normalise_path(requested)
        resolved = self._resolve(path)
        if resolved is None:
            return {"path": requested, "error": UNRESOLVED_ERROR.format(verb="get")}
        kind, obj = resolved
        if kind == "node":
            data: Any = {
                "properties": obj.get("properties", {}),
                "children": {name: {} for name in obj.get("children", [])},
            }
        else:
            data = obj
        return {"path": requested, "data": data}

    def _apply_set(
        self, requested: str, raw_value: str | None
    ) -> tuple[dict[str, Any] | None, tuple[str, Any] | None]:
        """Return ``(reply, change)`` where ``change`` is ``(prop_path, value)`` or None."""
        path = normalise_path(requested)
        prop_path = self._property_path(path)
        meta = self._find_property(prop_path)
        if meta is None:
            return {"path": requested, "error": UNRESOLVED_ERROR.format(verb="set")}, None
        if raw_value is None:
            log.warning("set with no value: %r", requested)
            return {"path": requested, "error": UNPARSEABLE_VALUE_ERROR}, None
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            log.warning("set with unparseable JSON value: %r", raw_value)
            return {"path": requested, "error": UNPARSEABLE_VALUE_ERROR}, None
        previous = meta.get("value")
        # The real engine does NOT enforce its own readonly flags, so accept the write.
        meta["value"] = value
        change = None if previous == value else (prop_path, value)
        return {"path": prop_path + VALUE_SUFFIX, "data": value}, change

    def _reply_subscribe(self, conn: _Connection, requested: str) -> dict[str, Any]:
        prop_path = self._property_path(normalise_path(requested))
        meta = self._find_property(prop_path)
        if meta is None:
            return {"path": requested, "error": UNRESOLVED_ERROR.format(verb="subscribe")}
        conn.subscriptions.add(prop_path)
        return {"path": prop_path + VALUE_SUFFIX, "data": meta.get("value")}

    def _is_swallowed(self, requested: str) -> bool:
        if not self._never_reply_on:
            return False
        path = normalise_path(requested)
        candidates = {requested, path, self._property_path(path)}
        return bool(candidates & self._never_reply_on)

    async def _on_frame(self, conn: _Connection, raw: bytes) -> bool:
        """Handle one received frame. Returns False when the connection must be dropped."""
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            log.warning("undecodable frame (%d bytes) dropped", len(raw))
            return True
        text = text.strip()
        if not text:
            return True

        self.sent_frames.append(text)
        conn.frames_received += 1
        verb, path, raw_value = _parse_request(text)
        if verb == "set":
            self._set_count += 1

        if self._drop_after is not None and conn.frames_received >= self._drop_after:
            log.debug("drop_after=%s reached; aborting connection", self._drop_after)
            conn.abrupt = True
            return False

        if verb is None or verb not in VALID_VERBS or path is None:
            # Unknown verbs are silently dropped: never wait on a reply for one.
            log.debug("dropping frame with unusable verb/path: %r", text)
            return True
        if self._is_swallowed(path):
            log.debug("never_reply_on swallowed: %r", text)
            return True

        reply: dict[str, Any] | None = None
        change: tuple[str, Any] | None = None
        if verb == "get":
            reply = self._reply_get(path)
        elif verb == "set":
            reply, change = self._apply_set(path, raw_value)
        elif verb == "subscribe":
            reply = self._reply_subscribe(conn, path)
        elif verb == "unsubscribe":
            conn.subscriptions.discard(self._property_path(normalise_path(path)))
        elif verb == "post":
            # SPEC lists `post` as a valid verb but documents no semantics for it.
            log.debug("ignoring post: %r", text)

        if reply is not None:
            if self._reply_delay:
                await asyncio.sleep(self._reply_delay)
            self._enqueue(conn, reply)
        if change is not None:
            self._push_to_subscribers(*change)
        return True

    # ------------------------------------------------------------------ broadcasting

    def _push_to_subscribers(self, prop_path: str, value: Any) -> None:
        frame = {"path": prop_path + VALUE_SUFFIX, "data": value}
        for conn in list(self._connections):
            if prop_path in conn.subscriptions:
                self._enqueue(conn, frame)

    def _broadcast(self, frame: dict[str, Any]) -> None:
        for conn in list(self._connections):
            self._enqueue(conn, frame)

    async def _meter_noise_loop(self) -> None:
        interval = 1.0 / self._meter_noise_hz
        while True:
            await asyncio.sleep(interval)
            if not self._connections or not self._meter_paths:
                continue
            prop_path = self._rng.choice(self._meter_paths)
            value = round(self._rng.uniform(METER_FLOOR_DB, 0.0), 3)
            meta = self._find_property(prop_path)
            if meta is not None:
                meta["value"] = value
            self._broadcast({"path": prop_path + VALUE_SUFFIX, "data": value})

    # ------------------------------------------------------------------ transport

    def _enqueue(self, conn: _Connection, frame: dict[str, Any]) -> None:
        encoded = json.dumps(frame, ensure_ascii=False).encode("utf-8") + FRAME_TERMINATOR
        conn.outbox.put_nowait(encoded)

    async def _writer_loop(self, conn: _Connection) -> None:
        while True:
            payload = await conn.outbox.get()
            if self._batch_frames:
                # Coalesce whatever else shows up in a short window into one TCP write.
                await asyncio.sleep(_BATCH_WINDOW_S)
                pending = [payload]
                while not conn.outbox.empty():
                    pending.append(conn.outbox.get_nowait())
                payload = b"".join(pending)
            await self._write_bytes(conn, payload)

    async def _write_bytes(self, conn: _Connection, payload: bytes) -> None:
        writer = conn.writer
        if writer.is_closing():
            return
        try:
            if self._split_frames:
                for chunk in _split_bytes(payload, self._rng):
                    writer.write(chunk)
                    await writer.drain()
                    await asyncio.sleep(0)
            else:
                writer.write(payload)
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, OSError) as exc:
            log.debug("write failed, client gone: %r", exc)

    def _abort(self, conn: _Connection) -> None:
        with contextlib.suppress(Exception):
            transport = conn.writer.transport
            transport.abort()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        conn = _Connection(reader, writer)
        self._connections.add(conn)
        handler = asyncio.current_task()
        if handler is not None:
            self._handlers.add(handler)
        writer_task = asyncio.create_task(self._writer_loop(conn))
        buffer = bytearray()
        try:
            while True:
                data = await reader.read(_READ_CHUNK)
                if not data:
                    break
                buffer.extend(data)
                dropped = False
                while True:
                    index = buffer.find(FRAME_TERMINATOR)
                    if index < 0:
                        break
                    raw = bytes(buffer[:index])
                    del buffer[: index + 1]
                    if not await self._on_frame(conn, raw):
                        dropped = True
                        break
                if dropped:
                    break
        except (ConnectionResetError, asyncio.IncompleteReadError, OSError) as exc:
            log.debug("client read ended: %r", exc)
        finally:
            writer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writer_task
            self._connections.discard(conn)
            if handler is not None:
                self._handlers.discard(handler)
            if conn.abrupt:
                self._abort(conn)
            else:
                with contextlib.suppress(Exception):
                    writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
