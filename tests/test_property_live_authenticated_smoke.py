from __future__ import annotations

import io
import json
import urllib.request

import scripts.propertyquarry_live_authenticated_smoke as authenticated_smoke
from scripts.propertyquarry_live_authenticated_smoke import build_live_authenticated_smoke_receipt


SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=()",
}

SIGN_IN_BODY = (
    "PropertyQuarry Open search Continue with Google "
    "First sign-in creates the account automatically. "
    "<button>Log out</button>"
)

SIGN_IN_ACTIVE_BODY = (
    "PropertyQuarry Open search Continue with Google "
    "First sign-in creates the account automatically. "
    "<button>Log out</button>"
)

ACCOUNT_AGENT_BODY = (
    'PropertyQuarry <section class="pqx-account-logout-strip" aria-label="Signed-in session">'
    "<button>Log out</button></section> <h2>Account</h2> <h2>Notifications</h2> <h2>Agent</h2>"
    '<form action="/app/api/property/account/notifications">'
    '<input type="checkbox" name="notification_channels" value="email">'
    '<input type="checkbox" name="notification_channels" value="telegram">'
    '<input type="checkbox" name="notification_channels" value="whatsapp">'
    '<input type="radio" name="preferred_channel" value="email">'
    '<input type="tel" name="whatsapp_ai_support_phone">'
    "<button>Save notifications</button></form>"
)

ACCOUNT_FREE_BODY = ACCOUNT_AGENT_BODY.replace("<h2>Agent</h2>", "<h2>Free</h2>")
ACCOUNT_FREE_STANDARD_BODY = ACCOUNT_AGENT_BODY.replace(
    "<h2>Agent</h2>",
    "<h2>Free standard research</h2>",
)

BILLING_PORTAL_UNAVAILABLE_BODY = (
    "PropertyQuarry Billing portal unavailable. "
    "The billing portal is still being connected. "
    "Your PropertyQuarry access stays active from the account page."
)

BILLING_PORTAL_LOGIN_REQUIRED_BODY = (
    "PropertyQuarry Billing portal unavailable. "
    "This billing account still opens another sign-in, so PropertyQuarry is keeping it closed for now. "
    "Your PropertyQuarry access stays active from the account page."
)

AUTHENTICATED_SHARED_FAST_RUN_BODY = (
    "PropertyQuarry Search results Matching homes "
    "Altbau near U6 "
    '<a href="/app/research/latest-real-home?run_id=run-42">Open property</a>'
)


def _fake_response(
    body: str,
    *,
    status_code: int = 200,
    final_url: str = "",
    headers: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "status_code": status_code,
        "final_url": final_url or "https://propertyquarry.com/app/account",
        "headers": {"Content-Type": "text/html; charset=utf-8", **(headers or SECURITY_HEADERS)},
        "body": body.encode("utf-8"),
        "duration_ms": 14,
    }


def test_live_authenticated_smoke_passes_paid_customer_surfaces_without_network() -> None:
    bodies = {
        "https://propertyquarry.com/app/account": ACCOUNT_AGENT_BODY,
        "https://propertyquarry.com/app/billing": BILLING_PORTAL_UNAVAILABLE_BODY,
        "https://propertyquarry.com/sign-in": SIGN_IN_BODY,
    }

    receipt = build_live_authenticated_smoke_receipt(
        base_url="https://propertyquarry.com",
        api_token="token",
        principal_id="cf-email:tibor.girschele@gmail.com",
        expected_plan_label="Agent",
        fetcher=lambda url, _timeout: _fake_response(bodies[url], final_url=url),
    )

    assert receipt["status"] == "pass"
    assert receipt["failed_count"] == 0
    assert receipt["billing_readiness"] == {
        "state": "unavailable",
        "available": False,
        "paid_persona": True,
        "required_for_paid_persona": True,
        "compatibility_ok": True,
        "flagship_ok": False,
        "reason": "fail_closed_recovery_only",
    }


def test_live_authenticated_smoke_default_live_routes_cover_shared_fast_run_without_network() -> None:
    shared_path = authenticated_smoke.AUTHENTICATED_SHARED_FAST_RUN_PATH
    bodies = {
        "https://propertyquarry.com/app/account": ACCOUNT_AGENT_BODY,
        "https://propertyquarry.com/app/billing": BILLING_PORTAL_UNAVAILABLE_BODY,
        "https://propertyquarry.com/sign-in": SIGN_IN_BODY,
        f"https://propertyquarry.com{shared_path}": AUTHENTICATED_SHARED_FAST_RUN_BODY,
    }

    receipt = build_live_authenticated_smoke_receipt(
        base_url="https://propertyquarry.com",
        api_token="token",
        principal_id="cf-email:tibor.girschele@gmail.com",
        expected_plan_label="Agent",
        routes=authenticated_smoke.DEFAULT_LIVE_ROUTES,
        fetcher=lambda url, _timeout: _fake_response(bodies[url], final_url=url),
    )

    assert receipt["status"] == "pass"
    assert receipt["failed_count"] == 0
    shared_row = next(row for row in receipt["checks"] if row["path"] == shared_path)
    assert any(check["name"] == "authenticated_fast_run_heading" and check["ok"] is True for check in shared_row["checks"])
    assert any(check["name"] == "authenticated_fast_run_no_sample_copy" and check["ok"] is True for check in shared_row["checks"])
    assert any(check["name"] == "authenticated_fast_run_no_example_shortlist" and check["ok"] is True for check in shared_row["checks"])


def test_live_authenticated_smoke_accepts_shared_fast_run_redirect_to_latest_without_network() -> None:
    shared_path = authenticated_smoke.AUTHENTICATED_SHARED_FAST_RUN_PATH

    def fetcher(url: str, _timeout: float) -> dict[str, object]:
        assert url == f"https://propertyquarry.com{shared_path}"
        return _fake_response(
            "",
            status_code=303,
            final_url=url,
            headers={**SECURITY_HEADERS, "Location": "/app/shortlist/run/run-42"},
        )

    receipt = build_live_authenticated_smoke_receipt(
        base_url="https://propertyquarry.com",
        api_token="token",
        principal_id="cf-email:tibor.girschele@gmail.com",
        expected_plan_label="Agent",
        routes=(shared_path,),
        fetcher=fetcher,
    )

    assert receipt["status"] == "pass"
    shared_row = receipt["checks"][0]
    assert any(check["name"] == "authenticated_fast_run_redirects_to_latest" and check["ok"] is True for check in shared_row["checks"])
    assert any(check["name"] == "authenticated_fast_run_not_public_entrypoint_redirect" and check["ok"] is True for check in shared_row["checks"])


def test_live_authenticated_smoke_rejects_shared_fast_run_sample_leak_without_network() -> None:
    shared_path = authenticated_smoke.AUTHENTICATED_SHARED_FAST_RUN_PATH
    sample_body = (
        "PropertyQuarry Sample homes Danube Flats demo "
        '<a href="/app/example/shortlist?candidate=danube-flats-demo">Open property</a>'
    )

    receipt = build_live_authenticated_smoke_receipt(
        base_url="https://propertyquarry.com",
        api_token="token",
        principal_id="cf-email:tibor.girschele@gmail.com",
        expected_plan_label="Agent",
        routes=(shared_path,),
        fetcher=lambda url, _timeout: _fake_response(sample_body, final_url=url),
    )

    assert receipt["status"] == "fail"
    shared_row = receipt["checks"][0]
    assert any(check["name"] == "authenticated_fast_run_heading" and check["ok"] is False for check in shared_row["checks"])
    assert any(check["name"] == "authenticated_fast_run_no_sample_copy" and check["ok"] is False for check in shared_row["checks"])
    assert any(check["name"] == "authenticated_fast_run_no_example_shortlist" and check["ok"] is False for check in shared_row["checks"])


def test_live_authenticated_smoke_rejects_shared_fast_run_public_entrypoint_redirect_without_network() -> None:
    shared_path = authenticated_smoke.AUTHENTICATED_SHARED_FAST_RUN_PATH

    receipt = build_live_authenticated_smoke_receipt(
        base_url="https://propertyquarry.com",
        api_token="token",
        principal_id="cf-email:tibor.girschele@gmail.com",
        expected_plan_label="Agent",
        routes=(shared_path,),
        fetcher=lambda url, _timeout: _fake_response(
            "",
            status_code=303,
            final_url=url,
            headers={**SECURITY_HEADERS, "Location": "/app/shortlist/run/public-demo-run"},
        ),
    )

    assert receipt["status"] == "fail"
    shared_row = receipt["checks"][0]
    assert any(check["name"] == "authenticated_fast_run_redirects_to_latest" and check["ok"] is False for check in shared_row["checks"])
    assert any(check["name"] == "authenticated_fast_run_not_public_entrypoint_redirect" and check["ok"] is False for check in shared_row["checks"])


def test_live_authenticated_smoke_accepts_active_signed_in_copy_without_network() -> None:
    bodies = {
        "https://propertyquarry.com/app/account": ACCOUNT_AGENT_BODY,
        "https://propertyquarry.com/app/billing": BILLING_PORTAL_UNAVAILABLE_BODY,
        "https://propertyquarry.com/sign-in": SIGN_IN_ACTIVE_BODY,
    }

    receipt = build_live_authenticated_smoke_receipt(
        base_url="https://propertyquarry.com",
        api_token="token",
        principal_id="cf-email:tibor.girschele@gmail.com",
        expected_plan_label="Agent",
        fetcher=lambda url, _timeout: _fake_response(bodies[url], final_url=url),
    )

    assert receipt["status"] == "pass"
    assert receipt["failed_count"] == 0


def test_live_authenticated_smoke_accepts_login_required_fail_closed_billing_copy_without_network() -> None:
    bodies = {
        "https://propertyquarry.com/app/account": ACCOUNT_AGENT_BODY,
        "https://propertyquarry.com/app/billing": BILLING_PORTAL_LOGIN_REQUIRED_BODY,
        "https://propertyquarry.com/sign-in": SIGN_IN_BODY,
    }

    receipt = build_live_authenticated_smoke_receipt(
        base_url="https://propertyquarry.com",
        api_token="token",
        principal_id="cf-email:tibor.girschele@gmail.com",
        expected_plan_label="Agent",
        fetcher=lambda url, _timeout: _fake_response(bodies[url], final_url=url),
    )

    assert receipt["status"] == "pass"
    assert receipt["failed_count"] == 0


def test_live_authenticated_smoke_accepts_external_billing_redirect_without_network() -> None:
    bodies = {
        "https://propertyquarry.com/app/account": ACCOUNT_AGENT_BODY,
        "https://propertyquarry.com/app/billing": "",
        "https://propertyquarry.com/sign-in": SIGN_IN_BODY,
    }

    def fetcher(url: str, _timeout: float) -> dict[str, object]:
        if url.endswith("/app/billing"):
            return _fake_response(
                "",
                status_code=303,
                final_url=url,
                headers={**SECURITY_HEADERS, "Location": "https://billing.propertyquarry.com/account"},
            )
        return _fake_response(bodies[url], final_url=url)

    receipt = build_live_authenticated_smoke_receipt(
        base_url="https://propertyquarry.com",
        api_token="token",
        principal_id="cf-email:tibor.girschele@gmail.com",
        expected_plan_label="Agent",
        fetcher=fetcher,
        billing_handoff_resolver=lambda _host, _port: [(object(),)],
    )

    assert receipt["status"] == "pass"
    billing_row = next(row for row in receipt["checks"] if row["path"] == "/app/billing")
    assert any(check["name"] == "billing_external_handoff" and check["ok"] is True for check in billing_row["checks"])
    assert any(check["name"] == "billing_external_handoff_resolves" and check["ok"] is True for check in billing_row["checks"])
    assert any(check["name"] == "billing_external_handoff_usable" and check["ok"] is True for check in billing_row["checks"])
    assert receipt["billing_readiness"]["state"] == "available"
    assert receipt["billing_readiness"]["flagship_ok"] is True


def test_live_authenticated_smoke_accepts_internal_account_fallback_billing_redirect_without_network() -> None:
    bodies = {
        "https://propertyquarry.com/app/account": ACCOUNT_AGENT_BODY,
        "https://propertyquarry.com/app/billing": "",
        "https://propertyquarry.com/sign-in": SIGN_IN_BODY,
    }

    def fetcher(url: str, _timeout: float) -> dict[str, object]:
        if url.endswith("/app/billing"):
            return _fake_response(
                "",
                status_code=303,
                final_url=url,
                headers={**SECURITY_HEADERS, "Location": "/app/account?billing=1#delivery"},
            )
        return _fake_response(bodies[url], final_url=url)

    receipt = build_live_authenticated_smoke_receipt(
        base_url="https://propertyquarry.com",
        api_token="token",
        principal_id="cf-email:tibor.girschele@gmail.com",
        expected_plan_label="Agent",
        fetcher=fetcher,
    )

    assert receipt["status"] == "pass"
    billing_row = next(row for row in receipt["checks"] if row["path"] == "/app/billing")
    assert any(check["name"] == "billing_internal_account_fallback" and check["ok"] is True for check in billing_row["checks"])
    assert billing_row.get("billing_handoff_probe") == {}
    assert billing_row.get("billing_handoff_resolution") == {}
    assert receipt["billing_readiness"]["state"] == "degraded"
    assert receipt["billing_readiness"]["compatibility_ok"] is True
    assert receipt["billing_readiness"]["flagship_ok"] is False


def test_live_authenticated_smoke_accepts_local_bridge_launch_then_external_billing_redirect_without_network() -> None:
    bodies = {
        "https://propertyquarry.com/app/account": ACCOUNT_AGENT_BODY,
        "https://propertyquarry.com/app/billing": "",
        "https://propertyquarry.com/sign-in": SIGN_IN_BODY,
        "https://propertyquarry.com/app/api/property/billing/bridge-launch": "",
    }

    def fetcher(url: str, _timeout: float) -> dict[str, object]:
        if url.endswith("/app/billing"):
            return _fake_response(
                "",
                status_code=303,
                final_url=url,
                headers={**SECURITY_HEADERS, "Location": "/app/api/property/billing/bridge-launch"},
            )
        if "/app/api/property/billing/bridge-launch" in url:
            return _fake_response(
                "",
                status_code=303,
                final_url=url,
                headers={**SECURITY_HEADERS, "Location": "https://billing.propertyquarry.com/sso/propertyquarry?pq_bridge=token"},
            )
        return _fake_response(bodies[url], final_url=url)

    receipt = build_live_authenticated_smoke_receipt(
        base_url="https://propertyquarry.com",
        api_token="token",
        principal_id="cf-email:tibor.girschele@gmail.com",
        expected_plan_label="Agent",
        fetcher=fetcher,
        billing_handoff_resolver=lambda _host, _port: [(object(),)],
    )

    assert receipt["status"] == "pass"
    billing_row = next(row for row in receipt["checks"] if row["path"] == "/app/billing")
    assert billing_row["billing_handoff_resolution"]["bridge_launch_used"] is True
    assert billing_row["billing_handoff_resolution"]["bridge_launch_status_code"] == 303
    assert isinstance(billing_row["billing_handoff_resolution"]["bridge_launch_result"]["body"], str)
    assert any(check["name"] == "billing_bridge_launch" and check["ok"] is True for check in billing_row["checks"])
    assert any(check["name"] == "billing_external_handoff" and check["ok"] is True for check in billing_row["checks"])
    assert billing_row["billing_handoff_probe"]["status_code"] == 0
    json.dumps(receipt, sort_keys=True)


def test_live_authenticated_smoke_redacts_member_token_billing_redirects_without_network() -> None:
    bodies = {
        "https://propertyquarry.com/app/account": ACCOUNT_AGENT_BODY,
        "https://propertyquarry.com/app/billing": "",
        "https://propertyquarry.com/sign-in": SIGN_IN_BODY,
        "https://propertyquarry.com/app/api/property/billing/bridge-launch": "",
    }

    tokenized_location = (
        "https://billing.propertyquarry.com/login/token/super-secret-member-token/home"
        "?state=oauth-state&code=oauth-code&pq_bridge=bridge-secret"
    )

    def fetcher(url: str, _timeout: float) -> dict[str, object]:
        if url.endswith("/app/billing"):
            return _fake_response(
                "",
                status_code=303,
                final_url=url,
                headers={**SECURITY_HEADERS, "Location": "/app/api/property/billing/bridge-launch"},
            )
        if "/app/api/property/billing/bridge-launch" in url:
            return _fake_response(
                "",
                status_code=303,
                final_url=url,
                headers={**SECURITY_HEADERS, "Location": tokenized_location},
            )
        return _fake_response(bodies[url], final_url=url)

    receipt = build_live_authenticated_smoke_receipt(
        base_url="https://propertyquarry.com",
        api_token="token",
        principal_id="cf-email:tibor.girschele@gmail.com",
        expected_plan_label="Agent",
        fetcher=fetcher,
        billing_handoff_resolver=lambda _host, _port: [(object(),)],
    )

    assert receipt["status"] == "pass"
    serialized = json.dumps(receipt, sort_keys=True)
    assert "super-secret-member-token" not in serialized
    assert "oauth-state" not in serialized
    assert "oauth-code" not in serialized
    assert "bridge-secret" not in serialized
    assert "/login/token/[redacted]/home" in serialized
    assert "state=[redacted]" in serialized
    assert "code=[redacted]" in serialized
    assert "pq_bridge=[redacted]" in serialized


def test_live_authenticated_smoke_rejects_bridge_guided_login_assist_when_vendor_lane_still_requires_login() -> None:
    bodies = {
        "https://propertyquarry.com/app/account": ACCOUNT_AGENT_BODY,
        "https://propertyquarry.com/app/billing": "",
        "https://propertyquarry.com/sign-in": SIGN_IN_BODY,
        "https://propertyquarry.com/app/api/property/billing/bridge-launch": "",
    }

    def fetcher(url: str, _timeout: float) -> dict[str, object]:
        if url.endswith("/app/billing"):
            return _fake_response(
                "",
                status_code=303,
                final_url=url,
                headers={**SECURITY_HEADERS, "Location": "/app/api/property/billing/bridge-launch"},
            )
        if "/app/api/property/billing/bridge-launch" in url:
            return _fake_response(
                "",
                status_code=303,
                final_url=url,
                headers={**SECURITY_HEADERS, "Location": "https://billing.propertyquarry.com/sso/propertyquarry?pq_bridge=token"},
            )
        return _fake_response(bodies[url], final_url=url)

    receipt = build_live_authenticated_smoke_receipt(
        base_url="https://propertyquarry.com",
        api_token="token",
        principal_id="cf-email:tibor.girschele@gmail.com",
        expected_plan_label="Agent",
        fetcher=fetcher,
        billing_handoff_resolver=lambda _host, _port: [(object(),)],
        billing_handoff_checker=lambda _location, _timeout: {
            "ok": False,
            "status_code": 302,
            "redirect_location": "/login?login_direct_url=/account",
            "error": "handoff_url_requires_separate_login",
        },
        billing_bridge_assist_checker=lambda _location, _timeout: {
            "ok": True,
            "status_code": 200,
            "final_url": "https://billing.propertyquarry.com/login?login_direct_url=%2Faccount",
            "has_guided_markers": True,
            "has_email_field": True,
            "error": "",
        },
    )

    assert receipt["status"] == "fail"
    billing_row = next(row for row in receipt["checks"] if row["path"] == "/app/billing")
    assert any(check["name"] == "billing_bridge_launch" and check["ok"] is True for check in billing_row["checks"])
    assert any(check["name"] == "billing_external_handoff_usable" and check["ok"] is False for check in billing_row["checks"])
    assert any(check["name"] == "billing_no_second_login" and check["ok"] is False for check in billing_row["checks"])
    assert any(check["name"] == "billing_bridge_guided_login_assist" and check["ok"] is True for check in billing_row["checks"])
    assert billing_row["billing_handoff_probe"]["error"] == "handoff_url_requires_separate_login"
    assert billing_row["billing_bridge_login_assist_probe"]["ok"] is True


def test_live_authenticated_smoke_accepts_local_bridge_launch_then_internal_account_fallback_without_network() -> None:
    bodies = {
        "https://propertyquarry.com/app/account": ACCOUNT_AGENT_BODY,
        "https://propertyquarry.com/app/billing": "",
        "https://propertyquarry.com/sign-in": SIGN_IN_BODY,
        "https://propertyquarry.com/app/api/property/billing/bridge-launch": "",
    }

    def fetcher(url: str, _timeout: float) -> dict[str, object]:
        if url.endswith("/app/billing"):
            return _fake_response(
                "",
                status_code=303,
                final_url=url,
                headers={**SECURITY_HEADERS, "Location": "/app/api/property/billing/bridge-launch"},
            )
        if "/app/api/property/billing/bridge-launch" in url:
            return _fake_response(
                "",
                status_code=303,
                final_url=url,
                headers={**SECURITY_HEADERS, "Location": "/app/account?billing=1#delivery"},
            )
        return _fake_response(bodies[url], final_url=url)

    receipt = build_live_authenticated_smoke_receipt(
        base_url="https://propertyquarry.com",
        api_token="token",
        principal_id="cf-email:tibor.girschele@gmail.com",
        expected_plan_label="Agent",
        fetcher=fetcher,
    )

    assert receipt["status"] == "pass"
    billing_row = next(row for row in receipt["checks"] if row["path"] == "/app/billing")
    assert billing_row["billing_handoff_resolution"]["bridge_launch_used"] is True
    assert billing_row["billing_handoff_resolution"]["bridge_launch_status_code"] == 303
    assert any(check["name"] == "billing_bridge_launch" and check["ok"] is True for check in billing_row["checks"])
    assert any(check["name"] == "billing_internal_account_fallback" and check["ok"] is True for check in billing_row["checks"])
    assert billing_row.get("billing_handoff_probe") == {}
    json.dumps(receipt, sort_keys=True)


def test_live_authenticated_smoke_rejects_external_billing_redirect_404_without_network() -> None:
    bodies = {
        "https://propertyquarry.com/app/account": ACCOUNT_AGENT_BODY,
        "https://propertyquarry.com/app/billing": "",
        "https://propertyquarry.com/sign-in": SIGN_IN_BODY,
    }

    def fetcher(url: str, _timeout: float) -> dict[str, object]:
        if url.endswith("/app/billing"):
            return _fake_response(
                "",
                status_code=303,
                final_url=url,
                headers={**SECURITY_HEADERS, "Location": "https://billing.propertyquarry.com/account"},
            )
        return _fake_response(bodies[url], final_url=url)

    receipt = build_live_authenticated_smoke_receipt(
        base_url="https://propertyquarry.com",
        api_token="token",
        principal_id="cf-email:tibor.girschele@gmail.com",
        expected_plan_label="Agent",
        fetcher=fetcher,
        billing_handoff_resolver=lambda _host, _port: [(object(),)],
        billing_handoff_checker=lambda _location, _timeout: {"ok": False, "status_code": 404, "error": "handoff_url_http_404"},
    )

    assert receipt["status"] == "fail"
    billing_row = next(row for row in receipt["checks"] if row["path"] == "/app/billing")
    assert any(check["name"] == "billing_external_handoff_resolves" and check["ok"] is True for check in billing_row["checks"])
    assert any(check["name"] == "billing_external_handoff_usable" and check["ok"] is False for check in billing_row["checks"])
    assert billing_row["billing_handoff_probe"]["status_code"] == 404


def test_live_authenticated_smoke_rejects_cloudflare_error_page_billing_handoff() -> None:
    bodies = {
        "https://propertyquarry.com/app/account": ACCOUNT_AGENT_BODY,
        "https://propertyquarry.com/app/billing": "",
        "https://propertyquarry.com/sign-in": SIGN_IN_BODY,
    }

    def fetcher(url: str, _timeout: float) -> dict[str, object]:
        if url.endswith("/app/billing"):
            return _fake_response(
                "",
                status_code=303,
                final_url=url,
                headers={**SECURITY_HEADERS, "Location": "https://billing.propertyquarry.com/account"},
            )
        return _fake_response(bodies[url], final_url=url)

    receipt = build_live_authenticated_smoke_receipt(
        base_url="https://propertyquarry.com",
        api_token="token",
        principal_id="cf-email:tibor.girschele@gmail.com",
        expected_plan_label="Agent",
        fetcher=fetcher,
        billing_handoff_resolver=lambda _host, _port: [(object(),)],
        billing_handoff_checker=lambda _location, _timeout: {
            "ok": False,
            "status_code": 403,
            "error": "handoff_url_cloudflare_error_1014",
        },
    )

    assert receipt["status"] == "fail"
    billing_row = next(row for row in receipt["checks"] if row["path"] == "/app/billing")
    assert any(check["name"] == "billing_external_handoff_usable" and check["ok"] is False for check in billing_row["checks"])
    assert billing_row["billing_handoff_probe"]["error"] == "handoff_url_cloudflare_error_1014"


def test_live_authenticated_smoke_rejects_billing_handoff_that_requires_second_login() -> None:
    bodies = {
        "https://propertyquarry.com/app/account": ACCOUNT_AGENT_BODY,
        "https://propertyquarry.com/app/billing": "",
        "https://propertyquarry.com/sign-in": SIGN_IN_BODY,
    }

    def fetcher(url: str, _timeout: float) -> dict[str, object]:
        if url.endswith("/app/billing"):
            return _fake_response(
                "",
                status_code=303,
                final_url=url,
                headers={**SECURITY_HEADERS, "Location": "https://propertyquarry.directoryup.com/account"},
            )
        return _fake_response(bodies[url], final_url=url)

    receipt = build_live_authenticated_smoke_receipt(
        base_url="https://propertyquarry.com",
        api_token="token",
        principal_id="cf-email:tibor.girschele@gmail.com",
        expected_plan_label="Agent",
        fetcher=fetcher,
        billing_handoff_resolver=lambda _host, _port: [(object(),)],
        billing_handoff_checker=lambda _location, _timeout: {
            "ok": False,
            "status_code": 302,
            "redirect_location": "/login?login_direct_url=/account",
            "error": "handoff_url_requires_separate_login",
        },
    )

    assert receipt["status"] == "fail"
    billing_row = next(row for row in receipt["checks"] if row["path"] == "/app/billing")
    assert any(check["name"] == "billing_external_handoff_usable" and check["ok"] is False for check in billing_row["checks"])
    assert billing_row["billing_handoff_probe"]["error"] == "handoff_url_requires_separate_login"


def test_live_authenticated_smoke_rejects_white_label_redirect_chain_that_ends_in_login(
    monkeypatch,
) -> None:
    class _RedirectChainOpener:
        def open(self, request, timeout: float = 0):  # noqa: ANN001
            if request.full_url == "https://billing.propertyquarry.com/account":
                raise urllib.error.HTTPError(
                    request.full_url,
                    302,
                    "redirect",
                    {"Location": "https://propertyquarry.directoryup.com/account"},
                    io.BytesIO(b""),
                )
            if request.full_url == "https://propertyquarry.directoryup.com/account":
                raise urllib.error.HTTPError(
                    request.full_url,
                    302,
                    "redirect",
                    {"Location": "/login?login_direct_url=/account"},
                    io.BytesIO(b""),
                )
            raise AssertionError(request.full_url)

    monkeypatch.setattr("scripts.propertyquarry_billing_handoff_probe.no_proxy_opener", lambda *handlers: _RedirectChainOpener())

    probe = authenticated_smoke._https_handoff_url_usable("https://billing.propertyquarry.com/account", timeout_seconds=3.0)

    assert probe["ok"] is False
    assert probe["status_code"] == 302
    assert probe["error"] == "handoff_url_requires_separate_login"
    assert probe["redirect_chain"] == [
        "https://propertyquarry.directoryup.com/account",
        "https://propertyquarry.directoryup.com/login?login_direct_url=/account",
    ]


def test_live_authenticated_smoke_rejects_unresolved_external_billing_redirect_without_network(monkeypatch) -> None:
    bodies = {
        "https://propertyquarry.com/app/account": ACCOUNT_AGENT_BODY,
        "https://propertyquarry.com/sign-in": SIGN_IN_BODY,
    }

    def fetcher(url: str, _timeout: float) -> dict[str, object]:
        if url.endswith("/app/billing"):
            return _fake_response(
                "",
                status_code=303,
                final_url=url,
                headers={**SECURITY_HEADERS, "Location": "https://billing.propertyquarry.com/account"},
            )
        return _fake_response(bodies[url], final_url=url)

    def unresolved(_host: str, _port: int) -> None:
        raise OSError("missing dns")

    def public_dns_unavailable(_request, timeout=0):
        raise OSError("public dns unavailable")

    monkeypatch.setattr(urllib.request, "urlopen", public_dns_unavailable)

    receipt = build_live_authenticated_smoke_receipt(
        base_url="https://propertyquarry.com",
        api_token="token",
        principal_id="cf-email:tibor.girschele@gmail.com",
        expected_plan_label="Agent",
        fetcher=fetcher,
        billing_handoff_resolver=unresolved,
    )

    assert receipt["status"] == "fail"
    billing_row = next(row for row in receipt["checks"] if row["path"] == "/app/billing")
    assert any(check["name"] == "billing_external_handoff" and check["ok"] is True for check in billing_row["checks"])
    assert any(check["name"] == "billing_external_handoff_resolves" and check["ok"] is False for check in billing_row["checks"])


def test_live_authenticated_smoke_accepts_public_dns_for_stale_local_billing_resolver(monkeypatch) -> None:
    bodies = {
        "https://propertyquarry.com/app/account": ACCOUNT_AGENT_BODY,
        "https://propertyquarry.com/sign-in": SIGN_IN_BODY,
    }

    def fetcher(url: str, _timeout: float) -> dict[str, object]:
        if url.endswith("/app/billing"):
            return _fake_response(
                "",
                status_code=303,
                final_url=url,
                headers={**SECURITY_HEADERS, "Location": "https://billing.propertyquarry.com/account"},
            )
        return _fake_response(bodies[url], final_url=url)

    def unresolved(_host: str, _port: int) -> None:
        raise OSError("stale local dns")

    class _DnsResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return (
                b'{"Status":0,"Answer":[{"name":"billing.propertyquarry.com.",'
                b'"type":5,"data":"members.brilliantdirectories.com."}]}'
            )

    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=0: _DnsResponse())

    receipt = build_live_authenticated_smoke_receipt(
        base_url="https://propertyquarry.com",
        api_token="token",
        principal_id="cf-email:tibor.girschele@gmail.com",
        expected_plan_label="Agent",
        fetcher=fetcher,
        billing_handoff_resolver=unresolved,
        billing_handoff_dns_target="members.brilliantdirectories.com",
    )

    assert receipt["status"] == "pass"
    billing_row = next(row for row in receipt["checks"] if row["path"] == "/app/billing")
    assert any(check["name"] == "billing_external_handoff_resolves" and check["ok"] is True for check in billing_row["checks"])


def test_live_authenticated_smoke_accepts_public_dns_without_expected_target(monkeypatch) -> None:
    bodies = {
        "https://propertyquarry.com/app/account": ACCOUNT_AGENT_BODY,
        "https://propertyquarry.com/sign-in": SIGN_IN_BODY,
    }

    def fetcher(url: str, _timeout: float) -> dict[str, object]:
        if url.endswith("/app/billing"):
            return _fake_response(
                "",
                status_code=303,
                final_url=url,
                headers={**SECURITY_HEADERS, "Location": "https://billing.propertyquarry.com/account"},
            )
        return _fake_response(bodies[url], final_url=url)

    def unresolved(_host: str, _port: int) -> None:
        raise OSError("stale local dns")

    class _DnsResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return (
                b'{"Status":0,"Answer":[{"name":"billing.propertyquarry.com.",'
                b'"type":5,"data":"members.brilliantdirectories.com."}]}'
            )

    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=0: _DnsResponse())

    receipt = build_live_authenticated_smoke_receipt(
        base_url="https://propertyquarry.com",
        api_token="token",
        principal_id="cf-email:tibor.girschele@gmail.com",
        expected_plan_label="Agent",
        fetcher=fetcher,
        billing_handoff_resolver=unresolved,
    )

    assert receipt["status"] == "pass"
    billing_row = next(row for row in receipt["checks"] if row["path"] == "/app/billing")
    assert any(check["name"] == "billing_external_handoff_resolves" and check["ok"] is True for check in billing_row["checks"])


def test_live_authenticated_smoke_accepts_cloudflare_proxied_billing_host(monkeypatch) -> None:
    bodies = {
        "https://propertyquarry.com/app/account": ACCOUNT_AGENT_BODY,
        "https://propertyquarry.com/sign-in": SIGN_IN_BODY,
    }

    def fetcher(url: str, _timeout: float) -> dict[str, object]:
        if url.endswith("/app/billing"):
            return _fake_response(
                "",
                status_code=303,
                final_url=url,
                headers={**SECURITY_HEADERS, "Location": "https://billing.propertyquarry.com/account"},
            )
        return _fake_response(bodies[url], final_url=url)

    def unresolved(_host: str, _port: int) -> None:
        raise OSError("stale local dns")

    class _DnsResponse:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return self._body

    def fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
        url = str(getattr(request, "full_url", request))
        if "type=CNAME" in url:
            return _DnsResponse(
                b'{"Status":0,"Answer":[{"name":"billing.propertyquarry.com.",'
                b'"type":5,"data":"members.brilliantdirectories.com."}]}'
            )
        if "type=A" in url:
            return _DnsResponse(
                b'{"Status":0,"Answer":[{"name":"billing.propertyquarry.com.",'
                b'"type":1,"data":"188.114.96.3"}]}'
            )
        return _DnsResponse(b'{"Status":0,"Answer":[]}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    receipt = build_live_authenticated_smoke_receipt(
        base_url="https://propertyquarry.com",
        api_token="token",
        principal_id="cf-email:tibor.girschele@gmail.com",
        expected_plan_label="Agent",
        fetcher=fetcher,
        billing_handoff_resolver=unresolved,
        billing_handoff_dns_target="propertyquarry.directoryup.com",
    )

    assert receipt["status"] == "pass"
    billing_row = next(row for row in receipt["checks"] if row["path"] == "/app/billing")
    assert any(check["name"] == "billing_external_handoff_resolves" and check["ok"] is True for check in billing_row["checks"])


def test_live_authenticated_smoke_rejects_public_dns_target_mismatch(monkeypatch) -> None:
    bodies = {
        "https://propertyquarry.com/app/account": ACCOUNT_AGENT_BODY,
        "https://propertyquarry.com/sign-in": SIGN_IN_BODY,
    }

    def fetcher(url: str, _timeout: float) -> dict[str, object]:
        if url.endswith("/app/billing"):
            return _fake_response(
                "",
                status_code=303,
                final_url=url,
                headers={**SECURITY_HEADERS, "Location": "https://billing.propertyquarry.com/account"},
            )
        return _fake_response(bodies[url], final_url=url)

    def unresolved(_host: str, _port: int) -> None:
        raise OSError("stale local dns")

    class _DnsResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return b'{"Status":0,"Answer":[{"name":"billing.propertyquarry.com.","type":5,"data":"wrong.example.com."}]}'

    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=0: _DnsResponse())

    receipt = build_live_authenticated_smoke_receipt(
        base_url="https://propertyquarry.com",
        api_token="token",
        principal_id="cf-email:tibor.girschele@gmail.com",
        expected_plan_label="Agent",
        fetcher=fetcher,
        billing_handoff_resolver=unresolved,
        billing_handoff_dns_target="members.brilliantdirectories.com",
    )

    assert receipt["status"] == "fail"
    billing_row = next(row for row in receipt["checks"] if row["path"] == "/app/billing")
    assert any(check["name"] == "billing_external_handoff_resolves" and check["ok"] is False for check in billing_row["checks"])


def test_live_authenticated_smoke_accepts_fail_closed_billing_recovery_without_network() -> None:
    bodies = {
        "https://propertyquarry.com/app/account": ACCOUNT_AGENT_BODY,
        "https://propertyquarry.com/app/billing": BILLING_PORTAL_UNAVAILABLE_BODY,
        "https://propertyquarry.com/sign-in": SIGN_IN_BODY,
    }

    def fetcher(url: str, _timeout: float) -> dict[str, object]:
        if url.endswith("/app/billing"):
            return _fake_response(bodies[url], status_code=503, final_url=url)
        return _fake_response(bodies[url], final_url=url)

    receipt = build_live_authenticated_smoke_receipt(
        base_url="https://propertyquarry.com",
        api_token="token",
        principal_id="cf-email:tibor.girschele@gmail.com",
        expected_plan_label="Agent",
        fetcher=fetcher,
    )

    assert receipt["status"] == "pass"
    billing_row = next(row for row in receipt["checks"] if row["path"] == "/app/billing")
    assert any(check["name"] == "billing_fail_closed_recovery" and check["ok"] is True for check in billing_row["checks"])
    assert receipt["billing_readiness"]["state"] == "unavailable"
    assert receipt["billing_readiness"]["compatibility_ok"] is True
    assert receipt["billing_readiness"]["flagship_ok"] is False


def test_live_authenticated_smoke_passes_free_customer_surfaces_when_free_is_expected() -> None:
    bodies = {
        "https://propertyquarry.com/app/account": ACCOUNT_FREE_BODY,
        "https://propertyquarry.com/app/billing": BILLING_PORTAL_UNAVAILABLE_BODY,
        "https://propertyquarry.com/sign-in": SIGN_IN_BODY,
    }

    receipt = build_live_authenticated_smoke_receipt(
        base_url="https://propertyquarry.com",
        api_token="token",
        principal_id="user-free@example.test",
        expected_plan_label="Free",
        fetcher=lambda url, _timeout: _fake_response(bodies[url], final_url=url),
    )

    assert receipt["status"] == "pass"
    assert receipt["failed_count"] == 0


def test_live_authenticated_smoke_recognizes_descriptive_free_plan_as_non_paid() -> None:
    bodies = {
        "https://propertyquarry.com/app/account": ACCOUNT_FREE_STANDARD_BODY,
        "https://propertyquarry.com/app/billing": BILLING_PORTAL_UNAVAILABLE_BODY,
        "https://propertyquarry.com/sign-in": SIGN_IN_BODY,
    }

    receipt = build_live_authenticated_smoke_receipt(
        base_url="https://propertyquarry.com",
        api_token="token",
        principal_id="propertyquarry-release-probe",
        expected_plan_label="Free standard research",
        fetcher=lambda url, _timeout: _fake_response(bodies[url], final_url=url),
    )

    assert receipt["status"] == "pass"
    assert receipt["billing_readiness"]["paid_persona"] is False
    assert receipt["billing_readiness"]["required_for_paid_persona"] is False


def test_live_authenticated_smoke_fails_when_account_loses_paid_plan_projection() -> None:
    bodies = {
        "https://propertyquarry.com/app/account": ACCOUNT_FREE_BODY,
        "https://propertyquarry.com/app/billing": BILLING_PORTAL_UNAVAILABLE_BODY,
        "https://propertyquarry.com/sign-in": SIGN_IN_BODY,
    }

    receipt = build_live_authenticated_smoke_receipt(
        base_url="https://propertyquarry.com",
        api_token="token",
        principal_id="cf-email:tibor.girschele@gmail.com",
        expected_plan_label="Agent",
        fetcher=lambda url, _timeout: _fake_response(bodies[url], final_url=url),
    )

    assert receipt["status"] == "fail"
    account_row = next(row for row in receipt["checks"] if row["path"] == "/app/account")
    assert any(check["name"] == "account_paid_plan" and check["ok"] is False for check in account_row["checks"])


def test_live_authenticated_smoke_fails_when_account_loses_logout_strip() -> None:
    bodies = {
        "https://propertyquarry.com/app/account": ACCOUNT_AGENT_BODY.replace("pqx-account-logout-strip", "pqx-account-session"),
        "https://propertyquarry.com/app/billing": BILLING_PORTAL_UNAVAILABLE_BODY,
        "https://propertyquarry.com/sign-in": SIGN_IN_BODY,
    }

    receipt = build_live_authenticated_smoke_receipt(
        base_url="https://propertyquarry.com",
        api_token="token",
        principal_id="cf-email:tibor.girschele@gmail.com",
        expected_plan_label="Agent",
        fetcher=lambda url, _timeout: _fake_response(bodies[url], final_url=url),
    )

    assert receipt["status"] == "fail"
    account_row = next(row for row in receipt["checks"] if row["path"] == "/app/account")
    assert any(check["name"] == "account_logout_strip" and check["ok"] is False for check in account_row["checks"])


def test_live_authenticated_smoke_fails_when_account_duplicates_logout_actions() -> None:
    bodies = {
        "https://propertyquarry.com/app/account": ACCOUNT_AGENT_BODY.replace("</section>", "</section><button>Log out</button>", 1),
        "https://propertyquarry.com/app/billing": BILLING_PORTAL_UNAVAILABLE_BODY,
        "https://propertyquarry.com/sign-in": SIGN_IN_BODY,
    }

    receipt = build_live_authenticated_smoke_receipt(
        base_url="https://propertyquarry.com",
        api_token="token",
        principal_id="cf-email:tibor.girschele@gmail.com",
        expected_plan_label="Agent",
        fetcher=lambda url, _timeout: _fake_response(bodies[url], final_url=url),
    )

    assert receipt["status"] == "fail"
    account_row = next(row for row in receipt["checks"] if row["path"] == "/app/account")
    assert any(check["name"] == "account_single_logout" and check["ok"] is False for check in account_row["checks"])


def test_live_authenticated_smoke_fails_when_sign_in_surface_duplicates_logout() -> None:
    bodies = {
        "https://propertyquarry.com/app/account": ACCOUNT_AGENT_BODY,
        "https://propertyquarry.com/app/billing": BILLING_PORTAL_UNAVAILABLE_BODY,
        "https://propertyquarry.com/sign-in": SIGN_IN_BODY.replace("<button>Log out</button>", "<button>Log out</button><button>Log out</button>"),
    }

    receipt = build_live_authenticated_smoke_receipt(
        base_url="https://propertyquarry.com",
        api_token="token",
        principal_id="cf-email:tibor.girschele@gmail.com",
        expected_plan_label="Agent",
        fetcher=lambda url, _timeout: _fake_response(bodies[url], final_url=url),
    )

    assert receipt["status"] == "fail"
    sign_in_row = next(row for row in receipt["checks"] if row["path"] == "/sign-in")
    assert any(check["name"] == "sign_in_single_logout" and check["ok"] is False for check in sign_in_row["checks"])


def test_live_authenticated_smoke_accepts_signed_in_surface_without_anonymous_provider_copy() -> None:
    bodies = {
        "https://propertyquarry.com/sign-in": "PropertyQuarry Open search <button>Log out</button>",
    }

    receipt = build_live_authenticated_smoke_receipt(
        base_url="https://propertyquarry.com",
        api_token="token",
        principal_id="cf-email:tibor.girschele@gmail.com",
        routes=("/sign-in",),
        fetcher=lambda url, _timeout: _fake_response(bodies[url], final_url=url),
    )

    assert receipt["status"] == "pass"
    sign_in_row = next(row for row in receipt["checks"] if row["path"] == "/sign-in")
    assert all(check["ok"] is True for check in sign_in_row["checks"])


def test_live_authenticated_smoke_retries_transient_transport_failures_without_network() -> None:
    bodies = {
        "https://propertyquarry.com/app/account": ACCOUNT_AGENT_BODY,
        "https://propertyquarry.com/app/billing": BILLING_PORTAL_UNAVAILABLE_BODY,
        "https://propertyquarry.com/sign-in": SIGN_IN_BODY,
    }
    attempts: dict[str, int] = {}

    def fetcher(url: str, _timeout: float) -> dict[str, object]:
        attempts[url] = attempts.get(url, 0) + 1
        if url.endswith("/app/account") and attempts[url] == 1:
            return {
                "status_code": 0,
                "final_url": url,
                "headers": {},
                "body": b"",
                "duration_ms": 8000,
                "error": "TimeoutError: timed out",
            }
        return _fake_response(bodies[url], final_url=url)

    receipt = build_live_authenticated_smoke_receipt(
        base_url="https://propertyquarry.com",
        api_token="token",
        principal_id="cf-email:tibor.girschele@gmail.com",
        expected_plan_label="Agent",
        retry_count=2,
        retry_backoff_seconds=0,
        fetcher=fetcher,
    )

    assert receipt["status"] == "pass"
    account_row = next(row for row in receipt["checks"] if row["path"] == "/app/account")
    assert account_row["attempt_count"] == 2


def test_live_authenticated_smoke_fetcher_uses_no_proxy_opener(monkeypatch) -> None:
    seen_handlers: list[tuple[object, ...]] = []

    class _FakeResponse:
        status = 200
        headers = {"Content-Type": "text/html; charset=utf-8", **SECURITY_HEADERS}

        def __init__(self, url: str) -> None:
            self._url = url

        def geturl(self) -> str:
            return self._url

        def read(self, _limit: int | None = None) -> bytes:
            return ACCOUNT_AGENT_BODY.encode("utf-8")

        def __enter__(self) -> "_FakeResponse":
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

    class _FakeOpener:
        def open(self, request, timeout=0):  # type: ignore[no-untyped-def]
            return _FakeResponse(request.full_url)

    def _fake_build_opener(*handlers):  # type: ignore[no-untyped-def]
        seen_handlers.append(tuple(handlers))
        return _FakeOpener()

    monkeypatch.setattr(authenticated_smoke.urllib.request, "build_opener", _fake_build_opener)

    receipt = build_live_authenticated_smoke_receipt(
        base_url="http://localhost:8097",
        api_token="token",
        principal_id="cf-email:tibor.girschele@gmail.com",
        expected_plan_label="Agent",
        routes=("/app/account",),
    )

    assert receipt["status"] == "pass"
    assert seen_handlers
    proxy_handler = next(
        handler for handler in seen_handlers[0] if isinstance(handler, urllib.request.ProxyHandler)
    )
    assert proxy_handler.proxies == {}


def test_live_authenticated_smoke_rejects_local_billing_board_without_network() -> None:
    bodies = {
        "https://propertyquarry.com/app/account": ACCOUNT_AGENT_BODY,
        "https://propertyquarry.com/app/billing": "PropertyQuarry Plan Agent Deep Multi All Billing history Compare plans View plans",
        "https://propertyquarry.com/sign-in": SIGN_IN_BODY,
    }

    receipt = build_live_authenticated_smoke_receipt(
        base_url="https://propertyquarry.com",
        api_token="token",
        principal_id="cf-email:tibor.girschele@gmail.com",
        expected_plan_label="Agent",
        fetcher=lambda url, _timeout: _fake_response(bodies[url], final_url=url),
    )

    assert receipt["status"] == "fail"
    billing_row = next(row for row in receipt["checks"] if row["path"] == "/app/billing")
    assert any(check["name"] == "billing_local_board_deleted" and check["ok"] is False for check in billing_row["checks"])
