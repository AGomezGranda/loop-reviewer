"""Run and validate the fixed isolated CPython worker."""

import re
import sys
import tempfile
from math import isfinite
from pathlib import Path
from subprocess import TimeoutExpired
from time import monotonic
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from loop_reviewer.integrations.review_process import (
    OutputLimitExceeded,
    capture_review_process,
)
from loop_reviewer.reviewer.pinned_source import (
    PinnedTextBlob,
    ReadFailure,
    SourceRange,
)
from loop_reviewer.reviewer.python_context import (
    Call,
    Declaration,
    Diagnostic,
    ImportAlias,
    PythonAnalysis,
)

_WORKER = Path(__file__).resolve().with_name("python_analyzer_worker.py")
_Name = Annotated[str, Field(min_length=1, max_length=512)]
_Line = Annotated[int, Field(ge=1, le=128 * 1024)]


class _WorkerReport(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    version: Annotated[str, Field(pattern=r"^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$")]
    result: Literal["passed", "syntax_error"]
    message: Annotated[str, Field(min_length=1, max_length=1024)]
    location: _Line | None
    declarations: Annotated[
        tuple[
            tuple[_Name, Literal["class", "function", "async_function"], _Line, _Line],
            ...,
        ],
        Field(max_length=128),
    ]
    calls: Annotated[tuple[tuple[_Name, _Line, _Line], ...], Field(max_length=128)]
    imports: Annotated[tuple[tuple[_Name, _Name], ...], Field(max_length=128)]
    limited: bool


def _is_dotted(name: str) -> bool:
    return all(part.isidentifier() for part in name.split("."))


def _from_worker(output: bytes, blob: PinnedTextBlob) -> PythonAnalysis | ReadFailure:
    try:
        report = _WorkerReport.model_validate_json(output)
    except ValidationError:
        return "unavailable"
    line_count = blob.text.count(b"\n") + int(
        bool(blob.text) and not blob.text.endswith(b"\n")
    )
    if report.version != ".".join(map(str, sys.version_info[:3])):
        return "unavailable"
    if report.location is not None and (
        report.result == "passed" or report.location > line_count
    ):
        return "unavailable"
    if any(
        not _is_dotted(name) or not start <= end <= line_count
        for name, _, start, end in report.declarations
    ):
        return "unavailable"
    if any(
        not _is_dotted(name) or not start <= end <= line_count
        for name, start, end in report.calls
    ):
        return "unavailable"
    if any(
        not name.isidentifier() or not _is_dotted(qualified)
        for name, qualified in report.imports
    ):
        return "unavailable"
    if (
        report.location is not None or report.declarations or report.calls
    ) and re.search(rb"\r(?!\n)", blob.text):
        return "unavailable"
    location = (
        None
        if report.location is None
        else SourceRange(
            blob.side,
            blob.commit_id,
            blob.path,
            blob.blob_id,
            report.location,
            report.location,
        )
    )
    return PythonAnalysis(
        Diagnostic(blob, report.version, report.result, report.message, location),
        tuple(Declaration(*row) for row in report.declarations),
        tuple(Call(*row) for row in report.calls),
        tuple(ImportAlias(*row) for row in report.imports),
        report.limited,
    )


class CpythonAnalyzer:
    def analyze(
        self, blob: PinnedTextBlob, *, timeout_seconds: float
    ) -> PythonAnalysis | ReadFailure:
        if (
            not isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or len(blob.text) > 128 * 1024
        ):
            return "limit_reached"
        deadline = monotonic() + min(timeout_seconds, 2)
        try:
            with tempfile.TemporaryDirectory(prefix="loop-reviewer-python-") as scratch:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return "limit_reached"
                result = capture_review_process(
                    [sys.executable, "-I", "-S", str(_WORKER)],
                    cwd=Path(scratch),
                    env={},
                    timeout=remaining,
                    output_limit=64 * 1024,
                    input=blob.text,
                )
        except TimeoutExpired:
            return "limit_reached"
        except OSError, OutputLimitExceeded:
            return "unavailable"
        return "unavailable" if result.returncode else _from_worker(result.stdout, blob)
