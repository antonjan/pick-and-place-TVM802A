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
		A1		:	4444.44,
		A2		:	4444.44,
		Nozzle	:	32808		
	},
	buzzerOnLimit 	: false
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
	
	

	
	
}

var beep = false;

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
		
		
		// Start Polling
		setTimeout(
			function(){ 
				
					if(settings.enablePolling){
						setInterval(function(){ poll();},settings.pollingInterval);
					}
					
					
					setInterval(function(){ getState();},settings.pollingInterval*10);
					
					/*
					setInterval(function(){
						beep = !beep;
						if(beep){
							tvmClient.write(commands.PrickOn);
						} else {						
							tvmClient.write(commands.PrickOff);
						}
						
					},1000);
					*/
			}, 
			settings.delayBeforePolling
		);		

		
	});

	tvmClient.on('close', function() {
		console.log('TVM connection closed.');
	});

	tvmClient.on('error', function (err) {
		console.log("TVM Connection - " + err );
	});

	tvmClient.on('data', function(data) {
		processMessage(data);
	});

// ####################### TVM Main functions #######################
	function poll(){
		console.log("Sending Poll.");	
		tvmClient.write(commands.poll);
	}
	
	function getState(){
		console.log("Getting State.");	
		tvmClient.write(commands.reqState);		
	}
	
	function processMessage(msg){
		console.log("  <<<<< P&P Msg <<<<<");
		switch(msg[0]) {
			case 0x00:
				console.log("Machine State");
				updateState(msg);
				break;
			case 0x01:
				console.log("Heart Beat");
				break;
			default:
				console.log("Message not processed");
				console.log(msg);
		}
	}
	
	
	function updateState(msg){
		if(msg.length !== 44){
			console.log("Invalid message");
			return;
		}
		
		if((msg[4] & 8) === 8){ state.VacuumPump = 1; } else { state.VacuumPump = 0; };
		if((msg[4] & 4) === 4){ state.Vacuum1 = 1; } else { state.Vacuum1 = 0; };
		if((msg[5] & 32) === 32){ state.Vacuum2 = 1; } else { state.Vacuum2 = 0; };

		if((msg[4] & 2) === 2){ state.Blowing1 = 1; } else { state.Blowing1 = 0; };
		if((msg[5] & 16) === 16){ state.Blowing2 = 1; } else { state.Blowing2 = 0; };
		
		if((msg[5] & 8) === 8){ state.Buzzer = 1; } else { state.Buzzer = 0; };
		if((msg[5] & 64) === 64){ state.Prick = 1; } else { state.Prick = 0; };
		
		if((msg[8] & 4) === 4){ state.Limit.Top = 1; } else { state.Limit.Top = 0; };
		if((msg[8] & 8) === 8){ state.Limit.Bottom = 1; } else { state.Limit.Bottom = 0; };
		if((msg[8] & 2) === 2){ state.Limit.Left = 1; } else { state.Limit.Left = 0; };
		if((msg[8] & 1) === 1){ state.Limit.Right = 1; } else { state.Limit.Right = 0; };
		
		state.Limit = { Top : 0, Bottom : 0 , Left : 0 , Right : 0 };
		if((msg[8] & 4) === 4){ state.Limit.Top = 1; }
		if((msg[8] & 8) === 8){ state.Limit.Bottom = 1; }
		if((msg[8] & 2) === 2){ state.Limit.Left = 1; }
		if((msg[8] & 1) === 1){ state.Limit.Right = 1; } 
		
		if((msg[5] & 2) === 2){ state.Leds = 1; } else { state.Leds = 0; };
		
		if((msg[8] & 16) === 16) { state.Pressure1 = 0; } else { state.Pressure1 = 1; };
		if((msg[10] & 16) === 16){ state.Pressure2 = 0; } else { state.Pressure2 = 1; };
		
		var msgStr = msg.toString('hex');
		
		

		state.PositionRaw.X 		= parseInt(BuildNumber(msgStr.substring(40, 48)),16);
		state.PositionRaw.Y 		= parseInt(BuildNumber(msgStr.substring(56, 64)),16);
		state.PositionRaw.A1 		= parseInt(BuildNumber(msgStr.substring(48, 56)),16);
		state.PositionRaw.A2 		= parseInt(BuildNumber(msgStr.substring(64, 72)),16);
		state.PositionRaw.Nozzle 	= parseInt(BuildNumber(msgStr.substring(32, 40)),16);
		
		state.Position.X 		= state.PositionRaw.X /  settings.stepsPerMM.X;
		state.Position.Y 		= state.PositionRaw.Y /  settings.stepsPerMM.Y;
		state.Position.A1 		= state.PositionRaw.A1 / settings.stepsPerMM.A1;
		state.Position.A2 		= state.PositionRaw.A2 / settings.stepsPerMM.A2;
		state.Position.Nozzle 	= state.PositionRaw.Nozzle / settings.stepsPerMM.Nozzle;
		
		
		if((msg[5] & 1) === 1 && (msg[4] & 128) !== 128) {state.SpeedMode = "Slow";};
		// Slow bit is not always setting
		if((msg[5] & 1) !== 1 && (msg[4] & 128) === 128) {state.SpeedMode = "Fast";}
		
		var limitState = LimitsTriggered();

		if(settings.buzzerOnLimit){
			if(limitState){
				if(state.Buzzer === 0){
					console.log("Buzzzzzzzz");					
				}
			} else {
				if(state.Buzzer === 1){
					console.log("BuzzOff");			
				}
			}
			
		}
		

		//console.log(msg);
		//console.log(state);
		
	}
	
	function LimitsTriggered(){
		return state.Limit.Top + state.Limit.Bottom + state.Limit.Left + state.Limit.Right > 0;
	}
	

	
	function convertNozzlePositionToMM(value){
		/*
		//getting nozzle position
			int nozzle = BuildNumber(RecvBuffer + 16, 4);
			
			
			double slope = 1;
			int step = 78000;
			double offset = 0;
			int nozzle_abs = abs(nozzle);
			//linear interpolation of nozzle position
			if (nozzle_abs >= 0 && nozzle_abs <= 78000) {
				slope = (5.13 - 0) / step;
				offset = 0;
			}
			else if (nozzle_abs > 78000 && nozzle_abs <= 156000) {
				slope = (9.78 - 5.13) / step;
				offset = 0.48;
			}
			else if (nozzle_abs > 156000 && nozzle_abs <= 234000) {
				slope = (13.51 - 9.78) / step;
				offset = 2.32;
			}
			else {
				slope = (15.99 - 13.51) / step;
				offset = 6.07;
			}
			
			
			if (nozzle < 0)
				offset = -offset;
			N_pos = nozzle * slope + offset;
			N_pos = round(N_pos * 100) / 100;

		*/
		var result 	= 0;
		var nozzle 	= parseInt((value.substring(6,8) + value.substring(4,6) + value.substring(2,4) + value.substring(0,2)),16);
		var slope	= 1.00;
		var step = 78000;
		var offset 	= 0.00;
		
		//linear interpolation of nozzle position
		if (nozzle >= 0 && nozzle <= 78000) {
			slope = (5.13 - 0) / step;
			offset = 0;
		}
		else if (nozzle > 78000 && nozzle <= 156000) {
			slope = (9.78 - 5.13) / step;
			offset = 0.48;
		}
		else if (nozzle > 156000 && nozzle <= 234000) {
			slope = (13.51 - 9.78) / step;
			offset = 2.32;
		}
		else {
			slope = (15.99 - 13.51) / step;
			offset = 6.07;
		}
		
		if (nozzle < 0){
			offset = -offset;
		}
		
		result = nozzle * slope + offset;
		result = Math.round(result * 100) / 100;
		
		return result;
	}
	
	function BuildNumber(value){
		return value.substring(6,8) + value.substring(4,6) + value.substring(2,4) + value.substring(0,2);		
	}
	
	function getBit(msg,index){
		var byteIndex = Math.floor(index/8);
		var bitIndex = index % 8;
		var targetByte = msg[byteIndex];
		return targetByte;		
	}

	function PositionToReversedHEX(position,stepsPerMM){
		var roundedLocation = Math.round(position*100)/100;
		var steps = Math.round(roundedLocation * stepsPerMM);
		var pad = "00000000"
		var stepsHex = steps.toString(16);
		var stepsHexPad = pad.substring(0,pad.length - stepsHex.length) + stepsHex;
	

	
		return BuildNumber(stepsHexPad);
	}

	function StepsToRevHex(steps){
		var pad = "00000000"
		var stepsHex = steps.toString(16);
		var stepsHexPad = pad.substring(0,pad.length - stepsHex.length) + stepsHex;
	
		return BuildNumber(stepsHexPad);		
	}
	
	
	function StopAll(){
		tvmClient.write(commands.StopX);
		tvmClient.write(commands.StopY);
		tvmClient.write(commands.StopZ);
		tvmClient.write(commands.StopA);
	}
	
	function processMoveRequest(query){
				var keys = Object.keys(query);
		if(keys.length > 1 || keys.length === 0){
			return -1;
		}
		
		/*
			Y
			0x08, 0x00, 0x10
			0 x 8
			pos x 4
			0 x 12
			
			X
			0x08, 0x00, 0x04
			0 x 8
			pos x 4
			0 x 12

			Z
			
			A1
			
			A2
			

			
		*/
		
		
		
		switch (keys[0]){
				case 'StepY':
					console.log("StepY:" + query.StepY);
					
					//08:00:10:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:
					//a0:80:02:00:
					//00:00:00:00
					
					
					Move(new Buffer([0x08, 0x00, 0x10, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 
					//0x68, 0x10, 0x32, 0x00, 
					0xa0, 0x80, 0x02, 0x00,
					0x00, 0x00, 0x00, 0x00]));
					return 1;
				case "StepX":
					console.log("StepX:" + query.StepX);
					
					//08:00:10:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:
					//38:31:96:00:
					//00:00:00:00
					Move(new Buffer([0x08, 0x00, 0x10, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 
					//0xa8, 0x16, 0x4b, 0x00, 
					0x38, 0x31, 0x96, 0x00,
					0x00, 0x00, 0x00, 0x00]));
					return 1;
				case "StopAll":
					console.log("STOP ALL!!!");
					StopAll();
					return 1;
				default :
					return -1;
		}
		return -1;
	}

	function Move(msg){
		//if(!LimitsTriggered()){
			tvmClient.write(new Buffer([0x06, 0x00, 0x04, 0x00, 0x03, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0xa0, 0x0f, 0x00, 0x00, 0xb8, 0x0b, 0x00, 0x00, 0x88, 0x13, 0x00, 0x00, 0xa0, 0x0f, 0x00, 0x00, 0x88, 0x13, 0x00, 0x00, 0xa0, 0x0f, 0x00, 0x00]));
			tvmClient.write(msg);
			console.log(msg);
		//} else {
		//	console.log("Endstop triggered");
		//}
	}
	
	function processAPIRequest(query){		
		var keys = Object.keys(query);
		if(keys.length > 1 || keys.length === 0){
			return -1;
		}
		
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
		return -1;
	}
	
	function processHomeReq(){
		var result = -1;		
		console.log("Homing Started");
		
		function PrepHome(){
			// Stop command
			tvmClient.write(commands.StopOnLimitHit); 

			tvmClient.write(new Buffer([0x0b, 0x00, 0x00, 0x00]));
			tvmClient.write(new Buffer([0x15, 0x00, 0x00, 0x00, 0x20, 0x40, 0x00, 0x00]));
			tvmClient.write(new Buffer([0x15, 0x00, 0x00, 0x00, 0x2e, 0x74, 0x00, 0x00]));

			// Speed Command
			tvmClient.write(new Buffer([0x06, 0x00, 0x04, 0x00, 0x03, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0xa0, 0x0f, 0x00, 0x00, 0xb8, 0x0b, 0x00, 0x00, 0x88, 0x13, 0x00, 0x00, 0xa0, 0x0f, 0x00, 0x00, 0x88, 0x13, 0x00, 0x00, 0xa0, 0x0f, 0x00, 0x00]));
			tvmClient.write(new Buffer([0x06, 0x00, 0x04, 0x00, 0x03, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0xa0, 0x0f, 0x00, 0x00, 0xb8, 0x0b, 0x00, 0x00, 0x88, 0x13, 0x00, 0x00, 0xa0, 0x0f, 0x00, 0x00, 0x88, 0x13, 0x00, 0x00, 0xa0, 0x0f, 0x00, 0x00]));
		}
		
		function HomeY(){
			var GoHomeYDirCommand = new Buffer([0x08, 0x00, 0x10, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x40, 0x9c, 0xf4, 0x01, 0x00, 0x00, 0x00, 0x00]);
		
			// Send x in home direction
			tvmClient.write(GoHomeYDirCommand);
			NonBlockingCall(function(){},
				5000,
				{ get value() { return state.Limit.Top}},
				1,
				MoveYBack,
				function(){console.log("Bad");}
			);

			function MoveYBack(){
				console.log(state.PositionRaw.Y);
				console.log(state.PositionRaw.Y -2500);
				
				var rawSteps = state.PositionRaw.Y - 2500;
				var MoveBackHex = StepsToRevHex(rawSteps);
				
				
				//console.log(MoveBackHex);
				//console.log(Buffer.from(MoveBackHex, 'hex'));
				var Msg = Buffer.concat([
					new Buffer([0x08, 0x00, 0x10, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]),
					Buffer.from(MoveBackHex, 'hex'),
					new Buffer([0x00, 0x00, 0x00, 0x00])
				]);
				
				tvmClient.write(Msg);
				NonBlockingCall(function(){},
					5000,
					{ get value() { return state.PositionRaw.Y}},
					rawSteps-500,
					HomeY2,
					function(){console.log("Bad");}
				);
				
				function HomeY2(){
					tvmClient.write(GoHomeYDirCommand);
					NonBlockingCall(function(){},
						5000,
						{ get value() { return state.Limit.Top}},
						1,
						function(){
							// Set current Position
							var SetYHome = new Buffer ([0x07, 0x00, 0x10, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0xe0, 0xf6, 0xaf, 0x00, 0x00, 0x00, 0x00, 0x00]);
							tvmClient.write(SetYHome);
							console.log("---- Y Homed");
							},
						function(){console.log("Bad");}
					);
					
				}
				
			}




			
			// On endstop activate reverse 5mm
			
			// Send x in home direction
			//tvmClient.write(GoHomeXDirCommand);
			
			
			// Pre-op send speed command to be slower ?!?!?
			// On endstop clear registers
			
		}


		PrepHome();
		HomeY();
		

		return result;
	}

function NonBlockingCall(Run,Timeout,Value,Target,OnSucces,OnFail){
	Run();	
	
	function CheckForChange(target){
		console.log(Value.value);
		if(Value.value === target){
			clearTimeout(timer);
			clearInterval(checker);			
			OnSucces();
		}
	}
	
	// CheckForChange
	//console.log("Start CheckForChange");
	var checker = setInterval(function(){CheckForChange(Target);},50);	
	
	// Set Timeout
	var timer = setTimeout(function(){
		clearInterval(checker);			
		OnFail();		
	},Timeout);

}


// ####################### TVM Exports #######################
module.exports = {
	init : function() {
		tvmClient.connect(settings.tvmPort,settings.tvmIP, function() {
			console.log('TVM connecting.');
		});
	},
	processAPIRequest 	: processAPIRequest,
	processMoveRequest	: processMoveRequest,
	processHomeReq		: processHomeReq,
	state : state
};    
