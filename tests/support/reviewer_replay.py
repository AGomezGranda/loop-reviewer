"""Build the frozen development replay without calling a reviewer model."""

import argparse
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterator
from itertools import chain, zip_longest
from pathlib import Path
from typing import Literal, TypedDict

from loop_reviewer.integrations.git_comparison import _configuration
from loop_reviewer.integrations.review_process import (
    OutputLimitExceeded,
    capture_review_process,
    with_absolute_path,
)
from loop_reviewer.reviewer.integrations.git_inventory import open_local_comparison
from loop_reviewer.reviewer.inventory import validated_inventory
from loop_reviewer.reviewer.inventory_contract import ChangedFile, PinnedComparison
from loop_reviewer.reviewer.pinned_source import Hunk, PinnedSource
from loop_reviewer.reviewer.semantic_limits import hunk_retrieval_bytes
from tests.support.inventory_git import PATH_ENV, commit, git, init, revision

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "reviewer_replay"
_LIMIT = 64 * 1024
_MAX_PATCH_BYTES = 128 * 1024
_BINARY = re.compile(r"(?:^|\n)(?:Binary files .* differ|GIT binary patch)")
_BASE_DATE = "2000-01-01T00:00:00+00:00"
_HEAD_DATE = "2000-01-02T00:00:00+00:00"
_CommitFiles = dict[str, bytes]
_LegacyStatus = Literal[
    "eligible", "unsupported_size", "unsupported_encoding", "unsupported_binary"
]
_PRODUCER = b"def fetch_items():\n    return None\n"
_CHANGED = b"def fetch_items():\n    return []\n"
_CONSUMER = b"from producer import fetch_items\n\ndef first_item():\n    items = fetch_items()\n    if items is None:\n        return 'missing'\n    return items[0]\n"


def _cases() -> Iterator[tuple[str, str, _CommitFiles, _CommitFiles]]:
    yield (
        "test-only",
        "development",
        {
            "producer.py": _PRODUCER,
            "tests/test_items.py": b"from producer import fetch_items\n\ndef test_missing():\n    assert fetch_items() is None\n",
        },
        {
            "producer.py": _PRODUCER,
            "tests/test_items.py": b"from producer import fetch_items\n\ndef test_missing():\n    result = fetch_items()\n    assert result is None\n",
        },
    )
    yield (
        "internal-refactor",
        "development",
        {
            "producer.py": b"def scale(n):\n    return (n + 1) * 10\n",
        },
        {
            "producer.py": b"def scale(n):\n    adjusted = n + 1\n    return adjusted * 10\n",
        },
    )
    yield (
        "syntax-regression",
        "development",
        {
            "producer.py": b"def first_item():\n    return 'missing'\n",
        },
        {
            "producer.py": b"def first_item():\n    break\n",
        },
    )
    yield (
        "unchanged-consumer",
        "development",
        {
            "producer.py": _PRODUCER,
            "consumer.py": _CONSUMER,
        },
        {
            "producer.py": _CHANGED,
            "consumer.py": _CONSUMER,
        },
    )
    yield (
        "consumer-adaptation",
        "holdout",
        {
            "producer.py": _PRODUCER,
            "consumer.py": _CONSUMER,
        },
        {
            "producer.py": _CHANGED,
            "consumer.py": _CONSUMER.replace(b"items is None", b"not items"),
        },
    )
    before, after = _large_case()
    yield "generated-churn", "holdout", before, after


def _mixed_cases() -> Iterator[tuple[str, str, _CommitFiles, _CommitFiles]]:
    yield (
        "mixed-clean",
        "development",
        {
            "settings.json": b'{"theme":"default","showClock":true}\n',
            "screen.ts": b'export const title = "Clock";\n',
        },
        {
            "settings.json": b'{\n  "showClock": true,\n  "theme": "default"\n}\n',
            "screen.ts": b'export const title = "Clock";\n',
        },
    )
    yield (
        "single-file-total",
        "development",
        {
            "cart.ts": b"export function total(price: number, quantity: number) {\n  return price * quantity;\n}\n"
        },
        {
            "cart.ts": b"export function total(price: number, quantity: number) {\n  return price + quantity;\n}\n"
        },
    )
    command = b'import { defaultCurrency } from "./defaultCurrency.js";\nexport function formatInvoice(total) { return defaultCurrency + total; }\n'
    yield (
        "deleted-currency",
        "holdout",
        {
            "defaultCurrency.js": b'export const defaultCurrency = "USD ";\n',
            "invoice.js": command,
        },
        {"invoice.js": command},
    )
    consumer = b'import { readFileSync } from "node:fs";\nconst settings = JSON.parse(readFileSync("./invoice.json", "utf8"));\nexport const invoicePrefix = settings.invoicePrefix.toUpperCase();\n'
    yield (
        "cross-file-settings",
        "holdout",
        {"invoice.json": b'{"invoicePrefix":"inv"}\n', "print.mjs": consumer},
        {"invoice.json": b'{"invoicePrefix":null}\n', "print.mjs": consumer},
    )


def _large_case() -> tuple[_CommitFiles, _CommitFiles]:
    before = {
        ".gitattributes": b"generated/** linguist-generated\n",
        "consumer.py": _CONSUMER,
    }
    after = before.copy()
    for index in range(93):
        path = f"generated/churn-{index:02d}.py"
        before[path] = (
            (f"# old autogenerated section {index:02d} " + "x" * 64 + "\n").encode()
        ) * 25
        after[path] = (
            (f"# new autogenerated section {index:02d} " + "y" * 64 + "\n").encode()
        ) * 25
    before["producer.py"] = b"# previous generated annotations\n" * 24 + _PRODUCER
    after["producer.py"] = b"# revised generated annotations\n" * 24 + _CHANGED
    before["deleted.py"] = b"# deleted\n" * 100
    before["before-rename.py"] = b"# move this source file\n"
    after["after-rename.py"] = before["before-rename.py"]
    after["generated/added.py"] = b"# added\n" * 100
    after["generated/oversized.py"] = (b"#" + b"z" * 11_000 + b"\n") * 100
    after["binary.bin"] = b"\0binary\n"
    after["empty.py"] = b""
    return before, after


def _case_commit(
    repo: Path, files: _CommitFiles, previous: _CommitFiles, *, message: str, date: str
) -> str:
    paths = sorted(files.keys() | previous.keys())
    for name in paths:
        path = repo / name
        if name not in files:
            path.unlink()
        elif files[name] != previous.get(name):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(files[name])
            path.chmod(0o644)
    git(repo, "add", "-A", "--", *paths)
    return commit(
        repo,
        stage=False,
        message=message,
        environment={"TZ": "UTC", "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date},
    )


def _make_repo(
    repo: Path, case: str, base_files: _CommitFiles, head_files: _CommitFiles
) -> PinnedComparison:
    repo.mkdir()
    init(repo)
    git(repo, "config", "core.autocrlf", "false")
    git(repo, "config", "core.filemode", "true")
    base = _case_commit(
        repo, base_files, {}, message=f"Replay {case}: base", date=_BASE_DATE
    )
    git(repo, "branch", "base", base)
    head = _case_commit(
        repo, head_files, base_files, message=f"Replay {case}: head", date=_HEAD_DATE
    )
    return PinnedComparison(merge_base=base, head=head)


def _json(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _file(change: ChangedFile) -> dict[str, object]:
    def metadata(side: str) -> object:
        value = change.old if side == "base" else change.new
        return (
            None
            if value is None
            else {"path": value.path, "mode": value.mode, "generated": value.generated}
        )

    return {
        "old": metadata("base"),
        "new": metadata("head"),
        "change_type": change.change_type,
        "added_lines": change.added_lines,
        "deleted_lines": change.deleted_lines,
        "binary": change.binary,
        "oversized": change.oversized,
    }


def _omission(index: int, reason: str, count: int | None) -> dict[str, object]:
    return {"file": index, "reason": reason, "omitted_hunks": count}


def bounded_diff_only(
    comparison: PinnedComparison,
    rows: tuple[ChangedFile, ...],
    source: PinnedSource,
    *,
    max_bytes: int = _LIMIT,
) -> tuple[bytes, int]:
    limit = min(max_bytes, _LIMIT)
    files: list[dict[str, object]] = []
    hunks: list[dict[str, object]] = []
    omissions: dict[int, dict[str, object]] = {}
    patches: list[tuple[Hunk, ...]] = []
    omitted_paths = len(rows)
    retrieved_bytes = 0

    def serialize() -> bytes:
        return _json(
            {
                "comparison": comparison.model_dump(),
                "files": files,
                "hunks": hunks,
                "omissions": [omissions[k] for k in sorted(omissions)],
                "omitted_paths": omitted_paths,
            }
        )

    if len(serialize()) > limit:
        raise ValueError("The byte allowance does not fit the comparison header")
    for row in rows:
        index = len(files)
        omitted_paths -= 1
        files.append(_file(row))
        # Reserve enough room to record either a failed read or the full hunk count.
        omissions[index] = _omission(index, "payload_limit", _LIMIT)
        if len(serialize()) > limit:
            omissions.pop(index)
            files.pop()
            omitted_paths += 1
            continue
        patch = source.hunks(row, max_bytes=_LIMIT, timeout_seconds=5)
        retrieved_bytes += hunk_retrieval_bytes(patch, _LIMIT)
        if isinstance(patch, str):
            omissions[index] = _omission(index, patch, None)
            patches.append(())
        else:
            patches.append(patch)
            if patch:
                omissions[index] = _omission(index, "payload_limit", len(patch))
            else:
                omissions.pop(index)

    blocked: set[int] = set()
    for ordinal, batch in enumerate(zip_longest(*patches)):
        for index, hunk in enumerate(batch):
            if hunk is None or index in blocked:
                continue
            remaining = len(patches[index]) - ordinal - 1
            previous = omissions.pop(index)
            if remaining:
                omissions[index] = _omission(index, "payload_limit", remaining)
            hunks.append(
                {
                    "file": index,
                    "patch": hunk.patch.decode("utf-8", errors="backslashreplace"),
                }
            )
            if len(serialize()) > limit:
                hunks.pop()
                omissions[index] = previous
                blocked.add(index)
    return serialize(), retrieved_bytes


def _legacy(repo: Path, pins: PinnedComparison) -> tuple[_LegacyStatus, bytes | None]:
    environment = with_absolute_path(
        PATH_ENV
        | {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    try:
        result = capture_review_process(
            [
                "git",
                *_configuration(),
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                "--no-renames",
                pins.merge_base,
                pins.head,
                "--",
            ],
            cwd=repo,
            env=environment,
            timeout=60,
            output_limit=_MAX_PATCH_BYTES,
        )
    except OutputLimitExceeded:
        return "unsupported_size", None
    if result.returncode:
        raise RuntimeError("Git failed while preparing the legacy replay input")
    try:
        text = result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return "unsupported_encoding", None
    if _BINARY.search(text):
        return "unsupported_binary", None
    return "eligible", result.stdout


class ManifestEntry(TypedDict):
    id: str
    split: str
    merge_base: str
    head: str
    expected_coverage_count: int
    expected_changed_lines: int
    input_sha256: str
    diff_retrieval_bytes: int
    legacy_status: _LegacyStatus


def _prepare(output: Path, *, policy: Literal[3, 5] = 3) -> list[ManifestEntry]:
    for directory in ("repos", "diff-only", "legacy"):
        (output / directory).mkdir()
    entries: list[ManifestEntry] = []
    legacy: dict[str, _LegacyStatus] = {}
    cases = _cases() if policy == 3 else chain(_cases(), _mixed_cases())
    for name, split, base, head in cases:
        repo = output / "repos" / name
        pins = _make_repo(repo, name, base, head)
        with open_local_comparison(repo, pins, PATH_ENV) as source:
            rows = validated_inventory(source, pins)
            payload, retrieved_bytes = bounded_diff_only(pins, rows, source)
        (output / "diff-only" / f"{name}.json").write_bytes(payload)
        eligibility, patch = _legacy(repo, pins)
        legacy[name] = eligibility
        if patch is not None:
            (output / "legacy" / f"{name}.patch").write_bytes(patch)
        entries.append(
            {
                "id": name,
                "split": split,
                "merge_base": pins.merge_base,
                "head": pins.head,
                "expected_coverage_count": len(rows),
                "expected_changed_lines": sum(
                    (r.added_lines or 0) + (r.deleted_lines or 0) for r in rows
                ),
                "input_sha256": hashlib.sha256(payload).hexdigest(),
                "diff_retrieval_bytes": retrieved_bytes,
                "legacy_status": eligibility,
            }
        )
    (output / "legacy.json").write_bytes(_json(legacy))
    return entries


def _verify_existing(output: Path, staged: Path) -> None:
    if output.is_symlink() or not output.is_dir():
        raise ValueError("Replay output must be a directory, not a symlink")
    for source in (
        *sorted((staged / "diff-only").glob("*.json")),
        *sorted((staged / "legacy").glob("*.patch")),
        staged / "legacy.json",
    ):
        target = output / source.relative_to(staged)
        if (
            not target.is_file()
            or target.stat().st_size != source.stat().st_size
            or target.read_bytes() != source.read_bytes()
        ):
            raise ValueError(f"Existing replay differs: {target}")
    for repo in sorted((staged / "repos").iterdir()):
        actual = output / "repos" / repo.name
        if not actual.is_dir() or any(
            revision(actual, ref) != revision(repo, ref) for ref in ("HEAD", "base")
        ):
            raise ValueError(f"Existing replay pins differ: {actual}")


def replay_manifest(policy: Literal[3, 5]) -> list[ManifestEntry]:
    manifest: list[ManifestEntry] = json.loads(
        (FIXTURES / "manifest.json").read_bytes()
    )
    if policy == 5:
        manifest.extend(json.loads((FIXTURES / "manifest-v5-extra.json").read_bytes()))
    return manifest


def build_replay(output: Path, *, policy: Literal[3, 5] = 3) -> None:
    manifest = replay_manifest(policy)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".reviewer-replay-", dir=output.parent
    ) as temp:
        staged = Path(temp) / "replay"
        staged.mkdir()
        entries = _prepare(staged, policy=policy)
        if entries != manifest:
            raise ValueError(
                "Generated replay pins, coverage, retrieval charges, or payload digests do not match the frozen manifest"
            )
        if output.exists() or output.is_symlink():
            _verify_existing(output, staged)
        else:
            staged.rename(output)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare the frozen local reviewer replay."
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--policy", type=int, choices=(3, 5), default=3)
    args = parser.parse_args()
    try:
        build_replay(args.output, policy=args.policy)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Replay preparation failed: {error}\n")


if __name__ == "__main__":
    main()
