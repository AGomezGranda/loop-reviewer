"""Represent watched repositories and their review inbox projection."""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Self
from urllib.parse import urlsplit

from loop_reviewer.review.model import ReviewIdentity, RunStatus
from loop_reviewer.review.pull_request import PullRequestRef

_REQUESTED = "Your review was requested"


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

    def reasons(
        self, projects: tuple[JiraProjectKey, ...], account: str
    ) -> tuple[str, ...]:
        if self.state != "open" or self.draft:
            return ()
        reasons: list[str] = []
        if any(user.lower() == account.lower() for user in self.requested_users):
            reasons.append(_REQUESTED)
        if self.author.lower() == account.lower():
            reasons.append("Your PR")
        reasons.extend(
            f"Matches {project.label}"
            for project in projects
            if project.matches(self.title)
        )
        return tuple(reasons)


@dataclass(frozen=True)
class InboxRow:
    identity: ReviewIdentity
    pull_request: DiscoveredPullRequest
    reasons: tuple[str, ...]
    last_saved: RunStatus | None
    stale: bool
    deferred: bool = False

    @property
    def order(self) -> tuple[bool, str, str, int]:
        ref = self.pull_request.ref
        return (
            _REQUESTED not in self.reasons,
            ref.host,
            f"{ref.owner}/{ref.repo}".casefold(),
            ref.number,
        )


@dataclass(frozen=True)
class InboxSettings:
    projects: tuple[JiraProjectKey, ...]
    repositories: tuple[WatchedRepository, ...]
    version: int

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
