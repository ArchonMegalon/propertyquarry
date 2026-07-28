from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest
import yaml

from scripts import propertyquarry_release_local_runtime_gate as gate


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_COMPOSE = ROOT / "compose.propertyquarry-release-runtime-v2.yml"
FROZEN_TEST_COMPOSE = ROOT / "compose.propertyquarry-release-control-v2.yml"
FROZEN_TEST_COMPOSE_SHA256 = (
    "sha256:bc9311d9437f6346d7fb3c45f16243021d29d47bed963c5f72eaf3e389b6bdc7"
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _write(path: Path, value: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    path.chmod(mode)


def _fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, dict[str, object], str]:
    monkeypatch.setattr(gate.time, "sleep", lambda _seconds: None)
    layout = tmp_path / "layout"
    layout.mkdir(mode=0o700)
    source_manifest_digest = "sha256:" + "a" * 64
    native_receipt = _canonical(
        {
            "build_flags": [
                "-mod=readonly",
                "-trimpath",
                "-buildvcs=false",
                "-buildmode=exe",
            ],
            "ldflags": (
                "-buildid= -linkmode=internal -X propertyquarry.local/"
                "release-control-v2/internal/releasecontrol."
                "SourceManifestDigest="
                + source_manifest_digest
                + " -X propertyquarry.local/release-control-v2/internal/"
                "releasecontrol.ScratchExecutionContract="
                + gate.oci.package_payload.NATIVE_SCRATCH_EXECUTION["contract"]
            ),
            "scratch_execution": (
                gate.oci.package_payload.NATIVE_SCRATCH_EXECUTION
            ),
            "source_manifest_sha256": source_manifest_digest,
            "toolchain": "go1.26.5 linux/amd64",
        }
    )
    native_path = tmp_path / "native-build-receipt.json"
    _write(native_path, native_receipt, 0o644)
    anchor_path = tmp_path / "durable-root-anchor" / "package-authority.pem"
    _write(anchor_path, b"PUBLIC-ANCHOR\n", 0o444)
    image_receipt: dict[str, object] = {
        "image_id": "sha256:" + "b" * 64,
        "image_digest": "sha256:" + "c" * 64,
        "native_build_receipt_sha256": _digest(native_receipt),
        "authentication_sha256": "sha256:" + "d" * 64,
        "authenticated_payload_tree_digest": "sha256:" + "e" * 64,
        "runtime_user": "65532:1999",
        "authority_user": "65532:1999",
        "caller_user": "65534:1999",
    }
    tree = gate.oci.OciTreeSnapshot(
        layout=b'{"imageLayoutVersion":"1.0.0"}',
        index=b"{}",
        installation_manifest=b"{}",
        materialization_receipt=b"{}",
        compose=b"compose",
        docker_archive=b"archive",
        blobs={},
    )
    authority_key_id = "sha256:" + "f" * 64
    monkeypatch.setattr(
        gate.oci, "verify_pinned", lambda *args, **kwargs: image_receipt
    )
    monkeypatch.setattr(gate.oci, "_snapshot_oci_tree", lambda *args: tree)
    monkeypatch.setattr(
        gate.oci,
        "_snapshot_authenticated_wrapper",
        lambda **kwargs: (
            object(),
            b"authentication",
            b"signature",
            {"signature_profile": {"key_id": authority_key_id}},
            {},
        ),
    )

    def open_anchor(_path: str) -> gate.DurableAnchor:
        parent_fd = os.open(
            anchor_path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        file_fd = os.open(anchor_path, os.O_RDONLY)
        metadata = os.fstat(file_fd)
        return gate.DurableAnchor(
            str(anchor_path),
            parent_fd,
            file_fd,
            anchor_path.name,
            gate._metadata_identity(metadata),
            anchor_path.read_bytes(),
        )

    monkeypatch.setattr(gate, "_open_durable_anchor", open_anchor)
    return layout, native_path, anchor_path, image_receipt, authority_key_id


def _health(
    image_receipt: dict[str, object], authority_key_id: str
) -> bytes:
    return _canonical(
        gate._expected_health_document(
            image_receipt=image_receipt,
            authority_key_id=authority_key_id,
            source_manifest_digest="sha256:" + "a" * 64,
        )
    ) + b"\n"


def _successful_runner(
    image_receipt: dict[str, object],
    authority_key_id: str,
    calls: list[tuple[tuple[str, ...], dict[str, str]]],
    *,
    retained_container: bool = False,
    retained_volume: bool = False,
    changed_after_inventory: bool = False,
    mutate_anchor: Path | None = None,
) -> gate.CommandRunner:
    inventory_calls = 0
    runtime_state_calls = 0
    anchor_mutated = False
    volume_removed = False

    def runner(command, environment) -> gate.CommandResult:
        nonlocal anchor_mutated, inventory_calls, runtime_state_calls
        nonlocal volume_removed
        command = tuple(command)
        calls.append((command, dict(environment)))
        if command[3:] == (
            "container",
            "ls",
            "--all",
            "--quiet",
            "--no-trunc",
        ):
            inventory_calls += 1
            identifier = ("1" if changed_after_inventory and inventory_calls > 1 else "0") * 64
            return gate.CommandResult(0, identifier.encode("ascii") + b"\n", b"")
        if command[3:6] == ("container", "inspect", "--format"):
            format_value = command[6]
            if format_value == gate._CONTAINER_STATE_FORMAT:
                identifier = command[7]
                return gate.CommandResult(
                    0,
                    (
                        f"{identifier}\t/app\trunning\thealthy\t0\n"
                    ).encode("ascii"),
                    b"",
                )
            if format_value == gate._RUNTIME_STATE_FORMAT:
                runtime_state_calls += 1
                supervisor = environment[
                    "PROPERTYQUARRY_RELEASE_RUNTIME_SUPERVISOR_NAME"
                ]
                watchdog = environment[
                    "PROPERTYQUARRY_RELEASE_RUNTIME_WATCHDOG_NAME"
                ]
                restarts = 0 if runtime_state_calls <= 2 else 1
                return gate.CommandResult(
                    0,
                    (
                        f"/{supervisor}\trunning\thealthy\t{restarts}\n"
                        f"/{watchdog}\trunning\thealthy\t{restarts}\n"
                    ).encode("ascii"),
                    b"",
                )
        if command[3:6] == ("image", "inspect", "--format"):
            return gate.CommandResult(
                0, str(image_receipt["image_id"]).encode("ascii") + b"\n", b""
            )
        if command[3] == "compose" and "up" in command:
            return gate.CommandResult(0, b"", b"")
        if command[3] == "compose" and "down" in command:
            return gate.CommandResult(0, b"", b"")
        if command[3] == "compose" and "exec" in command:
            if "--request-smoke" in command:
                if mutate_anchor is not None and not anchor_mutated:
                    mutate_anchor.unlink()
                    mutate_anchor.write_bytes(b"REPLACED-ANCHOR\n")
                    mutate_anchor.chmod(0o444)
                    anchor_mutated = True
                return gate.CommandResult(0, b"", b"")
            if "--docker-restart-stimulus" in command:
                return gate.CommandResult(137, b"", b"")
        if command[3:5] == ("container", "logs"):
            name = command[-1]
            if name.endswith("-watchdog"):
                return gate.CommandResult(
                    0, _health(image_receipt, authority_key_id), b""
                )
            return gate.CommandResult(0, b"", b"")
        if command[3:5] == ("container", "exec"):
            if "--health-json" in command:
                return gate.CommandResult(
                    0, _health(image_receipt, authority_key_id), b""
                )
        if command[3:6] == ("container", "kill", "--signal"):
            return gate.CommandResult(0, command[-1].encode("ascii") + b"\n", b"")
        if command[3:6] == ("container", "rm", "--force"):
            return gate.CommandResult(
                0,
                b"\n".join(value.encode("ascii") for value in command[6:])
                + b"\n",
                b"",
            )
        if (
            command[3:9]
            == (
                "container",
                "ls",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
            )
        ):
            return gate.CommandResult(
                0,
                (("9" * 64 + "\n").encode("ascii") if retained_container else b""),
                b"",
            )
        if command[3:6] == ("volume", "ls", "--quiet"):
            project = environment["PROPERTYQUARRY_RELEASE_RUNTIME_GATE"]
            volume = f"{project}_authority-replay-state-v1"
            output = (
                volume.encode("ascii") + b"\n"
                if retained_volume and not volume_removed
                else b""
            )
            return gate.CommandResult(0, output, b"")
        if command[3:5] == ("volume", "rm"):
            project = environment["PROPERTYQUARRY_RELEASE_RUNTIME_GATE"]
            assert command[5:] == (
                f"{project}_authority-replay-state-v1",
            )
            volume_removed = True
            return gate.CommandResult(0, b"", b"")
        if command[3:6] == ("network", "ls", "--quiet"):
            return gate.CommandResult(0, b"", b"")
        if command[3:5] == ("network", "rm"):
            return gate.CommandResult(0, b"", b"")
        raise AssertionError(command)

    return runner


def test_runtime_compose_is_exact_isolated_persistent_contract() -> None:
    raw = RUNTIME_COMPOSE.read_bytes()
    assert _digest(raw) == gate.PINNED_COMPOSE_SHA256
    gate._validate_runtime_compose(raw)
    document = yaml.safe_load(raw)
    assert set(document) == {
        "name",
        "services",
        "volumes",
        "x-propertyquarry-release-runtime",
    }
    assert set(document["services"]) == {"release-supervisor", "release-watchdog"}
    assert set(document["volumes"]) == {
        "runtime-socket",
        "authority-replay-state-v1",
    }
    gid = (
        "${PROPERTYQUARRY_RELEASE_RUNTIME_GID:?Set "
        "PROPERTYQUARRY_RELEASE_RUNTIME_GID to the validated service gid}"
    )
    assert document["volumes"]["runtime-socket"] == {
        "driver": "local",
        "driver_opts": {
            "type": "tmpfs",
            "device": "tmpfs",
            "o": f"uid=65532,gid={gid},mode=0750,nosuid,nodev,noexec,size=1048576",
        },
    }
    assert document["volumes"]["authority-replay-state-v1"] == {
        "driver": "local",
    }
    for service in document["services"].values():
        assert service["network_mode"] == "none"
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["mem_limit"] == "256m"
        assert service["mem_reservation"] == "16m"
        assert service["memswap_limit"] == "256m"
        assert service["restart"] == "on-failure:3"
        assert service["environment"] == []
        for forbidden in (
            "build",
            "ports",
            "devices",
            "env_file",
            "extra_hosts",
            "privileged",
        ):
            assert forbidden not in service
    supervisor = document["services"]["release-supervisor"]
    watchdog = document["services"]["release-watchdog"]
    assert supervisor["user"].startswith(
        "${PROPERTYQUARRY_RELEASE_RUNTIME_AUTHORITY_USER:"
    )
    assert watchdog["user"].startswith(
        "${PROPERTYQUARRY_RELEASE_RUNTIME_CALLER_USER:"
    )
    assert [mount["target"] for mount in supervisor["volumes"]] == [
        gate.EXTERNAL_ANCHOR_TARGET,
        gate.RUNTIME_SOCKET_DIRECTORY,
        gate.RUNTIME_STATE_DIRECTORY,
    ]
    assert [mount["target"] for mount in watchdog["volumes"]] == [
        gate.EXTERNAL_ANCHOR_TARGET,
        gate.RUNTIME_SOCKET_DIRECTORY,
    ]
    assert all(
        mount["target"] != gate.RUNTIME_STATE_DIRECTORY
        for mount in watchdog["volumes"]
    )
    for mounts in (supervisor["volumes"], watchdog["volumes"]):
        assert mounts[0]["read_only"] is True
        assert mounts[0]["bind"] == {"create_host_path": False}
        assert mounts[1]["volume"] == {"nocopy": True}
    assert supervisor["volumes"][2] == {
        "type": "volume",
        "source": "authority-replay-state-v1",
        "target": gate.RUNTIME_STATE_DIRECTORY,
    }
    assert all(
        mount.get("source") != "authority-replay-state-v1"
        for mount in watchdog["volumes"]
    )
    assert b"docker.sock" not in raw
    assert b"/docker/property" not in raw
    assert _digest(FROZEN_TEST_COMPOSE.read_bytes()) == FROZEN_TEST_COMPOSE_SHA256


def test_runtime_compose_mutation_is_rejected() -> None:
    raw = RUNTIME_COMPOSE.read_bytes()
    changed = raw.replace(b"network_mode: none", b"network_mode: host", 1)
    with pytest.raises(gate.RuntimeGateError, match="pinned-digest-mismatch"):
        gate._validate_runtime_compose(changed)


def test_container_inventory_template_allows_missing_health_map_key() -> None:
    assert '.State.Health' not in gate._CONTAINER_STATE_FORMAT
    assert 'index .State "Health"' in gate._CONTAINER_STATE_FORMAT
    assert '.State.Health' not in gate._RUNTIME_STATE_FORMAT
    assert 'index .State "Health"' in gate._RUNTIME_STATE_FORMAT
    assert "\\t" not in gate._CONTAINER_STATE_FORMAT
    assert gate._CONTAINER_STATE_FORMAT.count("\t") == 4
    assert "\\t" not in gate._RUNTIME_STATE_FORMAT
    assert gate._RUNTIME_STATE_FORMAT.count("\t") == 3
    identifier = "1" * 64
    inventory = gate._parse_container_inventory(
        (identifier + "\n").encode("ascii"),
        (f"{identifier}\t/no-healthcheck\trunning\tnone\t0\n").encode("ascii"),
    )
    assert json.loads(inventory) == [
        {
            "health": "none",
            "id": identifier,
            "name": "no-healthcheck",
            "restart_count": 0,
            "status": "running",
        }
    ]


def test_container_inventory_preserves_preexisting_unhealthy_state() -> None:
    identifier = "2" * 64
    inventory = gate._parse_container_inventory(
        (identifier + "\n").encode("ascii"),
        (f"{identifier}\t/existing\texited\tunhealthy\t7\n").encode("ascii"),
    )
    assert json.loads(inventory) == [
        {
            "health": "unhealthy",
            "id": identifier,
            "name": "existing",
            "restart_count": 7,
            "status": "exited",
        }
    ]


def test_runtime_gate_proves_lifecycle_no_effects_restart_and_clean_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, native_path, anchor_path, image_receipt, authority_key_id = _fixture(
        tmp_path, monkeypatch
    )
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []
    sealed: dict[int, bytes] = {}
    real_sealed = gate._sealed_memfd

    def record_sealed(name: str, data: bytes) -> int:
        descriptor = real_sealed(name, data)
        sealed[descriptor] = data
        return descriptor

    monkeypatch.setattr(gate, "_sealed_memfd", record_sealed)
    receipt_parent = tmp_path / "receipts"
    receipt_parent.mkdir(mode=0o700)
    receipt_path = receipt_parent / "runtime-gate.v2.json"
    result = gate.run_gate(
        layout=str(layout),
        wrapper=str(tmp_path / "wrapper"),
        external_anchor=str(anchor_path),
        native_build_receipt=str(native_path),
        output_receipt=str(receipt_path),
        command_runner=_successful_runner(
            image_receipt, authority_key_id, calls
        ),
    )
    assert result.receipt_sha256 == _digest(receipt_path.read_bytes())
    receipt = json.loads(receipt_path.read_bytes())
    assert set(receipt) == {
        "after_container_health_sha256",
        "authority_user",
        "authentication_sha256",
        "authoritative_for_local_runtime_gate",
        "authoritative_for_release_effects",
        "before_container_health_sha256",
        "caller_user",
        "docker_store_mutated",
        "drift_restart_verified",
        "external_anchor_sha256",
        "framed_request_fail_closed",
        "health_stdout_sha256",
        "image_digest",
        "image_id",
        "initial_health_stdout_sha256",
        "native_build_receipt_sha256",
        "network_requests_permitted",
        "no_runtime_containers_retained",
        "no_runtime_networks_retained",
        "no_runtime_volumes_retained",
        "performs_release_effects",
        "preexisting_container_health_unchanged",
        "production_ready",
        "recovered_health_stdout_sha256",
        "runtime_compose_sha256",
        "runtime_plane_ready",
        "runtime_plane_removed",
        "runtime_plane_started",
        "runtime_user",
        "schema",
        "socket_accepting",
        "source_manifest_sha256",
        "supervisor_restart_count",
        "version",
        "watchdog_restart_count",
    }
    assert receipt["schema"] == gate.RECEIPT_SCHEMA
    assert receipt["version"] == 2
    assert {
        key: receipt[key]
        for key in (
            "authoritative_for_local_runtime_gate",
            "authoritative_for_release_effects",
            "docker_store_mutated",
            "network_requests_permitted",
            "no_runtime_containers_retained",
            "no_runtime_networks_retained",
            "no_runtime_volumes_retained",
            "performs_release_effects",
            "production_ready",
            "runtime_plane_removed",
        )
    } == {
        "authoritative_for_local_runtime_gate": True,
        "authoritative_for_release_effects": False,
        "docker_store_mutated": True,
        "network_requests_permitted": False,
        "no_runtime_containers_retained": True,
        "no_runtime_networks_retained": True,
        "no_runtime_volumes_retained": True,
        "performs_release_effects": False,
        "production_ready": False,
        "runtime_plane_removed": True,
    }
    assert receipt["runtime_plane_started"] is True
    assert receipt["runtime_plane_ready"] is True
    assert receipt["socket_accepting"] is True
    assert receipt["framed_request_fail_closed"] is True
    assert receipt["drift_restart_verified"] is True
    assert receipt["runtime_plane_removed"] is True
    assert receipt["preexisting_container_health_unchanged"] is True
    assert receipt["supervisor_restart_count"] >= 1
    assert receipt["watchdog_restart_count"] >= 1
    assert receipt["runtime_user"] == "65532:1999"
    assert receipt["authority_user"] == "65532:1999"
    assert receipt["caller_user"] == "65534:1999"
    assert receipt["before_container_health_sha256"] == receipt[
        "after_container_health_sha256"
    ]
    compose_commands = [command for command, _env in calls if command[3] == "compose"]
    assert len([command for command in compose_commands if "up" in command]) == 1
    assert len([command for command in compose_commands if "down" in command]) == 1
    assert len([command for command in compose_commands if "exec" in command]) == 3
    up = next(command for command in compose_commands if "up" in command)
    down = next(command for command in compose_commands if "down" in command)
    assert up[-10:] == (
        "up",
        "--detach",
        "--no-build",
        "--pull",
        "never",
        "--wait",
        "--wait-timeout",
        "60",
        "release-supervisor",
        "release-watchdog",
    )
    assert down[-5:] == (
        "down",
        "--volumes",
        "--remove-orphans",
        "--timeout",
        "10",
    )
    project = next(
        environment["PROPERTYQUARRY_RELEASE_RUNTIME_GATE"]
        for _command, environment in calls
        if "PROPERTYQUARRY_RELEASE_RUNTIME_GATE" in environment
    )
    assert gate.PROJECT_RE.fullmatch(project) is not None
    for command, environment in calls:
        if "PROPERTYQUARRY_RELEASE_RUNTIME_GATE" in environment:
            assert set(environment) == {
                "DOCKER_CONFIG",
                "HOME",
                "PATH",
                "PROPERTYQUARRY_RELEASE_RUNTIME_ANCHOR",
                "PROPERTYQUARRY_RELEASE_RUNTIME_AUTHORITY_USER",
                "PROPERTYQUARRY_RELEASE_RUNTIME_CALLER_USER",
                "PROPERTYQUARRY_RELEASE_RUNTIME_GATE",
                "PROPERTYQUARRY_RELEASE_RUNTIME_GID",
                "PROPERTYQUARRY_RELEASE_RUNTIME_IMAGE",
                "PROPERTYQUARRY_RELEASE_RUNTIME_SUPERVISOR_NAME",
                "PROPERTYQUARRY_RELEASE_RUNTIME_WATCHDOG_NAME",
            }
            assert environment["PROPERTYQUARRY_RELEASE_RUNTIME_ANCHOR"] == str(
                anchor_path
            )
    assert any("--request-smoke" in command for command, _env in calls)
    assert all(
        ("exec", "-T", "release-watchdog")
        == command[command.index("exec") : command.index("exec") + 3]
        for command, _env in calls
        if "--request-smoke" in command
    )
    restart_stimuli = [
        command
        for command, _env in calls
        if "--docker-restart-stimulus" in command
    ]
    assert len(restart_stimuli) == 1
    assert (
        "exec",
        "-T",
        "release-supervisor",
        gate.SUPERVISOR_PATH,
        "--installed-local-authority",
        "--docker-restart-stimulus",
    ) == restart_stimuli[0][-6:]
    assert any(
        command[3:6] == ("volume", "ls", "--quiet")
        and command[-1] == f"label=com.docker.compose.project={project}"
        for command, _env in calls
    )
    assert any(
        command[3:6] == ("network", "ls", "--quiet")
        and "--no-trunc" in command
        and command[-1] == f"label=com.docker.compose.project={project}"
        for command, _env in calls
    )
    assert len(sealed) == 1
    for descriptor, expected in sealed.items():
        with pytest.raises(OSError):
            fcntl.fcntl(descriptor, fcntl.F_GET_SEALS)
        assert expected == RUNTIME_COMPOSE.read_bytes()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("runtime_user", "65534:1999"),
        ("authority_user", "65534:1999"),
        ("caller_user", "65532:1999"),
        ("caller_user", "65534:2000"),
        ("authority_user", "65532:4294967296"),
    ],
)
def test_runtime_gate_rejects_collapsed_or_mismatched_principal_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    layout, native_path, anchor_path, image_receipt, _authority_key_id = _fixture(
        tmp_path, monkeypatch
    )
    image_receipt[field] = value
    invoked = False

    def runner(command, environment) -> gate.CommandResult:
        nonlocal invoked
        del command, environment
        invoked = True
        return gate.CommandResult(0, b"", b"")

    with pytest.raises(gate.RuntimeGateError, match="runtime-interpolation-invalid"):
        gate.run_gate(
            layout=str(layout),
            wrapper=str(tmp_path / "wrapper"),
            external_anchor=str(anchor_path),
            native_build_receipt=str(native_path),
            output_receipt=str(tmp_path / "must-not-exist.json"),
            command_runner=runner,
        )
    assert invoked is False


def test_runtime_gate_cleanup_failure_never_publishes_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, native_path, anchor_path, image_receipt, authority_key_id = _fixture(
        tmp_path, monkeypatch
    )
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []
    receipt_parent = tmp_path / "receipts"
    receipt_parent.mkdir(mode=0o700)
    receipt = receipt_parent / "must-not-exist.json"
    with pytest.raises(gate.RuntimeGateError, match="runtime-cleanup-failed"):
        gate.run_gate(
            layout=str(layout),
            wrapper=str(tmp_path / "wrapper"),
            external_anchor=str(anchor_path),
            native_build_receipt=str(native_path),
            output_receipt=str(receipt),
            command_runner=_successful_runner(
                image_receipt,
                authority_key_id,
                calls,
                retained_container=True,
            ),
        )
    assert not receipt.exists()
    assert any(
        command[3:6] == ("container", "rm", "--force")
        for command, _environment in calls
    )


def test_runtime_gate_forcibly_removes_retained_authority_replay_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, native_path, anchor_path, image_receipt, authority_key_id = _fixture(
        tmp_path, monkeypatch
    )
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []
    receipt_parent = tmp_path / "receipts"
    receipt_parent.mkdir(mode=0o700)
    receipt = receipt_parent / "runtime-gate.json"
    result = gate.run_gate(
        layout=str(layout),
        wrapper=str(tmp_path / "wrapper"),
        external_anchor=str(anchor_path),
        native_build_receipt=str(native_path),
        output_receipt=str(receipt),
        command_runner=_successful_runner(
            image_receipt,
            authority_key_id,
            calls,
            retained_volume=True,
        ),
    )
    assert result.receipt == str(receipt)
    assert receipt.exists()
    assert any(
        command[3:5] == ("volume", "rm")
        and command[5].endswith("_authority-replay-state-v1")
        for command, _environment in calls
    )


def test_runtime_gate_never_removes_an_unexpected_project_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, native_path, anchor_path, image_receipt, authority_key_id = _fixture(
        tmp_path, monkeypatch
    )
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []
    base_runner = _successful_runner(
        image_receipt,
        authority_key_id,
        calls,
    )

    def runner(command, environment) -> gate.CommandResult:
        exact_command = tuple(command)
        if exact_command[3:6] == ("volume", "ls", "--quiet"):
            calls.append((exact_command, dict(environment)))
            project = environment["PROPERTYQUARRY_RELEASE_RUNTIME_GATE"]
            return gate.CommandResult(
                0,
                f"{project}_unexpected-volume\n".encode("ascii"),
                b"",
            )
        return base_runner(command, environment)

    receipt = tmp_path / "must-not-exist.json"
    with pytest.raises(gate.RuntimeGateError, match="runtime-cleanup-failed"):
        gate.run_gate(
            layout=str(layout),
            wrapper=str(tmp_path / "wrapper"),
            external_anchor=str(anchor_path),
            native_build_receipt=str(native_path),
            output_receipt=str(receipt),
            command_runner=runner,
        )
    assert not receipt.exists()
    assert not any(
        command[3:5] == ("volume", "rm")
        for command, _environment in calls
    )


def test_runtime_gate_detects_preexisting_container_health_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, native_path, anchor_path, image_receipt, authority_key_id = _fixture(
        tmp_path, monkeypatch
    )
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []
    receipt_parent = tmp_path / "receipts"
    receipt_parent.mkdir(mode=0o700)
    receipt = receipt_parent / "must-not-exist.json"
    with pytest.raises(
        gate.RuntimeGateError, match="preexisting-container-health-changed"
    ):
        gate.run_gate(
            layout=str(layout),
            wrapper=str(tmp_path / "wrapper"),
            external_anchor=str(anchor_path),
            native_build_receipt=str(native_path),
            output_receipt=str(receipt),
            command_runner=_successful_runner(
                image_receipt,
                authority_key_id,
                calls,
                changed_after_inventory=True,
            ),
        )
    assert not receipt.exists()


@pytest.mark.parametrize(
    "docker_binary,docker_host",
    [
        ("docker", "unix:///var/run/docker.sock"),
        ("/usr/bin/docker", "tcp://127.0.0.1:2375"),
    ],
)
def test_runtime_gate_rejects_nonlocal_docker_boundary_before_runner(
    tmp_path: Path,
    docker_binary: str,
    docker_host: str,
) -> None:
    invoked = False

    def runner(command, environment) -> gate.CommandResult:
        nonlocal invoked
        del command, environment
        invoked = True
        return gate.CommandResult(0, b"", b"")

    with pytest.raises(gate.RuntimeGateError, match="docker-boundary-invalid"):
        gate.run_gate(
            layout=str(tmp_path / "layout"),
            wrapper=str(tmp_path / "wrapper"),
            external_anchor=str(tmp_path / "anchor"),
            native_build_receipt=str(tmp_path / "receipt"),
            output_receipt=str(tmp_path / "output"),
            docker_binary=docker_binary,
            docker_host=docker_host,
            command_runner=runner,
        )
    assert invoked is False


def test_volatile_anchor_path_is_rejected() -> None:
    with pytest.raises(gate.RuntimeGateError, match="not-durable"):
        gate._open_durable_anchor("/tmp/propertyquarry-anchor.pem")


def test_volatile_anchor_filesystem_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate, "_filesystem_type", lambda _descriptor: 0x01021994)
    with pytest.raises(
        gate.RuntimeGateError, match="runtime-anchor-filesystem-volatile"
    ):
        gate._open_durable_anchor("/opt/propertyquarry/package-authority.pem")


def test_anchor_metadata_rejects_ancestor_symlink_and_hardlinked_leaf() -> None:
    directory = SimpleNamespace(
        st_mode=stat.S_IFDIR | 0o755,
        st_uid=0,
        st_gid=0,
    )
    assert gate._anchor_ancestor_metadata_valid(directory)
    assert not gate._anchor_ancestor_metadata_valid(
        SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o775,
            st_uid=0,
            st_gid=0,
        )
    )
    assert not gate._anchor_ancestor_metadata_valid(
        SimpleNamespace(
            st_mode=stat.S_IFLNK | 0o777,
            st_uid=0,
            st_gid=0,
        )
    )
    regular = SimpleNamespace(
        st_mode=stat.S_IFREG | 0o444,
        st_nlink=1,
        st_uid=0,
        st_gid=0,
        st_size=32,
    )
    assert gate._anchor_file_metadata_valid(regular)
    assert not gate._anchor_file_metadata_valid(
        SimpleNamespace(
            st_mode=regular.st_mode,
            st_nlink=2,
            st_uid=regular.st_uid,
            st_gid=regular.st_gid,
            st_size=regular.st_size,
        )
    )


def test_anchor_revalidation_rejects_path_replacement(tmp_path: Path) -> None:
    parent = tmp_path / "anchor-parent"
    parent.mkdir(mode=0o700)
    path = parent / "anchor.pem"
    _write(path, b"ORIGINAL\n", 0o444)
    parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    file_fd = os.open(path, os.O_RDONLY)
    metadata = os.fstat(file_fd)
    anchor = gate.DurableAnchor(
        str(path),
        parent_fd,
        file_fd,
        path.name,
        gate._metadata_identity(metadata),
        b"ORIGINAL\n",
    )
    try:
        path.unlink()
        _write(path, b"REPLACED\n", 0o444)
        with pytest.raises(
            gate.RuntimeGateError, match="runtime-anchor-concurrent-mutation"
        ):
            gate._revalidate_anchor(anchor)
    finally:
        os.close(file_fd)
        os.close(parent_fd)


def test_anchor_drift_cannot_block_sealed_project_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, native_path, anchor_path, image_receipt, authority_key_id = _fixture(
        tmp_path, monkeypatch
    )
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []
    receipt_parent = tmp_path / "receipts"
    receipt_parent.mkdir(mode=0o700)
    receipt = receipt_parent / "must-not-exist.json"
    with pytest.raises(
        gate.RuntimeGateError, match="runtime-anchor-concurrent-mutation"
    ):
        gate.run_gate(
            layout=str(layout),
            wrapper=str(tmp_path / "wrapper"),
            external_anchor=str(anchor_path),
            native_build_receipt=str(native_path),
            output_receipt=str(receipt),
            command_runner=_successful_runner(
                image_receipt,
                authority_key_id,
                calls,
                mutate_anchor=anchor_path,
            ),
        )
    assert not receipt.exists()
    assert any(
        command[3] == "compose" and "down" in command
        for command, _environment in calls
    )
    assert len(
        [
            command
            for command, _environment in calls
            if command[3:9]
            == (
                "container",
                "ls",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
            )
        ]
    ) == 2
