"""Read pull request metadata and revision comparisons through gh."""

from collections.abc import Mapping
from typing import cast
from unicodedata import category
from urllib.parse import quote

from loop_reviewer.integrations.gh_api import GhApi
from loop_reviewer.integrations.github_reads import INVALID_RESPONSE, read_json
from loop_reviewer.review.pull_request import (
    PullRequest,
    PullRequestRef,
    ReviewedRevision,
    commit_id,
)
from loop_reviewer.review.selection import GitHubReadError

_CONTENTS_GUIDANCE = "Check the active account's repository and Contents read access."


class GhPullRequestReader:
    def __init__(
        self, environment: Mapping[str, str], *, api: GhApi | None = None
    ) -> None:
        self._api = api if api is not None else GhApi(environment)

    def read_pull_request(self, ref: PullRequestRef) -> tuple[str, PullRequest]:
        account = _name(self._get(ref.host, "user").get("login"))
        return account, self.read_metadata(ref)

    def read_metadata(self, ref: PullRequestRef) -> PullRequest:
        data = self._get(
            ref.host,
            f"repos/{ref.owner}/{ref.repo}/pulls/{ref.number}",
            unavailable="The pull request was not found or inaccessible. Check the active account's repository access.",
        )
        number, state, draft = data.get("number"), data.get("state"), data.get("draft")
        if (
            type(number) is not int
            or number != ref.number
            or (state != "open" and state != "closed")
            or not isinstance(draft, bool)
        ):
            raise GitHubReadError(INVALID_RESPONSE)
        author = _name(_object(data.get("user")).get("login"))
        base = _object(data.get("base"))
        repository = _object(base.get("repo"))
        full_name = _name(repository.get("full_name"))
        repository_id = repository.get("id")
        if type(repository_id) is not int or repository_id < 1:
            raise GitHubReadError(INVALID_RESPONSE)
        branch = _name(base.get("ref"))
        head = _name(_object(data.get("head")).get("sha"))
        try:
            canonical_ref = PullRequestRef.from_repository(
                ref.host, full_name, ref.number, repository_id=repository_id
            )
            return PullRequest(canonical_ref, author, state, draft, branch, head)
        except ValueError:
            raise GitHubReadError(INVALID_RESPONSE) from None

    def read_revision(self, pull_request: PullRequest) -> ReviewedRevision:
        ref = pull_request.ref
        repository = f"repos/{ref.owner}/{ref.repo}"
        branch = pull_request.target_branch
        branch_data = self._get(
            ref.host,
            f"{repository}/git/ref/heads/{quote(branch, safe='/')}",
            unavailable=f"Could not read the target branch. {_CONTENTS_GUIDANCE}",
        )
        if branch_data.get("ref") != f"refs/heads/{branch}":
            raise GitHubReadError(INVALID_RESPONSE)
        base_tip = _sha(_object(branch_data.get("object")).get("sha"))
        head = pull_request.head
        comparison = self._get(
            ref.host,
            f"{repository}/compare/{base_tip}...{head}?per_page=1",
            unavailable=f"Could not compare the selected commits. {_CONTENTS_GUIDANCE} Reopen the PR to retry.",
        )
        if _sha(_object(comparison.get("base_commit")).get("sha")) != base_tip:
            raise GitHubReadError(INVALID_RESPONSE)
        merge_base = _sha(_object(comparison.get("merge_base_commit")).get("sha"))
        return ReviewedRevision(branch, base_tip, merge_base, head)

    def _get(
        self, host: str, endpoint: str, *, unavailable: str | None = None
    ) -> dict[str, object]:
        _, data = read_json(self._api, host, endpoint, unavailable=unavailable)
        return _object(data)


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise GitHubReadError(INVALID_RESPONSE)
    return cast("dict[str, object]", value)


def _name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(
            character.isspace() or category(character) in {"Cc", "Cs"}
            for character in value
        )
    ):
        raise GitHubReadError(INVALID_RESPONSE)
    return value


def _sha(value: object) -> str:
    try:
        return commit_id(_name(value))
    except ValueError:
        raise GitHubReadError(INVALID_RESPONSE) from None
