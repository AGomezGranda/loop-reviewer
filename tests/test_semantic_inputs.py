from dataclasses import replace
from typing import override

import pytest

from loop_reviewer.reviewer.local_evidence import EvidenceIndex, LocalEvidenceWork
from loop_reviewer.reviewer.pinned_source import PinnedTextBlob
from loop_reviewer.reviewer.python_context import PythonAnalysis
from loop_reviewer.reviewer.semantic_inputs import (
    FrozenHunk,
    build_diff_bundle,
    build_targeted_bundle,
)
from tests.test_local_checks import PINS, SharedCallerAnalyzer, SharedCallerSource


def test_targeted_bundle_shows_unchanged_caller_and_not_compiler_diagnostics() -> None:
    source = SharedCallerSource()
    work = LocalEvidenceWork(source, SharedCallerAnalyzer(), PINS, lambda: 0, 30)
    bundle = build_targeted_bundle(
        PINS, source.inventory(), work, EvidenceIndex(), 64 * 1024
    )
    assert [file.path for file in bundle.inventory] == ["a.py", "b.py"]
    assert bundle.selected_ids == ("p00001", "p00002")
    assert "caller.py" in bundle.evidence_text
    assert (
        "cpython_compile" not in bundle.evidence_text
        and "fixture" not in bundle.evidence_text
    )
    assert bundle.evidence_bytes == len(bundle.evidence_text.encode())
    assert {
        entry.source.range.path
        for entry in bundle.excerpts
        if entry.source.role == "call_site"
    } == {"caller.py"}


@pytest.mark.parametrize(
    "same_rank", [True, False], ids=("break-tie", "keep-evidence-rank")
)
def test_discussion_path_can_only_break_retrieval_rank_ties(same_rank: bool) -> None:
    class Analyzer(SharedCallerAnalyzer):
        @override
        def analyze(
            self, blob: PinnedTextBlob, *, timeout_seconds: float
        ) -> PythonAnalysis:
            result = super().analyze(blob, timeout_seconds=timeout_seconds)
            if not same_rank and blob.path == "caller.py":
                return replace(
                    result, calls=result.calls[:1], imports=result.imports[:1]
                )
            return result

    source = SharedCallerSource()
    rows = source.inventory()
    work = LocalEvidenceWork(source, Analyzer(), PINS, lambda: 0, 30)
    bundle = build_targeted_bundle(
        PINS,
        rows,
        work,
        EvidenceIndex(),
        64 * 1024,
        discussion_paths=frozenset({rows[0]}),
    )
    assert bundle.selected_ids == (
        ("p00002", "p00001") if same_rank else ("p00001", "p00002")
    )


def test_whole_excerpts_and_omissions_under_byte_limits() -> None:
    source = SharedCallerSource()
    bytes_by_limit = []
    for limit in (512, 2048, 65536):
        work = LocalEvidenceWork(source, SharedCallerAnalyzer(), PINS, lambda: 0, 30)
        bundle = build_targeted_bundle(
            PINS, source.inventory(), work, EvidenceIndex(), limit
        )
        assert bundle.evidence_bytes <= limit
        assert all(
            excerpt.source.text in bundle.evidence_text.replace(r"\n", "\n")
            for excerpt in bundle.excerpts
        )
        bytes_by_limit.append(bundle.evidence_bytes)
        if not bundle.selected_ids:
            assert {item.file_id for item in bundle.omissions} == {"p00001", "p00002"}
    assert bytes_by_limit[0] < bytes_by_limit[-1]


def test_diff_bundle_retains_frozen_bytes_and_selects_frozen_order() -> None:
    source = SharedCallerSource()
    rows = source.inventory()[::-1]
    hunks = []
    for index in (1, 0):
        patch = source.hunks(rows[index], max_bytes=16384, timeout_seconds=10)
        assert not isinstance(patch, str)
        hunks.append(FrozenHunk(index, 0, patch[0]))
    artifact = b'{"unchanged": false}\n'
    bundle = build_diff_bundle(PINS, rows, artifact, tuple(hunks), 8192)
    assert bundle.evidence_text.endswith(artifact.decode())
    assert "caller.py" not in bundle.evidence_text
    assert bundle.selected_ids == ("p00002", "p00001")
    assert all(entry.file_ids for entry in bundle.excerpts)
    with pytest.raises(ValueError, match="byte limit"):
        build_diff_bundle(PINS, rows, artifact, tuple(hunks), 1)
