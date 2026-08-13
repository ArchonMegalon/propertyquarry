from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.routes import product_api_delivery as delivery
from app.services import property_billing


class _Response:
    def __init__(self, payload: dict[str, object], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, object]:
        return self._payload


def _paypal_order(
    *,
    principal_id: str,
    plan_key: str = "plus",
    order_id: str = "ORDER-1",
    status: str = "APPROVED",
    amount_eur: str = "3.00",
    capture_amount_eur: str | None = None,
) -> dict[str, object]:
    purchase_unit: dict[str, object] = {
        "reference_id": f"propertyquarry-{plan_key}",
        "custom_id": property_billing._paypal_principal_binding(
            principal_id=principal_id,
            plan_key=plan_key,
        ),
        "amount": {
            "currency_code": "EUR",
            "value": amount_eur,
        },
    }
    if status == "COMPLETED":
        purchase_unit["payments"] = {
            "captures": [
                {
                    "id": "CAPTURE-1",
                    "status": "COMPLETED",
                    "amount": {
                        "currency_code": "EUR",
                        "value": capture_amount_eur or amount_eur,
                    },
                    "create_time": "2026-08-13T08:30:00Z",
                }
            ]
        }
    return {
        "id": order_id,
        "status": status,
        "purchase_units": [purchase_unit],
        "payer": {"email_address": "sandbox-buyer@example.test"},
        "update_time": "2026-08-13T08:30:00Z",
    }


def test_paypal_create_uses_hashed_principal_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    principal_id = "cf-email:private-customer@example.test"
    seen: dict[str, object] = {}
    monkeypatch.setattr(property_billing, "_paypal_access_token", lambda: "token")
    monkeypatch.setattr(
        property_billing.requests,
        "post",
        lambda url, **kwargs: (
            seen.update({"url": url, **kwargs})
            or _Response(
                {
                    "id": "ORDER-1",
                    "status": "CREATED",
                    "links": [{"rel": "approve", "href": "https://www.sandbox.paypal.com/approve"}],
                },
                status_code=201,
            )
        ),
    )

    result = property_billing.create_paypal_property_order(
        principal_id=principal_id,
        plan_key="plus",
        return_url="https://propertyquarry.com/app/api/property/billing/return/plus",
        cancel_url="https://propertyquarry.com/app/api/property/billing/cancel/plus",
    )

    custom_id = str(dict(list(dict(seen["json"])["purchase_units"])[0])["custom_id"])
    assert result["order_id"] == "ORDER-1"
    assert principal_id not in custom_id
    assert custom_id == property_billing._paypal_principal_binding(
        principal_id=principal_id,
        plan_key="plus",
    )


def test_paypal_capture_rejects_cross_principal_order_before_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(property_billing, "_paypal_access_token", lambda: "token")
    monkeypatch.setattr(
        property_billing.requests,
        "get",
        lambda *_args, **_kwargs: _Response(
            _paypal_order(principal_id="another-principal")
        ),
    )
    monkeypatch.setattr(
        property_billing.requests,
        "post",
        lambda *_args, **_kwargs: pytest.fail("cross-principal order must not be captured"),
    )

    with pytest.raises(RuntimeError, match="paypal_order_principal_mismatch"):
        property_billing.capture_paypal_property_order(
            order_id="ORDER-1",
            principal_id="expected-principal",
            plan_key="plus",
        )


def test_paypal_capture_validates_exact_contract_and_uses_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "principal-1"
    seen: dict[str, object] = {}
    monkeypatch.setattr(property_billing, "_paypal_access_token", lambda: "token")
    monkeypatch.setattr(
        property_billing.requests,
        "get",
        lambda *_args, **_kwargs: _Response(_paypal_order(principal_id=principal_id)),
    )

    def capture(url: str, **kwargs: object) -> _Response:
        seen.update({"url": url, **kwargs})
        return _Response(
            _paypal_order(
                principal_id=principal_id,
                status="COMPLETED",
            )
        )

    monkeypatch.setattr(property_billing.requests, "post", capture)

    result = property_billing.capture_paypal_property_order(
        order_id="ORDER-1",
        principal_id=principal_id,
        plan_key="plus",
    )

    assert result == {
        "order_id": "ORDER-1",
        "capture_id": "CAPTURE-1",
        "payment_status": "completed",
        "payer_email": "sandbox-buyer@example.test",
        "amount_eur": "3.00",
        "currency": "EUR",
        "plan_key": "plus",
        "captured_at": "2026-08-13T08:30:00+00:00",
        "active_until": "2026-09-12T08:30:00+00:00",
        "replayed": False,
    }
    assert seen["json"] == {}
    assert str(dict(seen["headers"])["PayPal-Request-Id"]).startswith(
        "propertyquarry-capture-"
    )


def test_paypal_completed_order_replay_does_not_capture_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "principal-1"
    monkeypatch.setattr(property_billing, "_paypal_access_token", lambda: "token")
    monkeypatch.setattr(
        property_billing.requests,
        "get",
        lambda *_args, **_kwargs: _Response(
            _paypal_order(principal_id=principal_id, status="COMPLETED")
        ),
    )
    monkeypatch.setattr(
        property_billing.requests,
        "post",
        lambda *_args, **_kwargs: pytest.fail("completed order must not be captured twice"),
    )

    result = property_billing.capture_paypal_property_order(
        order_id="ORDER-1",
        principal_id=principal_id,
        plan_key="plus",
    )

    assert result["replayed"] is True
    assert result["capture_id"] == "CAPTURE-1"


def test_paypal_capture_rejects_wrong_captured_amount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "principal-1"
    monkeypatch.setattr(property_billing, "_paypal_access_token", lambda: "token")
    monkeypatch.setattr(
        property_billing.requests,
        "get",
        lambda *_args, **_kwargs: _Response(_paypal_order(principal_id=principal_id)),
    )
    monkeypatch.setattr(
        property_billing.requests,
        "post",
        lambda *_args, **_kwargs: _Response(
            _paypal_order(
                principal_id=principal_id,
                status="COMPLETED",
                capture_amount_eur="0.01",
            )
        ),
    )

    with pytest.raises(RuntimeError, match="paypal_order_capture_amount_mismatch"):
        property_billing.capture_paypal_property_order(
            order_id="ORDER-1",
            principal_id=principal_id,
            plan_key="plus",
        )


class _Onboarding:
    def __init__(self, commercial: dict[str, object]) -> None:
        self.commercial = commercial
        self.saved: dict[str, object] | None = None

    def status(self, *, principal_id: str) -> dict[str, object]:
        return {
            "property_search_preferences": {
                "city": "Vienna",
                "property_commercial": self.commercial,
            }
        }

    def upsert_property_search_preferences(
        self,
        *,
        principal_id: str,
        property_search_preferences_json: dict[str, object],
        trusted_commercial_update: bool,
    ) -> dict[str, object]:
        assert trusted_commercial_update is True
        self.saved = property_search_preferences_json
        return property_search_preferences_json


def test_capture_route_rejects_order_not_pending_for_signed_in_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    onboarding = _Onboarding(
        {
            "pending_order_id": "ORDER-OTHER",
            "pending_plan_key": "plus",
        }
    )
    monkeypatch.setattr(delivery, "paypal_configured", lambda: True)
    monkeypatch.setattr(
        delivery,
        "capture_paypal_property_order",
        lambda **_kwargs: pytest.fail("mismatched pending order must not reach PayPal"),
    )

    with pytest.raises(HTTPException) as exc_info:
        delivery.capture_property_billing_order(
            body=SimpleNamespace(plan_key="plus", order_id="ORDER-1"),
            container=SimpleNamespace(onboarding=onboarding),
            context=SimpleNamespace(principal_id="principal-1"),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "paypal_pending_order_mismatch"
    assert onboarding.saved is None


def test_capture_route_persists_only_validated_principal_bound_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    onboarding = _Onboarding(
        {
            "pending_order_id": "ORDER-1",
            "pending_plan_key": "plus",
        }
    )
    observed: dict[str, object] = {}
    monkeypatch.setattr(delivery, "paypal_configured", lambda: True)

    def capture(**kwargs: object) -> dict[str, object]:
        observed.update(kwargs)
        return {
            "order_id": "ORDER-1",
            "capture_id": "CAPTURE-1",
            "payment_status": "completed",
            "payer_email": "sandbox-buyer@example.test",
            "amount_eur": "3.00",
            "currency": "EUR",
            "plan_key": "plus",
            "captured_at": "2026-08-13T08:30:00+00:00",
            "active_until": "2026-09-12T08:30:00+00:00",
            "replayed": False,
        }

    monkeypatch.setattr(delivery, "capture_paypal_property_order", capture)

    result = delivery.capture_property_billing_order(
        body=SimpleNamespace(plan_key="plus", order_id="ORDER-1"),
        container=SimpleNamespace(onboarding=onboarding),
        context=SimpleNamespace(principal_id="principal-1"),
    )

    assert observed == {
        "order_id": "ORDER-1",
        "principal_id": "principal-1",
        "plan_key": "plus",
    }
    assert result.current_plan_key == "plus"
    assert result.active_until == "2026-09-12T08:30:00+00:00"
    assert onboarding.saved is not None
    saved = dict(dict(onboarding.saved)["property_commercial"])
    assert saved["last_order_id"] == "ORDER-1"
    assert saved["last_capture_id"] == "CAPTURE-1"
    assert saved["pending_order_id"] == ""
    assert saved["active_plan_key"] == "plus"


def test_capture_route_replay_returns_existing_entitlement_without_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    onboarding = _Onboarding(
        {
            "active_plan_key": "plus",
            "status": "active",
            "active_until": "2999-01-01T00:00:00+00:00",
            "last_order_id": "ORDER-1",
            "last_capture_id": "CAPTURE-1",
            "last_payment_status": "completed",
            "last_payment_amount_eur": "3.00",
            "last_payer_email": "sandbox-buyer@example.test",
        }
    )
    monkeypatch.setattr(delivery, "paypal_configured", lambda: True)
    monkeypatch.setattr(
        delivery,
        "capture_paypal_property_order",
        lambda **_kwargs: pytest.fail("local completed replay must not call provider"),
    )

    result = delivery.capture_property_billing_order(
        body=SimpleNamespace(plan_key="plus", order_id="ORDER-1"),
        container=SimpleNamespace(onboarding=onboarding),
        context=SimpleNamespace(principal_id="principal-1"),
    )

    assert result.capture_id == "CAPTURE-1"
    assert result.payment_status == "completed"
    assert result.active_until == "2999-01-01T00:00:00+00:00"
    assert onboarding.saved is None
