"""Render the result of a review started from a URL or an inbox row."""

import sys
from argparse import ArgumentParser
from collections.abc import Callable

from loop_reviewer.cli.output import render_review_outcome
from loop_reviewer.review.generate import (
    ReviewerError,
    ReviewIdentityMismatch,
    ReviewOutcome,
    SavedDraftHistoryUnavailable,
    SavedDraftInterrupted,
)
from loop_reviewer.review.pull_request import PullRequestNotReviewable, PullRequestRef
from loop_reviewer.review.selection import GitHubReadError
from loop_reviewer.review.store import ReviewStoreError


def run_review_command(
    command: ArgumentParser, run: Callable[[], tuple[PullRequestRef, ReviewOutcome]]
) -> int:
    try:
        ref, outcome = run()
    except SavedDraftInterrupted as error:
        print(
            f"{command.prog}: review interrupted after saving draft {error.run_id}; "
            f"freshness was not confirmed. Run loop-reviewer draft show {error.run_id}.",
            file=sys.stderr,
        )
        return 130
    except SavedDraftHistoryUnavailable as error:
        print(
            f"{command.prog}: draft {error.run_id} saved, but history could not be "
            f"checked: {error} Run loop-reviewer draft show {error.run_id} to check its status.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print(
            f"{command.prog}: review interrupted. "
            "Run loop-reviewer review PR_URL to check the status before retrying.",
            file=sys.stderr,
        )
        return 130
    except (
        GitHubReadError,
        PullRequestNotReviewable,
        ReviewerError,
        ReviewIdentityMismatch,
        ReviewStoreError,
    ) as error:
        print(f"{command.prog}: error: {error}", file=sys.stderr)
        return 1
    return render_review_outcome(outcome, ref)
