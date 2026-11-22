"""Poll on monotonic launch-relative ticks without overlapping scans."""

from dataclasses import dataclass
from math import ceil
from typing import Literal

from loop_reviewer.inbox.model import InboxReport, InboxRow
from loop_reviewer.review.model import ReviewIdentity, RunStatus

POLL_INTERVAL_SECONDS = 120


type ScanOutcome = Literal["complete", "unconfigured", "superseded", "error"]


@dataclass(frozen=True)
class ScanCycle:
    cohort: str
    number: int
    outcome: ScanOutcome


@dataclass(frozen=True)
class WatchCycle:
    report: InboxReport
    changed: tuple[InboxRow, ...]


type _VisibleFields = tuple[str, str, str, tuple[str, ...], RunStatus | None]


def _visible_fields(row: InboxRow) -> _VisibleFields:
    pr = row.pull_request
    return pr.title, pr.author, pr.ref.url, row.reasons, row.last_saved


def next_deadline(deadline: float, completed_at: float) -> float:
    deadline += POLL_INTERVAL_SECONDS
    late = completed_at - deadline
    if late > 0:
        deadline += ceil(late / POLL_INTERVAL_SECONDS) * POLL_INTERVAL_SECONDS
    return deadline


class InboxChanges:
    def __init__(self) -> None:
        self._previous: dict[ReviewIdentity, _VisibleFields] = {}

    def observe(self, report: InboxReport) -> WatchCycle:
        current = {row.identity: _visible_fields(row) for row in report.active}
        changed = tuple(
            row
            for row in report.active
            if not row.stale
            and current[row.identity] != self._previous.get(row.identity)
        )
        self._previous = current
        return WatchCycle(report, changed)
