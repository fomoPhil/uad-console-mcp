> **Unofficial project.** Not affiliated with, endorsed by, or supported by Universal Audio, Inc.
> UAD, Apollo, Unison, and Console are trademarks of Universal Audio, Inc. This software is an
> independent implementation built against the local API that the UA Mixer Engine already exposes
> on this machine.

# uad-console-mcp

An MCP server that lets Claude (or any MCP client) read and control a Universal Audio Apollo
interface: channel names and faders, preamp gain, cue and aux sends, the control-room monitor,
talkback, clock and sample rate, UAD plugin inserts, meters, and whole-rig snapshots. It talks to
the **UA Mixer Engine** over its local TCP API on `127.0.0.1:4710`, the same API the Console app
uses.

The Console *app* does not need to be open. The **engine** does need to be running: it starts with
UAD Console and keeps running after you quit the app. Nothing leaves your machine.

## Does this work for me?

| Setup | Status |
|---|---|
| Apollo x8 Gen 2, Thunderbolt | **Live-tested** against real hardware (UA Mixer Engine, UAD 11.7.1) |
| Other Thunderbolt / USB Apollos (Twin, x4, x6, x8p, x16, Solo, Duo, Quad) | **Expected to work.** Same engine, same protocol, and discovery is fully dynamic: no channel index, monitor output, or slot count is hardcoded. Unverified, so please [report back](#contributing) |
| Multi-unit cascades (two or more Apollos) | **Mock-tested** against a derived two-device fixture. The code enumerates every `/devices/N` and takes an explicit `device` argument everywhere. Never run against real cascaded hardware |
| macOS | **Verified** |
| Windows | **Protocol-compatible, unverified.** The server is pure Python over plain TCP with nothing platform-specific in it, so it should work wherever the engine listens on `127.0.0.1:4710`. Nobody has tried it yet |

Discovery is why "expected to work" is a real expectation and not a hope: the server walks the
engine's own tree, finds the monitor output by the presence of its `CRMonitorLevel` property,
reads each strip's `IOType`, and asks the engine which send buses and insert slots exist. A model
with a different channel count is a different tree, not a different code path.

## Safety: read this before you install

This server can make your speakers loud and can put 48 volts down a microphone cable. An LLM
driving it will occasionally be confidently wrong. The safety layer exists because of that, and
because of one measured fact about the engine:

> **The UA Mixer Engine does not enforce its own read-only flags.** A `set` on a property the
> engine itself reported as `readonly: true` was accepted and applied on real hardware. The engine
> will not stop a bad write. The gate in `safety.py` is the only real barrier.

### Three modes

| `UAD_MCP_MODE` | What it allows |
|---|---|
| `readonly` | Nothing is written, ever. Every write is refused before it reaches the socket. Good for a first look, and for letting an LLM inspect a live session during a take |
| `safe` (**default**) | Writes allowed only to an allowlist of 34 property names: names, faders, pans, mutes, solos, preamp controls, sends, plugin slots and parameters, monitor, talkback, clock and rate. Everything else is refused |
| `full` | The allowlist is bypassed and `uad_raw_set` is registered. The monitor rail, the permanent denylist, and the write rate limit still apply |

In every mode a permanent denylist refuses device identity and engine internals (`DeviceHwID`,
`MultiUnitOrder`, `AppCommand`, `MixerState`, `DeviceTypeID`, anything under `/pluginscenes`, and
anything that looks like firmware, a bootloader, or a serial number).

### Hearing and speaker rails

Monitor level is not treated as a normal property. It is railed three ways at once, and unmuting
or un-dimming counts as a level event: releasing a mute on a stored level of 0 dB is exactly as
loud as setting 0 dB.

| Variable | Default | What it does |
|---|---|---|
| `UAD_MCP_MODE` | `safe` | `readonly`, `safe`, or `full` (above) |
| `UAD_MCP_MAX_MONITOR_DB` | `-6.0` | Hard ceiling on monitor level in dB. Requests above it are refused, not silently clamped. Unmuting onto an above-ceiling stored level is refused too |
| `UAD_MCP_MAX_MONITOR_STEP_DB` | `6.0` | Largest increase allowed in one call. Bigger jumps are clamped down to this |
| `UAD_MCP_MONITOR_WINDOW_DB` | `10.0` | Total increase allowed inside the rolling window |
| `UAD_MCP_MONITOR_WINDOW_SECONDS` | `30.0` | Length of that rolling window, in seconds |
| `UAD_MCP_WRITES_PER_SECOND` | `10.0` | Global write rate limit (token bucket), so a confused agent cannot machine-gun the engine |
| `UAD_MCP_WRITE_BURST` | `20` | Burst size for that rate limit |
| `UAD_MCP_ALLOW_READONLY_WRITES` | `0` | Off by default: writes to properties the engine marks read-only are refused. Only set this if you know exactly which property you mean |
| `UAD_MCP_SNAPSHOT_DIR` | `~/.uad-console-mcp/snapshots` | Where `uad_snapshot` files are written |

Every refusal names the variable or confirm flag that would unblock it, so an agent can tell you
the remedy instead of retrying blindly:

```
Refused monitor level -3.0 dB: above the -6.0 dB ceiling. Ask for -6.0 dB or less,
or raise UAD_MCP_MAX_MONITOR_DB (hearing and speaker damage risk).
```

### 48V phantom power and ribbon microphones

`uad_set_preamp(phantom_48v=true)` is refused unless the same call carries
`confirm_condenser_mic=true`. **Phantom power can permanently destroy a ribbon microphone**, and
some vintage dynamics. A model that has not been told what microphone is plugged in cannot
satisfy that flag honestly, which is the point:

```
Refused to switch 48V phantom power on at /devices/0/inputs/0/preamps/0/48V: it can
permanently damage ribbon microphones and some vintage dynamics. Confirm the connected
microphone is a condenser and pass confirm_condenser_mic=True.
```

### No warranty

This is MIT-licensed software with **no warranty of any kind**. It writes to real audio hardware
in real time. You are responsible for your ears, your monitors, your microphones, and your
session. Sudden or sustained loud audio causes permanent hearing damage. The rails here reduce
the chance of an accident; they are not hearing protection and they are not a substitute for your
own hand on the monitor knob. Do not run this during a take you cannot afford to lose, and start
in `readonly` mode.

## Install

Requires Python 3.11 or newer, and the UAD software installed with the engine running. Not on
PyPI yet, so run it straight from git with [uv](https://docs.astral.sh/uv/):

```bash
uvx --from git+https://github.com/fomoPhil/uad-console-mcp uad-console-mcp
```

**Claude Code:**

```bash
claude mcp add uad-console -- uvx --from git+https://github.com/fomoPhil/uad-console-mcp uad-console-mcp
```

**Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "uad-console": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/fomoPhil/uad-console-mcp",
        "uad-console-mcp"
      ],
      "env": {
        "UAD_MCP_MODE": "safe",
        "UAD_MCP_MAX_MONITOR_DB": "-12"
      }
    }
  }
}
```

### Verify it before you wire it up

`--check` is a doctor. It reads and never writes, so it is safe to run at any time:

```console
$ uvx --from git+https://github.com/fomoPhil/uad-console-mcp uad-console-mcp --check
uad-console-mcp 0.1.0
engine:        reachable at 127.0.0.1:4710
               UAD version 11.7.1, protocol 1.0.0
clock:         Internal, locked=True
sample rate:   48000 Hz
dsp load:      11.4% dsp, 8.0% program
device 0:      Apollo x8 Gen 2 (Apollo x8 Gen 2) online: 28 inputs, 2 auxs, 26 outputs
monitor out:   MONITOR at /devices/0/outputs/22
plugins:       215 installed
safety mode:   safe (from UAD_MCP_MODE)
monitor rail:  ceiling -6.0 dB, max +6.0 dB per call, +10.0 dB per 30 s
write limit:   10/s, burst 20
raw set tool:  not registered
```

It exits non-zero if the engine is unreachable, no device is found, or a device is offline.

Other flags: `--dump-tree [FILE]` (walk the whole tree into the test-fixture format, home
directory scrubbed out, `-` for stdout), `--host` / `--port` (defaults `127.0.0.1` and `4710`),
`--log-level` (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`, default `INFO`, always on stderr),
and `--version`.

## What a session looks like

Labelling an 8-channel ADAT expander and building the drummer's cue mix. Condensed, but these are
the real calls and the real result shapes.

**You:** *"My ADAT expander has kick, snare top, snare bottom and two overheads on the first five
channels. Name them properly and give the drummer a headphone mix."*

```
uad_get_status()
-> clock: {source: "Internal", locked: true, sample_rate: 48000}
   devices: [{device: 0, name: "Apollo x8 Gen 2", online: true, cue_buses: 2,
              channels: {input: 28, aux: 2, output: 26}}]
   safety: {mode: "safe", max_monitor_db: -6.0}

uad_list_channels(kind="input")
-> ... {index: 8,  name: "UG 1", io_type: "ADAT", fader_db: 0.0, mute: false, has_preamp: false}
       {index: 9,  name: "UG 2", io_type: "ADAT", ...}
       {index: 10, name: "UG 3", io_type: "ADAT", ...}   (8 ADAT strips, indices 8 to 15)
```

The eight `UG n` strips are the expander. Renaming and unhiding them in one batch:

```
uad_set_channels(items=[
  {target: "UG 1", name: "KICK IN",   fader_db: 0, mute: false, hidden: false},
  {target: "UG 2", name: "SNARE TOP", fader_db: 0, mute: false, hidden: false},
  {target: "UG 3", name: "SNARE BTM", fader_db: 0, mute: false, hidden: false},
  {target: "UG 4", name: "OH L",      fader_db: 0, mute: false, hidden: false, pan: -1.0},
  {target: "UG 5", name: "OH R",      fader_db: 0, mute: false, hidden: false, pan:  1.0},
])
-> results[0]: {resolved: {device: 0, index: 8, name: "UG 1"}, ok: true,
                writes: [{path: "/devices/0/inputs/8/Name", property: "Name",
                          verified: true, before: "UG 1", after: "KICK IN", warnings: []}, ...]}
```

`verified: true` is a real read-back, not an assumption: the server sets the value, gets it again,
and compares.

```
uad_set_sends(items=[
  {target: "KICK IN",   send: "cue1", level_db: -6},
  {target: "SNARE TOP", send: "cue1", level_db: -8},
  {target: "SNARE BTM", send: "cue1", level_db: -12},
  {target: "OH L",      send: "cue1", level_db: -10, pan: -1.0},
  {target: "OH R",      send: "cue1", level_db: -10, pan:  1.0},
])
-> results[0]: {send: "cue1", resolved: {index: 8, name: "KICK IN"}, ok: true,
                writes: [{path: "/devices/0/inputs/8/sends/2/Gain",
                          verified: true, before: -144.0, after: -6.0}]}
```

The new names work immediately: a verified rename updates the cached topology in place, so the
resolver does not have to wait out its 30 second TTL. Send IDs are resolved from the engine too,
which is why `cue1` landed on `sends/2` here rather than on a guessed index.

```
uad_watch_meters(targets=["KICK IN","SNARE TOP","OH L","OH R"], duration_s=10, threshold_db=-40)
-> {triggered: true, triggered_channels: ["KICK IN"],
    peaks_db: {"KICK IN": -8.4, "SNARE TOP": -11.2, "OH L": -19.0, "OH R": -18.6}, polls: 7}
```

**Claude:** *"Named and metered, kick peaking at -8 dBFS. The drummer's cue is built on cue1, and
HP 1 is already fed from cue1. The headphone volume itself has to come off the front-panel knob:
the protocol exposes no level property for the HP outputs."*

## Tool reference

21 tools in `readonly` and `safe` mode, 22 in `full` mode. Every write result carries
`{verified, before, after, warnings}` plus the resolved `{device, index, name}`, and every tool
returns an actionable sentence instead of a traceback when something fails.

| Tool | One line |
|---|---|
| `uad_get_status` | Engine, clock, sample rate, DSP load, every Apollo in the cascade, and the effective safety limits |
| `uad_list_channels` | Every input, aux or output strip across every unit, grouped by device, with live fader, mute, solo, pan, hidden and stereo state |
| `uad_set_channels` | Batch: `fader_db`, `pan`, `pan2`, `mute`, `solo`, `solo_safe`, `name`, `hidden`, `stereo`, `enabled` per item, targeted by name or index |
| `uad_set_preamp` | One mic/line input's `gain_db`, `pad`, `low_cut`, `phase`, `hi_z`, `source`, and 48V (which needs `confirm_condenser_mic=true`) |
| `uad_set_sends` | Batch: a channel's `level_db`, `pan` or `bypass` into an aux or cue bus (`send: "aux1"`, `"cue1"`, or an index) |
| `uad_set_monitor` | Control room: `level_db` (railed), `dim`, `mute`, `mono`, `alt_speaker`, `dim_attenuation_db` |
| `uad_talkback` | Talkback on/off and level, with an auto-off timer (default 30 s) so an open talkback mic cannot be forgotten |
| `uad_safe_state` | Panic button, no arguments: mute every monitor output and turn talkback off |
| `uad_set_clock` | Change clock source (`Internal`, `Word Clock`, `ADAT`, `S/PDIF`), verify `ClockLocked`, auto-revert if it does not lock. Needs `confirm=true` and silent inputs |
| `uad_set_sample_rate` | Change the system sample rate. Needs `confirm=true` and silent inputs, because every running stream will glitch |
| `uad_list_plugins` | The installed UAD catalog: name, categories, Unison capability, authorization. Filterable |
| `uad_get_inserts` | What is loaded where: every insert slot plus the Unison slot, with plugin, power state and preset |
| `uad_load_plugin` | Load a plugin into an insert slot or `slot="unison"`, then poll up to 5 s for its parameter nodes and report whether they appeared |
| `uad_remove_plugin` | Clear an insert or Unison slot, reporting what was removed |
| `uad_get_plugin_params` | A loaded plugin's parameters: names, normalized 0-1 values, and the engine's own display text |
| `uad_set_plugin_params` | Batch-set a loaded plugin's parameters by name, using normalized 0-1 values |
| `uad_plugin_preset` | Recall a preset by name, validated against that plugin's preset browser when the engine lists it |
| `uad_peek_meters` | One-shot meter read (level, peak, clip) with at-floor channels called out |
| `uad_watch_meters` | Poll meters for up to `duration_s` (capped at 30 s) and return the moment any channel crosses `threshold_db` |
| `uad_snapshot` | `action="save" / "list" / "diff" / "restore"` for whole-rig state. Restore is `dry_run=true` by default and captures a `pre-restore-*` snapshot first |
| `uad_raw_get` | Read any node or property in the engine tree verbatim, with its metadata. Read-only escape hatch |
| `uad_raw_set` | **`full` mode only.** Write any property, bypassing the allowlist. The monitor rail, denylist and rate limit still apply |

### Resources

| URI | Contents |
|---|---|
| `uad://state` | Live `uad_get_status` payload (JSON) |
| `uad://channels` | Live `uad_list_channels` payload for every input, aux and output (JSON) |
| `uad://limitations` | The cannot-do list, with a workaround for each item (Markdown) |

### Prompts

`setup_vocal_tracking`, `onboard_adat_expander`, `troubleshoot_clock`, `panic_reset`. Prose
versions of all four, plus a gain-staging guide, live in [docs/RECIPES.md](docs/RECIPES.md).

## Not supported

These are hard limits of the API this server talks to, not a to-do list. The `uad://limitations`
resource says the same thing to the model at runtime.

| Not supported | Why, and what to do instead |
|---|---|
| **Headphone output level** | The HP outputs expose no level property at all. On the captured x8 Gen 2 tree, `HP 1` has only `MixInSource` (which cue bus feeds it), `Stereo` and a read-only `Active`. Workaround: pick the cue bus with `MixInSource`, build the balance with `uad_set_sends` into that bus, and use the physical knob for the final trim |
| **Saving a Console session file** | There is no session save or recall in this API, and writes under `/pluginscenes` are permanently denied. Use `Cmd+S` in Console for a real session file. The workaround inside this server is `uad_snapshot`, which captures every property it is allowed to write and restores them in a safe order (plugins first, monitor last and opt-in) |
| **Re-patching the I/O matrix** | `/inputroutes` and `/outputroutes` can be read with `uad_raw_get`, but the property carrying the mapping is not on the allowlist and no tool writes it. Re-routing needs Console |
| **Dante** | No Dante tools, and no Dante routing nodes in the tree this server walks. The x8 Gen 2 reports a read-only `SupportsDante: false`. Dante configuration lives in UA's and Audinate's own software |
| **Other UA local ports** | Only TCP 4710 is implemented. Whatever the UAD software may offer on other local ports (community projects mention a helper on 4720) is out of scope: not reverse-engineered, not used, not tested |
| **Remote or networked engines** | `--host` / `--port` exist so `--check` and `--dump-tree` can be pointed at a mock engine. The protocol has no authentication and no encryption, so running it across a network is not a supported configuration |
| **Creating or deleting channels** | The strip list is fixed by the hardware model |
| **Audio itself** | No capture, no playback, no analysis. Meters are polled at about 3 Hz, `uad_watch_meters` caps at 30 s, and a meter at the floor (-77 dB) cannot tell a silent source from a dead digital link |
| **Per-plugin DSP cost** | Only the totals are exposed (`TotalDSPLoad`, `TotalPGMLoad`, `TotalMEMLoad`), in `uad_get_status` |
| **Monitor level above the ceiling** | By design: `UAD_MCP_MAX_MONITOR_DB` (default -6 dB), at most +6 dB per call and +10 dB per 30 s. Raise the variable deliberately, or turn the knob yourself |

## Companion skills

Two Claude skills ship in [`skills/`](skills/). They are optional. The server works without them,
but they carry the studio judgement that does not belong in a tool description.

| Skill | What it is for |
|---|---|
| `apollo-onboarding` | Planning and configuring a rig from scratch: the interview questions to ask, which physical input suits which source, the clock master decision for ADAT expanders and other digital gear, ADAT sample-rate and S/MUX channel limits, multi-Apollo cascades, then channel naming, hiding and a baseline snapshot |
| `apollo-studio-tasks` | Day-to-day operation: tracking setup with a Unison preamp and honest gain staging, insert chains, cue and headphone mixes for one or several performers, podcast and streaming layouts, troubleshooting decision trees ("no sound from channel 3", clicks and pops, a strip that vanished), and snapshot-before-you-experiment habits |

Each skill is a self-contained directory: a `SKILL.md` plus a `references/` folder it points at
when a step needs the detail. Install by copying both into your personal skills folder:

```bash
mkdir -p ~/.claude/skills
cp -R skills/apollo-onboarding skills/apollo-studio-tasks ~/.claude/skills/
```

See [`skills/README.md`](skills/README.md) for details.

For agent-facing rules (hard prohibitions, canonical tool sequences, an error table and a studio
glossary), see [AGENTS.md](AGENTS.md).

## Contributing

**The most useful contribution is a tree dump from an Apollo model nobody has tested yet.** That is
exactly how a model moves from "expected to work" to "verified".

```bash
uad-console-mcp --dump-tree ~/Desktop/my-apollo-tree.json
```

It only reads. The walk takes about 11 seconds for roughly 1,900 nodes, and your home directory is
scrubbed out of every string before the file is written (preset browser paths otherwise leak your
login name). Open an issue titled with your model, attach the JSON, and say which OS and UAD
version you are on. From that fixture the test suite can prove the code handles your channel
count, your monitor output index and your send layout without ever touching your hardware.

Bug reports are far more useful with the `--check` output plus the exact tool call and result.

### Dev setup

```bash
git clone https://github.com/fomoPhil/uad-console-mcp
cd uad-console-mcp
uv sync
uv run pytest            # 405 tests, all against a mock engine, no hardware needed
uv run ruff check .
```

A further 7 tests in `tests/live/` are marked `live`: they need a real engine and a real Apollo,
and are excluded by default (`addopts = -m 'not live'`). Run them deliberately, never in CI:

```bash
UAD_MCP_LIVE=1 uv run pytest -m live
```

Two rules in this codebase are not negotiable:

1. **Never probe by writing against real hardware.** The engine accepts writes to its own
   read-only properties. All write-behaviour tests run against `tests/mock_engine.py`.
2. **Never hardcode a channel index, a monitor output index, or a slot count.** Discovery reads
   them from the tree. The derived `twin` fixture exists specifically to catch this.

The wire protocol is documented in [docs/PROTOCOL.md](docs/PROTOCOL.md), and the module contracts
in [docs/SPEC.md](docs/SPEC.md).

## Acknowledgements and attribution

This is an independent implementation, informed by published community descriptions of the
engine's local API. Credit where it is due:

- [**raduvarga/UA-Midi-Control**](https://github.com/raduvarga/UA-Midi-Control) for the original
  public write-up of how the UA Mixer Engine's TCP API works. That write-up is the reason this
  project was possible at all.
- [**Kalskiid/ua-commander**](https://github.com/Kalskiid/ua-commander) (MIT) for the keepalive
  and monitor-discovery patterns.
- **uaaccess** and **open-apollo** were referenced for orientation. Both are GPL-licensed, and
  **no code or data from either was copied** into this project.
- The test fixture (`tests/fixtures/apollo_x8_gen2_tree.json`) is our own read-only capture from
  our own Apollo x8 Gen 2.

Universal Audio has not been involved in this project. Please do not send them support requests
about it.

## License

MIT. See [LICENSE](LICENSE).
