"""Standalone, model-optional review of two pinned local commits."""

import argparse
import os
import sys
from pathlib import Path
from time import monotonic

from loop_reviewer.reviewer.discussion_contract import (
    SNAPSHOT_BYTES,
    DiscussionSnapshotV1,
)
from loop_reviewer.reviewer.discussion_report import DiscussionReviewRequest
from loop_reviewer.reviewer.full_review_contract import FullReviewRequest
from loop_reviewer.reviewer.full_review_v6 import FullReviewRequestV6
from loop_reviewer.reviewer.integrations.git_inventory import open_local_comparison
from loop_reviewer.reviewer.integrations.pi_review import PiReview, PiSetupError
from loop_reviewer.reviewer.integrations.python_analyzer import CpythonAnalyzer
from loop_reviewer.reviewer.inventory_contract import (
    MAX_RUNTIME_SPEND,
    InventoryBudget,
    PinnedComparison,
)
from loop_reviewer.reviewer.semantic_contract import SemanticReviewRequest
from loop_reviewer.reviewer.semantic_review import (
    review_full,
    review_full_v6,
    review_semantic,
    review_semantic_with_discussion,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Review a pinned local Git comparison.", allow_abbrev=False
    )
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--merge-base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--max-model-spend-usd-micros", type=int, default=0)
    parser.add_argument("--report-version", type=int, choices=(3, 4, 5, 6), default=5)
    parser.add_argument(
        "--discussion-snapshot",
        type=Path,
        help="Read a trusted, private discussion snapshot without GitHub I/O.",
    )
    parser.add_argument(
        "--allow-provider",
        action="store_true",
        help="Authorize sending selected pinned source to the configured Pi model.",
    )
    args = parser.parse_args()
    try:
        comparison = PinnedComparison(
            merge_base=args.merge_base.lower(), head=args.head.lower()
        )
        budget = InventoryBudget(
            max_model_spend_usd_micros=args.max_model_spend_usd_micros
        )
    except ValueError:
        parser.error(
            f"Use full commit IDs of the same hash format and a spend limit between 0 and {MAX_RUNTIME_SPEND} USD micros."
        )
    if budget.max_model_spend_usd_micros and not args.allow_provider:
        parser.error("Positive model spend requires --allow-provider.")
    if args.report_version == 3 and args.discussion_snapshot is not None:
        parser.error("Report version 3 does not accept a discussion snapshot.")
    if args.report_version == 4 and args.discussion_snapshot is None:
        parser.error("Report version 4 requires --discussion-snapshot.")
    snapshot = None
    if args.discussion_snapshot is not None:
        try:
            with args.discussion_snapshot.open("rb") as stream:
                data = stream.read(SNAPSHOT_BYTES + 1)
            if len(data) > SNAPSHOT_BYTES:
                raise ValueError
            snapshot = DiscussionSnapshotV1.model_validate_json(data)
            if snapshot.target.comparison != comparison:
                raise ValueError
        except OSError, ValueError, RecursionError:
            parser.error(
                "Use a readable discussion snapshot within 512 KiB matching the pinned commits."
            )
    environment = {"PATH": os.environ.get("PATH", os.defpath)}
    model = None
    if (
        args.allow_provider
        and budget.max_model_spend_usd_micros
        and args.report_version in (5, 6)
    ):
        try:
            model = PiReview.require_configured(os.environ)
        except PiSetupError as error:
            print(f"AI analysis unavailable: {error}", file=sys.stderr)

    def open_source(pins: PinnedComparison):
        return open_local_comparison(args.repo, pins, environment)

    if args.report_version == 6:
        report = review_full_v6(
            FullReviewRequestV6(
                schema_version=6,
                scope="full_review",
                comparison=comparison,
                budget=budget,
                discussion=snapshot,
            ),
            open_source,
            model,
            monotonic,
        )
    elif args.report_version == 5:
        report = review_full(
            FullReviewRequest(
                schema_version=5,
                scope="full_review",
                comparison=comparison,
                budget=budget,
                discussion=snapshot,
            ),
            open_source,
            CpythonAnalyzer(),
            model,
            monotonic,
        )
    elif snapshot is not None:
        report = review_semantic_with_discussion(
            DiscussionReviewRequest(
                schema_version=4,
                scope="full_review",
                comparison=comparison,
                budget=budget,
                discussion=snapshot,
            ),
            open_source,
            CpythonAnalyzer(),
            model,
            monotonic,
        )
    else:
        report = review_semantic(
            SemanticReviewRequest(
                schema_version=3,
                scope="full_review",
                comparison=comparison,
                budget=budget,
                discussion=None,
            ),
            open_source,
            CpythonAnalyzer(),
            model,
            monotonic,
        )
    print(report.model_dump_json())
    return {"complete": 0, "failed": 1, "partial": 3}[report.root.status]
