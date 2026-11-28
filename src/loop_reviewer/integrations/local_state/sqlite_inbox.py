"""Persist versioned inbox settings and all-or-nothing repository snapshots."""

import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import cast

from loop_reviewer.inbox.model import (
    DiscoveredPullRequest,
    InboxSettings,
    JiraProjectKey,
    RepositoryAddress,
    WatchedRepository,
    WatchedRepositoryKey,
)
from loop_reviewer.inbox.store import (
    FailedScan,
    InboxRowTarget,
    InboxSettingsChanged,
    InboxSnapshot,
    InboxStoreError,
    RepositorySnapshot,
    ScanResult,
    ScanTicket,
    SuccessfulScan,
)
from loop_reviewer.integrations.local_state.database import (
    LocalDatabase,
    UnsupportedSchemaVersion,
)
from loop_reviewer.review.pull_request import PullRequestRef

_KEY = "host = ? AND repository_id = ? AND account = ?"
_SCAN_VERSION = "EXISTS (SELECT 1 FROM inbox_settings WHERE scan_version = ?)"


class SqliteInboxStore:
    def __init__(self, path: Path) -> None:
        self._database = LocalDatabase(path)

    def settings(self) -> InboxSettings | None:
        with self._transaction() as connection:
            return _settings(connection)

    def replace_settings(
        self,
        projects: tuple[JiraProjectKey, ...],
        repositories: tuple[WatchedRepository, ...],
        *,
        excluded_title_terms: tuple[str, ...] = (),
        expected_version: int | None = None,
    ) -> InboxSettings:
        with self._transaction(write=True) as connection:
            old = _settings(connection)
            current_version = old.version if old else 0
            if expected_version is not None and expected_version != current_version:
                raise InboxSettingsChanged
            settings = (
                InboxSettings(projects, repositories, 1, 1, excluded_title_terms)
                if old is None
                else old.reconfigured(projects, repositories, excluded_title_terms)
            )
            connection.execute(
                """INSERT INTO inbox_settings (singleton, projects, version, excluded_title_terms, scan_version) VALUES (1, ?, ?, ?, ?)
                ON CONFLICT (singleton) DO UPDATE SET projects = excluded.projects, version = excluded.version,
                excluded_title_terms = excluded.excluded_title_terms, scan_version = excluded.scan_version""",
                (
                    ",".join(project.value for project in projects),
                    settings.version,
                    json.dumps(excluded_title_terms),
                    settings.scan_version,
                ),
            )
            retained = {repo.key for repo in repositories}
            if old is not None:
                for repo in old.repositories:
                    if repo.key not in retained:
                        parts = _parts(repo.key)
                        connection.execute(
                            f"DELETE FROM inbox_deferrals WHERE {_KEY}", parts
                        )
                        connection.execute(
                            f"DELETE FROM inbox_repositories WHERE {_KEY}", parts
                        )
                if set(old.projects) != set(projects):
                    connection.execute("""UPDATE inbox_repositories SET outcome = NULL, checked_at = NULL,
                        error = NULL, show_cached = 0, success_at = NULL, eligible_rows = NULL""")
            connection.executemany(
                """INSERT INTO inbox_repositories (host, repository_id, account, slug, position)
                VALUES (?, ?, ?, ?, ?) ON CONFLICT (host, repository_id, account)
                DO UPDATE SET slug = excluded.slug, position = excluded.position""",
                (
                    (*_parts(repo.key), repo.address.slug, index)
                    for index, repo in enumerate(repositories)
                ),
            )
        return settings

    def begin_scan(
        self, scan_version: int, repository_key: WatchedRepositoryKey
    ) -> ScanTicket | None:
        with self._transaction(write=True) as connection:
            row = connection.execute(
                f"""UPDATE inbox_repositories SET sequence = sequence + 1, outcome = NULL,
                checked_at = NULL, error = NULL, show_cached = 0 WHERE {_KEY} AND {_SCAN_VERSION} RETURNING sequence""",
                (*_parts(repository_key), scan_version),
            ).fetchone()
            return (
                None
                if row is None
                else ScanTicket(scan_version, repository_key, row[0])
            )

    def record_scan(self, ticket: ScanTicket, result: ScanResult) -> bool:
        with self._transaction(write=True) as connection:
            if isinstance(result, SuccessfulScan):
                outcome = (
                    "outcome = ?, checked_at = ?, success_at = ?, eligible_rows = ?"
                )
                checked = result.checked_at.isoformat()
                values = ("success", checked, checked, _encode(result.eligible_rows))
            else:
                outcome = "outcome = ?, checked_at = ?, error = ?, show_cached = ?"
                values = (
                    "failed",
                    result.checked_at.isoformat(),
                    result.error,
                    int(result.may_show_cached_rows),
                )
            cursor = connection.execute(
                f"""UPDATE inbox_repositories SET {outcome}
                WHERE {_KEY} AND {_SCAN_VERSION} AND sequence = ? AND outcome IS NULL""",
                (
                    *values,
                    *_parts(ticket.repository_key),
                    ticket.scan_version,
                    ticket.sequence,
                ),
            )
            return cursor.rowcount == 1

    def snapshot(self, settings_version: int) -> InboxSnapshot | None:
        with self._transaction() as connection:
            settings = _settings(connection)
            if settings is None or settings.version != settings_version:
                return None
            return InboxSnapshot(
                settings,
                tuple(
                    _repository_snapshot(connection, repo)
                    for repo in settings.repositories
                ),
            )

    def resolve_row(self, url: PullRequestRef) -> InboxRowTarget | None:
        with self._transaction() as connection:
            return _resolve_row(connection, url)

    def set_deferred(self, target: InboxRowTarget, deferred: bool) -> bool:
        with self._transaction(write=True) as connection:
            if _resolve_row(connection, target.ref) != target:
                return False
            parts = (*_parts(target.repository_key), target.ref.number)
            if deferred:
                connection.execute(
                    "INSERT INTO inbox_deferrals VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                    parts,
                )
            else:
                connection.execute(
                    f"DELETE FROM inbox_deferrals WHERE {_KEY} AND pr_number = ?", parts
                )
            return True

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Generator[sqlite3.Connection]:
        try:
            with (
                self._database.write() if write else self._database.read()
            ) as connection:
                yield connection
        except UnsupportedSchemaVersion:
            raise InboxStoreError(
                "The inbox database uses an unsupported schema version. Use a compatible release."
            ) from None
        except sqlite3.Error, OSError, ValueError, TypeError, RecursionError:
            action = "save" if write else "read"
            raise InboxStoreError(
                f"Could not {action} local inbox state. Check the database, state directory permissions and available space; retry if another operation is running."
            ) from None


def _settings(connection: sqlite3.Connection) -> InboxSettings | None:
    row = connection.execute(
        "SELECT projects, version, excluded_title_terms, scan_version FROM inbox_settings WHERE singleton = 1"
    ).fetchone()
    if row is None:
        return None
    repositories = tuple(
        WatchedRepository(RepositoryAddress(host, slug), repository_id, account)
        for host, slug, repository_id, account in connection.execute(
            "SELECT host, slug, repository_id, account FROM inbox_repositories ORDER BY position"
        )
    )
    projects = tuple(JiraProjectKey(value) for value in row[0].split(","))
    terms: object = json.loads(row[2])
    if not isinstance(terms, list) or not all(isinstance(term, str) for term in terms):
        raise ValueError("Invalid cached title exclusions")
    return InboxSettings(
        projects, repositories, row[1], row[3], tuple(cast("list[str]", terms))
    )


def _parts(key: WatchedRepositoryKey) -> tuple[str, int, str]:
    return key.host, key.repository_id, key.account


def _repository_snapshot(
    connection: sqlite3.Connection, repo: WatchedRepository
) -> RepositorySnapshot:
    row = connection.execute(
        f"""SELECT outcome, checked_at, error, show_cached, success_at, eligible_rows
        FROM inbox_repositories WHERE {_KEY}""",
        _parts(repo.key),
    ).fetchone()
    if row is None:
        raise ValueError
    outcome, checked_at, error, show_cached, success_at, eligible_rows = row
    success = (
        None
        if success_at is None
        else SuccessfulScan(
            datetime.fromisoformat(success_at), _decode(repo, eligible_rows)
        )
    )
    latest: ScanResult | None = None
    if outcome == "success":
        if success is None:
            raise ValueError
        latest = success
    elif outcome == "failed":
        latest = FailedScan(
            datetime.fromisoformat(checked_at), error, bool(show_cached)
        )
    deferred = frozenset(
        number
        for (number,) in connection.execute(
            f"SELECT pr_number FROM inbox_deferrals WHERE {_KEY}", _parts(repo.key)
        )
    )
    return RepositorySnapshot(repo, latest, success, deferred)


def _resolve_row(
    connection: sqlite3.Connection, ref: PullRequestRef
) -> InboxRowTarget | None:
    settings = _settings(connection)
    if settings is None:
        return None
    match: tuple[WatchedRepositoryKey, DiscoveredPullRequest] | None = None
    for slug, repo_id, account, raw in connection.execute(
        "SELECT slug, repository_id, account, eligible_rows FROM inbox_repositories WHERE host = ? AND eligible_rows IS NOT NULL",
        (ref.host,),
    ):
        repo = WatchedRepository(RepositoryAddress(ref.host, slug), repo_id, account)
        for row in _decode(repo, raw):
            canonical = row.ref
            if (
                canonical.number == ref.number
                and canonical.owner.casefold() == ref.owner.casefold()
                and canonical.repo.casefold() == ref.repo.casefold()
            ):
                if match is not None:
                    return None  # A cached slug can outlive a rename and refer to two stable keys.
                match = repo.key, row
    if match is None:
        return None
    key, row = match
    return InboxRowTarget.from_cached_row(settings, key, row)


def _opened_at(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise ValueError("Invalid PR opening time")
    return value


def _encode(rows: tuple[DiscoveredPullRequest, ...]) -> str:
    return json.dumps(
        [
            [
                f"{row.ref.owner}/{row.ref.repo}",
                row.ref.number,
                row.title,
                row.author,
                row.state,
                row.draft,
                row.requested_users,
                _opened_at(row.opened_at),
            ]
            for row in rows
        ],
        separators=(",", ":"),
    )


def _decode(repo: WatchedRepository, raw: str) -> tuple[DiscoveredPullRequest, ...]:
    data: object = json.loads(raw)
    if not isinstance(data, list):
        raise TypeError
    rows: list[DiscoveredPullRequest] = []
    for item in cast("list[object]", data):
        if not isinstance(item, list) or len(item) not in (7, 8):
            raise ValueError
        opened_at = _opened_at(item[7]) if len(item) == 8 else None
        slug, number, title, author, state, draft, users = cast(
            "list[object]", item[:7]
        )
        if (
            not isinstance(slug, str)
            or type(number) is not int
            or not isinstance(title, str)
            or not isinstance(author, str)
            or not isinstance(state, str)
            or type(draft) is not bool
            or not isinstance(users, list)
            or any(not isinstance(user, str) for user in cast("list[object]", users))
        ):
            raise ValueError
        ref = PullRequestRef.from_repository(
            repo.address.host, slug, number, repository_id=repo.repository_id
        )
        rows.append(
            DiscoveredPullRequest(
                ref,
                title,
                author,
                state,
                draft,
                tuple(cast("list[str]", users)),
                opened_at,
            )
        )
    return tuple(rows)
