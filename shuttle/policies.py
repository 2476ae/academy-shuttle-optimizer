"""Dispatch rules: how the vehicle picks its next move.

* ``fcfs``      - 요청 순서대로: models today's group-chat practice. Go to the
                  branch of the request that is ready first, take everyone ready
                  there, deliver them nearest-first, repeat.
* ``optimized`` - 자동 배차: re-plan the whole known workload with the exact
                  planner at every decision and follow its first step.
* ``fixed``     - 정시 출발: leave branch A only at fixed slot times, go A ->
                  B·C, deliver everyone, come back. Works without an app.
"""

from __future__ import annotations

import math
from typing import Callable, Sequence

from .models import Board, Decision, Go, Idle, Plan, Request, View
from .network import Network
from .planner import Weights, plan_route

EPS = 1e-9


def fit(groups: Sequence[Request], room: int) -> list[Request]:
    """Groups in the given order that fit into ``room`` seats (smaller ones may skip ahead)."""
    out = []
    for g in groups:
        if g.count <= room:
            out.append(g)
            room -= g.count
    return out


def nearest_dest(net: Network, stop: str, onboard: Sequence[Request]) -> str:
    return min({r.dest for r in onboard}, key=lambda d: (net.travel(stop, d), d))


class Policy:
    name = "policy"
    label = "운행 규칙"

    def __init__(self, net: Network):
        self.net = net

    def decide(self, view: View) -> Decision:  # pragma: no cover - interface
        raise NotImplementedError

    def plan(self, view: View) -> Plan:
        """Expected stop sequence for the known requests (for ETAs)."""
        return rollout(self, view)


class FcfsPolicy(Policy):
    name = "fcfs"
    label = "요청 순서대로 (지금 방식)"

    def decide(self, view: View) -> Decision:
        if view.onboard:
            return Go(nearest_dest(self.net, view.stop, view.onboard))
        if not view.waiting:
            return Idle()
        oldest = min(view.waiting, key=lambda r: (r.ready_at, r.created_at, r.id))
        if view.stop != oldest.pickup:
            return Go(oldest.pickup)
        if oldest.ready_at > view.now + EPS:
            return Board((), oldest.ready_at)
        take = self._ready_here(view)
        return Board(tuple(r.id for r in take), view.now)

    def _ready_here(self, view: View) -> list[Request]:
        here = sorted(
            (r for r in view.waiting if r.pickup == view.stop and r.ready_at <= view.now + EPS),
            key=lambda r: (r.ready_at, r.created_at, r.id),
        )
        return fit(here, self.net.capacity - view.load)


class OptimizedPolicy(Policy):
    name = "optimized"
    label = "자동 배차"

    def __init__(self, net: Network, weights: Weights = Weights()):
        super().__init__(net)
        self.weights = weights

    def plan(self, view: View) -> Plan:
        return plan_route(self.net, view.stop, max(view.now, view.free_at), view.onboard, view.waiting, self.weights)

    def decide(self, view: View) -> Decision:
        plan = self.plan(view)
        if not plan.steps:
            return Idle()
        first = plan.steps[0]
        if first.stop == view.stop and first.board:
            return Board(first.board, first.board_at if first.board_at is not None else view.now)
        if first.stop == view.stop:
            # Nothing to board here (drop-offs already happened on arrival); move on.
            nxt = plan.steps[1] if len(plan.steps) > 1 else None
            return Go(nxt.stop) if nxt else Idle()
        return Go(first.stop)


class FixedSchedulePolicy(Policy):
    """Departures from A at ``first_slot + k * interval``; loop A -> B·C -> deliveries -> A.

    A slot with nobody ready anywhere is skipped. If the vehicle gets back to A
    after its next slot time, it leaves at once instead of waiting a full slot.
    """

    name = "fixed"
    label = "정시 출발"
    MEMORY_KEY = "fixed_last_slot"

    def __init__(self, net: Network, interval: float = 30.0, first_slot: float | None = None, base: str = "A", second: str = "BC"):
        super().__init__(net)
        self.interval = interval
        self.first_slot = net.service_start + 30 if first_slot is None else first_slot
        self.base = base
        self.second = second

    def slot_time(self, k: int) -> float:
        return self.first_slot + k * self.interval

    def slot_at_or_after(self, t: float) -> int:
        return max(0, math.ceil((t - self.first_slot) / self.interval - EPS))

    def decide(self, view: View) -> Decision:
        net = self.net
        room = net.capacity - view.load
        if view.stop == self.base and not view.onboard:
            last = view.memory.get(self.MEMORY_KEY)
            k = self.slot_at_or_after(view.now) if last is None else last + 1
            if self.slot_time(k) > view.now + EPS:
                return Board((), self.slot_time(k))
            arrive_second = view.now + net.travel(self.base, self.second)
            ready_base = [r for r in view.waiting if r.pickup == self.base and r.ready_at <= view.now + EPS]
            ready_second = [r for r in view.waiting if r.pickup == self.second and r.ready_at <= arrive_second + EPS]
            if not ready_base and not ready_second:
                # Nobody to carry: let every slot up to now pass and wait for the next one.
                nxt = self.slot_at_or_after(view.now + EPS)
                if self.slot_time(nxt) <= view.now + EPS:
                    nxt += 1
                view.memory[self.MEMORY_KEY] = nxt - 1
                return Board((), self.slot_time(nxt))
            view.memory[self.MEMORY_KEY] = k
            take = fit(sorted(ready_base, key=lambda r: (r.ready_at, r.id)), room)
            if take and not view.visit_boarded:
                return Board(tuple(r.id for r in take), view.now)
            return Go(self.second)
        if view.stop == self.base and view.onboard:
            return Go(self.second)
        if view.stop == self.second and not view.visit_boarded:
            ready = [r for r in view.waiting if r.pickup == self.second and r.ready_at <= view.now + EPS]
            take = fit(sorted(ready, key=lambda r: (r.ready_at, r.id)), room)
            if take:
                return Board(tuple(r.id for r in take), view.now)
        if view.onboard:
            # Deliver in the best order for the students already on board.
            route = plan_route(net, view.stop, view.now, view.onboard, [], Weights())
            first = next((st for st in route.steps if st.stop != view.stop), None)
            return Go(first.stop) if first else Go(nearest_dest(net, view.stop, view.onboard))
        return Go(self.base)


POLICIES: dict[str, Callable[[Network], Policy]] = {
    "fcfs": FcfsPolicy,
    "optimized": OptimizedPolicy,
    "fixed": FixedSchedulePolicy,
}

POLICY_LABELS = {name: cls.label for name, cls in POLICIES.items()}  # type: ignore[attr-defined]


def make_policy(name: str, net: Network) -> Policy:
    try:
        return POLICIES[name](net)
    except KeyError:
        raise ValueError(f"unknown policy {name!r}; choose one of {', '.join(POLICIES)}") from None


def rollout(policy: Policy, view: View, max_steps: int = 400) -> Plan:
    """Play ``policy`` forward on the known requests only, to predict its stop sequence."""
    from .dispatcher import Dispatcher  # local import: dispatcher imports this module

    # Policies keep no state between decisions, so the same object can drive the copy.
    sim = Dispatcher.from_view(policy.net, policy, view)
    sim.run_until_done(max_events=max_steps)
    return Plan(tuple(sim.visits))
