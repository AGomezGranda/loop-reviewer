"""Fail-closed pilot policy: source witnesses, not agreement between prompts."""

from dataclasses import dataclass
from typing import Literal

from loop_reviewer.reviewer.local_check_contract import SourceEvidence, SourceLocation
from loop_reviewer.reviewer.local_evidence import EvidenceIndex, LocalEvidenceWork
from loop_reviewer.reviewer.model_contract import Candidate, Impact, VerificationReplyV1
from loop_reviewer.reviewer.pinned_source import PinnedTextBlob, SourceRange
from loop_reviewer.reviewer.python_context import changed_lines
from loop_reviewer.reviewer.semantic_inputs import (
    BundleFile,
    EvidenceBundle,
    ShownExcerpt,
)

IMPACT_ORDER: dict[Impact, int] = {
    "data_loss": 0,
    "access_control_failure": 0,
    "core_workflow_outage": 0,
    "material_incorrect_behavior": 1,
    "localized_recoverable_regression": 2,
    "unknown": 3,
}

# Observable predicates the pilot can prove without running the changed package.
WITNESS_INSTRUCTIONS = (
    "For an empty-result indexing claim the local checker can prove, phrase changed_behavior "
    "as '<producer module>.<function>() returns [] instead of None' (use () for an empty tuple), "
    "trigger as 'Call <consumer module>.<function>()', and consequence as "
    "'<consumer module>.<function>() raises IndexError'. Do not invent the caller or assume "
    "a name match proves binding. Report other plausible problems only with shown citations."
)


@dataclass(frozen=True)
class PrivateLead:
    id: str
    file_id: str
    candidate: Candidate
    missing_fact: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class Finding:
    id: str
    file_id: str
    candidate: Candidate
    evidence_ids: tuple[str, ...]
    anchor: SourceLocation | None
    equivalence: tuple[str, ...]
    severity: Literal["high", "medium", "low"] = "medium"
    python_witness: bool = True


@dataclass(frozen=True)
class Discard:
    id: str
    file_id: str
    reason: Literal["contradicted", "preexisting", "duplicate"]
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class CheckedCandidate:
    id: str
    file: BundleFile
    candidate: Candidate
    cited_ids: tuple[str, ...]
    anchor: SourceLocation | None


@dataclass(frozen=True)
class Witness:
    verdict: Literal["supports", "contradicted", "preexisting"]
    evidence_ids: tuple[str, ...]
    equivalence: tuple[str, ...]


@dataclass(frozen=True)
class VerificationPacket:
    entries: tuple[SourceEvidence, ...]
    witness: Witness | None


def candidate_order(
    candidates: tuple[Candidate, ...],
) -> tuple[tuple[str, Candidate], ...]:
    indexed = tuple(
        (f"c{i:05d}", candidate) for i, candidate in enumerate(candidates, 1)
    )
    return tuple(
        sorted(indexed, key=lambda pair: IMPACT_ORDER[pair[1].proposed_impact])
    )


def check_candidate(
    identifier: str,
    candidate: Candidate,
    shown_ids: frozenset[str],
    bundle: EvidenceBundle,
    work: LocalEvidenceWork,
) -> CheckedCandidate | PrivateLead:
    file = next(f for f in bundle.inventory if f.id == candidate.file_id)
    excerpts = {
        item.source.id: item
        for item in bundle.excerpts
        if item.source.id in shown_ids and file.id in item.file_ids
    }
    accepted: dict[str, ShownExcerpt] = {}
    for key in dict.fromkeys(c.evidence_id for c in candidate.citations):
        item = excerpts.get(key)
        if item is None or not source_resolves(item.source, work):
            return PrivateLead(
                identifier,
                file.id,
                candidate,
                "A citation did not resolve to the pinned source.",
                tuple(accepted),
            )
        accepted[key] = item
    head_change = any(
        c.role == "head_change"
        and accepted[c.evidence_id].source.range.side == "head"
        and accepted[c.evidence_id].changed_lines
        for c in candidate.citations
    )
    base_behavior = any(
        c.role == "base_behavior"
        and accepted[c.evidence_id].source.range.side == "base"
        and file.change.old is not None
        and accepted[c.evidence_id].source.range.path == file.change.old.path
        for c in candidate.citations
    )
    if file.change.old is None and file.change.new is not None:
        base_behavior = work.blob("base", file.change.new.path) == "not_found"
    if not head_change or not base_behavior:
        return PrivateLead(
            identifier,
            file.id,
            candidate,
            "Pinned base behavior and changed head source are required.",
            tuple(accepted),
        )
    proposed = excerpts.get(candidate.proposed_anchor_id or "")
    anchor = None
    if proposed is not None:
        loc = proposed.source.range
        side = file.change.old if loc.side == "base" else file.change.new
        if (
            side
            and (side.path, side.object_id) == (loc.path, loc.blob_id)
            and loc.start_line == loc.end_line
            and loc.start_line in proposed.changed_lines
            and source_resolves(proposed.source, work)
        ):
            patch = work.hunks(file.change)
            if not isinstance(patch, str) and any(
                loc.start_line in changed_lines(hunk, loc.side) for hunk in patch
            ):
                anchor = loc
    return CheckedCandidate(identifier, file, candidate, tuple(accepted), anchor)


def source_resolves(entry: SourceEvidence, work: LocalEvidenceWork) -> bool:
    loc = entry.range
    blob = work.blob(loc.side, loc.path)
    return (
        not isinstance(blob, str)
        and work.excerpt(
            SourceRange(
                loc.side,
                loc.commit_id,
                loc.path,
                loc.blob_id,
                loc.start_line,
                loc.end_line,
            )
        )
        == entry.text
    )


def _claim(text: str) -> str:
    return " ".join(text.strip().removesuffix(".").split())


def _entry(
    evidence: EvidenceIndex,
    work: LocalEvidenceWork,
    blob: PinnedTextBlob,
    start: int,
    end: int,
) -> SourceEvidence | None:
    loc = SourceRange(blob.side, blob.commit_id, blob.path, blob.blob_id, start, end)
    text = work.excerpt(loc)
    if text is None:
        return None
    key = evidence.source("check_location", loc, text)
    return (
        None
        if key is None
        else SourceEvidence(
            kind="source",
            id=key,
            role="check_location",
            range=SourceLocation.from_range(loc),
            text=text,
        )
    )


def build_verification_packet(
    checked: CheckedCandidate,
    bundle: EvidenceBundle,
    work: LocalEvidenceWork,
    evidence: EvidenceIndex,
    *,
    allow_qualified_consequence: bool = False,
) -> VerificationPacket:
    file = checked.file.change
    by_id = {entry.source.id: entry.source for entry in bundle.excerpts}
    cited = tuple(by_id[identifier] for identifier in checked.cited_ids)
    if file.old is None or file.new is None or file.old.path != file.new.path:
        return VerificationPacket(cited, None)
    base_analysis = work.analysis("base", file.old.path, expected=file.old)
    head_analysis = work.analysis("head", file.new.path, expected=file.new)
    if isinstance(base_analysis, str) or isinstance(head_analysis, str):
        return VerificationPacket(cited, None)
    module = head_analysis.module
    base, head = base_analysis.empty_return, head_analysis.empty_return
    if (
        module is None
        or base_analysis.module != module
        or base is None
        or head is None
        or base.name != head.name
        or head.value == "None"
    ):
        return VerificationPacket(cited, None)
    base_blob, head_blob = base_analysis.diagnostic.blob, head_analysis.diagnostic.blob
    candidate = checked.candidate
    if _claim(candidate.changed_behavior) != _claim(
        f"{module}.{head.name}() returns {head.value} instead of None"
    ):
        return VerificationPacket(cited, None)
    # Both concrete returns must be named in the investigator's citations.
    roles = {(citation.role, citation.evidence_id) for citation in candidate.citations}
    if not all(
        any(
            (role, entry.id) in roles
            and (entry.range.path, entry.range.side, entry.range.blob_id)
            == (blob.path, blob.side, blob.blob_id)
            and entry.range.start_line <= line <= entry.range.end_line
            for entry in cited
        )
        for role, blob, line in (
            ("base_behavior", base_blob, base.line),
            ("head_change", head_blob, head.line),
        )
    ):
        return VerificationPacket(cited, None)
    patch = work.hunks(file)
    if isinstance(patch, str):
        return VerificationPacket(cited, None)
    changed = any(head.line in changed_lines(h, "head") for h in patch)
    matches = work.search("head", head.name)
    if isinstance(matches, str):
        return VerificationPacket(cited, None)
    for path in matches.paths:
        caller_analysis = work.analysis("head", path)
        if isinstance(caller_analysis, str):
            continue
        consumer, caller = caller_analysis.module, caller_analysis.indexed_call
        if (
            consumer is None
            or consumer == module
            or caller is None
            or (caller.module, caller.imported_name) != (module, head.name)
            or _claim(candidate.trigger) != f"Call {consumer}.{caller.name}()"
        ):
            continue
        expected_consequence = f"{consumer}.{caller.name}() raises IndexError"
        consequence = _claim(candidate.consequence)
        exact_consequence = consequence == expected_consequence
        if not exact_consequence and not (
            allow_qualified_consequence
            and consequence.startswith(expected_consequence + " ")
        ):
            continue
        # In both arms check base reachability and the complete head caller as pinned source.
        before_analysis = work.analysis("base", path)
        if isinstance(before_analysis, str):
            break
        before = before_analysis.indexed_call
        if (
            before is None
            or before_analysis.module != consumer
            or (before.module, before.imported_name, before.name)
            != (module, base.name, caller.name)
        ):
            break
        if any(
            work.blob(side, f"{name}/__init__.py") != "not_found"
            for side in ("base", "head")
            for name in (module, consumer)
        ):
            break
        verdict: Literal["supports", "contradicted", "preexisting"]
        if caller.guarded:
            verdict = "contradicted"
        elif base.value != "None" and not before.guarded:
            verdict = "preexisting"
        elif base.value == "None" and changed and exact_consequence:
            verdict = "supports"
        else:
            break
        entries = (
            _entry(evidence, work, base_blob, base.line, base.line),
            _entry(evidence, work, head_blob, head.line, head.line),
            _entry(evidence, work, before_analysis.diagnostic.blob, 1, before.end),
            _entry(evidence, work, caller_analysis.diagnostic.blob, 1, caller.end),
        )
        if any(entry is None for entry in entries):
            break
        material = tuple(entry for entry in entries if entry is not None)
        return VerificationPacket(
            material,
            Witness(
                verdict,
                tuple(entry.id for entry in material),
                (file.new.path, head.name, path, caller.name, "IndexError"),
            ),
        )
    return VerificationPacket(cited, None)


def decide_candidate(
    checked: CheckedCandidate,
    packet: VerificationPacket,
    verification: VerificationReplyV1,
) -> Finding | PrivateLead | Discard:
    known = {entry.id for entry in packet.entries}
    ids = set(verification.evidence_ids)
    witness = packet.witness
    candidate = checked.candidate
    if (
        verification.candidate_id == checked.id
        and witness is not None
        and ids <= known
        and set(witness.evidence_ids) <= ids
    ):
        if witness.verdict in ("contradicted", "preexisting"):
            return Discard(
                checked.id, candidate.file_id, witness.verdict, witness.evidence_ids
            )
        if (
            witness.verdict == "supports"
            and verification.assessment == "supports"
            and candidate.proposed_impact == "material_incorrect_behavior"
        ):
            return Finding(
                checked.id,
                candidate.file_id,
                candidate,
                witness.evidence_ids,
                checked.anchor,
                witness.equivalence,
            )
    return PrivateLead(
        checked.id,
        candidate.file_id,
        candidate,
        "The packet did not establish the claimed behavior, reachability, consequence, and impact.",
        tuple(key for key in verification.evidence_ids if key in known)
        or checked.cited_ids,
    )
