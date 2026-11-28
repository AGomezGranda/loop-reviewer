import json
import sys
import tomllib
from pathlib import Path
from typing import TypedDict

import pytest

from loop_reviewer import semantic_review_cli
from loop_reviewer.semantic_review_cli import main
from tests.support.inventory_git import commit, init, revision


@pytest.mark.parametrize(
    "version", [3, 5, 6], ids=["legacy-v3", "default-v5", "opt-in-v6"]
)
def test_standalone_semantic_entrypoint_is_registered_and_model_optional(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    version: int,
) -> None:
    scripts = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    )["project"]["scripts"]
    assert scripts["loop-reviewer-semantic"] == "loop_reviewer.semantic_review_cli:main"
    init(tmp_path)
    targets = [tmp_path / f"module{i:02d}.py" for i in range(17 if version == 3 else 1)]
    for target in targets:
        target.write_text("result = 0\n")
    commit(tmp_path)
    base = revision(tmp_path, "HEAD")
    for target in targets:
        target.write_text("result = 1\n")
    commit(tmp_path)
    args = [
        "loop-reviewer-semantic",
        "--repo",
        str(tmp_path),
        "--merge-base",
        base,
        "--head",
        revision(tmp_path, "HEAD"),
    ]
    if version != 5:
        args += ["--report-version", str(version)]
    if version == 6:
        monkeypatch.setattr(
            semantic_review_cli,
            "CpythonAnalyzer",
            lambda: pytest.fail("v6 must not construct an analyzer"),
        )
    monkeypatch.setattr(sys, "argv", args)
    monkeypatch.delenv("LOOP_REVIEWER_PI_MODEL", raising=False)
    assert main() == 3
    result = json.loads(capsys.readouterr().out)
    assert (
        result["file_coverage"][0]["reason"] == "budget_exhausted"
        and result["usage"]["model_calls"] == 0
    )
    assert (result["schema_version"], result["discussion_status"]) == (
        version,
        "not_requested",
    )
    if version == 6:
        assert result["policy_version"] == 4
        assert [item["kind"] for item in result["investigation_coverage"]] == [
            "behavior"
        ]
    spend = ["--max-model-spend-usd-micros", "250000"]
    monkeypatch.setattr(sys, "argv", args + spend)
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2 and not capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", args + spend + ["--allow-provider"])
    assert main() == 3
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    if version in (5, 6):
        assert "AI analysis unavailable: Set LOOP_REVIEWER_PI_MODEL" in captured.err
    else:
        assert not captured.err
    assert {row["reason"] for row in result["file_coverage"]} == {
        "unsupported_model"
    } and result["usage"]["model_calls"] == 0


@pytest.mark.parametrize(
    "valid", [True, False], ids=["empty-comparison", "missing-pin"]
)
def test_v6_standalone_exit_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    valid: bool,
) -> None:
    init(tmp_path)
    (tmp_path / "file.txt").write_text("original\n")
    commit(tmp_path)
    head = revision(tmp_path, "HEAD")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "loop-reviewer-semantic",
            "--repo",
            str(tmp_path),
            "--merge-base",
            head,
            "--head",
            head if valid else "f" * 40,
            "--report-version",
            "6",
        ],
    )
    assert main() == (0 if valid else 1)
    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == 6
    assert report["status"] == ("complete" if valid else "failed")
    assert report["file_coverage"] == []


class _SnapshotFixture(TypedDict):
    schema_version: int
    target: dict[str, object]
    captured_at: str
    status: str
    omissions: list[dict[str, object]]
    threads: list[object]
    reviews: list[object]
    conversation: list[dict[str, str]]


@pytest.mark.parametrize("version", [4, 6], ids=["legacy-v4", "pinned-v6"])
@pytest.mark.parametrize(
    "status",
    [
        "complete",
        "partial",
        "unavailable",
        "mismatch",
        "invalid",
        "oversized",
        "missing",
    ],
)
def test_local_snapshot_is_validated_before_review_and_text_stays_private(
    status: str,
    version: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    init(tmp_path)
    target = tmp_path / "module.py"
    target.write_text("result = 0\n")
    commit(tmp_path)
    base = revision(tmp_path, "HEAD")
    target.write_text("result =\n")
    commit(tmp_path)
    head = revision(tmp_path, "HEAD")
    snapshot = tmp_path / "snapshot.json"
    data: _SnapshotFixture = {
        "schema_version": 1,
        "target": {
            "host": "github.com",
            "repository_id": 42,
            "owner": "octo",
            "repo": "demo",
            "number": 7,
            "target_branch": "main",
            "comparison": {"merge_base": base, "head": head},
        },
        "captured_at": "2026-11-23T12:00:00Z",
        "status": status
        if status in {"complete", "partial", "unavailable"}
        else "complete",
        "omissions": [{"scope": "threads", "code": "read_error", "thread_id": None}]
        if status in {"partial", "unavailable"}
        else [],
        "threads": [],
        "reviews": [],
        "conversation": []
        if status == "unavailable"
        else [{"id": "PRC_1", "body": "PRIVATE_CANARY"}],
    }
    if status == "mismatch":
        data["target"] = {
            **data["target"],
            "comparison": {"merge_base": base, "head": "a" * 40},
        }
    if status != "missing":
        snapshot.write_text(
            {"invalid": "{}", "oversized": " " * (512 * 1024 + 1)}.get(
                status, json.dumps(data)
            )
        )
    invalid = status in {"mismatch", "invalid", "oversized", "missing"}
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "loop-reviewer-semantic",
            "--repo",
            str(tmp_path / "absent" if invalid else tmp_path),
            "--merge-base",
            base,
            "--head",
            head,
            "--report-version",
            str(version),
            "--discussion-snapshot",
            str(snapshot),
        ],
    )
    if invalid:
        with pytest.raises(SystemExit) as error:
            main()
        assert error.value.code == 2 and not capsys.readouterr().out
        return
    assert main() == (0 if version == 4 and status == "complete" else 3)
    output = capsys.readouterr().out
    assert "PRIVATE_CANARY" not in output
    report = json.loads(output)
    assert (
        report["schema_version"],
        report["policy_version"],
        report["discussion_status"],
    ) == (version, 2 if version == 4 else 4, status)
    if version == 4:
        assert (
            report["stop_reason"] == "finished"
            and report["recommendation"] == "comment"
        )
        assert report["file_coverage"][0]["outcome"] == "complete"
        assert report["findings"][0]["decision_reason"] == "reproduced_regression"
    else:
        assert (
            report["stop_reason"] == "budget_exhausted"
            and report["recommendation"] == "none"
        )
        assert report["file_coverage"][0]["outcome"] == "unfinished"
        assert report["findings"] == []
