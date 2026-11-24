"""Capture bounded, read-only GitHub discussion between two selection checks."""

import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import cast

from loop_reviewer.integrations.gh_api import (
    BOUNDED_GET_BYTES,
    GhApi,
    GhInvocationError,
)
from loop_reviewer.integrations.github import GhPullRequestReader
from loop_reviewer.integrations.github_reads import INVALID_RESPONSE, GhReadFailure
from loop_reviewer.review.pull_request import PullRequest, PullRequestRef
from loop_reviewer.review.selection import GitHubReadError
from loop_reviewer.reviewer.discussion_contract import (
    ITEM_BYTES,
    ITEM_LIMIT,
    REPLY_LIMIT,
    SNAPSHOT_BYTES,
    CaptureOmission,
    DiscussionComment,
    DiscussionItem,
    DiscussionLocation,
    DiscussionReview,
    DiscussionScope,
    DiscussionSnapshotV1,
    DiscussionStatus,
    DiscussionTarget,
    DiscussionThread,
    OmissionCode,
    omission_key,
    rooted_comment_ids,
    unavailable_discussion,
    valid_permalink,
)
from loop_reviewer.reviewer.inventory import RevisionSide
from loop_reviewer.reviewer.inventory_contract import encode_path

_RESPONSE_BYTES = 256 * 1024
_MAX_CALLS = 32
_FINAL_CALLS = 3
_FINAL_SECONDS = _FINAL_CALLS * 5
_PAGE_SIZE = 20

_THREADS = """
query Threads($owner: String!, $repo: String!, $number: Int!, $size: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    databaseId nameWithOwner
    pullRequest(number: $number) {
      id number baseRefName headRefOid
      reviewThreads(first: $size, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id isResolved isOutdated path diffSide startDiffSide line startLine
          comments(first: $size) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id body path originalLine originalStartLine originalCommit { oid }
              diffHunk replyTo { id } pullRequest { id } url
            }
          }
        }
      }
    }
  }
}
"""
_REVIEWS = """
query Reviews($owner: String!, $repo: String!, $number: Int!, $size: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    databaseId nameWithOwner
    pullRequest(number: $number) {
      id number baseRefName headRefOid
      reviews(first: $size, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { id body commit { oid } }
      }
    }
  }
}
"""
_CONVERSATION = """
query Conversation($owner: String!, $repo: String!, $number: Int!, $size: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    databaseId nameWithOwner
    pullRequest(number: $number) {
      id number baseRefName headRefOid
      comments(first: $size, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { id body }
      }
    }
  }
}
"""
_REPLIES = """
query Replies($thread: ID!, $size: Int!, $cursor: String) {
  node(id: $thread) {
    ... on PullRequestReviewThread {
      id
      comments(first: $size, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id body path originalLine originalStartLine originalCommit { oid }
          diffHunk replyTo { id } pullRequest { id } url
        }
      }
    }
  }
}
"""


class _CaptureError(Exception):
    def __init__(self, code: OmissionCode) -> None:
        super().__init__(code)
        self.code = code


class _Budget:
    def __init__(self, monotonic: Callable[[], float]) -> None:
        self.clock = monotonic
        self.deadline = monotonic() + 30
        self.calls = 0

    def take(self, *, paging: bool = False) -> tuple[float, int]:
        until = self.deadline - (_FINAL_SECONDS if paging else 0)
        remaining = until - self.clock()
        if remaining <= 0 or self.calls >= _MAX_CALLS - (_FINAL_CALLS if paging else 0):
            raise _CaptureError("limit_reached")
        self.calls += 1
        return min(5, remaining), _RESPONSE_BYTES if paging else BOUNDED_GET_BYTES

    def check_deadline(self) -> None:
        if self.clock() >= self.deadline:
            raise _CaptureError("limit_reached")


type _Node = dict[str, object]


def _node(value: object) -> _Node:
    if not isinstance(value, dict):
        raise TypeError("Expected an object")
    return cast("_Node", value)


def _id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9_+=:/.-]{1,256}", value
    ):
        raise ValueError("Expected a node ID")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("Expected discussion text")
    if len(value.encode("utf-8")) > ITEM_BYTES:
        raise _CaptureError("limit_reached")
    return value


def _path(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("Expected a GitHub path")
    return encode_path(value.encode("utf-8"))


def _thread_side(data: _Node) -> RevisionSide | None:
    github_side = data["diffSide"]
    start_side = data["startDiffSide"]
    if github_side not in ("LEFT", "RIGHT", None) or start_side not in (
        "LEFT",
        "RIGHT",
        None,
    ):
        raise ValueError("Invalid diff side")
    if start_side not in (None, github_side):
        return None
    return (
        "base" if github_side == "LEFT" else "head" if github_side == "RIGHT" else None
    )


def _location(
    path: str | None, side: RevisionSide | None, start: object, end: object
) -> DiscussionLocation | None:
    return (
        None
        if path is None and side is None and start is None and end is None
        else DiscussionLocation.model_validate(
            {"path": path, "side": side, "start_line": start, "end_line": end}
        )
    )


@dataclass
class _Contents:
    threads: list[DiscussionThread] = field(default_factory=list)
    reviews: list[DiscussionReview] = field(default_factory=list)
    conversation: list[DiscussionItem] = field(default_factory=list)
    omissions: set[CaptureOmission] = field(default_factory=set)
    ids: set[str] = field(default_factory=set)

    def omit(
        self, scope: DiscussionScope, code: OmissionCode, thread_id: str | None = None
    ) -> None:
        self.omissions.add(CaptureOmission(scope=scope, code=code, thread_id=thread_id))

    def accept(self, ids: set[str]) -> None:
        if ids & self.ids:
            raise ValueError("Duplicate discussion ID")
        self.ids.update(ids)

    def snapshot(self, target: DiscussionTarget, at: datetime) -> DiscussionSnapshotV1:
        # Charge each serialized item once. The envelope accounts for omission metadata.
        collections: tuple[
            tuple[
                list[DiscussionThread] | list[DiscussionReview] | list[DiscussionItem],
                DiscussionScope,
            ],
            ...,
        ] = (
            (self.threads, "threads"),
            (self.reviews, "reviews"),
            (self.conversation, "conversation"),
        )
        size = sum(
            len(item.model_dump_json().encode("utf-8")) + 1
            for items, _ in collections
            for item in items
        )
        while True:
            content = any(items for items, _ in collections)
            status: DiscussionStatus = (
                "complete"
                if not self.omissions
                else "partial"
                if content
                else "unavailable"
            )
            omissions = tuple(sorted(self.omissions, key=omission_key))
            envelope = DiscussionSnapshotV1.model_construct(
                schema_version=1,
                target=target,
                captured_at=at,
                status=status,
                omissions=omissions,
                threads=(),
                reviews=(),
                conversation=(),
            )
            separators = sum(bool(items) for items, _ in collections)
            if (
                size - separators + len(envelope.model_dump_json().encode("utf-8"))
                <= SNAPSHOT_BYTES
            ):
                return DiscussionSnapshotV1(
                    schema_version=1,
                    target=target,
                    captured_at=at,
                    status=status,
                    omissions=omissions,
                    threads=tuple(sorted(self.threads, key=lambda item: item.id)),
                    reviews=tuple(sorted(self.reviews, key=lambda item: item.id)),
                    conversation=tuple(
                        sorted(self.conversation, key=lambda item: item.id)
                    ),
                )
            for items, scope in reversed(collections):
                if items:
                    dropped = items.pop()
                    size -= len(dropped.model_dump_json().encode("utf-8")) + 1
                    self.omit(scope, "limit_reached")
                    break
            else:
                raise ValueError("Discussion envelope exceeds snapshot cap")


class GhDiscussionReader:
    def __init__(
        self,
        api: GhApi,
        pull_requests: Callable[[GhApi], GhPullRequestReader],
        *,
        expected_account: str,
        monotonic: Callable[[], float],
        utc_now: Callable[[], datetime],
    ) -> None:
        self._api = api
        self._pull_requests = pull_requests
        self._account = expected_account.lower()
        self._clock = monotonic
        self._utc_now = utc_now

    def read(self, target: DiscussionTarget) -> DiscussionSnapshotV1:
        at = self._utc_now()
        budget = _Budget(self._clock)
        reader = self._pull_requests(self._api.with_bounded_get(budget.take))
        ref = PullRequestRef(
            target.host, target.owner, target.repo, target.number, target.repository_id
        )
        try:
            account, pr = reader.read_pull_request(ref)
            if account.lower() != self._account:
                raise _CaptureError("selection_changed")
            _attest(reader, target, ref, pr)
            walk = _Walk(self._api, target, budget)
            content = walk.capture()
            _attest(reader, target, ref, reader.read_metadata(ref))
            budget.check_deadline()
            return content.snapshot(target, at)
        except _CaptureError as error:
            return unavailable_discussion(target, at, error.code)
        except GitHubReadError as error:
            code = (
                "limit_reached"
                if isinstance(error, GhReadFailure) and error.kind == "limit_reached"
                else "invalid_response"
                if str(error) == INVALID_RESPONSE
                or (
                    isinstance(error, GhReadFailure)
                    and error.kind == "invalid_response"
                )
                else "read_error"
            )
            return unavailable_discussion(target, at, code)
        except ValueError:
            return unavailable_discussion(target, at, "invalid_response")


def _attest(
    reader: GhPullRequestReader,
    target: DiscussionTarget,
    ref: PullRequestRef,
    pr: PullRequest,
) -> None:
    if (pr.ref, pr.target_branch, pr.head) != (
        ref,
        target.target_branch,
        target.comparison.head,
    ):
        raise _CaptureError("selection_changed")
    revision = reader.read_revision(pr)
    if (revision.target_branch, revision.merge_base, revision.head) != (
        target.target_branch,
        target.comparison.merge_base,
        target.comparison.head,
    ):
        raise _CaptureError("selection_changed")


class _Walk:
    def __init__(self, api: GhApi, target: DiscussionTarget, budget: _Budget) -> None:
        self.api = api
        self.target = target
        self.budget = budget
        self.content = _Contents()
        self.pr_id: str | None = None

    def query(self, document: str, variables: dict[str, object]) -> _Node:
        timeout, cap = self.budget.take(paging=True)
        try:
            result = self.api.query(
                self.target.host,
                document,
                variables | {"size": _PAGE_SIZE},
                timeout_seconds=timeout,
                output_limit=cap,
            )
        except GhInvocationError as error:
            raise _CaptureError(
                "limit_reached"
                if error.reason == "output"
                else "invalid_response"
                if error.reason == "encoding"
                else "read_error"
            ) from None
        if result.returncode or result.status is None or not 200 <= result.status < 300:
            raise _CaptureError("read_error")
        data = _node(json.loads(result.body))
        if "errors" in data and data["errors"] != []:
            raise _CaptureError("read_error")
        return _node(data["data"])

    def pr_connection(self, document: str, name: str, cursor: str | None) -> object:
        target = self.target
        data = self.query(
            document,
            {
                "owner": target.owner,
                "repo": target.repo,
                "number": target.number,
                "cursor": cursor,
            },
        )
        repo = _node(data["repository"])
        pr = _node(repo["pullRequest"])
        if (
            type(repo["databaseId"]) is not int
            or type(pr["number"]) is not int
            or (
                repo["databaseId"],
                repo["nameWithOwner"],
                pr["number"],
                pr["baseRefName"],
                pr["headRefOid"],
            )
            != (
                target.repository_id,
                f"{target.owner}/{target.repo}",
                target.number,
                target.target_branch,
                target.comparison.head,
            )
        ):
            raise _CaptureError("selection_changed")
        key = _id(pr["id"])
        if self.pr_id is not None and self.pr_id != key:
            raise _CaptureError("selection_changed")
        self.pr_id = key
        return pr[name]

    def reply_connection(self, thread_id: str, cursor: str | None) -> object:
        data = self.query(_REPLIES, {"thread": thread_id, "cursor": cursor})
        node = _node(data["node"])
        if node["id"] != thread_id:
            raise ValueError("Reply continuation came from another thread")
        return node["comments"]

    def items(
        self,
        fetch: Callable[[str | None], object],
        scope: DiscussionScope,
        cap: int,
        *,
        initial: object = None,
        thread_id: str | None = None,
    ) -> Iterator[_Node]:
        cursor = None
        cursors: set[str] = set()
        seen: set[str] = set()
        count = 0
        try:
            while True:
                connection = _node(
                    initial
                    if cursor is None and thread_id is not None
                    else fetch(cursor)
                )
                info = _node(connection["pageInfo"])
                nodes, more, end = (
                    connection["nodes"],
                    info["hasNextPage"],
                    info["endCursor"],
                )
                if (
                    not isinstance(nodes, list)
                    or len(nodes) > _PAGE_SIZE
                    or type(more) is not bool
                    or (not nodes and (more or end is not None))
                    or (
                        nodes
                        and (
                            not isinstance(end, str)
                            or not end
                            or len(end) > 1024
                            or end in cursors
                        )
                    )
                ):
                    raise ValueError("Invalid discussion page")
                page_nodes = tuple(_node(item) for item in nodes)
                ids = {_id(item["id"]) for item in page_nodes}
                if len(ids) != len(page_nodes) or ids & seen:
                    raise ValueError("Repeated discussion node")
                seen.update(ids)
                yield from page_nodes[: cap - count]
                count += len(page_nodes)
                if count > cap or (count == cap and more):
                    raise _CaptureError("limit_reached")
                if not more:
                    break
                assert isinstance(end, str)
                cursor = end
                cursors.add(end)
        except _CaptureError as error:
            if error.code == "selection_changed":
                raise
            self.content.omit(scope, error.code, thread_id)
        except TypeError, ValueError, KeyError, RecursionError:
            self.content.omit(scope, "invalid_response", thread_id)

    def capture(self) -> _Contents:
        # Walk independent PR connections before reply continuations consume their budget.
        threads = tuple(
            self.items(
                lambda cursor: self.pr_connection(_THREADS, "reviewThreads", cursor),
                "threads",
                ITEM_LIMIT,
            )
        )
        for data in self.items(
            lambda cursor: self.pr_connection(_REVIEWS, "reviews", cursor),
            "reviews",
            ITEM_LIMIT,
        ):
            self.store_item(data, review=True)
        for data in self.items(
            lambda cursor: self.pr_connection(_CONVERSATION, "comments", cursor),
            "conversation",
            ITEM_LIMIT,
        ):
            self.store_item(data, review=False)
        for data in threads:
            try:
                self.store_thread(data)
            except TypeError, ValueError, KeyError:
                self.content.omit("threads", "invalid_response")
        return self.content

    def store_item(self, data: _Node, *, review: bool) -> None:
        scope = "reviews" if review else "conversation"
        try:
            key, body = _id(data["id"]), _text(data["body"])
            if review:
                commit = data["commit"]
                item = DiscussionReview.model_validate(
                    {
                        "id": key,
                        "body": body,
                        "commit": None if commit is None else _node(commit)["oid"],
                    }
                )
                self.content.accept({key})
                self.content.reviews.append(item)
            else:
                item = DiscussionItem(id=key, body=body)
                self.content.accept({key})
                self.content.conversation.append(item)
        except _CaptureError as error:
            self.content.omit(scope, error.code)
        except TypeError, ValueError, KeyError:
            self.content.omit(scope, "invalid_response")

    def store_thread(self, data: _Node) -> None:
        thread_id = _id(data["id"])
        side = _thread_side(data)
        current = _location(_path(data["path"]), side, data["startLine"], data["line"])
        resolved, outdated = data["isResolved"], data["isOutdated"]
        if (
            type(resolved) is not bool
            or type(outdated) is not bool
            or thread_id in self.content.ids
        ):
            raise ValueError("Invalid thread identity or status")
        comments: dict[str, DiscussionComment] = {}
        permalink = None
        root_id = None
        for raw in self.items(
            lambda cursor: self.reply_connection(thread_id, cursor),
            "replies",
            REPLY_LIMIT,
            initial=data["comments"],
            thread_id=thread_id,
        ):
            try:
                if _node(raw["pullRequest"])["id"] != self.pr_id:
                    raise ValueError("Review comment came from another pull request")
                item = _comment(raw, side)
                if item.id == thread_id or item.id in self.content.ids:
                    raise ValueError("Repeated discussion comment")
                if item.parent_id is None:
                    if root_id is not None:
                        raise ValueError("Repeated thread root")
                    root_id = item.id
                    link = raw["url"]
                    permalink = (
                        link
                        if isinstance(link, str) and valid_permalink(self.target, link)
                        else None
                    )
                comments[item.id] = item
            except _CaptureError as error:
                self.content.omit("replies", error.code, thread_id)
            except TypeError, ValueError, KeyError:
                self.content.omit("replies", "invalid_response", thread_id)
        if root_id is None:
            self.content.omit("threads", "invalid_response")
            return
        rooted = rooted_comment_ids(comments, root_id)
        if rooted != comments.keys():
            self.content.omit("replies", "invalid_response", thread_id)
        thread = DiscussionThread(
            id=thread_id,
            is_resolved=resolved,
            is_outdated=outdated,
            current_location=current,
            permalink=permalink,
            comments=tuple(comments[key] for key in sorted(rooted)),
        )
        self.content.accept({thread_id, *rooted})
        self.content.threads.append(thread)


def _comment(data: _Node, root_side: RevisionSide | None) -> DiscussionComment:
    reply_to, commit = data["replyTo"], data["originalCommit"]
    parent = None if reply_to is None else _id(_node(reply_to)["id"])
    path = _path(data["path"])
    diff = data["diffHunk"]
    return DiscussionComment.model_validate(
        {
            "id": _id(data["id"]),
            "body": _text(data["body"]),
            "parent_id": parent,
            "original_commit": None if commit is None else _node(commit)["oid"],
            "original_location": _location(
                path,
                root_side if parent is None else None,
                data["originalStartLine"],
                data["originalLine"],
            ),
            "diff_hunk": None if diff is None else _text(diff),
        }
    )
