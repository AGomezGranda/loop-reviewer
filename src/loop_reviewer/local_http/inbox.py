"""Serve inbox reads, actions, scans and watch invalidation events."""

from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import APIRouter
from pydantic import Field

from loop_reviewer.inbox.actions import InboxActions
from loop_reviewer.inbox.discovery import ConfigureInbox, InboxNotConfigured
from loop_reviewer.inbox.model import (
    InboxReport,
    InboxRow,
    InboxSettings,
    JiraProjectKey,
    RepositoryAddress,
    RepositoryScanStatus,
    WatchedRepositoryKey,
)
from loop_reviewer.inbox.store import InboxRowTarget
from loop_reviewer.local_http.models import (
    ApiError,
    DecimalId,
    PullRequestURL,
    RequestModel,
    ResponseModel,
    SQLiteId,
)
from loop_reviewer.local_http.review import OpenOutcome, open_outcome, parse_pr
from loop_reviewer.local_http.watch import BrowserWatch, WatchEvent, WatchResponse
from loop_reviewer.review.generate import ReviewOutcome
from loop_reviewer.review.history import ReviewHistory
from loop_reviewer.review.model import RunStatus
from loop_reviewer.review.pull_request import PullRequestRef


class RowIdentity(RequestModel):
    host: str
    repository_id: SQLiteId
    number: DecimalId
    account: str


class ConfigureInboxRequest(RequestModel):
    settings_version: SQLiteId | None
    jira_project_keys: list[str]
    repository_urls: list[str]


class InboxConfiguration(ResponseModel):
    settings_version: SQLiteId | None
    jira_project_keys: list[str]
    repository_urls: list[str]

    @classmethod
    def from_settings(cls, settings: InboxSettings | None) -> InboxConfiguration:
        if settings is None:
            return cls(settings_version=None, jira_project_keys=[], repository_urls=[])
        return cls(
            settings_version=str(settings.version),
            jira_project_keys=[project.value for project in settings.projects],
            repository_urls=[repo.address.url for repo in settings.repositories],
        )


class RowIdentityResponse(ResponseModel):
    host: str
    repository_id: SQLiteId
    number: DecimalId
    account: str


class RowAction(RequestModel):
    url: PullRequestURL
    settings_version: SQLiteId
    identity: RowIdentity

    def target(self) -> tuple[PullRequestRef, InboxRowTarget]:
        ref = parse_pr(self.url)
        identity = self.identity
        if ref.host != identity.host or str(ref.number) != identity.number:
            raise ApiError(
                422, "invalid_input", "The inbox identity does not match the URL."
            )
        repository_id = int(identity.repository_id)
        saved_ref = PullRequestRef(
            ref.host, ref.owner, ref.repo, ref.number, repository_id
        )
        return ref, InboxRowTarget(
            int(self.settings_version),
            WatchedRepositoryKey(identity.host, repository_id, identity.account),
            saved_ref,
        )


class InboxRowResponse(ResponseModel):
    url: str
    identity: RowIdentityResponse
    title: str
    repository: str
    author: str
    reasons: tuple[str, ...]
    last_saved: RunStatus | None
    stale: bool

    @classmethod
    def from_row(cls, row: InboxRow) -> InboxRowResponse:
        ref = row.pull_request.ref
        assert ref.repository_id is not None
        return cls(
            url=ref.url,
            identity=RowIdentityResponse(
                host=row.identity.host,
                repository_id=str(ref.repository_id),
                number=str(ref.number),
                account=row.identity.account,
            ),
            title=row.pull_request.title,
            repository=f"{ref.owner}/{ref.repo}",
            author=row.pull_request.author,
            reasons=row.reasons,
            last_saved=row.last_saved,
            stale=row.stale,
        )


class RepositoryResponse(ResponseModel):
    repository_id: SQLiteId
    host: str
    repository: str
    account: str
    status: Literal["success", "failed", "pending"]
    last_success: str | None
    error: str | None

    @classmethod
    def from_status(cls, status: RepositoryScanStatus) -> RepositoryResponse:
        repo = status.repository
        return cls(
            repository_id=str(repo.repository_id),
            host=repo.address.host,
            repository=repo.address.slug,
            account=repo.account,
            status=status.outcome,
            last_success=status.last_success.isoformat()
            if status.last_success
            else None,
            error=status.error,
        )


class UnconfiguredInbox(ResponseModel):
    kind: Literal["unconfigured"] = "unconfigured"


class ConfiguredInbox(ResponseModel):
    kind: Literal["report"] = "report"
    settings_version: SQLiteId
    prefix: str
    repositories: list[RepositoryResponse]
    active: list[InboxRowResponse]
    later: list[InboxRowResponse]


InboxResponse = Annotated[
    UnconfiguredInbox | ConfiguredInbox, Field(discriminator="kind")
]


def _report(read: Callable[[], InboxReport]) -> InboxResponse:
    try:
        report = read()
    except InboxNotConfigured:
        return UnconfiguredInbox()
    return ConfiguredInbox(
        settings_version=str(report.settings.version),
        prefix=report.settings.prefix_label,
        repositories=[
            RepositoryResponse.from_status(status) for status in report.statuses
        ],
        active=[InboxRowResponse.from_row(row) for row in report.active],
        later=[InboxRowResponse.from_row(row) for row in report.deferred],
    )


def routes(
    read: Callable[[], InboxReport],
    scan: Callable[[], InboxReport],
    configure: ConfigureInbox,
    actions: InboxActions[ReviewOutcome],
    history: ReviewHistory,
    watch: BrowserWatch,
) -> APIRouter:
    router = APIRouter(prefix="/inbox")

    @router.get("")
    def get_inbox() -> InboxResponse:
        return _report(read)

    @router.get("/configuration")
    def get_configuration() -> InboxConfiguration:
        return InboxConfiguration.from_settings(configure.settings())

    @router.post("/configuration")
    def save_configuration(body: ConfigureInboxRequest) -> InboxConfiguration:
        try:
            projects = tuple(JiraProjectKey(key) for key in body.jira_project_keys)
            addresses = tuple(
                RepositoryAddress.from_url(url) for url in body.repository_urls
            )
            settings = configure.run(
                projects, addresses, expected_version=int(body.settings_version or 0)
            )
        except ValueError as error:
            raise ApiError(422, "invalid_input", str(error)) from None
        return InboxConfiguration.from_settings(settings)

    @router.post("/scan")
    def scan_inbox() -> InboxResponse:
        return _report(scan)

    @router.post("/open")
    def open_row(body: RowAction) -> OpenOutcome:
        ref, expected = body.target()
        _, result = actions.open(ref, expected_target=expected)
        return open_outcome(result, history)

    @router.post("/later")
    def defer_row(body: RowAction) -> InboxResponse:
        ref, expected = body.target()
        actions.defer(ref, expected_target=expected)
        return _report(read)

    @router.post("/restore")
    def restore_row(body: RowAction) -> InboxResponse:
        ref, expected = body.target()
        actions.restore(ref, expected_target=expected)
        return _report(read)

    @router.get(
        "/watch",
        response_class=WatchResponse,
        status_code=200,
        responses={
            200: {
                "content": {
                    "application/x-ndjson": {"schema": WatchEvent.model_json_schema()}
                }
            },
        },
    )
    async def watch_inbox() -> WatchResponse:
        return WatchResponse(watch.events())

    return router
