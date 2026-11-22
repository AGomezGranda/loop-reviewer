"""Wire the review and inbox workflows for local clients."""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from loop_reviewer.inbox.actions import InboxActions
from loop_reviewer.inbox.discovery import ConfigureInbox, ReadInbox, ScanInbox
from loop_reviewer.integrations.git_comparison import GitComparison
from loop_reviewer.integrations.github import GhPullRequestReader
from loop_reviewer.integrations.github_credentials import open_gh_environment
from loop_reviewer.integrations.github_discovery import GhRepositoryDiscovery
from loop_reviewer.integrations.github_publication import open_gh_publication
from loop_reviewer.integrations.local_state.inbox_watch import InboxWatch
from loop_reviewer.integrations.local_state.publication_lock import LockedPublication
from loop_reviewer.integrations.local_state.review_lock import LockedPiExecution
from loop_reviewer.integrations.local_state.sqlite_inbox import SqliteInboxStore
from loop_reviewer.integrations.local_state.sqlite_reviews import (
    SqliteReviewRunStore,
)
from loop_reviewer.integrations.pi_reviewer import PiReviewer
from loop_reviewer.review.drafts import EditReviewDraft
from loop_reviewer.review.generate import Reviewer, ReviewOutcome, ReviewPullRequest
from loop_reviewer.review.history import ReviewHistory
from loop_reviewer.review.model import ReviewIdentity, ReviewSelection
from loop_reviewer.review.publication import PreparePublication, PublishReview
from loop_reviewer.review.publication_model import PublicationId
from loop_reviewer.review.pull_request import PullRequestRef
from loop_reviewer.review.selection import open_pull_request


class Workflows:
    def __init__(self, state: Path, environment: dict[str, str]) -> None:
        self._state = state
        self._environment = environment
        self._database = state / "reviews.sqlite3"

    def select(self, ref: PullRequestRef) -> ReviewSelection:
        with open_gh_environment(
            self._environment, ref.host, prefix="loop-reviewer-read-gh-"
        ) as (pinned, _):
            return open_pull_request(ref, GhPullRequestReader(pinned))

    def review(
        self,
        ref: PullRequestRef,
        *,
        retry: bool = False,
        expected_identity: ReviewIdentity | None = None,
    ) -> ReviewOutcome:
        environment = self._environment
        with open_gh_environment(
            environment, ref.host, prefix="loop-reviewer-read-gh-"
        ) as (pinned, _):

            def reviewer_factory(descriptor: int) -> Reviewer:
                return PiReviewer(
                    GitComparison(pinned, inherited_fd=descriptor).read,
                    environment.get("LOOP_REVIEWER_PI_MODEL") or None,
                    environment,
                    inherited_fd=descriptor,
                )

            workflow = ReviewPullRequest(
                GhPullRequestReader(pinned),
                SqliteReviewRunStore(self._database),
                LockedPiExecution(self._state / "locks", reviewer_factory),
                select_current=self.select,
            )
            return workflow.run(ref, retry=retry, expected_identity=expected_identity)

    def edit_review_draft(self) -> EditReviewDraft:
        return EditReviewDraft(SqliteReviewRunStore(self._database))

    def review_history(self) -> ReviewHistory:
        store = SqliteReviewRunStore(self._database)
        return ReviewHistory(store, store)

    def prepare_publication(self) -> PreparePublication:
        return PreparePublication(
            SqliteReviewRunStore(self._database),
            lambda host: open_gh_publication(self._environment, host),
            lambda: PublicationId(str(uuid4())),
        )

    def publish_review(self) -> PublishReview:
        guard = LockedPublication(
            self._state / "publication-locks",
            lambda fd: (
                lambda host: open_gh_publication(
                    self._environment, host, inherited_fd=fd
                )
            ),
        )
        return PublishReview(SqliteReviewRunStore(self._database), guard)

    def configure_inbox(self) -> ConfigureInbox:
        return ConfigureInbox(
            GhRepositoryDiscovery(self._environment), SqliteInboxStore(self._database)
        )

    def scan_inbox(self) -> ScanInbox:
        return ScanInbox(
            GhRepositoryDiscovery(self._environment),
            SqliteInboxStore(self._database),
            SqliteReviewRunStore(self._database),
            now=lambda: datetime.now(UTC),
        )

    def inbox_watch(self, *, monotonic: Callable[[], float]) -> InboxWatch:
        return InboxWatch(
            self._state / "inbox-watch", self.scan_inbox().run, monotonic=monotonic
        )

    def read_inbox(self) -> ReadInbox:
        return ReadInbox(
            SqliteInboxStore(self._database), SqliteReviewRunStore(self._database)
        )

    def inbox_actions(self) -> InboxActions[ReviewOutcome]:
        return InboxActions(
            SqliteInboxStore(self._database),
            lambda ref, identity: self.review(ref, expected_identity=identity),
        )
