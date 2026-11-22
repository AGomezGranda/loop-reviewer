import json
import os
from pathlib import Path

import pytest

from loop_reviewer.inbox.discovery import RepositoryDiscoveryError
from loop_reviewer.inbox.model import RepositoryAddress, WatchedRepository
from loop_reviewer.integrations.github_discovery import GhRepositoryDiscovery


@pytest.fixture
def fake_github(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[GhRepositoryDiscovery, Path, Path]:
    script = tmp_path / "gh"
    calls, replies = tmp_path / "calls", tmp_path / "replies"
    script.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
if args[:2] == ['auth', 'token']:
    print('pinned')
    raise SystemExit
assert args[args.index('--method')+1] == 'GET', args
assert os.environ['GH_TOKEN'] == 'pinned'
assert not list(pathlib.Path(os.environ['GH_CONFIG_DIR']).iterdir())
index = len(pathlib.Path(os.environ['CALLS']).read_text().splitlines()) if pathlib.Path(os.environ['CALLS']).exists() else 0
with open(os.environ['CALLS'], 'a') as file:
    print(args[-1], file=file)
status, headers, body = json.loads(pathlib.Path(os.environ['REPLIES']).read_text())[index]
print(f'HTTP/2 {status}')
for name, value in headers.items():
    print(f'{name}: {value}')
print()
print(json.dumps(body))
raise SystemExit(status >= 400)
""")
    script.chmod(0o700)
    monkeypatch.chdir(tmp_path)
    environment = dict(
        os.environ,
        PATH=f".{os.pathsep}{os.environ['PATH']}",
        CALLS=str(calls),
        REPLIES=str(replies),
    )
    return GhRepositoryDiscovery(environment), calls, replies


_REPO = {"id": 22, "full_name": "Owner/Canonical"}
_LINK = '<https://api.github.com/repositories/22/pulls?state=open&per_page=100&page=2>; rel="next"'


def test_setup_canonicalizes_and_accepts_empty_listing(
    fake_github: tuple[GhRepositoryDiscovery, Path, Path],
) -> None:
    adapter, calls, replies = fake_github
    replies.write_text(
        json.dumps([(200, {}, {"login": "Alice"}), (200, {}, _REPO), (200, {}, [])])
    )
    repo = adapter.verify(RepositoryAddress("github.com", "owner/old"))
    assert repo == WatchedRepository(
        RepositoryAddress("github.com", "Owner/Canonical"), 22, "alice"
    )
    assert calls.read_text().splitlines() == [
        "user",
        "repos/owner/old",
        "repos/Owner/Canonical/pulls?state=open&per_page=100&page=1",
    ]


def _pr(number: int, repository: int = 22) -> dict[str, object]:
    return {
        "number": number,
        "title": "Fix",
        "state": "open",
        "draft": False,
        "user": {"login": "bob"},
        "requested_reviewers": [{"login": "alice"}],
        "base": {"repo": {"id": repository}},
    }


def test_refresh_paginates_locally_and_rechecks_identity(
    fake_github: tuple[GhRepositoryDiscovery, Path, Path],
) -> None:
    adapter, calls, replies = fake_github
    replies.write_text(
        json.dumps(
            [
                (200, {}, {"login": "Alice"}),
                (200, {}, _REPO),
                (200, {"Link": _LINK}, [_pr(1)]),
                (200, {}, [_pr(2)]),
                (200, {}, _REPO),
            ]
        )
    )
    rows = adapter.list_open(
        WatchedRepository(
            RepositoryAddress("github.com", "Owner/Canonical"), 22, "alice"
        )
    )
    assert [row.ref.number for row in rows] == [1, 2]
    endpoint = "repos/Owner/Canonical"
    assert calls.read_text().splitlines() == [
        "user",
        endpoint,
        f"{endpoint}/pulls?state=open&per_page=100&page=1",
        f"{endpoint}/pulls?state=open&per_page=100&page=2",
        endpoint,
    ]


@pytest.mark.parametrize(
    ("login", "replies_after_user", "fallback"),
    [
        pytest.param("bob", [], False, id="account-switch"),
        pytest.param(
            "alice", [(200, {}, {**_REPO, "id": 33})], False, id="replaced-repo"
        ),
        pytest.param(
            "alice",
            [(200, {}, _REPO), (200, {}, []), (200, {}, {**_REPO, "id": 33})],
            False,
            id="replacement-after-empty-list",
        ),
        pytest.param(
            "alice", [(200, {}, _REPO), (404, {}, {})], False, id="lost-access"
        ),
        pytest.param(
            "alice", [(200, {}, _REPO), (451, {}, {})], False, id="listing-denied"
        ),
        pytest.param(
            "alice", [(200, {}, _REPO), (302, {}, {})], False, id="unresolved-redirect"
        ),
        pytest.param(
            "alice", [(200, {}, _REPO), (500, {}, {})], True, id="temporary-failure"
        ),
        pytest.param(
            "alice",
            [
                (200, {}, _REPO),
                (
                    200,
                    {"Link": _LINK.replace("api.github.com", "invalid.example")},
                    [_pr(1)],
                ),
            ],
            True,
            id="bad-next-link",
        ),
        pytest.param(
            "alice",
            [(200, {}, _REPO), (200, {}, [_pr(1, 33)])],
            False,
            id="wrong-base-repo",
        ),
        pytest.param(
            "alice",
            [(200, {}, _REPO), (200, {}, [{}])],
            False,
            id="missing-row-identity",
        ),
        pytest.param(
            "alice",
            [(200, {}, _REPO), (200, {}, [_pr(i) for i in range(1, 102)])],
            True,
            id="oversized-page",
        ),
        pytest.param(
            "alice",
            [(200, {}, _REPO), (200, {"Link": _LINK}, [_pr(1)]), (200, {}, [_pr(1)])],
            True,
            id="duplicate-across-pages",
        ),
    ],
)
def test_refresh_failure_visibility(
    fake_github: tuple[GhRepositoryDiscovery, Path, Path],
    login: str,
    replies_after_user: list[object],
    fallback: bool,
) -> None:
    adapter, _, replies = fake_github
    replies.write_text(json.dumps([(200, {}, {"login": login}), *replies_after_user]))
    with pytest.raises(RepositoryDiscoveryError) as error:
        adapter.list_open(
            WatchedRepository(
                RepositoryAddress("github.com", "Owner/Canonical"), 22, "alice"
            )
        )
    assert error.value.may_show_cached_rows is fallback


@pytest.mark.parametrize("has_next", [False, True], ids=["complete", "incomplete"])
def test_refresh_at_the_page_limit(
    fake_github: tuple[GhRepositoryDiscovery, Path, Path],
    has_next: bool,
) -> None:
    adapter, calls, replies = fake_github
    pages: list[tuple[int, dict[str, str], object]] = [
        (200, {"Link": _LINK.replace("page=2>", f"page={page + 1}>")}, [_pr(page)])
        for page in range(1, 21)
    ]
    if not has_next:
        terminal_link: str = _LINK.replace(
            'page=2>; rel="next"', 'page=19>; rel="prev"'
        )
        headers: dict[str, str] = {"Link": terminal_link}
        pages[-1] = (
            200,
            headers,
            [_pr(number) for number in range(20, 120)],
        )
        pages.append((200, {}, _REPO))
    replies.write_text(
        json.dumps([(200, {}, {"login": "Alice"}), (200, {}, _REPO), *pages])
    )
    repository = WatchedRepository(
        RepositoryAddress("github.com", "Owner/Canonical"), 22, "alice"
    )
    if has_next:
        with pytest.raises(RepositoryDiscoveryError, match="20-page scan limit"):
            adapter.list_open(repository)
    else:
        assert len(adapter.list_open(repository)) == 119
    endpoints = calls.read_text().splitlines()
    assert endpoints[21].endswith("&page=20")
    assert len(endpoints) == (22 if has_next else 23)
