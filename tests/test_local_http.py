import asyncio
import socket
import sqlite3
import sys
from collections.abc import AsyncGenerator
from contextlib import closing, nullcontext
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
from tomllib import loads
from typing import Literal, override

import httpx
import pytest
from fastapi import FastAPI

from loop_reviewer import cli, composition
from loop_reviewer.composition import Workflows
from loop_reviewer.inbox.discovery import RepositoryDiscoveryError
from loop_reviewer.inbox.model import (
    DiscoveredPullRequest,
    JiraProjectKey,
    RepositoryAddress,
    WatchedRepository,
)
from loop_reviewer.inbox.polling import ScanCycle
from loop_reviewer.integrations.local_state.inbox_watch import InboxWatch
from loop_reviewer.integrations.local_state.review_lock import LockedReviewExecution
from loop_reviewer.integrations.local_state.sqlite_inbox import SqliteInboxStore
from loop_reviewer.integrations.local_state.sqlite_reviews import SqliteReviewRunStore
from loop_reviewer.local_http.app import create_app
from loop_reviewer.local_http.serve import BrowserServer
from loop_reviewer.local_http.watch import BrowserWatch
from loop_reviewer.review.history import ReviewHistory
from loop_reviewer.review.hosted_report import project_report
from loop_reviewer.review.model import (
    GeneratedReview,
    PublicationReceipt,
    ReviewDraft,
    ReviewSelection,
)
from loop_reviewer.review.publication_model import PublicationPreview
from loop_reviewer.review.pull_request import (
    PullRequest,
    PullRequestRef,
    ReviewedRevision,
)
from loop_reviewer.review.selection import GitHubReadError
from loop_reviewer.review.store import ReviewStoreError

from .support.browser_review import (
    FakePublication,
    FakeReader,
    fake_gh_environment,
)
from .support.hosted_report import offline_pinned_report, offline_report

_ORIGIN = "http://127.0.0.1:43210"
_HEADERS = {"Authorization": "Bearer test-capability", "Origin": _ORIGIN}
_REVIEW_URL = "https://github.com/org/repo/pull/1"
_SAVED_SELECTION = ReviewSelection(
    PullRequestRef("github.com", "org", "repo", 1, 42),
    "reviewer",
    "teammate",
    ReviewedRevision("main", "a" * 40, "b" * 40, "c" * 40),
)
_GENERATED = GeneratedReview(ReviewDraft("comment", "Ready", ()), "fixture/model", None)


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _app(
    workflows: Workflows, watcher: InboxWatch, *, origin: str = _ORIGIN
) -> FastAPI:
    return create_app(
        origin=origin,
        capability="test-capability",
        read_inbox=workflows.read_inbox,
        read_cached_inbox=workflows.read_cached_inbox,
        scan_inbox=watcher.scan_now,
        configure_inbox=workflows.configure_inbox(),
        inbox_actions=workflows.inbox_actions(),
        review_pull_request=workflows.review,
        drafts=workflows.edit_review_draft(),
        review_history=workflows.review_history(),
        review_stats=workflows.review_stats(),
        active_reviews=workflows.active_reviews(),
        prepare=workflows.prepare_publication(),
        publish=workflows.publish_review(),
        join_watch=watcher.join,
        watch_tick=watcher.tick,
        watch_cycle=watcher.cycle,
    )


@pytest.fixture
async def client(tmp_path: Path) -> AsyncGenerator[httpx.AsyncClient]:
    workflows = Workflows(
        tmp_path / "loop-reviewer", {}, config_path=tmp_path / "config.toml"
    )
    watcher = workflows.inbox_watch(monotonic=lambda: 0)
    app = _app(workflows, watcher)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url=_ORIGIN) as session,
    ):
        yield session


async def test_stats_reads_only_local_aggregates(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    store = SqliteReviewRunStore(tmp_path / "loop-reviewer" / "reviews.sqlite3")
    for account in ("reviewer", "other"):
        run = store.start(
            replace(_SAVED_SELECTION, account=account), replaces=None
        ).finish(_GENERATED)
        store.save(run)
        store.complete(run.complete_locally())
    store.start(
        replace(_SAVED_SELECTION, ref=replace(_SAVED_SELECTION.ref, number=2)),
        replaces=None,
    )
    with sqlite3.connect(tmp_path / "loop-reviewer" / "reviews.sqlite3") as connection:
        connection.execute(
            "UPDATE review_completions SET recorded_at = NULL WHERE run_id = 1"
        )
        connection.execute(
            "UPDATE review_completions SET final_draft = 'not decoded by stats'"
        )

    url = "/api/v1/stats"
    assert (await client.get(url)).status_code == 401
    response = await client.get(url, headers=_HEADERS)
    assert response.status_code == 200
    data = response.json()
    counts = {"completed": 1, "published": 0, "completedLocally": 1}
    option = {
        "key": "github.com/42",
        "host": "github.com",
        "slug": "org/repo",
        "legacyNameOnly": False,
    }
    assert data == {
        "totals": counts,
        "completedLast7Days": 1,
        "undatedCompletions": 1,
        "repositoryOptions": [option],
        "repositories": [{"repository": option, "counts": counts}],
        "weeks": data["weeks"],
    }
    assert sum(week["counts"]["completed"] for week in data["weeks"]) == 1
    assert all(set(week) == {"start", "end", "counts"} for week in data["weeks"])
    all_time = await client.get(
        url, headers=_HEADERS, params={"range": "all", "repository": option["key"]}
    )
    assert all_time.status_code == 200
    assert all_time.json()["totals"] == {
        "completed": 2,
        "published": 0,
        "completedLocally": 2,
    }
    assert (
        await client.get(url, headers=_HEADERS, params={"range": "7d"})
    ).status_code == 422
    assert (
        await client.get(url, headers=_HEADERS, params={"repository": ""})
    ).status_code == 422
    assert (
        await client.get(url, headers=_HEADERS, params={"repository": "github.com/99"})
    ).status_code == 404


async def test_saved_edit_can_be_returned_during_publication_history_failure(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SqliteReviewRunStore(tmp_path / "loop-reviewer" / "reviews.sqlite3")
    selection = ReviewSelection(
        PullRequestRef("github.com", "org", "repo", 1, 42),
        "reviewer",
        "teammate",
        ReviewedRevision("main", "a" * 40, "b" * 40, "c" * 40),
    )
    saved = store.start(selection, replaces=None).finish(
        GeneratedReview(ReviewDraft("approved", "Original", ()), "provider/model", None)
    )
    store.save(saved)

    def unavailable(_history: ReviewHistory, _run_id: int) -> None:
        raise ReviewStoreError("Publication history unavailable.")

    url = f"/api/v1/drafts/{saved.id}"
    with monkeypatch.context() as patch:
        patch.setattr(ReviewHistory, "unconfirmed_for", unavailable)
        response = await client.patch(
            url,
            json={"version": "1", "kind": "edit_summary", "text": "Saved edit"},
            headers=_HEADERS,
        )
    assert response.status_code == 200
    assert response.json()["version"] == "2"
    assert (await client.get(url, headers=_HEADERS)).json() == response.json()


async def test_background_launches_accept_two_and_queue_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflows = Workflows(tmp_path / "state", {}, config_path=tmp_path / "config.toml")
    app = _app(workflows, workflows.inbox_watch(monotonic=lambda: 0))
    entered = {number: Event() for number in (1, 2, 3)}
    release = {number: Event() for number in entered}
    calls: list[int] = []

    class GateReviewer:
        def __init__(self, *_args: object) -> None:
            pass

        def review(self, selection: ReviewSelection) -> GeneratedReview:
            number = selection.ref.number
            calls.append(number)
            entered[number].set()
            assert release[number].wait(10)
            return GeneratedReview(
                ReviewDraft("comment", "Ready", ()), "fixture/model", None
            )

    monkeypatch.setattr(composition, "open_gh_environment", fake_gh_environment)
    monkeypatch.setattr(composition, "GhPullRequestReader", FakeReader)
    monkeypatch.setattr(composition, "HostedReportReviewer", GateReviewer)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=_ORIGIN
    ) as client:

        async def launch(number: int) -> httpx.Response:
            return await asyncio.wait_for(
                client.post(
                    "/api/v1/reviews/launches",
                    headers=_HEADERS,
                    json={"url": f"https://github.com/org/repo/pull/{number}"},
                ),
                2,
            )

        async with app.router.lifespan_context(app):
            try:
                first = await launch(1)
                assert first.status_code == 202
                assert await asyncio.to_thread(entered[1].wait, 2)
                assert (await launch(2)).status_code == 202
                assert await asyncio.to_thread(entered[2].wait, 2)
                third = await launch(3)
                assert third.status_code == 202
                assert (await launch(3)).json() == third.json()
                rows = (await client.get("/api/v1/reviews", headers=_HEADERS)).json()
                assert sorted(row["status"] for row in rows) == [
                    "queued",
                    "running",
                    "running",
                ]
                assert not entered[3].is_set()
                for number in range(4, 35):
                    assert (await launch(number)).status_code == 202
                full = await launch(35)
                assert full.status_code == 409
                assert full.json()["code"] == "review_queue_full"
                release[1].set()
                assert await asyncio.to_thread(entered[3].wait, 2)
                assert not release[2].is_set()
            finally:
                app.state.stop_launches()
                for event in release.values():
                    event.set()
    assert calls == [1, 2, 3]
    restarted = _app(workflows, workflows.inbox_watch(monotonic=lambda: 0))
    async with (
        restarted.router.lifespan_context(restarted),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted), base_url=_ORIGIN
        ) as client,
    ):
        rows = (await client.get("/api/v1/reviews", headers=_HEADERS)).json()
        assert len(rows) == 3
        assert all(row["status"] == "draft_ready" for row in rows)


class _BackgroundReviewer:
    def __init__(self, *_args: object) -> None:
        pass

    def review(self, selection: ReviewSelection) -> GeneratedReview:
        return _GENERATED


@pytest.mark.parametrize(
    "case", ["stale", "unverified", "github", "inbox", "completed", "competing"]
)
async def test_active_list_reports_background_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    state = tmp_path / "state"
    workflows = Workflows(state, {}, config_path=tmp_path / "config.toml")
    app = _app(workflows, workflows.inbox_watch(monotonic=lambda: 0))
    store = SqliteReviewRunStore(state / "reviews.sqlite3")
    saved = None

    class Reader(FakeReader):
        @override
        def read_pull_request(self, ref: PullRequestRef) -> tuple[str, PullRequest]:
            if case == "github":
                raise GitHubReadError("Could not read GitHub.")
            return super().read_pull_request(ref)

    def verify(_workflows: Workflows, _ref: PullRequestRef) -> ReviewSelection:
        if case == "unverified":
            raise GitHubReadError("Could not verify GitHub.")
        return replace(
            _SAVED_SELECTION, revision=replace(_SAVED_SELECTION.revision, head="d" * 40)
        )

    monkeypatch.setattr(composition, "open_gh_environment", fake_gh_environment)
    monkeypatch.setattr(composition, "GhPullRequestReader", Reader)
    monkeypatch.setattr(composition, "HostedReportReviewer", _BackgroundReviewer)
    if case in ("stale", "unverified"):
        monkeypatch.setattr(Workflows, "select", verify)
    if case == "completed":
        saved = store.start(_SAVED_SELECTION, replaces=None).finish(_GENERATED)
        store.save(saved)
        store.complete(saved.complete_locally())
    if case == "competing":
        saved = store.start(_SAVED_SELECTION, replaces=None)
    competing = LockedReviewExecution(state / "locks", _BackgroundReviewer)
    with (
        competing.acquire(_SAVED_SELECTION.key)
        if case == "competing"
        else nullcontext()
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=_ORIGIN
        ) as client:
            async with app.router.lifespan_context(app):
                if case == "inbox":
                    response = await client.post(
                        "/api/v1/inbox/launch",
                        headers=_HEADERS,
                        json={
                            "url": _REVIEW_URL,
                            "settingsVersion": "1",
                            "identity": {
                                "host": "github.com",
                                "repositoryId": "42",
                                "number": "1",
                                "account": "reviewer",
                            },
                        },
                    )
                else:
                    response = await client.post(
                        "/api/v1/reviews/launches",
                        headers=_HEADERS,
                        json={"url": _REVIEW_URL},
                    )
                assert response.status_code == 202
            (row,) = (await client.get("/api/v1/reviews", headers=_HEADERS)).json()
    if case in ("stale", "unverified"):
        assert row["kind"] == "saved"
        assert row["status"] == "draft_ready"
        assert row["changes"] == (["head"] if case == "stale" else "unavailable")
    elif case == "competing":
        assert saved is not None and row["runId"] == str(saved.id)
        assert row["status"] == "running"
    elif case == "completed":
        assert saved is not None and row["completedRunId"] == str(saved.id)
        assert row["kind"] == "launch" and row["status"] == "completed"
    else:
        assert row["kind"] == "launch" and row["status"] == "failed"
        assert row["error"]["code"] == (
            "github_unavailable" if case == "github" else "inbox_changed"
        )


@pytest.mark.parametrize("changed", ["comparison", "replacement", "same"])
async def test_background_retry_rechecks_the_saved_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    state = tmp_path / "state"
    workflows = Workflows(state, {}, config_path=tmp_path / "config.toml")
    app = _app(workflows, workflows.inbox_watch(monotonic=lambda: 0))
    store = SqliteReviewRunStore(state / "reviews.sqlite3")
    original = store.start(_SAVED_SELECTION, replaces=None)
    store.save(original.fail("Original failure", "failed"))
    entered, release = Event(), Event()

    class Reader(FakeReader):
        @override
        def read_pull_request(self, ref: PullRequestRef) -> tuple[str, PullRequest]:
            entered.set()
            assert release.wait(5)
            account, pr = super().read_pull_request(ref)
            return account, replace(
                pr, head="d" * 40
            ) if changed == "comparison" else pr

        @override
        def read_revision(self, _pull_request: PullRequest) -> ReviewedRevision:
            return replace(
                super().read_revision(_pull_request), head=_pull_request.head
            )

    monkeypatch.setattr(composition, "open_gh_environment", fake_gh_environment)
    monkeypatch.setattr(composition, "GhPullRequestReader", Reader)
    monkeypatch.setattr(composition, "HostedReportReviewer", _BackgroundReviewer)
    replacement_id = original.id
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=_ORIGIN
    ) as client:
        retry_url = f"/api/v1/reviews/{original.id}/retry"
        async with app.router.lifespan_context(app):
            try:
                accepted = await client.post(retry_url, headers=_HEADERS)
                assert accepted.status_code == 202
                assert await asyncio.to_thread(entered.wait, 2)
                duplicate = await client.post(retry_url, headers=_HEADERS)
                assert duplicate.json() == accepted.json()
                (pending,) = (
                    await client.get("/api/v1/reviews", headers=_HEADERS)
                ).json()
                assert pending["status"] == "running"
                if changed == "replacement":
                    replacement = store.start(
                        _SAVED_SELECTION, replaces=original.id
                    ).finish(_GENERATED)
                    store.save(replacement)
                    replacement_id = replacement.id
            finally:
                release.set()
        rows = (await client.get("/api/v1/reviews", headers=_HEADERS)).json()
        if changed == "same":
            (saved,) = rows
            assert saved["runId"] != str(original.id)
        else:
            failure, saved = rows
            assert failure["kind"] == "launch"
            assert failure["error"]["code"] == "review_unavailable"
            assert "attempt or its comparison changed" in failure["error"]["message"]
            assert saved["runId"] == str(replacement_id)
        assert saved["status"] == (
            "failed" if changed == "comparison" else "draft_ready"
        )
        if changed != "comparison":
            stale_retry = await client.post(retry_url, headers=_HEADERS)
            assert stale_retry.status_code == 409
            assert (
                stale_retry.json()["message"]
                == "This attempt is no longer available to retry."
            )


@pytest.mark.parametrize("action", ["confirm", "recover", "retry"])
async def test_publication_command_maps_unknown_preview_to_not_found(
    client: httpx.AsyncClient, action: str
) -> None:
    response = await client.post(
        f"/api/v1/publications/66206015-5d37-4d21-8bf2-10d48d043413/{action}",
        headers=_HEADERS,
        json={"acceptDuplicateRisk": False} if action == "retry" else None,
    )
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


@pytest.mark.parametrize("action", ["scan", "confirm", "recover", "review-retry"])
@pytest.mark.parametrize("media_type", ["application/json", "text/plain", None])
async def test_bodyless_mutation_rejects_any_body(
    client: httpx.AsyncClient, action: str, media_type: str | None
) -> None:
    route = (
        "/inbox/scan"
        if action == "scan"
        else "/reviews/1/retry"
        if action == "review-retry"
        else f"/publications/66206015-5d37-4d21-8bf2-10d48d043413/{action}"
    )
    headers = _HEADERS | ({"Content-Type": media_type} if media_type else {})
    response = await client.post(f"/api/v1{route}", headers=headers, content=b"{}")
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_input"
    assert response.json()["message"] == "This action does not accept a request body."


async def test_requires_capability_host_and_write_origin(
    client: httpx.AsyncClient,
) -> None:
    unauthorized = await client.get("/api/v1/inbox")
    assert unauthorized.status_code == 401
    wrong_host = await client.get(
        "/api/v1/inbox", headers={**_HEADERS, "Host": "bad.invalid"}
    )
    assert wrong_host.status_code == 403
    cross_origin = await client.post(
        "/api/v1/inbox/scan", headers={**_HEADERS, "Origin": "https://bad.invalid"}
    )
    assert cross_origin.status_code == 403
    assert "access-control-allow-origin" not in cross_origin.headers
    valid = await client.get("/api/v1/inbox", headers=_HEADERS)
    assert valid.json() == {"kind": "unconfigured"}
    assert valid.headers["cache-control"] == "no-store"
    assert valid.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize(
    ("method", "route"),
    [
        ("GET", "/reviews"),
        ("POST", "/reviews/launches"),
        ("POST", "/inbox/launch"),
        ("POST", "/reviews/1/retry"),
    ],
)
async def test_background_routes_enforce_local_session(
    client: httpx.AsyncClient, method: str, route: str
) -> None:
    url = f"/api/v1{route}"
    assert (await client.request(method, url)).status_code == 401
    assert (
        await client.request(method, url, headers=_HEADERS | {"Host": "bad.invalid"})
    ).status_code == 403
    if method == "POST":
        denied = await client.request(
            method, url, headers=_HEADERS | {"Origin": "https://bad.invalid"}
        )
        assert denied.status_code == 403


@pytest.mark.parametrize("priced", [True, False], ids=["priced", "legacy"])
async def test_draft_and_completion_return_saved_generation_metadata(
    client: httpx.AsyncClient, tmp_path: Path, priced: bool
) -> None:
    store = SqliteReviewRunStore(tmp_path / "loop-reviewer" / "reviews.sqlite3")
    selection = ReviewSelection(
        PullRequestRef("github.com", "org", "repo", 1, 42),
        "reviewer",
        "teammate",
        ReviewedRevision("main", "a" * 40, "b" * 40, "c" * 40),
    )
    amount = 0.023456789 if priced else None
    saved = store.start(selection, replaces=None).finish(
        GeneratedReview(
            ReviewDraft("approved", "Original", ()), "provider/model", amount
        )
    )
    if not priced:
        saved = replace(saved, generation_model=None)
    store.save(saved)
    url = f"/api/v1/drafts/{saved.id}"
    draft = await client.get(url, headers=_HEADERS)
    finished = await client.post(
        f"{url}/finish", headers=_HEADERS, json={"version": "1"}
    )
    history = await client.get(f"/api/v1/history/{saved.id}", headers=_HEADERS)
    for response in (draft, finished, history):
        assert response.status_code == 200
        assert response.json()["generationModel"] == (
            "provider/model" if priced else None
        )
        assert response.json()["estimatedCostUsd"] == amount


@pytest.mark.parametrize(
    "data",
    [
        {"version": 1, "kind": "edit_summary", "text": "Saved locally"},
        {"version": "1", "kind": "edit_summary", "text": "🌿" * 16385},
        {"version": "0", "kind": "drop_finding", "number": 1},
        {"version": "1", "kind": "drop_finding", "number": True},
        {
            "version": "1",
            "kind": "drop_finding",
            "number": 1,
            "unexpected": "private data",
        },
    ],
    ids=[
        "version-number",
        "decoded-size",
        "zero-version",
        "boolean-position",
        "extra-field",
    ],
)
async def test_invalid_patch_returns_typed_error(
    client: httpx.AsyncClient, data: dict[str, object]
) -> None:
    response = await client.patch("/api/v1/drafts/1", json=data, headers=_HEADERS)
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_input"
    assert "private data" not in response.text


@pytest.mark.parametrize("repair", [False, True], ids=["first-save", "repair"])
async def test_setup_applies_one_project_to_multiple_repositories(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repair: bool,
) -> None:
    urls = ["https://github.com/org/first", "https://github.com/org/second"]
    account = "alice"

    def verify(_self: object, address: RepositoryAddress) -> WatchedRepository:
        if address.slug == "org/unavailable":
            raise RepositoryDiscoveryError("Repository unavailable")
        return WatchedRepository(address, urls.index(address.url) + 1, account)

    def discover(
        _self: object, repo: WatchedRepository
    ) -> tuple[DiscoveredPullRequest, ...]:
        return tuple(
            DiscoveredPullRequest(
                PullRequestRef.from_repository(
                    repo.address.host,
                    repo.address.slug,
                    number,
                    repository_id=repo.repository_id,
                ),
                f"[{key}-1] Check",
                "bob",
                "open",
                False,
                (),
                1795780800 if number == 1 else None,
            )
            for number, key in enumerate(("ABC", "XYZ", "OTHER"), 1)
        )

    monkeypatch.setattr(composition.GhRepositoryDiscovery, "verify", verify)
    monkeypatch.setattr(composition.GhRepositoryDiscovery, "list_open", discover)
    url = "/api/v1/inbox/configuration"
    path = tmp_path / "config.toml"
    if repair:
        path.write_text('[inbox]\njira_project_key = "OLD"\nrepositories = []\n')
    empty = await client.get(url, headers=_HEADERS)
    assert empty.json() == {
        "settingsVersion": None,
        "excludedTitleTerms": [],
        "jiraProjectKeys": [],
        "repositoryUrls": [],
        "configRevision": empty.json()["configRevision"],
    }
    body: dict[str, object] = {
        "settingsVersion": None,
        "jiraProjectKeys": [" abc "],
        "repositoryUrls": urls,
        "configRevision": empty.json()["configRevision"],
    }
    saved = await client.post(url, json=body, headers=_HEADERS)
    assert saved.status_code == 200
    settings = saved.json()
    assert settings == {
        "settingsVersion": "1",
        "excludedTitleTerms": [],
        "jiraProjectKeys": ["ABC"],
        "repositoryUrls": urls,
        "configRevision": settings["configRevision"],
    }
    assert loads(path.read_text()) == {
        "inbox": {"jira_project_key": "ABC", "repositories": urls},
    }
    stale = await client.post(url, json=body, headers=_HEADERS)
    assert stale.status_code == 409
    assert stale.json()["code"] == "inbox_changed"
    body["settingsVersion"] = "1"
    body["configRevision"] = settings["configRevision"]
    extra_project = await client.post(
        url, json={**body, "jiraProjectKeys": ["ABC", "XYZ"]}, headers=_HEADERS
    )
    assert extra_project.status_code == 422
    failed = await client.post(
        url,
        json={
            **body,
            "repositoryUrls": [urls[0], "https://github.com/org/unavailable"],
        },
        headers=_HEADERS,
    )
    assert failed.status_code == 502
    assert (await client.get(url, headers=_HEADERS)).json() == settings

    legacy = '\n[reviewer]\nengine = "legacy"\nmodel = 123\n'
    path.write_text(path.read_text() + legacy)
    conflict = await client.post(url, json=body, headers=_HEADERS)
    assert conflict.status_code == 409
    latest = (await client.get(url, headers=_HEADERS)).json()
    assert latest["settingsVersion"] == settings["settingsVersion"]
    assert "reviewerModel" not in latest

    scanned = await client.post("/api/v1/inbox/scan", headers=_HEADERS)
    assert scanned.status_code == 200
    report = scanned.json()
    assert report["prefix"] == "[ABC-…]"
    assert [(row["repository"], row["reasons"]) for row in report["active"]] == [
        (repo, ["Matches [ABC-…]"]) for repo in ("org/first", "org/second")
    ]
    assert [row["openedAt"] for row in report["active"]] == [1795780800] * 2
    assert [row["reasonDetails"] for row in report["active"]] == [
        [{"code": "jira_project", "label": "Matches [ABC-…]"}]
    ] * 2

    updated = await client.post(
        url,
        json={**latest, "jiraProjectKeys": ["XYZ"], "repositoryUrls": urls[1:]},
        headers=_HEADERS,
    )
    assert updated.json() == {
        "settingsVersion": "2",
        "excludedTitleTerms": [],
        "jiraProjectKeys": ["XYZ"],
        "repositoryUrls": urls[1:],
        "configRevision": updated.json()["configRevision"],
    }
    assert legacy in path.read_text()
    rescanned = await client.post("/api/v1/inbox/scan", headers=_HEADERS)
    assert [row["title"] for row in rescanned.json()["active"]] == ["[XYZ-1] Check"]

    account = "carol"
    path.write_text(path.read_text().replace("XYZ", "OTHER"))
    blocked = await client.get("/api/v1/inbox", headers=_HEADERS)
    assert blocked.status_code == 502
    assert "identity changed" in blocked.json()["message"]
    pending = (await client.get(url, headers=_HEADERS)).json()
    assert pending["jiraProjectKeys"] == ["OTHER"]
    accepted = await client.post(url, json=pending, headers=_HEADERS)
    assert accepted.status_code == 200
    assert accepted.json()["settingsVersion"] == "3"
    rescanned = await client.post("/api/v1/inbox/scan", headers=_HEADERS)
    (row,) = rescanned.json()["active"]
    assert row["openedAt"] is None
    invalid = await client.post(
        url,
        json={**accepted.json(), "excludedTitleTerms": ["release\nnotes"]},
        headers=_HEADERS,
    )
    assert invalid.status_code == 422
    assert "single line" in invalid.json()["message"]
    filtered = await client.post(
        url,
        json={
            **accepted.json(),
            "excludedTitleTerms": ["unused", " chECK ", "CHECK", " "],
        },
        headers=_HEADERS,
    )
    assert filtered.json()["excludedTitleTerms"] == ["unused", "chECK"]
    assert loads(path.read_text())["inbox"]["excluded_title_terms"] == [
        "unused",
        "chECK",
    ]
    annotated = (
        '[\n    "unused", # Keep this rationale\n    " chECK ", "CHECK", " ",\n]'
    )
    path.write_text(path.read_text().replace('["unused", "chECK"]', annotated))
    same_report = (await client.get("/api/v1/inbox", headers=_HEADERS)).json()
    assert same_report["active"] == []
    assert same_report["settingsVersion"] == filtered.json()["settingsVersion"]
    same_settings = (await client.get(url, headers=_HEADERS)).json()
    assert same_settings["excludedTitleTerms"] == filtered.json()["excludedTitleTerms"]
    without_terms = {
        key: value
        for key, value in same_settings.items()
        if key != "excludedTitleTerms"
    }
    resaved = await client.post(url, json=without_terms, headers=_HEADERS)
    assert resaved.status_code == 200
    assert annotated in path.read_text()
    cleared = await client.post(
        url, json={**resaved.json(), "excludedTitleTerms": []}, headers=_HEADERS
    )
    assert cleared.status_code == 200
    assert (await client.get("/api/v1/inbox", headers=_HEADERS)).json()["active"] == [
        row
    ]
    assert row["title"] == "[OTHER-1] Check"
    assert row["identity"]["account"] == "carol"

    deferred = await client.post(
        "/api/v1/inbox/later",
        json={
            "url": row["url"],
            "identity": row["identity"],
            "settingsVersion": cleared.json()["settingsVersion"],
        },
        headers=_HEADERS,
    )
    assert deferred.status_code == 200
    account = "alice"
    hidden = await client.post(
        url, json={**cleared.json(), "excludedTitleTerms": ["Check"]}, headers=_HEADERS
    )
    assert hidden.status_code == 200
    assert (await client.get("/api/v1/inbox", headers=_HEADERS)).json()["later"] == []
    for action in ("open", "later", "restore"):
        rejected = await client.post(
            f"/api/v1/inbox/{action}",
            json={
                "url": row["url"],
                "identity": row["identity"],
                "settingsVersion": hidden.json()["settingsVersion"],
            },
            headers=_HEADERS,
        )
        assert rejected.status_code == 409
        assert rejected.json()["code"] == "inbox_changed"
    shown = await client.post(
        url, json={**hidden.json(), "excludedTitleTerms": []}, headers=_HEADERS
    )
    assert shown.status_code == 200
    assert (await client.get("/api/v1/inbox", headers=_HEADERS)).json()["later"] == [
        row
    ]


@pytest.mark.parametrize(
    ("initial_terms", "repeated_terms"),
    [([], []), (["Check"], [" Check ", "CHECK", " "])],
    ids=["unchanged-empty", "unchanged-after-normalization"],
)
async def test_explicit_save_reverifies_unchanged_title_exclusions(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    initial_terms: list[str],
    repeated_terms: list[str],
) -> None:
    account = "alice"

    def verify(_self: object, address: RepositoryAddress) -> WatchedRepository:
        return WatchedRepository(address, 42, account)

    def discover(
        _self: object, repo: WatchedRepository
    ) -> tuple[DiscoveredPullRequest, ...]:
        assert repo.account == account
        return ()

    monkeypatch.setattr(composition.GhRepositoryDiscovery, "verify", verify)
    monkeypatch.setattr(composition.GhRepositoryDiscovery, "list_open", discover)
    url = "/api/v1/inbox/configuration"
    initial = (await client.get(url, headers=_HEADERS)).json()
    saved = await client.post(
        url,
        json={
            **initial,
            "jiraProjectKeys": ["ABC"],
            "repositoryUrls": ["https://github.com/org/repo"],
            "excludedTitleTerms": initial_terms,
        },
        headers=_HEADERS,
    )
    assert saved.status_code == 200
    account = "bob"
    resaved = await client.post(
        url,
        json={**saved.json(), "excludedTitleTerms": repeated_terms},
        headers=_HEADERS,
    )
    assert resaved.status_code == 200
    scanned = await client.post("/api/v1/inbox/scan", headers=_HEADERS)
    assert scanned.status_code == 200
    assert scanned.json()["repositories"][0]["account"] == "bob"


async def test_reconfigured_inbox_rejects_old_row_and_preserves_partial_results(
    client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbox = SqliteInboxStore(tmp_path / "loop-reviewer" / "reviews.sqlite3")
    first = WatchedRepository(RepositoryAddress("github.com", "org/repo"), 42, "alice")
    second = WatchedRepository(
        RepositoryAddress("github.com", "org/other"), 43, "alice"
    )
    ref = PullRequestRef("github.com", "org", "repo", 1, 42)
    row = DiscoveredPullRequest(ref, "[ABC-1] Check", "bob", "open", False, ())
    existing = inbox.replace_settings((JiraProjectKey("ABC"),), (first, second))

    def discover(
        _self: object, repo: WatchedRepository
    ) -> tuple[DiscoveredPullRequest, ...]:
        if repo == second:
            raise RepositoryDiscoveryError("Repository unavailable")
        return (row,)

    monkeypatch.setattr(composition.GhRepositoryDiscovery, "list_open", discover)
    before = await client.get("/api/v1/inbox", headers=_HEADERS)
    assert [repo["status"] for repo in before.json()["repositories"]] == [
        "pending",
        "pending",
    ]
    scanned = await client.post("/api/v1/inbox/scan", headers=_HEADERS)
    assert scanned.status_code == 200
    report = scanned.json()
    assert [repo["status"] for repo in report["repositories"]] == ["success", "failed"]
    stale = {
        "url": ref.url,
        "settingsVersion": str(existing.version),
        "identity": report["active"][0]["identity"],
    }
    latest = inbox.replace_settings((JiraProjectKey("ABC"),), (first, second))
    await client.post("/api/v1/inbox/scan", headers=_HEADERS)
    for action in ("open", "later", "restore"):
        result = await client.post(
            f"/api/v1/inbox/{action}", json=stale, headers=_HEADERS
        )
        assert result.status_code == 409
        assert result.json()["code"] == "inbox_changed"

    def offline(_self: object, _address: RepositoryAddress) -> WatchedRepository:
        raise RepositoryDiscoveryError("Offline")

    monkeypatch.setattr(composition.GhRepositoryDiscovery, "verify", offline)
    (tmp_path / "config.toml").write_text(
        '[inbox]\njira_project_key = "XYZ"\nrepositories = ["https://github.com/org/repo"]\n'
    )
    blocked = await client.get("/api/v1/inbox", headers=_HEADERS)
    assert blocked.status_code == 502
    current = {**stale, "settingsVersion": str(latest.version)}
    for action, section in (("later", "later"), ("restore", "active")):
        result = await client.post(
            f"/api/v1/inbox/{action}", json=current, headers=_HEADERS
        )
        assert result.status_code == 200
        assert [row["url"] for row in result.json()[section]] == [ref.url]


async def test_cli_and_http_edit_share_version_and_exact_publication(
    client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "loop-reviewer" / "reviews.sqlite3"
    store = SqliteReviewRunStore(path)
    ref = PullRequestRef("github.com", "org", "repo", 1, 42)
    revision = ReviewedRevision("main", "a" * 40, "b" * 40, "c" * 40)
    selection = ReviewSelection(ref, "reviewer", "teammate", revision)
    store.start(replace(selection, account="other"), replaces=None)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE sqlite_sequence SET seq = 9007199254740992 WHERE name = 'review_runs'"
        )
    saved = store.start(selection, replaces=None).finish(
        GeneratedReview(
            ReviewDraft("approved", "Original", ()), "provider/model", 0.023456789
        )
    )
    store.save(saved)
    assert saved.id == 9007199254740993
    url = f"/api/v1/drafts/{saved.id}"

    original = await client.get(url, headers=_HEADERS)
    assert original.status_code == 200
    assert original.json()["runId"] == str(saved.id)
    edit = tmp_path / "feedback.txt"
    edit.write_bytes(b"Edited from CLI.\r\n")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "loop-reviewer",
            "draft",
            "edit-summary",
            str(saved.id),
            "--version",
            "1",
            "--file",
            str(edit),
        ],
    )
    assert cli.main() == 0
    conflicted = await client.patch(
        url,
        json={"version": "1", "kind": "edit_summary", "text": "Stale browser edit"},
        headers=_HEADERS,
    )
    assert conflicted.status_code == 409
    assert conflicted.json()["code"] == "draft_changed"
    changed = await client.patch(
        url,
        json={
            "version": "2",
            "kind": "edit_summary",
            "text": "Saved on the web.\r\nSecond line.",
        },
        headers=_HEADERS,
    )
    assert changed.status_code == 200
    assert changed.json()["version"] == "3"
    assert (await client.get(f"{url}/copy", headers=_HEADERS)).json()[
        "markdown"
    ] == "Saved on the web.\nSecond line."

    class RecoveringPublication(FakePublication):
        @override
        def submit(self, preview: PublicationPreview) -> Literal["unknown"]:
            assert (
                preview.body
                == f"{preview.id.marker}\n\nSaved on the web.\nSecond line."
            )
            return "unknown"

        def reconcile(
            self, preview: PublicationPreview, *, ref: PullRequestRef
        ) -> PublicationReceipt:
            assert preview.id.marker in preview.body
            assert ref.url == preview.selection.ref.url
            return PublicationReceipt(71, "comment")

    def session(
        _env: dict[str, str], _host: str, *, inherited_fd: int | None = None
    ) -> nullcontext[RecoveringPublication]:
        return nullcontext(RecoveringPublication())

    monkeypatch.setattr(composition, "open_gh_publication", session)
    prepared = await client.post(
        f"{url}/publication-preview",
        json={"version": "3", "action": "comment"},
        headers=_HEADERS,
    )
    assert prepared.status_code == 200
    preview = prepared.json()
    id = preview["id"]
    snapshot = await client.get(f"/api/v1/publications/{id}", headers=_HEADERS)
    assert snapshot.json()["preview"] == preview
    assert (
        preview["body"]
        == f"<!-- loop-reviewer:{id} -->\n\nSaved on the web.\nSecond line."
    )
    assert (
        await client.post(f"/api/v1/publications/{id}/confirm", headers=_HEADERS)
    ).json()["status"] == "unconfirmed"
    pending = await client.get("/api/v1/publications/unconfirmed", headers=_HEADERS)
    assert [record["preview"]["id"] for record in pending.json()] == [id]
    assert (await client.get(url, headers=_HEADERS)).json()["publicationId"] == id
    recovered = await client.post(
        f"/api/v1/publications/{id}/recover", headers=_HEADERS
    )
    assert recovered.json()["status"] == "published"
    history = await client.get(f"/api/v1/history/{saved.id}", headers=_HEADERS)
    assert history.json()["receipt"]["reviewId"] == "71"
    assert history.json()["selection"] == preview["selection"]
    assert history.json()["generationModel"] == "provider/model"
    assert history.json()["estimatedCostUsd"] == 0.023456789
    assert (await client.get(url, headers=_HEADERS)).json()["completedRunId"] == str(
        saved.id
    )


@pytest.mark.parametrize("version", [5, 6], ids=["compiler-v5", "pinned-v6"])
async def test_saved_report_survives_browser_edits_and_is_omitted_from_list(
    client: httpx.AsyncClient, tmp_path: Path, version: int
) -> None:
    fixture = offline_report if version == 5 else offline_pinned_report
    report = fixture("alpha.py", "beta.py")
    pins = report.root.comparison
    selection = ReviewSelection(
        PullRequestRef("github.com", "org", "repo", 1, 42),
        "reviewer",
        "teammate",
        ReviewedRevision("main", pins.merge_base, pins.merge_base, pins.head),
    )
    store = SqliteReviewRunStore(tmp_path / "loop-reviewer" / "reviews.sqlite3")
    run = store.start(selection, replaces=None).finish(
        GeneratedReview(project_report(report), None, None, report)
    )
    store.save(run)
    url = f"/api/v1/drafts/{run.id}"
    saved = (await client.get(url, headers=_HEADERS)).json()
    original = report.model_dump(mode="json")
    assert saved["report"] == original
    assert [finding["reportId"] for finding in saved["feedback"]["findings"]] == [
        finding["id"] for finding in original["findings"]
    ]
    assert [row["new"]["path"] for row in saved["report"]["file_coverage"]] == [
        "alpha.py",
        "beta.py",
    ]
    edited = (
        await client.patch(
            url,
            json={"kind": "drop_finding", "number": 1, "version": "1"}
            if version == 5
            else {"kind": "edit_summary", "text": "Reviewed locally", "version": "1"},
            headers=_HEADERS,
        )
    ).json()
    assert len(edited["feedback"]["findings"]) == (1 if version == 5 else 0)
    assert edited["report"] == original
    finished = await client.post(
        f"{url}/finish", json={"version": edited["version"]}, headers=_HEADERS
    )
    assert finished.status_code == 200
    completion = (
        await client.get(f"/api/v1/history/{run.id}", headers=_HEADERS)
    ).json()
    assert completion["report"] == original
    page = (await client.get("/api/v1/history", headers=_HEADERS)).json()
    assert "report" not in page["completions"][0]


async def test_concurrent_opens_share_review_lock(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = Event()
    release = Event()

    class FakeReviewer:
        def __init__(self, *_args: object) -> None:
            pass

        def review(self, _selection: ReviewSelection) -> GeneratedReview:
            entered.set()
            assert release.wait(5)
            return GeneratedReview(
                ReviewDraft("approved", "Generated once", ()), "provider/model", None
            )

    monkeypatch.setattr(composition, "open_gh_environment", fake_gh_environment)
    monkeypatch.setattr(composition, "GhPullRequestReader", FakeReader)
    monkeypatch.setattr(composition, "HostedReportReviewer", FakeReviewer)

    async def open_review() -> httpx.Response:
        return await client.post(
            "/api/v1/reviews/open",
            json={"url": "https://github.com/org/repo/pull/1", "retry": False},
            headers=_HEADERS,
        )

    first = asyncio.create_task(open_review())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        concurrent = await open_review()
        assert concurrent.json() == {"kind": "unavailable", "reason": "in_progress"}
    finally:
        release.set()
        original = await first
    resumed = await open_review()
    assert original.json()["status"] == "generated"
    assert resumed.json()["status"] == "resumed"
    assert original.json()["draft"] == resumed.json()["draft"]


@pytest.mark.parametrize(
    ("content", "content_type", "expected_status", "code"),
    [
        (b'{"version":"1"}', "text/plain", 415, "unsupported_media_type"),
        (b"{not-json", "application/json", 422, "invalid_input"),
        (
            b'{"text":"' + b"\\u0061" * 140_000 + b'"}',
            "application/json",
            413,
            "body_too_large",
        ),
    ],
    ids=["non-json-media", "malformed-json", "escaped-body-limit"],
)
@pytest.mark.parametrize(
    ("method", "route"),
    [
        ("PATCH", "/drafts/1"),
        ("POST", "/reviews/launches"),
        ("POST", "/inbox/launch"),
    ],
)
async def test_mutation_envelope_before_parsing(
    client: httpx.AsyncClient,
    content: bytes,
    content_type: str,
    expected_status: int,
    code: str,
    method: str,
    route: str,
) -> None:
    result = await client.request(
        method,
        f"/api/v1{route}",
        headers={**_HEADERS, "Content-Type": content_type},
        content=content,
    )
    assert result.status_code == expected_status
    assert result.json()["code"] == code
    assert result.headers["cache-control"] == "no-store"


async def test_reconnect_restarts_polling_before_failed_stream_closes() -> None:
    calls = 0

    def tick() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("Transient watch error")

    watch = BrowserWatch(
        nullcontext, tick, lambda: ScanCycle("test", calls, "complete")
    )
    first, second = watch.events(), watch.events()
    try:
        assert '"ready"' in await anext(first)
        await asyncio.wait_for(watch.drain(), 1)
        assert '"ready"' in await anext(second)
        assert await anext(first, None) is None
        assert '"scan-finished"' in await asyncio.wait_for(anext(second), 1)
    finally:
        await first.aclose()
        await second.aclose()
        await watch.drain()


def test_last_loopback_stream_disconnect_releases_browser_membership(
    tmp_path: Path,
) -> None:
    workflows = Workflows(tmp_path, {}, config_path=tmp_path / "config.toml")
    watcher = workflows.inbox_watch(monotonic=lambda: 0)
    ready = Event()

    def ready_browser(_url: str) -> bool:
        ready.set()
        return True

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = BrowserServer(
            _app(workflows, watcher, origin=origin),
            origin,
            "test-capability",
            ready_browser,
        )
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            assert ready.wait(5)
            with httpx.Client(
                base_url=origin, headers=_HEADERS, timeout=5, trust_env=False
            ) as connection:
                path = "/api/v1/inbox/watch"
                with connection.stream("GET", path) as first:
                    first_events = first.iter_lines()
                    assert next(first_events) == '{"event":"ready"}'
                    with connection.stream("GET", path) as second:
                        second_events = second.iter_lines()
                        assert next(second_events) == '{"event":"ready"}'
                        assert watcher.cycle() is not None
                    assert watcher.cycle() is not None
                deadline = monotonic() + 5
                while watcher.cycle() is not None and monotonic() < deadline:
                    sleep(0.01)
                assert watcher.cycle() is None
                with connection.stream("GET", path) as active:
                    active_events = active.iter_lines()
                    assert next(active_events) == '{"event":"ready"}'
                    server.should_exit = True
                    thread.join(timeout=5)
                    assert not thread.is_alive()
                    assert watcher.cycle() is None
        finally:
            server.should_exit = True
            thread.join(timeout=5)
