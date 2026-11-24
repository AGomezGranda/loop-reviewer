"""Version 5 contracts for language-independent pinned review."""

from typing import Annotated, Literal, Self, override

from pydantic import ConfigDict, Field, RootModel, model_validator

from loop_reviewer.reviewer.discussion_contract import (
    DiscussionSnapshotV1,
    DiscussionStatus,
    validate_capture,
)
from loop_reviewer.reviewer.discussion_report import (
    DiscussionDetails,
    DiscussionReviewedPayload,
    DiscussionSummary,
)
from loop_reviewer.reviewer.inventory_contract import (
    ChangedFile,
    InventoryBudget,
    InventoryError,
    PinnedComparison,
    _Empty,
    _Record,
)
from loop_reviewer.reviewer.local_check_contract import Evidence, Investigation
from loop_reviewer.reviewer.local_check_policy import supported_python
from loop_reviewer.reviewer.semantic_contract import (
    BehaviorInvestigation,
    InvalidReport,
    SemanticCoverage,
    SemanticUsage,
    validate_compiler_evidence,
)

_V5 = Annotated[int, Field(ge=5, le=5)]
_Policy = Annotated[int, Field(ge=3, le=3)]


def text_eligible(row: ChangedFile) -> bool:
    return not row.binary and all(
        side.mode in ("100644", "100755")
        for side in (row.old, row.new)
        if side is not None
    )


class FullReviewRequest(_Record):
    schema_version: _V5
    scope: Literal["full_review"]
    comparison: PinnedComparison
    budget: InventoryBudget
    discussion: DiscussionSnapshotV1 | None = None

    @model_validator(mode="after")
    def matching_pins(self) -> Self:
        if (
            self.discussion is not None
            and self.comparison != self.discussion.target.comparison
        ):
            raise ValueError("Discussion comparison must match the review pins")
        return self


class _Common(_Record):
    schema_version: _V5
    policy_version: _Policy
    scope: Literal["full_review"]
    comparison: PinnedComparison
    discussion: DiscussionSummary | None
    discussion_status: DiscussionStatus | Literal["not_requested"]
    usage: SemanticUsage

    @model_validator(mode="after")
    def valid_provenance(self) -> Self:
        if self.discussion_status == "not_requested":
            if self.discussion is not None:
                raise ValueError("Unrequested discussion cannot have provenance")
        else:
            if (
                self.discussion is None
                or self.discussion.target.comparison != self.comparison
            ):
                raise ValueError("Discussion provenance must match the review pins")
            validate_capture(
                self.discussion.captured_at,
                self.discussion_status,
                self.discussion.capture_omissions,
            )
        return self

    def has_discussion_gap(self) -> bool:
        return self.discussion is not None and (
            self.discussion_status != "complete"
            or bool(self.discussion.mapping_omissions)
        )


class _Reviewed(DiscussionReviewedPayload, _Common):
    @model_validator(mode="after")
    def closed_discussion_references(self) -> Self:
        if self.discussion_status == "unavailable" and self.discussion_references:
            raise ValueError("Unavailable discussion cannot have references")
        self.validate_discussion_references(self.discussion)
        return self

    @staticmethod
    @override
    def _file_outcome(
        row: SemanticCoverage,
        work: tuple[Investigation | BehaviorInvestigation, ...],
        evidence: dict[str, Evidence],
    ) -> None:
        if not text_eligible(row):
            expected = (
                ("excluded", "binary")
                if row.binary
                else ("unfinished", "unsupported_language")
            )
            if (
                work
                or (row.outcome, row.reason) != expected
                or row.highest_completed_check != "inventory_only"
            ):
                raise ValueError("File eligibility and coverage disagree")
            return
        requires_compiler = supported_python(row.old) or supported_python(row.new)
        if len(work) != (2 if requires_compiler else 1) or not isinstance(
            work[-1], BehaviorInvestigation
        ):
            raise ValueError("Regular text requires a behavior investigation")
        path = row.new or row.old
        assert path is not None
        if any(item.path != path.path for item in work):
            raise ValueError("Investigations must name the assigned file")
        behavior = work[-1]
        reason = behavior.reason
        level = "inventory_only"
        if requires_compiler:
            compiler = work[0]
            if not isinstance(compiler, Investigation):
                raise ValueError("Python requires a compiler investigation")
            validate_compiler_evidence(row, compiler, evidence)
            reason = compiler.reason or reason
            if compiler.outcome == "complete":
                level = "mechanical"
        if reason is None:
            level = "semantic"
        if (row.outcome, row.reason, row.highest_completed_check) != (
            "complete" if reason is None else "unfinished",
            reason,
            level,
        ) or (row.oversized and behavior.outcome == "complete"):
            raise ValueError("File outcome must reflect its investigations")


class CompleteFullReview(_Reviewed):
    status: Literal["complete"]
    stop_reason: Literal["finished"]

    @model_validator(mode="after")
    def entirely_complete(self) -> Self:
        if (
            self.has_discussion_gap()
            or self.usage.accounting_fault
            or any(row.outcome == "unfinished" for row in self.file_coverage)
        ):
            raise ValueError("Complete review has outstanding work or accounting fault")
        return self


class PartialFullReview(_Reviewed):
    status: Literal["partial"]
    stop_reason: Literal["finished", "work_limit", "budget_exhausted"]

    @model_validator(mode="after")
    def has_gaps(self) -> Self:
        paths = any(row.outcome == "unfinished" for row in self.file_coverage)
        mapping_gap = self.discussion is not None and bool(
            self.discussion.mapping_omissions
        )
        if not paths and not self.has_discussion_gap():
            raise ValueError("Partial review requires unfinished work")
        if not paths:
            if self.stop_reason != ("work_limit" if mapping_gap else "finished"):
                raise ValueError("Discussion-only gap does not explain stop reason")
        elif self.stop_reason != "finished" and not (
            (self.stop_reason == "work_limit" and mapping_gap)
            or any(
                row.reason == self.stop_reason
                for row in (*self.file_coverage, *self.investigation_coverage)
            )
        ):
            raise ValueError("Stop reason must explain unfinished work")
        return self


class FailedFullReview(_Common):
    status: Literal["failed"]
    stop_reason: Literal["fatal_error"]
    error: InventoryError | InvalidReport
    recommendation: Literal["none"]
    file_coverage: _Empty
    investigation_coverage: _Empty
    evidence_index: _Empty
    findings: _Empty
    private_leads: _Empty
    discussion_references: _Empty
    linked_existing_threads: _Empty

    @model_validator(mode="after")
    def no_mapping(self) -> Self:
        if self.discussion is not None and self.discussion.mapping_omissions:
            raise ValueError("Failed intake cannot contain mapping results")
        return self


class FullReviewResult(
    RootModel[
        Annotated[
            CompleteFullReview | PartialFullReview | FailedFullReview,
            Field(discriminator="status"),
        ]
    ]
):
    model_config = ConfigDict(strict=True, frozen=True, revalidate_instances="always")


def render_v5(
    payload: dict[str, object],
    snapshot: DiscussionSnapshotV1 | None,
    details: DiscussionDetails,
) -> FullReviewResult:
    if snapshot is not None and payload["status"] != "failed":
        if snapshot.status != "complete" or details.omissions:
            payload = payload | {"status": "partial"}
        if details.omissions and payload["stop_reason"] == "finished":
            payload = payload | {"stop_reason": "work_limit"}
    return FullReviewResult.model_validate(
        payload
        | {
            "schema_version": 5,
            "policy_version": 3,
            "discussion_status": snapshot.status
            if snapshot is not None
            else "not_requested",
            "discussion": DiscussionSummary(
                target=snapshot.target,
                captured_at=snapshot.captured_at,
                capture_omissions=snapshot.omissions,
                mapping_omissions=details.omissions,
            )
            if snapshot is not None
            else None,
            "discussion_references": details.references,
            "linked_existing_threads": details.links,
        }
    )
