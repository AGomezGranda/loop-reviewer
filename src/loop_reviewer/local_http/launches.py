"""Dispatch browser review launches and retain compact session outcomes."""

from collections import deque
from collections.abc import Callable, Hashable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from threading import Lock
from typing import Literal, Protocol

from loop_reviewer.local_http.errors import api_error
from loop_reviewer.local_http.models import ApiError, ErrorResponse, ResponseModel
from loop_reviewer.review.generate import (
    DraftReview,
    FinishedReview,
    RetryTarget,
    ReviewOutcome,
    SavedDraftHistoryUnavailable,
    SavedDraftInterrupted,
)
from loop_reviewer.review.model import ReviewIdentity, ReviewSelection, SelectionChange
from loop_reviewer.review.pull_request import PullRequestRef

type ObserveSelection = Callable[[ReviewSelection], None]
type LaunchWork = Callable[[ObserveSelection], ReviewOutcome]
type RevisionChanges = tuple[SelectionChange, ...] | Literal["unavailable"]
type LaunchSource = Literal["url", "inbox", "retry"]
type LaunchKey = tuple[LaunchSource, Hashable]


class ReviewRequest(Protocol):
    def __call__(
        self,
        ref: PullRequestRef,
        *,
        retry: bool = False,
        expected_identity: ReviewIdentity | None = None,
        observe_selection: ObserveSelection | None = None,
        retry_target: RetryTarget | None = None,
    ) -> ReviewOutcome: ...


class LaunchReceipt(ResponseModel):
    launch_id: str


@dataclass(frozen=True)
class DraftResult:
    run_id: int
    changes: RevisionChanges


@dataclass(frozen=True)
class CompletionResult:
    run_id: int


@dataclass(frozen=True)
class Launch:
    receipt: LaunchReceipt
    url: str
    source: LaunchSource
    status: Literal["queued", "running", "settled"] = "queued"
    selection: ReviewSelection | None = None
    retry_run_id: int | None = None
    result: DraftResult | CompletionResult | ErrorResponse | None = None


class ReviewLaunches:
    def __init__(self, new_id: Callable[[], str]) -> None:
        self._new_id = new_id
        self._lock = Lock()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="review")
        self._pending: dict[LaunchKey, Launch] = {}
        self._waiting: deque[tuple[LaunchKey, LaunchWork]] = deque()
        self._finished: deque[Launch] = deque(maxlen=50)
        self._running = 0
        self._closed = False

    def pending(self, key: LaunchKey) -> LaunchReceipt | None:
        with self._lock:
            launch = self._pending.get(key)
            return None if launch is None else launch.receipt

    def submit(
        self,
        key: LaunchKey,
        run: LaunchWork,
        *,
        url: str,
        selection: ReviewSelection | None = None,
        retry_run_id: int | None = None,
    ) -> LaunchReceipt:
        with self._lock:
            if self._closed:
                raise ApiError(
                    409, "review_unavailable", "The workspace is shutting down."
                )
            if key in self._pending:
                return self._pending[key].receipt
            if len(self._waiting) >= 32:
                raise ApiError(
                    409,
                    "review_queue_full",
                    "The review queue is full. Try again after a review finishes.",
                )
            receipt = LaunchReceipt(launch_id=self._new_id())
            self._pending[key] = Launch(
                receipt, url, key[0], selection=selection, retry_run_id=retry_run_id
            )
            self._waiting.append((key, run))
            self._dispatch()
            return receipt

    def snapshot(self) -> tuple[Launch, ...]:
        with self._lock:
            return (*self._pending.values(), *reversed(self._finished))

    def stop(self) -> None:
        with self._lock:
            self._closed = True
            while self._waiting:
                key, _ = self._waiting.popleft()
                del self._pending[key]

    def close(self) -> None:
        self.stop()
        self._executor.shutdown()

    def _dispatch(self) -> None:
        while self._running < 2 and self._waiting:
            key, run = self._waiting.popleft()
            self._pending[key] = replace(self._pending[key], status="running")
            self._running += 1
            self._executor.submit(self._run, key, run)

    def _run(self, key: LaunchKey, run: LaunchWork) -> None:
        def observe(selection: ReviewSelection) -> None:
            with self._lock:
                self._pending[key] = replace(self._pending[key], selection=selection)

        result = None
        try:
            outcome = run(observe)
            if isinstance(outcome, DraftReview):
                result = DraftResult(outcome.run.id, outcome.changes)
            elif isinstance(outcome, FinishedReview):
                result = CompletionResult(outcome.completion.source.run_id)
            elif outcome.status == "no_retryable_run":
                result = ErrorResponse(
                    code="review_unavailable",
                    message="There is no saved attempt to retry.",
                )
        except (SavedDraftInterrupted, SavedDraftHistoryUnavailable) as error:
            result = DraftResult(error.run_id, "unavailable")
        except KeyboardInterrupt:
            result = ErrorResponse(
                code="review_unavailable", message="The review was interrupted."
            )
        except Exception as error:  # noqa: BLE001 - The worker has no HTTP request to report failures through.
            result = api_error(error).body
        finally:
            with self._lock:
                launch = self._pending.pop(key)
                self._running -= 1
                if result is not None:
                    self._finished.append(
                        replace(launch, status="settled", result=result)
                    )
                self._dispatch()
