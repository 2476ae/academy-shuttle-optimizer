from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.service import Settings, ShuttleService

KST = ZoneInfo("Asia/Seoul")


class FakeWall:
    """A wall clock the test moves by hand."""

    def __init__(self, start=datetime(2026, 10, 5, 17, 0, tzinfo=KST)):
        self.t = start

    def __call__(self):
        return self.t

    def forward(self, minutes):
        self.t += timedelta(minutes=minutes)


def demo_client(**settings):
    svc = ShuttleService(Settings(**{"mode": "demo", "demo_requests": False, "seed": 1, "data_dir": None, **settings}))
    return TestClient(create_app(svc)), svc


def real_client(tmp_path, wall=None, **settings):
    wall = wall or FakeWall()
    svc = ShuttleService(Settings(**{"mode": "real", "data_dir": str(tmp_path), **settings}), wall=wall)
    return TestClient(create_app(svc)), svc, wall


def test_real_mode_is_the_default():
    s = Settings()
    assert s.mode == "real" and s.access_code is None


def test_config_lists_stops_branches_and_policies():
    c, _ = demo_client()
    with c:
        data = c.get("/api/config").json()
    assert {s["id"] for s in data["stops"]} == {"A", "BC", "D1", "D2", "D3", "D4"}
    assert [b["id"] for b in data["branches"]] == ["A", "B", "C"]
    assert {p["name"] for p in data["policies"]} == {"fcfs", "optimized", "fixed"}
    assert data["minutes"]["A"]["BC"] == 5


def test_teacher_request_gets_an_eta_and_keeps_the_note():
    c, _ = demo_client()
    with c:
        res = c.post("/api/requests", json={"branch": "B", "dest": "D2", "count": 2, "ready_in": 5, "note": "  김OO,   이OO "})
        assert res.status_code == 201
        req = res.json()
        assert req["status"] == "waiting" and req["note"] == "김OO, 이OO"
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
    c, _ = demo_client()
    with c:
        res = c.post("/api/requests", json=body)
    assert res.status_code == 400
    assert message in res.json()["detail"]


def test_cancel_only_while_waiting():
    c, _ = demo_client()
    with c:
        rid = c.post("/api/requests", json={"branch": "A", "dest": "D3", "count": 1, "ready_in": 30}).json()["id"]
        assert c.post(f"/api/requests/{rid}/cancel").json()["status"] == "cancelled"
        assert c.post(f"/api/requests/{rid}/cancel").status_code == 409
        assert c.post("/api/requests/nope/cancel").status_code == 404


def test_real_day_flow_with_driver_confirmations(tmp_path):
    c, svc, wall = real_client(tmp_path)
    with c:
        b = c.post("/api/requests", json={"branch": "B", "dest": "D2", "count": 2}).json()["id"]
        gone = c.post("/api/requests", json={"branch": "C", "dest": "D3", "count": 1}).json()["id"]
        state = c.get("/api/state").json()
        assert state["mode"] == "real" and state["manual"] is True
        assert state["instruction"]["action"] == "depart"
        assert c.post("/api/driver/arrive").status_code == 409  # not driving yet
        assert c.post("/api/driver/depart").json()["instruction"]["action"] == "arrive"
        wall.forward(6)
        state = c.post("/api/driver/arrive").json()
        assert state["instruction"]["action"] == "board"
        assert {g["id"] for g in state["vehicle"]["boarding"]} == {b, gone}
        state = c.post("/api/driver/board", json={"boarded": [b]}).json()
        by_id = {r["id"]: r for r in state["requests"]}
        assert by_id[b]["status"] == "onboard"
        assert by_id[gone]["status"] == "no_show" and by_id[gone]["status_text"] == "미탑승"
        assert state["instruction"]["action"] == "depart"
        assert state["stats"]["no_show"] == 1


def test_a_restart_picks_the_day_up_again(tmp_path):
    wall = FakeWall()
    c, _, _ = real_client(tmp_path, wall)
    with c:
        b = c.post("/api/requests", json={"branch": "B", "dest": "D1", "count": 2, "note": "박OO"}).json()["id"]
        c.post("/api/requests", json={"branch": "A", "dest": "D3", "count": 1, "ready_in": 40})
        c.post("/api/driver/depart")
        wall.forward(5)
        c.post("/api/driver/arrive")
        c.post("/api/driver/board", json={"boarded": [b]})
        before = c.get("/api/state").json()

    restarted = ShuttleService(Settings(mode="real", data_dir=str(tmp_path)), wall=wall)
    assert restarted.restored == 5
    after = restarted.snapshot()
    keep = ("id", "status", "count", "note", "picked_at", "ready_at")
    assert [{k: r[k] for k in keep} for r in after["requests"]] == [{k: r[k] for k in keep} for r in before["requests"]]
    assert after["vehicle"]["stop"] == before["vehicle"]["stop"]
    assert after["vehicle"]["status"] == before["vehicle"]["status"]
    # New requests continue the numbering instead of reusing ids.
    assert restarted.create_request("A", "D4", 1)["id"] == "q003"


def test_driver_can_correct_location_and_take_a_detour(tmp_path):
    c, _, wall = real_client(tmp_path)
    with c:
        c.post("/api/requests", json={"branch": "B", "dest": "D2", "count": 1})
        state = c.post("/api/driver/locate", json={"stop": "BC"}).json()
        assert state["vehicle"]["stop"] == "BC" and state["instruction"]["action"] == "board"
        assert c.post("/api/driver/locate", json={"stop": "ZZ"}).status_code == 400
        rid = state["vehicle"]["boarding"][0]["id"]
        c.post("/api/driver/board", json={"boarded": [rid]})
        state = c.post("/api/driver/depart", json={"to": "D4"}).json()  # driver chose another way
        assert state["vehicle"]["stop"] == "D4"
        wall.forward(3)
        state = c.post("/api/driver/arrive", json={"at": "D2"}).json()  # and actually went straight to D2
        assert {r["id"]: r["status"] for r in state["requests"]}[rid] == "done"


def test_driver_buttons_refused_when_the_demo_drives_itself():
    c, _ = demo_client(manual=False)
    with c:
        assert c.post("/api/driver/depart").status_code == 409


def test_demo_reset_switches_policy_but_not_in_real_mode(tmp_path):
    c, _ = demo_client()
    with c:
        c.post("/api/requests", json={"branch": "A", "dest": "D1", "count": 1})
        state = c.post("/api/demo/reset", json={"policy": "fcfs", "speed": 60}).json()
    assert state["policy"]["name"] == "fcfs"
    assert state["speed"] == 60
    assert state["requests"] == []
    c, _, _ = real_client(tmp_path)
    with c:
        assert c.post("/api/demo/reset", json={}).status_code == 409


def test_real_mode_starts_a_new_day_after_midnight(tmp_path):
    wall = FakeWall(datetime(2026, 10, 5, 23, 50, tzinfo=KST))
    svc = ShuttleService(Settings(mode="real", data_dir=str(tmp_path)), wall=wall)
    svc.create_request("A", "D3", 1, ready_in=5)
    assert svc.snapshot()["requests"]
    wall.forward(20)  # 00:10 the next day
    state = svc.snapshot()
    assert state["requests"] == [] and state["day"] == "2026-10-06"
    assert (tmp_path / "days" / "2026-10-05.jsonl").exists()


def test_access_code_guards_the_api(tmp_path):
    c, _, _ = real_client(tmp_path, access_code="2468")
    with c:
        assert c.get("/api/session").json() == {"required": True, "ok": False}
        assert c.get("/api/state").status_code == 401
        assert c.get("/").status_code == 200  # pages load; their data calls ask for the code
        assert c.post("/api/login", json={"code": "1111"}).status_code == 401
        res = c.post("/api/login", json={"code": " 2468 "})
        assert res.status_code == 200 and "httponly" in res.headers["set-cookie"].lower()
        assert c.get("/api/session").json() == {"required": True, "ok": True}
        assert c.get("/api/state").status_code == 200
        c.post("/api/logout")
        assert c.get("/api/state").status_code == 401


def test_repeated_wrong_codes_are_slowed_down(tmp_path):
    c, _, _ = real_client(tmp_path, access_code="2468")
    with c:
        codes = [c.post("/api/login", json={"code": f"{i:04d}"}).status_code for i in range(11)]
        assert codes[:10] == [401] * 10 and codes[10] == 429
        assert c.post("/api/login", json={"code": "2468"}).status_code == 429


def test_page_routes_ignore_query_parameters():
    c, _ = demo_client()
    with c:
        res = c.get("/?n=../../pyproject.toml")
    assert res.status_code == 200 and "<!doctype html>" in res.text.lower()


def test_pages_are_served():
    c, _ = demo_client()
    with c:
        for path in ("/", "/teacher?branch=A", "/driver", "/board", "/login", "/setup", "/manifest.webmanifest", "/static/app.css", "/static/common.js"):
            assert c.get(path).status_code == 200, path


def test_setup_hints_offer_a_lan_address():
    c, _ = demo_client()
    with c:
        hints = c.get("/api/setup-hints").json()
    assert isinstance(hints["lan"], list)
    assert all(u.startswith("http://") and not u.startswith("http://127.") for u in hints["lan"])


def test_restart_skips_saved_lines_that_no_longer_make_sense(tmp_path):
    wall = FakeWall()
    svc = ShuttleService(Settings(mode="real", data_dir=str(tmp_path)), wall=wall)
    svc.create_request("B", "D2", 2)
    day = tmp_path / "days" / "2026-10-05.jsonl"
    with open(day, "a", encoding="utf-8") as fh:
        fh.write('{"type": "request", "id": "x1"}\n')  # no time
        fh.write('{"t": 1020.5, "type": "request", "id": "q009", "branch": "A", "dest": "D3", "count": 99, "ready_at": 1021}\n')  # too many
        fh.write('{"t": 1020.6, "type": "teleport"}\n')  # unknown action
        fh.write('{"t": 1020.7, "type": "cancel"\n')  # cut off mid-write
    restarted = ShuttleService(Settings(mode="real", data_dir=str(tmp_path)), wall=wall)
    assert restarted.restored == 1
    assert [r["id"] for r in restarted.snapshot()["requests"]] == ["q001"]
