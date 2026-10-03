"""Synthetic ride requests for one day.

Real request counts are unknown, so this follows the assumptions in
docs/solution.md: elementary classes finish irregularly, middle/high classes at
fixed times, and about half of the irregular finishes are announced 5-10
minutes ahead.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from .models import Request
from .network import Network, parse_clock


@dataclass(frozen=True)
class DemandModel:
    a_students: float = 20.0  # 학원 A (초등반) riders per day
    b_students: float = 14.0  # 학원 B (초·중등반)
    c_students: float = 1.5  # 학원 C (중·고등반) rarely rides
    b_fixed_share: float = 0.3  # share of B's groups that leave at fixed class ends (중등)
    announce_share: float = 0.5  # irregular finishes announced ahead of time
    lead_min: float = 5.0
    lead_max: float = 10.0
    fixed_lead: float = 10.0  # fixed class ends are always announced this far ahead

    # Ready-time windows for irregular finishes: (start, most likely, end)
    a_window: tuple[str, str, str] = ("15:30", "17:30", "19:30")
    b_window: tuple[str, str, str] = ("16:00", "18:30", "20:30")
    b_fixed_ends: tuple[str, ...] = ("18:50", "20:50")
    c_fixed_ends: tuple[str, ...] = ("19:50", "21:20")

    # Where riders go (destinations next to their branch are walked)
    a_dest: tuple[tuple[str, float], ...] = (("D1", 0.35), ("D3", 0.30), ("D4", 0.35))
    b_dest: tuple[tuple[str, float], ...] = (("D1", 0.40), ("D2", 0.40), ("D3", 0.20))
    c_dest: tuple[tuple[str, float], ...] = (("D1", 1 / 3), ("D2", 1 / 3), ("D3", 1 / 3))

    group_sizes: tuple[tuple[int, float], ...] = ((1, 0.6), (2, 0.3), (3, 0.1))


def _poisson(rng: random.Random, mean: float) -> int:
    # Knuth's method; means here are small.
    if mean <= 0:
        return 0
    limit = pow(2.718281828459045, -mean)
    k, p = 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def _pick(rng: random.Random, options: tuple[tuple, ...]):
    values = [o[0] for o in options]
    weights = [o[1] for o in options]
    return rng.choices(values, weights=weights, k=1)[0]


def _triangular(rng: random.Random, window: tuple[str, str, str]) -> float:
    lo, mode, hi = (parse_clock(x) for x in window)
    return rng.triangular(lo, hi, mode)


def generate_day(net: Network, model: DemandModel, rng: random.Random, prefix: str = "r") -> list[Request]:
    """Requests for one day, sorted by when they are posted."""
    mean_group = sum(size * w for size, w in model.group_sizes)
    out: list[Request] = []

    def add(branch: str, dest: str, ready: float, created: float) -> None:
        count = _pick(rng, model.group_sizes)
        out.append(
            Request(
                id=f"{prefix}{len(out) + 1:03d}",
                branch=branch,
                pickup=net.branches[branch].stop,
                dest=dest,
                count=count,
                created_at=round(created, 2),
                ready_at=round(ready, 2),
            )
        )

    def irregular(branch: str, n_groups: int, window, dests) -> None:
        for _ in range(n_groups):
            ready = _triangular(rng, window)
            lead = rng.uniform(model.lead_min, model.lead_max) if rng.random() < model.announce_share else 0.0
            add(branch, _pick(rng, dests), ready, ready - lead)

    def fixed(branch: str, n_groups: int, ends, dests) -> None:
        for _ in range(n_groups):
            end = parse_clock(rng.choice(ends)) + rng.uniform(0, 3)
            add(branch, _pick(rng, dests), end, end - model.fixed_lead)

    a_groups = _poisson(rng, model.a_students / mean_group)
    b_groups = _poisson(rng, model.b_students / mean_group)
    c_groups = _poisson(rng, model.c_students / mean_group)
    b_fixed = sum(1 for _ in range(b_groups) if rng.random() < model.b_fixed_share)

    irregular("A", a_groups, model.a_window, model.a_dest)
    irregular("B", b_groups - b_fixed, model.b_window, model.b_dest)
    fixed("B", b_fixed, model.b_fixed_ends, model.b_dest)
    fixed("C", c_groups, model.c_fixed_ends, model.c_dest)

    out.sort(key=lambda r: (r.created_at, r.id))
    return out
