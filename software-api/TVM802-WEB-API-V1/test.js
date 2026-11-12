

//409cf401 -- 1000 -- Far
//e085ad00 -- current minus 5
//4896df00 -- move far 
//e0f6af00  -- Set Register

	function BuildNumber(value){
		return value.substring(6,8) + value.substring(4,6) + value.substring(2,4) + value.substring(0,2);		
	}
	
	
	//console.log(parseInt(BuildNumber('409cf401'),16)/32808);
	//console.log(parseInt(BuildNumber('e085ad00'),16)/32808);
	//console.log(parseInt(BuildNumber('4896df00'),16)/32808);
	//console.log(parseInt(BuildNumber('e0f6af00'),16)/32808);
	
	console.log(parseInt(BuildNumber('280a0000'),16)/32808);
	console.log(parseInt(BuildNumber('a086e304'),16));
	

	
	

	
	function PositionToReversedHEX(position,stepsPerMM){
		var roundedLocation = Math.round(position*100)/100;
		var steps = Math.round(roundedLocation * stepsPerMM);
		var pad = "00000000"
		var stepsHex = steps.toString(16);
		var stepsHexPad = pad.substring(0,pad.length - stepsHex.length) + stepsHex;
	

	
		return BuildNumber(stepsHexPad);
	}	
	
	function PositionToReversedHEX(position){
		var roundedLocation = Math.round(position*100)/100;
		var steps = Math.round(roundedLocation);
		var pad = "00000000"
		var stepsHex = steps.toString(16);
		var stepsHexPad = pad.substring(0,pad.length - stepsHex.length) + stepsHex;
	

	
		return BuildNumber(stepsHexPad);
	}	

		console.log(PositionToReversedHEX(2500));
	
	
	
	//console.log(PositionToReversedHEX(110,32808));
	
	//console.log(Buffer.from(PositionToReversedHEX(110,32808), 'hex'));