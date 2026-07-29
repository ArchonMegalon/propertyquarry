from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.propertyquarry_advanced_visual_gold_binding import (
    REQUIRED_SOURCE_RECEIPTS,
    build_advanced_visual_binding_receipt,
    verify_advanced_visual_binding_receipt,
)


NOW = datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc)
RELEASE_SHA = "1" * 40
IMAGE_DIGEST = "sha256:" + "2" * 64
VIDEO_SHA = "a" * 64


def _write_json(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_paths(tmp_path: Path) -> dict[str, Path]:
    generated_at = (NOW - timedelta(minutes=5)).isoformat()
    candidate_identity = {
        "release_commit_sha": RELEASE_SHA,
        "image_digest": IMAGE_DIGEST,
    }
    payloads: dict[str, dict[str, object]] = {
        "walkthrough_quality": {
            "contract_name": "propertyquarry.walkthrough_quality_gate.v1",
            **candidate_identity,
            "generated_at": generated_at,
            "status": "pass",
            "video_sha256": VIDEO_SHA,
        },
        "walkthrough_provider_proof": {
            "contract_name": "propertyquarry.walkthrough_provider_proof_gate.v1",
            **candidate_identity,
            "generated_at": generated_at,
            "status": "pass",
            "provider_results": [
                {
                    "provider": "magicfit",
                    "status": "pass",
                    "video_sha256": VIDEO_SHA,
                },
                {
                    "provider": "omagic",
                    "status": "pass",
                    "video_sha256": VIDEO_SHA,
                },
            ],
        },
        "scene_video_readiness": {
            "contract_name": "propertyquarry.scene_video_readiness.v1",
            **candidate_identity,
            "generated_at": generated_at,
            "summary": {"blocked_count": 0},
        },
        "scene_video_readiness_verifier": {
            "contract_name": "propertyquarry.scene_video_readiness_verifier.v1",
            **candidate_identity,
            "generated_at": generated_at,
            "status": "pass",
            "blockers": [],
        },
        "scene_video_runtime_status": {
            "contract_name": "propertyquarry.scene_video_runtime_status.v1",
            **candidate_identity,
            "generated_at": generated_at,
            "providers": [
                {
                    "provider": provider,
                    "status": "ready",
                    "ready": True,
                    "runtime_account_count": 1,
                    "visible_account_gap": 0,
                    "credit_state": "funded",
                }
                for provider in ("magicfit", "magic", "omagic")
            ],
        },
        "scene_video_provider_refresh_packet": {
            "contract_name": "propertyquarry.scene_video_provider_refresh_packet.v1",
            **candidate_identity,
            "generated_at": generated_at,
            "providers": [{"provider": "magicfit"}, {"provider": "omagic"}],
        },
        "scene_video_provider_refresh_packet_verifier": {
            "contract_name": "propertyquarry.scene_video_provider_refresh_packet_verifier.v1",
            **candidate_identity,
            "generated_at": generated_at,
            "status": "pass",
            "blockers": [],
            "checked_providers": ["magicfit", "omagic"],
        },
        "privacy": {
            "schema": "propertyquarry.security_posture_receipt.v1",
            **candidate_identity,
            "generated_at": generated_at,
            "status": "pass",
            "failed_count": 0,
            "failures": [],
        },
    }
    assert set(payloads) == set(REQUIRED_SOURCE_RECEIPTS)
    paths = {
        name: tmp_path / f"{name}.json" for name in REQUIRED_SOURCE_RECEIPTS
    }
    for standalone in (
        "walkthrough_provider_proof",
        "scene_video_readiness",
        "privacy",
    ):
        _write_json(paths[standalone], payloads[standalone])
    payloads["walkthrough_quality"]["provider_proof_receipt_sha256"] = (
        _sha256_path(paths["walkthrough_provider_proof"])
    )
    _write_json(paths["walkthrough_quality"], payloads["walkthrough_quality"])
    readiness_sha = _sha256_path(paths["scene_video_readiness"])
    for derived in (
        "scene_video_readiness_verifier",
        "scene_video_runtime_status",
        "scene_video_provider_refresh_packet",
    ):
        payloads[derived]["source_receipt_sha256"] = readiness_sha
        _write_json(paths[derived], payloads[derived])
    payloads["scene_video_provider_refresh_packet_verifier"][
        "source_packet_sha256"
    ] = _sha256_path(paths["scene_video_provider_refresh_packet"])
    _write_json(
        paths["scene_video_provider_refresh_packet_verifier"],
        payloads["scene_video_provider_refresh_packet_verifier"],
    )
    return paths


def test_advanced_visual_binding_is_exact_offline_candidate_authority(
    tmp_path: Path,
) -> None:
    source_paths = _source_paths(tmp_path)

    receipt = build_advanced_visual_binding_receipt(
        release_commit_sha=RELEASE_SHA,
        release_image_digest=IMAGE_DIGEST,
        source_receipt_paths=source_paths,
        max_age_hours=1,
        now=NOW,
    )

    assert receipt["status"] == "pass"
    assert receipt["binding_state"] == "bound"
    assert receipt["release_commit_sha"] == RELEASE_SHA
    assert receipt["release_image_digest"] == IMAGE_DIGEST
    assert receipt["source_artifact_hashes"] == {
        "magicfit": VIDEO_SHA,
        "omagic": VIDEO_SHA,
    }
    assert all(
        row["ready"] is True
        for row in receipt["account_quota_state"].values()
    )
    assert all(row["sha256"] for row in receipt["source_links"].values())
    assert verify_advanced_visual_binding_receipt(
        receipt,
        expected_release_commit_sha=RELEASE_SHA,
        expected_release_image_digest=IMAGE_DIGEST,
        source_receipt_paths=source_paths,
        max_age_hours=1,
        now=NOW,
    ) == []


def test_advanced_visual_binding_accepts_authenticated_omagic_without_balance_api(
    tmp_path: Path,
) -> None:
    source_paths = _source_paths(tmp_path)
    runtime_path = source_paths["scene_video_runtime_status"]
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    for row in runtime["providers"]:
        if row["provider"] in {"magic", "omagic"}:
            row["credit_state"] = "not_exposed_by_configured_api"
            row["quota_capability_state"] = (
                "authenticated_model_template_ready"
            )
    _write_json(runtime_path, runtime)

    receipt = build_advanced_visual_binding_receipt(
        release_commit_sha=RELEASE_SHA,
        release_image_digest=IMAGE_DIGEST,
        source_receipt_paths=source_paths,
        max_age_hours=1,
        now=NOW,
    )

    assert receipt["status"] == "pass"
    assert receipt["account_quota_state"]["magic"]["ready"] is True
    assert receipt["account_quota_state"]["omagic"]["ready"] is True


def test_advanced_visual_binding_rejects_stale_candidate_receipt(
    tmp_path: Path,
) -> None:
    source_paths = _source_paths(tmp_path)
    receipt = build_advanced_visual_binding_receipt(
        release_commit_sha=RELEASE_SHA,
        release_image_digest=IMAGE_DIGEST,
        source_receipt_paths=source_paths,
        max_age_hours=1,
        now=NOW,
    )
    receipt["generated_at"] = (NOW - timedelta(hours=2)).isoformat()

    errors = verify_advanced_visual_binding_receipt(
        receipt,
        expected_release_commit_sha=RELEASE_SHA,
        expected_release_image_digest=IMAGE_DIGEST,
        source_receipt_paths=source_paths,
        max_age_hours=1,
        now=NOW,
    )

    assert "binding:stale" in errors


def test_advanced_visual_binding_rejects_release_sha_mismatch(
    tmp_path: Path,
) -> None:
    source_paths = _source_paths(tmp_path)
    receipt = build_advanced_visual_binding_receipt(
        release_commit_sha=RELEASE_SHA,
        release_image_digest=IMAGE_DIGEST,
        source_receipt_paths=source_paths,
        max_age_hours=1,
        now=NOW,
    )

    errors = verify_advanced_visual_binding_receipt(
        receipt,
        expected_release_commit_sha="b" * 40,
        expected_release_image_digest=IMAGE_DIGEST,
        source_receipt_paths=source_paths,
        max_age_hours=1,
        now=NOW,
    )

    assert "release_commit_sha_mismatch" in errors


def test_advanced_visual_binding_rejects_source_receipt_hash_mismatch(
    tmp_path: Path,
) -> None:
    source_paths = _source_paths(tmp_path)
    receipt = build_advanced_visual_binding_receipt(
        release_commit_sha=RELEASE_SHA,
        release_image_digest=IMAGE_DIGEST,
        source_receipt_paths=source_paths,
        max_age_hours=1,
        now=NOW,
    )
    quality_path = source_paths["walkthrough_quality"]
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    quality["extra_unbound_field"] = True
    _write_json(quality_path, quality)

    errors = verify_advanced_visual_binding_receipt(
        receipt,
        expected_release_commit_sha=RELEASE_SHA,
        expected_release_image_digest=IMAGE_DIGEST,
        source_receipt_paths=source_paths,
        max_age_hours=1,
        now=NOW,
    )

    assert "source_receipts_mismatch" in errors


def test_advanced_visual_binding_rejects_malformed_provider_artifact_hash(
    tmp_path: Path,
) -> None:
    source_paths = _source_paths(tmp_path)
    proof_path = source_paths["walkthrough_provider_proof"]
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    proof["provider_results"][0]["video_sha256"] = "not-a-sha256"
    _write_json(proof_path, proof)

    receipt = build_advanced_visual_binding_receipt(
        release_commit_sha=RELEASE_SHA,
        release_image_digest=IMAGE_DIGEST,
        source_receipt_paths=source_paths,
        max_age_hours=1,
        now=NOW,
    )

    assert receipt["status"] == "blocked"
    assert "magicfit:provider_artifact_binding_invalid" in receipt["errors"]


def test_advanced_visual_binding_rejects_current_unbound_producer_shapes(
    tmp_path: Path,
) -> None:
    source_paths = _source_paths(tmp_path)
    for path in source_paths.values():
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.pop("release_commit_sha", None)
        payload.pop("image_digest", None)
        payload.pop("source_receipt_sha256", None)
        payload.pop("source_packet_sha256", None)
        payload.pop("provider_proof_receipt_sha256", None)
        if path.stem in {
            "scene_video_readiness_verifier",
            "scene_video_provider_refresh_packet_verifier",
        }:
            payload.pop("contract_name", None)
        _write_json(path, payload)

    receipt = build_advanced_visual_binding_receipt(
        release_commit_sha=RELEASE_SHA,
        release_image_digest=IMAGE_DIGEST,
        source_receipt_paths=source_paths,
        max_age_hours=1,
        now=NOW,
    )

    assert receipt["status"] == "blocked"
    assert receipt["binding_state"] == "unavailable_unbound_producer_receipts"
    assert "unavailable_unbound_producer_receipts" in receipt["errors"]
    assert "walkthrough_quality:release_commit_sha_missing" in receipt["errors"]
    assert (
        "scene_video_readiness_verifier:source_receipt_sha256_missing"
        in receipt["errors"]
    )
    assert all(
        row["release_commit_sha"] == "" and row["image_digest"] == ""
        for row in receipt["source_receipts"].values()
    )


def test_advanced_visual_binding_rejects_cross_candidate_and_image_replay(
    tmp_path: Path,
) -> None:
    source_paths = _source_paths(tmp_path)
    readiness_path = source_paths["scene_video_readiness"]
    readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
    readiness["release_commit_sha"] = "b" * 40
    readiness["image_digest"] = "sha256:" + "c" * 64
    _write_json(readiness_path, readiness)

    receipt = build_advanced_visual_binding_receipt(
        release_commit_sha=RELEASE_SHA,
        release_image_digest=IMAGE_DIGEST,
        source_receipt_paths=source_paths,
        max_age_hours=1,
        now=NOW,
    )

    assert receipt["status"] == "blocked"
    assert "scene_video_readiness:release_commit_sha_mismatch" in receipt["errors"]
    assert "scene_video_readiness:image_digest_mismatch" in receipt["errors"]
    assert (
        "scene_video_readiness_verifier:source_receipt_sha256_mismatch"
        in receipt["errors"]
    )


def test_advanced_visual_binding_rejects_verifier_packet_hash_replay(
    tmp_path: Path,
) -> None:
    source_paths = _source_paths(tmp_path)
    verifier_path = source_paths[
        "scene_video_provider_refresh_packet_verifier"
    ]
    verifier = json.loads(verifier_path.read_text(encoding="utf-8"))
    verifier["source_packet_sha256"] = "d" * 64
    _write_json(verifier_path, verifier)

    receipt = build_advanced_visual_binding_receipt(
        release_commit_sha=RELEASE_SHA,
        release_image_digest=IMAGE_DIGEST,
        source_receipt_paths=source_paths,
        max_age_hours=1,
        now=NOW,
    )

    assert receipt["status"] == "blocked"
    assert (
        "scene_video_provider_refresh_packet_verifier:source_packet_sha256_mismatch"
        in receipt["errors"]
    )


def test_advanced_visual_binding_output_does_not_echo_untrusted_source_values(
    tmp_path: Path,
) -> None:
    source_paths = _source_paths(tmp_path)
    verifier_path = source_paths[
        "scene_video_provider_refresh_packet_verifier"
    ]
    verifier = json.loads(verifier_path.read_text(encoding="utf-8"))
    verifier["release_commit_sha"] = "PRIVATE-CANDIDATE-TOKEN"
    verifier["image_digest"] = "PRIVATE-IMAGE-TOKEN"
    verifier["generated_at"] = "PRIVATE-TIMESTAMP-TOKEN"
    verifier["blockers"] = ["PRIVATE-PROVIDER-TOKEN"]
    verifier["checked_providers"].append("PRIVATE-PROVIDER-NAME")
    _write_json(verifier_path, verifier)

    receipt = build_advanced_visual_binding_receipt(
        release_commit_sha=RELEASE_SHA,
        release_image_digest=IMAGE_DIGEST,
        source_receipt_paths=source_paths,
        max_age_hours=1,
        now=NOW,
    )
    rendered = json.dumps(receipt, sort_keys=True)

    assert receipt["status"] == "blocked"
    assert "PRIVATE-" not in rendered
    assert receipt["isolation_state"]["blocker_count"] == 1
