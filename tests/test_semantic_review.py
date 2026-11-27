import json
from dataclasses import replace
from pathlib import Path
from typing import override

import pytest

from loop_reviewer.review.hosted_report import project_report
from loop_reviewer.reviewer.full_review_contract import (
    FullReviewRequest,
    FullReviewResult,
)
from loop_reviewer.reviewer.integrations.git_inventory import open_local_comparison
from loop_reviewer.reviewer.integrations.python_analyzer import CpythonAnalyzer
from loop_reviewer.reviewer.inventory import RevisionSide
from loop_reviewer.reviewer.inventory_contract import (
    ChangedFile,
    InventoryBudget,
    PinnedComparison,
)
from loop_reviewer.reviewer.model_contract import (
    CallQuote,
    Citation,
    InvestigationReplyV1,
    ModelAttempt,
    ModelCall,
    TokenUsage,
    parse_reply,
)
from loop_reviewer.reviewer.pinned_source import (
    PinnedTextBlob,
    ReadFailure,
    SearchPaths,
)
from loop_reviewer.reviewer.python_context import PythonAnalysis
from loop_reviewer.reviewer.semantic_contract import (
    SemanticReviewRequest,
    SemanticReviewResult,
)
from loop_reviewer.reviewer.semantic_review import (
    review_full,
    review_semantic,
    review_semantic_with_discussion,
)
from tests.support.discussion import discussion_request, known_snapshot
from tests.support.inventory_git import PATH_ENV, commit, init, revision
from tests.test_local_checks import PINS, Analyzer, Source, row
from tests.test_semantic_batch import (
    CALLER,
    ScriptedModel,
    WitnessSource,
)


def request(amount: int) -> SemanticReviewRequest:
    return SemanticReviewRequest(
        schema_version=3,
        scope="full_review",
        comparison=PINS,
        discussion=None,
        budget=InventoryBudget(max_model_spend_usd_micros=amount),
    )


@pytest.mark.parametrize(
    ("kind", "status", "findings", "leads"),
    [
        ("candidate", "complete", 1, 0),
        ("unknown", "partial", 0, 1),
        ("reviewed", "complete", 0, 0),
        ("empty", "partial", 0, 0),
    ],
)
def test_full_review_reports_behavior_separately(
    kind: str, status: str, findings: int, leads: int
) -> None:
    source = WitnessSource()
    result = review_semantic(
        request(100),
        source.open,
        CpythonAnalyzer(),
        ScriptedModel(kind=kind),
        lambda: 0,
    )
    decoded = SemanticReviewResult.model_validate_json(result.model_dump_json()).root
    assert decoded.status == status
    assert len(decoded.findings) == findings and len(decoded.private_leads) == leads
    assert decoded.file_coverage[0].highest_completed_check == (
        "semantic" if status == "complete" else "mechanical"
    )
    assert decoded.recommendation == ("comment" if findings else "none")


def test_zero_spend_retains_all_rows_without_preparing_or_dispatching_calls() -> None:
    class ForbiddenModel(ScriptedModel):
        @override
        def quote(self, call: ModelCall) -> CallQuote:
            pytest.fail("zero spend must not quote")

    source = Source(
        tuple(row(f"code-{i:02d}.py") for i in range(98))
        + (row("asset", binary=True), row("README"))
    )
    result = review_semantic(
        request(0), source.open, Analyzer(head_error=False), ForbiddenModel(), lambda: 0
    ).root
    assert result.status == "partial" and result.stop_reason == "budget_exhausted"
    assert result.usage.model_calls == 0 and len(result.file_coverage) == 100
    assert {row.reason for row in result.file_coverage} == {
        "budget_exhausted",
        "unsupported_language",
        "binary",
    }


@pytest.mark.parametrize("amount", [0, 100], ids=["zero-spend", "no-provider"])
def test_v5_retains_regular_text_as_unfinished_without_a_model(amount: int) -> None:
    source = Source((row("README"), row("assets/brand", binary=True), row("main.py")))
    result = review_full(
        FullReviewRequest(
            schema_version=5,
            scope="full_review",
            comparison=PINS,
            budget=InventoryBudget(max_model_spend_usd_micros=amount),
        ),
        source.open,
        Analyzer(head_error=False),
        None,
        lambda: 0,
    )
    report = FullReviewResult.model_validate_json(result.model_dump_json()).root
    assert (report.schema_version, report.policy_version) == (5, 3)
    assert (report.discussion, report.discussion_status) == (None, "not_requested")
    assert report.discussion_references == report.linked_existing_threads == ()
    assert report.status == "partial" and report.recommendation == "none"
    assert report.stop_reason == ("finished" if amount else "budget_exhausted")
    assert report.usage.model_calls == 0
    reason = "unsupported_model" if amount else "budget_exhausted"
    assert [
        (r.outcome, r.reason, r.highest_completed_check) for r in report.file_coverage
    ] == [
        ("unfinished", reason, "inventory_only"),
        ("excluded", "binary", "inventory_only"),
        ("unfinished", reason, "mechanical"),
    ]
    assert [entry.kind for entry in report.investigation_coverage] == [
        "behavior",
        "python_compile",
        "behavior",
    ]


def test_v5_skips_compiler_work_for_nonregular_type_changes() -> None:
    class RegularSource(Source):
        @override
        def read_text_blob(
            self,
            side: RevisionSide,
            path: str,
            *,
            max_bytes: int,
            timeout_seconds: float,
        ) -> PinnedTextBlob | ReadFailure:
            assert path != "a.py"
            return super().read_text_blob(
                side, path, max_bytes=max_bytes, timeout_seconds=timeout_seconds
            )

    original = row("a.py")
    assert original.new is not None
    type_change = ChangedFile.model_validate(
        original.model_dump()
        | {
            "change_type": "type_changed",
            "new": original.new.model_dump() | {"mode": "120000"},
        }
    )
    source = RegularSource((type_change, row()))
    report = review_full(
        FullReviewRequest(
            schema_version=5,
            scope="full_review",
            comparison=PINS,
            budget=InventoryBudget(max_model_spend_usd_micros=0),
        ),
        source.open,
        Analyzer(),
        None,
        lambda: 0,
    ).root
    assert report.status == "partial"
    assert report.file_coverage[0].reason == "unsupported_language"
    assert len(report.findings) == 1


@pytest.mark.parametrize("count", [1, 18], ids=["one-bundle", "two-bundles"])
def test_v5_can_finish_non_python_text_with_no_compiler_work(count: int) -> None:
    class CleanModel(ScriptedModel):
        @override
        def send(
            self, call: ModelCall, quote: CallQuote, *, timeout_seconds: float
        ) -> ModelAttempt:
            assert call.kind == "investigation"
            self.calls.append(call)
            payload = json.loads(call.prompt.split("\n\n", 1)[1])
            selected = tuple(payload["selected_file_ids"])
            first = 1 + 16 * (len(self.calls) - 1)
            assert selected == tuple(
                f"p{i:05d}" for i in range(first, min(first + 16, count + 1))
            )
            assert len(selected) <= 16
            return ModelAttempt(
                "success",
                TokenUsage(1, 1, 2),
                reply=InvestigationReplyV1(
                    schema_version=1,
                    investigation_id=call.unit_id,
                    kind="submit_candidates",
                    reviewed_file_ids=selected,
                    candidates=(),
                ),
                output_bytes=100,
            )

    source = Source(tuple(row(f"config-{i:02d}.toml") for i in range(count)))
    model = CleanModel()
    result = review_full(
        FullReviewRequest(
            schema_version=5,
            scope="full_review",
            comparison=PINS,
            budget=InventoryBudget(max_model_spend_usd_micros=100),
        ),
        source.open,
        Analyzer(),
        model,
        lambda: 0,
    ).root
    assert result.status == "complete" and result.recommendation == "none"
    assert {entry.highest_completed_check for entry in result.file_coverage} == {
        "semantic"
    }
    assert [entry.kind for entry in result.investigation_coverage] == [
        "behavior"
    ] * count
    assert [call.unit_id for call in model.calls] == [
        f"i{i:05d}" for i in range(1, 2 + int(count > 16))
    ]
    assert result.usage.model_calls == len(model.calls)


@pytest.mark.parametrize(
    ("assessment", "citation_case", "disposition"),
    [
        pytest.param("supports", "complete", "finding", id="supported"),
        pytest.param("supports", "whitespace-path", "finding", id="whitespace-anchor"),
        pytest.param("supports", "python-index-error", "finding", id="generic-python"),
        pytest.param("supports", "search-limit", "finding", id="optional-search-limit"),
        pytest.param(
            "supports", "optional-overflow", "finding", id="trim-optional-excerpts"
        ),
        pytest.param("contradicts", "complete", "discard", id="counterevidence"),
        pytest.param("insufficient_evidence", "complete", "lead", id="not-verified"),
        pytest.param("supports", "missing-trigger", "lead", id="missing-trigger"),
        pytest.param("supports", "wrong-path", "lead", id="wrong-changed-path"),
        pytest.param("supports", "wrong-side", "lead", id="wrong-changed-side"),
        pytest.param(
            "supports", "budget-stop", "finding", id="verify-after-budget-stop"
        ),
        pytest.param(
            "supports", "partial-hunks", "finding", id="verify-with-unseen-hunks"
        ),
        pytest.param(
            "supports", "deleted", "finding", id="deleted-with-surviving-caller"
        ),
        pytest.param(
            "supports", "deleted-no-caller", "lead", id="deleted-without-head-evidence"
        ),
    ],
)
def test_v5_requires_independently_cited_generic_chain(
    tmp_path: Path, assessment: str, citation_case: str, disposition: str
) -> None:
    deleted = citation_case.startswith("deleted")
    python = citation_case == "python-index-error"
    search_limited = citation_case == "search-limit"
    optional_overflow = citation_case == "optional-overflow"
    path = "settings.py" if python else "settings.toml"
    whitespace_path = citation_case == "whitespace-path"
    if whitespace_path:
        path = "%20"
    changed_file = tmp_path / (" " if whitespace_path else path)
    caller = "consumer.js"
    init(tmp_path)
    extra_before, extra_after = "", ""
    if citation_case == "partial-hunks":
        extra_before = ("\n" * 10 + "# before\n") * 4
        extra_after = extra_before.replace("# before", "# after")
    other_files = (
        [tmp_path / f"zz-{i:02d}.txt" for i in range(17)]
        if citation_case == "budget-stop"
        else [tmp_path / f"zz-{i:02d}.toml" for i in range(19)]
        if search_limited
        else []
    )
    for i, file in enumerate(other_files):
        file.write_text(f"uniqueOption{i} = 1\n" if search_limited else "a\n")
    before = (
        "serviceEndpoint = (1, 2)[1]\n" if python else 'serviceEndpoint = "primary"\n'
    )
    if search_limited:
        before = before.replace("serviceEndpoint", "priorEndpoint")
    padding = "#" + "x" * 394 + "\n" if optional_overflow else ""
    changed_file.write_text(
        before.rstrip() + padding if padding else before + extra_before
    )
    reference = "sendRequest(serviceEndpoint);\n"
    if optional_overflow:
        reference += "//" + "x" * 334 + "\n"
        (tmp_path / "other.js").write_text(reference)
    (tmp_path / caller).write_text(reference)
    commit(tmp_path)
    base_pin = revision(tmp_path, "HEAD")
    if deleted:
        changed_file.unlink()
    else:
        after = (
            "serviceEndpoint = (1,)[1]\n" if python else 'serviceEndpoint = "retired"\n'
        )
        changed_file.write_text(
            after.rstrip() + padding if padding else after + extra_after
        )
    for i, file in enumerate(other_files):
        file.write_text(f"uniqueOption{i} = 2\n" if search_limited else "b\n")
    commit(tmp_path)
    pins = PinnedComparison(merge_base=base_pin, head=revision(tmp_path, "HEAD"))

    def open_source(comparison: PinnedComparison):
        return open_local_comparison(tmp_path, comparison, PATH_ENV)

    class Model(ScriptedModel):
        @override
        def send(
            self, call: ModelCall, quote: CallQuote, *, timeout_seconds: float
        ) -> ModelAttempt:
            self.calls.append(call)
            if call.kind == "investigation":
                payload = json.loads(call.prompt.split("\n\n", 1)[1])
                entries: dict[tuple[str, str], str] = {}
                for entry in payload["excerpts"]:
                    loc = entry["range"]
                    entries.setdefault((loc["side"], loc["path"]), entry["id"])
                if ("base", path) not in entries:
                    return ModelAttempt(
                        "success",
                        TokenUsage(1, 1, 2),
                        InvestigationReplyV1(
                            schema_version=1,
                            investigation_id=call.unit_id,
                            kind="submit_candidates",
                            reviewed_file_ids=tuple(payload["selected_file_ids"]),
                            candidates=(),
                        ),
                        output_bytes=100,
                    )
                base = entries["base", path]
                head = entries["head", caller if deleted else path]
                edit = base if deleted or citation_case == "wrong-side" else head
                if citation_case == "wrong-path":
                    edit = entries["head", caller]
                if citation_case == "deleted-no-caller":
                    head = base
                roles = [
                    ("base_behavior", base),
                    ("head_change", edit),
                    ("impact", head),
                    ("consequence", head),
                ]
                if citation_case != "missing-trigger":
                    roles.append(("trigger", head))
                response = {
                    "schema_version": 1,
                    "investigation_id": call.unit_id,
                    "kind": "submit_candidates",
                    "reviewed_file_ids": payload["selected_file_ids"],
                    "candidates": [
                        {
                            "file_id": "p00001",
                            "changed_behavior": "The server changes its endpoint.",
                            "trigger": "Start the server with the default configuration.",
                            "consequence": (
                                "Evaluating the default raises IndexError"
                                if python
                                else "Consumers receive the wrong endpoint."
                            ),
                            "proposed_impact": "material_incorrect_behavior",
                            "citations": [
                                {"role": role, "evidence_id": ident}
                                for role, ident in roles
                            ],
                            "proposed_anchor_id": head if whitespace_path else None,
                        }
                    ],
                }
            else:
                packet = json.loads(call.prompt.rsplit("\n", 1)[1])
                if optional_overflow:
                    assert all(e["range"]["path"] != "other.js" for e in packet)
                assert (
                    any(
                        e["range"]["path"] == caller and e["range"]["side"] == "head"
                        for e in packet
                    )
                    is not search_limited
                )
                response = {
                    "schema_version": 1,
                    "candidate_id": call.unit_id,
                    "assessment": assessment,
                    "evidence_ids": [entry["id"] for entry in packet],
                    "explanation": "Independent fixture assessment of the pinned configuration.",
                }
            raw = json.dumps(response)
            return ModelAttempt(
                "success", TokenUsage(1, 1, 2), parse_reply(call, raw), len(raw)
            )

    model = Model()
    report = review_full(
        FullReviewRequest(
            schema_version=5,
            scope="full_review",
            comparison=pins,
            budget=InventoryBudget(
                max_model_spend_usd_micros=27 if citation_case == "budget-stop" else 100
            ),
        ),
        open_source,
        Analyzer(head_error=False),
        model,
        lambda: 0,
    )
    result = report.root
    assert [call.unit_id for call in model.calls] == (
        ["i00001", "i00002", "c00001"]
        if search_limited
        else ["i00001", "c00001"]
        if citation_case
        in (
            "complete",
            "deleted",
            "budget-stop",
            "partial-hunks",
            "python-index-error",
            "optional-overflow",
            "whitespace-path",
        )
        else ["i00001"]
    )
    assert [finding.severity for finding in result.findings] == (
        ["medium"] if disposition == "finding" else []
    )
    if whitespace_path:
        (verified,) = result.findings
        assert verified.anchor is not None and verified.anchor.path == path
        (finding,) = project_report(report).findings
        assert finding.location is None and finding.report_id == verified.id
    assert len(result.private_leads) == int(disposition == "lead")
    partial = disposition == "lead" or citation_case in ("budget-stop", "partial-hunks")
    assert result.status == ("partial" if partial else "complete")
    if citation_case == "budget-stop":
        assert result.stop_reason == "budget_exhausted"
        assert result.file_coverage[-1].outcome == "unfinished"
    if citation_case == "partial-hunks":
        assert result.file_coverage[0].outcome == "unfinished"
    behavior = result.investigation_coverage[int(python)]
    assert behavior.kind == "behavior"
    assert [d.reason for d in behavior.discarded_candidates] == (
        ["contradicted"] if disposition == "discard" else []
    )


def test_v5_verifies_smaller_candidate_after_oversized_packet(tmp_path: Path) -> None:
    init(tmp_path)
    large = tmp_path / "large.toml"
    small = tmp_path / "small.toml"
    large.write_text(f'largeValue = "{"a" * 900}"\n')
    small.write_text("smallValue = 1\n")
    commit(tmp_path)
    base = revision(tmp_path, "HEAD")
    large.write_text(f'largeValue = "{"b" * 900}"\n')
    small.write_text("smallValue = 2\n")
    commit(tmp_path)
    pins = PinnedComparison(merge_base=base, head=revision(tmp_path, "HEAD"))

    class Model(ScriptedModel):
        @override
        def send(
            self, call: ModelCall, quote: CallQuote, *, timeout_seconds: float
        ) -> ModelAttempt:
            if call.kind == "verification":
                return super().send(call, quote, timeout_seconds=timeout_seconds)
            self.calls.append(call)
            payload = json.loads(call.prompt.split("\n\n", 1)[1])
            candidates = []
            for file in payload["files"]:
                key = file["id"]
                entries = {
                    item["range"]["side"]: item["id"]
                    for item in payload["excerpts"]
                    if item["range"]["path"] == file["new"]["path"]
                }
                candidates.append(
                    {
                        "file_id": key,
                        "changed_behavior": "The configuration value changes.",
                        "trigger": "Load the configuration with its default.",
                        "consequence": "The consumer receives a different value.",
                        "proposed_impact": "material_incorrect_behavior",
                        "citations": [
                            {"role": "base_behavior", "evidence_id": entries["base"]}
                        ]
                        + [
                            {"role": role, "evidence_id": entries["head"]}
                            for role in (
                                "head_change",
                                "trigger",
                                "consequence",
                                "impact",
                            )
                        ],
                        "proposed_anchor_id": None,
                    }
                )
            raw = json.dumps(
                {
                    "schema_version": 1,
                    "investigation_id": call.unit_id,
                    "kind": "submit_candidates",
                    "reviewed_file_ids": payload["selected_file_ids"],
                    "candidates": candidates,
                }
            )
            return ModelAttempt(
                "success", TokenUsage(1, 1, 2), parse_reply(call, raw), len(raw)
            )

    model = Model()
    report = review_full(
        FullReviewRequest(
            schema_version=5,
            scope="full_review",
            comparison=pins,
            budget=InventoryBudget(max_model_spend_usd_micros=100),
        ),
        lambda comparison: open_local_comparison(tmp_path, comparison, PATH_ENV),
        Analyzer(),
        model,
        lambda: 0,
    ).root
    assert [call.unit_id for call in model.calls] == ["i00001", "c00002"]
    assert report.status == "partial"
    assert [lead.file_id for lead in report.private_leads] == ["p00001"]
    assert [finding.file_id for finding in report.findings] == ["p00002"]


def test_v5_applies_python_counterevidence_to_a_qualified_consequence() -> None:
    class Model(ScriptedModel):
        @override
        def send(
            self, call: ModelCall, quote: CallQuote, *, timeout_seconds: float
        ) -> ModelAttempt:
            attempt = super().send(call, quote, timeout_seconds=timeout_seconds)
            if isinstance(attempt.reply, InvestigationReplyV1):
                (claim,) = attempt.reply.candidates
                head = next(
                    c.evidence_id for c in claim.citations if c.role == "head_change"
                )
                claim = claim.model_copy(
                    update={
                        "consequence": claim.consequence + " at startup",
                        "citations": (
                            *claim.citations,
                            Citation(role="trigger", evidence_id=head),
                            Citation(role="consequence", evidence_id=head),
                            Citation(role="impact", evidence_id=head),
                        ),
                    }
                )
                return replace(
                    attempt,
                    reply=attempt.reply.model_copy(update={"candidates": (claim,)}),
                )
            return attempt

    report = review_full(
        FullReviewRequest(
            schema_version=5,
            scope="full_review",
            comparison=PINS,
            budget=InventoryBudget(max_model_spend_usd_micros=100),
        ),
        WitnessSource("guarded").open,
        CpythonAnalyzer(),
        Model(),
        lambda: 0,
    ).root
    assert report.status == "complete" and not report.findings
    behavior = report.investigation_coverage[-1]
    assert behavior.kind == "behavior"
    assert [decision.reason for decision in behavior.discarded_candidates] == [
        "contradicted"
    ]


def test_v5_witness_cites_the_same_utf8_text_as_the_pinned_reader() -> None:
    class EncodingDeclarationSource(WitnessSource):
        @override
        def read_text_blob(
            self,
            side: RevisionSide,
            path: str,
            *,
            max_bytes: int,
            timeout_seconds: float,
        ) -> PinnedTextBlob | ReadFailure:
            blob = super().read_text_blob(
                side, path, max_bytes=max_bytes, timeout_seconds=timeout_seconds
            )
            if path == "consumer.py" and isinstance(blob, PinnedTextBlob):
                text = "# coding: latin-1\n# café\n".encode() + blob.text
                return replace(blob, text=text)
            return blob

    report = review_full(
        FullReviewRequest(
            schema_version=5,
            scope="full_review",
            comparison=PINS,
            budget=InventoryBudget(max_model_spend_usd_micros=100),
        ),
        EncodingDeclarationSource().open,
        CpythonAnalyzer(),
        ScriptedModel(),
        lambda: 0,
    ).root
    assert report.status == "complete"
    (finding,) = report.findings
    excerpts = [
        entry.text
        for entry in report.evidence_index
        if entry.kind == "source"
        and entry.range.path == "consumer.py"
        and entry.id in finding.evidence_ids
    ]
    assert excerpts == [
        "# coding: latin-1\n# café\n" + CALLER.decode(),
        "# coding: latin-1\n# café\n" + CALLER.decode(),
    ]


def test_v5_rejects_discussion_with_other_pins() -> None:
    snapshot = known_snapshot()
    with pytest.raises(ValueError, match="match the review pins"):
        FullReviewRequest(
            schema_version=5,
            scope="full_review",
            comparison=PINS.model_copy(update={"head": "f" * 40}),
            budget=InventoryBudget(max_model_spend_usd_micros=0),
            discussion=snapshot,
        )


def test_confirmed_compile_failure_can_settle_behavior_without_model() -> None:
    source = Source((row(),))
    result = review_semantic(request(0), source.open, Analyzer(), None, lambda: 0).root
    assert result.status == "complete" and result.usage.model_calls == 0
    assert result.findings[0].decision_reason == "reproduced_regression"
    assert result.file_coverage[0].highest_completed_check == "semantic"


@pytest.mark.parametrize(
    ("rows", "status", "stop", "paths", "evidence"),
    [
        ((row(),), "complete", "finished", ("code.py",), ("e00001", "e00002")),
        (
            (row(), row("z.py")),
            "partial",
            "budget_exhausted",
            ("code.py", "z.py"),
            ("e00001", "e00002", "e00003", "e00004"),
        ),
        ((row(), row()), "failed", "fatal_error", (), ()),
    ],
    ids=("complete-compiler", "budget-with-compiler", "invalid-inventory"),
)
def test_serialized_v3_report(
    rows: tuple[ChangedFile, ...],
    status: str,
    stop: str,
    paths: tuple[str, ...],
    evidence: tuple[str, ...],
) -> None:
    class SelectiveAnalyzer(Analyzer):
        @override
        def analyze(
            self, blob: PinnedTextBlob, *, timeout_seconds: float
        ) -> PythonAnalysis:
            return Analyzer(head_error=blob.path == "code.py").analyze(
                blob, timeout_seconds=timeout_seconds
            )

    result = review_semantic(
        request(0), Source(rows).open, SelectiveAnalyzer(), None, lambda: 0
    )
    payload = json.loads(result.model_dump_json())
    assert set(payload) == {
        "schema_version",
        "policy_version",
        "scope",
        "comparison",
        "discussion",
        "discussion_status",
        "usage",
        "linked_existing_threads",
        "status",
        "stop_reason",
        "error",
        "recommendation",
        "file_coverage",
        "investigation_coverage",
        "evidence_index",
        "findings",
        "private_leads",
    }
    assert (payload["schema_version"], payload["policy_version"]) == (3, 1)
    assert (payload["discussion_status"], payload["discussion"]) == (
        "not_requested",
        None,
    )
    assert (payload["status"], payload["stop_reason"]) == (status, stop)
    assert tuple(item["new"]["path"] for item in payload["file_coverage"]) == paths
    assert tuple(item["id"] for item in payload["evidence_index"]) == evidence
    assert [item["evidence_ids"] for item in payload["findings"]] == (
        [list(evidence[:2])] if paths else []
    )


class TwoCallers(WitnessSource):
    @override
    def search(
        self,
        side: RevisionSide,
        literal: str,
        *,
        max_paths: int,
        max_bytes: int,
        timeout_seconds: float,
        regular_text: bool = False,
    ) -> SearchPaths:
        return SearchPaths(("consumer.py", "other.py"), False, retrieved_bytes=101)

    @override
    def read_text_blob(
        self, side: RevisionSide, path: str, *, max_bytes: int, timeout_seconds: float
    ) -> PinnedTextBlob | ReadFailure:
        if path == "other.py":
            guarded = CALLER.replace(b"value is None", b"not value")
            text = guarded if side == "base" else CALLER
            return PinnedTextBlob(
                side,
                PINS.merge_base if side == "base" else PINS.head,
                path,
                ("e" if side == "base" else "f") * 40,
                text,
            )
        return super().read_text_blob(
            side, path, max_bytes=max_bytes, timeout_seconds=timeout_seconds
        )


@pytest.mark.parametrize(
    ("scenario", "linked", "kept", "leads"),
    [
        ("known", 1, 0, 0),
        ("resolved", 0, 1, 0),
        ("distinct", 1, 1, 0),
        ("unsupported", 0, 0, 1),
    ],
)
def test_private_discussion_links_only_verified_duplicates(
    scenario: str, linked: int, kept: int, leads: int
) -> None:
    source = TwoCallers("guarded" if scenario == "resolved" else "regression")
    snapshot = known_snapshot(resolved=scenario == "resolved", count=2)
    model = ScriptedModel(
        kind={
            "resolved": "distinct",
            "distinct": "distinct",
            "unsupported": "unknown",
        }.get(scenario, "candidate")
    )
    result = review_semantic_with_discussion(
        discussion_request(100, snapshot),
        source.open,
        CpythonAnalyzer(),
        model,
        lambda: 0,
    )
    report = result.root
    assert len(report.linked_existing_threads) == linked
    assert len(report.findings) == kept and len(report.private_leads) == leads
    assert report.status == ("partial" if leads else "complete")
    assert report.file_coverage[0].outcome == ("unfinished" if leads else "complete")
    assert report.recommendation == ("comment" if kept else "none")
    assert len(report.discussion_references) == 2
    if linked:
        (link,) = report.linked_existing_threads
        assert (link.thread_id, link.investigation_id, link.reason) == (
            "PRIVATE_THREAD_000",
            "b00001",
            "existing_thread",
        )
        assert (
            report.discussion_references[0].permalink
            == "https://github.com/octo/demo/pull/7#discussion_r1"
        )
        assert set(link.evidence_ids) <= {entry.id for entry in report.evidence_index}
    if kept:
        (finding,) = report.findings
        assert finding.trigger == "Call other.first()"
    assert {call.kind for call in model.calls} == {"investigation", "verification"}
    for token in (
        "PRIVATE_CANARY",
        "PRIVATE_THREAD",
        "PRIVATE_ROOT",
        "PRIVATE_ITEM",
        "discussion_r",
        "ignore source",
    ):
        assert all(token not in call.prompt for call in model.calls)
    assert (
        "PRIVATE_CANARY" not in result.model_dump_json()
        and "PRIVATE_ROOT" not in result.model_dump_json()
    )


def test_reply_remains_local_and_keeps_the_verified_finding() -> None:
    snapshot = known_snapshot()
    (thread,) = snapshot.threads
    root = thread.root
    reply = root.model_copy(
        update={
            "id": "ZZ_PRIVATE_REPLY",
            "parent_id": root.id,
            "body": "IGNORE_PREVIOUS_INSTRUCTIONS_CANARY",
        }
    )
    snapshot = snapshot.model_copy(
        update={"threads": (thread.model_copy(update={"comments": (root, reply)}),)}
    )
    source = WitnessSource()
    baseline, model = ScriptedModel(), ScriptedModel()
    review_semantic(request(100), source.open, CpythonAnalyzer(), baseline, lambda: 0)
    result = review_semantic_with_discussion(
        discussion_request(100, snapshot),
        source.open,
        CpythonAnalyzer(),
        model,
        lambda: 0,
    )
    assert model.calls == baseline.calls
    assert result.root.findings and result.root.recommendation == "comment"
    assert result.root.discussion_references and not result.root.linked_existing_threads
    assert "IGNORE_PREVIOUS_INSTRUCTIONS_CANARY" not in result.model_dump_json()
    assert "ZZ_PRIVATE_REPLY" not in result.model_dump_json()


@pytest.mark.parametrize("count", [1, 65], ids=("zero-spend", "mapping-limit"))
def test_private_mapping_neither_adds_calls_nor_drops_compiler_results(
    count: int,
) -> None:
    source = Source((row(),))
    report = review_semantic_with_discussion(
        discussion_request(
            0, known_snapshot(count=count, path="code.py", quote="pass")
        ),
        source.open,
        Analyzer(),
        None,
        lambda: 0,
    ).root
    assert report.status == ("complete" if count == 1 else "partial")
    assert report.stop_reason == ("finished" if count == 1 else "work_limit")
    assert (
        report.file_coverage[0].outcome == "complete" and report.usage.model_calls == 0
    )
    assert (
        report.findings[0].decision_reason == "reproduced_regression"
        and report.recommendation == "comment"
    )
    assert report.discussion_references and not report.linked_existing_threads
    assert bool(report.discussion.mapping_omissions) == (count > 1)
