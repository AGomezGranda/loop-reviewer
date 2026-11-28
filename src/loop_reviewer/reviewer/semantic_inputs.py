"""Select bounded pinned evidence without giving the investigator retrieval tools."""

import json
from dataclasses import dataclass, replace
from typing import Literal

from loop_reviewer.reviewer.full_review_contract import text_eligible
from loop_reviewer.reviewer.inventory import RevisionSide
from loop_reviewer.reviewer.inventory_contract import (
    ChangedFile,
    FileSide,
    PinnedComparison,
    decode_path,
)
from loop_reviewer.reviewer.local_check_contract import SourceEvidence, SourceLocation
from loop_reviewer.reviewer.local_evidence import (
    ContextSelection,
    EvidenceIndex,
    LocalEvidenceWork,
    WorkLimit,
    collect_python_context,
    python_sides,
)
from loop_reviewer.reviewer.pinned_source import (
    Hunk,
    LineSpan,
    SourceRange,
    changed_lines,
    lf_lines,
)
from loop_reviewer.reviewer.python_context import PythonAnalysis
from loop_reviewer.reviewer.text_references import literal_references

MAX_FILES = 16
MAX_EVIDENCE_BYTES = 64 * 1024


def _serialize(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _bytes(text: str) -> int:
    return len(text.encode("utf-8"))


@dataclass(frozen=True)
class BundleFile:
    id: str
    change: ChangedFile

    @property
    def path(self) -> str:
        side = self.change.new or self.change.old
        assert side is not None
        return side.path

    @property
    def eligible(self) -> bool:
        return not self.change.binary and bool(python_sides(self.change))

    def projection(self) -> dict[str, object]:
        return {
            "id": self.id,
            **self.change.model_dump(
                exclude={
                    "old": {"object_id", "blob_bytes"},
                    "new": {"object_id", "blob_bytes"},
                }
            ),
        }


@dataclass(frozen=True)
class ShownExcerpt:
    source: SourceEvidence
    file_ids: tuple[str, ...]
    changed_lines: tuple[int, ...] = ()


@dataclass(frozen=True)
class Omission:
    file_id: str
    reason: Literal["ineligible", "not_scheduled", "missing_evidence", "work_limit"]


@dataclass(frozen=True)
class EvidenceBundle:
    comparison: PinnedComparison
    inventory: tuple[BundleFile, ...]
    selected_ids: tuple[str, ...]
    excerpts: tuple[ShownExcerpt, ...]
    omissions: tuple[Omission, ...]
    evidence_text: str
    complete_ids: frozenset[str] = frozenset()

    @property
    def evidence_bytes(self) -> int:
        return _bytes(self.evidence_text)

    @property
    def shown_ids(self) -> frozenset[str]:
        return frozenset(excerpt.source.id for excerpt in self.excerpts)


def _inventory(rows: tuple[ChangedFile, ...]) -> tuple[BundleFile, ...]:
    files = (BundleFile(f"p{n:05d}", row) for n, row in enumerate(rows, 1))
    return tuple(
        sorted(
            files,
            key=lambda file: (
                decode_path(file.path),
                decode_path(file.change.old.path) if file.change.old else b"",
            ),
        )
    )


def canonical_inventory(rows: tuple[ChangedFile, ...]) -> tuple[BundleFile, ...]:
    return tuple(
        BundleFile(f"p{n:05d}", file.change)
        for n, file in enumerate(_inventory(rows), 1)
    )


def _location(location: SourceLocation) -> SourceRange:
    return SourceRange(
        location.side,
        location.commit_id,
        location.path,
        location.blob_id,
        location.start_line,
        location.end_line,
    )


def _changed(
    entry: SourceEvidence, file: BundleFile, patch: tuple[Hunk, ...]
) -> tuple[int, ...]:
    location = entry.range
    side = file.change.old if location.side == "base" else file.change.new
    if side is None or (side.path, side.object_id) != (location.path, location.blob_id):
        return ()
    return tuple(
        line
        for hunk in patch
        for line in changed_lines(hunk, location.side)
        if location.start_line <= line <= location.end_line
    )


def _regular_sides(change: ChangedFile) -> tuple[tuple[RevisionSide, FileSide], ...]:
    return tuple(
        (side, entry)
        for side in ("base", "head")
        if (entry := change.old if side == "base" else change.new) is not None
    )


def _fallback(
    file: BundleFile,
    patch: tuple[Hunk, ...],
    work: LocalEvidenceWork,
    evidence: EvidenceIndex,
    full_sides: frozenset[RevisionSide],
    *,
    full: bool = False,
) -> tuple[SourceEvidence, ...]:
    entries: list[SourceEvidence] = []
    sides = _regular_sides(file.change) if full else python_sides(file.change)
    for side, entry in sides:
        blob = work.blob(side, entry.path, expected=entry)
        if isinstance(blob, str):
            continue
        for hunk in patch[:4]:
            span = hunk.old_span if side == "base" else hunk.new_span
            if not span.line_count:
                continue
            ranges: list[tuple[int, int]] = []
            if side in full_sides:
                ranges.append((span.start_line, span.start_line + span.line_count - 1))
            line = next(changed_lines(hunk, side), None)
            if line is not None:
                ranges.append((line, line))
            for start, end in dict.fromkeys(ranges):
                where = SourceRange(
                    side, blob.commit_id, blob.path, blob.blob_id, start, end
                )
                text = work.excerpt(where)
                identifier = evidence.source("check_location", where, text)
                if identifier is not None and text is not None:
                    entries.append(
                        SourceEvidence(
                            kind="source",
                            id=identifier,
                            role="check_location",
                            range=SourceLocation.from_range(where),
                            text=text,
                        )
                    )
    return tuple(entries)


def _enclosing_caller(
    entry: SourceEvidence, work: LocalEvidenceWork, index: EvidenceIndex
) -> SourceEvidence:
    if entry.role not in ("call_site", "test_call"):
        return entry
    loc = entry.range
    analysis = work.analyses.get((loc.side, loc.path))
    if analysis is None or isinstance(analysis, str):
        return entry
    calls = [
        call
        for call in analysis.calls
        if loc.start_line <= call.start_line <= call.end_line <= loc.end_line
    ]
    if len(calls) != 1:
        return entry
    call = calls[0]
    scopes = [
        d
        for d in analysis.declarations
        if d.kind != "class"
        and d.start_line <= call.start_line <= call.end_line <= d.end_line
        and d.end_line - d.start_line < 120
    ]
    if not scopes:
        return entry
    scope = min(scopes, key=lambda d: (d.end_line - d.start_line, d.start_line))
    if loc.start_line <= scope.start_line and loc.end_line >= scope.end_line:
        return entry
    start, end = (
        min(loc.start_line, scope.start_line),
        max(loc.end_line, scope.end_line),
    )
    if end - start >= 120:
        return entry
    source_range = replace(_location(loc), start_line=start, end_line=end)
    excerpt = work.excerpt(source_range)
    identifier = index.source(entry.role, source_range, excerpt)
    if identifier is None or excerpt is None:
        return entry
    return SourceEvidence(
        kind="source",
        id=identifier,
        role=entry.role,
        range=SourceLocation.from_range(source_range),
        text=excerpt,
    )


@dataclass(frozen=True)
class _Target:
    file: BundleFile
    excerpts: tuple[ShownExcerpt, ...]
    rank: int
    position: int
    context_gap: Omission | None
    patch: tuple[Hunk, ...]


def _target(
    file: BundleFile,
    work: LocalEvidenceWork,
    evidence: EvidenceIndex,
    selection: ContextSelection | None,
    *,
    full: bool = False,
) -> _Target | None:
    work.remaining()
    patch = work.hunks(file.change)
    if patch == "limit_reached":
        raise WorkLimit
    if isinstance(patch, str) or not patch:
        return None
    if full:
        for side, entry in _regular_sides(file.change):
            blob = work.blob(side, entry.path, expected=entry)
            if blob == "limit_reached":
                raise WorkLimit
            if isinstance(blob, str) or (blob.text and not work.text_lines(blob)):
                return None
    analyses: dict[RevisionSide, PythonAnalysis] = {}
    for side, entry in python_sides(file.change):
        analysis = work.analysis(side, entry.path, expected=entry)
        if not isinstance(analysis, str):
            analyses[side] = analysis
    if selection is None:
        selection = collect_python_context(file.change, analyses, work, evidence)
    included = set(selection.evidence_ids)
    context = [
        entry
        for entry in evidence.entries
        if isinstance(entry, SourceEvidence)
        and entry.id in included
        and entry.role != "text_match"
        and work.excerpt(_location(entry.range)) == entry.text
    ]
    excerpts = [
        ShownExcerpt(entry, (file.id,), _changed(entry, file, patch))
        for entry in (_enclosing_caller(item, work, evidence) for item in context)
    ]
    backed_sides = {
        item.source.range.side
        for item in excerpts
        if item.source.role in ("base_declaration", "head_declaration")
    }
    full_sides = frozenset(
        side for side, _ in python_sides(file.change) if side not in backed_sides
    )
    excerpts.extend(
        ShownExcerpt(entry, (file.id,), _changed(entry, file, patch))
        for entry in _fallback(
            file,
            patch,
            work,
            evidence,
            frozenset(("base", "head")) if full else full_sides,
            full=full,
        )
    )
    position = min(
        (line for item in excerpts for line in item.changed_lines), default=None
    )
    if position is None:
        return None
    if full and not analyses:
        seeds = tuple(item.source for item in excerpts if item.changed_lines)
        try:
            references = literal_references(
                seeds, work, evidence, sides=("base", "head")
            )
        except WorkLimit:
            references = ()
        excerpts.extend(ShownExcerpt(entry, (file.id,)) for entry in references)
    has_reference = any(entry.role in ("call_site", "test_call") for entry in context)
    has_declaration = any(
        entry.role in ("base_declaration", "head_declaration")
        and any(
            declaration.start_line <= entry.range.start_line <= declaration.end_line
            for declaration in analyses[entry.range.side].declarations
        )
        for entry in context
        if entry.range.side in analyses
    )
    return _Target(
        file,
        tuple(excerpts),
        0 if has_reference else 1 if has_declaration else 2,
        position,
        Omission(file.id, "work_limit")
        if selection.status == "limited"
        else Omission(file.id, "missing_evidence")
        if selection.status == "unavailable"
        else None,
        patch,
    )


def _covers_legacy_span(
    loc: SourceLocation, side: RevisionSide, file: FileSide, span: LineSpan
) -> bool:
    return (
        loc.side == side
        and (loc.path, loc.blob_id) == (file.path, file.object_id)
        and loc.start_line <= span.start_line
        and loc.end_line >= span.start_line + span.line_count - 1
    )


def _all_hunks_presented(target: _Target, shown: dict[str, ShownExcerpt]) -> bool:
    for hunk in target.patch:
        for side, span, file in (
            ("base", hunk.old_span, target.file.change.old),
            ("head", hunk.new_span, target.file.change.new),
        ):
            if not span.line_count:
                continue
            if file is None or not any(
                item.source.id in shown
                and _covers_legacy_span(item.source.range, side, file, span)
                for item in target.excerpts
            ):
                return False
    return True


def build_full_bundle(
    comparison: PinnedComparison,
    rows: tuple[ChangedFile, ...],
    work: LocalEvidenceWork,
    evidence: EvidenceIndex,
    max_bytes: int,
    *,
    skip_ids: frozenset[str] = frozenset(),
    discussion_paths: frozenset[ChangedFile] = frozenset(),
) -> EvidenceBundle:
    return build_targeted_bundle(
        comparison,
        rows,
        work,
        evidence,
        max_bytes,
        discussion_paths=discussion_paths,
        full=True,
        skip_ids=skip_ids,
    )


def build_targeted_bundle(
    comparison: PinnedComparison,
    rows: tuple[ChangedFile, ...],
    work: LocalEvidenceWork,
    evidence: EvidenceIndex,
    max_bytes: int,
    *,
    context: dict[ChangedFile, ContextSelection] | None = None,
    settled: frozenset[ChangedFile] = frozenset(),
    discussion_paths: frozenset[ChangedFile] = frozenset(),
    full: bool = False,
    skip_ids: frozenset[str] = frozenset(),
) -> EvidenceBundle:
    limit = min(max_bytes, MAX_EVIDENCE_BYTES)
    inventory = canonical_inventory(rows)
    omissions: dict[str, Omission] = {}
    targets: list[_Target] = []
    # Cheap metadata schedules reads only; final selection uses the evidence rank below.
    read_order = sorted(
        inventory,
        key=lambda f: any(
            side.generated == "marked"
            for side in (f.change.old, f.change.new)
            if side is not None
        ),
    )
    for file in read_order:
        reason: Omission | None = None
        if file.id in skip_ids:
            continue
        if not (text_eligible(file.change) if full else file.eligible):
            reason = Omission(file.id, "ineligible")
        elif full and file.change.oversized:
            reason = Omission(file.id, "work_limit")
        elif file.change in settled:
            reason = Omission(file.id, "not_scheduled")
        elif file.change.added_lines == file.change.deleted_lines == 0:
            reason = Omission(file.id, "missing_evidence")
        elif full and len(targets) >= 2 * MAX_FILES:
            reason = Omission(file.id, "not_scheduled")
        else:
            try:
                target = _target(
                    file, work, evidence, (context or {}).get(file.change), full=full
                )
            except WorkLimit:
                reason = Omission(file.id, "work_limit")
            else:
                if target is None:
                    reason = Omission(file.id, "missing_evidence")
                else:
                    targets.append(target)
        if reason:
            omissions[file.id] = reason

    targets.sort(
        key=lambda item: (
            item.rank,
            item.file.change not in discussion_paths,
            decode_path(item.file.path),
            item.position,
        )
    )
    chosen = targets[:MAX_FILES]
    if len(targets) > MAX_FILES and targets[-1].rank > chosen[-1].rank:
        chosen[-1] = next(
            target for target in targets[MAX_FILES:] if target.rank > chosen[-1].rank
        )
    selected: list[str] = []
    packed: dict[str, ShownExcerpt] = {}
    metadata: list[dict[str, object]] = []
    records: list[dict[str, object]] = []
    complete: set[str] = set()

    def serialize() -> str:
        return _serialize(
            {
                "comparison": comparison.model_dump(),
                "files": metadata,
                "selected_file_ids": selected,
                "excerpts": records,
            }
        )

    if _bytes(serialize()) > limit:
        return EvidenceBundle(
            comparison,
            inventory,
            (),
            (),
            tuple(omissions.get(f.id, Omission(f.id, "work_limit")) for f in inventory),
            "",
        )
    for target in chosen:
        if target.context_gap is not None:
            omissions[target.file.id] = target.context_gap
        metadata.append(target.file.projection())
        selected.append(target.file.id)
        new = list(
            {
                item.source.id: item
                for item in target.excerpts
                if item.source.id not in packed
            }.values()
        )
        records.extend(item.source.model_dump() for item in new)
        if _bytes(serialize()) > limit:
            # Pack the paired changed declarations or hunks before optional context.
            if new:
                del records[-len(new) :]
            required = [
                item
                for item in new
                if item.changed_lines
                or item.source.role in ("base_declaration", "head_declaration")
            ]
            records.extend(item.source.model_dump() for item in required)
            if _bytes(serialize()) > limit:
                if required:
                    del records[-len(required) :]
                selected.pop()
                metadata.pop()
                omissions[target.file.id] = Omission(target.file.id, "work_limit")
                continue
            new = required
            omissions[target.file.id] = Omission(target.file.id, "work_limit")
        for item in new:
            packed[item.source.id] = item
        for item in target.excerpts:
            previous = packed.get(item.source.id)
            if previous and target.file.id not in previous.file_ids:
                packed[item.source.id] = replace(
                    previous,
                    file_ids=(*previous.file_ids, target.file.id),
                    changed_lines=tuple(
                        sorted(set(previous.changed_lines + item.changed_lines))
                    ),
                )
        if full and _all_hunks_presented(target, packed):
            complete.add(target.file.id)
    # Preserve other inventory metadata where it fits, without granting coverage for it.
    for file in () if full else inventory:
        if file.id not in selected:
            metadata.append(file.projection())
            if _bytes(serialize()) > limit:
                metadata.pop()
                omissions.setdefault(file.id, Omission(file.id, "work_limit"))
                break
    for file in inventory:
        if file.id not in selected:
            omissions.setdefault(file.id, Omission(file.id, "not_scheduled"))
    order = {file.id: index for index, file in enumerate(inventory)}
    metadata.sort(key=lambda item: order[str(item["id"])])
    return EvidenceBundle(
        comparison,
        inventory,
        tuple(selected),
        tuple(packed.values()),
        tuple(omissions[file.id] for file in inventory if file.id in omissions),
        serialize(),
        frozenset(complete),
    )


@dataclass(frozen=True)
class FrozenHunk:
    file_index: int
    ordinal: int
    hunk: Hunk


def _hunk_text(hunk: Hunk, side: RevisionSide) -> str:
    edit = b"-" if side == "base" else b"+"
    lines: list[bytes] = []
    last_included = False
    for line in lf_lines(hunk.patch)[1:]:
        if line.startswith(b"\\") and last_included:
            lines[-1] = lines[-1].removesuffix(b"\n")
        last_included = line[:1] in (b" ", edit)
        if last_included:
            lines.append(line[1:])
    return b"".join(lines).decode("utf-8", errors="backslashreplace")


def build_diff_bundle(
    comparison: PinnedComparison,
    rows: tuple[ChangedFile, ...],
    artifact: bytes,
    hunks: tuple[FrozenHunk, ...],
    max_bytes: int,
    *,
    file_indices: tuple[int, ...] | None = None,
    full: bool = False,
    incomplete_ids: frozenset[str] = frozenset(),
) -> EvidenceBundle:
    inventory = _inventory(rows)
    by_index = {int(file.id[1:]) - 1: file for file in inventory}
    files = (
        by_index
        if file_indices is None
        else {i: by_index[index] for i, index in enumerate(file_indices)}
    )
    selected: dict[int, str] = {}
    for item in hunks:
        file = files[item.file_index]
        if (
            len(selected) < MAX_FILES
            and (text_eligible(file.change) if full else file.eligible)
            and any(
                next(changed_lines(item.hunk, side), None) is not None
                and 0 < len(_hunk_text(item.hunk, side)) <= 4096
                for side, _ in (
                    _regular_sides(file.change) if full else python_sides(file.change)
                )
            )
        ):
            selected[item.file_index] = file.id
    incomplete = set(incomplete_ids)
    table: list[tuple[str, int, int, RevisionSide, str]] = []
    excerpts: list[ShownExcerpt] = []
    for item in sorted(hunks, key=lambda item: (item.file_index, item.ordinal)):
        if item.file_index not in selected:
            continue
        file = files[item.file_index]
        for side, entry in (
            _regular_sides(file.change) if full else python_sides(file.change)
        ):
            span = item.hunk.old_span if side == "base" else item.hunk.new_span
            text = _hunk_text(item.hunk, side)
            if not span.line_count:
                continue
            if not text or len(text) > 4096:
                incomplete.add(file.id)
                continue
            identifier = f"e{len(excerpts) + 1:05d}"
            commit = comparison.merge_base if side == "base" else comparison.head
            location = SourceRange(
                side,
                commit,
                entry.path,
                entry.object_id,
                span.start_line,
                span.start_line + span.line_count - 1,
            )
            source = SourceEvidence(
                kind="source",
                id=identifier,
                role="check_location",
                range=SourceLocation.from_range(location),
                text=text,
            )
            excerpts.append(
                ShownExcerpt(source, (file.id,), _changed(source, file, (item.hunk,)))
            )
            table.append((file.id, item.file_index, item.ordinal, side, identifier))
    shown_files = {item.file_ids[0] for item in excerpts if item.changed_lines}
    selected_ids = tuple(
        file_id for file_id in selected.values() if file_id in shown_files
    )
    text = (
        _serialize(
            {
                "selected_file_ids": selected_ids,
                "excerpt_table_columns": [
                    "file_id",
                    "file_index",
                    "file_hunk_ordinal",
                    "side",
                    "evidence_id",
                ],
                "excerpt_table": table,
            }
        )
        + "\n"
        + artifact.decode("utf-8")
    )
    if _bytes(text) > min(max_bytes, MAX_EVIDENCE_BYTES):
        raise ValueError("Frozen evidence exceeds byte limit")
    omissions = tuple(
        Omission(
            file.id,
            "not_scheduled"
            if (text_eligible(file.change) if full else file.eligible)
            else "ineligible",
        )
        for file in inventory
        if file.id not in shown_files
    )
    return EvidenceBundle(
        comparison,
        inventory,
        selected_ids,
        tuple(excerpts),
        omissions,
        text,
        frozenset(set(selected_ids) - incomplete) if full else frozenset(),
    )
