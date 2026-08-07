from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]


def _client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("EA_STORAGE_BACKEND", "memory")
    monkeypatch.delenv("EA_LEDGER_BACKEND", raising=False)
    monkeypatch.setenv("EA_API_TOKEN", "")
    monkeypatch.setenv("EA_RUNTIME_MODE", "dev")
    monkeypatch.setenv("PROPERTYQUARRY_RUNTIME_PROFILE", "propertyquarry")
    monkeypatch.setenv("EA_DATABASE_URL", "")
    from app.api.app import create_app

    return TestClient(create_app(), base_url="https://propertyquarry.com")


def test_mobile_runtime_contract_is_bounded_server_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _client(monkeypatch).get("/mobile/runtime-contract")

    assert response.status_code == 200
    assert len(response.content) < 8192
    assert response.json() == {
        "status": "ok",
        "contract_version": "1",
        "app_id": "com.myexternalbrain.propertyquarry",
        "public_origin": "https://propertyquarry.com",
        "minimum_android_build": 1,
        "start_path": "/app/search",
        "external_auth_path": "/sign-in/google",
        "mobile_auth_return_to": "/mobile/auth/complete",
        "mobile_auth_redeem_path": "/mobile/auth/redeem",
        "share_import_path": "/app/api/mobile/property-links",
        "app_link_paths": ["/app", "/app/*", "/shortlist", "/shortlist/*"],
        "app_links_ready": False,
        "app_links_ready_by_app_id": {
            "com.myexternalbrain.propertyquarry": False,
            "com.myexternalbrain.propertyquarry.preview": False,
        },
        "walkthrough_default": "camera",
        "spatial_tour_providers": ["3dvista", "matterport"],
        "vr_mode": "optional",
    }


def test_assetlinks_are_empty_until_a_release_certificate_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch)
    assert client.get("/.well-known/assetlinks.json").json() == []

    fingerprint = ":".join(["AB"] * 32)
    monkeypatch.setenv(
        "PROPERTYQUARRY_ANDROID_APP_LINK_SHA256_CERTS",
        fingerprint,
    )
    assetlinks = client.get("/.well-known/assetlinks.json")
    runtime = client.get("/mobile/runtime-contract")

    assert assetlinks.status_code == 200
    assert assetlinks.json() == [
        {
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {
                "namespace": "android_app",
                "package_name": "com.myexternalbrain.propertyquarry",
                "sha256_cert_fingerprints": [fingerprint],
            },
        }
    ]
    assert runtime.json()["app_links_ready"] is True
    assert runtime.json()["app_links_ready_by_app_id"] == {
        "com.myexternalbrain.propertyquarry": True,
        "com.myexternalbrain.propertyquarry.preview": False,
    }

    monkeypatch.setenv(
        "PROPERTYQUARRY_ANDROID_PREVIEW_APP_LINK_SHA256_CERTS",
        fingerprint,
    )
    preview_runtime = client.get("/mobile/runtime-contract")
    assert preview_runtime.json()["app_links_ready_by_app_id"] == {
        "com.myexternalbrain.propertyquarry": True,
        "com.myexternalbrain.propertyquarry.preview": True,
    }


def test_mobile_bridge_gets_are_side_effect_free_and_posts_mutations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch)
    auth = client.get("/mobile/auth/bridge")
    share = client.get("/mobile/share/bridge")
    script = client.get("/mobile/bridge.js")
    styles = client.get("/mobile/bridge.css")
    auth_de = client.get(
        "/mobile/auth/bridge",
        headers={"accept-language": "de-AT,de;q=0.9,en;q=0.7"},
    )
    share_es = client.get(
        "/mobile/share/bridge",
        headers={"accept-language": "es-CR,es;q=0.9,en;q=0.7"},
    )

    assert auth.status_code == share.status_code == script.status_code == styles.status_code == 200
    assert "default-src 'none'" in auth.headers["content-security-policy"]
    assert "data-mobile-bridge=\"auth\"" in auth.text
    assert "data-mobile-bridge=\"share\"" in share.text
    assert 'aria-live="polite"' in auth.text
    assert 'aria-busy="true"' in auth.text
    assert 'class="steps"' in auth.text
    assert '/mobile/bridge.css?v=3' in auth.text
    assert "Your Google password never enters PropertyQuarry." in auth.text
    assert '<html lang="de">' in auth_de.text
    assert "Sichere Anmeldung abschließen" in auth_de.text
    assert "Ihr Google-Passwort wird niemals an PropertyQuarry übertragen." in auth_de.text
    assert '<html lang="es">' in share_es.text
    assert "Agregando propiedad" in share_es.text
    assert "Solo se importa el enlace del anuncio que usted aprobó." in share_es.text
    assert "fetchWithTimeout('/mobile/auth/redeem'" in script.text
    assert "fetchWithTimeout('/app/api/mobile/property-links'" in script.text
    assert "method: 'POST'" in script.text
    assert "location.search" not in script.text
    assert "const withTimeout" in script.text
    assert "window.setTimeout(run, 350)" in script.text
    assert "The app is still waking up. Try again to finish sign-in." in script.text
    assert "The app is still waking up. Try again to add the property." in script.text
    assert "await withTimeout(\n      native.clearPendingAuth()" in script.text
    assert "await withTimeout(\n      native.clearPendingShare()" in script.text
    assert "const getNative" in script.text
    assert "const isAppShell" in script.text
    assert "retry.hidden = !insideApp" in script.text
    assert "const setProgress" in script.text
    assert "const setComplete" in script.text
    assert "const setFailure" in script.text
    assert script.headers["content-type"] == "application/javascript; charset=utf-8"
    assert "prefers-reduced-motion:reduce" in styles.text
    assert "forced-colors:active" in styles.text
    assert ".steps li:not(:last-child)::after" in styles.text
    assert "clip:rect(0,0,0,0)" in styles.text
    assert "main::after" not in styles.text
    assert 'body[data-state="failed"]' in styles.text
    assert styles.headers["content-type"] == "text/css; charset=utf-8"


def test_mobile_share_import_requires_auth_and_explicit_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.dependencies import RequestContext, get_request_context
    from app.product.service import ProductService

    client = _client(monkeypatch)
    body = {
        "property_url": "https://www.willhaben.at/iad/immobilien/d/eigentumswohnung/wien/example-123",
        "confirmed": True,
        "idempotency_key": "android-1234567890abcdef",
    }
    assert client.post("/app/api/mobile/property-links", json=body).status_code == 401

    client.app.dependency_overrides[get_request_context] = lambda: RequestContext(
        principal_id="pqacct_mobile_test",
        authenticated=True,
        auth_source="propertyquarry_google_identity",
        access_email="owner@example.com",
    )
    calls: list[dict[str, object]] = []

    def imported(self: ProductService, **kwargs: object) -> dict[str, object]:
        calls.append(dict(kwargs))
        return {
            "status": "saved",
            "property_ref": "property-mobile-123",
            "shortlist_url": "/app/shortlist?candidate=property-mobile-123",
            "candidate": {"title": "Example home"},
        }

    monkeypatch.setattr(ProductService, "import_mobile_property_link", imported)
    missing_confirmation = client.post(
        "/app/api/mobile/property-links",
        json={**body, "confirmed": False},
    )
    saved = client.post("/app/api/mobile/property-links", json=body)

    assert missing_confirmation.status_code == 422
    assert saved.status_code == 201
    assert saved.json()["property_ref"] == "property-mobile-123"
    assert calls == [
        {
            "principal_id": "pqacct_mobile_test",
            "property_url": body["property_url"],
            "actor": "owner@example.com",
            "idempotency_key": body["idempotency_key"],
        }
    ]


def test_mobile_share_storage_is_bounded_without_losing_priority_facts() -> None:
    from app.product.service import ProductService

    urls = [
        f"https://images.example.test/property/{index}.jpg#tracking"
        for index in range(40)
    ]
    urls.extend(["http://images.example.test/insecure.jpg", urls[0]])
    bounded_urls = ProductService._bounded_mobile_property_urls(urls, limit=16)
    oversized_facts: dict[str, object] = {
        "address": "Karl-Czerny-Gasse 7, Wien",
        "price": 480000,
        **{f"extra_{index}": "x" * 4000 for index in range(150)},
    }
    bounded_facts = ProductService._bounded_mobile_property_facts(oversized_facts)

    assert len(bounded_urls) == 16
    assert all(url.startswith("https://") and "#" not in url for url in bounded_urls)
    assert bounded_facts["address"] == "Karl-Czerny-Gasse 7, Wien"
    assert bounded_facts["price"] == 480000
    assert bounded_facts["mobile_storage_truncated"] is True
    assert len(json.dumps(bounded_facts, ensure_ascii=False).encode("utf-8")) <= 49152


def test_android_source_is_isolated_secure_and_preserves_tour_hierarchy() -> None:
    package = json.loads((ROOT / "mobile" / "package.json").read_text(encoding="utf-8"))
    capacitor = json.loads(
        (ROOT / "mobile" / "capacitor.config.json").read_text(encoding="utf-8")
    )
    manifest = (
        ROOT / "mobile" / "android" / "app" / "src" / "main" / "AndroidManifest.xml"
    ).read_text(encoding="utf-8")
    runtime = (
        ROOT
        / "mobile"
        / "android"
        / "app"
        / "src"
        / "main"
        / "java"
        / "com"
        / "myexternalbrain"
        / "propertyquarry"
        / "PropertyQuarryRuntimeContract.java"
    ).read_text(encoding="utf-8")
    main_activity = (
        ROOT
        / "mobile"
        / "android"
        / "app"
        / "src"
        / "main"
        / "java"
        / "com"
        / "myexternalbrain"
        / "propertyquarry"
        / "MainActivity.java"
    ).read_text(encoding="utf-8")
    native_plugin = (
        ROOT
        / "mobile"
        / "android"
        / "app"
        / "src"
        / "main"
        / "java"
        / "com"
        / "myexternalbrain"
        / "propertyquarry"
        / "PropertyQuarryNativePlugin.java"
    ).read_text(encoding="utf-8")
    all_mobile_text = "\n".join((manifest, runtime, json.dumps(package), json.dumps(capacitor)))

    assert capacitor["appId"] == "com.myexternalbrain.propertyquarry"
    assert "url" not in capacitor["server"]
    assert capacitor["server"]["cleartext"] is False
    assert capacitor["android"]["allowMixedContent"] is False
    assert 'android:allowBackup="false"' in manifest
    assert 'android:usesCleartextTraffic="false"' in manifest
    assert 'android:name="android.intent.action.SEND"' in manifest
    assert 'android:autoVerify="true"' in manifest
    assert 'android:scheme="propertyquarry"' in manifest
    assert 'requireExact(payload, "walkthrough_default", "camera")' in runtime
    assert 'List.of("3dvista", "matterport")' not in runtime
    assert "activityResumed = true;" in main_activity
    assert "pendingIntent = intent;" in main_activity
    assert "private void continueWhenReady()" in main_activity
    assert "if (!runtimeReady || !activityResumed) return;" in main_activity
    assert '.remove(PKCE_VERIFIER)\n            .commit();' in native_plugin
    assert '.remove(SHARED_IDEMPOTENCY)\n            .commit();' in native_plugin
    assert 'call.reject("native_auth_cleanup_failed")' in native_plugin
    assert 'call.reject("native_share_cleanup_failed")' in native_plugin
    assert "memorial" not in all_mobile_text.lower()
