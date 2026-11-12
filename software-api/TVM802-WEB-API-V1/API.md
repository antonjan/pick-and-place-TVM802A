# TVM802 Web API — endpoint reference

Base URL: `http://<server-host>/` (the Node server listens on port 80).
Every endpoint is a plain HTTP **GET**. Command endpoints accept exactly **one**
query parameter per request and reply with JSON `{"result": 1}` on success or
`{"result": -1}` for an unknown/invalid parameter. Commands are fire-and-forget:
the HTTP reply confirms the frame was sent to the machine, not that motion has
finished — poll `/api/status` to see the result.

---

## Machine status

```
GET /api/status
```

Returns the live state object (refreshed from the machine every ~200 ms):

| Field | Meaning |
|---|---|
| `Position.X` / `.Y` | Head position in **mm** (machine coordinates after homing) |
| `Position.Nozzle` | Nozzle height, steps ÷ 32808 (approximate — OEM uses a piecewise mapping) |
| `Position.A1` / `.A2` | Nozzle rotations, steps ÷ 4444.44 (unit unverified, likely degrees) |
| `PositionRaw.*` | Same five axes in raw controller steps |
| `Limit.Top` / `.Bottom` / `.Left` / `.Right` | Limit switch states, `1` = triggered |
| `VacuumPump` | Main vacuum pump on/off |
| `Vacuum1` / `Vacuum2` | Per-nozzle vacuum valves |
| `Blowing1` / `Blowing2` | Per-nozzle blow-off valves |
| `Pressure1` / `Pressure2` | Per-nozzle pressure sensor flags (polarity unverified — check while holding a part) |
| `Buzzer`, `Prick`, `Leds` | Output states |
| `SpeedMode` | `"Slow"` or `"Fast"` (read-only; no API to change it yet) |
| `last_reply` | Unused — never updated by the current code |

Conversion constant for X/Y: **32 808 steps per mm**.

---

## Switched outputs

```
GET /api/set?<output>=1      (on)
GET /api/set?<output>=0      (off — any value other than 1 switches off)
```

| Parameter | Controls |
|---|---|
| `pump` | Main vacuum pump |
| `vacuum1` / `vacuum2` | Vacuum valve, nozzle 1 / 2 (pick & hold) |
| `blowing1` / `blowing2` | Blow-off valve, nozzle 1 / 2 (release on place) |
| `leds` | Up-facing camera LED ring |
| `buzzer` | Buzzer |
| `prick` | Pricker / needle actuator |

---

## Motion

```
GET /api/move?StepX=<mm>     relative X jog, e.g. StepX=10, StepX=-2.5
GET /api/move?StepY=<mm>     relative Y jog
GET /api/move?StopAll=1      immediate stop of X, Y, Z and A channels
```

Jogs are **relative** to the current position, decimals allowed, negative
values allowed. Targets are clamped at raw position 0, so a negative jog does
nothing until the axis has moved into positive territory (i.e. home first).

```
GET /api/home                homes Y, then X
GET /api/home?axis=X         home a single axis
GET /api/home?axis=Y
```

Homing sequence per axis: drive toward the limit switch (the controller halts
the axis when the switch trips) → back off 5 mm → re-approach for a repeatable
trip point → write the switch's machine coordinate into the position register.
After homing, coordinates are absolute machine coordinates: the Y switch sits
at **351.50 mm**, the X switch at **300.02 mm** (X value inferred from a
captured OEM frame — tune `homeRegister` in `TVM-manager.js` if offset).
Progress and failures are reported on the server console; on any failure the
axis is stopped. Completion can be detected by polling `/api/status` until
`PositionRaw` stops changing.

---

## Not implemented yet (relevant for OpenPnP)

- Absolute `moveTo(x, y)` — only relative jogs are exposed so far
- Z / nozzle up-down motion
- A1 / A2 nozzle rotation
- Speed mode selection

The underlying protocol supports all of these — the frame layout is already
mapped in `TVM-manager.js` (channel masks: Z = `0x02`, A1 = `0x08`,
A2 = `0x20`, with matching 4-byte position slots) — they just need endpoints.

## OpenPnP integration notes

OpenPnP has no built-in driver that speaks this HTTP API. Two viable routes:

1. **Custom `ReferenceDriver`** (Java) calling these endpoints — the method
   list to implement is in the repo's `openPNPNotes.md` (`home`, `moveTo`,
   `pick`, `place`, `setEnabled`, `actuate`).
2. **A small bridge** that accepts G-code from OpenPnP's `GcodeDriver` and
   translates it to these HTTP calls.

Either way the switched outputs map directly onto OpenPnP actuators
(vacuum/blow per nozzle, pump, LEDs), `pick`/`place` are vacuum + blow
sequences, and "move complete" should be implemented by polling
`/api/status` until the position settles.
