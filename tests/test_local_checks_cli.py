import subprocess
import sys
from pathlib import Path

from loop_reviewer.reviewer.local_check_contract import LocalCheckResult, SourceEvidence
from tests.support.inventory_git import PATH_ENV, commit, git, init


def test_local_checks_command_exposes_context_and_only_compiler_regressions(
    tmp_path: Path,
) -> None:
    repo, home, state = (tmp_path / name for name in ("repo", "home", "state"))
    for directory in (repo, home, state):
        directory.mkdir()
    init(repo)
    producer = "def fetch():\n    return None\n\ndef same_module():\n    if fetch() is None:\n        return 'missing'\n"
    (repo / "producer.py").write_text(producer)
    (repo / "consumer.py").write_text(
        "from producer import fetch\n\nif fetch() is None:\n    print('missing')\n"
    )
    (repo / "broken.py").write_text("def run():\n    pass\n")
    base = commit(repo)
    (repo / "producer.py").write_text(producer.replace("return None", "return []"))
    (repo / "broken.py").write_text("def run():\n    break\n")
    head = commit(repo)
    (repo / "README.md").write_text("Not checked by the Python compiler\n")
    partial_head = commit(repo)
    (repo / "consumer.py").write_text("dirty\n")

    def run(head_arg: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [
                str(Path(sys.executable).parent / "loop-reviewer-check"),
                "--repo",
                str(repo),
                "--merge-base",
                base,
                "--head",
                head_arg,
            ],
            cwd=tmp_path,
            env={**PATH_ENV, "HOME": str(home), "XDG_STATE_HOME": str(state)},
            capture_output=True,
            check=False,
            timeout=90,
        )

    result = run(head)
    assert result.returncode == 0, result.stderr
    assert result.stdout.count(b"\n") == 1
    checked = LocalCheckResult.model_validate_json(result.stdout).root
    assert checked.status == "complete" and checked.usage.model_calls == 0
    excerpts = {
        entry.role: entry
        for entry in checked.evidence_index
        if isinstance(entry, SourceEvidence)
        and entry.range.path in ("producer.py", "consumer.py")
    }
    assert "return None" in excerpts["base_declaration"].text
    assert "return []" in excerpts["head_declaration"].text
    callers = {
        entry.range.path: entry
        for entry in checked.evidence_index
        if isinstance(entry, SourceEvidence) and entry.role == "call_site"
    }
    assert "def same_module():" in callers["producer.py"].text
    caller = callers["consumer.py"]
    assert "if fetch() is None:" in caller.text
    assert caller.text.encode() == git(repo, "show", f"{head}:consumer.py")
    (finding,) = checked.findings
    assert finding.anchor is not None and finding.anchor.path == "broken.py"
    diff = git(repo, "diff", base, head, "--", "broken.py")
    assert b"+    break\n" in diff and finding.anchor.start_line == 2
    assert checked.recommendation == "comment"

    result = run(partial_head)
    assert result.returncode == 3, result.stderr
    partial = LocalCheckResult.model_validate_json(result.stdout).root
    assert partial.status == "partial" and len(partial.findings) == 1
    assert any(row.reason == "unsupported_language" for row in partial.file_coverage)

    result = run("f" * len(head))
    assert result.returncode == 1 and result.stderr == b""
    failed = LocalCheckResult.model_validate_json(result.stdout).root
    assert failed.status == "failed" and failed.error.code == "missing_commit"

    result = run(head[:12])
    assert result.returncode == 2 and not result.stdout
    assert b"full" in result.stderr
    assert not list(home.iterdir()) and not list(state.iterdir())
