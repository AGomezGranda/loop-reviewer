"""Exercise bound credentials and GitHub's right-side diff anchors."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from loop_reviewer.integrations.github_publication import open_gh_publication
from loop_reviewer.review.model import (
    Finding,
    FindingLocation,
    PublicationReceipt,
    ReviewDraft,
    ReviewSelection,
    SavedDraft,
)
from loop_reviewer.review.publication_model import (
    PublicationId,
    PublicationPreview,
    build_preview,
)
from loop_reviewer.review.pull_request import PullRequestRef, ReviewedRevision
from loop_reviewer.review.selection import GitHubReadError


def response(data: object, *, headers: str = "") -> bytes:
    return f"HTTP/2.0 200 OK\r\n{headers}\r\n{json.dumps(data)}".encode()


@pytest.mark.parametrize(
    ("host", "key"),
    [
        ("github.com", "GH_TOKEN"),
        ("org.ghe.com", "GH_TOKEN"),
        ("github.example.com", "GH_ENTERPRISE_TOKEN"),
    ],
    ids=["public", "enterprise-cloud", "enterprise-server"],
)
def test_session_pins_a_single_host_credential(host: str, key: str) -> None:
    environments: list[dict[str, str]] = []

    def run(
        argv: list[str], *, env: dict[str, str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        environments.append(env)
        assert "captured-token" not in argv
        if argv[1] == "auth":
            assert argv == ["gh", "auth", "token", "--hostname", host]
            assert env["GH_CONFIG_DIR"] == "/mutable"
            return subprocess.CompletedProcess(argv, 0, b"captured-token\n")
        assert argv[-1] == "user"
        assert not list(Path(env["GH_CONFIG_DIR"]).iterdir())
        return subprocess.CompletedProcess(
            argv, 0, response({"id": 123, "login": "reviewer"})
        )

    with (
        patch(
            "loop_reviewer.integrations.github_credentials.capture_review_process",
            side_effect=run,
        ),
        patch(
            "loop_reviewer.integrations.gh_api.capture_review_process", side_effect=run
        ),
        open_gh_publication(
            {
                "GH_CONFIG_DIR": "/mutable",
                "GH_HOST": host,
                "GH_TOKEN": "old",
                "GH_ENTERPRISE_TOKEN": "other",
                "GH_DEBUG": "api",
            },
            host,
        ) as session,
    ):
        assert (session.login, session.actor_id) == ("reviewer", 123)
    assert {
        name: value for name, value in environments[1].items() if "TOKEN" in name
    } == {key: "captured-token"}
    assert environments[1]["GH_HOST"] == host
    assert "GH_DEBUG" not in environments[1]
    assert not Path(environments[1]["GH_CONFIG_DIR"]).exists()


@pytest.mark.parametrize(
    "token",
    [b"", b"secret-token\nsecond\n", b"secret token\n"],
    ids=["missing", "multiple", "whitespace"],
)
def test_session_rejects_invalid_token_without_exposing_it(token: bytes) -> None:
    with (
        patch(
            "loop_reviewer.integrations.github_credentials.capture_review_process",
            return_value=subprocess.CompletedProcess([], 0, token),
        ),
        pytest.raises(GitHubReadError, match="credential") as error,
        open_gh_publication({}, "github.com"),
    ):
        pytest.fail("invalid credential was accepted")
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    ("failure", "guidance"),
    [
        pytest.param(FileNotFoundError(), "Install the GitHub CLI", id="missing"),
        pytest.param(subprocess.TimeoutExpired(["gh"], 15), "timed out", id="timeout"),
    ],
)
def test_session_reports_credential_invocation_failure(
    failure: BaseException, guidance: str
) -> None:
    with (
        patch(
            "loop_reviewer.integrations.github_credentials.capture_review_process",
            side_effect=failure,
        ),
        pytest.raises(GitHubReadError, match=guidance),
        open_gh_publication({}, "github.com"),
    ):
        pytest.fail("credential invocation failure was ignored")


def test_anchors_use_verified_right_side_additions() -> None:
    ref = PullRequestRef("github.com", "Acme", "Repo", 7, 1)
    first = [
        {
            "filename": "new.py",
            "previous_filename": "old.py",
            "patch": "@@ -1,2 +1,2 @@\n context\n-removed\n+added\n@@ -9 +9,2 @@\n unchanged\n+later",
        },
        {"filename": "truncated.py", "patch": "@@ -1,2 +1,2 @@\n+not proven"},
        {"filename": "binary.png"},
    ]
    pages = [
        response({"id": 1, "login": "reviewer"}),
        response(
            first,
            headers='Link: <https://api.github.com/repos/Acme/Repo/pulls/7/files?page=2>; rel="next"\r\n',
        ),
        response([{"filename": "other.py", "patch": "@@ -0,0 +1 @@\n+other"}]),
    ]
    matches = frozenset(
        {
            FindingLocation("new.py", 2),
            FindingLocation("new.py", 10),
            FindingLocation("other.py", 1),
        }
    )
    requested = matches | {
        FindingLocation("new.py", 1),
        FindingLocation("old.py", 2),
        FindingLocation("truncated.py", 1),
        FindingLocation("binary.png", 1),
    }
    with (
        patch(
            "loop_reviewer.integrations.github_credentials.capture_review_process",
            return_value=subprocess.CompletedProcess([], 0, b"captured\n"),
        ),
        patch(
            "loop_reviewer.integrations.gh_api.capture_review_process",
            side_effect=[subprocess.CompletedProcess([], 0, body) for body in pages],
        ) as run,
        open_gh_publication({}, ref.host) as session,
    ):
        assert session.usable_anchors(ref, requested) == matches
    assert [call.args[0][-1] for call in run.call_args_list] == [
        "user",
        "repos/Acme/Repo/pulls/7/files?per_page=100&page=1",
        "repos/Acme/Repo/pulls/7/files?per_page=100&page=2",
    ]


_REVIEWS = "repos/Acme/Repo/pulls/7/reviews"
_USER = subprocess.CompletedProcess([], 0, response({"id": 1, "login": "reviewer"}))
_TOKEN = subprocess.CompletedProcess([], 0, b"captured\n")


def _preview() -> PublicationPreview:
    selection = ReviewSelection(
        PullRequestRef("github.com", "Acme", "Repo", 7, 1),
        "reviewer",
        "author",
        ReviewedRevision("main", "a" * 40, "b" * 40, "c" * 40),
    )
    location = FindingLocation("file.py", 2)
    draft = ReviewDraft(
        "comment", "Résumé", (Finding("high", "condition", "conséquence", location),)
    )
    return build_preview(
        PublicationId("75f718d3-9633-42ad-9097-22f320d068a8"),
        SavedDraft(1, 1, selection, draft),
        selection,
        1,
        "approve",
        frozenset({location}),
    )


def _review(preview: PublicationPreview, **changes: object) -> dict[str, object]:
    return {
        "id": 45,
        "user": {"id": 1, "login": "reviewer"},
        "body": preview.body,
        "commit_id": preview.selection.revision.head,
        "state": "APPROVED",
        **changes,
    }


def test_creates_one_pinned_review_through_gh() -> None:
    preview = _preview()
    with (
        patch(
            "loop_reviewer.integrations.github_credentials.capture_review_process",
            return_value=_TOKEN,
        ),
        patch(
            "loop_reviewer.integrations.gh_api.capture_review_process",
            side_effect=[
                _USER,
                subprocess.CompletedProcess([], 0, response(_review(preview))),
            ],
        ) as run,
        open_gh_publication({}, "github.com", inherited_fd=17) as session,
    ):
        assert session.submit(preview) == PublicationReceipt(45, "approve")
    assert run.call_count == 2
    args, kwargs = run.call_args
    assert args[0][:7] == [
        "gh",
        "api",
        "--hostname",
        "github.com",
        "--method",
        "POST",
        "--include",
    ]
    assert args[0][-3:] == ["--input", "-", _REVIEWS]
    assert all(secret not in args[0] for secret in (preview.body, "captured"))
    (inline,) = preview.comments
    assert json.loads(kwargs["input"]) == {
        "commit_id": preview.selection.revision.head,
        "event": "APPROVE",
        "body": preview.body,
        "comments": [
            {
                "path": inline.path,
                "line": inline.line,
                "side": "RIGHT",
                "body": inline.body,
            }
        ],
    }
    assert "Résumé".encode() in kwargs["input"]
    assert kwargs["pass_fds"] == (17,)
    assert kwargs["timeout"] == 15 and kwargs["output_limit"] == 1024**2
    assert kwargs["env"]["GH_TOKEN"] == "captured"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "authentication"),
        (403, "access"),
        (404, "access"),
        (422, "validation"),
        (429, "rate_limit"),
        (500, "unknown"),
        (4221, "unknown"),
        (200, "unknown"),
        (0, "unknown"),
        (-1, "interrupt"),
    ],
    ids=[
        "auth",
        "forbidden",
        "missing",
        "invalid",
        "rate-limit",
        "server",
        "malformed-status",
        "mismatching-success",
        "timeout",
        "interruption",
    ],
)
def test_classifies_create_response_conservatively(status: int, expected: str) -> None:
    preview = _preview()
    result: subprocess.CompletedProcess[bytes] | BaseException
    if status == 0:
        result = subprocess.TimeoutExpired(["gh"], 15)
    elif status == -1:
        result = KeyboardInterrupt()
    elif status == 200:
        result = subprocess.CompletedProcess(
            [], 0, response(_review(preview, state="COMMENTED"))
        )
    else:
        result = subprocess.CompletedProcess(
            [], 1, f'HTTP/2.0 {status} Error\r\n\r\n{{"message":"secret"}}'.encode()
        )
    with (
        patch(
            "loop_reviewer.integrations.github_credentials.capture_review_process",
            return_value=_TOKEN,
        ),
        patch(
            "loop_reviewer.integrations.gh_api.capture_review_process",
            side_effect=[_USER, result],
        ),
        open_gh_publication({}, "github.com", inherited_fd=17) as session,
    ):
        if expected == "interrupt":
            with pytest.raises(KeyboardInterrupt):
                session.submit(preview)
        else:
            assert session.submit(preview) == expected


@pytest.mark.parametrize(
    "mode",
    [
        "later-page",
        "dismissed",
        "quoted-marker",
        "conflict",
        "duplicate",
        "incomplete",
        "bad-link",
        "no-match",
    ],
)
def test_reconciliation_requires_complete_unambiguous_evidence(mode: str) -> None:
    preview = _preview()
    review = _review(preview)
    first = [review] if mode in {"duplicate", "incomplete"} else []
    evidence: dict[str, list[dict[str, object]]] = {
        "later-page": [review],
        "dismissed": [_review(preview, state="DISMISSED")],
        "quoted-marker": [
            review,
            _review(preview, id=47, body=f"Example marker: {preview.id.marker}"),
        ],
        "conflict": [_review(preview, commit_id="d" * 40)],
        "duplicate": [review],
        "no-match": [],
    }
    second = evidence.get(mode)
    link = (
        f'Link: <https://api.github.com/{_REVIEWS}?per_page=100&page=2>; rel="next"\r\n'
    )
    pages = [
        response(first, headers="Link: malformed\r\n" if mode == "bad-link" else link),
        b"truncated" if second is None else response(second),
    ]
    with (
        patch(
            "loop_reviewer.integrations.github_credentials.capture_review_process",
            return_value=_TOKEN,
        ),
        patch(
            "loop_reviewer.integrations.gh_api.capture_review_process",
            side_effect=[
                _USER,
                *(subprocess.CompletedProcess([], 0, data) for data in pages),
            ],
        ) as run,
        open_gh_publication({}, "github.com") as session,
    ):
        result = session.reconcile(preview, ref=preview.selection.ref)
    assert result == (
        PublicationReceipt(45, "approve")
        if mode in {"later-page", "dismissed", "quoted-marker"}
        else "no_match"
        if mode == "no-match"
        else "inconclusive"
    )
    assert all(call.args[0][5] == "GET" for call in run.call_args_list)
