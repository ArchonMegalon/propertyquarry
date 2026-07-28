#!/usr/bin/env python3
"""Explicit opt-in local Docker load and native one-shot gate.

Importing this module and running its unit tests never contact Docker.  The CLI
is the mutation boundary: it reauthenticates the OCI source, verifies all local
artifacts, then loads one image, runs three sealed-Compose self-tests, and runs
three direct hardened refusal checks.
"""

from __future__ import annotations

import argparse
import dataclasses
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import tempfile
from typing import Callable, Final, Mapping, Sequence

import yaml

try:  # Support module and direct-script execution.
    from scripts import propertyquarry_release_oci_materializer as oci
except ModuleNotFoundError:  # pragma: no cover - direct CLI coverage
    import propertyquarry_release_oci_materializer as oci


RECEIPT_SCHEMA: Final = "propertyquarry.release-control.local-container-gate.v2"
MAX_OUTPUT_BYTES: Final = 65_536
MAX_JSON_BYTES: Final = 256 * 1024
_DIGEST_RE: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DOCKER_HOST_RE: Final = re.compile(r"unix:///[A-Za-z0-9_./-]{1,4000}\Z")
_PROJECT_RE: Final = re.compile(r"pq-release-gate-[0-9a-f]{16}\Z")
_RUNTIME_USER_RE: Final = re.compile(
    r"(?:0|[1-9][0-9]{0,9}):(?:0|[1-9][0-9]{0,9})\Z"
)
# This literal is deliberately independent of the unsigned materialization
# receipt and the workspace Compose path. Changing the accepted control plane
# requires an intentional code review and digest update here.
PINNED_COMPOSE_SHA256: Final = (
    "sha256:bc9311d9437f6346d7fb3c45f16243021d29d47bed963c5f72eaf3e389b6bdc7"
)
_IMAGE_INTERPOLATION: Final = (
    "${PROPERTYQUARRY_RELEASE_CONTROL_IMAGE:?Set "
    "PROPERTYQUARRY_RELEASE_CONTROL_IMAGE to the verified local image ID "
    "sha256:<64 lowercase hex>}"
)
_USER_INTERPOLATION: Final = (
    "${PROPERTYQUARRY_RELEASE_CONTROL_USER:?Set "
    "PROPERTYQUARRY_RELEASE_CONTROL_USER to numeric uid:gid from the "
    "materialization receipt}"
)
_REQUIRED_SEALS: Final = (
    fcntl.F_SEAL_SEAL
    | fcntl.F_SEAL_SHRINK
    | fcntl.F_SEAL_GROW
    | fcntl.F_SEAL_WRITE
)

SELF_TESTS: Final = {
    "controller-self-test": "propertyquarry-release-controller-v2",
    "supervisor-self-test": "propertyquarry-release-supervisor-v2",
    "watchdog-self-test": "propertyquarry-release-watchdog-v2",
}
REFUSAL_TESTS: Final = (
    "controller-refusal-test",
    "supervisor-refusal-test",
    "watchdog-refusal-test",
)
REFUSAL_COMMANDS: Final = {
    "controller-refusal-test": (
        "/usr/libexec/propertyquarry-release-control/"
        "propertyquarry-release-controller-v2",
        (),
    ),
    "supervisor-refusal-test": (
        "/usr/libexec/propertyquarry-release-control/"
        "propertyquarry-release-supervisor-v2",
        (),
    ),
    "watchdog-refusal-test": (
        "/usr/libexec/propertyquarry-release-control/"
        "propertyquarry-release-watchdog-v2",
        (
            "--config",
            "/etc/propertyquarry-release-control/watchdog-v2.json",
        ),
    ),
}
GATE_CONTAINER_LABEL: Final = "propertyquarry.release-control.gate"
BUILD_INFO_KEYS: Final = frozenset(
    {
        "schema",
        "version",
        "component",
        "toolchain",
        "source_manifest_digest",
        "scratch_execution_contract",
        "authoritative",
        "production_ready",
        "performs_effects",
        "self_test",
    }
)


class ContainerGateError(ValueError):
    """A bounded and secret-free local-container gate failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclasses.dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclasses.dataclass(frozen=True, slots=True)
class GateResult:
    receipt: str
    receipt_sha256: str
    image_id: str


CommandRunner = Callable[[Sequence[str], Mapping[str, str]], CommandResult]


def _fail(code: str) -> None:
    raise ContainerGateError(code)


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        _fail("canonical-json-invalid")


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _exact_equal(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return set(left) == set(right) and all(
            _exact_equal(left[key], right[key]) for key in left
        )
    if type(left) is list:
        return len(left) == len(right) and all(
            _exact_equal(a, b) for a, b in zip(left, right)
        )
    return left == right


def _expected_compose_document() -> dict[str, object]:
    base: dict[str, object] = {
        "image": _IMAGE_INTERPOLATION,
        "pull_policy": "never",
        "network_mode": "none",
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "pids_limit": 32,
        "mem_limit": "64m",
        "mem_reservation": "16m",
        "memswap_limit": "64m",
        "user": _USER_INTERPOLATION,
        "working_dir": "/",
        "environment": [],
        "stdin_open": False,
        "tty": False,
        "init": False,
        "restart": "no",
    }
    entrypoints = {
        component: [
            "/usr/libexec/propertyquarry-release-control/"
            f"propertyquarry-release-{component}-v2"
        ]
        for component in ("controller", "supervisor", "watchdog")
    }
    services: dict[str, object] = {}
    for component, entrypoint in entrypoints.items():
        services[f"{component}-self-test"] = {
            **base,
            "profiles": ["self-test"],
            "entrypoint": entrypoint,
            "command": ["--self-test"],
        }
        services[f"{component}-refusal-test"] = {
            **base,
            "profiles": ["refusal-test"],
            "entrypoint": entrypoint,
            "command": (
                [
                    "--config",
                    "/etc/propertyquarry-release-control/watchdog-v2.json",
                ]
                if component == "watchdog"
                else []
            ),
        }
    return {
        "name": "propertyquarry-release-control-v2",
        "x-propertyquarry-release-control-self-test": base,
        "services": services,
    }


def _validate_compose_document(document: object) -> None:
    """Require the closed six-service, zero-host-access control plane."""

    if not _exact_equal(document, _expected_compose_document()):
        _fail("compose-contract-invalid")


def _validate_pinned_compose(raw: bytes) -> None:
    if type(raw) is not bytes or _digest(raw) != PINNED_COMPOSE_SHA256:
        _fail("compose-pinned-digest-mismatch")
    try:
        document = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError, RecursionError):
        _fail("compose-contract-invalid")
    _validate_compose_document(document)


def _validate_runtime_interpolations(image_id: object, runtime_user: object) -> tuple[str, str]:
    if type(image_id) is not str or _DIGEST_RE.fullmatch(image_id) is None:
        _fail("compose-image-id-invalid")
    if type(runtime_user) is not str or _RUNTIME_USER_RE.fullmatch(runtime_user) is None:
        _fail("compose-runtime-user-invalid")
    uid_text, gid_text = runtime_user.split(":", 1)
    if int(uid_text) > 0xFFFFFFFF or int(gid_text) > 0xFFFFFFFF:
        _fail("compose-runtime-user-invalid")
    return image_id, runtime_user


def _read_regular(path: Path, *, maximum: int, mode: int | None = None) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size <= 0
                or before.st_size > maximum
                or (mode is not None and stat.S_IMODE(before.st_mode) != mode)
            ):
                _fail("gate-input-file-invalid")
            value = b""
            remaining = before.st_size
            while remaining:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    _fail("gate-input-short-read")
                value += chunk
                remaining -= len(chunk)
            after = os.fstat(descriptor)
            identity = lambda item: (  # noqa: E731
                item.st_dev,
                item.st_ino,
                item.st_mode,
                item.st_nlink,
                item.st_uid,
                item.st_gid,
                item.st_size,
                item.st_mtime_ns,
                item.st_ctime_ns,
            )
            if identity(before) != identity(after):
                _fail("gate-input-concurrent-mutation")
            return value
        finally:
            os.close(descriptor)
    except ContainerGateError:
        raise
    except OSError:
        _fail("gate-input-read-failed")


def _load_json(data: bytes, code: str) -> object:
    try:
        value = json.loads(data.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail(code)
    if _canonical(value) != data:
        _fail(code)
    return value


def _load_digest_bound_json(data: bytes, code: str) -> object:
    """Parse exact externally digest-bound JSON without rewriting its bytes."""

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                _fail(code)
            value[key] = item
        return value

    def reject_constant(_value: str) -> object:
        _fail(code)

    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except ContainerGateError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        _fail(code)


def _sealed_memfd(name: str, data: bytes) -> int:
    descriptor = -1
    try:
        descriptor = os.memfd_create(
            name,
            getattr(os, "MFD_CLOEXEC", 0) | getattr(os, "MFD_ALLOW_SEALING", 0),
        )
        view = memoryview(data)
        while view:
            count = os.write(descriptor, view)
            if count <= 0:
                _fail("sealed-input-write-failed")
            view = view[count:]
        os.lseek(descriptor, 0, os.SEEK_SET)
        fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, _REQUIRED_SEALS)
        if fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) != _REQUIRED_SEALS:
            _fail("sealed-input-seal-failed")
        return descriptor
    except ContainerGateError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except (AttributeError, OSError):
        if descriptor >= 0:
            os.close(descriptor)
        _fail("sealed-input-unavailable")


def _revalidate_sealed_memfd(descriptor: int, expected: bytes) -> None:
    try:
        if (
            fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) != _REQUIRED_SEALS
            or os.fstat(descriptor).st_size != len(expected)
        ):
            _fail("sealed-input-mutated")
        os.lseek(descriptor, 0, os.SEEK_SET)
        observed = bytearray()
        while len(observed) < len(expected):
            chunk = os.read(descriptor, min(65_536, len(expected) - len(observed)))
            if not chunk:
                _fail("sealed-input-mutated")
            observed.extend(chunk)
        if bytes(observed) != expected or os.read(descriptor, 1):
            _fail("sealed-input-mutated")
        os.lseek(descriptor, 0, os.SEEK_SET)
    except ContainerGateError:
        raise
    except OSError:
        _fail("sealed-input-mutated")


def _default_runner(command: Sequence[str], environment: Mapping[str, str]) -> CommandResult:
    try:
        result = subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(environment),
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        _fail("docker-command-failed")
    if len(result.stdout) > MAX_OUTPUT_BYTES or len(result.stderr) > MAX_OUTPUT_BYTES:
        _fail("docker-command-output-oversized")
    return CommandResult(result.returncode, result.stdout, result.stderr)


def _run(
    runner: CommandRunner,
    command: Sequence[str],
    environment: Mapping[str, str],
) -> CommandResult:
    try:
        result = runner(tuple(command), environment)
    except ContainerGateError:
        raise
    except Exception:
        _fail("docker-command-failed")
    if (
        not isinstance(result, CommandResult)
        or not isinstance(result.returncode, int)
        or isinstance(result.returncode, bool)
        or len(result.stdout) > MAX_OUTPUT_BYTES
        or len(result.stderr) > MAX_OUTPUT_BYTES
    ):
        _fail("docker-command-result-invalid")
    return result


def _parse_build_info(
    output: bytes,
    *,
    component: str,
    toolchain: str,
    source_manifest_digest: str,
) -> Mapping[str, object]:
    if not output.endswith(b"\n") or output.count(b"\n") != 1:
        _fail("native-self-test-output-invalid")
    document = _load_json(output[:-1], "native-self-test-output-invalid")
    if not isinstance(document, dict) or set(document) != BUILD_INFO_KEYS:
        _fail("native-self-test-output-invalid")
    expected = {
        "schema": "propertyquarry.release-control.native-build-info.v2",
        "version": 2,
        "component": component,
        "toolchain": toolchain,
        "source_manifest_digest": source_manifest_digest,
        "scratch_execution_contract": (
            oci.package_payload.NATIVE_SCRATCH_EXECUTION["contract"]
        ),
        "authoritative": False,
        "production_ready": False,
        "performs_effects": False,
        "self_test": True,
    }
    if document != expected:
        _fail("native-self-test-output-invalid")
    return document


def _parse_native_build_receipt(path: Path) -> tuple[bytes, str, str]:
    receipt_bytes = _read_regular(path, maximum=65_536, mode=0o644)
    receipt = _load_digest_bound_json(
        receipt_bytes, "native-build-receipt-invalid"
    )
    try:
        source_manifest_digest = receipt["source_manifest_sha256"]  # type: ignore[index]
        toolchain_description = receipt["toolchain"]  # type: ignore[index]
        scratch_execution = receipt["scratch_execution"]  # type: ignore[index]
        build_flags = receipt["build_flags"]  # type: ignore[index]
        ldflags = receipt["ldflags"]  # type: ignore[index]
    except (KeyError, TypeError):
        _fail("native-build-receipt-invalid")
    if (
        not isinstance(source_manifest_digest, str)
        or _DIGEST_RE.fullmatch(source_manifest_digest) is None
        or not isinstance(toolchain_description, str)
        or not toolchain_description.endswith(" linux/amd64")
        or scratch_execution != oci.package_payload.NATIVE_SCRATCH_EXECUTION
        or build_flags
        != ["-mod=readonly", "-trimpath", "-buildvcs=false", "-buildmode=exe"]
        or ldflags
        != (
            "-buildid= -linkmode=internal -X propertyquarry.local/"
            "release-control-v2/internal/"
            "releasecontrol.SourceManifestDigest="
            + source_manifest_digest
            + " -X propertyquarry.local/release-control-v2/internal/"
            "releasecontrol.ScratchExecutionContract="
            + oci.package_payload.NATIVE_SCRATCH_EXECUTION["contract"]
        )
    ):
        _fail("native-build-receipt-invalid")
    return receipt_bytes, source_manifest_digest, toolchain_description.split(" ", 1)[0]


def _write_receipt(path: Path, value: bytes) -> None:
    parent = path.parent
    try:
        metadata = os.lstat(parent)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or path.exists()
        ):
            _fail("gate-receipt-parent-invalid")
    except ContainerGateError:
        raise
    except OSError:
        _fail("gate-receipt-parent-invalid")
    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(temporary_fd, 0o600)
        view = memoryview(value)
        while view:
            count = os.write(temporary_fd, view)
            if count <= 0:
                _fail("gate-receipt-write-failed")
            view = view[count:]
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = -1
        parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            oci._rename_noreplace(parent_fd, temporary.name, path.name)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except ContainerGateError:
        raise
    except OSError:
        _fail("gate-receipt-write-failed")
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if temporary.exists():
            temporary.unlink()


def run_gate(
    *,
    layout: str,
    wrapper: str,
    external_anchor: str,
    native_build_receipt: str,
    output_receipt: str,
    docker_binary: str = "/usr/bin/docker",
    docker_host: str = "unix:///var/run/docker.sock",
    command_runner: CommandRunner = _default_runner,
) -> GateResult:
    """Verify, load, run closed tests, and publish a success receipt last."""

    docker_path = Path(docker_binary)
    if not docker_path.is_absolute() or _DOCKER_HOST_RE.fullmatch(docker_host) is None:
        _fail("docker-boundary-invalid")
    native_receipt_bytes, source_manifest_digest, toolchain = (
        _parse_native_build_receipt(Path(native_build_receipt))
    )
    layout_fd = -1
    compose_fd = -1
    archive_fd = -1
    docker_config: tempfile.TemporaryDirectory[str] | None = None
    try:
        try:
            layout_path = oci.package_payload._path_text(
                layout, "oci-output-path-invalid"
            )
            layout_fd, _layout_identity = (
                oci.package_payload._open_controlled_directory(
                    layout_path, writable=False
                )
            )
        except oci.package_payload.PayloadError:
            _fail("oci-layout-open-failed")
        image_receipt = oci.verify_pinned(
            layout_fd,
            wrapper=wrapper,
            external_anchor=external_anchor,
        )
        tree_snapshot = oci._snapshot_oci_tree(layout_fd)
        if (
            _digest(tree_snapshot.compose) != image_receipt["compose_sha256"]
            or _digest(tree_snapshot.docker_archive)
            != image_receipt["docker_archive_sha256"]
        ):
            _fail("sealed-input-binding-mismatch")
        _validate_pinned_compose(tree_snapshot.compose)
        if (
            _digest(native_receipt_bytes)
            != image_receipt["native_build_receipt_sha256"]
        ):
            _fail("native-build-receipt-binding-mismatch")
        compose_fd = _sealed_memfd(
            "propertyquarry-control-plane-compose",
            tree_snapshot.compose,
        )
        archive_fd = _sealed_memfd(
            "propertyquarry-control-plane-image",
            tree_snapshot.docker_archive,
        )
        compose_path = f"/proc/{os.getpid()}/fd/{compose_fd}"
        archive_path = f"/proc/{os.getpid()}/fd/{archive_fd}"
        image_id, runtime_user = _validate_runtime_interpolations(
            image_receipt["image_id"],
            image_receipt["runtime_user"],
        )
        project = "pq-release-gate-" + secrets.token_hex(8)
        if _PROJECT_RE.fullmatch(project) is None:  # pragma: no cover - invariant
            _fail("compose-project-invalid")
        docker_config = tempfile.TemporaryDirectory(prefix="pq-docker-config-")
    except Exception:
        if docker_config is not None:
            docker_config.cleanup()
        if archive_fd >= 0:
            os.close(archive_fd)
        if compose_fd >= 0:
            os.close(compose_fd)
        if layout_fd >= 0:
            os.close(layout_fd)
        raise
    environment = {
        "DOCKER_CONFIG": docker_config.name,
        "HOME": docker_config.name,
        "PATH": "/usr/bin:/bin",
        "PROPERTYQUARRY_RELEASE_CONTROL_IMAGE": image_id,
        "PROPERTYQUARRY_RELEASE_CONTROL_USER": runtime_user,
    }
    base = [docker_binary, "--host", docker_host]
    compose = base + [
        "compose",
        "--progress",
        "quiet",
        "--project-name",
        project,
        "--file",
        compose_path,
    ]

    def run_pinned(command: Sequence[str]) -> CommandResult:
        _revalidate_sealed_memfd(compose_fd, tree_snapshot.compose)
        _revalidate_sealed_memfd(archive_fd, tree_snapshot.docker_archive)
        result = _run(command_runner, command, environment)
        _revalidate_sealed_memfd(compose_fd, tree_snapshot.compose)
        _revalidate_sealed_memfd(archive_fd, tree_snapshot.docker_archive)
        return result

    try:
        loaded = run_pinned(
            base + ["image", "load", "--input", archive_path],
        )
        if loaded.returncode != 0:
            _fail("docker-image-load-failed")
        inspected = run_pinned(
            base + ["image", "inspect", "--format", "{{.Id}}", image_id],
        )
        if inspected.returncode != 0 or inspected.stdout != image_id.encode("ascii") + b"\n":
            _fail("docker-image-id-mismatch")
        self_test_hashes: dict[str, str] = {}
        for service, component in SELF_TESTS.items():
            result = run_pinned(
                compose
                + [
                    "--profile",
                    "self-test",
                    "run",
                    "--rm",
                    "-T",
                    "--no-deps",
                    "--pull",
                    "never",
                    service,
                ],
            )
            if result.returncode != 0 or result.stderr:
                _fail("native-self-test-failed")
            _parse_build_info(
                result.stdout,
                component=component,
                toolchain=toolchain,
                source_manifest_digest=source_manifest_digest,
            )
            self_test_hashes[component] = _digest(result.stdout)
        compose_remaining = run_pinned(compose + ["ps", "--all", "--quiet"])
        if (
            compose_remaining.returncode != 0
            or compose_remaining.stdout
            or compose_remaining.stderr
        ):
            _fail("gate-container-cleanup-failed")
        refusal_results: dict[str, int] = {}
        refusal_label = f"{GATE_CONTAINER_LABEL}={project}"
        refusal_base = base + [
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
            runtime_user,
            "--workdir",
            "/",
            "--label",
            refusal_label,
        ]
        try:
            for service in REFUSAL_TESTS:
                entrypoint, arguments = REFUSAL_COMMANDS[service]
                container_name = f"{project}-{service.removesuffix('-test')}"
                result = run_pinned(
                    refusal_base
                    + [
                        "--name",
                        container_name,
                        "--entrypoint",
                        entrypoint,
                        image_id,
                        *arguments,
                    ]
                )
                if result.returncode != 50 or result.stdout or result.stderr:
                    _fail("native-refusal-test-failed")
                refusal_results[service] = result.returncode
        finally:
            labeled_remaining = run_pinned(
                base
                + [
                    "container",
                    "ls",
                    "--all",
                    "--quiet",
                    "--filter",
                    f"label={refusal_label}",
                ]
            )
            if (
                labeled_remaining.returncode != 0
                or labeled_remaining.stdout
                or labeled_remaining.stderr
            ):
                _fail("gate-container-cleanup-failed")
        final_image_receipt = oci.verify_pinned(
            layout_fd,
            wrapper=wrapper,
            external_anchor=external_anchor,
        )
        final_tree_snapshot = oci._snapshot_oci_tree(layout_fd)
        if (
            final_image_receipt != image_receipt
            or final_tree_snapshot != tree_snapshot
        ):
            _fail("pinned-layout-mutated")
    finally:
        docker_config.cleanup()
        os.close(archive_fd)
        archive_fd = -1
        os.close(compose_fd)
        compose_fd = -1
        os.close(layout_fd)
        layout_fd = -1
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "version": 2,
        "authoritative_for_local_container_gate": True,
        "external_production_authority": False,
        "public_launch_authority": False,
        "docker_store_mutated": True,
        "network_requests_permitted": False,
        "image_loaded": True,
        "all_self_tests_passed": True,
        "all_refusal_tests_passed": True,
        "no_test_containers_retained": True,
        "scratch_execution_contract": (
            oci.package_payload.NATIVE_SCRATCH_EXECUTION["contract"]
        ),
        "scratch_execution_contract_verified": True,
        "image_id": image_id,
        "image_digest": image_receipt["image_digest"],
        "docker_archive_sha256": image_receipt["docker_archive_sha256"],
        "materialization_receipt_sha256": _digest(
            tree_snapshot.materialization_receipt
        ),
        "authentication_sha256": image_receipt["authentication_sha256"],
        "authentication_signature_sha256": image_receipt[
            "authentication_signature_sha256"
        ],
        "native_build_receipt_sha256": _digest(native_receipt_bytes),
        "source_manifest_sha256": source_manifest_digest,
        "compose_sha256": image_receipt["compose_sha256"],
        "runtime_user": runtime_user,
        "self_test_stdout_sha256": self_test_hashes,
        "refusal_exit_codes": refusal_results,
    }
    receipt_bytes = _canonical(receipt)
    receipt_path = Path(output_receipt)
    _write_receipt(receipt_path, receipt_bytes)
    return GateResult(str(receipt_path), _digest(receipt_bytes), image_id)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Opt-in PropertyQuarry local Docker image/self-test gate."
    )
    parser.add_argument("--layout", required=True)
    parser.add_argument("--wrapper", required=True)
    parser.add_argument("--external-anchor", required=True)
    parser.add_argument("--native-build-receipt", required=True)
    parser.add_argument("--output-receipt", required=True)
    parser.add_argument("--docker-binary", default="/usr/bin/docker")
    parser.add_argument("--docker-host", default="unix:///var/run/docker.sock")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = run_gate(
            layout=arguments.layout,
            wrapper=arguments.wrapper,
            external_anchor=arguments.external_anchor,
            native_build_receipt=arguments.native_build_receipt,
            output_receipt=arguments.output_receipt,
            docker_binary=arguments.docker_binary,
            docker_host=arguments.docker_host,
        )
    except (ContainerGateError, oci.MaterializationError) as error:
        print(f"error:{error.code}", file=os.sys.stderr)
        return 1
    print(
        _canonical(
            {
                "image_id": result.image_id,
                "receipt": result.receipt,
                "receipt_sha256": result.receipt_sha256,
            }
        ).decode("ascii")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
