"""Validate pre-cutover observations and compare adjudicated holdout runs."""

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import Field, model_validator

from loop_reviewer.reviewer.inventory_contract import (
    _ObjectId,
    _Path,
    _Record,
    canonical_path,
)
from loop_reviewer.reviewer.model_contract import strict_json

_Text = Annotated[str, Field(min_length=1, max_length=1024)]


class LegacyFinding(_Record):
    condition: _Text
    consequence: _Text
    severity: Literal["high", "medium", "low"]
    path: _Path | None
    line: Annotated[int, Field(gt=0, le=2**31 - 1)] | None

    @model_validator(mode="after")
    def valid_location(self) -> Self:
        if self.path is not None:
            canonical_path(self.path)
        if self.path is None and self.line is not None:
            raise ValueError("A legacy line requires a path")
        if not self.condition.strip() or not self.consequence.strip():
            raise ValueError("Expected a nonblank legacy claim")
        return self


class LegacyCase(_Record):
    case: Annotated[str, Field(pattern=r"^[a-z0-9-]{1,64}$")]
    merge_base: _ObjectId
    head: _ObjectId
    checkout_commit: _ObjectId
    model_id: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_.:/@-]{1,128}$")]
    outcome: Literal["reviewed", "patch_limit", "failed"]
    findings: Annotated[tuple[LegacyFinding, ...], Field(max_length=32)]

    @model_validator(mode="after")
    def findings_only_on_reviewed(self) -> Self:
        if self.outcome != "reviewed" and self.findings:
            raise ValueError("Unsupported legacy outcomes cannot contain findings")
        return self


class LegacyReplayV1(_Record):
    schema_version: Annotated[int, Field(ge=1, le=1)]
    cases: Annotated[tuple[LegacyCase, ...], Field(min_length=1, max_length=100)]


def read_legacy_replay(
    path: Path, manifest: Iterable[Mapping[str, object]]
) -> dict[str, dict[str, Any]]:
    cap = 512 * 1024
    with path.open("rb") as stream:
        data = stream.read(cap + 1)
    if len(data) > cap:
        raise ValueError("Legacy replay exceeds its byte limit")
    strict_json(data)
    replay = LegacyReplayV1.model_validate_json(data)
    entries = {row["id"]: row for row in manifest}
    results: dict[str, dict[str, Any]] = {}
    for case in replay.cases:
        if case.case in results:
            raise ValueError("Duplicate legacy replay case")
        expected = entries.get(case.case)
        if expected is None or (expected["merge_base"], expected["head"]) != (
            case.merge_base,
            case.head,
        ):
            raise ValueError(f"Mismatched case or pins in legacy replay: {case.case}")
        eligibility = expected["legacy_status"]
        if (case.outcome == "reviewed" and eligibility != "eligible") or (
            case.outcome == "patch_limit" and eligibility != "unsupported_size"
        ):
            raise ValueError(
                f"Legacy outcome disagrees with patch eligibility: {case.case}"
            )
        results[case.case] = case.model_dump(exclude={"findings"}) | {
            "findings": [
                {"id": f"l{i:05d}", **f.model_dump()}
                for i, f in enumerate(case.findings, 1)
            ]
        }
    return results


def _cost(runs: list[dict[str, Any]]) -> int:
    return sum(
        sum(
            row["usage"][key]
            for key in ("estimated_cost_usd_micros", "unaccounted_reserved_usd_micros")
        )
        for row in runs
    )


def paired_gate(runs: list[dict[str, Any]], *, measured: bool) -> dict[str, object]:
    by_arm = {
        arm: {r["case"]: r for r in runs if r["arm"] == arm}
        for arm in ("legacy", "diff", "targeted")
    }
    legacy, diff, targeted = (by_arm[arm] for arm in ("legacy", "diff", "targeted"))
    supported = {
        name
        for name, row in legacy.items()
        if row.get("legacy_outcome") != "patch_limit"
    }
    if (
        not measured
        or not supported
        or not supported <= targeted.keys() & diff.keys()
        or not targeted.keys() <= legacy.keys()
        or any(
            arm[name]["matches"] is None
            for name in supported
            for arm in (legacy, diff, targeted)
        )
    ):
        return {"passes_pilot": False, "pilot_status": "not_measured"}
    observed = [row for row in targeted.values() if row["matches"] is not None]
    matched_count = sum(len(row["matches"]) for row in observed)
    false_count = sum(row["false_findings"] for row in observed)
    precision = (
        matched_count / (matched_count + false_count)
        if matched_count + false_count
        else None
    )
    preserves_legacy = all(
        set(legacy[n]["matches"]) <= set(targeted[n]["matches"]) for n in supported
    )
    improves_legacy = any(
        set(legacy[n]["matches"]) < set(targeted[n]["matches"]) for n in supported
    ) or sum(
        targeted[n]["false_findings"] + targeted[n]["duplicates"] for n in supported
    ) < sum(legacy[n]["false_findings"] + legacy[n]["duplicates"] for n in supported)
    paired = sorted(
        n
        for n in targeted.keys() & diff.keys()
        if targeted[n]["matches"] is not None and diff[n]["matches"] is not None
    )
    preserves_diff = bool(paired) and all(
        set(diff[n]["matches"]) <= set(targeted[n]["matches"]) for n in paired
    )
    improves_diff = any(
        set(diff[n]["matches"]) < set(targeted[n]["matches"]) for n in paired
    )
    targeted_cost, diff_cost = (
        _cost([arm[n] for n in paired]) for arm in (targeted, diff)
    )
    checks = {
        "precision": precision is not None and precision >= 0.8,
        "false_findings_per_pr": all(row["false_findings"] <= 1 for row in observed),
        "legacy_nonregression_and_improvement": preserves_legacy and improves_legacy,
        "diff_efficiency": preserves_diff
        and (
            (improves_diff and targeted_cost <= diff_cost) or targeted_cost < diff_cost
        ),
    }
    passes = all(checks.values())
    return {
        "passes_pilot": passes,
        "pilot_status": "passed" if passes else "failed",
        "criteria": checks,
        "targeted_precision": precision,
    }
