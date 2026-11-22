"""Exercise pinned diff retrieval through the Git integration."""

import os
import subprocess
from pathlib import Path

import pytest

from loop_reviewer.integrations import git_comparison
from loop_reviewer.integrations.git_comparison import GitComparison
from loop_reviewer.review.generate import ReviewerError
from loop_reviewer.review.model import ReviewSelection
from loop_reviewer.review.pull_request import PullRequestRef, ReviewedRevision

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


@pytest.mark.parametrize(
    "crlf",
    [
        pytest.param(False, id="selected-head"),
        pytest.param(True, id="line-ending-only"),
    ],
)
def test_prepares_exact_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crlf: bool
) -> None:
    repository, merge_base, base_tip, head = _repository(tmp_path, crlf=crlf)

    def remote(_: PullRequestRef) -> str:
        return str(repository)

    monkeypatch.setattr(git_comparison, "_remote_url", remote)

    patch = GitComparison(_ENVIRONMENT).read(_selection(base_tip, merge_base, head))

    expected = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "diff",
            "--no-color",
            "--no-renames",
            merge_base,
            head,
            "--",
        ],
        check=True,
        capture_output=True,
        env=_GIT_ENVIRONMENT,
    ).stdout
    assert patch.encode("utf-8") == expected
    if crlf:
        assert "-root\r\n+root\n" in patch
    else:
        assert "+selected" in patch
    assert "newer.txt" not in patch
    assert "base-only.txt" not in patch


def test_empty_comparison_is_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, _, base_tip, head = _repository(tmp_path)

    def remote(_: PullRequestRef) -> str:
        return str(repository)

    monkeypatch.setattr(git_comparison, "_remote_url", remote)

    assert GitComparison(_ENVIRONMENT).read(_selection(base_tip, head, head)) == ""


@pytest.mark.parametrize(
    ("case", "message"),
    [
        pytest.param("missing-object", "no longer available", id="missing-object"),
        pytest.param("nonancestor", "not an ancestor", id="nonancestor"),
        pytest.param("oversized", "128 KiB", id="oversized"),
        pytest.param("binary", "binary", id="binary"),
    ],
)
def test_rejects_unavailable_or_oversized_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, message: str
) -> None:
    repository, merge_base, base_tip, head = _repository(
        tmp_path, large=case == "oversized", binary=case == "binary"
    )
    if case == "missing-object":
        head = "0" * 40
    elif case == "nonancestor":
        merge_base = base_tip

    def remote(_: PullRequestRef) -> str:
        return str(repository)

    monkeypatch.setattr(git_comparison, "_remote_url", remote)

    with pytest.raises(ReviewerError, match=message) as error:
        GitComparison(_ENVIRONMENT).read(_selection(base_tip, merge_base, head))

    assert error.value.kind == "input"


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
    assert len(comparison.read(selection).encode()) < 1024

    monkeypatch.setattr(git_comparison, "_MAX_FETCH_FILE_BYTES", 64 * 1024)
    with pytest.raises(ReviewerError, match="Git pack fits"):
        comparison.read(selection)


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
    with pytest.raises(ReviewerError, match="Check access and try again") as error:
        GitComparison(_ENVIRONMENT).read(_selection("a" * 40, "b" * 40, "c" * 40))
    assert error.value.kind == "failed"
