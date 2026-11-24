import json
from dataclasses import replace
from typing import override

import pytest

from loop_reviewer.reviewer.integrations.git_hunks import decode_hunks
from loop_reviewer.reviewer.inventory import RevisionSide
from loop_reviewer.reviewer.inventory_contract import ChangedFile
from loop_reviewer.reviewer.local_evidence import EvidenceIndex, LocalEvidenceWork
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
from loop_reviewer.reviewer.python_context import (
    Call,
    Declaration,
    ImportAlias,
    PythonAnalysis,
)
from loop_reviewer.reviewer.semantic_batch import run_semantic_batch
from loop_reviewer.reviewer.semantic_inputs import build_targeted_bundle
from loop_reviewer.reviewer.semantic_limits import SemanticLimits
from tests.test_local_checks import PINS, Analyzer, Source, row

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


class WitnessAnalyzer(Analyzer):
    def __init__(self) -> None:
        super().__init__(head_error=False)

    @override
    def analyze(
        self, blob: PinnedTextBlob, *, timeout_seconds: float
    ) -> PythonAnalysis:
        diagnostic = super().analyze(blob, timeout_seconds=timeout_seconds).diagnostic
        if blob.path == "consumer.py":
            return PythonAnalysis(
                diagnostic,
                (Declaration("first", "function", 3, 7),),
                (Call("items", 4, 4),),
                (ImportAlias("items", "producer.items"),),
            )
        return PythonAnalysis(diagnostic, (Declaration("items", "function", 1, 2),))


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
    work = LocalEvidenceWork(source, WitnessAnalyzer(), PINS, lambda: 0, 120)
    bundle = build_targeted_bundle(PINS, source.rows, work, index, 8192)
    ledger = SpendLedger(1000, lambda: 0)
    outcome = run_semantic_batch(
        bundle, source, model, ledger, SemanticLimits(120), lambda: 0, evidence=index
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
    work = LocalEvidenceWork(source, WitnessAnalyzer(), PINS, lambda: 0, 120)
    bundle = build_targeted_bundle(PINS, source.rows, work, index, 8192)
    ledger = SpendLedger(27, lambda: 0)
    limits = SemanticLimits(120, evidence_limit=bundle.evidence_bytes + 2048)
    result = run_semantic_batch(
        bundle, source, model, ledger, limits, lambda: 0, evidence=index
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
    work = LocalEvidenceWork(source, WitnessAnalyzer(), PINS, lambda: 0, 120)
    bundle = build_targeted_bundle(PINS, source.rows, work, index, 8192)
    result = run_semantic_batch(
        bundle,
        source,
        model,
        SpendLedger(1000, lambda: 0),
        SemanticLimits(120),
        lambda: 0,
        evidence=index,
    )
    assert [f.id for f in result.findings] == (
        ["c00001", "c00002"] if distinct else ["c00001"]
    )
    assert len({f.anchor for f in result.findings}) == 1
    assert [decision.reason for decision in result.discards] == (
        [] if distinct else ["duplicate"]
    )
