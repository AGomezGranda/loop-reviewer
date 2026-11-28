"""Read only the saved facts needed to summarize completion history."""

import sqlite3

from loop_reviewer.integrations.local_state.records import selection_from_json
from loop_reviewer.review.stats import CompletionFact
from loop_reviewer.review.store import ReviewStoreError


def read_completion_facts(connection: sqlite3.Connection) -> tuple[CompletionFact, ...]:
    facts: list[CompletionFact] = []
    for run_id, selection, status, recorded_at in connection.execute(
        "SELECT run_id, selection, publication_status, recorded_at FROM review_completions"
    ):
        if (
            type(run_id) is not int
            or run_id < 1
            or status not in ("published", "not_published")
            or (
                recorded_at is not None
                and (type(recorded_at) is not int or recorded_at < 0)
            )
        ):
            raise ReviewStoreError("The local review database is unreadable.")
        facts.append(
            CompletionFact(
                run_id,
                selection_from_json(selection).ref,
                status == "published",
                recorded_at,
            )
        )
    return tuple(facts)
