"""Exercise revision selection through a fake pull request reader."""

from typing import Literal

import pytest

from loop_reviewer.review.model import ReviewSelection
from loop_reviewer.review.pull_request import (
    PullRequest,
    PullRequestNotReviewable,
    PullRequestRef,
    ReviewedRevision,
)
from loop_reviewer.review.selection import (
    GitHubReadError,
    open_pull_request,
)

_REQUESTED = PullRequestRef("github.com", "acme", "repo", 12)
_CANONICAL = PullRequestRef("github.com", "Acme", "Repo", 12)
_REVISION = ReviewedRevision("main", "a" * 40, "b" * 40, "c" * 40)


class StubReader:
    def __init__(self, result: PullRequest | GitHubReadError) -> None:
        self.result = result
        self.calls: list[str] = []

    def read_pull_request(self, ref: PullRequestRef) -> tuple[str, PullRequest]:
        assert ref == _REQUESTED
        self.calls.append("pull request")
        if isinstance(self.result, GitHubReadError):
            raise self.result
        return "reviewer", self.result

    def read_revision(self, pull_request: PullRequest) -> ReviewedRevision:
        assert pull_request == self.result
        self.calls.append("revision")
        return _REVISION


@pytest.mark.parametrize("author", ["teammate", "reviewer"], ids=["teammate", "own-pr"])
def test_open_pull_request_selects_revision(author: str) -> None:
    reader = StubReader(
        PullRequest(_CANONICAL, author, "open", False, "main", _REVISION.head)
    )

    assert open_pull_request(_REQUESTED, reader) == ReviewSelection(
        _CANONICAL, "reviewer", author, _REVISION
    )
    assert reader.calls == ["pull request", "revision"]


@pytest.mark.parametrize(
    ("state", "draft", "error", "message"),
    [
        pytest.param(None, False, GitHubReadError, "inaccessible", id="inaccessible"),
        pytest.param(
            "open", True, PullRequestNotReviewable, "ready for review", id="draft"
        ),
        pytest.param(
            "closed", False, PullRequestNotReviewable, "closed", id="closed-or-merged"
        ),
    ],
)
def test_open_pull_request_rejects_unavailable_or_ineligible_pr(
    state: Literal["open", "closed"] | None,
    draft: bool,
    error: type[Exception],
    message: str,
) -> None:
    reader = StubReader(
        GitHubReadError("inaccessible")
        if state is None
        else PullRequest(_CANONICAL, "teammate", state, draft, "main", _REVISION.head)
    )

    with pytest.raises(error, match=message):
        open_pull_request(_REQUESTED, reader)

    assert reader.calls == ["pull request"]
