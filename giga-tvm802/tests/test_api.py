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


def test_reconnect_endpoint_and_link_error():
    import time as _time

    sim = TvmSimulator(port=0)
    cfg = MachineConfig()
    cfg.controller.poll_interval_idle = 0.05
    cfg.controller.poll_interval_moving = 0.01
    cfg.controller.reconnect_delay = 0.2
    machine = Machine(cfg)
    machine.t.delay_before_polling = 0.02

    async def startup():
        await sim.start()
        machine.t.host, machine.t.port = "127.0.0.1", sim.port

    def wait_for(pred, timeout=5.0):
        end = _time.monotonic() + timeout
        while _time.monotonic() < end:
            s = c.get("/api/state").json()
            if pred(s):
                return s
            _time.sleep(0.05)
        raise AssertionError(f"condition not reached: {s}")

    with TestClient(create_app(machine, extra_startup=startup, extra_shutdown=sim.stop)) as c:
        wait_for(lambda s: s["connected"])
        sim.mute = True  # machine powered off: no replies, socket stays open
        s = wait_for(lambda s: not s["connected"])
        assert "stopped answering" in (s["link_error"] or "")
        sim.mute = False
        assert c.post("/api/machine/reconnect").status_code == 200
        s = wait_for(lambda s: s["connected"])
        assert s["link_error"] is None and not s["homed"]


def test_ui_files_are_revalidated():
    with make_client() as c:
        for path in ("/", "/static/app.js"):
            assert c.get(path).headers.get("cache-control") == "no-cache"


def test_needle_interlock_over_http():
    with make_client() as c:
        for _ in range(100):
            if c.get("/api/state").json()["connected"]:
                break
        assert c.post("/api/home").status_code == 200
        assert c.post("/api/output", json={"name": "prick", "on": True}).status_code == 200
        r = c.post("/api/jog", json={"axis": "x", "delta": -1})
        assert r.status_code == 409 and "needle" in r.json()["detail"]
        # the Setup needle step lowers it AND allows jogging
        r = c.post("/api/calibrate/needle", json={"down": True})
        assert r.status_code == 200 and r.json()["needle_setup"] is True
        assert c.post("/api/jog", json={"axis": "x", "delta": -1}).status_code == 200
        r = c.post("/api/calibrate/needle", json={"down": False})
        assert r.json() == {"needle_down": False, "needle_setup": False}


def test_zero_z_endpoint():
    with make_client() as c:
        for _ in range(100):
            if c.get("/api/state").json()["connected"]:
                break
        assert c.post("/api/move", json={"z": 2.0}).status_code == 200
        r = c.post("/api/machine/zero-z")
        assert r.status_code == 200 and abs(r.json()["position"]["z"]) < 0.031



def test_alarm_endpoints_are_safe_without_alarm_and_always_allowed():
    from pnp.api.app import ALWAYS_ALLOWED

    assert {"/api/alarm/clear", "/api/alarm/silence", "/api/alarm/abort"} <= ALWAYS_ALLOWED
    with make_client() as c:
        for path in ("/api/alarm/clear", "/api/alarm/silence", "/api/alarm/abort"):
            r = c.post(path)
            assert r.status_code == 200 and r.json() == {"alarm": None}
        assert c.get("/api/state").json()["alarm"] is None
