# Cue mixes for performers

## What a cue bus is, and what this server can do with it

A cue bus is a separate mix, built from send levels, that feeds a headphone output. The performer
hears the cue mix, the engineer hears the control room mix, and the two are independent.

What this server controls:

- **Send levels, pans and bypass into each cue bus**, with `uad_set_sends`.
- **Aux sends** (`aux1`, `aux2`) the same way, for effects returns rather than headphones.

What it cannot control:

- **The headphone output level.** The headphone outputs expose no level property at all, only which
  cue bus feeds them (`MixInSource`, set in Console). Build the balance with sends and let the
  performer use the physical knob. Say this explicitly rather than promising a level change.
- **Which physical headphone jack listens to which cue bus.** That is a Console setting.

## How many cue buses exist

Per model. Read it, do not assume:

```
uad_get_status()
```

`devices[].cue_buses` gives the count. Send IDs are `aux1`, `aux2`, `cue1` through `cue4`, or an
index. If the ID is wrong, `uad_set_sends` returns an error listing the available IDs for that
channel, which is the fastest way to discover the real set.

## One performer

Everything they need to hear, in one call. The performer's own signal loudest, everything else
under it:

```
uad_set_sends(items=[
  {"target": "LEAD VOX", "send": "cue1", "level_db": -6},
  {"target": "DAW L",    "send": "cue1", "level_db": -12},
  {"target": "DAW R",    "send": "cue1", "level_db": -12}
])
```

Then ask, do not guess: "more of yourself or more of the track?" and move that one send by 3 dB at a
time.

## Two or more performers who want different balances

Same sources, one bus per performer, different levels on each bus. A singer usually wants themselves
loud; a guitarist usually wants the drums loud.

```
uad_set_sends(items=[
  {"target": "LEAD VOX", "send": "cue1", "level_db": -4},
  {"target": "GUITAR",   "send": "cue1", "level_db": -14},
  {"target": "DRUMS",    "send": "cue1", "level_db": -12},

  {"target": "LEAD VOX", "send": "cue2", "level_db": -14},
  {"target": "GUITAR",   "send": "cue2", "level_db": -4},
  {"target": "DRUMS",    "send": "cue2", "level_db": -8}
])
```

Batch the whole thing in one call so the result comes back as one comparable set. Every item reports
its own `{verified, before, after, warnings}` plus the resolved `{device, index, name}`, so a single
failed send is visible without re-reading everything.

## Pre-fader or post-fader

`PostFaderOverride` exists on each send (`uad_set_sends` does not expose it directly; read it with
`uad_raw_get` on the send node if you need to know). What matters practically:

- A **pre-fader** cue send is unaffected when the engineer moves the channel fader, which is what a
  performer wants: their headphone mix should not change because the engineer is auditioning
  something.
- A **post-fader** aux send follows the fader, which is what an effects send usually wants.

If a performer complains that their headphone mix changes whenever the engineer touches something,
this is the thing to check, in Console.

## Mono questions

Two different mono controls, often confused:

| Control | What it does | What it is for |
|---|---|---|
| `uad_set_monitor(mono=true)` | folds the **control room** output to mono (`MixToMono`) | checking a mix for phase and mono compatibility |
| Send pan set to centre | puts that source in the middle of a **cue** mix | a performer on one headphone, or a mono cue feed |

`mono=true` on the monitor does **not** change what any performer hears. A performer who wants a
centred cue gets it by panning their sends:

```
uad_set_sends(items=[
  {"target": "LEAD VOX", "send": "cue1", "level_db": -6, "pan": 0},
  {"target": "DRUMS",    "send": "cue1", "level_db": -10, "pan": 0}
])
```

Mono-fold the control room when checking a mix, then put it back:

```
uad_set_monitor(mono=true)
uad_set_monitor(mono=false)
```

## Talkback

```
uad_talkback(on=true, level=60)
uad_talkback(on=false)
```

- **A 30 s auto-off timer starts by default.** Calling `uad_talkback` again replaces the timer.
  `uad_get_status` reports `talkback.auto_off_timer_running`.
- Never leave talkback open. An open talkback mic in the control room is both a privacy problem and
  a feedback path.
- Which microphone is the talkback mic, and where it is routed, is set in Console, not here.
- `uad_safe_state()` turns talkback off as part of getting safe.

## Verifying a cue mix without guessing

The server cannot hear the cue bus. Verify the pieces:

1. **The sources are live:** `uad_peek_meters(kind="input", above_db=-60)`.
2. **The sends landed:** every `uad_set_sends` item reports `verified` and the `after` value. Check
   them.
3. **The performer's report is the real test.** Ask for one change at a time and move it 3 dB.

## Snapshot the cue mixes

Cue mixes take real conversation to build and are trivial to destroy. Save them:

```
uad_snapshot(action="save", name="cue-band-tracking")
```

Later, to see what drifted:

```
uad_snapshot(action="diff", name="cue-band-tracking")
```
