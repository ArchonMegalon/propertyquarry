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
        mode=0o444,
    )
    _replace_json(
        case.permit_path,
        _permit_envelope(
            expected,
            private_key=case.private_key,
            now=case.now,
        ),
        mode=0o400,
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
    volume_id = "propertyquarry-public-tours-production"
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
    _write_json(compose_plan_path, compose_plan, mode=0o444)
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
        "logical_purpose": "public-tours",
        "application_setting": admission.CANONICAL_PUBLIC_TOUR_SETTING,
        "application_setting_value": (
            admission.CANONICAL_PUBLIC_TOUR_MOUNT_TARGET
        ),
        "storage_kind": admission.CANONICAL_PUBLIC_TOUR_STORAGE_KIND,
        "docker_volume_name": admission.CANONICAL_PUBLIC_TOUR_VOLUME_NAME,
        "container_mount_source": str(public_root),
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
    _write_json(volume_profile_path, volume_profile, mode=0o444)
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
    _write_json(trust_assertion_path, trust_assertion, mode=0o444)

    permit_relpath = "prater-ai-panorama-install.json"
    permit_path = permit_root / permit_relpath
    _write_json(
        permit_path,
        _permit_envelope(
            expected,
            private_key=private_key,
            now=now,
        ),
        mode=0o400,
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
        == "property_propertyquarry_public_tours"
    )
    assert dry_run.public_tour_mount_target == "/data/public_property_tours"
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
    _write_json(permit_case.permit_path, envelope, mode=0o400)
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
        mode=0o400,
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
        mode=0o400,
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
        consumed.request_id,
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
    _replace_json(permit_case.trust_assertion_path, trust, mode=0o444)
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
        mode=0o444,
    )
    compose = json.loads(
        permit_case.compose_plan_path.read_text(encoding="utf-8")
    )
    compose["web_mount_read_only"] = False
    _replace_json(permit_case.compose_plan_path, compose, mode=0o444)
    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_compose_plan_invalid",
    ):
        admission.verify_ai_panorama_install_permit(
            permit_case.permit_relpath,
            permit_case.expected,
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
    _replace_json(permit_case.volume_profile_path, profile, mode=0o444)
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
        return (
            real_mount_id(descriptor, code=code)
            + namespace_offset["value"]
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
