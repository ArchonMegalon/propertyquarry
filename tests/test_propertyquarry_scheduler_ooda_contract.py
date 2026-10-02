"""Contract tests for the PropertyQuarry OODA runtime-control topology.

The 2026-10 architecture moved OODA staging and public-origin observation
from dedicated compose sidecar services (propertyquarry-ooda-stage,
propertyquarry-ooda-public-origin-observer) to a hardened host-side
systemd controller: scripts/propertyquarry_ooda_host_controller.py fired
by config/systemd/propertyquarry-ooda-host-controller.{service,timer}.

These tests codify that topology exactly as it ships:

- the compose scheduler stays fail-closed: OODA notification default 0,
  no stage/observer sidecars anywhere in the file, no docker socket in
  the scheduler, bounded concurrency, and Phygital spend gates locked
  at enabled=0 / dry_run / generate=0;
- the host controller is a oneshot systemd unit whose only writable host
  path is the operator-owned _completion directory, driven by a
  persistent five-minute timer;
- the shared private artifact lane (artifacts volume plus the read-only
  config mount carrying the producer trust registry) stays intact.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _compose_text() -> str:
    return (ROOT / "docker-compose.property.yml").read_text(encoding="utf-8")


def _scheduler_compose_section() -> str:
    compose = _compose_text()
    marker = "  propertyquarry-scheduler:\n"
    assert marker in compose
    section = compose.split(marker, 1)[1]
    return section.split("\n  propertyquarry-render-tools:\n", 1)[0]


def _host_controller_service_unit() -> str:
    path = (
        ROOT
        / "config"
        / "systemd"
        / "propertyquarry-ooda-host-controller.service"
    )
    return path.read_text(encoding="utf-8")


def _host_controller_timer_unit() -> str:
    path = (
        ROOT / "config" / "systemd" / "propertyquarry-ooda-host-controller.timer"
    )
    return path.read_text(encoding="utf-8")


def test_propertyquarry_scheduler_ooda_is_fail_closed_by_default() -> None:
    section = _scheduler_compose_section()

    assert (
        'PROPERTYQUARRY_OODA_NOTIFICATION_ENABLED: '
        '"${PROPERTYQUARRY_OODA_NOTIFICATION_ENABLED:-0}"'
    ) in section
    assert (
        "# Host-side OODA runtime-control is not in the web image. Default off so"
        in section
    )
    assert "EA_SCHEDULER_STEP_CONCURRENCY_LIMIT: \"1\"" in section
    assert "/var/run/docker.sock" not in section
    assert "propertyquarry-ooda-stage:" not in section

    # Phygital spend gates stay fail-closed in the same section.
    assert (
        'PROPERTYQUARRY_PHYGITAL_ENABLED: "${PROPERTYQUARRY_PHYGITAL_ENABLED:-0}"'
        in section
    )
    assert (
        'PROPERTYQUARRY_PHYGITAL_MODE: "${PROPERTYQUARRY_PHYGITAL_MODE:-dry_run}"'
        in section
    )
    assert (
        'PROPERTYQUARRY_PHYGITAL_GENERATE: "${PROPERTYQUARRY_PHYGITAL_GENERATE:-0}"'
        in section
    )

    # Core service restart policy stays operator-safe.
    assert (
        'restart: "${PROPERTYQUARRY_SCHEDULER_RESTART_POLICY:-unless-stopped}"'
        in section
    )
    # Startup still gates on a healthy database.
    assert "condition: service_healthy" in section


def test_propertyquarry_scheduler_keeps_the_shared_private_artifact_lane() -> None:
    section = _scheduler_compose_section()
    compose = _compose_text()

    # The scheduler reads the producer trust registry and writes artifacts
    # through the same mounts production uses today.
    assert "propertyquarry_artifacts:/data/artifacts" in section
    assert "./config:/config:ro" in section
    assert (
        ROOT / "config" / "propertyquarry_ooda_source_refresh_producer_trust.v1.json"
    ).is_file()

    # The old tmpfs signal lane from the sidecar topology is gone.
    assert "/run/propertyquarry/ooda-signals" not in section
    assert "ooda-producer-claims" not in section
    assert "ooda-producer-completions" not in section

    # No stage/observer sidecars exist anywhere in the compose topology.
    assert "propertyquarry-ooda-stage:" not in compose
    assert "propertyquarry-ooda-public-origin-observer:" not in compose


def test_property_web_image_contains_only_lightweight_ooda_runtime_modules() -> None:
    dockerfile = (ROOT / "ea" / "Dockerfile.property-web").read_text(encoding="utf-8")

    for filename in (
        "propertyquarry_operator_action.py",
        "propertyquarry_ooda_approved_signals.py",
        "propertyquarry_ooda_public_origin_observation.py",
        "propertyquarry_secure_file_io.py",
        "propertyquarry_strict_json.py",
        "propertyquarry_notify_gold_status.py",
        "propertyquarry_notify_scene_video_provider_refresh.py",
        "propertyquarry_ooda_notification_cycle.py",
        "propertyquarry_ooda_operator_status.py",
        "propertyquarry_ooda_source_refresh_request.py",
        "propertyquarry_ooda_source_refresh_handoff.py",
        "propertyquarry_ooda_source_refresh_claims.py",
        "propertyquarry_ooda_source_refresh_settlement.py",
        "propertyquarry_ooda_source_refresh_trust_intake.py",
        "propertyquarry_ooda_source_refresh_trust_decision.py",
        "propertyquarry_ooda_source_refresh_trust_enrollment_preview.py",
        "propertyquarry_ooda_source_refresh_trust_enrollment_authorization.py",
        "propertyquarry_ooda_source_refresh_trust_enrollment_execution_readiness.py",
        "propertyquarry_ooda_source_refresh_trust_notification.py",
        "propertyquarry_ooda_scheduler_witness.py",
        "propertyquarry_stage_ooda_signals.py",
    ):
        assert f"COPY scripts/{filename} /app/scripts/{filename}" in dockerfile
    assert (
        "COPY --chmod=0555 "
        "scripts/propertyquarry_ooda_source_refresh_trust_enrollment_execution.py "
        "/app/scripts/propertyquarry_ooda_source_refresh_trust_enrollment_execution.py"
        in dockerfile
    )
    assert "COPY scripts/propertyquarry_gold_status.py /app/scripts/propertyquarry_gold_status.py" not in dockerfile


def test_deploy_prepares_only_the_dedicated_operator_owned_ingress() -> None:
    deploy = (ROOT / "scripts" / "deploy_propertyquarry.sh").read_text(encoding="utf-8")

    assert (
        'export PROPERTYQUARRY_OODA_SIGNAL_DIR="${APP_ROOT}/_completion/propertyquarry_ooda_signal_ingress"'
        in deploy
    )
    assert '[[ -L "${PROPERTYQUARRY_OODA_SIGNAL_DIR}" ]]' in deploy
    assert 'ooda_signal_dir_mode & 8#022' in deploy
    assert '/usr/bin/install -d -m 0755 -- "${PROPERTYQUARRY_OODA_SIGNAL_DIR}"' in deploy
    assert (
        'export PROPERTYQUARRY_OODA_STAGE_RECEIPT_DIR="${APP_ROOT}/_completion/propertyquarry_ooda_notification_cycle"'
        in deploy
    )
    assert (
        'export PROPERTYQUARRY_OODA_PUBLIC_ORIGIN_SOURCE_DIR="${APP_ROOT}/_completion/propertyquarry_ooda_public_origin_observation"'
        in deploy
    )
    assert 'ooda_source_file_mode & 8#022' in deploy
    assert 'ooda_public_origin_dir_mode & 8#077' in deploy
    assert (
        '/usr/bin/install -d -m 0700 -- "${PROPERTYQUARRY_OODA_PUBLIC_ORIGIN_SOURCE_DIR}"'
        in deploy
    )
    assert '/usr/bin/install -d -m 0700 -- "${PROPERTYQUARRY_OODA_STAGE_RECEIPT_DIR}"' in deploy


def test_public_origin_observation_is_host_side_with_bounded_authority() -> None:
    compose = _compose_text()
    assert "propertyquarry-ooda-public-origin-observer:" not in compose

    service_unit = _host_controller_service_unit()
    assert "Type=oneshot" in service_unit
    assert "UMask=0077" in service_unit
    assert "NoNewPrivileges=true" in service_unit
    assert "ProtectSystem=full" in service_unit
    assert "ReadWritePaths=/docker/property/_completion" in service_unit

    timer_unit = _host_controller_timer_unit()
    assert "OnUnitActiveSec=5min" in timer_unit
    assert "Persistent=true" in timer_unit
    assert "Unit=propertyquarry-ooda-host-controller.service" in timer_unit


def test_ooda_staging_has_no_container_sidecar_and_only_completion_write_access() -> None:
    compose = _compose_text()
    section = _scheduler_compose_section()

    assert "propertyquarry-ooda-stage:" not in compose
    assert "propertyquarry-ooda-public-origin-observer:" not in compose
    assert "/var/run/docker.sock" not in section

    service_unit = _host_controller_service_unit()
    assert (
        "ExecStart=/usr/bin/python3 /docker/property/scripts/propertyquarry_ooda_host_controller.py"
        in service_unit
    )
    assert "WorkingDirectory=/docker/property" in service_unit
    # Exactly one writable host path: the operator-owned completion dir.
    assert service_unit.count("ReadWritePaths=") == 1
    assert "/docker/property/_completion" in service_unit


def test_operator_summary_reads_the_runtime_trust_alert_receipt() -> None:
    summary = (ROOT / "scripts" / "operator_summary.sh").read_text(
        encoding="utf-8"
    )

    assert "scripts/propertyquarry_ooda_action_only.py" in summary
    assert "if ((OPERATOR_ACTION_ONLY == 1)); then" in summary
    for option in (
        "--trust-notification-receipt",
        "--trust-presentation-state",
        "--trust-decision-dir",
        "--trust-claim-verification",
        "--trust-registry",
        "--trust-candidate-dir",
        "--trust-intake-receipt",
        "--trust-intake-verification",
    ):
        assert option in summary

    assert (
        "/app/scripts/"
        "propertyquarry_ooda_source_refresh_trust_notification.py"
    ) in summary
    assert "runtime_source_refresh_trust_notification_projection" in summary
    assert '"--presentation-state"' in summary
    assert '"--receipt"' in summary
    assert '"trust candidate alert: "' in summary
    assert '"trust alert delivery: "' in summary
    assert '"trust alert receipt:  "' in summary
    assert (
        "/app/scripts/"
        "propertyquarry_ooda_source_refresh_trust_enrollment_preview.py"
    ) in summary
    assert (
        "runtime_source_refresh_trust_enrollment_preview_projection"
        in summary
    )
    assert '"trust enrollment preview: "' in summary
    assert '"trust registry preview: "' in summary
    assert '"trust preview consent: REQUIRED separate exact decision; "' in summary
    assert '"trust preview receipt: "' in summary
    assert (
        "/app/scripts/"
        "propertyquarry_ooda_source_refresh_trust_enrollment_authorization.py"
    ) in summary
    assert (
        "runtime_source_refresh_trust_enrollment_authorization_projection"
        in summary
    )
    assert '"trust enrollment authorization: "' in summary
    assert '"authorize exact preview template: "' in summary
    assert 'for trust_authorization_decision in ("reject", "defer")' in summary
    assert 'f"trust preview {trust_authorization_decision} template: "' in summary
    assert '"trust authorization execution: delegated only ' in summary
    assert (
        "/app/scripts/"
        "propertyquarry_ooda_source_refresh_trust_enrollment_execution_readiness.py"
    ) in summary
    assert (
        "runtime_source_refresh_trust_enrollment_execution_readiness_projection"
        in summary
    )
    assert '"trust enrollment execution readiness: "' in summary
    assert '"trust execution request: STAGED "' in summary
    assert '"trust execution availability: governed one-shot executor available; ' in summary
    assert '"trust execution readiness receipt: "' in summary
    assert (
        "/app/scripts/"
        "propertyquarry_ooda_source_refresh_trust_enrollment_execution.py"
    ) in summary
    assert (
        "runtime_source_refresh_trust_enrollment_execution_projection"
        in summary
    )
    assert '"trust enrollment execution: "' in summary
    assert '"trust execution template: "' in summary
