from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import propertyquarry_postgres_dr as dr
from scripts import propertyquarry_s3_backup_verify as verify


NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc).timestamp()
VERSION_ID = "3LgX-locked-release-version"
ETAG = "d41d8cd98f00b204e9800998ecf8427e"
RETAIN_UNTIL = "2026-09-11T12:00:00Z"


def _environment(artifact: Path) -> dict[str, str]:
    payload = artifact.read_bytes()
    return {
        "PROPERTYQUARRY_BACKUP_ARTIFACT_PATH": str(artifact),
        "PROPERTYQUARRY_BACKUP_ARTIFACT_SHA256": hashlib.sha256(payload).hexdigest(),
        "PROPERTYQUARRY_BACKUP_ARTIFACT_SIZE_BYTES": str(len(payload)),
        "PROPERTYQUARRY_BACKUP_ARTIFACT_ENCRYPTED": "1",
        "PROPERTYQUARRY_DR_S3_BUCKET": "propertyquarry-dr-eu",
        "PROPERTYQUARRY_DR_S3_KEY_PREFIX": "prod/propertyquarry",
        "PROPERTYQUARRY_DR_S3_OBJECT_LOCK_DAYS": "30",
        "AWS_REGION": "eu-central-1",
        "AWS_ACCESS_KEY_ID": "test-access-id",
        "AWS_SECRET_ACCESS_KEY": "test-secret",
    }


def _provider_payload(artifact: Path) -> dict[str, object]:
    digest = hashlib.sha256(artifact.read_bytes()).digest()
    return {
        "VersionId": VERSION_ID,
        "ETag": f'"{ETAG}"',
        "ContentLength": artifact.stat().st_size,
        "ChecksumSHA256": base64.b64encode(digest).decode("ascii"),
        "ServerSideEncryption": "AES256",
        "ObjectLockMode": "COMPLIANCE",
        "ObjectLockRetainUntilDate": RETAIN_UNTIL,
        "Metadata": {
            "propertyquarry-sha256": hashlib.sha256(
                artifact.read_bytes()
            ).hexdigest(),
        },
    }


def _install_provider(
    monkeypatch: pytest.MonkeyPatch,
    artifact: Path,
    *,
    mutate_head=None,
    mutate_get=None,
) -> list[tuple[str, list[str]]]:
    calls: list[tuple[str, list[str]]] = []
    attestation = {"path": "/trusted/aws", "sha256": "a" * 64}
    monkeypatch.setattr(
        verify,
        "_attest_aws_cli",
        lambda **_kwargs: dict(attestation),
    )

    def run_cli(*, step, arguments, **_kwargs):
        args = [str(value) for value in arguments]
        calls.append((step, args))
        if step == "put_locked_backup_object":
            payload = _provider_payload(artifact)
            return SimpleNamespace(
                stdout=json.dumps(
                    {
                        "VersionId": payload["VersionId"],
                        "ETag": payload["ETag"],
                        "ChecksumSHA256": payload["ChecksumSHA256"],
                    }
                ),
                stderr="",
                returncode=0,
            )
        payload = _provider_payload(artifact)
        payload["ResponseMetadata"] = {
            "RequestId": (
                "head-request-identity"
                if step == "head_locked_backup_object"
                else "get-request-identity"
            )
        }
        if step == "head_locked_backup_object" and mutate_head is not None:
            mutate_head(payload)
        if step == "get_locked_backup_object" and mutate_get is not None:
            mutate_get(payload)
        if step == "get_locked_backup_object":
            Path(args[-1]).write_bytes(artifact.read_bytes())
        return SimpleNamespace(
            stdout=json.dumps(payload),
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr(verify, "_run_attested_aws_cli", run_cli)
    return calls


def test_locked_s3_hook_uploads_and_reads_back_exact_encrypted_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "propertyquarry.dump.gpg"
    artifact.write_bytes(b"gpg-encrypted-propertyquarry-backup")
    artifact.chmod(0o600)
    calls = _install_provider(monkeypatch, artifact)

    receipt = verify.upload_and_verify_s3_backup(
        environ=_environment(artifact),
        clock=lambda: NOW,
    )

    assert [step for step, _args in calls] == [
        "put_locked_backup_object",
        "head_locked_backup_object",
        "get_locked_backup_object",
    ]
    put_args = calls[0][1]
    assert put_args[put_args.index("--object-lock-mode") + 1] == "COMPLIANCE"
    assert (
        put_args[put_args.index("--object-lock-retain-until-date") + 1]
        == RETAIN_UNTIL
    )
    assert receipt["version_id"] == VERSION_ID
    assert receipt["object_lock_mode"] == "COMPLIANCE"
    assert receipt["object_lock_retain_until"] == RETAIN_UNTIL
    assert receipt["immutability_verified"] is True
    assert receipt["checksum_verified"] is True
    assert receipt["provider_request_id"] == (
        "head-request-identity:get-request-identity"
    )
    assert receipt["object_key"].endswith(
        f"/{hashlib.sha256(artifact.read_bytes()).hexdigest()}.dump.gpg"
    )

    projected = dr._validated_off_host_object(
        receipt,
        artifact={
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "size_bytes": artifact.stat().st_size,
            "encrypted": True,
        },
        now_epoch=NOW,
    )
    assert projected["immutability_verified"] is True
    assert projected["object_lock_retain_until"] == RETAIN_UNTIL


def test_hook_rejects_credentials_or_versioning_without_compliance_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "propertyquarry.dump.gpg"
    artifact.write_bytes(b"gpg-encrypted-propertyquarry-backup")
    artifact.chmod(0o600)
    _install_provider(
        monkeypatch,
        artifact,
        mutate_head=lambda payload: payload.update(
            {"ObjectLockMode": "GOVERNANCE"}
        ),
    )

    with pytest.raises(verify.S3BackupVerificationError) as exc:
        verify.upload_and_verify_s3_backup(
            environ=_environment(artifact),
            clock=lambda: NOW,
        )

    assert exc.value.code == "head_object_identity_invalid"


@pytest.mark.parametrize(
    ("key", "value", "expected_code"),
    [
        (
            "PROPERTYQUARRY_DR_S3_KEY_PREFIX",
            "/prod/propertyquarry/",
            "s3_object_key_prefix_invalid",
        ),
        (
            "PROPERTYQUARRY_DR_S3_BUCKET",
            "PropertyQuarry-DR-EU",
            "s3_provider_identity_invalid",
        ),
    ],
)
def test_hook_rejects_noncanonical_provider_configuration_before_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    value: str,
    expected_code: str,
) -> None:
    artifact = tmp_path / "propertyquarry.dump.gpg"
    artifact.write_bytes(b"gpg-encrypted-propertyquarry-backup")
    artifact.chmod(0o600)
    env = _environment(artifact)
    env[key] = value
    monkeypatch.setattr(
        verify,
        "_attest_aws_cli",
        lambda **_kwargs: pytest.fail("AWS must not be contacted"),
    )

    with pytest.raises(verify.S3BackupVerificationError) as exc:
        verify.upload_and_verify_s3_backup(
            environ=env,
            clock=lambda: NOW,
        )

    assert exc.value.code == expected_code


@pytest.mark.parametrize(
    ("mutator", "expected_code"),
    [
        (
            lambda payload: payload.update(
                {"ServerSideEncryption": "aws:kms"}
            ),
            "head_object_identity_invalid",
        ),
        (
            lambda payload: payload.update(
                {"ObjectLockMode": "GOVERNANCE"}
            ),
            "get_object_identity_invalid",
        ),
    ],
)
def test_hook_rejects_weakened_readback_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutator,
    expected_code: str,
) -> None:
    artifact = tmp_path / "propertyquarry.dump.gpg"
    artifact.write_bytes(b"gpg-encrypted-propertyquarry-backup")
    artifact.chmod(0o600)
    if expected_code == "head_object_identity_invalid":
        _install_provider(monkeypatch, artifact, mutate_head=mutator)
    else:
        _install_provider(monkeypatch, artifact, mutate_get=mutator)

    with pytest.raises(verify.S3BackupVerificationError) as exc:
        verify.upload_and_verify_s3_backup(
            environ=_environment(artifact),
            clock=lambda: NOW,
        )

    assert exc.value.code == expected_code


def test_hook_requires_distinct_head_and_get_provider_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "propertyquarry.dump.gpg"
    artifact.write_bytes(b"gpg-encrypted-propertyquarry-backup")
    artifact.chmod(0o600)

    def reuse_request_id(payload: dict[str, object]) -> None:
        payload["ResponseMetadata"] = {"RequestId": "reused-request-identity"}

    _install_provider(
        monkeypatch,
        artifact,
        mutate_head=reuse_request_id,
        mutate_get=reuse_request_id,
    )

    with pytest.raises(verify.S3BackupVerificationError) as exc:
        verify.upload_and_verify_s3_backup(
            environ=_environment(artifact),
            clock=lambda: NOW,
        )

    assert exc.value.code == "provider_request_identity_missing"


def test_hook_rejects_plaintext_before_any_provider_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "propertyquarry.dump"
    artifact.write_bytes(b"plaintext-propertyquarry-backup")
    artifact.chmod(0o600)
    env = _environment(artifact)
    env["PROPERTYQUARRY_BACKUP_ARTIFACT_ENCRYPTED"] = "0"
    monkeypatch.setattr(
        verify,
        "_attest_aws_cli",
        lambda **_kwargs: pytest.fail("AWS must not be contacted"),
    )

    with pytest.raises(verify.S3BackupVerificationError) as exc:
        verify.upload_and_verify_s3_backup(
            environ=env,
            clock=lambda: NOW,
        )

    assert exc.value.code == "backup_artifact_not_encrypted"


def test_hook_requires_no_arguments() -> None:
    assert verify.main(["unexpected"]) == 2


def test_main_reports_attestation_failure_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail() -> dict[str, object]:
        raise dr.DisasterRecoveryError(
            "aws_cli_release_pin_unavailable",
            "Sensitive provider detail must not escape.",
        )

    monkeypatch.setattr(verify, "upload_and_verify_s3_backup", fail)

    assert verify.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "PropertyQuarry locked S3 backup refused: "
        "aws_cli_release_pin_unavailable\n"
    )
    assert "Sensitive provider detail" not in captured.err
