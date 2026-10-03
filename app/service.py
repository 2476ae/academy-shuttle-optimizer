"""The live dispatch service behind the web pages.

One shuttle, one day. ``real`` mode (the default) follows the local clock:
teachers call the shuttle, the driver taps 출발/도착 and confirms who got on,
and every action is appended to a per-day file so a restart picks the day up
where it left off. ``demo`` mode runs a fast clock from a set start time, with
optional synthetic teachers, for trying things out.
"""

from __future__ import annotations

import json
import math
import os
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from shuttle import Dispatcher, Request, format_clock, load_network, make_policy, parse_clock
from shuttle.demand import DemandModel, generate_day
from shuttle.dispatcher import BOARDING, DRIVING, DWELL, FREE, READY, WAIT
from shuttle.models import CANCELLED, DONE, NO_SHOW, ONBOARD
from shuttle.models import WAITING as REQ_WAITING
from shuttle.network import Network
from shuttle.policies import POLICIES

STATUS_TEXT = {
    REQ_WAITING: "기다리는 중",
    ONBOARD: "탑승",
    DONE: "도착",
    CANCELLED: "취소됨",
    NO_SHOW: "미탑승",
}
NOTE_MAX = 40


class ServiceError(Exception):
    """A request the service refuses; ``message`` is shown to the user."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


class Clock:
    def __init__(self, mode: str, start: float, speed: float, tz: str, wall: Callable[[], datetime] | None = None):
        self.mode = mode
        self.start = start
        self.speed = speed
        self.tz = ZoneInfo(tz)
        self._wall = wall or (lambda: datetime.now(self.tz))
        self._t0 = time.monotonic()

    def today(self) -> str:
        """Calendar day the clock is on (real mode); the demo is always one day."""
        return self._wall().strftime("%Y-%m-%d") if self.mode == "real" else "demo"

    def now(self) -> float:
        if self.mode == "real":
            dt = self._wall()
            return dt.hour * 60 + dt.minute + dt.second / 60 + dt.microsecond / 60e6
        return self.start + (time.monotonic() - self._t0) * self.speed / 60.0


@dataclass
class Settings:
    mode: str = "real"  # "real" or "demo"
    speed: float = 30.0  # demo only: simulated seconds per real second
    start: str = "15:20"  # demo only: clock time when the demo starts
    policy: str = "optimized"
    demo_requests: bool = True  # demo only: synthetic teachers post requests
    manual: bool | None = None  # driver confirms 출발/도착/탑승; default: on in real mode
    seed: int | None = None
    tz: str = "Asia/Seoul"
    data_dir: str | None = "var"  # real mode keeps each day's actions here; None turns it off
    access_code: str | None = None  # staff enter this once per device; None leaves the API open

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        manual = env.get("SHUTTLE_MANUAL")
        seed = env.get("SHUTTLE_SEED")
        return cls(
            mode=env.get("SHUTTLE_MODE", "real"),
            speed=float(env.get("SHUTTLE_SPEED", "30")),
            start=env.get("SHUTTLE_START", "15:20"),
            policy=env.get("SHUTTLE_POLICY", "optimized"),
            demo_requests=env.get("SHUTTLE_DEMO_REQUESTS", "1") not in ("0", "false", "no"),
            manual=None if manual is None else manual not in ("0", "false", "no"),
            seed=None if seed is None else int(seed),
            tz=env.get("SHUTTLE_TZ", "Asia/Seoul"),
            data_dir=env.get("SHUTTLE_DATA_DIR", "var") or None,
            access_code=(env.get("SHUTTLE_ACCESS_CODE") or "").strip() or None,
        )


class DayLog:
    """Append-only record of one day's actions, replayed after a restart."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        events = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a half-written last line after a crash
        return events

    def append(self, event: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())


def _seq_of(rid: str) -> int:
    digits = "".join(ch for ch in rid if ch.isdigit())
    return int(digits) if digits else 0


class ShuttleService:
    def __init__(self, settings: Settings | None = None, net: Network | None = None, wall: Callable[[], datetime] | None = None):
        self.settings = settings or Settings()
        if self.settings.mode not in ("demo", "real"):
            raise ValueError("SHUTTLE_MODE must be 'real' or 'demo'")
        self.net = net or load_network()
        self.lock = threading.RLock()
        self._wall = wall
        self._replaying = False
        self.reset()

    # ----------------------------------------------------------------- set-up

    @property
    def manual(self) -> bool:
        m = self.settings.manual
        return self.settings.mode == "real" if m is None else m

    def reset(self, **changes) -> None:
        """Start the day: replay today's saved actions (real mode) or a fresh demo."""
        with self.lock:
            for key, value in changes.items():
                if value is not None:
                    setattr(self.settings, key, value)
            s = self.settings
            if s.policy not in POLICIES:
                raise ServiceError(f"알 수 없는 운행 규칙입니다: {s.policy}")
            if s.speed <= 0 or s.speed > 600:
                raise ServiceError("속도는 1~600배 사이로 정해 주세요.")
            self.clock = Clock(s.mode, parse_clock(s.start), s.speed, s.tz, self._wall)
            self._day = self.clock.today()
            now = self.clock.now()
            self.day_log = DayLog(Path(s.data_dir) / "days" / f"{self._day}.jsonl") if s.mode == "real" and s.data_dir else None
            events = [e for e in (self.day_log.load() if self.day_log else []) if isinstance(e, dict) and isinstance(e.get("t"), (int, float))]
            start = min([now, *(float(e["t"]) for e in events[:1])])
            self.dispatcher = Dispatcher(self.net, make_policy(s.policy, self.net), now=start, start_stop="A", auto_drive=not self.manual)
            self._seq = 0
            self._feed: list[Request] = []
            if s.mode == "demo" and s.demo_requests:
                seed = s.seed if s.seed is not None else random.randrange(1 << 30)
                day = generate_day(self.net, DemandModel(), random.Random(seed), prefix="demo-")
                self._feed = [r for r in day if r.created_at >= now]
            self._replaying = True
            try:
                self.restored = sum(1 for e in events if self._replay(e))
            finally:
                self._replaying = False
            self.dispatcher.advance(max(now, self.dispatcher.now))

    # ------------------------------------------------------------------ clock

    def tick(self) -> None:
        with self.lock:
            self._tick_locked()

    def _tick_locked(self) -> float:
        if self.clock.today() != self._day:
            # A new day in real mode: yesterday's requests are over, start fresh.
            self.reset()
        now = self.clock.now()
        d = self.dispatcher
        while self._feed and self._feed[0].created_at <= now:
            r = self._feed.pop(0)
            d.advance(r.created_at)
            d.add_request(r)
        d.advance(now)
        return now

    # ---------------------------------------------------------- action replay

    def _apply(self, e: dict) -> bool:
        """Carry out one recorded action; the same code serves live calls and replay."""
        d = self.dispatcher
        t = float(e["t"])
        kind = e.get("type")
        if kind == "request":
            b = self.net.branches.get(e.get("branch", ""))
            if b is None or e.get("dest") not in self.net.stops or e["id"] in d.requests:
                return False
            d.advance(t)
            d.add_request(
                Request(
                    id=e["id"],
                    branch=e["branch"],
                    pickup=b.stop,
                    dest=e["dest"],
                    count=int(e["count"]),
                    created_at=t,
                    ready_at=float(e["ready_at"]),
                    note=str(e.get("note", ""))[:NOTE_MAX],
                )
            )
            d.advance(t)
            self._seq = max(self._seq, _seq_of(e["id"]))
            return True
        if kind == "cancel":
            d.advance(t)
            ok = d.cancel_request(e["id"])
            d.advance(t)
            return ok
        if kind == "depart":
            return d.driver_depart(t, e.get("to"))
        if kind == "arrive":
            return d.driver_arrive(t, e.get("at"))
        if kind == "board":
            return d.driver_board(t, e.get("boarded", []))
        if kind == "locate":
            return d.driver_locate(t, e.get("stop", ""))
        return False

    def _replay(self, event: dict) -> bool:
        """Re-apply a saved action; a line that no longer makes sense (e.g. after a config change) is skipped."""
        try:
            return self._apply(event)
        except (KeyError, TypeError, ValueError):
            return False

    def _do(self, event: dict, refusal: str) -> None:
        if not self._apply(event):
            raise ServiceError(refusal, 409)
        if self.day_log and not self._replaying:
            self.day_log.append(event)

    # ---------------------------------------------------------------- actions

    def create_request(self, branch: str, dest: str, count: int, ready_in: float = 0.0, note: str = "") -> dict:
        with self.lock:
            now = self._tick_locked()
            b = self.net.branches.get(branch)
            if b is None:
                raise ServiceError("알 수 없는 학원입니다.")
            if dest not in b.destinations:
                raise ServiceError(f"{b.name}에서는 이 목적지로 셔틀을 운행하지 않습니다.")
            if not 1 <= count <= self.net.capacity:
                raise ServiceError(f"인원은 1~{self.net.capacity}명으로 정해 주세요.")
            if not 0 <= ready_in <= 60:
                raise ServiceError("준비 시각은 지금부터 60분 안으로 정해 주세요.")
            rid = f"q{self._seq + 1:03d}"
            event = {
                "t": now,
                "type": "request",
                "id": rid,
                "branch": branch,
                "dest": dest,
                "count": count,
                "ready_at": now + ready_in,
                "note": " ".join((note or "").split())[:NOTE_MAX],
            }
            self._do(event, "요청을 받지 못했습니다. 다시 시도해 주세요.")
            return self._request_json(self.dispatcher.requests[rid])

    def cancel_request(self, rid: str) -> dict:
        with self.lock:
            now = self._tick_locked()
            req = self.dispatcher.requests.get(rid)
            if req is None:
                raise ServiceError("요청을 찾을 수 없습니다.", 404)
            self._do({"t": now, "type": "cancel", "id": rid}, "이미 탑승했거나 끝난 요청은 취소할 수 없습니다.")
            return self._request_json(req)

    def _driver_action(self, event: dict, refusal: str) -> None:
        with self.lock:
            event["t"] = self._tick_locked()
            if not self.manual:
                raise ServiceError("시연 모드에서는 셔틀이 자동으로 움직입니다.", 409)
            for key in ("to", "at", "stop"):
                if event.get(key) is not None and event[key] not in self.net.stops:
                    raise ServiceError("알 수 없는 정류장입니다.")
            self._do(event, refusal)

    def driver_depart(self, to: str | None = None) -> None:
        self._driver_action({"type": "depart", "to": to}, "지금은 출발할 수 없습니다. 탑승 확인이나 도착을 먼저 눌러 주세요.")

    def driver_arrive(self, at: str | None = None) -> None:
        self._driver_action({"type": "arrive", "at": at}, "지금은 이동 중이 아닙니다.")

    def driver_board(self, boarded: list[str]) -> None:
        self._driver_action({"type": "board", "boarded": list(boarded)}, "지금은 탑승을 확인할 차례가 아닙니다.")

    def driver_locate(self, stop: str) -> None:
        self._driver_action({"type": "locate", "stop": stop}, "이동 중이거나 탑승 확인 중에는 위치를 바꿀 수 없습니다.")

    # --------------------------------------------------------------- snapshot

    def snapshot(self) -> dict:
        with self.lock:
            now = self._tick_locked()
            d = self.dispatcher
            plan = d.plan()
            requests = sorted(d.requests.values(), key=lambda r: (r.created_at, r.id))
            done = [r for r in requests if r.status == DONE]
            served = sum(r.count for r in done)
            waiting = [r for r in requests if r.status == REQ_WAITING]
            return {
                "now": now,
                "clock": format_clock(math.floor(now)),  # a clock shows the minute that has started
                "day": self._day,
                "mode": self.settings.mode,
                "speed": self.settings.speed,
                "manual": self.manual,
                "demo_requests": self.settings.demo_requests,
                "policy": {"name": d.policy.name, "label": d.policy.label},
                "vehicle": self._vehicle_json(now),
                "instruction": self._instruction(now, plan),
                "plan": [self._step_json(s) for s in plan.steps],
                "requests": [self._request_json(r, plan) for r in requests],
                "stats": {
                    "served": served,
                    "waiting": sum(r.count for r in waiting),
                    "onboard": sum(d.requests[rid].count for rid in d.onboard),
                    "no_show": sum(r.count for r in requests if r.status == NO_SHOW),
                    "avg_wait": _weighted(done, lambda r: r.wait),
                    "max_wait": max((r.wait for r in done), default=None),
                    "avg_total": _weighted(done, lambda r: r.total),
                    "driving": round(d.driving, 1),
                },
                "log": [
                    {"t": e["t"], "clock": format_clock(e["t"]), "kind": e["kind"], "text": e["text"]}
                    for e in reversed(d.log[-40:])
                ],
            }

    def config(self) -> dict:
        data = self.net.to_dict()
        data["policies"] = [{"name": name, "label": cls.label} for name, cls in POLICIES.items()]  # type: ignore[attr-defined]
        data["mode"] = self.settings.mode
        return data

    # ---------------------------------------------------------------- helpers

    def _request_json(self, r: Request, plan=None) -> dict:
        net = self.net
        plan = plan if plan is not None else self.dispatcher.plan()
        pickup_eta = plan.pickup_eta(r.id) if r.status == REQ_WAITING else None
        drop_eta = plan.drop_eta(r.id) if r.status in (REQ_WAITING, ONBOARD) else None
        return {
            "id": r.id,
            "branch": r.branch,
            "branch_name": net.branches[r.branch].name,
            "pickup": r.pickup,
            "dest": r.dest,
            "dest_name": net.stop_name(r.dest),
            "dest_short": net.stops[r.dest].short,
            "count": r.count,
            "note": r.note,
            "status": r.status,
            "status_text": STATUS_TEXT[r.status],
            "created_at": r.created_at,
            "ready_at": r.ready_at,
            "ready_text": format_clock(r.ready_at),
            "pickup_eta": pickup_eta,
            "pickup_eta_text": format_clock(pickup_eta) if pickup_eta is not None else None,
            "drop_eta": drop_eta,
            "drop_eta_text": format_clock(drop_eta) if drop_eta is not None else None,
            "picked_at": r.picked_at,
            "picked_text": format_clock(r.picked_at) if r.picked_at is not None else None,
            "dropped_at": r.dropped_at,
            "dropped_text": format_clock(r.dropped_at) if r.dropped_at is not None else None,
        }

    def _group_json(self, rid: str) -> dict:
        r = self.dispatcher.requests[rid]
        return {
            "id": rid,
            "branch": r.branch,
            "branch_name": self.net.branches[r.branch].name,
            "dest": r.dest,
            "dest_name": self.net.stop_name(r.dest),
            "dest_short": self.net.stops[r.dest].short,
            "count": r.count,
            "note": r.note,
        }

    def _vehicle_json(self, now: float) -> dict:
        d = self.dispatcher
        net = self.net
        onboard: dict[str, int] = {}
        for rid in d.onboard:
            r = d.requests[rid]
            onboard[r.dest] = onboard.get(r.dest, 0) + r.count
        progress = None
        if d.status == DRIVING and d.depart_at is not None:
            span = max(d.until - d.depart_at, 1e-6)
            progress = min(1.0, max(0.0, (now - d.depart_at) / span))
        boarding = [rid for rid in d.pending_board if d.requests[rid].status == REQ_WAITING] if d.status == BOARDING else []
        return {
            "status": d.status,
            "stop": d.stop,
            "stop_name": net.stop_name(d.stop),
            "origin": d.origin,
            "next_stop": d.next_stop,
            "depart_at": d.depart_at,
            "until": d.until,
            "until_text": format_clock(d.until),
            "progress": progress,
            "load": sum(onboard.values()),
            "capacity": net.capacity,
            "onboard": [
                {"dest": k, "dest_name": net.stop_name(k), "dest_short": net.stops[k].short, "count": v}
                for k, v in sorted(onboard.items())
            ],
            "boarding": [self._group_json(rid) for rid in boarding],
        }

    def _step_json(self, s) -> dict:
        net = self.net
        reqs = self.dispatcher.requests

        def groups(ids):
            return [
                {
                    "id": rid,
                    "count": reqs[rid].count,
                    "dest_short": net.stops[reqs[rid].dest].short,
                    "branch": reqs[rid].branch,
                    "note": reqs[rid].note,
                }
                for rid in ids
                if rid in reqs
            ]

        return {
            "stop": s.stop,
            "stop_name": net.stop_name(s.stop),
            "short": net.stops[s.stop].short,
            "kind": net.stops[s.stop].kind,
            "arrive": s.arrive,
            "arrive_text": format_clock(s.arrive),
            "board_at": s.board_at,
            "board_at_text": format_clock(s.board_at) if s.board_at is not None else None,
            "depart": s.depart,
            "board": groups(s.board),
            "alight": groups(s.alight),
            "board_count": sum(reqs[rid].count for rid in s.board if rid in reqs),
            "alight_count": sum(reqs[rid].count for rid in s.alight if rid in reqs),
        }

    def _instruction(self, now: float, plan) -> dict:
        """What the driver should do right now, in one headline."""
        d = self.dispatcher
        net = self.net
        here = next((s for s in plan.steps if s.stop == d.stop), None) if plan.steps else None
        first = plan.steps[0] if plan.steps else None

        def todo(step) -> str:
            if step is None:
                return ""
            parts = []
            if step.alight:
                parts.append(f"{sum(d.requests[r].count for r in step.alight)}명 내리기")
            if step.board:
                parts.append(f"{sum(d.requests[r].count for r in step.board)}명 태우기")
            return " · ".join(parts)

        def say(headline, detail, action=None, label=None):
            return {"headline": headline, "detail": detail, "action": action, "action_label": label}

        if d.status == BOARDING:
            total = sum(d.requests[rid].count for rid in d.pending_board if d.requests[rid].status == REQ_WAITING)
            return say(f"{net.stop_name(d.stop)}에서 태우기", f"{total}명 · 탄 학생을 확인하고 눌러 주세요", "board", "탑승 완료")
        if d.status == DRIVING:
            target = first if first is not None and first.stop == d.stop else None
            detail = todo(target)
            returning = not detail and not d.onboard and d.stop in net.pickup_stops and not d.active()
            return say(
                f"{'복귀' if returning else '다음 정류장'} · {net.stop_name(d.stop)}",
                f"{format_clock(d.until)} 도착 예정" + (f" · {detail}" if detail else ""),
                "arrive" if self.manual else None,
                "도착했어요" if self.manual else None,
            )
        if d.status == READY and d.next_stop:
            nxt = next((s for s in plan.steps if s.stop == d.next_stop), None)
            detail = todo(nxt)
            returning = not detail and not d.onboard and not d.active()
            return say(
                f"{'복귀' if returning else '출발'} → {net.stop_name(d.next_stop)}",
                detail or ("학원으로 돌아가 다음 요청을 기다려요" if returning else "다음 정류장으로 이동"),
                "depart",
                "출발",
            )
        if d.status == WAIT:
            if here is not None and here.board:
                at = here.board_at if here.board_at is not None else d.until
                detail = f"{format_clock(at)}에 {sum(d.requests[r].count for r in here.board)}명 탑승 예정"
            else:
                detail = f"{format_clock(d.until)}까지 대기"
            return say(f"{net.stop_name(d.stop)}에서 대기", detail)
        if d.status in (DWELL, FREE):
            return say(f"{net.stop_name(d.stop)} · 타고 내리는 중", "잠시 후 다음 안내")
        return say(f"{net.stop_name(d.stop)}에서 대기", "들어온 요청이 없어요" if not d.active() else "다음 요청을 기다리는 중")


def _weighted(reqs, fn) -> float | None:
    total = sum(r.count for r in reqs)
    if not total:
        return None
    return round(sum(fn(r) * r.count for r in reqs) / total, 1)
