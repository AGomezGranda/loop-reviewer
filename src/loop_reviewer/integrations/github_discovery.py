"""Discover open PR metadata with a bounded, host-bound GitHub credential."""

import re
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from datetime import datetime
from typing import cast

from loop_reviewer.inbox.discovery import RepositoryDiscoveryError
from loop_reviewer.inbox.model import (
    DiscoveredPullRequest,
    RepositoryAddress,
    WatchedRepository,
)
from loop_reviewer.integrations.gh_api import GhApi
from loop_reviewer.integrations.github_credentials import open_gh_environment
from loop_reviewer.integrations.github_pagination import has_next_page
from loop_reviewer.integrations.github_reads import GhReadFailure, read_json
from loop_reviewer.review.pull_request import PullRequestRef
from loop_reviewer.review.selection import GitHubReadError

_MAX_PAGES = 20
_PAGE_SIZE = 100
_INVALID = "GitHub returned invalid or incomplete pull request data."
_ACCESS = "Could not list pull requests. Check repository access or run inbox setup with the current URL."
_IDENTITY = "Could not verify the configured repository identity. Run inbox setup with the current URL."


class GhRepositoryDiscovery:
    def __init__(self, environment: Mapping[str, str]) -> None:
        self._environment = dict(environment)

    @contextmanager
    def _session(self, host: str) -> Generator[GhApi]:
        if host == "localhost" or host.endswith((".localhost", ".github.com")):
            raise RepositoryDiscoveryError(
                "Use the canonical GitHub host for inbox setup."
            )
        try:
            with open_gh_environment(
                self._environment, host, prefix="loop-reviewer-inbox-gh-"
            ) as (pinned, directory):
                yield GhApi(pinned, private_cwd=directory)
        except GitHubReadError as error:
            raise RepositoryDiscoveryError(str(error)) from None

    def verify(self, address: RepositoryAddress) -> WatchedRepository:
        with self._session(address.host) as api:
            account = _account(api, address.host)
            repository = _repository(api, address, account)
            _page(api, repository, 1)
            return repository

    def list_open(
        self, repository: WatchedRepository
    ) -> tuple[DiscoveredPullRequest, ...]:
        with self._session(repository.address.host) as api:
            account = _account(api, repository.address.host)
            if account.lower() != repository.account:
                raise RepositoryDiscoveryError(
                    "The GitHub account changed. Sign back in or run inbox setup for this account."
                )
            canonical = _repository(api, repository.address, account)
            if canonical.key != repository.key:
                raise RepositoryDiscoveryError(_IDENTITY)
            seen: dict[int, DiscoveredPullRequest] = {}
            for page in range(1, _MAX_PAGES + 1):
                rows, more = _page(api, canonical, page)
                for row in rows:
                    if row.ref.number in seen:
                        raise RepositoryDiscoveryError(
                            _INVALID, may_show_cached_rows=True
                        )
                    seen[row.ref.number] = row
                if not more:
                    break
            else:
                raise RepositoryDiscoveryError(
                    "Pull request listing exceeded the 20-page scan limit.",
                    may_show_cached_rows=True,
                )
            checked = _repository(api, canonical.address, account)
            if checked != canonical:
                raise RepositoryDiscoveryError(_IDENTITY)
            return tuple(seen.values())


def _account(api: GhApi, host: str) -> str:
    try:
        response, value = read_json(api, host, "user")
        if response.status != 200:
            raise ValueError
        return _login(_object(value).get("login"))
    except GhReadFailure as error:
        raise RepositoryDiscoveryError(str(error)) from None
    except TypeError, ValueError:
        raise RepositoryDiscoveryError(
            "Could not verify the authenticated GitHub account."
        ) from None


def _repository(
    api: GhApi, address: RepositoryAddress, account: str
) -> WatchedRepository:
    try:
        response, value = read_json(
            api, address.host, f"repos/{address.slug}", unavailable=_ACCESS
        )
        data = _object(value)
        repository_id, slug = data.get("id"), data.get("full_name")
        if (
            response.status != 200
            or type(repository_id) is not int
            or not isinstance(slug, str)
        ):
            raise ValueError
        return WatchedRepository(
            RepositoryAddress(address.host, slug), repository_id, account
        )
    except GhReadFailure as error:
        raise RepositoryDiscoveryError(str(error)) from None
    except TypeError, ValueError:
        raise RepositoryDiscoveryError(_IDENTITY) from None


def _page(
    api: GhApi, repo: WatchedRepository, page: int
) -> tuple[tuple[DiscoveredPullRequest, ...], bool]:
    host, slug = repo.address.host, repo.address.slug
    resource = f"repos/{slug}/pulls"
    try:
        response, data = read_json(
            api,
            host,
            f"{resource}?state=open&per_page={_PAGE_SIZE}&page={page}",
            unavailable=_ACCESS,
        )
    except GhReadFailure as error:
        raise RepositoryDiscoveryError(
            str(error),
            may_show_cached_rows=error.kind
            in {"rate_limit", "transport", "invalid_response"},
        ) from None
    if response.status != 200 or not isinstance(data, list) or len(data) > _PAGE_SIZE:
        raise RepositoryDiscoveryError(_INVALID, may_show_cached_rows=True)
    try:
        rows = tuple(_pull_request(repo, value) for value in cast("list[object]", data))
        if len({row.ref.number for row in rows}) != len(rows):
            raise ValueError
        more = has_next_page(
            response,
            host,
            frozenset({f"/{resource}", f"/repositories/{repo.repository_id}/pulls"}),
            page,
            len(rows),
            invalid_message=_INVALID,
        )
    except GitHubReadError, TypeError, ValueError:
        raise RepositoryDiscoveryError(_INVALID, may_show_cached_rows=True) from None
    return rows, more


def _pull_request(repo: WatchedRepository, value: object) -> DiscoveredPullRequest:
    try:
        data = _object(value)
        base = _object(_object(data.get("base")).get("repo")).get("id")
    except TypeError:
        raise RepositoryDiscoveryError(_IDENTITY) from None
    if type(base) is not int or base != repo.repository_id:
        raise RepositoryDiscoveryError(_IDENTITY)
    number, title, state, draft = (
        data.get(name) for name in ("number", "title", "state", "draft")
    )
    created = data.get("created_at")
    reviewers = data.get("requested_reviewers")
    if (
        type(number) is not int
        or not 0 < number < 2**63
        or not isinstance(created, str)
        or not isinstance(title, str)
        or not isinstance(state, str)
        or state not in {"open", "closed"}
        or type(draft) is not bool
        or not isinstance(reviewers, list)
    ):
        raise ValueError
    opened = datetime.fromisoformat(created)
    if opened.tzinfo is None:
        raise ValueError
    ref = PullRequestRef.from_repository(
        repo.address.host, repo.address.slug, number, repository_id=base
    )
    return DiscoveredPullRequest(
        ref,
        title,
        _login(_object(data.get("user")).get("login")),
        state,
        draft,
        tuple(
            _login(_object(user).get("login"))
            for user in cast("list[object]", reviewers)
        ),
        int(opened.timestamp()),
    )


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError
    return cast("dict[str, object]", value)


def _login(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]+(?:\[bot\])?", value
    ):
        raise ValueError
    return value
