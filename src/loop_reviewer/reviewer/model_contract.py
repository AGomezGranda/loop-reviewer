"""Strict private model exchanges and the prequoted model port."""

import json
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol, Self

from pydantic import Field, model_validator

from loop_reviewer.reviewer.inventory_contract import _Record

ModelKind = Literal["investigation", "verification"]
AttemptStatus = Literal[
    "success",
    "invalid_output",
    "truncated",
    "transient_error",
    "refused",
    "unsupported_model",
]
Impact = Literal[
    "data_loss",
    "access_control_failure",
    "core_workflow_outage",
    "material_incorrect_behavior",
    "localized_recoverable_regression",
    "unknown",
]
CitationRole = Literal[
    "base_behavior", "head_change", "trigger", "consumer", "consequence", "impact"
]
_Text = Annotated[str, Field(min_length=1, max_length=1024)]
_EvidenceId = Annotated[str, Field(pattern=r"^e[0-9]{5}$")]
_FileId = Annotated[str, Field(pattern=r"^p[0-9]{5}$")]
_InvestigationId = Annotated[str, Field(pattern=r"^i[0-9]{5}$")]
_CandidateId = Annotated[str, Field(pattern=r"^c[0-9]{5}$")]
_EvidenceIds = Annotated[tuple[_EvidenceId, ...], Field(max_length=12)]


def _require_unique(items: tuple[str, ...]) -> None:
    if len(set(items)) != len(items):
        raise ValueError("Duplicate ID")


class Citation(_Record):
    role: CitationRole
    evidence_id: _EvidenceId


class Candidate(_Record):
    file_id: _FileId
    changed_behavior: _Text
    trigger: _Text
    consequence: _Text
    proposed_impact: Impact
    citations: Annotated[tuple[Citation, ...], Field(max_length=12)]
    proposed_anchor_id: _EvidenceId | None

    @model_validator(mode="after")
    def distinct_citations(self) -> Self:
        if len({(c.role, c.evidence_id) for c in self.citations}) != len(
            self.citations
        ):
            raise ValueError("Duplicate citation")
        return self


class _Reply(_Record):
    schema_version: Annotated[int, Field(ge=1, le=1, json_schema_extra={"const": 1})]


class InvestigationReplyV1(_Reply):
    investigation_id: _InvestigationId
    kind: Literal["submit_candidates"]
    reviewed_file_ids: Annotated[tuple[_FileId, ...], Field(max_length=16)]
    candidates: Annotated[tuple[Candidate, ...], Field(max_length=4)]

    @model_validator(mode="after")
    def distinct_files(self) -> Self:
        _require_unique(self.reviewed_file_ids)
        return self


class VerificationReplyV1(_Reply):
    candidate_id: _CandidateId
    assessment: Literal["supports", "contradicts", "insufficient_evidence"]
    evidence_ids: _EvidenceIds
    explanation: _Text

    @model_validator(mode="after")
    def distinct_evidence(self) -> Self:
        _require_unique(self.evidence_ids)
        return self


ModelReply = InvestigationReplyV1 | VerificationReplyV1


@dataclass(frozen=True)
class ModelCall:
    kind: ModelKind
    unit_id: str
    prompt: str
    output_bytes: int = 4096


def _reply_type(
    kind: ModelKind,
) -> type[InvestigationReplyV1 | VerificationReplyV1]:
    return InvestigationReplyV1 if kind == "investigation" else VerificationReplyV1


def strict_json(text: str | bytes) -> object:
    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        if len({k for k, _ in pairs}) != len(pairs):
            raise ValueError("Duplicate JSON key")
        return dict(pairs)

    def invalid_constant(_: str) -> object:
        raise ValueError("Expected a JSON value")

    try:
        return json.loads(
            text, object_pairs_hook=object_pairs, parse_constant=invalid_constant
        )
    except RecursionError:
        raise ValueError("JSON nesting is excessive") from None


def parse_reply(call: ModelCall, content: str) -> ModelReply:
    if len(content.encode("utf-8")) > call.output_bytes:
        raise ValueError("Reply exceeds byte limit")
    strict_json(content)
    reply = _reply_type(call.kind).model_validate_json(content)
    identifier = (
        reply.investigation_id
        if isinstance(reply, InvestigationReplyV1)
        else reply.candidate_id
    )
    if identifier != call.unit_id:
        raise ValueError("Reply names a different unit")
    return reply


def reply_schema(kind: ModelKind) -> dict[str, object]:
    return _reply_type(kind).model_json_schema()


@dataclass(frozen=True)
class TokenQuote:
    profile: str
    input_cap: int
    output_cap: int
    input_micros_per_million: int
    output_micros_per_million: int

    def charge(self, input_tokens: int, output_tokens: int) -> int:
        rate_product = (
            input_tokens * self.input_micros_per_million
            + output_tokens * self.output_micros_per_million
        )
        return (rate_product + 999_999) // 1_000_000

    @property
    def worst_case(self) -> int:
        return self.charge(self.input_cap, self.output_cap)


@dataclass(frozen=True)
class CallQuote:
    call: ModelCall
    request_body: bytes
    tokens: TokenQuote

    @property
    def worst_case(self) -> int:
        return self.tokens.worst_case


@dataclass(frozen=True)
class UnsupportedModel:
    pass


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class ModelAttempt:
    status: AttemptStatus
    usage: TokenUsage | None = None
    reply: ModelReply | None = None
    output_bytes: int | None = None
    billing_compatible: bool = True


class ReviewModel(Protocol):
    def quote(self, call: ModelCall) -> CallQuote | UnsupportedModel: ...

    def send(
        self, call: ModelCall, quote: CallQuote, *, timeout_seconds: float
    ) -> ModelAttempt: ...
