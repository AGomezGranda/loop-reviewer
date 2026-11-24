import json
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import override

import pytest
from pydantic import ValidationError

from loop_reviewer.reviewer.integrations.git_hunks import decode_hunks, one_sided_hunks
from loop_reviewer.reviewer.inventory import InventorySourceError, RevisionSide
from loop_reviewer.reviewer.inventory_contract import (
    ChangedFile,
    FileSide,
    PinnedComparison,
)
from loop_reviewer.reviewer.local_check_contract import (
    LocalCheckBudget,
    LocalCheckRequest,
    LocalCheckResult,
)
from loop_reviewer.reviewer.local_checks import review_local_checks
from loop_reviewer.reviewer.pinned_source import (
    Hunk,
    PinnedTextBlob,
    ReadFailure,
    SearchPaths,
    SourceRange,
)
from loop_reviewer.reviewer.python_context import (
    Call,
    Declaration,
    Diagnostic,
    ImportAlias,
    PythonAnalysis,
)

PINS = PinnedComparison(merge_base="a" * 40, head="b" * 40)
REQUEST = LocalCheckRequest(
    schema_version=2,
    scope="local_checks",
    comparison=PINS,
    budget=LocalCheckBudget(max_model_spend_usd_micros=0),
    discussion=None,
)


def row(
    path: str = "code.py", *, added: bool = False, binary: bool = False
) -> ChangedFile:
    return ChangedFile(
        change_type="added" if added else "modified",
        old=None
        if added
        else FileSide(
            path=path,
            object_id="c" * 40,
            mode="100644",
            blob_bytes=15,
            generated="unknown",
        ),
        new=FileSide(
            path=path,
            object_id="d" * 40,
            mode="100644",
            blob_bytes=15,
            generated="unknown",
        ),
        added_lines=None if binary else 1,
        deleted_lines=None if binary else int(not added),
        binary=binary,
        oversized=False,
    )


class Source:
    def __init__(
        self,
        rows: tuple[ChangedFile, ...],
        *,
        base_failure: ReadFailure | None = None,
        hunk_failure: ReadFailure | None = None,
    ) -> None:
        self.rows = rows
        self.base_failure = base_failure
        self.hunk_failure = hunk_failure
        self.time = 0.0
        self.after_read: float | None = None

    @contextmanager
    def open(self, pins: PinnedComparison) -> Generator[Source]:
        assert pins == PINS
        yield self

    def inventory(self) -> tuple[ChangedFile, ...]:
        return self.rows

    def read_text_blob(
        self, side: RevisionSide, path: str, *, max_bytes: int, timeout_seconds: float
    ) -> PinnedTextBlob | ReadFailure:
        if self.after_read is not None and path.startswith("z"):
            self.time = self.after_read
        if side == "base":
            if self.base_failure:
                return self.base_failure
            if any(
                change.old is None and change.new and change.new.path == path
                for change in self.rows
            ):
                return "not_found"
        return PinnedTextBlob(
            side,
            PINS.merge_base if side == "base" else PINS.head,
            path,
            ("c" if side == "base" else "d") * 40,
            b"# context\npass\n",
        )

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
        return SearchPaths((), False, retrieved_bytes=0)

    def hunks(
        self, change: ChangedFile, *, max_bytes: int, timeout_seconds: float
    ) -> tuple[Hunk, ...] | ReadFailure:
        if self.hunk_failure:
            return self.hunk_failure
        if change.change_type == "added":
            return one_sided_hunks(b"# context\npass\n", added=True, limit=max_bytes)
        return decode_hunks(b"@@ -1,2 +1,2 @@\n # context\n-old\n+pass\n")


class Analyzer:
    def __init__(
        self, *, base_error: bool = False, head_error: bool = True, line: int | None = 2
    ) -> None:
        self.base_error = base_error
        self.head_error = head_error
        self.line = line

    def analyze(
        self, blob: PinnedTextBlob, *, timeout_seconds: float
    ) -> PythonAnalysis:
        error = self.base_error if blob.side == "base" else self.head_error
        line = self.line
        location = (
            SourceRange(blob.side, blob.commit_id, blob.path, blob.blob_id, line, line)
            if error and line is not None
            else None
        )
        return PythonAnalysis(
            Diagnostic(
                blob,
                "3.15.2",
                "syntax_error" if error else "passed",
                "fixture",
                location,
            )
        )


@pytest.mark.parametrize(
    ("change", "base_failure", "analyzer", "expected", "status"),
    [
        pytest.param(row(), None, Analyzer(), 1, "complete", id="paired-new-error"),
        pytest.param(row(added=True), None, Analyzer(), 1, "complete", id="addition"),
        pytest.param(
            row(), None, Analyzer(base_error=True), 0, "complete", id="preexisting"
        ),
        pytest.param(
            row(), "unavailable", Analyzer(), 0, "partial", id="unavailable-base"
        ),
        pytest.param(row(), None, Analyzer(line=1), 0, "complete", id="unchanged-line"),
        pytest.param(
            row(), None, Analyzer(line=None), 0, "complete", id="no-usable-line"
        ),
    ],
)
def test_compile_finding_requires_pinned_head_only_failure(
    change: ChangedFile,
    base_failure: ReadFailure | None,
    analyzer: Analyzer,
    expected: int,
    status: str,
) -> None:
    source = Source((change,), base_failure=base_failure)
    report = review_local_checks(
        REQUEST, source.open, analyzer, lambda: source.time
    ).root
    assert report.status == status
    assert len(report.findings) == expected
    if report.findings:
        (finding,) = report.findings
        assert finding.anchor is not None and finding.anchor.commit_id == PINS.head
        assert finding.anchor.start_line == 2
        assert len(finding.evidence_ids) == (1 if change.change_type == "added" else 2)


def test_local_checks_preserve_coverage_and_findings_on_limits() -> None:
    source = Source(
        (row("z.py"), row("a.py"), row("asset", binary=True), row("readme"))
    )
    source.after_read = 31
    result = review_local_checks(
        REQUEST, source.open, Analyzer(), lambda: source.time
    ).root
    assert result.status == "partial" and result.stop_reason == "work_limit"
    assert result.recommendation == "comment" and len(result.findings) == 1
    assert [
        (r.new.path, r.outcome, r.reason) for r in result.file_coverage if r.new
    ] == [
        ("a.py", "complete", None),
        ("asset", "excluded", "binary"),
        ("readme", "unfinished", "unsupported_language"),
        ("z.py", "unfinished", "work_limit"),
    ]
    optional = Source((row(),), hunk_failure="limit_reached")
    result = review_local_checks(
        REQUEST, optional.open, Analyzer(head_error=False), lambda: 0
    ).root
    assert (
        result.status == "complete"
        and result.file_coverage[0].context_status == "limited"
    )
    result = review_local_checks(REQUEST, optional.open, Analyzer(), lambda: 0).root
    assert (
        result.status == "partial"
        and result.stop_reason == "finished"
        and not result.findings
    )
    assert result.file_coverage[0].reason == "work_limit"
    empty = Source(())
    assert (
        review_local_checks(REQUEST, empty.open, Analyzer(), lambda: 0).root.status
        == "complete"
    )

    @contextmanager
    def broken(_: PinnedComparison) -> Generator[Source]:
        raise InventorySourceError("missing_commit", "Missing pin")
        yield empty

    failure = review_local_checks(REQUEST, broken, Analyzer(), lambda: 0).root
    assert failure.status == "failed" and failure.error.code == "missing_commit"


class SharedCallerSource(Source):
    def __init__(self, *, truncated: bool = False) -> None:
        super().__init__((row("b.py"), row("a.py")))
        self.truncated = truncated

    @override
    def read_text_blob(
        self, side: RevisionSide, path: str, *, max_bytes: int, timeout_seconds: float
    ) -> PinnedTextBlob:
        name = path[0].encode()
        body = b"  ...\n" if side == "base" else b"  f()\n"
        text = (
            b"from a import f\nfrom b import g\nf(); g()\n"
            if path == "caller.py"
            else b"def " + name + b"():\n" + body
        )
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
        return SearchPaths(("caller.py",), self.truncated, retrieved_bytes=50)

    @override
    def hunks(
        self, change: ChangedFile, *, max_bytes: int, timeout_seconds: float
    ) -> tuple[Hunk, ...] | ReadFailure:
        assert change.new is not None
        name = change.new.path[0].encode()
        return decode_hunks(b"@@ -1,2 +1,2 @@\n def " + name + b"():\n-  ...\n+  f()\n")


class SharedCallerAnalyzer(Analyzer):
    def __init__(self) -> None:
        super().__init__(head_error=False)

    @override
    def analyze(
        self, blob: PinnedTextBlob, *, timeout_seconds: float
    ) -> PythonAnalysis:
        diagnostic = super().analyze(blob, timeout_seconds=timeout_seconds).diagnostic
        if blob.path == "caller.py":
            return PythonAnalysis(
                diagnostic,
                calls=(Call("f", 3, 3), Call("g", 3, 3)),
                imports=(ImportAlias("f", "a.f"), ImportAlias("g", "b.g")),
            )
        return PythonAnalysis(
            diagnostic,
            declarations=(
                Declaration("f" if blob.path == "a.py" else "g", "function", 1, 2),
            ),
        )


@pytest.mark.parametrize(
    "truncated", [False, True], ids=["complete-context", "limited-context"]
)
def test_local_checks_preserve_serialized_report_with_shared_caller(
    truncated: bool,
) -> None:
    source = SharedCallerSource(truncated=truncated)
    result = review_local_checks(
        REQUEST, source.open, SharedCallerAnalyzer(), lambda: 0
    )
    snapshot = Path(__file__).parent / "fixtures" / "local_checks_shared_caller.json"
    expected = json.loads(snapshot.read_text())
    if truncated:
        for file in expected["file_coverage"]:
            file["context_status"] = "limited"
    assert json.loads(result.model_dump_json()) == expected


@pytest.mark.parametrize(
    "case",
    [
        "version",
        "unknown-field",
        "dangling-evidence",
        "wrong-revision",
        "recommendation",
        "status",
    ],
)
def test_local_check_contract_rejects_invalid_reports(case: str) -> None:
    source = Source((row(),))
    report = review_local_checks(REQUEST, source.open, Analyzer(), lambda: 0)
    payload = json.loads(report.model_dump_json())
    match case:
        case "version":
            payload["schema_version"] = 1
        case "unknown-field":
            payload["evidence_index"][0]["unknown"] = True
        case "dangling-evidence":
            payload["findings"][0]["evidence_ids"] = ["e99999"]
        case "wrong-revision":
            payload["evidence_index"][0]["commit_id"] = PINS.head
        case "recommendation":
            payload["recommendation"] = "none"
        case "status":
            payload["status"] = "partial"
    with pytest.raises(ValidationError):
        LocalCheckResult.model_validate_json(json.dumps(payload))
