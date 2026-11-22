"""Private nonblocking process locks, shareable with child processes."""

import os
from collections.abc import Generator
from contextlib import contextmanager
from logging import getLogger
from pathlib import Path
from typing import Literal


class LocalLockError(Exception):
    def __init__(
        self, reason: Literal["platform", "directory", "open", "mode", "lock"]
    ) -> None:
        super().__init__(reason)
        self.reason = reason


@contextmanager
def private_lock(
    directory: Path, name: str, *, shared: bool = False, blocking: bool = False
) -> Generator[int | None]:
    try:
        import fcntl
    except ImportError:
        raise LocalLockError("platform") from None
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    except OSError:
        raise LocalLockError("directory") from None
    try:
        descriptor = os.open(directory / name, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        raise LocalLockError("open") from None
    try:
        try:
            os.fchmod(descriptor, 0o600)
        except OSError:
            raise LocalLockError("mode") from None
        try:
            mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            fcntl.flock(descriptor, mode if blocking else mode | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        except OSError:
            raise LocalLockError("lock") from None
        yield descriptor
    finally:
        # Closing without LOCK_UN preserves exclusion while a child holds the descriptor.
        try:
            os.close(descriptor)
        except OSError:
            getLogger(__name__).warning("Could not close the local review lock.")
