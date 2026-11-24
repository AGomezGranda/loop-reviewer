import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from stat import S_IMODE
from subprocess import CompletedProcess
from typing import override

import pytest

from loop_reviewer import cli
from loop_reviewer.composition import Workflows
from loop_reviewer.integrations import gh_api, github_credentials
from loop_reviewer.integrations.gh_api import BOUNDED_GET_BYTES, GhApi
from loop_reviewer.integrations.github import GhPullRequestReader
from loop_reviewer.integrations.github_discussion import GhDiscussionReader
from loop_reviewer.integrations.review_process import OutputLimitExceeded
from loop_reviewer.review.model import ReviewSelection
from loop_reviewer.review.pull_request import (
    PullRequest,
    PullRequestRef,
    ReviewedRevision,
)
from loop_reviewer.reviewer.discussion_contract import (
    DiscussionSnapshotV1,
    DiscussionTarget,
)
from tests.test_local_checks import PINS

TARGET = DiscussionTarget(
    host="github.com",
    repository_id=42,
    owner="octo",
    repo="demo",
    number=7,
    target_branch="main",
    comparison=PINS,
)
NOW = datetime(2026, 11, 23, tzinfo=UTC)


def page(
    nodes: list[dict[str, object]], cursor: str = "next", *, more: bool = False
) -> dict[str, object]:
    return {
        "nodes": nodes,
        "pageInfo": {"hasNextPage": more, "endCursor": cursor if nodes else None},
    }


def comment(key: str, parent: str | None = None) -> dict[str, object]:
    return {
        "id": key,
        "body": "private text",
        "path": "old.py",
        "originalCommit": {"oid": PINS.head},
        "originalLine": 2,
        "originalStartLine": None,
        "pullRequest": {"id": "PR1"},
        "diffHunk": "@@ -1 +1 @@\n-old\n+new",
        "replyTo": {"id": parent} if parent else None,
        "url": TARGET.url + "#discussion_r123",
    }


def thread(key: str, *, more: bool = False) -> dict[str, object]:
    return {
        "id": key,
        "isResolved": False,
        "isOutdated": False,
        "path": "new.py",
        "diffSide": "RIGHT",
        "startDiffSide": None,
        "line": 21,
        "startLine": None,
        "comments": page([comment(key + "root")], more=more),
    }


class FakeGitHub:
    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.elapsed = 0.0
        self.calls: list[tuple[str, str | None]] = []
        self.metadata_calls = 0
        self.compares = 0

    def now(self) -> float:
        return self.elapsed

    def response(self, endpoint: str, raw: bytes | None, timeout: float) -> object:
        if raw is None:
            if self.scenario == "deadline" and self.elapsed >= 15:
                assert timeout >= 4
                self.elapsed += 4
            self.calls.append((endpoint, None))
            return self.rest(endpoint)
        payload = json.loads(raw)
        variables = payload["variables"]
        cursor = variables["cursor"]
        assert variables["size"] == 20
        query = payload["query"]
        self.calls.append((query, cursor))
        if self.scenario == "deadline":
            self.elapsed += timeout
        if "node(id:" in query:
            assert variables["thread"] == "T1"
            reply = comment(
                "T1reply", "FOREIGN" if self.scenario == "foreign-parent" else "T1root"
            )
            if self.scenario == "foreign-pr":
                reply["pullRequest"] = {"id": "OTHER"}
            if self.scenario == "oversized-reply":
                reply["body"] = "🌊" * 1025
            return {
                "data": {
                    "node": {
                        "id": "FOREIGN" if self.scenario == "foreign-node" else "T1",
                        "comments": page([reply], "done"),
                    }
                }
            }
        assert (variables["owner"], variables["repo"], variables["number"]) == (
            "octo",
            "demo",
            7,
        )
        field = next(
            key
            for key in ("reviewThreads", "reviews", "comments")
            if f"{key}(first:" in query
        )
        if self.scenario == "page-error" and field == "reviews" and cursor:
            return {"data": None, "errors": [{"message": "private text"}]}
        more = cursor is None and self.scenario not in {
            "empty",
            "deadline",
            "drift",
            "wrong-host",
            "wrong-head",
            "wrong-repo",
            "oversized",
            "wrong-account",
            "unattested",
        }
        if self.scenario in {"calls", "deadline"} and field == "reviewThreads":
            number = len(self.calls)
            connection = page([thread(f"T{number}")], f"cursor{number}", more=True)
        elif self.scenario == "empty":
            connection = page([])
        elif self.scenario == "snapshot-cap" and field in {"reviews", "comments"}:
            start = int(cursor or 0)
            connection = page(
                [
                    {"id": f"{field}{i:03d}", "body": "x" * 4096, "commit": None}
                    for i in range(start, start + 20)
                ],
                str(start + 20),
                more=start < 80,
            )
        else:
            suffix = "2" if cursor and self.scenario != "repeat-id" else "1"
            item = (
                thread(
                    "T" + suffix,
                    more=more
                    and self.scenario
                    in {
                        "pages",
                        "foreign-parent",
                        "foreign-pr",
                        "foreign-node",
                        "oversized-reply",
                    },
                )
                if field == "reviewThreads"
                else {"id": field + suffix, "body": "private text"}
            )
            if field == "reviews":
                item["commit"] = None
            if self.scenario == "oversized" and field == "reviews":
                item["body"] = "a" * (256 * 1024)
            if (
                self.scenario == "reply-before-root"
                and field == "reviewThreads"
                and not cursor
            ):
                item["comments"] = page(
                    [comment("T1reply", "T1root"), comment("T1root")]
                )
            connection = page(
                [item],
                "next" if not cursor or self.scenario == "repeat-cursor" else "done",
                more=more or (self.scenario == "repeat-cursor" and bool(cursor)),
            )
        return {
            "data": {
                "repository": {
                    "databaseId": 42,
                    "nameWithOwner": "octo/demo",
                    "pullRequest": {
                        "id": "PR1",
                        "number": 7,
                        "baseRefName": "main",
                        "headRefOid": PINS.head,
                        field: connection,
                    },
                }
            }
        }

    def rest(self, endpoint: str) -> dict[str, object]:
        if endpoint == "user":
            return {
                "login": "other" if self.scenario == "wrong-account" else "reviewer"
            }
        if "/pulls/" in endpoint:
            self.metadata_calls += 1
            head = (
                "d" * 40
                if self.scenario == "wrong-head"
                or (self.scenario == "drift" and self.metadata_calls == 2)
                else PINS.head
            )
            return {
                "number": 7,
                "state": "open",
                "draft": False,
                "user": {"login": "author"},
                "base": {
                    "ref": "main",
                    "repo": {
                        "id": 43 if self.scenario == "wrong-repo" else 42,
                        "full_name": "octo/demo",
                    },
                },
                "head": {"sha": head},
            }
        tip = ("e" if self.compares else "f") * 40
        if "/git/ref/" in endpoint:
            return {"ref": "refs/heads/main", "object": {"sha": tip}}
        self.compares += 1
        if self.scenario == "unattested" and self.compares == 2:
            return {}
        result: dict[str, object] = {
            "base_commit": {"sha": tip},
            "merge_base_commit": {"sha": PINS.merge_base},
        }
        if self.scenario in {"large-compare", "oversized-compare"}:
            size = 1600 if self.scenario == "large-compare" else 30_000
            result["files"] = [
                {"filename": f"file{i}.py", "patch": "x" * size} for i in range(300)
            ]
        return result

    def capture(
        self,
        arguments: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: float,
        output_limit: int,
        input: bytes | None = None,
        pass_fds: tuple[int, ...] = (),
    ) -> CompletedProcess[bytes]:
        assert (
            arguments[:2] == ["gh", "api"]
            and arguments[arguments.index("--hostname") + 1] == TARGET.host
        )
        assert not pass_fds and env["GH_TOKEN"] == "testtoken"
        assert 0 < timeout <= 5
        assert output_limit == (256 * 1024 if input is not None else BOUNDED_GET_BYTES)
        if input is not None:
            assert (
                "--input" in arguments
                and "octo" not in arguments
                and "next" not in arguments
            )
        body = json.dumps(self.response(arguments[-1], input, timeout)).encode()
        if len(body) > output_limit:
            raise OutputLimitExceeded
        return CompletedProcess(arguments, 0, b"HTTP/2.0 200 OK\r\n\r\n" + body, b"")


@pytest.mark.parametrize(
    ("scenario", "status", "code"),
    [
        ("pages", "complete", None),
        ("reply-before-root", "complete", None),
        ("empty", "complete", None),
        ("page-error", "partial", "read_error"),
        ("foreign-parent", "partial", "invalid_response"),
        ("foreign-pr", "partial", "invalid_response"),
        ("foreign-node", "partial", "invalid_response"),
        ("oversized-reply", "partial", "limit_reached"),
        ("snapshot-cap", "partial", "limit_reached"),
        ("repeat-cursor", "partial", "invalid_response"),
        ("repeat-id", "partial", "invalid_response"),
        ("oversized", "partial", "limit_reached"),
        ("large-compare", "complete", None),
        ("oversized-compare", "unavailable", "limit_reached"),
        ("deadline", "partial", "limit_reached"),
        ("calls", "partial", "limit_reached"),
        ("drift", "unavailable", "selection_changed"),
        ("wrong-head", "unavailable", "selection_changed"),
        ("wrong-repo", "unavailable", "selection_changed"),
        ("unattested", "unavailable", "invalid_response"),
        ("wrong-host", "unavailable", "selection_changed"),
        ("wrong-account", "unavailable", "selection_changed"),
    ],
)
def test_capture_preserves_accepted_pages_and_attests_selection(
    scenario: str,
    status: str,
    code: str | None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = FakeGitHub(scenario)

    class Reader(GhPullRequestReader):
        @override
        def read_metadata(self, ref: PullRequestRef) -> PullRequest:
            result = super().read_metadata(ref)
            if scenario != "wrong-host":
                return result
            from dataclasses import replace

            return replace(result, ref=replace(result.ref, host="example.org"))

    def capture(
        arguments: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: float,
        output_limit: int,
        input: bytes | None = None,
        pass_fds: tuple[int, ...] = (),
    ) -> CompletedProcess[bytes]:
        assert cwd == tmp_path
        return fake.capture(
            arguments,
            cwd=cwd,
            env=env,
            timeout=timeout,
            output_limit=output_limit,
            input=input,
            pass_fds=pass_fds,
        )

    monkeypatch.setattr(gh_api, "capture_review_process", capture)
    env = {"GH_TOKEN": "testtoken"}
    result = GhDiscussionReader(
        GhApi(env, private_cwd=tmp_path),
        lambda api: Reader(env, api=api),
        expected_account="reviewer",
        monotonic=fake.now,
        utc_now=lambda: NOW,
    ).read(TARGET)
    assert (result.status, result.captured_at) == (status, NOW)
    assert (
        code in {item.code for item in result.omissions}
        if code
        else not result.omissions
    )
    assert len(fake.calls) <= 32 and fake.elapsed <= 30
    if status != "unavailable":
        assert fake.metadata_calls == 2 and fake.compares == 2
    if scenario == "pages":
        assert [item.id for item in result.threads] == ["T1", "T2"]
        assert [item.id for item in result.threads[0].comments] == ["T1reply", "T1root"]
        assert (
            result.threads[0].current_location is not None
            and result.threads[0].current_location.end_line == 21
        )
        original = result.threads[0].root.original_location
        assert (
            original is not None
            and original.path == "old.py"
            and original.end_line == 2
        )
        assert len(result.reviews) == len(result.conversation) == 2
    if scenario == "reply-before-root":
        assert [item.id for item in result.threads[0].comments] == ["T1reply", "T1root"]
    if scenario == "page-error":
        assert len(result.reviews) == 1 and len(result.conversation) == 2
    if scenario in {"foreign-parent", "foreign-pr", "foreign-node", "oversized-reply"}:
        assert [item.id for item in result.threads[0].comments] == ["T1root"]
        assert any(
            item.scope == "replies" and item.thread_id == "T1"
            for item in result.omissions
        )
    if scenario == "snapshot-cap":
        assert len(result.reviews) == 100 and 0 < len(result.conversation) < 100
        assert len(result.model_dump_json().encode()) <= 512 * 1024
    if scenario == "calls":
        assert len(fake.calls) == 32


def test_hosted_capture_pins_account_and_runs_apis_in_private_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub("empty")

    def capture(
        arguments: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: float,
        output_limit: int,
        input: bytes | None = None,
        pass_fds: tuple[int, ...] = (),
    ) -> CompletedProcess[bytes]:
        assert cwd.is_dir() and not (cwd / ".git").exists() and cwd != Path.cwd()
        assert "GH_DEBUG" not in env and "GH_ENTERPRISE_TOKEN" not in env
        if arguments[:3] == ["gh", "auth", "token"]:
            assert (
                arguments[3:] == ["--hostname", TARGET.host] and output_limit == 16384
            )
            if fake.scenario == "setup-error":
                return CompletedProcess(arguments, 1, b"private auth output", b"")
            return CompletedProcess(arguments, 0, b"testtoken\n", b"")
        assert cwd == Path(env["GH_CONFIG_DIR"])
        return fake.capture(
            arguments,
            cwd=cwd,
            env=env,
            timeout=timeout,
            output_limit=output_limit,
            input=input,
            pass_fds=pass_fds,
        )

    monkeypatch.setattr(gh_api, "capture_review_process", capture)
    monkeypatch.setattr(github_credentials, "capture_review_process", capture)
    selection = ReviewSelection(
        PullRequestRef("github.com", "octo", "demo", 7, 42),
        "reviewer",
        "author",
        ReviewedRevision("main", "c" * 40, PINS.merge_base, PINS.head),
    )
    workflows = Workflows(
        tmp_path,
        {"GH_DEBUG": "api", "GH_ENTERPRISE_TOKEN": "unrelated"},
        config_path=tmp_path / "config.toml",
    )

    def select(self: Workflows, ref: PullRequestRef) -> ReviewSelection:
        assert ref.url == selection.ref.url
        return selection

    assert workflows.capture_discussion(selection).status == "complete"
    fake.scenario = "pages"
    monkeypatch.setattr(cli, "_workflows", lambda: workflows)
    monkeypatch.setattr(Workflows, "select", select)
    destination = tmp_path / "discussion.json"
    argv = ["loop-reviewer", "open", TARGET.url, "--discussion-snapshot"]
    monkeypatch.setattr(sys, "argv", [*argv, str(destination)])
    assert cli.main() == 0
    original = destination.read_bytes()
    assert S_IMODE(destination.stat().st_mode) == 0o600
    assert DiscussionSnapshotV1.model_validate_json(original).status == "complete"
    assert b"private text" in original and "private text" not in capsys.readouterr().out
    with pytest.raises(SystemExit) as exists:
        cli.main()
    assert exists.value.code == 1 and destination.read_bytes() == original

    fake.scenario = "wrong-account"
    assert workflows.capture_discussion(selection).status == "unavailable"
    unavailable = tmp_path / "unavailable.json"
    monkeypatch.setattr(sys, "argv", [*argv, str(unavailable)])
    with pytest.raises(SystemExit) as failed_capture:
        cli.main()
    assert failed_capture.value.code == 1 and not unavailable.exists()
    fake.scenario = "setup-error"
    failed = workflows.capture_discussion(selection)
    assert failed.status == "unavailable" and failed.omissions[0].code == "read_error"
    assert "private auth output" not in failed.model_dump_json()
