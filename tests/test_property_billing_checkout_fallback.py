from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.routes import product_api_delivery as delivery


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
