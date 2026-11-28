"""Adapt pinned reviewer reports to hosted draft generation."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from unicodedata import category

from loop_reviewer.review.generate import ReviewerError
from loop_reviewer.review.model import (
    DraftValidationError,
    Finding,
    FindingLocation,
    GeneratedReview,
    ReviewDraft,
    ReviewSelection,
)
from loop_reviewer.reviewer.discussion_contract import DiscussionSnapshotV1
from loop_reviewer.reviewer.full_review_v6 import FullReviewRequestV6
from loop_reviewer.reviewer.hosted_report_contract import HostedReport
from loop_reviewer.reviewer.inventory_contract import (
    InventoryBudget,
    PinnedComparison,
    canonical_path,
)
from loop_reviewer.reviewer.local_check_contract import SourceLocation
from loop_reviewer.reviewer.model_contract import ReviewModel
from loop_reviewer.reviewer.pinned_source import PinnedSource
from loop_reviewer.reviewer.semantic_review import review_full_v6


class HostedReportReviewer:
    def __init__(
        self,
        open_source: Callable[
            [ReviewSelection, PinnedComparison], AbstractContextManager[PinnedSource]
        ],
        capture_discussion: Callable[[ReviewSelection], DiscussionSnapshotV1],
        model_settings: Callable[
            [], tuple[InventoryBudget, ReviewModel | None, str | None]
        ],
        monotonic: Callable[[], float],
    ) -> None:
        self._open_source = open_source
        self._capture_discussion = capture_discussion
        self._model_settings = model_settings
        self._monotonic = monotonic

    def review(self, selection: ReviewSelection) -> GeneratedReview:
        budget, model, model_id = self._model_settings()
        revision = selection.revision
        request = FullReviewRequestV6(
            schema_version=6,
            scope="full_review",
            comparison=PinnedComparison(
                merge_base=revision.merge_base, head=revision.head
            ),
            budget=budget,
            discussion=self._capture_discussion(selection),
        )
        report = review_full_v6(
            request,
            lambda pins: self._open_source(selection, pins),
            model,
            self._monotonic,
        )
        draft = project_report(report)
        usage = report.root.usage
        return GeneratedReview(
            draft,
            model_id if usage.model_calls else None,
            usage.estimated_cost_usd_micros / 1_000_000 if usage.model_calls else None,
            report,
        )


def project_report(report: HostedReport) -> ReviewDraft:
    original = report.root
    if original.status == "failed":
        raise ReviewerError(
            f"The reviewer could not analyze the pinned comparison. {original.error.message}",
            "failed",
        )
    try:
        return ReviewDraft(
            original.recommendation,
            "",
            tuple(
                Finding(
                    item.severity,
                    item.trigger,
                    item.consequence,
                    _right_location(item.anchor),
                    item.id,
                )
                for item in original.findings
            ),
            summary_origin="generated",
        )
    except DraftValidationError:
        raise ReviewerError(
            "The reviewer returned invalid draft feedback.", "failed"
        ) from None


def _right_location(anchor: SourceLocation | None) -> FindingLocation | None:
    if anchor is None or anchor.side != "head":
        return None
    try:
        path = canonical_path(anchor.path).decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not path.strip() or any(category(char) == "Cc" for char in path):
        return None
    return FindingLocation(path, anchor.start_line)
