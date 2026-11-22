"""Register the local HTTP contract with injected workflows."""

from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractContextManager, asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, JSONResponse
from starlette.staticfiles import StaticFiles

from loop_reviewer.inbox.actions import InboxActions, InboxRowChanged, InboxRowNotFound
from loop_reviewer.inbox.configuration import ConfigureLocal, LocalConfigurationError
from loop_reviewer.inbox.discovery import (
    RepositoryDiscoveryError,
    ScanSuperseded,
)
from loop_reviewer.inbox.model import InboxReport
from loop_reviewer.inbox.polling import ScanCycle
from loop_reviewer.inbox.store import InboxSettingsChanged, InboxStoreError
from loop_reviewer.local_http import history, inbox, publication, review
from loop_reviewer.local_http.models import ApiError, ErrorResponse
from loop_reviewer.local_http.session import LocalSession, error_response
from loop_reviewer.local_http.watch import BrowserWatch
from loop_reviewer.review.drafts import DraftNotFound, EditReviewDraft
from loop_reviewer.review.generate import (
    ReviewerError,
    ReviewIdentityMismatch,
    ReviewOutcome,
)
from loop_reviewer.review.history import ReviewHistory
from loop_reviewer.review.model import DraftValidationError, ReviewAlreadyCompleted
from loop_reviewer.review.publication import (
    PreparePublication,
    PublicationBlocked,
    PublicationNotFound,
    PublishReview,
)
from loop_reviewer.review.publication_model import PublicationError
from loop_reviewer.review.pull_request import PullRequestNotReviewable, PullRequestRef
from loop_reviewer.review.selection import GitHubReadError
from loop_reviewer.review.store import (
    DraftConflict,
    ReviewStoreError,
    UnconfirmedPublication,
)


def create_app(
    *,
    origin: str,
    capability: str,
    read_inbox: Callable[[], InboxReport],
    scan_inbox: Callable[[], InboxReport],
    configure_inbox: ConfigureLocal,
    inbox_actions: InboxActions[ReviewOutcome],
    review_pull_request: Callable[[PullRequestRef, bool], ReviewOutcome],
    drafts: EditReviewDraft,
    review_history: ReviewHistory,
    prepare: PreparePublication,
    publish: PublishReview,
    join_watch: Callable[[], AbstractContextManager[None]],
    watch_tick: Callable[[], None],
    watch_cycle: Callable[[], ScanCycle | None],
    assets: Path | None = None,
) -> FastAPI:
    watch = BrowserWatch(join_watch, watch_tick, watch_cycle)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        yield
        await watch.drain()

    app = FastAPI(
        title="Loop Reviewer Local API",
        version="1",
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
        redirect_slashes=False,
    )
    app.state.stop_watch = watch.stop
    app.add_middleware(LocalSession, origin=origin, capability=capability)
    api = APIRouter(
        prefix="/api/v1",
        responses={
            status: {"model": ErrorResponse}
            for status in (401, 403, 404, 409, 413, 415, 422, 500, 502)
        },
    )
    api.include_router(
        inbox.routes(
            read_inbox,
            scan_inbox,
            configure_inbox,
            inbox_actions,
            review_history,
            watch,
        )
    )
    api.include_router(review.routes(review_pull_request, drafts, review_history))
    api.include_router(publication.routes(review_history, prepare, publish))
    api.include_router(history.routes(review_history))
    app.include_router(api)
    _register_errors(app)

    if assets is not None:
        app.mount("/assets", StaticFiles(directory=assets / "assets"), name="assets")

        def index() -> FileResponse:
            return FileResponse(assets / "index.html")

        for path in (
            "/",
            "/inbox",
            "/setup",
            "/history",
            "/history/{runId}",
            "/drafts/{runId}",
            "/publications/{id}",
        ):
            app.add_api_route(
                path, index, include_in_schema=False, methods=["GET", "HEAD"]
            )

    return app


def _register_errors(app: FastAPI) -> None:
    async def handle_error(_request: Request, error: Exception) -> JSONResponse:
        return error_response(_api_error(error))

    for error_type in (
        ApiError,
        RequestValidationError,
        HTTPException,
        DraftNotFound,
        DraftValidationError,
        PullRequestNotReviewable,
        ReviewAlreadyCompleted,
        DraftConflict,
        InboxRowChanged,
        InboxRowNotFound,
        InboxSettingsChanged,
        ScanSuperseded,
        ReviewIdentityMismatch,
        UnconfirmedPublication,
        PublicationBlocked,
        PublicationNotFound,
        PublicationError,
        GitHubReadError,
        RepositoryDiscoveryError,
        ReviewerError,
        LocalConfigurationError,
        InboxStoreError,
        ReviewStoreError,
    ):
        app.add_exception_handler(error_type, handle_error)


def _api_error(error: Exception) -> ApiError:
    if isinstance(error, ApiError):
        return error
    if isinstance(error, RequestValidationError):
        return ApiError(
            422, "invalid_input", "Check the request fields and feedback size."
        )
    if isinstance(error, HTTPException):
        return ApiError(
            error.status_code,
            "not_found" if error.status_code == 404 else "invalid_input",
            "This route or method is unavailable.",
        )
    if isinstance(error, DraftNotFound | InboxRowNotFound):
        return ApiError(404, "not_found", str(error))
    if isinstance(error, LocalConfigurationError):
        return ApiError(422, "invalid_input", str(error))
    if isinstance(error, DraftValidationError):
        return ApiError(422, "invalid_input", str(error))
    if isinstance(error, ReviewAlreadyCompleted):
        return ApiError(
            409,
            "review_completed",
            str(error),
            completed_run_id=str(error.completion.source.run_id),
        )
    if isinstance(error, DraftConflict):
        return ApiError(409, "draft_changed", str(error))
    if isinstance(error, InboxRowChanged | InboxSettingsChanged | ScanSuperseded):
        return ApiError(409, "inbox_changed", str(error))
    if isinstance(error, ReviewIdentityMismatch):
        return ApiError(409, "identity_changed", str(error))
    if isinstance(error, UnconfirmedPublication):
        return ApiError(
            409,
            "recovery_required",
            str(error),
            publication_id=error.publication_id.value,
        )
    if isinstance(error, PublicationBlocked):
        return ApiError(
            409, error.reason, str(error), publication_id=error.publication_id.value
        )
    if isinstance(error, PublicationNotFound):
        return ApiError(404, "not_found", "No publication for this preview ID.")
    if isinstance(error, PublicationError):
        return ApiError(409, "publication_preflight", str(error))
    if isinstance(error, GitHubReadError | RepositoryDiscoveryError):
        return ApiError(502, "github_unavailable", str(error))
    if isinstance(error, ReviewerError | PullRequestNotReviewable):
        return ApiError(409, "review_unavailable", str(error))
    return ApiError(500, "local_state", "Could not read or update local review state.")
