"""Inventory pinned commits through a private, read-only Git object view."""

import os
import re
import tempfile
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from stat import S_ISREG
from subprocess import CompletedProcess, TimeoutExpired
from time import monotonic

from loop_reviewer.integrations.review_process import (
    OutputLimitExceeded,
    capture_review_process,
    remove_review_scratch,
    with_absolute_path,
)
from loop_reviewer.reviewer.integrations.git_hunks import (
    decode_blob_diff,
    one_sided_hunks,
)
from loop_reviewer.reviewer.inventory import (
    BlobReadFailure,
    InventorySourceError,
    RevisionSide,
)
from loop_reviewer.reviewer.inventory_contract import (
    MAX_CHANGES,
    OVERSIZED_BYTES,
    ChangedFile,
    ChangeType,
    FileMode,
    FileSide,
    GeneratedMarker,
    PinnedComparison,
    canonical_path,
    encode_path,
)
from loop_reviewer.reviewer.pinned_source import (
    Hunk,
    PinnedTextBlob,
    ReadFailure,
    SearchPaths,
)

_METADATA_BYTES = 8 * 1024 * 1024
_INTAKE_SECONDS = 60
_BLOB_SECONDS = 10
_EVIDENCE_SECONDS = 5
_PATCH_BYTES = 64 * 1024
_TEXT_BYTES = 128 * 1024
_REGULAR = ("100644", "100755")
_ATTRIBUTE = b"linguist-generated"
_MODES: dict[bytes, FileMode] = {
    b"100644": "100644",
    b"100755": "100755",
    b"120000": "120000",
    b"160000": "160000",
}
_CHANGES: dict[bytes, ChangeType] = {
    b"A": "added",
    b"D": "deleted",
    b"M": "modified",
    b"R": "renamed",
    b"T": "type_changed",
}


@dataclass(frozen=True)
class _Git:
    cwd: Path
    env: Mapping[str, str]

    def run(
        self, args: list[str], *, timeout: float, limit: int, input: bytes | None = None
    ) -> CompletedProcess[bytes]:
        return capture_review_process(
            ["git", "-c", f"core.attributesFile={os.devnull}", *args],
            cwd=self.cwd,
            env=self.env,
            timeout=timeout,
            output_limit=limit,
            input=input,
        )

    def metadata(
        self, deadline: float, args: list[str], *, input: bytes | None = None
    ) -> CompletedProcess[bytes]:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise _work_limit()
        try:
            return self.run(args, timeout=remaining, limit=_METADATA_BYTES, input=input)
        except (OutputLimitExceeded, TimeoutExpired) as error:
            raise _work_limit() from error
        except FileNotFoundError as error:
            raise InventorySourceError(
                "git_unavailable",
                "Install Git 2.43 or later and make sure it is on PATH.",
            ) from error
        except OSError as error:
            raise InventorySourceError(
                "git_error", "Could not run Git for the pinned comparison."
            ) from error


def _work_limit() -> InventorySourceError:
    return InventorySourceError(
        "work_limit", "Git intake exceeded the metadata, change count, or time limit."
    )


def _invalid_metadata() -> InventorySourceError:
    return InventorySourceError(
        "invalid_inventory", "Git returned incomplete or invalid file metadata."
    )


def _output(result: CompletedProcess[bytes], purpose: str) -> bytes:
    if result.returncode:
        raise InventorySourceError(
            "git_error", f"Git could not {purpose} for the pinned comparison."
        )
    return result.stdout


def _environment(environment: Mapping[str, str]) -> dict[str, str]:
    return with_absolute_path(
        {
            "PATH": environment.get("PATH", os.defpath),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )


def _object_store(git: _Git, deadline: float) -> tuple[Path, str]:
    objects = git.metadata(
        deadline, ["rev-parse", "--path-format=absolute", "--git-path", "objects"]
    )
    storage = git.metadata(deadline, ["rev-parse", "--show-object-format"])
    try:
        directory = Path(os.fsdecode(objects.stdout.removesuffix(b"\n")))
        if (
            objects.returncode
            or storage.returncode
            or not directory.is_absolute()
            or not directory.is_dir()
        ):
            raise ValueError
        formats = {b"sha1\n": "sha1", b"sha256\n": "sha256"}
        return directory.resolve(strict=True), formats[storage.stdout]
    except (KeyError, OSError, ValueError) as error:
        raise InventorySourceError(
            "invalid_repository",
            "The directory does not provide a readable Git object store.",
        ) from error


def _copy_shallow_boundaries(git: _Git, deadline: float, private: Path) -> None:
    output = _output(
        git.metadata(
            deadline, ["rev-parse", "--path-format=absolute", "--git-path", "shallow"]
        ),
        "locate shallow boundaries",
    )
    path = Path(os.fsdecode(output.removesuffix(b"\n")))
    try:
        if not path.is_absolute():
            raise ValueError("Expected an absolute shallow file path")
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except FileNotFoundError:
            return
        with os.fdopen(descriptor, "rb") as stream:
            if not S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Expected a regular shallow file")
            boundaries = stream.read(_METADATA_BYTES + 1)
        if len(boundaries) > _METADATA_BYTES:
            raise _work_limit()
        (private / "shallow").write_bytes(boundaries)
    except (OSError, ValueError) as error:
        raise InventorySourceError(
            "git_error", "Could not read shallow boundaries for the pinned comparison."
        ) from error


def _verify(git: _Git, deadline: float, comparison: PinnedComparison) -> None:
    pins = [comparison.merge_base.encode("ascii"), comparison.head.encode("ascii")]
    objects = _batch_check(git, deadline, pins, "%(objectname) %(objecttype)")
    for pin, result in zip(pins, objects, strict=True):
        if result == pin + b" missing":
            raise InventorySourceError(
                "missing_commit",
                "A pinned commit is missing from the local object store.",
            )
        if result != pin + b" commit":
            raise InventorySourceError(
                "invalid_comparison", "Both pinned IDs must identify commit objects."
            )
    ancestry = git.metadata(
        deadline,
        ["merge-base", "--is-ancestor", comparison.merge_base, comparison.head],
    )
    if ancestry.returncode == 1:
        raise InventorySourceError(
            "invalid_comparison",
            "The pinned merge base is not an ancestor of the head.",
        )
    _output(ancestry, "verify ancestry")


def _batch_check(
    git: _Git, deadline: float, ids: list[bytes], fields: str
) -> list[bytes]:
    if not ids:
        return []
    output = _output(
        git.metadata(
            deadline,
            ["cat-file", f"--batch-check={fields}"],
            input=b"\n".join(ids) + b"\n",
        ),
        "inspect objects",
    )
    records = output.split(b"\n")
    if len(records) != len(ids) + 1 or records[-1]:
        raise _invalid_metadata()
    return records[:-1]


@dataclass(frozen=True)
class _Entry:
    path: bytes
    object_id: bytes
    mode: FileMode


@dataclass(frozen=True)
class _Change:
    kind: ChangeType
    old: _Entry | None
    new: _Entry | None


_PathTuple = tuple[bytes, ...]
_LineCounts = tuple[int | None, int | None]


def _entry(
    path: bytes, mode: bytes, oid: bytes, width: int, *, absent: bool
) -> _Entry | None:
    if len(oid) != width or not re.fullmatch(rb"[0-9a-f]+", oid):
        raise _invalid_metadata()
    zero = oid == b"0" * width
    if absent and mode == b"000000" and zero:
        return None
    if absent or zero or mode not in _MODES:
        raise _invalid_metadata()
    return _Entry(path, oid, _MODES[mode])


def _raw_change(
    header: bytes, records: Generator[bytes], width: int
) -> tuple[_PathTuple, _Change]:
    mode_old, mode_new, oid_old, oid_new, status = header[1:].split(b" ")
    kind = _CHANGES.get(status[:1])
    if (
        kind is None
        or (kind == "renamed" and not re.fullmatch(rb"R(?:100|0[0-9]{2})", status))
        or (kind != "renamed" and len(status) != 1)
    ):
        raise _invalid_metadata()
    old_path = next(records)
    new_path = next(records) if kind == "renamed" else old_path
    key = (old_path, new_path) if kind == "renamed" else (old_path,)
    return key, _Change(
        kind,
        _entry(old_path, mode_old, oid_old, width, absent=kind == "added"),
        _entry(new_path, mode_new, oid_new, width, absent=kind == "deleted"),
    )


def _integer(value: bytes) -> int:
    if not value.isdigit() or len(value) > 19 or (number := int(value)) > 2**63 - 1:
        raise _invalid_metadata()
    return number


def _numstat(
    record: bytes, records: Generator[bytes]
) -> tuple[_PathTuple, _LineCounts]:
    added, deleted, path = record.split(b"\t", 2)
    key = (path,) if path else (next(records), next(records))
    if (added, deleted) == (b"-", b"-"):
        return key, (None, None)
    return key, (_integer(added), _integer(deleted))


def _nul_records(output: bytes) -> Generator[bytes]:
    start = 0
    while start < len(output):
        end = output.find(b"\0", start)
        if end < 0:
            raise _invalid_metadata()
        yield output[start:end]
        start = end + 1


def _decode_diff(output: bytes, width: int) -> list[tuple[_Change, _LineCounts]]:
    changes: dict[_PathTuple, _Change] = {}
    joined: list[tuple[_Change, _LineCounts]] = []
    records = _nul_records(output)
    try:
        for header in records:
            if header.startswith(b":") and not joined:
                if len(changes) >= MAX_CHANGES:
                    raise _work_limit()
                key, change = _raw_change(header, records, width)
                if key in changes:
                    raise _invalid_metadata()
                changes[key] = change
            else:
                key, counts = _numstat(header, records)
                joined.append((changes.pop(key), counts))
    except (KeyError, StopIteration, ValueError) as error:
        raise _invalid_metadata() from error
    if changes:
        raise _invalid_metadata()
    return joined


def _sizes(git: _Git, deadline: float, entries: list[_Entry]) -> dict[bytes, int]:
    ids = sorted({entry.object_id for entry in entries if entry.mode != "160000"})
    records = _batch_check(
        git, deadline, ids, "%(objectname) %(objecttype) %(objectsize)"
    )
    sizes: dict[bytes, int] = {}
    for oid, line in zip(ids, records, strict=True):
        fields = line.split(b" ")
        if len(fields) != 3 or fields[:2] != [oid, b"blob"]:
            raise _invalid_metadata()
        sizes[oid] = _integer(fields[2])
    return sizes


def _marker(value: bytes) -> GeneratedMarker:
    # shortcut: generated detection uses committed Linguist markers, add another evidence source when missed generated churn affects review
    if value in (b"set", b"true"):
        return "marked"
    if value in (b"unset", b"false"):
        return "unmarked"
    return "unknown"


def _attributes(
    git: _Git, deadline: float, commit: str, entries: list[_Entry]
) -> dict[bytes, GeneratedMarker]:
    if not entries:
        return {}
    paths = [entry.path for entry in entries]
    if len(paths) != len(set(paths)) or not all(paths):
        raise _invalid_metadata()
    output = _output(
        git.metadata(
            deadline,
            [
                "check-attr",
                f"--source={commit}",
                "-z",
                "--stdin",
                _ATTRIBUTE.decode("ascii"),
            ],
            input=b"\0".join(paths) + b"\0",
        ),
        "read committed attributes",
    )
    records = _nul_records(output)
    attributes: dict[bytes, GeneratedMarker] = {}
    for path in paths:
        actual, name, value = (
            next(records, None),
            next(records, None),
            next(records, None),
        )
        if actual != path or name != _ATTRIBUTE or value is None:
            raise _invalid_metadata()
        attributes[path] = _marker(value)
    if next(records, None) is not None:
        raise _invalid_metadata()
    return attributes


def _file_side(
    entry: _Entry, sizes: dict[bytes, int], attributes: dict[bytes, GeneratedMarker]
) -> FileSide:
    return FileSide(
        path=encode_path(entry.path),
        object_id=entry.object_id.decode("ascii"),
        mode=entry.mode,
        blob_bytes=None if entry.mode == "160000" else sizes[entry.object_id],
        generated=attributes[entry.path],
    )


def _inventory(
    git: _Git, deadline: float, pins: PinnedComparison
) -> tuple[ChangedFile, ...]:
    diff = _output(
        git.metadata(
            deadline,
            [
                f"--attr-source={pins.head}",
                "diff",
                "--raw",
                "--numstat",
                "-z",
                "--no-abbrev",
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                "--ignore-submodules=none",
                "--submodule=short",
                "-M50%",
                "-l1000",
                pins.merge_base,
                pins.head,
                "--",
            ],
        ),
        "enumerate changes",
    )
    changes = _decode_diff(diff, len(pins.head))
    old_entries = [change.old for change, _ in changes if change.old is not None]
    new_entries = [change.new for change, _ in changes if change.new is not None]
    sizes = _sizes(git, deadline, old_entries + new_entries)
    old_attrs = _attributes(git, deadline, pins.merge_base, old_entries)
    new_attrs = _attributes(git, deadline, pins.head, new_entries)
    rows: list[ChangedFile] = []
    try:
        for change, (added, deleted) in changes:
            old = _file_side(change.old, sizes, old_attrs) if change.old else None
            new = _file_side(change.new, sizes, new_attrs) if change.new else None
            rows.append(
                ChangedFile(
                    change_type=change.kind,
                    old=old,
                    new=new,
                    added_lines=added,
                    deleted_lines=deleted,
                    binary=added is None,
                    oversized=any(
                        (side.blob_bytes or 0) > OVERSIZED_BYTES
                        for side in (old, new)
                        if side is not None
                    ),
                )
            )
    except ValueError as error:
        raise _invalid_metadata() from error
    if monotonic() >= deadline:
        raise _work_limit()
    return tuple(rows)


@dataclass(frozen=True)
class _EvidenceGit:
    git: _Git
    deadline: float

    def run(
        self, args: list[str], limit: int, *, input: bytes | None = None
    ) -> CompletedProcess[bytes] | ReadFailure:
        remaining = self.deadline - monotonic()
        if remaining <= 0 or limit < 0:
            return "limit_reached"
        try:
            result = self.git.run(args, timeout=remaining, limit=limit, input=input)
        except OutputLimitExceeded, TimeoutExpired:
            return "limit_reached"
        except OSError:
            return "unavailable"
        return "limit_reached" if monotonic() >= self.deadline else result

    def read_blob(self, oid: str, size: int) -> bytes | ReadFailure:
        result = self.run(["cat-file", "blob", oid], size)
        if isinstance(result, str):
            return result
        if result.returncode or len(result.stdout) != size:
            return "unavailable"
        if b"\0" in result.stdout[:8000]:
            return "unsupported"
        return result.stdout

    def blob_size(self, oid: str) -> int | ReadFailure:
        raw = oid.encode("ascii")
        result = self.run(
            ["cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
            96,
            input=raw + b"\n",
        )
        if isinstance(result, str):
            return result
        prefix = raw + b" blob "
        if (
            result.returncode
            or not result.stdout.startswith(prefix)
            or not result.stdout.endswith(b"\n")
        ):
            return "unavailable"
        try:
            return _integer(result.stdout[len(prefix) : -1])
        except InventorySourceError:
            return "unavailable"

    def tree_blob(self, commit: str, raw_path: bytes) -> _Entry | ReadFailure:
        result = self.run(
            [
                "--literal-pathspecs",
                "ls-tree",
                "-z",
                "--full-tree",
                commit,
                "--",
                os.fsdecode(raw_path),
            ],
            8192,
        )
        if isinstance(result, str):
            return result
        if result.returncode:
            return "unavailable"
        if not result.stdout:
            return "not_found"
        metadata, separator, name = result.stdout.partition(b"\t")
        if not separator or name != raw_path + b"\0":
            return "unavailable"
        fields = metadata.split(b" ")
        if len(fields) != 3:
            return "unavailable"
        mode, kind, oid = fields
        if mode not in (b"100644", b"100755"):
            return "unsupported"
        if (
            kind != b"blob"
            or len(oid) != len(commit)
            or not re.fullmatch(rb"[0-9a-f]+", oid)
        ):
            return "unavailable"
        return _Entry(raw_path, oid, _MODES[mode])


def _change_paths(change: ChangedFile) -> tuple[str | None, str | None]:
    return (
        change.old.path if change.old else None,
        change.new.path if change.new else None,
    )


class _LocalSource:
    def __init__(self, git: _Git, deadline: float, pins: PinnedComparison) -> None:
        self._git = git
        self._deadline = deadline
        self._pins = pins
        self._rows: tuple[ChangedFile, ...] | None = None
        self._files: dict[tuple[RevisionSide, str], FileSide] = {}
        self._changes: dict[tuple[str | None, str | None], ChangedFile] = {}
        self._closed = False

    def inventory(self) -> tuple[ChangedFile, ...]:
        if self._closed:
            raise InventorySourceError("git_error", "The pinned Git source is closed.")
        if self._rows is None:
            self._rows = _inventory(self._git, self._deadline, self._pins)
            for row in self._rows:
                self._changes[_change_paths(row)] = row
                if row.old is not None:
                    self._files["base", row.old.path] = row.old
                if row.new is not None:
                    self._files["head", row.new.path] = row.new
        return self._rows

    def read_blob(
        self, side: RevisionSide, path: str, *, max_bytes: int
    ) -> bytes | BlobReadFailure:
        if self._closed:
            return "unavailable"
        entry = self._files.get((side, path))
        if entry is None or entry.blob_bytes is None:
            return "not_found"
        limit = min(max_bytes, OVERSIZED_BYTES)
        if entry.blob_bytes > limit:
            return "limit_reached"
        try:
            result = self._git.run(
                ["cat-file", "blob", entry.object_id],
                timeout=_BLOB_SECONDS,
                limit=limit,
            )
        except OutputLimitExceeded, TimeoutExpired:
            return "limit_reached"
        except OSError:
            return "unavailable"
        if result.returncode or len(result.stdout) != entry.blob_bytes:
            return "unavailable"
        return result.stdout

    def _operation(self, timeout_seconds: float) -> _EvidenceGit | ReadFailure:
        if self._closed:
            return "unavailable"
        if not isfinite(timeout_seconds) or timeout_seconds <= 0:
            return "limit_reached"
        return _EvidenceGit(
            self._git, monotonic() + min(timeout_seconds, _EVIDENCE_SECONDS)
        )

    def _commit(self, side: RevisionSide) -> str:
        return self._pins.merge_base if side == "base" else self._pins.head

    def read_text_blob(
        self, side: RevisionSide, path: str, *, max_bytes: int, timeout_seconds: float
    ) -> PinnedTextBlob | ReadFailure:
        git = self._operation(timeout_seconds)
        if isinstance(git, str):
            return git
        try:
            raw_path = canonical_path(path)
        except ValueError:
            return "unsupported"
        if max_bytes < 0:
            return "limit_reached"
        commit = self._commit(side)
        entry = git.tree_blob(commit, raw_path)
        if isinstance(entry, str):
            return entry
        oid = entry.object_id.decode("ascii")
        size = git.blob_size(oid)
        if isinstance(size, str):
            return size
        if size > min(max_bytes, _TEXT_BYTES):
            return "limit_reached"
        data = git.read_blob(oid, size)
        if isinstance(data, str):
            return data
        return PinnedTextBlob(side, commit, path, oid, data)

    def hunks(
        self, change: ChangedFile, *, max_bytes: int, timeout_seconds: float
    ) -> tuple[Hunk, ...] | ReadFailure:
        git = self._operation(timeout_seconds)
        if isinstance(git, str):
            return git
        confirmed = self._changes.get(_change_paths(change))
        if confirmed is None or confirmed != change:
            return "not_found"
        old, new = confirmed.old, confirmed.new
        sides = [side for side in (old, new) if side is not None]
        if confirmed.binary or any(side.mode not in _REGULAR for side in sides):
            return "unsupported"
        if confirmed.oversized or max_bytes < 0:
            return "limit_reached"
        limit = min(max_bytes, _PATCH_BYTES)
        if old is None or new is None:
            (side,) = sides
            assert side.blob_bytes is not None
            if side.blob_bytes > limit:
                return "limit_reached"
            data = git.read_blob(side.object_id, side.blob_bytes)
            return (
                data
                if isinstance(data, str)
                else one_sided_hunks(data, added=old is None, limit=limit)
            )
        if old.object_id == new.object_id:
            return ()
        result = git.run(
            [
                "diff",
                "--diff-algorithm=myers",
                "--unified=3",
                "--no-indent-heuristic",
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                "--no-renames",
                "--full-index",
                "--src-prefix=a/",
                "--dst-prefix=b/",
                old.object_id,
                new.object_id,
                "--",
            ],
            limit,
        )
        if isinstance(result, str):
            return result
        if result.returncode:
            return "unavailable"
        try:
            return decode_blob_diff(result.stdout, old.object_id, new.object_id)
        except ValueError:
            return "unavailable"

    def search(
        self,
        side: RevisionSide,
        literal: str,
        *,
        max_paths: int,
        max_bytes: int,
        timeout_seconds: float,
        regular_text: bool = False,
    ) -> SearchPaths | ReadFailure:
        git = self._operation(timeout_seconds)
        if isinstance(git, str):
            return git
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,127}", literal):
            return "unsupported"
        if max_paths < 0 or max_bytes <= 0:
            return "limit_reached"
        commit = self._commit(side)
        result = git.run(
            [
                "grep",
                "-F",
                "-l",
                "-z",
                "-I",
                "-e",
                literal,
                commit,
                "--",
                *([] if regular_text else [":(glob)**/*.py"]),
            ],
            min(max_bytes, _PATCH_BYTES),
        )
        if isinstance(result, str):
            return result
        if result.returncode == 1 and not result.stdout:
            return SearchPaths((), False, retrieved_bytes=0)
        if result.returncode or not result.stdout:
            return "unavailable"
        prefix = commit.encode("ascii") + b":"
        paths: dict[bytes, str] = {}
        try:
            for record in _nul_records(result.stdout):
                if not record.startswith(prefix):
                    return "unavailable"
                raw = record[len(prefix) :]
                path = encode_path(raw)
                canonical_path(path)
                if (not regular_text and not raw.endswith(b".py")) or raw in paths:
                    return "unavailable"
                paths[raw] = path
        except ValueError, InventorySourceError:
            return "unavailable"
        limit = min(max_paths, 20)
        return SearchPaths(
            tuple(paths[raw] for raw in sorted(paths)[:limit]),
            len(paths) > limit,
            len(result.stdout),
        )

    def close(self) -> None:
        self._closed = True
        self._rows = None
        self._files.clear()
        self._changes.clear()


@contextmanager
def open_local_comparison(
    repository: Path,
    comparison: PinnedComparison,
    environment: Mapping[str, str],
) -> Generator[_LocalSource]:
    deadline = monotonic() + _INTAKE_SECONDS
    try:
        cwd = repository.resolve(strict=True)
        if not cwd.is_dir():
            raise NotADirectoryError
    except (OSError, ValueError) as error:
        raise InventorySourceError(
            "invalid_repository", "The repository directory is not accessible."
        ) from error
    env = _environment(environment)
    original = _Git(cwd, env)
    objects, object_format = _object_store(original, deadline)
    if len(comparison.head) != (40 if object_format == "sha1" else 64):
        raise InventorySourceError(
            "invalid_comparison",
            "The pinned IDs do not match the repository's hash format.",
        )
    try:
        private = Path(tempfile.mkdtemp(prefix="loop-reviewer-inventory-"))
    except OSError as error:
        raise InventorySourceError(
            "git_error", "Could not create a private directory for Git intake."
        ) from error
    try:
        _output(
            _Git(private, env).metadata(
                deadline,
                ["init", "--bare", "--template=", f"--object-format={object_format}"],
            ),
            "initialize the private object view",
        )
        _copy_shallow_boundaries(original, deadline, private)
        git = _Git(private, env | {"GIT_OBJECT_DIRECTORY": str(objects)})
        _verify(git, deadline, comparison)
        source = _LocalSource(git, deadline, comparison)
        try:
            yield source
        finally:
            source.close()
    finally:
        remove_review_scratch(private, "Git inventory")
