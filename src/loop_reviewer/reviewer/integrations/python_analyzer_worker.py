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


def _producer(
    body: list[ast.stmt],
) -> tuple[str, Literal["None", "[]", "()"], int] | None:
    func = _function(body[0]) if len(body) == 1 else None
    if func is None or len(func.body) != 1 or not isinstance(func.body[0], ast.Return):
        return None
    statement = func.body[0]
    value = statement.value
    if isinstance(value, ast.Constant) and value.value is None:
        return func.name, "None", statement.lineno
    if isinstance(value, ast.List) and not value.elts:
        return func.name, "[]", statement.lineno
    if isinstance(value, ast.Tuple) and not value.elts:
        return func.name, "()", statement.lineno
    return None


def _name(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _caller(tree: list[ast.stmt]) -> tuple[str, str, str, bool, int] | None:
    if len(tree) != 2 or not isinstance(tree[0], ast.ImportFrom):
        return None
    imp = tree[0]
    if (
        imp.level
        or imp.module is None
        or len(imp.names) != 1
        or imp.names[0].name == "*"
    ):
        return None
    alias = imp.names[0].asname or imp.names[0].name
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
    return fn.name, imp.module, imp.names[0].name, guarded, fn.end_lineno


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
    empty_return = None
    indexed_call = None
    # CPython normalizes bare CR to LF; those offsets don't map to Git lines.
    if b"\r" in source.replace(b"\r\n", b""):
        location = None
    else:
        try:
            tree = ast.parse(source, filename="<pinned-source>")
            declarations, calls, imports, limited = _facts(tree)
            if result == "passed" and len(source) <= 16 * 1024:
                empty_return = _producer(tree.body)
                indexed_call = _caller(tree.body)
        except SyntaxError, RecursionError, ValueError:
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
                "empty_return": empty_return,
                "indexed_call": indexed_call,
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
