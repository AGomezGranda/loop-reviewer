"""Exercise pull request and revision reads through the GitHub integration."""

import json
import subprocess
from collections.abc import Mapping, Sequence
from unittest.mock import patch

import pytest

from loop_reviewer.integrations.github import GhPullRequestReader
from loop_reviewer.review.pull_request import PullRequestRef, ReviewedRevision
from loop_reviewer.review.selection import (
    GitHubReadError,
    open_pull_request,
)

_HOST = "git.example.com"
_REF = PullRequestRef(_HOST, "acme", "repo", 7)
_BRANCH = "release/next+#%"
_BASE_TIP, _MERGE_BASE, _HEAD, _STALE_TIP = (digit * 40 for digit in "abcd")
_ENDPOINTS = [
    "user",
    "repos/acme/repo/pulls/7",
    "repos/Acme/Repo/git/ref/heads/release/next%2B%23%25",
    f"repos/Acme/Repo/compare/{_BASE_TIP}...{_HEAD}?per_page=1",
]


def _pr(
    *,
    fork: bool = False,
    number: object = 7,
    head: str = _HEAD,
    repository_id: object = 123,
) -> dict[str, object]:
    return {
        "number": number,
        "user": {"login": "teammate"},
        "state": "open",
        "draft": False,
        "base": {
            "ref": _BRANCH,
            "sha": _STALE_TIP,
            "repo": {"full_name": "Acme/Repo", "id": repository_id},
        },
        "head": {
            "sha": head,
            "repo": {"full_name": "Fork/Repo" if fork else "Acme/Repo"},
        },
    }


def _response(
    body: object = None, *, status: int = 200, headers: str = "X-Request-Id: test\r\n"
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        ["gh"],
        0 if status == 200 else 1,
        f"HTTP/2.0 {status} Test\r\n{headers}\r\n{json.dumps(body)}",
        "raw stderr should never be shown",
    )


def _branch_reply(branch: str = _BRANCH) -> subprocess.CompletedProcess[str]:
    return _response({"ref": f"refs/heads/{branch}", "object": {"sha": _BASE_TIP}})


def _login() -> subprocess.CompletedProcess[str]:
    return _response({"login": "reviewer"})


@pytest.mark.parametrize("fork", [False, True], ids=["same-repo", "fork"])
def test_reads_revision_by_commit(fork: bool) -> None:
    comparison = _response(
        {"base_commit": {"sha": _BASE_TIP}, "merge_base_commit": {"sha": _MERGE_BASE}}
    )
    with patch(
        "subprocess.run",
        side_effect=[_login(), _response(_pr(fork=fork)), _branch_reply(), comparison],
    ) as run:
        selection = open_pull_request(_REF, GhPullRequestReader({}))

    assert selection.ref == PullRequestRef(_HOST, "Acme", "Repo", 7, repository_id=123)
    assert selection.account == "reviewer"
    assert selection.author == "teammate"
    assert selection.revision == ReviewedRevision(
        _BRANCH, _BASE_TIP, _MERGE_BASE, _HEAD
    )
    assert [call.args[0] for call in run.call_args_list] == [
        ["gh", "api", "--hostname", _HOST, "--method", "GET", "--include", endpoint]
        for endpoint in _ENDPOINTS
    ]
    for call in run.call_args_list:
        assert call.kwargs["env"]["GH_PROMPT_DISABLED"] == "1"
        assert call.kwargs["timeout"] == 15
        assert call.kwargs["stdout"] == subprocess.PIPE
        assert call.kwargs["stderr"] == subprocess.DEVNULL


@pytest.mark.parametrize(
    "repository_id", [None, True, 0, "123"], ids=["null", "boolean", "zero", "string"]
)
def test_requires_a_stable_base_repository_id(repository_id: object) -> None:
    with (
        patch(
            "subprocess.run",
            side_effect=[_login(), _response(_pr(repository_id=repository_id))],
        ),
        pytest.raises(GitHubReadError, match="invalid or unexpected response"),
    ):
        GhPullRequestReader({}).read_pull_request(_REF)


_SECRET = "raw response should never be shown"


@pytest.mark.parametrize(
    ("responses", "message"),
    [
        pytest.param(
            [FileNotFoundError(_SECRET)], "Install the GitHub CLI", id="missing-gh"
        ),
        pytest.param([PermissionError(_SECRET)], "Could not run", id="cannot-execute"),
        pytest.param(
            [subprocess.CompletedProcess(["gh"], 4, "", _SECRET)],
            f"gh auth login --hostname {_HOST}",
            id="gh-exit-4",
        ),
        pytest.param(
            [_response(_SECRET, status=401)], "gh auth login", id="http-unauthorized"
        ),
        pytest.param(
            [_login(), _response(_SECRET, status=404)],
            "not found or inaccessible",
            id="pr-missing",
        ),
        pytest.param(
            [_login(), _response(_SECRET, status=403)],
            "not found or inaccessible",
            id="pr-forbidden",
        ),
        pytest.param(
            [
                _login(),
                _response(_SECRET, status=403, headers="x-RaTeLiMiT-Remaining: 0\r\n"),
            ],
            "rate limit",
            id="primary-rate-limit",
        ),
        pytest.param(
            [_login(), _response(_SECRET, status=403, headers="Retry-After: 30\r\n")],
            "rate limit",
            id="secondary-rate-limit-with-retry-after",
        ),
        pytest.param(
            [
                _login(),
                _response(
                    {"message": f"You have exceeded a secondary rate limit. {_SECRET}"},
                    status=403,
                ),
            ],
            "rate limit",
            id="secondary-rate-limit-without-retry-after",
        ),
        pytest.param(
            [_response(_SECRET, status=429)], "rate limit", id="http-429-crlf"
        ),
        pytest.param(
            [
                subprocess.CompletedProcess(
                    ["gh"], 0, f"HTTP/2.0 999 Test\r\n\r\n{_SECRET}", _SECRET
                )
            ],
            "invalid or unexpected",
            id="malformed-http-status",
        ),
        pytest.param(
            [subprocess.TimeoutExpired(["gh"], 15, output=_SECRET)],
            "timed out",
            id="timeout",
        ),
        pytest.param([_response(_SECRET, status=503)], "try again", id="upstream"),
        pytest.param(
            [_login(), _response(_pr()), _response(_SECRET, status=404)],
            "Contents read access",
            id="missing-branch",
        ),
        pytest.param(
            [
                _login(),
                _response(_pr()),
                _branch_reply(),
                _response(_SECRET, status=422),
            ],
            "Could not compare",
            id="unresolved-comparison",
        ),
        pytest.param(
            [_response(_SECRET)], "invalid or unexpected", id="malformed-payload"
        ),
        pytest.param(
            [
                subprocess.CompletedProcess(
                    ["gh"], 0, "HTTP/2.0 200 OK\n\n{" + _SECRET, ""
                )
            ],
            "invalid or unexpected",
            id="malformed-json",
        ),
        pytest.param(
            [_login(), _response(_pr(number=8))],
            "invalid or unexpected",
            id="wrong-pr-number",
        ),
        pytest.param(
            [_login(), _response(_pr(head="abc"))],
            "invalid or unexpected",
            id="invalid-head",
        ),
        pytest.param(
            [_login(), _response(_pr()), _branch_reply("other")],
            "invalid or unexpected",
            id="wrong-ref",
        ),
        pytest.param(
            [
                _login(),
                _response(_pr()),
                _branch_reply(),
                _response(
                    {
                        "base_commit": {"sha": _STALE_TIP},
                        "merge_base_commit": {"sha": _MERGE_BASE},
                    }
                ),
            ],
            "invalid or unexpected",
            id="wrong-compare-base",
        ),
    ],
)
def test_reports_github_read_failures(
    responses: Sequence[subprocess.CompletedProcess[str] | Exception], message: str
) -> None:
    with (
        patch("subprocess.run", side_effect=responses) as run,
        pytest.raises(GitHubReadError, match=message) as error,
    ):
        open_pull_request(_REF, GhPullRequestReader({}))

    assert _SECRET not in str(error.value)
    assert "raw stderr" not in str(error.value)
    assert run.call_count == len(responses)


_TOKENS = {
    "GH_TOKEN": "gh-token",
    "GITHUB_TOKEN": "github-token",
    "GH_ENTERPRISE_TOKEN": "enterprise-token",
    "GITHUB_ENTERPRISE_TOKEN": "github-enterprise-token",
}


@pytest.mark.parametrize(
    ("host", "gh_host", "forwarded"),
    [
        pytest.param(_HOST, "Git.Example.com", _TOKENS, id="matching-enterprise"),
        pytest.param(_HOST, "other.example.com", {}, id="different-enterprise-host"),
        pytest.param(
            "org.ghe.com", None, {}, id="cloud-enterprise-needs-explicit-host"
        ),
        pytest.param(
            "github.com",
            _HOST,
            {key: _TOKENS[key] for key in ("GH_TOKEN", "GITHUB_TOKEN")},
            id="github-com",
        ),
    ],
)
def test_scopes_environment_credentials(
    host: str, gh_host: str | None, forwarded: Mapping[str, str]
) -> None:
    environment = {**_TOKENS, "GH_CONFIG_DIR": "/example", "GH_FORCE_TTY": "1"}
    if gh_host is not None:
        environment["GH_HOST"] = gh_host
    original = environment.copy()

    with patch("subprocess.run", side_effect=[_login(), _response(_pr())]) as run:
        GhPullRequestReader(environment).read_pull_request(
            PullRequestRef(host, "acme", "repo", 7)
        )

    for call in run.call_args_list:
        child = call.kwargs["env"]
        assert {key: child[key] for key in _TOKENS if key in child} == forwarded
        assert child["GH_CONFIG_DIR"] == "/example"
        assert child["GH_PROMPT_DISABLED"] == "1"
        assert "GH_FORCE_TTY" not in child
    assert environment == original
