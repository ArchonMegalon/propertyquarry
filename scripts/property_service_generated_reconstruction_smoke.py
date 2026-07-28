#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.ensure_propertyquarry_render_bridge_runtime import build_render_bridge_runtime_receipt
from scripts.property_runtime_reconstruction_smoke import (
    _check_generated_reconstruction_browser_shell,
    _check_generated_reconstruction_public_contract,
    _coverage_proof_covers_walkthrough_route,
    _label_list,
    _labels_contain_keyword,
    _looks_like_generic_route_label,
    _resolved_local_public_base_url,
    _sync_container_tour_to_host_root,
)


DEFAULT_API_CONTAINER = "propertyquarry-api"
CONTRACT_NAME = "propertyquarry.service_generated_reconstruction_smoke.v1"


def _run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)


def _service_generated_reconstruction_smoke_timeout_seconds() -> int:
    raw_generation_timeout = str(os.getenv("PROPERTYQUARRY_RECONSTRUCTION_TIMEOUT_SECONDS") or "").strip()
    try:
        generation_timeout_seconds = int(float(raw_generation_timeout or "420"))
    except Exception:
        generation_timeout_seconds = 420
    raw_request_timeout = str(os.getenv("PROPERTYQUARRY_RECONSTRUCTION_REQUEST_TIMEOUT_SECONDS") or "").strip()
    try:
        request_timeout_seconds = int(float(raw_request_timeout or str(generation_timeout_seconds + 60)))
    except Exception:
        request_timeout_seconds = generation_timeout_seconds + 60
    minimum_timeout_seconds = max(360, generation_timeout_seconds + 120, request_timeout_seconds + 60)
    raw_smoke_timeout = str(os.getenv("PROPERTYQUARRY_SERVICE_GENERATED_RECONSTRUCTION_SMOKE_TIMEOUT_SECONDS") or "").strip()
    try:
        smoke_timeout_seconds = int(float(raw_smoke_timeout or str(minimum_timeout_seconds)))
    except Exception:
        smoke_timeout_seconds = minimum_timeout_seconds
    return max(smoke_timeout_seconds, minimum_timeout_seconds)


def _generated_reconstruction_canonical_url(*, public_base_url: str, slug: str) -> str:
    normalized_base = str(public_base_url or "").strip().rstrip("/")
    normalized_slug = str(slug or "").strip().strip("/")
    if not normalized_base or not normalized_slug:
        return ""
    return f"{normalized_base}/tours/{urllib.parse.quote(normalized_slug, safe='')}"


def _positive_float(value: object) -> float:
    try:
        parsed = float(value or 0.0)
    except Exception:
        return 0.0
    return parsed if parsed > 0.0 else 0.0


def _minimum_walkthrough_duration_seconds(
    *,
    route_labels: list[str],
    walkthrough_route_labels: list[str],
    generated_photo_count: int,
) -> float:
    stop_count = max(
        len([label for label in list(route_labels or []) if str(label or "").strip()]),
        len([label for label in list(walkthrough_route_labels or []) if str(label or "").strip()]),
        max(int(generated_photo_count or 0), 0),
        1,
    )
    return min(60.0, max(30.0, float(stop_count) * 6.0))


def build_service_generated_reconstruction_receipt(
    *,
    container: str,
    slug: str,
    public_base_url: str = "",
    host_header: str = "",
    require_public_contract: bool = False,
    require_browser_shell: bool = False,
    command_timeout_seconds: int | None = None,
    ensure_render_bridge_runtime: bool = True,
) -> dict[str, object]:
    started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if not shutil.which("docker"):
        return {
            "contract_name": CONTRACT_NAME,
            "status": "blocked",
            "reason": "docker_missing",
            "generated_at": started_at,
        }
    render_bridge_runtime: dict[str, object] = {"status": "skipped", "reason": "render_bridge_runtime_ensure_disabled"}
    if ensure_render_bridge_runtime:
        render_bridge_runtime = build_render_bridge_runtime_receipt(
            container=str(os.getenv("PROPERTYQUARRY_RENDER_CONTAINER_NAME") or "propertyquarry-render-tools").strip(),
            service=str(os.getenv("PROPERTYQUARRY_RENDER_SERVICE") or "propertyquarry-render-tools").strip(),
            compose_file=str(os.getenv("PROPERTYQUARRY_COMPOSE_FILE") or "docker-compose.property.yml").strip(),
            compose_project_name=(
                str(os.getenv("PROPERTYQUARRY_COMPOSE_PROJECT_NAME") or os.getenv("COMPOSE_PROJECT_NAME") or "").strip()
            ),
        )
        if render_bridge_runtime.get("status") != "pass":
            return {
                "contract_name": CONTRACT_NAME,
                "status": "blocked" if render_bridge_runtime.get("status") == "blocked" else "failed",
                "reason": "render_bridge_runtime_unavailable",
                "generated_at": started_at,
                "container": container,
                "slug": slug,
                "render_bridge_runtime": render_bridge_runtime,
            }

    setup_script = f"""
import json
import os
from pathlib import Path
import shutil
from PIL import Image, ImageDraw
from app.product import service as product_service

slug = {slug!r}
src = Path('/tmp') / f'propertyquarry-service-reconstruction-{{slug}}'
shutil.rmtree(Path('/data/public_property_tours') / slug, ignore_errors=True)
shutil.rmtree(src, ignore_errors=True)
src.mkdir(parents=True, exist_ok=True)
title = f'Runtime service reconstruction smoke {{slug}}'
listing_id = f'runtime-service-reconstruction-smoke-{{slug}}'
property_url = (
    'https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/'
    f'runtime-service-reconstruction-smoke-{{slug}}'
)
source_ref = f'willhaben:runtime-service-reconstruction-smoke:{{slug}}'
external_id = f'runtime-service-reconstruction-smoke:{{slug}}'

def write_floorplan(path: Path) -> None:
    image = Image.new('RGB', (1200, 800), color=(248, 244, 235))
    draw = ImageDraw.Draw(image)
    draw.rectangle((80, 80, 1120, 720), outline=(42, 36, 28), width=12)
    draw.line((620, 80, 620, 720), fill=(42, 36, 28), width=8)
    draw.line((80, 420, 620, 420), fill=(42, 36, 28), width=8)
    draw.line((320, 80, 320, 420), fill=(42, 36, 28), width=8)
    draw.line((620, 250, 1120, 250), fill=(42, 36, 28), width=8)
    draw.line((870, 250, 870, 720), fill=(42, 36, 28), width=8)
    image.save(path, format='JPEG')

def write_photo(path: Path, color: tuple[int, int, int]) -> None:
    image = Image.new('RGB', (900, 700), color=color)
    draw = ImageDraw.Draw(image)
    draw.rectangle((80, 100, 820, 620), outline=(255, 255, 255), width=8)
    image.save(path, format='JPEG')

floorplan = src / 'floorplan.jpg'
write_floorplan(floorplan)
photo_specs = [
    ('living.jpg', (126, 108, 82)),
    ('living-detail.jpg', (132, 118, 92)),
    ('sleeping.jpg', (98, 104, 122)),
    ('balcony.jpg', (116, 126, 98)),
    ('kitchen.jpg', (86, 104, 112)),
]
asset_map = {{
    'https://img.example.test/floorplan.jpg': floorplan,
}}
for name, color in photo_specs:
    path = src / name
    write_photo(path, color)
    asset_map[f'https://img.example.test/{{name}}'] = path

os.environ['EA_PUBLIC_TOUR_DIR'] = '/data/public_property_tours'
os.environ['PROPERTYQUARRY_RECONSTRUCTION_WALKTHROUGH_SECONDS_PER_STOP'] = '8'
product_service._download_property_reconstruction_image = lambda url, target_dir, *, stem: asset_map.get(str(url or '').strip())
materialized_slug = product_service._make_hosted_property_tour_slug(
    title=title,
    listing_id=listing_id,
    property_url=property_url,
    variant_key='layout_first',
)
shutil.rmtree(Path('/data/public_property_tours') / materialized_slug, ignore_errors=True)

payload = product_service._write_generated_reconstruction_property_tour_bundle(
    principal_id='property-tour-service-smoke',
    title=title,
    listing_id=listing_id,
    property_url=property_url,
    variant_key='layout_first',
    media_urls=[
        'https://img.example.test/living.jpg',
        'https://img.example.test/living-detail.jpg',
        'https://img.example.test/sleeping.jpg',
        'https://img.example.test/balcony.jpg',
        'https://img.example.test/kitchen.jpg',
    ],
    floorplan_urls=['https://img.example.test/floorplan.jpg'],
    property_facts_json={{
        'rooms': 4,
        'description': 'Maisonette mit Balkon und separater Kueche.',
        'has_floorplan': True,
        'has_balcony': True,
        'has_terrace': True,
    }},
    source_host='www.willhaben.at',
    source_ref=source_ref,
    external_id=external_id,
    recipient_email='owner@example.test',
    diorama_style_hint='Ikea',
)
tour_url = f"https://propertyquarry.com/tours/{{payload['slug']}}"
delivery = product_service._hosted_property_tour_video_delivery(tour_url)
context = product_service._property_walkthrough_scene_video_context(tour_url)
bundle_dir = Path('/data/public_property_tours') / payload['slug']
generated = dict(payload.get('generated_reconstruction') or {{}})
paths = {{
    'viewer': bundle_dir / 'generated-reconstruction' / 'viewer.html',
    'obj': bundle_dir / 'generated-reconstruction' / 'model.obj',
    'mtl': bundle_dir / 'generated-reconstruction' / 'model.mtl',
    'glb': bundle_dir / 'generated-reconstruction' / 'model.glb',
    'receipt': bundle_dir / 'generated-reconstruction' / 'reconstruction.json',
    'walkthrough_video': bundle_dir / 'generated-reconstruction' / 'generated-walkthrough.mp4',
    'walkthrough_sidecar': bundle_dir / 'generated-reconstruction' / 'generated-walkthrough.quality.json',
}}
print(json.dumps({{
    'slug': payload.get('slug'),
    'tour_url': tour_url,
    'top_level_video_relpath': payload.get('video_relpath'),
    'top_level_video_provider': payload.get('video_provider'),
    'top_level_video_provider_key': payload.get('video_provider_key'),
    'top_level_video_coverage_proof': payload.get('video_coverage_proof'),
    'top_level_video_sidecar_relpath': payload.get('video_sidecar_relpath'),
    'generated_route_labels': list(generated.get('route_labels') or []),
    'generated_walkthrough_route_labels': list(generated.get('walkthrough_route_labels') or []),
    'generated_room_stop_count': generated.get('room_stop_count'),
    'generated_walkthrough_stop_count': generated.get('walkthrough_stop_count'),
    'generated_photo_count': len(list(generated.get('photo_relpaths') or [])),
    'generated_walkthrough_video_relpath': generated.get('walkthrough_video_relpath'),
    'generated_walkthrough_sidecar_relpath': generated.get('walkthrough_sidecar_relpath'),
    'generated_walkthrough_coverage_proof': generated.get('walkthrough_coverage_proof') or {{}},
    'walkable_scene_route_labels': [row.get('label') for row in list((generated.get('walkable_scene') or {{}}).get('route') or []) if isinstance(row, dict)],
    'video_delivery': delivery,
    'context_route_labels': list(context.get('route_labels') or []),
    'walkthrough_asset_url': product_service._hosted_property_tour_walkthrough_asset_url(tour_url),
    'paths': {{key: {{'exists': value.is_file(), 'size_bytes': value.stat().st_size if value.exists() else 0}} for key, value in paths.items()}},
}}, sort_keys=True))
"""
    explicit_command_timeout_seconds = max(int(command_timeout_seconds or 0), 0)
    resolved_command_timeout_seconds = (
        max(120, explicit_command_timeout_seconds)
        if explicit_command_timeout_seconds
        else _service_generated_reconstruction_smoke_timeout_seconds()
    )
    try:
        generated = _run(
            ["docker", "exec", container, "python", "-c", setup_script],
            timeout=resolved_command_timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "contract_name": CONTRACT_NAME,
            "status": "failed",
            "generated_at": started_at,
            "container": container,
            "slug": slug,
            "render_bridge_runtime": render_bridge_runtime,
            "reason": "service_generated_reconstruction_command_timeout",
            "timeout_seconds": resolved_command_timeout_seconds,
            "error": str(exc),
        }
    if generated.returncode != 0:
        return {
            "contract_name": CONTRACT_NAME,
            "status": "failed",
            "generated_at": started_at,
            "container": container,
            "slug": slug,
            "render_bridge_runtime": render_bridge_runtime,
            "reason": "service_generated_reconstruction_command_failed",
            "returncode": generated.returncode,
            "stdout_tail": (generated.stdout or "")[-1000:],
            "stderr_tail": (generated.stderr or "")[-1000:],
        }

    try:
        details = json.loads((generated.stdout or "").strip().splitlines()[-1])
    except Exception as exc:
        return {
            "contract_name": CONTRACT_NAME,
            "status": "failed",
            "generated_at": started_at,
            "container": container,
            "slug": slug,
            "render_bridge_runtime": render_bridge_runtime,
            "reason": "service_generated_reconstruction_unparseable",
            "error": type(exc).__name__,
            "stdout_tail": (generated.stdout or "")[-1000:],
        }

    generated_slug = str(details.get("slug") or slug).strip()
    paths = details.get("paths") if isinstance(details.get("paths"), dict) else {}
    route_labels = _label_list(details.get("generated_route_labels"))
    walkthrough_route_labels = _label_list(details.get("generated_walkthrough_route_labels"))
    walkable_scene_route_labels = _label_list(details.get("walkable_scene_route_labels"))
    context_route_labels = _label_list(details.get("context_route_labels"))
    video_delivery = dict(details.get("video_delivery") or {}) if isinstance(details.get("video_delivery"), dict) else {}
    delivery_route_labels = _label_list(video_delivery.get("covered_route_labels"))
    generated_coverage = (
        dict(details.get("generated_walkthrough_coverage_proof") or {})
        if isinstance(details.get("generated_walkthrough_coverage_proof"), dict)
        else {}
    )
    generated_photo_count = max(0, int(details.get("generated_photo_count") or 0))
    detail_required = generated_photo_count > len(route_labels)
    minimum_walkthrough_duration_seconds = _minimum_walkthrough_duration_seconds(
        route_labels=route_labels,
        walkthrough_route_labels=walkthrough_route_labels,
        generated_photo_count=generated_photo_count,
    )

    required_paths_ok = all(
        bool((paths.get(key) or {}).get("exists"))
        for key in ("viewer", "obj", "mtl", "receipt", "walkthrough_video", "walkthrough_sidecar")
    )
    top_level_video_contract_ok = (
        str(details.get("top_level_video_relpath") or "").strip() == "generated-reconstruction/generated-walkthrough.mp4"
        and str(details.get("top_level_video_provider") or "").strip() == "propertyquarry_generated_reconstruction"
        and str(details.get("top_level_video_provider_key") or "").strip() == "propertyquarry_generated_reconstruction"
        and str(details.get("top_level_video_coverage_proof") or "").strip() == "boundary_verified_frame_continuation"
        and str(details.get("top_level_video_sidecar_relpath") or "").strip() == "generated-reconstruction/generated-walkthrough.quality.json"
    )
    route_label_quality_ok = (
        len(route_labels) >= 4
        and not any(_looks_like_generic_route_label(label) for label in route_labels)
        and (
            _labels_contain_keyword(route_labels, r"\b(entry|hall|foyer|vorraum|flur)\b")
            or _labels_contain_keyword(route_labels, r"\b(stair(?:case)?|treppe)\b")
        )
        and _labels_contain_keyword(route_labels, r"\b(living|wohn)\b")
        and _labels_contain_keyword(route_labels, r"\b(sleep(?:ing)?|bedroom|schlaf|stair|treppe|balcony|terrace|loggia|balkon|terrasse)\b")
        and walkable_scene_route_labels == route_labels
        and context_route_labels == route_labels
        and int(details.get("generated_room_stop_count") or 0) == len(route_labels)
    )
    walkthrough_generated_ok = (
        len(walkthrough_route_labels) >= max(len(route_labels), generated_photo_count)
        and not any(_looks_like_generic_route_label(label) for label in walkthrough_route_labels)
        and (not detail_required or any("detail" in label.lower() for label in walkthrough_route_labels))
        and int(details.get("generated_walkthrough_stop_count") or 0) == len(walkthrough_route_labels)
        and _coverage_proof_covers_walkthrough_route(generated_coverage, walkthrough_route_labels)
        and str(details.get("generated_walkthrough_video_relpath") or "").strip() == "generated-reconstruction/generated-walkthrough.mp4"
        and str(details.get("generated_walkthrough_sidecar_relpath") or "").strip() == "generated-reconstruction/generated-walkthrough.quality.json"
    )
    delivery_contract_ok = (
        str(details.get("walkthrough_asset_url") or "").strip().endswith(
            f"/tours/files/{urllib.parse.quote(generated_slug, safe='')}/generated-reconstruction/generated-walkthrough.mp4"
        )
        and str(video_delivery.get("video_url") or "").strip().endswith(
            f"/tours/files/{urllib.parse.quote(generated_slug, safe='')}/generated-reconstruction/generated-walkthrough.mp4"
        )
        and str(video_delivery.get("provider_key") or "").strip() == "propertyquarry_generated_reconstruction"
        and _positive_float(video_delivery.get("duration_seconds")) >= minimum_walkthrough_duration_seconds
        and str(video_delivery.get("coverage_proof") or "").strip() == "boundary_verified_frame_continuation"
        and delivery_route_labels == walkthrough_route_labels
    )
    resolved_public_base_url = _resolved_local_public_base_url(
        public_base_url,
        public_container=str(os.getenv("PROPERTYQUARRY_API_CONTAINER_NAME") or DEFAULT_API_CONTAINER).strip(),
    )
    host_public_tour_sync: dict[str, object] = {"status": "skipped", "reason": "public_base_url_missing"}
    if resolved_public_base_url and (require_public_contract or require_browser_shell):
        host_public_tour_sync = _sync_container_tour_to_host_root(
            container,
            slug=generated_slug,
            public_base_url=resolved_public_base_url,
        )
    public_contract_receipt: dict[str, object] = {}
    public_contract_ok = True
    if resolved_public_base_url:
        public_contract_receipt = _check_generated_reconstruction_public_contract(
            public_base_url=resolved_public_base_url,
            slug=generated_slug,
            host_header=host_header,
        )
        public_contract_ok = public_contract_receipt.get("status") == "pass"
    elif require_public_contract:
        public_contract_ok = False
        public_contract_receipt = {"status": "blocked", "reason": "public_base_url_missing"}
    browser_shell_receipt: dict[str, object] = {}
    browser_shell_ok = True
    if require_browser_shell:
        if resolved_public_base_url:
            browser_shell_receipt = _check_generated_reconstruction_browser_shell(
                public_base_url=resolved_public_base_url,
                slug=generated_slug,
                host_header=host_header,
                expected_route_stop_count=len(route_labels),
                expected_photo_count=generated_photo_count,
                expected_route_labels=route_labels,
            )
            browser_shell_ok = browser_shell_receipt.get("status") == "pass"
        else:
            browser_shell_ok = False
            browser_shell_receipt = {"status": "blocked", "reason": "public_base_url_missing"}

    status = (
        "pass"
        if (
            required_paths_ok
            and top_level_video_contract_ok
            and route_label_quality_ok
            and walkthrough_generated_ok
            and delivery_contract_ok
            and public_contract_ok
            and browser_shell_ok
        )
        else "failed"
    )
    return {
        "contract_name": CONTRACT_NAME,
        "status": status,
        "generated_at": started_at,
        "container": container,
        "slug": generated_slug,
        "render_bridge_runtime": render_bridge_runtime,
        "tour_url": str(details.get("tour_url") or ""),
        "viewer_url": _generated_reconstruction_canonical_url(
            public_base_url=resolved_public_base_url or public_base_url,
            slug=generated_slug,
        ),
        "resolved_public_base_url": resolved_public_base_url,
        "host_public_tour_sync": host_public_tour_sync,
        "required_paths_ok": required_paths_ok,
        "top_level_video_contract_ok": top_level_video_contract_ok,
        "route_label_quality_ok": route_label_quality_ok,
        "walkthrough_generated_ok": walkthrough_generated_ok,
        "delivery_contract_ok": delivery_contract_ok,
        "minimum_walkthrough_duration_seconds": minimum_walkthrough_duration_seconds,
        "public_route_contract_ok": public_contract_ok,
        "browser_shell_ok": browser_shell_ok,
        "public_route_contract": public_contract_receipt,
        "browser_shell": browser_shell_receipt,
        "details": details,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke the service-owned PropertyQuarry generated reconstruction bundle writer.")
    parser.add_argument("--container", default=DEFAULT_API_CONTAINER)
    parser.add_argument("--slug", default="runtime-service-generated-reconstruction-smoke")
    parser.add_argument("--public-base-url", default="")
    parser.add_argument("--host-header", default=os.getenv("PROPERTYQUARRY_LIVE_HOST_HEADER") or "propertyquarry.com")
    parser.add_argument("--require-public-contract", action="store_true")
    parser.add_argument("--require-browser-shell", action="store_true")
    parser.add_argument("--command-timeout-seconds", type=int, default=0)
    parser.add_argument(
        "--skip-render-bridge-runtime-ensure",
        action="store_true",
        help="Skip the compose/runtime bootstrap that ensures the render bridge container exists before the smoke runs.",
    )
    parser.add_argument("--write", default="_completion/tours/property-service-generated-reconstruction-smoke-current.json")
    parser.add_argument("--fail-on-error", action="store_true")
    args = parser.parse_args()

    receipt = build_service_generated_reconstruction_receipt(
        container=args.container,
        slug=args.slug,
        public_base_url=args.public_base_url,
        host_header=str(args.host_header or "").strip(),
        require_public_contract=bool(args.require_public_contract),
        require_browser_shell=bool(args.require_browser_shell),
        command_timeout_seconds=int(args.command_timeout_seconds or 0),
        ensure_render_bridge_runtime=not bool(args.skip_render_bridge_runtime_ensure),
    )
    output = json.dumps(receipt, indent=2, sort_keys=True)
    if args.write:
        out_path = Path(args.write)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output + "\n", encoding="utf-8")
    print(output)
    if args.fail_on_error and receipt.get("status") != "pass":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
