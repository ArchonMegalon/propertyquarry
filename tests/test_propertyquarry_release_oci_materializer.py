from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
from types import SimpleNamespace

import pytest
import yaml

from scripts import propertyquarry_release_authenticated_package as authenticated
from scripts import propertyquarry_release_installation_model as installation
from scripts import propertyquarry_release_local_identity as local_identity
from scripts import propertyquarry_release_oci_materializer as oci
from scripts import propertyquarry_release_package_payload as package_payload


ROOT = Path(__file__).resolve().parents[1]
REAL_NATIVE_BUNDLE = (
    ROOT / "build/propertyquarry-release-control-v2/linux-amd64"
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


def _synthetic_static_elf(label: str) -> bytes:
    payload_bytes = b"\xc3" + label.encode("ascii")
    program_offset = 64
    code_offset = program_offset + 2 * 56
    total = code_offset + len(payload_bytes)
    identity = b"\x7fELF\x02\x01\x01" + b"\x00" * 9
    return b"".join(
        (
            struct.pack(
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
                2,
                0,
                0,
                0,
            ),
            struct.pack(
                "<IIQQQQQQ",
                1,
                5,
                0,
                0x400000,
                0x400000,
                total,
                total,
                0x1000,
            ),
            struct.pack("<IIQQQQQQ", 0x6474E551, 6, 0, 0, 0, 0, 0, 8),
            payload_bytes,
        )
    )


def _set_payload_directory_modes(root: Path) -> None:
    for directory in sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        relative = directory.relative_to(root).as_posix()
        directory.chmod(
            0o750
            if relative.startswith("rootfs/etc/propertyquarry-release-control")
            else 0o755
        )
    root.chmod(0o700)


def _authentication_document(
    *,
    manifest: bytes,
    package_receipt: bytes,
    directory_count: int,
    native_receipt_digest: str,
    tree_digest: str = "sha256:" + "b" * 64,
) -> dict[str, object]:
    return {
        "schema": oci.AUTHENTICATION_SCHEMA,
        "version": 2,
        "signature_profile": {
            "algorithm": "ed25519",
            "encoding": "raw-64-byte",
            "key_id": "sha256:" + "a" * 64,
            "signed_message": (
                "domain-separated-uint64be-length-prefixed-canonical-json"
            ),
        },
        "authority_scope": {
            "kind": "local-docker",
            "scope_id": "propertyquarry-local-docker",
            "authoritative_for_package_authentication": True,
            "external_production_authority": False,
            "public_launch_authority": False,
            "performs_release_effects": False,
        },
        "payload": {
            "tree_digest": tree_digest,
            "file_count": 21,
            "directory_count": directory_count,
            "role_count": 19,
            "installation_manifest_sha256": _digest(manifest),
            "package_payload_receipt_sha256": _digest(package_receipt),
            "native_build_receipt_sha256": native_receipt_digest,
        },
    }


def _build_wrapper(tmp_path: Path, *, service_gid: int = 1999) -> Path:
    wrapper = tmp_path / "wrapper"
    payload_root = wrapper / "payload"
    payload_root.mkdir(parents=True, mode=0o700)
    roles: list[dict[str, object]] = []
    for index, contract in enumerate(installation.ROLE_CONTRACTS):
        data = (
            _synthetic_static_elf(contract.role)
            if contract.role
            in {
                "controller-executable",
                "supervisor-executable",
                "watchdog-executable",
            }
            else f"authenticated-role:{index}:{contract.role}\n".encode("ascii")
        )
        path = payload_root / "rootfs" / contract.path[1:]
        _write(path, data, contract.mode)
        roles.append(
            {
                "role": contract.role,
                "path": contract.path,
                "sha256": _digest(data),
                "size": len(data),
                "mode": contract.mode,
                "uid": 0,
                "gid": (
                    service_gid
                    if contract.role in installation.SERVICE_GROUP_ROLES
                    else 0
                ),
            }
        )
    manifest = _canonical(
        {"schema": installation.SCHEMA, "version": 2, "roles": roles}
    )
    native_receipt_digest = "sha256:" + "c" * 64
    package_receipt = _canonical(
        {
            "schema": package_payload.RECEIPT_SCHEMA,
            "role_count": 19,
            "installation_manifest_sha256": _digest(manifest),
            "input_integrity": {
                "native_build_receipt_sha256": native_receipt_digest
            },
        }
    )
    _write(payload_root / "installation-manifest.v2.json", manifest, 0o644)
    _write(
        payload_root / "package-payload-receipt.v2.json", package_receipt, 0o644
    )
    _set_payload_directory_modes(payload_root)
    snapshot = package_payload._snapshot_tree(
        str(payload_root), oci._payload_specification()
    )
    authentication = _canonical(
        _authentication_document(
            manifest=manifest,
            package_receipt=package_receipt,
            directory_count=len(snapshot.directories),
            native_receipt_digest=native_receipt_digest,
            tree_digest=oci._captured_payload_tree_digest(snapshot),
        )
    )
    _write(wrapper / "authentication.v2.json", authentication, 0o644)
    _write(wrapper / "authentication.v2.sig", b"S" * 64, 0o644)
    wrapper.chmod(0o700)
    return wrapper


def _mock_phase_a_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    def verify_wrapper(*, wrapper: str, external_anchor: str) -> dict[str, object]:
        del external_anchor
        root = Path(wrapper).resolve()
        authentication_bytes = (root / "authentication.v2.json").read_bytes()
        signature = (root / "authentication.v2.sig").read_bytes()
        return {
            "wrapper": str(root),
            "authentication": json.loads(authentication_bytes),
            "authentication_sha256": _digest(authentication_bytes),
            "signature_sha256": _digest(signature),
        }

    monkeypatch.setattr(oci, "authenticated", SimpleNamespace(verify_wrapper=verify_wrapper))


def _blob(output: Path, digest: str) -> bytes:
    return (output / "blobs" / "sha256" / digest.removeprefix("sha256:")).read_bytes()


def test_daemonless_materializer_is_deterministic_and_independently_audited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_phase_a_verifier(monkeypatch)
    wrapper = _build_wrapper(tmp_path)
    anchor = tmp_path / "external-anchor.pem"
    _write(anchor, b"PUBLIC-ANCHOR", 0o600)
    parent_a = tmp_path / "out-a"
    parent_b = tmp_path / "out-b"
    parent_a.mkdir(mode=0o700)
    parent_b.mkdir(mode=0o700)

    first = oci.materialize(
        wrapper=str(wrapper),
        external_anchor=str(anchor),
        output=str(parent_a / "image"),
    )
    second = oci.materialize(
        wrapper=str(wrapper),
        external_anchor=str(anchor),
        output=str(parent_b / "image"),
    )

    assert first.image_id == second.image_id
    assert first.image_digest == second.image_digest
    assert first.docker_archive_sha256 == second.docker_archive_sha256
    assert first.receipt_sha256 == second.receipt_sha256
    for relative in (
        "index.json",
        "installation-manifest.v2.json",
        "materialization-receipt.v2.json",
        "oci-layout",
        "docker-image.tar",
    ):
        assert (parent_a / "image" / relative).read_bytes() == (
            parent_b / "image" / relative
        ).read_bytes()

    receipt = oci.verify(
        first.output, wrapper=str(wrapper), external_anchor=str(anchor)
    )
    assert receipt["role_count"] == 19
    assert receipt["independent_active_role_audit"] is True
    assert receipt["projected_numeric_ownership_verified"] is True
    assert receipt["loads_or_runs_docker"] is False
    assert receipt["network_implementation_present"] is False
    assert receipt["runtime_user"] == "65532:1999"
    assert receipt["authority_user"] == "65532:1999"
    assert receipt["caller_user"] == "65534:1999"
    assert first.image_id == receipt["image_id"]

    layer = _blob(parent_a / "image", str(receipt["layer_digest"]))
    records = oci._parse_tar(layer)
    files = {item.path: item for item in records if item.data is not None}
    directories = {item.path: item for item in records if item.data is None}
    assert len([path for path in files if path in {
        contract.path[1:] for contract in installation.ROLE_CONTRACTS
    }]) == 19
    assert (
        f"{oci.LOCAL_AUTHORITY_PREFIX}/authentication.v2.json" in files
    )
    assert f"{oci.LOCAL_AUTHORITY_PREFIX}/authentication.v2.sig" in files
    assert (
        f"{oci.LOCAL_AUTHORITY_PREFIX}/payload/installation-manifest.v2.json"
        in files
    )
    retained_payload = directories[f"{oci.LOCAL_AUTHORITY_PREFIX}/payload"]
    assert (retained_payload.mode, retained_payload.uid, retained_payload.gid) == (
        0o755,
        0,
        0,
    )
    retained_private = directories[
        f"{oci.LOCAL_AUTHORITY_PREFIX}/payload/rootfs/etc/"
        "propertyquarry-release-control/trust.d"
    ]
    assert (retained_private.mode, retained_private.uid, retained_private.gid) == (
        0o750,
        0,
        1999,
    )
    retained_public = directories[
        f"{oci.LOCAL_AUTHORITY_PREFIX}/payload/rootfs/usr/libexec/"
        "propertyquarry-release-control"
    ]
    assert (retained_public.mode, retained_public.uid, retained_public.gid) == (
        0o755,
        0,
        0,
    )
    runtime_socket = directories[oci.RUNTIME_SOCKET_DIRECTORY]
    runtime_state = directories[oci.RUNTIME_STATE_DIRECTORY]
    assert (runtime_socket.mode, runtime_socket.uid, runtime_socket.gid) == (
        0o750,
        65532,
        1999,
    )
    assert (runtime_state.mode, runtime_state.uid, runtime_state.gid) == (
        0o700,
        65532,
        1999,
    )
    assert (directories["run/secrets"].mode, directories["run/secrets"].uid) == (
        0o755,
        0,
    )
    anchor_target = files[oci.EXTERNAL_ANCHOR_MOUNT_TARGET]
    assert (
        anchor_target.mode,
        anchor_target.uid,
        anchor_target.gid,
        anchor_target.data,
    ) == (0o444, 0, 0, b"")
    for contract in installation.ROLE_CONTRACTS:
        active = files[contract.path[1:]]
        assert active.mode == contract.mode
        assert active.uid == 0
        assert active.gid == (
            1999 if contract.role in installation.SERVICE_GROUP_ROLES else 0
        )


def test_image_config_has_closed_public_metadata_and_no_history_or_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_phase_a_verifier(monkeypatch)
    wrapper = _build_wrapper(tmp_path)
    anchor = tmp_path / "anchor.pem"
    _write(anchor, b"PUBLIC", 0o600)
    parent = tmp_path / "out"
    parent.mkdir(mode=0o700)
    result = oci.materialize(
        wrapper=str(wrapper),
        external_anchor=str(anchor),
        output=str(parent / "image"),
    )
    config = json.loads(_blob(Path(result.output), result.image_id))
    assert "history" not in config
    assert "created" not in config
    assert "Entrypoint" not in config["config"]
    assert "Cmd" not in config["config"]
    assert config["config"]["User"] == "65532:1999"
    encoded = _canonical(config)
    assert str(wrapper).encode() not in encoded
    assert str(anchor).encode() not in encoded
    assert b"PRIVATE KEY" not in encoded


def test_private_key_material_is_rejected_before_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_phase_a_verifier(monkeypatch)
    wrapper = _build_wrapper(tmp_path)
    trust = (
        wrapper
        / "payload/rootfs/etc/propertyquarry-release-control/trust.d/"
        "package-authority-v2.pem"
    )
    trust.write_bytes(b"-----BEGIN PRIVATE KEY-----\nforbidden\n")
    trust.chmod(0o640)
    anchor = tmp_path / "anchor.pem"
    _write(anchor, b"PUBLIC", 0o600)
    parent = tmp_path / "out"
    parent.mkdir(mode=0o700)
    with pytest.raises(oci.MaterializationError, match="private-key-material-rejected"):
        oci.materialize(
            wrapper=str(wrapper),
            external_anchor=str(anchor),
            output=str(parent / "image"),
        )
    assert not (parent / "image").exists()


def test_signed_tree_digest_must_match_the_materializer_captured_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_phase_a_verifier(monkeypatch)
    wrapper = _build_wrapper(tmp_path)
    authentication_path = wrapper / "authentication.v2.json"
    authentication = json.loads(authentication_path.read_bytes())
    authentication["payload"]["tree_digest"] = "sha256:" + "0" * 64
    authentication_path.write_bytes(_canonical(authentication))
    authentication_path.chmod(0o644)
    anchor = tmp_path / "anchor.pem"
    _write(anchor, b"PUBLIC", 0o600)
    parent = tmp_path / "out"
    parent.mkdir(mode=0o700)
    with pytest.raises(oci.MaterializationError, match="payload-tree-mismatch"):
        oci.materialize(
            wrapper=str(wrapper),
            external_anchor=str(anchor),
            output=str(parent / "image"),
        )
    assert not (parent / "image").exists()


def test_captured_authenticated_payload_directory_modes_are_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_phase_a_verifier(monkeypatch)
    wrapper = _build_wrapper(tmp_path)
    changed = (
        wrapper
        / "payload/rootfs/etc/propertyquarry-release-control/trust.d"
    )
    changed.chmod(0o755)
    anchor = tmp_path / "anchor.pem"
    _write(anchor, b"PUBLIC", 0o600)
    parent = tmp_path / "out"
    parent.mkdir(mode=0o700)
    with pytest.raises(oci.MaterializationError, match="payload-tree-invalid"):
        oci.materialize(
            wrapper=str(wrapper),
            external_anchor=str(anchor),
            output=str(parent / "image"),
        )
    assert not (parent / "image").exists()


def test_source_mutation_after_output_audit_fails_closed_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_phase_a_verifier(monkeypatch)
    wrapper = _build_wrapper(tmp_path)
    anchor = tmp_path / "anchor.pem"
    _write(anchor, b"PUBLIC", 0o600)
    parent = tmp_path / "out"
    parent.mkdir(mode=0o700)

    def mutate(phase: str) -> None:
        if phase == "output-independently-audited":
            signature = wrapper / "authentication.v2.sig"
            signature.write_bytes(b"T" * 64)
            signature.chmod(0o644)

    with pytest.raises(oci.MaterializationError, match="concurrent-mutation"):
        oci.materialize(
            wrapper=str(wrapper),
            external_anchor=str(anchor),
            output=str(parent / "image"),
            phase_hook=mutate,
        )
    assert not (parent / "image").exists()
    assert not any(".assembling." in path.name for path in parent.iterdir())


def test_verify_rejects_layer_tampering_even_when_manifest_path_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_phase_a_verifier(monkeypatch)
    wrapper = _build_wrapper(tmp_path)
    anchor = tmp_path / "anchor.pem"
    _write(anchor, b"PUBLIC", 0o600)
    parent = tmp_path / "out"
    parent.mkdir(mode=0o700)
    result = oci.materialize(
        wrapper=str(wrapper),
        external_anchor=str(anchor),
        output=str(parent / "image"),
    )
    receipt = json.loads(
        (Path(result.output) / "materialization-receipt.v2.json").read_bytes()
    )
    layer_path = (
        Path(result.output)
        / "blobs/sha256"
        / str(receipt["layer_digest"]).removeprefix("sha256:")
    )
    changed = bytearray(layer_path.read_bytes())
    changed[512] ^= 1
    layer_path.write_bytes(changed)
    layer_path.chmod(0o644)
    with pytest.raises(oci.MaterializationError, match="digest-mismatch"):
        oci.verify(
            result.output, wrapper=str(wrapper), external_anchor=str(anchor)
        )


def test_verify_rechecks_identity_after_wrapper_and_layer_recomputation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_phase_a_verifier(monkeypatch)
    wrapper = _build_wrapper(tmp_path)
    anchor = tmp_path / "anchor.pem"
    _write(anchor, b"PUBLIC", 0o600)
    parent = tmp_path / "out"
    parent.mkdir(mode=0o700)
    result = oci.materialize(
        wrapper=str(wrapper),
        external_anchor=str(anchor),
        output=str(parent / "image"),
    )
    output = Path(result.output)
    real_audit = oci._audit_materialized_tree
    audit_calls = 0

    def replace_after_initial_audit(*args, **kwargs) -> oci.AuditedOciTree:
        nonlocal audit_calls
        audit_calls += 1
        audited = real_audit(*args, **kwargs)
        if audit_calls == 1:
            selected = output / "index.json"
            replacement = output / "index.json.replacement"
            _write(replacement, selected.read_bytes(), 0o644)
            os.replace(replacement, selected)
        return audited

    monkeypatch.setattr(oci, "_audit_materialized_tree", replace_after_initial_audit)
    with pytest.raises(oci.MaterializationError, match="object-identity-changed"):
        oci.verify(
            result.output,
            wrapper=str(wrapper),
            external_anchor=str(anchor),
        )
    assert audit_calls == 2


@pytest.mark.parametrize("directory", ["blobs", "blobs/sha256"])
def test_materialize_rejects_intermediate_directory_symlink_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    directory: str,
) -> None:
    _mock_phase_a_verifier(monkeypatch)
    wrapper = _build_wrapper(tmp_path)
    anchor = tmp_path / "anchor.pem"
    _write(anchor, b"PUBLIC", 0o600)
    parent = tmp_path / "out"
    parent.mkdir(mode=0o700)
    output = parent / "image"
    detached = tmp_path / ("escaped-" + directory.replace("/", "-"))

    def substitute(phase: str) -> None:
        if phase == "output-written":
            temporary = next(path for path in parent.iterdir() if path.is_dir())
            target = temporary / directory
            os.rename(target, detached)
            os.symlink(detached, target, target_is_directory=True)

    with pytest.raises(oci.MaterializationError, match="directory"):
        oci.materialize(
            wrapper=str(wrapper),
            external_anchor=str(anchor),
            output=str(output),
            phase_hook=substitute,
        )
    assert not output.exists()
    assert detached.is_dir()
    assert not any("oci-assembling" in path.name for path in parent.iterdir())


@pytest.mark.parametrize("directory", ["blobs", "blobs/sha256"])
def test_verify_rejects_intermediate_directory_symlink_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    directory: str,
) -> None:
    _mock_phase_a_verifier(monkeypatch)
    wrapper = _build_wrapper(tmp_path)
    anchor = tmp_path / "anchor.pem"
    _write(anchor, b"PUBLIC", 0o600)
    parent = tmp_path / "out"
    parent.mkdir(mode=0o700)
    result = oci.materialize(
        wrapper=str(wrapper),
        external_anchor=str(anchor),
        output=str(parent / "image"),
    )
    target = Path(result.output) / directory
    detached = tmp_path / ("detached-" + directory.replace("/", "-"))
    os.rename(target, detached)
    os.symlink(detached, target, target_is_directory=True)

    with pytest.raises(oci.MaterializationError, match="directory"):
        oci.verify(
            result.output, wrapper=str(wrapper), external_anchor=str(anchor)
        )


def test_late_post_rename_mutation_is_identity_safely_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_phase_a_verifier(monkeypatch)
    wrapper = _build_wrapper(tmp_path)
    anchor = tmp_path / "anchor.pem"
    _write(anchor, b"PUBLIC", 0o600)
    parent = tmp_path / "out"
    parent.mkdir(mode=0o700)
    output = parent / "image"

    def mutate(phase: str) -> None:
        if phase == "after-rename-before-audit":
            changed = output / "index.json"
            changed.write_bytes(b"{}")
            changed.chmod(0o644)

    with pytest.raises(oci.MaterializationError):
        oci.materialize(
            wrapper=str(wrapper),
            external_anchor=str(anchor),
            output=str(output),
            phase_hook=mutate,
        )
    assert not output.exists()
    assert not any("oci-assembling" in path.name for path in parent.iterdir())


@pytest.mark.parametrize("target", ["index.json", "blob", "blobs/sha256"])
def test_same_byte_object_replacement_after_rename_never_returns_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    _mock_phase_a_verifier(monkeypatch)
    wrapper = _build_wrapper(tmp_path)
    anchor = tmp_path / "anchor.pem"
    _write(anchor, b"PUBLIC", 0o600)
    parent = tmp_path / "out"
    parent.mkdir(mode=0o700)
    output = parent / "image"

    def replace(phase: str) -> None:
        if phase != "after-rename-before-audit":
            return
        if target == "blob":
            receipt = json.loads(
                (output / "materialization-receipt.v2.json").read_bytes()
            )
            selected = (
                output
                / "blobs/sha256"
                / receipt["image_digest"].removeprefix("sha256:")
            )
        else:
            selected = output / target
        if selected.is_dir():
            replacement = output / "blobs/replacement-sha256"
            detached = parent / "detached-staged-sha256"
            shutil.copytree(selected, replacement, copy_function=shutil.copy2)
            os.rename(selected, detached)
            os.rename(replacement, selected)
        else:
            replacement = selected.with_name(selected.name + ".replacement")
            _write(
                replacement,
                selected.read_bytes(),
                selected.stat().st_mode & 0o777,
            )
            os.replace(replacement, selected)

    with pytest.raises(oci.MaterializationError, match="object-identity-changed"):
        oci.materialize(
            wrapper=str(wrapper),
            external_anchor=str(anchor),
            output=str(output),
            phase_hook=replace,
        )
    assert not output.exists()


def test_same_byte_replacement_after_parent_fsync_is_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_phase_a_verifier(monkeypatch)
    wrapper = _build_wrapper(tmp_path)
    anchor = tmp_path / "anchor.pem"
    _write(anchor, b"PUBLIC", 0o600)
    parent = tmp_path / "out"
    parent.mkdir(mode=0o700)
    output = parent / "image"
    after_rename = False
    replaced = False
    real_fsync = oci.os.fsync

    def phase_hook(phase: str) -> None:
        nonlocal after_rename
        if phase == "after-rename-before-audit":
            after_rename = True

    def replace_after_fsync(descriptor: int) -> None:
        nonlocal replaced
        real_fsync(descriptor)
        if after_rename and not replaced:
            selected = output / "index.json"
            replacement = output / "index.json.replacement"
            _write(replacement, selected.read_bytes(), 0o644)
            os.replace(replacement, selected)
            replaced = True

    monkeypatch.setattr(oci.os, "fsync", replace_after_fsync)
    with pytest.raises(oci.MaterializationError, match="object-identity-changed"):
        oci.materialize(
            wrapper=str(wrapper),
            external_anchor=str(anchor),
            output=str(output),
            phase_hook=phase_hook,
        )
    assert replaced is True
    assert not output.exists()


def test_output_name_swap_after_parent_fsync_preserves_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_phase_a_verifier(monkeypatch)
    wrapper = _build_wrapper(tmp_path)
    anchor = tmp_path / "anchor.pem"
    _write(anchor, b"PUBLIC", 0o600)
    parent = tmp_path / "out"
    parent.mkdir(mode=0o700)
    output = parent / "image"
    detached = parent / "detached-owned-image-after-fsync"
    after_rename = False
    replaced = False
    real_fsync = oci.os.fsync

    def phase_hook(phase: str) -> None:
        nonlocal after_rename
        if phase == "after-rename-before-audit":
            after_rename = True

    def replace_after_fsync(descriptor: int) -> None:
        nonlocal replaced
        real_fsync(descriptor)
        if after_rename and not replaced:
            os.rename(output, detached)
            output.mkdir(mode=0o700)
            (output / "attacker-marker").write_text("preserve", encoding="utf-8")
            replaced = True

    monkeypatch.setattr(oci.os, "fsync", replace_after_fsync)
    with pytest.raises(oci.MaterializationError, match="cleanup-target-replaced"):
        oci.materialize(
            wrapper=str(wrapper),
            external_anchor=str(anchor),
            output=str(output),
            phase_hook=phase_hook,
        )
    assert replaced is True
    assert (output / "attacker-marker").read_text(encoding="utf-8") == "preserve"
    assert detached.is_dir()


def test_post_rename_name_swap_never_deletes_unowned_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_phase_a_verifier(monkeypatch)
    wrapper = _build_wrapper(tmp_path)
    anchor = tmp_path / "anchor.pem"
    _write(anchor, b"PUBLIC", 0o600)
    parent = tmp_path / "out"
    parent.mkdir(mode=0o700)
    output = parent / "image"
    detached = parent / "detached-owned-image"

    def replace(phase: str) -> None:
        if phase == "after-rename-before-audit":
            os.rename(output, detached)
            output.mkdir(mode=0o700)
            (output / "attacker-marker").write_text("preserve", encoding="utf-8")

    with pytest.raises(oci.MaterializationError, match="cleanup-target-replaced"):
        oci.materialize(
            wrapper=str(wrapper),
            external_anchor=str(anchor),
            output=str(output),
            phase_hook=replace,
        )
    assert (output / "attacker-marker").read_text(encoding="utf-8") == "preserve"
    assert detached.is_dir()


def test_authentication_scope_must_be_exactly_local_docker_authority() -> None:
    document = _authentication_document(
        manifest=b"{}",
        package_receipt=b"{}",
        directory_count=1,
        native_receipt_digest="sha256:" + "c" * 64,
    )
    document["authority_scope"][  # type: ignore[index]
        "authoritative_for_package_authentication"
    ] = False
    encoded = _canonical(document)
    with pytest.raises(oci.MaterializationError, match="authority-scope-invalid"):
        oci._validate_authentication_document(
            document, auth_bytes=encoded, signature_bytes=b"S" * 64
        )


def test_separate_compose_defines_only_hardened_one_shot_services() -> None:
    compose_path = ROOT / "compose.propertyquarry-release-control-v2.yml"
    document = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    assert document["name"] == "propertyquarry-release-control-v2"
    assert set(document["services"]) == {
        "controller-self-test",
        "supervisor-self-test",
        "watchdog-self-test",
        "controller-refusal-test",
        "supervisor-refusal-test",
        "watchdog-refusal-test",
    }
    for name, service in document["services"].items():
        assert service["network_mode"] == "none"
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["pids_limit"] == 32
        assert service["mem_limit"] == "64m"
        assert service["memswap_limit"] == "64m"
        assert service["pull_policy"] == "never"
        assert service["restart"] == "no"
        assert service["environment"] == []
        for forbidden in (
            "build",
            "ports",
            "volumes",
            "devices",
            "env_file",
            "extra_hosts",
            "privileged",
        ):
            assert forbidden not in service, (name, forbidden)
    for component in ("controller", "supervisor", "watchdog"):
        assert document["services"][f"{component}-self-test"]["command"] == [
            "--self-test"
        ]


def test_materializer_source_has_no_network_or_docker_execution_primitive() -> None:
    source = (ROOT / "scripts/propertyquarry_release_oci_materializer.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    imported = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert imported.isdisjoint({"subprocess", "socket", "urllib", "requests", "docker"})
    for forbidden in ("docker.sock", "http://", "https://"):
        assert forbidden not in source


def test_real_local_authority_to_exact_payload_to_oci_handoff(
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
    payload_path = package_payload.assemble_payload(
        native_bundle=str(REAL_NATIVE_BUNDLE),
        private_bundle=state.package_input,
        service_gid=1999,
        output=str(tmp_path / "payload"),
    )
    wrapper = authenticated.create_authenticated_wrapper(
        payload=payload_path,
        private_key=state.package_private_key,
        external_anchor=state.package_external_anchor,
        output=str(tmp_path / "authenticated-wrapper"),
    )
    result = oci.materialize(
        wrapper=wrapper,
        external_anchor=state.package_external_anchor,
        output=str(tmp_path / "oci-image"),
    )
    receipt = oci.verify(
        result.output,
        wrapper=wrapper,
        external_anchor=state.package_external_anchor,
    )
    assert receipt["image_id"] == result.image_id
    assert receipt["image_digest"] == result.image_digest
    assert receipt["docker_archive_sha256"] == result.docker_archive_sha256
    assert receipt["role_count"] == 19
    assert receipt["independent_active_role_audit"] is True
