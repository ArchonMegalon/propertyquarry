from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path

import pytest
import yaml

from scripts import propertyquarry_release_authenticated_package as authenticated
from scripts import propertyquarry_release_local_container_gate as gate
from scripts import propertyquarry_release_local_identity as local_identity
from scripts import propertyquarry_release_package_payload as package_payload


ROOT = Path(__file__).resolve().parents[1]
PINNED_COMPOSE_PATH = ROOT / "compose.propertyquarry-release-control-v2.yml"
REAL_NATIVE_BUNDLE = ROOT / "build/propertyquarry-release-control-v2/linux-amd64"


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


def _fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    layout = tmp_path / "layout"
    layout.mkdir(mode=0o700)
    materialization_receipt = b'{"schema":"fixture"}'
    _write(
        layout / "materialization-receipt.v2.json",
        materialization_receipt,
        0o644,
    )
    archive = b"deterministic-docker-archive"
    _write(layout / "docker-image.tar", archive, 0o600)
    compose = PINNED_COMPOSE_PATH.read_bytes()
    _write(layout / "control-plane.compose.yml", compose, 0o644)
    native_receipt = _canonical(
        {
            "source_manifest_sha256": "sha256:" + "a" * 64,
            "toolchain": "go1.26.5 linux/amd64",
            "scratch_execution": dict(
                package_payload.NATIVE_SCRATCH_EXECUTION
            ),
            "build_flags": [
                "-mod=readonly",
                "-trimpath",
                "-buildvcs=false",
                "-buildmode=exe",
            ],
            "ldflags": (
                "-buildid= -linkmode=internal -X propertyquarry.local/"
                "release-control-v2/internal/releasecontrol."
                "SourceManifestDigest=sha256:"
                + "a" * 64
                + " -X propertyquarry.local/release-control-v2/internal/"
                "releasecontrol.ScratchExecutionContract="
                + package_payload.NATIVE_SCRATCH_EXECUTION["contract"]
            ),
        }
    )
    native_path = tmp_path / "build-receipt.json"
    _write(native_path, native_receipt, 0o644)
    image_receipt: dict[str, object] = {
        "image_id": "sha256:" + "b" * 64,
        "image_digest": "sha256:" + "c" * 64,
        "docker_archive_sha256": _digest(archive),
        "native_build_receipt_sha256": _digest(native_receipt),
        "authentication_sha256": "sha256:" + "d" * 64,
        "authentication_signature_sha256": "sha256:" + "e" * 64,
        "compose_sha256": _digest(compose),
        "runtime_user": "65534:1999",
    }
    return layout, native_path, image_receipt


def _mock_verified_layout(
    monkeypatch: pytest.MonkeyPatch,
    layout: Path,
    image_receipt: dict[str, object],
) -> None:
    def snapshot(*args, **kwargs) -> gate.oci.OciTreeSnapshot:
        del args, kwargs
        return gate.oci.OciTreeSnapshot(
            layout=b'{"imageLayoutVersion":"1.0.0"}',
            index=b"{}",
            installation_manifest=b"{}",
            materialization_receipt=(
                layout / "materialization-receipt.v2.json"
            ).read_bytes(),
            compose=(layout / "control-plane.compose.yml").read_bytes(),
            docker_archive=(layout / "docker-image.tar").read_bytes(),
            blobs={},
        )
    monkeypatch.setattr(
        gate.oci, "verify_pinned", lambda *args, **kwargs: image_receipt
    )
    monkeypatch.setattr(
        gate.oci, "_snapshot_oci_tree", snapshot
    )


def _build_info(component: str) -> bytes:
    return _canonical(
        {
            "schema": "propertyquarry.release-control.native-build-info.v2",
            "version": 2,
            "component": component,
            "toolchain": "go1.26.5",
            "source_manifest_digest": "sha256:" + "a" * 64,
            "scratch_execution_contract": (
                package_payload.NATIVE_SCRATCH_EXECUTION["contract"]
            ),
            "authoritative": False,
            "production_ready": False,
            "performs_effects": False,
            "self_test": True,
        }
    ) + b"\n"


def test_opt_in_gate_loads_by_id_runs_closed_checks_and_receipts_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, native_path, image_receipt = _fixture(tmp_path)
    _mock_verified_layout(monkeypatch, layout, image_receipt)
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []
    observed_inputs: dict[str, bytes] = {}
    sealed_inputs: dict[int, bytes] = {}
    real_sealed_memfd = gate._sealed_memfd

    def record_sealed_memfd(name: str, data: bytes) -> int:
        descriptor = real_sealed_memfd(name, data)
        sealed_inputs[descriptor] = data
        return descriptor

    monkeypatch.setattr(gate, "_sealed_memfd", record_sealed_memfd)

    def runner(command, environment) -> gate.CommandResult:
        command = tuple(command)
        calls.append((command, dict(environment)))
        assert len(sealed_inputs) == 2
        for descriptor, expected in sealed_inputs.items():
            assert (
                fcntl.fcntl(descriptor, fcntl.F_GET_SEALS)
                == gate._REQUIRED_SEALS
            )
            assert Path(f"/proc/{os.getpid()}/fd/{descriptor}").read_bytes() == expected
        if "--file" in command:
            observed_inputs["compose"] = Path(
                command[command.index("--file") + 1]
            ).read_bytes()
        if "--input" in command:
            observed_inputs["archive"] = Path(
                command[command.index("--input") + 1]
            ).read_bytes()
        if command[-3:] == ("image", "load", "--input"):
            raise AssertionError("archive argument unexpectedly absent")
        if "load" in command:
            return gate.CommandResult(0, b"Loaded image\n", b"")
        if "inspect" in command:
            return gate.CommandResult(
                0, str(image_receipt["image_id"]).encode("ascii") + b"\n", b""
            )
        if command[3:6] == ("container", "ls", "--all"):
            return gate.CommandResult(0, b"", b"")
        if command[3] == "run":
            return gate.CommandResult(50, b"", b"")
        if command[-3:] == ("ps", "--all", "--quiet"):
            return gate.CommandResult(0, b"", b"")
        service = command[-1]
        if service in gate.SELF_TESTS:
            return gate.CommandResult(0, _build_info(gate.SELF_TESTS[service]), b"")
        raise AssertionError(command)

    receipt_parent = tmp_path / "receipts"
    receipt_parent.mkdir(mode=0o700)
    receipt_path = receipt_parent / "container-gate.v2.json"
    result = gate.run_gate(
        layout=str(layout),
        wrapper=str(tmp_path / "wrapper"),
        external_anchor=str(tmp_path / "anchor.pem"),
        native_build_receipt=str(native_path),
        output_receipt=str(receipt_path),
        command_runner=runner,
    )

    assert result.image_id == image_receipt["image_id"]
    assert result.receipt_sha256 == _digest(receipt_path.read_bytes())
    receipt = json.loads(receipt_path.read_bytes())
    assert receipt["schema"] == gate.RECEIPT_SCHEMA
    assert receipt["docker_store_mutated"] is True
    assert receipt["all_self_tests_passed"] is True
    assert receipt["all_refusal_tests_passed"] is True
    assert receipt["no_test_containers_retained"] is True
    assert receipt["scratch_execution_contract_verified"] is True
    assert receipt["scratch_execution_contract"] == (
        package_payload.NATIVE_SCRATCH_EXECUTION["contract"]
    )
    assert receipt["source_manifest_sha256"] == "sha256:" + "a" * 64
    assert set(receipt["self_test_stdout_sha256"]) == set(gate.SELF_TESTS.values())
    assert receipt["refusal_exit_codes"] == {
        service: 50 for service in gate.REFUSAL_TESTS
    }
    assert len(calls) == 10
    for command, environment in calls:
        assert command[0:3] == (
            "/usr/bin/docker",
            "--host",
            "unix:///var/run/docker.sock",
        )
        assert environment["PROPERTYQUARRY_RELEASE_CONTROL_IMAGE"] == image_receipt[
            "image_id"
        ]
        assert environment["PROPERTYQUARRY_RELEASE_CONTROL_USER"] == "65534:1999"
        assert "DOCKER_CONFIG" in environment
        assert set(environment) == {
            "DOCKER_CONFIG",
            "HOME",
            "PATH",
            "PROPERTYQUARRY_RELEASE_CONTROL_IMAGE",
            "PROPERTYQUARRY_RELEASE_CONTROL_USER",
        }
    compose_runs = [
        command
        for command, _env in calls
        if command[3] == "compose" and "run" in command
    ]
    assert len(compose_runs) == 3
    self_test_runs = [
        command for command in compose_runs if command[-1] in gate.SELF_TESTS
    ]
    assert len(self_test_runs) == 3
    for command in self_test_runs:
        assert "--pull" in command
        assert command[command.index("--pull") + 1] == "never"
        assert "--rm" in command
        assert "--no-deps" in command
        assert "-T" in command
        compose_input = command[command.index("--file") + 1]
        assert compose_input.startswith(f"/proc/{os.getpid()}/fd/")
    project = self_test_runs[0][self_test_runs[0].index("--project-name") + 1]
    assert gate._PROJECT_RE.fullmatch(project) is not None
    refusal_label = f"{gate.GATE_CONTAINER_LABEL}={project}"
    direct_runs = [command for command, _env in calls if command[3] == "run"]
    assert len(direct_runs) == 3
    expected_refusals = []
    for service in gate.REFUSAL_TESTS:
        entrypoint, arguments = gate.REFUSAL_COMMANDS[service]
        expected_refusals.append(
            (
                "/usr/bin/docker",
                "--host",
                "unix:///var/run/docker.sock",
                "run",
                "--rm",
                "--pull",
                "never",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "32",
                "--memory",
                "64m",
                "--memory-reservation",
                "16m",
                "--memory-swap",
                "64m",
                "--user",
                "65534:1999",
                "--workdir",
                "/",
                "--label",
                refusal_label,
                "--name",
                f"{project}-{service.removesuffix('-test')}",
                "--entrypoint",
                entrypoint,
                str(image_receipt["image_id"]),
                *arguments,
            )
        )
    assert direct_runs == expected_refusals
    forbidden_direct_options = {
        "--env",
        "-e",
        "--volume",
        "-v",
        "--mount",
        "--publish",
        "-p",
        "--device",
        "--interactive",
        "-i",
        "--tty",
        "-t",
    }
    for command in direct_runs:
        assert forbidden_direct_options.isdisjoint(command)
    label_cleanup = next(
        command for command, _env in calls if command[3:6] == ("container", "ls", "--all")
    )
    assert label_cleanup == (
        "/usr/bin/docker",
        "--host",
        "unix:///var/run/docker.sock",
        "container",
        "ls",
        "--all",
        "--quiet",
        "--filter",
        f"label={refusal_label}",
    )
    load_command = next(command for command, _env in calls if "load" in command)
    archive_input = load_command[load_command.index("--input") + 1]
    assert archive_input.startswith(f"/proc/{os.getpid()}/fd/")
    assert observed_inputs == {
        "compose": (layout / "control-plane.compose.yml").read_bytes(),
        "archive": (layout / "docker-image.tar").read_bytes(),
    }


def test_self_test_output_mismatch_fails_without_success_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, native_path, image_receipt = _fixture(tmp_path)
    _mock_verified_layout(monkeypatch, layout, image_receipt)

    def runner(command, environment) -> gate.CommandResult:
        del environment
        if "load" in command:
            return gate.CommandResult(0, b"", b"")
        if "inspect" in command:
            return gate.CommandResult(
                0, str(image_receipt["image_id"]).encode() + b"\n", b""
            )
        return gate.CommandResult(0, b'{"self_test":true}\n', b"")

    receipt_parent = tmp_path / "receipts"
    receipt_parent.mkdir(mode=0o700)
    receipt = receipt_parent / "must-not-exist.json"
    with pytest.raises(gate.ContainerGateError, match="self-test-output-invalid"):
        gate.run_gate(
            layout=str(layout),
            wrapper=str(tmp_path / "wrapper"),
            external_anchor=str(tmp_path / "anchor.pem"),
            native_build_receipt=str(native_path),
            output_receipt=str(receipt),
            command_runner=runner,
        )
    assert not receipt.exists()


def test_build_info_parser_rejects_valid_but_noncanonical_key_order() -> None:
    component = "propertyquarry-release-controller-v2"
    canonical = _build_info(component)
    document = json.loads(canonical)
    reordered = dict(reversed(tuple(document.items())))
    noncanonical = (
        json.dumps(reordered, separators=(",", ":")).encode("ascii") + b"\n"
    )
    assert json.loads(noncanonical) == document
    assert noncanonical != canonical
    with pytest.raises(
        gate.ContainerGateError,
        match="native-self-test-output-invalid",
    ):
        gate._parse_build_info(
            noncanonical,
            component=component,
            toolchain="go1.26.5",
            source_manifest_digest="sha256:" + "a" * 64,
        )


def test_canonical_self_test_stdout_with_stderr_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, native_path, image_receipt = _fixture(tmp_path)
    _mock_verified_layout(monkeypatch, layout, image_receipt)

    def runner(command, environment) -> gate.CommandResult:
        del environment
        if "load" in command:
            return gate.CommandResult(0, b"", b"")
        if "inspect" in command:
            return gate.CommandResult(
                0, str(image_receipt["image_id"]).encode() + b"\n", b""
            )
        component = next(iter(gate.SELF_TESTS.values()))
        return gate.CommandResult(0, _build_info(component), b"diagnostic\n")

    receipt_parent = tmp_path / "receipts"
    receipt_parent.mkdir(mode=0o700)
    receipt = receipt_parent / "must-not-exist.json"
    with pytest.raises(gate.ContainerGateError, match="native-self-test-failed"):
        gate.run_gate(
            layout=str(layout),
            wrapper=str(tmp_path / "wrapper"),
            external_anchor=str(tmp_path / "anchor.pem"),
            native_build_receipt=str(native_path),
            output_receipt=str(receipt),
            command_runner=runner,
        )
    assert not receipt.exists()


def test_direct_refusal_rejects_compose_style_stderr_newline_and_checks_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, native_path, image_receipt = _fixture(tmp_path)
    _mock_verified_layout(monkeypatch, layout, image_receipt)
    cleanup_queried = False
    direct_runs = 0

    def runner(command, environment) -> gate.CommandResult:
        nonlocal cleanup_queried, direct_runs
        del environment
        command = tuple(command)
        if "load" in command:
            return gate.CommandResult(0, b"", b"")
        if "inspect" in command:
            return gate.CommandResult(
                0, str(image_receipt["image_id"]).encode() + b"\n", b""
            )
        if command[3:6] == ("container", "ls", "--all"):
            cleanup_queried = True
            return gate.CommandResult(0, b"", b"")
        if command[3] == "run":
            direct_runs += 1
            return gate.CommandResult(50, b"", b"\n")
        if command[-3:] == ("ps", "--all", "--quiet"):
            return gate.CommandResult(0, b"", b"")
        service = command[-1]
        if service in gate.SELF_TESTS:
            return gate.CommandResult(
                0, _build_info(gate.SELF_TESTS[service]), b""
            )
        raise AssertionError(command)

    receipt_parent = tmp_path / "receipts"
    receipt_parent.mkdir(mode=0o700)
    receipt = receipt_parent / "must-not-exist.json"
    with pytest.raises(gate.ContainerGateError, match="native-refusal-test-failed"):
        gate.run_gate(
            layout=str(layout),
            wrapper=str(tmp_path / "wrapper"),
            external_anchor=str(tmp_path / "anchor.pem"),
            native_build_receipt=str(native_path),
            output_receipt=str(receipt),
            command_runner=runner,
        )
    assert direct_runs == 1
    assert cleanup_queried is True
    assert not receipt.exists()


def test_direct_refusal_gate_requires_zero_uniquely_labeled_leftovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, native_path, image_receipt = _fixture(tmp_path)
    _mock_verified_layout(monkeypatch, layout, image_receipt)
    cleanup_filters: list[str] = []

    def runner(command, environment) -> gate.CommandResult:
        del environment
        command = tuple(command)
        if "load" in command:
            return gate.CommandResult(0, b"", b"")
        if "inspect" in command:
            return gate.CommandResult(
                0, str(image_receipt["image_id"]).encode() + b"\n", b""
            )
        if command[3:6] == ("container", "ls", "--all"):
            cleanup_filters.append(command[-1])
            return gate.CommandResult(0, b"retained-container-id\n", b"")
        if command[3] == "run":
            return gate.CommandResult(50, b"", b"")
        if command[-3:] == ("ps", "--all", "--quiet"):
            return gate.CommandResult(0, b"", b"")
        service = command[-1]
        if service in gate.SELF_TESTS:
            return gate.CommandResult(
                0, _build_info(gate.SELF_TESTS[service]), b""
            )
        raise AssertionError(command)

    receipt_parent = tmp_path / "receipts"
    receipt_parent.mkdir(mode=0o700)
    receipt = receipt_parent / "must-not-exist.json"
    with pytest.raises(gate.ContainerGateError, match="container-cleanup-failed"):
        gate.run_gate(
            layout=str(layout),
            wrapper=str(tmp_path / "wrapper"),
            external_anchor=str(tmp_path / "anchor.pem"),
            native_build_receipt=str(native_path),
            output_receipt=str(receipt),
            command_runner=runner,
        )
    assert len(cleanup_filters) == 1
    assert cleanup_filters[0].startswith(
        f"label={gate.GATE_CONTAINER_LABEL}=pq-release-gate-"
    )
    assert not receipt.exists()


def test_native_build_receipt_must_match_authenticated_image_before_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, native_path, image_receipt = _fixture(tmp_path)
    image_receipt["native_build_receipt_sha256"] = "sha256:" + "0" * 64
    _mock_verified_layout(monkeypatch, layout, image_receipt)
    invoked = False

    def runner(command, environment) -> gate.CommandResult:
        nonlocal invoked
        del command, environment
        invoked = True
        return gate.CommandResult(0, b"", b"")

    receipt_parent = tmp_path / "receipts"
    receipt_parent.mkdir(mode=0o700)
    with pytest.raises(gate.ContainerGateError, match="binding-mismatch"):
        gate.run_gate(
            layout=str(layout),
            wrapper=str(tmp_path / "wrapper"),
            external_anchor=str(tmp_path / "anchor.pem"),
            native_build_receipt=str(native_path),
            output_receipt=str(receipt_parent / "receipt.json"),
            command_runner=runner,
        )
    assert invoked is False


@pytest.mark.parametrize(
    "receipt_field",
    ["compose_sha256", "docker_archive_sha256"],
)
def test_verified_execution_inputs_must_bind_before_first_docker_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    receipt_field: str,
) -> None:
    layout, native_path, image_receipt = _fixture(tmp_path)
    image_receipt[receipt_field] = "sha256:" + "0" * 64
    _mock_verified_layout(monkeypatch, layout, image_receipt)
    invoked = False

    def runner(command, environment) -> gate.CommandResult:
        nonlocal invoked
        del command, environment
        invoked = True
        return gate.CommandResult(0, b"", b"")

    receipt_parent = tmp_path / "receipts"
    receipt_parent.mkdir(mode=0o700)
    with pytest.raises(gate.ContainerGateError, match="sealed-input-binding-mismatch"):
        gate.run_gate(
            layout=str(layout),
            wrapper=str(tmp_path / "wrapper"),
            external_anchor=str(tmp_path / "anchor.pem"),
            native_build_receipt=str(native_path),
            output_receipt=str(receipt_parent / "receipt.json"),
            command_runner=runner,
        )
    assert invoked is False


def test_compose_and_unsigned_receipt_digest_substitution_is_rejected_before_docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout, native_path, image_receipt = _fixture(tmp_path)
    changed = (layout / "control-plane.compose.yml").read_bytes().replace(
        b"network_mode: none",
        b"network_mode: bridge",
        1,
    )
    assert changed != PINNED_COMPOSE_PATH.read_bytes()
    _write(layout / "control-plane.compose.yml", changed, 0o644)
    # Model a coordinated rewrite of the unsigned materialization receipt by
    # returning the attacker's matching digest from the otherwise successful
    # image verifier.
    image_receipt["compose_sha256"] = _digest(changed)
    _mock_verified_layout(monkeypatch, layout, image_receipt)
    calls: list[tuple[str, ...]] = []

    def runner(command, environment) -> gate.CommandResult:
        del environment
        calls.append(tuple(command))
        return gate.CommandResult(0, b"", b"")

    receipt_parent = tmp_path / "receipts"
    receipt_parent.mkdir(mode=0o700)
    with pytest.raises(
        gate.ContainerGateError,
        match="compose-pinned-digest-mismatch",
    ):
        gate.run_gate(
            layout=str(layout),
            wrapper=str(tmp_path / "wrapper"),
            external_anchor=str(tmp_path / "anchor.pem"),
            native_build_receipt=str(native_path),
            output_receipt=str(receipt_parent / "receipt.json"),
            command_runner=runner,
        )
    assert calls == []
    assert not (receipt_parent / "receipt.json").exists()


def test_real_verified_layout_rejects_coordinated_compose_receipt_rewrite(
    tmp_path: Path,
) -> None:
    if not REAL_NATIVE_BUNDLE.is_dir():
        if os.environ.get("PROPERTYQUARRY_REQUIRE_REAL_NATIVE_BUNDLE") == "1":
            pytest.fail("required real native bundle is missing")
        pytest.skip("real native bundle is not available")
    state = local_identity.bootstrap_local_identity(
        state_root=str(tmp_path / "authority-state"),
        candidate_sha="b" * 40,
        workflow_sha="a" * 40,
    )
    payload = package_payload.assemble_payload(
        native_bundle=str(REAL_NATIVE_BUNDLE),
        private_bundle=state.package_input,
        service_gid=1999,
        output=str(tmp_path / "payload"),
    )
    wrapper = authenticated.create_authenticated_wrapper(
        payload=payload,
        private_key=state.package_private_key,
        external_anchor=state.package_external_anchor,
        output=str(tmp_path / "authenticated-wrapper"),
    )
    materialized = gate.oci.materialize(
        wrapper=wrapper,
        external_anchor=state.package_external_anchor,
        output=str(tmp_path / "oci-image"),
    )
    layout = Path(materialized.output)
    changed = (layout / "control-plane.compose.yml").read_bytes().replace(
        b"network_mode: none",
        b"network_mode: bridge",
        1,
    )
    receipt_path = layout / "materialization-receipt.v2.json"
    receipt = json.loads(receipt_path.read_bytes())
    receipt["compose_sha256"] = _digest(changed)
    _write(layout / "control-plane.compose.yml", changed, 0o644)
    _write(receipt_path, _canonical(receipt), 0o644)
    calls: list[tuple[str, ...]] = []

    def runner(command, environment) -> gate.CommandResult:
        del environment
        calls.append(tuple(command))
        return gate.CommandResult(0, b"", b"")

    receipt_parent = tmp_path / "gate-receipts"
    receipt_parent.mkdir(mode=0o700)
    with pytest.raises(
        gate.ContainerGateError,
        match="compose-pinned-digest-mismatch",
    ):
        gate.run_gate(
            layout=str(layout),
            wrapper=wrapper,
            external_anchor=state.package_external_anchor,
            native_build_receipt=str(REAL_NATIVE_BUNDLE / "build-receipt.json"),
            output_receipt=str(receipt_parent / "must-not-exist.json"),
            command_runner=runner,
        )
    assert calls == []
    assert not (receipt_parent / "must-not-exist.json").exists()


def test_pinned_compose_digest_and_closed_six_service_contract() -> None:
    raw = PINNED_COMPOSE_PATH.read_bytes()
    assert _digest(raw) == gate.PINNED_COMPOSE_SHA256
    gate._validate_pinned_compose(raw)
    document = yaml.safe_load(raw)
    mutations = []

    def mutate_extra_service(value: dict[str, object]) -> None:
        value["services"]["hostile"] = copy.deepcopy(  # type: ignore[index]
            value["services"]["controller-self-test"]  # type: ignore[index]
        )

    mutations.append(mutate_extra_service)
    for field, replacement in (
        ("build", "."),
        ("volumes", ["/:/host"]),
        ("ports", ["127.0.0.1:1:1"]),
        ("network_mode", "host"),
        ("privileged", True),
        ("devices", ["/dev/null:/dev/null"]),
    ):
        def mutate(
            value: dict[str, object],
            *,
            selected_field: str = field,
            selected_replacement: object = replacement,
        ) -> None:
            value["services"]["controller-self-test"][  # type: ignore[index]
                selected_field
            ] = selected_replacement

        mutations.append(mutate)
    for mutate in mutations:
        candidate = copy.deepcopy(document)
        mutate(candidate)
        with pytest.raises(gate.ContainerGateError, match="compose-contract-invalid"):
            gate._validate_compose_document(candidate)


@pytest.mark.parametrize(
    ("target", "replacement"),
    [
        ("control-plane.compose.yml", b"name: hostile\nservices: {}\n"),
        ("docker-image.tar", b"hostile-archive"),
    ],
)
def test_layout_name_substitution_cannot_change_sealed_docker_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    replacement: bytes,
) -> None:
    layout, native_path, image_receipt = _fixture(tmp_path)
    _mock_verified_layout(monkeypatch, layout, image_receipt)
    original_compose = (layout / "control-plane.compose.yml").read_bytes()
    original_archive = (layout / "docker-image.tar").read_bytes()
    substituted = False

    def runner(command, environment) -> gate.CommandResult:
        nonlocal substituted
        del environment
        command = tuple(command)
        if "--file" in command:
            assert (
                Path(command[command.index("--file") + 1]).read_bytes()
                == original_compose
            )
        if "--input" in command:
            assert (
                Path(command[command.index("--input") + 1]).read_bytes()
                == original_archive
            )
        if not substituted:
            changed = layout / f"{target}.replacement"
            mode = 0o600 if target == "docker-image.tar" else 0o644
            _write(changed, replacement, mode)
            os.replace(changed, layout / target)
            substituted = True
        if "load" in command:
            return gate.CommandResult(0, b"", b"")
        if "inspect" in command:
            return gate.CommandResult(
                0, str(image_receipt["image_id"]).encode() + b"\n", b""
            )
        if command[3:6] == ("container", "ls", "--all"):
            return gate.CommandResult(0, b"", b"")
        if command[3] == "run":
            return gate.CommandResult(50, b"", b"")
        if command[-3:] == ("ps", "--all", "--quiet"):
            return gate.CommandResult(0, b"", b"")
        service = command[-1]
        if service in gate.SELF_TESTS:
            return gate.CommandResult(0, _build_info(gate.SELF_TESTS[service]), b"")
        raise AssertionError(command)

    receipt_parent = tmp_path / "receipts"
    receipt_parent.mkdir(mode=0o700)
    receipt = receipt_parent / "must-not-publish.json"
    with pytest.raises(gate.ContainerGateError, match="pinned-layout-mutated"):
        gate.run_gate(
            layout=str(layout),
            wrapper=str(tmp_path / "wrapper"),
            external_anchor=str(tmp_path / "anchor.pem"),
            native_build_receipt=str(native_path),
            output_receipt=str(receipt),
            command_runner=runner,
        )
    assert substituted is True
    assert not receipt.exists()


@pytest.mark.parametrize(
    "docker_binary,docker_host",
    [
        ("docker", "unix:///var/run/docker.sock"),
        ("/usr/bin/docker", "tcp://127.0.0.1:2375"),
        ("/usr/bin/docker", "ssh://host"),
    ],
)
def test_gate_accepts_only_absolute_client_and_local_unix_socket(
    tmp_path: Path, docker_binary: str, docker_host: str
) -> None:
    with pytest.raises(gate.ContainerGateError, match="docker-boundary-invalid"):
        gate.run_gate(
            layout=str(tmp_path / "layout"),
            wrapper=str(tmp_path / "wrapper"),
            external_anchor=str(tmp_path / "anchor"),
            native_build_receipt=str(tmp_path / "receipt"),
            output_receipt=str(tmp_path / "output"),
            docker_binary=docker_binary,
            docker_host=docker_host,
        )
