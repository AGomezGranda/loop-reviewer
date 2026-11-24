"""Read and act on persisted publication snapshots by ID."""

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Path

from loop_reviewer.local_http.history import CompletionResponse
from loop_reviewer.local_http.models import (
    ApiError,
    PublicationKey,
    ReceiptResponse,
    RequestModel,
    ResponseModel,
    SelectionResponse,
    SQLiteId,
)
from loop_reviewer.local_http.review import DraftVersion
from loop_reviewer.local_http.session import JsonBodyRoute, require_empty_body
from loop_reviewer.review.history import ReviewHistory
from loop_reviewer.review.model import CompletedReview, DraftTarget, PublicationAction
from loop_reviewer.review.publication import (
    PreparePublication,
    PublicationOutcome,
    PublishReview,
    RejectedPublication,
    RejectionReason,
)
from loop_reviewer.review.publication_model import (
    PublicationId,
    PublicationPreview,
    PublicationRecord,
    PublicationStatus,
)
from loop_reviewer.review.pull_request import PullRequestNotReviewable
from loop_reviewer.review.selection import GitHubReadError


class PreviewOptions(DraftVersion):
    action: PublicationAction
    summary_only: bool = False


class InlineResponse(ResponseModel):
    path: str
    line: str
    body: str


class PreviewResponse(ResponseModel):
    id: PublicationKey
    run_id: SQLiteId
    version: SQLiteId
    selection: SelectionResponse
    action: PublicationAction
    body: str
    comments: list[InlineResponse]

    @classmethod
    def from_preview(cls, preview: PublicationPreview) -> PreviewResponse:
        return cls(
            id=preview.id.value,
            run_id=str(preview.source.run_id),
            version=str(preview.source.version),
            selection=SelectionResponse.from_selection(preview.selection),
            action=preview.action,
            body=preview.body,
            comments=[
                InlineResponse(
                    path=comment.path, line=str(comment.line), body=comment.body
                )
                for comment in preview.comments
            ],
        )


class PublicationResponse(ResponseModel):
    preview: PreviewResponse
    status: PublicationStatus
    receipt: ReceiptResponse | None

    @classmethod
    def from_record(cls, record: PublicationRecord) -> PublicationResponse:
        return cls(
            preview=PreviewResponse.from_preview(record.preview),
            status=record.status,
            receipt=ReceiptResponse.from_receipt(record.receipt),
        )


class PublicationResult(ResponseModel):
    status: PublicationStatus
    completion: CompletionResponse | None = None
    rejection_reason: RejectionReason | None = None


class RetryOptions(RequestModel):
    accept_duplicate_risk: bool


def _operate(
    id: PublicationId,
    operation: Callable[[PublicationId], PublicationOutcome],
) -> PublicationResult:
    try:
        result = operation(id)
    except (GitHubReadError, PullRequestNotReviewable) as error:
        raise ApiError(
            409, "publication_preflight", str(error), publication_id=id.value
        ) from None
    if isinstance(result, CompletedReview):
        return PublicationResult(
            status="published", completion=CompletionResponse.from_completion(result)
        )
    if isinstance(result, RejectedPublication):
        return PublicationResult(
            status=result.record.status, rejection_reason=result.reason
        )
    return PublicationResult(status=result.status)


def routes(
    history: ReviewHistory,
    prepare: PreparePublication,
    publish: PublishReview,
) -> APIRouter:
    router = APIRouter(route_class=JsonBodyRoute)

    @router.post("/drafts/{runId}/publication-preview")
    def prepare_preview(
        run_id: Annotated[SQLiteId, Path(alias="runId")],
        body: PreviewOptions,
    ) -> PreviewResponse:
        try:
            preview = prepare.preview(
                DraftTarget(int(run_id), int(body.version)),
                body.action,
                summary_only=body.summary_only,
            )
        except (GitHubReadError, PullRequestNotReviewable) as error:
            raise ApiError(409, "publication_preflight", str(error)) from None
        return PreviewResponse.from_preview(preview)

    @router.get("/publications/unconfirmed")
    def list_unconfirmed() -> list[PublicationResponse]:
        return [
            PublicationResponse.from_record(record)
            for record in history.unconfirmed_publications()
        ]

    @router.get("/publications/{id}")
    def get_publication(id: PublicationKey) -> PublicationResponse:
        record = history.publication(PublicationId(id))
        if record is None:
            raise ApiError(404, "not_found", "No publication for this preview ID.")
        return PublicationResponse.from_record(record)

    @router.post(
        "/publications/{id}/confirm", dependencies=[Depends(require_empty_body)]
    )
    def confirm(id: PublicationKey) -> PublicationResult:
        return _operate(PublicationId(id), publish.publish)

    @router.post(
        "/publications/{id}/recover", dependencies=[Depends(require_empty_body)]
    )
    def recover(id: PublicationKey) -> PublicationResult:
        return _operate(PublicationId(id), publish.recover)

    @router.post("/publications/{id}/retry")
    def retry(id: PublicationKey, body: RetryOptions) -> PublicationResult:
        return _operate(
            PublicationId(id),
            lambda key: publish.retry(
                key, accept_duplicate_risk=body.accept_duplicate_risk
            ),
        )

    return router
