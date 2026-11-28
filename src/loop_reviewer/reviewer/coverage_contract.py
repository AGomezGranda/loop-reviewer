"""Investigation packets and the coverage evidence port."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from typing import Protocol

from loop_reviewer.reviewer.pinned_source import SourceRange
from loop_reviewer.reviewer.semantic_inputs import BundleFile, EvidenceBundle, Omission


@dataclass(frozen=True)
class CoverageRange:
    file_id: str
    source: SourceRange

    def lines(self) -> Iterator[CoverageRange]:
        for line in range(self.source.start_line, self.source.end_line + 1):
            yield CoverageRange(
                self.file_id, replace(self.source, start_line=line, end_line=line)
            )


@dataclass(frozen=True)
class CoveragePacket:
    bundle: EvidenceBundle
    offered: frozenset[CoverageRange]
    unsendable: frozenset[CoverageRange]


class CoverageEvidence(Protocol):
    @property
    def inventory(self) -> tuple[BundleFile, ...]: ...

    @property
    def required(self) -> tuple[CoverageRange, ...]: ...

    @property
    def omissions(self) -> tuple[Omission, ...]: ...

    @property
    def has_support(self) -> bool: ...

    @property
    def support_deadline(self) -> float | None: ...

    def packet(
        self,
        unoffered: frozenset[CoverageRange],
        max_bytes: int,
        *,
        supporting_bytes: int,
        supporting_fits: Callable[[str], bool] | None = None,
        reference_bytes: int = 0,
        reference_deadline: float | None = None,
    ) -> CoveragePacket: ...
