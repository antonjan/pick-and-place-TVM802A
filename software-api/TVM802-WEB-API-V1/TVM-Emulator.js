var net = require('net');

var globalSocket ={};

var server = net.createServer(function(socket) {
	globalSocket = socket;
	globalSocket.on('error', function (err) {
		console.log("Client Error: " + err);
	});
	globalSocket.on('data', function (msg) {
		console.log(msg);
	});
});

server.on("connection", function() {
	console.log("New Connection");
	globalSocket.write('OK');
})

server.on('close', function() {
	console.log('Connection closed.');
});

server.on('error', function (err) {
	console.log( err );
});

server.on('data', function (msg) {
	console.log(msg);
});




server.listen(701, '127.0.0.1');