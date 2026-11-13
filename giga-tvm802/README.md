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
5. [Daily use on the machine](#5-daily-use-on-the-machine)
6. [The web interface](#6-the-web-interface)
7. [Procedures](#7-procedures)
8. [Configuration files](#8-configuration-files)
9. [Troubleshooting](#9-troubleshooting)
10. [Command-line tools](#10-command-line-tools)
11. [Tests](#11-tests)
12. [Status and planned features](#12-status-and-planned-features)
13. [Known limits](#13-known-limits)
14. [Recent changes](#14-recent-changes)

---

## 1. Safety

> **WARNING:** The machine can move quickly and without a warning. Keep your hands away from the head and the bed when the machine is connected.

> **WARNING:** Keep one hand near the machine power switch when you do the first moves on a machine.

> **CAUTION:** Do the first moves at low speed and with small distances. Make sure that each axis moves in the correct direction before you start an automatic job.

- The **E-stop** button is at the top right of every screen. The **Esc** key also does an E-stop.
- An E-stop stops all axes. It also turns off the vacuum, the pump, the blow-off and the feeder needle.
- To continue after an E-stop, click **Reset E-stop**. Then home the machine again.
- After you home the machine, the application does not let X and Y go outside the soft limits.
- Before X and Y move, the application lifts the two nozzles.
- If a limit switch operates during a move, the application stops all axes.
- If the connection to the machine stops, the application sets the machine to "not homed".
- When a job runs, the application blocks manual commands (jog, outputs, homing, calibration, settings). Only E-stop, Reset E-stop, the alarm buttons and the job controls stay available.
- **Needle interlock:** when the feeder needle is down, X and Y do not move and homing is not possible. There are two exceptions: the drag during a feed, and the **Prick needle offset** step on the Setup tab. The application reads the needle position from the needle sensor.
- **Stuck needle alarm:** if the needle does not go up after a feed, the buzzer starts and an alarm window opens. Nothing moves until the needle is free (see [7.8](#78-clear-a-stuck-needle-alarm)).

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

> **NOTE:** In this folder, `.venv` is a Windows virtual environment. On Linux, use the folder `.venv-linux`. All Linux commands in this README use `.venv-linux`.

1. Open a terminal in the application folder (`giga-tvm802`).
2. Make a virtual environment and install the application:

   ```bash
   uv venv .venv-linux && uv pip install --python .venv-linux/bin/python -e '.[dev]'
   ```

   If you do not have `uv`, use these commands:

   ```bash
   python3 -m venv .venv-linux
   ```

   ```bash
   .venv-linux/bin/pip install -e '.[dev]'
   ```

3. Install the camera rule. This rule stops USB power saving on the two cameras. Without it, the cameras can stop:

   ```bash
   sudo cp config/udev/90-tvm802-cameras.rules /etc/udev/rules.d/
   ```

   ```bash
   sudo udevadm control --reload
   ```

4. Disconnect the two cameras and connect them again.

> **NOTE:** If you update an existing installation, run the install command of step 2 again. The application now also needs the `ruamel.yaml` package (for the Defaults tab).

### 3.2 Windows (simulator only)

1. Open a terminal in the application folder.
2. Make a virtual environment and install the application:

   ```bash
   py -m venv .venv
   ```

   ```bash
   .venv/Scripts/python -m pip install -e ".[dev]"
   ```

> **NOTE:** On Windows, use `.venv/Scripts/python` where this README shows `.venv-linux/bin/python`.

---

## 4. Start the application

### 4.1 Start with the simulator

Use the simulator when the machine is not available. The simulator does not need a machine or cameras.

1. Start the server:

   ```bash
   .venv-linux/bin/python -m pnp.api --sim
   ```

2. Open `http://127.0.0.1:8080` in a browser.

The simulator gives you:

- A simulated controller and two simulated cameras.
- A calibration that is already complete.
- Two example feeders (`F1` strip, `F2` drag).
- Two example feeder banks: `back` (8 lanes, 8 mm tape) and `front` (4 lanes, 12 mm tape).
- An example job: the board in `examples/demo.pos`, as a panel of two boards.

The simulator keeps its data in temporary files. Your files in `config/` do not change. This also applies to the Defaults tab: in the simulator, it saves to a temporary copy of `machine.yaml`.

### 4.2 Start with the machine

1. Connect the computer to the machine controller with an Ethernet cable.
2. Set the computer network address in the same network as the controller (for example `192.168.0.100`).
3. Turn on the machine.
4. Start the server:

   ```bash
   .venv-linux/bin/python -m pnp.api
   ```

5. Open `http://127.0.0.1:8080` in a browser.
6. Make sure that the status at the top shows **Connected**. If it does not, click **Connect** (see [9.1](#91-the-machine-is-not-connected)).
7. Click **Home machine** on the Machine tab.

> **TIP:** You can run the simulator and the machine at the same time on different ports. For example, use `--sim --port 8080` for the simulator and `--port 8081` for the machine.

### 4.3 Stop and restart the server

1. Press **Ctrl+C** in the terminal of the server.
2. Before you start the server again, make sure that no process uses the cameras:

   ```bash
   fuser /dev/video*
   ```

   If this command shows no output, the cameras are free.
3. Start the server again (see [4.2](#42-start-with-the-machine)).

A restart does not move the machine. After a restart, home the machine again.

> **NOTE:** A change to a web interface file (`pnp/web/*`) only needs a reload of the browser page. A change to a Python file needs a restart of the server.

### 4.4 Server options

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

## 5. Daily use on the machine

This is the usual sequence for a work session. The sections that follow give the details.

1. Turn on the machine. Start the server and open the web interface (see [4.2](#42-start-with-the-machine)).
2. Make sure that the two camera views at the top show a live image. If a view stays empty, see [9.2](#92-a-camera-view-stays-empty).
3. Click **Home machine**.
4. Make sure that the two nozzles are level. If they are not, see [7.9](#79-set-the-z-zero).
5. Install the feeder stacks. On the Feeders tab, load the layout of each stack, or import a feeder file (see [7.3](#73-change-a-stack) and [7.4](#74-export-and-import-the-feeders)).
6. On the Job tab, import the KiCad position file and set the board with two reference points (see [7.6](#76-run-a-job)).
7. Do one test pick and place on the Feeders tab if you changed a feeder.
8. Click **Start job**.

---

## 6. The web interface

The web interface has seven tabs: **Machine**, **Setup**, **Feeders**, **Job**, **Log**, **Defaults** and **Settings**. The **Settings** tab is at the right side of the tab bar.

The bar at the top is always on the screen. It does not scroll. It shows:

- The connection status, the homing status, the busy status and the speed mode. Put the mouse on the connection status to see the last connection error.
- **Connect**: this button shows when the machine is not connected. Click it to connect again, for example after you turn the machine off and on.
- The **E-stop** button.
- The **camera views**: the down camera (left) and the up camera (right). Each view has a crosshair and shows the frame rate. Click a point in a view to move the head to that point (the camera must be calibrated). **Up LEDs** turns the up camera lights on and off.

### 6.1 Machine tab

Use the Machine tab for manual control. The tab has three columns: position, jog and outputs.

- **Position:** the X, Y, Z, A1 and A2 positions.
- **Sensors:** the four limit switches, the vacuum sensor of each nozzle and the **Needle** light. The Needle light comes on when the needle sensor reads "down".
- **Go to:** moves the head to an X/Y position. You must home the machine first.
- **Home machine:** homes Y, then X.
- **Jog:** moves X and Y by the selected step (0.01 mm to 50 mm). **Raise** lifts the two nozzles.
- **Nozzles:** lowers and lifts each nozzle by the selected Z step. It also turns each nozzle by the selected angle, or back to 0°.
- **Set Z zero here:** sets the current Z position as 0 (the two nozzle tips are level). This button does not move the machine (see [7.9](#79-set-the-z-zero)).
- **Outputs:** turns the pump, vacuum, blow-off, feeder needle, LEDs and buzzer on and off.

Keyboard keys:

| Key | Function |
|---|---|
| Arrow keys | Jog X and Y |
| Page Up / Page Down | Lift or lower nozzle 1 |
| Esc | E-stop |

### 6.2 Setup tab

Use the Setup tab to calibrate the cameras and the tools. Do the steps in sequence (see [7.1](#71-calibrate-the-machine)).

1. **Down camera scale.** Finds the relation between camera pixels and millimetres.
2. **Nozzle offsets.** Finds the position of each nozzle tip relative to the down camera centre.
3. **Prick needle offset.** Finds the position of the feeder needle tip relative to the down camera centre. In this step only, the head can move with the needle down.
4. **Up camera scale.** Finds the pixel scale of the up camera.
5. **Up camera positions.** Records the position of each nozzle above the up camera.

**Auto-centre on mark** and **Auto-centre tip** move the head until the mark or the nozzle tip is at the camera centre. These buttons do not record a value. Auto-centre only uses round marks of 0.3 mm to 4 mm. It moves at most 2 mm for each correction, and it stops if the mark does not follow the head.

### 6.3 Feeders tab

Use the Feeders tab to define feeders and to do manual picks.

- **Banks:** a bank is a stack of tape lanes, for example the front stack or the back stack.
  - **Generate lanes** makes all lanes of a bank from these data: the origin, the lane direction, the feed direction, the sprocket side, the tape width and the pitch of each lane.
  - **Save layout** keeps the lanes and the parts in the lanes with a name.
  - **Load layout** puts a saved layout back. Use this when you install a different stack.
- **Feeders:** a table of all feeders. A feeder has a type:
  - **Strip:** a cut tape. The pick point moves one pitch for each part.
  - **Drag:** a tape that the needle moves. The pick point does not move.
- **Export** and **Import…:** save all feeders, banks and layouts in one file, and load them again (see [7.4](#74-export-and-import-the-feeders)).
- **Feeder form:** edit one feeder (value, package, pick point, feed direction, pitch, count, part rotation, pick depth, needle point). A new feeder gets the default pick depth from the Defaults tab.
- **Actions:**
  - **Nozzle:** N1 or N2 for the head actions.
  - **Vision check:** On or Off. With On, **Pick** and **Pick and place at target** inspect the part on the up camera.
  - **Feeder:** **Teach pick from camera** and **Teach prick from camera** record the point at the camera crosshair. **Go to pick** moves the camera to the pick point. **Feed** moves the tape one pitch.
  - **Place target:** the X, Y, rotation and depth for a place. If X and Y are empty, the application uses the camera crosshair. If the depth is empty, the application uses the default place depth. **Use crosshair** copies the crosshair position into X and Y. **Go to target (camera)** moves the camera to the target.
  - **Head:** **Pick**, **Pick and place at target**, **Place at target**, **Inspect part** and **Discard** operate the selected nozzle. The result of the last inspection of each nozzle shows at **Holding**.

> **NOTE:** After a pick, the vacuum stays on until a place, a discard or an E-stop. The application does not use the vacuum sensor to release the part.

### 6.4 Job tab

Use the Job tab to place a full board or panel.

- **Run:** **Start job**, **Pause**, **Resume**, **Stop** and **Reset statuses**. A progress bar and the counts of placed, failed and skipped parts.
- **Import:** reads a KiCad position file (`.pos` or `.csv`).
- **Board:** sets the board position and rotation from two **reference points** that you measure with the down camera (see [7.6](#76-run-a-job)).
- **Fiducials:** the fiducial marks of the board. The application measures them with the down camera and corrects the board origin and rotation (see [7.7](#77-use-fiducials)).
- **Panel:** a grid of boards (rows, columns, step X, step Y). You can skip a bad board.
- **Parts:** a table of all parts on all boards. For each part you can:
  - Select a different nozzle or a different feeder.
  - Set **Vision**: **Auto** (the nozzle rule decides), **On** or **Off**.
  - Skip the part.
  - Move the camera to the part (**Go to**).
  - Set a placed or failed part back to "to do".

  The **Problem** column shows why a part cannot be placed.

### 6.5 Log tab

The Log tab shows the last 200 messages, with the newest message first. Errors are red. **Clear log** removes all messages.

### 6.6 Defaults tab

Use the Defaults tab to change the default values of the machine. You do not have to edit `machine.yaml` in a text editor.

The values are in groups:

- **Pick & place:** default pick depth, default place depth, pick dwell, place dwell, blow-off time, vacuum settle, vacuum sensor check, needle settle, needle sensor timeout and the discard bin position.
- **Bottom vision:** vision on or off, up LEDs, camera settle, brightness threshold, search radius, nozzle tip diameters, size tolerance, maximum part offset and maximum part angle.
- **Soft limits:** the X range, the Y range and the lowest point of each nozzle.
- **Motion:** the speed estimates, the extra move time and the jog settings before homing.
- **Advanced** (closed by default): homing, steps per mm, the controller connection and motion internals.

> **CAUTION:** The Advanced values were measured on this machine. An incorrect value can make all moves the wrong length, stop the homing or stop the connection to the controller. Change these values only if you know the correct value.

Each value shows a short help text and the built-in default. A changed field has a coloured border. **Default** sets one field back to the built-in default. **Revert** cancels all changes that you did not save. See [7.10](#710-change-the-machine-defaults).

### 6.7 Settings tab

- **Appearance:** System, Dark or Light colour theme.
- **Nozzle rules:** rules that select the nozzle for each package (see [7.5](#75-set-the-nozzle-rules)).
- **Config files:** **Reload config** reads `machine.yaml` and `cameras.yaml` again. You do not have to restart the server.

---

## 7. Procedures

### 7.1 Calibrate the machine

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

> **NOTE:** Lower the needle only with **Lower needle** on the Setup tab. If you lower it with the **Outputs** card on the Machine tab, the head cannot move until you lift the needle.

### 7.2 Make a feeder bank

1. Go to the Feeders tab.
2. In the **Banks** card, click **Add bank**. Type an ID (for example `back`) and a name.
3. Open **Generate lanes** for the bank.
4. Move the down camera to the first pocket of lane 1. Click **Use camera position**.
5. Set these values:
   - **Lane direction:** the direction from lane 1 to lane 2.
   - **Feed direction:** the direction that the tape moves when the needle pulls it.
   - **Sprocket side:** the side of the pocket where the sprocket holes are.
   - **Lanes**, **width** and **pitch** of each lane.
   - **Pick depth:** the default comes from the Defaults tab.
6. Click **Generate lanes**.
7. In the feeder table, select each lane that has parts. Type the **Value** and the **Package** as they are in the KiCad file. Click **Save feeder**.
8. Click **Save layout**. Type a name for the layout.

> **NOTE:** The needle point of each lane is calculated from the tape standard. Look at one lane before the first job. If the needle point is not correct, select the lane, put the crosshair on the sprocket hole and click **Teach prick from camera**.

When you import a job, the application finds the feeder for each part automatically. It uses the **Value** and the **Package** of the feeder.

### 7.3 Change a stack

1. Install the stack on the machine.
2. Go to the Feeders tab.
3. In the bank of that stack, select the layout in the list.
4. Click **Load layout**.

### 7.4 Export and import the feeders

Use this procedure to keep the feeder setup of each project. Then you do not have to set up the feeders again.

To export:

1. Go to the Feeders tab.
2. Click **Export**. The browser downloads one YAML file. The file contains all feeders, all banks and all saved layouts.

To import:

1. Go to the Feeders tab.
2. Set the import options:
   - **Merge (keep feeders not in the file):** off (default) replaces all feeders and banks with the contents of the file. On keeps the feeders that are not in the file and replaces the feeders with the same ID.
   - **Reset part counts:** on (default) sets the part count of each feeder to the start value.
3. Click **Import…** and select the file.
4. Read the message and click the button to confirm.

The application checks all of the file before it changes a feeder. If the file has an error, nothing changes.

### 7.5 Set the nozzle rules

The machine places each part with one nozzle. Nozzle 1 has the thick tip. Nozzle 2 has the thin tip.

1. Go to the Settings tab.
2. In **Nozzle rules**, select the default nozzle.
3. Add or change the rules. A rule has:
   - A package pattern.
   - A nozzle.
   - A body size (optional): the length and the width of the part as the up camera sees it, with the leads.
   - A place depth (optional). If it is empty, the application uses the default place depth from the Defaults tab.
   - A **Vision** setting: on, to inspect the part with the up camera.
   - A note.
4. Click **Save rules**.

The first rule that agrees with the package is used. The `*` character agrees with all text. For example, `*0402*` agrees with `C_0402_1005Metric`.

### 7.6 Run a job

Before you start:

- Calibrate the machine (see [7.1](#71-calibrate-the-machine)).
- Make sure that the feeders have the parts of the job.
- Home the machine.

Procedure:

1. In KiCad, export the component position file (ASCII `.pos` or CSV, in millimetres).
2. Go to the Job tab.
3. In **Import**, select the file and click **Import file**.
4. In **Panel**, set the rows, the columns and the step. Click **Make grid**. For one board, use 1 row and 1 column.
5. Set the board with two reference points. In the **Board** card, find **Reference points**:
   1. **Point 1 · origin:** the board position is 0, 0 (the KiCad origin). Change it if you use a different point. Move the down camera onto that point on the board. Click **Capture camera**. The machine position fields get the camera position (for example X 150, Y 100).
   2. **Point 2:** type the board position of a second point (for example a mounting hole), or select a part to use its centre. Move the down camera onto that point. Click **Capture camera**.
   3. Select the **Board** of the panel that you measured.
   4. Click **Set board from points 1 and 2**.
   5. Read the result. It compares the distance between the two points on the board with the measured distance. The two values must be almost the same.

   Use two points that are far apart. They must be at least 3 mm apart. You can also type the machine positions.
6. Look at the **Parts** table. Make sure that no part shows a problem. Skip a part, select a different feeder or set **Vision** if necessary.
7. Click **Start job**.

The reference points are saved with the job. The machine positions are not saved: capture them again each time you put a board on the machine.

During the job:

- **Pause** stops the job after the current part.
- **Stop** stops the job after the current part and goes back to idle.
- **Resume** continues from the next part.

If a pick fails, the application tries the pick two more times. For a drag feeder, it moves the tape before each new try. After three failures, the application marks the part as failed and pauses the job.

If a feeder is empty, the application pauses the job. Fill the feeder, then click **Resume**.

**Bottom vision.** After each pick, the application moves the part above the up camera and turns on the up LEDs. It measures the position, the angle and the size of the part on the nozzle. Then it corrects the place move. The application discards the part and tries the pick again if:

- It does not see a part.
- The part size is not the body size in the nozzle rules.
- The part is more than the maximum offset from the nozzle centre (default 1 mm).
- The part is turned more than the maximum angle (default 15°).

You can turn bottom vision on or off at three levels:

- For one part: the **Vision** column in the **Parts** table (**Auto**, **On** or **Off**).
- For a package: **Vision** in the nozzle rules.
- For all parts: **Bottom vision on** on the Defaults tab.

The **Last part** line on the Run card shows the result of the last inspection.

The application records the status of each part in `job.yaml` after each placement. If the server stops, start it again, home the machine and click **Start job**. The job continues from the first part that is not placed.

> **NOTE:** The application does not place bottom-side parts. It skips these parts automatically.

### 7.7 Use fiducials

Fiducials are small round copper marks on the board. The application finds them with the down camera. It then calculates the exact board origin and rotation.

When you import a KiCad file, the application finds the fiducials automatically. A part is a fiducial if its reference starts with `FID` (for example `FID1`), or if its package name contains `Fiducial`.

1. Import the job and set the board with the reference points (see [7.6](#76-run-a-job)). An error of 1 to 2 mm is satisfactory.
2. On the Job tab, find the **Fiducials** card.
3. Select the mode:
   - **Panel:** the application measures the fiducials on the first board and on the last board. It calculates one position for the full panel.
   - **Each board:** the application measures the fiducials on each board. Use this mode for a panel that is not accurate.
   - **Off:** the application uses the origin and the rotation that you set.
4. Make sure that the **Diameter** is the copper diameter of the fiducials (for example 1 mm).
5. Click **Measure fiducials**. Alternatively, click **Start job**: the job measures the fiducials before the first part.
6. Look at the result. **RMS** must be small (less than approximately 0.05 mm). **Correction** shows the distance between your rough origin and the measured position of each fiducial.

If the application cannot find a fiducial, it stops and shows a message. Make sure that the origin is approximately correct and that the diameter is correct. Then try again.

When you change the origin, the rotation or the panel, the application removes the measurement. Measure the fiducials again.

### 7.8 Clear a stuck needle alarm

The needle sensor tells the application if the needle is up or down. If the needle does not go up after a feed, the application:

- Stops the feed, the pick or the job that is in progress.
- Starts the machine buzzer.
- Opens the **Alarm** window. The window shows the needle sensor value.

Procedure:

1. Click **Silence buzzer** to stop the buzzer. The alarm stays open.
2. Free the needle by hand. Make sure that **Needle sensor** in the alarm window shows **UP**.
3. Click **Clear and continue**. The application continues the feed, the pick or the job that was in progress.

To stop the work instead, click **Abort**. An E-stop also ends the alarm.

If the needle does not go down when it must, the application lifts it, does not drag the tape and shows an error.

### 7.9 Set the Z zero

The two nozzles are on a seesaw. At Z = 0 the two nozzle tips are level. A positive Z lowers nozzle 1. A negative Z lowers nozzle 2. Each nozzle can go down 9.5 mm.

Z has no home switch. If a nozzle goes past its end, the motor loses steps and the zero moves.

1. Jog Z until the two nozzle tips are level.
2. Click **Set Z zero here** on the Machine tab.

This button does not move the machine.

### 7.10 Change the machine defaults

1. Go to the Defaults tab.
2. Change the values. For a pair of values (for example the X range), fill in the two fields. To remove the discard bin, make the two fields empty.
3. Click **Save defaults**. If you changed an Advanced value, read the message and confirm.

The application:

- Checks all values. If a value is not correct (for example a minimum that is more than the maximum), it shows a message and saves nothing.
- Writes only the changed values to `config/machine.yaml`. The comments in the file stay.
- Uses the new values immediately. You do not have to restart the server.

After you save:

- A change to the controller IP address or port needs a restart of the server.
- After a change to steps per mm or to the homing registers, home the machine again.

---

## 8. Configuration files

All files are in the `config/` folder. The application writes each file in one operation, so a power failure cannot leave a half-written file.

| File | Contents | Who changes it |
|---|---|---|
| `machine.yaml` | Controller address, steps per mm, soft limits, homing, motion, head timing, pick and place depths, discard position, bottom vision, HTTP server | The Defaults tab, or you (text editor) |
| `cameras.yaml` | Camera devices (by serial number), resolution, rotation, mirror | You (text editor) |
| `calibration.yaml` | Camera scales, nozzle offsets, needle offset, up camera positions | The Setup tab |
| `feeders.yaml` | All feeders and bank lanes | The Feeders tab |
| `banks.yaml` | Banks and their saved layouts | The Feeders tab |
| `packages.yaml` | Nozzle rules | The Settings tab |
| `job.yaml` | The current job, the reference points and the status of each part | The Job tab |
| `udev/90-tvm802-cameras.rules` | Linux rule that stops USB power saving on the cameras | You (install one time) |

If you change `machine.yaml` or `cameras.yaml` in a text editor, click **Reload config** on the Settings tab. A change to the controller address or the HTTP server needs a restart of the server.

Values measured on this machine (in `machine.yaml`):

| Value | Setting |
|---|---|
| X soft limits | −120 mm to 299 mm |
| Y soft limits | −75 mm to 350.5 mm |
| Lowest point of each nozzle | 9.5 mm |
| Down camera scale | 0.0236 mm/pixel (in `calibration.yaml`) |

---

## 9. Troubleshooting

### 9.1 The machine is not connected

- After you turn the machine off and on, the application connects again automatically. If the controller does not reply three times, the application closes the connection and tries again.
- To connect immediately, click **Connect** in the top bar.
- Put the mouse on the connection status to see the last error.
- Make sure that the Ethernet cable is connected and that the computer address is in the `192.168.0.x` network.

### 9.2 A camera view stays empty

1. Look at the status text above the camera view.
2. If the status shows "cannot open", or the view stays empty for more than 10 seconds, the camera USB connection can be stopped. Disconnect that camera, wait 2 seconds and connect it again. The application opens the camera again automatically.

   > **NOTE:** Do not use USB port 4 for the cameras.
3. After a computer restart, USB power saving can come on again. To apply the camera rule again, use this command:

   ```bash
   sudo udevadm trigger --action=add --subsystem-match=usb --attr-match=idVendor=045e
   ```

4. If the two views stay empty but the status shows a frame rate, reload the browser page.

### 9.3 Homing stops on a limit switch

The controller sometimes ignores a move that comes immediately after a stop. The application checks that each homing move starts, and sends the move again up to three times. If homing still stops, click **Home machine** again.

### 9.4 The head does not move

- Look at the **Needle** light on the Machine tab. If the needle is down, the head does not move. Lift the needle with the **Outputs** card.
- Make sure that the machine is homed. **Go to** and clicks in the camera views need a homed machine.
- Make sure that no job runs.

### 9.5 A picked part falls off

The vacuum stays on from the pick until the place. By default, the application does not use the vacuum sensor to check a pick (**Check vacuum sensor** on the Defaults tab is off). If you turn the check on and the sensor does not see the part, the application releases the part.

---

## 10. Command-line tools

| Command | Function |
|---|---|
| `.venv-linux/bin/tvm-pnp` | Starts the server. This is the same as `python -m pnp.api`. |
| `.venv-linux/bin/tvm-sim --port 7001` | Starts only the simulated controller. |
| `.venv-linux/bin/tvm-monitor` | Shows the live state of the machine. It does not send commands that move the machine. |
| `.venv-linux/bin/tvm-monitor --raw --unknown` | Also shows the raw state data and the unknown replies. Use this to find unknown signals, for example the front panel keys. |
| `.venv-linux/bin/tvm-monitor --host 127.0.0.1 --port 7001` | Shows the state of the simulated controller. |

When the server runs, `http://127.0.0.1:8080/api/debug/raw` shows the raw state bytes of the controller.

---

## 11. Tests

To run all tests, use this command:

```bash
.venv-linux/bin/python -m pytest -q
```

The tests use the simulator. They do not need a machine or cameras. There are 455 tests. A full run takes approximately 8 minutes.

> **NOTE:** On Windows, the test `test_camera.py::test_device_holders_finds_other_process` fails. This test is for Linux only.

---

## 12. Status and planned features

| Phase | Function | Status |
|---|---|---|
| 0 | Protocol and simulator | Complete |
| 1 | Machine control and manual control interface | Complete. Tested on the machine. |
| 2 | Cameras and calibration | Complete. The down camera scale and the nozzle 2 offset are calibrated on the machine. |
| 3 | Feeders and manual pick and place | Complete. Pick, feed and place tested on the machine. |
| 4 | KiCad jobs, feeder banks, panels, placement without vision | Complete in the simulator. Job import tested on the machine. |
| 5 | Fiducials | Complete in the simulator. Not tested on the machine. |
| 6 | Bottom vision | Complete in the simulator. Not tested on the machine. |
| 7 | Improvements | Planned |

### 12.1 Planned features

- **Improvements (phase 7):**
  - Speed settings for each package.
  - Compensation for nozzle runout.
  - Camera check of the sprocket holes on the feeders.
  - Statistics for each job.
- **Front panel keys:** use the jog keys and the start and stop keys on the machine front panel. The signals of these keys are not known yet. Use `tvm-monitor --raw --unknown` on the machine to find them.
- **Needle back-off:** move the head a small distance back after a drag, to release the needle from the tape. Not started.

### 12.2 Work to do on the machine

Do these tasks before the first job on the machine:

1. Calibrate the nozzle 1 offset, the prick needle offset, the up camera scale and the up camera positions (see [7.1](#71-calibrate-the-machine)).
2. Set the discard bin position (Defaults tab, **Discard bin X, Y**).
3. Measure the pick depth and the place depth. The current value of each depth is 3.0 mm, which is only an estimate. Set the values on the Defaults tab.
4. Measure the X and Y speed. The move time limits use an estimated speed of 25 mm/s.
5. Teach the origin of each stack. Make sure that the needle point of one lane is correct.
6. Before you use bottom vision: make sure that a positive A turns the part counterclockwise (seen from above), and that parts show brighter than the nozzle on the up camera.
7. Place a first board. Make sure that the part rotation from KiCad agrees with the tape orientation.

---

## 13. Known limits

- **2 mm pitch tape (0402 and 0201):** the sprocket holes are 4 mm apart. After a feed of 2 mm, no hole is below the needle point. The method for 2 mm pitch is not known yet.
- **Esc key:** Esc does an E-stop on all screens, also when a dialog is open.
- **Speed:** the application cannot change the machine speed yet. The data that sets the speed is not known.
- **One job:** the application keeps one job at a time. When you import a new file, the application replaces the current job.
- **Vacuum sensor:** on this machine, the vacuum sensor does not show a held part. The pick check with the sensor is off by default.
- **Cameras:** the USB cameras can stop when they open. Then you must disconnect the camera and connect it again (see [9.2](#92-a-camera-view-stays-empty)).
- **Browser dialogs:** the web interface uses its own confirmation windows. It does not use the browser `alert`, `confirm` or `prompt` dialogs, because some browser panes block them.

---

## 14. Recent changes

Changes from the work on the machine (November 2026):

- **Defaults tab:** a new tab to change all default values of the machine (pick and place depths, timing, vision, soft limits, motion). Advanced values are in a closed section with a warning. The values are saved in `machine.yaml` with the comments kept, and are used immediately.
- **Default pick depth:** a new value (`head.pick_depth`, 3 mm) for new feeders and bank lanes.
- **Camera views on all tabs:** the down and up camera views are in the top bar. They stay on the screen on all tabs and when you scroll. The camera views on the separate tabs are removed.
- **Machine tab layout:** the Outputs card is next to the Jog card.
- **Two-point board setup:** the board position and rotation come from two reference points that you capture with the down camera. Point 1 is the board origin (0, 0 by default). This replaces **Set origin from camera** and **Align rotation on part**.
- **Vision for each part:** the Parts table has a **Vision** column (Auto, On, Off).
- **Feeder export and import:** save and load all feeders, banks and layouts as one file, with Replace or Merge.
- **Feeders tab actions:** a **Place target** with X, Y, rotation and depth, and a **Vision check** On/Off switch.
- **Needle sensor:** the application reads the needle position sensor. It waits until the needle is really down before a drag, and until it is really up before the next move.
- **Needle interlock:** the head does not move when the needle is down, except during a feed and in the Setup needle step.
- **Stuck needle alarm:** the buzzer and an alarm window with **Silence buzzer**, **Clear and continue** and **Abort**.
- **Vacuum:** the vacuum stays on from the pick until the place. The vacuum sensor check is off by default. The vacuum outputs of nozzle 1 and nozzle 2 were the wrong way around; this is corrected.
- **Z:** the seesaw Z is documented, the lowest point of each nozzle is 9.5 mm, and **Set Z zero here** sets the Z zero without a move.
- **Soft limits:** X is −120 mm to 299 mm, Y is −75 mm to 350.5 mm (measured).
- **Connection:** the application connects again automatically after a machine power cycle. A **Connect** button connects immediately.
- **Homing:** each homing move is checked and sent again if the controller ignores it.
- **Down camera calibration:** works with the red glow of the head in the image (high-pass filter). Measured scale: 0.0236 mm/pixel.
- **Auto-centre on mark:** more reliable; it does not drive away from the mark.
- **Cameras:** a camera that stops when it opens does not block the other camera for more than 10 seconds.
