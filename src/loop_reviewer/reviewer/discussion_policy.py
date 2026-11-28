"""Map private discussion to pinned source and link only exact verified witnesses."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from loop_reviewer.reviewer.discussion_contract import (
    DiscussionSnapshotV1,
    DiscussionThread,
    capture_blocks_thread_link,
)
from loop_reviewer.reviewer.discussion_report import DiscussionKind, MappingOmission
from loop_reviewer.reviewer.full_review_contract import text_eligible
from loop_reviewer.reviewer.inventory import RevisionSide
from loop_reviewer.reviewer.inventory_contract import ChangedFile, FileSide, encode_path
from loop_reviewer.reviewer.local_check_contract import SourceEvidence
from loop_reviewer.reviewer.local_evidence import (
    LocalEvidenceWork,
    PinnedEvidenceWork,
    WorkLimit,
    python_sides,
)
from loop_reviewer.reviewer.pinned_source import SourceRange, changed_lines
from loop_reviewer.reviewer.semantic_policy import Finding

# Each attachment consumes an inspection, so this also bounds the reference count below 100.
_INSPECTIONS = 64
_SECONDS = 2
_FENCE = re.compile(
    r"^```(?:[a-zA-Z0-9_+-]+)?[ \t]*\r?\n(?P<quote>.*?)^```[ \t]*(?=\r?$)",
    re.MULTILINE | re.DOTALL,
)
_PATH_LINE = re.compile(r"(?:[Pp]ath:[ \t]*)?(?:`([^`\r\n]+)`|([^\s`]+))")
_HEADER = re.compile(
    rb"@@ -([0-9]{1,10})(?:,([0-9]{1,10}))? \+([0-9]{1,10})(?:,([0-9]{1,10}))? @@.*"
)
_TICKS = re.compile(r"(?<!`)(`{1,3})([^`\r\n]+)\1(?!`)")
_STATEMENTS = re.compile(r"[ \t]*\.[ \t]*(?:\n+|[ \t]+)|[ \t]*\n+[ \t]*")


@dataclass(frozen=True)
class Attachment:
    kind: DiscussionKind
    item_id: str
    location: SourceRange
    thread: DiscussionThread | None = field(default=None, repr=False)
    can_link: bool = False


@dataclass(frozen=True)
class DiscussionContext:
    by_file: Mapping[ChangedFile, tuple[Attachment, ...]]
    omissions: tuple[MappingOmission, ...] = ()

    @property
    def unresolved_paths(self) -> frozenset[ChangedFile]:
        return frozenset(
            row
            for row, items in self.by_file.items()
            if any(
                item.thread
                and not item.thread.is_resolved
                and not item.thread.is_outdated
                for item in items
            )
        )


@dataclass(frozen=True)
class ThreadMatch:
    thread_id: str
    finding: Finding


@dataclass(frozen=True)
class DiscussionDisposition:
    kept: tuple[Finding, ...]
    linked: tuple[ThreadMatch, ...]


@dataclass
class _Inspection:
    work: PinnedEvidenceWork
    left: int = _INSPECTIONS

    def eligible(self, row: ChangedFile) -> bool:
        if isinstance(self.work, LocalEvidenceWork):
            return not row.binary and bool(python_sides(row))
        return text_eligible(row)

    def take(self) -> None:
        self.work.remaining()
        if not self.left:
            raise WorkLimit
        self.left -= 1

    def matches(
        self, quote: bytes, side: RevisionSide, file: FileSide
    ) -> tuple[SourceRange, ...] | None:
        self.take()
        blob = self.work.blob(side, file.path, expected=file)
        if blob == "limit_reached":
            raise WorkLimit
        if isinstance(blob, str):
            return None
        text = b"\n" + blob.text.removesuffix(b"\n") + b"\n"
        needle = b"\n" + quote + b"\n"
        matches: list[SourceRange] = []
        offset = 0
        while len(matches) < 2 and (at := text.find(needle, offset)) != -1:
            start = text.count(b"\n", 0, at) + 1
            matches.append(
                SourceRange(
                    side,
                    blob.commit_id,
                    blob.path,
                    blob.blob_id,
                    start,
                    start + quote.count(b"\n"),
                )
            )
            offset = at + 1
        self.work.remaining()
        return tuple(matches)

    def changed(self, row: ChangedFile, loc: SourceRange, *, declaration: bool) -> bool:
        self.take()
        hunks = self.work.hunks(row)
        if hunks == "limit_reached":
            raise WorkLimit
        if isinstance(hunks, str):
            return False
        lines: set[int] = set()
        for hunk in hunks:
            span = hunk.old_span if loc.side == "base" else hunk.new_span
            edits = set(changed_lines(hunk, loc.side))
            if (
                edits
                and span.start_line
                <= loc.start_line
                <= loc.end_line
                < span.start_line + span.line_count
            ):
                return True
            if declaration:
                lines.update(edits)
        if not lines or not isinstance(self.work, LocalEvidenceWork):
            return False
        self.take()
        file = row.old if loc.side == "base" else row.new
        analysis = self.work.analysis(loc.side, loc.path, expected=file)
        if analysis == "limit_reached":
            raise WorkLimit
        return not isinstance(analysis, str) and any(
            d.start_line <= loc.start_line <= loc.end_line <= d.end_line
            and any(d.start_line <= line <= d.end_line for line in lines)
            for d in analysis.declarations
        )


def _fenced(body: str) -> tuple[bytes, ...]:
    return tuple(
        m["quote"].encode("utf-8").removesuffix(b"\n") for m in _FENCE.finditer(body)
    )


def _hunk_quote(hunk: str, side: RevisionSide, start: int, end: int) -> bytes | None:
    header, *body = hunk.encode("utf-8").removesuffix(b"\n").split(b"\n")
    match = _HEADER.fullmatch(header)
    if match is None:
        return None
    group = 1 if side == "base" else 3
    line = int(match[group])
    if not line <= start <= end < line + int(match[group + 1] or b"1"):
        return None
    lines: list[bytes] = []
    own, other = (b"-", b"+") if side == "base" else (b"+", b"-")
    for raw in body:
        marker = raw[:1]
        if marker == other or raw == b"\\ No newline at end of file":
            continue
        if marker not in (own, b" "):
            return None
        if start <= line <= end:
            lines.append(raw[1:])
        line += 1
    return b"\n".join(lines) if len(lines) == end - start + 1 else None


def _indexes(
    rows: tuple[ChangedFile, ...],
) -> tuple[
    dict[tuple[RevisionSide, str], ChangedFile],
    dict[str, set[ChangedFile]],
]:
    by_side: dict[tuple[RevisionSide, str], ChangedFile] = {}
    by_path: dict[str, set[ChangedFile]] = {}
    for row in rows:
        for side, file in (("base", row.old), ("head", row.new)):
            if file is not None:
                by_side[side, file.path] = row
                by_path.setdefault(file.path, set()).add(row)
    return by_side, by_path


def _unique_on_pair(
    inspect: _Inspection, row: ChangedFile, quote: bytes
) -> SourceRange | None:
    match = None
    for side, file in (("base", row.old), ("head", row.new)):
        if file is None:
            continue
        found = inspect.matches(quote, side, file)
        if found is None or len(found) > 1 or (found and match is not None):
            return None
        if found:
            (match,) = found
    return match


def _only_row(rows: set[ChangedFile] | None) -> ChangedFile | None:
    return next(iter(rows)) if rows and len(rows) == 1 else None


def _anchored(
    thread: DiscussionThread,
    inspect: _Inspection,
    by_side: Mapping[tuple[RevisionSide, str], ChangedFile],
    by_path: Mapping[str, set[ChangedFile]],
) -> tuple[ChangedFile, SourceRange, bool] | None:
    root = thread.root
    original = root.original_location
    if (
        original is None
        or original.path is None
        or original.side is None
        or original.end_line is None
        or root.original_commit is None
    ):
        return None
    side, path, end = original.side, original.path, original.end_line
    start = original.start_line or end
    excerpts = _fenced(root.body)
    if len(excerpts) > 1:
        return None
    hunk = (
        _hunk_quote(root.diff_hunk, side, start, end)
        if root.diff_hunk is not None
        else None
    )
    if hunk is not None and excerpts and excerpts[0] != hunk:
        return None
    quote = hunk if hunk is not None else excerpts[0] if excerpts else b""
    if not quote.strip() or quote.count(b"\n") != end - start:
        return None
    pins = inspect.work.comparison
    if root.original_commit in (pins.merge_base, pins.head):
        # GitHub's original comment commit can be the head for a deletion-side line.
        if side == "head" and root.original_commit != pins.head:
            return None
        deletion_side = side == "base" and root.original_commit == pins.head
        if deletion_side and hunk is None:
            return None
        row = by_side.get((side, path))
        if row is None or not inspect.eligible(row):
            return None
        file = row.old if side == "base" else row.new
        assert file is not None
        matches = inspect.matches(quote, side, file)
        if (
            matches
            and len(matches) == 1
            and (matches[0].start_line, matches[0].end_line)
            == (
                start,
                end,
            )
            and (
                not deletion_side or inspect.changed(row, matches[0], declaration=False)
            )
        ):
            return row, matches[0], True
        return None
    row = _only_row(by_path.get(path))
    if row is None or not inspect.eligible(row):
        return None
    location = _unique_on_pair(inspect, row, quote)
    return (
        (row, location, False)
        if location and inspect.changed(row, location, declaration=True)
        else None
    )


def _unanchored(
    body: str, inspect: _Inspection, by_path: Mapping[str, set[ChangedFile]]
) -> tuple[ChangedFile, SourceRange] | None:
    fences = tuple(_FENCE.finditer(body))
    if len(fences) != 1:
        return None
    (fence,) = fences
    preceding = body[: fence.start()].rstrip().splitlines()
    path_line = _PATH_LINE.fullmatch(preceding[-1].strip()) if preceding else None
    quote = fence["quote"].encode("utf-8").removesuffix(b"\n")
    if path_line is None or not quote.strip():
        return None
    path = path_line[1] or path_line[2]
    possibilities = by_path.get(path, set()) | by_path.get(
        encode_path(path.encode("utf-8")), set()
    )
    row = _only_row(possibilities)
    if row is None or not inspect.eligible(row):
        return None
    location = _unique_on_pair(inspect, row, quote)
    return (
        (row, location)
        if location and inspect.changed(row, location, declaration=False)
        else None
    )


def map_discussion(
    snapshot: DiscussionSnapshotV1,
    rows: tuple[ChangedFile, ...],
    work: PinnedEvidenceWork,
) -> DiscussionContext:
    by_side, by_path = _indexes(rows)
    attachments: dict[ChangedFile, list[Attachment]] = {}
    inspect = _Inspection(work)
    omissions: tuple[MappingOmission, ...] = ()
    deadline = work.deadline
    work.deadline = min(deadline, work.monotonic() + _SECONDS)
    try:
        for thread in snapshot.threads:
            work.remaining()
            anchored = _anchored(thread, inspect, by_side, by_path)
            work.remaining()
            if anchored is not None:
                row, location, pinned = anchored
                can_link = (
                    pinned
                    and not thread.is_resolved
                    and not thread.is_outdated
                    and thread.permalink is not None
                    and len(thread.comments) == 1
                    and not capture_blocks_thread_link(snapshot.omissions, thread.id)
                )
                attachments.setdefault(row, []).append(
                    Attachment("thread", thread.id, location, thread, can_link)
                )
        for items, kind in (
            (snapshot.reviews, "review"),
            (snapshot.conversation, "conversation"),
        ):
            for item in items:
                work.remaining()
                unanchored = _unanchored(item.body, inspect, by_path)
                work.remaining()
                if unanchored is not None:
                    row, location = unanchored
                    attachments.setdefault(row, []).append(
                        Attachment(kind, item.id, location)
                    )
    except WorkLimit:
        omissions = (MappingOmission(scope="mapping", code="limit_reached"),)
    finally:
        work.deadline = deadline
    return DiscussionContext(
        {row: tuple(attachments[row]) for row in rows if row in attachments}, omissions
    )


def _statement(text: str) -> str:
    return " ".join(_TICKS.sub(r"\2", text).strip().removesuffix(".").split())


def _matches_claim(thread: DiscussionThread, finding: Finding) -> bool:
    producer_path, producer_name, caller_path, caller_name, exception = (
        finding.equivalence
    )
    producer = f"{producer_path.removesuffix('.py')}.{producer_name}()"
    caller = f"{caller_path.removesuffix('.py')}.{caller_name}()"
    body = _FENCE.sub("", thread.root.body).strip()
    statements = tuple(map(_statement, _STATEMENTS.split(body)))
    if len(statements) != 3:
        return False
    # The existing source witness proves the canonical return value, caller and exception.
    verified = re.fullmatch(
        rf"{re.escape(producer)} returns (\[\]|\(\)) instead of None",
        _statement(finding.candidate.changed_behavior),
    )
    return (
        verified is not None
        and re.fullmatch(
            rf"{re.escape(producer)} (?i:returns) {re.escape(verified[1])} (?i:instead of) None",
            statements[0],
        )
        is not None
        and re.fullmatch(rf"(?i:call) {re.escape(caller)}", statements[1]) is not None
        and re.fullmatch(
            rf"{re.escape(caller)} (?i:raises) {re.escape(exception)}", statements[2]
        )
        is not None
    )


def _covers_witness(
    attachment: Attachment, finding: Finding, proof: Mapping[str, SourceEvidence]
) -> bool:
    loc = attachment.location
    producer = finding.equivalence[0]
    return loc.path == producer and any(
        (e.range.side, e.range.commit_id, e.range.path, e.range.blob_id)
        == (loc.side, loc.commit_id, loc.path, loc.blob_id)
        and loc.start_line <= e.range.start_line == e.range.end_line <= loc.end_line
        for key in finding.evidence_ids
        if (e := proof.get(key)) is not None
    )


def link_verified_threads(
    findings: tuple[Finding, ...],
    context: DiscussionContext,
    evidence: tuple[SourceEvidence, ...],
) -> DiscussionDisposition:
    proof = {item.id: item for item in evidence}
    threads = sorted(
        (item for items in context.by_file.values() for item in items if item.can_link),
        key=lambda item: item.item_id,
    )
    kept: list[Finding] = []
    links: list[ThreadMatch] = []
    used: set[str] = set()
    for finding in findings:
        if not finding.python_witness:
            kept.append(finding)
            continue
        for item in threads:
            if (
                item.item_id not in used
                and item.thread is not None
                and _covers_witness(item, finding, proof)
                and _matches_claim(item.thread, finding)
            ):
                links.append(ThreadMatch(item.item_id, finding))
                used.add(item.item_id)
                break
        else:
            kept.append(finding)
    return DiscussionDisposition(tuple(kept), tuple(links))
