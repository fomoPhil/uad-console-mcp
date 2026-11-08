"""The rig profile: what is plugged in where, which the engine cannot know (docs/SPEC.md §4.8).

The engine knows strips, not microphones. An agent that has to ask "what mic is on input 1?"
every session cannot know whether 48V is safe, which input is the user's voice, or which virtual
pair carries system audio. The profile is a small JSON file the user (through an agent) fills in
once, so every later session starts from the user's real setup.

It is read by:

* ``uad_set_preamp``: a source marked ``mic_type: "ribbon"`` refuses 48V even with
  ``confirm_condenser_mic=true``, until the profile says otherwise.
* ``uad_mode``: roles (``voice``, ``instrument``, ``system_audio``, ``call_guest``) are the
  defaults for which inputs a mode works on.

Sources are stored by ``(device, index)``, never by name: modes and users rename strips, and the
index is what stays put. The name at save time is kept only so a human can read the file.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .client import UAError
from .discovery import ChannelInfo, Rig

logger = logging.getLogger("uad_console_mcp.profile")

ENV_PROFILE_PATH = "UAD_MCP_RIG_PROFILE"
DEFAULT_PROFILE_PATH = Path("~/.uad-console-mcp/rig-profile.json")
SCHEMA_VERSION = 1

ROLES = ("voice", "instrument", "system_audio", "call_guest", "other")
"""What a source is for. Modes read the first four; ``other`` is just a label."""

MIC_TYPES = ("dynamic", "condenser", "ribbon", "line", "unknown")
"""``line`` means no microphone at all (a synth, a DI, a converter)."""

SOURCE_KEYS = ("target", "device", "plugged_in", "mic_type", "role", "notes")
MAX_TEXT = 200
MAX_NOTES = 2000


class ProfileError(UAError):
    """The profile could not be read, validated or written. The message says how to fix it."""


@dataclass(frozen=True)
class Source:
    """One input the user has described."""

    device: int
    index: int
    name: str
    plugged_in: str = ""
    mic_type: str = ""
    role: str = ""
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"device": self.device, "index": self.index, "name": self.name}
        for key in ("plugged_in", "mic_type", "role", "notes"):
            value = getattr(self, key)
            if value:
                out[key] = value
        return out


@dataclass
class Profile:
    sources: list[Source] = field(default_factory=list)
    notes: str = ""
    device_types: list[str] = field(default_factory=list)
    updated: str | None = None

    def source(self, device: int, index: int) -> Source | None:
        for item in self.sources:
            if item.device == device and item.index == index:
                return item
        return None

    def with_role(self, role: str) -> list[Source]:
        return [item for item in self.sources if item.role == role]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "updated": self.updated,
            "device_types": list(self.device_types),
            "sources": [item.as_dict() for item in self.sources],
            "notes": self.notes,
        }


def profile_path(env: Mapping[str, str] | None = None) -> Path:
    raw = (env if env is not None else os.environ).get(ENV_PROFILE_PATH)
    return Path(raw).expanduser() if raw else DEFAULT_PROFILE_PATH.expanduser()


def _text(value: Any, key: str, limit: int = MAX_TEXT) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ProfileError(f"{key} must be text, got {value!r}.")
    text = value.strip()
    if len(text) > limit:
        raise ProfileError(f"{key} is {len(text)} characters; keep it under {limit}.")
    return text


def _choice(value: Any, key: str, allowed: Sequence[str]) -> str:
    text = _text(value, key).casefold()
    if text and text not in allowed:
        raise ProfileError(f"{key} {value!r} is not one of: {', '.join(allowed)}.")
    return text


def build_sources(items: Sequence[Mapping[str, Any]], rig: Rig) -> list[Source]:
    """Validate agent-supplied source items against the live rig. All or nothing."""
    sources: list[Source] = []
    seen: set[tuple[int, int]] = set()
    for position, item in enumerate(items, start=1):
        if not isinstance(item, Mapping):
            raise ProfileError(f"source {position} must be an object, got {item!r}.")
        extra = sorted(str(key) for key in item if key not in SOURCE_KEYS)
        if extra:
            raise ProfileError(
                f"source {position} has unknown key(s) {', '.join(map(repr, extra))}. Allowed: "
                f"{', '.join(SOURCE_KEYS)}. Nothing was saved."
            )
        if item.get("target") in (None, ""):
            raise ProfileError(
                f"source {position} needs a 'target': the input's current name or index."
            )
        channel: ChannelInfo = rig.resolve(item["target"], device=item.get("device"))
        key = (channel.device, channel.index)
        if key in seen:
            raise ProfileError(f"{channel.label()} is listed twice. Merge the two entries.")
        seen.add(key)
        mic_type = _choice(item.get("mic_type"), "mic_type", MIC_TYPES)
        if mic_type and mic_type not in ("line", "unknown") and not channel.has_preamp:
            raise ProfileError(
                f"{channel.label()} has no preamp, so it cannot have a {mic_type} microphone on "
                "it directly. Use mic_type 'line' for a converter or expander channel, and put "
                "the microphone in plugged_in."
            )
        sources.append(
            Source(
                device=channel.device,
                index=channel.index,
                name=channel.name,
                plugged_in=_text(item.get("plugged_in"), "plugged_in"),
                mic_type=mic_type,
                role=_choice(item.get("role"), "role", ROLES),
                notes=_text(item.get("notes"), "notes"),
            )
        )
    return sources


class ProfileStore:
    """One JSON file. A missing file is an empty profile, never an error."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path).expanduser() if path is not None else profile_path()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def exists(self) -> bool:
        return self._path.is_file()

    def load(self) -> Profile:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return Profile()
        except OSError as exc:
            raise ProfileError(f"Could not read the rig profile at {self._path}: {exc}") from exc
        try:
            doc = json.loads(raw)
        except ValueError as exc:
            raise ProfileError(
                f"The rig profile at {self._path} is not valid JSON ({exc}). Save a new one with "
                "uad_rig_profile(action='save', ...)."
            ) from exc
        if not isinstance(doc, dict) or doc.get("schema_version") != SCHEMA_VERSION:
            raise ProfileError(
                f"The rig profile at {self._path} has an unknown schema. Save a new one with "
                "uad_rig_profile(action='save', ...)."
            )
        sources = []
        for item in doc.get("sources") or []:
            try:
                sources.append(
                    Source(
                        device=int(item["device"]),
                        index=int(item["index"]),
                        name=str(item.get("name") or ""),
                        plugged_in=str(item.get("plugged_in") or ""),
                        mic_type=str(item.get("mic_type") or ""),
                        role=str(item.get("role") or ""),
                        notes=str(item.get("notes") or ""),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ProfileError(
                    f"The rig profile at {self._path} has a broken source entry {item!r}. Save "
                    "a new one with uad_rig_profile(action='save', ...)."
                ) from exc
        return Profile(
            sources=sources,
            notes=str(doc.get("notes") or ""),
            device_types=[str(d) for d in doc.get("device_types") or []],
            updated=doc.get("updated"),
        )

    def save(self, profile: Profile) -> Path:
        profile.updated = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        payload = json.dumps(profile.as_dict(), indent=1, ensure_ascii=False)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_suffix(".json.tmp")
            temporary.write_text(payload, encoding="utf-8")
            temporary.replace(self._path)
        except OSError as exc:
            raise ProfileError(f"Could not write the rig profile to {self._path}: {exc}") from exc
        logger.info("saved rig profile (%d sources) to %s", len(profile.sources), self._path)
        return self._path


def notes_text(value: Any) -> str:
    return _text(value, "notes", MAX_NOTES)
