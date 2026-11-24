"""Read completed reviews from the local coordinator."""

from typing import Annotated, Literal, override

from fastapi import APIRouter, Path, Query

from loop_reviewer.local_http.models import (
    ApiError,
    FeedbackResponse,
    ReceiptResponse,
    ResponseModel,
    SelectionResponse,
    SQLiteId,
)
from loop_reviewer.review.history import ReviewHistory
from loop_reviewer.review.model import CompletedReview
from loop_reviewer.reviewer.full_review_contract import FullReviewResult


class CompletionResponse(ResponseModel):
    run_id: SQLiteId
    version: SQLiteId
    selection: SelectionResponse
    feedback: FeedbackResponse
    disposition: Literal["not_published", "published"]
    receipt: ReceiptResponse | None
    generation_model: str | None
    estimated_cost_usd: float | None

    @classmethod
    def from_completion(cls, completion: CompletedReview) -> CompletionResponse:
        return cls(
            run_id=str(completion.source.run_id),
            version=str(completion.source.version),
            selection=SelectionResponse.from_selection(completion.selection),
            feedback=FeedbackResponse.from_draft(completion.final_draft),
            disposition=completion.publication_status,
            receipt=ReceiptResponse.from_receipt(completion.receipt),
            generation_model=completion.generation_model,
            estimated_cost_usd=completion.estimated_cost_usd,
        )


class CompletionDetail(CompletionResponse):
    report: FullReviewResult | None

    @classmethod
    @override
    def from_completion(cls, completion: CompletedReview) -> CompletionDetail:
        row = CompletionResponse.from_completion(completion)
        return cls.model_validate({**row.model_dump(), "report": completion.report})


class HistoryPage(ResponseModel):
    completions: list[CompletionResponse]
    next_before: SQLiteId | None


def routes(history: ReviewHistory) -> APIRouter:
    router = APIRouter(prefix="/history")

    @router.get("")
    def list_history(before: Annotated[SQLiteId | None, Query()] = None) -> HistoryPage:
        # Look ahead one row while keeping each workflow read within its page cap.
        page = history.completions(int(before) if before is not None else None, 50)
        next_before = None
        if len(page) == 50 and history.completions(page[-1].source.run_id, 1):
            next_before = str(page[-1].source.run_id)
        return HistoryPage(
            completions=[CompletionResponse.from_completion(item) for item in page],
            next_before=next_before,
        )

    @router.get("/{runId}")
    def get_completion(
        run_id: Annotated[SQLiteId, Path(alias="runId")],
    ) -> CompletionDetail:
        item = history.completion(int(run_id))
        if item is None:
            raise ApiError(404, "not_found", "No completion for this review.")
        return CompletionDetail.from_completion(item)

    return router
