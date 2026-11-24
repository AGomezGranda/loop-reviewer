"""Run model-free checks against two pinned local commits."""

import argparse
import os
from pathlib import Path
from time import monotonic

from loop_reviewer.reviewer.integrations.git_inventory import open_local_comparison
from loop_reviewer.reviewer.integrations.python_analyzer import CpythonAnalyzer
from loop_reviewer.reviewer.inventory_contract import PinnedComparison
from loop_reviewer.reviewer.local_check_contract import (
    LocalCheckBudget,
    LocalCheckRequest,
)
from loop_reviewer.reviewer.local_checks import review_local_checks


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check Python changes between two pinned local commits.",
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
    request = LocalCheckRequest(
        schema_version=2,
        scope="local_checks",
        comparison=comparison,
        budget=LocalCheckBudget(max_model_spend_usd_micros=0),
        discussion=None,
    )
    environment = {"PATH": os.environ.get("PATH", os.defpath)}
    report = review_local_checks(
        request,
        lambda pins: open_local_comparison(args.repo, pins, environment),
        CpythonAnalyzer(),
        monotonic,
    )
    print(report.model_dump_json())
    return {"complete": 0, "failed": 1, "partial": 3}[report.root.status]
