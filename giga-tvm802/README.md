# TVM802 control

This application controls a TVM802 pick-and-place machine. It replaces the vendor software and OpenPnP.

The application has two parts:

- A Python server. The server sends commands to the machine controller over the network and reads the two cameras.
- A web interface. You use the web interface in a browser.

The application also has a simulator. Use the simulator to learn the application and to test changes when the machine is not available.

> This README is written in ASD-STE100 Simplified Technical English.

---

## Contents

1. [Safety](#1-safety)
2. [Requirements](#2-requirements)
3. [Install the application](#3-install-the-application)
4. [Start the application](#4-start-the-application)
5. [The web interface](#5-the-web-interface)
6. [Procedures](#6-procedures)
7. [Configuration files](#7-configuration-files)
8. [Command-line tools](#8-command-line-tools)
9. [Tests](#9-tests)
10. [Status and planned features](#10-status-and-planned-features)
11. [Known limits](#11-known-limits)

---

## 1. Safety

> **WARNING:** The machine can move quickly and without a warning. Keep your hands away from the head and the bed when the machine is connected.

> **WARNING:** Keep one hand near the machine power switch when you do the first moves on a machine.

> **CAUTION:** Do the first moves at low speed and with small distances. Make sure that each axis moves in the correct direction before you start an automatic job.

- The **E-stop** button is at the top right of every screen. The **Esc** key also does an E-stop.
- An E-stop stops all axes. It also turns off the vacuum, the pump, the blow-off and the feeder needle.
- To continue after an E-stop, click **Reset E-stop**. Then home the machine again.
- The application does not let X and Y go outside the soft limits after you home the machine.
- Before X and Y move, the application lifts the two nozzles.
- If a limit switch operates during a move, the application stops all axes.
- If the connection to the machine stops, the application sets the machine to "not homed".
- When a job runs, the application blocks manual commands (jog, outputs, homing, calibration). Only E-stop, Reset E-stop and the job controls stay available.

---

## 2. Requirements

| Item | Requirement |
|---|---|
| Computer | Linux (primary). Windows is possible for the simulator. |
| Python | Version 3.11 or newer |
| Network | A direct Ethernet connection to the machine controller. The controller address is `192.168.0.8`, port `701`. |
| Cameras | Two USB (UVC) cameras: one down camera on the head and one up camera under the head path |
| Browser | A modern browser (Chrome, Edge or Firefox) |

---

## 3. Install the application

### 3.1 Linux

1. Open a terminal in the application folder.
2. Make a virtual environment and install the application:

   ```bash
   uv venv .venv && uv pip install --python .venv/bin/python -e '.[dev]'
   ```

   If you do not have `uv`, use these commands:

   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -e '.[dev]'
   ```

3. Install the camera rule. This rule stops USB power saving on the two cameras. Without it, the cameras can stop:

   ```bash
   sudo cp config/udev/90-tvm802-cameras.rules /etc/udev/rules.d/
   sudo udevadm control --reload
   ```

4. Disconnect the two cameras and connect them again.

### 3.2 Windows (simulator only)

1. Open a terminal in the application folder.
2. Make a virtual environment and install the application:

   ```bash
   py -m venv .venv
   .venv/Scripts/python -m pip install -e ".[dev]"
   ```

> **NOTE:** On Windows, use `.venv/Scripts/python` where this README shows `.venv/bin/python`.

---

## 4. Start the application

### 4.1 Start with the simulator

Use the simulator when the machine is not available. The simulator does not need a machine or cameras.

1. Start the server:

   ```bash
   .venv/bin/python -m pnp.api --sim
   ```

2. Open `http://127.0.0.1:8080` in a browser.

The simulator gives you:

- A simulated controller and two simulated cameras.
- A calibration that is already complete.
- Two example feeders (`F1` strip, `F2` drag).
- Two example feeder banks: `back` (8 lanes, 8 mm tape) and `front` (4 lanes, 12 mm tape).
- An example job: the board in `examples/demo.pos`, as a panel of two boards.

The simulator keeps its data in temporary files. Your files in `config/` do not change.

### 4.2 Start with the machine

1. Connect the computer to the machine controller with an Ethernet cable.
2. Set the computer network address in the same network as the controller (for example `192.168.0.100`).
3. Turn on the machine.
4. Start the server:

   ```bash
   .venv/bin/python -m pnp.api
   ```

5. Open `http://127.0.0.1:8080` in a browser.
6. Make sure that the status at the top shows **Connected**.
7. Click **Home machine** on the Machine tab.

### 4.3 Server options

| Option | Function |
|---|---|
| `--sim` | Use the built-in simulator. |
| `--port N` | Use HTTP port N. The default is in `config/machine.yaml` (8080). |
| `--host ADDRESS` | Use this network address for the web interface. Use `0.0.0.0` to open the interface from a different computer. |
| `--config FILE` | Use a different machine configuration file. |
| `--no-cameras` | Do not start the cameras. |
| `--cameras FILE` | Use a different camera configuration file. |
| `--calibration FILE` | Use a different calibration file. |
| `--feeders FILE` | Use a different feeder file. |
| `--banks FILE` | Use a different feeder bank file. |
| `--rules FILE` | Use a different nozzle rules file. |
| `--job FILE` | Use a different job file. |
| `--debug` | Show more messages in the terminal. |

---

## 5. The web interface

The web interface has six tabs. The **Settings** tab is at the right side of the tab bar.

The bar at the top always shows:

- The connection status, the homing status, the busy status and the speed mode.
- The **E-stop** button.

### 5.1 Machine tab

Use the Machine tab for manual control.

- **Position:** the X, Y, Z, A1 and A2 positions.
- **Sensors:** the four limit switches and the vacuum sensor of each nozzle.
- **Go to:** moves the head to an X/Y position. You must home the machine first.
- **Home machine:** homes Y, then X.
- **Jog:** moves X and Y by the selected step (0.01 mm to 50 mm). **Raise** lifts the two nozzles.
- **Nozzles:** lowers and lifts each nozzle by the selected Z step. It also turns each nozzle by the selected angle, or back to 0°.
- **Cameras:** live views of the down camera and the up camera. Click a point in a view to move the head to that point. **Up LEDs** turns the up camera lights on and off.
- **Outputs:** turns the pump, vacuum, blow-off, feeder needle, LEDs and buzzer on and off.

Keyboard keys:

| Key | Function |
|---|---|
| Arrow keys | Jog X and Y |
| Page Up / Page Down | Lift or lower nozzle 1 |
| Esc | E-stop |

### 5.2 Setup tab

Use the Setup tab to calibrate the cameras and the tools. Do the steps in sequence (see [6.1](#61-calibrate-the-machine)).

1. **Down camera scale.** Finds the relation between camera pixels and millimetres.
2. **Nozzle offsets.** Finds the position of each nozzle tip relative to the down camera centre.
3. **Prick needle offset.** Finds the position of the feeder needle tip relative to the down camera centre.
4. **Up camera scale.** Finds the pixel scale of the up camera.
5. **Up camera positions.** Records the position of each nozzle above the up camera.

**Auto-centre on mark** and **Auto-centre tip** move the head until the mark or the nozzle tip is at the camera centre. These buttons do not record a value.

### 5.3 Feeders tab

Use the Feeders tab to define feeders and to do manual picks.

- **Down camera:** a live view with a crosshair. Click the view to move the head.
- **Banks:** a bank is a stack of tape lanes, for example the front stack or the back stack.
  - **Generate lanes** makes all lanes of a bank from these data: the origin, the lane direction, the feed direction, the sprocket side, the tape width and the pitch of each lane.
  - **Save layout** keeps the lanes and the parts in the lanes with a name.
  - **Load layout** puts a saved layout back. Use this when you install a different stack.
- **Feeders:** a table of all feeders. A feeder has a type:
  - **Strip:** a cut tape. The pick point moves one pitch for each part.
  - **Drag:** a tape that the needle moves. The pick point does not move.
- **Feeder form:** edit one feeder (value, package, pick point, feed direction, pitch, count, part rotation, pick depth, needle point).
- **Actions:**
  - **Teach pick from camera** and **Teach prick from camera** record the point at the camera crosshair.
  - **Go to pick** moves the camera to the pick point.
  - **Feed** moves the tape one pitch.
  - **Pick**, **Place at crosshair**, **Pick and place at crosshair** and **Discard** operate the selected nozzle.
  - **Inspect part** moves the nozzle above the up camera and measures the part on it. The result of the last inspection of each nozzle shows below **Holding**.

### 5.4 Job tab

Use the Job tab to place a full board or panel.

- **Run:** **Start job**, **Pause**, **Resume**, **Stop** and **Reset statuses**. A progress bar and the counts of placed, failed and skipped parts.
- **Import:** reads a KiCad position file (`.pos` or `.csv`).
- **Board:** the board origin and the board rotation. You can set the two values from the camera.
- **Fiducials:** the fiducial marks of the board. The application measures them with the down camera and corrects the board origin and rotation (see [6.6](#66-use-fiducials)).
- **Panel:** a grid of boards (rows, columns, step X, step Y). You can skip a bad board.
- **Parts:** a table of all parts on all boards. For each part you can:
  - Select a different nozzle or a different feeder.
  - Skip the part.
  - Move the camera to the part (**Go to**).
  - Set a placed or failed part back to "to do".

  The **Problem** column shows why a part cannot be placed.

### 5.5 Log tab

The Log tab shows the last 200 messages, with the newest message first. Errors are red. **Clear log** removes all messages.

### 5.6 Settings tab

- **Appearance:** System, Dark or Light colour theme.
- **Nozzle rules:** rules that select the nozzle for each package (see [6.4](#64-set-the-nozzle-rules)).
- **Config files:** **Reload config** reads `machine.yaml` and `cameras.yaml` again. You do not have to restart the server.

---

## 6. Procedures

### 6.1 Calibrate the machine

Do this procedure one time on a new machine. Also do it after you change a nozzle, the needle or a camera.

Before you start:

- Home the machine.
- Put a small, dark dot on white tape on the bed. Use this dot as the mark.

Procedure:

1. Go to the Setup tab.
2. **Down camera scale:**
   1. Move the down camera above a surface with texture, for example a PCB or printed text.
   2. Click **Calibrate down camera**.
   3. Make sure that a scale value shows.
3. **Nozzle offsets:**
   1. Move the down camera above the mark.
   2. Click **Auto-centre on mark**.
   3. Click **Camera is on mark**.
   4. Lower nozzle 1 until it almost touches the mark. Jog until the nozzle is at the centre of the mark.
   5. Click **Nozzle 1 is on mark**.
   6. Lift the nozzle. Do steps 4 and 5 again for nozzle 2.
4. **Prick needle offset:**
   1. Make sure that the camera mark is recorded (step 3.3).
   2. Click **Lower needle**.
   3. Jog until the needle tip touches the mark.
   4. Click **Needle is on mark**.
   5. Click **Raise needle**.
5. **Up camera scale:**
   1. Move nozzle 1 above the up camera. Turn on the up LEDs.
   2. Click **Calibrate up camera**.
6. **Up camera positions:**
   1. Click **Auto-centre tip**.
   2. Click **Record nozzle 1 position**.
   3. Do steps 6.1 and 6.2 again for nozzle 2.

### 6.2 Make a feeder bank

1. Go to the Feeders tab.
2. In the **Banks** card, click **Add bank**. Type an ID (for example `back`) and a name.
3. Open **Generate lanes** for the bank.
4. Move the down camera to the first pocket of lane 1. Click **Use camera position**.
5. Set these values:
   - **Lane direction:** the direction from lane 1 to lane 2.
   - **Feed direction:** the direction that the tape moves when the needle pulls it.
   - **Sprocket side:** the side of the pocket where the sprocket holes are.
   - **Lanes**, **width** and **pitch** of each lane.
6. Click **Generate lanes**.
7. In the feeder table, select each lane that has parts. Type the **Value** and the **Package** as they are in the KiCad file. Click **Save feeder**.
8. Click **Save layout**. Type a name for the layout.

> **NOTE:** The needle point of each lane is calculated from the tape standard. Look at one lane before the first job. If the needle point is not correct, select the lane, put the crosshair on the sprocket hole and click **Teach prick from camera**.

### 6.3 Change a stack

1. Install the stack on the machine.
2. Go to the Feeders tab.
3. In the bank of that stack, select the layout in the list.
4. Click **Load layout**.

### 6.4 Set the nozzle rules

The machine places each part with one nozzle. Nozzle 1 has the thick tip. Nozzle 2 has the thin tip.

1. Go to the Settings tab.
2. In **Nozzle rules**, select the default nozzle.
3. Add or change the rules. A rule has:
   - A package pattern.
   - A nozzle.
   - A body size (optional): the length and the width of the part as the up camera sees it, with the leads.
   - A place depth (optional).
   - A **Vision** setting: on, to inspect the part with the up camera.
   - A note.
4. Click **Save rules**.

The first rule that agrees with the package is used. The `*` character agrees with all text. For example, `*0402*` agrees with `C_0402_1005Metric`.

### 6.5 Run a job

Before you start:

- Calibrate the machine (see [6.1](#61-calibrate-the-machine)).
- Make sure that the feeders have the parts of the job.
- Home the machine.

Procedure:

1. In KiCad, export the component position file (ASCII `.pos` or CSV, in millimetres).
2. Go to the Job tab.
3. In **Import**, select the file and click **Import file**.
4. In **Panel**, set the rows, the columns and the step. Click **Make grid**. For one board, use 1 row and 1 column.
5. Move the down camera above the board origin. Click **Set origin from camera**.
6. To set the board rotation:
   1. Select a part in **Align rotation**.
   2. Move the down camera above that part.
   3. Click **Align rotation on part**.
7. Look at the **Parts** table. Make sure that no part shows a problem. Skip a part or select a different feeder if necessary.
8. Click **Start job**.

During the job:

- **Pause** stops the job after the current part.
- **Stop** stops the job after the current part and goes back to idle.
- **Resume** continues from the next part.

If a pick fails, the application tries the pick two more times. For a drag feeder, it moves the tape before each new try. After three failures, the application marks the part as failed and pauses the job.

If a feeder is empty, the application pauses the job. Fill the feeder, then click **Resume**.

**Bottom vision.** After each pick, the application moves the part above the up camera and turns on the up LEDs. It measures the position, the angle and the size of the part on the nozzle. Then it corrects the place move. The application discards the part and tries the pick again if:

- It does not see a part.
- The part size is not the body size in the nozzle rules.
- The part is more than 1 mm from the nozzle centre.
- The part is turned more than 15°.

The **Last part** line on the Run card shows the result of the last inspection. To turn off bottom vision for a package, clear **Vision** in the nozzle rules. To turn it off for all parts, set `vision.bottom_vision: false` in `machine.yaml`.

The application records the status of each part in `job.yaml` after each placement. If the server stops, start it again, home the machine and click **Start job**. The job continues from the first part that is not placed.

> **NOTE:** The application does not place bottom-side parts. It skips these parts automatically.

### 6.6 Use fiducials

Fiducials are small round copper marks on the board. The application finds them with the down camera. It then calculates the exact board origin and rotation.

When you import a KiCad file, the application finds the fiducials automatically. A part is a fiducial if its reference starts with `FID` (for example `FID1`), or if its package name contains `Fiducial`.

1. Import the job (see [6.5](#65-run-a-job)).
2. Set the board origin roughly with the camera. An error of 1 to 2 mm is satisfactory.
3. On the Job tab, find the **Fiducials** card.
4. Select the mode:
   - **Panel:** the application measures the fiducials on the first board and on the last board. It calculates one position for the full panel.
   - **Each board:** the application measures the fiducials on each board. Use this mode for a panel that is not accurate.
   - **Off:** the application uses the origin and the rotation that you set.
5. Make sure that the **Diameter** is the copper diameter of the fiducials (for example 1 mm).
6. Click **Measure fiducials**. Alternatively, click **Start job**: the job measures the fiducials before the first part.
7. Look at the result. **RMS** must be small (less than approximately 0.05 mm). **Correction** shows the distance between your rough origin and the measured position of each fiducial.

If the application cannot find a fiducial, it stops and shows a message. Make sure that the origin is approximately correct and that the diameter is correct. Then try again.

When you change the origin, the rotation or the panel, the application removes the measurement. Measure the fiducials again.

---

## 7. Configuration files

All files are in the `config/` folder. The application writes each file in one operation, so a power failure cannot leave a half-written file.

| File | Contents | Who changes it |
|---|---|---|
| `machine.yaml` | Controller address, steps per mm, soft limits, homing, motion, head timing, discard position, HTTP server | You (text editor) |
| `cameras.yaml` | Camera devices (by serial number), resolution, rotation, mirror | You (text editor) |
| `calibration.yaml` | Camera scales, nozzle offsets, needle offset, up camera positions | The Setup tab |
| `feeders.yaml` | All feeders and bank lanes | The Feeders tab |
| `banks.yaml` | Banks and their saved layouts | The Feeders tab |
| `packages.yaml` | Nozzle rules | The Settings tab |
| `job.yaml` | The current job and the status of each part | The Job tab |
| `udev/90-tvm802-cameras.rules` | Linux rule that stops USB power saving on the cameras | You (install one time) |

After you change `machine.yaml` or `cameras.yaml`, click **Reload config** on the Settings tab. A change to the controller address or the HTTP server needs a restart of the server.

---

## 8. Command-line tools

| Command | Function |
|---|---|
| `.venv/bin/tvm-pnp` | Starts the server. This is the same as `python -m pnp.api`. |
| `.venv/bin/tvm-sim --port 7001` | Starts only the simulated controller. |
| `.venv/bin/tvm-monitor` | Shows the live state of the machine. It does not send commands that move the machine. |
| `.venv/bin/tvm-monitor --raw --unknown` | Also shows the raw state data and the unknown replies. Use this to find unknown signals, for example the front panel keys. |
| `.venv/bin/tvm-monitor --host 127.0.0.1 --port 7001` | Shows the state of the simulated controller. |

---

## 9. Tests

To run all tests, use this command:

```bash
.venv/bin/python -m pytest
```

The tests use the simulator. They do not need a machine or cameras.

> **NOTE:** On Windows, the test `test_camera.py::test_device_holders_finds_other_process` fails. This test is for Linux only.

---

## 10. Status and planned features

| Phase | Function | Status |
|---|---|---|
| 0 | Protocol and simulator | Complete |
| 1 | Machine control and manual control interface | Complete. Tested on the machine. |
| 2 | Cameras and calibration | Complete in the simulator. The cameras operate on the machine. The calibration on the machine is not done. |
| 3 | Feeders and manual pick and place | Complete in the simulator. Not tested on the machine. |
| 4 | KiCad jobs, feeder banks, panels, placement without vision | Complete in the simulator. Not tested on the machine. |
| 5 | Fiducials | Complete in the simulator. Not tested on the machine. |
| 6 | Bottom vision | Complete in the simulator. Not tested on the machine. |
| 7 | Improvements | Planned |

### 10.1 Planned features

- **Improvements (phase 7):**
  - Speed settings for each package.
  - Compensation for nozzle runout.
  - Camera check of the sprocket holes on the feeders.
  - Statistics for each job.
- **Front panel keys:** use the jog keys and the start and stop keys on the machine front panel. The signals of these keys are not known yet. Use `tvm-monitor --raw --unknown` on the machine to find them.

### 10.2 Work to do on the machine

Do these tasks before the first job on the machine:

1. Adjust the focus of the down camera.
2. Do the calibration (see [6.1](#61-calibrate-the-machine)).
3. Set the discard position (`head.discard` in `machine.yaml`).
4. Measure the pick depth and the place depth. The current value of each depth is 3.0 mm, which is only an estimate.
5. Measure the X and Y speed. The move time limits use an estimated speed of 25 mm/s.
6. Teach the origin of each stack. Make sure that the needle point of one lane is correct.
7. Place a first board. Make sure that the part rotation from KiCad agrees with the tape orientation.

---

## 11. Known limits

- **2 mm pitch tape (0402 and 0201):** the sprocket holes are 4 mm apart. After a feed of 2 mm, no hole is below the needle point. The method for 2 mm pitch is not known yet.
- **Esc key:** Esc does an E-stop on all screens, also when a dialog is open.
- **Speed:** the application cannot change the machine speed yet. The data that sets the speed is not known.
- **One job:** the application keeps one job at a time. When you import a new file, the application replaces the current job.
