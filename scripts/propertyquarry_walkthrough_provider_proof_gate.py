#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from property_magicfit_delivery_contract import accepted_sidecar_relpath
    from property_magicfit_public_eligibility import evaluate_magicfit_public_eligibility
except ModuleNotFoundError:
    from scripts.property_magicfit_delivery_contract import accepted_sidecar_relpath
    from scripts.property_magicfit_public_eligibility import evaluate_magicfit_public_eligibility


REQUIRED_PROVIDERS = ("magicfit", "omagic")
ORCHESTRATOR_KEY = "ea"
SIDECAR_FILENAMES = {
    "magicfit": ".magicfit-deliveries/<delivery-digest>.json",
    "omagic": "tour.omagic.json",
}
VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".m4v"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _check(name: str, ok: bool, **extra: object) -> dict[str, object]:
    return {"name": name, "ok": bool(ok), **extra}


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _safe_relpath(value: object) -> str:
    raw_value = str(value or "")
    raw = raw_value.strip()
    if (
        not raw
        or raw != raw_value
        or raw.startswith("/")
        or "\\" in raw
        or "://" in raw
        or "\x00" in raw
    ):
        return ""
    path = PurePosixPath(raw)
    normalized = "/".join(path.parts)
    if (
        path.is_absolute()
        or normalized != raw
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        return ""
    return normalized


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_disqualified(sidecar: dict[str, Any]) -> bool:
    return (
        str(sidecar.get("acceptance_status") or "").strip().lower()
        in {"disqualified", "rejected", "failed"}
        or sidecar.get("launch_eligible") is False
    )


def _walkthrough_family_fingerprint(provider: str, sidecar: dict[str, Any]) -> str:
    route_labels = [
        " ".join(str(label or "").strip().lower().split())
        for label in list(sidecar.get("route_labels") or [])
        if " ".join(str(label or "").strip().lower().split())
    ]
    composition = str(sidecar.get("composition") or "").strip().lower()
    if not route_labels or not composition:
        return ""
    canonical = json.dumps(
        {
            "provider": str(provider or "").strip().lower(),
            "composition": composition,
            "route_labels": route_labels,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _disqualified_media_registry(tour_root: Path) -> tuple[set[str], set[str]]:
    hashes: set[str] = set()
    family_fingerprints: set[str] = set()
    if not tour_root.is_dir():
        return hashes, family_fingerprints
    for bundle_dir in tour_root.iterdir():
        if not bundle_dir.is_dir():
            continue
        for provider, sidecar_name in SIDECAR_FILENAMES.items():
            sidecar = _load_json(bundle_dir / sidecar_name)
            declared_hash = str(sidecar.get("video_sha256") or "").strip().lower()
            if not _is_disqualified(sidecar):
                continue
            if len(declared_hash) == 64:
                hashes.add(declared_hash)
            fingerprint = _walkthrough_family_fingerprint(provider, sidecar)
            if fingerprint:
                family_fingerprints.add(fingerprint)
    return hashes, family_fingerprints


def _video_metadata(path: Path, *, timeout_seconds: float = 20.0) -> dict[str, object]:
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height,duration:format=duration,size",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=max(1.0, float(timeout_seconds or 20.0)),
        )
        payload = json.loads(completed.stdout or "{}")
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    streams = list(payload.get("streams") or []) if isinstance(payload, dict) else []
    stream = dict(streams[0]) if streams and isinstance(streams[0], dict) else {}
    format_payload = dict(payload.get("format") or {}) if isinstance(payload, dict) else {}
    try:
        duration = float(stream.get("duration") or format_payload.get("duration") or 0.0)
    except Exception:
        duration = 0.0
    try:
        size_bytes = int(format_payload.get("size") or path.stat().st_size)
    except Exception:
        size_bytes = 0
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    return {
        "ok": bool(duration > 0 and width > 0 and height > 0 and size_bytes > 0),
        "duration_seconds": round(duration, 3),
        "width": width,
        "height": height,
        "size_bytes": size_bytes,
        "error": "",
    }


def _video_decodes(path: Path, *, timeout_seconds: float = 30.0) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-f",
                "null",
                "-",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=max(1.0, float(timeout_seconds or 30.0)),
        )
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    error = str(completed.stderr or "").strip().replace("\n", " ")[-500:]
    return completed.returncode == 0, error


def _normalized_labels(value: object) -> list[str]:
    return [
        " ".join(str(label or "").strip().lower().split())
        for label in list(value or [])
        if " ".join(str(label or "").strip().lower().split())
    ]


def _safe_int(value: object, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return int(default)


def _verify_magicfit_exact_v4_provider_proof(
    bundle_dir: Path,
    *,
    ffprobe_timeout_seconds: float = 20.0,
    decode_timeout_seconds: float = 30.0,
) -> dict[str, object]:
    manifest_path = bundle_dir / "tour.json"
    manifest = _load_json(manifest_path)
    eligibility = evaluate_magicfit_public_eligibility(bundle_dir, manifest)
    delivery_digest = str(eligibility.delivery_digest or "").strip().lower()
    try:
        sidecar_relpath = accepted_sidecar_relpath(delivery_digest) if delivery_digest else ""
    except (TypeError, ValueError):
        sidecar_relpath = ""
    video_relpath = _safe_relpath(eligibility.video_relpath)
    sidecar_path = bundle_dir / sidecar_relpath if sidecar_relpath else bundle_dir / "__missing__"
    video_path = bundle_dir / video_relpath if video_relpath else bundle_dir / "__missing__"
    sidecar = _load_json(sidecar_path) if sidecar_relpath else {}
    metadata = (
        _video_metadata(video_path, timeout_seconds=ffprobe_timeout_seconds)
        if eligibility.eligible and video_path.is_file()
        else {}
    )
    decode_ok, decode_error = (
        _video_decodes(video_path, timeout_seconds=decode_timeout_seconds)
        if metadata.get("ok") is True
        else (False, "video_metadata_unavailable")
    )
    declared_video_sha256 = str(sidecar.get("video_sha256") or "").strip().lower()
    actual_video_sha256 = (
        _sha256(video_path)
        if eligibility.eligible and video_path.is_file()
        else ""
    )
    checks = [
        _check("bundle_directory_not_symlink", not bundle_dir.is_symlink(), bundle_dir=str(bundle_dir)),
        _check("manifest_present", manifest_path.is_file(), manifest_path=str(manifest_path)),
        _check("magicfit_exact_v4_declared", eligibility.declared, reason=eligibility.reason),
        _check("magicfit_exact_v4_public_eligible", eligibility.declared and eligibility.eligible, reason=eligibility.reason),
        _check("accepted_delivery_digest_present", len(delivery_digest) == 64, delivery_digest=delivery_digest),
        _check("accepted_sidecar_present", bool(sidecar_relpath) and sidecar_path.is_file(), sidecar_path=str(sidecar_path)),
        _check("verified_video_relpath_present", bool(video_relpath), video_relpath=video_relpath),
        _check("verified_video_file_present", bool(video_relpath) and video_path.is_file(), video_path=str(video_path)),
        _check(
            "verified_video_sha256_matches",
            len(declared_video_sha256) == 64 and declared_video_sha256 == actual_video_sha256,
            declared_video_sha256=declared_video_sha256,
            actual_video_sha256=actual_video_sha256,
        ),
        _check("video_metadata_available", metadata.get("ok") is True, metadata=metadata),
        _check("video_frame_decodes", decode_ok, error=decode_error),
    ]
    failed = [row for row in checks if not row.get("ok")]
    return {
        "provider": "magicfit",
        "slug": bundle_dir.name,
        "status": "pass" if not failed else "fail",
        "failed_count": len(failed),
        "check_count": len(checks),
        "checks": checks,
        "sidecar_path": str(sidecar_path) if sidecar_relpath else "",
        "video_relpath": video_relpath,
        "video_path": str(video_path) if video_relpath else "",
        "video_sha256": actual_video_sha256,
        "delivery_digest": delivery_digest,
        "eligibility_reason": eligibility.reason,
        "media_disqualified": not (eligibility.declared and eligibility.eligible),
    }


def _verify_bundle_provider_proof(
    bundle_dir: Path,
    *,
    provider: str,
    disqualified_video_hashes: set[str] | None = None,
    disqualified_family_fingerprints: set[str] | None = None,
    ffprobe_timeout_seconds: float = 20.0,
    decode_timeout_seconds: float = 30.0,
) -> dict[str, object]:
    provider_key = str(provider or "").strip().lower()
    if provider_key == "magicfit":
        return _verify_magicfit_exact_v4_provider_proof(
            bundle_dir,
            ffprobe_timeout_seconds=ffprobe_timeout_seconds,
            decode_timeout_seconds=decode_timeout_seconds,
        )
    sidecar_name = SIDECAR_FILENAMES[provider_key]
    sidecar_path = bundle_dir / sidecar_name
    manifest_path = bundle_dir / "tour.json"
    sidecar = _load_json(sidecar_path)
    manifest = _load_json(manifest_path)
    video_relpath = _safe_relpath(sidecar.get("video_relpath"))
    video_path = (bundle_dir / video_relpath).resolve() if video_relpath else bundle_dir / "__missing__"
    inside_bundle = bool(video_relpath and bundle_dir.resolve() in video_path.parents)
    metadata = _video_metadata(video_path, timeout_seconds=ffprobe_timeout_seconds) if inside_bundle and video_path.is_file() else {}
    declared_video_sha256 = str(sidecar.get("video_sha256") or "").strip().lower()
    actual_video_sha256 = _sha256(video_path) if inside_bundle and video_path.is_file() else ""
    disqualified_hashes = disqualified_video_hashes or set()
    family_fingerprint = _walkthrough_family_fingerprint(provider_key, sidecar)
    disqualified_fingerprints = disqualified_family_fingerprints or set()
    media_disqualified = _is_disqualified(sidecar) or (
        bool(declared_video_sha256) and declared_video_sha256 in disqualified_hashes
    ) or (
        bool(family_fingerprint) and family_fingerprint in disqualified_fingerprints
    )
    decode_ok, decode_error = (
        _video_decodes(video_path, timeout_seconds=decode_timeout_seconds)
        if metadata.get("ok") is True
        else (False, "video_metadata_unavailable")
    )
    manifest_provider = str(
        manifest.get("video_provider_key")
        or manifest.get("video_provider")
        or manifest.get("video_render_provider")
        or ""
    ).strip().lower()
    manifest_video_relpath = _safe_relpath(
        manifest.get("video_relpath")
        or manifest.get("flythrough_video_relpath")
        or manifest.get("magicfit_video_relpath")
    )
    checks = [
        _check(
            "bundle_directory_not_symlink",
            not bundle_dir.is_symlink(),
            bundle_dir=str(bundle_dir),
        ),
        _check("sidecar_present", sidecar_path.is_file(), sidecar_path=str(sidecar_path)),
        _check("manifest_present", manifest_path.is_file(), manifest_path=str(manifest_path)),
        _check("provider_key_exact", str(sidecar.get("provider_key") or "").strip().lower() == provider_key),
        _check(
            "provider_backend_key_exact",
            str(sidecar.get("provider_backend_key") or "").strip().lower() == provider_key,
        ),
        _check(
            "render_completed",
            str(sidecar.get("status") or "").strip().lower() == "rendered"
            and str(sidecar.get("render_status") or "").strip().lower() == "completed",
        ),
        _check("video_relpath_safe", bool(video_relpath) and PurePosixPath(video_relpath).suffix.lower() in VIDEO_SUFFIXES),
        _check("video_file_present", inside_bundle and video_path.is_file(), video_path=str(video_path)),
        _check("video_sha256_declared", len(declared_video_sha256) == 64),
        _check(
            "video_sha256_matches",
            bool(declared_video_sha256) and declared_video_sha256 == actual_video_sha256,
            declared_video_sha256=declared_video_sha256,
            actual_video_sha256=actual_video_sha256,
        ),
        _check(
            "media_not_disqualified",
            not media_disqualified,
            acceptance_status=str(sidecar.get("acceptance_status") or "unreviewed"),
            launch_eligible=sidecar.get("launch_eligible"),
        ),
        _check("video_metadata_available", metadata.get("ok") is True, metadata=metadata),
        _check("video_frame_decodes", decode_ok, error=decode_error),
        _check("manifest_provider_matches", manifest_provider == provider_key, manifest_provider=manifest_provider),
        _check(
            "manifest_video_matches",
            bool(video_relpath) and manifest_video_relpath == video_relpath,
            manifest_video_relpath=manifest_video_relpath,
        ),
    ]
    if provider_key == "magicfit":
        route_labels = _normalized_labels(sidecar.get("route_labels"))
        covered_labels = _normalized_labels(sidecar.get("covered_route_labels"))
        try:
            duration = float(sidecar.get("duration_seconds") or 0.0)
            required_duration = float(sidecar.get("required_duration_seconds") or 0.0)
        except Exception:
            duration = 0.0
            required_duration = 0.0
        checks.extend(
            (
                _check(
                    "magicfit_continuity_composition",
                    str(sidecar.get("composition") or "").strip() == "boundary_verified_frame_continuation",
                ),
                _check("magicfit_segment_count", _safe_int(sidecar.get("segment_count")) > 0),
                _check(
                    "magicfit_route_coverage",
                    bool(route_labels) and route_labels == covered_labels,
                    route_labels=route_labels,
                    covered_route_labels=covered_labels,
                ),
                _check(
                    "magicfit_duration_floor",
                    duration > 0 and required_duration > 0 and duration + 0.25 >= required_duration,
                    duration_seconds=duration,
                    required_duration_seconds=required_duration,
                ),
            )
        )
    if provider_key == "omagic":
        checks.extend(
            (
                _check("omagic_model_input_consumed", sidecar.get("model_input_consumed") is True),
                _check(
                    "omagic_model_input_consumption_proof",
                    bool(str(sidecar.get("model_input_consumption_proof") or "").strip()),
                ),
                _check(
                    "omagic_model_input_declared",
                    bool(str(sidecar.get("model_path") or sidecar.get("model_url") or "").strip()),
                ),
            )
        )
    failed = [row for row in checks if not row.get("ok")]
    return {
        "provider": provider_key,
        "slug": bundle_dir.name,
        "status": "pass" if not failed else "fail",
        "failed_count": len(failed),
        "check_count": len(checks),
        "checks": checks,
        "sidecar_path": str(sidecar_path),
        "video_relpath": video_relpath,
        "video_path": str(video_path) if video_relpath else "",
        "video_sha256": actual_video_sha256,
        "walkthrough_family_fingerprint": family_fingerprint,
        "media_disqualified": media_disqualified,
    }


def build_walkthrough_provider_proof_receipt(
    *,
    tour_root: Path,
    required_providers: tuple[str, ...] = REQUIRED_PROVIDERS,
    ffprobe_timeout_seconds: float = 20.0,
    decode_timeout_seconds: float = 30.0,
    release_commit_sha: str = "",
    image_digest: str = "",
) -> dict[str, object]:
    resolved_root = tour_root.expanduser().resolve()
    disqualified_hashes, disqualified_family_fingerprints = _disqualified_media_registry(
        resolved_root
    )
    provider_results: list[dict[str, object]] = []
    missing_providers: list[str] = []
    for provider in required_providers:
        candidates: list[tuple[float, dict[str, object]]] = []
        if resolved_root.is_dir():
            for bundle_dir in sorted(resolved_root.iterdir(), key=lambda path: path.name):
                if not bundle_dir.is_dir():
                    continue
                if provider == "magicfit":
                    manifest = _load_json(bundle_dir / "tour.json")
                    provider_aliases = {
                        str(manifest.get(key) or "").strip().lower()
                        for key in (
                            "video_provider",
                            "video_provider_key",
                            "video_provider_backend_key",
                            "video_render_provider",
                        )
                    }
                    magicfit_footprint = bool(
                        "magicfit" in provider_aliases
                        or isinstance(manifest.get("magicfit_import"), dict)
                        or str(manifest.get("video_sidecar_relpath") or "").startswith(".magicfit-deliveries/")
                        or str(manifest.get("video_relpath") or "").startswith("magicfit-media/")
                        or (bundle_dir / "tour.magicfit.json").is_file()
                    )
                    if not magicfit_footprint:
                        continue
                    result = _verify_magicfit_exact_v4_provider_proof(
                        bundle_dir,
                        ffprobe_timeout_seconds=ffprobe_timeout_seconds,
                        decode_timeout_seconds=decode_timeout_seconds,
                    )
                    evidence_path = Path(str(result.get("sidecar_path") or ""))
                    timestamp_path = evidence_path if evidence_path.is_file() else bundle_dir / "tour.json"
                else:
                    sidecar_path = bundle_dir / SIDECAR_FILENAMES[provider]
                    if not sidecar_path.is_file():
                        continue
                    result = _verify_bundle_provider_proof(
                        bundle_dir,
                        provider=provider,
                        disqualified_video_hashes=disqualified_hashes,
                        disqualified_family_fingerprints=disqualified_family_fingerprints,
                        ffprobe_timeout_seconds=ffprobe_timeout_seconds,
                        decode_timeout_seconds=decode_timeout_seconds,
                    )
                    timestamp_path = sidecar_path
                try:
                    modified_at = float(timestamp_path.stat().st_mtime)
                except OSError:
                    modified_at = 0.0
                candidates.append((modified_at, result))
        passing = [row for _, row in candidates if row.get("status") == "pass"]
        if passing:
            selected = max(
                ((modified_at, row) for modified_at, row in candidates if row.get("status") == "pass"),
                key=lambda item: item[0],
            )[1]
        elif candidates:
            selected = max(candidates, key=lambda item: item[0])[1]
        else:
            missing_providers.append(provider)
            selected = {
                "provider": provider,
                "slug": "",
                "status": "fail",
                "failed_count": 1,
                "check_count": 1,
                "checks": [
                    _check(
                        "provider_proof_sidecar_present",
                        False,
                        sidecar_filename=(
                            "accepted-v4 digest sidecar"
                            if provider == "magicfit"
                            else SIDECAR_FILENAMES[provider]
                        ),
                    )
                ],
                "sidecar_path": "",
                "video_relpath": "",
                "video_path": "",
            }
        provider_results.append({**selected, "candidate_count": len(candidates)})
    failed = [row for row in provider_results if row.get("status") != "pass"]
    gate_status = "pass" if not failed else "fail"
    provenance_index = [
        {
            "key": ORCHESTRATOR_KEY,
            "kind": "orchestrator",
            "role": "governance_and_verification",
            "status": gate_status,
            "media_authorship": False,
            "evidence_contract": "propertyquarry.walkthrough_provider_proof_gate.v1",
        },
        *[
            {
                "key": str(row.get("provider") or ""),
                "kind": "media_provider",
                "role": "walkthrough_media_provider",
                "status": str(row.get("status") or "fail"),
                "media_authorship": True,
                "evidence_sidecar_path": str(row.get("sidecar_path") or ""),
                "evidence_bundle_slug": str(row.get("slug") or ""),
                "evidence_video_relpath": str(row.get("video_relpath") or ""),
                "evidence_video_sha256": str(row.get("video_sha256") or ""),
            }
            for row in provider_results
        ],
    ]
    return {
        "contract_name": "propertyquarry.walkthrough_provider_proof_gate.v1",
        "generated_at": _utc_now(),
        "release_commit_sha": str(release_commit_sha or "").strip().lower(),
        "image_digest": str(image_digest or "").strip().lower(),
        "status": gate_status,
        "tour_root": str(resolved_root),
        "required_providers": list(required_providers),
        "verified_providers": [str(row.get("provider") or "") for row in provider_results if row.get("status") == "pass"],
        "verified_orchestrators": [ORCHESTRATOR_KEY] if gate_status == "pass" else [],
        "indexed_participants": [row["key"] for row in provenance_index],
        "provenance_index": provenance_index,
        "missing_providers": missing_providers,
        "provider_count": len(provider_results),
        "failed_count": len(failed),
        "disqualified_video_sha256s": sorted(disqualified_hashes),
        "disqualified_walkthrough_family_fingerprints": sorted(
            disqualified_family_fingerprints
        ),
        "provider_results": provider_results,
        "notes": [
            "Provider readiness is not walkthrough proof.",
            "MagicFit passes only through the shared exact accepted-v4 public-eligibility contract; tour.magicfit.json is never release authority.",
            "Pass requires accepted provider provenance, a manifest-linked hosted bundle video, and a decoded video frame.",
            "EA is indexed only as the governance and verification orchestrator; it is not credited as the MagicFit or OMagic media author.",
            "An operator disqualification applies to every bundle carrying the same video SHA-256.",
            "A rejected walkthrough route family remains rejected across frame-rate, interpolation, and encoding variants.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Hard exact-v4 MagicFit and OMagic walkthrough provider proof gate.")
    parser.add_argument("--tour-root", default=os.getenv("EA_PUBLIC_TOUR_DIR", "state/public_property_tours"))
    parser.add_argument("--providers", default=",".join(REQUIRED_PROVIDERS))
    parser.add_argument("--ffprobe-timeout-seconds", type=float, default=20.0)
    parser.add_argument("--decode-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--release-commit-sha", default="")
    parser.add_argument("--image-digest", default="")
    parser.add_argument("--write", default="_completion/smoke/property-live-walkthrough-provider-proof-latest.json")
    args = parser.parse_args()
    providers = tuple(
        provider
        for provider in (item.strip().lower() for item in str(args.providers or "").split(","))
        if provider in REQUIRED_PROVIDERS
    )
    receipt = build_walkthrough_provider_proof_receipt(
        tour_root=Path(args.tour_root),
        required_providers=providers or REQUIRED_PROVIDERS,
        ffprobe_timeout_seconds=max(1.0, float(args.ffprobe_timeout_seconds or 20.0)),
        decode_timeout_seconds=max(1.0, float(args.decode_timeout_seconds or 30.0)),
        release_commit_sha=args.release_commit_sha,
        image_digest=args.image_digest,
    )
    output = json.dumps(receipt, ensure_ascii=True, indent=2, sort_keys=True)
    if args.write:
        out_path = Path(args.write)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0 if receipt.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
