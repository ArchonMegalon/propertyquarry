#!/usr/bin/env python3
from __future__ import annotations

import argparse
import email
import hashlib
import imaplib
import ipaddress
import json
import os
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
EA_ROOT = ROOT / "ea"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(EA_ROOT) not in sys.path:
    sys.path.insert(0, str(EA_ROOT))

from scripts.propertyquarry_playwright_runtime import (  # noqa: E402
    normalize_playwright_engine,
    playwright_browser_type,
    playwright_engine_launch_kwargs,
)


REQUIRED_JOURNEY_STEPS = (
    "landing",
    "real_authentication",
    "account_create_or_reopen",
    "first_real_search",
    "real_provider_results",
    "shortlist",
    "research",
    "walkthrough_request_or_reuse",
    "walkthrough_ready",
    "logout",
    "relogin",
    "safe_cleanup",
)
SUPPORTED_AUTH_MODES = ("google", "email_link")
DEFAULT_STATE_PATH = Path("_completion/activation_to_value/run-state.json")


def _env_flag(name: str, *, default: bool = False) -> bool:
    raw = str(os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(repr=False)
class ActivationJourneyConfig:
    base_url: str
    persona_id: str
    persona_email: str
    run_key: str
    release_commit_sha: str
    auth_mode: str = "google"
    browser_engine: str = "chromium"
    expected_account_state: str = "existing"
    allow_account_create: bool = False
    allow_walkthrough_request: bool = False
    search_country_code: str = ""
    search_region_code: str = ""
    search_location: str = ""
    visual_style: str = "Urban jungle"
    provider_password: str = field(default="", repr=False)
    imap_host: str = field(default="", repr=False)
    imap_port: int = 993
    imap_username: str = field(default="", repr=False)
    imap_password: str = field(default="", repr=False)
    imap_mailbox: str = "INBOX"
    auth_timeout_seconds: int = 180
    search_timeout_seconds: int = 900
    walkthrough_timeout_seconds: int = 1200
    allowed_host_suffixes: tuple[str, ...] = ("propertyquarry.com",)
    state_path: Path = DEFAULT_STATE_PATH
    live_authorized: bool = False

    @property
    def normalized_base_url(self) -> str:
        return self.base_url.strip().rstrip("/")

    @property
    def persona_digest(self) -> str:
        value = f"{self.persona_id.strip()}|{self.persona_email.strip().lower()}"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(
            value
            for value in (
                self.persona_id,
                self.persona_email,
                self.provider_password,
                self.imap_username,
                self.imap_password,
            )
            if value
        )


def validate_live_config(config: ActivationJourneyConfig) -> list[str]:
    failures: list[str] = []
    parsed = urllib.parse.urlparse(config.normalized_base_url)
    host = str(parsed.hostname or "").strip().lower()
    if parsed.scheme != "https" or not host:
        failures.append("activation_live_base_url_must_be_https")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        failures.append("activation_live_base_url_must_be_origin_only")
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".test", ".invalid")):
        failures.append("activation_live_base_url_must_not_be_local_or_test")
    try:
        if host and ipaddress.ip_address(host).is_private:
            failures.append("activation_live_base_url_must_not_be_private_ip")
    except ValueError:
        pass
    allowed_suffixes = tuple(
        suffix.strip().lower().lstrip(".")
        for suffix in config.allowed_host_suffixes
        if suffix.strip().lower().lstrip(".")
    )
    allowed_suffixes_valid = bool(allowed_suffixes) and all(
        "." in suffix
        and re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", suffix)
        and ".." not in suffix
        for suffix in allowed_suffixes
    )
    if not allowed_suffixes_valid:
        failures.append("activation_allowed_host_suffixes_invalid")
    if not allowed_suffixes_valid or not any(
        host == suffix or host.endswith("." + suffix)
        for suffix in allowed_suffixes
    ):
        failures.append("activation_live_host_not_explicitly_allowed")
    if config.live_authorized is not True:
        failures.append("activation_live_run_not_explicitly_authorized")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", config.persona_id.strip()):
        failures.append("activation_persona_id_missing_or_invalid")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", config.persona_email.strip()):
        failures.append("activation_persona_email_missing_or_invalid")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", config.run_key.strip()):
        failures.append("activation_run_key_missing_or_invalid")
    if re.fullmatch(r"[0-9a-f]{40}", str(config.release_commit_sha or "")) is None:
        failures.append("activation_release_commit_sha_missing_or_invalid")
    auth_mode = config.auth_mode.strip().lower()
    if auth_mode not in SUPPORTED_AUTH_MODES:
        failures.append(f"activation_auth_mode_unsupported:{auth_mode or 'missing'}")
    if auth_mode == "google" and not config.provider_password:
        failures.append("activation_google_persona_password_missing")
    if auth_mode == "email_link" and not all(
        (config.imap_host, config.imap_username, config.imap_password)
    ):
        failures.append("activation_email_link_imap_credentials_missing")
    expected_state = config.expected_account_state.strip().lower()
    if expected_state not in {"existing", "new"}:
        failures.append("activation_expected_account_state_invalid")
    if expected_state == "new" and not config.allow_account_create:
        failures.append("activation_account_creation_not_explicitly_authorized")
    if expected_state == "new":
        failures.append("activation_new_account_cleanup_not_supported_use_preprovisioned_persona")
    try:
        normalize_playwright_engine(config.browser_engine)
    except ValueError as exc:
        failures.append(str(exc))
    return failures


def _redact(value: Any, *, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact(item, secrets=secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item, secrets=secrets) for item in value]
    if isinstance(value, str):
        redacted = value
        for secret in secrets:
            if secret:
                redacted = redacted.replace(secret, "[redacted]")
        redacted = re.sub(
            r"(?i)([?&](?:access_token|code|id_token|login_token|state|token)=)[^&#\s\"'>]+",
            r"\1[redacted]",
            redacted,
        )
        return redacted
    return value


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def reserve_run_key(config: ActivationJourneyConfig) -> dict[str, Any]:
    state_path = config.state_path.expanduser().resolve()
    if state_path.is_file():
        try:
            existing = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError("activation_run_state_invalid") from exc
        if not isinstance(existing, dict):
            raise RuntimeError("activation_run_state_invalid")
        if (
            str(existing.get("run_key") or "") == config.run_key
            and str(existing.get("status") or "") in {"reserved", "running", "pass", "fail"}
        ):
            if str(existing.get("release_commit_sha") or "") != config.release_commit_sha:
                raise RuntimeError(
                    f"activation_run_key_candidate_mismatch:{config.run_key}"
                )
            raise RuntimeError(f"activation_run_key_already_used:{config.run_key}")
    state = {
        "status": "reserved",
        "reserved_at": datetime.now(timezone.utc).isoformat(),
        "run_key": config.run_key,
        "release_commit_sha": config.release_commit_sha,
        "persona_digest": config.persona_digest,
        "base_origin": urllib.parse.urlunparse(
            (*urllib.parse.urlparse(config.normalized_base_url)[:2], "", "", "", "")
        ),
    }
    _write_json(state_path, state)
    return state


def _extract_same_origin_sign_in_link(
    message_bytes: bytes,
    *,
    base_url: str,
    not_before: datetime,
) -> str:
    message = email.message_from_bytes(message_bytes)
    message_date = message.get("Date")
    if not message_date:
        return ""
    try:
        parsed_date = parsedate_to_datetime(message_date)
        if parsed_date.tzinfo is None:
            parsed_date = parsed_date.replace(tzinfo=timezone.utc)
        if parsed_date.astimezone(timezone.utc) < not_before - timedelta(minutes=2):
            return ""
    except Exception:
        return ""
    parts: list[str] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
    body = "\n".join(parts)
    expected_origin = urllib.parse.urlunparse((*urllib.parse.urlparse(base_url)[:2], "", "", "", ""))
    for candidate in re.findall(r"https?://[^\s<>\"']+", body):
        normalized = candidate.replace("&amp;", "&").rstrip(".,)")
        parsed = urllib.parse.urlparse(normalized)
        origin = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
        query = urllib.parse.parse_qs(parsed.query)
        if origin == expected_origin and any(key in query for key in ("token", "login_token", "access_token")):
            return normalized
    return ""


def fetch_email_sign_in_link(
    config: ActivationJourneyConfig,
    *,
    not_before: datetime,
) -> str:
    deadline = time.monotonic() + max(30, config.auth_timeout_seconds)
    while time.monotonic() < deadline:
        with imaplib.IMAP4_SSL(config.imap_host, config.imap_port) as client:
            client.login(config.imap_username, config.imap_password)
            client.select(config.imap_mailbox, readonly=True)
            status, data = client.search(None, "ALL")
            if status == "OK" and data:
                message_ids = data[0].split()[-25:]
                for message_id in reversed(message_ids):
                    fetch_status, rows = client.fetch(message_id, "(RFC822)")
                    if fetch_status != "OK":
                        continue
                    for row in rows:
                        if not isinstance(row, tuple) or not isinstance(row[1], bytes):
                            continue
                        link = _extract_same_origin_sign_in_link(
                            row[1],
                            base_url=config.normalized_base_url,
                            not_before=not_before,
                        )
                        if link:
                            return link
        time.sleep(5)
    raise TimeoutError("activation_email_sign_in_link_timeout")


def _wait_for_app_account(page: Any, config: ActivationJourneyConfig) -> None:
    page.wait_for_url(
        re.compile(rf"^{re.escape(config.normalized_base_url)}/(?:app(?:/.*)?|sign-in(?:\?.*)?)$"),
        timeout=config.auth_timeout_seconds * 1000,
    )
    response = page.goto(f"{config.normalized_base_url}/app/account", wait_until="domcontentloaded")
    if response is None or not response.ok:
        raise RuntimeError("activation_authenticated_account_route_unavailable")
    page.locator("[data-account-page-sign-out]").wait_for(
        state="visible",
        timeout=config.auth_timeout_seconds * 1000,
    )


def authenticate_with_google(page: Any, config: ActivationJourneyConfig) -> dict[str, Any]:
    page.goto(f"{config.normalized_base_url}/sign-in", wait_until="domcontentloaded")
    provider_link = page.locator('a[href="/sign-in/google"][data-auth-provider-link]').first
    if provider_link.count() == 0 or not provider_link.is_visible():
        raise RuntimeError("activation_google_provider_not_available")
    provider_link.click()
    page.wait_for_url(re.compile(r"^https://accounts\.google\.com/"), timeout=config.auth_timeout_seconds * 1000)
    account_choice = page.get_by_text(config.persona_email, exact=False).first
    if account_choice.count() and account_choice.is_visible():
        account_choice.click()
        page.wait_for_timeout(250)
    if str(page.url or "").startswith(config.normalized_base_url + "/"):
        _wait_for_app_account(page, config)
        return {"provider": "google", "provider_origin_verified": True}
    email_field = page.locator('input[type="email"]').first
    if email_field.count() and email_field.is_visible():
        email_field.fill(config.persona_email)
        page.get_by_role("button", name=re.compile(r"^(Next|Continue)$", re.I)).first.click()
        page.wait_for_timeout(250)
    if str(page.url or "").startswith(config.normalized_base_url + "/"):
        _wait_for_app_account(page, config)
        return {"provider": "google", "provider_origin_verified": True}
    password_field = page.locator('input[type="password"]').first
    password_field.wait_for(state="visible", timeout=config.auth_timeout_seconds * 1000)
    password_field.fill(config.provider_password)
    page.get_by_role("button", name=re.compile(r"^(Next|Continue)$", re.I)).first.click()
    try:
        _wait_for_app_account(page, config)
    except Exception as exc:
        current_host = str(urllib.parse.urlparse(str(page.url or "")).hostname or "")
        if current_host.endswith("google.com"):
            raise RuntimeError("activation_google_challenge_or_consent_not_completed") from exc
        raise
    return {"provider": "google", "provider_origin_verified": True}


def authenticate_with_email_link(
    page: Any,
    config: ActivationJourneyConfig,
    *,
    link_fetcher: Callable[[ActivationJourneyConfig], str] | None = None,
) -> dict[str, Any]:
    requested_at = datetime.now(timezone.utc)
    page.goto(f"{config.normalized_base_url}/sign-in", wait_until="domcontentloaded")
    form = page.locator('form[action="/sign-in/email-link"]').first
    if form.count() == 0 or not form.is_visible():
        raise RuntimeError("activation_email_link_provider_not_available")
    form.locator('input[name="email"]').fill(config.persona_email)
    form.get_by_role("button", name="Send sign-in link").click()
    page.wait_for_url(re.compile(r"/sign-in\?.*link_status=submitted"), timeout=config.auth_timeout_seconds * 1000)
    if link_fetcher is None:
        link = fetch_email_sign_in_link(config, not_before=requested_at)
    else:
        link = link_fetcher(config)
    parsed_link = urllib.parse.urlparse(link)
    expected = urllib.parse.urlparse(config.normalized_base_url)
    if (parsed_link.scheme, parsed_link.netloc) != (expected.scheme, expected.netloc):
        raise RuntimeError("activation_email_link_wrong_origin")
    page.goto(link, wait_until="domcontentloaded")
    _wait_for_app_account(page, config)
    return {"provider": "email_link", "provider_origin_verified": True}


def _authenticate(page: Any, config: ActivationJourneyConfig) -> dict[str, Any]:
    if config.auth_mode.strip().lower() == "google":
        return authenticate_with_google(page, config)
    return authenticate_with_email_link(page, config)


def _step(name: str, ok: bool, **details: Any) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), **details}


def _perform_logout(
    page: Any,
    config: ActivationJourneyConfig,
    *,
    allow_already_signed_out: bool = False,
) -> None:
    account_url = f"{config.normalized_base_url}/app/account"
    response = page.goto(account_url, wait_until="domcontentloaded")
    current = urllib.parse.urlparse(str(page.url or ""))
    if current.path.rstrip("/") == "/sign-in":
        if allow_already_signed_out and response is not None and response.ok:
            return
        raise RuntimeError("activation_session_was_not_authenticated_before_logout")
    form = page.locator("[data-account-page-sign-out]").first
    form.wait_for(state="visible", timeout=30_000)
    form.get_by_role("button", name="Log out").click()
    page.wait_for_timeout(250)
    response = page.goto(account_url, wait_until="domcontentloaded")
    current = urllib.parse.urlparse(str(page.url or ""))
    if (
        response is None
        or not response.ok
        or current.path.rstrip("/") != "/sign-in"
        or page.locator("[data-account-page-sign-out]").count() != 0
    ):
        raise RuntimeError("activation_logout_did_not_clear_session")


def _run_real_search(page: Any, config: ActivationJourneyConfig) -> dict[str, Any]:
    response = page.goto(f"{config.normalized_base_url}/app/search", wait_until="domcontentloaded")
    if response is None or not response.ok:
        raise RuntimeError("activation_search_route_unavailable")
    form = page.locator('[data-console-form-variant="property_search"]').first
    form.wait_for(state="visible", timeout=30_000)
    if config.search_country_code:
        country = page.locator('select[name="country_code"]').first
        if country.count():
            country.select_option(config.search_country_code)
    if config.search_region_code:
        region = page.locator('select[name="region_code"]').first
        if region.count():
            region.select_option(config.search_region_code)
    if config.search_location:
        location = page.locator(
            'input[name="custom_location_query"]:visible, input[name="location_query"]:visible'
        ).first
        if location.count():
            location.fill(config.search_location)
    selected_providers = page.eval_on_selector_all(
        'input[name="selected_platforms"]:checked:not(:disabled)',
        "nodes => nodes.map(node => String(node.value || '').trim()).filter(Boolean)",
    )
    if not selected_providers:
        raise RuntimeError("activation_no_real_search_providers_selected")
    with page.expect_response(
        lambda item: "/app/api/property/search-runs" in item.url and item.request.method == "POST",
        timeout=60_000,
    ) as response_info:
        page.locator("[data-property-start-top]").first.click()
    run_response = response_info.value
    if not run_response.ok:
        raise RuntimeError(f"activation_real_search_submit_failed:{run_response.status}")
    try:
        run_payload = dict(run_response.json() or {})
    except Exception:
        run_payload = {}
    request_payload = run_response.request.post_data_json
    request_payload = dict(request_payload) if isinstance(request_payload, dict) else {}
    run_id = str(run_payload.get("run_id") or "").strip()
    if not run_id:
        raise RuntimeError("activation_real_search_run_id_missing")
    page.wait_for_url(re.compile(rf"(?:run_id=|/run/){re.escape(run_id)}"), timeout=60_000)
    page.locator("[data-workbench-row]").first.wait_for(
        state="visible",
        timeout=max(60, config.search_timeout_seconds) * 1000,
    )
    raw_provider_values = (
        request_payload.get("selected_platforms")
        or dict(request_payload.get("property_preferences") or {}).get("selected_platforms")
        or []
    )
    if isinstance(raw_provider_values, str):
        observed_provider_values = [
            value.strip() for value in raw_provider_values.split(",") if value.strip()
        ]
    elif isinstance(raw_provider_values, (list, tuple, set)):
        observed_provider_values = [
            str(value or "").strip() for value in raw_provider_values if str(value or "").strip()
        ]
    else:
        observed_provider_values = []
    if not observed_provider_values:
        raise RuntimeError("activation_real_search_provider_payload_missing")
    if set(observed_provider_values) != set(str(value) for value in selected_providers):
        raise RuntimeError("activation_real_search_provider_payload_mismatch")
    return {
        "run_id": run_id,
        "provider_count": len(observed_provider_values),
        "result_count": page.locator("[data-workbench-row]").count(),
    }


def _open_shortlist_and_research(page: Any, config: ActivationJourneyConfig, *, run_id: str) -> dict[str, Any]:
    shortlist_url = f"{config.normalized_base_url}/app/shortlist?" + urllib.parse.urlencode(
        {"run_id": run_id, "full": "1"}
    )
    response = page.goto(shortlist_url, wait_until="domcontentloaded")
    if response is None or not response.ok:
        raise RuntimeError("activation_shortlist_route_unavailable")
    row = page.locator("[data-workbench-row]").first
    row.wait_for(state="visible", timeout=60_000)
    shortlist_result_count = page.locator("[data-workbench-row]").count()
    packet_path = str(row.get_attribute("data-candidate-packet-url") or "").strip()
    if not packet_path:
        packet_path = str(row.locator(".pqx-result-title").first.get_attribute("href") or "").strip()
    if not packet_path:
        raise RuntimeError("activation_research_link_missing")
    parsed_packet = urllib.parse.urlparse(packet_path)
    if parsed_packet.scheme and parsed_packet.netloc != urllib.parse.urlparse(config.normalized_base_url).netloc:
        raise RuntimeError("activation_research_link_wrong_origin")
    research_url = packet_path if parsed_packet.scheme else urllib.parse.urljoin(config.normalized_base_url + "/", packet_path.lstrip("/"))
    research_response = page.goto(research_url, wait_until="domcontentloaded")
    if research_response is None or not research_response.ok:
        raise RuntimeError("activation_research_route_unavailable")
    page.locator("[data-property-research-detail]").wait_for(state="visible", timeout=60_000)
    return {
        "shortlist_result_count": shortlist_result_count,
        "research_path": urllib.parse.urlparse(research_url).path,
    }


def _walkthrough_href_ready(href: str) -> bool:
    normalized = str(href or "").strip()
    if not normalized or normalized == "#" or normalized.lower().startswith("javascript:"):
        return False
    parsed = urllib.parse.urlparse(normalized)
    if parsed.scheme:
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    return normalized.startswith("/")


def _request_or_reuse_walkthrough(page: Any, config: ActivationJourneyConfig) -> dict[str, Any]:
    open_button = page.get_by_role("button", name=re.compile("Open walkthrough", re.I)).first
    if open_button.count() and open_button.is_visible():
        href = str(open_button.get_attribute("data-pw-visual-href") or open_button.get_attribute("href") or "").strip()
        return {
            "mode": "reused_ready",
            "ready": _walkthrough_href_ready(href),
            "request_status": "not_needed",
        }
    open_link = page.get_by_role("link", name=re.compile("Open walkthrough", re.I)).first
    if open_link.count() and open_link.is_visible():
        return {
            "mode": "reused_ready",
            "ready": _walkthrough_href_ready(str(open_link.get_attribute("href") or "")),
            "request_status": "not_needed",
        }
    request_button = page.get_by_role("button", name="Request walkthrough").first
    if request_button.count() == 0 or not request_button.is_visible():
        raise RuntimeError("activation_walkthrough_request_control_missing")
    if not config.allow_walkthrough_request:
        raise RuntimeError("activation_walkthrough_request_not_explicitly_authorized")
    request_button.click()
    dialog = page.locator("[data-prd-visual-style-dialog]").first
    dialog.wait_for(state="visible", timeout=10_000)
    option = dialog.locator("[data-prd-style-option]", has_text=config.visual_style).first
    if option.count() == 0:
        option = dialog.locator("[data-prd-style-option]").first
    option.click()
    with page.expect_response(
        lambda item: "/app/api/signals/willhaben/property-tour" in item.url and item.request.method == "POST",
        timeout=60_000,
    ) as response_info:
        dialog.locator("[data-prd-style-confirm]").first.click()
    request_response = response_info.value
    if not request_response.ok:
        raise RuntimeError(f"activation_walkthrough_request_failed:{request_response.status}")
    ready = page.get_by_role("button", name=re.compile("Open walkthrough", re.I)).first
    ready.wait_for(
        state="visible",
        timeout=max(60, config.walkthrough_timeout_seconds) * 1000,
    )
    href = str(ready.get_attribute("data-pw-visual-href") or ready.get_attribute("href") or "").strip()
    return {
        "mode": "requested",
        "ready": _walkthrough_href_ready(href),
        "request_status": str(request_response.status),
    }


def run_deployed_activation_journey(config: ActivationJourneyConfig) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    engine = normalize_playwright_engine(config.browser_engine)
    steps: list[dict[str, Any]] = []
    cleanup_ok = False
    journey_error = ""
    with sync_playwright() as playwright:
        browser_type = playwright_browser_type(playwright, engine=engine)
        browser = browser_type.launch(
            **playwright_engine_launch_kwargs(
                playwright,
                engine=engine,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
        )
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, service_workers="block")
        page = context.new_page()
        page.set_default_timeout(30_000)
        try:
            landing_response = page.goto(config.normalized_base_url + "/", wait_until="domcontentloaded")
            landing_ok = bool(landing_response and landing_response.ok) and page.get_by_role(
                "link", name=re.compile("sign in", re.I)
            ).count() > 0
            steps.append(_step("landing", landing_ok))
            if not landing_ok:
                raise RuntimeError("activation_landing_not_ready")

            auth_details = _authenticate(page, config)
            steps.append(_step("real_authentication", True, **auth_details))
            steps.append(
                _step(
                    "account_create_or_reopen",
                    True,
                    outcome="reopened" if config.expected_account_state == "existing" else "created",
                )
            )

            search = _run_real_search(page, config)
            steps.append(_step("first_real_search", True, run_id=search["run_id"]))
            steps.append(
                _step(
                    "real_provider_results",
                    search["provider_count"] > 0 and search["result_count"] > 0,
                    provider_count=search["provider_count"],
                    result_count=search["result_count"],
                )
            )
            research = _open_shortlist_and_research(page, config, run_id=str(search["run_id"]))
            steps.append(
                _step(
                    "shortlist",
                    research["shortlist_result_count"] > 0,
                    result_count=research["shortlist_result_count"],
                )
            )
            steps.append(_step("research", True, research_path=research["research_path"]))
            walkthrough = _request_or_reuse_walkthrough(page, config)
            steps.append(
                _step(
                    "walkthrough_request_or_reuse",
                    True,
                    mode=walkthrough["mode"],
                    request_status=walkthrough["request_status"],
                )
            )
            steps.append(_step("walkthrough_ready", walkthrough["ready"] is True))

            _perform_logout(page, config)
            steps.append(_step("logout", True))
            _authenticate(page, config)
            steps.append(_step("relogin", True, provider=config.auth_mode))
            _perform_logout(page, config)
            cleanup_ok = True
            steps.append(
                _step(
                    "safe_cleanup",
                    True,
                    session_cleared=True,
                    durable_evidence_policy="preserved_for_audit",
                )
            )
        except Exception as exc:
            journey_error = f"{type(exc).__name__}: {exc}"
        finally:
            if not cleanup_ok:
                try:
                    _perform_logout(page, config, allow_already_signed_out=True)
                    cleanup_ok = True
                except Exception:
                    cleanup_ok = False
            if not any(str(step.get("name") or "") == "safe_cleanup" for step in steps):
                steps.append(
                    _step(
                        "safe_cleanup",
                        cleanup_ok,
                        session_cleared=cleanup_ok,
                        recovery_after_failure=True,
                    )
                )
            context.close()
            browser.close()
    return {
        "steps": steps,
        "cleanup_ok": cleanup_ok,
        "browser_engine": engine,
        "error": journey_error,
    }


def build_activation_to_value_receipt(
    *,
    config: ActivationJourneyConfig,
    journey_runner: Callable[[ActivationJourneyConfig], dict[str, Any]] = run_deployed_activation_journey,
) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc).isoformat()
    failures = validate_live_config(config)
    deployed_runner = journey_runner is run_deployed_activation_journey
    base = {
        "generated_at": started_at,
        "started_at": started_at,
        "base_url": config.normalized_base_url,
        "persona_digest": config.persona_digest,
        "run_key": config.run_key,
        "release_commit_sha": config.release_commit_sha,
        "auth_mode": config.auth_mode,
        "browser_engine": str(config.browser_engine or "chromium").strip().lower(),
        "expected_account_state": config.expected_account_state,
        "proof_mode": "deployed_playwright" if deployed_runner else "contract_mock",
        "live_contract": {
            "explicit_persona": bool(config.persona_id.strip() and config.persona_email.strip()),
            "principal_headers_forbidden": True,
            "session_injection_forbidden": True,
            "provider_response_mocking_forbidden": deployed_runner,
            "local_execution_forbidden": True,
            "deployed_playwright_runner": deployed_runner,
        },
    }
    if failures:
        return _redact(
            {
                **base,
                "status": "blocked",
                "failed_count": len(failures),
                "checks": [
                    {"name": "protected_live_configuration", "ok": False, "reason": failure}
                    for failure in failures
                ],
                "steps": [],
            },
            secrets=config.secrets,
        )
    try:
        reserve_run_key(config)
    except Exception as exc:
        return _redact(
            {
                **base,
                "status": "blocked",
                "failed_count": 1,
                "checks": [{"name": "idempotent_run_reservation", "ok": False, "reason": f"{type(exc).__name__}: {exc}"}],
                "steps": [],
            },
            secrets=config.secrets,
        )
    try:
        result = dict(journey_runner(config) or {})
        steps = [dict(step) for step in list(result.get("steps") or []) if isinstance(step, dict)]
        observed = {str(step.get("name") or ""): step.get("ok") is True for step in steps}
        missing_steps = [name for name in REQUIRED_JOURNEY_STEPS if observed.get(name) is not True]
        cleanup_ok = result.get("cleanup_ok") is True and observed.get("safe_cleanup") is True
        journey_error = str(result.get("error") or "").strip()
        checks = [
            {"name": "protected_live_configuration", "ok": True},
            {"name": "idempotent_run_reservation", "ok": True},
            {"name": "activation_step_matrix_complete", "ok": not missing_steps, "missing_steps": missing_steps},
            {"name": "safe_cleanup_complete", "ok": cleanup_ok},
        ]
        if journey_error:
            checks.append(
                {
                    "name": "deployed_activation_journey",
                    "ok": False,
                    "reason": journey_error,
                }
            )
        failed_steps = [step for step in steps if step.get("ok") is not True]
        failed_checks = [check for check in checks if check.get("ok") is not True]
        receipt = {
            **base,
            "status": "pass" if not failed_steps and not failed_checks else "fail",
            "failed_count": len(failed_steps) + len(failed_checks),
            "checks": checks,
            "steps": steps,
        }
    except Exception as exc:
        receipt = {
            **base,
            "status": "fail",
            "failed_count": 1,
            "checks": [
                {
                    "name": "deployed_activation_journey",
                    "ok": False,
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            ],
            "steps": [],
        }
    state = {
        "status": receipt["status"],
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "run_key": config.run_key,
        "release_commit_sha": config.release_commit_sha,
        "persona_digest": config.persona_digest,
    }
    receipt["generated_at"] = state["completed_at"]
    _write_json(config.state_path.expanduser().resolve(), state)
    return _redact(receipt, secrets=config.secrets)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the protected deployed PropertyQuarry activation-to-value flagship journey."
    )
    parser.add_argument("--base-url", default=os.environ.get("PROPERTYQUARRY_ACTIVATION_BASE_URL", ""))
    parser.add_argument("--persona-id", default=os.environ.get("PROPERTYQUARRY_ACTIVATION_PERSONA_ID", ""))
    parser.add_argument("--run-key", default=os.environ.get("PROPERTYQUARRY_ACTIVATION_RUN_KEY", ""))
    parser.add_argument(
        "--release-sha",
        default=(
            os.environ.get("PROPERTYQUARRY_RELEASE_COMMIT_SHA", "")
            or os.environ.get("PROPERTYQUARRY_EXPECTED_RELEASE_COMMIT_SHA", "")
        ),
    )
    parser.add_argument(
        "--auth-mode",
        choices=SUPPORTED_AUTH_MODES,
        default=os.environ.get("PROPERTYQUARRY_ACTIVATION_AUTH_MODE", "google"),
    )
    parser.add_argument(
        "--browser-engine",
        choices=("chromium", "firefox", "webkit"),
        default=os.environ.get("PROPERTYQUARRY_ACTIVATION_BROWSER_ENGINE", "chromium"),
    )
    parser.add_argument("--state-path", default=str(DEFAULT_STATE_PATH))
    parser.add_argument("--write", default="_completion/smoke/property-live-activation-to-value-latest.json")
    parser.add_argument("--confirm-live", action="store_true", default=_env_flag("PROPERTYQUARRY_ACTIVATION_LIVE_RUN"))
    args = parser.parse_args()

    config = ActivationJourneyConfig(
        base_url=str(args.base_url or ""),
        persona_id=str(args.persona_id or ""),
        persona_email=str(os.environ.get("PROPERTYQUARRY_ACTIVATION_PERSONA_EMAIL") or ""),
        run_key=str(args.run_key or ""),
        release_commit_sha=str(args.release_sha or ""),
        auth_mode=str(args.auth_mode or "google"),
        browser_engine=str(args.browser_engine or "chromium"),
        expected_account_state=str(os.environ.get("PROPERTYQUARRY_ACTIVATION_EXPECTED_ACCOUNT_STATE") or "existing"),
        allow_account_create=_env_flag("PROPERTYQUARRY_ACTIVATION_ALLOW_ACCOUNT_CREATE"),
        allow_walkthrough_request=_env_flag("PROPERTYQUARRY_ACTIVATION_ALLOW_WALKTHROUGH_REQUEST"),
        search_country_code=str(os.environ.get("PROPERTYQUARRY_ACTIVATION_SEARCH_COUNTRY_CODE") or ""),
        search_region_code=str(os.environ.get("PROPERTYQUARRY_ACTIVATION_SEARCH_REGION_CODE") or ""),
        search_location=str(os.environ.get("PROPERTYQUARRY_ACTIVATION_SEARCH_LOCATION") or ""),
        visual_style=str(os.environ.get("PROPERTYQUARRY_ACTIVATION_VISUAL_STYLE") or "Urban jungle"),
        provider_password=str(os.environ.get("PROPERTYQUARRY_ACTIVATION_PROVIDER_PASSWORD") or ""),
        imap_host=str(os.environ.get("PROPERTYQUARRY_ACTIVATION_IMAP_HOST") or ""),
        imap_port=int(os.environ.get("PROPERTYQUARRY_ACTIVATION_IMAP_PORT") or 993),
        imap_username=str(os.environ.get("PROPERTYQUARRY_ACTIVATION_IMAP_USERNAME") or ""),
        imap_password=str(os.environ.get("PROPERTYQUARRY_ACTIVATION_IMAP_PASSWORD") or ""),
        imap_mailbox=str(os.environ.get("PROPERTYQUARRY_ACTIVATION_IMAP_MAILBOX") or "INBOX"),
        auth_timeout_seconds=int(os.environ.get("PROPERTYQUARRY_ACTIVATION_AUTH_TIMEOUT_SECONDS") or 180),
        search_timeout_seconds=int(os.environ.get("PROPERTYQUARRY_ACTIVATION_SEARCH_TIMEOUT_SECONDS") or 900),
        walkthrough_timeout_seconds=int(os.environ.get("PROPERTYQUARRY_ACTIVATION_WALKTHROUGH_TIMEOUT_SECONDS") or 1200),
        allowed_host_suffixes=tuple(
            suffix.strip()
            for suffix in str(
                os.environ.get("PROPERTYQUARRY_ACTIVATION_ALLOWED_HOST_SUFFIXES")
                or "propertyquarry.com"
            ).split(",")
            if suffix.strip()
        ),
        state_path=Path(args.state_path),
        live_authorized=bool(args.confirm_live),
    )
    receipt = build_activation_to_value_receipt(config=config)
    output = json.dumps(receipt, indent=2, sort_keys=True)
    if args.write:
        output_path = Path(args.write)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0 if receipt.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
