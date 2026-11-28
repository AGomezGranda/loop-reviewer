from datetime import UTC, datetime
from pathlib import Path

import pytest

from loop_reviewer.inbox.actions import InboxActions
from loop_reviewer.inbox.discovery import ReadInbox, ScanInbox
from loop_reviewer.inbox.model import (
    DiscoveredPullRequest,
    InboxReason,
    JiraProjectKey,
    RepositoryAddress,
    WatchedRepository,
)
from loop_reviewer.inbox.store import FailedScan, SuccessfulScan
from loop_reviewer.integrations.local_state.sqlite_inbox import SqliteInboxStore
from loop_reviewer.integrations.local_state.sqlite_reviews import SqliteReviewRunStore
from loop_reviewer.review.model import (
    GeneratedReview,
    ReviewDraft,
    ReviewIdentity,
    ReviewSelection,
)
from loop_reviewer.review.pull_request import PullRequestRef, ReviewedRevision
from loop_reviewer.review.store import ReviewActivity


@pytest.mark.parametrize(
    ("title", "matches"),
    [
        pytest.param("[ABC_2-007] Fix", True, id="leading-zeroes"),
        pytest.param("[ABC_2-1]", True, id="prefix-only"),
        pytest.param("[abc_2-1] Fix", False, id="case-sensitive"),
        pytest.param(" [ABC_2-1] Fix", False, id="not-at-start"),
        pytest.param("[ABC_2-١] Fix", False, id="non-ascii-digit"),
        pytest.param("[ABC_2-] Fix", False, id="empty-number"),
        pytest.param("[ABC_2-+1] Fix", False, id="signed-number"),
        pytest.param("[ABC_22-1] Fix", False, id="longer-key"),
    ],
)
def test_project_prefix(title: str, matches: bool) -> None:
    assert JiraProjectKey(" abc_2 ").matches(title) is matches


@pytest.mark.parametrize("key", ["", "1ABC", "a-b", "é", "ß", "a" * 33])
def test_invalid_project_key(key: str) -> None:
    with pytest.raises(ValueError):
        JiraProjectKey(key)


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/a/b",
        "https://user@github.com/a/b",
        "https://github.com:443/a/b",
        "https://github.com/a/b?",
        "https://github.com/a/b#",
        "https://github.com/a/..",
    ],
)
def test_invalid_repository_url(url: str) -> None:
    with pytest.raises(ValueError):
        RepositoryAddress.from_url(url)


@pytest.mark.parametrize(
    ("title", "author", "state", "draft", "requested", "expected"),
    [
        pytest.param(
            "[ABC-1] Fix",
            "Alice",
            "open",
            False,
            ("ALICE",),
            (
                InboxReason("direct_request", "Your review was requested"),
                InboxReason("authored", "Your PR"),
                InboxReason("jira_project", "Matches [ABC-…]"),
            ),
            id="all-reasons",
        ),
        pytest.param(
            "Other work",
            "bob",
            "open",
            False,
            ("ALICE",),
            (InboxReason("direct_request", "Your review was requested"),),
            id="direct-request",
        ),
        pytest.param(
            "Other work",
            "Alice",
            "open",
            False,
            (),
            (InboxReason("authored", "Your PR"),),
            id="own-pr",
        ),
        pytest.param("Other work", "bob", "open", False, (), (), id="unrelated"),
        pytest.param(
            "[ABC-1] Fix", "alice", "closed", False, ("alice",), (), id="closed"
        ),
        pytest.param("[ABC-1] Fix", "alice", "open", True, ("alice",), (), id="draft"),
    ],
)
def test_eligibility(
    title: str,
    author: str,
    state: str,
    draft: bool,
    requested: tuple[str, ...],
    expected: tuple[InboxReason, ...],
) -> None:
    pr = DiscoveredPullRequest(
        PullRequestRef("github.com", "a", "b", 1, 22),
        title,
        author,
        state,
        draft,
        requested,
    )
    assert pr.reasons((JiraProjectKey("ABC"),), "alice") == expected


@pytest.mark.parametrize("phase", ["discovery", "projection"])
def test_title_only_edit_during_scan_keeps_results_for_all_repositories(
    tmp_path: Path,
    phase: str,
) -> None:
    path = tmp_path / "state" / "reviews.sqlite3"
    inbox = SqliteInboxStore(path)
    projects = (JiraProjectKey("ABC"),)
    first = WatchedRepository(RepositoryAddress("github.com", "org/first"), 10, "alice")
    second = WatchedRepository(
        RepositoryAddress("github.com", "org/second"), 20, "alice"
    )
    before = inbox.replace_settings(projects, (first, second))

    def add_filter() -> None:
        SqliteInboxStore(path).replace_settings(
            projects, (first, second), excluded_title_terms=("ignore me",)
        )

    class Discovery:
        def verify(self, address: RepositoryAddress) -> WatchedRepository:
            pytest.fail(f"Unexpected verification of {address.url}")

        def list_open(
            self, repository: WatchedRepository
        ) -> tuple[DiscoveredPullRequest, ...]:
            if repository == first and phase == "discovery":
                add_filter()
            return tuple(
                DiscoveredPullRequest(
                    PullRequestRef.from_repository(
                        repository.address.host,
                        repository.address.slug,
                        number,
                        repository_id=repository.repository_id,
                    ),
                    title,
                    "bob",
                    "open",
                    False,
                    ("alice",),
                )
                for number, title in enumerate(("Keep", "Ignore me"), 1)
            )

    class Activity:
        changed = False

        def activity(
            self, identities: tuple[ReviewIdentity, ...]
        ) -> dict[ReviewIdentity, ReviewActivity]:
            if phase == "projection" and not self.changed:
                self.changed = True
                add_filter()
            return {}

    report = ScanInbox(
        Discovery(), inbox, Activity(), now=lambda: datetime.now(UTC)
    ).run()
    assert report.succeeded
    assert report.settings.version > before.version
    assert [row.pull_request.title for row in report.active] == ["Keep", "Keep"]
    cleared = inbox.replace_settings(projects, (first, second))
    assert cleared.scan_version == before.scan_version
    assert len(ReadInbox(inbox, SqliteReviewRunStore(path)).run().active) == 4


def test_read_inbox_reflects_local_actions_without_discovery(tmp_path: Path) -> None:
    path = tmp_path / "reviews.sqlite3"
    inbox = SqliteInboxStore(path)
    reviews = SqliteReviewRunStore(path)
    good = WatchedRepository(RepositoryAddress("github.com", "a/b"), 10, "alice")
    failed = WatchedRepository(RepositoryAddress("github.com", "a/c"), 20, "alice")
    pending = WatchedRepository(RepositoryAddress("github.com", "a/d"), 30, "alice")
    settings = inbox.replace_settings((JiraProjectKey("ABC"),), (good, failed, pending))
    ref = PullRequestRef("github.com", "a", "b", 1, 10)
    row = DiscoveredPullRequest(ref, "[ABC-1] Fix", "bob", "open", False, ())
    first = inbox.begin_scan(settings.scan_version, good.key)
    second = inbox.begin_scan(settings.scan_version, failed.key)
    assert first is not None and second is not None
    now = datetime.now(UTC)
    inbox.record_scan(first, SuccessfulScan(now, (row,)))
    inbox.record_scan(second, FailedScan(now, "Access denied", False))
    reader = ReadInbox(inbox, reviews)
    report = reader.run()
    assert {status.outcome for status in report.statuses} == {
        "success",
        "failed",
        "pending",
    }
    assert [item.pull_request for item in report.active] == [row]
    actions = InboxActions(inbox, lambda *_: None)
    actions.defer(ref)
    assert [item.pull_request for item in reader.run().deferred] == [row]
    actions.restore(ref)
    select = ReviewSelection(
        ref, "alice", "bob", ReviewedRevision("main", "a" * 40, "b" * 40, "c" * 40)
    )
    run = reviews.start(select, replaces=None)
    assert reader.run().active[0].last_saved == "running"
    run = run.finish(
        GeneratedReview(
            ReviewDraft("approved", "Looks good", ()), "provider/model", None
        )
    )
    reviews.save(run)
    reviews.complete(run.complete_locally())
    assert not reader.run().active
