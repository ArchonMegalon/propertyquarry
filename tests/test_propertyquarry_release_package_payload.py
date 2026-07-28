from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import shlex
import stat
import struct

import pytest

from scripts import propertyquarry_release_installation_model as installation
from scripts import propertyquarry_release_package_payload as payload


ROOT = Path(__file__).resolve().parents[1]
REAL_NATIVE_BUNDLE = (
    ROOT / "build" / "propertyquarry-release-control-v2" / "linux-amd64"
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _sha(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _write(path: Path, value: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    path.chmod(mode)


def _synthetic_static_elf(label: str) -> bytes:
    payload_bytes = b"\xc3" + label.encode("ascii")
    program_offset = 64
    program_count = 2
    code_offset = program_offset + program_count * 56
    total = code_offset + len(payload_bytes)
    identity = b"\x7fELF\x02\x01\x01" + b"\x00" * 9
    header = struct.pack(
        "<16sHHIQQQIHHHHHH",
        identity,
        2,
        62,
        1,
        0x400000 + code_offset,
        program_offset,
        0,
        0,
        64,
        56,
        program_count,
        0,
        0,
        0,
    )
    load = struct.pack(
        "<IIQQQQQQ", 1, 5, 0, 0x400000, 0x400000, total, total, 0x1000
    )
    stack = struct.pack("<IIQQQQQQ", 0x6474E551, 6, 0, 0, 0, 0, 0, 8)
    return header + load + stack + payload_bytes


def _invalid_static_elf(case: str) -> bytes:
    data = bytearray(_synthetic_static_elf(case))
    if case == "et-dyn":
        struct.pack_into("<H", data, 16, 3)
    elif case == "pt-interp":
        struct.pack_into("<I", data, 64, 3)
    elif case == "pt-dynamic":
        struct.pack_into("<I", data, 64, 2)
    elif case == "writable-executable-load":
        struct.pack_into("<I", data, 68, 7)
    elif case == "executable-stack":
        struct.pack_into("<I", data, 64 + 56 + 4, 7)
    elif case in {"dynamic-section", "interp-section"}:
        names = b"\x00.shstrtab\x00.dynamic\x00.interp\x00"
        names_offset = len(data)
        data.extend(names)
        section_offset = len(data)
        section_name = 11 if case == "dynamic-section" else 20
        section_type = 6 if case == "dynamic-section" else 1
        sections = (
            struct.pack("<IIQQQQIIQQ", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
            struct.pack(
                "<IIQQQQIIQQ",
                1,
                3,
                0,
                0,
                names_offset,
                len(names),
                0,
                0,
                1,
                0,
            ),
            struct.pack(
                "<IIQQQQIIQQ",
                section_name,
                section_type,
                0,
                0,
                0,
                0,
                0,
                0,
                1,
                0,
            ),
        )
        data.extend(b"".join(sections))
        struct.pack_into("<Q", data, 40, section_offset)
        struct.pack_into("<HHH", data, 58, 64, len(sections), 1)
    elif case == "truncated-program-table":
        return bytes(data[:80])
    elif case == "not-elf":
        data[:4] = b"NOPE"
    else:  # pragma: no cover - the parametrization is closed below.
        raise AssertionError(case)
    return bytes(data)


def test_static_elf_byte_contract_accepts_loaderless_et_exec() -> None:
    payload._validate_static_elf_bytes(_synthetic_static_elf("valid"))


@pytest.mark.parametrize(
    "case",
    (
        "et-dyn",
        "pt-interp",
        "pt-dynamic",
        "writable-executable-load",
        "executable-stack",
        "dynamic-section",
        "interp-section",
        "truncated-program-table",
        "not-elf",
    ),
)
def test_static_elf_byte_contract_rejects_unsafe_inputs(case: str) -> None:
    with pytest.raises(payload.PayloadError) as error:
        payload._validate_static_elf_bytes(_invalid_static_elf(case))
    assert error.value.code == "native-static-elf-invalid"


def _controller_config() -> dict[str, object]:
    def mediator(name: str) -> dict[str, str]:
        return {
            "endpoint": f"https://{name}.mediator.invalid/v2",
            "trust_root_path": (
                "/etc/propertyquarry-release-control/trust.d/"
                "resource-mediator-v2.pem"
            ),
            "credential_name": "resource-mediator-client",
        }

    authority = lambda name, trust, credential: {  # noqa: E731
        "endpoint": f"https://{name}.invalid/v2",
        "trust_root_path": (
            f"/etc/propertyquarry-release-control/trust.d/{trust}"
        ),
        "credential_name": credential,
    }
    return {
        "schema": "propertyquarry.release-control.controller-config.v2",
        "version": 2,
        "environment": "propertyquarry-production",
        "identity_policy": {
            "repository": "ArchonMegalon/property",
            "ref": "refs/heads/main",
            "workflow_ref": (
                "ArchonMegalon/property/.github/workflows/"
                "smoke-runtime.yml@refs/heads/main"
            ),
            "workflow_sha": "a" * 40,
            "job": "propertyquarry-release-v2",
            "environment": "propertyquarry-production",
        },
        "oidc": {
            "allowed_request_url_origin": (
                "https://vstoken.actions.githubusercontent.com"
            ),
            "audience": "propertyquarry-release-control-v2",
        },
        "root_policy_path": "/etc/propertyquarry-release-control/policy-v2.json",
        "package_trust_root_path": (
            "/etc/propertyquarry-release-control/trust.d/package-authority-v2.pem"
        ),
        "authorities": {
            "request": {
                "endpoint": "https://request-authority.invalid/v2",
                "request_trust_root_path": (
                    "/etc/propertyquarry-release-control/trust.d/"
                    "request-authority-v2.pem"
                ),
                "response_trust_root_path": (
                    "/etc/propertyquarry-release-control/trust.d/"
                    "response-authority-v2.pem"
                ),
                "credential_name": "request-authority-client",
            },
            "lifecycle_cas": authority(
                "lifecycle-cas",
                "lifecycle-cas-v2.pem",
                "lifecycle-cas-client",
            ),
            "evidence_store": authority(
                "evidence",
                "evidence-authority-v2.pem",
                "evidence-store-client",
            ),
        },
        "resource_mediators": {
            name: mediator(name.replace("_", "-"))
            for name in (
                "database",
                "launch_authority",
                "monitoring_delivery",
                "overlay",
                "public_tour",
                "runtime",
                "traffic",
            )
        },
        "state": {
            "cache_path": "/var/lib/propertyquarry-release-control-v2/cache",
            "journal_path": "/var/lib/propertyquarry-release-control-v2/journal",
            "receipt_path": "/var/lib/propertyquarry-release-control-v2/receipts",
        },
        "limits": {
            "identity_token_limit_bytes": 16384,
            "request_limit_bytes": 1048576,
            "response_limit_bytes": 1048576,
            "diagnostic_limit_bytes": 65536,
            "callback_timeout_seconds": 30,
            "operation_timeout_seconds": 9600,
        },
    }


def _watchdog_config() -> dict[str, object]:
    return {
        "schema": "propertyquarry.release-control.watchdog-config.v2",
        "version": 2,
        "environment": "propertyquarry-production",
        "root_policy_path": "/etc/propertyquarry-release-control/policy-v2.json",
        "lifecycle_cas": {
            "endpoint": "https://lifecycle-cas.invalid/v2",
            "trust_root_path": (
                "/etc/propertyquarry-release-control/trust.d/lifecycle-cas-v2.pem"
            ),
            "credential_name": "watchdog-takeover-client",
        },
        "resource_recovery": {
            "endpoint": "https://resource-recovery.invalid/v2",
            "trust_root_path": (
                "/etc/propertyquarry-release-control/trust.d/"
                "resource-mediator-v2.pem"
            ),
            "credential_name": "resource-recovery-client",
        },
        "resource_kinds": [
            "database",
            "launch-authority",
            "monitoring-delivery",
            "overlay",
            "public-tour",
            "runtime",
            "traffic",
        ],
        "state": {
            "cache_path": "/var/lib/propertyquarry-release-watchdog-v2/cache"
        },
        "limits": {
            "poll_interval_seconds": 10,
            "callback_timeout_seconds": 30,
            "reconciliation_timeout_seconds": 1800,
        },
    }


def _root_policy() -> dict[str, object]:
    return {
        "schema": "propertyquarry.release-root-policy.v2",
        "identity": {
            "audience": "propertyquarry-release-control-v2",
            "repository": "ArchonMegalon/property",
            "ref": "refs/heads/main",
            "candidate_sha": "b" * 40,
            "workflow_ref": (
                "ArchonMegalon/property/.github/workflows/"
                "smoke-runtime.yml@refs/heads/main"
            ),
            "workflow_sha": "a" * 40,
            "run_id": "123456789",
            "run_attempt": 1,
            "job": "propertyquarry-release-v2",
            "environment": "propertyquarry-production",
        },
        "required_checks": ["release-evidence", "tour-control", "deploy-guard"],
        "decision_policy_digest": "sha256:" + "c" * 64,
        "max_request_ttl": 900,
        "max_preflight_validity": 3600,
    }


def build_private_fixture(root: Path) -> Path:
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    _write(root / "controller-v2.json", _canonical(_controller_config()), 0o600)
    _write(root / "watchdog-v2.json", _canonical(_watchdog_config()), 0o600)
    _write(root / "policy-v2.json", _canonical(_root_policy()), 0o600)
    for relative in payload.PRIVATE_FILES:
        if relative.startswith("trust.d/"):
            label = relative.removeprefix("trust.d/").encode("ascii")
            _write(
                root / relative,
                b"-----BEGIN PUBLIC KEY-----\n" + label.hex().encode() + b"\n"
                b"-----END PUBLIC KEY-----\n",
                0o600,
            )
    (root / "trust.d").chmod(0o700)
    return root


def _native_receipt(binary_data: dict[str, bytes]) -> dict[str, object]:
    source_digest = "sha256:" + "d" * 64
    return {
        "schema": "propertyquarry.release-control.native-build-receipt.v2",
        "authoritative": False,
        "production_ready": False,
        "reproducible_double_build": True,
        "distinct_absolute_source_roots": True,
        "isolated_build_caches": True,
        "independent_toolchain_extractions": True,
        "go_subprocess_environment_allowlisted": True,
        "go_subprocess_inherited_environment_cleared": True,
        "module_network_resolution_disabled": True,
        "host_network_namespace_isolated": False,
        "go_tests_passed_in_both_builds": True,
        "scratch_execution": dict(payload.NATIVE_SCRATCH_EXECUTION),
        "source_manifest_reverified_after_build": True,
        "receipt_published_last": True,
        "root_install_performed": False,
        "package_signature_verified": False,
        "builder_identity_authenticated": False,
        "toolchain": "go1.26.5 linux/amd64",
        "toolchain_archive_bytes": 66_879_095,
        "toolchain_archive_sha256": (
            "5c2c3b16caefa1d968a94c1daca04a7ca301a496d9b086e17ad77bb81393f053"
        ),
        "go_binary_sha256": (
            "sha256:8da5fd321795754b994c64e3eb8a5a14ff47bd285559a7e876f3c79abafc67f9"
        ),
        "source_manifest_sha256": source_digest,
        "build_flags": [
            "-mod=readonly",
            "-trimpath",
            "-buildvcs=false",
            "-buildmode=exe",
        ],
        "ldflags": (
            "-buildid= -linkmode=internal -X propertyquarry.local/"
            "release-control-v2/internal/"
            "releasecontrol.SourceManifestDigest="
            + source_digest
            + " -X propertyquarry.local/release-control-v2/internal/"
            "releasecontrol.ScratchExecutionContract="
            + payload.NATIVE_SCRATCH_EXECUTION["contract"]
        ),
        "build_environment": dict(payload._NATIVE_BUILD_ENVIRONMENT),
        "binary_mode": "0755",
        "binary_sizes": {name: len(binary_data[name]) for name in payload._NATIVE_NAMES},
        "binaries": {name: _sha(binary_data[name]) for name in payload._NATIVE_NAMES},
    }


def build_synthetic_native_fixture(root: Path) -> Path:
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    marker = root.with_name(root.name + "-executed-marker")
    binary_data = {
        name: _synthetic_static_elf(name) for name in payload._NATIVE_NAMES
    }
    for name, data in binary_data.items():
        _write(root / name, data, 0o755)
    _write(root / "build-receipt.json", _canonical(_native_receipt(binary_data)), 0o644)
    return root


def _assemble(
    tmp_path: Path,
    *,
    native: Path | None = None,
    private: Path | None = None,
    name: str = "payload",
    phase_hook=None,
) -> Path:
    native = native or build_synthetic_native_fixture(tmp_path / f"native-{name}")
    private = private or build_private_fixture(tmp_path / f"private-{name}")
    output = tmp_path / name
    payload.assemble_payload(
        native_bundle=str(native),
        private_bundle=str(private),
        service_gid=1999,
        output=str(output),
        phase_hook=phase_hook,
    )
    return output


def _tree_bytes(root: Path) -> dict[str, tuple[bytes, int]]:
    result: dict[str, tuple[bytes, int]] = {}
    for directory, _directories, files in os.walk(root):
        for name in files:
            path = Path(directory) / name
            result[str(path.relative_to(root))] = (
                path.read_bytes(),
                stat.S_IMODE(path.stat().st_mode),
            )
    return result


def test_exact_19_role_payload_is_deterministic_and_explicitly_non_authoritative(
    tmp_path: Path,
) -> None:
    native = build_synthetic_native_fixture(tmp_path / "native")
    private = build_private_fixture(tmp_path / "private")
    old_umask = os.umask(0o077)
    try:
        first = _assemble(
            tmp_path,
            native=native,
            private=private,
            name="payload-one",
        )
        second = _assemble(
            tmp_path,
            native=native,
            private=private,
            name="payload-two",
        )
    finally:
        os.umask(old_umask)

    assert _tree_bytes(first) == _tree_bytes(second)
    assert not native.with_name(native.name + "-executed-marker").exists()
    directory_metadata: dict[str, tuple[int, int, int]] = {}
    for directory, directories, _files in os.walk(first):
        path = Path(directory)
        metadata = path.stat()
        relative = "." if path == first else str(path.relative_to(first))
        directory_metadata[relative] = (
            stat.S_IMODE(metadata.st_mode),
            metadata.st_uid,
            metadata.st_gid,
        )
        assert set(directories) <= {
            child.name for child in path.iterdir() if child.is_dir()
        }
    expected_directory_modes = {
        ".": 0o700,
        **payload._final_directory_modes(),
    }
    assert set(directory_metadata) == set(expected_directory_modes)
    for relative, mode in expected_directory_modes.items():
        assert directory_metadata[relative] == (mode, os.geteuid(), os.getegid())
    rootfs_files = {
        "/" + relative
        for relative in _tree_bytes(first / "rootfs")
    }
    assert rootfs_files == {contract.path for contract in installation.ROLE_CONTRACTS}

    manifest_raw = (first / "installation-manifest.v2.json").read_bytes()
    manifest = json.loads(manifest_raw)
    assert manifest_raw == installation.canonical_manifest_bytes(manifest)
    assert [entry["role"] for entry in manifest["roles"]] == list(
        installation.ROLE_NAMES
    )
    for entry, contract in zip(
        manifest["roles"], installation.ROLE_CONTRACTS, strict=True
    ):
        assert entry["path"] == contract.path
        assert entry["mode"] == contract.mode
        assert entry["uid"] == 0
        assert entry["gid"] == (
            1999 if contract.role in installation.SERVICE_GROUP_ROLES else 0
        )

    receipt_raw = (first / "package-payload-receipt.v2.json").read_bytes()
    receipt = json.loads(receipt_raw)
    assert receipt_raw == _canonical(receipt)
    assert set(receipt) == {
        "schema",
        "version",
        "authoritative",
        "production_ready",
        "readiness_authority",
        "payload_signed",
        "installs_or_repairs",
        "writes_payload_output",
        "payload_material_writes_only_within_output_parent",
        "performs_installation_writes",
        "root_install_performed",
        "package_signature_verified",
        "verifies_signatures",
        "builder_identity_authenticated",
        "input_authentication_verified",
        "native_bundle_authenticated",
        "private_material_authenticated",
        "production_ownership_verified",
        "receipt_published_last",
        "role_count",
        "service_gid_projection",
        "input_integrity",
        "installation_manifest_sha256",
        "simulation_audit",
    }
    assert set(receipt["input_integrity"]) == {
        "native_build_receipt_sha256",
        "native_bundle_material_sha256",
        "package_templates_material_sha256",
        "private_material_sha256",
    }
    assert set(receipt["simulation_audit"]) == {
        "mode",
        "ownership",
        "disposition",
        "all_files_match_expectations",
        "observed_role_count",
        "blocker_count",
        "authoritative",
        "performs_writes",
        "verifies_signatures",
        "readiness_authority",
    }
    for claim in (
        "authoritative",
        "production_ready",
        "readiness_authority",
        "payload_signed",
        "installs_or_repairs",
        "performs_installation_writes",
        "root_install_performed",
        "package_signature_verified",
        "verifies_signatures",
        "builder_identity_authenticated",
        "input_authentication_verified",
        "native_bundle_authenticated",
        "private_material_authenticated",
        "production_ownership_verified",
    ):
        assert receipt[claim] is False
    assert receipt["writes_payload_output"] is True
    assert receipt["payload_material_writes_only_within_output_parent"] is True
    assert receipt["receipt_published_last"] is True
    assert receipt["role_count"] == 19
    assert receipt["simulation_audit"] == {
        "mode": "simulation",
        "ownership": "actual-staging-ownership-projection-only",
        "disposition": "matches-expectations-non-authoritative",
        "all_files_match_expectations": True,
        "observed_role_count": 19,
        "blocker_count": 0,
        "authoritative": False,
        "performs_writes": False,
        "verifies_signatures": False,
        "readiness_authority": False,
    }
    assert not any("tmp" in key.lower() for key in receipt)


def test_stable_real_native_bundle_is_consumed_as_data_and_never_executed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = set(payload.NATIVE_FILES)
    if not REAL_NATIVE_BUNDLE.is_dir() or {
        path.name for path in REAL_NATIVE_BUNDLE.iterdir()
    } != expected:
        if os.environ.get("PROPERTYQUARRY_REQUIRE_REAL_NATIVE_BUNDLE") == "1":
            pytest.fail("required exact real native bundle is absent")
        pytest.skip("explicit real native payload gate requires the generated bundle")
    private = build_private_fixture(tmp_path / "private")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("native intake must never execute a process")

    monkeypatch.setattr(os, "system", forbidden)
    output = _assemble(
        tmp_path,
        native=REAL_NATIVE_BUNDLE,
        private=private,
        name="real-native-payload",
    )

    receipt = json.loads(
        (output / "package-payload-receipt.v2.json").read_bytes()
    )
    assert receipt["input_integrity"]["native_build_receipt_sha256"] == (
        _sha((REAL_NATIVE_BUNDLE / "build-receipt.json").read_bytes())
    )
    assert receipt["native_bundle_authenticated"] is False


@pytest.mark.parametrize("gid", [True, False, 0, -1, 1 << 31, "1999"])
def test_service_gid_is_one_positive_exact_int_before_any_write(
    tmp_path: Path, gid: object
) -> None:
    output = tmp_path / "payload"
    with pytest.raises(payload.PayloadError, match="service-gid-invalid"):
        payload.assemble_payload(
            native_bundle=str(tmp_path / "absent-native"),
            private_bundle=str(tmp_path / "absent-private"),
            service_gid=gid,
            output=str(output),
        )
    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


def test_existing_output_is_never_replaced(tmp_path: Path) -> None:
    native = build_synthetic_native_fixture(tmp_path / "native")
    private = build_private_fixture(tmp_path / "private")
    output = tmp_path / "payload"
    output.mkdir()
    marker = output / "owner-data"
    marker.write_text("preserve", encoding="utf-8")

    with pytest.raises(payload.PayloadError, match="output-exists"):
        payload.assemble_payload(
            native_bundle=str(native),
            private_bundle=str(private),
            service_gid=1999,
            output=str(output),
        )

    assert marker.read_text(encoding="utf-8") == "preserve"


@pytest.mark.parametrize(
    "phase",
    [
        "inputs-snapshotted",
        "native-integrity-validated",
        "role-files-copied",
        "manifest-written",
        "simulation-audited",
        "inputs-reverified",
        "receipt-written-last",
    ],
)
def test_crash_before_atomic_publish_leaves_no_output_or_temp(
    tmp_path: Path, phase: str
) -> None:
    native = build_synthetic_native_fixture(tmp_path / "native")
    private = build_private_fixture(tmp_path / "private")
    output = tmp_path / "payload"

    def crash(observed: str) -> None:
        if observed == phase:
            raise RuntimeError("injected crash")

    with pytest.raises(RuntimeError, match="injected crash"):
        payload.assemble_payload(
            native_bundle=str(native),
            private_bundle=str(private),
            service_gid=1999,
            output=str(output),
            phase_hook=crash,
        )

    assert not output.exists()
    assert not any(".assembling." in path.name for path in tmp_path.iterdir())


@pytest.mark.parametrize(
    "mutation",
    ["missing", "extra", "symlink", "hardlink", "fifo", "empty"],
)
def test_private_bundle_is_an_exact_descriptor_safe_regular_file_set(
    tmp_path: Path, mutation: str
) -> None:
    native = build_synthetic_native_fixture(tmp_path / "native")
    private = build_private_fixture(tmp_path / "private")
    target = private / "trust.d/request-authority-v2.pem"
    if mutation == "missing":
        target.unlink()
    elif mutation == "extra":
        _write(private / "trust.d/attacker.pem", b"attacker\n", 0o600)
    elif mutation == "symlink":
        target.unlink()
        target.symlink_to(private / "policy-v2.json")
    elif mutation == "hardlink":
        target.unlink()
        os.link(private / "policy-v2.json", target)
    elif mutation == "fifo":
        target.unlink()
        os.mkfifo(target, 0o600)
    elif mutation == "empty":
        target.write_bytes(b"")
        target.chmod(0o600)

    with pytest.raises(payload.PayloadError):
        payload.assemble_payload(
            native_bundle=str(native),
            private_bundle=str(private),
            service_gid=1999,
            output=str(tmp_path / "payload"),
        )
    assert not (tmp_path / "payload").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown-receipt-key",
        "duplicate-receipt-key",
        "nonfinite-receipt",
        "binary-digest",
        "binary-size",
        "binary-mode",
        "authority-forgery",
    ],
)
def test_native_bundle_receipt_and_bytes_are_closed_and_never_executed(
    tmp_path: Path, mutation: str
) -> None:
    native = build_synthetic_native_fixture(tmp_path / "native")
    private = build_private_fixture(tmp_path / "private")
    receipt_path = native / "build-receipt.json"
    receipt = json.loads(receipt_path.read_bytes())
    controller = native / "propertyquarry-release-controller-v2"
    if mutation == "unknown-receipt-key":
        receipt["attacker"] = True
        receipt_path.write_bytes(_canonical(receipt))
    elif mutation == "duplicate-receipt-key":
        raw = receipt_path.read_bytes()
        receipt_path.write_bytes(raw.replace(b'{"authoritative":false', b'{"authoritative":false,"authoritative":false', 1))
    elif mutation == "nonfinite-receipt":
        receipt_path.write_bytes(
            receipt_path.read_bytes()[:-1] + b',"attacker":NaN}'
        )
    elif mutation == "binary-digest":
        raw = bytearray(controller.read_bytes())
        raw[-1] ^= 1
        controller.write_bytes(raw)
    elif mutation == "binary-size":
        controller.write_bytes(controller.read_bytes() + b"x")
    elif mutation == "binary-mode":
        controller.chmod(0o700)
    elif mutation == "authority-forgery":
        receipt["builder_identity_authenticated"] = True
        receipt_path.write_bytes(_canonical(receipt))
    receipt_path.chmod(0o644)
    controller.chmod(0o700 if mutation == "binary-mode" else 0o755)

    with pytest.raises(payload.PayloadError):
        payload.assemble_payload(
            native_bundle=str(native),
            private_bundle=str(private),
            service_gid=1999,
            output=str(tmp_path / "payload"),
        )
    assert not native.with_name(native.name + "-executed-marker").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate-json-key",
        "control-endpoint",
        "unknown-config-key",
        "noncanonical-policy",
        "invalid-policy-digest",
    ],
)
def test_private_json_and_root_policy_are_strict_and_errors_are_secret_free(
    tmp_path: Path, mutation: str
) -> None:
    native = build_synthetic_native_fixture(tmp_path / "native")
    private = build_private_fixture(tmp_path / "private")
    controller_path = private / "controller-v2.json"
    policy_path = private / "policy-v2.json"
    secret = "DO-NOT-LOG-PRIVATE-MATERIAL"
    if mutation == "duplicate-json-key":
        controller_path.write_bytes(
            b'{"schema":"' + secret.encode() + b'","schema":"duplicate"}'
        )
    elif mutation == "control-endpoint":
        value = json.loads(controller_path.read_bytes())
        value["authorities"]["request"]["endpoint"] = (
            "https://request.invalid/v2\n" + secret
        )
        controller_path.write_bytes(_canonical(value))
    elif mutation == "unknown-config-key":
        value = json.loads(controller_path.read_bytes())
        value["private_secret"] = secret
        controller_path.write_bytes(_canonical(value))
    elif mutation == "noncanonical-policy":
        value = _root_policy()
        value["identity"]["audience"] = secret
        policy_path.write_text(
            json.dumps(value, indent=2),
            encoding="utf-8",
        )
    elif mutation == "invalid-policy-digest":
        value = _root_policy()
        value["decision_policy_digest"] = secret
        policy_path.write_bytes(_canonical(value))
    controller_path.chmod(0o600)
    policy_path.chmod(0o600)

    with pytest.raises(payload.PayloadError) as failure:
        payload.assemble_payload(
            native_bundle=str(native),
            private_bundle=str(private),
            service_gid=1999,
            output=str(tmp_path / "payload"),
        )
    assert secret not in str(failure.value)
    assert not (tmp_path / "payload").exists()


def test_same_size_source_mutation_is_detected_before_copy(
    tmp_path: Path,
) -> None:
    native = build_synthetic_native_fixture(tmp_path / "native")
    private = build_private_fixture(tmp_path / "private")
    target = private / "trust.d/package-authority-v2.pem"

    def mutate(phase: str) -> None:
        if phase == "inputs-snapshotted":
            raw = bytearray(target.read_bytes())
            raw[len(raw) // 2] ^= 1
            target.write_bytes(raw)
            target.chmod(0o600)

    with pytest.raises(payload.PayloadError, match="private-bundle-concurrent-mutation"):
        payload.assemble_payload(
            native_bundle=str(native),
            private_bundle=str(private),
            service_gid=1999,
            output=str(tmp_path / "payload"),
            phase_hook=mutate,
        )


def _assembling_directory(parent: Path, output_name: str = "payload") -> Path:
    matches = [
        path
        for path in parent.iterdir()
        if path.name.startswith(f".{output_name}.assembling.")
    ]
    assert len(matches) == 1
    return matches[0]


@pytest.mark.parametrize(
    "target",
    ["role", "manifest", "receipt", "directory-mode", "directory-replacement"],
)
def test_final_snapshot_rejects_every_late_payload_mutation(
    tmp_path: Path, target: str
) -> None:
    native = build_synthetic_native_fixture(tmp_path / "native")
    private = build_private_fixture(tmp_path / "private")

    def mutate(phase: str) -> None:
        expected_phase = (
            "receipt-written-last" if target == "receipt" else "simulation-audited"
        )
        if phase != expected_phase:
            return
        temporary = _assembling_directory(tmp_path)
        if target == "role":
            path = (
                temporary
                / "rootfs/etc/propertyquarry-release-control/controller-v2.json"
            )
            raw = bytearray(path.read_bytes())
            raw[-1] ^= 1
            path.write_bytes(raw)
            path.chmod(0o640)
        elif target == "manifest":
            path = temporary / "installation-manifest.v2.json"
            raw = bytearray(path.read_bytes())
            raw[-1] ^= 1
            path.write_bytes(raw)
            path.chmod(0o644)
        elif target == "receipt":
            path = temporary / "package-payload-receipt.v2.json"
            raw = bytearray(path.read_bytes())
            raw[-1] ^= 1
            path.write_bytes(raw)
            path.chmod(0o644)
        elif target == "directory-mode":
            (temporary / "rootfs/etc/propertyquarry-release-control").chmod(0o700)
        elif target == "directory-replacement":
            path = temporary / "rootfs/usr/lib/systemd/system"
            detached = path.with_name("system.detached")
            path.rename(detached)
            shutil.copytree(detached, path, copy_function=shutil.copy2)
            shutil.rmtree(detached)

    with pytest.raises(payload.PayloadError):
        payload.assemble_payload(
            native_bundle=str(native),
            private_bundle=str(private),
            service_gid=1999,
            output=str(tmp_path / "payload"),
            phase_hook=mutate,
        )
    assert not (tmp_path / "payload").exists()
    assert not any(".assembling." in path.name for path in tmp_path.iterdir())


@pytest.mark.parametrize(
    ("relative", "error"),
    [
        (
            "rootfs/etc/propertyquarry-release-control/controller-v2.json",
            "audited-file-metadata-changed",
        ),
        ("installation-manifest.v2.json", "payload-metadata-file-invalid"),
        ("package-payload-receipt.v2.json", "payload-metadata-file-invalid"),
    ],
)
def test_late_chgrp_of_role_or_metadata_file_is_rejected(
    tmp_path: Path, relative: str, error: str
) -> None:
    native = build_synthetic_native_fixture(tmp_path / "native")
    private = build_private_fixture(tmp_path / "private")
    alternate_gid = next(
        (group for group in os.getgroups() if group != os.getegid()),
        None,
    )
    if alternate_gid is None:
        pytest.skip("late-chgrp regression requires one supplemental group")

    def mutate(phase: str) -> None:
        if phase == "receipt-written-last":
            target = _assembling_directory(tmp_path) / relative
            os.chown(target, -1, alternate_gid)

    with pytest.raises(payload.PayloadError, match=error):
        payload.assemble_payload(
            native_bundle=str(native),
            private_bundle=str(private),
            service_gid=1999,
            output=str(tmp_path / "payload"),
            phase_hook=mutate,
        )
    assert not (tmp_path / "payload").exists()
    assert not any(".assembling." in path.name for path in tmp_path.iterdir())


def test_same_byte_role_inode_replacement_after_audit_is_rejected(
    tmp_path: Path,
) -> None:
    native = build_synthetic_native_fixture(tmp_path / "native")
    private = build_private_fixture(tmp_path / "private")

    def mutate(phase: str) -> None:
        if phase != "receipt-written-last":
            return
        target = (
            _assembling_directory(tmp_path)
            / "rootfs/etc/propertyquarry-release-control/controller-v2.json"
        )
        replacement = target.with_name("controller-v2.replacement")
        replacement.write_bytes(target.read_bytes())
        replacement.chmod(0o640)
        os.replace(replacement, target)

    with pytest.raises(payload.PayloadError, match="audited-file-metadata-changed"):
        payload.assemble_payload(
            native_bundle=str(native),
            private_bundle=str(private),
            service_gid=1999,
            output=str(tmp_path / "payload"),
            phase_hook=mutate,
        )
    assert not (tmp_path / "payload").exists()
    assert not any(".assembling." in path.name for path in tmp_path.iterdir())


def test_late_role_mode_change_is_rejected_without_group_prerequisite(
    tmp_path: Path,
) -> None:
    native = build_synthetic_native_fixture(tmp_path / "native")
    private = build_private_fixture(tmp_path / "private")

    def mutate(phase: str) -> None:
        if phase == "receipt-written-last":
            target = (
                _assembling_directory(tmp_path)
                / "rootfs/etc/propertyquarry-release-control/controller-v2.json"
            )
            target.chmod(0o600)

    with pytest.raises(payload.PayloadError, match="input-mode-invalid"):
        payload.assemble_payload(
            native_bundle=str(native),
            private_bundle=str(private),
            service_gid=1999,
            output=str(tmp_path / "payload"),
            phase_hook=mutate,
        )
    assert not (tmp_path / "payload").exists()
    assert not any(".assembling." in path.name for path in tmp_path.iterdir())


def test_temp_name_swap_is_not_traversed_or_deleted(tmp_path: Path) -> None:
    native = build_synthetic_native_fixture(tmp_path / "native")
    private = build_private_fixture(tmp_path / "private")
    state: dict[str, Path] = {}

    def swap(phase: str) -> None:
        if phase != "receipt-written-last":
            return
        temporary = _assembling_directory(tmp_path)
        detached = tmp_path / "forensic-original-temp"
        temporary.rename(detached)
        temporary.mkdir(mode=0o700)
        marker = temporary / "attacker-marker"
        marker.write_text("do not delete", encoding="utf-8")
        state.update(detached=detached, marker=marker)

    with pytest.raises(
        payload.PayloadError, match="temporary-cleanup-target-replaced"
    ):
        payload.assemble_payload(
            native_bundle=str(native),
            private_bundle=str(private),
            service_gid=1999,
            output=str(tmp_path / "payload"),
            phase_hook=swap,
        )

    assert state["marker"].read_text(encoding="utf-8") == "do not delete"
    assert state["detached"].is_dir()
    assert not (tmp_path / "payload").exists()


def test_output_parent_path_replacement_fails_before_path_snapshot_and_cleans_pinned_tree(
    tmp_path: Path,
) -> None:
    native = build_synthetic_native_fixture(tmp_path / "native")
    private = build_private_fixture(tmp_path / "private")
    parent = tmp_path / "controlled-parent"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    detached = tmp_path / "detached-controlled-parent"

    def replace(phase: str) -> None:
        if phase != "receipt-written-last":
            return
        parent.rename(detached)
        parent.mkdir(mode=0o700)
        parent.chmod(0o700)
        (parent / "attacker-marker").write_text("preserve", encoding="utf-8")

    with pytest.raises(payload.PayloadError, match="output-parent-replaced"):
        payload.assemble_payload(
            native_bundle=str(native),
            private_bundle=str(private),
            service_gid=1999,
            output=str(parent / "payload"),
            phase_hook=replace,
        )

    assert (parent / "attacker-marker").read_text(encoding="utf-8") == "preserve"
    assert not any(".assembling." in path.name for path in detached.iterdir())
    assert not (parent / "payload").exists()


def test_rename_noreplace_wins_output_creation_race_without_replacement(
    tmp_path: Path,
) -> None:
    native = build_synthetic_native_fixture(tmp_path / "native")
    private = build_private_fixture(tmp_path / "private")
    output = tmp_path / "payload"

    def race(phase: str) -> None:
        if phase == "receipt-written-last":
            output.mkdir(mode=0o700)
            (output / "owner-data").write_text("preserve", encoding="utf-8")

    with pytest.raises(payload.PayloadError, match="output-exists"):
        payload.assemble_payload(
            native_bundle=str(native),
            private_bundle=str(private),
            service_gid=1999,
            output=str(output),
            phase_hook=race,
        )
    assert (output / "owner-data").read_text(encoding="utf-8") == "preserve"
    assert not any(".assembling." in path.name for path in tmp_path.iterdir())


def test_post_rename_parent_fsync_failure_is_typed_and_leaves_quarantine_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = build_synthetic_native_fixture(tmp_path / "native")
    private = build_private_fixture(tmp_path / "private")
    renamed = False
    original_rename = payload._rename_noreplace
    original_fsync = os.fsync

    def rename(*args, **kwargs) -> None:
        nonlocal renamed
        original_rename(*args, **kwargs)
        renamed = True

    def fsync(descriptor: int) -> None:
        if renamed:
            raise OSError("injected durability uncertainty")
        original_fsync(descriptor)

    monkeypatch.setattr(payload, "_rename_noreplace", rename)
    monkeypatch.setattr(os, "fsync", fsync)
    output = tmp_path / "payload"
    with pytest.raises(payload.PayloadError, match="output-parent-durability-unknown"):
        payload.assemble_payload(
            native_bundle=str(native),
            private_bundle=str(private),
            service_gid=1999,
            output=str(output),
        )

    assert output.is_dir()
    assert len(_tree_bytes(output / "rootfs")) == 19


@pytest.mark.parametrize("target", ["native-receipt", "private-trust-root"])
def test_per_input_size_caps_reject_oversize_before_publication(
    tmp_path: Path, target: str
) -> None:
    native = build_synthetic_native_fixture(tmp_path / "native")
    private = build_private_fixture(tmp_path / "private")
    if target == "native-receipt":
        path = native / "build-receipt.json"
        maximum = payload.NATIVE_SIZE_LIMITS["build-receipt.json"]
        path.write_bytes(b"{" + b" " * maximum)
        path.chmod(0o644)
    else:
        path = private / "trust.d/request-authority-v2.pem"
        maximum = payload.PRIVATE_SIZE_LIMITS[
            "trust.d/request-authority-v2.pem"
        ]
        with path.open("wb") as stream:
            stream.truncate(maximum + 1)
        path.chmod(0o600)

    with pytest.raises(payload.PayloadError, match="input-size-invalid"):
        payload.assemble_payload(
            native_bundle=str(native),
            private_bundle=str(private),
            service_gid=1999,
            output=str(tmp_path / "payload"),
        )
    assert not (tmp_path / "payload").exists()


@pytest.mark.parametrize("shape", ["wide", "deep"])
def test_unexpected_tree_is_rejected_at_root_without_descending(
    tmp_path: Path, shape: str
) -> None:
    native = build_synthetic_native_fixture(tmp_path / "native")
    private = build_private_fixture(tmp_path / "private")
    attacker = private / "attacker-tree"
    attacker.mkdir(mode=0o700)
    if shape == "wide":
        for index in range(256):
            _write(attacker / f"entry-{index:04d}", b"x", 0o600)
    else:
        current = attacker
        for index in range(64):
            current = current / f"depth-{index:02d}"
            current.mkdir(mode=0o700)
        os.mkfifo(current / "must-not-be-visited", 0o600)

    with pytest.raises(payload.PayloadError, match="input-tree-set-invalid"):
        payload.assemble_payload(
            native_bundle=str(native),
            private_bundle=str(private),
            service_gid=1999,
            output=str(tmp_path / "payload"),
        )
    assert not (tmp_path / "payload").exists()


def test_public_canonical_manifest_helper_validates_then_normalizes() -> None:
    roles = []
    for contract in installation.ROLE_CONTRACTS:
        roles.append(
            {
                "gid": 1999
                if contract.role in installation.SERVICE_GROUP_ROLES
                else 0,
                "uid": 0,
                "mode": contract.mode,
                "size": 1,
                "sha256": "sha256:" + "a" * 64,
                "path": contract.path,
                "role": contract.role,
            }
        )
    document = {
        "roles": roles,
        "version": installation.VERSION,
        "schema": installation.SCHEMA,
    }
    canonical = installation.canonical_manifest_bytes(document)
    assert canonical == _canonical(document)
    document["roles"] = list(reversed(roles))
    with pytest.raises(installation.InstallationModelError):
        installation.canonical_manifest_bytes(document)


def test_cli_failure_is_secret_free_and_publishes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "SECRET-NATIVE-PATH-MUST-NOT-APPEAR"
    result = payload.main(
        [
            "--native-bundle",
            str(tmp_path / secret),
            "--private-bundle",
            str(tmp_path / "missing-private"),
            "--service-gid",
            "1999",
            "--output",
            str(tmp_path / "payload"),
        ]
    )
    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err.startswith("error:")
    assert secret not in captured.err
    assert not (tmp_path / "payload").exists()


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="requires procfs")
def test_temp_fchmod_fault_closes_descriptors_and_removes_partial_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = build_synthetic_native_fixture(tmp_path / "native")
    private = build_private_fixture(tmp_path / "private")
    before = len(list(Path("/proc/self/fd").iterdir()))

    def fail_fchmod(_descriptor: int, _mode: int) -> None:
        raise OSError("injected fchmod failure")

    monkeypatch.setattr(os, "fchmod", fail_fchmod)
    with pytest.raises(payload.PayloadError, match="temporary-directory-open-failed"):
        payload.assemble_payload(
            native_bundle=str(native),
            private_bundle=str(private),
            service_gid=1999,
            output=str(tmp_path / "payload"),
        )
    after = len(list(Path("/proc/self/fd").iterdir()))
    assert after == before
    assert not any(".assembling." in path.name for path in tmp_path.iterdir())


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="requires procfs")
def test_enumeration_child_fstat_fault_closes_opened_directory_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = build_private_fixture(tmp_path / "private")
    target = str(private / "trust.d")
    original_fstat = os.fstat
    injected = False
    before = len(list(Path("/proc/self/fd").iterdir()))

    def fstat(descriptor: int):
        nonlocal injected
        try:
            opened_path = os.readlink(f"/proc/self/fd/{descriptor}")
        except OSError:
            opened_path = ""
        if not injected and opened_path == target:
            injected = True
            raise OSError("injected directory fstat failure")
        return original_fstat(descriptor)

    monkeypatch.setattr(os, "fstat", fstat)
    with pytest.raises(payload.PayloadError, match="input-directory-open-failed"):
        payload._snapshot_tree(
            str(private),
            payload.PRIVATE_FILES,
            size_limits=payload.PRIVATE_SIZE_LIMITS,
        )
    after = len(list(Path("/proc/self/fd").iterdir()))
    assert injected is True
    assert after == before


@pytest.mark.parametrize("swap_target", ["ancestor", "file"])
def test_relative_file_open_binds_every_stat_to_opened_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    swap_target: str,
) -> None:
    private = build_private_fixture(tmp_path / "private")
    root_fd = os.open(str(private), payload._open_flags(directory=True))
    original_open = os.open
    target_name = "package-authority-v2.pem"
    target = private / "trust.d" / target_name
    original_raw = target.read_bytes()
    injected = False

    def swap_before_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal injected
        should_swap = (
            not injected
            and (
                (swap_target == "ancestor" and path == "trust.d")
                or (swap_target == "file" and path == target_name)
            )
        )
        if should_swap:
            injected = True
            if swap_target == "ancestor":
                ancestor = private / "trust.d"
                ancestor.rename(private / "trust.detached")
                ancestor.mkdir(mode=0o700)
                _write(ancestor / target_name, original_raw, 0o600)
            else:
                target.rename(target.with_name(target.name + ".detached"))
                _write(target, original_raw, 0o600)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swap_before_open)
    try:
        with pytest.raises(payload.PayloadError, match="input-concurrent-mutation"):
            payload._open_relative_file(
                root_fd,
                f"trust.d/{target_name}",
            )
    finally:
        os.close(root_fd)
    assert injected is True


def test_descriptor_cleanup_erases_pinned_tree_without_deleting_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary = tmp_path / "temporary"
    temporary.mkdir(mode=0o700)
    _write(temporary / "nested" / "owned-data", b"erase me", 0o600)
    parent_fd = os.open(str(tmp_path), payload._open_flags(directory=True))
    pinned_fd = os.open(str(temporary), payload._open_flags(directory=True))
    expected_identity = payload._directory_identity(os.fstat(pinned_fd))
    original_erase = payload._erase_directory_contents_fd
    detached = tmp_path / "detached-owned"
    replacement_marker = temporary / "replacement-marker"

    def replace_name_then_erase_pinned(descriptor: int) -> None:
        temporary.rename(detached)
        temporary.mkdir(mode=0o700)
        replacement_marker.write_text("preserve", encoding="ascii")
        original_erase(descriptor)

    monkeypatch.setattr(
        payload,
        "_erase_directory_contents_fd",
        replace_name_then_erase_pinned,
    )
    try:
        with pytest.raises(
            payload.PayloadError,
            match="temporary-cleanup-target-replaced",
        ):
            payload._remove_temp(parent_fd, temporary.name, expected_identity)
        assert replacement_marker.read_text(encoding="ascii") == "preserve"
        assert list(detached.iterdir()) == []
        assert os.fstat(pinned_fd).st_ino == detached.stat().st_ino
        assert os.fstat(pinned_fd).st_nlink > 0
    finally:
        os.close(pinned_fd)
        os.close(parent_fd)
