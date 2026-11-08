"""Shared pytest fixtures for the uad-console-mcp test suite.

Nothing here ever touches 127.0.0.1:4710. Every engine is a ``MockEngine`` on an
ephemeral loopback port.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
for _path in (REPO_ROOT, SRC_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from tests.fixtures.derive import load_base  # noqa: E402
from tests.mock_engine import MockEngine  # noqa: E402

MockFactory = Callable[..., Awaitable[MockEngine]]


@pytest.fixture(autouse=True)
def rig_profile_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the rig profile at a tmp file, so no test reads or writes the real one."""
    path = tmp_path / "rig-profile.json"
    monkeypatch.setenv("UAD_MCP_RIG_PROFILE", str(path))
    return path


@pytest.fixture(scope="session")
def base_fixture() -> dict[str, Any]:
    """The captured Apollo x8 Gen 2 tree, loaded once per session.

    ``MockEngine`` deep-copies whatever it is handed, so this stays pristine.
    """
    return load_base()


@pytest.fixture
async def mock(base_fixture: dict[str, Any]) -> AsyncIterator[MockEngine]:
    """A started ``MockEngine`` serving the x8 fixture with no adversarial behaviour."""
    engine = MockEngine(base_fixture)
    await engine.start()
    try:
        yield engine
    finally:
        await engine.stop()


@pytest.fixture
async def mock_factory(base_fixture: dict[str, Any]) -> AsyncIterator[MockFactory]:
    """Factory for adversarial engines: ``await mock_factory(fixture, split_frames=True)``.

    ``fixture`` defaults to the x8 base fixture. Every engine created is stopped on
    teardown.
    """
    engines: list[MockEngine] = []

    async def factory(fixture: dict[str, Any] | None = None, **kwargs: Any) -> MockEngine:
        engine = MockEngine(base_fixture if fixture is None else fixture, **kwargs)
        await engine.start()
        engines.append(engine)
        return engine

    try:
        yield factory
    finally:
        for engine in engines:
            await engine.stop()
