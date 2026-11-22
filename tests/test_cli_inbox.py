import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Never

import pytest

from loop_reviewer import cli, composition
from loop_reviewer.inbox.discovery import RepositoryDiscoveryError
from loop_reviewer.inbox.model import (
    DiscoveredPullRequest,
    RepositoryAddress,
    WatchedRepository,
)
from loop_reviewer.inbox.store import FailedScan, SuccessfulScan
from loop_reviewer.integrations.local_state.sqlite_inbox import SqliteInboxStore
from loop_reviewer.integrations.local_state.sqlite_reviews import SqliteReviewRunStore
from loop_reviewer.review.model import (
    GeneratedReview,
    PublicationReceipt,
    ReviewDraft,
    ReviewKey,
    ReviewSelection,
)
from loop_reviewer.review.publication_model import PublicationId, build_preview
from loop_reviewer.review.pull_request import (
    PullRequest,
    PullRequestRef,
    ReviewedRevision,
)

_MAIN = WatchedRepository(RepositoryAddress("github.com", "Org/Main"), 11, "alice")
_OTHER = WatchedRepository(RepositoryAddress("github.com", "Org/Other"), 22, "alice")


def _pr(
    repo: WatchedRepository, number: int, title: str, *, requested: bool = False
) -> DiscoveredPullRequest:
    ref = PullRequestRef.from_repository(
        repo.address.host, repo.address.slug, number, repository_id=repo.repository_id
    )
    return DiscoveredPullRequest(
        ref, title, "bob", "open", False, ("alice",) if requested else ()
    )


class FakeDiscovery:
    def __init__(self) -> None:
        self.verify_failure: RepositoryAddress | None = None
        self.rows: dict[
            WatchedRepository,
            tuple[DiscoveredPullRequest, ...] | RepositoryDiscoveryError,
        ] = {_MAIN: (), _OTHER: ()}
        self.scans: list[WatchedRepository] = []

    def verify(self, address: RepositoryAddress) -> WatchedRepository:
        if address == self.verify_failure:
            raise RepositoryDiscoveryError("Access denied")
        return next(repo for repo in self.rows if repo.address == address)

    def list_open(
        self, repository: WatchedRepository
    ) -> tuple[DiscoveredPullRequest, ...]:
        self.scans.append(repository)
        result = self.rows[repository]
        if isinstance(result, RepositoryDiscoveryError):
            raise result
        return result


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeDiscovery:
    github = FakeDiscovery()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    def discovery(_environment: object) -> FakeDiscovery:
        return github

    monkeypatch.setattr(composition, "GhRepositoryDiscovery", discovery)

    def unexpected(*args: object, **kwargs: object) -> Never:
        pytest.fail(
            "Inbox scan must not construct reviewer, lock, or publication session"
        )

    monkeypatch.setattr(composition, "PiReviewer", unexpected)
    monkeypatch.setattr(composition, "LockedPiExecution", unexpected)
    monkeypatch.setattr(composition, "open_gh_publication", unexpected)
    return github


def _run(monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["loop-reviewer", *args])
    return cli.main()


def _setup(
    monkeypatch: pytest.MonkeyPatch, *repos: WatchedRepository, key: str = "abc"
) -> int:
    options = [
        arg
        for repo in repos
        for arg in ("--repo", f"https://{repo.address.host}/{repo.address.slug}")
    ]
    return _run(monkeypatch, "inbox", "setup", "--jira-project", key, *options)


def test_cli_setup_and_scan_share_toml_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    github: FakeDiscovery,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "config" / "loop-reviewer" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        '# My settings\n[reviewer]\nengine = "pi"\nmodel = "provider/model"\n'
    )
    assert _setup(monkeypatch, _MAIN) == 0
    saved = path.read_text()
    assert "# My settings" in saved
    assert 'model = "provider/model"' in saved
    assert 'jira_project_key = "ABC"' in saved
    assert _MAIN.address.url in saved

    path.write_text(saved.replace('"ABC"', '"XYZ"').replace("Org/Main", "Org/Other"))
    assert _run(monkeypatch, "scan") == 0
    assert "[XYZ-…]" in capsys.readouterr().out
    assert github.scans == [_OTHER]


def test_toml_edit_does_not_implicitly_switch_github_accounts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    github: FakeDiscovery,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _setup(monkeypatch, _MAIN) == 0
    path = tmp_path / "config" / "loop-reviewer" / "config.toml"
    path.write_text(path.read_text().replace('"ABC"', '"XYZ"'))
    del github.rows[_MAIN]
    github.rows[replace(_MAIN, account="bob")] = ()
    assert _run(monkeypatch, "scan") == 1
    assert "GitHub account or repository identity changed" in capsys.readouterr().err
    saved = SqliteInboxStore(tmp_path / "loop-reviewer" / "reviews.sqlite3").settings()
    assert saved is not None
    assert saved.repositories == (_MAIN,)
    assert github.scans == []


def test_failed_setup_keeps_previous_settings(
    monkeypatch: pytest.MonkeyPatch,
    github: FakeDiscovery,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _setup(monkeypatch, _MAIN) == 0
    github.verify_failure = _OTHER.address
    assert _setup(monkeypatch, _MAIN, _OTHER, key="NEW") == 1
    assert "Access denied" in capsys.readouterr().err
    assert _run(monkeypatch, "scan") == 0
    output = capsys.readouterr().out
    assert "[ABC-…]" in output and "[NEW-" not in output
    assert github.scans == [_MAIN]


def test_partial_failure_shows_stale_only_with_access(
    monkeypatch: pytest.MonkeyPatch,
    github: FakeDiscovery,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _setup(monkeypatch, _MAIN, _OTHER) == 0
    github.rows[_MAIN] = (_pr(_MAIN, 1, "[ABC-7] Fix\x1b\nInjected"),)
    github.rows[_OTHER] = RepositoryDiscoveryError("Access denied")
    assert _run(monkeypatch, "scan") == 1
    output = capsys.readouterr().out
    assert "Scan failed" in output and "Never" in output
    assert "Fix\\x1b\\nInjected" in output and "\x1b" not in output
    assert "alice" in output and "bob" in output and "Not started" in output
    github.rows[_MAIN] = RepositoryDiscoveryError(
        "Too many requests", may_show_cached_rows=True
    )
    github.rows[_OTHER] = ()
    assert _run(monkeypatch, "scan") == 1
    output = capsys.readouterr().out
    assert "No eligible PRs awaiting review" in output
    assert "Too many requests" in output and "Stale" in output and "Fix" in output
    github.rows[_MAIN] = RepositoryDiscoveryError("Sign back in")
    assert _run(monkeypatch, "scan") == 1
    output = capsys.readouterr().out
    assert "Sign back in" in output and "Fix" not in output


@pytest.mark.parametrize("published", [False, True], ids=["local", "published"])
def test_completion_hides_cached_row_and_requested_reviews_sort_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    github: FakeDiscovery,
    capsys: pytest.CaptureFixture[str],
    published: bool,
) -> None:
    assert _setup(monkeypatch, _MAIN, _OTHER) == 0
    first = _pr(_MAIN, 2, "[ABC-8] Completed work")
    github.rows[_MAIN] = (
        first,
        _pr(_MAIN, 3, "[ABC-4] Prefix match"),
        _pr(_MAIN, 1, "[ABC-3] Earlier"),
    )
    github.rows[_OTHER] = (_pr(_OTHER, 1, "Direct request", requested=True),)
    store = SqliteReviewRunStore(tmp_path / "loop-reviewer" / "reviews.sqlite3")
    selection = ReviewSelection(
        first.ref,
        "alice",
        "bob",
        ReviewedRevision("main", "1" * 40, "1" * 40, "2" * 40),
    )
    saved = store.start(selection, replaces=None).finish(
        GeneratedReview(
            ReviewDraft("approved", "Looks good", ()), "provider/model", None
        )
    )
    store.save(saved)
    assert _run(monkeypatch, "scan") == 0
    output = capsys.readouterr().out
    assert (
        output.index("Direct request")
        < output.index("Earlier")
        < output.index("Prefix match")
    )
    assert "Last saved: draft ready" in output
    if published:
        preview = build_preview(
            PublicationId("75f718d3-9633-42ad-9097-22f320d068a8"),
            saved.saved_draft(),
            selection,
            17,
            "comment",
            frozenset(),
        )
        store.save_preview(preview)
        store.reserve_send(preview.id, "preview")
        store.record_published(preview.id, PublicationReceipt(45, "comment"))
    else:
        store.complete(saved.complete_locally())
    github.rows[_MAIN] = RepositoryDiscoveryError(
        "Timed out", may_show_cached_rows=True
    )
    assert _run(monkeypatch, "scan") == 1
    output = capsys.readouterr().out
    assert "Completed work" not in output and "Prefix match" in output


@pytest.mark.parametrize("newer_outcome", ["pending", "finished", "interrupted"])
def test_scan_projects_newer_attempt_instead_of_losing_result(
    newer_outcome: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    github: FakeDiscovery,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _setup(monkeypatch, _MAIN) == 0
    old = _pr(_MAIN, 1, "[ABC-1] Earlier snapshot")
    github.rows[_MAIN] = (old,)
    assert _run(monkeypatch, "scan") == 0
    capsys.readouterr()
    store = SqliteInboxStore(tmp_path / "loop-reviewer" / "reviews.sqlite3")
    settings = store.settings()
    assert settings is not None

    def concurrent_scan(
        repository: WatchedRepository,
    ) -> tuple[DiscoveredPullRequest, ...]:
        # Simulate an older process that writes scan tickets without the shared lock.
        ticket = store.begin_scan(settings.version, repository.key)
        assert ticket is not None
        if newer_outcome == "interrupted":
            assert store.record_scan(
                ticket, FailedScan(datetime.now(UTC), "Scan interrupted.", False)
            )
        elif newer_outcome == "finished":
            newest = _pr(_MAIN, 3, "[ABC-3] Newer scan")
            assert store.record_scan(
                ticket, SuccessfulScan(datetime.now(UTC), (newest,))
            )
        return (_pr(_MAIN, 2, "[ABC-2] Losing scan"),)

    monkeypatch.setattr(github, "list_open", concurrent_scan)
    assert _run(monkeypatch, "scan") == (0 if newer_outcome == "finished" else 1)
    output = capsys.readouterr().out
    assert "Earlier snapshot" not in output and "Losing scan" not in output
    assert {
        "pending": "Scan in progress",
        "finished": "Newer scan",
        "interrupted": "Scan failed: Scan interrupted.",
    }[newer_outcome] in output


@pytest.mark.parametrize("command", ["scan", "watch"])
def test_scan_before_setup_does_not_call_github(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    github: FakeDiscovery,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run(monkeypatch, command) == 1
    assert "inbox setup" in capsys.readouterr().err
    assert not github.scans


class ReviewReader:
    def __init__(self, selection: ReviewSelection) -> None:
        self.selection = selection
        self.revision_calls = 0

    def read_pull_request(self, ref: PullRequestRef) -> tuple[str, PullRequest]:
        assert ref.url == self.selection.ref.url
        return self.selection.account, PullRequest(
            self.selection.ref,
            self.selection.author,
            "open",
            False,
            self.selection.revision.target_branch,
            self.selection.revision.head,
        )

    def read_revision(self, pull_request: PullRequest) -> ReviewedRevision:
        self.revision_calls += 1
        return self.selection.revision


@pytest.mark.parametrize(
    "scenario", ["resume", "account-switch", "replaced-repo", "completed"]
)
def test_inbox_open_revalidates_before_history_or_reviewer(
    scenario: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    github: FakeDiscovery,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _setup(monkeypatch, _MAIN) == 0
    pr = _pr(_MAIN, 5, "[ABC-5] Existing review")
    github.rows[_MAIN] = (pr,)
    assert _run(monkeypatch, "scan") == 0
    assert f"loop-reviewer inbox open {pr.ref.url}" in capsys.readouterr().out
    selection = ReviewSelection(
        pr.ref, "alice", "bob", ReviewedRevision("main", "1" * 40, "1" * 40, "2" * 40)
    )
    reader = ReviewReader(selection)
    store = SqliteReviewRunStore(tmp_path / "loop-reviewer" / "reviews.sqlite3")
    draft = ReviewDraft("approved", "Existing feedback", ())

    class UnusedExecution:
        def __init__(self, *args: object) -> None: ...

        def acquire(self, *args: object) -> Never:
            pytest.fail("Must not acquire reviewer for an existing or mismatched row")

    def active_reader(_environment: object) -> ReviewReader:
        return reader

    def capture(
        args: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args, 0, b"fixture-token\n")

    monkeypatch.setattr(composition, "GhPullRequestReader", active_reader)
    monkeypatch.setattr(
        "loop_reviewer.integrations.github_credentials.capture_review_process",
        capture,
    )
    monkeypatch.setattr(composition, "LockedPiExecution", UnusedExecution)
    if scenario == "resume":

        class FakeReviewer:
            def review(self, selected: ReviewSelection) -> GeneratedReview:
                assert selected == selection
                return GeneratedReview(draft, "provider/model", None)

        class InitialExecution:
            def __init__(self, *args: object) -> None: ...

            @contextmanager
            def acquire(self, key: ReviewKey) -> Generator[FakeReviewer]:
                yield FakeReviewer()

        monkeypatch.setattr(composition, "LockedPiExecution", InitialExecution)
        assert _run(monkeypatch, "review", pr.ref.url) == 0
        assert "Private draft generated" in capsys.readouterr().out
        saved = store.latest(selection.key)
        assert saved is not None
        monkeypatch.setattr(composition, "LockedPiExecution", UnusedExecution)
        assert _run(monkeypatch, "inbox", "later", pr.ref.url) == 0
        assert _run(monkeypatch, "inbox", "open", pr.ref.url) == 0
        row_output = capsys.readouterr().out
        assert (
            f"Run ID: {saved.id}" in row_output
            and "Private draft resumed" in row_output
        )
        assert reader.revision_calls == 3
    elif scenario == "completed":
        saved = store.start(selection, replaces=None).finish(
            GeneratedReview(draft, "provider/model", None)
        )
        store.save(saved)
        store.complete(saved.complete_locally())
        assert _run(monkeypatch, "inbox", "open", pr.ref.url) == 0
        output = capsys.readouterr().out
        assert "Existing feedback" in output and "Finished locally" in output
        assert reader.revision_calls == 0
    else:

        def unexpected_history(*args: object) -> Never:
            pytest.fail("Mismatched identity must not look up review history")

        monkeypatch.setattr(SqliteReviewRunStore, "completed", unexpected_history)
        reader.selection = (
            replace(selection, account="another")
            if scenario == "account-switch"
            else replace(selection, ref=replace(pr.ref, repository_id=55))
        )
        assert _run(monkeypatch, "inbox", "open", pr.ref.url) == 1
        assert "Sign back in" in capsys.readouterr().err
        assert reader.revision_calls == 0


def test_later_and_restore_are_local_and_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    github: FakeDiscovery,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _setup(monkeypatch, _MAIN) == 0
    row = _pr(_MAIN, 5, "[ABC-5] To defer")
    github.rows[_MAIN] = (row,)
    assert _run(monkeypatch, "scan") == 0
    capsys.readouterr()
    for _ in range(2):
        assert _run(monkeypatch, "inbox", "later", row.ref.url) == 0
        assert "Later" in capsys.readouterr().out
    assert github.scans == [_MAIN]
    assert _run(monkeypatch, "scan") == 0
    output = capsys.readouterr().out
    assert "Active" not in output and "Later" in output and "To defer" in output
    for _ in range(2):
        assert _run(monkeypatch, "inbox", "restore", row.ref.url) == 0
        assert "active" in capsys.readouterr().out
    assert _run(monkeypatch, "scan") == 0
    assert "Active" in capsys.readouterr().out
    assert _run(monkeypatch, "inbox", "later", row.ref.url.removesuffix("5") + "6") == 1
    assert "scan" in capsys.readouterr().err


def test_watch_retries_superseded_scan_and_only_nudges_for_new_or_changed_active_rows(
    monkeypatch: pytest.MonkeyPatch,
    github: FakeDiscovery,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    assert _setup(monkeypatch, _MAIN, _OTHER) == 0
    later = _pr(_MAIN, 4, "[ABC-4] Deferred work")
    now = _pr(_MAIN, 8, "[ABC-8] Current work")
    github.rows[_MAIN] = (later, now)
    assert _run(monkeypatch, "scan") == 0
    assert _run(monkeypatch, "inbox", "later", later.ref.url) == 0
    capsys.readouterr()
    clock = 0.0
    cycles: list[str] = []
    original = github.list_open

    def maybe_superseded(
        repository: WatchedRepository,
    ) -> tuple[DiscoveredPullRequest, ...]:
        if repository == _MAIN and len(cycles) == 3:
            store = SqliteInboxStore(tmp_path / "loop-reviewer" / "reviews.sqlite3")
            settings = store.settings()
            assert settings is not None
            store.replace_settings(settings.projects, settings.repositories)
        return original(repository)

    def monotonic() -> float:
        return clock

    def sleep(seconds: float) -> None:
        nonlocal clock
        assert seconds == 1
        clock += seconds
        if (clock - 1) % 120:
            return
        result = capsys.readouterr()
        cycles.append(result.out + result.err)
        if len(cycles) == 1:
            # A newer comparison on a deferred PR must not produce a nudge.
            store = SqliteReviewRunStore(tmp_path / "loop-reviewer" / "reviews.sqlite3")
            selection = ReviewSelection(
                later.ref,
                "alice",
                "bob",
                ReviewedRevision("main", "1" * 40, "1" * 40, "2" * 40),
            )
            store.start(selection, replaces=None)
            pushed = replace(
                selection, revision=replace(selection.revision, head="3" * 40)
            )
            store.start(pushed, replaces=None)
        elif len(cycles) == 2:
            github.rows[_MAIN] = RepositoryDiscoveryError(
                "Temporarily unavailable", may_show_cached_rows=True
            )
        elif len(cycles) == 4:
            github.rows[_MAIN] = (later, replace(now, title="[ABC-8] Changed work"))
        elif len(cycles) == 5:
            raise KeyboardInterrupt

    monkeypatch.setattr(github, "list_open", maybe_superseded)
    monkeypatch.setattr(cli, "monotonic", monotonic, raising=False)
    monkeypatch.setattr(cli, "sleep", sleep, raising=False)
    assert _run(monkeypatch, "watch") == 130
    assert "interrupted" in capsys.readouterr().err
    first, unchanged, stale, superseded, changed = cycles
    assert "New or updated" in first and "Current work" in first
    assert "1 deferred" in unchanged and "New or updated" not in unchanged
    assert "Scan failed" in stale and "Stale" in stale and "New or updated" not in stale
    assert "settings changed" in superseded
    assert "New or updated" in changed and "Changed work" in changed
    assert not any("Deferred work" in cycle for cycle in cycles)
    assert _run(monkeypatch, "scan") == 0
    assert "Later\n[ABC-4] Deferred work" in capsys.readouterr().out
