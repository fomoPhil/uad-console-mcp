# Workflow audit, November 2026

The first audit (`docs/audit-2026-11.md`) checked safety and correctness. This one asked a
different question: can an agent run the jobs people actually bring to an Apollo, on that
person's own rig, without guessing? Read against `audit-improve` at `f118f91`, the fixture, and
read-only calls to a live Apollo x8 Gen 2 (UAD 11.9.0). Nothing was written to the hardware.

## Gaps found

1. **Livestreaming had no path.** Mic plus computer audio into a stream is the most common
   non-music job, and the server had no idea how. The engine knows all of it: the driver exposes
   `MON L/R` (the Console monitor mix), `AUX1 L/R` and `AUX2 L/R` as recordable inputs, `Virtual`
   outputs that feed Console strips, an `Active` flag per route, and an `IOMapPreset` enum that
   includes a `Livestreaming` I/O Matrix preset. None of it was surfaced; `uad://limitations` only
   said the routing could not be changed.
2. **No memory of the rig.** Every session started cold. The agent could not know which input is
   the user's voice, where computer audio plays, or that input 2 is a ribbon. The 48V rail relied
   on one `confirm_condenser_mic=true` from a model that might be guessing.
3. **Jobs were long hand-built call sequences.** Setting up a stream, a take or a mixdown meant
   5 to 20 calls the agent had to get right each time, with nothing undoing them as a group.
4. **No way to read a bus.** "What is in my headphones?" and "what does the call hear?" needed
   one `uad_raw_get` per send per strip. AGENTS.md said so outright.
5. **Mix-minus was prose only.** The podcast reference said a remote guest must not hear
   themselves, then left the whole thing to the user.
6. **Small doc drift.** The headphone workaround said to "pick the cue bus with `MixInSource`",
   which no tool can write.

## What changed

| Gap | Change |
|---|---|
| 1 | `uad_get_routing` reads the driver I/O map with real channel numbers and loopback inputs. `uad_mode(mode="livestream")` turns it into exact steps: which channels to pick in Audio MIDI Setup, which inputs the stream app records, and when the I/O Matrix has to move |
| 2 | `uad_rig_profile` saves what the user says about their gear, keyed by device and index. A `ribbon` in the profile blocks 48V regardless of confirmation, and an unreadable profile blocks it too. `uad_get_status` and the server instructions point new sessions at the profile |
| 3 | `uad_mode` with `livestream`, `tracking`, `mixing`: plan by default, apply on a yes, a `pre-mode-*` snapshot before every apply. Writes go through the existing tools, so the gate, rate limit and read-back are unchanged |
| 4 | `uad_get_mix(bus=...)` lists every source in a cue or aux bus, where a cue is heard and how an aux is returned and recorded |
| 5 | `livestream` with `guest=` builds the mix-minus on an aux bus and refuses when the guest has no strip of its own |
| 6 | Fixed in `uad://limitations` and the README |

Docs: AGENTS.md (rules 13-15, four flows, seven error rows, six glossary entries), RECIPES.md
recipes 5-8, README "Set it up for a job", SPEC §4.8-4.10, both skills, two new prompts.

## Decided against

- **Writing the I/O Matrix or `IOMapPreset`.** It renumbers every app's channels, the DAW's
  included, and has never been tested as a write. It stays read-only, and the steps tell the user
  where to change it.
- **Writing `MixInSource`** (which cue an HP output plays). Switching a headphone feed can jump the
  level in someone's ears, and headphone outputs have no level rail. Still a Console step.
- **Modes that touch the monitor, 48V or gain.** Modes warn instead. Lowering the monitor for a
  stream is a one-line `uad_set_monitor` the user should say yes to.
- **More modes** (podcast, practice, late-night). Each is `livestream` or a single call with
  different arguments. Easy to add once a real need shows up.

## Not verified without hardware writes

- Whether `AUX n L/R` capture is taken before or after the aux return mute. The guest mix-minus
  mutes the return and its check step says what to do if the guest then hears nothing.
- Whether `MON L/R` capture follows the monitor level or mute.
- What the `Livestreaming` I/O Matrix preset moves, exactly. Docs only say MON L/R can be moved to
  inputs 1-2 there, and `uad_get_routing` confirms it after the user does it.
- Route order equals the channel numbers apps show. It matches every capture so far, and the
  steps say to confirm in the app.
