# Multiple Apollos in one rig

## What a cascade is

Thunderbolt Apollos cascade: up to four units join into **one** Console mixer. The user does not
get several mixers, they get one mixer with more strips. Every tool in this server sees all of
them at once.

```
uad_get_status()
```

`devices` is a list. Each entry carries `device` (the index used as the `device` selector),
`name`, `type_id`, `online`, `multi_unit_order` (position in the chain) and `cue_buses`.

## Clocking between cascaded Apollos

**Handled over Thunderbolt. No word clock cables between Apollos are required.** Do not send the
user hunting for BNC cables to sync one Apollo to another.

External digital gear is a separate matter: an ADAT expander or S/PDIF box still needs the clock
decision in `clocking.md`, and the Apollo-side `ClockSource` is a single system-wide setting at
the root of the tree, not per unit.

## Monitors

**Monitors connect to one designated monitor unit.** Ask the user which unit the speakers are
physically plugged into, then confirm it from the rig rather than assuming:

```
uad_list_channels(kind="output")
```

The monitor output is the strip with `is_monitor: true`. The server identifies it by the presence
of a `CRMonitorLevel` property, because the output index differs per model. Never hardcode an
index.

`uad_set_monitor` takes an optional `device`. With more than one unit, pass it explicitly:

```
uad_set_monitor(device=0, level_db=-20)
```

## Expanders

An ADAT expander can feed **any** unit in the cascade, through that unit's own optical ADAT IN.
The expander's channels then appear as inputs on that unit, so the `device` selector for those
channels is the unit the optical cable lands on, not unit 0 by default.

## Device selectors and name collisions

Channel names are not unique across units. Two units straight out of the box both have a channel
called something like "Mic 1". Name resolution across a cascade therefore has three outcomes:

| Outcome | What to do |
|---|---|
| Exact single match | proceed |
| Ambiguous | the error lists the candidates with their device and index. Re-call with `device=N` |
| Not found | the error lists close names. Re-read `uad_list_channels` and pick from it |

Every channel-facing tool accepts `device`: `uad_set_channels` (per item), `uad_set_preamp`,
`uad_set_sends` (per item), `uad_load_plugin`, `uad_remove_plugin`, `uad_get_plugin_params`,
`uad_set_plugin_params`, `uad_plugin_preset`, `uad_get_inserts`, `uad_list_channels`,
`uad_peek_meters`, `uad_watch_meters`, `uad_set_monitor`.

**The single best fix for a cascade is naming.** Rename strips so they are unique and physical,
then targets stop being ambiguous:

```
uad_set_channels(items=[
  {"target": 1, "device": 0, "name": "A VOX"},
  {"target": 1, "device": 1, "name": "B DRUM OH L"}
])
```

## Reading device health

`uad_get_status` reports two different kinds of connected:

- `engine.connected`: this server's TCP link to the UA Mixer Engine.
- `devices[].online`: whether that particular Apollo is present and awake.

A unit that is powered down or unplugged from the Thunderbolt chain shows `online: false` while
the engine stays connected. Its strips may still be listed. Say which unit is offline instead of
reporting a generic failure.

## Snapshots across a cascade

`uad_snapshot(action="save", ...)` walks every device in the cascade, so one snapshot covers the
whole rig. Restore refuses across mismatched models (`DeviceTypeID` mismatch) unless
`allow_cross_model=true`, which then maps channels by I/O type and ordinal rather than raw index.
Prefer to fix the rig rather than to force a cross-model restore.
