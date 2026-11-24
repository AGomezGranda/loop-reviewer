"""Bounded private observations of prior discussion on a pinned GitHub PR."""

import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from loop_reviewer.reviewer.inventory_contract import (
    PinnedComparison,
    _Count,
    _ObjectId,
    _Path,
    _Record,
    _Version,
    canonical_path,
)
from loop_reviewer.source_names import validate_repository, validate_target_branch

SNAPSHOT_BYTES = 512 * 1024
ITEM_BYTES = 4096
ITEM_LIMIT = 100
REPLY_LIMIT = 50
OMISSION_LIMIT = 3 * ITEM_LIMIT + 15
DiscussionId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_+=:/.-]{1,256}$")]
Permalink = Annotated[str, Field(max_length=1024)]
_Body = Annotated[str, Field(max_length=ITEM_BYTES)]
_Line = Annotated[int, Field(ge=1, le=2**31 - 1)]
DiscussionStatus = Literal["complete", "partial", "unavailable"]
DiscussionScope = Literal["identity", "threads", "replies", "reviews", "conversation"]
OmissionCode = Literal[
    "read_error", "invalid_response", "limit_reached", "selection_changed"
]


class DiscussionTarget(_Record):
    host: Annotated[str, Field(max_length=253)]
    repository_id: Annotated[_Count, Field(gt=0)]
    owner: Annotated[str, Field(max_length=100)]
    repo: Annotated[str, Field(max_length=100)]
    number: Annotated[int, Field(ge=1, le=2**31 - 1)]
    target_branch: Annotated[str, Field(min_length=1, max_length=1024)]
    comparison: PinnedComparison

    @property
    def url(self) -> str:
        return f"https://{self.host}/{self.owner}/{self.repo}/pull/{self.number}"

    @model_validator(mode="after")
    def canonical_identity(self) -> Self:
        validate_repository(self.host, self.owner, self.repo)
        if self.host != self.host.lower():
            raise ValueError("Expected a canonical lowercase host")
        validate_target_branch(self.target_branch)
        return self


def valid_permalink(target: DiscussionTarget, link: str) -> bool:
    return (
        re.fullmatch(re.escape(target.url) + r"#discussion_r[1-9][0-9]{0,19}", link)
        is not None
    )


class DiscussionLocation(_Record):
    path: _Path | None
    side: Literal["base", "head"] | None
    start_line: _Line | None
    end_line: _Line | None

    @model_validator(mode="after")
    def valid_location(self) -> Self:
        if self.path is not None:
            canonical_path(self.path)
        if (
            self.start_line is not None
            and self.end_line is not None
            and self.end_line < self.start_line
        ):
            raise ValueError("Invalid discussion line range")
        return self


class DiscussionItem(_Record):
    id: DiscussionId
    body: _Body

    @model_validator(mode="after")
    def bounded_body(self) -> Self:
        if len(self.body.encode("utf-8")) > ITEM_BYTES:
            raise ValueError("Discussion body exceeds byte cap")
        return self


class DiscussionReview(DiscussionItem):
    commit: _ObjectId | None


class DiscussionComment(DiscussionItem):
    parent_id: DiscussionId | None
    original_commit: _ObjectId | None
    original_location: DiscussionLocation | None
    diff_hunk: _Body | None

    @model_validator(mode="after")
    def bounded_hunk(self) -> Self:
        if (
            self.diff_hunk is not None
            and len(self.diff_hunk.encode("utf-8")) > ITEM_BYTES
        ):
            raise ValueError("Discussion hunk exceeds byte cap")
        return self


def rooted_comment_ids(
    comments: Mapping[str, DiscussionComment], root_id: str
) -> set[str]:
    rooted = {root_id}
    while children := {
        key
        for key, item in comments.items()
        if key not in rooted and item.parent_id in rooted
    }:
        rooted.update(children)
    return rooted


class DiscussionThread(_Record):
    id: DiscussionId
    is_resolved: bool
    is_outdated: bool
    current_location: DiscussionLocation | None
    permalink: Permalink | None
    comments: Annotated[
        tuple[DiscussionComment, ...], Field(min_length=1, max_length=REPLY_LIMIT)
    ]

    @property
    def root(self) -> DiscussionComment:
        return next(item for item in self.comments if item.parent_id is None)

    @model_validator(mode="after")
    def closed_thread(self) -> Self:
        ids = {item.id: item for item in self.comments}
        if tuple(ids) != tuple(sorted(item.id for item in self.comments)) or len(
            ids
        ) != len(self.comments):
            raise ValueError("Expected distinct comments in ID order")
        roots = [item for item in self.comments if item.parent_id is None]
        if len(roots) != 1:
            raise ValueError("Expected one root comment")
        if rooted_comment_ids(ids, roots[0].id) != ids.keys():
            raise ValueError("Reply does not resolve to this thread's root")
        return self


class CaptureOmission(_Record):
    scope: DiscussionScope
    code: OmissionCode
    thread_id: DiscussionId | None

    @model_validator(mode="after")
    def scoped_omission(self) -> Self:
        if self.thread_id is not None and self.scope != "replies":
            raise ValueError("Only reply omissions may name a thread")
        if self.code == "selection_changed" and self.scope != "identity":
            raise ValueError("Selection drift is an identity omission")
        return self


def omission_key(item: CaptureOmission) -> tuple[str, str, str]:
    return item.scope, item.thread_id or "", item.code


def capture_blocks_thread_link(
    omissions: tuple[CaptureOmission, ...], thread_id: str
) -> bool:
    return any(
        (item.scope == "threads" and item.code == "invalid_response")
        or (item.scope == "replies" and item.thread_id in (None, thread_id))
        for item in omissions
    )


def validate_capture(
    captured_at: datetime,
    status: DiscussionStatus,
    omissions: tuple[CaptureOmission, ...],
) -> None:
    if captured_at.utcoffset() != timedelta(0):
        raise ValueError("Expected a UTC capture timestamp")
    keys = tuple(map(omission_key, omissions))
    if keys != tuple(sorted(set(keys))) or (status == "complete") == bool(omissions):
        raise ValueError("Capture status and omissions disagree")
    if status != "unavailable" and any(item.scope == "identity" for item in omissions):
        raise ValueError("Unattested identity invalidates the capture")


class DiscussionSnapshotV1(_Record):
    schema_version: _Version
    target: DiscussionTarget
    captured_at: datetime
    status: DiscussionStatus
    omissions: Annotated[tuple[CaptureOmission, ...], Field(max_length=OMISSION_LIMIT)]
    threads: Annotated[tuple[DiscussionThread, ...], Field(max_length=ITEM_LIMIT)]
    reviews: Annotated[tuple[DiscussionReview, ...], Field(max_length=ITEM_LIMIT)]
    conversation: Annotated[tuple[DiscussionItem, ...], Field(max_length=ITEM_LIMIT)]

    @model_validator(mode="after")
    def closed_snapshot(self) -> Self:
        validate_capture(self.captured_at, self.status, self.omissions)
        has_content = bool(self.threads or self.reviews or self.conversation)
        if (self.status == "partial" and not has_content) or (
            self.status == "unavailable" and has_content
        ):
            raise ValueError("Capture status and available content disagree")
        ids = set[str]()
        for collection in (self.threads, self.reviews, self.conversation):
            keys = tuple(item.id for item in collection)
            if keys != tuple(sorted(keys)):
                raise ValueError("Expected canonical discussion ID order")
            for item in collection:
                if item.id in ids:
                    raise ValueError("Duplicate discussion ID")
                ids.add(item.id)
                if isinstance(item, DiscussionThread):
                    if item.permalink is not None and not valid_permalink(
                        self.target, item.permalink
                    ):
                        raise ValueError("Invalid root permalink")
                    for comment in item.comments:
                        if comment.id in ids:
                            raise ValueError("Duplicate discussion ID")
                        ids.add(comment.id)
        if len(self.model_dump_json().encode("utf-8")) > SNAPSHOT_BYTES:
            raise ValueError("Snapshot exceeds byte cap")
        return self


def unavailable_discussion(
    target: DiscussionTarget, at: datetime, code: OmissionCode
) -> DiscussionSnapshotV1:
    return DiscussionSnapshotV1(
        schema_version=1,
        target=target,
        captured_at=at,
        status="unavailable",
        omissions=(CaptureOmission(scope="identity", code=code, thread_id=None),),
        threads=(),
        reviews=(),
        conversation=(),
    )
