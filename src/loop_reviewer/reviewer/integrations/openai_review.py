"""Single-request, fail-closed Chat Completions transport for the pilot."""

import json
import time
from collections.abc import Callable
from hashlib import sha256
from http.client import HTTPException, HTTPSConnection
from queue import Empty, Queue
from socket import SHUT_RDWR, socket
from threading import Event, Thread
from typing import cast

from loop_reviewer.reviewer.integrations.openai_profiles import QualifiedProfile
from loop_reviewer.reviewer.model_contract import (
    CallQuote,
    ModelAttempt,
    ModelCall,
    ModelKind,
    TokenQuote,
    TokenUsage,
    UnsupportedModel,
    parse_reply,
    reply_schema,
    strict_json,
)

_HOST = "api.openai.com"
_PATH = "/v1/chat/completions"
MAX_ENVELOPE_BYTES = 64 * 1024
_MAX_REQUEST_BYTES = 80 * 1024
_INPUT_FRAMING_TOKENS = 1024

Transport = Callable[[bytes, str, float, int], tuple[int, bytes]]


class EnvelopeOverflow(Exception):
    pass


def post_completion(
    body: bytes, api_key: str, timeout_seconds: float, limit: int
) -> tuple[int, bytes]:
    """Bound the entire exchange, including DNS, slow headers, and chunked bodies."""
    deadline = time.monotonic() + timeout_seconds
    connection = HTTPSConnection(_HOST, timeout=timeout_seconds)
    result: Queue[tuple[int, bytes] | Exception] = Queue(maxsize=1)
    cancelled = Event()
    wire: socket | None = None

    def exchange() -> None:
        nonlocal wire
        try:
            connection.connect()
            wire = connection.sock
            remaining = deadline - time.monotonic()
            if wire is None or cancelled.is_set() or remaining <= 0:
                raise TimeoutError
            wire.settimeout(remaining)
            connection.request(
                "POST",
                _PATH,
                body=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                },
            )
            with connection.getresponse() as response:
                encoding = response.getheader("Content-Encoding", "identity")
                if encoding != "identity":
                    raise HTTPException
                if response.length is not None and response.length > limit:
                    raise EnvelopeOverflow
                payload = response.read(limit + 1)
                if len(payload) > limit:
                    raise EnvelopeOverflow
                if response.length:
                    raise HTTPException
                result.put_nowait((response.status, payload))
        except (EnvelopeOverflow, HTTPException, OSError, ValueError) as error:
            result.put_nowait(error)
        finally:
            connection.close()

    Thread(target=exchange, daemon=True).start()
    try:
        outcome = result.get(timeout=max(0, deadline - time.monotonic()))
        if time.monotonic() >= deadline:
            raise TimeoutError
    except Empty:
        raise TimeoutError from None
    finally:
        cancelled.set()
        if wire is not None:
            try:
                wire.shutdown(SHUT_RDWR)
            except OSError:
                pass
    if isinstance(outcome, Exception):
        raise outcome
    return outcome


def _usage(raw: object) -> TokenUsage | None:
    if not isinstance(raw, dict):
        return None
    counts = tuple(
        raw.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")
    )
    if not all(type(n) is int for n in counts):
        return None
    return TokenUsage(*cast(tuple[int, int, int], counts))


def _attempt(
    call: ModelCall, tokens: TokenQuote, status: int, raw: bytes
) -> ModelAttempt:
    if len(raw) > MAX_ENVELOPE_BYTES:
        return ModelAttempt("truncated")
    try:
        envelope = strict_json(raw)
    except ValueError:
        envelope = None
    obj = cast(dict[str, object], envelope) if isinstance(envelope, dict) else {}
    usage = _usage(obj.get("usage"))
    compatible = (
        obj.get("model") == tokens.profile and obj.get("service_tier") == "default"
    )
    if status != 200:
        return ModelAttempt(
            "transient_error"
            if status in (408, 409, 429) or 500 <= status < 600
            else "unsupported_model",
            usage,
            billing_compatible=compatible or usage is None,
        )
    if obj.get("object") != "chat.completion":
        return ModelAttempt(
            "invalid_output", usage, billing_compatible=compatible or usage is None
        )
    if not compatible:
        return ModelAttempt("unsupported_model", usage, billing_compatible=False)
    choices = obj.get("choices")
    if (
        not isinstance(choices, list)
        or len(choices) != 1
        or not isinstance(choices[0], dict)
    ):
        return ModelAttempt("invalid_output", usage)
    choice = cast(dict[str, object], choices[0])
    message = choice.get("message")
    if (
        type(choice.get("index")) is not int
        or choice["index"] != 0
        or not isinstance(message, dict)
        or message.get("role") != "assistant"
    ):
        return ModelAttempt("invalid_output", usage)
    content = message.get("content")
    try:
        size = len(content.encode("utf-8")) if isinstance(content, str) else 0
    except UnicodeError:
        return ModelAttempt("invalid_output", usage)
    finish = choice.get("finish_reason")
    if message.get("refusal") is not None or finish == "content_filter":
        return ModelAttempt("refused", usage, output_bytes=size)
    if finish == "length" or size > call.output_bytes:
        return ModelAttempt("truncated", usage, output_bytes=size)
    if (
        finish != "stop"
        or not isinstance(content, str)
        or any(
            message.get(key) is not None
            for key in ("tool_calls", "function_call", "audio")
        )
    ):
        return ModelAttempt("invalid_output", usage, output_bytes=size)
    try:
        reply = parse_reply(call, content)
    except ValueError:
        return ModelAttempt("invalid_output", usage, output_bytes=size)
    return ModelAttempt("success", usage, reply, size)


_SCHEMA_KEYS = frozenset(
    {
        "$defs",
        "$ref",
        "additionalProperties",
        "anyOf",
        "enum",
        "items",
        "properties",
        "required",
        "type",
    }
)


def _project(node: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in node.items():
        if key in ("properties", "$defs"):
            result[key] = {
                name: _project(child)
                for name, child in cast(dict[str, dict[str, object]], value).items()
            }
        elif key == "items":
            result[key] = _project(cast(dict[str, object], value))
        elif key == "anyOf":
            result[key] = [
                _project(child) for child in cast(list[dict[str, object]], value)
            ]
        elif key == "const":
            result["enum"] = [value]
        elif key in _SCHEMA_KEYS:
            result[key] = value
    return result


def provider_schema(kind: ModelKind) -> dict[str, object]:
    return _project(reply_schema(kind))


def schema_digest(kind: ModelKind) -> str:
    return sha256(
        json.dumps(
            provider_schema(kind), sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


class OpenAIReview:
    def __init__(
        self,
        api_key: str,
        transport: Transport = post_completion,
        *,
        profile: QualifiedProfile | None = None,
    ) -> None:
        self._key = api_key
        self._post = transport
        self._profile = (
            profile
            if profile is not None
            and (
                profile.investigation_schema_sha256 == schema_digest("investigation")
                and profile.verification_schema_sha256 == schema_digest("verification")
            )
            else None
        )
        self._prepared: CallQuote | None = None

    def quote(self, call: ModelCall) -> CallQuote | UnsupportedModel:
        self._prepared = None
        profile = self._profile
        if profile is None or not self._key or not 0 < call.output_bytes <= 16 * 1024:
            return UnsupportedModel()
        body = json.dumps(
            {
                "model": profile.tokens.profile,
                "messages": [{"role": "user", "content": call.prompt}],
                "max_completion_tokens": profile.tokens.output_cap,
                "n": 1,
                "store": False,
                "stream": False,
                "service_tier": "default",
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": f"reviewer_{call.kind}_v1",
                        "strict": True,
                        "schema": provider_schema(call.kind),
                    },
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if (
            len(body) > _MAX_REQUEST_BYTES
            or len(body) + _INPUT_FRAMING_TOKENS > profile.accepted_input_tokens
        ):
            return UnsupportedModel()
        self._prepared = CallQuote(call, body, profile.tokens)
        return self._prepared

    def send(
        self, call: ModelCall, quote: CallQuote, *, timeout_seconds: float
    ) -> ModelAttempt:
        if (
            quote is not self._prepared
            or quote.call != call
            or len(quote.request_body) > _MAX_REQUEST_BYTES
            or not 0 < timeout_seconds <= 20
        ):
            return ModelAttempt(
                "unsupported_model", TokenUsage(0, 0, 0), output_bytes=0
            )
        self._prepared = None
        try:
            status, raw = self._post(
                quote.request_body, self._key, timeout_seconds, MAX_ENVELOPE_BYTES
            )
        except EnvelopeOverflow:
            return ModelAttempt("truncated")
        except HTTPException, OSError, ValueError:
            return ModelAttempt("transient_error")
        return _attempt(call, quote.tokens, status, raw)
