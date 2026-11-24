"""Create and clean up private scratch directories for hosted review."""

import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from loop_reviewer.integrations.review_process import remove_review_scratch
from loop_reviewer.review.generate import ReviewerError


@contextmanager
def review_scratch(purpose: str) -> Generator[Path]:
    try:
        path = Path(tempfile.mkdtemp(prefix=f"loop-reviewer-{purpose}-"))
    except OSError as error:
        raise ReviewerError(
            f"Could not create temporary space for {purpose}. Check permissions and free space.",
            "failed",
        ) from error
    try:
        yield path
    finally:
        remove_review_scratch(path, purpose)
