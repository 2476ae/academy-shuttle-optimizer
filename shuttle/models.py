"""Data shared by the planner, the dispatcher and the web service.

All times are minutes after midnight (floats).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union

WAITING = "waiting"
ONBOARD = "onboard"
DONE = "done"
CANCELLED = "cancelled"
NO_SHOW = "no_show"  # the shuttle came but the students did not get on


@dataclass
class Request:
    """A group of students from one branch going to one destination."""

    id: str
    branch: str
    pickup: str
    dest: str
    count: int
    created_at: float
    ready_at: float
    status: str = WAITING
    picked_at: float | None = None
    dropped_at: float | None = None
    note: str = ""  # optional, e.g. students' names for the driver

    @property
    def wait(self) -> float | None:
        return None if self.picked_at is None else self.picked_at - self.ready_at

    @property
    def total(self) -> float | None:
        return None if self.dropped_at is None else self.dropped_at - self.ready_at


@dataclass(frozen=True)
class Step:
    """One stop visit: arrive, (wait,) board or alight, leave."""

    stop: str
    arrive: float
    depart: float
    board: tuple[str, ...] = ()
    alight: tuple[str, ...] = ()
    board_at: float | None = None  # when boarding starts, if it waits for students


@dataclass(frozen=True)
class Plan:
    steps: tuple[Step, ...] = ()
    cost: float = 0.0

    def pickup_eta(self, rid: str) -> float | None:
        for s in self.steps:
            if rid in s.board:
                return s.board_at if s.board_at is not None else s.arrive
        return None

    def drop_eta(self, rid: str) -> float | None:
        for s in self.steps:
            if rid in s.alight:
                return s.arrive
        return None


@dataclass(frozen=True)
class Board:
    """Board these groups at the current stop at time ``at``.

    If ``at`` is in the future the vehicle waits there and the policy is asked
    again at ``at`` (or sooner, if something changes). ``groups`` may be empty
    to mean "wait here until ``at``".
    """

    groups: tuple[str, ...]
    at: float


@dataclass(frozen=True)
class Go:
    stop: str


@dataclass(frozen=True)
class Idle:
    pass


Decision = Union[Board, Go, Idle]


@dataclass
class View:
    """What a policy may look at when it decides the vehicle's next move."""

    now: float
    stop: str  # where the vehicle is (or will be, when it is still driving there)
    free_at: float  # when the vehicle can act at ``stop``
    onboard: list[Request]
    waiting: list[Request]
    visit_boarded: bool = False  # already boarded someone during this stop visit
    arriving: bool = False  # still driving to ``stop``; arrives at ``free_at``
    memory: dict = field(default_factory=dict)  # kept by the dispatcher for the policy
    load: int = field(init=False)

    def __post_init__(self) -> None:
        self.load = sum(r.count for r in self.onboard)
