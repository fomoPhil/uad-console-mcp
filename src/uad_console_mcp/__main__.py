"""Command line entry point (docs/SPEC.md §4.5).

Four modes:

* default -- serve MCP over stdio. **Nothing may be written to stdout in this mode** except the
  MCP protocol itself, so logging is configured onto stderr before anything else happens.
* ``--check`` -- a doctor: is the engine reachable, which devices are attached and online, what
  the clock and sample rate are, how many plugins are installed, and the effective safety limits.
* ``--dump-tree [FILE]`` -- walk the whole tree into the fixture format used by the test suite,
  with the home directory scrubbed out of string values before it is written anywhere.
* ``--version``.

``--host``/``--port`` exist so the doctor and the tree dump can be pointed at a mock engine; the
defaults are the real UA Mixer Engine on 127.0.0.1:4710.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

import anyio

from . import __version__
from .client import DEFAULT_HOST, DEFAULT_PORT, UAClient, UAError
from .discovery import discover
from .safety import DENIED_PROPERTY_SUBSTRINGS, ENV_MODE, gate_from_env
from .server import CONNECT_HELP, ServerContext, build_server

logger = logging.getLogger("uad_console_mcp")

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
HOME_PLACEHOLDER = "~"
DUMP_STDOUT = "-"
CONFIG_ERROR_EXIT = 2

REDACTED_NAME_SUBSTRINGS: tuple[str, ...] = (*DENIED_PROPERTY_SUBSTRINGS, "serial")
"""Property names whose VALUE never leaves this machine: the safety denylist plus serials."""

REDACTED_VALUE_KEYS = ("value", "default")


def _configure_logging(level: str) -> None:
    """Send every log record to stderr. stdout belongs to the MCP protocol."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=LOG_FORMAT,
        stream=sys.stderr,
        force=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uad-console-mcp",
        description=(
            "Unofficial MCP server for Universal Audio Apollo interfaces. With no arguments it "
            "serves MCP over stdio."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="run diagnostics against the engine and exit (does not write anything)",
    )
    parser.add_argument(
        "--dump-tree",
        nargs="?",
        const=DUMP_STDOUT,
        metavar="FILE",
        help=(
            "walk the engine tree and write it in test-fixture format to FILE "
            f"(or stdout with {DUMP_STDOUT!r}); the home directory is scrubbed from values"
        ),
    )
    parser.add_argument(
        "--host", default=DEFAULT_HOST, help=f"engine host (default {DEFAULT_HOST})"
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"engine port (default {DEFAULT_PORT})"
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="logging verbosity on stderr (default INFO)",
    )
    parser.add_argument("--version", action="version", version=f"uad-console-mcp {__version__}")
    return parser


# ------------------------------------------------------------------------------ sanitizing


def is_redacted_name(name: Any) -> bool:
    """True for property names that identify the hardware (serials, firmware, hardware IDs)."""
    folded = str(name).casefold()
    return any(needle in folded for needle in REDACTED_NAME_SUBSTRINGS)


def _redacted_scalar(value: Any) -> Any:
    """``0`` for numbers, ``"0"`` for everything else. Keeps the shape, drops the identity."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return 0
    return "0"


def _redact(item: Any, root: str) -> Any:
    """Blank the value of one identifying property, keeping its metadata intact."""
    if isinstance(item, dict) and any(key in item for key in REDACTED_VALUE_KEYS):
        return {
            key: _redacted_scalar(sub) if key in REDACTED_VALUE_KEYS else sanitize(sub, root)
            for key, sub in item.items()
        }
    if isinstance(item, dict | list):
        return sanitize(item, root)
    return _redacted_scalar(item)


def sanitize(value: Any, home: str | None = None) -> Any:
    """Scrub a captured tree before it is written anywhere, recursively.

    Two things go: the home directory (preset browser paths and volume names leak the user's
    login name straight into a file people paste into issues), and the VALUE of any property
    whose name identifies the hardware - serial numbers, firmware versions, hardware IDs. A
    dumped tree is meant to be shareable as a fixture.
    """
    root = str(Path.home()) if home is None else home
    if isinstance(value, str):
        return value.replace(root, HOME_PLACEHOLDER) if root and root in value else value
    if isinstance(value, dict):
        return {
            key: _redact(item, root) if is_redacted_name(key) else sanitize(item, root)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize(item, root) for item in value]
    return value


# --------------------------------------------------------------------------------- doctor


async def doctor(client: UAClient) -> tuple[int, list[str]]:
    """Diagnostics for ``--check``. Returns ``(exit code, lines to print)``."""
    lines: list[str] = [f"uad-console-mcp {__version__}"]
    gate = gate_from_env()
    limits = gate.describe()
    try:
        root, _ = await client.get_node("/")
    except UAError as exc:
        lines.append(f"engine:        UNREACHABLE at {client.host}:{client.port}")
        lines.append(f"               {CONNECT_HELP.format(host=client.host, port=client.port)}")
        lines.append(f"               detail: {exc}")
        lines.append(f"safety mode:   {limits['mode']} (from {limits['mode_env_var']})")
        return 1, lines

    def value(name: str) -> Any:
        prop = root.get(name)
        return None if prop is None else prop.value

    lines.append(f"engine:        reachable at {client.host}:{client.port}")
    lines.append(
        f"               UAD version {value('Version')}, "
        f"protocol {value('ProtocolVersion')}"
    )
    lines.append(f"clock:         {value('ClockSource')}, locked={value('ClockLocked')}")
    lines.append(f"sample rate:   {value('SampleRate')} Hz")
    dsp = value("TotalDSPLoad")
    if isinstance(dsp, int | float):
        lines.append(
            f"dsp load:      {float(dsp):.1f}% dsp, "
            f"{float(value('TotalPGMLoad') or 0):.1f}% program"
        )

    exit_code = 0
    try:
        rig = await discover(client)
    except UAError as exc:
        lines.append(f"devices:       could not enumerate: {exc}")
        return 1, lines
    if not rig.devices:
        lines.append("devices:       none found (is an Apollo attached and powered on?)")
        exit_code = 1
    for device in rig.devices:
        channels = [c for c in rig.channels if c.device == device.index]
        state = "online" if device.online else "OFFLINE"
        counts = ", ".join(
            f"{len([c for c in channels if c.kind == kind])} {kind}s"
            for kind in ("input", "aux", "output")
        )
        lines.append(
            f"device {device.index}:      {device.name} ({device.type_id}) {state}: {counts}"
        )
        if not device.online:
            exit_code = 1
    try:
        monitor = rig.monitor_output()
        lines.append(f"monitor out:   {monitor.name} at {monitor.path}")
    except UAError as exc:
        lines.append(f"monitor out:   not identified: {exc}")

    try:
        _, plugins = await client.get_node("/plugins")
        lines.append(f"plugins:       {len(plugins)} installed")
    except UAError as exc:
        lines.append(f"plugins:       could not read the catalog: {exc}")

    lines.append(f"safety mode:   {limits['mode']} (from {limits['mode_env_var']})")
    lines.append(
        f"monitor rail:  ceiling {limits['max_monitor_db']:+.1f} dB, "
        f"max +{limits['max_monitor_step_db']:.1f} dB per call, "
        f"+{limits['monitor_window_db']:.1f} dB per {limits['monitor_window_seconds']:.0f} s"
    )
    lines.append(
        f"write limit:   {limits['writes_per_second']:g}/s, burst {limits['write_burst']}"
    )
    lines.append(
        "raw set tool:  "
        + ("registered (full mode)" if limits["mode"] == "full" else "not registered")
    )
    return exit_code, lines


async def _run_check(host: str, port: int) -> int:
    client = UAClient(host, port)
    try:
        code, lines = await doctor(client)
    finally:
        await client.aclose()
    for line in lines:
        print(line)
    return code


# ------------------------------------------------------------------------------ tree dump


async def dump_tree(client: UAClient) -> dict[str, Any]:
    """Walk everything and return a fixture-format document with values sanitized."""
    nodes = await client.walk("/", progress=lambda count: logger.info("walked %d nodes", count))
    return {
        "_meta": {
            "device": "unknown",
            "captured_via": f"uad-console-mcp {__version__} --dump-tree (read-only)",
            "node_count": len(nodes),
            "errors": [],
        },
        "nodes": sanitize(nodes),
    }


async def _walk_for_dump(host: str, port: int) -> dict[str, Any]:
    client = UAClient(host, port)
    try:
        return await dump_tree(client)
    finally:
        await client.aclose()


def run_dump(host: str, port: int, target: str) -> int:
    """Walk the tree, then write it. Kept synchronous so the file I/O is not inside the loop."""
    try:
        document = asyncio.run(_walk_for_dump(host, port))
    except UAError as exc:
        print(CONNECT_HELP.format(host=host, port=port), file=sys.stderr)
        print(f"detail: {exc}", file=sys.stderr)
        return 1
    payload = json.dumps(document, indent=1, ensure_ascii=False)
    if target == DUMP_STDOUT:
        print(payload)
        return 0
    path = Path(target).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    print(f"wrote {len(document['nodes'])} nodes to {path}", file=sys.stderr)
    return 0


# ----------------------------------------------------------------------------------- serve


def serve(host: str, port: int) -> int:
    """Build the server and run it on stdio. Blocks until the client disconnects."""
    client = UAClient(host, port)
    ctx = ServerContext(client)
    server = build_server(ctx)
    logger.info(
        "serving MCP on stdio; engine %s:%s, safety mode %s", host, port, ctx.gate.mode
    )
    try:
        asyncio.run(_serve(server, ctx, client))
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        logger.info("interrupted; shutting down")
    return 0


async def _serve(server: Any, ctx: ServerContext, client: UAClient) -> None:
    """Serve, then clean up in the SAME event loop, so shutdown can still reach the engine."""
    try:
        await server.run_stdio_async()
    finally:
        with anyio.CancelScope(shield=True):
            await ctx.shutdown()
            await client.aclose()


# ------------------------------------------------------------------------------------ main


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.log_level)

    if args.check and args.dump_tree is not None:
        parser.error("--check and --dump-tree do different things; run them one at a time")

    # Validate the environment before anything connects: an unusable UAD_MCP_MODE or limit is a
    # configuration error, and a traceback is not how you tell someone they typo'd an env var.
    try:
        gate_from_env()
    except ValueError as exc:
        print(f"uad-console-mcp: {exc}", file=sys.stderr)
        print(f"(configuration comes from the environment; see {ENV_MODE})", file=sys.stderr)
        return CONFIG_ERROR_EXIT

    if args.check:
        return asyncio.run(_run_check(args.host, args.port))
    if args.dump_tree is not None:
        return run_dump(args.host, args.port, args.dump_tree)
    return serve(args.host, args.port)


if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(main())
