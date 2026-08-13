from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.routes import product_api_delivery as delivery
from app.services import property_billing


_RELEASE_COMMIT = "1" * 40
_RELEASE_IMAGE = "sha256:" + "2" * 64
_RECEIPT_DIGEST = "sha256:" + "3" * 64
_PRINCIPAL_DIGEST = "sha256:" + "4" * 64


def _install_safe_handoff(
    monkeypatch: pytest.MonkeyPatch,
    *,
    provider: str,
    verified_at: datetime | None = None,
) -> None:
    monkeypatch.setenv(
        "PROPERTYQUARRY_PAID_BILLING_SAFE_HANDOFF_CONTRACT",
        "propertyquarry.paid_billing_safe_handoff.v1",
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_PAID_BILLING_SAFE_HANDOFF_PROVIDER",
        provider,
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_PAID_BILLING_SAFE_HANDOFF_PROVIDER_ENVIRONMENT",
        "live",
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_PAID_BILLING_SAFE_HANDOFF_PLAN_KEYS",
        "agent,plus",
    )
    monkeypatch.setenv("PROPERTYQUARRY_RELEASE_COMMIT_SHA", _RELEASE_COMMIT)
    monkeypatch.setenv(
        "PROPERTYQUARRY_PAID_BILLING_SAFE_HANDOFF_RELEASE_COMMIT_SHA",
        _RELEASE_COMMIT,
    )
    monkeypatch.setenv("PROPERTYQUARRY_RELEASE_IMAGE_DIGEST", _RELEASE_IMAGE)
    monkeypatch.setenv(
        "PROPERTYQUARRY_PAID_BILLING_SAFE_HANDOFF_RELEASE_IMAGE_DIGEST",
        _RELEASE_IMAGE,
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_PAID_BILLING_SAFE_HANDOFF_RECEIPT_SHA256",
        _RECEIPT_DIGEST,
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_PAID_BILLING_SAFE_HANDOFF_PRINCIPAL_SHA256",
        _PRINCIPAL_DIGEST,
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_PAID_BILLING_SAFE_HANDOFF_VERIFIED_AT",
        (verified_at or datetime.now(timezone.utc)).isoformat(),
    )


def test_provider_credentials_alone_do_not_activate_customer_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_ENABLE_PAYPAL_CHECKOUT", "true")
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "configured-client")
    monkeypatch.setenv("PAYPAL_SECRET", "configured-secret")
    monkeypatch.setenv("PAYFUNNELS_WEBHOOK_SECRET", "configured-webhook")
    monkeypatch.setenv("PAYFUNNELS_API_KEY", "configured-api-key")

    assert property_billing.paypal_configured() is False
    assert property_billing.payfunnels_configured(plan_key="plus") is False


def test_exact_release_handoff_activates_only_the_canaried_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_ENABLE_PAYPAL_CHECKOUT", "true")
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "configured-client")
    monkeypatch.setenv("PAYPAL_SECRET", "configured-secret")
    monkeypatch.setenv("PAYFUNNELS_WEBHOOK_SECRET", "configured-webhook")
    monkeypatch.setenv("PAYFUNNELS_API_KEY", "configured-api-key")
    _install_safe_handoff(monkeypatch, provider="payfunnels")

    assert property_billing.payfunnels_configured(plan_key="plus") is True
    assert property_billing.payfunnels_configured(plan_key="agent") is True
    assert property_billing.paypal_configured() is False


def test_exact_release_handoff_activates_paypal_after_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_ENABLE_PAYPAL_CHECKOUT", "true")
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "configured-client")
    monkeypatch.setenv("PAYPAL_SECRET", "configured-secret")
    monkeypatch.setenv("PAYPAL_API_BASE", "https://api-m.paypal.com")
    _install_safe_handoff(monkeypatch, provider="paypal")

    assert property_billing.paypal_configured() is True


def test_sandbox_paypal_canary_cannot_activate_customer_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_ENABLE_PAYPAL_CHECKOUT", "true")
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "sandbox-client")
    monkeypatch.setenv("PAYPAL_SECRET", "sandbox-secret")
    monkeypatch.setenv(
        "PAYPAL_API_BASE",
        "https://api-m.sandbox.paypal.com",
    )
    _install_safe_handoff(monkeypatch, provider="paypal")

    assert property_billing.paypal_api_environment() == "sandbox"
    assert property_billing.paypal_configured() is False


def test_paid_handoff_requires_live_provider_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_safe_handoff(monkeypatch, provider="paypal")
    monkeypatch.setenv(
        "PROPERTYQUARRY_PAID_BILLING_SAFE_HANDOFF_PROVIDER_ENVIRONMENT",
        "sandbox",
    )

    assert (
        property_billing.paid_billing_safe_handoff_configured(
            provider="paypal"
        )
        is False
    )


def test_paypal_rejects_non_official_api_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_ENABLE_PAYPAL_CHECKOUT", "true")
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "configured-client")
    monkeypatch.setenv("PAYPAL_SECRET", "configured-secret")
    monkeypatch.setenv("PAYPAL_API_BASE", "https://payments.example.test")
    _install_safe_handoff(monkeypatch, provider="paypal")

    assert property_billing.paypal_api_environment() == ""
    assert property_billing.paypal_configured() is False
    with pytest.raises(RuntimeError, match="paypal_api_base_invalid"):
        property_billing._paypal_api_base()


def test_stale_or_cross_release_handoff_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_safe_handoff(
        monkeypatch,
        provider="paypal",
        verified_at=datetime.now(timezone.utc) - timedelta(hours=25),
    )

    assert (
        property_billing.paid_billing_safe_handoff_configured(
            provider="paypal"
        )
        is False
    )

    _install_safe_handoff(monkeypatch, provider="paypal")
    monkeypatch.setenv("PROPERTYQUARRY_RELEASE_COMMIT_SHA", "5" * 40)
    assert (
        property_billing.paid_billing_safe_handoff_configured(
            provider="paypal"
        )
        is False
    )


def test_generic_checkout_prefers_payfunnels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(delivery, "payfunnels_configured", lambda **_kwargs: True)
    monkeypatch.setattr(delivery, "paypal_configured", lambda: True)

    assert delivery._property_billing_checkout_provider(plan_key="plus") == "payfunnels"


def test_generic_checkout_falls_back_to_paypal(monkeypatch: pytest.MonkeyPatch) -> None:
    body = SimpleNamespace(plan_key="plus")
    expected = object()
    monkeypatch.setattr(delivery, "payfunnels_configured", lambda **_kwargs: False)
    monkeypatch.setattr(delivery, "paypal_configured", lambda: True)
    monkeypatch.setattr(delivery, "create_property_billing_order", lambda **_kwargs: expected)

    result = delivery.create_property_billing_checkout_order(
        body=body,
        request=object(),
        container=object(),
        context=object(),
    )

    assert result is expected


def test_generic_checkout_fails_closed_without_a_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(delivery, "payfunnels_configured", lambda **_kwargs: False)
    monkeypatch.setattr(delivery, "paypal_configured", lambda: False)

    with pytest.raises(HTTPException) as exc_info:
        delivery.create_property_billing_checkout_order(
            body=SimpleNamespace(plan_key="plus"),
            request=object(),
            container=object(),
            context=object(),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "property_billing_not_configured"


def test_paypal_capture_fails_closed_when_safe_handoff_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(delivery, "paypal_configured", lambda: False)
    monkeypatch.setattr(
        delivery,
        "capture_paypal_property_order",
        lambda **_kwargs: pytest.fail("provider capture must not be attempted"),
    )

    with pytest.raises(HTTPException) as exc_info:
        delivery.capture_property_billing_order(
            body=SimpleNamespace(plan_key="plus", order_id="order-1"),
            container=object(),
            context=object(),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "paypal_not_configured"
