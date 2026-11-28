from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from loop_reviewer.review.pull_request import PullRequestRef
from loop_reviewer.review.stats import (
    CompletionFact,
    OutcomeCounts,
    ReviewStats,
    UnknownStatsRepository,
)

_NOW = datetime(2026, 11, 30, tzinfo=UTC)  # Monday
_REPO = PullRequestRef("github.com", "org", "repo", 1, 42)


@dataclass
class Facts:
    rows: tuple[CompletionFact, ...]

    def completion_facts(self) -> tuple[CompletionFact, ...]:
        return self.rows


def _at(delta: timedelta) -> int:
    return int((_NOW + delta).timestamp())


def test_rolling_windows_and_clipped_utc_weeks() -> None:
    times = (
        _at(timedelta(days=-30)),
        _at(timedelta(days=-30, seconds=1)),
        _at(timedelta(days=-28)),
        _at(timedelta(days=-7)),
        _at(timedelta(days=-7, seconds=1)),
        _at(timedelta()),
        _at(timedelta(seconds=1)),
        None,
    )
    facts = Facts(
        tuple(CompletionFact(i, _REPO, i % 2 == 0, t) for i, t in enumerate(times))
    )
    clock_reads = 0

    def now() -> datetime:
        nonlocal clock_reads
        clock_reads += 1
        return _NOW

    stats = ReviewStats(facts, now)
    recent = stats.read("30d")
    assert clock_reads == 1
    assert recent.totals == OutcomeCounts(2, 3)
    assert recent.completed_last_7_days == 2
    assert recent.undated_completions == 1
    assert [
        (w.start.date().isoformat(), w.end.date().isoformat(), w.counts.completed)
        for w in recent.weeks
    ] == [
        ("2026-10-31", "2026-11-02", 1),
        ("2026-11-02", "2026-11-09", 1),
        ("2026-11-09", "2026-11-16", 0),
        ("2026-11-16", "2026-11-23", 0),
        ("2026-11-23", "2026-11-30", 2),
        ("2026-11-30", "2026-11-30", 1),
    ]
    assert all(w.start.tzinfo is UTC and w.end.tzinfo is UTC for w in recent.weeks)
    all_time = stats.read("all")
    assert all_time.totals == OutcomeCounts(4, 4)
    assert all_time.weeks == recent.weeks


def test_repository_identity_latest_slug_and_quiet_selection() -> None:
    old = PullRequestRef("github.com", "org", "old", 1, 42)
    other_host = PullRequestRef("github.example", "org", "repo", 1, 42)
    legacy = PullRequestRef("github.com", "Org", "Repo", 1)
    old_time = _at(timedelta(days=-31))
    facts = Facts(
        (
            CompletionFact(1, old, True, old_time),
            CompletionFact(2, _REPO, False, old_time),
            CompletionFact(
                3, _REPO, True, old_time
            ),  # another account's saved completion
            CompletionFact(4, other_host, True, _at(timedelta())),
            CompletionFact(5, legacy, False, None),
            CompletionFact(6, old, False, None),
            CompletionFact(
                7, PullRequestRef("github.com", "org", "repo", 2), False, old_time
            ),
        )
    )
    stats = ReviewStats(facts, lambda: _NOW)
    recent = stats.read("30d")
    assert [(r.key, r.slug, r.legacy_name_only) for r in recent.repository_options] == [
        ("github.com/42", "org/repo", False),
        ("github.com/org/repo", "org/repo", True),
        ("github.example/42", "org/repo", False),
    ]
    assert [r.repository.key for r in recent.repositories] == ["github.example/42"]
    quiet = stats.read("30d", "github.com/42")
    assert quiet.totals.completed == quiet.completed_last_7_days == 0
    assert quiet.undated_completions == 1
    assert len(quiet.repositories) == 1 and quiet.repositories[0].counts.completed == 0
    assert all(w.counts.completed == 0 for w in quiet.weeks)
    assert quiet.repository_options == recent.repository_options
    assert stats.read("all", "github.com/42").totals == OutcomeCounts(2, 2)
    assert [r.counts.completed for r in stats.read("all").repositories] == [4, 2, 1]


def test_empty_workspace_and_unknown_repository() -> None:
    stats = ReviewStats(Facts(()), lambda: _NOW)
    empty = stats.read("30d")
    assert (
        empty.totals.completed
        == empty.completed_last_7_days
        == empty.undated_completions
        == 0
    )
    assert empty.repositories == empty.repository_options == ()
    assert all(w.counts.completed == 0 for w in empty.weeks)
    with pytest.raises(UnknownStatsRepository):
        stats.read("all", "github.com/42")
