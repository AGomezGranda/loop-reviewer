"""Register the local HTTP contract with injected workflows."""

from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractContextManager, asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from starlette.responses import FileResponse, JSONResponse
from starlette.staticfiles import StaticFiles

from loop_reviewer.inbox.actions import InboxActions
from loop_reviewer.inbox.configuration import ConfigureLocal
from loop_reviewer.inbox.model import InboxReport
from loop_reviewer.inbox.polling import ScanCycle
from loop_reviewer.local_http import history, inbox, publication, review
from loop_reviewer.local_http.errors import ERROR_TYPES, api_error
from loop_reviewer.local_http.models import ErrorResponse
from loop_reviewer.local_http.session import LocalSession, error_response
from loop_reviewer.local_http.watch import BrowserWatch
from loop_reviewer.review.drafts import EditReviewDraft
from loop_reviewer.review.generate import (
    ReviewOutcome,
)
from loop_reviewer.review.history import ReviewHistory
from loop_reviewer.review.publication import (
    PreparePublication,
    PublishReview,
)
from loop_reviewer.review.pull_request import PullRequestRef


def create_app(
    *,
    origin: str,
    capability: str,
    read_inbox: Callable[[], InboxReport],
    read_cached_inbox: Callable[[], InboxReport],
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
            read_cached_inbox,
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
        return error_response(api_error(error))

    for error_type in ERROR_TYPES:
        app.add_exception_handler(error_type, handle_error)
