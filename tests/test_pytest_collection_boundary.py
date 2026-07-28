from __future__ import annotations

import ast
import configparser
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET_RECOVERY_CANARY_NODE = (
    "tests/e2e/test_propertyquarry_target_recovery_canary.py::"
    "test_property_target_recovery_canary_under_tibor"
)


def test_bare_pytest_collection_is_bounded_to_the_tracked_test_root() -> None:
    parser = configparser.ConfigParser(interpolation=None)
    config_path = REPO_ROOT / "pytest.ini"

    assert parser.read(config_path, encoding="utf-8") == [str(config_path)]
    assert parser.get("pytest", "testpaths").split() == ["tests"]
    assert "state" in parser.get("pytest", "norecursedirs").split()
    assert (REPO_ROOT / "tests").is_dir()


def test_deterministic_ci_excludes_live_target_recovery_but_keeps_explicit_canary(
) -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    deselections = makefile.split("TEST_API_PYTEST_DESELECT ?=", 1)[1].split(
        "\n\n", 1
    )[0]
    test_api_recipe = makefile.split("test-api:", 1)[1].split("\n\n", 1)[0]
    explicit_recipe = makefile.split(
        "propertyquarry-target-recovery-canary:", 1
    )[1].split("\n\n", 1)[0]

    assert deselections.count(f"--deselect={TARGET_RECOVERY_CANARY_NODE}") == 1
    assert "$(TEST_API_PYTEST_DESELECT)" in test_api_recipe
    assert TARGET_RECOVERY_CANARY_NODE in explicit_recipe
    assert "-m pytest -q" in explicit_recipe
    assert "--deselect" not in explicit_recipe
    assert "|| true" not in explicit_recipe

    module_path, function_name = TARGET_RECOVERY_CANARY_NODE.split("::", 1)
    module = ast.parse((REPO_ROOT / module_path).read_text(encoding="utf-8"))
    assert any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function_name
        for node in module.body
    )
