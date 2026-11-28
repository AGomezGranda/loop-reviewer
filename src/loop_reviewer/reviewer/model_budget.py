"""Conservative pre-dispatch accounting for one semantic batch."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from loop_reviewer.reviewer.inventory_contract import MAX_RUNTIME_SPEND
from loop_reviewer.reviewer.model_contract import (
    AttemptStatus,
    CallQuote,
    ModelAttempt,
    ModelKind,
    ReviewModel,
    TokenQuote,
    TokenUsage,
)

MAX_REPLAY_SPEND = 250_000
MAX_SECONDS = 120
MAX_CALL_SECONDS = 20
MAX_CALLS = 10
MAX_VERIFICATIONS = 4
RETRYABLE = frozenset({"invalid_output", "truncated", "transient_error"})
BudgetBlock = Literal["work_limit", "budget_exhausted", "accounting_fault"]


@dataclass(frozen=True)
class Reservation:
    quote: CallQuote


@dataclass(frozen=True)
class SpendSnapshot:
    model_calls: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd_micros: int
    unaccounted_reserved_usd_micros: int
    accounting_fault: bool


class SpendLedger:
    def __init__(
        self,
        amount: int,
        monotonic: Callable[[], float],
        *,
        started_at: float | None = None,
        replay: bool = False,
        call_limit: int = MAX_CALLS,
        multiple_investigations: bool = False,
    ) -> None:
        if type(amount) is not int or not 0 <= amount <= (
            MAX_REPLAY_SPEND if replay else MAX_RUNTIME_SPEND
        ):
            raise ValueError("Invalid model spend limit")
        if not 0 <= call_limit <= MAX_CALLS:
            raise ValueError("Invalid model call limit")
        self.amount = amount
        self._monotonic = monotonic
        self.deadline = (
            monotonic() if started_at is None else started_at
        ) + MAX_SECONDS
        self._call_limit = call_limit
        self._multiple_investigations = multiple_investigations
        self._active: Reservation | None = None
        self._dispatched = False
        self._tokens: TokenQuote | None = None
        self._units: dict[tuple[ModelKind, str], list[AttemptStatus]] = {}
        self._calls = self._input = self._output = self._spent = self._held = 0
        self._fault = False
        self.blocked_reason: BudgetBlock | None = None

    def investigation_context_fits(self, quote: CallQuote, later_packets: int) -> bool:
        # Budget up to two attempts per preceding packet and protected dispatch of the last.
        return (
            self._spent + self._held + (3 + 2 * later_packets) * quote.worst_case
            <= self.amount
        )

    def reserve(self, quote: CallQuote, purpose: ModelKind) -> Reservation | None:
        self.blocked_reason = self._block(quote, purpose)
        if self.blocked_reason is not None:
            return None
        self._tokens = quote.tokens
        self._active = Reservation(quote)
        self._dispatched = False
        self._calls += 1
        self._held += quote.worst_case
        return self._active

    def _block(self, quote: CallQuote, purpose: ModelKind) -> BudgetBlock | None:
        tokens = quote.tokens
        if (
            purpose != quote.call.kind
            or not quote.request_body
            or min(
                tokens.input_cap,
                tokens.output_cap,
                tokens.input_micros_per_million,
                tokens.output_micros_per_million,
            )
            <= 0
            or (self._tokens is not None and self._tokens != tokens)
        ):
            raise ValueError("Model quote does not match the run profile")
        if self._fault:
            return "accounting_fault"
        if self._active is not None or self._monotonic() >= self.deadline:
            return "work_limit"
        unit = (purpose, quote.call.unit_id)
        attempts = self._units.get(unit, [])
        if attempts and (len(attempts) >= 2 or attempts[-1] not in RETRYABLE):
            return "work_limit"
        protects = purpose == "investigation"
        if (
            protects
            and not self._multiple_investigations
            and any(key != unit for key in self._units)
        ):
            return "work_limit"
        if (
            not protects
            and not attempts
            and sum(key[0] == "verification" for key in self._units)
            >= MAX_VERIFICATIONS
        ):
            return "work_limit"
        slots = 3 if protects else 1
        if self._calls + slots > self._call_limit:
            return "work_limit"
        if (
            not self.amount
            or self._spent + self._held + slots * quote.worst_case > self.amount
        ):
            return "budget_exhausted"
        return None

    def dispatch(self, model: ReviewModel, reservation: Reservation) -> ModelAttempt:
        if reservation is not self._active or self._dispatched:
            raise ValueError("Dispatch requires an unused reservation")
        self._dispatched = True
        time_left = self.deadline - self._monotonic()
        if time_left <= 0:
            # No I/O took place; the caller can settle this authorized slot at zero.
            return ModelAttempt("transient_error", TokenUsage(0, 0, 0), output_bytes=0)
        return model.send(
            reservation.quote.call,
            reservation.quote,
            timeout_seconds=min(MAX_CALL_SECONDS, time_left),
        )

    def settle(
        self,
        reservation: Reservation,
        usage: TokenUsage | None,
        *,
        status: AttemptStatus = "success",
        billing_compatible: bool = True,
    ) -> None:
        if reservation is not self._active:
            raise ValueError("Settlement requires the active reservation")
        self._active = None
        quote = reservation.quote
        self._units.setdefault((quote.call.kind, quote.call.unit_id), []).append(status)
        self._fault |= not billing_compatible
        if usage is None:
            return
        counts = usage.input_tokens, usage.output_tokens, usage.total_tokens
        if any(type(count) is not int for count in counts):
            return
        tokens = quote.tokens
        over = (
            usage.input_tokens > tokens.input_cap
            or usage.output_tokens > tokens.output_cap
            or usage.total_tokens > tokens.input_cap + tokens.output_cap
        )
        self._fault |= over
        if min(counts) < 0 or sum(counts[:2]) != usage.total_tokens:
            return
        if over:
            return
        self._input += usage.input_tokens
        self._output += usage.output_tokens
        if not billing_compatible:
            return
        self._held -= quote.worst_case
        self._spent += tokens.charge(usage.input_tokens, usage.output_tokens)

    def snapshot(self) -> SpendSnapshot:
        return SpendSnapshot(
            self._calls, self._input, self._output, self._spent, self._held, self._fault
        )
