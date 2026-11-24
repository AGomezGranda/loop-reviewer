import json

import pytest
from pydantic import ValidationError

from loop_reviewer.reviewer.discussion_contract import (
    DiscussionSnapshotV1,
    DiscussionTarget,
)
from loop_reviewer.reviewer.discussion_report import DiscussionReviewResult
from loop_reviewer.reviewer.semantic_review import review_semantic_with_discussion
from tests.support.discussion import discussion_request, known_snapshot, snapshot_data
from tests.test_local_checks import PINS, row
from tests.test_semantic_batch import ScriptedModel, WitnessAnalyzer, WitnessSource


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("schema_version", "1"),
        ("unknown", False),
        ("captured_at", "2026-11-23T12:00:00"),
        ("captured_at", "2026-11-23T12:00:00+01:00"),
        ("status", "partial"),
        ("status", "unavailable"),
        ("omissions", [{"scope": "replies", "code": "read_error", "thread_id": "T1"}]),
        ("conversation", [{"id": "C1", "body": "duplicate"}]),
        ("conversation", [{"id": "W2", "body": ""}, {"id": "W1", "body": ""}]),
        ("conversation", [{"id": "W1", "body": "🌊" * 1025}]),
        ("conversation", [{"id": f"W{i:03d}", "body": ""} for i in range(101)]),
        (
            "conversation",
            [{"id": f"W{i:03d}", "body": "\n" * 4000} for i in range(100)],
        ),
    ],
    ids=(
        "version",
        "strict-type",
        "closed-fields",
        "naive",
        "non-utc",
        "partial-no-omission",
        "unavailable-with-content",
        "complete-with-omission",
        "cross-collection-id",
        "id-order",
        "utf8-byte-cap",
        "item-cap",
        "snapshot-byte-cap",
    ),
)
def test_snapshot_rejects_invalid_fields(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        DiscussionSnapshotV1.model_validate_json(
            json.dumps(snapshot_data() | {field: value})
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("host", "GITHUB.COM"),
        ("host", "github.com@example.org"),
        ("number", 0),
        ("repository_id", True),
        ("owner", "../octo"),
        ("repo", ".."),
        ("target_branch", "main..other"),
    ],
)
def test_discussion_target_requires_canonical_identity(
    field: str, value: object
) -> None:
    target = DiscussionTarget(
        host="github.com",
        owner="octo",
        repo="demo",
        number=7,
        repository_id=42,
        target_branch="main",
        comparison=PINS,
    )
    with pytest.raises(ValidationError):
        DiscussionTarget.model_validate(target.model_dump() | {field: value})


@pytest.mark.parametrize(
    "permalink",
    [
        "http://github.com/octo/demo/pull/7#discussion_r12",
        "https://github.com@evil.example/octo/demo/pull/7#discussion_r12",
        "https://evil.example/octo/demo/pull/7#discussion_r12",
        "https://github.com/octo/demo/pull/8#discussion_r12",
        "https://github.com/octo/demo/pull/7?foo=bar#discussion_r12",
        "https://github.com/octo/demo/pull/7#issuecomment-12",
    ],
)
def test_snapshot_rejects_unsafe_permalink(permalink: str) -> None:
    snapshot = DiscussionSnapshotV1.model_validate_json(json.dumps(snapshot_data()))
    thread = snapshot.threads[0].model_copy(update={"permalink": permalink})
    with pytest.raises(ValidationError):
        DiscussionSnapshotV1.model_validate(
            snapshot.model_copy(update={"threads": (thread,)})
        )


@pytest.mark.parametrize("path", ["/code.py", "a/../code.py", "a%2Fcode.py", "a%00.py"])
def test_snapshot_rejects_noncanonical_path(path: str) -> None:
    snapshot = DiscussionSnapshotV1.model_validate_json(json.dumps(snapshot_data()))
    thread = snapshot.threads[0]
    root = thread.root
    assert root.original_location is not None
    location = root.original_location.model_copy(update={"path": path})
    thread = thread.model_copy(
        update={"comments": (root.model_copy(update={"original_location": location}),)}
    )
    with pytest.raises(ValidationError):
        DiscussionSnapshotV1.model_validate(
            snapshot.model_copy(update={"threads": (thread,)})
        )


@pytest.mark.parametrize("count", [1, 51], ids=("foreign-parent", "reply-cap"))
def test_snapshot_rejects_invalid_reply_collection(count: int) -> None:
    snapshot = DiscussionSnapshotV1.model_validate_json(json.dumps(snapshot_data()))
    thread = snapshot.threads[0]
    comments = tuple(
        thread.root.model_copy(
            update={"id": f"R{i:03d}", "parent_id": "elsewhere" if count == 1 else "C1"}
        )
        for i in range(count)
    )
    with pytest.raises(ValidationError):
        DiscussionSnapshotV1.model_validate(
            snapshot.model_copy(
                update={
                    "threads": (
                        thread.model_copy(
                            update={"comments": (thread.root, *comments)}
                        ),
                    )
                }
            )
        )


@pytest.mark.parametrize(
    "scenario",
    [
        "no-reference",
        "cross-investigation",
        "unknown-evidence",
        "wrong-pins",
        "one-sided",
        "duplicate-disposition",
        "duplicate-thread",
        "closed-thread",
    ],
)
def test_v4_result_rejects_invalid_link_dispositions(scenario: str) -> None:
    source = WitnessSource()
    source.rows += (row("z.py"),)
    report = review_semantic_with_discussion(
        discussion_request(100, known_snapshot(count=2)),
        source.open,
        WitnessAnalyzer(),
        ScriptedModel(),
        lambda: 0,
    )
    data = json.loads(report.model_dump_json())
    assert {entry["id"] for entry in data["investigation_coverage"]} >= {
        "b00001",
        "b00002",
    }
    (link,) = data["linked_existing_threads"]
    if scenario == "no-reference":
        del data["discussion_references"][0]
    elif scenario == "cross-investigation":
        link["investigation_id"] = "b00002"
    elif scenario == "unknown-evidence":
        link["evidence_ids"][0] = "e99999"
    elif scenario == "wrong-pins":
        next(e for e in data["evidence_index"] if e["id"] == link["evidence_ids"][0])[
            "range"
        ]["commit_id"] = "e" * 40
    elif scenario == "one-sided":
        link["evidence_ids"] = [link["evidence_ids"][0]]
    elif scenario == "duplicate-disposition":
        data["linked_existing_threads"].append(
            link | {"thread_id": data["discussion_references"][1]["item_id"]}
        )
    elif scenario == "duplicate-thread":
        data["linked_existing_threads"].append(link | {"candidate_id": "c09999"})
    else:
        data["discussion_references"][0]["is_resolved"] = True
    with pytest.raises(ValidationError):
        DiscussionReviewResult.model_validate_json(json.dumps(data))
