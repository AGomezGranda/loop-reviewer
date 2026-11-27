"""Run required compiler checks before spending a bounded budget on context."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Literal

from loop_reviewer.reviewer.inventory import (
    InventorySourceError,
    RevisionSide,
    validated_inventory,
)
from loop_reviewer.reviewer.inventory_contract import (
    ChangedFile,
    InventoryUsage,
    PinnedComparison,
)
from loop_reviewer.reviewer.local_check_contract import (
    CompileFinding,
    CompleteLocalCheck,
    ContextStatus,
    FailedLocalCheck,
    Gap,
    Investigation,
    LocalCheckRequest,
    LocalCheckResult,
    LocalFileCoverage,
    PartialLocalCheck,
    SourceLocation,
)
from loop_reviewer.reviewer.local_check_policy import (
    decide_compile_regression,
    regression_candidate,
)
from loop_reviewer.reviewer.local_evidence import (
    EvidenceIndex,
    LocalEvidenceWork,
    WorkLimit,
    collect_python_context,
    python_sides,
)
from loop_reviewer.reviewer.pinned_source import (
    PinnedSource,
    ReadFailure,
    SourceRange,
)
from loop_reviewer.reviewer.python_context import (
    PythonAnalysis,
    PythonAnalyzer,
    changed_lines,
)


@dataclass
class CompileCheck:
    row: ChangedFile
    investigation_id: str | None = None
    reason: Gap | Literal["binary", "unsupported_language"] | None = None
    context: ContextStatus = "not_applicable"
    evidence_ids: list[str] = field(default_factory=list)
    analysis: dict[RevisionSide, PythonAnalysis] = field(default_factory=dict)

    def coverage(self) -> LocalFileCoverage:
        return LocalFileCoverage(
            **self.row.model_dump(),
            outcome="excluded"
            if self.reason == "binary"
            else "unfinished"
            if self.reason
            else "complete",
            reason=self.reason,
            highest_completed_check="mechanical"
            if self.reason is None
            else "inventory_only",
            required_investigation_ids=(self.investigation_id,)
            if self.investigation_id
            else (),
            context_status=self.context,
        )

    def investigation(self) -> Investigation | None:
        if self.investigation_id is None:
            return None
        reason = self.reason
        assert reason in (None, "work_limit", "missing_evidence")
        return Investigation(
            kind="python_compile",
            id=self.investigation_id,
            path=_path(self.row),
            outcome="unfinished" if reason else "complete",
            reason=reason,
            evidence_ids=tuple(self.evidence_ids),
        )


def _path(change: ChangedFile) -> str:
    side = change.new or change.old
    assert side is not None
    return side.path


def _gap(failure: ReadFailure) -> Gap:
    return "work_limit" if failure == "limit_reached" else "missing_evidence"


def _required(
    check: CompileCheck, work: LocalEvidenceWork, evidence: EvidenceIndex
) -> SourceRange | None:
    for side, file in python_sides(check.row):
        result = work.analysis(side, file.path, expected=file)
        if isinstance(result, str):
            check.reason = check.reason or _gap(result)
        else:
            check.analysis[side] = result
            check.evidence_ids.append(evidence.diagnostic(result.diagnostic))
    old, new = check.analysis.get("base"), check.analysis.get("head")
    base, head = old.diagnostic if old else None, new.diagnostic if new else None
    candidate = regression_candidate(check.row, base, head)
    if check.reason or candidate is None:
        return None
    assert head is not None
    if check.row.change_type == "added":
        prior = work.blob("base", candidate.path)
        if prior != "not_found":
            if prior in ("limit_reached", "unavailable"):
                assert isinstance(prior, str)
                check.reason = _gap(prior)
            return None
    hunks = work.hunks(check.row)
    if isinstance(hunks, str):
        check.reason = _gap(hunks)
        return None
    lines = {line for hunk in hunks for line in changed_lines(hunk, "head")}
    return decide_compile_regression(check.row, base, head, lines)


def _finding(number: int, check: CompileCheck, anchor: SourceRange) -> CompileFinding:
    head = check.analysis["head"].diagnostic
    comparison = (
        "This file is absent at the pinned base."
        if check.row.old is None
        else "The paired base file compiles under the same interpreter."
    )
    return CompileFinding(
        id=f"f{number:05d}",
        severity="medium",
        confidence="confirmed",
        decision_reason="reproduced_regression",
        trigger=f"Compile the pinned head file with CPython {head.tool_version}. {comparison}",
        consequence=f"CPython rejects the head file: {head.message}"[:1024],
        evidence_ids=tuple(check.evidence_ids),
        anchor=SourceLocation.from_range(anchor),
    )


def review_local_checks(
    request: LocalCheckRequest,
    open_source: Callable[[PinnedComparison], AbstractContextManager[PinnedSource]],
    analyzer: PythonAnalyzer,
    monotonic: Callable[[], float],
) -> LocalCheckResult:
    common = {
        "schema_version": 2,
        "policy_version": 1,
        "scope": "local_checks",
        "comparison": request.comparison,
        "discussion_status": "not_requested",
        "usage": InventoryUsage(
            model_calls=0,
            input_tokens=0,
            output_tokens=0,
            estimated_cost_usd_micros=0,
            unaccounted_reserved_usd_micros=0,
        ),
        "private_leads": (),
        "linked_existing_threads": (),
    }
    try:
        with open_source(request.comparison) as source:
            rows = validated_inventory(source, request.comparison)
            work = LocalEvidenceWork(
                source, analyzer, request.comparison, monotonic, monotonic() + 30
            )
            checks, evidence, findings, stopped = run_local_checks(rows, work)
    except InventorySourceError as error:
        return LocalCheckResult(
            FailedLocalCheck.model_validate(
                common
                | {
                    "status": "failed",
                    "stop_reason": "fatal_error",
                    "error": error.error,
                    "recommendation": "none",
                    "file_coverage": (),
                    "investigation_coverage": (),
                    "evidence_index": (),
                    "findings": (),
                }
            )
        )
    report = common | {
        "error": None,
        "recommendation": "comment" if findings else "none",
        "file_coverage": tuple(check.coverage() for check in checks),
        "investigation_coverage": tuple(
            investigation
            for check in checks
            if (investigation := check.investigation()) is not None
        ),
        "findings": tuple(findings),
        "evidence_index": tuple(evidence.entries),
    }
    if any(check.reason not in (None, "binary") for check in checks):
        return LocalCheckResult(
            PartialLocalCheck.model_validate(
                report
                | {
                    "status": "partial",
                    "stop_reason": "work_limit" if stopped else "finished",
                }
            )
        )
    return LocalCheckResult(
        CompleteLocalCheck.model_validate(
            report | {"status": "complete", "stop_reason": "finished"}
        )
    )


def run_local_checks(
    rows: tuple[ChangedFile, ...],
    work: LocalEvidenceWork,
    *,
    retrieve_context: bool = True,
    eligible: Callable[[ChangedFile], bool] | None = None,
) -> tuple[list[CompileCheck], EvidenceIndex, list[CompileFinding], bool]:
    checks: list[CompileCheck] = []
    evidence = EvidenceIndex()
    findings: list[CompileFinding] = []
    stopped = False
    next_id = 0
    for row in rows:
        check = CompileCheck(row)
        checks.append(check)
        if row.binary:
            check.reason = "binary"
        elif (eligible is not None and not eligible(row)) or not python_sides(row):
            check.reason = "unsupported_language"
        else:
            next_id += 1
            check.investigation_id = f"i{next_id:05d}"
            if stopped:
                check.reason = "work_limit"
                continue
            try:
                anchor = _required(check, work, evidence)
                if anchor:
                    findings.append(_finding(len(findings) + 1, check, anchor))
            except WorkLimit:
                check.reason = "work_limit"
                stopped = True
    for check in checks if retrieve_context else ():
        if check.investigation_id is not None:
            try:
                check.context = collect_python_context(
                    check.row,
                    check.analysis,
                    work,
                    evidence,
                    missing_analysis="limit_reached"
                    if check.reason == "work_limit"
                    else "unavailable",
                ).status
            except WorkLimit:
                check.context = "limited"
    return checks, evidence, findings, stopped
