import json
from collections.abc import Callable
from pathlib import Path
from typing import override

import pytest

from loop_reviewer.reviewer.discussion_contract import DiscussionSnapshotV1
from loop_reviewer.reviewer.full_review_v6 import (
    FullReviewRequestV6,
    FullReviewResultV6,
)
from loop_reviewer.reviewer.integrations.git_inventory import open_local_comparison
from loop_reviewer.reviewer.inventory_contract import InventoryBudget, PinnedComparison
from loop_reviewer.reviewer.model_contract import (
    CallQuote,
    ModelAttempt,
    ModelCall,
    TokenUsage,
    parse_reply,
)
from loop_reviewer.reviewer.semantic_review import review_full_v6
from tests.support.discussion import snapshot_data
from tests.support.inventory_git import PATH_ENV, commit, init, revision
from tests.test_semantic_batch import ScriptedModel


def review(
    path: Path,
    *,
    model: ScriptedModel | None,
    snapshot: DiscussionSnapshotV1 | None = None,
    monotonic: Callable[[], float] = lambda: 0,
) -> FullReviewResultV6:
    report = review_full_v6(
        FullReviewRequestV6(
            schema_version=6,
            scope="full_review",
            comparison=PinnedComparison(
                merge_base=revision(path, "HEAD^"), head=revision(path, "HEAD")
            ),
            budget=InventoryBudget(max_model_spend_usd_micros=100),
            discussion=snapshot,
        ),
        lambda pins: open_local_comparison(path, pins, PATH_ENV),
        model,
        monotonic,
    )
    return FullReviewResultV6.model_validate_json(report.model_dump_json())


class PinnedClaimModel(ScriptedModel):
    def __init__(self, case: str, *, assessment: str = "supports") -> None:
        super().__init__(assessment=assessment)
        self.case = case
        self.first_edit: str | None = None

    @override
    def send(
        self, call: ModelCall, quote: CallQuote, *, timeout_seconds: float
    ) -> ModelAttempt:
        self.calls.append(call)
        assert "PRIVATE_CANARY" not in call.prompt
        assert "python_witness" not in call.prompt
        if self.case == "later-unit-stop" and call.unit_id == "i00002":
            return ModelAttempt("refused", TokenUsage(1, 1, 2), output_bytes=4)
        if call.kind == "verification":
            if self.case == "later-stop" and call.unit_id == "c00002":
                return ModelAttempt("refused", TokenUsage(1, 1, 2), output_bytes=4)
            entries = json.loads(call.prompt.rsplit("\n", 1)[1])
            if self.case == "consumer":
                assert any(
                    e["role"] == "text_match" and e["range"]["path"] == "consumer"
                    for e in entries
                )
            response = {
                "schema_version": 1,
                "candidate_id": call.unit_id,
                "assessment": self.assessment,
                "evidence_ids": [e["id"] for e in entries],
                "explanation": "The pinned consumer supports this assessment.",
            }
        else:
            payload = json.loads(call.prompt.split("\n\n", 1)[1])
            response = {
                "schema_version": 1,
                "kind": "submit_candidates",
                "investigation_id": call.unit_id,
                "reviewed_file_ids": payload["selected_file_ids"],
                "candidates": [],
            }
            if self.case != "clean":
                changed = {
                    entry["range"]["side"]: entry["id"]
                    for entry in reversed(payload["changed_excerpts"])
                }
                absent = {
                    "addition": "base",
                    "deletion": "head",
                    "retained-deletion": "head",
                }.get(self.case)
                if absent is not None:
                    changed[absent] = next(
                        entry["id"]
                        for entry in payload["supporting_excerpts"]
                        if entry["range"]["side"] == absent
                        and entry["range"]["path"] == "consumer"
                    )
                base, head = changed["base"], changed["head"]
                trigger = head
                if self.case == "consumer":
                    trigger = next(
                        e["id"]
                        for e in payload["supporting_excerpts"]
                        if e["range"]["side"] == "head"
                        and e["range"]["path"] == "consumer"
                    )
                edit = {
                    "deletion": base,
                    "retained-deletion": base,
                    "wrong-side": base,
                    "unseen": "e99999",
                }.get(self.case, head)
                if self.case == "cross-packet":
                    if self.first_edit is None:
                        self.first_edit = head
                    else:
                        edit = self.first_edit
                        assert edit not in {
                            e["id"]
                            for e in payload["changed_excerpts"]
                            + payload["supporting_excerpts"]
                        }
                count = {"overflow": 3, "later-stop": 2}.get(self.case, 1)
                for n in range(count):
                    response["candidates"].append(
                        {
                            "file_id": "p00001",
                            "changed_behavior": "The endpoint changes.",
                            "trigger": f"Use endpoint in consumer case {n}.",
                            "consequence": "The consumer receives the wrong endpoint.",
                            "proposed_impact": "core_workflow_outage"
                            if self.case == "overflow"
                            and call.unit_id == "i00002"
                            and n == 0
                            else "material_incorrect_behavior",
                            "proposed_anchor_id": base
                            if self.case in {"deletion", "retained-deletion"}
                            else head,
                            "citations": [
                                {"role": role, "evidence_id": identifier}
                                for role, identifier in (
                                    ("base_behavior", base),
                                    ("head_change", edit),
                                    ("trigger", trigger),
                                    ("consequence", trigger),
                                    ("impact", trigger),
                                )
                            ],
                        }
                    )
        raw = json.dumps(response)
        return ModelAttempt(
            "success", TokenUsage(1, 1, 2), parse_reply(call, raw), len(raw)
        )


@pytest.mark.parametrize("extension", ["py", "ts", "go"])
@pytest.mark.parametrize("assessment", ["supports", "contradicts"])
def test_pinned_report_checks_changed_text_and_consumer(
    tmp_path: Path, extension: str, assessment: str
) -> None:
    init(tmp_path)
    declarations = {
        "py": "endpoint = {}",
        "ts": "export const endpoint = {};",
        "go": "const endpoint = {}",
    }
    declaration = declarations[extension]
    changed = tmp_path / f"config.{extension}"
    changed.write_text(declaration.format(1) + "\n")
    consumer = tmp_path / "consumer"
    consumer.write_text(
        "send(endpoint)\n"
        if assessment == "supports"
        else "send(normalize(endpoint, 2, 1))\n"
    )
    commit(tmp_path)
    changed.write_text(declaration.format(2) + "\n")
    commit(tmp_path)
    model = PinnedClaimModel("consumer", assessment=assessment)
    result = review(tmp_path, model=model).root
    assert result.status == "complete"
    assert [c.kind for c in model.calls] == ["investigation", "verification"]
    (work,) = result.investigation_coverage
    if assessment == "supports":
        (finding,) = result.findings
        assert len(finding.evidence_ids) >= 3
        assert finding.anchor is not None
        assert (
            finding.anchor.side,
            finding.anchor.path,
            finding.anchor.start_line,
        ) == ("head", changed.name, 1)
        assert work.discarded_candidates == ()
    else:
        assert result.findings == ()
        assert [d.reason for d in work.discarded_candidates] == ["contradicted"]


@pytest.mark.parametrize(
    "blob_bytes",
    [93_620, 45_000, 40_960],
    ids=["one-blob-too-large", "both-blobs-too-large", "patch-uses-headroom"],
)
def test_unreviewable_blob_does_not_starve_the_next_file(
    tmp_path: Path, blob_bytes: int
) -> None:
    init(tmp_path)
    large, small = tmp_path / "a.ts", tmp_path / "z.ts"
    for value in (1, 2):
        edit = f"const endpoint = {value};\n"
        filler = "// " + "x" * 100 + "\n"
        lines, tail = divmod(blob_bytes - len(edit), len(filler))
        large.write_text(edit + filler * lines + filler[:tail])
        small.write_text(f"const port = {value};\n")
        commit(tmp_path)

    model = PinnedClaimModel("clean")
    result = review(tmp_path, model=model).root
    assert result.status == "partial"
    first, second = result.file_coverage
    assert first.new and first.new.path == "a.ts" and not first.oversized
    assert (first.outcome, first.reason) == ("unfinished", "work_limit")
    assert second.new and second.new.path == "z.ts"
    assert (second.outcome, second.reason) == ("complete", None)
    assert [call.kind for call in model.calls] == ["investigation"]


def test_one_sided_patch_over_cap_does_not_starve_next_file(tmp_path: Path) -> None:
    init(tmp_path)
    small = tmp_path / "z.ts"
    filler = "// " + "x" * 96 + "\n"
    small.write_text("const port = 1;\n" + filler * 102)
    commit(tmp_path)
    (tmp_path / "a.ts").write_text(filler * 665)
    small.write_text("const port = 2;\n" + filler * 102)
    commit(tmp_path)

    model = PinnedClaimModel("clean")
    result = review(tmp_path, model=model).root
    first, second = result.file_coverage
    assert (first.outcome, first.reason) == ("unfinished", "work_limit")
    assert (second.outcome, second.reason) == ("complete", None)
    assert [call.kind for call in model.calls] == ["investigation"]


def test_large_optional_reference_does_not_starve_verification(tmp_path: Path) -> None:
    init(tmp_path)
    changed = tmp_path / "config.ts"
    changed.write_text("const endpoint = 1;\n")
    (tmp_path / "consumer").write_text("send(endpoint)\n" + "// filler\n" * 14_000)
    commit(tmp_path)
    changed.write_text("const endpoint = 2;\n")
    commit(tmp_path)

    model = PinnedClaimModel("changed-excerpts-only")
    result = review(tmp_path, model=model).root
    assert result.status == "complete"
    assert [call.kind for call in model.calls] == ["investigation", "verification"]
    assert len(result.findings) == 1
    assert not result.private_leads


@pytest.mark.parametrize("case", ["addition", "deletion", "retained-deletion"])
def test_pinned_report_verifies_both_sides_of_additions_and_removals(
    tmp_path: Path, case: str
) -> None:
    init(tmp_path)
    (tmp_path / "consumer").write_text("send(endpoint)\n")
    file = tmp_path / "config.ts"
    if case == "deletion":
        file.write_text("const endpoint = 1;\n")
    elif case == "retained-deletion":
        file.write_text("const endpoint = 1;\nexport const retained = true;\n")
    commit(tmp_path)
    if case == "deletion":
        file.unlink()
    elif case == "retained-deletion":
        file.write_text("export const retained = true;\n")
    else:
        file.write_text("const endpoint = 2;\n")
    commit(tmp_path)
    model = PinnedClaimModel(case)
    result = review(tmp_path, model=model).root
    assert result.status == "complete"
    (finding,) = result.findings
    cited = {
        entry.range.side
        for entry in result.evidence_index
        if entry.id in finding.evidence_ids
    }
    assert cited == {"base", "head"}
    if case in {"deletion", "retained-deletion"}:
        assert finding.anchor is None
    else:
        assert finding.anchor is not None and finding.anchor.side == "head"


@pytest.mark.parametrize(
    "case",
    [
        "wrong-side",
        "unseen",
        "cross-packet",
        "oversized",
        "later-stop",
        "later-unit-stop",
        "overflow",
    ],
)
def test_pinned_report_keeps_verification_bounded_and_fail_closed(
    tmp_path: Path, case: str
) -> None:
    init(tmp_path)
    file = tmp_path / "config.ts"

    def text(value: str) -> str:
        if case == "oversized":
            return f'const endpoint = "{value * 900}";\n'
        if case in {"overflow", "cross-packet", "later-unit-stop"}:
            return "".join(
                f'const setting{i:02d} = "{value * 140}";\n' for i in range(15)
            )
        return f'const endpoint = "{value}";\n'

    file.write_text(text("a"))
    commit(tmp_path)
    file.write_text(text("b"))
    commit(tmp_path)
    model = PinnedClaimModel(
        case, assessment="contradicts" if case == "overflow" else "supports"
    )
    result = review(tmp_path, model=model).root
    assert result.status == "partial"
    (work,) = result.investigation_coverage
    if case == "overflow":
        assert result.stop_reason == work.reason == "work_limit"
        assert [d.id for d in work.discarded_candidates] == [
            "c00004",
            "c00001",
            "c00002",
            "c00003",
        ]
        assert [c.kind for c in model.calls] == ["investigation"] * 2 + [
            "verification"
        ] * 4
        assert result.findings == result.private_leads == ()
    elif case == "later-unit-stop":
        assert work.reason == "refused"
        assert result.private_leads == ()
        assert [f.id for f in result.findings] == ["c00001"]
    elif case == "cross-packet":
        assert work.reason == "missing_evidence"
        assert [f.id for f in result.findings] == ["c00001"]
        (lead,) = result.private_leads
        assert "does not resolve" in lead.missing_fact
        assert model.first_edit not in lead.evidence_ids
        assert [c.unit_id for c in model.calls] == ["i00001", "i00002", "c00001"]
    else:
        assert len(result.private_leads) == 1
        if case == "later-stop":
            assert work.reason == "refused"
            assert [f.id for f in result.findings] == ["c00001"]
        else:
            assert work.reason == "missing_evidence"
            assert result.findings == ()
            assert [c.kind for c in model.calls] == ["investigation"]


def test_slow_later_investigation_leaves_time_to_verify(tmp_path: Path) -> None:
    init(tmp_path)
    file = tmp_path / "config.ts"
    for value in ("a", "b"):
        file.write_text(
            "".join(f'const setting{i:02d} = "{value * 140}";\n' for i in range(15))
        )
        commit(tmp_path)
    now = 0.0

    class SlowSecondPacket(PinnedClaimModel):
        @override
        def quote(self, call: ModelCall) -> CallQuote:
            nonlocal now
            if now == 0:
                now = (
                    79.0  # Most of the deadline has passed before investigation starts.
                )
            return super().quote(call)

        @override
        def send(
            self, call: ModelCall, quote: CallQuote, *, timeout_seconds: float
        ) -> ModelAttempt:
            nonlocal now
            if call.unit_id == "i00002":
                self.calls.append(call)
                now += timeout_seconds
                return ModelAttempt("transient_error")
            if call.unit_id == "i00001":
                assert timeout_seconds >= 19
                now += 19
            else:
                assert timeout_seconds == 20
            return super().send(call, quote, timeout_seconds=timeout_seconds)

    model = SlowSecondPacket("later-unit-stop")
    result = review(tmp_path, model=model, monotonic=lambda: now).root
    assert result.status == "partial"
    assert [finding.id for finding in result.findings] == ["c00001"]
    assert [call.unit_id for call in model.calls] == ["i00001", "i00002", "c00001"]


@pytest.mark.parametrize(
    "with_model", [False, True], ids=["without-model", "with-model"]
)
@pytest.mark.parametrize("case", ["unique", "ambiguous", "unavailable"])
def test_pinned_report_maps_discussion_privately(
    tmp_path: Path, with_model: bool, case: str
) -> None:
    init(tmp_path)
    file = tmp_path / "config.ts"
    file.write_text('const endpoint = "primary";\n')
    commit(tmp_path)
    head_line = 'const endpoint = "retired";'
    file.write_text((head_line + "\n") * (2 if case == "ambiguous" else 1))
    commit(tmp_path)
    data = DiscussionSnapshotV1.model_validate_json(
        json.dumps(snapshot_data())
    ).model_dump(mode="json")
    data["target"] = data["target"] | {
        "comparison": {
            "merge_base": revision(tmp_path, "HEAD^"),
            "head": revision(tmp_path, "HEAD"),
        }
    }
    if case == "unavailable":
        data |= {
            "status": "unavailable",
            "threads": [],
            "omissions": [
                {"scope": "identity", "code": "read_error", "thread_id": None}
            ],
        }
    else:
        data["threads"][0]["comments"][0] |= {
            "body": f"PRIVATE_CANARY\n\n```ts\n{head_line}\n```",
            "original_commit": revision(tmp_path, "HEAD"),
            "original_location": {
                "path": file.name,
                "side": "head",
                "start_line": None,
                "end_line": 1,
            },
        }
    snapshot = DiscussionSnapshotV1.model_validate_json(json.dumps(data))
    report = review(
        tmp_path,
        model=PinnedClaimModel("clean") if with_model else None,
        snapshot=snapshot,
    )
    result = report.root
    assert "PRIVATE_CANARY" not in report.model_dump_json()
    assert result.discussion_status == snapshot.status
    assert [r.item_id for r in result.discussion_references] == (
        ["T1"] if case == "unique" else []
    )
    assert result.linked_existing_threads == ()
