from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from loop_reviewer.integrations.local_state.sqlite_reviews import SqliteReviewRunStore
from loop_reviewer.review.active import ActiveReviews
from loop_reviewer.review.history import ReviewHistory
from loop_reviewer.review.model import (
    GeneratedReview,
    PublicationReceipt,
    ReviewDraft,
    ReviewRun,
    ReviewSelection,
)
from loop_reviewer.review.publication_model import PublicationId, build_preview
from loop_reviewer.review.pull_request import PullRequestRef, ReviewedRevision

_SELECTION = ReviewSelection(
    PullRequestRef("github.com", "acme", "repo", 1, 123),
    "alice",
    "bob",
    ReviewedRevision("main", "a" * 40, "b" * 40, "c" * 40),
)


def _draft(store: SqliteReviewRunStore, selection: ReviewSelection) -> ReviewRun:
    run = store.start(selection, replaces=None).finish(
        GeneratedReview(
            ReviewDraft("approved", "Saved feedback", ()), "provider/model", None
        )
    )
    store.save(run)
    return run


def test_completed_history_preserves_snapshot_and_pages_by_exclusive_run_id(
    tmp_path: Path,
) -> None:
    store = SqliteReviewRunStore(tmp_path / "reviews.sqlite3")
    history = ReviewHistory(store, store)
    completions = [
        store.complete(
            _draft(
                store,
                replace(
                    _SELECTION,
                    ref=replace(_SELECTION.ref, number=number, repository_id=None),
                ),
            ).complete_locally()
        )
        for number in range(1, 52)
    ]
    page = history.completions(None, 100)
    assert page == tuple(reversed(completions[1:]))
    assert history.completions(page[-1].source.run_id, 10) == (completions[0],)
    assert history.completions(completions[0].source.run_id, 10) == ()
    assert history.completion(completions[0].source.run_id) == completions[0]
    assert history.completion(completions[-1].source.run_id + 1) is None


def test_unconfirmed_history_matches_account_across_revisions(tmp_path: Path) -> None:
    store = SqliteReviewRunStore(tmp_path / "reviews.sqlite3")
    history = ReviewHistory(store, store)
    first = _draft(store, _SELECTION)
    second = _draft(
        store, replace(_SELECTION, revision=replace(_SELECTION.revision, head="d" * 40))
    )
    other = _draft(store, replace(_SELECTION, account="charlie"))
    preview = build_preview(
        PublicationId(str(uuid4())),
        first.saved_draft(),
        _SELECTION,
        123,
        "comment",
        frozenset(),
    )
    store.save_preview(preview)
    assert history.unconfirmed_publications() == ()
    record = store.reserve_send(preview.id, "preview")
    assert history.unconfirmed_publications() == (record,)
    assert history.publication(preview.id) == record
    assert history.unconfirmed_for(second.id) == record
    assert history.unconfirmed_for(other.id) is None
    store.record_rejection(preview.id)
    assert history.unconfirmed_publications() == ()
    assert history.unconfirmed_for(second.id) is None


@pytest.mark.parametrize("published", [False, True], ids=["local", "published"])
def test_active_reviews_follow_saved_attempts_and_completions(
    tmp_path: Path, published: bool
) -> None:
    store = SqliteReviewRunStore(tmp_path / "reviews.sqlite3")
    active = ActiveReviews(store, lambda _: False)
    first = _draft(store, _SELECTION)
    second = _draft(
        store, replace(_SELECTION, revision=replace(_SELECTION.revision, head="d" * 40))
    )
    other = replace(_SELECTION, account="charlie")
    old = store.start(other, replaces=None)
    store.save(old.fail("Failed", "failed"))
    retry = store.start(other, replaces=old.id)
    store.save(retry.fail("Retry failed", "setup"))
    legacy = _draft(
        store, replace(_SELECTION, ref=replace(_SELECTION.ref, repository_id=None))
    )
    preview = build_preview(
        PublicationId(str(uuid4())),
        first.saved_draft(),
        _SELECTION,
        123,
        "comment",
        frozenset(),
    )
    store.save_preview(preview)
    store.reserve_send(preview.id, "preview")
    assert [
        (item.run.id, item.status, item.run.publication_id) for item in active.list()
    ] == [
        (legacy.id, "draft_ready", None),
        (retry.id, "failed", None),
        (second.id, "recovery_required", preview.id),
        (first.id, "recovery_required", preview.id),
    ]
    assert store.unfinished_by_id(old.id) is None
    assert active.list()[1].run.failure == "Retry failed"
    assert active.list()[1].run.failure_kind == "setup"
    if published:
        store.record_published(preview.id, PublicationReceipt(45, "comment"))
    else:
        store.record_rejection(preview.id)
        store.complete(second.complete_locally())
    assert [item.run.id for item in active.list()] == [legacy.id, retry.id]
    assert store.unfinished_by_id(first.id) is None
    assert store.get_draft(first.id) == first
