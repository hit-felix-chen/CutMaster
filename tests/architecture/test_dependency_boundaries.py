from __future__ import annotations

import ast
import importlib.util
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "src" / "cutmaster"


def _module_name(path: Path) -> str:
    relative = path.relative_to(PACKAGE_ROOT.parent).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports(path: Path) -> Iterable[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    current_module = _module_name(path)
    package = (
        current_module
        if path.name == "__init__.py"
        else current_module.rpartition(".")[0]
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                yield node.lineno, node.module
            elif node.level:
                relative_name = "." * node.level + (node.module or "")
                yield node.lineno, importlib.util.resolve_name(
                    relative_name,
                    package,
                )


def _assert_import_boundary(
    relative_root: str,
    is_allowed: Callable[[str], bool],
) -> None:
    root = PACKAGE_ROOT / relative_root
    files = sorted(root.rglob("*.py"))
    assert files, f"Expected Python package at {root}"

    violations: list[str] = []
    for path in files:
        for line, module in _imports(path):
            if module == "cutmaster" or (
                module.startswith("cutmaster.") and not is_allowed(module)
            ):
                violations.append(
                    f"{path.relative_to(PROJECT_ROOT)}:{line} imports {module}"
                )

    assert not violations, "Layer boundary violations:\n" + "\n".join(violations)


def _is_domain_import(module: str) -> bool:
    return module == "cutmaster.domain" or module.startswith("cutmaster.domain.")


def _is_application_port_import(module: str) -> bool:
    allowed = (
        "cutmaster.domain",
        "cutmaster.application.ports",
        "cutmaster.workflow.contracts",
        "cutmaster.workflow.ports",
    )
    return any(
        module == prefix or module.startswith(prefix + ".") for prefix in allowed
    )


def _is_workflow_boundary_import(module: str) -> bool:
    allowed = (
        "cutmaster.domain",
        "cutmaster.workflow.contracts",
        "cutmaster.workflow.ports",
    )
    return any(
        module == prefix or module.startswith(prefix + ".") for prefix in allowed
    )


def test_domain_depends_only_on_domain_code() -> None:
    _assert_import_boundary("domain", _is_domain_import)


def test_application_ports_point_inward() -> None:
    _assert_import_boundary("application/ports", _is_application_port_import)


@pytest.mark.parametrize("relative_root", ["workflow/contracts", "workflow/ports"])
def test_workflow_contracts_and_ports_do_not_depend_on_outer_layers(
    relative_root: str,
) -> None:
    _assert_import_boundary(relative_root, _is_workflow_boundary_import)


def test_workflow_does_not_import_inbound_adapters() -> None:
    workflow_root = PACKAGE_ROOT / "workflow"
    violations: list[str] = []
    for path in sorted(workflow_root.rglob("*.py")):
        for line, module in _imports(path):
            if module == "cutmaster.adapters" or module.startswith(
                "cutmaster.adapters."
            ):
                violations.append(
                    f"{path.relative_to(PROJECT_ROOT)}:{line} imports {module}"
                )

    assert not violations, "Workflow imports inbound adapters:\n" + "\n".join(
        violations
    )


def test_peer_adapter_packages_do_not_import_each_other() -> None:
    adapters_root = PACKAGE_ROOT / "adapters"
    violations: list[str] = []
    for adapter_root in sorted(
        path for path in adapters_root.iterdir() if path.is_dir()
    ):
        adapter_name = adapter_root.name
        own_prefix = f"cutmaster.adapters.{adapter_name}"
        for path in sorted(adapter_root.rglob("*.py")):
            for line, module in _imports(path):
                if (
                    module.startswith("cutmaster.adapters.")
                    and not (
                        module == own_prefix
                        or module.startswith(own_prefix + ".")
                    )
                ):
                    violations.append(
                        f"{path.relative_to(PROJECT_ROOT)}:{line} imports {module}"
                    )

    assert not violations, "Peer adapters import each other:\n" + "\n".join(
        violations
    )


def test_planners_does_not_import_analyser_implementation() -> None:
    planners_root = PACKAGE_ROOT / "workflow" / "planners"
    violations: list[str] = []
    for path in sorted(planners_root.rglob("*.py")):
        for line, module in _imports(path):
            if module == "cutmaster.workflow.analyser" or module.startswith(
                "cutmaster.workflow.analyser."
            ):
                violations.append(
                    f"{path.relative_to(PROJECT_ROOT)}:{line} imports {module}"
                )

    assert not violations, (
        "Planners imports Analyser implementation:\n" + "\n".join(violations)
    )
