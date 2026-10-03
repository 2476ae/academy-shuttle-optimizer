"""Stops, branches and travel times.

The defaults follow the assumptions in docs/solution.md. Real place names are
kept out of the repository: put them in a local JSON file and point
``SHUTTLE_CONFIG`` at it (see ``load_network``).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class Stop:
    id: str
    name: str
    short: str
    kind: str  # "pickup" or "dropoff"
    pos: tuple[float, float] = (0.0, 0.0)  # schematic map position


@dataclass(frozen=True)
class Branch:
    id: str
    name: str
    level: str
    stop: str
    destinations: tuple[str, ...]  # destinations whose students ride the shuttle


@dataclass
class Network:
    stops: dict[str, Stop]
    branches: dict[str, Branch]
    minutes: dict[tuple[str, str], float]
    capacity: int = 8
    dwell: float = 1.0
    service_start: float = 15 * 60
    service_end: float = 21 * 60 + 30
    home_order: tuple[str, ...] = ("A", "BC")
    _closed: dict[tuple[str, str], float] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._closed = _shortest_paths(list(self.stops), self.minutes)

    def travel(self, a: str, b: str) -> float:
        """Driving minutes from ``a`` to ``b`` along the fastest known route."""
        if a == b:
            return 0.0
        return self._closed[(a, b)]

    @property
    def pickup_stops(self) -> tuple[str, ...]:
        return tuple(s.id for s in self.stops.values() if s.kind == "pickup")

    @property
    def dropoff_stops(self) -> tuple[str, ...]:
        return tuple(s.id for s in self.stops.values() if s.kind == "dropoff")

    def nearest_pickup(self, stop: str) -> str:
        """Closest pickup stop; ties go to the earlier stop in ``home_order``."""
        order = [s for s in self.home_order if s in self.stops] or list(self.pickup_stops)
        return min(order, key=lambda p: (self.travel(stop, p), order.index(p)))

    def stop_name(self, stop: str) -> str:
        return self.stops[stop].name

    def to_dict(self) -> dict:
        return {
            "capacity": self.capacity,
            "dwell": self.dwell,
            "service_start": self.service_start,
            "service_end": self.service_end,
            "stops": [
                {"id": s.id, "name": s.name, "short": s.short, "kind": s.kind, "pos": list(s.pos)}
                for s in self.stops.values()
            ],
            "branches": [
                {
                    "id": b.id,
                    "name": b.name,
                    "level": b.level,
                    "stop": b.stop,
                    "destinations": list(b.destinations),
                }
                for b in self.branches.values()
            ],
            "minutes": {
                a: {b: self.travel(a, b) for b in self.stops} for a in self.stops
            },
        }


def _shortest_paths(ids: list[str], minutes: dict[tuple[str, str], float]) -> dict[tuple[str, str], float]:
    """All-pairs fastest times, so the planner's bounds can rely on the triangle inequality."""
    inf = float("inf")
    dist = {(a, b): (0.0 if a == b else minutes.get((a, b), inf)) for a in ids for b in ids}
    for k in ids:
        for i in ids:
            dik = dist[(i, k)]
            if dik == inf:
                continue
            for j in ids:
                alt = dik + dist[(k, j)]
                if alt < dist[(i, j)]:
                    dist[(i, j)] = alt
    missing = [pair for pair, d in dist.items() if d == inf]
    if missing:
        raise ValueError(f"no route between {missing[0][0]} and {missing[0][1]}")
    return dist


def _symmetric(rows: Iterable[tuple[str, str, float]]) -> dict[tuple[str, str], float]:
    out: dict[tuple[str, str], float] = {}
    for a, b, m in rows:
        out[(a, b)] = float(m)
        out[(b, a)] = float(m)
    return out


# Minutes between stops. A<->BC, A<->D1 and BC<->D1 come from the driver;
# the rest are estimates from the map (docs/solution.md, "가정").
DEFAULT_MINUTES = (
    ("A", "BC", 5),
    ("A", "D1", 10),
    ("BC", "D1", 10),
    ("A", "D2", 1),
    ("A", "D3", 7),
    ("A", "D4", 4),
    ("BC", "D2", 5),
    ("BC", "D3", 3),
    ("BC", "D4", 2),
    ("D1", "D2", 10),
    ("D1", "D3", 12),
    ("D1", "D4", 9),
    ("D2", "D3", 7),
    ("D2", "D4", 4),
    ("D3", "D4", 3),
)


def default_network() -> Network:
    stops = {
        "A": Stop("A", "학원 A", "A", "pickup", (700, 120)),
        "BC": Stop("BC", "학원 B·C", "B·C", "pickup", (790, 290)),
        "D1": Stop("D1", "① 마을", "①", "dropoff", (100, 200)),
        "D2": Stop("D2", "② 아파트", "②", "dropoff", (540, 80)),
        "D3": Stop("D3", "③ 아파트", "③", "dropoff", (895, 380)),
        "D4": Stop("D4", "④ 아파트", "④", "dropoff", (600, 330)),
    }
    branches = {
        "A": Branch("A", "학원 A", "초등반", "A", ("D1", "D3", "D4")),
        "B": Branch("B", "학원 B", "초·중등반", "BC", ("D1", "D2", "D3")),
        "C": Branch("C", "학원 C", "중·고등반", "BC", ("D1", "D2", "D3")),
    }
    return Network(stops=stops, branches=branches, minutes=_symmetric(DEFAULT_MINUTES))


def load_network(path: str | os.PathLike[str] | None = None) -> Network:
    """Default network, optionally overridden by a local JSON file.

    The file may set ``capacity``, ``dwell``, ``service_start``/``service_end``
    (``"HH:MM"``), per-stop ``name``/``short``, per-branch ``name``/``destinations``,
    and ``minutes`` as ``[[from, to, minutes], ...]`` (applied both ways)::

        {"stops": {"D1": {"name": "① OO리"}}, "minutes": [["BC", "D3", 4]]}
    """
    net = default_network()
    path = path or os.environ.get("SHUTTLE_CONFIG")
    if not path:
        return net
    data = json.loads(Path(path).read_text(encoding="utf-8"))

    stops = dict(net.stops)
    for sid, patch in (data.get("stops") or {}).items():
        if sid not in stops:
            raise ValueError(f"unknown stop {sid!r} in {path}")
        allowed = {k: v for k, v in patch.items() if k in ("name", "short")}
        if "pos" in patch:
            allowed["pos"] = tuple(patch["pos"])
        stops[sid] = replace(stops[sid], **allowed)

    branches = dict(net.branches)
    for bid, patch in (data.get("branches") or {}).items():
        if bid not in branches:
            raise ValueError(f"unknown branch {bid!r} in {path}")
        allowed = {k: v for k, v in patch.items() if k in ("name", "level")}
        if "destinations" in patch:
            allowed["destinations"] = tuple(patch["destinations"])
        branches[bid] = replace(branches[bid], **allowed)

    minutes = dict(net.minutes)
    minutes.update(_symmetric(tuple(row) for row in data.get("minutes") or ()))

    return Network(
        stops=stops,
        branches=branches,
        minutes=minutes,
        capacity=int(data.get("capacity", net.capacity)),
        dwell=float(data.get("dwell", net.dwell)),
        service_start=parse_clock(data["service_start"]) if "service_start" in data else net.service_start,
        service_end=parse_clock(data["service_end"]) if "service_end" in data else net.service_end,
    )


def parse_clock(text: str) -> float:
    """``"18:05"`` -> minutes after midnight."""
    hours, minutes = text.split(":")
    return int(hours) * 60 + float(minutes)


def format_clock(minutes: float) -> str:
    """Minutes after midnight -> ``"18:05"`` (rounded to the nearest minute)."""
    total = int(round(minutes))
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"
