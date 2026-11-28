"""Changed-line requirements and packet packing for pinned-text reviews."""

import json
from collections.abc import Iterator
from dataclasses import dataclass, replace

from loop_reviewer.reviewer.inventory import RevisionSide
from loop_reviewer.reviewer.inventory_contract import PinnedComparison
from loop_reviewer.reviewer.local_check_contract import SourceEvidence
from loop_reviewer.reviewer.pinned_source import Hunk, SourceRange, changed_lines
from loop_reviewer.reviewer.semantic_inputs import (
    MAX_EVIDENCE_BYTES,
    MAX_FILES,
    BundleFile,
    EvidenceBundle,
    Omission,
    ShownExcerpt,
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
    sides: tuple[RevisionSide, ...] = ("base", "head")
    for hunk in hunks:
        for side in sides:
            entry = file.change.old if side == "base" else file.change.new
            if entry is None:
                continue
            commit = comparison.merge_base if side == "base" else comparison.head
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


@dataclass(frozen=True)
class ChangedLineExcerpt:
    """An exact, atomic line slice from prepared pinned evidence."""

    file_id: str
    source: SourceEvidence

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
class PreparedCoverage:
    comparison: PinnedComparison
    inventory: tuple[BundleFile, ...]
    required: tuple[CoverageRange, ...]
    changed: tuple[ChangedLineExcerpt, ...]
    supporting: tuple[ShownExcerpt, ...] = ()
    omissions: tuple[Omission, ...] = ()


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
    prepared: PreparedCoverage, unoffered: frozenset[CoverageRange], max_bytes: int
) -> CoveragePacket:
    limit = min(max_bytes, MAX_EVIDENCE_BYTES)
    files = {file.id: file for file in prepared.inventory}
    selected: dict[str, dict[str, object]] = {}
    changed: list[dict[str, object]] = []
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
    metadata = {key: file.projection() for key, file in files.items()}
    metadata_sizes = {
        key: _json_bytes(record) + _json_bytes(key) for key, record in metadata.items()
    }
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
        file_size = metadata_sizes[key]
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
            selected[key] = metadata[key]
        changed.append(record)
        offered.add(coverage)
        excerpts[item.source.id] = ShownExcerpt(
            item.source, (key,), (coverage.source.start_line,)
        )

    for item in prepared.supporting:
        associated = tuple(key for key in item.file_ids if key in selected)
        if not associated or item.source.id in excerpts:
            continue
        record = {"file_ids": associated, **item.source.model_dump()}
        extra = _json_bytes(record) + int(bool(supporting))
        if used + extra <= limit:
            used += extra
            supporting.append(record)
            excerpts[item.source.id] = ShownExcerpt(item.source, associated)
    bundle = EvidenceBundle(
        prepared.comparison,
        prepared.inventory,
        tuple(selected),
        tuple(excerpts.values()),
        prepared.omissions,
        _json(payload()) if offered else "",
    )
    return CoveragePacket(bundle, frozenset(offered), frozenset(unsendable))
