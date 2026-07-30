# Worked example: Apollo x8 Gen 2 plus Behringer Ultragain ADA8200

A real session on a live Apollo x8 Gen 2 (UA Mixer Engine, macOS). This is the canonical shape of
an ADAT expander fault, so follow it whenever eight new digital channels show nothing.

## The rig

- Apollo x8 Gen 2 over Thunderbolt.
- Behringer Ultragain ADA8200, eight mic preamps with an ADAT output, patched into the Apollo's
  optical **ADAT 1** input.
- One optical cable: ADA8200 ADAT OUT to Apollo ADAT IN.

## The symptom

The eight ADAT channels existed in Console and could be read by the server, but every one of them
sat at the meter floor while the ADA8200's own input meters showed signal.

```
uad_peek_meters(kind="input")
```

All eight ADAT strips reported the device-silence floor (-77.0 dB) and were flagged as at-floor.

**Why this is not "the source is quiet":** a meter at exactly the floor cannot distinguish silence
from a dead digital link. On an ADAT input, a whole block of channels pinned at the floor is a
clock or cabling fault until proven otherwise.

## The diagnosis

```
uad_get_status()
```

- `clock.source`: `Internal`
- `clock.locked`: true
- `clock.sample_rate`: 48000

`Internal` means the Apollo is the clock master. With only one optical cable in the rig, the clock
travels from the ADA8200 to the Apollo and nothing travels back. The ADA8200 was therefore still
running on its own internal clock while the Apollo insisted on being master. Two masters, no
usable link, eight channels of nothing.

The Apollo reported `locked: true` because it was locked to its own internal clock. **`locked`
being true does not mean the external box is in sync.** That is the trap in this fault.

## The fix

Two physical changes, both on the user's side:

1. **Add a return optical cable: Apollo ADAT OUT to ADA8200 ADAT IN.** This is the only path by
   which the Apollo's clock can reach the expander.
2. **Set the ADA8200's rear-panel SYNC switch to slave from ADAT IN.** Until that switch moves,
   the expander ignores the incoming clock and keeps using its own.

The alternative, had the user preferred a single cable, was to make the expander the master
instead: `uad_set_clock(source="ADAT", confirm=true)` with the ADA8200 left on internal. Either
arrangement works. Mixing them does not.

## The verification

```
uad_get_status()
uad_watch_meters(targets=["UG 1","UG 2","UG 3","UG 4"], duration_s=10, threshold_db=-60)
```

Signal appeared at **-17.8 dB**, which is close to the useful average target of about -18 dBFS.
That single non-floor reading is the proof the link is alive: everything else is inference.

## The tidy-up

The eight expander channels were renamed UG 1 through UG 8 so they could be targeted by name
afterwards:

```
uad_set_channels(items=[
  {"target": 8,  "name": "UG 1", "hidden": false, "enabled": true},
  {"target": 9,  "name": "UG 2", "hidden": false, "enabled": true},
  {"target": 10, "name": "UG 3", "hidden": false, "enabled": true},
  {"target": 11, "name": "UG 4", "hidden": false, "enabled": true},
  {"target": 12, "name": "UG 5", "hidden": false, "enabled": true},
  {"target": 13, "name": "UG 6", "hidden": false, "enabled": true},
  {"target": 14, "name": "UG 7", "hidden": false, "enabled": true},
  {"target": 15, "name": "UG 8", "hidden": false, "enabled": true}
])
uad_snapshot(action="save", name="adat-expander")
```

**Indexes 8 to 15 were the ADAT block on that particular unit.** Do not carry those numbers to
another Apollo. Always read `uad_list_channels(kind="input")` and pick the strips whose `io_type`
is `ADAT`.

Finally the user was reminded to press Cmd+S in Console, because the server cannot save a Console
session.

## Lessons to reuse

1. A block of digital channels at the meter floor is a clock or cable fault, not silence.
2. `clock.locked: true` only says the Apollo is locked to whatever it was told to follow. It says
   nothing about the box on the other end of the cable.
3. `ClockSource: Internal` plus one optical cable is an unfinished chain. Either add the return
   cable and slave the expander, or make the expander the master.
4. The expander's own sync switch is part of the fix. The MCP server cannot reach it, so hand that
   step to the user explicitly, naming the switch and its position.
5. Rename the channels the moment they work, then snapshot. Nameless strips make every later
   instruction ambiguous.
6. The ADA8200 tops out at 48 kHz, so this rig cannot use its eight channels at higher rates
   anyway. Check the expander's own ceiling before promising a rate.
