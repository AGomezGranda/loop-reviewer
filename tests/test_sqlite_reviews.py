"""Exercise durable review state, migrations, and concurrent SQLite writes."""

import json
import os
import sqlite3
import stat
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from typing import Protocol

import pytest

from loop_reviewer.integrations.local_state.inbox_schema import (
    create_inbox_tables,
    rename_inbox_projects,
)
from loop_reviewer.integrations.local_state.keys import identity_key
from loop_reviewer.integrations.local_state.review_schema import migrate_reviews
from loop_reviewer.integrations.local_state.sqlite_reviews import SqliteReviewRunStore
from loop_reviewer.review.drafts import EditReviewDraft
from loop_reviewer.review.feedback import feedback_markdown
from loop_reviewer.review.hosted_report import project_report
from loop_reviewer.review.model import (
    DraftTarget,
    DraftValidationError,
    Finding,
    FindingLocation,
    GeneratedReview,
    PublicationReceipt,
    ReviewAlreadyCompleted,
    ReviewDraft,
    ReviewRun,
    ReviewSelection,
)
from loop_reviewer.review.publication_model import (
    PublicationId,
    PublicationPreview,
    build_preview,
)
from loop_reviewer.review.pull_request import PullRequestRef, ReviewedRevision
from loop_reviewer.review.store import (
    DraftConflict,
    DraftStore,
    PublicationStore,
    ReviewRunStore,
    ReviewStoreError,
    UnconfirmedPublication,
)
from loop_reviewer.reviewer.full_review_contract import FullReviewResult
from tests.support.hosted_report import offline_pinned_report, offline_report
from tests.test_local_checks import PINS

_SELECTION = ReviewSelection(
    PullRequestRef("github.com", "acme", "repo", 7),
    "reviewer",
    "teammate",
    ReviewedRevision("main", "a" * 40, "b" * 40, "c" * 40),
)
_DRAFT = ReviewDraft(
    "comment",
    "Two findings.",
    (
        Finding(
            "high",
            "condition",
            "consequence",
            FindingLocation("src/app.py", 2),
        ),
        Finding("low", "unlocated condition", "unlocated consequence", None),
    ),
)


@pytest.mark.parametrize("published", [False, True], ids=["local", "published"])
def test_original_report_survives_edits_and_completion(
    tmp_path: Path, published: bool
) -> None:
    path = tmp_path / "state.db"
    store = SqliteReviewRunStore(path)
    selection = _report_selection()
    report = _partial_report()
    assert report.root.status == "partial"
    original = report.model_dump_json()
    draft = project_report(report)
    assert draft.summary == "" and draft.summary_origin == "generated"
    assert [item.location for item in draft.findings] == [
        FindingLocation("first.py", 2),
        FindingLocation("second.py", 2),
    ]
    assert [item.condition for item in draft.findings] == [
        item.trigger for item in report.root.findings
    ]
    started = store.start(selection, replaces=None)
    run = started.finish(GeneratedReview(draft, None, None, report))
    store.save(run)
    reopened = SqliteReviewRunStore(path)
    assert reopened.latest(selection.key) == run
    assert run.saved_draft().report == report

    edits = EditReviewDraft(reopened)
    edited = edits.edit_summary(run.draft_target, "My summary")
    dropped = edits.drop_finding(
        DraftTarget(run.id, edited.draft_version), 1
    ).saved_draft()
    assert dropped.draft.summary == "My summary"
    assert dropped.draft.verdict == report.root.recommendation
    assert "private lead canary" not in feedback_markdown(dropped.draft)
    assert tuple(item.report_id for item in dropped.draft.findings) == tuple(
        item.id for item in report.root.findings[1:]
    )
    updated = reopened.get_draft(run.id)
    assert updated is not None
    if published:
        id = PublicationId("75f718d3-9633-42ad-9097-22f320d068a8")
        summary_only = build_preview(
            id, dropped, selection, 123, "comment", frozenset()
        )
        assert not summary_only.comments
        assert (
            "second.py:2" in summary_only.body and "first.py" not in summary_only.body
        )
        assert "private lead canary" not in summary_only.body
        preview = build_preview(
            id,
            dropped,
            selection,
            123,
            "comment",
            frozenset({FindingLocation("second.py", 2)}),
        )
        assert preview.body == f"{id.marker}\n\nMy summary"
        (comment,) = preview.comments
        assert comment.path == "second.py" and "private lead canary" not in comment.body
        reopened.save_preview(preview)
        reopened.reserve_send(preview.id, "preview")
        completed = reopened.record_published(
            preview.id, PublicationReceipt(8, "comment")
        )
    else:
        completed = reopened.complete(updated.complete_locally())
    assert completed.report == report
    assert (
        reopened.completion(run.id)
        == reopened.completed(selection.identity)
        == completed
    )
    assert reopened.completions(None, 10)[0].report is None
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT reviewer_report FROM review_runs"
        ).fetchone() == (original,)


def test_pinned_report_round_trips_through_draft_and_history(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = SqliteReviewRunStore(path)
    report = offline_pinned_report("main.py", "settings.ts")
    run = store.start(_report_selection(), replaces=None).finish(
        GeneratedReview(project_report(report), None, None, report)
    )
    store.save(run)
    reopened = SqliteReviewRunStore(path)
    saved = reopened.get_draft(run.id)
    assert saved is not None
    assert saved.saved_draft().report == report
    edited = EditReviewDraft(reopened).edit_summary(
        run.draft_target, "Pinned text review"
    )
    reopened.complete(edited.complete_locally())
    completion = reopened.completion(run.id)
    assert completion is not None and completion.report == report
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT reviewer_report FROM review_runs"
        ).fetchone() == (report.model_dump_json(),)


@pytest.mark.parametrize(
    "corruption",
    [
        "invalid-json",
        "unknown-version",
        "string-version",
        "malformed-v5",
        "malformed-v6",
        "other-pins",
        "missing",
    ],
)
def test_saved_report_corruption_is_not_treated_as_legacy(
    tmp_path: Path, corruption: str
) -> None:
    path = tmp_path / "state.db"
    store = SqliteReviewRunStore(path)
    report = offline_report("README")
    start = store.start(_report_selection(), replaces=None)
    run = start.finish(GeneratedReview(project_report(report), None, None, report))
    store.save(run)
    store.complete(run.complete_locally())

    data = report.model_dump()
    payload: str | None = None
    if corruption == "invalid-json":
        payload = "not-json"
    elif corruption == "unknown-version":
        payload = json.dumps(data | {"schema_version": 99})
    elif corruption == "string-version":
        payload = json.dumps(data | {"schema_version": "5"})
    elif corruption == "malformed-v5":
        payload = json.dumps(data | {"policy_version": 4})
    elif corruption == "malformed-v6":
        compiler = offline_report("broken.py").model_dump(mode="json")
        payload = json.dumps(compiler | {"schema_version": 6, "policy_version": 4})
    elif corruption == "other-pins":
        data["comparison"]["head"] = "e" * 40
        payload = FullReviewResult.model_validate(data).model_dump_json()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE review_runs SET reviewer_report = ?", (payload,))

    with pytest.raises(ReviewStoreError, match="unreadable"):
        store.get_draft(run.id)
    with pytest.raises(ReviewStoreError, match="unreadable"):
        store.completion(run.id)
    assert store.completions(None, 1)[0].report is None


def _report_selection() -> ReviewSelection:
    return replace(
        _SELECTION,
        ref=replace(_SELECTION.ref, repository_id=42),
        revision=ReviewedRevision("main", PINS.merge_base, PINS.merge_base, PINS.head),
    )


def _partial_report() -> FullReviewResult:
    result = offline_report("first.py", "second.py")
    data = result.model_dump()
    assert len(result.root.findings) == 2
    data["private_leads"] = (
        {
            "id": "c00999",
            "file_id": result.root.findings[0].file_id,
            "changed_behavior": "private lead canary",
            "trigger": "private trigger",
            "consequence": "private consequence",
            "missing_fact": "Unverified",
            "evidence_ids": (),
        },
    )
    return FullReviewResult.model_validate(data)


class _PublicationPersistence(DraftStore, PublicationStore, ReviewRunStore, Protocol):
    pass


def _store(tmp_path: Path) -> SqliteReviewRunStore:
    return SqliteReviewRunStore(tmp_path / "state" / "reviews.sqlite3")


def _path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "reviews.sqlite3"


def test_round_trips_run_and_draft(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = store.start(_SELECTION, replaces=None)
    store.save(run.finish(GeneratedReview(_DRAFT, "provider/model", None)))

    stored = SqliteReviewRunStore(_path(tmp_path)).latest(_SELECTION.key)

    assert stored is not None
    assert stored.id == run.id
    assert stored.selection == _SELECTION
    assert stored.status == "draft_ready"
    assert stored.draft == _DRAFT
    assert stored.failure is None
    assert stored.draft_version == 1


@pytest.mark.parametrize("published", [False, True], ids=["local", "published"])
def test_generation_metadata_survives_edits_and_completion(
    tmp_path: Path, published: bool
) -> None:
    store = _store(tmp_path)
    selection = replace(_SELECTION, ref=replace(_SELECTION.ref, repository_id=17))
    generated = GeneratedReview(_DRAFT, "provider/model", 0.023456789)
    run = store.start(selection, replaces=None).finish(generated)
    store.save(run)

    reopened = _store(tmp_path)
    edited = EditReviewDraft(reopened).edit_summary(run.draft_target, "Edited")
    saved = _store(tmp_path).get_draft(run.id)
    assert saved == edited
    assert saved is not None
    assert saved.saved_draft().generation_model == generated.model
    assert saved.saved_draft().estimated_cost_usd == generated.estimated_cost_usd

    if published:
        id = PublicationId("75f718d3-9633-42ad-9097-22f320d068a8")
        preview = build_preview(
            id, saved.saved_draft(), selection, 17, "comment", frozenset()
        )
        legacy_preview = build_preview(
            id,
            replace(
                saved.saved_draft(), generation_model=None, estimated_cost_usd=None
            ),
            selection,
            17,
            "comment",
            frozenset(),
        )
        assert preview == legacy_preview
        reopened.save_preview(preview)
        reopened.reserve_send(id, "preview")
        receipt = PublicationReceipt(45, "comment")
        completion = reopened.record_published(id, receipt)
        assert _store(tmp_path).record_published(id, receipt) == completion
    else:
        completion = reopened.complete(saved.complete_locally())
        assert _store(tmp_path).complete(saved.complete_locally()) == completion
    assert _store(tmp_path).completion(run.id) == completion
    assert completion.final_draft.summary == "Edited"
    assert completion.generation_model == generated.model
    assert completion.estimated_cost_usd == generated.estimated_cost_usd


@pytest.mark.parametrize(
    "case",
    [
        "run-key",
        "completion-key",
        "nested-selection",
        "nested-draft",
        "blank-feedback",
        "invalid-location",
        "inconsistent-run",
    ],
)
def test_rejects_inconsistent_stored_records(tmp_path: Path, case: str) -> None:
    store = _store(tmp_path)
    run = store.start(_SELECTION, replaces=None).finish(
        GeneratedReview(_DRAFT, "provider/model", None)
    )
    store.save(run)
    store.complete(run.complete_locally())
    connection = sqlite3.connect(_path(tmp_path))
    try:
        table = "review_completions" if case == "completion-key" else "review_runs"
        if case in {"run-key", "completion-key"}:
            row = connection.execute(f"SELECT selection FROM {table}").fetchone()
            assert row is not None
            selection = json.loads(row[0])
            selection["account"] = "different-account"
            connection.execute(
                f"UPDATE {table} SET selection = ?", (json.dumps(selection),)
            )
        elif case == "inconsistent-run":
            connection.execute("UPDATE review_runs SET failure = 'unexpected'")
        elif case in {"blank-feedback", "invalid-location"}:
            row = connection.execute("SELECT draft FROM review_runs").fetchone()
            assert row is not None
            draft = json.loads(row[0])
            if case == "blank-feedback":
                draft["summary"] = " "
            else:
                draft["findings"][0]["location"]["right_line"] = 0
            connection.execute("UPDATE review_runs SET draft = ?", (json.dumps(draft),))
        else:
            column = "selection" if case == "nested-selection" else "draft"
            connection.execute(
                f"UPDATE review_runs SET {column} = ?", ("[" * 1500 + "]" * 1500,)
            )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(ReviewStoreError, match="unreadable"):
        if case == "completion-key":
            store.completed(_SELECTION.identity)
        else:
            store.latest(_SELECTION.key)


@pytest.mark.parametrize("change", ["selection", "version"])
def test_save_rejects_result_for_different_run_snapshot(
    tmp_path: Path, change: str
) -> None:
    store = _store(tmp_path)
    running = store.start(_SELECTION, replaces=None)
    finished = running.finish(GeneratedReview(_DRAFT, "provider/model", None))
    mismatched = (
        replace(finished, selection=_new_head())
        if change == "selection"
        else replace(finished, draft_version=2)
    )
    with pytest.raises(ReviewStoreError, match="does not match the saved run"):
        store.save(mismatched)
    assert store.latest(_SELECTION.key) == running
    store.save(finished)
    assert store.latest(_SELECTION.key) == finished


@pytest.mark.parametrize(
    "previous_repository_id", [123, None], ids=["reused-name", "legacy-name"]
)
def test_does_not_reuse_runs_or_history_for_unrelated_repository_ids(
    tmp_path: Path, previous_repository_id: int | None
) -> None:
    store = _store(tmp_path)
    previous = replace(
        _SELECTION, ref=replace(_SELECTION.ref, repository_id=previous_repository_id)
    )
    old = store.start(previous, replaces=None).finish(
        GeneratedReview(_DRAFT, "provider/model", None)
    )
    store.save(old)
    completed = store.complete(old.complete_locally())

    replacement = replace(_SELECTION, ref=replace(_SELECTION.ref, repository_id=456))
    reopened = _store(tmp_path)
    assert reopened.completed(replacement.identity) is None
    assert reopened.latest(replacement.key) is None
    assert reopened.completed(previous.identity) == completed
    assert reopened.complete(old.complete_locally()) == completed
    assert reopened.start(replacement, replaces=None).id != old.id


def test_draft_can_be_resumed_after_repository_rename(tmp_path: Path) -> None:
    store = _store(tmp_path)
    original = replace(_SELECTION, ref=replace(_SELECTION.ref, repository_id=123))
    saved = store.start(original, replaces=None).finish(
        GeneratedReview(_DRAFT, "provider/model", None)
    )
    store.save(saved)

    renamed = replace(
        original, ref=replace(original.ref, owner="new-owner", repo="new-name")
    )
    assert _store(tmp_path).latest(renamed.key) == saved


def test_corrupt_database_reports_an_unreadable_database(tmp_path: Path) -> None:
    path = _path(tmp_path)
    path.parent.mkdir()
    path.write_bytes(b"not a database")
    with pytest.raises(ReviewStoreError, match="database is unreadable") as error:
        _store(tmp_path).latest(_SELECTION.key)
    assert isinstance(error.value.__cause__, sqlite3.DatabaseError)


def _legacy_database(tmp_path: Path) -> str:
    path = _path(tmp_path)
    path.parent.mkdir()
    selection = json.dumps(
        {
            "ref": {"host": "github.com", "owner": "acme", "repo": "repo", "number": 7},
            "account": "reviewer",
            "author": "teammate",
            "revision": {
                "target_branch": "main",
                "base_tip": "a" * 40,
                "merge_base": "b" * 40,
                "head": "c" * 40,
            },
        }
    )
    connection = sqlite3.connect(path)
    try:
        connection.executescript("""
            CREATE TABLE review_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,
                review_key TEXT NOT NULL, selection TEXT NOT NULL, status TEXT NOT NULL,
                draft TEXT, failure TEXT, is_current INTEGER NOT NULL);
            CREATE UNIQUE INDEX review_runs_current ON review_runs (review_key)
                WHERE is_current = 1;
            PRAGMA user_version = 1;
            """)
        connection.execute(
            "INSERT INTO review_runs VALUES (1, ?, ?, 'draft_ready', ?, NULL, 1)",
            (
                json.dumps(
                    [
                        "github.com",
                        "acme",
                        "repo",
                        7,
                        "reviewer",
                        "main",
                        "b" * 40,
                        "c" * 40,
                    ],
                    separators=(",", ":"),
                ),
                selection,
                '{"verdict":"approved","summary":"legacy","findings":[]}',
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return selection


def test_upgrades_v1_drafts_without_changing_selection(tmp_path: Path) -> None:
    original_json = _legacy_database(tmp_path)
    store = _store(tmp_path)
    stored = store.get_draft(1)
    assert stored == ReviewRun(
        1, _SELECTION, "draft_ready", ReviewDraft("approved", "legacy", ()), None
    )
    updated = stored.edit_draft(ReviewDraft("approved", "edited", ()))
    store.save_edit(DraftTarget(1, 1), updated)
    assert _store(tmp_path).latest(_SELECTION.key) == updated
    connection = sqlite3.connect(_path(tmp_path))
    try:
        assert connection.execute("SELECT selection FROM review_runs").fetchone() == (
            original_json,
        )
        assert connection.execute("PRAGMA user_version").fetchone() == (13,)
    finally:
        connection.close()


def test_upgrades_v8_with_unpriced_draft_and_completion(tmp_path: Path) -> None:
    _legacy_database(tmp_path)
    with sqlite3.connect(_path(tmp_path)) as connection:
        migrate_reviews(connection, 1)
        create_inbox_tables(connection, 6)
        rename_inbox_projects(connection, 7)
        connection.execute(
            "INSERT INTO review_completions "
            "(identity_key, run_id, draft_version, selection, final_draft, publication_status) "
            "SELECT ?, id, draft_version, selection, draft, 'not_published' FROM review_runs",
            (identity_key(_SELECTION.identity),),
        )
        connection.execute("PRAGMA user_version = 8")

    store = _store(tmp_path)
    run = store.get_draft(1)
    completion = store.completion(1)
    assert run is not None and completion is not None
    assert run.saved_draft().generation_model is None
    assert run.saved_draft().estimated_cost_usd is None
    assert completion.generation_model is None
    assert completion.estimated_cost_usd is None
    assert completion.final_draft == ReviewDraft("approved", "legacy", ())
    assert _store(tmp_path).complete(completion) == completion
    with sqlite3.connect(_path(tmp_path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (13,)


def test_upgrades_v12_completion_without_inventing_a_date(tmp_path: Path) -> None:
    store = _store(tmp_path)
    completion = store.complete(_saved(store).complete_locally())
    with sqlite3.connect(_path(tmp_path)) as connection:
        connection.execute("ALTER TABLE review_completions DROP COLUMN recorded_at")
        connection.execute("PRAGMA user_version = 12")

    reopened = _store(tmp_path)
    assert reopened.completion(completion.source.run_id) == completion
    with sqlite3.connect(_path(tmp_path)) as connection:
        assert connection.execute(
            "SELECT recorded_at FROM review_completions"
        ).fetchone() == (None,)


@pytest.mark.parametrize("published", [False, True], ids=["local", "published"])
def test_first_completion_records_time_without_restamping(
    tmp_path: Path, published: bool
) -> None:
    now = datetime(2026, 11, 30, tzinfo=UTC)
    store = SqliteReviewRunStore(_path(tmp_path), now=lambda: now)
    if published:
        _, preview = _publication_setup(store)
        store.reserve_send(preview.id, "preview")
        receipt = PublicationReceipt(45, "comment")

        def finish() -> None:
            store.record_published(preview.id, receipt)
    else:
        completion = _saved(store).complete_locally()

        def finish() -> None:
            store.complete(completion)

    finish()
    first = int(now.timestamp())
    now = now.replace(hour=1)
    finish()
    with sqlite3.connect(_path(tmp_path)) as connection:
        assert connection.execute(
            "SELECT recorded_at FROM review_completions"
        ).fetchall() == [(first,)]


@pytest.mark.parametrize(
    "assignment",
    ["publication_status = 'invalid'", "recorded_at = -1", "recorded_at = 1.5"],
    ids=["status", "negative-time", "fractional-time"],
)
def test_stats_rejects_invalid_saved_facts(tmp_path: Path, assignment: str) -> None:
    store = _store(tmp_path)
    store.complete(_saved(store).complete_locally())
    with sqlite3.connect(_path(tmp_path)) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(f"UPDATE review_completions SET {assignment}")
    with pytest.raises(ReviewStoreError, match="unreadable"):
        store.completion_facts()


def test_upgrades_v9_without_changing_legacy_feedback_or_pending_send(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    run, preview = _publication_setup(store)
    pending = store.reserve_send(preview.id, "preview")
    completed_run = _saved(
        store, replace(run.selection, ref=replace(run.selection.ref, number=42))
    )
    completed = store.complete(completed_run.complete_locally())
    with sqlite3.connect(_path(tmp_path)) as connection:
        original = connection.execute(
            "SELECT preview FROM review_publications"
        ).fetchone()
        connection.execute("ALTER TABLE review_runs DROP COLUMN reviewer_report")
        connection.execute(
            "ALTER TABLE inbox_settings DROP COLUMN excluded_title_terms"
        )
        connection.execute("ALTER TABLE inbox_settings DROP COLUMN scan_version")
        connection.execute("ALTER TABLE review_completions DROP COLUMN recorded_at")
        connection.execute("PRAGMA user_version = 9")

    reopened = _store(tmp_path)
    assert reopened.get_draft(run.id) == run
    assert reopened.completion(completed_run.id) == completed
    assert reopened.unconfirmed_publications() == (pending,)
    with sqlite3.connect(_path(tmp_path)) as connection:
        assert (
            connection.execute("SELECT preview FROM review_publications").fetchone()
            == original
        )
        assert connection.execute("PRAGMA user_version").fetchone() == (13,)
    reopened.record_rejection(preview.id)
    assert reopened.complete(run.complete_locally()).final_draft == run.draft


def test_upgrades_v4_without_losing_completed_history(tmp_path: Path) -> None:
    store = _store(tmp_path)
    completion = store.complete(_saved(store).complete_locally())
    connection = sqlite3.connect(_path(tmp_path))
    try:
        _restore_legacy_completion_schema(connection)
        connection.execute("ALTER TABLE review_runs DROP COLUMN failure_kind")
        connection.execute("PRAGMA user_version = 4")
        connection.commit()
    finally:
        connection.close()

    reopened = _store(tmp_path)
    legacy_completion = replace(completion, generation_model=None)
    assert reopened.completed(_SELECTION.identity) == legacy_completion
    assert reopened.complete(completion) == legacy_completion


def _restore_v6_schema(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        ALTER TABLE review_runs DROP COLUMN reviewer_report;
        ALTER TABLE review_runs DROP COLUMN estimated_cost_usd;
        ALTER TABLE review_runs DROP COLUMN generation_model;
        ALTER TABLE review_completions DROP COLUMN recorded_at;
        ALTER TABLE review_completions DROP COLUMN estimated_cost_usd;
        ALTER TABLE review_completions DROP COLUMN generation_model;
        DROP INDEX review_runs_activity;
        DROP TABLE inbox_deferrals;
        DROP TABLE inbox_repositories;
        DROP TABLE inbox_settings;
        PRAGMA user_version = 6;
        """)


def _restore_legacy_completion_schema(connection: sqlite3.Connection) -> None:
    _restore_v6_schema(connection)
    connection.execute("DROP TABLE IF EXISTS review_publications")
    columns = "identity_key, run_id, draft_version, selection, final_draft, publication_status"
    connection.executescript(f"""
        ALTER TABLE review_completions RENAME TO new_completions;
        CREATE TABLE review_completions (
            identity_key TEXT PRIMARY KEY NOT NULL, run_id INTEGER NOT NULL,
            draft_version INTEGER NOT NULL CHECK (draft_version >= 1), selection TEXT NOT NULL,
            final_draft TEXT NOT NULL, publication_status TEXT NOT NULL CHECK (publication_status = 'not_published')
        );
        INSERT INTO review_completions ({columns}) SELECT {columns} FROM new_completions;
        DROP TABLE new_completions;
    """)


def test_upgrade_from_v5_preserves_edited_drafts_and_local_history(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    run = _saved(store)
    edited = EditReviewDraft(store).edit_summary(run.draft_target, "Edited")
    completion = store.complete(edited.complete_locally())
    with sqlite3.connect(_path(tmp_path)) as connection:
        _restore_legacy_completion_schema(connection)
        connection.execute("PRAGMA user_version = 5")
    reopened = _store(tmp_path)
    assert reopened.get_draft(run.id) == replace(edited, generation_model=None)
    legacy_completion = replace(completion, generation_model=None)
    assert reopened.completed(_SELECTION.identity) == legacy_completion
    assert reopened.complete(completion) == legacy_completion
    with sqlite3.connect(_path(tmp_path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (13,)
        assert connection.execute(
            "SELECT github_review_id, publication_action FROM review_completions"
        ).fetchone() == (None, None)


@pytest.mark.parametrize("change", ["edit", "finish"])
def test_preview_save_rejects_changed_or_completed_draft(
    tmp_path: Path, change: str
) -> None:
    store = _store(tmp_path)
    selection = replace(_SELECTION, ref=replace(_SELECTION.ref, repository_id=123))
    run = _saved(store, selection)
    preview = build_preview(
        PublicationId("75f718d3-9633-42ad-9097-22f320d068a8"),
        run.saved_draft(),
        selection,
        17,
        "comment",
        frozenset(),
    )
    store.save_preview(preview)
    recorded = _store(tmp_path).get_publication(preview.id)
    assert (
        recorded is not None
        and recorded.preview == preview
        and recorded.status == "preview"
    )
    if change == "edit":
        EditReviewDraft(store).edit_summary(run.draft_target, "Edited")
    else:
        store.complete(run.complete_locally())
    with pytest.raises((DraftConflict, ReviewAlreadyCompleted)):
        store.save_preview(
            replace(
                preview,
                id=PublicationId("75f718d3-9633-42ad-9097-22f320d068a9"),
                body=preview.body.replace(
                    preview.id.value, "75f718d3-9633-42ad-9097-22f320d068a9"
                ),
            )
        )


def _publication_setup(
    store: _PublicationPersistence, *, second: bool = False
) -> tuple[ReviewRun, PublicationPreview]:
    selection = replace(
        _new_head() if second else _SELECTION,
        ref=replace(_SELECTION.ref, repository_id=123),
    )
    run = _saved(store, selection)
    id = PublicationId(
        "75f718d3-9633-42ad-9097-22f320d068a9"
        if second
        else "75f718d3-9633-42ad-9097-22f320d068a8"
    )
    preview = build_preview(
        id, run.saved_draft(), selection, 17, "comment", frozenset()
    )
    store.save_preview(preview)
    return run, preview


def test_upgrade_from_v6_preserves_reviews_and_projects_stable_activity(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    completed_run, preview = _publication_setup(store)
    store.reserve_send(preview.id, "preview")
    completion = store.record_published(preview.id, PublicationReceipt(45, "comment"))
    draft_selection = replace(
        completed_run.selection, ref=replace(completed_run.selection.ref, number=9)
    )
    draft = _saved(store, draft_selection)
    latest = replace(
        draft_selection, revision=replace(draft_selection.revision, head="9" * 40)
    )
    store.start(latest, replaces=None)
    legacy = _saved(
        store,
        replace(draft_selection, ref=replace(draft_selection.ref, repository_id=None)),
    )
    malformed = _saved(
        store, replace(draft_selection, ref=replace(draft_selection.ref, number=10))
    )
    with sqlite3.connect(_path(tmp_path)) as connection:
        _restore_v6_schema(connection)
        connection.execute(
            "UPDATE review_runs SET review_key = 'not-json' WHERE id = ?",
            (malformed.id,),
        )
    reopened = _store(tmp_path)
    assert reopened.get_draft(draft.id) == replace(draft, generation_model=None)
    assert reopened.completed(completed_run.selection.identity) == replace(
        completion, generation_model=None
    )
    publication = reopened.get_publication(preview.id)
    assert publication is not None and publication.receipt == completion.receipt
    assert reopened.activity(
        (
            draft_selection.identity,
            legacy.selection.identity,
            malformed.selection.identity,
            completed_run.selection.identity,
        )
    ) == {
        draft_selection.identity: "running",
        completed_run.selection.identity: "completed",
    }


@pytest.mark.parametrize("open_store", [_store], ids=["sqlite"])
def test_publication_reservation_excludes_competing_send_finish_and_edit(
    tmp_path: Path, open_store: Callable[[Path], _PublicationPersistence]
) -> None:
    store, other = open_store(tmp_path), open_store(tmp_path)
    run, preview = _publication_setup(store)
    second, next_preview = _publication_setup(other, second=True)
    assert store.reserve_send(preview.id, "preview").status == "unconfirmed"
    fresh_preview = build_preview(
        PublicationId("75f718d3-9633-42ad-9097-22f320d068aa"),
        second.saved_draft(),
        second.selection,
        next_preview.actor_id,
        next_preview.action,
        frozenset(),
    )
    for operate in (
        lambda: other.reserve_send(next_preview.id, "preview"),
        lambda: other.save_preview(fresh_preview),
        lambda: other.complete(second.complete_locally()),
        lambda: EditReviewDraft(other).edit_summary(second.draft_target, "Edited"),
    ):
        with pytest.raises(UnconfirmedPublication) as blocked:
            operate()
        assert blocked.value.publication_id == preview.id
        assert blocked.value.ref == preview.selection.ref
    assert store.record_rejection(preview.id).status == "rejected"
    assert (
        other.complete(second.complete_locally()).publication_status == "not_published"
    )
    with pytest.raises(ReviewAlreadyCompleted):
        store.reserve_send(preview.id, "rejected")
    assert run.selection.identity == second.selection.identity


def test_publication_completion_is_atomic(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run, preview = _publication_setup(store)
    store.reserve_send(preview.id, "preview")
    receipt = PublicationReceipt(45, "comment")
    with sqlite3.connect(_path(tmp_path)) as connection:
        connection.execute(
            "CREATE TRIGGER fail_publish AFTER INSERT ON review_completions BEGIN SELECT RAISE(ABORT, 'forced failure'); END"
        )
    with pytest.raises(ReviewStoreError):
        store.record_published(preview.id, receipt)
    reopened = _store(tmp_path)
    record = reopened.get_publication(preview.id)
    assert record is not None and record.status == "unconfirmed"
    assert reopened.completed(run.selection.identity) is None
    with sqlite3.connect(_path(tmp_path)) as connection:
        assert (
            connection.execute("SELECT recorded_at FROM review_completions").fetchall()
            == []
        )
        connection.execute("DROP TRIGGER fail_publish")
    completed = reopened.record_published(preview.id, receipt)
    assert reopened.record_published(preview.id, receipt) == completed
    assert _store(tmp_path).completed(run.selection.identity) == completed
    assert completed.receipt == receipt


def test_upgrades_legacy_failure_without_inventing_a_category(tmp_path: Path) -> None:
    _legacy_database(tmp_path)
    connection = sqlite3.connect(_path(tmp_path))
    try:
        connection.execute(
            "UPDATE review_runs SET status = 'failed', draft = NULL, failure = 'legacy'"
        )
        connection.commit()
    finally:
        connection.close()

    run = _store(tmp_path).latest(_SELECTION.key)
    assert run is not None
    assert (
        run.status == "failed" and run.failure == "legacy" and run.failure_kind is None
    )


@pytest.mark.parametrize(
    ("run_id", "message"),
    [
        pytest.param(0, "positive integer", id="non-positive-run-id"),
        pytest.param(2**63, "outside the supported range", id="oversized-run-id"),
    ],
)
def test_draft_application_rejects_invalid_run_id(
    tmp_path: Path, run_id: int, message: str
) -> None:
    application = EditReviewDraft(_store(tmp_path))
    with pytest.raises(DraftValidationError, match=message):
        application.show(run_id)
    assert not _path(tmp_path).exists()


def test_draft_version_prevents_lost_update(tmp_path: Path) -> None:
    first, second = _store(tmp_path), _store(tmp_path)
    run = first.start(_SELECTION, replaces=None).finish(
        GeneratedReview(_DRAFT, "provider/model", None)
    )
    first.save(run)
    preview = second.get_draft(run.id)
    assert preview == run
    target = DraftTarget(run.id, 1)
    edited = run.edit_draft(_DRAFT.replace_summary("winner"))
    first.save_edit(target, edited)
    with pytest.raises(DraftConflict):
        second.save_edit(target, preview.edit_draft(_DRAFT.replace_summary("stale")))
    assert second.get_draft(run.id) == edited
    assert edited.draft_version == 2


@pytest.mark.parametrize("change", ["verdict", "severity", "location", "order"])
def test_store_rejects_edits_to_reviewer_metadata(tmp_path: Path, change: str) -> None:
    store = _store(tmp_path)
    run = _saved(store)
    first, second = _DRAFT.findings
    match change:
        case "verdict":
            altered = replace(_DRAFT, verdict="approved")
        case "severity":
            altered = replace(
                _DRAFT, findings=(replace(first, severity="medium"), second)
            )
        case "location":
            altered = replace(_DRAFT, findings=(replace(first, location=None), second))
        case _:
            altered = replace(_DRAFT, findings=(second, first))
    proposed = replace(run, draft=altered, draft_version=2)

    with pytest.raises(DraftValidationError, match="Edits must preserve"):
        store.save_edit(run.draft_target, proposed)
    assert store.get_draft(run.id) == run


def test_failed_draft_write_preserves_version_and_feedback(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = store.start(_SELECTION, replaces=None).finish(
        GeneratedReview(_DRAFT, "provider/model", None)
    )
    store.save(run)
    connection = sqlite3.connect(_path(tmp_path))
    try:
        connection.execute(
            "CREATE TRIGGER fail_edit AFTER UPDATE ON review_runs "
            "BEGIN SELECT RAISE(FAIL, 'boom'); END"
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(ReviewStoreError, match="Could not save"):
        store.save_edit(DraftTarget(run.id, 1), run.edit_draft(_DRAFT.drop_finding(1)))
    assert _store(tmp_path).get_draft(run.id) == run


def _saved(store: ReviewRunStore, selection: ReviewSelection = _SELECTION) -> ReviewRun:
    run = store.start(selection, replaces=None).finish(
        GeneratedReview(_DRAFT, "provider/model", None)
    )
    store.save(run)
    return run


def _new_head() -> ReviewSelection:
    return replace(_SELECTION, revision=replace(_SELECTION.revision, head="d" * 40))


def test_simultaneous_finishes_record_one_completion(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _saved(store)
    second = _saved(store, _new_head())
    edited = second.edit_draft(
        _DRAFT.replace_summary("Second comparison's edited text")
    )
    store.save_edit(DraftTarget(second.id, 1), edited)
    barrier = Barrier(2)

    def finish(run: ReviewRun) -> bool:
        barrier.wait(timeout=5)
        writer = SqliteReviewRunStore(
            _path(tmp_path), now=lambda: datetime.fromtimestamp(run.id, UTC)
        )
        try:
            assert writer.complete(run.complete_locally()) == run.complete_locally()
        except ReviewAlreadyCompleted:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(finish, [first, edited]))
    assert sum(results) == 1
    winning = first if results[0] else edited
    completion = winning.complete_locally()
    assert completion.source == DraftTarget(winning.id, winning.draft_version)
    assert completion.selection == winning.selection
    assert completion.final_draft == winning.draft
    assert completion.publication_status == "not_published"
    assert store.completed(_SELECTION.identity) == completion
    assert _store(tmp_path).complete(completion) == completion
    with sqlite3.connect(_path(tmp_path)) as connection:
        assert connection.execute(
            "SELECT recorded_at FROM review_completions"
        ).fetchall() == [(winning.id,)]

    another = _saved(store, replace(_SELECTION, account="another"))
    assert store.complete(another.complete_locally()) == another.complete_locally()
    assert store.completed(_SELECTION.identity) == completion


@pytest.mark.parametrize(
    "finish_first", [True, False], ids=["finish-wins", "edit-wins"]
)
def test_finish_and_edit_cannot_commit_against_stale_draft(
    tmp_path: Path, finish_first: bool
) -> None:
    editor, finisher = _store(tmp_path), _store(tmp_path)
    run = _saved(editor)
    preview = finisher.get_draft(run.id)
    assert preview == run
    edited = run.edit_draft(_DRAFT.drop_finding(1))
    target = DraftTarget(run.id, 1)
    completion = preview.complete_locally()

    if finish_first:
        assert finisher.complete(completion) == completion
        with pytest.raises(ReviewAlreadyCompleted):
            editor.save_edit(target, edited)
        assert editor.get_draft(run.id) == run
    else:
        editor.save_edit(target, edited)
        with pytest.raises(DraftConflict):
            finisher.complete(completion)
        assert finisher.completed(_SELECTION.identity) is None
        assert finisher.get_draft(run.id) == edited


def test_completion_prevents_new_run_admission(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _saved(store)
    store.complete(run.complete_locally())
    with pytest.raises(ReviewAlreadyCompleted):
        _store(tmp_path).start(_new_head(), replaces=None)
    assert store.latest(_new_head().key) is None


def test_failed_completion_leaves_draft_editable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _saved(store)
    connection = sqlite3.connect(_path(tmp_path))
    try:
        connection.execute(
            "CREATE TRIGGER fail_completion AFTER INSERT ON review_completions "
            "BEGIN SELECT RAISE(FAIL, 'boom'); END"
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(ReviewStoreError, match="Could not save"):
        store.complete(run.complete_locally())
    assert _store(tmp_path).completed(_SELECTION.identity) is None
    updated = run.edit_draft(_DRAFT.drop_finding(2))
    store.save_edit(DraftTarget(run.id, 1), updated)
    assert store.get_draft(run.id) == updated


def test_upgrades_edited_v2_draft(tmp_path: Path) -> None:
    _legacy_database(tmp_path)
    connection = sqlite3.connect(_path(tmp_path))
    try:
        connection.executescript("""
            ALTER TABLE review_runs ADD COLUMN draft_version INTEGER
                NOT NULL DEFAULT 1 CHECK (draft_version >= 1);
            UPDATE review_runs SET draft_version = 2,
                draft = '{"verdict":"approved","summary":"edited before upgrade","findings":[]}';
            PRAGMA user_version = 2;
            """)
    finally:
        connection.close()
    store = _store(tmp_path)
    saved = store.get_draft(1)
    assert saved == ReviewRun(
        1,
        _SELECTION,
        "draft_ready",
        ReviewDraft("approved", "edited before upgrade", ()),
        None,
        2,
    )
    assert store.completed(_SELECTION.identity) is None
    assert store.complete(saved.complete_locally()).source == DraftTarget(1, 2)


def test_run_and_draft_commit_together(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = store.start(_SELECTION, replaces=None)
    connection = sqlite3.connect(_path(tmp_path))
    try:
        connection.execute(
            "CREATE TRIGGER fail_update BEFORE UPDATE ON review_runs "
            "BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ReviewStoreError):
        store.save(run.finish(GeneratedReview(_DRAFT, "provider/model", None)))

    stored = store.latest(_SELECTION.key)
    assert stored is not None
    assert stored.status == "running"
    assert stored.draft is None

    connection = sqlite3.connect(_path(tmp_path))
    try:
        connection.execute("DROP TRIGGER fail_update")
        connection.commit()
    finally:
        connection.close()
    store.save(run.finish(GeneratedReview(_DRAFT, "provider/model", None)))
    stored = store.latest(_SELECTION.key)
    assert stored is not None
    assert stored.status == "draft_ready"
    assert stored.draft == _DRAFT


def test_rejects_late_finish_for_superseded_attempt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.start(_SELECTION, replaces=None)
    first = first.fail("boom", "failed")
    store.save(first)
    second = store.start(_SELECTION, replaces=first.id)
    late = ReviewRun(first.id, first.selection, "draft_ready", _DRAFT, None)

    with pytest.raises(ReviewStoreError, match="superseded"):
        store.save(late)

    stored = store.latest(_SELECTION.key)
    assert stored is not None
    assert stored.id == second.id
    assert stored.status == "running"
    assert stored.draft is None


def test_rejects_unknown_schema_version(tmp_path: Path) -> None:
    path = _path(tmp_path)
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA user_version = 99")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ReviewStoreError, match="unsupported schema version"):
        _store(tmp_path).latest(_SELECTION.key)

    connection = sqlite3.connect(path)
    try:
        version = connection.execute("PRAGMA user_version").fetchone()
    finally:
        connection.close()
    assert version == (99,)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_creates_private_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.start(_SELECTION, replaces=None)

    assert stat.S_IMODE(_path(tmp_path).parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(_path(tmp_path).stat().st_mode) == 0o600
