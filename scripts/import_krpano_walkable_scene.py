#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from PIL import Image

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


PANORAMA_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
EQUIRECTANGULAR_MIN_RATIO = 1.9
EQUIRECTANGULAR_MAX_RATIO = 2.1
CANONICAL_CUBE_FACE_KEYS = ("f", "b", "l", "r", "u", "d")


def _public_tour_dir() -> Path:
    return Path(os.getenv("EA_PUBLIC_TOUR_DIR") or "/data/public_property_tours").expanduser().resolve()


def _safe_relpath(value: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/").lstrip("/")
    parts = [part for part in normalized.split("/") if part and part not in {".", ".."}]
    return "/".join(parts)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _image_dimensions(path: Path) -> tuple[int, int]:
    try:
        with Image.open(path) as image:
            return int(image.width), int(image.height)
    except Exception:
        return (0, 0)


def _require_image_budget(path: Path, *, reason_prefix: str) -> None:
    try:
        require_bounded_file(
            path,
            reason_prefix=reason_prefix,
            maximum_bytes=tour_asset_max_bytes(),
        )
    except TourHostSafetyError as exc:
        raise SystemExit(str(exc)) from exc


def _validate_pixel_budget(width: int, height: int) -> None:
    maximum_pixels = bounded_env_int(
        "PROPERTYQUARRY_TOUR_IMAGE_MAX_PIXELS",
        default=100_000_000,
        minimum=1_048_576,
        maximum=250_000_000,
    )
    if width <= 0 or height <= 0 or width * height > maximum_pixels:
        raise SystemExit("krpano_image_pixel_limit")


def _validate_equirectangular(path: Path) -> tuple[int, int]:
    if path.suffix.lower() not in PANORAMA_IMAGE_EXTENSIONS:
        raise SystemExit("krpano_panorama_extension_invalid")
    width, height = _image_dimensions(path)
    _validate_pixel_budget(width, height)
    if width < 1024 or height < 512:
        raise SystemExit("krpano_panorama_too_small")
    ratio = width / height if height else 0
    if not EQUIRECTANGULAR_MIN_RATIO <= ratio <= EQUIRECTANGULAR_MAX_RATIO:
        raise SystemExit("krpano_panorama_not_equirectangular")
    return width, height


def _validate_cube_face(path: Path) -> tuple[int, int]:
    if path.suffix.lower() not in PANORAMA_IMAGE_EXTENSIONS:
        raise SystemExit("krpano_cube_face_extension_invalid")
    width, height = _image_dimensions(path)
    _validate_pixel_budget(width, height)
    if width < 512 or height < 512:
        raise SystemExit("krpano_cube_face_too_small")
    ratio = width / height if height else 0
    if not 0.9 <= ratio <= 1.1:
        raise SystemExit("krpano_cube_face_not_square")
    return width, height


def _copy_asset(source: Path, *, bundle_dir: Path, target_relpath: str) -> tuple[str, dict[str, object]]:
    relpath = _safe_relpath(target_relpath)
    if not relpath:
        raise SystemExit("invalid_krpano_target")
    target = (bundle_dir / relpath).resolve()
    if bundle_dir.resolve() not in target.parents:
        raise SystemExit("invalid_krpano_target")
    target.parent.mkdir(parents=True, exist_ok=True)
    maximum_bytes = tour_asset_max_bytes()
    _require_image_budget(source, reason_prefix="krpano_asset")
    try:
        require_free_disk(
            target.parent,
            reason_prefix="krpano_import",
            expected_write_bytes=int(source.stat(follow_symlinks=False).st_size),
        )
    except TourHostSafetyError as exc:
        raise SystemExit(str(exc)) from exc
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
                        raise TourHostSafetyError("krpano_asset_too_large")
                    output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        temporary_path.chmod(0o644)
        os.replace(temporary_path, target)
        temporary_path = None
    except TourHostSafetyError as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return relpath, {
        "relpath": relpath,
        "sha256": _sha256(target),
        "size_bytes": target.stat().st_size,
    }


def _scene_cube_face_sources(payload: dict[str, object], *, bundle_dir: Path, scene_index: int) -> list[tuple[str, Path]]:
    scenes = payload.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise SystemExit("krpano_source_scene_missing")
    if scene_index < 0 or scene_index >= len(scenes):
        raise SystemExit("krpano_source_scene_index_invalid")
    scene = scenes[scene_index]
    if not isinstance(scene, dict):
        raise SystemExit("krpano_source_scene_invalid")
    cube_faces = scene.get("cube_faces")
    if not isinstance(cube_faces, dict):
        raise SystemExit("krpano_source_scene_cube_faces_missing")

    sources: list[tuple[str, Path]] = []
    for face_key in CANONICAL_CUBE_FACE_KEYS:
        relpath = _safe_relpath(str(cube_faces.get(face_key) or ""))
        if not relpath:
            raise SystemExit(f"krpano_source_scene_cube_face_missing:{face_key}")
        source = (bundle_dir / relpath).resolve()
        if bundle_dir.resolve() not in source.parents or not source.is_file():
            raise SystemExit(f"krpano_source_scene_cube_face_file_missing:{face_key}")
        sources.append((face_key, source))
    return sources


def _license_runtime_config() -> dict[str, str]:
    domain = str(os.getenv("KRPANO_LICENSE_DOMAIN") or "").strip()
    key = str(os.getenv("KRPANO_LICENSE_KEY") or "").strip()
    if not domain or not key:
        return {}
    return {"domain": domain}


def _main_unlocked() -> int:
    parser = argparse.ArgumentParser(
        description="Import one real 360 panorama or cubemap for licensed krpano control."
    )
    parser.add_argument("--slug", required=True, help="Existing PropertyQuarry public tour slug.")
    parser.add_argument("--panorama", default="", help="Readable 2:1 equirectangular panorama image.")
    parser.add_argument("--cube-face", action="append", default=[], help="Square cube-face image. Provide exactly six.")
    parser.add_argument(
        "--from-existing-scene",
        type=int,
        default=None,
        help="Import cube faces from an existing tour.json scenes[index].cube_faces entry.",
    )
    parser.add_argument("--target-subdir", default="krpano", help="Subdirectory inside the tour bundle.")
    parser.add_argument("--skip-license-env-check", action="store_true", help="Test-only escape hatch for local fixture import.")
    args = parser.parse_args()

    slug = _safe_relpath(args.slug)
    if "/" in slug or not slug:
        raise SystemExit("invalid_tour_slug")
    license_config = _license_runtime_config()
    if not license_config and not args.skip_license_env_check:
        raise SystemExit("krpano_license_environment_missing")

    panorama_path = Path(args.panorama).expanduser().resolve() if args.panorama else None
    cube_paths = [Path(value).expanduser().resolve() for value in args.cube_face or [] if str(value or "").strip()]
    if args.from_existing_scene is None and bool(panorama_path) == bool(cube_paths):
        raise SystemExit("krpano_requires_panorama_or_six_cube_faces")

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
    target_subdir = _safe_relpath(args.target_subdir or "krpano") or "krpano"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit("invalid_tour_manifest")

    scene_cube_sources: list[tuple[str, Path]] = []
    if args.from_existing_scene is not None:
        if panorama_path or cube_paths:
            raise SystemExit("krpano_existing_scene_is_exclusive")
        scene_cube_sources = _scene_cube_face_sources(
            payload,
            bundle_dir=bundle_dir,
            scene_index=int(args.from_existing_scene),
        )
        cube_paths = [source for _face_key, source in scene_cube_sources]

    imported_assets: list[dict[str, object]] = []
    walkable_scene: dict[str, object]
    if panorama_path:
        if not panorama_path.is_file():
            raise SystemExit("krpano_panorama_missing")
        _require_image_budget(panorama_path, reason_prefix="krpano_panorama")
        width, height = _validate_equirectangular(panorama_path)
        suffix = panorama_path.suffix.lower()
        relpath, metadata = _copy_asset(
            panorama_path,
            bundle_dir=bundle_dir,
            target_relpath=f"{target_subdir}/panorama{suffix}",
        )
        imported_assets.append({**metadata, "width": width, "height": height, "role": "equirectangular_panorama"})
        walkable_scene = {
            "projection": "equirectangular",
            "type": "panorama",
            "panorama_relpath": relpath,
            "width": width,
            "height": height,
        }
        scene_strategy = "single_panorama"
        creation_mode = "hosted_panorama_360"
    else:
        if len(cube_paths) != 6:
            raise SystemExit("krpano_requires_six_cube_faces")
        cube_faces: dict[str, str] = {}
        for index, source in enumerate(cube_paths, start=1):
            if not source.is_file():
                raise SystemExit(f"krpano_cube_face_missing:{index}")
            _require_image_budget(source, reason_prefix="krpano_cube_face")
            width, height = _validate_cube_face(source)
            suffix = source.suffix.lower()
            relpath, metadata = _copy_asset(
                source,
                bundle_dir=bundle_dir,
                target_relpath=f"{target_subdir}/cube-face-{index}{suffix}",
            )
            face_key = scene_cube_sources[index - 1][0] if scene_cube_sources else f"face_{index}"
            imported_assets.append({**metadata, "width": width, "height": height, "role": f"cube_face_{face_key}"})
            cube_faces[face_key] = relpath
        walkable_scene = {
            "projection": "cubemap",
            "type": "cube",
            "cube_faces": cube_faces,
        }
        scene_strategy = "single_cubemap"
        creation_mode = "hosted_cubemap_360"

    payload["control_mode"] = "krpano"
    payload["viewer_provider"] = "krpano"
    payload["scene_strategy"] = scene_strategy
    payload["creation_mode"] = creation_mode
    payload["walkable_scene"] = walkable_scene
    payload["krpano_import"] = {
        "source": "verified_single_scene_360_assets",
        "imported_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "target_subdir": target_subdir,
        "license_domain": license_config.get("domain") or "",
        "asset_count": len(imported_assets),
        "assets": imported_assets,
    }
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "imported",
                "slug": slug,
                "provider": "krpano",
                "scene_strategy": scene_strategy,
                "control_url": f"/tours/{slug}/control/krpano",
                "asset_count": len(imported_assets),
            },
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    try:
        with bounded_lane_lock("krpano-import"):
            return _main_unlocked()
    except TourHostSafetyError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
