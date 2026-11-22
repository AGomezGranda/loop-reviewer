"""Version-six schema and publication snapshot codecs."""

import json
from dataclasses import asdict
from typing import cast

from loop_reviewer.integrations.local_state.keys import identity_key
from loop_reviewer.integrations.local_state.records import (
    selection_from_json,
    selection_json,
)
from loop_reviewer.review.model import (
    DraftTarget,
    DraftValidationError,
    PublicationReceipt,
)
from loop_reviewer.review.publication_model import (
    InlineComment,
    PublicationError,
    PublicationId,
    PublicationPreview,
    PublicationRecord,
    PublicationStatus,
)
from loop_reviewer.review.store import ReviewStoreError

PUBLICATION_COLUMNS = "id, identity_key, run_id, draft_version, preview, status, ambiguity_seen, review_id"
MIGRATE_V6 = (
    """CREATE TABLE review_publications (
        id TEXT PRIMARY KEY NOT NULL CHECK (length(id) = 36),
        identity_key TEXT NOT NULL,
        run_id INTEGER NOT NULL CHECK (typeof(run_id) = 'integer' AND run_id > 0),
        draft_version INTEGER NOT NULL CHECK (typeof(draft_version) = 'integer' AND draft_version > 0),
        preview TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('preview', 'unconfirmed', 'rejected', 'published')),
        ambiguity_seen INTEGER NOT NULL DEFAULT 0 CHECK (ambiguity_seen IN (0, 1)),
        review_id INTEGER,
        CHECK (ambiguity_seen = 0 OR status IN ('unconfirmed', 'published')),
        CHECK ((status = 'published' AND typeof(review_id) = 'integer' AND review_id > 0)
            OR (status != 'published' AND review_id IS NULL))
    )""",
    """CREATE UNIQUE INDEX review_publications_unconfirmed
        ON review_publications (identity_key) WHERE status = 'unconfirmed'""",
    "ALTER TABLE review_completions RENAME TO review_completions_v5",
    """CREATE TABLE review_completions (
        identity_key TEXT PRIMARY KEY NOT NULL,
        run_id INTEGER NOT NULL,
        draft_version INTEGER NOT NULL CHECK (draft_version >= 1),
        selection TEXT NOT NULL,
        final_draft TEXT NOT NULL,
        publication_status TEXT NOT NULL,
        github_review_id INTEGER,
        publication_action TEXT,
        CHECK ((publication_status = 'not_published' AND github_review_id IS NULL AND publication_action IS NULL)
            OR (publication_status = 'published' AND typeof(github_review_id) = 'integer' AND github_review_id > 0
                AND publication_action IS NOT NULL AND publication_action IN ('comment', 'approve')))
    )""",
    """INSERT INTO review_completions
        (identity_key, run_id, draft_version, selection, final_draft, publication_status)
        SELECT identity_key, run_id, draft_version, selection, final_draft, 'not_published'
        FROM review_completions_v5""",
    "DROP TABLE review_completions_v5",
)


def preview_json(preview: PublicationPreview) -> str:
    return json.dumps(
        {
            "id": preview.id.value,
            "source": asdict(preview.source),
            "selection": json.loads(selection_json(preview.selection)),
            "actor_id": preview.actor_id,
            "action": preview.action,
            "body": preview.body,
            "comments": [asdict(comment) for comment in preview.comments],
        },
        separators=(",", ":"),
    )


def publication_from_row(row: tuple[object, ...]) -> PublicationRecord:
    id_value, key, run_id, version, raw, status, ambiguity, review_id = row
    try:
        if (
            not isinstance(raw, str)
            or type(ambiguity) is not int
            or ambiguity not in (0, 1)
        ):
            raise ValueError
        data = _object(json.loads(raw))
        source = _object(data["source"])
        action, comments = data["action"], data["comments"]
        if (action != "comment" and action != "approve") or not isinstance(
            comments, list
        ):
            raise ValueError
        preview = PublicationPreview(
            PublicationId(_text(data["id"])),
            DraftTarget(_int(source["run_id"]), _int(source["version"])),
            selection_from_json(json.dumps(data["selection"])),
            _int(data["actor_id"]),
            action,
            _text(data["body"]),
            tuple(_comment(_object(item)) for item in comments),
        )
        if (id_value, key, run_id, version) != (
            preview.id.value,
            identity_key(preview.selection.identity),
            preview.source.run_id,
            preview.source.version,
        ):
            raise ValueError
        receipt = (
            None if review_id is None else PublicationReceipt(_int(review_id), action)
        )
        return PublicationRecord(
            preview, cast("PublicationStatus", status), bool(ambiguity), receipt
        )
    except (
        KeyError,
        ValueError,
        TypeError,
        RecursionError,
        PublicationError,
        DraftValidationError,
    ):
        raise ReviewStoreError("The local publication record is unreadable.") from None


def _comment(data: dict[str, object]) -> InlineComment:
    return InlineComment(_text(data["path"]), _int(data["line"]), _text(data["body"]))


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError
    return cast("dict[str, object]", value)


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError
    return value


def _int(value: object) -> int:
    if type(value) is not int:
        raise ValueError
    return value
