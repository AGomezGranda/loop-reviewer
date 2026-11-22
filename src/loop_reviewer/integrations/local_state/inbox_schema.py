"""Add inbox state and a stable review activity lookup to the local database."""

import sqlite3

# CASE protects both index creation and queries from malformed historical keys.
STABLE_IDENTITY = """
CASE WHEN json_valid(review_key) THEN
    CASE WHEN json_type(review_key, '$[1]') = 'integer' THEN json_array(
        json_extract(review_key, '$[0]'), json_extract(review_key, '$[1]'),
        json_extract(review_key, '$[2]'), json_extract(review_key, '$[3]')) END
END
"""


def create_inbox_tables(connection: sqlite3.Connection, _from_version: int) -> None:
    connection.execute("""
        CREATE TABLE inbox_settings (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            project TEXT NOT NULL,
            version INTEGER NOT NULL CHECK (version > 0)
        )
    """)
    connection.execute("""
        CREATE TABLE inbox_repositories (
            host TEXT NOT NULL,
            repository_id INTEGER NOT NULL,
            account TEXT NOT NULL,
            slug TEXT NOT NULL,
            position INTEGER NOT NULL,
            sequence INTEGER NOT NULL DEFAULT 0,
            outcome TEXT CHECK (outcome IN ('success', 'failed')),
            checked_at TEXT,
            error TEXT,
            show_cached INTEGER NOT NULL DEFAULT 0 CHECK (show_cached IN (0, 1)),
            success_at TEXT,
            eligible_rows TEXT,
            PRIMARY KEY (host, repository_id, account)
        )
    """)
    connection.execute("""
        CREATE TABLE inbox_deferrals (
            host TEXT NOT NULL,
            repository_id INTEGER NOT NULL,
            account TEXT NOT NULL,
            pr_number INTEGER NOT NULL CHECK (pr_number > 0),
            PRIMARY KEY (host, repository_id, account, pr_number)
        )
    """)
    connection.execute(f"""
        CREATE INDEX review_runs_activity ON review_runs (({STABLE_IDENTITY}), id DESC)
        WHERE is_current = 1
    """)


def rename_inbox_projects(connection: sqlite3.Connection, _from_version: int) -> None:
    connection.execute("ALTER TABLE inbox_settings RENAME COLUMN project TO projects")
