"""Assemble a semantic report from completed pinned review work."""

from dataclasses import asdict
from typing import Literal

from loop_reviewer.reviewer.discussion_policy import (
    Attachment,
    DiscussionContext,
    link_verified_threads,
)
from loop_reviewer.reviewer.discussion_report import (
    DiscussionDetails,
    DiscussionReference,
    LinkedExistingThread,
)
from loop_reviewer.reviewer.full_review_contract import text_eligible
from loop_reviewer.reviewer.inventory_contract import PinnedComparison, decode_path
from loop_reviewer.reviewer.local_check_contract import (
    CompileFinding,
    Evidence,
    Investigation,
)
from loop_reviewer.reviewer.local_checks import CompileCheck
from loop_reviewer.reviewer.model_budget import SpendLedger
from loop_reviewer.reviewer.semantic_batch import BatchOutcome
from loop_reviewer.reviewer.semantic_contract import (
    BehaviorInvestigation,
    CandidateDecision,
    SemanticCoverage,
    SemanticFinding,
    SemanticLead,
    SemanticReviewResult,
    SemanticUsage,
)
from loop_reviewer.reviewer.semantic_limits import BehaviorGap


def _common(comparison: PinnedComparison, ledger: SpendLedger) -> dict[str, object]:
    return {
        "scope": "full_review",
        "comparison": comparison,
        "usage": SemanticUsage.model_validate(asdict(ledger.snapshot())),
    }


def render_v3(payload: dict[str, object]) -> SemanticReviewResult:
    return SemanticReviewResult.model_validate(
        payload
        | {
            "schema_version": 3,
            "policy_version": 1,
            "discussion": None,
            "discussion_status": "not_requested",
            "linked_existing_threads": (),
        }
    )


def failed_report(
    comparison: PinnedComparison, ledger: SpendLedger, error: object
) -> dict[str, object]:
    return _common(comparison, ledger) | {
        "status": "failed",
        "stop_reason": "fatal_error",
        "error": error,
        "recommendation": "none",
        "file_coverage": (),
        "investigation_coverage": (),
        "evidence_index": (),
        "findings": (),
        "private_leads": (),
    }


def assemble_report(
    comparison: PinnedComparison,
    ledger: SpendLedger,
    checks: list[CompileCheck],
    compiler_findings: list[CompileFinding],
    evidence: tuple[Evidence, ...],
    batch: BatchOutcome,
    compiler_stopped: bool,
    discussion: DiscussionContext | None,
    *,
    full: bool = False,
) -> tuple[dict[str, object], DiscussionDetails]:
    disposition = (
        link_verified_threads(batch.findings, discussion, batch.evidence)
        if discussion
        else None
    )
    retained = (
        {item.id for item in disposition.kept}
        if disposition
        else {item.id for item in batch.findings}
    )
    references: list[DiscussionReference] = []
    candidate_owners: dict[str, str] = {}
    coverage: list[SemanticCoverage] = []
    investigations: list[Investigation | BehaviorInvestigation] = []
    findings: list[SemanticFinding] = []
    leads: list[SemanticLead] = []
    indexed = {entry.id for entry in evidence}
    confirmed = {
        finding.anchor.path: finding
        for finding in compiler_findings
        if finding.anchor is not None
    }
    for number, check in enumerate(checks, 1):
        file_id = f"p{number:05d}"
        side = check.row.new or check.row.old
        assert side is not None
        reason: BehaviorGap | Literal["binary"] | None = check.reason
        highest: Literal["inventory_only", "mechanical", "semantic"] = "inventory_only"
        assigned: tuple[str, ...] = ()
        compiler = check.investigation()
        eligible = text_eligible(check.row) if full else compiler is not None
        if full and not eligible:
            reason = "binary" if check.row.binary else "unsupported_language"
        if eligible:
            behavior_id = f"b{number:05d}"
            if discussion is not None:
                references.extend(
                    _reference(item, behavior_id)
                    for item in discussion.by_file.get(check.row, ())
                )
            mechanical = confirmed.get(side.path) if check.row.new is not None else None
            issue_ids: dict[str, None] = {}
            decisions: list[CandidateDecision] = []
            gap = batch.gaps.get(file_id)
            if gap is None and file_id not in batch.reviewed_ids:
                gap = "not_scheduled"
            if mechanical is not None:
                issue_ids.update(dict.fromkeys(mechanical.evidence_ids))
                findings.append(
                    SemanticFinding(
                        file_id=file_id,
                        changed_behavior="The pinned head no longer compiles.",
                        **mechanical.model_dump(),
                    )
                )
                if not full:
                    gap = None
            if mechanical is None or full:
                for semantic in batch.findings:
                    if semantic.file_id == file_id:
                        issue_ids.update(dict.fromkeys(semantic.evidence_ids))
                        if semantic.id not in retained:
                            candidate_owners[semantic.id] = behavior_id
                            continue
                        findings.append(
                            SemanticFinding(
                                id=semantic.id,
                                file_id=file_id,
                                severity=semantic.severity,
                                confidence="high",
                                decision_reason="supported_by_source",
                                changed_behavior=semantic.candidate.changed_behavior,
                                trigger=semantic.candidate.trigger,
                                consequence=semantic.candidate.consequence,
                                evidence_ids=semantic.evidence_ids,
                                anchor=semantic.anchor,
                            )
                        )
                for lead in batch.leads:
                    if lead.file_id == file_id:
                        leads.append(
                            SemanticLead(
                                id=lead.id,
                                file_id=file_id,
                                changed_behavior=lead.candidate.changed_behavior,
                                trigger=lead.candidate.trigger,
                                consequence=lead.candidate.consequence,
                                missing_fact=lead.missing_fact,
                                evidence_ids=tuple(
                                    dict.fromkeys(
                                        key
                                        for key in lead.evidence_ids
                                        if key in indexed
                                    )
                                ),
                            )
                        )
                for discarded in batch.discards:
                    if discarded.file_id == file_id:
                        decisions.append(
                            CandidateDecision(
                                id=discarded.id,
                                reason=discarded.reason,
                                evidence_ids=discarded.evidence_ids,
                            )
                        )
            reason = gap
            if compiler is not None:
                reason = compiler.reason or gap
                if compiler.outcome == "complete":
                    highest = "mechanical"
                assigned = (compiler.id,)
                investigations.append(compiler)
            if reason is None:
                highest = "semantic"
            assigned += (behavior_id,)
            investigations.append(
                BehaviorInvestigation(
                    id=behavior_id,
                    kind="behavior",
                    path=side.path,
                    outcome="complete" if gap is None else "unfinished",
                    reason=gap,
                    evidence_ids=tuple(issue_ids)[:16],
                    discarded_candidates=tuple(decisions),
                )
            )
        coverage.append(
            SemanticCoverage(
                **check.row.model_dump(),
                file_id=file_id,
                outcome="excluded"
                if reason == "binary"
                else "unfinished"
                if reason is not None
                else "complete",
                reason=reason,
                highest_completed_check=highest,
                required_investigation_ids=assigned,
            )
        )
    paths = {
        row.file_id: decode_path(side.path)
        for row in coverage
        if (side := row.new or row.old) is not None
    }
    findings.sort(
        key=lambda f: (
            ("high", "medium", "low").index(f.severity),
            f.confidence != "confirmed",
            paths[f.file_id],
            f.anchor.start_line if f.anchor is not None else 0,
            f.id,
        )
    )
    details = DiscussionDetails(
        references=tuple(references),
        links=tuple(
            LinkedExistingThread(
                thread_id=link.thread_id,
                investigation_id=candidate_owners[link.finding.id],
                candidate_id=link.finding.id,
                evidence_ids=link.finding.evidence_ids,
                reason="existing_thread",
            )
            for link in disposition.linked
        )
        if disposition
        else (),
        omissions=discussion.omissions if discussion else (),
    )
    payload = _common(comparison, ledger) | {
        "recommendation": "comment" if findings else "none",
        "error": None,
        "file_coverage": tuple(coverage),
        "investigation_coverage": tuple(investigations),
        "evidence_index": evidence,
        "findings": tuple(findings),
        "private_leads": tuple(leads),
    }
    if any(row.outcome == "unfinished" for row in coverage):
        stop = (
            "work_limit"
            if compiler_stopped or batch.stop == "work_limit"
            else "budget_exhausted"
            if batch.stop == "budget_exhausted"
            else "finished"
        )
        if not any(row.reason == stop for row in (*coverage, *investigations)):
            stop = "finished"
        return payload | {"status": "partial", "stop_reason": stop}, details
    return payload | {"status": "complete", "stop_reason": "finished"}, details


def _reference(item: Attachment, investigation: str) -> DiscussionReference:
    thread = item.thread
    return DiscussionReference(
        kind=item.kind,
        item_id=item.item_id,
        investigation_id=investigation,
        is_resolved=thread.is_resolved if thread else None,
        is_outdated=thread.is_outdated if thread else None,
        permalink=thread.permalink if thread else None,
    )
