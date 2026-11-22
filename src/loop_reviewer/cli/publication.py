"""Parse publication commands and report exact previews and recoverable outcomes."""

import sys
from argparse import ArgumentParser, ArgumentTypeError, Namespace
from collections.abc import Callable

from loop_reviewer.cli.output import render_completed_review, render_publication_preview
from loop_reviewer.review.drafts import DraftNotFound
from loop_reviewer.review.model import (
    CompletedReview,
    DraftTarget,
    DraftValidationError,
    InvalidRunId,
    ReviewAlreadyCompleted,
)
from loop_reviewer.review.publication import (
    PreparePublication,
    PublicationBlocked,
    PublicationInterrupted,
    PublicationOutcome,
    PublishReview,
    RejectedPublication,
    RejectionReason,
)
from loop_reviewer.review.publication_model import PublicationError, PublicationId
from loop_reviewer.review.pull_request import PullRequestNotReviewable
from loop_reviewer.review.selection import GitHubReadError
from loop_reviewer.review.store import (
    DraftConflict,
    ReviewStoreError,
    UnconfirmedPublication,
)


def add_publication_commands(
    parser: ArgumentParser, stored_integer: Callable[[str], int]
) -> dict[str, ArgumentParser]:
    commands = parser.add_subparsers(dest="publish_command", required=True)
    preview = commands.add_parser(
        "preview", help="Save and display the exact review; do not send it"
    )
    preview.add_argument("run_id", metavar="RUN_ID", type=stored_integer)
    preview.add_argument("--version", metavar="N", type=stored_integer, required=True)
    preview.add_argument("--action", choices=("comment", "approve"), required=True)
    preview.add_argument(
        "--summary-only",
        action="store_true",
        help="Put all feedback in the review body",
    )
    parsers = {"preview": preview}
    for name, help_text in (
        ("confirm", "Send exactly this preview"),
        ("recover", "Look for an existing review without sending anything"),
        (
            "retry",
            "Retry the same payload after rejection or an explicit duplicate-risk decision",
        ),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("id", metavar="PREVIEW_ID", type=_parse_id)
        if name == "retry":
            command.add_argument(
                "--accept-duplicate-risk",
                action="store_true",
                help="Allow a resend only after a complete scan finds no marker",
            )
        parsers[name] = command
    return parsers


def _parse_id(text: str) -> PublicationId:
    try:
        return PublicationId(text)
    except PublicationError as error:
        raise ArgumentTypeError(str(error)) from None


def run_publication(
    command: ArgumentParser,
    args: Namespace,
    prepare: PreparePublication,
    publish: PublishReview,
    discard_failed_stdout: Callable[[], None],
) -> int:
    action = args.publish_command
    try:
        if action == "preview":
            preview = prepare.preview(
                DraftTarget(args.run_id, args.version),
                args.action,
                summary_only=args.summary_only,
            )
            try:
                print(render_publication_preview(preview), flush=True)
                print(f"loop-reviewer publish confirm {preview.id.value}", flush=True)
            except OSError, UnicodeError:
                discard_failed_stdout()
                return _error(
                    command,
                    "Could not display the complete preview. No review was sent. Run publish preview again.",
                )
            return 0
        id: PublicationId = args.id
        if action == "confirm":
            outcome = publish.publish(id)
        elif action == "recover":
            outcome = publish.recover(id)
        else:
            outcome = publish.retry(
                id, accept_duplicate_risk=args.accept_duplicate_risk
            )
        return _show_result(command, outcome, id.value, discard_failed_stdout)
    except InvalidRunId as error:
        command.error(str(error))
    except PublicationInterrupted as error:
        return _error(command, _recovery_guidance(error), exit_code=130)
    except KeyboardInterrupt:
        guidance = (
            "Publication preview was interrupted. No review was sent."
            if action == "preview"
            else f"Publication was interrupted. Run loop-reviewer publish recover {args.id.value} to check the result."
        )
        return _error(command, guidance, exit_code=130)
    except ReviewAlreadyCompleted as error:
        return _error(
            command,
            f"{error} Run loop-reviewer review {error.completion.selection.ref.url} to view history.",
        )
    except DraftConflict as error:
        return _error(
            command, f"{error} Use draft show and make a fresh publication preview."
        )
    except PublicationBlocked as error:
        return _error(command, _blocked_guidance(error))
    except UnconfirmedPublication as error:
        return _error(command, unconfirmed_publication_guidance(error))
    except (
        DraftNotFound,
        DraftValidationError,
        GitHubReadError,
        PullRequestNotReviewable,
        PublicationError,
        ReviewStoreError,
    ) as error:
        return _error(command, str(error))


def _blocked_guidance(error: PublicationBlocked) -> str:
    id = error.publication_id.value
    if error.reason == "confirmation_required":
        return f"This preview was not sent to {error.ref.url}. Run loop-reviewer publish confirm {id} to consent."
    if error.reason == "duplicate_risk_consent_required":
        return (
            f"{error} To risk a duplicate after another clean scan: "
            f"loop-reviewer publish retry {id} --accept-duplicate-risk."
        )
    return _recovery_guidance(error)


def _recovery_guidance(error: PublicationBlocked | PublicationInterrupted) -> str:
    return f"{error} For {error.ref.url}, run loop-reviewer publish recover {error.publication_id.value}."


def unconfirmed_publication_guidance(error: UnconfirmedPublication) -> str:
    return f"{error} Run loop-reviewer publish recover {error.publication_id.value}."


def _show_result(
    command: ArgumentParser,
    outcome: PublicationOutcome,
    id: str,
    discard_failed_stdout: Callable[[], None],
) -> int:
    if isinstance(outcome, CompletedReview):
        try:
            print(render_completed_review(outcome), flush=True)
        except OSError, UnicodeError:
            discard_failed_stdout()
            return _error(
                command,
                f"GitHub receipt saved, but stdout failed. Run loop-reviewer publish recover {id} to display the receipt for {outcome.selection.ref.url}.",
            )
        return 0
    reason = None
    if isinstance(outcome, RejectedPublication):
        reason = outcome.reason
        outcome = outcome.record
    preview = outcome.preview
    ref = preview.selection.ref
    if outcome.status == "rejected":
        guidance_by_reason = {
            RejectionReason.AUTHENTICATION: f"Sign in as @{preview.selection.account} with gh auth login --hostname {ref.host}.",
            RejectionReason.ACCESS: "Check that the repository is accessible and the account can submit reviews.",
            RejectionReason.VALIDATION: "GitHub could not validate the review payload.",
            RejectionReason.RATE_LIMIT: "GitHub's rate limit was reached. Wait before retrying.",
        }
        guidance = "" if reason is None else guidance_by_reason[reason] + " "
        fallback = (
            " If inline anchors were rejected, make a fresh --summary-only preview."
            if preview.comments and reason in (None, RejectionReason.VALIDATION)
            else ""
        )
        return _error(
            command,
            f"GitHub rejected the review for {ref.url}. "
            f"{guidance}"
            f"Run loop-reviewer publish retry {id}, or make a fresh preview.{fallback}",
        )
    return _error(
        command,
        f"Publication is unconfirmed for {ref.url}. Run loop-reviewer publish recover {id}. Do not resend without a complete marker scan and an explicit duplicate-risk decision.",
    )


def _error(command: ArgumentParser, message: str, *, exit_code: int = 1) -> int:
    print(f"{command.prog}: {message}", file=sys.stderr)
    return exit_code
