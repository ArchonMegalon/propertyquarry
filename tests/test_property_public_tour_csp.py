from __future__ import annotations

import re

from app.api.routes import public_tours


def _control_document(nonce: str) -> str:
    return public_tours._tour_control_external_iframe_html(
        title="3DVista CSP",
        iframe_src="/tours/3dvista/3dvista-csp/3dvista/index.htm",
        badge="3DVista Control",
        payload={"slug": "3dvista-csp"},
        nonce=nonce,
    )


def test_public_tour_control_csp_stays_bound_across_sequential_nonces() -> None:
    previous_nonce = ""
    for nonce in (
        "PropertyQuarryCspNonceSequenceA1",
        "PropertyQuarryCspNonceSequenceB2",
    ):
        html_body = _control_document(nonce)
        headers = public_tours._public_tour_control_security_headers(
            html_body=html_body,
            nonce=nonce,
        )
        script_hashes = public_tours._public_tour_inline_csp_hashes(
            html_body,
            tag_name="script",
        )
        style_hashes = public_tours._public_tour_inline_csp_hashes(
            html_body,
            tag_name="style",
        )

        assert len(script_hashes) == 2
        assert len(style_hashes) == 1
        assert set(
            re.findall(r'<(?:script|style)\b[^>]*\bnonce="([^"]+)"', html_body)
        ) == {nonce}
        assert headers["Cache-Control"] == "no-store"
        for header_name in (
            "Content-Security-Policy",
            "Content-Security-Policy-Report-Only",
        ):
            policy = headers[header_name]
            assert f"'nonce-{nonce}'" in policy
            assert all(script_hash in policy for script_hash in script_hashes)
            assert all(style_hash in policy for style_hash in style_hashes)
            assert "'unsafe-inline'" not in policy
            assert "https://cdn.jsdelivr.net" not in policy
            if previous_nonce:
                assert previous_nonce not in policy
                assert previous_nonce not in html_body
        previous_nonce = nonce


def test_verified_provider_frame_wins_over_retained_ai_panorama_fallback_csp() -> None:
    nonce = "PropertyQuarryHybridProviderNonce1"
    html_body = _control_document(nonce)

    headers = public_tours._public_tour_control_security_headers(
        html_body=html_body,
        nonce=nonce,
        ai_panorama=True,
    )

    for header_name in (
        "Content-Security-Policy",
        "Content-Security-Policy-Report-Only",
    ):
        policy = headers[header_name]
        assert "frame-src 'none'" not in policy
        assert "frame-src 'self'" in policy


def test_vendor_export_report_only_policy_accepts_required_inline_runtime_without_eval() -> None:
    headers = public_tours._public_tour_security_headers(
        runtime_profile="vendor_export",
        allow_base_uri_self=True,
    )

    enforced = headers["Content-Security-Policy"]
    report_only = headers["Content-Security-Policy-Report-Only"]

    for policy in (enforced, report_only):
        assert "script-src 'self' 'unsafe-inline'" in policy
        assert "script-src-attr 'unsafe-inline'" in policy
        assert "style-src 'self' 'unsafe-inline'" in policy
        assert "style-src-attr 'unsafe-inline'" in policy
    assert "'unsafe-eval'" in enforced
    assert "'wasm-unsafe-eval'" in enforced
    assert "'unsafe-eval'" not in report_only
    assert "'wasm-unsafe-eval'" not in report_only
