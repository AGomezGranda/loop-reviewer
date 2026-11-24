import subprocess
import sys
from pathlib import Path

from loop_reviewer.reviewer.inventory_contract import ReviewerResult
from tests.support.inventory_git import PATH_ENV, commit, init


def test_inventory_command_emits_versioned_report(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    state = tmp_path / "state"
    for directory in (repo, home, state):
        directory.mkdir()
    init(repo)
    (repo / "deleted").write_text("old\n")
    base = commit(repo)
    (repo / "deleted").unlink()
    (repo / "binary").write_bytes(b"\0binary")
    head = commit(repo)

    def run(head_arg: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [
                str(Path(sys.executable).parent / "loop-reviewer-inventory"),
                "--repo",
                str(repo),
                "--merge-base",
                base.upper(),
                "--head",
                head_arg,
            ],
            cwd=tmp_path,
            env={**PATH_ENV, "HOME": str(home), "XDG_STATE_HOME": str(state)},
            capture_output=True,
            check=False,
            timeout=90,
        )

    success = run(head.upper())
    assert success.returncode == 0, success.stderr
    assert success.stderr == b"" and success.stdout.count(b"\n") == 1
    report = ReviewerResult.model_validate_json(success.stdout).root
    assert report.status == "complete" and report.schema_version == 1
    assert report.comparison.head == head and report.comparison.merge_base == base
    assert len(report.file_coverage) == 2
    assert any(row.binary for row in report.file_coverage)
    assert any(row.change_type == "deleted" for row in report.file_coverage)

    failure = run("f" * len(head))
    assert failure.returncode == 1 and failure.stderr == b""
    failed = ReviewerResult.model_validate_json(failure.stdout).root
    assert failed.status == "failed" and failed.error.code == "missing_commit"

    invalid = run(head[:12])
    assert invalid.returncode == 2 and invalid.stdout == b""
    assert b"full" in invalid.stderr and b"--head" in invalid.stderr
    assert not list(home.iterdir()) and not list(state.iterdir())
