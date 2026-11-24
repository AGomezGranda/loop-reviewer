from pathlib import Path

import pytest

from loop_reviewer.reviewer.integrations import git_inventory
from loop_reviewer.reviewer.integrations.git_inventory import open_local_comparison
from loop_reviewer.reviewer.inventory import review_inventory
from loop_reviewer.reviewer.inventory_contract import (
    InventoryBudget,
    PinnedComparison,
    ReviewerRequest,
    ReviewerResult,
)
from tests.support.inventory_git import PATH_ENV, commit, git, init, revision


def _report(repo: Path, base: str, head: str) -> ReviewerResult:
    request = ReviewerRequest(
        schema_version=1,
        scope="inventory",
        comparison=PinnedComparison(merge_base=base, head=head),
        budget=InventoryBudget(max_model_spend_usd_micros=0),
        discussion=None,
    )
    return review_inventory(
        request, lambda pins: open_local_comparison(repo, pins, PATH_ENV)
    )


def test_inventory_preserves_shallow_boundaries(tmp_path: Path) -> None:
    repo = tmp_path / "original"
    repo.mkdir()
    init(repo)
    commit(repo)
    git(repo, "branch", "other")
    (repo / "main.txt").write_text("main\n")
    commit(repo)
    git(repo, "checkout", "other")
    (repo / "other.txt").write_text("other\n")
    base = commit(repo)
    git(repo, "checkout", "main")
    git(repo, "merge", "--no-ff", "other", "-m", "Merge other")
    head = revision(repo)
    shallow = tmp_path / "shallow"
    git(tmp_path, "clone", "--depth=2", repo.as_uri(), str(shallow))
    git(shallow, "merge-base", "--is-ancestor", base, head)

    result = _report(shallow, base, head).root
    assert result.status == "complete", result.error
    assert [row.new.path for row in result.file_coverage if row.new] == ["main.txt"]


def test_cleanup_error_preserves_success_and_typed_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def denied(path: Path) -> None:
        raise PermissionError

    def scratch_path(**kwargs: object) -> str:
        return str(tmp_path / "scratch")

    init(tmp_path)
    base = commit(tmp_path)
    monkeypatch.setattr(git_inventory.tempfile, "mkdtemp", scratch_path)
    (tmp_path / "scratch").mkdir()
    monkeypatch.setattr("loop_reviewer.integrations.review_process.rmtree", denied)
    assert _report(tmp_path, base, base).root.status == "complete"
    missing = _report(tmp_path, base, "f" * 40).root
    assert missing.status == "failed" and missing.error.code == "missing_commit"
    assert "Could not remove private Git inventory scratch data." in caplog.text


def test_inventory_covers_reference_comparison(tmp_path: Path) -> None:
    init(tmp_path)
    (tmp_path / ".gitattributes").write_text("generated linguist-generated=true\n")
    for index in range(94):
        (tmp_path / f"edit-{index:02}").write_text("old\n" * 25)
    (tmp_path / "delete").write_text("deleted\n" * 100)
    (tmp_path / "from").write_text("move this file\n")
    base = commit(tmp_path)
    for index in range(94):
        (tmp_path / f"edit-{index:02}").write_text("new\n" * 25)
    (tmp_path / "delete").unlink()
    (tmp_path / "from").rename(tmp_path / "to")
    (tmp_path / "generated").write_text("generated\n" * 100)
    (tmp_path / "oversized").write_text(("x" * 11_000 + "\n") * 100)
    (tmp_path / "binary").write_bytes(b"\0binary")
    (tmp_path / "empty").touch()
    head = commit(tmp_path)

    report = _report(tmp_path, base, head).root
    assert report.status == "complete", report.error
    assert len(report.file_coverage) == 100
    assert (
        sum(
            (row.added_lines or 0) + (row.deleted_lines or 0)
            for row in report.file_coverage
        )
        == 5000
    )
    by_head = {row.new.path: row for row in report.file_coverage if row.new}
    assert by_head["binary"].binary and by_head["binary"].added_lines is None
    assert by_head["oversized"].oversized
    generated = by_head["generated"].new
    assert generated is not None and generated.generated == "marked"
    renamed = by_head["to"]
    assert renamed.change_type == "renamed"
    assert renamed.old is not None and renamed.old.path == "from"
    assert renamed.new is not None
    assert (
        renamed.old.object_id
        == renamed.new.object_id
        == revision(tmp_path, f"{head}:to")
    )
    deleted = next(row for row in report.file_coverage if row.change_type == "deleted")
    assert (
        deleted.old is not None and deleted.old.path == "delete" and deleted.new is None
    )
    assert by_head["empty"].added_lines == 0
    assert report.usage.model_calls == 0
    assert report.recommendation == "none"


def test_inventory_uses_pinned_objects_and_attributes(tmp_path: Path) -> None:
    init(tmp_path)
    attributes = tmp_path / ".gitattributes"
    attributes.write_text("asset -linguist-generated\n")
    asset = tmp_path / "asset"
    asset.write_text("old\n")
    base = commit(tmp_path)
    attributes.write_text("asset linguist-generated -diff\n")
    asset.write_text("new\n")
    head = commit(tmp_path)
    expected = _report(tmp_path, base, head)
    assert expected.root.status == "complete", expected.root.error
    row = next(
        row
        for row in expected.root.file_coverage
        if row.new and row.new.path == "asset"
    )
    assert row.old is not None and row.old.generated == "unmarked"
    assert row.new is not None and row.new.generated == "marked"
    assert row.binary

    attributes.write_text("asset linguist-generated=false diff\n")
    newer = commit(tmp_path)
    git(tmp_path, "replace", head, newer)
    info = tmp_path / ".git" / "info"
    info.mkdir(exist_ok=True)
    (info / "attributes").write_text("* diff -linguist-generated\n")
    attributes.write_text("* diff linguist-generated=maybe\n")
    git(tmp_path, "add", ".gitattributes")
    attributes.write_text("* diff linguist-generated=false\n")
    asset.write_text("dirty\n")
    assert _report(tmp_path, base, head).model_dump_json() == expected.model_dump_json()


def test_git_inventory_preserves_paths_and_modes(tmp_path: Path) -> None:
    init(tmp_path)
    (tmp_path / "binary-old").write_bytes(b"\0binary")
    (tmp_path / "type").write_text("regular file\n")
    (tmp_path / "mode").write_text("keep contents\n")
    base = commit(tmp_path)
    (tmp_path / "binary-old").rename(tmp_path / "binary-new")
    (tmp_path / "type").unlink()
    (tmp_path / "type").symlink_to("nowhere")
    git(tmp_path, "add", "-A")
    git(tmp_path, "update-index", "--chmod=+x", "mode")
    blob = git(tmp_path, "hash-object", "-w", "--stdin", input=b"encoded\n").strip()
    git(
        tmp_path,
        "update-index",
        "-z",
        "--index-info",
        input=(
            b"100644 " + blob + b"\todd\tline\n%\xff\0"
            b"160000 " + b"a" * len(base) + b"\tlink\0"
        ),
    )
    head = commit(tmp_path, stage=False)

    report = _report(tmp_path, base, head).root
    assert report.status == "complete", report.error
    rows = {row.new.path: row for row in report.file_coverage if row.new}
    assert set(rows) == {"binary-new", "type", "mode", "odd%09line%0A%25%FF", "link"}
    assert rows["binary-new"].binary and rows["binary-new"].change_type == "renamed"
    assert rows["type"].change_type == "type_changed"
    assert rows["type"].new is not None and rows["type"].new.mode == "120000"
    assert rows["type"].old is not None and rows["type"].old.mode == "100644"
    assert rows["mode"].new is not None and rows["mode"].new.mode == "100755"
    assert rows["link"].new is not None and rows["link"].new.mode == "160000"
    assert rows["link"].new.blob_bytes is None and rows[
        "link"
    ].new.object_id == "a" * len(base)


def test_inventory_pairs_rename_with_text_edits(tmp_path: Path) -> None:
    init(tmp_path)
    before = tmp_path / "old"
    before.write_text("same line\n" * 20 + "old ending\n")
    base = commit(tmp_path)
    after = tmp_path / "new"
    before.rename(after)
    after.write_text("same line\n" * 20 + "new ending\n")
    head = commit(tmp_path)

    report = _report(tmp_path, base, head).root
    assert report.status == "complete", report.error
    (row,) = report.file_coverage
    assert row.change_type == "renamed"
    assert row.old is not None and row.old.path == "old"
    assert row.new is not None and row.new.path == "new"
    assert (row.added_lines, row.deleted_lines) == (1, 1)


def test_blob_reads_use_the_requested_side_and_size_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init(tmp_path)
    (tmp_path / "old").write_bytes(b"old\n")
    base = commit(tmp_path)
    (tmp_path / "old").unlink()
    (tmp_path / "head").symlink_to("target")
    (tmp_path / "big").write_bytes(b"x" * (1024 * 1024 + 1))
    head = commit(tmp_path)
    with open_local_comparison(
        tmp_path, PinnedComparison(merge_base=base, head=head), PATH_ENV
    ) as source:
        source.inventory()
        assert source.read_blob("base", "old", max_bytes=4) == b"old\n"
        assert source.read_blob("head", "head", max_bytes=6) == b"target"
        assert source.read_blob("head", "old", max_bytes=100) == "not_found"

        def unexpected(*args: object, **kwargs: object) -> None:
            pytest.fail("The size check must run before spawning Git")

        monkeypatch.setattr(git_inventory, "capture_review_process", unexpected)
        assert source.read_blob("base", "old", max_bytes=3) == "limit_reached"
        assert source.read_blob("head", "big", max_bytes=2**24) == "limit_reached"
    assert source.read_blob("head", "head", max_bytes=6) == "unavailable"


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("missing-commit", "missing_commit"),
        ("tree-id", "invalid_comparison"),
        ("nonancestor", "invalid_comparison"),
        ("missing-blob", "git_error"),
        ("metadata-cap", "work_limit"),
        ("row-cap", "work_limit"),
        ("deadline", "work_limit"),
        ("invalid-repository", "invalid_repository"),
    ],
)
def test_inventory_rejects_invalid_git_comparisons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, code: str
) -> None:
    init(tmp_path)
    base = commit(tmp_path)
    (tmp_path / "file").write_text("new content\n")
    head = commit(tmp_path)
    repo = tmp_path
    match case:
        case "missing-commit":
            head = "f" * len(head)
        case "tree-id":
            head = revision(tmp_path, "HEAD^{tree}")
        case "nonancestor":
            base, head = head, base
        case "missing-blob":
            blob = revision(tmp_path, "HEAD:file")
            (tmp_path / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
        case "metadata-cap":
            nested = tmp_path.joinpath(*(["p" * 180] * 4))
            nested.mkdir(parents=True)
            (nested / "long-file").touch()
            head = commit(tmp_path)
            monkeypatch.setattr(git_inventory, "_METADATA_BYTES", 1024)
        case "row-cap":
            monkeypatch.setattr(git_inventory, "MAX_CHANGES", 0)
        case "deadline":
            monkeypatch.setattr(git_inventory, "_INTAKE_SECONDS", 0)
        case "invalid-repository":
            repo = tmp_path / "absent"
    report = _report(repo, base, head).root
    assert report.status == "failed"
    assert report.error.code == code, report.error
    assert report.file_coverage == ()


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
@pytest.mark.parametrize(
    "linked", [False, True], ids=["main-worktree", "linked-worktree"]
)
def test_inventory_supports_hash_formats_and_linked_worktrees(
    tmp_path: Path, object_format: str, linked: bool
) -> None:
    repo = tmp_path / "origin"
    repo.mkdir()
    init(repo, object_format=object_format)
    base = commit(repo)
    (repo / "file").write_bytes(b"data\n")
    head = commit(repo)
    if linked:
        worktree = tmp_path / "linked"
        git(repo, "worktree", "add", "--detach", str(worktree), base)
        repo = worktree
    report = _report(repo, base, head).root
    assert report.status == "complete", report.error
    assert len(report.file_coverage) == 1
    side = report.file_coverage[0].new
    assert side is not None and side.object_id == revision(repo, f"{head}:file")
    same = _report(repo, head, head).root
    assert same.status == "complete" and same.file_coverage == ()
