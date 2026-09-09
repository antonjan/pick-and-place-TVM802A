$("#buzzerOn"		).click(function() { $.getJSON("/api/set?buzzer=1", function(data) { console.log(data); });});
$("#buzzerOff"		).click(function() { $.getJSON("/api/set?buzzer=0", function(data) { console.log(data); });});

$("#prickOn"		).click(function() { $.getJSON("/api/set?prick=1", function(data) { console.log(data); });});
$("#prickOff"		).click(function() { $.getJSON("/api/set?prick=0", function(data) { console.log(data); });});
$("#pumpOn"			).click(function() { $.getJSON("/api/set?pump=1", function(data) { console.log(data); });});
$("#pumpOff"		).click(function() { $.getJSON("/api/set?pump=0", function(data) { console.log(data); });});
$("#blowing1On"		).click(function() { $.getJSON("/api/set?blowing1=1", function(data) { console.log(data); });});
$("#blowing1Off"	).click(function() { $.getJSON("/api/set?blowing1=0", function(data) { console.log(data); });});
$("#blowing2On"		).click(function() { $.getJSON("/api/set?blowing2=1", function(data) { console.log(data); });});
$("#blowing2Off"	).click(function() { $.getJSON("/api/set?blowing2=0", function(data) { console.log(data); });});
$("#vacuum1On"		).click(function() { $.getJSON("/api/set?vacuum1=1", function(data) { console.log(data); });});
$("#vacuum1Off"		).click(function() { $.getJSON("/api/set?vacuum1=0", function(data) { console.log(data); });});
$("#vacuum2On"		).click(function() { $.getJSON("/api/set?vacuum2=1", function(data) { console.log(data); });});
$("#vacuum2Off"		).click(function() { $.getJSON("/api/set?vacuum2=0", function(data) { console.log(data); });});
$("#ledsOn"			).click(function() { $.getJSON("/api/set?leds=1", function(data) { console.log(data); });});
$("#ledsOff"		).click(function() { $.getJSON("/api/set?leds=0", function(data) { console.log(data); });});




$("#yPlus"			).click(function() { $.getJSON("/api/move?StepY=10", function(data) { console.log(data); });});
$("#yMinus"			).click(function() { $.getJSON("/api/move?StepY=-10", function(data) { console.log(data); });});
$("#xPlus"			).click(function() { $.getJSON("/api/move?StepX=10", function(data) { console.log(data); });});
$("#xMinus"			).click(function() { $.getJSON("/api/move?StepX=-10", function(data) { console.log(data); });});
$("#stopAll"		).click(function() { $.getJSON("/api/move?StopAll=1", function(data) { console.log(data); });});
$("#home"			).click(function() { $.getJSON("/api/home", function(data) { console.log(data); });});
