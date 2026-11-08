# uad-console-mcp tool reference

Exact tool names and parameter names. Anything not listed here does not exist: do not invent
parameters, and do not rename them.

## Reading

| Tool | Parameters | Returns |
|---|---|---|
| `uad_get_status` | none | `engine{connected,host,port,version,protocol_version,audio_streaming}`, `clock{source,locked,sample_rate,max_delay_comp}`, `dsp{total_dsp_load_percent,program_load_percent,memory_load_percent}`, `talkback{on,level,auto_off_timer_running}`, `devices[]`, `safety`, `rig_profile{saved,sources?,updated?,hint}` |
| `uad_rig_profile` | `action` (`"get"` default, `"save"`), `sources?` (list of `{target, device?, plugged_in?, mic_type?, role?, notes?}`; replaces the whole list), `notes?` | `saved`, `path`, `sources[]` (`device,index,name`, plus `current_name` after a rename, `missing` if gone), `notes`. `mic_type`: `dynamic`, `condenser`, `ribbon`, `line`, `unknown`. `role`: `voice`, `instrument`, `system_audio`, `call_guest`, `other`. Saving writes a local file only |
| `uad_get_routing` | none | `driver_inputs[]` / `driver_outputs[]` as `{channel, name, io_type, index, active}` (channel = the number apps show), `loopback{monitor_mix,aux1,aux2}` each `{channels, names, active}`, `inputs_1_2`, `virtual_outputs[]`, `io_matrix_preset`, `io_matrix_presets`, `notes[]` |
| `uad_get_mix` | `bus` (`"aux1"`, `"aux2"`, `"cue1"`..`"cue4"`), `device` | `sources[]` `{name,index,kind,level_db,pan?,strip_muted?}`, `off[]`; a cue adds `heard_on[]` (HP outputs playing it); an aux adds `return{name,fader_db,mute}`, `sends_post_fader`, `plugins[]`, `recorded_on{channels,names,active}` |
| `uad_list_channels` | `kind` (`"input"` default, `"aux"`, `"output"`, `"all"`), `device`, `include_hidden` | `devices[].channels[]` with `device,index,name,kind,path,io_type,has_preamp,is_monitor,is_talkback,active,fader_db,mute,solo,solo_safe,pan,hidden,stereo,monitor_level_db`. `monitor_level_db` on the `is_monitor` strip is the only read path for monitor level: `uad_get_status` does not carry it |
| `uad_peek_meters` | `targets` (list of names, **optional**: omit it and every channel of `kind` is swept), `kind` (default `"input"`), `device`, `above_db` | one-shot level, peak, clip per channel, with at-floor channels called out |
| `uad_watch_meters` | `targets` (**optional**, same sweep rule as above), `duration_s` (default 5, cap 30), `threshold_db` (default -60), `interval_s` (default ~0.33), `kind` (default `"input"`), `device` | per-channel peaks, exits early when any watched channel crosses `threshold_db` |
| `uad_get_inserts` | `target`, `device`, `kind`, `loaded_only` (default true) | every insert slot plus the Unison slot, with plugin name, power, preset |
| `uad_get_plugin_params` | `target`, `slot` (default 0), `device` | parameter names, normalized 0 to 1 values, engine display text |
| `uad_list_plugins` | `search`, `category`, `unison_only`, `authorized_only`, `limit` (default 50) | catalog entries with `name`, categories, Unison capability, authorized |
| `uad_raw_get` | `path` | any node or property verbatim, with metadata (`type`, `min`, `max`, `values`, `readonly`) |

## Writing

| Tool | Parameters |
|---|---|
| `uad_set_channels` | `items`: list of `{target, device?, kind?, name?, fader_db?, mute?, solo?, solo_safe?, pan?, pan2?, hidden?, stereo?, enabled?}` |
| `uad_set_preamp` | `target`, `device?`, `gain_db?`, `pad?`, `low_cut?`, `phase?`, `hi_z?`, `source?` (`"Mic"` or `"Line"`), `phantom_48v?`, `confirm_condenser_mic` (default false) |
| `uad_set_sends` | `items`: list of `{target, send, device?, kind?, level_db?, pan?, bypass?}`; `send` is `"aux1"`, `"aux2"`, `"cue1"`..`"cue4"` or an index. `level_db` is -144 to +12 dB, where -144.0 means the send is off. Read the current value with `uad_raw_get(path=".../sends/N")`; a `cueN` above the device's `cue_buses` count verifies but returns a phantom-bus warning |
| `uad_set_monitor` | `device?`, `level_db?`, `dim?`, `mute?`, `mono?`, `alt_speaker?`, `dim_attenuation_db?` |
| `uad_talkback` | `on?`, `level?`, `auto_off_s?` (default 30 s, must be above 0) |
| `uad_safe_state` | none |
| `uad_set_clock` | `source`, `confirm` (must be true) |
| `uad_set_sample_rate` | `rate`, `confirm` (must be true) |
| `uad_load_plugin` | `target`, `plugin`, `slot` (0..3 or `"unison"`), `device?`, `preset?`, `power?` |
| `uad_remove_plugin` | `target`, `slot`, `device?` |
| `uad_set_plugin_params` | `target`, `params`: list of `{name, normalized}`, `slot`, `device?` |
| `uad_plugin_preset` | `target`, `preset`, `slot`, `device?` |
| `uad_snapshot` | `action` (`"save"`, `"list"`, `"diff"`, `"restore"`), `name?`, `dry_run` (default true), `ignore?` (glob list), `include_clock`, `include_monitor`, `allow_cross_model` |
| `uad_mode` | `action` (`"plan"` default, `"apply"`, `"list"`), `mode` (`"livestream"`, `"tracking"`, `"mixing"`), `inputs?`, `system_audio?` (`"none"` leaves it out), `guest?`, `bus?` (`"aux1"`/`"aux2"` for a guest, `"cue1"`.. for tracking), `device?`. Returns `summary`, `channels[]` and `sends[]` (the exact items apply writes), `warnings[]`, `you_do[]`, `check[]`, `notes[]`, `questions[]` (non-empty blocks apply), `writes`. Apply adds `snapshot` (the pre-mode snapshot name), `results`, `revert` |
| `uad_raw_set` | `path`, `value`. Registered **only** when `UAD_MCP_MODE=full` |

Every write result reports `{verified, before, after, warnings}` per property plus the resolved
`{device, index, name}`. Check `verified`, do not assume.

## Resources and prompts

Resources: `uad://state` (the `uad_get_status` payload), `uad://channels` (all inputs, auxes and
outputs), `uad://limitations` (what the server cannot do, with the workaround for each),
`uad://rig-profile` (the saved profile).

Prompts: `setup_vocal_tracking`, `onboard_adat_expander`, `troubleshoot_clock`, `panic_reset`,
`setup_livestream`, `rig_profile_interview`.

## Safety behaviour worth knowing

- **Modes** via `UAD_MCP_MODE`: `readonly` refuses every write, `safe` (the default) allows an
  allowlist of properties, `full` bypasses the allowlist and registers `uad_raw_set`. The monitor
  rail, the denylist and the write rate limit apply in every mode.
- **Monitor rail:** ceiling `UAD_MCP_MAX_MONITOR_DB` (default -6 dB), at most +6 dB per call and
  +10 dB per 30 s. A request **above** the ceiling is refused outright, with nothing written; the
  +6 dB step clamp only applies to requests at or below it. Unmute and un-dim count as level events
  and can be refused if the stored level is above the ceiling. Climbing back up from a very low level
  is therefore slow: for a big jump, tell the user to use the monitor knob.
- **48V** needs `confirm_condenser_mic=true`, and is refused outright on an input the rig profile
  marks `ribbon` (or when the profile file cannot be read).
- **Modes** never touch the monitor, 48V, gain, clock, sample rate or I/O Matrix, never move a
  fader or send that is already up, and save a `pre-mode-*` snapshot before writing.
- **Clock and sample rate** need `confirm=true` and refuse while audio is flowing (any input above
  -60 dB).
- Refusals name the env var or confirm flag that would unblock them. Relay that text.

## Hard limits (read `uad://limitations` for the full list)

- Headphone output level cannot be set. Use cue send levels plus the physical knob.
- The routing matrix can be read (`uad_get_routing`) but not re-patched, and which app plays to
  which channel is set in macOS. Hand those steps to the user.
- Channels cannot be created or deleted.
- Console sessions and plugin scenes cannot be saved or recalled. Use `uad_snapshot`, and have the
  user press Cmd+S in Console for the session itself.
- Plugin parameters are normalized 0 to 1 only, addressed by name, with no dB or Hz conversion.
- Plugin parameter nodes exist only after a plugin is loaded in that slot; `uad_load_plugin` polls
  up to 5 s and reports `materialized`.
- Meters are polled at about 3 Hz. The floor is -77.0 (device silence) or -144.0, and a floor
  reading cannot distinguish silence from a dead digital link.
- Per-plugin DSP cost is not exposed, only rig totals.
- `SRConvert` (per-digital-input sample-rate conversion) is readable with `uad_raw_get` and is on the
  safe-mode allowlist so snapshots restore it, but no tool writes it: that needs `uad_raw_set` in
  `full` mode, or Console.

**`enabled` vs `active`:** `enabled` writes the `EnabledByUser` property and is yours to set. `active` reflects the engine's own readonly `Active` state (stereo-link right sides and unused strips report false); it cannot be written, so never try to "fix" it.
