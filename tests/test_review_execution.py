"""Verify review lock behavior across processes and interruptions."""

import errno
import os
import signal
import subprocess
import sys
import time
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest

from loop_reviewer.integrations.local_state.review_lock import (
    LockedReviewExecution,
    review_lock_held,
)
from loop_reviewer.integrations.local_state.sqlite_reviews import SqliteReviewRunStore
from loop_reviewer.review.active import ActiveReviews
from loop_reviewer.review.generate import ReviewerError
from loop_reviewer.review.model import GeneratedReview, ReviewDraft, ReviewSelection
from loop_reviewer.review.pull_request import PullRequestRef, ReviewedRevision

_WORKER = """
import os
import subprocess
import sys
import time
from pathlib import Path

from loop_reviewer.integrations.git_comparison import GitComparison
from loop_reviewer.integrations.local_state.review_lock import LockedReviewExecution
from loop_reviewer.integrations.local_state.sqlite_reviews import SqliteReviewRunStore
from loop_reviewer.review.generate import ReviewPullRequest
from loop_reviewer.review.pull_request import (
    PullRequest,
    PullRequestRef,
    ReviewedRevision,
)
from loop_reviewer.review.model import GeneratedReview, ReviewDraft

db, locks, mode, number, started, release, marker, retry = sys.argv[1:]
number = int(number)
ref = PullRequestRef("github.com", "acme", "repo", number)
revision = ReviewedRevision("main", "a" * 40, "b" * 40, "c" * 40)


class Reader:
    def read_pull_request(self, requested):
        return "reviewer", PullRequest(
            requested, "teammate", "open", False,
            revision.target_branch, revision.head,
        )

    def read_revision(self, pull_request):
        return revision


class FakeReviewer:
    def __init__(self, descriptor):
        self.descriptor = descriptor

    def review(self, selection):
        with open(marker, "a") as handle:
            handle.write(f"{selection.ref.number}\\n")
        if mode == "block-git":
            root = str(Path(__file__).parent)
            environment = dict(os.environ, PATH=root)
            with GitComparison(environment, inherited_fd=self.descriptor).open_verified_repository(selection):
                pass
        elif mode == "block-reviewer":
            subprocess.Popen(
                [sys.executable, str(Path(__file__).with_name("child.py")),
                 started, release],
                pass_fds=(self.descriptor,),
            )
            while not Path(release).exists():
                time.sleep(0.05)
        return GeneratedReview(ReviewDraft("comment", "summary", ()), "provider/model", None)


application = ReviewPullRequest(
    Reader(),
    SqliteReviewRunStore(Path(db)),
    LockedReviewExecution(Path(locks), FakeReviewer),
)
print(application.run(ref, retry=retry == "retry").status)
"""

_PUBLISH_WORKER = """
import os
import sys
from pathlib import Path

from loop_reviewer.integrations.github_publication import open_gh_publication
from loop_reviewer.integrations.local_state.publication_lock import LockedPublication
from loop_reviewer.integrations.local_state.sqlite_reviews import SqliteReviewRunStore
from loop_reviewer.review.model import GeneratedReview, ReviewDraft, ReviewSelection
from loop_reviewer.review.publication import PublishReview
from loop_reviewer.review.publication_model import PublicationError, PublicationId, build_preview
from loop_reviewer.review.pull_request import PullRequestRef, ReviewedRevision

root = Path(sys.argv[1])
store = SqliteReviewRunStore(root / "state" / "reviews.sqlite3")
environment = dict(os.environ, PATH=str(root), FAKE_GH_ROOT=str(root))
id = PublicationId("75f718d3-9633-42ad-9097-22f320d068a8")
app = PublishReview(store, LockedPublication(root / "publication-locks", lambda fd: lambda host: open_gh_publication(environment, host, inherited_fd=fd)))
if sys.argv[2] == "send":
    selection = ReviewSelection(PullRequestRef("github.com", "acme", "repo", 7, 123), "reviewer", "author", ReviewedRevision("main", "a" * 40, "b" * 40, "c" * 40))
    run = store.start(selection, replaces=None).finish(
        GeneratedReview(ReviewDraft("comment", "summary", ()), "provider/model", None)
    )
    store.save(run)
    store.save_preview(build_preview(id, run.saved_draft(), selection, 1, "comment", frozenset()))
    print(app.publish(id).status)
else:
    try:
        receipt = app.recover(id).receipt
        print(f"published {receipt.review_id}")
    except PublicationError as error:
        print(error)
"""

_FAKE_GH = """
import json
import os
import sys
import time
from pathlib import Path

root = Path(os.environ["FAKE_GH_ROOT"])
if sys.argv[1] == "auth":
    print("fixture-token")
    sys.exit(0)
assert os.environ["GH_TOKEN"] == "fixture-token"
endpoint = sys.argv[-1]
destination = root / "remote.json"
if "POST" in sys.argv:
    payload = json.load(sys.stdin)
    with (root / "posts").open("a") as file:
        file.write("POST\\n")
    review = {"id": 45, "user": {"id": 1, "login": "reviewer"}, "commit_id": payload["commit_id"], "body": payload["body"], "state": "COMMENTED"}
    (root / "publication-started").write_text(str(os.getpid()))
    while not (root / "publication-release").exists():
        time.sleep(.03)
    destination.write_text(json.dumps(review))
    sys.exit(1) # The review was saved but no response reached the caller.
elif endpoint == "user":
    data = {"id": 1, "login": "reviewer"}
elif endpoint.endswith("pulls/7"):
    data = {"number": 7, "state": "open", "draft": False, "user": {"login": "author"}, "base": {"ref": "main", "repo": {"id": 123, "full_name": "acme/repo"}}, "head": {"sha": "c" * 40}}
elif "/git/ref/heads/" in endpoint:
    data = {"ref": "refs/heads/main", "object": {"sha": "a" * 40}}
elif "/compare/" in endpoint:
    data = {"base_commit": {"sha": "a" * 40}, "merge_base_commit": {"sha": "b" * 40}}
elif "/reviews?" in endpoint:
    data = [json.loads(destination.read_text())] if destination.exists() else []
else:
    sys.exit(1)
print("HTTP/2.0 200 OK\\n\\n" + json.dumps(data))
"""


def test_surviving_gh_excludes_recovery_after_parent_killed(tmp_path: Path) -> None:
    worker = tmp_path / "publisher.py"
    worker.write_text(_PUBLISH_WORKER)
    gh = tmp_path / "gh"
    gh.write_text(f"#!{sys.executable}\n{_FAKE_GH}")
    gh.chmod(0o700)
    started, release = (
        tmp_path / f"publication-{name}" for name in ("started", "release")
    )
    arguments = [sys.executable, str(worker), str(tmp_path)]
    owner = subprocess.Popen(
        [*arguments, "send"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )

    def recover() -> str:
        result = subprocess.run(
            [*arguments, "recover"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        return result.stdout.strip()

    try:
        _wait_for(started)
        owner.kill()
        owner.wait(timeout=10)
        assert "already in progress" in recover()
        release.touch()
        _wait_for(tmp_path / "remote.json")
        deadline = time.monotonic() + 10
        while (
            "already in progress" in (result := recover())
            and time.monotonic() < deadline
        ):
            time.sleep(0.03)
        assert result == "published 45"
        assert recover() == "published 45"
        assert (tmp_path / "posts").read_text() == "POST\n"
    finally:
        _release(owner, release)
        if owner.stderr is not None:
            owner.stderr.close()


_CHILD = """
import os
import sys
import time
from pathlib import Path

started, release = sys.argv[1:]
Path(started).write_text(str(os.getpid()))
while not Path(release).exists():
    time.sleep(0.05)
"""


def _worker(tmp_path: Path) -> Path:
    worker = tmp_path / "worker.py"
    worker.write_text(_WORKER)
    (tmp_path / "child.py").write_text(_CHILD)
    git = tmp_path / "git"
    arguments = [str(tmp_path / name) for name in ("git", "started", "release")]
    git.write_text(
        f"#!{sys.executable}\nimport sys\nsys.argv = {arguments!r}\n{_CHILD}"
    )
    git.chmod(0o700)
    return worker


def _start(
    worker: Path,
    db: Path,
    locks: Path,
    mode: str,
    number: int,
    started: Path,
    release: Path,
    marker: Path,
    retry: str,
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            str(worker),
            str(db),
            str(locks),
            mode,
            str(number),
            str(started),
            str(release),
            str(marker),
            retry,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(os.environ, TMPDIR=str(worker.parent)),
    )


def _finish(process: subprocess.Popen[str]) -> str:
    stdout, stderr = process.communicate(timeout=20)
    assert process.returncode == 0, stderr
    return stdout.strip()


def _wait_for(path: Path, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"Timed out waiting for {path}")


def _wait_for_exit(pid: int, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    raise AssertionError(f"Process {pid} did not exit")


def _release(owner: subprocess.Popen[str], release: Path) -> None:
    release.write_text("go")
    if owner.poll() is None:
        try:
            owner.wait(timeout=10)
        except subprocess.TimeoutExpired:
            owner.kill()
            owner.wait()


@pytest.mark.parametrize("fails", [False, True], ids=["success", "review-error"])
def test_lock_close_error_preserves_review_result(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, fails: bool
) -> None:
    class StubReviewer:
        def review(self, selection: ReviewSelection) -> GeneratedReview:
            if fails:
                raise ReviewerError("review failed", "failed")
            return GeneratedReview(
                ReviewDraft("comment", "summary", ()), "provider/model", None
            )

    selection = ReviewSelection(
        PullRequestRef("github.com", "acme", "repo", 7),
        "reviewer",
        "teammate",
        ReviewedRevision("main", "a" * 40, "b" * 40, "c" * 40),
    )
    execution = LockedReviewExecution(tmp_path / "locks", lambda _: StubReviewer())
    close = os.close

    def interrupted_close(descriptor: int) -> None:
        close(descriptor)
        raise InterruptedError(errno.EINTR, "simulated lock close failure")

    expected = (
        pytest.raises(ReviewerError, match="review failed") if fails else nullcontext()
    )
    with (
        expected,
        patch(
            "loop_reviewer.integrations.local_state.posix_lock.os.close",
            interrupted_close,
        ),
        execution.acquire(selection.key) as reviewer,
    ):
        assert reviewer is not None
        assert reviewer.review(selection).draft.summary == "summary"
    assert "Could not close the local review lock." in caplog.text


def test_concurrent_review_starts_invoke_reviewer_once(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    db = tmp_path / "state" / "reviews.sqlite3"
    locks = tmp_path / "state" / "locks"
    started = tmp_path / "started"
    release = tmp_path / "release"
    marker = tmp_path / "marker"
    owner = _start(
        worker, db, locks, "block-reviewer", 7, started, release, marker, "no"
    )
    try:
        _wait_for(started)
        second = _start(worker, db, locks, "quick", 7, started, release, marker, "no")
        assert _finish(second) == "in_progress"
        other = _start(worker, db, locks, "quick", 8, started, release, marker, "no")
        assert _finish(other) == "generated"
        release.write_text("go")
        assert _finish(owner) == "generated"
    finally:
        _release(owner, release)

    assert sorted(marker.read_text().splitlines()) == ["7", "8"]


def test_active_review_status_follows_comparison_lock(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    db = tmp_path / "state" / "reviews.sqlite3"
    locks = tmp_path / "state" / "locks"
    started, release, marker = (
        tmp_path / name for name in ("started", "release", "marker")
    )
    store = SqliteReviewRunStore(db)
    active = ActiveReviews(store, lambda key: review_lock_held(locks, key))
    owner = _start(
        worker, db, locks, "block-reviewer", 7, started, release, marker, "no"
    )
    try:
        _wait_for(started)
        (live,) = active.list()
        assert live.status == "running"
        assert (
            ActiveReviews(store, lambda _: None).list()[0].status
            == "status_unavailable"
        )
        owner.kill()
        owner.wait(timeout=10)
        assert active.list()[0].status == "running"
        release.touch()
        _wait_for_exit(int(started.read_text()))
        (interrupted,) = active.list()
        assert interrupted.status == "interrupted"
        assert interrupted.run == live.run
        assert store.unfinished_by_id(live.run.id) == live.run
    finally:
        _release(owner, release)


@pytest.mark.parametrize(
    "mode", ["block-git", "block-reviewer"], ids=["git", "reviewer"]
)
def test_killed_owner_is_not_retried_while_child_lives(
    tmp_path: Path,
    mode: str,
) -> None:
    worker = _worker(tmp_path)
    db = tmp_path / "state" / "reviews.sqlite3"
    locks = tmp_path / "state" / "locks"
    started = tmp_path / "started"
    release = tmp_path / "release"
    marker = tmp_path / "marker"
    owner = _start(worker, db, locks, mode, 7, started, release, marker, "no")
    child = None
    try:
        _wait_for(started)
        child = int(started.read_text())
        owner.kill()
        owner.wait()

        while_child_lives = _start(
            worker, db, locks, "quick", 7, started, release, marker, "retry"
        )
        assert _finish(while_child_lives) == "in_progress"

        release.write_text("go")
        _wait_for_exit(child)

        abandoned = _start(
            worker, db, locks, "quick", 7, started, release, marker, "no"
        )
        assert _finish(abandoned) == "abandoned"

        retried = _start(
            worker, db, locks, "quick", 7, started, release, marker, "retry"
        )
        assert _finish(retried) == "generated"
    finally:
        _release(owner, release)
        if child is not None:
            try:
                _wait_for_exit(child, timeout=5)
            except AssertionError:
                try:
                    os.kill(child, signal.SIGKILL)
                except ProcessLookupError:
                    pass
