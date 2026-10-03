import random

import pytest

from shuttle import Request, default_network, plan_route
from shuttle.planner import Weights, route_cost

NET = default_network()
T0 = 17 * 60.0


def req(rid, pickup, dest, count=1, ready=T0, branch=None):
    branch = branch or ("A" if pickup == "A" else "B")
    return Request(rid, branch, pickup, dest, count, created_at=ready, ready_at=ready)


def check_feasible(plan, start_stop, start, onboard, waiting):
    groups = {r.id: r for r in [*onboard, *waiting]}
    load = sum(r.count for r in onboard)
    on = {r.id for r in onboard}
    t = start
    stop = start_stop
    for s in plan.steps:
        assert s.arrive >= t + NET.travel(stop, s.stop) - 1e-9
        for rid in s.alight:
            assert rid in on and groups[rid].dest == s.stop
            on.remove(rid)
            load -= groups[rid].count
        for rid in s.board:
            g = groups[rid]
            assert g.pickup == s.stop
            assert (s.board_at or s.arrive) >= g.ready_at - 1e-9
            on.add(rid)
            load += g.count
        assert load <= NET.capacity
        t, stop = s.depart, s.stop
    assert not on
    boarded = {rid for s in plan.steps for rid in s.board}
    assert boarded == {r.id for r in waiting}


def test_drops_onboard_group_directly():
    g = req("g1", "A", "D3")
    plan = plan_route(NET, "A", T0, [g], [])
    assert [s.stop for s in plan.steps] == ["D3"]
    assert plan.drop_eta("g1") == pytest.approx(T0 + NET.travel("A", "D3"))


def test_boards_ready_group_then_drops():
    g = req("g1", "BC", "D2", count=2)
    plan = plan_route(NET, "BC", T0, [], [g])
    assert [s.stop for s in plan.steps] == ["BC", "D2"]
    assert plan.pickup_eta("g1") == pytest.approx(T0)
    assert plan.drop_eta("g1") == pytest.approx(T0 + NET.dwell + NET.travel("BC", "D2"))


def test_waits_until_group_is_ready():
    g = req("g1", "A", "D4", ready=T0 + 5)
    plan = plan_route(NET, "A", T0, [], [g])
    assert plan.steps[0].board_at == pytest.approx(T0 + 5)
    check_feasible(plan, "A", T0, [], [g])


def test_respects_capacity():
    waiting = [req(f"g{i}", "A", "D1", count=3) for i in range(4)]  # 12 students, 8 seats
    plan = plan_route(NET, "A", T0, [], waiting)
    check_feasible(plan, "A", T0, [], waiting)
    assert sum(1 for s in plan.steps if s.stop == "D1") == 2


def test_combines_far_trips_from_both_branches():
    waiting = [req("a", "A", "D1"), req("b", "BC", "D1")]
    plan = plan_route(NET, "A", T0, [], waiting)
    stops = [s.stop for s in plan.steps]
    assert stops == ["A", "BC", "D1"]


def test_cost_matches_route_cost():
    waiting = [req("a", "A", "D3", 2), req("b", "BC", "D2", 1, ready=T0 + 3), req("c", "BC", "D1", 3, ready=T0 + 8)]
    plan = plan_route(NET, "D1", T0, [], waiting)
    groups = {r.id: r for r in waiting}
    assert route_cost(NET, plan.steps, "D1", T0, groups) == pytest.approx(plan.cost)


def brute_force(stop, t, onboard, pending, w=Weights(), max_waits=3):
    """Same action model as the planner, searched exhaustively."""
    best = float("inf")

    def rec(stop, t, onboard, pending, cost):
        nonlocal best
        if not onboard and not pending:
            best = min(best, cost)
            return
        load = sum(g.count for g in onboard)
        for d in {g.dest for g in onboard}:
            tt = NET.travel(stop, d)
            arr = t + tt
            off = [g for g in onboard if g.dest == d]
            add = w.drive * tt + sum(g.count * (arr - g.ready_at) for g in off)
            rec(d, arr + NET.dwell, [g for g in onboard if g.dest != d], pending, cost + add)
        for p in {g.pickup for g in pending}:
            tt = NET.travel(stop, p)
            arr = t + tt
            here = sorted((g for g in pending if g.pickup == p), key=lambda g: (g.ready_at, g.id))
            waits = sorted({g.ready_at for g in here if g.ready_at > arr})[:max_waits]
            seen = set()
            for tau in [arr, *waits]:
                room = NET.capacity - load
                take = []
                for g in here:
                    if g.ready_at <= tau + 1e-9 and g.count <= room:
                        take.append(g)
                        room -= g.count
                key = tuple(g.id for g in take)
                if not take or key in seen:
                    continue
                seen.add(key)
                add = w.drive * tt
                for g in take:
                    over = tau - g.ready_at - w.wait_cap
                    if over > 0:
                        add += w.over_cap * g.count * over
                rest = [g for g in pending if g not in take]
                rec(p, tau + NET.dwell, onboard + take, rest, cost + add)

    rec(stop, t, list(onboard), list(pending), 0.0)
    return best


@pytest.mark.parametrize("seed", range(25))
def test_matches_brute_force_on_small_cases(seed):
    rng = random.Random(seed)
    dests = {"A": ["D1", "D3", "D4"], "BC": ["D1", "D2", "D3"]}
    pending = []
    for i in range(rng.randint(1, 4)):
        p = rng.choice(["A", "BC"])
        pending.append(req(f"p{i}", p, rng.choice(dests[p]), rng.randint(1, 3), ready=T0 + rng.choice([0, 0, 2, 6, 12])))
    onboard = []
    for i in range(rng.randint(0, 2)):
        p = rng.choice(["A", "BC"])
        onboard.append(req(f"o{i}", p, rng.choice(dests[p]), rng.randint(1, 2), ready=T0 - rng.randint(0, 10)))
    start = rng.choice(["A", "BC", "D1", "D3"])
    plan = plan_route(NET, start, T0, onboard, pending)
    check_feasible(plan, start, T0, onboard, pending)
    assert plan.cost == pytest.approx(brute_force(start, T0, onboard, pending))
