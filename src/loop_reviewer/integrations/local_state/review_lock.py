"""Hold a per-comparison process lock while the reviewer runs."""

import hashlib
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

from loop_reviewer.integrations.local_state.keys import comparison_key
from loop_reviewer.integrations.local_state.posix_lock import (
    LocalLockError,
    private_lock,
)
from loop_reviewer.review.generate import Reviewer, ReviewerError
from loop_reviewer.review.model import ReviewKey


def _lock_name(key: ReviewKey) -> str:
    name = hashlib.sha256(comparison_key(key).encode("utf-8")).hexdigest()
    return f"{name}.lock"


def review_lock_held(lock_dir: Path, key: ReviewKey) -> bool | None:
    try:
        with private_lock(lock_dir, _lock_name(key), shared=True) as descriptor:
            return descriptor is None
    except LocalLockError:
        return None


class LockedReviewExecution:
    def __init__(
        self, lock_dir: Path, reviewer_factory: Callable[[int], Reviewer]
    ) -> None:
        self._lock_dir = lock_dir
        self._reviewer_factory = reviewer_factory

    @contextmanager
    def acquire(self, key: ReviewKey) -> Generator[Reviewer | None]:
        try:
            with private_lock(self._lock_dir, _lock_name(key)) as descriptor:
                yield None if descriptor is None else self._reviewer_factory(descriptor)
        except LocalLockError as error:
            guidance = {
                "platform": "Private review drafts require macOS or Linux.",
                "directory": "Could not prepare the local review state directory. Check permissions and try again.",
                "open": "Could not open the local review lock. Check permissions on the state directory.",
                "mode": "Could not set private permissions on the local review lock.",
                "lock": "Could not lock the local review state. Check permissions on the state directory.",
            }
            raise ReviewerError(guidance[error.reason], "setup") from None
