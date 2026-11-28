import json
from dataclasses import replace
from pathlib import Path
from typing import override

import pytest

from loop_reviewer.reviewer.change_coverage import (
    ChangedLineExcerpt,
    PreparedCoverage,
    build_coverage_packet,
    prepare_coverage,
    required_ranges,
)
from loop_reviewer.reviewer.integrations.git_hunks import decode_hunks
from loop_reviewer.reviewer.integrations.git_inventory import open_local_comparison
from loop_reviewer.reviewer.integrations.python_analyzer import CpythonAnalyzer
from loop_reviewer.reviewer.inventory import RevisionSide, validated_inventory
from loop_reviewer.reviewer.inventory_contract import ChangedFile, PinnedComparison
from loop_reviewer.reviewer.local_check_contract import SourceEvidence, SourceLocation
from loop_reviewer.reviewer.local_evidence import (
    EvidenceIndex,
    LocalEvidenceWork,
    PinnedEvidenceWork,
)
from loop_reviewer.reviewer.model_budget import SpendLedger
from loop_reviewer.reviewer.model_contract import (
    CallQuote,
    ModelAttempt,
    ModelCall,
    TokenQuote,
    TokenUsage,
    parse_reply,
)
from loop_reviewer.reviewer.pinned_source import (
    Hunk,
    PinnedTextBlob,
    ReadFailure,
    SearchPaths,
)
from loop_reviewer.reviewer.semantic_batch import (
    COVERAGE_INVESTIGATION_PROMPT,
    run_coverage_batches,
    run_semantic_batch,
)
from loop_reviewer.reviewer.semantic_inputs import (
    Omission,
    ShownExcerpt,
    build_targeted_bundle,
    canonical_inventory,
)
from loop_reviewer.reviewer.semantic_limits import (
    VERIFY_EVIDENCE,
    VERIFY_PROMPT,
    MeteredSource,
    SemanticLimits,
)
from tests.support.inventory_git import PATH_ENV, commit, init
from tests.test_local_checks import PINS, Source, row

BASE = b"def items():\n    return None\n"
HEAD = BASE.replace(b"None", b"[]")
CALLER = b"from producer import items\n\ndef first():\n    value = items()\n    if value is None:\n        return 'missing'\n    return value[0]\n"


class WitnessSource(Source):
    def __init__(self, mode: str = "regression") -> None:
        self.producer = "sys" if mode == "shadowed" else "producer"
        change = row(f"{self.producer}.py")
        assert change.old and change.new
        self.base = BASE if mode != "preexisting" else HEAD.replace(b"[]", b"()")
        super().__init__(
            (
                ChangedFile.model_validate(
                    change.model_dump()
                    | {
                        "old": change.old.model_copy(
                            update={"blob_bytes": len(self.base)}
                        ),
                        "new": change.new.model_copy(update={"blob_bytes": len(HEAD)}),
                    }
                ),
            )
        )
        self.mode = mode

    @override
    def read_text_blob(
        self, side: RevisionSide, path: str, *, max_bytes: int, timeout_seconds: float
    ) -> PinnedTextBlob | ReadFailure:
        if path == f"{self.producer}.py":
            text = self.base if side == "base" else HEAD
        elif path == "consumer.py":
            text = CALLER.replace(b"producer", self.producer.encode())
            if side == "head" and self.mode == "guarded":
                text = CALLER.replace(b"value is None", b"not value")
            if self.mode == "ambiguous":
                text = CALLER.replace(b"first()", b"first(items)")
        else:
            return "not_found"
        if len(text) > max_bytes:
            return "limit_reached"
        return PinnedTextBlob(
            side,
            PINS.merge_base if side == "base" else PINS.head,
            path,
            ("c" if side == "base" else "d") * 40,
            text,
        )

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
        return SearchPaths(
            ("consumer.py",), self.mode == "truncated", retrieved_bytes=52
        )

    @override
    def hunks(
        self, change: ChangedFile, *, max_bytes: int, timeout_seconds: float
    ) -> tuple[Hunk, ...]:
        return decode_hunks(
            b"@@ -1,2 +1,2 @@\n def items():\n-"
            + self.base.splitlines()[1]
            + b"\n+    return []\n"
        )


class ScriptedModel:
    def __init__(
        self,
        *,
        assessment: str = "supports",
        kind: str = "candidate",
        second: bool = False,
        anchor: str = "changed",
        producer: str = "producer",
    ) -> None:
        self.assessment = assessment
        self.kind = kind
        self.second = second
        self.anchor = anchor
        self.producer = producer
        self.calls: list[ModelCall] = []

    def quote(self, call: ModelCall) -> CallQuote:
        return CallQuote(
            call, call.prompt.encode(), TokenQuote("fixture", 100, 10, 50000, 400000)
        )

    def send(
        self, call: ModelCall, quote: CallQuote, *, timeout_seconds: float
    ) -> ModelAttempt:
        self.calls.append(call)
        if self.kind == "retry" and len(self.calls) == 1:
            return ModelAttempt("truncated", output_bytes=4)
        if call.kind == "investigation":
            payload = json.loads(call.prompt.split("\n\n", 1)[1])
            base = next(
                e["id"] for e in payload["excerpts"] if e["role"] == "base_declaration"
            )
            head = next(
                e["id"] for e in payload["excerpts"] if e["role"] == "head_declaration"
            )
            anchor = next(
                e["id"]
                for e in payload["excerpts"]
                if e["role"] == "check_location" and e["range"]["side"] == "head"
            )
            candidate = {
                "file_id": "p00001",
                "changed_behavior": f"{self.producer}.items() returns [] instead of None",
                "trigger": "Call consumer.first()",
                "consequence": "consumer.first() raises IndexError",
                "proposed_impact": "unknown"
                if self.kind == "unknown"
                else "material_incorrect_behavior",
                "citations": [
                    {
                        "role": "head_change",
                        "evidence_id": "e99999" if self.kind == "forged" else head,
                    },
                    {"role": "base_behavior", "evidence_id": base},
                ],
                "proposed_anchor_id": anchor if self.anchor == "changed" else head,
            }
            reviewed: list[str] = [] if self.kind == "empty" else ["p00001"]
            candidates = [candidate] * (2 if self.second else 1)
            if self.kind == "distinct":
                candidates.append(
                    candidate
                    | {
                        "trigger": "Call other.first()",
                        "consequence": "other.first() raises IndexError",
                    }
                )
            if self.kind in ("empty", "reviewed"):
                candidates.clear()
            reply: dict[str, object] = {
                "schema_version": 1,
                "investigation_id": call.unit_id,
                "kind": "submit_candidates",
                "reviewed_file_ids": reviewed,
                "candidates": candidates,
            }
        else:
            evidence = json.loads(call.prompt.rsplit("\n", 1)[1])
            reply = {
                "schema_version": 1,
                "candidate_id": call.unit_id,
                "assessment": self.assessment,
                "evidence_ids": [e["id"] for e in evidence],
                "explanation": "Fixture assessment",
            }
        raw = json.dumps(reply)
        return ModelAttempt(
            "success", TokenUsage(100, 10, 110), parse_reply(call, raw), len(raw)
        )


@pytest.mark.parametrize(
    ("mode", "assessment", "kind", "wanted"),
    [
        ("regression", "supports", "candidate", "finding"),
        ("regression", "supports", "unknown", "lead"),
        ("guarded", "contradicts", "candidate", "contradicted"),
        ("guarded", "supports", "candidate", "contradicted"),
        ("regression", "contradicts", "candidate", "lead"),
        ("ambiguous", "supports", "candidate", "lead"),
        ("shadowed", "supports", "candidate", "lead"),
        ("preexisting", "contradicts", "candidate", "preexisting"),
        ("regression", "supports", "retry", "finding"),
        ("regression", "supports", "forged", "invalid_output"),
        ("regression", "supports", "empty", "not_scheduled"),
        ("truncated", "supports", "empty", "work_limit"),
        ("regression", "supports", "reviewed", "reviewed"),
    ],
)
def test_batch_disposition_requires_a_pinned_witness(
    mode: str, assessment: str, kind: str, wanted: str
) -> None:
    source = WitnessSource(mode)
    model = ScriptedModel(assessment=assessment, kind=kind, producer=source.producer)
    index = EvidenceIndex()
    work = LocalEvidenceWork(source, CpythonAnalyzer(), PINS, lambda: 0, 120)
    bundle = build_targeted_bundle(PINS, source.rows, work, index, 8192)
    ledger = SpendLedger(1000, lambda: 0)
    outcome = run_semantic_batch(
        bundle,
        source,
        model,
        ledger,
        SemanticLimits(120),
        lambda: 0,
        analyzer=CpythonAnalyzer(),
        evidence=index,
    )
    if wanted == "finding":
        (finding,) = outcome.findings
        assert finding.anchor is not None and finding.anchor.start_line == 2
        assert "consumer.py" in {e.range.path for e in outcome.evidence}
        assert "p00001" not in outcome.gaps
    elif wanted == "lead":
        assert len(outcome.leads) == 1 and not outcome.findings
        assert outcome.gaps == {"p00001": "missing_evidence"}
    elif wanted in ("contradicted", "preexisting"):
        assert [d.reason for d in outcome.discards] == [wanted]
        assert not outcome.findings and not outcome.gaps
    elif wanted == "reviewed":
        assert (
            outcome.reviewed_ids == ("p00001",)
            and not outcome.gaps
            and ledger.snapshot().model_calls == 1
        )
    else:
        assert not outcome.findings and outcome.gaps == {"p00001": wanted}


def test_completed_finding_survives_exhausted_followup_and_non_inline_anchor() -> None:
    source = WitnessSource()
    model = ScriptedModel(second=True, anchor="context")
    index = EvidenceIndex()
    work = LocalEvidenceWork(source, CpythonAnalyzer(), PINS, lambda: 0, 120)
    bundle = build_targeted_bundle(PINS, source.rows, work, index, 8192)
    ledger = SpendLedger(27, lambda: 0)
    limits = SemanticLimits(120, evidence_limit=bundle.evidence_bytes + 2048)
    result = run_semantic_batch(
        bundle,
        source,
        model,
        ledger,
        limits,
        lambda: 0,
        analyzer=CpythonAnalyzer(),
        evidence=index,
    )
    assert len(result.findings) == 1 and result.findings[0].anchor is None
    assert result.stop == "work_limit" and result.gaps == {"p00001": "work_limit"}


@pytest.mark.parametrize(
    "distinct", [False, True], ids=["same-witness", "same-line-different-consumer"]
)
def test_batch_deduplicates_only_proven_equivalence(distinct: bool) -> None:
    class TwoConsumers(WitnessSource):
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
            self,
            side: RevisionSide,
            path: str,
            *,
            max_bytes: int,
            timeout_seconds: float,
        ) -> PinnedTextBlob | ReadFailure:
            blob = super().read_text_blob(
                side,
                "consumer.py" if path == "other.py" else path,
                max_bytes=max_bytes,
                timeout_seconds=timeout_seconds,
            )
            return blob if isinstance(blob, str) else replace(blob, path=path)

    source = TwoConsumers()
    model = ScriptedModel(
        kind="distinct" if distinct else "candidate", second=not distinct
    )
    index = EvidenceIndex()
    work = LocalEvidenceWork(source, CpythonAnalyzer(), PINS, lambda: 0, 120)
    bundle = build_targeted_bundle(PINS, source.rows, work, index, 8192)
    result = run_semantic_batch(
        bundle,
        source,
        model,
        SpendLedger(1000, lambda: 0),
        SemanticLimits(120),
        lambda: 0,
        analyzer=CpythonAnalyzer(),
        evidence=index,
    )
    assert [f.id for f in result.findings] == (
        ["c00001", "c00002"] if distinct else ["c00001"]
    )
    assert len({f.anchor for f in result.findings}) == 1
    assert [decision.reason for decision in result.discards] == (
        [] if distinct else ["duplicate"]
    )


class CoverageModel(ScriptedModel):
    def __init__(
        self, *, omit_first: bool = False, anchor_in: str | None = None
    ) -> None:
        super().__init__()
        self.omit_first = omit_first
        self.anchor_in = anchor_in

    @override
    def send(
        self, call: ModelCall, quote: CallQuote, *, timeout_seconds: float
    ) -> ModelAttempt:
        self.calls.append(call)
        payload = json.loads(call.prompt.split("\n\n", 1)[1])
        candidates = []
        if self.anchor_in is not None:
            excerpt = payload[self.anchor_in][0]
            candidates.append(
                {
                    "file_id": excerpt["file_ids"][0]
                    if "file_ids" in excerpt
                    else excerpt["file_id"],
                    "changed_behavior": "An entry is added.",
                    "trigger": "Read the entries.",
                    "consequence": "The entry appears twice.",
                    "proposed_impact": "unknown",
                    "citations": (),
                    "proposed_anchor_id": excerpt["id"],
                }
            )
        raw = json.dumps(
            {
                "schema_version": 1,
                "investigation_id": call.unit_id,
                "kind": "submit_candidates",
                "reviewed_file_ids": []
                if self.omit_first and len(self.calls) == 1
                else payload["selected_file_ids"],
                "candidates": candidates,
            }
        )
        return ModelAttempt(
            "success", TokenUsage(100, 10, 110), parse_reply(call, raw), len(raw)
        )

    def shown_lines(self) -> list[list[tuple[str, int]]]:
        return [
            [
                (item["file_id"], item["range"]["start_line"])
                for item in json.loads(call.prompt.split("\n\n", 1)[1])[
                    "changed_excerpts"
                ]
            ]
            for call in self.calls
        ]


def _prepared_changes(*patches: bytes, widths: tuple[int, ...]) -> PreparedCoverage:
    inventory = canonical_inventory(
        tuple(row(f"file-{n}.go", added=True) for n in range(len(patches)))
    )
    required = tuple(
        item
        for file, patch in zip(inventory, patches, strict=True)
        for item in required_ranges(file, PINS, decode_hunks(patch))
    )
    changed = tuple(
        ChangedLineExcerpt(
            item.file_id,
            SourceEvidence(
                kind="source",
                id=f"e{n:05d}",
                role="check_location",
                range=SourceLocation.from_range(item.source),
                text="x" * width + "\n",
            ),
        )
        for n, (item, width) in enumerate(
            zip(
                (line for span in required for line in span.lines()),
                widths,
                strict=True,
            ),
            1,
        )
    )
    return PreparedCoverage(PINS, inventory, required, changed)


def test_fifth_separated_hunk_is_sent_in_a_later_packet() -> None:
    prepared = _prepared_changes(
        b"".join(f"@@ -0,0 +{line} @@\n+x\n".encode() for line in (1, 10, 20, 30, 40)),
        widths=(10,) * 5,
    )
    model = CoverageModel()
    result = run_coverage_batches(
        prepared,
        model,
        SpendLedger(1000, lambda: 0, multiple_investigations=True),
        SemanticLimits(120),
        lambda: 0,
        packet_bytes=1600,
    )
    assert model.shown_lines() == [
        [("p00001", line) for line in (1, 10, 20, 30)],
        [("p00001", 40)],
    ]
    assert result.reviewed_ids == ("p00001",)
    assert not result.gaps and result.stop is None
    assert result.offered == result.acknowledged


@pytest.mark.parametrize(
    "omit_first", [False, True], ids=["all-acknowledged", "first-slice-unacknowledged"]
)
def test_one_contiguous_hunk_can_span_multiple_packets(omit_first: bool) -> None:
    # An added line can be sent alone, but the two additions cannot fit together.
    prepared = _prepared_changes(b"@@ -0,0 +1,2 @@\n+x\n+x\n", widths=(300, 300))
    model = CoverageModel(omit_first=omit_first)
    result = run_coverage_batches(
        prepared,
        model,
        SpendLedger(1000, lambda: 0, multiple_investigations=True),
        SemanticLimits(120),
        lambda: 0,
        packet_bytes=1150,
    )
    assert model.shown_lines() == [[("p00001", 1)], [("p00001", 2)]]
    assert result.reviewed_ids == (() if omit_first else ("p00001",))
    assert result.gaps == ({"p00001": "not_scheduled"} if omit_first else {})
    assert result.stop is None
    assert {item.source.start_line for item in result.acknowledged} == (
        {2} if omit_first else {1, 2}
    )


@pytest.mark.parametrize("first", ["oversized", "unacknowledged", "budget", "no-edits"])
def test_unfinished_work_does_not_erase_prior_acknowledgments(first: str) -> None:
    prepared = _prepared_changes(
        b"" if first == "no-edits" else b"@@ -0,0 +1 @@\n+x\n",
        b"@@ -0,0 +1 @@\n+y\n",
        widths={"oversized": (1200, 300), "no-edits": (300,)}.get(first, (300, 300)),
    )
    model = CoverageModel(omit_first=first == "unacknowledged")
    ledger = SpendLedger(
        27 if first == "budget" else 1000, lambda: 0, multiple_investigations=True
    )
    result = run_coverage_batches(
        prepared, model, ledger, SemanticLimits(120), lambda: 0, packet_bytes=1150
    )
    wanted = {
        "oversized": (
            [[("p00002", 1)]],
            "p00002",
            "p00001",
            "work_limit",
            "work_limit",
        ),
        "unacknowledged": (
            [[("p00001", 1)], [("p00002", 1)]],
            "p00002",
            "p00001",
            "not_scheduled",
            None,
        ),
        "budget": (
            [[("p00001", 1)]],
            "p00001",
            "p00002",
            "budget_exhausted",
            "budget_exhausted",
        ),
        "no-edits": ([[("p00002", 1)]], "p00002", "p00001", "missing_evidence", None),
    }
    shown, reviewed, incomplete, gap, stop = wanted[first]
    assert model.shown_lines() == shown
    assert result.reviewed_ids == (reviewed,)
    assert result.gaps == {incomplete: gap}
    assert result.stop == stop
    assert ledger.snapshot().model_calls == len(shown)
    assert {item.file_id for item in result.acknowledged} == {reviewed}
    assert {item.file_id for item in result.offered} == {
        file_id for packet in shown for file_id, _ in packet
    }


class WrappedCoverageModel(CoverageModel):
    @override
    def quote(self, call: ModelCall) -> CallQuote:
        return replace(
            super().quote(call),
            request_body=json.dumps(
                {
                    "messages": [{"role": "user", "content": call.prompt}],
                    "schema": {"type": "object"},
                }
            ).encode(),
        )


@pytest.mark.parametrize(
    "boundary", ["request", "future-evidence", "future-request", "spend", "slack"]
)
def test_optional_context_does_not_consume_required_packet_allowance(
    boundary: str,
) -> None:
    prepared = _prepared_changes(b"@@ -0,0 +1,2 @@\n+x\n+x\n", widths=(350, 350))
    (first, second) = prepared.changed
    optional = first.source.model_copy(
        update={
            "id": "e00003",
            "text": '\\"\\\\' * 20 + "\n",
        }
    )
    prepared = replace(prepared, supporting=(ShownExcerpt(optional, (first.file_id,)),))
    packet_limit = 1550

    class MeteredCoverageModel(WrappedCoverageModel):
        @override
        def send(
            self, call: ModelCall, quote: CallQuote, *, timeout_seconds: float
        ) -> ModelAttempt:
            response = super().send(call, quote, timeout_seconds=timeout_seconds)
            if boundary != "spend":
                return response
            context = json.loads(call.prompt.split("\n\n", 1)[1])["supporting_excerpts"]
            return replace(
                response,
                usage=TokenUsage(21, 1, 22) if context else TokenUsage(1, 1, 2),
            )

    model = MeteredCoverageModel()
    limits = SemanticLimits(120)
    lines = frozenset((first.coverage, second.coverage))
    one = build_coverage_packet(prepared, lines, packet_limit, supporting_bytes=0)
    two = build_coverage_packet(
        prepared, lines - one.offered, packet_limit, supporting_bytes=0
    )
    assert one.offered == {first.coverage} and two.offered == {second.coverage}
    enriched = build_coverage_packet(prepared, lines, packet_limit)
    assert optional.id in {item.source.id for item in enriched.bundle.excerpts}

    def request_size(unit: str, evidence_text: str) -> int:
        prompt = (
            COVERAGE_INVESTIGATION_PROMPT
            + f"Investigation ID: {unit}.\n\n"
            + evidence_text
        )
        return len(
            model.quote(
                ModelCall("investigation", unit, prompt, output_bytes=8192)
            ).request_body
        )

    required_requests = request_size("i00001", one.bundle.evidence_text) + request_size(
        "i00002", two.bundle.evidence_text
    )
    if boundary == "future-evidence":
        limits.evidence_limit = (
            VERIFY_EVIDENCE + one.bundle.evidence_bytes + two.bundle.evidence_bytes
        )
    elif boundary == "future-request":
        limits.prompt_limit = VERIFY_PROMPT + required_requests
    elif boundary == "request":
        # Leave room for the next required request and some context, but not for the escaped record.
        extra = enriched.bundle.evidence_bytes - one.bundle.evidence_bytes
        limits.prompt_limit = VERIFY_PROMPT + required_requests + extra
        assert (
            request_size("i00001", enriched.bundle.evidence_text)
            - request_size("i00001", one.bundle.evidence_text)
            > extra
        )

    tokens = model.quote(ModelCall("investigation", "i00001", "test")).tokens
    budget = (
        3 * tokens.worst_case + tokens.charge(1, 1) if boundary == "spend" else 1000
    )
    result = run_coverage_batches(
        prepared,
        model,
        SpendLedger(budget, lambda: 0, multiple_investigations=True),
        limits,
        lambda: 0,
        packet_bytes=packet_limit,
    )
    assert result.acknowledged == lines and result.stop is None
    assert model.shown_lines() == [[(first.file_id, 1)], [(second.file_id, 2)]]
    payloads = [json.loads(call.prompt.split("\n\n", 1)[1]) for call in model.calls]
    assert [bool(payload["supporting_excerpts"]) for payload in payloads] == [
        boundary == "slack",
        boundary in {"slack", "spend"},
    ]
    if boundary not in {"slack", "spend"}:
        assert limits.prompt_bytes == required_requests


@pytest.mark.parametrize(
    "prepared_ranges", [False, True], ids=["capped-patch", "rejected-line"]
)
def test_coverage_reports_work_limit_when_preparation_prevents_dispatch(
    prepared_ranges: bool,
) -> None:
    prepared = _prepared_changes(b"@@ -0,0 +1 @@\n+x\n", widths=(1,))
    prepared = replace(
        prepared,
        changed=(),
        required=prepared.required if prepared_ranges else (),
        omissions=(Omission("p00001", "work_limit"),),
    )
    model = CoverageModel()
    result = run_coverage_batches(
        prepared,
        model,
        SpendLedger(1000, lambda: 0, multiple_investigations=True),
        SemanticLimits(120),
        lambda: 0,
    )
    assert not model.calls and not result.offered
    assert result.stop == "work_limit" and result.gaps == {"p00001": "work_limit"}


def test_git_packets_keep_renamed_paths_and_one_sided_edits_pinned(
    tmp_path: Path,
) -> None:
    init(tmp_path)
    unchanged = [f"stable long line {n}\n" for n in range(10)]
    old_path = tmp_path / "old.ts"
    old_path.write_text("".join(unchanged))
    (tmp_path / "retired.go").write_text("removed without newline")
    base = commit(tmp_path)
    new_path = tmp_path / "renamed.ts"
    old_path.rename(new_path)
    new_path.write_text("".join([*unchanged[:5], "inserted\n", *unchanged[5:]]))
    (tmp_path / "retired.go").unlink()
    (tmp_path / "script").write_text("added without newline")
    (tmp_path / "binary").write_bytes(b"\x00contents")
    (tmp_path / "symlink").symlink_to("script")
    (tmp_path / "z-big").write_bytes(b"x" * (65 * 1024))
    pins = PinnedComparison(merge_base=base, head=commit(tmp_path))
    new_path.write_text("dirty and not pinned\n")
    model = CoverageModel(anchor_in="supporting_excerpts")
    limits = SemanticLimits(120, preparing_investigation=True)

    with open_local_comparison(tmp_path, pins, PATH_ENV) as source:
        rows = validated_inventory(source, pins)
        work = PinnedEvidenceWork(
            MeteredSource(source, limits, lambda: 0), pins, lambda: 0, 120
        )
        prepared = prepare_coverage(rows, work, EvidenceIndex())
        result = run_coverage_batches(
            prepared,
            model,
            SpendLedger(1000, lambda: 0, multiple_investigations=True),
            limits,
            lambda: 0,
        )

    files = {file.path: file for file in prepared.inventory}
    renamed, deleted, added = (
        files[path] for path in ("renamed.ts", "retired.go", "script")
    )
    assert renamed.change.change_type == "renamed"
    assert result.reviewed_ids == (renamed.id, deleted.id, added.id)
    assert result.gaps == {
        files["symlink"].id: "unsupported_language",
        files["z-big"].id: "work_limit",
    }
    assert result.offered == result.acknowledged and len(result.acknowledged) == 3
    (call,) = model.calls
    payload = json.loads(call.prompt.split("\n\n", 1)[1])
    assert [
        (
            item["range"]["side"],
            item["range"]["commit_id"],
            item["range"]["path"],
            item["text"],
        )
        for item in payload["changed_excerpts"]
    ] == [
        ("head", pins.head, "renamed.ts", "inserted\n"),
        ("base", pins.merge_base, "retired.go", "removed without newline"),
        ("head", pins.head, "script", "added without newline"),
    ]
    context = payload["supporting_excerpts"]
    assert {item["file_ids"][0] for item in context} == {renamed.id}
    assert {
        (item["range"]["path"], item["range"]["commit_id"]) for item in context
    } == {("old.ts", pins.merge_base), ("renamed.ts", pins.head)}
    assert {item["text"] for item in context} == set(unchanged[2:8])
    ((candidate, origin),) = result.pending
    assert origin.selected_ids == result.reviewed_ids
    assert candidate.file_id == renamed.id and candidate.proposed_anchor_id is None


@pytest.mark.parametrize("mode", ["reserved", "available", "deadline"])
def test_coverage_batch_looks_up_text_only_with_retrieval_headroom(
    tmp_path: Path, mode: str
) -> None:
    init(tmp_path)
    edited = tmp_path / "edit.go"
    edited.write_text("func io() int { return 1 }\n")
    (tmp_path / "consumer").write_text("use(io())\n")
    base = commit(tmp_path)
    edited.write_text("func io() int { return 2 }\n")
    pins = PinnedComparison(merge_base=base, head=commit(tmp_path))
    model = CoverageModel()
    limits = SemanticLimits(2, preparing_investigation=True)
    elapsed = 0.0
    timeouts: list[float] = []

    def clock() -> float:
        return elapsed

    class SlowSearch(MeteredSource):
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
        ) -> SearchPaths | ReadFailure:
            nonlocal elapsed
            timeouts.append(timeout_seconds)
            elapsed += timeout_seconds
            return "limit_reached"

    with open_local_comparison(tmp_path, pins, PATH_ENV) as source:
        adapter = (SlowSearch if mode == "deadline" else MeteredSource)(
            source, limits, clock
        )
        work = PinnedEvidenceWork(adapter, pins, clock, limits.deadline)
        prepared = prepare_coverage(
            validated_inventory(source, pins), work, EvidenceIndex()
        )
        required_bytes = limits.retrieved_bytes
        room = 0 if mode == "reserved" else 4096
        limits.combined_limit = required_bytes + limits.prompt_limit + room
        result = run_coverage_batches(
            prepared,
            model,
            SpendLedger(1000, clock, multiple_investigations=True),
            limits,
            clock,
        )
    assert (
        result.reviewed_ids == ("p00001",) and not result.gaps and result.stop is None
    )
    (call,) = model.calls
    payload = json.loads(call.prompt.split("\n\n", 1)[1])
    assert [item["text"] for item in payload["changed_excerpts"]] == [
        "func io() int { return 1 }\n",
        "func io() int { return 2 }\n",
    ]
    matches = payload["supporting_excerpts"]
    if mode == "available":
        assert [(item["role"], item["text"]) for item in matches] == [
            ("text_match", "use(io())\n"),
            ("text_match", "use(io())\n"),
        ]
        assert required_bytes < limits.retrieved_bytes <= required_bytes + room
    else:
        assert matches == [] and limits.retrieved_bytes == required_bytes
    if mode == "deadline":
        assert timeouts == [1.0] and elapsed < limits.deadline
