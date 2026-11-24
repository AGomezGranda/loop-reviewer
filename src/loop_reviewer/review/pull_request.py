"""Define pull request identity, review eligibility, and pinned revisions."""

import re
from dataclasses import dataclass
from typing import Literal, Self
from unicodedata import category
from urllib.parse import urlsplit

from loop_reviewer.source_names import validate_repository, validate_target_branch

_PR_PATH = re.compile(r"/([^/]+/[^/]+)/pull/([0-9]+)/?")
_SHA = re.compile(r"[0-9a-fA-F]{40}")
_EXPECTED_URL = "Expected a pull request URL: https://HOST/OWNER/REPO/pull/NUMBER"


class PullRequestNotReviewable(Exception):
    pass


@dataclass(frozen=True)
class PullRequestRef:
    host: str
    owner: str
    repo: str
    number: int
    repository_id: int | None = None

    def __post_init__(self) -> None:
        validate_repository(self.host, self.owner, self.repo)
        if type(self.number) is not int or self.number < 1:
            raise ValueError("Expected a positive pull request number")
        if self.repository_id is not None and (
            type(self.repository_id) is not int or self.repository_id < 1
        ):
            raise ValueError("Expected a positive GitHub repository ID")
        object.__setattr__(self, "host", self.host.lower())

    @property
    def url(self) -> str:
        return f"https://{self.host}/{self.owner}/{self.repo}/pull/{self.number}"

    @classmethod
    def from_url(cls, url: str) -> Self:
        if any(character.isspace() or category(character) == "Cc" for character in url):
            raise ValueError(_EXPECTED_URL)
        try:
            parts = urlsplit(url)
            path = _PR_PATH.fullmatch(parts.path)
            if parts.scheme != "https" or path is None:
                raise ValueError
            return cls.from_repository(parts.netloc, path[1], int(path[2]))
        except ValueError:
            raise ValueError(_EXPECTED_URL) from None

    @classmethod
    def from_repository(
        cls, host: str, full_name: str, number: int, *, repository_id: int | None = None
    ) -> Self:
        owner, _, repo = full_name.partition("/")
        return cls(host, owner, repo, number, repository_id)


def commit_id(value: str) -> str:
    if not _SHA.fullmatch(value):
        raise ValueError("Expected a full Git commit ID")
    return value.lower()


@dataclass(frozen=True)
class PullRequest:
    ref: PullRequestRef
    author: str
    state: Literal["open", "closed"]
    is_draft: bool
    target_branch: str
    head: str

    def __post_init__(self) -> None:
        validate_target_branch(self.target_branch)
        object.__setattr__(self, "head", commit_id(self.head))

    def require_open_for_review(self) -> None:
        if self.state != "open":
            raise PullRequestNotReviewable("The pull request is closed.")
        if self.is_draft:
            raise PullRequestNotReviewable(
                "Mark the draft pull request ready for review first."
            )


@dataclass(frozen=True)
class ReviewComparison:
    target_branch: str
    merge_base: str
    head: str

    def __post_init__(self) -> None:
        validate_target_branch(self.target_branch)
        object.__setattr__(self, "merge_base", commit_id(self.merge_base))
        object.__setattr__(self, "head", commit_id(self.head))


@dataclass(frozen=True)
class ReviewedRevision:
    target_branch: str
    base_tip: str
    merge_base: str
    head: str

    def __post_init__(self) -> None:
        validate_target_branch(self.target_branch)
        object.__setattr__(self, "base_tip", commit_id(self.base_tip))
        object.__setattr__(self, "merge_base", commit_id(self.merge_base))
        object.__setattr__(self, "head", commit_id(self.head))

    @property
    def comparison(self) -> ReviewComparison:
        return ReviewComparison(self.target_branch, self.merge_base, self.head)
