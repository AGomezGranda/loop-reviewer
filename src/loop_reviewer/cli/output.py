"""Render review results as terminal output."""

import sys
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import assert_never

from loop_reviewer.review.feedback import finding_lines
from loop_reviewer.review.generate import (
    DraftReview,
    FailedReview,
    FinishedReview,
    ReviewOutcome,
)
from loop_reviewer.review.model import (
    CompletedReview,
    ReviewSelection,
    SavedDraft,
)
from loop_reviewer.review.publication_model import PublicationPreview
from loop_reviewer.review.pull_request import PullRequestRef
from loop_reviewer.reviewer.full_review_contract import FullReviewResult


def selection_lines(selection: ReviewSelection) -> list[str]:
    ref = selection.ref
    revision = selection.revision
    return [
        f"PR: {ref.owner}/{ref.repo}#{ref.number}",
        f"Host: {ref.host}",
        f"Account: {selection.account}",
        f"Author: {selection.author}",
        f"Target branch: {revision.target_branch}",
        f"Base tip: {revision.base_tip}",
        f"Merge base: {revision.merge_base}",
        f"Head: {revision.head}",
    ]


def render_draft(run: SavedDraft) -> str:
    notice = "private draft; nothing was posted to GitHub"
    feedback = (
        f"Recommendation: {run.draft.verdict} ({notice})"
        if run.report is None
        else f"Editable feedback ({notice}):"
    )
    return "\n".join(
        [
            *selection_lines(run.selection),
            _comparison_line(run.selection),
            f"Run ID: {run.id}",
            f"Version: {run.draft_version}",
            *_generation_lines(run.generation_model, run.estimated_cost_usd),
            feedback,
            *_findings_lines(run),
            "Summary:",
            run.draft.summary,
            *_report_lines(run.report),
        ]
    )


def render_publication_preview(preview: PublicationPreview) -> str:
    selection = preview.selection
    lines = [
        f"Destination: {selection.ref.url}",
        f"Event: {preview.action.upper()}",
        f"Host: {selection.ref.host}",
        f"Account: @{selection.account}",
        f"Target branch: {selection.revision.target_branch}",
        f"Merge base: {selection.revision.merge_base}",
        f"Head: {selection.revision.head}",
        f"Preview ID: {preview.id.value}",
        "----- BEGIN REVIEW BODY -----",
        preview.body,
        "----- END REVIEW BODY -----",
    ]
    for number, comment in enumerate(preview.comments, start=1):
        path = comment.path.replace("\n", "\\n").replace("\t", "\\t")
        lines.extend(
            (
                f"Inline {number}: {path}:{comment.line} (RIGHT)",
                "----- BEGIN INLINE BODY -----",
                comment.body,
                "----- END INLINE BODY -----",
            )
        )
    return "\n".join(lines)


def render_completed_review(review: CompletedReview) -> str:
    draft = review.final_draft
    receipt = review.receipt
    if receipt is None:
        status = "Finished locally — not published"
        receipt_lines: list[str] = []
    else:
        status = (
            "Published — Comment"
            if receipt.action == "comment"
            else "Published — Approval submitted"
        )
        receipt_lines = [
            f"GitHub review ID: {receipt.review_id}",
            f"GitHub review: {review.selection.ref.url}#pullrequestreview-{receipt.review_id}",
        ]
    return "\n".join(
        [
            status,
            *receipt_lines,
            *selection_lines(review.selection),
            _comparison_line(review.selection),
            *_generation_lines(review.generation_model, review.estimated_cost_usd),
            f"Recommendation: {draft.verdict}"
            if review.report is None
            else "Final feedback:",
            "Summary:",
            draft.summary,
            *_findings_lines(review),
            *_report_lines(review.report),
        ]
    )


def _findings_lines(review: SavedDraft | CompletedReview) -> list[str]:
    draft = review.draft if isinstance(review, SavedDraft) else review.final_draft
    if findings := finding_lines(draft):
        return ["Findings:", *findings]
    if (report := review.report) is not None:
        if report.root.findings:
            return ["No retained findings"]
        if report.root.status == "partial":
            return ["No verified findings in the reviewed scope"]
    return ["No findings"]


def _report_lines(saved: FullReviewResult | None) -> list[str]:
    if saved is None:
        return []
    report = saved.root
    usage = report.usage
    lines = [
        "",
        "Original reviewer report (private):",
        f"Scope: {report.scope} · Status: {report.status} · Stop: {report.stop_reason}",
        f"Original recommendation: {report.recommendation} · Discussion: {report.discussion_status}",
        f"Usage: {usage.model_calls} model calls · {usage.input_tokens} input / {usage.output_tokens} output tokens",
        f"Estimated cost: {usage.estimated_cost_usd_micros} USD micros · Held: {usage.unaccounted_reserved_usd_micros} USD micros · Accounting fault: {'yes' if usage.accounting_fault else 'no'}",
    ]
    if report.findings:
        lines.append("Original verified findings:")
        for item in report.findings:
            lines.append(
                f"  {item.id} [{item.severity}] {item.trigger!a} — {item.consequence!a}"
            )
    lines.append(f"File coverage ({len(report.file_coverage)}):")
    for row in report.file_coverage:
        file = row.new or row.old
        assert file is not None
        reason = f" ({row.reason})" if row.reason else ""
        lines.append(
            f"  {file.path}: {row.outcome} · {row.highest_completed_check}{reason}"
        )
    return lines


def _generation_lines(model: str | None, amount: float | None) -> list[str]:
    lines = []
    if model:
        lines.append(
            f"Generation model: {model if model.isprintable() else ascii(model)}"
        )
    if amount is None:
        return [*lines, "Cost unavailable"]
    with localcontext(rounding=ROUND_HALF_UP):
        usd = "< $0.0001" if 0 < amount < 0.0001 else f"${Decimal(str(amount)):.4f}"
    return [*lines, f"Estimated generation cost · {usd}"]


def _comparison_line(selection: ReviewSelection) -> str:
    ref, revision = selection.ref, selection.revision
    return f"Comparison: https://{ref.host}/{ref.owner}/{ref.repo}/compare/{revision.merge_base}...{revision.head}"


def render_review_outcome(outcome: ReviewOutcome, ref: PullRequestRef) -> int:
    url = ref.url
    if isinstance(outcome, FinishedReview):
        print(render_completed_review(outcome.completion))
        return 0
    if isinstance(outcome, DraftReview):
        title = (
            "Private draft generated"
            if outcome.status == "generated"
            else "Private draft resumed"
        )
        lines = [title, render_draft(outcome.run)]
        if outcome.changes:
            notice = (
                "Could not verify the current revision after review."
                if outcome.changes == "unavailable"
                else f"The pull request changed during review: {', '.join(outcome.changes)}."
            )
            lines.append(
                f"Notice: {notice} Run loop-reviewer review {url} "
                "to review the latest selection."
            )
        print("\n".join(lines))
        return 0

    if isinstance(outcome, FailedReview):
        guidance = (
            f"When a supported comparison is available, run loop-reviewer review {url}."
            if outcome.kind == "input"
            else f"Fix the problem, then run loop-reviewer review --retry {url}."
        )
        print(f"loop-reviewer: error: {outcome.message} {guidance}", file=sys.stderr)
        return 1

    status = outcome.status
    if status == "in_progress":
        print(
            "A review is already in progress for this comparison. "
            f"Wait for it to finish, then run loop-reviewer review {url}.",
            file=sys.stderr,
        )
        return 1
    if status == "abandoned":
        print(
            "The previous review attempt was abandoned. "
            f"Run loop-reviewer review --retry {url} to generate a new draft.",
            file=sys.stderr,
        )
        return 1
    if status == "no_retryable_run":
        print(
            f"There is no failed or abandoned attempt to retry. "
            f"Run loop-reviewer review {url}.",
            file=sys.stderr,
        )
        return 1
    assert_never(status)
