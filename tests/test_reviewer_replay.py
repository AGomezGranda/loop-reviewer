import hashlib
import json
from pathlib import Path

from loop_reviewer.reviewer.integrations.git_inventory import open_local_comparison
from loop_reviewer.reviewer.inventory import validated_inventory
from loop_reviewer.reviewer.inventory_contract import PinnedComparison
from tests.support.inventory_git import PATH_ENV, commit, git, init, revision
from tests.support.reviewer_replay import FIXTURES, bounded_diff_only, build_replay


def test_replay_regenerates_pins_and_diff_input_digests(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    build_replay(first)
    build_replay(second)
    build_replay(first)
    manifest = json.loads((FIXTURES / "manifest.json").read_bytes())
    labels = json.loads((FIXTURES / "labels.json").read_bytes())
    assert len(manifest) == 6 and {entry["id"] for entry in manifest} == set(labels)
    assert [entry["split"] for entry in manifest] == ["development"] * 4 + [
        "holdout"
    ] * 2
    legacy = json.loads((first / "legacy.json").read_bytes())
    for entry in manifest:
        name = entry["id"]
        repo = first / "repos" / name
        pins = PinnedComparison(merge_base=entry["merge_base"], head=entry["head"])
        assert len(pins.head) == 40
        assert revision(repo, "base") == pins.merge_base and revision(repo) == pins.head
        assert revision(second / "repos" / name, "base") == pins.merge_base
        assert revision(second / "repos" / name) == pins.head
        payload = (first / "diff-only" / f"{name}.json").read_bytes()
        assert (
            len(payload) <= 64 * 1024
            and payload == (second / "diff-only" / f"{name}.json").read_bytes()
        )
        assert hashlib.sha256(payload).hexdigest() == entry["input_sha256"]
        assert json.loads(payload)["comparison"] == pins.model_dump()
        with open_local_comparison(repo, pins, PATH_ENV) as source:
            rows = validated_inventory(source, pins)
        assert len(rows) == entry["expected_coverage_count"]
        churn = sum((r.added_lines or 0) + (r.deleted_lines or 0) for r in rows)
        assert churn == entry["expected_changed_lines"]
        assert legacy[name] == entry["legacy_status"]
        for defect in labels[name]:
            assert defect["id"].encode() not in payload
            assert defect["path"] in [r.new.path for r in rows if r.new]
            for location in defect["ranges"]:
                pin = pins.head if location["side"] == "head" else pins.merge_base
                text = git(repo, "show", f"{pin}:{location['path']}").splitlines()
                assert 1 <= location["start_line"] <= location["end_line"] <= len(text)
                assert (
                    location["text"]
                    == b"\n".join(
                        text[location["start_line"] - 1 : location["end_line"]]
                    ).decode()
                )
        if name == "generated-churn":
            assert (len(rows), churn) == (100, 5000)
            assert legacy[name] == "unsupported_size"
            assert any(r.binary for r in rows) and any(r.oversized for r in rows)
            assert any(r.new and r.new.generated == "marked" for r in rows)
            assert json.loads(payload)["omissions"]
        if name in ("unchanged-consumer", "generated-churn"):
            assert b"consumer.py" not in payload and b"if items is None:" not in payload


def test_diff_only_payload_is_bounded_and_has_no_context_or_labels(
    tmp_path: Path,
) -> None:
    init(tmp_path)
    (tmp_path / "consumer.py").write_text("unchanged()\n")
    base = commit(tmp_path)
    (tmp_path / "big.py").write_text("LARGE_HUNK\n" * 300)
    (tmp_path / "small.py").write_text("# small\n")
    head = commit(tmp_path)
    pins = PinnedComparison(merge_base=base, head=head)
    with open_local_comparison(tmp_path, pins, PATH_ENV) as source:
        rows = validated_inventory(source, pins)
        payload, retrieved = bounded_diff_only(pins, rows, source, max_bytes=700)
        metadata_limited, _ = bounded_diff_only(pins, rows, source, max_bytes=300)
    assert (
        len(payload) <= 700
        and b"LARGE_HUNK" not in payload
        and b"consumer.py" not in payload
    )
    decoded = json.loads(payload)
    assert decoded["hunks"] == [{"file": 1, "patch": "@@ -0,0 +1 @@\n+# small\n"}]
    assert retrieved > 3000
    assert decoded["omissions"] == [
        {"file": 0, "reason": "payload_limit", "omitted_hunks": 1}
    ]
    assert set(decoded) == {
        "comparison",
        "files",
        "hunks",
        "omissions",
        "omitted_paths",
    }
    assert (
        len(metadata_limited) <= 300
        and json.loads(metadata_limited)["omitted_paths"] > 0
    )
