# AGENTS.md

Instructions for an LLM driving `uad-console-mcp`. Human-facing docs: [README.md](README.md),
[docs/RECIPES.md](docs/RECIPES.md), [docs/PROTOCOL.md](docs/PROTOCOL.md).

## Orientation

1. This server controls Universal Audio Apollo interfaces through the local UA Mixer Engine on
   `127.0.0.1:4710`: channels, preamps, sends, monitor, talkback, clock, plugins, meters, snapshots.
2. Discovery first. Channel counts, indices, monitor output, send IDs and slot counts differ per
   model and are read from the engine, never assumed.
3. Every write is read back. A write result is `{verified, before, after, warnings}` plus the
   resolved `{device, index, name}`.
4. Refusals are information, not obstacles. Each one names the env var or confirm flag that would
   unblock it. Relay it; do not route around it.
5. This is live audio hardware next to human ears. Loud is irreversible.

## HARD RULES

1. **Never hardcode a channel index, monitor index or slot number.** Call `uad_list_channels` first
   and use what it returns. `ANALOG 1` is input 0 on one model and absent on another.
2. **Never raise monitor level without an explicit dB figure from the user.** No "a bit louder",
   no probing to check a fix. `uad_set_monitor(level_db=...)` only with a number they said.
3. **Never enable 48V without asking what microphone is connected.** Ask, get an answer, then pass
   `confirm_condenser_mic=true`. **Never on a ribbon mic**: phantom power destroys them permanently.
   If the answer is unknown or vague, leave 48V alone and say why.
4. **Never change clock source or sample rate while meters show signal.** Check `uad_peek_meters`
   first, warn the user the chain will re-lock, then pass `confirm=true`. Both refuse above -60 dB.
5. **Writes are verified. Treat `verified: false` as a failure, not as a retry cue.** Read the
   property with `uad_raw_get`, report what the engine actually holds, and stop. Never loop.
6. **Never probe by writing.** The engine accepts writes to properties it marks read-only, so a test
   write is a real change. Learn a property's type, range or enum with `uad_raw_get(path=...)`.
7. **On `SafetyRefusal`, relay the remedy to the user.** Do not raise the ceiling, do not switch
   mode, do not retry with a different tool that reaches the same property. The rail is the point.
8. **Call `uad_safe_state` first when anything is unexpectedly loud.** Diagnose afterwards. It
   needs no arguments and no confirmation.
9. **Meter targets are names, and `targets` is optional.** `uad_peek_meters` and `uad_watch_meters`
   both take `targets`, `kind`, `device` (watch also `duration_s`, `threshold_db`, `interval_s`).
   Omit `targets` and they sweep every channel of `kind` (default `"input"`). A numeric string is
   matched as a name first: `"8"` matches `ANALOG 8`, `UG 8`, `VIRTUAL 8` and errors as ambiguous.
10. **A verified rename is usable immediately** (the cached topology is updated in place), but if a
    rename comes back `verified: false` the old name is still the truth. Use the index until a
    fresh `uad_list_channels` proves otherwise.
11. **With more than one device, always pass `device`.** An index that exists on several Apollos is
    ambiguous, by design.
12. **Snapshot before experimenting**, and show a `dry_run=true` restore before running it for real.

## Canonical flows

### Label an ADAT expander

```
uad_get_status()                  -> clock.source, .locked, .sample_rate; locked false = stop, fix it
uad_list_channels(kind="input")   -> strips with io_type "ADAT" (x8 Gen 2: indices 8-15, "UG 1"..)
uad_set_channels(items=[{"target": "UG 1", "name": "KICK IN", "fader_db": 0, "mute": false,
                         "hidden": false}, {"target": "UG 2", "name": "SNARE TOP", ...}])
  -> writes[j] = {"path": ".../8/Name", "verified": true, "before": "UG 1", "after": "KICK IN"}
uad_peek_meters(targets=["KICK IN","SNARE TOP"])   # at_floor on all = clock or cable, NOT silence
uad_snapshot(action="save", name="adat-expander")
```

### Vocal tracking with Unison and a cue send

```
uad_get_status()                       -> confirm mode is not "readonly", note sample rate
uad_list_channels(kind="input")        -> choose a strip with has_preamp: true; ask if unsure
uad_set_channels(items=[{"target": "ANALOG 1", "name": "LEAD VOX", "fader_db": 0, "mute": false}])
uad_set_preamp(target=0, gain_db=20, low_cut=true, pad=false, phase=false)
uad_set_preamp(target=0, phantom_48v=true, confirm_condenser_mic=true)   # only after the user
                                                                        # names a condenser
uad_watch_meters(targets=["LEAD VOX"], duration_s=10, threshold_db=-6)
  -> {"triggered": false, "peaks_db": {"LEAD VOX": -18.2}}   aim for peaks -12 to -6 dBFS
uad_set_preamp(target=0, gain_db=26)   # move in <= 6 dB steps, re-measure after each
uad_list_plugins(unison_only=true, authorized_only=true)
uad_load_plugin(target=0, plugin="UAD UA 610-B", slot="unison")   # exact name from the catalog
  -> {"materialized": true, "parameter_count": N}   materialized false = params not addressable yet
uad_raw_get(path="/devices/0/inputs/0/sends")     # read before you write: no send-read tool exists
  -> {"kind": "node", "children": ["0".."5"]}     # one node per send; its ID names the bus
uad_raw_get(path="/devices/0/inputs/0/sends/2")   # IDs are aux1, aux2, cue1..cue4
  -> {"properties": {"ID": {"value": "cue1"}, "Gain": {"value": -144.0, "min": -144.0,
       "max": 12.0}, "Pan": ..., "Bypass": ...}}   # Gain -144..+12 dB; -144.0 = send off
uad_set_sends(items=[{"target": 0, "send": "cue1", "level_db": -6}])   # cue3/cue4 nodes exist even
  # where cue_buses is 2: such a write verifies but warns it reaches no real output. Relay it.
uad_snapshot(action="save", name="vocal-tracking")
```

### "No sound from my expander"

```
uad_get_status()
  -> engine.audio_streaming false  => the engine is passing no audio at all. Check this first:
                                      no meter anywhere will move until it is true
  -> clock.locked false            => the Apollo is chasing a clock it cannot see
  -> clock.locked true             => continue below
uad_watch_meters(kind="input", duration_s=10, threshold_db=-60)   # no targets: sweeps every input
  -> {"triggered": false, "peaks_db": {"UG 1": -76.99, ...}}
     every digital meter at -77 = dead link or no lock. A floor reading cannot prove silence.
uad_raw_get(path="/ClockSource")
  -> {"value": "Internal", "values": ["Internal","Word Clock","ADAT","S/PDIF"]}
```

Then reason it out loud with the user, do not guess in silence:

- Apollo `ClockSource: Internal` means the Apollo is master, so the expander must be slaved.
  With one optical cable (expander OUT into Apollo IN) there is nothing to slave it with. Fix:
  add the return cable (Apollo ADAT OUT into expander ADAT IN) and set the expander to slave, or
  make the expander master with `uad_set_clock(source="ADAT", confirm=true)`.
- **S/PDIF has no return-cable trick.** One S/PDIF cable, coax or optical, carries clock one way
  only, so the Apollo cannot slave the source the way a second ADAT cable can. Three fixes: let the
  source clock the Apollo (`uad_set_clock(source="S/PDIF", confirm=true)`, rates matched exactly),
  slave the source to the Apollo if it has a sync input, or turn on sample-rate conversion for that
  input (`SRConvert`). Meters at the floor with `ClockSource: Internal` and `SRConvert: false` is
  the classic signature.
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
        "snapshot_value": 0.0, "current_value": -4.5}, ...]}
uad_snapshot(action="restore", name="before-drum-bus")     # dry run: shows the ordered writes
  -> {"dry_run": true, "writes": [{"path": ".../Mute", "value": false, "phase": "channel"}, ...],
      "counts": {"planned": 2, "applied": 0, "failed": 0, "unchanged": 2623}}
uad_snapshot(action="restore", name="before-drum-bus", dry_run=false)
  -> {"pre_restore_snapshot": "pre-restore-20260730-043550", "counts": {"applied": 2, "failed": 0}}
```

Restore writes only what differs, in a safe order, and never aborts mid-way: per-entry failures come
back in `failed` with reasons. Clock, rate and monitor need `include_clock` / `include_monitor`.

## Errors: meaning and next action

| Error text starts with | Meaning | Do this |
|---|---|---|
| `UA Mixer Engine not reachable on 127.0.0.1:4710` | The engine process is not running or not listening | Tell the user to launch UAD Console once (the engine keeps running afterwards). Do not retry in a loop |
| `... did not answer; it may be busy loading plugins or scanning presets` | Timeout, engine alive | Wait a moment, retry once, then report |
| device reported `online: false` in `uad_get_status` | Apollo powered off, cable out, or driver asleep | Stop. Reads may still answer from cached engine state; writes are meaningless. Ask the user to check power and Thunderbolt/USB |
| `Refused write ...: safety mode is 'readonly'` | Server is read-only | Report that writes need `UAD_MCP_MODE=safe`. Do not attempt other tools |
| `Refused write ...: X is not in the safe-mode allowlist` | Property outside `safe` mode | Report it, name `UAD_MCP_MODE=full`, and say what the property does. Do not use `uad_raw_set` (unregistered outside full mode) |
| `Refused monitor level ...: above the -6.0 dB ceiling` | Monitor rail ceiling. The ceiling is checked **first**, and a request above it is refused outright: nothing is written, not even a partial step | Offer the ceiling value instead, or tell the user to raise `UAD_MCP_MAX_MONITOR_DB` themselves. The +6 dB per-call clamp only applies to requests at or below the ceiling |
| `Refused monitor increase ...: only +N dB of the +10.0 dB / 30 s budget is left` | Rolling window spent | Wait the stated seconds, or stop. Never split the jump into smaller calls to beat it |
| `Refused to unmute the monitor output: its stored level is ... above the ... ceiling` | Unmute would jump to a loud stored level | Lower `level_db` first, then release the mute |
| `Refused to switch 48V phantom power on ...` | No condenser confirmation | Ask what mic it is. Ribbon means never |
| `Refused write to /ClockSource: changing ClockSource re-locks the whole audio chain` | No `confirm` | Warn the user, then retry with `confirm=true` |
| `Refused write ...: audio is flowing (an input meter is above -60 dB)` | Signal present | Ask for playback and recording to stop, verify with `uad_peek_meters`, then retry |
| `Refused write ...: write rate limit reached` | Token bucket empty | Wait the stated seconds. Batch tools (`uad_set_channels`, `uad_set_sends`) instead of many single calls. Mutes and talkback-off are never limited, so `uad_safe_state` still works |
| `Could not read the input meters of N channel(s) ...` | The clock or rate gate could not prove silence | Run `uad_peek_meters`, report which meters do not answer, and stop. Nothing was changed |
| `unknown key(s) 'X'; nothing was written for this item` | A batch item had a key the tool does not know | Fix the key from the allowed list in the message and resend that item only |
| `The engine reports X after writing /ClockSource` (or `/SampleRate`) | The engine kept or changed the value | A failure, not a retry cue. Read it with `uad_raw_get` and report it |
| `'X' matches N input targets: ...` (`AmbiguousTargetError`) | Several channels matched | Pick one candidate from the list by exact name, or pass `device` and/or the index. Never guess |
| `No input matches 'X'. Closest input names: ...` | No match | Call `uad_list_channels` and use a real name. It may also be a just-renamed channel: use its index |
| `parameter nodes did not appear at ... within 5s` | Plugin loaded, parameters not materialized | Wait, call `uad_get_plugin_params` again. Parameter writes were skipped, not lost silently |
| `uad_watch_meters` returns all peaks at about -77 dB | Floor: silent source **or** dead digital link. The two are indistinguishable | On analog, suspect the source or the preamp gain. On ADAT/S-PDIF, suspect clock and cabling first |
| `active: false` on a stereo right leg | Normal, not a fault. `active` is read-only engine state | Nothing to fix and nothing to write. It is also normal on unused strips and on some outputs (the x8 Gen 2 `MONITOR` output reports `active: false` while working). The writable one is `enabled` (`EnabledByUser`) |
| a send write returns `verified: true` plus a `cue3`/`cue4` warning | The node exists, but the device has fewer cue buses, so the send reaches no real output | Relay the warning verbatim and rebuild the mix on `cue1`..`cue{cue_buses}` from `uad_get_status` |
| `engine.audio_streaming: false` in `uad_get_status` | The engine is passing no audio at all | Check this before any meter reading or channel hunt: nothing meters until it is true. Usually no DAW or player has the device open, or the driver is asleep |

## Glossary

| Studio term | Here it is |
|---|---|
| Unison | The preamp-emulation slot on a mic input. `uad_load_plugin(slot="unison")`, only where `has_preamp` is true, only with plugins where `unison: true` |
| Insert | A numbered plugin slot on the strip. `uad_load_plugin(target=..., plugin=..., slot=0)`, `slot=1` and so on; `uad_get_inserts(target=...)` lists the real slots |
| Cue | A performer's headphone mix bus (`cue1`..`cue4`, count per model in `cue_buses`). `uad_set_sends(items=[{"target": ..., "send": "cue1", "level_db": ...}])`. All four nodes exist even when `cue_buses` is 2, and a write above the count is warned about, not refused |
| Aux | An effects-send bus (`aux1`, `aux2`). Same tool, different `send` value in the item |
| Send level | `Gain` on the send node, range -144 to +12 dB, where -144.0 means the send is off. Read it with `uad_raw_get(path=".../sends/N")`; write it as `level_db` in `uad_set_sends` |
| `active` vs `enabled` | `active` is read-only engine state from `uad_list_channels`: `false` is normal on the right leg of every stereo pair, on unused strips and on some outputs, never a fault and never writable. `enabled` (`EnabledByUser`) is the writable one: `uad_set_channels(items=[{"target": N, "enabled": true}])`, read back with `uad_raw_get` |
| Monitor level | `CRMonitorLevel` on the monitor output. Read it as `monitor_level_db` from `uad_list_channels(kind="output")` on the strip with `is_monitor: true`; `uad_get_status` does not carry it. Write it with `uad_set_monitor(level_db=...)` |
| SRConvert | Per-digital-input sample-rate conversion, a bool on each S/PDIF strip. Read with `uad_raw_get`; no dedicated tool writes it, so writing it needs `uad_raw_set` in `full` mode. It is on the safe-mode allowlist, which is what lets a snapshot capture and restore it |
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
- Re-patch **I/O routing** (`/inputroutes`, `/outputroutes` are readable only), or create or delete
  channels; the strip list is fixed by the model.
- Touch **Dante**, or any UA API other than TCP 4710. No remote or networked engines.
- Record, play or analyse audio (meters poll at about 3 Hz, `uad_watch_meters` caps at 30 s), or
  report **per-plugin DSP cost**; only the totals exist.
- Exceed the monitor rails: ceiling -6 dB by default, at most +6 dB per call, +10 dB per 30 s,
  and no unmute or un-dim onto an above-ceiling stored level. Climbing back up from a very low
  level is therefore deliberately slow: for a big jump, tell the user to use the monitor knob.
- Write device identity or engine internals (`DeviceHwID`, `MultiUnitOrder`, `AppCommand`,
  `MixerState`, `DeviceTypeID`, firmware-like properties) in any mode, including `full`.
