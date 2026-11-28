"""Collect and resolve pinned evidence without a model or provider credentials."""

from dataclasses import dataclass

from loop_reviewer.reviewer.change_coverage import PreparedCoverage, prepare_coverage
from loop_reviewer.reviewer.inventory_contract import ChangedFile
from loop_reviewer.reviewer.local_evidence import (
    EvidenceIndex,
    OptionalRetrieval,
    PinnedEvidenceWork,
)
from loop_reviewer.reviewer.model_contract import Candidate
from loop_reviewer.reviewer.semantic_inputs import EvidenceBundle
from loop_reviewer.reviewer.semantic_policy import PrivateLead
from loop_reviewer.reviewer.source_verification import SourcePacket, build_source_packet


@dataclass
class LocalReviewEvidence:
    coverage: PreparedCoverage
    index: EvidenceIndex
    work: PinnedEvidenceWork

    @classmethod
    def prepare(
        cls, rows: tuple[ChangedFile, ...], work: PinnedEvidenceWork
    ) -> LocalReviewEvidence:
        index = EvidenceIndex()
        return cls(prepare_coverage(rows, work, index), index, work)

    def source_packet(
        self,
        identifier: str,
        candidate: Candidate,
        bundle: EvidenceBundle,
        optional: OptionalRetrieval,
    ) -> SourcePacket | PrivateLead:
        return build_source_packet(
            identifier,
            candidate,
            bundle,
            self.work,
            self.index,
            optional=optional,
            pinned_text=True,
        )
