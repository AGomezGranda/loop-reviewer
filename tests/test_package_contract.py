"""Verify import boundaries and keep environment access in CLI wiring."""

import ast
import sys
from collections.abc import Iterator
from importlib.util import resolve_name
from pathlib import Path

import pytest

_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "loop_reviewer"
_REVIEW = f"{_PACKAGE.name}.review"
_INBOX = f"{_PACKAGE.name}.inbox"
_CLI_ROOT = _PACKAGE / "cli" / "__init__.py"
_ENVIRONMENT_APIS = {"environ", "environb", "getenv", "getenvb", "putenv", "unsetenv"}


def _imported_names(path: Path) -> Iterator[tuple[int, str]]:
    relative_path = path.relative_to(_PACKAGE)
    package = ".".join((_PACKAGE.name, *relative_path.parent.parts))
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            module = resolve_name("." * node.level + (node.module or ""), package)
            for alias in node.names:
                yield node.lineno, f"{module}.{alias.name}"


def _within(name: str, root: str) -> bool:
    return name == root or name.startswith(f"{root}.")


def _import_violations(path: Path, *allowed: str) -> list[str]:
    return [
        f"{path.relative_to(_PACKAGE)}:{line}: {name}"
        for line, name in _imported_names(path)
        if name.partition(".")[0] not in sys.stdlib_module_names
        and not any(_within(name, root) for root in allowed)
    ]


def test_review_workflow_imports_only_stdlib_and_review_modules() -> None:
    violations = [
        violation
        for path in (_PACKAGE / "review").rglob("*.py")
        for violation in _import_violations(path, _REVIEW)
    ]
    assert not violations, "Unexpected review imports:\n" + "\n".join(violations)


def test_inbox_imports_review_only_through_its_read_contract_and_types() -> None:
    paths = list((_PACKAGE / "inbox").rglob("*.py"))
    assert paths, "Missing inbox workflow package"
    allowed = (
        _INBOX,
        f"{_REVIEW}.model.ReviewIdentity",
        f"{_REVIEW}.model.RunStatus",
        f"{_REVIEW}.pull_request.PullRequestRef",
        f"{_REVIEW}.store.ReviewActivity",
        f"{_REVIEW}.store.ReviewActivityReader",
    )
    violations = [v for path in paths for v in _import_violations(path, *allowed)]
    assert not violations, "Unexpected inbox imports:\n" + "\n".join(violations)


def test_cli_handlers_do_not_import_integrations_or_persistence_ports() -> None:
    wiring_only = (
        f"{_PACKAGE.name}.composition",
        f"{_PACKAGE.name}.integrations",
        f"{_INBOX}.store",
        f"{_REVIEW}.store",
    )
    errors = {
        f"{_INBOX}.store.InboxStoreError",
        f"{_INBOX}.store.InboxSettingsChanged",
        f"{_REVIEW}.store.DraftConflict",
        f"{_REVIEW}.store.ReviewStoreError",
        f"{_REVIEW}.store.UnconfirmedPublication",
    }
    violations = [
        f"{path.relative_to(_PACKAGE)}:{line}: {name}"
        for path in _CLI_ROOT.parent.rglob("*.py")
        for line, name in _imported_names(path)
        if any(_within(name, module) for module in wiring_only)
        and name not in errors
        and (path != _CLI_ROOT or name != f"{_PACKAGE.name}.composition.Workflows")
    ]
    assert not violations, (
        "CLI handlers must call workflows, not integrations or stores:\n"
        + "\n".join(violations)
    )


def test_cli_output_imports_only_stdlib_and_review_modules() -> None:
    violations = _import_violations(_PACKAGE / "cli" / "output.py", _REVIEW)
    assert not violations, "Unexpected CLI output imports:\n" + "\n".join(violations)


def test_inbox_output_imports_only_stdlib_inbox_and_review_modules() -> None:
    violations = _import_violations(
        _PACKAGE / "cli" / "inbox_output.py", _INBOX, _REVIEW
    )
    assert not violations, "Unexpected inbox output imports:\n" + "\n".join(violations)


def _direct_open_calls(source: str) -> list[int]:
    return sorted(
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "open"
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param('open("example")', [1], id="bare-builtin"),
        pytest.param('def read():\n    with open("example"): pass', [2], id="nested"),
        pytest.param('actions.open(ref)\nlabel = "open"', [], id="method-and-text"),
    ],
)
def test_direct_open_check(source: str, expected: list[int]) -> None:
    assert _direct_open_calls(source) == expected


def test_workflows_do_not_call_builtin_open_directly() -> None:
    violations = [
        f"{path.relative_to(_PACKAGE)}:{line}"
        for workflow in ("review", "inbox")
        for path in sorted((_PACKAGE / workflow).rglob("*.py"))
        for line in _direct_open_calls(path.read_text(encoding="utf-8"))
    ]
    assert not violations, "Direct open() in a workflow:\n" + "\n".join(violations)


def test_process_environment_is_accessed_only_at_composition_root() -> None:
    violations: list[str] = []
    for path in _PACKAGE.rglob("*.py"):
        if path == _CLI_ROOT:
            continue
        nodes = tuple(ast.walk(ast.parse(path.read_text(encoding="utf-8"))))
        os_names = {
            name.asname or name.name
            for node in nodes
            if isinstance(node, ast.Import)
            for name in node.names
            if name.name == "os"
        }
        for node in nodes:
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "os"
                and any(
                    name.name in _ENVIRONMENT_APIS or name.name == "*"
                    for name in node.names
                )
            ):
                violations.append(f"{path.relative_to(_PACKAGE)}:{node.lineno}")
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in os_names
                and node.attr in _ENVIRONMENT_APIS
            ):
                violations.append(f"{path.relative_to(_PACKAGE)}:{node.lineno}")
    assert not violations, (
        "Direct environment access outside CLI wiring:\n" + "\n".join(violations)
    )


def test_local_http_only_imports_transport_and_workflows() -> None:
    allowed = (
        f"{_PACKAGE.name}.local_http",
        _INBOX,
        _REVIEW,
        "fastapi",
        "pydantic",
        "starlette",
        "uvicorn",
    )
    violations = [
        violation
        for path in (_PACKAGE / "local_http").rglob("*.py")
        for violation in _import_violations(path, *allowed)
    ]
    assert not violations, "Unexpected HTTP adapter imports:\n" + "\n".join(violations)
