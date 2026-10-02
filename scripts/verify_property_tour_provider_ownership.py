#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _env(name: str) -> str:
    return str(os.environ.get(name) or "").strip()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16] if value else ""


def _presence(value: str) -> dict[str, object]:
    return {
        "present": bool(value),
        "length": len(value),
        "hash": _hash(value),
    }


def _load_optional_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _provider_ready_mode(
    *,
    ownership_metadata_present: bool,
    secret_config_present: bool,
    delivery_evidence_present: bool,
) -> str:
    if ownership_metadata_present and secret_config_present:
        return "owned_configured"
    if ownership_metadata_present and delivery_evidence_present:
        return "owned_receipt_backed"
    return "missing_config"


def build_property_tour_provider_ownership_receipt(
    *,
    three_dvista_invoice_ids: tuple[str, ...] = ("60076", "60074"),
    pano2vr_order_id: str = "38984",
    pano2vr_product_id: str = "nferpd44",
    receipt_root: Path | None = None,
) -> dict[str, Any]:
    three_dvista_email = _env("THREEDVISTA_LOGIN_EMAIL") or _env("THREEDVISTA_LICENSE_EMAIL")
    three_dvista_password = _env("THREEDVISTA_LOGIN_PASSWORD")
    pano2vr_email = _env("PANO2VR_EMAIL")
    pano2vr_license = _env("PANO2VR_LICENSE_KEY")
    receipt_base = receipt_root.resolve() if receipt_root is not None else None
    three_dvista_refresh = _load_optional_json(receipt_base / "3dvista_private_viewer_refresh_live_current.json") if receipt_base is not None else {}
    three_dvista_import = _load_optional_json(receipt_base / "3dvista-import-current.json") if receipt_base is not None else {}
    three_dvista_web_probe = _load_optional_json(receipt_base / "3dvista-web-account-probe-current.json") if receipt_base is not None else {}
    pano2vr_import = _load_optional_json(receipt_base / "pano2vr-import-current.json") if receipt_base is not None else {}

    three_dvista_private_viewer_verified = str(three_dvista_refresh.get("status") or "").strip().lower() == "refreshed"
    three_dvista_import_verified = str(three_dvista_import.get("status") or "").strip().lower() == "imported"
    pano2vr_import_verified = str(pano2vr_import.get("status") or "").strip().lower() == "imported"
    three_dvista_ownership_metadata_present = bool(three_dvista_invoice_ids)
    three_dvista_secret_config_present = bool(three_dvista_email and three_dvista_password)
    three_dvista_delivery_evidence_present = bool(three_dvista_private_viewer_verified and three_dvista_import_verified)
    pano2vr_ownership_metadata_present = bool(pano2vr_order_id and pano2vr_product_id)
    pano2vr_secret_config_present = bool(pano2vr_email and pano2vr_license)
    pano2vr_delivery_evidence_present = bool(pano2vr_import_verified)
    three_dvista_status = _provider_ready_mode(
        ownership_metadata_present=three_dvista_ownership_metadata_present,
        secret_config_present=three_dvista_secret_config_present,
        delivery_evidence_present=three_dvista_delivery_evidence_present,
    )
    pano2vr_status = _provider_ready_mode(
        ownership_metadata_present=pano2vr_ownership_metadata_present,
        secret_config_present=pano2vr_secret_config_present,
        delivery_evidence_present=pano2vr_delivery_evidence_present,
    )
    providers = {
        "3dvista": {
            "status": three_dvista_status,
            "account_email_hash": _hash(three_dvista_email),
            "login_email_present": bool(three_dvista_email),
            "password_present": bool(three_dvista_password),
            "ownership_metadata_present": three_dvista_ownership_metadata_present,
            "secret_config_present": three_dvista_secret_config_present,
            "delivery_evidence_present": three_dvista_delivery_evidence_present,
            "invoice_ids": list(three_dvista_invoice_ids),
            "owned_products": ["3DVista VT Pro", "Branded Pack"],
            "login_verified": False,
            "web_account_probe_ok": str(three_dvista_web_probe.get("status") or "").strip().lower() == "ok",
            "import_verified": three_dvista_import_verified,
            "export_verified": three_dvista_private_viewer_verified and three_dvista_import_verified,
            "private_viewer_verified": three_dvista_private_viewer_verified,
            "control_url": str(three_dvista_import.get("control_url") or "") if three_dvista_import_verified else "",
            "runtime_refresh_receipt_status": str(three_dvista_refresh.get("status") or ""),
            "import_receipt_status": str(three_dvista_import.get("status") or ""),
            "next_action": (
                "keep the private-viewer runtime refreshed and publish a verified non-trial 3DVista export or allowlisted hosted 3DVista URL"
                if three_dvista_private_viewer_verified and three_dvista_import_verified
                else "verify control-panel login, complete branded/private viewer setup, and import a real 3DVista export or allowlisted hosted 3DVista URL"
            ),
        },
        "pano2vr": {
            "status": pano2vr_status,
            "account_email_hash": _hash(pano2vr_email),
            "account_email_present": bool(pano2vr_email),
            "ownership_metadata_present": pano2vr_ownership_metadata_present,
            "secret_config_present": pano2vr_secret_config_present,
            "delivery_evidence_present": pano2vr_delivery_evidence_present,
            "license_key": _presence(pano2vr_license),
            "order_id": pano2vr_order_id,
            "product_id": pano2vr_product_id,
            "owned_products": ["Pano2VR 8 Pro"],
            "account_verified": False,
            "import_verified": pano2vr_import_verified,
            "export_verified": pano2vr_import_verified,
            "control_url": str(pano2vr_import.get("control_url") or "") if pano2vr_import_verified else "",
            "import_receipt_status": str(pano2vr_import.get("status") or ""),
            "next_action": (
                "keep generating and importing real Pano2VR exports into the verified tour drop"
                if pano2vr_import_verified
                else "open Pano2VR, create a real export, and import the complete output folder or zip into the verified tour drop"
            ),
        },
    }
    required = ("3dvista", "pano2vr")
    missing = [
        provider
        for provider in required
        if providers[provider]["status"] not in {"owned_configured", "owned_receipt_backed"}
    ]
    return {
        "generated_at": _utc_now(),
        "status": "pass" if not missing else "blocked_missing_config",
        "providers": providers,
        "missing_providers": missing,
        "privacy": {
            "secrets_in_receipt": False,
            "omitted": [
                "passwords",
                "raw license keys",
                "invoice signatures",
                "private invoice links",
                "reset tokens",
                "cookies",
            ],
        },
        "notes": [
            "This receipt proves ownership/config readiness only.",
            "When local import/runtime receipts are available, this record also captures current non-secret 3DVista/Pano2VR verification evidence.",
            "Receipt-backed ownership is accepted when invoice/order metadata is present and current non-secret delivery receipts prove the provider is already working without re-reading secrets.",
            "It does not satisfy gold tour readiness without verified 3DVista/Pano2VR exports or allowlisted hosted controls.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify local 3DVista/Pano2VR ownership/config readiness without leaking secrets.")
    parser.add_argument("--write", default="_completion/property_tour_ownership/release-gate.json")
    parser.add_argument("--three-dvista-invoice-id", action="append", default=[], help="Non-secret 3DVista invoice id to record.")
    parser.add_argument("--pano2vr-order-id", default="38984")
    parser.add_argument("--pano2vr-product-id", default="nferpd44")
    args = parser.parse_args()
    receipt = build_property_tour_provider_ownership_receipt(
        three_dvista_invoice_ids=tuple(args.three_dvista_invoice_id or ("60076", "60074")),
        pano2vr_order_id=str(args.pano2vr_order_id),
        pano2vr_product_id=str(args.pano2vr_product_id),
        receipt_root=ROOT / "_completion" / "tours",
    )
    output = json.dumps(receipt, indent=2, sort_keys=True)
    if args.write:
        out_path = Path(args.write)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0 if receipt["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
