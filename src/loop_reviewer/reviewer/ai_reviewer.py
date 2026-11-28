"""Investigate prepared evidence and independently verify candidates through the model port."""

from collections.abc import Callable
from typing import Protocol

from loop_reviewer.reviewer.coverage_contract import CoverageEvidence
from loop_reviewer.reviewer.local_evidence import (
    EvidenceIndex,
    OptionalRetrieval,
    WorkLimit,
)
from loop_reviewer.reviewer.model_budget import MAX_CALL_SECONDS, SpendLedger
from loop_reviewer.reviewer.model_contract import Candidate, ReviewModel
from loop_reviewer.reviewer.semantic_batch import (
    OPTIONAL_DISPATCH_RESERVE_SECONDS,
    BatchOutcome,
    finish_batch,
    run_coverage_batches,
    verify_source_packet,
)
from loop_reviewer.reviewer.semantic_inputs import EvidenceBundle
from loop_reviewer.reviewer.semantic_limits import Failure, SemanticLimits
from loop_reviewer.reviewer.semantic_policy import Discard, Finding, PrivateLead
from loop_reviewer.reviewer.source_verification import SourcePacket


class LocalEvidence(Protocol):
    @property
    def coverage(self) -> CoverageEvidence: ...

    @property
    def index(self) -> EvidenceIndex: ...

    def source_packet(
        self,
        identifier: str,
        candidate: Candidate,
        bundle: EvidenceBundle,
        optional: OptionalRetrieval,
    ) -> SourcePacket | PrivateLead: ...


def review_evidence(
    local: LocalEvidence,
    model: ReviewModel,
    ledger: SpendLedger,
    limits: SemanticLimits,
    monotonic: Callable[[], float],
) -> BatchOutcome:
    coverage = run_coverage_batches(
        local.coverage,
        model,
        ledger,
        limits,
        monotonic,
        check_citations=False,
        investigation_deadline=limits.deadline
        - MAX_CALL_SECONDS
        - OPTIONAL_DISPATCH_RESERVE_SECONDS,
    )
    limits.preparing_investigation = False

    def verify(
        identifier: str, candidate: Candidate, bundle: EvidenceBundle
    ) -> Finding | PrivateLead | Discard | Failure:
        try:
            packet = local.source_packet(
                identifier,
                candidate,
                bundle,
                OptionalRetrieval(
                    limits.optional_retrieval_limit(),
                    limits.deadline - OPTIONAL_DISPATCH_RESERVE_SECONDS,
                ),
            )
        except WorkLimit:
            return "work_limit"
        if isinstance(packet, PrivateLead):
            return packet
        return verify_source_packet(
            packet, model, ledger, limits, monotonic, pinned_text=True
        )

    return finish_batch(
        coverage.reviewed_ids,
        coverage.gaps,
        coverage.stop,
        [
            (f"c{number:05d}", candidate, bundle)
            for number, (candidate, bundle) in enumerate(coverage.pending, 1)
        ],
        local.index,
        verify,
        pinned_text=True,
    )
