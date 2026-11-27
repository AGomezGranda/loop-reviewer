"""Verify review draft, run, and completion rules."""

import pytest

from loop_reviewer.review.generate import ReviewerError
from loop_reviewer.review.hosted_report import project_report
from loop_reviewer.review.model import (
    DraftValidationError,
    Finding,
    FindingLocation,
    ReviewDraft,
    ReviewKey,
    ReviewSelection,
)
from loop_reviewer.review.pull_request import (
    PullRequestRef,
    ReviewComparison,
    ReviewedRevision,
)
from tests.support.hosted_report import offline_report

_REF = PullRequestRef("github.com", "Acme", "Repo", 7)
_BASE_TIP, _MERGE_BASE, _HEAD = (digit * 40 for digit in "abc")
_REVISION = ReviewedRevision("main", _BASE_TIP, _MERGE_BASE, _HEAD)


def _key(
    *,
    ref: PullRequestRef = _REF,
    account: str = "Reviewer",
    revision: ReviewedRevision = _REVISION,
) -> ReviewKey:
    return ReviewSelection(ref, account, "Author", revision).key


def test_review_key_uses_comparison_not_base_tip() -> None:
    assert _key() == _key(
        revision=ReviewedRevision("main", "d" * 40, _MERGE_BASE, _HEAD)
    )
    assert _key() != _key(
        revision=ReviewedRevision("release", _BASE_TIP, _MERGE_BASE, _HEAD)
    )
    assert _key() != _key(revision=ReviewedRevision("main", _BASE_TIP, "d" * 40, _HEAD))
    assert _key() != _key(
        revision=ReviewedRevision("main", _BASE_TIP, _MERGE_BASE, "d" * 40)
    )
    assert _key() == _key(
        ref=PullRequestRef("GitHub.com", "ACME", "REPO", 7), account="reviewer"
    )
    assert _key() != _key(account="teammate")
    assert _key().comparison == ReviewComparison("main", _MERGE_BASE, _HEAD)


def test_draft_orders_findings_by_impact() -> None:
    high_first = Finding("high", "first high", "consequence", None)
    low = Finding("low", "low", "consequence", None)
    medium = Finding("medium", "medium", "consequence", None)
    high_second = Finding("high", "second high", "consequence", None)
    draft = ReviewDraft("comment", "summary", (high_first, low, medium, high_second))

    assert draft.ordered_findings() == (
        high_first,
        high_second,
        medium,
        low,
    )
    assert ReviewDraft("approved", "summary", ()).ordered_findings() == ()


def test_draft_edits_preserve_recommendation_and_location() -> None:
    location = FindingLocation("app.py", 7)
    original = ReviewDraft(
        "comment", "original", (Finding("high", "old", "impact", location),)
    )
    summary = " \tNew summary\n"
    edited = original.replace_summary(summary).edit_finding(
        1, condition="new", consequence=None
    )
    assert edited == ReviewDraft(
        "comment", summary, (Finding("high", "new", "impact", location),), "edited"
    )
    assert original.summary == "original"
    assert edited.edit_finding(
        1, condition=None, consequence="new impact"
    ).findings == (Finding("high", "new", "new impact", location),)


def test_draft_mutations_use_preview_order() -> None:
    low = Finding("low", "low", "impact", None)
    tied = Finding("high", "same", "impact", None)
    draft = ReviewDraft("comment", "summary", (tied, low, tied))
    edited = draft.edit_finding(2, condition="edited", consequence=None)
    assert edited.findings == (tied, low, Finding("high", "edited", "impact", None))
    assert edited.drop_finding(2).findings == (tied, low)
    assert edited.drop_finding(2).drop_finding(2).drop_finding(1).findings == ()
    for number in (-1, 0, 4):
        with pytest.raises(DraftValidationError, match="Finding number"):
            draft.drop_finding(number)
    with pytest.raises(DraftValidationError, match="at least one"):
        draft.edit_finding(1, condition=None, consequence=None)


@pytest.mark.parametrize(
    ("path", "location"),
    [
        pytest.param("space%20%C3%A9.py", FindingLocation("space é.py", 2), id="utf8"),
        pytest.param("not-%FF.py", None, id="non-utf8"),
        pytest.param("line%0Abreak.py", None, id="control-in-path"),
    ],
)
def test_report_projection_only_offers_renderable_locations(
    path: str, location: FindingLocation | None
) -> None:
    report = offline_report(path)
    (finding,) = project_report(report).findings
    assert finding.location == location
    assert finding.report_id == report.root.findings[0].id


def test_failed_report_cannot_be_projected() -> None:
    report = offline_report("README", "README")
    with pytest.raises(ReviewerError, match="duplicate path"):
        project_report(report)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(" \n\t", id="blank"),
        pytest.param("é" * 32769, id="oversized-utf8"),
        pytest.param("bad\x00", id="nul"),
        pytest.param("bad\r", id="carriage-return"),
        pytest.param("bad\x85", id="c1-control"),
        pytest.param("bad\ud800", id="surrogate"),
    ],
)
def test_draft_rejects_invalid_edit_text(text: str) -> None:
    draft = ReviewDraft("comment", "summary", (Finding("high", "old", "impact", None),))
    with pytest.raises(DraftValidationError):
        ReviewDraft("comment", text, ())
    with pytest.raises(DraftValidationError):
        Finding("high", text, "impact", None)
    with pytest.raises(DraftValidationError):
        draft.replace_summary(text)
    with pytest.raises(DraftValidationError):
        draft.edit_finding(1, condition=None, consequence=text)
    assert draft.replace_summary("é" * 32768).summary == "é" * 32768
