# uad-console-mcp Implementation Spec

Contract for all modules. Facts marked VERIFIED were confirmed against a live Apollo x8 Gen 2
(UA Mixer Engine, UAD v11.x, macOS) on 2026-07-29. Do not deviate from module contracts without
updating this file.

## 1. Wire protocol (VERIFIED)

- TCP `127.0.0.1:4710`, served by the `UA Mixer Engine` process (runs even when the Console app
  is closed). Multiple concurrent clients are fine (Console itself is just another client).
- Every message in BOTH directions is UTF-8 JSON text terminated by a single `\0` byte.
- Requests: `<verb> <path>[ <json-value>]` where verb is `get`, `set`, `subscribe`,
  `unsubscribe`, or `post`. Unknown verbs are **silently dropped** (no reply, so never wait on one).
- Replies and broadcasts are JSON objects with a `path` key. There is NO request/response
  correlation ID. Broadcasts (subscription pushes, other clients' changes) interleave freely
  with replies. Dispatch strictly by matching `path`.

### Message shapes (VERIFIED)

| Request | Reply |
|---|---|
| `get /devices/0/inputs` (container node) | `{"path": "/devices/0/inputs", "data": {"properties": {...}, "children": {"0": {}, ...}}}` |
| `get /devices/0/inputs/8/Name` (property) | `{"path": ".../Name", "data": {"type": "string", "value": "UG 1", ...metadata}}` |
| `get /bogus` | `{"path": "/bogus", "error": "Unable to resolve path for get."}` |
| `set /devices/0/inputs/8/Name/value "UG 1"` | `{"path": ".../Name/value", "data": "UG 1"}` (ack echoes applied value) |
| `subscribe <prop-path>` | initial `{"path": "<prop-path>/value", "data": <current>}` then pushes on change only |

- Property metadata is self-describing: `type` (`string`/`bool`/`int`/`int64`/`float`),
  optional `min`, `max`, `default`, `readonly`, `values` (enum: either plain list or list of
  `{value, string, enabled}` objects).
- Getting a **node** returns ALL its scalar properties inline; children returned as name → `{}`.
- Numeric values: faders/gains in dB (`FaderLevel` -144..12); `*Tapered` twins are 0..1.
  The engine quantizes tapered values to a coarse grid, so prefer the dB properties for writes.

### CRITICAL SAFETY FACT (VERIFIED)

The engine does NOT enforce its own `readonly` flags: a `set` on `DeviceHwID`
(`readonly: true`) was **accepted and applied**. Consequences:
1. `safety.py` is the only real write barrier. It maintains an allowlist of writable property
   names (see §5); everything else is refused in `safe` mode.
2. NEVER "probe by writing" against real hardware: not in code, not in tests, not manually.
   All write-behavior tests run against the mock engine.

### Known quirks

- `get /?recursive=1` on the real engine returns only top-level stubs. Walk the tree node by
  node (full x8 Gen 2 walk = 1,941 nodes in ~11 s).
- Plugin parameter nodes (`.../effects/S/parameters/*`) only materialize AFTER a plugin is
  loaded into that slot. Poll for them with a timeout after `set .../EffectName`.
- Meter floor reads as -77.0 (device silence) / -144.0. ~3 Hz polling is proven and cheap.
- A meter at exactly the floor cannot distinguish "silent input" from "dead ADAT link".
- Some clients keepalive with `set /Sleep false` every 3 s. Only send it if the root node
  actually has a `Sleep` property (check the tree first).

## 2. Tree layout (x8 Gen 2 fixture: `tests/fixtures/apollo_x8_gen2_tree.json`)

Root children: `devices`, `inputroutes`, `outputroutes`, `plugins`, `pluginscenes`.
Root properties include `SampleRate` (enum), `ClockSource` (enum: Internal/Word Clock/ADAT/
S-PDIF), `ClockLocked` (bool), `Dirty`, `Tempo`, `MixerState`, `AppCommand` (opaque, never write).

`/devices/N` (one per cascaded Apollo; `MultiUnitOrder` gives chain position):
properties `DeviceName`, `DeviceTypeID`, `DeviceOnline`, `CueBusCount`, `TalkbackMaster`,
`DimAttenuation`, `AltMonSelection`, monitor/headphone correction props;
children `inputs`, `auxs`, `outputs`, `asioinputs`, `asiooutputs`.

`/devices/N/inputs/I`: properties `Name`, `IOType` (Mic/Line/ADAT/S-PDIF/Virtual/TalkbackMic),
`Active`, `ChannelHidden`, `EnabledByUser`, `FaderLevel`, `FaderLevelTapered`, `Mute`, `Solo`,
`SoloSafe`, `Pan`, `Pan2`, `Stereo`, `StereoName`, `Preset`, `TalkbackEnabled`, ...;
children `preamps` (only on preamp-capable channels), `sends`, `effects`, `meters`.

`.../preamps/0`: `Gain`, `GainTapered`, `48V`, `Pad`, `Phase`, `LowCut`, `HiZ` (where present),
plus an `effects/0` child (the Unison slot: `EffectName`, `Preset`, `Power`, `parameters/`).

`.../sends/S`: `ID` (aux1/aux2/cue1..cue4), `Gain`, `GainTapered`, `Pan`, `Bypass`,
`PostFaderOverride`. `.../meters/0`: `MeterLevel`, `MeterPeakLevel`, `MeterClip`.

`/devices/N/outputs/O`: monitor output identified by the PRESENCE of `CRMonitorLevel` /
`CRMonitorLevelTapered` properties (index differs per model, NEVER hardcode). Others: `Mute`,
`DimOn`, `MixToMono`, `AltMonEnabled`, `MixInSource`, `OutputDestination`.

`/plugins/P`: catalog entry: `Name`, `Categories`, `Unison` (bool), `Authorized`, `Status`,
`Preset` (with `values` = preset browser tree). 215 plugins on the fixture.

NOTE: 13 properties in the captured fixture have NO `value` key at all (e.g.
`/devices/0/auxs/0/Gain`, `/devices/0/inputs/27/IOType`). All metadata parsing must tolerate
a missing value (UAProperty.value becomes None).

## 3. Fixture format

```json
{"_meta": {...}, "nodes": {"<path>": {"properties": {"<name>": {...metadata}}, "children": ["a","b"]}}}
```
Derived fixtures are GENERATED at test time by `tests/fixtures/derive.py` (do not commit):
- `cascade`: duplicates `/devices/0` as `/devices/1` (DeviceName "Apollo x8 Gen 2 #2",
  MultiUnitOrder 1, input names prefixed "B ").
- `twin`: strips to 2 preamp inputs + 2 line + 2 virtual + 1 monitor out, renames DeviceTypeID
  "Apollo Twin X". Catches hardcoded-index assumptions.

## 4. Module contracts

All code: Python 3.11+, full type hints, `ruff` clean (config in pyproject), stdlib + `mcp` only.
Never call `time.sleep` in async code. No prints: use `logging` with logger `uad_console_mcp.*`
(MCP stdio servers MUST NOT write to stdout; log to stderr).

### 4.1 `client.py`

```python
class UAError(Exception): ...
class UAConnectError(UAError): ...        # engine unreachable / connection lost
class UATimeoutError(UAError): ...        # no matching reply in time
class UAPathError(UAError): ...           # engine replied {"error": ...}; msg included

@dataclass(frozen=True)
class UAProperty:
    path: str; type: str; value: Any
    min: float | None = None; max: float | None = None
    values: list | None = None; readonly: bool = False; default: Any = None

class UAClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 4710, *,
                 connect_timeout: float = 2.0, scalar_timeout: float = 2.0,
                 tree_timeout: float = 15.0) -> None: ...
    async def connect(self) -> None            # idempotent; lazy: every public op calls it
    async def aclose(self) -> None
    @property
    def connected(self) -> bool: ...
    def on_reconnect(self, cb: Callable[[], Awaitable[None]]) -> None
    async def get_node(self, path: str) -> tuple[dict[str, UAProperty], list[str]]
    async def get_prop(self, path: str) -> UAProperty
    async def get_value(self, path: str) -> Any
    async def set_value(self, path: str, value: Any) -> Any
        # sends `set <path>/value <json>`; awaits the ack for `<path>/value`;
        # ALWAYS verifies with a follow-up get_prop(path); returns the verified value.
        # Serialized: a global asyncio.Lock means one in-flight set at a time.
    async def walk(self, root: str = "/", *, max_nodes: int = 25000,
                   progress: Callable[[int], None] | None = None) -> dict[str, dict]
        # returns fixture-format nodes dict (used by --dump-tree and snapshot)
    async def subscribe(self, path: str, cb: Callable[[Any], None]) -> None
    async def unsubscribe(self, path: str) -> None
    async def poll_values(self, paths: list[str]) -> dict[str, Any]
        # batch read on the SECOND socket (meter/poll socket) so bulk meter reads
        # never contend with control-socket waiters
```

Transport rules:
- Two sockets: control (get/set/subscribe) + poll (poll_values, walk). Same framing code.
- One supervised reader task per socket. If the reader dies (parse error, EOF), it must:
  fail every pending waiter with `UAConnectError`, mark disconnected, schedule reconnect
  (exponential backoff 0.5 s → 8 s with jitter), and on success fire `on_reconnect` callbacks.
- Waiters: list of `(predicate: Callable[[dict], bool], future)`. A reply matches the FIRST
  waiter whose predicate accepts it; broadcasts matching no waiter go to subscription
  callbacks or are dropped. `get` waits for `path == requested`; `set` waits for
  `path == requested + "/value"` OR an `error` reply for the same prefix.
- Framing: accumulate a bytes buffer; split on `\0`; tolerate partial frames, many frames per
  read, multi-byte UTF-8 split across reads, frames up to 8 MB. Decode per frame, not per chunk.

### 4.2 `discovery.py`

```python
@dataclass(frozen=True)
class DeviceInfo:
    index: int; name: str; type_id: str; online: bool
    multi_unit_order: int; cue_bus_count: int

@dataclass(frozen=True)
class ChannelInfo:
    device: int; kind: Literal["input", "aux", "output"]; index: int
    name: str; io_type: str | None; stereo: bool; hidden: bool; active: bool
    has_preamp: bool; is_monitor: bool; is_talkback: bool
    path: str                      # e.g. "/devices/0/inputs/8"

class AmbiguousTargetError(UAError): ...   # carries .candidates: list[ChannelInfo]
class TargetNotFoundError(UAError): ...    # message lists close names

@dataclass
class Rig:
    devices: list[DeviceInfo]
    channels: list[ChannelInfo]
    engine_connected: bool
    def resolve(self, target: str | int, *, device: str | int | None = None,
                kind: str = "input") -> ChannelInfo
        # int → index (device required if >1 device). str → case-insensitive
        # exact match, then prefix, then substring. Ambiguity raises with candidates.
    def monitor_output(self, device: int | None = None) -> ChannelInfo

async def discover(client: UAClient) -> Rig
```
- Enumerate ALL of `/devices/*` (cascades). `is_monitor` = has `CRMonitorLevel` property.
- Topology cached by the server with 30 s TTL and invalidated on reconnect; live values
  (fader, mute, meters) are NEVER cached.

### 4.3 `safety.py`

```python
Mode = Literal["readonly", "safe", "full"]   # env UAD_MCP_MODE, default "safe"

class SafetyRefusal(UAError): ...  # message MUST name the env var / confirm flag that unblocks

class SafetyGate:
    def __init__(self, mode: Mode, max_monitor_db: float, ...) -> None
    def check_write(self, prop: UAProperty, new_value: Any, *, context: str) -> Any
        # returns possibly-clamped value or raises SafetyRefusal
    def monitor_level_check(self, current_db: float, target_db: float) -> float
    def note_monitor_event(self, applied_db: float) -> None
```
Rules:
- `readonly`: every write refused. Tests assert ZERO `set` frames reach the wire.
- `safe` (default): writes allowed only for allowlisted property names:
  `Name, FaderLevel, Pan, Pan2, Mute, Solo, SoloSafe, ChannelHidden, Gain, 48V, Pad, Phase,
  LowCut, HiZ, IOType, Bypass, PostFaderOverride, EffectName, Preset, Power, NormalizedValue,
  CRMonitorLevel, DimOn, MixToMono, DimAttenuation, TalkbackOn, TalkbackMaster,
  TalkbackEnabled, AltMonSelection, ClockSource, SampleRate, StereoName, Stereo, EnabledByUser, SRConvert`
  Clock and sample-rate additionally require the tool-level confirm gates.
- `full`: allowlist bypassed (raw set enabled) but monitor rail and rate limit still apply.
- Denylist in ALL modes: `DeviceHwID`, `MultiUnitOrder`, `AppCommand`, `MixerState`,
  anything under `/pluginscenes` writes, firmware/system props.
- Monitor rail: ceiling `UAD_MCP_MAX_MONITOR_DB` (default -6.0); max +6 dB increase per call;
  rolling max +10 dB per 30 s window. Unmute, un-dim AND a `DimAttenuation` DECREASE while
  `DimOn` is true all count as level events: if the level the release would expose
  (`CRMonitorLevel`, or `CRMonitorLevel - DimAttenuation` for the dim case) exceeds the ceiling,
  refuse with the exact remedy. Raising `DimAttenuation` is always allowed - it only attenuates.
- RULE FOR NEW TOOLS: any tool writing monitor `Mute`, `DimOn` or `DimAttenuation` MUST pass
  `WriteContext.is_monitor=True` plus what the rail needs (`monitor_level_db`, and `dim_on` for
  `DimAttenuation`), because the gate cannot see topology. With `is_monitor=True` a missing level
  is a FAILED READ and the write is REFUSED (fail closed); without it the gate can only log.
  `monitor_level_db` must be the level the ENGINE reported back, never the requested one.
- RULE FOR NEW TOOLS: "is this write a release?" is `safety.coerces_to_false(value)`, never
  `value is False`. The gate coerces `0`, `"no"`, `"n"`, `"off"`; an identity test lets exactly
  those spellings past the rail.
- The write rate limiter is charged inside `check_write`, for refusals as well as allowed writes
  (the caller already spent a property read to get there) - but AFTER the readonly-mode and
  denylist checks, which are permanent: answering those with "retry in 0.1 s" would invite a
  retry loop that can never succeed. `Mute` set true and `TalkbackOn` set false are never
  charged: the bucket is empty exactly when the panic button is needed.
- Every tool that can raise the monitor level runs under `ServerContext.monitor_lock`, because
  MCP runs tool calls concurrently and the rolling budget is read-check-write-record.
- Values clamped to the property's own min/max metadata; enums validated against `values`.
- Global token-bucket rate limit: 10 writes/s, burst 20.
- 48V writes require `confirm_condenser_mic=True` flowing in from the tool layer via `context`.
- Clock/sample-rate: tool layer must pass an is-audio-flowing check (any input meter above
  -60 dB → refuse) AND `confirm=True`.

### 4.4 `snapshot.py`

- `capture(client, rig) -> dict`: walks devices, keeps ONLY allowlisted-writable properties
  (same list as safety `safe` mode, minus ClockSource/SampleRate which are captured into
  `_meta` but not restored by default), PLUS the allowlisted properties that live at `/` rather
  than under `/devices` (the talkback pair `TalkbackOn`/`TalkbackMaster`), restored last in a
  `ROOT` phase. `TalkbackOn: true` is captured but NEVER restored (a restore would open a live
  mic with no auto-off timer behind it); it is reported as skipped, naming `uad_talkback`.
  Each entry: `{path, value, meta: {type,min,max,values}}`.
  Doc: `{schema_version: 1, created: iso8601, devices: [...], sample_rate, entries: [...]}`.
- Files: `~/.uad-console-mcp/snapshots/<name>.json`. `list_snapshots()`, `load(name)`.
- `diff(client, rig, doc, ignore: list[str]) -> list[Change]`: glob patterns against paths.
- `restore(client, rig, doc, *, dry_run=True, include_clock=False, include_monitor=False)`:
  1. auto-capture a `pre-restore-<ts>` snapshot first (not in dry-run);
  2. refuse cross-model restore (DeviceTypeID mismatch) unless `allow_cross_model=True`
     (then map channels by IOType + ordinal, never raw index);
  3. ordering: unlink stereo pairs → EffectName loads (poll for parameters to materialize,
     5 s timeout per slot) → plugin Presets → parameters → Unison → sends → fader/pan/mute/
     solo/name/hidden → relink stereo → (opt-in) monitor, clamped through SafetyGate → root
     (talkback). Monitor strips are the UNION of the document's and the live rig's, so a strip
     that is the control-room output now cannot escape the rail;
  4. per-entry failures collected, never abort mid-way: returns `{applied, skipped, failed}`
     with reasons; dry-run returns the exact ordered write list.

### 4.5 `server.py` + `__main__.py`

Tool surface (~19 tools):

| Tool | Purpose |
|---|---|
| uad_get_status | ALL devices in the cascade: clock, sample rate, DSP load if available, engine vs device online, safety mode + effective limits |
| uad_list_channels | across every unit, grouped by device: names, types, fader, mute/solo, pan, hidden, stereo links |
| uad_set_channels | BATCH: fader_db/pan/mute/solo/rename/hide per item, per-item results; name-or-index targeting with optional `device` |
| uad_set_preamp | gain_db, pad, low_cut, phase, hi_z, source (Mic/Line); 48V requires confirm_condenser_mic=true |
| uad_set_sends | BATCH: channel → aux/cue level_db/pan/bypass |
| uad_set_monitor | level_db (railed), dim, mute, mono, alt speaker; unmute/un-dim go through the rail |
| uad_talkback | on/off + level; auto-off timeout (default 30 s, asyncio task) |
| uad_safe_state | no params: mute monitor outputs + talkback off; the panic button |
| uad_set_clock | clock source; verify ClockLocked after 2.5 s, auto-revert on failure; refuses if audio flowing |
| uad_set_sample_rate | separate, confirm=true required, same audio-flowing refusal |
| uad_list_plugins | UAD catalog: name, categories, unison-capable, authorized |
| uad_get_inserts | what is loaded where (inserts + Unison) across channels |
| uad_load_plugin | insert slot 0-3 or slot="unison"; polls for parameters to materialize (5 s timeout) |
| uad_remove_plugin | clears a slot (EffectName "") |
| uad_get_plugin_params | list a loaded plugin's parameters with values |
| uad_set_plugin_params | BATCH by param name; `normalized` (0-1) values |
| uad_plugin_preset | recall preset by name from the engine's catalog |
| uad_peek_meters | one-shot, all (or filtered) channels |
| uad_watch_meters | poll up to `duration_s` (cap 30), early-exit when any watched channel exceeds `threshold_db`; returns per-channel peaks |
| uad_snapshot | action=save/list/diff/restore; restore defaults dry_run=true; ignore globs for diff |
| uad_raw_get | raw read of any path; raw set ONLY in `full` mode (separate uad_raw_set registered only when mode=full) |

- `FastMCP("uad-console")`, stdio transport:
  all `uad_`-prefixed, batch setters accept lists, every write result is
  `{verified: bool, before, after, warnings: [str]}` per item plus resolved
  `{device, index, name}` identity. Descriptions: first line = what it does; include one
  "Use when:" and one "Not for:" line (e.g. set_monitor: "Not for headphone level: the
  protocol cannot set it; see uad://limitations").
- Resources: `uad://state` (get_status payload), `uad://channels`, `uad://limitations`
  (the cannot-do list + workarounds).
- Prompts: `setup_vocal_tracking`, `onboard_adat_expander`, `troubleshoot_clock`,
  `panic_reset`. Mirrored prose lives in docs/RECIPES.md.
- Errors surfaced to the LLM must be actionable sentences, never tracebacks. Connection
  failure text: "UA Mixer Engine not reachable on 127.0.0.1:4710 - is the UAD software
  installed and running? (It starts with UAD Console and keeps running afterwards.)"
- `__main__.py`: `main()` with argparse. Default: serve; `--check`: doctor (engine reachable,
  devices + online states, safety mode + effective limits, sample rate/clock, plugin count);
  `--dump-tree [FILE]`: fixture-format walk (sanitizes the home directory out of string
  values); `--version`.

### 4.6 `tests/mock_engine.py`

```python
class MockEngine:
    def __init__(self, fixture: dict, *, split_frames=False, batch_frames=False,
                 meter_noise_hz: float = 0.0, reply_delay: float = 0.0,
                 drop_after: int | None = None, never_reply_on: set[str] = frozenset(),
                 port: int = 0) -> None
    async def start(self) -> int   # returns bound port
    async def stop(self) -> None
    sent_frames: list[str]         # every frame received from clients (wire-level asserts)
    def set_count(self) -> int
```
Behavior mirrors §1 EXACTLY, including: error shape for unknown paths, set ack echo on
`<path>/value`, accepting writes to readonly props (mirror the real engine!), silent drop of
unknown verbs, subscribe initial push + on-change pushes. Mutations are in-memory on a deep
copy of the fixture. Adversarial modes: `split_frames` (send replies in 1-3 byte chunks,
splitting UTF-8 multibyte sequences), `batch_frames` (coalesce several replies into one TCP
write), `meter_noise_hz` (broadcast random MeterLevel updates between request and reply),
`drop_after` (close connection abruptly after N frames), `never_reply_on` (swallow requests
for given paths).

### 4.7 Test suite (pytest, asyncio_mode=auto)

Required coverage: framing edges (all adversarial modes); interleaving correctness; N
concurrent tool calls → mock observes strictly serialized sets; reconnect (drop_after) →
pending ops fail with UAConnectError, next op reconnects, discovery re-runs, including a
reconnect where the fixture swapped to the twin (channel count changed); readonly mode →
`mock.set_count() == 0` after exercising every write tool; monitor rail property test: no
sequence of uad_set_monitor calls can exceed the ceiling; snapshot round-trip on cascade
fixture; cross-model restore refusal (x8 doc → twin fixture); golden-shape tests for
uad_get_status / uad_list_channels / uad_set_channels outputs; 60 s random-call soak
(`tests/test_soak.py`, marked `slow`) asserting no unhandled exceptions, the monitor ceiling
never exceeded on the wire, and no leaked tasks/sockets. Default `addopts` deselect both
markers (`-m 'not live and not slow'`); run the soak with `pytest tests/test_soak.py -m slow`
(`UAD_MCP_SOAK_SECONDS` shortens it). Live tests exist under `tests/live/` marked `live`,
skipped unless `UAD_MCP_LIVE=1`.

## 5. Attribution & legal (for docs authors)

- Line 1 of README: unofficial, not affiliated with/endorsed by Universal Audio; UAD, Apollo,
  Unison, Console are trademarks of Universal Audio, Inc.
- Protocol knowledge: independent implementation informed by published community
  descriptions. Credit `raduvarga/UA-Midi-Control` (origin write-up), `Kalskiid/ua-commander`
  (MIT; heartbeat + monitor-discovery patterns), `uaaccess` and `open-apollo` (referenced,
  GPL, no code or data copied). Fixture is our own capture from our own device.
- Hearing/equipment damage disclaimer near the top of README (monitor levels, 48V + ribbon mics).
