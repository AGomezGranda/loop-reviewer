import json
from dataclasses import replace

import pytest

from loop_reviewer.reviewer.integrations.openai_review import (
    MAX_ENVELOPE_BYTES,
    EnvelopeOverflow,
    OpenAIReview,
)
from loop_reviewer.reviewer.model_budget import SpendLedger
from loop_reviewer.reviewer.model_contract import TokenUsage, UnsupportedModel
from tests.support.openai_profile import FIXTURE_PROFILE, PILOT_PROFILE
from tests.test_model_contract import CALL, REPLY


@pytest.mark.parametrize(
    ("case", "status", "has_usage"),
    [
        ("success", "success", True),
        ("bad-json", "invalid_output", True),
        ("length", "truncated", True),
        ("refusal", "refused", True),
        ("timeout", "transient_error", False),
        ("bad-usage", "success", False),
        ("oversized", "truncated", False),
        ("bounded-read", "truncated", False),
        ("wrong-model", "unsupported_model", True),
        ("wrong-tier", "unsupported_model", True),
        ("wrong-tier-http-error", "transient_error", True),
        ("wrong-choice-count", "invalid_output", True),
        ("redirect", "unsupported_model", False),
        ("tools", "invalid_output", True),
        ("bad-envelope", "invalid_output", False),
    ],
)
def test_openai_bounded_exchange(case: str, status: str, has_usage: bool) -> None:
    requests: list[bytes] = []

    def transport(
        body: bytes, api_key: str, timeout_seconds: float, limit: int
    ) -> tuple[int, bytes]:
        assert (
            api_key == "fixture"
            and timeout_seconds == 20
            and limit == MAX_ENVELOPE_BYTES
        )
        requests.append(body)
        message: dict[str, object] = {
            "role": "assistant",
            "content": json.dumps(REPLY),
            "refusal": None,
        }
        choice = {"index": 0, "finish_reason": "stop", "message": message}
        usage: dict[str, object] = {
            "prompt_tokens": 12,
            "completion_tokens": 8,
            "total_tokens": 20,
        }
        envelope = {
            "object": "chat.completion",
            "model": PILOT_PROFILE.profile,
            "service_tier": "default",
            "choices": [choice],
            "usage": usage,
        }
        match case:
            case "bad-json":
                message["content"] = "not json"
            case "length":
                choice["finish_reason"] = "length"
            case "refusal":
                message["refusal"] = "no"
            case "timeout":
                raise TimeoutError
            case "bad-usage":
                usage["prompt_tokens"] = "12"
            case "oversized":
                return 200, b"a" * (limit + 1)
            case "bounded-read":
                raise EnvelopeOverflow
            case "wrong-model":
                envelope["model"] = "gpt-5-nano"
            case "wrong-tier":
                envelope["service_tier"] = "priority"
            case "wrong-tier-http-error":
                envelope["service_tier"] = "priority"
                return 503, json.dumps(envelope).encode()
            case "wrong-choice-count":
                envelope["choices"].append(choice)
            case "redirect":
                return 302, b""
            case "tools":
                message["tool_calls"] = [{"id": "not-text"}]
            case "bad-envelope":
                return 200, b"not json"
        return 200, json.dumps(envelope).encode()

    model = OpenAIReview("fixture", transport, profile=FIXTURE_PROFILE)
    quote = model.quote(CALL)
    assert not isinstance(quote, UnsupportedModel)
    assert quote.tokens == PILOT_PROFILE and quote.worst_case == 20_820
    ledger = SpendLedger(250_000, lambda: 0, replay=True)
    reservation = ledger.reserve(quote, CALL.kind)
    assert reservation is not None
    result = ledger.dispatch(model, reservation)
    assert len(requests) == 1 and requests[0] == quote.request_body
    payload = json.loads(requests[0])
    assert payload.keys() == {
        "model",
        "messages",
        "max_completion_tokens",
        "n",
        "store",
        "stream",
        "service_tier",
        "response_format",
    }
    assert payload["model"] == PILOT_PROFILE.profile
    assert (
        payload["n"] == 1 and payload["store"] is False and payload["stream"] is False
    )
    assert (
        payload["service_tier"] == "default"
        and payload["max_completion_tokens"] == 2048
    )
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert result.status == status
    assert (result.reply is not None) == (status == "success")
    assert result.usage == (TokenUsage(12, 8, 20) if has_usage else None)
    assert result.billing_compatible == (
        case not in {"wrong-model", "wrong-tier", "wrong-tier-http-error"}
    )
    if case == "wrong-tier-http-error":
        ledger.settle(
            reservation,
            result.usage,
            status=result.status,
            billing_compatible=result.billing_compatible,
        )
        assert ledger.snapshot().unaccounted_reserved_usd_micros == quote.worst_case
        assert ledger.snapshot().accounting_fault


def test_profile_must_attest_both_current_schemas_before_any_request() -> None:
    def forbid(*_: object) -> tuple[int, bytes]:
        pytest.fail("unexpected POST")

    stale = replace(FIXTURE_PROFILE, verification_schema_sha256="0" * 64)
    assert isinstance(
        OpenAIReview("fixture", forbid, profile=stale).quote(CALL), UnsupportedModel
    )


def test_unqualified_or_changed_prepared_request_never_posts() -> None:
    def forbid(*_: object) -> tuple[int, bytes]:
        pytest.fail("unexpected POST")

    assert isinstance(OpenAIReview("fixture", forbid).quote(CALL), UnsupportedModel)
    assert isinstance(
        OpenAIReview("", forbid, profile=FIXTURE_PROFILE).quote(CALL), UnsupportedModel
    )
    model = OpenAIReview("fixture", forbid, profile=FIXTURE_PROFILE)
    quote = model.quote(CALL)
    assert not isinstance(quote, UnsupportedModel)
    changed = replace(quote, request_body=b"changed")
    assert model.send(CALL, changed, timeout_seconds=1).status == "unsupported_model"
