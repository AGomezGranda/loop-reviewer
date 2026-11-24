"""Fixed stdlib-only worker. Treat stdin as source data, never as a program to run."""

import ast
import json
import sys
from typing import Literal

_FACT_LIMIT = 128
_OUTPUT_LIMIT = 65536


def _dotted(node: ast.expr) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute) and len(parts) < 128:
        parts.append(node.attr)
        node = node.value
    return ".".join((node.id, *reversed(parts))) if isinstance(node, ast.Name) else ""


def _imports(node: ast.Import | ast.ImportFrom) -> list[tuple[str, str]]:
    if isinstance(node, ast.Import):
        return [
            (
                alias.asname or alias.name.partition(".")[0],
                alias.name if alias.asname else alias.name.partition(".")[0],
            )
            for alias in node.names
        ]
    if node.level or node.module is None:
        return []
    return [
        (alias.asname or alias.name, f"{node.module}.{alias.name}")
        for alias in node.names
        if alias.name != "*"
    ]


def _facts(
    tree: ast.Module,
) -> tuple[
    list[tuple[str, str, int, int]],
    list[tuple[str, int, int]],
    list[tuple[str, str]],
    bool,
]:
    declarations: list[tuple[str, str, int, int]] = []
    calls: list[tuple[str, int, int]] = []
    imports: list[tuple[str, str]] = []
    limited = False
    pending: list[tuple[ast.AST, str]] = [(tree, "")]
    while pending:
        node, parent = pending.pop()
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            name = f"{parent}.{node.name}" if parent else node.name
            kind = (
                "class"
                if isinstance(node, ast.ClassDef)
                else "async_function"
                if isinstance(node, ast.AsyncFunctionDef)
                else "function"
            )
            if (
                node.end_lineno is not None
                and len(name) <= 512
                and len(declarations) < _FACT_LIMIT
            ):
                start = min(
                    [
                        node.lineno,
                        *(decorator.lineno for decorator in node.decorator_list),
                    ]
                )
                declarations.append((name, kind, start, node.end_lineno))
            else:
                limited = True
            parent = name
        elif isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name and node.end_lineno is not None:
                if len(name) <= 512 and len(calls) < _FACT_LIMIT:
                    calls.append((name, node.lineno, node.end_lineno))
                else:
                    limited = True
        elif isinstance(node, ast.Import | ast.ImportFrom):
            for alias, qualified in _imports(node):
                if (
                    len(alias) <= 512
                    and len(qualified) <= 512
                    and len(imports) < _FACT_LIMIT
                ):
                    imports.append((alias, qualified))
                else:
                    limited = True
        pending.extend(
            (child, parent) for child in reversed(list(ast.iter_child_nodes(node)))
        )
    return declarations, calls, imports, limited


def _main() -> None:
    source = sys.stdin.buffer.read(128 * 1024 + 1)
    if len(source) > 128 * 1024 or sys.implementation.name != "cpython":
        raise SystemExit(1)
    result: Literal["passed", "syntax_error"] = "passed"
    location = None
    message = "Source compiled without execution."
    try:
        compile(source, "<pinned-source>", "exec", dont_inherit=True, optimize=0)
    except SyntaxError as error:
        result = "syntax_error"
        message = error.msg[:1024]
        if error.lineno is not None and error.lineno > 0:
            location = error.lineno
    declarations: list[tuple[str, str, int, int]] = []
    calls: list[tuple[str, int, int]] = []
    imports: list[tuple[str, str]] = []
    limited = True
    # CPython normalizes bare CR to LF; those offsets don't map to Git lines.
    if b"\r" in source.replace(b"\r\n", b""):
        location = None
    else:
        try:
            declarations, calls, imports, limited = _facts(
                ast.parse(source, filename="<pinned-source>")
            )
        except SyntaxError, RecursionError:
            pass
    line_count = source.count(b"\n") + int(bool(source) and not source.endswith(b"\n"))
    if location is not None and location > line_count:
        location = None
    while True:
        output = json.dumps(
            {
                "version": ".".join(map(str, sys.version_info[:3])),
                "result": result,
                "location": location,
                "message": message,
                "declarations": declarations,
                "calls": calls,
                "imports": imports,
                "limited": limited,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8", errors="backslashreplace")
        if len(output) <= _OUTPUT_LIMIT:
            sys.stdout.buffer.write(output)
            return
        limited = True
        longest = max((declarations, calls, imports), key=len)
        del longest[len(longest) // 2 :]


if __name__ == "__main__":
    _main()
