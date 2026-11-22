"""Coordinate inbox discovery across local processes with OS locks."""

import json
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import monotonic as clock
from uuid import uuid4

from loop_reviewer.inbox.discovery import InboxNotConfigured, ScanSuperseded
from loop_reviewer.inbox.model import InboxReport
from loop_reviewer.inbox.polling import ScanCycle, ScanOutcome, next_deadline
from loop_reviewer.inbox.store import InboxStoreError
from loop_reviewer.integrations.local_state.posix_lock import (
    LocalLockError,
    private_lock,
)
from loop_reviewer.review.store import ReviewStoreError


@dataclass(frozen=True)
class _Schedule:
    cycle: ScanCycle
    due: float


class InboxWatch:
    def __init__(
        self,
        directory: Path,
        scan: Callable[[], InboxReport],
        *,
        monotonic: Callable[[], float] = clock,
    ) -> None:
        self._directory = directory
        self._path = directory / "schedule.json"
        self._scan = scan
        self._now = monotonic

    @contextmanager
    def join(self) -> Generator[None]:
        with ExitStack() as membership:
            with self._lock("membership.guard", blocking=True):
                if not self._has_members():
                    self._write(
                        _Schedule(ScanCycle(str(uuid4()), 0, "complete"), self._now())
                    )
                membership.enter_context(
                    self._lock("membership.lock", shared=True, blocking=True)
                )
            try:
                yield
            finally:
                with self._lock("membership.guard", blocking=True):
                    membership.close()

    def cycle(self) -> ScanCycle | None:
        with self._lock("membership.guard", blocking=True):
            return self._read().cycle if self._has_members() else None

    def scan_now(self) -> InboxReport:
        with self._lock("scan.lock", blocking=True):
            return self._run_scan(self._active_schedule())

    def tick(self) -> None:
        schedule = self._active_schedule()
        if schedule is None or self._now() < schedule.due:
            return
        with self._lock("scan.lock") as acquired:
            if acquired:
                current = self._active_schedule()
                if (
                    current is not None
                    and current.cycle.cohort == schedule.cycle.cohort
                    and self._now() >= current.due
                ):
                    try:
                        self._run_scan(current)
                    except (
                        InboxNotConfigured,
                        ScanSuperseded,
                        InboxStoreError,
                        ReviewStoreError,
                    ):
                        pass  # Watchers observe these outcomes through the shared cycle.

    def _run_scan(self, schedule: _Schedule | None) -> InboxReport:
        outcome: ScanOutcome | None = None
        try:
            report = self._scan()
        except InboxNotConfigured:
            outcome = "unconfigured"
            raise
        except ScanSuperseded:
            outcome = "superseded"
            raise
        except InboxStoreError, ReviewStoreError:
            outcome = "error"
            raise
        else:
            outcome = "complete"
            return report
        finally:
            if schedule is not None and outcome is not None:
                self._complete(schedule, outcome)

    def _complete(self, schedule: _Schedule, outcome: ScanOutcome) -> None:
        with self._lock("membership.guard", blocking=True):
            current = self._read()
            if current.cycle.cohort == schedule.cycle.cohort:
                completed = self._now()
                due = current.due
                if completed >= due:
                    due = next_deadline(due, completed)
                cycle = ScanCycle(
                    current.cycle.cohort, current.cycle.number + 1, outcome
                )
                self._write(_Schedule(cycle, due))

    def _active_schedule(self) -> _Schedule | None:
        with self._lock("membership.guard", blocking=True):
            return self._read() if self._has_members() else None

    def _has_members(self) -> bool:
        with self._lock("membership.lock") as exclusive:
            return not exclusive

    @contextmanager
    def _lock(
        self, name: str, *, shared: bool = False, blocking: bool = False
    ) -> Generator[bool]:
        try:
            with private_lock(
                self._directory, name, shared=shared, blocking=blocking
            ) as fd:
                yield fd is not None
        except LocalLockError as error:
            raise InboxStoreError(
                "Could not lock local inbox watcher state."
            ) from error

    def _read(self) -> _Schedule:
        try:
            cohort, number, outcome, due = json.loads(self._path.read_text())
            if (
                not isinstance(cohort, str)
                or type(number) is not int
                or outcome not in ("complete", "unconfigured", "superseded", "error")
                or type(due) not in (int, float)
                or not isfinite(due)
            ):
                raise ValueError
            return _Schedule(ScanCycle(cohort, number, outcome), due)
        except OSError, ValueError, TypeError:
            raise InboxStoreError("Could not read local inbox watcher state.") from None

    def _write(self, schedule: _Schedule) -> None:
        cycle = schedule.cycle
        try:
            with NamedTemporaryFile(
                dir=self._directory, delete_on_close=False
            ) as stream:
                stream.write(
                    json.dumps(
                        [cycle.cohort, cycle.number, cycle.outcome, schedule.due]
                    ).encode()
                )
                stream.flush()
                Path(stream.name).replace(self._path)
        except OSError:
            raise InboxStoreError("Could not save local inbox watcher state.") from None
