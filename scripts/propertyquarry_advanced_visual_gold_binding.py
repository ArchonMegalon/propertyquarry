#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


CONTRACT_NAME = "propertyquarry.advanced_visual_gold_binding.v1"
UNBOUND_PRODUCER_STATE = "unavailable_unbound_producer_receipts"
REQUIRED_SOURCE_RECEIPTS = (
    "walkthrough_quality",
    "walkthrough_provider_proof",
    "scene_video_readiness",
    "scene_video_readiness_verifier",
    "scene_video_runtime_status",
    "scene_video_provider_refresh_packet",
    "scene_video_provider_refresh_packet_verifier",
    "privacy",
)
SOURCE_RECEIPT_SCHEMAS = {
    "walkthrough_quality": (
        "contract_name",
        "propertyquarry.walkthrough_quality_gate.v1",
    ),
    "walkthrough_provider_proof": (
        "contract_name",
        "propertyquarry.walkthrough_provider_proof_gate.v1",
    ),
    "scene_video_readiness": (
        "contract_name",
        "propertyquarry.scene_video_readiness.v1",
    ),
    "scene_video_readiness_verifier": (
        "contract_name",
        "propertyquarry.scene_video_readiness_verifier.v1",
    ),
    "scene_video_runtime_status": (
        "contract_name",
        "propertyquarry.scene_video_runtime_status.v1",
    ),
    "scene_video_provider_refresh_packet": (
        "contract_name",
        "propertyquarry.scene_video_provider_refresh_packet.v1",
    ),
    "scene_video_provider_refresh_packet_verifier": (
        "contract_name",
        "propertyquarry.scene_video_provider_refresh_packet_verifier.v1",
    ),
    "privacy": (
        "schema",
        "propertyquarry.security_posture_receipt.v1",
    ),
}
SOURCE_RECEIPT_LINKS = (
    (
        "walkthrough_quality",
        "provider_proof_receipt_sha256",
        "walkthrough_provider_proof",
    ),
    (
        "scene_video_readiness_verifier",
        "source_receipt_sha256",
        "scene_video_readiness",
    ),
    (
        "scene_video_runtime_status",
        "source_receipt_sha256",
        "scene_video_readiness",
    ),
    (
        "scene_video_provider_refresh_packet",
        "source_receipt_sha256",
        "scene_video_readiness",
    ),
    (
        "scene_video_provider_refresh_packet_verifier",
        "source_packet_sha256",
        "scene_video_provider_refresh_packet",
    ),
)
REQUIRED_RUNTIME_PROVIDERS = ("magicfit", "magic", "omagic")
REQUIRED_MEDIA_PROVIDERS = ("magicfit", "omagic")
READY_CREDIT_STATES = {"funded", "available", "ready", "sufficient"}
AUTHENTICATED_NO_BALANCE_API_STATE = "authenticated_model_template_ready"
NO_BALANCE_API_CREDIT_STATE = "not_exposed_by_configured_api"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _utc_now(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("advanced_visual_binding_now_must_be_timezone_aware")
    return value.astimezone(timezone.utc)


def _timestamp(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_receipt(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("receipt_root_must_be_object")
    return payload


def _freshness_error(
    *,
    name: str,
    value: object,
    now: datetime,
    max_age_hours: float,
) -> str:
    observed = _timestamp(value)
    if observed is None:
        return f"{name}:generated_at_invalid"
    age_seconds = (now - observed).total_seconds()
    if age_seconds < -300:
        return f"{name}:generated_at_in_future"
    if age_seconds > max_age_hours * 3600:
        return f"{name}:stale"
    return ""


def build_advanced_visual_binding_receipt(
    *,
    release_commit_sha: str,
    release_image_digest: str,
    source_receipt_paths: Mapping[str, Path],
    max_age_hours: float = 24.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_at = _utc_now(now)
    errors: list[str] = []
    normalized_sha = str(release_commit_sha or "").strip().lower()
    normalized_digest = str(release_image_digest or "").strip().lower()
    if COMMIT_SHA_RE.fullmatch(normalized_sha) is None:
        errors.append("release_commit_sha_invalid")
    if IMAGE_DIGEST_RE.fullmatch(normalized_digest) is None:
        errors.append("release_image_digest_invalid")
    if not math.isfinite(float(max_age_hours)) or float(max_age_hours) <= 0:
        errors.append("max_age_hours_invalid")

    source_receipts: dict[str, dict[str, str]] = {}
    payloads: dict[str, dict[str, Any]] = {}
    producer_binding_errors: list[str] = []
    for name in REQUIRED_SOURCE_RECEIPTS:
        path = source_receipt_paths.get(name)
        if path is None:
            errors.append(f"{name}:path_missing")
            continue
        try:
            payload = _load_receipt(Path(path))
            digest = _sha256_path(Path(path))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{name}:unreadable:{type(exc).__name__}")
            continue
        generated_at = str(payload.get("generated_at") or "").strip()
        parsed_generated_at = _timestamp(generated_at)
        safe_generated_at = (
            parsed_generated_at.replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
            if parsed_generated_at is not None
            else ""
        )
        schema_field, expected_schema = SOURCE_RECEIPT_SCHEMAS[name]
        schema_ok = payload.get(schema_field) == expected_schema
        source_release_sha = str(payload.get("release_commit_sha") or "").strip().lower()
        source_image_digest = str(payload.get("image_digest") or "").strip().lower()
        source_receipts[name] = {
            "sha256": digest,
            "generated_at": safe_generated_at,
            "schema": expected_schema if schema_ok else "",
            "release_commit_sha": normalized_sha
            if source_release_sha == normalized_sha
            else "",
            "image_digest": normalized_digest
            if source_image_digest == normalized_digest
            else "",
        }
        payloads[name] = payload
        if not schema_ok:
            producer_binding_errors.append(f"{name}:schema_missing_or_mismatch")
        if not source_release_sha:
            producer_binding_errors.append(f"{name}:release_commit_sha_missing")
        elif COMMIT_SHA_RE.fullmatch(source_release_sha) is None:
            producer_binding_errors.append(f"{name}:release_commit_sha_invalid")
        elif source_release_sha != normalized_sha:
            producer_binding_errors.append(f"{name}:release_commit_sha_mismatch")
        if not source_image_digest:
            producer_binding_errors.append(f"{name}:image_digest_missing")
        elif IMAGE_DIGEST_RE.fullmatch(source_image_digest) is None:
            producer_binding_errors.append(f"{name}:image_digest_invalid")
        elif source_image_digest != normalized_digest:
            producer_binding_errors.append(f"{name}:image_digest_mismatch")
        if math.isfinite(float(max_age_hours)) and float(max_age_hours) > 0:
            freshness_error = _freshness_error(
                name=name,
                value=generated_at,
                now=observed_at,
                max_age_hours=float(max_age_hours),
            )
            if freshness_error:
                errors.append(freshness_error)

    source_links: dict[str, dict[str, str]] = {}
    for child_name, link_field, parent_name in SOURCE_RECEIPT_LINKS:
        child = payloads.get(child_name)
        parent_summary = source_receipts.get(parent_name)
        if child is None or parent_summary is None:
            continue
        expected_source_sha = str(parent_summary.get("sha256") or "")
        observed_source_sha = str(child.get(link_field) or "").strip().lower()
        link_key = f"{child_name}:{link_field}"
        source_links[link_key] = {
            "source_receipt": parent_name,
            "sha256": expected_source_sha
            if observed_source_sha == expected_source_sha
            else "",
        }
        if not observed_source_sha:
            producer_binding_errors.append(f"{child_name}:{link_field}_missing")
        elif SHA256_RE.fullmatch(observed_source_sha) is None:
            producer_binding_errors.append(f"{child_name}:{link_field}_invalid")
        elif observed_source_sha != expected_source_sha:
            producer_binding_errors.append(f"{child_name}:{link_field}_mismatch")

    if producer_binding_errors:
        errors.extend(producer_binding_errors)
        errors.append(UNBOUND_PRODUCER_STATE)

    proof = payloads.get("walkthrough_provider_proof", {})
    provider_results = {
        str(row.get("provider") or "").strip().lower(): dict(row)
        for row in list(proof.get("provider_results") or [])
        if isinstance(row, dict)
    }
    source_artifact_hashes: dict[str, str] = {}
    for provider in REQUIRED_MEDIA_PROVIDERS:
        row = provider_results.get(provider, {})
        artifact_hash = str(row.get("video_sha256") or "").strip().lower()
        if (
            str(row.get("status") or "").strip().lower() != "pass"
            or SHA256_RE.fullmatch(artifact_hash) is None
        ):
            errors.append(f"{provider}:provider_artifact_binding_invalid")
            continue
        source_artifact_hashes[provider] = artifact_hash
    quality = payloads.get("walkthrough_quality", {})
    quality_hash = str(quality.get("video_sha256") or "").strip().lower()
    if quality.get("status") != "pass" or SHA256_RE.fullmatch(quality_hash) is None:
        errors.append("walkthrough_quality:artifact_binding_invalid")
    elif source_artifact_hashes.get("magicfit") != quality_hash:
        errors.append("magicfit:quality_provider_artifact_hash_mismatch")

    runtime = payloads.get("scene_video_runtime_status", {})
    runtime_rows = {
        str(row.get("provider") or "").strip().lower(): dict(row)
        for row in list(runtime.get("providers") or [])
        if isinstance(row, dict)
    }
    account_quota_state: dict[str, dict[str, Any]] = {}
    for provider in REQUIRED_RUNTIME_PROVIDERS:
        row = runtime_rows.get(provider, {})
        try:
            runtime_account_count = int(row.get("runtime_account_count") or 0)
            visible_account_gap = int(row.get("visible_account_gap") or 0)
        except (TypeError, ValueError):
            runtime_account_count = 0
            visible_account_gap = -1
        credit_state = str(row.get("credit_state") or "").strip().lower()
        capability_state = str(
            row.get("quota_capability_state") or ""
        ).strip()
        credit_ready = credit_state in READY_CREDIT_STATES
        if provider in {"magic", "omagic"}:
            credit_ready = credit_ready or (
                credit_state == NO_BALANCE_API_CREDIT_STATE
                and capability_state == AUTHENTICATED_NO_BALANCE_API_STATE
            )
        ready = (
            row.get("ready") is True
            and str(row.get("status") or "").strip().lower() == "ready"
            and runtime_account_count > 0
            and visible_account_gap == 0
            and credit_ready
        )
        account_quota_state[provider] = {
            "ready": ready,
            "runtime_account_count": runtime_account_count,
            "visible_account_gap": visible_account_gap,
            "credit_state": credit_state
            if credit_ready
            else "invalid",
            "quota_capability_state": capability_state,
        }
        if not ready:
            errors.append(f"{provider}:runtime_account_or_quota_not_ready")

    privacy = payloads.get("privacy", {})
    privacy_failures = list(privacy.get("failures") or [])
    try:
        privacy_failed_count = int(privacy.get("failed_count") or 0)
    except (TypeError, ValueError):
        privacy_failed_count = -1
    privacy_ok = (
        str(privacy.get("status") or "").strip().lower() == "pass"
        and privacy_failed_count == 0
        and not privacy_failures
    )
    if not privacy_ok:
        errors.append("privacy:receipt_not_passed")
    privacy_state = {
        "ready": privacy_ok,
        "failed_count": privacy_failed_count,
    }

    isolation = payloads.get(
        "scene_video_provider_refresh_packet_verifier", {}
    )
    checked_providers = {
        str(provider or "").strip().lower()
        for provider in list(isolation.get("checked_providers") or [])
    }
    isolation_blockers = [
        str(value or "").strip()
        for value in list(isolation.get("blockers") or [])
        if str(value or "").strip()
    ]
    isolation_ok = (
        str(isolation.get("status") or "").strip().lower() == "pass"
        and not isolation_blockers
        and set(REQUIRED_MEDIA_PROVIDERS).issubset(checked_providers)
    )
    if not isolation_ok:
        errors.append("provider_isolation:receipt_not_passed")
    isolation_state = {
        "ready": isolation_ok,
        "checked_providers": sorted(
            set(REQUIRED_MEDIA_PROVIDERS).intersection(checked_providers)
        ),
        "blocker_count": len(isolation_blockers),
    }

    return {
        "contract_name": CONTRACT_NAME,
        "generated_at": observed_at.replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"
        ),
        "status": "pass" if not errors else "blocked",
        "binding_state": "bound"
        if not producer_binding_errors
        else UNBOUND_PRODUCER_STATE,
        "release_commit_sha": normalized_sha,
        "release_image_digest": normalized_digest,
        "max_age_hours": float(max_age_hours),
        "source_receipts": source_receipts,
        "source_links": source_links,
        "source_artifact_hashes": source_artifact_hashes,
        "account_quota_state": account_quota_state,
        "privacy_state": privacy_state,
        "isolation_state": isolation_state,
        "errors": sorted(set(errors)),
    }


def verify_advanced_visual_binding_receipt(
    receipt: Mapping[str, Any],
    *,
    expected_release_commit_sha: str,
    expected_release_image_digest: str,
    source_receipt_paths: Mapping[str, Path],
    max_age_hours: float = 24.0,
    now: datetime | None = None,
) -> list[str]:
    observed_at = _utc_now(now)
    errors: list[str] = []
    if receipt.get("contract_name") != CONTRACT_NAME:
        errors.append("binding_contract_invalid")
    if receipt.get("status") != "pass":
        errors.append("binding_status_not_passed")
    if receipt.get("binding_state") != "bound":
        errors.append(UNBOUND_PRODUCER_STATE)
    if str(receipt.get("release_commit_sha") or "") != str(
        expected_release_commit_sha or ""
    ).strip().lower():
        errors.append("release_commit_sha_mismatch")
    if str(receipt.get("release_image_digest") or "") != str(
        expected_release_image_digest or ""
    ).strip().lower():
        errors.append("release_image_digest_mismatch")
    freshness_error = _freshness_error(
        name="binding",
        value=receipt.get("generated_at"),
        now=observed_at,
        max_age_hours=max_age_hours,
    )
    if freshness_error:
        errors.append(freshness_error)

    expected = build_advanced_visual_binding_receipt(
        release_commit_sha=expected_release_commit_sha,
        release_image_digest=expected_release_image_digest,
        source_receipt_paths=source_receipt_paths,
        max_age_hours=max_age_hours,
        now=observed_at,
    )
    errors.extend(str(value) for value in list(expected.get("errors") or []))
    for key in (
        "binding_state",
        "source_receipts",
        "source_links",
        "source_artifact_hashes",
        "account_quota_state",
        "privacy_state",
        "isolation_state",
    ):
        if receipt.get(key) != expected.get(key):
            errors.append(f"{key}_mismatch")
    return sorted(set(errors))


def _source_paths(args: argparse.Namespace) -> dict[str, Path]:
    return {
        "walkthrough_quality": Path(args.walkthrough_quality_receipt),
        "walkthrough_provider_proof": Path(args.walkthrough_provider_proof_receipt),
        "scene_video_readiness": Path(args.scene_video_readiness_receipt),
        "scene_video_readiness_verifier": Path(
            args.scene_video_readiness_verifier_receipt
        ),
        "scene_video_runtime_status": Path(args.scene_video_runtime_status_receipt),
        "scene_video_provider_refresh_packet": Path(
            args.scene_video_provider_refresh_packet
        ),
        "scene_video_provider_refresh_packet_verifier": Path(
            args.scene_video_provider_refresh_packet_verifier_receipt
        ),
        "privacy": Path(args.privacy_receipt),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Materialize an offline exact-candidate Advanced Visual Gold binding."
    )
    parser.add_argument("--release-commit-sha", required=True)
    parser.add_argument("--release-image-digest", required=True)
    parser.add_argument("--walkthrough-quality-receipt", required=True)
    parser.add_argument("--walkthrough-provider-proof-receipt", required=True)
    parser.add_argument("--scene-video-readiness-receipt", required=True)
    parser.add_argument("--scene-video-readiness-verifier-receipt", required=True)
    parser.add_argument("--scene-video-runtime-status-receipt", required=True)
    parser.add_argument("--scene-video-provider-refresh-packet", required=True)
    parser.add_argument(
        "--scene-video-provider-refresh-packet-verifier-receipt", required=True
    )
    parser.add_argument("--privacy-receipt", required=True)
    parser.add_argument("--max-age-hours", type=float, default=24.0)
    parser.add_argument("--write", required=True)
    args = parser.parse_args()
    receipt = build_advanced_visual_binding_receipt(
        release_commit_sha=args.release_commit_sha,
        release_image_digest=args.release_image_digest,
        source_receipt_paths=_source_paths(args),
        max_age_hours=args.max_age_hours,
    )
    output = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    output_path = Path(args.write)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output, encoding="utf-8")
    print(output, end="")
    return 0 if receipt.get("status") == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
