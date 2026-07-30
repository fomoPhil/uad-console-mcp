---
name: apollo-studio-tasks
description: Runs day-to-day Universal Audio Apollo studio operations through the uad-console-mcp MCP server, including setting up a channel for vocal or instrument tracking with a Unison preamp and correct gain staging, building cue and headphone mixes for one or several performers, wiring up podcast and streaming setups with per-mic processing and computer audio, working troubleshooting decision trees, and snapshotting before experiments. Use when the user asks to set up a mic to record, set or check gain, load a plugin or Unison preamp, build a headphone or cue mix, get a podcast or livestream going, mute or unmute the control room, or reports symptoms like "no sound from channel 3", "clicks and pops", "a channel disappeared from Console", or asks to save the current state before changing something.
---

# Apollo studio tasks

**Bottom line: read the rig, do one task at a time with the literal sequences below, and check
`verified` on every write.** Channel counts, monitor output indexes, cue bus counts and available
plugins differ per rig. Discover them, never assume them.

Start every session with:

```
uad_get_status()
uad_list_channels(kind="input")
```

If `safety.mode` is `readonly`, say so up front: nothing can be changed. Exact tool and parameter
names for everything below are in `references/tools.md`.

## Etiquette rails (apply to every task)

- **Monitor level:** never jump it. The server rails it (default ceiling -6 dB, max +6 dB per
  call, +10 dB per 30 s) and treats unmute and un-dim as level events. When a call is refused,
  relay the refusal text verbatim: it names the env var or confirm flag that would unblock it, and
  the usual fix is to lower the stored level first, then release the mute.
- **Never raise the monitor level to check whether something works.** Use meters instead.
- **48V:** ask what the microphone is before enabling phantom power. It permanently damages ribbon
  microphones. Both `phantom_48v=true` and `confirm_condenser_mic=true` are required.
- **Talkback auto-off:** `uad_talkback(on=true)` starts a 30 s auto-off timer by default. Calling
  it again replaces the timer. Never leave talkback open, and never remove the timer to be
  convenient.
- **Anything unexpectedly loud, feedback included:** `uad_safe_state()` first, diagnose second.
- **Headphone output level cannot be set** by this protocol. Build the balance with cue sends and
  let the performer use the physical knob.
- **The server cannot save a Console session.** Remind the user to press Cmd+S in Console.

## Task: tracking a vocal or an instrument

**1. Find the input.** It must have `has_preamp: true`. If more than one candidate fits, ask
rather than guessing.

```
uad_list_channels(kind="input")
```

**2. Name it and set a clean starting state.**

```
uad_set_channels(items=[
  {"target": 1, "name": "LEAD VOX", "fader_db": 0, "mute": false, "solo": false, "hidden": false}
])
```

**3. Set the input type and preamp options.**

```
uad_set_preamp(target="LEAD VOX", source="Mic", gain_db=20, low_cut=true, pad=false, phase=false)
```

- `source="Line"` for a synth, drum machine or mixer output.
- `hi_z=true` for a guitar or bass with a passive pickup, on channels whose preamp exposes `HiZ`.
- `pad=true` when a hot source is clipping the preamp before the gain control helps.
- 48V, only after the user confirms a condenser:

```
uad_set_preamp(target="LEAD VOX", phantom_48v=true, confirm_condenser_mic=true)
```

**4. Gain stage against a real performance.** Ask the user to perform the loudest passage, then:

```
uad_watch_meters(targets=["LEAD VOX"], duration_s=10, threshold_db=-6)
```

Targets: **average around -18 dBFS, peaks under -6 dBFS** (aim -12 to -6 on the loudest passage).
Adjust in about 3 dB steps and re-measure. **Never chase a target by more than 6 dB in one step**,
and report the number you actually measured. Full method in `references/gain-staging.md`.

**5. Unison preamp, if wanted.** The Unison slot models the physical preamp, so it belongs before
the inserts.

```
uad_list_plugins(unison_only=true, authorized_only=true)
uad_load_plugin(target="LEAD VOX", plugin="<name from that list>", slot="unison")
```

Check `materialized` in the result. If it is false the parameter nodes never appeared, so
parameter writes for that slot will be skipped rather than silently lost.

**6. Insert chain.** Slots are 0 upward; a compressor then an EQ is a sane default.

```
uad_load_plugin(target="LEAD VOX", plugin="LA-2A", slot=0, preset="Vocal")
uad_load_plugin(target="LEAD VOX", plugin="Pultec EQP-1A", slot=1)
uad_get_plugin_params(target="LEAD VOX", slot=0)
uad_set_plugin_params(target="LEAD VOX", slot=0, params=[{"name": "Peak Reduction", "normalized": 0.4}])
```

Parameters are **normalized 0 to 1 only**, addressed by exact name. Read them before writing them.

**7. Cue mix for the talent.** See the next task.

**8. Snapshot, then hand back.**

```
uad_snapshot(action="save", name="vocal-tracking")
```

Report the measured peak, what is loaded where, and remind the user about Cmd+S in Console.

## Task: cue mixes for one or several performers

Each performer gets their own cue bus. How many exist is per model: read `cue_buses` from
`uad_get_status`, and if a send name is wrong the error lists the available send IDs.

**One performer, hearing themselves plus a backing track:**

```
uad_set_sends(items=[
  {"target": "LEAD VOX",  "send": "cue1", "level_db": -6},
  {"target": "DAW L",     "send": "cue1", "level_db": -12},
  {"target": "DAW R",     "send": "cue1", "level_db": -12}
])
```

**Two performers who want different balances:** same sources, different buses, different levels.

```
uad_set_sends(items=[
  {"target": "LEAD VOX", "send": "cue1", "level_db": -4},
  {"target": "GUITAR",   "send": "cue1", "level_db": -14},
  {"target": "LEAD VOX", "send": "cue2", "level_db": -14},
  {"target": "GUITAR",   "send": "cue2", "level_db": -4}
])
```

Then the headphone output that each performer uses is pointed at the right cue bus in Console via
`MixInSource`. **This server cannot set the headphone level**, only the sends feeding it. Say that
plainly instead of promising a level change.

Mono considerations: `uad_set_monitor(mono=true)` folds the **control room** to mono, which is a
mix-checking tool, not a cue tool. It does not change what a performer hears in headphones. A
performer who wants a centred mono cue gets it by panning their own sends to centre:

```
uad_set_sends(items=[{"target": "LEAD VOX", "send": "cue1", "level_db": -6, "pan": 0}])
```

More patterns, including talkback and drummer-in-the-live-room setups:
`references/cue-mixes.md`.

## Task: podcast or livestream

Multiple mics, per-mic processing, and computer audio coming back in. Full walkthrough with a
two-host and a remote-guest layout: `references/podcast-streaming.md`.

Skeleton:

```
uad_list_channels(kind="input")
uad_set_channels(items=[
  {"target": 1, "name": "HOST",  "fader_db": 0, "mute": false, "hidden": false},
  {"target": 2, "name": "GUEST", "fader_db": 0, "mute": false, "hidden": false}
])
uad_set_preamp(target="HOST",  source="Mic", gain_db=30, low_cut=true)
uad_set_preamp(target="GUEST", source="Mic", gain_db=30, low_cut=true)
uad_watch_meters(targets=["HOST","GUEST"], duration_s=15, threshold_db=-6)
uad_load_plugin(target="HOST",  plugin="<a compressor>", slot=0)
uad_load_plugin(target="GUEST", plugin="<a compressor>", slot=0)
uad_set_sends(items=[
  {"target": "HOST",  "send": "cue1", "level_db": -8},
  {"target": "GUEST", "send": "cue1", "level_db": -8}
])
uad_snapshot(action="save", name="podcast-2mic")
```

**Computer audio** (a remote guest on a call, music beds, video playback) arrives on channels whose
`io_type` is `Virtual`. Find them by that flag, name them, and send them into the cue buses so the
hosts can hear the remote guest:

```
uad_set_channels(items=[{"target": "<virtual channel index>", "name": "REMOTE", "hidden": false}])
uad_set_sends(items=[{"target": "REMOTE", "send": "cue1", "level_db": -10}])
```

Which application feeds which virtual channel is set in Console and in the operating system's
audio settings. **The routing matrix is not writable here**, so hand that step to the user and
verify the result with `uad_peek_meters`.

## Task: troubleshooting

Work the tree, report each reading before acting on it. Full trees in
`references/troubleshooting.md`.

**"No sound from channel X"**

```
uad_peek_meters(targets=["X"])
```

1. **Meter shows signal?** Then the input is fine and the problem is downstream: check `mute`,
   `fader_db`, `solo` on another channel stealing the bus, and where the channel is routed.
   `uad_list_channels(kind="input")` shows all of it at once.
2. **Meter at the floor (-77.0) on an analog input?** Check gain, the cable, phantom power for a
   condenser, and whether the source is actually making sound. `uad_set_preamp` reads back what it
   set, so confirm `verified`.
3. **Meter at the floor on a digital input (ADAT or S/PDIF)?** Do not conclude silence. A floor
   reading cannot distinguish silence from a dead link. Go to the clock chain:

```
uad_get_status()      # clock.source, clock.locked, clock.sample_rate
```

Exactly one device in the chain may be the clock master. One optical cable carries clock in one
direction only, so `ClockSource: Internal` with a single cable leaves an expander unclocked and
every one of its channels at the floor. Fix is either a return cable from the Apollo's ADAT OUT to
the expander's ADAT IN plus the expander's own sync switch set to slave, or making the expander the
master with `uad_set_clock(source="ADAT", confirm=true)`.

**"Clicks and pops"** is a clock mismatch until proven otherwise: two masters, a slave that is not
really slaved, or two boxes at different sample rates. Same chain as above.

**"A channel is missing in Console"**

1. `ChannelHidden` is true, so `uad_set_channels(items=[{"target": N, "hidden": false, "enabled": true}])`.
2. Or a sample-rate change removed it: at 88.2/96 kHz ADAT S/MUX gives 4 channels per port instead
   of 8, and at 176.4/192 kHz only 2. Channels 5 to 8 vanishing after a rate change is expected.
3. Or the unit it lives on is offline: check `devices[].online` in `uad_get_status`.

**Feedback or a runaway level:** `uad_safe_state()` immediately, then diagnose with
`uad_list_channels` and `uad_peek_meters` while the monitors stay muted. Bring the monitors back
last, in steps, through `uad_set_monitor`.

## Habit: snapshot before you experiment

Do this before any change the user might want to undo: loading a chain, re-gain-staging a whole
rig, rebuilding cue mixes.

```
uad_snapshot(action="save", name="before-experiment")
... make changes ...
uad_snapshot(action="diff", name="before-experiment", ignore=["*/meters/*"])
uad_snapshot(action="restore", name="before-experiment", dry_run=true)   # show the write list
uad_snapshot(action="restore", name="before-experiment", dry_run=false)  # only after the user agrees
```

- `dry_run=true` is the default and returns the exact ordered write list. **Show it to the user
  before running the real restore.**
- A real restore captures a `pre-restore-<timestamp>` snapshot first, so it is itself reversible.
- Restore does **not** put back clock source, sample rate or the monitor rail unless asked with
  `include_clock=true` or `include_monitor=true`. Leave those off unless the user asks.
- Snapshots live in `~/.uad-console-mcp/snapshots/`. `uad_snapshot(action="list")` shows them.
- Restore refuses across different Apollo models unless `allow_cross_model=true`. Prefer fixing
  the rig over forcing it.

## References

- `references/tools.md`: exact tool names, parameters, safety behaviour, hard limits.
- `references/gain-staging.md`: gain method, meter targets, reading `uad_peek_meters` and
  `uad_watch_meters` output, per-source starting points.
- `references/cue-mixes.md`: cue bus patterns for several performers, talkback, mono questions.
- `references/podcast-streaming.md`: multi-mic podcast and stream layouts, virtual channels.
- `references/troubleshooting.md`: the full decision trees, including clock faults and missing
  channels.
