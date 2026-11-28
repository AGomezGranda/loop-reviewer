"""Define the versioned inbox persistence contract."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from loop_reviewer.inbox.model import (
    DiscoveredPullRequest,
    InboxSettings,
    JiraProjectKey,
    WatchedRepository,
    WatchedRepositoryKey,
)
from loop_reviewer.review.model import ReviewIdentity
from loop_reviewer.review.pull_request import PullRequestRef


class InboxStoreError(Exception):
    pass


class InboxSettingsChanged(Exception):
    def __init__(self) -> None:
        super().__init__("Inbox settings changed. Refresh this page before saving.")


@dataclass(frozen=True)
class ScanTicket:
    scan_version: int
    repository_key: WatchedRepositoryKey
    sequence: int


@dataclass(frozen=True)
class SuccessfulScan:
    checked_at: datetime
    eligible_rows: tuple[DiscoveredPullRequest, ...]


@dataclass(frozen=True)
class FailedScan:
    checked_at: datetime
    error: str
    may_show_cached_rows: bool


type ScanResult = SuccessfulScan | FailedScan


@dataclass(frozen=True)
class RepositorySnapshot:
    repository: WatchedRepository
    latest: ScanResult | None
    last_success: SuccessfulScan | None
    deferred_numbers: frozenset[int] = frozenset()


@dataclass(frozen=True)
class InboxSnapshot:
    settings: InboxSettings
    repositories: tuple[RepositorySnapshot, ...]


@dataclass(frozen=True)
class InboxRowTarget:
    settings_version: int
    repository_key: WatchedRepositoryKey
    ref: PullRequestRef

    @classmethod
    def from_cached_row(
        cls,
        settings: InboxSettings,
        key: WatchedRepositoryKey,
        row: DiscoveredPullRequest,
    ) -> InboxRowTarget | None:
        if settings.excludes(row.title):
            return None
        return cls(settings.version, key, row.ref)

    @property
    def identity(self) -> ReviewIdentity:
        return ReviewIdentity.for_pull_request(self.ref, self.repository_key.account)


class InboxStore(Protocol):
    def settings(self) -> InboxSettings | None: ...

    def replace_settings(
        self,
        projects: tuple[JiraProjectKey, ...],
        repositories: tuple[WatchedRepository, ...],
        *,
        excluded_title_terms: tuple[str, ...] = (),
        expected_version: int | None = None,
    ) -> InboxSettings:
        """Replace settings atomically, clearing saved scan results when project keys change.

        Use InboxSettings.reconfigured to preserve scan tickets when the project
        keys and verified repositories are unchanged, including exclusion-only edits.
        Keep deferrals for retained repository identities and remove them for dropped identities.
        """
        ...

    def begin_scan(
        self, scan_version: int, repository_key: WatchedRepositoryKey
    ) -> ScanTicket | None: ...

    def record_scan(self, ticket: ScanTicket, result: ScanResult) -> bool: ...

    def snapshot(self, settings_version: int) -> InboxSnapshot | None: ...

    def resolve_row(self, url: PullRequestRef) -> InboxRowTarget | None:
        """Resolve an unambiguous cached URL, then apply the action rule in from_cached_row."""
        ...

    def set_deferred(self, target: InboxRowTarget, deferred: bool) -> bool:
        """Write only if the target still resolves under the same action rule."""
        ...
