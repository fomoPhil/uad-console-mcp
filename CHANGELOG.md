# Changelog

All notable changes to this project are documented here. This project follows
[semantic versioning](https://semver.org/).

## 0.1.0 (unreleased)

First public MCP server for UAD Console / the UA Mixer Engine.

### Added

- **Tools: 21 in `readonly` and `safe` mode, 22 in `full` mode.** Status and channel listing
  (`uad_get_status`, `uad_list_channels`), channel state (`uad_set_channels`), preamps
  (`uad_set_preamp`), aux and cue sends (`uad_set_sends`), control room (`uad_set_monitor`,
  `uad_talkback`, `uad_safe_state`), transport (`uad_set_clock`, `uad_set_sample_rate`), UAD
  plugins (`uad_list_plugins`, `uad_get_inserts`, `uad_load_plugin`, `uad_remove_plugin`,
  `uad_get_plugin_params`, `uad_set_plugin_params`, `uad_plugin_preset`), metering
  (`uad_peek_meters`, `uad_watch_meters`), whole-rig state (`uad_snapshot`), and a read-only
  escape hatch (`uad_raw_get`). `uad_raw_set` is registered only when `UAD_MCP_MODE=full`.
- **Every write is verified by read-back**, reported per property as
  `{verified, before, after, warnings}` alongside the resolved `{device, index, name}`. Batch
  setters report per-item results and never abort the batch on one failure.
- **Three-tier safety model** (`readonly` / `safe` / `full`), with a 35-property allowlist in
  `safe` mode and a permanent denylist (device identity, engine internals, `/pluginscenes`,
  firmware-like properties) that applies in every mode, including `full`. The engine does not
  enforce its own read-only flags, so this gate is the only real write barrier.
- **Hearing and speaker rails on monitor level**: a hard ceiling (`UAD_MCP_MAX_MONITOR_DB`,
  default -6 dB), a per-call step limit (+6 dB), and a rolling window budget (+10 dB per 30 s).
  Unmuting and un-dimming are treated as level events and can be refused. The rails fail closed:
  if the monitor level cannot be read, the release is refused rather than allowed, on every path
  including `uad_raw_set` and snapshot restore. Lowering `DimAttenuation` while dim is engaged is
  railed as the level event it is. Snapshot restore never switches talkback ON (it restores OFF
  only), so a restored session cannot silently open a mic without its auto-off timer. Every
  refusal names the environment variable or confirm flag that would unblock it.
- **Confirm gates**: 48V phantom power requires `confirm_condenser_mic=true` because phantom power
  destroys ribbon microphones; clock source and sample rate require `confirm=true` plus a
  meter check that no input is above -60 dB. `uad_set_clock` verifies `ClockLocked` and reverts
  itself if the new source does not lock.
- **Global write rate limit** (token bucket, 10 writes/s, burst 20) and a talkback auto-off timer
  (default 30 s) so an open talkback mic cannot be forgotten.
- **Dynamic discovery**: every device in a cascade is enumerated, the control-room monitor is found
  by the presence of its `CRMonitorLevel` property, and channels resolve by name (exact, then
  prefix, then substring) or index. No channel index, monitor index or slot count is hardcoded
  anywhere. Topology is cached with a 30 s TTL and invalidated on reconnect; live values are never
  cached.
- **Resources**: `uad://state` (live status), `uad://channels` (live channel list),
  `uad://limitations` (the cannot-do list with a workaround for each item).
- **Prompts**: `setup_vocal_tracking`, `onboard_adat_expander`, `troubleshoot_clock`,
  `panic_reset`. Prose versions in `docs/RECIPES.md`.
- **Snapshots**: `uad_snapshot` saves, lists, diffs and restores whole-rig state to
  `~/.uad-console-mcp/snapshots` (override with `UAD_MCP_SNAPSHOT_DIR`). Restore defaults to
  `dry_run=true` and returns the exact ordered write list; a real restore captures a
  `pre-restore-*` snapshot first, refuses cross-model restores unless asked, and leaves clock,
  sample rate and the monitor rail out unless opted in.
- **Resilient transport**: two sockets (control plus poll), `\0`-framed UTF-8 JSON, dispatch by
  path since the protocol has no correlation IDs, and supervised readers that fail pending waiters
  and reconnect with exponential backoff, re-running discovery afterwards.
- **CLI**: `--check` (read-only doctor: engine reachability, devices and online state, clock,
  sample rate, plugin count, effective safety limits), `--dump-tree [FILE]` (fixture-format tree
  walk with the home directory scrubbed from string values), `--host` / `--port`, `--log-level`,
  `--version`. Logging always goes to stderr, because stdout belongs to the MCP protocol.
- **Companion Claude skills** in `skills/`: `apollo-onboarding` (plan and configure a rig, clock
  master decisions, cascades, naming) and `apollo-studio-tasks` (tracking setup, gain staging, cue
  mixes, podcast and streaming layouts, troubleshooting trees), each with a `references/` folder.
- **Documentation**: `README.md`, `AGENTS.md` (rules and canonical flows for LLM callers),
  `docs/PROTOCOL.md` (wire protocol cheatsheet), `docs/RECIPES.md`, and `docs/SPEC.md` (module
  contracts).
- **543 tests** against `tests/mock_engine.py`, covering framing edge cases (split frames,
  batched frames, meter noise, abrupt disconnects, unanswered paths), reply interleaving,
  serialized writes under concurrency, reconnect and rediscovery, `readonly` mode reaching the
  wire zero times, the monitor rail as a property test, snapshot round-trips on a cascade fixture,
  cross-model restore refusal, and a randomized soak. Plus a 7-test `live` smoke suite that has
  been run against a real Apollo x8 Gen 2; it needs real hardware and is excluded by default.

### Fixed (November 2026 audit, see `docs/audit-2026-11.md`)

- Talkback that this server opened is switched off when the MCP session ends. Before, the auto-off
  timer died with the event loop and the mic stayed open.
- `uad_safe_state` is never rate-limited: mutes and talkback-off skip the token bucket.
- Monitor writes are serialized, so parallel calls can no longer beat the +10 dB / 30 s budget.
- Clock and sample-rate changes are refused when any input meter cannot be read.
- `full` mode: `CRMonitorLevelTapered` and `SurroundTrimLevel` are refused, paths are
  canonicalized (`//`, trailing `/value`, `..`), denylist matching ignores case, and
  `uad_raw_set` on `/TalkbackOn` arms the auto-off timer.
- `uad_watch_meters` labels same-named cascade channels with their device, and a numeric `device`
  string is read as an index.
- Failed tools return `isError: true`; read-only tools carry `readOnlyHint`; `kind` and `action`
  are enums in the schema.
- Batch items with unknown keys fail instead of being half-applied; an unverified clock or
  sample-rate write is `ok: false`; `auto_off_s` must be above 0.
- A malformed engine frame is dropped instead of the whole socket. `mcp` is pinned below 3.

### Verified scope

- Live-tested against an Apollo x8 Gen 2 (Thunderbolt, macOS, UA Mixer Engine, UAD 11.7.1).
- Multi-Apollo cascades are mock-tested only, against a derived two-device fixture.
- Other Apollo models are expected to work because discovery is dynamic, but are unverified. A
  `--dump-tree` capture from an untested model is the fastest way to change that.
- Windows is protocol-compatible in principle and untested.

### Known limitations

Headphone output level cannot be set (no such property exists; use cue-bus send levels).
Console sessions and plugin scenes cannot be saved or recalled. The I/O routing matrix is
readable but not writable. No Dante support, no networked or remote engines, and only TCP 4710
is implemented. Meters are polled at about 3 Hz and a meter at the floor cannot distinguish a
silent source from a dead digital link. Full list in `uad://limitations` and the README.
