"""Upgrade review tables from previous local database versions."""

import sqlite3

from loop_reviewer.integrations.local_state.publication_records import MIGRATE_V6

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS review_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        review_key TEXT NOT NULL,
        selection TEXT NOT NULL,
        status TEXT NOT NULL
            CHECK (status IN ('running', 'draft_ready', 'failed', 'abandoned')),
        draft TEXT,
        failure TEXT,
        is_current INTEGER NOT NULL CHECK (is_current IN (0, 1)),
        CHECK ((status = 'draft_ready') = (draft IS NOT NULL))
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS review_runs_current
        ON review_runs (review_key) WHERE is_current = 1
    """,
)


def migrate_reviews(connection: sqlite3.Connection, from_version: int) -> None:
    if from_version == 0:
        for statement in _SCHEMA:
            connection.execute(statement)
    if from_version < 2:
        connection.execute(
            "ALTER TABLE review_runs ADD COLUMN draft_version INTEGER "
            "NOT NULL DEFAULT 1 CHECK (draft_version >= 1)"
        )
    if from_version < 3:
        connection.execute("""
            CREATE TABLE review_completions (
                identity_key TEXT PRIMARY KEY NOT NULL,
                run_id INTEGER NOT NULL,
                draft_version INTEGER NOT NULL CHECK (draft_version >= 1),
                selection TEXT NOT NULL,
                final_draft TEXT NOT NULL,
                publication_status TEXT NOT NULL CHECK (publication_status = 'not_published')
            )
            """)
    if from_version < 5:
        connection.execute("""
            ALTER TABLE review_runs ADD COLUMN failure_kind TEXT
            CHECK (failure_kind IS NULL OR (
                status = 'failed' AND failure_kind IN ('setup', 'input', 'failed')
            ))
            """)
    if from_version < 6:
        for statement in MIGRATE_V6:
            connection.execute(statement)


def add_generation_metadata(connection: sqlite3.Connection, _from_version: int) -> None:
    for table in ("review_runs", "review_completions"):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN generation_model TEXT")
        connection.execute(
            f"ALTER TABLE {table} ADD COLUMN estimated_cost_usd REAL "
            "CHECK (estimated_cost_usd >= 0)"
        )


def add_reviewer_report(connection: sqlite3.Connection, _from_version: int) -> None:
    connection.execute("ALTER TABLE review_runs ADD COLUMN reviewer_report TEXT")


def add_completion_recorded_at(
    connection: sqlite3.Connection, _from_version: int
) -> None:
    connection.execute("""
        ALTER TABLE review_completions ADD COLUMN recorded_at INTEGER
        CHECK (recorded_at IS NULL OR (typeof(recorded_at) = 'integer' AND recorded_at >= 0))
        """)
