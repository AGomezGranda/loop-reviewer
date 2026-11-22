"""Read, edit, and finish saved drafts through the review store."""

from collections.abc import Callable

from loop_reviewer.review.model import (
    CompletedReview,
    DraftTarget,
    DraftVersionMismatch,
    ReviewAlreadyCompleted,
    ReviewDraft,
    ReviewRun,
    require_run_id,
)
from loop_reviewer.review.store import (
    DraftConflict,
    DraftStore,
)


class DraftNotFound(Exception):
    pass


class EditReviewDraft:
    def __init__(self, store: DraftStore) -> None:
        self._store = store

    def show(self, run_id: int) -> ReviewRun:
        run = self._load(run_id)
        completed = self._store.completed(run.selection.identity)
        if completed is not None:
            raise ReviewAlreadyCompleted(completed)
        return run

    def finish_locally(self, target: DraftTarget) -> CompletedReview:
        run = self._load(target.run_id)
        completed = self._store.completed(run.selection.identity)
        if completed is not None:
            return completed.receipt_for(target)
        _require_target(run, target)
        return self._store.complete(run.complete_locally())

    def _load(self, run_id: int) -> ReviewRun:
        require_run_id(run_id)
        run = self._store.get_draft(run_id)
        if run is None or run.draft is None:
            raise DraftNotFound(f"No saved draft for run {run_id}.")
        return run

    def edit_summary(self, target: DraftTarget, text: str) -> ReviewRun:
        return self._edit(target, lambda draft: draft.replace_summary(text))

    def edit_finding(
        self,
        target: DraftTarget,
        number: int,
        *,
        condition: str | None,
        consequence: str | None,
    ) -> ReviewRun:
        return self._edit(
            target,
            lambda draft: draft.edit_finding(
                number, condition=condition, consequence=consequence
            ),
        )

    def drop_finding(self, target: DraftTarget, number: int) -> ReviewRun:
        return self._edit(target, lambda draft: draft.drop_finding(number))

    def _edit(
        self, target: DraftTarget, mutate: Callable[[ReviewDraft], ReviewDraft]
    ) -> ReviewRun:
        run = self.show(target.run_id)
        _require_target(run, target)
        assert run.draft is not None
        updated = run.edit_draft(mutate(run.draft))
        self._store.save_edit(target, updated)
        return updated


def _require_target(run: ReviewRun, target: DraftTarget) -> None:
    try:
        run.require_draft_target(target)
    except DraftVersionMismatch as error:
        raise DraftConflict() from error
