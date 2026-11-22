"""Decode bounded GitHub GET responses without exposing raw command output."""

import json
from typing import Literal

from loop_reviewer.integrations.gh_api import GhApi, GhInvocationError, GhResponse
from loop_reviewer.review.selection import GitHubReadError

INVALID_RESPONSE = "GitHub returned an invalid or unexpected response."
type ReadFailureKind = Literal[
    "access", "rate_limit", "transport", "invalid_response", "missing_cli"
]


class GhReadFailure(GitHubReadError):
    def __init__(self, kind: ReadFailureKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def read_json(
    api: GhApi, host: str, endpoint: str, *, unavailable: str | None = None
) -> tuple[GhResponse, object]:
    try:
        result = api.get(host, endpoint)
    except GhInvocationError as error:
        failures: dict[str, tuple[ReadFailureKind, str]] = {
            "missing": (
                "missing_cli",
                "Install the GitHub CLI (gh) and make sure it is on PATH.",
            ),
            "timeout": ("transport", "The GitHub request timed out; try again."),
            "execution": (
                "transport",
                "Could not run the GitHub CLI (gh). Check your installation.",
            ),
            "encoding": ("invalid_response", INVALID_RESPONSE),
            "output": ("invalid_response", INVALID_RESPONSE),
        }
        raise GhReadFailure(*failures[error.reason]) from None

    status = result.status
    if result.returncode == 4 or status == 401:
        raise GhReadFailure("access", f"Sign in with gh auth login --hostname {host}.")
    if is_rate_limited(result):
        raise GhReadFailure(
            "rate_limit", "GitHub's rate limit was reached; try again later."
        )
    generic = "The GitHub request failed; check your connection or try again later."
    if status in {403, 404, 409, 422}:
        raise GhReadFailure("access", unavailable or generic)
    if status == 408:
        raise GhReadFailure("transport", generic)
    if status is not None and 300 <= status < 500:
        # Unresolved redirects and other client errors cannot confirm access.
        raise GhReadFailure("access", generic)
    if result.returncode != 0 or (status is not None and not 200 <= status < 300):
        kind = "transport" if status is None or status >= 500 else "invalid_response"
        raise GhReadFailure(kind, generic)
    if status is None:
        raise GhReadFailure("invalid_response", INVALID_RESPONSE)
    try:
        data: object = json.loads(result.body)
    except ValueError, RecursionError:
        raise GhReadFailure("invalid_response", INVALID_RESPONSE) from None
    return result, data


def is_rate_limited(response: GhResponse) -> bool:
    return response.status == 429 or (
        response.status == 403
        and (
            response.headers.get("x-ratelimit-remaining") == "0"
            or "retry-after" in response.headers
            or _reports_secondary_rate_limit(response.body)
        )
    )


def _reports_secondary_rate_limit(body: str) -> bool:
    try:
        data: object = json.loads(body)
    except ValueError, RecursionError:
        return False
    if not isinstance(data, dict):
        return False
    message: object = data.get("message")
    return isinstance(message, str) and "secondary rate limit" in message.lower()
