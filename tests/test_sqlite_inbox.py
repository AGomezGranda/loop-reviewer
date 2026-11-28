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
from loop_reviewer.inbox.store import FailedScan, InboxStoreError, SuccessfulScan
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
    ticket = store.begin_scan(settings.scan_version, _REPO.key)
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
        connection.execute(
            "ALTER TABLE inbox_settings DROP COLUMN excluded_title_terms"
        )
        connection.execute(
            "UPDATE inbox_repositories SET eligible_rows = json_remove(eligible_rows, '$[0][7]')"
        )
        connection.execute("ALTER TABLE inbox_settings DROP COLUMN scan_version")
        connection.execute("PRAGMA user_version = 7")

    snapshot = SqliteInboxStore(path).snapshot(settings.version)
    assert snapshot is not None and snapshot.settings == settings
    assert snapshot.repositories[0].last_success == success


def test_schema_11_preserves_exclusions_and_initializes_scan_version(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state" / "reviews.sqlite3"
    store = SqliteInboxStore(path)
    first = store.replace_settings((JiraProjectKey("ABC"),), (_REPO,))
    ticket = store.begin_scan(first.scan_version, _REPO.key)
    success = SuccessfulScan(_NOW, (_ROW,))
    assert ticket and store.record_scan(ticket, success)
    saved = store.replace_settings(
        first.projects, first.repositories, excluded_title_terms=("ignore",)
    )
    assert saved.version > first.version
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("ALTER TABLE inbox_settings DROP COLUMN scan_version")
        connection.execute("PRAGMA user_version = 11")
    snapshot = SqliteInboxStore(path).snapshot(saved.version)
    assert snapshot is not None
    assert snapshot.settings == replace(saved, scan_version=saved.version)
    assert snapshot.repositories[0].latest == success


@pytest.mark.parametrize(
    "rows", [(), (_ROW,)], ids=["empty-success", "eligible-success"]
)
def test_failure_preserves_last_success_and_exposes_latest_attempt(
    tmp_path: Path, rows: tuple[DiscoveredPullRequest, ...]
) -> None:
    store = SqliteInboxStore(tmp_path / "state" / "reviews.sqlite3")
    settings = store.replace_settings((JiraProjectKey("ABC"),), (_REPO,))
    first = store.begin_scan(settings.scan_version, _REPO.key)
    assert first is not None
    success = SuccessfulScan(_NOW, rows)
    assert store.record_scan(first, success)
    second = store.begin_scan(settings.scan_version, _REPO.key)
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
    slow = first.begin_scan(settings.scan_version, _REPO.key)
    fast = second.begin_scan(settings.scan_version, _REPO.key)
    assert slow is not None and fast is not None
    assert not first.record_scan(slow, SuccessfulScan(_NOW, (_ROW,)))
    pending = first.snapshot(settings.version)
    assert pending is not None and pending.repositories[0].latest is None
    success = SuccessfulScan(_NOW, ())
    assert second.record_scan(fast, success)
    assert not first.record_scan(slow, SuccessfulScan(_NOW, (_ROW,)))
    snapshot = first.snapshot(settings.version)
    assert snapshot is not None and snapshot.repositories[0].latest == success


def test_prefix_change_invalidates_tickets_and_clears_cache(
    tmp_path: Path,
) -> None:
    store = SqliteInboxStore(tmp_path / "state" / "reviews.sqlite3")
    old = store.replace_settings((JiraProjectKey("ABC"),), (_REPO,))
    start = store.begin_scan(old.scan_version, _REPO.key)
    assert start and store.record_scan(start, SuccessfulScan(_NOW, (_ROW,)))
    pending = store.begin_scan(old.scan_version, _REPO.key)
    assert pending is not None
    new = store.replace_settings((JiraProjectKey("OTHER"),), (_REPO,))
    assert store.snapshot(old.version) is None
    assert not store.record_scan(pending, SuccessfulScan(_NOW, (_ROW,)))
    snapshot = store.snapshot(new.version)
    assert snapshot is not None and snapshot.repositories[0].last_success is None


@pytest.mark.parametrize("raw", ['"fix"', "[7]"], ids=["string", "non-string-entry"])
def test_corrupt_title_exclusions_are_reported_as_store_error(
    tmp_path: Path, raw: str
) -> None:
    path = tmp_path / "state" / "reviews.sqlite3"
    store = SqliteInboxStore(path)
    store.replace_settings((JiraProjectKey("ABC"),), (_REPO,))
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE inbox_settings SET excluded_title_terms = ?", (raw,))
    with pytest.raises(InboxStoreError, match="Could not read local inbox state"):
        store.settings()


@pytest.mark.parametrize(
    "opened_at", ["not a timestamp", 1.5], ids=["string", "fractional"]
)
def test_invalid_cached_opening_time_is_reported_as_store_error(
    tmp_path: Path, opened_at: str | float
) -> None:
    path = tmp_path / "state" / "reviews.sqlite3"
    store = SqliteInboxStore(path)
    settings = store.replace_settings((JiraProjectKey("ABC"),), (_REPO,))
    ticket = store.begin_scan(settings.scan_version, _REPO.key)
    assert ticket and store.record_scan(ticket, SuccessfulScan(_NOW, (_ROW,)))
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE inbox_repositories SET eligible_rows = json_set(eligible_rows, '$[0][7]', ?)",
            (opened_at,),
        )

    with pytest.raises(InboxStoreError, match="Could not read local inbox state"):
        store.snapshot(settings.version)


def test_opening_time_outside_browser_range_round_trips(tmp_path: Path) -> None:
    store = SqliteInboxStore(tmp_path / "reviews.sqlite3")
    settings = store.replace_settings((JiraProjectKey("ABC"),), (_REPO,))
    ticket = store.begin_scan(settings.scan_version, _REPO.key)
    scan = SuccessfulScan(_NOW, (replace(_ROW, opened_at=8_640_000_000_001),))
    assert ticket and store.record_scan(ticket, scan)
    snapshot = store.snapshot(settings.version)
    assert snapshot is not None
    assert snapshot.repositories[0].last_success == scan


def test_deferral_survives_empty_scan_rename_and_prefix_change(tmp_path: Path) -> None:
    path = tmp_path / "state" / "reviews.sqlite3"
    store = SqliteInboxStore(path)
    settings = store.replace_settings((JiraProjectKey("ABC"),), (_REPO,))
    start = store.begin_scan(settings.scan_version, _REPO.key)
    assert start and store.record_scan(start, SuccessfulScan(_NOW, (_ROW,)))
    target = store.resolve_row(_ROW.ref)
    assert target and store.set_deferred(target, True)
    start = store.begin_scan(settings.scan_version, _REPO.key)
    assert start and store.record_scan(start, SuccessfulScan(_NOW, ()))
    assert not store.set_deferred(target, False)
    store = SqliteInboxStore(path)
    renamed = replace(_REPO, address=RepositoryAddress("github.com", "Owner/Renamed"))
    updated = store.replace_settings((JiraProjectKey("NEW"),), (renamed,))
    assert not store.set_deferred(target, False)
    row = replace(_ROW, ref=replace(_ROW.ref, repo="Renamed"), title="[NEW-3] Fix")
    start = store.begin_scan(updated.scan_version, renamed.key)
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


@pytest.mark.parametrize(
    "excluded",
    [(), ("Fix",), ("Replacement",)],
    ids=["both-visible", "old-hidden", "new-hidden"],
)
def test_title_exclusion_does_not_disambiguate_cached_urls(
    tmp_path: Path, excluded: tuple[str, ...]
) -> None:
    store = SqliteInboxStore(tmp_path / "reviews.sqlite3")
    projects = (JiraProjectKey("ABC"),)
    original = store.replace_settings(projects, (_REPO,))
    start = store.begin_scan(original.scan_version, _REPO.key)
    assert start and store.record_scan(start, SuccessfulScan(_NOW, (_ROW,)))

    renamed = replace(_REPO, address=RepositoryAddress("github.com", "Owner/Renamed"))
    replacement = replace(_REPO, repository_id=33)
    latest = store.replace_settings(
        projects, (renamed, replacement), excluded_title_terms=excluded
    )
    url_ref = replace(_ROW.ref, repository_id=None)
    replacement_row = replace(
        _ROW,
        ref=replace(url_ref, repository_id=replacement.repository_id),
        title="[ABC-2] Replacement",
    )
    start = store.begin_scan(latest.scan_version, replacement.key)
    assert start and store.record_scan(start, SuccessfulScan(_NOW, (replacement_row,)))
    assert store.resolve_row(url_ref) is None


def test_re_setup_invalidates_row_target_even_when_row_is_unchanged(
    tmp_path: Path,
) -> None:
    store = SqliteInboxStore(tmp_path / "state" / "reviews.sqlite3")
    settings = store.replace_settings((JiraProjectKey("ABC"),), (_REPO,))
    start = store.begin_scan(settings.scan_version, _REPO.key)
    assert start and store.record_scan(start, SuccessfulScan(_NOW, (_ROW,)))
    target = store.resolve_row(_ROW.ref)
    assert target is not None
    store.replace_settings(settings.projects, settings.repositories)
    assert not store.set_deferred(target, True)
    current = store.resolve_row(_ROW.ref)
    assert current and store.set_deferred(current, True)
