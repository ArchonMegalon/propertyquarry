from __future__ import annotations

import subprocess
from pathlib import Path

from scripts import propertyquarry_observability_receipts as observability_receipts


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_slo_capture_operator_cli_keeps_bearer_out_of_arguments_and_public_targets() -> None:
    script = _read("scripts/propertyquarry_slo_capture.py")
    completed = subprocess.run(
        ["python3", "scripts/propertyquarry_slo_capture.py", "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "--token-env" in completed.stdout
    assert "parser.add_argument(\"--token\"" not in script
    assert "Authorization" in script
    assert "credential_persisted" in script
    assert "loopback or private IP literal" in script
    assert "_RejectRedirects" in script
    assert "Cache-Control: no-store" in script
    assert "stat.S_IRUSR | stat.S_IWUSR" in script


def test_property_release_bundle_fails_closed_on_release_bound_slo_evidence_before_gold() -> None:
    script = _read("scripts/property_release_gates.sh")
    slo_gate = script.index("scripts/propertyquarry_slo_evidence.py")
    observability_gate = script.index("scripts/propertyquarry_observability_receipts.py verify")
    observability_gate_end = script.index(
        "scene_video_shared_env_file=",
        observability_gate,
    )
    gold_gate = script.index("scripts/propertyquarry_gold_status.py")
    observability_command = script[observability_gate:observability_gate_end]

    parser = observability_receipts.build_parser()
    command_action = next(
        action for action in parser._actions if action.dest == "command"
    )
    verify_parser = command_action.choices["verify"]
    required_verify_options = {
        option
        for action in verify_parser._actions
        if action.required
        for option in action.option_strings
    }

    assert "PROPERTYQUARRY_SLO_METRICS_SNAPSHOT" in script
    assert "PROPERTYQUARRY_SLO_METRICS_PROBE_RECEIPT" in script
    assert required_verify_options
    for option in required_verify_options:
        assert option in observability_command
    assert "--flagship" in script[slo_gate:gold_gate]
    assert '--release-sha "${dr_release_commit_sha}"' in script[slo_gate:gold_gate]
    assert '--image-digest "${dr_release_image_digest}"' in script[slo_gate:gold_gate]
    assert '--metrics-snapshot "${slo_metrics_snapshot}"' in script[observability_gate:gold_gate]
    assert '--metrics-probe "${slo_metrics_probe_receipt}"' in script[observability_gate:gold_gate]
    assert (
        '--dashboard-render-receipt "${dashboard_render_receipt}"'
        in script[observability_gate:gold_gate]
    )
    assert (
        '--structured-log-query-receipt "${structured_log_query_receipt}"'
        in script[observability_gate:gold_gate]
    )
    assert (
        '--distributed-trace-query-receipt "${distributed_trace_query_receipt}"'
        in script[observability_gate:gold_gate]
    )
    assert '--slo-evidence-receipt "${slo_evidence_receipt}"' in script[gold_gate:]
    assert '--expected-release-sha "${dr_release_commit_sha}"' in script[gold_gate:]
    assert '--expected-image-digest "${dr_release_image_digest}"' in script[gold_gate:]
    assert slo_gate < gold_gate


def test_production_deploy_holds_ingress_until_canonical_gold_then_consumes_signed_drain_receipt() -> None:
    script = _read("scripts/deploy_propertyquarry.sh")
    assert "PROPERTYQUARRY_UPSTREAM_DRAINED_ACKNOWLEDGEMENT" not in script
    assert "scripts/propertyquarry_deploy_drain_receipt.py verify" not in script
    assert "scripts/propertyquarry_deploy_drain_receipt.py consume" not in script
    assert "scripts/propertyquarry_deploy_controller_guard.py" not in script
    assert "PROPERTYQUARRY_DRAIN_RECEIPT_ED25519_PUBLIC_KEY" not in script
    assert "PROPERTYQUARRY_DEPLOY_DRAIN_CONSUMPTION_LEDGER" not in script
    assert "/usr/libexec/propertyquarry-release-control/propertyquarry-deploy-controller" in script
    assert "/etc/propertyquarry/release-control/deploy-drain-keyring.v2.json" in script
    assert "/etc/propertyquarry/monitoring-topology.v1.json" in script
    assert "/etc/propertyquarry/monitoring-tools.v1.json" in script
    assert "--controller-owns-all-privileged-actions" in script
    assert "--contain-before-candidate-validation" in script
    assert "--require-root-pinned-monitoring-runtime" in script
    assert "--require-cloudflared-immutable-digest-and-config-binding" in script
    assert "--forbid-caller-compose" in script
    assert "--forbid-candidate-output-authority" in script
    assert "docker compose" not in script.lower()
    assert "docker-compose" not in script.lower()


def test_slo_release_operator_environment_and_runbook_are_source_controlled() -> None:
    env_example = _read(".env.example")
    runbook = _read("docs/PROPERTYQUARRY_SLO_RELEASE_EVIDENCE.md")
    observability = _read("docs/PROPERTYQUARRY_OBSERVABILITY.md")
    checklist = _read("RELEASE_CHECKLIST.md")

    for key in (
        "PROPERTYQUARRY_DEPLOY_DRAIN_RECEIPT",
        "PROPERTYQUARRY_DEPLOY_PROMOTION_RECEIPT",
        "PROPERTYQUARRY_DEPLOY_TARGET_ID",
        "PROPERTYQUARRY_DEPLOY_ACTOR_ID",
        "PROPERTYQUARRY_EXPECTED_API_REPLICAS",
        "PROPERTYQUARRY_SLO_CAPTURE_PRINCIPAL_ID",
        "PROPERTYQUARRY_SLO_CAPTURE_TIMEOUT_SECONDS",
        "PROPERTYQUARRY_REQUIRE_SLO_RELEASE_EVIDENCE",
        "PROPERTYQUARRY_SLO_METRICS_SNAPSHOT",
        "PROPERTYQUARRY_SLO_METRICS_PROBE_RECEIPT",
        "PROPERTYQUARRY_SLO_EVIDENCE_RECEIPT",
        "PROPERTYQUARRY_MONITORING_RUNTIME_RECEIPT",
        "PROPERTYQUARRY_PROMETHEUS_RANGE_RECEIPT",
        "PROPERTYQUARRY_PROMETHEUS_RANGE_RESPONSE",
        "PROPERTYQUARRY_ALERT_DELIVERY_RECEIPT",
        "PROPERTYQUARRY_DASHBOARD_RENDER_RECEIPT",
        "PROPERTYQUARRY_STRUCTURED_LOG_QUERY_RECEIPT",
        "PROPERTYQUARRY_DISTRIBUTED_TRACE_QUERY_RECEIPT",
    ):
        assert key in env_example
        assert key in runbook or key in checklist
    assert "scripts/propertyquarry_slo_capture.py" in runbook
    assert "never put the token" in runbook
    assert "command line." in runbook
    assert "single-use" in runbook
    assert "/usr/libexec/propertyquarry-release-control/propertyquarry-deploy-controller" in runbook
    assert "/etc/propertyquarry/release-control/deploy-drain-keyring.v2.json" in runbook
    assert "monotonic" in runbook
    assert "UNCONFIGURED" in runbook
    assert "propertyquarry-cloudflared" in runbook
    assert "no more than 15 minutes old" in observability
    assert "credential_persisted: false" in observability
    assert "scripts/propertyquarry_slo_capture.py" in checklist
