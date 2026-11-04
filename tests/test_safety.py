"""Unit tests for the write barrier (docs/SPEC.md §4.3).

Pure policy: no sockets, no engine, no event loop. Nothing in this file can reach
127.0.0.1:4710 even by accident.

Properties are real :class:`UAProperty` instances, but the gate itself only duck-types them (see
``safety.PropertyLike``), so any object with those attributes works.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
for _path in (REPO_ROOT, SRC_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from uad_console_mcp.client import UAProperty  # noqa: E402
from uad_console_mcp.safety import (  # noqa: E402
    AUDIO_FLOWING_DB,
    CONFIRM_REQUIRED_PROPERTIES,
    DEFAULT_MAX_MONITOR_DB,
    ENV_ALLOW_READONLY,
    ENV_MAX_MONITOR_DB,
    ENV_MODE,
    ENV_MONITOR_STEP_DB,
    ENV_MONITOR_WINDOW_DB,
    ENV_MONITOR_WINDOW_SECONDS,
    ENV_WRITE_BURST,
    ENV_WRITE_RATE,
    MODES,
    WRITABLE_PROPERTIES,
    Mode,
    SafetyGate,
    SafetyRefusal,
    WriteContext,
    canonical_path,
    coerces_to_false,
    gate_from_env,
    mode_from_env,
    property_name,
)

# The exact SPEC §4.3 safe-mode allowlist, retyped so drift in either direction is caught.
SPEC_ALLOWLIST = {
    "Name",
    "SRConvert",
    "FaderLevel",
    "Pan",
    "Pan2",
    "Mute",
    "Solo",
    "SoloSafe",
    "ChannelHidden",
    "Gain",
    "48V",
    "Pad",
    "Phase",
    "LowCut",
    "HiZ",
    "IOType",
    "Bypass",
    "PostFaderOverride",
    "EffectName",
    "Preset",
    "Power",
    "NormalizedValue",
    "CRMonitorLevel",
    "DimOn",
    "MixToMono",
    "DimAttenuation",
    "TalkbackOn",
    "TalkbackMaster",
    "TalkbackEnabled",
    "AltMonSelection",
    "ClockSource",
    "SampleRate",
    "StereoName",
    "Stereo",
    "EnabledByUser",
}

#: Tokens that count as "this message tells the caller how to unblock the action".
UNBLOCK_TOKENS = (
    ENV_MODE,
    ENV_MAX_MONITOR_DB,
    ENV_MONITOR_STEP_DB,
    ENV_MONITOR_WINDOW_DB,
    ENV_MONITOR_WINDOW_SECONDS,
    ENV_WRITE_RATE,
    ENV_WRITE_BURST,
    ENV_ALLOW_READONLY,
    "confirm=True",
    "confirm_condenser_mic=True",
    "audio_flowing=False",
)

INPUT = "/devices/0/inputs/0"
MONITOR = "/devices/0/outputs/22"


class FakeClock:
    """Monotonic clock we drive by hand, so no test ever sleeps."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def prop(
    path: str,
    kind: str = "float",
    value: Any = 0.0,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    values: list[Any] | None = None,
    readonly: bool = False,
) -> UAProperty:
    return UAProperty(
        path=path,
        type=kind,
        value=value,
        min=minimum,
        max=maximum,
        values=values,
        readonly=readonly,
    )


def fader(path: str = f"{INPUT}/FaderLevel", value: float = 0.0) -> UAProperty:
    return prop(path, "float", value, minimum=-144.0, maximum=12.0)


def monitor_level(value: float = -22.0) -> UAProperty:
    return prop(f"{MONITOR}/CRMonitorLevel", "float", value, minimum=-96.0, maximum=0.0)


def clock_source(value: str = "Internal") -> UAProperty:
    return prop(
        "/ClockSource",
        "string",
        value,
        values=["Internal", "Word Clock", "ADAT", "S/PDIF"],
    )


def sample_rate(value: int = 48000, *, disable_192: bool = False) -> UAProperty:
    return prop(
        "/SampleRate",
        "int",
        value,
        values=[
            {"value": 44100, "string": "44.1 kHz", "enabled": True},
            {"value": 48000, "string": "48 kHz", "enabled": True},
            {"value": 192000, "string": "192 kHz", "enabled": not disable_192},
        ],
    )


def gate(mode: Mode = "safe", **kwargs: Any) -> SafetyGate:
    return SafetyGate(mode, **kwargs)


def assert_names_remedy(message: str) -> None:
    assert any(token in message for token in UNBLOCK_TOKENS), (
        f"refusal does not name an env var or confirm flag: {message!r}"
    )


# --------------------------------------------------------------------------- env + config


def test_mode_defaults_to_safe() -> None:
    assert mode_from_env({}) == "safe"
    assert mode_from_env({ENV_MODE: "   "}) == "safe"
    assert SafetyGate().mode == "safe"
    assert SafetyGate().max_monitor_db == DEFAULT_MAX_MONITOR_DB


@pytest.mark.parametrize("raw", ["readonly", "SAFE", " full ", "Full"])
def test_mode_parsing_is_forgiving_about_case(raw: str) -> None:
    assert mode_from_env({ENV_MODE: raw}) == raw.strip().casefold()


def test_invalid_mode_names_the_env_var_and_the_valid_values() -> None:
    with pytest.raises(ValueError) as excinfo:
        mode_from_env({ENV_MODE: "loud"})
    message = str(excinfo.value)
    assert ENV_MODE in message
    assert "loud" in message
    for mode in MODES:
        assert mode in message


def test_invalid_monitor_ceiling_names_the_env_var() -> None:
    with pytest.raises(ValueError, match=ENV_MAX_MONITOR_DB):
        gate_from_env({ENV_MAX_MONITOR_DB: "as loud as possible"})
    with pytest.raises(ValueError, match=ENV_MAX_MONITOR_DB):
        gate_from_env({ENV_MAX_MONITOR_DB: "inf"})


def test_gate_rejects_an_unknown_mode() -> None:
    with pytest.raises(ValueError, match=ENV_MODE):
        SafetyGate("loud")  # type: ignore[arg-type]


def test_gate_from_env_applies_every_knob() -> None:
    limits = gate_from_env(
        {
            ENV_MODE: "full",
            ENV_MAX_MONITOR_DB: "-12.5",
            ENV_MONITOR_STEP_DB: "3",
            ENV_MONITOR_WINDOW_DB: "7",
            ENV_MONITOR_WINDOW_SECONDS: "15",
            ENV_WRITE_RATE: "4",
            ENV_WRITE_BURST: "5",
            ENV_ALLOW_READONLY: "1",
        }
    ).describe()
    assert limits["mode"] == "full"
    assert limits["max_monitor_db"] == -12.5
    assert limits["max_monitor_step_db"] == 3.0
    assert limits["monitor_window_db"] == 7.0
    assert limits["monitor_window_seconds"] == 15.0
    assert limits["writes_per_second"] == 4.0
    assert limits["write_burst"] == 5
    assert limits["allow_readonly_writes"] is True
    assert limits["mode_env_var"] == ENV_MODE


def test_allowlist_matches_the_spec_exactly() -> None:
    assert set(WRITABLE_PROPERTIES) == SPEC_ALLOWLIST
    assert CONFIRM_REQUIRED_PROPERTIES <= WRITABLE_PROPERTIES


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (f"{INPUT}/FaderLevel", "FaderLevel"),
        (f"{INPUT}/FaderLevel/value", "FaderLevel"),
        ("/SampleRate", "SampleRate"),
        ("/devices/0/inputs/0/preamps/0/48V", "48V"),
    ],
)
def test_property_name_extraction(path: str, expected: str) -> None:
    assert property_name(path) == expected


# ------------------------------------------------------------------------------ mode matrix


@pytest.mark.parametrize("name", sorted(SPEC_ALLOWLIST))
def test_readonly_mode_refuses_every_allowlisted_property(name: str) -> None:
    barrier = gate("readonly")
    with pytest.raises(SafetyRefusal) as excinfo:
        barrier.check_write(prop(f"{INPUT}/{name}", "float", 0.0), 1.0, context="unit test")
    message = str(excinfo.value)
    assert "readonly" in message
    assert ENV_MODE in message
    assert_names_remedy(message)


def test_safe_mode_allows_an_allowlisted_write() -> None:
    assert gate("safe").check_write(fader(value=-3.0), -6.0, context="balance") == -6.0


@pytest.mark.parametrize("name", ["RecordPreEffects", "SelectedOnFront", "Isolate", "LLMDiskMix"])
def test_safe_mode_refuses_everything_off_the_allowlist(name: str) -> None:
    with pytest.raises(SafetyRefusal) as excinfo:
        gate("safe").check_write(prop(f"{INPUT}/{name}", "bool", False), True, context="poke")
    message = str(excinfo.value)
    assert name in message
    assert f"{ENV_MODE}=full" in message


@pytest.mark.parametrize("name", ["RecordPreEffects", "SelectedOnFront", "Isolate"])
def test_full_mode_bypasses_the_allowlist(name: str) -> None:
    target = prop(f"{INPUT}/{name}", "bool", False)
    assert gate("full").check_write(target, True, context="raw") is True


# --------------------------------------------------------------------------------- denylist


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    "name", ["DeviceHwID", "MultiUnitOrder", "AppCommand", "MixerState", "FirmwareVersion"]
)
def test_denylist_holds_in_every_mode(mode: Mode, name: str) -> None:
    with pytest.raises(SafetyRefusal) as excinfo:
        gate(mode).check_write(prop(f"/devices/0/{name}", "int", 0), 1, context="probe")
    assert_names_remedy(str(excinfo.value))


@pytest.mark.parametrize("mode", MODES)
def test_pluginscenes_writes_are_refused_in_every_mode(mode: Mode) -> None:
    with pytest.raises(SafetyRefusal) as excinfo:
        gate(mode).check_write(prop("/pluginscenes/0/Name", "string", "a"), "b", context="scene")
    message = str(excinfo.value)
    assert "/pluginscenes" in message
    assert_names_remedy(message)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    "path",
    [
        "//pluginscenes/0/Name",
        "/PluginScenes/0/Name",
        "/devices/0/multiunitorder",
        "/devices/0/APPCOMMAND",
        f"{MONITOR}/CRMonitorLevelTapered",
        f"{MONITOR}/SurroundTrimLevel",
    ],
)
def test_denylist_survives_other_spellings(mode: Mode, path: str) -> None:
    """Audit H5: whether the engine resolves these spellings is unknown; the gate must not care."""
    with pytest.raises(SafetyRefusal) as excinfo:
        gate(mode).check_write(prop(path, "float", 0.0), 1.0, context="probe")
    assert_names_remedy(str(excinfo.value))


def test_canonical_path_strips_value_and_empty_segments() -> None:
    assert canonical_path("//devices/0//outputs/22/Mute/value/") == "/devices/0/outputs/22/Mute"
    with pytest.raises(ValueError):
        canonical_path("/devices/../pluginscenes")


def test_engine_readonly_flag_is_honoured_even_in_full_mode() -> None:
    latency = prop(f"{INPUT}/Latency", "int", 32, readonly=True)
    with pytest.raises(SafetyRefusal) as excinfo:
        gate("full").check_write(latency, 0, context="probe")
    message = str(excinfo.value)
    assert ENV_ALLOW_READONLY in message
    assert gate("full", allow_readonly_writes=True).check_write(latency, 0, context="probe") == 0


# ------------------------------------------------------------------------ values and enums


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(999.0, 12.0), (-999.0, -144.0), (-6.5, -6.5), (12.0, 12.0), ("-3", -3.0)],
)
def test_numeric_values_are_clamped_to_the_property_metadata(
    requested: Any, expected: float
) -> None:
    assert gate("safe").check_write(fader(), requested, context="clamp") == expected


def test_int_properties_are_rounded_and_clamped() -> None:
    barrier = gate("safe")
    postfader = prop(f"{INPUT}/sends/0/PostFaderOverride", "int", -1, minimum=-1.0, maximum=1.0)
    assert barrier.check_write(postfader, 9, context="clamp") == 1
    assert barrier.check_write(postfader, -9, context="clamp") == -1
    assert barrier.check_write(postfader, 0.6, context="round") == 1
    assert isinstance(barrier.check_write(postfader, 0.6, context="round"), int)


@pytest.mark.parametrize(
    ("requested", "expected"), [(True, True), (False, False), (1, True), ("off", False)]
)
def test_bool_properties_accept_the_obvious_spellings(requested: Any, expected: bool) -> None:
    mute = prop(f"{INPUT}/Mute", "bool", False)
    assert gate("safe").check_write(mute, requested, context="mute") is expected


@pytest.mark.parametrize(
    ("kind", "bad"),
    [("bool", "banana"), ("float", "loud"), ("int", "loud"), ("string", 5), ("float", True)],
)
def test_unusable_values_raise_value_error_not_a_refusal(kind: str, bad: Any) -> None:
    target = prop(f"{INPUT}/Name" if kind == "string" else f"{INPUT}/Mute", kind, None)
    with pytest.raises(ValueError) as excinfo:
        gate("full").check_write(target, bad, context="typo")
    assert not isinstance(excinfo.value, SafetyRefusal)


@pytest.mark.parametrize(
    ("requested", "expected"), [("Mic", "Mic"), ("mic", "Mic"), ("  LINE ", "Line")]
)
def test_plain_list_enums_resolve_case_insensitively(requested: str, expected: str) -> None:
    io_type = prop(f"{INPUT}/IOType", "string", "Mic", values=["Line", "Mic", "Dante"])
    assert gate("safe").check_write(io_type, requested, context="input type") == expected


def test_unknown_enum_option_is_refused_and_lists_the_options() -> None:
    io_type = prop(f"{INPUT}/IOType", "string", "Mic", values=["Line", "Mic", "Dante"])
    with pytest.raises(SafetyRefusal) as excinfo:
        gate("safe").check_write(io_type, "Ribbon", context="input type")
    message = str(excinfo.value)
    assert "'Line'" in message
    assert "'Dante'" in message
    assert_names_remedy(message)


@pytest.mark.parametrize(
    ("requested", "expected"), [(48000, 48000), ("48 kHz", 48000), (44100.0, 44100)]
)
def test_object_shaped_enums_match_value_and_label(requested: Any, expected: int) -> None:
    context = WriteContext(reason="rate", confirm=True, audio_flowing=False)
    assert gate("safe").check_write(sample_rate(), requested, context=context) == expected


def test_disabled_enum_options_are_refused() -> None:
    context = WriteContext(reason="rate", confirm=True, audio_flowing=False)
    with pytest.raises(SafetyRefusal) as excinfo:
        gate("safe").check_write(sample_rate(disable_192=True), 192000, context=context)
    message = str(excinfo.value)
    assert "disabled" in message
    assert_names_remedy(message)


def test_hierarchical_preset_trees_skip_enum_validation() -> None:
    preset = prop(
        f"{INPUT}/Preset",
        "string",
        "",
        values=[
            {
                "value": "Bass",
                "type": "folder",
                "locked": True,
                "children": [{"value": "Andrew Scheps - Bass", "type": "file"}],
            }
        ],
    )
    applied = gate("safe").check_write(preset, "Andrew Scheps - Bass", context="preset")
    assert applied == "Andrew Scheps - Bass"


# ----------------------------------------------------------------------------- monitor rail


def test_monitor_ceiling_refusal_names_the_env_var() -> None:
    with pytest.raises(SafetyRefusal) as excinfo:
        gate("safe").monitor_level_check(-20.0, -3.0)
    message = str(excinfo.value)
    assert ENV_MAX_MONITOR_DB in message
    assert "-6.0" in message


def test_turning_the_monitor_down_is_always_allowed() -> None:
    barrier = gate("safe")
    assert barrier.monitor_level_check(-3.0, -20.0) == -20.0
    assert barrier.monitor_level_check(-96.0, -96.0) == -96.0


def test_monitor_increase_is_capped_at_six_db_per_call() -> None:
    assert gate("safe").monitor_level_check(-30.0, -10.0) == -24.0
    assert gate("safe").monitor_level_check(-30.0, -28.0) == -28.0


def test_monitor_rolling_window_limits_ten_db_per_thirty_seconds() -> None:
    clock = FakeClock()
    barrier = gate("safe", clock=clock)

    assert barrier.monitor_level_check(-30.0, -24.0) == -24.0
    barrier.note_monitor_event(-24.0)
    assert barrier.monitor_budget_remaining_db() == pytest.approx(4.0)

    with pytest.raises(SafetyRefusal) as excinfo:
        barrier.monitor_level_check(-24.0, -18.0)
    message = str(excinfo.value)
    assert ENV_MONITOR_WINDOW_DB in message

    assert barrier.monitor_level_check(-24.0, -21.0) == -21.0
    barrier.note_monitor_event(-21.0)
    with pytest.raises(SafetyRefusal):
        barrier.monitor_level_check(-21.0, -19.0)

    clock.advance(31.0)
    assert barrier.monitor_level_check(-21.0, -15.0) == -15.0


def test_no_sequence_of_monitor_calls_can_exceed_the_ceiling() -> None:
    """Property test: whatever the caller asks for, the applied level stays under the ceiling."""
    clock = FakeClock()
    ceiling = -6.0
    barrier = gate("safe", max_monitor_db=ceiling, clock=clock)
    rng = random.Random(20260729)
    level = -96.0

    for _ in range(400):
        target = rng.uniform(-96.0, 12.0)
        try:
            applied = barrier.monitor_level_check(level, target)
        except SafetyRefusal as refusal:
            assert_names_remedy(str(refusal))
            clock.advance(rng.choice([0.0, 0.5, 31.0]))
            continue
        assert applied <= ceiling + 1e-9, f"{applied} exceeded ceiling {ceiling}"
        assert applied <= level + 6.0 + 1e-9, "increase exceeded the +6 dB per-call step"
        barrier.note_monitor_event(applied)
        level = applied
        clock.advance(rng.choice([0.0, 0.5, 5.0]))

    assert level <= ceiling + 1e-9


def test_check_write_routes_the_monitor_level_through_the_rail() -> None:
    barrier = gate("safe")
    with pytest.raises(SafetyRefusal) as excinfo:
        barrier.check_write(monitor_level(-22.0), 0.0, context="monitor up")
    assert ENV_MAX_MONITOR_DB in str(excinfo.value)
    assert barrier.check_write(monitor_level(-22.0), -18.0, context="monitor up") == -18.0


def test_monitor_level_needs_a_known_current_value() -> None:
    with pytest.raises(ValueError):
        gate("safe").check_write(monitor_level(value=None), -20.0, context="monitor")  # type: ignore[arg-type]


@pytest.mark.parametrize("name", ["Mute", "DimOn"])
def test_unmute_and_undim_are_refused_when_the_stored_level_is_hot(name: str) -> None:
    barrier = gate("safe")
    hot = WriteContext(reason="open the mix", monitor_level_db=-2.0)
    with pytest.raises(SafetyRefusal) as excinfo:
        barrier.check_write(prop(f"{MONITOR}/{name}", "bool", True), False, context=hot)
    message = str(excinfo.value)
    assert ENV_MAX_MONITOR_DB in message
    assert "CRMonitorLevel" in message


@pytest.mark.parametrize("name", ["Mute", "DimOn"])
def test_engaging_mute_or_dim_is_always_allowed(name: str) -> None:
    hot = WriteContext(reason="protect ears", monitor_level_db=-2.0)
    target = prop(f"{MONITOR}/{name}", "bool", False)
    assert gate("safe").check_write(target, True, context=hot) is True


@pytest.mark.parametrize("name", ["Mute", "DimOn"])
def test_release_is_allowed_when_the_stored_level_is_safe(name: str) -> None:
    cool = WriteContext(reason="open the mix", monitor_level_db=-20.0)
    target = prop(f"{MONITOR}/{name}", "bool", True)
    assert gate("safe").check_write(target, False, context=cool) is False


@pytest.mark.parametrize("name", ["Mute", "DimOn"])
def test_release_on_an_identified_monitor_fails_closed_without_a_level(name: str) -> None:
    """B1: is_monitor=True turns a missing level into a FAILED READ, so the release is refused."""
    blind = WriteContext(reason="open the mix", is_monitor=True)
    with pytest.raises(SafetyRefusal) as excinfo:
        gate("safe").check_write(prop(f"{MONITOR}/{name}", "bool", True), False, context=blind)
    message = str(excinfo.value)
    assert "CRMonitorLevel" in message
    assert MONITOR in message
    assert_names_remedy(message)


@pytest.mark.parametrize("name", ["Mute", "DimOn"])
def test_release_on_an_unidentified_strip_is_still_allowed(name: str) -> None:
    """Ordinary channel mutes must not be caught by the monitor rail's fail-closed branch."""
    plain = WriteContext(reason="unmute a channel")
    target = prop(f"{INPUT}/{name}", "bool", True)
    assert gate("safe").check_write(target, False, context=plain) is False


@pytest.mark.parametrize("spelling", [False, 0, 0.0, "0", "false", "False", "no", "n", "off", "F"])
def test_every_spelling_of_false_is_a_monitor_release(spelling: Any) -> None:
    """The rail must fire on whatever the gate will COERCE to False, not just on ``False``."""
    assert coerces_to_false(spelling) is True
    hot = WriteContext(reason="unmute", monitor_level_db=-2.0, is_monitor=True)
    with pytest.raises(SafetyRefusal) as excinfo:
        gate("safe").check_write(prop(f"{MONITOR}/Mute", "bool", True), spelling, context=hot)
    assert ENV_MAX_MONITOR_DB in str(excinfo.value)


@pytest.mark.parametrize("spelling", [True, 1, "1", "true", "yes", "y", "on"])
def test_engaging_spellings_are_not_treated_as_a_release(spelling: Any) -> None:
    assert coerces_to_false(spelling) is False
    hot = WriteContext(reason="mute", monitor_level_db=-2.0, is_monitor=True)
    assert gate("safe").check_write(prop(f"{MONITOR}/Mute", "bool", False), spelling, context=hot)


def dim_attenuation(value: int = 17) -> UAProperty:
    return prop("/devices/0/DimAttenuation", "int", value, minimum=0.0, maximum=60.0)


def test_raising_the_dim_attenuation_never_needs_context() -> None:
    assert gate("safe").check_write(dim_attenuation(17), 30, context="deeper dim") == 30


def test_lowering_the_dim_attenuation_is_free_while_dim_is_off() -> None:
    off = WriteContext(reason="prep", dim_on=False, is_monitor=True)
    assert gate("safe").check_write(dim_attenuation(17), 0, context=off) == 0


def test_lowering_the_dim_attenuation_is_railed_while_dim_is_on() -> None:
    """B2: level 0 dB dimmed by 17 dB is safe; taking the dim away exposes 0 dB. Refuse."""
    dimmed = WriteContext(reason="un-dim by stealth", dim_on=True, monitor_level_db=0.0)
    with pytest.raises(SafetyRefusal) as excinfo:
        gate("safe").check_write(dim_attenuation(17), 0, context=dimmed)
    message = str(excinfo.value)
    assert ENV_MAX_MONITOR_DB in message
    assert "un-dim" in message
    # The same write is fine when what it exposes is under the ceiling.
    quiet = WriteContext(reason="un-dim", dim_on=True, monitor_level_db=-22.0)
    assert gate("safe").check_write(dim_attenuation(17), 0, context=quiet) == 0


@pytest.mark.parametrize(
    "context",
    [
        WriteContext(reason="dim state unknown", monitor_level_db=0.0),
        WriteContext(reason="level unreadable", dim_on=True),
        WriteContext(reason="nothing known at all"),
    ],
)
def test_lowering_the_dim_attenuation_fails_closed_when_the_reads_fail(
    context: WriteContext,
) -> None:
    with pytest.raises(SafetyRefusal) as excinfo:
        gate("safe").check_write(dim_attenuation(17), 6, context=context)
    message = str(excinfo.value)
    assert "DimOn" in message and "CRMonitorLevel" in message
    assert_names_remedy(message)


def test_dim_attenuation_with_an_unreadable_current_value_is_treated_as_a_decrease() -> None:
    """If we cannot prove the write is an increase, it must be railed like a decrease."""
    blind = prop("/devices/0/DimAttenuation", "int", None, minimum=0.0, maximum=60.0)
    with pytest.raises(SafetyRefusal):
        gate("safe").check_write(blind, 60, context=WriteContext(reason="unknown current"))
    dimmed = WriteContext(reason="unknown current", dim_on=True, monitor_level_db=0.0)
    with pytest.raises(SafetyRefusal) as excinfo:
        gate("safe").check_write(blind, 3, context=dimmed)
    assert ENV_MAX_MONITOR_DB in str(excinfo.value)
    # Dim is off, so nothing is in circuit and the write is harmless whatever the old value was.
    off = WriteContext(reason="unknown current", dim_on=False)
    assert gate("safe").check_write(blind, 60, context=off) == 60


def test_dim_attenuation_partial_reduction_uses_the_exposed_level() -> None:
    """-20 dB dimmed 17 dB is -37; cutting the dim to 6 dB exposes -26, still under the ceiling."""
    dimmed = WriteContext(reason="less dim", dim_on=True, monitor_level_db=-20.0)
    assert gate("safe").check_write(dim_attenuation(17), 6, context=dimmed) == 6
    hot = WriteContext(reason="less dim", dim_on=True, monitor_level_db=-4.0)
    with pytest.raises(SafetyRefusal):
        gate("safe").check_write(dim_attenuation(17), 0, context=hot)


def test_check_monitor_unmute_is_callable_on_its_own() -> None:
    barrier = gate("safe")
    assert barrier.check_monitor_unmute(-20.0) is None
    with pytest.raises(SafetyRefusal) as excinfo:
        barrier.check_monitor_unmute(-2.0, action="un-dim")
    message = str(excinfo.value)
    assert "un-dim" in message
    assert_names_remedy(message)


# ------------------------------------------------------------------------------- rate limit


def test_token_bucket_allows_a_burst_of_twenty_then_refuses() -> None:
    clock = FakeClock()
    barrier = gate("safe", clock=clock)
    for _ in range(20):
        barrier.check_write(fader(), -6.0, context="burst")

    with pytest.raises(SafetyRefusal) as excinfo:
        barrier.check_write(fader(), -6.0, context="burst")
    message = str(excinfo.value)
    assert "10 writes/s" in message
    assert "burst 20" in message
    assert ENV_WRITE_RATE in message

    clock.advance(0.1)
    assert barrier.check_write(fader(), -6.0, context="refilled") == -6.0
    with pytest.raises(SafetyRefusal):
        barrier.check_write(fader(), -6.0, context="empty again")

    clock.advance(60.0)
    for _ in range(20):
        barrier.check_write(fader(), -6.0, context="refilled to burst")


def test_refused_writes_spend_a_token_too() -> None:
    """A refusal still costs a read and a decision, so hammering a closed gate is not free."""
    clock = FakeClock()
    barrier = gate("safe", write_burst=1, clock=clock)
    off_allowlist = prop(f"{INPUT}/Isolate", "bool", False)

    with pytest.raises(SafetyRefusal) as first:
        barrier.check_write(off_allowlist, True, context="off allowlist")
    assert "not in the safe-mode allowlist" in str(first.value)

    with pytest.raises(SafetyRefusal) as second:
        barrier.check_write(off_allowlist, True, context="again")
    assert "write rate limit reached" in str(second.value), "the refusal spent the only token"

    clock.advance(1.0)
    with pytest.raises(SafetyRefusal) as refilled:
        barrier.check_write(off_allowlist, True, context="refilled")
    assert "not in the safe-mode allowlist" in str(refilled.value)


def test_quieting_writes_are_never_rate_limited() -> None:
    """Audit H2: an empty bucket is exactly when the panic button has to work."""
    barrier = gate("safe", write_burst=1, clock=FakeClock())
    barrier.check_write(fader(), -6.0, context="spends the only token")
    with pytest.raises(SafetyRefusal, match="rate limit"):
        barrier.check_write(fader(), -6.0, context="empty")
    mute = prop(f"{INPUT}/Mute", "bool", False)
    talkback = prop("/TalkbackOn", "bool", True)
    for _ in range(20):
        assert barrier.check_write(mute, True, context="panic") is True
        assert barrier.check_write(talkback, False, context="panic") is False
    with pytest.raises(SafetyRefusal, match="rate limit"):
        barrier.check_write(mute, False, context="an unmute is not exempt")
    with pytest.raises(SafetyRefusal, match="rate limit"):
        barrier.check_write(talkback, True, context="talkback on is not exempt")


def test_readonly_mode_never_degrades_into_a_rate_limit_message() -> None:
    """No wait unblocks readonly mode, so never answer a readonly write with 'retry in 0.1 s'."""
    barrier = gate("readonly", write_burst=1, clock=FakeClock())
    for _ in range(50):
        with pytest.raises(SafetyRefusal) as excinfo:
            barrier.check_write(fader(), -6.0, context="hammering")
        assert "safety mode is 'readonly'" in str(excinfo.value)
        assert "rate limit" not in str(excinfo.value)


def test_denylisted_writes_are_free_and_keep_naming_the_denylist() -> None:
    barrier = gate("full", write_burst=1, clock=FakeClock())
    denied = prop("/devices/0/DeviceHwID", "int", 0)
    for _ in range(50):
        with pytest.raises(SafetyRefusal) as excinfo:
            barrier.check_write(denied, 1, context="hammering")
        assert "permanent denylist" in str(excinfo.value)
    # The bucket was never touched, so real work still gets its full burst.
    assert barrier.check_write(fader(), -6.0, context="real work") == -6.0


# ------------------------------------------------------------------------- confirm gates


def phantom(value: bool = False) -> UAProperty:
    return prop("/devices/0/inputs/0/preamps/0/48V", "bool", value)


@pytest.mark.parametrize("mode", ["safe", "full"])
def test_phantom_power_needs_the_condenser_confirmation(mode: Mode) -> None:
    with pytest.raises(SafetyRefusal) as excinfo:
        gate(mode).check_write(phantom(), True, context="vocal chain")
    message = str(excinfo.value)
    assert "confirm_condenser_mic=True" in message
    assert "ribbon" in message


def test_phantom_power_is_allowed_once_confirmed() -> None:
    confirmed = WriteContext(reason="condenser on ANALOG 1", confirm_condenser_mic=True)
    assert gate("safe").check_write(phantom(), True, context=confirmed) is True


def test_switching_phantom_power_off_needs_no_confirmation() -> None:
    assert gate("safe").check_write(phantom(True), False, context="pack down") is False


@pytest.mark.parametrize("prop_factory", [clock_source, sample_rate])
def test_clock_and_sample_rate_need_confirm_and_a_meter_check(prop_factory: Any) -> None:
    barrier = gate("safe")
    target = prop_factory()
    requested: Any = "ADAT" if target.path == "/ClockSource" else 44100

    with pytest.raises(SafetyRefusal) as excinfo:
        barrier.check_write(target, requested, context="switch clock")
    assert "confirm=True" in str(excinfo.value)

    with pytest.raises(SafetyRefusal) as excinfo:
        barrier.check_write(target, requested, context=WriteContext(confirm=True))
    message = str(excinfo.value)
    assert "audio_flowing=False" in message
    assert f"{AUDIO_FLOWING_DB:+.0f}" in message

    with pytest.raises(SafetyRefusal) as excinfo:
        barrier.check_write(
            target, requested, context=WriteContext(confirm=True, audio_flowing=True)
        )
    message = str(excinfo.value)
    assert "audio is flowing" in message
    assert f"{AUDIO_FLOWING_DB:+.0f}" in message

    applied = barrier.check_write(
        target, requested, context=WriteContext(confirm=True, audio_flowing=False)
    )
    assert applied == requested


@pytest.mark.parametrize("mode", ["safe", "full"])
def test_confirm_gates_are_not_bypassed_by_full_mode(mode: Mode) -> None:
    with pytest.raises(SafetyRefusal):
        gate(mode).check_write(clock_source(), "ADAT", context="switch clock")


# ----------------------------------------------------------------- refusal message quality


def _every_refusal() -> list[str]:
    """One refusal from every gate in the module, for message-quality assertions."""
    messages: list[str] = []
    clock = FakeClock()
    confirmed = WriteContext(confirm=True, audio_flowing=False)
    io_type = prop(f"{INPUT}/IOType", "string", "Mic", values=["Line", "Mic"])

    scenarios: list[tuple[SafetyGate, Any, Any, Any]] = [
        (gate("readonly"), fader(), -6.0, "readonly mode"),
        (gate("safe"), prop(f"{INPUT}/Isolate", "bool", False), True, "off allowlist"),
        (gate("full"), prop("/devices/0/DeviceHwID", "int", 0), 1, "denylist"),
        (gate("full"), prop("/pluginscenes/0/Name", "string", "a"), "b", "plugin scene"),
        (gate("full"), prop(f"{INPUT}/Latency", "int", 32, readonly=True), 0, "readonly prop"),
        (gate("safe"), phantom(), True, "phantom power"),
        (gate("safe"), clock_source(), "ADAT", "clock without confirm"),
        (gate("safe"), sample_rate(disable_192=True), 192000, confirmed),
        (gate("safe"), io_type, "Ribbon", "bad enum"),
        (gate("safe"), monitor_level(-22.0), 0.0, "monitor ceiling"),
        (
            gate("safe"),
            prop(f"{MONITOR}/Mute", "bool", True),
            False,
            WriteContext(reason="unmute", monitor_level_db=-1.0),
        ),
        (
            gate("safe"),
            prop(f"{MONITOR}/DimOn", "bool", True),
            False,
            WriteContext(reason="un-dim blind", is_monitor=True),
        ),
        (
            gate("safe"),
            dim_attenuation(17),
            0,
            WriteContext(reason="un-dim by stealth", dim_on=True, monitor_level_db=0.0),
        ),
        (gate("safe"), dim_attenuation(17), 0, WriteContext(reason="dim state unknown")),
    ]
    for barrier, target, value, context in scenarios:
        with pytest.raises(SafetyRefusal) as excinfo:
            barrier.check_write(target, value, context=context)
        messages.append(str(excinfo.value))

    rate_limited = gate("safe", write_burst=1, clock=clock)
    rate_limited.check_write(fader(), -6.0, context="first")
    with pytest.raises(SafetyRefusal) as excinfo:
        rate_limited.check_write(fader(), -6.0, context="second")
    messages.append(str(excinfo.value))

    windowed = gate("safe", clock=clock)
    windowed.monitor_level_check(-30.0, -24.0)
    windowed.note_monitor_event(-24.0)
    with pytest.raises(SafetyRefusal) as excinfo:
        windowed.monitor_level_check(-24.0, -18.0)
    messages.append(str(excinfo.value))

    with pytest.raises(SafetyRefusal) as excinfo:
        gate("safe").check_monitor_unmute(-1.0)
    messages.append(str(excinfo.value))
    return messages


def test_every_refusal_names_its_remedy() -> None:
    messages = _every_refusal()
    assert len(messages) >= 14
    for message in messages:
        assert_names_remedy(message)


def test_every_refusal_reads_like_a_sentence() -> None:
    for message in _every_refusal():
        assert message.endswith(".")
        assert len(message) > 40
        assert "Traceback" not in message
        assert "\n" not in message


def test_describe_reports_the_effective_limits() -> None:
    limits = gate("safe").describe()
    assert limits["mode"] == "safe"
    assert limits["max_monitor_db"] == DEFAULT_MAX_MONITOR_DB
    assert set(limits["allowlisted_properties"]) == SPEC_ALLOWLIST
    assert limits["confirm_required_properties"] == sorted(CONFIRM_REQUIRED_PROPERTIES)
