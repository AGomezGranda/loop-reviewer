"""Open, read, edit, copy and finish private reviews."""

from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, Path
from pydantic import Field

from loop_reviewer.local_http.history import CompletionDetail
from loop_reviewer.local_http.launches import (
    CompletionResult,
    DraftResult,
    LaunchReceipt,
    LaunchSource,
    ReviewLaunches,
    ReviewRequest,
    RevisionChanges,
)
from loop_reviewer.local_http.models import (
    ApiError,
    ErrorResponse,
    Feedback,
    FeedbackResponse,
    Position,
    PullRequestURL,
    RequestModel,
    ResponseModel,
    SelectionResponse,
    SQLiteId,
)
from loop_reviewer.local_http.session import JsonBodyRoute, require_empty_body
from loop_reviewer.review.active import ActiveReview, ActiveReviews, ActiveStatus
from loop_reviewer.review.drafts import EditReviewDraft
from loop_reviewer.review.feedback import feedback_markdown
from loop_reviewer.review.generate import (
    DraftReview,
    FailedReview,
    FinishedReview,
    RetryTarget,
    ReviewOutcome,
)
from loop_reviewer.review.history import ReviewHistory
from loop_reviewer.review.model import (
    DraftTarget,
    ReviewerFailureKind,
    ReviewIdentity,
    SavedDraft,
    SelectionChange,
)
from loop_reviewer.review.pull_request import PullRequestRef
from loop_reviewer.reviewer.full_review_contract import FullReviewResult


class LaunchReview(RequestModel):
    url: PullRequestURL


class OpenReview(LaunchReview):
    retry: bool = False


class SavedReviewRow(ResponseModel):
    kind: Literal["saved"] = "saved"
    run_id: SQLiteId
    selection: SelectionResponse
    status: ActiveStatus | Literal["queued"]
    failure: str | None
    failure_kind: ReviewerFailureKind | None
    publication_id: str | None
    changes: RevisionChanges = ()

    @classmethod
    def from_active(cls, active: ActiveReview) -> SavedReviewRow:
        run = active.run
        return cls(
            run_id=str(run.id),
            selection=SelectionResponse.from_selection(run.selection),
            status=active.status,
            failure=run.failure,
            failure_kind=run.failure_kind,
            publication_id=run.publication_id.value if run.publication_id else None,
        )


class LaunchReviewRow(ResponseModel):
    kind: Literal["launch"] = "launch"
    launch_id: str
    url: str
    source: LaunchSource
    selection: SelectionResponse | None
    status: Literal["queued", "running", "failed", "completed"]
    error: ErrorResponse | None = None
    completed_run_id: SQLiteId | None = None


ReviewRow = Annotated[SavedReviewRow | LaunchReviewRow, Field(discriminator="kind")]


def review_rows(active: ActiveReviews, launches: ReviewLaunches) -> list[ReviewRow]:
    snapshot = launches.snapshot()
    saved = {
        item.run.selection.key: SavedReviewRow.from_active(item)
        for item in active.list()
    }
    transient: list[LaunchReviewRow] = []
    completed: dict[ReviewIdentity, bool] = {}
    for launch in snapshot:
        selection = launch.selection
        key = selection.key if selection is not None else None
        row = saved.get(key) if key is not None else None
        result = launch.result
        if isinstance(result, DraftResult) and row is None:
            continue
        if selection is not None:
            identity = selection.identity
            if identity not in completed:
                completed[identity] = active.completed_run_id(identity) is not None
                if completed[identity]:
                    saved = {
                        key: item
                        for key, item in saved.items()
                        if key.identity != identity
                    }
            if completed[identity] and not isinstance(result, CompletionResult):
                continue
        if launch.status != "settled":
            if row is not None:
                if launch.retry_run_id == int(row.run_id) and row.status in (
                    "failed",
                    "interrupted",
                ):
                    row.status = launch.status
                continue
            status = launch.status
        elif isinstance(result, DraftResult):
            if row is not None and row.run_id == str(result.run_id) and not row.changes:
                row.changes = result.changes
            continue
        elif isinstance(result, CompletionResult):
            status = "completed"
        else:
            status = "failed"
        transient.append(
            LaunchReviewRow(
                launch_id=launch.receipt.launch_id,
                url=selection.ref.url if selection is not None else launch.url,
                source=launch.source,
                selection=SelectionResponse.from_selection(selection)
                if selection is not None
                else None,
                status=status,
                completed_run_id=str(result.run_id)
                if isinstance(result, CompletionResult)
                else None,
                error=result if isinstance(result, ErrorResponse) else None,
            )
        )
    return [*transient, *saved.values()]


class DraftResponse(ResponseModel):
    run_id: SQLiteId
    version: SQLiteId
    selection: SelectionResponse
    feedback: FeedbackResponse
    publication_id: str | None
    generation_model: str | None
    estimated_cost_usd: float | None
    report: FullReviewResult | None

    @classmethod
    def from_saved(
        cls, saved: SavedDraft, publication_id: str | None = None
    ) -> DraftResponse:
        return cls(
            run_id=str(saved.id),
            version=str(saved.draft_version),
            selection=SelectionResponse.from_selection(saved.selection),
            feedback=FeedbackResponse.from_draft(saved.draft),
            publication_id=publication_id,
            generation_model=saved.generation_model,
            estimated_cost_usd=saved.estimated_cost_usd,
            report=saved.report,
        )


class DraftOutcome(ResponseModel):
    kind: Literal["draft"] = "draft"
    status: Literal["generated", "resumed"]
    draft: DraftResponse
    changes: tuple[SelectionChange, ...] | Literal["unavailable"]


class CompletedOutcome(ResponseModel):
    kind: Literal["completion"] = "completion"
    completion: CompletionDetail


class FailedOutcome(ResponseModel):
    kind: Literal["failure"] = "failure"
    message: str
    reason: ReviewerFailureKind | None


class UnavailableOutcome(ResponseModel):
    kind: Literal["unavailable"] = "unavailable"
    reason: Literal["in_progress", "abandoned", "no_retryable_run"]


OpenOutcome = Annotated[
    DraftOutcome | CompletedOutcome | FailedOutcome | UnavailableOutcome,
    Field(discriminator="kind"),
]


def _draft_with_publication(saved: SavedDraft, history: ReviewHistory) -> DraftResponse:
    record = history.unconfirmed_for(saved.id)
    return DraftResponse.from_saved(saved, record.preview.id.value if record else None)


def open_outcome(result: ReviewOutcome, history: ReviewHistory) -> OpenOutcome:
    if isinstance(result, DraftReview):
        return DraftOutcome(
            status=result.status,
            draft=_draft_with_publication(result.run, history),
            changes=result.changes,
        )
    if isinstance(result, FinishedReview):
        return CompletedOutcome(
            completion=CompletionDetail.from_completion(result.completion)
        )
    if isinstance(result, FailedReview):
        return FailedOutcome(message=result.message, reason=result.kind)
    return UnavailableOutcome(reason=result.status)


class DraftVersion(RequestModel):
    version: SQLiteId


class EditSummary(DraftVersion):
    kind: Literal["edit_summary"]
    text: Feedback


class EditFinding(DraftVersion):
    kind: Literal["edit_finding"]
    number: Position
    condition: Feedback | None = None
    consequence: Feedback | None = None


class DropFinding(DraftVersion):
    kind: Literal["drop_finding"]
    number: Position


class CopyResponse(ResponseModel):
    markdown: str


def parse_pr(url: str) -> PullRequestRef:
    try:
        return PullRequestRef.from_url(url)
    except ValueError as error:
        raise ApiError(422, "invalid_input", str(error)) from None


def routes(
    review: ReviewRequest,
    drafts: EditReviewDraft,
    history: ReviewHistory,
    active: ActiveReviews,
    launches: ReviewLaunches,
) -> APIRouter:
    router = APIRouter(route_class=JsonBodyRoute)

    @router.post("/reviews/open")
    def open_review(body: OpenReview) -> OpenOutcome:
        return open_outcome(review(parse_pr(body.url), retry=body.retry), history)

    @router.get("/reviews")
    def list_reviews() -> list[ReviewRow]:
        return review_rows(active, launches)

    @router.post("/reviews/launches", status_code=202)
    def launch_review(body: LaunchReview) -> LaunchReceipt:
        ref = parse_pr(body.url)
        return launches.submit(
            ("url", ref),
            lambda observe: review(ref, observe_selection=observe),
            url=ref.url,
        )

    @router.post(
        "/reviews/{runId}/retry",
        status_code=202,
        dependencies=[Depends(require_empty_body)],
    )
    def retry_review(run_id: Annotated[SQLiteId, Path(alias="runId")]) -> LaunchReceipt:
        id = int(run_id)
        key: tuple[Literal["retry"], int] = ("retry", id)
        if (pending := launches.pending(key)) is not None:
            return pending
        item = active.by_id(id)
        if item is None or item.status not in ("failed", "interrupted"):
            if (pending := launches.pending(key)) is not None:
                return pending
            raise ApiError(
                409,
                "review_unavailable",
                "This attempt is no longer available to retry.",
            )
        selection = item.run.selection
        target = RetryTarget(id, selection.key)
        return launches.submit(
            key,
            lambda observe: review(
                selection.ref, observe_selection=observe, retry_target=target
            ),
            url=selection.ref.url,
            selection=selection,
            retry_run_id=id,
        )

    @router.get("/drafts/{runId}")
    def get_draft(run_id: Annotated[SQLiteId, Path(alias="runId")]) -> DraftResponse:
        return _draft_with_publication(drafts.show(int(run_id)).saved_draft(), history)

    @router.get("/drafts/{runId}/copy")
    def copy_draft(run_id: Annotated[SQLiteId, Path(alias="runId")]) -> CopyResponse:
        return CopyResponse(
            markdown=feedback_markdown(drafts.show(int(run_id)).saved_draft().draft)
        )

    @router.patch("/drafts/{runId}")
    def edit_draft(
        run_id: Annotated[SQLiteId, Path(alias="runId")],
        body: Annotated[
            EditSummary | EditFinding | DropFinding, Body(discriminator="kind")
        ],
    ) -> DraftResponse:
        target = DraftTarget(int(run_id), int(body.version))
        if isinstance(body, EditSummary):
            run = drafts.edit_summary(target, body.text)
        elif isinstance(body, EditFinding):
            run = drafts.edit_finding(
                target,
                body.number,
                condition=body.condition,
                consequence=body.consequence,
            )
        else:
            run = drafts.drop_finding(target, body.number)
        return DraftResponse.from_saved(run.saved_draft())

    @router.post("/drafts/{runId}/finish")
    def finish_draft(
        run_id: Annotated[SQLiteId, Path(alias="runId")], body: DraftVersion
    ) -> CompletionDetail:
        return CompletionDetail.from_completion(
            drafts.finish_locally(DraftTarget(int(run_id), int(body.version)))
        )

    return router
