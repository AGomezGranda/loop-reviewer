from loop_reviewer.reviewer.integrations.git_hunks import decode_hunks
from loop_reviewer.reviewer.pinned_source import PinnedTextBlob
from loop_reviewer.reviewer.python_context import (
    Call,
    Declaration,
    Diagnostic,
    ImportAlias,
    PythonAnalysis,
    Symbol,
    decoded_lines,
    rank_references,
    select_declarations,
)


def analysis(
    path: str,
    source: bytes,
    *,
    base: bool = False,
    declarations: tuple[Declaration, ...] = (),
    calls: tuple[Call, ...] = (),
    imports: tuple[ImportAlias, ...] = (),
) -> PythonAnalysis:
    blob = PinnedTextBlob(
        "base" if base else "head", ("a" if base else "b") * 40, path, "c" * 40, source
    )
    return PythonAnalysis(
        Diagnostic(blob, "3.15.2", "passed", "Compiled", None),
        declarations,
        calls,
        imports,
    )


def test_hunks_select_old_and_new_declarations() -> None:
    base = analysis(
        "old.py",
        b"",
        base=True,
        declarations=(
            Declaration("Outer", "class", 2, 5),
            Declaration("Outer.item", "function", 3, 5),
            Declaration("removed", "function", 6, 7),
        ),
    )
    head = analysis(
        "new.py",
        b"",
        declarations=(
            Declaration("Outer", "class", 2, 5),
            Declaration("Outer.item", "function", 3, 5),
        ),
    )
    hunks = decode_hunks(
        b"@@ -1,7 +1,5 @@\n-before\n+after\n class Outer:\n- @old\n+ @new\n  def item():\n-  return None\n-def removed():\n- pass\n+  return []\n"
    )
    module, paired, deleted = select_declarations(hunks, base, head)
    assert module.name is None
    assert module.base is not None and module.base.start_line == 1
    assert paired.name == "Outer.item"
    assert (
        paired.base is not None
        and paired.base.start_line == 3
        and paired.base.path == "old.py"
    )
    assert (
        paired.head is not None
        and paired.head.end_line == 5
        and paired.head.path == "new.py"
    )
    assert (
        deleted.name == "removed" and deleted.base is not None and deleted.head is None
    )


def test_python_excerpts_follow_encoding_and_preserve_bad_bytes() -> None:
    encoded = analysis("latin.py", b"# coding: latin-1\r\n# caf\xe9").diagnostic.blob
    assert decoded_lines(encoded) == ("# coding: latin-1\r\n", "# café")
    invalid = analysis("invalid.py", b"# \\xff \xff\npass\n").diagnostic.blob
    assert decoded_lines(invalid) == (r"# \\xff \xff" + "\n", "pass\n")


def test_reference_ranking_labels_evidence_without_binding() -> None:
    refs = rank_references(
        Symbol("parse", "producer.py"),
        [
            analysis("aaa.py", b"# parse is unrelated\n"),
            analysis(
                "tests/test_parser.py", b"parse()\n", calls=(Call("parse", 1, 1),)
            ),
            analysis(
                "consumer.py",
                b"from producer import parse\nif parse() is None:\n pass\n",
                calls=(Call("parse", 2, 2),),
                imports=(ImportAlias("parse", "producer.parse"),),
            ),
        ],
    )
    assert [(ref.role, ref.location.path, ref.line) for ref in refs] == [
        ("call_site", "consumer.py", 2),
        ("test_call", "tests/test_parser.py", 1),
        ("text_match", "aaa.py", 1),
    ]
