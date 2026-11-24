"""V4 review contracts expose provenance and links, never raw discussion."""

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, RootModel, model_validator

from loop_reviewer.reviewer.discussion_contract import (
    ITEM_LIMIT,
    OMISSION_LIMIT,
    CaptureOmission,
    DiscussionId,
    DiscussionSnapshotV1,
    DiscussionStatus,
    DiscussionTarget,
    Permalink,
    capture_blocks_thread_link,
    valid_permalink,
    validate_capture,
)
from loop_reviewer.reviewer.inventory_contract import (
    InventoryBudget,
    InventoryError,
    PinnedComparison,
    _Empty,
    _Record,
)
from loop_reviewer.reviewer.local_check_contract import SourceEvidence
from loop_reviewer.reviewer.semantic_contract import (
    BehaviorInvestigation,
    InvalidReport,
    ReviewedPayload,
    SemanticUsage,
    _Id,
    _Ids,
)

_V4 = Annotated[int, Field(ge=4, le=4)]
_Policy = Annotated[int, Field(ge=2, le=2)]
DiscussionKind = Literal["thread", "review", "conversation"]


class DiscussionReviewRequest(_Record):
    schema_version: _V4
    scope: Literal["full_review"]
    comparison: PinnedComparison
    budget: InventoryBudget
    discussion: DiscussionSnapshotV1

    @model_validator(mode="after")
    def matching_pins(self) -> Self:
        if self.comparison != self.discussion.target.comparison:
            raise ValueError("Discussion comparison must match the review pins")
        return self


class MappingOmission(_Record):
    scope: Literal["mapping"]
    code: Literal["limit_reached"]


class DiscussionSummary(_Record):
    target: DiscussionTarget
    captured_at: datetime
    capture_omissions: Annotated[
        tuple[CaptureOmission, ...], Field(max_length=OMISSION_LIMIT)
    ]
    mapping_omissions: Annotated[tuple[MappingOmission, ...], Field(max_length=1)]


class DiscussionReference(_Record):
    kind: DiscussionKind
    item_id: DiscussionId
    investigation_id: _Id
    is_resolved: bool | None
    is_outdated: bool | None
    permalink: Permalink | None

    @model_validator(mode="after")
    def thread_metadata(self) -> Self:
        if self.kind == "thread":
            if self.is_resolved is None or self.is_outdated is None:
                raise ValueError("Thread references require status flags")
        elif any(
            item is not None
            for item in (self.is_resolved, self.is_outdated, self.permalink)
        ):
            raise ValueError("Thread metadata cannot describe an unanchored item")
        return self


class LinkedExistingThread(_Record):
    thread_id: DiscussionId
    investigation_id: _Id
    candidate_id: _Id
    evidence_ids: _Ids
    reason: Literal["existing_thread"]


class _DiscussionCommon(_Record):
    schema_version: _V4
    policy_version: _Policy
    scope: Literal["full_review"]
    comparison: PinnedComparison
    discussion: DiscussionSummary
    discussion_status: DiscussionStatus
    usage: SemanticUsage

    @model_validator(mode="after")
    def valid_provenance(self) -> Self:
        if self.discussion.target.comparison != self.comparison:
            raise ValueError("Discussion provenance must match the review pins")
        validate_capture(
            self.discussion.captured_at,
            self.discussion_status,
            self.discussion.capture_omissions,
        )
        return self


class DiscussionReviewedPayload(ReviewedPayload):
    discussion_references: Annotated[
        tuple[DiscussionReference, ...], Field(max_length=ITEM_LIMIT)
    ]
    linked_existing_threads: Annotated[
        tuple[LinkedExistingThread, ...], Field(max_length=4)
    ]

    def validate_discussion_references(self, summary: DiscussionSummary | None) -> None:
        if summary is None and (
            self.discussion_references or self.linked_existing_threads
        ):
            raise ValueError("Discussion references require captured discussion")
        work = {
            item.id: item
            for item in self.investigation_coverage
            if isinstance(item, BehaviorInvestigation)
        }
        refs = {item.item_id: item for item in self.discussion_references}
        if len(refs) != len(self.discussion_references):
            raise ValueError("Discussion must have unique available references")
        for ref in refs.values():
            if ref.investigation_id not in work or (
                ref.permalink is not None
                and (
                    summary is None
                    or not valid_permalink(summary.target, ref.permalink)
                )
            ):
                raise ValueError("Invalid discussion reference")
        dispositions = {item.id for item in (*self.findings, *self.private_leads)} | {
            item.id
            for investigation in self.investigation_coverage
            if isinstance(investigation, BehaviorInvestigation)
            for item in investigation.discarded_candidates
        }
        evidence = {
            entry.id: entry
            for entry in self.evidence_index
            if isinstance(entry, SourceEvidence)
        }
        threads: set[str] = set()
        for link in self.linked_existing_threads:
            ref = refs.get(link.thread_id)
            if (
                ref is None
                or ref.kind != "thread"
                or ref.is_resolved is not False
                or ref.is_outdated is not False
                or ref.permalink is None
                or ref.investigation_id != link.investigation_id
                or link.thread_id in threads
                or link.candidate_id in dispositions
                or not link.candidate_id.startswith("c")
                or summary is None
                or capture_blocks_thread_link(summary.capture_omissions, link.thread_id)
            ):
                raise ValueError("Invalid thread disposition")
            ids = set(link.evidence_ids)
            if (
                len(ids) != len(link.evidence_ids)
                or not ids <= evidence.keys()
                or not ids <= set(work[link.investigation_id].evidence_ids)
                or {evidence[key].range.side for key in ids} != {"base", "head"}
            ):
                raise ValueError("A thread link must retain both pinned source sides")
            threads.add(link.thread_id)
            dispositions.add(link.candidate_id)


class _DiscussionReviewed(DiscussionReviewedPayload, _DiscussionCommon):
    @model_validator(mode="after")
    def closed_discussion_references(self) -> Self:
        if self.discussion_status == "unavailable" and self.discussion_references:
            raise ValueError("Discussion must have unique available references")
        self.validate_discussion_references(self.discussion)
        return self

    def has_discussion_gap(self) -> bool:
        return self.discussion_status != "complete" or bool(
            self.discussion.mapping_omissions
        )


class CompleteDiscussionReview(_DiscussionReviewed):
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


class PartialDiscussionReview(_DiscussionReviewed):
    status: Literal["partial"]
    stop_reason: Literal["finished", "work_limit", "budget_exhausted"]

    @model_validator(mode="after")
    def has_gaps(self) -> Self:
        paths = any(row.outcome == "unfinished" for row in self.file_coverage)
        if not paths and not self.has_discussion_gap():
            raise ValueError("Partial review requires unfinished work")
        if not paths:
            expected = "work_limit" if self.discussion.mapping_omissions else "finished"
            if self.stop_reason != expected:
                raise ValueError("Discussion-only gap does not explain stop reason")
        elif (
            self.stop_reason != "finished"
            and not (
                self.stop_reason == "work_limit" and self.discussion.mapping_omissions
            )
            and not any(
                row.reason == self.stop_reason
                for row in (*self.file_coverage, *self.investigation_coverage)
            )
        ):
            raise ValueError("Stop reason must explain unfinished work")
        return self


class FailedDiscussionReview(_DiscussionCommon):
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
        if self.discussion.mapping_omissions:
            raise ValueError("Failed intake cannot contain mapping results")
        return self


class DiscussionReviewResult(
    RootModel[
        Annotated[
            CompleteDiscussionReview | PartialDiscussionReview | FailedDiscussionReview,
            Field(discriminator="status"),
        ]
    ]
):
    model_config = ConfigDict(strict=True, frozen=True, revalidate_instances="always")


@dataclass(frozen=True)
class DiscussionDetails:
    references: tuple[DiscussionReference, ...] = ()
    links: tuple[LinkedExistingThread, ...] = ()
    omissions: tuple[MappingOmission, ...] = ()


def render_v4(
    payload: dict[str, object],
    snapshot: DiscussionSnapshotV1,
    details: DiscussionDetails,
) -> DiscussionReviewResult:
    if payload["status"] != "failed":
        if snapshot.status != "complete" or details.omissions:
            payload = payload | {"status": "partial"}
        if details.omissions and payload["stop_reason"] == "finished":
            payload = payload | {"stop_reason": "work_limit"}
    return DiscussionReviewResult.model_validate(
        payload
        | {
            "schema_version": 4,
            "policy_version": 2,
            "discussion_status": snapshot.status,
            "discussion": DiscussionSummary(
                target=snapshot.target,
                captured_at=snapshot.captured_at,
                capture_omissions=snapshot.omissions,
                mapping_omissions=details.omissions,
            ),
            "discussion_references": details.references,
            "linked_existing_threads": details.links,
        }
    )
