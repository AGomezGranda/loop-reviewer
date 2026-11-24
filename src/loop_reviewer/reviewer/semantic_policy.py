"""Fail-closed pilot policy: source witnesses, not agreement between prompts."""

import ast
import sys
from dataclasses import dataclass
from importlib.machinery import FrozenImporter
from typing import Literal

from loop_reviewer.reviewer.inventory_contract import decode_path
from loop_reviewer.reviewer.local_check_contract import SourceEvidence, SourceLocation
from loop_reviewer.reviewer.local_evidence import EvidenceIndex, LocalEvidenceWork
from loop_reviewer.reviewer.model_contract import Candidate, Impact, VerificationReplyV1
from loop_reviewer.reviewer.pinned_source import PinnedTextBlob, SourceRange
from loop_reviewer.reviewer.python_context import changed_lines, decoded_lines
from loop_reviewer.reviewer.semantic_inputs import (
    BundleFile,
    EvidenceBundle,
    ShownExcerpt,
)

IMPACT_ORDER: dict[Impact, int] = {
    "data_loss": 0,
    "access_control_failure": 0,
    "core_workflow_outage": 0,
    "material_incorrect_behavior": 1,
    "localized_recoverable_regression": 2,
    "unknown": 3,
}

# Observable predicates the pilot can prove without running the changed package.
WITNESS_INSTRUCTIONS = (
    "For an empty-result indexing claim the local checker can prove, phrase changed_behavior "
    "as '<producer module>.<function>() returns [] instead of None' (use () for an empty tuple), "
    "trigger as 'Call <consumer module>.<function>()', and consequence as "
    "'<consumer module>.<function>() raises IndexError'. Do not invent the caller or assume "
    "a name match proves binding. Report other plausible problems only with shown citations."
)


@dataclass(frozen=True)
class PrivateLead:
    id: str
    file_id: str
    candidate: Candidate
    missing_fact: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class Finding:
    id: str
    file_id: str
    candidate: Candidate
    evidence_ids: tuple[str, ...]
    anchor: SourceLocation | None
    equivalence: tuple[str, ...]
    severity: Literal["high", "medium", "low"] = "medium"
    python_witness: bool = True


@dataclass(frozen=True)
class Discard:
    id: str
    file_id: str
    reason: Literal["contradicted", "preexisting", "duplicate"]
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class CheckedCandidate:
    id: str
    file: BundleFile
    candidate: Candidate
    cited_ids: tuple[str, ...]
    anchor: SourceLocation | None


@dataclass(frozen=True)
class Witness:
    verdict: Literal["supports", "contradicted", "preexisting"]
    evidence_ids: tuple[str, ...]
    equivalence: tuple[str, ...]


@dataclass(frozen=True)
class VerificationPacket:
    entries: tuple[SourceEvidence, ...]
    witness: Witness | None


def candidate_order(
    candidates: tuple[Candidate, ...],
) -> tuple[tuple[str, Candidate], ...]:
    indexed = tuple(
        (f"c{i:05d}", candidate) for i, candidate in enumerate(candidates, 1)
    )
    return tuple(
        sorted(indexed, key=lambda pair: IMPACT_ORDER[pair[1].proposed_impact])
    )


def check_candidate(
    identifier: str,
    candidate: Candidate,
    shown_ids: frozenset[str],
    bundle: EvidenceBundle,
    work: LocalEvidenceWork,
) -> CheckedCandidate | PrivateLead:
    file = next(f for f in bundle.inventory if f.id == candidate.file_id)
    excerpts = {
        item.source.id: item
        for item in bundle.excerpts
        if item.source.id in shown_ids and file.id in item.file_ids
    }
    accepted: dict[str, ShownExcerpt] = {}
    for key in dict.fromkeys(c.evidence_id for c in candidate.citations):
        item = excerpts.get(key)
        if item is None or not source_resolves(item.source, work):
            return PrivateLead(
                identifier,
                file.id,
                candidate,
                "A citation did not resolve to the pinned source.",
                tuple(accepted),
            )
        accepted[key] = item
    head_change = any(
        c.role == "head_change"
        and accepted[c.evidence_id].source.range.side == "head"
        and accepted[c.evidence_id].changed_lines
        for c in candidate.citations
    )
    base_behavior = any(
        c.role == "base_behavior"
        and accepted[c.evidence_id].source.range.side == "base"
        and file.change.old is not None
        and accepted[c.evidence_id].source.range.path == file.change.old.path
        for c in candidate.citations
    )
    if file.change.old is None and file.change.new is not None:
        base_behavior = work.blob("base", file.change.new.path) == "not_found"
    if not head_change or not base_behavior:
        return PrivateLead(
            identifier,
            file.id,
            candidate,
            "Pinned base behavior and changed head source are required.",
            tuple(accepted),
        )
    proposed = excerpts.get(candidate.proposed_anchor_id or "")
    anchor = None
    if proposed is not None:
        loc = proposed.source.range
        side = file.change.old if loc.side == "base" else file.change.new
        if (
            side
            and (side.path, side.object_id) == (loc.path, loc.blob_id)
            and loc.start_line == loc.end_line
            and loc.start_line in proposed.changed_lines
            and source_resolves(proposed.source, work)
        ):
            patch = work.hunks(file.change)
            if not isinstance(patch, str) and any(
                loc.start_line in changed_lines(hunk, loc.side) for hunk in patch
            ):
                anchor = loc
    return CheckedCandidate(identifier, file, candidate, tuple(accepted), anchor)


def source_resolves(entry: SourceEvidence, work: LocalEvidenceWork) -> bool:
    loc = entry.range
    blob = work.blob(loc.side, loc.path)
    return (
        not isinstance(blob, str)
        and work.excerpt(
            SourceRange(
                loc.side,
                loc.commit_id,
                loc.path,
                loc.blob_id,
                loc.start_line,
                loc.end_line,
            )
        )
        == entry.text
    )


@dataclass(frozen=True)
class _Producer:
    name: str
    value: Literal["None", "[]", "()"]
    line: int


@dataclass(frozen=True)
class _Caller:
    name: str
    guarded: bool
    end: int


def _tree(blob: PinnedTextBlob) -> list[ast.stmt]:
    if len(blob.text) > 16 * 1024 or b"\r" in blob.text.replace(b"\r\n", b""):
        return []
    try:
        return ast.parse(blob.text).body
    except SyntaxError, RecursionError, ValueError:
        return []


def _function(node: ast.stmt) -> ast.FunctionDef | None:
    if isinstance(node, ast.FunctionDef) and not any(
        (
            node.decorator_list,
            node.type_params,
            node.returns,
            node.args.posonlyargs,
            node.args.args,
            node.args.kwonlyargs,
            node.args.vararg,
            node.args.kwarg,
        )
    ):
        return node
    return None


def _producer(blob: PinnedTextBlob) -> _Producer | None:
    body = _tree(blob)
    func = _function(body[0]) if len(body) == 1 else None
    if func is None or len(func.body) != 1 or not isinstance(func.body[0], ast.Return):
        return None
    statement = func.body[0]
    value = statement.value
    if isinstance(value, ast.Constant) and value.value is None:
        return _Producer(func.name, "None", statement.lineno)
    if isinstance(value, ast.List) and not value.elts:
        return _Producer(func.name, "[]", statement.lineno)
    if isinstance(value, ast.Tuple) and not value.elts:
        return _Producer(func.name, "()", statement.lineno)
    return None


def _name(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _caller(blob: PinnedTextBlob, module: str, name: str) -> _Caller | None:
    tree = _tree(blob)
    if len(tree) != 2 or not isinstance(tree[0], ast.ImportFrom):
        return None
    imp = tree[0]
    if (
        imp.level
        or imp.module != module
        or len(imp.names) != 1
        or imp.names[0].name != name
    ):
        return None
    alias = imp.names[0].asname or name
    fn = _function(tree[1])
    if fn is None or fn.name == alias or fn.end_lineno is None or len(fn.body) != 3:
        return None
    assign, guard, final = fn.body
    if (
        not isinstance(assign, ast.Assign)
        or len(assign.targets) != 1
        or not isinstance(assign.targets[0], ast.Name)
    ):
        return None
    name = assign.targets[0].id
    call = assign.value
    if (
        name == alias
        or not isinstance(call, ast.Call)
        or call.args
        or call.keywords
        or not _name(call.func, alias)
    ):
        return None
    if not isinstance(guard, ast.If) or guard.orelse or len(guard.body) != 1:
        return None
    early = guard.body[0]
    if not isinstance(early, ast.Return) or not isinstance(early.value, ast.Constant):
        return None
    test = guard.test
    guarded = (
        isinstance(test, ast.UnaryOp)
        and isinstance(test.op, ast.Not)
        and _name(test.operand, name)
    )
    none_guard = (
        isinstance(test, ast.Compare)
        and _name(test.left, name)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Is)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value is None
    )
    if (
        not (guarded or none_guard)
        or not isinstance(final, ast.Return)
        or not isinstance(final.value, ast.Subscript)
    ):
        return None
    index = final.value.slice
    if (
        not _name(final.value.value, name)
        or not isinstance(index, ast.Constant)
        or type(index.value) is not int
        or index.value < 0
    ):
        return None
    return _Caller(fn.name, guarded, fn.end_lineno)


def _module(path: str) -> str | None:
    # No package initialization, dynamic imports, reexports, or relative lookup is proven.
    raw = decode_path(path)
    if not raw.endswith(b".py") or not raw.isascii():
        return None
    name = raw[:-3].decode("ascii")
    if (
        not name.isidentifier()
        or name == "__init__"
        or name in sys.stdlib_module_names
        or name in sys.builtin_module_names
        or FrozenImporter.find_spec(name) is not None
    ):
        return None
    return name


def _claim(text: str) -> str:
    return " ".join(text.strip().removesuffix(".").split())


def _entry(
    evidence: EvidenceIndex, blob: PinnedTextBlob, start: int, end: int
) -> SourceEvidence | None:
    text = "".join(decoded_lines(blob)[start - 1 : end])
    loc = SourceRange(blob.side, blob.commit_id, blob.path, blob.blob_id, start, end)
    key = evidence.source("check_location", loc, text)
    return (
        None
        if key is None
        else SourceEvidence(
            kind="source",
            id=key,
            role="check_location",
            range=SourceLocation.from_range(loc),
            text=text,
        )
    )


def build_verification_packet(
    checked: CheckedCandidate,
    bundle: EvidenceBundle,
    work: LocalEvidenceWork,
    evidence: EvidenceIndex,
) -> VerificationPacket:
    file = checked.file.change
    by_id = {entry.source.id: entry.source for entry in bundle.excerpts}
    cited = tuple(by_id[identifier] for identifier in checked.cited_ids)
    if file.old is None or file.new is None or file.old.path != file.new.path:
        return VerificationPacket(cited, None)
    module = _module(file.new.path)
    if module is None:
        return VerificationPacket(cited, None)
    base_blob = work.blob("base", file.old.path, expected=file.old)
    head_blob = work.blob("head", file.new.path, expected=file.new)
    if isinstance(base_blob, str) or isinstance(head_blob, str):
        return VerificationPacket(cited, None)
    base, head = _producer(base_blob), _producer(head_blob)
    if base is None or head is None or base.name != head.name or head.value == "None":
        return VerificationPacket(cited, None)
    candidate = checked.candidate
    if _claim(candidate.changed_behavior) != _claim(
        f"{module}.{head.name}() returns {head.value} instead of None"
    ):
        return VerificationPacket(cited, None)
    # Both concrete returns must be named in the investigator's citations.
    roles = {(citation.role, citation.evidence_id) for citation in candidate.citations}
    if not all(
        any(
            (role, entry.id) in roles
            and (entry.range.path, entry.range.side, entry.range.blob_id)
            == (blob.path, blob.side, blob.blob_id)
            and entry.range.start_line <= line <= entry.range.end_line
            for entry in cited
        )
        for role, blob, line in (
            ("base_behavior", base_blob, base.line),
            ("head_change", head_blob, head.line),
        )
    ):
        return VerificationPacket(cited, None)
    patch = work.hunks(file)
    if isinstance(patch, str):
        return VerificationPacket(cited, None)
    changed = any(head.line in changed_lines(h, "head") for h in patch)
    matches = work.search("head", head.name)
    if isinstance(matches, str):
        return VerificationPacket(cited, None)
    for path in matches.paths:
        consumer = _module(path)
        if consumer is None or consumer == module:
            continue
        caller_blob = work.blob("head", path)
        if isinstance(caller_blob, str):
            continue
        caller = _caller(caller_blob, module, head.name)
        if (
            caller is None
            or _claim(candidate.trigger) != _claim(f"Call {consumer}.{caller.name}()")
            or _claim(candidate.consequence)
            != _claim(f"{consumer}.{caller.name}() raises IndexError")
        ):
            continue
        # In both arms check base reachability and the complete head caller as pinned source.
        before_blob = work.blob("base", path)
        before = (
            None
            if isinstance(before_blob, str)
            else _caller(before_blob, module, base.name)
        )
        if before is None or before.name != caller.name or isinstance(before_blob, str):
            break
        if any(
            work.blob(side, f"{name}/__init__.py") != "not_found"
            for side in ("base", "head")
            for name in (module, consumer)
        ):
            break
        verdict: Literal["supports", "contradicted", "preexisting"]
        if caller.guarded:
            verdict = "contradicted"
        elif base.value != "None" and not before.guarded:
            verdict = "preexisting"
        elif base.value == "None" and changed:
            verdict = "supports"
        else:
            break
        entries = (
            _entry(evidence, base_blob, base.line, base.line),
            _entry(evidence, head_blob, head.line, head.line),
            _entry(evidence, before_blob, 1, before.end),
            _entry(evidence, caller_blob, 1, caller.end),
        )
        if any(entry is None for entry in entries):
            break
        material = tuple(entry for entry in entries if entry is not None)
        return VerificationPacket(
            material,
            Witness(
                verdict,
                tuple(entry.id for entry in material),
                (file.new.path, head.name, path, caller.name, "IndexError"),
            ),
        )
    return VerificationPacket(cited, None)


def decide_candidate(
    checked: CheckedCandidate,
    packet: VerificationPacket,
    verification: VerificationReplyV1,
) -> Finding | PrivateLead | Discard:
    known = {entry.id for entry in packet.entries}
    ids = set(verification.evidence_ids)
    witness = packet.witness
    candidate = checked.candidate
    if (
        verification.candidate_id == checked.id
        and witness is not None
        and ids <= known
        and set(witness.evidence_ids) <= ids
    ):
        if witness.verdict in ("contradicted", "preexisting"):
            return Discard(
                checked.id, candidate.file_id, witness.verdict, witness.evidence_ids
            )
        if (
            witness.verdict == "supports"
            and verification.assessment == "supports"
            and candidate.proposed_impact == "material_incorrect_behavior"
        ):
            return Finding(
                checked.id,
                candidate.file_id,
                candidate,
                witness.evidence_ids,
                checked.anchor,
                witness.equivalence,
            )
    return PrivateLead(
        checked.id,
        candidate.file_id,
        candidate,
        "The packet did not establish the claimed behavior, reachability, consequence, and impact.",
        tuple(key for key in verification.evidence_ids if key in known)
        or checked.cited_ids,
    )
