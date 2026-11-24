"""Immutable provider attestation; only measured profiles belong in production."""

import re
from dataclasses import dataclass
from datetime import date
from typing import Final

from loop_reviewer.reviewer.model_budget import MAX_REPLAY_SPEND
from loop_reviewer.reviewer.model_contract import TokenQuote

_DATED_MODEL = re.compile(r"[a-z0-9-]+-(\d{4}-\d{2}-\d{2})")
_HASH = re.compile(r"[a-f0-9]{64}")


@dataclass(frozen=True)
class QualifiedProfile:
    tokens: TokenQuote
    accepted_input_tokens: int
    investigation_schema_sha256: str
    verification_schema_sha256: str
    qualified_on: date

    def __post_init__(self) -> None:
        match = _DATED_MODEL.fullmatch(self.tokens.profile)
        if (
            match is None
            or date.fromisoformat(match[1]) > self.qualified_on
            or not 0 < self.accepted_input_tokens <= self.tokens.input_cap
            or min(
                self.tokens.output_cap,
                self.tokens.input_micros_per_million,
                self.tokens.output_micros_per_million,
            )
            <= 0
            or 3 * self.tokens.worst_case > MAX_REPLAY_SPEND
            or not _HASH.fullmatch(self.investigation_schema_sha256)
            or not _HASH.fullmatch(self.verification_schema_sha256)
        ):
            raise ValueError("Expected a dated and bounded provider qualification")


QUALIFIED_PROFILE: Final[QualifiedProfile | None] = None
