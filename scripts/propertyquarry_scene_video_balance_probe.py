#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import requests
from playwright.sync_api import sync_playwright

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import render_magicai_model_upload_adapter as omagic_adapter
from property_magicfit_env import (
    _load_accounts_from_json_text,
    _resolve_accounts_json_file_path,
    discover_magicfit_env,
)
from propertyquarry_playwright_runtime import playwright_chromium_launch_kwargs
from render_magicfit_property_flythrough import MAGICFIT_VIDEO_URL, maybe_login, visible_body_text


DEFAULT_SHARED_ENV_FILE = Path("state/runtime/property_scene_video_shared.env")
SUPPORTED_PROVIDERS = ("magicfit", "omagic")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _configured_accounts(
    values: dict[str, str],
    *,
    inline_names: tuple[str, ...],
    file_names: tuple[str, ...],
) -> list[dict[str, str]]:
    loaded: list[dict[str, object]] = []
    source = ""
    for name in file_names:
        raw_path = str(values.get(name) or "").strip()
        if not raw_path:
            continue
        path = _resolve_accounts_json_file_path(raw_path)
        if path.is_file():
            with contextlib.suppress(Exception):
                loaded = _load_accounts_from_json_text(path.read_text(encoding="utf-8"))
            if loaded:
                source = "file"
                break
    if not loaded:
        for name in inline_names:
            loaded = _load_accounts_from_json_text(str(values.get(name) or ""))
            if loaded:
                source = "inline_env"
                break
    accounts: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in loaded:
        email = str(row.get("email") or row.get("username") or row.get("login") or "").strip()
        password = str(row.get("password") or row.get("pass") or "").strip()
        if not email or not password or email.lower() in seen:
            continue
        seen.add(email.lower())
        accounts.append({"email": email, "password": password, "source": source})
    return accounts


@contextlib.contextmanager
def _magicfit_account_env(account: dict[str, str]) -> Iterator[None]:
    names = (
        "PROPERTYQUARRY_MAGICFIT_EMAIL",
        "MAGICFIT_EMAIL",
        "PROPERTYQUARRY_MAGICFIT_PASSWORD",
        "MAGICFIT_PASSWORD",
    )
    previous = {name: os.environ.get(name) for name in names}
    os.environ["PROPERTYQUARRY_MAGICFIT_EMAIL"] = account["email"]
    os.environ["MAGICFIT_EMAIL"] = account["email"]
    os.environ["PROPERTYQUARRY_MAGICFIT_PASSWORD"] = account["password"]
    os.environ["MAGICFIT_PASSWORD"] = account["password"]
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _magicfit_credit_snapshot(body_text: str) -> dict[str, object]:
    normalized = str(body_text or "")
    if re.search(r"\bnot enough credits\b|\binsufficient credits\b", normalized, flags=re.IGNORECASE):
        return {
            "credit_state": "depleted",
            "remaining": 0,
            "unit": "credits",
            "credit_ui_present": True,
        }
    explicit_balance = re.search(r"(?P<amount>\d[\d\s,.]*)\s+credits?\b", normalized, flags=re.IGNORECASE)
    if explicit_balance is not None:
        digits = re.sub(r"[^0-9]", "", explicit_balance.group("amount"))
        if digits:
            remaining = int(digits)
            return {
                "credit_state": "funded" if remaining > 0 else "depleted",
                "remaining": remaining,
                "unit": "credits",
                "credit_ui_present": True,
            }
    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    credit_action_present = any(re.search(r"buy credits|get more credits", line, flags=re.IGNORECASE) for line in lines)
    numeric_candidates: list[int] = []
    for line in lines:
        if len(line) > 24 or not re.fullmatch(r"[\d\s,.]+", line):
            continue
        digits = re.sub(r"[^0-9]", "", line)
        if digits:
            numeric_candidates.append(int(digits))
    for index, line in enumerate(lines):
        if not re.search(r"buy credits|get more credits", line, flags=re.IGNORECASE):
            continue
        for candidate in reversed(lines[max(0, index - 12) : index]):
            if not re.fullmatch(r"[\d\s,.]+", candidate):
                continue
            digits = re.sub(r"[^0-9]", "", candidate)
            if digits:
                remaining = int(digits)
                return {
                    "credit_state": "funded" if remaining > 0 else "depleted",
                    "remaining": remaining,
                    "unit": "credits",
                    "credit_ui_present": True,
                }
    if credit_action_present and numeric_candidates:
        remaining = max(numeric_candidates)
        return {
            "credit_state": "funded" if remaining > 0 else "depleted",
            "remaining": remaining,
            "unit": "credits",
            "credit_ui_present": True,
        }
    return {
        "credit_state": "unprobed",
        "remaining": None,
        "unit": "credits",
        "credit_ui_present": credit_action_present,
    }


def _probe_magicfit_account(browser, *, account: dict[str, str], index: int, max_attempts: int = 2) -> dict[str, object]:
    started_at = time.monotonic()
    last_result: dict[str, object] = {
        "account_label": f"account-{index}",
        "status": "fail",
        "source": "magicfit_browser_ui",
        "credit_state": "unprobed",
        "remaining": None,
        "unit": "credits",
    }
    for attempt in range(1, max(1, int(max_attempts)) + 1):
        context = browser.new_context(viewport={"width": 1440, "height": 1100})
        page = context.new_page()
        try:
            with _magicfit_account_env(account):
                maybe_login(page)
            page.goto(MAGICFIT_VIDEO_URL, wait_until="domcontentloaded", timeout=120000)
            page.wait_for_timeout(6000)
            snapshot = _magicfit_credit_snapshot(visible_body_text(page))
            last_result = {
                "account_label": f"account-{index}",
                "status": "pass" if snapshot["credit_state"] in {"funded", "depleted"} else "fail",
                "source": "magicfit_browser_ui",
                "attempt_count": attempt,
                "elapsed_seconds": round(time.monotonic() - started_at, 2),
                **snapshot,
            }
            if last_result["status"] == "pass":
                return last_result
        except Exception as exc:  # noqa: BLE001
            last_result = {
                "account_label": f"account-{index}",
                "status": "fail",
                "source": "magicfit_browser_ui",
                "credit_state": "unprobed",
                "remaining": None,
                "unit": "credits",
                "attempt_count": attempt,
                "elapsed_seconds": round(time.monotonic() - started_at, 2),
                "error": f"{type(exc).__name__}: {exc}"[:240],
            }
        finally:
            context.close()
    return last_result


def _probe_magicfit(values: dict[str, str]) -> dict[str, object]:
    accounts = _configured_accounts(
        values,
        inline_names=("PROPERTYQUARRY_MAGICFIT_ACCOUNTS_JSON", "MAGICFIT_ACCOUNTS_JSON"),
        file_names=("PROPERTYQUARRY_MAGICFIT_ACCOUNTS_JSON_FILE", "MAGICFIT_ACCOUNTS_JSON_FILE"),
    )
    if not accounts:
        return {
            "provider": "magicfit",
            "status": "fail",
            "reason": "magicfit_accounts_missing",
            "account_count": 0,
            "accounts": [],
        }
    account_results: list[dict[str, object]] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            **playwright_chromium_launch_kwargs(playwright, args=["--no-sandbox"])
        )
        try:
            for index, account in enumerate(accounts, start=1):
                account_results.append(_probe_magicfit_account(browser, account=account, index=index))
        finally:
            browser.close()
    funded = [row for row in account_results if row.get("credit_state") == "funded"]
    probed = [row for row in account_results if row.get("credit_state") in {"funded", "depleted"}]
    return {
        "provider": "magicfit",
        "status": "pass" if funded and len(probed) == len(account_results) else "fail",
        "account_count": len(account_results),
        "funded_account_count": len(funded),
        "total_remaining": sum(int(row.get("remaining") or 0) for row in funded),
        "unit": "credits",
        "accounts": account_results,
    }


def _probe_omagic(values: dict[str, str]) -> dict[str, object]:
    for key, value in values.items():
        os.environ.setdefault(key, value)
    accounts = _configured_accounts(
        values,
        inline_names=(
            "PROPERTYQUARRY_OMAGIC_ACCOUNTS_JSON",
            "OMAGIC_ACCOUNTS_JSON",
            "PROPERTYQUARRY_MAGIC_ACCOUNTS_JSON",
            "MAGIC_ACCOUNTS_JSON",
        ),
        file_names=(
            "PROPERTYQUARRY_OMAGIC_ACCOUNTS_JSON_FILE",
            "OMAGIC_ACCOUNTS_JSON_FILE",
            "PROPERTYQUARRY_MAGIC_ACCOUNTS_JSON_FILE",
            "MAGIC_ACCOUNTS_JSON_FILE",
        ),
    )
    _api_key_name, api_key = omagic_adapter._first_env(omagic_adapter.API_KEY_ENV_NAMES)
    _api_base_name, api_base_url = omagic_adapter._resolve_api_base_url()
    if not api_key:
        return {
            "provider": "omagic",
            "status": "fail",
            "reason": "omagic_api_key_missing",
            "runtime_account_count": len(accounts),
        }
    session = requests.Session()
    session.headers.update(omagic_adapter._request_headers(api_key))
    try:
        template = omagic_adapter._discover_template(session, api_base_url=api_base_url)
    except Exception as exc:  # noqa: BLE001
        return {
            "provider": "omagic",
            "status": "fail",
            "reason": "omagic_template_discovery_failed",
            "error": f"{type(exc).__name__}: {exc}"[:240],
            "runtime_account_count": len(accounts),
        }
    model_argument_name = str(template.get("d3_argument_name") or "").strip()
    variant_id = str(template.get("template_variant_id") or "").strip()
    return {
        "provider": "omagic",
        "provider_backend_key": "omagic",
        "status": "pass" if variant_id and model_argument_name else "fail",
        "capability_state": "authenticated_model_template_ready",
        "credit_state": "not_exposed_by_configured_api",
        "runtime_account_count": len(accounts),
        "template_variant_id": variant_id,
        "model_argument_name": model_argument_name,
        "selection_source": str(template.get("selection_source") or "").strip(),
        "source": "omagic_authenticated_template_discovery",
    }


def build_balance_probe_receipt(
    *,
    providers: tuple[str, ...],
    shared_env_file: Path,
    release_commit_sha: str = "",
    image_digest: str = "",
) -> dict[str, object]:
    values, _sources = discover_magicfit_env((shared_env_file,))
    results: list[dict[str, object]] = []
    for provider in providers:
        if provider == "magicfit":
            results.append(_probe_magicfit(values))
        elif provider == "omagic":
            results.append(_probe_omagic(values))
    failed = [row for row in results if row.get("status") != "pass"]
    return {
        "contract_name": "propertyquarry.scene_video_provider_balance_probe.v1",
        "generated_at": _utc_now(),
        "release_commit_sha": str(release_commit_sha or "").strip().lower(),
        "image_digest": str(image_digest or "").strip().lower(),
        "status": "pass" if results and not failed else "fail",
        "failed_count": len(failed),
        "provider_count": len(results),
        "providers": list(providers),
        "render_submitted": False,
        "quota_consumed": False,
        "provider_results": results,
        "notes": [
            "MagicFit balances are read from the authenticated generator UI without submitting its form.",
            "OMagic proves authenticated model-template eligibility only; its configured API does not expose a balance endpoint.",
            "This receipt is readiness evidence, not provider-authored walkthrough proof.",
        ],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Probe PropertyQuarry scene-video provider state without rendering.")
    parser.add_argument("--providers", default=",".join(SUPPORTED_PROVIDERS))
    parser.add_argument("--shared-env-file", default=str(DEFAULT_SHARED_ENV_FILE))
    parser.add_argument("--release-commit-sha", default="")
    parser.add_argument("--image-digest", default="")
    parser.add_argument("--write", default="")
    return parser


def main() -> int:
    args = _parser().parse_args()
    providers = tuple(
        dict.fromkeys(
            token.strip().lower()
            for token in str(args.providers or "").split(",")
            if token.strip().lower() in SUPPORTED_PROVIDERS
        )
    )
    receipt = build_balance_probe_receipt(
        providers=providers,
        shared_env_file=Path(args.shared_env_file).expanduser(),
        release_commit_sha=args.release_commit_sha,
        image_digest=args.image_digest,
    )
    output = json.dumps(receipt, indent=2, sort_keys=True)
    if args.write:
        target = Path(args.write).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{output}\n", encoding="utf-8")
    print(output)
    return 0 if receipt["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
