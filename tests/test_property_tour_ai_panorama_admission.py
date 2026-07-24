from __future__ import annotations

import base64
import hashlib
import inspect
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from app.product import property_tour_ai_panorama_admission as admission
from app.product import (
    property_tour_ai_panorama_operation_journal as operation_journal,
)


@dataclass(frozen=True)
class _Case:
    expected: admission.AiPanoramaInstallExpectedBindings
    permit_relpath: str
    permit_path: Path
    ledger_path: Path
    tombstone_root: Path
    control_root: Path
    public_root: Path
    artifact_root: Path
    volume_profile_path: Path
    compose_plan_path: Path
    trust_assertion_path: Path
    keyring_path: Path
    private_key: Ed25519PrivateKey
    key_id: str
    key_sha256: str
    keyring_sha256: str
    now: datetime


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_json(path: Path, value: object, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical(value) + b"\n")
    path.chmod(mode)


def _replace_json(path: Path, value: object, *, mode: int) -> None:
    path.chmod(0o600)
    _write_json(path, value, mode=mode)


def _empty_ledger(instance_id: str = "a" * 32) -> dict[str, object]:
    return {
        "schema": admission.LEDGER_SCHEMA,
        "authority": admission.LEDGER_AUTHORITY,
        "instance_id": instance_id,
        "sequence": 0,
        "tip_sha256": "0" * 64,
        "entries": [],
    }


def _empty_operation_journal(
    instance_id: str = "e" * 32,
) -> dict[str, object]:
    return {
        "schema": operation_journal.OPERATION_JOURNAL_SCHEMA,
        "authority": operation_journal.OPERATION_JOURNAL_AUTHORITY,
        "instance_id": instance_id,
        "sequence": 0,
        "tip_sha256": "0" * 64,
        "entries": [],
    }


def _provision_operation_journal(case: _Case) -> None:
    _write_json(
        case.control_root / operation_journal.OPERATION_JOURNAL_NAME,
        _empty_operation_journal(),
        mode=operation_journal.OPERATION_JOURNAL_MODE,
    )
    lock_path = (
        case.control_root / operation_journal.OPERATION_JOURNAL_LOCK_NAME
    )
    lock_path.write_bytes(b"lock\n")
    lock_path.chmod(operation_journal.OPERATION_JOURNAL_MODE)


def _journal_snapshot(case: _Case) -> tuple[bytes, tuple[int, int]]:
    path = case.control_root / operation_journal.OPERATION_JOURNAL_NAME
    details = path.stat(follow_symlinks=False)
    return path.read_bytes(), (int(details.st_dev), int(details.st_ino))


def _absent_target_manifest(
    expected: admission.AiPanoramaInstallExpectedBindings,
) -> dict[str, object]:
    return {
        "state": "absent",
        "target_relpath": expected.expected_slug,
        "public_root_device": expected.public_tour_root_device,
        "public_root_inode": expected.public_tour_root_inode,
        "reserved_entry_count": 0,
        "reserved_entries_sha256": hashlib.sha256(b"[]").hexdigest(),
    }


def _prepared_release_evidence(
    expected: admission.AiPanoramaInstallExpectedBindings,
    *,
    target_manifest: dict[str, object] | None = None,
    after_sha256: str = "8" * 64,
) -> dict[str, object]:
    return {
        "contract": "propertyquarry.prater_ai_panorama_governed_release.v1",
        "phase": "prepared",
        "slug": expected.expected_slug,
        "listing_url_sha256": hashlib.sha256(
            expected.listing_url.encode("utf-8")
        ).hexdigest(),
        "source_tree_sha256": expected.expected_source_tree_sha256,
        "tour_sha256": expected.expected_tour_sha256,
        "core_manifest_sha256": expected.expected_core_manifest_sha256,
        "materialization_receipt_sha256": (
            expected.expected_materialization_receipt_sha256
        ),
        "candidate_marker_sha256": (
            expected.expected_candidate_marker_sha256
        ),
        "publication_record_sha256": (
            expected.expected_publication_record_sha256
        ),
        "volume_profile_sha256": expected.volume_profile_sha256,
        "public_tour_volume_name": (
            admission.CANONICAL_PUBLIC_TOUR_VOLUME_NAME
        ),
        "public_tour_mount_target": (
            admission.CANONICAL_PUBLIC_TOUR_MOUNT_TARGET
        ),
        "target_manifest": (
            dict(target_manifest)
            if target_manifest is not None
            else _absent_target_manifest(expected)
        ),
        "publication_binding_preparation": {
            "status": (
                "already-bound"
                if after_sha256
                == expected.expected_publication_record_sha256
                else "change-required"
            ),
            "publication_binding_expected_before_sha256": (
                expected.expected_publication_record_sha256
            ),
            "publication_binding_expected_after_sha256": after_sha256,
            "publication_binding_bound_at": "2026-07-24T08:00:00Z",
            "database_mutation_performed": False,
            "private_values_redacted": True,
        },
        "private_values_redacted": True,
    }


def _historical_terminal_evidence(
    operation: operation_journal.AiPanoramaHistoricalOperationObservation,
    expected: admission.AiPanoramaInstallExpectedBindings,
    *,
    event: str,
    classification: str,
    basis: str,
    observed_database_record_sha256: str,
    target_manifest: dict[str, object] | None = None,
    target_state: str = "absent",
    binding_exact: bool = False,
) -> dict[str, object]:
    manifest = (
        dict(target_manifest)
        if target_manifest is not None
        else _absent_target_manifest(expected)
    )
    return {
        "schema": "propertyquarry.prater-ai-panorama-recovery-evidence.v1",
        "version": 1,
        "authority": operation_journal.OPERATION_JOURNAL_AUTHORITY,
        "phase": event,
        "classification": classification,
        "classification_basis": basis,
        "prepared_entry_sha256": (
            operation.prepared_entry_sha256 or "genesis"
        ),
        "prepared_evidence_sha256": (
            operation.prepared_evidence_sha256 or "genesis"
        ),
        "observed_target_manifest": manifest,
        "observed_target_manifest_sha256": hashlib.sha256(
            _canonical(manifest)
        ).hexdigest(),
        "observed_target_identity": {
            "state": target_state,
            "source_tree_sha256": expected.expected_source_tree_sha256,
            "source_tour_sha256": expected.expected_tour_sha256,
            "core_manifest_sha256": (
                expected.expected_core_manifest_sha256
            ),
            "public_root_device": expected.public_tour_root_device,
            "public_root_inode": expected.public_tour_root_inode,
            "private_values_redacted": True,
        },
        "observed_database_record_sha256": (
            observed_database_record_sha256
        ),
        "observed_publication_binding_exact": binding_exact,
        "publication_binding_expected_before_sha256": (
            expected.expected_publication_record_sha256
        ),
        "publication_binding_expected_after_sha256": (
            operation.prepared_evidence.get(
                "publication_binding_preparation", {}
            ).get(
                "publication_binding_expected_after_sha256",
                expected.expected_publication_record_sha256,
            )
        ),
        "publication_binding_plan_status": (
            operation.prepared_evidence.get(
                "publication_binding_preparation", {}
            ).get("status", "not-prepared")
        ),
        "historical_consumption_binding": {
            "ledger_instance_id": (
                operation.historical_consumption.ledger_instance_id
            ),
            "ledger_sequence": (
                operation.historical_consumption.ledger_sequence
            ),
            "ledger_entry_sha256": (
                operation.historical_consumption.ledger_entry_sha256
            ),
        },
        "database_mutation_performed": False,
        "public_target_mutation_performed": False,
        "private_values_redacted": True,
    }


def _write_fresh_attempt(
    case: _Case,
    *,
    request_id: str,
    nonce: str,
    now: datetime | None = None,
) -> tuple[
    admission.AiPanoramaInstallExpectedBindings,
    str,
    Path,
]:
    expected = replace(case.expected, request_id=request_id)
    relpath = admission.ai_panorama_install_permit_relpath(request_id)
    path = case.permit_path.parent / relpath
    _write_json(
        path,
        _permit_envelope(
            expected,
            private_key=case.private_key,
            now=now or case.now,
            nonce=nonce,
        ),
        mode=admission.CONTROLLER_FILE_MODE,
    )
    return expected, relpath, path


def _write_panorama_keyring(
    tmp_path: Path,
    *,
    private_key: Ed25519PrivateKey,
    now: datetime,
    activates_at: datetime | None = None,
) -> tuple[Path, str, str, str]:
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    public_text = base64.urlsafe_b64encode(public_bytes).decode("ascii").rstrip("=")
    key_id = "release-control-test-key"
    key_sha256 = hashlib.sha256(public_bytes).hexdigest()
    keyring = {
        "schema": admission.KEYRING_SCHEMA,
        "version": 1,
        "authority": admission.LEDGER_AUTHORITY,
        "algorithm": "Ed25519",
        "status": "active",
        "usage": admission.PERMIT_KEY_USAGE,
        "rotation_epoch": 1,
        "minimum_accepted_epoch": 1,
        "keys": [
            {
                "key_id": key_id,
                "epoch": 1,
                "usage": admission.PERMIT_KEY_USAGE,
                "public_key": public_text,
                "public_key_sha256": key_sha256,
                "activates_at": (
                    activates_at or now - timedelta(days=1)
                ).isoformat().replace("+00:00", "Z"),
                "accept_until": None,
                "revoked_at": None,
            }
        ],
    }
    keyring_path = tmp_path / "ai-panorama-install-keyring.v1.json"
    _write_json(keyring_path, keyring, mode=0o444)
    return (
        keyring_path,
        key_id,
        key_sha256,
        hashlib.sha256(keyring_path.read_bytes()).hexdigest(),
    )


def _mount_id(path: Path) -> int:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        return admission._descriptor_mount_id(
            descriptor,
            code="test_mount_id_unavailable",
        )
    finally:
        os.close(descriptor)


def _permit_envelope(
    expected: admission.AiPanoramaInstallExpectedBindings,
    *,
    private_key: Ed25519PrivateKey,
    now: datetime,
    nonce: str = "b" * 32,
    lifetime_seconds: int = 180,
) -> dict[str, object]:
    permit = {
        **admission._expected_payload(expected),
        "issued_at": (now - timedelta(seconds=5))
        .isoformat()
        .replace("+00:00", "Z"),
        "expires_at": (now + timedelta(seconds=lifetime_seconds))
        .isoformat()
        .replace("+00:00", "Z"),
        "nonce": nonce,
    }
    envelope: dict[str, object] = {
        "schema": admission.PERMIT_SCHEMA,
        "version": admission.PERMIT_VERSION,
        "permit": permit,
        "signature": {
            "algorithm": "Ed25519",
            "key_id": "release-control-test-key",
            "encoding": "base64url",
            "value": "",
        },
    }
    signature = private_key.sign(admission._signature_preimage(envelope))
    envelope["signature"]["value"] = (  # type: ignore[index]
        base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    )
    return envelope


def _trust_assertion(
    expected: admission.AiPanoramaInstallExpectedBindings,
) -> dict[str, object]:
    return {
        "schema": admission.TRUST_ASSERTION_SCHEMA,
        "version": 1,
        "authority": admission.LEDGER_AUTHORITY,
        "status": "active",
        **{
            field: getattr(expected, field)
            for field in (
                "subject",
                "actor_principal_id",
                "repository",
                "git_ref",
                "git_head_sha",
                "workflow_ref",
                "job",
                "environment",
                "review_receipt_sha256",
                "web_image",
                "web_image_id",
                "key_usage",
                "key_id",
                "key_epoch",
                "key_sha256",
                "keyring_sha256",
                "volume_profile_sha256",
                "compose_plan_sha256",
                "volume_id",
                "artifact_root_device",
                "artifact_root_inode",
                "public_tour_root_device",
                "public_tour_root_inode",
                "execution_lease_seconds",
            )
        },
    }


def _rewrite_trust_and_permit(
    case: _Case,
    expected: admission.AiPanoramaInstallExpectedBindings,
) -> None:
    _replace_json(
        case.trust_assertion_path,
        _trust_assertion(expected),
        mode=admission.RUNTIME_PROFILE_MODE,
    )
    _replace_json(
        case.permit_path,
        _permit_envelope(
            expected,
            private_key=case.private_key,
            now=case.now,
        ),
        mode=admission.CONTROLLER_FILE_MODE,
    )


def _rewrite_keyring_and_bind(
    case: _Case,
    keyring: dict[str, object],
) -> admission.AiPanoramaInstallExpectedBindings:
    _replace_json(case.keyring_path, keyring, mode=0o444)
    expected = replace(
        case.expected,
        keyring_sha256=hashlib.sha256(
            case.keyring_path.read_bytes()
        ).hexdigest(),
    )
    _rewrite_trust_and_permit(case, expected)
    return expected


@pytest.fixture
def permit_case(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> _Case:
    now = datetime(2026, 7, 24, 8, 0, 0, tzinfo=timezone.utc)
    private_key = Ed25519PrivateKey.generate()
    (
        keyring_path,
        key_id,
        key_sha256,
        keyring_sha256,
    ) = _write_panorama_keyring(
        tmp_path,
        private_key=private_key,
        now=now,
    )
    monkeypatch.setattr(admission, "_utc_now", lambda: now)
    monkeypatch.setattr(
        admission,
        "CANONICAL_PUBLIC_TOUR_RUNTIME_UID",
        os.geteuid(),
    )
    monkeypatch.setattr(
        admission,
        "CANONICAL_PUBLIC_TOUR_RUNTIME_GID",
        os.getegid(),
    )
    monkeypatch.setattr(
        admission,
        "_descriptor_mount_is_read_only",
        lambda _descriptor, *, code: True,
    )

    control_root = tmp_path / "controller"
    permit_root = control_root / "permits"
    tombstone_root = control_root / "tombstones"
    artifact_root = tmp_path / "native-authority" / "prater-v1"
    public_root = tmp_path / "docker-volume-mountpoint"
    for path in (
        control_root,
        permit_root,
        tombstone_root,
        artifact_root,
        public_root,
    ):
        path.mkdir(parents=True, mode=0o700)

    ledger_path = control_root / "consumption-ledger.v2.json"
    lock_path = control_root / "consumption-ledger.v2.lock"
    _write_json(ledger_path, _empty_ledger(), mode=0o600)
    lock_path.write_bytes(b"lock\n")
    lock_path.chmod(0o600)
    volume_profile_path = tmp_path / "public-tour-volume-profile.v2.json"
    compose_plan_path = tmp_path / "public-tour-compose-plan.v1.json"
    trust_assertion_path = (
        tmp_path / "ai-panorama-install-trust-assertion.v1.json"
    )
    public_host_mount_source = Path(
        "/synthetic/docker-root/volumes/"
        "property_propertyquarry_governed_public_tours/_data"
    )

    paths = admission._ControllerPaths(
        control_root=control_root,
        permit_root=permit_root,
        tombstone_root=tombstone_root,
        sealed_artifact_root=artifact_root,
        ledger_path=ledger_path,
        ledger_lock_path=lock_path,
        volume_profile_path=volume_profile_path,
        compose_plan_path=compose_plan_path,
        trust_assertion_path=trust_assertion_path,
        keyring_path=keyring_path,
        public_tour_runtime_root=public_root,
        required_uid=os.geteuid(),
    )
    monkeypatch.setattr(admission, "_CONTROLLER_PATHS", paths)

    slug = "prater-messe-maisonette-ai-360-053ad185e1c44b2e"
    artifact_relpath = f"bundle/{slug}"
    source_bundle = artifact_root / artifact_relpath
    source_bundle.mkdir(parents=True, mode=0o700)
    marker_path = artifact_root / admission.CANONICAL_CANDIDATE_MARKER_RELPATH
    marker_bytes = _canonical(
        {
            "schema": "propertyquarry.ai-panorama-candidate.v1",
            "status": "approved",
            "slug": slug,
        }
    ) + b"\n"
    marker_path.write_bytes(marker_bytes)
    marker_path.chmod(0o400)
    receipt_relpath = "materialization.receipt.json"
    receipt_path = artifact_root / receipt_relpath
    receipt_bytes = _canonical(
        {
            "contract": "propertyquarry.ai_panorama_materialization_receipt.v1",
            "status": "pass",
            "slug": slug,
        }
    ) + b"\n"
    receipt_path.write_bytes(receipt_bytes)
    receipt_path.chmod(0o400)

    artifact_details = artifact_root.stat(follow_symlinks=False)
    public_details = public_root.stat(follow_symlinks=False)
    volume_id = admission.CANONICAL_PUBLIC_TOUR_VOLUME_ID
    web_image = (
        f"{admission.CANONICAL_WEB_IMAGE_REPOSITORY}@sha256:"
        + "c" * 64
    )
    web_image_id = "sha256:" + "d" * 64
    compose_plan = {
        "schema": admission.COMPOSE_PLAN_SCHEMA,
        "version": 1,
        "authority": admission.LEDGER_AUTHORITY,
        "status": "active",
        "environment": admission.CANONICAL_ENVIRONMENT,
        "web_image": web_image,
        "web_image_id": web_image_id,
        "volume_id": volume_id,
        "storage_kind": admission.CANONICAL_PUBLIC_TOUR_STORAGE_KIND,
        "docker_volume_name": admission.CANONICAL_PUBLIC_TOUR_VOLUME_NAME,
        "container_mount_target": admission.CANONICAL_PUBLIC_TOUR_MOUNT_TARGET,
        "artifact_mount_read_only": True,
        "web_mount_read_only": True,
        "publisher_mount_read_write": True,
        "artifact_root_device": artifact_details.st_dev,
        "artifact_root_inode": artifact_details.st_ino,
        "public_tour_root_device": public_details.st_dev,
        "public_tour_root_inode": public_details.st_ino,
    }
    _write_json(
        compose_plan_path,
        compose_plan,
        mode=admission.RUNTIME_PROFILE_MODE,
    )
    compose_plan_sha256 = hashlib.sha256(
        compose_plan_path.read_bytes()
    ).hexdigest()
    volume_profile = {
        "schema": admission.VOLUME_PROFILE_SCHEMA,
        "version": 2,
        "authority": admission.LEDGER_AUTHORITY,
        "status": "active",
        "environment": admission.CANONICAL_ENVIRONMENT,
        "volume_id": volume_id,
        "logical_purpose": admission.CANONICAL_PUBLIC_TOUR_LOGICAL_PURPOSE,
        "application_setting": admission.CANONICAL_PUBLIC_TOUR_SETTING,
        "application_setting_value": (
            admission.CANONICAL_PUBLIC_TOUR_MOUNT_TARGET
        ),
        "storage_kind": admission.CANONICAL_PUBLIC_TOUR_STORAGE_KIND,
        "docker_volume_name": admission.CANONICAL_PUBLIC_TOUR_VOLUME_NAME,
        "container_mount_source": str(public_host_mount_source),
        "container_mount_target": admission.CANONICAL_PUBLIC_TOUR_MOUNT_TARGET,
        "runtime_uid": admission.CANONICAL_PUBLIC_TOUR_RUNTIME_UID,
        "runtime_gid": admission.CANONICAL_PUBLIC_TOUR_RUNTIME_GID,
        "artifact_root": str(artifact_root),
        "artifact_root_device": artifact_details.st_dev,
        "artifact_root_inode": artifact_details.st_ino,
        "artifact_mount_read_only": True,
        "public_tour_root": str(public_root),
        "public_tour_root_device": public_details.st_dev,
        "public_tour_root_inode": public_details.st_ino,
        "compose_plan_sha256": compose_plan_sha256,
    }
    _write_json(
        volume_profile_path,
        volume_profile,
        mode=admission.RUNTIME_PROFILE_MODE,
    )
    volume_profile_sha256 = hashlib.sha256(
        volume_profile_path.read_bytes()
    ).hexdigest()

    expected = admission.AiPanoramaInstallExpectedBindings(
        subject=admission.CANONICAL_SUBJECT,
        actor_principal_id="propertyquarry-release-controller",
        owner_principal_id="private-owner@example.invalid",
        search_run_id="98bed75e984549c6bd4371d602662ab8",
        candidate_ref="053ad185e1c44b2e",
        external_id="1807240910",
        listing_url=(
            "https://www.willhaben.at/iad/immobilien/d/1807240910/"
        ),
        source_ref="property-scout:1807240910",
        provider_key="willhaben",
        expected_slug=slug,
        expected_source_tree_sha256="1" * 64,
        expected_tour_sha256="2" * 64,
        expected_core_manifest_sha256="3" * 64,
        expected_materialization_receipt_sha256=hashlib.sha256(
            receipt_bytes
        ).hexdigest(),
        expected_candidate_marker_sha256=hashlib.sha256(
            marker_bytes
        ).hexdigest(),
        expected_publication_record_sha256="5" * 64,
        artifact_relpath=artifact_relpath,
        materialization_receipt_relpath=receipt_relpath,
        request_id="7" * 32,
        repository=admission.CANONICAL_REPOSITORY,
        git_ref=admission.CANONICAL_GIT_REF,
        git_head_sha="d" * 40,
        workflow_ref=admission.CANONICAL_WORKFLOW_REF,
        job=admission.CANONICAL_JOB,
        environment=admission.CANONICAL_ENVIRONMENT,
        review_receipt_sha256="6" * 64,
        web_image=web_image,
        web_image_id=web_image_id,
        key_usage=admission.PERMIT_KEY_USAGE,
        key_id=key_id,
        key_epoch=1,
        key_sha256=key_sha256,
        keyring_sha256=keyring_sha256,
        volume_profile_sha256=volume_profile_sha256,
        compose_plan_sha256=compose_plan_sha256,
        volume_id=volume_id,
        artifact_root_device=artifact_details.st_dev,
        artifact_root_inode=artifact_details.st_ino,
        public_tour_root_device=public_details.st_dev,
        public_tour_root_inode=public_details.st_ino,
        execution_lease_seconds=600,
    )
    trust_assertion = _trust_assertion(expected)
    _write_json(
        trust_assertion_path,
        trust_assertion,
        mode=admission.RUNTIME_PROFILE_MODE,
    )

    permit_relpath = admission.ai_panorama_install_permit_relpath(
        expected.request_id
    )
    permit_path = permit_root / permit_relpath
    _write_json(
        permit_path,
        _permit_envelope(
            expected,
            private_key=private_key,
            now=now,
        ),
        mode=admission.CONTROLLER_FILE_MODE,
    )
    return _Case(
        expected=expected,
        permit_relpath=permit_relpath,
        permit_path=permit_path,
        ledger_path=ledger_path,
        tombstone_root=tombstone_root,
        control_root=control_root,
        public_root=public_root,
        artifact_root=artifact_root,
        volume_profile_path=volume_profile_path,
        compose_plan_path=compose_plan_path,
        trust_assertion_path=trust_assertion_path,
        keyring_path=keyring_path,
        private_key=private_key,
        key_id=key_id,
        key_sha256=key_sha256,
        keyring_sha256=keyring_sha256,
        now=now,
    )


def test_verified_admission_is_exact_fixed_root_and_revalidated(
    permit_case: _Case,
) -> None:
    assert str(admission.SEALED_ARTIFACT_ROOT) == (
        "/var/lib/propertyquarry-release-single-host-v2/"
        "ai-panorama-artifacts/prater-v1"
    )
    dry_run = admission.verify_ai_panorama_install_permit(
        permit_case.permit_relpath,
        permit_case.expected,
    )

    assert dry_run.permit_verified is True
    assert dry_run.nonce_consumed is False
    assert dry_run.incoming_root == permit_case.artifact_root
    assert dry_run.source_bundle == (
        permit_case.artifact_root / permit_case.expected.artifact_relpath
    )
    assert dry_run.candidate_marker_path == (
        permit_case.artifact_root
        / admission.CANONICAL_CANDIDATE_MARKER_RELPATH
    )
    assert dry_run.public_tour_dir == permit_case.public_root
    assert dry_run.artifact_relpath == (
        f"bundle/{permit_case.expected.expected_slug}"
    )
    assert (
        dry_run.materialization_receipt_relpath
        == "materialization.receipt.json"
    )
    assert (
        dry_run.public_tour_volume_name
        == "property_propertyquarry_governed_public_tours"
    )
    assert (
        dry_run.public_tour_mount_target
        == "/data/governed_public_property_tours"
    )
    assert dry_run.public_control_url.endswith(
        f"/tours/{permit_case.expected.expected_slug}/control"
    )
    assert (
        admission.revalidate_ai_panorama_install_admission(
            dry_run,
            require_consumed=False,
        )
        == dry_run
    )
    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_permit_not_consumed",
    ):
        admission.validate_verified_admission(dry_run)

    consumed = admission.consume_ai_panorama_install_permit(
        permit_case.permit_relpath,
        permit_case.expected,
    )

    assert consumed.nonce_consumed is True
    assert consumed._ledger_sequence == 1
    assert admission.validate_verified_admission(consumed) == consumed
    assert admission.is_verified_ai_panorama_install_admission(
        consumed,
        require_consumed=True,
    )
    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_permit_replayed",
    ):
        admission.consume_ai_panorama_install_permit(
            permit_case.permit_relpath,
            permit_case.expected,
        )


@pytest.mark.parametrize(
    "relpath",
    (
        "prater-ai-panorama-install.json",
        f"prater-ai-panorama-install-{'7' * 31}.v2.json",
        f"prater-ai-panorama-install-{'7' * 32}.json",
        f"prater-ai-panorama-install-{'7' * 32}.V2.json",
        f"prater-ai-panorama-install-{'8' * 32}.v2.json",
        f"nested/prater-ai-panorama-install-{'7' * 32}.v2.json",
        f"../prater-ai-panorama-install-{'7' * 32}.v2.json",
    ),
)
def test_permit_leaf_is_exactly_derived_from_signed_request_id(
    permit_case: _Case,
    relpath: str,
) -> None:
    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_permit_relpath_invalid",
    ):
        admission.verify_ai_panorama_install_permit(
            relpath,
            permit_case.expected,
        )


@pytest.mark.parametrize("mutation", ("wrong-mode", "hardlink"))
def test_per_attempt_permit_leaf_requires_private_single_link_file(
    permit_case: _Case,
    mutation: str,
) -> None:
    if mutation == "wrong-mode":
        permit_case.permit_path.chmod(0o400)
    else:
        os.link(
            permit_case.permit_path,
            permit_case.permit_path.with_name("copied-permit.v2.json"),
        )

    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_permit_file_invalid",
    ):
        admission.verify_ai_panorama_install_permit(
            permit_case.permit_relpath,
            permit_case.expected,
        )


@pytest.mark.parametrize("terminal_event", ("failed-clean", "rolled-back"))
def test_fresh_unique_attempt_survives_consumed_clean_predecessor(
    permit_case: _Case,
    terminal_event: str,
) -> None:
    _provision_operation_journal(permit_case)
    first = admission.consume_ai_panorama_install_permit(
        permit_case.permit_relpath,
        permit_case.expected,
    )
    first_handle = operation_journal.begin_ai_panorama_install_operation(
        first,
        evidence={"attempt": 1, "phase": "prepared"},
    )
    operation_journal.finish_ai_panorama_install_operation(
        first_handle,
        event=terminal_event,
        evidence={"attempt": 1, "phase": terminal_event},
    )

    second_expected, second_relpath, second_path = _write_fresh_attempt(
        permit_case,
        request_id="8" * 32,
        nonce="c" * 32,
    )
    second = admission.consume_ai_panorama_install_permit(
        second_relpath,
        second_expected,
    )
    second_handle = operation_journal.begin_ai_panorama_install_operation(
        second,
        evidence={"attempt": 2, "phase": "prepared"},
    )
    operation_journal.finish_ai_panorama_install_operation(
        second_handle,
        event="failed-clean",
        evidence={"attempt": 2, "phase": "failed-clean"},
    )

    assert second_path.exists()
    assert permit_case.permit_path.exists()
    assert second._ledger_sequence == 2
    assert second.request_id != first.request_id
    assert second.nonce != first.nonce
    assert second.permit_sha256 != first.permit_sha256
    assert second_handle.operation_id != first_handle.operation_id
    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_permit_replayed",
    ):
        admission.consume_ai_panorama_install_permit(
            permit_case.permit_relpath,
            permit_case.expected,
        )


def test_fresh_unique_attempt_survives_expired_unconsumed_predecessor(
    permit_case: _Case,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission.verify_ai_panorama_install_permit(
        permit_case.permit_relpath,
        permit_case.expected,
    )
    retry_now = permit_case.now + timedelta(minutes=4)
    monkeypatch.setattr(admission, "_utc_now", lambda: retry_now)
    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_permit_not_fresh",
    ):
        admission.verify_ai_panorama_install_permit(
            permit_case.permit_relpath,
            permit_case.expected,
        )

    second_expected, second_relpath, _second_path = _write_fresh_attempt(
        permit_case,
        request_id="8" * 32,
        nonce="c" * 32,
        now=retry_now,
    )
    second = admission.consume_ai_panorama_install_permit(
        second_relpath,
        second_expected,
    )

    assert second.nonce_consumed is True
    assert second._ledger_sequence == 1
    assert permit_case.permit_path.exists()


def test_copied_permit_bytes_cannot_authorize_another_request_leaf(
    permit_case: _Case,
) -> None:
    second_expected = replace(permit_case.expected, request_id="8" * 32)
    second_relpath = admission.ai_panorama_install_permit_relpath(
        second_expected.request_id
    )
    copied_path = permit_case.permit_path.parent / second_relpath
    copied_path.write_bytes(permit_case.permit_path.read_bytes())
    copied_path.chmod(admission.CONTROLLER_FILE_MODE)

    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_permit_binding_mismatch",
    ):
        admission.verify_ai_panorama_install_permit(
            second_relpath,
            second_expected,
        )


def test_replacing_selected_leaf_breaks_admission_identity(
    permit_case: _Case,
) -> None:
    verified = admission.verify_ai_panorama_install_permit(
        permit_case.permit_relpath,
        permit_case.expected,
    )
    replacement = permit_case.permit_path.with_name(".replacement")
    replacement.write_bytes(permit_case.permit_path.read_bytes())
    replacement.chmod(admission.CONTROLLER_FILE_MODE)
    os.replace(replacement, permit_case.permit_path)

    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_admission_context_changed",
    ):
        admission.revalidate_ai_panorama_install_admission(
            verified,
            require_consumed=False,
        )


def test_partial_old_tombstone_does_not_authorize_or_block_unique_attempt(
    permit_case: _Case,
) -> None:
    old_request_sha256 = hashlib.sha256(
        permit_case.expected.request_id.encode("ascii")
    ).hexdigest()
    orphan = permit_case.tombstone_root / (
        f"request-{old_request_sha256}.json"
    )
    orphan.write_bytes(b"orphan-evidence\n")
    orphan.chmod(admission.CONTROLLER_FILE_MODE)
    second_expected, second_relpath, _second_path = _write_fresh_attempt(
        permit_case,
        request_id="8" * 32,
        nonce="c" * 32,
    )

    second = admission.consume_ai_panorama_install_permit(
        second_relpath,
        second_expected,
    )

    assert second.nonce_consumed is True
    assert second._ledger_sequence == 1
    assert orphan.read_bytes() == b"orphan-evidence\n"
    assert not (
        permit_case.tombstone_root
        / f"request-{old_request_sha256}.json"
    ).samefile(
        permit_case.tombstone_root
        / f"request-{hashlib.sha256(second.request_id.encode('ascii')).hexdigest()}.json"
    )


def test_recovery_required_attempt_cannot_be_reused_or_replayed(
    permit_case: _Case,
) -> None:
    _provision_operation_journal(permit_case)
    consumed = admission.consume_ai_panorama_install_permit(
        permit_case.permit_relpath,
        permit_case.expected,
    )
    handle = operation_journal.begin_ai_panorama_install_operation(
        consumed,
        evidence={"attempt": 1, "phase": "prepared"},
    )
    operation_journal.finish_ai_panorama_install_operation(
        handle,
        event="recovery-required",
        evidence={"attempt": 1, "phase": "recovery-required"},
    )

    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_permit_replayed",
    ):
        admission.consume_ai_panorama_install_permit(
            permit_case.permit_relpath,
            permit_case.expected,
        )
    with pytest.raises(
        operation_journal.AiPanoramaOperationJournalError,
        match="ai_panorama_operation_journal_transition_invalid",
    ):
        operation_journal.begin_ai_panorama_install_operation(
            consumed,
            evidence={"attempt": 1, "phase": "prepared-again"},
        )


def test_signature_context_and_exact_release_bindings_fail_closed(
    permit_case: _Case,
) -> None:
    wrong = replace(permit_case.expected, git_head_sha="e" * 40)
    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_trusted_context_mismatch",
    ):
        admission.verify_ai_panorama_install_permit(
            permit_case.permit_relpath,
            wrong,
        )

    envelope = json.loads(permit_case.permit_path.read_text(encoding="utf-8"))
    envelope["permit"]["nonce"] = "c" * 32
    permit_case.permit_path.chmod(0o600)
    _write_json(
        permit_case.permit_path,
        envelope,
        mode=admission.CONTROLLER_FILE_MODE,
    )
    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_signature_invalid",
    ):
        admission.verify_ai_panorama_install_permit(
            permit_case.permit_relpath,
            permit_case.expected,
        )


def test_concurrent_consumers_have_exactly_one_winner(
    permit_case: _Case,
) -> None:
    def consume() -> object:
        try:
            return admission.consume_ai_panorama_install_permit(
                permit_case.permit_relpath,
                permit_case.expected,
            )
        except admission.AiPanoramaInstallPermitError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: consume(), range(2)))

    winners = [
        result
        for result in results
        if isinstance(result, admission.VerifiedAiPanoramaInstallAdmission)
    ]
    failures = [
        result
        for result in results
        if isinstance(result, admission.AiPanoramaInstallPermitError)
    ]
    assert len(winners) == 1
    assert [failure.code for failure in failures] == [
        "ai_panorama_permit_replayed"
    ]
    assert admission.validate_verified_admission(winners[0]) == winners[0]


def test_deletion_reset_and_alternate_ledger_fail_closed(
    permit_case: _Case,
) -> None:
    consumed = admission.consume_ai_panorama_install_permit(
        permit_case.permit_relpath,
        permit_case.expected,
    )
    permit_case.ledger_path.unlink()
    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_ledger_unavailable",
    ):
        admission.validate_verified_admission(consumed)
    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_ledger_unavailable",
    ):
        admission.consume_ai_panorama_install_permit(
            permit_case.permit_relpath,
            permit_case.expected,
        )

    _write_json(
        permit_case.ledger_path,
        _empty_ledger(instance_id="f" * 32),
        mode=0o600,
    )
    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_permit_replayed",
    ):
        admission.consume_ai_panorama_install_permit(
            permit_case.permit_relpath,
            permit_case.expected,
        )
    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_consumption_record_missing",
    ):
        admission.validate_verified_admission(consumed)


def test_constructor_boolean_and_permit_deletion_do_not_authorize(
    permit_case: _Case,
) -> None:
    dry_run = admission.verify_ai_panorama_install_permit(
        permit_case.permit_relpath,
        permit_case.expected,
    )
    forged = replace(dry_run, nonce_consumed=True)
    assert not admission.is_verified_ai_panorama_install_admission(
        forged,
        require_consumed=True,
    )

    consumed = admission.consume_ai_panorama_install_permit(
        permit_case.permit_relpath,
        permit_case.expected,
    )
    permit_case.permit_path.unlink()
    assert not admission.is_verified_ai_panorama_install_admission(
        consumed,
        require_consumed=True,
    )


def test_public_api_has_no_caller_selected_authority_paths(
    permit_case: _Case,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    assert tuple(
        inspect.signature(
            admission.consume_ai_panorama_install_permit
        ).parameters
    ) == ("permit_relpath", "expected")
    assert tuple(
        inspect.signature(
            admission.revalidate_ai_panorama_install_admission
        ).parameters
    ) == ("admission", "require_consumed")
    assert tuple(
        inspect.signature(
            admission.revalidate_ai_panorama_install_recovery
        ).parameters
    ) == ("admission",)
    assert tuple(
        inspect.signature(
            admission.recover_ai_panorama_install_consumption
        ).parameters
    ) == ("permit_relpath", "expected")
    assert tuple(
        inspect.signature(
            admission.load_ai_panorama_install_historical_consumption
        ).parameters
    ) == ("permit_relpath", "expected")
    assert (
        tuple(
            inspect.signature(
                admission.load_ai_panorama_install_trusted_context
            ).parameters
        )
        == ()
    )
    assert not any(
        name.startswith("register_ai_panorama")
        for name in vars(admission)
    )

    consumed = admission.consume_ai_panorama_install_permit(
        permit_case.permit_relpath,
        permit_case.expected,
    )
    alternate = tmp_path / "alternate-controller"
    alternate.mkdir(mode=0o700)
    monkeypatch.setattr(
        admission,
        "_CONTROLLER_PATHS",
        replace(
            admission._CONTROLLER_PATHS,
            control_root=alternate,
            permit_root=alternate / "permits",
            tombstone_root=alternate / "tombstones",
            sealed_artifact_root=alternate / "sealed-artifacts",
            ledger_path=alternate / "consumption-ledger.v2.json",
            ledger_lock_path=alternate / "consumption-ledger.v2.lock",
        ),
    )
    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_admission_invalid",
    ):
        admission.validate_verified_admission(consumed)


def test_volume_identity_and_artifact_symlinks_are_rejected(
    permit_case: _Case,
) -> None:
    source_bundle = (
        permit_case.artifact_root / permit_case.expected.artifact_relpath
    )
    moved = source_bundle.with_name(source_bundle.name + "-real")
    source_bundle.rename(moved)
    source_bundle.symlink_to(moved, target_is_directory=True)
    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_source_bundle_invalid",
    ):
        admission.verify_ai_panorama_install_permit(
            permit_case.permit_relpath,
            permit_case.expected,
        )


def test_sealed_artifact_root_must_be_read_only(
    permit_case: _Case,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        admission,
        "_descriptor_mount_is_read_only",
        lambda _descriptor, *, code: False,
    )
    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_artifact_root_not_read_only",
    ):
        admission.verify_ai_panorama_install_permit(
            permit_case.permit_relpath,
            permit_case.expected,
        )


def test_candidate_marker_is_fixed_mode_and_digest_bound(
    permit_case: _Case,
) -> None:
    marker_path = (
        permit_case.artifact_root
        / admission.CANONICAL_CANDIDATE_MARKER_RELPATH
    )
    marker_path.chmod(0o600)
    marker_path.write_bytes(b'{"status":"tampered"}\n')
    marker_path.chmod(0o400)
    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_candidate_marker_digest_mismatch",
    ):
        admission.verify_ai_panorama_install_permit(
            permit_case.permit_relpath,
            permit_case.expected,
        )


def test_short_ttl_and_external_keyring_are_mandatory(
    permit_case: _Case,
) -> None:
    permit_case.permit_path.chmod(0o600)
    _write_json(
        permit_case.permit_path,
        _permit_envelope(
            permit_case.expected,
            private_key=permit_case.private_key,
            now=permit_case.now,
            lifetime_seconds=301,
        ),
        mode=admission.CONTROLLER_FILE_MODE,
    )
    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_permit_not_fresh",
    ):
        admission.verify_ai_panorama_install_permit(
            permit_case.permit_relpath,
            permit_case.expected,
        )

    permit_case.permit_path.chmod(0o600)
    _write_json(
        permit_case.permit_path,
        _permit_envelope(
            permit_case.expected,
            private_key=permit_case.private_key,
            now=permit_case.now,
        ),
        mode=admission.CONTROLLER_FILE_MODE,
    )
    permit_case.keyring_path.unlink()
    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_release_keyring_unavailable",
    ):
        admission.verify_ai_panorama_install_permit(
            permit_case.permit_relpath,
            permit_case.expected,
        )


def test_trusted_context_actor_owner_source_ref_and_private_repr(
    permit_case: _Case,
) -> None:
    trusted = admission.load_ai_panorama_install_trusted_context()
    assert trusted.actor_principal_id == permit_case.expected.actor_principal_id
    assert trusted.git_head_sha == permit_case.expected.git_head_sha
    assert trusted.key_usage == admission.PERMIT_KEY_USAGE
    assert not hasattr(trusted, "owner_principal_id")

    consumed = admission.consume_ai_panorama_install_permit(
        permit_case.permit_relpath,
        permit_case.expected,
    )
    assert consumed.actor_principal_id == "propertyquarry-release-controller"
    assert consumed.owner_principal_id == "private-owner@example.invalid"
    assert (
        consumed.authenticated_principal_id
        == "private-owner@example.invalid"
    )
    assert consumed.source_ref == "property-scout:1807240910"

    private_values = (
        consumed.owner_principal_id,
        consumed.listing_url,
        consumed.source_ref,
    )
    for private_value in private_values:
        assert private_value not in repr(permit_case.expected)
        assert private_value not in repr(consumed)
        assert private_value not in permit_case.ledger_path.read_text(
            encoding="utf-8"
        )
        assert all(
            private_value not in path.read_text(encoding="utf-8")
            for path in permit_case.tombstone_root.iterdir()
        )
    assert consumed.request_id not in repr(permit_case.expected)
    assert consumed.request_id not in repr(consumed)
    assert consumed._permit_relpath in permit_case.ledger_path.read_text(
        encoding="utf-8"
    )
    assert all(
        consumed._permit_relpath in path.read_text(encoding="utf-8")
        for path in permit_case.tombstone_root.iterdir()
    )
    assert hashlib.sha256(consumed.request_id.encode("ascii")).hexdigest() in (
        permit_case.ledger_path.read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    ("field", "value", "code"),
    (
        (
            "source_ref",
            "https://www.willhaben.at/iad/immobilien/d/1807240910",
            "ai_panorama_source_ref_invalid",
        ),
        (
            "request_id",
            "release-request-1807240910",
            "ai_panorama_request_id_invalid",
        ),
        (
            "key_usage",
            "propertyquarry.some-other-purpose",
            "ai_panorama_release_context_invalid",
        ),
        (
            "artifact_relpath",
            "incoming_property_tours/prater/bundle",
            "ai_panorama_artifact_layout_invalid",
        ),
        (
            "owner_principal_id",
            "propertyquarry-release-controller",
            "ai_panorama_principal_roles_not_separated",
        ),
        (
            "subject",
            "repo:ArchonMegalon/propertyquarry:"
            "environment:propertyquarry-production",
            "ai_panorama_release_context_invalid",
        ),
    ),
)
def test_opaque_identifiers_and_key_purpose_fail_closed(
    permit_case: _Case,
    field: str,
    value: str,
    code: str,
) -> None:
    expected = replace(permit_case.expected, **{field: value})
    with pytest.raises(admission.AiPanoramaInstallPermitError, match=code):
        admission.verify_ai_panorama_install_permit(
            permit_case.permit_relpath,
            expected,
        )


def test_fixed_trust_and_compose_context_drift_fail_closed(
    permit_case: _Case,
) -> None:
    dry_run = admission.verify_ai_panorama_install_permit(
        permit_case.permit_relpath,
        permit_case.expected,
    )
    trust = json.loads(
        permit_case.trust_assertion_path.read_text(encoding="utf-8")
    )
    trust["actor_principal_id"] = "different-release-controller"
    _replace_json(
        permit_case.trust_assertion_path,
        trust,
        mode=admission.RUNTIME_PROFILE_MODE,
    )
    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_trusted_context_mismatch",
    ):
        admission.revalidate_ai_panorama_install_admission(
            dry_run,
            require_consumed=False,
        )

    _replace_json(
        permit_case.trust_assertion_path,
        _trust_assertion(permit_case.expected),
        mode=admission.RUNTIME_PROFILE_MODE,
    )
    compose = json.loads(
        permit_case.compose_plan_path.read_text(encoding="utf-8")
    )
    compose["web_mount_read_only"] = False
    _replace_json(
        permit_case.compose_plan_path,
        compose,
        mode=admission.RUNTIME_PROFILE_MODE,
    )
    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_compose_plan_invalid",
    ):
        admission.verify_ai_panorama_install_permit(
            permit_case.permit_relpath,
            permit_case.expected,
        )


def test_fully_resigned_old_dynamic_volume_profile_is_rejected(
    permit_case: _Case,
) -> None:
    old_volume_id = "propertyquarry-public-tours-production"
    old_volume_name = "property_propertyquarry_public_tours"
    old_target = "/data/public_property_tours"

    compose = json.loads(
        permit_case.compose_plan_path.read_text(encoding="utf-8")
    )
    compose.update(
        {
            "volume_id": old_volume_id,
            "docker_volume_name": old_volume_name,
            "container_mount_target": old_target,
        }
    )
    _replace_json(
        permit_case.compose_plan_path,
        compose,
        mode=admission.RUNTIME_PROFILE_MODE,
    )
    compose_sha256 = hashlib.sha256(
        permit_case.compose_plan_path.read_bytes()
    ).hexdigest()

    profile = json.loads(
        permit_case.volume_profile_path.read_text(encoding="utf-8")
    )
    profile.update(
        {
            "volume_id": old_volume_id,
            "logical_purpose": "public-tours",
            "application_setting": "EA_PUBLIC_TOUR_DIR",
            "application_setting_value": old_target,
            "docker_volume_name": old_volume_name,
            "container_mount_source": (
                "/synthetic/docker-root/volumes/"
                "property_propertyquarry_public_tours/_data"
            ),
            "container_mount_target": old_target,
            "compose_plan_sha256": compose_sha256,
        }
    )
    _replace_json(
        permit_case.volume_profile_path,
        profile,
        mode=admission.RUNTIME_PROFILE_MODE,
    )
    expected = replace(
        permit_case.expected,
        volume_id=old_volume_id,
        compose_plan_sha256=compose_sha256,
        volume_profile_sha256=hashlib.sha256(
            permit_case.volume_profile_path.read_bytes()
        ).hexdigest(),
    )
    _rewrite_trust_and_permit(permit_case, expected)

    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_release_context_invalid",
    ):
        admission.verify_ai_panorama_install_permit(
            permit_case.permit_relpath,
            expected,
        )


@pytest.mark.parametrize(
    "identity_field",
    ("artifact_root_device", "artifact_root_inode"),
)
def test_signed_root_device_and_inode_identity_fail_closed(
    permit_case: _Case,
    identity_field: str,
) -> None:
    profile = json.loads(
        permit_case.volume_profile_path.read_text(encoding="utf-8")
    )
    profile[identity_field] += 1
    _replace_json(
        permit_case.volume_profile_path,
        profile,
        mode=admission.RUNTIME_PROFILE_MODE,
    )
    profile_sha256 = hashlib.sha256(
        permit_case.volume_profile_path.read_bytes()
    ).hexdigest()
    expected = replace(
        permit_case.expected,
        **{
            identity_field: getattr(permit_case.expected, identity_field) + 1,
            "volume_profile_sha256": profile_sha256,
        },
    )
    _rewrite_trust_and_permit(permit_case, expected)
    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_volume_identity_mismatch",
    ):
        admission.verify_ai_panorama_install_permit(
            permit_case.permit_relpath,
            expected,
        )


def test_relative_artifact_walk_rejects_nested_device_or_mount(
    permit_case: _Case,
) -> None:
    artifact_mount_id = _mount_id(permit_case.artifact_root)
    root_descriptor = os.open(
        permit_case.artifact_root,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        for required_device, required_mount_id in (
            (
                permit_case.expected.artifact_root_device + 1,
                artifact_mount_id,
            ),
            (
                permit_case.expected.artifact_root_device,
                artifact_mount_id + 1,
            ),
        ):
            with pytest.raises(
                admission.AiPanoramaInstallPermitError,
                match="ai_panorama_source_bundle_invalid",
            ):
                admission._open_relative_directory(
                    root_descriptor,
                    permit_case.expected.artifact_relpath,
                    code="ai_panorama_source_bundle_invalid",
                    required_uid=os.geteuid(),
                    forbid_writable=True,
                    required_device=required_device,
                    required_mount_id=required_mount_id,
                )
    finally:
        os.close(root_descriptor)


def test_runtime_mount_ids_may_shift_between_preflight_and_apply(
    permit_case: _Case,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_mount_id = admission._descriptor_mount_id
    namespace_offset = {"value": 0}

    def shifted_mount_id(descriptor: int, *, code: str) -> int:
        resolved = Path(f"/proc/self/fd/{descriptor}").resolve()
        runtime_roots = (
            permit_case.artifact_root,
            permit_case.public_root,
        )
        shifted = any(
            resolved == root or root in resolved.parents
            for root in runtime_roots
        )
        return real_mount_id(descriptor, code=code) + (
            namespace_offset["value"] if shifted else 0
        )

    monkeypatch.setattr(
        admission,
        "_descriptor_mount_id",
        shifted_mount_id,
    )
    consumed = admission.consume_ai_panorama_install_permit(
        permit_case.permit_relpath,
        permit_case.expected,
    )
    namespace_offset["value"] = 10_000
    assert admission.validate_verified_admission(consumed) == consumed


def test_key_must_be_active_at_issuance_and_current_check(
    permit_case: _Case,
) -> None:
    keyring = json.loads(
        permit_case.keyring_path.read_text(encoding="utf-8")
    )
    keyring["keys"][0]["activates_at"] = (
        permit_case.now - timedelta(seconds=2)
    ).isoformat().replace("+00:00", "Z")
    expected = _rewrite_keyring_and_bind(permit_case, keyring)
    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_release_key_untrusted",
    ):
        admission.verify_ai_panorama_install_permit(
            permit_case.permit_relpath,
            expected,
        )


def test_keyring_attests_per_key_usage(
    permit_case: _Case,
) -> None:
    keyring = json.loads(
        permit_case.keyring_path.read_text(encoding="utf-8")
    )
    keyring["keys"][0]["usage"] = "propertyquarry.some-other-purpose"
    expected = _rewrite_keyring_and_bind(permit_case, keyring)
    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_release_keyring_invalid",
    ):
        admission.verify_ai_panorama_install_permit(
            permit_case.permit_relpath,
            expected,
        )


@pytest.mark.parametrize(
    "malformation",
    (
        "duplicate_epoch",
        "noncanonical_order",
        "rotation_not_reached",
        "revoked_before_activation",
    ),
)
def test_keyring_rotation_history_is_unambiguous(
    permit_case: _Case,
    malformation: str,
) -> None:
    keyring = json.loads(
        permit_case.keyring_path.read_text(encoding="utf-8")
    )
    first = keyring["keys"][0]
    if malformation == "duplicate_epoch":
        second = dict(first)
        second["key_id"] = "release-control-test-key-duplicate"
        keyring["keys"].append(second)
    elif malformation == "noncanonical_order":
        second = dict(first)
        second["key_id"] = "release-control-test-key-epoch-2"
        second["epoch"] = 2
        keyring["rotation_epoch"] = 2
        keyring["keys"] = [second, first]
    elif malformation == "rotation_not_reached":
        keyring["rotation_epoch"] = 2
        keyring["minimum_accepted_epoch"] = 2
    else:
        first["revoked_at"] = (
            permit_case.now - timedelta(days=2)
        ).isoformat().replace("+00:00", "Z")
    expected = _rewrite_keyring_and_bind(permit_case, keyring)
    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_release_keyring_invalid",
    ):
        admission.verify_ai_panorama_install_permit(
            permit_case.permit_relpath,
            expected,
        )


def test_key_must_remain_current_during_execution_lease(
    permit_case: _Case,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keyring = json.loads(
        permit_case.keyring_path.read_text(encoding="utf-8")
    )
    keyring["keys"][0]["accept_until"] = (
        permit_case.now + timedelta(seconds=100)
    ).isoformat().replace("+00:00", "Z")
    expected = _rewrite_keyring_and_bind(permit_case, keyring)
    consumed = admission.consume_ai_panorama_install_permit(
        permit_case.permit_relpath,
        expected,
    )
    monkeypatch.setattr(
        admission,
        "_utc_now",
        lambda: permit_case.now + timedelta(seconds=101),
    )
    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_release_key_untrusted",
    ):
        admission.validate_verified_admission(consumed)


def test_consumed_permit_has_bounded_post_expiry_execution_lease(
    permit_case: _Case,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dry_run = admission.verify_ai_panorama_install_permit(
        permit_case.permit_relpath,
        permit_case.expected,
    )
    consumed = admission.consume_ai_panorama_install_permit(
        permit_case.permit_relpath,
        permit_case.expected,
    )

    monkeypatch.setattr(
        admission,
        "_utc_now",
        lambda: permit_case.now + timedelta(seconds=200),
    )
    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_permit_not_fresh",
    ):
        admission.revalidate_ai_panorama_install_admission(
            dry_run,
            require_consumed=False,
        )
    assert admission.validate_verified_admission(consumed) == consumed

    monkeypatch.setattr(
        admission,
        "_utc_now",
        lambda: permit_case.now + timedelta(seconds=601),
    )
    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_consumed_execution_lease_expired",
    ):
        admission.validate_verified_admission(consumed)
    recovery = admission.revalidate_ai_panorama_install_recovery(consumed)
    assert type(recovery) is admission.VerifiedAiPanoramaInstallRecoveryEvidence
    assert recovery.permit_sha256 == consumed.permit_sha256
    assert recovery.ledger_entry_sha256 == consumed._ledger_entry_sha256
    assert not admission.is_verified_ai_panorama_install_admission(
        recovery,
        require_consumed=True,
    )

    monkeypatch.setattr(
        admission,
        "_utc_now",
        lambda: permit_case.now
        + timedelta(seconds=admission.MAX_CONSUMPTION_RECOVERY_SECONDS),
    )
    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_consumption_recovery_expired",
    ):
        admission.revalidate_ai_panorama_install_recovery(consumed)


def test_missing_tombstone_invalidates_consumed_admission(
    permit_case: _Case,
) -> None:
    consumed = admission.consume_ai_panorama_install_permit(
        permit_case.permit_relpath,
        permit_case.expected,
    )
    tombstones = sorted(permit_case.tombstone_root.iterdir())
    assert len(tombstones) == 3
    tombstones[0].unlink()
    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_tombstone_invalid",
    ):
        admission.validate_verified_admission(consumed)


def test_recovery_reconstructs_evidence_after_process_loss(
    permit_case: _Case,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_consumption_record_missing",
    ):
        admission.recover_ai_panorama_install_consumption(
            permit_case.permit_relpath,
            permit_case.expected,
        )

    consumed = admission.consume_ai_panorama_install_permit(
        permit_case.permit_relpath,
        permit_case.expected,
    )
    expected_evidence = {
        "permit_sha256": consumed.permit_sha256,
        "request_id_sha256": hashlib.sha256(
            consumed.request_id.encode("ascii")
        ).hexdigest(),
        "nonce_sha256": hashlib.sha256(
            consumed.nonce.encode("ascii")
        ).hexdigest(),
        "context_sha256": consumed._context_sha256,
        "ledger_instance_id": consumed._ledger_instance_id,
        "ledger_sequence": consumed._ledger_sequence,
        "ledger_entry_sha256": consumed._ledger_entry_sha256,
    }
    del consumed
    monkeypatch.setattr(
        admission,
        "_utc_now",
        lambda: permit_case.now + timedelta(seconds=601),
    )
    evidence = admission.recover_ai_panorama_install_consumption(
        permit_case.permit_relpath,
        permit_case.expected,
    )
    for field, value in expected_evidence.items():
        assert getattr(evidence, field) == value
    assert not admission.is_verified_ai_panorama_install_admission(
        evidence,
        require_consumed=True,
    )


def test_historical_consumption_uses_exact_archived_old_keyring_after_rotation(
    permit_case: _Case,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _provision_operation_journal(permit_case)
    old_keyring = json.loads(
        permit_case.keyring_path.read_text(encoding="utf-8")
    )
    old_keyring["keys"][0]["accept_until"] = (
        permit_case.now + timedelta(seconds=100)
    ).isoformat().replace("+00:00", "Z")
    expected = _rewrite_keyring_and_bind(permit_case, old_keyring)
    consumed = admission.consume_ai_panorama_install_permit(
        permit_case.permit_relpath,
        expected,
    )
    archived_keyring_bytes = permit_case.keyring_path.read_bytes()
    journal_before = _journal_snapshot(permit_case)
    state_before = {
        path.name: path.read_bytes()
        for path in (
            permit_case.ledger_path,
            *sorted(permit_case.tombstone_root.iterdir()),
        )
    }
    monkeypatch.setattr(
        admission,
        "_utc_now",
        lambda: permit_case.now + timedelta(days=30),
    )

    rotated_root = tmp_path / "rotated-keyring"
    rotated_root.mkdir()
    rotated_private_key = Ed25519PrivateKey.generate()
    rotated_path, _key_id, _key_sha256, _keyring_sha256 = (
        _write_panorama_keyring(
            rotated_root,
            private_key=rotated_private_key,
            now=permit_case.now + timedelta(days=1),
        )
    )
    rotated_bytes = rotated_path.read_bytes()
    assert rotated_bytes != archived_keyring_bytes
    _replace_json(
        permit_case.keyring_path,
        json.loads(rotated_bytes.decode("utf-8")),
        mode=admission.EXTERNAL_KEYRING_MODE,
    )
    with pytest.raises(admission.AiPanoramaInstallPermitError):
        admission.load_ai_panorama_install_historical_consumption(
            permit_case.permit_relpath,
            expected,
        )
    assert _journal_snapshot(permit_case) == journal_before

    _replace_json(
        permit_case.keyring_path,
        json.loads(archived_keyring_bytes.decode("utf-8")),
        mode=admission.EXTERNAL_KEYRING_MODE,
    )
    proof = admission.load_ai_panorama_install_historical_consumption(
        permit_case.permit_relpath,
        expected,
    )
    assert (
        type(proof)
        is admission.VerifiedAiPanoramaHistoricalConsumptionProof
    )
    assert proof.permit_sha256 == consumed.permit_sha256
    assert proof.ledger_entry_sha256 == consumed._ledger_entry_sha256
    assert _journal_snapshot(permit_case) == journal_before
    assert {
        path.name: path.read_bytes()
        for path in (
            permit_case.ledger_path,
            *sorted(permit_case.tombstone_root.iterdir()),
        )
    } == state_before


@pytest.mark.parametrize(
    "mutation",
    (
        "missing",
        "symlink",
        "wrong-mode",
        "wrong-digest",
        "wrong-key-id",
        "wrong-key-epoch",
        "wrong-key-sha256",
        "wrong-signature",
    ),
)
def test_historical_consumption_rejects_wrong_archived_trust_material(
    permit_case: _Case,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    _provision_operation_journal(permit_case)
    admission.consume_ai_panorama_install_permit(
        permit_case.permit_relpath,
        permit_case.expected,
    )
    monkeypatch.setattr(
        admission,
        "_utc_now",
        lambda: permit_case.now + timedelta(days=30),
    )
    journal_before = _journal_snapshot(permit_case)
    ledger_before = permit_case.ledger_path.read_bytes()
    tombstones_before = {
        path.name: path.read_bytes()
        for path in permit_case.tombstone_root.iterdir()
    }
    if mutation == "missing":
        permit_case.keyring_path.unlink()
    elif mutation == "symlink":
        retained = permit_case.keyring_path.with_name(".archived-keyring")
        permit_case.keyring_path.rename(retained)
        permit_case.keyring_path.symlink_to(retained.name)
    elif mutation == "wrong-mode":
        permit_case.keyring_path.chmod(0o600)
    elif mutation == "wrong-signature":
        envelope = json.loads(
            permit_case.permit_path.read_text(encoding="utf-8")
        )
        signature = str(envelope["signature"]["value"])
        envelope["signature"]["value"] = (
            ("A" if signature[0] != "A" else "B") + signature[1:]
        )
        _replace_json(
            permit_case.permit_path,
            envelope,
            mode=admission.CONTROLLER_FILE_MODE,
        )
    else:
        keyring = json.loads(
            permit_case.keyring_path.read_text(encoding="utf-8")
        )
        if mutation == "wrong-digest":
            keyring["status"] = "retired"
        elif mutation == "wrong-key-id":
            keyring["keys"][0]["key_id"] = "wrong-archive-key"
        elif mutation == "wrong-key-epoch":
            keyring["keys"][0]["epoch"] = 2
        else:
            keyring["keys"][0]["public_key_sha256"] = "f" * 64
        _replace_json(
            permit_case.keyring_path,
            keyring,
            mode=admission.EXTERNAL_KEYRING_MODE,
        )

    with pytest.raises(admission.AiPanoramaInstallPermitError):
        admission.load_ai_panorama_install_historical_consumption(
            permit_case.permit_relpath,
            permit_case.expected,
        )
    assert _journal_snapshot(permit_case) == journal_before
    assert permit_case.ledger_path.read_bytes() == ledger_before
    assert {
        path.name: path.read_bytes()
        for path in permit_case.tombstone_root.iterdir()
    } == tombstones_before


@pytest.mark.parametrize(
    "mutation",
    (
        "byte-identical-replacement",
        "hardlink",
        "symlink",
        "wrong-mode",
        "mount-id",
        "timestamp",
        "chown",
    ),
)
def test_historical_consumption_rejects_changed_retained_permit_identity(
    permit_case: _Case,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    _provision_operation_journal(permit_case)
    consumed = admission.consume_ai_panorama_install_permit(
        permit_case.permit_relpath,
        permit_case.expected,
    )
    monkeypatch.setattr(
        admission,
        "_utc_now",
        lambda: permit_case.now + timedelta(days=30),
    )
    reopened = admission.load_ai_panorama_install_historical_consumption(
        permit_case.permit_relpath,
        permit_case.expected,
    )
    assert reopened.permit_sha256 == consumed.permit_sha256
    journal_before = _journal_snapshot(permit_case)
    ledger_before = permit_case.ledger_path.read_bytes()
    tombstones_before = {
        path.name: path.read_bytes()
        for path in permit_case.tombstone_root.iterdir()
    }
    before = permit_case.permit_path.stat(follow_symlinks=False)
    if mutation == "byte-identical-replacement":
        replacement = permit_case.permit_path.with_name(".replacement")
        replacement.write_bytes(permit_case.permit_path.read_bytes())
        replacement.chmod(admission.CONTROLLER_FILE_MODE)
        os.replace(replacement, permit_case.permit_path)
        assert (
            permit_case.permit_path.stat(follow_symlinks=False).st_ino
            != before.st_ino
        )
    elif mutation == "hardlink":
        os.link(
            permit_case.permit_path,
            permit_case.permit_path.with_name(".retained-hardlink"),
        )
    elif mutation == "symlink":
        retained = permit_case.permit_path.with_name(".retained-original")
        permit_case.permit_path.rename(retained)
        permit_case.permit_path.symlink_to(retained.name)
    elif mutation == "wrong-mode":
        permit_case.permit_path.chmod(0o400)
    elif mutation == "mount-id":
        real_mount_id = admission._descriptor_mount_id

        def _shifted_permit_mount(
            descriptor: int,
            *,
            code: str,
        ) -> int:
            resolved = Path(f"/proc/self/fd/{descriptor}").resolve()
            shifted = (
                resolved == permit_case.permit_path.parent
                or permit_case.permit_path.parent in resolved.parents
            )
            return real_mount_id(descriptor, code=code) + (
                10_000 if shifted else 0
            )

        monkeypatch.setattr(
            admission,
            "_descriptor_mount_id",
            _shifted_permit_mount,
        )
    elif mutation == "timestamp":
        os.utime(
            permit_case.permit_path,
            ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000),
        )
    else:
        os.chown(
            permit_case.permit_path,
            os.geteuid(),
            os.getegid(),
        )
        assert (
            permit_case.permit_path.stat(follow_symlinks=False).st_ctime_ns
            != before.st_ctime_ns
        )

    with pytest.raises(admission.AiPanoramaInstallPermitError):
        admission.load_ai_panorama_install_historical_consumption(
            permit_case.permit_relpath,
            permit_case.expected,
        )
    assert _journal_snapshot(permit_case) == journal_before
    assert permit_case.ledger_path.read_bytes() == ledger_before
    assert {
        path.name: path.read_bytes()
        for path in permit_case.tombstone_root.iterdir()
    } == tombstones_before


def test_historical_terminal_append_rejects_forged_observation_and_evidence(
    permit_case: _Case,
) -> None:
    _provision_operation_journal(permit_case)
    consumed = admission.consume_ai_panorama_install_permit(
        permit_case.permit_relpath,
        permit_case.expected,
    )
    operation_journal.begin_ai_panorama_install_operation(
        consumed,
        evidence=_prepared_release_evidence(permit_case.expected),
    )
    observed = operation_journal.load_historical_ai_panorama_install_operation(
        permit_case.permit_relpath,
        permit_case.expected,
    )
    clean_evidence = _historical_terminal_evidence(
        observed,
        permit_case.expected,
        event="failed-clean",
        classification="failed-clean",
        basis="prepared-before-binding-and-baseline-target",
        observed_database_record_sha256=(
            permit_case.expected.expected_publication_record_sha256
        ),
    )
    forged_prepared = dict(observed.prepared_evidence)
    forged_plan = dict(
        forged_prepared["publication_binding_preparation"]  # type: ignore[arg-type]
    )
    forged_plan["publication_binding_expected_after_sha256"] = "9" * 64
    forged_prepared["publication_binding_preparation"] = forged_plan
    forged = replace(observed, prepared_evidence=forged_prepared)
    journal_before = _journal_snapshot(permit_case)

    with pytest.raises(
        operation_journal.AiPanoramaOperationJournalError,
        match="ai_panorama_operation_historical_state_invalid",
    ):
        operation_journal._finish_historical_ai_panorama_install_operation(
            permit_case.permit_relpath,
            permit_case.expected,
            forged,
            event="failed-clean",
            evidence=clean_evidence,
        )
    assert _journal_snapshot(permit_case) == journal_before

    class _PreparedEvidenceSubclass(dict[str, object]):
        pass

    forged_subclass = replace(
        observed,
        prepared_evidence=_PreparedEvidenceSubclass(
            observed.prepared_evidence
        ),
    )
    with pytest.raises(
        operation_journal.AiPanoramaOperationJournalError,
        match="ai_panorama_operation_historical_state_invalid",
    ):
        operation_journal._finish_historical_ai_panorama_install_operation(
            permit_case.permit_relpath,
            permit_case.expected,
            forged_subclass,
            event="failed-clean",
            evidence=clean_evidence,
        )
    assert _journal_snapshot(permit_case) == journal_before

    invalid_evidence = dict(clean_evidence)
    invalid_evidence["unexpected"] = True
    with pytest.raises(
        operation_journal.AiPanoramaOperationJournalError,
        match="ai_panorama_operation_evidence_invalid",
    ):
        operation_journal._finish_historical_ai_panorama_install_operation(
            permit_case.permit_relpath,
            permit_case.expected,
            observed,
            event="failed-clean",
            evidence=invalid_evidence,
        )
    assert _journal_snapshot(permit_case) == journal_before

    invalid_nested = json.loads(json.dumps(clean_evidence))
    invalid_nested["observed_target_identity"]["unexpected"] = True
    with pytest.raises(
        operation_journal.AiPanoramaOperationJournalError,
        match="ai_panorama_operation_evidence_invalid",
    ):
        operation_journal._finish_historical_ai_panorama_install_operation(
            permit_case.permit_relpath,
            permit_case.expected,
            observed,
            event="failed-clean",
            evidence=invalid_nested,
        )
    assert _journal_snapshot(permit_case) == journal_before

    false_contradiction = dict(clean_evidence)
    false_contradiction.update(
        {
            "phase": "recovery-required",
            "classification": "recovery-required",
            "classification_basis": "prepared-observation-contradiction",
        }
    )
    with pytest.raises(
        operation_journal.AiPanoramaOperationJournalError,
        match="ai_panorama_operation_evidence_invalid",
    ):
        operation_journal._finish_historical_ai_panorama_install_operation(
            permit_case.permit_relpath,
            permit_case.expected,
            observed,
            event="recovery-required",
            evidence=false_contradiction,
        )
    assert _journal_snapshot(permit_case) == journal_before


def test_historical_reload_rejects_unknown_standard_terminal_schema(
    permit_case: _Case,
) -> None:
    _provision_operation_journal(permit_case)
    consumed = admission.consume_ai_panorama_install_permit(
        permit_case.permit_relpath,
        permit_case.expected,
    )
    handle = operation_journal.begin_ai_panorama_install_operation(
        consumed,
        evidence=_prepared_release_evidence(permit_case.expected),
    )
    operation_journal.finish_ai_panorama_install_operation(
        handle,
        event="failed-clean",
        evidence={
            "contract": "unknown-terminal-contract.v1",
            "phase": "failed-clean",
        },
    )
    journal_before = _journal_snapshot(permit_case)

    with pytest.raises(
        operation_journal.AiPanoramaOperationJournalError,
        match="ai_panorama_operation_evidence_invalid",
    ):
        operation_journal.load_historical_ai_panorama_install_operation(
            permit_case.permit_relpath,
            permit_case.expected,
        )
    assert _journal_snapshot(permit_case) == journal_before


def test_historical_reload_accepts_exact_standard_live_terminal_schema(
    permit_case: _Case,
) -> None:
    _provision_operation_journal(permit_case)
    consumed = admission.consume_ai_panorama_install_permit(
        permit_case.permit_relpath,
        permit_case.expected,
    )
    prepared = _prepared_release_evidence(permit_case.expected)
    handle = operation_journal.begin_ai_panorama_install_operation(
        consumed,
        evidence=prepared,
    )
    terminal = {
        key: value
        for key, value in prepared.items()
        if key != "publication_binding_preparation"
    }
    terminal.update(
        {
            "phase": "failed-clean",
            "error_code": "ai_panorama_install_failed",
            "publication_outcome": "uncommitted",
        }
    )
    operation_journal.finish_ai_panorama_install_operation(
        handle,
        event="failed-clean",
        evidence=terminal,
    )

    reloaded = (
        operation_journal.load_historical_ai_panorama_install_operation(
            permit_case.permit_relpath,
            permit_case.expected,
        )
    )
    assert reloaded.state == "terminal"
    assert reloaded.terminal_event == "failed-clean"
    assert reloaded.terminal_evidence == terminal


def test_consumed_without_prepared_operation_gets_one_truthful_terminal(
    permit_case: _Case,
) -> None:
    _provision_operation_journal(permit_case)
    admission.consume_ai_panorama_install_permit(
        permit_case.permit_relpath,
        permit_case.expected,
    )
    observed = operation_journal.load_historical_ai_panorama_install_operation(
        permit_case.permit_relpath,
        permit_case.expected,
    )
    assert observed.state == "consumed-no-operation"
    evidence = _historical_terminal_evidence(
        observed,
        permit_case.expected,
        event="consumed-failed-clean",
        classification="failed-clean",
        basis="consumed-before-operation-preparation",
        observed_database_record_sha256=(
            permit_case.expected.expected_publication_record_sha256
        ),
    )
    terminal = (
        operation_journal._record_consumed_without_operation_failed_clean(
            permit_case.permit_relpath,
            permit_case.expected,
            observed,
            evidence=evidence,
        )
    )
    assert len(terminal) == 64
    assert (
        operation_journal._record_consumed_without_operation_failed_clean(
            permit_case.permit_relpath,
            permit_case.expected,
            observed,
            evidence=evidence,
        )
        == terminal
    )
    reloaded = (
        operation_journal.load_historical_ai_panorama_install_operation(
            permit_case.permit_relpath,
            permit_case.expected,
        )
    )
    assert reloaded.state == "terminal"
    assert reloaded.terminal_event == "consumed-failed-clean"


def test_independent_golden_signature_vector() -> None:
    fixture_path = (
        Path(__file__).with_name("fixtures")
        / "property_tour_ai_panorama_admission_v2_golden.json"
    )
    vector = json.loads(fixture_path.read_text(encoding="utf-8"))
    envelope = vector["envelope"]
    assert (
        envelope["permit"]["subject"]
        == admission.CANONICAL_SUBJECT
        == "repo:ArchonMegalon@11421547/propertyquarry@1257593732:"
        "environment:propertyquarry-production"
    )
    assert (
        envelope["permit"]["workflow_ref"]
        == admission.CANONICAL_WORKFLOW_REF
        == "ArchonMegalon/propertyquarry/.github/workflows/"
        "smoke-runtime.yml@refs/heads/main"
    )
    assert (
        envelope["permit"]["volume_id"]
        == admission.CANONICAL_PUBLIC_TOUR_VOLUME_ID
        == "propertyquarry-governed-public-tours-production"
    )
    preimage = admission._signature_preimage(envelope)
    assert hashlib.sha256(preimage).hexdigest() == vector["preimage_sha256"]
    public_key = base64.urlsafe_b64decode(
        vector["public_key_base64url"]
        + "=" * ((4 - len(vector["public_key_base64url"]) % 4) % 4)
    )
    signature = admission._decode_signature(
        envelope["signature"]["value"]
    )
    Ed25519PublicKey.from_public_bytes(public_key).verify(
        signature,
        preimage,
    )
