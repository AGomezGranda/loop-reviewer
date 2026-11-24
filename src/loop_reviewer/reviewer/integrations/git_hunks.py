"""Decode fixed-format Git patches without retaining file headers."""

import re

from loop_reviewer.reviewer.pinned_source import Hunk, LineSpan, ReadFailure, lf_lines

_HEADER = re.compile(
    rb"@@ -(\d{1,10})(?:,(\d{1,10}))? \+(\d{1,10})(?:,(\d{1,10}))? @@(?: [^\n]*)?\n"
)
_NO_NEWLINE = b"\\ No newline at end of file\n"
_BODY_SIDES = {b" ": (0, 1), b"-": (0,), b"+": (1,)}


def decode_hunks(patch: bytes) -> tuple[Hunk, ...]:
    hunks: list[Hunk] = []
    lines = lf_lines(patch)
    index = 0
    next_offsets = [0, 0]
    ended: set[int] = set()
    while index < len(lines):
        header = _HEADER.fullmatch(lines[index])
        if header is None:
            raise ValueError("Invalid hunk header")
        old_start, old_count, new_start, new_count = header.groups()
        spans = (
            LineSpan(int(old_start), int(old_count or b"1")),
            LineSpan(int(new_start), int(new_count or b"1")),
        )
        for side, span in enumerate(spans):
            offset = span.start_line - int(span.line_count > 0)
            if offset < next_offsets[side]:
                raise ValueError("Overlapping hunks")
            next_offsets[side] = offset + span.line_count
        start = index
        index += 1
        remaining = [span.line_count for span in spans]
        previous: tuple[int, ...] = ()
        changed = False
        while index < len(lines) and not lines[index].startswith(b"@@ "):
            line = lines[index]
            index += 1
            if line == _NO_NEWLINE and previous:
                ended.update(previous)
                previous = ()
                continue
            sides = _BODY_SIDES.get(line[:1])
            if (
                sides is None
                or not line.endswith(b"\n")
                or any(side in ended or remaining[side] == 0 for side in sides)
            ):
                raise ValueError("Invalid hunk body")
            for side in sides:
                remaining[side] -= 1
            changed |= len(sides) == 1
            previous = sides
        if any(remaining) or not changed:
            raise ValueError("Incomplete or unchanged hunk")
        normalized_header = _header(*spans)
        hunks.append(
            Hunk(*spans, normalized_header + b"".join(lines[start + 1 : index]))
        )
    return tuple(hunks)


def _header(old: LineSpan, new: LineSpan) -> bytes:
    def coordinates(span: LineSpan) -> str:
        if span.line_count == 1:
            return str(span.start_line)
        return f"{span.start_line},{span.line_count}"

    return f"@@ -{coordinates(old)} +{coordinates(new)} @@\n".encode("ascii")


def decode_blob_diff(
    output: bytes, old_id: str, new_id: str
) -> tuple[Hunk, ...] | ReadFailure:
    old, new = old_id.encode("ascii"), new_id.encode("ascii")
    prefix = b"diff --git a/" + old + b" b/" + new
    lines = output.split(b"\n", 4)
    if (
        len(lines) < 3
        or lines[0] != prefix
        or lines[1] != b"index " + old + b".." + new + b" 100644"
    ):
        raise ValueError("Invalid blob diff header")
    if lines[2] == b"Binary files a/" + old + b" and b/" + new + b" differ":
        return "unsupported"
    if (
        len(lines) != 5
        or lines[2:4] != [b"--- a/" + old, b"+++ b/" + new]
        or not lines[4]
    ):
        raise ValueError("Missing blob diff body")
    return decode_hunks(lines[4])


def one_sided_hunks(
    blob: bytes, *, added: bool, limit: int
) -> tuple[Hunk, ...] | ReadFailure:
    if not blob:
        return ()
    if b"\0" in blob[:8000]:
        return "unsupported"
    if len(blob) > limit:
        return "limit_reached"
    marker = b"+" if added else b"-"
    lines = lf_lines(blob)
    existing = LineSpan(1, len(lines))
    empty = LineSpan(0, 0)
    old, new = (empty, existing) if added else (existing, empty)
    patch = _header(old, new) + b"".join(marker + line for line in lines)
    if not blob.endswith(b"\n"):
        patch += b"\n" + _NO_NEWLINE
    if len(patch) > limit:
        return "limit_reached"
    return (Hunk(old, new, patch),)
