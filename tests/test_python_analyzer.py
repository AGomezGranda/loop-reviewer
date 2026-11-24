from pathlib import Path

import pytest

from loop_reviewer.reviewer.integrations import python_analyzer
from loop_reviewer.reviewer.integrations.python_analyzer import CpythonAnalyzer
from loop_reviewer.reviewer.pinned_source import PinnedTextBlob


def blob(text: bytes) -> PinnedTextBlob:
    return PinnedTextBlob("head", "a" * 40, "code.py", "b" * 40, text)


def test_worker_compiles_without_executing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    sentinel = tmp_path / "executed"
    (tmp_path / "ast.py").write_text("raise RuntimeError('repository ast')\n")
    (tmp_path / "sitecustomize.py").write_text(
        "raise RuntimeError('repository config')\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    analysis = CpythonAnalyzer().analyze(
        blob(
            f"import missing_dependency\nopen({str(sentinel)!r}, 'w').close()\n".encode()
        ),
        timeout_seconds=2,
    )
    assert not isinstance(analysis, str)
    assert analysis.diagnostic.result == "passed"
    assert not sentinel.exists()
    invalid = CpythonAnalyzer().analyze(blob(b"return 1\n"), timeout_seconds=2)
    assert not isinstance(invalid, str)
    assert invalid.diagnostic.result == "syntax_error"
    assert invalid.diagnostic.location is not None
    assert invalid.diagnostic.location.start_line == 1


def test_ast_fact_cap_does_not_erase_a_compiler_result() -> None:
    source = b"".join(f"def f{i}(): pass\n".encode() for i in range(150))
    analysis = CpythonAnalyzer().analyze(
        blob(source + b"return 1\n"), timeout_seconds=2
    )
    assert not isinstance(analysis, str)
    assert analysis.limited and len(analysis.declarations) == 128
    assert analysis.diagnostic.result == "syntax_error"
    assert analysis.diagnostic.location is not None
    assert analysis.diagnostic.location.start_line == 151


@pytest.mark.parametrize(
    ("worker", "expected"),
    [
        ("while True: pass", "limit_reached"),
        ("print('{}')", "unavailable"),
        ("raise MemoryError", "unavailable"),
    ],
    ids=["hang", "malformed", "crash"],
)
def test_worker_failure_never_becomes_a_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker: str, expected: str
) -> None:
    script = tmp_path / "worker.py"
    script.write_text(worker)
    monkeypatch.setattr(python_analyzer, "_WORKER", script)
    timeout = 0.1 if expected == "limit_reached" else 2
    assert (
        CpythonAnalyzer().analyze(blob(b"pass\n"), timeout_seconds=timeout) == expected
    )
