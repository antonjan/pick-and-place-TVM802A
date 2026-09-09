# TVM802-WEB-API
Web API to control a TVM802(A) Pick and place machine..

Prerequisites
-------------
NodeJS - (https://nodejs.org/en/)

Installation
------------

Download the code and run the following command to install the needed modules 

`npm install`

Running
-------

`node TVM-API-Server-V1.js`

Usage
-----

# 🚀 Features
 * HTTP REST API: Quick controls for toggling outputs (pumps, vacuums, LEDs, solenoids), querying full machine state, manual jogging, and coordinate moves.
 * OpenPnP Integration: Emulates a standard TCP G-Code controller supporting G0/G1, G28, G90/G91, and M114.
 * Seesaw Z Axis Logic: Handles automatic calculation for the shared Z motor controlling Nozzle 1 (positive values) and Nozzle 2 (negative values).
 * Safe Homing Sequences: Features auto-retraction for nozzles prior to axis homing and double-touch limit switch registration.
⚙️ Configuration & Ports
- HTTP Web Server: 3000 REST API and Web Interface 
- TCP G-Code Socket:2222 TCP Connection for OpenPnP 

## 🌐 HTTP API Reference
All HTTP endpoints are accessible via GET requests on port 3000.
1. Hardware State Query
 * Endpoint: GET /api/status
 * Description: Returns full machine state, input sensor flags, current speed modes, raw encoder ticks, and converted millimeter/degree positions.
 * Example Response:
   {
  "VacuumPump": 0,
  "Vacuum1": 0,
  "Vacuum2": 0,
  "Blowing1": 0,
  "Blowing2": 0,
  "Leds": 1,
  "Position": { "X": 100.5, "Y": 50.2, "A1": 0, "A2": 0, "Nozzle": 0 },
  "Limit": { "Top": 0, "Bottom": 0, "Left": 0, "Right": 0 }
}

2. Output Controls
 * Endpoint: GET /api/set?<parameter>=<value>
 * Description: Toggle binary hardware outputs (1 = ON, 0 = OFF). Only one parameter per request.

- pump  1 / 0  Vacuum Pump power 
- vacuum1  1 / 0  Nozzle 1 vacuum solenoid 
- vacuum2  1 / 0  Nozzle 2 vacuum solenoid 
- blowing1  1 / 0  Nozzle 1 puff/blow solenoid 
- blowing2  1 / 0  Nozzle 2 puff/blow solenoid 
- leds  1 / 0  Machine lighting / LEDs 
- buzzer  1 / 0  Hardware buzzer sound 
- prick  1 / 0  Component pin/needle driver solenoid 
 * Example Requests:
   * http://localhost:3000/api/set?pump=1
   * http://localhost:3000/api/set?vacuum1=1
   * http://localhost:3000/api/set?leds=0
3. Motion & Jog Controls
 * Endpoint: GET /api/move?<parameter>=<value>
 * Description: Issue absolute moves or incremental axis relative steps (jogging).
Absolute Moves
Pass multiple axes in a single request (units in mm for X/Y/Z, degrees for A1/A2):

- X  mm  X-Axis Target
- Y  mm  Y-Axis Target 
- Z / Z1 / Nozzle  mm  Nozzle 1 Downward Travel (+mm) 
- Z2  mm  Nozzle 2 Downward Travel (-mm) 
- A / A1  Degrees  Nozzle 1 Rotation
- B / A2  Degrees  Nozzle 2 Rotation

 * Example Request:
   * http://localhost:3000/api/move?X=150&Y=200&Z=5&A1=90
Incremental Jogging & Emergency Stop
Pass a single jog parameter per request:
StepX  mm  Relative X movement (+ or -) 
StepY  mm  Relative Y movement (+ or -) 
StepZ / StepZ1 / StepNozzle  mm  Relative Nozzle 1 move (+ steps down) 
StepZ2  mm  Relative Nozzle 2 move (+ steps down) 
StepA / StepA1  Degrees  Relative Nozzle 1 rotation 
StepA2  Degrees  Relative Nozzle 2 rotation 
StopAll  1  Immediately stops all axis stepper motors
 * Example Requests:
   * http://localhost:3000/api/move?StepX=10 (jog X right by 10mm)
   * http://localhost:3000/api/move?StepY=-5 (jog Y back by 5mm)
   * http://localhost:3000/api/move?StopAll=1 (E-STOP)
4. Homing API
 * Endpoint: GET /api/home or GET /api/home?axis=<X|Y>
 * Description: Initiates automated safe homing. Automatically retracts both nozzles to 0\,\text{mm} before executing homing.
 * Example Requests:
   * http://localhost:3000/api/home (Homes Y, then X)
   * http://localhost:3000/api/home?axis=X (Homes X only)
## 📡 TCP G-Code Controls (OpenPnP)
Connect OpenPnP or any raw TCP terminal client to Port 2222.
Supported Commands

 G90  Set Absolute Positioning Mode  All subsequent X, Y, Z, A, B values are treated as absolute target positions.
 
 G91  Set Relative Positioning Mode  All subsequent movement values are calculated relative to current position.
 
 G0 / G1  Linear Move  Accepts X, Y, Z, A (or C), and B coordinates.
 - Z controls seesaw Z (+ lowers N1).
 - A or C controls Nozzle 1 angle.
 - B controls Nozzle 2 angle.
 Example: G1 X120.5 Y45.0 Z2.0 A90

G28  Home Axis / Machine  Safe homing sequence.
- G28 (Homes both axes)
- G28 X (Homes X axis)
- G28 Y (Homes Y axis) 

M114  Get Current Position  Returns current coordinates in standard reprap format:
X:<val> Y:<val> Z:<val> C:<val> A:<val> B:<val> ok 
