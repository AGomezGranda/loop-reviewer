import json
from dataclasses import replace
from pathlib import Path
from typing import override

import pytest

from loop_reviewer.reviewer.change_coverage import (
    ChangedLineExcerpt,
    CoverageRange,
    PreparedCoverage,
    build_coverage_packet,
    required_ranges,
)
from loop_reviewer.reviewer.integrations.git_hunks import decode_hunks
from loop_reviewer.reviewer.integrations.git_inventory import open_local_comparison
from loop_reviewer.reviewer.integrations.python_analyzer import CpythonAnalyzer
from loop_reviewer.reviewer.inventory import validated_inventory
from loop_reviewer.reviewer.inventory_contract import PinnedComparison
from loop_reviewer.reviewer.local_check_contract import SourceEvidence, SourceLocation
from loop_reviewer.reviewer.local_evidence import EvidenceIndex, LocalEvidenceWork
from loop_reviewer.reviewer.pinned_source import PinnedTextBlob, SourceRange
from loop_reviewer.reviewer.python_context import PythonAnalysis
from loop_reviewer.reviewer.semantic_inputs import (
    BundleFile,
    FrozenHunk,
    ShownExcerpt,
    build_diff_bundle,
    build_full_bundle,
    build_targeted_bundle,
    canonical_inventory,
)
from tests.support.inventory_git import PATH_ENV, commit, init, revision
from tests.test_local_checks import PINS, SharedCallerAnalyzer, SharedCallerSource, row


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


def test_bundle_serializes_an_enclosing_caller_once(tmp_path: Path) -> None:
    init(tmp_path)
    producer = tmp_path / "producer.py"
    producer.write_text("def load():\n    return 1\n")
    (tmp_path / "consumer.py").write_text(
        "from producer import load\ndef run():\n    pass\n    pass\n    load()\n"
        + "    pass\n" * 8
        + "    load()\n"
        + "    pass\n" * 4
        + "    return 1\n"
    )
    commit(tmp_path)
    base = revision(tmp_path, "HEAD")
    producer.write_text("def load():\n    return 2\n")
    commit(tmp_path)
    pins = PinnedComparison(merge_base=base, head=revision(tmp_path, "HEAD"))

    with open_local_comparison(tmp_path, pins, PATH_ENV) as source:
        rows = validated_inventory(source, pins)
        work = LocalEvidenceWork(source, CpythonAnalyzer(), pins, lambda: 0, 30)
        bundle = build_full_bundle(pins, rows, work, EvidenceIndex(), 64 * 1024)
    excerpts = json.loads(bundle.evidence_text)["excerpts"]
    assert [item["id"] for item in excerpts] == [
        item.source.id for item in bundle.excerpts
    ]
    (caller,) = [item for item in excerpts if item["range"]["path"] == "consumer.py"]
    assert caller["text"].count("load()") == 2


def test_required_ranges_use_edit_records_and_each_rename_side() -> None:
    change = row("new.ts")
    assert change.old is not None
    rename = change.model_copy(
        update={
            "change_type": "renamed",
            "old": change.old.model_copy(update={"path": "old.ts"}),
        }
    )
    inventory = canonical_inventory((row("z.go"), rename))
    assert [(file.id, file.path) for file in inventory] == [
        ("p00001", "new.ts"),
        ("p00002", "z.go"),
    ]
    hunks = decode_hunks(
        b"@@ -3,5 +8,4 @@\n ctx\n-before\n-before again\n middle\n-after\n+replacement\n+last\n\\ No newline at end of file\n"
    )
    assert required_ranges(inventory[0], PINS, hunks) == (
        CoverageRange(
            "p00001", SourceRange("base", PINS.merge_base, "old.ts", "c" * 40, 4, 5)
        ),
        CoverageRange(
            "p00001", SourceRange("base", PINS.merge_base, "old.ts", "c" * 40, 7, 7)
        ),
        CoverageRange(
            "p00001", SourceRange("head", PINS.head, "new.ts", "d" * 40, 10, 11)
        ),
    )
    assert not required_ranges(inventory[1], PINS, ())


@pytest.mark.parametrize(
    "impostor", ["context", "wrong_side", "wrong_blob", "wrong_commit", "other_file"]
)
def test_prepared_packet_credits_only_exact_changed_line(impostor: str) -> None:
    file = BundleFile("p00001", row("code.go"))
    location = SourceRange("head", PINS.head, file.path, "d" * 40, 5, 5)
    required = CoverageRange(file.id, location)

    def excerpt(identifier: str, where: SourceRange) -> SourceEvidence:
        return SourceEvidence(
            kind="source",
            id=identifier,
            role="check_location",
            range=SourceLocation.from_range(where),
            text='run("é")\n',
        )

    wrong = {
        "context": replace(location, start_line=4, end_line=4),
        "wrong_side": replace(location, side="base"),
        "wrong_blob": replace(location, blob_id="e" * 40),
        "wrong_commit": replace(location, commit_id=PINS.merge_base),
        "other_file": location,
    }[impostor]
    supporting = (ShownExcerpt(excerpt("e00001", location), (file.id,)),)
    invalid = ChangedLineExcerpt(
        "p00002" if impostor == "other_file" else file.id, excerpt("e00002", wrong)
    )
    prepared = PreparedCoverage(PINS, (file,), (required,), (invalid,), supporting)
    empty = build_coverage_packet(prepared, frozenset({required}), 4096)
    assert not empty.offered and not empty.bundle.selected_ids

    prepared = replace(
        prepared,
        changed=(invalid, ChangedLineExcerpt(file.id, excerpt("e00003", location))),
    )
    packet = build_coverage_packet(prepared, frozenset({required}), 4096)
    assert packet.offered == frozenset({required})
    payload = json.loads(packet.bundle.evidence_text)
    assert payload["selected_file_ids"] == [file.id]
    assert [record["id"] for record in payload["changed_excerpts"]] == ["e00003"]
    assert [record["id"] for record in payload["supporting_excerpts"]] == ["e00001"]

    payload["supporting_excerpts"] = []
    required_bytes = len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    )
    tight = build_coverage_packet(prepared, frozenset({required}), required_bytes)
    assert tight.offered == frozenset({required})
    assert tight.bundle.evidence_bytes == required_bytes
    assert not json.loads(tight.bundle.evidence_text)["supporting_excerpts"]
