from pathlib import Path
from subprocess import TimeoutExpired

import pytest

from loop_reviewer.reviewer.integrations import git_inventory
from loop_reviewer.reviewer.integrations.git_inventory import open_local_comparison
from loop_reviewer.reviewer.inventory import RevisionSide
from loop_reviewer.reviewer.inventory_contract import PinnedComparison, encode_path
from loop_reviewer.reviewer.pinned_source import (
    PinnedTextBlob,
    ReadFailure,
    SearchPaths,
)
from loop_reviewer.reviewer.semantic_limits import MeteredSource, SemanticLimits
from tests.support.inventory_git import PATH_ENV, commit, git, init, revision


def test_context_reads_unchanged_caller_from_both_pins(tmp_path: Path) -> None:
    init(tmp_path)
    producer = tmp_path / "old.py"
    producer.write_text("# Unchanged line\n" * 20 + "def parse(): return None\n")
    caller = tmp_path / "with spaces.py"
    caller.write_bytes(b"from old import parse\nif parse() is None:\n    pass\n")
    base = commit(tmp_path)
    new = tmp_path / "new.py"
    producer.rename(new)
    new.write_text("# Unchanged line\n" * 20 + "def parse(): return []\n")
    raw_path = b"raw-\xff.py"
    oid = git(tmp_path, "hash-object", "-w", "--stdin", input=b"# parse\n").strip()
    git(tmp_path, "add", "-A")
    git(
        tmp_path,
        "update-index",
        "-z",
        "--index-info",
        input=b"100644 " + oid + b"\t" + raw_path + b"\0",
    )
    head = commit(tmp_path, stage=False)
    git(tmp_path, "checkout", "--detach", base)
    caller.write_text("dirty, not pinned\n")
    with open_local_comparison(
        tmp_path, PinnedComparison(merge_base=base, head=head), PATH_ENV
    ) as source:
        pins: tuple[tuple[RevisionSide, str], ...] = (("base", base), ("head", head))
        for side, commit_id in pins:
            blob = source.read_text_blob(
                side, "with%20spaces.py", max_bytes=128, timeout_seconds=2
            )
            assert isinstance(blob, PinnedTextBlob)
            assert blob.text == git(tmp_path, "show", f"{commit_id}:with spaces.py")
            assert blob.commit_id == commit_id
            assert blob.blob_id == revision(tmp_path, f"{commit_id}:with spaces.py")
        assert source.search(
            "head", "parse", max_paths=20, max_bytes=1024, timeout_seconds=2
        ) == SearchPaths(("new.py", "raw-%FF.py", "with%20spaces.py"), False)
        blob = source.read_text_blob(
            "head", encode_path(raw_path), max_bytes=128, timeout_seconds=2
        )
        assert blob == PinnedTextBlob(
            "head", head, "raw-%FF.py", oid.decode("ascii"), b"# parse\n"
        )
        change = next(row for row in source.inventory() if row.change_type == "renamed")
        assert change.new is not None and change.new.path == "new.py"
        hunks = source.hunks(change, max_bytes=1024, timeout_seconds=2)
        assert isinstance(hunks, tuple)
        (hunk,) = hunks
        assert hunk.patch.startswith(b"@@ -18,4 +18,4 @@\n")
        assert hunk.patch.endswith(
            b"-def parse(): return None\n+def parse(): return []\n"
        )
        assert source.hunks(change, max_bytes=1, timeout_seconds=2) == "limit_reached"


def test_pinned_reads_refuse_unsupported_paths_and_enforce_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init(tmp_path)
    base = commit(tmp_path)
    (tmp_path / "literal[1].py").write_bytes(b"needle\nlast\rpart")
    (tmp_path / "literal1.py").write_bytes(b"needle\n")
    (tmp_path / "binary.py").write_bytes(b"\0needle\n")
    (tmp_path / "sym.py").symlink_to("literal1.py")
    (tmp_path / "big").write_bytes(b"x" * (1024 * 1024 + 1))
    (tmp_path / "empty").touch()
    git(tmp_path, "add", "-A")
    git(tmp_path, "update-index", "--add", "--cacheinfo", f"160000,{base},link.py")
    head = commit(tmp_path, stage=False)
    with open_local_comparison(
        tmp_path, PinnedComparison(merge_base=base, head=head), PATH_ENV
    ) as source:

        def read(path: str) -> PinnedTextBlob | ReadFailure:
            return source.read_text_blob(
                "head", path, max_bytes=2**20, timeout_seconds=2
            )

        assert read("sym.py") == read("link.py") == read("binary.py") == "unsupported"
        assert read("big") == "limit_reached"
        assert read("missing.py") == "not_found"
        assert read("%2E%2E/missing.py") == "unsupported"
        blob = read("literal%5B1%5D.py")
        assert isinstance(blob, PinnedTextBlob) and blob.text == b"needle\nlast\rpart"
        assert source.search(
            "head", "needle", max_paths=1, max_bytes=1024, timeout_seconds=2
        ) == SearchPaths(("literal1.py",), True)
        assert source.search(
            "base", "needle", max_paths=1, max_bytes=1024, timeout_seconds=2
        ) == SearchPaths((), False)
        limits = SemanticLimits(
            deadline=2, combined_limit=128, prompt_limit=0, preparing_investigation=True
        )
        metered = MeteredSource(source, limits, lambda: 0)
        assert metered.search(
            "head", "needle", max_paths=1, max_bytes=64 * 1024, timeout_seconds=2
        ) == SearchPaths(("literal1.py",), True)
        before = limits.retrieved_bytes
        assert 0 < before < 128
        assert metered.search(
            "base", "needle", max_paths=1, max_bytes=64 * 1024, timeout_seconds=2
        ) == SearchPaths((), False)
        assert limits.retrieved_bytes == before
        assert (
            source.search("head", "needle", max_paths=1, max_bytes=1, timeout_seconds=2)
            == "limit_reached"
        )
        assert (
            source.search("head", "a-b", max_paths=1, max_bytes=1024, timeout_seconds=2)
            == "unsupported"
        )
        rows = {row.new.path: row for row in source.inventory() if row.new}
        for path in ("sym.py", "link.py", "binary.py"):
            assert (
                source.hunks(rows[path], max_bytes=1024, timeout_seconds=2)
                == "unsupported"
            )
        assert (
            source.hunks(rows["big"], max_bytes=2**20, timeout_seconds=2)
            == "limit_reached"
        )
        assert source.hunks(rows["empty"], max_bytes=1024, timeout_seconds=2) == ()
        foreign = rows["empty"].model_copy(update={"added_lines": 1})
        assert source.hunks(foreign, max_bytes=1024, timeout_seconds=2) == "not_found"

        def timeout(*args: object, **kwargs: object) -> None:
            raise TimeoutExpired("git", 2)

        monkeypatch.setattr(git_inventory, "capture_review_process", timeout)
        assert read("literal1.py") == "limit_reached"
    assert read("literal1.py") == "unavailable"
    assert (
        source.search("head", "needle", max_paths=1, max_bytes=1024, timeout_seconds=2)
        == "unavailable"
    )
    assert (
        source.hunks(rows["empty"], max_bytes=1024, timeout_seconds=2) == "unavailable"
    )
