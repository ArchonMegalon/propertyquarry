from __future__ import annotations

import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import hashlib
import html
from dataclasses import dataclass
from datetime import datetime, timezone

from app.services.property_customer_copy import normalize_property_fit_note


EMAILIT_API_BASE = "https://api.emailit.com/v2/emails"
CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"
DEFAULT_SENDER_EMAIL = "property@propertyquarry.com"
DEFAULT_SENDER_NAME = "PropertyQuarry"
_PLAINTEXT_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_SENDER_ADDR_SPEC_RE = re.compile(
    r"(?=.{3,254}\Z)[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}\Z"
)
_CLOUDFLARE_ACCOUNT_ID_RE = re.compile(r"[0-9a-f]{32}\Z")


@dataclass(frozen=True)
class RegistrationEmailReceipt:
    provider: str
    message_id: str
    accepted_at: str


def email_delivery_enabled() -> bool:
    if str(os.environ.get("EMAILIT_API_KEY") or "").strip():
        return True
    try:
        return _cloudflare_email_credentials() is not None
    except RuntimeError:
        return False


def _cloudflare_email_credentials() -> tuple[str, str] | None:
    raw_token = str(
        os.environ.get("PROPERTYQUARRY_CLOUDFLARE_EMAIL_API_TOKEN") or ""
    )
    raw_account_id = str(
        os.environ.get("PROPERTYQUARRY_CLOUDFLARE_EMAIL_ACCOUNT_ID") or ""
    )
    token = raw_token.strip()
    account_id = raw_account_id.strip()
    if bool(token) != bool(account_id):
        raise RuntimeError("registration_email_cloudflare_config_incomplete")
    if not token:
        return None
    if (
        not _CLOUDFLARE_ACCOUNT_ID_RE.fullmatch(account_id)
        or raw_token != token
        or raw_account_id != account_id
        or len(token) < 20
        or any(character.isspace() or ord(character) < 33 for character in token)
    ):
        raise RuntimeError("registration_email_cloudflare_config_invalid")
    return token, account_id


def _propertyquarry_production_sender_required() -> bool:
    return str(os.environ.get("EA_RUNTIME_MODE") or "").strip().lower() == "prod"


def _propertyquarry_sender_email_allowed(value: str) -> bool:
    normalized = str(value or "").strip()
    if not _SENDER_ADDR_SPEC_RE.fullmatch(normalized):
        return False
    domain = normalized.rsplit("@", 1)[1].lower()
    return domain == "propertyquarry.com" or domain.endswith(".propertyquarry.com")


def _enforce_propertyquarry_production_sender(value: str) -> str:
    normalized = str(value or "").strip()
    if _propertyquarry_production_sender_required() and not _propertyquarry_sender_email_allowed(normalized):
        raise RuntimeError("registration_email_sender_domain_forbidden")
    return normalized


def _force_fallback_sender() -> bool:
    return str(os.environ.get("EA_REGISTRATION_EMAIL_FORCE_FALLBACK") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _registration_sender_email() -> str:
    if _force_fallback_sender():
        forced = _fallback_sender_email()
        if forced:
            return forced
    configured = str(os.environ.get("EA_REGISTRATION_EMAIL_FROM") or "").strip()
    if configured:
        return configured
    fallback = str(os.environ.get("EA_EMAIL_DEFAULT_FROM") or "").strip()
    return fallback or DEFAULT_SENDER_EMAIL


def delivery_sender_emails() -> tuple[str, ...]:
    values = {
        DEFAULT_SENDER_EMAIL.strip().lower(),
        str(os.environ.get("EA_EMAIL_DEFAULT_FROM") or "").strip().lower(),
        str(os.environ.get("EA_REGISTRATION_EMAIL_FROM") or "").strip().lower(),
        _registration_sender_email().strip().lower(),
    }
    return tuple(
        sorted(
            value
            for value in values
            if value
            and (
                not _propertyquarry_production_sender_required()
                or _propertyquarry_sender_email_allowed(value)
            )
        )
    )


def _registration_sender_name() -> str:
    if _force_fallback_sender():
        return _fallback_sender_name()
    configured = str(os.environ.get("EA_REGISTRATION_EMAIL_NAME") or "").strip()
    if configured:
        return configured
    fallback = str(os.environ.get("EA_EMAIL_DEFAULT_NAME") or "").strip()
    return fallback or DEFAULT_SENDER_NAME


def _fallback_sender_email() -> str:
    configured = str(os.environ.get("EA_REGISTRATION_EMAIL_FROM_FALLBACK") or "").strip()
    if configured:
        return configured
    fallback = str(os.environ.get("EA_EMAIL_DEFAULT_FROM") or "").strip()
    return fallback


def _fallback_sender_name() -> str:
    configured = str(os.environ.get("EA_REGISTRATION_EMAIL_NAME_FALLBACK") or "").strip()
    if configured:
        return configured
    fallback = str(os.environ.get("EA_EMAIL_DEFAULT_NAME") or "").strip()
    return fallback or _registration_sender_name()


def _resolved_sender_email(sender_email: str = "") -> str:
    configured = str(sender_email or "").strip()
    if configured:
        return _enforce_propertyquarry_production_sender(configured)
    return _enforce_propertyquarry_production_sender(_registration_sender_email())


def _resolved_sender_name(sender_name: str = "") -> str:
    configured = str(sender_name or "").strip()
    if configured:
        return configured
    return _registration_sender_name()


def _registration_subject() -> str:
    return "Verify your email for PropertyQuarry"


def _minutes_until(*, expires_at: int | None = None, expires_at_iso: str = "") -> int:
    if expires_at is not None:
        return max(1, int((int(expires_at) - int(time.time())) / 60))
    normalized = str(expires_at_iso or "").strip()
    if not normalized:
        return 60
    try:
        when = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return 60
    return max(1, int((when - datetime.now(timezone.utc)).total_seconds() / 60))


def _registration_text(*, verification_code: str, magic_link_url: str, expires_at: int) -> str:
    minutes = _minutes_until(expires_at=expires_at)
    return (
        "Hello,\n\n"
        "Use this verification code to create your PropertyQuarry account:\n\n"
        f"{verification_code}\n\n"
        "Or use the titled secure-access button in this email.\n\n"
        f"This link and code expire in about {minutes} minutes.\n\n"
        "Google can optionally confirm your PropertyQuarry identity after sign-up. "
        "It does not connect Gmail, Calendar, MyExternalBrain, or EA.\n\n"
        "If you did not request this email, you can ignore it.\n"
    )


def _registration_html(
    *,
    verification_code: str,
    magic_link_url: str,
    expires_at: int,
) -> str:
    minutes = _minutes_until(expires_at=expires_at)
    body = (
        '<p style="margin:0 0 14px;line-height:1.6;">'
        "Use this verification code to create your PropertyQuarry account:"
        "</p>"
        '<div style="margin:0 0 16px;font-size:28px;letter-spacing:.18em;'
        'font-weight:800;color:#242321;">'
        f"{_html_escape(verification_code)}"
        "</div>"
        + _email_button_row(
            [
                _email_button(
                    href=magic_link_url,
                    label="Verify email and continue",
                )
            ]
        )
        + '<p style="margin:0 0 14px;line-height:1.6;color:#6c675f;">'
        f"The button and code expire in about {minutes} minutes."
        "</p>"
        '<p style="margin:0;line-height:1.6;color:#6c675f;">'
        "Google can optionally confirm your PropertyQuarry identity afterward. "
        "It does not connect Gmail, Calendar, MyExternalBrain, or EA."
        "</p>"
        + _email_footer_html(
            reason="You are receiving this because this address started PropertyQuarry registration."
        )
    )
    return _html_email_shell(
        title="Verify your email for PropertyQuarry",
        body_html=body,
        preheader="Use your six-digit code or the secure verification button.",
    )


def _registration_verification_ref() -> str:
    return f"pqvr_{secrets.token_urlsafe(24)}"


def _meta_ref(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def _emailit_meta_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    except TypeError:
        return str(value).strip()


def _emailit_meta_payload(*, kind: str, recipient_email: str, meta: dict[str, object] | None = None) -> dict[str, str]:
    raw = {
        "kind": kind,
        "recipient_email": str(recipient_email or "").strip(),
        **dict(meta or {}),
    }
    payload: dict[str, str] = {}
    for key, value in raw.items():
        normalized_key = str(key or "").strip()[:80]
        if not normalized_key:
            continue
        normalized_value = _emailit_meta_value(value)
        if not normalized_value:
            continue
        payload[normalized_key] = normalized_value[:500]
    return payload


def _digest_preview_excerpt(plain_text: str, *, max_lines: int = 8, max_chars: int = 800) -> str:
    lines: list[str] = []
    for raw_line in str(plain_text or "").splitlines():
        stripped = _strip_plaintext_urls(raw_line).strip()
        if not stripped:
            continue
        if stripped.lower().startswith("open digest:"):
            continue
        if len(stripped) > 240 and "http" in stripped:
            continue
        lines.append(stripped)
        if len(lines) >= max_lines:
            break
    excerpt = "\n".join(lines).strip()
    if len(excerpt) > max_chars:
        excerpt = excerpt[: max_chars - 3].rstrip() + "..."
    return excerpt


def _strip_plaintext_urls(value: object) -> str:
    text = str(value or "")
    if not text:
        return ""
    return _PLAINTEXT_URL_RE.sub("[titled link]", text)


_EMAIL_LINK_STYLE = "color:#0b57d0;text-decoration:underline;"


def _html_escape(value: object) -> str:
    return html.escape(str(value or "").strip(), quote=True)


def _html_link(*, href: object, label: object) -> str:
    url = str(href or "").strip()
    text = str(label or "").strip() or url
    if not url:
        return _html_escape(text)
    return f'<a href="{_html_escape(url)}" style="{_EMAIL_LINK_STYLE}">{_html_escape(text)}</a>'


def _email_footer_html(*, reason: str = "You are receiving this because you started or were invited into a PropertyQuarry workflow.") -> str:
    return (
        '<div style="margin-top:22px;padding-top:16px;border-top:1px solid #e7dece;">'
        f'<div style="font-size:12px;line-height:1.6;color:#6c675f;">{_html_escape(reason)}</div>'
        '<div style="font-size:12px;line-height:1.6;color:#6c675f;">'
        'PropertyQuarry treats these links as secure workflow continuations, not public listing broadcasts.'
        "</div>"
        "</div>"
    )


def _email_button(*, href: object, label: object, kind: str = "primary") -> str:
    url = str(href or "").strip()
    text = str(label or "").strip()
    if not url or not text:
        return ""
    if kind == "secondary":
        style = "display:inline-block;padding:12px 16px;border-radius:999px;border:1px solid #d7c7ad;color:#1f1d19;text-decoration:none;font-weight:700;"
    else:
        style = "display:inline-block;padding:12px 16px;border-radius:999px;background:#276b53;color:#fffdf8;text-decoration:none;font-weight:700;"
    return f'<a href="{_html_escape(url)}" style="{style}">{_html_escape(text)}</a>'


def _append_query_params(href: object, **params: object) -> str:
    url = str(href or "").strip()
    if not url:
        return ""
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    merged: dict[str, str] = {str(key): str(value) for key, value in query}
    for key, value in params.items():
        normalized_key = str(key or "").strip()
        if not normalized_key:
            continue
        normalized_value = str(value or "").strip()
        if not normalized_value:
            merged.pop(normalized_key, None)
        else:
            merged[normalized_key] = normalized_value
    encoded_query = urllib.parse.urlencode(list(merged.items()))
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, encoded_query, parsed.fragment))


def _canonical_property_research_url(href: object) -> str:
    url = str(href or "").strip()
    if not url:
        return ""
    parsed = urllib.parse.urlsplit(url)
    match = re.fullmatch(r"(?P<prefix>.*/app/research)/(?P<run_id>[^/]+)/(?P<candidate_ref>[^/]+)", parsed.path or "")
    if not match:
        return url
    run_id = str(match.group("run_id") or "").strip()
    candidate_ref = str(match.group("candidate_ref") or "").strip()
    if not run_id or not candidate_ref:
        return url
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    merged: dict[str, str] = {str(key): str(value) for key, value in query}
    merged["run_id"] = run_id
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            f"{match.group('prefix')}/{candidate_ref}",
            urllib.parse.urlencode(list(merged.items())),
            parsed.fragment,
        )
    )


def _email_button_row(buttons: list[str]) -> str:
    clean = [str(button or "").strip() for button in buttons if str(button or "").strip()]
    if not clean:
        return ""
    return f'<div style="margin:0 0 14px;">{" &nbsp; ".join(clean)}</div>'


def _property_decision_action_urls(
    *,
    packet_url: object = "",
    tour_url: object = "",
    property_url: object = "",
) -> dict[str, str]:
    review_url = _canonical_property_research_url(packet_url or tour_url or property_url)
    return {
        "review": review_url,
        "yes": _append_query_params(review_url, decision="yes"),
        "maybe": _append_query_params(review_url, decision="maybe"),
        "no": _append_query_params(review_url, decision="no", clippy="1", prompt="What is the strongest blocker here?"),
        "ask_agent": _append_query_params(review_url, clippy="1", prompt="What should I ask the agent next?"),
        "investment_risk": _append_query_params(review_url, clippy="1", prompt="What is the biggest investment risk here?"),
    }


def _html_email_shell(*, title: str, body_html: str, preheader: str = "") -> str:
    preheader_html = (
        f'<div style="display:none;max-height:0;overflow:hidden;opacity:0;mso-hide:all;">{_html_escape(preheader)}</div>'
        if str(preheader or "").strip()
        else ""
    )
    return (
        '<!doctype html><html><body style="margin:0;padding:0;background:#f6f3ee;'
        'font-family:Arial,Helvetica,sans-serif;color:#191714;">'
        f"{preheader_html}"
        '<div style="max-width:760px;margin:0 auto;padding:24px;">'
        '<div style="border:1px solid #ded6c8;border-radius:18px;background:#fffdf8;padding:20px;box-shadow:0 10px 28px rgba(36,35,33,0.06);">'
        '<div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#a37a2c;font-weight:700;margin:0 0 8px;">PropertyQuarry</div>'
        f'<h1 style="margin:0 0 14px;font-size:22px;line-height:1.2;color:#242321;">{_html_escape(title)}</h1>'
        f"{body_html}"
        "</div></div></body></html>"
    )


def _workspace_email_shell(
    *,
    eyebrow: str,
    title: str,
    summary: str,
    primary_label: str,
    primary_href: str,
    detail_rows: list[tuple[str, str]] | None = None,
    secondary_label: str = "",
    secondary_href: str = "",
    preheader: str = "",
) -> str:
    details = list(detail_rows or [])
    details_html = "".join(
        "<tr>"
        f'<td style="padding:8px 10px 8px 0;color:#6c675f;font-size:12px;white-space:nowrap;border-top:1px solid #ece4d8;">{_html_escape(label)}</td>'
        f'<td style="padding:8px 0;color:#242321;font-size:13px;border-top:1px solid #ece4d8;">{_html_escape(value)}</td>'
        "</tr>"
        for label, value in details
        if str(label or "").strip() and str(value or "").strip()
    )
    actions = []
    if str(primary_href or "").strip():
        actions.append(
            f'<a href="{_html_escape(primary_href)}" style="display:inline-block;padding:12px 16px;border-radius:999px;background:#276b53;color:#fffdf8;text-decoration:none;font-weight:700;">{_html_escape(primary_label)}</a>'
        )
    if str(secondary_href or "").strip():
        actions.append(
            f'<a href="{_html_escape(secondary_href)}" style="display:inline-block;padding:12px 16px;border-radius:999px;border:1px solid #d7c7ad;color:#1f1d19;text-decoration:none;font-weight:700;">{_html_escape(secondary_label)}</a>'
        )
    return _html_email_shell(
        title=title,
        preheader=preheader,
        body_html=(
            f'<div style="font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:#a37a2c;font-weight:700;margin:0 0 10px;">{_html_escape(eyebrow)}</div>'
            f'<p style="margin:0 0 16px;font-size:15px;line-height:1.65;color:#51493f;">{_html_escape(summary)}</p>'
            + (
                '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;margin:0 0 18px;">'
                f"{details_html}</table>"
                if details_html
                else ""
            )
            + (f'<div style="margin:0 0 14px;">{" &nbsp; ".join(actions)}</div>' if actions else "")
            + '<p style="margin:0;font-size:13px;line-height:1.6;color:#6c675f;">PropertyQuarry only uses this link to continue the exact account flow described above.</p>'
            + _email_footer_html(reason="You are receiving this because a PropertyQuarry account flow needs your action or confirmation.")
        ),
    )


def _property_email_facts(row: dict[str, object]) -> list[tuple[str, str]]:
    fact_specs = (
        ("Fit", row.get("fit_summary")),
        ("Source", row.get("source_label")),
        ("Price", row.get("price_label")),
        ("Area", row.get("area_label")),
        ("Rooms", row.get("rooms_label")),
        ("Location", row.get("location_label")),
        ("360", str(row.get("tour_status") or "").strip().replace("_", " ")),
    )
    facts: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for label, value in fact_specs:
        normalized = str(value or "").strip()
        if not normalized:
            continue
        key = (label, normalized)
        if key in seen:
            continue
        seen.add(key)
        facts.append((label, normalized))
    return facts


def _property_email_fit_note(row: dict[str, object]) -> str:
    return normalize_property_fit_note(row.get("compare_reason"))


def _property_email_fit_note_html(row: dict[str, object]) -> str:
    fit_note = _property_email_fit_note(row)
    if not fit_note:
        return ""
    return (
        '<div style="margin:8px 0 10px;font-size:13px;line-height:1.55;color:#3f4c46;">'
        f'<strong>Fit note:</strong> {_html_escape(fit_note)}'
        "</div>"
    )


def _property_search_results_ready_html(
    *,
    results_url: str,
    result_total: int,
    hosted_tour_total: int,
    property_rows: list[dict[str, object]],
) -> str:
    metric_table = (
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'style="border-collapse:collapse;margin:0 0 16px;">'
        "<tr>"
        '<td style="padding:10px;border:1px solid #ded6c8;background:#fdf9f1;">'
        '<div style="font-size:12px;color:#6c675f;text-transform:uppercase;">Ranked results</div>'
        f'<strong style="font-size:18px;">{max(int(result_total), 0)}</strong>'
        "</td>"
        '<td style="padding:10px;border:1px solid #ded6c8;background:#fdf9f1;">'
        '<div style="font-size:12px;color:#6c675f;text-transform:uppercase;">Hosted tours ready</div>'
        f'<strong style="font-size:18px;">{max(int(hosted_tour_total), 0)}</strong>'
        "</td>"
        "</tr></table>"
    )
    rows_html: list[str] = []
    for index, row in enumerate(property_rows[:5], start=1):
        title = str(row.get("title") or "Property match").strip() or "Property match"
        review_url = str(row.get("review_url") or "").strip()
        tour_url = str(row.get("tour_url") or "").strip()
        property_url = str(row.get("property_url") or "").strip()
        primary_url = review_url or tour_url or property_url
        facts_html = "".join(
            "<tr>"
            f'<td style="padding:4px 8px 4px 0;color:#6c675f;font-size:12px;white-space:nowrap;">{_html_escape(label)}</td>'
            f'<td style="padding:4px 0;color:#191714;font-size:13px;">{_html_escape(value)}</td>'
            "</tr>"
            for label, value in _property_email_facts(row)
        ) or '<tr><td style="padding:4px 0;color:#6c675f;font-size:13px;" colspan="2">No structured facts captured.</td></tr>'
        action_links = [
            _html_link(href=review_url, label="Open property") if review_url else "",
            _html_link(href=tour_url, label="Open 360") if tour_url else "",
            _html_link(href=property_url, label="Open listing") if property_url else "",
        ]
        action_html = " &nbsp; ".join(link for link in action_links if link) or _html_link(href=primary_url, label="Open")
        rows_html.append(
            "<tr>"
            f'<td style="width:38px;vertical-align:top;padding:12px;border-top:1px solid #ded6c8;color:#6c675f;">#{index}</td>'
            '<td style="vertical-align:top;padding:12px;border-top:1px solid #ded6c8;">'
            f'<div style="font-weight:700;margin-bottom:8px;">{_html_link(href=primary_url, label=title)}</div>'
            f"{_property_email_fit_note_html(row)}"
            '<table role="presentation" cellspacing="0" cellpadding="0" style="border-collapse:collapse;">'
            f"{facts_html}</table>"
            f'<div style="margin-top:10px;font-size:13px;">{action_html}</div>'
            "</td></tr>"
        )
    properties_table = ""
    if rows_html:
        properties_table = (
            '<h2 style="font-size:16px;margin:18px 0 8px;">Best matches</h2>'
            '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
            'style="border-collapse:collapse;border:1px solid #ded6c8;background:#fffefa;">'
            + "".join(rows_html)
            + "</table>"
        )
    results_link = (
        f'<p style="margin:18px 0 0;">{_html_link(href=results_url, label="Open shortlist")}</p>'
        if str(results_url or "").strip()
        else ""
    )
    return _html_email_shell(
        title="PropertyQuarry results are ready",
        body_html=(
            '<p style="margin:0 0 14px;color:#383633;">Your ranked shortlist is ready.</p>'
            f"{metric_table}{properties_table}{results_link}"
            + _email_footer_html(reason="You are receiving this because PropertyQuarry finished a search run for your account.")
        ),
    )


def _property_match_html(
    *,
    title: str,
    provider: str,
    primary_link: str,
    review_url: str,
    tour_url: str,
    property_url: str,
    fit_summary: str,
    decision_summary: dict[str, object],
) -> str:
    good_fit_reasons = [str(value or "").strip() for value in list(decision_summary.get("good_fit_reasons") or []) if str(value or "").strip()]
    bad_fit_reasons = [str(value or "").strip() for value in list(decision_summary.get("bad_fit_reasons") or []) if str(value or "").strip()]
    unknowns = [str(value or "").strip() for value in list(decision_summary.get("unknowns") or []) if str(value or "").strip()]
    facts = [
        ("Source", provider),
        ("Fit", fit_summary),
        ("360", "ready" if tour_url else "pending"),
    ]
    facts_html = "".join(
        "<tr>"
        f'<td style="padding:6px 10px;color:#6c675f;font-size:12px;white-space:nowrap;border-bottom:1px solid #ded6c8;">{_html_escape(label)}</td>'
        f'<td style="padding:6px 10px;color:#191714;font-size:13px;border-bottom:1px solid #ded6c8;">{_html_escape(value)}</td>'
        "</tr>"
        for label, value in facts
        if str(value or "").strip()
    )
    links = [
        _html_link(href=review_url or primary_link, label="Open property") if (review_url or primary_link) else "",
        _html_link(href=tour_url, label="Open 360") if tour_url else "",
        _html_link(href=property_url, label="Open listing") if property_url else "",
    ]
    action_urls = _property_decision_action_urls(packet_url=review_url or primary_link, tour_url=tour_url, property_url=property_url)
    decision_buttons = _email_button_row(
        [
            _email_button(href=action_urls["yes"], label="Yes, shortlist"),
            _email_button(href=action_urls["maybe"], label="Maybe, keep watching", kind="secondary"),
            _email_button(href=action_urls["no"], label="No — tell us why", kind="secondary"),
        ]
    )
    followup_buttons = _email_button_row(
        [
            _email_button(href=review_url or primary_link, label="Open property"),
            _email_button(href=tour_url, label="Open 360", kind="secondary"),
            _email_button(href=action_urls["ask_agent"], label="Ask agent", kind="secondary"),
        ]
    )
    reason_sections: list[str] = []
    for heading, values in (
        ("Why it stands out", good_fit_reasons[:4]),
        ("Main caution", bad_fit_reasons[:3]),
        ("Open checks", unknowns[:3]),
    ):
        if not values:
            continue
        reason_sections.append(
            f'<h2 style="font-size:15px;margin:16px 0 6px;">{_html_escape(heading)}</h2>'
            "<ul style=\"margin:0;padding-left:20px;\">"
            + "".join(f'<li style="margin:4px 0;">{_html_escape(value)}</li>' for value in values)
            + "</ul>"
        )
    return _html_email_shell(
        title=f"Property match: {title}",
        body_html=(
            '<div style="font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:#a37a2c;font-weight:700;margin:0 0 8px;">PropertyQuarry shortlist</div>'
            f'<h1 style="margin:0 0 10px;font-size:24px;line-height:1.25;color:#242321;">PropertyQuarry shortlisted a property match: {_html_escape(title)}</h1>'
            f'<p style="margin:0 0 12px;color:#51493f;">{_html_link(href=primary_link, label=title)}</p>'
            '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
            'style="border-collapse:collapse;border:1px solid #ded6c8;background:#fffefa;">'
            f"{facts_html}</table>"
            f"{decision_buttons}{followup_buttons}"
            f'<p style="margin:14px 0 0;">{" &nbsp; ".join(link for link in links if link)}</p>'
            + "".join(reason_sections)
            + _email_footer_html(reason="You are receiving this because PropertyQuarry shortlisted a property for your account.")
        ),
    )


def _cloudflare_error_code(payload: object) -> str:
    if not isinstance(payload, dict):
        return "unknown"
    errors = payload.get("errors")
    if not isinstance(errors, list) or not errors or not isinstance(errors[0], dict):
        return "unknown"
    normalized = str(errors[0].get("code") or "unknown").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", normalized):
        return "unknown"
    return normalized


def _cloudflare_response_payload(
    request: urllib.request.Request,
    *,
    failure_code: str,
) -> dict[str, object]:
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw or "{}")
        except Exception:
            payload = {}
        raise RuntimeError(
            f"{failure_code}:{exc.code}:{_cloudflare_error_code(payload)}"
        ) from None
    try:
        payload = json.loads(raw or "{}")
    except Exception:
        raise RuntimeError(f"{failure_code}:invalid_response") from None
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise RuntimeError(
            f"{failure_code}:provider_rejected:{_cloudflare_error_code(payload)}"
        )
    return payload


def _cloudflare_recipient_is_verified(
    *,
    token: str,
    account_id: str,
    recipient_email: str,
) -> bool:
    normalized_recipient = recipient_email.strip().lower()
    per_page = 50
    for page in range(1, 21):
        query = urllib.parse.urlencode({"page": page, "per_page": per_page})
        request = urllib.request.Request(
            f"{CLOUDFLARE_API_BASE}/accounts/{account_id}/email/routing/addresses?{query}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            method="GET",
        )
        payload = _cloudflare_response_payload(
            request,
            failure_code="registration_email_cloudflare_destination_check_failed",
        )
        rows = payload.get("result")
        if not isinstance(rows, list):
            raise RuntimeError(
                "registration_email_cloudflare_destination_check_failed:invalid_result"
            )
        if any(
            isinstance(row, dict)
            and bool(row.get("verified"))
            and str(row.get("email") or "").strip().lower() == normalized_recipient
            for row in rows
        ):
            return True
        result_info = payload.get("result_info")
        raw_total_pages = (
            result_info.get("total_pages") if isinstance(result_info, dict) else 0
        )
        try:
            total_pages = int(raw_total_pages or 0)
        except (TypeError, ValueError, OverflowError):
            raise RuntimeError(
                "registration_email_cloudflare_destination_check_failed:"
                "invalid_pagination"
            ) from None
        if isinstance(raw_total_pages, bool) or total_pages < 0:
            raise RuntimeError(
                "registration_email_cloudflare_destination_check_failed:"
                "invalid_pagination"
            )
        if (total_pages and page >= total_pages) or (not total_pages and len(rows) < per_page):
            break
    return False


def _send_cloudflare_verified_email(
    *,
    recipient_email: str,
    subject: str,
    text: str,
    html_body: str,
    sender_email: str,
    sender_name: str,
    idempotency_key: str,
) -> RegistrationEmailReceipt:
    credentials = _cloudflare_email_credentials()
    if credentials is None:
        raise RuntimeError("registration_email_cloudflare_not_configured")
    token, account_id = credentials
    normalized_recipient = str(recipient_email or "").strip()
    if not _SENDER_ADDR_SPEC_RE.fullmatch(normalized_recipient):
        raise RuntimeError("registration_email_cloudflare_recipient_invalid")
    normalized_sender = _enforce_propertyquarry_production_sender(sender_email)
    if not _propertyquarry_sender_email_allowed(normalized_sender):
        raise RuntimeError("registration_email_cloudflare_sender_domain_forbidden")
    if not _cloudflare_recipient_is_verified(
        token=token,
        account_id=account_id,
        recipient_email=normalized_recipient,
    ):
        raise RuntimeError("registration_email_cloudflare_recipient_unverified")

    provider_key = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    body: dict[str, object] = {
        "from": {"address": normalized_sender, "name": sender_name},
        "to": [normalized_recipient],
        "subject": str(subject or "").strip(),
        "text": str(text or "").strip(),
        "reply_to": normalized_sender,
        "headers": {"X-PropertyQuarry-Idempotency-Key": provider_key},
    }
    normalized_html = str(html_body or "").strip()
    if normalized_html:
        body["html"] = normalized_html
    request = urllib.request.Request(
        f"{CLOUDFLARE_API_BASE}/accounts/{account_id}/email/sending/send",
        data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    payload = _cloudflare_response_payload(
        request,
        failure_code="registration_email_cloudflare_send_failed",
    )
    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("registration_email_cloudflare_send_failed:invalid_result")
    normalized_target = normalized_recipient.lower()
    delivered_or_queued = {
        str(value or "").strip().lower()
        for key in ("delivered", "queued")
        for value in (result.get(key) if isinstance(result.get(key), list) else [])
    }
    permanent_bounces = result.get("permanent_bounces")
    message_id = str(result.get("message_id") or "").strip()
    if (
        normalized_target not in delivered_or_queued
        or (isinstance(permanent_bounces, list) and bool(permanent_bounces))
        or not message_id
    ):
        raise RuntimeError("registration_email_cloudflare_send_failed:not_accepted")
    return RegistrationEmailReceipt(
        provider="cloudflare_email_sending",
        message_id=message_id,
        accepted_at=datetime.now(timezone.utc).isoformat(),
    )


def _send_emailit_email(
    *,
    recipient_email: str,
    subject: str,
    text: str,
    html_body: str = "",
    kind: str,
    meta: dict[str, object] | None = None,
    sender_email: str = "",
    sender_name: str = "",
    idempotency_key: str = "",
) -> RegistrationEmailReceipt:
    api_key = str(os.environ.get("EMAILIT_API_KEY") or "").strip()
    resolved_sender_email = _resolved_sender_email(sender_email)
    resolved_sender_name = _resolved_sender_name(sender_name)
    payload = {
        "from": f"{resolved_sender_name} <{resolved_sender_email}>",
        "to": str(recipient_email or "").strip(),
        "subject": str(subject or "").strip(),
        "text": _strip_plaintext_urls(text).strip(),
        "html": str(html_body or "").strip(),
        "reply_to": resolved_sender_email,
        "tracking": False,
        "meta": _emailit_meta_payload(kind=kind, recipient_email=recipient_email, meta=meta),
    }
    idempotency_seed = json.dumps(
        {
            "kind": kind,
            "recipient_email": str(recipient_email or "").strip().lower(),
            "subject": str(subject or "").strip(),
            "meta": dict(meta or {}),
            "sender_email": resolved_sender_email.lower(),
            "sender_name": resolved_sender_name,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    requested_idempotency_key = str(idempotency_key or "").strip()
    resolved_idempotency_key = (
        f"ea-mail-{hashlib.sha256(requested_idempotency_key.encode('utf-8')).hexdigest()[:32]}"
        if requested_idempotency_key
        else f"ea-mail-{hashlib.sha256(idempotency_seed.encode('utf-8')).hexdigest()[:24]}"
    )
    if not api_key:
        if _cloudflare_email_credentials() is None:
            raise RuntimeError("registration_email_api_key_missing")
        return _send_cloudflare_verified_email(
            recipient_email=recipient_email,
            subject=subject,
            text=str(payload["text"]),
            html_body=str(payload["html"]),
            sender_email=resolved_sender_email,
            sender_name=resolved_sender_name,
            idempotency_key=resolved_idempotency_key,
        )

    def _request_for_payload(active_payload: dict[str, object], active_idempotency_key: str):
        return urllib.request.Request(
            EMAILIT_API_BASE,
            data=json.dumps(active_payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Idempotency-Key": active_idempotency_key,
            },
            method="POST",
        )

    request = _request_for_payload(payload, resolved_idempotency_key)
    last_error = ""
    used_fallback_sender = False
    cloudflare_error = ""
    cloudflare_attempted = False
    for _ in range(7):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read().decode("utf-8", errors="replace")
            parsed = json.loads(body or "{}")
            return RegistrationEmailReceipt(
                provider="emailit",
                message_id=str(parsed.get("id") or ""),
                accepted_at=datetime.now(timezone.utc).isoformat(),
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            last_error = f"registration_email_send_failed:{exc.code}:{detail[:600]}"
            if exc.code == 422 and "Domain not verified" in detail and not used_fallback_sender:
                if not cloudflare_attempted:
                    cloudflare_attempted = True
                    try:
                        if _cloudflare_email_credentials() is not None:
                            return _send_cloudflare_verified_email(
                                recipient_email=recipient_email,
                                subject=subject,
                                text=str(payload["text"]),
                                html_body=str(payload["html"]),
                                sender_email=resolved_sender_email,
                                sender_name=resolved_sender_name,
                                idempotency_key=resolved_idempotency_key,
                            )
                    except RuntimeError as cloudflare_exc:
                        cloudflare_error = str(cloudflare_exc)
                fallback_email = _fallback_sender_email()
                if (
                    fallback_email
                    and fallback_email.lower() != resolved_sender_email.lower()
                    and (
                        not _propertyquarry_production_sender_required()
                        or _propertyquarry_sender_email_allowed(fallback_email)
                    )
                ):
                    fallback_name = _fallback_sender_name()
                    payload["from"] = f"{fallback_name} <{fallback_email}>"
                    payload["reply_to"] = fallback_email
                    payload["meta"] = _emailit_meta_payload(
                        kind=kind,
                        recipient_email=recipient_email,
                        meta={
                            **dict(payload.get("meta") or {}),
                            "sender_fallback_used": True,
                            "preferred_sender_email": resolved_sender_email,
                            "fallback_sender_email": fallback_email,
                        },
                    )
                    fallback_seed = json.dumps(
                        {
                            "kind": kind,
                            "recipient_email": str(recipient_email or "").strip().lower(),
                            "subject": str(subject or "").strip(),
                            "meta": dict(payload.get("meta") or {}),
                            "sender_email": fallback_email.lower(),
                            "sender_name": fallback_name,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    fallback_key_seed = (
                        f"{requested_idempotency_key}:fallback:{fallback_email.lower()}"
                        if requested_idempotency_key
                        else fallback_seed
                    )
                    request = _request_for_payload(
                        payload,
                        f"ea-mail-{hashlib.sha256(fallback_key_seed.encode('utf-8')).hexdigest()[:32]}",
                    )
                    used_fallback_sender = True
                    continue
            if exc.code == 429:
                retry_after = 1
                try:
                    retry_after = int(json.loads(detail).get("retry_after") or 1)
                except Exception:
                    retry_after = 1
                time.sleep(max(1, retry_after))
                continue
            break
    if cloudflare_error:
        raise RuntimeError(
            f"{last_error or 'registration_email_send_failed'}:{cloudflare_error}"
        )
    raise RuntimeError(last_error or "registration_email_send_failed")


def send_registration_email(*, recipient_email: str, verification_code: str, magic_link_url: str, expires_at: int) -> RegistrationEmailReceipt:
    return _send_emailit_email(
        recipient_email=recipient_email,
        subject=_registration_subject(),
        text=_registration_text(
            verification_code=verification_code,
            magic_link_url=magic_link_url,
            expires_at=expires_at,
        ),
        html_body=_registration_html(
            verification_code=verification_code,
            magic_link_url=magic_link_url,
            expires_at=expires_at,
        ),
        kind="propertyquarry_registration_verification_v2",
        meta={"verification_ref": _registration_verification_ref()},
    )


def send_workspace_invitation_email(
    *,
    recipient_email: str,
    invite_url: str,
    role: str,
    invited_by: str,
    note: str = "",
    expires_at: str = "",
) -> RegistrationEmailReceipt:
    minutes = _minutes_until(expires_at_iso=expires_at)
    role_label = str(role or "operator").strip().replace("_", " ").title() or "Operator"
    inviter = str(invited_by or "PropertyQuarry").strip() or "PropertyQuarry"
    note_text = str(note or "").strip()
    body = [
        "Hello,",
        "",
        f"{inviter} invited you to join a PropertyQuarry account as {role_label}.",
        "",
        "Open this secure link to accept the invite:",
        "",
        invite_url,
        "",
        f"This link expires in about {minutes} minutes.",
    ]
    if note_text:
        body.extend(["", "Message from the sender:", note_text])
    body.extend(
        [
            "",
            "You will get account access after accepting the invite.",
            "Google can optionally confirm your PropertyQuarry identity. It does not connect Gmail, Calendar, MyExternalBrain, or EA.",
        ]
    )
    return _send_emailit_email(
        recipient_email=recipient_email,
        subject=f"{inviter} invited you to PropertyQuarry",
        text="\n".join(body).strip() + "\n",
        html_body=_workspace_email_shell(
            eyebrow="Account invite",
            title=f"{inviter} invited you to PropertyQuarry",
            summary="Review the invite, confirm the role, and continue through a secure account link before it expires.",
            primary_label="Open invite",
            primary_href=invite_url,
            detail_rows=[
                ("Role", role_label),
                ("Invited by", inviter),
                ("Expires in", f"about {minutes} minutes"),
                *([("Message", note_text)] if note_text else []),
            ],
            preheader="Review the invite and continue through a secure PropertyQuarry account link.",
        ),
        kind="ea_workspace_invitation",
        meta={"invite_ref": _meta_ref(invite_url), "role": str(role or "").strip().lower()},
    )


def send_workspace_access_email(
    *,
    recipient_email: str,
    workspace_name: str,
    access_url: str,
    role: str,
    display_name: str = "",
    expires_at: str = "",
) -> RegistrationEmailReceipt:
    minutes = _minutes_until(expires_at_iso=expires_at)
    role_label = str(role or "principal").strip().replace("_", " ").title() or "Principal"
    workspace_label = str(workspace_name or "PropertyQuarry account").strip() or "PropertyQuarry account"
    display = str(display_name or "").strip()
    body = [
        "Hello,",
        "",
        f"Use the titled access button in this email to return to {workspace_label}.",
        "",
        f"This link expires in about {minutes} minutes.",
    ]
    if display:
        body.extend(["", f"This link opens your {role_label.lower()} access as {display}."])
    else:
        body.extend(["", f"This link opens your {role_label.lower()} access to PropertyQuarry."])
    body.extend(
        [
            "",
            "Google is connected later as an optional account data source. It is not your app login.",
        ]
    )
    return _send_emailit_email(
        recipient_email=recipient_email,
        subject=f"Your access link for {workspace_label}",
        text="\n".join(body).strip() + "\n",
        html_body=_workspace_email_shell(
            eyebrow="Account access",
            title=f"Your access link for {workspace_label}",
            summary="Open the secure link below to return directly into PropertyQuarry without restarting the whole sign-in flow.",
            primary_label="Open access link",
            primary_href=access_url,
            detail_rows=[
                ("Account", workspace_label),
                ("Role", role_label),
                *([("Identity", display)] if display else []),
                ("Expires in", f"about {minutes} minutes"),
            ],
            preheader="Open your secure PropertyQuarry access link before it expires.",
        ),
        kind="ea_workspace_access_session",
        meta={
            "access_ref": _meta_ref(access_url),
            "workspace_name": workspace_label,
            "role": str(role or "").strip().lower(),
        },
    )


def send_google_connect_email(
    *,
    recipient_email: str,
    workspace_name: str,
    connect_url: str,
    scope_label: str,
    scope_summary: str,
    primary_google_email: str = "",
    connected_account_total: int = 0,
    expires_at: str = "",
) -> RegistrationEmailReceipt:
    minutes = _minutes_until(expires_at_iso=expires_at)
    workspace_label = str(workspace_name or "PropertyQuarry account").strip() or "PropertyQuarry account"
    label = str(scope_label or "Google Full Workspace").strip() or "Google Full Workspace"
    summary = str(scope_summary or "").strip()
    primary_email = str(primary_google_email or "").strip().lower()
    body = [
        "Hello,",
        "",
        f"Use the titled Google-connect button in this email to connect a Google inbox to {workspace_label}.",
        "",
        f"This link expires in about {minutes} minutes.",
        "",
        "The link signs you into PropertyQuarry first, then starts Google consent.",
        f"Requested bundle: {label}.",
    ]
    if summary:
        body.extend(["", summary])
    if connected_account_total <= 0:
        body.extend(["", "No Google inbox is connected to this account yet, so this link will attach the first one."])
    elif primary_email:
        body.extend(["", f"The current primary inbox stays {primary_email}. This link adds another inbox to the same account."])
    else:
        body.extend(["", "This link adds another Google inbox to the same account."])
    body.extend(
        [
            "",
            "Google is used here as account data and action consent. It is not your app login.",
        ]
    )
    return _send_emailit_email(
        recipient_email=recipient_email,
        subject=f"Connect Google to {workspace_label}",
        text="\n".join(body).strip() + "\n",
        html_body=_workspace_email_shell(
            eyebrow="Google connection",
            title=f"Connect Google to {workspace_label}",
            summary="This secure link returns to PropertyQuarry first and then starts the Google consent flow for the requested bundle.",
            primary_label="Connect Google",
            primary_href=connect_url,
            detail_rows=[
                ("Account", workspace_label),
                ("Bundle", label),
                *([("Summary", summary)] if summary else []),
                ("Connected inboxes", str(max(int(connected_account_total or 0), 0))),
                *([("Primary inbox", primary_email)] if primary_email else []),
                ("Expires in", f"about {minutes} minutes"),
            ],
            preheader="Return to PropertyQuarry and start the Google consent flow from a secure account link.",
        ),
        kind="ea_google_connect_link",
        sender_email=str(os.environ.get("EA_EMAIL_DEFAULT_FROM") or "").strip(),
        sender_name=str(os.environ.get("EA_EMAIL_DEFAULT_NAME") or "").strip(),
        meta={
            "connect_ref": _meta_ref(connect_url),
            "workspace_name": workspace_label,
            "scope_label": label,
        },
    )


def send_property_tour_email(
    *,
    recipient_email: str,
    property_title: str,
    property_url: str,
    tour_url: str,
    variant_key: str = "",
    listing_id: str = "",
    area_label: str = "",
    rooms_label: str = "",
    price_label: str = "",
    decision_summary_json: dict[str, object] | None = None,
    sender_email: str = "",
    sender_name: str = "",
) -> RegistrationEmailReceipt:
    title = str(property_title or "Apartment tour").strip() or "Apartment tour"
    property_url = _canonical_property_research_url(property_url)
    variant_label = str(variant_key or "").strip().replace("_", " ")
    subject = f"Apartment tour ready: {title}"
    if variant_label:
        subject = f"{subject} · {variant_label}"
    body = [
        "Hello,",
        "",
        f"PropertyQuarry prepared a 360 review for {title}.",
        "",
        "Open the titled review button in this email.",
    ]
    if property_url:
        body.append("Use the titled source-listing button if you need the original listing.")
    if listing_id:
        body.append(f"Listing ID: {listing_id}")
    facts = [value for value in (area_label, rooms_label, price_label) if str(value or "").strip()]
    if facts:
        body.extend(["", "Quick facts:", *facts])
    decision_summary = decision_summary_json if isinstance(decision_summary_json, dict) else {}
    good_fit_reasons = [str(value or "").strip() for value in list(decision_summary.get("good_fit_reasons") or []) if str(value or "").strip()]
    bad_fit_reasons = [str(value or "").strip() for value in list(decision_summary.get("bad_fit_reasons") or []) if str(value or "").strip()]
    unknowns = [str(value or "").strip() for value in list(decision_summary.get("unknowns") or []) if str(value or "").strip()]
    recommendation = str(decision_summary.get("recommendation") or "").strip().replace("_", " ")
    if recommendation:
        body.extend(["", f"Recommendation: {recommendation}"])
    if good_fit_reasons:
        body.extend(["", "Why it stands out:", *[f"- {entry}" for entry in good_fit_reasons[:3]]])
    if bad_fit_reasons:
        body.extend(["", "Main caution:", *[f"- {entry}" for entry in bad_fit_reasons[:3]]])
    if unknowns:
        body.extend(["", "Open checks:", *[f"- {entry}" for entry in unknowns[:3]]])
    body.extend(
        [
            "",
            "Open the 360 review first, then continue into the property page if you need the full evidence and decisions.",
        ]
    )
    facts_table_html = "".join(
        "<tr>"
        f'<td style="padding:8px 10px 8px 0;color:#6c675f;font-size:12px;white-space:nowrap;border-top:1px solid #ece4d8;">{_html_escape(label)}</td>'
        f'<td style="padding:8px 0;color:#242321;font-size:13px;border-top:1px solid #ece4d8;">{_html_escape(value)}</td>'
        "</tr>"
        for label, value in (
            ("Listing ID", listing_id),
            ("Area", area_label),
            ("Rooms", rooms_label),
            ("Price", price_label),
        )
        if str(value or "").strip()
    )
    html_body = (
        '<div style="font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:#a37a2c;font-weight:700;margin:0 0 10px;">Review page</div>'
        '<p style="margin:0 0 16px;font-size:15px;line-height:1.65;color:#51493f;">PropertyQuarry prepared a 360 review page for this property. Open the space first, then continue into the property page and the decision desk.</p>'
    )
    action_urls = _property_decision_action_urls(packet_url=property_url, tour_url=tour_url, property_url=property_url)
    html_body += _email_button_row(
        [
            _email_button(href=tour_url, label="Open 360 review"),
            _email_button(href=property_url, label="Open property", kind="secondary") if str(property_url or "").strip() else "",
            _email_button(href=action_urls["yes"], label="Yes, shortlist", kind="secondary"),
            _email_button(href=action_urls["no"], label="No — tell us why", kind="secondary"),
            _email_button(href=action_urls["ask_agent"], label="Ask agent", kind="secondary"),
        ]
    )
    if str(property_url or "").strip():
        html_body += f'<p style="margin:0 0 14px;">{_html_link(href=property_url, label="Open property")}</p>'
    if facts_table_html:
        html_body += (
            '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;margin:12px 0 0;">'
            + facts_table_html
            + "</table>"
        )
    html_body += _email_footer_html(reason="You are receiving this because PropertyQuarry prepared a property page for a property in your account.")
    return _send_emailit_email(
        recipient_email=recipient_email,
        subject=subject[:220],
        text="\n".join(body).strip() + "\n",
        html_body=_html_email_shell(
            title=f"360 review ready: {title}",
            preheader="PropertyQuarry prepared a 360 review for this property.",
            body_html=html_body,
        ),
        kind="ea_property_tour_delivery",
        meta={
            "tour_ref": _meta_ref(tour_url),
            "listing_ref": _meta_ref(property_url),
            "variant_key": str(variant_key or "").strip(),
        },
        sender_email=sender_email,
        sender_name=sender_name,
    )


def send_property_match_email(
    *,
    recipient_email: str,
    property_title: str,
    property_url: str,
    review_url: str = "",
    tour_url: str = "",
    provider_label: str = "",
    fit_summary: str = "",
    decision_summary_json: dict[str, object] | None = None,
    sender_email: str = "",
    sender_name: str = "",
) -> RegistrationEmailReceipt:
    title = str(property_title or "Property match").strip() or "Property match"
    provider = str(provider_label or "").strip()
    review_url = _canonical_property_research_url(review_url)
    subject = f"Property match: {title}"
    primary_link = str(tour_url or review_url or property_url).strip()
    body = [
        "Hello,",
        "",
        f"PropertyQuarry shortlisted a property match: {title}",
    ]
    if provider:
        body.append(f"Source: {provider}")
    if fit_summary:
        body.extend(["", fit_summary])
    if primary_link:
        body.extend(["", "Open the titled review button in this email."])
    if review_url and review_url != primary_link:
        body.append("A titled research-packet button is included.")
    if tour_url and tour_url not in {primary_link, review_url}:
        body.append("A titled hosted-tour button is included.")
    if property_url and property_url not in {primary_link, review_url, tour_url}:
        body.append("A titled original-listing button is included.")
    decision_summary = decision_summary_json if isinstance(decision_summary_json, dict) else {}
    good_fit_reasons = [str(value or "").strip() for value in list(decision_summary.get("good_fit_reasons") or []) if str(value or "").strip()]
    bad_fit_reasons = [str(value or "").strip() for value in list(decision_summary.get("bad_fit_reasons") or []) if str(value or "").strip()]
    unknowns = [str(value or "").strip() for value in list(decision_summary.get("unknowns") or []) if str(value or "").strip()]
    if good_fit_reasons:
        body.extend(["", "Why it stands out:", *[f"- {entry}" for entry in good_fit_reasons[:4]]])
    if bad_fit_reasons:
        body.extend(["", "Main caution:", *[f"- {entry}" for entry in bad_fit_reasons[:3]]])
    if unknowns:
        body.extend(["", "Open checks:", *[f"- {entry}" for entry in unknowns[:3]]])
    return _send_emailit_email(
        recipient_email=recipient_email,
        subject=subject[:220],
        text="\n".join(body).strip() + "\n",
        html_body=_property_match_html(
            title=title,
            provider=provider,
            primary_link=primary_link,
            review_url=review_url,
            tour_url=tour_url,
            property_url=property_url,
            fit_summary=fit_summary,
            decision_summary=decision_summary,
        ),
        kind="ea_property_match_delivery",
        meta={
            "review_ref": _meta_ref(review_url or primary_link),
            "tour_ref": _meta_ref(tour_url),
            "listing_ref": _meta_ref(property_url),
        },
        sender_email=sender_email,
        sender_name=sender_name,
    )


def send_property_market_ready_email(
    *,
    recipient_email: str,
    country_label: str,
    workspace_url: str,
    sender_email: str = "",
    sender_name: str = "",
) -> RegistrationEmailReceipt:
    label = str(country_label or "Requested market").strip() or "Requested market"
    review_url = str(workspace_url or "").strip()
    body = [
        "Hello,",
        "",
        f"Initialization for {label} is complete.",
        "",
        "Your market is now ready in PropertyQuarry. You can start the search now.",
    ]
    if review_url:
        body.extend(["", "Open PropertyQuarry with the titled account button in this email."])
    return _send_emailit_email(
        recipient_email=recipient_email,
        subject=f"PropertyQuarry market ready: {label}"[:220],
        text="\n".join(body).strip() + "\n",
        html_body=_workspace_email_shell(
            eyebrow="Market ready",
            title=f"{label} is ready in PropertyQuarry",
            summary="The market initialization finished and the search desk can now start the first real sweep.",
            primary_label="Open PropertyQuarry",
            primary_href=review_url,
            detail_rows=[("Market", label)],
            preheader="The market setup is complete and PropertyQuarry is ready for the next search.",
        ),
        kind="ea_property_market_ready_delivery",
        meta={"market_label": label, "workspace_ref": _meta_ref(review_url)},
        sender_email=sender_email,
        sender_name=sender_name,
    )


def send_property_search_results_ready_email(
    *,
    recipient_email: str,
    results_url: str,
    result_total: int,
    hosted_tour_total: int,
    top_properties: list[dict[str, object]] | None = None,
    sender_email: str = "",
    sender_name: str = "",
) -> RegistrationEmailReceipt:
    property_rows = [dict(item) for item in list(top_properties or []) if isinstance(item, dict)]
    best_row = property_rows[0] if property_rows else {}
    best_title = str(best_row.get("title") or "No ranked property yet").strip() or "No ranked property yet"
    best_fit_summary = str(best_row.get("fit_summary") or "").strip()
    best_fit_note = _property_email_fit_note(best_row)
    best_facts = " | ".join(
        part
        for part in (
            str(best_row.get("price_label") or "").strip(),
            str(best_row.get("area_label") or "").strip(),
            str(best_row.get("rooms_label") or "").strip(),
            str(best_row.get("location_label") or "").strip(),
        )
        if part
    )
    body = [
        "Hello,",
        "",
        "Your PropertyQuarry results are ready.",
        "",
        f"Ranked results: {max(int(result_total), 0)}",
        f"Hosted tours ready: {max(int(hosted_tour_total), 0)}",
    ]
    if property_rows:
        body.extend(
            [
                "",
                "Research summary:",
                f"- Best current match: {best_title}",
                f"- Assessment: {best_fit_summary or 'A ranked shortlist is ready for review.'}",
                f"- Fit note: {best_fit_note or 'It best matches your search.'}",
                f"- Key facts: {best_facts or 'Open the packet for the structured facts.'}",
            ]
        )
    if property_rows:
        body.extend(["", "Best matches:"])
        for index, row in enumerate(property_rows[:5], start=1):
            title = str(row.get("title") or "Property match").strip() or "Property match"
            source = str(row.get("source_label") or "").strip()
            fit_summary = str(row.get("fit_summary") or "").strip()
            fit_note = _property_email_fit_note(row)
            price_label = str(row.get("price_label") or "").strip()
            area_label = str(row.get("area_label") or "").strip()
            rooms_label = str(row.get("rooms_label") or "").strip()
            location_label = str(row.get("location_label") or "").strip()
            review_url = _canonical_property_research_url(row.get("review_url") or "")
            tour_url = str(row.get("tour_url") or "").strip()
            property_url = str(row.get("property_url") or "").strip()
            tour_status = str(row.get("tour_status") or "").strip().replace("_", " ") or (
                "ready" if tour_url else "pending"
            )
            body.append(f"{index}. {title}")
            details = [part for part in (fit_summary, source, f"360: {tour_status}") if part]
            if details:
                body.append(f"   {' | '.join(details)}")
            if fit_note:
                body.append(f"   Fit note: {fit_note}")
            facts_line = " | ".join(part for part in (price_label, area_label, rooms_label, location_label) if part)
            if facts_line:
                body.append(f"   Facts: {facts_line}")
            if review_url:
                body.append("   Action: open the titled property-page button.")
            elif property_url:
                body.append("   Action: open the titled listing button.")
            if tour_url:
                body.append("   Action: open the titled 360-view button.")
    if str(results_url or "").strip():
        body.extend(["", "Open the shortlist with the titled button in this email."])
    cards = []
    for row in property_rows[:5]:
        title = html.escape(str(row.get("title") or "Property match").strip() or "Property match")
        fit_summary = html.escape(str(row.get("fit_summary") or "").strip() or "Ranked and ready for review.")
        fit_note = html.escape(_property_email_fit_note(row))
        source = html.escape(str(row.get("source_label") or "").strip())
        facts_line = " | ".join(
            html.escape(part)
            for part in (
                str(row.get("price_label") or "").strip(),
                str(row.get("area_label") or "").strip(),
                str(row.get("rooms_label") or "").strip(),
                str(row.get("location_label") or "").strip(),
            )
            if part
        )
        review_url = html.escape(_canonical_property_research_url(row.get("review_url") or ""))
        tour_url = html.escape(str(row.get("tour_url") or "").strip())
        property_url = html.escape(str(row.get("property_url") or "").strip())
        actions = []
        if review_url:
            actions.append(_email_button(href=review_url, label="Open property", kind="secondary"))
        if tour_url:
            actions.append(_email_button(href=tour_url, label="Open 360"))
        elif property_url:
            actions.append(_email_button(href=property_url, label="Open listing", kind="secondary"))
        cards.append(
            """
            <tr>
              <td style="padding:0 0 16px 0;">
                <div style="border:1px solid #ded6c8;border-radius:16px;background:#fffdf8;padding:18px;">
                  <div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#7d7468;margin-bottom:8px;">%s</div>
                  <div style="font-size:18px;line-height:1.3;font-weight:700;color:#242321;margin-bottom:8px;">%s</div>
                  <div style="font-size:14px;line-height:1.55;color:#51493f;margin-bottom:8px;">%s</div>
                  %s
                  %s
                  <div style="padding-top:14px;">%s</div>
                </div>
              </td>
            </tr>
            """
            % (
                source or "PropertyQuarry shortlist",
                title,
                fit_summary,
                f'<div style="font-size:13px;line-height:1.55;color:#3f4c46;margin-bottom:10px;"><strong>Fit note:</strong> {fit_note}</div>' if fit_note else "",
                f'<div style="font-size:13px;line-height:1.5;color:#7d7468;margin-bottom:12px;">{facts_line}</div>' if facts_line else "",
                "&nbsp;".join(actions),
            )
        )
    html_body = (
        f"""
        <div style="margin:0;padding:24px;background:#f6f3ee;font-family:Arial,sans-serif;color:#242321;">
          <div style="max-width:760px;margin:0 auto;background:#fffdf8;border:1px solid #ded6c8;border-radius:24px;overflow:hidden;">
            <div style="padding:28px 28px 20px;background:linear-gradient(135deg,#fff7ea 0%,#f6f3ee 100%);border-bottom:1px solid #e7dece;">
              <div style="font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:#a37a2c;font-weight:700;">PropertyQuarry research brief</div>
              <h1 style="margin:10px 0 12px 0;font-size:28px;line-height:1.2;color:#242321;">Your search is ready</h1>
              <p style="margin:0 0 16px 0;font-size:15px;line-height:1.65;color:#51493f;">
                We ranked <strong>{max(int(result_total), 0)}</strong> result(s) and prepared
                <strong>{max(int(hosted_tour_total), 0)}</strong> hosted tour view(s).
              </p>
              <div style="border:1px solid #e1d1b4;border-radius:18px;background:#fffdf8;padding:16px;">
                <div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#7d7468;margin-bottom:6px;">Quick take</div>
                <div style="font-size:18px;line-height:1.35;font-weight:700;color:#242321;margin-bottom:8px;">{html.escape(best_title)}</div>
                <div style="font-size:14px;line-height:1.6;color:#51493f;">{html.escape(best_fit_summary or 'A ranked shortlist is ready for review.')}</div>
                {f'<div style="font-size:13px;line-height:1.55;color:#3f4c46;margin-top:10px;"><strong>Fit note:</strong> {html.escape(best_fit_note)}</div>' if best_fit_note else ''}
                {f'<div style="font-size:13px;line-height:1.5;color:#7d7468;margin-top:10px;">{html.escape(best_facts)}</div>' if best_facts else ''}
              </div>
              <div style="padding-top:18px;">
                {_email_button(href=str(results_url or '').strip(), label="Open full search desk")}
              </div>
            </div>
            <div style="padding:24px 28px 12px;">
              <div style="font-size:13px;line-height:1.6;color:#51493f;margin-bottom:18px;">
                The links below open directly. If your PropertyQuarry session is not active yet, PropertyQuarry will establish access first and then continue to the correct property.
              </div>
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                {''.join(cards)}
              </table>
              {_email_footer_html(reason="You are receiving this because PropertyQuarry finished a search run for your account.")}
            </div>
          </div>
        </div>
        """
        if str(results_url or "").strip()
        else _property_search_results_ready_html(
            results_url=str(results_url or "").strip(),
            result_total=result_total,
            hosted_tour_total=hosted_tour_total,
            property_rows=property_rows,
        )
    )
    return _send_emailit_email(
        recipient_email=recipient_email,
        subject="PropertyQuarry results ready"[:220],
        text="\n".join(body).strip() + "\n",
        html_body=html_body,
        kind="ea_property_search_results_ready_delivery",
        meta={
            "results_ref": _meta_ref(results_url),
            "top_property_refs": [
                _meta_ref(row.get("review_url") or row.get("tour_url") or row.get("property_url"))
                for row in property_rows[:5]
            ],
        },
        sender_email=sender_email,
        sender_name=sender_name,
    )


def property_notification_preview(template_key: str) -> dict[str, object]:
    normalized = str(template_key or "").strip().lower()
    if normalized == "search_results_ready":
        property_rows = [
            {
                "title": "Altbau near U6",
                "fit_summary": "Personal fit 92/100 · shortlist · Lift and transit fit.",
                "compare_reason": "Chosen ahead of the next option because it scored 5 points higher on the current brief and includes a floorplan.",
                "price_label": "EUR 420,000",
                "area_label": "78 m2",
                "rooms_label": "3 rooms",
                "location_label": "Berlin Mitte",
                "source_label": "ImmoScout24 Germany",
                "review_url": "https://propertyquarry.com/app/research/altbau-near-u6?run_id=run-42",
                "tour_url": "https://propertyquarry.com/tours/altbau-near-u6",
                "property_url": "https://www.immobilienscout24.de/expose/altbau-near-u6",
                "tour_status": "ready",
            },
            {
                "title": "Family flat near Augarten",
                "fit_summary": "Personal fit 88/100 · family shortlist · Floorplan and daily-life radius fit.",
                "price_label": "EUR 498,000",
                "area_label": "84 m2",
                "rooms_label": "3 rooms",
                "location_label": "1020 Wien",
                "source_label": "Willhaben",
                "review_url": "https://propertyquarry.com/app/research/family-flat-near-augarten?run_id=run-42",
                "tour_url": "",
                "property_url": "https://www.willhaben.at/iad/immobilien/d/demo",
                "tour_status": "queued",
            },
        ]
        best_row = property_rows[0]
        return {
            "template_key": normalized,
            "subject": "PropertyQuarry found 2 strong matches · 1 tour ready",
            "preheader": "Your ranked shortlist, property pages, and 360 reviews are ready.",
            "text": (
                "Hello,\n\n"
                "Your PropertyQuarry results are ready.\n\n"
                "Ranked results: 2\n"
                "Hosted tours ready: 1\n\n"
                f"Research summary:\n- Best current match: {best_row['title']}\n"
                f"- Assessment: {best_row['fit_summary']}\n"
                f"- Fit note: {normalize_property_fit_note(best_row['compare_reason'])}\n"
                "- Key facts: EUR 420,000 | 78 m2 | 3 rooms | Berlin Mitte\n\n"
                "Open the shortlist with the titled button in this email.\n"
            ),
            "html": _property_search_results_ready_html(
                results_url="https://propertyquarry.com/app/shortlist?run_id=run-42",
                result_total=2,
                hosted_tour_total=1,
                property_rows=property_rows,
            ),
        }
    if normalized == "property_match":
        decision_summary = {
            "good_fit_reasons": ["Lift and transit fit."],
            "bad_fit_reasons": ["Street noise still unknown."],
            "unknowns": ["Heating source still needs confirmation."],
        }
        return {
            "template_key": normalized,
            "subject": "Property match: Altbau near U6",
            "preheader": "A new property is worth reviewing.",
            "text": (
                "Hello,\n\n"
                "PropertyQuarry shortlisted a property match: Altbau near U6\n"
                "Source: ImmoScout24 Germany\n\n"
                "Personal fit 92/100 · shortlist · Lift and transit fit.\n\n"
                "Open the property page with the titled button in this email.\n"
            ),
            "html": _property_match_html(
                title="Altbau near U6",
                provider="ImmoScout24 Germany",
                primary_link="https://propertyquarry.com/tours/altbau-u6",
                review_url="https://propertyquarry.com/app/research/altbau-u6?run_id=run-42",
                tour_url="https://propertyquarry.com/tours/altbau-u6",
                property_url="https://www.immobilienscout24.de/expose/altbau-u6",
                fit_summary="Personal fit 92/100 · shortlist · Lift and transit fit.",
                decision_summary=decision_summary,
            ),
        }
    if normalized == "tour_ready":
        action_urls = _property_decision_action_urls(
            packet_url="https://propertyquarry.com/app/research/family-flat-near-augarten?run_id=run-42",
            tour_url="https://propertyquarry.com/tours/family-flat-near-augarten",
            property_url="https://propertyquarry.com/source/property-1",
        )
        return {
            "template_key": normalized,
            "subject": "Apartment tour ready: Family flat near Augarten · layout first",
            "preheader": "PropertyQuarry prepared a 360 review for this property.",
            "text": (
                "Hello,\n\n"
                "PropertyQuarry prepared a 360 review page for Family flat near Augarten:\n\n"
                "Use the titled review and source-listing buttons in this email.\n\n"
                "Open the 360 review first, then continue into the property page if needed.\n"
            ),
            "html": _html_email_shell(
                title="360 review ready",
                body_html=(
                    '<div style="font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:#a37a2c;font-weight:700;">Review page</div>'
                    '<h1 style="margin:10px 0 12px 0;font-size:28px;line-height:1.2;color:#242321;">360 review ready: Family flat near Augarten</h1>'
                    '<p style="margin:0 0 16px 0;font-size:15px;line-height:1.65;color:#51493f;">PropertyQuarry prepared a 360 review for this property. Open the space first, then review the risks and missing facts.</p>'
                    + _email_button_row(
                        [
                            _email_button(href="https://propertyquarry.com/tours/family-flat-near-augarten", label="Open 360 review"),
                            _email_button(href="https://propertyquarry.com/app/research/family-flat-near-augarten?run_id=run-42", label="Open property", kind="secondary"),
                            _email_button(href=action_urls["yes"], label="Yes, shortlist", kind="secondary"),
                            _email_button(href=action_urls["no"], label="No — tell us why", kind="secondary"),
                        ]
                    )
                    + f'<p style="margin:0;">{_html_link(href=action_urls["ask_agent"], label="Ask agent about blockers or missing facts")}</p>'
                ),
            ),
        }
    if normalized == "investment_research_ready":
        action_urls = _property_decision_action_urls(
            packet_url="https://propertyquarry.com/app/research/altbau-u6?run_id=run-42&investment=1",
            property_url="https://propertyquarry.com/app/research/altbau-u6?run_id=run-42&investment=1",
        )
        return {
            "template_key": normalized,
            "subject": "Investment research ready: yield, risk, and missing facts",
            "preheader": "The underwriting packet is ready for review.",
            "text": (
                "Hello,\n\n"
                "PropertyQuarry prepared the investment property page.\n\n"
                "Quick take:\n"
                "- Recommendation: investigate further\n"
                "- Gross yield: 4.14%\n"
                "- Net yield: 2.8-3.2%\n"
                "- Missing documents: operating costs, energy certificate\n\n"
                "Open the investment property page with the titled button in this email.\n"
            ),
            "html": _html_email_shell(
                title="Investment research ready",
                body_html=(
                    '<div style="font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:#a37a2c;font-weight:700;">Investment page</div>'
                    '<h1 style="margin:10px 0 12px 0;font-size:28px;line-height:1.2;color:#242321;">Investment research ready</h1>'
                    '<p style="margin:0 0 16px 0;font-size:15px;line-height:1.65;color:#51493f;">PropertyQuarry prepared the underwriting read with yield, risk, and missing-document posture.</p>'
                    '<ul style="margin:0 0 16px;padding-left:20px;"><li>Gross yield: 4.14%</li><li>Net yield: 2.8-3.2%</li><li>Missing documents: operating costs, energy certificate</li></ul>'
                    + _email_button_row(
                        [
                            _email_button(href="https://propertyquarry.com/app/research/altbau-u6?run_id=run-42&investment=1", label="Open investment packet"),
                            _email_button(href=action_urls["ask_agent"], label="Ask for documents", kind="secondary"),
                            _email_button(href=action_urls["no"], label="Pass — too risky", kind="secondary"),
                        ]
                    )
                    + f'<p style="margin:0;">{_html_link(href=action_urls["investment_risk"], label="Open the biggest investment-risk explanation")}</p>'
                ),
            ),
        }
    if normalized == "workspace_invitation":
        invite_url = "https://propertyquarry.com/workspace-invites/invite-demo"
        return {
            "template_key": normalized,
            "subject": "Mara invited you to PropertyQuarry",
            "preheader": "Review the invite and continue through a secure PropertyQuarry account link.",
            "text": (
                "Hello,\n\n"
                "Mara invited you to join a PropertyQuarry account as Advisor.\n\n"
                "Use the titled invite button in this email to accept the invite.\n\n"
                "This link expires in about 60 minutes.\n"
            ),
            "html": _workspace_email_shell(
                eyebrow="Account invite",
                title="Mara invited you to PropertyQuarry",
                summary="Review the invite, confirm the role, and continue through a secure account link before it expires.",
                primary_label="Open invite",
                primary_href=invite_url,
                detail_rows=[("Role", "Advisor"), ("Invited by", "Mara"), ("Expires in", "about 60 minutes")],
                preheader="Review the invite and continue through a secure PropertyQuarry account link.",
            ),
        }
    if normalized == "workspace_access":
        access_url = "https://propertyquarry.com/workspace-access/token-demo"
        return {
            "template_key": normalized,
            "subject": "Your access link for PropertyQuarry account",
            "preheader": "Open your secure PropertyQuarry access link before it expires.",
            "text": (
                "Hello,\n\n"
                "Use the titled access button in this email to return to PropertyQuarry account.\n\n"
                "This link expires in about 60 minutes.\n"
            ),
            "html": _workspace_email_shell(
                eyebrow="Account access",
                title="Your access link for PropertyQuarry account",
                summary="Open the secure link below to return directly into PropertyQuarry without restarting the whole sign-in flow.",
                primary_label="Open access link",
                primary_href=access_url,
                detail_rows=[("Account", "PropertyQuarry account"), ("Role", "Principal"), ("Expires in", "about 60 minutes")],
                preheader="Open your secure PropertyQuarry access link before it expires.",
            ),
        }
    if normalized == "google_connect":
        connect_url = "https://propertyquarry.com/workspace-access/token-demo?return_to=%2Fapp%2Factions%2Fgoogle%2Fconnect"
        return {
            "template_key": normalized,
            "subject": "Connect Google to PropertyQuarry account",
            "preheader": "Return to PropertyQuarry and start the Google consent flow from a secure account link.",
            "text": (
                "Hello,\n\n"
                "Use the titled Google-connect button in this email to connect a Google inbox to PropertyQuarry account.\n\n"
                "This link expires in about 60 minutes.\n"
            ),
            "html": _workspace_email_shell(
                eyebrow="Google connection",
                title="Connect Google to PropertyQuarry account",
                summary="This secure link returns to PropertyQuarry first and then starts the Google consent flow for the requested bundle.",
                primary_label="Connect Google",
                primary_href=connect_url,
                detail_rows=[("Account", "PropertyQuarry account"), ("Bundle", "Google Full Workspace"), ("Connected inboxes", "0"), ("Expires in", "about 60 minutes")],
                preheader="Return to PropertyQuarry and start the Google consent flow from a secure account link.",
            ),
        }
    if normalized == "market_ready":
        return {
            "template_key": normalized,
            "subject": "PropertyQuarry market ready: Vienna",
            "preheader": "The market setup is complete and PropertyQuarry is ready for the next search.",
            "text": (
                "Hello,\n\n"
                "Initialization for Vienna is complete.\n\n"
                "Your market is now ready in PropertyQuarry. You can start the search now.\n"
            ),
            "html": _workspace_email_shell(
                eyebrow="Market ready",
                title="Vienna is ready in PropertyQuarry",
                summary="The market initialization finished and the search desk can now start the first real sweep.",
                primary_label="Open PropertyQuarry",
                primary_href="https://propertyquarry.com/app/properties",
                detail_rows=[("Market", "Vienna")],
                preheader="The market setup is complete and PropertyQuarry is ready for the next search.",
            ),
        }
    raise ValueError("unsupported_property_notification_preview")


def send_channel_digest_email(
    *,
    recipient_email: str,
    digest_key: str,
    headline: str,
    preview_text: str,
    delivery_url: str,
    plain_text: str,
    expires_at: str = "",
    idempotency_key: str = "",
) -> RegistrationEmailReceipt:
    minutes = _minutes_until(expires_at_iso=expires_at)
    label = str(headline or "PropertyQuarry update").strip() or "PropertyQuarry update"
    preview = str(preview_text or "").strip()
    digest_excerpt = _digest_preview_excerpt(plain_text)
    body = [
        label,
        "",
    ]
    if preview:
        body.extend([preview, ""])
    body.extend(
        [
            "Open this secure workspace view with the titled button in this email.",
            "",
            f"This link expires in about {minutes} minutes.",
        ]
    )
    if digest_excerpt:
        body.extend(["", "Digest preview", "", digest_excerpt])
    return _send_emailit_email(
        recipient_email=recipient_email,
        subject=label,
        text="\n".join(body).strip() + "\n",
        kind="ea_channel_digest_delivery",
        meta={"digest_key": str(digest_key or "").strip().lower(), "delivery_ref": _meta_ref(delivery_url)},
        idempotency_key=idempotency_key,
    )


def send_plaintext_digest_email(
    *,
    recipient_email: str,
    digest_key: str,
    headline: str,
    preview_text: str,
    plain_text: str,
    sender_email: str = "",
    sender_name: str = "",
) -> RegistrationEmailReceipt:
    label = str(headline or "PropertyQuarry update").strip() or "PropertyQuarry update"
    preview = str(preview_text or "").strip()
    body = [label, ""]
    if preview:
        body.extend([preview, ""])
    body.extend([_strip_plaintext_urls(plain_text).strip()])
    return _send_emailit_email(
        recipient_email=recipient_email,
        subject=label,
        text="\n".join(body).strip() + "\n",
        kind="ea_plaintext_digest_delivery",
        meta={"digest_key": str(digest_key or "").strip().lower()},
        sender_email=sender_email,
        sender_name=sender_name,
    )
