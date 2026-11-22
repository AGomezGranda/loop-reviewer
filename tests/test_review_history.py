from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from loop_reviewer.integrations.local_state.sqlite_reviews import SqliteReviewRunStore
from loop_reviewer.review.history import ReviewHistory
from loop_reviewer.review.model import (
    GeneratedReview,
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
