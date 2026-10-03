"""Exact route planner for one vehicle.

Given where the vehicle is, who is on board and who is waiting, find the stop
sequence that minimises the students' total time from "ready" to "home",
plus a small charge for driving and a heavy charge for waits over a cap.

The search space is small (two pickup stops, four drop-off stops), so a
depth-first branch-and-bound over stop visits finds the optimum quickly:

* visiting a drop-off stop lets off everyone on board who is going there;
* visiting a pickup stop boards the groups that are ready, in ready order,
  as long as seats remain. The vehicle may also wait there for a group that
  will be ready soon, which is how batching is decided.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .models import Plan, Request, Step
from .network import Network

INF = float("inf")


@dataclass(frozen=True)
class Weights:
    drive: float = 0.5  # cost of one driving minute, in student-minutes
    wait_cap: float = 30.0  # minutes a student should wait at most
    over_cap: float = 10.0  # extra cost per student-minute waited beyond the cap


@dataclass(frozen=True)
class _Group:
    id: str
    pickup: str
    dest: str
    count: int
    ready: float


def plan_route(
    net: Network,
    stop: str,
    start: float,
    onboard: Sequence[Request],
    waiting: Sequence[Request],
    weights: Weights = Weights(),
    max_groups: int = 8,
    max_waits: int = 3,
    node_limit: int = 40_000,
) -> Plan:
    """Best stop sequence from ``stop`` at time ``start``.

    Only the ``max_groups`` earliest-ready waiting groups are optimised
    together; any others are planned afterwards in further rounds, so every
    known group gets an ETA. ``max_waits`` limits how many future ready times
    the vehicle considers waiting for at a pickup stop.
    """
    on = [_Group(r.id, r.pickup, r.dest, r.count, r.ready_at) for r in onboard]
    pending = sorted(
        (_Group(r.id, r.pickup, r.dest, min(r.count, net.capacity), r.ready_at) for r in waiting),
        key=lambda g: (g.ready, g.id),
    )
    steps: list[Step] = []
    total = 0.0
    while True:
        batch, pending = pending[:max_groups], pending[max_groups:]
        part_steps, part_cost = _search(net, stop, start, on, batch, weights, max_waits, node_limit)
        steps.extend(part_steps)
        total += part_cost
        if not pending:
            break
        if part_steps:
            stop, start = part_steps[-1].stop, part_steps[-1].depart
        on = []
    return Plan(tuple(steps), total)


def _search(
    net: Network,
    stop0: str,
    t0: float,
    onboard: list[_Group],
    pending: list[_Group],
    w: Weights,
    max_waits: int,
    node_limit: int,
) -> tuple[list[Step], float]:
    groups = onboard + pending
    n = len(groups)
    if n == 0:
        return [], 0.0

    travel = net.travel
    dwell = net.dwell
    cap = net.capacity
    on_mask0 = (1 << len(onboard)) - 1
    pend_mask0 = ((1 << n) - 1) ^ on_mask0
    load0 = sum(g.count for g in onboard)

    by_pickup: dict[str, list[int]] = {}
    for i in range(len(onboard), n):
        by_pickup.setdefault(groups[i].pickup, []).append(i)
    for idxs in by_pickup.values():
        idxs.sort(key=lambda i: (groups[i].ready, groups[i].id))

    def bits(mask: int):
        i = 0
        while mask:
            if mask & 1:
                yield i
            mask >>= 1
            i += 1

    def lower_bound(stop: str, t: float, on_mask: int, pend_mask: int) -> float:
        """Admissible: every group needs at least its direct path, the vehicle the longest leg."""
        total = 0.0
        far = 0.0
        for i in bits(on_mask):
            g = groups[i]
            tt = travel(stop, g.dest)
            total += g.count * (t + tt - g.ready)
            if tt > far:
                far = tt
        for i in bits(pend_mask):
            g = groups[i]
            tb = travel(stop, g.pickup)
            board = max(t + tb, g.ready)
            leg = travel(g.pickup, g.dest)
            total += g.count * (board + dwell + leg - g.ready)
            over = board - g.ready - w.wait_cap
            if over > 0:
                total += w.over_cap * g.count * over
            if tb + leg > far:
                far = tb + leg
        return total + w.drive * far

    best_cost = INF
    best_steps: list[Step] = []
    nodes = 0
    # Pareto front of (time, cost) per (stop, onboard, pending): an earlier,
    # cheaper arrival at the same situation can never do worse afterwards.
    seen: dict[tuple[str, int, int], list[tuple[float, float]]] = {}

    def dominated(key: tuple[str, int, int], t: float, cost: float) -> bool:
        front = seen.get(key)
        if front is None:
            seen[key] = [(t, cost)]
            return False
        for ft, fc in front:
            if ft <= t + 1e-9 and fc <= cost + 1e-9:
                return True
        front[:] = [(ft, fc) for ft, fc in front if not (t <= ft + 1e-9 and cost <= fc + 1e-9)]
        front.append((t, cost))
        return False

    def children(stop: str, t: float, load: int, on_mask: int, pend_mask: int, cost: float):
        out = []
        # Drop-offs: one child per destination of the groups on board.
        dests: dict[str, list[int]] = {}
        for i in bits(on_mask):
            dests.setdefault(groups[i].dest, []).append(i)
        for d, idxs in dests.items():
            tt = travel(stop, d)
            arr = t + tt
            add = w.drive * tt
            freed = 0
            mask = 0
            for i in idxs:
                g = groups[i]
                add += g.count * (arr - g.ready)
                freed += g.count
                mask |= 1 << i
            step = Step(d, arr, arr + dwell, alight=tuple(groups[i].id for i in idxs))
            out.append((cost + add, d, arr + dwell, load - freed, on_mask & ~mask, pend_mask, step))
        # Pickups: board now, or wait for one of the next few ready times.
        free = cap - load
        for p, idxs in by_pickup.items():
            avail = [i for i in idxs if pend_mask >> i & 1]
            if not avail:
                continue
            tt = travel(stop, p)
            arr = t + tt
            waits = sorted({groups[i].ready for i in avail if groups[i].ready > arr})[:max_waits]
            previous: tuple[int, ...] = ()
            for tau in [arr, *waits]:
                take = []
                room = free
                for i in avail:
                    g = groups[i]
                    if g.ready <= tau + 1e-9 and g.count <= room:
                        take.append(i)
                        room -= g.count
                take_t = tuple(take)
                if not take_t or take_t == previous:
                    continue
                previous = take_t
                add = w.drive * tt
                mask = 0
                boarded = 0
                for i in take_t:
                    g = groups[i]
                    over = tau - g.ready - w.wait_cap
                    if over > 0:
                        add += w.over_cap * g.count * over
                    mask |= 1 << i
                    boarded += g.count
                step = Step(
                    p,
                    arr,
                    tau + dwell,
                    board=tuple(groups[i].id for i in take_t),
                    board_at=tau,
                )
                out.append((cost + add, p, tau + dwell, load + boarded, on_mask | mask, pend_mask & ~mask, step))
        return out

    def dfs(stop: str, t: float, load: int, on_mask: int, pend_mask: int, cost: float, path: list[Step]) -> None:
        nonlocal best_cost, best_steps, nodes
        if on_mask == 0 and pend_mask == 0:
            if cost < best_cost - 1e-9:
                best_cost = cost
                best_steps = list(path)
            return
        nodes += 1
        if nodes > node_limit and best_cost < INF:
            return
        if cost + lower_bound(stop, t, on_mask, pend_mask) >= best_cost - 1e-9:
            return
        if dominated((stop, on_mask, pend_mask), t, cost):
            return
        kids = children(stop, t, load, on_mask, pend_mask, cost)
        kids.sort(key=lambda k: k[0] + lower_bound(k[1], k[2], k[4], k[5]))
        for c, s, tt, ld, om, pm, step in kids:
            path.append(step)
            dfs(s, tt, ld, om, pm, c, path)
            path.pop()

    dfs(stop0, t0, load0, on_mask0, pend_mask0, 0.0, [])
    if best_cost == INF:
        raise RuntimeError("planner found no feasible route")
    return best_steps, best_cost


def route_cost(net: Network, steps: Sequence[Step], start_stop: str, start: float, groups: dict[str, Request], weights: Weights = Weights()) -> float:
    """Cost of a given step sequence under the planner's objective (for tests and reports)."""
    cost = 0.0
    stop = start_stop
    for s in steps:
        cost += weights.drive * net.travel(stop, s.stop)
        for rid in s.board:
            g = groups[rid]
            board_t = s.board_at if s.board_at is not None else s.arrive
            over = board_t - g.ready_at - weights.wait_cap
            if over > 0:
                cost += weights.over_cap * g.count * over
        for rid in s.alight:
            g = groups[rid]
            cost += g.count * (s.arrive - g.ready_at)
        stop = s.stop
    return cost
