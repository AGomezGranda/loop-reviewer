"""Version-dispatched report contract shared by hosted readers."""

from typing import Annotated

from pydantic import Discriminator, Tag, TypeAdapter

from loop_reviewer.reviewer.full_review_contract import FullReviewResult
from loop_reviewer.reviewer.full_review_v6 import FullReviewResultV6


def _schema_version(report: object) -> int | None:
    if isinstance(report, (FullReviewResult, FullReviewResultV6)):
        return report.root.schema_version
    if isinstance(report, dict):
        version = report.get("schema_version")
        if type(version) is int:
            return version
    return None


type HostedReport = Annotated[
    Annotated[FullReviewResult, Tag(5)] | Annotated[FullReviewResultV6, Tag(6)],
    Discriminator(_schema_version),
]

_DECODER: TypeAdapter[HostedReport] = TypeAdapter(HostedReport)


def decode_hosted_report(payload: str) -> HostedReport:
    return _DECODER.validate_json(payload)
