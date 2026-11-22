from dataclasses import replace

import pytest

from loop_reviewer.inbox.model import (
    DiscoveredPullRequest,
    InboxReport,
    InboxRow,
    InboxSettings,
    JiraProjectKey,
    RepositoryAddress,
    RepositoryScanStatus,
    WatchedRepository,
)
from loop_reviewer.inbox.polling import InboxChanges, next_deadline
from loop_reviewer.review.model import ReviewIdentity
from loop_reviewer.review.pull_request import PullRequestRef

_REPO = WatchedRepository(RepositoryAddress("github.com", "org/repo"), 22, "alice")
_SETTINGS = InboxSettings((JiraProjectKey("ABC"),), (_REPO,), 1)
_FAILURE = InboxReport(
    _SETTINGS,
    (RepositoryScanStatus(_REPO, "failed", None, "Temporary failure", 0),),
    (),
)
_SUCCESS = InboxReport(
    _SETTINGS, (RepositoryScanStatus(_REPO, "success", None, None, 0),), ()
)


@pytest.mark.parametrize(
    ("duration", "second_tick"),
    [
        pytest.param(0, 120, id="fixed-cadence"),
        pytest.param(120, 120, id="exactly-on-tick"),
        pytest.param(245, 360, id="skip-elapsed-ticks"),
    ],
)
def test_next_deadline_skips_elapsed_ticks(duration: float, second_tick: float) -> None:
    assert next_deadline(67, 67 + duration) == 67 + second_tick


def test_watch_emits_structured_changes_only_for_fresh_active_work() -> None:
    ref = PullRequestRef.from_repository("github.com", "org/repo", 1, repository_id=22)
    pr = DiscoveredPullRequest(ref, "[ABC-1] Current work", "bob", "open", False, ())
    row = InboxRow(
        ReviewIdentity.for_pull_request(ref, "alice"),
        pr,
        pr.reasons(_SETTINGS.projects, "alice"),
        None,
        stale=False,
    )
    first = replace(_SUCCESS, active=(row,))
    invisible_change = replace(
        row, pull_request=replace(pr, requested_users=("carol",))
    )
    changed = replace(row, last_saved="draft_ready")
    reports = iter(
        (
            first,
            None,  # A superseded scan does not reset the previous result.
            replace(_SUCCESS, active=(invisible_change,)),
            replace(_SUCCESS, active=(changed,)),
            replace(_FAILURE, active=(replace(changed, stale=True),)),
            replace(_SUCCESS, active=(changed,)),
            replace(_SUCCESS, deferred=(replace(changed, deferred=True),)),
            replace(_SUCCESS, active=(changed,)),
        )
    )
    changes = InboxChanges()
    cycles = [changes.observe(report) for report in reports if report is not None]

    assert [cycle.changed for cycle in cycles] == [
        (row,),
        (),
        (changed,),
        (),
        (),
        (),
        (changed,),
    ]
