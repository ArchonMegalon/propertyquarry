from __future__ import annotations

import io
import os
from pathlib import Path
import stat

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts import propertyquarry_predeploy_backup_v2 as backup


RUNTIME_SHA = "a" * 64


def test_chunked_encryption_roundtrip_authenticates_footer_and_has_no_plaintext_file(
    tmp_path: Path,
) -> None:
    plaintext = (b"propertyquarry-backup-block\n" * 200_000) + b"tail"
    destination = tmp_path / "artifact.pqenc"
    key = bytes(range(32))

    encrypted = backup.encrypt_stream(
        io.BytesIO(plaintext),
        destination,
        master_key=key,
        runtime_sha=RUNTIME_SHA,
        artifact_name="fixture",
        artifact_kind="test-stream",
    )
    observed = bytearray()
    decrypted = backup.decrypt_stream(
        destination,
        observed.extend,
        master_key=key,
        expected_runtime_sha=RUNTIME_SHA,
        expected_artifact_name="fixture",
        expected_artifact_kind="test-stream",
    )

    assert bytes(observed) == plaintext
    assert encrypted == decrypted
    assert encrypted["chunk_count"] >= 2
    assert encrypted["plaintext_bytes"] == len(plaintext)
    assert destination.read_bytes().find(b"propertyquarry-backup-block") == -1
    assert list(tmp_path.iterdir()) == [destination]


def test_chunked_encryption_rejects_tampering_and_truncation(tmp_path: Path) -> None:
    key = bytes(range(32))
    original = tmp_path / "original.pqenc"
    backup.encrypt_stream(
        io.BytesIO(b"verified-backup" * 10_000),
        original,
        master_key=key,
        runtime_sha=RUNTIME_SHA,
        artifact_name="fixture",
        artifact_kind="test-stream",
    )
    encoded = bytearray(original.read_bytes())

    tampered = tmp_path / "tampered.pqenc"
    encoded[len(encoded) // 2] ^= 0x01
    tampered.write_bytes(encoded)
    with pytest.raises(Exception):
        backup.decrypt_stream(
            tampered,
            lambda _chunk: None,
            master_key=key,
            expected_runtime_sha=RUNTIME_SHA,
            expected_artifact_name="fixture",
            expected_artifact_kind="test-stream",
        )

    truncated = tmp_path / "truncated.pqenc"
    truncated.write_bytes(original.read_bytes()[:-17])
    with pytest.raises(backup.BackupError, match="encrypted_stream_truncated"):
        backup.decrypt_stream(
            truncated,
            lambda _chunk: None,
            master_key=key,
            expected_runtime_sha=RUNTIME_SHA,
            expected_artifact_name="fixture",
            expected_artifact_kind="test-stream",
        )


def test_encryption_key_is_exclusive_private_and_stable(tmp_path: Path) -> None:
    parent = tmp_path / "keys"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    path = parent / "propertyquarry-predeploy-backup-v2.key"
    uid = os.getuid()
    gid = os.getgid()

    first_key, first_id, first_created = backup._load_or_create_encryption_key(  # noqa: SLF001
        path,
        expected_parent_uid=uid,
        expected_parent_gid=gid,
        expected_file_uid=uid,
        expected_file_gid=gid,
    )
    second_key, second_id, second_created = backup._load_or_create_encryption_key(  # noqa: SLF001
        path,
        expected_parent_uid=uid,
        expected_parent_gid=gid,
        expected_file_uid=uid,
        expected_file_gid=gid,
    )

    assert first_created is True
    assert second_created is False
    assert first_key == second_key
    assert first_id == second_id
    assert len(first_key) == 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_bytes().endswith(b"\n")
    assert len(path.read_bytes()) == 65


def test_receipt_signature_uses_exact_three_key_wrapper_and_domain() -> None:
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    public_der = public.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    key_id = "sha256:" + backup._sha256_bytes(public_der)  # noqa: SLF001
    payload = {
        "disposition": "verified-and-published",
        "runtime_sha": RUNTIME_SHA,
        "schema": backup.RECEIPT_SCHEMA,
    }

    wrapper = backup._sign_receipt(payload, private, key_id)  # noqa: SLF001
    verified = backup._verify_receipt_wrapper(wrapper, public, key_id)  # noqa: SLF001

    assert set(wrapper) == {"payload", "signature", "signature_key_id"}
    assert wrapper["signature_key_id"] == key_id
    assert verified == payload

    broken = dict(wrapper)
    broken["signature"] = "A" + str(wrapper["signature"])[1:]
    with pytest.raises(backup.BackupError, match="receipt_signature_invalid"):
        backup._verify_receipt_wrapper(broken, public, key_id)  # noqa: SLF001


def test_tar_artifact_is_encrypted_then_decrypt_list_validated(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "one.txt").write_text("one", encoding="utf-8")
    (source / "two.txt").write_text("two", encoding="utf-8")
    destination = tmp_path / "archive.pqenc"
    spec = backup.ArtifactSpec(
        name="fixture-tar",
        kind="tar-gzip",
        producer=(
            backup.TAR_BIN,
            "--gzip",
            "--create",
            "--file",
            "-",
            "--directory",
            str(source),
            ".",
        ),
        verification="tar_gzip_list",
        coverage=(str(source),),
        required_paths=(source,),
    )
    key = bytes(range(32))

    encrypted = backup._encrypt_command(  # noqa: SLF001
        spec,
        destination,
        master_key=key,
        runtime_sha=RUNTIME_SHA,
    )
    decrypted, verification = backup._verify_artifact(  # noqa: SLF001
        destination,
        spec,
        master_key=key,
        runtime_sha=RUNTIME_SHA,
    )

    assert encrypted == decrypted
    assert verification["method"] == "decrypt-tar-gzip-list"
    assert verification["entries"] >= 3
    assert destination.read_bytes().find(b"one") == -1


def test_production_sources_cover_database_roles_volumes_and_isolated_envs() -> None:
    specs = backup.production_artifact_specs()
    names = {spec.name for spec in specs}
    rendered = "\n".join(" ".join(spec.producer) for spec in specs)

    assert {"database", "roles"}.issubset(names)
    assert {
        "volume-provider-ledger",
        "volume-artifacts",
        "volume-governed-render-consents",
        "volume-public-tours",
    }.issubset(names)
    assert "propertyquarry_google_identity.env" in rendered
    assert "propertyquarry_registration_email.env" in rendered
    by_name = {spec.name: spec for spec in specs}
    assert "/docker/property/.env" in by_name["runtime-identity-config"].coverage
    assert "propertyquarry-db-live" in rendered
    assert "pg_dump --format=custom" in rendered
    assert "pg_dumpall --roles-only" in rendered
    assert "EMAILIT_API_KEY" not in rendered
    assert "PROPERTYQUARRY_IDENTITY_SESSION_SECRET" not in rendered


def test_create_recovers_published_remote_after_receipt_crash_and_detects_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_sha = "b" * 64
    envelope_sha = "c" * 64
    web_image = "ghcr.io/example/propertyquarry@sha256:" + ("d" * 64)
    render_image = "ghcr.io/example/propertyquarry-render@sha256:" + ("e" * 64)
    package_key_id = "sha256:" + ("f" * 64)
    install_root = tmp_path / "install"
    receipt_root = tmp_path / "receipts"
    remote_root = tmp_path / "remote"
    install_root.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    (source / "evidence.txt").write_text(
        "encrypted backup evidence",
        encoding="utf-8",
    )
    machine_id = tmp_path / "machine-id"
    machine_id.write_text("machine-fixture\n", encoding="utf-8")

    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    private_path = install_root / "receipt-authority-v2.key"
    public_path = install_root / "receipt-authority-v2.pem"
    private_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        public.public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    private_path.chmod(0o400)
    public_path.chmod(0o444)
    public_der = public.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    receipt_key_id = "sha256:" + backup._sha256_bytes(public_der)  # noqa: SLF001

    plan = {
        "schema": "propertyquarry.release-control.single-host-transaction-plan.v2",
        "runtime_sha": runtime_sha,
        "envelope_sha": envelope_sha,
        "web_image": web_image,
        "render_image": render_image,
    }
    plan_raw = backup._canonical_json_bytes(plan)  # noqa: SLF001
    (install_root / "transaction-plan.v2.json").write_bytes(plan_raw)
    plan_digest = "sha256:" + backup._sha256_bytes(plan_raw)  # noqa: SLF001
    authority = {
        "schema": "propertyquarry.release-control.single-host-profile.v2",
        "runtime_sha": runtime_sha,
        "envelope_sha": envelope_sha,
        "web_image": web_image,
        "render_image": render_image,
        "package_authority_key_id": package_key_id,
        "receipt_authority_key_id": receipt_key_id,
        "plan_digest": plan_digest,
    }
    authority_raw = backup._canonical_json_bytes(authority)  # noqa: SLF001
    (install_root / "authority.v2.json").write_bytes(authority_raw)
    authority_digest = "sha256:" + backup._sha256_bytes(authority_raw)  # noqa: SLF001
    manifest = {
        "schema": "propertyquarry.release-control.single-host-package.v2",
        "runtime_sha": runtime_sha,
        "envelope_sha": envelope_sha,
        "package_authority_key_id": package_key_id,
        "config_digest": authority_digest,
        "plan_digest": plan_digest,
    }
    (install_root / "package-manifest.v2.json").write_bytes(
        backup._canonical_json_bytes(manifest)  # noqa: SLF001
    )
    (install_root / "authority.v2.sig").write_bytes(b"authority-signature")
    (install_root / "package-manifest.v2.sig").write_bytes(
        b"manifest-signature"
    )

    key_parent = tmp_path / "keys"
    key_parent.mkdir(mode=0o700)
    key_parent.chmod(0o700)
    encryption_key = key_parent / "propertyquarry-predeploy-backup-v2.key"
    monkeypatch.setattr(backup, "EXPECTED_ENCRYPTION_KEY_PATH", encryption_key)
    monkeypatch.setattr(backup, "REMOTE_DIRECTORY_MODE", 0o700)
    monkeypatch.setattr(backup, "REMOTE_DIRECTORY_NLINK", 2)
    monkeypatch.setattr(backup, "REMOTE_FILE_MODE", 0o600)
    monkeypatch.setattr(backup, "REMOTE_UID", os.getuid())
    monkeypatch.setattr(backup, "REMOTE_GID", os.getgid())
    paths = backup.BackupPaths(
        install_root=install_root,
        receipt_root=receipt_root,
        remote_root=remote_root,
        machine_id=machine_id,
        receipt_private_key=private_path,
        receipt_public_key=public_path,
    )
    request = backup.BackupRequest(
        runtime_sha=runtime_sha,
        envelope_sha=envelope_sha,
        web_image=web_image,
        render_image=render_image,
        receipt_path=receipt_root / f"{runtime_sha}.json",
        encryption_key_path=encryption_key,
    )
    spec = backup.ArtifactSpec(
        name="fixture-tar",
        kind="tar-gzip",
        producer=(
            backup.TAR_BIN,
            "--gzip",
            "--create",
            "--file",
            "-",
            "--directory",
            str(source),
            ".",
        ),
        verification="tar_gzip_list",
        coverage=(str(source),),
        required_paths=(source,),
    )
    uid_gid = (os.getuid(), os.getgid())

    first = backup.create_backup(
        request,
        paths=paths,
        artifact_specs=(spec,),
        require_root=False,
        key_owner=uid_gid,
    )
    final_path = remote_root / runtime_sha
    artifact_path = final_path / "fixture-tar.pqenc"
    artifact_before = artifact_path.read_bytes()
    assert first["payload"]["disposition"] == "verified-and-published"
    assert first["payload"]["plaintext_retained"] is False
    assert str(first["payload"]["config_digest"]).startswith("sha256:")
    assert set(entry.name for entry in final_path.iterdir()) == {
        "fixture-tar.pqenc",
        "manifest.v2.json",
    }

    request.receipt_path.unlink()
    recovered = backup.create_backup(
        request,
        paths=paths,
        artifact_specs=(spec,),
        require_root=False,
        key_owner=uid_gid,
    )
    assert recovered["payload"]["disposition"] == "verified-and-published"
    assert artifact_path.read_bytes() == artifact_before
    assert request.receipt_path.is_file()

    idempotent = backup.create_backup(
        request,
        paths=paths,
        artifact_specs=(spec,),
        require_root=False,
        key_owner=uid_gid,
    )
    assert idempotent == recovered

    tampered = bytearray(artifact_path.read_bytes())
    tampered[len(tampered) // 2] ^= 0x01
    artifact_path.write_bytes(tampered)
    artifact_path.chmod(0o600)
    with pytest.raises(
        backup.BackupError,
        match="existing_receipt_artifact_digest_mismatch",
    ):
        backup.create_backup(
            request,
            paths=paths,
            artifact_specs=(spec,),
            require_root=False,
            key_owner=uid_gid,
        )
