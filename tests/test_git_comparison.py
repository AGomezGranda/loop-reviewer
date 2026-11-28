"""Exercise pinned object retrieval through the Git integration."""

import os
import subprocess
from collections.abc import Generator
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

import pytest

from loop_reviewer.integrations import git_comparison
from loop_reviewer.integrations.git_comparison import GitComparison
from loop_reviewer.review.generate import ReviewerError
from loop_reviewer.review.hosted_report import HostedReportReviewer
from loop_reviewer.review.model import ReviewSelection
from loop_reviewer.review.pull_request import PullRequestRef, ReviewedRevision
from loop_reviewer.reviewer.discussion_contract import (
    DiscussionTarget,
    unavailable_discussion,
)
from loop_reviewer.reviewer.integrations.git_inventory import open_local_comparison
from loop_reviewer.reviewer.inventory import validated_inventory
from loop_reviewer.reviewer.inventory_contract import InventoryBudget, PinnedComparison
from loop_reviewer.reviewer.pinned_source import PinnedSource

_ENVIRONMENT = {"PATH": os.environ.get("PATH", "")}
_GIT_ENVIRONMENT = {
    **_ENVIRONMENT,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
}


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=_GIT_ENVIRONMENT,
    )
    return result.stdout.strip()


def _commit(repository: Path, name: str, content: str, message: str) -> str:
    (repository / name).write_text(content)
    _git(repository, "add", name)
    _git(repository, "commit", "-q", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def _commit_bytes(repository: Path, name: str, content: bytes, message: str) -> str:
    (repository / name).write_bytes(content)
    _git(repository, "add", name)
    _git(repository, "commit", "-q", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def _repository(
    tmp_path: Path, *, large: bool = False, binary: bool = False, crlf: bool = False
) -> tuple[Path, str, str, str]:
    repository = tmp_path / "origin"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    merge_base = _commit_bytes(
        repository, "README.md", b"root\r\n" if crlf else b"root\n", "root"
    )
    _git(repository, "checkout", "-q", "-b", "fork")
    if large:
        head = _commit(repository, "large.txt", "x\n" * (70 * 1024), "large")
    elif binary:
        head = _commit_bytes(repository, "image.bin", b"\x00\x01\x02", "binary")
    elif crlf:
        head = _commit_bytes(
            repository, "README.md", b"root\n", "normalize line ending"
        )
    else:
        head = _commit(repository, "selected.txt", "selected\n", "selected")
    _commit(repository, "newer.txt", "newer\n", "newer")
    _git(repository, "update-ref", "refs/pull/7/head", "HEAD")
    _git(repository, "checkout", "-q", "main")
    base_tip = _commit(repository, "base-only.txt", "base\n", "base advance")
    return repository, merge_base, base_tip, head


def _selection(base_tip: str, merge_base: str, head: str) -> ReviewSelection:
    return ReviewSelection(
        PullRequestRef("github.com", "acme", "repo", 7),
        "reviewer",
        "teammate",
        ReviewedRevision("main", base_tip, merge_base, head),
    )


def test_empty_comparison_is_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, _, base_tip, head = _repository(tmp_path)

    def remote(_: PullRequestRef) -> str:
        return str(repository)

    monkeypatch.setattr(git_comparison, "_remote_url", remote)

    with GitComparison(_ENVIRONMENT).open_verified_repository(
        _selection(base_tip, head, head)
    ) as scratch:
        pins = PinnedComparison(merge_base=head, head=head)
        with open_local_comparison(scratch, pins, _ENVIRONMENT) as source:
            assert validated_inventory(source, pins) == ()


@pytest.mark.parametrize(
    ("case", "message", "kind"),
    [
        pytest.param(
            "missing-object", "pinned commit was missing", "failed", id="missing-object"
        ),
        pytest.param("nonancestor", "not an ancestor", "input", id="nonancestor"),
    ],
)
def test_rejects_unavailable_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, message: str, kind: str
) -> None:
    repository, merge_base, base_tip, head = _repository(tmp_path)
    if case == "missing-object":
        head = "0" * 40
    elif case == "nonancestor":
        merge_base = base_tip

    def remote(_: PullRequestRef) -> str:
        return str(repository)

    monkeypatch.setattr(git_comparison, "_remote_url", remote)

    with (
        pytest.raises(ReviewerError, match=message) as error,
        GitComparison(_ENVIRONMENT).open_verified_repository(
            _selection(base_tip, merge_base, head)
        ),
    ):
        pytest.fail("Unavailable comparison entered analysis")

    assert error.value.kind == kind


@pytest.mark.parametrize("consumer_fails", [False, True], ids=["exit", "error"])
def test_verified_repository_exposes_pins_without_rendering_oversized_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, consumer_fails: bool
) -> None:
    repository, merge_base, base_tip, head = _repository(tmp_path, large=True)

    def remote(_: PullRequestRef) -> str:
        return str(repository)

    monkeypatch.setattr(git_comparison, "_remote_url", remote)
    comparison = GitComparison(_ENVIRONMENT)
    selection = _selection(base_tip, merge_base, head)
    expected = (
        pytest.raises(RuntimeError, match="analysis failed")
        if consumer_fails
        else nullcontext()
    )
    with expected, comparison.open_verified_repository(selection) as scratch:
        assert _git(scratch, "rev-parse", "--is-bare-repository") == "true"
        assert _git(scratch, "show", f"{merge_base}:README.md") == "root"
        assert len(_git(scratch, "show", f"{head}:large.txt")) > 128 * 1024
        if consumer_fails:
            raise RuntimeError("analysis failed")
    assert not scratch.exists()


def test_fetch_bounds_pack_bytes_even_when_the_patch_is_small(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "origin"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    _commit_bytes(repository, "old.bin", os.urandom(128 * 1024), "old artifact")
    _git(repository, "rm", "-q", "old.bin")
    _git(repository, "commit", "-qm", "remove artifact")
    base = _git(repository, "rev-parse", "HEAD")
    head = _commit(repository, "small.txt", "change\n", "small review")
    _git(repository, "update-ref", "refs/pull/7/head", head)

    def remote(_: PullRequestRef) -> str:
        return repository.as_uri()

    monkeypatch.setattr(git_comparison, "_remote_url", remote)
    comparison = GitComparison(_ENVIRONMENT)
    selection = _selection(head, base, head)
    with comparison.open_verified_repository(selection) as scratch:
        assert _git(scratch, "show", f"{head}:small.txt") == "change"

    monkeypatch.setattr(git_comparison, "_MAX_FETCH_FILE_BYTES", 64 * 1024)
    with (
        pytest.raises(ReviewerError, match="Git pack fits"),
        comparison.open_verified_repository(selection),
    ):
        pytest.fail("Oversized pack entered analysis")


@pytest.mark.parametrize(
    ("amount", "reason"), [(0, "budget_exhausted"), (1, "unsupported_model")]
)
def test_hosted_review_keeps_pins_and_gaps_without_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, amount: int, reason: str
) -> None:
    repository, merge_base, base_tip, head = _repository(tmp_path)

    def remote(_: PullRequestRef) -> str:
        return str(repository)

    monkeypatch.setattr(git_comparison, "_remote_url", remote)
    selection = _selection(base_tip, merge_base, head)
    comparison = GitComparison(_ENVIRONMENT)

    @contextmanager
    def open_source(
        selected: ReviewSelection, pins: PinnedComparison
    ) -> Generator[PinnedSource]:
        with (
            comparison.open_verified_repository(selected) as scratch,
            open_local_comparison(scratch, pins, _ENVIRONMENT) as source,
        ):
            yield source

    def discussion(selected: ReviewSelection):
        assert selected == selection
        return unavailable_discussion(
            DiscussionTarget(
                host="github.com",
                owner="acme",
                repo="repo",
                repository_id=42,
                number=7,
                target_branch="main",
                comparison=PinnedComparison(merge_base=merge_base, head=head),
            ),
            datetime.now(UTC),
            "read_error",
        )

    generated = HostedReportReviewer(
        open_source,
        discussion,
        lambda: (InventoryBudget(max_model_spend_usd_micros=amount), None, None),
        monotonic,
    ).review(selection)
    assert generated.report is not None
    report = generated.report.root
    assert (report.schema_version, report.policy_version, report.status) == (
        6,
        4,
        "partial",
    )
    assert [item.kind for item in report.investigation_coverage] == ["behavior"]
    assert report.discussion_status == "unavailable"
    assert [
        (row.new.path if row.new else None, row.reason) for row in report.file_coverage
    ] == [("selected.txt", reason)]
    assert generated.draft.verdict == "none"
    assert generated.model is None


@pytest.mark.parametrize("command", ["cat-file", "merge-base"])
def test_command_error_is_not_a_stale_selection(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    def capture(
        arguments: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        data = b""
        if "cat-file" in arguments and command != "cat-file":
            data = f"{'b' * 40} commit\n{'c' * 40} commit\n".encode()
        return subprocess.CompletedProcess(
            arguments, 128 if command in arguments else 0, data
        )

    monkeypatch.setattr(git_comparison, "capture_review_process", capture)
    selection = _selection("a" * 40, "b" * 40, "c" * 40)
    with (
        pytest.raises(ReviewerError, match="Check access and try again") as error,
        GitComparison(_ENVIRONMENT).open_verified_repository(selection),
    ):
        pytest.fail("Failed Git command entered analysis")
    assert error.value.kind == "failed"
