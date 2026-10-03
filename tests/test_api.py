import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.service import Settings, ShuttleService


def client(**settings):
    svc = ShuttleService(Settings(**{"demo_requests": False, "seed": 1, **settings}))
    return TestClient(create_app(svc)), svc


def test_config_lists_stops_branches_and_policies():
    c, _ = client()
    with c:
        data = c.get("/api/config").json()
    assert {s["id"] for s in data["stops"]} == {"A", "BC", "D1", "D2", "D3", "D4"}
    assert [b["id"] for b in data["branches"]] == ["A", "B", "C"]
    assert {p["name"] for p in data["policies"]} == {"fcfs", "optimized", "fixed"}
    assert data["minutes"]["A"]["BC"] == 5


def test_teacher_request_gets_an_eta():
    c, _ = client()
    with c:
        res = c.post("/api/requests", json={"branch": "B", "dest": "D2", "count": 2, "ready_in": 5})
        assert res.status_code == 201
        req = res.json()
        assert req["status"] == "waiting"
        assert req["pickup_eta"] is not None and req["pickup_eta"] >= req["ready_at"] - 1e-6
        state = c.get("/api/state").json()
    assert [r["id"] for r in state["requests"]] == [req["id"]]
    assert state["stats"]["waiting"] == 2
    assert state["plan"], "the shuttle should have a plan"


@pytest.mark.parametrize(
    "body, message",
    [
        ({"branch": "A", "dest": "D2", "count": 1}, "운행하지 않습니다"),  # A's students walk to D2
        ({"branch": "Z", "dest": "D1", "count": 1}, "알 수 없는 학원"),
        ({"branch": "A", "dest": "D1", "count": 9}, "인원"),
    ],
)
def test_bad_requests_are_explained(body, message):
    c, _ = client()
    with c:
        res = c.post("/api/requests", json=body)
    assert res.status_code == 400
    assert message in res.json()["detail"]


def test_cancel_only_while_waiting():
    c, _ = client()
    with c:
        rid = c.post("/api/requests", json={"branch": "A", "dest": "D3", "count": 1, "ready_in": 30}).json()["id"]
        assert c.post(f"/api/requests/{rid}/cancel").json()["status"] == "cancelled"
        assert c.post(f"/api/requests/{rid}/cancel").status_code == 409
        assert c.post("/api/requests/nope/cancel").status_code == 404


def test_driver_buttons_in_manual_mode():
    c, svc = client(manual=True)
    with c:
        c.post("/api/requests", json={"branch": "B", "dest": "D2", "count": 1})
        state = c.get("/api/state").json()
        assert state["instruction"]["action"] == "depart"
        assert c.post("/api/driver/arrive").status_code == 409  # not driving yet
        state = c.post("/api/driver/depart").json()
        assert state["vehicle"]["status"] == "driving"
        assert state["instruction"]["action"] == "arrive"
        state = c.post("/api/driver/arrive").json()
    assert state["requests"][0]["status"] == "onboard"


def test_driver_buttons_refused_when_the_demo_drives_itself():
    c, _ = client(manual=False)
    with c:
        assert c.post("/api/driver/depart").status_code == 409


def test_demo_reset_switches_policy():
    c, _ = client()
    with c:
        c.post("/api/requests", json={"branch": "A", "dest": "D1", "count": 1})
        state = c.post("/api/demo/reset", json={"policy": "fcfs", "speed": 60}).json()
    assert state["policy"]["name"] == "fcfs"
    assert state["speed"] == 60
    assert state["requests"] == []


def test_pages_are_served():
    c, _ = client()
    with c:
        for path in ("/", "/teacher?branch=A", "/driver", "/board", "/static/app.css", "/static/common.js"):
            assert c.get(path).status_code == 200, path


def test_real_mode_starts_a_new_day_after_midnight():
    svc = ShuttleService(Settings(mode="real", demo_requests=False))
    svc.create_request("A", "D3", 1, ready_in=30)
    assert svc.snapshot()["requests"]
    svc._day = "1999-12-31"  # pretend the service was started yesterday
    state = svc.snapshot()
    assert state["requests"] == []
    assert state["mode"] == "real" and state["manual"] is True
