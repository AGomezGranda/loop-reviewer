"""Wire the review and inbox workflows for local clients."""

import os
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from uuid import uuid4

from loop_reviewer.inbox.actions import InboxActions
from loop_reviewer.inbox.discovery import ConfigureInbox, ReadInbox, ScanInbox
from loop_reviewer.inbox.model import InboxReport
from loop_reviewer.integrations.gh_api import GhApi
from loop_reviewer.integrations.git_comparison import GitComparison
from loop_reviewer.integrations.github import GhPullRequestReader
from loop_reviewer.integrations.github_credentials import open_gh_environment
from loop_reviewer.integrations.github_discovery import GhRepositoryDiscovery
from loop_reviewer.integrations.github_discussion import GhDiscussionReader
from loop_reviewer.integrations.github_publication import open_gh_publication
from loop_reviewer.integrations.local_state.inbox_watch import InboxWatch
from loop_reviewer.integrations.local_state.publication_lock import LockedPublication
from loop_reviewer.integrations.local_state.review_lock import (
    LockedReviewExecution,
    review_lock_held,
)
from loop_reviewer.integrations.local_state.sqlite_inbox import SqliteInboxStore
from loop_reviewer.integrations.local_state.sqlite_reviews import (
    SqliteReviewRunStore,
)
from loop_reviewer.integrations.local_state.toml_settings import TomlSettings
from loop_reviewer.review.active import ActiveReviews
from loop_reviewer.review.drafts import EditReviewDraft
from loop_reviewer.review.generate import (
    RetryTarget,
    Reviewer,
    ReviewerError,
    ReviewOutcome,
    ReviewPullRequest,
)
from loop_reviewer.review.history import ReviewHistory
from loop_reviewer.review.hosted_report import HostedReportReviewer
from loop_reviewer.review.model import ReviewIdentity, ReviewSelection
from loop_reviewer.review.publication import PreparePublication, PublishReview
from loop_reviewer.review.publication_model import PublicationId
from loop_reviewer.review.pull_request import PullRequestRef
from loop_reviewer.review.selection import GitHubReadError, open_pull_request
from loop_reviewer.review.stats import ReviewStats
from loop_reviewer.reviewer.discussion_contract import (
    DiscussionSnapshotV1,
    DiscussionTarget,
    unavailable_discussion,
)
from loop_reviewer.reviewer.integrations.git_inventory import open_local_comparison
from loop_reviewer.reviewer.integrations.pi_review import (
    PI_ENV,
    PI_SETTINGS,
    PiReview,
    PiSetupError,
)
from loop_reviewer.reviewer.inventory_contract import (
    MAX_RUNTIME_SPEND,
    InventoryBudget,
    PinnedComparison,
)
from loop_reviewer.reviewer.model_contract import ReviewModel
from loop_reviewer.reviewer.pinned_source import PinnedSource
from loop_reviewer.reviewer.semantic_batch import minimum_investigation_spend

_MODEL_SPEND = "LOOP_REVIEWER_MAX_MODEL_SPEND_USD_MICROS"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _hosted_model(
    environment: dict[str, str],
) -> tuple[InventoryBudget, ReviewModel | None, str | None]:
    raw = environment.get(_MODEL_SPEND, "0")
    try:
        if not (raw.isascii() and raw.isdecimal()):
            raise ValueError
        budget = InventoryBudget(max_model_spend_usd_micros=int(raw))
    except ValueError:
        raise ReviewerError(
            f"Set {_MODEL_SPEND} to an integer from 0 to {MAX_RUNTIME_SPEND}.", "setup"
        ) from None
    if not budget.max_model_spend_usd_micros:
        return budget, None, None
    try:
        model = PiReview.require_configured(environment)
    except PiSetupError as error:
        raise ReviewerError(str(error), "setup") from error
    return budget, model, model.tokens.profile


class Workflows:
    def __init__(
        self, state: Path, environment: dict[str, str], *, config_path: Path
    ) -> None:
        self._state = state
        self._model_environment = {
            name: environment[name]
            for name in (_MODEL_SPEND, *PI_ENV)
            if name in environment
        }
        self._environment = {
            name: value
            for name, value in environment.items()
            if name not in (_MODEL_SPEND, *PI_SETTINGS)
        }
        self._database = state / "reviews.sqlite3"
        store = SqliteInboxStore(self._database)
        self._config = TomlSettings(
            config_path,
            ConfigureInbox(GhRepositoryDiscovery(self._environment)),
            store,
        )

    @staticmethod
    def check_review_model(environment: dict[str, str]) -> str:
        budget, model, name = _hosted_model(environment)
        if model is None:
            return f"AI analysis is off. Set {_MODEL_SPEND} to a positive allowance to enable it."
        minimum = minimum_investigation_spend(model)
        if minimum is None:
            raise ReviewerError(
                "The selected model cannot fit the review instructions.", "setup"
            )
        if budget.max_model_spend_usd_micros < minimum:
            raise ReviewerError(
                "The allowance cannot start an investigation with verification reserved. "
                f"Set {_MODEL_SPEND} to at least {minimum} for this model; PR evidence needs additional allowance.",
                "setup",
            )
        return (
            f"Pi model {name} is configured. The allowance covers an empty investigation "
            f"with verification reserved ({minimum} USD micros); PR evidence adds cost. "
            "No provider request was made."
        )

    def select(self, ref: PullRequestRef) -> ReviewSelection:
        with open_gh_environment(
            self._environment, ref.host, prefix="loop-reviewer-read-gh-"
        ) as (pinned, _):
            return open_pull_request(ref, GhPullRequestReader(pinned))

    def capture_discussion(self, selection: ReviewSelection) -> DiscussionSnapshotV1:
        ref, revision = selection.ref, selection.revision
        if ref.repository_id is None:
            raise ValueError("Discussion capture requires a canonical hosted selection")
        target = DiscussionTarget(
            host=ref.host,
            owner=ref.owner,
            repo=ref.repo,
            repository_id=ref.repository_id,
            number=ref.number,
            target_branch=revision.target_branch,
            comparison=PinnedComparison(
                merge_base=revision.merge_base, head=revision.head
            ),
        )
        started_at = datetime.now(UTC)
        try:
            with open_gh_environment(
                self._environment,
                ref.host,
                prefix="loop-reviewer-discussion-gh-",
                private_capture_cwd=True,
            ) as (pinned, directory):
                return GhDiscussionReader(
                    GhApi(pinned, private_cwd=directory),
                    lambda api: GhPullRequestReader(pinned, api=api),
                    expected_account=selection.account,
                    monotonic=monotonic,
                    utc_now=lambda: started_at,
                ).read(target)
        except GitHubReadError:
            return unavailable_discussion(target, started_at, "read_error")

    def review(
        self,
        ref: PullRequestRef,
        *,
        retry: bool = False,
        expected_identity: ReviewIdentity | None = None,
        observe_selection: Callable[[ReviewSelection], None] | None = None,
        retry_target: RetryTarget | None = None,
    ) -> ReviewOutcome:
        environment = self._environment
        with open_gh_environment(
            environment, ref.host, prefix="loop-reviewer-read-gh-"
        ) as (pinned, _):

            def reviewer_factory(descriptor: int) -> Reviewer:
                comparison = GitComparison(pinned, inherited_fd=descriptor)
                source_environment = {"PATH": environment.get("PATH", os.defpath)}

                @contextmanager
                def open_source(
                    selection: ReviewSelection, pins: PinnedComparison
                ) -> Generator[PinnedSource]:
                    with (
                        comparison.open_verified_repository(selection) as repository,
                        open_local_comparison(
                            repository, pins, source_environment
                        ) as source,
                    ):
                        yield source

                return HostedReportReviewer(
                    open_source,
                    self.capture_discussion,
                    lambda: _hosted_model(self._model_environment),
                    monotonic,
                )

            workflow = ReviewPullRequest(
                GhPullRequestReader(pinned),
                SqliteReviewRunStore(self._database),
                LockedReviewExecution(self._state / "locks", reviewer_factory),
                select_current=self.select,
            )
            return workflow.run(
                ref,
                retry=retry,
                expected_identity=expected_identity,
                observe_selection=observe_selection,
                retry_target=retry_target,
            )

    def edit_review_draft(self) -> EditReviewDraft:
        return EditReviewDraft(SqliteReviewRunStore(self._database, now=_utc_now))

    def review_history(self) -> ReviewHistory:
        store = SqliteReviewRunStore(self._database)
        return ReviewHistory(store, store)

    def review_stats(self) -> ReviewStats:
        return ReviewStats(SqliteReviewRunStore(self._database), _utc_now)

    def active_reviews(self) -> ActiveReviews:
        return ActiveReviews(
            SqliteReviewRunStore(self._database),
            lambda key: review_lock_held(self._state / "locks", key),
        )

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
        return PublishReview(SqliteReviewRunStore(self._database, now=_utc_now), guard)

    def configure_inbox(self) -> TomlSettings:
        return self._config

    def scan_inbox(self) -> InboxReport:
        self._config.sync_inbox()
        return ScanInbox(
            GhRepositoryDiscovery(self._environment),
            SqliteInboxStore(self._database),
            SqliteReviewRunStore(self._database),
            now=lambda: datetime.now(UTC),
        ).run()

    def inbox_watch(self, *, monotonic: Callable[[], float]) -> InboxWatch:
        return InboxWatch(
            self._state / "inbox-watch", self.scan_inbox, monotonic=monotonic
        )

    def read_inbox(self) -> InboxReport:
        self._config.sync_inbox()
        return self.read_cached_inbox()

    def read_cached_inbox(self) -> InboxReport:
        return ReadInbox(
            SqliteInboxStore(self._database), SqliteReviewRunStore(self._database)
        ).run()

    def inbox_actions(self) -> InboxActions[ReviewOutcome]:
        return InboxActions(
            SqliteInboxStore(self._database),
            lambda ref, identity: self.review(ref, expected_identity=identity),
            refresh_settings=self._config.sync_inbox,
        )
