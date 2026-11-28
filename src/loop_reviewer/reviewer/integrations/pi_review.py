"""Pi subprocess adapter for the AI reviewer's bounded model port."""

import json
import os
from collections.abc import Mapping
from pathlib import Path
from subprocess import TimeoutExpired
from tempfile import TemporaryDirectory
from typing import cast

from loop_reviewer.integrations.review_process import (
    OutputLimitExceeded,
    capture_review_process,
    with_absolute_path,
)
from loop_reviewer.reviewer.model_contract import (
    AttemptStatus,
    CallQuote,
    ModelAttempt,
    ModelCall,
    RequestTooLarge,
    TokenQuote,
    TokenUsage,
    UnsupportedModel,
    parse_reply,
    reply_schema,
    strict_json,
)
from loop_reviewer.reviewer.semantic_limits import VERIFY_PROMPT

_INPUT_FRAMING_TOKENS = 1024


class PiSetupError(ValueError):
    pass


PI_MODEL = "LOOP_REVIEWER_PI_MODEL"
_KEYS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
PI_SETTINGS = (PI_MODEL, "PI_CODING_AGENT_DIR", *_KEYS.values())
PI_ENV = ("PATH", "HOME", "XDG_DATA_HOME", *PI_SETTINGS)


def _exchange(
    environment: Mapping[str, str], request: bytes, timeout: float
) -> dict[str, object]:
    with TemporaryDirectory(prefix="loop-reviewer-pi-") as scratch:
        result = capture_review_process(
            [
                "pi",
                "--mode",
                "rpc",
                "--offline",
                "--no-session",
                "--no-tools",
                "--no-mcp",
                "--no-skills",
                "--no-context-files",
                "--no-prompt-templates",
                "--no-extensions",
                "--no-approve",
                "-e",
                str(Path(__file__).with_name("pi_request.mjs")),
            ],
            cwd=Path(scratch),
            env=environment,
            timeout=timeout,
            output_limit=64 * 1024,
            input=request,
        )
    if result.returncode:
        raise ValueError("Pi failed")
    reply = strict_json(result.stdout)
    return reply if isinstance(reply, dict) else {}


class PiReview:
    def __init__(
        self,
        environment: dict[str, str],
        profile: dict[str, object],
        tokens: TokenQuote,
    ):
        self.environment = environment
        self.profile = profile
        self.tokens = tokens

    @classmethod
    def require_configured(cls, environment: Mapping[str, str]) -> PiReview:
        name = environment.get(PI_MODEL, "")
        provider, slash, model = name.partition("/")
        if not slash or not provider or not model:
            raise PiSetupError(f"Set {PI_MODEL} to provider/exact-model-id.")
        names = {
            "PATH",
            "HOME",
            "XDG_DATA_HOME",
            "PI_CODING_AGENT_DIR",
            _KEYS.get(provider),
        }
        child = with_absolute_path({k: v for k, v in environment.items() if k in names})
        child.setdefault("PATH", os.defpath)
        try:
            response = _exchange(child, json.dumps({"model": name}).encode(), 10)
        except FileNotFoundError:
            raise PiSetupError("Install Pi 1.7.0 and put pi on PATH.") from None
        except TimeoutExpired:
            raise PiSetupError(
                "Pi did not answer the model check within 10 seconds."
            ) from None
        except OSError, ValueError, OutputLimitExceeded:
            raise PiSetupError(
                "Could not start or read Pi. Check that Pi 1.7.0 runs on this PATH."
            ) from None
        if response.get("reason") == "unknown_model":
            raise PiSetupError(
                f"The model selected by {PI_MODEL} is not in the Pi catalog."
            )
        if response.get("reason") == "missing_auth":
            raise PiSetupError(
                "Authenticate the selected provider in Pi before reviewing."
            )
        profile = response.get("profile")
        if response.get("status") != "ready" or not isinstance(profile, dict):
            raise PiSetupError(
                "The selected Pi model needs a supported protocol and positive catalog pricing."
            )
        values = [
            profile.get(k) for k in ("inputCap", "outputCap", "inputRate", "outputRate")
        ]
        if (profile.get("provider"), profile.get("id")) != (provider, model) or not all(
            type(v) is int and 0 < v < 2**53 for v in values
        ):
            raise PiSetupError("Pi returned an invalid model profile.")
        return cls(child, profile, TokenQuote(name, *cast(list[int], values)))

    def quote(self, call: ModelCall) -> CallQuote | UnsupportedModel:
        schema = json.dumps(reply_schema(call.kind), separators=(",", ":"))
        prompt = f"Return only a JSON object matching this schema:\n{schema}\n\n{call.prompt}"
        # Quote UTF-8 bytes plus protocol framing, then check the actual provider payload.
        input_cap = (
            len(json.dumps(prompt, ensure_ascii=False).encode()) + _INPUT_FRAMING_TOKENS
        )
        body = json.dumps(
            {
                "model": self.tokens.profile,
                "profile": self.profile,
                "prompt": prompt,
                "inputCap": input_cap,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        available = self.tokens.input_cap - self.tokens.output_cap
        if not 0 < call.output_bytes <= 16 * 1024:
            return UnsupportedModel()
        if input_cap > available or len(body) > 80 * 1024:
            return RequestTooLarge()
        verification_cap = min(available, VERIFY_PROMPT + _INPUT_FRAMING_TOKENS)
        if call.kind == "verification":
            verification_cap = input_cap
        return CallQuote(
            call,
            body,
            self.tokens,
            input_cap,
            self.tokens.charge(verification_cap, self.tokens.output_cap),
        )

    def send(
        self, call: ModelCall, quote: CallQuote, *, timeout_seconds: float
    ) -> ModelAttempt:
        if quote != self.quote(call) or not 0 < timeout_seconds <= 20:
            return ModelAttempt("unsupported_model")
        try:
            response = _exchange(self.environment, quote.request_body, timeout_seconds)
        except OutputLimitExceeded:
            return ModelAttempt("truncated")
        except TimeoutExpired, OSError:
            return ModelAttempt("transient_error")
        except ValueError:
            return ModelAttempt("invalid_output")
        status = response.get("status")
        if status not in (
            "success",
            "truncated",
            "transient_error",
            "unsupported_model",
            "invalid_output",
        ):
            return ModelAttempt("invalid_output")
        if "model" in response and response["model"] != self.tokens.profile:
            return ModelAttempt("unsupported_model", billing_compatible=False)
        counts = response.get("usage")
        usage = None
        if (
            isinstance(counts, list)
            and len(counts) == 3
            and all(type(n) is int and n >= 0 for n in counts)
            and counts[2] > 0
            and sum(counts[:2]) == counts[2]
        ):
            usage = TokenUsage(*counts)
        if status != "success":
            return ModelAttempt(cast(AttemptStatus, status), usage)
        content = response.get("content")
        if response.get("model") != self.tokens.profile or not isinstance(content, str):
            return ModelAttempt("invalid_output", usage)
        size = None
        try:
            size = len(content.encode())
            if size > call.output_bytes:
                return ModelAttempt("truncated", usage, output_bytes=size)
            reply = parse_reply(call, content)
        except ValueError:
            return ModelAttempt("invalid_output", usage, output_bytes=size)
        return ModelAttempt("success", usage, reply, size)
