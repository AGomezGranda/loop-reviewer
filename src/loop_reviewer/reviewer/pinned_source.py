"""Read evidence from the two commits of a pinned local comparison."""

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Literal, Protocol

from loop_reviewer.reviewer.inventory import InventorySource, RevisionSide
from loop_reviewer.reviewer.inventory_contract import ChangedFile

ReadFailure = Literal["not_found", "unsupported", "limit_reached", "unavailable"]


def lf_lines(data: bytes) -> list[bytes]:
    *terminated, tail = data.split(b"\n")
    return [line + b"\n" for line in terminated] + ([tail] if tail else [])


@dataclass(frozen=True)
class SourceRange:
    side: RevisionSide
    commit_id: str
    path: str
    blob_id: str
    start_line: int
    end_line: int

    def __post_init__(self) -> None:
        if not 1 <= self.start_line <= self.end_line:
            raise ValueError("Source ranges are one-based and inclusive")


@dataclass(frozen=True)
class LineSpan:
    start_line: int
    line_count: int

    def __post_init__(self) -> None:
        if self.line_count < 0 or self.start_line < int(self.line_count > 0):
            raise ValueError("Invalid Git line span")


@dataclass(frozen=True)
class Hunk:
    old_span: LineSpan
    new_span: LineSpan
    patch: bytes


def changed_lines(hunk: Hunk, side: RevisionSide) -> Iterator[int]:
    line = hunk.old_span.start_line if side == "base" else hunk.new_span.start_line
    edit = b"-" if side == "base" else b"+"
    for record in lf_lines(hunk.patch)[1:]:
        marker = record[:1]
        if marker == edit:
            yield line
        if marker in (edit, b" "):
            line += 1


@dataclass(frozen=True)
class PinnedTextBlob:
    side: RevisionSide
    commit_id: str
    path: str
    blob_id: str
    text: bytes


@dataclass(frozen=True)
class SearchPaths:
    paths: tuple[str, ...]
    truncated: bool
    retrieved_bytes: int | None = field(default=None, compare=False)


class PinnedSource(InventorySource, Protocol):
    def hunks(
        self, change: ChangedFile, *, max_bytes: int, timeout_seconds: float
    ) -> tuple[Hunk, ...] | ReadFailure: ...

    def read_text_blob(
        self, side: RevisionSide, path: str, *, max_bytes: int, timeout_seconds: float
    ) -> PinnedTextBlob | ReadFailure: ...

    def search(
        self,
        side: RevisionSide,
        literal: str,
        *,
        max_paths: int,
        max_bytes: int,
        timeout_seconds: float,
        regular_text: bool = False,
    ) -> SearchPaths | ReadFailure: ...
