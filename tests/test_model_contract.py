import json
from pathlib import Path

import pytest

from loop_reviewer.reviewer.integrations.openai_review import provider_schema
from loop_reviewer.reviewer.model_contract import (
    ModelCall,
    ModelKind,
    parse_reply,
    reply_schema,
)

CALL = ModelCall("investigation", "i00001", "fixture")
REPLY = {
    "schema_version": 1,
    "investigation_id": "i00001",
    "kind": "submit_candidates",
    "reviewed_file_ids": ["p00001"],
    "candidates": [
        {
            "file_id": "p00001",
            "changed_behavior": "changes the result",
            "trigger": "input",
            "consequence": "observable result",
            "proposed_impact": "unknown",
            "citations": [{"role": "head_change", "evidence_id": "e00001"}],
            "proposed_anchor_id": None,
        }
    ],
}


@pytest.mark.parametrize(
    "case",
    [
        "version",
        "bool-version",
        "unknown",
        "enum",
        "id",
        "bound",
        "duplicate",
        "missing-nullable",
        "duplicate-key",
        "wrong-unit",
    ],
)
def test_strict_investigation_reply(case: str) -> None:
    payload = json.loads(json.dumps(REPLY))
    match case:
        case "version":
            payload["schema_version"] = 2
        case "bool-version":
            payload["schema_version"] = True
        case "unknown":
            payload["confidence"] = "high"
        case "enum":
            payload["candidates"][0]["proposed_impact"] = "severe"
        case "id":
            payload["reviewed_file_ids"] = ["/path.py"]
        case "bound":
            payload["candidates"][0]["trigger"] = "x" * 1025
        case "duplicate":
            payload["reviewed_file_ids"] *= 2
        case "missing-nullable":
            del payload["candidates"][0]["proposed_anchor_id"]
        case "wrong-unit":
            payload["investigation_id"] = "i00002"
    raw = json.dumps(payload)
    if case == "duplicate-key":
        raw = '{"schema_version": 1,' + raw[1:]
    with pytest.raises(ValueError):
        parse_reply(CALL, raw)
    assert parse_reply(CALL, json.dumps(REPLY)).schema_version == 1


@pytest.mark.parametrize("kind", ["investigation", "verification"])
def test_provider_schema_matches_fixture(kind: ModelKind) -> None:
    root = Path(__file__).resolve().parents[1]
    assert (
        root / "tests" / "fixtures" / f"provider-{kind}-v1.json"
    ).read_text() == json.dumps(provider_schema(kind), indent=2) + "\n"
    schema = json.loads(json.dumps(provider_schema(kind)))
    local_schema = json.loads(json.dumps(reply_schema(kind)))
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == schema["properties"].keys()
    if kind == "verification":
        assert schema["properties"]["assessment"]["enum"] == [
            "supports",
            "contradicts",
            "insufficient_evidence",
        ]
    else:
        candidate = schema["$defs"]["Candidate"]
        assert set(candidate["required"]) == candidate["properties"].keys()
        assert candidate["properties"]["proposed_anchor_id"] == {
            "anyOf": [{"type": "string"}, {"type": "null"}]
        }
        assert (
            candidate["properties"]["proposed_impact"]["enum"]
            == local_schema["$defs"]["Candidate"]["properties"]["proposed_impact"][
                "enum"
            ]
        )
