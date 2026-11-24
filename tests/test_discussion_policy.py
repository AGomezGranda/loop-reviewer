import json
from dataclasses import replace
from typing import override

import pytest

from loop_reviewer.reviewer.discussion_contract import (
    CaptureOmission,
    DiscussionItem,
    DiscussionSnapshotV1,
    DiscussionThread,
)
from loop_reviewer.reviewer.discussion_policy import (
    link_verified_threads,
    map_discussion,
)
from loop_reviewer.reviewer.integrations.git_hunks import decode_hunks
from loop_reviewer.reviewer.inventory import RevisionSide
from loop_reviewer.reviewer.inventory_contract import ChangedFile
from loop_reviewer.reviewer.local_evidence import EvidenceIndex, LocalEvidenceWork
from loop_reviewer.reviewer.model_budget import SpendLedger
from loop_reviewer.reviewer.pinned_source import Hunk, PinnedTextBlob, ReadFailure
from loop_reviewer.reviewer.semantic_batch import BatchOutcome, run_semantic_batch
from loop_reviewer.reviewer.semantic_inputs import build_targeted_bundle
from loop_reviewer.reviewer.semantic_limits import SemanticLimits
from tests.support.discussion import KNOWN_CLAIM, known_snapshot
from tests.test_local_checks import PINS
from tests.test_semantic_batch import (
    BASE,
    HEAD,
    ScriptedModel,
    WitnessAnalyzer,
    WitnessSource,
)


@pytest.fixture
def verified() -> BatchOutcome:
    source = WitnessSource()
    index = EvidenceIndex()
    work = LocalEvidenceWork(source, WitnessAnalyzer(), PINS, lambda: 0, 120)
    bundle = build_targeted_bundle(PINS, source.rows, work, index, 8192)
    return run_semantic_batch(
        bundle,
        source,
        ScriptedModel(),
        SpendLedger(100, lambda: 0),
        SemanticLimits(120),
        lambda: 0,
        evidence=index,
    )


@pytest.mark.parametrize(
    ("scenario", "links", "references"),
    [
        ("exact", True, True),
        ("deletion-side", True, True),
        ("deletion-side-without-hunk", False, False),
        ("whitespace-prose-case", True, True),
        ("missing-trigger", False, True),
        ("missing-caller", False, True),
        ("other-caller-same-anchor", False, True),
        ("changed-return-value", False, True),
        ("different-exception-case", False, True),
        ("negation", False, True),
        ("outdated", False, True),
        ("unknown-line", False, False),
        ("reply", False, True),
        ("missing-permalink", False, True),
        ("incomplete-replies", False, True),
        ("ambiguous-thread-pages", False, True),
        ("quote-does-not-prove-return", False, True),
    ],
)
def test_linking_requires_unambiguous_open_root_and_witness(
    scenario: str,
    links: bool,
    references: bool,
    verified: BatchOutcome,
) -> None:
    source = WitnessSource()
    body = {
        "whitespace-prose-case": "producer.items()  RETURNS [] INSTEAD OF None\n\ncall consumer.first()\nconsumer.first() RAISES IndexError",
        "missing-trigger": "\n".join(
            (KNOWN_CLAIM.splitlines()[0], KNOWN_CLAIM.splitlines()[2])
        ),
        "missing-caller": KNOWN_CLAIM.replace("consumer.first()", "first()"),
        "other-caller-same-anchor": KNOWN_CLAIM.replace(
            "consumer.first()", "other.first()"
        ),
        "changed-return-value": KNOWN_CLAIM.replace("[]", "()"),
        "different-exception-case": KNOWN_CLAIM.replace("IndexError", "indexerror"),
        "negation": "This is not a defect:\n" + KNOWN_CLAIM,
    }.get(scenario, KNOWN_CLAIM)
    snapshot = known_snapshot(body=body)
    (thread,) = snapshot.threads
    root = thread.root
    original = root.original_location
    assert original is not None
    if scenario.startswith("deletion-side"):
        root = root.model_copy(
            update={
                "original_location": original.model_copy(update={"side": "base"}),
                "diff_hunk": root.diff_hunk if scenario == "deletion-side" else None,
            }
        )
    if scenario == "unknown-line":
        root = root.model_copy(
            update={"original_location": original.model_copy(update={"end_line": None})}
        )
    if scenario == "quote-does-not-prove-return":
        root = root.model_copy(
            update={"original_location": original.model_copy(update={"end_line": 1})}
        )
    comments = (root,)
    if scenario == "reply":
        comments += (
            root.model_copy(
                update={"id": "R1", "parent_id": root.id, "body": "Unrelated reply"}
            ),
        )
    thread = DiscussionThread.model_validate(
        thread.model_copy(
            update={
                "is_outdated": scenario == "outdated",
                "comments": comments,
                "permalink": None
                if scenario == "missing-permalink"
                else thread.permalink,
            }
        )
    )
    update: dict[str, object] = {"threads": (thread,)}
    if scenario in {"incomplete-replies", "ambiguous-thread-pages"}:
        update.update(
            status="partial",
            omissions=(
                CaptureOmission(
                    scope="replies" if scenario == "incomplete-replies" else "threads",
                    code="read_error"
                    if scenario == "incomplete-replies"
                    else "invalid_response",
                    thread_id=thread.id if scenario == "incomplete-replies" else None,
                ),
            ),
        )
    snapshot = DiscussionSnapshotV1.model_validate(snapshot.model_copy(update=update))
    context = map_discussion(
        snapshot,
        source.rows,
        LocalEvidenceWork(source, WitnessAnalyzer(), PINS, lambda: 0, 120),
    )
    assert bool(context.by_file) is references and not context.omissions
    disposition = link_verified_threads(verified.findings, context, verified.evidence)
    assert bool(disposition.linked) is links
    assert disposition.kept == (() if links else verified.findings)


class RenamedSource(WitnessSource):
    def __init__(self, repeated: bool) -> None:
        super().__init__()
        self.head = b"# moved\n" + HEAD + (b"\n    return []\n" if repeated else b"")
        (change,) = self.rows
        assert change.old and change.new
        self.rows = (
            ChangedFile.model_validate(
                change.model_dump()
                | {
                    "change_type": "renamed",
                    "old": change.old.model_copy(update={"path": "old.py"}),
                    "new": change.new.model_copy(
                        update={"path": "new.py", "blob_bytes": len(self.head)}
                    ),
                }
            ),
        )

    @override
    def read_text_blob(
        self, side: RevisionSide, path: str, *, max_bytes: int, timeout_seconds: float
    ) -> PinnedTextBlob | ReadFailure:
        if (side, path) == ("base", "old.py"):
            return PinnedTextBlob(side, PINS.merge_base, path, "c" * 40, BASE)
        if (side, path) == ("head", "new.py"):
            return PinnedTextBlob(side, PINS.head, path, "d" * 40, self.head)
        pytest.fail("mapping must not read unrelated paths")

    @override
    def hunks(
        self, change: ChangedFile, *, max_bytes: int, timeout_seconds: float
    ) -> tuple[Hunk, ...]:
        assert change == self.rows[0]
        return decode_hunks(
            b"@@ -1,2 +1,3 @@\n+# moved\n def items():\n-    return None\n+    return []\n"
        )


@pytest.mark.parametrize(
    "repeated", [False, True], ids=("unique-move", "ambiguous-excerpt")
)
def test_stale_quote_maps_only_when_unique_on_recorded_rename_pair(
    repeated: bool,
) -> None:
    source = RenamedSource(repeated)
    data = json.loads(known_snapshot(path="old.py").model_dump_json())
    data["threads"][0]["comments"][0]["original_commit"] = "e" * 40
    snapshot = DiscussionSnapshotV1.model_validate_json(json.dumps(data))
    context = map_discussion(
        snapshot,
        source.rows,
        LocalEvidenceWork(source, WitnessAnalyzer(), PINS, lambda: 0, 120),
    )
    assert not context.omissions
    if repeated:
        assert not context.by_file
    else:
        (attachment,) = context.by_file[source.rows[0]]
        assert (attachment.location.path, attachment.location.start_line) == (
            "new.py",
            3,
        )
        assert not attachment.can_link


def test_unanchored_reference_requires_explicit_path_and_exact_unique_hunk() -> None:
    source = WitnessSource()
    snapshot = known_snapshot(count=0)
    bodies = (
        "I looked at producer.py\n```py\n    return []\n```",
        "Path: `producer.py`\n```py\n    return ()\n```",
        "Path: `producer.py`\n```py\n    return []\n```",
        "Look at producer.py",
    )
    snapshot = DiscussionSnapshotV1.model_validate(
        snapshot.model_copy(
            update={
                "conversation": tuple(
                    DiscussionItem(id=f"I{i}", body=body)
                    for i, body in enumerate(bodies)
                )
            }
        )
    )
    context = map_discussion(
        snapshot,
        source.rows,
        LocalEvidenceWork(source, WitnessAnalyzer(), PINS, lambda: 0, 120),
    )
    assert [
        (item.item_id, item.kind, item.can_link)
        for item in context.by_file[source.rows[0]]
    ] == [("I2", "conversation", False)]
    assert not context.unresolved_paths and not context.omissions


def test_mapping_uses_smallest_thread_id_and_never_assigns_one_thread_twice(
    verified: BatchOutcome,
) -> None:
    source = WitnessSource()
    snapshot = known_snapshot(count=2)
    context = map_discussion(
        snapshot,
        source.rows,
        LocalEvidenceWork(source, WitnessAnalyzer(), PINS, lambda: 0, 120),
    )
    (finding,) = verified.findings
    disposition = link_verified_threads((finding,), context, verified.evidence)
    assert [item.thread_id for item in disposition.linked] == [snapshot.threads[0].id]
    single = map_discussion(
        known_snapshot(),
        source.rows,
        LocalEvidenceWork(source, WitnessAnalyzer(), PINS, lambda: 0, 120),
    )
    second = replace(finding, id="c00402")
    disposition = link_verified_threads((finding, second), single, verified.evidence)
    assert len(disposition.linked) == 1 and disposition.kept == (second,)


def test_mapping_deadline_is_local_and_uses_remaining_timeout() -> None:
    class SlowSource(WitnessSource):
        @override
        def read_text_blob(
            self,
            side: RevisionSide,
            path: str,
            *,
            max_bytes: int,
            timeout_seconds: float,
        ) -> PinnedTextBlob | ReadFailure:
            assert 0 < timeout_seconds <= 2
            self.time += 2
            return super().read_text_blob(
                side, path, max_bytes=max_bytes, timeout_seconds=timeout_seconds
            )

    source = SlowSource()
    work = LocalEvidenceWork(source, WitnessAnalyzer(), PINS, lambda: source.time, 120)
    context = map_discussion(known_snapshot(), source.rows, work)
    assert not context.by_file
    assert [(item.scope, item.code) for item in context.omissions] == [
        ("mapping", "limit_reached")
    ]
    assert work.remaining() == 118
