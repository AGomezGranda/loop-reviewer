"""Define review drafts, run transitions, and completion rules."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal, Self, TypeIs
from unicodedata import category

from loop_reviewer.feedback_text import validate_feedback_text
from loop_reviewer.review.pull_request import (
    PullRequestRef,
    ReviewComparison,
    ReviewedRevision,
)

if TYPE_CHECKING:
    from loop_reviewer.reviewer.full_review_contract import FullReviewResult

Severity = Literal["high", "medium", "low"]
Verdict = Literal["approved", "comment"]
Recommendation = Literal["approved", "comment", "none"]
SummaryOrigin = Literal["legacy", "generated", "edited"]
PublicationAction = Literal["comment", "approve"]


@dataclass(frozen=True)
class PublicationReceipt:
    review_id: int
    action: PublicationAction

    def __post_init__(self) -> None:
        if type(self.review_id) is not int or self.review_id < 1:
            raise ValueError("Expected a positive GitHub review ID.")
        if self.action not in ("comment", "approve"):
            raise ValueError("Expected comment or approve.")


ReviewerFailureKind = Literal["setup", "input", "failed"]
RetryableRunStatus = Literal["failed", "abandoned"]
RunStatus = Literal["running", "draft_ready"] | RetryableRunStatus
SelectionChange = Literal[
    "target branch", "merge base", "head", "repository", "authenticated account"
]


AttemptDisposition = (
    Literal["start", "replace", "resumed", "no_retryable_run"] | RetryableRunStatus
)


def is_retryable_status(status: object) -> TypeIs[RetryableRunStatus]:
    return isinstance(status, str) and status in {"failed", "abandoned"}


MAX_STORED_INTEGER = 2**63 - 1
MAX_EDIT_BYTES = 64 * 1024

_SEVERITY_ORDER: dict[Severity, int] = {"high": 0, "medium": 1, "low": 2}


class DraftValidationError(Exception):
    pass


class InvalidRunId(DraftValidationError):
    pass


def require_run_id(run_id: int) -> None:
    if type(run_id) is not int or run_id < 1:
        raise InvalidRunId("Run ID must be a positive integer.")


@dataclass(frozen=True)
class DraftTarget:
    run_id: int
    version: int

    def __post_init__(self) -> None:
        require_run_id(self.run_id)
        if type(self.version) is not int or self.version < 1:
            raise DraftValidationError("Draft version must be a positive integer.")


class RunTransitionError(Exception):
    pass


class DraftVersionMismatch(RunTransitionError):
    pass


class ReportAssociationError(RunTransitionError):
    pass


class ReviewAlreadyCompleted(Exception):
    def __init__(self, completion: CompletedReview) -> None:
        self.completion = completion
        super().__init__("The initial review is already completed.")


@dataclass(frozen=True)
class ReviewIdentity:
    host: str
    repository: int | tuple[str, str]
    number: int
    account: str

    @classmethod
    def for_pull_request(cls, ref: PullRequestRef, account: str) -> Self:
        _validate_account(account)
        repository = ref.repository_id
        return cls(
            ref.host,
            repository
            if repository is not None
            else (ref.owner.lower(), ref.repo.lower()),
            ref.number,
            account.lower(),
        )


def _validate_account(account: str) -> None:
    if not account or any(
        char.isspace() or category(char) in {"Cc", "Cs"} for char in account
    ):
        raise ValueError("Expected a GitHub account name")


@dataclass(frozen=True)
class ReviewKey:
    identity: ReviewIdentity
    comparison: ReviewComparison


@dataclass(frozen=True)
class FindingLocation:
    path: str
    right_line: int

    def __post_init__(self) -> None:
        _validate_feedback(self.path)
        if type(self.right_line) is not int or self.right_line < 1:
            raise DraftValidationError(
                "A finding location must have a positive line number."
            )


@dataclass(frozen=True)
class Finding:
    severity: Severity
    condition: str
    consequence: str
    location: FindingLocation | None
    report_id: str | None = None

    def __post_init__(self) -> None:
        if self.severity not in _SEVERITY_ORDER:
            raise DraftValidationError("Invalid finding severity.")
        _validate_feedback(self.condition)
        _validate_feedback(self.consequence)
        if self.report_id is not None:
            _validate_feedback(self.report_id)


@dataclass(frozen=True)
class ReviewDraft:
    verdict: Recommendation
    summary: str
    findings: tuple[Finding, ...]
    summary_origin: SummaryOrigin = "legacy"

    def __post_init__(self) -> None:
        if self.verdict not in {"approved", "comment", "none"} or (
            self.verdict == "none" and self.summary_origin == "legacy"
        ):
            raise DraftValidationError("Invalid review verdict.")
        if self.summary_origin not in {"legacy", "generated", "edited"}:
            raise DraftValidationError("Invalid summary origin.")
        if self.summary_origin == "generated":
            if self.summary:
                raise DraftValidationError("A generated summary must be empty.")
        else:
            _validate_feedback(self.summary)

    def ordered_findings(self) -> tuple[Finding, ...]:
        return tuple(self.findings[index] for index in self._ordered_indices())

    def replace_summary(self, text: str) -> Self:
        return replace(self, summary=text, summary_origin="edited")

    def edit_finding(
        self, number: int, *, condition: str | None, consequence: str | None
    ) -> Self:
        index = self._finding_index(number)
        finding = self.findings[index]
        if condition is None and consequence is None:
            raise DraftValidationError("Provide at least one finding field to edit.")
        finding = replace(
            finding,
            condition=finding.condition if condition is None else condition,
            consequence=finding.consequence if consequence is None else consequence,
        )
        return replace(
            self,
            findings=(*self.findings[:index], finding, *self.findings[index + 1 :]),
        )

    def drop_finding(self, number: int) -> Self:
        index = self._finding_index(number)
        return replace(
            self, findings=self.findings[:index] + self.findings[index + 1 :]
        )

    def require_edit_of(self, original: ReviewDraft) -> None:
        if self.verdict != original.verdict:
            raise DraftValidationError("Edits must preserve the review verdict.")
        if self.summary_origin not in (original.summary_origin, "edited"):
            raise DraftValidationError("Edits cannot reset the summary origin.")
        remaining = iter(original.findings)
        for retained in self.findings:
            if not any(
                (candidate.severity, candidate.location, candidate.report_id)
                == (retained.severity, retained.location, retained.report_id)
                for candidate in remaining
            ):
                raise DraftValidationError(
                    "Edits must preserve the identity, severity, location and order of retained findings."
                )

    def _ordered_indices(self) -> list[int]:
        return sorted(
            range(len(self.findings)),
            key=lambda index: _SEVERITY_ORDER[self.findings[index].severity],
        )

    def _finding_index(self, number: int) -> int:
        if not 1 <= number <= len(self.findings):
            raise DraftValidationError("Finding number is not in the draft preview.")
        return self._ordered_indices()[number - 1]


def _validate_feedback(text: str) -> None:
    try:
        validate_feedback_text(text)
    except ValueError as error:
        raise DraftValidationError(str(error)) from error
    if len(text.encode("utf-8")) > MAX_EDIT_BYTES:
        raise DraftValidationError("Feedback must be at most 64 KiB per field.")


@dataclass(frozen=True)
class ReviewSelection:
    ref: PullRequestRef
    account: str
    author: str
    revision: ReviewedRevision

    def __post_init__(self) -> None:
        _validate_account(self.account)

    @property
    def identity(self) -> ReviewIdentity:
        return ReviewIdentity.for_pull_request(self.ref, self.account)

    @property
    def key(self) -> ReviewKey:
        return ReviewKey(self.identity, self.revision.comparison)

    def changes_since(self, original: ReviewSelection) -> tuple[SelectionChange, ...]:
        current, previous = self.revision.comparison, original.revision.comparison
        changes: list[SelectionChange] = []
        if current.target_branch != previous.target_branch:
            changes.append("target branch")
        if current.merge_base != previous.merge_base:
            changes.append("merge base")
        if current.head != previous.head:
            changes.append("head")
        if self.identity.repository != original.identity.repository:
            changes.append("repository")
        if self.identity.account != original.identity.account:
            changes.append("authenticated account")
        return tuple(changes)


def require_report_association(
    report: FullReviewResult | None, selection: ReviewSelection, draft: ReviewDraft
) -> None:
    if report is None:
        if (
            draft.summary_origin == "generated"
            or draft.verdict == "none"
            or any(finding.report_id is not None for finding in draft.findings)
        ):
            raise ReportAssociationError(
                "Report-derived feedback requires its original report."
            )
        return
    original = report.root
    revision = selection.revision
    if (
        original.status == "failed"
        or (original.comparison.merge_base, original.comparison.head)
        != (revision.merge_base, revision.head)
        or original.recommendation != draft.verdict
        or draft.summary_origin == "legacy"
    ):
        raise ReportAssociationError(
            "The reviewer report does not match the draft and pinned comparison."
        )
    if original.discussion is not None:
        target, ref = original.discussion.target, selection.ref
        if (target.host, target.repository_id, target.number, target.target_branch) != (
            ref.host,
            ref.repository_id,
            ref.number,
            revision.target_branch,
        ):
            raise ReportAssociationError(
                "The reviewer report names a different discussion target."
            )
    remaining = iter(original.findings)
    for retained in draft.findings:
        if not any(
            (item.id, item.severity) == (retained.report_id, retained.severity)
            for item in remaining
        ):
            raise ReportAssociationError(
                "The draft findings do not match the original report."
            )


@dataclass(frozen=True)
class GeneratedReview:
    draft: ReviewDraft
    model: str | None
    estimated_cost_usd: float | None
    report: FullReviewResult | None = None


@dataclass(frozen=True)
class SavedDraft:
    id: int
    draft_version: int
    selection: ReviewSelection
    draft: ReviewDraft
    generation_model: str | None = None
    estimated_cost_usd: float | None = None
    report: FullReviewResult | None = None


@dataclass(frozen=True)
class CompletedReview:
    source: DraftTarget
    selection: ReviewSelection
    final_draft: ReviewDraft
    receipt: PublicationReceipt | None = None
    generation_model: str | None = None
    estimated_cost_usd: float | None = None
    report: FullReviewResult | None = None

    @property
    def publication_status(self) -> Literal["not_published", "published"]:
        return "not_published" if self.receipt is None else "published"

    def receipt_for(self, target: DraftTarget) -> Self:
        if self.source != target:
            raise ReviewAlreadyCompleted(self)
        return self


@dataclass(frozen=True)
class ReviewRun:
    id: int
    selection: ReviewSelection
    status: RunStatus
    draft: ReviewDraft | None
    failure: str | None
    draft_version: int = 1
    failure_kind: ReviewerFailureKind | None = None
    generation_model: str | None = None
    estimated_cost_usd: float | None = None
    report: FullReviewResult | None = None

    def __post_init__(self) -> None:
        if (self.status == "draft_ready") != (self.draft is not None):
            raise RunTransitionError("The review attempt has an inconsistent draft.")
        if self.draft is None and (
            self.generation_model is not None
            or self.estimated_cost_usd is not None
            or self.report is not None
        ):
            raise RunTransitionError(
                "The review attempt has inconsistent generation metadata."
            )
        if (self.status == "failed") != (self.failure is not None) or (
            self.status != "failed" and self.failure_kind is not None
        ):
            raise RunTransitionError("The review attempt has an inconsistent failure.")
        if self.draft_version < 1 or (
            self.status != "draft_ready" and self.draft_version != 1
        ):
            raise RunTransitionError(
                "The review attempt has an inconsistent draft version."
            )

        if self.draft is not None:
            require_report_association(self.report, self.selection, self.draft)

    @property
    def draft_target(self) -> DraftTarget:
        return DraftTarget(self.id, self.draft_version)

    def require_draft_target(self, target: DraftTarget) -> None:
        if self.draft_target != target:
            raise DraftVersionMismatch("The draft changed.")

    def edit_draft(self, draft: ReviewDraft) -> Self:
        if self.status != "draft_ready" or self.draft is None:
            raise RunTransitionError("Only a saved draft can be edited.")
        draft.require_edit_of(self.draft)
        return replace(self, draft=draft, draft_version=self.draft_version + 1)

    def saved_draft(self) -> SavedDraft:
        if self.draft is None:
            raise RunTransitionError("There is no saved draft for this attempt.")
        return SavedDraft(
            self.id,
            self.draft_version,
            self.selection,
            self.draft,
            self.generation_model,
            self.estimated_cost_usd,
            self.report,
        )

    def complete_locally(self) -> CompletedReview:
        if self.status != "draft_ready" or self.draft is None:
            raise RunTransitionError("Only a saved draft can be finished locally.")
        return CompletedReview(
            self.draft_target,
            self.selection,
            self.draft,
            generation_model=self.generation_model,
            estimated_cost_usd=self.estimated_cost_usd,
            report=self.report,
        )

    def finish(self, generated: GeneratedReview) -> Self:
        self._require_running()
        return replace(
            self,
            status="draft_ready",
            draft=generated.draft,
            failure=None,
            generation_model=generated.model,
            estimated_cost_usd=generated.estimated_cost_usd,
            report=generated.report,
        )

    def fail(self, message: str, kind: ReviewerFailureKind) -> Self:
        self._require_running()
        return replace(
            self, status="failed", draft=None, failure=message, failure_kind=kind
        )

    def abandon(self) -> Self:
        self._require_running()
        return replace(self, status="abandoned", draft=None, failure=None)

    def _require_running(self) -> None:
        if self.status != "running":
            raise RunTransitionError(
                f"Cannot change a review attempt in state {self.status}."
            )


def prepare_unlocked_attempt(
    current: ReviewRun | None, *, retry: bool
) -> tuple[AttemptDisposition, ReviewRun | None]:
    if current is None:
        return ("no_retryable_run" if retry else "start"), None
    if current.status == "draft_ready":
        return "resumed", None
    if current.status == "running":
        return ("replace" if retry else "abandoned"), current.abandon()
    return ("replace" if retry else current.status), None
