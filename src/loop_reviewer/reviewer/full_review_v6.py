"""Strict pinned-text review contracts, with no compiler disposition."""

from typing import Annotated, Self, override

from pydantic import ConfigDict, Field, RootModel, model_validator

from loop_reviewer.reviewer.discussion_contract import DiscussionSnapshotV1
from loop_reviewer.reviewer.discussion_report import (
    DiscussionDetails,
    LinkedExistingThread,
)
from loop_reviewer.reviewer.full_review_contract import (
    _Complete,
    _Failed,
    _FullReviewRequest,
    _Partial,
    _Reviewed,
    discussion_payload,
    text_eligible,
)
from loop_reviewer.reviewer.inventory_contract import MAX_CHANGES
from loop_reviewer.reviewer.local_check_contract import (
    MAX_EVIDENCE,
    Evidence,
    Investigation,
    SourceEvidence,
)
from loop_reviewer.reviewer.semantic_contract import (
    BehaviorInvestigation,
    SemanticCoverage,
)

_V6 = Annotated[int, Field(ge=6, le=6)]
_Policy4 = Annotated[int, Field(ge=4, le=4)]


class FullReviewRequestV6(_FullReviewRequest):
    schema_version: _V6


class _ReviewedV6(_Reviewed):
    investigation_coverage: Annotated[
        tuple[BehaviorInvestigation, ...], Field(max_length=MAX_CHANGES)
    ]
    evidence_index: Annotated[
        tuple[SourceEvidence, ...], Field(max_length=MAX_EVIDENCE)
    ]
    linked_existing_threads: Annotated[
        tuple[LinkedExistingThread, ...], Field(max_length=0)
    ]

    @model_validator(mode="after")
    def source_findings_only(self) -> Self:
        if any(
            item.decision_reason != "supported_by_source"
            or (item.anchor is not None and item.anchor.side != "head")
            for item in self.findings
        ):
            raise ValueError(
                "Pinned-text findings require source evidence and head-only anchors"
            )
        return self

    @staticmethod
    @override
    def _file_outcome(
        row: SemanticCoverage,
        work: tuple[Investigation | BehaviorInvestigation, ...],
        evidence: dict[str, Evidence],
    ) -> None:
        if text_eligible(row):
            side = row.new or row.old
            assert side is not None
            if (
                len(work) != 1
                or not isinstance(work[0], BehaviorInvestigation)
                or work[0].path != side.path
            ):
                raise ValueError(
                    "Regular text requires exactly one behavior investigation on its path"
                )
            behavior = work[0]
            expected = (
                behavior.outcome,
                behavior.reason,
                "semantic" if behavior.outcome == "complete" else "inventory_only",
            )
            if row.oversized and behavior.outcome == "complete":
                raise ValueError(
                    "Oversized changes cannot have complete behavior coverage"
                )
        else:
            if work:
                raise ValueError("Ineligible files cannot have investigations")
            expected = (
                ("excluded", "binary", "inventory_only")
                if row.binary
                else ("unfinished", "unsupported_language", "inventory_only")
            )
        if (row.outcome, row.reason, row.highest_completed_check) != expected:
            raise ValueError("File outcome must reflect its investigations")


class CompleteFullReviewV6(_ReviewedV6, _Complete):
    schema_version: _V6
    policy_version: _Policy4


class PartialFullReviewV6(_ReviewedV6, _Partial):
    schema_version: _V6
    policy_version: _Policy4


class FailedFullReviewV6(_Failed):
    schema_version: _V6
    policy_version: _Policy4


class FullReviewResultV6(
    RootModel[
        Annotated[
            CompleteFullReviewV6 | PartialFullReviewV6 | FailedFullReviewV6,
            Field(discriminator="status"),
        ]
    ]
):
    model_config = ConfigDict(strict=True, frozen=True, revalidate_instances="always")


def render_v6(
    payload: dict[str, object],
    snapshot: DiscussionSnapshotV1 | None,
    details: DiscussionDetails,
) -> FullReviewResultV6:
    return FullReviewResultV6.model_validate(
        discussion_payload(payload, snapshot, details)
        | {"schema_version": 6, "policy_version": 4}
    )
