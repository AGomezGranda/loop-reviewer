"""Run actions against cached inbox rows without depending on an interface."""

from collections.abc import Callable

from loop_reviewer.inbox.store import InboxRowTarget, InboxStore
from loop_reviewer.review.model import ReviewIdentity
from loop_reviewer.review.pull_request import PullRequestRef


class InboxRowNotFound(ValueError):
    pass


class InboxRowChanged(ValueError):
    pass


class InboxActions[ReviewResult]:
    def __init__(
        self,
        store: InboxStore,
        review: Callable[[PullRequestRef, ReviewIdentity], ReviewResult],
        *,
        refresh_settings: Callable[[], None] | None = None,
    ) -> None:
        self._store = store
        self._review = review
        self._refresh_settings = refresh_settings

    def open(
        self, ref: PullRequestRef, *, expected_target: InboxRowTarget | None = None
    ) -> tuple[PullRequestRef, ReviewResult]:
        target = self._target(ref, expected_target)
        return target.ref, self._review(target.ref, target.identity)

    def defer(
        self, ref: PullRequestRef, *, expected_target: InboxRowTarget | None = None
    ) -> PullRequestRef:
        return self._set_deferred(ref, expected_target, deferred=True)

    def restore(
        self, ref: PullRequestRef, *, expected_target: InboxRowTarget | None = None
    ) -> PullRequestRef:
        return self._set_deferred(ref, expected_target, deferred=False)

    def _set_deferred(
        self,
        ref: PullRequestRef,
        expected_target: InboxRowTarget | None,
        *,
        deferred: bool,
    ) -> PullRequestRef:
        target = self._target(ref, expected_target)
        if not self._store.set_deferred(target, deferred):
            raise InboxRowChanged("The inbox row changed.")
        return target.ref

    def _target(
        self, ref: PullRequestRef, expected_target: InboxRowTarget | None
    ) -> InboxRowTarget:
        if self._refresh_settings is not None:
            self._refresh_settings()
        target = self._store.resolve_row(ref)
        if expected_target is not None and expected_target != target:
            raise InboxRowChanged("The inbox row changed.")
        if target is None:
            raise InboxRowNotFound("No matching inbox row.")
        return target
