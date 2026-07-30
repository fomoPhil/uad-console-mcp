# Troubleshooting decision trees

Rules for all of these: read before you write, report each reading to the user before acting on it,
and change one thing at a time.

## Tree 1: "no sound from channel X"

**Step 1. Is signal arriving at all?**

```
uad_peek_meters(targets=["X"])
```

| Reading | Branch |
|---|---|
| Above about -60 dB | signal is arriving. Go to Step 2 (downstream) |
| At the floor (-77.0 or -144.0) on an **analog** input | Go to Step 3 |
| At the floor on a **digital** input (`io_type` ADAT or S-PDIF) | Go to Step 4. Do not conclude silence |

**Step 2. Signal is arriving, so the problem is downstream.**

```
uad_list_channels(kind="input")
```

Check, in this order:

1. `mute: true` on that channel. Fix: `uad_set_channels(items=[{"target": "X", "mute": false}])`.
2. `fader_db` very low or at the bottom of its range. Fix: set it to 0 and re-listen.
3. `solo: true` on **some other channel**. A solo anywhere mutes everything else. Scan the whole list
   for it and clear it: `uad_set_channels(items=[{"target": "<the soloed one>", "solo": false}])`.
4. `hidden: true` or `enabled: false`. The channel is working but not shown.
5. The monitor path: `uad_list_channels(kind="output")`, find the strip with `is_monitor: true`, and
   check its `mute` and `monitor_level_db`. If the monitor is muted, un-muting goes through the rail:
   if the stored level is above the ceiling the unmute is refused, and the fix is to lower the level
   first with `uad_set_monitor(level_db=...)` and then release the mute.
6. Where the channel is routed. The routing matrix can be read with `uad_raw_get` on
   `/inputroutes` but not changed here. If routing is wrong, that is a Console job.

**Step 3. Analog input at the floor.**

1. Is the source actually making sound? Ask.
2. Cable and connector, including a swap test.
3. Gain: `uad_set_preamp(target="X", gain_db=...)` and check `verified` in the result.
4. `source`: a mic plugged into a channel set to `Line`, or a line source on a channel set to `Mic`,
   both misbehave. `uad_set_preamp(target="X", source="Mic")`.
5. A condenser mic with no phantom power produces nothing at all. Confirm the mic type with the user
   first, then `uad_set_preamp(target="X", phantom_48v=true, confirm_condenser_mic=true)`.
   **Never enable 48V speculatively: it destroys ribbon microphones.**
6. `pad: true` on an already quiet source costs a fixed amount of level. Try `pad=false`.
7. A guitar with a passive pickup into a line input is quiet and thin. Try `hi_z=true` where the
   preamp exposes `HiZ`.

**Step 4. Digital input at the floor: go to the clock chain.**

**A meter at exactly the floor cannot distinguish silence from a dead link.** On ADAT or S/PDIF, a
whole block of channels at the floor is a clock or cabling fault until proven otherwise.

```
uad_get_status()
```

Read `clock.source`, `clock.locked`, `clock.sample_rate`, then work the table:

| State | Meaning | Fix |
|---|---|---|
| `locked: false` | the Apollo is chasing a clock it cannot see | the external box is off, unplugged, at a different rate, or `ClockSource` points at an input with no clock. Known-good recovery: stop audio, `uad_set_clock(source="Internal", confirm=true)` |
| `locked: true`, `source: Internal`, one optical cable | the Apollo is master but the expander never receives its clock | add a return cable from the Apollo's ADAT OUT to the expander's ADAT IN **and** set the expander's own sync switch to slave from ADAT IN. Or make the expander the master: `uad_set_clock(source="ADAT", confirm=true)` |
| `locked: true`, `source: ADAT`, still nothing | the expander is meant to be master but is not sending clock | expander powered and on internal clock, cable in the right port, rate matching |
| rates differ between boxes | most common cause of all | match them exactly. `uad_set_sample_rate(rate=48000, confirm=true)` |

Key facts to state to the user:

- **Exactly one device may be the clock master.** Two masters: nothing locks, audio drifts or clicks.
  Two slaves: nothing locks at all.
- **One optical cable carries clock in one direction only.** The device sending audio sends the
  clock; the receiving device sends nothing back.
- **`clock.locked: true` only means the Apollo locked to whatever it was told to follow.** It says
  nothing about the box at the other end.
- TOSLINK failures are silent, so swap the cable rather than trusting it, and confirm the port is the
  ADAT port and not a look-alike optical S/PDIF port.

Verify after every change:

```
uad_get_status()
uad_watch_meters(targets=["<the digital channels>"], duration_s=10, threshold_db=-60)
```

`uad_set_clock` and `uad_set_sample_rate` both require `confirm=true` and refuse while audio is
flowing (any input above -60 dB). `uad_set_clock` verifies `ClockLocked` after about 2.5 s and
reverts itself if the new source does not lock: a revert is a real answer, meaning that clock is not
present.

## Tree 2: "clicks and pops"

**Treat this as a clock mismatch until proven otherwise.** Intermittent clicks with everything
apparently working is the signature of two devices running on near-identical but independent clocks.

```
uad_get_status()
```

1. **Who is master?** Exactly one device. Walk the table in Tree 1, Step 4.
2. **Are all boxes at the same sample rate, to the digit?** 44.1 against 48 kHz produces clicks, and
   in the worst case audio at the wrong pitch and speed.
3. **Is every slave really slaved?** A box left on internal clock while the Apollo is also internal
   is the classic case. The expander's own switch or menu is part of the fix and the server cannot
   reach it.
4. **Cable quality.** Swap the optical cable.
5. **Only after the clock chain is clean**, consider host-side causes (buffer size, CPU load, a
   Thunderbolt chain problem). Those are outside this server: say so rather than guessing at them
   through the mixer.

## Tree 3: "a channel is missing in Console"

```
uad_list_channels(kind="input", include_hidden=true)
```

| Finding | Cause | Fix |
|---|---|---|
| Channel present with `hidden: true` | `ChannelHidden` | `uad_set_channels(items=[{"target": N, "hidden": false}])` |
| Present but `active: false` or `enabled` off | `EnabledByUser` | `uad_set_channels(items=[{"target": N, "enabled": true}])` |
| Not in the list at all, ADAT channels 5 to 8 | S/MUX at a high sample rate | at 88.2/96 kHz an ADAT port carries 4 channels, at 176.4/192 kHz only 2. Expected. Drop to 48 kHz to get all 8 back |
| A whole unit's channels gone | that Apollo is offline | check `devices[].online` in `uad_get_status`, then power and Thunderbolt |
| Channel exists but Console shows something different | Console and the engine are both clients of the same engine, so they agree. A stale view usually means the rate changed under it | `uad_get_status` is the authority. Re-read it |

Channels **cannot be created or deleted**: the strip list is fixed by the hardware model. A channel
that does not exist at any sample rate is not a bug to fix.

## Tree 4: feedback, a runaway level, anything unexpectedly loud

**Do this first, before diagnosing anything:**

```
uad_safe_state()
```

No arguments, no confirmation. It mutes every monitor output and turns talkback off.

Then, with the monitors still muted:

```
uad_get_status()
uad_list_channels(kind="input")
uad_peek_meters(kind="input")
```

Look for channels unexpectedly unmuted, soloed, or sitting at a high `fader_db`, and for what is
actually hot on the meters. An open microphone next to the monitor it is feeding is the usual
feedback path.

Fix the cause with `uad_set_channels` (mute the offender, `solo: false`, sensible `fader_db`), and
only then bring the monitors back:

```
uad_set_monitor(mute=false)
```

If that is refused, the stored monitor level is above the safety ceiling. Lower it first with
`uad_set_monitor(level_db=...)`, then release the mute, and come up in steps: the rail allows at most
+6 dB per call and +10 dB per 30 s.

**While sorting this out, do not:** raise the monitor level to hear whether it is fixed; unsolo or
unmute everything at once; change the clock source or sample rate; enable 48V.

## Tree 5: a write did not take effect

1. **Check `verified` in the result.** Every write reports `{verified, before, after, warnings}`. If
   `verified` is false, the value did not stick and `warnings` says why.
2. **Was it refused?** A `SafetyRefusal` message always names the env var or confirm flag that would
   unblock it. Relay that text rather than retrying. Common ones: monitor rail, 48V without
   `confirm_condenser_mic=true`, clock or rate without `confirm=true`, a property not on the
   safe-mode allowlist.
3. **Is the mode `readonly`?** `uad_get_status` reports `safety.mode`. In `readonly` every write is
   refused by design.
4. **Was the target resolved to the channel you meant?** Every result includes the resolved
   `{device, index, name}`. On a multi-unit rig, an ambiguous name error lists the candidates: re-call
   with `device=N`.
5. **Plugin parameters silently skipped?** Parameter nodes exist only after a plugin is loaded.
   `uad_load_plugin` polls up to 5 s and reports `materialized`. If it is false, load again or check
   authorization with `uad_list_plugins`.
6. **An unauthorized or demo plugin loads but does not process audio.** Check `authorized` in
   `uad_list_plugins`.

## Tree 6: the server itself is not responding

The error text says it: the UA Mixer Engine listens on 127.0.0.1:4710, ships with the UAD software,
starts with UAD Console and keeps running after Console is closed.

1. Is the UAD software installed and has Console been launched at least once since boot?
2. Is the Apollo powered and connected? `uad_get_status` reports `engine.connected` separately from
   `devices[].online`, so an engine that is up with an offline unit is a different fault from an
   engine that is down.
3. The client reconnects on its own with backoff, so a single failed call followed by a working one
   is normal after a sleep or a cable event. Re-read status rather than assuming state.
