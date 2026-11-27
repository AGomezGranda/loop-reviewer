"""Meter the source and serialized inputs of one semantic arm."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Never, override

from loop_reviewer.reviewer.inventory import RevisionSide
from loop_reviewer.reviewer.inventory_contract import ChangedFile
from loop_reviewer.reviewer.local_evidence import WorkLimit
from loop_reviewer.reviewer.pinned_source import (
    Hunk,
    PinnedSource,
    PinnedTextBlob,
    ReadFailure,
    SearchPaths,
)

VERIFY_EVIDENCE = 2 * 1024
VERIFY_PROMPT = 12 * 1024
VERIFY_OUTPUT = 4 * 1024


class VerificationPacketLimit(Exception):
    pass


Failure = Literal[
    "work_limit",
    "budget_exhausted",
    "accounting_fault",
    "invalid_output",
    "truncated",
    "transient_error",
    "refused",
    "unsupported_model",
]
BehaviorGap = (
    Failure | Literal["missing_evidence", "not_scheduled", "unsupported_language"]
)


def hunk_retrieval_bytes(result: tuple[Hunk, ...] | ReadFailure, cap: int) -> int:
    return cap if isinstance(result, str) else sum(len(hunk.patch) for hunk in result)


@dataclass
class SemanticLimits:
    deadline: float
    evidence_limit: int = 64 * 1024
    prompt_limit: int = 80 * 1024
    combined_limit: int = 160 * 1024
    output_limit: int = 16 * 1024
    retrieved_bytes: int = 0
    evidence_bytes: int = 0
    prompt_bytes: int = 0
    output_bytes: int = 0
    hit_limit: bool = False
    preparing_investigation: bool = False
    preserve_verification_reserve: bool = False

    def check_time(self, monotonic: Callable[[], float]) -> None:
        if monotonic() >= self.deadline:
            self.hit_limit = True
            raise WorkLimit

    @property
    def retrieval_left(self) -> int:
        return max(0, self.combined_limit - self.retrieved_bytes - self.prompt_bytes)

    def retrieved(self, size: int) -> None:
        self.retrieved_bytes += size
        if (
            self.retrieval_left == 0
            and self.retrieved_bytes + self.prompt_bytes > self.combined_limit
        ):
            self.hit_limit = True
            raise WorkLimit

    def preparation_limit(self, *, investigation: bool) -> int:
        rest = self.evidence_limit - self.evidence_bytes
        return max(
            0, rest - VERIFY_EVIDENCE if investigation else min(rest, VERIFY_EVIDENCE)
        )

    def output_cap(self, *, investigation: bool) -> int:
        rest = self.output_limit - self.output_bytes
        return max(
            0, rest - VERIFY_OUTPUT if investigation else min(rest, VERIFY_OUTPUT)
        )

    def prepare(
        self, evidence: int, request: int, *, investigation: bool, trim: bool = False
    ) -> None:
        if (
            self.preserve_verification_reserve
            and not investigation
            and (evidence > VERIFY_EVIDENCE or request > VERIFY_PROMPT)
        ):
            raise VerificationPacketLimit
        evidence_cap = self.preparation_limit(investigation=investigation)
        remaining = self.prompt_limit - self.prompt_bytes
        prompt_cap = min(self.retrieval_left, remaining)
        if investigation:
            prompt_cap -= VERIFY_PROMPT
        else:
            prompt_cap = min(prompt_cap, VERIFY_PROMPT)
        fits = evidence <= evidence_cap and request <= prompt_cap
        if trim and not fits:
            raise VerificationPacketLimit
        if fits or not self.preserve_verification_reserve:
            self.evidence_bytes += evidence
            self.prompt_bytes += request
        if not fits:
            self.hit_limit = True
            raise WorkLimit

    def received(self, size: int | None, reserved: int) -> bool:
        self.output_bytes += reserved if size is None else size
        if self.output_bytes > self.output_limit:
            self.hit_limit = True
            return False
        return True


class MeteredSource(PinnedSource):
    def __init__(
        self,
        source: PinnedSource,
        limits: SemanticLimits,
        monotonic: Callable[[], float],
    ) -> None:
        self._source = source
        self._limits = limits
        self._monotonic = monotonic

    @override
    def inventory(self) -> tuple[ChangedFile, ...]:
        return self._source.inventory()

    def _allocate(self, max_bytes: int) -> int:
        self._limits.check_time(self._monotonic)
        remaining = self._limits.retrieval_left
        if self._limits.preparing_investigation:
            remaining = min(
                remaining,
                self._limits.combined_limit
                - self._limits.prompt_limit
                - self._limits.retrieved_bytes,
            )
        size = min(max_bytes, remaining)
        if size <= 0:
            self._limits.hit_limit = True
            raise WorkLimit
        return size

    def _limited(self) -> Never:
        self._limits.hit_limit = True
        raise WorkLimit

    def _charge(
        self, actual: int | None, reserved: int, *, truncated: bool = False
    ) -> None:
        self._limits.retrieved(reserved if actual is None else actual)
        if truncated or (actual is not None and actual > reserved):
            self._limited()
        self._limits.check_time(self._monotonic)

    @override
    def read_text_blob(
        self, side: RevisionSide, path: str, *, max_bytes: int, timeout_seconds: float
    ) -> PinnedTextBlob | ReadFailure:
        cap = self._allocate(max_bytes)
        result = self._source.read_text_blob(
            side, path, max_bytes=cap, timeout_seconds=timeout_seconds
        )
        self._charge(
            (0 if result == "not_found" else None)
            if isinstance(result, str)
            else len(result.text),
            cap,
            truncated=result == "limit_reached",
        )
        return result

    @override
    def hunks(
        self, change: ChangedFile, *, max_bytes: int, timeout_seconds: float
    ) -> tuple[Hunk, ...] | ReadFailure:
        cap = self._allocate(max_bytes)
        result = self._source.hunks(
            change, max_bytes=cap, timeout_seconds=timeout_seconds
        )
        self._charge(
            hunk_retrieval_bytes(result, cap),
            cap,
            truncated=result == "limit_reached",
        )
        return result

    @override
    def search(
        self,
        side: RevisionSide,
        literal: str,
        *,
        max_paths: int,
        max_bytes: int,
        timeout_seconds: float,
        regular_text: bool = False,
    ) -> SearchPaths | ReadFailure:
        cap = self._allocate(max_bytes)
        result = self._source.search(
            side,
            literal,
            max_paths=max_paths,
            max_bytes=cap,
            timeout_seconds=timeout_seconds,
            regular_text=regular_text,
        )
        self._charge(
            None if isinstance(result, str) else result.retrieved_bytes,
            cap,
            truncated=result == "limit_reached",
        )
        return result
