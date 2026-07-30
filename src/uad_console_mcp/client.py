"""Async transport client for the UA Mixer Engine's local TCP API.

Wire protocol (see docs/SPEC.md §1): UTF-8 JSON text frames terminated by a single
``\\0`` byte, in both directions. Requests are ``<verb> <path>[ <json-value>]``.
Replies and broadcasts are JSON objects carrying a ``path`` key -- there is NO
request/response correlation id, so dispatch is strictly by matching ``path``.

Two sockets are used: a *control* socket (get/set/subscribe/unsubscribe) and a
*poll* socket (``poll_values``/``walk``), so bulk meter reads never contend with
control-socket waiters. Each socket has one supervised reader task; if a reader
dies every pending waiter fails with :class:`UAConnectError`, the socket is marked
disconnected and a reconnect is scheduled with exponential backoff + jitter.

stdlib only. No stdout writes -- MCP stdio servers must keep stdout clean.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("uad_console_mcp.client")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4710

#: Largest single frame we will buffer before declaring the stream broken.
MAX_FRAME_BYTES = 8 * 1024 * 1024
_READ_CHUNK = 65536
_BACKOFF_START = 0.5
_BACKOFF_MAX = 8.0
#: In-flight node reads while walking the tree (bounded so we never flood the engine).
_WALK_CONCURRENCY = 8
_VALUE_SUFFIX = "/value"


class UAError(Exception):
    """Base class for every error raised by this package's transport layer."""


class UAConnectError(UAError):
    """The UA Mixer Engine is unreachable, or an established connection was lost."""


class UATimeoutError(UAError):
    """No reply matching the request arrived within the allotted time."""


class UAPathError(UAError):
    """The engine replied with an ``{"error": ...}`` object; its message is included."""


@dataclass(frozen=True)
class UAProperty:
    """A single scalar property plus the metadata the engine self-describes it with.

    ``min``/``max`` are passed through verbatim, so they may be ``int`` or ``float``
    depending on the property. ``values`` (enum domain) is either a plain list of
    scalars or a list of ``{"value", "string", "enabled"}`` objects -- callers must
    handle both. ``readonly`` is frequently absent from the reply and defaults to
    ``False``; note that the engine does NOT enforce it (see SPEC §1 safety fact).
    """

    path: str
    type: str
    value: Any
    min: float | None = None
    max: float | None = None
    values: list | None = None
    readonly: bool = False
    default: Any = None

    @classmethod
    def from_reply(cls, path: str, data: dict[str, Any]) -> UAProperty:
        """Build a property from the ``data`` object of a ``get <property-path>`` reply."""
        raw_values = data.get("values")
        return cls(
            path=path,
            type=str(data.get("type", "")),
            value=data.get("value"),
            min=data.get("min"),
            max=data.get("max"),
            values=list(raw_values) if isinstance(raw_values, list) else None,
            readonly=bool(data.get("readonly", False)),
            default=data.get("default"),
        )


def _normalize(path: str) -> str:
    """Normalize a node/property path: leading slash, no trailing slash (except root)."""
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    return path


def _join(parent: str, child: str) -> str:
    return f"/{child}" if parent == "/" else f"{parent}/{child}"


def _path_predicate(path: str) -> Callable[[dict[str, Any]], bool]:
    """Match a reply for exactly ``path`` (a ``get`` of a node or property)."""

    def _match(msg: dict[str, Any]) -> bool:
        return msg.get("path") == path

    return _match


def _set_predicate(path: str, value_path: str) -> Callable[[dict[str, Any]], bool]:
    """Match the ack for ``set <path>/value`` or an error reply for the same prefix."""

    def _match(msg: dict[str, Any]) -> bool:
        msg_path = msg.get("path")
        if msg_path == value_path:
            return True
        return "error" in msg and msg_path in (path, value_path)

    return _match


def _extract_value(msg: dict[str, Any]) -> Any:
    """Pull the scalar value out of a reply, tolerating both reply shapes."""
    data = msg.get("data")
    if isinstance(data, dict):
        if "value" in data:
            return data["value"]
        raise UAError(f"{msg.get('path')!r} is not a scalar property (no value in reply)")
    return data


class _Channel:
    """One TCP connection to the engine: framing, waiters, supervised reader, reconnect."""

    def __init__(
        self,
        client: UAClient,
        label: str,
        *,
        on_reconnected: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._client = client
        self.label = label
        self._on_reconnected = on_reconnected
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._buf = bytearray()
        self._waiters: list[tuple[Callable[[dict[str, Any]], bool], asyncio.Future[dict[str, Any]]]]
        self._waiters = []
        self._reader_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._connect_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._connected = False
        self._ever_connected = False
        #: Instrumentation: frames written on this socket (used by tests + logging).
        self.frames_sent = 0

    @property
    def connected(self) -> bool:
        return self._connected

    # -- connection lifecycle ------------------------------------------------

    async def ensure_connected(self) -> None:
        """Idempotent connect. Concurrent callers collapse onto one attempt."""
        if self._connected:
            return
        async with self._connect_lock:
            if self._connected:
                return
            await self._open()

    async def _open(self) -> None:
        host, port = self._client.host, self._client.port
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), self._client.connect_timeout
            )
        except (TimeoutError, OSError) as exc:
            raise UAConnectError(
                f"UA Mixer Engine not reachable on {host}:{port} "
                f"({self.label} socket): {exc}"
            ) from exc
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
        self._reader = reader
        self._writer = writer
        self._buf.clear()
        self._connected = True
        self._reader_task = asyncio.create_task(
            self._read_loop(reader), name=f"uad-{self.label}-reader"
        )
        reconnected = self._ever_connected
        self._ever_connected = True
        logger.debug("%s socket connected to %s:%s", self.label, host, port)
        if reconnected:
            logger.info("%s socket reconnected to %s:%s", self.label, host, port)
            if self._on_reconnected is not None:
                self._client._spawn(self._on_reconnected(), f"uad-{self.label}-reconnect-hook")

    async def close(self) -> None:
        """Tear the socket down and fail anything still pending."""
        self._connected = False
        tasks = [t for t in (self._reader_task, self._reconnect_task) if t is not None]
        self._reader_task = None
        self._reconnect_task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        writer = self._writer
        self._writer = None
        self._reader = None
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 1.0)
        self._fail_all(UAConnectError(f"UAClient closed ({self.label} socket)"))

    def _drop_transport(self) -> None:
        writer = self._writer
        self._writer = None
        self._reader = None
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()

    def _on_reader_death(self, reason: str) -> None:
        self._connected = False
        self._drop_transport()
        self._fail_all(
            UAConnectError(
                f"lost connection to UA Mixer Engine on "
                f"{self._client.host}:{self._client.port} ({self.label} socket): {reason}"
            )
        )
        if not self._client.closed:
            self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        task = self._reconnect_task
        if task is not None and not task.done():
            return
        self._reconnect_task = self._client._spawn(
            self._reconnect_loop(), f"uad-{self.label}-reconnect"
        )

    async def _reconnect_loop(self) -> None:
        """Exponential backoff 0.5s -> 8s with jitter until the socket is back."""
        attempt = 0
        while not self._client.closed and not self._connected:
            delay = min(_BACKOFF_MAX, _BACKOFF_START * (2**attempt))
            delay = min(_BACKOFF_MAX, delay * (0.5 + random.random()))
            await asyncio.sleep(delay)
            if self._client.closed or self._connected:
                return
            try:
                await self.ensure_connected()
            except UAConnectError as exc:
                attempt += 1
                logger.debug(
                    "%s socket reconnect attempt %d failed: %s", self.label, attempt, exc
                )

    # -- framing -------------------------------------------------------------

    def _feed(self, chunk: bytes) -> list[dict[str, Any]]:
        """Accumulate bytes and return every complete frame decoded from the buffer.

        Tolerates partial frames, many frames per read, and multi-byte UTF-8
        sequences split across reads: decoding happens per frame, never per chunk.
        """
        self._buf.extend(chunk)
        messages: list[dict[str, Any]] = []
        while True:
            end = self._buf.find(0)
            if end < 0:
                break
            raw = bytes(self._buf[:end])
            del self._buf[: end + 1]
            if not raw.strip():
                continue
            try:
                msg = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise UAError(f"malformed frame from engine ({len(raw)} bytes): {exc}") from exc
            if isinstance(msg, dict) and "path" in msg:
                messages.append(msg)
            else:
                logger.debug("dropping frame with no path: %.120r", raw)
        if len(self._buf) > MAX_FRAME_BYTES:
            raise UAError(
                f"frame exceeds {MAX_FRAME_BYTES} bytes with no terminator "
                f"({len(self._buf)} bytes buffered)"
            )
        return messages

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        reason = "engine closed the connection"
        try:
            while True:
                chunk = await reader.read(_READ_CHUNK)
                if not chunk:
                    break
                for msg in self._feed(chunk):
                    self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            logger.warning("%s socket reader stopped: %s", self.label, reason)
        self._on_reader_death(reason)

    # -- dispatch ------------------------------------------------------------

    def _dispatch(self, msg: dict[str, Any]) -> None:
        """Give the frame to the FIRST waiter whose predicate accepts it."""
        for index, (predicate, future) in enumerate(self._waiters):
            if future.done():
                continue
            try:
                matched = predicate(msg)
            except Exception:
                logger.exception("waiter predicate raised on %s socket", self.label)
                matched = False
            if matched:
                del self._waiters[index]
                future.set_result(msg)
                return
        self._client._handle_broadcast(msg)

    def register_waiter(
        self, predicate: Callable[[dict[str, Any]], bool]
    ) -> asyncio.Future[dict[str, Any]]:
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiters.append((predicate, future))
        return future

    def drop_waiter(self, future: asyncio.Future[dict[str, Any]]) -> None:
        for index, (_, existing) in enumerate(self._waiters):
            if existing is future:
                del self._waiters[index]
                return

    def _fail_all(self, exc: UAConnectError) -> None:
        waiters, self._waiters = self._waiters, []
        for _, future in waiters:
            if not future.done():
                future.set_exception(exc)

    # -- io ------------------------------------------------------------------

    async def send(self, frames: Sequence[str]) -> None:
        """Write frames (NUL-terminated) in a single socket write."""
        if not frames:
            return
        payload = b"".join(frame.encode("utf-8") + b"\0" for frame in frames)
        async with self._write_lock:
            writer = self._writer
            if writer is None or not self._connected:
                raise UAConnectError(
                    f"not connected to UA Mixer Engine ({self.label} socket)"
                )
            try:
                writer.write(payload)
                await writer.drain()
            except (OSError, RuntimeError) as exc:
                raise UAConnectError(
                    f"write to UA Mixer Engine failed ({self.label} socket): {exc}"
                ) from exc
        self.frames_sent += len(frames)
        if logger.isEnabledFor(logging.DEBUG):
            for frame in frames:
                logger.debug(">> [%s] %.200s", self.label, frame)

    async def request(
        self,
        frame: str,
        predicate: Callable[[dict[str, Any]], bool],
        timeout_s: float,
        *,
        what: str,
    ) -> dict[str, Any]:
        """Send one frame and await the first reply its predicate accepts."""
        await self.ensure_connected()
        future = self.register_waiter(predicate)
        try:
            await self.send([frame])
            return await asyncio.wait_for(future, timeout_s)
        except TimeoutError as exc:
            raise UATimeoutError(
                f"no reply from UA Mixer Engine for {what} within {timeout_s:g}s"
            ) from exc
        finally:
            self.drop_waiter(future)


class UAClient:
    """Async client for the UA Mixer Engine JSON/NUL protocol on 127.0.0.1:4710."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        *,
        connect_timeout: float = 2.0,
        scalar_timeout: float = 2.0,
        tree_timeout: float = 15.0,
    ) -> None:
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.scalar_timeout = scalar_timeout
        self.tree_timeout = tree_timeout
        self._control = _Channel(self, "control", on_reconnected=self._after_reconnect)
        self._poll = _Channel(self, "poll")
        self._set_lock = asyncio.Lock()
        self._subscriptions: dict[str, list[Callable[[Any], None]]] = {}
        self._reconnect_cbs: list[Callable[[], Awaitable[None]]] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    # -- lifecycle -----------------------------------------------------------

    @property
    def closed(self) -> bool:
        """True once :meth:`aclose` has been called (terminal -- create a new client)."""
        return self._closed

    @property
    def connected(self) -> bool:
        """True when BOTH the control and poll sockets are up."""
        return self._control.connected and self._poll.connected

    async def connect(self) -> None:
        """Open both sockets. Idempotent; every public operation calls it."""
        if self._closed:
            raise UAConnectError("UAClient is closed")
        await self._control.ensure_connected()
        await self._poll.ensure_connected()

    async def aclose(self) -> None:
        """Close both sockets, cancel supervised tasks, fail pending waiters."""
        self._closed = True
        await self._control.close()
        await self._poll.close()
        tasks = list(self._tasks)
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._subscriptions.clear()

    async def __aenter__(self) -> UAClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def on_reconnect(self, cb: Callable[[], Awaitable[None]]) -> None:
        """Register a coroutine to run after the control socket reconnects."""
        self._reconnect_cbs.append(cb)

    def _spawn(self, coro: Awaitable[None], name: str) -> asyncio.Task[None]:
        task = asyncio.ensure_future(coro)
        task.set_name(name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _after_reconnect(self) -> None:
        """Re-arm subscriptions on the fresh socket, then fire user callbacks."""
        paths = list(self._subscriptions)
        if paths:
            try:
                await self._control.send([f"subscribe {path}" for path in paths])
            except UAConnectError as exc:
                logger.warning("could not re-subscribe after reconnect: %s", exc)
        for cb in list(self._reconnect_cbs):
            try:
                await cb()
            except Exception:
                logger.exception("on_reconnect callback failed")

    # -- broadcasts ----------------------------------------------------------

    def _handle_broadcast(self, msg: dict[str, Any]) -> None:
        """Route a frame that matched no waiter to subscription callbacks."""
        path = msg.get("path", "")
        callbacks = self._subscriptions.get(path)
        if callbacks is None and path.endswith(_VALUE_SUFFIX):
            callbacks = self._subscriptions.get(path[: -len(_VALUE_SUFFIX)])
        if not callbacks:
            if "error" in msg:
                logger.warning("unsolicited engine error for %s: %s", path, msg["error"])
            else:
                logger.debug("dropping unmatched broadcast for %s", path)
            return
        value = msg.get("data")
        for cb in list(callbacks):
            try:
                cb(value)
            except Exception:
                logger.exception("subscription callback for %s raised", path)

    # -- reads ---------------------------------------------------------------

    async def get_node(self, path: str) -> tuple[dict[str, UAProperty], list[str]]:
        """Return ``({property name: UAProperty}, [child name, ...])`` for a node."""
        path = _normalize(path)
        data = await self._get_node_raw(self._control, path, self.tree_timeout)
        properties = {
            name: UAProperty.from_reply(_join(path, name), meta)
            for name, meta in (data.get("properties") or {}).items()
            if isinstance(meta, dict)
        }
        return properties, list(data.get("children") or {})

    async def get_prop(self, path: str) -> UAProperty:
        """Return one scalar property with its metadata."""
        return await self._get_prop_on(self._control, _normalize(path), self.scalar_timeout)

    async def get_value(self, path: str) -> Any:
        """Return just the current value of one scalar property."""
        prop = await self.get_prop(path)
        return prop.value

    async def _get_node_raw(
        self, channel: _Channel, path: str, timeout_s: float
    ) -> dict[str, Any]:
        await self.connect()
        msg = await channel.request(
            f"get {path}", _path_predicate(path), timeout_s, what=f"get {path}"
        )
        if "error" in msg:
            raise UAPathError(f"{path}: {msg['error']}")
        data = msg.get("data")
        if not isinstance(data, dict):
            raise UAError(f"unexpected reply for get {path}: {msg!r}")
        if "properties" not in data and "children" not in data:
            raise UAError(f"{path} is a property, not a node")
        return data

    async def _get_prop_on(
        self, channel: _Channel, path: str, timeout_s: float
    ) -> UAProperty:
        await self.connect()
        msg = await channel.request(
            f"get {path}", _path_predicate(path), timeout_s, what=f"get {path}"
        )
        if "error" in msg:
            raise UAPathError(f"{path}: {msg['error']}")
        data = msg.get("data")
        if not isinstance(data, dict) or "type" not in data:
            raise UAError(f"{path} is not a scalar property (reply: {msg!r})")
        return UAProperty.from_reply(path, data)

    # -- writes --------------------------------------------------------------

    async def set_value(self, path: str, value: Any) -> Any:
        """Write ``value`` to a property, then verify it with a follow-up read.

        Sends ``set <path>/value <json>`` and awaits the ack on ``<path>/value``,
        then ALWAYS re-reads the property and returns the verified value (which may
        differ from ``value``: the engine clamps and quantizes silently). Serialized
        against every other ``set_value`` call by a client-global lock.
        """
        path = _normalize(path)
        value_path = f"{path}{_VALUE_SUFFIX}"
        frame = f"set {value_path} {json.dumps(value)}"
        async with self._set_lock:
            await self.connect()
            msg = await self._control.request(
                frame,
                _set_predicate(path, value_path),
                self.scalar_timeout,
                what=f"set {path}",
            )
            if "error" in msg:
                raise UAPathError(f"{path}: {msg['error']}")
            acked = msg.get("data")
            prop = await self._get_prop_on(self._control, path, self.scalar_timeout)
        if prop.value != value:
            logger.info(
                "set %s: requested %r, engine reports %r (ack %r)", path, value, prop.value, acked
            )
        return prop.value

    # -- subscriptions -------------------------------------------------------

    async def subscribe(self, path: str, cb: Callable[[Any], None]) -> None:
        """Subscribe to a property. ``cb`` gets the initial push, then every change.

        The callback is invoked from the reader task with the pushed value; it must
        not block. Exceptions raised by it are logged and swallowed.
        """
        path = _normalize(path)
        await self.connect()
        callbacks = self._subscriptions.setdefault(path, [])
        callbacks.append(cb)
        try:
            await self._control.send([f"subscribe {path}"])
        except UAConnectError:
            callbacks.remove(cb)
            if not callbacks:
                self._subscriptions.pop(path, None)
            raise

    async def unsubscribe(self, path: str) -> None:
        """Drop every callback for ``path`` and tell the engine to stop pushing."""
        path = _normalize(path)
        await self.connect()
        self._subscriptions.pop(path, None)
        await self._control.send([f"unsubscribe {path}"])

    # -- bulk ----------------------------------------------------------------

    async def poll_values(self, paths: list[str]) -> dict[str, Any]:
        """Read many property values in one batch on the poll socket.

        Every request is written in a single socket write and all replies awaited
        together, so bulk meter reads never queue behind control-socket waiters.
        Paths the engine errors on (or does not answer in time) are omitted from the
        result; a batch the engine answered nothing at all for raises
        :class:`UATimeoutError`.
        """
        if not paths:
            return {}
        await self.connect()
        wanted = [_normalize(p) for p in paths]
        unique = list(dict.fromkeys(wanted))
        futures: list[tuple[str, asyncio.Future[dict[str, Any]]]] = [
            (path, self._poll.register_waiter(_path_predicate(path))) for path in unique
        ]
        try:
            await self._poll.send([f"get {path}" for path in unique])
        except UAConnectError:
            for _, future in futures:
                self._poll.drop_waiter(future)
                future.cancel()
            raise
        await asyncio.wait([future for _, future in futures], timeout=self.scalar_timeout)
        values: dict[str, Any] = {}
        replies = 0
        connect_error: UAConnectError | None = None
        for path, future in futures:
            if not future.done():
                self._poll.drop_waiter(future)
                future.cancel()
                logger.debug("poll_values: no reply for %s", path)
                continue
            exc = future.exception()
            if exc is not None:
                if isinstance(exc, UAConnectError) and connect_error is None:
                    connect_error = exc
                continue
            replies += 1
            msg = future.result()
            if "error" in msg:
                logger.debug("poll_values: %s -> %s", path, msg["error"])
                continue
            try:
                values[path] = _extract_value(msg)
            except UAError as exc:
                logger.debug("poll_values: %s", exc)
        if connect_error is not None:
            raise connect_error
        if replies == 0:
            raise UATimeoutError(
                f"no reply for any of {len(unique)} polled path(s) within "
                f"{self.scalar_timeout:g}s"
            )
        return values

    async def walk(
        self,
        root: str = "/",
        *,
        max_nodes: int = 25000,
        progress: Callable[[int], None] | None = None,
    ) -> dict[str, dict]:
        """Walk the tree breadth-first and return a fixture-format ``nodes`` mapping.

        ``{"<path>": {"properties": {<name>: <raw metadata>}, "children": [<name>]}}``.
        Runs on the poll socket. Individual nodes that error or time out are skipped
        with a warning; the whole walk is bounded by ``tree_timeout``.
        """
        await self.connect()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.tree_timeout
        semaphore = asyncio.Semaphore(_WALK_CONCURRENCY)
        nodes: dict[str, dict] = {}
        frontier = [_normalize(root)]
        seen: set[str] = set(frontier)
        while frontier:
            if len(nodes) >= max_nodes:
                logger.warning("walk stopped at max_nodes=%d (the tree is larger)", max_nodes)
                break
            room = max_nodes - len(nodes)
            level, frontier = frontier[:room], frontier[room:]
            results = await asyncio.gather(
                *(self._walk_node(path, semaphore, deadline) for path in level),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
                path, data = result
                if data is None:
                    continue
                children = list(data.get("children") or {})
                nodes[path] = {
                    "properties": data.get("properties") or {},
                    "children": children,
                }
                for child in children:
                    child_path = _join(path, child)
                    if child_path not in seen:
                        seen.add(child_path)
                        frontier.append(child_path)
            if progress is not None:
                try:
                    progress(len(nodes))
                except Exception:
                    logger.exception("walk progress callback failed")
        return nodes

    async def _walk_node(
        self, path: str, semaphore: asyncio.Semaphore, deadline: float
    ) -> tuple[str, dict[str, Any] | None]:
        async with semaphore:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise UATimeoutError(
                    f"tree walk exceeded tree_timeout={self.tree_timeout:g}s at {path}"
                )
            try:
                data = await self._get_node_raw(
                    self._poll, path, min(self.scalar_timeout, remaining)
                )
            except UAConnectError:
                raise
            except UAError as exc:
                logger.warning("walk: skipping %s (%s)", path, exc)
                return path, None
            return path, data
