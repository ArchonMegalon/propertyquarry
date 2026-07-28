from __future__ import annotations

import copy
import dataclasses
import hashlib
import os
import socket
import tempfile
from pathlib import Path

import pytest

from scripts import propertyquarry_release_installation_model as model


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _content(role: str) -> bytes:
    return f"propertyquarry installation fixture: {role}\n".encode("ascii")


def _manifest(*, uid: int | None = None, gid: int | None = None) -> dict[str, object]:
    uid = os.getuid() if uid is None else uid
    gid = os.getgid() if gid is None else gid
    return {
        "schema": model.SCHEMA,
        "version": model.VERSION,
        "roles": [
            {
                "role": contract.role,
                "path": contract.path,
                "sha256": _digest(_content(contract.role)),
                "size": len(_content(contract.role)),
                "mode": contract.mode,
                "uid": uid,
                "gid": gid,
            }
            for contract in model.ROLE_CONTRACTS
        ],
    }


def _production_manifest(*, service_gid: int = 1999) -> dict[str, object]:
    manifest = _manifest(uid=0, gid=0)
    for role in model.SERVICE_GROUP_ROLES:
        _entry(manifest, role)["gid"] = service_gid
    return manifest


def _path(root: Path, contract: model.RoleContract) -> Path:
    return root.joinpath(*contract.path[1:].split("/"))


def _materialize(root: Path) -> dict[str, object]:
    manifest = _manifest()
    for contract in model.ROLE_CONTRACTS:
        target = _path(root, contract)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_content(contract.role))
        target.chmod(contract.mode)
    return manifest


@pytest.fixture
def installation(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    root = tmp_path / "isolated-rootfs"
    root.mkdir()
    return root, _materialize(root)


def _entry(manifest: dict[str, object], role: str) -> dict[str, object]:
    return next(
        item
        for item in manifest["roles"]  # type: ignore[index,union-attr]
        if item["role"] == role  # type: ignore[index]
    )


def _codes(result: model.InstallationAuditResult) -> set[model.BlockerCode]:
    return {blocker.code for blocker in result.blockers}


def test_contract_is_closed_fixed_and_explicitly_non_authoritative() -> None:
    contract = model.describe_contract()

    assert contract["schema"] == model.SCHEMA
    assert contract["version"] == 2
    assert contract["authoritative"] is False
    assert contract["performs_writes"] is False
    assert contract["installs_or_repairs"] is False
    assert contract["verifies_signatures"] is False
    assert contract["readiness_authority"] is False
    assert contract["traversal"] == "descriptor-relative-o-nofollow"
    assert contract["accepted_final_type"] == "single-link-regular-file"
    assert contract["production_private_group"] == "one-consistent-nonroot-gid"
    assert contract["private_service_group_roles"] == sorted(
        model.SERVICE_GROUP_ROLES
    )
    assert len(model.ROLE_CONTRACTS) == 19
    assert len(set(model.ROLE_NAMES)) == 19
    paths = [item.path for item in model.ROLE_CONTRACTS]
    assert len(set(paths)) == 19
    assert all(path.startswith("/") and ".." not in path.split("/") for path in paths)
    assert model.ROLE_BY_NAME["supervisor-executable"].path == (
        "/usr/libexec/propertyquarry-release-control/"
        "propertyquarry-release-supervisor-v2"
    )
    assert model.ROLE_BY_NAME["controller-executable"].path.endswith(
        "/propertyquarry-release-controller-v2"
    )
    assert model.ROLE_BY_NAME["watchdog-executable"].path.endswith(
        "/propertyquarry-release-watchdog-v2"
    )
    trust_paths = {
        model.ROLE_BY_NAME[name].path
        for name in (
            "request-trust-root",
            "response-trust-root",
            "package-trust-root",
        )
    }
    assert len(trust_paths) == 3
    with pytest.raises(TypeError):
        model.ROLE_BY_NAME["attacker"] = model.ROLE_CONTRACTS[0]  # type: ignore[index]
    assert model.MAX_FILE_BYTES == 128 * 1024 * 1024


def test_matching_simulation_is_immutable_and_never_authoritative(
    installation: tuple[Path, dict[str, object]],
) -> None:
    root, manifest = installation

    result = model.audit_installation(manifest, mode="simulation", rootfs=str(root))

    assert result.disposition == "matches-expectations-non-authoritative"
    assert result.all_files_match_expectations is True
    assert result.mode == "simulation"
    assert result.authoritative is False
    assert result.performs_writes is False
    assert result.verifies_signatures is False
    assert result.readiness_authority is False
    assert len(result.observed_files) == 19
    assert result.blockers == ()
    assert all(item.link_count == 1 for item in result.observed_files)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.rootfs = "/attacker"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.observed_files[0].size = 0  # type: ignore[misc]


def test_manifest_bytes_are_strict_and_duplicate_keys_fail_as_typed_blocker(
    installation: tuple[Path, dict[str, object]],
) -> None:
    root, manifest = installation
    raw = model._canonical_bytes(manifest)
    assert model.audit_installation(
        raw, mode="simulation", rootfs=str(root)
    ).all_files_match_expectations

    duplicate = raw.replace(b'"version":2', b'"version":2,"version":2', 1)
    result = model.audit_installation(
        duplicate, mode="simulation", rootfs=str(root)
    )
    assert result.disposition == "blocked-non-authoritative"
    assert result.blockers[0].code is model.BlockerCode.MANIFEST_INVALID


def test_surrogate_json_string_is_a_typed_blocker_not_an_encoding_crash(
    installation: tuple[Path, dict[str, object]],
) -> None:
    root, manifest = installation
    manifest["schema"] = "\ud800"

    result = model.audit_installation(manifest, mode="simulation", rootfs=str(root))

    assert result.blockers[0].code is model.BlockerCode.MANIFEST_INVALID
    assert result.blockers[0].detail == "json-surrogate-rejected"


@pytest.mark.parametrize("field", ["size", "mode", "uid", "gid"])
def test_bool_integer_aliases_are_rejected(
    installation: tuple[Path, dict[str, object]], field: str
) -> None:
    root, manifest = installation
    _entry(manifest, "supervisor-executable")[field] = True

    result = model.audit_installation(manifest, mode="simulation", rootfs=str(root))

    assert result.blockers[0].code is model.BlockerCode.MANIFEST_INVALID
    assert result.observed_files == ()


@pytest.mark.parametrize(
    ("mutation", "detail"),
    [
        (lambda value: value.__setitem__("version", True), "version-invalid"),
        (
            lambda value: _entry(value, "supervisor-executable").__setitem__(
                "path", "/usr/libexec/../escape"
            ),
            "role-path-invalid",
        ),
        (
            lambda value: _entry(value, "supervisor-executable").__setitem__(
                "unexpected", "field"
            ),
            "closed-schema-mismatch",
        ),
    ],
)
def test_malformed_manifest_never_reaches_filesystem(
    installation: tuple[Path, dict[str, object]], mutation, detail: str
) -> None:
    root, manifest = installation
    mutation(manifest)

    result = model.audit_installation(manifest, mode="simulation", rootfs=str(root))

    assert result.blockers[0].code is model.BlockerCode.MANIFEST_INVALID
    assert result.blockers[0].detail == detail
    assert result.observed_files == ()


def test_missing_and_extra_roles_fail_closed(
    installation: tuple[Path, dict[str, object]],
) -> None:
    root, manifest = installation
    missing = copy.deepcopy(manifest)
    missing["roles"].pop()  # type: ignore[union-attr]
    extra = copy.deepcopy(manifest)
    extra["roles"].append(copy.deepcopy(extra["roles"][0]))  # type: ignore[union-attr,index]

    for candidate in (missing, extra):
        result = model.audit_installation(
            candidate, mode="simulation", rootfs=str(root)
        )
        assert result.blockers[0].code is model.BlockerCode.MANIFEST_INVALID
        assert result.blockers[0].detail == "role-set-invalid"


def test_empty_installation_artifact_is_never_a_valid_expectation(
    installation: tuple[Path, dict[str, object]],
) -> None:
    root, manifest = installation
    _entry(manifest, "supervisor-executable")["size"] = 0

    result = model.audit_installation(manifest, mode="simulation", rootfs=str(root))

    assert result.blockers[0].code is model.BlockerCode.MANIFEST_INVALID
    assert result.blockers[0].detail == "size-invalid"


def test_missing_file_returns_typed_blocker_not_exception(
    installation: tuple[Path, dict[str, object]],
) -> None:
    root, manifest = installation
    contract = model.ROLE_BY_NAME["controller-schema"]
    _path(root, contract).unlink()

    result = model.audit_installation(manifest, mode="simulation", rootfs=str(root))

    blocker = next(item for item in result.blockers if item.role == contract.role)
    assert blocker.code is model.BlockerCode.PATH_MISSING
    assert result.authoritative is False


@pytest.mark.parametrize(
    ("field", "replacement", "expected_code"),
    [
        ("mode", 0o600, model.BlockerCode.MODE_MISMATCH),
        ("uid", 123456, model.BlockerCode.UID_MISMATCH),
        ("gid", 123456, model.BlockerCode.GID_MISMATCH),
        ("size", 1, model.BlockerCode.SIZE_MISMATCH),
        ("sha256", "sha256:" + "0" * 64, model.BlockerCode.DIGEST_MISMATCH),
    ],
)
def test_metadata_size_and_hash_mismatches_are_typed(
    installation: tuple[Path, dict[str, object]],
    field: str,
    replacement: object,
    expected_code: model.BlockerCode,
) -> None:
    root, manifest = installation
    role = "supervisor-executable"
    contract = model.ROLE_BY_NAME[role]
    if field == "mode":
        _path(root, contract).chmod(int(replacement))
    else:
        _entry(manifest, role)[field] = replacement

    result = model.audit_installation(manifest, mode="simulation", rootfs=str(root))

    blocker = next(item for item in result.blockers if item.role == role)
    assert blocker.code is expected_code


def test_same_size_byte_change_is_detected_by_exact_hash(
    installation: tuple[Path, dict[str, object]],
) -> None:
    root, manifest = installation
    contract = model.ROLE_BY_NAME["watchdog-schema"]
    path = _path(root, contract)
    content = bytearray(path.read_bytes())
    content[-2] ^= 1
    path.write_bytes(content)
    path.chmod(contract.mode)

    result = model.audit_installation(manifest, mode="simulation", rootfs=str(root))

    blocker = next(item for item in result.blockers if item.role == contract.role)
    assert blocker.code is model.BlockerCode.DIGEST_MISMATCH


def test_final_symlink_is_rejected_even_when_target_bytes_match(
    installation: tuple[Path, dict[str, object]], tmp_path: Path
) -> None:
    root, manifest = installation
    contract = model.ROLE_BY_NAME["package-trust-root"]
    target = _path(root, contract)
    outside = tmp_path / "outside.pem"
    outside.write_bytes(target.read_bytes())
    outside.chmod(contract.mode)
    target.unlink()
    target.symlink_to(outside)

    result = model.audit_installation(manifest, mode="simulation", rootfs=str(root))

    blocker = next(item for item in result.blockers if item.role == contract.role)
    assert blocker.code is model.BlockerCode.PATH_SYMLINK_REJECTED


def test_ancestor_symlink_is_rejected(
    installation: tuple[Path, dict[str, object]],
) -> None:
    root, manifest = installation
    trust_dir = root / "etc/propertyquarry-release-control/trust.d"
    real = trust_dir.with_name("trust.real")
    trust_dir.rename(real)
    trust_dir.symlink_to(real, target_is_directory=True)

    result = model.audit_installation(manifest, mode="simulation", rootfs=str(root))

    blocker = next(item for item in result.blockers if item.role == "request-trust-root")
    assert blocker.code is model.BlockerCode.PATH_SYMLINK_REJECTED
    assert blocker.detail == "ancestor"


def test_rootfs_symlink_ancestor_is_rejected(
    installation: tuple[Path, dict[str, object]], tmp_path: Path
) -> None:
    root, manifest = installation
    alias = tmp_path / "rootfs-alias"
    alias.symlink_to(root, target_is_directory=True)

    result = model.audit_installation(manifest, mode="simulation", rootfs=str(alias))

    assert result.blockers[0].code is model.BlockerCode.ROOTFS_SYMLINK_REJECTED


def test_hardlink_is_rejected_before_hash_can_legitimize_it(
    installation: tuple[Path, dict[str, object]], tmp_path: Path
) -> None:
    root, manifest = installation
    contract = model.ROLE_BY_NAME["tmpfiles-config"]
    target = _path(root, contract)
    peer = tmp_path / "hardlink-peer"
    os.link(target, peer)

    result = model.audit_installation(manifest, mode="simulation", rootfs=str(root))

    blocker = next(item for item in result.blockers if item.role == contract.role)
    assert blocker.code is model.BlockerCode.HARDLINK_REJECTED


def test_fifo_is_never_read_or_accepted(
    installation: tuple[Path, dict[str, object]],
) -> None:
    root, manifest = installation
    contract = model.ROLE_BY_NAME["sysusers-config"]
    target = _path(root, contract)
    target.unlink()
    os.mkfifo(target, contract.mode)

    result = model.audit_installation(manifest, mode="simulation", rootfs=str(root))

    blocker = next(item for item in result.blockers if item.role == contract.role)
    assert blocker.code is model.BlockerCode.NOT_REGULAR_FILE


def test_unix_socket_is_rejected_before_open() -> None:
    with tempfile.TemporaryDirectory(prefix="pqi-", dir="/tmp") as temporary:
        root = Path(temporary) / "r"
        root.mkdir()
        manifest = _materialize(root)
        contract = model.ROLE_BY_NAME["sysusers-config"]
        target = _path(root, contract)
        target.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(target))
            result = model.audit_installation(
                manifest, mode="simulation", rootfs=str(root)
            )
        finally:
            server.close()

    blocker = next(item for item in result.blockers if item.role == contract.role)
    assert blocker.code is model.BlockerCode.NOT_REGULAR_FILE


def test_manifest_is_snapshotted_before_filesystem_callbacks(
    installation: tuple[Path, dict[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, manifest = installation
    original = model._open_isolated_root

    def mutate_after_snapshot(rootfs: str) -> int:
        manifest["roles"].clear()  # type: ignore[union-attr]
        manifest["schema"] = "attacker"
        return original(rootfs)

    monkeypatch.setattr(model, "_open_isolated_root", mutate_after_snapshot)

    result = model.audit_installation(manifest, mode="simulation", rootfs=str(root))

    assert result.all_files_match_expectations is True
    assert len(result.observed_files) == 19


def test_ancestor_replacement_during_read_cannot_validate_detached_tree(
    installation: tuple[Path, dict[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, manifest = installation
    parent = root / "usr/libexec/propertyquarry-release-control"
    detached = parent.with_name("propertyquarry-release-control.detached")
    original_read = model.os.read
    swapped = False

    def replace_ancestor(fd: int, size: int) -> bytes:
        nonlocal swapped
        chunk = original_read(fd, size)
        if chunk and not swapped:
            swapped = True
            parent.rename(detached)
            parent.mkdir()
            for role in (
                "supervisor-executable",
                "controller-executable",
                "watchdog-executable",
            ):
                contract = model.ROLE_BY_NAME[role]
                target = _path(root, contract)
                target.write_bytes(_content(role))
                target.chmod(contract.mode)
        return chunk

    monkeypatch.setattr(model.os, "read", replace_ancestor)

    result = model.audit_installation(manifest, mode="simulation", rootfs=str(root))

    blocker = next(
        item for item in result.blockers if item.role == "supervisor-executable"
    )
    assert blocker.code is model.BlockerCode.CONCURRENT_MUTATION
    assert blocker.detail == "path-changed"
    assert result.all_files_match_expectations is False


def test_rootfs_replacement_during_read_is_detected_before_result(
    installation: tuple[Path, dict[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, manifest = installation
    detached = root.with_name("isolated-rootfs.detached")
    original_read = model.os.read
    swapped = False

    def replace_rootfs(fd: int, size: int) -> bytes:
        nonlocal swapped
        chunk = original_read(fd, size)
        if chunk and not swapped:
            swapped = True
            root.rename(detached)
            root.mkdir()
        return chunk

    monkeypatch.setattr(model.os, "read", replace_rootfs)

    result = model.audit_installation(manifest, mode="simulation", rootfs=str(root))

    blocker = next(item for item in result.blockers if item.role is None)
    assert blocker.code is model.BlockerCode.CONCURRENT_MUTATION
    assert blocker.detail == "rootfs-path-changed"
    assert result.all_files_match_expectations is False


@pytest.mark.parametrize(
    ("mode", "rootfs"),
    [("production", "/tmp/not-root"), ("simulation", "/"), (True, "/")],
)
def test_mode_and_rootfs_aliases_fail_closed(mode: object, rootfs: object) -> None:
    result = model.audit_installation(_manifest(uid=0, gid=0), mode=mode, rootfs=rootfs)

    assert result.blockers[0].code is model.BlockerCode.AUDIT_TARGET_INVALID
    assert result.authoritative is False


@pytest.mark.parametrize("rootfs", ["/tmp/bad\nroot", "/tmp/bad\x7froot", "\ud800"])
def test_rootfs_control_characters_and_surrogates_are_typed_blockers(
    rootfs: str,
) -> None:
    result = model.audit_installation(
        _manifest(), mode="simulation", rootfs=rootfs
    )

    assert result.blockers[0].code is model.BlockerCode.AUDIT_TARGET_INVALID
    assert result.authoritative is False
    assert result.rootfs == ""


def test_production_private_roles_require_one_nonroot_service_group() -> None:
    valid = _production_manifest()
    mismatched = copy.deepcopy(valid)
    _entry(mismatched, "package-trust-root")["gid"] = 2000
    root_group = _production_manifest(service_gid=0)

    for manifest in (mismatched, root_group):
        result = model.audit_installation(manifest, mode="production", rootfs="/")
        assert model.BlockerCode.GID_MISMATCH in _codes(result)
        assert all(
            blocker.detail == "production-service-group"
            for blocker in result.blockers
        )


def test_production_manifest_requires_root_owner_where_mandated() -> None:
    manifest = _manifest(uid=1, gid=1)

    result = model.audit_installation(manifest, mode="production", rootfs="/")

    blocker = next(item for item in result.blockers if item.role == "supervisor-executable")
    assert blocker.code is model.BlockerCode.UID_MISMATCH
    assert blocker.detail == "production-expectation"


def test_real_host_absence_or_mismatch_is_reported_truthfully_without_writes() -> None:
    manifest = _production_manifest()

    result = model.audit_installation(manifest, mode="production", rootfs="/")

    assert result.disposition == "blocked-non-authoritative"
    assert result.all_files_match_expectations is False
    assert result.blockers
    assert result.authoritative is False
    assert result.performs_writes is False
