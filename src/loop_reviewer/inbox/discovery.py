"""Verify inbox settings, refresh each repository and project visible work."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Protocol

from loop_reviewer.inbox.configuration import InboxInputs, saved_inbox_inputs
from loop_reviewer.inbox.model import (
    DiscoveredPullRequest,
    InboxReport,
    InboxRow,
    InboxSettings,
    JiraProjectKey,
    RepositoryAddress,
    RepositoryScanStatus,
    WatchedRepository,
)
from loop_reviewer.inbox.store import (
    FailedScan,
    InboxSnapshot,
    InboxStore,
    InboxStoreError,
    ScanResult,
    SuccessfulScan,
)
from loop_reviewer.review.model import ReviewIdentity
from loop_reviewer.review.store import ReviewActivityReader


class RepositoryDiscoveryError(Exception):
    def __init__(self, message: str, *, may_show_cached_rows: bool = False) -> None:
        super().__init__(message)
        self.may_show_cached_rows = may_show_cached_rows


class InboxNotConfigured(Exception):
    def __init__(self) -> None:
        super().__init__("No inbox configured. Run loop-reviewer inbox setup first.")


class ScanSuperseded(Exception):
    def __init__(self) -> None:
        super().__init__("Inbox settings changed during the scan. Scan again.")


class RepositoryDiscovery(Protocol):
    def verify(self, address: RepositoryAddress) -> WatchedRepository: ...

    def list_open(
        self, repository: WatchedRepository
    ) -> tuple[DiscoveredPullRequest, ...]: ...


class ConfigureInbox:
    def __init__(self, discovery: RepositoryDiscovery, store: InboxStore) -> None:
        self._discovery = discovery
        self._store = store

    def run(
        self,
        projects: tuple[JiraProjectKey, ...],
        addresses: tuple[RepositoryAddress, ...],
        *,
        expected_version: int | None = None,
    ) -> InboxSettings:
        return self._store.replace_settings(
            projects,
            self.verify(projects, addresses),
            expected_version=expected_version,
        )

    def verify(
        self,
        projects: tuple[JiraProjectKey, ...],
        addresses: tuple[RepositoryAddress, ...],
    ) -> tuple[WatchedRepository, ...]:
        if len(projects) != 1:
            raise ValueError(
                "Configure exactly one Jira project key for all repositories."
            )
        if not addresses:
            raise ValueError("Configure at least one repository.")
        repositories = tuple(self._verify(address) for address in addresses)
        if len({repo.key for repo in repositories}) != len(repositories):
            raise ValueError("The same repository is configured more than once.")
        accounts: dict[str, str] = {}
        for repo in repositories:
            if accounts.setdefault(repo.address.host, repo.account) != repo.account:
                raise ValueError(
                    "Use one authenticated account per host when setting up the inbox."
                )
        return repositories

    def settings(self) -> InboxSettings | None:
        return self._store.settings()

    def verify_manual_edit(
        self, desired: InboxInputs, saved: InboxSettings | None
    ) -> tuple[WatchedRepository, ...]:
        if desired == saved_inbox_inputs(saved):
            return ()
        projects, addresses = desired
        verified = self.verify(projects, addresses)
        if saved is not None:
            accounts = {repo.address.host: repo.account for repo in saved.repositories}
            identities = {
                (repo.address.host, repo.address.slug.casefold()): repo.key
                for repo in saved.repositories
            }
            for address, repo in zip(addresses, verified, strict=True):
                if (
                    accounts.get(address.host, repo.account) != repo.account
                    or identities.get((address.host, address.slug.casefold()), repo.key)
                    != repo.key
                ):
                    raise RepositoryDiscoveryError(
                        "The GitHub account or repository identity changed. Verify the new watch list explicitly in Setup."
                    )
        return tuple(
            replace(repo, address=address)
            for address, repo in zip(addresses, verified, strict=True)
        )

    def _verify(self, address: RepositoryAddress) -> WatchedRepository:
        try:
            return self._discovery.verify(address)
        except RepositoryDiscoveryError as error:
            raise RepositoryDiscoveryError(
                f"{address.host}/{address.slug}: {error}"
            ) from None


class ScanInbox:
    def __init__(
        self,
        discovery: RepositoryDiscovery,
        store: InboxStore,
        reviews: ReviewActivityReader,
        *,
        now: Callable[[], datetime],
    ) -> None:
        self._discovery = discovery
        self._store = store
        self._read = ReadInbox(store, reviews)
        self._now = now

    def run(self) -> InboxReport:
        settings = self._store.settings()
        if settings is None:
            raise InboxNotConfigured
        for repository in settings.repositories:
            ticket = self._store.begin_scan(settings.version, repository.key)
            if ticket is None:
                raise ScanSuperseded
            try:
                self._store.record_scan(
                    ticket, self._check(repository, settings.projects)
                )
            except KeyboardInterrupt:
                try:
                    self._store.record_scan(
                        ticket, FailedScan(self._now(), "Scan interrupted.", False)
                    )
                except InboxStoreError:
                    pass  # Preserve the interrupt if local state could not be updated.
                raise
        return self._read.run(expected_version=settings.version)

    def _check(
        self, repository: WatchedRepository, projects: tuple[JiraProjectKey, ...]
    ) -> ScanResult:
        try:
            rows = self._discovery.list_open(repository)
        except RepositoryDiscoveryError as error:
            return FailedScan(self._now(), str(error), error.may_show_cached_rows)
        eligible = tuple(
            row for row in rows if row.reasons(projects, repository.account)
        )
        return SuccessfulScan(self._now(), eligible)


class ReadInbox:
    def __init__(self, store: InboxStore, reviews: ReviewActivityReader) -> None:
        self._store = store
        self._reviews = reviews

    def run(self, *, expected_version: int | None = None) -> InboxReport:
        version = expected_version
        if version is None:
            settings = self._store.settings()
            if settings is None:
                raise InboxNotConfigured
            version = settings.version
        snapshot = self._store.snapshot(version)
        if snapshot is None:
            raise ScanSuperseded
        report = self._project(snapshot)
        current = self._store.settings()
        if current is None or current.version != version:
            raise ScanSuperseded
        return report

    def _project(self, snapshot: InboxSnapshot) -> InboxReport:
        candidates = [
            (
                repo,
                row,
                ReviewIdentity.for_pull_request(row.ref, repo.repository.account),
            )
            for repo in snapshot.repositories
            if isinstance(repo.latest, SuccessfulScan)
            or (
                isinstance(repo.latest, FailedScan) and repo.latest.may_show_cached_rows
            )
            if repo.last_success is not None
            for row in repo.last_success.eligible_rows
        ]
        activity = self._reviews.activity(
            tuple(identity for _, _, identity in candidates)
        )
        rows: list[InboxRow] = []
        counts = dict.fromkeys(
            (repo.repository.key for repo in snapshot.repositories), 0
        )
        for repo, row, identity in candidates:
            status = activity.get(identity)
            if status != "completed":
                rows.append(
                    InboxRow(
                        identity,
                        row,
                        row.reasons(snapshot.settings.projects, identity.account),
                        status,
                        isinstance(repo.latest, FailedScan),
                        row.ref.number in repo.deferred_numbers,
                    )
                )
                counts[repo.repository.key] += 1
        statuses = tuple(
            RepositoryScanStatus(
                repo.repository,
                "failed"
                if isinstance(repo.latest, FailedScan)
                else "success"
                if repo.latest
                else "pending",
                repo.last_success.checked_at if repo.last_success else None,
                repo.latest.error if isinstance(repo.latest, FailedScan) else None,
                counts[repo.repository.key],
            )
            for repo in snapshot.repositories
        )
        rows.sort(key=lambda row: row.order)
        return InboxReport(
            snapshot.settings,
            statuses,
            tuple(row for row in rows if not row.deferred),
            tuple(row for row in rows if row.deferred),
        )
