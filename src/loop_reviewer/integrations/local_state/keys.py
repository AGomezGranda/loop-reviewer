"""Encode stable local keys for review storage and locking."""

import json

from loop_reviewer.review.model import ReviewIdentity, ReviewKey


def _identity_parts(
    identity: ReviewIdentity,
) -> tuple[str, int, int, str] | tuple[str, str, str, int, str]:
    repository = identity.repository
    if isinstance(repository, int):
        return identity.host, repository, identity.number, identity.account
    return identity.host, *repository, identity.number, identity.account


def identity_key(identity: ReviewIdentity) -> str:
    return json.dumps(_identity_parts(identity), separators=(",", ":"))


def comparison_key(key: ReviewKey) -> str:
    comparison = key.comparison
    return json.dumps(
        (
            *_identity_parts(key.identity),
            comparison.target_branch,
            comparison.merge_base,
            comparison.head,
        ),
        separators=(",", ":"),
    )
