var net = require('net');

var settings = {
	tvmPort 			: 701,
	tvmIP				: '192.168.0.8',
	//tvmIP				: '127.0.0.1',		// Emulator
	delayBeforePolling 	: 1000,
	pollingInterval		: 20,
	enablePolling		: false,
	stepsPerMM			:{
		X		:	32808,
		Y		:	32808,
		A1		:	4444.44,	// Steps per Degree for Nozzle 1 Rotation
		A2		:	4444.44,	// Steps per Degree for Nozzle 2 Rotation
		Nozzle	:	32808		// Seesaw Z axis (Positive = N1 down, Negative = N2 down)
	},
	buzzerOnLimit 	: false,
	debugLog			: false		// set true to log every poll / status message
}

var state = {
	last_reply	: "2016/01/01 00:00",
	VacuumPump 	: 0,
	Vacuum1 	: 0,
	Vacuum2 	: 0,
	Blowing1 	: 0,
	Blowing2 	: 0,
	Pressure1	: 0,
	Pressure2	: 0,
	Buzzer		: 0,
	Prick		: 0,
	Leds		: 0,
	Position	: {
		X 		: 0,
		Y 		: 0,
		A1 		: 0,
		A2 		: 0,
		Nozzle 	: 0
	},
	PositionRaw	: {
		X 		: 0,
		Y 		: 0,
		A1 		: 0,
		A2 		: 0,
		Nozzle 	: 0
	},
	Limit : {
		Top 	: 0,
		Bottom 	: 0,
		Left 	: 0,
		Right 	: 0
	},
	SpeedMode	: "Slow"	
};

var commands = {
	poll 		: new Buffer([0x01, 0x00, 0x00, 0x00, 0xf4, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]),	
	reqState	: new Buffer([0x00, 0x00, 0x00, 0x00]),
	
	BuzzerOn 	: new Buffer([0x14, 0x00, 0x00, 0x00, 0x00, 0x08, 0x00, 0x00]),
	BuzzerOff	: new Buffer([0x15, 0x00, 0x00, 0x00, 0x00, 0x08, 0x00, 0x00]),
	
	PrickOn 	: new Buffer([0x14, 0x00, 0x00, 0x00, 0x00, 0x40, 0x00, 0x00]),
	PrickOff 	: new Buffer([0x15, 0x00, 0x00, 0x00, 0x00, 0x40, 0x00, 0x00]),

	PumpOn		: new Buffer([0x14, 0x00, 0x00, 0x00, 0x08, 0x04, 0x00, 0x00]),
	PumpOff		: new Buffer([0x15, 0x00, 0x00, 0x00, 0x08, 0x04, 0x00, 0x00]),
	
	Blowing1On	: new Buffer([0x14, 0x00, 0x00, 0x00, 0x02, 0x00, 0x00, 0x00]),
	Blowing1Off	: new Buffer([0x15, 0x00, 0x00, 0x00, 0x02, 0x00, 0x00, 0x00]),
	
	Blowing2On	: new Buffer([0x14, 0x00, 0x00, 0x00, 0x00, 0x10, 0x00, 0x00]),
	Blowing2Off	: new Buffer([0x15, 0x00, 0x00, 0x00, 0x00, 0x10, 0x00, 0x00]),
	
	Vacuum1On	: new Buffer([0x14, 0x00, 0x00, 0x00, 0x04, 0x00, 0x00, 0x00]),
	Vacuum1Off	: new Buffer([0x15, 0x00, 0x00, 0x00, 0x04, 0x00, 0x00, 0x00]),

	Vacuum2On	: new Buffer([0x14, 0x00, 0x00, 0x00, 0x00, 0x20, 0x00, 0x00]),
	Vacuum2Off	: new Buffer([0x15, 0x00, 0x00, 0x00, 0x00, 0x20, 0x00, 0x00]),
	
	LedsOn		: new Buffer([0x14, 0x00, 0x00, 0x00, 0x00, 0x02, 0x00, 0x00]),
	LedsOff		: new Buffer([0x15, 0x00, 0x00, 0x00, 0x00, 0x02, 0x00, 0x00]),

	StopX		: new Buffer([0x09, 0x00, 0x04, 0x00]),
	StopY		: new Buffer([0x09, 0x00, 0x10, 0x00]),
	StopZ		: new Buffer([0x09, 0x00, 0x02, 0x00]),
	StopA 		: new Buffer([0x09, 0x00, 0x08, 0x00]),
	StopOnLimitHit:   new Buffer([0x09, 0x00, 0x3f, 0x00]),

	SpeedSetup	: new Buffer([0x06, 0x00, 0x04, 0x00, 0x03, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0xa0, 0x0f, 0x00, 0x00, 0xb8, 0x0b, 0x00, 0x00, 0x88, 0x13, 0x00, 0x00, 0xa0, 0x0f, 0x00, 0x00, 0x88, 0x13, 0x00, 0x00, 0xa0, 0x0f, 0x00, 0x00]),

};

// ####################### TVM Connection #######################
var tvmClient = new net.Socket();
tvmClient.on('connect', function(data) {
	console.log('TVM connected.');
	
	tvmClient.write(new Buffer([0x5, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0]));
	tvmClient.write(new Buffer([0x15, 0x0, 0x0, 0x0, 0x8, 0x0, 0x0, 0x0]));	
	tvmClient.write(new Buffer([0x14, 0x0, 0x0, 0x0, 0x0, 0x1, 0x0, 0x0]));
	tvmClient.write(new Buffer([0x14, 0x0, 0x0, 0x0, 0x0, 0x2, 0x0, 0x0]));
	tvmClient.write(new Buffer([0x0b, 0x0, 0x0, 0x0]));
	tvmClient.write(new Buffer([0x15, 0x0, 0x0, 0x0]));
	tvmClient.write(new Buffer([0x48, 0x0, 0x0, 0x0]));		
	
	setTimeout(
		function(){ 
			if(settings.enablePolling){
				setInterval(function(){ poll();}, settings.pollingInterval);
			}
			setInterval(function(){ getState();}, settings.pollingInterval * 10);
		}, 
		settings.delayBeforePolling
	);		
});

tvmClient.on('close', function() { console.log('TVM connection closed.'); });
tvmClient.on('error', function (err) { console.log("TVM Connection - " + err); });
tvmClient.on('data', function(data) { processMessage(data); });

// ####################### TVM Main functions #######################
function poll(){ if(settings.debugLog){ console.log("Sending Poll."); } tvmClient.write(commands.poll); }
function getState(){ if(settings.debugLog){ console.log("Getting State."); } tvmClient.write(commands.reqState); }

function processMessage(msg){
	switch(msg[0]) {
		case 0x00: updateState(msg); break;
		case 0x01: break;
		default: break;
	}
}

function updateState(msg){
	if(msg.length !== 44){ return; }
	
	if((msg[4] & 8) === 8){ state.VacuumPump = 1; } else { state.VacuumPump = 0; }
	if((msg[4] & 4) === 4){ state.Vacuum1 = 1; } else { state.Vacuum1 = 0; }
	if((msg[5] & 32) === 32){ state.Vacuum2 = 1; } else { state.Vacuum2 = 0; }

	if((msg[4] & 2) === 2){ state.Blowing1 = 1; } else { state.Blowing1 = 0; }
	if((msg[5] & 16) === 16){ state.Blowing2 = 1; } else { state.Blowing2 = 0; }
	
	if((msg[5] & 8) === 8){ state.Buzzer = 1; } else { state.Buzzer = 0; }
	if((msg[5] & 64) === 64){ state.Prick = 1; } else { state.Prick = 0; }
	
	state.Limit = { Top : 0, Bottom : 0, Left : 0, Right : 0 };
	if((msg[8] & 4) === 4){ state.Limit.Top = 1; }
	if((msg[8] & 8) === 8){ state.Limit.Bottom = 1; }
	if((msg[8] & 2) === 2){ state.Limit.Left = 1; }
	if((msg[8] & 1) === 1){ state.Limit.Right = 1; } 
	
	if((msg[5] & 2) === 2){ state.Leds = 1; } else { state.Leds = 0; }
	
	if((msg[8] & 16) === 16) { state.Pressure1 = 0; } else { state.Pressure1 = 1; }
	if((msg[10] & 16) === 16){ state.Pressure2 = 0; } else { state.Pressure2 = 1; }
	
	var msgStr = msg.toString('hex');

	state.PositionRaw.X  = parseInt(BuildNumber(msgStr.substring(40, 48)), 16);
	state.PositionRaw.Y  = parseInt(BuildNumber(msgStr.substring(56, 64)), 16);
	state.PositionRaw.A1 = parseInt(BuildNumber(msgStr.substring(48, 56)), 16);
	state.PositionRaw.A2 = parseInt(BuildNumber(msgStr.substring(64, 72)), 16);
	
	// Read signed 32-bit integer for Z (Nozzle seesaw) position
	var nozzleHex = BuildNumber(msgStr.substring(32, 40));
	var buf = new Buffer(nozzleHex, 'hex');
	state.PositionRaw.Nozzle = buf.readInt32BE(0);
	
	state.Position.X      = state.PositionRaw.X / settings.stepsPerMM.X;
	state.Position.Y      = state.PositionRaw.Y / settings.stepsPerMM.Y;
	state.Position.A1     = state.PositionRaw.A1 / settings.stepsPerMM.A1;
	state.Position.A2     = state.PositionRaw.A2 / settings.stepsPerMM.A2;
	state.Position.Nozzle = state.PositionRaw.Nozzle / settings.stepsPerMM.Nozzle;
	
	if((msg[5] & 1) === 1 && (msg[4] & 128) !== 128) { state.SpeedMode = "Slow"; }
	if((msg[5] & 1) !== 1 && (msg[4] & 128) === 128) { state.SpeedMode = "Fast"; }
}

function BuildNumber(value){
	return value.substring(6,8) + value.substring(4,6) + value.substring(2,4) + value.substring(0,2);		
}

function StopAll(){
	tvmClient.write(commands.StopX);
	tvmClient.write(commands.StopY);
	tvmClient.write(commands.StopZ);
	tvmClient.write(commands.StopA);
}

// ####################### Movement #######################

var axes = {
	Nozzle : { selector : 0x02, slot : 1, direction : 1, limitHit : function(){ return 0; }, homeRegister : 0 },
	X      : { selector : 0x04, slot : 2, direction : 1, limitHit : function(){ return (state.Limit.Left + state.Limit.Right) > 0 ? 1 : 0; }, homeRegister : 9843000 },
	A1     : { selector : 0x08, slot : 3, direction : 1, limitHit : function(){ return 0; }, homeRegister : 0 },
	Y      : { selector : 0x10, slot : 4, direction : 1, limitHit : function(){ return state.Limit.Top; }, homeRegister : 11532000 },
	A2     : { selector : 0x20, slot : 5, direction : 1, limitHit : function(){ return 0; }, homeRegister : 0 }
};

// 28-byte axis frame builder using signed 32-bit integers to support negative target steps
function BuildAxisFrame(msgType, axis, rawValue){
	var msg = new Buffer(28);
	msg.fill(0x00);
	msg[0] = msgType;
	msg[2] = axes[axis].selector;
	msg.writeInt32LE(rawValue, 4 + (axes[axis].slot * 4));
	return msg;
}

function BuildMultiAxisFrame(msgType, targetSteps){
	var msg = new Buffer(28);
	msg.fill(0x00);
	msg[0] = msgType;
	
	var selectorMask = 0;
	for(var axis in targetSteps){
		if(axes[axis] !== undefined) {
			selectorMask |= axes[axis].selector;
			var rawValue = targetSteps[axis];
			msg.writeInt32LE(rawValue, 4 + (axes[axis].slot * 4));
		}
	}
	msg[2] = selectorMask;
	return msg;
}

function JogAxis(axis, deltaUnits){
	console.log("Axis:", axis);
	console.log("Delta Units:", deltaUnits);
	var delta = parseFloat(deltaUnits);
	if(isNaN(delta)){ return -1; }

	var targetSteps = Math.round(state.PositionRaw[axis] + (delta * settings.stepsPerMM[axis]));
	//if(axis !== 'Nozzle' && targetSteps < 0){ targetSteps = 0; }

	var msg = BuildAxisFrame(0x08, axis, targetSteps);
	console.log("Jog " + axis + " " + delta + " -> raw target " + targetSteps);
	Move(msg);
	return 1;
}

function processMoveRequest(query){
	var keys = Object.keys(query);
	if(keys.length === 0){ return -1; }

	// Jogging commands
	if(keys.length === 1) {
		switch (keys[0]){
			case 'StepY': return JogAxis('Y', query.StepY);
			case 'StepX': return JogAxis('X', query.StepX);
			case 'StepZ':
			case 'StepZ1':
			case 'StepNozzle': return JogAxis('Nozzle', query[keys[0]]);
			case 'StepZ2': return JogAxis('Nozzle', -parseFloat(query.StepZ2));
			case 'StepA':
			case 'StepA1': return JogAxis('A1', query[keys[0]]);
			case 'StepA2': return JogAxis('A2', query[keys[0]]);
			case 'StopAll': StopAll(); return 1;
		}
	}

	// Absolute Moves
	var targetSteps = {};
	var absoluteMove = false;
	
	for(var key in query) {
		var axisKey = key.toUpperCase();
		var formattedAxis = "";
		var targetVal = parseFloat(query[key]);

		if(isNaN(targetVal)) continue;

		if(axisKey === 'X') { formattedAxis = 'X'; }
		else if(axisKey === 'Y') { formattedAxis = 'Y'; }
		else if(axisKey === 'Z' || axisKey === 'NOZZLE' || axisKey === 'Z1') { 
			formattedAxis = 'Nozzle'; 
			// Positive Z lowers Nozzle 1
		} 
		else if(axisKey === 'Z2') { 
			formattedAxis = 'Nozzle'; 
			// Z2 down is represented as negative steps on the seesaw Z motor
			targetVal = -Math.abs(targetVal); 
		} 
		else if(axisKey === 'A' || axisKey === 'A1') { formattedAxis = 'A1'; }
		else if(axisKey === 'B' || axisKey === 'A2') { formattedAxis = 'A2'; }

		if(formattedAxis !== "") {
			if(formattedAxis === 'A1' || formattedAxis === 'A2') {
				targetVal = ((targetVal % 360) + 360) % 360;
			}

			var steps = Math.round(targetVal * settings.stepsPerMM[formattedAxis]);
			//if(formattedAxis !== 'Nozzle' && steps < 0) { steps = 0; }

			targetSteps[formattedAxis] = steps;
			absoluteMove = true;
		}
	}

	if(absoluteMove) {
		var msg = BuildMultiAxisFrame(0x08, targetSteps);
		console.log("Absolute Move -> raw targets:", targetSteps);
		Move(msg);
		return 1;
	}
	
	return -1;
}

function Move(msg){
	tvmClient.write(commands.SpeedSetup);
	tvmClient.write(msg);
}

function processAPIRequest(query){		
	var keys = Object.keys(query);
	if(keys.length > 1 || keys.length === 0){ return -1; }
	
	switch (keys[0]){
		case 'buzzer':
			if(query[keys[0]] === '1'){ tvmClient.write(commands.BuzzerOn); } else { tvmClient.write(commands.BuzzerOff); }
			return 1;
		case "prick":
			if(query[keys[0]] === '1'){ tvmClient.write(commands.PrickOn); } else { tvmClient.write(commands.PrickOff); }
			return 1;
		case "pump":
			if(query[keys[0]] === '1'){ tvmClient.write(commands.PumpOn); } else { tvmClient.write(commands.PumpOff); }
			return 1;
		case "blowing1":
			if(query[keys[0]] === '1'){ tvmClient.write(commands.Blowing1On); } else { tvmClient.write(commands.Blowing1Off); }
			return 1;
		case "blowing2":
			if(query[keys[0]] === '1'){ tvmClient.write(commands.Blowing2On); } else { tvmClient.write(commands.Blowing2Off); }
			return 1;
		case "vacuum1":
			if(query[keys[0]] === '1'){ tvmClient.write(commands.Vacuum1On); } else { tvmClient.write(commands.Vacuum1Off); }
			return 1;
		case "vacuum2":
			if(query[keys[0]] === '1'){ tvmClient.write(commands.Vacuum2On); } else { tvmClient.write(commands.Vacuum2Off); }
			return 1;
		case "leds":
			if(query[keys[0]] === '1'){ tvmClient.write(commands.LedsOn); } else { tvmClient.write(commands.LedsOff); }
			return 1;
		default :
			return -1;
	}
}

// ####################### Homing Routine #######################

function processHomeReq(query){
	var axis = "";
	if(query && query.axis){ axis = String(query.axis).toUpperCase(); }

	console.log("Homing Started (" + (axis || "Y then X") + ")");

	// 1. Send command to retract both Nozzles to 0 position first
	console.log("---- Safety: Retracting Nozzles before homing...");
	tvmClient.write(commands.SpeedSetup);
	tvmClient.write(BuildAxisFrame(0x08, "Nozzle", 0));

	// 2. Wait until nozzle reaches top position before proceeding
	var checks = 0;
	var checkRetraction = setInterval(function(){
		checks++;
		if(Math.abs(state.PositionRaw.Nozzle) <= 100 || checks >= 60) {
			clearInterval(checkRetraction);

			if(checks >= 60) {
				console.log("---- Warning: Nozzle retract wait timed out, continuing homing sequence.");
			} else {
				console.log("---- Nozzle retracted safely. Executing axis homing.");
			}

			// Reset rotation axes registers (A1 / A2) to 0°
			tvmClient.write(BuildAxisFrame(0x07, "A1", 0));
			tvmClient.write(BuildAxisFrame(0x07, "A2", 0));

			// 3. Pre-homing setup sequence for X / Y
			tvmClient.write(commands.StopOnLimitHit);
			tvmClient.write(new Buffer([0x0b, 0x00, 0x00, 0x00]));
			tvmClient.write(new Buffer([0x15, 0x00, 0x00, 0x00, 0x20, 0x40, 0x00, 0x00]));
			tvmClient.write(new Buffer([0x15, 0x00, 0x00, 0x00, 0x2e, 0x74, 0x00, 0x00]));
			tvmClient.write(commands.SpeedSetup);

			if(axis === "X"){
				HomeAxis("X");
			} else if(axis === "Y"){
				HomeAxis("Y");
			} else {
				HomeAxis("Y", function(){ HomeAxis("X"); });
			}
		}
	}, 50);

	return 1;
}

function HomeAxis(axis, onDone){
	var cfg     = axes[axis];
	var far     = 32808000;                                     // 1000mm - past end of travel
	var backOff = Math.round(5 * settings.stepsPerMM[axis]);    // 5mm back-off

	console.log("---- Homing " + axis);

	function FirstApproach(){
		if(cfg.direction === 1){
			tvmClient.write(BuildAxisFrame(0x08, axis, far));
		} else {
			tvmClient.write(BuildAxisFrame(0x07, axis, far));
			tvmClient.write(BuildAxisFrame(0x08, axis, 0));
		}
		WaitFor(cfg.limitHit, 1, 60000, MoveOffSwitch, Fail("limit switch never tripped"));
	}

	function MoveOffSwitch(){
		var backTarget = state.PositionRaw[axis] - (cfg.direction * backOff);
		if(backTarget < 0){ backTarget = 0; }
		tvmClient.write(BuildAxisFrame(0x08, axis, backTarget));
		WaitFor(cfg.limitHit, 0, 15000, SecondApproach, Fail("limit switch never released"));
	}

	function SecondApproach(){
		tvmClient.write(BuildAxisFrame(0x08, axis, (cfg.direction === 1) ? far : 0));
		WaitFor(cfg.limitHit, 1, 15000, SetRegister, Fail("limit switch never tripped on 2nd pass"));
	}

	function SetRegister(){
		tvmClient.write(BuildAxisFrame(0x07, axis, cfg.homeRegister));
		console.log("---- " + axis + " homed. Position register set to " + cfg.homeRegister);
		if(onDone){ onDone(); }
	}

	function Fail(reason){
		return function(){
			console.log("---- " + axis + " homing FAILED: " + reason);
			StopAll();
		};
	}

	function WaitFor(getter, target, timeout, OnSuccess, OnFail){
		NonBlockingCall(function(){}, timeout,
			{ get value() { return getter(); } },
			target, OnSuccess, OnFail
		);
	}

	FirstApproach();
}

function NonBlockingCall(Run, Timeout, Value, Target, OnSuccess, OnFail){
	Run();	
	
	function CheckForChange(target){
		if(settings.debugLog){ console.log(Value.value); }
		if(Value.value === target){
			clearTimeout(timer);
			clearInterval(checker);			
			OnSuccess();
		}
	}
	
	var checker = setInterval(function(){ CheckForChange(Target); }, 50);	
	
	var timer = setTimeout(function(){
		clearInterval(checker);			
		OnFail();		
	}, Timeout);
}

// ####################### TVM Exports #######################
module.exports = {
	init : function() {
		tvmClient.connect(settings.tvmPort, settings.tvmIP, function() {
			console.log('TVM connecting.');
		});
	},
	processAPIRequest 	: processAPIRequest,
	processMoveRequest	: processMoveRequest,
	processHomeReq		: processHomeReq,
	state : state
};
