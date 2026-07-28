#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

try:
    from property_tour_host_safety import (
        TourHostSafetyError,
        bounded_env_int,
        bounded_lane_lock,
        require_bounded_file,
        require_free_disk,
        tour_asset_max_bytes,
        tour_manifest_max_bytes,
    )
except ModuleNotFoundError:
    from scripts.property_tour_host_safety import (
        TourHostSafetyError,
        bounded_env_int,
        bounded_lane_lock,
        require_bounded_file,
        require_free_disk,
        tour_asset_max_bytes,
        tour_manifest_max_bytes,
    )


PUBLIC_VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm"}
MAGICFIT_HOSTED_VIDEO_RE = re.compile(
    r"^https://(?:cdn\.pushowl\.com|media\.powlcdn\.com)/magicfit/[^\"'\s<>]+?\.(?:mp4|webm)(?:[?#][^\"'\s<>]*)?$",
    re.IGNORECASE,
)


class _DuplicateJsonKey(ValueError):
    pass


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"nonfinite:{value}")


def _public_tour_dir() -> Path:
    return Path(os.getenv("EA_PUBLIC_TOUR_DIR") or "/data/public_property_tours").expanduser().resolve()


def _safe_relpath(value: object) -> str:
    """Return only an already-canonical, filesystem-safe relative path."""

    if not isinstance(value, str) or not value or value != value.strip():
        return ""
    if "\\" in value or value.startswith("/"):
        return ""
    if any(part in {"", ".", ".."} for part in value.split("/")):
        return ""
    if any(
        ord(character) < 0x20
        or ord(character) == 0x7F
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        return ""
    if PurePosixPath(value).as_posix() != value:
        return ""
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: dict[str, object], *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.chmod(mode)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _copy_video_atomic(source: Path, target: Path, *, maximum_bytes: int) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        source_size = int(source.stat(follow_symlinks=False).st_size)
        require_free_disk(
            target.parent,
            reason_prefix="magicfit_import",
            expected_write_bytes=source_size,
        )
    except TourHostSafetyError:
        raise
    except OSError as exc:
        raise TourHostSafetyError("magicfit_video_unreadable") from exc
    temporary_path: Path | None = None
    total = 0
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temporary_path = Path(output.name)
            with source.open("rb") as input_handle:
                while True:
                    chunk = input_handle.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > maximum_bytes:
                        raise TourHostSafetyError("magicfit_video_too_large")
                    output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if total <= 0:
            raise TourHostSafetyError("magicfit_video_empty")
        temporary_path.chmod(0o644)
        os.replace(temporary_path, target)
        temporary_path = None
        return total
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _video_is_playable(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix not in PUBLIC_VIDEO_EXTENSIONS:
        return False
    try:
        with path.open("rb") as handle:
            header = handle.read(64)
    except OSError:
        return False
    if len(header) < 12:
        return False
    signature_ok = False
    if suffix in {".mp4", ".m4v", ".mov"}:
        signature_ok = b"ftyp" in header[:32]
    elif suffix == ".webm":
        signature_ok = header.startswith(b"\x1aE\xdf\xa3")
    if not signature_ok:
        return False
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return True
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_type,duration:format=duration",
                "-of",
                "json",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception:
        return False
    if result.returncode != 0:
        return False
    try:
        payload = json.loads(result.stdout or "{}")
    except Exception:
        return False
    streams = [row for row in list(payload.get("streams") or []) if isinstance(row, dict)]
    if not any(str(row.get("codec_type") or "").strip().lower() == "video" for row in streams):
        return False
    durations: list[float] = []
    if isinstance(payload.get("format"), dict):
        try:
            durations.append(float(payload["format"].get("duration")))
        except Exception:
            pass
    for row in streams:
        try:
            durations.append(float(row.get("duration")))
        except Exception:
            pass
    return bool(durations and max(durations) > 0.0)


def _receipt_target_matches_slug(payload: dict[str, object], *, slug: str) -> bool:
    expected = slug if isinstance(slug, str) and slug == slug.strip() else ""
    if not expected:
        return False
    for key in ("target_slug", "tour_slug", "property_slug", "slug"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip() == expected:
            return True
    for key in ("property_url", "tour_url", "hosted_url", "public_url"):
        raw_value = payload.get(key)
        value = raw_value.strip().rstrip("/") if isinstance(raw_value, str) else ""
        if value and value.rsplit("/", 1)[-1] == expected:
            return True
    return False


def _load_magicfit_receipt(
    path_value: str,
    *,
    source: Path,
    slug: str,
    allow_unreceipted: bool,
) -> tuple[dict[str, object], str, str]:
    if allow_unreceipted:
        return {}, "", ""
    receipt_path = Path(path_value or "").expanduser().resolve()
    if not receipt_path.is_file():
        raise SystemExit("magicfit_receipt_missing")
    try:
        require_bounded_file(
            receipt_path,
            reason_prefix="magicfit_receipt",
            maximum_bytes=bounded_env_int(
                "PROPERTYQUARRY_MAGICFIT_RECEIPT_MAX_BYTES",
                default=1024 * 1024,
                minimum=1_024,
                maximum=8 * 1024 * 1024,
            ),
        )
        receipt_bytes = receipt_path.read_bytes()
        payload = json.loads(
            receipt_bytes.decode("utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_nonfinite_json,
        )
    except TourHostSafetyError as exc:
        raise SystemExit(str(exc)) from exc
    except Exception as exc:
        raise SystemExit(f"magicfit_receipt_invalid:{type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("magicfit_receipt_invalid")
    provider_value = payload.get("provider")
    provider = provider_value.strip().lower() if isinstance(provider_value, str) else ""
    if provider != "magicfit":
        raise SystemExit("magicfit_receipt_provider_mismatch")
    output_file_value = payload.get("output_file")
    if "output_file" in payload and not isinstance(output_file_value, str):
        raise SystemExit("magicfit_receipt_output_invalid:type")
    output_file = output_file_value.strip() if isinstance(output_file_value, str) else ""
    if output_file:
        try:
            if Path(output_file).expanduser().resolve() != source:
                raise SystemExit("magicfit_receipt_output_mismatch")
        except OSError as exc:
            raise SystemExit(f"magicfit_receipt_output_invalid:{type(exc).__name__}") from exc
    if not _receipt_target_matches_slug(payload, slug=slug):
        raise SystemExit("magicfit_receipt_target_mismatch")
    backend_value = payload.get("provider_backend_key")
    backend = backend_value.strip().lower() if isinstance(backend_value, str) else ""
    if backend != "magicfit":
        raise SystemExit("magicfit_receipt_backend_mismatch")
    render_value = payload.get("render_status")
    render_status = render_value.strip().lower() if isinstance(render_value, str) else ""
    if render_status not in {"completed", "rendered", "success", "succeeded"}:
        raise SystemExit("magicfit_receipt_render_incomplete")
    hosted_value = payload.get("hosted_walkthrough_video_url")
    output_url_value = payload.get("video_output_url")
    if (
        "hosted_walkthrough_video_url" in payload
        and not isinstance(hosted_value, str)
    ) or ("video_output_url" in payload and not isinstance(output_url_value, str)):
        raise SystemExit("magicfit_receipt_hosted_video_unverified")
    hosted_video_url = (
        hosted_value.strip()
        if isinstance(hosted_value, str) and hosted_value.strip()
        else output_url_value.strip()
        if isinstance(output_url_value, str)
        else ""
    )
    if not MAGICFIT_HOSTED_VIDEO_RE.match(hosted_video_url):
        raise SystemExit("magicfit_receipt_hosted_video_unverified")
    return payload, str(receipt_path), hashlib.sha256(receipt_bytes).hexdigest()


def _coverage_proof_from_receipt(payload: dict[str, object]) -> dict[str, object]:
    for key in (
        "walkthrough_coverage_proof",
        "magicfit_walkthrough_coverage",
        "walkthrough_quality_receipt",
        "coverage_proof",
    ):
        value = payload.get(key)
        if isinstance(value, dict):
            return dict(value)
    return {}


def _main_unlocked() -> int:
    parser = argparse.ArgumentParser(description="Import a verified MagicFit walkthrough video into a public tour bundle.")
    parser.add_argument("--slug", required=True, help="Existing PropertyQuarry public tour slug.")
    parser.add_argument("--video-path", required=True, help="Playable MagicFit MP4/M4V/MOV/WebM render.")
    parser.add_argument("--target-relpath", default="", help="Optional target path inside the tour bundle.")
    parser.add_argument("--source-receipt", default="", help="MagicFit render receipt path to reference without embedding secrets.")
    parser.add_argument(
        "--allow-unreceipted-test-asset",
        action="store_true",
        help="Allow a playable local fixture without MagicFit provenance. Intended for tests only.",
    )
    args = parser.parse_args()

    slug = _safe_relpath(args.slug)
    if "/" in slug or not slug:
        raise SystemExit("invalid_tour_slug")
    source = Path(args.video_path).expanduser().resolve()
    if not source.is_file():
        raise SystemExit("magicfit_video_missing")
    try:
        require_bounded_file(
            source,
            reason_prefix="magicfit_video",
            maximum_bytes=tour_asset_max_bytes(),
        )
    except TourHostSafetyError as exc:
        raise SystemExit(str(exc)) from exc
    if not _video_is_playable(source):
        raise SystemExit("magicfit_video_unverified")
    receipt_payload, _receipt_relpath, source_receipt_sha256 = _load_magicfit_receipt(
        args.source_receipt,
        source=source,
        slug=slug,
        allow_unreceipted=bool(args.allow_unreceipted_test_asset),
    )

    bundle_dir = _public_tour_dir() / slug
    manifest_path = bundle_dir / "tour.json"
    if not manifest_path.is_file():
        raise SystemExit("tour_manifest_missing")
    try:
        require_bounded_file(
            manifest_path,
            reason_prefix="tour_manifest",
            maximum_bytes=tour_manifest_max_bytes(),
        )
    except TourHostSafetyError as exc:
        raise SystemExit(str(exc)) from exc

    if args.target_relpath:
        target_relpath = _safe_relpath(args.target_relpath)
        if not target_relpath:
            raise SystemExit("invalid_magicfit_target")
    else:
        target_relpath = f"magicfit-walkthrough{source.suffix.lower()}"
    if PurePosixPath(target_relpath).suffix.lower() not in PUBLIC_VIDEO_EXTENSIONS:
        raise SystemExit("invalid_magicfit_target")
    target = (bundle_dir / target_relpath).resolve()
    if bundle_dir.resolve() not in target.parents:
        raise SystemExit("invalid_magicfit_target")

    try:
        payload = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_nonfinite_json,
        )
    except Exception as exc:
        raise SystemExit(f"invalid_tour_manifest:{type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("invalid_tour_manifest")

    try:
        _copy_video_atomic(source, target, maximum_bytes=tour_asset_max_bytes())
    except TourHostSafetyError as exc:
        raise SystemExit(str(exc)) from exc

    imported_at = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    video_sha256 = _sha256(target)
    delivery_sidecar_relpath = "tour.magicfit.json"
    delivery_sidecar = {
        "contract_name": "propertyquarry.magicfit_delivery_acceptance.v1",
        "provider": "magicfit",
        "provider_key": "magicfit",
        "provider_backend_key": "magicfit",
        # _load_magicfit_receipt has already accepted the provider's equivalent
        # success spellings.  The public acceptance contract uses one canonical
        # value so reviewers and verifiers do not need alias logic.
        "render_status": "completed",
        "status": "rendered_pending_delivery_acceptance",
        "acceptance_status": "pending",
        "launch_eligible": False,
        "video_relpath": target_relpath,
        "video_sha256": video_sha256,
        "source_receipt_sha256": source_receipt_sha256,
        "generated_at": imported_at,
    }
    _write_json_atomic(bundle_dir / delivery_sidecar_relpath, delivery_sidecar)

    payload["video_provider"] = "magicfit"
    payload["video_provider_backend_key"] = "magicfit"
    payload["video_relpath"] = target_relpath
    payload["video_sidecar_relpath"] = delivery_sidecar_relpath
    magicfit_import = {
        "source": "magicfit_rendered_walkthrough",
        "provider_backend_key": "magicfit",
        "proof_status": "render_verified_pending_delivery_acceptance",
        "imported_at": imported_at,
        "target_relpath": target_relpath,
        "sha256": video_sha256,
        "size_bytes": target.stat().st_size,
        "source_receipt_sha256": source_receipt_sha256,
        "delivery_sidecar_relpath": delivery_sidecar_relpath,
    }
    coverage_proof = _coverage_proof_from_receipt(receipt_payload)
    if coverage_proof:
        payload["video_coverage_proof"] = (
            "route_coverage_verified_pending_delivery_acceptance"
        )
        payload["walkthrough_coverage_proof"] = coverage_proof
        magicfit_import["coverage_proof"] = coverage_proof
    else:
        payload["video_coverage_proof"] = (
            "provider_render_pending_delivery_acceptance"
        )
    payload["magicfit_import"] = magicfit_import
    _write_json_atomic(manifest_path, payload)
    print(
        json.dumps(
            {
                "status": "imported",
                "slug": slug,
                "video_relpath": target_relpath,
                "video_url": f"/tours/files/{slug}/{target_relpath}",
                "provider": "magicfit",
                "provider_backend_key": "magicfit",
                "acceptance_status": "pending",
                "launch_eligible": False,
            },
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    try:
        with bounded_lane_lock("magicfit-import"):
            return _main_unlocked()
    except TourHostSafetyError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
