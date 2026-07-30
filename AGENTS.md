# AGENTS.md

Instructions for an LLM driving `uad-console-mcp`. Human-facing docs: [README.md](README.md),
[docs/RECIPES.md](docs/RECIPES.md), [docs/PROTOCOL.md](docs/PROTOCOL.md).

## Orientation

1. This server controls one or more Universal Audio Apollo interfaces through the local UA Mixer
   Engine on `127.0.0.1:4710`: channel state, preamps, sends, monitor, talkback, clock, UAD
   plugins, meters, snapshots.
2. Discovery first. Channel counts, indices, monitor output, send IDs and slot counts differ per
   model and are read from the engine, never assumed.
3. Every write is read back. A write result is `{verified, before, after, warnings}` plus the
   resolved `{device, index, name}`.
4. Refusals are information, not obstacles. Each one names the env var or confirm flag that would
   unblock it. Relay it; do not route around it.
5. This is live audio hardware next to human ears. Loud is irreversible.

## HARD RULES

1. **Never hardcode a channel index, monitor index or slot number.** Call `uad_list_channels`
   first and use what it returns. `ANALOG 1` is input 0 on one model and does not exist on another.
2. **Never raise monitor level without an explicit dB figure from the user.** No "a bit louder",
   no probing to check a fix. `uad_set_monitor(level_db=...)` only with a number they said.
3. **Never enable 48V without asking what microphone is connected.** Ask, get an answer, then pass
   `confirm_condenser_mic=true`. **Never on a ribbon mic**: phantom power destroys them
   permanently. If the answer is unknown or vague, leave 48V alone and say why.
4. **Never change clock source or sample rate while meters show signal.** Check with
   `uad_peek_meters` first, tell the user you are about to re-lock the chain, and only then pass
   `confirm=true`. Both tools refuse while any input is above -60 dB.
5. **Writes are verified. Treat `verified: false` as a failure, not as a retry cue.** Read the
   property with `uad_raw_get`, report what the engine actually holds, and stop. Never loop.
6. **Never probe by writing.** The engine accepts writes to properties it marks read-only, so a
   test write is a real change. To learn a property's type, range or enum, use `uad_raw_get`.
7. **On `SafetyRefusal`, relay the remedy to the user.** Do not raise the ceiling, do not switch
   mode, do not retry with a different tool that reaches the same property. The rail is the point.
8. **Call `uad_safe_state` first when anything is unexpectedly loud.** Diagnose afterwards. It
   needs no arguments and no confirmation.
9. **Meter targets are names.** `uad_peek_meters` / `uad_watch_meters` take `targets: list[str]`
   resolved by name. A numeric string is matched as a name first: `"8"` matches `ANALOG 8`,
   `UG 8` and `VIRTUAL 8` and errors as ambiguous. Pass exact names.
10. **A verified rename is usable immediately** (the cached topology is updated in place), but if a
    rename comes back `verified: false` the old name is still the truth. Use the index until a
    fresh `uad_list_channels` proves otherwise.
11. **With more than one device, always pass `device`.** An index that exists on several Apollos is
    ambiguous, by design.
12. **Snapshot before experimenting**, and show a `dry_run=true` restore before running it for real.

## Canonical flows

### Label an ADAT expander

```
uad_get_status()
  -> clock.source, clock.locked, clock.sample_rate. If locked is false, stop: fix clock first.
uad_list_channels(kind="input")
  -> pick the strips with io_type "ADAT" (x8 Gen 2: indices 8-15, named "UG 1".."UG 8")
uad_set_channels(items=[
  {"target": "UG 1", "name": "KICK IN",   "fader_db": 0, "mute": false, "hidden": false},
  {"target": "UG 2", "name": "SNARE TOP", "fader_db": 0, "mute": false, "hidden": false}])
  -> results[i].writes[j] = {"path": "/devices/0/inputs/8/Name", "verified": true,
                             "before": "UG 1", "after": "KICK IN", "warnings": []}
uad_peek_meters(targets=["KICK IN","SNARE TOP"])
  -> every ADAT channel at_floor: true means clock or cable, NOT silence. Go to the clock flow.
uad_snapshot(action="save", name="adat-expander")
```

### Vocal tracking with Unison and a cue send

```
uad_get_status()                       -> confirm mode is not "readonly", note sample rate
uad_list_channels(kind="input")        -> choose a strip with has_preamp: true; ask if unsure
uad_set_channels(items=[{"target": "ANALOG 1", "name": "LEAD VOX", "fader_db": 0,
                         "mute": false, "solo": false, "hidden": false}])
uad_set_preamp(target=0, gain_db=20, low_cut=true, pad=false, phase=false)
  # 48V only after the user names a condenser:
uad_set_preamp(target=0, phantom_48v=true, confirm_condenser_mic=true)
uad_watch_meters(targets=["LEAD VOX"], duration_s=10, threshold_db=-6)
  -> {"triggered": false, "peaks_db": {"LEAD VOX": -18.2}}   aim for peaks -12 to -6 dBFS
uad_set_preamp(target=0, gain_db=26)   # move in <= 6 dB steps, re-measure after each
uad_list_plugins(unison_only=true, authorized_only=true)
uad_load_plugin(target=0, plugin="UAD UA 610-B", slot="unison")   # exact name from the catalog
  -> {"materialized": true, "parameter_count": N}   materialized false = params not addressable yet
uad_set_sends(items=[{"target": 0, "send": "cue1", "level_db": -6}])
uad_snapshot(action="save", name="vocal-tracking")
```

### "No sound from my expander"

```
uad_get_status()
  -> clock.locked false            => the Apollo is chasing a clock it cannot see
  -> clock.locked true             => continue below
uad_watch_meters(kind="input", duration_s=10, threshold_db=-60)
  -> {"triggered": false, "peaks_db": {"UG 1": -76.99, ...}}
     every digital meter at -77 = dead link or no lock. A floor reading cannot prove silence.
uad_raw_get("/ClockSource")   -> {"value": "Internal", "values": ["Internal","Word Clock","ADAT","S/PDIF"]}
```

Then reason it out loud with the user, do not guess in silence:

- Apollo `ClockSource: Internal` means the Apollo is master, so the expander must be slaved.
  With one optical cable (expander OUT into Apollo IN) there is nothing to slave it with. Fix:
  add the return cable (Apollo ADAT OUT into expander ADAT IN) and set the expander to slave, or
  make the expander master with `uad_set_clock(source="ADAT", confirm=true)`.
- Exactly one device may be master. Both internal: nothing locks. Both slaved: nothing locks.
- Sample rates must match exactly. At 88.2 / 96 kHz ADAT runs S/MUX, so 8 channels become 4.
  Channels 5 to 8 going dead at high rates is expected, not a fault.
- Verify after each change: `uad_get_status()` for `clock.locked`, then
  `uad_watch_meters(targets=[...], duration_s=10)` for actual signal.

### Snapshot around an experiment

```
uad_snapshot(action="save", name="before-drum-bus")
  -> {"entries": 2625, "devices": ["Apollo x8 Gen 2"], "sample_rate": 48000, "path": "..."}
...make changes...
uad_snapshot(action="diff", name="before-drum-bus", ignore=["*/effects/*"])
  -> {"changed": 2, "changes": [{"path": "/devices/0/inputs/8/FaderLevel",
        "property": "FaderLevel", "snapshot_value": 0.0, "current_value": -4.5}, ...]}
uad_snapshot(action="restore", name="before-drum-bus")     # dry run: shows the ordered writes
  -> {"dry_run": true, "writes": [{"path": ".../Mute", "value": false, "phase": "channel"}, ...],
      "counts": {"planned": 2, "applied": 0, "skipped": 0, "failed": 0, "unchanged": 2623}}
uad_snapshot(action="restore", name="before-drum-bus", dry_run=false)
  -> {"pre_restore_snapshot": "pre-restore-20260730-043550",
      "counts": {"planned": 2, "applied": 2, "skipped": 0, "failed": 0, "unchanged": 2623}}
```

Restore writes only what differs, in a safe order, and never aborts mid-way: per-entry failures
come back in `failed` with reasons. Clock, sample rate and monitor are excluded unless asked for
(`include_clock`, `include_monitor`).

## Errors: meaning and next action

| Error text starts with | Meaning | Do this |
|---|---|---|
| `UA Mixer Engine not reachable on 127.0.0.1:4710` | The engine process is not running or not listening | Tell the user to launch UAD Console once (the engine keeps running afterwards). Do not retry in a loop |
| `... did not answer; it may be busy loading plugins or scanning presets` | Timeout, engine alive | Wait a moment, retry once, then report |
| device reported `online: false` in `uad_get_status` | Apollo powered off, cable out, or driver asleep | Stop. Reads may still answer from cached engine state; writes are meaningless. Ask the user to check power and Thunderbolt/USB |
| `Refused write ...: safety mode is 'readonly'` | Server is read-only | Report that writes need `UAD_MCP_MODE=safe`. Do not attempt other tools |
| `Refused write ...: X is not in the safe-mode allowlist` | Property outside `safe` mode | Report it, name `UAD_MCP_MODE=full`, and say what the property does. Do not use `uad_raw_set` (unregistered outside full mode) |
| `Refused monitor level ...: above the -6.0 dB ceiling` | Monitor rail ceiling | Offer the ceiling value instead, or tell the user to raise `UAD_MCP_MAX_MONITOR_DB` themselves |
| `Refused monitor increase ...: only +N dB of the +10.0 dB / 30 s budget is left` | Rolling window spent | Wait the stated seconds, or stop. Never split the jump into smaller calls to beat it |
| `Refused to unmute the monitor output: its stored level is ... above the ... ceiling` | Unmute would jump to a loud stored level | Lower `level_db` first, then release the mute |
| `Refused to switch 48V phantom power on ...` | No condenser confirmation | Ask what mic it is. Ribbon means never |
| `Refused write to /ClockSource: changing ClockSource re-locks the whole audio chain` | No `confirm` | Warn the user, then retry with `confirm=true` |
| `Refused write ...: audio is flowing (an input meter is above -60 dB)` | Signal present | Ask for playback and recording to stop, verify with `uad_peek_meters`, then retry |
| `Refused write ...: write rate limit reached` | Token bucket empty | Wait the stated seconds. Batch tools (`uad_set_channels`, `uad_set_sends`) instead of many single calls |
| `'X' matches N input targets: ...` (`AmbiguousTargetError`) | Several channels matched | Pick one candidate from the list by exact name, or pass `device` and/or the index. Never guess |
| `No input matches 'X'. Closest input names: ...` | No match | Call `uad_list_channels` and use a real name. It may also be a just-renamed channel: use its index |
| `parameter nodes did not appear at ... within 5s` | Plugin loaded, parameters not materialized | Wait, call `uad_get_plugin_params` again. Parameter writes were skipped, not lost silently |
| `uad_watch_meters` returns all peaks at about -77 dB | Floor: silent source **or** dead digital link. The two are indistinguishable | On analog, suspect the source or the preamp gain. On ADAT/S-PDIF, suspect clock and cabling first |

## Glossary

| Studio term | Here it is |
|---|---|
| Unison | The preamp-emulation slot on a mic input. `uad_load_plugin(slot="unison")`, only where `has_preamp` is true, only with plugins where `unison: true` |
| Insert | A numbered plugin slot on the strip. `uad_load_plugin(slot=0,1,2,...)`; `uad_get_inserts` lists the real slots |
| Cue | A performer's headphone mix bus (`cue1`..`cue4`, count per model in `cue_buses`). `uad_set_sends(send="cue1", level_db=...)` |
| Aux | An effects-send bus (`aux1`, `aux2`). Same tool, different `send` ID |
| Headphone level | Not settable. `HP n` outputs only choose a cue bus (`MixInSource`). Build the balance with sends; the knob does the rest |
| HiZ | High-impedance instrument input. `uad_set_preamp(hi_z=true)`, where the preamp has it |
| Pad | Input attenuator for hot sources. `uad_set_preamp(pad=true)` |
| Dim | Fixed monitor attenuation. `uad_set_monitor(dim=true)`, depth is `dim_attenuation_db`. Un-dimming is a level event and can be refused |
| Talkback | Control-room mic into the cue buses. `uad_talkback(on=true)`, auto-off after 30 s by default |
| Alt speakers | Second monitor pair. `uad_set_monitor(alt_speaker=N)` |
| ADAT expander | Outboard converter on the optical port. Its strips show `io_type: "ADAT"` |
| Word clock | BNC clock. `uad_set_clock(source="Word Clock", confirm=true)` |
| S/MUX | How ADAT carries high rates: 8 channels at 44.1/48 kHz become 4 at 88.2/96 kHz |
| Snapshot | This server's own state file, not a Console session and not a plugin scene |

## This server cannot

- Set **headphone output level** (no such property). Use cue-bus send levels plus the knob.
- Save or recall a **Console session** or a **plugin scene** (`/pluginscenes` writes are always
  denied). Tell the user to press Cmd+S in Console; use `uad_snapshot` for what this server owns.
- Re-patch **I/O routing** (`/inputroutes`, `/outputroutes` are readable only).
- Touch **Dante**, or any UA API other than TCP 4710. No remote or networked engines.
- Create or delete channels; the strip list is fixed by the model.
- Record, play or analyse audio. Meters poll at about 3 Hz and `uad_watch_meters` caps at 30 s.
- Report **per-plugin DSP cost**; only the totals exist.
- Exceed the monitor rails: ceiling -6 dB by default, at most +6 dB per call, +10 dB per 30 s,
  and no unmute or un-dim onto an above-ceiling stored level.
- Write device identity or engine internals (`DeviceHwID`, `MultiUnitOrder`, `AppCommand`,
  `MixerState`, `DeviceTypeID`, firmware-like properties) in any mode, including `full`.
