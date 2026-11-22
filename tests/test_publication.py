"""Verify the exact feedback a developer is asked to approve."""

from collections.abc import Generator
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest

from loop_reviewer.integrations.local_state.sqlite_reviews import SqliteReviewRunStore
from loop_reviewer.review.model import (
    CompletedReview,
    Finding,
    FindingLocation,
    GeneratedReview,
    PublicationReceipt,
    ReviewDraft,
    ReviewIdentity,
    ReviewRun,
    ReviewSelection,
    SavedDraft,
)
from loop_reviewer.review.publication import (
    PreparePublication,
    PublicationBlocked,
    PublicationSession,
    PublicationSessionFactory,
    PublishReview,
    RejectedPublication,
    RejectionReason,
)
from loop_reviewer.review.publication_model import (
    PublicationError,
    PublicationId,
    PublicationPreview,
    PublicationRecord,
    build_preview,
)
from loop_reviewer.review.pull_request import (
    PullRequest,
    PullRequestNotReviewable,
    PullRequestRef,
    ReviewedRevision,
)
from loop_reviewer.review.selection import GitHubReadError
from loop_reviewer.review.store import DraftConflict

_ID = PublicationId("75f718d3-9633-42ad-9097-22f320d068a8")
_MARKER = "<!-- loop-reviewer:75f718d3-9633-42ad-9097-22f320d068a8 -->"
_REVISION = ReviewedRevision("main", "a" * 40, "b" * 40, "c" * 40)
_SELECTION = ReviewSelection(
    PullRequestRef("github.com", "Acme", "repo", 7, 123),
    "reviewer",
    "author",
    _REVISION,
)


@pytest.mark.parametrize("summary_only", [False, True], ids=["inline", "summary-only"])
def test_payload_routes_retained_findings_once(summary_only: bool) -> None:
    location = FindingLocation("src/file.py", 8)
    missing = FindingLocation("src/file.py", 10)
    draft = ReviewDraft(
        "approved",
        "Edited summary",
        (
            Finding("low", "second at anchor", "low consequence", location),
            Finding("high", "first at anchor", "high consequence", location),
            Finding("medium", "missing anchor", "medium consequence", missing),
            Finding("low", "no location", "other consequence", None),
            Finding("low", "dropped", "dropped consequence", None),
        ),
    ).drop_finding(5)
    preview = build_preview(
        _ID,
        SavedDraft(1, 2, _SELECTION, draft),
        _SELECTION,
        123,
        "comment",
        frozenset() if summary_only else frozenset({location}),
    )
    assert preview.action == "comment"
    high = "**High**\n\nfirst at anchor\n\nhigh consequence"
    low = "**Low**\n\nsecond at anchor\n\nlow consequence"
    medium = "**Medium** (`src/file.py:10`)\n\nmissing anchor\n\nmedium consequence"
    no_location = "**Low**\n\nno location\n\nother consequence"
    if summary_only:
        assert not preview.comments
        blocks = [
            high.replace("**High**", "**High** (`src/file.py:8`)"),
            medium,
            low.replace("**Low**", "**Low** (`src/file.py:8`)"),
            no_location,
        ]
    else:
        (comment,) = preview.comments
        assert (comment.path, comment.line, comment.body) == (
            location.path,
            location.right_line,
            f"{high}\n\n{low}",
        )
        blocks = [medium, no_location]
    assert (
        preview.body
        == f"{_MARKER}\n\nEdited summary\n\n## Findings\n\n" + "\n\n".join(blocks)
    )


def test_publication_action_is_explicit() -> None:
    own = ReviewSelection(_SELECTION.ref, "reviewer", "REVIEWER", _REVISION)
    saved = SavedDraft(1, 1, own, ReviewDraft("approved", "Summary", ()))
    assert (
        build_preview(_ID, saved, own, 123, "comment", frozenset()).action == "comment"
    )
    with pytest.raises(PublicationError, match="own pull request"):
        build_preview(_ID, saved, own, 123, "approve", frozenset())


@pytest.mark.parametrize("mode", ["drift", "failed-anchors", "summary-only", "anchors"])
def test_preview_rechecks_comparison_after_anchor_lookup(
    tmp_path: Path, mode: str
) -> None:
    location = FindingLocation("file.py", 3)
    store = SqliteReviewRunStore(tmp_path / "state" / "reviews.sqlite3")
    run = store.start(_SELECTION, replaces=None).finish(
        GeneratedReview(
            ReviewDraft(
                "approved",
                "Summary",
                (Finding("low", "condition", "consequence", location),),
            ),
            "provider/model",
            None,
        )
    )
    store.save(run)
    requests: list[str] = []

    class Session:
        login = "REVIEWER"
        actor_id = 1

        def read_pull_request(self, ref: PullRequestRef) -> PullRequest:
            requests.append("pr")
            head = "d" * 40 if mode == "drift" and len(requests) > 1 else _REVISION.head
            return PullRequest(ref, "author", "open", False, "main", head)

        def read_revision(self, pull_request: PullRequest) -> ReviewedRevision:
            return replace(_REVISION, head=pull_request.head, base_tip="d" * 40)

        def usable_anchors(
            self, ref: PullRequestRef, requested: frozenset[FindingLocation]
        ) -> frozenset[FindingLocation]:
            requests.append("anchors")
            if mode == "failed-anchors":
                raise GitHubReadError("unavailable")
            return requested

    previewer = PreparePublication(store, lambda _: nullcontext(Session()), lambda: _ID)
    if mode == "drift":
        with pytest.raises(PublicationError, match="head"):
            previewer.preview(run.draft_target, "comment")
        assert store.get_publication(_ID) is None
    else:
        preview = previewer.preview(
            run.draft_target, "comment", summary_only=mode == "summary-only"
        )
        assert preview.selection.revision == _REVISION
        assert bool(preview.comments) == (mode == "anchors")
        assert requests == (
            ["pr"] if mode == "summary-only" else ["pr", "anchors", "pr"]
        )


class Remote:
    def __init__(self) -> None:
        self.login = "reviewer"
        self.actor_id = 123
        self.revision = _REVISION
        self.pull = PullRequest(
            _SELECTION.ref, "author", "open", False, "main", _REVISION.head
        )
        self.sent: list[PublicationPreview] = []
        self.outcome: PublicationReceipt | RejectionReason | Literal["unknown"] = (
            PublicationReceipt(45, "approve")
        )
        self.evidence: PublicationReceipt | Literal["no_match", "inconclusive"] = (
            "no_match"
        )
        self.scans = 0

    def read_pull_request(self, ref: PullRequestRef) -> PullRequest:
        return self.pull

    def read_revision(self, pull_request: PullRequest) -> ReviewedRevision:
        return self.revision

    def submit(
        self, preview: PublicationPreview
    ) -> PublicationReceipt | RejectionReason | Literal["unknown"]:
        self.sent.append(preview)
        return self.outcome

    def reconcile(
        self, preview: PublicationPreview, *, ref: PullRequestRef
    ) -> PublicationReceipt | Literal["no_match", "inconclusive"]:
        self.scans += 1
        return self.evidence

    @contextmanager
    def acquire(
        self, identity: ReviewIdentity
    ) -> Generator[PublicationSessionFactory[PublicationSession] | None]:
        assert identity == _SELECTION.identity
        yield lambda _: nullcontext(self)


def _ready(store: SqliteReviewRunStore) -> tuple[ReviewRun, PublicationPreview]:
    run = store.start(_SELECTION, replaces=None).finish(
        GeneratedReview(
            ReviewDraft("comment", "Exact summary", ()), "provider/model", None
        )
    )
    store.save(run)
    preview = build_preview(
        _ID, run.saved_draft(), _SELECTION, 123, "approve", frozenset()
    )
    store.save_preview(preview)
    return run, preview


@pytest.mark.parametrize(
    "change",
    [
        "edited",
        "wrong-login",
        "wrong-id",
        "rename",
        "retarget",
        "merge-base",
        "head",
        "closed",
        "own-pr",
        "base-tip",
    ],
)
def test_send_rechecks_snapshot_before_post(tmp_path: Path, change: str) -> None:
    store = SqliteReviewRunStore(tmp_path / "state" / "reviews.sqlite3")
    run, preview = _ready(store)
    remote = Remote()
    app = PublishReview(store, remote)
    if change == "edited":
        store.save_edit(
            run.draft_target,
            run.edit_draft(run.saved_draft().draft.replace_summary("Edited")),
        )
    elif change == "wrong-login":
        remote.login = "other"
    elif change == "wrong-id":
        remote.actor_id = 456
    elif change == "rename":
        remote.pull = replace(remote.pull, ref=replace(remote.pull.ref, repo="renamed"))
    elif change == "retarget":
        remote.revision = replace(_REVISION, target_branch="release")
        remote.pull = replace(remote.pull, target_branch="release")
    elif change == "merge-base":
        remote.revision = replace(_REVISION, merge_base="d" * 40)
    elif change == "head":
        remote.revision = replace(_REVISION, head="d" * 40)
        remote.pull = replace(remote.pull, head="d" * 40)
    elif change == "closed":
        remote.pull = replace(remote.pull, state="closed")
    elif change == "own-pr":
        remote.pull = replace(remote.pull, author="REVIEWER")
    else:
        remote.revision = replace(_REVISION, base_tip="d" * 40)
    if change == "base-tip":
        completed = app.publish(_ID)
        assert isinstance(completed, CompletedReview)
        assert completed == store.completed(run.selection.identity)
        assert completed.final_draft == run.saved_draft().draft
        assert completed.receipt == remote.outcome
        assert app.publish(_ID) == completed
        assert remote.sent == [preview]
    else:
        with pytest.raises((PublicationError, DraftConflict, PullRequestNotReviewable)):
            app.publish(_ID)
        assert not remote.sent
        record = store.get_publication(_ID)
        assert record is not None and record.status == "preview"


@pytest.mark.parametrize(
    "mode",
    [
        "found-after-closure",
        "crash-before-post",
        "not-found",
        "inconclusive",
        "rejected",
        "rejected-after-ambiguity",
    ],
)
def test_retry_recovers_or_requires_explicit_duplicate_risk(
    tmp_path: Path, mode: str
) -> None:
    path = tmp_path / "state" / "reviews.sqlite3"
    store = SqliteReviewRunStore(path)
    _, preview = _ready(store)
    remote = Remote()
    remote.outcome = RejectionReason.VALIDATION if mode == "rejected" else "unknown"
    if mode == "crash-before-post":
        store.reserve_send(_ID, "preview")
    else:
        outcome = PublishReview(store, remote).publish(_ID)
        if mode == "rejected":
            assert isinstance(outcome, RejectedPublication)
            assert outcome.reason is RejectionReason.VALIDATION
            assert outcome.record.status == "rejected"
        else:
            assert (
                isinstance(outcome, PublicationRecord)
                and outcome.status == "unconfirmed"
            )
    app = PublishReview(SqliteReviewRunStore(path), remote)
    if mode == "found-after-closure":
        remote.pull = replace(remote.pull, state="closed", head="d" * 40)
        remote.evidence = PublicationReceipt(45, "approve")
        completed = app.recover(_ID)
        assert isinstance(completed, CompletedReview)
        assert completed.receipt == remote.evidence
        assert completed == store.completed(preview.selection.identity)
        assert app.retry(_ID) == completed
        assert remote.sent == [preview]
        assert remote.scans == 1
    elif mode == "rejected":
        outcome = app.retry(_ID)
        assert (
            isinstance(outcome, RejectedPublication)
            and outcome.record.status == "rejected"
        )
        assert remote.sent == [preview, preview]
        assert remote.scans == 0
    elif mode == "inconclusive":
        remote.evidence = "inconclusive"
        with pytest.raises(PublicationBlocked) as blocked:
            app.retry(_ID, accept_duplicate_risk=True)
        assert blocked.value.reason == "evidence_inconclusive"
        assert blocked.value.publication_id == _ID
        assert blocked.value.ref == preview.selection.ref
        assert remote.sent == [preview]
        assert remote.scans == 1
    else:
        for operate in (app.recover, app.retry):
            with pytest.raises(PublicationBlocked) as blocked:
                operate(_ID)
            assert blocked.value.reason == "duplicate_risk_consent_required"
            assert blocked.value.publication_id == _ID
            assert blocked.value.ref == preview.selection.ref
        assert remote.sent == ([] if mode == "crash-before-post" else [preview])
        remote.outcome = RejectionReason.VALIDATION
        outcome = app.retry(_ID, accept_duplicate_risk=True)
        assert (
            isinstance(outcome, PublicationRecord) and outcome.status == "unconfirmed"
        )
        assert remote.sent[-1] == preview
        assert remote.scans == 3
        record = store.get_publication(_ID)
        assert record is not None and record.ambiguity_seen


def test_recovery_of_unsent_preview_requires_confirmation(tmp_path: Path) -> None:
    store = SqliteReviewRunStore(tmp_path / "state" / "reviews.sqlite3")
    _, preview = _ready(store)
    remote = Remote()
    app = PublishReview(store, remote)

    for operate in (app.recover, app.retry):
        with pytest.raises(PublicationBlocked) as blocked:
            operate(_ID)
        assert blocked.value.reason == "confirmation_required"
        assert blocked.value.publication_id == _ID
        assert blocked.value.ref == preview.selection.ref
    assert not remote.sent
    assert remote.scans == 0


def test_published_recovery_rejects_mismatched_history_without_resending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SqliteReviewRunStore(tmp_path / "state" / "reviews.sqlite3")
    _, preview = _ready(store)
    remote = Remote()
    app = PublishReview(store, remote)
    completed = app.publish(_ID)
    assert isinstance(completed, CompletedReview)
    wrong_receipt = PublicationReceipt(999, "approve")

    def mismatched_history(_: ReviewIdentity) -> CompletedReview:
        return replace(completed, receipt=wrong_receipt)

    monkeypatch.setattr(store, "completed", mismatched_history)

    with pytest.raises(PublicationError, match="history could not be read") as blocked:
        app.recover(_ID)
    assert f"{preview.selection.ref.url}#pullrequestreview-45" in str(blocked.value)
    assert "Check the local review database" in str(blocked.value)

    assert remote.sent == [preview]
    assert remote.scans == 0


def test_payload_is_not_truncated() -> None:
    draft = ReviewDraft("comment", "x" * 65_536, ())
    with pytest.raises(PublicationError, match="65,536"):
        build_preview(
            _ID,
            SavedDraft(1, 1, _SELECTION, draft),
            _SELECTION,
            123,
            "comment",
            frozenset(),
        )
