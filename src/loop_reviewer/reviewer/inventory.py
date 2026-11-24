"""Assign inventory coverage without reading blob contents."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Literal, Protocol

from loop_reviewer.reviewer.inventory_contract import (
    MAX_CHANGES,
    ChangedFile,
    CompleteInventory,
    FailedInventory,
    FileCoverage,
    InventoryError,
    InventoryErrorCode,
    InventoryUsage,
    PinnedComparison,
    ReviewerRequest,
    ReviewerResult,
    decode_path,
)

RevisionSide = Literal["base", "head"]
BlobReadFailure = Literal["not_found", "limit_reached", "unavailable"]


class InventorySource(Protocol):
    def inventory(self) -> tuple[ChangedFile, ...]: ...


class InventorySourceError(Exception):
    def __init__(self, code: InventoryErrorCode, message: str) -> None:
        super().__init__(message)
        self.error = InventoryError(code=code, message=message)


def _sort_key(row: ChangedFile) -> tuple[bytes, bytes]:
    path = row.new or row.old
    assert path is not None
    return decode_path(path.path), decode_path(row.old.path) if row.old else b""


def validated_inventory(
    source: InventorySource, comparison: PinnedComparison
) -> tuple[ChangedFile, ...]:
    rows = source.inventory()
    hash_width = len(comparison.head)
    if len(rows) > MAX_CHANGES:
        raise InventorySourceError(
            "work_limit", "The comparison exceeds 10,000 changes."
        )
    old_paths: set[str] = set()
    new_paths: set[str] = set()
    validated: list[ChangedFile] = []
    for candidate in rows:
        try:
            row = ChangedFile.model_validate(candidate)
        except ValueError as error:
            raise InventorySourceError(
                "invalid_inventory", "File metadata is invalid."
            ) from error
        for side, paths in ((row.old, old_paths), (row.new, new_paths)):
            if side is None:
                continue
            if side.path in paths or len(side.object_id) != hash_width:
                raise InventorySourceError(
                    "invalid_inventory",
                    "File metadata has a duplicate path or wrong object ID format.",
                )
            paths.add(side.path)
        validated.append(row)
    return tuple(sorted(validated, key=_sort_key))


def _coverage(row: ChangedFile) -> FileCoverage:
    return FileCoverage(
        change_type=row.change_type,
        old=row.old,
        new=row.new,
        added_lines=row.added_lines,
        deleted_lines=row.deleted_lines,
        binary=row.binary,
        oversized=row.oversized,
        outcome="complete",
        highest_completed_check="inventory_only",
        required_investigation_ids=(),
    )


def review_inventory(
    request: ReviewerRequest,
    open_source: Callable[[PinnedComparison], AbstractContextManager[InventorySource]],
) -> ReviewerResult:
    comparison = request.comparison
    shared = {
        "schema_version": 1,
        "policy_version": 1,
        "comparison": comparison,
        "scope": "inventory",
        "recommendation": "none",
        "discussion_status": "not_requested",
        "usage": InventoryUsage(
            model_calls=0,
            input_tokens=0,
            output_tokens=0,
            estimated_cost_usd_micros=0,
            unaccounted_reserved_usd_micros=0,
        ),
        "investigation_coverage": (),
        "evidence_index": (),
        "findings": (),
        "private_leads": (),
        "linked_existing_threads": (),
    }
    try:
        with open_source(comparison) as source:
            coverage = tuple(
                _coverage(row) for row in validated_inventory(source, comparison)
            )
    except InventorySourceError as error:
        return ReviewerResult(
            FailedInventory.model_validate(
                shared
                | {
                    "status": "failed",
                    "stop_reason": "work_limit"
                    if error.error.code == "work_limit"
                    else "fatal_error",
                    "error": error.error,
                    "file_coverage": (),
                }
            )
        )
    return ReviewerResult(
        CompleteInventory.model_validate(
            shared
            | {
                "status": "complete",
                "stop_reason": "finished",
                "error": None,
                "file_coverage": coverage,
            }
        )
    )
