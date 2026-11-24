"""Fail-closed evidence checks for language-independent claims."""

from dataclasses import dataclass
from typing import Literal

from loop_reviewer.reviewer.local_check_contract import SourceEvidence, SourceLocation
from loop_reviewer.reviewer.local_evidence import EvidenceIndex, LocalEvidenceWork
from loop_reviewer.reviewer.model_contract import (
    Candidate,
    CitationRole,
    VerificationReplyV1,
)
from loop_reviewer.reviewer.python_context import changed_lines
from loop_reviewer.reviewer.semantic_inputs import EvidenceBundle, ShownExcerpt
from loop_reviewer.reviewer.semantic_policy import (
    IMPACT_ORDER,
    Discard,
    Finding,
    PrivateLead,
    source_resolves,
)
from loop_reviewer.reviewer.text_references import literal_references


@dataclass(frozen=True)
class SourcePacket:
    id: str
    candidate: Candidate
    entries: tuple[SourceEvidence, ...]
    chain: dict[CitationRole, tuple[str, ...]]
    anchor: SourceLocation | None
    absent_side: Literal["base", "head"] | None


def build_source_packet(
    identifier: str,
    candidate: Candidate,
    bundle: EvidenceBundle,
    work: LocalEvidenceWork,
    index: EvidenceIndex,
) -> SourcePacket | PrivateLead:
    file = next(item for item in bundle.inventory if item.id == candidate.file_id)
    change = file.change
    excerpts = {
        entry.source.id: entry for entry in bundle.excerpts if file.id in entry.file_ids
    }
    accepted: dict[str, SourceEvidence] = {}

    def lead(reason: str) -> PrivateLead:
        return PrivateLead(identifier, file.id, candidate, reason, tuple(accepted))

    for citation in candidate.citations:
        excerpt = excerpts.get(citation.evidence_id)
        if excerpt is None or not source_resolves(excerpt.source, work):
            return lead("A citation does not resolve to pinned source.")
        accepted[citation.evidence_id] = excerpt.source
    absent = "base" if change.old is None else "head" if change.new is None else None
    if absent is not None and work.blob(absent, file.path) != "not_found":
        return lead("The missing side of the changed path was not verified.")
    patch = work.hunks(change)
    if isinstance(patch, str):
        return lead("The affected path's pinned hunks are unavailable.")
    hunks = patch

    def on_edit(excerpt: ShownExcerpt) -> bool:
        loc = excerpt.source.range
        expected = change.old if loc.side == "base" else change.new
        return (
            expected is not None
            and (loc.path, loc.blob_id) == (expected.path, expected.object_id)
            and any(
                loc.start_line <= line <= loc.end_line
                for hunk in hunks
                for line in changed_lines(hunk, loc.side)
            )
        )

    roles: dict[CitationRole, list[str]] = {}
    for citation in candidate.citations:
        key = citation.evidence_id
        loc = accepted[key].range
        role = citation.role
        if role == "head_change":
            valid = loc.side == ("base" if absent == "head" else "head") and on_edit(
                excerpts[key]
            )
        elif role == "base_behavior":
            valid = loc.side == "base" and (
                change.old is None
                or (loc.path, loc.blob_id) == (change.old.path, change.old.object_id)
            )
        else:
            valid = loc.side == "head"
        if not valid:
            return lead(
                "Citation role does not match the affected path or pinned side."
            )
        roles.setdefault(role, []).append(key)
    required = {"base_behavior", "head_change", "trigger", "consequence", "impact"}
    if not required <= roles.keys() or candidate.proposed_impact == "unknown":
        return lead(
            "The base, change, reachable trigger, consequence, and impact need evidence."
        )
    anchor = None
    if (proposed := excerpts.get(candidate.proposed_anchor_id or "")) is not None:
        loc = proposed.source.range
        if (
            loc.start_line == loc.end_line
            and on_edit(proposed)
            and source_resolves(proposed.source, work)
        ):
            anchor = loc
    material = tuple(accepted.values())
    alternatives = literal_references(
        tuple(accepted[key] for key in roles["head_change"]),
        work,
        index,
        sides=("head",),
        exclude_ids=frozenset(accepted),
    )
    return SourcePacket(
        identifier,
        candidate,
        (*material, *alternatives),
        {role: tuple(ids) for role, ids in roles.items()},
        anchor,
        absent,
    )


def decide_source_claim(
    packet: SourcePacket, reply: VerificationReplyV1
) -> Finding | PrivateLead | Discard:
    candidate = packet.candidate
    cited = set(reply.evidence_ids)
    known = {entry.id for entry in packet.entries}
    if reply.candidate_id == packet.id and cited <= known and reply.explanation.strip():
        if reply.assessment == "contradicts" and (
            cited & set(packet.chain["base_behavior"])
            or cited
            & {entry.id for entry in packet.entries if entry.range.side == "head"}
        ):
            return Discard(
                packet.id, candidate.file_id, "contradicted", reply.evidence_ids
            )
        if reply.assessment == "supports" and all(
            cited.intersection(ids) for ids in packet.chain.values()
        ):
            impact = IMPACT_ORDER[candidate.proposed_impact]
            severity = ("high", "medium", "low")[impact]
            return Finding(
                packet.id,
                candidate.file_id,
                candidate,
                reply.evidence_ids,
                packet.anchor,
                (
                    candidate.file_id,
                    " ".join(candidate.changed_behavior.split()),
                    " ".join(candidate.trigger.split()),
                    " ".join(candidate.consequence.split()),
                    *sorted(key for ids in packet.chain.values() for key in ids),
                ),
                severity=severity,
                python_witness=False,
            )
    return PrivateLead(
        packet.id,
        candidate.file_id,
        candidate,
        "Independent verification did not establish the complete cited claim.",
        tuple(key for key in reply.evidence_ids if key in known),
    )
