# Clocking, sample rates and dead digital links

Everything here is about digital inputs: ADAT, S/PDIF, word clock. Analog mic and line inputs have
no clock relationship with anything.

## The three rules

1. **One master, everything else slaved.** Two masters and nothing locks: audio drifts, clicks or
   arrives garbled. Two slaves and nothing locks at all.
2. **One optical cable carries clock in one direction only.** The device sending audio also sends
   the clock on that cable. The receiving device sends nothing back on it.
3. **Both boxes must sit at exactly the same sample rate.** 44.1 against 48 kHz shows up as no
   lock, or as audio that plays at the wrong pitch and speed.

## Reading the current state

```
uad_get_status()
```

Relevant fields:

| Field | Meaning |
|---|---|
| `clock.source` | which clock the Apollo is following (`Internal` means it is the master) |
| `clock.locked` | whether it has actually locked to that source |
| `clock.sample_rate` | the rate the whole rig is running at |
| `engine.audio_streaming` | whether the engine believes audio is moving |

To see the exact enum values this engine accepts before writing a clock source:

```
uad_raw_get(path="/ClockSource")
```

The reply includes `values`. Use the strings it reports. Spelling of the S/PDIF entry varies
between engine versions (this engine reports `S/PDIF`), so never hardcode it.

## The two valid arrangements for an ADAT expander

| Arrangement | Apollo `ClockSource` | Expander sync setting | Cables required |
|---|---|---|---|
| Expander is master | `ADAT` | its own internal clock | expander ADAT OUT to Apollo ADAT IN |
| Apollo is master | `Internal` | slave from ADAT IN (or word clock in) | the above **plus** Apollo ADAT OUT to expander ADAT IN |

Word clock over BNC is an alternative to the return optical cable when both boxes have the ports.

Setting the Apollo side:

```
uad_set_clock(source="ADAT", confirm=true)       # expander is master
uad_set_clock(source="Internal", confirm=true)   # Apollo is master
```

`uad_set_clock` refuses while audio is flowing (any input above -60 dB), requires `confirm=true`,
waits about 2.5 s, checks `ClockLocked`, and reverts itself if the new source does not lock. A
revert is a real answer: it means the clock the user pointed at is not present.

## S/PDIF: no return-cable option

The "Apollo is master" row above does not exist for S/PDIF. One S/PDIF link, coax or optical, carries
clock in one direction only, so there is no second cable that would slave the source. Three ways to
make an S/PDIF source work, in order of preference:

1. **Source clocks the Apollo:** `uad_set_clock(source="S/PDIF", confirm=true)`, both boxes at the
   same sample rate.
2. **Slave the source to the Apollo** if it has a word clock or sync input of its own.
3. **Sample-rate conversion on that input.** Each S/PDIF strip carries a boolean `SRConvert`,
   readable with `uad_raw_get(path="/devices/0/inputs/16/SRConvert")`. No tool writes it: that needs
   `uad_raw_set` in `full` mode, or Console.

Meters at the floor with `ClockSource: Internal` and `SRConvert: false` is the classic S/PDIF
signature: the Apollo is master, the source runs on its own clock, and nothing converts between them.

## Sample rate and ADAT channel counts

| Sample rate | Channels per ADAT optical port | Mechanism |
|---|---|---|
| 44.1 / 48 kHz | 8 | normal |
| 88.2 / 96 kHz | 4 | S/MUX |
| 176.4 / 192 kHz | 2 | S/MUX4 |

Consequences to explain rather than debug:

- Channels 5 to 8 of an expander going silent after a move to 88.2 or 96 kHz is the protocol
  working correctly.
- The channel count in Console can change with the rate. A strip that "disappeared" after a rate
  change is usually an S/MUX channel that no longer exists.
- **Many budget expanders top out at 48 kHz.** The Behringer ADA8200 is one. Verify from the
  unit's own manual or panel, not from the Apollo.

Changing the rate:

```
uad_set_sample_rate(rate=48000, confirm=true)
```

Refused while audio is flowing. Every running stream glitches when it succeeds, so warn the user
first and stop playback and recording.

## No signal on a digital input: decision tree

Work top to bottom and report each reading before acting on it.

**1. What does the meter say?**

```
uad_peek_meters(kind="input")
```

`-77.0` is the device-silence floor and `-144.0` is the absolute floor. A meter at exactly the
floor **cannot** distinguish silence from a dead link. Never conclude "the source is just quiet"
from a floor reading on a digital input.

**2. Is the clock locked?**

`clock.locked: false` means the Apollo is chasing a clock it cannot see. Causes, in order of
likelihood:

- the external device is off, unplugged, or at a different rate
- `ClockSource` points at an input that has no clock arriving on it
- the optical cable is dead or plugged into the wrong port

Known-good recovery: stop all audio, then `uad_set_clock(source="Internal", confirm=true)`.

**3. Locked, but the digital inputs read nothing.** Now the master question. Ask which device the
user believes is the master, then check it against the table above. The classic failure is Apollo
on `Internal` with a single optical cable: the expander is left on its own internal clock, no
return clock reaches it, and every one of its channels arrives at the meter floor.

**4. Sample-rate mismatch.** Compare the external box's front panel to `clock.sample_rate`.
Mismatch is the single most common cause of both no-lock and garbled or wrong-pitch audio.

**5. Cabling.** TOSLINK failures are silent, so swap the cable rather than trusting it. Confirm
OUT goes to IN in each direction, and confirm the port really is the ADAT port and not an optical
S/PDIF port that looks identical.

**6. Are the channels enabled and visible?** A channel hidden in Console (`hidden: true`) or with
`enabled: false` will not appear even when signal is arriving:

```
uad_list_channels(kind="input")
uad_set_channels(items=[{"target": 8, "hidden": false, "enabled": true}])
```

**7. After every change, verify twice.**

```
uad_get_status()                                          # clock.locked true, source as planned
uad_watch_meters(targets=["UG 1","UG 2"], duration_s=10, threshold_db=-60)
```

`uad_watch_meters` polls at about 3 Hz, exits as soon as any watched channel crosses
`threshold_db`, and caps at 30 s per call.

## Clicks, pops and drift

Intermittent clicks with everything apparently locked is the signature of two devices running on
near-identical but independent clocks. The chain to check is the same as above: exactly one
master, everyone else slaved to it, all boxes at one rate, and a real clock path (return optical
cable or word clock) reaching every slave.

## Symptom-to-cause quick table

| Symptom | Most likely cause |
|---|---|
| All ADAT channels at the meter floor, `clock.locked` true | Expander never receives the Apollo's clock (missing return cable, or expander not switched to slave) |
| `clock.locked` false | `ClockSource` points at an absent clock, or rate mismatch, or dead cable |
| Channels 5 to 8 silent | Running at 88.2/96 kHz, S/MUX halves the channel count |
| Audio at the wrong pitch or garbled | Sample-rate mismatch between the boxes |
| Intermittent clicks | Two masters, or a slave that is not really slaved |
| Strip missing in Console | `ChannelHidden`, `EnabledByUser` false, or the rate change removed the channel |
