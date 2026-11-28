"""Collect a small set of literal references from pinned text, not executable code."""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from loop_reviewer.reviewer.inventory import RevisionSide
from loop_reviewer.reviewer.inventory_contract import FileSide, decode_path
from loop_reviewer.reviewer.local_check_contract import SourceEvidence, SourceLocation
from loop_reviewer.reviewer.local_evidence import (
    EvidenceIndex,
    OptionalRetrieval,
    PinnedEvidenceWork,
)
from loop_reviewer.reviewer.pinned_source import SourceRange

_LITERAL = re.compile(r"[A-Za-z_][A-Za-z0-9_]{5,63}")
_COMMON = frozenset(
    ("context", "return", "function", "import", "export", "public", "private")
)


def literal_references(
    seeds: tuple[SourceEvidence, ...],
    work: PinnedEvidenceWork,
    index: EvidenceIndex,
    *,
    sides: tuple[RevisionSide, ...],
    exclude_ids: frozenset[str] = frozenset(),
    optional: OptionalRetrieval | None = None,
) -> tuple[SourceEvidence, ...]:
    if optional is not None and optional.bytes_left <= 0:
        return ()
    literals: dict[str, None] = {}
    for seed in seeds:
        for match in _LITERAL.finditer(seed.text):
            literal = match.group()
            if literal not in _COMMON:
                literals.setdefault(literal, None)
            if literals:
                break
        if literals:
            break
    if not literals:
        return ()
    literal = next(iter(literals))
    known_paths = {seed.range.path for seed in seeds}
    entries: dict[str, SourceEvidence] = {}
    for side in sides:
        matches = work.search(side, literal, regular_text=True, optional=optional)
        if isinstance(matches, str):
            continue
        for path in tuple(path for path in matches.paths if path not in known_paths)[
            :2
        ]:
            blob = work.blob(side, path, optional=optional)
            if isinstance(blob, str):
                continue
            lines = work.text_lines(blob)
            position = next(
                (i for i, line in enumerate(lines, 1) if literal in line), None
            )
            if position is None:
                continue
            span = SourceRange(
                side,
                blob.commit_id,
                blob.path,
                blob.blob_id,
                max(1, position - 1),
                min(len(lines), position + 1),
            )
            text = work.excerpt(span)
            if text is None or len(text.encode("utf-8")) > 384:
                continue
            identifier = index.source("text_match", span, text)
            if identifier is not None and identifier not in exclude_ids:
                entries[identifier] = SourceEvidence(
                    kind="source",
                    id=identifier,
                    role="text_match",
                    range=SourceLocation.from_range(span),
                    text=text,
                )
    return tuple(entries.values())


_ASCII_WORD = re.compile(r"[A-Za-z0-9_]+")
_PACKET_COMMON = _COMMON | frozenset(
    [
        "async",
        "await",
        "bool",
        "break",
        "case",
        "chan",
        "class",
        "const",
        "continue",
        "def",
        "default",
        "defer",
        "do",
        "else",
        "except",
        "false",
        "finally",
        "fn",
        "for",
        "from",
        "func",
        "go",
        "if",
        "in",
        "int",
        "interface",
        "let",
        "map",
        "match",
        "new",
        "nil",
        "None",
        "null",
        "package",
        "pass",
        "raise",
        "range",
        "select",
        "self",
        "str",
        "string",
        "struct",
        "switch",
        "this",
        "throw",
        "true",
        "try",
        "type",
        "typeof",
        "undefined",
        "var",
        "while",
        "with",
        "yield",
        "False",
        "True",
    ]
)


@dataclass(frozen=True)
class TextReference:
    span: SourceRange
    text: str
    file_ids: tuple[str, ...]


class ShownLines:
    def __init__(self) -> None:
        self._lines: dict[tuple[RevisionSide, str, str, str, int], set[str]] = {}

    def add(
        self, span: SourceRange | SourceLocation, file_ids: tuple[str, ...]
    ) -> None:
        for line in range(span.start_line, span.end_line + 1):
            self._lines.setdefault(self._key(span, line), set()).update(file_ids)

    def owners(self, span: SourceRange, line: int) -> set[str]:
        return self._lines.get(self._key(span, line), set())

    def has_new_text(self, span: SourceRange, file_ids: tuple[str, ...]) -> bool:
        return any(
            not self.owners(span, line).issuperset(file_ids)
            for line in range(span.start_line, span.end_line + 1)
        )

    @staticmethod
    def _key(
        span: SourceRange | SourceLocation, line: int
    ) -> tuple[RevisionSide, str, str, str, int]:
        return span.side, span.commit_id, span.path, span.blob_id, line


def _packet_literals(
    edited: Iterable[tuple[str, int, SourceEvidence]],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    by_hunk: dict[tuple[str, int], dict[str, set[RevisionSide]]] = {}
    owners: dict[str, dict[str, None]] = {}
    for file_id, hunk, source in edited:
        tokens = by_hunk.setdefault((file_id, hunk), {})
        for match in _ASCII_WORD.finditer(source.text):
            token = match.group()
            if (
                2 <= len(token) <= 128
                and not token[0].isdigit()
                and token not in _PACKET_COMMON
            ):
                tokens.setdefault(token, set()).add(source.range.side)
                owners.setdefault(token, {})[file_id] = None
    ranked = dict.fromkeys(
        token
        for tokens in by_hunk.values()
        for token, sides in tokens.items()
        if len(sides) == 1
    )
    ranked.update(dict.fromkeys(owners))
    return tuple((token, tuple(owners[token])) for token in tuple(ranked)[:2])


def packet_references(
    edited: Iterable[tuple[str, int, SourceEvidence]],
    shown: ShownLines,
    expected: Mapping[tuple[RevisionSide, str], FileSide],
    work: PinnedEvidenceWork,
    budget: OptionalRetrieval,
) -> tuple[TextReference, ...]:
    found: dict[SourceRange, TextReference] = {}
    sides: tuple[RevisionSide, ...] = ("base", "head")
    for literal, owners in _packet_literals(edited):
        token = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(literal)}(?![A-Za-z0-9_])")
        for side in sides:
            matches = work.search(side, literal, regular_text=True, optional=budget)
            if isinstance(matches, str):
                continue
            for path in sorted(set(matches.paths), key=decode_path)[:2]:
                blob = work.blob(
                    side, path, expected=expected.get((side, path)), optional=budget
                )
                if isinstance(blob, str):
                    continue
                lines = work.text_lines(blob)
                fallback: TextReference | None = None
                for number, line in enumerate(lines, 1):
                    if not token.search(line):
                        continue
                    span = SourceRange(
                        side,
                        blob.commit_id,
                        path,
                        blob.blob_id,
                        max(1, number - 1),
                        min(len(lines), number + 1),
                    )
                    already_shown = shown.owners(span, number)
                    if already_shown.issuperset(owners) or (
                        already_shown and fallback is not None
                    ):
                        continue
                    text = work.excerpt(span)
                    if not text or len(text.encode("utf-8")) > 384:
                        continue
                    fallback = TextReference(span, text, owners)
                    if not already_shown:
                        break
                if fallback is not None:
                    previous = found.get(fallback.span)
                    if previous:
                        fallback = TextReference(
                            fallback.span,
                            fallback.text,
                            tuple(dict.fromkeys((*previous.file_ids, *owners))),
                        )
                    found[fallback.span] = fallback
    return tuple(found.values())
