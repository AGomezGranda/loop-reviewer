"""Verify pull request URL parsing and revision invariants."""

import pytest

from loop_reviewer.review.pull_request import PullRequestRef, ReviewedRevision


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        pytest.param(
            "https://github.com/Acme/some_repo.py/pull/123",
            ("github.com", "Acme", "some_repo.py", 123),
            id="public",
        ),
        pytest.param(
            "https://GitHub.Example.com/Acme-Co/repo/pull/2/?x=1#files",
            ("github.example.com", "Acme-Co", "repo", 2),
            id="enterprise-with-suffix",
        ),
    ],
)
def test_parse_pull_request_url(url: str, expected: tuple[str, str, str, int]) -> None:
    assert PullRequestRef.from_url(url) == PullRequestRef(*expected)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("http://github.com/o/r/pull/1", id="http"),
        pytest.param("https:///o/r/pull/1", id="no-host"),
        pytest.param("https://-example.com/o/r/pull/1", id="bad-label"),
        pytest.param("https://bad..example.com/o/r/pull/1", id="empty-label"),
        pytest.param("https://éxample.com/o/r/pull/1", id="non-ascii-host"),
        pytest.param("https://[::1]/o/r/pull/1", id="ipv6"),
        pytest.param("https://[broken/o/r/pull/1", id="malformed-authority"),
        pytest.param("https://github.com:443/o/r/pull/1", id="explicit-port"),
        pytest.param("https://name:token@github.com/o/r/pull/1", id="credentials"),
        pytest.param("https://github.com/o/r", id="repository"),
        pytest.param("https://github.com/o/r/issues/1", id="issue"),
        pytest.param("https://github.com/o/r/pull/1/files", id="extra-path"),
        pytest.param("https://github.com/o/../pull/1", id="dot-repo"),
        pytest.param("https://github.com/bad_owner/r/pull/1", id="bad-owner"),
        pytest.param("https://github.com/o/r%2Fr/pull/1", id="encoded-separator"),
        pytest.param("https://github.com/o/r/pull/1?x= x", id="whitespace"),
        pytest.param("https://git\nhub.com/o/r/pull/1", id="tab-or-newline"),
        pytest.param("https://github.com/o/r/pull/1#\x7f", id="control"),
        pytest.param("https://github.com/o/r/pull/0", id="zero"),
        pytest.param("https://github.com/o/r/pull/-1", id="negative"),
        pytest.param("https://github.com/o/r/pull/١", id="non-ascii-number"),
        pytest.param("https://github.com/o/r/pull/1.0", id="not-decimal"),
    ],
)
def test_rejects_invalid_pull_request_url(url: str) -> None:
    with pytest.raises(ValueError, match="https://HOST/OWNER/REPO/pull/NUMBER"):
        PullRequestRef.from_url(url)


@pytest.mark.parametrize(
    ("host", "owner", "repo", "number"),
    [
        pytest.param("bad..host", "owner", "repo", 1, id="host"),
        pytest.param("github.com", "bad_owner", "repo", 1, id="owner"),
        pytest.param("github.com", "owner", "..", 1, id="repo"),
        pytest.param("github.com", "owner", "repo", 0, id="number"),
    ],
)
def test_validates_pull_request_refs_outside_url_parser(
    host: str, owner: str, repo: str, number: int
) -> None:
    with pytest.raises(ValueError):
        PullRequestRef(host, owner, repo, number)


@pytest.mark.parametrize(
    ("branch", "base_tip", "merge_base", "head"),
    [
        pytest.param("main..old", "a" * 40, "b" * 40, "c" * 40, id="branch"),
        pytest.param("main", "z" * 40, "b" * 40, "c" * 40, id="base-tip"),
        pytest.param("main", "a" * 40, "b" * 39, "c" * 40, id="merge-base"),
        pytest.param("main", "a" * 40, "b" * 40, "HEAD", id="head"),
    ],
)
def test_rejects_invalid_reviewed_revision(
    branch: str, base_tip: str, merge_base: str, head: str
) -> None:
    with pytest.raises(ValueError):
        ReviewedRevision(branch, base_tip, merge_base, head)
