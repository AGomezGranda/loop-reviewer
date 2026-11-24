import json
from collections.abc import Generator
from contextlib import contextmanager
from typing import cast

import pytest
from pydantic import ValidationError

from loop_reviewer.reviewer.inventory import InventorySourceError, review_inventory
from loop_reviewer.reviewer.inventory_contract import (
    ChangedFile,
    FileSide,
    PinnedComparison,
    ReviewerRequest,
    ReviewerResult,
)


def request_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "scope": "inventory",
        "comparison": {"merge_base": "a" * 40, "head": "b" * 40},
        "budget": {"max_model_spend_usd_micros": 0},
        "discussion": None,
    }


def result_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "policy_version": 1,
        "comparison": request_payload()["comparison"],
        "scope": "inventory",
        "recommendation": "none",
        "discussion_status": "not_requested",
        "usage": dict.fromkeys(
            (
                "model_calls",
                "input_tokens",
                "output_tokens",
                "estimated_cost_usd_micros",
                "unaccounted_reserved_usd_micros",
            ),
            0,
        ),
        "investigation_coverage": [],
        "evidence_index": [],
        "findings": [],
        "private_leads": [],
        "linked_existing_threads": [],
        "status": "complete",
        "stop_reason": "finished",
        "error": None,
        "file_coverage": [],
    }


@pytest.mark.parametrize(
    ("model", "overrides"),
    [
        pytest.param(ReviewerRequest, {"schema_version": 2}, id="unknown-version"),
        pytest.param(ReviewerRequest, {"schema_version": True}, id="boolean-version"),
        pytest.param(ReviewerRequest, {"schema_version": 1.0}, id="float-version"),
        pytest.param(ReviewerRequest, {"extra": 1}, id="unknown-field"),
        pytest.param(ReviewerRequest, {"scope": "full_review"}, id="wrong-scope"),
        pytest.param(
            ReviewerRequest,
            {"comparison": {"merge_base": "abcd", "head": "b" * 40}},
            id="short-id",
        ),
        pytest.param(
            ReviewerRequest,
            {"comparison": {"merge_base": "a" * 64, "head": "b" * 40}},
            id="mixed-width",
        ),
        pytest.param(
            ReviewerResult, {"recommendation": "comment"}, id="wrong-recommendation"
        ),
        pytest.param(
            ReviewerResult,
            {"file_coverage": [{"outcome": "complete"}]},
            id="malformed-coverage",
        ),
        pytest.param(
            ReviewerResult,
            {"status": "failed", "stop_reason": "fatal_error"},
            id="failure-without-error",
        ),
        pytest.param(
            ReviewerResult,
            {
                "status": "failed",
                "stop_reason": "fatal_error",
                "error": {"code": "work_limit", "message": "Too many files"},
            },
            id="mismatched-stop-reason",
        ),
    ],
)
def test_inventory_contract_rejects_invalid_payloads(
    model: type[ReviewerRequest | ReviewerResult], overrides: dict[str, object]
) -> None:
    payload = request_payload() if model is ReviewerRequest else result_payload()
    with pytest.raises(ValidationError):
        model.model_validate_json(json.dumps(payload | overrides))


def added(path: str) -> ChangedFile:
    return ChangedFile(
        change_type="added",
        old=None,
        new=FileSide(
            path=path,
            object_id="c" * 40,
            mode="100644",
            blob_bytes=1,
            generated="unknown",
        ),
        added_lines=1,
        deleted_lines=0,
        binary=False,
        oversized=False,
    )


class Source:
    def __init__(self, rows: tuple[ChangedFile, ...]) -> None:
        self.rows = rows

    def inventory(self) -> tuple[ChangedFile, ...]:
        return self.rows


def test_inventory_reports_all_rows_in_stable_order() -> None:
    request = ReviewerRequest.model_validate(request_payload())

    @contextmanager
    def source(comparison: PinnedComparison) -> Generator[Source]:
        assert comparison == request.comparison
        yield Source((added("%FF"), added("a/b"), added("%0A")))

    report = review_inventory(request, source).root
    assert report.status == "complete"
    assert [row.new.path for row in report.file_coverage if row.new] == [
        "%0A",
        "a/b",
        "%FF",
    ]
    assert all(
        row.highest_completed_check == "inventory_only" for row in report.file_coverage
    )
    assert report.recommendation == "none"
    assert report.usage.model_calls == 0


@pytest.mark.parametrize("case", ["empty", "malformed", "duplicate", "limit"])
def test_inventory_reports_empty_or_failed_comparisons(case: str) -> None:
    @contextmanager
    def source(_: PinnedComparison) -> Generator[Source]:
        match case:
            case "limit":
                raise InventorySourceError("work_limit", "Too many files")
            case "malformed":
                yield Source((cast(ChangedFile, {"change_type": "modified"}),))
            case "duplicate":
                yield Source((added("a"), added("a")))
            case _:
                yield Source(())

    report = review_inventory(
        ReviewerRequest.model_validate(request_payload()), source
    ).root
    assert report.file_coverage == ()
    if case == "empty":
        assert report.status == "complete"
        assert report.error is None
    else:
        assert report.status == "failed"
        assert report.error.code == (
            "work_limit" if case == "limit" else "invalid_inventory"
        )
