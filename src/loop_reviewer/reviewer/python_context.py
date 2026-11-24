"""Python facts and source selection for the local reviewer."""

import codecs
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from io import BytesIO
from tokenize import detect_encoding
from typing import Literal, Protocol

from loop_reviewer.reviewer.inventory import RevisionSide
from loop_reviewer.reviewer.inventory_contract import decode_path
from loop_reviewer.reviewer.pinned_source import (
    Hunk,
    PinnedTextBlob,
    ReadFailure,
    SourceRange,
    lf_lines,
)


@dataclass(frozen=True)
class Diagnostic:
    blob: PinnedTextBlob
    tool_version: str
    result: Literal["passed", "syntax_error"]
    message: str
    location: SourceRange | None


@dataclass(frozen=True)
class Declaration:
    name: str
    kind: Literal["class", "function", "async_function"]
    start_line: int
    end_line: int


@dataclass(frozen=True)
class Call:
    name: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class ImportAlias:
    name: str
    qualified: str


@dataclass(frozen=True)
class PythonAnalysis:
    diagnostic: Diagnostic
    declarations: tuple[Declaration, ...] = ()
    calls: tuple[Call, ...] = ()
    imports: tuple[ImportAlias, ...] = ()
    limited: bool = False


class PythonAnalyzer(Protocol):
    def analyze(
        self, blob: PinnedTextBlob, *, timeout_seconds: float
    ) -> PythonAnalysis | ReadFailure: ...


def changed_lines(hunk: Hunk, side: RevisionSide) -> Iterator[int]:
    line = hunk.old_span.start_line if side == "base" else hunk.new_span.start_line
    edit = b"-" if side == "base" else b"+"
    for record in lf_lines(hunk.patch)[1:]:
        marker = record[:1]
        if marker == edit:
            yield line
        if marker in (edit, b" "):
            line += 1


def _range(blob: PinnedTextBlob, start: int, end: int) -> SourceRange:
    return SourceRange(blob.side, blob.commit_id, blob.path, blob.blob_id, start, end)


@dataclass(frozen=True)
class DeclarationSelection:
    name: str | None
    base: SourceRange | None
    head: SourceRange | None


def _span(declaration: Declaration, focus: int) -> tuple[int, int]:
    start = declaration.start_line
    if declaration.end_line - start >= 120:
        start = max(start, focus - 15)
    return start, min(declaration.end_line, start + 119)


def _touched(
    hunks: tuple[Hunk, ...], analysis: PythonAnalysis | None
) -> dict[str | int, SourceRange]:
    selected: dict[str | int, SourceRange] = {}
    if analysis is None:
        return selected
    blob = analysis.diagnostic.blob
    declarations = sorted(
        analysis.declarations,
        key=lambda d: (-d.name.count("."), d.end_line - d.start_line, d.start_line),
    )
    for index, hunk in enumerate(hunks):
        module: list[int] = []
        for line in changed_lines(hunk, blob.side):
            declaration = next(
                (d for d in declarations if d.start_line <= line <= d.end_line), None
            )
            if declaration is None:
                module.append(line)
            elif declaration.name not in selected:
                selected[declaration.name] = _range(blob, *_span(declaration, line))
        if module:
            selected[index] = _range(blob, module[0], min(module[-1], module[0] + 119))
    return selected


def _counterpart(name: str, analysis: PythonAnalysis | None) -> SourceRange | None:
    if analysis is not None:
        matches = tuple(d for d in analysis.declarations if d.name == name)
        if len(matches) == 1:
            return _range(
                analysis.diagnostic.blob, *_span(matches[0], matches[0].start_line)
            )
    return None


def select_declarations(
    hunks: tuple[Hunk, ...],
    old_analysis: PythonAnalysis | None,
    new_analysis: PythonAnalysis | None,
) -> tuple[DeclarationSelection, ...]:
    old = _touched(hunks, old_analysis)
    new = _touched(hunks, new_analysis)
    selected = []
    for key in dict.fromkeys((*old, *new)):
        base, head = old.get(key), new.get(key)
        if isinstance(key, str):
            base = base or _counterpart(key, old_analysis)
            head = head or _counterpart(key, new_analysis)
        selected.append(
            DeclarationSelection(key if isinstance(key, str) else None, base, head)
        )
    return tuple(sorted(selected, key=_selection_position))


def _selection_position(selection: DeclarationSelection) -> tuple[int, str]:
    primary = selection.head or selection.base
    assert primary is not None
    return primary.start_line, selection.name or ""


@dataclass(frozen=True)
class Symbol:
    name: str
    path: str

    @property
    def literal(self) -> str:
        return self.name.rsplit(".", 1)[-1]


@dataclass(frozen=True)
class Reference:
    role: Literal["call_site", "test_call", "text_match"]
    location: SourceRange
    line: int


def decoded_lines(blob: PinnedTextBlob) -> tuple[str, ...]:
    original = lf_lines(blob.text)
    try:
        encoding, _ = detect_encoding(BytesIO(blob.text).readline)
        text = blob.text.decode(encoding)
        chunks = text.split("\n")
        lines = tuple(line + "\n" for line in chunks[:-1]) + (
            (chunks[-1],) if chunks[-1] else ()
        )
        if len(lines) == len(original):
            return lines
    except SyntaxError, UnicodeError, LookupError:
        pass
    return tuple(
        codecs.escape_encode(line.removesuffix(b"\n"))[0].decode("ascii")
        + ("\n" if line.endswith(b"\n") else "")
        for line in original
    )


def _reference_rank(reference: Reference) -> tuple[int, bytes, int, RevisionSide]:
    return (
        ("call_site", "test_call", "text_match").index(reference.role),
        decode_path(reference.location.path),
        reference.line,
        reference.location.side,
    )


def _module(path: str) -> str:
    raw = decode_path(path)
    return (
        raw.removesuffix(b".py")
        .removesuffix(b"/__init__")
        .decode("utf-8", errors="replace")
        .replace("/", ".")
    )


def rank_references(
    symbol: Symbol, candidates: Iterable[PythonAnalysis]
) -> tuple[Reference, ...]:
    references: list[Reference] = []
    qualified = f"{_module(symbol.path)}.{symbol.name}"
    identifier = re.compile(rf"(?<!\w){re.escape(symbol.literal)}(?!\w)")
    for candidate in candidates:
        blob = candidate.diagnostic.blob
        lines = decoded_lines(blob)
        raw = decode_path(blob.path)
        test = raw.startswith(b"tests/") or raw.rsplit(b"/", 1)[-1].startswith(b"test_")
        found = False
        for call in candidate.calls:
            first, *tail = call.name.split(".")
            imported = any(
                alias.name == first and ".".join((alias.qualified, *tail)) == qualified
                for alias in candidate.imports
            )
            if not 1 <= call.start_line <= call.end_line <= len(lines):
                continue
            if imported or (blob.path == symbol.path and call.name == symbol.name):
                role: Literal["call_site", "test_call"] = "call_site"
            elif test and call.name.rsplit(".", 1)[-1] == symbol.literal:
                role = "test_call"
            else:
                continue
            references.append(
                Reference(
                    role,
                    _range(
                        blob,
                        max(1, call.start_line - 2),
                        min(len(lines), call.end_line + 3, call.start_line + 5),
                    ),
                    call.start_line,
                )
            )
            found = True
        if not found and blob.path != symbol.path:
            for number, line in enumerate(lines, 1):
                if identifier.search(line):
                    references.append(
                        Reference("text_match", _range(blob, number, number), number)
                    )
                    break
    return tuple(sorted(references, key=_reference_rank))
