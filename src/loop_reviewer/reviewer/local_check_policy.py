"""Required compiler coverage and the head-only regression promotion rule."""

from loop_reviewer.reviewer.inventory_contract import ChangedFile, FileSide, decode_path
from loop_reviewer.reviewer.pinned_source import SourceRange
from loop_reviewer.reviewer.python_context import Diagnostic


def supported_python(side: FileSide | None) -> bool:
    return (
        side is not None
        and side.mode in ("100644", "100755")
        and decode_path(side.path).endswith(b".py")
    )


def regression_candidate(
    change: ChangedFile, base: Diagnostic | None, head: Diagnostic | None
) -> SourceRange | None:
    if (
        change.binary
        or not supported_python(change.new)
        or head is None
        or head.result != "syntax_error"
    ):
        return None
    if change.old is None:
        if change.change_type != "added":
            return None
    elif (
        not supported_python(change.old)
        or base is None
        or base.result != "passed"
        or base.tool_version != head.tool_version
    ):
        return None
    return head.location


def decide_compile_regression(
    change: ChangedFile,
    base: Diagnostic | None,
    head: Diagnostic | None,
    head_changed_lines: set[int],
) -> SourceRange | None:
    location = regression_candidate(change, base, head)
    if location and all(
        line in head_changed_lines
        for line in range(location.start_line, location.end_line + 1)
    ):
        return location
    return None
