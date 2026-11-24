import json
from pathlib import Path
from typing import Literal

import pytest

from tests.support.legacy_replay import paired_gate, read_legacy_replay
from tests.support.reviewer_replay import build_replay, replay_manifest
from tests.support.score_reviewer import adjudicate, run_replay


def test_legacy_replay_requires_exact_pins_and_unique_cases(tmp_path: Path) -> None:
    case = {
        "id": "authored",
        "merge_base": "a" * 40,
        "head": "b" * 40,
        "legacy_status": "eligible",
    }
    result = {
        "case": "authored",
        "merge_base": case["merge_base"],
        "head": case["head"],
        "checkout_commit": "c" * 40,
        "model_id": "example/model",
        "outcome": "reviewed",
        "findings": (),
    }
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps({"schema_version": 1, "cases": [result]}))
    assert read_legacy_replay(path, [case])["authored"]["outcome"] == "reviewed"
    for cases, reason in (
        ([result, result], "Duplicate"),
        ([result | {"head": "c" * 40}], "Mismatch"),
        ([result | {"raw_output": "private"}], "raw_output"),
    ):
        path.write_text(json.dumps({"schema_version": 1, "cases": cases}))
        with pytest.raises(ValueError, match=reason):
            read_legacy_replay(path, [case])


@pytest.mark.parametrize(
    "failure", [None, "loss", "false-findings", "unmeasured", "unmatched-spend"]
)
def test_paired_gate_requires_measured_precision_and_nonregression(
    failure: str | None,
) -> None:
    def run(case: str, arm: str, matches: list[str], *, false: int = 0, cost: int = 10):
        return {
            "case": case,
            "arm": arm,
            "matches": matches,
            "false_findings": false,
            "duplicates": 0,
            "usage": {
                "estimated_cost_usd_micros": cost,
                "unaccounted_reserved_usd_micros": 0,
            },
        }

    rows = [
        run("authored", "legacy", ["legacy-witness"]),
        run("authored", "diff", ["legacy-witness"]),
        run(
            "authored",
            "targeted",
            ["new-witness"] if failure == "loss" else ["legacy-witness", "new-witness"],
            false=2 if failure == "false-findings" else 0,
            cost=20 if failure == "unmatched-spend" else 10,
        ),
        run("large", "legacy", [])
        | {"matches": None, "legacy_outcome": "patch_limit", "large_case": True},
    ]
    evaluation = paired_gate(rows, measured=failure != "unmeasured")
    assert evaluation["passes_pilot"] is (failure is None)


@pytest.mark.parametrize("policy", [3, 5], ids=["frozen-v3", "extended-v5"])
def test_fixture_replay_respects_split_lock_and_frozen_digest(
    tmp_path: Path, policy: Literal[3, 5]
) -> None:
    replay = tmp_path / "replay"
    fixtures = tmp_path / "responses"
    output = tmp_path / "score"
    fixtures.mkdir()
    build_replay(replay, policy=policy)
    with pytest.raises(ValueError, match="on development"):
        run_replay(replay, output, "holdout", fixtures, policy=policy)
    reply = {
        "reply": {
            "schema_version": 1,
            "investigation_id": "i00001",
            "kind": "submit_candidates",
            "reviewed_file_ids": ["p00001"],
            "candidates": (),
        }
    }
    manifest = replay_manifest(policy)
    names = {entry["id"] for entry in manifest if entry["split"] == "development"}
    for name in names:
        (fixtures / f"{name}.json").write_text(
            json.dumps({"diff": [reply], "targeted": [reply]})
        )
    result = run_replay(replay, output, "development", fixtures, policy=policy)
    assert {entry["case"] for entry in result["cases"]} == names
    assert all(entry["reviewed_intersection"] for entry in result["cases"])
    assert all(
        entry["targeted"]["usage"]["model_calls"] == 1 for entry in result["cases"]
    )
    charges = {case["id"]: case["diff_retrieval_bytes"] for case in manifest}
    assert all(
        entry["diff"]["limits"]["retrieved_bytes"] == charges[entry["case"]]
        for entry in result["cases"]
    )
    if policy == 5:
        assert "mixed-clean" in names
        mixed = next(
            entry for entry in result["cases"] if entry["case"] == "mixed-clean"
        )
        assert mixed["reviewed_intersection"] == ["settings.json"]
    scored = adjudicate(result, [])
    assert not scored["passes_pilot"]
    assert next(run for run in scored["runs"] if run["case"] == "unchanged-consumer")[
        "misses"
    ] == ["empty-result-indexing"]
    with pytest.raises(ValueError, match="configuration differs"):
        run_replay(replay, output, "holdout", None, live=True, policy=policy)
    artifact = replay / "diff-only" / "test-only.json"
    artifact.write_bytes(artifact.read_bytes() + b" ")
    with pytest.raises(ValueError, match="Existing replay differs"):
        run_replay(replay, output, "development", fixtures, policy=policy)


def test_offline_adjudication_distinguishes_duplicates_false_and_unsupported() -> None:
    modeled = {
        "stop": None,
        "findings": [{"id": "c00001"}, {"id": "c00002"}, {"id": "c00003"}],
        "reviewed_paths": ["producer.py"],
        "gaps": {},
        "usage": {},
        "elapsed_seconds": 0,
    }
    report = {
        "split": "development",
        "cases": [
            {
                "case": "unchanged-consumer",
                "compiler_findings": (),
                "large_case": False,
                "diff": modeled
                | {"stop": "unsupported_model", "reviewed_paths": (), "findings": ()},
                "targeted": modeled,
            }
        ],
    }
    adjudications = [
        {
            "case": "unchanged-consumer",
            "arm": "targeted",
            "candidate_id": "c00001",
            "verdict": "match",
            "label": "empty-result-indexing",
        },
        {
            "case": "unchanged-consumer",
            "arm": "targeted",
            "candidate_id": "c00002",
            "verdict": "duplicate",
            "label": "empty-result-indexing",
        },
        {
            "case": "unchanged-consumer",
            "arm": "targeted",
            "candidate_id": "c00003",
            "verdict": "false_finding",
            "rationale": "Pinned head consumer line 5 handles None.",
        },
    ]
    diff, targeted = adjudicate(report, adjudications)["runs"]
    assert diff["misses"] is None and diff["precision"] is None
    assert targeted["duplicates"] == 1 and targeted["false_findings"] == 1
    assert targeted["precision"] == 0.5 and targeted["misses"] == []
    for invalid in (None, False, 23, " "):
        with pytest.raises(ValueError, match="require a pinned-evidence rationale"):
            adjudicate(
                report,
                adjudications[:-1] + [adjudications[-1] | {"rationale": invalid}],
            )
