"""Select a reviewable pull request and pin its revision through a reader."""

from typing import Protocol

from loop_reviewer.review.model import ReviewSelection
from loop_reviewer.review.pull_request import (
    PullRequest,
    PullRequestRef,
    ReviewedRevision,
)


class PullRequestReader(Protocol):
    def read_pull_request(self, ref: PullRequestRef) -> tuple[str, PullRequest]: ...

    def read_revision(self, pull_request: PullRequest) -> ReviewedRevision: ...


class GitHubReadError(Exception):
    pass


def open_pull_request(
    ref: PullRequestRef, reader: PullRequestReader
) -> ReviewSelection:
    account, pull_request = reader.read_pull_request(ref)
    return select_revision(account, pull_request, reader)


def select_revision(
    account: str, pull_request: PullRequest, reader: PullRequestReader
) -> ReviewSelection:
    pull_request.require_open_for_review()
    revision = reader.read_revision(pull_request)
    return ReviewSelection(pull_request.ref, account, pull_request.author, revision)
