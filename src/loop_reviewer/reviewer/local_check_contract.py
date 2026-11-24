"""Version 2 wire records for model-free local checks."""

from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, RootModel, model_validator

from loop_reviewer.reviewer.inventory import RevisionSide
from loop_reviewer.reviewer.inventory_contract import (
    MAX_CHANGES,
    ChangedFile,
    FileSide,
    InventoryError,
    InventoryUsage,
    PinnedComparison,
    _Empty,
    _ObjectId,
    _Path,
    _Record,
    _Version,
    _Zero,
    canonical_path,
)
from loop_reviewer.reviewer.local_check_policy import supported_python
from loop_reviewer.reviewer.pinned_source import SourceRange

MAX_EVIDENCE = 4096
MAX_FINDINGS = 1000
_Version2 = Annotated[int, Field(ge=2, le=2)]
_ID = Annotated[str, Field(min_length=1, max_length=64)]
_EvidenceIDs = Annotated[tuple[_ID, ...], Field(max_length=16)]
_Message = Annotated[str, Field(min_length=1, max_length=1024)]
_Line = Annotated[int, Field(ge=1, le=2**63 - 1)]
Gap = Literal["work_limit", "missing_evidence"]
ContextStatus = Literal["not_applicable", "complete", "limited", "unavailable"]
SourceRole = Literal[
    "base_declaration",
    "head_declaration",
    "call_site",
    "test_call",
    "text_match",
    "check_location",
]


class LocalCheckBudget(_Record):
    max_model_spend_usd_micros: _Zero


class LocalCheckRequest(_Record):
    schema_version: _Version2
    scope: Literal["local_checks"]
    comparison: PinnedComparison
    budget: LocalCheckBudget
    discussion: None


class LocalFileCoverage(ChangedFile):
    outcome: Literal["complete", "excluded", "unfinished"]
    reason: (
        Literal["binary", "unsupported_language", "work_limit", "missing_evidence"]
        | None
    )
    highest_completed_check: Literal["inventory_only", "mechanical"]
    required_investigation_ids: Annotated[tuple[_ID, ...], Field(max_length=1)]
    context_status: ContextStatus

    @model_validator(mode="after")
    def coverage_agrees(self) -> Self:
        expected: tuple[str, str | None]
        if self.binary:
            expected = "excluded", "binary"
        elif not (supported_python(self.old) or supported_python(self.new)):
            expected = "unfinished", "unsupported_language"
        else:
            if len(self.required_investigation_ids) != 1:
                raise ValueError("Expected exactly one compiler investigation")
            if (self.outcome, self.reason) not in (
                ("complete", None),
                ("unfinished", "work_limit"),
                ("unfinished", "missing_evidence"),
            ):
                raise ValueError("Invalid compiler coverage")
            expected = self.outcome, self.reason
        if (self.outcome, self.reason) != expected:
            raise ValueError("File eligibility and coverage disagree")
        if self.reason in ("binary", "unsupported_language") and (
            self.required_investigation_ids or self.context_status != "not_applicable"
        ):
            raise ValueError("An ineligible file cannot have compiler or context work")
        if (self.highest_completed_check == "mechanical") != (
            self.outcome == "complete"
        ):
            raise ValueError("Mechanical coverage requires a completed check")
        return self


class Investigation(_Record):
    kind: Literal["python_compile"]
    id: _ID
    path: _Path
    outcome: Literal["complete", "unfinished"]
    reason: Gap | None
    evidence_ids: _EvidenceIDs

    @model_validator(mode="after")
    def valid_investigation(self) -> Self:
        canonical_path(self.path)
        if (self.outcome == "complete") != (self.reason is None):
            raise ValueError("Only unfinished investigations have a gap")
        return self


class _PinnedIdentity(_Record):
    side: RevisionSide
    commit_id: _ObjectId
    path: _Path
    blob_id: _ObjectId

    @model_validator(mode="after")
    def path_and_hash(self) -> Self:
        canonical_path(self.path)
        if len(self.commit_id) != len(self.blob_id):
            raise ValueError("Blob and commit must use the same hash format")
        return self


class SourceLocation(_PinnedIdentity):
    start_line: _Line
    end_line: _Line

    @model_validator(mode="after")
    def inclusive_range(self) -> Self:
        if self.start_line > self.end_line:
            raise ValueError("Expected a nonempty inclusive source range")
        return self

    @classmethod
    def from_range(cls, location: SourceRange) -> Self:
        return cls(
            side=location.side,
            commit_id=location.commit_id,
            path=location.path,
            blob_id=location.blob_id,
            start_line=location.start_line,
            end_line=location.end_line,
        )


class SourceEvidence(_Record):
    kind: Literal["source"]
    id: _ID
    range: SourceLocation
    role: SourceRole
    text: Annotated[str, Field(min_length=1, max_length=4096)]

    @model_validator(mode="after")
    def range_matches_role_and_text(self) -> Self:
        if self.role in (
            "base_declaration",
            "head_declaration",
        ) and not self.role.startswith(self.range.side):
            raise ValueError("Declaration role and revision side disagree")
        if (
            self.text.count("\n") + int(not self.text.endswith("\n"))
            != self.range.end_line - self.range.start_line + 1
        ):
            raise ValueError("Excerpt does not match its line range")
        return self


class DiagnosticEvidence(_PinnedIdentity):
    kind: Literal["diagnostic"]
    id: _ID
    tool: Literal["cpython_compile"]
    tool_version: Annotated[str, Field(pattern=r"^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$")]
    result: Literal["passed", "syntax_error"]
    message: _Message
    location: SourceLocation | None

    @model_validator(mode="after")
    def matching_location(self) -> Self:
        location = self.location
        if location is not None and (
            self.result != "syntax_error" or _identity(location) != _identity(self)
        ):
            raise ValueError("Diagnostic and location disagree")
        return self


def _identity(source: _PinnedIdentity) -> tuple[str, str, str, str]:
    return source.side, source.commit_id, source.path, source.blob_id


Evidence = Annotated[SourceEvidence | DiagnosticEvidence, Field(discriminator="kind")]


class CompileFinding(_Record):
    id: _ID
    severity: Literal["medium"]
    confidence: Literal["confirmed"]
    decision_reason: Literal["reproduced_regression"]
    trigger: _Message
    consequence: _Message
    evidence_ids: _EvidenceIDs
    anchor: SourceLocation | None


class _LocalResult(_Record):
    schema_version: _Version2
    policy_version: _Version
    comparison: PinnedComparison
    scope: Literal["local_checks"]
    discussion_status: Literal["not_requested"]
    usage: InventoryUsage
    private_leads: _Empty
    linked_existing_threads: _Empty


class _CheckedLocal(_LocalResult):
    error: None
    recommendation: Literal["none", "comment"]
    file_coverage: Annotated[
        tuple[LocalFileCoverage, ...], Field(max_length=MAX_CHANGES)
    ]
    investigation_coverage: Annotated[
        tuple[Investigation, ...], Field(max_length=MAX_CHANGES)
    ]
    evidence_index: Annotated[tuple[Evidence, ...], Field(max_length=MAX_EVIDENCE)]
    findings: Annotated[tuple[CompileFinding, ...], Field(max_length=MAX_FINDINGS)]

    @model_validator(mode="after")
    def references_resolve(self) -> Self:
        if (self.recommendation == "comment") != bool(self.findings):
            raise ValueError("Recommendation and findings disagree")
        if len({f.id for f in self.findings}) != len(self.findings):
            raise ValueError("Duplicate finding ID")
        evidence = {entry.id: entry for entry in self.evidence_index}
        investigations = {work.id: work for work in self.investigation_coverage}
        if len(evidence) != len(self.evidence_index) or len(investigations) != len(
            self.investigation_coverage
        ):
            raise ValueError("Duplicate evidence or investigation ID")
        base_paths: dict[str, FileSide] = {}
        head_paths: dict[str, FileSide] = {}
        work_by_head: dict[str, tuple[Investigation, LocalFileCoverage]] = {}
        used: set[str] = set()
        for row in self.file_coverage:
            for side, paths in ((row.old, base_paths), (row.new, head_paths)):
                if side:
                    if side.path in paths or len(side.object_id) != len(
                        self.comparison.head
                    ):
                        raise ValueError("Duplicate path or wrong blob hash width")
                    paths[side.path] = side
            for work_id in row.required_investigation_ids:
                work = investigations.get(work_id)
                path = row.new or row.old
                assert path is not None
                if (
                    work is None
                    or work_id in used
                    or (work.path, work.outcome, work.reason)
                    != (path.path, row.outcome, row.reason)
                ):
                    raise ValueError("Investigation and file coverage disagree")
                used.add(work_id)
                self._check_diagnostics(work, row, evidence)
                if row.new is not None:
                    work_by_head[row.new.path] = work, row
        if used != investigations.keys():
            raise ValueError("Unreferenced investigation")
        for entry in self.evidence_index:
            identity = entry.range if isinstance(entry, SourceEvidence) else entry
            commit, paths = (
                (self.comparison.merge_base, base_paths)
                if identity.side == "base"
                else (self.comparison.head, head_paths)
            )
            if identity.commit_id != commit:
                raise ValueError("Evidence names the wrong pinned commit")
            side = paths.get(identity.path)
            if side is not None and side.object_id != identity.blob_id:
                raise ValueError("Evidence names the wrong changed blob")
        reported_paths: set[str] = set()
        for finding in self.findings:
            paths = self._check_finding(finding, evidence, work_by_head)
            if paths in reported_paths:
                raise ValueError("More than one finding per path")
            reported_paths.add(paths)
        return self

    @staticmethod
    def _check_diagnostics(
        work: Investigation, row: LocalFileCoverage, evidence: dict[str, Evidence]
    ) -> None:
        expected = {
            (name, side.path, side.object_id)
            for name, side in (("base", row.old), ("head", row.new))
            if side and supported_python(side)
        }
        actual: set[tuple[str, str, str]] = set()
        for key in work.evidence_ids:
            entry = evidence.get(key)
            if entry is None or isinstance(entry, SourceEvidence):
                raise ValueError("Investigations must cite diagnostics")
            actual.add((entry.side, entry.path, entry.blob_id))
        if (
            len(actual) != len(work.evidence_ids)
            or not actual <= expected
            or (work.outcome == "complete" and actual != expected)
        ):
            raise ValueError("Diagnostic coverage and investigation disagree")

    @staticmethod
    def _check_finding(
        finding: CompileFinding,
        evidence: dict[str, Evidence],
        work_by_head: dict[str, tuple[Investigation, LocalFileCoverage]],
    ) -> str:
        diagnostics: dict[RevisionSide, DiagnosticEvidence] = {}
        for key in finding.evidence_ids:
            entry = evidence.get(key)
            if not isinstance(entry, DiagnosticEvidence) or entry.side in diagnostics:
                raise ValueError(
                    "Finding must cite at most one compiler diagnostic per side"
                )
            diagnostics[entry.side] = entry
        head, base = diagnostics.get("head"), diagnostics.get("base")
        if (
            head is None
            or head.result != "syntax_error"
            or head.location is None
            or (finding.anchor is not None and finding.anchor != head.location)
        ):
            raise ValueError("Finding must cite a located head error")
        completed = work_by_head.get(head.path)
        if completed is None:
            raise ValueError("Finding must cite a changed file")
        work, row = completed
        if work.outcome != "complete" or set(finding.evidence_ids) != set(
            work.evidence_ids
        ):
            raise ValueError("Finding must cite a completed compiler investigation")
        if row.change_type != "added" and (
            base is None
            or base.result != "passed"
            or base.tool_version != head.tool_version
        ):
            raise ValueError("Finding must cite a passing base or true addition")
        return head.path


class CompleteLocalCheck(_CheckedLocal):
    status: Literal["complete"]
    stop_reason: Literal["finished"]

    @model_validator(mode="after")
    def no_unfinished_work(self) -> Self:
        if any(row.outcome == "unfinished" for row in self.file_coverage):
            raise ValueError("Complete run contains unfinished work")
        return self


class PartialLocalCheck(_CheckedLocal):
    status: Literal["partial"]
    stop_reason: Literal["finished", "work_limit"]

    @model_validator(mode="after")
    def unfinished_work(self) -> Self:
        if not any(row.outcome == "unfinished" for row in self.file_coverage):
            raise ValueError("Partial run has no unfinished work")
        if self.stop_reason == "work_limit" and not any(
            row.reason == "work_limit" for row in self.file_coverage
        ):
            raise ValueError("Work limit requires an unfinished work-limit gap")
        return self


class FailedLocalCheck(_LocalResult):
    status: Literal["failed"]
    stop_reason: Literal["fatal_error"]
    error: InventoryError
    recommendation: Literal["none"]
    file_coverage: _Empty
    investigation_coverage: _Empty
    evidence_index: _Empty
    findings: _Empty


class LocalCheckResult(
    RootModel[
        Annotated[
            CompleteLocalCheck | PartialLocalCheck | FailedLocalCheck,
            Field(discriminator="status"),
        ]
    ]
):
    model_config = ConfigDict(strict=True, frozen=True, revalidate_instances="always")
