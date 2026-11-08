"""Workflow modes: one call that sets the Console up for a job (docs/SPEC.md §4.9).

A mode is a recipe, not a saved state. It works out which strips matter from the arguments, then
the rig profile, then safe defaults, and turns the job into ordinary ``uad_set_channels`` and
``uad_set_sends`` items. The server runs those through the same tools as any other write, so a
mode can never reach past the safety gate, and it saves a snapshot first so it can be undone.

Rules every mode follows:

* It only opens or closes what the job needs. A fader is moved only when it is off (at or below
  -60 dB), so the user's own balance survives.
* It never touches the monitor output, 48V, gain, the clock or the sample rate. It warns instead.
* It never guesses a microphone. No mic named and none in the profile is a question, not a pick.

Everything here is pure; ``server.tool_mode`` gathers the live facts and applies the result.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import routing
from .discovery import ChannelInfo, Rig
from .profile import Profile

OFF_DB = -60.0
"""A fader or send at or below this is treated as off, and is the only kind a mode will move."""

SEND_OFF_DB = -144.0
VOICE_DB = 0.0
SYSTEM_AUDIO_DB = -10.0
"""Computer audio starts 10 dB under the voice, which is where a music bed or game sits."""
CUE_SEND_DB = -6.0
NONE_WORDS = frozenset({"none", "off", "no", "false", ""})
HARDWARE_EXCLUDED_IO = frozenset({"Virtual", "TalkbackMic"})

MODES: dict[str, dict[str, Any]] = {
    "livestream": {
        "does": (
            "Mic(s) and computer audio in one Console mix that a stream app records from the "
            "Apollo's monitor-mix inputs (MON L/R). With a guest, builds a mix-minus on an aux "
            "bus for the call app."
        ),
        "arguments": {
            "inputs": "your mic(s); default: profile role 'voice'",
            "system_audio": "the virtual strip macOS plays to; default: profile, then VIRTUAL 1; "
            "'none' to leave it out",
            "guest": "the strip a call app plays into; default: profile role 'call_guest'",
            "bus": "aux bus for the guest's mix-minus; default 'aux2'",
        },
    },
    "tracking": {
        "does": (
            "Record a mic or instrument: open it in the Console mix, put it in a headphone cue "
            "and clear stray solos. Gain and 48V are left to you."
        ),
        "arguments": {
            "inputs": "what you are recording; default: profile roles 'voice' and 'instrument'",
            "bus": "the headphone cue; default 'cue1'",
        },
    },
    "mixing": {
        "does": (
            "Mix in the DAW: mute every hardware input in the Console mix (mics, line, ADAT, "
            "S/PDIF) and clear solos, so only playback reaches the monitors. Virtual inputs stay "
            "open."
        ),
        "arguments": {},
    },
}

NEVER = (
    "raises or unmutes the monitor output",
    "switches 48V, gain, pad or phase",
    "changes the clock, the sample rate or the I/O Matrix",
    "moves a fader or send that is already up",
)


@dataclass(frozen=True)
class StripState:
    mute: bool | None = None
    solo: bool | None = None
    fader_db: float | None = None
    hidden: bool | None = None
    phantom: bool | None = None


@dataclass
class Selection:
    mode: str
    device: int
    inputs: list[ChannelInfo] = field(default_factory=list)
    system_audio: ChannelInfo | None = None
    guest: ChannelInfo | None = None
    bus: str | None = None
    questions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def strips(self) -> list[ChannelInfo]:
        picked = [*self.inputs, self.system_audio, self.guest]
        return [channel for channel in picked if channel is not None]


@dataclass
class Facts:
    """Live values the planner needs. Gathered by the server, never cached."""

    strips: dict[str, StripState]
    bus_sends: dict[str, float | None] = field(default_factory=dict)
    bus_plugins: list[str] = field(default_factory=list)
    routing: dict[str, Any] = field(default_factory=dict)
    monitor_muted: bool | None = None
    headphones: dict[str, str] = field(default_factory=dict)


def describe() -> list[dict[str, Any]]:
    return [{"mode": name, **spec} for name, spec in MODES.items()]


# ------------------------------------------------------------------------------- selection


def _is_none(value: Any) -> bool:
    return isinstance(value, str) and value.strip().casefold() in NONE_WORDS


def _from_profile(
    rig: Rig, profile: Profile, roles: Sequence[str], device: int
) -> list[ChannelInfo]:
    picked = []
    for source in profile.sources:
        if source.role not in roles or source.device != device:
            continue
        for channel in rig.channels_of("input", device):
            if channel.index == source.index:
                picked.append(channel)
    return picked


def _mic_list(rig: Rig, device: int) -> str:
    names = [c.name for c in rig.channels_of("input", device) if c.has_preamp]
    return ", ".join(names) or "none found"


def select(
    mode: str,
    rig: Rig,
    profile: Profile,
    *,
    device: int | None = None,
    inputs: Sequence[str | int] | None = None,
    system_audio: str | int | None = None,
    guest: str | int | None = None,
    bus: str | None = None,
) -> Selection:
    """Work out which strips a mode acts on. Raises on a bad target; asks when it cannot know."""
    name = (mode or "").strip().casefold()
    if name not in MODES:
        raise ValueError(f"mode {mode!r} is not one of: {', '.join(MODES)}.")
    if device is None:
        device = min(rig.devices, key=lambda d: d.multi_unit_order).index if rig.devices else 0
    else:
        rig.device(device)
    picked = Selection(mode=name, device=device)

    def resolve(target: str | int) -> ChannelInfo:
        return rig.resolve(target, device=device)

    if name == "mixing":
        extra = [
            key
            for key, value in (
                ("inputs", inputs),
                ("system_audio", system_audio),
                ("guest", guest),
                ("bus", bus),
            )
            if value not in (None, [])
        ]
        if extra:
            raise ValueError(
                f"mixing takes no {', '.join(extra)}: it mutes every hardware input. Leave "
                "them out."
            )
        return picked

    if name == "tracking" and (system_audio is not None or guest is not None):
        raise ValueError("tracking takes no system_audio or guest. Leave them out.")

    roles = ("voice",) if name == "livestream" else ("voice", "instrument")
    if inputs:
        picked.inputs = [resolve(target) for target in inputs]
    else:
        picked.inputs = _from_profile(rig, profile, roles, device)
        if picked.inputs:
            picked.notes.append(
                "Inputs from your rig profile: " + ", ".join(c.name for c in picked.inputs) + "."
            )
        else:
            what = "microphone" if name == "livestream" else "mic or instrument"
            picked.questions.append(
                f"Which input is your {what}? Inputs with a preamp: {_mic_list(rig, device)}. "
                "Pass inputs=[...], or save it once with uad_rig_profile so you are not asked "
                "again."
            )

    if name == "tracking":
        cue = (bus or "cue1").strip().casefold()
        if not (cue.startswith("cue") and cue[3:].isdigit()):
            raise ValueError(f"tracking needs a cue bus such as 'cue1', not {bus!r}.")
        count = rig.device(device).cue_bus_count
        if int(cue[3:]) > count or int(cue[3:]) < 1:
            raise ValueError(
                f"{cue} does not exist on this device, which has {count} cue bus(es). Use "
                + ("cue1." if count <= 1 else f"cue1..cue{count}.")
            )
        picked.bus = cue
        return picked

    # livestream
    if system_audio is not None and _is_none(system_audio):
        picked.system_audio = None
    elif system_audio is not None:
        picked.system_audio = resolve(system_audio)
    else:
        from_profile = _from_profile(rig, profile, ("system_audio",), device)
        if from_profile:
            picked.system_audio = from_profile[0]
        else:
            virtual = [c for c in rig.channels_of("input", device) if c.io_type == "Virtual"]
            if virtual:
                picked.system_audio = virtual[0]
                picked.notes.append(
                    f"Assumed computer audio arrives on {virtual[0].name}, the usual pair. Pass "
                    "system_audio, or save role 'system_audio' in the rig profile, if it is "
                    "another one."
                )
    if guest is not None and not _is_none(guest):
        picked.guest = resolve(guest)
    elif guest is None:
        from_profile = _from_profile(rig, profile, ("call_guest",), device)
        picked.guest = from_profile[0] if from_profile else None
    if picked.guest is not None:
        call_bus = (bus or "aux2").strip().casefold()
        if call_bus not in ("aux1", "aux2"):
            raise ValueError(
                f"the guest's mix-minus needs an aux bus, 'aux1' or 'aux2', not {bus!r}."
            )
        picked.bus = call_bus
        clash = [
            c.name
            for c in (*picked.inputs, picked.system_audio)
            if c and c.path == picked.guest.path
        ]
        if clash:
            picked.questions.append(
                f"The guest and {clash[0]} are the same strip, so the guest would hear "
                "themselves. The call app needs its own strip (usually its own virtual pair): "
                "pass a different guest."
            )
    elif bus is not None:
        raise ValueError("bus is only used for a guest's mix-minus. Pass guest, or leave bus out.")
    return picked


# --------------------------------------------------------------------------------- planning


class _Items:
    """Channel items keyed by strip, merging fields, keeping only what actually changes."""

    def __init__(self, facts: Facts) -> None:
        self._facts = facts
        self._items: dict[str, dict[str, Any]] = {}

    def want(self, channel: ChannelInfo, **fields: Any) -> None:
        state = self._facts.strips.get(channel.path, StripState())
        changes = {}
        for key, value in fields.items():
            current = getattr(state, key)
            if key == "fader_db":
                if current is not None and abs(float(current) - float(value)) < 0.05:
                    continue
            elif current is not None and current == value:
                continue
            changes[key] = value
        if not changes:
            return
        item = self._items.setdefault(
            channel.path,
            {
                "target": channel.index,
                "device": channel.device,
                **({"kind": channel.kind} if channel.kind != "input" else {}),
            },
        )
        item.update(changes)

    def open(self, channel: ChannelInfo, default_db: float) -> None:
        """Unmute and show a strip; raise its fader only if it is off."""
        state = self._facts.strips.get(channel.path, StripState())
        fields: dict[str, Any] = {"mute": False, "hidden": False}
        if state.fader_db is None or state.fader_db <= OFF_DB:
            fields["fader_db"] = default_db
        self.want(channel, **fields)

    def list(self) -> list[dict[str, Any]]:
        return list(self._items.values())


def _clear_solos(items: _Items, rig: Rig, facts: Facts, device: int) -> list[str]:
    cleared = []
    for kind in ("input", "aux"):
        for channel in rig.channels_of(kind, device):
            if facts.strips.get(channel.path, StripState()).solo:
                items.want(channel, solo=False)
                cleared.append(channel.name)
    return cleared


def _phantom_warnings(selection: Selection, profile: Profile, facts: Facts) -> list[str]:
    urgent: list[str] = []
    warnings: list[str] = []
    for channel in selection.inputs:
        if not channel.has_preamp:
            continue
        source = profile.source(channel.device, channel.index)
        phantom = facts.strips.get(channel.path, StripState()).phantom
        if source is None:
            continue
        if source.mic_type == "ribbon" and phantom:
            urgent.append(
                f"48V is ON at {channel.name}, and your rig profile says a ribbon mic "
                f"({source.plugged_in or 'unnamed'}) is plugged in there. Turn it off now: "
                f"uad_set_preamp(target={channel.index}, phantom_48v=false)."
            )
        elif source.mic_type == "condenser" and phantom is False:
            warnings.append(
                f"48V is off at {channel.name}, and your rig profile says a condenser "
                f"({source.plugged_in or 'unnamed'}) is plugged in there, so it will be silent. "
                "Ask the user to confirm the mic is still that condenser, then "
                f"uad_set_preamp(target={channel.index}, phantom_48v=true, "
                "confirm_condenser_mic=true)."
            )
    return urgent + warnings  # a ribbon under 48V comes first, whatever the input order


def _span(info: Mapping[str, Any] | None) -> str:
    if not info:
        return "?"
    channels = info["channels"]
    return f"{channels[0]}-{channels[-1]}" if len(channels) > 1 else str(channels[0])


def _virtual_ordinal(rig: Rig, channel: ChannelInfo) -> int | None:
    virtual = [c for c in rig.channels_of("input", channel.device) if c.io_type == "Virtual"]
    for position, item in enumerate(virtual, start=1):
        if item.path == channel.path:
            return position
    return None


def _virtual_pair(
    rig: Rig, facts: Facts, channel: ChannelInfo, warnings: list[str]
) -> dict[str, Any] | None:
    """The driver outputs that play into a Virtual strip, warning if the I/O Matrix has them off."""
    ordinal = _virtual_ordinal(rig, channel)
    if ordinal is None:
        return None
    pair = routing.virtual_output(facts.routing.get("driver_outputs") or [], ordinal)
    if pair is not None and not pair["active"]:
        warnings.append(
            f"{' and '.join(pair['names'])} are disabled in Console's I/O Matrix, so nothing can "
            "play to them. Enable them in UAD Console > Settings > I/O Matrix first."
        )
    return pair


def _system_audio_step(rig: Rig, facts: Facts, channel: ChannelInfo, warnings: list[str]) -> str:
    if _virtual_ordinal(rig, channel) is None:
        return (
            f"Make sure the computer audio actually reaches {channel.name}: this server cannot "
            "route it. Prove it with uad_peek_meters while something plays."
        )
    pair = _virtual_pair(rig, facts, channel, warnings)
    if pair is None:
        return (
            f"Send computer audio to {channel.name}: in Audio MIDI Setup, select the Apollo, "
            "Configure Speakers, Stereo, and pick its virtual channels for left and right. Then "
            "choose the Apollo in System Settings > Sound > Output."
        )
    left, right = (pair["channels"] + pair["channels"])[:2]
    return (
        f"Send computer audio to {channel.name}: open Audio MIDI Setup, select the Apollo, click "
        f"Configure Speakers, choose Stereo, and set left to channel {left} and right to channel "
        f"{right} ({' / '.join(pair['names'])}). Then choose the Apollo in System Settings > "
        "Sound > Output."
    )


def _plan_livestream(
    selection: Selection, rig: Rig, profile: Profile, facts: Facts
) -> dict[str, Any]:
    items = _Items(facts)
    sends: list[dict[str, Any]] = []
    warnings: list[str] = []
    you_do: list[str] = []
    check: list[str] = []

    for channel in selection.inputs:
        items.open(channel, VOICE_DB)
    if selection.system_audio is not None:
        items.open(selection.system_audio, SYSTEM_AUDIO_DB)
    if selection.guest is not None:
        items.open(selection.guest, VOICE_DB)
    cleared = _clear_solos(items, rig, facts, selection.device)
    if cleared:
        warnings.append(
            f"Clearing solo on {', '.join(cleared)}: a solo would leave the stream with only "
            "that strip."
        )

    warnings[:0] = _phantom_warnings(selection, profile, facts)
    if facts.monitor_muted is False and any(c.has_preamp for c in selection.inputs):
        warnings.append(
            "The monitors are on with an open mic, which can feed back. Headphones are safer; "
            "ask the user before muting the monitors with uad_set_monitor(mute=true)."
        )

    if selection.system_audio is not None:
        you_do.append(_system_audio_step(rig, facts, selection.system_audio, warnings))
    inputs = facts.routing.get("driver_inputs") or []
    monitor = routing.capture(inputs, routing.MONITOR_MIX)
    you_do.append(
        f"In the stream app, record the Apollo's inputs {_span(monitor)} "
        f"({' / '.join(monitor['names']) if monitor else 'MON L / MON R'}): the Console monitor "
        "mix, with the mic, the computer audio and their UAD plugins in it."
    )
    if monitor and monitor["channels"] != [1, 2]:
        first_two = " / ".join(facts.routing.get("inputs_1_2") or []) or "the first two inputs"
        you_do.append(
            f"If the stream app only takes inputs 1-2 (most do), it will get {first_two} "
            "instead. Move MON L/R to inputs 1-2 in UAD Console > Settings > I/O Matrix (a "
            "'Livestreaming' preset is listed there on most Apollos), then check with "
            "uad_get_routing."
        )
    you_do.append(
        "Do not set the stream app's audio monitoring output to the Apollo's virtual channels: "
        "the stream would feed back into itself."
    )

    names = [c.name for c in selection.strips()]
    if selection.guest is not None and selection.bus:
        bus = selection.bus
        send_targets = [(c, VOICE_DB) for c in selection.inputs]
        if selection.system_audio is not None:
            send_targets.append((selection.system_audio, SYSTEM_AUDIO_DB))
        for channel, level in send_targets:
            current = facts.bus_sends.get(channel.path)
            if current is None or current <= OFF_DB:
                sends.append(
                    {
                        "target": channel.index,
                        "device": channel.device,
                        "send": bus,
                        "level_db": level,
                    }
                )
        current = facts.bus_sends.get(selection.guest.path)
        if current is None or current > SEND_OFF_DB + 0.05:
            sends.append(
                {
                    "target": selection.guest.index,
                    "device": selection.guest.device,
                    "send": bus,
                    "level_db": SEND_OFF_DB,
                }
            )
        chosen = {c.path for c in selection.strips()}
        others = [
            channel.name
            for channel in rig.channels_of("input", selection.device)
            if channel.path not in chosen and _above_off(facts.bus_sends.get(channel.path))
        ]
        if others:
            warnings.append(
                f"Also sending to {bus}, so the guest hears them too: {', '.join(others)}."
            )
        if facts.bus_plugins:
            warnings.append(
                f"{bus} has {', '.join(facts.bus_plugins)} loaded; the guest hears it on the "
                "call mix."
            )
        aux = aux_strip(rig, selection.device, bus)
        if aux is not None:
            items.want(aux, mute=True)
        capture = routing.capture(inputs, bus)
        speaker = _virtual_pair(rig, facts, selection.guest, warnings)
        you_do.append(
            f"Call app microphone: the Apollo's inputs {_span(capture)} "
            f"({' / '.join(capture['names']) if capture else bus.upper()}), the mix without the "
            f"guest. Call app speaker: {selection.guest.name}"
            + (f", outputs {_span(speaker)}" if speaker else "")
            + ". Call apps usually only use channels 1-2, so this needs a channel router "
            "(Loopback, BlackHole) or a DAW in between; see the 'Livestream with a call guest' "
            "recipe in docs/RECIPES.md."
        )
        check.append(
            "Ask the guest whether they hear an echo of themselves (they should not). If they "
            f"hear nothing from you at all, the {bus} capture follows the aux return on this "
            "setup: unmute it with uad_set_channels(items=[{"
            f"'target': '{aux.name if aux else bus}', 'kind': 'aux', 'mute': false}}]) and "
            "accept that the call mix also plays in your monitors."
        )

    check[:0] = [
        f"uad_watch_meters(targets={names!r}, duration_s=10) while the user talks and plays "
        "something: the mic should peak around -12 to -6 dBFS and the computer audio should "
        "move.",
        "Watch the stream app's audio meter: voice and computer audio should both move it. Make "
        "a short test recording before going live.",
    ]
    return {
        "summary": (
            f"{', '.join(c.name for c in selection.inputs) or 'No mic yet'}"
            + (f" and {selection.system_audio.name}" if selection.system_audio else "")
            + " into the Console mix; the stream app records MON L/R"
            + (
                f"; {selection.guest.name} gets a mix-minus on {selection.bus}"
                if selection.guest
                else ""
            )
            + "."
        ),
        "channels": items.list(),
        "sends": sends,
        "warnings": warnings,
        "you_do": you_do,
        "check": check,
    }


def _above_off(level: float | None) -> bool:
    return level is not None and level > OFF_DB


def aux_strip(rig: Rig, device: int, bus: str) -> ChannelInfo | None:
    auxes = rig.channels_of("aux", device)
    n = int(bus[3:]) if bus[3:].isdigit() else 0
    return auxes[n - 1] if 0 < n <= len(auxes) else None


def _plan_tracking(
    selection: Selection, rig: Rig, profile: Profile, facts: Facts
) -> dict[str, Any]:
    items = _Items(facts)
    sends: list[dict[str, Any]] = []
    warnings = _phantom_warnings(selection, profile, facts)
    you_do: list[str] = []
    bus = selection.bus or "cue1"
    for channel in selection.inputs:
        items.open(channel, VOICE_DB)
        current = facts.bus_sends.get(channel.path)
        if current is None or current <= OFF_DB:
            sends.append(
                {
                    "target": channel.index,
                    "device": channel.device,
                    "send": bus,
                    "level_db": CUE_SEND_DB,
                }
            )
    cleared = _clear_solos(items, rig, facts, selection.device)
    if cleared:
        warnings.append(f"Clearing solo on {', '.join(cleared)}.")
    hearing = sorted(name for name, source in facts.headphones.items() if source == bus)
    if facts.headphones and not hearing:
        heard = ", ".join(
            f"{name} plays {source}" for name, source in sorted(facts.headphones.items())
        )
        warnings.append(
            f"No headphone output plays {bus} right now ({heard}). Pick {bus} for the "
            "performer's headphones in Console; this server cannot change that."
        )
    cue = routing.cue_output(facts.routing.get("driver_outputs") or [], bus)
    if cue:
        you_do.append(
            f"In the DAW, play the backing track to outputs {_span(cue)} "
            f"({' / '.join(cue['names'])}) to put it in this headphone mix."
        )
    names = [c.name for c in selection.inputs]
    return {
        "summary": (
            f"{', '.join(names) or 'No input yet'} open in the Console mix and in {bus}"
            + (f", heard on {', '.join(hearing)}" if hearing else "")
            + "."
        ),
        "channels": items.list(),
        "sends": sends,
        "warnings": warnings,
        "you_do": you_do,
        "check": [
            f"uad_watch_meters(targets={names!r}, duration_s=10, threshold_db=-6) while the "
            "performer plays or sings at their loudest. Aim for peaks of -12 to -6 dBFS and move "
            "uad_set_preamp(gain_db=...) in steps of 6 dB or less.",
            "Ask the performer whether the headphone balance works; adjust with uad_set_sends.",
        ],
    }


def _plan_mixing(selection: Selection, rig: Rig, profile: Profile, facts: Facts) -> dict[str, Any]:
    items = _Items(facts)
    muted = []
    for channel in rig.channels_of("input", selection.device):
        if channel.io_type is None or channel.io_type in HARDWARE_EXCLUDED_IO:
            continue
        if facts.strips.get(channel.path, StripState()).mute is not True:
            muted.append(channel.name)
        items.want(channel, mute=True)
    cleared = _clear_solos(items, rig, facts, selection.device)
    warnings = [f"Clearing solo on {', '.join(cleared)}."] if cleared else []
    return {
        "summary": (
            f"Mute {len(muted)} hardware input(s) in the Console mix"
            + (f" ({', '.join(muted[:8])}{', ...' if len(muted) > 8 else ''})" if muted else "")
            + "; virtual inputs stay open."
        ),
        "channels": items.list(),
        "sends": [],
        "warnings": warnings,
        "you_do": [],
        "check": [
            "Play the DAW session: the monitors should carry playback only. Console mutes change "
            "what you hear, not what the DAW records.",
        ],
    }


_PLANNERS = {
    "livestream": _plan_livestream,
    "tracking": _plan_tracking,
    "mixing": _plan_mixing,
}


def plan(selection: Selection, rig: Rig, profile: Profile, facts: Facts) -> dict[str, Any]:
    """The full plan: what apply would write, what the user must do, and how to check it."""
    body = _PLANNERS[selection.mode](selection, rig, profile, facts)
    for item in body["channels"]:
        if item.get("kind") == "output":  # pragma: no cover - a planner bug, never user input
            raise AssertionError("modes never write output strips")
    return {
        "mode": selection.mode,
        "device": selection.device,
        **body,
        "notes": selection.notes,
        "questions": selection.questions,
        "writes": len(body["channels"]) + len(body["sends"]),
    }
