from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _run_promotion_scenario(
    tmp_path: Path,
    *,
    scenario: str,
    initial_ingress: str = "running",
    initial_api: str = "running",
    initial_worker: str = "running",
    initial_scheduler: str = "running",
    initial_render: str = "running",
    initial_migrate: str = "stopped",
    unknown_writer: str = "",
    postgres_session: str = "",
    replacement_writer: str = "",
    allowed_external_writer: bool = False,
    containment_journal_failure: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    events_path = tmp_path / f"{scenario}.events"
    shell = r'''
set -euo pipefail

declare -A SERVICE_STATE=(
  [ingress]="${INITIAL_INGRESS}"
  [api]="${INITIAL_API}"
  [worker]="${INITIAL_WORKER}"
  [scheduler]="${INITIAL_SCHEDULER}"
  [render]="${INITIAL_RENDER}"
  [migrate]="${INITIAL_MIGRATE}"
)

event() { printf '%s\n' "$*" >> "${EVENTS_PATH}"; }

container_state_line() {
  local service="${1#cid-}"
  case "${SERVICE_STATE[${service}]:-missing}" in
    running) printf 'running|healthy' ;;
    restarting) printf 'restarting|starting' ;;
    paused) printf 'paused|none' ;;
    stopped) printf 'exited|none' ;;
    dead) printf 'dead|none' ;;
    missing) printf 'unknown|none' ;;
  esac
}

fake_compose() {
  local action="$1"
  local skip_next=0
  local arg=""
  local service=""
  shift
  if [[ "${action}" == "ps" ]]; then
    for arg in "$@"; do service="${arg}"; done
    if [[ "${SERVICE_STATE[${service}]:-missing}" != "missing" ]]; then
      printf 'cid-%s' "${service}"
    fi
    return 0
  fi
  event "compose ${action} $*"
  case "${action}" in
    stop)
      for arg in "$@"; do
        if [[ "${skip_next}" == "1" ]]; then skip_next=0; continue; fi
        if [[ "${arg}" == "--timeout" ]]; then skip_next=1; continue; fi
        if [[ "${SERVICE_STATE[${arg}]:-missing}" != "missing" ]]; then
          SERVICE_STATE["${arg}"]="stopped"
        fi
      done
      ;;
    start)
      for arg in "$@"; do SERVICE_STATE["${arg}"]="running"; done
      ;;
    up)
      for arg in "$@"; do service="${arg}"; done
      SERVICE_STATE["${service}"]="running"
      ;;
    *) return 2 ;;
  esac
}

DC=(fake_compose)
source "${QUIESCE_HELPER}"
PROPERTYQUARRY_ALLOWED_DATABASE_WRITER_CONTAINER_NAMES=(api worker scheduler migrate)
EXTERNAL_ACTIVE=0
if [[ "${ALLOWED_EXTERNAL_WRITER}" == "1" ]]; then
  PROPERTYQUARRY_ALLOWED_DATABASE_WRITER_CONTAINER_NAMES+=(cross-stack-writer)
  EXTERNAL_ACTIVE=1
fi

database_writer_inventory_lines() {
  if [[ "${SERVICE_STATE[api]}" != "stopped" && "${SERVICE_STATE[api]}" != "missing" ]]; then
    printf 'cid-api|api\n'
  fi
  if [[ "${SERVICE_STATE[worker]}" != "stopped" && "${SERVICE_STATE[worker]}" != "missing" ]]; then
    printf 'cid-worker|worker\n'
  fi
  if [[ "${SERVICE_STATE[scheduler]}" != "stopped" && "${SERVICE_STATE[scheduler]}" != "missing" ]]; then
    printf 'cid-scheduler|scheduler\n'
  fi
  if [[ "${SERVICE_STATE[migrate]}" != "stopped" && "${SERVICE_STATE[migrate]}" != "missing" ]]; then
    printf 'cid-migrate|migrate\n'
  fi
  if [[ -n "${UNKNOWN_WRITER}" ]]; then
    printf 'cid-rogue|%s\n' "${UNKNOWN_WRITER}"
  fi
  if [[ -n "${REPLACEMENT_WRITER}" && "${SERVICE_STATE[api]}" == "stopped" ]]; then
    printf 'cid-replacement|%s\n' "${REPLACEMENT_WRITER}"
  fi
  if [[ "${EXTERNAL_ACTIVE}" == "1" ]]; then
    printf 'cid-cross|cross-stack-writer\n'
  fi
}

database_writer_session_inventory_lines() {
  [[ -z "${POSTGRES_SESSION}" ]] || printf '%s\n' "${POSTGRES_SESSION}"
}

stop_database_writer_container() {
  event "docker stop --time $2 $1"
  [[ "$1" != "cid-cross" ]] || EXTERNAL_ACTIVE=0
}
database_writer_container_is_active() {
  [[ "$1" == "cid-cross" && "${EXTERNAL_ACTIVE}" == "1" ]]
}
record_deploy_containment_journal() {
  event journal-contained
  [[ "${CONTAINMENT_JOURNAL_FAILURE}" != "1" ]]
}

propertyquarry_install_schema_quiesce_traps
PROPERTYQUARRY_SCHEMA_QUIESCE_TIMEOUT_SECONDS=30
propertyquarry_register_public_ingress_hold ingress ingress
if [[ "${SCENARIO}" == "crash-reconcile" || "${SCENARIO}" == "crash-reconcile-stale-receipt" ]]; then
  propertyquarry_reconcile_incomplete_deploy_runtime \
    api api worker worker scheduler scheduler render render migrate migrate 30
  event external-journal-reconciled
  propertyquarry_complete_crash_reconciliation
  event crash-reconciliation-complete
  if [[ "${SCENARIO}" == "crash-reconcile-stale-receipt" ]]; then
    event drain-receipt-stale
    false
  fi
  PROPERTYQUARRY_PUBLIC_INGRESS_HOLD_ARMED=0
  trap - EXIT INT TERM
  exit 0
fi
propertyquarry_hold_public_ingress_stopped
propertyquarry_quiesce_schema_writers \
  api api worker worker scheduler scheduler render render migrate migrate 30 2

case "${SCENARIO}" in
  first-deploy-failure|precommit-failure)
    SERVICE_STATE[migrate]="running"
    event migration-failed
    false
    ;;
  postcommit-failure|slo-failure)
    event migration-committed
    propertyquarry_mark_schema_migration_committed
    SERVICE_STATE[api]="running"
    SERVICE_STATE[worker]="running"
    SERVICE_STATE[scheduler]="running"
    SERVICE_STATE[render]="running"
    event candidate-services-ready
    [[ "${SCENARIO}" != "slo-failure" ]] || event canonical-slo-failed
    false
    ;;
  replay)
    event migration-committed
    propertyquarry_mark_schema_migration_committed
    SERVICE_STATE[api]="running"
    SERVICE_STATE[worker]="running"
    SERVICE_STATE[scheduler]="running"
    SERVICE_STATE[render]="running"
    event canonical-gates-green
    event drain-replay-rejected
    false
    ;;
  success)
    event migration-committed
    propertyquarry_mark_schema_migration_committed
    SERVICE_STATE[api]="running"
    SERVICE_STATE[worker]="running"
    SERVICE_STATE[scheduler]="running"
    SERVICE_STATE[render]="running"
    event canonical-gates-green
    event drain-consumed
    fake_compose up -d --no-deps --force-recreate ingress
    event public-release-verified
    propertyquarry_mark_public_ingress_promoted
    propertyquarry_finish_schema_quiesce
    ;;
  *) exit 64 ;;
esac
'''
    completed = subprocess.run(
        ["bash", "-c", shell],
        cwd=ROOT,
        env={
            **os.environ,
            "QUIESCE_HELPER": str(ROOT / "scripts/propertyquarry_deploy_quiesce.sh"),
            "EVENTS_PATH": str(events_path),
            "SCENARIO": scenario,
            "INITIAL_INGRESS": initial_ingress,
            "INITIAL_API": initial_api,
            "INITIAL_WORKER": initial_worker,
            "INITIAL_SCHEDULER": initial_scheduler,
            "INITIAL_RENDER": initial_render,
            "INITIAL_MIGRATE": initial_migrate,
            "UNKNOWN_WRITER": unknown_writer,
            "POSTGRES_SESSION": postgres_session,
            "REPLACEMENT_WRITER": replacement_writer,
            "ALLOWED_EXTERNAL_WRITER": "1" if allowed_external_writer else "0",
            "CONTAINMENT_JOURNAL_FAILURE": "1" if containment_journal_failure else "0",
        },
        text=True,
        capture_output=True,
        check=False,
    )
    events = events_path.read_text(encoding="utf-8").splitlines() if events_path.exists() else []
    return completed, events


def test_first_deploy_failure_keeps_absent_ingress_and_writers_inactive(tmp_path: Path) -> None:
    completed, events = _run_promotion_scenario(
        tmp_path,
        scenario="first-deploy-failure",
        initial_ingress="missing",
        initial_api="missing",
        initial_worker="missing",
        initial_scheduler="missing",
        initial_render="missing",
    )

    assert completed.returncode != 0
    assert events == [
        "compose stop --timeout 30 ingress",
        "compose stop --timeout 30 api worker scheduler render",
        "migration-failed",
        "compose stop --timeout 30 ingress",
        "compose stop --timeout 30 migrate",
    ]
    assert not any(event.startswith("compose start") for event in events)


def test_precommit_failure_holds_ingress_then_restores_only_prior_writers(tmp_path: Path) -> None:
    completed, events = _run_promotion_scenario(tmp_path, scenario="precommit-failure")

    assert completed.returncode != 0
    assert events == [
        "compose stop --timeout 30 ingress",
        "compose stop --timeout 30 api worker scheduler render",
        "migration-failed",
        "compose stop --timeout 30 ingress",
        "compose stop --timeout 30 migrate",
        "compose start api",
        "compose start worker",
        "compose start scheduler",
        "compose start render",
    ]


@pytest.mark.parametrize("scenario", ["postcommit-failure", "slo-failure", "replay"])
def test_every_postcommit_gate_failure_holds_ingress_api_worker_scheduler_and_render(
    tmp_path: Path,
    scenario: str,
) -> None:
    completed, events = _run_promotion_scenario(tmp_path, scenario=scenario)

    assert completed.returncode != 0
    assert events[0] == "compose stop --timeout 30 ingress"
    assert events[1] == "compose stop --timeout 30 api worker scheduler render"
    assert events[-3:] == [
        "compose stop --timeout 30 ingress",
        "compose stop --timeout 30 api worker scheduler render",
        "journal-contained",
    ]
    assert "compose up -d --no-deps --force-recreate ingress" not in events
    assert not any(event.startswith("compose start") for event in events)


def test_success_consumes_receipt_only_after_gates_and_before_ingress_start(tmp_path: Path) -> None:
    completed, events = _run_promotion_scenario(tmp_path, scenario="success")

    assert completed.returncode == 0, completed.stderr
    assert events == [
        "compose stop --timeout 30 ingress",
        "compose stop --timeout 30 api worker scheduler render",
        "migration-committed",
        "canonical-gates-green",
        "drain-consumed",
        "compose up -d --no-deps --force-recreate ingress",
        "public-release-verified",
    ]


@pytest.mark.parametrize("initial_render", ["running", "restarting", "paused"])
def test_active_render_is_stopped_and_verified_before_migration(
    tmp_path: Path,
    initial_render: str,
) -> None:
    completed, events = _run_promotion_scenario(
        tmp_path,
        scenario="success",
        initial_render=initial_render,
    )

    assert completed.returncode == 0, completed.stderr
    assert events.index("compose stop --timeout 30 api worker scheduler render") < events.index(
        "migration-committed"
    )


@pytest.mark.parametrize(
    ("unknown_writer", "postgres_session"),
    [("rogue-writer", ""), ("cross-stack-writer", ""), ("", "postgres-session-919")],
)
def test_unknown_container_or_postgres_writer_blocks_before_ddl_with_ingress_held(
    tmp_path: Path,
    unknown_writer: str,
    postgres_session: str,
) -> None:
    completed, events = _run_promotion_scenario(
        tmp_path,
        scenario="success",
        unknown_writer=unknown_writer,
        postgres_session=postgres_session,
    )

    assert completed.returncode != 0
    assert "migration-committed" not in events
    assert events[0] == "compose stop --timeout 30 ingress"
    assert events.count("compose stop --timeout 30 ingress") >= 2


def test_writer_appearing_after_snapshot_is_caught_by_immediate_pre_ddl_revalidation(
    tmp_path: Path,
) -> None:
    completed, events = _run_promotion_scenario(
        tmp_path,
        scenario="success",
        replacement_writer="late-writer",
    )

    assert completed.returncode != 0
    assert "migration-committed" not in events
    assert "compose stop --timeout 30 api worker scheduler render" in events
    assert events.count("compose stop --timeout 30 ingress") >= 2


def test_allowlisted_cross_stack_writer_is_stopped_and_verified_before_ddl(
    tmp_path: Path,
) -> None:
    completed, events = _run_promotion_scenario(
        tmp_path,
        scenario="success",
        allowed_external_writer=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert events.index("docker stop --time 30 cid-cross") < events.index("migration-committed")


def test_startup_crash_reconciliation_contains_every_writer_before_completion(
    tmp_path: Path,
) -> None:
    completed, events = _run_promotion_scenario(
        tmp_path,
        scenario="crash-reconcile",
        allowed_external_writer=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert events == [
        "compose stop --timeout 30 ingress",
        "compose stop --timeout 30 api worker scheduler render migrate",
        "docker stop --time 30 cid-cross",
        "external-journal-reconciled",
        "crash-reconciliation-complete",
    ]


def test_crash_reconciliation_session_recheck_failure_recontains_and_journals(
    tmp_path: Path,
) -> None:
    completed, events = _run_promotion_scenario(
        tmp_path,
        scenario="crash-reconcile",
        postgres_session="stale-postgres-session",
    )

    assert completed.returncode != 0
    assert events[-3:] == [
        "compose stop --timeout 30 ingress",
        "compose stop --timeout 30 api worker scheduler render",
        "journal-contained",
    ]
    assert "crash-reconciliation-complete" not in events


def test_containment_journal_failure_never_releases_postcommit_runtime(
    tmp_path: Path,
) -> None:
    completed, events = _run_promotion_scenario(
        tmp_path,
        scenario="postcommit-failure",
        containment_journal_failure=True,
    )

    assert completed.returncode != 0
    assert events[-3:] == [
        "compose stop --timeout 30 ingress",
        "compose stop --timeout 30 api worker scheduler render",
        "journal-contained",
    ]
    assert "compose up -d --no-deps --force-recreate ingress" not in events


def test_stale_new_receipt_cannot_prevent_crash_containment_of_live_migrator(
    tmp_path: Path,
) -> None:
    completed, events = _run_promotion_scenario(
        tmp_path,
        scenario="crash-reconcile-stale-receipt",
        initial_migrate="running",
    )

    assert completed.returncode != 0
    first_runtime_stop = events.index(
        "compose stop --timeout 30 api worker scheduler render migrate"
    )
    journal_reconciled = events.index("external-journal-reconciled")
    reconciliation_complete = events.index("crash-reconciliation-complete")
    stale_receipt = events.index("drain-receipt-stale")
    assert events[0] == "compose stop --timeout 30 ingress"
    assert first_runtime_stop < journal_reconciled < reconciliation_complete < stale_receipt
    assert events[-1] == "compose stop --timeout 30 ingress"
    assert "compose up -d --no-deps --force-recreate ingress" not in events
    assert not any(event.startswith("compose start") for event in events)
