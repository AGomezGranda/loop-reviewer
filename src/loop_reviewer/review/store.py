"""Define the persistence contracts for review runs, drafts, and completions."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from loop_reviewer.review.model import (
    CompletedReview,
    DraftTarget,
    PublicationReceipt,
    ReportStatus,
    ReviewerFailureKind,
    ReviewIdentity,
    ReviewKey,
    ReviewRun,
    ReviewSelection,
    RunStatus,
)
from loop_reviewer.review.publication_model import (
    PublicationId,
    PublicationPreview,
    PublicationRecord,
    PublicationStatus,
)


class CompletionReader(Protocol):
    def completed(self, identity: ReviewIdentity) -> CompletedReview | None: ...


@dataclass(frozen=True)
class UnfinishedReview:
    id: int
    selection: ReviewSelection
    status: RunStatus
    failure: str | None
    failure_kind: ReviewerFailureKind | None
    publication_id: PublicationId | None
    previous_draft: bool = False
    report_status: ReportStatus | None = None


class UnfinishedReviewReader(Protocol):
    def unfinished(self) -> tuple[UnfinishedReview, ...]: ...

    def unfinished_by_id(self, run_id: int) -> UnfinishedReview | None: ...

    def completed_run_id(self, identity: ReviewIdentity) -> int | None: ...


class PublicationStore(CompletionReader, Protocol):
    def get_draft(self, run_id: int) -> ReviewRun | None: ...

    def save_preview(self, preview: PublicationPreview) -> None:
        """Save for an open draft; reject an unresolved send for its identity."""
        ...

    def get_publication(self, id: PublicationId) -> PublicationRecord | None: ...

    def reserve_send(
        self, id: PublicationId, expected: PublicationStatus
    ) -> PublicationRecord:
        """Atomically check the draft and expected status, exclude competing sends, and persist uncertainty before sending.

        An unresolved send is exclusive per review identity across runs and revisions.
        Reserving that same send again preserves evidence of prior ambiguity.
        """
        ...

    def record_rejection(self, id: PublicationId) -> PublicationRecord:
        """Release the reservation only if no previous send is still ambiguous."""
        ...

    def record_published(
        self, id: PublicationId, receipt: PublicationReceipt
    ) -> CompletedReview:
        """Atomically save the receipt and sole completion; replaying the same receipt is idempotent."""
        ...


class ReviewStoreError(Exception):
    pass


class UnconfirmedPublication(ReviewStoreError):
    def __init__(self, preview: PublicationPreview) -> None:
        self.publication_id = preview.id
        self.ref = preview.selection.ref
        super().__init__(f"Publication is unconfirmed for {self.ref.url}.")


class DraftConflict(ReviewStoreError):
    def __init__(self) -> None:
        super().__init__("The draft changed.")


type ReviewActivity = Literal["completed"] | RunStatus


class ReviewActivityReader(Protocol):
    def activity(
        self, identities: tuple[ReviewIdentity, ...]
    ) -> Mapping[ReviewIdentity, ReviewActivity]: ...


class DraftStore(CompletionReader, Protocol):
    def get_draft(self, run_id: int) -> ReviewRun | None: ...

    def save_edit(self, target: DraftTarget, run: ReviewRun) -> None:
        """Save only for an open draft with no unresolved send for its identity."""
        ...

    def complete(self, review: CompletedReview) -> CompletedReview:
        """Atomically record the targeted draft or return its existing receipt.

        Reject a new completion while any send for the identity is unresolved.
        """
        ...


class ReviewRunStore(CompletionReader, Protocol):
    def latest(self, key: ReviewKey) -> ReviewRun | None: ...

    def latest_draft(self, key: ReviewKey) -> ReviewRun | None:
        """Find the newest saved draft even if a newer attempt has no draft."""
        ...

    def start(self, selection: ReviewSelection, *, replaces: int | None) -> ReviewRun:
        """Admit the run only while the initial review is open."""
        ...

    def save(self, run: ReviewRun) -> None:
        """Resolve the same current running attempt without replacing its selection."""
        ...


class CompletionHistoryReader(Protocol):
    def completions(
        self, before_run_id: int | None, limit: int
    ) -> tuple[CompletedReview, ...]: ...

    def completion(self, run_id: int) -> CompletedReview | None: ...


class PublicationHistoryReader(Protocol):
    def get_publication(self, id: PublicationId) -> PublicationRecord | None: ...

    def unconfirmed_publications(self) -> tuple[PublicationRecord, ...]: ...

    def unconfirmed_for(self, run_id: int) -> PublicationRecord | None: ...
