"""Review one pinned comparison, preserving mechanical results on semantic stops."""

from collections.abc import Callable
from contextlib import AbstractContextManager

from loop_reviewer.reviewer.discussion_policy import map_discussion
from loop_reviewer.reviewer.discussion_report import (
    DiscussionDetails,
    DiscussionReviewRequest,
    DiscussionReviewResult,
    render_v4,
)
from loop_reviewer.reviewer.full_review_contract import (
    FullReviewRequest,
    FullReviewResult,
    render_v5,
    text_eligible,
)
from loop_reviewer.reviewer.full_review_v6 import (
    FullReviewRequestV6,
    FullReviewResultV6,
    render_v6,
)
from loop_reviewer.reviewer.inventory import InventorySourceError, validated_inventory
from loop_reviewer.reviewer.inventory_contract import PinnedComparison
from loop_reviewer.reviewer.local_checks import run_local_checks
from loop_reviewer.reviewer.local_evidence import LocalEvidenceWork, PinnedEvidenceWork
from loop_reviewer.reviewer.model_budget import SpendLedger
from loop_reviewer.reviewer.model_contract import ReviewModel
from loop_reviewer.reviewer.pinned_source import PinnedSource
from loop_reviewer.reviewer.python_context import PythonAnalyzer
from loop_reviewer.reviewer.semantic_batch import (
    BatchOutcome,
    run_full_batches,
    run_pinned_batches,
    run_semantic_batch,
)
from loop_reviewer.reviewer.semantic_contract import (
    InvalidReport,
    SemanticReviewRequest,
    SemanticReviewResult,
)
from loop_reviewer.reviewer.semantic_inputs import (
    build_targeted_bundle,
    canonical_inventory,
)
from loop_reviewer.reviewer.semantic_limits import (
    BehaviorGap,
    MeteredSource,
    SemanticLimits,
)
from loop_reviewer.reviewer.semantic_report import (
    assemble_pinned_report,
    assemble_report,
    failed_report,
    render_v3,
)


def review_semantic(
    request: SemanticReviewRequest,
    open_source: Callable[[PinnedComparison], AbstractContextManager[PinnedSource]],
    analyzer: PythonAnalyzer,
    model: ReviewModel | None,
    monotonic: Callable[[], float],
) -> SemanticReviewResult:
    return _execute(
        request,
        open_source,
        analyzer,
        model,
        monotonic,
        lambda payload, _: render_v3(payload),
    )


def review_semantic_with_discussion(
    request: DiscussionReviewRequest,
    open_source: Callable[[PinnedComparison], AbstractContextManager[PinnedSource]],
    analyzer: PythonAnalyzer,
    model: ReviewModel | None,
    monotonic: Callable[[], float],
) -> DiscussionReviewResult:
    if request.comparison != request.discussion.target.comparison:
        raise ValueError("Discussion comparison must match the review pins")
    return _execute(
        request,
        open_source,
        analyzer,
        model,
        monotonic,
        lambda payload, details: render_v4(payload, request.discussion, details),
    )


def review_full(
    request: FullReviewRequest,
    open_source: Callable[[PinnedComparison], AbstractContextManager[PinnedSource]],
    analyzer: PythonAnalyzer,
    model: ReviewModel | None,
    monotonic: Callable[[], float],
) -> FullReviewResult:
    request.require_matching_pins()
    return _execute(
        request,
        open_source,
        analyzer,
        model,
        monotonic,
        lambda payload, details: render_v5(payload, request.discussion, details),
    )


def review_full_v6(
    request: FullReviewRequestV6,
    open_source: Callable[[PinnedComparison], AbstractContextManager[PinnedSource]],
    model: ReviewModel | None,
    monotonic: Callable[[], float],
) -> FullReviewResultV6:
    request.require_matching_pins()
    comparison = request.comparison
    amount = request.budget.max_model_spend_usd_micros
    ledger = SpendLedger(amount, monotonic)
    try:
        with open_source(comparison) as source:
            rows = validated_inventory(source, comparison)
            ledger = SpendLedger(amount, monotonic, multiple_investigations=True)
            discussion = None
            if request.discussion is not None:
                discussion_limits = SemanticLimits(ledger.deadline)
                discussion = map_discussion(
                    request.discussion,
                    rows,
                    PinnedEvidenceWork(
                        MeteredSource(source, discussion_limits, monotonic),
                        comparison,
                        monotonic,
                        ledger.deadline,
                    ),
                )
            if amount and model is not None:
                limits = SemanticLimits(
                    ledger.deadline,
                    preparing_investigation=True,
                    preserve_verification_reserve=True,
                )
                work = PinnedEvidenceWork(
                    MeteredSource(source, limits, monotonic),
                    comparison,
                    monotonic,
                    ledger.deadline,
                    retrieval_capacity=limits.retrieval_capacity,
                )
                batch = run_pinned_batches(rows, work, model, ledger, limits)
            else:
                reason: BehaviorGap = (
                    "unsupported_model" if amount else "budget_exhausted"
                )
                gaps = {
                    file.id: reason
                    for file in canonical_inventory(rows)
                    if text_eligible(file.change)
                }
                batch = BatchOutcome(
                    (),
                    gaps,
                    (),
                    (),
                    (),
                    (),
                    "budget_exhausted" if gaps and not amount else None,
                )
        payload, details = assemble_pinned_report(
            comparison, ledger, rows, batch, discussion
        )
        return render_v6(payload, request.discussion, details)
    except InventorySourceError as error:
        payload = failed_report(comparison, ledger, error.error)
    except ValueError:
        payload = _invalid_report(comparison, ledger)
    return render_v6(payload, request.discussion, DiscussionDetails())


def _execute[Report](
    request: SemanticReviewRequest | DiscussionReviewRequest | FullReviewRequest,
    open_source: Callable[[PinnedComparison], AbstractContextManager[PinnedSource]],
    analyzer: PythonAnalyzer,
    model: ReviewModel | None,
    monotonic: Callable[[], float],
    render: Callable[[dict[str, object], DiscussionDetails], Report],
) -> Report:
    full = isinstance(request, FullReviewRequest)
    comparison = request.comparison
    amount = request.budget.max_model_spend_usd_micros
    ledger = SpendLedger(amount, monotonic)
    try:
        with open_source(comparison) as source:
            rows = validated_inventory(source, comparison)
            started = monotonic()
            ledger = SpendLedger(
                amount, monotonic, started_at=started, multiple_investigations=full
            )
            work = LocalEvidenceWork(
                source,
                analyzer,
                comparison,
                monotonic,
                min(started + 30, ledger.deadline),
                utf8_only=full,
            )
            checks, index, findings, stopped = run_local_checks(
                rows,
                work,
                retrieve_context=False,
                eligible=text_eligible if full else None,
            )
            compiled_failures = {
                finding.anchor.path
                for finding in findings
                if finding.anchor is not None
            }
            settled = frozenset(
                row
                for row in rows
                if not full
                and row.new is not None
                and row.new.path in compiled_failures
            )
            run_model = bool(amount and model is not None)
            limits = SemanticLimits(
                ledger.deadline,
                preparing_investigation=True,
                preserve_verification_reserve=full,
            )
            discussion = None
            if run_model or request.discussion is not None:
                work.deadline = ledger.deadline
                work.source = MeteredSource(source, limits, monotonic)
                if request.discussion is not None:
                    discussion = map_discussion(request.discussion, rows, work)
            discussion_paths = (
                discussion.unresolved_paths if discussion else frozenset()
            )
            if run_model and model is not None and full:
                batch = run_full_batches(
                    rows,
                    work,
                    index,
                    model,
                    ledger,
                    limits,
                    discussion_paths=discussion_paths,
                )
            elif run_model and model is not None:
                bundle = build_targeted_bundle(
                    comparison,
                    rows,
                    work,
                    index,
                    limits.preparation_limit(investigation=True),
                    settled=settled,
                    discussion_paths=discussion_paths,
                )
                limits.preparing_investigation = False
                batch = run_semantic_batch(
                    bundle,
                    source,
                    model,
                    ledger,
                    limits,
                    monotonic,
                    analyzer=analyzer,
                    evidence=index,
                )
            else:
                reason: BehaviorGap = (
                    "budget_exhausted" if not amount else "unsupported_model"
                )
                gaps = {
                    f"p{number:05d}": reason
                    for number, check in enumerate(checks, 1)
                    if (
                        text_eligible(check.row)
                        if full
                        else check.investigation_id is not None
                    )
                    and check.row not in settled
                }
                batch = BatchOutcome(
                    (),
                    gaps,
                    (),
                    (),
                    (),
                    (),
                    "budget_exhausted" if gaps and not amount else None,
                )
    except InventorySourceError as error:
        return render(
            failed_report(comparison, ledger, error.error), DiscussionDetails()
        )
    except ValueError:
        return render(_invalid_report(comparison, ledger), DiscussionDetails())
    try:
        included = {entry.id for entry in batch.evidence}
        for finding in batch.findings:
            included.update(
                entry.id
                for entry in index.entries
                if entry.kind == "source" and entry.range == finding.anchor
            )
        return render(
            *assemble_report(
                comparison,
                ledger,
                checks,
                findings,
                tuple(
                    entry
                    for entry in index.entries
                    if entry.kind == "diagnostic" or entry.id in included
                ),
                batch,
                stopped,
                discussion,
                full=full,
            )
        )
    except ValueError:
        return render(_invalid_report(comparison, ledger), DiscussionDetails())


def _invalid_report(
    comparison: PinnedComparison, ledger: SpendLedger
) -> dict[str, object]:
    return failed_report(
        comparison,
        ledger,
        InvalidReport(
            code="invalid_report",
            message="Could not assemble a valid pinned review report.",
        ),
    )
