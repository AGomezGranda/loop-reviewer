"""Collect a small set of literal references from pinned text, not executable code."""

import re

from loop_reviewer.reviewer.inventory import RevisionSide
from loop_reviewer.reviewer.local_check_contract import SourceEvidence, SourceLocation
from loop_reviewer.reviewer.local_evidence import EvidenceIndex, LocalEvidenceWork
from loop_reviewer.reviewer.pinned_source import SourceRange

_LITERAL = re.compile(r"[A-Za-z_][A-Za-z0-9_]{5,63}")
_COMMON = frozenset(
    ("context", "return", "function", "import", "export", "public", "private")
)


def literal_references(
    seeds: tuple[SourceEvidence, ...],
    work: LocalEvidenceWork,
    index: EvidenceIndex,
    *,
    sides: tuple[RevisionSide, ...],
    exclude_ids: frozenset[str] = frozenset(),
) -> tuple[SourceEvidence, ...]:
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
        matches = work.search(side, literal, regular_text=True)
        if isinstance(matches, str):
            continue
        for path in tuple(path for path in matches.paths if path not in known_paths)[
            :2
        ]:
            blob = work.blob(side, path)
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
