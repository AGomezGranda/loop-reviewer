"""Version 1 wire records for the standalone reviewer."""

from typing import Annotated, Literal, Self
from urllib.parse import quote_from_bytes, unquote_to_bytes

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

MAX_CHANGES = 10_000
OVERSIZED_BYTES = 1024 * 1024
_MAX_PATH_BYTES = 4096

_ObjectId = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
_Path = Annotated[
    str,
    Field(
        min_length=1,
        max_length=3 * _MAX_PATH_BYTES,
        pattern=r"^(?:[A-Za-z0-9._~/\-]|%[0-9A-F]{2})+$",
    ),
]
_Count = Annotated[int, Field(ge=0, le=2**63 - 1)]
_Version = Annotated[int, Field(ge=1, le=1)]
_Zero = Annotated[int, Field(ge=0, le=0)]
_Empty = tuple[()]
GeneratedMarker = Literal["marked", "unmarked", "unknown"]
FileMode = Literal["100644", "100755", "120000", "160000"]
ChangeType = Literal["added", "deleted", "modified", "renamed", "type_changed"]
InventoryErrorCode = Literal[
    "invalid_repository",
    "git_unavailable",
    "missing_commit",
    "invalid_comparison",
    "invalid_inventory",
    "git_error",
    "work_limit",
]


def encode_path(raw: bytes) -> str:
    return quote_from_bytes(raw, safe="/")


def decode_path(path: str) -> bytes:
    return unquote_to_bytes(path)


def canonical_path(path: str) -> bytes:
    raw = decode_path(path)
    if (
        len(raw) > _MAX_PATH_BYTES
        or b"\0" in raw
        or any(part in (b"", b".", b"..") for part in raw.split(b"/"))
        or encode_path(raw) != path
    ):
        raise ValueError("Expected a canonical percent-encoded Git path")
    return raw


class _Record(BaseModel):
    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", revalidate_instances="always"
    )


class PinnedComparison(_Record):
    merge_base: _ObjectId
    head: _ObjectId

    @model_validator(mode="after")
    def same_hash_width(self) -> Self:
        if len(self.merge_base) != len(self.head):
            raise ValueError("Commit IDs must use the same hash format")
        return self


MAX_RUNTIME_SPEND = 3_000_000


class InventoryBudget(_Record):
    max_model_spend_usd_micros: Annotated[int, Field(ge=0, le=MAX_RUNTIME_SPEND)]


class ReviewerRequest(_Record):
    schema_version: _Version
    scope: Literal["inventory"]
    comparison: PinnedComparison
    budget: InventoryBudget
    discussion: None


class FileSide(_Record):
    path: _Path
    object_id: _ObjectId
    mode: FileMode
    blob_bytes: _Count | None
    generated: GeneratedMarker

    @model_validator(mode="after")
    def valid_side(self) -> Self:
        canonical_path(self.path)
        if (self.mode == "160000") != (self.blob_bytes is None):
            raise ValueError("Only gitlinks have no blob size")
        return self


class ChangedFile(_Record):
    change_type: ChangeType
    old: FileSide | None
    new: FileSide | None
    added_lines: _Count | None
    deleted_lines: _Count | None
    binary: bool
    oversized: bool

    @model_validator(mode="after")
    def valid_change(self) -> Self:
        if (self.old is None) != (self.change_type == "added") or (
            (self.new is None) != (self.change_type == "deleted")
        ):
            raise ValueError("File sides must match the change type")
        if any(
            (count is None) != self.binary
            for count in (self.added_lines, self.deleted_lines)
        ):
            raise ValueError("Only binary changes omit both line counts")
        if self.oversized != any(
            side.blob_bytes is not None and side.blob_bytes > OVERSIZED_BYTES
            for side in (self.old, self.new)
            if side is not None
        ):
            raise ValueError("Oversized flag must match the blob sizes")
        return self


class FileCoverage(ChangedFile):
    outcome: Literal["complete"]
    highest_completed_check: Literal["inventory_only"]
    required_investigation_ids: _Empty


class InventoryError(_Record):
    code: InventoryErrorCode
    message: Annotated[str, Field(min_length=1, max_length=1024)]


class InventoryUsage(_Record):
    model_calls: _Zero
    input_tokens: _Zero
    output_tokens: _Zero
    estimated_cost_usd_micros: _Zero
    unaccounted_reserved_usd_micros: _Zero


class _InventoryResult(_Record):
    schema_version: _Version
    policy_version: _Version
    comparison: PinnedComparison
    scope: Literal["inventory"]
    recommendation: Literal["none"]
    discussion_status: Literal["not_requested"]
    usage: InventoryUsage
    investigation_coverage: _Empty
    evidence_index: _Empty
    findings: _Empty
    private_leads: _Empty
    linked_existing_threads: _Empty


class CompleteInventory(_InventoryResult):
    status: Literal["complete"]
    stop_reason: Literal["finished"]
    error: None
    file_coverage: Annotated[tuple[FileCoverage, ...], Field(max_length=MAX_CHANGES)]


class FailedInventory(_InventoryResult):
    status: Literal["failed"]
    stop_reason: Literal["work_limit", "fatal_error"]
    error: InventoryError
    file_coverage: _Empty

    @model_validator(mode="after")
    def matching_stop_reason(self) -> Self:
        if (self.stop_reason == "work_limit") != (self.error.code == "work_limit"):
            raise ValueError("Work limit error and stop reason must agree")
        return self


class ReviewerResult(
    RootModel[
        Annotated[CompleteInventory | FailedInventory, Field(discriminator="status")]
    ]
):
    model_config = ConfigDict(strict=True, frozen=True, revalidate_instances="always")
