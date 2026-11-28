"""Project unfinished attempts and the status of their comparison locks."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from loop_reviewer.review.model import ReviewIdentity, ReviewKey, can_rerun_analysis
from loop_reviewer.review.store import UnfinishedReview, UnfinishedReviewReader

type ActiveStatus = Literal[
    "running",
    "draft_ready",
    "failed",
    "interrupted",
    "recovery_required",
    "status_unavailable",
]

RetryAction = Literal["retry", "rerun"]


@dataclass(frozen=True)
class ActiveReview:
    run: UnfinishedReview
    status: ActiveStatus

    @property
    def retry_action(self) -> RetryAction | None:
        if self.run.previous_draft:
            return None
        if self.status in ("failed", "interrupted"):
            return "retry"
        if self.status == "draft_ready" and can_rerun_analysis(self.run.report_status):
            return "rerun"
        return None


class ActiveReviews:
    def __init__(
        self,
        store: UnfinishedReviewReader,
        lock_held: Callable[[ReviewKey], bool | None],
    ) -> None:
        self._store = store
        self._lock_held = lock_held

    def list(self) -> tuple[ActiveReview, ...]:
        return tuple(
            item
            for run in self._store.unfinished()
            if (item := self._project(run)) is not None
        )

    def by_id(self, run_id: int) -> ActiveReview | None:
        run = self._store.unfinished_by_id(run_id)
        return None if run is None else self._project(run)

    def completed_run_id(self, identity: ReviewIdentity) -> int | None:
        return self._store.completed_run_id(identity)

    def _project(self, run: UnfinishedReview) -> ActiveReview | None:
        if run.status == "running" and run.publication_id is None:
            held = self._lock_held(run.selection.key)
            if held is None:
                return ActiveReview(run, "status_unavailable")
            if held:
                return ActiveReview(run, "running")
            # A writer may have finished or replaced the run since the list snapshot.
            current = self._store.unfinished_by_id(run.id)
            if current is None:
                return None
            run = current
        if run.publication_id is not None:
            return ActiveReview(run, "recovery_required")
        if run.status in ("running", "abandoned"):
            return ActiveReview(run, "interrupted")
        return ActiveReview(run, run.status)
