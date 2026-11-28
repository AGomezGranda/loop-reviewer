"""Summarize saved initial review completions across the local workspace."""

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Literal, Protocol

from loop_reviewer.review.pull_request import PullRequestRef

_DAY = timedelta(days=1)
_WEEK = 7 * _DAY


@dataclass(frozen=True)
class CompletionFact:
    run_id: int
    repository: PullRequestRef
    published: bool
    recorded_at: int | None


class CompletionFacts(Protocol):
    def completion_facts(self) -> tuple[CompletionFact, ...]: ...


@dataclass(frozen=True)
class OutcomeCounts:
    published: int
    completed_locally: int

    @property
    def completed(self) -> int:
        return self.published + self.completed_locally


@dataclass(frozen=True)
class RepositoryOption:
    key: str
    host: str
    slug: str
    legacy_name_only: bool


@dataclass(frozen=True)
class RepositoryStats:
    repository: RepositoryOption
    counts: OutcomeCounts


@dataclass(frozen=True)
class WeekStats:
    start: datetime
    end: datetime
    counts: OutcomeCounts


@dataclass(frozen=True)
class StatsSnapshot:
    totals: OutcomeCounts
    completed_last_7_days: int
    undated_completions: int
    repository_options: tuple[RepositoryOption, ...]
    repositories: tuple[RepositoryStats, ...]
    weeks: tuple[WeekStats, ...]


class UnknownStatsRepository(Exception):
    pass


class ReviewStats:
    def __init__(self, facts: CompletionFacts, now: Callable[[], datetime]) -> None:
        self._facts = facts
        self._now = now

    def read(
        self, period: Literal["30d", "all"], repository_key: str | None = None
    ) -> StatsSnapshot:
        end = datetime.fromtimestamp(int(self._now().timestamp()), UTC)
        start = end - 30 * _DAY
        groups: defaultdict[str, list[CompletionFact]] = defaultdict(list)
        for fact in self._facts.completion_facts():
            groups[_repository_key(fact.repository)].append(fact)
        if repository_key is not None and repository_key not in groups:
            raise UnknownStatsRepository(
                "That repository is not in local review history."
            )

        options = tuple(
            sorted(
                (_repository_option(key, rows) for key, rows in groups.items()),
                key=_repository_order,
            )
        )
        recent: list[CompletionFact] = []
        breakdown: list[RepositoryStats] = []
        undated = 0
        start_epoch, end_epoch = int(start.timestamp()), int(end.timestamp())
        for option in options:
            if repository_key is not None and option.key != repository_key:
                continue
            rows = groups[option.key]
            in_window = []
            for row in rows:
                stamp = row.recorded_at
                if stamp is None:
                    undated += 1
                elif start_epoch < stamp <= end_epoch:
                    in_window.append(row)
            recent.extend(in_window)
            counts = _counts(rows if period == "all" else in_window)
            if counts.completed or repository_key is not None:
                breakdown.append(RepositoryStats(option, counts))
        breakdown.sort(
            key=lambda row: (-row.counts.completed, *_repository_order(row.repository))
        )
        cutoff_7 = int((end - _WEEK).timestamp())
        return StatsSnapshot(
            OutcomeCounts(
                sum(r.counts.published for r in breakdown),
                sum(r.counts.completed_locally for r in breakdown),
            ),
            sum(
                row.recorded_at is not None and row.recorded_at > cutoff_7
                for row in recent
            ),
            undated,
            options,
            tuple(breakdown),
            _weeks(start, end, recent),
        )


def _repository_key(ref: PullRequestRef) -> str:
    if ref.repository_id is not None:
        return f"{ref.host}/{ref.repository_id}"
    return f"{ref.host}/{ref.owner.lower()}/{ref.repo.lower()}"


def _repository_option(key: str, rows: list[CompletionFact]) -> RepositoryOption:
    def recency(row: CompletionFact) -> tuple[int, int]:
        return (-1 if row.recorded_at is None else row.recorded_at, row.run_id)

    ref = max(rows, key=recency).repository
    return RepositoryOption(
        key, ref.host, f"{ref.owner}/{ref.repo}", ref.repository_id is None
    )


def _repository_order(option: RepositoryOption) -> tuple[str, str, str]:
    return option.host, option.slug, option.key


def _counts(rows: Iterable[CompletionFact]) -> OutcomeCounts:
    published = local = 0
    for row in rows:
        if row.published:
            published += 1
        else:
            local += 1
    return OutcomeCounts(published, local)


def _weeks(
    start: datetime, end: datetime, recent: list[CompletionFact]
) -> tuple[WeekStats, ...]:
    monday = datetime.combine(start.date() - start.weekday() * _DAY, time.min, UTC)
    weeks: list[WeekStats] = []
    while monday <= end:
        next_monday = monday + _WEEK
        left, right = max(start, monday), min(end, next_monday)
        first, next_week = int(left.timestamp()), int(next_monday.timestamp())
        counts = _counts(
            row
            for row in recent
            if row.recorded_at is not None and first <= row.recorded_at < next_week
        )
        weeks.append(WeekStats(left, right, counts))
        monday = next_monday
    return tuple(weeks)
