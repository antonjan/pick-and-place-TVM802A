"""REST/WebSocket API tests (in-process, against the simulator)."""

from fastapi.testclient import TestClient

from pnp.api.app import create_app
from pnp.config import MachineConfig
from pnp.machine.machine import Machine
from pnp.sim.tvm_sim import TvmSimulator


def make_client() -> TestClient:
    sim = TvmSimulator(port=0)
    cfg = MachineConfig()
    cfg.controller.poll_interval_idle = 0.05
    cfg.controller.poll_interval_moving = 0.01
    machine = Machine(cfg)
    machine.t.delay_before_polling = 0.02

    async def startup():
        await sim.start()
        machine.t.host, machine.t.port = "127.0.0.1", sim.port

    app = create_app(machine, extra_startup=startup, extra_shutdown=sim.stop)
    return TestClient(app)


def test_full_flow():
    with make_client() as c:
        # wait until connected and a state arrived
        for _ in range(100):
            s = c.get("/api/state").json()
            if s["connected"] and s["speed_mode"]:
                break
        assert s["connected"]

        r = c.post("/api/move", json={"x": 10})
        assert r.status_code == 409 and "home" in r.json()["detail"]

        assert c.post("/api/home").status_code == 200
        r = c.post("/api/move", json={"x": 100.5, "y": 20})
        assert r.status_code == 200
        assert abs(r.json()["position"]["x"] - 100.5) < 0.031

        assert c.post("/api/move", json={"x": 1000}).status_code == 400
        assert c.post("/api/jog", json={"axis": "q", "delta": 1}).status_code == 400

        assert c.post("/api/output", json={"name": "vacuum1", "on": True}).status_code == 200
        assert c.post("/api/output", json={"name": "nope", "on": True}).status_code == 400

        assert c.post("/api/estop").status_code == 200
        assert c.get("/api/state").json()["estopped"]
        assert c.post("/api/jog", json={"axis": "x", "delta": 1}).status_code == 409
        assert c.post("/api/reset").status_code == 200

        with c.websocket_connect("/ws") as ws:
            snap = ws.receive_json()
            assert snap["homed"] and snap["connected"]

        assert c.get("/").status_code == 200
        assert c.get("/static/app.js").status_code == 200
