"""Define exact publication snapshots and submission state."""

from dataclasses import dataclass, replace
from typing import Literal, TypeGuard
from unicodedata import category
from uuid import UUID

from loop_reviewer.review.model import (
    CompletedReview,
    DraftTarget,
    Finding,
    FindingLocation,
    PublicationAction,
    PublicationReceipt,
    ReviewSelection,
    SavedDraft,
)

PublicationStatus = Literal["preview", "unconfirmed", "rejected", "published"]


class PublicationError(Exception):
    pass


@dataclass(frozen=True)
class PublicationId:
    value: str

    def __post_init__(self) -> None:
        try:
            parsed = UUID(self.value)
            if parsed.version == 4 and str(parsed) == self.value:
                return
        except ValueError:
            pass
        raise PublicationError("Expected a canonical UUID4 preview ID.")

    @property
    def marker(self) -> str:
        return f"<!-- loop-reviewer:{self.value} -->"


@dataclass(frozen=True)
class InlineComment:
    path: str
    line: int
    body: str

    def __post_init__(self) -> None:
        FindingLocation(self.path, self.line)
        _require_body(self.body)


@dataclass(frozen=True)
class PublicationPreview:
    id: PublicationId
    source: DraftTarget
    selection: ReviewSelection
    actor_id: int
    action: PublicationAction
    body: str
    comments: tuple[InlineComment, ...]

    def __post_init__(self) -> None:
        if type(self.actor_id) is not int or self.actor_id < 1:
            raise PublicationError("Expected a positive GitHub user ID.")
        if self.selection.ref.repository_id is None:
            raise PublicationError(
                "Publication requires a stable GitHub repository ID."
            )
        require_action(self.action, self.selection.account, self.selection.author)
        if not self.body.startswith(f"{self.id.marker}\n\n"):
            raise PublicationError("The publication body has an invalid marker.")
        _require_body(self.body)
        if len({(comment.path, comment.line) for comment in self.comments}) != len(
            self.comments
        ):
            raise PublicationError("Inline comments must use distinct locations.")


@dataclass(frozen=True)
class PublicationRecord:
    preview: PublicationPreview
    status: PublicationStatus
    ambiguity_seen: bool
    receipt: PublicationReceipt | None

    def __post_init__(self) -> None:
        if self.status not in ("preview", "unconfirmed", "rejected", "published"):
            raise PublicationError("Invalid publication status.")
        if (
            (self.receipt is not None) != (self.status == "published")
            or (self.receipt is not None and self.receipt.action != self.preview.action)
            or (self.ambiguity_seen and self.status not in ("unconfirmed", "published"))
        ):
            raise PublicationError("Inconsistent publication record.")

    def reserve(self, expected: PublicationStatus) -> PublicationRecord:
        if self.status != expected or self.status == "published":
            raise PublicationError(
                "The publication state changed. Recover or preview again."
            )
        return replace(
            self, status="unconfirmed", ambiguity_seen=self.status == "unconfirmed"
        )

    def reject(self) -> PublicationRecord:
        if self.status != "unconfirmed":
            raise PublicationError("Only a reserved publication can be rejected.")
        return self if self.ambiguity_seen else replace(self, status="rejected")

    def publish(self, receipt: PublicationReceipt) -> PublicationRecord:
        if self.status == "published" and self.receipt == receipt:
            return self
        if self.status != "unconfirmed" or receipt.action != self.preview.action:
            raise PublicationError(
                "The receipt does not match the reserved publication."
            )
        return replace(self, status="published", receipt=receipt)

    def matches_completion(
        self, completed: CompletedReview | None
    ) -> TypeGuard[CompletedReview]:
        return (
            self.receipt is not None
            and completed is not None
            and completed.receipt == self.receipt
            and completed.source == self.preview.source
            and completed.selection == self.preview.selection
        )


def require_action(action: PublicationAction, account: str, author: str) -> None:
    if action not in ("comment", "approve"):
        raise PublicationError("Choose comment or approve explicitly.")
    if action == "approve" and account.lower() == author.lower():
        raise PublicationError(
            "You cannot approve your own pull request. Preview a comment or use draft finish."
        )


def build_preview(
    id: PublicationId,
    saved: SavedDraft,
    selection: ReviewSelection,
    actor_id: int,
    action: PublicationAction,
    anchors: frozenset[FindingLocation],
) -> PublicationPreview:
    inline: dict[FindingLocation, list[str]] = {}
    fallback: list[str] = []
    for finding in saved.draft.ordered_findings():
        location = finding.location
        if location is not None and location in anchors:
            inline.setdefault(location, []).append(
                _finding_block(finding, location=None)
            )
        else:
            fallback.append(_finding_block(finding, location=location))
    body = f"{id.marker}\n\n{saved.draft.summary}"
    if fallback:
        body += "\n\n## Findings\n\n" + "\n\n".join(fallback)
    return PublicationPreview(
        id,
        DraftTarget(saved.id, saved.draft_version),
        selection,
        actor_id,
        action,
        body,
        tuple(
            InlineComment(loc.path, loc.right_line, "\n\n".join(blocks))
            for loc, blocks in inline.items()
        ),
    )


def _finding_block(finding: Finding, *, location: FindingLocation | None) -> str:
    label = f"**{finding.severity.capitalize()}**"
    if location is not None:
        label += f" (`{location.path}:{location.right_line}`)"
    return f"{label}\n\n{finding.condition}\n\n{finding.consequence}"


def _require_body(body: str) -> None:
    if not body.strip() or any(
        char not in "\n\t" and category(char) in {"Cc", "Cs"} for char in body
    ):
        raise PublicationError("Invalid publication body.")
    if len(body) > 65_536:
        raise PublicationError(
            "Review and inline bodies must each be at most 65,536 characters."
        )
