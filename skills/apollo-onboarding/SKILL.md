---
name: apollo-onboarding
description: Plans and configures a Universal Audio Apollo rig end to end through the uad-console-mcp MCP server, covering the interview questions to ask, which physical input suits which source, the clock master decision for ADAT expanders and other digital gear, ADAT sample-rate and channel-count limits, multi-Apollo Thunderbolt cascades, and channel naming and hiding. Use when the user says they just got an Apollo or wants help setting one up, lists their gear ("I have an Apollo x8, a Behringer ADA8200, KRK monitors, an SP-404, a turntable with a phono preamp"), asks to connect an ADAT expander or a second Apollo, asks where to plug a source in, asks which box should be the clock master, asks to rename or tidy the Console channel strips, or reports that newly connected digital inputs show no signal.
---

# Apollo onboarding

**Bottom line: discover reality, plan the wiring, settle the clock, then configure. Never assume
per-model I/O.** Channel counts, input types and monitor-output indexes differ across Apollo
models. `uad_list_channels` and `uad_raw_get` report what is actually there; use them instead of
guessing from the model name.

## Rails that apply to every step

- **Read before you write.** `uad_get_status` and `uad_list_channels` first, every time.
- **Never raise the monitor level for the user beyond the rail.** The server caps monitor level
  at `UAD_MCP_MAX_MONITOR_DB` (default -6 dB), allows at most +6 dB per call and +10 dB per 30 s,
  and treats unmute and un-dim as level events. If a call is refused, relay the refusal text: a
  `SafetyRefusal` message always names the env var or confirm flag that would unblock it. Do not
  retry blindly.
- **Ask before 48V.** `uad_set_preamp(phantom_48v=true)` also needs
  `confirm_condenser_mic=true`, because phantom power can destroy a ribbon microphone. If the user
  has not said what the mic is, ask.
- **Clock and sample-rate changes need `confirm=true`** and are refused while audio is flowing
  (any input above -60 dB). Stop playback and recording first, and say so before changing either.
- **The server cannot save a Console session.** After configuring, remind the user to press
  Cmd+S in the Console app. `uad_snapshot` is this server's own state file, not a Console session.

## Step 0: prove the engine is reachable

```
uad_get_status()
```

Note `engine.connected`, `clock.source`, `clock.locked`, `clock.sample_rate`, every entry in
`devices` (a cascade shows more than one), and `safety.mode`. If the mode is `readonly`, stop and
tell the user that nothing can be changed until `UAD_MCP_MODE` is set to `safe` or `full`.

If the engine is unreachable the error says so plainly: the UA Mixer Engine listens on
127.0.0.1:4710 and starts with the UAD software, staying up after Console is closed.

## Step 1: interview the user

Ask these before planning anything. Do not fill gaps with assumptions.

1. **Which Apollo model or models?** Confirm against `devices[].type_id` from
   `uad_get_status`. If there is more than one unit, ask which one the monitors are connected to.
2. **What sources?** For each one: microphone (condenser, dynamic or ribbon?), line-level box,
   instrument with a passive pickup, or a digital device.
3. **Any digital gear?** ADAT expander, S/PDIF box, another interface in ADAT mode, an outboard
   converter. Every digital box forces a clock decision (Step 3).
4. **Monitors and headphones.** Which output pair do the monitors use, and who needs a cue mix?
5. **Sample rate needs.** 44.1 or 48 kHz for most work. Ask whether anything in the chain is
   limited to 48 kHz, and whether any high-rate work is planned, because that halves or quarters
   ADAT channel counts (Step 4).
6. **Purpose.** Tracking, podcast, streaming, mixing, or a mix of them. This decides how many cue
   buses matter and what the channel names should be.

## Step 2: plan the physical connections

Match each source to an input type, then confirm the type exists on this rig by looking at
`io_type` and `has_preamp` in `uad_list_channels(kind="input")`.

| Source | Input to use | Notes |
|---|---|---|
| Microphone | Mic input (a channel with `has_preamp: true`), XLR | 48V only for condensers, after confirmation |
| Synth, drum machine, SP-404, mixer output | Line input | `uad_set_preamp(source="Line")` where the channel supports switching |
| Guitar or bass straight in, passive pickup | Hi-Z / instrument input | Only channels whose preamp exposes `HiZ`; set with `uad_set_preamp(hi_z=true)` |
| Turntable with an external phono preamp | Line input | The phono preamp does the RIAA correction; a bare turntable into a line input will be thin and quiet |
| Another interface, ADAT expander, 8-channel preamp with ADAT out | Optical ADAT IN | 8 channels at 44.1/48 kHz, fewer at higher rates (Step 4) |
| CD player, digital mixer, coax or optical stereo digital | S/PDIF IN | 2 channels; the optical S/PDIF port is not the ADAT port even when it looks identical |
| DAW or computer playback | Virtual channels (`io_type: "Virtual"`) | Routing of computer audio to those channels is set in Console and the OS, not here |

**The routing matrix is not writable through this server.** `/inputroutes` and `/outputroutes`
can be inspected with `uad_raw_get` but re-patching needs the Console app itself. Say so rather
than promising a re-patch.

## Step 3: clocking is a first-class step, not an afterthought

**Every digital box in the chain must run on one shared clock, and exactly one device may be the
master.** Two masters means nothing locks and audio drifts or clicks. Two slaves means nothing
locks at all.

**One optical cable carries clock in one direction only.** An expander's ADAT OUT into the
Apollo's ADAT IN gives the Apollo the expander's audio and the expander's clock. It gives the
expander nothing. That single fact explains most dead ADAT links.

Pick one of the two arrangements:

**A. Expander is the master** (simplest with one cable)

```
uad_set_clock(source="ADAT", confirm=true)
```

The Apollo slaves to the clock arriving on its ADAT IN. The expander stays on its own internal
clock, and its sample rate must equal the Apollo's. Read the real enum before writing:
`uad_raw_get(path="/ClockSource")` returns the `values` list this engine accepts (typically
`Internal`, `Word Clock`, `ADAT`, `S-PDIF`; spellings vary, so use what the engine reports).

**B. Apollo is the master** (`ClockSource: Internal`, needs a second cable)

1. Keep or set `uad_set_clock(source="Internal", confirm=true)`.
2. Run a **return optical cable from the Apollo's ADAT OUT to the expander's ADAT IN**. Without
   it the expander has no way to hear the Apollo's clock.
3. Set the expander's own sync switch to slave from ADAT IN. On the Behringer ADA8200 this is a
   rear-panel SYNC switch; other boxes use a menu or a different switch. Tell the user to check
   the box's own panel or manual.
4. Word clock is the alternative to the return optical cable when both boxes have BNC word clock
   ports.

Then verify, in this order:

```
uad_get_status()                      # clock.locked must be true, clock.source as planned
uad_watch_meters(targets=["<the digital channel names>"], duration_s=10, threshold_db=-60)
```

**A meter sitting at exactly the floor (-77.0 dB) cannot tell silence from a dead link.** If
every digital input is pinned at the floor while the source is definitely playing, treat it as a
clock or cabling fault, not a quiet source. Full decision tree in
`references/clocking.md`.

## Step 4: sample rate and ADAT channel counts

ADAT carries fewer channels as the rate rises. This is the protocol, not a fault:

| Sample rate | ADAT channels per optical port | Mechanism |
|---|---|---|
| 44.1 / 48 kHz | 8 | normal |
| 88.2 / 96 kHz | 4 | S/MUX |
| 176.4 / 192 kHz | 2 | S/MUX4 |

So channels 5 to 8 of an expander going dead after a switch to 96 kHz is expected behaviour.

**Many budget expanders top out at 48 kHz.** The Behringer ADA8200 is one of them. Confirm the
ceiling from the unit's own panel or manual before promising a higher rate, and never infer it
from the Apollo side. Both boxes must be at the same rate to the digit.

To change the rate:

```
uad_set_sample_rate(rate=48000, confirm=true)
```

It refuses while audio is flowing, and every running stream will glitch when it succeeds. Warn
first.

## Step 5: more than one Apollo

Thunderbolt Apollos cascade: up to four units appear inside **one** Console mixer, so the whole
rig is a single mixer with more strips, not several mixers.

- **Clocking between cascaded Apollos is handled over Thunderbolt.** No word clock cables between
  Apollos are needed.
- **The monitors connect to one designated monitor unit.** Ask which. The monitor output is the
  one with a `CRMonitorLevel` property, and its index differs per model, so find it via
  `uad_list_channels(kind="output")` and its `is_monitor` flag rather than hardcoding.
- **Expanders can feed any unit** over that unit's own ADAT IN.
- **Every tool takes a device selector** once more than one unit exists: `device=0`, `device=1`,
  and so on, matching `devices[].device` in `uad_get_status`. Channel name lookups that are
  ambiguous across units come back as an ambiguity error listing the candidates. Pass `device`
  to resolve it.

Details and a worked cascade layout: `references/cascades.md`.

## Step 6: configure through the MCP server

Only now write anything. Literal sequence:

```
uad_list_channels(kind="input")        # reality check: names, io_type, has_preamp, hidden
```

Rename each channel after the real source, unhide what is in use, hide what is not, and set a
sane starting state:

```
uad_set_channels(items=[
  {"target": 8,  "name": "UG 1",   "hidden": false, "enabled": true, "fader_db": 0, "mute": false},
  {"target": 1,  "name": "LEAD VOX","hidden": false, "fader_db": 0, "mute": false},
  {"target": 3,  "name": "SP-404 L","hidden": false},
  {"target": 20, "hidden": true}
])
```

Item keys accepted per item: `target` (name or index), `device`, `kind`, `name`, `fader_db`,
`mute`, `solo`, `solo_safe`, `pan`, `pan2`, `hidden`, `stereo`, `enabled`. Every item reports the
resolved `{device, index, name}` plus per-property `{verified, before, after, warnings}`. Check
`verified` rather than assuming the write landed.

If the plan needs a clock change, do it here with `uad_set_clock(source=..., confirm=true)`,
which verifies `ClockLocked` after about 2.5 s and reverts itself if the new source does not lock.

Set preamp basics for mic and instrument channels:

```
uad_set_preamp(target="LEAD VOX", source="Mic", gain_db=20, low_cut=true, pad=false, phase=false)
uad_set_preamp(target="BASS DI", hi_z=true)
```

Verify signal on everything before declaring victory:

```
uad_peek_meters(kind="input")          # at_floor channels are called out for you
```

Finish with:

```
uad_snapshot(action="save", name="studio-baseline")
```

Then tell the user: **press Cmd+S in Console to save the Console session.** Saving a session is
not part of this protocol, so a snapshot is not a substitute for it.

## Worked example: Apollo x8 Gen 2 plus Behringer ADA8200

A real session, from symptom to fix. Read it whenever an ADAT expander shows no signal:
`references/worked-example-ada8200.md`. Short version: eight ADAT channels sat at the meter
floor, `ClockSource` was `Internal`, and only one optical cable was connected, so the expander
never received the Apollo's clock. Adding the return cable from the Apollo's ADAT OUT to the
ADA8200's ADAT IN and flipping the ADA8200's rear SYNC switch to slave from ADAT IN brought the
channels to life at -17.8 dB. The eight channels were then renamed UG 1 to UG 8.

## Safety notes to carry through onboarding

- **Monitor level:** never raise it to check something. If a level call is refused, read the
  stored level back and relay the remedy from the refusal message.
- **48V:** ask what the microphone is, then pass both `phantom_48v=true` and
  `confirm_condenser_mic=true`. Never set it speculatively.
- **Feedback or anything unexpectedly loud:** call `uad_safe_state()` first, diagnose after. It
  mutes every monitor output and turns talkback off, takes no arguments and needs no confirmation.
- **Headphone output level cannot be set** by this protocol at all. Build a performer's balance
  with cue send levels (`uad_set_sends`) and let them use the physical knob for the final trim.

## References

Open these only when the step calls for them.

- `references/clocking.md`: the clock decision table, verification loop, the full no-signal
  decision tree, and the sample-rate and S/MUX detail.
- `references/cascades.md`: multi-Apollo cascades, device selectors, monitor unit choice.
- `references/worked-example-ada8200.md`: the real x8 Gen 2 plus ADA8200 session, step by step.
- `references/tools.md`: exact tool names and parameter names for the whole server.
