import json
import shutil
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import pytest

from loop_reviewer import composition
from loop_reviewer.cli import main as hosted_main
from loop_reviewer.reviewer.integrations import pi_review
from loop_reviewer.reviewer.integrations.pi_review import PI_MODEL, PiReview
from loop_reviewer.reviewer.model_contract import (
    CallQuote,
    ModelCall,
    TokenQuote,
    TokenUsage,
)
from loop_reviewer.semantic_review_cli import main
from tests.support.inventory_git import commit, init, revision
from tests.test_pinned_review import PinnedClaimModel


@pytest.mark.skipif(shutil.which("pi") is None, reason="requires the optional Pi CLI")
@pytest.mark.parametrize("case", ["clean", "consumer", "malformed", "small-context"])
def test_standalone_review_through_pi(
    case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    init(repo)
    changed = repo / "config.ts"
    (repo / "consumer").write_text("send(endpoint)\n")
    for value in (1, 2):
        if case == "small-context":
            changed.write_text(
                "".join(f'const item{n} = "{str(value) * 100}";\n' for n in range(8))
            )
        else:
            changed.write_text(f"export const endpoint = {value};\n")
        commit(repo)
    scripted = PinnedClaimModel("clean" if case == "small-context" else case)
    prompts: list[str] = []

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            assert self.path == "/v1/chat/completions"
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert request["model"] == "fixture"
            assert not request.get("tools")
            assert (
                0
                < (request.get("max_tokens") or request["max_completion_tokens"])
                <= 2048
            )
            prompt = request["messages"][-1]["content"]
            if isinstance(prompt, list):
                prompt = prompt[0]["text"]
            prompt = prompt.split("\n\n", 1)[1]
            prompts.append(prompt)
            if case == "malformed":
                answer = "invalid"
            else:
                call = (
                    ModelCall("verification", "c00001", prompt)
                    if prompt.startswith("Independently")
                    else ModelCall(
                        "investigation",
                        prompt.split("Investigation ID: ", 1)[1][:6],
                        prompt,
                    )
                )
                reply = scripted.send(
                    call, scripted.quote(call), timeout_seconds=20
                ).reply
                assert reply is not None
                answer = reply.model_dump_json()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks: list[tuple[dict[str, str], str | None]] = [
                ({"role": "assistant", "content": answer}, None),
                ({}, "stop"),
            ]
            for delta, finish in chunks:
                chunk = {
                    "id": "fixture",
                    "model": "fixture",
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 30,
                        "total_tokens": 130,
                    },
                }
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")

    with HTTPServer(("127.0.0.1", 0), Provider) as server:
        agent_dir = tmp_path / "pi"
        agent_dir.mkdir()
        (agent_dir / "models.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "fixture": {
                            "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
                            "apiKey": "test",
                            "api": "openai-completions",
                            "models": [
                                {
                                    "id": "fixture",
                                    "contextWindow": 8192
                                    if case == "small-context"
                                    else 400_000,
                                    "maxTokens": 2048,
                                    "cost": {
                                        "input": 2.5,
                                        "output": 15,
                                        "cacheRead": 0,
                                        "cacheWrite": 0,
                                    },
                                }
                            ],
                        }
                    }
                }
            )
        )
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
        monkeypatch.setenv(PI_MODEL, "fixture/fixture")
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "loop-reviewer-semantic",
                "--repo",
                str(repo),
                "--merge-base",
                revision(repo, "HEAD^"),
                "--head",
                revision(repo, "HEAD"),
                "--report-version",
                "6",
                "--allow-provider",
                "--max-model-spend-usd-micros",
                "250000",
            ],
        )
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            status = main()
        finally:
            server.shutdown()
            thread.join()
    report = json.loads(capsys.readouterr().out)
    assert status == (3 if case == "malformed" else 0), report
    if case == "small-context":
        assert len(prompts) > 1
    else:
        assert len(prompts) == (1 if case == "clean" else 2)
    assert len(report["findings"]) == (1 if case == "consumer" else 0)
    assert report["usage"]["model_calls"] == len(prompts)


def test_pi_rejects_invalid_unicode_without_losing_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "fixture/fixture"
    model = PiReview({}, {}, TokenQuote(name, 100_000, 2048, 1_000_000, 1_000_000))
    call = ModelCall("investigation", "i00001", "Review this change.")
    quote = model.quote(call)
    assert isinstance(quote, CallQuote)

    def exchange(*_args: object) -> dict[str, object]:
        return {
            "status": "success",
            "model": name,
            "usage": [100, 30, 130],
            "content": "\ud800",
        }

    monkeypatch.setattr(pi_review, "_exchange", exchange)
    attempt = model.send(call, quote, timeout_seconds=20)
    assert attempt.status == "invalid_output"
    assert attempt.usage == TokenUsage(100, 30, 130)


def test_pi_profile_change_stops_reserved_call(monkeypatch: pytest.MonkeyPatch) -> None:
    name = "fixture/fixture"
    calls: list[bytes] = []

    def exchange(
        environment: dict[str, str], body: bytes, timeout: float
    ) -> dict[str, object]:
        assert "GH_TOKEN" not in environment and "OPENAI_API_KEY" not in environment
        calls.append(body)
        if len(calls) == 1:
            return {
                "status": "ready",
                "profile": {
                    "provider": "fixture",
                    "id": "fixture",
                    "inputCap": 10000,
                    "outputCap": 2048,
                    "inputRate": 1000000,
                    "outputRate": 1000000,
                },
            }
        return {"status": "unsupported_model"}

    monkeypatch.setattr(pi_review, "_exchange", exchange)
    env = {PI_MODEL: name, "GH_TOKEN": "private", "OPENAI_API_KEY": "private"}
    assert composition._hosted_model(env)[1] is None
    assert calls == []
    _, model, selected = composition._hosted_model(
        env | {"LOOP_REVIEWER_MAX_MODEL_SPEND_USD_MICROS": "100000"}
    )
    assert model is not None and selected == name
    call = ModelCall("investigation", "i00001", "inspect evidence")
    quote = model.quote(call)
    assert isinstance(quote, CallQuote)
    larger = model.quote(ModelCall("investigation", "i00001", call.prompt + "é" * 200))
    assert isinstance(larger, CallQuote)
    assert larger.worst_case - quote.worst_case == 400
    assert quote.worst_case < quote.tokens.worst_case
    assert model.send(call, quote, timeout_seconds=20).status == "unsupported_model"
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ({"reason": "missing_auth"}, "Authenticate the selected provider"),
        ({"reason": "unknown_model"}, "not in the Pi catalog"),
        (
            {"status": "unsupported_model"},
            "supported protocol and positive catalog pricing",
        ),
    ],
    ids=["missing-auth", "unknown-model", "unsupported-profile"],
)
def test_hosted_model_check_reports_setup_problem(
    monkeypatch: pytest.MonkeyPatch, response: dict[str, object], message: str
) -> None:
    def exchange(_env: object, body: bytes, _timeout: float) -> dict[str, object]:
        assert json.loads(body) == {"model": "fixture/fixture"}
        return response

    monkeypatch.setattr(pi_review, "_exchange", exchange)
    with pytest.raises(composition.ReviewerError, match=message) as error:
        composition.Workflows.check_review_model(
            {
                PI_MODEL: "fixture/fixture",
                "LOOP_REVIEWER_MAX_MODEL_SPEND_USD_MICROS": "100000",
            }
        )
    assert error.value.kind == "setup"


@pytest.mark.parametrize(
    ("allowance", "status", "message"),
    [
        ("0", 0, "AI analysis is off"),
        ("1", 1, "cannot start an investigation"),
        ("250000", 0, "No provider request was made"),
    ],
    ids=["disabled", "insufficient", "ready"],
)
def test_check_review_model_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    allowance: str,
    status: int,
    message: str,
) -> None:
    def exchange(_env: object, body: bytes, _timeout: float) -> dict[str, object]:
        assert allowance != "0"
        assert json.loads(body) == {"model": "fixture/fixture"}
        return {
            "status": "ready",
            "profile": {
                "provider": "fixture",
                "id": "fixture",
                "inputCap": 400_000,
                "outputCap": 2048,
                "inputRate": 2_500_000,
                "outputRate": 15_000_000,
            },
        }

    monkeypatch.setattr(pi_review, "_exchange", exchange)
    monkeypatch.setenv(PI_MODEL, "fixture/fixture")
    monkeypatch.setenv("LOOP_REVIEWER_MAX_MODEL_SPEND_USD_MICROS", allowance)
    monkeypatch.setattr(sys, "argv", ["loop-reviewer", "check-review-model"])
    if status:
        with pytest.raises(SystemExit) as error:
            hosted_main()
        assert error.value.code == status
        assert message in capsys.readouterr().err
    else:
        assert hosted_main() == status
        assert message in capsys.readouterr().out
