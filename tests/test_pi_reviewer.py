"""Exercise Pi invocation and validation of generated feedback."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from loop_reviewer.integrations import pi_reviewer
from loop_reviewer.integrations.pi_reviewer import PiReviewer
from loop_reviewer.integrations.review_process import OutputLimitExceeded
from loop_reviewer.review.generate import ReviewerError
from loop_reviewer.review.model import (
    Finding,
    FindingLocation,
    ReviewDraft,
    ReviewSelection,
)
from loop_reviewer.review.pull_request import PullRequestRef, ReviewedRevision

_PATCH = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,3 @@
 line one
+added line
 line two
"""
_MERGE_BASE, _HEAD = (digit * 40 for digit in "bc")
_SELECTION = ReviewSelection(
    PullRequestRef("github.com", "acme", "repo", 7),
    "reviewer",
    "teammate",
    ReviewedRevision("main", "a" * 40, _MERGE_BASE, _HEAD),
)
_TOKENS = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
)
_ENVIRONMENT = {
    "PATH": "/usr/bin",
    **{token: f"{token}-secret" for token in _TOKENS},
    "GH_FORCE_TTY": "1",
}
_SECRET = "raw subprocess output should never be shown"


class StubComparison:
    def __init__(self, patch: str) -> None:
        self.calls = 0
        self.patch = patch

    def read(self, selection: ReviewSelection) -> str:
        self.calls += 1
        return self.patch


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "verdict": "comment",
        "summary": "Summary",
        "findings": [],
    }
    payload.update(overrides)
    return payload


def _response(
    payload: object, *, returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        ["pi"],
        returncode,
        json.dumps(payload),
        _SECRET,
    )


def _install(
    monkeypatch: pytest.MonkeyPatch,
    response: subprocess.CompletedProcess[str] | Exception,
    *,
    model: str | None = "provider/model",
    which: str | None = "/fake/pi",
    inherited_fd: int | None = None,
    patch: str = _PATCH,
) -> tuple[
    PiReviewer,
    StubComparison,
    list[tuple[list[str], dict[str, object]]],
    dict[str, object],
]:
    comparison = StubComparison(patch)
    calls: list[tuple[list[str], dict[str, object]]] = []
    observed: dict[str, object] = {}

    def fake_run(
        arguments: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((arguments, kwargs))
        cwd = Path(str(kwargs["cwd"]))
        observed["cwd_exists"] = cwd.is_dir()
        observed["cwd_empty"] = not any(cwd.iterdir())
        if isinstance(response, Exception):
            raise response
        output = response.stdout.encode("utf-8")
        limit = kwargs["output_limit"]
        assert isinstance(limit, int)
        if len(output) > limit:
            raise OutputLimitExceeded
        return subprocess.CompletedProcess(arguments, response.returncode, output)

    def fake_which(name: str, path: str | None = None) -> str | None:
        return which

    monkeypatch.setattr(pi_reviewer.shutil, "which", fake_which)
    monkeypatch.setattr(pi_reviewer, "capture_review_process", fake_run)
    return (
        PiReviewer(
            comparison.read,
            model,
            _ENVIRONMENT,
            inherited_fd=inherited_fd,
        ),
        comparison,
        calls,
        observed,
    )


def test_pi_lookup_uses_default_path_when_injected_environment_omits_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/ambient-path")

    def fake_which(name: str, path: str | None = None) -> str | None:
        assert name == "pi"
        assert path == os.defpath
        return None

    monkeypatch.setattr(pi_reviewer.shutil, "which", fake_which)
    comparison = StubComparison(_PATCH)
    reviewer = PiReviewer(comparison.read, "provider/model", {})
    with pytest.raises(ReviewerError, match="Install the Pi CLI"):
        reviewer.review(_SELECTION)
    assert comparison.calls == 0


def test_pi_receives_only_selected_diff(monkeypatch: pytest.MonkeyPatch) -> None:
    reviewer, comparison, calls, observed = _install(
        monkeypatch, _response(_payload()), inherited_fd=9
    )

    draft = reviewer.review(_SELECTION)

    assert draft == ReviewDraft("comment", "Summary", ())
    assert comparison.calls == 1
    arguments, kwargs = calls[0]
    assert arguments == [
        "/fake/pi",
        "--print",
        "--model",
        "provider/model",
        "--no-session",
        "--no-tools",
        "--no-extensions",
        "--no-mcp",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
        "--no-approve",
        "--system-prompt",
        pi_reviewer._SYSTEM_PROMPT,
    ]
    raw_input = kwargs["input"]
    assert isinstance(raw_input, bytes)
    prompt = raw_input.decode("utf-8")
    assert "+added line" in prompt
    assert _MERGE_BASE in prompt
    assert _HEAD in prompt
    assert kwargs["timeout"] == 180
    assert kwargs["pass_fds"] == (9,)
    assert observed == {"cwd_exists": True, "cwd_empty": True}
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    for token in _TOKENS:
        assert token not in environment
    assert environment["GH_PROMPT_DISABLED"] == "1"
    assert "GH_FORCE_TTY" not in environment


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param(_payload(), ReviewDraft("comment", "Summary", ()), id="none"),
        pytest.param(
            _payload(
                findings=[
                    {
                        "severity": "high",
                        "condition": "Missing check",
                        "consequence": "Bad input is accepted",
                        "location": {"path": "src/app.py", "right_line": 2},
                    }
                ]
            ),
            ReviewDraft(
                "comment",
                "Summary",
                (
                    Finding(
                        "high",
                        "Missing check",
                        "Bad input is accepted",
                        FindingLocation("src/app.py", 2),
                    ),
                ),
            ),
            id="findings",
        ),
        pytest.param(
            _payload(
                findings=[
                    {
                        "severity": "low",
                        "condition": "Unanchored",
                        "consequence": "No location",
                        "location": {"path": "src/app.py", "right_line": 99},
                    }
                ]
            ),
            ReviewDraft(
                "comment",
                "Summary",
                (Finding("low", "Unanchored", "No location", None),),
            ),
            id="unanchorable-location",
        ),
    ],
)
def test_pi_accepts_valid_review_output(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
    expected: ReviewDraft,
) -> None:
    reviewer, _, _, _ = _install(monkeypatch, _response(payload))

    assert reviewer.review(_SELECTION) == expected


@pytest.mark.parametrize(
    ("header", "path"),
    [
        pytest.param('"b/caf\\303\\251.py"', "café.py", id="git-quoted-path"),
        pytest.param("b/with space.py\t", "with space.py", id="git-path-terminator"),
    ],
)
def test_pi_keeps_anchors_from_git_patch(
    monkeypatch: pytest.MonkeyPatch, header: str, path: str
) -> None:
    patch = f"+++ {header}\n@@ -1 +1,2 @@\n-old\n+within\u2028+same line\n+target\n"
    payload = _payload(
        findings=[
            {
                "severity": "high",
                "condition": "Missing check",
                "consequence": "Bad input is accepted",
                "location": {"path": path, "right_line": 2},
            }
        ]
    )
    reviewer, _, calls, _ = _install(monkeypatch, _response(payload), patch=patch)
    assert reviewer.review(_SELECTION).findings[0].location == FindingLocation(path, 2)
    prompt = calls[0][1]["input"]
    assert isinstance(prompt, bytes)
    assert prompt.split(b"<diff>\n")[1].removesuffix(b"</diff>\n") == patch.encode(
        "utf-8"
    )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        pytest.param("{", "invalid", id="truncated-json"),
        pytest.param(json.dumps(_payload(verdict=[])), "invalid", id="array-verdict"),
        pytest.param(
            json.dumps(_payload(findings=[{"severity": {}}])),
            "invalid",
            id="object-severity",
        ),
        pytest.param(
            {"verdict": "approve", "summary": "s", "findings": []},
            "invalid",
            id="invalid-verdict",
        ),
        pytest.param(
            _payload(
                findings=[
                    {
                        "severity": "critical",
                        "condition": "c",
                        "consequence": "c",
                        "location": None,
                    }
                ]
            ),
            "invalid",
            id="invalid-severity",
        ),
        pytest.param(
            _payload(
                findings=[
                    {
                        "severity": "high",
                        "condition": "c",
                        "consequence": "c",
                        "location": {"path": "src/app.py", "right_line": "2"},
                    }
                ]
            ),
            "invalid",
            id="malformed-location",
        ),
        pytest.param(
            _payload(
                findings=[
                    {
                        "severity": "high",
                        "condition": "c",
                        "consequence": "c",
                        "location": None,
                        "extra": True,
                    }
                ]
            ),
            "invalid",
            id="extra-finding-key",
        ),
        pytest.param(
            _payload(summary="bad\x1b[31m"), "invalid", id="control-characters"
        ),
        pytest.param(_payload(extra=True), "invalid", id="extra-top-level-key"),
        pytest.param(_payload(summary=""), "invalid", id="empty-summary"),
    ],
)
def test_pi_rejects_invalid_review_output(
    monkeypatch: pytest.MonkeyPatch, payload: object, message: str
) -> None:
    stdout = payload if isinstance(payload, str) else json.dumps(payload)
    reviewer, _, _, _ = _install(
        monkeypatch, subprocess.CompletedProcess(["pi"], 0, stdout, _SECRET)
    )

    with pytest.raises(ReviewerError, match=message) as error:
        reviewer.review(_SELECTION)

    assert error.value.kind == "failed"
    assert _SECRET not in str(error.value)


@pytest.mark.parametrize(
    ("response", "model", "which", "message", "kind", "fetched"),
    [
        pytest.param(
            _response(_payload()),
            None,
            "/fake/pi",
            "LOOP_REVIEWER_PI_MODEL",
            "setup",
            False,
            id="missing-model",
        ),
        pytest.param(
            _response(_payload()),
            "provider/model",
            None,
            "Install the Pi CLI",
            "setup",
            False,
            id="missing-pi",
        ),
        pytest.param(
            subprocess.CompletedProcess(["pi"], 1, _SECRET, _SECRET),
            "provider/model",
            "/fake/pi",
            "could not produce a review",
            "failed",
            True,
            id="process-failure",
        ),
        pytest.param(
            subprocess.TimeoutExpired(["pi"], 180, output=_SECRET),
            "provider/model",
            "/fake/pi",
            "timed out",
            "failed",
            True,
            id="timeout",
        ),
        pytest.param(
            subprocess.CompletedProcess(["pi"], 0, "x" * (64 * 1024 + 1), ""),
            "provider/model",
            "/fake/pi",
            "oversized",
            "failed",
            True,
            id="oversized",
        ),
    ],
)
def test_pi_reports_actionable_failure(
    monkeypatch: pytest.MonkeyPatch,
    response: subprocess.CompletedProcess[str] | Exception,
    model: str | None,
    which: str | None,
    message: str,
    kind: str,
    fetched: bool,
) -> None:
    reviewer, comparison, _, _ = _install(
        monkeypatch, response, model=model, which=which
    )

    with pytest.raises(ReviewerError, match=message) as error:
        reviewer.review(_SELECTION)

    assert error.value.kind == kind
    assert comparison.calls == (1 if fetched else 0)
    assert _SECRET not in str(error.value)
