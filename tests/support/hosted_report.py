"""Build hosted report fixtures from the offline pinned reviewer."""

from loop_reviewer.reviewer.discussion_contract import DiscussionSnapshotV1
from loop_reviewer.reviewer.full_review_contract import (
    FullReviewRequest,
    FullReviewResult,
)
from loop_reviewer.reviewer.full_review_v6 import (
    FullReviewRequestV6,
    FullReviewResultV6,
)
from loop_reviewer.reviewer.inventory_contract import InventoryBudget
from loop_reviewer.reviewer.semantic_review import review_full, review_full_v6
from tests.test_local_checks import PINS, Analyzer, Source, row


def offline_report(
    *paths: str, discussion: DiscussionSnapshotV1 | None = None
) -> FullReviewResult:
    return review_full(
        FullReviewRequest(
            schema_version=5,
            scope="full_review",
            comparison=PINS,
            budget=InventoryBudget(max_model_spend_usd_micros=0),
            discussion=discussion,
        ),
        Source(tuple(row(path) for path in paths)).open,
        Analyzer(),
        None,
        lambda: 0,
    )


def offline_pinned_report(*paths: str) -> FullReviewResultV6:
    return review_full_v6(
        FullReviewRequestV6(
            schema_version=6,
            scope="full_review",
            comparison=PINS,
            budget=InventoryBudget(max_model_spend_usd_micros=0),
        ),
        Source(tuple(row(path) for path in paths)).open,
        None,
        lambda: 0,
    )
