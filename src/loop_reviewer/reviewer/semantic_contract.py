"""Version 3 wire contract for a full review of a pinned comparison."""

from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, RootModel, model_validator

from loop_reviewer.reviewer.inventory_contract import (
    MAX_CHANGES,
    ChangedFile,
    InventoryBudget,
    InventoryError,
    PinnedComparison,
    _Empty,
    _Path,
    _Record,
    _Version,
)
from loop_reviewer.reviewer.local_check_contract import (
    MAX_EVIDENCE,
    MAX_FINDINGS,
    DiagnosticEvidence,
    Evidence,
    Investigation,
    SourceEvidence,
    SourceLocation,
)
from loop_reviewer.reviewer.local_check_policy import supported_python
from loop_reviewer.reviewer.model_budget import MAX_CALLS
from loop_reviewer.reviewer.semantic_limits import BehaviorGap

_V3 = Annotated[int, Field(ge=3, le=3)]
_Id = Annotated[str, Field(pattern=r"^[bcefip][0-9]{5}$", max_length=6)]
_Text = Annotated[str, Field(min_length=1, max_length=1024)]
_Ids = Annotated[tuple[_Id, ...], Field(max_length=16)]
_UsageCount = Annotated[int, Field(ge=0, le=2**63 - 1)]


class SemanticReviewRequest(_Record):
    schema_version: _V3
    scope: Literal["full_review"]
    comparison: PinnedComparison
    budget: InventoryBudget
    discussion: None


class SemanticUsage(_Record):
    model_calls: Annotated[int, Field(ge=0, le=MAX_CALLS)]
    input_tokens: _UsageCount
    output_tokens: _UsageCount
    estimated_cost_usd_micros: _UsageCount
    unaccounted_reserved_usd_micros: _UsageCount
    accounting_fault: bool

    @model_validator(mode="after")
    def requires_calls(self) -> Self:
        if not self.model_calls and any(
            (
                self.input_tokens,
                self.output_tokens,
                self.estimated_cost_usd_micros,
                self.unaccounted_reserved_usd_micros,
                self.accounting_fault,
            )
        ):
            raise ValueError("Usage requires a model call")
        return self


class SemanticCoverage(ChangedFile):
    file_id: _Id
    outcome: Literal["excluded", "complete", "unfinished"]
    reason: BehaviorGap | Literal["binary"] | None
    highest_completed_check: Literal["inventory_only", "mechanical", "semantic"]
    required_investigation_ids: Annotated[tuple[_Id, ...], Field(max_length=2)]


class CandidateDecision(_Record):
    id: _Id
    reason: Literal["contradicted", "preexisting", "duplicate"]
    evidence_ids: _Ids


class BehaviorInvestigation(_Record):
    kind: Literal["behavior"]
    id: _Id
    path: _Path
    outcome: Literal["complete", "unfinished"]
    reason: BehaviorGap | None
    evidence_ids: _Ids
    discarded_candidates: Annotated[tuple[CandidateDecision, ...], Field(max_length=4)]

    @model_validator(mode="after")
    def reason_only_for_unfinished(self) -> Self:
        if (self.reason is None) != (self.outcome == "complete"):
            raise ValueError("Behavior gap and outcome disagree")
        return self


class SemanticFinding(_Record):
    id: _Id
    file_id: _Id
    severity: Literal["high", "medium", "low"]
    confidence: Literal["confirmed", "high"]
    decision_reason: Literal["reproduced_regression", "supported_by_source"]
    changed_behavior: _Text
    trigger: _Text
    consequence: _Text
    evidence_ids: _Ids
    anchor: SourceLocation | None

    @model_validator(mode="after")
    def evidence_decides_confidence(self) -> Self:
        if (self.confidence == "confirmed") != (
            self.decision_reason == "reproduced_regression"
        ) or not self.evidence_ids:
            raise ValueError("Finding disposition and evidence disagree")
        if self.anchor is not None and self.anchor.start_line != self.anchor.end_line:
            raise ValueError("Inline anchors must name one source line")
        return self


class SemanticLead(_Record):
    id: _Id
    file_id: _Id
    changed_behavior: _Text
    trigger: _Text
    consequence: _Text
    missing_fact: _Text
    evidence_ids: _Ids


class InvalidReport(_Record):
    code: Literal["invalid_report"]
    message: _Text


class _Common(_Record):
    schema_version: _V3
    policy_version: _Version
    scope: Literal["full_review"]
    comparison: PinnedComparison
    discussion: None
    discussion_status: Literal["not_requested"]
    usage: SemanticUsage
    linked_existing_threads: _Empty


class ReviewedPayload(_Record):
    comparison: PinnedComparison
    error: None
    recommendation: Literal["comment", "none"]
    file_coverage: Annotated[
        tuple[SemanticCoverage, ...], Field(max_length=MAX_CHANGES)
    ]
    investigation_coverage: Annotated[
        tuple[
            Annotated[
                Investigation | BehaviorInvestigation, Field(discriminator="kind")
            ],
            ...,
        ],
        Field(max_length=2 * MAX_CHANGES),
    ]
    evidence_index: Annotated[tuple[Evidence, ...], Field(max_length=MAX_EVIDENCE)]
    findings: Annotated[tuple[SemanticFinding, ...], Field(max_length=MAX_FINDINGS + 4)]
    private_leads: Annotated[tuple[SemanticLead, ...], Field(max_length=4)]

    @model_validator(mode="after")
    def closed_references(self) -> Self:
        by_file = {row.file_id: row for row in self.file_coverage}
        work = {row.id: row for row in self.investigation_coverage}
        evidence = {row.id: row for row in self.evidence_index}
        if (
            len(by_file) != len(self.file_coverage)
            or len(work) != len(self.investigation_coverage)
            or len(evidence) != len(self.evidence_index)
        ):
            raise ValueError("Duplicate file, investigation, or evidence ID")
        if bool(self.findings) != (self.recommendation == "comment"):
            raise ValueError("Recommendation must reflect retained findings")
        side_paths = {
            (side, item.path): item.object_id
            for row in self.file_coverage
            for side, item in (("base", row.old), ("head", row.new))
            if item is not None
        }
        if len(side_paths) != sum(
            int(row.old is not None) + int(row.new is not None)
            for row in self.file_coverage
        ) or any(len(oid) != len(self.comparison.head) for oid in side_paths.values()):
            raise ValueError("Invalid pinned inventory identity")
        all_work: set[str] = set()
        for row in self.file_coverage:
            assigned = row.required_investigation_ids
            if any(key not in work or key in all_work for key in assigned) or len(
                assigned
            ) != len(set(assigned)):
                raise ValueError("Investigations must belong to one inventory row")
            all_work.update(assigned)
            self._file_outcome(row, tuple(work[key] for key in assigned), evidence)
        if all_work != work.keys():
            raise ValueError("Unreferenced investigation")
        for item in self.evidence_index:
            loc = item.range if isinstance(item, SourceEvidence) else item
            commit = (
                self.comparison.merge_base
                if loc.side == "base"
                else self.comparison.head
            )
            expected = side_paths.get((loc.side, loc.path), loc.blob_id)
            if (loc.commit_id, loc.blob_id) != (commit, expected):
                raise ValueError("Evidence does not match the pinned comparison")
        discarded = tuple(
            item
            for row in self.investigation_coverage
            if isinstance(row, BehaviorInvestigation)
            for item in row.discarded_candidates
        )
        dispositions = (*self.findings, *self.private_leads, *discarded)
        if len({item.id for item in dispositions}) != len(dispositions):
            raise ValueError("Duplicate candidate disposition ID")
        for item in (*self.investigation_coverage, *dispositions):
            if (
                len(set(item.evidence_ids)) != len(item.evidence_ids)
                or not set(item.evidence_ids) <= evidence.keys()
            ):
                raise ValueError("Unresolvable or duplicate evidence citation")
        for item in (*self.findings, *self.private_leads):
            if (
                item.file_id not in by_file
                or not by_file[item.file_id].required_investigation_ids
            ):
                raise ValueError("Disposition must belong to an investigated file")
        for lead in self.private_leads:
            row = by_file[lead.file_id]
            if row.outcome != "unfinished":
                raise ValueError(
                    "A private lead leaves its behavior investigation unfinished"
                )
        for finding in self.findings:
            row = by_file[finding.file_id]
            compile_check = work[row.required_investigation_ids[0]]
            types = tuple(evidence[key] for key in finding.evidence_ids)
            if finding.decision_reason == "reproduced_regression":
                diagnostics = {
                    item.side: item
                    for item in types
                    if isinstance(item, DiagnosticEvidence)
                }
                base, head = diagnostics.get("base"), diagnostics.get("head")
                if (
                    compile_check.kind != "python_compile"
                    or compile_check.outcome != "complete"
                    or set(finding.evidence_ids) != set(compile_check.evidence_ids)
                    or head is None
                    or head.result != "syntax_error"
                    or head.location is None
                    or (finding.anchor is not None and finding.anchor != head.location)
                    or (
                        row.old is not None
                        and (
                            base is None
                            or base.result != "passed"
                            or base.tool_version != head.tool_version
                        )
                    )
                ):
                    raise ValueError(
                        "Confirmed finding must cite its completed compiler check"
                    )
            elif not all(isinstance(entry, SourceEvidence) for entry in types) or {
                entry.range.side for entry in types if isinstance(entry, SourceEvidence)
            } != {"base", "head"}:
                raise ValueError("Semantic findings must cite both pinned sides")
            if finding.anchor is not None:
                anchor = finding.anchor
                side = row.old if anchor.side == "base" else row.new
                commit = (
                    self.comparison.merge_base
                    if anchor.side == "base"
                    else self.comparison.head
                )
                if side is None or (anchor.commit_id, anchor.path, anchor.blob_id) != (
                    commit,
                    side.path,
                    side.object_id,
                ):
                    raise ValueError("Anchor must match the affected pinned path")
                if finding.decision_reason == "supported_by_source" and not any(
                    isinstance(entry, SourceEvidence) and entry.range == anchor
                    for entry in self.evidence_index
                ):
                    raise ValueError("Semantic anchor must resolve to a pinned excerpt")
        return self

    @staticmethod
    def _file_outcome(
        row: SemanticCoverage,
        work: tuple[Investigation | BehaviorInvestigation, ...],
        evidence: dict[str, Evidence],
    ) -> None:
        eligible = supported_python(row.old) or supported_python(row.new)
        if row.binary or not eligible:
            expected = (
                ("excluded", "binary")
                if row.binary
                else ("unfinished", "unsupported_language")
            )
            if (
                (row.outcome, row.reason) != expected
                or work
                or row.highest_completed_check != "inventory_only"
            ):
                raise ValueError("File eligibility and coverage disagree")
            return
        if (
            len(work) != 2
            or not isinstance(work[0], Investigation)
            or not isinstance(work[1], BehaviorInvestigation)
        ):
            raise ValueError("An eligible path requires compiler and behavior coverage")
        compiler, behavior = work[0], work[1]
        path = row.new or row.old
        assert path is not None
        if compiler.path != path.path or behavior.path != path.path:
            raise ValueError("Investigations must name the assigned file")
        comp_done = compiler.outcome == "complete"
        behavior_done = behavior.outcome == "complete"
        level = (
            "semantic"
            if comp_done and behavior_done
            else "mechanical"
            if comp_done
            else "inventory_only"
        )
        reason = compiler.reason or behavior.reason
        if (row.highest_completed_check, row.outcome, row.reason) != (
            level,
            "complete" if reason is None else "unfinished",
            reason,
        ):
            raise ValueError("File outcome must reflect its investigations")
        validate_compiler_evidence(row, compiler, evidence)


def validate_compiler_evidence(
    row: SemanticCoverage, compiler: Investigation, evidence: dict[str, Evidence]
) -> None:
    expected_diagnostics = {
        (side, item.path, item.object_id)
        for side, item in (("base", row.old), ("head", row.new))
        if item is not None and supported_python(item)
    }
    diagnostics = {key: evidence.get(key) for key in compiler.evidence_ids}
    actual = {
        (item.side, item.path, item.blob_id)
        for item in diagnostics.values()
        if isinstance(item, DiagnosticEvidence)
    }
    if (
        len(actual) != len(diagnostics)
        or not actual <= expected_diagnostics
        or (compiler.outcome == "complete" and actual != expected_diagnostics)
    ):
        raise ValueError("Compiler coverage must use the proper pinned diagnostics")


class _Reviewed(ReviewedPayload, _Common):
    pass


class CompleteSemanticReview(_Reviewed):
    status: Literal["complete"]
    stop_reason: Literal["finished"]

    @model_validator(mode="after")
    def entirely_complete(self) -> Self:
        if (
            any(row.outcome == "unfinished" for row in self.file_coverage)
            or self.usage.accounting_fault
        ):
            raise ValueError("Complete review has outstanding work or accounting fault")
        return self


class PartialSemanticReview(_Reviewed):
    status: Literal["partial"]
    stop_reason: Literal["finished", "work_limit", "budget_exhausted"]

    @model_validator(mode="after")
    def has_gaps(self) -> Self:
        if not any(row.outcome == "unfinished" for row in self.file_coverage):
            raise ValueError("Partial review must have unfinished paths")
        if self.stop_reason != "finished" and not any(
            row.reason == self.stop_reason
            for row in (*self.file_coverage, *self.investigation_coverage)
        ):
            raise ValueError("Stop reason must explain unfinished work")
        return self


class FailedSemanticReview(_Common):
    status: Literal["failed"]
    stop_reason: Literal["fatal_error"]
    error: InventoryError | InvalidReport
    recommendation: Literal["none"]
    file_coverage: _Empty
    investigation_coverage: _Empty
    evidence_index: _Empty
    findings: _Empty
    private_leads: _Empty


class SemanticReviewResult(
    RootModel[
        Annotated[
            CompleteSemanticReview | PartialSemanticReview | FailedSemanticReview,
            Field(discriminator="status"),
        ]
    ]
):
    model_config = ConfigDict(strict=True, frozen=True, revalidate_instances="always")
