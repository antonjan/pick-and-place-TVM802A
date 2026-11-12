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
| 7 | **Polish** | Two-nozzle cycles, speed profiles per package, job resume, nozzle runout compensation, feeder hole vision, logs/stats |

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
5. **Machine IP:** is `192.168.0.8` fixed? It will be configurable either way.
6. **Cameras (phase 2):** exact camera models and resolution; does the up camera need a specific exposure setting (v4l2-ctl manual exposure)?
7. **Nozzles (phase 6):** which nozzle tips are fitted on N1/N2, and do you swap them manually during a job (the TVM802 has no auto tool changer)?
8. **Panels (later):** single boards only, or panelized jobs too?

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

**Real cameras:** both Microsoft 045e:0294 cameras streamed at first (≈4–7 fps at 960×720, auto-exposure), then stopped responding within minutes (STREAMON EIO / control timeouts) while still enumerated on USB. Both sit directly on USB 2.0 bus 1 (ports 3/4) with autosuspend on, alongside the Wi-Fi dongle. Isolation test after a replug: the **up camera `…11585848` streams at 29.9 fps alone at 640×480**, while the **head camera `…56215848` hangs on open even alone on the bus** and afterwards also knocks the up camera out until replugged. Port-swap test: the head camera streams fine on port 3, so the **fault was the PC's USB port 4 (or the cabling to it)**, not the camera. With the head camera on port 3 and the up camera on port 10, both stream together at about 15 fps (auto-exposure), with 0 stalls in 3 minutes. Avoid port 4. The real-machine UI shows both live views. The down view is rotated 270° (90° anticlockwise); the user confirmed X+ pans right and Y+ pans up. Camera hangs, root cause narrowed down (13:25): the hung capture threads sit in kernel state D in `usb_start_wait_urb`, meaning the camera never answers a USB control request. Both cameras have USB autosuspend on (2 s). Countermeasures: `config/udev/90-tvm802-cameras.rules` (turns autosuspend off, installed with sudo), opening only one camera at a time, a 3 s settle delay after plug-in, and never opening a device that another process holds (the Claude desktop app's capture service probes freshly plugged cameras). **Result (13:36):** with the rule installed (`power/control=on`), both cameras reconnected cleanly after a replug, one after the other, and kept streaming at about 15 fps. Camera roles re-verified by motion and LED tests: `…56215848` moves with the head and `…11585848` reacts to the up LEDs. **Open:** focusing the head camera and the down-camera calibration are postponed (no suitable PCB at hand; sharpness only about 27). Printed text or a sheet of paper with a few sharp dots also gives the scale calibration enough texture. Measured travel ends: X −115 mm and Y −77 mm. Soft limits are now X [−113, 299] and Y [−75, 350.5]. Next: check focus (the head view is a blur), then run the Setup wizard on the machine.

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

## 6. First concrete steps (original)

1. Create `pyproject.toml` and the package skeleton above.
2. Write `pnp/tvm/protocol.py` and tests from the frames in section 1.1.
3. Write the simulator, then `transport.py` against it.
4. Build `Machine.move_to()` with completion detection against the simulator.
5. Minimal FastAPI app plus the Machine tab (DRO + jog + actuators + E-stop), then the first real-machine session using the safety checklist.
