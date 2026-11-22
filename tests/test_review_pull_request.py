"""Exercise private review generation, resume, retry, and history."""

import subprocess
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Literal, override

import pytest

from loop_reviewer.integrations import pi_reviewer
from loop_reviewer.integrations.local_state.sqlite_reviews import SqliteReviewRunStore
from loop_reviewer.integrations.pi_reviewer import PiReviewer
from loop_reviewer.review.generate import (
    DraftReview,
    FailedReview,
    Reviewer,
    ReviewerError,
    ReviewPullRequest,
    SavedDraftHistoryUnavailable,
    SavedDraftInterrupted,
)
from loop_reviewer.review.model import (
    CompletedReview,
    Finding,
    GeneratedReview,
    ReviewAlreadyCompleted,
    ReviewDraft,
    ReviewIdentity,
    ReviewKey,
    ReviewRun,
    ReviewSelection,
)
from loop_reviewer.review.pull_request import (
    PullRequest,
    PullRequestRef,
    ReviewedRevision,
)
from loop_reviewer.review.selection import GitHubReadError
from loop_reviewer.review.store import ReviewStoreError

_REF = PullRequestRef("github.com", "acme", "repo", 7)
_REVISION = ReviewedRevision("main", "a" * 40, "b" * 40, "c" * 40)
_ORIGINAL = ReviewSelection(_REF, "reviewer", "teammate", _REVISION)
_DRAFT = ReviewDraft(
    "comment",
    "One finding.",
    (Finding("high", "condition", "consequence", None),),
)


class StubReader:
    def __init__(self, selection: ReviewSelection) -> None:
        self.current = selection
        self.fail_recheck = False
        self.interrupt_recheck = False
        self.calls = 0
        self.revision_calls = 0
        self.state: Literal["open", "closed"] = "open"
        self.is_draft = False

    def read_pull_request(self, ref: PullRequestRef) -> tuple[str, PullRequest]:
        self.calls += 1
        if self.calls > 1:
            if self.interrupt_recheck:
                raise KeyboardInterrupt
            if self.fail_recheck:
                raise GitHubReadError("unavailable")
        selection = self.current
        return selection.account, PullRequest(
            selection.ref,
            selection.author,
            self.state,
            self.is_draft,
            selection.revision.target_branch,
            selection.revision.head,
        )

    def read_revision(self, pull_request: PullRequest) -> ReviewedRevision:
        self.revision_calls += 1
        return self.current.revision


class MemoryStore:
    def __init__(self) -> None:
        self.runs: dict[int, ReviewRun] = {}
        self.current: dict[ReviewKey, int] = {}
        self.next_id = 1
        self.history: dict[ReviewIdentity, CompletedReview] = {}
        self.before_start: Callable[[], None] | None = None

    def completed(self, identity: ReviewIdentity) -> CompletedReview | None:
        return self.history.get(identity)

    def latest(self, key: ReviewKey) -> ReviewRun | None:
        run_id = self.current.get(key)
        return None if run_id is None else self.runs[run_id]

    def start(self, selection: ReviewSelection, *, replaces: int | None) -> ReviewRun:
        if self.before_start is not None:
            self.before_start()
        completed = self.completed(selection.identity)
        if completed is not None:
            raise ReviewAlreadyCompleted(completed)
        key = selection.key
        if replaces is None:
            assert key not in self.current
        else:
            assert self.current[key] == replaces
            assert self.runs[replaces].status in {"failed", "abandoned"}
        run = ReviewRun(self.next_id, selection, "running", None, None)
        self.runs[run.id] = run
        self.current[key] = run.id
        self.next_id += 1
        return run

    def save(self, run: ReviewRun) -> None:
        self.runs[run.id] = run


class StubExecution:
    def __init__(self, reviewer: Reviewer, *, contended: bool = False) -> None:
        self.reviewer = reviewer
        self.contended = contended
        self.calls = 0

    @contextmanager
    def acquire(self, key: ReviewKey) -> Generator[Reviewer | None]:
        self.calls += 1
        yield None if self.contended else self.reviewer


class StubReviewer:
    def __init__(
        self,
        *,
        on_review: Callable[[], None] | None = None,
        error: Exception | None = None,
        interrupt: bool = False,
    ) -> None:
        self.on_review = on_review
        self.error = error
        self.interrupt = interrupt
        self.calls = 0

    def review(self, selection: ReviewSelection) -> GeneratedReview:
        self.calls += 1
        if self.interrupt:
            raise KeyboardInterrupt
        if self.error is not None:
            raise self.error
        if self.on_review is not None:
            self.on_review()
        return GeneratedReview(_DRAFT, "provider/model", 0.0234)


def _app(
    reader: StubReader,
    store: MemoryStore,
    reviewer: StubReviewer,
    *,
    contended: bool = False,
) -> ReviewPullRequest:
    return ReviewPullRequest(
        reader, store, StubExecution(reviewer, contended=contended)
    )


def _saved(store: MemoryStore, selection: ReviewSelection, status: str) -> ReviewRun:
    run = store.start(selection, replaces=None)
    if status == "failed":
        run = run.fail("The reviewer failed.", "failed")
    elif status == "abandoned":
        run = run.abandon()
    else:
        run = run.finish(GeneratedReview(_DRAFT, "provider/model", None))
    store.save(run)
    return run


@pytest.mark.parametrize("case", ["new-head", "retargeted", "closed", "draft", "retry"])
def test_completed_pr_routes_to_history_before_revision_selection(case: str) -> None:
    reader = StubReader(_ORIGINAL)
    if case == "new-head":
        _drift("head", reader)
    elif case == "retargeted":
        _drift("target-branch", reader)
    elif case == "closed":
        reader.state = "closed"
    elif case == "draft":
        reader.is_draft = True
    store = MemoryStore()
    run = _saved(store, _ORIGINAL, "draft_ready")
    completion = run.complete_locally()
    store.history[run.selection.identity] = completion
    reviewer = StubReviewer()
    execution = StubExecution(reviewer)
    outcome = ReviewPullRequest(reader, store, execution).run(
        _REF, retry=case == "retry"
    )
    assert outcome.status == "completed"
    assert outcome.completion == completion
    assert reader.calls == 1
    assert reader.revision_calls == execution.calls == reviewer.calls == 0


@pytest.mark.parametrize(
    ("ref", "account", "status"),
    [
        pytest.param(
            PullRequestRef("GITHUB.com", "ACME", "Repo", 7),
            "REVIEWER",
            "completed",
            id="canonical-casing",
        ),
        pytest.param(
            replace(_REF, host="other.example.com"),
            "reviewer",
            "generated",
            id="different-host",
        ),
        pytest.param(
            replace(_REF, owner="other"), "reviewer", "generated", id="different-owner"
        ),
        pytest.param(
            replace(_REF, repo="other"), "reviewer", "generated", id="different-repo"
        ),
        pytest.param(
            replace(_REF, number=8), "reviewer", "generated", id="different-number"
        ),
        pytest.param(_REF, "another", "generated", id="different-account"),
    ],
)
def test_completion_lookup_is_scoped_to_canonical_pr_and_account(
    ref: PullRequestRef, account: str, status: str
) -> None:
    reader = StubReader(replace(_ORIGINAL, ref=ref, account=account))
    store = MemoryStore()
    run = _saved(store, _ORIGINAL, "draft_ready")
    store.history[run.selection.identity] = run.complete_locally()
    alias = replace(_REF, owner="alias", repo="renamed")
    outcome = _app(reader, store, StubReviewer()).run(alias)
    assert outcome.status == status
    if outcome.status == "completed":
        assert outcome.completion == run.complete_locally()


@pytest.mark.parametrize("when", ["during-generation", "at-admission"])
def test_concurrent_completion_returns_history(when: str) -> None:
    reader = StubReader(_ORIGINAL)
    _drift("head", reader)
    store = MemoryStore()
    run = _saved(store, _ORIGINAL, "draft_ready")
    completion = run.complete_locally()

    def finish_older_draft() -> None:
        store.history[run.selection.identity] = completion

    reviewer = StubReviewer(
        on_review=finish_older_draft if when == "during-generation" else None
    )
    if when == "at-admission":
        store.before_start = finish_older_draft
    outcome = _app(reader, store, reviewer).run(_REF)

    assert outcome.status == "completed"
    assert outcome.completion == completion
    assert store.completed(run.selection.identity) == completion
    newer_run = store.latest(reader.current.key)
    if when == "during-generation":
        assert reviewer.calls == 1
        assert newer_run is not None and newer_run.status == "draft_ready"
    else:
        assert reviewer.calls == 0
        assert newer_run is None


def test_review_resumes_saved_draft_without_reviewer() -> None:
    reader = StubReader(_ORIGINAL)
    store = MemoryStore()
    _saved(store, _ORIGINAL, "draft_ready")
    reviewer = StubReviewer(error=AssertionError("must not be called"))

    outcome = _app(reader, store, reviewer).run(_REF)

    assert outcome.status == "resumed"
    assert outcome.run.draft == _DRAFT
    assert reviewer.calls == 0


@pytest.mark.parametrize("state", ["failed", "abandoned", "missing"])
def test_review_requires_explicit_retry(state: str) -> None:
    reader = StubReader(_ORIGINAL)
    store = MemoryStore()
    reviewer = StubReviewer()
    app = _app(reader, store, reviewer)
    old = None if state == "missing" else _saved(store, _ORIGINAL, state)

    if state == "missing":
        assert app.run(_REF, retry=True).status == "no_retryable_run"
        assert reviewer.calls == 0
        assert app.run(_REF).status == "generated"
        assert reviewer.calls == 1
        return

    outcome = app.run(_REF)
    assert outcome.status == state
    assert reviewer.calls == 0
    retried = app.run(_REF, retry=True)
    assert retried.status == "generated"
    assert reviewer.calls == 1
    assert old is not None
    assert retried.run.id != old.id


@pytest.mark.parametrize("retry", [False, True], ids=["no-retry", "explicit-retry"])
def test_review_abandons_unlocked_running_attempt(retry: bool) -> None:
    reader = StubReader(_ORIGINAL)
    store = MemoryStore()
    run = store.start(_ORIGINAL, replaces=None)
    reviewer = StubReviewer()

    outcome = _app(reader, store, reviewer).run(_REF, retry=retry)

    assert store.runs[run.id].status == "abandoned"
    current = store.latest(_ORIGINAL.key)
    assert current is not None
    if retry:
        assert outcome.status == "generated"
        assert reviewer.calls == 1
        assert outcome.run.id == current.id != run.id
    else:
        assert outcome.status == "abandoned"
        assert reviewer.calls == 0
        assert current.id == run.id


def test_review_reports_in_progress_on_contention() -> None:
    reader = StubReader(_ORIGINAL)
    store = MemoryStore()
    reviewer = StubReviewer()

    outcome = _app(reader, store, reviewer, contended=True).run(_REF)

    assert outcome.status == "in_progress"
    assert reviewer.calls == 0


def _drift(case: str, reader: StubReader) -> None:
    if case == "recheck-failure":
        reader.fail_recheck = True
        return
    if case == "account":
        reader.current = replace(reader.current, account="other")
    elif case == "base-tip":
        reader.current = replace(
            reader.current,
            revision=replace(reader.current.revision, base_tip="d" * 40),
        )
    else:
        reader.current = replace(
            reader.current,
            revision=replace(
                reader.current.revision,
                **{case.replace("-", "_"): "d" * 40}
                if case != "target-branch"
                else {"target_branch": "release"},
            ),
        )


@pytest.mark.parametrize(
    ("case", "changes"),
    [
        pytest.param("target-branch", ("target branch",), id="retarget"),
        pytest.param("merge-base", ("merge base",), id="changed-merge-base"),
        pytest.param("head", ("head",), id="changed-head"),
        pytest.param("base-tip", (), id="harmless-base-advance"),
        pytest.param("account", ("authenticated account",), id="account-switch"),
        pytest.param("recheck-failure", "unavailable", id="failed-recheck"),
    ],
)
def test_review_preserves_original_revision_on_drift(
    case: str, changes: tuple[str, ...] | str
) -> None:
    reader = StubReader(_ORIGINAL)
    store = MemoryStore()
    reviewer = StubReviewer(on_review=lambda: _drift(case, reader))

    outcome = _app(reader, store, reviewer).run(_REF)

    assert outcome.status == "generated"
    assert outcome.run.selection == _ORIGINAL
    assert outcome.run.draft == _DRAFT
    assert outcome.changes == changes
    stored = store.latest(_ORIGINAL.key)
    assert stored is not None
    assert stored.draft == _DRAFT


def test_review_abandons_interrupted_attempt() -> None:
    reader = StubReader(_ORIGINAL)
    store = MemoryStore()
    reviewer = StubReviewer(interrupt=True)

    with pytest.raises(KeyboardInterrupt):
        _app(reader, store, reviewer).run(_REF)

    stored = store.latest(_ORIGINAL.key)
    assert stored is not None
    assert stored.status == "abandoned"


def test_review_reports_saved_run_when_freshness_check_is_interrupted() -> None:
    reader = StubReader(_ORIGINAL)
    reader.interrupt_recheck = True
    store = MemoryStore()
    with pytest.raises(SavedDraftInterrupted) as interrupted:
        _app(reader, store, StubReviewer()).run(_REF)
    saved = store.latest(_ORIGINAL.key)
    assert saved is not None
    assert saved.id == interrupted.value.run_id
    assert saved.status == "draft_ready"
    assert saved.draft == _DRAFT


@pytest.mark.parametrize("failed_read", [1, 2], ids=["before-recheck", "after-recheck"])
def test_review_reports_saved_run_when_history_check_fails(
    tmp_path: Path, failed_read: int
) -> None:
    class FlakyHistory(SqliteReviewRunStore):
        reads_after_save = 0
        saved_id: int | None = None

        @override
        def save(self, run: ReviewRun) -> None:
            super().save(run)
            if run.status == "draft_ready":
                self.saved_id = run.id

        @override
        def completed(self, identity: ReviewIdentity) -> CompletedReview | None:
            if self.saved_id is not None:
                self.reads_after_save += 1
                if self.reads_after_save == failed_read:
                    raise ReviewStoreError("Could not read the local review state.")
            return super().completed(identity)

    path = tmp_path / "reviews.sqlite3"
    reader = StubReader(_ORIGINAL)
    store = FlakyHistory(path)
    reviewer = StubReviewer(on_review=lambda: _drift("head", reader))
    app = ReviewPullRequest(reader, store, StubExecution(reviewer))

    with pytest.raises(SavedDraftHistoryUnavailable, match="Could not read") as error:
        app.run(_REF)

    saved = SqliteReviewRunStore(path).get_draft(error.value.run_id)
    assert saved is not None
    assert saved.draft == _DRAFT
    assert saved.selection == _ORIGINAL != reader.current


def test_malformed_pi_output_is_persisted_as_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def which(name: str, *, path: str | None = None) -> str:
        return "/fake/pi"

    def capture(
        args: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            args, 0, b'{"verdict": [], "summary": "x", "findings": []}'
        )

    def read_patch(selection: ReviewSelection) -> str:
        return ""

    monkeypatch.setattr(pi_reviewer.shutil, "which", which)
    monkeypatch.setattr(pi_reviewer, "capture_review_process", capture)
    reviewer = PiReviewer(read_patch, "provider/model", {})
    path = tmp_path / "reviews.sqlite3"
    app = ReviewPullRequest(
        StubReader(_ORIGINAL), SqliteReviewRunStore(path), StubExecution(reviewer)
    )
    assert app.run(_REF).status == "failed"
    stored = SqliteReviewRunStore(path).latest(_ORIGINAL.key)
    assert stored is not None and stored.status == "failed"
    assert stored.failure == "Pi returned an invalid review. Try again."


def test_review_records_reviewer_failure(tmp_path: Path) -> None:
    reader = StubReader(_ORIGINAL)
    path = tmp_path / "reviews.sqlite3"
    store = SqliteReviewRunStore(path)
    reviewer = StubReviewer(
        error=ReviewerError("Binary changes are unsupported.", "input")
    )
    outcome = ReviewPullRequest(reader, store, StubExecution(reviewer)).run(_REF)
    assert outcome == FailedReview("Binary changes are unsupported.", "input")
    failed = store.latest(_ORIGINAL.key)
    assert failed is not None
    assert failed.generation_model is None
    assert failed.estimated_cost_usd is None

    reopened = SqliteReviewRunStore(path)
    assert (
        ReviewPullRequest(reader, reopened, StubExecution(reviewer)).run(_REF)
        == outcome
    )
    assert reviewer.calls == 1
    reviewer.error = None
    retried = ReviewPullRequest(reader, reopened, StubExecution(reviewer)).run(
        _REF, retry=True
    )
    assert isinstance(retried, DraftReview)
    assert retried.run.id != failed.id
    assert retried.run.generation_model == "provider/model"
    assert retried.run.estimated_cost_usd == 0.0234
    assert reviewer.calls == 2
