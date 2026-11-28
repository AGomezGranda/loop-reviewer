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
from loop_reviewer.reviewer.coverage_contract import CoverageRange
from loop_reviewer.reviewer.integrations.git_hunks import decode_hunks
from loop_reviewer.reviewer.integrations.git_inventory import open_local_comparison
from loop_reviewer.reviewer.integrations.python_analyzer import CpythonAnalyzer
from loop_reviewer.reviewer.inventory import RevisionSide, validated_inventory
from loop_reviewer.reviewer.inventory_contract import ChangedFile, PinnedComparison
from loop_reviewer.reviewer.local_check_contract import SourceEvidence, SourceLocation
from loop_reviewer.reviewer.local_evidence import (
    EvidenceIndex,
    LocalEvidenceWork,
    OptionalRetrieval,
    PinnedEvidenceWork,
)
from loop_reviewer.reviewer.model_contract import Candidate, Citation
from loop_reviewer.reviewer.pinned_source import (
    Hunk,
    PinnedTextBlob,
    ReadFailure,
    SearchPaths,
    SourceRange,
)
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
from loop_reviewer.reviewer.semantic_policy import PrivateLead
from loop_reviewer.reviewer.source_verification import SourcePacket, build_source_packet
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


class CoverageSource:
    def __init__(
        self,
        path: str,
        base: bytes = b"before\nold\nafter\n",
        head: bytes = b"before\nnew\nafter\n",
    ) -> None:
        change = row(path)
        assert change.old and change.new
        self.change = change.model_copy(
            update={
                "old": change.old.model_copy(update={"blob_bytes": len(base)}),
                "new": change.new.model_copy(update={"blob_bytes": len(head)}),
            }
        )
        self.text = {"base": base, "head": head}
        self.reads: list[tuple[RevisionSide, str]] = []
        self.patch_reads = 0
        self.patch: tuple[Hunk, ...] | ReadFailure = decode_hunks(
            b"@@ -1,3 +1,3 @@\n before\n-ignored\n+ignored\n after\n"
        )

    def inventory(self) -> tuple[ChangedFile, ...]:
        return (self.change,)

    def read_text_blob(
        self, side: RevisionSide, path: str, *, max_bytes: int, timeout_seconds: float
    ) -> PinnedTextBlob | ReadFailure:
        self.reads.append((side, path))
        text = self.text[side]
        if len(text) > max_bytes:
            return "limit_reached"
        return PinnedTextBlob(
            side,
            PINS.merge_base if side == "base" else PINS.head,
            path,
            ("c" if side == "base" else "d") * 40,
            text,
        )

    def hunks(
        self, change: ChangedFile, *, max_bytes: int, timeout_seconds: float
    ) -> tuple[Hunk, ...] | ReadFailure:
        self.patch_reads += 1
        return self.patch

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
        pytest.fail("Packet preparation must not search without an enrichment budget")


def _all_lines(prepared: PreparedCoverage) -> frozenset[CoverageRange]:
    return frozenset(line for span in prepared.required for line in span.lines())


@pytest.mark.parametrize("path", ["code.py", "code.ts", "code.go", "code"])
def test_pinned_packet_is_language_agnostic(path: str) -> None:
    source = CoverageSource(path)
    work = PinnedEvidenceWork(source, PINS, lambda: 0, 30)
    prepared = prepare_coverage(source.inventory(), work, EvidenceIndex())
    packet = build_coverage_packet(prepared, _all_lines(prepared), 8192)

    assert packet.bundle.selected_ids == ("p00001",)
    assert packet.offered == _all_lines(prepared)
    changed, head, *context = packet.bundle.excerpts
    assert [(entry.source.range, entry.source.text) for entry in (changed, head)] == [
        (
            SourceLocation(
                side="base",
                commit_id=PINS.merge_base,
                path=path,
                blob_id="c" * 40,
                start_line=2,
                end_line=2,
            ),
            "old\n",
        ),
        (
            SourceLocation(
                side="head",
                commit_id=PINS.head,
                path=path,
                blob_id="d" * 40,
                start_line=2,
                end_line=2,
            ),
            "new\n",
        ),
    ]
    assert {item.source.text for item in context} == {"before\n", "after\n"}
    assert not any(item.changed_lines for item in context)
    assert source.reads == [("base", path), ("head", path)] and source.patch_reads == 1
    assert packet.bundle.evidence_bytes == len(packet.bundle.evidence_text.encode())


@pytest.mark.parametrize(
    ("case", "gap", "blob_reads", "patch_reads"),
    [
        ("binary", "ineligible", 0, 0),
        ("symlink", "ineligible", 0, 0),
        ("mode-only", "missing_evidence", 0, 0),
        ("rename-only", "missing_evidence", 0, 0),
        ("no-patch", "missing_evidence", 0, 1),
        ("unavailable-patch", "missing_evidence", 0, 1),
        ("truncated-patch", "work_limit", 0, 1),
        ("invalid-utf8", "missing_evidence", 2, 1),
        ("wrong-blob", "missing_evidence", 2, 1),
        ("wrong-commit", "missing_evidence", 1, 1),
        ("capped-blob", "work_limit", 0, 0),
    ],
    ids=lambda val: val if isinstance(val, str) else None,
)
def test_unusable_pinned_files_offer_no_coverage(
    case: str, gap: str, blob_reads: int, patch_reads: int
) -> None:
    kwargs = {"head": b"before\n\xff\nafter\n"} if case == "invalid-utf8" else {}
    if case == "capped-blob":
        kwargs = {"base": b"x" * (128 * 1024 + 1)}
    source = CoverageSource("code", **kwargs)
    change = source.change
    assert change.old and change.new
    if case == "binary":
        change = row("code", binary=True)
    elif case == "symlink":
        change = change.model_copy(
            update={"new": change.new.model_copy(update={"mode": "120000"})}
        )
    elif case in ("mode-only", "rename-only"):
        new = change.new.model_copy(update={"object_id": change.old.object_id})
        change = change.model_copy(
            update={
                "change_type": "renamed" if case == "rename-only" else "modified",
                "new": new.model_copy(
                    update={"path": "renamed"}
                    if case == "rename-only"
                    else {"mode": "100755"}
                ),
                "added_lines": 0,
                "deleted_lines": 0,
            }
        )
    elif case == "no-patch":
        source.patch = ()
    elif case == "unavailable-patch":
        source.patch = "unavailable"
    elif case == "truncated-patch":
        source.patch = "limit_reached"
    elif case == "wrong-blob":
        change = change.model_copy(
            update={"new": change.new.model_copy(update={"object_id": "e" * 40})}
        )
    pins = (
        PINS.model_copy(update={"merge_base": "f" * 40})
        if case == "wrong-commit"
        else PINS
    )
    work = PinnedEvidenceWork(source, pins, lambda: 0, 30)
    prepared = prepare_coverage((change,), work, EvidenceIndex())
    assert not prepared.required and not prepared.changed
    assert [(item.file_id, item.reason) for item in prepared.omissions] == [
        ("p00001", gap)
    ]
    assert len(source.reads) == blob_reads and source.patch_reads == patch_reads


@pytest.mark.parametrize(
    "length", [500, 4096], ids=["escaped-packet-limit", "schema-limit"]
)
def test_long_lines_are_never_truncated_to_fit(length: int) -> None:
    source = CoverageSource("code", head=b"before\n" + b"\x01" * length + b"\nafter\n")
    work = PinnedEvidenceWork(source, PINS, lambda: 0, 30)
    prepared = prepare_coverage(source.inventory(), work, EvidenceIndex())
    all_lines = _all_lines(prepared)
    assert len(all_lines) == 2
    packet = build_coverage_packet(prepared, all_lines, 1600)
    assert [(item.source.side, item.source.start_line) for item in packet.offered] == [
        ("base", 2)
    ]
    assert packet.bundle.evidence_bytes <= 1600
    assert [
        item.source.text for item in packet.bundle.excerpts if item.changed_lines
    ] == ["old\n"]
    if length == 500:
        assert packet.unsendable == all_lines - packet.offered
        roomier = build_coverage_packet(prepared, all_lines - packet.offered, 4096)
        (entry,) = [item for item in roomier.bundle.excerpts if item.changed_lines]
        assert entry.source.text == "\x01" * length + "\n"
    else:
        assert [item.reason for item in prepared.omissions] == ["work_limit"]


class ReferenceSource(CoverageSource):
    def __init__(self, base: bytes, head: bytes) -> None:
        super().__init__("edit.go", base, head)
        self.matches: dict[str, SearchPaths | ReadFailure] = {}
        self.references: dict[str, bytes] = {}
        self.searches: list[tuple[RevisionSide, str, int]] = []
        self.reference_reads: list[tuple[RevisionSide, str, int]] = []

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
        assert regular_text
        self.searches.append((side, literal, max_bytes))
        return self.matches.get(literal, SearchPaths((), False, 0))

    @override
    def read_text_blob(
        self, side: RevisionSide, path: str, *, max_bytes: int, timeout_seconds: float
    ) -> PinnedTextBlob | ReadFailure:
        if path == "edit.go":
            return super().read_text_blob(
                side, path, max_bytes=max_bytes, timeout_seconds=timeout_seconds
            )
        self.reference_reads.append((side, path, max_bytes))
        text = self.references[path]
        if len(text) > max_bytes:
            return "limit_reached"
        return PinnedTextBlob(
            side, PINS.merge_base if side == "base" else PINS.head, path, "e" * 40, text
        )


def test_packet_ranks_whole_short_tokens_and_inspects_only_two_canonical_paths() -> (
    None
):
    source = ReferenceSource(
        b"before\nreturn shared -1\nafter\n", b"before\nreturn shared + io\nafter\n"
    )
    source.matches["io"] = SearchPaths(("z", "b.ts", "a.go"), True, 20)
    source.references = {
        "a.go": b"// trio, io2, _io: not whole matches\n",
        "b.ts": b"// unrelated io\nfunction other() {}\n",
    }
    work = PinnedEvidenceWork(source, PINS, lambda: 0, 30)
    prepared = prepare_coverage(source.inventory(), work, EvidenceIndex())
    assert not source.searches
    packet = build_coverage_packet(
        prepared, _all_lines(prepared), 8192, reference_bytes=4096
    )
    assert [(side, literal) for side, literal, _ in source.searches] == [
        ("base", "io"),
        ("head", "io"),
        ("base", "shared"),
        ("head", "shared"),
    ]
    assert [(side, path) for side, path, _ in source.reference_reads] == [
        ("base", "a.go"),
        ("base", "b.ts"),
        ("head", "a.go"),
        ("head", "b.ts"),
    ]
    matches = [
        item for item in packet.bundle.excerpts if item.source.role == "text_match"
    ]
    assert len(matches) == 2
    assert all(
        item.source.range.path == "b.ts" and item.file_ids == ("p00001",)
        for item in matches
    )
    assert {item.source.text for item in matches} == {
        "// unrelated io\nfunction other() {}\n"
    }
    assert packet.offered == _all_lines(prepared)


def test_reference_seeds_come_only_from_offered_slices() -> None:
    source = ReferenceSource(
        b"before\nalpha beta gamma\nafter\n", b"before\ndelta epsilon zeta\nafter\n"
    )
    work = PinnedEvidenceWork(source, PINS, lambda: 0, 30)
    prepared = prepare_coverage(source.inventory(), work, EvidenceIndex())
    head = frozenset(
        line for line in _all_lines(prepared) if line.source.side == "head"
    )
    packet = build_coverage_packet(prepared, head, 8192, reference_bytes=4096)
    assert packet.offered == head
    assert [(side, token) for side, token, _ in source.searches] == [
        ("base", "delta"),
        ("head", "delta"),
        ("base", "epsilon"),
        ("head", "epsilon"),
    ]


@pytest.mark.parametrize(
    "text",
    [b"\xff io\n", ("é" * 191 + " io\n").encode()],
    ids=["invalid-utf8", "384-byte-cap"],
)
def test_reference_excerpt_must_be_short_exact_utf8(text: bytes) -> None:
    source = ReferenceSource(
        b"before\nreturn io + 1\nafter\n", b"before\nreturn io + 2\nafter\n"
    )
    source.matches["io"] = SearchPaths(("consumer",), False, 4)
    source.references["consumer"] = text
    work = PinnedEvidenceWork(source, PINS, lambda: 0, 30)
    prepared = prepare_coverage(source.inventory(), work, EvidenceIndex())
    baseline = build_coverage_packet(prepared, _all_lines(prepared), 8192)
    packet = build_coverage_packet(
        prepared, _all_lines(prepared), 8192, reference_bytes=4096
    )
    assert len(source.reference_reads) == 2
    assert packet == baseline


@pytest.mark.parametrize(
    "case",
    ["punctuation", "failed-search", "no-headroom", "thin-headroom", "no-packet-space"],
)
def test_optional_references_cannot_displace_required_evidence(case: str) -> None:
    source = ReferenceSource(
        b"before\n!\nafter\n"
        if case == "punctuation"
        else b"before\nreturn old\nafter\n",
        b"before\n?\nafter\n"
        if case == "punctuation"
        else b"before\nreturn new\nafter\n",
    )
    source.matches["old"] = (
        SearchPaths(("consumer",), False, 8)
        if case == "thin-headroom"
        else "limit_reached"
    )
    source.references["consumer"] = b"old " + b"x" * 40
    work = PinnedEvidenceWork(source, PINS, lambda: 0, 30)
    prepared = prepare_coverage(source.inventory(), work, EvidenceIndex())
    if case == "no-packet-space":
        prepared = replace(
            prepared,
            changed=tuple(replace(item, surrounding=()) for item in prepared.changed),
        )
    lines = _all_lines(prepared)
    baseline = build_coverage_packet(prepared, lines, 8192)
    packet = build_coverage_packet(
        prepared,
        lines,
        baseline.bundle.evidence_bytes if case == "no-packet-space" else 8192,
        reference_bytes={"no-headroom": 0, "thin-headroom": 10}.get(case, 2048),
    )
    assert packet == baseline
    if case == "failed-search":
        assert source.searches and not source.reference_reads
    elif case == "thin-headroom":
        assert source.searches == [("base", "old", 10)]
        assert source.reference_reads == [("base", "consumer", 2)]
        assert isinstance(work.blob("base", "consumer"), PinnedTextBlob)
    else:
        assert not source.searches and not source.reference_reads


@pytest.mark.parametrize("remaining", ["blob-bytes", "blob-call", "search-call"])
def test_optional_retrieval_leaves_capacity_for_required_reads(remaining: str) -> None:
    source = ReferenceSource(
        b"before\nreturn io + 1\nafter\n", b"before\nreturn io + 2\nafter\n"
    )
    source.matches["io"] = SearchPaths(("consumer",), False, 4)
    source.references = {"consumer": b"io\n", "verification": b"ok\n"}
    work = PinnedEvidenceWork(source, PINS, lambda: 0, 30)
    prepared = prepare_coverage(source.inventory(), work, EvidenceIndex())
    if remaining == "blob-bytes":
        work.bytes_left = 3
    elif remaining == "blob-call":
        work.counts["blob"] = 249
    else:
        work.counts["search"] = 39
    packet = build_coverage_packet(
        prepared, _all_lines(prepared), 8192, reference_bytes=4096
    )
    assert packet.offered == _all_lines(prepared)
    assert not any(item.source.role == "text_match" for item in packet.bundle.excerpts)
    assert not source.reference_reads
    if remaining == "search-call":
        assert not source.searches
        assert isinstance(
            work.search("base", "verification", regular_text=True), SearchPaths
        )
    else:
        assert isinstance(work.blob("base", "verification"), PinnedTextBlob)


@pytest.mark.parametrize(
    "shown_as_change", [True, False], ids=["changed", "supporting-only"]
)
def test_v6_change_citation_must_be_shown_as_changed(shown_as_change: bool) -> None:
    source = ReferenceSource(b"before\nold\nafter\n", b"before\nnew\nafter\n")
    work = PinnedEvidenceWork(source, PINS, lambda: 0, 30)
    index = EvidenceIndex()
    prepared = prepare_coverage(source.inventory(), work, index)
    bundle = build_coverage_packet(prepared, _all_lines(prepared), 8192).bundle
    base, head, *_ = bundle.excerpts
    if not shown_as_change:
        bundle = replace(bundle, excerpts=(base, replace(head, changed_lines=())))
    candidate = Candidate(
        file_id="p00001",
        changed_behavior="The value changes.",
        trigger="Read the value.",
        consequence="The result differs.",
        proposed_impact="material_incorrect_behavior",
        proposed_anchor_id=None,
        citations=(
            Citation(role="base_behavior", evidence_id=base.source.id),
            Citation(role="head_change", evidence_id=head.source.id),
            Citation(role="trigger", evidence_id=head.source.id),
            Citation(role="consequence", evidence_id=head.source.id),
            Citation(role="impact", evidence_id=head.source.id),
        ),
    )
    packet = build_source_packet(
        "c00001", candidate, bundle, work, index, pinned_text=True
    )
    if shown_as_change:
        assert isinstance(packet, SourcePacket)
    else:
        assert isinstance(packet, PrivateLead)
        assert "Citation role" in packet.missing_fact


def test_preparation_preserves_verification_blob_calls() -> None:
    source = ReferenceSource(b"before\nold\nafter\n", b"before\nnew\nafter\n")
    source.references = {"verification": b"ok\n"}
    work = PinnedEvidenceWork(source, PINS, lambda: 0, 30, counts={"blob": 241})
    prepared = prepare_coverage(source.inventory(), work, EvidenceIndex())
    assert [item.reason for item in prepared.omissions] == ["work_limit"]
    assert not source.reads and not source.patch_reads
    assert isinstance(work.blob("base", "verification"), PinnedTextBlob)


def test_unfittable_nearby_context_does_not_allocate_evidence_ids() -> None:
    source = CoverageSource("edit.go")
    work = PinnedEvidenceWork(source, PINS, lambda: 0, 30)
    index = EvidenceIndex()
    prepared = prepare_coverage(source.inventory(), work, index)
    required = _all_lines(prepared)
    baseline = build_coverage_packet(prepared, required, 8192, supporting_bytes=0)
    indexed = tuple(index.entries)
    assert (
        build_coverage_packet(prepared, required, baseline.bundle.evidence_bytes + 1)
        == baseline
    )
    assert tuple(index.entries) == indexed


@pytest.mark.parametrize("mismatch", ["blob-id", "size"])
def test_optional_blob_rejected_against_inventory_does_not_poison_required_read(
    mismatch: str,
) -> None:
    class InconsistentSource(CoverageSource):
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
            assert isinstance(blob, PinnedTextBlob)
            if len(self.reads) != 1:
                return blob
            return (
                replace(blob, blob_id="e" * 40)
                if mismatch == "blob-id"
                else replace(blob, text=b"wrong length")
            )

    source = InconsistentSource("edit.go")
    work = PinnedEvidenceWork(source, PINS, lambda: 0, 30)
    expected = source.change.new
    assert expected is not None
    assert (
        work.blob(
            "head", expected.path, expected=expected, optional=OptionalRetrieval(4096)
        )
        == "unavailable"
    )
    loaded = work.blob("head", expected.path, expected=expected)
    assert isinstance(loaded, PinnedTextBlob) and loaded.text == source.text["head"]
    assert source.reads == [("head", expected.path)] * 2


def test_reference_to_existing_context_can_be_cited_for_another_file(
    tmp_path: Path,
) -> None:
    init(tmp_path)
    producer = tmp_path / "a.go"
    consumer = tmp_path / "b.go"
    producer.write_text("func io() int { return 1 }\n")
    consumer.write_text("use(io())\nsink(1)\n")
    base = commit(tmp_path)
    producer.write_text("func io() int { return 2 }\n")
    consumer.write_text("use(io())\nsink(2)\n")
    pins = PinnedComparison(merge_base=base, head=commit(tmp_path))
    with open_local_comparison(tmp_path, pins, PATH_ENV) as source:
        work = PinnedEvidenceWork(source, pins, lambda: 0, 30)
        prepared = prepare_coverage(
            validated_inventory(source, pins), work, EvidenceIndex()
        )
        lines = _all_lines(prepared)
        baseline = build_coverage_packet(prepared, lines, 8192)
        packet = build_coverage_packet(prepared, lines, 8192, reference_bytes=8192)
    a, b = prepared.inventory
    assert {
        item.file_ids
        for item in baseline.bundle.excerpts
        if item.source.text == "use(io())\n"
    } == {(b.id,)}
    assert {
        item.source.range.side
        for item in packet.bundle.excerpts
        if item.source.role == "text_match"
        and item.source.range.path == b.path
        and a.id in item.file_ids
        and item.source.text.startswith("use(io())\n")
    } == {"base", "head"}


def test_packet_text_matches_use_pinned_consumers_including_the_edited_path(
    tmp_path: Path,
) -> None:
    init(tmp_path)
    producer = tmp_path / "b.go"
    filler = "// stable filler\n" * 12
    producer.write_text(
        "func io() int { return 1 }\n" + filler + "func local() int { return io() }\n"
    )
    consumer = tmp_path / "a"
    consumer.write_text("// consumer\nuse(io())\n")
    other_edit = tmp_path / "c.go"
    other_edit.write_text("sink(io(), 1)\n")
    base = commit(tmp_path)
    producer.write_text(
        "func io() int { return 2 }\n" + filler + "func local() int { return io() }\n"
    )
    other_edit.write_text("sink(io(), 2)\n")
    pins = PinnedComparison(merge_base=base, head=commit(tmp_path))
    producer.write_text("dirty\n")
    consumer.write_text("dirty\n")
    with open_local_comparison(tmp_path, pins, PATH_ENV) as source:
        work = PinnedEvidenceWork(source, pins, lambda: 0, 30)
        prepared = prepare_coverage(
            validated_inventory(source, pins), work, EvidenceIndex()
        )
        packet = build_coverage_packet(
            prepared, _all_lines(prepared), 8192, reference_bytes=8192
        )
    matches = [
        item for item in packet.bundle.excerpts if item.source.role == "text_match"
    ]
    assert {
        (item.source.range.commit_id, item.source.range.path) for item in matches
    } == {
        (commit_id, path)
        for commit_id in (pins.merge_base, pins.head)
        for path in ("a", "b.go")
    }
    for item in matches:
        loc = item.source.range
        assert loc.blob_id == revision(tmp_path, f"{loc.commit_id}:{loc.path}")
        assert item.file_ids == ("p00001", "p00002") and not item.changed_lines
        assert loc.start_line == (1 if loc.path == "a" else 13)
        assert item.source.text == (
            "// consumer\nuse(io())\n"
            if loc.path == "a"
            else "// stable filler\nfunc local() int { return io() }\n"
        )
    assert packet.offered == _all_lines(prepared)
