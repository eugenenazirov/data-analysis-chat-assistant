from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parents[2] / "retail_agent"


def _imports_under(path: Path) -> list[tuple[Path, str]]:
    imports: list[tuple[Path, str]] = []
    for source_path in path.rglob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend((source_path, alias.name) for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append((source_path, node.module))
    return imports


def _assert_no_imports(path: Path, forbidden: tuple[str, ...]) -> None:
    violations = [
        f"{source.relative_to(PACKAGE_ROOT)} -> {module}"
        for source, module in _imports_under(path)
        if module in forbidden
        or module.startswith(tuple(f"{item}." for item in forbidden))
    ]
    assert violations == []


def test_domain_has_no_outward_or_framework_dependencies():
    _assert_no_imports(
        PACKAGE_ROOT / "domain",
        (
            "os",
            "typer",
            "pydantic_ai",
            "google.cloud",
            "qdrant_client",
            "logfire",
            "retail_agent.application",
            "retail_agent.infrastructure",
            "retail_agent.presentation",
        ),
    )


def test_application_depends_only_on_domain_contracts():
    _assert_no_imports(
        PACKAGE_ROOT / "application",
        (
            "os",
            "typer",
            "pydantic_ai",
            "google.cloud",
            "qdrant_client",
            "logfire",
            "retail_agent.infrastructure",
            "retail_agent.presentation",
            "retail_agent.bigquery",
            "retail_agent.golden_store",
        ),
    )


def test_expected_architecture_packages_exist():
    expected = {
        "application",
        "domain",
        "infrastructure",
        "presentation",
    }
    assert expected <= {path.name for path in PACKAGE_ROOT.iterdir() if path.is_dir()}
