from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from ea.app.services.property_billing import (
    _paypal_principal_binding,
    property_plan_spec,
)
from scripts import propertyquarry_paypal_sandbox_canary as canary


def _result(*, stdout: str = "", returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


def _passing_payload(principal_sha256: str) -> dict[str, object]:
    plans = []
    for plan_key, spec in sorted(canary.PLAN_CONTRACT.items()):
        plans.append(
            {
                "plan_key": plan_key,
                "amount_eur": spec["amount_eur"],
                "currency": "EUR",
                "create_http_status": 201,
                "read_http_status": 200,
                "provider_status": "CREATED",
                "order_reference_sha256": hashlib.sha256(
                    f"order:{plan_key}".encode()
                ).hexdigest(),
                "idempotency_key_sha256": hashlib.sha256(
                    f"request:{plan_key}".encode()
                ).hexdigest(),
                "approval_link_present": True,
                "create_payload_verified": True,
                "readback_verified": True,
            }
        )
    return {
        "schema": canary.SCHEMA,
        "status": "pass",
        "state": "sandbox_orders_verified",
        "provider": "paypal",
        "api_environment": "sandbox",
        "principal_sha256": principal_sha256,
        "synthetic_principal": True,
        "token_http_status": 200,
        "plans": plans,
        "approval_attempted": False,
        "capture_attempted": False,
        "entitlement_mutated": False,
        "webhook_claimed": False,
        "production_billing_enabled": False,
        "secret_values_recorded": False,
    }


def test_canary_prices_match_the_customer_billing_catalog() -> None:
    for plan_key, contract in canary.PLAN_CONTRACT.items():
        spec = property_plan_spec(plan_key)
        assert contract == {
            "amount_eur": spec.amount_eur,
            "display_name": spec.display_name,
        }


def test_canary_uses_production_principal_binding_contract() -> None:
    principal_sha256 = canary._principal_sha256(canary.SYNTHETIC_PRINCIPAL_LABEL)
    for plan_key in sorted(canary.PLAN_CONTRACT):
        assert canary._principal_binding(
            principal_sha256=principal_sha256,
            plan_key=plan_key,
        ) == _paypal_principal_binding(
            principal_id=canary.SYNTHETIC_PRINCIPAL_LABEL,
            plan_key=plan_key,
        )


def test_canary_accepts_exact_safe_provider_receipt_without_exposing_secrets() -> None:
    principal_label = "private-test-principal-label"
    principal_sha256 = hashlib.sha256(principal_label.encode()).hexdigest()
    observed_sources: list[str] = []

    def runner(command, **kwargs):
        assert list(command) == ["docker", "exec", "-i", "ea-api", "python", "-"]
        observed_sources.append(str(kwargs["input"]))
        return _result(stdout=json.dumps(_passing_payload(principal_sha256)))

    receipt = canary.execute_paypal_sandbox_canary(
        principal_label=principal_label,
        runner=runner,
        clock=lambda: 1_786_604_800.0,
    )

    assert receipt["status"] == "pass"
    assert receipt["observed_at"] == "2026-08-13T07:06:40Z"
    assert receipt["principal_sha256"] == principal_sha256
    assert receipt["synthetic_principal"] is True
    assert receipt["capture_attempted"] is False
    assert receipt["production_billing_enabled"] is False
    assert [row["plan_key"] for row in receipt["plans"]] == ["agent", "plus"]
    assert principal_label not in json.dumps(receipt)
    assert observed_sources and principal_label not in observed_sources[0]
    compile(observed_sources[0], "<paypal-sandbox-canary>", "exec")
    assert "/capture" not in observed_sources[0]


def test_canary_rejects_provider_receipt_with_wrong_price_or_capture_claim() -> None:
    principal_sha256 = canary._principal_sha256(canary.SYNTHETIC_PRINCIPAL_LABEL)
    payload = _passing_payload(principal_sha256)
    payload["plans"][0]["amount_eur"] = "0.01"
    payload["capture_attempted"] = True

    receipt = canary.execute_paypal_sandbox_canary(
        runner=lambda *_args, **_kwargs: _result(stdout=json.dumps(payload)),
        clock=lambda: 1_786_604_800.0,
    )

    assert receipt["status"] == "fail"
    assert receipt["state"] == "runtime_receipt_invalid"
    assert receipt["plans"] == []
    assert receipt["capture_attempted"] is False


def test_cli_writes_private_receipt(tmp_path: Path, monkeypatch) -> None:
    principal_sha256 = canary._principal_sha256(canary.SYNTHETIC_PRINCIPAL_LABEL)
    monkeypatch.setattr(
        canary,
        "execute_paypal_sandbox_canary",
        lambda **_kwargs: {
            **_passing_payload(principal_sha256),
            "observed_at": "2026-08-13T00:00:00Z",
        },
    )
    target = tmp_path / "private" / "receipt.json"

    assert canary.main(["--receipt", str(target)]) == 0
    assert target.stat().st_mode & 0o777 == 0o600
    assert json.loads(target.read_text(encoding="utf-8"))["status"] == "pass"
