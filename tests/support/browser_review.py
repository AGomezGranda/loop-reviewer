"""Run the installed CLI entry point with deterministic external collaborators."""

import os
from contextlib import nullcontext
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Literal
from unittest.mock import patch

from loop_reviewer import cli, composition
from loop_reviewer.local_http.serve import serve
from loop_reviewer.review.model import (
    Finding,
    FindingLocation,
    GeneratedReview,
    PublicationReceipt,
    ReviewDraft,
    ReviewSelection,
)
from loop_reviewer.review.publication_model import PublicationPreview
from loop_reviewer.review.pull_request import (
    PullRequest,
    PullRequestRef,
    ReviewedRevision,
)

_REVISION = ReviewedRevision("main", "a" * 40, "b" * 40, "c" * 40)


def _pull_request(ref: PullRequestRef) -> PullRequest:
    return PullRequest(
        replace(ref, repository_id=42),
        "teammate",
        "open",
        False,
        "main",
        _REVISION.head,
    )


def _count(event: str) -> None:
    with (Path(os.environ["XDG_STATE_HOME"]) / "events").open("a") as stream:
        stream.write(event + "\n")


class FakeReader:
    def __init__(self, _environment: dict[str, str]) -> None:
        pass

    def read_pull_request(self, ref: PullRequestRef) -> tuple[str, PullRequest]:
        return "reviewer", _pull_request(ref)

    def read_revision(self, _pull_request: PullRequest) -> ReviewedRevision:
        return _REVISION


class FakeReviewer:
    def __init__(self, *_args: object, **_options: object) -> None:
        pass

    def review(self, _selection: ReviewSelection) -> GeneratedReview:
        _count("review")
        draft = ReviewDraft(
            "comment",
            "Original CLI feedback",
            (
                Finding(
                    "high",
                    "The request can fail.",
                    "The user sees an error.",
                    FindingLocation("example.py", 8),
                ),
            ),
        )
        return GeneratedReview(draft, "provider/model", 0.0234)


class FakePublication:
    login = "reviewer"
    actor_id = 7

    def read_pull_request(self, ref: PullRequestRef) -> PullRequest:
        return _pull_request(ref)

    def read_revision(self, _pull_request: PullRequest) -> ReviewedRevision:
        return _REVISION

    def usable_anchors(
        self, _ref: PullRequestRef, requested: frozenset[FindingLocation]
    ) -> frozenset[FindingLocation]:
        return requested

    def submit(
        self, _preview: PublicationPreview
    ) -> PublicationReceipt | Literal["unknown"]:
        _count("submit")
        raise AssertionError("The browser handoff smoke never sends a review.")


def fake_gh_environment(
    environment: dict[str, str], _host: str, *, prefix: str
) -> nullcontext[tuple[dict[str, str], str]]:
    return nullcontext((environment, "fixture"))


def fake_publication(
    _env: dict[str, str], _host: str, *, inherited_fd: int | None = None
) -> nullcontext[FakePublication]:
    return nullcontext(FakePublication())


if __name__ == "__main__":
    with (
        patch.object(composition, "open_gh_environment", fake_gh_environment),
        patch.object(composition, "GhPullRequestReader", FakeReader),
        patch.object(composition, "PiReviewer", FakeReviewer),
        patch.object(composition, "open_gh_publication", fake_publication),
        patch.object(cli, "serve", partial(serve, open_browser=lambda _url: False)),
    ):
        raise SystemExit(cli.main())
