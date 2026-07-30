# Gain staging with the meters this server can read

## The targets

| Measure | Target | Why |
|---|---|---|
| Average level while performing | around **-18 dBFS** | leaves headroom and sits where analog-modelled plugins expect signal |
| Peaks on the loudest passage | **under -6 dBFS**, aim -12 to -6 | keeps a margin for a louder take without clipping the converter |
| Clip indicator | never | `MeterClip` true means the converter clipped, which cannot be undone |

Anything averaging near 0 dBFS is too hot, whatever it sounds like. Anything averaging near -50
dBFS will need so much makeup gain later that the noise floor comes with it.

## Method

1. **Set a starting gain**, then measure. Do not try to compute the right gain in advance.

```
uad_set_preamp(target="LEAD VOX", source="Mic", gain_db=20, low_cut=true)
```

2. **Ask the performer for the loudest thing they will do**, then measure against that, not against
   a spoken test.

```
uad_watch_meters(targets=["LEAD VOX"], duration_s=10, threshold_db=-6)
```

`uad_watch_meters` polls at about 3 Hz, returns per-channel peaks, and exits as soon as any watched
channel crosses `threshold_db`. An early exit at `threshold_db=-6` means the source is already too
hot.

3. **Adjust in about 3 dB steps and re-measure.** Never move by more than 6 dB in one step, because
   a single big jump can clip the next take or bury it.

4. **Report the number you measured**, not a judgement. "Peaked at -4.2 dB, so I am dropping the
   preamp 6 dB and re-checking" is useful. "Sounds about right" is not.

5. **If the source is too hot even at minimum gain**, engage the pad:

```
uad_set_preamp(target="LEAD VOX", pad=true)
```

Then re-measure, because the pad drops the level by a fixed amount that differs per model.

## Reading meter output

`uad_peek_meters` is one-shot; `uad_watch_meters` polls. Both report level, peak and clip per
channel and flag channels sitting at the floor.

| Reading | Meaning |
|---|---|
| `-77.0` | device-silence floor. Silence **or** a dead digital link. Cannot tell them apart |
| `-144.0` | absolute floor |
| around -18 | healthy average for a performance |
| above -6 | too hot, back the gain off |
| clip true | the converter clipped, fix the gain and redo the take |

**On a digital input, a floor reading is a clock or cable suspicion, not a silence conclusion.**
See the onboarding skill's clocking reference or `troubleshooting.md`.

Filter noise out of a rig-wide read:

```
uad_peek_meters(kind="input", above_db=-60)
```

That shows only channels with something actually on them, which is the fastest way to find where a
source landed when the user is not sure which input they used.

## Starting points per source

Starting gains, to be measured and corrected immediately. Preamp gain ranges differ per model, so
these are directions, not settings.

| Source | Starting `gain_db` | Options |
|---|---|---|
| Loud dynamic mic close on a voice | ~25 to 35 | `low_cut=true` for proximity rumble |
| Condenser on a voice at speaking distance | ~20 to 30 | 48V required, after confirmation |
| Condenser as a room or overhead mic | ~35 to 45 | 48V required |
| Ribbon mic | high, often 40+ | **never** 48V. Confirm the mic before touching phantom power |
| Guitar or bass amp mic | ~20 to 30 | |
| Guitar or bass direct, passive pickup | ~15 to 25 | `hi_z=true` where the preamp exposes `HiZ` |
| Synth, drum machine, SP-404 | `source="Line"`, low gain | `pad=true` if it still clips |
| Turntable through an external phono preamp | `source="Line"`, low gain | the phono preamp does the correction |
| Digital input (ADAT, S/PDIF) | no preamp at all | level is set on the sending device |

## Faders are not gain

`fader_db` on the channel strip (via `uad_set_channels`) sets what the Console mix and the cue
buses hear. It does **not** change the level going to the recording in the DAW when the DAW is
recording the interface input. Fix a recording level with `uad_set_preamp(gain_db=...)`, and fix a
monitoring balance with `fader_db` or send levels.

A channel too quiet in the room but correct on the meter is a fader or send problem. A channel
correct in the room but clipping the meter is a gain problem.

## Unison changes the gain relationship

Loading a Unison plugin into `slot="unison"` models the physical preamp, and its own drive or input
controls interact with `gain_db`. Load the Unison plugin **first**, then gain stage, then dial the
plugin. Re-measure after any Unison change:

```
uad_load_plugin(target="LEAD VOX", plugin="<unison-capable name>", slot="unison")
uad_watch_meters(targets=["LEAD VOX"], duration_s=10, threshold_db=-6)
```

Plugin parameters are normalized 0 to 1 only, with no dB conversion, so read them before writing:

```
uad_get_plugin_params(target="LEAD VOX", slot="unison")
```
