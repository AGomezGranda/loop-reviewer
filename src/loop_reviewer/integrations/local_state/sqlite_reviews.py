"""Persist review runs, drafts, and completions in SQLite transactions."""

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from loop_reviewer.integrations.local_state.database import (
    LocalDatabase,
    UnsupportedSchemaVersion,
)
from loop_reviewer.integrations.local_state.inbox_schema import STABLE_IDENTITY
from loop_reviewer.integrations.local_state.keys import comparison_key, identity_key
from loop_reviewer.integrations.local_state.publication_records import (
    PUBLICATION_COLUMNS,
    preview_json,
    publication_from_row,
)
from loop_reviewer.integrations.local_state.records import (
    completion_from_row,
    draft_json,
    run_from_row,
    selection_json,
)
from loop_reviewer.review.model import (
    MAX_STORED_INTEGER,
    CompletedReview,
    DraftTarget,
    DraftVersionMismatch,
    InvalidRunId,
    PublicationReceipt,
    ReviewAlreadyCompleted,
    ReviewIdentity,
    ReviewKey,
    ReviewRun,
    ReviewSelection,
    is_retryable_status,
    require_run_id,
)
from loop_reviewer.review.publication_model import (
    PublicationId,
    PublicationPreview,
    PublicationRecord,
    PublicationStatus,
)
from loop_reviewer.review.store import (
    DraftConflict,
    ReviewActivity,
    ReviewStoreError,
    UnconfirmedPublication,
)

_RUN_COLUMNS = "id, selection, status, draft, failure, draft_version, failure_kind, generation_model, estimated_cost_usd, reviewer_report"
_COMPLETION_COLUMNS = "identity_key, run_id, draft_version, selection, final_draft, publication_status, github_review_id, publication_action, generation_model, estimated_cost_usd"
_COMPLETION_REPORT = (
    "(SELECT reviewer_report FROM review_runs WHERE id = review_completions.run_id)"
)
_UNREADABLE = "The local review database is unreadable."


class SqliteReviewRunStore:
    def __init__(self, path: Path) -> None:
        self._database = LocalDatabase(path)

    def latest(self, key: ReviewKey) -> ReviewRun | None:
        with self._read() as connection:
            row = connection.execute(
                f"SELECT {_RUN_COLUMNS} FROM review_runs "
                "WHERE review_key = ? AND is_current = 1",
                (comparison_key(key),),
            ).fetchone()
            if row is None:
                return None
            run = run_from_row(row)
            if run.selection.key != key:
                raise ReviewStoreError(_UNREADABLE)
            return run

    def get_draft(self, run_id: int) -> ReviewRun | None:
        _require_supported_run_id(run_id)
        with self._read() as connection:
            return self._draft_for_run(connection, run_id)

    def completed(self, identity: ReviewIdentity) -> CompletedReview | None:
        with self._read() as connection:
            return self._completion_for_identity(connection, identity)

    def completions(
        self, before_run_id: int | None, limit: int
    ) -> tuple[CompletedReview, ...]:
        if before_run_id is not None:
            _require_supported_run_id(before_run_id)
        with self._read() as connection:
            rows = connection.execute(
                f"SELECT {_COMPLETION_COLUMNS} FROM review_completions "
                "WHERE (? IS NULL OR run_id < ?) ORDER BY run_id DESC LIMIT ?",
                (before_run_id, before_run_id, limit),
            )
            return tuple(_decode_completion(row) for row in rows)

    def completion(self, run_id: int) -> CompletedReview | None:
        _require_supported_run_id(run_id)
        with self._read() as connection:
            row = connection.execute(
                f"SELECT {_COMPLETION_COLUMNS}, {_COMPLETION_REPORT} "
                "FROM review_completions WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            return None if row is None else _decode_completion(row)

    def unconfirmed_publications(self) -> tuple[PublicationRecord, ...]:
        with self._read() as connection:
            rows = connection.execute(
                f"SELECT {PUBLICATION_COLUMNS} FROM review_publications "
                "WHERE status = 'unconfirmed' ORDER BY run_id DESC, id",
            )
            return tuple(publication_from_row(row) for row in rows)

    def unconfirmed_for(self, run_id: int) -> PublicationRecord | None:
        _require_supported_run_id(run_id)
        with self._read() as connection:
            run = self._draft_for_run(connection, run_id)
            return (
                None
                if run is None
                else self._unconfirmed(connection, run.selection.identity)
            )

    def activity(
        self, identities: tuple[ReviewIdentity, ...]
    ) -> dict[ReviewIdentity, ReviewActivity]:
        result: dict[ReviewIdentity, ReviewActivity] = {}
        with self._read() as connection:
            for identity in identities:
                if not isinstance(identity.repository, int) or identity in result:
                    continue
                key = identity_key(identity)
                row = connection.execute(
                    f"""
                    SELECT CASE WHEN EXISTS (SELECT 1 FROM review_completions WHERE identity_key = :key)
                        THEN 'completed' ELSE (
                            SELECT status FROM review_runs WHERE is_current = 1 AND ({STABLE_IDENTITY}) = :key
                            ORDER BY id DESC LIMIT 1
                        ) END
                    """,
                    {"key": key},
                ).fetchone()
                status: object = None if row is None else row[0]
                if status is None:
                    continue
                if status in (
                    "completed",
                    "running",
                    "draft_ready",
                ) or is_retryable_status(status):
                    result[identity] = status
                else:
                    raise ReviewStoreError(_UNREADABLE)
        return result

    def save_preview(self, preview: PublicationPreview) -> None:
        with self._write() as connection:
            self._require_publication_source(connection, preview)
            self._require_no_unconfirmed(connection, preview.selection.identity)
            connection.execute(
                "INSERT INTO review_publications (id, identity_key, run_id, draft_version, preview, status) "
                "VALUES (?, ?, ?, ?, ?, 'preview')",
                (
                    preview.id.value,
                    identity_key(preview.selection.identity),
                    preview.source.run_id,
                    preview.source.version,
                    preview_json(preview),
                ),
            )

    def get_publication(self, id: PublicationId) -> PublicationRecord | None:
        with self._read() as connection:
            return self._publication(connection, id)

    def reserve_send(
        self, id: PublicationId, expected: PublicationStatus
    ) -> PublicationRecord:
        with self._write() as connection:
            record = self._require_publication(connection, id)
            reserved = record.reserve(expected)
            self._require_publication_source(connection, record.preview)
            self._require_no_unconfirmed(
                connection, record.preview.selection.identity, except_id=id
            )
            self._update_publication(connection, record, reserved)
        return reserved

    def record_rejection(self, id: PublicationId) -> PublicationRecord:
        with self._write() as connection:
            record = self._require_publication(connection, id)
            updated = record.reject()
            self._update_publication(connection, record, updated)
        return updated

    def record_published(
        self, id: PublicationId, receipt: PublicationReceipt
    ) -> CompletedReview:
        with self._write() as connection:
            record = self._require_publication(connection, id)
            updated = record.publish(receipt)
            preview = record.preview
            if updated == record:
                existing = self._completion_for_identity(
                    connection, preview.selection.identity
                )
                if not record.matches_completion(existing):
                    raise ReviewStoreError(_UNREADABLE)
                return existing
            run = self._require_publication_source(connection, preview)
            completion = CompletedReview(
                preview.source,
                preview.selection,
                run.saved_draft().draft,
                receipt,
                run.generation_model,
                run.estimated_cost_usd,
                run.report,
            )
            self._update_publication(connection, record, updated)
            self._insert_completion(connection, completion)
        return completion

    def _require_publication(
        self, connection: sqlite3.Connection, id: PublicationId
    ) -> PublicationRecord:
        record = self._publication(connection, id)
        if record is None:
            raise ReviewStoreError(f"Unknown publication ID {id.value}.")
        return record

    def _update_publication(
        self,
        connection: sqlite3.Connection,
        initial: PublicationRecord,
        updated: PublicationRecord,
    ) -> None:
        cursor = connection.execute(
            "UPDATE review_publications SET status = ?, ambiguity_seen = ?, review_id = ? WHERE id = ? AND status = ?",
            (
                updated.status,
                int(updated.ambiguity_seen),
                None if updated.receipt is None else updated.receipt.review_id,
                initial.preview.id.value,
                initial.status,
            ),
        )
        if cursor.rowcount != 1:
            raise ReviewStoreError(
                "The publication state changed. Recover or preview again."
            )

    def _publication(
        self, connection: sqlite3.Connection, id: PublicationId
    ) -> PublicationRecord | None:
        row = connection.execute(
            f"SELECT {PUBLICATION_COLUMNS} FROM review_publications WHERE id = ?",
            (id.value,),
        ).fetchone()
        return None if row is None else publication_from_row(row)

    def _require_publication_source(
        self, connection: sqlite3.Connection, preview: PublicationPreview
    ) -> ReviewRun:
        target, selection = preview.source, preview.selection
        _require_supported_run_id(target.run_id)
        self._require_uncompleted(connection, selection.identity)
        run = self._draft_for_run(connection, target.run_id)
        if (
            run is None
            or run.draft_target != target
            or run.selection.identity != selection.identity
            or run.selection.revision != selection.revision
        ):
            raise DraftConflict()
        return run

    def _require_no_unconfirmed(
        self,
        connection: sqlite3.Connection,
        identity: ReviewIdentity,
        *,
        except_id: PublicationId | None = None,
    ) -> None:
        record = self._unconfirmed(connection, identity)
        if record is not None and record.preview.id != except_id:
            raise UnconfirmedPublication(record.preview)

    def _unconfirmed(
        self, connection: sqlite3.Connection, identity: ReviewIdentity
    ) -> PublicationRecord | None:
        row = connection.execute(
            f"SELECT {PUBLICATION_COLUMNS} FROM review_publications WHERE identity_key = ? AND status = 'unconfirmed'",
            (identity_key(identity),),
        ).fetchone()
        return None if row is None else publication_from_row(row)

    def complete(self, review: CompletedReview) -> CompletedReview:
        _require_supported_run_id(review.source.run_id)
        identity = review.selection.identity
        with self._write() as connection:
            existing = self._completion_for_identity(connection, identity)
            if existing is not None:
                return existing.receipt_for(review.source)
            self._require_no_unconfirmed(connection, identity)
            run = self._draft_for_run(connection, review.source.run_id)
            if run is None or run.selection.identity != identity:
                raise DraftConflict()
            snapshot = run.complete_locally()
            if snapshot != review:
                raise DraftConflict()
            self._insert_completion(connection, snapshot)
        return snapshot

    def _insert_completion(
        self, connection: sqlite3.Connection, snapshot: CompletedReview
    ) -> None:
        receipt = snapshot.receipt
        connection.execute(
            """
            INSERT INTO review_completions
                (identity_key, run_id, draft_version, selection, final_draft, publication_status, github_review_id, publication_action,
                 generation_model, estimated_cost_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                identity_key(snapshot.selection.identity),
                snapshot.source.run_id,
                snapshot.source.version,
                selection_json(snapshot.selection),
                draft_json(snapshot.final_draft),
                snapshot.publication_status,
                None if receipt is None else receipt.review_id,
                None if receipt is None else receipt.action,
                snapshot.generation_model,
                snapshot.estimated_cost_usd,
            ),
        )

    def _completion_for_identity(
        self, connection: sqlite3.Connection, identity: ReviewIdentity
    ) -> CompletedReview | None:
        row = connection.execute(
            f"SELECT {_COMPLETION_COLUMNS}, {_COMPLETION_REPORT} "
            "FROM review_completions WHERE identity_key = ?",
            (identity_key(identity),),
        ).fetchone()
        if row is None:
            return None
        completion = _decode_completion(row)
        if completion.selection.identity != identity:
            raise ReviewStoreError(_UNREADABLE)
        return completion

    def _require_uncompleted(
        self, connection: sqlite3.Connection, identity: ReviewIdentity
    ) -> None:
        existing = self._completion_for_identity(connection, identity)
        if existing is not None:
            raise ReviewAlreadyCompleted(existing)

    def save_edit(self, target: DraftTarget, run: ReviewRun) -> None:
        _require_supported_run_id(target.run_id)
        with self._write() as connection:
            current = self._draft_for_run(connection, target.run_id)
            if current is None:
                raise DraftConflict()
            self._require_uncompleted(connection, current.selection.identity)
            self._require_no_unconfirmed(connection, current.selection.identity)
            try:
                current.require_draft_target(target)
            except DraftVersionMismatch as error:
                raise DraftConflict() from error
            if run.draft is None or current.edit_draft(run.draft) != run:
                raise ReviewStoreError("The edited draft does not match its target.")
            cursor = connection.execute(
                """
                UPDATE review_runs SET draft = ?, draft_version = draft_version + 1
                WHERE id = ? AND is_current = 1 AND status = 'draft_ready'
                    AND draft_version = ? AND draft_version < ?
                """,
                (
                    draft_json(run.draft),
                    target.run_id,
                    target.version,
                    MAX_STORED_INTEGER,
                ),
            )
            if cursor.rowcount != 1:
                raise DraftConflict()

    def _draft_for_run(
        self, connection: sqlite3.Connection, run_id: int
    ) -> ReviewRun | None:
        row = connection.execute(
            f"SELECT {_RUN_COLUMNS} FROM review_runs "
            "WHERE id = ? AND is_current = 1 AND status = 'draft_ready'",
            (run_id,),
        ).fetchone()
        return None if row is None else run_from_row(row)

    def start(self, selection: ReviewSelection, *, replaces: int | None) -> ReviewRun:
        key = comparison_key(selection.key)
        with self._write() as connection:
            self._require_uncompleted(connection, selection.identity)
            current = connection.execute(
                "SELECT id, status FROM review_runs "
                "WHERE review_key = ? AND is_current = 1",
                (key,),
            ).fetchone()
            if replaces is None:
                if current is not None:
                    raise ReviewStoreError(
                        "A review attempt already exists for this comparison."
                    )
            else:
                if current is None or current[0] != replaces:
                    raise ReviewStoreError(
                        "The review attempt to retry is no longer current."
                    )
                if not is_retryable_status(current[1]):
                    raise ReviewStoreError(
                        "Only a failed or abandoned attempt can be retried."
                    )
                connection.execute(
                    "UPDATE review_runs SET is_current = 0 WHERE id = ?",
                    (replaces,),
                )
            cursor = connection.execute(
                """
                INSERT INTO review_runs
                    (review_key, selection, status, draft, failure, is_current)
                VALUES (?, ?, 'running', NULL, NULL, 1)
                """,
                (key, selection_json(selection)),
            )
            if cursor.lastrowid is None:
                raise ReviewStoreError("Could not create the review attempt.")
            run_id = cursor.lastrowid
        return ReviewRun(run_id, selection, "running", None, None)

    def save(self, run: ReviewRun) -> None:
        if run.status == "running":
            raise ReviewStoreError(
                "Cannot save a review attempt that is still running."
            )
        draft = None if run.draft is None else draft_json(run.draft)
        report = None if run.report is None else run.report.model_dump_json()
        with self._write() as connection:
            row = connection.execute(
                f"SELECT {_RUN_COLUMNS} FROM review_runs "
                "WHERE id = ? AND is_current = 1 AND status = 'running'",
                (run.id,),
            ).fetchone()
            if row is None:
                raise ReviewStoreError(
                    "The review attempt was superseded or is no longer running."
                )
            current = run_from_row(row)
            if (current.selection, current.draft_target) != (
                run.selection,
                run.draft_target,
            ):
                raise ReviewStoreError(
                    "The review attempt does not match the saved run."
                )
            cursor = connection.execute(
                """
                UPDATE review_runs SET status = ?, draft = ?, failure = ?, failure_kind = ?,
                    generation_model = ?, estimated_cost_usd = ?, reviewer_report = ?
                WHERE id = ? AND is_current = 1 AND status = 'running'
                """,
                (
                    run.status,
                    draft,
                    run.failure,
                    run.failure_kind,
                    run.generation_model,
                    run.estimated_cost_usd,
                    report,
                    run.id,
                ),
            )
            if cursor.rowcount != 1:
                raise ReviewStoreError(
                    "The review attempt was superseded or is no longer running."
                )

    @contextmanager
    def _read(self) -> Generator[sqlite3.Connection]:
        try:
            with self._database.read() as connection:
                yield connection
        except UnsupportedSchemaVersion:
            raise ReviewStoreError(
                "The local review database uses an unsupported schema version."
            ) from None
        except (sqlite3.Error, OSError) as error:
            raise _storage_error("read", error) from error

    @contextmanager
    def _write(self) -> Generator[sqlite3.Connection]:
        try:
            with self._database.write() as connection:
                yield connection
        except UnsupportedSchemaVersion:
            raise ReviewStoreError(
                "The local review database uses an unsupported schema version."
            ) from None
        except (sqlite3.Error, OSError) as error:
            raise _storage_error("save", error) from error


def _require_supported_run_id(run_id: int) -> None:
    require_run_id(run_id)
    if run_id > MAX_STORED_INTEGER:
        raise InvalidRunId("Run ID is outside the supported range.")


def _storage_error(operation: str, error: sqlite3.Error | OSError) -> ReviewStoreError:
    code = getattr(error, "sqlite_errorcode", 0) & 0xFF
    if code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        guidance = "The database is busy. Try again after the other operation finishes."
    elif isinstance(error, PermissionError) or code in {
        sqlite3.SQLITE_PERM,
        sqlite3.SQLITE_READONLY,
        sqlite3.SQLITE_AUTH,
    }:
        guidance = "Check permissions on the state directory."
    elif code == sqlite3.SQLITE_FULL:
        guidance = "Check available disk space."
    elif code in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
        guidance = "The database is unreadable. Check the local database."
    else:
        guidance = "Check the local database and state directory."
    return ReviewStoreError(f"Could not {operation} the local review state. {guidance}")


def _decode_completion(row: tuple[object, ...]) -> CompletedReview:
    completion = completion_from_row(row[1:])
    if identity_key(completion.selection.identity) != row[0]:
        raise ReviewStoreError(_UNREADABLE)
    return completion
