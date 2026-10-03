"""Compare dispatch rules on synthetic days.

    python -m shuttle.simulate --days 200 --seed 1

Every rule sees exactly the same requests on each day. Results depend on the
demand assumptions in ``shuttle.demand``; they show how the rules compare,
not what the real numbers will be.
"""

from __future__ import annotations

import argparse
import copy
import csv
import random
import sys
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .demand import DemandModel, generate_day
from .dispatcher import Dispatcher
from .models import DONE, Request
from .network import Network, load_network
from .policies import POLICIES, Policy, make_policy


@dataclass
class DayResult:
    policy: str
    requests: list[Request]
    driving: float
    stop_visits: int

    @property
    def done(self) -> list[Request]:
        return [r for r in self.requests if r.status == DONE]

    @property
    def students(self) -> int:
        return sum(r.count for r in self.requests)


def run_day(net: Network, policy: Policy | str, requests: Sequence[Request], start: float | None = None, start_stop: str = "A") -> DayResult:
    """Feed the day's requests to a fresh dispatcher in posting order and drive until all are home."""
    if isinstance(policy, str):
        policy = make_policy(policy, net)
    t0 = net.service_start if start is None else start
    d = Dispatcher(net, policy, now=min([t0, *(r.created_at for r in requests)]), start_stop=start_stop, keep_log=False)
    for r in sorted(requests, key=lambda r: (r.created_at, r.id)):
        d.advance(r.created_at)
        d.add_request(copy.copy(r))
    d.run_until_done()
    undelivered = [r.id for r in d.requests.values() if r.status != DONE]
    if undelivered:
        raise RuntimeError(f"{policy.name}: requests not delivered: {undelivered[:5]}")
    return DayResult(policy.name, list(d.requests.values()), d.driving, len(d.visits))


def _expand(pairs: Iterable[tuple[float, int]]) -> list[float]:
    out: list[float] = []
    for value, count in pairs:
        out.extend([value] * count)
    return sorted(out)


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    k = (len(sorted_values) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


@dataclass
class Summary:
    policy: str
    label: str
    days: int
    students: int
    wait_mean: float
    wait_p90: float
    wait_max: float
    ride_mean: float
    total_mean: float
    total_p90: float
    home_within_20: float  # share of students home within 20 minutes of being ready
    driving_per_day: float
    visits_per_day: float
    extra: dict = field(default_factory=dict)


def summarize(results: Sequence[DayResult], label: str = "") -> Summary:
    done = [r for day in results for r in day.done]
    waits = _expand((r.wait, r.count) for r in done)
    rides = _expand((r.dropped_at - r.picked_at, r.count) for r in done)
    totals = _expand((r.total, r.count) for r in done)
    n = len(totals) or 1
    days = len(results) or 1
    return Summary(
        policy=results[0].policy if results else "",
        label=label,
        days=len(results),
        students=len(totals),
        wait_mean=sum(waits) / n,
        wait_p90=_percentile(waits, 0.9),
        wait_max=max(waits, default=0.0),
        ride_mean=sum(rides) / n,
        total_mean=sum(totals) / n,
        total_p90=_percentile(totals, 0.9),
        home_within_20=sum(1 for t in totals if t <= 20 + 1e-9) / n,
        driving_per_day=sum(d.driving for d in results) / days,
        visits_per_day=sum(d.stop_visits for d in results) / days,
    )


def compare(
    net: Network,
    policies: Sequence[str] = ("fcfs", "fixed", "optimized"),
    days: int = 100,
    seed: int = 1,
    model: DemandModel = DemandModel(),
) -> list[Summary]:
    rng = random.Random(seed)
    day_requests = [generate_day(net, model, random.Random(rng.random())) for _ in range(days)]
    out = []
    for name in policies:
        results = [run_day(net, name, reqs) for reqs in day_requests]
        out.append(summarize(results, label=POLICIES[name].label))  # type: ignore[attr-defined]
    return out


def format_table(summaries: Sequence[Summary]) -> str:
    head = "| 운행 규칙 | 평균 대기 | 대기 상위 10% | 최대 대기 | 평균 탑승 | 평균 귀가 시간 | 20분 안에 귀가 | 하루 운전 |"
    rule = "|---|---:|---:|---:|---:|---:|---:|---:|"
    rows = [head, rule]
    for s in summaries:
        rows.append(
            f"| {s.label} | {s.wait_mean:.1f}분 | {s.wait_p90:.1f}분 | {s.wait_max:.0f}분 | {s.ride_mean:.1f}분 "
            f"| {s.total_mean:.1f}분 | {s.home_within_20 * 100:.0f}% | {s.driving_per_day:.0f}분 |"
        )
    return "\n".join(rows)


def write_csv(path: str, summaries: Sequence[Summary]) -> None:
    fields = [
        "policy", "label", "days", "students", "wait_mean", "wait_p90", "wait_max", "ride_mean",
        "total_mean", "total_p90", "home_within_20", "driving_per_day", "visits_per_day",
    ]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for s in summaries:
            w.writerow({f: getattr(s, f) for f in fields})


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compare shuttle dispatch rules on synthetic days.")
    ap.add_argument("--days", type=int, default=100, help="number of synthetic days (default 100)")
    ap.add_argument("--seed", type=int, default=1, help="random seed (default 1)")
    ap.add_argument("--policies", default="fcfs,fixed,optimized", help="comma-separated rules to compare")
    ap.add_argument("--announce", type=float, default=None, help="share of irregular finishes announced ahead (0-1)")
    ap.add_argument("--scale", type=float, default=1.0, help="multiply the number of riders (e.g. 1.5 for a busy day)")
    ap.add_argument("--csv", help="also write the summary to this CSV file")
    args = ap.parse_args(argv)

    net = load_network()
    base = DemandModel()
    model = DemandModel(
        a_students=base.a_students * args.scale,
        b_students=base.b_students * args.scale,
        c_students=base.c_students * args.scale,
        announce_share=base.announce_share if args.announce is None else args.announce,
    )
    names = [p.strip() for p in args.policies.split(",") if p.strip()]
    unknown = [p for p in names if p not in POLICIES]
    if unknown:
        ap.error(f"unknown policy: {', '.join(unknown)} (choose from {', '.join(POLICIES)})")

    summaries = compare(net, names, days=args.days, seed=args.seed, model=model)
    students = summaries[0].students if summaries else 0
    print(f"가상의 하루 {args.days}일, 학생 {students}명 (하루 평균 {students / max(args.days, 1):.1f}명), seed {args.seed}\n")
    print(format_table(summaries))
    if args.csv:
        write_csv(args.csv, summaries)
        print(f"\nCSV: {args.csv}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
