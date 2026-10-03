"""The live dispatch service behind the web pages.

One shuttle, one day. In ``demo`` mode the clock runs fast from a set start
time and, optionally, synthetic teachers post requests so the screens come
alive. In ``real`` mode the clock is the local time and the driver confirms
departures and arrivals.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from shuttle import Dispatcher, Request, format_clock, load_network, make_policy, parse_clock
from shuttle.demand import DemandModel, generate_day
from shuttle.dispatcher import DRIVING, DWELL, FREE, READY, WAIT
from shuttle.models import CANCELLED, DONE, ONBOARD
from shuttle.models import WAITING as REQ_WAITING
from shuttle.network import Network
from shuttle.policies import POLICIES

STATUS_TEXT = {
    REQ_WAITING: "기다리는 중",
    ONBOARD: "탑승",
    DONE: "도착",
    CANCELLED: "취소됨",
}


class ServiceError(Exception):
    """A request the service refuses; ``message`` is shown to the user."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


class Clock:
    def __init__(self, mode: str, start: float, speed: float, tz: str):
        self.mode = mode
        self.start = start
        self.speed = speed
        self.tz = ZoneInfo(tz)
        self._t0 = time.monotonic()

    def today(self) -> str:
        """Calendar day the clock is on (real mode); the demo is always one day."""
        return datetime.now(self.tz).strftime("%Y-%m-%d") if self.mode == "real" else "demo"

    def now(self) -> float:
        if self.mode == "real":
            dt = datetime.now(self.tz)
            return dt.hour * 60 + dt.minute + dt.second / 60 + dt.microsecond / 60e6
        return self.start + (time.monotonic() - self._t0) * self.speed / 60.0


@dataclass
class Settings:
    mode: str = "demo"  # "demo" or "real"
    speed: float = 30.0  # demo only: simulated seconds per real second
    start: str = "15:20"  # demo only: clock time when the demo starts
    policy: str = "optimized"
    demo_requests: bool = True  # demo only: synthetic teachers post requests
    manual: bool | None = None  # driver confirms 출발/도착; default: on in real mode
    seed: int | None = None
    tz: str = "Asia/Seoul"
    log_dir: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        manual = env.get("SHUTTLE_MANUAL")
        seed = env.get("SHUTTLE_SEED")
        return cls(
            mode=env.get("SHUTTLE_MODE", "demo"),
            speed=float(env.get("SHUTTLE_SPEED", "30")),
            start=env.get("SHUTTLE_START", "15:20"),
            policy=env.get("SHUTTLE_POLICY", "optimized"),
            demo_requests=env.get("SHUTTLE_DEMO_REQUESTS", "1") not in ("0", "false", "no"),
            manual=None if manual is None else manual not in ("0", "false", "no"),
            seed=None if seed is None else int(seed),
            tz=env.get("SHUTTLE_TZ", "Asia/Seoul"),
            log_dir=env.get("SHUTTLE_LOG_DIR"),
        )


class ShuttleService:
    def __init__(self, settings: Settings | None = None, net: Network | None = None):
        self.settings = settings or Settings()
        if self.settings.mode not in ("demo", "real"):
            raise ValueError("SHUTTLE_MODE must be 'demo' or 'real'")
        self.net = net or load_network()
        self.lock = threading.RLock()
        self._logged = 0
        self.reset()

    # ----------------------------------------------------------------- set-up

    @property
    def manual(self) -> bool:
        m = self.settings.manual
        return self.settings.mode == "real" if m is None else m

    def reset(self, **changes) -> None:
        """Start the day over (demo) with optional new settings."""
        with self.lock:
            for key, value in changes.items():
                if value is not None:
                    setattr(self.settings, key, value)
            s = self.settings
            if s.policy not in POLICIES:
                raise ServiceError(f"알 수 없는 운행 규칙입니다: {s.policy}")
            if s.speed <= 0 or s.speed > 600:
                raise ServiceError("속도는 1~600배 사이로 정해 주세요.")
            self.clock = Clock(s.mode, parse_clock(s.start), s.speed, s.tz)
            self._day = self.clock.today()
            now = self.clock.now()
            self.dispatcher = Dispatcher(self.net, make_policy(s.policy, self.net), now=now, start_stop="A", auto_drive=not self.manual)
            self._seq = 0
            self._logged = 0
            self._feed: list[Request] = []
            if s.mode == "demo" and s.demo_requests:
                seed = s.seed if s.seed is not None else random.randrange(1 << 30)
                day = generate_day(self.net, DemandModel(), random.Random(seed), prefix="demo-")
                self._feed = [r for r in day if r.created_at >= now]

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
        self._write_log()
        return now

    def _write_log(self) -> None:
        log = self.dispatcher.log
        if not self.settings.log_dir or self._logged >= len(log):
            self._logged = len(log)
            return
        path = Path(self.settings.log_dir)
        path.mkdir(parents=True, exist_ok=True)
        day = datetime.now(self.clock.tz).strftime("%Y-%m-%d")
        with open(path / f"{day}.jsonl", "a", encoding="utf-8") as fh:
            for entry in log[self._logged:]:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._logged = len(log)

    # ---------------------------------------------------------------- actions

    def create_request(self, branch: str, dest: str, count: int, ready_in: float = 0.0) -> dict:
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
            self._seq += 1
            req = Request(
                id=f"q{self._seq:03d}",
                branch=branch,
                pickup=b.stop,
                dest=dest,
                count=count,
                created_at=now,
                ready_at=now + ready_in,
            )
            self.dispatcher.add_request(req)
            self.dispatcher.advance(now)
            return self._request_json(req)

    def cancel_request(self, rid: str) -> dict:
        with self.lock:
            self._tick_locked()
            req = self.dispatcher.requests.get(rid)
            if req is None:
                raise ServiceError("요청을 찾을 수 없습니다.", 404)
            if not self.dispatcher.cancel_request(rid):
                raise ServiceError("이미 탑승했거나 끝난 요청은 취소할 수 없습니다.", 409)
            self.dispatcher.advance(self.dispatcher.now)
            return self._request_json(req)

    def driver_depart(self) -> None:
        with self.lock:
            now = self._tick_locked()
            if not self.manual:
                raise ServiceError("시연 모드에서는 셔틀이 자동으로 움직입니다.", 409)
            if not self.dispatcher.driver_depart(now):
                raise ServiceError("지금은 출발할 차례가 아닙니다.", 409)

    def driver_arrive(self) -> None:
        with self.lock:
            now = self._tick_locked()
            if not self.manual:
                raise ServiceError("시연 모드에서는 셔틀이 자동으로 움직입니다.", 409)
            if not self.dispatcher.driver_arrive(now):
                raise ServiceError("지금은 이동 중이 아닙니다.", 409)

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
                "clock": format_clock(now),
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
        }

    def _step_json(self, s) -> dict:
        net = self.net
        reqs = self.dispatcher.requests

        def groups(ids):
            return [
                {"id": rid, "count": reqs[rid].count, "dest_short": net.stops[reqs[rid].dest].short, "branch": reqs[rid].branch}
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

        if d.status == DRIVING:
            target = first if first is not None and first.stop == d.stop else None
            detail = todo(target)
            returning = not detail and not d.onboard and d.stop in net.pickup_stops and not d.active()
            return {
                "headline": f"{'복귀' if returning else '다음 정류장'} · {net.stop_name(d.stop)}",
                "detail": f"{format_clock(d.until)} 도착 예정" + (f" · {detail}" if detail else ""),
                "action": "arrive" if self.manual else None,
                "action_label": "도착했어요" if self.manual else None,
            }
        if d.status == READY and d.next_stop:
            nxt = next((s for s in plan.steps if s.stop == d.next_stop), None)
            detail = todo(nxt)
            return {
                "headline": f"출발 → {net.stop_name(d.next_stop)}",
                "detail": detail or "다음 정류장으로 이동",
                "action": "depart",
                "action_label": "출발",
            }
        if d.status == WAIT:
            if here is not None and here.board:
                at = here.board_at if here.board_at is not None else d.until
                detail = f"{format_clock(at)}에 {sum(d.requests[r].count for r in here.board)}명 탑승 예정"
            else:
                detail = f"{format_clock(d.until)}까지 대기"
            return {"headline": f"{net.stop_name(d.stop)}에서 대기", "detail": detail, "action": None, "action_label": None}
        if d.status in (DWELL, FREE):
            return {"headline": f"{net.stop_name(d.stop)} · 타고 내리는 중", "detail": "잠시 후 다음 안내", "action": None, "action_label": None}
        detail = "들어온 요청이 없어요" if not d.active() else "다음 요청을 기다리는 중"
        return {"headline": f"{net.stop_name(d.stop)}에서 대기", "detail": detail, "action": None, "action_label": None}


def _weighted(reqs, fn) -> float | None:
    total = sum(r.count for r in reqs)
    if not total:
        return None
    return round(sum(fn(r) * r.count for r in reqs) / total, 1)
