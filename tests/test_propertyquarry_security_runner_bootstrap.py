from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github/workflows/propertyquarry-security-runner-bootstrap.yml"
SMOKE_WORKFLOW_PATH = ROOT / ".github/workflows/smoke-runtime.yml"
BOOTSTRAP_PATH = ROOT / "scripts/propertyquarry_security_runner_bootstrap.sh"
PREFLIGHT_PATH = ROOT / "scripts/propertyquarry_security_runner_preflight.sh"
RUNNER_LOCK_PATH = ROOT / "config/propertyquarry_security_runner_requirements.lock"
README_PATH = ROOT / "README.md"
RELEASE_CHECKLIST_PATH = ROOT / "RELEASE_CHECKLIST.md"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _workflow() -> dict[str, object]:
    payload = yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(payload, dict)
    return payload


def test_bootstrap_is_manual_main_only_and_uses_the_protected_hosted_lane() -> None:
    payload = _workflow()
    assert payload["on"] == {
        "workflow_dispatch": {
            "inputs": {
                "security_run_id": {
                    "description": (
                        "Exact pre-approved smoke-runtime workflow_dispatch run waiting "
                        "for the flagship security runner"
                    ),
                    "required": "true",
                    "type": "string",
                },
                "security_runner_label": {
                    "description": "Exact one-time pqsec label supplied to the pre-approved security run",
                    "required": "true",
                    "type": "string",
                },
                "security_runner_token_expires_at": {
                    "description": (
                        "Expiration returned with the just-in-time operator-minted registration token"
                    ),
                    "required": "true",
                    "type": "string",
                }
            }
        }
    }
    assert payload["permissions"] == {
        "actions": "read",
        "contents": "read",
        "packages": "read",
    }
    assert payload["concurrency"] == {
        "group": "propertyquarry-security-runner-bootstrap",
        "cancel-in-progress": "false",
    }

    job = payload["jobs"]["bootstrap"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == "330"
    assert job["environment"] == {"name": "propertyquarry-production"}
    assert "refs/heads/main" in job["if"]
    assert (
        "github.event.repository.full_name == 'ArchonMegalon/propertyquarry'"
        in job["if"]
    )
    assert (
        "github.event.repository.full_name == 'ArchonMegalon/property'"
        not in job["if"]
    )


def test_bootstrap_validates_exact_queued_job_before_any_repo_source_runs() -> None:
    payload = _workflow()
    steps = payload["jobs"]["bootstrap"]["steps"]
    assert steps[0]["name"] == "Validate the exact queued flagship security job"
    validation = steps[0]["run"]
    for expected in (
        '.event == "workflow_dispatch"',
        '.head_branch == "main"',
        '.head_sha == $head_sha',
        '.path == ".github/workflows/smoke-runtime.yml"',
        '.name == "propertyquarry-flagship-security"',
        '.status == "queued"',
        'index("self-hosted")',
        'index("propertyquarry-security")',
        "index($runner_label)",
        "^pqsec-[0-9a-f]{32}$",
        '[[ "${SECURITY_RUNNER_LABEL}" == "${APPROVED_SECURITY_RUNNER_LABEL}" ]]',
    ):
        assert expected in validation

    assert steps[0]["env"]["APPROVED_SECURITY_RUNNER_LABEL"] == (
        "${{ vars.PROPERTYQUARRY_SECURITY_RUNNER_LABEL }}"
    )
    assert '--arg runner_label "${APPROVED_SECURITY_RUNNER_LABEL}"' in validation
    assert (
        'write_command_scalar "${GITHUB_ENV}" PQ_SECURITY_RUNNER_LABEL '
        '"${APPROVED_SECURITY_RUNNER_LABEL}"'
        in validation
    )

    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "actions/checkout@" not in workflow_text
    assert "?ref=${GITHUB_SHA}" in workflow_text
    assert workflow_text.index("Validate the exact queued flagship security job") < workflow_text.index(
        "Materialize exact commit-bound bootstrap sources"
    )
    assert workflow_text.count("PROPERTYQUARRY_SECURITY_RUNNER_TOKEN") == 1


def test_registration_token_must_be_operator_minted_just_in_time() -> None:
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "PROPERTYQUARRY_SECURITY_RUNNER_TOKEN" in workflow_text
    assert "security_runner_token_expires_at" in workflow_text
    assert "date -u -d \"${PQ_RUNNER_TOKEN_EXPIRES_AT" in workflow_text
    assert "registration_remaining_seconds >= 3000" in workflow_text
    assert "registration_remaining_seconds <= 3700" in workflow_text
    assert "::add-mask::%s" in workflow_text
    assert "PROPERTYQUARRY_SECURITY_RUNNER_ADMIN_TOKEN" not in workflow_text


def test_bootstrap_binds_the_standalone_repository_and_image_namespaces() -> None:
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    script = BOOTSTRAP_PATH.read_text(encoding="utf-8")

    assert '"ArchonMegalon/propertyquarry"' in workflow_text
    assert '"ArchonMegalon/property"' not in workflow_text
    assert '"ArchonMegalon/propertyquarry"' in script
    assert '"ArchonMegalon/property"' not in script
    assert (
        "ArchonMegalon/propertyquarry/.github/workflows/smoke-runtime.yml@refs/heads/main"
        in script
    )
    assert "propertyquarry-standalone-web-runtime@sha256" in script
    assert "propertyquarry-standalone-render-runtime@sha256" in script
    assert "propertyquarry-web-runtime@sha256" not in script
    assert "propertyquarry-render-runtime@sha256" not in script


def test_downloaded_sources_are_bound_to_reviewed_hashes() -> None:
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert _sha256(BOOTSTRAP_PATH) == "4c6543ab71940154c12b7e750d0af1daa92be46a472a3b5ddd3efd64830edbad"
    assert _sha256(PREFLIGHT_PATH) == "1450cd2506ed02edd5445861678aa80da92f5535161cdfa569d34622311796e3"
    assert _sha256(RUNNER_LOCK_PATH) == "e968dda8c1dee309698cf05e42932f786397e954ac034c4f90a0be0db32844fd"
    for identity in (_sha256(BOOTSTRAP_PATH), _sha256(PREFLIGHT_PATH), _sha256(RUNNER_LOCK_PATH)):
        assert identity in workflow_text
    assert "sha256sum --check --strict" in workflow_text


def test_only_commit_pinned_actions_execute() -> None:
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    uses = re.findall(r"^\s*uses:\s*([^\s#]+)", workflow_text, flags=re.MULTILINE)
    assert uses == [
        "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065",
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
    ]
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", item) for item in uses)


def test_rootless_runtime_bundle_is_exact_and_does_not_weaken_the_host() -> None:
    script = BOOTSTRAP_PATH.read_text(encoding="utf-8")
    for expected in (
        'EXPECTED_IMAGE_VERSION="20260714.240.1"',
        'EXPECTED_KERNEL="6.17.0-1020-azure"',
        'EXPECTED_DOCKER_VERSION="28.0.4"',
        'ROOTLESS_EXTRAS_SHA256="2abb177d60561ac77b50a42b60500ab194b70f40f4b225d837c1fdccaaab7a28"',
        'APPARMOR_SHA256="45c30f4a9724a21e2f5f91a0556f979c13ab2042e6a38c7fdd6da87829e8d67e"',
        'DBUS_USER_SHA256="e585b1694b854c3b75bfb39cc4022cafe7b14e44fd435433b613b8fb9919cb41"',
        'UIDMAP_SHA256="a80cb7f72dd18c73cbb0b07b7fbe855504f26bfafae072a9b3d125c89d499b9e"',
        'SLIRP_SHA256="3fc72a72a376a3ad3b439434bc87d89d245f9d54a1d540e8a06b74d4e2385e0a"',
        "apparmor_parser --replace /etc/apparmor.d/rootlesskit",
        "kernel.apparmor_restrict_unprivileged_userns",
        "verify_subid /etc/subuid",
        "verify_subid /etc/subgid",
        'chown root:"${PQ_USER}" "${INSTALL_ROOT}"',
        'chmod 750 "${INSTALL_ROOT}"',
        "security install root posture mismatch",
        "unprivileged_userns_clone",
        "max_user_namespaces",
        'as_pq rootlesskit true || fail "RootlessKit namespace smoke check failed"',
        "dockerd-rootless-setuptool.sh install --force",
        "10-propertyquarry.conf",
        "Environment=XDG_CONFIG_HOME=${PQ_HOME}/.config",
        "Environment=XDG_DATA_HOME=${PQ_HOME}/.local/share",
        "Environment=XDG_STATE_HOME=${PQ_HOME}/.local/state",
        "Environment=XDG_CACHE_HOME=${PQ_HOME}/.cache",
        "UnsetEnvironment=DOCKER_CONFIG DOCKER_CONTEXT DOCKER_HOST DOCKER_IGNORE_BR_NETFILTER_ERROR",
        "DOCKERD_ROOTLESS_ROOTLESSKIT_DISABLE_HOST_LOOPBACK=true",
        '\"bridge\": \"none\"',
        '\"ip-forward\": false',
        '\"ip-masq\": false',
        '\"icc\": false',
        'dockerd --validate --config-file "${PQ_HOME}/.config/docker/daemon.json"',
        "capture_rootless_diagnostics",
        "[socket-posture]",
        "security user runtime directory posture mismatch",
        "rootless Docker socket owner UID mismatch",
        "rootless Docker socket group is outside the security user mapping",
        "600|660|1600|1660",
        "outer runner user can reach the isolated rootless Docker socket",
        "systemctl --user --no-pager --full status docker.service",
        "journalctl --user --unit docker.service",
        "rootless Docker user service failed to start; diagnostic receipt preserved",
        '.SecurityOptions | any(contains("name=rootless"))',
        '.Driver == "overlay2"',
        '.CgroupDriver == "systemd"',
        "DOCKER_HOST=unix:///var/run/docker.sock docker info",
    ):
        assert expected in script

    for forbidden in (
        "apt-get",
        "curl | sh",
        "--privileged",
        "chmod 666",
        "chmod 777",
        'chmod 755 "${INSTALL_ROOT}"',
        "usermod -aG docker",
        "kernel.apparmor_restrict_unprivileged_userns=0",
        "sysctl -w",
        'Environment=DOCKER_IGNORE_BR_NETFILTER_ERROR=',
        'export DOCKER_IGNORE_BR_NETFILTER_ERROR=',
        '\"iptables\": false',
        '\"ip6tables\": false',
        "stat -Lc '%U:%G'",
    ):
        assert forbidden not in script
    assert script.count("DOCKER_IGNORE_BR_NETFILTER_ERROR") == 1
    assert "\n    --replace" not in script


def test_runner_and_scanners_are_ephemeral_offline_and_hash_bound() -> None:
    script = BOOTSTRAP_PATH.read_text(encoding="utf-8")
    for expected in (
        'RUNNER_ARCHIVE_SHA256="4ef2f25285f0ae4477f1fe1e346db76d2f3ebf03824e2ddd1973a2819bf6c8cf"',
        'SYFT_ARCHIVE_SHA256="6cef9a7f37220d9067eaf9cfaaa2fce986e9f320a8d42cbc36658c99af78ea04"',
        'SYFT_BINARY_SHA256="fd260522b9695350ee23483c88b803e96ffe9f8f3954106a7bcad7940a1ade89"',
        'TRIVY_ARCHIVE_SHA256="bbb64b9695866ce4a7a8f5c9592002c5961cab378577fa3f8a040df362b9b2ea"',
        'TRIVY_BINARY_SHA256="0e69edd134a3c338baa1a6806920773615d682b18cbc6a0cba2a3b658ef9b63e"',
        "--require-hashes --only-binary=:all:",
        'chmod -R a+rX,go-w "${INSTALL_ROOT}/pip-audit"',
        "pip-audit venv writability audit failed",
        "pip-audit venv contains a group- or world-writable path",
        "security user cannot execute the hash-locked pip-audit environment",
        "image --download-db-only",
        "image --download-java-db-only",
        "--ephemeral",
        "--disableupdate",
        "ACTIONS_RUNNER_HOOK_JOB_STARTED=",
        "PROPERTYQUARRY_WORKFLOW_HEAD_SHA=${PQ_EXPECTED_HEAD_SHA}",
        "PROPERTYQUARRY_SECURITY_RUNNER_LABEL=${PQ_SECURITY_RUNNER_LABEL}",
        "PROPERTYQUARRY_WEB_IMAGE=${PQ_WEB_IMAGE}",
        "PROPERTYQUARRY_RENDER_IMAGE=${PQ_RENDER_IMAGE}",
        "runuser -u \"${PQ_USER}\" -- env -i",
        '--labels "propertyquarry-security,${PQ_SECURITY_RUNNER_LABEL}"',
        'chmod -R a+rX,go-w "${RUNNER_ROOT}/bin" "${RUNNER_ROOT}/externals"',
        "Actions runner runtime contains a group- or world-writable path",
        "Actions runner Node 24 runtime posture mismatch",
        "security user cannot execute the Actions runner Node 24 runtime",
        'PATH="$(<"${RUNNER_ROOT}/.path")"',
        '"EXPECTED_SYSCTL_BIN=/usr/sbin/sysctl"',
        '"EXPECTED_SYSCTL_SHA256=$(sha256_file /usr/sbin/sysctl)"',
        '|| LISTENER_EXIT_CODE="$?"',
        '[[ "${LISTENER_EXIT_CODE}" == "2" ]]',
        "listener_exit_code:$listener_exit_code",
        "pinned runner exited outside the immutable local-config cleanup boundary",
        '"${RUNNER_SETTINGS_SHA256}" "root:${PQ_USER}:440"',
        '"${RUNNER_CREDENTIALS_SHA256}" "root:${PQ_USER}:440"',
        '"${RUNNER_RSA_SHA256}" "root:${PQ_USER}:440"',
        'TRIVY_CACHE_BACKEND=memory',
        'operational_cache="${SNAPSHOT_ROOT}"',
        'post-job-integrity.json',
        'BOOTSTRAP_STATUS="listener_exited"',
    ):
        assert expected in script
    assert "\n    --replace" not in script
    assert 'BOOTSTRAP_STATUS="pass"' not in script
    assert "runner-diag" not in script
    assert 'chmod -R go-w "${INSTALL_ROOT}/pip-audit"' not in script
    assert 'chmod -R go-w "${RUNNER_ROOT}/bin"' not in script
    assert (
        'pip/_internal/__init__.py")" == "root:root:755"'
        in script
    )
    assert script.index(
        'as_pq "${INSTALL_ROOT}/pip-audit/bin/pip-audit" --version'
    ) < script.index('"${RUNNER_ROOT}/config.sh"')


def test_preflight_rejects_wrong_identity_credentials_or_runtime_state() -> None:
    preflight = PREFLIGHT_PATH.read_text(encoding="utf-8")
    for expected in (
        "GITHUB_REPOSITORY",
        "GITHUB_EVENT_NAME",
        "GITHUB_WORKFLOW_REF",
        "GITHUB_WORKFLOW_SHA",
        "GITHUB_JOB",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_SHA",
        "PROPERTYQUARRY_WORKFLOW_HEAD_SHA",
        "PROPERTYQUARRY_SECURITY_RUNNER_LABEL",
        "PROPERTYQUARRY_WEB_IMAGE",
        "PROPERTYQUARRY_RENDER_IMAGE",
        "GH_TOKEN GITHUB_TOKEN PROPERTYQUARRY_SECURITY_RUNNER_TOKEN PQ_RUNNER_TOKEN PQ_RUNNER_TOKEN_EXPIRES_AT",
        "Trivy cache backend is not memory-only",
        "Trivy snapshot root ownership or mode changed",
        "required governed file is missing",
        '"${PATH:-}" == "${governed_scanner_path}"',
        '"$(command -v pip-audit)" == "${EXPECTED_PIP_AUDIT_BIN}"',
        '"$(command -v syft)" == "${EXPECTED_SYFT_BIN}"',
        '"$(command -v trivy)" == "${EXPECTED_TRIVY_BIN}"',
        'verify_file "${EXPECTED_SYSCTL_BIN}" "${EXPECTED_SYSCTL_SHA256}" "root:root:755"',
        '"${EXPECTED_SYSCTL_BIN}" -n kernel.apparmor_restrict_unprivileged_userns',
        "Trivy database snapshot is stale",
        "rootless Docker daemon posture changed",
        "exact image digest is not local",
        "security install root posture changed",
        "rootless Docker socket owner UID changed",
        "rootless Docker socket group is outside the security user mapping",
        "600|660|1600|1660",
    ):
        assert expected in preflight
    assert "sudo -n true" in preflight
    assert "-r /var/run/docker.sock" in preflight
    assert '"root:root:4755"' in preflight
    assert 'stat -Lc \'%U:%G\' "${EXPECTED_DOCKER_SOCKET}"' not in preflight
    assert '"${INSTALL_ROOT}/pip-audit/bin:${SCANNER_ROOT}:/usr/bin:/bin"' in BOOTSTRAP_PATH.read_text(
        encoding="utf-8"
    )


def test_runner_lock_hashes_every_exact_dependency() -> None:
    lock_text = RUNNER_LOCK_PATH.read_text(encoding="utf-8")
    assert "pip-audit==2.10.1" in lock_text
    assert "pip==26.1.2" in lock_text
    blocks = re.split(r"\n(?=[a-z0-9][a-z0-9._-]*==)", lock_text)
    requirement_blocks = [block for block in blocks if re.match(r"^[a-z0-9][a-z0-9._-]*==", block)]
    assert requirement_blocks
    assert all("--hash=sha256:" in block for block in requirement_blocks)


def test_existing_flagship_security_job_remains_fixed_and_offline() -> None:
    smoke = SMOKE_WORKFLOW_PATH.read_text(encoding="utf-8")
    start = smoke.index("  propertyquarry-flagship-security:")
    end = smoke.index("\n  propertyquarry-security-bootstrap-attestation:", start)
    job = smoke[start:end]
    validated_label = (
        "${{ needs['propertyquarry-protected-dispatch-inputs'].outputs."
        "security_runner_label }}"
    )
    assert f'runs-on: [self-hosted, propertyquarry-security, "{validated_label}"]' in job
    assert f"PROPERTYQUARRY_SECURITY_RUNNER_LABEL: {validated_label}" in job
    assert 'runs-on: [self-hosted, propertyquarry-security, "${{ inputs.security_runner_label }}"]' not in job
    assert "PROPERTYQUARRY_SECURITY_RUNNER_LABEL: ${{ inputs.security_runner_label }}" not in job
    assert "startsWith(inputs.security_runner_label, 'pqsec-')" in job
    assert "contents: read" in job
    assert "persist-credentials: false" in job
    assert "scripts/propertyquarry_release_security_gate.py" in job
    assert "pip install" not in job
    assert "docker pull" not in job
    assert "download-db-only" not in job


def test_release_authority_jobs_bind_the_standalone_repository() -> None:
    smoke = yaml.load(
        SMOKE_WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader
    )

    for job_name in (
        "propertyquarry-live-release-gates",
        "propertyquarry-launch-gold",
    ):
        assert smoke["jobs"][job_name]["env"][
            "PROPERTYQUARRY_EXPECTED_RELEASE_REPOSITORY"
        ] == "ArchonMegalon/propertyquarry"


def test_hosted_preflight_is_the_only_self_hosted_runner_label_authority() -> None:
    payload = yaml.load(
        SMOKE_WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader
    )
    preflight = payload["jobs"]["propertyquarry-protected-dispatch-inputs"]
    flagship = payload["jobs"]["propertyquarry-flagship-security"]
    validated_label = (
        "${{ needs['propertyquarry-protected-dispatch-inputs'].outputs."
        "security_runner_label }}"
    )

    assert preflight["environment"] == {"name": "propertyquarry-production"}
    assert preflight["outputs"] == {
        "release_runner_label": "${{ steps.validate.outputs.release_runner_label }}",
        "release_runner_ticket_sha256": (
            "${{ steps.validate.outputs.release_runner_ticket_sha256 }}"
        ),
        "security_runner_label": "${{ steps.validate.outputs.security_runner_label }}",
        "security_runner_token_expires_at": (
            "${{ steps.validate.outputs.security_runner_token_expires_at }}"
        ),
    }
    assert preflight["env"]["PROPERTYQUARRY_APPROVED_SECURITY_RUNNER_LABEL"] == (
        "${{ vars.PROPERTYQUARRY_SECURITY_RUNNER_LABEL }}"
    )
    assert flagship["runs-on"] == [
        "self-hosted",
        "propertyquarry-security",
        validated_label,
    ]
    assert flagship["env"]["PROPERTYQUARRY_SECURITY_RUNNER_LABEL"] == validated_label
    assert "startsWith(inputs.security_runner_label, 'pqsec-')" in flagship["if"]


def test_bootstrap_consumption_is_required_by_the_active_release_graph() -> None:
    smoke = yaml.load(
        SMOKE_WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader
    )
    attestation = smoke["jobs"]["propertyquarry-security-bootstrap-attestation"]
    release = smoke["jobs"]["propertyquarry-release-v2"]

    assert attestation["runs-on"] == "ubuntu-24.04"
    assert "propertyquarry-flagship-security" in attestation["needs"]
    assert "propertyquarry-security-bootstrap-attestation" in release["needs"]
    assert (
        "needs['propertyquarry-security-bootstrap-attestation'].result == 'success'"
        in release["if"]
    )


def test_evidence_upload_and_exact_consumption_check_are_mandatory() -> None:
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "Verify the exact flagship security job consumed the runner" in workflow_text
    assert '.conclusion == "success"' in workflow_text
    assert ".runner_name == $runner_name" in workflow_text
    assert "index($runner_label)" in workflow_text
    assert "propertyquarry.security_runner_post_job_integrity.v1" in BOOTSTRAP_PATH.read_text(
        encoding="utf-8"
    )
    assert "if-no-files-found: error" in workflow_text
    assert "retention-days: 30" in workflow_text
