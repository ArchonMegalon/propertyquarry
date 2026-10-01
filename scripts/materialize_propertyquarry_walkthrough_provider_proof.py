#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from import_magicfit_walkthrough import _load_magicfit_receipt
    from property_magicfit_delivery_contract import validate_magicfit_source_receipt
except ModuleNotFoundError:
    from scripts.import_magicfit_walkthrough import _load_magicfit_receipt
    from scripts.property_magicfit_delivery_contract import (
        validate_magicfit_source_receipt,
    )


SIDECAR_NAMES = {
    "omagic": "tour.omagic.json",
}
SUPPORTED_PROVIDERS = ("magicfit", "omagic")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise RuntimeError(f"json_object_required:{path}")
    return dict(loaded)


def _probe_video(path: Path) -> dict[str, object]:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height:format=duration,size",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(completed.stdout or "{}")
    streams = list(payload.get("streams") or [])
    stream = dict(streams[0]) if streams else {}
    format_payload = dict(payload.get("format") or {})
    result = {
        "duration_seconds": round(float(format_payload.get("duration") or 0.0), 3),
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "size_bytes": int(format_payload.get("size") or path.stat().st_size),
    }
    if any(float(result[key]) <= 0 for key in ("duration_seconds", "width", "height", "size_bytes")):
        raise RuntimeError("provider_video_probe_invalid")
    return result


def _validate_source(
    provider: str, source: dict[str, Any], *, slug: str = ""
) -> None:
    if provider == "magicfit":
        try:
            validate_magicfit_source_receipt(source, slug=slug)
        except ValueError as exc:
            raise RuntimeError("magicfit_strict_source_receipt_required") from exc
        return
    if str(source.get("provider_key") or source.get("provider") or "").strip().lower() != provider:
        raise RuntimeError("provider_source_key_mismatch")
    if str(source.get("provider_backend_key") or "").strip().lower() != provider:
        raise RuntimeError("provider_source_backend_mismatch")
    if str(source.get("render_status") or "").strip().lower() != "completed":
        raise RuntimeError("provider_source_render_incomplete")
    if source.get("model_input_consumed") is not True:
        raise RuntimeError("omagic_source_model_not_consumed")
    if not str(source.get("model_input_consumption_proof") or "").strip():
        raise RuntimeError("omagic_source_consumption_proof_missing")


def _source_sidecar(
    *,
    provider: str,
    source: dict[str, Any],
    video_relpath: str,
    source_receipt_path: Path,
    metadata: dict[str, object],
    model_relpath: str = "",
    model_sha256: str = "",
) -> dict[str, object]:
    if provider == "magicfit":
        raise RuntimeError("magicfit_shallow_public_sidecar_forbidden")
    payload: dict[str, object] = {
        "provider": "OMagic",
        "provider_key": provider,
        "provider_backend_key": provider,
        "status": "rendered",
        "render_status": "completed",
        "video_relpath": video_relpath,
        "video_sha256": str(source.get("video_sha256") or ""),
        "video_metadata": metadata,
        "source_receipt_path": str(source_receipt_path),
        "source_receipt_sha256": _sha256(source_receipt_path),
        "generated_at": _utc_now(),
    }
    payload.update(
        {
            "model_input_consumed": True,
            "model_input_consumption_proof": str(source["model_input_consumption_proof"]),
            "model_path": model_relpath,
            "model_sha256": model_sha256,
            "input_library_item_id": source.get("input_library_item_id"),
            "output_library_item_id": source.get("output_library_item_id"),
            "template_variant_id": str(source.get("template_variant_id") or ""),
            "truth_boundary": "property-specific generated reconstruction; not a measured scan or native apartment walkthrough",
        }
    )
    return payload


def materialize(
    *,
    provider: str,
    tour_root: Path,
    slug: str,
    title: str,
    video_path: Path,
    source_receipt_path: Path,
    model_path: Path | None = None,
) -> Path:
    normalized_provider = str(provider or "").strip().lower()
    if normalized_provider not in SUPPORTED_PROVIDERS:
        raise RuntimeError("unsupported_provider")
    if not slug or "/" in slug or slug in {".", ".."}:
        raise RuntimeError("invalid_bundle_slug")
    if not video_path.is_file() or not source_receipt_path.is_file():
        raise RuntimeError("provider_source_artifact_missing")
    if normalized_provider == "magicfit":
        try:
            source, _, _ = _load_magicfit_receipt(
                str(source_receipt_path),
                source=video_path,
                slug=slug,
                allow_unreceipted=False,
            )
        except SystemExit as exc:
            raise RuntimeError(
                f"magicfit_strict_source_receipt_required:{exc}"
            ) from exc
        declared_sha = str(source.get("video_sha256") or "")
        if declared_sha and declared_sha != _sha256(video_path):
            raise RuntimeError("magicfit_video_hash_mismatch")
        handoff_command = shlex.join(
            (
                "python",
                "scripts/import_magicfit_walkthrough.py",
                "--slug",
                slug,
                "--video-path",
                str(video_path),
                "--source-receipt",
                str(source_receipt_path),
            )
        )
        raise RuntimeError(
            f"magicfit_public_materialization_forbidden:{handoff_command}"
        )
    source = _load_json(source_receipt_path)
    _validate_source(normalized_provider, source, slug=slug)
    metadata = _probe_video(video_path)
    if normalized_provider == "omagic" and (model_path is None or not model_path.is_file()):
        raise RuntimeError("omagic_model_artifact_missing")

    tour_root.mkdir(parents=True, exist_ok=True)
    bundle_dir = tour_root / slug
    if bundle_dir.exists():
        raise RuntimeError(f"provider_bundle_already_exists:{bundle_dir}")
    with tempfile.TemporaryDirectory(prefix=f".{slug}-", dir=str(tour_root)) as temp_raw:
        temp_dir = Path(temp_raw)
        video_relpath = "walkthrough.mp4"
        shutil.copy2(video_path, temp_dir / video_relpath)
        copied_video_sha = _sha256(temp_dir / video_relpath)
        model_relpath = ""
        model_sha = ""
        if model_path is not None:
            model_relpath = f"generated-reconstruction/{model_path.name}"
            copied_model = temp_dir / model_relpath
            copied_model.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(model_path, copied_model)
            model_sha = _sha256(copied_model)
        manifest = {
            "slug": slug,
            "title": title,
            "video_relpath": video_relpath,
            "flythrough_video_relpath": video_relpath,
            "video_sidecar_relpath": SIDECAR_NAMES[normalized_provider],
            "video_provider_key": normalized_provider,
            "video_provider": normalized_provider,
            "flythrough_url": f"/tours/files/{slug}/{video_relpath}",
            "artifact_sha256": copied_video_sha,
            "generated_at": _utc_now(),
        }
        sidecar = _source_sidecar(
            provider=normalized_provider,
            source=source,
            video_relpath=video_relpath,
            source_receipt_path=source_receipt_path,
            metadata=metadata,
            model_relpath=model_relpath,
            model_sha256=model_sha,
        )
        sidecar["video_sha256"] = copied_video_sha
        (temp_dir / "tour.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (temp_dir / SIDECAR_NAMES[normalized_provider]).write_text(
            json.dumps(sidecar, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temp_dir.rename(bundle_dir)
    return bundle_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Materialize truthful PropertyQuarry provider-proof tour bundles.")
    parser.add_argument("--provider", choices=SUPPORTED_PROVIDERS, required=True)
    parser.add_argument("--tour-root", default="state/public_property_tours")
    parser.add_argument("--slug", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--video", required=True)
    parser.add_argument("--source-receipt", required=True)
    parser.add_argument("--model", default="")
    args = parser.parse_args()
    bundle_dir = materialize(
        provider=args.provider,
        tour_root=Path(args.tour_root).expanduser().resolve(),
        slug=str(args.slug).strip(),
        title=str(args.title).strip(),
        video_path=Path(args.video).expanduser().resolve(),
        source_receipt_path=Path(args.source_receipt).expanduser().resolve(),
        model_path=Path(args.model).expanduser().resolve() if args.model else None,
    )
    print(json.dumps({"status": "pass", "bundle_dir": str(bundle_dir), "provider": args.provider}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
