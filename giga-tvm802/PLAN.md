# TVM802 Pick-and-Place: Plan

A standalone Linux app that drives a TVM802 directly over its network protocol, with no OpenPnP and no G-code bridge. Python backend, browser UI.

**Decisions so far**

| Topic | Decision |
|---|---|
| Stack | Python 3.12, asyncio, FastAPI + WebSocket; browser UI |
| Machine link | TCP to the TVM802 controller (`192.168.0.8:701`), protocol ported from `TVM802-WEB-API-master/TVM-manager.js` |
| Cameras | 2× USB UVC (`/dev/videoN`) via OpenCV/V4L2: down-looking (head) and up-looking (bottom) |
| Vision v1 | Fiducial board alignment + bottom vision (part offset/rotation on nozzle) |
| Input | KiCad `.pos` files (CSV export too) |
| Feeders | Stock TVM802 strip feeders, advanced with the prick needle |

---

## 1. What the existing code tells us

### 1.1 Protocol facts (from `TVM-manager.js`)

All multi-byte values are **little-endian int32**.

| Purpose | Frame |
|---|---|
| Connect | TCP `192.168.0.8:701` |
| Init sequence (on connect) | `05 00 00 00 00 00 00 00`, `15 00 00 00 08 00 00 00`, `14 00 00 00 00 01 00 00`, `14 00 00 00 00 02 00 00`, `0b 00 00 00`, `15 00 00 00`, `48 00 00 00` |
| Request state | `00 00 00 00` → **44-byte reply** starting `0x00` |
| Poll (meaning unknown) | `01 00 00 00 f4 01 00 00 00 00 00 00` (reply type `0x01` is currently ignored) |
| Output ON / OFF | `0x14` / `0x15` + 3 zero bytes + 4-byte bitmask (bytes 4..7) |
| Absolute move | 28 bytes: `[0]=0x08`, `[2]=axis selector mask`, int32 target steps at `4 + slot*4` |
| Set position register | Same layout with `[0]=0x07` (used for homing and zeroing A) |
| Stop axes | `09 00 <mask> 00` (`0x3f` = all/"stop on limit") |
| Speed setup | `0x06` + 76-byte opaque blob, sent before every move (contents not decoded) |

**Confirmed on the real machine (2026-11-12): request/response, one command per TCP read**

The controller handles only the first command in each TCP read and silently drops the rest. The Node bridge (and therefore OpenPnP) lost commands whenever writes got coalesced. The transport now sends one command and waits for its reply before sending the next.

| Sent | Reply |
|---|---|
| (connect) | `4f 4b 0d 0a` = `OK\r\n` |
| `05 …` | `05 00 03 00` (firmware version 3?) |
| `06` speed, `07` set pos, `09` stop, `0b`, `14`, `15` (incl. the 4-byte `15 00 00 00`) | `<cmd> 00` |
| `08` move | assumed `08 00` (not yet observed) |
| `01` poll | 12 bytes: `01 00 00 00` + outputs u32 + 4 unknown bytes (zero while idle) |
| `48` | no reply |
| `00` state | 44 bytes |

**Motion resolution (measured):** the controller moves in whole multiples of **1000 steps** of *distance*, rounding the requested distance **up**, e.g. 0 → 32808 ends at 33000, 8000 → 12345 ends at 13000. This applies to X, Y, Z and A, so the resolution is 0.0305 mm and 0.225°. `Machine` now snaps every target to a whole number of quanta from the current position (`motion.step_quantum`), and the simulator rounds the same way. A 1 mm jog takes about 0.2 s.

**Limits and homing (confirmed 2026-11-12):** the controller refuses moves *toward* a pressed limit switch. After power-on the head sat on the Right switch, which releases about 3 mm to the left. X+ = right (Right switch), Y+ = toward the Top switch. Homing now accepts only the matching switch and aborts on any other. The first real home took 2.4 s and gave X = 300.018, Y = 351.500, with both switches pressed at the home corner.

Idle state seen after power-on: outputs `0x3e0` (byte 4 `0xe0` always set → `speed_mode` reads "?"), byte 8 `0xf1` (Right limit bit set), bytes 9–11 `ff ff 03` unknown.

**Axis table (move frame)**

| Axis | Selector | Slot (byte offset) | Scale |
|---|---|---|---|
| Z seesaw ("Nozzle") | `0x02` | 1 (8) | 32808 steps/mm; **+ = nozzle 1 down, − = nozzle 2 down** |
| X | `0x04` | 2 (12) | 32808 steps/mm |
| A1 (N1 rotate) | `0x08` | 3 (16) | 4444.44 steps/° |
| Y | `0x10` | 4 (20) | 32808 steps/mm |
| A2 (N2 rotate) | `0x20` | 5 (24) | 4444.44 steps/° |

**Output bitmask (bytes 4..5 of the 0x14/0x15 frame; the state reply mirrors it)**

| Output | byte 4 | byte 5 |
|---|---|---|
| Blow 1 | `0x02` | |
| Vacuum 1 | `0x04` | |
| Pump | `0x08` | `0x04` |
| Up-cam LEDs | | `0x02` |
| Buzzer | | `0x08` |
| Blow 2 | | `0x10` |
| Vacuum 2 | | `0x20` |
| Prick needle | | `0x40` |
| Speed mode | `0x80` = fast | `0x01` = slow |

**State reply (44 bytes)**

- byte 8: limits (`0x01` right, `0x02` left, `0x04` top, `0x08` bottom), `0x10` = **no** pressure on N1 (active-low)
- byte 10: `0x10` = **no** pressure on N2
- bytes 16..19 Z (signed), 20..23 X, 24..27 A1, 28..31 Y, 32..35 A2

**Homing:** retract Z to 0 → zero A1/A2 registers → `09 00 3f 00`, `0b…`, two `0x15` mask writes, speed setup → drive axis toward +1000 mm until limit trips → back off 5 mm → re-approach → set the register to the home value (X = 9 843 000 steps ≈ 300.0 mm, Y = 11 532 000 ≈ 351.5 mm). Y homes first, then X. So home is at the **max** corner and the working area is 0..~300 × 0..~351 mm.

### 1.2 Why the OpenPnP integration hurts (things to fix in the port)

1. **No motion-complete signal.** `TVM-API-Server.js` answers `ok` to G0/G1/G28 the moment the frame is sent, so OpenPnP takes camera frames and fires vacuum while the head is still moving. `TVM-TCP-Server.js` tries position polling, but only for X/Y/Z, with no timeout and a 200 ms state poll.
2. **TCP framing.** `processMessage` assumes one `data` event = one message. Coalesced or split replies are silently dropped (`length !== 44`).
3. **Signedness.** X/Y/A positions are parsed unsigned; anything slightly negative becomes ~4 billion.
4. **Rotation wrap.** Absolute A targets are folded into 0..360°, so 350° → 10° spins the long way, and the position read back no longer matches what the controller was told.
5. **Z1/Z2 collision.** `Z` and `Z2` both write the single seesaw axis; a command naming both silently overwrites.
6. **No reconnect, no soft limits, no E-stop path**, `new Buffer` deprecated, the speed blob is resent on every move, and `M114` reports A1 under both `A` and `C`.

The new app owns the whole loop (motion → wait → vacuum → vision), so these become direct, awaitable calls instead of G-code guesswork.

---

## 2. Architecture

```
┌──────────────── Browser UI (served by the app) ────────────────┐
│  Machine (DRO, jog, actuators, cams) · Setup wizards · Job run │
└───────────────▲──────────── REST + WebSocket + MJPEG ──────────┘
                │
┌───────────────┴──────── FastAPI app (pnp/api) ─────────────────┐
│  job runner ─ planner ─ feeders ─ vision ─ calibration         │
│                     │                                          │
│               Machine (pnp/machine)  ── awaitable motion, I/O,  │
│                     │                    homing, safety         │
│               Protocol + Transport (pnp/tvm) ── frames, framing │
└─────────────────────┼──────────────────────────────────────────┘
                      │ TCP :701
          TVM802 controller   or   pnp/sim (simulator)
```

### 2.1 Repository layout

```
PnP-Software/
  PLAN.md
  pyproject.toml            # fastapi, uvicorn, opencv-python, numpy, pydantic, pyyaml, pytest
  config/
    machine.yaml            # IP, steps/mm, limits, speeds, home values, Z heights, safe Z
    cameras.yaml            # device paths, resolution, calibration results
    nozzles.yaml            # nozzle tips, offsets
    feeders.yaml            # feeder slots, positions, pitch, feed moves
    packages.yaml           # package → height, nozzle, vision params
  pnp/
    tvm/        protocol.py (encode/decode, pure, unit-tested)  transport.py (asyncio, framing, reconnect)
    machine/    machine.py  motion.py  homing.py  actuators.py  safety.py
    sim/        tvm_sim.py  (fake controller on 127.0.0.1:701 that integrates moves over time)
    vision/     camera.py  calibration.py  fiducial.py  bottom.py
    job/        kicad_import.py  board.py  planner.py  runner.py  feeders.py
    api/        app.py  routes_*.py  ws.py
    web/        index.html, app.js, styles.css  (no build step)
  tests/
    captures/   # real byte captures from the machine (Wireshark exports)
```

### 2.2 Protocol and transport (`pnp/tvm`)

- `protocol.py`: pure functions only, e.g. `encode_move({axis: steps})`, `encode_set_register`, `encode_outputs(on/off, mask)`, `encode_stop(mask)`, `decode_state(bytes) -> State`. Signed int32 LE throughout. Unit-tested against the byte strings from `TVM-manager.js` and real captures.
- `transport.py`: one asyncio TCP connection, a **stream reassembler** (accumulate bytes, cut frames by type/length), serialized writes, auto-reconnect that replays the init sequence, and a status poller (aim for 20–50 ms while moving, ~200 ms idle).

### 2.3 Machine layer (`pnp/machine`)

Everything in mm and degrees, and everything awaitable:

- `await move_to(x=, y=, a1=, a2=, z=, speed=)`: rejects targets outside soft limits, sends the frame, then **waits for completion** = every commanded axis within ±N steps of its target and unchanged for 3 consecutive polls, with a timeout scaled by distance. On timeout: stop all axes and raise.
- `await move_safe(x, y)`: raise Z to safe height, rotate if needed, move XY, never diagonal with a nozzle down.
- `await nozzle_down(n, depth)` / `nozzle_up()`: wrap the seesaw (N1 = +, N2 = −). The API never lets both nozzles be "down".
- Rotation: track **unwrapped** angles internally and choose the shortest path; re-zero the A register occasionally so it can't overflow.
- `await home()`: port of the existing routine, made fully awaitable, with failure reporting and Y-then-X order.
- Actuators: `vacuum(n, on)`, `blow(n, on)`, `pump(on)`, `prick(on)`, `leds(on)`, `buzzer(on)`, plus `has_part(n)` from the pressure bits.
- Safety: E-stop button in the UI → `stop all` + vacuum off; limit-switch trips during normal moves abort the job; nothing moves until homed; watchdog when the controller connection drops.

### 2.4 Simulator (`pnp/sim`)

A fake controller on `127.0.0.1:701` that understands the frames above, integrates position at a fixed speed, emits 44-byte state replies, trips limits at the travel ends and fakes the pressure sensor when vacuum is on over a "part". It allows all development and CI without the machine, and doubles as a protocol regression test.

### 2.5 Vision (`pnp/vision`)

- **Cameras:** OpenCV `VideoCapture(path, cv2.CAP_V4L2)` with MJPG at a fixed resolution, using `/dev/v4l/by-id/...` paths so the two cameras don't swap on reboot. Each camera has a background grab thread, keeps only the latest frame, and serves MJPEG to the UI. Settling: discard frames until N ms after motion completes.
- **Calibration wizards:**
  1. *Units per pixel*: jog the down camera over a known feature, move ±X/±Y, track it, solve the 2×2 pixel→mm matrix (covers rotation of the camera too).
  2. *Down camera → nozzle offsets*: place a mark with N1 (or touch a target), find it with the camera. Repeat for N2.
  3. *Up camera position*: center N1 tip over the up camera, record XY. Repeat for N2. Also nozzle runout: rotate 0..360° and fit a circle; store the eccentricity for compensation (later phase).
  4. Optional: lens distortion with a checkerboard (`cv2.calibrateCamera`).
- **Fiducials:** move the down camera to the nominal position (board origin + KiCad fiducial coords), detect a circle (HoughCircles, then sub-pixel centroid of the thresholded blob), iterate once to center, record the actual position. With 2 fiducials: solve translation + rotation (+ uniform scale check). With 3: full affine. Flag a scale error above 0.2% as "wrong fiducial".
- **Bottom vision:** after picking, move the part over the up camera at a fixed Z, LEDs on, threshold, take the largest contour, `cv2.minAreaRect` → (dx, dy, dθ). Check the size against the package dimensions (wrong part or tombstoned pick → reject and discard). Apply the correction at placement: rotate by −dθ, offset XY by the rotated (dx, dy). Per-package threshold and ROI live in `packages.yaml`.

### 2.6 Job layer (`pnp/job`)

- **KiCad import:** parse `.pos` (ASCII or CSV export): Ref, Val, Package, PosX, PosY, Rot, Side. Ignore bottom-side parts or warn about them, and let the user mark parts DNP/skip. Fiducials (`FID*`) are auto-detected and used for alignment, not placed.
- **Board model:** origin + rotation set either manually (jog the camera to two known points) or from fiducials. Transform = KiCad coordinates (mirrored Y axis, as KiCad's Y points down in some exports) → machine coordinates.
- **Packages:** map `Value + Package` → package definition (height, nozzle tip, vision settings, pick/place speed). Unknown packages block the job until defined.
- **Strip feeders:** each feeder slot stores the first pocket position, pitch (2/4/8 mm), tape direction vector, part rotation in tape, pick Z, and the **feed move** for the prick needle: go to the feed hole, `prick down`, drag by pitch, `prick up`. Optional: a down-camera check of the sprocket hole to correct drift (later phase). A part counter per feeder plus a "parts remaining" warning.
- **Planner:** group placements by feeder, pick on **both nozzles** (N1 and N2) per cycle where both parts are compatible, order placements nearest-neighbour, and put larger/taller parts last.
- **Runner:** a state machine `IDLE → HOMED → ALIGNING → (FEED → PICK → CHECK_VACUUM → BOTTOM_VISION → PLACE)* → DONE`, with **Pause, Step, Skip part, Retry, Abort**. On mis-pick: up to N retries, then discard into a bin location and pause. Progress is saved to disk so a job can be resumed after a crash or E-stop.

### 2.7 API and UI

- REST: `/machine/*` (home, move, jog, actuators, stop), `/cameras/{id}/stream.mjpg`, `/setup/*` (calibration steps, feeders, packages), `/job/*` (import, align, run/pause/step/abort).
- WebSocket `/ws`: live DRO, I/O state, limits, job progress, and log lines.
- UI (plain HTML/JS, no build tooling), three tabs:
  - **Machine**: DRO, jog pad with step sizes, Z/rotation controls, actuator toggles, both camera views with a crosshair, click-on-image-to-move, E-stop.
  - **Setup**: calibration wizards, nozzle offsets, feeder editor ("teach" = jog the camera to the pocket, then click *Set*), package editor.
  - **Job**: import `.pos`, placement table (status per part), board overlay on the camera, feeder assignment, Run/Pause/Step/Abort.

---

## 3. Phases

Each phase ends in something usable on the real machine.

| # | Phase | Done when |
|---|---|---|
| 0 | **Protocol port + simulator** | `protocol.py` passes tests on known frames; simulator responds; `transport.py` reconnects and reassembles frames |
| 1 | **Machine core + manual control UI** | Home, jog, absolute moves with real completion detection, Z1/Z2, rotation, all actuators, vacuum sensing, E-stop, soft limits, all working from the browser on the real machine |
| 2 | **Cameras + calibration** | Both UVC cameras streaming; units-per-pixel, camera→nozzle offsets and up-camera position calibrated and saved |
| 3 | **Feeders + manual pick/place** | Teach a strip feeder; button "pick from feeder 3 with N1 and place at camera crosshair" works repeatably, tape feeding included |
| 4 | **KiCad job, blind placement** | Import `.pos`, set board origin by jogging, place a full job of 0805s without vision correction |
| 5 | **Fiducials** | Board alignment from 2–3 fiducials; placement error dominated by pick error, not board offset |
| 6 | **Bottom vision** | Per-part offset/rotation correction; mis-pick detection by part size; SOIC/QFN place correctly |
| 7 | **Polish** | Speed profiles per package, job resume, nozzle runout compensation, feeder hole vision, logs/stats |

### Hardware-in-the-loop safety checklist (before phase 1 on the real machine)

- First real-machine moves at slow speed, small distances, with a hand on the power switch.
- Z moves only to safe height until the nozzle depths are measured.
- Verify the sign of every axis and the Z seesaw direction against the DRO before any automated move.
- Confirm that homing failure stops all motion.

---

## 4. Open questions / things to find out

These don't block phase 0 but need answers before the listed phase.

1. **Busy/idle bit (phase 1):** is there a "moving" flag in the 44-byte state reply or in the `0x01` poll reply? A Wireshark capture of the vendor software during a move would settle it, and would make completion detection more robust than position matching. Do you have captures (`WireSharkFilter.txt` suggests you did some)?
2. **Speed blob (phase 1/7):** the 76-byte `0x06` frame looks like 12 × int32 values plus 6 × int32 speeds (`0x0fa0` = 4000, `0x0bb8` = 3000, `0x1388` = 5000). Capturing the vendor software at different speed settings would let us expose real speed/acceleration control.
3. **Travel and Z (phase 1):** usable X/Y travel, the safe-Z position, and how deep each nozzle goes in mm, for pick height and board height.
4. **Strip feeders (phase 3):** how many feeder lanes and of which widths (8/12/16 mm)? Does the stock software feed by poking the needle into the **component pocket** or the **sprocket hole**, and how far does it drag? A short phone video of the stock software feeding would answer it.
   - *Answered 2026-11-12:* the stock software feeds with the prick needle. 26 stock lanes, but the machine is modified with extra stacks, and the back stack is swapped for different tape sizes per job. So lane sizes and positions must be user-defined in the app, not hard-coded. The needle goes into a **sprocket hole**, drags the tape, then lifts and releases. Each feed drags the tape **one part pitch** (confirmed).
   - *Needle point (2026-11-12):* a pocket centre is 2 mm along the tape from the nearest sprocket hole (EIA-481 P2), so generated lanes put the needle on the hole 2 mm upstream, not level with the pocket. **Open: 2 mm pitch tape (0402/0201).** Holes are 4 mm apart, so after a 2 mm drag no hole is under the needle point any more. Find out how the stock software feeds 2 mm pitch (e.g. a 4 mm drag every second part, alternating needle points, or no support at all).
   - *Feeder banks (agreed design):* both a **back stack** and an added **front stack** are swapped for different tape sizes per job. Each stack is a bank with its own saved, named **layouts** (position, lane count, lane widths/positions, feed direction). Loading a layout replaces that bank's lanes without re-teaching each one.
5. **Machine IP:** is `192.168.0.8` fixed? It will be configurable either way.
6. **Cameras (phase 2):** exact camera models and resolution; does the up camera need a specific exposure setting (v4l2-ctl manual exposure)?
7. **Nozzles (phase 6):** which nozzle tips are fitted on N1/N2, and do you swap them manually during a job (the TVM802 has no auto tool changer)?
   - *Answered 2026-11-12:* no tip swaps. The two nozzles carry fixed tips: **N1 = thick**, **N2 = thin**, and only **one nozzle is used per part**: one component per trip, never two picked at once. So the job runner chooses N1 or N2 per part (by package or a per-part setting), with no dual-nozzle cycles. This replaces the "two-nozzle cycles" item in phase 7.
8. **Panels (later):** single boards only, or panelized jobs too?
   - *Answered 2026-11-12:* panels wanted. Plan for a job made of a board plus a panel layout (rows × columns with a step, or a list of board origins/rotations), and per-board skip ("bad board") support.

---

## 5. Progress

### Phase 0: done (2026-11-12)

- `pnp/tvm/protocol.py`: encoders and decoders, signed positions, and a stream reassembler for split or merged replies.
- `pnp/tvm/transport.py`: asyncio link with serialised writes, state polling, auto-reconnect (replays the init sequence), and `request_state()` / `wait_state()`.
- `pnp/sim/`: simulated controller with constant-speed motion, separate physical position and register (so homing behaves like the real machine), X/Y limit switches, and vacuum pick → pressure sensor.
- `pnp/tvm/monitor.py`: read-only live state printer for a first check on the real machine.
- 31 tests: frames checked against the Node bridge and vendor captures in `backup.js`, plus transport↔simulator integration (move, homing pattern, stop, pick, reconnect).

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/python -m pytest
.venv/bin/tvm-sim --port 7001                              # simulator
.venv/bin/tvm-monitor --host 127.0.0.1 --port 7001         # watch the simulator
.venv/bin/tvm-monitor --unknown                            # real machine (192.168.0.8:701)
```

Known gaps carried forward:
- Only the 44-byte state reply length is known. Any other reply type is flushed as an "unknown" frame (`tvm-monitor --unknown` prints them). Add each length to `REPLY_LENGTHS` once identified.
- The init sequence has a 4-byte `15 00 00 00` frame. It is sent verbatim; the simulator special-cases it.

### Phase 1: done in the simulator (2026-11-12), not yet run on the machine

- `config/machine.yaml` + `pnp/config.py`: typed config; typos in keys are rejected.
- `pnp/machine/machine.py`:
  - **Moves:** `move_to`, `jog`, `nozzle_down/nozzles_up`, `rotate` (shortest path, unwrapped angles), `home`, `set_output`, `has_part`, `estop/reset`.
  - **Waiting for arrival:** a move returns only when every axis it moved is within 2 steps of its target and has reported the same position 3 times in a row. State is polled every 30 ms while moving.
  - **Safety:** soft limits (X/Y only after homing); X/Y moves raise both nozzles first; X/Y jogs of up to 10 mm are allowed before homing; a limit switch that triggers mid-move stops everything; a move that runs too long stops everything; E-stop interrupts a running move and switches off vacuum, pump, blow and prick; one operation at a time; losing the connection clears "homed".
- `pnp/api`: FastAPI REST (`/api/state|home|move|jog|nozzle/down|nozzle/up|rotate|output|estop|reset`) plus a `/ws` state stream.
- `pnp/web`: Machine tab with position readout, sensors, go-to, home, jog pad, nozzle depth/rotation, output toggles, log and E-stop. Keys: arrows, PgUp/PgDn, Esc.
- 47 tests in total (machine scenarios and API flow against the simulator).

```bash
.venv/bin/python -m pnp.api --sim      # UI on http://127.0.0.1:8080 with simulator
.venv/bin/python -m pnp.api            # real machine from config/machine.yaml
```

**First real-machine session: done 2026-11-12.** Connection (direct Ethernet, PC 192.168.0.100 on `enp2s0`), init, jogging on every axis, homing (2.4 s), and both nozzles down to 7.803 mm and back up all work through the app and the web UI. Saved in `machine.yaml`: `z1_max`/`z2_max` = 7.803. Still open: the front/left travel ends (the Left/Bottom switches protect them meanwhile), the real XY speed (timeouts still assume 25 mm/s), and the vendor speed blob (speed readout shows "?" because byte 4 `0x80` is always set).

Original checklist, kept for reference:
1. `tvm-pnp`, then check the position readout while moving the head by hand or with the vendor software: are X+ right and Y+ toward the Top switch?
2. Jog 1 mm in X and Y before homing. Check the direction and that the readout changes by exactly 1.000.
3. Try Home with a hand on the power switch. If the Y or X switch bits are different on the real machine, adjust `HOME_SWITCH`.
4. Lower N1 and N2 by 0.5 mm steps to find the real `z1_max` / `z2_max`; jog toward front/left to find the X/Y minimums. Put all four in `machine.yaml`.
5. Time a long X move to set `motion.xy_speed` (timeouts are based on 25 mm/s for now).

### Phase 2: software done in the simulator (2026-11-12); real cameras blocked by hardware

Built mostly by Haiku agents against [docs/phase2-design.md](docs/phase2-design.md), then reviewed and fixed:
- `pnp/vision/camera.py`: threaded UVC/sim cameras (latest frame only, auto-reconnect), `config/cameras.yaml` (devices by serial, roles confirmed by the user: down/head = `…56215848`, up = `…11585848`; 640×480).
- `pnp/vision/calibration.py` and `calibration_store.py`: template-match scale calibration (2×2 px/mm matrix incl. rotation/mirror), blob centring, nozzle offsets, up-camera positions, saved to `config/calibration.yaml`. Review fixes: scale uses the *measured* head moves (1000-step quanta, otherwise a 0.6 % error), and centring stops at machine resolution.
- `pnp/sim/sim_vision.py`: synthetic down/up camera images with known ground truth.
- `pnp/api/vision_routes.py` + UI: live MJPEG views with crosshair and click-to-move, and a 4-step Setup wizard. Review fix: calibration positions are only recorded after homing.
- In the simulator the full wizard recovers every value to within one machine quantum (0.03 mm). 93 tests in total.

**Real cameras:** both Microsoft 045e:0294 cameras streamed at first (≈4–7 fps at 960×720, auto-exposure), then stopped responding within minutes (STREAMON EIO / control timeouts) while still enumerated on USB. Both sit directly on USB 2.0 bus 1 (ports 3/4) with autosuspend on, alongside the Wi-Fi dongle. Isolation test after a replug: the **up camera `…11585848` streams at 29.9 fps alone at 640×480**, while the **head camera `…56215848` hangs on open even alone on the bus** and afterwards also knocks the up camera out until replugged. Port-swap test: the head camera streams fine on port 3, so the **fault was the PC's USB port 4 (or the cabling to it)**, not the camera. With the head camera on port 3 and the up camera on port 10, both stream together at about 15 fps (auto-exposure), with 0 stalls in 3 minutes. Avoid port 4. The real-machine UI shows both live views. The down view is rotated 270° (90° anticlockwise); the user confirmed X+ pans right and Y+ pans up. Camera hangs, root cause narrowed down (13:25): the hung capture threads sit in kernel state D in `usb_start_wait_urb`, meaning the camera never answers a USB control request. Both cameras have USB autosuspend on (2 s). Countermeasures: `config/udev/90-tvm802-cameras.rules` (turns autosuspend off, installed with sudo), opening only one camera at a time, a 3 s settle delay after plug-in, and never opening a device that another process holds (the Claude desktop app's capture service probes freshly plugged cameras). **Result (13:36):** with the rule installed (`power/control=on`), both cameras reconnected cleanly after a replug, one after the other, and kept streaming at about 15 fps. Camera roles re-verified by motion and LED tests: `…56215848` moves with the head and `…11585848` reacts to the up LEDs. **Open:** focusing the head camera and the down-camera calibration are postponed (no suitable PCB at hand; sharpness only about 27). Printed text or a sheet of paper with a few sharp dots also gives the scale calibration enough texture. Measured travel ends: X −115 mm and Y −77 mm. Soft limits are now X [−113, 299] and Y [−75, 350.5]. On 2026-11-13 the user widened the X minimum to −120 (the head travels further left than first measured). Next: check focus (the head view is a blur), then run the Setup wizard on the machine.

### Phase 3: done in the simulator (2026-11-12), not yet on the machine

Built by Haiku agents against [docs/phase3-design.md](docs/phase3-design.md), then reviewed and fixed:
- `pnp/job/feeders.py`: strip feeders (the pick point advances one pitch per part, with a capacity count) and drag feeders (fixed pick point; the prick needle drags the tape one pitch). Stored in `config/feeders.yaml`.
- `pnp/job/head_ops.py`:
  - **Operations:** pick (feeds a drag feeder first, then checks the vacuum sensor; a failed pick does not advance the feeder), place (rotation relative to the tape, release, blow-off), discard, and pick and place.
  - **Safety:** one operation at a time; vacuum goes off and the nozzles go up on errors.
  - **Review fixes:** a nozzle that already holds a part is refused, and a place that fails before the release keeps the part instead of dropping it.
- **Prick needle offset:** added to the calibration, with a Setup step for it. `head:` section in `machine.yaml`: dwell times, blow time, place depth, discard bin.
- **Feeders tab:** teach by camera, feed, pick, place, pick and place at the crosshair, discard, and a "holding" display. The API refuses targets outside the soft limits before anything moves.
- **Simulator:** an event log (pick/release/needle) for the tests. `--sim` fills in missing calibration values and adds two example feeders. 135 tests in total.
- **Demo in the simulator through the UI:** strip F1 → N1 placed at the crosshair, and drag F2 → N2 (fed, picked, placed).

**Before the first real pick:** focus and calibrate the down camera (scale and nozzle offsets), calibrate the prick offset, set `head.discard`, and measure the real pick and place depths (both are 3.0 mm for now).

### Phase 4: done in the simulator (2026-11-12), not yet on the machine

Built by Haiku agents against [docs/phase4-design.md](docs/phase4-design.md), then reviewed and fixed:
- `pnp/job/banks.py`:
  - Feeder banks: front and back stacks whose lanes are drag feeders tagged with `bank`/`lane`.
  - Lanes are generated from an origin, lane and feed directions, and per-lane tape width and pitch. The needle point is placed at the sprocket holes using the EIA-481 offsets.
  - Named layouts can be saved and loaded, so a swapped stack doesn't need re-teaching.
- `pnp/job/rules.py`: package → nozzle rules (N1 thick, N2 thin, first match wins) with an optional place depth per package.
- `pnp/job/kicad.py`: KiCad ASCII `.pos` and CSV import (mm or inches, values with spaces, BOM/CRLF).
- `pnp/job/job.py`:
  - Jobs with panels (grid or a free list of boards, bad boards skipped) and board → panel → world transforms.
  - Board rotation is fitted from a second point.
  - Feeder matching by value and package, with per-part nozzle, feeder and skip overrides, and a placement plan with a stated problem per part.
- `pnp/job/runner.py`:
  - One part at a time (pick and place under one head lock).
  - Pause and stop take effect between parts.
  - A failed pick is retried twice; drag lanes are re-fed before a retry. After that the part is marked failed and the job pauses.
  - An empty lane pauses the job. A machine error goes to error.
- API (`bank_routes.py`, `job_routes.py`):
  - Endpoints for banks, layouts, package rules and the job.
  - While a job runs, every change request except E-stop, E-stop reset and the job controls is refused with 409 (review fix: manual jog, outputs and calibration were not blocked).
- UI:
  - Job tab: import, board origin and rotation from the camera, panel grid, a parts table with overrides and Go to, and run controls with progress.
  - Banks card on the Feeders tab.
  - Nozzle rules card on the Setup tab.
- Review fixes:
  - Saving a bank lane in the feeder form no longer detaches it from its bank.
  - Bottom-side parts report "Bottom side".
- `--sim` seeds a back bank (8 × 8 mm, layout "Passives") and a front bank (4 × 12 mm, layout "ICs"), plus a 2-board demo panel from `examples/demo.pos`.
- Demo through the UI: all 20 placements of the demo panel placed, 0 failed. 268 tests in total.

**Before the first real job:**
- Everything listed under phase 3.
- Teach the real stack origins (Generate lanes → Use camera position) and check one lane's needle point by eye.
- Measure the real place depth per package family.
- Check the KiCad rotation convention against the tape orientation on a first board.

### Phase 5: done in the simulator (2026-11-12), not yet on the machine

Built by Haiku agents against [docs/phase5-design.md](docs/phase5-design.md), then reviewed and fixed:
- `pnp/vision/fiducials.py`:
  - Finds round pads of the expected diameter (size, circularity, not cut by the image border), nearest first.
  - `locate_fiducial` centres on a pad in up to 3 moves.
  - `fit_rigid` does a 2-D rotation and translation fit with a scale report and per-point residuals.
  - Review fix: only bright *regions* count. Before, a dark hole in a bright area (a bed dot, a via) could pass as a fiducial.
- `pnp/job/job.py`:
  - KiCad parts named `FID*`, or with "fiducial" in the package, become job fiducials (bottom-side ones are dropped).
  - Modes: off, panel (one fit for the whole panel, measured on the first and last board) or each board (one fit per board, in `board_fits`).
  - Any manual change of origin, rotation or panel clears the fit.
- `pnp/job/align.py`:
  - Measures every fiducial from the user's rough origin, refining the prediction as points come in.
  - Rejects a fit with RMS > 0.15 mm or scale error > 0.5 %, and records each fiducial's correction.
- **Runner:** measures fiducials automatically before the first part when the job has fiducials and no fit.
- **API:** `PUT /api/job/fiducials`, `POST /api/job/fiducials/measure|clear`, `POST /api/job/fiducials/{name}/goto`.
- **Fiducials card on the Job tab:** mode, diameter, editable list with Go to, Measure, and a result showing RMS, rotation, scale and per-point correction.
- **Simulator:**
  - The down camera now draws a realistic board (mask, fiducials, part pads) at a *true* position 0.35/−0.25 mm and 0.4° off the job's nominal origin, so the tests prove the correction.
  - Review fix: board drawing shifted by 0.5 px to match the measuring pixel convention.
  - `examples/demo.pos` gained FID1–FID3.
- **Results in the simulator:**
  - Panel fit: origin within 0.001 mm and rotation within 0.001°, RMS 0.002 mm.
  - With alignment, every placement lands within 0.06 mm of the true pad. Without it, placements are off by about 0.4 mm.
  - 326 tests in total.

**Before using fiducials on the machine:**
- Check the real fiducial contrast (copper on mask) and set the diameter.
- Check that the down camera is calibrated and in focus.
- Run Measure fiducials once by hand before trusting the automatic step.

### Phase 6: done in the simulator (2026-11-12), not yet on the machine

Built by Haiku agents against [docs/phase6-design.md](docs/phase6-design.md), then reviewed and fixed:
- `pnp/vision/bottom.py`: measures the part on the nozzle from an up-camera frame.
  - Bright regions within 6 mm of the axis are merged into one outline (body + leads), at sub-pixel precision via 4× upsampling, and fitted in mm (so mirroring and non-square pixels are handled).
  - It reports the offset from the nozzle axis, the long-side angle and the size.
  - A lone round blob no bigger than the tip counts as no part.
- `VisionConfig` (`vision:` in `machine.yaml`): on/off, LEDs, settle time, threshold, search radius, tip diameters (N1 0.8 / N2 0.4 mm), size tolerance (30 %), max offset (1 mm), max angle (15°).
- **Package rules** gain `body` (length × width as seen from below) and `vision`. Defaults: 0201 to 2512, SOT-23 and SOIC-8.
- **HeadOps:**
  - After each pick, the nozzle goes over the up camera and the part is measured.
  - A missing part, wrong size, too far off-centre or too much turned is discarded, then `PickError`, so the runner retries (and re-feeds drag lanes).
  - The place move corrects the angle (`rotation − tape − error`) and the position: the tip goes to `W − rot(φ) · offset`, using the angle the controller actually reached.
  - `POST /api/head/inspect` runs a manual inspection.
  - The last inspection per nozzle is shown on the Feeders tab and the Job run card.
- **Simulator:**
  - Each pick gets a random offset (≤ 0.3 mm) and turn (≤ 4°). The up camera draws the part, and the true landing position is recorded.
  - Review fix: a tip under a part draws dark, so an edge sticking out doesn't merge into the part outline.
- **Review fixes:**
  - The rules API dropped `body`/`vision` on save.
  - `inspect_nozzle` called `machine.state()`, but `state` is a property, so every real inspection would have crashed (the agent had added an adapter; fixed at the source instead).
- **Results in the simulator:**
  - With vision, true part centres land within 0.05 mm and 0.5° of target, also at 37° and 90° placement rotations. Without it, they are off by up to 0.3 mm and 4°.
  - The demo panel: 20/20 placed, 0 failed.
  - 382 tests in total.

**Before using bottom vision on the machine:**
- Check the A-axis direction: positive must turn the part CCW seen from above (the correction relies on it).
- Check the up-camera exposure with the LEDs on, so parts are bright against the background.
- Check the nozzle faces read darker than the parts. If not, set `vision.min_brightness`.
- Measure the real tip diameters.
- Check the body sizes of your common packages with Inspect part.

### Real-machine session 2026-11-13

- **Linux env:** `.venv` is now a Windows venv. Linux uses `.venv-linux` (launch configs `tvm-pnp-sim-linux` / `tvm-pnp-machine-linux`).
- **Camera power-saving:** after a reboot, power-saving was back on for the up camera. `sudo udevadm trigger --action=add --subsystem-match=usb --attr-match=idVendor=045e` re-applies the rule.
- **Down-camera calibration fix:** the head's red glow (fixed in the image) captured the template match (fit residual 17.4 px). `measure_shift` now high-pass filters both images first (0.21 px on the real frames, kept as a regression test in `tests/data/down_glow`). Calibrated on the machine: **0.0236 mm/px**, residual 0.70 px.
- **UI fixes:**
  - Request errors no longer get hidden by the next live-state update.
  - UI files are served with `Cache-Control: no-cache`, so the browser can't keep a stale `app.js`.
- **Reconnect:**
  - A powered-off controller leaves a dead TCP connection. After 3 unanswered state requests the transport now drops it and reconnects by itself.
  - A **Connect** button in the header forces a reconnect (`POST /api/machine/reconnect`). The badge tooltip shows `link_error`.
- **Auto-centre fix:** local (adaptive) threshold, round marks 0.3–4 mm only, at most 2 mm per correction, and a stop if the mark doesn't follow the head (the old version locked onto the glow-bounded dark area and drove away).
- **Needle interlock:** X/Y moves and homing are refused while the feeder needle is down (HTTP 409). Exceptions: the drag during a feed (`needle_moves_allowed`) and the Setup needle step (`POST /api/calibrate/needle`; raising the needle ends it). The Machine-tab output toggle does not allow moves.
- **Vacuum outputs swapped** (checked on the machine): nozzle 1 vacuum is bit `0x2000` and nozzle 2 is `0x0004`, the reverse of the Node bridge. Blow-off and the vacuum sensors were already correct.
- **Vacuum held until place:** `head.check_vacuum` now defaults to off, because the real sensor didn't report held parts and good picks got dropped. Vacuum stays on from pick until place, discard or E-stop. An error during a pick keeps the vacuum on, lifts the nozzle and marks it as holding. Bottom-vision rejections still discard.
- **Feeder export/import:** the Feeders tab has **Export** (one YAML file with all feeders, banks and bank layouts; `GET /api/feeders/export`) and **Import…** (`POST /api/feeders/import`), with Replace (default) or Merge by id, and Reset part counts (default on). The whole file is checked before anything changes (`pnp/job/feeder_config.py`).
- **Two-point board alignment** (replaces Set origin from camera / Align rotation on part): on the Job tab's Board card, set Point 1 (the origin, board 0,0 by default) and Point 2, each with a board position and a machine position (Capture camera fills it in; editable), (a part's centre, or custom board X/Y such as a mounting hole), then Set board from points 1 and 2. It sets origin and rotation together and reports the A–B distance check. The points are saved with the job (`references`, `reference_board`); `POST /api/job/align/two-points`, `fit_two_points` in job.py. The old endpoints still exist.
- **Pinned camera strip:** the down and up views (status, crosshair, click-to-move, Up LEDs) live in the sticky top bar and stay visible on every tab while the page scrolls under them. The per-tab camera views (Machine card, Setup steps, Feeders card) are removed. Each view has a fixed height with its width from the camera's aspect, so clicks map 1:1 to camera pixels.
- **No native browser dialogs:** the Claude browser pane suppresses `confirm()` (it silently returns false), which made the feeder import do nothing. All confirmations use the in-page `askConfirm()` dialog; don't use `alert`/`confirm`/`prompt` in the UI.
- **Needle-stuck alarm:** if the needle sensor still reads down after the needle is switched up, the buzzer sounds and a popup offers Silence, Clear and continue (resumes the waiting feed, pick or job once the sensor reads up) and Abort. E-stop ends it too. Endpoints `/api/alarm/clear|silence|abort` stay allowed during a job.
- **Cameras:** the head camera hung again once on reopening after a restart (even with power-saving off); a replug fixed it. A hung camera now holds the one-at-a-time open turn for at most 10 s (`OPEN_TURN_TIMEOUT`), so it can't block the other camera forever.
- **Homing robustness:** homing sometimes stuck on the Y Top switch, then worked when run again. The suspected cause is that right after a stop (or the switch stopping the axis) the controller acknowledges but ignores the next move. Each homing move now waits for the axis to be at rest, checks that the move really starts (position changes within 0.6 s), and resends up to 3 times; failures are logged. The simulator models the behaviour (`move_ignored_after_stop`).
- **Needle position sensor** (found 2026-11-13 with `GET /api/debug/raw`): state byte 10 bit `0x08` is set when the needle is UP and clear when it is DOWN. It is a physical sensor: it lags the command by the needle's travel time (41 ms down and 62 ms up on the machine). Switching the needle now waits for it (`head.needle_timeout`, 1 s). A needle that doesn't go down is switched off again and nothing is dragged; a needle stuck down stops the feed, and the move interlock trusts the sensor, so nothing moves until it is freed. There is a Needle light on the Machine tab.
- **Z (seesaw):** 0 = both nozzle tips level. N1 goes down on +Z and N2 on −Z. There is no home switch, so a nozzle driven past its end makes the motor skip steps and the zero drifts. The **Set Z zero here** button on the Machine tab (`POST /api/machine/zero-z`) re-zeroes without moving. Measured lowest point: **9.5 mm per nozzle** (`z1_max`/`z2_max`).
- **Feeders tab Place target:** X/Y/rotation/depth fields for Place and Pick and place (empty X/Y = crosshair), plus Use crosshair and Go to target.
- **Per-part vision:** the Job parts table has a Vision column (Auto = package rule / On / Off), stored per part reference (`JobPart.vision`). Old job files load as Auto.
- **Defaults tab:** every value in `config/machine.yaml` (head, vision, limits, motion; homing, steps, controller and motion internals in a collapsed Advanced section with a warning) with a label, help text and the built-in default. Save writes only the changed values into machine.yaml with ruamel.yaml (comments kept; new dependency `ruamel.yaml`) and applies them in place at once (`pnp/config_editor.py`, `GET/PUT /api/config/defaults`). Controller IP/port need a restart. New `head.pick_depth` (3 mm) is the pick depth for new feeders and bank lanes. In `--sim` mode the tab saves to a temporary copy, never the real machine.yaml.
- **Calibration status:** down scale ✅, nozzle 2 offset ✅ (−24.38, −49.16). Still to do: nozzle 1, prick, up camera, depths, discard bin, feeders.
- **Tests:** 388. One intermittent failure was seen once in a full run and not reproduced in two more runs.

### Real-machine session 2026-11-16

- **Front-panel keys** (found with `python -m pnp.tvm.keyprobe`, a read-only bit logger): each key pulls one bit LOW in the state reply while held. Byte 10: `0x01` X−, `0x02` DotMove/ConMove, `0x04` Y+. Byte 9: `0x08` Y−, `0x10` X+, `0x20` Step, `0x40` Pause/Cont, `0x80` Start/Stop. The controller does nothing with them itself. `protocol.PANEL_KEYS`, `MachineState.keys`; `pnp/machine/panel.py` acts on them: arrows jog (Dot = one UI step per press, Con = `Machine.jog_hold` while held, to the soft limit at most), DotMove/ConMove toggles the mode, Start/Stop and Pause/Cont drive the job, Step drives the job stepper. Mode and step are in `Machine.panel` and on the Jog card. Idle status poll is now 50 ms so a tap isn't missed.
- **Job step mode** (`JobRunner.step`, `POST /api/job/step`, Step key and Job tab Step button): one stage per press: needle over the lane, needle down + drag, needle up (drag lanes that need a feed only), nozzle over the part, down, up, move to the place (bottom vision on the way), place. HeadOps feed/pick/place are split into these stages; the automatic run calls them in the same order. Stop lifts needle and nozzles; a picked part stays held and is reported.
- **Rotation motors swapped** (checked on the machine): nozzle 1 rotation is axis bit `0x20` (move slot 5, state offset 32), nozzle 2 is `0x08` (slot 3, offset 24), the reverse of the Node bridge, like the vacuum outputs.
- **Two-point board setup pitfall:** KiCad exports without the drill/place origin use page coordinates with Y negated (grelay4-low_power: outline X 92..150, Y −41.75..−120.75), so "board 0,0" is the page corner and KiCad's on-screen Y has the wrong sign. Setting the board now refuses a point far (>40 mm) from every part, a measured/board distance mismatch (>1 mm or 2 %), and any fit that puts parts outside the soft limits (the previous board is kept).
- **Job parts:** On switch per part (`JobPart.skip`, all boards; survives Reset statuses), Remove (`DELETE /api/job/parts/{ref}`), Disabled filter and Enable/Disable shown (`POST /api/job/parts/enable`).
- **Reset nozzles** (`POST /api/head/reset`, Job tab Run card): vacuum off, nozzles up, holding cleared, so a bad pick no longer blocks every next pick until a place.
- **Fixed:** two elements had `id="confirm"`, so every `confirmDialog` (delete feeder/bank/job, load layout, reset statuses) crashed silently; it now uses the in-page box.
- **Rotation sense** (checked on the machine): a positive A angle turns the nozzle anticlockwise seen from above, the same as KiCad, which the placement maths relies on. Only the Machine tab's rotate buttons were labelled the wrong way round (fixed in the UI; the motor direction is unchanged).
- **Pump off** when a job stops (automatic run, step mode, Stop from paused/error) or finishes (`JobRunner._pump_off`). Leaving step mode after a pick releases the part (vacuum off, nozzle cleared) and says so.
- **Rotation offset per feeder / stack:** parts sit in the tape at an angle set by the tape standard (an 0805 lies across 8 mm tape), so one file can need different corrections per feeder. The feeder form shows `Feeder.rotation` as a "Rotation offset" (offset = −rotation; −90 = 90° more clockwise). Banks: `rotation_offset` in Generate lanes and `POST /api/banks/{id}/rotation`. The plan has `nozzle_angle` (rotation − feeder rotation), shown as "Place at" in the Parts table.
- **Editable placement:** the Parts table edits each part's file X / Y / rotation (`PUT /api/job/parts/{ref}` takes `x`, `y`, `rotation`; None = unchanged).
- **Centre on mark for reference points** (`POST /api/calibrate/down/centre` with `mark_mm` and `method`): the Job tab uses method "auto" = circle edge first (`find_circle`: CLAHE + Hough, refined by a least-squares fit to the edge pixels, ~0.5 px repeatable), then the threshold blob. Board holes are dark on dark solder mask with only a faint edge ring, so the blob search couldn't see them and went to nearby "USB" text. Candidates are chosen by distance to their EDGE, not their centre. Real frames are kept as regression tests (`tests/data/cutout_usb`).
- **Alignment on the Voip board:** corners captured by eye were 0.5 mm off; the four 3.25 mm corner holes (file X 4.041/62.817, Y 4.108/86.049) with Centre on mark put Go to on the parts. The two-point distance came out 0.34 % short over 100 mm: if far parts drift, check X/Y steps per mm.
- **Detection overlay:** `GET /api/vision/detect/{down|up}` returns the latest frame's detection as shapes in camera pixels (down: mark rings + found mark; up: search radius + measured part rectangle). The camera views have a Detect switch that draws them as an SVG overlay (read-only, no moves or LEDs).
- **Tests:** 516.

## 6. First concrete steps (original)

1. Create `pyproject.toml` and the package skeleton above.
2. Write `pnp/tvm/protocol.py` and tests from the frames in section 1.1.
3. Write the simulator, then `transport.py` against it.
4. Build `Machine.move_to()` with completion detection against the simulator.
5. Minimal FastAPI app plus the Machine tab (DRO + jog + actuators + E-stop), then the first real-machine session using the safety checklist.
