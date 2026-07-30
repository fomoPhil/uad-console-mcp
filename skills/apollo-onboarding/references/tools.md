# uad-console-mcp tool reference

Exact tool names and parameter names. Anything not listed here does not exist: do not invent
parameters, and do not rename them.

## Reading

| Tool | Parameters | Returns |
|---|---|---|
| `uad_get_status` | none | `engine{connected,host,port,version,protocol_version,audio_streaming}`, `clock{source,locked,sample_rate,max_delay_comp}`, `dsp{total_dsp_load_percent,program_load_percent,memory_load_percent}`, `talkback{on,level,auto_off_timer_running}`, `devices[]`, `safety` |
| `uad_list_channels` | `kind` (`"input"` default, `"aux"`, `"output"`, `"all"`), `device`, `include_hidden` | `devices[].channels[]` with `device,index,name,io_type,has_preamp,is_monitor,is_talkback,active,fader_db,mute,solo,solo_safe,pan,hidden,stereo,monitor_level_db` |
| `uad_peek_meters` | `targets` (list of names), `kind`, `device`, `above_db` | one-shot level, peak, clip per channel, with at-floor channels called out |
| `uad_watch_meters` | `targets`, `duration_s` (default 5, cap 30), `threshold_db` (default -60), `interval_s` (default ~0.33), `kind`, `device` | per-channel peaks, exits early when any watched channel crosses `threshold_db` |
| `uad_get_inserts` | `target`, `device`, `kind`, `loaded_only` (default true) | every insert slot plus the Unison slot, with plugin name, power, preset |
| `uad_get_plugin_params` | `target`, `slot` (default 0), `device` | parameter names, normalized 0 to 1 values, engine display text |
| `uad_list_plugins` | `search`, `category`, `unison_only`, `authorized_only`, `limit` (default 50) | catalog entries with `name`, categories, Unison capability, authorized |
| `uad_raw_get` | `path` | any node or property verbatim, with metadata (`type`, `min`, `max`, `values`, `readonly`) |

## Writing

| Tool | Parameters |
|---|---|
| `uad_set_channels` | `items`: list of `{target, device?, kind?, name?, fader_db?, mute?, solo?, solo_safe?, pan?, pan2?, hidden?, stereo?, enabled?}` |
| `uad_set_preamp` | `target`, `device?`, `gain_db?`, `pad?`, `low_cut?`, `phase?`, `hi_z?`, `source?` (`"Mic"` or `"Line"`), `phantom_48v?`, `confirm_condenser_mic` (default false) |
| `uad_set_sends` | `items`: list of `{target, send, device?, kind?, level_db?, pan?, bypass?}`; `send` is `"aux1"`, `"aux2"`, `"cue1"`..`"cue4"` or an index |
| `uad_set_monitor` | `device?`, `level_db?`, `dim?`, `mute?`, `mono?`, `alt_speaker?`, `dim_attenuation_db?` |
| `uad_talkback` | `on?`, `level?`, `auto_off_s?` (default 30 s) |
| `uad_safe_state` | none |
| `uad_set_clock` | `source`, `confirm` (must be true) |
| `uad_set_sample_rate` | `rate`, `confirm` (must be true) |
| `uad_load_plugin` | `target`, `plugin`, `slot` (0..3 or `"unison"`), `device?`, `preset?`, `power?` |
| `uad_remove_plugin` | `target`, `slot`, `device?` |
| `uad_set_plugin_params` | `target`, `params`: list of `{name, normalized}`, `slot`, `device?` |
| `uad_plugin_preset` | `target`, `preset`, `slot`, `device?` |
| `uad_snapshot` | `action` (`"save"`, `"list"`, `"diff"`, `"restore"`), `name?`, `dry_run` (default true), `ignore?` (glob list), `include_clock`, `include_monitor`, `allow_cross_model` |
| `uad_raw_set` | `path`, `value`. Registered **only** when `UAD_MCP_MODE=full` |

Every write result reports `{verified, before, after, warnings}` per property plus the resolved
`{device, index, name}`. Check `verified`, do not assume.

## Resources and prompts

Resources: `uad://state` (the `uad_get_status` payload), `uad://channels` (all inputs, auxes and
outputs), `uad://limitations` (what the server cannot do, with the workaround for each).

Prompts: `setup_vocal_tracking`, `onboard_adat_expander`, `troubleshoot_clock`, `panic_reset`.

## Safety behaviour worth knowing

- **Modes** via `UAD_MCP_MODE`: `readonly` refuses every write, `safe` (the default) allows an
  allowlist of properties, `full` bypasses the allowlist and registers `uad_raw_set`. The monitor
  rail, the denylist and the write rate limit apply in every mode.
- **Monitor rail:** ceiling `UAD_MCP_MAX_MONITOR_DB` (default -6 dB), at most +6 dB per call and
  +10 dB per 30 s. Unmute and un-dim count as level events and can be refused if the stored level
  is above the ceiling.
- **48V** needs `confirm_condenser_mic=true`.
- **Clock and sample rate** need `confirm=true` and refuse while audio is flowing (any input above
  -60 dB).
- Refusals name the env var or confirm flag that would unblock them. Relay that text.

## Hard limits (read `uad://limitations` for the full list)

- Headphone output level cannot be set. Use cue send levels plus the physical knob.
- The routing matrix (`/inputroutes`, `/outputroutes`) can be read but not re-patched.
- Channels cannot be created or deleted.
- Console sessions and plugin scenes cannot be saved or recalled. Use `uad_snapshot`, and have the
  user press Cmd+S in Console for the session itself.
- Plugin parameters are normalized 0 to 1 only, addressed by name, with no dB or Hz conversion.
- Plugin parameter nodes exist only after a plugin is loaded in that slot; `uad_load_plugin` polls
  up to 5 s and reports `materialized`.
- Meters are polled at about 3 Hz. The floor is -77.0 (device silence) or -144.0, and a floor
  reading cannot distinguish silence from a dead digital link.
- Per-plugin DSP cost is not exposed, only rig totals.

**`enabled` vs `active`:** `enabled` writes the `EnabledByUser` property and is yours to set. `active` reflects the engine's own readonly `Active` state (stereo-link right sides and unused strips report false); it cannot be written, so never try to "fix" it.
