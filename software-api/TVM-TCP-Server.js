const net = require('net');
const tvmManager = require("./TVM-manager");

const PORT = 3000;
const POSITION_TOLERANCE_MM = 0.05;

// Initialize the TVM Connection
tvmManager.init();

const server = net.createServer((socket) => {
    console.log('OpenPnP connected via TCP!');
    let buffer = '';

    socket.on('data', (data) => {
        buffer += data.toString();
        let lines = buffer.split('\n');

        // Keep the last incomplete line in the buffer
        buffer = lines.pop();

        lines.forEach(line => {
            let cmd = line.trim().toUpperCase();
            if (cmd) {
                processGcode(cmd, socket);
            }
        });
    });

    socket.on('end', () => console.log('OpenPnP disconnected.'));
    socket.on('error', (err) => console.log('TCP Error: ', err.message));
});

function processGcode(cmd, socket) {
    // 1. Rapid or Linear Move (G0 / G1)
    if (cmd.startsWith('G0') || cmd.startsWith('G1')) {
        let targetMove = {};
        let parts = cmd.split(' ');

        parts.forEach(part => {
            let axis = part[0];
            let val = parseFloat(part.substring(1));

            if (axis === 'X') targetMove.X = val;
            if (axis === 'Y') targetMove.Y = val;
            if (axis === 'Z') targetMove.Z1 = val; // Nozzle 1
            if (axis === 'C') targetMove.Z2 = val; // Nozzle 2
            if (axis === 'B') targetMove.A1 = val; // Nozzle 1 Rotation
            if (axis === 'A') targetMove.A2 = val; // Nozzle 2 Rotation
        });

        tvmManager.processMoveRequest(targetMove);
        waitForMoveCompletion(targetMove, socket);
        return;
    }

    // 2. Homing (G28)
    if (cmd.startsWith('G28')) {
        tvmManager.processHomeReq({});
        // Note: For a robust setup, you should poll tvmManager.state.Limit here
        // to block until homing is physically complete. For now, we simulate a delay.
        setTimeout(() => socket.write("ok\n"), 8000);
        return;
    }

    // 3. Actuator Controls (M-Codes)
    // Vacuum 1
    if (cmd === 'M800') { tvmManager.processAPIRequest({vacuum1: '1'}); return socket.write("ok\n"); }
    if (cmd === 'M801') { tvmManager.processAPIRequest({vacuum1: '0'}); return socket.write("ok\n"); }
    // Vacuum 2
    if (cmd === 'M802') { tvmManager.processAPIRequest({vacuum2: '1'}); return socket.write("ok\n"); }
    if (cmd === 'M803') { tvmManager.processAPIRequest({vacuum2: '0'}); return socket.write("ok\n"); }
    // Pump
    if (cmd === 'M804') { tvmManager.processAPIRequest({pump: '1'}); return socket.write("ok\n"); }
    if (cmd === 'M805') { tvmManager.processAPIRequest({pump: '0'}); return socket.write("ok\n"); }

    // 4. Report Position (M114)
    if (cmd === 'M114') {
        let pos = tvmManager.state.Position;
        socket.write(`X:${pos.X.toFixed(3)} Y:${pos.Y.toFixed(3)} Z:${pos.Nozzle.toFixed(3)} C:${(-pos.Nozzle).toFixed(3)} B:${pos.A1.toFixed(3)} A:${pos.A2.toFixed(3)}\n`);
        socket.write("ok\n");
        return;
    }

    // Catch-all for unhandled commands (safely ignore them)
    socket.write("ok\n");
}

function waitForMoveCompletion(targetMove, socket) {
    let checkInterval = setInterval(() => {
        let current = tvmManager.state.Position;
        let isMoving = false;

        if (targetMove.X !== undefined && Math.abs(current.X - targetMove.X) > POSITION_TOLERANCE_MM) isMoving = true;
        if (targetMove.Y !== undefined && Math.abs(current.Y - targetMove.Y) > POSITION_TOLERANCE_MM) isMoving = true;
        if (targetMove.Z1 !== undefined && Math.abs(current.Nozzle - targetMove.Z1) > POSITION_TOLERANCE_MM) isMoving = true;
        if (targetMove.Z2 !== undefined && Math.abs(current.Nozzle - targetMove.Z2) > POSITION_TOLERANCE_MM) isMoving = true;

        if (!isMoving) {
            clearInterval(checkInterval);
            socket.write("ok\n"); // Release OpenPnP to send the next command
        }
    }, 20); // Poll every 20ms matching TVM polling rate
}

server.listen(PORT, () => {
    console.log(`OpenPnP G-Code Bridge running on port ${PORT}`);
});
