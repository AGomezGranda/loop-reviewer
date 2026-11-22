"""Open, read, edit, copy and finish private reviews."""

from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Path
from pydantic import Field

from loop_reviewer.local_http.history import CompletionResponse
from loop_reviewer.local_http.models import (
    ApiError,
    Feedback,
    FeedbackResponse,
    Position,
    PullRequestURL,
    RequestModel,
    ResponseModel,
    SelectionResponse,
    SQLiteId,
)
from loop_reviewer.review.drafts import EditReviewDraft
from loop_reviewer.review.feedback import feedback_markdown
from loop_reviewer.review.generate import (
    DraftReview,
    FailedReview,
    FinishedReview,
    ReviewOutcome,
)
from loop_reviewer.review.history import ReviewHistory
from loop_reviewer.review.model import (
    DraftTarget,
    ReviewerFailureKind,
    SavedDraft,
    SelectionChange,
)
from loop_reviewer.review.pull_request import PullRequestRef


class OpenReview(RequestModel):
    url: PullRequestURL
    retry: bool = False


class DraftResponse(ResponseModel):
    run_id: SQLiteId
    version: SQLiteId
    selection: SelectionResponse
    feedback: FeedbackResponse
    publication_id: str | None

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
        )


class DraftOutcome(ResponseModel):
    kind: Literal["draft"] = "draft"
    status: Literal["generated", "resumed"]
    draft: DraftResponse
    changes: tuple[SelectionChange, ...] | Literal["unavailable"]


class CompletedOutcome(ResponseModel):
    kind: Literal["completion"] = "completion"
    completion: CompletionResponse


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
            completion=CompletionResponse.from_completion(result.completion)
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
    review: Callable[[PullRequestRef, bool], ReviewOutcome],
    drafts: EditReviewDraft,
    history: ReviewHistory,
) -> APIRouter:
    router = APIRouter()

    @router.post("/reviews/open")
    def open_review(body: OpenReview) -> OpenOutcome:
        return open_outcome(review(parse_pr(body.url), body.retry), history)

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
    ) -> CompletionResponse:
        return CompletionResponse.from_completion(
            drafts.finish_locally(DraftTarget(int(run_id), int(body.version)))
        )

    return router
