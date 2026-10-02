#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EA_ROOT = ROOT / "ea"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(EA_ROOT) not in sys.path:
    sys.path.insert(0, str(EA_ROOT))

from scripts.propertyquarry_live_mobile_surface_smoke import seed_research_detail_fixture
from scripts.propertyquarry_live_http_security import (
    headers_for_authorized_origin,
    normalized_origin,
    redact_secret_values,
    url_matches_origin,
    validated_live_base_origin,
)


DEFAULT_DEMO_SLUG = "luxury-residence-with-breathtaking-skyline-views-danubeflats-vienna-layout-first-742df65557"
DEFAULT_PROVIDER_RECEIPT = "_completion/smoke/property-live-provider-latest.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _visible_text(text: str) -> str:
    without_hidden = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    without_tags = re.sub(r"<[^>]+>", " ", without_hidden)
    return re.sub(r"\s+", " ", without_tags).strip()


def _fetch(
    url: str,
    *,
    timeout_seconds: float,
    host_header: str = "",
    api_token: str = "",
    principal_id: str = "",
    method: str = "GET",
    authorized_origin: str = "",
) -> dict[str, Any]:
    headers = {
        "User-Agent": "PropertyQuarry-live-presentation-e2e/1.0",
        "Accept": "text/html,application/json,video/*,*/*",
    }
    if host_header:
        headers["Host"] = host_header
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"
        headers["X-EA-API-Token"] = api_token
    if principal_id:
        headers["X-EA-Principal-ID"] = principal_id
    if api_token and not authorized_origin:
        return {
            "status_code": 0,
            "final_url": url,
            "headers": {},
            "body_byte_count": 0,
            "body": "",
            "error": "authorized_origin_required_for_api_token",
        }
    scoped_origin = normalized_origin(authorized_origin or url)

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, response_headers, newurl):  # noqa: ANN001
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    current_url = str(url or "").strip()
    for _redirect_count in range(6):
        request = urllib.request.Request(
            current_url,
            headers=headers_for_authorized_origin(
                url=current_url,
                authorized_origin=scoped_origin,
                headers=headers,
            ),
            method=method,
        )
        try:
            with opener.open(request, timeout=timeout_seconds) as response:
                body = b"" if method == "HEAD" else response.read(1_500_000)
                return {
                    "status_code": int(response.status),
                    "final_url": str(response.geturl()),
                    "headers": dict(response.headers.items()),
                    "body_byte_count": len(body),
                    "body": body.decode("utf-8", errors="replace"),
                }
        except urllib.error.HTTPError as exc:
            body = b"" if method == "HEAD" else exc.read(1_500_000)
            result = {
                "status_code": int(exc.code),
                "final_url": str(exc.geturl()),
                "headers": dict(exc.headers.items()),
                "body_byte_count": len(body),
                "body": body.decode("utf-8", errors="replace"),
                "error": str(exc),
            }
            location = _header(dict(exc.headers.items()), "Location")
            if int(exc.code or 0) not in {301, 302, 303, 307, 308} or not location:
                return result
            next_url = urllib.parse.urljoin(current_url, location)
            if not url_matches_origin(next_url, scoped_origin):
                result["redirect_blocked"] = "cross_origin"
                result["redirect_location"] = next_url
                return result
            current_url = next_url
            if int(exc.code or 0) == 303:
                method = "GET"
        except Exception as exc:
            return {
                "status_code": 0,
                "final_url": current_url,
                "headers": {},
                "body_byte_count": 0,
                "body": "",
                "error": f"{type(exc).__name__}: {exc}",
            }
    return {
        "status_code": 0,
        "final_url": current_url,
        "headers": {},
        "body_byte_count": 0,
        "body": "",
        "error": "same_origin_redirect_limit_exceeded",
    }


def _header(headers: dict[str, object], name: str) -> str:
    normalized = name.lower()
    for key, value in headers.items():
        if str(key).lower() == normalized:
            return str(value or "").strip()
    return ""


def _load_json(path: str) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _check(name: str, ok: bool, **extra: object) -> dict[str, object]:
    return {"name": name, "ok": bool(ok), **extra}


def _has_public_control_link(body: str, demo_path: str, provider: str) -> bool:
    return f"{demo_path}/control/{provider}" in body and "Open 3D tour" in body


def _has_walkthrough_chip_link(body: str, slug: str) -> bool:
    normalized_slug = urllib.parse.quote(str(slug or "").strip(), safe="")
    if not normalized_slug:
        return False
    candidates = (
        f"/tours/files/{normalized_slug}/",
        f"/tours/{normalized_slug}?pane=flythrough-pane",
        f"/tours/{normalized_slug}/walkthrough",
    )
    return any(candidate in body for candidate in candidates)


def _final_path(value: object) -> str:
    parsed = urllib.parse.urlparse(str(value or "").strip())
    return parsed.path


def build_live_presentation_e2e_receipt(
    *,
    base_url: str,
    host_header: str,
    api_token: str,
    principal_id: str,
    provider_receipt_path: str,
    require_provider_matrix: bool,
    demo_slug: str,
    timeout_seconds: float,
    seed_research_detail: bool,
    research_detail_route: str = "",
) -> dict[str, Any]:
    base = str(base_url or "http://localhost:8097").strip().rstrip("/")
    try:
        authorized_origin = validated_live_base_origin(base)
    except ValueError as exc:
        return {
            "contract_name": "propertyquarry.live_presentation_e2e.v1",
            "generated_at": _utc_now(),
            "status": "blocked",
            "base_url": base,
            "failed_count": 1,
            "checks": [_check("live_base_origin_safe", False, reason=str(exc))],
        }
    slug = str(demo_slug or DEFAULT_DEMO_SLUG).strip()
    checks: list[dict[str, object]] = []

    provider_receipt = _load_json(provider_receipt_path)
    provider_summary = provider_receipt.get("targeted_search_matrix_summary")
    provider_summary = dict(provider_summary) if isinstance(provider_summary, dict) else {}
    provider_matrix_ok = (
        provider_receipt.get("status") == "pass"
        and provider_receipt.get("targeted_search_matrix_executed") is True
        and provider_receipt.get("targeted_search_matrix_status") == "pass"
    )
    checks.append(
        _check(
            "search_provider_matrix_executed",
            provider_matrix_ok if require_provider_matrix else True,
            required=require_provider_matrix,
            observed_status=provider_receipt.get("status"),
            receipt_path=provider_receipt_path,
            count=provider_receipt.get("targeted_search_matrix_count"),
            status_counts=provider_summary.get("status_counts"),
        )
    )

    home = _fetch(
        f"{base}/?home=1",
        timeout_seconds=timeout_seconds,
        host_header=host_header,
    )
    home_body = str(home.get("body") or "")
    checks.extend(
        [
            _check("hero_route_ok", int(home.get("status_code") or 0) == 200, status_code=home.get("status_code")),
            _check("hero_demo_listing_visible", "Danube Flats demo" in home_body),
            _check("hero_demo_link_points_to_example", 'href="/app/example/shortlist?candidate=danube-flats-demo#danube-flats-demo"' in home_body),
            _check("hero_3d_tour_chip_visible", "3D tour available" in home_body and f"/tours/{slug}/control/" in home_body),
            _check("hero_walkthrough_chip_visible", "Walkthrough available" in home_body and _has_walkthrough_chip_link(home_body, slug)),
        ]
    )

    demo_path = f"/tours/{urllib.parse.quote(slug, safe='')}"
    demo = _fetch(f"{base}{demo_path}", timeout_seconds=timeout_seconds, host_header=host_header)
    demo_body = str(demo.get("body") or "")
    demo_final_path = _final_path(demo.get("final_url"))
    demo_opened_primary_control = demo_final_path == f"{demo_path}/control/3dvista"
    checks.extend(
        [
            _check("demo_tour_route_ok", int(demo.get("status_code") or 0) == 200, status_code=demo.get("status_code")),
            _check(
                "demo_tour_opens_primary_control_directly",
                demo_opened_primary_control,
                final_path=demo_final_path,
                expected_path=f"{demo_path}/control/3dvista",
            ),
            _check(
                "demo_tour_is_propertyquarry_presentation",
                "3D Tour" in demo_body
                and "provider-frame" in demo_body
                and f"/tours/3dvista/{slug}/3dvista/index.htm" in demo_body,
            ),
            _check(
                "demo_tour_has_verified_3dvista_control",
                demo_opened_primary_control and "provider-frame" in demo_body and "Load 3D tour" not in demo_body,
            ),
            _check(
                "demo_tour_hides_retired_matterport",
                f"{demo_path}/control/matterport" not in demo_body and "my.matterport.com" not in demo_body,
            ),
            _check("demo_tour_hides_panorama_export", f"{demo_path}/control/pano2vr" not in demo_body),
            _check(
                "demo_tour_has_walkthrough",
                f'href="/tours/{slug}/walkthrough"' in demo_body and "Open walkthrough" in demo_body,
            ),
            _check(
                "demo_tour_no_generated_cube_fallback",
                "pure_360_cube" not in demo_body and "generated 3d cube fallback" not in _visible_text(demo_body).lower(),
            ),
        ]
    )

    matterport_route = f"{demo_path}/control/matterport"
    matterport_control = _fetch(
        f"{base}{matterport_route}",
        timeout_seconds=timeout_seconds,
        host_header=host_header,
    )
    matterport_body = str(matterport_control.get("body") or "")
    checks.append(
        _check(
            "matterport_control_retired",
            int(matterport_control.get("status_code") or 0) == 404
            and "provider-frame" not in matterport_body
            and "my.matterport.com" not in matterport_body,
            route=matterport_route,
            status_code=matterport_control.get("status_code"),
        )
    )
    three_d_vista_route = f"{demo_path}/control/3dvista"
    three_d_vista_control = _fetch(
        f"{base}{three_d_vista_route}",
        timeout_seconds=timeout_seconds,
        host_header=host_header,
    )
    three_d_vista_body = str(three_d_vista_control.get("body") or "")
    checks.append(
        _check(
            "3dvista_control_route_ok",
            int(three_d_vista_control.get("status_code") or 0) == 200
            and "provider-frame" in three_d_vista_body
            and f"/tours/3dvista/{slug}/3dvista/index.htm" in three_d_vista_body
            and "Load 3D tour" not in three_d_vista_body,
            route=three_d_vista_route,
            status_code=three_d_vista_control.get("status_code"),
        )
    )

    walkthrough_path = f"/tours/files/{urllib.parse.quote(slug, safe='')}/magicfit-walkthrough.mp4"
    video = _fetch(f"{base}{walkthrough_path}", timeout_seconds=timeout_seconds, host_header=host_header, method="HEAD")
    content_type = _header(dict(video.get("headers") or {}), "Content-Type").lower()
    content_length = _header(dict(video.get("headers") or {}), "Content-Length")
    try:
        video_bytes = int(content_length or "0")
    except Exception:
        video_bytes = 0
    if int(video.get("status_code") or 0) == 200 and video_bytes <= 0:
        video_get = _fetch(f"{base}{walkthrough_path}", timeout_seconds=timeout_seconds, host_header=host_header)
        get_content_type = _header(dict(video_get.get("headers") or {}), "Content-Type").lower()
        if get_content_type:
            content_type = get_content_type
        get_content_length = _header(dict(video_get.get("headers") or {}), "Content-Length")
        try:
            video_bytes = int(get_content_length or "0")
        except Exception:
            video_bytes = 0
        if video_bytes <= 0:
            video_bytes = int(video_get.get("body_byte_count") or 0)
    checks.extend(
        [
            _check("walkthrough_route_ok", int(video.get("status_code") or 0) == 200, status_code=video.get("status_code")),
            _check("walkthrough_is_video", content_type.startswith("video/"), content_type=content_type),
            _check("walkthrough_not_stub", video_bytes > 1_000_000, content_length=video_bytes),
        ]
    )

    seeded_route = ""
    detail_route = str(research_detail_route or "").strip()
    if seed_research_detail:
        try:
            seeded_route = seed_research_detail_fixture(
                base_url=base,
                api_token=api_token,
                principal_id=principal_id,
                host_header=host_header,
            )
        except Exception as exc:
            checks.append(
                _check(
                    "app_research_detail_seeded",
                    False,
                    error=redact_secret_values(
                        f"{type(exc).__name__}: {exc}",
                        secrets=(api_token,),
                    ),
                )
            )
    if seeded_route:
        detail_route = seeded_route
        checks.append(_check("app_research_detail_seeded", True, route="/app/research/[redacted]"))
    if detail_route:
        detail = _fetch(
            f"{base}{detail_route}",
            timeout_seconds=timeout_seconds,
            host_header=host_header,
            api_token=api_token,
            principal_id=principal_id,
            authorized_origin=authorized_origin,
        )
        detail_body = str(detail.get("body") or "")
        checks.extend(
            [
                _check("app_research_detail_route_ok", int(detail.get("status_code") or 0) == 200, status_code=detail.get("status_code")),
                _check("app_research_detail_visual_controls", 'data-pw-visual-request="tour"' in detail_body and 'data-pw-visual-request="flythrough"' in detail_body),
                _check(
                    "app_research_detail_walkthrough_provider_bound",
                    'data-pw-walkthrough-provider="default"' in detail_body
                    or 'data-pw-walkthrough-provider="magicfit"' in detail_body,
                ),
            ]
        )
    else:
        checks.append(_check("app_research_detail_route_configured", False))

    failed = [row for row in checks if not row.get("ok")]
    return {
        "contract_name": "propertyquarry.live_presentation_e2e.v1",
        "generated_at": _utc_now(),
        "status": "pass" if not failed else "fail",
        "base_url": base,
        "host_header": host_header,
        "principal_id": principal_id,
        "demo_slug": slug,
        "demo_url": f"https://propertyquarry.com{demo_path}",
        "seeded_research_detail_route": "/app/research/[redacted]" if seeded_route else "",
        "research_detail_route_configured": bool(detail_route),
        "provider_receipt_path": provider_receipt_path,
        "check_count": len(checks),
        "failed_count": len(failed),
        "checks": checks,
        "notes": [
            "This receipt composes live presentation and media probes.",
            "When --require-provider-matrix is set, it also fails unless the latest provider matrix is an executed pass.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the live PropertyQuarry presentation E2E receipt.")
    parser.add_argument("--base-url", default=os.getenv("PROPERTYQUARRY_LIVE_BASE_URL", "http://localhost:8097"))
    parser.add_argument("--host-header", default=os.getenv("PROPERTYQUARRY_LIVE_HOST_HEADER", "propertyquarry.com"))
    parser.add_argument("--api-token", default=os.getenv("EA_API_TOKEN", ""))
    parser.add_argument("--principal-id", default=os.getenv("EA_PRINCIPAL_ID", "pq-live-presentation-e2e"))
    parser.add_argument("--provider-receipt", default=DEFAULT_PROVIDER_RECEIPT)
    parser.add_argument("--require-provider-matrix", action="store_true")
    parser.add_argument("--demo-slug", default=DEFAULT_DEMO_SLUG)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument(
        "--research-detail-route",
        default=os.getenv("PROPERTYQUARRY_LIVE_RESEARCH_DETAIL_ROUTE", ""),
    )
    parser.add_argument("--seed-research-detail", action="store_true")
    parser.add_argument("--no-seed-research-detail", action="store_true")
    parser.add_argument("--write", default="_completion/smoke/property-live-presentation-e2e-latest.json")
    args = parser.parse_args()

    receipt = build_live_presentation_e2e_receipt(
        base_url=args.base_url,
        host_header=args.host_header,
        api_token=args.api_token,
        principal_id=args.principal_id,
        provider_receipt_path=args.provider_receipt,
        require_provider_matrix=args.require_provider_matrix,
        demo_slug=args.demo_slug,
        timeout_seconds=max(1.0, float(args.timeout_seconds or 20.0)),
        seed_research_detail=bool(args.seed_research_detail) and not bool(args.no_seed_research_detail),
        research_detail_route=str(args.research_detail_route or ""),
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
