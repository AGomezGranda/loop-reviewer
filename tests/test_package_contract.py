"""Verify import boundaries and keep environment access in CLI wiring."""

import ast
import subprocess
import sys
from collections.abc import Iterator
from importlib.util import resolve_name
from pathlib import Path

import pytest

_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "loop_reviewer"
_REVIEW = f"{_PACKAGE.name}.review"
_REVIEWER = f"{_PACKAGE.name}.reviewer"
_REPORT = f"{_REVIEWER}.hosted_report_contract.HostedReport"
_SOURCE_NAMES = f"{_PACKAGE.name}.source_names"
_FEEDBACK_TEXT = f"{_PACKAGE.name}.feedback_text"
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


def test_review_workflow_uses_reviewer_only_at_its_boundary() -> None:
    reviewer_imports = {
        "model.py": (_REPORT,),
        "hosted_report.py": (
            _REPORT,
            f"{_REVIEWER}.discussion_contract.DiscussionSnapshotV1",
            f"{_REVIEWER}.full_review_v6.FullReviewRequestV6",
            f"{_REVIEWER}.inventory_contract.InventoryBudget",
            f"{_REVIEWER}.inventory_contract.PinnedComparison",
            f"{_REVIEWER}.inventory_contract.canonical_path",
            f"{_REVIEWER}.local_check_contract.SourceLocation",
            f"{_REVIEWER}.model_contract.ReviewModel",
            f"{_REVIEWER}.pinned_source.PinnedSource",
            f"{_REVIEWER}.semantic_review.review_full_v6",
        ),
    }
    violations = [
        v
        for path in (_PACKAGE / "review").rglob("*.py")
        for v in _import_violations(
            path,
            _REVIEW,
            _SOURCE_NAMES,
            _FEEDBACK_TEXT,
            *reviewer_imports.get(path.name, ()),
        )
    ]
    assert not violations, "Unexpected review imports:\n" + "\n".join(violations)


def test_reviewer_policy_imports_only_stdlib_and_reviewer_contracts() -> None:
    contracts = {
        "discussion_contract.py",
        "discussion_report.py",
        "full_review_contract.py",
        "full_review_v6.py",
        "hosted_report_contract.py",
        "inventory_contract.py",
        "local_check_contract.py",
        "model_contract.py",
        "semantic_contract.py",
    }
    violations = [
        violation
        for path in (_PACKAGE / "reviewer").glob("*.py")
        for violation in [
            *_import_violations(
                path,
                _REVIEWER,
                _SOURCE_NAMES,
                _FEEDBACK_TEXT,
                *(("pydantic",) if path.name in contracts else ()),
            ),
            *(
                f"{path.relative_to(_PACKAGE)}:{line}: {name}"
                for line, name in _imported_names(path)
                if any(
                    _within(name, root)
                    for root in (
                        f"{_REVIEWER}.integrations",
                        "ast",
                        "importlib.machinery",
                        "sys",
                    )
                )
            ),
        ]
    ]
    assert not violations, "Unexpected reviewer policy imports:\n" + "\n".join(
        violations
    )


def test_shared_helpers_import_only_stdlib() -> None:
    assert not _import_violations(_PACKAGE / "integrations" / "review_process.py")
    assert not _import_violations(_PACKAGE / "source_names.py")
    assert not _import_violations(_PACKAGE / "feedback_text.py")


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


def test_cli_output_imports_only_stdlib_review_modules_and_the_report() -> None:
    violations = _import_violations(_PACKAGE / "cli" / "output.py", _REVIEW, _REPORT)
    assert not violations, "Unexpected CLI output imports:\n" + "\n".join(violations)


def test_inbox_output_imports_only_stdlib_inbox_and_review_modules() -> None:
    violations = _import_violations(
        _PACKAGE / "cli" / "inbox_output.py", _INBOX, _REVIEW
    )
    assert not violations, "Unexpected inbox output imports:\n" + "\n".join(violations)


@pytest.mark.parametrize("command", ["inventory", "check", "semantic"])
def test_installed_standalone_command_loads_without_hosted_modules(
    command: str,
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """\
import sys
from importlib.metadata import distribution

for module in ('cli', 'composition', 'review', 'inbox', 'local_http'):
    sys.modules[f'loop_reviewer.{module}'] = None
entry, = (ep for ep in distribution('loop-reviewer').entry_points if ep.name == sys.argv[1])
sys.argv = [entry.name, '--help']
try:
    entry.load()()
except SystemExit as error:
    assert error.code == 0
assert not any(
    name.startswith('loop_reviewer.integrations.')
    and name != 'loop_reviewer.integrations.review_process'
    for name in sys.modules
)
""",
            f"loop-reviewer-{command}",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()


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
        for workflow in ("review", "inbox", "reviewer")
        for path in sorted((_PACKAGE / workflow).glob("*.py"))
        for line in _direct_open_calls(path.read_text(encoding="utf-8"))
    ]
    assert not violations, "Direct open() in a workflow:\n" + "\n".join(violations)


def test_process_environment_is_accessed_only_at_composition_root() -> None:
    violations: list[str] = []
    for path in _PACKAGE.rglob("*.py"):
        if path in (
            _CLI_ROOT,
            _PACKAGE / "inventory_cli.py",
            _PACKAGE / "local_checks_cli.py",
            _PACKAGE / "semantic_review_cli.py",
        ):
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
        for violation in _import_violations(
            path,
            *allowed,
            *((_REPORT,) if path.name in {"review.py", "history.py"} else ()),
        )
    ]
    assert not violations, "Unexpected HTTP adapter imports:\n" + "\n".join(violations)
