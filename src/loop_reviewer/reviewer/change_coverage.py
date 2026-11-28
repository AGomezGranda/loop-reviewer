"""Changed-line requirements and packet packing for pinned-text reviews."""

import json
from bisect import bisect_left
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from itertools import zip_longest

from loop_reviewer.reviewer.full_review_contract import text_eligible
from loop_reviewer.reviewer.inventory import RevisionSide
from loop_reviewer.reviewer.inventory_contract import (
    ChangedFile,
    FileSide,
    PinnedComparison,
)
from loop_reviewer.reviewer.local_check_contract import (
    SourceEvidence,
    SourceLocation,
    SourceRole,
)
from loop_reviewer.reviewer.local_evidence import (
    EvidenceIndex,
    OptionalRetrieval,
    PinnedEvidenceWork,
    WorkLimit,
)
from loop_reviewer.reviewer.pinned_source import (
    Hunk,
    ReadFailure,
    SourceRange,
    changed_lines,
)
from loop_reviewer.reviewer.semantic_inputs import (
    MAX_EVIDENCE_BYTES,
    MAX_FILES,
    BundleFile,
    EvidenceBundle,
    Omission,
    ShownExcerpt,
    canonical_inventory,
)
from loop_reviewer.reviewer.text_references import (
    ShownLines,
    TextReference,
    packet_references,
)


@dataclass(frozen=True)
class CoverageRange:
    file_id: str
    source: SourceRange

    def lines(self) -> Iterator[CoverageRange]:
        for line in range(self.source.start_line, self.source.end_line + 1):
            yield CoverageRange(
                self.file_id, replace(self.source, start_line=line, end_line=line)
            )


def _runs(lines: Iterator[int]) -> Iterator[tuple[int, int]]:
    start = end = next(lines, None)
    if start is None or end is None:
        return
    for line in lines:
        if line != end + 1:
            yield start, end
            start = line
        end = line
    yield start, end


def required_ranges(
    file: BundleFile, comparison: PinnedComparison, hunks: tuple[Hunk, ...]
) -> tuple[CoverageRange, ...]:
    ranges: list[CoverageRange] = []
    for hunk in hunks:
        for side, entry, commit in _pinned_sides(file.change, comparison):
            for start, end in _runs(changed_lines(hunk, side)):
                ranges.append(
                    CoverageRange(
                        file.id,
                        SourceRange(
                            side, commit, entry.path, entry.object_id, start, end
                        ),
                    )
                )
    return tuple(ranges)


def _pinned_sides(
    change: ChangedFile, comparison: PinnedComparison
) -> Iterator[tuple[RevisionSide, FileSide, str]]:
    if change.old is not None:
        yield "base", change.old, comparison.merge_base
    if change.new is not None:
        yield "head", change.new, comparison.head


@dataclass(frozen=True)
class ChangedLineExcerpt:
    """An exact, atomic line slice from prepared pinned evidence."""

    file_id: str
    source: SourceEvidence
    surrounding: tuple[SourceRange, ...] = ()
    hunk_index: int = 0

    def __post_init__(self) -> None:
        if self.source.range.start_line != self.source.range.end_line:
            raise ValueError("Expected exactly one changed line per slice")

    @property
    def coverage(self) -> CoverageRange:
        loc = self.source.range
        return CoverageRange(
            self.file_id,
            SourceRange(
                loc.side,
                loc.commit_id,
                loc.path,
                loc.blob_id,
                loc.start_line,
                loc.end_line,
            ),
        )


@dataclass(frozen=True)
class _PinnedContext:
    work: PinnedEvidenceWork
    index: EvidenceIndex

    def excerpts(self, shown: list[ChangedLineExcerpt]) -> Iterator[TextReference]:
        ranges: dict[SourceRange, dict[str, None]] = {}
        for item in shown:
            for nearby in item.surrounding:
                ranges.setdefault(nearby, {})[item.file_id] = None
        for location, file_ids in ranges.items():
            text = self.work.excerpt(location)
            if text and len(text.encode("utf-8")) <= 384:
                yield TextReference(location, text, tuple(file_ids))


@dataclass(frozen=True)
class PreparedCoverage:
    comparison: PinnedComparison
    inventory: tuple[BundleFile, ...]
    required: tuple[CoverageRange, ...]
    changed: tuple[ChangedLineExcerpt, ...]
    supporting: tuple[ShownExcerpt, ...] = ()
    omissions: tuple[Omission, ...] = ()
    context: _PinnedContext | None = None


def _source(identifier: str, location: SourceRange, text: str) -> SourceEvidence:
    return SourceEvidence(
        kind="source",
        id=identifier,
        role="check_location",
        range=SourceLocation.from_range(location),
        text=text,
    )


def _hunk_slices(
    hunk: Hunk,
    required: tuple[CoverageRange, ...],
    file: BundleFile,
    comparison: PinnedComparison,
) -> Iterator[tuple[CoverageRange, tuple[SourceRange, ...]]]:
    common: dict[RevisionSide, list[int]] = {}
    for side, bounds in (("base", hunk.old_span), ("head", hunk.new_span)):
        edited = {
            number
            for span in required
            if span.source.side == side
            for number in range(span.source.start_line, span.source.end_line + 1)
        }
        common[side] = [
            number
            for number in range(
                bounds.start_line, bounds.start_line + bounds.line_count
            )
            if number not in edited
        ]
    # Align paired edits and context by the count of unchanged records before each edit.
    blocks: dict[int, list[CoverageRange]] = {}
    for span in required:
        offset = bisect_left(common[span.source.side], span.source.start_line)
        blocks.setdefault(offset, []).append(span)
    sides = tuple(_pinned_sides(file.change, comparison))
    for offset, spans in sorted(blocks.items()):
        nearby = tuple(
            SourceRange(side, commit, entry.path, entry.object_id, number, number)
            for distance in range(3)
            for position in (offset - 1 - distance, offset + distance)
            for side, entry, commit in sides
            if 0 <= position < len(common[side])
            for number in (common[side][position],)
        )
        for pair in zip_longest(*(span.lines() for span in spans)):
            for line in pair:
                if line is not None:
                    yield line, nearby


def _pinned_hunks(
    change: ChangedFile, work: PinnedEvidenceWork
) -> tuple[Hunk, ...] | ReadFailure:
    if change.old and change.new and change.old.object_id == change.new.object_id:
        return ()
    entries = tuple(
        (side, entry) for side, entry, _ in _pinned_sides(change, work.comparison)
    )
    if change.oversized or not work.can_read_blobs(entries):
        return "limit_reached"
    patch = work.hunks(change, preflight_one_sided=True)
    if isinstance(patch, str) or not patch:
        return patch
    if not work.can_read_blobs(entries):
        return "limit_reached"
    for side, entry in entries:
        blob = work.blob(side, entry.path, expected=entry)
        if isinstance(blob, str):
            return blob
        if blob.text and not work.text_lines(blob):
            return "unavailable"
    return patch


def prepare_coverage(
    rows: tuple[ChangedFile, ...], work: PinnedEvidenceWork, index: EvidenceIndex
) -> PreparedCoverage:
    inventory = canonical_inventory(rows)
    required: list[CoverageRange] = []
    changed: list[ChangedLineExcerpt] = []
    omissions: list[Omission] = []
    for file in inventory:
        change = file.change
        if not text_eligible(change):
            omissions.append(Omission(file.id, "ineligible"))
            continue
        gap: Omission | None = None
        try:
            patch = _pinned_hunks(change, work)
            if isinstance(patch, str) or not patch:
                omissions.append(
                    Omission(
                        file.id,
                        "work_limit"
                        if patch == "limit_reached"
                        else "missing_evidence",
                    )
                )
                continue
            hunks = tuple(
                (hunk, required_ranges(file, work.comparison, (hunk,)))
                for hunk in patch
            )
            required.extend(span for _, ranges in hunks for span in ranges)
            for hunk_index, (hunk, ranges) in enumerate(hunks):
                for line, surrounding in _hunk_slices(
                    hunk, ranges, file, work.comparison
                ):
                    work.remaining()
                    location = line.source
                    text = work.excerpt(location)
                    if text is None:
                        gap = gap or Omission(file.id, "missing_evidence")
                        continue
                    identifier = index.source("check_location", location, text)
                    if identifier is None:
                        gap = Omission(file.id, "work_limit")
                        continue
                    changed.append(
                        ChangedLineExcerpt(
                            file.id,
                            _source(identifier, location, text),
                            surrounding,
                            hunk_index,
                        )
                    )
        except WorkLimit:
            gap = Omission(file.id, "work_limit")
        if gap is not None:
            omissions.append(gap)
    return PreparedCoverage(
        work.comparison,
        inventory,
        tuple(required),
        tuple(changed),
        omissions=tuple(omissions),
        context=_PinnedContext(work, index),
    )


@dataclass(frozen=True)
class CoveragePacket:
    bundle: EvidenceBundle
    offered: frozenset[CoverageRange]
    unsendable: frozenset[CoverageRange]


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_bytes(value: object) -> int:
    return len(_json(value).encode("utf-8"))


def build_coverage_packet(
    prepared: PreparedCoverage,
    unoffered: frozenset[CoverageRange],
    max_bytes: int,
    *,
    supporting_bytes: int | None = None,
    supporting_fits: Callable[[str], bool] | None = None,
    reference_bytes: int = 0,
    reference_deadline: float | None = None,
) -> CoveragePacket:
    limit = min(max_bytes, MAX_EVIDENCE_BYTES)
    files = {file.id: file for file in prepared.inventory}
    selected: dict[str, dict[str, object]] = {}
    changed: list[dict[str, object]] = []
    chosen: list[ChangedLineExcerpt] = []
    supporting: list[dict[str, object]] = []
    offered: set[CoverageRange] = set()
    unsendable: set[CoverageRange] = set()
    excerpts: dict[str, ShownExcerpt] = {}

    def payload() -> dict[str, object]:
        return {
            "comparison": prepared.comparison.model_dump(),
            "files": tuple(selected.values()),
            "selected_file_ids": tuple(selected),
            "changed_excerpts": changed,
            "supporting_excerpts": supporting,
        }

    overhead = used = _json_bytes(payload())
    metadata: dict[str, tuple[dict[str, object], int]] = {}
    for item in prepared.changed:
        key = item.file_id
        coverage = item.coverage
        if (
            coverage not in unoffered
            or coverage in offered
            or key not in files
            or item.source.id in excerpts
        ):
            continue
        record: dict[str, object] = {"file_id": key, **item.source.model_dump()}
        record_size = _json_bytes(record)
        if key not in metadata:
            projection = files[key].projection()
            metadata[key] = projection, _json_bytes(projection) + _json_bytes(key)
        projection, file_size = metadata[key]
        if overhead + record_size + file_size > limit:
            unsendable.add(coverage)
            continue
        fresh = key not in selected
        if fresh and len(selected) >= MAX_FILES:
            continue
        extra = record_size + int(bool(changed))
        if fresh:
            extra += file_size + 2 * int(bool(selected))
        if used + extra > limit:
            continue
        used += extra
        if fresh:
            selected[key] = projection
        changed.append(record)
        chosen.append(item)
        offered.add(coverage)
        excerpts[item.source.id] = ShownExcerpt(
            item.source, (key,), (coverage.source.start_line,)
        )

    support_limit = (
        limit if supporting_bytes is None else min(limit, used + supporting_bytes)
    )

    def fit(record: dict[str, object]) -> int:
        extra = _json_bytes(record) + int(bool(supporting))
        if used + extra > support_limit:
            return 0
        if supporting_fits is not None:
            supporting.append(record)
            fits = supporting_fits(_json(payload()))
            supporting.pop()
            if not fits:
                return 0
        return extra

    if chosen and used < support_limit:
        for item in prepared.supporting:
            associated = tuple(key for key in item.file_ids if key in selected)
            if not associated or item.source.id in excerpts:
                continue
            record = {"file_ids": associated, **item.source.model_dump()}
            if extra := fit(record):
                used += extra
                supporting.append(record)
                excerpts[item.source.id] = ShownExcerpt(item.source, associated)
    if chosen and (context := prepared.context) and used < support_limit:
        shown = ShownLines()
        for entry in excerpts.values():
            shown.add(entry.source.range, entry.file_ids)

        def add_context(ref: TextReference, role: SourceRole) -> None:
            nonlocal used
            if not shown.has_new_text(ref.span, ref.file_ids):
                return
            location = SourceLocation.from_range(ref.span)
            record: dict[str, object] = {
                "file_ids": ref.file_ids,
                "kind": "source",
                "id": "e00000",  # EvidenceIndex IDs have a fixed serialized width.
                "role": role,
                "range": location.model_dump(),
                "text": ref.text,
            }
            if not (extra := fit(record)):
                return
            identifier = context.index.source(role, ref.span, ref.text)
            if identifier is not None:
                used += extra
                record["id"] = identifier
                supporting.append(record)
                excerpts[identifier] = ShownExcerpt(
                    SourceEvidence(
                        kind="source",
                        id=identifier,
                        role=role,
                        range=location,
                        text=ref.text,
                    ),
                    ref.file_ids,
                )
                shown.add(ref.span, ref.file_ids)

        try:
            for ref in context.excerpts(chosen):
                add_context(ref, "check_location")
            if reference_bytes > 0 and used < support_limit:
                expected = {
                    (side, entry.path): entry
                    for file in prepared.inventory
                    for side, entry, _ in _pinned_sides(
                        file.change, prepared.comparison
                    )
                }
                references = packet_references(
                    ((item.file_id, item.hunk_index, item.source) for item in chosen),
                    shown,
                    expected,
                    context.work,
                    OptionalRetrieval(reference_bytes, reference_deadline),
                )
                for ref in references:
                    add_context(ref, "text_match")
        except WorkLimit:
            pass  # Optional context cannot invalidate the selected edits.

    bundle = EvidenceBundle(
        prepared.comparison,
        prepared.inventory,
        tuple(selected),
        tuple(excerpts.values()),
        prepared.omissions,
        _json(payload()) if offered else "",
    )
    return CoveragePacket(bundle, frozenset(offered), frozenset(unsendable))
