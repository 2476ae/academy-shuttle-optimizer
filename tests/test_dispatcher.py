import copy
import random

import pytest

from shuttle import Dispatcher, Request, default_network, make_policy
from shuttle.demand import DemandModel, generate_day
from shuttle.dispatcher import DRIVING, READY
from shuttle.simulate import run_day

NET = default_network()
T0 = 17 * 60.0


def req(rid, branch, dest, count=1, ready=T0, created=None):
    return Request(rid, branch, NET.branches[branch].stop, dest, count, created_at=ready if created is None else created, ready_at=ready)


def replay_loads(visits, requests):
    load = 0
    for s in visits:
        load -= sum(requests[r].count for r in s.alight)
        load += sum(requests[r].count for r in s.board)
        assert 0 <= load <= NET.capacity
    assert load == 0


@pytest.mark.parametrize("policy", ["fcfs", "optimized", "fixed"])
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_every_policy_delivers_everyone(policy, seed):
    reqs = generate_day(NET, DemandModel(), random.Random(seed))
    d = Dispatcher(NET, make_policy(policy, NET), now=NET.service_start)
    for r in reqs:
        d.advance(r.created_at)
        d.add_request(copy.copy(r))
    d.run_until_done()
    assert all(r.status == "done" for r in d.requests.values())
    for r in d.requests.values():
        assert r.picked_at >= r.ready_at - 1e-9
        assert r.dropped_at >= r.picked_at + NET.travel(r.pickup, r.dest) - 1e-9
    replay_loads(d.visits, d.requests)


def test_fcfs_goes_to_the_earliest_ready_branch_first():
    d = Dispatcher(NET, make_policy("fcfs", NET), now=T0, start_stop="A")
    d.add_request(req("late-a", "A", "D4", ready=T0 + 10))
    d.add_request(req("early-b", "B", "D2", ready=T0))
    d.advance(T0)
    assert d.status == DRIVING and d.stop == "BC"


def test_fixed_schedule_leaves_only_on_slots():
    policy = make_policy("fixed", NET)  # slots at 15:30, 16:00, ...
    d = Dispatcher(NET, policy, now=15 * 60, start_stop="A")
    d.add_request(req("a", "A", "D3", ready=15 * 60 + 40))
    d.run_until_done()
    assert d.requests["a"].picked_at == pytest.approx(16 * 60)


def test_optimized_plan_matches_what_happens_without_new_requests():
    reqs = [req("a", "A", "D1", 2), req("b", "B", "D2", 1, ready=T0 + 4), req("c", "B", "D3", 3, ready=T0 + 9), req("d", "A", "D4", 1, ready=T0 + 15)]
    d = Dispatcher(NET, make_policy("optimized", NET), now=T0, start_stop="D1")
    for r in reqs:
        d.add_request(copy.copy(r))
    d.advance(T0)
    plan = d.plan()
    d.run_until_done()
    for r in d.requests.values():
        assert r.dropped_at == pytest.approx(plan.drop_eta(r.id))


def test_cancelled_request_is_not_served():
    d = Dispatcher(NET, make_policy("optimized", NET), now=T0, start_stop="A")
    d.add_request(req("a", "B", "D2"))
    d.add_request(req("b", "A", "D3"))
    assert d.cancel_request("a")
    d.run_until_done()
    assert d.requests["a"].status == "cancelled"
    assert d.requests["b"].status == "done"
    assert all("a" not in s.board for s in d.visits)


def test_manual_driving_waits_for_the_driver():
    d = Dispatcher(NET, make_policy("optimized", NET), now=T0, start_stop="A", auto_drive=False)
    d.add_request(req("b", "B", "D2", 2))
    d.advance(T0 + 30)
    assert d.status == READY and d.next_stop == "BC"
    assert d.driver_depart(T0 + 30)
    d.advance(T0 + 36)  # past the modelled 5 minutes
    assert d.status == DRIVING  # still driving until the driver says they arrived
    assert d.driver_arrive(T0 + 37)
    assert d.requests["b"].status == "onboard"
    assert d.driving == pytest.approx(7)  # actual minutes driven, not the model's 5


def test_run_day_reports_every_student():
    reqs = generate_day(NET, DemandModel(), random.Random(9))
    result = run_day(NET, "optimized", reqs)
    assert result.students == sum(r.count for r in reqs)
    assert len(result.done) == len(reqs)
