"""Publish exact review payloads and read evidence with a host-bound credential."""

import json
import re
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from typing import Literal, cast

from loop_reviewer.integrations.gh_api import GhApi, GhInvocationError, GhResponse
from loop_reviewer.integrations.github import GhPullRequestReader
from loop_reviewer.integrations.github_credentials import open_gh_environment
from loop_reviewer.integrations.github_pagination import has_next_page
from loop_reviewer.integrations.github_reads import is_rate_limited
from loop_reviewer.review.model import FindingLocation, PublicationReceipt
from loop_reviewer.review.publication import RejectionReason
from loop_reviewer.review.publication_model import PublicationPreview
from loop_reviewer.review.pull_request import (
    PullRequest,
    PullRequestRef,
    ReviewedRevision,
)
from loop_reviewer.review.selection import GitHubReadError

_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?")
_INVALID = "GitHub returned invalid or incomplete publication data."


@contextmanager
def open_gh_publication(
    environment: Mapping[str, str],
    host: str,
    *,
    inherited_fd: int | None = None,
) -> Generator[GhPublicationSession]:
    if host == "localhost" or host.endswith((".localhost", ".github.com")):
        raise GitHubReadError("Publication requires the canonical GitHub host.")
    with open_gh_environment(environment, host, prefix="loop-reviewer-gh-") as (
        pinned,
        directory,
    ):
        yield GhPublicationSession(
            host, GhApi(pinned, private_cwd=directory), inherited_fd
        )


class GhPublicationSession:
    def __init__(self, host: str, api: GhApi, inherited_fd: int | None) -> None:
        self._host = host
        self._inherited_fd = inherited_fd
        self._api = api
        self._reader = GhPullRequestReader({}, api=api)
        user = _object(_json(self._get("user")))
        actor_id, login = user.get("id"), user.get("login")
        if (
            type(actor_id) is not int
            or actor_id < 1
            or not isinstance(login, str)
            or not re.fullmatch(r"[a-zA-Z0-9_-]+(?:\[bot\])?", login)
        ):
            raise GitHubReadError(_INVALID)
        self.login = login
        self.actor_id = actor_id

    def _get(self, endpoint: str) -> GhResponse:
        try:
            return self._api.get(self._host, endpoint)
        except GhInvocationError:
            raise GitHubReadError(
                "Could not read GitHub publication data; check access and try again."
            ) from None

    def _require_host(self, ref: PullRequestRef) -> None:
        if ref.host != self._host:
            raise GitHubReadError(
                "The GitHub host does not match the bound credential."
            )

    def read_pull_request(self, ref: PullRequestRef) -> PullRequest:
        self._require_host(ref)
        return self._reader.read_metadata(ref)

    def read_revision(self, pull_request: PullRequest) -> ReviewedRevision:
        self._require_host(pull_request.ref)
        return self._reader.read_revision(pull_request)

    def usable_anchors(
        self, ref: PullRequestRef, requested: frozenset[FindingLocation]
    ) -> frozenset[FindingLocation]:
        self._require_host(ref)
        wanted: dict[str, set[int]] = {}
        for location in requested:
            wanted.setdefault(location.path, set()).add(location.right_line)
        found: set[FindingLocation] = set()
        for page in range(1, 31):
            if not wanted:
                break
            try:
                response = self._get(f"{_pull(ref)}/files?per_page=100&page={page}")
                files = _items(response)
            except GitHubReadError:
                break
            for file in files:
                path, patch = file.get("filename"), file.get("patch")
                if isinstance(path, str) and path in wanted and isinstance(patch, str):
                    lines = wanted.pop(path)
                    found.update(
                        FindingLocation(path, line)
                        for line in lines & _added_lines(patch)
                    )
            try:
                if not _has_next(response, ref, "files", page, len(files)):
                    break
            except GitHubReadError:
                break
        return frozenset(found)

    def submit(
        self, preview: PublicationPreview
    ) -> PublicationReceipt | RejectionReason | Literal["unknown"]:
        ref = preview.selection.ref
        self._require_host(ref)
        if self._inherited_fd is None:
            return "unknown"
        payload = json.dumps(
            {
                "commit_id": preview.selection.revision.head,
                "event": "COMMENT" if preview.action == "comment" else "APPROVE",
                "body": preview.body,
                "comments": [
                    {
                        "path": comment.path,
                        "line": comment.line,
                        "side": "RIGHT",
                        "body": comment.body,
                    }
                    for comment in preview.comments
                ],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        try:
            response = self._api.create(
                ref.host,
                f"{_pull(ref)}/reviews",
                payload,
                inherited_fd=self._inherited_fd,
            )
            data = _object(json.loads(response.body))
        except GhInvocationError, GitHubReadError, ValueError, RecursionError:
            return "unknown"
        if response.returncode in (1, 4) and response.status in {
            401,
            403,
            404,
            422,
            429,
        }:
            message = data.get("message")
            if not isinstance(message, str) or not message.strip():
                return "unknown"
            if is_rate_limited(response):
                return RejectionReason.RATE_LIMIT
            if response.status == 401 or response.returncode == 4:
                return RejectionReason.AUTHENTICATION
            if response.status == 422:
                return RejectionReason.VALIDATION
            return RejectionReason.ACCESS
        if (
            response.returncode == 0
            and response.status is not None
            and 200 <= response.status < 300
        ):
            return _matching_receipt(data, preview, allow_dismissed=False) or "unknown"
        return "unknown"

    def reconcile(
        self, preview: PublicationPreview, *, ref: PullRequestRef
    ) -> PublicationReceipt | Literal["no_match", "inconclusive"]:
        self._require_host(ref)
        if (
            ref.host != preview.selection.ref.host
            or ref.repository_id != preview.selection.ref.repository_id
            or ref.number != preview.selection.ref.number
        ):
            return "inconclusive"
        receipt: PublicationReceipt | None = None
        seen: set[int] = set()
        try:
            for page in range(1, 51):
                response = self._get(f"{_pull(ref)}/reviews?per_page=100&page={page}")
                reviews = _items(response)
                for review in reviews:
                    body, review_id = review.get("body"), review.get("id")
                    if (
                        not isinstance(body, str)
                        or type(review_id) is not int
                        or review_id < 1
                        or review_id in seen
                    ):
                        return "inconclusive"
                    seen.add(review_id)
                    if body.partition("\n")[0] != preview.id.marker:
                        continue
                    matched = _matching_receipt(review, preview, allow_dismissed=True)
                    if matched is None or receipt is not None:
                        return "inconclusive"
                    receipt = matched
                if not _has_next(response, ref, "reviews", page, len(reviews)):
                    return receipt or "no_match"
        except GitHubReadError:
            pass
        return "inconclusive"


def _matching_receipt(
    review: dict[str, object], preview: PublicationPreview, *, allow_dismissed: bool
) -> PublicationReceipt | None:
    user, review_id = review.get("user"), review.get("id")
    expected = "COMMENTED" if preview.action == "comment" else "APPROVED"
    state = review.get("state")
    if (
        (state != expected and not (allow_dismissed and state == "DISMISSED"))
        or type(review_id) is not int
        or not 0 < review_id < 2**63
        or review.get("body") != preview.body
        or review.get("commit_id") != preview.selection.revision.head
        or not isinstance(user, dict)
    ):
        return None
    actor: object = user.get("id")
    login: object = user.get("login")
    if (
        type(actor) is not int
        or actor != preview.actor_id
        or not isinstance(login, str)
        or login.lower() != preview.selection.identity.account
    ):
        return None
    return PublicationReceipt(review_id, preview.action)


def _pull(ref: PullRequestRef) -> str:
    return f"repos/{ref.owner}/{ref.repo}/pulls/{ref.number}"


def _has_next(
    response: GhResponse, ref: PullRequestRef, resource: str, page: int, count: int
) -> bool:
    paths = frozenset(
        {
            f"/{_pull(ref)}/{resource}",
            f"/repositories/{ref.repository_id}/pulls/{ref.number}/{resource}",
        }
    )
    return has_next_page(
        response, ref.host, paths, page, count, invalid_message=_INVALID
    )


def _json(response: GhResponse) -> object:
    if response.returncode != 0 or response.status != 200:
        raise GitHubReadError(
            "GitHub publication data is unavailable; check account access."
        )
    try:
        return json.loads(response.body)
    except ValueError, RecursionError:
        raise GitHubReadError(_INVALID) from None


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise GitHubReadError(_INVALID)
    return cast("dict[str, object]", value)


def _items(response: GhResponse) -> list[dict[str, object]]:
    data = _json(response)
    if not isinstance(data, list) or len(data) > 100:
        raise GitHubReadError(_INVALID)
    return [_object(item) for item in data]


def _added_lines(patch: str) -> set[int]:
    additions: set[int] = set()
    right = old_left = new_left = 0
    current: set[int] = set()
    active = False
    for line in patch.split("\n"):
        if line.startswith("@@"):
            if active and old_left == new_left == 0:
                additions.update(current)
            current = set()
            header = _HUNK.fullmatch(line)
            active = header is not None
            if header:
                try:
                    old, old_left, right, new_left = (
                        int(part) if part is not None else 1 for part in header.groups()
                    )
                    active = (old > 0 or old_left == 0) and (right > 0 or new_left == 0)
                except ValueError:
                    active = False
        elif active and line.startswith("\\ No newline at end of file"):
            continue
        elif active:
            if not line and old_left == new_left == 0:
                continue
            prefix = line[:1]
            if prefix not in (" ", "-", "+"):
                active = False
                continue
            if prefix != "+":
                old_left -= 1
            if prefix != "-":
                new_left -= 1
                if prefix == "+":
                    current.add(right)
                right += 1
            active = old_left >= 0 and new_left >= 0
    if active and old_left == new_left == 0:
        additions.update(current)
    return additions
