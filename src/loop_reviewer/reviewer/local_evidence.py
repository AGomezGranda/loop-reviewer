"""Bounded pinned source work and evidence indexing shared by local reviewers."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from loop_reviewer.reviewer.inventory import RevisionSide
from loop_reviewer.reviewer.inventory_contract import (
    ChangedFile,
    FileSide,
    PinnedComparison,
)
from loop_reviewer.reviewer.local_check_contract import (
    MAX_EVIDENCE,
    ContextStatus,
    DiagnosticEvidence,
    Evidence,
    SourceEvidence,
    SourceLocation,
    SourceRole,
)
from loop_reviewer.reviewer.local_check_policy import supported_python
from loop_reviewer.reviewer.pinned_source import (
    Hunk,
    PinnedSource,
    PinnedTextBlob,
    ReadFailure,
    SearchPaths,
    SourceRange,
    lf_lines,
)
from loop_reviewer.reviewer.python_context import (
    Diagnostic,
    PythonAnalysis,
    PythonAnalyzer,
    Symbol,
    decoded_lines,
    rank_references,
    select_declarations,
)

_TEXT_BYTES = 128 * 1024
_SEARCH_BYTES = 64 * 1024
_LIMITS = {"blob": 250, "analysis": 250, "hunk": 200, "search": 40}
_Operation = Literal["blob", "analysis", "hunk", "search"]
_PathKey = tuple[RevisionSide, str]


class WorkLimit(Exception):
    pass


@dataclass
class LocalEvidenceWork:
    source: PinnedSource
    analyzer: PythonAnalyzer | None
    comparison: PinnedComparison
    monotonic: Callable[[], float]
    deadline: float
    bytes_left: int = 8 * 1024 * 1024
    counts: dict[_Operation, int] = field(default_factory=dict)
    blobs: dict[_PathKey, PinnedTextBlob | ReadFailure] = field(default_factory=dict)
    analyses: dict[_PathKey, PythonAnalysis | ReadFailure] = field(default_factory=dict)
    patches: dict[tuple[str | None, str | None], tuple[Hunk, ...] | ReadFailure] = (
        field(default_factory=dict)
    )
    searches: dict[tuple[RevisionSide, str, bool], SearchPaths | ReadFailure] = field(
        default_factory=dict
    )
    lines: dict[_PathKey, tuple[str, ...]] = field(default_factory=dict)
    utf8_only: bool = False

    def remaining(self) -> float:
        time_left = self.deadline - self.monotonic()
        if time_left <= 0:
            raise WorkLimit
        return time_left

    def _allocate(self, operation: _Operation) -> float:
        time_left = self.remaining()
        count = self.counts.get(operation, 0)
        if count >= _LIMITS[operation]:
            raise WorkLimit
        self.counts[operation] = count + 1
        return time_left

    def blob(
        self, side: RevisionSide, path: str, *, expected: FileSide | None = None
    ) -> PinnedTextBlob | ReadFailure:
        key = side, path
        if key not in self.blobs:
            if self.bytes_left <= 0 or (
                expected
                and expected.blob_bytes is not None
                and self.bytes_left < expected.blob_bytes <= _TEXT_BYTES
            ):
                raise WorkLimit
            limit = min(self.bytes_left, _TEXT_BYTES)
            result = self.source.read_text_blob(
                side, path, max_bytes=limit, timeout_seconds=self._allocate("blob")
            )
            self.remaining()
            if not isinstance(result, str):
                commit = (
                    self.comparison.merge_base
                    if side == "base"
                    else self.comparison.head
                )
                if (result.side, result.commit_id, result.path) != (
                    side,
                    commit,
                    path,
                ) or len(result.text) > limit:
                    result = "unavailable"
                else:
                    self.bytes_left -= len(result.text)
            elif result == "limit_reached" and limit < _TEXT_BYTES and expected is None:
                # A context candidate may fit the per-blob cap but not the remaining shared bytes.
                raise WorkLimit
            self.blobs[key] = result
        blob = self.blobs[key]
        if (
            expected is not None
            and not isinstance(blob, str)
            and (blob.blob_id, len(blob.text))
            != (expected.object_id, expected.blob_bytes)
        ):
            return "unavailable"
        return blob

    def analysis(
        self, side: RevisionSide, path: str, *, expected: FileSide | None = None
    ) -> PythonAnalysis | ReadFailure:
        blob = self.blob(side, path, expected=expected)
        if isinstance(blob, str):
            return blob
        key = side, path
        if key not in self.analyses:
            analysis = (
                self.analyzer.analyze(blob, timeout_seconds=self._allocate("analysis"))
                if self.analyzer is not None
                else "unavailable"
            )
            self.remaining()
            if not isinstance(analysis, str) and analysis.diagnostic.blob != blob:
                analysis = "unavailable"
            self.analyses[key] = analysis
        return self.analyses[key]

    def hunks(self, change: ChangedFile) -> tuple[Hunk, ...] | ReadFailure:
        path = (
            change.old.path if change.old else None,
            change.new.path if change.new else None,
        )
        if path not in self.patches:
            self.patches[path] = self.source.hunks(
                change, max_bytes=64 * 1024, timeout_seconds=self._allocate("hunk")
            )
            self.remaining()
        return self.patches[path]

    def search(
        self, side: RevisionSide, literal: str, *, regular_text: bool = False
    ) -> SearchPaths | ReadFailure:
        key = side, literal, regular_text
        if key not in self.searches:
            self.searches[key] = self.source.search(
                side,
                literal,
                max_paths=20,
                max_bytes=_SEARCH_BYTES,
                timeout_seconds=self._allocate("search"),
                regular_text=regular_text,
            )
            self.remaining()
        return self.searches[key]

    def text_lines(self, blob: PinnedTextBlob) -> tuple[str, ...]:
        key = blob.side, blob.path
        if key not in self.lines:
            if self.utf8_only:
                try:
                    self.lines[key] = tuple(
                        line.decode("utf-8") for line in lf_lines(blob.text)
                    )
                except UnicodeDecodeError:
                    self.lines[key] = ()
            else:
                self.lines[key] = decoded_lines(blob)
        return self.lines[key]

    def excerpt(self, location: SourceRange) -> str | None:
        key = location.side, location.path
        blob = self.blobs.get(key)
        if (
            blob is None
            or isinstance(blob, str)
            or (blob.commit_id, blob.blob_id) != (location.commit_id, location.blob_id)
        ):
            return None
        lines = self.text_lines(blob)
        if not 1 <= location.start_line <= location.end_line <= len(lines):
            return None
        return "".join(lines[location.start_line - 1 : location.end_line])


@dataclass
class EvidenceIndex:
    entries: list[Evidence] = field(default_factory=list)
    diagnostics: dict[_PathKey, str] = field(default_factory=dict)
    sources: dict[tuple[SourceRole, SourceRange], str] = field(default_factory=dict)
    _sequence: int = 0

    def _next(self) -> str:
        if len(self.entries) >= MAX_EVIDENCE:
            raise WorkLimit
        self._sequence += 1
        return f"e{self._sequence:05d}"

    def add_existing_source(self, entry: SourceEvidence) -> None:
        if len(self.entries) >= MAX_EVIDENCE:
            raise WorkLimit
        loc = entry.range
        self._sequence = max(self._sequence, int(entry.id[1:]))
        self.entries.append(entry)
        self.sources.setdefault(
            (
                entry.role,
                SourceRange(
                    loc.side,
                    loc.commit_id,
                    loc.path,
                    loc.blob_id,
                    loc.start_line,
                    loc.end_line,
                ),
            ),
            entry.id,
        )

    def diagnostic(self, diagnostic: Diagnostic) -> str:
        blob = diagnostic.blob
        key = blob.side, blob.path
        if key not in self.diagnostics:
            identifier = self._next()
            self.entries.append(
                DiagnosticEvidence(
                    kind="diagnostic",
                    id=identifier,
                    side=blob.side,
                    commit_id=blob.commit_id,
                    path=blob.path,
                    blob_id=blob.blob_id,
                    tool="cpython_compile",
                    tool_version=diagnostic.tool_version,
                    result=diagnostic.result,
                    message=diagnostic.message,
                    location=SourceLocation.from_range(diagnostic.location)
                    if diagnostic.location
                    else None,
                )
            )
            self.diagnostics[key] = identifier
        return self.diagnostics[key]

    def source(
        self, role: SourceRole, location: SourceRange, text: str | None
    ) -> str | None:
        key = role, location
        if key in self.sources:
            return self.sources[key]
        if not text or len(text) > 4096:
            return None
        identifier = self._next()
        self.entries.append(
            SourceEvidence(
                kind="source",
                id=identifier,
                role=role,
                range=SourceLocation.from_range(location),
                text=text,
            )
        )
        self.sources[key] = identifier
        return identifier


def python_sides(change: ChangedFile) -> tuple[tuple[RevisionSide, FileSide], ...]:
    sides: list[tuple[RevisionSide, FileSide]] = []
    if change.old and supported_python(change.old):
        sides.append(("base", change.old))
    if change.new and supported_python(change.new):
        sides.append(("head", change.new))
    return tuple(sides)


def _context_gap(current: ContextStatus, failure: ReadFailure) -> ContextStatus:
    if current == "unavailable" or failure in ("not_found", "unavailable"):
        return "unavailable"
    return "limited"


def _find_references(
    work: LocalEvidenceWork,
    symbol: Symbol,
    sides: tuple[RevisionSide, ...],
) -> tuple[list[PythonAnalysis], ContextStatus]:
    candidates: list[PythonAnalysis] = []
    status: ContextStatus = "complete"
    for side in sides:
        try:
            matches = work.search(side, symbol.literal)
        except WorkLimit:
            return candidates, _context_gap(status, "limit_reached")
        if isinstance(matches, str):
            status = _context_gap(status, matches)
            continue
        if matches.truncated:
            status = _context_gap(status, "limit_reached")
        for path in matches.paths:
            try:
                analysis = work.analysis(side, path)
            except WorkLimit:
                return candidates, _context_gap(status, "limit_reached")
            if isinstance(analysis, str):
                status = _context_gap(status, analysis)
            else:
                candidates.append(analysis)
                if analysis.limited:
                    status = _context_gap(status, "limit_reached")
    return candidates, status


@dataclass(frozen=True)
class ContextSelection:
    status: ContextStatus
    evidence_ids: tuple[str, ...]


def collect_python_context(
    change: ChangedFile,
    analyses: Mapping[RevisionSide, PythonAnalysis],
    work: LocalEvidenceWork,
    evidence: EvidenceIndex,
    *,
    missing_analysis: ReadFailure = "unavailable",
) -> ContextSelection:
    if change.added_lines == change.deleted_lines == 0:
        return ContextSelection("not_applicable", ())
    hunks = work.hunks(change)
    if isinstance(hunks, str):
        return ContextSelection(_context_gap("complete", hunks), ())
    if not hunks:
        return ContextSelection("not_applicable", ())
    status: ContextStatus = "complete"
    for side, file in python_sides(change):
        analysis = analyses.get(side)
        if analysis is None:
            failed = work.analyses.get(
                (side, file.path), work.blobs.get((side, file.path))
            )
            if not isinstance(failed, str):
                failed = missing_analysis
            status = _context_gap(status, failed)
        elif analysis.limited:
            status = _context_gap(status, "limit_reached")
    ids: dict[str, None] = {}
    selected_names = 0
    for selection in select_declarations(
        hunks, analyses.get("base"), analyses.get("head")
    ):
        work.remaining()
        if selection.name is not None:
            if selected_names >= 5:
                status = _context_gap(status, "limit_reached")
                continue
            selected_names += 1
        for role, location in (
            ("base_declaration", selection.base),
            ("head_declaration", selection.head),
        ):
            if location:
                identifier = evidence.source(role, location, work.excerpt(location))
                if identifier is None:
                    status = _context_gap(status, "limit_reached")
                else:
                    ids[identifier] = None
        if selection.name is None:
            continue
        primary = selection.head or selection.base
        assert primary is not None
        sides: tuple[RevisionSide, ...] = (
            ("head",) if selection.head else ("base", "head")
        )
        symbol = Symbol(selection.name, primary.path)
        candidates, completeness = _find_references(work, symbol, sides)
        if completeness != "complete":
            status = _context_gap(
                status,
                "unavailable" if completeness == "unavailable" else "limit_reached",
            )
        for reference in rank_references(symbol, candidates)[:2]:
            identifier = evidence.source(
                reference.role, reference.location, work.excerpt(reference.location)
            )
            if identifier is None:
                status = _context_gap(status, "limit_reached")
            else:
                ids[identifier] = None
    return ContextSelection(status, tuple(ids))
