"""Serialize publication across runs and hold the lock through gh submission."""

import hashlib
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

from loop_reviewer.integrations.local_state.keys import identity_key
from loop_reviewer.integrations.local_state.posix_lock import (
    LocalLockError,
    private_lock,
)
from loop_reviewer.review.model import ReviewIdentity
from loop_reviewer.review.publication import (
    PublicationSession,
    PublicationSessionFactory,
)
from loop_reviewer.review.publication_model import PublicationError


class LockedPublication:
    def __init__(
        self,
        directory: Path,
        bind: Callable[[int], PublicationSessionFactory[PublicationSession]],
    ) -> None:
        self._directory = directory
        self._bind = bind

    @contextmanager
    def acquire(
        self, identity: ReviewIdentity
    ) -> Generator[PublicationSessionFactory[PublicationSession] | None]:
        name = hashlib.sha256(identity_key(identity).encode("utf-8")).hexdigest()
        try:
            with private_lock(self._directory, f"{name}.lock") as descriptor:
                yield None if descriptor is None else self._bind(descriptor)
        except LocalLockError as error:
            if error.reason == "platform":
                raise PublicationError(
                    "Publishing reviews requires macOS or Linux."
                ) from None
            raise PublicationError(
                "Could not acquire the publication lock. Check permissions on the state directory."
            ) from None
