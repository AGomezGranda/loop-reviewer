"""Run fixture-only arm comparisons and adjudicate saved replay findings offline."""

import argparse
import hashlib
import json
import os
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from time import monotonic
from typing import Any, Literal, get_args

from loop_reviewer.reviewer.integrations.git_hunks import decode_hunks
from loop_reviewer.reviewer.integrations.git_inventory import open_local_comparison
from loop_reviewer.reviewer.integrations.openai_profiles import QUALIFIED_PROFILE
from loop_reviewer.reviewer.integrations.openai_review import (
    OpenAIReview,
    provider_schema,
)
from loop_reviewer.reviewer.integrations.python_analyzer import CpythonAnalyzer
from loop_reviewer.reviewer.inventory import validated_inventory
from loop_reviewer.reviewer.inventory_contract import ChangedFile, PinnedComparison
from loop_reviewer.reviewer.local_checks import run_local_checks
from loop_reviewer.reviewer.local_evidence import (
    EvidenceIndex,
    LocalEvidenceWork,
    WorkLimit,
)
from loop_reviewer.reviewer.model_budget import MAX_REPLAY_SPEND, SpendLedger
from loop_reviewer.reviewer.model_contract import ModelKind, ReviewModel
from loop_reviewer.reviewer.semantic_batch import (
    FULL_INVESTIGATION_PROMPT,
    INVESTIGATION_PROMPT,
    VERIFICATION_PROMPT,
    run_full_batches,
    run_semantic_batch,
)
from loop_reviewer.reviewer.semantic_inputs import (
    FrozenHunk,
    build_diff_bundle,
    build_targeted_bundle,
)
from loop_reviewer.reviewer.semantic_limits import (
    VERIFY_EVIDENCE,
    VERIFY_OUTPUT,
    VERIFY_PROMPT,
    MeteredSource,
    SemanticLimits,
)
from tests.support.inventory_git import PATH_ENV
from tests.support.legacy_replay import paired_gate, read_legacy_replay
from tests.support.openai_profile import FIXTURE_PROFILE, PILOT_PROFILE
from tests.support.reviewer_replay import (
    FIXTURES,
    _file,
    _json,
    build_replay,
    replay_manifest,
)

Arm = Literal["diff", "targeted"]


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _lock(mode: str, policy: Literal[3, 5]) -> dict[str, object]:
    limits = SemanticLimits(0)
    lock: dict[str, object] = {
        "mode": mode,
        "policy_version": 1 if policy == 3 else 3,
        "profile": (
            asdict(PILOT_PROFILE)
            if policy == 3 or mode == "fixtures"
            else None
            if QUALIFIED_PROFILE is None
            else {
                **asdict(QUALIFIED_PROFILE),
                "qualified_on": QUALIFIED_PROFILE.qualified_on.isoformat(),
            }
        ),
        "tier": "default",
        "store": False,
        "stream": False,
        "n": 1,
        "ceiling_usd_micros": MAX_REPLAY_SPEND,
        "ceilings": {
            "evidence": limits.evidence_limit,
            "prompt": limits.prompt_limit,
            "combined": limits.combined_limit,
            "output": limits.output_limit,
        },
        "protected_verification": {
            "evidence": VERIFY_EVIDENCE,
            "prompt": VERIFY_PROMPT,
            "output": VERIFY_OUTPUT,
        },
        "schemas": {
            kind: _digest(_json(provider_schema(kind))) for kind in get_args(ModelKind)
        },
        "prompts": {
            "investigation": _digest(INVESTIGATION_PROMPT.encode()),
            "verification": _digest(VERIFICATION_PROMPT.encode()),
        },
        "manifest_sha256": _digest((FIXTURES / "manifest.json").read_bytes()),
    }
    if policy == 5:
        lock["report_version"] = 5
        lock["full_investigation_sha256"] = _digest(FULL_INVESTIGATION_PROMPT.encode())
        lock["manifest_v5_extra_sha256"] = _digest(
            (FIXTURES / "manifest-v5-extra.json").read_bytes()
        )
    return lock


def _read_json(path: Path, limit: int = 256 * 1024) -> Any:
    with path.open("rb") as stream:
        payload = stream.read(limit + 1)
    if len(payload) > limit:
        raise ValueError(f"Input exceeds its byte limit: {path.name}")
    return json.loads(payload)


def _fixture_model(responses: list[dict[str, Any]]) -> ReviewModel:
    pending = iter(responses)

    def transport(
        body: bytes, key: str, timeout: float, limit: int
    ) -> tuple[int, bytes]:
        answer = next(pending, None)
        if answer is None:
            raise ValueError("Fixture omitted this attempt")
        if answer.get("transport_error") == "timeout":
            raise TimeoutError
        if "http_status" in answer:
            return int(answer["http_status"]), b"{}"
        content = answer.get("raw_content")
        if content is None:
            content = _json(answer.get("reply")).decode()
        envelope = {
            "object": "chat.completion",
            "model": PILOT_PROFILE.profile,
            "service_tier": "default",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": answer.get("finish_reason", "stop"),
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "refusal": answer.get("refusal"),
                    },
                }
            ],
            "usage": answer.get(
                "usage",
                {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
            ),
        }
        return 200, _json(envelope)

    return OpenAIReview("offline fixture", transport, profile=FIXTURE_PROFILE)


def _frozen(
    rows: tuple[ChangedFile, ...], data: bytes
) -> tuple[tuple[FrozenHunk, ...], tuple[int, ...], dict[str, Any]]:
    artifact = json.loads(data)
    indexes = {_json(_file(row)): index for index, row in enumerate(rows)}
    files = tuple(indexes[_json(file)] for file in artifact["files"])
    ordinals: Counter[int] = Counter()
    hunks = []
    for entry in artifact["hunks"]:
        index = int(entry["file"])
        (hunk,) = decode_hunks(entry["patch"].encode())
        hunks.append(FrozenHunk(index, ordinals[index], hunk))
        ordinals[index] += 1
    return (
        tuple(hunks),
        files,
        {"hunks": artifact["omissions"], "omitted_paths": artifact["omitted_paths"]},
    )


def _arm(
    repo: Path,
    pins: PinnedComparison,
    arm: Arm,
    data: bytes,
    model: ReviewModel,
    *,
    frozen_retrieval_bytes: int,
    policy: Literal[3, 5] = 3,
) -> dict[str, Any]:
    with open_local_comparison(repo, pins, PATH_ENV) as source:
        rows = validated_inventory(source, pins)
        started = monotonic()
        ledger = SpendLedger(
            MAX_REPLAY_SPEND,
            monotonic,
            started_at=started,
            replay=True,
            multiple_investigations=policy == 5,
        )
        limits = SemanticLimits(
            ledger.deadline,
            preparing_investigation=True,
            preserve_verification_reserve=policy == 5,
        )
        index = EvidenceIndex()
        frozen_omissions = None
        selected: tuple[str, ...] = ()
        report: dict[str, Any]
        try:
            work = LocalEvidenceWork(
                MeteredSource(source, limits, monotonic),
                CpythonAnalyzer(),
                pins,
                monotonic,
                ledger.deadline,
                utf8_only=policy == 5,
            )
            bundle = None
            if arm == "targeted" and policy == 3:
                bundle = build_targeted_bundle(
                    pins,
                    rows,
                    work,
                    index,
                    limits.preparation_limit(investigation=True),
                )
            elif arm == "diff":
                limits.retrieved(frozen_retrieval_bytes)
                hunks, file_indices, frozen_omissions = _frozen(rows, data)
                try:
                    bundle = build_diff_bundle(
                        pins,
                        rows,
                        data,
                        hunks,
                        limits.preparation_limit(investigation=True),
                        file_indices=file_indices,
                        full=policy == 5,
                        incomplete_ids=frozenset(
                            f"p{file_indices[item['file']] + 1:05d}"
                            for item in (
                                frozen_omissions["hunks"] if policy == 5 else ()
                            )
                        ),
                    )
                except ValueError as error:
                    if str(error) != "Frozen evidence exceeds byte limit":
                        raise
                    raise WorkLimit from error
            if policy == 5:
                batch = run_full_batches(
                    rows, work, index, model, ledger, limits, frozen=bundle
                )
                selected, omissions = batch.selected_ids, batch.omissions
            else:
                assert bundle is not None
                selected, omissions = bundle.selected_ids, bundle.omissions
                limits.preparing_investigation = False
                batch = run_semantic_batch(
                    bundle,
                    source,
                    model,
                    ledger,
                    limits,
                    monotonic,
                    analyzer=CpythonAnalyzer(),
                    evidence=index,
                )
            file_paths = {
                f"p{i:05d}": file.path
                for i, row in enumerate(rows, 1)
                if (file := row.new or row.old) is not None
            }
            report = {
                "stop": batch.stop,
                "selected_paths": [file_paths[key] for key in selected],
                "reviewed_paths": [file_paths[key] for key in batch.reviewed_ids],
                "gaps": batch.gaps,
                "omissions": [asdict(item) for item in omissions],
                "findings": [
                    {
                        "id": f.id,
                        "file_id": f.file_id,
                        "path": file_paths[f.file_id],
                        "claim": f.candidate.model_dump(),
                        "evidence_ids": f.evidence_ids,
                    }
                    for f in batch.findings
                ],
                "private_leads": [
                    {
                        "id": lead.id,
                        "file_id": lead.file_id,
                        "missing_fact": lead.missing_fact,
                    }
                    for lead in batch.leads
                ],
                "discarded_candidates": [asdict(item) for item in batch.discards],
                "evidence": [entry.model_dump() for entry in batch.evidence],
            }
        except WorkLimit:
            report = {
                "stop": "work_limit",
                "selected_paths": [],
                "reviewed_paths": [],
                "gaps": {
                    f"p{i:05d}": "work_limit"
                    for i, row in enumerate(rows, 1)
                    if not row.binary
                },
                "omissions": [],
                "findings": [],
                "private_leads": [],
                "discarded_candidates": [],
                "evidence": [],
            }
        return report | {
            "frozen_omissions": frozen_omissions,
            "coverage_count": len(rows),
            "inventory_paths": {
                f"p{i:05d}": file.path
                for i, row in enumerate(rows, 1)
                if (file := row.new or row.old) is not None
            },
            "usage": asdict(ledger.snapshot()),
            "limits": asdict(limits),
            "elapsed_seconds": monotonic() - started,
        }


def _compiler(repo: Path, pins: PinnedComparison) -> list[dict[str, Any]]:
    with open_local_comparison(repo, pins, PATH_ENV) as source:
        rows = validated_inventory(source, pins)
        work = LocalEvidenceWork(
            source, CpythonAnalyzer(), pins, monotonic, monotonic() + 30
        )
        _, _, findings, _ = run_local_checks(rows, work, retrieve_context=False)
        return [finding.model_dump() for finding in findings]


def run_replay(
    replay_dir: Path,
    output: Path,
    split: str,
    fixture_responses: Path | None,
    *,
    live: bool = False,
    policy: Literal[3, 5] = 3,
    legacy_results: Path | None = None,
) -> dict[str, Any]:
    if split not in ("development", "holdout") or (fixture_responses is None) == (
        not live
    ):
        raise ValueError("Choose one split and exactly one replay mode")
    if not replay_dir.is_dir():
        raise ValueError("Prepare the authored replay directory before scoring it")
    manifest = replay_manifest(policy)
    if legacy_results is not None and policy != 5:
        raise ValueError("Legacy observations require --policy 5")
    legacy = read_legacy_replay(legacy_results, manifest) if legacy_results else {}
    measured_live = live and policy == 5 and QUALIFIED_PROFILE is not None
    api_key = os.environ.get("OPENAI_API_KEY", "") if measured_live else ""
    if measured_live and not api_key:
        raise ValueError("Live replay with a qualified profile requires OPENAI_API_KEY")
    if (
        measured_live
        and split == "holdout"
        and not all(
            row["id"] in legacy for row in manifest if row["split"] == "holdout"
        )
    ):
        raise ValueError("The live holdout requires a legacy observation for each case")
    mode = (
        "fixtures"
        if fixture_responses is not None
        else "live"
        if measured_live
        else "unqualified_live"
    )
    configuration = _lock(mode, policy)
    lockfile = replay_dir / "score-config.lock.json"
    if lockfile.exists():
        if _read_json(lockfile) != configuration:
            raise ValueError("Replay configuration differs from the development lock")
    elif split == "holdout":
        raise ValueError("Lock the configuration on development before running holdout")
    build_replay(replay_dir, policy=policy)
    cases: list[dict[str, Any]] = []
    for case in manifest:
        if case["split"] != split:
            continue
        case_id = case["id"]
        artifact = replay_dir / "diff-only" / f"{case_id}.json"
        with artifact.open("rb") as stream:
            data = stream.read(64 * 1024 + 1)
        if len(data) > 64 * 1024 or _digest(data) != case["input_sha256"]:
            raise ValueError("Frozen diff digest mismatch")
        repo = replay_dir / "repos" / case_id
        pins = PinnedComparison(merge_base=case["merge_base"], head=case["head"])
        responses = (
            _read_json(fixture_responses / f"{case_id}.json")
            if fixture_responses is not None
            else None
        )

        def model(arm: Arm, script: dict[str, Any] | None = responses) -> ReviewModel:
            return (
                OpenAIReview(
                    api_key, profile=QUALIFIED_PROFILE if measured_live else None
                )
                if script is None
                else _fixture_model(script[arm])
            )

        diff = _arm(
            repo,
            pins,
            "diff",
            data,
            model("diff"),
            frozen_retrieval_bytes=case["diff_retrieval_bytes"],
            policy=policy,
        )
        targeted = _arm(
            repo,
            pins,
            "targeted",
            data,
            model("targeted"),
            frozen_retrieval_bytes=0,
            policy=policy,
        )
        cases.append(
            {
                "case": case_id,
                "pins": pins.model_dump(),
                "legacy_status": case["legacy_status"],
                "frozen_sha256": case["input_sha256"],
                "compiler_findings": _compiler(repo, pins),
                "diff": diff,
                "targeted": targeted,
                "reviewed_intersection": sorted(
                    set(diff["reviewed_paths"]) & set(targeted["reviewed_paths"])
                ),
                "large_case": case_id == "generated-churn",
            }
        )
        if policy == 5:
            cases[-1]["legacy"] = legacy.get(case_id)
    report = {
        "split": split,
        "configuration": configuration,
        "cases": cases,
        "measured_live": measured_live,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "runs.json").write_bytes(_json(report))
    if split == "development" and not lockfile.exists():
        with lockfile.open("xb") as stream:
            stream.write(_json(configuration))
    return report


def adjudicate(
    report: dict[str, Any], adjudications: list[dict[str, Any]]
) -> dict[str, Any]:
    labels = _read_json(FIXTURES / "labels.json")
    policy5 = report.get("configuration", {}).get("report_version") == 5
    if policy5:
        labels.update(_read_json(FIXTURES / "labels-v5-extra.json"))
    index = {
        (item["case"], item["arm"], item["candidate_id"]): item
        for item in adjudications
    }
    if len(index) != len(adjudications):
        raise ValueError("Duplicate candidate adjudication")
    consumed = set()
    summaries: list[dict[str, Any]] = []
    for case in report["cases"]:
        name = case["case"]
        compiler_paths = {
            f["anchor"]["path"]
            for f in case["compiler_findings"]
            if f["anchor"] is not None
        }
        expected = {
            item["id"]
            for item in labels[name]
            if item["path"] not in compiler_paths or item["id"] != "invalid-break"
        }
        arms = (
            ("diff", "targeted", "legacy")
            if case.get("legacy") is not None
            else ("diff", "targeted")
        )
        for arm in arms:
            run = case[arm]
            matches: set[str] = set()
            duplicates = false = unadjudicated = 0
            for candidate in run["findings"]:
                key = name, arm, candidate["id"]
                item = index.get(key)
                if item is None:
                    unadjudicated += 1
                    continue
                consumed.add(key)
                verdict = item["verdict"]
                label = item.get("label")
                rationale = item.get("rationale")
                if verdict in ("match", "duplicate") and (
                    not isinstance(label, str) or label not in expected
                ):
                    raise ValueError("Unknown label for this arm and case")
                if verdict == "match" and isinstance(label, str):
                    if label in matches:
                        raise ValueError("Mark repeat matches as duplicates")
                    matches.add(label)
                elif verdict == "duplicate":
                    duplicates += 1
                elif (
                    verdict == "false_finding"
                    and isinstance(rationale, str)
                    and rationale.strip()
                ):
                    false += 1
                else:
                    raise ValueError(
                        "False findings require a pinned-evidence rationale"
                    )
            if any(
                row.get("label") not in matches
                for key in consumed
                if key[:2] == (name, arm)
                and (row := index[key])["verdict"] == "duplicate"
            ):
                raise ValueError("A duplicate label must have an adjudicated original")
            measurable = not unadjudicated and (
                run["outcome"] == "reviewed"
                if arm == "legacy"
                else bool(run["reviewed_paths"])
                and run["stop"] not in ("unsupported_model", "accounting_fault")
                and not run["usage"].get("accounting_fault")
            )
            summaries.append(
                {
                    "case": name,
                    "arm": arm,
                    "large_case": case["large_case"],
                    "matches": sorted(matches) if measurable else None,
                    "misses": sorted(expected - matches) if measurable else None,
                    "precision": len(matches) / (len(matches) + false)
                    if measurable and len(matches) + false
                    else None,
                    "false_findings": false if measurable else None,
                    "duplicates": duplicates if measurable else None,
                    "unadjudicated": unadjudicated,
                    "gaps": run.get("gaps", {}),
                    "usage": run.get("usage"),
                    "elapsed_seconds": run.get("elapsed_seconds"),
                    **({"legacy_outcome": run["outcome"]} if arm == "legacy" else {}),
                }
            )
    if consumed != index.keys():
        raise ValueError("Adjudication does not name a saved finding in this split")
    return {
        "split": report["split"],
        "runs": summaries,
        **paired_gate(
            summaries,
            measured=policy5
            and report["split"] == "holdout"
            and report.get("measured_live") is True,
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--replay-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--split", choices=("development", "holdout"), required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--live",
        action="store_true",
        help="Authorize live requests for the authored corpus under the qualified v5 profile, if available",
    )
    mode.add_argument("--fixture-responses", type=Path)
    parser.add_argument("--adjudications", type=Path)
    parser.add_argument("--policy", type=int, choices=(3, 5), default=3)
    parser.add_argument("--legacy-results", type=Path)
    args = parser.parse_args()
    if not (
        args.live
        or args.fixture_responses is not None
        or args.adjudications is not None
    ):
        parser.error("Choose a run mode or provide offline adjudications.")
    try:
        if args.live or args.fixture_responses is not None:
            report = run_replay(
                args.replay_dir,
                args.output,
                args.split,
                args.fixture_responses,
                live=args.live,
                policy=args.policy,
                legacy_results=args.legacy_results,
            )
        else:
            report = _read_json(args.output / "runs.json", 4 * 1024 * 1024)
            if report["split"] != args.split:
                raise ValueError(
                    "Adjudications and saved results must use the same split"
                )
        if args.adjudications is not None:
            scored = adjudicate(report, _read_json(args.adjudications))
            (args.output / "scores.json").write_bytes(_json(scored))
    except (KeyError, TypeError, OSError, ValueError) as error:
        parser.exit(1, f"Replay scoring failed: {error}\n")


if __name__ == "__main__":
    main()
