"""Exercise the installed CLI and its review and draft workflows."""

import json
import os
import subprocess
import sys
import sysconfig
from collections.abc import Callable
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from typing import Literal, override

import pytest

from loop_reviewer import cli, composition
from loop_reviewer.integrations.github import GhPullRequestReader
from loop_reviewer.integrations.local_state.sqlite_reviews import SqliteReviewRunStore
from loop_reviewer.review.generate import (
    DraftReview,
    DraftReviewStatus,
    FailedReview,
    ReviewerError,
    ReviewOutcome,
    SavedDraftHistoryUnavailable,
    SavedDraftInterrupted,
    UnavailableReview,
)
from loop_reviewer.review.model import (
    Finding,
    FindingLocation,
    ReviewDraft,
    ReviewIdentity,
    ReviewRun,
    ReviewSelection,
    SelectionChange,
)
from loop_reviewer.review.pull_request import (
    PullRequest,
    PullRequestRef,
    ReviewedRevision,
)
from loop_reviewer.review.selection import (
    GitHubReadError,
    PullRequestReader,
)
from loop_reviewer.review.store import ReviewStoreError


@pytest.fixture(autouse=True)
def github_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    def capture(
        args: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        assert args[:3] == ["gh", "auth", "token"]
        return subprocess.CompletedProcess(args, 0, b"fixture-token\n")

    monkeypatch.setattr(
        "loop_reviewer.integrations.github_credentials.capture_review_process", capture
    )


@pytest.mark.parametrize(
    ("options", "exit_code", "message"),
    [
        pytest.param(["--help"], 0, "usage: loop-reviewer", id="help"),
        pytest.param(
            ["--unknown"], 2, "unrecognized arguments: --unknown", id="unknown-option"
        ),
        pytest.param(
            ["open", "--help"], 0, "usage: loop-reviewer open", id="open-help"
        ),
        pytest.param(["open"], 2, "arguments are required: PR_URL", id="missing-url"),
        pytest.param(
            ["publish", "--help"], 0, "usage: loop-reviewer publish", id="publish-help"
        ),
        pytest.param(
            ["publish", "confirm", "bogus"],
            2,
            "canonical UUID4",
            id="invalid-preview-id",
        ),
        pytest.param(
            ["publish", "preview", "1", "--version", "1"],
            2,
            "--action",
            id="missing-action",
        ),
        pytest.param(
            ["publish", "preview", "1", "--version", str(2**63), "--action", "comment"],
            2,
            "outside the supported integer range",
            id="oversized-preview-version",
        ),
        pytest.param(
            ["review", "--help"], 0, "usage: loop-reviewer review", id="review-help"
        ),
        pytest.param(
            ["review"], 2, "arguments are required: PR_URL", id="review-missing-url"
        ),
        pytest.param(
            ["review", "https://github.com/acme/repo/issues/7"],
            2,
            "Expected a pull request URL",
            id="review-invalid-url",
        ),
    ],
)
def test_cli_exit_contract(options: list[str], exit_code: int, message: str) -> None:
    scripts = Path(sysconfig.get_path("scripts"))
    executable = scripts / (
        "loop-reviewer.exe" if sys.platform == "win32" else "loop-reviewer"
    )
    result = subprocess.run(
        [executable, *options], capture_output=True, text=True, check=False, timeout=10
    )

    assert result.returncode == exit_code, result.stderr
    assert message in (result.stdout if exit_code == 0 else result.stderr)


_URL = "https://Git.Example.com/acme/repo/pull/7"
_REVISION = ReviewedRevision("release/next", "a" * 40, "b" * 40, "c" * 40)
_SELECTION = ReviewSelection(
    PullRequestRef("git.example.com", "acme", "repo", 7),
    "reviewer",
    "teammate",
    _REVISION,
)
_FINDINGS = (
    Finding("low", "Low condition", "Low consequence", None),
    Finding(
        "high", "High condition", "High consequence", FindingLocation("src/app.py", 2)
    ),
    Finding("medium", "Medium condition", "Medium consequence", None),
)
_RUN = ReviewRun(
    1,
    _SELECTION,
    "draft_ready",
    ReviewDraft("comment", "Summary text.", ()),
    None,
)


class StubReader:
    def __init__(self, failure: str | None) -> None:
        self.failure = failure

    def read_pull_request(self, ref: PullRequestRef) -> tuple[str, PullRequest]:
        assert ref == PullRequestRef("git.example.com", "acme", "repo", 7)
        return "reviewer", PullRequest(
            PullRequestRef(ref.host, "Acme", "Repo", ref.number),
            "teammate",
            "open",
            self.failure == "draft",
            _REVISION.target_branch,
            _REVISION.head,
        )

    def read_revision(self, pull_request: PullRequest) -> ReviewedRevision:
        if self.failure == "resolution":
            raise GitHubReadError("Could not compare the selected commits.")
        return _REVISION


class StubApplication:
    def __init__(
        self,
        outcome: ReviewOutcome | None,
        recorded: dict[str, object],
        error: BaseException | None,
    ) -> None:
        self.outcome = outcome
        self.recorded = recorded
        self.error = error

    def run(
        self,
        ref: PullRequestRef,
        *,
        retry: bool = False,
        expected_identity: ReviewIdentity | None = None,
    ) -> ReviewOutcome:
        assert expected_identity is None
        assert ref == PullRequestRef("git.example.com", "acme", "repo", 7)
        self.recorded["retry"] = retry
        if self.error is not None:
            raise self.error
        assert self.outcome is not None
        return self.outcome


def _run_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    outcome: ReviewOutcome | None,
    *,
    retry: bool = False,
    error: BaseException | None = None,
    url: str = _URL,
) -> tuple[int, dict[str, object]]:
    recorded: dict[str, object] = {}

    def build_reader(environment: dict[str, str]) -> object:
        return object()

    def build_store(path: Path) -> object:
        return object()

    def build_execution(path: Path, factory: object) -> object:
        return object()

    def build_application(
        reader: object, store: object, execution: object, *, select_current: object
    ) -> StubApplication:
        return StubApplication(outcome, recorded, error)

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(composition, "GhPullRequestReader", build_reader, raising=False)
    monkeypatch.setattr(composition, "SqliteReviewRunStore", build_store, raising=False)
    monkeypatch.setattr(
        composition, "LockedPiExecution", build_execution, raising=False
    )
    monkeypatch.setattr(
        composition, "ReviewPullRequest", build_application, raising=False
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "loop-reviewer",
            "review",
            *(["--retry"] if retry else []),
            url,
        ],
    )
    return cli.main(), recorded


def test_cli_open_renders_selection(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def build_reader(environment: dict[str, str]) -> PullRequestReader:
        assert environment["GH_HOST"] == "git.example.com"
        return StubReader(None)

    monkeypatch.setenv("GH_HOST", "git.example.com")
    monkeypatch.setattr(composition, "GhPullRequestReader", build_reader, raising=False)
    monkeypatch.setattr(sys, "argv", ["loop-reviewer", "open", _URL])

    assert cli.main() == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines() == [
        "PR: Acme/Repo#7",
        "Host: git.example.com",
        "Account: reviewer",
        "Author: teammate",
        f"Target branch: {_REVISION.target_branch}",
        f"Base tip: {_REVISION.base_tip}",
        f"Merge base: {_REVISION.merge_base}",
        f"Head: {_REVISION.head}",
    ]


def test_cli_open_does_not_construct_review_state(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("review state was constructed for open")

    def build_reader(environment: dict[str, str]) -> PullRequestReader:
        return StubReader(None)

    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("LOOP_REVIEWER_PI_MODEL", raising=False)
    monkeypatch.setattr(composition, "SqliteReviewRunStore", forbidden, raising=False)
    monkeypatch.setattr(composition, "LockedPiExecution", forbidden, raising=False)
    monkeypatch.setattr(composition, "PiReviewer", forbidden, raising=False)
    monkeypatch.setattr(composition, "GhPullRequestReader", build_reader, raising=False)
    monkeypatch.setattr(sys, "argv", ["loop-reviewer", "open", _URL])

    assert cli.main() == 0
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("url", "failure", "exit_code", "message"),
    [
        pytest.param(
            "https://github.com/acme/repo/issues/7",
            None,
            2,
            "Expected a pull request URL",
            id="invalid-url",
        ),
        pytest.param(_URL, "draft", 1, "ready for review", id="draft"),
        pytest.param(_URL, "resolution", 1, "Could not compare", id="resolution"),
    ],
)
def test_cli_open_reports_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    url: str,
    failure: str | None,
    exit_code: int,
    message: str,
) -> None:
    def build_reader(_: dict[str, str]) -> PullRequestReader:
        assert failure is not None, "Don't construct a reader for invalid URLs"
        return StubReader(failure)

    monkeypatch.setattr(composition, "GhPullRequestReader", build_reader, raising=False)
    monkeypatch.setattr(sys, "argv", ["loop-reviewer", "open", url])

    with pytest.raises(SystemExit) as error:
        cli.main()

    captured = capsys.readouterr()
    assert error.value.code == exit_code
    assert captured.out == ""
    assert message in captured.err


@pytest.mark.parametrize(
    ("status", "findings", "changes", "notice", "title"),
    [
        pytest.param(
            "generated", _FINDINGS, (), None, "Private draft generated", id="generated"
        ),
        pytest.param("resumed", (), (), None, "Private draft resumed", id="resumed"),
        pytest.param(
            "generated",
            _FINDINGS,
            ("head",),
            "The pull request changed during review: head.",
            "Private draft generated",
            id="stale",
        ),
        pytest.param(
            "generated",
            (),
            "unavailable",
            "Could not verify the current revision after review.",
            "Private draft generated",
            id="unchecked",
        ),
    ],
)
def test_cli_review_displays_private_draft(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    status: DraftReviewStatus,
    findings: tuple[Finding, ...],
    changes: tuple[SelectionChange, ...] | Literal["unavailable"],
    notice: str | None,
    title: str,
) -> None:
    run = replace(
        _RUN,
        draft=ReviewDraft("comment", "Summary text.", findings),
    )
    code, _ = _run_review(
        monkeypatch,
        tmp_path,
        DraftReview(status, run.saved_draft(), changes),
    )

    captured = capsys.readouterr()
    assert code == 0
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert lines[0] == title
    assert "PR: acme/repo#7" in lines
    assert f"Target branch: {_REVISION.target_branch}" in lines
    assert f"Merge base: {_REVISION.merge_base}" in lines
    assert f"Head: {_REVISION.head}" in lines
    assert (
        f"Comparison: https://git.example.com/acme/repo/compare/"
        f"{_REVISION.merge_base}...{_REVISION.head}"
    ) in lines
    assert (
        "Recommendation: comment (private draft; nothing was posted to GitHub)" in lines
    )
    sections = ["PR: acme/repo#7", "Recommendation:"]
    if findings:
        draft = run.draft
        assert draft is not None
        ordered = [finding.condition for finding in draft.ordered_findings()]
        sections.extend(["Findings:", *ordered])
        assert "Location: src/app.py:2" in captured.out
    else:
        sections.append("No findings")
    sections.extend(["Summary:", "Summary text."])
    if notice is not None:
        sections.append(f"Notice: {notice}")
    positions = [captured.out.index(section) for section in sections]
    assert positions == sorted(positions)


@pytest.mark.parametrize(
    ("outcome", "message"),
    [
        pytest.param(
            UnavailableReview("in_progress"), "already in progress", id="in-progress"
        ),
        pytest.param(
            FailedReview("The model is not configured.", "setup"),
            "Fix the problem",
            id="failed-setup",
        ),
        pytest.param(
            FailedReview("Binary changes are unsupported.", "input"),
            "When a supported comparison is available",
            id="unsupported-input",
        ),
        pytest.param(
            FailedReview("Legacy failure.", None),
            "Fix the problem",
            id="legacy-failure",
        ),
        pytest.param(UnavailableReview("abandoned"), "abandoned", id="abandoned"),
        pytest.param(
            UnavailableReview("no_retryable_run"),
            "no failed or abandoned",
            id="no-retry",
        ),
    ],
)
def test_cli_review_reports_next_action(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    outcome: ReviewOutcome,
    message: str,
) -> None:

    code, _ = _run_review(
        monkeypatch, tmp_path, outcome, url=f"{_URL}?view=files&tab=all#details"
    )

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert message in captured.err
    assert "https://git.example.com/acme/repo/pull/7" in captured.err
    assert "?view=" not in captured.err and "#details" not in captured.err
    if isinstance(outcome, FailedReview):
        assert outcome.message in captured.err
        assert ("--retry" in captured.err) == (outcome.kind != "input")


@pytest.mark.parametrize(
    ("error", "message"),
    [
        pytest.param(
            ReviewerError("Private review drafts require macOS or Linux.", "setup"),
            "require macOS or Linux",
            id="unsupported-platform",
        ),
        pytest.param(
            ReviewStoreError("Could not save the local review state."),
            "local review state",
            id="storage-failure",
        ),
        pytest.param(
            SavedDraftHistoryUnavailable(42, ReviewStoreError("Database busy.")),
            "draft 42 saved, but history could not be checked: Database busy. Run loop-reviewer draft show 42",
            id="history-failure-after-save",
        ),
    ],
)
def test_cli_review_reports_adapter_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    error: Exception,
    message: str,
) -> None:
    code, _ = _run_review(monkeypatch, tmp_path, None, error=error)

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert message in captured.err


@pytest.mark.parametrize(
    ("error", "next_step"),
    [
        pytest.param(
            KeyboardInterrupt(),
            "check the status before retrying",
            id="unknown-save-state",
        ),
        pytest.param(
            SavedDraftInterrupted(42), "loop-reviewer draft show 42", id="saved-draft"
        ),
    ],
)
def test_cli_review_reports_interruption(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    error: KeyboardInterrupt,
    next_step: str,
) -> None:
    code, _ = _run_review(monkeypatch, tmp_path, None, error=error)

    captured = capsys.readouterr()
    assert code == 130
    assert captured.out == ""
    assert "interrupted" in captured.err
    assert next_step in captured.err


def test_cli_review_wires_state_and_model(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    recorded: dict[str, object] = {}

    def build_reader(environment: dict[str, str]) -> object:
        return object()

    def build_store(path: Path) -> object:
        recorded["db"] = path
        return object()

    def build_execution(path: Path, factory: object) -> object:
        recorded["locks"] = path
        recorded["factory"] = factory
        return object()

    def build_application(
        reader: object, store: object, execution: object, *, select_current: object
    ) -> StubApplication:
        return StubApplication(UnavailableReview("in_progress"), recorded, None)

    class StubComparison:
        def read(self, selection: object) -> str:
            return ""

    def build_git(environment: dict[str, str], *, inherited_fd: int) -> StubComparison:
        recorded["git_environment"] = environment
        recorded["git_inherited_fd"] = inherited_fd
        return StubComparison()

    def build_pi(
        comparison: object,
        model: str | None,
        environment: dict[str, str],
        *,
        inherited_fd: int | None = None,
    ) -> object:
        assert callable(comparison)
        recorded["model"] = model
        recorded["inherited_fd"] = inherited_fd
        return object()

    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("LOOP_REVIEWER_PI_MODEL", "provider/model")
    monkeypatch.setattr(composition, "GhPullRequestReader", build_reader, raising=False)
    monkeypatch.setattr(composition, "SqliteReviewRunStore", build_store, raising=False)
    monkeypatch.setattr(
        composition, "LockedPiExecution", build_execution, raising=False
    )
    monkeypatch.setattr(
        composition, "ReviewPullRequest", build_application, raising=False
    )
    monkeypatch.setattr(composition, "GitComparison", build_git, raising=False)
    monkeypatch.setattr(composition, "PiReviewer", build_pi, raising=False)
    monkeypatch.setattr(sys, "argv", ["loop-reviewer", "review", _URL])

    assert cli.main() == 1
    factory = recorded["factory"]
    assert callable(factory)
    factory(7)

    assert recorded["db"] == state / "loop-reviewer" / "reviews.sqlite3"
    assert recorded["locks"] == state / "loop-reviewer" / "locks"
    assert recorded["model"] == "provider/model"
    assert recorded["inherited_fd"] == recorded["git_inherited_fd"] == 7
    assert capsys.readouterr().err.startswith("A review is already in progress")


def _invoke(monkeypatch: pytest.MonkeyPatch, *options: str) -> int:
    monkeypatch.setattr(sys, "argv", ["loop-reviewer", *options])
    return cli.main()


def _local_store(monkeypatch: pytest.MonkeyPatch, path: Path) -> SqliteReviewRunStore:
    monkeypatch.setenv("XDG_STATE_HOME", str(path))
    monkeypatch.delenv("LOOP_REVIEWER_PI_MODEL", raising=False)

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("local drafts must not construct external collaborators")

    for collaborator in (
        "GhPullRequestReader",
        "PiReviewer",
        "LockedPiExecution",
        "GitComparison",
    ):
        monkeypatch.setattr(composition, collaborator, forbidden)
    return SqliteReviewRunStore(path / "loop-reviewer" / "reviews.sqlite3")


class _PublicationGitHub:
    def __init__(self) -> None:
        self.actor = "reviewer"
        self.author = "teammate"
        self.head = _REVISION.head
        self.mode = "success"
        self.posts: list[dict[str, object]] = []
        self.reviews: list[dict[str, object]] = []

    def call(
        self, args: list[str], *, input: bytes | None = None, **options: object
    ) -> subprocess.CompletedProcess[bytes]:
        if args[1] == "auth":
            return subprocess.CompletedProcess(args, 0, b"fixture-token\n")
        endpoint = args[-1]
        status = 200
        data: object
        if args[5] == "POST":
            assert input is not None
            payload: dict[str, object] = json.loads(input)
            self.posts.append(payload)
            if self.mode == "interrupt":
                raise KeyboardInterrupt
            if self.mode in {"rejected", "rate-limited"}:
                status = 422 if self.mode == "rejected" else 429
                data = {"message": "private server details"}
            else:
                review = {
                    "id": 45,
                    "commit_id": payload["commit_id"],
                    "state": {"COMMENT": "COMMENTED", "APPROVE": "APPROVED"}[
                        str(payload["event"])
                    ],
                    "user": {"id": 1, "login": self.actor},
                    "body": payload["body"],
                }
                if self.mode != "unknown":
                    self.reviews.append(review)
                if self.mode in ("lost", "unknown"):
                    raise subprocess.TimeoutExpired(args, 15)
                data = review
        elif endpoint == "user":
            data = {"login": self.actor, "id": 1}
        elif endpoint.endswith("/pulls/7"):
            data = {
                "number": 7,
                "state": "open",
                "draft": False,
                "user": {"login": self.author},
                "base": {
                    "ref": _REVISION.target_branch,
                    "repo": {"id": 123, "full_name": "Acme/Repo"},
                },
                "head": {"sha": self.head},
            }
        elif "/files?" in endpoint:
            data = [
                {
                    "filename": "src/app.py",
                    "patch": "@@ -1 +1,2 @@\n context\n+addition",
                }
            ]
        elif "/git/ref/heads/" in endpoint:
            data = {
                "ref": f"refs/heads/{_REVISION.target_branch}",
                "object": {"sha": _REVISION.base_tip},
            }
        elif "/compare/" in endpoint:
            data = {
                "base_commit": {"sha": _REVISION.base_tip},
                "merge_base_commit": {"sha": _REVISION.merge_base},
            }
        elif "/reviews?" in endpoint:
            data = self.reviews
        else:
            raise AssertionError(f"Unexpected GitHub request: {endpoint}")
        return subprocess.CompletedProcess(
            args,
            0 if status == 200 else 1,
            f"HTTP/2.0 {status} Test\r\n\r\n{json.dumps(data)}".encode(),
        )

    def ordinary_read(
        self, args: list[str], **options: object
    ) -> subprocess.CompletedProcess[str]:
        result = self.call(args)
        return subprocess.CompletedProcess(
            args, result.returncode, result.stdout.decode(), ""
        )


def test_review_pins_reads_and_fetch_then_checks_current_account(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    remote = _PublicationGitHub()
    active = "reviewer"
    reads: list[str] = []
    fetched: list[str] = []
    configs: set[Path] = set()

    def capture(
        args: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args, 0, f"{active}\n".encode())

    def read(
        args: list[str], *, env: dict[str, str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal active
        config = Path(env["GH_CONFIG_DIR"])
        assert list(config.iterdir()) == []
        configs.add(config)
        remote.actor = env["GH_ENTERPRISE_TOKEN"]
        reads.append(remote.actor)
        if args[-1] == "user":
            active = "other"
        return remote.ordinary_read(args)

    class Comparison:
        def __init__(self, environment: dict[str, str], *, inherited_fd: int) -> None:
            self.environment = environment

        def read(self, selection: ReviewSelection) -> str:
            config = Path(self.environment["GH_CONFIG_DIR"])
            assert config in configs and config.is_dir()
            fetched.append(self.environment["GH_ENTERPRISE_TOKEN"])
            assert selection.account == "reviewer"
            return "pinned diff"

    class Reviewer:
        def __init__(
            self,
            comparison: Callable[[ReviewSelection], str],
            model: str | None,
            environment: dict[str, str],
            *,
            inherited_fd: int,
        ) -> None:
            assert environment.get("GH_CONFIG_DIR") == str(tmp_path)
            self.comparison = comparison

        def review(self, selection: ReviewSelection) -> ReviewDraft:
            assert self.comparison(selection) == "pinned diff"
            return ReviewDraft("comment", "Reviewed.", ())

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(
        "loop_reviewer.integrations.github_credentials.capture_review_process", capture
    )
    monkeypatch.setattr("loop_reviewer.integrations.gh_api.subprocess.run", read)
    monkeypatch.setattr(composition, "GitComparison", Comparison)
    monkeypatch.setattr(composition, "PiReviewer", Reviewer)
    assert _invoke(monkeypatch, "review", _URL) == 0
    assert reads == ["reviewer"] * 4 + ["other"] * 4
    assert fetched == ["reviewer"]
    assert len(configs) == 2 and all(not path.exists() for path in configs)
    assert "changed during review: authenticated account" in capsys.readouterr().out
    store = SqliteReviewRunStore(tmp_path / "loop-reviewer" / "reviews.sqlite3")
    selection = replace(_SELECTION, ref=replace(_SELECTION.ref, repository_id=123))
    saved = store.latest(selection.key)
    assert saved is not None and saved.status == "draft_ready"


@pytest.fixture
def publication_fixture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[_PublicationGitHub, SqliteReviewRunStore, ReviewRun]:
    remote = _PublicationGitHub()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("LOOP_REVIEWER_PI_MODEL", raising=False)
    monkeypatch.setattr(
        "loop_reviewer.integrations.github_credentials.capture_review_process",
        remote.call,
    )
    monkeypatch.setattr(
        "loop_reviewer.integrations.gh_api.capture_review_process", remote.call
    )
    monkeypatch.setattr(
        "loop_reviewer.integrations.gh_api.subprocess.run", remote.ordinary_read
    )

    def no_pi(*args: object, **kwargs: object) -> None:
        pytest.fail("publication must not construct Pi")

    monkeypatch.setattr(composition, "PiReviewer", no_pi)
    store = SqliteReviewRunStore(tmp_path / "loop-reviewer" / "reviews.sqlite3")
    selection = replace(_SELECTION, ref=replace(_SELECTION.ref, repository_id=123))
    run = store.start(selection, replaces=None).finish(
        ReviewDraft("approved", "  Résumé from draft\n\n", _FINDINGS)
    )
    store.save(run)
    return remote, store, run


def _publish_preview(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run: ReviewRun,
    action: str = "comment",
) -> tuple[str, str]:
    assert (
        _invoke(
            monkeypatch,
            "publish",
            "preview",
            str(run.id),
            "--version",
            str(run.draft_version),
            "--action",
            action,
        )
        == 0
    )
    output = capsys.readouterr().out
    last = output.splitlines()[-1]
    assert last.startswith("loop-reviewer publish confirm ")
    return last.split()[-1], output


def test_publish_preview_confirm_and_reopen_history(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    publication_fixture: tuple[_PublicationGitHub, SqliteReviewRunStore, ReviewRun],
) -> None:
    remote, store, run = publication_fixture
    id, preview = _publish_preview(monkeypatch, capsys, run)
    assert not remote.posts and store.completed(run.selection.identity) is None
    assert "Event: COMMENT" in preview and f"Head: {_REVISION.head}" in preview
    assert f"Target branch: {_REVISION.target_branch}" in preview
    assert f"Merge base: {_REVISION.merge_base}" in preview
    assert _invoke(monkeypatch, "publish", "confirm", id) == 0
    output = capsys.readouterr().out
    assert "Published — Comment" in output
    assert "#pullrequestreview-45" in output
    (post,) = remote.posts
    assert (
        preview.split("----- BEGIN REVIEW BODY -----\n", 1)[1].split(
            "\n----- END REVIEW BODY -----", 1
        )[0]
        == post["body"]
    )
    comments = post["comments"]
    assert isinstance(comments, list)
    (inline,) = comments
    assert (
        preview.split("----- BEGIN INLINE BODY -----\n", 1)[1].split(
            "\n----- END INLINE BODY -----", 1
        )[0]
        == inline["body"]
    )
    assert post["event"] == "COMMENT"
    remote.head = "d" * 40
    assert _invoke(monkeypatch, "review", _URL) == 0
    output = capsys.readouterr().out
    assert "Published — Comment" in output and "Recommendation: approved" in output
    assert _REVISION.head in output and remote.head not in output
    assert "GitHub review ID: 45" in output
    assert len(remote.posts) == 1


@pytest.mark.parametrize("evidence", ["unsent", "absent", "conflicting"])
def test_publish_recovery_guidance_does_not_resend(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    publication_fixture: tuple[_PublicationGitHub, SqliteReviewRunStore, ReviewRun],
    evidence: str,
) -> None:
    remote, store, run = publication_fixture
    id, _ = _publish_preview(monkeypatch, capsys, run)
    if evidence != "unsent":
        remote.mode = "unknown"
        assert _invoke(monkeypatch, "publish", "confirm", id) == 1
        capsys.readouterr()
        assert (
            _invoke(
                monkeypatch,
                "draft",
                "finish",
                str(run.id),
                "--version",
                str(run.draft_version),
            )
            == 1
        )
        assert f"publish recover {id}" in capsys.readouterr().err
    if evidence == "conflicting":
        remote.reviews.append({"id": 45})

    assert _invoke(monkeypatch, "publish", "recover", id) == 1
    error = capsys.readouterr().err
    expected = {
        "unsent": f"publish confirm {id}",
        "absent": f"publish retry {id} --accept-duplicate-risk",
        "conflicting": f"publish recover {id}",
    }
    assert expected[evidence] in error
    assert all(
        other not in error for mode, other in expected.items() if mode != evidence
    )
    assert len(remote.posts) == (0 if evidence == "unsent" else 1)
    assert store.completed(run.selection.identity) is None


def test_publish_lost_response_recovers_across_invocations(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    publication_fixture: tuple[_PublicationGitHub, SqliteReviewRunStore, ReviewRun],
) -> None:
    remote, store, run = publication_fixture
    id, _ = _publish_preview(monkeypatch, capsys, run, "approve")
    remote.mode = "lost"
    assert _invoke(monkeypatch, "publish", "confirm", id) == 1
    assert f"recover {id}" in capsys.readouterr().err
    assert store.completed(run.selection.identity) is None
    assert _invoke(monkeypatch, "publish", "recover", id) == 0
    assert "Published — Approval submitted" in capsys.readouterr().out
    assert _invoke(monkeypatch, "publish", "confirm", id) == 0
    assert len(remote.posts) == 1


@pytest.mark.parametrize(
    ("mode", "code", "guidance"),
    [
        ("self", 1, "draft finish"),
        ("wrong", 1, "Sign in as @reviewer"),
        ("stale", 1, "loop-reviewer review https://"),
        ("rejected", 1, "--summary-only"),
        ("rate-limited", 1, "GitHub's rate limit was reached"),
        ("unknown", 1, "publish recover"),
        ("interrupt", 130, "publish recover"),
        ("output", 1, "publish recover"),
    ],
    ids=[
        "self-approval",
        "wrong-account",
        "stale-head",
        "rejection",
        "rate-limit",
        "unresolved",
        "interruption",
        "receipt-output",
    ],
)
def test_publish_reports_required_next_action(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    publication_fixture: tuple[_PublicationGitHub, SqliteReviewRunStore, ReviewRun],
    mode: str,
    code: int,
    guidance: str,
) -> None:
    remote, store, run = publication_fixture
    if mode == "self":
        remote.author = remote.actor
        assert (
            _invoke(
                monkeypatch,
                "publish",
                "preview",
                str(run.id),
                "--version",
                "1",
                "--action",
                "approve",
            )
            == code
        )
        assert not remote.posts
    else:
        id, _ = _publish_preview(monkeypatch, capsys, run)
        if mode == "wrong":
            remote.actor = "other"
        elif mode == "stale":
            remote.head = "d" * 40
        elif mode != "output":
            remote.mode = mode
        if mode == "output":

            class BrokenOutput(StringIO):
                @override
                def write(self, text: str, /) -> int:
                    raise BrokenPipeError

            with redirect_stdout(BrokenOutput()):
                assert _invoke(monkeypatch, "publish", "confirm", id) == code
        else:
            assert _invoke(monkeypatch, "publish", "confirm", id) == code
        if mode in ("wrong", "stale"):
            assert not remote.posts
        elif mode == "output":
            assert store.completed(run.selection.identity) is not None
            assert _invoke(monkeypatch, "publish", "recover", id) == 0
            assert len(remote.posts) == 1
    error = capsys.readouterr().err
    assert guidance in error and "private server details" not in error


def test_local_draft_workflow_survives_restart(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    store = _local_store(monkeypatch, tmp_path)
    run = store.start(_SELECTION, replaces=None).finish(
        ReviewDraft("comment", "Old summary", _FINDINGS)
    )
    store.save(run)
    run_id = str(run.id)
    edit = tmp_path / "edit.txt"
    edit.write_text("New summary", encoding="utf-8")
    assert (
        _invoke(
            monkeypatch,
            "draft",
            "edit-summary",
            run_id,
            "--version",
            "1",
            "--file",
            str(edit),
        )
        == 0
    )
    assert "Version: 2" in capsys.readouterr().out
    edit.write_text("New high condition", encoding="utf-8")
    assert (
        _invoke(
            monkeypatch,
            "draft",
            "edit-finding",
            run_id,
            "1",
            "--version",
            "2",
            "--condition-file",
            str(edit),
        )
        == 0
    )
    assert "Version: 3" in capsys.readouterr().out
    assert (
        _invoke(monkeypatch, "draft", "drop-finding", run_id, "2", "--version", "3")
        == 0
    )
    preview = capsys.readouterr()
    assert preview.err == ""
    assert "Version: 4" in preview.out

    def fresh(command: str) -> subprocess.CompletedProcess[str]:
        executable = Path(sysconfig.get_path("scripts")) / (
            "loop-reviewer.exe" if sys.platform == "win32" else "loop-reviewer"
        )
        return subprocess.run(
            [executable, "draft", command, run_id],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

    shown = fresh("show")
    assert shown.returncode == 0, shown.stderr
    assert shown.stdout == preview.out
    assert f"Run ID: {run_id}" in shown.stdout
    assert "Account: reviewer" in shown.stdout
    assert f"Head: {_REVISION.head}" in shown.stdout
    assert "New high condition" in shown.stdout
    assert "Medium condition" not in shown.stdout
    copied = fresh("copy")
    assert copied.returncode == 0
    assert copied.stderr == ""
    assert copied.stdout == (
        "New summary\n\n## Findings\n\n"
        "1. [high] New high condition\n   Consequence: High consequence\n"
        "   Location: src/app.py:2\n"
        "2. [low] Low condition\n   Consequence: Low consequence\n"
    )
    assert (
        _invoke(monkeypatch, "draft", "drop-finding", run_id, "1", "--version", "1")
        == 1
    )
    stale = capsys.readouterr()
    assert stale.out == ""
    assert "draft show" in stale.err
    assert fresh("show").stdout == shown.stdout


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"], ids=["LF", "CRLF"])
def test_edit_accepts_utf8_text_line_endings(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    newline: bytes,
) -> None:
    store = _local_store(monkeypatch, tmp_path)
    original = store.start(_SELECTION, replaces=None).finish(
        ReviewDraft("comment", "Original", ())
    )
    store.save(original)
    text = tmp_path / "edit.txt"
    text.write_bytes(newline.join(["Résumé".encode(), b"Second line"]))
    assert (
        _invoke(
            monkeypatch,
            "draft",
            "edit-summary",
            str(original.id),
            "--version",
            "1",
            "--file",
            str(text),
        )
        == 0
    )
    receipt = capsys.readouterr()
    assert receipt.err == "" and "Résumé\nSecond line" in receipt.out
    edited = store.get_draft(original.id)
    assert edited is not None and edited.draft == ReviewDraft(
        "comment", "Résumé\nSecond line", ()
    )


def test_copy_keeps_multiline_feedback_under_its_finding(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    store = _local_store(monkeypatch, tmp_path)
    findings = (
        Finding("high", "First\n\nSecond paragraph", "Effect\n\nAnother effect", None),
    )
    run = store.start(_SELECTION, replaces=None).finish(
        ReviewDraft("comment", "Summary", findings)
    )
    store.save(run)
    assert _invoke(monkeypatch, "draft", "copy", str(run.id)) == 0
    assert capsys.readouterr().out.endswith(
        "1. [high] First\n\n   Second paragraph\n   Consequence: Effect\n\n   Another effect\n"
    )


@pytest.mark.parametrize("action", ["finish", "edit-summary"])
def test_draft_mutation_prints_utf8_receipt_with_ascii_stdout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, action: str
) -> None:
    store = _local_store(monkeypatch, tmp_path)
    run = store.start(_SELECTION, replaces=None).finish(
        ReviewDraft("comment", "Résumé", ())
    )
    store.save(run)
    options = [action, str(run.id), "--version", "1"]
    if action == "edit-summary":
        path = tmp_path / "summary.txt"
        path.write_text("Updated résumé", encoding="utf-8")
        options += ["--file", str(path)]
    monkeypatch.setenv("PYTHONIOENCODING", "ascii")
    result = _fresh_draft(*options)
    assert result.returncode == 0, result.stderr
    if action == "finish":
        assert "Finished locally — not published" in result.stdout
        assert store.completed(_SELECTION.identity) is not None
        assert f"/compare/{_REVISION.merge_base}...{_REVISION.head}" in result.stdout
    else:
        assert "Updated résumé" in result.stdout
        edited = store.get_draft(run.id)
        assert edited is not None and edited.draft_version == 2


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX named pipes")
def test_edit_rejects_fifo_without_waiting_for_writer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _local_store(monkeypatch, tmp_path)
    run = store.start(_SELECTION, replaces=None).finish(
        ReviewDraft("comment", "Summary", ())
    )
    store.save(run)
    pipe = tmp_path / "edit.fifo"
    os.mkfifo(pipe)
    result = _fresh_draft(
        "edit-summary", str(run.id), "--version", "1", "--file", str(pipe)
    )
    assert result.returncode == 1
    assert "regular file" in result.stderr
    assert store.get_draft(run.id) == run


def _fresh_draft(*options: str) -> subprocess.CompletedProcess[str]:
    executable = Path(sysconfig.get_path("scripts")) / (
        "loop-reviewer.exe" if sys.platform == "win32" else "loop-reviewer"
    )
    return subprocess.run(
        [executable, "draft", *options],
        capture_output=True,
        encoding="utf-8",
        check=False,
        timeout=5,
    )


@pytest.mark.skipif(os.name != "posix", reason="requires a POSIX subprocess pipe")
def test_finish_keeps_exit_status_when_stdout_pipe_is_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _local_store(monkeypatch, tmp_path)
    saved = store.start(_SELECTION, replaces=None).finish(
        ReviewDraft("comment", "Summary", ())
    )
    store.save(saved)
    executable = Path(sysconfig.get_path("scripts")) / "loop-reviewer"
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    try:
        result = subprocess.run(
            [executable, "draft", "finish", str(saved.id), "--version", "1"],
            stdout=write_fd,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            check=False,
            timeout=5,
        )
    finally:
        os.close(write_fd)
    assert result.returncode == 1
    assert "Review finished locally, but could not write the receipt" in result.stderr
    assert "Exception ignored" not in result.stderr
    assert store.completed(_SELECTION.identity) == saved.complete_locally()


@pytest.mark.parametrize(
    ("options", "message"),
    [
        pytest.param(
            ["edit-summary", "1", "--version", "1", "--file", "missing.txt"],
            "edit file",
            id="unreadable-file",
        ),
        pytest.param(
            ["edit-summary", "1", "--version", "1", "--file", "bad.txt"],
            "UTF-8",
            id="invalid-utf8",
        ),
        pytest.param(
            ["edit-summary", "1", "--version", "1", "--file", "large.txt"],
            "64 KiB",
            id="oversized-file",
        ),
        pytest.param(["show", "100"], "No saved draft", id="unknown-run"),
        pytest.param(["copy", "2"], "No saved draft", id="non-draft-run"),
        pytest.param(
            ["drop-finding", "1", "10", "--version", "1"],
            "Finding number",
            id="invalid-finding",
        ),
    ],
)
def test_local_draft_runtime_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    options: list[str],
    message: str,
) -> None:
    store = _local_store(monkeypatch, tmp_path)
    saved = store.start(_SELECTION, replaces=None).finish(
        ReviewDraft("comment", "Summary", _FINDINGS)
    )
    store.save(saved)
    store.start(
        replace(_SELECTION, revision=replace(_REVISION, head="e" * 40)), replaces=None
    )
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bad.txt").write_bytes(b"\xff")
    (tmp_path / "large.txt").write_bytes(b"x" * 65537)

    assert _invoke(monkeypatch, "draft", *options) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err
    assert store.get_draft(saved.id) == saved


@pytest.mark.parametrize(
    "options",
    [
        pytest.param(["show", "0"], id="non-positive-id"),
        pytest.param(["copy", "9223372036854775808"], id="out-of-range-id"),
        pytest.param(
            ["drop-finding", "1", "1", "--version", "no"], id="invalid-version"
        ),
        pytest.param(
            ["finish", "1", "--version", str(2**63)], id="out-of-range-version"
        ),
        pytest.param(
            ["edit-finding", "1", "1", "--version", "1"], id="missing-edit-field"
        ),
        pytest.param(
            ["edit-summary", "1", "--file", "feedback.txt"], id="missing-version"
        ),
    ],
)
def test_local_draft_argument_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    options: list[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        _invoke(monkeypatch, "draft", *options)
    assert error.value.code == 2
    assert "usage: loop-reviewer draft " in capsys.readouterr().err


@pytest.mark.parametrize("repo", ["repo", "renamed"], ids=["same-name", "renamed"])
def test_finishes_saved_draft_privately_and_reopens_history(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    repo: str,
) -> None:
    execution = composition.LockedPiExecution
    store = _local_store(monkeypatch, tmp_path)
    self_review = replace(
        _SELECTION,
        author=_SELECTION.account,
        ref=replace(_SELECTION.ref, repository_id=123),
    )
    older = store.start(self_review, replaces=None).finish(
        ReviewDraft("approved", "Old summary", _FINDINGS)
    )
    store.save(older)
    new_head = "d" * 40
    newer = store.start(
        replace(self_review, revision=replace(_REVISION, head=new_head)), replaces=None
    )
    store.save(newer.finish(ReviewDraft("comment", "Newer draft", ())))
    run_id = str(older.id)
    edit = tmp_path / "summary.txt"
    edit.write_text("Exact edited summary", encoding="utf-8")
    assert (
        _invoke(
            monkeypatch,
            "draft",
            "edit-summary",
            run_id,
            "--version",
            "1",
            "--file",
            str(edit),
        )
        == 0
    )
    assert (
        _invoke(monkeypatch, "draft", "drop-finding", run_id, "2", "--version", "2")
        == 0
    )
    capsys.readouterr()

    assert _invoke(monkeypatch, "draft", "finish", run_id, "--version", "1") == 1
    stale = capsys.readouterr()
    assert stale.out == "" and "draft show" in stale.err
    assert store.completed(self_review.identity) is None

    finish = ("draft", "finish", run_id, "--version", "3")
    assert _invoke(monkeypatch, *finish) == 0
    completed = capsys.readouterr()
    assert completed.err == ""
    assert "Finished locally — not published" in completed.out
    assert "Account: reviewer" in completed.out
    assert f"Head: {_REVISION.head}" in completed.out
    assert "Recommendation: approved" in completed.out
    assert "Exact edited summary" in completed.out
    assert "High condition" in completed.out
    assert "Medium condition" not in completed.out and new_head not in completed.out
    assert _invoke(monkeypatch, *finish) == 0
    assert capsys.readouterr() == completed

    for options in (
        ("show", run_id),
        ("copy", run_id),
        ("drop-finding", run_id, "1", "--version", "3"),
        ("finish", run_id, "--version", "2"),
        ("finish", str(newer.id), "--version", "1"),
    ):
        assert _invoke(monkeypatch, "draft", *options) == 1
        blocked = capsys.readouterr()
        assert blocked.out == ""
        assert "already completed" in blocked.err
        assert "review https://git.example.com/acme/repo/pull/7" in blocked.err

    calls: list[str] = []

    def github_get(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        assert command[:6] == [
            "gh",
            "api",
            "--hostname",
            "git.example.com",
            "--method",
            "GET",
        ]
        endpoint = command[-1]
        calls.append(endpoint)
        if endpoint == "user":
            payload = {"login": "REVIEWER"}
        else:
            assert endpoint == f"repos/acme/{repo}/pulls/7", command
            payload = {
                "number": 7,
                "state": "open",
                "draft": False,
                "user": {"login": "reviewer"},
                "base": {
                    "ref": _REVISION.target_branch,
                    "repo": {"full_name": f"Acme/{repo}", "id": 123},
                },
                "head": {"sha": new_head},
            }
        return subprocess.CompletedProcess(
            command, 0, "HTTP/2.0 200 OK\n\n" + json.dumps(payload), ""
        )

    monkeypatch.setattr(composition, "GhPullRequestReader", GhPullRequestReader)
    monkeypatch.setattr(composition, "LockedPiExecution", execution)
    monkeypatch.setattr(subprocess, "run", github_get)
    assert (
        _invoke(monkeypatch, "review", f"https://git.example.com/acme/{repo}/pull/7")
        == 0
    )
    assert capsys.readouterr() == completed
    assert calls == ["user", f"repos/acme/{repo}/pulls/7"]
