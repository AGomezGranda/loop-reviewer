"""Represent watched repositories and their review inbox projection."""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Self
from urllib.parse import urlsplit

from loop_reviewer.review.model import ReviewIdentity, RunStatus
from loop_reviewer.review.pull_request import PullRequestRef

type InboxReasonCode = Literal["direct_request", "authored", "jira_project"]


@dataclass(frozen=True)
class InboxReason:
    code: InboxReasonCode
    label: str


@dataclass(frozen=True)
class JiraProjectKey:
    value: str

    def __post_init__(self) -> None:
        key = self.value.strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", key):
            raise ValueError(
                "Expected an ASCII Jira project key: a letter followed by up to 31 letters, digits or underscores."
            )
        object.__setattr__(self, "value", key.upper())

    def matches(self, title: str) -> bool:
        return re.match(rf"\[{self.value}-[0-9]+\]", title) is not None

    @property
    def label(self) -> str:
        return f"[{self.value}-…]"


@dataclass(frozen=True)
class RepositoryAddress:
    host: str
    slug: str

    def __post_init__(self) -> None:
        ref = PullRequestRef.from_repository(self.host, self.slug, 1)
        object.__setattr__(self, "host", ref.host)

    @property
    def url(self) -> str:
        return f"https://{self.host}/{self.slug}"

    @classmethod
    def from_url(cls, url: str) -> Self:
        try:
            if (
                any(not char.isprintable() or char.isspace() for char in url)
                or "?" in url
                or "#" in url
            ):
                raise ValueError
            parts = urlsplit(url)
            if parts.scheme != "https" or not parts.path.startswith("/"):
                raise ValueError
            return cls(parts.netloc, parts.path[1:].removesuffix("/"))
        except ValueError:
            raise ValueError(
                "Expected a repository URL: https://HOST/OWNER/REPO"
            ) from None


@dataclass(frozen=True)
class WatchedRepositoryKey:
    host: str
    repository_id: int
    account: str


@dataclass(frozen=True)
class WatchedRepository:
    address: RepositoryAddress
    repository_id: int
    account: str

    def __post_init__(self) -> None:
        if type(self.repository_id) is not int or not 0 < self.repository_id < 2**63:
            raise ValueError(
                "Expected a positive GitHub repository ID in the supported range."
            )
        if not re.fullmatch(r"[A-Za-z0-9_-]+(?:\[bot\])?", self.account):
            raise ValueError("Expected a GitHub account name.")
        object.__setattr__(self, "account", self.account.lower())

    @property
    def key(self) -> WatchedRepositoryKey:
        return WatchedRepositoryKey(self.address.host, self.repository_id, self.account)


@dataclass(frozen=True)
class DiscoveredPullRequest:
    ref: PullRequestRef
    title: str
    author: str
    state: str
    draft: bool
    requested_users: tuple[str, ...]

    opened_at: int | None = None

    def reasons(
        self, projects: tuple[JiraProjectKey, ...], account: str
    ) -> tuple[InboxReason, ...]:
        if self.state != "open" or self.draft:
            return ()
        reasons: list[InboxReason] = []
        if any(user.lower() == account.lower() for user in self.requested_users):
            reasons.append(InboxReason("direct_request", "Your review was requested"))
        if self.author.lower() == account.lower():
            reasons.append(InboxReason("authored", "Your PR"))
        reasons.extend(
            InboxReason("jira_project", f"Matches {project.label}")
            for project in projects
            if project.matches(self.title)
        )
        return tuple(reasons)


@dataclass(frozen=True)
class InboxRow:
    identity: ReviewIdentity
    pull_request: DiscoveredPullRequest
    reasons: tuple[InboxReason, ...]
    last_saved: RunStatus | None
    stale: bool
    deferred: bool = False

    @property
    def order(self) -> tuple[bool, int, str, str, int]:
        ref = self.pull_request.ref
        return (
            self.pull_request.opened_at is None,
            -(self.pull_request.opened_at or 0),
            ref.host,
            f"{ref.owner}/{ref.repo}".casefold(),
            -ref.number,
        )


@dataclass(frozen=True)
class InboxSettings:
    projects: tuple[JiraProjectKey, ...]
    repositories: tuple[WatchedRepository, ...]
    version: int
    scan_version: int
    excluded_title_terms: tuple[str, ...] = ()

    def reconfigured(
        self,
        projects: tuple[JiraProjectKey, ...],
        repositories: tuple[WatchedRepository, ...],
        excluded_title_terms: tuple[str, ...],
    ) -> InboxSettings:
        version = self.version + 1
        scan_version = (
            self.scan_version
            if (projects, repositories) == (self.projects, self.repositories)
            else version
        )
        return InboxSettings(
            projects, repositories, version, scan_version, excluded_title_terms
        )

    def excludes(self, title: str) -> bool:
        title = title.casefold()
        return any(term.casefold() in title for term in self.excluded_title_terms)

    @property
    def prefix_label(self) -> str:
        return ", ".join(project.label for project in self.projects)


@dataclass(frozen=True)
class RepositoryScanStatus:
    repository: WatchedRepository
    outcome: Literal["success", "failed", "pending"]
    last_success: datetime | None
    error: str | None
    visible_count: int


@dataclass(frozen=True)
class InboxReport:
    settings: InboxSettings
    statuses: tuple[RepositoryScanStatus, ...]
    active: tuple[InboxRow, ...]
    deferred: tuple[InboxRow, ...] = ()

    @property
    def succeeded(self) -> bool:
        return all(status.outcome == "success" for status in self.statuses)
