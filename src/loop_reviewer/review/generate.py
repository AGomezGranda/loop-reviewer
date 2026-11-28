"""Start, resume, or retry a private review through injected collaborators."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Literal, Protocol

from loop_reviewer.review.model import (
    CompletedReview,
    GeneratedReview,
    ReportAssociationError,
    ReviewAlreadyCompleted,
    ReviewerFailureKind,
    ReviewIdentity,
    ReviewKey,
    ReviewRun,
    ReviewSelection,
    SavedDraft,
    SelectionChange,
    can_rerun_analysis,
    prepare_unlocked_attempt,
)
from loop_reviewer.review.pull_request import PullRequestNotReviewable, PullRequestRef
from loop_reviewer.review.selection import (
    GitHubReadError,
    PullRequestReader,
    open_pull_request,
    select_revision,
)
from loop_reviewer.review.store import (
    ReviewRunStore,
    ReviewStoreError,
)

DraftReviewStatus = Literal["generated", "resumed"]
UnavailableReviewStatus = Literal["in_progress", "abandoned", "no_retryable_run"]


class ReviewerError(Exception):
    def __init__(self, message: str, kind: ReviewerFailureKind) -> None:
        super().__init__(message)
        self.kind = kind


class ReviewIdentityMismatch(Exception):
    def __init__(self) -> None:
        super().__init__(
            "The GitHub account or repository no longer matches this inbox row. Sign back in or run inbox setup, then scan again."
        )


class ReviewRetryChanged(Exception):
    def __init__(self) -> None:
        super().__init__(
            "The saved attempt or its comparison changed. Check Reviews for the current status."
        )


@dataclass(frozen=True)
class RetryTarget:
    run_id: int
    key: ReviewKey


class SavedDraftInterrupted(KeyboardInterrupt):
    def __init__(self, run_id: int) -> None:
        super().__init__()
        self.run_id = run_id


class SavedDraftHistoryUnavailable(ReviewStoreError):
    def __init__(self, run_id: int, error: ReviewStoreError) -> None:
        super().__init__(str(error))
        self.run_id = run_id


class Reviewer(Protocol):
    def review(self, selection: ReviewSelection) -> GeneratedReview: ...


class ReviewExecution(Protocol):
    def acquire(self, key: ReviewKey) -> AbstractContextManager[Reviewer | None]: ...


@dataclass(frozen=True)
class DraftReview:
    status: DraftReviewStatus
    run: SavedDraft
    changes: tuple[SelectionChange, ...] | Literal["unavailable"] = ()


@dataclass(frozen=True)
class FinishedReview:
    completion: CompletedReview
    status: Literal["completed"] = "completed"


@dataclass(frozen=True)
class FailedReview:
    message: str
    kind: ReviewerFailureKind | None
    status: Literal["failed"] = "failed"


@dataclass(frozen=True)
class UnavailableReview:
    status: UnavailableReviewStatus


ReviewOutcome = DraftReview | FinishedReview | FailedReview | UnavailableReview


class ReviewPullRequest:
    def __init__(
        self,
        reader: PullRequestReader,
        store: ReviewRunStore,
        execution: ReviewExecution,
        *,
        select_current: Callable[[PullRequestRef], ReviewSelection] | None = None,
    ) -> None:
        self._reader = reader
        self._store = store
        self._execution = execution
        self._select_current = select_current

    def run(
        self,
        ref: PullRequestRef,
        *,
        retry: bool = False,
        expected_identity: ReviewIdentity | None = None,
        observe_selection: Callable[[ReviewSelection], None] | None = None,
        retry_target: RetryTarget | None = None,
    ) -> ReviewOutcome:
        account, pull_request = self._reader.read_pull_request(ref)
        identity = ReviewIdentity.for_pull_request(pull_request.ref, account)
        if retry_target is not None and retry_target.key.identity != identity:
            raise ReviewRetryChanged
        if expected_identity is not None and identity != expected_identity:
            raise ReviewIdentityMismatch
        if (history := self._history(identity)) is not None:
            return history
        selection = select_revision(account, pull_request, self._reader)
        key = selection.key
        if retry_target is not None and retry_target.key != key:
            raise ReviewRetryChanged
        if observe_selection is not None:
            observe_selection(selection)
        if retry_target is None:
            latest = self._store.latest(key) if retry else self._store.latest_draft(key)
            if (
                latest is not None
                and latest.status == "draft_ready"
                and not (retry and can_rerun_analysis(latest.report_status))
            ):
                return self._resume(latest)

        saved: SavedDraft | None = None
        try:
            with self._execution.acquire(key) as reviewer:
                if (history := self._history(identity)) is not None:
                    return history
                if reviewer is None:
                    return UnavailableReview("in_progress")
                attempt = self._prepare_attempt(
                    selection,
                    retry=retry or retry_target is not None,
                    target=retry_target,
                )
                if not isinstance(attempt, ReviewRun):
                    return attempt
                try:
                    finished = attempt.finish(reviewer.review(selection))
                except (ReviewerError, ReportAssociationError) as error:
                    kind = error.kind if isinstance(error, ReviewerError) else "failed"
                    self._store.save(attempt.fail(str(error), kind))
                    return self._history(identity) or FailedReview(str(error), kind)
                except KeyboardInterrupt:
                    self._abandon(key, attempt.id)
                    raise
                self._store.save(finished)
                saved = finished.saved_draft()

            if (history := self._history(identity)) is not None:
                return history
            changes = self._check_changes(ref, selection)
            return self._history(identity) or DraftReview("generated", saved, changes)
        except KeyboardInterrupt as error:
            if saved is not None:
                raise SavedDraftInterrupted(saved.id) from error
            raise
        except ReviewStoreError as error:
            if saved is not None:
                raise SavedDraftHistoryUnavailable(saved.id, error) from error
            raise

    def _prepare_attempt(
        self, selection: ReviewSelection, *, retry: bool, target: RetryTarget | None
    ) -> ReviewRun | ReviewOutcome:
        if not retry and (draft := self._store.latest_draft(selection.key)) is not None:
            return self._resume(draft)
        current = self._store.latest(selection.key)
        if target is not None and (
            current is None
            or current.id != target.run_id
            or (
                current.status == "draft_ready"
                and not can_rerun_analysis(current.report_status)
            )
        ):
            raise ReviewRetryChanged
        action, abandoned = prepare_unlocked_attempt(current, retry=retry)
        if abandoned is not None:
            self._store.save(abandoned)
            current = abandoned
        if action in {"resumed", "failed", "replace"}:
            assert current is not None
            if action == "resumed":
                return self._resume(current)
            if action == "failed":
                assert current.failure is not None
                return FailedReview(current.failure, current.failure_kind)
            replaces = current.id
        elif action == "start":
            replaces = None
        else:
            return UnavailableReview(action)
        try:
            return self._store.start(selection, replaces=replaces)
        except ReviewAlreadyCompleted as error:
            return FinishedReview(error.completion)

    def _resume(self, draft: ReviewRun) -> ReviewOutcome:
        return self._history(draft.selection.identity) or DraftReview(
            "resumed", draft.saved_draft()
        )

    def _history(self, identity: ReviewIdentity) -> FinishedReview | None:
        completed = self._store.completed(identity)
        return None if completed is None else FinishedReview(completed)

    def _abandon(self, key: ReviewKey, run_id: int) -> None:
        try:
            current = self._store.latest(key)
            if (
                current is not None
                and current.id == run_id
                and current.status == "running"
            ):
                self._store.save(current.abandon())
        except ReviewStoreError:
            pass

    def _check_changes(
        self, ref: PullRequestRef, original: ReviewSelection
    ) -> tuple[SelectionChange, ...] | Literal["unavailable"]:
        try:
            current = (
                open_pull_request(ref, self._reader)
                if self._select_current is None
                else self._select_current(ref)
            )
        except GitHubReadError, PullRequestNotReviewable:
            return "unavailable"
        return current.changes_since(original)
