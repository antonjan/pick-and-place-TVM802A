var express = require('express');
var net = require('net');
var path = require('path');
var tvmManager = require("./TVM-manager");

var config = {
	wwwPort : 3000,
	gcodePort : 2222
};

var server = express();
server.use(express.static(__dirname + '/static', { maxAge: 100, etag: false }));

// HTTP Endpoints
server.get('/api/status', function (req, res) {
	res.json(tvmManager.state);
});

server.get('/', function (req, res) {
	res.redirect('index.html');
});

server.get('/api/set', function (req, res) {
	var result = tvmManager.processAPIRequest(req.query);
	res.json({ 'result': result });
});

server.get('/api/home', function (req, res) {
	var result = tvmManager.processHomeReq(req.query);
	res.json({ 'result': result });
});

server.get('/api/move', function (req, res) {
	var result = tvmManager.processMoveRequest(req.query);
	res.json({ 'result': result });
});

// G-Code TCP Server
var isRelative = false; // Tracks G90 (Absolute) vs G91 (Relative) positioning mode

var gcodeServer = net.createServer(function(socket) {
	console.log('OpenPnP connected via TCP G-Code socket.');

	socket.on('data', function(data) {
		var lines = data.toString().split('\n');
		lines.forEach(function(rawLine) {
			// 1. Strip comments (anything after ; or inside parentheses)
			var line = rawLine.split(';')[0].split('(')[0].trim();
			if (!line) return;

			// 2. Strip line number prefixes (e.g., N10 G0 X20 -> G0 X20)
			line = line.replace(/^N\d+\s*/i, '');

			// 3. Handle Positioning Modes
			if (line.match(/^G90\b/i)) {
				isRelative = false;
				return socket.write("ok\n");
			}
			if (line.match(/^G91\b/i)) {
				isRelative = true;
				return socket.write("ok\n");
			}

			// 4. Handle Homing (G28)
			if (line.startsWith('G28')) {
				var homeQuery = {};
				if (line.includes('X')) homeQuery.axis = 'X';
				else if (line.includes('Y')) homeQuery.axis = 'Y';

				tvmManager.processHomeReq(homeQuery);
				return socket.write("ok\n");
			}

			// 5. Handle Movement Commands (G0 / G1)
			if (line.match(/^(G00?|G01?)\b/i)) {
				var query = {};
				var curPos = tvmManager.state.Position;

				var xMatch = line.match(/X\s*(-?\d+(?:\.\d+)?)/i);
				var yMatch = line.match(/Y\s*(-?\d+(?:\.\d+)?)/i);
				var zMatch = line.match(/Z\s*(-?\d+(?:\.\d+)?)/i);
				var aMatch = line.match(/[AC]\s*(-?\d+(?:\.\d+)?)/i);
				var bMatch = line.match(/B\s*(-?\d+(?:\.\d+)?)/i);

				if (xMatch) {
					var val = parseFloat(xMatch[1]);
					query.X = isRelative ? (curPos.X + val) : val;
				}
				if (yMatch) {
					var val = parseFloat(yMatch[1]);
					query.Y = isRelative ? (curPos.Y + val) : val;
				}
				if (zMatch) {
					var val = parseFloat(zMatch[1]);
					query.Z = isRelative ? (curPos.Nozzle + val) : val;
				}
				if (aMatch) {
					var val = parseFloat(aMatch[1]);
					query.A1 = isRelative ? (curPos.A1 + val) : val;
				}
				if (bMatch) {
					var val = parseFloat(bMatch[1]);
					query.A2 = isRelative ? (curPos.A2 + val) : val;
				}

				if (Object.keys(query).length > 0) {
					tvmManager.processMoveRequest(query);
				}
				return socket.write("ok\n");
			}

			// 6. Handle Position Query (M114)
			if (line.startsWith('M114')) {
				var pos = tvmManager.state.Position;
				return socket.write("X:" + pos.X + " Y:" + pos.Y + " Z:" + pos.Nozzle + " C:" + pos.A1 + " A:" + pos.A1 + " B:" + pos.A2 + " ok\n");
			}

			// Catch-all response for unsupported setup codes (G20, G21, M82, feedrates, etc.)
			socket.write("ok\n");
		});
	});

	socket.on('error', function(err) {
		console.log("G-Code Socket Client Error: " + err.message);
	});
});

// Catch TCP listener errors to prevent script termination
gcodeServer.on('error', function(err) {
	console.error("G-Code TCP Server Error:", err.message);
});

// Main Startup
function main(){
	server.listen(config.wwwPort, function () {
		require('dns').lookup(require('os').hostname(), function (err, add, fam) {
			console.log('HTTP Server listening on ' + add + ':' + config.wwwPort);
		});
	});

	gcodeServer.listen(config.gcodePort, function() {
		console.log('G-Code TCP Server listening on port ' + config.gcodePort);
	});

	// Connect to TVM Hardware
	tvmManager.init();
}

main();
