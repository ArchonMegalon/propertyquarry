from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
FLEET_ROOT = Path(str(os.environ.get("FLEET_ROOT") or "/docker/fleet"))


def _optional_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _assert_valid_dotenv_template(template_path: Path) -> None:
    for line_number, raw_line in enumerate(template_path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw_line.strip()
        assert stripped != r"\n", f"{template_path.name}:{line_number} contains a literal \\\\n placeholder line"
        if not stripped or stripped.startswith("#"):
            continue
        assert "=" in raw_line, f"{template_path.name}:{line_number} is not valid dotenv syntax"


def _dotenv_template_values(template_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in template_path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, value = raw_line.split("=", 1)
        values[key] = value
    return values


def _smoke_runtime_text() -> str:
    parts: list[str] = []
    for path in sorted((ROOT / "tests").glob("smoke_runtime_api*.py")):
        parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def test_release_smoke_keeps_propertyquarry_homepage_anonymous() -> None:
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    probe = smoke_api.split(
        'echo "== smoke: anonymous PropertyQuarry homepage =="', 1
    )[1].split('echo "== smoke: openapi =="', 1)[0]

    assert 'curl -fsS "${BASE}/"' in probe
    assert "-H 'Host: propertyquarry.com'" in probe
    assert "-H 'Accept: text/html'" in probe
    assert '"${AUTH_ARGS[@]}"' not in probe
    assert "auth_required" in probe
    assert "anonymous PropertyQuarry homepage is unavailable or auth-protected" in probe
    assert 'PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED="${PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED:-1}"' in smoke_api
    assert 'if [[ "${PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED}" == "1" ]]; then' in smoke_api


def test_postgres_smoke_scopes_propertyquarry_homepage_probe_to_branded_service() -> None:
    smoke_postgres = (ROOT / "scripts/smoke_postgres.sh").read_text(encoding="utf-8")

    assert 'if [[ -n "${PROPERTYQUARRY_API_SERVICE:-}" ]]; then' in smoke_postgres
    assert 'PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED="1"' in smoke_postgres
    assert 'PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED="0"' in smoke_postgres
    assert '-e PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED="${PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED}"' in smoke_postgres

    generic_env = dict(os.environ)
    generic_env.pop("PROPERTYQUARRY_API_SERVICE", None)
    generic_env.pop("PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED", None)
    generic = subprocess.run(
        ["bash", "scripts/smoke_postgres.sh", "--print-service-selection"],
        cwd=ROOT,
        env=generic_env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "api=ea-api" in generic.stdout
    assert "public_home_required=0" in generic.stdout

    branded_env = {**generic_env, "PROPERTYQUARRY_API_SERVICE": "propertyquarry-api"}
    branded = subprocess.run(
        ["bash", "scripts/smoke_postgres.sh", "--print-service-selection"],
        cwd=ROOT,
        env=branded_env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "api=propertyquarry-api" in branded.stdout
    assert "public_home_required=1" in branded.stdout


def test_db_size_help_explains_pgdata_volume() -> None:
    result = subprocess.run(
        ["bash", "scripts/db_size.sh", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "ea_pgdata" in result.stdout
    assert "/var/lib/postgresql/data" in result.stdout
    assert "not RAM" in result.stdout


def test_docs_explain_pgdata_volume_usage() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")

    assert "ea_pgdata" in readme
    assert "/var/lib/postgresql/data" in readme
    assert "not RAM" in readme

    assert "ea_pgdata" in runbook
    assert "/var/lib/postgresql/data" in runbook
    assert "not RAM" in runbook


def test_operator_summary_lists_ltd_release_gates() -> None:
    operator_summary = (ROOT / "scripts/operator_summary.sh").read_text(encoding="utf-8")

    assert "propertyquarry_release_make_dispatch.py ltd-release-gates" in operator_summary
    assert (
        "propertyquarry_release_make_dispatch.py "
        "verify-ltd-critical-entries-authenticated"
        in operator_summary
    )
    assert (
        "propertyquarry_release_make_dispatch.py "
        "verify-ltd-flagship-subset-authenticated"
        in operator_summary
    )


def test_local_env_rotation_slots_and_gitignore_cover_browseract_and_onemin_keys() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    env_codexea_example = (ROOT / ".env.codexea.example").read_text(encoding="utf-8")
    env_local_example = (ROOT / ".env.local.example").read_text(encoding="utf-8")

    assert ".env" in gitignore
    assert ".env.*" in gitignore
    assert "BROWSERACT_API_KEY" in env_local_example
    assert "BROWSERACT_API_KEY_FALLBACK_1" in env_local_example
    assert "BROWSERACT_API_KEY_FALLBACK_2" in env_local_example
    assert "BROWSERACT_API_KEY_FALLBACK_3" in env_local_example
    assert "BROWSERACT_CHATPLAYGROUND_URL" in env_local_example
    assert "BROWSERACT_CHATPLAYGROUND_AUDIT_WORKFLOW_ID" in env_local_example
    assert "BROWSERACT_CHATPLAYGROUND_AUDIT_WORKFLOW_QUERY" in env_local_example
    assert "BROWSERACT_CHATPLAYGROUND_AUDIT_RESULT_PATH" in env_local_example
    assert "BROWSERACT_CHATPLAYGROUND_AUDIT_TIMEOUT_SECONDS" in env_local_example
    assert "ONEMIN_AI_API_KEY" in env_local_example
    assert "ONEMIN_AI_API_KEY_FALLBACK_1" in env_local_example
    assert "ONEMIN_AI_API_KEY_FALLBACK_2" in env_local_example
    assert "ONEMIN_AI_API_KEY_FALLBACK_3" in env_local_example
    assert "ONEMIN_AI_API_KEY_FALLBACK_4" in env_local_example
    assert "ONEMIN_AI_API_KEY_FALLBACK_5" in env_local_example
    assert "ONEMIN_AI_API_KEY_FALLBACK_6" in env_local_example
    assert "ONEMIN_AI_API_KEY_FALLBACK_7" in env_local_example
    assert "ONEMIN_AI_API_KEY_FALLBACK_8" in env_local_example
    assert "ONEMIN_AI_API_KEY_FALLBACK_9" in env_local_example
    assert "ONEMIN_AI_API_KEY_FALLBACK_10" in env_local_example
    assert "EA_RESPONSES_MAGICX_HEALTH_CHECK" in env_local_example
    assert "EA_RESPONSES_MAGICX_HEALTH_INTERVAL_SECONDS" in env_local_example
    assert "EA_RESPONSES_MAGICX_HEALTH_TIMEOUT_SECONDS" in env_local_example
    assert "EA_RESPONSES_ONEMIN_INCLUDED_CREDITS_PER_KEY" in env_local_example
    assert "EA_RESPONSES_ONEMIN_BONUS_CREDITS_PER_KEY" in env_local_example
    assert "EA_RESPONSES_ONEMIN_DELETED_KEY_QUARANTINE_SECONDS" in env_local_example
    assert "EA_RESPONSES_ONEMIN_OWNER_LEDGER_PATH" in env_local_example
    assert "EA_RESPONSES_ONEMIN_PROBE_MODEL" in env_local_example
    assert "EA_RESPONSES_ONEMIN_PROBE_TIMEOUT_SECONDS" in env_local_example
    assert "CODEXEA_DEFAULT_BASE_URL" not in env_example
    assert "EA_RESPONSES_ONEMIN_CODE_MODELS" not in env_example
    assert "CODEXEA_DEFAULT_BASE_URL" in env_codexea_example
    assert "EA_RESPONSES_ONEMIN_CODE_MODELS" in env_codexea_example
    assert "EA_RESPONSES_ONEMIN_REVIEW_MODELS" in env_codexea_example
    assert (ROOT / "scripts/resolve_onemin_ai_key.sh").exists()
    assert (ROOT / "scripts/resolve_browseract_key.sh").exists()


def test_responses_provider_health_credit_debug_contract_is_documented() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    env_matrix = (ROOT / "ENVIRONMENT_MATRIX.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    tasks_work_log = _optional_text(ROOT / "TASKS_WORK_LOG.md")

    assert "estimated_remaining_credits_total" in readme
    assert "remaining_percent_of_max" in readme
    assert "estimated_burn_credits_per_hour" in readme
    assert "observed_consumed_credits" in readme
    assert "/v1/responses/_provider_health" in runbook
    assert "/v1/codex/profiles" in runbook
    assert "/v1/providers/onemin/probe-all" in runbook
    assert "estimated_remaining_credits_total" in runbook
    assert "estimated_hours_remaining_at_current_pace" in runbook
    assert "observed_consumed_credits" in runbook
    assert "EA_RESPONSES_MAGICX_HEALTH_CHECK" in runbook
    assert "EA_RESPONSES_ONEMIN_INCLUDED_CREDITS_PER_KEY" in env_matrix
    assert "EA_RESPONSES_ONEMIN_DELETED_KEY_QUARANTINE_SECONDS" in env_matrix
    assert "EA_RESPONSES_ONEMIN_OWNER_LEDGER_PATH" in env_matrix
    assert "EA_RESPONSES_ONEMIN_PROBE_MODEL" in env_matrix
    assert "EA_RESPONSES_MAGICX_HEALTH_CHECK" in env_matrix
    assert "account-attributed credit estimates" in http_examples
    assert "/v1/providers/onemin/probe-all" in http_examples
    assert "estimated_remaining_credits_total" in changelog
    assert "remaining_percent_of_max" in changelog
    assert "estimated_burn_credits_per_hour" in changelog
    assert "observed_consumed_credits" in changelog
    if tasks_work_log:
        assert "D-513" in tasks_work_log
    else:
        assert "TASKS_WORK_LOG.md is no longer tracked" in changelog


def test_makefile_selects_a_pytest_capable_python_for_local_tests() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "PYTHON_BIN ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)" in makefile
    assert "PYTEST_PYTHON_BIN ?= $(shell if" in makefile
    assert ".venv/bin/python -c 'import pytest'" in makefile
    assert "python3 -c 'import pytest'" in makefile
    assert (
        "scripts/propertyquarry_release_python.sh -c 'import pytest'"
        in makefile
    )
    assert "PYTHONPATH=ea EA_STORAGE_BACKEND=memory $(PYTEST_PYTHON_BIN) -m pytest -q tests" in makefile
    assert "override PYTHONDONTWRITEBYTECODE := 1" in makefile
    assert "export PYTHONDONTWRITEBYTECODE" in makefile
    planned = subprocess.run(
        ["make", "--no-print-directory", "-n", "test-all"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tokens = planned.split()
    pytest_module_index = tokens.index("-m")
    selected_python = tokens[pytest_module_index - 1]
    subprocess.run(
        [selected_python, "-c", "import pytest"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert (
        "$(PYTHON_BIN) scripts/propertyquarry_compileall_clean.py"
        in makefile
    )
    assert (
        "./scripts/propertyquarry_release_python.sh "
        "scripts/propertyquarry_compileall_clean.py"
        in makefile
    )
    assert " -m compileall " not in makefile


def test_core_ci_gate_is_bounded_and_reports_slow_tests_without_reducing_coverage() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/smoke-runtime.yml").read_text(encoding="utf-8")
    )

    job = workflow["jobs"]["smoke-runtime-api"]
    assert job["timeout-minutes"] == 120
    assert [
        step
        for step in job["steps"]
        if step.get("name")
        == "Reproduce canonical release receipts in disposable checkout"
    ] == [
        {
            "name": "Reproduce canonical release receipts in disposable checkout",
            "working-directory": "/docker/property",
            "run": (
                "./scripts/propertyquarry_release_python.sh "
                "scripts/propertyquarry_release_make_dispatch.py "
                "materialize-release-assets-authenticated"
            ),
        }
    ]
    assert [
        step
        for step in job["steps"]
        if step.get("name") == "Verify reproduced release receipts match HEAD"
    ] == [
        {
            "name": "Verify reproduced release receipts match HEAD",
            "working-directory": "/docker/property",
            "run": (
                "./scripts/propertyquarry_release_python.sh "
                "scripts/propertyquarry_release_make_dispatch.py "
                "verify-generated-release-artifacts-clean-authenticated"
            ),
        }
    ]
    assert [
        step
        for step in job["steps"]
        if step.get("name") == "Run read-only authenticated core CI gates"
    ] == [
        {
            "name": "Run read-only authenticated core CI gates",
            "working-directory": "/docker/property",
            "run": (
                "./scripts/propertyquarry_release_python.sh "
                "scripts/propertyquarry_release_make_dispatch.py "
                "ci-gates-authenticated"
            ),
        }
    ]
    step_names = [step.get("name") for step in job["steps"]]
    assert step_names.index(
        "Install Chromium in canonical release cache"
    ) < step_names.index(
        "Reproduce canonical release receipts in disposable checkout"
    ) < step_names.index(
        "Verify reproduced release receipts match HEAD"
    ) < step_names.index(
        "Run read-only authenticated core CI gates"
    )

    isolated_test_prefix = [
        "\t@set -eu; \\",
        (
            '\tpropertyquarry_ci_temp="$$(/usr/bin/mktemp -d '
            '/tmp/propertyquarry-ci-test.XXXXXXXX)"; \\'
        ),
        "\tcleanup_propertyquarry_ci_temp() { \\",
        '\t  case "$$propertyquarry_ci_temp" in \\',
        (
            "\t    /tmp/propertyquarry-ci-test.*) /bin/rm -rf -- "
            '"$$propertyquarry_ci_temp" ;; \\'
        ),
        (
            '\t    *) echo "refusing unsafe CI temp cleanup: '
            '$$propertyquarry_ci_temp" >&2; return 70 ;; \\'
        ),
        "\t  esac; \\",
        "\t}; \\",
        "\ttrap cleanup_propertyquarry_ci_temp EXIT; \\",
    ]
    test_api_body = makefile.split("test-api:\n", 1)[1].split("\n\n", 1)[0]
    assert test_api_body.splitlines() == [
        *isolated_test_prefix,
        (
            '\tPROPERTYQUARRY_GATE_RECEIPT_DIR="'
            '$$propertyquarry_ci_temp/exit-gate" PYTHONPATH=ea '
            "EA_STORAGE_BACKEND=memory $(PYTEST_PYTHON_BIN) -m pytest "
            "-q tests -p no:cacheprovider --durations=25 "
            "--durations-min=1.0 $(TEST_API_PYTEST_IGNORE) "
            "$(TEST_API_PYTEST_DESELECT)"
        ),
    ]
    authenticated_test_api_body = makefile.split(
        "test-api-authenticated:\n", 1
    )[1].split("\n\n", 1)[0]
    assert authenticated_test_api_body.splitlines() == [
        *isolated_test_prefix,
        (
            '\tPROPERTYQUARRY_GATE_RECEIPT_DIR="'
            '$$propertyquarry_ci_temp/exit-gate" '
            "/usr/bin/env -u PROPERTYQUARRY_RELEASE_DISPATCH "
            "EA_STORAGE_BACKEND=memory "
            "./scripts/propertyquarry_release_python.sh -m pytest -q tests "
            "-p no:cacheprovider --durations=25 --durations-min=1.0 "
            "$(TEST_API_PYTEST_IGNORE) $(TEST_API_PYTEST_DESELECT)"
        )
    ]

    ci_gates_body = makefile.split("ci-gates:\n", 1)[1].split("\n\n", 1)[0]
    assert [
        line.strip() for line in ci_gates_body.splitlines() if line.startswith("\t$(MAKE) ")
    ] == [
        "$(MAKE) smoke-help",
        "$(MAKE) ci-local",
        "$(MAKE) test-api",
        "$(MAKE) verify-release-assets",
        "$(MAKE) verify-flagship-release-readiness",
        "$(MAKE) verify-generated-release-artifacts-clean",
    ]

    authenticated_body = makefile.split(
        "ci-gates-authenticated:\n", 1
    )[1].split("\n\n", 1)[0]
    assert [
        line.strip()
        for line in authenticated_body.splitlines()
        if line.startswith("\t$(MAKE) ")
    ] == [
        "$(MAKE) smoke-help-authenticated",
        "$(MAKE) ci-local-authenticated",
        "$(MAKE) test-api-authenticated",
        "$(MAKE) verify-release-assets-authenticated",
        "$(MAKE) verify-flagship-release-readiness-authenticated",
        "$(MAKE) verify-generated-release-artifacts-clean-authenticated",
    ]


def test_release_preflight_names_local_and_full_operator_contracts_truthfully() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    local_body = makefile.split("release-preflight:\n", 1)[1].split("\n\n", 1)[0]
    full_body = makefile.split("propertyquarry-release-preflight:\n", 1)[1].split("\n\n", 1)[0]

    assert "property-release-gates" not in local_body
    assert "$(MAKE) release-preflight" in full_body
    assert "$(MAKE) property-release-gates" in full_body
    assert "source/local preflight" in readme
    assert "does not make a live-runtime or disaster-recovery claim" in readme
    assert "explicit full operator gate" in readme


def test_property_compose_review_template_uses_nonsecret_render_placeholders() -> None:
    env_values = _dotenv_template_values(ROOT / ".env.example")
    compose = yaml.safe_load((ROOT / "docker-compose.property.yml").read_text(encoding="utf-8"))
    api_environment = compose["services"]["propertyquarry-api"]["environment"]
    render = compose["services"]["propertyquarry-render-tools"]
    render_environment = render["environment"]

    assert env_values["PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN"] == (
        "REVIEW_ONLY_NOT_A_SECRET_REPLACE_BEFORE_DEPLOY"
    )
    grace_seconds = int(env_values["PROPERTYQUARRY_RENDER_STOP_GRACE_SECONDS"])
    max_generation_match = re.fullmatch(
        r"\$\{PROPERTYQUARRY_RECONSTRUCTION_RENDER_MAX_GENERATION_SECONDS:-(\d+)\}",
        render_environment["PROPERTYQUARRY_RECONSTRUCTION_RENDER_MAX_GENERATION_SECONDS"],
    )
    assert max_generation_match is not None
    assert grace_seconds > int(max_generation_match.group(1))
    assert api_environment["PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN"].startswith(
        "${PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN:?"
    )
    assert render_environment["PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN"].startswith(
        "${PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN:?"
    )
    assert render_environment["PROPERTYQUARRY_RENDER_STOP_GRACE_SECONDS"] == (
        "${PROPERTYQUARRY_RENDER_STOP_GRACE_SECONDS:-1860}"
    )
    assert render["stop_grace_period"] == "${PROPERTYQUARRY_RENDER_STOP_GRACE_SECONDS:-1860}s"


def test_env_templates_use_only_valid_dotenv_lines() -> None:
    _assert_valid_dotenv_template(ROOT / ".env.example")
    _assert_valid_dotenv_template(ROOT / ".env.codexea.example")
    _assert_valid_dotenv_template(ROOT / ".env.local.example")


def _route_prefixes_from_router_modules() -> set[str]:
    prefixes: set[str] = set()
    for route_path in sorted((ROOT / "ea" / "app" / "api" / "routes").glob("*.py")):
        content = route_path.read_text(encoding="utf-8")
        match = re.search(r'APIRouter\(prefix="([^"]+)"', content)
        if match is not None:
            prefixes.add(match.group(1))
    return prefixes


def _documented_api_prefixes_from_architecture_map() -> list[str]:
    architecture_map = (ROOT / "ARCHITECTURE_MAP.md").read_text(encoding="utf-8")
    return [
        match.group(1).removesuffix("/*")
        for match in re.finditer(r"^- [^:]+: `([^`]+)`", architecture_map, flags=re.MULTILINE)
        if match.group(1).startswith(("/v1/", "/app/", "/internal/"))
    ]


@pytest.mark.parametrize("documented_prefix", _documented_api_prefixes_from_architecture_map())
def test_architecture_map_api_surface_route_prefix_matches_router(documented_prefix: str) -> None:
    documented_prefixes = _documented_api_prefixes_from_architecture_map()
    actual_prefixes = _route_prefixes_from_router_modules()
    assert documented_prefixes
    assert actual_prefixes

    assert any(
        actual == documented_prefix or actual.startswith(f"{documented_prefix}/") for actual in actual_prefixes
    ), f"ARCHITECTURE_MAP.md documents {documented_prefix} but no APIRouter prefix matches it"


def test_architecture_map_documents_every_mounted_api_router_prefix() -> None:
    documented_prefixes = _documented_api_prefixes_from_architecture_map()
    actual_prefixes = _route_prefixes_from_router_modules()
    assert documented_prefixes
    assert actual_prefixes

    undocumented_prefixes = sorted(
        actual_prefix
        for actual_prefix in actual_prefixes
        if not any(
            actual_prefix == documented_prefix or actual_prefix.startswith(f"{documented_prefix}/")
            for documented_prefix in documented_prefixes
        )
    )

    assert not undocumented_prefixes, (
        "ARCHITECTURE_MAP.md is missing mounted API router prefixes: " + ", ".join(undocumented_prefixes)
    )


def test_runtime_capabilities_reference_materialized_backlog_ids() -> None:
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))
    tasks_work_log = _optional_text(ROOT / "TASKS_WORK_LOG.md")
    queued_ids = {
        match.group(1)
        for match in re.finditer(r"^\|\s*(Q-\d+)\s*\|", tasks_work_log, flags=re.MULTILINE)
    }
    done_ids = {
        match.group(1)
        for match in re.finditer(r"^\|\s*(D-\d+)\s*\|", tasks_work_log, flags=re.MULTILINE)
    }
    expected_refs = {
        "provider_registry_capability_routing": {"D-451"},
        "runtime_surface_docs_env_deploy_parity": {"D-452"},
        "startup_authoritative_runtime_profile": {"D-446"},
    }

    for capability_name, expected_queue_ids in expected_refs.items():
        capability = next(entry for entry in milestone["capabilities"] if entry["name"] == capability_name)
        task_refs = set(capability.get("task_refs") or [])

        assert task_refs == expected_queue_ids
        if tasks_work_log:
            assert task_refs.issubset(queued_ids | done_ids)


def test_published_queue_overlay_stays_empty_for_materialized_uncovered_scope() -> None:
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))
    overlay = yaml.safe_load((ROOT / ".codex-studio" / "published" / "QUEUE.generated.yaml").read_text(encoding="utf-8"))
    required_released = {
        "runtime_surface_docs_env_deploy_parity",
        "provider_registry_capability_routing",
        "typed_task_and_skill_policy_models",
        "startup_authoritative_runtime_profile",
    }
    released_caps = {
        entry["name"]
        for entry in milestone["capabilities"]
        if entry["name"] in required_released and entry.get("status") == "released"
    }

    assert released_caps == required_released
    assert overlay.get("mode") == "append"
    items = overlay.get("items") or []
    assert isinstance(items, list)
    lowered_items = [str(item).lower() for item in items]
    forbidden_fragments = (
        "docs, env scaffolding, and deployment configuration still lag the actual router and provider surface",
        "startup can still resolve into a mixed durability/auth profile instead of one authoritative runtime mode",
        "provider capability-routing",
        "typed task-contract and skill metadata",
    )
    for fragment in forbidden_fragments:
        assert not any(fragment in item for item in lowered_items), (
            "Published queue overlay should not re-queue already materialized uncovered scope: " + fragment
        )

    mirror_items = [
        item
        for item in items
        if isinstance(item, dict) and item.get("audit_finding_key") == "project.design_mirror_missing_or_stale"
    ]
    assert len(mirror_items) == 1, "Published queue overlay should keep exactly one bounded mirror-drift queue slice."

    mirror_item = mirror_items[0]
    assert mirror_item["package_id"] == "audit-task-4257456"
    assert mirror_item["audit_scope_id"] == "ea"
    assert mirror_item["source_ref"] == "audit_task_candidates[4257456]"
    assert mirror_item["owned_surfaces"] == ["design_mirror:ea"]
    assert mirror_item["allowed_paths"] == [".codex-design"]
    assert mirror_item["source_items"] == [
        "/docker/EA/.codex-design/product/NEXT_90_DAY_QUEUE_STAGING.generated.yaml",
    ]
    assert (
        "keep one bounded queue slice" in str(mirror_item["task"]).lower()
    ), "Mirror-drift queue slice should encode the non-reopen intent."


def test_role_aware_healthcheck_contract_covers_api_and_worker_roles() -> None:
    dockerfile = (ROOT / "ea" / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "EA_ROLE" in dockerfile
    assert "COPY scripts/willhaben_property_packet.py /app/scripts/willhaben_property_packet.py" in dockerfile
    assert 'role=${EA_ROLE:-api}; case \\"$role\\" in' in dockerfile
    assert "worker|scheduler)" in dockerfile
    assert "http://127.0.0.1:8090/health/live" in dockerfile
    assert "EA_ROLE=api" in compose
    assert "EA_ROLE=worker" in compose
    assert "EA_ROLE=scheduler" in compose
    assert "ea-responses-proxy" in compose
    assert "EA_RESPONSES_PROXY_HOST=0.0.0.0" in compose
    assert "EA_RESPONSES_PROXY_AUTH_TOKEN=${EA_API_TOKEN:?" in compose
    assert '"${EA_RESPONSES_PROXY_PUBLISH_HOST:-127.0.0.1}:${EA_RESPONSES_PROXY_HOST_PORT:-8092}:8091"' in compose
    assert "http://127.0.0.1:8091/health/ready" in compose


def test_cloudflared_tunnel_is_only_available_via_override() -> None:
    base_compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    tunnel_override = (ROOT / "docker-compose.cloudflared.yml").read_text(encoding="utf-8")
    legacy_edge_override = (ROOT / "docker-compose.property-legacy-edge.yml").read_text(encoding="utf-8")
    property_compose = (ROOT / "docker-compose.property.yml").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "ea-cloudflared" not in base_compose
    assert "propertyquarry-cloudflared" in tunnel_override
    assert "PROPERTYQUARRY_CF_TUNNEL_TOKEN" in tunnel_override
    assert "propertyquarry-api" in tunnel_override
    assert "ea-api" not in property_compose
    assert "ea-api" in legacy_edge_override
    assert "docker-compose.cloudflared.yml" in readme


def test_deploy_propertyquarry_delegates_immutable_cloudflared_plan() -> None:
    deploy = (ROOT / "scripts" / "deploy_propertyquarry.sh").read_text(encoding="utf-8")
    overlay = (ROOT / "docker-compose.cloudflared.yml").read_text(encoding="utf-8")

    assert "--canonical-compose-plan" in deploy
    assert "--require-cloudflared-immutable-digest-and-config-binding" in deploy
    assert "--forbid-caller-compose" in deploy
    assert "PROPERTYQUARRY_ENABLE_CLOUDFLARED" not in deploy
    assert "docker-compose.cloudflared.yml" not in deploy
    assert "cloudflare/cloudflared@sha256:" in overlay
    assert "cloudflare/cloudflared:latest" not in overlay


def test_propertyquarry_cloudflared_bootstrap_script_help_and_contract() -> None:
    result = subprocess.run(
        ["python3", "scripts/bootstrap_propertyquarry_cloudflared_tunnel.py", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    script = (ROOT / "scripts" / "bootstrap_propertyquarry_cloudflared_tunnel.py").read_text(encoding="utf-8")

    assert "dedicated PropertyQuarry Cloudflare tunnel" in result.stdout
    assert "--tunnel-name" in result.stdout
    assert "--property-env" in result.stdout
    assert "--enable-cloudflared" in result.stdout
    assert "--dry-run" in result.stdout
    assert "CLOUDFLARE_GLOBAL_API_KEY" in script
    assert "PROPERTYQUARRY_CF_TUNNEL_TOKEN" in script
    assert '"/accounts/{account_id}/cfd_tunnel"' in script
    assert '"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/token"' in script
    assert "isinstance(result, dict)" in script


def test_host_recovery_scripts_are_guarded_and_classified() -> None:
    isolation_check = (ROOT / "scripts" / "check_property_repo_isolation.py").read_text(encoding="utf-8")
    harden_script = (ROOT / "scripts" / "harden_propertyquarry_docker.sh").read_text(encoding="utf-8")
    recover_script = (ROOT / "scripts" / "recover_host_after_reboot.sh").read_text(encoding="utf-8")

    assert "QUARANTINED_HOST_OPS_SCRIPTS" in isolation_check
    assert "PROPERTYQUARRY_HOST_RECOVERY_ALLOW" in isolation_check
    assert "PROPERTYQUARRY_HOST_RECOVERY_DRY_RUN" in isolation_check
    assert "PROPERTYQUARRY_HOST_RECOVERY_ALLOW" in harden_script
    assert "PROPERTYQUARRY_HOST_RECOVERY_DRY_RUN" in harden_script
    assert 'ROOT="${PROPERTYQUARRY_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"' in harden_script
    assert "PROPERTYQUARRY_HOST_RECOVERY_ALLOW" in recover_script
    assert "PROPERTYQUARRY_HOST_RECOVERY_DRY_RUN" in recover_script


def test_deploy_script_waits_for_worker_topology_and_dumps_role_logs() -> None:
    deploy = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    assert 'TOPOLOGY_SERVICES=(ea-api)' in deploy
    assert 'TOPOLOGY_SERVICES=(ea-teable-relay ea-api ea-responses-proxy ea-worker ea-scheduler ea-db)' in deploy
    assert 'for service in "${build_services[@]}"; do' in deploy
    assert 'compose up -d --no-build --no-deps --force-recreate "${service}"' in deploy
    assert 'echo "Service failed to become ready during deploy: ${service}" >&2' in deploy
    assert 'service_container_ready "${service}"' in deploy
    assert "docker inspect -f '{{.State.Running}}'" in deploy
    assert "docker inspect -f '{{.State.Restarting}}'" in deploy
    assert 'curl -fsS "http://localhost:${HOST_PORT}/health"' in deploy
    assert 'FAILURE_LOG_SERVICES=(ea-teable-relay ea-api ea-responses-proxy ea-worker ea-scheduler ea-db ea-openvoice)' in deploy
    assert 'compose logs --tail 200 "${FAILURE_LOG_SERVICES[@]}"' in deploy


def test_smoke_api_curl_wrapper_retries_transient_runtime_bounces() -> None:
    smoke_api = (ROOT / "scripts" / "smoke_api.sh").read_text(encoding="utf-8")

    assert "--retry 20" in smoke_api
    assert "--retry-delay 1" in smoke_api
    assert "--retry-max-time 120" in smoke_api
    assert "--retry-all-errors" in smoke_api
    assert "--retry-connrefused" in smoke_api
    assert "--connect-timeout 5" in smoke_api
    assert "--max-time 600" in smoke_api


def test_deploy_script_keeps_fastestvpn_overlay_explicit() -> None:
    deploy = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")

    assert 'EA_ENABLE_FASTESTVPN=1' in deploy
    assert 'if [[ "${EA_ENABLE_FASTESTVPN:-0}" == "1" ]]; then' in deploy
    assert "EA_ENABLE_FASTESTVPN=1 but no FastestVPN *.ovpn profiles were found" in deploy
    assert "If you deploy through `scripts/deploy.sh`, keep the overlay explicit with `EA_ENABLE_FASTESTVPN=1`." in readme
    assert "If you use `scripts/deploy.sh`, keep that overlay explicit with `EA_ENABLE_FASTESTVPN=1`." in runbook


def test_milestone_uses_status_model_and_release_tags() -> None:
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert set(milestone["status_model"]) == {"planned", "coded", "wired", "tested", "released"}
    assert "ci_gate_bundle" in milestone["release_tags"]
    assert "release_preflight_bundle" in milestone["release_tags"]
    assert "docs_verify_alias" in milestone["release_tags"]
    smoke_bundle = next(entry for entry in milestone["capabilities"] if entry["name"] == "smoke_and_release_gate_bundle")
    assert smoke_bundle["status"] == "released"
    rewrite_denial = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "rewrite_policy_disallowed_tool_api_coverage"
    )
    assert rewrite_denial["status"] == "released"


def test_support_bundle_help_mentions_db_volume_attribution() -> None:
    result = subprocess.run(
        ["bash", "scripts/support_bundle.sh", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "SUPPORT_INCLUDE_DB_VOLUME=0|1" in result.stdout


def test_operator_summary_prints_grounded_packet_guidance() -> None:
    result = subprocess.run(
        ["bash", "scripts/operator_summary.sh"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "-- grounded packets --" in result.stdout
    assert "public help:" in result.stdout
    assert "support question:" in result.stdout
    assert "operator cadence:" in result.stdout
    assert "-- codex governance --" in result.stdout
    assert "hard coder:" in result.stdout
    assert "support/help:" in result.stdout
    assert "support fallout:" in result.stdout
    assert "guide freshness:" in result.stdout


def test_support_bundle_writes_grounding_summary() -> None:
    env = os.environ.copy()
    env.update(
        {
            "SUPPORT_BUNDLE_PREFIX": "grounding_contract",
            "SUPPORT_INCLUDE_API": "0",
            "SUPPORT_INCLUDE_DB": "0",
            "SUPPORT_INCLUDE_DB_VOLUME": "0",
            "SUPPORT_INCLUDE_DB_SIZE": "0",
            "SUPPORT_INCLUDE_QUEUE": "0",
            "SUPPORT_LOG_TAIL_LINES": "1",
        }
    )
    result = subprocess.run(
        ["bash", "scripts/support_bundle.sh"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    match = re.search(r"support bundle written: (.+)", result.stdout)
    assert match is not None
    bundle_path = Path(match.group(1).strip())
    try:
        text = bundle_path.read_text(encoding="utf-8")
    finally:
        bundle_path.unlink(missing_ok=True)
    assert "-- grounding --" in text
    assert "public_help_heading=" in text
    assert "support_scorecard_question=" in text
    assert "operator_review_cadence=" in text
    assert "-- codex governance --" in text
    assert "codex_review_cadence=" in text
    assert "codex_core_expectation=" in text
    assert "codex_support_help_boundary=" in text
    assert "support_fallout_state=" in text
    assert "support_closures_waiting=" in text
    assert "public_guide_freshness=" in text


def test_support_bundle_pgdata_attribution_release_baseline_is_pinned() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    support_bundle = (ROOT / "scripts/support_bundle.sh").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))
    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "support_bundle_pgdata_attribution")

    assert "SUPPORT_INCLUDE_DB_VOLUME=0" in readme
    assert "ea-db mount/volume attribution" in readme
    assert "ea_pgdata" in readme
    assert "/var/lib/postgresql/data" in readme

    assert "SUPPORT_INCLUDE_DB_VOLUME=0 bash scripts/support_bundle.sh" in runbook
    assert "ea_pgdata" in runbook
    assert "/var/lib/postgresql/data" in runbook

    assert "support_bundle_pgdata_attribution" in changelog
    assert "SUPPORT_INCLUDE_DB_VOLUME" in changelog
    assert "ea-db volume/mount attribution" in changelog

    assert 'echo "expected_runtime_volume=ea_pgdata"' in support_bundle
    assert 'echo "expected_container_mount=/var/lib/postgresql/data"' in support_bundle
    assert 'docker inspect "${DB_CONTAINER}" --format' in support_bundle
    assert capability["status"] == "released"


def test_db_visibility_and_retention_help_contracts_cover_release_baseline_flags() -> None:
    db_size_help = subprocess.run(
        ["bash", "scripts/db_size.sh", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    db_retention_help = subprocess.run(
        ["bash", "scripts/db_retention.sh", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    support_bundle_help = subprocess.run(
        ["bash", "scripts/support_bundle.sh", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert "EA_DB_SIZE_SCHEMA" in db_size_help
    assert "EA_DB_SIZE_SORT_KEY" in db_size_help
    assert "EA_DB_SIZE_TABLE_PREFIX" in db_size_help
    assert "EA_DB_SIZE_MIN_MB" in db_size_help

    assert "EA_RETENTION_PROFILE" in db_retention_help
    assert "EA_RETENTION_TABLES" in db_retention_help
    assert "EA_RETENTION_SKIP_TABLES" in db_retention_help

    assert "SUPPORT_INCLUDE_DB_SIZE=0|1" in support_bundle_help
    assert "SUPPORT_DB_SIZE_LIMIT=<n>" in support_bundle_help


def test_db_operator_scripts_support_propertyquarry_service_aliases() -> None:
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    db_bootstrap = (ROOT / "scripts/db_bootstrap.sh").read_text(encoding="utf-8")
    db_status = (ROOT / "scripts/db_status.sh").read_text(encoding="utf-8")
    db_retention = (ROOT / "scripts/db_retention.sh").read_text(encoding="utf-8")
    db_size = (ROOT / "scripts/db_size.sh").read_text(encoding="utf-8")

    assert "PROPERTYQUARRY_API_SERVICE=propertyquarry-api" in env_example
    assert (
        "PROPERTYQUARRY_WORKER_SERVICE=propertyquarry-worker"
        in env_example
    )
    assert "PROPERTYQUARRY_WORKER_PROFILE=property_only" in env_example
    assert "PROPERTYQUARRY_SCHEDULER_SERVICE=propertyquarry-scheduler" in env_example
    assert "PROPERTYQUARRY_DB_SERVICE=propertyquarry-db" in env_example

    assert "PROPERTYQUARRY_DB_SERVICE" in readme
    assert "PROPERTYQUARRY_DB_SERVICE" in runbook
    assert 'DB_SERVICE="${PROPERTYQUARRY_DB_SERVICE:-${EA_DB_SERVICE:-ea-db}}"' in db_bootstrap
    assert '"${DC[@]}" up -d "${DB_SERVICE}"' in db_bootstrap
    assert 'DB_SERVICE="${PROPERTYQUARRY_DB_SERVICE:-${EA_DB_SERVICE:-ea-db}}"' in db_status
    assert '"${DC[@]}" up -d "${DB_SERVICE}"' in db_status
    assert 'DB_SERVICE="${PROPERTYQUARRY_DB_SERVICE:-${EA_DB_SERVICE:-ea-db}}"' in db_retention
    assert '"${DC[@]}" up -d "${DB_SERVICE}"' in db_retention
    assert 'DB_SERVICE="${PROPERTYQUARRY_DB_SERVICE:-${EA_DB_SERVICE:-ea-db}}"' in db_size
    assert '"${DC[@]}" up -d "${DB_SERVICE}"' in db_size


def test_support_bundle_supports_propertyquarry_service_aliases() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    support_bundle = (ROOT / "scripts/support_bundle.sh").read_text(encoding="utf-8")

    assert "scripts/support_bundle.sh" in readme
    assert "scripts/support_bundle.sh" in runbook
    assert "PROPERTYQUARRY_API_SERVICE" in readme
    assert "PROPERTYQUARRY_DB_SERVICE" in readme
    assert "PROPERTYQUARRY_API_SERVICE" in runbook
    assert "PROPERTYQUARRY_DB_SERVICE" in runbook
    assert 'API_SERVICE="${PROPERTYQUARRY_API_SERVICE:-${EA_API_SERVICE:-ea-api}}"' in support_bundle
    assert 'DB_SERVICE="${PROPERTYQUARRY_DB_SERVICE:-${EA_DB_SERVICE:-ea-db}}"' in support_bundle
    assert '"${DC[@]}" logs --tail "${TAIL_LINES}" "${API_SERVICE}"' in support_bundle
    assert '"${DC[@]}" logs --tail "${TAIL_LINES}" "${DB_SERVICE}"' in support_bundle
    assert 'DB_CONTAINER="${EA_DB_CONTAINER:-${DB_SERVICE}}"' in support_bundle


def test_db_visibility_and_retention_docs_and_scripts_are_pinned() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    support_bundle = (ROOT / "scripts/support_bundle.sh").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))
    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "operator_db_visibility_and_retention")

    assert "EA_RETENTION_PROFILE=aggressive|standard|conservative" in readme
    assert "EA_RETENTION_TABLES" in readme
    assert "EA_RETENTION_SKIP_TABLES" in readme
    assert "EA_DB_SIZE_SCHEMA=<schema>" in readme
    assert "EA_DB_SIZE_SORT_KEY=total|table|index" in readme
    assert "EA_DB_SIZE_TABLE_PREFIX=<prefix>" in readme
    assert "EA_DB_SIZE_MIN_MB=<n>" in readme
    assert "SUPPORT_INCLUDE_DB_SIZE=0" in readme
    assert "SUPPORT_DB_SIZE_LIMIT=<n>" in readme

    assert "EA_RETENTION_PROFILE=aggressive bash scripts/db_retention.sh" in runbook
    assert "EA_RETENTION_TABLES=execution_events,delivery_outbox bash scripts/db_retention.sh" in runbook
    assert "EA_RETENTION_SKIP_TABLES=observation_events,policy_decisions bash scripts/db_retention.sh" in runbook
    assert "EA_DB_SIZE_SCHEMA=public bash scripts/db_size.sh" in runbook
    assert "EA_DB_SIZE_SORT_KEY=index bash scripts/db_size.sh" in runbook
    assert "EA_DB_SIZE_TABLE_PREFIX=execution_ bash scripts/db_size.sh" in runbook
    assert "EA_DB_SIZE_MIN_MB=25 bash scripts/db_size.sh" in runbook
    assert "SUPPORT_INCLUDE_DB_SIZE=0 bash scripts/support_bundle.sh" in runbook
    assert "SUPPORT_DB_SIZE_LIMIT=15 bash scripts/support_bundle.sh" in runbook

    assert "Support bundle export now optionally includes DB size snapshots" in changelog
    assert "Retention operator flow now supports profile presets" in changelog
    assert "Retention operator flow now supports table allowlist/skip filters" in changelog
    assert "DB size operator flow now supports schema scoping" in changelog
    assert "DB size operator flow now supports sort-key selection" in changelog
    assert "DB size operator flow now supports table-prefix scoping" in changelog
    assert "DB size operator flow now supports minimum-size filtering" in changelog
    assert 'echo "-- db size snapshot --"' in support_bundle
    assert 'EA_DB_SIZE_LIMIT="${DB_SIZE_LIMIT}" bash scripts/db_size.sh' in support_bundle
    assert capability["status"] == "released"


def test_version_info_reports_milestone_status_counts() -> None:
    result = subprocess.run(
        ["bash", "scripts/version_info.sh"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "milestone_status_counts=planned:" in result.stdout
    assert "milestone_release_tags=ci_gate_bundle" in result.stdout


def test_postgres_contract_script_help_and_wiring() -> None:
    result = subprocess.run(
        ["bash", "scripts/test_postgres_contracts.sh", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    workflow = (ROOT / ".github/workflows/smoke-runtime.yml").read_text(encoding="utf-8")
    smoke_help = (ROOT / "scripts/smoke_help.sh").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    script = (ROOT / "scripts/test_postgres_contracts.sh").read_text(encoding="utf-8")

    assert "EA_TEST_POSTGRES_DB" in result.stdout
    assert "scripts/test_postgres_contracts.sh" in smoke_help
    assert "test-postgres-contracts:" in makefile
    assert "bash scripts/test_postgres_contracts.sh" in workflow
    assert "tests/test_postgres_contract_matrix_integration.py" in script
    assert "tests/test_generic_async_dependency_projection_contracts.py" in script
    assert "tests/test_memory_router_contracts.py" in script
    assert "tests/test_openapi_async_acceptance_examples_contracts.py" in script
    assert "tests/test_openapi_dependency_examples_contracts.py" in script
    assert "tests/test_plan_scope_contracts.py" in script
    assert "tests/test_planner.py" in script
    assert "tests/test_policy.py" in script
    assert "tests/test_principal_fallback_contracts.py" in script
    assert "tests/test_queue_retry_contracts.py" in script
    assert "tests/test_rewrite_scope_contracts.py" in script
    assert "tests/test_rewrite_api_scope_contracts.py" in script
    assert "tests/test_rewrite_dependency_projection_contracts.py" in script
    assert "tests/test_skills.py" in script
    assert "tests/test_step_parent_projection_contracts.py" in script
    assert "tests/test_tool_execution.py" in script


def test_payfunnels_bootstrap_script_help_and_wiring() -> None:
    result = subprocess.run(
        ["python3", "scripts/bootstrap_payfunnels_propertyquarry.py", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    smoke_help = (ROOT / "scripts/smoke_help.sh").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "Prepare PropertyQuarry PayFunnels runtime configuration." in result.stdout
    assert "scripts/bootstrap_payfunnels_propertyquarry.py" in smoke_help
    assert "scripts/bootstrap_payfunnels_propertyquarry.py" in makefile
    assert "scripts/bootstrap_payfunnels_propertyquarry.py" in runbook
    assert "bootstrap_payfunnels_propertyquarry.py" in readme


def test_property_repair_fleet_canary_script_emits_receipt() -> None:
    env = os.environ.copy()
    env["EA_RUNTIME_MODE"] = "prod"
    env["EA_API_TOKEN"] = "inherited-release-gate-token"
    env.pop("DATABASE_URL", None)
    result = subprocess.run(
        [sys.executable, "scripts/propertyquarry_repair_fleet_canary.py"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)

    assert payload["status"] == "pass"
    assert str(payload.get("generated_at") or "").strip()
    assert payload["run_status"] == "completed_partial"
    assert payload["receipt_resolution"] == "provider_quarantined_retry_budget_exhausted"
    assert payload["source_repair_status"] == "returned"
    assert payload["repair_summary"]["resolved_total"] == 1
    assert payload["repair_summary"]["deferred_total"] == 0


def test_emailit_bootstrap_script_help_and_wiring() -> None:
    result = subprocess.run(
        ["python3", "scripts/bootstrap_emailit_propertyquarry.py", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    smoke_help = (ROOT / "scripts/smoke_help.sh").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "Prepare and inspect the PropertyQuarry Emailit sending domain." in result.stdout
    assert "--send-test-to" in result.stdout
    assert "scripts/bootstrap_emailit_propertyquarry.py" in smoke_help
    assert "scripts/bootstrap_emailit_propertyquarry.py" in makefile
    assert "scripts/bootstrap_emailit_propertyquarry.py" in runbook
    assert "bootstrap_emailit_propertyquarry.py" in readme


def test_postgres_contract_and_fastestvpn_helpers_support_standalone_paths() -> None:
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    postgres_script = (ROOT / "scripts/test_postgres_contracts.sh").read_text(encoding="utf-8")
    fastestvpn_script = (ROOT / "scripts/ensure_fastestvpn_proxy_pool.sh").read_text(encoding="utf-8")
    release_script = (ROOT / "scripts/release_v115_rag.sh").read_text(encoding="utf-8")

    assert 'DB_SERVICE="${PROPERTYQUARRY_DB_SERVICE:-${EA_DB_SERVICE:-ea-db}}"' in postgres_script
    assert 'DB_CONTAINER="${EA_DB_CONTAINER:-${DB_SERVICE}}"' in postgres_script
    assert '"${DC[@]}" up -d "${DB_SERVICE}"' in postgres_script
    assert "compose DB service container" in postgres_script

    assert "PROPERTYQUARRY_PROXY_POOL_NETWORK" in fastestvpn_script
    assert "PROPERTYQUARRY_FASTESTVPN_PROXY_IMAGE" in fastestvpn_script
    assert 'root = Path("/docker/property/vpn/fastestvpn")' in fastestvpn_script
    assert "/docker/EA/vpn/fastestvpn" not in fastestvpn_script

    assert "/docker/property/scripts/release_v115_rag.sh prune_meta" in release_script
    assert "/docker/property/scripts/release_v115_rag.sh prune_pycache" in release_script
    assert "/docker/property/scripts/release_v115_rag.sh clean_rewrite_baseline" in release_script

    assert "/docker/property/docker-compose.fastestvpn.yml" in runbook
    assert "/docker/property/vpn/fastestvpn/README.md" in runbook
    assert "/docker/property/scripts/bootstrap_fastestvpn_configs.sh" in runbook
    assert "/docker/property/scripts/rotate_fastestvpn_proxy.sh" in runbook
    assert "/docker/property/LTDs.md" in runbook
    assert "/docker/property/SKILLS.md" in runbook


def test_postgres_smoke_exports_openapi_dependency_examples() -> None:
    smoke = (ROOT / "scripts/smoke_postgres.sh").read_text(encoding="utf-8")

    assert "exports OpenAPI and verifies paused session-step dependency examples" in smoke
    assert 'API_SERVICE="${PROPERTYQUARRY_API_SERVICE:-${EA_API_SERVICE:-ea-api}}"' in smoke
    assert '"${DC[@]}" up -d --no-deps --build --force-recreate "${API_SERVICE}"' in smoke
    assert "bash scripts/export_openapi.sh" in smoke
    assert "step-artifact-save-waiting-approval" in smoke
    assert "step-artifact-save-blocked-human" in smoke
    assert "openapi export ok" in smoke


def test_session_step_dependency_projection_is_covered_by_contract_tests() -> None:
    rewrite_route = (ROOT / "ea/app/api/routes/rewrite.py").read_text(encoding="utf-8")
    contract_test = (ROOT / "tests/test_rewrite_dependency_projection_contracts.py").read_text(encoding="utf-8")

    assert "dependency_keys: list[str]" in rewrite_route
    assert "dependency_states: dict[str, str]" in rewrite_route
    assert "dependency_step_ids: dict[str, str]" in rewrite_route
    assert "blocked_dependency_keys: list[str]" in rewrite_route
    assert "dependencies_satisfied: bool" in rewrite_route
    assert "Current state for each declared dependency key. Paused approval-backed sessions keep completed " in rewrite_route
    assert "This can still be true for a `waiting_approval` step, " in rewrite_route
    assert '"step_id": "step-artifact-save-waiting-approval"' in rewrite_route
    assert '"step_id": "step-artifact-save-blocked-human"' in rewrite_route
    assert "_step_dependency_projection(" in rewrite_route
    assert "step_policy_evaluate" in contract_test
    assert '["step_input_prepare"]' in contract_test
    assert '["step_policy_evaluate"]' in contract_test
    assert '"dependency_states"] == {"step_policy_evaluate": "completed"}' in contract_test
    assert 'steps["step_artifact_save"]["state"] == "waiting_approval"' in contract_test
    assert 'steps["step_artifact_save"]["blocked_dependency_keys"] == ["step_human_review"]' in contract_test


def test_session_step_dependency_projection_is_covered_by_smoke_runtime() -> None:
    smoke_test = _smoke_runtime_text()
    smoke_script = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")

    assert 'steps_by_key["step_policy_evaluate"]["dependency_states"] == {"step_input_prepare": "completed"}' in smoke_test
    assert 'steps_by_key["step_artifact_save"]["dependency_states"] == {"step_policy_evaluate": "completed"}' in smoke_test
    assert 'approval_steps["step_artifact_save"]["state"] == "waiting_approval"' in smoke_test
    assert 'review_steps["step_artifact_save"]["blocked_dependency_keys"] == ["step_human_review"]' in smoke_test
    assert 'generic_approval_steps["step_artifact_save"]["state"] == "waiting_approval"' in smoke_test
    assert 'generic_review_steps["step_artifact_save"]["blocked_dependency_keys"] == ["step_human_review"]' in smoke_test
    assert "projection_ok=(" in smoke_script
    assert "dependency_states') == {'step_policy_evaluate': 'completed'}" in smoke_script
    assert "dependency_states') == {'step_input_prepare': 'completed'}" in smoke_script
    assert "save_step.get('state',''), policy_step.get('dependency_states') == {'step_input_prepare': 'completed'}" in smoke_script
    assert "save_step.get('blocked_dependency_keys') == ['step_human_review']" in smoke_script
    assert 'GENERIC_APPROVAL_TASK_KEY="decision_brief_approval_${SMOKE_RUN_TOKEN}"' in smoke_script
    assert '${GENERIC_APPROVAL_TASK_KEY}|awaiting_approval|waiting_approval|True|True|True|True|True' in smoke_script
    assert "stakeholder_briefing_review|awaiting_human|waiting_human|True|True|True|True|queued|True|True|True" in smoke_script


def test_openapi_dependency_examples_are_guarded() -> None:
    openapi_test = (ROOT / "tests/test_openapi_dependency_examples_contracts.py").read_text(encoding="utf-8")
    smoke_script = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")

    assert 'step-artifact-save-waiting-approval' in openapi_test
    assert 'step-artifact-save-blocked-human' in openapi_test
    assert 'waiting_approval["dependency_states"] == {"step_policy_evaluate": "completed"}' in openapi_test
    assert 'blocked_human["blocked_dependency_keys"] == ["step_human_review"]' in openapi_test
    assert 'curl -fsS "${BASE}/openapi.json"' in smoke_script
    assert "waiting.get('state','')" in smoke_script
    assert "blocked.get('blocked_dependency_keys') == ['step_human_review']" in smoke_script


def test_openapi_async_acceptance_examples_are_guarded() -> None:
    openapi_test = (ROOT / "tests/test_openapi_async_acceptance_examples_contracts.py").read_text(encoding="utf-8")
    smoke_script = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")

    assert 'schemas["RewriteAcceptedOut"]["examples"]' in openapi_test
    assert 'schemas["PlanExecuteAcceptedOut"]["examples"]' in openapi_test
    assert 'rewrite_approval["approval_id"] == "approval-123"' in openapi_test
    assert 'plan_human["task_key"] == "stakeholder_briefing_review"' in openapi_test
    assert "rewrite_examples=(schemas.get('RewriteAcceptedOut') or {}).get('examples') or []" in smoke_script
    assert "plan_examples=(schemas.get('PlanExecuteAcceptedOut') or {}).get('examples') or []" in smoke_script
    assert "approval-123|human-task-123|poll_or_subscribe|poll_or_subscribe|poll_or_subscribe|decision_brief_approval|stakeholder_briefing_review|rewrite_retry_delayed" in smoke_script


def test_plan_scope_contracts_are_wired_into_focused_contract_bundle() -> None:
    script = (ROOT / "scripts/test_postgres_contracts.sh").read_text(encoding="utf-8")
    plan_scope_test = (ROOT / "tests/test_plan_scope_contracts.py").read_text(encoding="utf-8")

    assert "tests/test_plan_scope_contracts.py" in script
    assert "/v1/plans/compile" in plan_scope_test
    assert "/v1/plans/execute" in plan_scope_test
    assert "/v1/rewrite/sessions/" in plan_scope_test
    assert "/v1/rewrite/artifacts/" in plan_scope_test
    assert "/v1/rewrite/receipts/" in plan_scope_test
    assert "/v1/rewrite/run-costs/" in plan_scope_test
    assert 'denied.json()["error"]["code"] == "principal_scope_mismatch"' in plan_scope_test


def test_plan_execute_input_contracts_are_wired_into_focused_contract_bundle() -> None:
    script = (ROOT / "scripts/test_postgres_contracts.sh").read_text(encoding="utf-8")
    execute_input_test = (ROOT / "tests/test_plan_execute_input_contracts.py").read_text(encoding="utf-8")
    plans_route = (ROOT / "ea/app/api/routes/plans.py").read_text(encoding="utf-8")

    assert "tests/test_plan_execute_input_contracts.py" in script
    assert '"input_json"' in execute_input_test
    assert '"context_refs"' in execute_input_test
    assert "text_or_input_json_required" in execute_input_test
    assert "input_json: dict[str, object]" in plans_route
    assert "context_refs: list[str]" in plans_route
    assert "text_or_input_json_required" in plans_route


def test_plan_graph_validation_contracts_are_wired_into_focused_contract_bundle() -> None:
    script = (ROOT / "scripts/test_postgres_contracts.sh").read_text(encoding="utf-8")
    validation_test = (ROOT / "tests/test_plan_graph_validation_contracts.py").read_text(encoding="utf-8")
    domain_models = (ROOT / "ea/app/domain/models.py").read_text(encoding="utf-8")
    planner = (ROOT / "ea/app/services/planner.py").read_text(encoding="utf-8")
    task_orchestration = (ROOT / "ea/app/services/execution_task_orchestration_service.py").read_text(encoding="utf-8")

    assert "tests/test_plan_graph_validation_contracts.py" in script
    assert "unknown_dependency:step_policy_evaluate:step_missing" in validation_test
    assert "duplicate_step_key:step_input_prepare" in validation_test
    assert "dependency_cycle:step_input_prepare" in validation_test
    assert "PlanValidationError" in domain_models
    assert "validate_plan_spec(plan)" in planner
    assert "validate_plan_spec(plan)" in task_orchestration


def test_step_io_contracts_are_wired_into_focused_contract_bundle() -> None:
    script = (ROOT / "scripts/test_postgres_contracts.sh").read_text(encoding="utf-8")
    io_test = (ROOT / "tests/test_step_io_contracts.py").read_text(encoding="utf-8")
    orchestrator = (ROOT / "ea/app/services/orchestrator.py").read_text(encoding="utf-8")

    assert "tests/test_step_io_contracts.py" in script
    assert "missing_step_input:step_policy_evaluate:normalized_text" in io_test
    assert "missing_step_output:step_artifact_save:missing_output" in io_test
    assert "_validate_step_input_contract" in orchestrator
    assert "_validate_step_output_contract" in orchestrator


def test_task_contract_workflow_templates_are_wired_into_focused_contract_bundle() -> None:
    script = (ROOT / "scripts/test_postgres_contracts.sh").read_text(encoding="utf-8")
    workflow_test = (ROOT / "tests/test_task_contract_step_templates.py").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "tests/test_task_contract_step_templates.py" in script
    assert "artifact_then_dispatch" in workflow_test
    assert "step_connector_dispatch" in workflow_test
    assert "workflow_template" in readme
    assert "artifact_then_dispatch" in readme
    assert "workflow_template" in runbook
    assert "artifact_then_dispatch" in runbook
    assert "stakeholder_dispatch" in http_examples
    assert "artifact_then_dispatch" in http_examples
    assert "stakeholder_dispatch" in smoke_api
    assert "step_connector_dispatch" in smoke_api
    assert "stakeholder_dispatch" in smoke_runtime
    assert "step_connector_dispatch" in smoke_runtime
    assert "Promoted milestone capability `task_contract_workflow_templates` to released" in changelog

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "task_contract_workflow_templates")
    assert capability["status"] == "released"
    assert "release/operator guards now pin that baseline dispatch-template contract" in capability["notes"]


def test_composable_post_artifact_workflow_packs_are_documented_and_guarded() -> None:
    script = (ROOT / "scripts/test_postgres_contracts.sh").read_text(encoding="utf-8")
    workflow_test = (ROOT / "tests/test_task_contract_step_templates.py").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "tests/test_task_contract_step_templates.py" in script
    assert "artifact_then_packs" in workflow_test
    assert "post_artifact_packs" in workflow_test
    assert "unknown_post_artifact_pack:unknown_pack" in workflow_test
    assert "artifact_then_packs" in readme
    assert "post_artifact_packs" in readme
    assert "artifact_then_packs" in runbook
    assert "post_artifact_packs" in runbook
    assert "stakeholder_pack_template" in http_examples
    assert "artifact_then_packs" in http_examples
    assert "Promoted milestone capability `composable_post_artifact_workflow_packs` to released" in changelog

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "composable_post_artifact_workflow_packs"
    )
    assert capability["status"] == "released"
    assert "release/operator guards now pin that composable post-artifact workflow-pack contract" in capability["notes"]


def test_artifact_then_memory_candidate_workflow_template_is_documented_and_guarded() -> None:
    workflow_test = (ROOT / "tests/test_task_contract_step_templates.py").read_text(encoding="utf-8")
    smoke_test = _smoke_runtime_text()
    smoke_script = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "artifact_then_memory_candidate" in workflow_test
    assert "step_memory_candidate_stage" in workflow_test
    assert "stakeholder_memory_candidate" in smoke_test
    assert "step_memory_candidate_stage" in smoke_test
    assert '"memory_write_allowed",' in smoke_test
    assert "stakeholder_memory_candidate" in smoke_script
    assert "step_memory_candidate_stage" in smoke_script
    assert "memory-candidate workflow template to stage a pending principal-scoped candidate row" in smoke_script
    assert "artifact_then_memory_candidate" in readme
    assert "step_input_prepare -> step_policy_evaluate -> step_artifact_save -> step_memory_candidate_stage" in readme
    assert "artifact_then_memory_candidate" in runbook
    assert "step_input_prepare -> step_policy_evaluate -> step_artifact_save -> step_memory_candidate_stage" in runbook
    assert "artifact_then_memory_candidate" in changelog
    assert "Promoted milestone capability `artifact_then_memory_candidate_workflow_template` to released" in changelog
    assert "stakeholder_memory_candidate" in http_examples
    assert "step_memory_candidate_stage" in http_examples

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "artifact_then_memory_candidate_workflow_template"
    )
    assert capability["status"] == "released"


def test_browseract_extract_then_artifact_workflow_template_is_documented_and_guarded() -> None:
    workflow_test = (ROOT / "tests/test_task_contract_step_templates.py").read_text(encoding="utf-8")
    smoke_test = _smoke_runtime_text()
    smoke_script = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "browseract_extract_then_artifact" in workflow_test
    assert "step_browseract_extract" in workflow_test
    assert "browseract_ltd_discovery" in smoke_test
    assert "browseract.extract_account_facts" in smoke_test
    assert "browseract_ltd_discovery" in smoke_script
    assert "step_browseract_extract" in smoke_script
    assert "browseract_extract_then_artifact" in readme
    assert "step_input_prepare -> step_browseract_extract -> step_artifact_save" in readme
    assert "browseract_extract_then_artifact" in runbook
    assert "step_input_prepare -> step_browseract_extract -> step_artifact_save" in runbook
    assert "browseract_extract_then_artifact" in changelog
    assert "browseract_ltd_discovery" in http_examples
    assert "browseract.extract_account_facts" in http_examples

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "browseract_extract_then_artifact_workflow_template"
    )
    assert capability["status"] == "released"


def test_tool_then_artifact_workflow_template_is_documented_and_guarded() -> None:
    workflow_test = (ROOT / "tests/test_task_contract_step_templates.py").read_text(encoding="utf-8")
    smoke_test = _smoke_runtime_text()
    smoke_script = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert 'workflow_template": "tool_then_artifact"' in workflow_test
    assert "browseract_ltd_discovery_generic" in workflow_test
    assert "pre_artifact_tool_name" in workflow_test
    assert "unsupported_tool_then_artifact" in workflow_test
    assert "browseract_ltd_discovery_generic" in smoke_test
    assert 'workflow_template": "tool_then_artifact"' in smoke_test
    assert "browseract_ltd_discovery_generic" in smoke_script
    assert "generic tool-then-artifact workflow template" in smoke_script
    assert "workflow_template=tool_then_artifact" in readme
    assert "workflow_template=tool_then_artifact" in runbook
    assert "workflow_template=tool_then_artifact" in changelog
    assert "browseract_ltd_discovery_generic" in http_examples
    assert '"pre_artifact_tool_name": "browseract.extract_account_facts"' in http_examples

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "tool_then_artifact_workflow_template")
    assert capability["status"] == "released"


def test_browseract_account_inventory_tool_execution_slice_is_documented_and_smoked() -> None:
    workflow_test = (ROOT / "tests/test_task_contract_step_templates.py").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    tool_execution_tests = (ROOT / "tests/test_tool_execution.py").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "/v1/tools/execute" in readme
    assert "browseract.extract_account_inventory" in readme
    assert "/v1/tools/execute" in runbook
    assert "browseract.extract_account_inventory" in runbook
    assert "/v1/tools/execute" in http_examples
    assert "browseract.extract_account_inventory" in http_examples
    assert "browseract_ltd_inventory_refresh" in http_examples
    assert '"pre_artifact_tool_name": "browseract.extract_account_inventory"' in http_examples
    assert "browseract.extract_account_inventory|BrowserAct,Teable,UnknownService|UnknownService|License Tier 4|missing" in smoke_api
    assert "browseract_ltd_inventory_refresh" in smoke_api
    assert "browseract.extract_account_inventory" in smoke_runtime
    assert "browseract_ltd_inventory_refresh" in smoke_runtime
    assert "step_browseract_inventory_extract" in workflow_test
    assert "test_tool_execution_service_executes_builtin_browseract_inventory_handler" in tool_execution_tests

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "browseract_account_inventory_tool_execution_slice"
    )
    assert capability["status"] == "released"


def test_browseract_live_hint_projection_slice_is_documented_and_guarded() -> None:
    workflow_test = (ROOT / "tests/test_task_contract_step_templates.py").read_text(encoding="utf-8")
    tool_execution_tests = (ROOT / "tests/test_tool_execution.py").read_text(encoding="utf-8")
    skills_test = (ROOT / "tests/test_skills.py").read_text(encoding="utf-8")
    smoke_test = _smoke_runtime_text()
    smoke_script = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    skills_service = (ROOT / "ea/app/services/skills.py").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert '"account_hints_json"' in skills_service
    assert '"run_url"' in skills_service
    assert '"instructions"' in skills_service
    assert "requested_run_url" in tool_execution_tests
    assert "account_hints_json" in tool_execution_tests
    assert "run_url" in workflow_test
    assert "requested_run_url" in smoke_test
    assert "account_hints_json" in smoke_script
    assert "Use stored BrowserAct credentials" in smoke_script
    assert "account_hints_json" in skills_test
    assert "run_url" in http_examples
    assert "account_hints_json" in http_examples
    assert "account_hints_json" in readme
    assert "account_hints_json" in runbook
    assert "account_hints_json" in changelog

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "browseract_live_discovery_input_projection"
    )
    assert capability["status"] == "released"


def test_skill_catalog_layer_is_documented_and_guarded() -> None:
    script = (ROOT / "scripts/test_postgres_contracts.sh").read_text(encoding="utf-8")
    skill_test = (ROOT / "tests/test_skills.py").read_text(encoding="utf-8")
    smoke_test = _smoke_runtime_text()
    smoke_script = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    skills_doc = (ROOT / "SKILLS.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "tests/test_skills.py" in script
    assert "meeting_prep" in skill_test
    assert "/v1/skills" in skill_test
    assert "test_skill_catalog_flow_and_meeting_prep_compilation" in smoke_test
    assert "meeting_prep" in smoke_script
    assert "skills ok" in smoke_script
    assert "/v1/skills*" in readme
    assert "SKILLS.md" in readme
    assert "/v1/skills" in runbook
    assert "/v1/skills" in http_examples
    assert "meeting_prep" in http_examples
    assert "Skill Catalog" in skills_doc
    assert "`meeting_prep`" in skills_doc
    assert "first-class `/v1/skills` catalog" in changelog
    assert "Promoted milestone capability `skill_catalog_layer` to released" in changelog

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "skill_catalog_layer")
    assert capability["status"] == "released"


def test_ltd_inventory_refresh_skill_slice_is_documented_and_guarded() -> None:
    skills_test = (ROOT / "tests/test_skills.py").read_text(encoding="utf-8")
    smoke_test = _smoke_runtime_text()
    smoke_script = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    skills_doc = (ROOT / "SKILLS.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "ltd_inventory_refresh" in skills_test
    assert "test_skill_catalog_can_execute_ltd_inventory_refresh_skill" in skills_test
    assert "ltd_inventory_refresh" in smoke_test
    assert "test_skill_catalog_can_project_ltd_inventory_refresh_runtime" in smoke_test
    assert "ltd_inventory_refresh" in smoke_script
    assert "browseract.extract_account_inventory" in smoke_script
    assert "ltd_inventory_refresh" in readme
    assert "ltd_inventory_refresh" in runbook
    assert "ltd_inventory_refresh" in changelog
    assert "ltd_inventory_refresh" in http_examples
    assert "`ltd_inventory_refresh`" in skills_doc

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "ltd_inventory_refresh_skill_catalog_slice"
    )
    assert capability["status"] == "released"


def test_chummer6_visual_director_skill_slice_is_documented_and_guarded() -> None:
    skills_test = (ROOT / "tests/test_skills.py").read_text(encoding="utf-8")
    worker_test = (ROOT / "tests/test_chummer6_guide_worker.py").read_text(encoding="utf-8")
    smoke_script = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    skills_doc = (ROOT / "SKILLS.md").read_text(encoding="utf-8")
    worker = (ROOT / "scripts/chummer6_guide_worker.py").read_text(encoding="utf-8")
    readiness = (ROOT / "scripts/chummer6_provider_readiness.py").read_text(encoding="utf-8")
    fleet_scaffolder_path = FLEET_ROOT / "scripts" / "advance_ea_chummer6_worker.py"
    fleet_scaffolder = _optional_text(fleet_scaffolder_path)

    assert "chummer6_visual_director" in skills_test
    assert "provider.gemini_vortex.structured_generate" in skills_test
    assert "test_chat_json_rejects_legacy_provider_aliases" in worker_test
    assert "test_ea_json_executes_public_writer_skill_identity_by_default" in worker_test
    assert "test_ea_json_can_execute_visual_director_skill_identity" in worker_test
    assert "chummer6_visual_director" in smoke_script
    assert "Gemini Vortex" in smoke_script
    assert "chummer6_visual_director" not in readme
    assert "chummer6_visual_director" in runbook
    assert "chummer6_visual_director" in http_examples
    assert "`chummer6_visual_director`" in skills_doc
    assert "chummer6_public_writer" in skills_doc
    assert "Gemini Vortex" in skills_doc
    assert 'VISUAL_DIRECTOR_SKILL_KEY = "chummer6_visual_director"' in worker
    assert "unsupported_chummer6_text_provider" in worker
    assert "codex_json(" not in worker
    assert "onemin_json(" not in worker
    assert 'return ["ea"]' in readiness
    assert "expected to run through EA only" in readiness
    if fleet_scaffolder:
        assert "codex_json(" not in fleet_scaffolder
        assert "onemin_json(" not in fleet_scaffolder
        assert '"secondary": ["Codex"]' not in fleet_scaffolder


def test_chummer6_public_writer_skill_slice_is_documented_and_guarded() -> None:
    skills_test = (ROOT / "tests/test_skills.py").read_text(encoding="utf-8")
    worker_test = (ROOT / "tests/test_chummer6_guide_worker.py").read_text(encoding="utf-8")
    smoke_script = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    skills_doc = (ROOT / "SKILLS.md").read_text(encoding="utf-8")
    worker = (ROOT / "scripts/chummer6_guide_worker.py").read_text(encoding="utf-8")

    assert "chummer6_public_writer" in skills_test
    assert "test_skill_catalog_can_execute_chummer6_public_writer_skill" in skills_test
    assert "test_public_reader_guard_rejects_maintainer_imperatives" in worker_test
    assert "chummer6_public_writer" in smoke_script
    assert "chummer6_public_writer" not in readme
    assert "chummer6_public_writer" in runbook
    assert "chummer6_public_writer" in http_examples
    assert "`chummer6_public_writer`" in skills_doc
    assert 'PUBLIC_WRITER_SKILL_KEY = "chummer6_public_writer"' in worker
    assert "test_ea_json_missing_writer_skill_does_not_fall_back_to_visual_director" in worker_test
    assert "apply_skill_payload(EA_CONTAINER.skills, payload)" not in worker
    assert "current `chummer6_guide_worker.py` generation path" in skills_doc
    assert "public_screenshot_registry" not in skills_doc


def test_chummer6_visual_skill_slice_tracks_image_curation_and_local_mirror_contracts() -> None:
    skills_doc = (ROOT / "SKILLS.md").read_text(encoding="utf-8")
    bootstrap_script = (ROOT / "scripts/bootstrap_chummer6_guide_skill.py").read_text(encoding="utf-8")
    image_curation = (ROOT / ".codex-design" / "product" / "PUBLIC_GUIDE_IMAGE_CURATION.yaml").read_text(encoding="utf-8")

    assert "public_guide_image_curation" in skills_doc
    assert "public_guide_image_curation" in bootstrap_script
    assert "page_image_policy_source" in bootstrap_script
    assert "PUBLIC_SCREENSHOT_REGISTRY.yaml" not in bootstrap_script
    assert "assets/hero/chummer6-hero.png" in image_curation


def test_browseract_bootstrap_manager_skill_slice_is_documented_and_guarded() -> None:
    skills_test = (ROOT / "tests/test_skills.py").read_text(encoding="utf-8")
    smoke_script = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    skills_doc = (ROOT / "SKILLS.md").read_text(encoding="utf-8")
    bootstrap_script = (ROOT / "scripts/bootstrap_browseract_bootstrap_skill.py").read_text(encoding="utf-8")
    fleet_bootstrap = _optional_text(FLEET_ROOT / "scripts" / "bootstrap_ea_browseract_architect.py")
    fleet_deploy = _optional_text(FLEET_ROOT / "scripts" / "deploy.sh")

    assert "browseract_bootstrap_manager" in skills_test
    assert "browseract.build_workflow_spec" in skills_test
    assert "browseract_bootstrap_manager" in smoke_script
    assert "step_browseract_workflow_spec_build" in smoke_script
    assert "browseract_bootstrap_manager" in readme
    assert "browseract_bootstrap_manager" in runbook
    assert "browseract_bootstrap_manager" in http_examples
    assert "`browseract_bootstrap_manager`" in skills_doc
    assert '"workflow_template": "tool_then_artifact"' in bootstrap_script
    assert '"allowed_tools": ["browseract.build_workflow_spec", "artifact_repository"]' in bootstrap_script
    assert '"pre_artifact_capability_key": "workflow_spec_build"' in bootstrap_script
    assert '"secondary": ["Codex"]' not in bootstrap_script
    if fleet_bootstrap:
        assert '"secondary": ["Codex"]' not in fleet_bootstrap
    if fleet_deploy:
        assert 'python3 /docker/EA/scripts/bootstrap_browseract_bootstrap_skill.py' in fleet_deploy


def test_skill_provider_hints_projection_is_documented_and_released() -> None:
    skills_route = (ROOT / "ea/app/api/routes/skills.py").read_text(encoding="utf-8")
    skills_service = (ROOT / "ea/app/services/skills.py").read_text(encoding="utf-8")
    skills_models = (ROOT / "ea/app/domain/models.py").read_text(encoding="utf-8")
    skills_test = (ROOT / "tests/test_skills.py").read_text(encoding="utf-8")
    smoke_test = _smoke_runtime_text()
    smoke_script = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    skills_doc = (ROOT / "SKILLS.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "provider_hints_json" in skills_models
    assert "provider_hints_json" in skills_service
    assert "provider_hints_json" in skills_route
    assert 'body["provider_hints_json"]["primary"] == ["1min.AI"]' in skills_test
    assert 'fetched_body["provider_hints_json"]["research"] == ["BrowserAct", "Paperguide"]' in skills_test
    assert 'created.json()["provider_hints_json"]["primary"] == ["1min.AI"]' in smoke_test
    assert 'fetched.json()["provider_hints_json"]["research"] == ["BrowserAct", "Paperguide"]' in smoke_test
    assert "provider_hints_json" in smoke_script
    assert "provider-hint" in readme
    assert "provider policy" in runbook
    assert "Promoted milestone capability `skill_provider_hints_projection` to released" in changelog
    assert "provider_hints_json" in changelog
    assert "provider_hints_json" in http_examples
    assert "provider_hints_json" in skills_doc

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "skill_provider_hints_projection")
    assert capability["status"] == "released"


def test_skill_provider_hint_filtering_is_documented_and_guarded() -> None:
    skills_route = (ROOT / "ea/app/api/routes/skills.py").read_text(encoding="utf-8")
    skills_service = (ROOT / "ea/app/services/skills.py").read_text(encoding="utf-8")
    skills_test = (ROOT / "tests/test_skills.py").read_text(encoding="utf-8")
    smoke_test = _smoke_runtime_text()
    smoke_script = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    skills_doc = (ROOT / "SKILLS.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "provider_hint: str = Query" in skills_route
    assert "provider_hint=provider_hint" in skills_route
    assert "def list_skills(self, limit: int = 100, provider_hint: str = \"\")" in skills_service
    assert "_collect_string_values" in skills_service
    assert 'client.get("/v1/skills", params={"limit": 10, "provider_hint": "browseract"})' in skills_test
    assert 'client.get("/v1/skills", params={"limit": 10, "provider_hint": "browseract"})' in smoke_test
    assert "provider_hint=browseract" in smoke_script
    assert "provider_hint=BrowserAct" in readme
    assert "provider_hint=<value>" in runbook
    assert "provider_hint=<value>" in changelog
    assert "provider_hint=BrowserAct" in http_examples
    assert "provider_hint=BrowserAct" in skills_doc

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "skill_provider_hint_filtering")
    assert capability["status"] == "released"


def test_session_status_transition_api_is_documented_and_guarded() -> None:
    queue_retry_test = (ROOT / "tests/test_queue_retry_contracts.py").read_text(encoding="utf-8")
    postgres_contract_test = (ROOT / "tests/test_postgres_contract_matrix_integration.py").read_text(encoding="utf-8")
    ledger_repo = (ROOT / "ea/app/repositories/ledger.py").read_text(encoding="utf-8")
    ledger_postgres = (ROOT / "ea/app/repositories/ledger_postgres.py").read_text(encoding="utf-8")
    orchestrator = (ROOT / "ea/app/services/orchestrator.py").read_text(encoding="utf-8")
    approval_pause_service = (ROOT / "ea/app/services/execution_approval_pause_service.py").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "set_session_status" in queue_retry_test
    assert "_RecordingLedger" in queue_retry_test
    assert 'ledger.status_updates == ["running", "queued"]' in queue_retry_test
    assert 'ledger.completion_updates == []' in queue_retry_test
    assert 'ledger.set_session_status(session.session_id, "awaiting_approval")' in postgres_contract_test
    assert "def set_session_status(" in ledger_repo
    assert "def set_session_status(" in ledger_postgres
    assert '_set_session_status(session_id, "awaiting_approval")' in approval_pause_service
    assert "set_session_status(...)" in readme
    assert "set_session_status(...)" in runbook
    assert "Promoted milestone capability `session_status_transition_api` to released" in changelog
    assert "set_session_status(...)" in changelog

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "session_status_transition_api")
    assert capability["status"] == "released"
    assert "release/operator guards now pin that explicit nonterminal session-status transition contract" in capability["notes"]


def test_skill_identity_projection_is_documented_and_guarded() -> None:
    plans_route = (ROOT / "ea/app/api/routes/plans.py").read_text(encoding="utf-8")
    skills_test = (ROOT / "tests/test_skills.py").read_text(encoding="utf-8")
    execute_input_test = (ROOT / "tests/test_plan_execute_input_contracts.py").read_text(encoding="utf-8")
    smoke_test = _smoke_runtime_text()
    smoke_script = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    openapi_test = (ROOT / "tests/test_openapi_async_acceptance_examples_contracts.py").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "class PlanCompileOut(BaseModel):" in plans_route
    assert "skill_key: str" in plans_route
    assert "_resolve_skill_key(" in plans_route
    assert 'compiled.json()["skill_key"] == "meeting_prep"' in skills_test
    assert 'executed.json()["skill_key"] == "meeting_prep"' in skills_test
    assert 'body["skill_key"] == "rewrite_text"' in execute_input_test
    assert 'execute.json()["skill_key"] == "rewrite_retry_delayed_plan"' in execute_input_test
    assert 'compiled.json()["skill_key"] == "meeting_prep"' in smoke_test
    assert "compiled.get('skill_key','')" in smoke_script
    assert "body.get('skill_key','')" in smoke_script
    assert 'plan_approval["skill_key"] == "decision_briefing"' in openapi_test
    assert "resolved `skill_key`" in readme
    assert "resolved `skill_key`" in runbook
    assert "resolved `skill_key`" in changelog

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "skill_identity_projection")
    assert capability["status"] == "released"


def test_runtime_skill_identity_projection_is_documented_and_guarded() -> None:
    rewrite_route = (ROOT / "ea/app/api/routes/rewrite.py").read_text(encoding="utf-8")
    skills_test = (ROOT / "tests/test_skills.py").read_text(encoding="utf-8")
    smoke_test = _smoke_runtime_text()
    smoke_script = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "intent_skill_key: str" in rewrite_route
    assert "skill_key: str = \"\"" in rewrite_route
    assert "_resolve_skill_key(" in rewrite_route
    assert 'session_body["intent_skill_key"] == "meeting_prep"' in skills_test
    assert 'fetched_artifact.json()["skill_key"] == "meeting_prep"' in skills_test
    assert 'session_body["intent_skill_key"] == "stakeholder_briefing"' in smoke_test
    assert 'fetched_receipt.json()["skill_key"] == "stakeholder_briefing"' in smoke_test
    assert "body.get('intent_skill_key','')" in smoke_script
    assert "body.get('skill_key','')" in smoke_script
    assert "intent_skill_key" in readme
    assert "intent_skill_key" in runbook
    assert "intent_skill_key" in changelog

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "runtime_skill_identity_projection")
    assert capability["status"] == "released"


def test_plan_skill_key_entrypoint_alias_is_documented_and_guarded() -> None:
    plans_route = (ROOT / "ea/app/api/routes/plans.py").read_text(encoding="utf-8")
    skills_test = (ROOT / "tests/test_skills.py").read_text(encoding="utf-8")
    execute_input_test = (ROOT / "tests/test_plan_execute_input_contracts.py").read_text(encoding="utf-8")
    smoke_test = _smoke_runtime_text()
    smoke_script = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "skill_key: str = Field(default=\"\", max_length=200)" in plans_route
    assert "_resolve_task_key(" in plans_route
    assert "task_or_skill_key_required" in plans_route
    assert "task_skill_key_mismatch" in plans_route
    assert "compiled_via_skill = client.post(" in skills_test
    assert "executed_via_skill = client.post(" in skills_test
    assert "task_or_skill_key_required" in execute_input_test
    assert "compiled_via_skill = client.post(" in smoke_test
    assert "LTD_SKILL_PLAN_BY_SKILL_JSON" in smoke_script
    assert '"skill_key":"meeting_prep"' in smoke_script
    assert '"skill_key":"ltd_inventory_refresh"' in smoke_script
    assert "accepts either `task_key` or `skill_key`" in readme
    assert "accepts either `task_key` or `skill_key`" in runbook
    assert "accept either `task_key` or `skill_key`" in changelog
    assert '"skill_key": "meeting_prep"' in http_examples
    assert '"skill_key": "ltd_inventory_refresh"' in http_examples

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "plan_skill_key_entrypoint_alias")
    assert capability["status"] == "released"


def test_ltd_discovery_markdown_refresh_is_documented_and_guarded() -> None:
    service = (ROOT / "ea/app/services/ltd_inventory_markdown.py").read_text(encoding="utf-8")
    shell_script = (ROOT / "scripts/refresh_ltds_from_inventory.sh").read_text(encoding="utf-8")
    script = (ROOT / "scripts/refresh_ltds_from_inventory.py").read_text(encoding="utf-8")
    test_file = (ROOT / "tests/test_ltd_inventory_markdown.py").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    ltds = (ROOT / "LTDs.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "DISCOVERY_TRACKING_HEADING" in service
    assert "build_discovery_updates" in service
    assert "update_discovery_tracking_table" in service
    assert "refresh_inventory_markdown" in service
    assert "refresh_ltds_from_inventory.py" in shell_script
    assert "refresh_inventory_markdown" in script
    assert "test_refresh_inventory_markdown_updates_rows_and_syncs_metadata" in test_file
    assert "test_refresh_ltds_script_can_write_updated_markdown" in test_file
    assert "refresh_ltds_from_inventory.sh" in readme
    assert "refresh_ltds_from_inventory.sh" in runbook
    assert "refresh_ltds_from_inventory.sh" in changelog
    assert "refresh_ltds_from_inventory.sh" in ltds

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "ltd_discovery_markdown_refresh")
    assert capability["status"] == "released"


def test_ltd_discovery_api_refresh_runner_is_documented_and_guarded() -> None:
    service = (ROOT / "ea/app/services/ltd_inventory_api.py").read_text(encoding="utf-8")
    shell_script = (ROOT / "scripts/refresh_ltds_via_api.sh").read_text(encoding="utf-8")
    script = (ROOT / "scripts/refresh_ltds_via_api.py").read_text(encoding="utf-8")
    test_file = (ROOT / "tests/test_ltd_inventory_api.py").read_text(encoding="utf-8")
    smoke_script = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    ltds = (ROOT / "LTDs.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "build_inventory_execute_payload" in service
    assert "extract_inventory_output_json" in service
    assert "refresh_ltds_via_api.py" in shell_script
    assert "/v1/plans/execute" in script
    assert "refresh_inventory_markdown" in script
    assert "test_refresh_ltds_via_api_script_executes_skill_and_updates_markdown" in test_file
    assert "refresh_ltds_via_api.sh" in smoke_script
    assert "refresh_ltds_via_api.sh" in readme
    assert "refresh_ltds_via_api.sh" in runbook
    assert "refresh_ltds_via_api.sh" in changelog
    assert "refresh_ltds_via_api.sh" in ltds

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "ltd_discovery_api_refresh_runner")
    assert capability["status"] == "released"


def test_tibor_smoke_runner_uses_repo_rooted_ltd_and_rewrite_reset_contracts() -> None:
    smoke_script = (ROOT / "scripts/smoke_api_tibor.sh").read_text(encoding="utf-8")

    assert "reset_rewrite_contract()" in smoke_script
    assert "trap cleanup_smoke_contract_state EXIT" in smoke_script
    assert 'cp "${EA_ROOT}/LTDs.md"' in smoke_script
    assert 'bash "${EA_ROOT}/scripts/refresh_ltds_via_api.sh"' in smoke_script


def test_artifact_evidence_pack_output_template_is_documented_and_guarded() -> None:
    planner = (ROOT / "ea/app/services/planner.py").read_text(encoding="utf-8")
    runtime_service = (ROOT / "ea/app/services/execution_step_runtime_service.py").read_text(encoding="utf-8")
    workflow_test = (ROOT / "tests/test_task_contract_step_templates.py").read_text(encoding="utf-8")
    smoke_script = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "_artifact_output_template_key" in planner
    assert "artifact_output_template" in planner
    assert "\"format\": \"evidence_pack\"" in runtime_service
    assert "test_planner_can_project_evidence_pack_artifact_output_template" in workflow_test
    assert "test_artifact_then_memory_candidate_evidence_pack_persists_structured_output" in workflow_test
    assert "plan_execute_artifact_json" in smoke_script
    assert "artifact_output_template\":\"evidence_pack" in smoke_script
    assert "artifact_output_template=evidence_pack" in readme
    assert "artifact_output_template=evidence_pack" in runbook
    assert "Promoted milestone capability `artifact_evidence_pack_output_template` to released" in changelog
    assert "artifact_output_template=evidence_pack" in changelog

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "artifact_evidence_pack_output_template")
    assert capability["status"] == "released"
    assert "release/operator guards now pin that evidence-pack output-template contract" in capability["notes"]


def test_evidence_pack_memory_candidate_projection_is_documented_and_guarded() -> None:
    runtime_service = (ROOT / "ea/app/services/execution_step_runtime_service.py").read_text(encoding="utf-8")
    workflow_test = (ROOT / "tests/test_task_contract_step_templates.py").read_text(encoding="utf-8")
    smoke_script = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert '"evidence_pack": artifact_structured_output_json' in runtime_service
    assert 'fact_json["claims"]' in workflow_test
    assert 'fact_json["evidence_refs"]' in workflow_test
    assert "EVIDENCE_CANDIDATE_FIELDS" in smoke_script
    assert "memory-candidate staging" in readme
    assert "memory-candidate staging" in runbook
    assert "memory-candidate staging" in changelog
    assert "Promoted milestone capability `evidence_pack_memory_candidate_projection` to released" in changelog

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "evidence_pack_memory_candidate_projection")
    assert capability["status"] == "released"


def test_evidence_object_ledger_api_is_documented_and_guarded() -> None:
    router = (ROOT / "ea/app/api/routes/evidence.py").read_text(encoding="utf-8")
    runtime = (ROOT / "ea/app/services/evidence_runtime.py").read_text(encoding="utf-8")
    tool_execution = (ROOT / "ea/app/services/tool_execution_artifact_adapter.py").read_text(encoding="utf-8")
    tool_test = (ROOT / "tests/test_tool_execution.py").read_text(encoding="utf-8")
    postgres_contracts = (ROOT / "tests/test_postgres_contract_matrix_integration.py").read_text(encoding="utf-8")
    smoke_test = _smoke_runtime_text()
    smoke_script = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert 'prefix="/v1/evidence"' in router
    assert "merge_objects(" in runtime
    assert '"evidence_object_id"' in tool_execution
    assert "test_tool_execution_service_materializes_evidence_objects_for_evidence_pack_artifacts" in tool_test
    assert "test_postgres_evidence_object_repo_materializes_queries_and_merges_evidence_pack_rows" in postgres_contracts
    assert "test_evidence_object_routes_materialize_and_merge_evidence_pack_artifacts" in smoke_test
    assert "EVIDENCE_OBJECT_FIELDS" in smoke_script
    assert "/v1/evidence/objects" in readme
    assert "/v1/evidence/objects" in runbook
    assert "/v1/evidence/objects" in changelog
    assert "/v1/evidence/objects" in http_examples
    assert "Promoted milestone capability `evidence_object_ledger_api` to released" in changelog

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "evidence_object_ledger_api")
    assert capability["status"] == "released"


def test_knowledge_fabric_projection_slice_is_documented_and_released() -> None:
    evidence_runtime = (ROOT / "ea/app/services/evidence_runtime.py").read_text(encoding="utf-8")
    evidence_router = (ROOT / "ea/app/api/routes/evidence.py").read_text(encoding="utf-8")
    evidence_models = (ROOT / "ea/app/domain/models.py").read_text(encoding="utf-8")
    smoke_test = _smoke_runtime_text()
    smoke_script = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    chummer_worker_test = (ROOT / "tests/test_chummer6_guide_worker.py").read_text(encoding="utf-8")
    chummer_canon_test = (ROOT / "tests/test_chummer6_guide_canon.py").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "citation_handles = normalize_evidence_strings" in evidence_runtime
    assert 'prefix="/v1/evidence"' in evidence_router
    assert "def evidence_citation_handle(" in evidence_models
    assert "test_evidence_object_routes_materialize_and_merge_evidence_pack_artifacts" in smoke_test
    assert "EVIDENCE_OBJECT_FIELDS" in smoke_script
    assert "how_can_i_help" in chummer_worker_test
    assert "test_load_faq_and_help_canon_track_public_question_sets" in chummer_canon_test
    assert "/v1/evidence/objects*" in readme
    assert "/v1/evidence/objects" in runbook
    assert "Promoted milestone capability `knowledge_fabric_projection_slice` to released" in changelog

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "knowledge_fabric_projection_slice")
    assert capability["status"] == "released"
    assert "Release/operator guards now pin these citation/query/help contracts." in capability["notes"]


def test_dispatch_then_memory_candidate_workflow_template_is_documented_and_guarded() -> None:
    workflow_test = (ROOT / "tests/test_task_contract_step_templates.py").read_text(encoding="utf-8")
    smoke_test = _smoke_runtime_text()
    smoke_script = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "artifact_then_dispatch_then_memory_candidate" in workflow_test
    assert "stakeholder_dispatch_memory_candidate" in workflow_test
    assert "dispatch-memory@example.com" in workflow_test
    assert "stakeholder_dispatch_memory_candidate" in smoke_test
    assert "dispatch-memory@example.com" in smoke_test
    assert "stakeholder_dispatch_memory_candidate" in smoke_script
    assert "dispatch-memory@example.com" in smoke_script
    assert "artifact_then_dispatch_then_memory_candidate" in readme
    assert "step_input_prepare -> step_artifact_save -> step_policy_evaluate -> step_connector_dispatch -> step_memory_candidate_stage" in readme
    assert "artifact_then_dispatch_then_memory_candidate" in runbook
    assert "step_input_prepare -> step_artifact_save -> step_policy_evaluate -> step_connector_dispatch -> step_memory_candidate_stage" in runbook
    assert "artifact_then_dispatch_then_memory_candidate" in changelog
    assert "stakeholder_dispatch_memory_candidate" in http_examples
    assert "artifact_then_dispatch_then_memory_candidate" in http_examples
    assert "Promoted milestone capability `dispatch_then_memory_candidate_workflow_template` to released" in changelog

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "dispatch_then_memory_candidate_workflow_template"
    )
    assert capability["status"] == "released"


def test_review_dispatch_then_memory_candidate_workflow_template_is_documented_and_guarded() -> None:
    workflow_test = (ROOT / "tests/test_task_contract_step_templates.py").read_text(encoding="utf-8")
    smoke_test = _smoke_runtime_text()
    smoke_script = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "stakeholder_review_dispatch_memory_candidate" in workflow_test
    assert "reviewed-memory@example.com" in workflow_test
    assert "stakeholder_review_dispatch_memory_candidate" in smoke_test
    assert "reviewed-memory@example.com" in smoke_test
    assert "stakeholder_review_dispatch_memory_candidate" in smoke_script
    assert "reviewed-memory@example.com" in smoke_script
    assert "artifact_then_dispatch_then_memory_candidate" in readme
    assert "step_input_prepare -> step_human_review -> step_artifact_save -> step_policy_evaluate -> step_connector_dispatch -> step_memory_candidate_stage" in readme
    assert "artifact_then_dispatch_then_memory_candidate" in runbook
    assert "step_input_prepare -> step_human_review -> step_artifact_save -> step_policy_evaluate -> step_connector_dispatch -> step_memory_candidate_stage" in runbook
    assert "Promoted milestone capability `review_dispatch_then_memory_candidate_workflow_template` to released" in changelog
    assert "hybrid human-review case" in changelog
    assert "stakeholder_review_dispatch_memory_candidate" in http_examples
    assert "artifact_then_dispatch_then_memory_candidate" in http_examples

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "review_dispatch_then_memory_candidate_workflow_template"
    )
    assert capability["status"] == "released"


def test_unknown_workflow_templates_fail_fast_at_planner_and_api_boundaries() -> None:
    workflow_test = (ROOT / "tests/test_task_contract_step_templates.py").read_text(encoding="utf-8")
    planner = (ROOT / "ea/app/services/planner.py").read_text(encoding="utf-8")
    plans_route = (ROOT / "ea/app/api/routes/plans.py").read_text(encoding="utf-8")
    rewrite_route = (ROOT / "ea/app/api/routes/rewrite.py").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "unknown_workflow_template:not_real" in workflow_test
    assert '"/v1/rewrite/artifact"' in workflow_test
    assert "_workflow_template_builders" in planner
    assert "unknown_workflow_template:" in planner
    assert "except PlanValidationError as exc" in plans_route
    assert "status_code=422" in plans_route
    assert "except PlanValidationError as exc" in rewrite_route
    assert "unknown_workflow_template:<value>" in readme
    assert "unknown_workflow_template:<value>" in runbook
    assert "unknown_workflow_template:<value>" in changelog
    assert "Promoted milestone capability `workflow_template_registry_validation` to released" in changelog

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "workflow_template_registry_validation")
    assert capability["status"] == "released"


def test_review_then_dispatch_workflow_template_is_documented_and_guarded() -> None:
    workflow_test = (ROOT / "tests/test_task_contract_step_templates.py").read_text(encoding="utf-8")
    smoke_test = _smoke_runtime_text()
    smoke_script = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "stakeholder_review_dispatch" in workflow_test
    assert "stakeholder_review_dispatch" in smoke_test
    assert "hybrid@example.com" in smoke_test
    assert '"step_input_prepare",' in workflow_test
    assert '"step_human_review",' in workflow_test
    assert '"step_artifact_save",' in workflow_test
    assert '"step_policy_evaluate",' in workflow_test
    assert '"step_connector_dispatch",' in workflow_test
    assert "review and send a stakeholder briefing" in workflow_test
    assert "stakeholder_review_dispatch" in smoke_script
    assert "hybrid@example.com" in smoke_script
    assert "review-then-dispatch workflow to pause behind human review first" in smoke_script
    assert "expected review-then-dispatch workflow to pause for approval after human return and artifact persistence" in smoke_script
    assert "artifact_then_dispatch" in readme
    assert "step_human_review -> step_artifact_save -> step_policy_evaluate -> step_connector_dispatch" in readme
    assert "artifact_then_dispatch" in runbook
    assert "step_human_review -> step_artifact_save -> step_policy_evaluate -> step_connector_dispatch" in runbook
    assert "Promoted milestone capability `review_then_dispatch_workflow_template` to released" in changelog
    assert "combined human-review case" in changelog

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "review_then_dispatch_workflow_template")
    assert capability["status"] == "released"
    assert "release/operator guards now pin that review-then-dispatch workflow" in capability["notes"]


def test_execution_queue_retry_runtime_is_documented_and_guarded() -> None:
    retry_test = (ROOT / "tests/test_queue_retry_contracts.py").read_text(encoding="utf-8")
    postgres_matrix = (ROOT / "tests/test_postgres_contract_matrix_integration.py").read_text(encoding="utf-8")
    script = (ROOT / "scripts/test_postgres_contracts.sh").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "test_retry_failure_strategy_requeues_a_failed_step_until_it_succeeds" in retry_test
    assert "test_retry_failure_strategy_exhausts_into_terminal_session_failure" in retry_test
    assert "step_retry_scheduled" in retry_test
    assert "test_postgres_execution_queue_retry_requeues_the_same_row" in postgres_matrix
    assert "retry_queue_item" in postgres_matrix
    assert "tests/test_queue_retry_contracts.py" in script
    assert "failure_strategy=retry" in readme
    assert "failure_strategy=retry" in runbook
    assert "Queued step failures can now actually honor `failure_strategy=retry`" in changelog

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "execution_queue_retry_runtime")
    assert capability["status"] == "released"


def test_inline_retry_drain_runtime_is_documented_and_guarded() -> None:
    retry_test = (ROOT / "tests/test_queue_retry_contracts.py").read_text(encoding="utf-8")
    queue_service = (ROOT / "ea/app/services/execution_queue_service.py").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "test_execute_task_artifact_drains_zero_backoff_retries_inline_to_completion" in retry_test
    assert "test_approval_resume_drains_zero_backoff_retries_inline_to_completion" in retry_test
    assert "drain_session_inline(" in queue_service
    assert "_next_eligible_queue_item_for_session" in queue_service
    assert "zero-backoff retries now keep draining same-session queue work inline" in readme
    assert "retry_backoff_seconds=0" in runbook
    assert "Zero-backoff retries now keep draining the same session inline" in changelog

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "inline_retry_drain_runtime")
    assert capability["status"] == "released"


def test_contract_retry_policy_metadata_is_documented_and_guarded() -> None:
    planner_test = (ROOT / "tests/test_planner.py").read_text(encoding="utf-8")
    workflow_test = (ROOT / "tests/test_task_contract_step_templates.py").read_text(encoding="utf-8")
    retry_test = (ROOT / "tests/test_queue_retry_contracts.py").read_text(encoding="utf-8")
    planner = (ROOT / "ea/app/services/planner.py").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "test_planner_can_compile_artifact_retry_policy_from_task_contract_metadata" in planner_test
    assert "test_planner_can_compile_dispatch_retry_policy_from_task_contract_metadata" in workflow_test
    assert "test_execute_task_artifact_uses_compiled_artifact_retry_policy_from_contract_metadata" in retry_test
    assert "_step_retry_policy" in planner
    assert 'prefix="artifact"' in planner
    assert 'prefix="dispatch"' in planner
    assert "budget_policy_json.artifact_failure_strategy|artifact_max_attempts|artifact_retry_backoff_seconds" in readme
    assert "artifact_failure_strategy|artifact_max_attempts|artifact_retry_backoff_seconds" in runbook
    assert "Task-contract metadata can now tune the built-in artifact and dispatch retry posture" in changelog

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "contract_retry_policy_metadata")
    assert capability["status"] == "released"


def test_delayed_retry_async_acceptance_is_documented_and_guarded() -> None:
    retry_test = (ROOT / "tests/test_queue_retry_contracts.py").read_text(encoding="utf-8")
    plan_test = (ROOT / "tests/test_plan_execute_input_contracts.py").read_text(encoding="utf-8")
    rewrite_test = (ROOT / "tests/test_rewrite_api_scope_contracts.py").read_text(encoding="utf-8")
    openapi_test = (ROOT / "tests/test_openapi_async_acceptance_examples_contracts.py").read_text(encoding="utf-8")
    orchestrator = (ROOT / "ea/app/services/orchestrator.py").read_text(encoding="utf-8")
    plans_route = (ROOT / "ea/app/api/routes/plans.py").read_text(encoding="utf-8")
    rewrite_route = (ROOT / "ea/app/api/routes/rewrite.py").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "test_execute_task_artifact_returns_queued_async_state_for_delayed_retry" in retry_test
    assert "test_approval_resume_keeps_delayed_retry_sessions_async_instead_of_erroring" in retry_test
    assert "test_plan_execute_surfaces_delayed_retry_as_queued_async_acceptance" in plan_test
    assert "test_rewrite_artifact_surfaces_delayed_retry_as_queued_async_acceptance" in rewrite_test
    assert 'example["status"] == "queued"' in openapi_test
    assert "AsyncExecutionQueuedError" in orchestrator
    assert "except AsyncExecutionQueuedError as exc" in plans_route
    assert "except AsyncExecutionQueuedError as exc" in rewrite_route
    assert "first-class `202 queued` async acceptance" in readme
    assert "`202 queued`" in runbook
    assert "Nonzero-backoff retries now surface as a first-class `202 queued` async acceptance" in changelog

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "delayed_retry_async_acceptance")
    assert capability["status"] == "released"


def test_review_dispatch_delayed_retry_runtime_is_documented_and_guarded() -> None:
    workflow_test = (ROOT / "tests/test_task_contract_step_templates.py").read_text(encoding="utf-8")
    smoke_test = _smoke_runtime_text()
    smoke_script = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "test_planner_can_compile_review_then_dispatch_retry_policy_from_task_contract_metadata" in workflow_test
    assert "test_review_then_dispatch_workflow_template_keeps_delayed_dispatch_retry_async_after_approval" in workflow_test
    assert "test_review_then_dispatch_delayed_retry_stays_queued_after_http_approval" in smoke_test
    assert "stakeholder_review_dispatch_retry" in smoke_script
    assert "hybrid-retry@example.com" in smoke_script
    assert "expected delayed review-then-dispatch approval flow to leave dispatch queued behind next_attempt_at" in smoke_script
    assert "dispatch_failure_strategy|max_attempts|retry_backoff_seconds" in readme
    assert "dispatch_failure_strategy|dispatch_max_attempts|dispatch_retry_backoff_seconds" in runbook
    assert "Promoted milestone capability `review_dispatch_delayed_retry_runtime` to released" in changelog

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "review_dispatch_delayed_retry_runtime")
    assert capability["status"] == "released"


def test_principal_fallback_contracts_are_wired_into_focused_contract_bundle() -> None:
    script = (ROOT / "scripts/test_postgres_contracts.sh").read_text(encoding="utf-8")
    fallback_test = (ROOT / "tests/test_principal_fallback_contracts.py").read_text(encoding="utf-8")

    assert "tests/test_principal_fallback_contracts.py" in script
    assert "principal_id_required" in fallback_test
    assert "planner.build_plan" in fallback_test
    assert "orchestrator.build_artifact" in fallback_test
    assert "orchestrator.execute_task_artifact" in fallback_test
    assert "service.compile_rewrite_intent" in fallback_test


def test_artifact_principal_ownership_is_guarded_across_routes_and_smoke() -> None:
    rewrite_route = (ROOT / "ea/app/api/routes/rewrite.py").read_text(encoding="utf-8")
    plans_route = (ROOT / "ea/app/api/routes/plans.py").read_text(encoding="utf-8")
    artifact_repo = (ROOT / "ea/app/repositories/artifacts_postgres.py").read_text(encoding="utf-8")
    postgres_test = (ROOT / "tests/test_artifacts_postgres_integration.py").read_text(encoding="utf-8")
    rewrite_scope_test = (ROOT / "tests/test_rewrite_scope_contracts.py").read_text(encoding="utf-8")
    rewrite_api_scope_test = (ROOT / "tests/test_rewrite_api_scope_contracts.py").read_text(encoding="utf-8")
    smoke_test = _smoke_runtime_text()
    smoke_script = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")

    assert "principal_id: str" in rewrite_route
    assert "principal_id: str" in plans_route
    assert "principal_id TEXT NOT NULL" in artifact_repo
    assert "WHERE a.session_id = es.session_id::text" in artifact_repo
    assert 'loaded.principal_id == "exec-1"' in postgres_test
    assert 'scoped_artifact[0].principal_id == "exec-1"' in rewrite_scope_test
    assert 'payload["principal_id"] == "exec-1"' in rewrite_api_scope_test
    assert 'body["artifacts"][0]["principal_id"] == "exec-1"' in smoke_test
    assert 'fetched_artifact.json()["principal_id"] == "exec-1"' in smoke_test
    assert "first.get('principal_id','')" in smoke_script
    assert 'match="principal_id_required"' in (ROOT / "tests/test_tool_execution.py").read_text(encoding="utf-8")


def test_postgres_ledger_runtime_compatibility_is_guarded_across_runtime_and_smoke() -> None:
    ledger_repo = (ROOT / "ea/app/repositories/ledger_postgres.py").read_text(encoding="utf-8")
    smoke_postgres = (ROOT / "scripts/smoke_postgres.sh").read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS name TEXT" in ledger_repo
    assert "ALTER COLUMN event_id TYPE TEXT USING event_id::text" in ledger_repo
    assert "ALTER COLUMN event_type SET DEFAULT 'event'" in ledger_repo
    assert "ADD COLUMN IF NOT EXISTS step_kind TEXT" in ledger_repo
    assert "ADD COLUMN IF NOT EXISTS state TEXT" in ledger_repo
    assert "execution_events missing runtime columns" in smoke_postgres
    assert "execution_events.event_id type mismatch" in smoke_postgres
    assert "execution_steps missing runtime columns" in smoke_postgres


def test_postgres_approval_runtime_compatibility_is_guarded() -> None:
    approvals_repo = (ROOT / "ea/app/repositories/approvals_postgres.py").read_text(encoding="utf-8")

    assert "SELECT approval_request_id" in approvals_repo
    assert "approval_request_id" in approvals_repo
    assert "decision_payload_json" in approvals_repo
    assert "request_status = %s" in approvals_repo


def test_artifact_principal_ownership_docs_and_milestone_cover_explicit_scope() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "explicit `principal_id` ownership" in readme
    assert "explicit `principal_id` ownership" in runbook
    assert "principal_id ownership" in http_examples
    assert "explicit `principal_id` ownership" in changelog
    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "artifact_principal_ownership_projection")
    assert capability["status"] == "released"


def test_step_parent_projection_contracts_are_wired_into_focused_contract_bundle() -> None:
    script = (ROOT / "scripts/test_postgres_contracts.sh").read_text(encoding="utf-8")
    parent_test = (ROOT / "tests/test_step_parent_projection_contracts.py").read_text(encoding="utf-8")
    smoke_test = _smoke_runtime_text()
    smoke_script = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")

    assert "tests/test_step_parent_projection_contracts.py" in script
    assert 'save_step.parent_step_id is None' in parent_test
    assert 'policy_step.parent_step_id == input_step.step_id' in parent_test
    assert 'sidecar_step.parent_step_id == input_step.step_id' in parent_test
    assert 'steps_by_key["step_policy_evaluate"]["parent_step_id"] == steps_by_key["step_input_prepare"]["step_id"]' in smoke_test
    assert 'steps_by_key["step_artifact_save"]["parent_step_id"] == steps_by_key["step_policy_evaluate"]["step_id"]' in smoke_test
    assert "policy_step.get('parent_step_id') == input_id" in smoke_script
    assert "save_step.get('parent_step_id') == policy_id" in smoke_script


def test_single_dependency_parent_projection_docs_and_milestone_are_present() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "multi-prerequisite join steps stay parentless" in readme
    assert "multi-prerequisite join steps stay parentless" in runbook
    assert "parent_step_id` only from actual single-dependency edges" in changelog
    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "single_dependency_parent_projection")
    assert capability["status"] == "released"


def test_policy_docs_and_milestone_cover_external_action_evaluation() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    policy_tests = (ROOT / "tests/test_policy.py").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "/v1/policy/evaluate" in readme
    assert "step_kind" in readme
    assert "/v1/policy/evaluate" in runbook
    assert "step/authority/review metadata" in runbook
    assert "/v1/policy/evaluate" in http_examples
    assert '"step_kind": "connector_call"' in http_examples
    assert "connector_call|execute|manager" in smoke_api
    assert "test_policy_requires_approval_for_connector_dispatch_step_even_without_explicit_send_action" in policy_tests

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "external_action_policy_api_exposure")
    assert capability["status"] == "released"
    assert "policy_step_action_metadata_projection" in capability["scope"]


def test_artifact_lookup_docs_and_milestone_cover_direct_fetch() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "/v1/rewrite/artifacts/{artifact_id}" in readme
    assert "/v1/rewrite/artifacts/{artifact_id}" in runbook
    assert "/v1/rewrite/artifacts/{{artifact_id}}" in http_examples
    assert "/v1/rewrite/artifacts/${ARTIFACT_ID}" in smoke_api

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "artifact_lookup_api_exposure")
    assert capability["status"] == "released"


def test_receipt_and_run_cost_lookup_docs_and_milestone_cover_direct_fetch() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "/v1/rewrite/receipts/{receipt_id}" in readme
    assert "/v1/rewrite/run-costs/{cost_id}" in readme
    assert "/v1/rewrite/receipts/{receipt_id}" in runbook
    assert "/v1/rewrite/run-costs/{cost_id}" in runbook
    assert "/v1/rewrite/receipts/{{receipt_id}}" in http_examples
    assert "/v1/rewrite/run-costs/{{cost_id}}" in http_examples
    assert "/v1/rewrite/receipts/${RECEIPT_ID}" in smoke_api
    assert "/v1/rewrite/run-costs/${COST_ID}" in smoke_api
    assert "TASK_EXECUTE_RECEIPT_JSON" in smoke_api
    assert "TASK_EXECUTE_COST_JSON" in smoke_api
    assert 'fetched_receipt.json()["task_key"] == "stakeholder_briefing"' in smoke_runtime
    assert 'fetched_cost.json()["task_key"] == "stakeholder_briefing"' in smoke_runtime
    assert "receipt_and_run_cost_lookup_api_exposure" in changelog
    assert "README/RUNBOOK/examples" in changelog
    assert "receipt and run-cost fetch coverage" in changelog

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "receipt_and_run_cost_lookup_api_exposure"
    )
    assert capability["status"] == "released"


def test_approval_resume_docs_and_milestone_cover_inline_completion() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "resumes execution inline" in readme
    assert "resumes execution immediately" in runbook
    assert "approve and resume execution" in http_examples
    assert "approval resume path ok" in smoke_api

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "approval_resume_execution")
    assert capability["status"] == "released"


def test_execution_queue_docs_and_milestone_cover_runtime_path() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    db_bootstrap = (ROOT / "scripts/db_bootstrap.sh").read_text(encoding="utf-8")
    db_status = (ROOT / "scripts/db_status.sh").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_postgres = (ROOT / "scripts/smoke_postgres.sh").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    postgres_matrix = (ROOT / "tests/test_postgres_contract_matrix_integration.py").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert "rewrite execution now persists durable `execution_queue` rows and drains them inline for API requests before returning" in readme
    assert "Allowed and approved rewrites now pass through durable `execution_queue` rows first; the current API path drains that queue inline, while non-API runner roles can drain it as workers." in runbook
    assert "v0_23 execution queue kernel" in db_bootstrap
    assert "execution_queue" in db_status
    assert "queue_items" in smoke_api
    assert "execution_queue" in smoke_postgres
    assert "test_postgres_execution_queue_enqueue_lease_complete_and_list" in postgres_matrix
    assert 'lease_next_queue_item(lease_owner="contract-worker"' in postgres_matrix
    assert "Promoted milestone capability `execution_queue_inline_worker` to released" in changelog

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "execution_queue_inline_worker")
    assert capability["status"] == "released"
    assert "ea/schema/20260305_v0_23_execution_queue_kernel.sql" in milestone["migrations"]
    assert "release/operator guards now pin that inline-drain and worker-lease contract" in capability["notes"]


def test_runtime_mode_docs_and_smoke_cover_prod_fail_fast_storage() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    env_matrix = (ROOT / "ENVIRONMENT_MATRIX.md").read_text(encoding="utf-8")
    smoke_postgres = (ROOT / "scripts/smoke_postgres.sh").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "EA_RUNTIME_MODE=dev|test|prod" in readme
    assert "EA_RUNTIME_MODE=prod" in readme
    assert "EA_RUNTIME_MODE=prod" in runbook
    assert "EA_RUNTIME_MODE" in env_matrix
    assert 'set_env_value "EA_API_TOKEN" "smoke-prod-token"' in smoke_postgres
    assert (
        "EA_RUNTIME_MODE=prod requires (EA_SIGNING_SECRET|DATABASE_URL|a durable postgres runtime profile)"
        in smoke_postgres
    )
    assert "prod fail-fast path ok" in smoke_postgres

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "runtime_mode_fail_fast_storage")
    assert capability["status"] == "released"


def test_human_task_docs_and_milestone_cover_session_linked_packets() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    db_bootstrap = (ROOT / "scripts/db_bootstrap.sh").read_text(encoding="utf-8")
    db_status = (ROOT / "scripts/db_status.sh").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "/v1/human/tasks" in readme
    assert "human task packets" in readme
    assert "human_task_returned" in readme
    assert "resume_session_on_return=true" in readme

    assert "/v1/human/tasks" in runbook
    assert "human_task_created" in runbook
    assert "human_task_returned" in runbook
    assert "awaiting_human" in runbook
    assert "Promoted the human-task packets kernel into a released milestone capability" in changelog

    assert "/v1/human/tasks/{{human_task_id}}/return" in http_examples
    assert "role_required=communications_reviewer&overdue_only=true" in http_examples
    assert "assigned_operator_id=operator&status=claimed" in http_examples
    assert "/v1/human/tasks/backlog?role_required=communications_reviewer&overdue_only=true&limit=20" in http_examples
    assert "/v1/human/tasks/unassigned?role_required=communications_reviewer&overdue_only=true&limit=20" in http_examples
    assert "/v1/human/tasks/mine?operator_id=operator&limit=20" in http_examples
    assert "/v1/human/tasks/{{human_task_id}}/assign" in http_examples
    assert "assignment_state=assigned&limit=20" in http_examples
    assert "\"resume_session_on_return\": true" in http_examples

    assert "v0_24 human tasks kernel" in db_bootstrap
    assert "v0_25 human task resume kernel" in db_bootstrap
    assert "v0_26 human task assignment-state kernel" in db_bootstrap
    assert "human_tasks" in db_status

    assert "human tasks ok" in smoke_api
    assert "awaiting_human|True|True" in smoke_api
    assert "role/overdue human task queue filter" in smoke_api
    assert "assigned-operator human task queue filter" in smoke_api
    assert "human task backlog endpoint" in smoke_api
    assert "human task mine endpoint" in smoke_api
    assert "pre-assigned task" in smoke_api
    assert "human task unassigned endpoint" in smoke_api
    assert "assigned-only backlog endpoint" in smoke_api
    assert "/v1/human/tasks" in smoke_api
    assert "test_human_task_flow_and_session_projection" in smoke_runtime

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_packets_kernel")
    assert capability["status"] == "released"


def test_human_task_review_contract_metadata_release_baseline_is_documented_and_guarded() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    planner_test = (ROOT / "tests/test_planner.py").read_text(encoding="utf-8")
    postgres_matrix = (ROOT / "tests/test_postgres_contract_matrix_integration.py").read_text(encoding="utf-8")
    db_bootstrap = (ROOT / "scripts/db_bootstrap.sh").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "human_review_authority_required" in readme
    assert "human_review_why_human" in readme
    assert "human_review_quality_rubric_json" in readme
    assert "human_review_authority_required" in runbook
    assert "human_review_why_human" in runbook
    assert "human_review_quality_rubric_json" in runbook
    assert "send_on_behalf_review" in smoke_api
    assert "External executive communication needs human tone review." in smoke_api
    assert 'review_task["authority_required"] == "send_on_behalf_review"' in smoke_runtime
    assert "quality_rubric_json" in smoke_runtime
    assert "human_review_authority_required" in planner_test
    assert "human_review_quality_rubric_json" in planner_test
    assert 'authority_required="send_on_behalf_review"' in postgres_matrix
    assert "v0_27 human task review contract kernel" in db_bootstrap
    assert "Promoted milestone capability `human_task_review_contract_metadata` to released" in changelog

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_review_contract_metadata")
    assert capability["status"] == "released"
    assert "release/operator guards now pin that review-contract metadata" in capability["notes"]


def test_operator_profile_specialized_backlog_routing_is_documented_and_released() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    postgres_matrix = (ROOT / "tests/test_postgres_contract_matrix_integration.py").read_text(encoding="utf-8")
    db_bootstrap = (ROOT / "scripts/db_bootstrap.sh").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "/v1/human/tasks/operators" in readme
    assert "skill-tag" in readme
    assert "/v1/human/tasks/operators" in runbook
    assert "operator_id=<id>" in runbook
    assert "Promoted the operator-profile specialized backlog routing slice into a released milestone capability" in changelog
    assert "Promoted the human-task operator queue filters slice into a released milestone capability" in changelog
    assert "Promoted milestone capability `human_task_operator_backlog_endpoints` to released" in changelog
    assert "release/operator guards" in changelog
    assert "/v1/human/tasks/mine" in changelog
    assert "operator-specialist" in smoke_api
    assert "operator-specialized backlog endpoint" in smoke_api
    assert "operator-specialized backlog endpoint to exclude" in smoke_api
    assert '"/v1/human/tasks/operators"' in smoke_runtime
    assert "operator-specialist" in smoke_runtime
    assert "test_postgres_operator_profiles_upsert_get_and_list" in postgres_matrix
    assert "v0_28 operator profiles kernel" in db_bootstrap

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "operator_profile_specialized_backlog_routing"
    )
    assert capability["status"] == "released"
    resume_capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "human_task_pause_resume_session_flow"
    )
    assert resume_capability["status"] == "released"
    filter_capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "human_task_operator_queue_filters"
    )
    assert filter_capability["status"] == "released"
    assert "human_task_role_required_filter" in filter_capability["scope"]
    assert "human_task_assigned_operator_filter" in filter_capability["scope"]
    assert "human_task_overdue_only_filter" in filter_capability["scope"]
    backlog_capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "human_task_operator_backlog_endpoints"
    )
    assert backlog_capability["status"] == "released"
    assignment_capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "human_task_operator_assignment"
    )
    assert assignment_capability["status"] == "released"
    visibility_capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "human_task_assignment_state_visibility"
    )
    assert visibility_capability["status"] == "released"
    assert "human_task_assignment_state_field" in visibility_capability["scope"]
    assert "claimed_and_returned_assignment_projection" in visibility_capability["scope"]
    assert "release/operator guards now pin that assignment-state visibility contract" in visibility_capability["notes"]
    assert "ea/schema/20260305_v0_26_human_task_assignment_state.sql" in milestone["migrations"]


def test_human_task_operator_assignment_hints_are_documented_and_released() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    postgres_contracts = (ROOT / "tests/test_postgres_contract_matrix_integration.py").read_text(encoding="utf-8")
    rewrite_route = (ROOT / "ea/app/api/routes/rewrite.py").read_text(encoding="utf-8")
    human_route = (ROOT / "ea/app/api/routes/human.py").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "routing_hints_json" in readme
    assert "auto_assign_operator_id" in readme
    assert "routing_hints_json" in runbook
    assert "auto_assign_operator_id" in runbook
    assert "operator auto-assignment hint" in smoke_api
    assert "routing_hints_json" in smoke_runtime
    assert "auto_assign_operator_id" in smoke_runtime
    assert "test_postgres_human_task_operator_assignment_hints" in postgres_contracts
    assert "routing_hints_json: dict[str, object]" in rewrite_route
    assert "routing_hints_json: dict[str, object]" in human_route
    assert "human_task_operator_assignment_hints" in changelog
    assert "release/operator guards" in changelog
    assert "recommended_operator_id" in changelog

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_operator_assignment_hints")
    assert capability["status"] == "released"
    assert "suggested_operator_ids" in capability["scope"]
    assert "auto_assign_operator_id" in capability["scope"]


def test_human_task_recommended_assignment_action_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    human_route = (ROOT / "ea/app/api/routes/human.py").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "/v1/human/tasks/{human_task_id}/assign" in readme
    assert "omits `operator_id`" in readme
    assert "auto_assign_operator_id" in runbook
    assert "omits `operator_id`" in runbook
    assert "-d '{}'" in smoke_api
    assert "pending|assigned|operator-specialist" in smoke_api
    assert 'json={}' in smoke_runtime
    assert 'assigned.json()["assigned_operator_id"] == "operator-specialist"' in smoke_runtime
    assert "human_task_no_auto_assign_candidate" in human_route

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "human_task_recommended_assignment_action"
    )
    assert capability["status"] == "released"
    assert "auto_assign_operator_id_consumption" in capability["scope"]


def test_planner_human_task_auto_preselection_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    planner_test = (ROOT / "tests/test_planner.py").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "human_review_auto_assign_if_unique" in readme
    assert "human_review_auto_assign_if_unique" in runbook
    assert "human_review_auto_assign_if_unique" in smoke_api
    assert "assigned|operator-specialist" in smoke_api
    assert "human_review_auto_assign_if_unique" in smoke_runtime
    assert 'review_task["assignment_state"] == "assigned"' in smoke_runtime
    assert 'review_task["assigned_operator_id"] == "operator-specialist"' in smoke_runtime
    assert "human_review_auto_assign_if_unique" in planner_test
    assert "auto_assign_if_unique is True" in planner_test

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "planner_human_task_auto_preselection")
    assert capability["status"] == "released"
    assert "plan_step_auto_assign_projection" in capability["scope"]
    assert "runtime_human_task_auto_assignment" in capability["scope"]


def test_human_task_assignment_source_visibility_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    postgres_matrix = (ROOT / "tests/test_postgres_contract_matrix_integration.py").read_text(encoding="utf-8")
    db_bootstrap = (ROOT / "scripts/db_bootstrap.sh").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "assignment_source" in readme
    assert "assignment_source" in runbook
    assert "assignment_source" in smoke_api
    assert "operator-specialist|recommended" in smoke_api
    assert "operator-junior|manual" in smoke_api
    assert "auto_preselected" in smoke_api
    assert 'task["assignment_source"] == ""' in smoke_runtime
    assert 'assigned.json()["assignment_source"] == "recommended"' in smoke_runtime
    assert 'review_task["assignment_source"] == "auto_preselected"' in smoke_runtime
    assert 'assignment_source="manual"' in postgres_matrix
    assert "v0_29 human task assignment-source kernel" in db_bootstrap

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "human_task_assignment_source_visibility"
    )
    assert capability["status"] == "released"
    assert "manual_recommended_auto_preselected_labels" in capability["scope"]
    assert "ea/schema/20260305_v0_29_human_task_assignment_source.sql" in milestone["migrations"]


def test_human_task_assignment_provenance_fields_are_documented_and_released() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    postgres_matrix = (ROOT / "tests/test_postgres_contract_matrix_integration.py").read_text(encoding="utf-8")
    db_bootstrap = (ROOT / "scripts/db_bootstrap.sh").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "assigned_at" in readme
    assert "assigned_by_actor_id" in readme
    assert "assigned_at" in runbook
    assert "assigned_by_actor_id" in runbook
    assert "assigned_by_actor_id" in smoke_api
    assert "orchestrator:auto_preselected" in smoke_api
    assert 'task["assigned_by_actor_id"] == ""' in smoke_runtime
    assert 'assigned.json()["assigned_by_actor_id"] == "exec-1"' in smoke_runtime
    assert 'review_task["assigned_by_actor_id"] == "orchestrator:auto_preselected"' in smoke_runtime
    assert 'assigned_by_actor_id="principal-1"' in postgres_matrix
    assert 'assigned_by_actor_id == "operator-1"' in postgres_matrix
    assert "v0_30 human task assignment provenance kernel" in db_bootstrap
    assert "human_task_assignment_provenance_fields" in changelog
    assert "release/operator guards" in changelog
    assert "assigned_at" in changelog
    assert "assigned_by_actor_id" in changelog

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "human_task_assignment_provenance_fields"
    )
    assert capability["status"] == "released"
    assert "assignment_provenance_event_payloads" in capability["scope"]
    assert "ea/schema/20260305_v0_30_human_task_assignment_provenance.sql" in milestone["migrations"]


def test_human_task_assignment_history_api_is_documented_and_released() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "/v1/human/tasks/{human_task_id}/assignment-history" in readme
    assert "/v1/human/tasks/{human_task_id}/assignment-history" in runbook
    assert "Promoted the human-task assignment-history API slice into a released milestone capability" in changelog
    assert "assignment history (includes originating task_key and deliverable_type)" in http_examples
    assert "/v1/human/tasks/${HUMAN_TASK_ID}/assignment-history" in smoke_api
    assert "human_task_created,human_task_assigned,human_task_assigned,human_task_claimed,human_task_returned" in smoke_api
    assert '/assignment-history", params={"limit": 10}' in smoke_runtime
    assert 'all(row["task_key"] == "rewrite_text" for row in history_rows)' in smoke_runtime

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_assignment_history_api")
    assert capability["status"] == "released"
    assert "ledger_backed_reassignment_audit" in capability["scope"]


def test_human_task_assignment_history_task_identity_projection_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "assignment-history` exposes task-scoped ownership transitions, now carries originating task identity too" in readme
    assert "those direct history rows now also carry originating `task_key`/`deliverable_type`" in runbook
    assert "assignment history (includes originating task_key and deliverable_type)" in http_examples
    assert "GENERIC_HUMAN_HISTORY_FIELDS" in smoke_api
    assert 'review_history.json()[0]["task_key"] == "stakeholder_briefing_review"' in smoke_runtime

    capability = next(
        entry
        for entry in milestone["capabilities"]
        if entry["name"] == "human_task_assignment_history_task_identity_projection"
    )
    assert capability["status"] == "released"


def test_session_human_task_assignment_history_projection_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "human_task_assignment_history" in readme
    assert "human_task_assignment_history" in runbook
    assert "human_task_assignment_history" in smoke_api
    assert 'body["human_task_assignment_history"] == []' in smoke_runtime
    assert 'session_body["human_task_assignment_history"]' in smoke_runtime
    assert 'body["human_task_assignment_history"][1]["assignment_source"] == "auto_preselected"' in smoke_runtime

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "session_human_task_assignment_history_projection"
    )
    assert capability["status"] == "released"


def test_session_human_task_assignment_history_task_identity_projection_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "inline human-task assignment-history rows now carry originating task identity" in readme
    assert "assignment-history rows now also carry originating `task_key`/`deliverable_type`" in runbook
    assert "human-task assignment-history rows include originating task_key and deliverable_type" in http_examples
    assert "GENERIC_HUMAN_SESSION_HISTORY_FIELDS" in smoke_api
    assert 'review_session_body["human_task_assignment_history"][0]["task_key"] == "stakeholder_briefing_review"' in smoke_runtime
    assert "Promoted milestone capability `session_human_task_assignment_history_task_identity_projection` to released" in changelog

    capability = next(
        entry
        for entry in milestone["capabilities"]
        if entry["name"] == "session_human_task_assignment_history_task_identity_projection"
    )
    assert capability["status"] == "released"
    assert "release/operator guards pin that embedded session assignment-history identity contract" in capability["notes"]


def test_session_human_task_packet_task_identity_projection_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "inline human-task packet rows now carry originating task identity" in readme
    assert "inline `human_tasks` rows now also carry originating `task_key`/`deliverable_type`" in runbook
    assert "Promoted milestone capability `session_human_task_packet_task_identity_projection` to released" in changelog
    assert "human-task packet, and human-task assignment-history rows include originating task_key and deliverable_type" in http_examples
    assert "GENERIC_HUMAN_SESSION_TASK_FIELDS" in smoke_api
    assert 'review_session_body["human_tasks"][0]["task_key"] == "stakeholder_briefing_review"' in smoke_runtime

    capability = next(
        entry
        for entry in milestone["capabilities"]
        if entry["name"] == "session_human_task_packet_task_identity_projection"
    )
    assert capability["status"] == "released"
    assert "generic_session_human_task_identity" in capability["scope"]


def test_human_task_assignment_history_filters_are_documented_and_smoked() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "assigned_operator_id" in readme
    assert "assigned_by_actor_id" in readme
    assert "assigned_operator_id" in runbook
    assert "assigned_by_actor_id" in runbook
    assert "event_name=human_task_assigned&assigned_by_actor_id=exec-1" in smoke_api
    assert "event_name=human_task_returned&assigned_operator_id=operator-junior" in smoke_api
    assert 'params={"limit": 10, "event_name": "human_task_assigned", "assigned_by_actor_id": "exec-1"}' in smoke_runtime
    assert 'params={"limit": 10, "event_name": "human_task_returned", "assigned_operator_id": "operator-junior"}' in smoke_runtime
    assert "/v1/human/tasks/{{human_task_id}}/assignment-history?limit=20&event_name=human_task_assigned&assigned_by_actor_id={{principal_id}}" in http_examples
    assert "Promoted the human-task assignment-history filters slice into a released milestone capability" in changelog

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_assignment_history_filters")
    assert capability["status"] == "released"
    assert "assigned_by_actor_history_filter" in capability["scope"]


def test_human_task_last_transition_summary_projection_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    human_route = (ROOT / "ea/app/api/routes/human.py").read_text(encoding="utf-8")
    rewrite_route = (ROOT / "ea/app/api/routes/rewrite.py").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "last_transition_event_name" in readme
    assert "last_transition_operator_id" in readme
    assert "last_transition_by_actor_id" in readme
    assert "last_transition_event_name" in runbook
    assert "last_transition_operator_id" in runbook
    assert "last_transition_by_actor_id" in runbook
    assert "HUMAN_CREATE_SUMMARY_FIELDS" in smoke_api
    assert "HUMAN_REWRITE_SUMMARY_FIELDS" in smoke_api
    assert "human_task_returned|True|returned|operator-junior|manual|operator-junior" in smoke_api
    assert 'task["last_transition_event_name"] == "human_task_created"' in smoke_runtime
    assert 'assigned.json()["last_transition_event_name"] == "human_task_assigned"' in smoke_runtime
    assert 'returned.json()["last_transition_event_name"] == "human_task_returned"' in smoke_runtime
    assert 'review_task["last_transition_event_name"] == "human_task_assigned"' in smoke_runtime
    assert 'last_transition_event_name: str' in human_route
    assert 'last_transition_event_name: str' in rewrite_route
    assert "Promoted the human-task last-transition summary projection slice into a released milestone capability" in changelog

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "human_task_last_transition_summary_projection"
    )
    assert capability["status"] == "released"
    assert "session_and_queue_row_summary" in capability["scope"]


def test_human_task_last_transition_sorting_is_documented_and_released() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    human_route = (ROOT / "ea/app/api/routes/human.py").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "sort=last_transition_desc" in readme
    assert "sort=created_asc|created_desc|last_transition_desc|priority_desc_created_asc|sla_due_at_asc|sla_due_at_asc_last_transition_desc" in runbook
    assert "human task last-transition sort ok" in smoke_api
    assert "SORT_LIST_JSON" in smoke_api
    assert "SORT_BACKLOG_JSON" in smoke_api
    assert 'params={"status": "pending", "sort": "last_transition_desc", "limit": 10}' in smoke_runtime
    assert 'params={"sort": "last_transition_desc", "limit": 10}' in smoke_runtime
    assert "/v1/human/tasks/backlog?sort=last_transition_desc&limit=20" in http_examples
    assert 'sla_due_at_asc_last_transition_desc' in human_route
    assert "human_task_last_transition_sorting" in changelog
    assert "release/operator guards" in changelog
    assert "freshest-transition queue ordering" in changelog

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_last_transition_sorting")
    assert capability["status"] == "released"
    assert "last_transition_desc_runtime_ordering" in capability["scope"]


def test_human_task_sla_sorting_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    human_route = (ROOT / "ea/app/api/routes/human.py").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "sort=sla_due_at_asc" in readme
    assert "sort=created_asc|created_desc|last_transition_desc|priority_desc_created_asc|sla_due_at_asc|sla_due_at_asc_last_transition_desc" in runbook
    assert "human task SLA sort ok" in smoke_api
    assert "SLA_LIST_JSON" in smoke_api
    assert "SLA_BACKLOG_JSON" in smoke_api
    assert 'params={"status": "pending", "sort": "sla_due_at_asc", "limit": 10}' in smoke_runtime
    assert 'params={"sort": "sla_due_at_asc", "limit": 10}' in smoke_runtime
    assert "/v1/human/tasks/backlog?sort=sla_due_at_asc&limit=20" in http_examples
    assert 'sla_due_at_asc_last_transition_desc' in human_route

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_sla_sorting")
    assert capability["status"] == "released"
    assert "sla_due_at_asc_runtime_ordering" in capability["scope"]


def test_human_task_combined_sla_transition_sorting_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    human_route = (ROOT / "ea/app/api/routes/human.py").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "sort=sla_due_at_asc_last_transition_desc" in readme
    assert "sort=created_asc|created_desc|last_transition_desc|priority_desc_created_asc|sla_due_at_asc|sla_due_at_asc_last_transition_desc" in runbook
    assert "human task combined sort ok" in smoke_api
    assert "COMBINED_LIST_JSON" in smoke_api
    assert "COMBINED_BACKLOG_JSON" in smoke_api
    assert 'params={"status": "pending", "sort": "sla_due_at_asc_last_transition_desc", "limit": 10}' in smoke_runtime
    assert 'params={"sort": "sla_due_at_asc_last_transition_desc", "limit": 10}' in smoke_runtime
    assert "/v1/human/tasks/backlog?sort=sla_due_at_asc_last_transition_desc&limit=20" in http_examples
    assert 'sla_due_at_asc_last_transition_desc' in human_route
    assert "Promoted milestone capability `human_task_sla_transition_combined_sorting` to released" in changelog
    assert "release/operator guards" in changelog
    assert "tie-break ordering contract" in changelog

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "human_task_sla_transition_combined_sorting"
    )
    assert capability["status"] == "released"
    assert "sla_due_at_asc_last_transition_desc_runtime_ordering" in capability["scope"]


def test_human_task_unscheduled_fallback_sorting_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "fall back to oldest-created ordering for tasks without `sla_due_at`" in readme
    assert "fall back to oldest-created ordering for tasks without `sla_due_at`" in runbook
    assert "Promoted the human-task unscheduled SLA fallback sorting slice into a released milestone capability" in changelog
    assert "human task unscheduled fallback sort ok" in smoke_api
    assert "UNSCHED_SLA_LIST_JSON" in smoke_api
    assert "UNSCHED_COMBINED_BACKLOG_JSON" in smoke_api
    assert 'params={"status": "pending", "sort": "sla_due_at_asc", "limit": 10}' in smoke_runtime
    assert 'params={"status": "pending", "sort": "sla_due_at_asc_last_transition_desc", "limit": 10}' in smoke_runtime
    assert "/v1/human/tasks?principal_id={{principal_id}}&status=pending&sort=sla_due_at_asc&limit=20" in http_examples

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "human_task_unscheduled_fallback_sorting"
    )
    assert capability["status"] == "released"
    assert "unscheduled_backlog_stability" in capability["scope"]


def test_human_task_created_asc_sorting_is_documented_and_released() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    human_route = (ROOT / "ea/app/api/routes/human.py").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "sort=created_asc" in readme
    assert "sort=created_asc|created_desc|last_transition_desc|priority_desc_created_asc|sla_due_at_asc|sla_due_at_asc_last_transition_desc" in runbook
    assert "human task created-asc sort ok" in smoke_api
    assert "CREATED_ASC_LIST_JSON" in smoke_api
    assert "CREATED_ASC_MINE_JSON" in smoke_api
    assert 'params={"status": "pending", "sort": "created_asc", "limit": 10}' in smoke_runtime
    assert 'params={"sort": "created_asc", "limit": 10}' in smoke_runtime
    assert 'params={"operator_id": "operator-sorter", "status": "pending", "sort": "created_asc", "limit": 10}' in smoke_runtime
    assert "/v1/human/tasks/backlog?sort=created_asc&limit=20" in http_examples
    assert "created_asc" in human_route
    assert "human_task_created_asc_sorting" in changelog
    assert "release/operator guards" in changelog
    assert "oldest-created FIFO queue ordering" in changelog

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_created_asc_sorting")
    assert capability["status"] == "released"
    assert "human_task_operator_fifo_queue_ordering" in capability["scope"]


def test_human_task_priority_created_sorting_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    human_route = (ROOT / "ea/app/api/routes/human.py").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "sort=priority_desc_created_asc" in readme
    assert "sort=created_asc|created_desc|last_transition_desc|priority_desc_created_asc|sla_due_at_asc|sla_due_at_asc_last_transition_desc" in runbook
    assert "human task priority-desc-created-asc sort ok" in smoke_api
    assert "PRIORITY_SORT_LIST_JSON" in smoke_api
    assert "PRIORITY_SORT_MINE_JSON" in smoke_api
    assert 'params={"status": "pending", "sort": "priority_desc_created_asc", "limit": 10}' in smoke_runtime
    assert 'params={"sort": "priority_desc_created_asc", "limit": 10}' in smoke_runtime
    assert 'params={"operator_id": "operator-sorter", "status": "pending", "sort": "priority_desc_created_asc", "limit": 10}' in smoke_runtime
    assert "/v1/human/tasks/backlog?sort=priority_desc_created_asc&limit=20" in http_examples
    assert "priority_desc_created_asc" in human_route

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "human_task_priority_created_sorting"
    )
    assert capability["status"] == "released"
    assert "priority_band_fifo_queue_ordering" in capability["scope"]


def test_human_task_priority_filters_are_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "accept `priority=<level>` filters" in readme
    assert "supports `priority`" in runbook
    assert "priority=urgent|high|normal|low" in runbook
    assert "human task priority filter ok" in smoke_api
    assert "PRIORITY_FILTER_LIST_JSON" in smoke_api
    assert "PRIORITY_FILTER_MINE_JSON" in smoke_api
    assert 'params={"status": "pending", "priority": "high", "sort": "created_asc", "limit": 10}' in smoke_runtime
    assert 'params={"priority": "high", "sort": "created_asc", "limit": 10}' in smoke_runtime
    assert 'params={"operator_id": "operator-sorter", "status": "pending", "priority": "urgent", "sort": "created_asc", "limit": 10}' in smoke_runtime
    assert "/v1/human/tasks/backlog?priority=high&sort=created_asc&limit=20" in http_examples

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_priority_filters")
    assert capability["status"] == "released"
    assert "human_task_operator_priority_band_views" in capability["scope"]


def test_human_task_multi_priority_filters_are_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "comma-separated values like `priority=urgent,high`" in readme
    assert "priority=urgent,high" in runbook
    assert "human task multi-priority filter ok" in smoke_api
    assert "MULTI_PRIORITY_LIST_JSON" in smoke_api
    assert "MULTI_PRIORITY_MINE_JSON" in smoke_api
    assert 'params={"status": "pending", "priority": "urgent,high", "sort": "priority_desc_created_asc", "limit": 10}' in smoke_runtime
    assert 'params={"priority": "urgent,high", "sort": "priority_desc_created_asc", "limit": 10}' in smoke_runtime
    assert 'params={"operator_id": "operator-sorter", "status": "pending", "priority": "urgent,high", "sort": "priority_desc_created_asc", "limit": 10}' in smoke_runtime
    assert "/v1/human/tasks/backlog?priority=urgent,high&sort=priority_desc_created_asc&limit=20" in http_examples

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_multi_priority_filters")
    assert capability["status"] == "released"
    assert "combined_priority_band_queue_views" in capability["scope"]


def test_human_task_priority_summary_is_documented_and_released() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    human_route = (ROOT / "ea/app/api/routes/human.py").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "GET /v1/human/tasks/priority-summary" in readme
    assert "/v1/human/tasks/priority-summary" in runbook
    assert "human task priority summary ok" in smoke_api
    assert "PRIORITY_SUMMARY_JSON" in smoke_api
    assert "PRIORITY_SUMMARY_UNASSIGNED_JSON" in smoke_api
    assert 'params={"status": "pending", "role_required": role_required}' in smoke_runtime
    assert 'params={"status": "pending", "role_required": role_required, "assignment_state": "unassigned"}' in smoke_runtime
    assert "/v1/human/tasks/priority-summary?status=pending&role_required=communications_reviewer" in http_examples
    assert '@router.get("/priority-summary")' in human_route
    assert "human_task_priority_summary" in changelog
    assert "release/operator guards" in changelog
    assert "highest_priority" in changelog

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_priority_summary")
    assert capability["status"] == "released"
    assert "priority_band_count_projection" in capability["scope"]


def test_human_task_assigned_priority_summary_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "also accepts `assigned_operator_id`" in readme
    assert "assigned_operator_id" in runbook
    assert "PRIORITY_SUMMARY_ASSIGNED_JSON" in smoke_api
    assert "PRIORITY_SUMMARY_ASSIGNED_FIELDS" in smoke_api
    assert 'params={"status": "pending", "role_required": role_required, "assigned_operator_id": operator_id}' in smoke_runtime
    assert "/v1/human/tasks/priority-summary?status=pending&role_required=communications_reviewer&assigned_operator_id=operator" in http_examples

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_assigned_priority_summary")
    assert capability["status"] == "released"
    assert "mine_queue_priority_band_projection" in capability["scope"]


def test_human_task_operator_matched_priority_summary_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    human_route = (ROOT / "ea/app/api/routes/human.py").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "also accepts `operator_id`" in readme
    assert "operator_id" in runbook
    assert "PRIORITY_SUMMARY_MATCHED_JSON" in smoke_api
    assert "PRIORITY_SUMMARY_MATCHED_FIELDS" in smoke_api
    assert 'params={' in smoke_runtime
    assert 'operator-specialist-summary-' in smoke_runtime
    assert "/v1/human/tasks/priority-summary?status=pending&assignment_state=unassigned&operator_id=operator-specialist" in http_examples
    assert "operator_id: str" in human_route

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "human_task_operator_matched_priority_summary"
    )
    assert capability["status"] == "released"
    assert "role_skill_trust_filtered_backlog_counts" in capability["scope"]


def test_human_task_assignment_source_priority_summary_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    human_route = (ROOT / "ea/app/api/routes/human.py").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "also accepts `assignment_source`" in readme
    assert "assignment_source" in runbook
    assert "PRIORITY_SUMMARY_MANUAL_JSON" in smoke_api
    assert "HUMAN_REWRITE_AUTO_SUMMARY_JSON" in smoke_api
    assert '"assignment_source": "auto_preselected"' in smoke_runtime
    assert "/v1/human/tasks/priority-summary?status=pending&assignment_source=manual" in http_examples
    assert "assignment_source: str" in human_route

    capability = next(
        entry for entry in milestone["capabilities"]
        if entry["name"] == "human_task_priority_summary_assignment_source_filter"
    )
    assert capability["status"] == "released"
    assert "manual_vs_auto_preselected_pending_projection" in capability["scope"]


def test_human_task_priority_summary_mixed_source_non_ownerless_isolation_is_documented_and_released() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "rechecked after extra ownerless rows are added" in readme
    assert "rechecked after extra ownerless rows are added" in runbook
    assert "PRIORITY_SUMMARY_MANUAL_MIXED_FIELDS" in smoke_api
    assert "HUMAN_REWRITE_AUTO_SUMMARY_MIXED_FIELDS" in smoke_api
    assert "human_task_priority_summary_mixed_source_non_ownerless_isolation" in changelog
    assert "release/operator guards" in changelog
    assert "mixed-source churn does not contaminate non-ownerless summary counts" in changelog

    capability = next(
        entry
        for entry in milestone["capabilities"]
        if entry["name"] == "human_task_priority_summary_mixed_source_non_ownerless_isolation"
    )
    assert capability["status"] == "released"
    assert "manual_summary_after_ownerless_churn" in capability["scope"]
    assert "auto_preselected_summary_after_ownerless_churn" in capability["scope"]


def test_human_task_assignment_source_queue_filters_are_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "queue views now also accept `assignment_source=<source>`" in readme
    assert "assignment_source=manual|recommended|auto_preselected" in runbook
    assert "PRIORITY_SUMMARY_MANUAL_LIST_JSON" in smoke_api
    assert "HUMAN_REWRITE_AUTO_BACKLOG_JSON" in smoke_api
    assert 'params={"status": "pending", "assignment_source": "manual"}' in smoke_runtime
    assert 'params={"operator_id": "operator-auto-summary", "assignment_source": "auto_preselected"}' in smoke_runtime
    assert "/v1/human/tasks/backlog?assignment_source=auto_preselected&limit=20" in http_examples

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "human_task_assignment_source_queue_filters"
    )
    assert capability["status"] == "released"
    assert "human_task_backlog_assignment_source_filter" in capability["scope"]


def test_human_task_ownerless_assignment_source_alias_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    postgres_matrix = (ROOT / "tests/test_postgres_contract_matrix_integration.py").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "assignment_source=none" in readme
    assert "assignment_source=none" in runbook
    assert "HUMAN_UNASSIGNED_NONE_JSON" in smoke_api
    assert "PRIORITY_SUMMARY_NONE_JSON" in smoke_api
    assert 'params={"status": "pending", "assignment_state": "unassigned", "assignment_source": "none"}' in smoke_runtime
    assert 'params={"assignment_source": "none"}' in smoke_runtime
    assert 'assignment_source="none"' in postgres_matrix
    assert "/v1/human/tasks/unassigned?assignment_source=none&limit=20" in http_examples
    assert "human_task_ownerless_assignment_source_alias" in changelog
    assert "release/operator guards" in changelog
    assert "ownerless queue and priority-summary alias" in changelog

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "human_task_ownerless_assignment_source_alias"
    )
    assert capability["status"] == "released"
    assert "human_task_unassigned_ownerless_source_alias" in capability["scope"]


def test_human_task_ownerless_session_history_alias_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "human_task_assignment_source=none" in readme
    assert "human_task_assignment_source=none" in runbook
    assert "SESSION_HUMAN_NONE_JSON" in smoke_api
    assert "HUMAN_HISTORY_NONE_JSON" in smoke_api
    assert 'params={"limit": 10, "assignment_source": "none"}' in smoke_runtime
    assert 'params={"human_task_assignment_source": "none"}' in smoke_runtime
    assert "/v1/rewrite/sessions/{{session_id}}?human_task_assignment_source=none" in http_examples
    assert "/v1/human/tasks/{{human_task_id}}/assignment-history?limit=20&assignment_source=none" in http_examples

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "human_task_ownerless_session_history_alias"
    )
    assert capability["status"] == "released"
    assert "session_human_task_ownerless_source_alias" in capability["scope"]


def test_human_task_ownerless_backlog_alias_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "assignment_state=unassigned&assignment_source=none" in readme
    assert "assignment_state=unassigned&assignment_source=none" in runbook
    assert "HUMAN_OWNERLESS_BACKLOG_JSON" in smoke_api
    assert 'params={"assignment_state": "unassigned", "assignment_source": "none"}' in smoke_runtime
    assert "/v1/human/tasks/backlog?assignment_state=unassigned&assignment_source=none&limit=20" in http_examples
    assert "Promoted milestone capability `human_task_ownerless_backlog_alias` to released" in changelog
    assert "release/operator guards" in changelog
    assert "backlog and unassigned queue slices aligned" in changelog

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "human_task_ownerless_backlog_alias"
    )
    assert capability["status"] == "released"
    assert "human_task_backlog_ownerless_source_alias" in capability["scope"]


def test_human_task_ownerless_backlog_created_sort_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "assignment_state=unassigned&assignment_source=none&sort=created_asc" in readme
    assert "assignment_state=unassigned&assignment_source=none&sort=created_asc" in runbook
    assert "HUMAN_OWNERLESS_BACKLOG_CREATED_JSON" in smoke_api
    assert 'params={\n            "assignment_state": "unassigned",\n            "assignment_source": "none",\n            "sort": "created_asc",' in smoke_runtime
    assert "/v1/human/tasks/backlog?assignment_state=unassigned&assignment_source=none&sort=created_asc&limit=20" in http_examples

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "human_task_ownerless_backlog_created_sort"
    )
    assert capability["status"] == "released"
    assert "ownerless_backlog_created_asc_fifo" in capability["scope"]


def test_human_task_ownerless_backlog_last_transition_sort_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "assignment_state=unassigned&assignment_source=none&sort=last_transition_desc" in readme
    assert "assignment_state=unassigned&assignment_source=none&sort=last_transition_desc" in runbook
    assert "HUMAN_OWNERLESS_BACKLOG_TRANSITION_JSON" in smoke_api
    assert 'params={\n            "assignment_state": "unassigned",\n            "assignment_source": "none",\n            "sort": "last_transition_desc",' in smoke_runtime
    assert "/v1/human/tasks/backlog?assignment_state=unassigned&assignment_source=none&sort=last_transition_desc&limit=20" in http_examples

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "human_task_ownerless_backlog_last_transition_sort"
    )
    assert capability["status"] == "released"
    assert "ownerless_backlog_last_transition_desc_ordering" in capability["scope"]


def test_human_task_ownerless_unassigned_last_transition_sort_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "assignment_source=none&sort=last_transition_desc" in readme
    assert "assignment_source=none&sort=last_transition_desc" in runbook
    assert "HUMAN_OWNERLESS_UNASSIGNED_TRANSITION_JSON" in smoke_api
    assert 'params={"assignment_source": "none", "sort": "last_transition_desc"}' in smoke_runtime
    assert "/v1/human/tasks/unassigned?assignment_source=none&sort=last_transition_desc&limit=20" in http_examples

    capability = next(
        entry
        for entry in milestone["capabilities"]
        if entry["name"] == "human_task_ownerless_unassigned_last_transition_sort"
    )
    assert capability["status"] == "released"
    assert "ownerless_unassigned_last_transition_desc_ordering" in capability["scope"]


def test_human_task_ownerless_unassigned_created_sort_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert "assignment_source=none&sort=created_asc" in readme
    assert "assignment_source=none&sort=created_asc" in runbook
    assert "HUMAN_OWNERLESS_UNASSIGNED_CREATED_JSON" in smoke_api
    assert 'params={"assignment_source": "none", "sort": "created_asc"}' in smoke_runtime
    assert "/v1/human/tasks/unassigned?assignment_source=none&sort=created_asc&limit=20" in http_examples
    assert "Promoted milestone capability `human_task_ownerless_unassigned_created_sort` to released" in changelog

    capability = next(
        entry
        for entry in milestone["capabilities"]
        if entry["name"] == "human_task_ownerless_unassigned_created_sort"
    )
    assert capability["status"] == "released"
    assert "ownerless_unassigned_created_asc_fifo" in capability["scope"]


def test_human_task_ownerless_list_created_sort_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "status=pending&assignment_state=unassigned&assignment_source=none&sort=created_asc" in readme
    assert "status=pending&assignment_state=unassigned&assignment_source=none&sort=created_asc" in runbook
    assert "HUMAN_OWNERLESS_LIST_CREATED_JSON" in smoke_api
    assert 'params={\n            "status": "pending",\n            "assignment_state": "unassigned",\n            "assignment_source": "none",\n            "sort": "created_asc",' in smoke_runtime
    assert "/v1/human/tasks?status=pending&assignment_state=unassigned&assignment_source=none&sort=created_asc&limit=20" in http_examples

    capability = next(
        entry
        for entry in milestone["capabilities"]
        if entry["name"] == "human_task_ownerless_list_created_sort"
    )
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert "Promoted milestone capability `human_task_ownerless_list_created_sort` to released" in changelog
    assert capability["status"] == "released"
    assert "ownerless_list_created_asc_fifo" in capability["scope"]


def test_human_task_ownerless_list_last_transition_sort_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "status=pending&assignment_state=unassigned&assignment_source=none&sort=last_transition_desc" in readme
    assert "status=pending&assignment_state=unassigned&assignment_source=none&sort=last_transition_desc" in runbook
    assert "Promoted milestone capability `human_task_ownerless_list_last_transition_sort` to released" in changelog
    assert "HUMAN_OWNERLESS_LIST_TRANSITION_JSON" in smoke_api
    assert 'params={\n            "status": "pending",\n            "assignment_state": "unassigned",\n            "assignment_source": "none",\n            "sort": "last_transition_desc",' in smoke_runtime
    assert "/v1/human/tasks?status=pending&assignment_state=unassigned&assignment_source=none&sort=last_transition_desc&limit=20" in http_examples

    capability = next(
        entry
        for entry in milestone["capabilities"]
        if entry["name"] == "human_task_ownerless_list_last_transition_sort"
    )
    assert capability["status"] == "released"
    assert "ownerless_list_last_transition_desc_ordering" in capability["scope"]


def test_human_task_session_ownerless_created_sort_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "session_id=<id>&assignment_source=none&sort=created_asc" in readme
    assert "session_id=<id>&assignment_source=none&sort=created_asc" in runbook
    assert "Promoted milestone capability `human_task_session_ownerless_created_sort` to released" in changelog
    assert "SESSION_HUMAN_NONE_CREATED_JSON" in smoke_api
    assert 'params={"session_id": session_id, "assignment_source": "none", "sort": "created_asc"}' in smoke_runtime
    assert "/v1/human/tasks?session_id={{session_id}}&assignment_source=none&sort=created_asc&limit=20" in http_examples

    capability = next(
        entry
        for entry in milestone["capabilities"]
        if entry["name"] == "human_task_session_ownerless_created_sort"
    )
    assert capability["status"] == "released"
    assert "session_ownerless_created_asc_fifo" in capability["scope"]


def test_human_task_session_ownerless_last_transition_sort_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "session_id=<id>&assignment_source=none&sort=last_transition_desc" in readme
    assert "session_id=<id>&assignment_source=none&sort=last_transition_desc" in runbook
    assert "SESSION_HUMAN_NONE_TRANSITION_JSON" in smoke_api
    assert 'params={"session_id": session_id, "assignment_source": "none", "sort": "last_transition_desc"}' in smoke_runtime
    assert "/v1/human/tasks?session_id={{session_id}}&assignment_source=none&sort=last_transition_desc&limit=20" in http_examples

    capability = next(
        entry
        for entry in milestone["capabilities"]
        if entry["name"] == "human_task_session_ownerless_last_transition_sort"
    )
    assert capability["status"] == "released"
    assert "session_ownerless_last_transition_desc_ordering" in capability["scope"]


def test_human_task_session_ownerless_mixed_source_isolation_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "manual and auto-preselected neighbors too" in readme
    assert "manual and auto-preselected neighbors present" in runbook
    assert "SESSION_HUMAN_NONE_CREATED_JSON" in smoke_api
    assert "SESSION_HUMAN_NONE_TRANSITION_JSON" in smoke_api
    assert "keeping mixed-source neighbors out" in smoke_api
    assert "ownerless_session_created_all_ids ==" in smoke_runtime
    assert "ownerless_session_transition_all_ids ==" in smoke_runtime

    capability = next(
        entry
        for entry in milestone["capabilities"]
        if entry["name"] == "human_task_session_ownerless_mixed_source_isolation"
    )
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert capability["status"] == "released"
    assert "session_ownerless_created_asc_excludes_non_ownerless" in capability["scope"]
    assert "Promoted milestone capability `human_task_session_ownerless_mixed_source_isolation` to released" in changelog


def test_human_task_ownerless_sorted_queue_mixed_source_isolation_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "manual and auto-preselected neighbors" in readme
    assert "manual and auto-preselected neighbors present" in runbook
    assert "HUMAN_OWNERLESS_BACKLOG_CREATED_JSON" in smoke_api
    assert "HUMAN_OWNERLESS_UNASSIGNED_CREATED_JSON" in smoke_api
    assert "HUMAN_OWNERLESS_LIST_CREATED_JSON" in smoke_api
    assert "keeping mixed-source neighbors out" in smoke_api
    assert "ownerless_backlog_created_all_ids ==" in smoke_runtime
    assert "ownerless_unassigned_created_all_ids ==" in smoke_runtime
    assert "ownerless_list_created_all_ids ==" in smoke_runtime
    assert "ownerless_backlog_transition_all_ids ==" in smoke_runtime
    assert "ownerless_unassigned_transition_all_ids ==" in smoke_runtime
    assert "ownerless_list_transition_all_ids ==" in smoke_runtime

    capability = next(
        entry
        for entry in milestone["capabilities"]
        if entry["name"] == "human_task_ownerless_sorted_queue_mixed_source_isolation"
    )
    assert capability["status"] == "released"
    assert "ownerless_backlog_sorted_excludes_non_ownerless" in capability["scope"]


def test_human_task_ownerless_priority_summary_mixed_source_counts_are_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "ownerless `priority-summary?assignment_state=unassigned&assignment_source=none` slice is now explicitly covered after mixed-source churn" in readme
    assert "ownerless `priority-summary?status=pending&assignment_state=unassigned&assignment_source=none` slice is now also covered after mixed-source churn" in runbook
    assert "PRIORITY_SUMMARY_NONE_MIXED_JSON" in smoke_api
    assert "stay ownerless-only after mixed-source churn" in smoke_api
    assert "ownerless_summary_after_churn" in smoke_runtime
    assert 'ownerless_summary_after_churn_body["total"] == 2' in smoke_runtime
    assert 'ownerless_summary_after_churn_body["counts_json"]["low"] == 2' in smoke_runtime

    capability = next(
        entry
        for entry in milestone["capabilities"]
        if entry["name"] == "human_task_ownerless_priority_summary_mixed_source_counts"
    )
    assert capability["status"] == "released"
    assert "ownerless_priority_summary_total_excludes_non_ownerless_after_churn" in capability["scope"]


def test_human_task_ownerless_unsorted_queue_mixed_source_isolation_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "unsorted ownerless `assignment_source=none` list, backlog, and unassigned slices are now also explicitly covered after mixed-source churn" in readme
    assert "unsorted ownerless `assignment_source=none` list, backlog, and unassigned slices are now also covered after mixed-source churn" in runbook
    assert "HUMAN_OWNERLESS_LIST_MIXED_JSON" in smoke_api
    assert "HUMAN_UNASSIGNED_NONE_MIXED_JSON" in smoke_api
    assert "HUMAN_OWNERLESS_BACKLOG_MIXED_JSON" in smoke_api
    assert "stay ownerless-only after mixed-source churn" in smoke_api
    assert "ownerless_list_after_churn_ids ==" in smoke_runtime
    assert "ownerless_unassigned_after_churn_ids ==" in smoke_runtime
    assert "ownerless_backlog_after_churn_ids ==" in smoke_runtime

    capability = next(
        entry
        for entry in milestone["capabilities"]
        if entry["name"] == "human_task_ownerless_unsorted_queue_mixed_source_isolation"
    )
    assert capability["status"] == "released"
    assert "ownerless_list_unsorted_excludes_non_ownerless_after_churn" in capability["scope"]


def test_human_task_session_ownerless_unsorted_mixed_source_isolation_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "unsorted session-scoped `session_id=<id>&assignment_source=none` slice is now also explicitly covered after mixed-source churn" in readme
    assert "unsorted session-scoped `session_id=<id>&assignment_source=none` slice is now also covered after mixed-source churn" in runbook
    assert "SESSION_HUMAN_NONE_MIXED_JSON" in smoke_api
    assert "stay ownerless-only after mixed-source churn" in smoke_api
    assert "ownerless_session_list_after_churn_ids ==" in smoke_runtime

    capability = next(
        entry
        for entry in milestone["capabilities"]
        if entry["name"] == "human_task_session_ownerless_unsorted_mixed_source_isolation"
    )
    assert capability["status"] == "released"
    assert "session_ownerless_unsorted_excludes_non_ownerless_after_churn" in capability["scope"]


def test_session_ownerless_projection_mixed_source_counts_are_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "mixed-source session-detail ownerless slice is now also explicitly count-checked" in readme
    assert "mixed-source session-detail ownerless projection is now also count-checked" in runbook
    assert "SESSION_HUMAN_NONE_PROJECTION_JSON" in smoke_api
    assert "longer empty-source history trail" in smoke_api
    assert 'len(ownerless_session_projection_body["human_tasks"]) == 2' in smoke_runtime
    assert 'len(ownerless_session_projection_body["human_task_assignment_history"]) > len(' in smoke_runtime

    capability = next(
        entry
        for entry in milestone["capabilities"]
        if entry["name"] == "session_ownerless_projection_mixed_source_counts"
    )
    assert capability["status"] == "released"
    assert "session_ownerless_projection_current_count_after_churn" in capability["scope"]


def test_session_ownerless_projection_created_order_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "human_task_assignment_source=none" in readme
    assert "human_task_assignment_source=none" in runbook
    assert "SESSION_HUMAN_NONE_PROJECTION_JSON" in smoke_api
    assert 'params={"human_task_assignment_source": "none"}' in smoke_runtime
    assert "ownerless_session_projection_ids == [ownerless_task_id, ownerless_newer_task_id]" in smoke_runtime
    assert "ownerless_session_history_ids == [ownerless_task_id, ownerless_newer_task_id]" in smoke_runtime
    assert "/v1/rewrite/sessions/{{session_id}}?human_task_assignment_source=none" in http_examples
    assert "Promoted milestone capability `session_ownerless_projection_created_order` to released" in changelog

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "session_ownerless_projection_created_order"
    )
    assert capability["status"] == "released"
    assert "session_ownerless_projection_human_tasks_created_asc" in capability["scope"]


def test_session_ownerless_projection_mixed_source_isolation_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "manual and auto-preselected work" in readme
    assert "manual and auto-preselected neighbors" in runbook
    assert "SESSION_HUMAN_NONE_PROJECTION_JSON" in smoke_api
    assert "two-row current ownerless slice" in smoke_api
    assert 'row["human_task_id"] not in {manual_task_id, auto_task_id}' in smoke_runtime
    assert "ownerless_session_projection_history_all_ids[:4]" in smoke_runtime

    capability = next(
        entry
        for entry in milestone["capabilities"]
        if entry["name"] == "session_ownerless_projection_mixed_source_isolation"
    )
    assert capability["status"] == "released"
    assert "session_ownerless_projection_current_rows_exclude_non_ownerless" in capability["scope"]


def test_human_task_assignment_history_source_filter_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "assignment-history` also accepts `event_name`, `assigned_operator_id`, `assigned_by_actor_id`, and `assignment_source`" in readme
    assert "assignment_source" in runbook
    assert "HUMAN_HISTORY_RECOMMENDED_JSON" in smoke_api
    assert 'params={"limit": 10, "assignment_source": "recommended"}' in smoke_runtime
    assert "/v1/human/tasks/{{human_task_id}}/assignment-history?limit=20&assignment_source=recommended" in http_examples

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "human_task_assignment_history_source_filter"
    )
    assert capability["status"] == "released"
    assert "recommended_transition_isolation" in capability["scope"]


def test_session_human_task_assignment_source_filter_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "also accepts `human_task_assignment_source`" in readme
    assert "human_task_assignment_source" in runbook
    assert "SESSION_HUMAN_MANUAL_JSON" in smoke_api
    assert "HUMAN_REWRITE_AUTO_SESSION_JSON" in smoke_api
    assert 'params={"human_task_assignment_source": "manual"}' in smoke_runtime
    assert "/v1/rewrite/sessions/{{session_id}}?human_task_assignment_source=manual" in http_examples

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "session_human_task_assignment_source_filter"
    )
    assert capability["status"] == "released"
    assert "manual_session_task_slice" in capability["scope"]


def test_session_scoped_human_task_assignment_source_queue_filters_are_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "session_id=<id>&assignment_source=<source>" in readme
    assert "session_id=<id>&assignment_source=<source>" in runbook
    assert "PRIORITY_SUMMARY_MANUAL_SESSION_JSON" in smoke_api
    assert "HUMAN_REWRITE_AUTO_LIST_JSON" in smoke_api
    assert 'params={"session_id": session_id, "assignment_source": "manual"}' in smoke_runtime
    assert "/v1/human/tasks?principal_id={{principal_id}}&session_id={{session_id}}&assignment_source=manual&limit=20" in http_examples
    assert "Promoted milestone capability `session_scoped_human_task_assignment_source_filters` to released" in changelog
    assert "release/operator guards now pin the existing README/RUNBOOK/examples plus approved smoke coverage" in changelog
    assert "session-scoped `assignment_source=<source>` queue filtering" in changelog

    capability = next(
        entry
        for entry in milestone["capabilities"]
        if entry["name"] == "session_scoped_human_task_assignment_source_filters"
    )
    assert capability["status"] == "released"
    assert "session_scoped_manual_queue_slice" in capability["scope"]


def test_postgres_contract_matrix_release_baseline_is_documented_and_guarded() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/smoke-runtime.yml").read_text(encoding="utf-8")
    script = (ROOT / "scripts/test_postgres_contracts.sh").read_text(encoding="utf-8")
    postgres_matrix = (ROOT / "tests/test_postgres_contract_matrix_integration.py").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))
    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "postgres_contract_matrix")

    assert "current matrix covers artifacts, channel runtime, approvals, policy decisions, and task contracts" in readme
    assert "Current `scripts/test_postgres_contracts.sh` coverage includes artifacts, channel runtime, approvals, policy decisions, and task contracts." in runbook
    assert "bash scripts/test_postgres_contracts.sh" in workflow
    assert "tests/test_postgres_contract_matrix_integration.py" in script
    assert "test_postgres_approvals_create_decide_and_list_history" in postgres_matrix
    assert "test_postgres_policy_decisions_append_and_filter_recent" in postgres_matrix
    assert "test_postgres_task_contracts_upsert_get_and_list" in postgres_matrix
    assert "test_postgres_evidence_object_repo_materializes_queries_and_merges_evidence_pack_rows" in postgres_matrix
    assert "Promoted milestone capability `postgres_contract_matrix` to released" in changelog
    assert capability["status"] == "released"
    assert "release/operator guards now pin that matrix" in capability["notes"]


def test_principal_scoped_memory_seed_surface_is_released_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "/v1/memory/candidates" in readme
    assert "/v1/memory/stakeholders" in readme
    assert "/v1/memory/interruption-budgets" in readme

    assert "/v1/memory/candidates" in runbook
    assert "/v1/memory/stakeholders" in runbook
    assert "/v1/memory/interruption-budgets" in runbook

    assert "Promoted milestone capability `principal_scoped_memory_seed_apis` to released" in changelog

    assert "/v1/memory/candidates" in smoke_api
    assert "/v1/memory/stakeholders" in smoke_api
    assert "/v1/memory/interruption-budgets" in smoke_api

    assert "test_memory_candidate_promotion_flow" in smoke_runtime
    assert "test_memory_stakeholders_principal_scope_flow" in smoke_runtime
    assert "test_memory_interruption_budgets_principal_scope_flow" in smoke_runtime

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "principal_scoped_memory_seed_apis")
    assert capability["status"] == "released"


def test_principal_request_context_guardrails_are_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    env_matrix = (ROOT / "ENVIRONMENT_MATRIX.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "X-EA-Principal-ID" in readme
    assert "EA_DEFAULT_PRINCIPAL_ID" in readme
    assert "principal_scope_mismatch" in readme

    assert "X-EA-Principal-ID" in runbook
    assert "EA_DEFAULT_PRINCIPAL_ID" in runbook
    assert "principal_scope_mismatch" in runbook

    assert "EA_DEFAULT_PRINCIPAL_ID" in env_matrix

    assert "X-EA-Principal-ID" in http_examples
    assert "principal_scope_mismatch" in http_examples

    assert "X-EA-Principal-ID" in smoke_api
    assert "principal_scope_mismatch" in smoke_api

    assert "test_tool_registry_and_connector_bindings_flow" in smoke_runtime
    assert "test_memory_routes_use_default_principal_when_header_and_body_are_omitted" in smoke_runtime
    assert "principal_request_context_guardrails" in changelog
    assert "release/operator guards" in changelog
    assert "X-EA-Principal-ID" in changelog

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "principal_request_context_guardrails")
    assert capability["status"] == "released"


def test_principal_scoped_rewrite_and_plan_routes_are_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "rewrite/session/artifact/receipt/run-cost, plan-compile/execute" in readme
    assert "/v1/rewrite/sessions/{session_id}" in runbook
    assert "/v1/plans/compile" in runbook
    assert "/v1/plans/execute" in runbook
    assert "403 principal_scope_mismatch" in runbook
    assert '"principal_id": "exec-2"' in http_examples
    assert "REWRITE_SESSION_MISMATCH_CODE" in smoke_api
    assert "PLAN_MISMATCH_CODE" in smoke_api
    assert "test_rewrite_routes_enforce_principal_scope" in smoke_runtime
    assert "test_plan_compile_derives_request_principal_and_rejects_mismatch" in smoke_runtime

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "principal_scoped_rewrite_and_plan_routes")
    assert capability["status"] == "released"


def test_session_principal_scoped_human_task_routes_are_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "session-bound human task create/list requests now also enforce the linked execution session principal" in readme
    assert "GET /v1/human/tasks?session_id=..." in runbook
    assert "HUMAN_CREATE_MISMATCH_CODE" in smoke_api
    assert "HUMAN_SESSION_LIST_MISMATCH_CODE" in smoke_api
    assert "test_human_task_session_routes_enforce_session_principal_scope" in smoke_runtime

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "session_principal_scoped_human_task_routes")
    assert capability["status"] == "released"


def test_generic_task_execution_runtime_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    postgres_contracts = (ROOT / "tests/test_postgres_contract_matrix_integration.py").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "/v1/plans/execute" in readme
    assert "non-`rewrite_text` artifact flows" in readme
    assert "structured `input_json` plus `context_refs`" in readme
    assert "/v1/plans/execute" in runbook
    assert "stakeholder briefings" in runbook
    assert "structured `input_json` plus `context_refs`" in runbook
    assert "Promoted milestone capability `generic_task_execution_runtime` to released" in changelog
    assert "POST {{host}}/v1/plans/execute" in http_examples
    assert '"input_json": {' in http_examples
    assert '"context_refs": [' in http_examples
    assert "TASK_EXECUTE_JSON" in smoke_api
    assert "context_refs" in smoke_api
    assert "alex-exec" in smoke_api
    assert "generic task execution ok" in smoke_api
    assert "test_generic_task_execution_uses_compiled_contract_runtime" in smoke_runtime
    assert 'session_body["steps"][0]["input_json"]["context_refs"] ==' in smoke_runtime
    assert "test_postgres_orchestrator_executes_non_rewrite_task_contract" in postgres_contracts

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "generic_task_execution_runtime")
    assert capability["status"] == "released"


def test_memory_reasoning_context_packs_are_documented_and_guarded() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    plan_execute_contracts = (ROOT / "tests/test_plan_execute_input_contracts.py").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "/v1/memory/context-pack" in readme
    assert "injects synthesized `context_pack` payloads from principal-scoped memory reasoning" in readme
    assert "/v1/memory/context-pack" in runbook
    assert "including promoted-memory signals, conflict rows, commitment-risk rows, and unresolved refs" in runbook
    assert "Promoted milestone capability `memory_reasoning_context_packs` to released" in changelog
    assert "test_memory_context_pack_route_returns_reasoned_pack" in plan_execute_contracts
    assert "test_plan_execute_accepts_structured_input_json_and_context_refs" in plan_execute_contracts

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "memory_reasoning_context_packs")
    assert capability["status"] == "released"
    assert "release/operator guards now pin that docs plus runtime contract baseline behavior" in capability["notes"]


def test_plan_graph_validation_is_documented_and_guarded() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "validates duplicate step keys, unknown dependency keys, and dependency cycles before queue execution starts" in readme
    assert "duplicate step keys, unknown dependency keys, and dependency cycles before any session rows are started" in runbook
    assert "duplicate step keys, unknown dependency keys, and dependency cycles before queue execution or session creation begins" in changelog
    assert "Promoted milestone capability `plan_graph_validation` to released" in changelog

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "plan_graph_validation")
    assert capability["status"] == "released"


def test_step_io_contracts_are_documented_and_guarded() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "only merges declared dependency inputs and validates declared step outputs before completion" in readme
    assert "only merges declared dependency inputs and fails missing declared outputs before a step can complete" in runbook
    assert "only merge declared dependency inputs and now fail fast when a completed step omits any declared output key" in changelog
    assert "Promoted milestone capability `step_io_contract_enforcement` to released" in changelog

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "step_io_contract_enforcement")
    assert capability["status"] == "released"
    assert "release/operator guards now pin those runtime IO contracts" in capability["notes"]


def test_generic_task_execution_async_contracts_are_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "same first-class `202 awaiting_approval` and `202 awaiting_human` async contract" in readme
    assert 'step_artifact_save.state=waiting_approval' in readme
    assert 'blocked_dependency_keys=["step_human_review"]' in readme
    assert "same first-class `202 awaiting_approval` and `202 awaiting_human` workflow contract" in runbook
    assert 'step_artifact_save` in `waiting_approval`' in runbook
    assert 'blocked_dependency_keys=["step_human_review"]' in runbook
    assert '"task_key": "decision_brief_approval"' in http_examples
    assert '"task_key": "stakeholder_briefing_review"' in http_examples
    assert "inspect paused approval-backed session dependency projection" in http_examples
    assert "inspect paused human-review-backed session dependency projection" in http_examples
    assert "GENERIC_APPROVAL_JSON" in smoke_api
    assert "GENERIC_APPROVAL_TASK_KEY" in smoke_api
    assert "GENERIC_HUMAN_JSON" in smoke_api
    assert "generic task async contracts ok" in smoke_api
    assert 'local attempts="${3:-120}"' in smoke_api
    assert 'timed out waiting for session ${session_id} to reach ${expected_status}' in smoke_api
    assert "test_generic_task_execution_supports_async_approval_and_human_contracts" in smoke_runtime
    assert "Promoted milestone capability `generic_task_execution_async_contracts` to released" in changelog

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "generic_task_execution_async_contracts")
    assert capability["status"] == "released"
    assert "paused generic task sessions keep the same dependency-state projection" in capability["notes"]


def test_artifact_lookup_task_identity_projection_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "originating task key and deliverable type" in readme
    assert "originating `task_key`/`deliverable_type`" in runbook
    assert "includes originating task_key and deliverable_type" in http_examples
    assert "Promoted the direct artifact task-identity lookup slice into a released milestone capability" in changelog
    assert "TASK_EXECUTE_ARTIFACT_JSON" in smoke_api
    assert "TASK_EXECUTE_ARTIFACT_FIELDS" in smoke_api
    assert 'fetched_artifact.json()["task_key"] == "stakeholder_briefing"' in smoke_runtime
    assert 'fetched_artifact.json()["deliverable_type"] == "stakeholder_briefing"' in smoke_runtime

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "artifact_lookup_task_identity_projection")
    assert capability["status"] == "released"


def test_artifact_preview_handle_projection_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "preview_text" in readme
    assert "storage_handle" in readme
    assert "mime_type" in readme
    assert "body_ref" in readme
    assert "preview_text" in runbook
    assert "storage_handle" in runbook
    assert "mime_type" in runbook
    assert "body_ref" in runbook
    assert "preview_text and storage_handle" in http_examples
    assert "Promoted milestone capability `artifact_preview_handle_projection` to released" in changelog
    assert "TASK_EXECUTE_ARTIFACT_FIELDS" in smoke_api
    assert "REWRITE_ARTIFACT_FIELDS" in smoke_api
    assert 'fetched_artifact.json()["mime_type"] == "text/plain"' in smoke_runtime
    assert 'fetched_artifact.json()["preview_text"] == "Board context and stakeholder sensitivities."' in smoke_runtime
    assert 'fetched_artifact.json()["storage_handle"] == f"artifact://{body[\'artifact_id\']}"' in smoke_runtime
    assert 'fetched_artifact.json()["body_ref"].startswith("file://")' in smoke_runtime

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "artifact_preview_handle_projection")
    assert capability["status"] == "released"


def test_proof_lookup_task_identity_projection_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "direct execution proof records" in readme
    assert "originating `task_key`/`deliverable_type`" in runbook
    assert "fetch receipt (includes originating task_key and deliverable_type)" in http_examples
    assert "fetch run cost (includes originating task_key and deliverable_type)" in http_examples
    assert "TASK_EXECUTE_RECEIPT_JSON" in smoke_api
    assert "TASK_EXECUTE_COST_JSON" in smoke_api
    assert 'fetched_receipt.json()["task_key"] == "stakeholder_briefing"' in smoke_runtime
    assert 'fetched_cost.json()["task_key"] == "stakeholder_briefing"' in smoke_runtime
    assert "Promoted milestone capability `proof_lookup_task_identity_projection` to released" in changelog

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "proof_lookup_task_identity_projection")
    assert capability["status"] == "released"
    assert "release/operator guards pin that direct proof identity contract" in capability["notes"]


def test_session_artifact_task_identity_projection_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "inline artifact/proof rows now carry originating task identity" in readme
    assert "self-describing artifact/proof task identity" in runbook
    assert "Promoted milestone capability `session_artifact_task_identity_projection` to released" in changelog
    assert "TASK_EXECUTE_SESSION_FIELDS" in smoke_api
    assert "stakeholder_briefing|stakeholder_briefing|stakeholder_briefing" in smoke_api
    assert 'session_body["artifacts"][0]["task_key"] == "stakeholder_briefing"' in smoke_runtime
    assert 'session_body["artifacts"][0]["deliverable_type"] == "stakeholder_briefing"' in smoke_runtime

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "session_artifact_task_identity_projection")
    assert capability["status"] == "released"


def test_async_queue_projection_task_identity_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "approval projections now carry the originating task identity" in readme
    assert "queue/detail payloads now also carry the originating task identity" in readme
    assert "Approval and human-task queue/detail payloads now stay self-describing" in runbook
    assert "Approvals -> pending (includes originating task_key and deliverable_type)" in http_examples
    assert "Human tasks -> direct detail (includes originating task_key and deliverable_type)" in http_examples
    assert "GENERIC_APPROVAL_PENDING_FIELDS" in smoke_api
    assert "GENERIC_APPROVAL_HISTORY_FIELDS" in smoke_api
    assert "GENERIC_HUMAN_LIST_FIELDS" in smoke_api
    assert 'pending_row["task_key"] == "decision_brief_approval"' in smoke_runtime
    assert 'review_detail.json()["task_key"] == "stakeholder_briefing_review"' in smoke_runtime

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "async_queue_projection_task_identity")
    assert "release/operator guards now pin that self-describing async queue identity contract" in capability["notes"]
    assert capability["status"] == "released"


def test_dependency_aware_execution_scheduler_release_baseline_is_documented_and_guarded() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    postgres_contracts = (ROOT / "tests/test_postgres_contract_matrix_integration.py").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "queue advancement now enqueues every currently ready step from satisfied dependency edges" in readme
    assert "queue advancement now enqueues every currently ready step from satisfied dependency edges" in runbook
    assert "Queue advancement now enqueues the full ready set from satisfied `depends_on` edges" in changelog
    assert "Promoted milestone capability `dependency_aware_execution_scheduler` to released" in changelog
    assert "test_postgres_orchestrator_dependency_scheduler_waits_for_all_dependencies" in postgres_contracts
    assert "test_postgres_queue_leasing_skips_paused_sessions_even_with_ready_items" in postgres_contracts

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "dependency_aware_execution_scheduler")
    assert capability["status"] == "released"
    assert "release/operator guards now pin that dependency-aware ready-set scheduling contract" in capability["notes"]


def test_queued_policy_step_audit_truthfulness_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "policy_decision` is now recorded by the queued `step_policy_evaluate` handler after `input_prepared`" in readme
    assert "policy_decision` is now emitted from the queued `step_policy_evaluate` handler after `input_prepared`" in runbook
    assert "Policy decisions are now recorded from the queued `step_policy_evaluate` handler after `input_prepared`" in changelog
    assert "queued_policy_step_audit_truthfulness" in changelog
    assert "release/operator guards" in changelog
    assert "policy_decision" in smoke_api
    assert "order_ok" in smoke_api
    assert 'event_names.index("input_prepared") < event_names.index("policy_decision")' in smoke_runtime

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "queued_policy_step_audit_truthfulness")
    assert capability["status"] == "released"


def test_human_task_dependency_input_merge_is_documented_and_released() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    postgres_contracts = (ROOT / "tests/test_postgres_contract_matrix_integration.py").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "compiled human-review steps now merge dependency outputs into the created packet input" in readme
    assert "queued human-review step now also merges dependency outputs into the packet input" in runbook
    assert "Human-review step execution now merges dependency outputs into the created packet input" in changelog
    assert "test_postgres_human_task_step_merges_dependency_outputs" in postgres_contracts

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "human_task_dependency_input_merge")
    assert capability["status"] == "released"


def test_typed_step_handler_gateway_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    planner_test = (ROOT / "tests/test_planner.py").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "step_input_prepare" in readme
    assert "step_policy_evaluate" in readme
    assert "step_artifact_save" in readme
    assert "step_input_prepare" in runbook
    assert "step_policy_evaluate" in runbook
    assert "step_artifact_save" in runbook
    assert "step_input_prepare" in smoke_api
    assert "step_policy_evaluate" in smoke_api
    assert "input_prepared" in smoke_api
    assert "policy_step_completed" in smoke_api
    assert "step_input_prepare" in smoke_runtime
    assert "step_policy_evaluate" in smoke_runtime
    assert "input_prepared" in smoke_runtime
    assert "policy_step_completed" in smoke_runtime
    assert "step_input_prepare" in planner_test
    assert "step_policy_evaluate" in planner_test
    assert "typed_step_handler_gateway" in changelog
    assert "release/operator guards" in changelog
    assert "step_input_prepare" in changelog

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "typed_step_handler_gateway")
    assert capability["status"] == "released"


def test_planner_dependency_graph_projection_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    planner_test = (ROOT / "tests/test_planner.py").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "queued step execution now only merges declared dependency inputs and validates declared step outputs before completion" in readme
    assert "`POST /v1/plans/compile` now exposes explicit plan-step dependencies plus declared input/output keys" in readme
    assert "`POST /v1/plans/compile` exposes `depends_on`, `input_keys`, and `output_keys`" in runbook
    assert "The queue runtime now only merges declared dependency inputs and fails missing declared outputs before a step can complete" in runbook
    assert "Promoted the dependency-aware planner graph projection into a released milestone capability" in changelog
    assert "expected direct three-step plan compile response with explicit artifact-save semantics" in smoke_api
    assert 'compiled.json()["plan"]["steps"][1]["depends_on"] == ["step_input_prepare"]' in smoke_runtime
    assert 'compiled.json()["plan"]["steps"][1]["output_keys"] == [' in smoke_runtime
    assert 'plan.steps[1].depends_on == ("step_input_prepare",)' in planner_test
    assert 'plan.steps[0].output_keys == ("normalized_text", "text_length")' in planner_test

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "planner_dependency_graph_projection"
    )
    assert capability["status"] == "released"


def test_plan_step_operational_semantics_are_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    planner_test = (ROOT / "tests/test_planner.py").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "owner`, `authority_class`, `review_class`, `failure_strategy`, `timeout_budget_seconds`, `max_attempts`, and `retry_backoff_seconds`" in readme
    assert "`owner`, `authority_class`, `review_class`, `failure_strategy`, `timeout_budget_seconds`, `max_attempts`, and `retry_backoff_seconds`" in runbook
    assert "Compiled plan steps now project explicit owner, authority_class, review_class, failure_strategy, timeout_budget_seconds, max_attempts, and retry_backoff_seconds semantics" in changelog
    assert "expected direct three-step plan compile response with explicit artifact-save semantics" in smoke_api
    assert 'compiled.json()["plan"]["steps"][0]["owner"] == "system"' in smoke_runtime
    assert 'compiled.json()["plan"]["steps"][0]["timeout_budget_seconds"] == 30' in smoke_runtime
    assert 'compiled_review.json()["plan"]["steps"][2]["review_class"] == "operator"' in smoke_runtime
    assert 'compiled_review.json()["plan"]["steps"][2]["timeout_budget_seconds"] == 3600' in smoke_runtime
    assert 'plan.steps[2].authority_class == "draft"' in planner_test
    assert 'plan.steps[2].owner == "human"' in planner_test
    assert 'plan.steps[2].timeout_budget_seconds == 3600' in planner_test

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "plan_step_operational_semantics_projection")
    assert capability["status"] == "released"


def test_planner_human_task_branch_projection_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    planner_test = (ROOT / "tests/test_planner.py").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "human_review_role" in readme
    assert "step_human_review" in readme
    assert "human_review_role" in runbook
    assert "step_human_review" in runbook
    assert "rewrite_review" in smoke_api
    assert "communications_reviewer" in smoke_api
    assert "step_human_review" in smoke_runtime
    assert "communications_review" in smoke_runtime
    assert "human_review_role" in planner_test
    assert "step_human_review" in planner_test

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "planner_human_task_branch_projection"
    )
    assert capability["status"] == "released"


def test_runtime_human_task_step_execution_is_documented_and_released() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "202 awaiting_human" in runbook
    assert "awaiting_human" in readme
    assert "Promoted the compiled human-review runtime execution slice into a released milestone capability" in changelog
    assert "compiled human review runtime ok" in smoke_api
    assert "awaiting_human|poll_or_subscribe|True|" in smoke_api
    assert "test_rewrite_compiled_human_review_branch_pauses_and_resumes" in smoke_runtime
    assert "human_task_step_started" in smoke_runtime

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "runtime_human_task_step_execution"
    )
    assert capability["status"] == "released"


def test_human_review_payload_artifact_override_is_documented_and_released() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "returned_payload_json.final_text" in readme
    assert "final_text" in runbook
    assert "edited by reviewer" in smoke_api
    assert 'body_after["artifacts"][0]["content"]' in smoke_runtime
    assert 'returned_payload_json": {"final_text": reviewed_text}' in smoke_runtime
    assert "human_review_payload_artifact_override" in changelog
    assert "release/operator guards" in changelog
    assert "reviewer-edited content" in changelog

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "human_review_payload_artifact_override"
    )
    assert capability["status"] == "released"


def test_planner_human_review_operational_metadata_is_documented_and_released() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    planner_test = (ROOT / "tests/test_planner.py").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "human_review_priority" in readme
    assert "human_review_sla_minutes" in readme
    assert "human_review_desired_output_json" in readme
    assert "human_review_priority" in runbook
    assert "human_review_sla_minutes" in runbook
    assert "human_review_desired_output_json" in runbook
    assert "Promoted the planner human-review operational metadata slice into a released milestone capability" in changelog
    assert "manager_review" in smoke_api
    assert "high|45|3600|1|0|True|manager_review" in smoke_api
    assert 'review_task["priority"] == "high"' in smoke_runtime
    assert 'review_task["desired_output_json"]["escalation_policy"] == "manager_review"' in smoke_runtime
    assert "human_review_sla_minutes" in planner_test
    assert 'timeout_budget_seconds == 3600' in planner_test
    assert 'desired_output_json["escalation_policy"] == "manager_review"' in planner_test

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "planner_human_review_operational_metadata"
    )
    assert capability["status"] == "released"


def test_registry_backed_tool_execution_service_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    tool_execution_tests = (ROOT / "tests/test_tool_execution.py").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "ToolExecutionService" in readme
    assert "tool.v1" in readme
    assert "self-heals missing built-in tool definitions" in readme
    assert "ToolExecutionService" in runbook
    assert "tool.v1" in runbook
    assert "self-heals its registry definition" in runbook
    assert "Promoted the registry-backed tool execution service slice into a released milestone capability" in changelog
    assert "artifact_repository|tool.v1" in smoke_api
    assert "tool_execution_completed" in smoke_api
    assert "artifact_repository" in smoke_runtime
    assert "tool_execution_completed" in smoke_runtime
    assert "invocation_contract" in smoke_runtime
    assert "test_tool_execution_service_self_heals_missing_builtin_artifact_definition" in tool_execution_tests
    assert "test_tool_execution_service_self_heals_missing_builtin_connector_dispatch_definition" in tool_execution_tests

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "registry_backed_tool_execution_service")
    assert capability["status"] == "released"
    assert "builtin_tool_registry_self_heal" in capability["scope"]


def test_connector_dispatch_tool_execution_slice_is_documented_and_smoked() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    tool_execution_tests = (ROOT / "tests/test_tool_execution.py").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "/v1/tools/execute" in readme
    assert "connector.dispatch" in readme
    assert "/v1/tools/execute" in runbook
    assert "connector.dispatch" in runbook
    assert "/v1/tools/execute" in http_examples
    assert "connector.dispatch" in http_examples
    assert 'TOOL_EXEC_STATUS="$(python3 -c ' in smoke_api
    assert '"${TOOL_EXEC_STATUS}" != "queued" && "${TOOL_EXEC_STATUS}" != "retry"' in smoke_api
    assert "connector.dispatch|tool.v1" in smoke_api
    assert "connector.dispatch" in smoke_runtime
    assert "/v1/tools/execute" in smoke_runtime
    assert "test_tool_execution_service_executes_builtin_connector_dispatch_handler" in tool_execution_tests

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "connector_dispatch_tool_execution_slice")
    assert capability["status"] == "released"


def test_browseract_account_facts_tool_execution_slice_is_documented_and_released() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    tool_execution_tests = (ROOT / "tests/test_tool_execution.py").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "/v1/tools/execute" in readme
    assert "browseract.extract_account_facts" in readme
    assert "/v1/tools/execute" in runbook
    assert "browseract.extract_account_facts" in runbook
    assert "/v1/tools/execute" in http_examples
    assert "browseract.extract_account_facts" in http_examples
    assert "Promoted the BrowserAct account-facts tool execution slice into a released milestone capability" in changelog
    assert "browseract.extract_account_facts|BrowserAct|Tier 3|ops@example.com" in smoke_api
    assert "browseract.extract_account_facts" in smoke_runtime
    assert "browseract_ltd_discovery" in smoke_runtime
    assert "test_tool_execution_service_executes_builtin_browseract_extract_handler" in tool_execution_tests
    assert "test_tool_execution_service_self_heals_missing_builtin_browseract_definition" in tool_execution_tests

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "browseract_account_facts_tool_execution_slice"
    )
    assert capability["status"] == "released"


def test_connector_dispatch_binding_scope_guardrails_are_documented_and_released() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    tool_execution_tests = (ROOT / "tests/test_tool_execution.py").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "enabled connector binding" in readme
    assert "principal scope" in runbook
    assert "\"binding_id\"" in http_examples
    assert "principal_scope_mismatch" in smoke_api
    assert "binding_id" in smoke_api
    assert "execute_mismatch" in smoke_runtime
    assert "binding_id" in smoke_runtime
    assert 'execute_mismatch.json()["error"]["code"] == "operator_scope_required"' in smoke_runtime
    assert "test_tool_execution_service_rejects_foreign_connector_binding_scope" in tool_execution_tests
    assert "connector_dispatch_binding_scope_guardrails" in changelog
    assert "release/operator guards" in changelog
    assert "delivery side effect is queued" in changelog

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "connector_dispatch_binding_scope_guardrails")
    assert capability["status"] == "released"


def test_approval_async_acceptance_contract_is_documented_and_released() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    http_examples = (ROOT / "HTTP_EXAMPLES.http").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "202 Accepted" in readme
    assert "awaiting_approval" in readme
    assert "202 awaiting_approval" in runbook
    assert "poll_or_subscribe" in runbook
    assert "expected 202 for approval-required path" in smoke_api
    assert "awaiting_approval|poll_or_subscribe" in smoke_api
    assert "assert create.status_code == 202" in smoke_runtime
    assert "next_action" in smoke_runtime
    assert "approval-required acceptance contract" in http_examples
    assert "approval_async_acceptance_contract" in changelog
    assert "release/operator guards" in changelog
    assert "202 Accepted" in changelog

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "approval_async_acceptance_contract")
    assert capability["status"] == "released"


def test_typed_task_and_skill_policy_models_are_documented_and_released() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text(encoding="utf-8")
    runtime_policy_tests = (ROOT / "tests/test_task_contract_runtime_policy.py").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "typed runtime policy models" in readme
    assert "artifact_retry" in readme
    assert "skill_catalog" in readme
    assert "typed runtime policy models" in runbook
    assert "artifact_retry" in runbook
    assert "skill_catalog" in runbook
    assert "Promoted milestone capability `typed_task_and_skill_policy_models` to released" in changelog
    assert "typed runtime policy projection" in changelog
    assert "artifact_failure_strategy" in smoke_api
    assert "human_review_role" in smoke_api
    assert "artifact_output_template" in smoke_api
    assert "pre_artifact_tool_name" in smoke_api
    assert "test_task_contract_runtime_policy_parses_typed_metadata" in runtime_policy_tests
    assert "policy.skill_catalog.skill_key" in runtime_policy_tests
    assert "policy.artifact_retry.failure_strategy" in runtime_policy_tests

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "typed_task_and_skill_policy_models")
    assert capability["status"] == "released"
    assert "release/operator guards now pin that typed runtime-policy projection" in capability["notes"]


def test_provider_registry_capability_routing_is_documented_and_released() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    smoke_runtime = _smoke_runtime_text()
    tool_execution_tests = (ROOT / "tests/test_tool_execution.py").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "Promoted milestone capability `provider_registry_capability_routing` to released" in changelog
    assert "dynamically registered runtime tools" in changelog
    assert 'execute_unregistered.json()["error"]["code"] == "tool_not_registered:provider.not_registered"' in smoke_runtime
    assert 'email_handler_missing.json()["error"]["code"] == "tool_handler_missing:email.send"' in smoke_runtime
    assert "test_tool_execution_service_executes_registered_tool_not_in_provider_catalog" in tool_execution_tests

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "provider_registry_capability_routing"
    )
    assert capability["status"] == "released"
    assert "release/operator guards now pin that capability-addressed routing baseline" in capability["notes"]


def test_append_only_session_ledger_and_delta_sync_slice_is_documented_and_released() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    rewrite_route = (ROOT / "ea/app/api/routes/rewrite.py").read_text(encoding="utf-8")
    memory_ledger = (ROOT / "ea/app/repositories/ledger.py").read_text(encoding="utf-8")
    postgres_ledger = (ROOT / "ea/app/repositories/ledger_postgres.py").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "Promoted milestone capability `append_only_session_ledger_and_delta_sync_slice` to released" in changelog
    assert "append-only session-event writes" in changelog
    assert "events_for(...)" in changelog
    assert "delta-sync baseline" in changelog
    assert "event_id: str" in rewrite_route
    assert "created_at: str" in rewrite_route
    assert "events: list[SessionEventOut]" in rewrite_route
    assert "self._events[sid].append(event)" in memory_ledger
    assert "return list(self._events.get(str(session_id or \"\"), []))" in memory_ledger
    assert "INSERT INTO execution_events" in postgres_ledger
    assert "ORDER BY created_at ASC, event_id ASC" in postgres_ledger

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "append_only_session_ledger_and_delta_sync_slice"
    )
    assert capability["status"] == "released"
    assert capability.get("task_refs") == ["D-518"]
    assert "append-only and ledger-backed" in capability["notes"]
    assert "delta-sync" in capability["notes"]


def test_portable_engine_host_posture_is_documented_and_released() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "Promoted milestone capability `portable_engine_host_posture` to released" in changelog
    assert "deterministic core host-portability posture" in changelog
    assert "server/browser/embed host profile planning" in changelog
    assert "release/operator guards now pin that portability contract" in changelog

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "portable_engine_host_posture")
    assert capability["status"] == "released"
    assert capability.get("task_refs") == ["D-519"]
    assert "release/operator guards now pin that portability contract" in capability["notes"]


def test_local_coprocessor_optional_lane_is_documented_and_released() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))

    assert "Promoted milestone capability `local_coprocessor_optional_lane` to released" in changelog
    assert "optional/local BYOC acceleration lane" in changelog
    assert "feature-flagged and advisory-only baseline behavior" in changelog
    assert "no shipped runtime path requires local co-processor execution" in changelog

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "local_coprocessor_optional_lane")
    assert capability["status"] == "released"
    assert capability.get("task_refs") == ["D-520"]
    assert "optional and non-blocking branch baseline behavior" in capability["notes"]
    assert "no shipped runtime path may require local compute" in capability["notes"]


def test_codex_onemin_specialist_router_admission_is_documented_and_released() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))
    brain_catalog = (ROOT / "ea/app/services/brain_catalog.py").read_text(encoding="utf-8")
    provider_registry = (ROOT / "ea/app/services/provider_registry.py").read_text(encoding="utf-8")
    responses_upstream = (ROOT / "ea/app/services/responses_upstream.py").read_text(encoding="utf-8")

    assert "Promoted milestone capability `codex_onemin_specialist_router_admission` to released" in changelog
    assert "1min specialist-escalation-only router posture" in changelog
    assert "core lane admission stays explicit and capacity-gated" in changelog
    assert "proof-backed 1min top-up/billing snapshots" in changelog
    assert 'profile="core"' in brain_catalog
    assert 'provider_hint_order=("onemin",)' in brain_catalog
    assert 'capability_key="code_generate"' in provider_registry
    assert 'capability_key="reasoned_patch_review"' in provider_registry
    assert "billing_next_topup_at" in responses_upstream
    assert "billing_topup_amount" in responses_upstream
    assert "depletes_before_next_topup" in responses_upstream

    capability = next(
        entry for entry in milestone["capabilities"] if entry["name"] == "codex_onemin_specialist_router_admission"
    )
    assert capability["status"] == "released"
    assert capability.get("task_refs") == ["D-521"]
    assert "specialist escalation posture" in capability["notes"]
    assert "release/operator guards now pin those admission and specialist bindings" in capability["notes"]


def test_replay_forensics_horizon_bootstrap_is_documented_and_released() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    milestone = json.loads((ROOT / "MILESTONE.json").read_text(encoding="utf-8"))
    rewrite_route = (ROOT / "ea/app/api/routes/rewrite.py").read_text(encoding="utf-8")
    memory_ledger = (ROOT / "ea/app/repositories/ledger.py").read_text(encoding="utf-8")
    postgres_ledger = (ROOT / "ea/app/repositories/ledger_postgres.py").read_text(encoding="utf-8")
    retry_contracts = (ROOT / "tests/test_queue_retry_contracts.py").read_text(encoding="utf-8")

    assert "Promoted milestone capability `replay_forensics_horizon_bootstrap` to released" in changelog
    assert "retry/approval snapshot replay stability coverage" in changelog
    assert "event_id: str" in rewrite_route
    assert "created_at: str" in rewrite_route
    assert '@router.get("/receipts/{receipt_id}")' in rewrite_route
    assert '@router.get("/run-costs/{cost_id}")' in rewrite_route
    assert '@router.get("/artifacts/{artifact_id}")' in rewrite_route
    assert "return list(self._events.get(str(session_id or \"\"), []))" in memory_ledger
    assert "ORDER BY created_at ASC, event_id ASC" in postgres_ledger
    assert "test_approval_resume_snapshot_is_stable_for_retry_session_replay" in retry_contracts
    assert "test_approval_resume_service_snapshot_is_stable_for_retry_session_replay" in retry_contracts
    assert "test_approval_resume_delayed_retry_snapshot_is_stable_for_async_replay" in retry_contracts

    capability = next(entry for entry in milestone["capabilities"] if entry["name"] == "replay_forensics_horizon_bootstrap")
    assert capability["status"] == "released"
    assert capability.get("task_refs") == ["D-522"]
    assert "release/operator guards now pin those bootstrap artifacts" in capability["notes"]
