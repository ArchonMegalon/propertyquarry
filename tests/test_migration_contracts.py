from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_schema_readme_lists_latest_migrations() -> None:
    text = (ROOT / "ea/schema/README.md").read_text()
    assert "20260305_v0_5_artifacts_kernel.sql" in text
    assert "20260305_v0_6_execution_ledger_v2.sql" in text
    assert "20260305_v0_7_approvals_kernel.sql" in text
    assert "20260305_v0_8_channel_runtime_reliability.sql" in text
    assert "20260305_v0_9_tool_connector_kernel.sql" in text
    assert "20260305_v0_10_task_contracts_kernel.sql" in text
    assert "20260305_v0_31_artifact_principal_scope.sql" in text
    assert "20260305_v0_32_provider_bindings_kernel.sql" in text
    assert "20260305_v0_33_task_contract_runtime_policy.sql" in text
    assert "20260305_v0_34_assistant_onboarding_canonical_schema.sql" in text
    assert "20260305_v0_35_execution_ledger_legacy_compat.sql" in text
    assert "20260305_v0_36_propertyquarry_property_passport.sql" in text
    assert "20260305_v0_37_runtime_repository_contract.sql" in text
    assert "20260305_v0_36_propertyquarry_property_passport.sql" in text


def test_db_bootstrap_includes_latest_migrations() -> None:
    text = (ROOT / "scripts/db_bootstrap.sh").read_text()
    assert "20260305_v0_5_artifacts_kernel.sql" in text
    assert "20260305_v0_6_execution_ledger_v2.sql" in text
    assert "20260305_v0_7_approvals_kernel.sql" in text
    assert "20260305_v0_8_channel_runtime_reliability.sql" in text
    assert "20260305_v0_9_tool_connector_kernel.sql" in text
    assert "20260305_v0_10_task_contracts_kernel.sql" in text
    assert "20260305_v0_31_artifact_principal_scope.sql" in text
    assert "20260305_v0_32_provider_bindings_kernel.sql" in text
    assert "20260305_v0_33_task_contract_runtime_policy.sql" in text
    assert "20260305_v0_34_assistant_onboarding_canonical_schema.sql" in text
    assert "20260305_v0_35_execution_ledger_legacy_compat.sql" in text
    assert "20260305_v0_36_propertyquarry_property_passport.sql" in text
    assert "20260305_v0_37_runtime_repository_contract.sql" in text
    assert "20260305_v0_36_propertyquarry_property_passport.sql" in text


def test_latest_kernel_migrations_define_provider_bindings_and_runtime_policy_column() -> None:
    provider_bindings = (ROOT / "ea/schema/20260305_v0_32_provider_bindings_kernel.sql").read_text()
    runtime_policy = (ROOT / "ea/schema/20260305_v0_33_task_contract_runtime_policy.sql").read_text()
    artifact_scope = (ROOT / "ea/schema/20260305_v0_31_artifact_principal_scope.sql").read_text()

    assert "CREATE TABLE IF NOT EXISTS provider_bindings" in provider_bindings
    assert "idx_provider_bindings_principal_provider" in provider_bindings
    assert "idx_provider_bindings_principal_updated" in provider_bindings

    assert "ALTER TABLE task_contracts" in runtime_policy
    assert "ADD COLUMN IF NOT EXISTS runtime_policy_json JSONB NOT NULL DEFAULT '{}'::jsonb" in runtime_policy
    assert "WHERE a.session_id = es.session_id::text" in artifact_scope


def test_propertyquarry_property_passport_migration_defines_canonical_graph_tables() -> None:
    passport = (ROOT / "ea/schema/20260305_v0_36_propertyquarry_property_passport.sql").read_text()

    assert "CREATE TABLE IF NOT EXISTS propertyquarry_property_entities" in passport
    assert "CREATE TABLE IF NOT EXISTS propertyquarry_listing_instances" in passport
    assert "CREATE TABLE IF NOT EXISTS propertyquarry_property_claims" in passport
    assert "CREATE TABLE IF NOT EXISTS propertyquarry_property_events" in passport
    assert "PRIMARY KEY (principal_id, property_id)" in passport
    assert "UNIQUE (principal_id, identity_key)" in passport
    assert "REFERENCES propertyquarry_property_entities(principal_id, property_id)" in passport
    assert "idx_propertyquarry_property_claims_property_field_seen" in passport
    assert "idx_propertyquarry_property_events_property_seen" in passport


def test_legacy_migration_regression_smoke_contract_is_wired() -> None:
    smoke = (ROOT / "scripts/smoke_postgres.sh").read_text()
    smoke_api = (ROOT / "scripts/smoke_api.sh").read_text()
    postgres_contracts = (ROOT / "scripts/test_postgres_contracts.sh").read_text()
    makefile = (ROOT / "Makefile").read_text()

    assert "--legacy-fixture" in smoke
    assert "apply_legacy_fixture()" in smoke
    assert "validate_legacy_upgrade()" in smoke
    assert 'POSTGRES_DB="${SMOKE_DB}" bash scripts/db_bootstrap.sh' in smoke
    assert "smoke-postgres legacy fixture complete" in smoke
    assert "execution_events missing runtime columns" in smoke
    assert "execution_events.event_id type mismatch" in smoke
    assert "execution_steps missing runtime columns" in smoke
    assert "approval_requests missing runtime columns" in smoke
    assert "approval_decisions missing runtime columns" in smoke
    assert 'set_env_value "EA_API_TOKEN" "smoke-postgres-token"' in smoke
    assert 'export EA_API_TOKEN="smoke-postgres-token"' in smoke
    assert 'set_env_value "EA_RUNTIME_MODE" "test"' in smoke
    assert smoke.index('set_env_value "EA_RUNTIME_MODE" "test"') < smoke.index(
        'set_env_value "EA_ALLOW_LOOPBACK_NO_AUTH" "1"'
    )
    assert smoke.index('set_env_value "EA_RUNTIME_MODE" "test"') < smoke.index(
        'set_env_value "EA_RUNTIME_MODE" "prod"'
    )
    assert 'set_env_value "EA_ALLOW_LOOPBACK_NO_AUTH" "1"' in smoke
    assert 'export EA_ALLOW_LOOPBACK_NO_AUTH="1"' in smoke
    assert 'set_env_value "EA_ALLOW_LOOPBACK_NO_AUTH" "0"' in smoke
    assert 'export EA_ALLOW_LOOPBACK_NO_AUTH="0"' in smoke
    assert smoke.index('set_env_value "EA_ALLOW_LOOPBACK_NO_AUTH" "0"') < smoke.index(
        'set_env_value "EA_RUNTIME_MODE" "prod"'
    )
    assert "secrets.token_hex(32)" in smoke
    assert 'set_env_value "EA_SIGNING_SECRET" "${smoke_signing_secret}"' in smoke
    assert 'export EA_SIGNING_SECRET="${smoke_signing_secret}"' in smoke
    assert 'python -m app.product.propertyquarry_schema migrate' in smoke
    assert '--applied-by smoke-postgres' in smoke
    assert '"${PYTHON_BIN}" -m app.product.property_search_schema migrate' in postgres_contracts
    assert '--applied-by postgres-contracts' in postgres_contracts
    assert postgres_contracts.index("app.product.property_search_schema migrate") < postgres_contracts.index(
        '"${PYTHON_BIN}" -m pytest'
    )
    assert "container_loopback_no_auth=" in smoke
    assert "expected ea-api smoke container to enable EA_ALLOW_LOOPBACK_NO_AUTH" in smoke
    assert "container_api_token=" in smoke
    assert "ORIGINAL_EA_API_TOKEN=" in smoke
    assert "token_candidates=" in smoke
    assert 'X-EA-API-Token: ${candidate_token}' in smoke
    assert 'X-EA-API-Token: ${EA_API_TOKEN}' in smoke_api
    assert smoke_api.index("resolve_api_container()") < smoke_api.index(
        'api_container="$(resolve_api_container)"'
    )
    assert smoke_api.count('if [[ -z "${EA_API_TOKEN}" ]] && command -v docker') == 1
    assert "2>/dev/null | head -n1 || true" in smoke_api
    assert 'set_env_value "EA_OPERATOR_PRINCIPAL_IDS" "exec-1"' in smoke
    assert "docker cp" in smoke
    assert 'API_SERVICE="${PROPERTYQUARRY_API_SERVICE:-${EA_API_SERVICE:-ea-api}}"' in smoke
    assert 'resolve_service_container()' in smoke
    assert '"${API_CONTAINER}" bash /app/scripts/smoke_api.sh' in smoke
    assert "refresh_ltds_via_api.sh" in smoke
    assert "refresh_ltds_via_api.py" in smoke
    assert "container_operator_principal=" in smoke
    assert 'EA_OPERATOR_PRINCIPAL_ID="${container_operator_principal}"' in smoke
    assert "EA_ALLOW_LOOPBACK_NO_AUTH=${EA_ALLOW_LOOPBACK_NO_AUTH:-0}" in (ROOT / "docker-compose.yml").read_text()
    assert "wait_for_postgres_sql 90" in smoke
    assert "consecutive=$((consecutive + 1))" in smoke
    assert "compose up (api + worker)" in smoke
    assert '"${DC[@]}" up -d --no-deps --build --force-recreate "${API_SERVICE}" "${WORKER_SERVICE}"' in smoke
    assert "bash scripts/smoke_postgres.sh --legacy-fixture" in makefile
    assert "smoke-postgres-legacy:" in makefile


def test_postgres_smoke_resolves_default_and_propertyquarry_service_aliases() -> None:
    base_env = os.environ.copy()
    for key in (
        "PROPERTYQUARRY_API_SERVICE",
        "PROPERTYQUARRY_WORKER_SERVICE",
        "PROPERTYQUARRY_SCHEDULER_SERVICE",
        "PROPERTYQUARRY_DB_SERVICE",
        "PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED",
        "EA_API_SERVICE",
        "EA_WORKER_SERVICE",
        "EA_SCHEDULER_SERVICE",
        "EA_DB_SERVICE",
    ):
        base_env.pop(key, None)

    def resolved(extra_env: dict[str, str] | None = None) -> dict[str, str]:
        env = base_env | (extra_env or {})
        result = subprocess.run(
            ["bash", "scripts/smoke_postgres.sh", "--print-service-selection"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        return dict(line.split("=", 1) for line in result.stdout.splitlines())

    assert resolved() == {
        "api": "ea-api",
        "worker": "ea-worker",
        "scheduler": "ea-scheduler",
        "db": "ea-db",
        "public_home_required": "0",
    }
    assert resolved(
        {
            "PROPERTYQUARRY_API_SERVICE": "propertyquarry-api-candidate",
            "PROPERTYQUARRY_WORKER_SERVICE": "propertyquarry-worker-candidate",
            "PROPERTYQUARRY_SCHEDULER_SERVICE": "propertyquarry-scheduler-candidate",
            "PROPERTYQUARRY_DB_SERVICE": "propertyquarry-db-candidate",
        }
    ) == {
        "api": "propertyquarry-api-candidate",
        "worker": "propertyquarry-worker-candidate",
        "scheduler": "propertyquarry-scheduler-candidate",
        "db": "propertyquarry-db-candidate",
        "public_home_required": "1",
    }


def test_legacy_compatibility_migrations_encode_uuid_and_approval_upgrades() -> None:
    ledger = (ROOT / "ea/schema/20260305_v0_6_execution_ledger_v2.sql").read_text()
    ledger_compat = (ROOT / "ea/schema/20260305_v0_35_execution_ledger_legacy_compat.sql").read_text()
    approvals = (ROOT / "ea/schema/20260305_v0_7_approvals_kernel.sql").read_text()
    human_tasks = (ROOT / "ea/schema/20260305_v0_24_human_tasks_kernel.sql").read_text()

    assert "Some older installations use UUID-typed session identifiers" in ledger
    assert "format_type(a.atttypid, a.atttypmod)" in ledger
    assert "session_id %s NOT NULL REFERENCES execution_sessions(session_id)" in ledger

    assert "Older rewrite installations may still expose bigint event IDs" in ledger_compat
    assert "ADD COLUMN IF NOT EXISTS name TEXT" in ledger_compat
    assert "ALTER COLUMN event_id TYPE TEXT USING event_id::text" in ledger_compat
    assert "ALTER COLUMN event_type SET DEFAULT ''event''" in ledger_compat
    assert "ADD COLUMN IF NOT EXISTS step_kind TEXT" in ledger_compat
    assert "ADD COLUMN IF NOT EXISTS state TEXT" in ledger_compat
    assert "ADD COLUMN IF NOT EXISTS error_json JSONB" in ledger_compat

    assert "Older installations may have legacy approval tables" in approvals
    assert "approval_request_id" in approvals
    assert "approval_decision_id" in approvals
    assert "SET approval_id = 'legacy-' || approval_request_id::text" in approvals
    assert "SET decision_id = 'legacy-' || approval_decision_id::text" in approvals

    assert "Some upgraded installations may still use UUID-typed session identifiers" in human_tasks
    assert "format_type(a.atttypid, a.atttypmod)" in human_tasks
    assert "session_id %s NOT NULL REFERENCES execution_sessions(session_id)" in human_tasks
    assert "step_id %s NULL REFERENCES execution_steps(step_id)" in human_tasks


def test_postgres_ledger_runtime_bootstrap_heals_legacy_event_and_step_shapes() -> None:
    ledger_repo = (ROOT / "ea/app/repositories/ledger_postgres.py").read_text()

    assert "format_type(a.atttypid, a.atttypmod)" in ledger_repo
    assert "ADD COLUMN IF NOT EXISTS name TEXT" in ledger_repo
    assert "ALTER COLUMN event_id TYPE TEXT USING event_id::text" in ledger_repo
    assert "ALTER COLUMN event_type SET DEFAULT 'event'" in ledger_repo
    assert "ADD COLUMN IF NOT EXISTS step_kind TEXT" in ledger_repo
    assert "ADD COLUMN IF NOT EXISTS error_json JSONB" in ledger_repo


def test_operator_summary_lists_legacy_postgres_shortcuts() -> None:
    text = (ROOT / "scripts/operator_summary.sh").read_text()
    smoke_help = (ROOT / "scripts/smoke_help.sh").read_text()
    makefile = (ROOT / "Makefile").read_text()

    assert "Usage:" in text
    assert "make smoke-postgres-legacy" in text
    assert "make release-smoke" in text
    assert "make all-local" in text
    assert "make ci-gates-postgres-legacy" in text
    assert "make ci-gates-postgres" in text
    assert "make verify-release-assets" in text
    assert "make verify-flagship-release-readiness" in text
    assert "propertyquarry_release_make_dispatch.py release-preflight" in text
    assert "propertyquarry_release_make_dispatch.py ci-gates-authenticated" in text
    assert "propertyquarry_release_make_dispatch.py ltd-release-gates" in text
    assert "make provider-readiness" in text
    assert "verify-flagship-release-readiness" in makefile
    assert "verify-flagship-release-readiness:" in makefile
    assert "provider-readiness:" in makefile
    assert "make overlay-vision-check" in text
    assert "make overlay-vision-pull" in text
    assert "make support-bundle" in text
    assert "make tasks-archive" in text
    assert "make tasks-archive-dry-run" in text
    assert "make tasks-archive-prune" in text
    assert "scripts/operator_summary.sh" in smoke_help
    assert "scripts/operator_summary.sh" in makefile
    assert "scripts/chummer6_overlay_vision_readiness.py" in makefile


def _make_target_body(makefile: str, target: str) -> str:
    marker = f"{target}:"
    start = makefile.index(marker)
    target_lines = makefile[start:].splitlines()
    lines = [target_lines[0]]
    for line in target_lines[1:]:
        if line and not line.startswith(("\t", " ")):
            break
        lines.append(line)
    return "\n".join(lines)


def test_local_gate_bundles_include_flagship_readiness_and_generated_cleanliness() -> None:
    makefile = (ROOT / "Makefile").read_text()
    readme = (ROOT / "README.md").read_text()
    runbook = (ROOT / "RUNBOOK.md").read_text()
    deploy = (ROOT / "scripts/deploy_propertyquarry.sh").read_text()

    ci_gates = _make_target_body(makefile, "ci-gates")
    all_local = _make_target_body(makefile, "all-local")
    release_preflight = _make_target_body(makefile, "release-preflight")

    for body in (ci_gates, all_local, release_preflight):
        assert "verify-release-assets" in body
        assert "verify-flagship-release-readiness" in body
        assert "verify-generated-release-artifacts-clean" in body

    generated_clean = _make_target_body(makefile, "verify-generated-release-artifacts-clean")
    assert "scripts/verify_generated_release_artifacts_clean.py" in generated_clean
    assert "--materialize-in-sandbox" not in generated_clean
    assert "materialize-release-assets" not in generated_clean
    assert "git diff --exit-code -- .codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json" not in generated_clean

    for target in ("verify-release-assets", "verify-flagship-release-readiness", "test-api"):
        assert "materialize-release-assets" not in _make_target_body(makefile, target)

    assert "scripts/check_property_repository_role.py" in deploy
    assert "scripts/propertyquarry_local_deployment_receipt.py" in deploy
    assert "flagship release-readiness verification" in readme
    assert "generated release artifact cleanliness" in readme
    assert "flagship release readiness" in runbook
    assert "generated release artifact cleanliness" in runbook
    assert "- `make verify-flagship-release-readiness`" in runbook
    assert "- `make verify-generated-release-artifacts-clean`" in runbook


def test_hard_exit_gate_targets_and_runtime_gate_scripts_are_wired() -> None:
    makefile = (ROOT / "Makefile").read_text()
    readme = (ROOT / "README.md").read_text()
    runbook = (ROOT / "RUNBOOK.md").read_text()
    deploy = (ROOT / "scripts/deploy.sh").read_text()
    runtime_gate = (ROOT / "scripts/runtime_hard_exit_gates.sh").read_text()
    full_gate = (ROOT / "scripts/hard_exit_gates.sh").read_text()
    tibor_smoke = (ROOT / "scripts/smoke_api_tibor.sh").read_text()

    runtime_target = _make_target_body(makefile, "runtime-hard-exit-gates")
    hard_target = _make_target_body(makefile, "hard-exit-gates")
    ltd_target = _make_target_body(makefile, "ltd-release-gates")
    critical_target = _make_target_body(makefile, "verify-ltd-critical-entries")
    flagship_target = _make_target_body(makefile, "verify-ltd-flagship-subset")
    test_all = _make_target_body(makefile, "test-all")

    assert "scripts/runtime_hard_exit_gates.sh" in runtime_target
    assert "scripts/hard_exit_gates.sh" in hard_target
    assert "bootstrap-propertyquarry-release-python" in hard_target
    assert "verify-ltd-critical-entries" in ltd_target
    assert "verify-ltd-flagship-subset" in ltd_target
    assert "scripts/verify_ltd_critical_entries.py" in critical_target
    assert "scripts/verify_ltd_flagship_subset.py" in flagship_target
    assert "pytest -q" in test_all

    assert "bash scripts/smoke_help.sh" in runtime_gate
    assert "env -u EA_API_TOKEN bash scripts/smoke_api.sh" in runtime_gate
    assert (
        'if [[ "${propertyquarry_runtime_gates_enabled}" == "0" ]]; then\n'
        "  env -u EA_API_TOKEN bash scripts/smoke_api.sh"
    ) in runtime_gate
    assert runtime_gate.startswith("#!/bin/bash -p\nset -euo pipefail\nset +x\n")
    assert 'release_probe_secret="${PROPERTYQUARRY_LIVE_PROBE_SECRET:-${PROPERTYQUARRY_PERFORMANCE_RELEASE_PROBE_SECRET:-}}"' in runtime_gate
    assert "unset PROPERTYQUARRY_LIVE_PROBE_SECRET PROPERTYQUARRY_PERFORMANCE_RELEASE_PROBE_SECRET" in runtime_gate
    assert "unset PROPERTYQUARRY_RELEASE_PROBE_SECRET PROPERTYQUARRY_RELEASE_PROBE_PRINCIPAL_ID" in runtime_gate
    assert "release-probe credential must be one line of at most 4096 bytes" in runtime_gate
    assert "PROPERTYQUARRY_RUNTIME_GATES=1 requires PROPERTYQUARRY_LIVE_PROBE_SECRET" in runtime_gate
    assert "skipping authenticated/mobile/provider" not in runtime_gate
    assert (
        'env -u EA_API_TOKEN PYTHONPATH=ea "${PYTHON_BIN}" '
        "scripts/propertyquarry_live_public_smoke.py"
    ) in runtime_gate
    assert "smoke_api_tibor.sh` stays in the full hard-exit bundle" in runtime_gate
    assert 'PYTHON_BIN="${PYTHON_BIN:-}"' in runtime_gate
    assert '"${PYTHON_BIN}" scripts/verify_pocket_audio_archive.py' in runtime_gate
    assert runtime_gate.count("--release-probe-secret-stdin") == 3
    assert runtime_gate.count('<<<"${release_probe_secret}"') == 3
    assert runtime_gate.count("env -u EA_API_TOKEN -u PROPERTYQUARRY_LIVE_API_TOKEN") == 3
    assert '--api-token "${EA_API_TOKEN}"' not in runtime_gate
    assert "--seed-research-detail-fixture" not in runtime_gate
    assert 'propertyquarry_probe_principal_id="propertyquarry-release-probe"' in runtime_gate
    assert 'propertyquarry_probe_plan_label="${PROPERTYQUARRY_LIVE_SMOKE_PLAN_LABEL:-Free}"' in runtime_gate
    assert 'propertyquarry_base_url="${PROPERTYQUARRY_LIVE_SMOKE_BASE_URL:-https://propertyquarry.com}"' in runtime_gate
    assert "--no-execute-search-matrix" in runtime_gate
    assert "--no-cross-country-sanitization" in runtime_gate
    assert runtime_gate.rindex("unset release_probe_secret") > runtime_gate.index(
        "PROPERTYQUARRY_LIVE_PROVIDER_SMOKE=1"
    )

    assert '"${RELEASE_PYTHON}" -m pytest -q' in full_gate
    assert '"${RELEASE_DISPATCH}" release-preflight' in full_gate
    assert '"${RELEASE_DISPATCH}" verify-pocket-audio-archive' in full_gate
    assert (
        '"${RELEASE_DISPATCH}" verify-ltd-critical-entries-authenticated'
        in full_gate
    )
    assert (
        '"${RELEASE_DISPATCH}" verify-ltd-flagship-subset-authenticated'
        in full_gate
    )
    assert "/bin/bash -p scripts/test_postgres_contracts.sh" in full_gate
    assert "run_flagship_postgres_smoke() {" in full_gate
    assert '/bin/bash -p scripts/smoke_postgres.sh "$@"' in full_gate
    assert full_gate.count("\nrun_flagship_postgres_smoke\n") == 1
    assert (
        full_gate.count("\nrun_flagship_postgres_smoke --legacy-fixture\n")
        == 1
    )
    assert "PROPERTYQUARRY_SMOKE_PUBLIC_HOME_REQUIRED=1" in full_gate
    assert "/bin/bash -p scripts/smoke_api_tibor.sh" in full_gate
    assert "make " not in full_gate
    assert "./scripts/bootstrap_propertyquarry_release_python.sh" in full_gate
    assert "./scripts/hard_exit_gates.sh" in full_gate

    assert "EA_RUN_RUNTIME_HARD_EXIT_GATES=1|0" in deploy
    assert 'EA_RUN_RUNTIME_HARD_EXIT_GATES:-1' in deploy
    assert 'scripts/runtime_hard_exit_gates.sh' in deploy

    assert "make runtime-hard-exit-gates" in readme
    assert "make hard-exit-gates" in readme
    assert "propertyquarry_release_make_dispatch.py ltd-release-gates" in readme
    assert "make verify-ltd-critical-entries" in readme
    assert "make verify-ltd-flagship-subset" in readme
    assert "hard-exit and LTD verifier scripts" in readme
    assert "make runtime-hard-exit-gates" in runbook
    assert "make hard-exit-gates" in runbook
    assert "propertyquarry_release_make_dispatch.py ltd-release-gates" in runbook
    assert "verify_ltd_critical_entries.py" in runbook
    assert "verify_ltd_flagship_subset.py" in runbook
    assert 'cp "${EA_ROOT}/LTDs.md"' in tibor_smoke
    assert 'bash "${EA_ROOT}/scripts/refresh_ltds_via_api.sh"' in tibor_smoke
    assert "PYTHON_BIN" in (ROOT / "scripts/smoke_help.sh").read_text()


def test_endpoint_version_openapi_scripts_have_help_contracts_and_wiring() -> None:
    smoke_help = (ROOT / "scripts/smoke_help.sh").read_text()
    makefile = (ROOT / "Makefile").read_text()

    for rel in (
        "scripts/list_endpoints.sh",
        "scripts/version_info.sh",
        "scripts/export_openapi.sh",
        "scripts/diff_openapi.sh",
        "scripts/prune_openapi.sh",
    ):
        text = (ROOT / rel).read_text()
        assert "Usage:" in text
        assert rel in smoke_help
        assert rel in makefile


def test_smoke_help_has_help_contract_and_operator_help_wiring() -> None:
    smoke_help = (ROOT / "scripts/smoke_help.sh").read_text()
    makefile = (ROOT / "Makefile").read_text()

    assert "Usage:" in smoke_help
    assert "scripts/smoke_help.sh" in makefile
    for rel in (
        "scripts/hard_exit_gates.sh",
        "scripts/runtime_hard_exit_gates.sh",
        "scripts/verify_ltd_critical_entries.py",
        "scripts/verify_ltd_flagship_subset.py",
        "scripts/bootstrap_payfunnels_propertyquarry.py",
        "scripts/bootstrap_emailit_propertyquarry.py",
    ):
        assert rel in makefile
        assert rel in smoke_help
