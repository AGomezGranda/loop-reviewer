import pytest

from loop_reviewer.reviewer.integrations.git_hunks import decode_hunks, one_sided_hunks
from loop_reviewer.reviewer.pinned_source import LineSpan


@pytest.mark.parametrize(
    ("base", "head", "spans", "patch"),
    [
        pytest.param(
            None,
            b"a\nb",
            (LineSpan(0, 0), LineSpan(1, 2)),
            b"@@ -0,0 +1,2 @@\n+a\n+b\n\\ No newline at end of file\n",
            id="add",
        ),
        pytest.param(
            b"one\vline\n",
            None,
            (LineSpan(1, 1), LineSpan(0, 0)),
            b"@@ -1 +0,0 @@\n-one\vline\n",
            id="delete",
        ),
        pytest.param(b"same\n", b"same\n", None, b"", id="pure-rename"),
        pytest.param(
            b"a\nb\n",
            b"c\nd\n",
            (LineSpan(1, 2), LineSpan(1, 2)),
            b"@@ -1,2 +1,2 @@\n-a\n-b\n+c\n+d\n",
            id="adjacent-edits",
        ),
    ],
)
def test_git_hunk_coordinates(
    base: bytes | None,
    head: bytes | None,
    spans: tuple[LineSpan, LineSpan] | None,
    patch: bytes,
) -> None:
    if base is None or head is None:
        blob = head if base is None else base
        assert blob is not None
        hunks = one_sided_hunks(blob, added=base is None, limit=1024)
        assert hunks == decode_hunks(patch)
    else:
        hunks = decode_hunks(patch)
    if spans is None:
        assert hunks == ()
    else:
        assert isinstance(hunks, tuple)
        (hunk,) = hunks
        assert (hunk.old_span, hunk.new_span) == spans
        assert hunk.patch == patch


@pytest.mark.parametrize(
    "patch",
    [
        b"@@ -1 +1 @@\n-a\n+b",
        b"@@ -1,2 +1 @@\n-a\n+b\n",
        b"@@ -1 +1 @@\n\\ No newline at end of file\n-a\n+b\n",
        b"diff --git a/abc b/def\n",
    ],
)
def test_incomplete_patch_is_not_empty_diff(patch: bytes) -> None:
    with pytest.raises(ValueError):
        decode_hunks(patch)
