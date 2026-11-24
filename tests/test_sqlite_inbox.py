import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from loop_reviewer.inbox.model import (
    DiscoveredPullRequest,
    JiraProjectKey,
    RepositoryAddress,
    WatchedRepository,
)
from loop_reviewer.inbox.store import FailedScan, SuccessfulScan
from loop_reviewer.integrations.local_state.sqlite_inbox import SqliteInboxStore
from loop_reviewer.review.pull_request import PullRequestRef

_NOW = datetime(2026, 11, 21, tzinfo=UTC)
_REPO = WatchedRepository(RepositoryAddress("github.com", "Owner/Repo"), 22, "alice")
_ROW = DiscoveredPullRequest(
    PullRequestRef("github.com", "Owner", "Repo", 1, 22),
    "[ABC-1] Fix",
    "bob",
    "open",
    False,
    (),
)


def test_schema_7_setup_keeps_its_project_repository_and_scan(tmp_path: Path) -> None:
    path = tmp_path / "state" / "reviews.sqlite3"
    store = SqliteInboxStore(path)
    settings = store.replace_settings((JiraProjectKey("ABC"),), (_REPO,))
    ticket = store.begin_scan(settings.version, _REPO.key)
    assert ticket is not None
    success = SuccessfulScan(_NOW, (_ROW,))
    assert store.record_scan(ticket, success)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("ALTER TABLE review_runs DROP COLUMN reviewer_report")
        for table in ("review_runs", "review_completions"):
            connection.execute(f"ALTER TABLE {table} DROP COLUMN estimated_cost_usd")
            connection.execute(f"ALTER TABLE {table} DROP COLUMN generation_model")
        connection.execute(
            "ALTER TABLE inbox_settings RENAME COLUMN projects TO project"
        )
        connection.execute("PRAGMA user_version = 7")

    snapshot = SqliteInboxStore(path).snapshot(settings.version)
    assert snapshot is not None and snapshot.settings == settings
    assert snapshot.repositories[0].last_success == success


@pytest.mark.parametrize(
    "rows", [(), (_ROW,)], ids=["empty-success", "eligible-success"]
)
def test_failure_preserves_last_success_and_exposes_latest_attempt(
    tmp_path: Path, rows: tuple[DiscoveredPullRequest, ...]
) -> None:
    store = SqliteInboxStore(tmp_path / "state" / "reviews.sqlite3")
    settings = store.replace_settings((JiraProjectKey("ABC"),), (_REPO,))
    first = store.begin_scan(settings.version, _REPO.key)
    assert first is not None
    success = SuccessfulScan(_NOW, rows)
    assert store.record_scan(first, success)
    second = store.begin_scan(settings.version, _REPO.key)
    assert second is not None
    failure = FailedScan(_NOW + timedelta(seconds=1), "Try again", True)
    assert store.record_scan(second, failure)
    snapshot = store.snapshot(settings.version)
    assert snapshot is not None
    assert snapshot.repositories[0].latest == failure
    assert snapshot.repositories[0].last_success == success


def test_slow_scan_cannot_overwrite_newer_in_flight_or_finished_outcome(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state" / "reviews.sqlite3"
    first, second = SqliteInboxStore(path), SqliteInboxStore(path)
    settings = first.replace_settings((JiraProjectKey("ABC"),), (_REPO,))
    slow = first.begin_scan(settings.version, _REPO.key)
    fast = second.begin_scan(settings.version, _REPO.key)
    assert slow is not None and fast is not None
    assert not first.record_scan(slow, SuccessfulScan(_NOW, (_ROW,)))
    pending = first.snapshot(settings.version)
    assert pending is not None and pending.repositories[0].latest is None
    success = SuccessfulScan(_NOW, ())
    assert second.record_scan(fast, success)
    assert not first.record_scan(slow, SuccessfulScan(_NOW, (_ROW,)))
    snapshot = first.snapshot(settings.version)
    assert snapshot is not None and snapshot.repositories[0].latest == success


def test_settings_version_invalidates_tickets_and_prefix_change_clears_cache(
    tmp_path: Path,
) -> None:
    store = SqliteInboxStore(tmp_path / "state" / "reviews.sqlite3")
    old = store.replace_settings((JiraProjectKey("ABC"),), (_REPO,))
    start = store.begin_scan(old.version, _REPO.key)
    assert start and store.record_scan(start, SuccessfulScan(_NOW, (_ROW,)))
    pending = store.begin_scan(old.version, _REPO.key)
    assert pending is not None
    new = store.replace_settings((JiraProjectKey("OTHER"),), (_REPO,))
    assert store.snapshot(old.version) is None
    assert not store.record_scan(pending, SuccessfulScan(_NOW, (_ROW,)))
    snapshot = store.snapshot(new.version)
    assert snapshot is not None and snapshot.repositories[0].last_success is None


def test_deferral_survives_empty_scan_rename_and_prefix_change(tmp_path: Path) -> None:
    path = tmp_path / "state" / "reviews.sqlite3"
    store = SqliteInboxStore(path)
    settings = store.replace_settings((JiraProjectKey("ABC"),), (_REPO,))
    start = store.begin_scan(settings.version, _REPO.key)
    assert start and store.record_scan(start, SuccessfulScan(_NOW, (_ROW,)))
    target = store.resolve_row(_ROW.ref)
    assert target and store.set_deferred(target, True)
    start = store.begin_scan(settings.version, _REPO.key)
    assert start and store.record_scan(start, SuccessfulScan(_NOW, ()))
    assert not store.set_deferred(target, False)
    store = SqliteInboxStore(path)
    renamed = replace(_REPO, address=RepositoryAddress("github.com", "Owner/Renamed"))
    updated = store.replace_settings((JiraProjectKey("NEW"),), (renamed,))
    assert not store.set_deferred(target, False)
    row = replace(_ROW, ref=replace(_ROW.ref, repo="Renamed"), title="[NEW-3] Fix")
    start = store.begin_scan(updated.version, renamed.key)
    assert start and store.record_scan(start, SuccessfulScan(_NOW, (row,)))
    snapshot = store.snapshot(updated.version)
    assert snapshot is not None and snapshot.repositories[0].deferred_numbers == {
        row.ref.number
    }
    other = replace(renamed, repository_id=88)
    store.replace_settings(updated.projects, (other,))
    restored = store.replace_settings(updated.projects, (renamed,))
    snapshot = store.snapshot(restored.version)
    assert snapshot is not None and not snapshot.repositories[0].deferred_numbers


def test_re_setup_invalidates_row_target_even_when_row_is_unchanged(
    tmp_path: Path,
) -> None:
    store = SqliteInboxStore(tmp_path / "state" / "reviews.sqlite3")
    settings = store.replace_settings((JiraProjectKey("ABC"),), (_REPO,))
    start = store.begin_scan(settings.version, _REPO.key)
    assert start and store.record_scan(start, SuccessfulScan(_NOW, (_ROW,)))
    target = store.resolve_row(_ROW.ref)
    assert target is not None
    store.replace_settings(settings.projects, settings.repositories)
    assert not store.set_deferred(target, True)
    current = store.resolve_row(_ROW.ref)
    assert current and store.set_deferred(current, True)
