"""Run the standalone inventory reviewer against two pinned local commits."""

import argparse
import os
from pathlib import Path

from loop_reviewer.reviewer.integrations.git_inventory import open_local_comparison
from loop_reviewer.reviewer.inventory import review_inventory
from loop_reviewer.reviewer.inventory_contract import (
    InventoryBudget,
    PinnedComparison,
    ReviewerRequest,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inventory all Git changes between two pinned local commits.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--repo", required=True, type=Path, help="Local Git repository directory"
    )
    parser.add_argument("--merge-base", required=True, help="Full merge-base commit ID")
    parser.add_argument("--head", required=True, help="Full head commit ID")
    args = parser.parse_args()
    try:
        comparison = PinnedComparison(
            merge_base=args.merge_base.lower(), head=args.head.lower()
        )
    except ValueError:
        parser.error(
            "--merge-base and --head must be full hexadecimal commit IDs of the same format (SHA-1 or SHA-256)."
        )
    request = ReviewerRequest(
        schema_version=1,
        scope="inventory",
        comparison=comparison,
        budget=InventoryBudget(max_model_spend_usd_micros=0),
        discussion=None,
    )
    environment = {"PATH": os.environ.get("PATH", os.defpath)}
    report = review_inventory(
        request, lambda pins: open_local_comparison(args.repo, pins, environment)
    )
    print(report.model_dump_json())
    return 0 if report.root.status == "complete" else 1
