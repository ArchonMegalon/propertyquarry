from __future__ import annotations

import hashlib
import json
import os
import signal
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from scripts import propertyquarry_deploy_controller_guard as guard


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


@pytest.fixture
def configured_controller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    secure_root = tmp_path / "secure-root"
    tracked_root = secure_root / "tracked"
    external_root = secure_root / "etc" / "propertyquarry" / "release-control"
    controller_root = secure_root / "usr" / "libexec" / "propertyquarry-release-control"
    lock_root = secure_root / "var" / "lock" / "propertyquarry"
    for directory in (tracked_root, external_root, controller_root, lock_root):
        directory.mkdir(parents=True, mode=0o700)
        directory.chmod(0o700)
    controller = controller_root / "propertyquarry-deploy-controller"
    controller.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    controller.chmod(0o555)
    controller_sha = hashlib.sha256(controller.read_bytes()).hexdigest()
    manifest = {
        "schema": guard.SCHEMA,
        "status": "active",
        "controller_path": str(controller),
        "controller_sha256": controller_sha,
        "protocol_version": 1,
        "monotonic_authority_id": "release-control-test",
        "minimum_monotonic_generation": 17,
    }
    tracked = tracked_root / "controller.json"
    external = external_root / "controller.json"
    tracked.write_bytes(_canonical(manifest))
    external.write_bytes(_canonical(manifest))
    external.chmod(0o444)
    monkeypatch.setattr(guard, "SECURE_PATH_ROOT", secure_root)
    monkeypatch.setattr(guard, "TRACKED_MANIFEST_PATH", tracked)
    monkeypatch.setattr(guard, "EXTERNAL_MANIFEST_PATH", external)
    monkeypatch.setattr(guard, "CONTROLLER_PATH", controller)
    monkeypatch.setattr(guard, "CONTROLLER_LOCK_PATH", lock_root / "deploy-controller.lock")
    monkeypatch.setattr(guard, "REQUIRED_UID", os.getuid())
    monkeypatch.setattr(guard, "MANIFEST_STATUS", "active")
    monkeypatch.setattr(guard, "MANIFEST_SHA256", hashlib.sha256(_canonical(manifest)).hexdigest())
    monkeypatch.setattr(guard, "CONTROLLER_SHA256", controller_sha)
    monkeypatch.setattr(guard, "MONOTONIC_AUTHORITY_ID", "release-control-test")
    monkeypatch.setattr(guard, "MINIMUM_MONOTONIC_GENERATION", 17)
    for name in guard.FORBIDDEN_SELECTOR_ENV:
        monkeypatch.delenv(name, raising=False)
    return controller, external


def test_repository_controller_manifest_is_unconfigured_and_fails_closed() -> None:
    payload = json.loads(guard.TRACKED_MANIFEST_PATH.read_text(encoding="utf-8"))

    assert payload["status"] == "UNCONFIGURED"
    assert payload["controller_sha256"] == "UNCONFIGURED"
    with pytest.raises(guard.ControllerGuardError, match="UNCONFIGURED"):
        guard.validate_external_controller()


def test_repository_database_fence_policy_is_explicitly_unconfigured_and_strict() -> None:
    root = Path(__file__).resolve().parents[1]
    payload = json.loads(
        (
            root
            / "config"
            / "release"
            / "propertyquarry_database_fence_policy.v1.json"
        ).read_text(encoding="utf-8")
    )

    assert payload["status"] == "UNCONFIGURED"
    assert "postgres" in payload["target"]["forbidden_database_names"]
    assert payload["roles"]["require_non_superuser"] is True
    assert payload["roles"]["forbid_runtime_object_ownership"] is True
    assert payload["fence"]["runtime_roles_nologin_during_maintenance"] is True
    assert payload["fence"]["require_stable_zero_backends"] is True
    assert payload["fence"]["max_prepared_transactions"] == 0
    assert payload["runtime"]["restart_policy"] == "controller-only"
    assert "migrator" in payload["secrets"]["application_forbidden_credentials"]


def test_legacy_guard_can_attest_but_cannot_invoke_controller(
    configured_controller: tuple[Path, Path],
) -> None:
    controller, _ = configured_controller

    manifest = guard.validate_external_controller()
    assert manifest["controller_path"] == str(controller)
    with pytest.raises(guard.ControllerGuardError, match="candidate Python controller invocation is disabled"):
        guard.invoke_controller("attest", ["--require-external-monotonic-cas"])


def test_environment_cannot_select_controller_path_or_hash(
    configured_controller: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_DEPLOY_CONTROLLER_PATH", "/tmp/exit-zero")

    with pytest.raises(guard.ControllerGuardError, match="caller-selected"):
        guard.validate_external_controller()


def test_manifest_or_controller_replacement_is_rejected(
    configured_controller: tuple[Path, Path],
) -> None:
    controller, external = configured_controller
    controller.chmod(0o755)
    controller.write_text("#!/bin/sh\nexit 0 # replaced\n", encoding="utf-8")
    controller.chmod(0o555)
    with pytest.raises(guard.ControllerGuardError, match="hash pin"):
        guard.validate_external_controller()

    controller.chmod(0o755)
    controller.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    controller.chmod(0o555)
    external.chmod(0o644)
    with pytest.raises(guard.ControllerGuardError, match="mode 0444"):
        guard.validate_external_controller()


@pytest.fixture
def external_handoff(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Build a real-process fixture without adding a production test seam."""

    root = Path(__file__).resolve().parents[1]
    authority_root = tmp_path / "authority"
    controller = (
        authority_root
        / "usr"
        / "libexec"
        / "propertyquarry-release-control"
        / "propertyquarry-deploy-controller"
    )
    manifest = (
        authority_root
        / "etc"
        / "propertyquarry"
        / "release-control"
        / "external-deploy-controller.v1.json"
    )
    controller_pin = manifest.with_name("external-deploy-controller.sha256")
    request = authority_root / "run" / "release-control" / "deploy-request.json"
    candidate = authority_root / "candidate"
    script = candidate / "scripts" / "deploy_propertyquarry.sh"
    for parent in (controller.parent, manifest.parent, request.parent, script.parent):
        parent.mkdir(parents=True, exist_ok=True)
    for path in authority_root.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)
    authority_root.chmod(0o755)

    shutil.copyfile("/usr/bin/echo", controller)
    controller.chmod(0o555)
    controller_sha = hashlib.sha256(controller.read_bytes()).hexdigest()
    manifest.write_bytes(
        _canonical(
            {
                "schema": "propertyquarry.external-deploy-controller.v1",
                "status": "active",
                "controller_path": str(controller),
                "controller_sha256": controller_sha,
                "protocol_version": 1,
                "monotonic_authority_id": "host-authority-test",
                "minimum_monotonic_generation": 1,
            }
        )
    )
    manifest.chmod(0o444)
    controller_pin.write_text(f"{controller_sha}\n", encoding="ascii")
    controller_pin.chmod(0o444)
    request.write_bytes(_canonical({"schema": "test.signed-request.v1"}))
    request.chmod(0o400)

    source = (root / "scripts" / "deploy_propertyquarry.sh").read_text(
        encoding="utf-8"
    )
    source = source.replace(
        "PROPERTYQUARRY_RELEASE_CONTROL_UID=0",
        f"PROPERTYQUARRY_RELEASE_CONTROL_UID={os.getuid()}",
    ).replace(
        "PROPERTYQUARRY_RELEASE_CONTROL_GID=0",
        f"PROPERTYQUARRY_RELEASE_CONTROL_GID={os.getgid()}",
    ).replace(
        'PROPERTYQUARRY_EXTERNAL_CONTROLLER_PATH="/usr/libexec/propertyquarry-release-control/propertyquarry-deploy-controller"',
        f'PROPERTYQUARRY_EXTERNAL_CONTROLLER_PATH="{controller}"',
    ).replace(
        'PROPERTYQUARRY_EXTERNAL_CONTROLLER_MANIFEST="/etc/propertyquarry/release-control/external-deploy-controller.v1.json"',
        f'PROPERTYQUARRY_EXTERNAL_CONTROLLER_MANIFEST="{manifest}"',
    ).replace(
        'PROPERTYQUARRY_EXTERNAL_CONTROLLER_PIN="/etc/propertyquarry/release-control/external-deploy-controller.sha256"',
        f'PROPERTYQUARRY_EXTERNAL_CONTROLLER_PIN="{controller_pin}"',
    ).replace(
        '[[ "${current}" == "/" ]] && return 0',
        f'[[ "${{current}}" == "{authority_root}" ]] && return 0',
    ).replace(
        "/var/run/docker.sock",
        str(authority_root / "no-docker-authority.sock"),
    )
    if os.geteuid() == 0:
        source = source.replace("if (( EUID == 0 )); then", "if (( EUID == -1 )); then")
    script.write_text(source, encoding="utf-8")
    script.chmod(0o555)
    return script, controller, request, candidate


def _run_external_handoff(
    fixture: tuple[Path, Path, Path, Path],
    *,
    mode: str = "prod",
    preflight: bool = False,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    script, _, request, _ = fixture
    env = {
        "EA_RUNTIME_MODE": mode,
        "PROPERTYQUARRY_DEPLOY_SIGNED_REQUEST": str(request),
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    }
    env.update(extra_env or {})
    command = [str(script)]
    if preflight:
        command.append("--preflight-only")
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def test_candidate_checkout_cannot_authorize_or_execute_deploy_actions(
    external_handoff: tuple[Path, Path, Path, Path],
) -> None:
    script, controller, _, candidate = external_handoff
    action_log = candidate / "candidate-action.log"
    fake = candidate / "fake-exit-zero"
    fake.write_text(
        f"#!/bin/sh\nprintf '%s\\n' invoked >> '{action_log}'\nexit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    for name in (
        "propertyquarry_deploy_controller_guard.py",
        "propertyquarry_deploy_drain_receipt.py",
        "bash",
        "dirname",
        "pwd",
        "env",
        "docker",
        "docker-compose",
        "git",
    ):
        shutil.copyfile(fake, script.parent / name)
        (script.parent / name).chmod(0o755)

    # A hostile checkout can replace every old verifier with exit zero, but it
    # cannot update the authority-owned manifest after replacing the controller.
    controller.chmod(0o755)
    controller.write_bytes(controller.read_bytes() + b"candidate tamper")
    controller.chmod(0o555)
    result = _run_external_handoff(
        external_handoff,
        extra_env={
            "PATH": f"{script.parent}:/usr/sbin:/usr/bin:/sbin:/bin",
            "PROPERTYQUARRY_DEPLOY_PYTHON_BIN": str(fake),
            "PROPERTYQUARRY_COMPOSE_FILE": str(fake),
            "PROPERTYQUARRY_DEPLOY_CONTROLLER_PATH": str(fake),
        },
    )

    assert result.returncode == 2
    assert "does not match its root-owned digest pin" in result.stderr
    assert not action_log.exists()


def test_controller_path_swap_after_fd_open_fails_closed_without_reopen(
    external_handoff: tuple[Path, Path, Path, Path],
) -> None:
    script, controller, request, _ = external_handoff
    pin = controller.parents[3] / "etc" / "propertyquarry" / "release-control" / "external-deploy-controller.sha256"
    controller.chmod(0o755)
    controller.write_bytes(controller.read_bytes() + (b"\0" * (32 * 1024 * 1024)))
    controller.chmod(0o555)
    original = controller.stat()
    pin.chmod(0o644)
    pin.write_text(f"{hashlib.sha256(controller.read_bytes()).hexdigest()}\n", encoding="ascii")
    pin.chmod(0o444)
    env = {
        "EA_RUNTIME_MODE": "prod",
        "PROPERTYQUARRY_DEPLOY_SIGNED_REQUEST": str(request),
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    }
    process = subprocess.Popen(
        [str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    hashing_verified_controller = False
    wrapper_stopped = False
    deadline = time.monotonic() + 5
    while process.poll() is None and time.monotonic() < deadline:
        try:
            child_pids = (
                Path(f"/proc/{process.pid}/task/{process.pid}/children")
                .read_text(encoding="ascii")
                .split()
            )
        except OSError:
            child_pids = []
        for child_pid in child_pids:
            try:
                executable = os.readlink(f"/proc/{child_pid}/exe")
            except OSError:
                continue
            if Path(executable).name != "sha256sum":
                continue
            for descriptor in range(0, 64):
                try:
                    observed = os.stat(f"/proc/{child_pid}/fd/{descriptor}")
                except OSError:
                    continue
                if (observed.st_dev, observed.st_ino) == (
                    original.st_dev,
                    original.st_ino,
                ):
                    hashing_verified_controller = True
                    break
            if hashing_verified_controller:
                break
        if hashing_verified_controller:
            os.kill(process.pid, signal.SIGSTOP)
            wrapper_stopped = True
            break
        time.sleep(0.001)
    try:
        replacement = controller.with_name("replacement-controller")
        shutil.copyfile("/usr/bin/false", replacement)
        replacement.chmod(0o555)
        os.replace(replacement, controller)
    finally:
        if wrapper_stopped and process.poll() is None:
            os.kill(process.pid, signal.SIGCONT)
    stdout, stderr = process.communicate(timeout=10)

    assert hashing_verified_controller, (
        "wrapper never hashed its retained verified controller FD"
    )
    assert process.returncode == 2
    assert not stdout
    assert (
        "External handoff file changed while it was opened" in stderr
        or "identity changed after hashing" in stderr
    )


def test_production_handoff_records_requested_external_controller_contract(
    external_handoff: tuple[Path, Path, Path, Path],
) -> None:
    # /usr/bin/echo is only a native argv recorder. This proves the FD handoff
    # contract, not containment, fencing, evidence, or traffic semantics.
    result = _run_external_handoff(external_handoff)

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("deploy-run ")
    assert "--controller-owns-all-privileged-actions" in result.stdout
    assert "--contain-before-candidate-validation" in result.stdout
    assert "--forbid-caller-compose" in result.stdout
    assert "--forbid-candidate-output-authority" in result.stdout
    assert "--require-cloudflared-immutable-digest-and-config-binding" in result.stdout
    assert "--canonical-compose-plan /etc/propertyquarry/release-control/deploy-compose-plan.v1.json" in result.stdout


def test_external_controller_receives_only_the_documented_clean_environment(
    external_handoff: tuple[Path, Path, Path, Path],
) -> None:
    _, controller, _, candidate = external_handoff
    capture_path = candidate / "controller-environment.txt"
    source_path = candidate / "controller-environment.c"
    replacement = candidate / "controller-environment"
    source_path.write_text(
        "#include <stdio.h>\n"
        "extern char **environ;\n"
        "int main(void) {\n"
        f"  FILE *output = fopen({json.dumps(str(capture_path))}, \"w\");\n"
        "  char **entry;\n"
        "  if (output == NULL) return 70;\n"
        "  for (entry = environ; *entry != NULL; ++entry) {\n"
        "    if (fprintf(output, \"%s\\n\", *entry) < 0) return 71;\n"
        "  }\n"
        "  return fclose(output) == 0 ? 0 : 72;\n"
        "}\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["/usr/bin/cc", "-O2", "-o", str(replacement), str(source_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    replacement.chmod(0o555)
    controller.chmod(0o755)
    os.replace(replacement, controller)
    controller.chmod(0o555)
    controller_sha = hashlib.sha256(controller.read_bytes()).hexdigest()
    pin = (
        controller.parents[3]
        / "etc"
        / "propertyquarry"
        / "release-control"
        / "external-deploy-controller.sha256"
    )
    pin.chmod(0o644)
    pin.write_text(f"{controller_sha}\n", encoding="ascii")
    pin.chmod(0o444)

    result = _run_external_handoff(
        external_handoff,
        extra_env={
            "UNRELATED_PRIVATE_VALUE": "must-not-cross",
            "PROPERTYQUARRY_COMPOSE_FILE": "/candidate/ignored-compose.yml",
        },
    )

    assert result.returncode == 0, result.stderr
    assert sorted(capture_path.read_text(encoding="utf-8").splitlines()) == [
        "HOME=/nonexistent",
        "LANG=C",
        "PATH=/usr/sbin:/usr/bin:/sbin:/bin",
    ]


def test_nonproduction_handoff_records_database_identity_and_fence_requirements(
    external_handoff: tuple[Path, Path, Path, Path],
) -> None:
    result = _run_external_handoff(external_handoff, mode="development")

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("candidate-run ")
    assert "--require-server-derived-database-identity" in result.stdout
    assert "--require-signed-disposable-or-allowed-database-target" in result.stdout
    assert "--database-fence-policy /etc/propertyquarry/release-control/database-fence-policy.v1.json" in result.stdout

    rejected = _run_external_handoff(
        external_handoff,
        mode="development",
        extra_env={"DATABASE_URL": "postgresql://candidate@production.example/property"},
    )
    assert rejected.returncode == 2
    assert "credentials belong only to the installed controller" in rejected.stderr


def test_preflight_handoff_records_explicit_read_only_operation_request(
    external_handoff: tuple[Path, Path, Path, Path],
) -> None:
    result = _run_external_handoff(external_handoff, preflight=True)

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("deploy-preflight ")
    assert "--read-only" in result.stdout
    assert "--forbid-containment" in result.stdout
    assert "--forbid-state-mutation" in result.stdout
    assert "--require-explicit-preflight-disposition" in result.stdout
    assert "--contain-before-candidate-validation" not in result.stdout
    assert "--controller-owns-all-privileged-actions" not in result.stdout


def test_fixed_shebang_and_startup_environment_ignore_hostile_path_and_bash_env(
    external_handoff: tuple[Path, Path, Path, Path],
) -> None:
    script, _, _, candidate = external_handoff
    hostile_bin = candidate / "hostile-bin"
    hostile_bin.mkdir()
    marker = candidate / "startup-authority-executed"
    fake = hostile_bin / "fake"
    fake.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$0\" >> '{marker}'\nexit 91\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    for name in ("bash", "dirname", "pwd", "env"):
        shutil.copyfile(fake, hostile_bin / name)
        (hostile_bin / name).chmod(0o755)
    bash_env = candidate / "hostile-bash-env"
    bash_env.write_text(
        f"builtin printf '%s\\n' BASH_ENV >> '{marker}'\n",
        encoding="utf-8",
    )
    exported_function = (
        f"() {{ builtin printf '%s\\n' EXPORTED_FUNCTION >> '{marker}'; }}"
    )

    result = _run_external_handoff(
        external_handoff,
        extra_env={
            "PATH": str(hostile_bin),
            "BASH_ENV": str(bash_env),
            "ENV": str(bash_env),
            "CDPATH": str(hostile_bin),
            "GLOBIGNORE": "*",
            "SHELLOPTS": "braceexpand:hashall:interactive-comments:xtrace",
            "BASHOPTS": "checkwinsize:cmdhist:complete_fullquote:sourcepath",
            "BASH_FUNC_dirname%%": exported_function,
            "BASH_FUNC_pwd%%": exported_function,
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("deploy-run ")
    assert not marker.exists()
    assert script.read_text(encoding="utf-8").splitlines()[0] == "#!/bin/bash -p"


def test_sourced_wrapper_refuses_before_external_handoff(
    external_handoff: tuple[Path, Path, Path, Path],
) -> None:
    script, _, _, _ = external_handoff

    completed = subprocess.run(
        ["/bin/bash", "-p", "-c", 'source "$1"', "source-test", str(script)],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
    )

    assert completed.returncode == 2
    assert "Refusing sourced execution" in completed.stderr
    assert "deploy-run" not in completed.stdout


def test_invoking_user_owned_private_request_is_transport_not_authority(
    external_handoff: tuple[Path, Path, Path, Path],
) -> None:
    _, _, request, _ = external_handoff

    assert request.stat().st_uid == os.geteuid()
    assert request.stat().st_mode & 0o777 == 0o400
    result = _run_external_handoff(external_handoff)
    assert result.returncode == 0, result.stderr
    # Recorder success proves readability/FD transport only. The real installed
    # controller must authenticate the signed bytes, challenge and nonce.
    assert "--signed-request-fd" in result.stdout


def test_tampered_user_request_can_only_reach_rejecting_external_controller(
    external_handoff: tuple[Path, Path, Path, Path],
) -> None:
    _, controller, request, candidate = external_handoff
    pin = controller.parents[3] / "etc" / "propertyquarry" / "release-control" / "external-deploy-controller.sha256"
    action_log = candidate / "candidate-action.log"
    hostile_bin = candidate / "hostile-request-bin"
    hostile_bin.mkdir()
    fake = hostile_bin / "fake"
    fake.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$0\" >> '{action_log}'\nexit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    for name in ("python3", "docker", "docker-compose", "git"):
        shutil.copyfile(fake, hostile_bin / name)
        (hostile_bin / name).chmod(0o755)
    request.chmod(0o600)
    request.write_bytes(b'{"tampered":true}\n')
    request.chmod(0o400)
    controller.chmod(0o755)
    shutil.copyfile("/usr/bin/false", controller)
    controller.chmod(0o555)
    pin.chmod(0o644)
    pin.write_text(f"{hashlib.sha256(controller.read_bytes()).hexdigest()}\n", encoding="ascii")
    pin.chmod(0o444)

    result = _run_external_handoff(
        external_handoff,
        extra_env={"PATH": str(hostile_bin)},
    )

    assert result.returncode == 1
    assert not action_log.exists()


def test_wrapper_has_no_candidate_or_local_privileged_execution_path() -> None:
    root = Path(__file__).resolve().parents[1]
    deploy = (root / "scripts" / "deploy_propertyquarry.sh").read_text(encoding="utf-8")
    cloudflared = (root / "docker-compose.cloudflared.yml").read_text(encoding="utf-8")

    for forbidden in (
        "propertyquarry_deploy_controller_guard.py",
        "propertyquarry_deploy_drain_receipt.py",
        "docker compose",
        "docker-compose",
        "psql",
        "pg_stat_activity",
        "cloudflared:latest",
    ):
        assert forbidden not in deploy
    assert "PROPERTYQUARRY_DEPLOY_PYTHON_BIN" not in deploy
    assert "/usr/bin/python" not in deploy
    assert deploy.startswith("#!/bin/bash -p\nPATH=/usr/sbin:/usr/bin:/sbin:/bin\n")
    assert "$(dirname" not in deploy
    assert "cloudflare/cloudflared@sha256:" in cloudflared
    assert "cloudflare/cloudflared:latest" not in cloudflared
    assert '--monitoring-topology "${PROPERTYQUARRY_EXTERNAL_MONITORING_TOPOLOGY}"' in deploy
    assert '--monitoring-tools "${PROPERTYQUARRY_EXTERNAL_MONITORING_TOOLS}"' in deploy
