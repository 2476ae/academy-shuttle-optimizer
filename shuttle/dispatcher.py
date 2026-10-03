"""Event-driven state of the one shuttle.

The dispatcher owns the requests and the vehicle, asks the policy what to do
whenever the vehicle is free at a stop, and carries the decision out. The same
class drives the simulator (``auto_drive=True``: driving and dwelling take
their modelled time) and the live service (``auto_drive=False``: the driver
taps 출발 and 도착).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from .models import CANCELLED, DONE, ONBOARD, Board, Go, Plan, Request, Step, View
from .models import WAITING as REQ_WAITING
from .network import Network, format_clock
from .policies import Policy

INF = float("inf")
EPS = 1e-9

# Vehicle states
IDLE = "idle"  # at a stop, nothing to do
FREE = "free"  # at a stop, about to ask the policy
WAIT = "waiting"  # at a stop, waiting until ``until`` (students not ready yet, or a fixed slot)
DWELL = "dwell"  # students getting on or off, until ``until``
READY = "ready"  # manual driving: decided where to go, waiting for the driver to leave
DRIVING = "driving"  # on the way to ``stop``, arriving at ``until``


@dataclass
class _Visit:
    stop: str
    arrive: float
    board: list[str] = field(default_factory=list)
    alight: list[str] = field(default_factory=list)
    board_at: float | None = None

    def step(self, depart: float) -> Step:
        return Step(self.stop, self.arrive, depart, tuple(self.board), tuple(self.alight), self.board_at)


class Dispatcher:
    def __init__(
        self,
        net: Network,
        policy: Policy,
        now: float,
        start_stop: str = "A",
        auto_drive: bool = True,
        keep_log: bool = True,
    ):
        self.net = net
        self.policy = policy
        self.now = now
        self.auto_drive = auto_drive
        self.keep_log = keep_log
        self.requests: dict[str, Request] = {}

        self.stop = start_stop
        self.origin: str | None = None
        self.status = IDLE
        self.until = now
        self.depart_at: float | None = None
        self.next_stop: str | None = None
        self.onboard: list[str] = []
        self.visit_boarded = False
        self.memory: dict = {}

        self.visits: list[Step] = []
        self.log: list[dict] = []
        self.driving = 0.0
        self.version = 0
        self._visit: _Visit | None = _Visit(start_stop, now)
        self._needs_decision = True
        self._plan: Plan | None = None

    # ------------------------------------------------------------------ inputs

    def add_request(self, req: Request) -> None:
        if req.id in self.requests:
            raise ValueError(f"duplicate request id {req.id}")
        if req.count < 1 or req.count > self.net.capacity:
            raise ValueError(f"count must be between 1 and {self.net.capacity}")
        self.requests[req.id] = req
        self._needs_decision = True
        self._note(
            "request",
            f"{self.net.branches[req.branch].name} → {self.net.stop_name(req.dest)} {req.count}명 요청"
            f" (준비 {format_clock(req.ready_at)})",
            request=req.id,
        )

    def cancel_request(self, rid: str) -> bool:
        req = self.requests.get(rid)
        if req is None or req.status != REQ_WAITING:
            return False
        req.status = CANCELLED
        self._needs_decision = True
        self._note("cancel", f"{self.net.branches[req.branch].name} → {self.net.stop_name(req.dest)} {req.count}명 요청 취소", request=rid)
        return True

    def driver_depart(self, now: float) -> bool:
        """Manual driving: the driver leaves for the planned next stop."""
        self.advance(now)
        if self.status != READY or self.next_stop is None:
            return False
        self._depart(self.next_stop)
        self.advance(now)
        return True

    def driver_arrive(self, now: float) -> bool:
        """Manual driving: the driver reached the stop they were driving to."""
        self.advance(now)
        if self.status != DRIVING:
            return False
        self.now = max(self.now, now)
        # Count the time actually driven instead of the modelled travel time.
        if self.depart_at is not None and self.origin is not None:
            self.driving += (self.now - self.depart_at) - self.net.travel(self.origin, self.stop)
        self._arrive()
        self.advance(now)
        return True

    # -------------------------------------------------------------------- time

    def advance(self, until: float) -> None:
        """Carry out everything that happens up to time ``until``."""
        for _ in range(100_000):
            if self.status == DRIVING and self.auto_drive and self.until <= until + EPS:
                self.now = max(self.now, self.until)
                self._arrive()
            elif self.status in (DWELL, WAIT) and self.until <= until + EPS:
                self.now = max(self.now, self.until)
                self.status = FREE
                self._needs_decision = True
                self._changed()
            elif self.status in (FREE, IDLE, WAIT, READY) and self._needs_decision:
                self._decide()
            else:
                break
        else:  # pragma: no cover - defensive
            raise RuntimeError("dispatcher did not settle")
        if until != INF:
            self.now = max(self.now, until)

    def next_event_time(self) -> float | None:
        if self._needs_decision and self.status in (FREE, IDLE, WAIT, READY):
            return self.now
        if self.status in (DWELL, WAIT) or (self.status == DRIVING and self.auto_drive):
            return self.until
        return None

    def active(self) -> list[Request]:
        return [r for r in self.requests.values() if r.status in (REQ_WAITING, ONBOARD)]

    def run_until_done(self, max_events: int = 10_000) -> None:
        """Advance until every known request is delivered and the vehicle has settled."""
        for _ in range(max_events):
            if not self.active() and self.status not in (DRIVING, DWELL):
                break
            nxt = self.next_event_time()
            if nxt is None:
                break
            self.advance(nxt)
        self._close_visit(max(self.now, self.until if self.status == DWELL else self.now))

    # ------------------------------------------------------------------- plans

    def view(self) -> View:
        if self.status == DRIVING:
            free_at = self.until if self.auto_drive else max(self.now, self.until)
        elif self.status == DWELL:
            free_at = self.until
        else:
            free_at = self.now
        return View(
            now=self.now,
            stop=self.stop,
            free_at=free_at,
            onboard=[self.requests[rid] for rid in self.onboard],
            waiting=[r for r in self.requests.values() if r.status == REQ_WAITING],
            visit_boarded=self.visit_boarded,
            arriving=self.status == DRIVING,
            memory=self.memory,
        )

    def plan(self) -> Plan:
        """Expected stop sequence for every known request (cached until something changes)."""
        if self._plan is None:
            self._plan = self.policy.plan(self.view())
        return self._plan

    @classmethod
    def from_view(cls, net: Network, policy: Policy, view: View) -> "Dispatcher":
        """A headless copy that starts from ``view`` and knows only its requests."""
        d = cls(net, policy, now=view.now, start_stop=view.stop, auto_drive=True, keep_log=False)
        d.memory = copy.deepcopy(view.memory)
        for r in view.onboard:
            rc = copy.copy(r)
            d.requests[rc.id] = rc
            d.onboard.append(rc.id)
        for r in view.waiting:
            d.requests[r.id] = copy.copy(r)
        if view.arriving:
            d.status = DRIVING
            d.until = max(view.now, view.free_at)
            d.depart_at = view.now
            d._visit = None
            d._needs_decision = False
        else:
            d.visit_boarded = view.visit_boarded
            if view.free_at > view.now + EPS:
                d.status = DWELL
                d.until = view.free_at
                d._needs_decision = False
        return d

    # --------------------------------------------------------------- internals

    def _decide(self) -> None:
        self._needs_decision = False
        decision = self.policy.decide(self.view())
        if isinstance(decision, Board):
            if decision.at > self.now + EPS:
                self.status = WAIT
                self.until = decision.at
                self._changed()
                return
            ids = self._boardable(decision.groups)
            if ids:
                self._board(ids)
            else:
                self.status = IDLE
                self._changed()
        elif isinstance(decision, Go):
            if decision.stop == self.stop:
                self.status = IDLE
                self._changed()
            else:
                self._start_drive(decision.stop)
        elif self.stop not in self.net.pickup_stops:
            # Nothing to do at a drop-off stop: head back to the nearest branch.
            self._start_drive(self.net.nearest_pickup(self.stop))
        else:
            self.status = IDLE
            self._changed()

    def _boardable(self, ids) -> list[str]:
        room = self.net.capacity - sum(self.requests[rid].count for rid in self.onboard)
        out = []
        for rid in ids:
            r = self.requests.get(rid)
            if r is None or r.status != REQ_WAITING or r.pickup != self.stop or r.ready_at > self.now + EPS:
                continue
            if r.count <= room:
                out.append(rid)
                room -= r.count
        return out

    def _board(self, ids: list[str]) -> None:
        names = []
        for rid in ids:
            r = self.requests[rid]
            r.status = ONBOARD
            r.picked_at = self.now
            self.onboard.append(rid)
            names.append(f"{self.net.stops[r.dest].short} {r.count}명")
        self.visit_boarded = True
        if self._visit is None:
            self._visit = _Visit(self.stop, self.now)
        self._visit.board.extend(ids)
        if self._visit.board_at is None:
            self._visit.board_at = self.now
        self.status = DWELL
        self.until = self.now + self.net.dwell
        self._note("board", f"{self.net.stop_name(self.stop)}에서 탑승: {', '.join(names)}", requests=ids)

    def _start_drive(self, stop: str) -> None:
        if self.auto_drive:
            self._depart(stop)
        else:
            self.status = READY
            self.next_stop = stop
            self._changed()

    def _depart(self, stop: str) -> None:
        self._close_visit(self.now)
        self.origin = self.stop
        self.stop = stop
        self.next_stop = None
        self.depart_at = self.now
        travel = self.net.travel(self.origin, stop)
        self.until = self.now + travel
        self.driving += travel
        self.status = DRIVING
        self._note("depart", f"{self.net.stop_name(self.origin)} 출발 → {self.net.stop_name(stop)} ({format_clock(self.until)} 도착 예정)")

    def _arrive(self) -> None:
        self.visit_boarded = False
        self._visit = _Visit(self.stop, self.now)
        alight = [rid for rid in self.onboard if self.requests[rid].dest == self.stop]
        for rid in alight:
            r = self.requests[rid]
            r.status = DONE
            r.dropped_at = self.now
            self.onboard.remove(rid)
        self.origin = None
        if alight:
            self._visit.alight.extend(alight)
            self.status = DWELL
            self.until = self.now + self.net.dwell
            total = sum(self.requests[rid].count for rid in alight)
            self._note("alight", f"{self.net.stop_name(self.stop)} 도착: {total}명 하차", requests=alight)
        else:
            self.status = FREE
            self._note("arrive", f"{self.net.stop_name(self.stop)} 도착")
        self._needs_decision = True

    def _close_visit(self, depart: float) -> None:
        v = self._visit
        if v is not None and (v.board or v.alight):
            self.visits.append(v.step(depart))
        self._visit = None

    def _changed(self) -> None:
        self.version += 1
        self._plan = None

    def _note(self, kind: str, text: str, **extra) -> None:
        self._changed()
        if self.keep_log:
            self.log.append({"t": self.now, "kind": kind, "text": text, **extra})
