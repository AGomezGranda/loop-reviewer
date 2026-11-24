from dataclasses import replace
from typing import override

import pytest

from loop_reviewer.reviewer.inventory import RevisionSide
from loop_reviewer.reviewer.local_check_contract import SourceEvidence
from loop_reviewer.reviewer.local_evidence import (
    EvidenceIndex,
    LocalEvidenceWork,
    collect_python_context,
    python_sides,
)
from loop_reviewer.reviewer.pinned_source import PinnedTextBlob
from loop_reviewer.reviewer.python_context import PythonAnalysis
from tests.test_local_checks import PINS, SharedCallerAnalyzer, SharedCallerSource


def test_context_selections_reuse_the_unchanged_caller_evidence_id() -> None:
    source = SharedCallerSource()
    work = LocalEvidenceWork(source, SharedCallerAnalyzer(), PINS, lambda: 0, 30)
    evidence = EvidenceIndex()
    selected = []
    for change in source.inventory():
        analyses: dict[RevisionSide, PythonAnalysis] = {}
        for side, file in python_sides(change):
            analysis = work.analysis(side, file.path, expected=file)
            assert not isinstance(analysis, str)
            analyses[side] = analysis
        selected.append(collect_python_context(change, analyses, work, evidence))
    (caller,) = [
        e
        for e in evidence.entries
        if isinstance(e, SourceEvidence) and e.role == "call_site"
    ]
    assert [selection.status for selection in selected] == ["complete", "complete"]
    assert set(selected[0].evidence_ids) & set(selected[1].evidence_ids) == {caller.id}
    assert len(evidence.entries) == 5


@pytest.mark.parametrize("identity", ["commit", "side", "path"])
def test_context_does_not_index_spoofed_identity(identity: str) -> None:
    class SpoofedSource(SharedCallerSource):
        @override
        def read_text_blob(
            self,
            side: RevisionSide,
            path: str,
            *,
            max_bytes: int,
            timeout_seconds: float,
        ) -> PinnedTextBlob:
            blob = super().read_text_blob(
                side, path, max_bytes=max_bytes, timeout_seconds=timeout_seconds
            )
            if path != "caller.py":
                return blob
            match identity:
                case "commit":
                    return replace(blob, commit_id=PINS.merge_base)
                case "side":
                    return replace(blob, side="base")
                case _:
                    return replace(blob, path="other.py")

    source = SpoofedSource()
    work = LocalEvidenceWork(source, SharedCallerAnalyzer(), PINS, lambda: 0, 30)
    evidence = EvidenceIndex()
    change = source.inventory()[0]
    analyses: dict[RevisionSide, PythonAnalysis] = {}
    for side, file in python_sides(change):
        analysis = work.analysis(side, file.path, expected=file)
        assert not isinstance(analysis, str)
        analyses[side] = analysis
    selected = collect_python_context(change, analyses, work, evidence)
    assert selected.status == "unavailable"
    assert len(selected.evidence_ids) == 2
    assert all(
        isinstance(e, SourceEvidence) and e.range.path != "caller.py"
        for e in evidence.entries
    )
