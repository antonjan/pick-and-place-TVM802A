var express = require('express');
var net = require('net');
var path = require('path');
var tvmManager = require("./TVM-manager");

var config = {
	wwwPort : 80,
};


// ####################### Web Server #######################
	var server = express();
	server.use(express.static(__dirname + '/static',{ maxAge: 100 ,etag: false })); 			// Static folder
	
	

	server.get('/api/status', function (req, res) {
		res.json(tvmManager.state);
	});
	
	// redirect home for now
	server.get('/', function (req, res) {
		//res.redirect('/api/status');
		//res.sendFile('index.html');
		//res.sendFile(path.join(__dirname + '/index.html'));
		res.redirect('index.html')
	});
	
	server.get('/api/set', function (req, res) {
		var result = tvmManager.processAPIRequest(req.query);
	
		var results = { 'result': result };
		res.json(results);
	});
	
	server.get('/api/home', function (req, res) {
		var result = tvmManager.processHomeReq(req.query);
	
		var results = { 'result': result };
		res.json(results);
	});
	
	
	server.get('/api/move', function (req, res) {
		var result = tvmManager.processMoveRequest(req.query);
	
		var results = { 'result': result };
		res.json(results);
	});
	
	
// ####################### Main #######################
	function main(){
			server.listen(config.wwwPort, function () {
				require('dns').lookup(require('os').hostname(), function (err, add, fam) {
					console.log('HTTP Server listening on '+ add +':'+config.wwwPort);
				})
			});
			
			tvmManager.init();			
	}

	main();	