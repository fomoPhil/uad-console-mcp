# Podcast and livestream setups

Three things make these different from music tracking: computer audio has to come into the
Console mix, the finished mix has to get back out to an app, and a remote guest must never hear
themselves.

## Discover first

```
uad_get_status()
uad_rig_profile()
uad_list_channels(kind="input")
uad_get_routing()
```

Inputs with `has_preamp: true` are the mics. Inputs with `io_type: "Virtual"` are computer audio.
`uad_get_routing` gives the channel numbers apps see. Every model differs, so read them.

## How the Apollo does loopback

No extra software is needed for the basic case.

```
macOS audio ──> VIRTUAL 1/2 strip ─┐
                                   ├─> Console monitor mix ──> MON L/R driver inputs ──> stream app
mics (+ UAD plugins) ──────────────┘                       └─> monitors and headphones
```

- **Computer audio in.** macOS plays to the Apollo's virtual outputs. Audio MIDI Setup > select
  the Apollo > Configure Speakers > Stereo picks which pair system audio uses (VIRTUAL 1/2 is
  channels 21-22 on an x8, per `uad_get_routing().virtual_outputs`). Then System Settings > Sound >
  Output: the Apollo. The audio arrives on the matching `VIRTUAL` strip in Console.
- **The mix out.** The driver inputs `MON L/R` carry the Console monitor mix (27-28 on an x8, per
  `uad_get_routing().loopback.monitor_mix`). `AUX1 L/R` and `AUX2 L/R` carry the aux buses.
- **Apps that only read inputs 1-2.** Zoom, browsers and most stream apps do. Then MON L/R must
  move to inputs 1-2 in UAD Console > Settings > I/O Matrix (most Apollos list a `Livestreaming`
  preset). That renumbers the DAW's inputs too, so switch back for recording sessions. After the
  user changes it, `uad_get_routing().loopback.monitor_mix.channels == [1, 2]` confirms it.
- **Disabled virtual channels.** The I/O Matrix can switch virtual channels off. They show
  `active: false` in `uad_get_routing().virtual_outputs`, and nothing can play to them until the
  user enables them in Console.

None of the routing is writable by this server. Hand each step to the user with the exact channel
numbers, wait, then prove it with meters.

## Solo stream: one mic plus computer audio

```
uad_mode(action="plan", mode="livestream")       # defaults from the rig profile
```

The plan opens the mic and the computer-audio strip if they are muted or down (voice at 0 dB,
computer audio at -10 dB, only when the fader was off), clears every solo (a solo leaves the stream
with one strip), and lists:

- `warnings`: monitors live with an open mic, 48V that disagrees with the profile, disabled
  virtual channels. Relay each one.
- `you_do`: Audio MIDI Setup, the stream app's input, the I/O Matrix if needed, and the rule
  against looping the stream app's monitor output back into the Apollo.
- `check`: the meter watch and a test recording.

Apply with the same arguments on the user's yes. Keep the returned `snapshot` name to undo it.

## Two hosts in one room

```
uad_set_channels(items=[
  {"target": 1, "name": "HOST",  "fader_db": 0, "mute": false, "solo": false, "hidden": false},
  {"target": 2, "name": "GUEST", "fader_db": 0, "mute": false, "solo": false, "hidden": false}
])
uad_set_preamp(target="HOST",  source="Mic", gain_db=30, low_cut=true)
uad_set_preamp(target="GUEST", source="Mic", gain_db=30, low_cut=true)
uad_mode(action="plan", mode="livestream", inputs=["HOST", "GUEST"])
```

If either mic is a condenser, and only after the user confirms it:

```
uad_set_preamp(target="HOST", phantom_48v=true, confirm_condenser_mic=true)
```

Gain stage each mic separately against real speech at its loudest, one at a time:

```
uad_watch_meters(targets=["HOST"],  duration_s=15, threshold_db=-6)
uad_watch_meters(targets=["GUEST"], duration_s=15, threshold_db=-6)
```

Targets are the same as anywhere else: average around -18 dBFS, peaks under -6.

**Two open mics in one room will pick each other up.** That is a microphone-placement problem, not a
software one. Recommend closer mic technique and pointing the null of each mic at the other host
before reaching for processing.

## Per-mic processing

Speech benefits from a compressor first, then an EQ, on each mic separately rather than on a bus.
Plugins on the strip are in the MON L/R mix, so the stream gets them.

```
uad_list_plugins(search="compressor", authorized_only=true)
uad_load_plugin(target="HOST",  plugin="<name>", slot=0)
uad_load_plugin(target="GUEST", plugin="<name>", slot=0)
uad_get_plugin_params(target="HOST", slot=0)
uad_set_plugin_params(target="HOST", slot=0, params=[{"name": "<exact name>", "normalized": 0.35}])
```

Check `materialized` on each load. If it is false, the parameter nodes never appeared and parameter
writes for that slot are skipped rather than silently lost.

A Unison preamp on each voice is optional character, loaded before the inserts:

```
uad_list_plugins(unison_only=true, authorized_only=true)
uad_load_plugin(target="HOST", plugin="<unison-capable name>", slot="unison")
```

Then re-check gain, because Unison changes the gain relationship.

**DSP is finite.** `uad_get_status` reports `dsp.total_dsp_load_percent`. Per-plugin cost is not
exposed, so if a load starts failing, check the total and remove something with
`uad_remove_plugin(target=..., slot=...)`.

## A remote guest: the mix-minus

The guest must hear the hosts and the music, never themselves. The call app therefore needs:

- **its own strip**: the call app plays into its own virtual pair (for example VIRTUAL 3/4), not
  the pair the rest of the computer audio uses;
- **its own mix**: an aux bus with everything except that strip.

```
uad_mode(action="plan", mode="livestream", guest="VIRTUAL 3")      # or role call_guest in the profile
  -> sends: mics -> aux2 at 0 dB, computer audio -> aux2 at -10 dB (only if they were off),
            the guest -> aux2 off
  -> channels: the AUX 2 return muted, so the call mix is not added to the monitor mix twice
  -> you_do: call app mic = AUX2 L/R (31-32 on an x8), call app speaker = the guest's pair
uad_get_mix(bus="aux2")                                             # prove what the guest hears
```

`bus="aux1"` uses the other aux. The plan warns if other strips also send to that aux or if a
plugin (a reverb) is loaded on it. If the guest and the computer audio are the same strip, it
asks instead of applying: the guest would hear themselves.

The honest catch: most call apps use only channels 1-2 of a device, so they cannot pick AUX2 L/R
or VIRTUAL 3/4 by themselves. That step needs a channel router (Rogue Amoeba Loopback, or
BlackHole with an aggregate device) or a DAW in between. Say so plainly.

Unverified on hardware: whether the driver's AUX capture is taken before or after the aux return
mute. If the guest hears nothing at all from the hosts, unmute the return
(`uad_set_channels(items=[{"target": "AUX 2", "kind": "aux", "mute": false}])`) and accept that the
call mix also plays in the monitors. Test by asking the guest; they are the only real meter.

## Headphone feeds for the hosts

Each host hears the other hosts, the remote guest and the music, through a cue bus:

```
uad_set_sends(items=[
  {"target": "HOST",      "send": "cue1", "level_db": -8},
  {"target": "GUEST",     "send": "cue1", "level_db": -8},
  {"target": "VIRTUAL 3", "send": "cue1", "level_db": -10},
  {"target": "VIRTUAL 1", "send": "cue1", "level_db": -16}
])
uad_get_mix(bus="cue1")              # sources, levels, and which HP output plays cue1
```

**The headphone output level itself cannot be set by this protocol.** Build the balance with these
sends; the physical knob does the final trim.

## Feedback paths

- Open mics with the monitors up. Hosts on headphones and monitors muted is the safe setup, but
  ask before `uad_set_monitor(mute=true)`: the user may have no headphones on. Never raise the
  monitor level to test anything.
- An app that records MON L/R and also plays its own output to the Apollo's virtual channels
  (a stream app's "monitor" output, a call app on the same pair). That loops the mix into itself.
- **If it squeals, `uad_safe_state()` first.** It mutes every monitor output and turns talkback off,
  takes no arguments and needs no confirmation. Diagnose afterwards.

## Mute discipline during a show

Muting a channel mid-show is a normal request and is safe:

```
uad_set_channels(items=[{"target": "GUEST", "mute": true}])
uad_set_channels(items=[{"target": "GUEST", "mute": false}])
```

Solo is not a mute. Soloing one channel changes what everyone hears, the stream included, and is
almost never wanted during a live show. `uad_mode(mode="livestream")` clears stray solos.

## Lock it in

```
uad_snapshot(action="save", name="podcast-2mic")
```

Then remind the user to press Cmd+S in Console: the server cannot save a Console session, and a
snapshot is not a substitute for one.
