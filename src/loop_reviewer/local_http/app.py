"""Register the local HTTP contract with injected workflows."""

from asyncio import to_thread
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractContextManager, asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, FastAPI, Request
from starlette.responses import FileResponse, JSONResponse
from starlette.staticfiles import StaticFiles

from loop_reviewer.inbox.actions import InboxActions
from loop_reviewer.inbox.configuration import ConfigureLocal
from loop_reviewer.inbox.model import InboxReport
from loop_reviewer.inbox.polling import ScanCycle
from loop_reviewer.local_http import history, inbox, publication, review, stats
from loop_reviewer.local_http.errors import ERROR_TYPES, api_error
from loop_reviewer.local_http.launches import ReviewLaunches, ReviewRequest
from loop_reviewer.local_http.models import ErrorResponse
from loop_reviewer.local_http.session import LocalSession, error_response
from loop_reviewer.local_http.watch import BrowserWatch
from loop_reviewer.review.active import ActiveReviews
from loop_reviewer.review.drafts import EditReviewDraft
from loop_reviewer.review.generate import (
    ReviewOutcome,
)
from loop_reviewer.review.history import ReviewHistory
from loop_reviewer.review.publication import (
    PreparePublication,
    PublishReview,
)
from loop_reviewer.review.stats import ReviewStats


def create_app(
    *,
    origin: str,
    capability: str,
    read_inbox: Callable[[], InboxReport],
    read_cached_inbox: Callable[[], InboxReport],
    scan_inbox: Callable[[], InboxReport],
    configure_inbox: ConfigureLocal,
    inbox_actions: InboxActions[ReviewOutcome],
    review_pull_request: ReviewRequest,
    drafts: EditReviewDraft,
    review_history: ReviewHistory,
    review_stats: ReviewStats,
    active_reviews: ActiveReviews,
    prepare: PreparePublication,
    publish: PublishReview,
    join_watch: Callable[[], AbstractContextManager[None]],
    watch_tick: Callable[[], None],
    watch_cycle: Callable[[], ScanCycle | None],
    assets: Path | None = None,
) -> FastAPI:
    watch = BrowserWatch(join_watch, watch_tick, watch_cycle)
    launches = ReviewLaunches(lambda: str(uuid4()))

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            launches.stop()
            await to_thread(launches.close)
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
    app.state.stop_launches = launches.stop
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
            review_pull_request,
            launches,
        )
    )
    api.include_router(
        review.routes(
            review_pull_request, drafts, review_history, active_reviews, launches
        )
    )
    api.include_router(publication.routes(review_history, prepare, publish))
    api.include_router(history.routes(review_history))
    api.include_router(stats.routes(review_stats))
    app.include_router(api)
    _register_errors(app)

    if assets is not None:
        app.mount("/assets", StaticFiles(directory=assets / "assets"), name="assets")

        def index() -> FileResponse:
            return FileResponse(assets / "index.html")

        for path in (
            "/",
            "/inbox",
            "/reviews",
            "/setup",
            "/history",
            "/stats",
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
