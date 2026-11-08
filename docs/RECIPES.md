# Recipes

The server ships six prompts. If your MCP client exposes prompts (Claude Desktop and Claude Code
both do), you can invoke them by name and the model gets the full instruction text. This file is
the human-readable version of the same six, plus the livestream-with-a-guest and job-switching
recipes, a gain-staging guide and a worked ADAT expander walkthrough.

| Prompt | Use it when |
|---|---|
| `setup_vocal_tracking` | Getting a microphone recording-ready, end to end |
| `onboard_adat_expander` | Adding an 8-channel ADAT expander or a second interface in ADAT mode |
| `troubleshoot_clock` | A digital input reads nothing, or the clock will not lock |
| `panic_reset` | Feedback, a runaway level, anything unexpectedly loud |
| `rig_profile_interview` | The first session: tell the agent your gear once |
| `setup_livestream` | Streaming your mic plus your computer's audio |

A note that applies to all of them: every write this server makes is read back and reported as
`verified`. If a model tells you it changed something, ask it what the read-back said.

---

## 1. Set up an input for vocal tracking

The order matters. Gain is set against a real performance, not guessed, and phantom power is the
one step that waits for you.

1. **Check the rig.** `uad_get_status`: is the interface online, what is the sample rate and clock
   source, and which safety mode is active? In `readonly` mode nothing below will work, and the
   model should say so rather than trying.
2. **Find the strip.** `uad_list_channels(kind="input")` and pick the one with
   `has_preamp: true` that the microphone is actually plugged into. If it is not obvious, the model
   should ask instead of guessing.
3. **Label and clear it.** `uad_set_channels` with a meaningful `name` (for example `LEAD VOX`),
   `mute: false`, `fader_db: 0`, `hidden: false`, `solo: false`. A named strip is worth the ten
   seconds later.
4. **Preamp basics.** `uad_set_preamp` with `low_cut: true` for a close vocal, `pad: false`,
   `phase: false`, and `gain_db: 20` as a starting point only.
5. **Phantom power, deliberately.** 48V goes on only after you have said the microphone is a
   condenser: `uad_set_preamp(phantom_48v=true, confirm_condenser_mic=true)`. **48V can permanently
   destroy a ribbon microphone.** Without your confirmation the write is refused, which is correct
   behaviour, not a bug.
6. **Set the gain against the loudest thing they will actually do.**
   `uad_watch_meters(targets=["LEAD VOX"], duration_s=10, threshold_db=-6)`, aiming for peaks
   between -12 and -6 dBFS. Adjust `gain_db` in about 3 dB steps and re-measure. Never chase a
   target by more than 6 dB in one move.
7. **Character, if you want it.** `uad_list_plugins(unison_only=true, authorized_only=true)`, then
   `uad_load_plugin(target="LEAD VOX", plugin=..., slot="unison")` for the preamp emulation, and
   inserts for a compressor and an EQ. Each load reports `materialized`: if it is false the
   parameter nodes have not appeared yet and parameter writes will be skipped rather than lost.
8. **The singer's headphones.** `uad_set_sends` with `send: "cue1"` and `level_db` around -6, plus
   some of the backing track. The headphone output *level* cannot be set from here at all, so the
   cue balance is made entirely with send levels and the final trim is the physical knob.
   Read the sends first, because no tool reads them for you: `uad_raw_get(path=".../inputs/0/sends")`
   lists one node per send, and each node carries `ID` (`aux1`, `aux2`, `cue1` to `cue4`) plus
   `Gain`, `Pan` and `Bypass`. `Gain` runs -144 to +12 dB, and **-144.0 means that send is off**, so
   a `before: -144.0` in the write result is the normal starting point, not a failure. All four
   `cueN` nodes exist even on a two-cue Apollo: a write to `cue3` there verifies but comes back with
   a warning that it reaches no real output, and that warning is the answer, not a footnote.
9. **Control room.** `uad_set_monitor(level_db=...)`. The ceiling is enforced, and a request above it
   is refused outright rather than part-applied, so the right response is to tell you the stored
   level and the ceiling, not to retry. To read the current level, use
   `uad_list_channels(kind="output")` and take `monitor_level_db` from the strip with
   `is_monitor: true`: `uad_get_status` does not report it.
10. **Save it.** `uad_snapshot(action="save", name="vocal-tracking")`, so tomorrow's session starts
    from today's setup.

Session rules worth stating out loud to the model: never engage 48V without explicit confirmation,
never raise the monitor level to "check" something, and if anything squeals call `uad_safe_state`
first and ask questions afterwards.

---

## 2. Onboard an ADAT expander

Adding an ADA8200, a second interface in ADAT mode, or any 8-channel optical expander.

1. **Write down the clock facts first.** `uad_get_status` gives `clock.source`, `clock.locked` and
   `clock.sample_rate`. Those three decide everything else.
2. **Cabling, stated in ports you can look at.** The expander's ADAT OUT goes to the Apollo's ADAT
   IN. If the expander is going to be slaved to the Apollo, the Apollo's ADAT OUT must **also** go
   back to the expander's ADAT IN (or use word clock). Optical cables fail silently, so check both
   ends physically.
3. **Decide who is master. Exactly one device may be.**
   - *Apollo master* (`ClockSource: Internal`): the expander must be set to slave from ADAT (or word
     clock) on the return cable, at the same sample rate.
   - *Expander master*: `uad_set_clock(source="ADAT", confirm=true)`. It refuses while audio is
     flowing, so stop playback and recording first.
   - Never both internal, never both slaved. Both internal means nothing locks and audio drifts or
     clicks. Both slaved means nothing locks at all.
4. **Sample rates must match exactly.** At 88.2 or 96 kHz ADAT runs S/MUX, so the 8 channels become
   4. Channels 5 to 8 going dead at high rates is expected behaviour, not a fault.
5. **Name the strips.** `uad_list_channels(kind="input")` and find the ones with `io_type: "ADAT"`.
   With `uad_set_channels`, rename each to what is physically patched into the expander, set
   `hidden: false`, `enabled: true`, `fader_db: 0`, `mute: false`. The `active` flag in that listing
   is a different thing: it is read-only engine state, and `active: false` is normal on the right leg
   of a stereo pair, on unused strips and on some outputs. It is not a fault and not writable.
   `enabled` (`EnabledByUser`) is the one you set.
6. **Verify with signal, not with hope.** Send something through the expander, then
   `uad_peek_meters(targets=[...the ADAT channels...])`. If every ADAT meter sits at exactly the
   floor (-77 dB), that is the signature of no clock lock or a dead optical cable, **not** silence.
   Go to recipe 3.
7. **Snapshot it** once all 8 channels meter correctly: `uad_snapshot(action="save",
   name="adat-expander")`.

---

## 3. Troubleshoot clock and missing digital audio

For no signal on ADAT or S/PDIF inputs, a clock that will not lock, or garbled digital audio. Read
each step's numbers before acting on them.

1. `uad_get_status`. Note `engine.audio_streaming` first: if it is false the engine is passing no
   audio at all, no meter anywhere will move, and nothing below will tell you anything useful.
   Then note `clock.source`, `clock.locked`, `clock.sample_rate`.
2. **If `clock.locked` is false**, the Apollo is chasing a clock it cannot see: the external device
   is off, unplugged, or at a different rate, or `ClockSource` points at an input with no clock on
   it. To get back to known-good, stop all audio and call
   `uad_set_clock(source="Internal", confirm=true)`. That tool verifies lock and reverts itself if
   the new source does not lock.
3. **If `clock.locked` is true but a digital input reads nothing**, run `uad_peek_meters` on the
   ADAT and S/PDIF strips. Every digital meter pinned at exactly the floor is the classic signature
   of a dead digital link. A floor reading **cannot** tell silence from a dead link, so "the source
   is just quiet" is not a conclusion you are allowed to draw.
4. **Then check who is master.** Apollo master (`Internal`) means every outboard converter must be
   slaved to it, from the ADAT return cable or from word clock out. A device left on its own
   internal clock will never lock here. External master means the Apollo's `ClockSource` must be
   `ADAT`, `Word Clock` or `S/PDIF` to match, and the external rate must equal the Apollo's.
5. **Sample rate mismatch is the single most common cause.** 44.1 against 48 kHz shows up as no lock
   or as garbled, wrong-pitch audio. Compare the device's front panel to `clock.sample_rate`.
6. **S/PDIF is a special case: there is no return-cable trick.** One S/PDIF link, coax or optical,
   carries clock in one direction only, so you cannot slave the source the way a second ADAT cable
   slaves an expander. Three ways out, in order of preference: let the source clock the Apollo with
   `uad_set_clock(source="S/PDIF", confirm=true)` and matched sample rates; slave the source to the
   Apollo if it has a sync input of its own; or turn on sample-rate conversion for that input
   (`SRConvert` on the S/PDIF strip, readable with `uad_raw_get`, writable only in `full` mode with
   `uad_raw_set` or in Console). Meters at the floor with `ClockSource: Internal` and
   `SRConvert: false` is the classic signature of exactly this.
7. **Cabling.** Swap the optical cable. Confirm OUT goes to IN in both directions, and that you are
   not looking at an S/PDIF optical port. TOSLINK failures are silent.
8. **After every change**, `uad_get_status` again for `clock.locked`, then `uad_peek_meters` to
   confirm signal is actually arriving.

Why the confirmations exist: `uad_set_clock` and `uad_set_sample_rate` both refuse while audio is
flowing (any input above -60 dB) and both require `confirm=true`, because re-locking the clock
glitches or drops every running stream. Stop playback and recording first.

---

## 4. Panic reset

Something is wrong right now. Get safe first, diagnose second.

1. **Immediately** `uad_safe_state`. No parameters, no confirmation, no questions: it mutes every
   monitor output and turns talkback off.
2. `uad_get_status` to see where things stand.
3. **With the monitors still muted**, find the cause. `uad_list_channels` for channels that are
   unexpectedly unmuted, soloed, or sitting at a high `fader_db`; `uad_peek_meters` for what is
   actually hot. An open microphone next to its own monitor is the usual feedback path.
4. Fix the cause with `uad_set_channels`: mute the offender, `solo: false`, sensible `fader_db`.
5. **Only then** bring the monitors back: `uad_set_monitor(mute=false)`. If that is refused, the
   stored monitor level is above the safety ceiling: lower it first with
   `uad_set_monitor(level_db=...)`, then release the mute. Come up in steps. The rail allows at most
   +6 dB per call and +10 dB per 30 seconds, and anything **above** the ceiling is refused outright
   rather than trimmed to fit, so ask for a number at or below it. Because of that budget, climbing
   back out of a very low level through the server is deliberately slow: for a big jump, use the
   physical monitor knob instead of waiting the window out.
6. If the state is scrambled beyond quick repair, `uad_snapshot(action="list")`, then
   `uad_snapshot(action="restore", name=..., dry_run=true)` to see the exact ordered write list
   before running it with `dry_run=false`. A real restore captures a `pre-restore-*` snapshot first,
   so it is recoverable.

Do not, while sorting this out: raise the monitor level to hear whether it is fixed, unsolo or
unmute everything at once, change the clock source or sample rate, or engage 48V.

---

## 5. Tell the agent your rig once

The engine knows which strips exist. It does not know that an SM7B is on input 1, that input 2 is
a ribbon, or that your computer plays to VIRTUAL 1. Without that, every session starts with
questions, and 48V is one confident guess away from a dead ribbon.

1. Run the `rig_profile_interview` prompt, or just say "learn my setup".
2. The agent lists your inputs and asks, a few at a time: what is plugged in, what kind of source
   it is (dynamic, condenser, ribbon or line), which one is your voice, where computer audio plays,
   and what gear sits around the Apollo (expander, monitors, headphones, DAW, stream app).
3. It shows you the profile, then saves it with `uad_rig_profile(action="save", ...)`. It is a
   file on your Mac (`~/.uad-console-mcp/rig-profile.json`). Nothing is written to the Apollo.

What it changes: any input marked `ribbon` refuses 48V, even if the agent confirms a condenser,
until you update the profile. And `uad_mode` uses your roles as its defaults, so "set me up to
stream" just works.

---

## 6. Livestream your mic plus computer audio

The Apollo does this without extra software. Computer audio plays into a **Virtual** channel, so
it shows up as a Console strip next to your mic. Console mixes the two, with your UAD plugins on
the mic. The driver then offers that mix back to your Mac as two more inputs, **MON L/R**. Your
stream app records those.

```
macOS audio ──> VIRTUAL 1/2 strip ─┐
                                   ├─> Console monitor mix ──> MON L/R inputs ──> OBS / stream app
mic ──> ANALOG 1 (+ UAD plugins) ──┘                       └─> your headphones and monitors
```

1. `uad_get_status` and `uad_rig_profile()`. No profile? Do recipe 5 first, or name the mic.
2. `uad_mode(action="plan", mode="livestream")`. Read the summary and the warnings. It opens the
   mic and the computer-audio strip if they are muted or down, clears any solo (a solo would leave
   the stream with only one strip), and leaves faders that are already up where they are.
3. Say yes, and the agent runs it with `action="apply"`. It saves a snapshot first; restoring that
   snapshot undoes everything the mode changed.
4. The steps only you can do, which the plan spells out with your exact channel numbers:
   - **Computer audio to VIRTUAL 1/2.** Audio MIDI Setup > select the Apollo > Configure Speakers
     > Stereo: left and right to the VIRTUAL 1 and VIRTUAL 2 channels (21 and 22 on an x8). Then
     choose the Apollo in System Settings > Sound > Output.
   - **The stream app's input.** Pick the Apollo, inputs MON L/R (27-28 on an x8). If the app only
     uses inputs 1-2, which most do, move MON L/R to inputs 1-2 in UAD Console > Settings > I/O
     Matrix. There is a `Livestreaming` preset there. Your DAW's input numbers move while it is
     selected, so switch back for a recording session. `uad_get_routing` confirms where MON L/R
     ended up.
   - **No loop.** Do not set the stream app's monitoring output to the Apollo's virtual channels.
     The stream would play back into its own input.
5. Check: talk and play something while the agent runs `uad_watch_meters` on the mic and the
   computer-audio strip, watch the stream app's meter, and make a short test recording.

Monitors on with an open mic can feed back. Headphones are the safe setup. The mode warns about it
and never touches the monitor itself.

---

## 7. Livestream with a call guest (mix-minus)

A guest on Zoom, Discord or Riverside has to hear you and the computer audio, but not themselves,
or they get their own voice back half a second late. That needs a separate mix for the call: a
**mix-minus**.

1. Give the call app **its own virtual pair**, for example VIRTUAL 3/4. They may be disabled in
   the I/O Matrix; `uad_get_routing` says so, and Console > Settings > I/O Matrix turns them on.
2. `uad_mode(action="plan", mode="livestream", guest="VIRTUAL 3")` (or save role `call_guest` in
   the profile). The plan sends your mic and the computer audio to `aux2`, keeps the guest out of
   it, and mutes the aux2 return so the call mix is not added to your monitor mix a second time.
3. The call app's microphone is the Apollo's AUX2 L/R inputs (31-32 on an x8). Its speaker is the
   guest's virtual pair. The stream app keeps recording MON L/R, which has everyone.
4. The catch: most call apps only use channels 1-2 of a device, and they cannot pick AUX2 L/R or
   VIRTUAL 3/4 by themselves. Put a channel router in between (Rogue Amoeba Loopback, or BlackHole
   with an aggregate device), or run the call through a DAW.
5. Check with the guest: they should hear you and the music, and no echo of themselves. If they
   hear nothing from you at all, the AUX capture on your setup follows the aux return: unmute the
   aux2 return (`uad_set_channels(items=[{"target": "AUX 2", "kind": "aux", "mute": false}])`) and
   accept that the call mix also plays in your monitors. (Whether AUX L/R is taken before or after
   the return mute is not verified on hardware yet.)

---

## 8. Switch jobs, and switch back

| Say | Mode | What it does |
|---|---|---|
| "I'm going live" | `livestream` | Recipe 6 |
| "I'm recording a vocal / guitar" | `tracking` | Opens that input, sends it to `cue1` at -6 dB if it was off there, clears stray solos, tells you which headphone output plays `cue1` and which DAW outputs feed it. Gain and 48V stay with you |
| "I'm mixing now" | `mixing` | Mutes every hardware input in the Console mix (mics, line, ADAT, S/PDIF) and clears solos, so expander hiss and room mics stay out of the monitors. Virtual inputs stay open. Console mutes do not stop the DAW recording those inputs |

Every apply saves `pre-mode-<mode>-<time>`. To go back:
`uad_snapshot(action="restore", name="pre-mode-mixing-...")` shows the dry run, and
`dry_run=false` runs it. For a setup you want to recall by name ("Friday stream"), apply the mode,
adjust to taste, then `uad_snapshot(action="save", name="friday-stream")`.

What no mode ever does: raise or unmute the monitor, switch 48V, gain, pad or phase, change the
clock, sample rate or I/O Matrix, or move a fader or send that is already up.

---

## Gain staging with the meters this server can read

### Targets

| Measure | Target | Why |
|---|---|---|
| Average level while performing | about **-18 dBFS** | Leaves headroom and sits where analog-modelled plugins expect signal |
| Peaks on the loudest passage | **under -6 dBFS**, aim -12 to -6 | Keeps margin for a louder take without clipping the converter |
| `MeterClip` | never true | The converter clipped, and that cannot be undone after the fact |

Anything averaging near 0 dBFS is too hot no matter how it sounds. Anything averaging near
-50 dBFS needs so much makeup gain later that the noise floor comes with it.

### Method

1. **Set a starting gain, then measure.** Do not compute the "right" gain in advance:
   `uad_set_preamp(target="LEAD VOX", source="Mic", gain_db=20, low_cut=true)`.
2. **Measure against the loudest thing the performer will actually do**, not a spoken test:
   `uad_watch_meters(targets=["LEAD VOX"], duration_s=10, threshold_db=-6)`. An early exit means it
   is already too hot.
3. **Adjust in about 3 dB steps and re-measure.** Never more than 6 dB in one move: a single big
   jump either clips the next take or buries it.
4. **Quote the number, not a judgement.** "Peaked at -4.2 dB, dropping the preamp 6 dB and
   re-checking" is useful. "Sounds about right" is not.
5. **Too hot at minimum gain?** `uad_set_preamp(pad=true)`, then re-measure, because the pad's
   attenuation differs per model.

### Reading the numbers

| Reading | Meaning |
|---|---|
| `-77.0` (or -76.99999) | Device-silence floor. Silence **or** a dead digital link, indistinguishable |
| `-144.0` | Absolute floor. On a send's `Gain` (range -144 to +12 dB) the same number means that send is off |
| around -18 | Healthy average for a performance |
| above -6 | Too hot, back the gain off |
| `clip: true` | The converter clipped. Fix the gain and redo the take |

Two things the meters cannot do: they are polled at about 3 Hz (not sample-accurate, so a single
short transient can be missed), and `uad_watch_meters` is capped at 30 seconds per call by design.

On a **digital** input, a floor reading is a clock suspicion, not a silence conclusion.

---

## Worked example: eight ADAT channels that were not there

This is the real debugging session that shaped the tools, condensed. It is the single most common
ADAT failure, and it looks exactly like broken hardware.

**The symptom.** An 8-channel expander is connected to the Apollo's optical input. Console shows the
strips. Nothing meters, on any of them, ever.

**What the tools said.**

```
uad_peek_meters(kind="input")
-> "UG 1".."UG 8" all at level_db: -76.99999237060547, at_floor: true
   note: "A meter at the floor cannot distinguish a silent source from a dead digital link..."

uad_get_status()
-> clock: {source: "Internal", locked: true, sample_rate: 48000}
```

That combination is the whole diagnosis:

- All eight ADAT strips at exactly the floor. Eight independent sources do not all go silent at
  once, so this is one shared cause: the link, not the sources.
- `ClockSource: Internal` means **the Apollo is the clock master**.
- `ClockLocked: true` is a red herring here. The Apollo is locked to its own internal clock and
  reports so happily. It says nothing about whether the expander is locked to anything.
- And there was **one optical cable in the rig**: expander OUT into Apollo IN.

**The trap.** With the Apollo as master, the expander has to receive its clock from somewhere. The
only path for that is the return cable, Apollo ADAT OUT into expander ADAT IN, and there was no
return cable. So the expander ran on its own internal clock, the two boxes drifted, and the Apollo
never resolved a valid ADAT stream. From the mixer's point of view the input is digitally silent,
which is exactly the floor reading.

**The fix, either way round.**

*Option A, Apollo stays master (usually what you want):*

1. Add the second optical cable: Apollo ADAT OUT into expander ADAT IN.
2. Set the expander's clock switch to slave from ADAT (some units label it SYNC, EXT, or ADAT IN).
3. Match the sample rate on the expander to the Apollo's `clock.sample_rate`.
4. Leave the Apollo on `ClockSource: Internal`.

*Option B, expander is master:*

1. Set the expander to internal clock at the rate you want.
2. Stop all playback and recording, then `uad_set_clock(source="ADAT", confirm=true)`. It refuses
   while audio is flowing, verifies `ClockLocked` after 2.5 seconds, and reverts itself if the new
   source never locks.

**Verify with signal, not with a status flag.**

```
uad_watch_meters(targets=["UG 1","UG 2","UG 3","UG 4"], duration_s=10, threshold_db=-40)
-> {"triggered": true, "triggered_channels": ["UG 1"],
    "peaks_db": {"UG 1": -12.4, "UG 2": -18.9, "UG 3": -22.1, "UG 4": -21.7}}
```

`triggered: true` with real peaks is proof. `ClockLocked: true` on its own is not.

**Then name them**, because `UG 1` through `UG 8` tells you nothing at 2am, and snapshot the
result: `uad_snapshot(action="save", name="adat-expander")`.

**Two more expander gotchas from the same session.**

- **High rates halve the channel count.** At 88.2 or 96 kHz, ADAT uses S/MUX and carries 4 channels
  instead of 8. Channels 5 to 8 sitting at the floor at 96 kHz is the protocol working as designed.
- **Optical failures are silent.** A cracked TOSLINK cable, a cable in the S/PDIF optical port
  instead of the ADAT one, or a cable in the wrong direction all present identically: floor
  readings on every channel. Swap the cable before you suspect the converter.
