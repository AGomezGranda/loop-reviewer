"""Translate between SQLite records and review workflow objects."""

import json
import math
from typing import cast

from loop_reviewer.review.model import (
    CompletedReview,
    DraftTarget,
    DraftValidationError,
    Finding,
    FindingLocation,
    PublicationReceipt,
    Recommendation,
    ReviewDraft,
    ReviewerFailureKind,
    ReviewRun,
    ReviewSelection,
    RunStatus,
    RunTransitionError,
    Severity,
    SummaryOrigin,
    require_report_association,
    require_run_id,
)
from loop_reviewer.review.pull_request import PullRequestRef, ReviewedRevision
from loop_reviewer.review.store import ReviewStoreError
from loop_reviewer.reviewer.full_review_contract import FullReviewResult

_UNREADABLE = "The local review database is unreadable."
_STATUSES = {"running", "draft_ready", "failed", "abandoned"}


def attempt_fields(
    run_id: object, status: object, failure: object, failure_kind: object
) -> tuple[int, RunStatus, str | None, ReviewerFailureKind | None]:
    try:
        if type(run_id) is not int:
            raise DraftValidationError
        require_run_id(run_id)
    except DraftValidationError:
        raise ReviewStoreError(_UNREADABLE) from None
    if (
        not isinstance(status, str)
        or status not in _STATUSES
        or (failure is not None and not isinstance(failure, str))
        or failure_kind not in (None, "setup", "input", "failed")
        or (status == "failed") != (failure is not None)
        or (status != "failed" and failure_kind is not None)
    ):
        raise ReviewStoreError(_UNREADABLE)
    return (
        run_id,
        cast("RunStatus", status),
        failure,
        cast("ReviewerFailureKind | None", failure_kind),
    )


def completion_from_row(row: tuple[object, ...]) -> CompletedReview:
    (
        run_id,
        version,
        selection,
        draft,
        status,
        review_id,
        action,
        model,
        cost,
        *original_report,
    ) = row
    if type(run_id) is not int or run_id < 1 or type(version) is not int or version < 1:
        raise ReviewStoreError(_UNREADABLE)
    receipt = None
    if (
        status == "published"
        and type(review_id) is int
        and review_id > 0
        and (action == "comment" or action == "approve")
    ):
        receipt = PublicationReceipt(review_id, action)
    elif status != "not_published" or review_id is not None or action is not None:
        raise ReviewStoreError(_UNREADABLE)
    chosen = selection_from_json(selection)
    feedback = _draft_from_json(draft)
    report = None
    if original_report:
        report = _report_from_json(original_report[0])
        try:
            require_report_association(report, chosen, feedback)
        except RunTransitionError:
            raise ReviewStoreError(_UNREADABLE) from None
    return CompletedReview(
        DraftTarget(run_id, version),
        chosen,
        feedback,
        receipt,
        _generation_model(model),
        _generation_cost(cost),
        report,
    )


def selection_json(selection: ReviewSelection) -> str:
    return json.dumps(
        {
            "ref": {
                "host": selection.ref.host,
                "owner": selection.ref.owner,
                "repo": selection.ref.repo,
                "number": selection.ref.number,
                "repository_id": selection.ref.repository_id,
            },
            "account": selection.account,
            "author": selection.author,
            "revision": {
                "target_branch": selection.revision.target_branch,
                "base_tip": selection.revision.base_tip,
                "merge_base": selection.revision.merge_base,
                "head": selection.revision.head,
            },
        },
        separators=(",", ":"),
    )


def draft_json(draft: ReviewDraft) -> str:
    return json.dumps(
        {
            "verdict": draft.verdict,
            "summary": draft.summary,
            **(
                {"summary_origin": draft.summary_origin}
                if draft.summary_origin != "legacy"
                else {}
            ),
            "findings": [
                {
                    "severity": finding.severity,
                    "condition": finding.condition,
                    "consequence": finding.consequence,
                    **(
                        {"report_id": finding.report_id}
                        if finding.report_id is not None
                        else {}
                    ),
                    "location": None
                    if finding.location is None
                    else {
                        "path": finding.location.path,
                        "right_line": finding.location.right_line,
                    },
                }
                for finding in draft.findings
            ],
        },
        separators=(",", ":"),
    )


def run_from_row(row: tuple[object, ...]) -> ReviewRun:
    (
        run_id,
        selection,
        status,
        draft_text,
        failure,
        version,
        failure_kind,
        model,
        cost,
        report,
    ) = row
    id, saved_status, message, kind = attempt_fields(
        run_id, status, failure, failure_kind
    )
    if type(version) is not int or version < 1:
        raise ReviewStoreError(_UNREADABLE)
    draft = None if draft_text is None else _draft_from_json(draft_text)
    try:
        return ReviewRun(
            id,
            selection_from_json(selection),
            saved_status,
            draft,
            message,
            version,
            kind,
            _generation_model(model),
            _generation_cost(cost),
            _report_from_json(report),
        )
    except RunTransitionError:
        raise ReviewStoreError(_UNREADABLE) from None


def _report_from_json(value: object) -> FullReviewResult | None:
    if value is None:
        return None
    try:
        return FullReviewResult.model_validate_json(_json_text(value))
    except TypeError, ValueError, RecursionError:
        raise ReviewStoreError(_UNREADABLE) from None


def _generation_model(value: object) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ReviewStoreError(_UNREADABLE)
    return value


def _generation_cost(value: object) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ReviewStoreError(_UNREADABLE)
    return float(value)


def selection_from_json(value: object) -> ReviewSelection:
    try:
        data = json.loads(_json_text(value))
        ref = _object(data["ref"])
        revision = _object(data["revision"])
        repository_id = ref.get("repository_id")
        return ReviewSelection(
            PullRequestRef(
                _string(ref["host"]),
                _string(ref["owner"]),
                _string(ref["repo"]),
                _integer(ref["number"]),
                None if repository_id is None else _integer(repository_id),
            ),
            _string(data["account"]),
            _string(data["author"]),
            ReviewedRevision(
                _string(revision["target_branch"]),
                _string(revision["base_tip"]),
                _string(revision["merge_base"]),
                _string(revision["head"]),
            ),
        )
    except KeyError, TypeError, ValueError, RecursionError:
        raise ReviewStoreError(_UNREADABLE) from None


def _draft_from_json(value: object) -> ReviewDraft:
    try:
        data = _object(json.loads(_json_text(value)))
        verdict = _string(data["verdict"])
        findings = data["findings"]
        if not isinstance(findings, list):
            raise TypeError
        return ReviewDraft(
            cast("Recommendation", verdict),
            _string(data["summary"]),
            tuple(_finding(item) for item in findings),
            cast("SummaryOrigin", _string(data.get("summary_origin", "legacy"))),
        )
    except KeyError, TypeError, ValueError, RecursionError, DraftValidationError:
        raise ReviewStoreError(_UNREADABLE) from None


def _finding(value: object) -> Finding:
    data = _object(value)
    severity = _string(data["severity"])
    location = data.get("location")
    report_id = data.get("report_id")
    return Finding(
        cast("Severity", severity),
        _string(data["condition"]),
        _string(data["consequence"]),
        None if location is None else _location(location),
        None if report_id is None else _string(report_id),
    )


def _location(value: object) -> FindingLocation:
    data = _object(value)
    return FindingLocation(_string(data["path"]), _integer(data["right_line"]))


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError
    return cast("dict[str, object]", value)


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError
    return value


def _integer(value: object) -> int:
    if type(value) is not int:
        raise ValueError
    return value


def _json_text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError
    return value
