"""Serve aggregate completion statistics over the local API."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query

from loop_reviewer.local_http.models import ApiError, ResponseModel
from loop_reviewer.review.stats import (
    OutcomeCounts,
    ReviewStats,
    UnknownStatsRepository,
)


class CountsResponse(ResponseModel):
    completed: int
    published: int
    completed_locally: int

    @classmethod
    def from_counts(cls, counts: OutcomeCounts) -> CountsResponse:
        return cls(
            completed=counts.completed,
            published=counts.published,
            completed_locally=counts.completed_locally,
        )


class RepositoryOptionResponse(ResponseModel):
    key: str
    host: str
    slug: str
    legacy_name_only: bool


class RepositoryStatsResponse(ResponseModel):
    repository: RepositoryOptionResponse
    counts: CountsResponse


class WeekStatsResponse(ResponseModel):
    start: datetime
    end: datetime
    counts: CountsResponse


class StatsResponse(ResponseModel):
    totals: CountsResponse
    completed_last_7_days: int
    undated_completions: int
    repository_options: tuple[RepositoryOptionResponse, ...]
    repositories: tuple[RepositoryStatsResponse, ...]
    weeks: tuple[WeekStatsResponse, ...]


def routes(stats: ReviewStats) -> APIRouter:
    router = APIRouter(prefix="/stats")

    @router.get("")
    def get_stats(
        period: Annotated[Literal["30d", "all"], Query(alias="range")] = "30d",
        repository: Annotated[str | None, Query(min_length=1)] = None,
    ) -> StatsResponse:
        try:
            snapshot = stats.read(period, repository)
        except UnknownStatsRepository as error:
            raise ApiError(404, "not_found", str(error)) from None
        options = {
            option.key: RepositoryOptionResponse.model_validate(
                option, from_attributes=True
            )
            for option in snapshot.repository_options
        }
        return StatsResponse(
            totals=CountsResponse.from_counts(snapshot.totals),
            completed_last_7_days=snapshot.completed_last_7_days,
            undated_completions=snapshot.undated_completions,
            repository_options=tuple(options.values()),
            repositories=tuple(
                RepositoryStatsResponse(
                    repository=options[row.repository.key],
                    counts=CountsResponse.from_counts(row.counts),
                )
                for row in snapshot.repositories
            ),
            weeks=tuple(
                WeekStatsResponse(
                    start=week.start,
                    end=week.end,
                    counts=CountsResponse.from_counts(week.counts),
                )
                for week in snapshot.weeks
            ),
        )

    return router
