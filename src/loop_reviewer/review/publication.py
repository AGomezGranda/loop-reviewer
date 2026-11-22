"""Preview, submit and recover exact, host-bound GitHub reviews."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Literal, Protocol

from loop_reviewer.review.drafts import DraftNotFound
from loop_reviewer.review.model import (
    CompletedReview,
    DraftTarget,
    FindingLocation,
    PublicationAction,
    PublicationReceipt,
    ReviewIdentity,
    ReviewSelection,
)
from loop_reviewer.review.publication_model import (
    PublicationError,
    PublicationId,
    PublicationPreview,
    PublicationRecord,
    build_preview,
    require_action,
)
from loop_reviewer.review.pull_request import (
    PullRequest,
    PullRequestNotReviewable,
    PullRequestRef,
    ReviewedRevision,
)
from loop_reviewer.review.selection import GitHubReadError
from loop_reviewer.review.store import DraftConflict, PublicationStore, ReviewStoreError


class PublicationReader(Protocol):
    @property
    def login(self) -> str: ...

    @property
    def actor_id(self) -> int: ...

    def read_pull_request(self, ref: PullRequestRef) -> PullRequest: ...

    def read_revision(self, pull_request: PullRequest) -> ReviewedRevision: ...


class PreviewSession(PublicationReader, Protocol):
    def usable_anchors(
        self, ref: PullRequestRef, requested: frozenset[FindingLocation]
    ) -> frozenset[FindingLocation]: ...


class RejectionReason(StrEnum):
    AUTHENTICATION = "authentication"
    ACCESS = "access"
    VALIDATION = "validation"
    RATE_LIMIT = "rate_limit"


class PublicationSession(PublicationReader, Protocol):
    def submit(
        self, preview: PublicationPreview
    ) -> PublicationReceipt | RejectionReason | Literal["unknown"]: ...

    def reconcile(
        self, preview: PublicationPreview, *, ref: PullRequestRef
    ) -> PublicationReceipt | Literal["no_match", "inconclusive"]: ...


type PublicationSessionFactory[S: PublicationReader] = Callable[
    [str], AbstractContextManager[S]
]


class PublicationGuard(Protocol):
    def acquire(
        self, identity: ReviewIdentity
    ) -> AbstractContextManager[
        PublicationSessionFactory[PublicationSession] | None
    ]: ...


class PreparePublication:
    def __init__(
        self,
        store: PublicationStore,
        session_factory: PublicationSessionFactory[PreviewSession],
        new_id: Callable[[], PublicationId],
    ) -> None:
        self._store = store
        self._session_factory = session_factory
        self._new_id = new_id

    def preview(
        self,
        target: DraftTarget,
        action: PublicationAction,
        *,
        summary_only: bool = False,
    ) -> PublicationPreview:
        run = self._store.get_draft(target.run_id)
        if run is None:
            raise DraftNotFound(f"No saved draft for run {target.run_id}.")
        if run.draft_target != target:
            raise DraftConflict()
        saved = run.saved_draft()
        original = saved.selection
        if original.ref.repository_id is None:
            raise PublicationError(
                "This draft has no stable GitHub repository ID. Finish locally or open a new review."
            )
        with self._session_factory(original.ref.host) as session:
            selection = _inspect_fresh(session, original, action)
            anchors: frozenset[FindingLocation] = frozenset()
            requested = frozenset(
                finding.location
                for finding in saved.draft.findings
                if finding.location is not None
            )
            if requested and not summary_only:
                try:
                    anchors = session.usable_anchors(selection.ref, requested)
                except GitHubReadError:
                    pass
                current = _inspect_fresh(session, selection, action)
                if not _same_slug(selection.ref, current.ref):
                    raise PublicationError(
                        "The repository destination changed during preflight. Preview again."
                    )
                selection = current
            preview = build_preview(
                self._new_id(),
                saved,
                replace(selection, revision=original.revision),
                session.actor_id,
                action,
                anchors,
            )
        self._store.save_preview(preview)
        return preview


type PublicationBlockedReason = Literal[
    "confirmation_required",
    "recovery_required",
    "evidence_inconclusive",
    "duplicate_risk_consent_required",
]


class PublicationNotFound(PublicationError):
    pass


class PublicationBlocked(PublicationError):
    def __init__(
        self,
        reason: PublicationBlockedReason,
        preview: PublicationPreview,
        message: str,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.publication_id = preview.id
        self.ref = preview.selection.ref


class PublicationInterrupted(KeyboardInterrupt):
    def __init__(self, preview: PublicationPreview) -> None:
        super().__init__("Publication was interrupted.")
        self.publication_id = preview.id
        self.ref = preview.selection.ref


@dataclass(frozen=True)
class RejectedPublication:
    record: PublicationRecord
    reason: RejectionReason


type PublicationOutcome = CompletedReview | PublicationRecord | RejectedPublication


class PublishReview:
    def __init__(self, store: PublicationStore, guard: PublicationGuard) -> None:
        self._store = store
        self._guard = guard

    def publish(self, id: PublicationId) -> PublicationOutcome:
        return self._operate(id, "confirm")

    def recover(self, id: PublicationId) -> PublicationOutcome:
        return self._operate(id, "recover")

    def retry(
        self, id: PublicationId, *, accept_duplicate_risk: bool = False
    ) -> PublicationOutcome:
        return self._operate(id, "retry", accept_duplicate_risk=accept_duplicate_risk)

    def _operate(
        self,
        id: PublicationId,
        operation: Literal["confirm", "recover", "retry"],
        *,
        accept_duplicate_risk: bool = False,
    ) -> PublicationOutcome:
        record = self._get(id)
        if record.status == "published":
            return self._published_completion(record)
        with self._guard.acquire(record.preview.selection.identity) as factory:
            record = self._get(id)
            if record.status == "published":
                return self._published_completion(record)
            if operation == "recover" and record.status == "rejected":
                return record
            preview = record.preview
            if factory is None:
                raise PublicationBlocked(
                    "recovery_required", preview, "Publication is already in progress."
                )
            if record.status == "preview" and operation != "confirm":
                raise PublicationBlocked(
                    "confirmation_required", preview, "This preview was not sent."
                )
            if operation == "confirm" and record.status != "preview":
                if record.status == "unconfirmed":
                    raise PublicationBlocked(
                        "recovery_required",
                        preview,
                        "The previous send is unconfirmed.",
                    )
                return record
            try:
                with factory(preview.selection.ref.host) as session:
                    if record.status == "unconfirmed":
                        pull = _inspect_identity(
                            session, preview.selection, actor_id=preview.actor_id
                        )
                        evidence = session.reconcile(preview, ref=pull.ref)
                        if isinstance(evidence, PublicationReceipt):
                            return self._finalize(record, evidence)
                        if evidence == "inconclusive":
                            raise PublicationBlocked(
                                "evidence_inconclusive",
                                preview,
                                "GitHub review evidence was incomplete or conflicting.",
                            )
                        if operation != "retry" or not accept_duplicate_risk:
                            raise PublicationBlocked(
                                "duplicate_risk_consent_required",
                                preview,
                                "No marker was found. The previous send may still have succeeded.",
                            )
                    return self._send(session, record)
            except KeyboardInterrupt:
                raise PublicationInterrupted(preview) from None
            except PublicationBlocked:
                raise
            except (
                GitHubReadError,
                PublicationError,
                PullRequestNotReviewable,
                ReviewStoreError,
            ) as error:
                if record.status == "unconfirmed":
                    raise PublicationBlocked(
                        "recovery_required", preview, str(error)
                    ) from None
                raise

    def _send(
        self, session: PublicationSession, record: PublicationRecord
    ) -> PublicationOutcome:
        preview = record.preview
        current = _inspect_fresh(
            session, preview.selection, preview.action, actor_id=preview.actor_id
        )
        if not _same_slug(preview.selection.ref, current.ref):
            raise PublicationError(
                f"The repository destination for {preview.selection.ref.url} changed. Make a fresh preview for {current.ref.url}."
            )
        reserved = self._store.reserve_send(preview.id, record.status)
        try:
            outcome = session.submit(preview)
        except GitHubReadError as error:
            raise PublicationBlocked("recovery_required", preview, str(error)) from None
        if isinstance(outcome, PublicationReceipt):
            return self._finalize(reserved, outcome)
        if isinstance(outcome, RejectionReason):
            try:
                rejected = self._store.record_rejection(preview.id)
            except ReviewStoreError, PublicationError:
                raise PublicationBlocked(
                    "recovery_required",
                    preview,
                    "Could not save the result of the send.",
                ) from None
            if rejected.status == "rejected":
                return RejectedPublication(rejected, outcome)
            return rejected
        return reserved

    def _finalize(
        self, record: PublicationRecord, receipt: PublicationReceipt
    ) -> CompletedReview:
        try:
            return self._store.record_published(record.preview.id, receipt)
        except ReviewStoreError, PublicationError:
            raise PublicationBlocked(
                "recovery_required",
                record.preview,
                "Could not save the GitHub receipt.",
            ) from None

    def _published_completion(self, record: PublicationRecord) -> CompletedReview:
        preview = record.preview
        try:
            completed = self._store.completed(preview.selection.identity)
        except ReviewStoreError:
            completed = None
        if record.matches_completion(completed):
            return completed
        receipt = record.receipt
        assert receipt is not None
        raise PublicationError(
            f"GitHub receipt saved, but local history could not be read. Review "
            f"{receipt.review_id} is at {preview.selection.ref.url}#pullrequestreview-{receipt.review_id}. "
            "Check the local review database."
        )

    def _get(self, id: PublicationId) -> PublicationRecord:
        record = self._store.get_publication(id)
        if record is None:
            raise PublicationNotFound(f"Unknown publication ID {id.value}.")
        return record


def _inspect_identity(
    session: PublicationReader,
    selection: ReviewSelection,
    *,
    actor_id: int | None = None,
) -> PullRequest:
    ref = selection.ref
    if session.login.lower() != selection.identity.account or (
        actor_id is not None and actor_id != session.actor_id
    ):
        raise PublicationError(
            f"Wrong GitHub account for {ref.url}. Sign in as @{selection.account} on {ref.host}."
        )
    pull = session.read_pull_request(ref)
    if ReviewIdentity.for_pull_request(pull.ref, session.login) != selection.identity:
        raise PublicationError(
            f"GitHub returned a different pull request identity for {ref.url}."
        )
    return pull


def _inspect_fresh(
    session: PublicationReader,
    selection: ReviewSelection,
    action: PublicationAction,
    *,
    actor_id: int | None = None,
) -> ReviewSelection:
    pull = _inspect_identity(session, selection, actor_id=actor_id)
    pull.require_open_for_review()
    require_action(action, session.login, pull.author)
    revision = session.read_revision(pull)
    current = ReviewSelection(pull.ref, session.login, pull.author, revision)
    changes = current.changes_since(selection)
    if changes:
        raise PublicationError(
            f"The reviewed comparison changed ({', '.join(changes)}). No review was sent by this command. Run loop-reviewer review {selection.ref.url}."
        )
    return current


def _same_slug(first: PullRequestRef, second: PullRequestRef) -> bool:
    return (first.owner.lower(), first.repo.lower()) == (
        second.owner.lower(),
        second.repo.lower(),
    )
