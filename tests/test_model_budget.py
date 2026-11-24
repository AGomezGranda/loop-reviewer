from dataclasses import replace

import pytest

from loop_reviewer.reviewer.model_budget import SpendLedger
from loop_reviewer.reviewer.model_contract import (
    CallQuote,
    ModelAttempt,
    ModelCall,
    TokenQuote,
    TokenUsage,
)

INVESTIGATE = ModelCall("investigation", "i00001", "fixture")
VERIFY = ModelCall("verification", "c00001", "fixture")
PRICE = TokenQuote("fixture", 100, 10, 50_000, 400_000)


def quote(call: ModelCall = INVESTIGATE) -> CallQuote:
    return CallQuote(call, b"fixture", PRICE)


def test_protects_two_verification_attempts_and_rounds_each_charge() -> None:
    worst = PRICE.worst_case
    ledger = SpendLedger(worst * 3 - 1, lambda: 0)
    assert ledger.reserve(quote(), "investigation") is None
    assert ledger.blocked_reason == "budget_exhausted"
    ledger = SpendLedger(worst * 3, lambda: 0)
    for call in (INVESTIGATE, VERIFY):
        reservation = ledger.reserve(quote(call), call.kind)
        assert reservation is not None
        assert ledger.reserve(quote(call), call.kind) is None
        ledger.settle(reservation, TokenUsage(1, 1, 2))
    assert ledger.snapshot().estimated_cost_usd_micros == 2


def test_unknown_billing_is_held_through_single_replacement() -> None:
    ledger = SpendLedger(4 * PRICE.worst_case, lambda: 0)
    for _ in range(2):
        reservation = ledger.reserve(quote(), "investigation")
        assert reservation is not None
        ledger.settle(reservation, None, status="transient_error")
    assert ledger.reserve(quote(), "investigation") is None
    snapshot = ledger.snapshot()
    assert snapshot.model_calls == 2
    assert snapshot.unaccounted_reserved_usd_micros == 2 * PRICE.worst_case


@pytest.mark.parametrize(
    ("usage", "fault"),
    [
        (TokenUsage(-1, 0, 0), False),
        (TokenUsage(1, 1, 3), False),
        (TokenUsage(101, 0, 101), True),
        (TokenUsage(101, 0, 1), True),
    ],
    ids=[
        "negative",
        "contradictory-total",
        "exceeds-quote",
        "exceeds-with-contradiction",
    ],
)
def test_invalid_usage_holds_reservation(usage: TokenUsage, fault: bool) -> None:
    ledger = SpendLedger(100, lambda: 0)
    reservation = ledger.reserve(quote(), "investigation")
    assert reservation is not None
    ledger.settle(reservation, usage, status="invalid_output")
    assert ledger.snapshot().unaccounted_reserved_usd_micros == PRICE.worst_case
    assert ledger.snapshot().accounting_fault is fault
    if fault:
        assert ledger.reserve(quote(), "investigation") is None
    with pytest.raises(ValueError):
        ledger.settle(reservation, usage)


@pytest.mark.parametrize("calls", [0, 1, 2])
def test_call_slots_protected_before_investigation(calls: int) -> None:
    ledger = SpendLedger(100, lambda: 0, call_limit=calls)
    assert ledger.reserve(quote(), "investigation") is None
    assert ledger.blocked_reason == "work_limit"


def test_call_deadline_and_no_unreserved_dispatch() -> None:
    class Model:
        def quote(self, call: ModelCall) -> CallQuote:
            return quote(call)

        def send(
            self, call: ModelCall, quote: CallQuote, *, timeout_seconds: float
        ) -> ModelAttempt:
            assert call == INVESTIGATE and timeout_seconds == 1
            return ModelAttempt("refused")

    now = 119.0
    ledger = SpendLedger(100, lambda: now, started_at=0)
    other = SpendLedger(100, lambda: now, started_at=0)
    reservation = ledger.reserve(quote(), "investigation")
    assert reservation is not None
    with pytest.raises(ValueError):
        other.dispatch(Model(), reservation)
    assert ledger.dispatch(Model(), reservation).status == "refused"
    with pytest.raises(ValueError):
        ledger.dispatch(Model(), reservation)
    ledger.settle(reservation, None, status="refused")
    assert ledger.reserve(quote(), "investigation") is None
    now = 120
    assert ledger.reserve(quote(VERIFY), "verification") is None
    assert ledger.blocked_reason == "work_limit"


def test_total_calls_and_candidate_slots_are_bounded() -> None:
    ledger = SpendLedger(1000, lambda: 0)
    units = (INVESTIGATE, *(replace(VERIFY, unit_id=f"c{n:05d}") for n in range(1, 5)))
    for unit in units:
        for _ in range(2):
            reservation = ledger.reserve(quote(unit), unit.kind)
            assert reservation is not None
            ledger.settle(reservation, None, status="truncated")
    assert ledger.snapshot().model_calls == 10
    assert (
        ledger.reserve(quote(replace(VERIFY, unit_id="c00005")), "verification") is None
    )


@pytest.mark.parametrize(
    ("amount", "replay"),
    [(3_000_001, False), (250_001, True), (-1, False), (True, False)],
)
def test_budget_bounds(amount: int, replay: bool) -> None:
    with pytest.raises(ValueError):
        SpendLedger(amount, lambda: 0, replay=replay)
    assert SpendLedger(0, lambda: 0).reserve(quote(), "investigation") is None
