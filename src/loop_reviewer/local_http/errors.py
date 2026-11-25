"""Translate workflow exceptions into safe local API errors."""

from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException

from loop_reviewer.inbox.actions import InboxRowChanged, InboxRowNotFound
from loop_reviewer.inbox.configuration import LocalConfigurationError
from loop_reviewer.inbox.discovery import (
    RepositoryDiscoveryError,
    ScanSuperseded,
)
from loop_reviewer.inbox.store import InboxSettingsChanged, InboxStoreError
from loop_reviewer.local_http.models import ApiError
from loop_reviewer.review.drafts import DraftNotFound
from loop_reviewer.review.generate import (
    ReviewerError,
    ReviewIdentityMismatch,
)
from loop_reviewer.review.model import DraftValidationError, ReviewAlreadyCompleted
from loop_reviewer.review.publication import (
    PublicationBlocked,
    PublicationNotFound,
)
from loop_reviewer.review.publication_model import PublicationError
from loop_reviewer.review.pull_request import PullRequestNotReviewable
from loop_reviewer.review.selection import GitHubReadError
from loop_reviewer.review.store import (
    DraftConflict,
    ReviewStoreError,
    UnconfirmedPublication,
)

ERROR_TYPES = (
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
)


def api_error(error: Exception) -> ApiError:
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
