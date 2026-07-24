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
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.product import property_tour_ai_panorama_admission as admission
from scripts import propertyquarry_deploy_drain_keyring as release_keyring


@dataclass(frozen=True)
class _Case:
    expected: admission.AiPanoramaInstallExpectedBindings
    permit_relpath: str
    permit_path: Path
    ledger_path: Path
    control_root: Path
    public_root: Path
    artifact_root: Path
    private_key: Ed25519PrivateKey
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


def _empty_ledger(instance_id: str = "a" * 32) -> dict[str, object]:
    return {
        "schema": admission.LEDGER_SCHEMA,
        "authority": admission.LEDGER_AUTHORITY,
        "instance_id": instance_id,
        "sequence": 0,
        "tip_sha256": "0" * 64,
        "entries": [],
    }


def _configure_release_keyring(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    private_key: Ed25519PrivateKey,
    now: datetime,
) -> None:
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    public_text = base64.urlsafe_b64encode(public_bytes).decode("ascii").rstrip("=")
    keyring = {
        "schema": release_keyring.SCHEMA,
        "authority": release_keyring.AUTHORITY,
        "algorithm": "Ed25519",
        "status": "active",
        "rotation_epoch": 1,
        "minimum_accepted_epoch": 1,
        "keys": [
            {
                "key_id": "release-control-test-key",
                "epoch": 1,
                "public_key": public_text,
                "public_key_sha256": hashlib.sha256(public_bytes).hexdigest(),
                "activates_at": (
                    now - timedelta(days=1)
                ).isoformat().replace("+00:00", "Z"),
                "accept_until": None,
                "revoked_at": None,
            }
        ],
    }
    trust_root = tmp_path / "release-keyring"
    trust_root.mkdir(mode=0o700)
    tracked = trust_root / "tracked.json"
    external_parent = trust_root / "external"
    external_parent.mkdir(mode=0o700)
    external = external_parent / "keyring.json"
    _write_json(tracked, keyring, mode=0o444)
    _write_json(external, keyring, mode=0o444)

    monkeypatch.setattr(release_keyring, "TRACKED_KEYRING_PATH", tracked)
    monkeypatch.setattr(release_keyring, "EXTERNAL_KEYRING_PATH", external)
    monkeypatch.setattr(release_keyring, "KEYRING_STATUS", "active")
    monkeypatch.setattr(
        release_keyring,
        "KEYRING_MANIFEST_SHA256",
        hashlib.sha256(release_keyring._canonical_bytes(keyring)).hexdigest(),
    )
    monkeypatch.setattr(release_keyring, "KEYRING_ROTATION_EPOCH", 1)
    monkeypatch.setattr(release_keyring, "KEYRING_MINIMUM_ACCEPTED_EPOCH", 1)
    monkeypatch.setattr(release_keyring, "REQUIRED_UID", os.geteuid())
    monkeypatch.setattr(release_keyring, "REQUIRED_MODE", 0o444)
    monkeypatch.setattr(release_keyring, "SECURE_PATH_ROOT", trust_root)


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


@pytest.fixture
def permit_case(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> _Case:
    now = datetime(2026, 7, 24, 8, 0, 0, tzinfo=timezone.utc)
    private_key = Ed25519PrivateKey.generate()
    _configure_release_keyring(
        monkeypatch,
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

    control_root = tmp_path / "controller"
    permit_root = control_root / "permits"
    artifact_root = tmp_path / "artifact-root"
    public_root = tmp_path / "docker-volume-mountpoint"
    for path in (control_root, permit_root, artifact_root, public_root):
        path.mkdir(mode=0o700)

    ledger_path = control_root / "consumption-ledger.v1.json"
    lock_path = control_root / "consumption-ledger.v1.lock"
    _write_json(ledger_path, _empty_ledger(), mode=0o600)
    lock_path.write_bytes(b"lock\n")
    lock_path.chmod(0o600)
    volume_profile_path = tmp_path / "public-tour-volume-profile.v1.json"

    paths = admission._ControllerPaths(
        control_root=control_root,
        permit_root=permit_root,
        ledger_path=ledger_path,
        ledger_lock_path=lock_path,
        volume_profile_path=volume_profile_path,
        required_uid=os.geteuid(),
    )
    monkeypatch.setattr(admission, "_CONTROLLER_PATHS", paths)

    slug = "prater-messe-maisonette-ai-360-053ad185e1c44b2e"
    artifact_relpath = (
        "incoming_property_tours/prater-053ad185e1c44b2e/"
        f"ai-panorama-v2-yaw65-final/{slug}"
    )
    source_bundle = artifact_root / artifact_relpath
    source_bundle.mkdir(parents=True, mode=0o700)
    receipt_relpath = (
        "runtime/propertyquarry_source_reconcile.private/"
        "incoming-canonical-before/prater-053ad185e1c44b2e/"
        "ai-panorama-v2-yaw65-final.receipt.json"
    )
    receipt_path = artifact_root / receipt_relpath
    receipt_bytes = _canonical(
        {
            "contract": "propertyquarry.ai_panorama_materialization_receipt.v1",
            "status": "pass",
            "slug": slug,
        }
    ) + b"\n"
    receipt_path.parent.mkdir(parents=True, mode=0o700)
    receipt_path.write_bytes(receipt_bytes)
    receipt_path.chmod(0o400)

    expected = admission.AiPanoramaInstallExpectedBindings(
        subject="repo:ArchonMegalon/propertyquarry:environment:production",
        authenticated_principal_id="propertyquarry-release-controller",
        search_run_id="98bed75e984549c6bd4371d602662ab8",
        candidate_ref="053ad185e1c44b2e",
        external_id="053ad185e1c44b2e",
        listing_url="https://propertyquarry.com/app/research/053ad185e1c44b2e",
        source_ref="https://www.willhaben.at/iad/immobilien/d/123456",
        provider_key="willhaben",
        expected_slug=slug,
        expected_source_tree_sha256="1" * 64,
        expected_tour_sha256="2" * 64,
        expected_core_manifest_sha256="3" * 64,
        expected_materialization_receipt_sha256=hashlib.sha256(
            receipt_bytes
        ).hexdigest(),
        expected_candidate_marker_sha256="4" * 64,
        expected_publication_record_sha256="5" * 64,
        artifact_relpath=artifact_relpath,
        materialization_receipt_relpath=receipt_relpath,
        request_id="request-053ad185e1c44b2e",
        repository="ArchonMegalon/propertyquarry",
        git_ref="refs/heads/main",
        git_head_sha="d" * 40,
        workflow_ref=(
            "ArchonMegalon/propertyquarry/.github/workflows/"
            "propertyquarry-release-v2.yml@refs/heads/main"
        ),
        job="propertyquarry-release-v2",
        environment="propertyquarry-production",
        review_receipt_sha256="6" * 64,
    )

    artifact_details = artifact_root.stat(follow_symlinks=False)
    public_details = public_root.stat(follow_symlinks=False)
    volume_profile = {
        "schema": admission.VOLUME_PROFILE_SCHEMA,
        "version": 1,
        "authority": admission.LEDGER_AUTHORITY,
        "status": "active",
        "environment": expected.environment,
        "volume_id": "propertyquarry-public-tours-production",
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
        "public_tour_root": str(public_root),
        "public_tour_root_device": public_details.st_dev,
        "public_tour_root_inode": public_details.st_ino,
    }
    _write_json(volume_profile_path, volume_profile, mode=0o444)

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
        control_root=control_root,
        public_root=public_root,
        artifact_root=artifact_root,
        private_key=private_key,
        now=now,
    )


def test_verified_admission_is_exact_fixed_root_and_revalidated(
    permit_case: _Case,
) -> None:
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
    assert dry_run.public_tour_dir == permit_case.public_root
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
        match="ai_panorama_permit_binding_mismatch",
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
            ledger_path=alternate / "consumption-ledger.v1.json",
            ledger_lock_path=alternate / "consumption-ledger.v1.lock",
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
    release_keyring.EXTERNAL_KEYRING_PATH.unlink()
    with pytest.raises(
        admission.AiPanoramaInstallPermitError,
        match="ai_panorama_release_key_untrusted",
    ):
        admission.verify_ai_panorama_install_permit(
            permit_case.permit_relpath,
            permit_case.expected,
        )
