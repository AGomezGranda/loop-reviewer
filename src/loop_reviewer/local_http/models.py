"""Shared JSON shapes and validation for the local API."""

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from loop_reviewer.review.model import (
    MAX_EDIT_BYTES,
    MAX_STORED_INTEGER,
    PublicationAction,
    PublicationReceipt,
    Recommendation,
    ReviewDraft,
    ReviewSelection,
    Severity,
)
from loop_reviewer.review.publication_model import PublicationError, PublicationId


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, alias_generator=to_camel)


class ResponseModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        validate_by_name=True,
        serialize_by_alias=True,
        json_schema_serialization_defaults_required=True,
    )


def _check_sqlite_id(value: str) -> str:
    if int(value) > MAX_STORED_INTEGER:
        raise ValueError("Value is outside the supported integer range.")
    return value


def _check_feedback(value: str) -> str:
    if len(value.encode("utf-8")) > MAX_EDIT_BYTES:
        raise ValueError("Feedback must be at most 64 KiB per field.")
    return value.replace("\r\n", "\n")


def _check_publication_id(value: str) -> str:
    try:
        return PublicationId(value).value
    except PublicationError as error:
        raise ValueError(str(error)) from None


SQLiteId = Annotated[
    str, Field(pattern=r"^[1-9][0-9]{0,18}$"), AfterValidator(_check_sqlite_id)
]
DecimalId = Annotated[str, Field(pattern=r"^[1-9][0-9]*$")]
PublicationKey = Annotated[
    str,
    Field(json_schema_extra={"format": "uuid"}),
    AfterValidator(_check_publication_id),
]
Feedback = Annotated[
    str, Field(max_length=MAX_EDIT_BYTES), AfterValidator(_check_feedback)
]
Position = Annotated[int, Field(gt=0)]
PullRequestURL = Annotated[str, Field(min_length=1, max_length=2048)]


class SelectionResponse(ResponseModel):
    url: str
    account: str
    author: str
    target_branch: str
    base_tip: str
    merge_base: str
    head: str

    @classmethod
    def from_selection(cls, selection: ReviewSelection) -> SelectionResponse:
        revision = selection.revision
        return cls(
            url=selection.ref.url,
            account=selection.account,
            author=selection.author,
            target_branch=revision.target_branch,
            base_tip=revision.base_tip,
            merge_base=revision.merge_base,
            head=revision.head,
        )


class FindingResponse(ResponseModel):
    number: int
    severity: Severity
    condition: str
    consequence: str
    path: str | None
    line: str | None
    report_id: str | None


class FeedbackResponse(ResponseModel):
    verdict: Recommendation
    summary: str
    findings: list[FindingResponse]

    @classmethod
    def from_draft(cls, draft: ReviewDraft) -> FeedbackResponse:
        return cls(
            verdict=draft.verdict,
            summary=draft.summary,
            findings=[
                FindingResponse(
                    number=number,
                    severity=finding.severity,
                    condition=finding.condition,
                    consequence=finding.consequence,
                    path=finding.location.path if finding.location else None,
                    line=str(finding.location.right_line) if finding.location else None,
                    report_id=finding.report_id,
                )
                for number, finding in enumerate(draft.ordered_findings(), start=1)
            ],
        )


class ReceiptResponse(ResponseModel):
    review_id: DecimalId
    action: PublicationAction

    @classmethod
    def from_receipt(cls, receipt: PublicationReceipt | None) -> ReceiptResponse | None:
        return (
            cls(review_id=str(receipt.review_id), action=receipt.action)
            if receipt
            else None
        )


type ErrorCode = Literal[
    "invalid_input",
    "invalid_host",
    "invalid_origin",
    "unauthorized",
    "not_found",
    "unsupported_media_type",
    "body_too_large",
    "draft_changed",
    "review_completed",
    "inbox_changed",
    "identity_changed",
    "recovery_required",
    "confirmation_required",
    "evidence_inconclusive",
    "duplicate_risk_consent_required",
    "publication_preflight",
    "github_unavailable",
    "review_unavailable",
    "local_state",
    "internal_error",
]


class ErrorResponse(ResponseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=False)

    code: ErrorCode
    message: str
    publication_id: str | None = None
    completed_run_id: str | None = None


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: ErrorCode,
        message: str,
        *,
        publication_id: str | None = None,
        completed_run_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = ErrorResponse(
            code=code,
            message=message,
            publication_id=publication_id,
            completed_run_id=completed_run_id,
        )
