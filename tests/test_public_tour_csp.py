from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from tests.product_test_helpers import build_product_client


def _clean_3dvista_proof(*, slug: str, provider_url: str) -> dict[str, object]:
    return {
        "three_d_vista_white_label_proof": {
            "source_project": "propertyquarry",
            "private_viewer_verified": True,
            "non_trial_export_verified": True,
            "propertyquarry_tour_metadata": True,
            "trial_branding_checked": True,
            "trial_branding_present": False,
        },
        "three_d_vista_browser_render_proof": {
            "provider": "3dvista",
            "status": "pass",
            "rendered_viewer": True,
        },
        "three_d_vista_target_provenance": {
            "schema": "propertyquarry.3dvista_target_provenance.v1",
            "status": "pass",
            "provider": "3dvista",
            "target_slug": slug,
            "artifact": {
                "kind": "hosted_url",
                "sha256": hashlib.sha256(provider_url.encode("utf-8")).hexdigest(),
            },
            "authorization": {
                "status": "approved",
                "reference": "test-fixture:licensed-3dvista",
            },
            "review": {
                "property_match": "pass",
                "visual_match": "pass",
                "reviewed_by": "propertyquarry-test-suite",
                "reviewed_at": "2026-07-19T00:00:00+00:00",
            },
        },
    }


def _write_external_3dvista_bundle(root: Path, *, slug: str) -> None:
    bundle_dir = root / slug
    bundle_dir.mkdir(parents=True)
    provider_url = "https://viewer.3dvista.com/tours/launch-ready/index.html"
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "display_title": '<img src=x onerror="window.__propertyQuarryXss=1">Injected title',
                "scenes": [
                    {
                        "name": '</script><img src=x onerror="window.__propertyQuarryXss=2">',
                        "role": "photo",
                        "image_url": "https://media.3dvista.com/public/scene.jpg",
                        "mime_type": "image/jpeg",
                    },
                    {
                        "name": "Rejected origin",
                        "role": "photo",
                        "image_url": "https://3dvista.com.evil.example/attack.jpg",
                        "mime_type": "image/jpeg",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    private_path = bundle_dir / "tour.private.json"
    private_path.write_text(
        json.dumps(
            {
                "three_d_vista_url": provider_url,
                **_clean_3dvista_proof(slug=slug, provider_url=provider_url),
            }
        ),
        encoding="utf-8",
    )
    private_path.chmod(0o600)


def _control_response(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, slug: str = "csp-external"):
    _write_external_3dvista_bundle(tmp_path, slug=slug)
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    monkeypatch.setenv("PROPERTYQUARRY_ENABLE_PUBLIC_TOURS", "1")
    return build_product_client(principal_id="public-tour-csp-test").get(f"/tours/{slug}/control/3dvista")


def test_public_tour_document_csp_uses_nonce_without_broad_execution_grants() -> None:
    from app.api.routes import public_tours

    nonce = "launchReadyNonce_1234567890"
    headers = public_tours._public_tour_security_headers(nonce=nonce)
    policy = headers["Content-Security-Policy"]

    assert f"'nonce-{nonce}'" in policy
    assert "'unsafe-inline'" not in policy
    assert "'unsafe-eval'" not in policy
    assert "'wasm-unsafe-eval'" not in policy
    assert "img-src 'self' data: blob: https:;" not in policy
    assert "media-src 'self' data: blob: https:;" not in policy
    assert "script-src-attr 'none'" in policy
    assert "style-src-attr 'none'" in policy
    assert "https://js.clickrank.ai" not in policy
    assert "https://app.rybbit.io" not in policy
    assert headers["Reporting-Endpoints"] == 'propertyquarry-csp="/tours/security/csp-report"'
    assert headers["Content-Security-Policy-Report-Only"]


def test_vendor_export_csp_confines_legacy_runtime_grants_to_vendor_profile() -> None:
    from app.api.routes import public_tours

    normal_policy = public_tours._public_tour_security_headers()["Content-Security-Policy"]
    headers = public_tours._public_tour_security_headers(runtime_profile="vendor_export")
    vendor_policy = headers["Content-Security-Policy"]
    report_only_policy = headers["Content-Security-Policy-Report-Only"]

    assert "'unsafe-inline'" not in normal_policy
    assert "'unsafe-eval'" not in normal_policy
    assert "'unsafe-inline'" in vendor_policy
    assert "'unsafe-eval'" in vendor_policy
    assert "'unsafe-inline'" in report_only_policy
    assert "script-src 'self' 'unsafe-inline'" in report_only_policy
    assert "script-src-attr 'unsafe-inline'" in report_only_policy
    assert "style-src 'self' 'unsafe-inline'" in report_only_policy
    assert "style-src-attr 'unsafe-inline'" in report_only_policy
    assert "'unsafe-eval'" not in report_only_policy
    assert "'wasm-unsafe-eval'" not in report_only_policy


def test_public_tour_origins_reject_url_parser_confusion_and_env_directive_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import public_tours

    monkeypatch.setenv(
        "PROPERTYQUARRY_PUBLIC_MEDIA_ALLOWED_HOSTS",
        "media.example,*.cdn.example,https://bad.example,evil.example;script-src *",
    )
    policy = public_tours._public_tour_security_headers()["Content-Security-Policy"]

    assert "https://media.example" in policy
    assert "https://*.cdn.example" in policy
    assert "https://bad.example" not in policy
    assert "evil.example;script-src" not in policy
    assert public_tours._public_tour_static_media_url_allowed("https://media.example/video.mp4")
    assert not public_tours._public_tour_static_media_url_allowed("http://media.example/video.mp4")
    assert not public_tours._safe_3dvista_external_url("https://3dvista.com.evil.example/tour")
    assert not public_tours._safe_3dvista_external_url("https://3dvista.com\\@evil.example/tour")
    assert not public_tours._safe_3dvista_external_url("https://user@3dvista.com/tour")
    assert not public_tours._safe_matterport_external_url("https://my.matterport.com/unsupported?m=MODEL123")
    assert (
        public_tours._safe_matterport_external_url("https://my.matterport.com/show/?m=MODEL123&mls=2&token=secret")
        == "https://my.matterport.com/show/?m=MODEL123&mls=2"
    )


def test_public_tour_control_response_binds_every_authored_block_to_header_nonce_and_drops_bad_media(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    response = _control_response(monkeypatch, tmp_path)

    assert response.status_code == 200
    policy = response.headers["content-security-policy"]
    nonce_match = re.search(r"'nonce-([A-Za-z0-9_-]+)'", policy)
    assert nonce_match is not None
    nonce = nonce_match.group(1)
    assert "'unsafe-inline'" not in policy
    assert "'unsafe-eval'" not in policy
    assert "https://3dvista.com.evil.example" not in response.text
    assert "https://viewer.3dvista.com/tours/launch-ready/index.html" in response.text
    assert "https://media.3dvista.com/public/scene.jpg" in response.text
    assert "<img src=x onerror=" not in response.text
    for tag in re.findall(r"<(?:script|style)\b[^>]*>", response.text, flags=re.IGNORECASE):
        assert f'nonce="{nonce}"' in tag


def test_primary_provider_control_iframe_is_not_blocked_by_retained_ai_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    slug = "csp-hybrid-provider"
    _write_external_3dvista_bundle(tmp_path, slug=slug)
    manifest_path = tmp_path / slug / "tour.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["walkable_scene"] = {"representation_kind": "ai_reconstruction"}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    monkeypatch.setenv("PROPERTYQUARRY_ENABLE_PUBLIC_TOURS", "1")

    response = build_product_client(
        principal_id="public-tour-csp-hybrid-test"
    ).get(f"/tours/{slug}/control")

    assert response.status_code == 200
    assert "<iframe" in response.text
    policy = response.headers["content-security-policy"]
    assert "frame-src 'none'" not in policy
    assert "frame-src 'self'" in policy


def test_public_tour_csp_report_endpoint_accepts_bounded_reports_without_reflecting_secrets(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_ENABLE_PUBLIC_TOURS", "1")
    client = build_product_client(principal_id="public-tour-csp-report")
    with caplog.at_level("WARNING"):
        response = client.post(
            "/tours/security/csp-report",
            content=json.dumps(
                {
                    "csp-report": {
                        "effective-directive": "script-src-elem",
                        "document-uri": "https://propertyquarry.com/tours/example?access_token=secret",
                        "blocked-uri": "https://evil.example/payload.js?token=secret",
                    }
                }
            ),
            headers={"content-type": "application/csp-report"},
        )

    assert response.status_code == 204
    assert "content-security-policy" in response.headers
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "script-src-elem" in log_text
    assert "access_token" not in log_text
    assert "token=secret" not in log_text


def test_public_tour_control_xss_payload_does_not_execute_in_chromium(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    response = _control_response(monkeypatch, tmp_path, slug="csp-browser")
    assert response.status_code == 200
    main_url = "https://propertyquarry.test/tours/csp-browser/control/3dvista"

    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - depends on the pinned browser image
            pytest.skip(f"pinned Chromium unavailable: {exc}")
        page = browser.new_page()

        def handle_route(route) -> None:
            if route.request.url == main_url:
                route.fulfill(
                    status=response.status_code,
                    body=response.content,
                    headers=dict(response.headers),
                )
            else:
                route.abort()

        page.route("**/*", handle_route)
        page.goto(main_url, wait_until="domcontentloaded")
        assert page.evaluate("typeof window.__propertyQuarryXss") == "undefined"
        assert "Injected title" in page.locator("h1").inner_text()
        browser.close()
