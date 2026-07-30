# Podcast and livestream setups

Two things make these different from music tracking: several open microphones in one room, and
computer audio that has to come back into the Apollo and go out to the hosts without echoing back to
a remote guest.

## Discover first

```
uad_get_status()
uad_list_channels(kind="input")
```

Note which inputs have `has_preamp: true` (the mics) and which have `io_type: "Virtual"` (computer
audio). The number of virtual channels differs per model, so read them rather than assuming.

## Two hosts in one room

```
uad_set_channels(items=[
  {"target": 1, "name": "HOST",  "fader_db": 0, "mute": false, "solo": false, "hidden": false},
  {"target": 2, "name": "GUEST", "fader_db": 0, "mute": false, "solo": false, "hidden": false}
])
uad_set_preamp(target="HOST",  source="Mic", gain_db=30, low_cut=true)
uad_set_preamp(target="GUEST", source="Mic", gain_db=30, low_cut=true)
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

## Computer audio in and out

Computer audio (a remote guest on a call, music beds, video playback) reaches the Apollo on channels
whose `io_type` is `Virtual`. Find them, name them, and treat them like any other source:

```
uad_set_channels(items=[
  {"target": "<virtual index>",     "name": "REMOTE",  "hidden": false, "fader_db": 0},
  {"target": "<virtual index + 1>", "name": "MUSIC",   "hidden": false, "fader_db": -6}
])
uad_peek_meters(targets=["REMOTE","MUSIC"])
```

**Which application feeds which virtual channel is set in Console and in the operating system's
audio settings, not here.** The routing matrix (`/inputroutes`, `/outputroutes`) can be inspected
with `uad_raw_get` but cannot be re-patched by this server. Hand that step to the user, then prove it
worked with `uad_peek_meters` while they play something.

## Headphone feeds for the hosts

Each host hears the other hosts, the remote guest and the music, through a cue bus:

```
uad_set_sends(items=[
  {"target": "HOST",   "send": "cue1", "level_db": -8},
  {"target": "GUEST",  "send": "cue1", "level_db": -8},
  {"target": "REMOTE", "send": "cue1", "level_db": -10},
  {"target": "MUSIC",  "send": "cue1", "level_db": -16},

  {"target": "HOST",   "send": "cue2", "level_db": -8},
  {"target": "GUEST",  "send": "cue2", "level_db": -8},
  {"target": "REMOTE", "send": "cue2", "level_db": -10},
  {"target": "MUSIC",  "send": "cue2", "level_db": -16}
])
```

**The headphone output level itself cannot be set by this protocol.** Build the balance with these
sends; the physical knob does the final trim.

## Avoiding echo for the remote guest

A remote guest must not hear themselves come back. The mix sent to the call application must contain
the local mics and the music, but **not** the `REMOTE` channel. That separation is made in the
Console routing and in the call application's device selection, which this server cannot change.
State the requirement clearly and let the user set it, then test by asking the guest whether they
hear an echo.

## Streaming: keep the control room out of the feedback loop

Open mics plus speakers in the same room is a feedback path. For a live stream:

- Hosts on headphones, monitors muted, is the safe configuration:
  `uad_set_monitor(mute=true)`.
- If the monitors must be up, the mics need to be well away from them and the level kept low. Do not
  raise the monitor level to test this. The server rails it anyway (default ceiling -6 dB, +6 dB per
  call, +10 dB per 30 s) and an unmute onto an above-ceiling stored level is refused.
- **If it squeals, `uad_safe_state()` first.** It mutes every monitor output and turns talkback off,
  takes no arguments and needs no confirmation. Diagnose afterwards.

## Mute discipline during a recording

Muting a channel mid-show is a normal request and is safe:

```
uad_set_channels(items=[{"target": "GUEST", "mute": true}])
uad_set_channels(items=[{"target": "GUEST", "mute": false}])
```

Solo is not a mute. Soloing one channel changes what everyone hears and is almost never wanted
during a live show. If `solo` is set anywhere unexpectedly, `uad_list_channels` shows it.

## Lock it in

```
uad_snapshot(action="save", name="podcast-2mic")
```

Then remind the user to press Cmd+S in Console: the server cannot save a Console session, and a
snapshot is not a substitute for one.
