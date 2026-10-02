#!/usr/bin/env python3
"""Create and read back bounded PropertyQuarry PayPal sandbox orders."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Callable, Mapping, Sequence

if __package__:
    from .propertyquarry_live_external_authority_probe import _write_private_json
else:
    from propertyquarry_live_external_authority_probe import _write_private_json


SCHEMA = "propertyquarry.paypal_sandbox_canary.v1"
EA_API_CONTAINER = "ea-api"
SYNTHETIC_PRINCIPAL_LABEL = "propertyquarry-paypal-sandbox-canary-v1"
PAYPAL_PRINCIPAL_BINDING_VERSION = "v1"
PLAN_CONTRACT = {
    "agent": {"amount_eur": "99.00", "display_name": "Agent"},
    "plus": {"amount_eur": "3.00", "display_name": "Plus"},
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_PROVIDER_STATUS_RE = re.compile(r"^[A-Z_]{1,40}$")

Runner = Callable[..., Any]
Clock = Callable[[], float]


def _utc_iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _principal_sha256(label: str) -> str:
    normalized = str(label or "").strip()
    if not normalized or len(normalized) > 160:
        raise ValueError("sandbox_principal_label_invalid")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _principal_binding(*, principal_sha256: str, plan_key: str) -> str:
    normalized_digest = str(principal_sha256 or "").strip().lower()
    normalized_plan = str(plan_key or "").strip().lower()
    if not _SHA256_RE.fullmatch(normalized_digest):
        raise ValueError("sandbox_principal_digest_invalid")
    if normalized_plan not in PLAN_CONTRACT:
        raise ValueError("sandbox_plan_key_invalid")
    return ":".join(
        (
            "propertyquarry",
            PAYPAL_PRINCIPAL_BINDING_VERSION,
            normalized_plan,
            normalized_digest,
        )
    )


def _http_status(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return -1
    return parsed if 0 <= parsed <= 599 else -1


def _inner_source(*, principal_sha256: str) -> str:
    config = {
        "binding_contract": f"propertyquarry:{PAYPAL_PRINCIPAL_BINDING_VERSION}",
        "principal_bindings": {
            plan_key: _principal_binding(
                principal_sha256=principal_sha256,
                plan_key=plan_key,
            )
            for plan_key in PLAN_CONTRACT
        },
        "principal_sha256": principal_sha256,
        "plans": PLAN_CONTRACT,
        "schema": SCHEMA,
    }
    template = r'''
import hashlib
import json
import os
import urllib.parse

import requests

config = json.loads(__CONFIG_JSON__)
api_base = "https://api-m.sandbox.paypal.com"
client_id = str(os.getenv("PAYPAL_CLIENT_ID") or "").strip()
secret = str(os.getenv("PAYPAL_SECRET") or "").strip()


def safe_failure(code, *, token_status=0):
    return {
        "schema": config["schema"],
        "status": "fail",
        "state": code,
        "provider": "paypal",
        "api_environment": "sandbox",
        "principal_sha256": config["principal_sha256"],
        "synthetic_principal": True,
        "token_http_status": int(token_status),
        "plans": [],
        "approval_attempted": False,
        "capture_attempted": False,
        "entitlement_mutated": False,
        "webhook_claimed": False,
        "production_billing_enabled": False,
        "secret_values_recorded": False,
    }


if not client_id or not secret:
    print(json.dumps(safe_failure("credentials_not_configured"), sort_keys=True))
    raise SystemExit(0)

token_response = requests.post(
    f"{api_base}/v1/oauth2/token",
    auth=(client_id, secret),
    headers={"Accept": "application/json", "Accept-Language": "en_US"},
    data={"grant_type": "client_credentials"},
    timeout=30,
)
token_payload = token_response.json() if token_response.status_code < 400 else {}
token = str(token_payload.get("access_token") or "") if isinstance(token_payload, dict) else ""
if token_response.status_code >= 400 or not token:
    print(
        json.dumps(
            safe_failure(
                "sandbox_token_failed",
                token_status=token_response.status_code,
            ),
            sort_keys=True,
        )
    )
    raise SystemExit(0)

headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Prefer": "return=representation",
}
plan_receipts = []
for plan_key in sorted(config["plans"]):
    plan = config["plans"][plan_key]
    custom_id = config["principal_bindings"][plan_key]
    request_id = "pq-sbx-" + hashlib.sha256(
        f"{config['schema']}:{config['binding_contract']}:{config['principal_sha256']}:{plan_key}".encode("utf-8")
    ).hexdigest()[:30]
    payload = {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "reference_id": f"propertyquarry-{plan_key}",
                "description": f"PropertyQuarry {plan['display_name']} sandbox canary",
                "custom_id": custom_id,
                "amount": {
                    "currency_code": "EUR",
                    "value": plan["amount_eur"],
                },
            }
        ],
        "application_context": {
            "brand_name": "PropertyQuarry sandbox canary",
            "user_action": "PAY_NOW",
            "return_url": "https://propertyquarry.com/app/billing?canary=return",
            "cancel_url": "https://propertyquarry.com/app/billing?canary=cancel",
        },
    }
    create_headers = {**headers, "PayPal-Request-Id": request_id}
    created = requests.post(
        f"{api_base}/v2/checkout/orders",
        headers=create_headers,
        json=payload,
        timeout=30,
    )
    created_payload = created.json() if created.status_code < 400 else {}
    order_id = str(created_payload.get("id") or "") if isinstance(created_payload, dict) else ""
    created_units = created_payload.get("purchase_units") if isinstance(created_payload, dict) else []
    created_unit = dict(created_units[0]) if isinstance(created_units, list) and len(created_units) == 1 else {}
    created_amount = dict(created_unit.get("amount") or {})
    links = created_payload.get("links") if isinstance(created_payload, dict) else []
    approval_link_present = False
    if isinstance(links, list):
        for item in links:
            if not isinstance(item, dict) or str(item.get("rel") or "") != "approve":
                continue
            parsed = urllib.parse.urlsplit(str(item.get("href") or ""))
            if parsed.scheme == "https" and str(parsed.hostname or "").endswith(".sandbox.paypal.com"):
                approval_link_present = True
    create_verified = bool(
        created.status_code in {200, 201}
        and order_id
        and created_unit.get("reference_id") == f"propertyquarry-{plan_key}"
        and created_unit.get("custom_id") == custom_id
        and created_amount.get("currency_code") == "EUR"
        and created_amount.get("value") == plan["amount_eur"]
        and approval_link_present
    )
    read_status = 0
    read_verified = False
    provider_status = str(created_payload.get("status") or "") if isinstance(created_payload, dict) else ""
    if create_verified:
        readback = requests.get(
            f"{api_base}/v2/checkout/orders/{urllib.parse.quote(order_id, safe='')}",
            headers=headers,
            timeout=30,
        )
        read_status = int(readback.status_code)
        read_raw = readback.json() if readback.status_code < 400 else {}
        read_payload = read_raw if isinstance(read_raw, dict) else {}
        read_units = read_payload.get("purchase_units") if isinstance(read_payload, dict) else []
        read_unit = dict(read_units[0]) if isinstance(read_units, list) and len(read_units) == 1 else {}
        read_amount = dict(read_unit.get("amount") or {})
        read_verified = bool(
            readback.status_code == 200
            and read_payload.get("id") == order_id
            and read_unit.get("reference_id") == f"propertyquarry-{plan_key}"
            and read_unit.get("custom_id") == custom_id
            and read_amount.get("currency_code") == "EUR"
            and read_amount.get("value") == plan["amount_eur"]
        )
        provider_status = str(read_payload.get("status") or provider_status)
    plan_receipts.append(
        {
            "plan_key": plan_key,
            "amount_eur": plan["amount_eur"],
            "currency": "EUR",
            "create_http_status": int(created.status_code),
            "read_http_status": read_status,
            "provider_status": provider_status,
            "order_reference_sha256": hashlib.sha256(order_id.encode("utf-8")).hexdigest() if order_id else "",
            "idempotency_key_sha256": hashlib.sha256(request_id.encode("utf-8")).hexdigest(),
            "approval_link_present": approval_link_present,
            "create_payload_verified": create_verified,
            "readback_verified": read_verified,
        }
    )

passed = len(plan_receipts) == 2 and all(
    row["create_payload_verified"] and row["readback_verified"]
    for row in plan_receipts
)
receipt = {
    "schema": config["schema"],
    "status": "pass" if passed else "fail",
    "state": "sandbox_orders_verified" if passed else "sandbox_order_verification_failed",
    "provider": "paypal",
    "api_environment": "sandbox",
    "principal_sha256": config["principal_sha256"],
    "synthetic_principal": True,
    "token_http_status": int(token_response.status_code),
    "plans": plan_receipts,
    "approval_attempted": False,
    "capture_attempted": False,
    "entitlement_mutated": False,
    "webhook_claimed": False,
    "production_billing_enabled": False,
    "secret_values_recorded": False,
}
print(json.dumps(receipt, sort_keys=True))
'''
    return template.replace("__CONFIG_JSON__", repr(json.dumps(config, sort_keys=True)))


def _failure_receipt(
    state: str,
    *,
    principal_sha256: str,
    observed_at: str,
) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "observed_at": observed_at,
        "status": "fail",
        "state": state,
        "provider": "paypal",
        "api_environment": "sandbox",
        "principal_sha256": principal_sha256,
        "synthetic_principal": True,
        "token_http_status": 0,
        "plans": [],
        "approval_attempted": False,
        "capture_attempted": False,
        "entitlement_mutated": False,
        "webhook_claimed": False,
        "production_billing_enabled": False,
        "secret_values_recorded": False,
    }


def _validated_receipt(
    payload: object,
    *,
    principal_sha256: str,
    observed_at: str,
) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        return _failure_receipt(
            "runtime_receipt_invalid",
            principal_sha256=principal_sha256,
            observed_at=observed_at,
        )
    receipt = dict(payload)
    receipt["observed_at"] = observed_at
    expected_keys = {
        "api_environment",
        "approval_attempted",
        "capture_attempted",
        "entitlement_mutated",
        "observed_at",
        "plans",
        "principal_sha256",
        "production_billing_enabled",
        "provider",
        "schema",
        "secret_values_recorded",
        "state",
        "status",
        "synthetic_principal",
        "token_http_status",
        "webhook_claimed",
    }
    if (
        set(receipt) != expected_keys
        or receipt.get("schema") != SCHEMA
        or receipt.get("provider") != "paypal"
        or receipt.get("api_environment") != "sandbox"
        or receipt.get("principal_sha256") != principal_sha256
        or receipt.get("synthetic_principal") is not True
        or receipt.get("approval_attempted") is not False
        or receipt.get("capture_attempted") is not False
        or receipt.get("entitlement_mutated") is not False
        or receipt.get("webhook_claimed") is not False
        or receipt.get("production_billing_enabled") is not False
        or receipt.get("secret_values_recorded") is not False
        or receipt.get("status") not in {"pass", "fail"}
    ):
        return _failure_receipt(
            "runtime_receipt_invalid",
            principal_sha256=principal_sha256,
            observed_at=observed_at,
        )
    plans = receipt.get("plans")
    if receipt.get("status") == "pass":
        if (
            receipt.get("state") != "sandbox_orders_verified"
            or _http_status(receipt.get("token_http_status")) != 200
            or not isinstance(plans, list)
            or len(plans) != len(PLAN_CONTRACT)
        ):
            return _failure_receipt(
                "runtime_receipt_invalid",
                principal_sha256=principal_sha256,
                observed_at=observed_at,
            )
        observed_plans: set[str] = set()
        for row_value in plans:
            if not isinstance(row_value, Mapping):
                return _failure_receipt(
                    "runtime_receipt_invalid",
                    principal_sha256=principal_sha256,
                    observed_at=observed_at,
                )
            row = dict(row_value)
            plan_key = str(row.get("plan_key") or "")
            spec = PLAN_CONTRACT.get(plan_key)
            if (
                set(row)
                != {
                    "amount_eur",
                    "approval_link_present",
                    "create_http_status",
                    "create_payload_verified",
                    "currency",
                    "idempotency_key_sha256",
                    "order_reference_sha256",
                    "plan_key",
                    "provider_status",
                    "read_http_status",
                    "readback_verified",
                }
                or spec is None
                or plan_key in observed_plans
                or row.get("amount_eur") != spec["amount_eur"]
                or row.get("currency") != "EUR"
                or _http_status(row.get("create_http_status")) not in {200, 201}
                or _http_status(row.get("read_http_status")) != 200
                or not _SHA256_RE.fullmatch(
                    str(row.get("order_reference_sha256") or "")
                )
                or not _SHA256_RE.fullmatch(
                    str(row.get("idempotency_key_sha256") or "")
                )
                or row.get("approval_link_present") is not True
                or row.get("create_payload_verified") is not True
                or row.get("readback_verified") is not True
                or not _SAFE_PROVIDER_STATUS_RE.fullmatch(
                    str(row.get("provider_status") or "")
                )
            ):
                return _failure_receipt(
                    "runtime_receipt_invalid",
                    principal_sha256=principal_sha256,
                    observed_at=observed_at,
                )
            observed_plans.add(plan_key)
    elif receipt.get("state") in {
        "credentials_not_configured",
        "sandbox_token_failed",
    }:
        if plans != []:
            return _failure_receipt(
                "runtime_receipt_invalid",
                principal_sha256=principal_sha256,
                observed_at=observed_at,
            )
    elif receipt.get("state") == "sandbox_order_verification_failed":
        if not isinstance(plans, list) or len(plans) != len(PLAN_CONTRACT):
            return _failure_receipt(
                "runtime_receipt_invalid",
                principal_sha256=principal_sha256,
                observed_at=observed_at,
            )
        failed_plan_keys: set[str] = set()
        for row_value in plans:
            if not isinstance(row_value, Mapping):
                return _failure_receipt(
                    "runtime_receipt_invalid",
                    principal_sha256=principal_sha256,
                    observed_at=observed_at,
                )
            row = dict(row_value)
            plan_key = str(row.get("plan_key") or "")
            spec = PLAN_CONTRACT.get(plan_key)
            order_sha256 = str(row.get("order_reference_sha256") or "")
            provider_status = str(row.get("provider_status") or "")
            if (
                set(row)
                != {
                    "amount_eur",
                    "approval_link_present",
                    "create_http_status",
                    "create_payload_verified",
                    "currency",
                    "idempotency_key_sha256",
                    "order_reference_sha256",
                    "plan_key",
                    "provider_status",
                    "read_http_status",
                    "readback_verified",
                }
                or spec is None
                or plan_key in failed_plan_keys
                or row.get("amount_eur") != spec["amount_eur"]
                or row.get("currency") != "EUR"
                or _http_status(row.get("create_http_status")) < 0
                or _http_status(row.get("read_http_status")) < 0
                or (order_sha256 and not _SHA256_RE.fullmatch(order_sha256))
                or not _SHA256_RE.fullmatch(
                    str(row.get("idempotency_key_sha256") or "")
                )
                or (
                    provider_status
                    and not _SAFE_PROVIDER_STATUS_RE.fullmatch(provider_status)
                )
                or not isinstance(row.get("approval_link_present"), bool)
                or not isinstance(row.get("create_payload_verified"), bool)
                or not isinstance(row.get("readback_verified"), bool)
            ):
                return _failure_receipt(
                    "runtime_receipt_invalid",
                    principal_sha256=principal_sha256,
                    observed_at=observed_at,
                )
            failed_plan_keys.add(plan_key)
    else:
        return _failure_receipt(
            "runtime_receipt_invalid",
            principal_sha256=principal_sha256,
            observed_at=observed_at,
        )
    return receipt


def execute_paypal_sandbox_canary(
    *,
    principal_label: str = SYNTHETIC_PRINCIPAL_LABEL,
    runner: Runner = subprocess.run,
    clock: Clock = time.time,
) -> dict[str, object]:
    principal_sha256 = _principal_sha256(principal_label)
    observed_at = _utc_iso(clock())
    source = _inner_source(principal_sha256=principal_sha256)
    try:
        result = runner(
            ["docker", "exec", "-i", EA_API_CONTAINER, "python", "-"],
            input=source,
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
        if int(getattr(result, "returncode", 1)) != 0:
            return _failure_receipt(
                "runtime_probe_failed",
                principal_sha256=principal_sha256,
                observed_at=observed_at,
            )
        payload = json.loads(str(getattr(result, "stdout", "") or ""))
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        return _failure_receipt(
            "runtime_probe_failed",
            principal_sha256=principal_sha256,
            observed_at=observed_at,
        )
    return _validated_receipt(
        payload,
        principal_sha256=principal_sha256,
        observed_at=observed_at,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument(
        "--principal-label",
        default=SYNTHETIC_PRINCIPAL_LABEL,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    receipt = execute_paypal_sandbox_canary(principal_label=args.principal_label)
    _write_private_json(args.receipt, receipt)
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt.get("status") == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
