# The UA Mixer Engine local protocol

A cheatsheet for the wire protocol this server speaks. Everything marked VERIFIED was confirmed
against a live Apollo x8 Gen 2 (UA Mixer Engine, UAD 11.7.1, macOS) on 2026-07-29. Module
contracts live in [SPEC.md](SPEC.md); this file is the part you need if you are reading the
transport code, debugging a capture, or writing your own client.

Unofficial and reverse-engineered from published community descriptions. Universal Audio does not
document this API, does not support it, and can change it in any update.

## Transport

- **TCP `127.0.0.1:4710`**, served by the `UA Mixer Engine` process. The engine keeps running when
  the Console app is closed, so the app is not required.
- Multiple concurrent clients are fine. Console itself is just another client, and it sees your
  changes live.
- **Framing:** every message in both directions is UTF-8 JSON text terminated by a single `\0`
  byte. No length prefix, no header. A robust reader must accumulate bytes, split on `\0`, and
  decode **per frame** rather than per read: one TCP read can carry several frames, a frame can
  span reads, and a multi-byte UTF-8 sequence can be split across the boundary. This client
  tolerates frames up to 8 MB.
- **Two sockets** are used here: a control socket (`get`, `set`, `subscribe`) and a poll socket
  (batch value reads, tree walks), so bulk meter polling never contends with control-socket
  waiters.

## Verbs

```
<verb> <path>[ <json-value>]\0
```

| Verb | Use |
|---|---|
| `get` | Read a node or a property |
| `set` | Write. Always addressed at `<property-path>/value` |
| `subscribe` | Initial value push, then a push on every change |
| `unsubscribe` | Stop those pushes |
| `post` | Exists in the protocol; not used by this server |

**Unknown verbs are silently dropped.** There is no error reply, so never wait on one.

## Replies

Replies and broadcasts are JSON objects with a `path` key. **There is no request/response
correlation ID.** Subscription pushes and other clients' changes interleave freely with your
replies, so dispatch strictly by matching `path`: a `get` waits for `path == requested`, a `set`
waits for `path == requested + "/value"` or an `error` reply on the same prefix.

Read a container node:

```
-> get /devices/0/inputs
<- {"path": "/devices/0/inputs", "data": {"properties": {...}, "children": {"0": {}, "1": {}, ...}}}
```

Read a property (metadata comes with it):

```
-> get /devices/0/inputs/8/Name
<- {"path": "/devices/0/inputs/8/Name",
    "data": {"type": "string", "default": "ADAT 1", "value": "UG 1"}}
```

Write a property (the ack echoes the applied value):

```
-> set /devices/0/inputs/8/Name/value "UG 1"
<- {"path": "/devices/0/inputs/8/Name/value", "data": "UG 1"}
```

Unresolvable path:

```
-> get /bogus
<- {"path": "/bogus", "error": "Unable to resolve path for get."}
```

Subscribe:

```
-> subscribe /devices/0/inputs/8/meters/0/MeterLevel
<- {"path": "/devices/0/inputs/8/meters/0/MeterLevel/value", "data": -76.99999237060547}
   ... then one push per change, and only on change
```

### Property metadata

Properties are self-describing. `type` is one of `string`, `bool`, `int`, `int64`, `float`, plus
optional `min`, `max`, `default`, `readonly`, and `values` for enums. Enums come in two shapes:

```json
"ClockSource": {"type": "string", "values": ["Internal", "Word Clock", "ADAT", "S/PDIF"],
                "value": "Internal"}

"SampleRate": {"type": "int", "value": 48000,
               "values": [{"value": 44100, "string": "44.1 kHz", "enabled": true},
                          {"value": 48000, "string": "48 kHz",   "enabled": true}]}
```

Getting a **node** returns all of its scalar properties inline, with children as name to `{}`.
That is why a tree walk is cheap in requests: one `get` per node, not one per property.

Numeric conventions: faders and gains are in dB (`FaderLevel` is `-144..12`, preamp `Gain` is
`-144..65`, `CRMonitorLevel` is `-96..0` on the x8 Gen 2). Most of them have a `*Tapered` twin on
`0..1`; the engine quantizes tapered values onto a coarse grid, so **write the dB property, not
the tapered one**.

## The critical safety fact

**The engine does not enforce its own `readonly` flags.** A `set` on `DeviceHwID`, which the engine
itself reports as `readonly: true`, was accepted and applied on real hardware.

Two consequences, both load-bearing for this project:

1. Client-side policy is the only real write barrier. In this server that is `safety.py`, with an
   allowlist of writable property names in `safe` mode and a permanent denylist in every mode.
2. **Never probe by writing** against real hardware, in code, in tests, or by hand. Every
   write-behaviour test in this repo runs against `tests/mock_engine.py`, which deliberately
   mirrors the engine's willingness to accept read-only writes.

## Tree layout

Root children: `devices`, `inputroutes`, `outputroutes`, `plugins`, `pluginscenes`.

Root properties include `SampleRate` (enum), `ClockSource` (enum: `Internal`, `Word Clock`, `ADAT`,
`S/PDIF`), `ClockLocked` (bool), `TalkbackOn`, `TalkbackMaster` (`-1..100`), `TotalDSPLoad`,
`TotalPGMLoad`, `TotalMEMLoad` (all read-only), `Version`, `ProtocolVersion`, `AudioStreaming`,
`MaxDelayComp`, `Dirty`, `Tempo`, `MixerState`, and `AppCommand` (opaque; never write it).

```
/devices/N                     one per cascaded Apollo; MultiUnitOrder gives chain position
  DeviceName, DeviceTypeID, DeviceOnline, CueBusCount, TalkbackMaster, DimAttenuation,
  AltMonSelection, monitor/headphone correction properties
  /inputs/I
    Name, IOType (Mic/Line/ADAT/S-PDIF/Virtual/TalkbackMic), Active, ChannelHidden,
    EnabledByUser, FaderLevel, FaderLevelTapered, Mute, Solo, SoloSafe, Pan, Pan2,
    Stereo, StereoName, Preset, TalkbackEnabled, ...
    /preamps/0                 only on preamp-capable strips
      Gain, GainTapered, 48V, Pad, Phase, LowCut, HiZ (where present)
      /effects/0               the Unison slot: EffectName, Preset, Power, parameters/
    /sends/S                   ID (aux1/aux2/cue1..cue4), Gain, GainTapered, Pan, Bypass,
                               PostFaderOverride
    /effects/S                 insert slots: EffectName, Preset, Power, parameters/
    /meters/0                  MeterLevel, MeterPeakLevel, MeterClip
  /auxs/A                      Name, Gain, FaderLevel, Mute, MixToMono, Preset, ...
  /outputs/O                   Mute, DimOn, MixToMono, AltMonEnabled, MixInSource,
                               OutputDestination
  /asioinputs, /asiooutputs
/plugins/P                     catalog entry: Name, Categories, Unison, Authorized, Status,
                               Preset (values = the preset browser tree)
```

**The control-room monitor output is identified by the presence of `CRMonitorLevel` /
`CRMonitorLevelTapered`, never by index.** It is output 22 on the x8 Gen 2 and will be somewhere
else on your model. The same rule applies to every index in this tree.

Reference numbers from the captured x8 Gen 2 tree (`tests/fixtures/apollo_x8_gen2_tree.json`):
1,941 nodes; 28 input strips (4 mic with preamps, 4 line, 8 ADAT, 2 S/PDIF, 8 virtual, 1 talkback,
1 unused); 2 auxs; 26 outputs; 6 sends per input strip (`aux1`, `aux2`, `cue1`..`cue4`); 8
`effects` children per input strip; 215 installed plugins; `CueBusCount: 2`.

## Known quirks

- `get /?recursive=1` returns only top-level stubs on the real engine. Walk node by node. A full
  x8 Gen 2 walk is 1,941 nodes in about 11 seconds.
- **Plugin parameter nodes (`.../effects/S/parameters/*`) do not exist until a plugin is loaded**
  into that slot. After `set .../EffectName`, poll for them with a timeout (this server allows
  5 seconds) rather than assuming.
- **Meter floor reads as -77.0** (or -144.0). Captured values wobble by float noise
  (-76.99999237060547), so compare with slack. Polling at about 3 Hz is proven and cheap.
- **A meter at exactly the floor cannot distinguish a silent input from a dead ADAT link.** No
  amount of reading resolves it; check clock and cabling.
- 13 properties in the captured fixture have **no `value` key at all** (for example
  `/devices/0/auxs/0/Gain`, `/devices/0/inputs/27/IOType`). Metadata parsing must tolerate that.
- The preset browser exposes `values` as a **hierarchical folder/file tree**, not a flat enum, so
  it cannot be validated like one.
- Some community clients keepalive with `set /Sleep false` every 3 seconds. Only send it if the
  root node actually has a `Sleep` property. The x8 Gen 2 root does not, and this server does not
  send it.
- `set` acks echo the applied value, but an ack is not proof: this server always follows a write
  with a `get` and reports `verified` from the read-back.

## Capturing a tree dump, and contributing it

A fixture from an untested Apollo is the most useful thing you can send this project. The dump is
read-only.

```bash
uad-console-mcp --dump-tree ~/Desktop/apollo-<model>-tree.json
```

- Walks the whole tree and writes it in the fixture format:
  `{"_meta": {...}, "nodes": {"<path>": {"properties": {...}, "children": [...]}}}`.
- **Your home directory is replaced with `~` in every string value** before anything is written,
  because preset browser paths and volume names otherwise leak your login name.
- Use `-` as the filename to write to stdout instead. Logs always go to stderr.
- Point it elsewhere with `--host` / `--port` (defaults `127.0.0.1` and `4710`).

Open an issue with your Apollo model in the title, attach the JSON, and note your OS and UAD
version. From that file the test suite can prove the code handles your channel count, monitor
output index and send layout without touching your hardware. Derived variants (a two-device
cascade, a stripped-down "twin") are generated at test time by `tests/fixtures/derive.py`.

## Attribution

Independent implementation, informed by published community descriptions of this API:

- [raduvarga/UA-Midi-Control](https://github.com/raduvarga/UA-Midi-Control): the original public
  write-up of the engine's TCP API, and the origin of the protocol knowledge used here.
- [Kalskiid/ua-commander](https://github.com/Kalskiid/ua-commander) (MIT): keepalive and
  monitor-discovery patterns.
- `uaaccess` and `open-apollo`: referenced for orientation only. Both are GPL-licensed and **no
  code or data from either was copied**.
- `tests/fixtures/apollo_x8_gen2_tree.json` is our own read-only capture from our own device.

UAD, Apollo, Unison and Console are trademarks of Universal Audio, Inc. This project is not
affiliated with, endorsed by, or supported by Universal Audio, Inc.
