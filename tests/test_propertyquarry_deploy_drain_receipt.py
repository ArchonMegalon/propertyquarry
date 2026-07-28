from __future__ import annotations

import base64
import json
import multiprocessing
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts import propertyquarry_deploy_drain_receipt as drain_receipt
from scripts.propertyquarry_deploy_drain_receipt import (
    ACTION,
    ISSUER,
    SCHEMA,
    DrainReceiptBindings,
    DrainReceiptError,
    build_parser,
    consume_drain_receipt,
    verify_drain_receipt,
)


RELEASE_SHA = "a" * 40
IMAGE_DIGEST = "sha256:" + "b" * 64


def _b64url(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _canonical(payload: Any) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


@pytest.fixture
def bindings() -> DrainReceiptBindings:
    return DrainReceiptBindings(
        release_sha=RELEASE_SHA,
        image_digest=IMAGE_DIGEST,
        deployment_id="deploy-20260714-01",
        target_id="propertyquarry-prod-primary",
        compose_project="property",
        public_origin="https://propertyquarry.com",
        api_container="propertyquarry-api",
        worker_container="propertyquarry-worker",
        scheduler_container="propertyquarry-scheduler",
        render_container="propertyquarry-render-tools",
        ingress_container="propertyquarry-cloudflared",
        writer_topology_sha256="d" * 64,
        actor_id="release-operator@example.com",
    )


@pytest.fixture
def signing_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Ed25519PrivateKey:
    key = Ed25519PrivateKey.generate()
    public_raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    public_key = _b64url(public_raw)
    public_key_sha256 = drain_receipt._sha256_bytes(public_raw)
    key_id = "release-control-test-rotation-7"
    anchor = {
        "schema": drain_receipt.TRUST_ANCHOR_SCHEMA,
        "authority": ISSUER,
        "algorithm": "Ed25519",
        "rotation_epoch": 7,
        "key_id": key_id,
        "public_key": public_key,
        "public_key_sha256": public_key_sha256,
        "status": "active",
    }
    anchor_path = tmp_path / "tracked" / "release-controlled-trust-anchor.json"
    external_anchor_path = tmp_path / "external" / "release-controlled-trust-anchor.json"
    anchor_path.parent.mkdir(mode=0o700)
    external_anchor_path.parent.mkdir(mode=0o700)
    anchor_path.write_text(json.dumps(anchor), encoding="utf-8")
    external_anchor_path.write_text(json.dumps(anchor), encoding="utf-8")
    external_anchor_path.chmod(0o444)
    monkeypatch.setattr(drain_receipt, "TRACKED_TRUST_ANCHOR_PATH", anchor_path)
    monkeypatch.setattr(drain_receipt, "EXTERNAL_TRUST_ANCHOR_PATH", external_anchor_path)
    monkeypatch.setattr(drain_receipt, "TRUST_ANCHOR_STATUS", "active")
    monkeypatch.setattr(drain_receipt, "TRUST_ANCHOR_ROTATION_EPOCH", 7)
    monkeypatch.setattr(drain_receipt, "TRUST_ANCHOR_KEY_ID", key_id)
    monkeypatch.setattr(drain_receipt, "TRUST_ANCHOR_PUBLIC_KEY_SHA256", public_key_sha256)
    monkeypatch.setattr(
        drain_receipt,
        "TRUST_ANCHOR_MANIFEST_SHA256",
        drain_receipt._sha256_bytes(_canonical(anchor)),
    )
    monkeypatch.setattr(drain_receipt, "TRUST_ANCHOR_REQUIRED_UID", external_anchor_path.stat().st_uid)
    monkeypatch.setattr(drain_receipt, "TRUST_ANCHOR_REQUIRED_MODE", 0o444)
    monkeypatch.setattr(
        drain_receipt,
        "CONSUMPTION_LEDGER_PATH",
        tmp_path / "private" / "consumed.json",
    )
    drain_receipt._atomic_write_json(
        drain_receipt.CONSUMPTION_LEDGER_PATH,
        drain_receipt._empty_ledger(),
    )
    for name in (*drain_receipt.LEGACY_CALLER_TRUST_ENV, *drain_receipt.FORBIDDEN_SIGNER_ENV):
        monkeypatch.delenv(name, raising=False)
    return key


def _write_receipt(
    path: Path,
    *,
    key: Ed25519PrivateKey,
    bindings: DrainReceiptBindings,
    now: datetime,
    mutate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "issuer": ISSUER,
        "key_id": drain_receipt.TRUST_ANCHOR_KEY_ID,
        "release": {
            "commit_sha": bindings.release_sha,
            "image_digest": bindings.image_digest,
        },
        "deployment_id": bindings.deployment_id,
        "target": bindings.target(),
        "actor_id": bindings.actor_id,
        "action": ACTION,
        "issued_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(minutes=4)).isoformat().replace("+00:00", "Z"),
        "nonce": "c" * 32,
    }
    if mutate:
        payload.update(mutate)
    payload["signature"] = _b64url(key.sign(_canonical(payload)))
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def test_signed_receipt_verifies_only_for_exact_release_target_actor_and_time(
    tmp_path: Path,
    bindings: DrainReceiptBindings,
    signing_key: Ed25519PrivateKey,
) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    receipt_path = tmp_path / "drain.json"
    _write_receipt(receipt_path, key=signing_key, bindings=bindings, now=now)

    verification = verify_drain_receipt(receipt_path, bindings, now=now + timedelta(seconds=10))

    assert verification["verified"] is True
    assert verification["release"] == {
        "commit_sha": RELEASE_SHA,
        "image_digest": IMAGE_DIGEST,
    }
    assert verification["target"] == bindings.target()
    assert verification["trust_anchor"]["rotation_epoch"] == 7
    assert "nonce" not in verification
    assert len(verification["nonce_sha256"]) == 64

    wrong_actor = DrainReceiptBindings(**{**bindings.__dict__, "actor_id": "different-operator"})
    with pytest.raises(DrainReceiptError, match="actor_id"):
        verify_drain_receipt(receipt_path, wrong_actor, now=now + timedelta(seconds=10))

    wrong_worker = DrainReceiptBindings(
        **{**bindings.__dict__, "worker_container": "propertyquarry-worker-other"}
    )
    with pytest.raises(DrainReceiptError, match="target binding"):
        verify_drain_receipt(receipt_path, wrong_worker, now=now + timedelta(seconds=10))


def test_database_fence_policy_tracks_every_long_lived_database_writer() -> None:
    root = Path(__file__).resolve().parents[1]
    topology = json.loads(
        (root / "config/release/propertyquarry_deploy_writer_topology.v1.json").read_text(
            encoding="utf-8"
        )
    )
    policy = json.loads(
        (root / "config/release/propertyquarry_database_fence_policy.v1.json").read_text(
            encoding="utf-8"
        )
    )
    runtime_writers = {
        role
        for role, contract in topology["target"].items()
        if contract["database_writer"] is True and role != "migration"
    }

    assert runtime_writers == {"api", "worker", "scheduler", "render"}
    assert {
        key.removesuffix("_runtime_epoch_prefix")
        for key in policy["roles"]
        if key.endswith("_runtime_epoch_prefix")
    } == runtime_writers
    assert set(policy["secrets"]["application_allowed_credentials"]) == {
        f"{role}_runtime_epoch" for role in runtime_writers
    }


def test_receipt_rejects_tampering_expiry_future_and_unsigned_fields(
    tmp_path: Path,
    bindings: DrainReceiptBindings,
    signing_key: Ed25519PrivateKey,
) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    receipt_path = tmp_path / "drain.json"
    payload = _write_receipt(receipt_path, key=signing_key, bindings=bindings, now=now)
    payload["deployment_id"] = "tampered-deployment"
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DrainReceiptError, match="deployment_id"):
        verify_drain_receipt(receipt_path, bindings, now=now)

    _write_receipt(receipt_path, key=signing_key, bindings=bindings, now=now - timedelta(minutes=10))
    with pytest.raises(DrainReceiptError, match="expired"):
        verify_drain_receipt(receipt_path, bindings, now=now)

    _write_receipt(receipt_path, key=signing_key, bindings=bindings, now=now + timedelta(minutes=2))
    with pytest.raises(DrainReceiptError, match="future"):
        verify_drain_receipt(receipt_path, bindings, now=now)

    _write_receipt(
        receipt_path,
        key=signing_key,
        bindings=bindings,
        now=now,
        mutate={"self_asserted": True},
    )
    with pytest.raises(DrainReceiptError, match="unexpected self_asserted"):
        verify_drain_receipt(receipt_path, bindings, now=now)


def test_caller_environment_cannot_replace_release_control_trust_anchor(
    tmp_path: Path,
    bindings: DrainReceiptBindings,
    signing_key: Ed25519PrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    attacker_key = Ed25519PrivateKey.generate()
    attacker_public = attacker_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    receipt_path = tmp_path / "attacker-drain.json"
    _write_receipt(receipt_path, key=attacker_key, bindings=bindings, now=now)
    monkeypatch.setenv("PROPERTYQUARRY_DRAIN_RECEIPT_KEY_ID", drain_receipt.TRUST_ANCHOR_KEY_ID)
    monkeypatch.setenv(
        "PROPERTYQUARRY_DRAIN_RECEIPT_ED25519_PUBLIC_KEY",
        _b64url(attacker_public),
    )

    with pytest.raises(DrainReceiptError, match="trust-anchor overrides are forbidden"):
        verify_drain_receipt(receipt_path, bindings, now=now)

    monkeypatch.delenv("PROPERTYQUARRY_DRAIN_RECEIPT_KEY_ID")
    monkeypatch.delenv("PROPERTYQUARRY_DRAIN_RECEIPT_ED25519_PUBLIC_KEY")
    with pytest.raises(DrainReceiptError, match="signature is invalid"):
        verify_drain_receipt(receipt_path, bindings, now=now)


def test_rotation_requires_manifest_and_compiled_pins_to_change_together(
    tmp_path: Path,
    bindings: DrainReceiptBindings,
    signing_key: Ed25519PrivateKey,
) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    receipt_path = tmp_path / "drain.json"
    _write_receipt(receipt_path, key=signing_key, bindings=bindings, now=now)
    anchor = json.loads(drain_receipt.TRACKED_TRUST_ANCHOR_PATH.read_text(encoding="utf-8"))
    anchor["rotation_epoch"] = 8
    drain_receipt.TRACKED_TRUST_ANCHOR_PATH.write_text(json.dumps(anchor), encoding="utf-8")

    with pytest.raises(DrainReceiptError, match="compiled manifest pin"):
        verify_drain_receipt(receipt_path, bindings, now=now)


def test_deploy_controlled_metadata_cannot_authorize_without_external_anchor(
    tmp_path: Path,
    bindings: DrainReceiptBindings,
    signing_key: Ed25519PrivateKey,
) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    receipt_path = tmp_path / "drain.json"
    _write_receipt(receipt_path, key=signing_key, bindings=bindings, now=now)
    drain_receipt.EXTERNAL_TRUST_ANCHOR_PATH.unlink()

    with pytest.raises(DrainReceiptError, match="external release-control.*unavailable"):
        verify_drain_receipt(receipt_path, bindings, now=now)


def test_repository_trust_metadata_is_explicitly_unconfigured_by_default() -> None:
    payload = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "config/release/propertyquarry_deploy_drain_trust_anchor.v1.json"
        ).read_text(encoding="utf-8")
    )

    assert payload["status"] == "UNCONFIGURED"
    assert payload["rotation_epoch"] == 0
    assert drain_receipt.CONSUMPTION_LEDGER_PATH.is_absolute()


def test_private_signer_material_is_rejected_from_deploy_runtime(
    tmp_path: Path,
    bindings: DrainReceiptBindings,
    signing_key: Ed25519PrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    receipt_path = tmp_path / "drain.json"
    _write_receipt(receipt_path, key=signing_key, bindings=bindings, now=now)
    monkeypatch.setenv("PROPERTYQUARRY_DRAIN_RECEIPT_SIGNING_SEED", "attacker-controlled")

    with pytest.raises(DrainReceiptError, match="private drain signer material is forbidden"):
        verify_drain_receipt(receipt_path, bindings, now=now)


def test_atomic_consumption_is_private_and_replay_fails_closed(
    tmp_path: Path,
    bindings: DrainReceiptBindings,
    signing_key: Ed25519PrivateKey,
) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    receipt_path = tmp_path / "drain.json"
    ledger_path = drain_receipt.CONSUMPTION_LEDGER_PATH
    _write_receipt(receipt_path, key=signing_key, bindings=bindings, now=now)

    result = consume_drain_receipt(receipt_path, bindings, now=now)

    assert result["single_use_consumed"] is True
    assert stat.S_IMODE(ledger_path.stat().st_mode) == 0o600
    assert stat.S_IMODE((ledger_path.with_name("consumed.json.lock")).stat().st_mode) == 0o600
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert len(ledger["entries"]) == 1
    assert "nonce" not in ledger["entries"][0]
    with pytest.raises(DrainReceiptError, match="already been consumed"):
        consume_drain_receipt(receipt_path, bindings, now=now)


def _concurrent_consume_worker(
    receipt_path: str,
    bindings: DrainReceiptBindings,
    now: datetime,
    start: Any,
    results: Any,
) -> None:
    start.wait()
    try:
        consume_drain_receipt(receipt_path, bindings, now=now)
    except DrainReceiptError as exc:
        results.put(("rejected", str(exc)))
    else:
        results.put(("consumed", ""))


def test_concurrent_consumption_allows_exactly_one_promotion_authority(
    tmp_path: Path,
    bindings: DrainReceiptBindings,
    signing_key: Ed25519PrivateKey,
) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    receipt_path = tmp_path / "drain.json"
    ledger_path = drain_receipt.CONSUMPTION_LEDGER_PATH
    _write_receipt(receipt_path, key=signing_key, bindings=bindings, now=now)
    context = multiprocessing.get_context("fork")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_concurrent_consume_worker,
            args=(str(receipt_path), bindings, now, start, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    outcomes = [results.get(timeout=15) for _ in processes]
    for process in processes:
        process.join(timeout=15)

    assert sorted(status for status, _ in outcomes) == ["consumed", "rejected"]
    assert any("already been consumed" in message for status, message in outcomes if status == "rejected")
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert len(ledger["entries"]) == 1


def test_same_nonce_cannot_be_reused_in_a_differently_signed_receipt(
    tmp_path: Path,
    bindings: DrainReceiptBindings,
    signing_key: Ed25519PrivateKey,
) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    _write_receipt(first, key=signing_key, bindings=bindings, now=now)
    _write_receipt(second, key=signing_key, bindings=bindings, now=now + timedelta(seconds=1))

    consume_drain_receipt(first, bindings, now=now + timedelta(seconds=2))
    with pytest.raises(DrainReceiptError, match="already been consumed"):
        consume_drain_receipt(second, bindings, now=now + timedelta(seconds=2))


def test_failed_atomic_ledger_replace_does_not_create_partial_consumption(
    tmp_path: Path,
    bindings: DrainReceiptBindings,
    signing_key: Ed25519PrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    receipt_path = tmp_path / "drain.json"
    _write_receipt(receipt_path, key=signing_key, bindings=bindings, now=now)
    real_atomic_write = drain_receipt._atomic_write_json

    def crash_before_replace(*args: Any, **kwargs: Any) -> None:
        raise OSError("simulated crash before atomic replace")

    monkeypatch.setattr(drain_receipt, "_atomic_write_json", crash_before_replace)
    with pytest.raises(OSError, match="simulated crash"):
        consume_drain_receipt(receipt_path, bindings, now=now)
    assert json.loads(
        drain_receipt.CONSUMPTION_LEDGER_PATH.read_text(encoding="utf-8")
    )["entries"] == []

    monkeypatch.setattr(drain_receipt, "_atomic_write_json", real_atomic_write)
    assert consume_drain_receipt(receipt_path, bindings, now=now)["single_use_consumed"] is True


def test_deleted_ledger_fails_closed_instead_of_resetting_single_use_state(
    tmp_path: Path,
    bindings: DrainReceiptBindings,
    signing_key: Ed25519PrivateKey,
) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    receipt_path = tmp_path / "drain.json"
    _write_receipt(receipt_path, key=signing_key, bindings=bindings, now=now)
    consume_drain_receipt(receipt_path, bindings, now=now)
    drain_receipt.CONSUMPTION_LEDGER_PATH.unlink()

    with pytest.raises(DrainReceiptError, match="refusing implicit reset"):
        consume_drain_receipt(receipt_path, bindings, now=now)


def test_cli_cannot_select_an_alternate_consumption_ledger() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["consume", "--ledger", "/tmp/attacker-ledger.json"])
