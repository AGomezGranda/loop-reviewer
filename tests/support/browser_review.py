"""Exercise hosted CLI and HTTP workflows with real pinned Git and scripted I/O."""

import json
import os
from contextlib import nullcontext
from dataclasses import replace
from functools import partial
from pathlib import Path
from time import monotonic, sleep
from typing import Literal
from unittest.mock import patch

from loop_reviewer import cli, composition
from loop_reviewer.inbox.model import (
    DiscoveredPullRequest,
    RepositoryAddress,
    WatchedRepository,
)
from loop_reviewer.integrations import git_comparison
from loop_reviewer.local_http.serve import serve
from loop_reviewer.review.model import FindingLocation, PublicationReceipt
from loop_reviewer.review.publication_model import PublicationPreview
from loop_reviewer.review.pull_request import (
    PullRequest,
    PullRequestRef,
    ReviewedRevision,
)
from loop_reviewer.reviewer.inventory_contract import InventoryBudget
from loop_reviewer.reviewer.model_contract import (
    CallQuote,
    ModelAttempt,
    ModelCall,
    TokenQuote,
    TokenUsage,
    parse_reply,
)
from tests.support.inventory_git import commit, git, init, revision

_REVISION = ReviewedRevision("main", "a" * 40, "b" * 40, "c" * 40)


def _fixture(repo: Path) -> ReviewedRevision:
    if not repo.exists():
        repo.mkdir()
        init(repo)
        (repo / "example.py").write_text("value = 1\n")
        (repo / "settings.txt").write_text("endpoint=primary\n")
        commit(repo)
        git(repo, "switch", "-qc", "changes")
        (repo / "example.py").write_text("value = )\n")
        (repo / "settings.txt").write_text("endpoint=retired\n")
        head = commit(repo)
        for number in (17, 18, 19):
            git(repo, "update-ref", f"refs/pull/{number}/head", head)
    base = revision(repo, "main")
    return ReviewedRevision("main", base, base, revision(repo, "refs/pull/17/head"))


def _pull_request(ref: PullRequestRef) -> PullRequest:
    return PullRequest(
        replace(ref, repository_id=42),
        "teammate",
        "open",
        False,
        "main",
        _REVISION.head,
    )


class FakeReader:
    def __init__(self, _environment: dict[str, str]) -> None:
        pass

    def read_pull_request(self, ref: PullRequestRef) -> tuple[str, PullRequest]:
        return "reviewer", _pull_request(ref)

    def read_revision(self, _pull_request: PullRequest) -> ReviewedRevision:
        return _REVISION


class FakeDiscovery:
    def __init__(self, _environment: dict[str, str]) -> None:
        pass

    def verify(self, address: RepositoryAddress) -> WatchedRepository:
        return WatchedRepository(address, 42, "reviewer")

    def list_open(
        self, repository: WatchedRepository
    ) -> tuple[DiscoveredPullRequest, ...]:
        address = repository.address
        return (
            DiscoveredPullRequest(
                PullRequestRef.from_repository(
                    address.host,
                    address.slug,
                    17,
                    repository_id=repository.repository_id,
                ),
                "First fixture review",
                "teammate",
                "open",
                False,
                ("reviewer",),
            ),
        )


class BrowserModel:
    def quote(self, call: ModelCall) -> CallQuote:
        return CallQuote(
            call, call.prompt.encode(), TokenQuote("fixture", 100, 10, 50000, 400000)
        )

    def send(
        self, call: ModelCall, quote: CallQuote, *, timeout_seconds: float
    ) -> ModelAttempt:
        if call.kind != "investigation":
            raise AssertionError(
                "An unknown-impact candidate must not reach verification"
            )
        packet = json.loads(call.prompt.split("\n\n", 1)[1])
        head = next(
            item["id"]
            for item in packet["excerpts"]
            if item["range"]["path"] == "settings.txt"
            and item["range"]["side"] == "head"
        )
        response = {
            "schema_version": 1,
            "investigation_id": call.unit_id,
            "kind": "submit_candidates",
            "reviewed_file_ids": ["p00002"],
            "candidates": [
                {
                    "file_id": "p00002",
                    "changed_behavior": "The endpoint changes.",
                    "trigger": "PRIVATE-LEAD-CANARY",
                    "consequence": "An unproven consumer may fail.",
                    "proposed_impact": "unknown",
                    "citations": [{"role": "head_change", "evidence_id": head}],
                    "proposed_anchor_id": None,
                }
            ],
        }
        wire = json.dumps(response)
        return ModelAttempt(
            "success",
            TokenUsage(20, 5, 25),
            parse_reply(call, wire),
            len(wire.encode()),
        )


class FakePublication:
    login = "reviewer"
    actor_id = 7

    def read_pull_request(self, ref: PullRequestRef) -> PullRequest:
        return _pull_request(ref)

    def read_revision(self, _pull_request: PullRequest) -> ReviewedRevision:
        return _REVISION

    def usable_anchors(
        self, _ref: PullRequestRef, requested: frozenset[FindingLocation]
    ) -> frozenset[FindingLocation]:
        return requested

    def submit(
        self, preview: PublicationPreview
    ) -> PublicationReceipt | Literal["unknown"]:
        if (
            os.environ.get("LOOP_REVIEWER_FIXTURE_GATES")
            and preview.selection.ref.number == 18
        ):
            return "unknown"
        with (Path(os.environ["XDG_STATE_HOME"]) / "sent.jsonl").open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "body": preview.body,
                        "action": preview.action,
                        "head": preview.selection.revision.head,
                        "comments": len(preview.comments),
                    }
                )
                + "\n"
            )
        return PublicationReceipt(71, preview.action)


def fake_gh_environment(
    environment: dict[str, str],
    _host: str,
    *,
    prefix: str,
    private_capture_cwd: bool = False,
) -> nullcontext[tuple[dict[str, str], Path]]:
    return nullcontext((environment, Path.cwd()))


def fake_publication(
    _env: dict[str, str], _host: str, *, inherited_fd: int | None = None
) -> nullcontext[FakePublication]:
    return nullcontext(FakePublication())


def _model(_environment: dict[str, str]) -> tuple[InventoryBudget, BrowserModel, str]:
    with (Path(os.environ["XDG_STATE_HOME"]) / "events").open("a") as stream:
        stream.write("review\n")
    return (
        InventoryBudget(max_model_spend_usd_micros=100),
        BrowserModel(),
        "fixture/model",
    )


if __name__ == "__main__":
    repo = Path(os.environ["XDG_STATE_HOME"]) / "fixture-git"
    _REVISION = _fixture(repo)

    def remote(ref: PullRequestRef) -> str:
        if (gates := os.environ.get("LOOP_REVIEWER_FIXTURE_GATES")) and ref.number in (
            17,
            18,
        ):
            directory = Path(gates)
            (directory / f"started-{ref.number}").touch()
            deadline = monotonic() + 20
            while not (directory / f"release-{ref.number}").exists():
                if monotonic() >= deadline:
                    raise TimeoutError(
                        f"Fixture gate for PR {ref.number} was not released"
                    )
                sleep(0.02)
        return str(repo)

    with (
        patch.object(git_comparison, "_remote_url", remote),
        patch.object(composition, "open_gh_environment", fake_gh_environment),
        patch.object(composition, "GhPullRequestReader", FakeReader),
        patch.object(composition, "GhRepositoryDiscovery", FakeDiscovery),
        patch.object(
            composition.GhDiscussionReader,
            "read",
            side_effect=composition.GitHubReadError("Fixture discussion unavailable"),
        ),
        patch.object(composition, "_hosted_model", _model),
        patch.object(composition, "open_gh_publication", fake_publication),
        patch.object(cli, "serve", partial(serve, open_browser=lambda _url: False)),
    ):
        raise SystemExit(cli.main())
