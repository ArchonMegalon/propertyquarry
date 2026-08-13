from __future__ import annotations

import inspect
import json
import hashlib
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse, urlunparse

from app.domain.models import ToolDefinition, ToolInvocationRequest, ToolInvocationResult, artifact_preview_text, now_utc_iso
from app.services.browseract_ui_service_catalog import (
    BrowserActUiServiceDefinition,
    browseract_ui_service_by_service_key,
    browseract_ui_service_by_tool,
)
from app.services.browseract_ui_template_catalog import browseract_ui_template_spec
from app.services.tool_execution_common import ToolExecutionError
from app.services.tool_execution_connector_dispatch_adapter import ConnectorDispatchToolAdapter
from app.services.browseract_binding_readiness import configured_browseract_service_names


def _extract_textish(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value).strip()
    if isinstance(value, list):
        return "\n".join(part for part in (_extract_textish(item) for item in value) if part).strip()
    if isinstance(value, dict):
        for key in ("text", "answer", "summary", "consensus", "recommendation", "message", "output", "result", "normalized_text"):
            text = _extract_textish(value.get(key))
            if text:
                return text
        try:
            return json.dumps(value, ensure_ascii=True)
        except Exception:
            return ""
    return ""


def _collect_text_fragments(value: object, *, limit: int = 64) -> tuple[str, ...]:
    collected: list[str] = []

    def _visit(node: object) -> None:
        if len(collected) >= limit:
            return
        if node is None:
            return
        if isinstance(node, (str, int, float, bool)):
            text = str(node).strip()
            if text:
                collected.append(text[:500])
            return
        if isinstance(node, dict):
            for key, nested in node.items():
                if len(collected) >= limit:
                    break
                key_text = str(key or "").strip()
                if key_text:
                    collected.append(key_text[:120])
                _visit(nested)
            return
        if isinstance(node, (list, tuple, set)):
            for nested in node:
                if len(collected) >= limit:
                    break
                _visit(nested)

    _visit(value)
    return tuple(collected)


def _has_marker(fragments: tuple[str, ...], markers: tuple[str, ...]) -> bool:
    lowered = tuple(fragment.lower() for fragment in fragments if fragment)
    return any(marker in fragment for fragment in lowered for marker in markers)


def _normalize_text_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, dict):
        values: list[str] = []
        for nested in value.values():
            values.extend(_normalize_text_list(nested))
        return values
    if isinstance(value, (list, tuple, set)):
        values: list[str] = []
        for nested in value:
            values.extend(_normalize_text_list(nested))
        return values
    text = str(value).strip()
    return [text] if text else []


def _chatplayground_roles(value: object) -> list[str]:
    roles = [entry.strip().lower() for entry in _normalize_text_list(value) if entry.strip()]
    if roles:
        return roles
    return ["factuality", "adversarial", "completeness", "risk"]


def _normalize_chatplayground_audit_payload(payload: dict[str, object] | None) -> tuple[str, str, list[str], list[str], list[str], list[str], dict[str, object]]:
    root = dict(payload or {})
    body = root.get("data") if isinstance(root.get("data"), dict) else root
    if not isinstance(body, dict):
        body = {}
    normalized = dict(body)
    consensus = str(
        normalized.get("consensus")
        or normalized.get("recommendation")
        or normalized.get("summary")
        or ""
    ).strip()
    recommendation = str(normalized.get("recommendation") or consensus or "").strip()
    disagreements = [entry for entry in _normalize_text_list(normalized.get("disagreements")) if entry]
    risks = [entry for entry in _normalize_text_list(normalized.get("risks")) if entry]
    model_deltas = [
        entry
        for entry in _normalize_text_list(normalized.get("model_deltas") or normalized.get("model_delta"))
        if entry
    ]
    instruction_trace = [entry for entry in _normalize_text_list(normalized.get("instruction_trace")) if entry]
    roles = _chatplayground_roles(normalized.get("roles"))
    return (
        consensus,
        recommendation,
        roles,
        disagreements,
        risks,
        model_deltas,
        {
            "consensus": consensus,
            "recommendation": recommendation,
            "disagreements": disagreements,
            "risks": risks,
            "model_deltas": model_deltas,
            "instruction_trace": instruction_trace,
            "roles": roles,
            "audit_scope": str(normalized.get("audit_scope") or "jury").strip() or "jury",
            "requested_models": _normalize_text_list(normalized.get("requested_models")),
            "requested_at": str(normalized.get("requested_at") or now_utc_iso()).strip() or now_utc_iso(),
            "raw_response": root,
            "parsed_at": now_utc_iso(),
        },
    )


def _strip_code_fences(text: object) -> str:
    raw = str(text or "").strip()
    if not raw.startswith("```"):
        return raw
    lines = raw.splitlines()
    if lines:
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _jsonish_candidates(text: object) -> tuple[str, ...]:
    raw = str(text or "").strip()
    if not raw:
        return ()
    candidates: list[str] = []

    def _add(candidate: object) -> None:
        normalized = str(candidate or "").strip()
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    _add(_strip_code_fences(raw))
    _add(raw)
    for match in re.findall(r"```(?:json)?\s*(.*?)```", raw, flags=re.IGNORECASE | re.DOTALL):
        _add(match)
        _add(_strip_code_fences(match))
    for opener, closer in (("{", "}"), ("[", "]")):
        start = raw.find(opener)
        end = raw.rfind(closer)
        if start >= 0 and end > start:
            _add(raw[start : end + 1])
    return tuple(candidates)


def _load_jsonish(text: object) -> object | None:
    for candidate in _jsonish_candidates(text):
        try:
            return json.loads(candidate)
        except Exception:
            continue
    return None


def _unwrap_browseract_output_payload(value: object) -> object | None:
    if value is None:
        return None
    if isinstance(value, dict):
        recognized = {
            "consensus",
            "recommendation",
            "summary",
            "disagreements",
            "risks",
            "model_deltas",
            "roles",
            "requested_models",
            "requested_at",
        }
        if recognized.intersection(value.keys()):
            return dict(value)
        for key in (
            "audit_response",
            "result",
            "output",
            "answer",
            "message",
            "content",
            "text",
            "string",
            "value",
            "generated_prompt",
        ):
            if key not in value:
                continue
            unwrapped = _unwrap_browseract_output_payload(value.get(key))
            if unwrapped is not None:
                return unwrapped
        if len(value) == 1:
            only_value = next(iter(value.values()))
            return _unwrap_browseract_output_payload(only_value)
        return dict(value)
    if isinstance(value, (list, tuple, set)):
        for nested in value:
            unwrapped = _unwrap_browseract_output_payload(nested)
            if unwrapped is not None:
                return unwrapped
        return None
    if isinstance(value, str):
        parsed = _load_jsonish(value)
        if parsed is not None and parsed != value:
            unwrapped = _unwrap_browseract_output_payload(parsed)
            if unwrapped is not None:
                return unwrapped
        text = value.strip()
        return text or None
    text = str(value).strip()
    return text or None


def _chatplayground_workflow_max_prompt_chars() -> int:
    raw = (
        str(os.getenv("BROWSERACT_CHATPLAYGROUND_WORKFLOW_MAX_PROMPT_CHARS") or "").strip()
        or str(os.getenv("EA_CHATPLAYGROUND_AUDIT_MAX_PROMPT_CHARS") or "").strip()
        or "16000"
    )
    try:
        return max(2000, min(120000, int(raw)))
    except Exception:
        return 16000


def _truncate_chatplayground_workflow_text(text: object, *, limit: int) -> str:
    value = str(text or "")
    if limit <= 0 or len(value) <= limit:
        return value
    if limit <= 96:
        return value[:limit]
    spacer = "\n\n[... omitted for ChatPlayground workflow transport ...]\n\n"
    remaining = limit - len(spacer)
    if remaining <= 32:
        return value[:limit]
    head = remaining // 2
    tail = remaining - head
    return f"{value[:head]}{spacer}{value[-tail:]}".strip()


def _render_chatplayground_workflow_prompt(
    *,
    prompt: str,
    roles: list[str],
    audit_scope: str,
    requested_models: list[str],
) -> str:
    role_list = [str(role).strip() for role in roles if str(role).strip()]
    if not role_list:
        role_list = ["factuality", "adversarial", "completeness", "risk"]
    model_list = [str(model).strip() for model in requested_models if str(model).strip()]
    scope_label = "review_light" if str(audit_scope or "").strip().lower() == "review_light" else "jury"
    material = str(prompt or "").strip()
    if not material:
        return ""
    limit = _chatplayground_workflow_max_prompt_chars()
    base_lines = [
        "You are the jury/audit reviewer for an external automation system.",
        f"Audit scope: {scope_label}",
        f"Review roles: {', '.join(role_list)}",
    ]
    if model_list:
        base_lines.append(f"Requested comparison models: {', '.join(model_list)}")
    base_lines.extend(
        [
            "Review the material and return exactly one JSON object with no markdown fences and no prose outside the JSON.",
            'Use this schema: {{"consensus":"pass|fail|needs_revision|unavailable","recommendation":"short verdict","disagreements":["..."],"risks":["..."],"model_deltas":["..."]}}',
            "Rules:",
            "- consensus must be one of pass, fail, needs_revision, or unavailable",
            "- recommendation must be a short actionable verdict",
            "- disagreements, risks, and model_deltas must be arrays of short strings",
            "- if the material is too incomplete to judge, use needs_revision or unavailable and explain why",
            "",
            "Material to review:",
            "<material>",
            "{material}",
            "</material>",
        ]
    )
    template = "\n".join(base_lines)
    wrapped = template.format(material=material)
    if len(wrapped) <= limit:
        return wrapped
    available = limit - len(template.format(material=""))
    if available <= 512:
        available = max(512, limit // 2)
    compact_material = _truncate_chatplayground_workflow_text(material, limit=available)
    rendered = template.format(material=compact_material)
    if len(rendered) <= limit:
        return rendered
    return _truncate_chatplayground_workflow_text(rendered, limit=limit)


class BrowserActToolAdapter:
    def __init__(self, *, connector_dispatch: ConnectorDispatchToolAdapter) -> None:
        self._connector_dispatch = connector_dispatch
        self._chatplayground_audit = None
        self._crezlo_property_tour = None
        self._gemini_web_generate = None
        self._onemin_billing_usage = None
        self._onemin_member_reconciliation = None
        self._ui_service_callbacks: dict[str, object] = {
            "mootion_movie": self._create_mootion_movie_direct,
            "browseract.mootion_movie": self._create_mootion_movie_direct,
            "avomap_flyover": self._create_avomap_flyover_direct,
            "browseract.avomap_flyover": self._create_avomap_flyover_direct,
            "booka_book": self._create_booka_book_direct,
            "browseract.booka_book": self._create_booka_book_direct,
        }

    @staticmethod
    def _looks_like_cloudflare_challenge(payload: dict[str, object]) -> bool:
        return _has_marker(
            _collect_text_fragments(payload),
            (
                "cloudflare",
                "just a moment",
                "checking your browser",
                "attention required",
                "verify you are human",
                "prove you are human",
                "human verification",
                "security check",
                "browser integrity check",
            ),
        )

    @staticmethod
    def _looks_like_turnstile(payload: dict[str, object]) -> bool:
        return _has_marker(
            _collect_text_fragments(payload),
            (
                "turnstile",
                "cf-turnstile",
                "challenge-platform",
                "cf_challenge",
            ),
        )

    @staticmethod
    def _looks_like_chatgpt_human_verification(payload: dict[str, object]) -> bool:
        fragments = _collect_text_fragments(payload)
        has_product = _has_marker(fragments, ("chatgpt", "openai"))
        has_challenge = _has_marker(
            fragments,
            (
                "verify you are human",
                "prove you are human",
                "human verification",
                "captcha",
            ),
        )
        return has_product and has_challenge

    @staticmethod
    def _looks_like_ui_session_expired(payload: dict[str, object]) -> bool:
        return _has_marker(
            _collect_text_fragments(payload),
            (
                "session expired",
                "please sign in",
                "sign in to continue",
                "log in to continue",
                "login required",
                "reauthenticate",
            ),
        )

    @staticmethod
    def _looks_like_ui_invalid_credentials(payload: dict[str, object]) -> bool:
        return _has_marker(
            _collect_text_fragments(payload),
            (
                "the email or password you entered is incorrect",
                "email or password you entered is incorrect",
                "incorrect email or password",
                "invalid email or password",
                "invalid credentials",
            ),
        )

    @classmethod
    def _raise_for_ui_lane_failure(cls, *, payload: dict[str, object], backend: str) -> None:
        explicit = str(
            payload.get("ui_failure_code")
            or payload.get("failure_code")
            or payload.get("error_code")
            or payload.get("challenge_state")
            or ""
        ).strip().lower()
        if explicit in {"challenge_required", "challenge_loop", "session_expired", "lane_unavailable", "timeout", "invalid_credentials"}:
            raise ToolExecutionError(f"ui_lane_failure:{backend}:{explicit}")
        if cls._looks_like_ui_invalid_credentials(payload):
            raise ToolExecutionError(f"ui_lane_failure:{backend}:invalid_credentials")
        if cls._looks_like_ui_session_expired(payload):
            raise ToolExecutionError(f"ui_lane_failure:{backend}:session_expired")
        if (
            cls._looks_like_turnstile(payload)
            or cls._looks_like_cloudflare_challenge(payload)
            or cls._looks_like_chatgpt_human_verification(payload)
        ):
            raise ToolExecutionError(f"ui_lane_failure:{backend}:challenge_required")

    @staticmethod
    def _normalize_lookup_key(value: object) -> str:
        return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")

    @classmethod
    def _browseract_scalar_map(cls, value: object, *, limit: int = 512) -> dict[str, str]:
        pairs: dict[str, str] = {}

        def _visit(node: object) -> None:
            if len(pairs) >= limit or node is None:
                return
            if isinstance(node, dict):
                for raw_key, nested in node.items():
                    if len(pairs) >= limit:
                        break
                    key = cls._normalize_lookup_key(raw_key)
                    if isinstance(nested, (str, int, float, bool)):
                        text = str(nested).strip()
                        if key and text and key not in pairs:
                            pairs[key] = text
                    _visit(nested)
                return
            if isinstance(node, (list, tuple, set)):
                for nested in node:
                    if len(pairs) >= limit:
                        break
                    _visit(nested)

        _visit(value)
        return pairs

    @classmethod
    def _browseract_text_candidates(cls, value: object, *, limit: int = 32) -> list[str]:
        candidates: list[str] = []

        def _add(text: object) -> None:
            normalized = str(text or "").strip()
            if normalized and normalized not in candidates:
                candidates.append(normalized)

        def _visit(node: object) -> None:
            if len(candidates) >= limit or node is None:
                return
            if isinstance(node, dict):
                for raw_key, nested in node.items():
                    if len(candidates) >= limit:
                        break
                    key = cls._normalize_lookup_key(raw_key)
                    if key in {
                        "raw_text",
                        "text",
                        "normalized_text",
                        "page_body",
                        "billing_usage_page",
                        "billing_usage_pre_bonus_page",
                        "billing_usage_bonus_page",
                        "billing_settings_page",
                        "usage_statistics_page",
                        "usage_records_page",
                        "invoice_page",
                        "unlock_free_credits_surface",
                        "daily_bonus_page",
                        "members_page",
                        "output_text",
                        "content",
                        "message",
                        "result",
                        "summary",
                    }:
                        _add(_extract_textish(nested))
                    _visit(nested)
                return
            if isinstance(node, (list, tuple, set)):
                for nested in node:
                    if len(candidates) >= limit:
                        break
                    _visit(nested)
                return
            if isinstance(node, str):
                _add(node)

        _visit(value)
        if not candidates:
            _add(_extract_textish(value))
        return candidates[:limit]

    @classmethod
    def _first_scalar_for_aliases(cls, scalar_map: dict[str, str], *aliases: str) -> str:
        for alias in aliases:
            value = scalar_map.get(cls._normalize_lookup_key(alias))
            if value:
                return value
        return ""

    @staticmethod
    def _browseract_normalization_payload(value: object) -> object:
        if isinstance(value, dict):
            task_output = BrowserActToolAdapter._browseract_task_output(value)
            if BrowserActToolAdapter._browseract_output_has_content(task_output):
                return task_output
        return value

    @staticmethod
    def _parse_number(value: object) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value or "").strip()
        if not text:
            return None
        match = re.search(r"-?\d[\d,]*(?:\.\d+)?", text.replace(" ", ""))
        if match is None:
            return None
        try:
            return float(match.group(0).replace(",", ""))
        except Exception:
            return None

    @staticmethod
    def _parse_credit_int(value: object) -> int | None:
        parsed = BrowserActToolAdapter._parse_number(value)
        if parsed is None:
            return None
        return max(0, int(round(parsed)))

    @staticmethod
    def _parse_percent(value: object) -> float | None:
        parsed = BrowserActToolAdapter._parse_number(value)
        if parsed is None:
            return None
        return max(0.0, min(100.0, round(float(parsed), 2)))

    @staticmethod
    def _parse_bool_text(value: object) -> bool | None:
        text = str(value or "").strip().lower()
        if not text:
            return None
        if any(marker in text for marker in ("rollover enabled", "rollover: yes", "lifetime credits roll over", "roll over")):
            return True
        if any(marker in text for marker in ("rollover disabled", "rollover: no", "no rollover")):
            return False
        if text in {"true", "yes", "enabled", "on"}:
            return True
        if text in {"false", "no", "disabled", "off"}:
            return False
        return None

    @staticmethod
    def _parse_datetime_text(value: object) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        normalized = text.replace("UTC", "+00:00").replace("Z", "+00:00")
        candidates = [normalized]
        if normalized.endswith("+00:00") and "T" not in normalized and " " in normalized:
            candidates.append(normalized.replace(" ", "T", 1))
        for candidate in candidates:
            try:
                parsed = datetime.fromisoformat(candidate)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            except Exception:
                continue
        for fmt in (
            "%Y-%m-%d",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%b %d, %Y",
            "%B %d, %Y",
            "%b %d, %Y %I:%M %p",
            "%B %d, %Y %I:%M %p",
            "%b %d %Y",
            "%B %d %Y",
        ):
            try:
                parsed = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
                return parsed.isoformat().replace("+00:00", "Z")
            except Exception:
                continue
        return text

    @classmethod
    def _find_label_value(cls, raw_text: str, labels: tuple[str, ...]) -> str:
        text = str(raw_text or "")
        if not text:
            return ""
        for label in labels:
            pattern = re.compile(
                rf"{re.escape(label)}\s*(?:[:\-]|is)?\s*([^\n\r|]+)",
                flags=re.IGNORECASE,
            )
            match = pattern.search(text)
            if match is not None:
                return str(match.group(1) or "").strip()
        return ""

    @classmethod
    def _visible_labels(cls, raw_text: str, labels: tuple[str, ...]) -> list[str]:
        text = str(raw_text or "")
        if not text:
            return []
        found: list[str] = []
        seen: set[str] = set()
        for label in labels:
            if re.search(rf"\b{re.escape(label)}\b", text, flags=re.IGNORECASE) is None:
                continue
            key = cls._normalize_lookup_key(label)
            if key in seen:
                continue
            seen.add(key)
            found.append(label)
        return found

    @classmethod
    def _extract_onemin_usage_rows(cls, value: object) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        seen: set[str] = set()

        def _visit(node: object) -> None:
            if isinstance(node, dict):
                lowered = {cls._normalize_lookup_key(key): nested for key, nested in node.items()}
                before_deduction = cls._parse_credit_int(
                    lowered.get("before_deduction")
                    or lowered.get("beforededuction")
                )
                after_deduction = cls._parse_credit_int(
                    lowered.get("after_deduction")
                    or lowered.get("afterdeduction")
                    or lowered.get("remaining_credits")
                )
                credit = cls._parse_credit_int(
                    lowered.get("credit")
                    or lowered.get("credits")
                    or lowered.get("deduction")
                )
                raw_date = str(lowered.get("date") or "").strip()
                raw_time = str(lowered.get("time") or "").strip()
                observed_at = cls._parse_datetime_text(
                    lowered.get("observed_at")
                    or lowered.get("created_at")
                    or lowered.get("createdat")
                    or lowered.get("datetime")
                    or lowered.get("date_time")
                    or " ".join(part for part in (raw_date, raw_time) if part)
                    or raw_date
                )
                if (
                    before_deduction is not None
                    or after_deduction is not None
                    or credit is not None
                ):
                    row = {
                        "user": str(
                            lowered.get("user")
                            or lowered.get("name")
                            or lowered.get("member_name")
                            or ""
                        ).strip(),
                        "before_deduction": before_deduction,
                        "after_deduction": after_deduction,
                        "credit": credit,
                        "date": raw_date,
                        "time": raw_time,
                        "observed_at": observed_at,
                    }
                    fingerprint = json.dumps(row, ensure_ascii=True, sort_keys=True)
                    if fingerprint not in seen:
                        seen.add(fingerprint)
                        rows.append(row)
                for nested in node.values():
                    _visit(nested)
                return
            if isinstance(node, (list, tuple, set)):
                for nested in node:
                    _visit(nested)
                return
            if isinstance(node, str):
                parsed = _load_jsonish(node)
                if parsed is not None and parsed != node:
                    _visit(parsed)

        _visit(value)
        return rows

    @classmethod
    def _latest_onemin_usage_remaining(cls, rows: list[dict[str, object]]) -> int | None:
        latest_remaining: int | None = None
        latest_epoch = float("-inf")
        fallback_remaining: int | None = None
        for index, row in enumerate(rows):
            remaining = cls._parse_credit_int(row.get("after_deduction"))
            if remaining is None:
                continue
            if fallback_remaining is None:
                fallback_remaining = remaining
            observed_at = cls._parse_datetime_text(row.get("observed_at"))
            if observed_at:
                try:
                    epoch = datetime.fromisoformat(observed_at.replace("Z", "+00:00")).timestamp()
                except Exception:
                    epoch = float("-inf")
                if epoch >= latest_epoch:
                    latest_epoch = epoch
                    latest_remaining = remaining
                continue
            if latest_remaining is None and index == 0:
                latest_remaining = remaining
        return latest_remaining if latest_remaining is not None else fallback_remaining

    @classmethod
    def _summarize_onemin_usage_rows(cls, rows: list[dict[str, object]]) -> dict[str, object]:
        usage_history_count = len(rows)
        latest_usage_at = None
        earliest_usage_at = None
        latest_usage_credit = None
        observed_usage_credits_total = None
        observed_usage_window_hours = None
        observed_usage_burn_credits_per_hour = None
        latest_epoch = float("-inf")
        earliest_epoch = float("inf")
        credit_total = 0
        credit_count = 0

        for index, row in enumerate(rows):
            credit = cls._parse_credit_int(row.get("credit"))
            if credit is not None:
                credit_total += credit
                credit_count += 1
            observed_at = cls._parse_datetime_text(row.get("observed_at"))
            if not observed_at:
                continue
            try:
                epoch = datetime.fromisoformat(observed_at.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            if epoch >= latest_epoch:
                latest_epoch = epoch
                latest_usage_at = observed_at
                latest_usage_credit = credit
            if epoch <= earliest_epoch:
                earliest_epoch = epoch
                earliest_usage_at = observed_at
            if latest_usage_at is None and index == 0:
                latest_usage_at = observed_at
                latest_usage_credit = credit

        if credit_count > 0:
            observed_usage_credits_total = credit_total
        raw_usage_window_hours = None
        if latest_epoch > earliest_epoch >= 0:
            raw_usage_window_hours = (latest_epoch - earliest_epoch) / 3600.0
            observed_usage_window_hours = round(raw_usage_window_hours, 4)
        if (
            observed_usage_credits_total is not None
            and raw_usage_window_hours not in (None, 0)
        ):
            observed_usage_burn_credits_per_hour = round(
                float(observed_usage_credits_total) / float(raw_usage_window_hours),
                2,
            )

        packet = {
            "usage_history_count": usage_history_count,
            "latest_usage_at": latest_usage_at,
            "earliest_usage_at": earliest_usage_at,
            "latest_usage_credit": latest_usage_credit,
            "observed_usage_credits_total": observed_usage_credits_total,
            "observed_usage_window_hours": observed_usage_window_hours,
            "observed_usage_burn_credits_per_hour": observed_usage_burn_credits_per_hour,
        }
        return packet

    @classmethod
    def _extract_onemin_billing_sections(cls, value: object) -> dict[str, list[dict[str, object]]]:
        sections: dict[str, list[dict[str, object]]] = {}

        def _visit(node: object) -> None:
            if isinstance(node, dict):
                lowered = {cls._normalize_lookup_key(key): item for key, item in node.items()}
                section_type = str(lowered.get("section_type") or lowered.get("section") or "").strip()
                if section_type:
                    sections.setdefault(cls._normalize_lookup_key(section_type), []).append(dict(node))
                for child in node.values():
                    _visit(child)
            elif isinstance(node, list):
                for item in node:
                    _visit(item)

        _visit(value)
        return sections

    @classmethod
    def _first_onemin_section_scalar(
        cls,
        sections: dict[str, list[dict[str, object]]],
        section_names: tuple[str, ...],
        *aliases: str,
    ) -> object:
        for section_name in section_names:
            rows = sections.get(cls._normalize_lookup_key(section_name), [])
            for row in rows:
                lowered = {cls._normalize_lookup_key(key): value for key, value in row.items()}
                for alias in aliases:
                    value = lowered.get(cls._normalize_lookup_key(alias))
                    if value not in (None, "", [], {}):
                        return value
        return ""

    @classmethod
    def _extract_onemin_bonus_rows(
        cls,
        sections: dict[str, list[dict[str, object]]],
    ) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for row in sections.get("billing_usage_bonus_page", []):
            bonus_type = str(row.get("bonus_type") or row.get("name") or row.get("title") or "").strip()
            description = str(
                row.get("description")
                or row.get("bonus_description")
                or row.get("details")
                or ""
            ).strip()
            bonus_credits = cls._parse_credit_int(
                row.get("bonus_credits") or row.get("credits") or row.get("reward_credits")
            )
            rows.append(
                {
                    "bonus_type": bonus_type or None,
                    "bonus_credits": bonus_credits,
                    "description": description or None,
                }
            )
        return rows

    @classmethod
    def _extract_onemin_json_rows_from_text(cls, raw_text: str) -> list[dict[str, object]]:
        text = str(raw_text or "").strip()
        if not text:
            return []
        candidates = [text]
        start = text.find("[")
        end = text.rfind("]")
        if 0 <= start < end:
            candidates.append(text[start : end + 1])
        rows: list[dict[str, object]] = []
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except Exception:
                continue
            if isinstance(parsed, dict):
                parsed = [parsed]
            if not isinstance(parsed, list):
                continue
            rows = [dict(item) for item in parsed if isinstance(item, dict)]
            if rows:
                return rows
        return []

    @classmethod
    def _infer_onemin_billing_section_name(cls, row: dict[str, object]) -> str:
        lowered = {cls._normalize_lookup_key(key): value for key, value in row.items()}
        if any(key in lowered for key in ("plan_name", "billing_plan", "billing_cycle", "available_credit", "current_credit", "credit_balance")):
            return "billing_settings_page"
        if any(key in lowered for key in ("bonus_type", "bonus_description", "requirement")):
            return "billing_usage_bonus_page"
        if any(key in lowered for key in ("before_deduction", "after_deduction", "credit", "record_type")):
            return "usage_records_page"
        return ""

    @classmethod
    def _normalize_onemin_billing_payload(
        cls,
        *,
        response: dict[str, object],
        source_url: str,
        account_label: str,
    ) -> dict[str, object]:
        normalized_payload = cls._browseract_normalization_payload(response)
        scalar_map = cls._browseract_scalar_map(normalized_payload)
        structured_response = dict(response.get("structured_output_json") or {}) if isinstance(response, dict) else {}
        extract_texts: dict[str, str] = {}
        raw_text_candidates = cls._browseract_text_candidates(normalized_payload)
        extracts_payload = structured_response.get("extracts")
        if isinstance(extracts_payload, dict):
            for raw_key, raw_value in extracts_payload.items():
                text_value = str(raw_value or "").strip()
                if not text_value:
                    continue
                extract_texts[cls._normalize_lookup_key(raw_key)] = text_value
                if text_value not in raw_text_candidates:
                    raw_text_candidates.append(text_value)
        raw_text = "\n\n".join(raw_text_candidates).strip()
        label_map = dict(scalar_map)
        section_rows = cls._extract_onemin_billing_sections(normalized_payload)
        billing_settings_text = extract_texts.get("billing_settings_page", "")
        usage_records_text = extract_texts.get("usage_records_page", "")
        billing_bonus_text = extract_texts.get("billing_usage_bonus_page", "")
        json_rows = cls._extract_onemin_json_rows_from_text(raw_text)
        for row in json_rows:
            inferred_section = cls._infer_onemin_billing_section_name(row)
            if inferred_section:
                section_rows.setdefault(inferred_section, []).append(row)
        usage_rows = cls._extract_onemin_usage_rows(normalized_payload)
        if not usage_rows and json_rows:
            usage_rows = cls._extract_onemin_usage_rows(json_rows)
        bonus_rows = cls._extract_onemin_bonus_rows(section_rows)
        visible_actions = cls._visible_labels(
            raw_text,
            (
                "Manage Subscription",
                "Top Up Credits",
                "Unlock Free Credits",
                "Claim Free Credits",
                "Claim Daily Bonus",
            ),
        )
        visible_tabs = cls._visible_labels(
            raw_text,
            (
                "Subscription",
                "Invoice",
                "Voucher",
                "Usage Statistics",
                "Usage Records",
            ),
        )
        for section_name, tab_label in (
            ("billing_settings_page", "Subscription"),
            ("usage_records_page", "Usage Records"),
            ("billing_usage_bonus_page", "Voucher"),
        ):
            if section_rows.get(section_name) and tab_label not in visible_tabs:
                visible_tabs.append(tab_label)
        if billing_settings_text and "Subscription" not in visible_tabs:
            visible_tabs.append("Subscription")
        if usage_records_text and "Usage Records" not in visible_tabs:
            visible_tabs.append("Usage Records")
        if billing_bonus_text and "Voucher" not in visible_tabs:
            visible_tabs.append("Voucher")
        for alias in (
            "manage_subscription_button_text",
            "top_up_credits_button_text",
            "unlock_free_credits_button_text",
            "claim_free_credits_button_text",
            "claim_daily_bonus_button_text",
        ):
            value = str(
                cls._first_onemin_section_scalar(section_rows, ("billing_settings_page",), alias) or ""
            ).strip()
            if value and value not in visible_actions:
                visible_actions.append(value)
        settings_remaining_credits = cls._parse_credit_int(
            cls._first_onemin_section_scalar(
                section_rows,
                ("billing_settings_page",),
                "current_credit",
                "credit_balance",
                "credit_balance_after_bonus",
                "remaining_credits",
                "current_balance",
            )
        )
        if settings_remaining_credits is None and billing_settings_text:
            settings_remaining_credits = cls._parse_credit_int(
                cls._find_label_value(
                    billing_settings_text,
                    ("Credit", "Current Credit", "Available Credit", "Remaining Credits"),
                )
            )

        remaining_credits = settings_remaining_credits or cls._parse_credit_int(
            cls._first_scalar_for_aliases(
                scalar_map,
                "remaining_credits",
                "free_credits",
                "credits_left",
                "available_credits",
                "credits_available",
            )
            or cls._find_label_value(
                raw_text,
                (
                    "Remaining credits",
                    "Credits left",
                    "Available credits",
                    "Credits available",
                ),
            )
        )
        if remaining_credits is None and billing_settings_text:
            remaining_credits = cls._parse_credit_int(
                cls._find_label_value(
                    billing_settings_text,
                    ("Credit", "Current Credit", "Available Credit", "Remaining Credits"),
                )
            )
        if remaining_credits is None:
            remaining_credits = cls._latest_onemin_usage_remaining(usage_rows)
        if remaining_credits is None:
            remaining_credits = cls._parse_credit_int(
                cls._first_scalar_for_aliases(scalar_map, "credit")
                or cls._find_label_value(raw_text, ("Credit",))
            )
        max_credits = cls._parse_credit_int(
            cls._first_scalar_for_aliases(
                scalar_map,
                "max_credits",
                "total_credits",
                "credits_total",
                "plan_credits",
                "monthly_credits",
                "included_credits",
            )
            or cls._find_label_value(
                raw_text,
                (
                    "Total credits",
                    "Max credits",
                    "Monthly credits",
                    "Included credits",
                    "Plan credits",
                ),
            )
        )
        used_percent = cls._parse_percent(
            cls._first_scalar_for_aliases(scalar_map, "used_percent", "usage_percent", "percent_used")
            or cls._find_label_value(raw_text, ("Used", "Usage", "Used percent", "Usage percent"))
        )
        next_topup_at = cls._parse_datetime_text(
            cls._first_scalar_for_aliases(
                scalar_map,
                "next_topup_at",
                "next_billing",
                "next_renewal",
                "renews_on",
                "renewal_date",
            )
            or cls._find_label_value(raw_text, ("Next top-up", "Next billing", "Next renewal", "Renews on"))
        )
        cycle_start_at = cls._parse_datetime_text(
            cls._first_scalar_for_aliases(scalar_map, "cycle_start_at", "period_start", "cycle_start")
            or cls._find_label_value(raw_text, ("Cycle start", "Period start"))
        )
        cycle_end_at = cls._parse_datetime_text(
            cls._first_scalar_for_aliases(scalar_map, "cycle_end_at", "period_end", "cycle_end")
            or cls._find_label_value(raw_text, ("Cycle end", "Period end"))
        )
        topup_amount = cls._parse_credit_int(
            cls._first_scalar_for_aliases(scalar_map, "topup_amount", "monthly_allocation", "included_credits")
            or cls._find_label_value(raw_text, ("Top-up amount", "Monthly allocation", "Included credits", "Monthly credits"))
        )
        rollover_enabled = cls._parse_bool_text(
            cls._first_scalar_for_aliases(scalar_map, "rollover_enabled", "rollover")
            or raw_text
        )
        plan_name = (
            str(
                cls._first_onemin_section_scalar(
                    section_rows,
                    ("billing_settings_page",),
                    "plan_name",
                    "billing_plan",
                    "plan",
                    "subscription_plan",
                )
                or ""
            ).strip()
            or
            cls._first_scalar_for_aliases(scalar_map, "plan_name", "plan", "subscription_plan")
            or (cls._find_label_value(billing_settings_text, ("Plan",)) if billing_settings_text else "")
            or cls._find_label_value(raw_text, ("Plan",))
        )
        billing_cycle = (
            str(
                cls._first_onemin_section_scalar(
                    section_rows,
                    ("billing_settings_page",),
                    "billing_cycle",
                    "cycle",
                    "subscription_cycle",
                )
                or ""
            ).strip()
            or
            cls._first_scalar_for_aliases(scalar_map, "billing_cycle", "cycle", "subscription_cycle")
            or (cls._find_label_value(billing_settings_text, ("Billing Cycle", "Cycle")) if billing_settings_text else "")
            or cls._find_label_value(raw_text, ("Billing Cycle", "Cycle"))
        )
        subscription_status = (
            str(
                cls._first_onemin_section_scalar(
                    section_rows,
                    ("billing_settings_page",),
                    "subscription_status",
                    "status",
                )
                or ""
            ).strip()
            or
            cls._first_scalar_for_aliases(scalar_map, "subscription_status", "status")
            or (cls._find_label_value(billing_settings_text, ("Subscription Status", "Status")) if billing_settings_text else "")
            or cls._find_label_value(raw_text, ("Subscription Status", "Status"))
        )
        daily_bonus_cta_text = str(
            cls._first_onemin_section_scalar(
                section_rows,
                ("billing_settings_page",),
                "unlock_free_credits_button_text",
                "claim_free_credits_button_text",
                "claim_daily_bonus_button_text",
            )
            or ""
        ).strip()
        for label in (
            "Unlock Free Credits",
            "Claim Free Credits",
            "Claim Daily Bonus",
            "Claim Bonus",
            "Daily Bonus",
            "Check In",
            "Check-In",
        ):
            if not daily_bonus_cta_text and re.search(rf"\b{re.escape(label)}\b", raw_text, flags=re.IGNORECASE):
                daily_bonus_cta_text = label
                break
        daily_visit_bonus_row = next(
            (
                row
                for row in bonus_rows
                if "daily_visit" in cls._normalize_lookup_key(row.get("bonus_type") or "")
                or "every_day" in cls._normalize_lookup_key(row.get("description") or "")
            ),
            None,
        )
        daily_bonus_available = None
        lowered_raw_text = raw_text.lower()
        if daily_bonus_cta_text or daily_visit_bonus_row is not None:
            daily_bonus_available = True
        elif any(
            marker in lowered_raw_text
            for marker in (
                "already claimed",
                "claimed today",
                "come back tomorrow",
                "next claim",
                "free credits unlocked",
            )
        ):
            daily_bonus_available = False
        daily_bonus_credits = (
            daily_visit_bonus_row.get("bonus_credits")
            if isinstance(daily_visit_bonus_row, dict)
            else None
        ) or cls._parse_credit_int(
            cls._first_scalar_for_aliases(
                scalar_map,
                "daily_bonus_credits",
                "bonus_credits",
                "free_credit_amount",
                "free_credits_amount",
                "claim_amount",
                "reward_credits",
            )
            or cls._find_label_value(
                raw_text,
                (
                    "Daily bonus",
                    "Bonus credits",
                    "Daily reward",
                    "Free credits reward",
                    "Claim amount",
                ),
            )
        )
        usage_summary = cls._summarize_onemin_usage_rows(usage_rows)
        latest_usage_at = usage_summary.get("latest_usage_at")
        earliest_usage_at = usage_summary.get("earliest_usage_at")
        latest_usage_credit = usage_summary.get("latest_usage_credit")
        observed_usage_credits_total = usage_summary.get("observed_usage_credits_total")
        observed_usage_window_hours = usage_summary.get("observed_usage_window_hours")
        observed_usage_burn_credits_per_hour = usage_summary.get("observed_usage_burn_credits_per_hour")
        basis = "actual_billing_usage_page" if remaining_credits is not None else "page_seen_but_unparsed"
        structured_output_json = {
            "raw_text": raw_text,
            "label_map": label_map,
            "visible_actions_json": visible_actions,
            "visible_tabs_json": visible_tabs,
            "billing_overview_json": {
                "plan_name": plan_name or None,
                "billing_cycle": billing_cycle or None,
                "subscription_status": subscription_status or None,
                "daily_bonus_cta_text": daily_bonus_cta_text or None,
                "daily_bonus_available": daily_bonus_available,
                "daily_bonus_credits": daily_bonus_credits,
            },
            "usage_summary_json": {
                "usage_history_count": usage_summary.get("usage_history_count"),
                "latest_usage_at": latest_usage_at,
                "earliest_usage_at": earliest_usage_at,
                "latest_usage_credit": latest_usage_credit,
                "observed_usage_credits_total": observed_usage_credits_total,
                "observed_usage_window_hours": observed_usage_window_hours,
                "observed_usage_burn_credits_per_hour": observed_usage_burn_credits_per_hour,
            },
        }
        if bonus_rows:
            structured_output_json["bonus_catalog_json"] = bonus_rows
        if usage_rows:
            structured_output_json["usage_history_json"] = usage_rows
        page_visible_credits = None
        billing_overview = structured_output_json.get("billing_overview_json")
        if isinstance(billing_overview, dict):
            page_visible_credits = cls._parse_credit_int(
                billing_overview.get("credits_balance")
                or billing_overview.get("current_credits")
                or billing_overview.get("available_credits")
                or billing_overview.get("available_credit")
            )
        raw_text = str(structured_output_json.get("raw_text") or "").strip()
        if page_visible_credits is None and raw_text:
            match = re.search(r'"(?:credits_balance|current_credits|available_credits)"\s*:\s*([0-9][0-9,]*)', raw_text)
            if match is not None:
                page_visible_credits = cls._parse_credit_int(match.group(1))
        if page_visible_credits is not None:
            if not isinstance(billing_overview, dict):
                billing_overview = {}
                structured_output_json["billing_overview_json"] = billing_overview
            billing_overview.setdefault("credits_balance", page_visible_credits)
            billing_overview.setdefault("current_credits", page_visible_credits)
            billing_overview.setdefault("available_credits", page_visible_credits)
            if remaining_credits is None or page_visible_credits > remaining_credits:
                remaining_credits = page_visible_credits
        return {
            "provider_backend": "onemin_billing_usage_page",
            "account_label": account_label,
            "remaining_credits": remaining_credits,
            "max_credits": max_credits,
            "used_percent": used_percent,
            "next_topup_at": next_topup_at,
            "cycle_start_at": cycle_start_at,
            "cycle_end_at": cycle_end_at,
            "topup_amount": topup_amount,
            "rollover_enabled": rollover_enabled,
            "plan_name": plan_name or None,
            "billing_cycle": billing_cycle or None,
            "subscription_status": subscription_status or None,
            "daily_bonus_cta_text": daily_bonus_cta_text or None,
            "daily_bonus_available": daily_bonus_available,
            "daily_bonus_credits": daily_bonus_credits,
            "usage_history_count": usage_summary.get("usage_history_count"),
            "latest_usage_at": latest_usage_at,
            "earliest_usage_at": earliest_usage_at,
            "latest_usage_credit": latest_usage_credit,
            "observed_usage_credits_total": observed_usage_credits_total,
            "observed_usage_window_hours": observed_usage_window_hours,
            "observed_usage_burn_credits_per_hour": observed_usage_burn_credits_per_hour,
            "source_url": source_url,
            "basis": basis,
            "structured_output_json": structured_output_json,
        }

    @classmethod
    def _extract_member_rows(cls, response: dict[str, object]) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []

        def _visit(node: object) -> None:
            if isinstance(node, dict):
                lowered = {cls._normalize_lookup_key(key): value for key, value in node.items()}
                if any(key in lowered for key in ("email", "member_email", "owner_email", "account_email")):
                    rows.append(
                        {
                            "name": str(
                                lowered.get("name")
                                or lowered.get("member_name")
                                or lowered.get("full_name")
                                or ""
                            ).strip(),
                            "email": str(
                                lowered.get("email")
                                or lowered.get("member_email")
                                or lowered.get("owner_email")
                                or lowered.get("account_email")
                                or ""
                            ).strip(),
                            "status": str(lowered.get("status") or lowered.get("member_status") or "").strip(),
                            "role": str(lowered.get("role") or lowered.get("member_role") or "").strip(),
                            "credit_limit": cls._parse_credit_int(
                                lowered.get("credit_limit")
                                or lowered.get("member_credit_limit")
                                or lowered.get("limit")
                            ),
                        }
                    )
                for nested in node.values():
                    _visit(nested)
            elif isinstance(node, (list, tuple, set)):
                for nested in node:
                    _visit(nested)

        _visit(response)
        if rows:
            unique: list[dict[str, object]] = []
            seen: set[str] = set()
            for row in rows:
                email = str(row.get("email") or "").strip().lower()
                if not email or email in seen:
                    continue
                seen.add(email)
                unique.append(row)
            return unique

        raw_text = "\n".join(cls._browseract_text_candidates(response)).strip()
        if not raw_text:
            return []
        email_re = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", flags=re.IGNORECASE)
        credit_re = re.compile(r"\b\d[\d,]{2,}\b")
        compact_lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        parsed_rows: list[dict[str, object]] = []
        seen_emails: set[str] = set()
        for index, line in enumerate(compact_lines):
            email_match = email_re.search(line)
            if email_match is None:
                continue
            email = email_match.group(0).strip()
            lowered_email = email.lower()
            if not lowered_email or lowered_email in seen_emails:
                continue
            seen_emails.add(lowered_email)
            window = compact_lines[index : min(len(compact_lines), index + 6)]
            window_joined = "\n".join(window).lower()
            name = line[: email_match.start()].strip(" -|:\t")
            if not name and index > 0:
                previous = compact_lines[index - 1].strip(" -|:\t")
                if previous and email_re.search(previous) is None:
                    name = previous
            status = ""
            for candidate in ("active", "deactivated", "inactive", "pending"):
                if re.search(rf"\b{re.escape(candidate)}\b", window_joined):
                    status = candidate
                    break
            role = ""
            for candidate in ("admin", "owner", "member", "viewer", "editor"):
                if re.search(rf"\b{re.escape(candidate)}\b", window_joined):
                    role = candidate
                    break
            credit_candidates: list[int] = []
            for candidate_line in window:
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate_line):
                    continue
                for match in credit_re.findall(candidate_line):
                    parsed_credit = cls._parse_credit_int(match)
                    if parsed_credit is not None:
                        credit_candidates.append(parsed_credit)
            parsed_rows.append(
                {
                    "name": name,
                    "email": email,
                    "status": status,
                    "role": role,
                    "credit_limit": max(credit_candidates) if credit_candidates else None,
                }
            )
        return parsed_rows

    @classmethod
    def _normalize_onemin_member_reconciliation_payload(
        cls,
        *,
        response: dict[str, object],
        source_url: str,
        account_label: str,
    ) -> dict[str, object]:
        from app.services import responses_upstream as upstream

        normalized_payload = cls._browseract_normalization_payload(response)
        members = cls._extract_member_rows(normalized_payload)
        owner_entries = list(upstream._onemin_owner_entries())
        owner_emails = {
            str(row.get("owner_email") or "").strip().lower()
            for row in owner_entries
            if str(row.get("owner_email") or "").strip()
        }
        if not owner_emails:
            raw_owner_payload = upstream._load_onemin_owner_ledger_payload()
            candidate_rows = []
            if isinstance(raw_owner_payload, dict):
                if isinstance(raw_owner_payload.get("slots"), list):
                    candidate_rows = raw_owner_payload.get("slots") or []
                elif isinstance(raw_owner_payload.get("owners"), list):
                    candidate_rows = raw_owner_payload.get("owners") or []
            elif isinstance(raw_owner_payload, list):
                candidate_rows = raw_owner_payload
            owner_emails = {
                str((row or {}).get("owner_email") or (row or {}).get("email") or "").strip().lower()
                for row in candidate_rows
                if isinstance(row, dict) and str((row or {}).get("owner_email") or (row or {}).get("email") or "").strip()
            }
        member_emails = {str(row.get("email") or "").strip().lower() for row in members if str(row.get("email") or "").strip()}
        missing_owner_emails = sorted(email for email in owner_emails if email not in member_emails)
        owner_mismatches = [
            row for row in members
            if str(row.get("email") or "").strip().lower() not in owner_emails
        ]
        basis = "actual_members_page" if members else "page_seen_but_unparsed"
        return {
            "provider_backend": "onemin_members_page",
            "account_label": account_label,
            "member_count": len(members),
            "matched_owner_slots": len(member_emails.intersection(owner_emails)),
            "missing_owner_emails": missing_owner_emails,
            "owner_mismatches": owner_mismatches,
            "members_json": members,
            "source_url": source_url,
            "basis": basis,
            "structured_output_json": {
                "raw_text": "\n\n".join(cls._browseract_text_candidates(normalized_payload)).strip(),
                "label_map": cls._browseract_scalar_map(normalized_payload),
            },
        }

    def execute_extract(self, request: ToolInvocationRequest, definition: ToolDefinition) -> ToolInvocationResult:
        payload = dict(request.payload_json or {})
        service_name = str(payload.get("service_name") or "").strip()
        principal_id, binding = self._resolve_browseract_binding(
            request=request,
            payload=payload,
            required_input_error="connector_binding_required:browseract.extract_account_facts",
            required_scopes=(service_name,) if service_name else None,
        )
        if not service_name:
            raise ToolExecutionError("service_name_required:browseract.extract_account_facts")
        record = self._extract_service_record(
            binding_auth_metadata_json=dict(binding.auth_metadata_json or {}),
            payload=payload,
            service_name=service_name,
            requested_fields=self._requested_fields(payload),
            allow_missing=False,
        )
        action_kind = str(request.action_kind or "account.extract") or "account.extract"
        structured_output_json = dict(record["structured_output_json"])
        structured_output_json.update(
            {"binding_id": binding.binding_id, "connector_name": binding.connector_name, "external_account_ref": binding.external_account_ref}
        )
        return ToolInvocationResult(
            tool_name=definition.tool_name,
            action_kind=action_kind,
            target_ref=f"browseract:{binding.binding_id}:{service_name.lower().replace(' ', '_')}",
            output_json={
                "binding_id": binding.binding_id,
                "connector_name": binding.connector_name,
                "external_account_ref": binding.external_account_ref,
                "service_name": record["service_name"],
                "facts_json": record["facts_json"],
                "requested_fields": record["requested_fields"],
                "missing_fields": record["missing_fields"],
                "account_email": record["account_email"],
                "plan_tier": record["plan_tier"],
                "discovery_status": record["discovery_status"],
                "verification_source": record["verification_source"],
                "last_verified_at": record["last_verified_at"],
                "instructions": record["instructions"],
                "account_hints_json": record["account_hints_json"],
                "requested_run_url": record["requested_run_url"],
                "requested_workflow_id": record["requested_workflow_id"],
                "browseract_task_id": record["browseract_task_id"],
                "live_discovery_error": record["live_discovery_error"],
                "normalized_text": record["normalized_text"],
                "preview_text": record["preview_text"],
                "mime_type": record["mime_type"],
                "structured_output_json": structured_output_json,
                "tool_name": definition.tool_name,
                "action_kind": action_kind,
            },
            receipt_json={
                "binding_id": binding.binding_id,
                "connector_name": binding.connector_name,
                "external_account_ref": binding.external_account_ref,
                "handler_key": definition.tool_name,
                "invocation_contract": "tool.v1",
                "principal_id": principal_id,
                "service_name": record["service_name"],
                "requested_fields": record["requested_fields"],
                "missing_fields": record["missing_fields"],
                "discovery_status": record["discovery_status"],
                "verification_source": record["verification_source"],
                "requested_run_url": record["requested_run_url"],
                "requested_workflow_id": record["requested_workflow_id"],
                "browseract_task_id": record["browseract_task_id"],
                "live_discovery_error": record["live_discovery_error"],
                "tool_version": definition.version,
            },
        )

    def execute_inventory(self, request: ToolInvocationRequest, definition: ToolDefinition) -> ToolInvocationResult:
        payload = dict(request.payload_json or {})
        service_names = self._requested_service_names(payload)
        principal_id, binding = self._resolve_browseract_binding(
            request=request,
            payload=payload,
            required_input_error="connector_binding_required:browseract.extract_account_inventory",
            required_scopes=service_names,
        )
        if not service_names:
            service_names = self._configured_service_names(
                binding_auth_metadata_json=dict(binding.auth_metadata_json or {}),
                binding_scope_json=dict(binding.scope_json or {}),
            )
        if not service_names:
            raise ToolExecutionError("service_names_required:browseract.extract_account_inventory")
        requested_fields = self._requested_fields(payload)
        services_json = [
            self._extract_service_record(
                binding_auth_metadata_json=dict(binding.auth_metadata_json or {}),
                payload=payload,
                service_name=service_name,
                requested_fields=requested_fields,
                allow_missing=True,
            )
            for service_name in service_names
        ]
        missing_services = [str(row["service_name"]) for row in services_json if str(row["discovery_status"]) == "missing"]
        action_kind = str(request.action_kind or "account.extract_inventory") or "account.extract_inventory"
        normalized_text = self._inventory_summary_text(services_json)
        structured_output_json = {
            "service_names": list(service_names),
            "services_json": services_json,
            "missing_services": missing_services,
            "binding_id": binding.binding_id,
            "connector_name": binding.connector_name,
            "external_account_ref": binding.external_account_ref,
            "instructions": str(payload.get("instructions") or "").strip(),
            "account_hints_json": dict(payload.get("account_hints_json") or {}),
            "requested_run_url": str(payload.get("run_url") or "").strip(),
        }
        return ToolInvocationResult(
            tool_name=definition.tool_name,
            action_kind=action_kind,
            target_ref=f"browseract:{binding.binding_id}:inventory",
            output_json={
                "binding_id": binding.binding_id,
                "connector_name": binding.connector_name,
                "external_account_ref": binding.external_account_ref,
                "service_names": list(service_names),
                "services_json": services_json,
                "missing_services": missing_services,
                "instructions": structured_output_json["instructions"],
                "account_hints_json": structured_output_json["account_hints_json"],
                "requested_run_url": structured_output_json["requested_run_url"],
                "normalized_text": normalized_text,
                "preview_text": artifact_preview_text(normalized_text),
                "mime_type": "text/plain",
                "structured_output_json": structured_output_json,
                "tool_name": definition.tool_name,
                "action_kind": action_kind,
            },
            receipt_json={
                "binding_id": binding.binding_id,
                "connector_name": binding.connector_name,
                "external_account_ref": binding.external_account_ref,
                "handler_key": definition.tool_name,
                "invocation_contract": "tool.v1",
                "principal_id": principal_id,
                "service_names": list(service_names),
                "missing_services": missing_services,
                "requested_run_url": structured_output_json["requested_run_url"],
                "tool_version": definition.version,
            },
        )

    def execute_onemin_billing_usage(self, request: ToolInvocationRequest, definition: ToolDefinition) -> ToolInvocationResult:
        from app.services import responses_upstream as upstream

        payload = dict(request.payload_json or {})
        principal_id, binding = self._resolve_browseract_binding(
            request=request,
            payload=payload,
            required_input_error="connector_binding_required:browseract.onemin_billing_usage",
            required_scopes=None,
        )
        binding_metadata = dict(binding.auth_metadata_json or {})
        run_url = str(
            payload.get("run_url")
            or binding_metadata.get("onemin_billing_usage_run_url")
            or binding_metadata.get("browseract_onemin_billing_usage_run_url")
            or binding_metadata.get("run_url")
            or ""
        ).strip()
        workflow_id = str(
            payload.get("workflow_id")
            or binding_metadata.get("onemin_billing_usage_workflow_id")
            or binding_metadata.get("browseract_onemin_billing_usage_workflow_id")
            or ""
        ).strip()
        page_url = str(payload.get("page_url") or "https://app.1min.ai/billing-usage").strip() or "https://app.1min.ai/billing-usage"
        account_label = str(payload.get("account_label") or binding.external_account_ref or binding.binding_id).strip() or binding.binding_id
        try:
            timeout_seconds = max(30, min(1800, int(payload.get("timeout_seconds") or 180)))
        except Exception:
            timeout_seconds = 180

        callback = getattr(self, "_onemin_billing_usage", None)
        if not run_url and not workflow_id:
            if bool(payload.get("force_browseract")):
                raise ToolExecutionError("run_url_or_workflow_id_required:browseract.onemin_billing_usage")
            login_email, login_password = self._onemin_login_credentials_for_account(
                account_label=account_label,
                binding_metadata=binding_metadata,
            )
            if not login_email:
                raise ToolExecutionError(f"owner_email_required:onemin:{account_label}")
            if not login_password:
                raise ToolExecutionError("onemin_password_missing")
            template_service = self._onemin_billing_usage_ui_service()
            local_payload = dict(payload)
            local_payload.update(
                {
                    "page_url": page_url,
                    "login_email": login_email,
                    "login_password": login_password,
                    "browseract_username": login_email,
                    "browseract_password": login_password,
                    "timeout_seconds": timeout_seconds,
                }
            )
            local_payload.update(
                self._browser_proxy_settings(
                    local_payload,
                    binding_metadata=binding_metadata,
                )
            )
            requested_inputs = self._build_browseract_ui_runtime_inputs(
                payload=local_payload,
                service=template_service,
            )
            requested_inputs.update(
                self._browseract_ui_service_runtime_credentials(
                    payload=local_payload,
                    binding_metadata=binding_metadata,
                    service=template_service,
                )
            )
            response = self._create_template_backed_ui_service_direct(
                run_url="",
                workflow_id="",
                request_payload=local_payload,
                requested_inputs=requested_inputs,
                binding_metadata=binding_metadata,
                service=template_service,
            )
            if not isinstance(response, dict):
                raise ToolExecutionError("browseract_template_execution_failed:onemin_billing_usage")
        elif callback is not None:
            maybe = callback(run_url=run_url, request_payload=dict(payload), page_url=page_url, account_label=account_label)
            if isinstance(maybe, dict):
                response = maybe
            elif workflow_id and not run_url:
                response = self._run_onemin_workflow_task(
                    workflow_id=workflow_id,
                    account_label=account_label,
                    timeout_seconds=timeout_seconds,
                    binding_metadata=binding_metadata,
                )
            else:
                response = self._post_browseract_json(
                    run_url=run_url,
                    request_payload={
                        "page_url": page_url,
                        "account_label": account_label,
                        "capture_raw_text": bool(payload.get("capture_raw_text", True)),
                        "principal_id": principal_id,
                        "binding_id": binding.binding_id,
                        "external_account_ref": binding.external_account_ref,
                    },
                    timeout_seconds=timeout_seconds,
                )
        else:
            if workflow_id and not run_url:
                response = self._run_onemin_workflow_task(
                    workflow_id=workflow_id,
                    account_label=account_label,
                    timeout_seconds=timeout_seconds,
                    binding_metadata=binding_metadata,
                )
            else:
                response = self._post_browseract_json(
                    run_url=run_url,
                    request_payload={
                        "page_url": page_url,
                        "account_label": account_label,
                        "capture_raw_text": bool(payload.get("capture_raw_text", True)),
                        "principal_id": principal_id,
                        "binding_id": binding.binding_id,
                        "external_account_ref": binding.external_account_ref,
                    },
                    timeout_seconds=timeout_seconds,
                )
        self._raise_for_ui_lane_failure(payload=response, backend="onemin_billing_usage")
        normalized = self._normalize_onemin_billing_payload(
            response=response,
            source_url=page_url,
            account_label=account_label,
        )
        snapshot = upstream.record_onemin_billing_snapshot(
            account_name=account_label,
            snapshot_json=normalized,
            source="browseract.onemin_billing_usage",
        )
        action_kind = str(request.action_kind or "billing.inspect") or "billing.inspect"
        normalized_text = json.dumps(normalized, ensure_ascii=True, separators=(",", ":"))
        structured_output_json = dict(normalized.get("structured_output_json") or {})
        structured_output_json["persisted_snapshot"] = snapshot
        return ToolInvocationResult(
            tool_name=definition.tool_name,
            action_kind=action_kind,
            target_ref=f"browseract:{binding.binding_id}:onemin_billing_usage:{account_label}",
            output_json={
                "binding_id": binding.binding_id,
                "connector_name": binding.connector_name,
                "external_account_ref": binding.external_account_ref,
                "account_label": account_label,
                "provider_backend": normalized.get("provider_backend"),
                "remaining_credits": normalized.get("remaining_credits"),
                "max_credits": normalized.get("max_credits"),
                "used_percent": normalized.get("used_percent"),
                "next_topup_at": normalized.get("next_topup_at"),
                "cycle_start_at": normalized.get("cycle_start_at"),
                "cycle_end_at": normalized.get("cycle_end_at"),
                "topup_amount": normalized.get("topup_amount"),
                "rollover_enabled": normalized.get("rollover_enabled"),
                "plan_name": normalized.get("plan_name"),
                "billing_cycle": normalized.get("billing_cycle"),
                "subscription_status": normalized.get("subscription_status"),
                "daily_bonus_cta_text": normalized.get("daily_bonus_cta_text"),
                "daily_bonus_available": normalized.get("daily_bonus_available"),
                "daily_bonus_credits": normalized.get("daily_bonus_credits"),
                "usage_history_count": normalized.get("usage_history_count"),
                "latest_usage_at": normalized.get("latest_usage_at"),
                "earliest_usage_at": normalized.get("earliest_usage_at"),
                "latest_usage_credit": normalized.get("latest_usage_credit"),
                "observed_usage_credits_total": normalized.get("observed_usage_credits_total"),
                "observed_usage_window_hours": normalized.get("observed_usage_window_hours"),
                "observed_usage_burn_credits_per_hour": normalized.get("observed_usage_burn_credits_per_hour"),
                "source_url": normalized.get("source_url"),
                "basis": normalized.get("basis"),
                "normalized_text": normalized_text,
                "preview_text": artifact_preview_text(normalized_text),
                "mime_type": "application/json",
                "structured_output_json": structured_output_json,
                "tool_name": definition.tool_name,
                "action_kind": action_kind,
            },
            receipt_json={
                "binding_id": binding.binding_id,
                "connector_name": binding.connector_name,
                "external_account_ref": binding.external_account_ref,
                "principal_id": principal_id,
                "handler_key": definition.tool_name,
                "invocation_contract": "tool.v1",
                "tool_version": definition.version,
                "tool_name": definition.tool_name,
                "action_kind": action_kind,
                "requested_url": run_url or f"browseract://workflow/{workflow_id}",
                "source_url": page_url,
                "account_label": account_label,
                "basis": normalized.get("basis"),
            },
        )

    def execute_onemin_member_reconciliation(
        self,
        request: ToolInvocationRequest,
        definition: ToolDefinition,
    ) -> ToolInvocationResult:
        from app.services import responses_upstream as upstream

        payload = dict(request.payload_json or {})
        principal_id, binding = self._resolve_browseract_binding(
            request=request,
            payload=payload,
            required_input_error="connector_binding_required:browseract.onemin_member_reconciliation",
            required_scopes=None,
        )
        binding_metadata = dict(binding.auth_metadata_json or {})
        run_url = str(
            payload.get("run_url")
            or binding_metadata.get("onemin_members_run_url")
            or binding_metadata.get("browseract_onemin_members_run_url")
            or binding_metadata.get("run_url")
            or ""
        ).strip()
        workflow_id = str(
            payload.get("workflow_id")
            or binding_metadata.get("onemin_members_workflow_id")
            or binding_metadata.get("browseract_onemin_members_workflow_id")
            or ""
        ).strip()
        page_url = str(payload.get("page_url") or "https://app.1min.ai/members").strip() or "https://app.1min.ai/members"
        account_label = str(payload.get("account_label") or binding.external_account_ref or binding.binding_id).strip() or binding.binding_id
        try:
            timeout_seconds = max(30, min(1800, int(payload.get("timeout_seconds") or 180)))
        except Exception:
            timeout_seconds = 180

        callback = getattr(self, "_onemin_member_reconciliation", None)
        if not run_url and not workflow_id:
            if bool(payload.get("force_browseract")):
                raise ToolExecutionError("run_url_or_workflow_id_required:browseract.onemin_member_reconciliation")
            login_email, login_password = self._onemin_login_credentials_for_account(
                account_label=account_label,
                binding_metadata=binding_metadata,
            )
            if not login_email:
                raise ToolExecutionError(f"owner_email_required:onemin:{account_label}")
            if not login_password:
                raise ToolExecutionError("onemin_password_missing")
            template_service = self._onemin_member_reconciliation_ui_service()
            local_payload = dict(payload)
            local_payload.update(
                {
                    "page_url": page_url,
                    "login_email": login_email,
                    "login_password": login_password,
                    "browseract_username": login_email,
                    "browseract_password": login_password,
                    "timeout_seconds": timeout_seconds,
                }
            )
            local_payload.update(
                self._browser_proxy_settings(
                    local_payload,
                    binding_metadata=binding_metadata,
                )
            )
            requested_inputs = self._build_browseract_ui_runtime_inputs(
                payload=local_payload,
                service=template_service,
            )
            requested_inputs.update(
                self._browseract_ui_service_runtime_credentials(
                    payload=local_payload,
                    binding_metadata=binding_metadata,
                    service=template_service,
                )
            )
            response = self._create_template_backed_ui_service_direct(
                run_url="",
                workflow_id="",
                request_payload=local_payload,
                requested_inputs=requested_inputs,
                binding_metadata=binding_metadata,
                service=template_service,
            )
            if not isinstance(response, dict):
                raise ToolExecutionError("browseract_template_execution_failed:onemin_member_reconciliation")
        elif callback is not None:
            maybe = callback(run_url=run_url, request_payload=dict(payload), page_url=page_url, account_label=account_label)
            if isinstance(maybe, dict):
                response = maybe
            elif workflow_id and not run_url:
                response = self._run_onemin_workflow_task(
                    workflow_id=workflow_id,
                    account_label=account_label,
                    timeout_seconds=timeout_seconds,
                    binding_metadata=binding_metadata,
                )
            else:
                response = self._post_browseract_json(
                    run_url=run_url,
                    request_payload={
                        "page_url": page_url,
                        "account_label": account_label,
                        "capture_raw_text": bool(payload.get("capture_raw_text", True)),
                        "principal_id": principal_id,
                        "binding_id": binding.binding_id,
                        "external_account_ref": binding.external_account_ref,
                    },
                    timeout_seconds=timeout_seconds,
                )
        else:
            if workflow_id and not run_url:
                response = self._run_onemin_workflow_task(
                    workflow_id=workflow_id,
                    account_label=account_label,
                    timeout_seconds=timeout_seconds,
                    binding_metadata=binding_metadata,
                )
            else:
                response = self._post_browseract_json(
                    run_url=run_url,
                    request_payload={
                        "page_url": page_url,
                        "account_label": account_label,
                        "capture_raw_text": bool(payload.get("capture_raw_text", True)),
                        "principal_id": principal_id,
                        "binding_id": binding.binding_id,
                        "external_account_ref": binding.external_account_ref,
                    },
                    timeout_seconds=timeout_seconds,
                )
        self._raise_for_ui_lane_failure(payload=response, backend="onemin_members")
        normalized = self._normalize_onemin_member_reconciliation_payload(
            response=response,
            source_url=page_url,
            account_label=account_label,
        )
        snapshot = upstream.record_onemin_member_reconciliation_snapshot(
            account_name=account_label,
            snapshot_json=normalized,
            source="browseract.onemin_member_reconciliation",
        )
        action_kind = str(request.action_kind or "billing.reconcile_members") or "billing.reconcile_members"
        normalized_text = json.dumps(normalized, ensure_ascii=True, separators=(",", ":"))
        structured_output_json = dict(normalized.get("structured_output_json") or {})
        structured_output_json["persisted_snapshot"] = snapshot
        return ToolInvocationResult(
            tool_name=definition.tool_name,
            action_kind=action_kind,
            target_ref=f"browseract:{binding.binding_id}:onemin_member_reconciliation:{account_label}",
            output_json={
                "binding_id": binding.binding_id,
                "connector_name": binding.connector_name,
                "external_account_ref": binding.external_account_ref,
                "account_label": account_label,
                "provider_backend": normalized.get("provider_backend"),
                "member_count": normalized.get("member_count"),
                "matched_owner_slots": normalized.get("matched_owner_slots"),
                "missing_owner_emails": list(normalized.get("missing_owner_emails") or []),
                "owner_mismatches": list(normalized.get("owner_mismatches") or []),
                "members_json": list(normalized.get("members_json") or []),
                "source_url": normalized.get("source_url"),
                "basis": normalized.get("basis"),
                "normalized_text": normalized_text,
                "preview_text": artifact_preview_text(normalized_text),
                "mime_type": "application/json",
                "structured_output_json": structured_output_json,
                "tool_name": definition.tool_name,
                "action_kind": action_kind,
            },
            receipt_json={
                "binding_id": binding.binding_id,
                "connector_name": binding.connector_name,
                "external_account_ref": binding.external_account_ref,
                "principal_id": principal_id,
                "handler_key": definition.tool_name,
                "invocation_contract": "tool.v1",
                "tool_version": definition.version,
                "tool_name": definition.tool_name,
                "action_kind": action_kind,
                "requested_url": run_url or f"browseract://workflow/{workflow_id}",
                "source_url": page_url,
                "account_label": account_label,
                "basis": normalized.get("basis"),
            },
        )

    @staticmethod
    def _browseract_safe_input_value(value: object) -> object:
        if isinstance(value, (dict, list, tuple, set)):
            try:
                return json.dumps(value, ensure_ascii=True)
            except Exception:
                return _extract_textish(value)
        return value

    @staticmethod
    def _browseract_redacted_runtime_inputs(values: dict[str, object]) -> dict[str, object]:
        safe: dict[str, object] = {}
        for key, value in values.items():
            normalized = str(key or "").strip()
            lowered = normalized.lower()
            if not normalized or any(marker in lowered for marker in ("password", "secret", "token", "cookie", "proxy")):
                continue
            if lowered in {
                "login_email",
                "crezlo_login_email",
                "browseract_username",
                "username",
                "email",
            }:
                continue
            safe[normalized] = value
        return safe

    @classmethod
    def _crezlo_redacted_runtime_inputs(cls, values: dict[str, object]) -> dict[str, object]:
        return cls._browseract_redacted_runtime_inputs(values)

    @staticmethod
    def _browseract_service_result_title(
        *,
        payload: dict[str, object],
        service: BrowserActUiServiceDefinition,
    ) -> str:
        return (
            str(payload.get("result_title") or payload.get("title") or payload.get(service.output_label) or "").strip()
            or service.name
        )

    @classmethod
    def _resolve_browseract_ui_service_target(
        cls,
        *,
        payload: dict[str, object],
        binding_metadata: dict[str, object],
        service: BrowserActUiServiceDefinition,
    ) -> tuple[str, str]:
        run_url = str(payload.get("run_url") or "").strip()
        workflow_id = str(payload.get("workflow_id") or "").strip()
        if not run_url:
            for key in (*service.binding_run_url_keys, "run_url"):
                value = str(binding_metadata.get(key) or "").strip()
                if value:
                    run_url = value
                    break
        if not workflow_id:
            for key in (*service.binding_workflow_id_keys, "workflow_id"):
                value = str(binding_metadata.get(key) or "").strip()
                if value:
                    workflow_id = value
                    break
        return run_url, workflow_id

    @staticmethod
    def _binding_service_account_email(
        *,
        binding_metadata: dict[str, object],
        service: BrowserActUiServiceDefinition,
    ) -> str:
        service_accounts = binding_metadata.get("service_accounts_json")
        if not isinstance(service_accounts, dict):
            return ""
        for service_name in service.browseract_service_names:
            account = service_accounts.get(service_name)
            if not isinstance(account, dict):
                continue
            email = str(
                account.get("account_email")
                or account.get("email")
                or account.get("login_email")
                or ""
            ).strip()
            if email:
                return email
        return ""

    @classmethod
    def _browseract_ui_service_runtime_credentials(
        cls,
        *,
        payload: dict[str, object],
        binding_metadata: dict[str, object],
        service: BrowserActUiServiceDefinition,
    ) -> dict[str, object]:
        login_email = str(
            payload.get("login_email")
            or payload.get("browseract_username")
            or binding_metadata.get("login_email")
            or binding_metadata.get("browseract_username")
            or binding_metadata.get("username")
            or cls._binding_service_account_email(binding_metadata=binding_metadata, service=service)
            or os.getenv("EA_UI_SERVICE_LOGIN_EMAIL")
        ).strip()
        login_password = str(
            payload.get("login_password")
            or payload.get("browseract_password")
            or binding_metadata.get("login_password")
            or binding_metadata.get("browseract_password")
            or binding_metadata.get("password")
            or os.getenv("EA_UI_SERVICE_LOGIN_PASSWORD")
        ).strip()
        credentials: dict[str, object] = {}
        if login_email:
            credentials["browseract_username"] = login_email
        if login_password:
            credentials["browseract_password"] = login_password
        return credentials

    @classmethod
    def _browseract_ui_direct_result_needs_remote_retry(
        cls,
        *,
        result: dict[str, object],
    ) -> bool:
        render_status = str(result.get("render_status") or "").strip().lower()
        if render_status in {"auth_handoff_required", "challenge_required", "session_expired"}:
            return True
        fragments = _collect_text_fragments(result)
        login_url = str(
            result.get("editor_url")
            or result.get("asset_url")
            or ((result.get("structured_output_json") or {}).get("url") if isinstance(result.get("structured_output_json"), dict) else "")
            or ""
        ).strip().lower()
        joined = "\n".join(fragment.lower() for fragment in fragments if fragment).strip()
        if "accounts.google.com" in login_url:
            return True
        loginish = (
            "/login" in login_url
            or "/signin" in login_url
            or "/sign-in" in login_url
            or "auth" in login_url
            or "accounts.google.com" in joined
        )
        if not loginish:
            return False
        return _has_marker(
            fragments,
            (
                "continue with google",
                "login with google",
                "sign in with google",
                "sign in",
                "log in",
                "login",
                "sign into",
                "welcome back",
            ),
        )

    @classmethod
    def _build_browseract_ui_runtime_inputs(
        cls,
        *,
        payload: dict[str, object],
        service: BrowserActUiServiceDefinition,
    ) -> dict[str, object]:
        runtime_inputs = payload.get("runtime_inputs_json")
        resolved: dict[str, object] = dict(runtime_inputs) if isinstance(runtime_inputs, dict) else {}
        for payload_key, runtime_key in service.payload_to_runtime_inputs.items():
            value = payload.get(payload_key)
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            resolved[runtime_key] = cls._browseract_safe_input_value(value)
        if "title" not in resolved:
            title = str(payload.get("title") or payload.get("result_title") or "").strip()
            if title:
                resolved["title"] = title
        for required_input in service.required_runtime_inputs:
            value = resolved.get(required_input)
            if value is None or (isinstance(value, str) and not value.strip()):
                raise ToolExecutionError(f"runtime_input_required:{service.tool_name}:{required_input}")
        return resolved

    @staticmethod
    def _maybe_url_text(value: object) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if text.lower().startswith(("http://", "https://")):
            return text
        return ""

    @classmethod
    def _browseract_url_entries(cls, value: object, *, limit: int = 128) -> list[tuple[str, str]]:
        entries: list[tuple[str, str]] = []
        seen: set[str] = set()

        def _add(label: object, url: object) -> None:
            normalized_url = cls._maybe_url_text(url)
            if not normalized_url or normalized_url in seen or len(entries) >= limit:
                return
            seen.add(normalized_url)
            entries.append((cls._normalize_lookup_key(label), normalized_url))

        def _visit(node: object, key_hint: str = "") -> None:
            if len(entries) >= limit or node is None:
                return
            if isinstance(node, dict):
                for raw_key, nested in node.items():
                    if len(entries) >= limit:
                        break
                    normalized_key = cls._normalize_lookup_key(raw_key)
                    if isinstance(nested, (str, int, float, bool)):
                        _add(normalized_key, nested)
                    _visit(nested, normalized_key or key_hint)
                return
            if isinstance(node, (list, tuple, set)):
                for nested in node:
                    if len(entries) >= limit:
                        break
                    _visit(nested, key_hint)
                return
            if isinstance(node, str):
                for match in re.findall(r"https?://[^\s<>'\"\\)]+", node):
                    _add(key_hint, match)

        _visit(value)
        return entries

    @classmethod
    def _first_browseract_url_for_markers(
        cls,
        entries: list[tuple[str, str]],
        *,
        key_markers: tuple[str, ...],
        suffixes: tuple[str, ...] = (),
    ) -> str:
        for key, url in entries:
            if key and any(marker in key for marker in key_markers):
                return url
        if suffixes:
            for _key, url in entries:
                lowered = urlparse(url).path.lower()
                if any(lowered.endswith(suffix) for suffix in suffixes):
                    return url
        return ""

    @classmethod
    def _normalize_browseract_ui_service_payload(
        cls,
        *,
        service: BrowserActUiServiceDefinition,
        response: dict[str, object],
        workflow_id: str,
        requested_url: str,
        requested_inputs: dict[str, object],
        result_title: str,
    ) -> dict[str, object]:
        normalized_payload = cls._browseract_normalization_payload(response)
        scalar_map = cls._browseract_scalar_map(normalized_payload)
        text_candidates = cls._browseract_text_candidates(normalized_payload)
        raw_text = "\n\n".join(text_candidates).strip()
        url_entries = cls._browseract_url_entries(normalized_payload)
        asset_urls = [url for _label, url in url_entries if url != requested_url]
        public_url = cls._first_scalar_for_aliases(
            scalar_map,
            "public_url",
            "public_link",
            "share_url",
            "share_link",
            "preview_url",
            "viewer_url",
            "live_url",
            "hosted_url",
        ) or cls._first_browseract_url_for_markers(
            url_entries,
            key_markers=("public", "share", "preview", "viewer", "view", "live", "hosted"),
        )
        editor_url = cls._first_scalar_for_aliases(
            scalar_map,
            "editor_url",
            "edit_url",
            "admin_url",
            "dashboard_url",
            "builder_url",
            "studio_url",
        ) or cls._first_browseract_url_for_markers(
            url_entries,
            key_markers=("editor", "admin", "dashboard", "builder", "studio"),
        )
        asset_url = cls._first_scalar_for_aliases(
            scalar_map,
            "asset_url",
            "result_url",
            "video_url",
            "movie_url",
            "flyover_url",
            "book_url",
            "pdf_url",
            "file_url",
        ) or cls._first_browseract_url_for_markers(
            url_entries,
            key_markers=("asset", "result", "video", "movie", "flyover", "book", "pdf", "file"),
            suffixes=(".mp4", ".mov", ".webm", ".m4v", ".pdf", ".epub", ".jpg", ".jpeg", ".png"),
        )
        download_url = cls._first_scalar_for_aliases(
            scalar_map,
            "download_url",
            "export_url",
        ) or cls._first_browseract_url_for_markers(
            url_entries,
            key_markers=("download", "export"),
            suffixes=(".mp4", ".mov", ".webm", ".m4v", ".pdf", ".epub"),
        )
        asset_url = asset_url or download_url or (asset_urls[0] if asset_urls else "")
        task_id = ""
        try:
            task_id = cls._browseract_task_id(response)
        except Exception:
            task_id = ""
        render_status = (
            cls._first_scalar_for_aliases(
                scalar_map,
                "render_status",
                "result_status",
                "generation_status",
                "status",
                "task_status",
                "state",
            )
            or cls._browseract_task_status(response)
            or ("completed" if cls._browseract_output_has_content(normalized_payload) else "unknown")
        )
        if asset_url and asset_url not in asset_urls:
            asset_urls.insert(0, asset_url)
        structured_output_json = {
            "service_key": service.service_key,
            "capability_key": service.capability_key,
            "tool_name": service.tool_name,
            "result_title": result_title or service.name,
            "render_status": render_status or "unknown",
            "asset_url": asset_url or None,
            "download_url": download_url or None,
            "public_url": public_url or None,
            "editor_url": editor_url or None,
            "asset_urls": asset_urls,
            "workflow_id": workflow_id or None,
            "task_id": task_id or None,
            "requested_url": requested_url,
            "requested_inputs": cls._browseract_redacted_runtime_inputs(requested_inputs),
            "raw_text": raw_text,
            "label_map": scalar_map,
            "url_entries": [{"label": label, "url": url} for label, url in url_entries],
            "workflow_output_json": normalized_payload if isinstance(normalized_payload, dict) else {"value": normalized_payload},
        }
        normalized_text = "\n".join(
            line
            for line in (
                f"Service: {service.name}",
                f"Result title: {result_title}" if result_title else "",
                f"Render status: {render_status}" if render_status else "",
                f"Asset URL: {asset_url}" if asset_url else "",
                f"Download URL: {download_url}" if download_url and download_url != asset_url else "",
                f"Public URL: {public_url}" if public_url else "",
                f"Editor URL: {editor_url}" if editor_url else "",
                f"Requested URL: {requested_url}" if requested_url else "",
                f"Task ID: {task_id}" if task_id else "",
            )
            if line
        )
        return {
            "service_key": service.service_key,
            "result_title": result_title or service.name,
            "render_status": render_status or "unknown",
            "asset_url": asset_url or None,
            "download_url": download_url or None,
            "public_url": public_url or None,
            "editor_url": editor_url or None,
            "asset_urls": asset_urls,
            "workflow_id": workflow_id or None,
            "task_id": task_id or None,
            "requested_url": requested_url,
            "normalized_text": normalized_text or (raw_text[:500] if raw_text else ""),
            "preview_text": artifact_preview_text(normalized_text or raw_text),
            "mime_type": "application/json",
            "structured_output_json": structured_output_json,
        }

    @classmethod
    def _crezlo_find_matching_scalar(cls, value: object, *, markers: tuple[str, ...]) -> str:
        if value is None:
            return ""
        if isinstance(value, dict):
            for key, nested in value.items():
                normalized_key = cls._normalize_lookup_key(key)
                if any(marker in normalized_key for marker in markers):
                    text = _extract_textish(nested)
                    if text:
                        return text
            for nested in value.values():
                text = cls._crezlo_find_matching_scalar(nested, markers=markers)
                if text:
                    return text
            return ""
        if isinstance(value, (list, tuple, set)):
            for nested in value:
                text = cls._crezlo_find_matching_scalar(nested, markers=markers)
                if text:
                    return text
            return ""
        return ""

    @staticmethod
    def _crezlo_maybe_url(value: object) -> str:
        text = str(value or "").strip()
        lowered = text.lower()
        if lowered.startswith(("http://", "https://", "browseract://")):
            return text
        return ""

    @staticmethod
    def _crezlo_api_base() -> str:
        return str(os.getenv("EA_CREZLO_API_BASE") or "https://api.caliqik.com/api/seller").strip().rstrip("/")

    @staticmethod
    def _crezlo_default_workspace_id() -> str:
        return (
            str(os.getenv("EA_CREZLO_WORKSPACE_ID") or "").strip()
            or str(os.getenv("CREZLO_WORKSPACE_ID") or "").strip()
            # Current authenticated EA Property Tours workspace.  Explicit
            # binding metadata and env configuration remain authoritative.
            or "019eafdf-15ae-73f7-b6d0-1cc4559fc465"
        )

    @staticmethod
    def _crezlo_default_workspace_domain() -> str:
        return (
            str(os.getenv("EA_CREZLO_WORKSPACE_DOMAIN") or "").strip()
            or str(os.getenv("CREZLO_WORKSPACE_DOMAIN") or "").strip()
            or "ea-property-tours-20260320.crezlotours.com"
        )

    @classmethod
    def _resolve_crezlo_workspace(
        cls,
        *,
        payload: dict[str, object],
        binding_metadata: dict[str, object],
    ) -> dict[str, str]:
        workspace_id = str(
            payload.get("workspace_id")
            or binding_metadata.get("crezlo_workspace_id")
            or binding_metadata.get("browseract_crezlo_workspace_id")
            or cls._crezlo_default_workspace_id()
        ).strip()
        workspace_domain = str(
            payload.get("workspace_domain")
            or binding_metadata.get("crezlo_workspace_domain")
            or binding_metadata.get("browseract_crezlo_workspace_domain")
            or cls._crezlo_default_workspace_domain()
        ).strip()
        workspace_base_url = str(
            payload.get("workspace_base_url")
            or binding_metadata.get("crezlo_workspace_base_url")
            or binding_metadata.get("browseract_crezlo_workspace_base_url")
            or (f"https://{workspace_domain}" if workspace_domain else "")
        ).strip()
        workspace_tours_url = str(
            payload.get("workspace_tours_url")
            or binding_metadata.get("crezlo_workspace_tours_url")
            or binding_metadata.get("browseract_crezlo_workspace_tours_url")
            or (f"{workspace_base_url.rstrip('/')}/admin/tours" if workspace_base_url else "")
        ).strip()
        return {
            "workspace_id": workspace_id,
            "workspace_domain": workspace_domain,
            "workspace_base_url": workspace_base_url,
            "workspace_tours_url": workspace_tours_url,
        }

    @staticmethod
    def _crezlo_workspace_label(value: object) -> str:
        domain = str(value or "").strip().split(".", 1)[0]
        if not domain:
            return ""
        normalized = re.sub(r"-\d{6,}$", "", domain)
        parts = [part for part in normalized.split("-") if part]
        if not parts:
            return ""
        return " ".join(part.upper() if len(part) <= 2 else (part[:1].upper() + part[1:]) for part in parts)

    @staticmethod
    def _crezlo_login_email(payload: dict[str, object], *, binding_metadata: dict[str, object]) -> str:
        return str(
            payload.get("login_email")
            or payload.get("crezlo_login_email")
            or binding_metadata.get("crezlo_login_email")
            or binding_metadata.get("login_email")
            or os.getenv("EA_CREZLO_LOGIN_EMAIL")
            or ""
        ).strip()

    @staticmethod
    def _crezlo_login_password(payload: dict[str, object], *, binding_metadata: dict[str, object]) -> str:
        return str(
            payload.get("login_password")
            or payload.get("crezlo_login_password")
            or binding_metadata.get("crezlo_login_password")
            or binding_metadata.get("login_password")
            or os.getenv("EA_CREZLO_LOGIN_PASSWORD")
            or ""
        ).strip()

    @classmethod
    def _build_crezlo_property_tour_worker_packet(
        cls,
        *,
        payload: dict[str, object],
        binding_metadata: dict[str, object],
        requested_inputs: dict[str, object],
        workspace: dict[str, str],
        timeout_seconds: int,
    ) -> dict[str, object]:
        workspace_label_candidates: list[str] = []
        for candidate in (
            payload.get("workspace_name"),
            payload.get("workspace_label"),
            binding_metadata.get("crezlo_workspace_name"),
            binding_metadata.get("browseract_crezlo_workspace_name"),
            cls._crezlo_workspace_label(workspace.get("workspace_domain")),
        ):
            text = str(candidate or "").strip()
            if text and text not in workspace_label_candidates:
                workspace_label_candidates.append(text)
        packet = {
            "login_email": cls._crezlo_login_email(payload, binding_metadata=binding_metadata),
            "login_password": cls._crezlo_login_password(payload, binding_metadata=binding_metadata),
            "tour_title": str(requested_inputs.get("tour_title") or payload.get("tour_title") or "").strip(),
            "workspace_id": str(workspace.get("workspace_id") or "").strip(),
            "workspace_domain": str(workspace.get("workspace_domain") or "").strip(),
            "workspace_base_url": str(workspace.get("workspace_base_url") or "").strip(),
            "workspace_tours_url": str(workspace.get("workspace_tours_url") or "").strip(),
            "workspace_label": workspace_label_candidates[0] if workspace_label_candidates else "",
            "workspace_name": str(payload.get("workspace_name") or binding_metadata.get("crezlo_workspace_name") or "").strip(),
            "workspace_label_candidates": workspace_label_candidates,
            "media_urls_json": cls._crezlo_normalize_url_list(payload.get("media_urls_json")),
            "floorplan_urls_json": cls._crezlo_normalize_url_list(payload.get("floorplan_urls_json")),
            "scene_strategy": str(payload.get("scene_strategy") or "compact").strip().lower() or "compact",
            "scene_selection_json": dict(payload.get("scene_selection_json") or {}),
            "timeout_seconds": timeout_seconds,
        }
        layout_contract = cls._crezlo_json_dict(
            requested_inputs.get("floorplan_analysis_json")
            or requested_inputs.get("source_geometry_json")
        )
        if layout_contract:
            packet["floorplan_analysis_json"] = layout_contract
        return packet

    @classmethod
    def _crezlo_api_request(
        cls,
        method: str,
        path: str,
        *,
        access_token: str,
        payload: dict[str, object] | None = None,
        query: dict[str, str] | None = None,
        timeout_seconds: int = 120,
    ) -> dict[str, object]:
        if not access_token:
            raise ToolExecutionError("crezlo_access_token_missing")
        url = cls._crezlo_api_base() + path
        if query:
            url += "?" + urlencode(query)
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": "EA-Crezlo/1.0",
        }
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ToolExecutionError(f"crezlo_api_http_error:{exc.code}:{detail[:240]}") from exc
        except urllib.error.URLError as exc:
            raise ToolExecutionError(f"crezlo_api_transport_error:{exc.reason}") from exc
        try:
            loaded = json.loads(body)
        except Exception as exc:
            raise ToolExecutionError("crezlo_api_response_invalid") from exc
        return loaded if isinstance(loaded, dict) else {"data": loaded}

    @staticmethod
    def _crezlo_login(
        *,
        login_email: str,
        login_password: str,
        timeout_seconds: int = 120,
    ) -> str:
        email = str(login_email or "").strip()
        password = str(login_password or "").strip()
        if not email:
            raise ToolExecutionError("crezlo_login_email_missing")
        if not password:
            raise ToolExecutionError("crezlo_login_password_missing")
        payload = json.dumps(
            {
                "email_id": email,
                "auth_type": "email",
                "password": password,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{BrowserActToolAdapter._crezlo_api_base()}/login?product_type=accounts",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Origin": "https://accounts.crezlo.com",
                "Referer": "https://accounts.crezlo.com/",
                "User-Agent": "EA-Crezlo/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ToolExecutionError(f"crezlo_login_http_error:{exc.code}:{detail[:240]}") from exc
        except urllib.error.URLError as exc:
            raise ToolExecutionError(f"crezlo_login_transport_error:{exc.reason}") from exc
        try:
            loaded = json.loads(body)
        except Exception as exc:
            raise ToolExecutionError("crezlo_login_response_invalid") from exc
        if not isinstance(loaded, dict):
            raise ToolExecutionError("crezlo_login_response_invalid")
        data = loaded.get("data") if isinstance(loaded.get("data"), dict) else {}
        token = str(data.get("access_token") or loaded.get("access_token") or "").strip()
        if not token:
            raise ToolExecutionError("crezlo_login_access_token_missing")
        return token

    @staticmethod
    def _crezlo_worker_script_path() -> Path:
        explicit = str(os.getenv("EA_CREZLO_PROPERTY_TOUR_WORKER") or "").strip()
        if explicit:
            return Path(explicit).expanduser()
        resolved = Path(__file__).resolve()
        for parent in resolved.parents:
            candidate = parent / "scripts" / "crezlo_property_tour_worker.py"
            if candidate.exists():
                return candidate
        return resolved.parents[0] / "scripts" / "crezlo_property_tour_worker.py"

    @classmethod
    def _run_crezlo_property_tour_worker(
        cls,
        *,
        packet: dict[str, object],
        timeout_seconds: int,
    ) -> dict[str, object]:
        script_path = cls._crezlo_worker_script_path()
        if not script_path.exists():
            raise ToolExecutionError(f"crezlo_worker_missing:{script_path}")
        try:
            completed = subprocess.run(
                ["python3", str(script_path)],
                input=json.dumps(packet, ensure_ascii=True),
                text=True,
                capture_output=True,
                timeout=max(120, timeout_seconds),
                check=False,
            )
        except FileNotFoundError as exc:
            raise ToolExecutionError("python3_missing:crezlo_property_tour_worker") from exc
        except subprocess.TimeoutExpired as exc:
            raise ToolExecutionError("crezlo_worker_timeout") from exc
        raw = str(completed.stdout or "").strip()
        if not raw:
            detail = str(completed.stderr or "").strip()
            raise ToolExecutionError(f"crezlo_worker_empty_output:{detail[:400]}")
        last_line = raw.splitlines()[-1].strip()
        try:
            loaded = json.loads(last_line)
        except Exception as exc:
            raise ToolExecutionError(f"crezlo_worker_non_json:{last_line[:400]}") from exc
        if completed.returncode != 0:
            detail = str((loaded if isinstance(loaded, dict) else {}).get("error") or completed.stderr or raw).strip()
            raise ToolExecutionError(f"crezlo_worker_failed:{detail[:400]}")
        if not isinstance(loaded, dict):
            raise ToolExecutionError("crezlo_worker_invalid_output")
        return loaded

    @staticmethod
    def _ui_service_worker_script_path(service_key: str) -> Path:
        normalized = str(service_key or "").strip().lower()
        service = browseract_ui_service_by_service_key(normalized)
        if service is None:
            builtin_service_map = {
                "onemin_billing_usage": BrowserActToolAdapter._onemin_billing_usage_ui_service,
                "onemin_member_reconciliation": BrowserActToolAdapter._onemin_member_reconciliation_ui_service,
            }
            builder = builtin_service_map.get(normalized)
            if builder is not None:
                service = builder()
        env_map = {
            "mootion_movie": "EA_MOOTION_MOVIE_WORKER",
            "avomap_flyover": "EA_AVOMAP_FLYOVER_WORKER",
            "booka_book": "EA_BOOKA_BOOK_WORKER",
            "browseract_template_service": "EA_BROWSERACT_TEMPLATE_SERVICE_WORKER",
            "onemin_billing_usage": "EA_BROWSERACT_TEMPLATE_SERVICE_WORKER",
            "onemin_member_reconciliation": "EA_BROWSERACT_TEMPLATE_SERVICE_WORKER",
        }
        filename_map = {
            "mootion_movie": "mootion_movie_worker.py",
            "avomap_flyover": "avomap_flyover_worker.py",
            "booka_book": "booka_book_worker.py",
            "browseract_template_service": "browseract_template_service_worker.py",
            "onemin_billing_usage": "browseract_template_service_worker.py",
            "onemin_member_reconciliation": "browseract_template_service_worker.py",
        }
        worker_name = str(service.worker_script_name if service is not None else "").strip()
        if worker_name:
            normalized = "browseract_template_service" if worker_name == "browseract_template_service_worker.py" else normalized
        explicit = str(os.getenv(env_map.get(normalized, "")) or "").strip()
        if explicit:
            return Path(explicit).expanduser()
        filename = worker_name or filename_map.get(normalized, "")
        if not filename:
            return Path("")
        docker_candidate = Path("/docker/EA/scripts") / filename
        if docker_candidate.exists():
            return docker_candidate
        resolved = Path(__file__).resolve()
        for parent in resolved.parents:
            candidate = parent / "scripts" / filename
            if candidate.exists():
                return candidate
        return resolved.parents[0] / "scripts" / filename

    @staticmethod
    def _ui_service_publisher_script_path() -> Path:
        explicit = str(os.getenv("EA_PUBLIC_RESULT_PUBLISHER") or "").strip()
        if explicit:
            return Path(explicit).expanduser()
        docker_candidate = Path("/docker/EA/scripts/publish_browseract_ui_results.py")
        if docker_candidate.exists():
            return docker_candidate
        resolved = Path(__file__).resolve()
        for parent in resolved.parents:
            candidate = parent / "scripts" / "publish_browseract_ui_results.py"
            if candidate.exists():
                return candidate
        return resolved.parents[0] / "scripts" / "publish_browseract_ui_results.py"

    @classmethod
    def _run_ui_service_worker(
        cls,
        *,
        service_key: str,
        packet: dict[str, object],
        timeout_seconds: int,
    ) -> dict[str, object]:
        script_path = cls._ui_service_worker_script_path(service_key)
        if not script_path.exists():
            raise ToolExecutionError(f"ui_service_worker_missing:{service_key}:{script_path}")
        try:
            completed = subprocess.run(
                ["python3", str(script_path)],
                input=json.dumps(packet, ensure_ascii=False),
                text=True,
                capture_output=True,
                timeout=max(180, timeout_seconds + 60),
                check=False,
            )
        except FileNotFoundError as exc:
            raise ToolExecutionError(f"python3_missing:{service_key}_worker") from exc
        except subprocess.TimeoutExpired as exc:
            raise ToolExecutionError(f"ui_service_worker_timeout:{service_key}") from exc
        raw = str(completed.stdout or "").strip()
        if not raw:
            detail = str(completed.stderr or "").strip()
            raise ToolExecutionError(f"ui_service_worker_empty_output:{service_key}:{detail[:400]}")
        last_line = raw.splitlines()[-1].strip()
        try:
            loaded = json.loads(last_line)
        except Exception as exc:
            raise ToolExecutionError(f"ui_service_worker_non_json:{service_key}:{last_line[:400]}") from exc
        if completed.returncode != 0:
            if isinstance(loaded, dict):
                failure_code = str(
                    loaded.get("ui_failure_code")
                    or loaded.get("failure_code")
                    or loaded.get("error_code")
                    or ""
                ).strip().lower()
                if failure_code:
                    raise ToolExecutionError(f"ui_lane_failure:{service_key}:{failure_code}")
            detail = str((loaded if isinstance(loaded, dict) else {}).get("error") or completed.stderr or raw).strip()
            raise ToolExecutionError(f"ui_service_worker_failed:{service_key}:{detail[:400]}")
        if not isinstance(loaded, dict):
            raise ToolExecutionError(f"ui_service_worker_invalid_output:{service_key}")
        return loaded

    @classmethod
    def _publish_ui_service_result(cls, row: dict[str, object]) -> str:
        script_path = cls._ui_service_publisher_script_path()
        if not script_path.exists():
            raise ToolExecutionError(f"ui_service_result_publisher_missing:{script_path}")
        with tempfile.TemporaryDirectory(prefix="ui-service-publish-") as temp_dir_raw:
            temp_dir = Path(temp_dir_raw)
            input_path = temp_dir / "input.json"
            input_path.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
            try:
                completed = subprocess.run(
                    ["python3", str(script_path), "--input", str(input_path)],
                    text=True,
                    capture_output=True,
                    timeout=180,
                    check=False,
                )
            except FileNotFoundError as exc:
                raise ToolExecutionError("python3_missing:publish_browseract_ui_results") from exc
            except subprocess.TimeoutExpired as exc:
                raise ToolExecutionError("ui_service_result_publish_timeout") from exc
            raw = str(completed.stdout or "").strip()
            if completed.returncode != 0:
                detail = str(completed.stderr or raw).strip()
                raise ToolExecutionError(f"ui_service_result_publish_failed:{detail[:400]}")
            payload = json.loads(raw.splitlines()[-1] or "{}")
            index_path = Path(str(payload.get("index") or "").strip())
            if not index_path.exists():
                raise ToolExecutionError("ui_service_result_publish_index_missing")
            rows = json.loads(index_path.read_text(encoding="utf-8"))
            if not isinstance(rows, list) or not rows:
                raise ToolExecutionError("ui_service_result_publish_index_invalid")
            hosted_url = str(rows[-1].get("hosted_url") or "").strip()
            if not hosted_url:
                raise ToolExecutionError("ui_service_result_publish_hosted_url_missing")
            return hosted_url

    @staticmethod
    def _crezlo_public_tour_dir() -> Path:
        return Path(
            str(os.getenv("EA_PUBLIC_TOUR_DIR") or "/docker/property/state/public_property_tours")
        ).expanduser()

    @staticmethod
    def _crezlo_public_tour_base_url() -> str:
        explicit = str(os.getenv("EA_PUBLIC_TOUR_BASE_URL") or "").strip().rstrip("/")
        if explicit:
            return explicit
        property_explicit = str(os.getenv("PROPERTYQUARRY_PUBLIC_TOUR_BASE_URL") or "").strip().rstrip("/")
        if property_explicit:
            return property_explicit
        public_app = (
            str(os.getenv("PROPERTYQUARRY_PUBLIC_BASE_URL") or "").strip().rstrip("/")
            or str(os.getenv("EA_PUBLIC_APP_BASE_URL") or "").strip().rstrip("/")
            or "https://propertyquarry.com"
        )
        return f"{public_app}/tours"

    @staticmethod
    def _crezlo_public_tour_slug(
        value: object,
        *,
        variant_key: str = "",
        digest_seed: str = "",
    ) -> str:
        lowered = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
        if not str(digest_seed or "").strip():
            return lowered or "tour"
        normalized_variant = re.sub(r"[^a-z0-9]+", "-", str(variant_key or "layout-first").lower()).strip("-") or "layout-first"
        material = str(digest_seed or value or "").strip().encode("utf-8")
        digest = hashlib.sha256(material).hexdigest()[:10]
        return f"{lowered or 'tour'}-{normalized_variant}-{digest}"

    @staticmethod
    def _crezlo_public_asset_url(value: object) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if text.lower().startswith(("http://", "https://")):
            return text
        return f"https://media.crezlo.com/{text.lstrip('/')}"

    @staticmethod
    def _crezlo_json_dict(value: object) -> dict[str, object]:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str) and value.strip():
            loaded = _load_jsonish(value)
            if isinstance(loaded, dict):
                return dict(loaded)
        return {}

    @staticmethod
    def _crezlo_json_list(value: object) -> list[object]:
        if isinstance(value, list):
            return list(value)
        if isinstance(value, str) and value.strip():
            loaded = _load_jsonish(value)
            if isinstance(loaded, list):
                return list(loaded)
        return []

    @classmethod
    def _crezlo_immersive_acceptance(cls, normalized: dict[str, object]) -> dict[str, object]:
        """Return fail-closed evidence for a customer-facing Crezlo tour.

        A list of JPEG-backed scenes is not spatial evidence.  Promotion
        requires actual panorama/cubemap scenes, a hotspot/navigation graph,
        an anonymous interaction proof, and a browser-proven first-party URL.
        """

        structured = cls._crezlo_json_dict(normalized.get("structured_output_json"))
        workflow_output = cls._crezlo_json_dict(structured.get("workflow_output_json"))
        detail = cls._crezlo_json_dict(
            workflow_output.get("tour_detail_json") or structured.get("tour_detail_json")
        )
        requested_inputs = cls._crezlo_json_dict(structured.get("requested_inputs"))
        requested_floorplans: list[object] = []
        for key in ("floorplan_urls_json", "floorplan_urls", "floorplans"):
            requested_floorplans.extend(cls._crezlo_json_list(requested_inputs.get(key)))
        floorplan_required = bool(requested_floorplans)
        requested_layout_contract = cls._crezlo_json_dict(
            requested_inputs.get("floorplan_analysis_json")
            or requested_inputs.get("source_geometry_json")
        )
        expected_geometry_projection: dict[str, object] = {}
        if requested_layout_contract:
            try:
                from scripts.property_tour_layout_contract import source_geometry_projection

                expected_geometry_projection = source_geometry_projection(requested_layout_contract)
            except Exception:
                # An unvalidated floorplan contract cannot be promoted as a
                # measured source.  The existing layout receipt checks below
                # will keep the acceptance blocked when this stays empty.
                expected_geometry_projection = {}
        expected_geometry_projection_hash = str(
            expected_geometry_projection.get("sha256") or ""
        ).strip().lower()
        expected_layout_hash = ""
        expected_layout_contract_name = ""
        expected_layout_review_status = ""
        expected_layout_room_count = 0
        expected_layout_portal_ids: set[str] = set()
        if requested_layout_contract:
            expected_layout_hash = hashlib.sha256(
                json.dumps(
                    requested_layout_contract,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            expected_layout_contract_name = str(
                requested_layout_contract.get("contract_name") or ""
            ).strip()
            expected_layout_review_status = str(
                requested_layout_contract.get("review_status") or ""
            ).strip().lower()
            try:
                expected_layout_room_count = int(requested_layout_contract.get("room_count") or 0)
            except Exception:
                expected_layout_room_count = 0
            source_geometry = cls._crezlo_json_dict(requested_layout_contract.get("source_geometry"))
            expected_layout_portal_ids = {
                str(row.get("id") or "").strip()
                for row in cls._crezlo_json_list(source_geometry.get("portals"))
                if isinstance(row, dict) and str(row.get("id") or "").strip()
            }
        scenes = [
            dict(entry)
            for entry in cls._crezlo_json_list(detail.get("scenes"))
            if isinstance(entry, dict)
        ]
        spatial_scene_count = 0
        hotspot_count = 0
        for scene in scenes:
            file_payload = cls._crezlo_json_dict(scene.get("file"))
            documents = (
                scene,
                cls._crezlo_json_dict(scene.get("payload")),
                file_payload,
                cls._crezlo_json_dict(file_payload.get("meta")),
            )
            cube_faces: set[str] = set()
            projection = ""
            width = 0
            height = 0
            for document in documents:
                for key, value in document.items():
                    lowered_key = str(key or "").strip().lower()
                    if lowered_key in {"cube_map", "cube_maps", "cubemap", "cubemaps"}:
                        cube_rows: list[object]
                        if isinstance(value, dict):
                            cube_rows = list(value.values())
                        else:
                            cube_rows = cls._crezlo_json_list(value)
                        for index, cube_row in enumerate(cube_rows):
                            if isinstance(cube_row, dict):
                                face = str(
                                    cube_row.get("face")
                                    or cube_row.get("side")
                                    or cube_row.get("name")
                                    or index
                                ).strip().lower()
                            else:
                                face = str(cube_row or index).strip().lower()
                            if face:
                                cube_faces.add(face)
                    if lowered_key in {"projection", "image_projection", "panorama_type", "mapping"}:
                        projection = str(value or "").strip().lower() or projection
                    if lowered_key in {"width", "image_width", "pixel_width"}:
                        try:
                            width = max(width, int(value or 0))
                        except Exception:
                            pass
                    if lowered_key in {"height", "image_height", "pixel_height"}:
                        try:
                            height = max(height, int(value or 0))
                        except Exception:
                            pass
                    if "hotspot" in lowered_key:
                        hotspot_count += len(cls._crezlo_json_list(value))
            equirectangular = (
                projection in {"equirectangular", "equirect", "360", "spherical"}
                and width >= 4096
                and height >= 2048
                and 1.8 <= (float(width) / float(height)) <= 2.2
            )
            complete_cube_map = len(cube_faces) >= 6
            if complete_cube_map or equirectangular:
                spatial_scene_count += 1

        proof = cls._crezlo_json_dict(
            workflow_output.get("immersive_viewer_proof")
            or structured.get("immersive_viewer_proof")
            or normalized.get("immersive_viewer_proof")
        )
        proof_status = str(proof.get("status") or "").strip().lower()
        try:
            anonymous_http_status = int(proof.get("anonymous_http_status") or 0)
        except Exception:
            anonymous_http_status = 0
        drag_look_verified = bool(proof.get("drag_look_verified"))
        scene_navigation_verified = bool(proof.get("scene_navigation_verified"))
        scene_graph_connected = proof.get("scene_graph_connected") is True
        all_required_scenes_navigable = proof.get("all_required_scenes_navigable") is True
        desktop_viewer_verified = proof.get("desktop_viewer_verified") is True
        mobile_viewer_verified = proof.get("mobile_viewer_verified") is True
        touch_look_verified = proof.get("touch_look_verified") is True
        # A 2:1 image can be generated by an AI model just as easily as it can
        # be captured from a camera.  Dimension heuristics alone therefore do
        # not establish a photorealistic tour.  Promotion requires an explicit
        # provider receipt for either a captured 360 source or a provider
        # render whose spatial provenance was verified against the floorplan.
        spatial_provenance_verified = bool(
            proof.get("captured_360_verified") is True
            or proof.get("provider_render_provenance_verified") is True
        )
        exact_property_provenance_verified = proof.get("exact_property_provenance_verified") is True
        source_asset_hashes = [
            str(value or "").strip().lower().removeprefix("sha256:")
            for value in cls._crezlo_json_list(proof.get("source_asset_hashes"))
        ]
        valid_source_asset_hashes = [
            value for value in source_asset_hashes if re.fullmatch(r"[0-9a-f]{64}", value)
        ]
        browser_receipt_sha256 = str(proof.get("browser_receipt_sha256") or "").strip().lower().removeprefix("sha256:")
        browser_receipt_verified = bool(re.fullmatch(r"[0-9a-f]{64}", browser_receipt_sha256))
        expected_property_url = str(requested_inputs.get("property_url") or "").strip()
        proof_property_url = str(proof.get("source_property_url") or "").strip()
        property_binding_verified = bool(
            expected_property_url
            and proof_property_url
            and expected_property_url == proof_property_url
            and exact_property_provenance_verified
        )
        floorplan_alignment_verified = proof.get("floorplan_alignment_verified") is True
        floorplan_geometry_receipt_verified = proof.get("floorplan_geometry_receipt_verified") is True
        proof_geometry_projection_hash = str(
            proof.get("floorplan_source_geometry_projection_sha256") or ""
        ).strip().lower().removeprefix("sha256:")
        floorplan_geometry_projection_verified = bool(
            not floorplan_required
            or (
                bool(re.fullmatch(r"[0-9a-f]{64}", expected_geometry_projection_hash))
                and proof.get("floorplan_geometry_projection_verified") is True
                and proof_geometry_projection_hash == expected_geometry_projection_hash
            )
        )
        proof_layout_hash = str(proof.get("floorplan_analysis_sha256") or "").strip().lower().removeprefix("sha256:")
        proof_layout_contract_name = str(proof.get("floorplan_analysis_contract_name") or "").strip()
        proof_layout_review_status = str(proof.get("floorplan_analysis_review_status") or "").strip().lower()
        try:
            proof_layout_room_count = int(proof.get("floorplan_source_room_count") or 0)
        except Exception:
            proof_layout_room_count = 0
        proof_layout_portal_ids = {
            str(value or "").strip()
            for value in cls._crezlo_json_list(proof.get("floorplan_source_portal_ids"))
            if str(value or "").strip()
        }
        floorplan_layout_receipt_verified = bool(
            not floorplan_required
            or (
                bool(requested_layout_contract)
                and expected_layout_contract_name == "propertyquarry.floorplan_analysis.v2"
                and expected_layout_review_status in {"approved", "reviewed"}
                and bool(re.fullmatch(r"[0-9a-f]{64}", expected_layout_hash))
                and proof_layout_hash == expected_layout_hash
                and proof_layout_contract_name == expected_layout_contract_name
                and proof_layout_review_status == expected_layout_review_status
                and proof_layout_room_count == expected_layout_room_count
                and expected_layout_portal_ids.issubset(proof_layout_portal_ids)
                and floorplan_geometry_receipt_verified
            )
        )
        try:
            required_spatial_scene_count = max(3, min(24, int(proof.get("required_spatial_scene_count") or 3)))
        except Exception:
            required_spatial_scene_count = 3
        try:
            required_space_count = max(1, min(24, int(proof.get("required_space_count") or required_spatial_scene_count)))
        except Exception:
            required_space_count = required_spatial_scene_count
        try:
            covered_space_count = max(0, int(proof.get("covered_space_count") or 0))
        except Exception:
            covered_space_count = 0
        first_party_url = str(proof.get("first_party_public_url") or "").strip()
        first_party_base = cls._crezlo_public_tour_base_url().rstrip("/")
        first_party_url_verified = bool(
            first_party_url
            and first_party_base
            and (first_party_url == first_party_base or first_party_url.startswith(first_party_base + "/"))
            and proof.get("first_party_viewer_verified") is True
        )
        provider_control_route_verified = bool(
            first_party_url_verified
            and proof.get("provider_control_route_verified") is True
        )
        accepted = bool(
            spatial_scene_count >= required_spatial_scene_count
            and covered_space_count >= required_space_count
            and hotspot_count >= spatial_scene_count - 1
            and scene_graph_connected
            and all_required_scenes_navigable
            and property_binding_verified
            and bool(valid_source_asset_hashes)
            and browser_receipt_verified
            and (not floorplan_required or floorplan_alignment_verified)
            and floorplan_layout_receipt_verified
            and floorplan_geometry_projection_verified
            and proof_status == "pass"
            and anonymous_http_status == 200
            and drag_look_verified
            and scene_navigation_verified
            and desktop_viewer_verified
            and mobile_viewer_verified
            and touch_look_verified
            and spatial_provenance_verified
            and provider_control_route_verified
        )
        if spatial_scene_count < required_spatial_scene_count:
            reason = "spatial_scenes_missing"
        elif covered_space_count < required_space_count:
            reason = "required_space_coverage_missing"
        elif hotspot_count < spatial_scene_count - 1:
            reason = "hotspot_graph_missing"
        elif not scene_graph_connected or not all_required_scenes_navigable:
            reason = "scene_graph_unverified"
        elif not property_binding_verified or not valid_source_asset_hashes:
            reason = "exact_property_provenance_unverified"
        elif not browser_receipt_verified:
            reason = "browser_receipt_unverified"
        elif not spatial_provenance_verified:
            reason = "spatial_provenance_unverified"
        elif floorplan_required and not floorplan_alignment_verified:
            reason = "floorplan_alignment_unverified"
        elif floorplan_required and not floorplan_layout_receipt_verified:
            reason = "floorplan_geometry_receipt_unverified"
        elif floorplan_required and not floorplan_geometry_projection_verified:
            reason = "floorplan_geometry_projection_unverified"
        elif proof_status != "pass":
            reason = "browser_proof_missing"
        elif anonymous_http_status != 200:
            reason = "anonymous_viewer_unverified"
        elif not drag_look_verified:
            reason = "drag_look_unverified"
        elif not scene_navigation_verified:
            reason = "scene_navigation_unverified"
        elif not desktop_viewer_verified or not mobile_viewer_verified or not touch_look_verified:
            reason = "cross_device_interaction_unverified"
        elif not provider_control_route_verified:
            reason = "first_party_viewer_unverified"
        else:
            reason = ""
        return {
            "accepted": accepted,
            "reason": reason,
            "scene_count": len(scenes),
            "spatial_scene_count": spatial_scene_count,
            "required_spatial_scene_count": required_spatial_scene_count,
            "required_space_count": required_space_count,
            "covered_space_count": covered_space_count,
            "hotspot_count": hotspot_count,
            "scene_graph_connected": scene_graph_connected,
            "all_required_scenes_navigable": all_required_scenes_navigable,
            "exact_property_provenance_verified": exact_property_provenance_verified,
            "property_binding_verified": property_binding_verified,
            "source_asset_hash_count": len(valid_source_asset_hashes),
            "browser_receipt_verified": browser_receipt_verified,
            "spatial_provenance_verified": spatial_provenance_verified,
            "floorplan_required": floorplan_required,
            "floorplan_alignment_verified": floorplan_alignment_verified,
            "floorplan_layout_receipt_verified": floorplan_layout_receipt_verified,
            "floorplan_geometry_projection_verified": floorplan_geometry_projection_verified,
            "floorplan_source_geometry_projection_sha256": (
                expected_geometry_projection_hash if floorplan_geometry_projection_verified else ""
            ),
            "anonymous_http_status": anonymous_http_status,
            "drag_look_verified": drag_look_verified,
            "scene_navigation_verified": scene_navigation_verified,
            "desktop_viewer_verified": desktop_viewer_verified,
            "mobile_viewer_verified": mobile_viewer_verified,
            "touch_look_verified": touch_look_verified,
            "first_party_viewer_verified": first_party_url_verified,
            "provider_control_route_verified": provider_control_route_verified,
            "first_party_public_url": first_party_url if first_party_url_verified else "",
        }

    @classmethod
    def _crezlo_public_asset_rows(cls, normalized: dict[str, object]) -> list[dict[str, object]]:
        structured = cls._crezlo_json_dict(normalized.get("structured_output_json"))
        requested_inputs = cls._crezlo_json_dict(structured.get("requested_inputs"))
        workflow_output = cls._crezlo_json_dict(structured.get("workflow_output_json"))
        detail = cls._crezlo_json_dict(
            workflow_output.get("tour_detail_json") or structured.get("tour_detail_json")
        )
        file_records = [
            cls._crezlo_json_dict(entry)
            for entry in cls._crezlo_json_list(
                workflow_output.get("file_records_json") or structured.get("file_records_json")
            )
            if isinstance(entry, dict)
        ]
        file_record_by_id = {
            str(entry.get("id") or "").strip(): entry
            for entry in file_records
            if str(entry.get("id") or "").strip()
        }
        rows: list[dict[str, object]] = []
        detail_scenes = cls._crezlo_json_list(detail.get("scenes"))
        for ordinal, entry in enumerate(detail_scenes, start=1):
            if not isinstance(entry, dict):
                continue
            scene = dict(entry)
            file_payload = cls._crezlo_json_dict(scene.get("file"))
            file_id = str(file_payload.get("id") or "").strip()
            file_record = file_record_by_id.get(file_id) or file_payload
            meta = cls._crezlo_json_dict(file_record.get("meta") or file_payload.get("meta"))
            image_url = cls._crezlo_maybe_url(file_record.get("path") or file_payload.get("path"))
            if not image_url:
                image_url = cls._crezlo_public_asset_url(file_record.get("path") or file_payload.get("path"))
            if not image_url:
                continue
            rows.append(
                {
                    "ordinal": ordinal,
                    "name": str(scene.get("name") or file_record.get("name") or f"scene-{ordinal}").strip()
                    or f"scene-{ordinal}",
                    "image_url": image_url,
                    "role": str(meta.get("role") or "photo").strip() or "photo",
                    "source_url": str(meta.get("source_url") or image_url).strip() or image_url,
                    "property_url": str(meta.get("property_url") or "").strip(),
                    "mime_type": str(file_record.get("mime_type") or file_payload.get("mime_type") or "").strip(),
                }
            )
        if rows:
            return rows
        for ordinal, file_record in enumerate(file_records, start=1):
            meta = cls._crezlo_json_dict(file_record.get("meta"))
            image_url = cls._crezlo_maybe_url(file_record.get("path"))
            if not image_url:
                image_url = cls._crezlo_public_asset_url(file_record.get("path"))
            if not image_url:
                continue
            rows.append(
                {
                    "ordinal": ordinal,
                    "name": str(file_record.get("name") or f"scene-{ordinal}").strip() or f"scene-{ordinal}",
                    "image_url": image_url,
                    "role": str(meta.get("role") or "photo").strip() or "photo",
                    "source_url": str(meta.get("source_url") or image_url).strip() or image_url,
                    "property_url": str(meta.get("property_url") or "").strip(),
                    "mime_type": str(file_record.get("mime_type") or "").strip(),
                }
            )
        if rows:
            return rows
        ui_worker_output = cls._crezlo_ui_worker_publishable_output(
            result=workflow_output,
            requested_inputs=requested_inputs,
        )
        if ui_worker_output:
            enriched_structured = dict(structured)
            enriched_workflow_output = dict(workflow_output)
            enriched_workflow_output.update(ui_worker_output)
            enriched_structured["workflow_output_json"] = enriched_workflow_output
            return cls._crezlo_public_asset_rows({"structured_output_json": enriched_structured})
        selected_assets = cls._crezlo_select_asset_urls(
            media_urls=cls._crezlo_normalize_url_list(requested_inputs.get("media_urls_json")),
            floorplan_urls=cls._crezlo_normalize_url_list(requested_inputs.get("floorplan_urls_json")),
            scene_strategy=str(requested_inputs.get("scene_strategy") or "compact").strip().lower() or "compact",
            scene_selection_json=cls._crezlo_json_dict(requested_inputs.get("scene_selection_json")),
        )
        property_url = str(requested_inputs.get("property_url") or "").strip()
        for ordinal, (role, asset_url) in enumerate(selected_assets, start=1):
            image_url = cls._crezlo_maybe_url(asset_url)
            if not image_url:
                continue
            rows.append(
                {
                    "ordinal": ordinal,
                    "name": f"scene-{ordinal}",
                    "image_url": image_url,
                    "role": str(role or "photo").strip() or "photo",
                    "source_url": image_url,
                    "property_url": property_url,
                    "mime_type": cls._crezlo_asset_mime_type(asset_url=image_url, role=str(role or "photo").strip() or "photo"),
                }
            )
        return rows

    @classmethod
    def _crezlo_ui_worker_publishable_output(
        cls,
        *,
        result: dict[str, object],
        requested_inputs: dict[str, object],
    ) -> dict[str, object]:
        scenes_response = cls._crezlo_json_dict(result.get("scenes_response_json"))
        scenes_root = cls._crezlo_json_dict(scenes_response.get("data"))
        raw_scenes = [
            cls._crezlo_json_dict(entry)
            for entry in cls._crezlo_json_list(scenes_root.get("data"))
            if isinstance(entry, dict)
        ]
        if not raw_scenes:
            return {}
        selected_assets = cls._crezlo_select_asset_urls(
            media_urls=cls._crezlo_normalize_url_list(requested_inputs.get("media_urls_json")),
            floorplan_urls=cls._crezlo_normalize_url_list(requested_inputs.get("floorplan_urls_json")),
            scene_strategy=str(requested_inputs.get("scene_strategy") or "compact").strip().lower() or "compact",
            scene_selection_json=cls._crezlo_json_dict(requested_inputs.get("scene_selection_json")),
        )
        property_url = str(requested_inputs.get("property_url") or "").strip()
        file_records: list[dict[str, object]] = []
        scenes: list[dict[str, object]] = []
        for ordinal, scene in enumerate(
            sorted(
                raw_scenes,
                key=lambda entry: int(entry.get("order") or 0),
            ),
            start=1,
        ):
            file_payload = cls._crezlo_json_dict(scene.get("file"))
            relative_path = str(file_payload.get("path") or "").strip()
            image_url = cls._crezlo_public_asset_url(relative_path)
            if not image_url:
                continue
            selection_index = ordinal - 1
            role = "photo"
            source_url = image_url
            if 0 <= selection_index < len(selected_assets):
                role, source_url = selected_assets[selection_index]
            meta = {
                "role": role,
                "source_url": source_url,
                "property_url": property_url,
            }
            file_record = dict(file_payload)
            file_record["path"] = image_url
            file_record["meta"] = meta
            file_records.append(file_record)
            scene_row = dict(scene)
            scene_row["file"] = file_record
            scenes.append(scene_row)
        if not file_records:
            return {}
        detail_json = {
            "id": str(result.get("tour_id") or "").strip(),
            "slug": str(result.get("slug") or "").strip(),
            "title": str(result.get("tour_title") or requested_inputs.get("tour_title") or "").strip(),
            "status": str(result.get("tour_status") or "").strip() or "published",
            "scenes": scenes,
        }
        return {
            "file_records_json": file_records,
            "tour_detail_json": detail_json,
        }

    @classmethod
    def _crezlo_download_public_asset(cls, url: str) -> tuple[bytes, str]:
        request = urllib.request.Request(url, headers={"User-Agent": "EA-Crezlo-Tour-Mirror/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                return response.read(), str(response.headers.get("Content-Type") or "").strip()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ToolExecutionError(f"crezlo_public_asset_http_error:{exc.code}:{detail[:240]}") from exc
        except urllib.error.URLError as exc:
            raise ToolExecutionError(f"crezlo_public_asset_transport_error:{exc.reason}") from exc

    @staticmethod
    def _crezlo_asset_suffix(*, url: str, content_type: str) -> str:
        guessed = mimetypes.guess_extension((content_type or "").split(";", 1)[0].strip())
        if guessed:
            return guessed
        suffix = Path(urlparse(url).path).suffix
        return suffix or ".bin"

    @classmethod
    def _publish_crezlo_public_tour_bundle(cls, normalized: dict[str, object]) -> str:
        from app.api.routes.public_tour_payloads import redacted_public_tour_payload

        # The service path normally calls this writer only after the
        # immersive gate.  Keep the writer fail-closed as well when a blocked
        # result is passed directly, so a vendor URL can never be repackaged
        # into a first-party gallery by an alternate caller.
        structured_for_gate = cls._crezlo_json_dict(normalized.get("structured_output_json"))
        try:
            from scripts.property_tour_publication_gate import publication_gate_result

            publication_accepted, _publication_reason = publication_gate_result(structured_for_gate)
        except Exception:
            publication_accepted = False
        if not publication_accepted:
            return ""
        acceptance_for_gate = cls._crezlo_json_dict(structured_for_gate.get("immersive_acceptance_json"))
        if not acceptance_for_gate or acceptance_for_gate.get("accepted") is not True:
            return ""
        for required_key in (
            "spatial_provenance_verified",
            "exact_property_provenance_verified",
            "browser_receipt_verified",
            "scene_graph_connected",
            "all_required_scenes_navigable",
            "first_party_viewer_verified",
            "provider_control_route_verified",
        ):
            if acceptance_for_gate.get(required_key) is not True:
                return ""
        if acceptance_for_gate.get("floorplan_required") is True and any(
            acceptance_for_gate.get(key) is not True
            for key in (
                "floorplan_alignment_verified",
                "floorplan_layout_receipt_verified",
                "floorplan_geometry_projection_verified",
            )
        ):
            return ""
        provenance_for_gate = cls._crezlo_json_dict(structured_for_gate.get("crezlo_source_provenance"))
        if (
            provenance_for_gate.get("schema") != "propertyquarry.crezlo_source_provenance.v1"
            or str(provenance_for_gate.get("status") or "").strip().lower() != "pass"
            or str(provenance_for_gate.get("provider") or "").strip().lower() != "crezlo"
        ):
            return ""

        rows = cls._crezlo_public_asset_rows(normalized)
        if not rows:
            return ""
        structured = cls._crezlo_json_dict(normalized.get("structured_output_json"))
        requested_inputs = cls._crezlo_json_dict(structured.get("requested_inputs"))
        workflow_output = cls._crezlo_json_dict(structured.get("workflow_output_json"))
        detail = cls._crezlo_json_dict(workflow_output.get("tour_detail_json") or structured.get("tour_detail_json"))
        property_facts = cls._crezlo_json_dict(requested_inputs.get("property_facts_json"))
        variant_key = str(
            requested_inputs.get("variant_key")
            or requested_inputs.get("scene_strategy")
            or workflow_output.get("variant_key")
            or ""
        ).strip()
        source_virtual_tour_url = str(requested_inputs.get("source_virtual_tour_url") or "").strip()
        listing_id = str(requested_inputs.get("listing_id") or "").strip()
        property_url = str(
            requested_inputs.get("property_url")
            or requested_inputs.get("listing_url")
            or ""
        ).strip()
        explicit_slug = str(normalized.get("slug") or detail.get("slug") or "").strip()
        if explicit_slug:
            slug = cls._crezlo_public_tour_slug(explicit_slug)
        else:
            slug = cls._crezlo_public_tour_slug(
                normalized.get("tour_title") or requested_inputs.get("tour_title"),
                variant_key=variant_key,
                digest_seed="|".join(
                    value
                    for value in (
                        property_url,
                        listing_id,
                        source_virtual_tour_url,
                        str(normalized.get("tour_id") or ""),
                        str(detail.get("id") or ""),
                    )
                    if value
                ),
            )
        output_dir = cls._crezlo_public_tour_dir()
        output_dir.mkdir(parents=True, exist_ok=True)
        target_dir = output_dir / slug
        staging_dir = output_dir / f".{slug}.tmp-{uuid.uuid4().hex}"
        staging_dir.mkdir(parents=True, exist_ok=True)
        published_rows: list[dict[str, object]] = []
        try:
            for ordinal, row in enumerate(rows, start=1):
                image_url = str(row.get("image_url") or "").strip()
                if not image_url:
                    continue
                data, content_type = cls._crezlo_download_public_asset(image_url)
                suffix = cls._crezlo_asset_suffix(
                    url=image_url,
                    content_type=content_type or str(row.get("mime_type") or ""),
                )
                filename = f"scene-{ordinal:02d}{suffix}"
                (staging_dir / filename).write_bytes(data)
                published_rows.append(
                    {
                        **row,
                        "ordinal": ordinal,
                        "asset_relpath": filename,
                        "mime_type": content_type.split(";", 1)[0].strip()
                        or str(row.get("mime_type") or "").strip(),
                    }
                )
            if not published_rows:
                return ""
            hosted_url = f"{cls._crezlo_public_tour_base_url()}/{slug}"
            if source_virtual_tour_url:
                hosted_url = f"{hosted_url}#live-360"
            if not normalized.get("slug"):
                normalized["slug"] = slug
            title = str(
                normalized.get("tour_title")
                or detail.get("title")
                or requested_inputs.get("tour_title")
                or "Property Tour"
            ).strip() or "Property Tour"
            display_title = str(
                requested_inputs.get("display_title")
                or detail.get("display_title")
                or property_facts.get("listing_title")
                or title
            ).strip() or title
            payload = {
                "slug": slug,
                "hosted_url": hosted_url,
                "listing_url": str(
                    requested_inputs.get("property_url")
                    or published_rows[0].get("property_url")
                    or ""
                ).strip(),
                "title": property_facts.get("listing_title") or title,
                "display_title": display_title,
                "tour_title": title,
                "tour_id": normalized.get("tour_id"),
                "variant_key": variant_key,
                "variant_label": variant_key.replace("_", " "),
                "scene_strategy": str(requested_inputs.get("scene_strategy") or variant_key).strip(),
                "scene_count": len(published_rows),
                "facts": {
                    "rooms": property_facts.get("rooms"),
                    "area_sqm": property_facts.get("area_sqm"),
                    "total_rent_eur": property_facts.get("total_rent_eur") or property_facts.get("price_total_rent"),
                    "availability": property_facts.get("availability") or property_facts.get("availability_text"),
                    "address_lines": property_facts.get("address_lines")
                    or [value for value in (property_facts.get("address"), property_facts.get("district")) if value],
                    "teaser_attributes": property_facts.get("teaser_attributes")
                    or [value for value in (property_facts.get("area_sqm"), property_facts.get("rooms")) if value],
                },
                "brief": {
                    "theme_name": str(requested_inputs.get("theme_name") or "").strip(),
                    "tour_style": str(requested_inputs.get("tour_style") or "").strip(),
                    "audience": str(requested_inputs.get("audience") or "").strip(),
                    "creative_brief": str(requested_inputs.get("creative_brief") or "").strip(),
                    "call_to_action": str(requested_inputs.get("call_to_action") or "").strip(),
                },
                "editor_url": str(normalized.get("editor_url") or "").strip(),
                "crezlo_public_url": str(normalized.get("public_url") or "").strip(),
                "source_virtual_tour_url": source_virtual_tour_url,
                "panorama_source": str(requested_inputs.get("panorama_source") or "").strip(),
                "brand_name": str(
                    requested_inputs.get("brand_name")
                    or os.getenv("EA_PUBLIC_TOUR_BRAND_NAME")
                    or "Pioche Lecombe"
                ).strip(),
                "scenes": published_rows,
                "control_mode": "crezlo",
                "viewer_provider": "crezlo",
            }
            private_receipt = {
                "contract_name": "propertyquarry.public_tour_private_receipt.v1",
                "slug": slug,
                "listing_url": str(payload.get("listing_url") or "").strip(),
                "property_url": property_url,
                "source_virtual_tour_url": source_virtual_tour_url,
                "panorama_source": str(requested_inputs.get("panorama_source") or "").strip(),
                "editor_url": str(normalized.get("editor_url") or "").strip(),
                "crezlo_public_url": str(normalized.get("public_url") or "").strip(),
                "brief": dict(payload.get("brief") or {}) if isinstance(payload.get("brief"), dict) else {},
                "created_at": datetime.now(timezone.utc).isoformat(),
                "crezlo_source_provenance": provenance_for_gate,
                "crezlo_browser_render_proof": dict(
                    structured_for_gate.get("crezlo_browser_render_proof") or {}
                ),
            }
            public_payload = redacted_public_tour_payload(
                payload,
                expose_asset_relpaths=True,
                url_allowed=lambda _url: False,
                bundle_dir_resolver=lambda requested_slug: staging_dir if str(requested_slug or "").strip() == slug else None,
            )
            if hosted_url:
                public_payload["hosted_url"] = hosted_url
                public_payload["public_url"] = hosted_url
            (staging_dir / "tour.json").write_text(
                json.dumps(public_payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            (staging_dir / "tour.private.json").write_text(
                json.dumps(private_receipt, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            backup_dir = output_dir / f".{slug}.bak-{uuid.uuid4().hex}"
            replaced_existing = False
            if target_dir.exists():
                target_dir.replace(backup_dir)
                replaced_existing = True
            staging_dir.replace(target_dir)
            if replaced_existing and backup_dir.exists():
                shutil.rmtree(backup_dir, ignore_errors=True)
            return hosted_url
        finally:
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)

    @classmethod
    def _ui_service_login_email(
        cls,
        payload: dict[str, object],
        *,
        binding_metadata: dict[str, object],
        service: BrowserActUiServiceDefinition,
    ) -> str:
        return str(
            payload.get("login_email")
            or payload.get("browseract_username")
            or binding_metadata.get("login_email")
            or binding_metadata.get("browseract_username")
            or binding_metadata.get("username")
            or cls._binding_service_account_email(binding_metadata=binding_metadata, service=service)
            or os.getenv("EA_UI_SERVICE_LOGIN_EMAIL")
        ).strip()

    @staticmethod
    def _ui_service_login_password(payload: dict[str, object], *, binding_metadata: dict[str, object]) -> str:
        return str(
            payload.get("login_password")
            or payload.get("browseract_password")
            or binding_metadata.get("login_password")
            or binding_metadata.get("browseract_password")
            or binding_metadata.get("password")
            or os.getenv("EA_UI_SERVICE_LOGIN_PASSWORD")
        ).strip()

    @staticmethod
    def _browser_proxy_setting(
        payload: dict[str, object],
        *,
        binding_metadata: dict[str, object],
        env_name: str,
        payload_keys: tuple[str, ...],
        metadata_keys: tuple[str, ...],
    ) -> str:
        for key in payload_keys:
            value = payload.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        for key in metadata_keys:
            value = binding_metadata.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return str(os.getenv(env_name) or "").strip()

    @classmethod
    def _browser_proxy_settings(
        cls,
        payload: dict[str, object],
        *,
        binding_metadata: dict[str, object],
    ) -> dict[str, str]:
        settings = {
            "browser_proxy_server": cls._browser_proxy_setting(
                payload,
                binding_metadata=binding_metadata,
                env_name="EA_UI_BROWSER_PROXY_SERVER",
                payload_keys=("browser_proxy_server", "proxy_server"),
                metadata_keys=("browser_proxy_server", "proxy_server"),
            ),
            "browser_proxy_username": cls._browser_proxy_setting(
                payload,
                binding_metadata=binding_metadata,
                env_name="EA_UI_BROWSER_PROXY_USERNAME",
                payload_keys=("browser_proxy_username", "proxy_username"),
                metadata_keys=("browser_proxy_username", "proxy_username"),
            ),
            "browser_proxy_password": cls._browser_proxy_setting(
                payload,
                binding_metadata=binding_metadata,
                env_name="EA_UI_BROWSER_PROXY_PASSWORD",
                payload_keys=("browser_proxy_password", "proxy_password"),
                metadata_keys=("browser_proxy_password", "proxy_password"),
            ),
            "browser_proxy_bypass": cls._browser_proxy_setting(
                payload,
                binding_metadata=binding_metadata,
                env_name="EA_UI_BROWSER_PROXY_BYPASS",
                payload_keys=("browser_proxy_bypass", "proxy_bypass"),
                metadata_keys=("browser_proxy_bypass", "proxy_bypass"),
            ),
        }
        return {key: value for key, value in settings.items() if value}

    @classmethod
    def _execute_ui_service_worker_direct(
        cls,
        *,
        service_key: str,
        request_payload: dict[str, object],
        requested_inputs: dict[str, object],
        binding_metadata: dict[str, object],
        service: BrowserActUiServiceDefinition,
        workflow_id: str,
        run_url: str,
        extra_packet: dict[str, object] | None = None,
        allow_force_local: bool = False,
    ) -> dict[str, object] | None:
        if bool(request_payload.get("force_browseract")) and not allow_force_local:
            return None
        timeout_seconds = max(120, int(request_payload.get("timeout_seconds") or 360))
        packet = dict(request_payload)
        packet.update(requested_inputs)
        if isinstance(extra_packet, dict):
            packet.update(extra_packet)
        packet.setdefault("service_key", service.service_key)
        packet.setdefault("timeout_seconds", timeout_seconds)
        packet.setdefault(
            "login_email",
            cls._ui_service_login_email(
                request_payload,
                binding_metadata=binding_metadata,
                service=service,
            ),
        )
        packet.setdefault(
            "login_password",
            cls._ui_service_login_password(
                request_payload,
                binding_metadata=binding_metadata,
            ),
        )
        packet.setdefault("workflow_id", workflow_id)
        packet.setdefault("run_url", run_url)
        result = cls._run_ui_service_worker(
            service_key=service_key,
            packet=packet,
            timeout_seconds=timeout_seconds,
        )
        if request_payload.get("proxy_result", True):
            hosted_url = cls._publish_ui_service_result(result)
            result["hosted_url"] = hosted_url
            result["public_url"] = hosted_url
        if workflow_id and not result.get("workflow_id"):
            result["workflow_id"] = workflow_id
        if run_url and not result.get("requested_url"):
            result["requested_url"] = run_url
        elif not result.get("requested_url"):
            template_key = str(packet.get("template_key") or "").strip()
            if template_key:
                result["requested_url"] = f"browseract-template://{template_key}"
        return result

    @classmethod
    def _create_mootion_movie_direct(
        cls,
        *,
        run_url: str,
        workflow_id: str,
        request_payload: dict[str, object],
        requested_inputs: dict[str, object],
        binding_metadata: dict[str, object],
        service: BrowserActUiServiceDefinition,
    ) -> dict[str, object] | None:
        return cls._execute_ui_service_worker_direct(
            service_key=service.service_key,
            request_payload=request_payload,
            requested_inputs=requested_inputs,
            binding_metadata=binding_metadata,
            service=service,
            workflow_id=workflow_id,
            run_url=run_url,
        )

    @classmethod
    def _create_avomap_flyover_direct(
        cls,
        *,
        run_url: str,
        workflow_id: str,
        request_payload: dict[str, object],
        requested_inputs: dict[str, object],
        binding_metadata: dict[str, object],
        service: BrowserActUiServiceDefinition,
    ) -> dict[str, object] | None:
        return cls._execute_ui_service_worker_direct(
            service_key=service.service_key,
            request_payload=request_payload,
            requested_inputs=requested_inputs,
            binding_metadata=binding_metadata,
            service=service,
            workflow_id=workflow_id,
            run_url=run_url,
        )

    @classmethod
    def _create_booka_book_direct(
        cls,
        *,
        run_url: str,
        workflow_id: str,
        request_payload: dict[str, object],
        requested_inputs: dict[str, object],
        binding_metadata: dict[str, object],
        service: BrowserActUiServiceDefinition,
    ) -> dict[str, object] | None:
        return cls._execute_ui_service_worker_direct(
            service_key=service.service_key,
            request_payload=request_payload,
            requested_inputs=requested_inputs,
            binding_metadata=binding_metadata,
            service=service,
            workflow_id=workflow_id,
            run_url=run_url,
        )

    @classmethod
    def _create_template_backed_ui_service_direct(
        cls,
        *,
        run_url: str,
        workflow_id: str,
        request_payload: dict[str, object],
        requested_inputs: dict[str, object],
        binding_metadata: dict[str, object],
        service: BrowserActUiServiceDefinition,
    ) -> dict[str, object] | None:
        if not service.template_key:
            raise ToolExecutionError(f"ui_service_template_missing:{service.service_key}")
        if bool(request_payload.get("force_browseract")):
            return None
        remote_fallback_allowed = bool(
            request_payload.get("force_browseract")
            or request_payload.get("allow_browseract_remote_fallback")
            or request_payload.get("remote_fallback_allowed")
        )
        try:
            template_spec = browseract_ui_template_spec(service.template_key)
        except KeyError as exc:
            raise ToolExecutionError(f"ui_service_template_missing:{service.service_key}:{service.template_key}") from exc
        try:
            result = cls._execute_ui_service_worker_direct(
                service_key=service.service_key,
                request_payload=request_payload,
                requested_inputs=requested_inputs,
                binding_metadata=binding_metadata,
                service=service,
                workflow_id=workflow_id,
                run_url=run_url,
                extra_packet={
                    "template_key": service.template_key,
                    "workflow_spec_json": template_spec,
                },
                allow_force_local=True,
            )
        except ToolExecutionError:
            if workflow_id and remote_fallback_allowed:
                return None
            raise
        if workflow_id and remote_fallback_allowed and cls._browseract_ui_direct_result_needs_remote_retry(result=result):
            return None
        return result

    @classmethod
    def _crezlo_fetch_tour_detail(
        cls,
        *,
        access_token: str,
        workspace_id: str,
        tour_id: str,
    ) -> dict[str, object]:
        if not workspace_id:
            raise ToolExecutionError("crezlo_workspace_id_missing")
        if not tour_id:
            raise ToolExecutionError("crezlo_tour_id_missing")
        body = cls._crezlo_api_request(
            "GET",
            f"/tours/{tour_id}",
            access_token=access_token,
            query={"product_type": "tours", "workspace_id": workspace_id},
            timeout_seconds=120,
        )
        data = body.get("data")
        if not isinstance(data, dict):
            raise ToolExecutionError("crezlo_tour_detail_missing")
        return dict(data)

    @staticmethod
    def _crezlo_tour_patch_payload(
        *,
        detail: dict[str, object],
        payload: dict[str, object],
    ) -> dict[str, object] | None:
        patch: dict[str, object] = {}
        for raw_key in ("tour_settings_json", "tour_patch_json"):
            value = payload.get(raw_key)
            if isinstance(value, dict):
                patch.update(dict(value))
        if "display_title" in payload:
            display_title = str(payload.get("display_title") or "").strip()
            patch["display_title"] = display_title or None
        if "is_private" in payload and payload.get("is_private") is not None:
            patch["is_private"] = bool(payload.get("is_private"))
        visibility = str(payload.get("tour_visibility") or "").strip().lower()
        if visibility in {"private", "locked"}:
            patch["is_private"] = True
        elif visibility in {"public", "shared", "published"}:
            patch["is_private"] = False
        raw_payload_json = payload.get("tour_payload_json")
        if raw_payload_json is not None:
            if isinstance(raw_payload_json, list):
                patch["payload"] = list(raw_payload_json)
            elif isinstance(raw_payload_json, dict):
                patch["payload"] = [dict(raw_payload_json)]
            else:
                patch["payload"] = [raw_payload_json]
        title = str(payload.get("tour_title") or detail.get("title") or "").strip()
        title_changed = title and title != str(detail.get("title") or "").strip()
        if not patch and not title_changed:
            return None
        body = dict(detail)
        if title:
            body["title"] = title
        body.setdefault("scenes", list(detail.get("scenes") or []))
        for key, value in patch.items():
            body[key] = value
        return body

    @classmethod
    def _crezlo_update_tour(
        cls,
        *,
        access_token: str,
        workspace_id: str,
        tour_id: str,
        body: dict[str, object],
    ) -> dict[str, object]:
        response = cls._crezlo_api_request(
            "PUT",
            f"/tours/{tour_id}",
            access_token=access_token,
            payload=body,
            query={"product_type": "tours", "workspace_id": workspace_id},
            timeout_seconds=180,
        )
        data = response.get("data")
        if not isinstance(data, dict):
            raise ToolExecutionError("crezlo_tour_update_missing")
        return dict(data)

    @staticmethod
    def _crezlo_candidate_public_url(*, workspace_domain: str, slug: str) -> str:
        domain = str(workspace_domain or "").strip()
        normalized_slug = str(slug or "").strip()
        if not domain or not normalized_slug:
            return ""
        return f"https://{domain}/tours/{normalized_slug}"

    @classmethod
    def _crezlo_normalize_url_list(cls, value: object) -> list[str]:
        if isinstance(value, list):
            values: list[str] = []
            for entry in value:
                text = str(entry or "").strip()
                if text:
                    values.append(text)
            return values
        if isinstance(value, str) and value.strip().startswith("["):
            loaded = _load_jsonish(value)
            if isinstance(loaded, list):
                return cls._crezlo_normalize_url_list(loaded)
        text = str(value or "").strip()
        return [text] if text else []

    @classmethod
    def _crezlo_select_asset_urls(
        cls,
        *,
        media_urls: list[str],
        floorplan_urls: list[str],
        scene_strategy: str,
        scene_selection_json: dict[str, object],
    ) -> list[tuple[str, str]]:
        photos = [("photo", entry) for entry in media_urls if str(entry or "").strip()]
        floorplans = [("floorplan", entry) for entry in floorplan_urls if str(entry or "").strip()]
        if bool(scene_selection_json.get("reverse_photos")):
            photos.reverse()

        requested_indexes = scene_selection_json.get("photo_indexes")
        if isinstance(requested_indexes, list) and requested_indexes:
            selected: list[tuple[str, str]] = []
            for raw in requested_indexes:
                try:
                    index = int(raw)
                except Exception:
                    continue
                if 0 <= index < len(photos):
                    selected.append(photos[index])
            if selected:
                photos = selected

        skipped: set[int] = set()
        raw_skip = scene_selection_json.get("skip_photo_indexes")
        if isinstance(raw_skip, list):
            for raw in raw_skip:
                try:
                    skipped.add(int(raw))
                except Exception:
                    continue
        if skipped:
            photos = [entry for index, entry in enumerate(photos) if index not in skipped]

        max_photos = scene_selection_json.get("max_photos")
        try:
            max_photos_int = max(1, int(max_photos)) if max_photos is not None else 0
        except Exception:
            max_photos_int = 0
        if max_photos_int > 0:
            photos = photos[:max_photos_int]

        include_floorplans = scene_selection_json.get("include_floorplans")
        if include_floorplans is None:
            include_floorplans = scene_strategy not in {"photo_only", "compact_photo_only"}
        floorplan_position = str(scene_selection_json.get("floorplan_position") or "").strip().lower()
        if not floorplan_position:
            if scene_strategy == "layout_first":
                floorplan_position = "start"
            elif scene_strategy in {"photo_only", "compact_photo_only"}:
                floorplan_position = "omit"
            else:
                floorplan_position = "end"

        if scene_strategy == "compact" and not max_photos_int:
            photos = photos[: min(6, len(photos))]
        elif scene_strategy == "story_first" and len(photos) > 8:
            hero = photos[:1]
            body = photos[1:6]
            tail = photos[-2:]
            photos = hero + body + tail

        if not include_floorplans or floorplan_position == "omit":
            return photos
        if floorplan_position == "start":
            return floorplans + photos
        if floorplan_position == "alternate":
            combined: list[tuple[str, str]] = []
            paired = max(len(photos), len(floorplans))
            for index in range(paired):
                if index < len(photos):
                    combined.append(photos[index])
                if index < len(floorplans):
                    combined.append(floorplans[index])
            return combined
        return photos + floorplans

    @classmethod
    def _crezlo_asset_filename(cls, *, asset_url: str, ordinal: int, role: str) -> str:
        parsed = urlparse(str(asset_url or "").strip())
        candidate = Path(parsed.path).name.strip()
        if not candidate:
            candidate = f"{role}_{ordinal:02d}"
        if "." not in candidate:
            guessed = mimetypes.guess_extension(mimetypes.guess_type(parsed.path)[0] or "") or ""
            if guessed:
                candidate += guessed
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", candidate).strip("._")
        return safe or f"{role}_{ordinal:02d}"

    @classmethod
    def _crezlo_asset_mime_type(cls, *, asset_url: str, role: str) -> str:
        parsed = urlparse(str(asset_url or "").strip())
        guessed, _ = mimetypes.guess_type(parsed.path)
        if guessed:
            return guessed
        if role == "floorplan":
            return "image/png"
        return "image/jpeg"

    @classmethod
    def _crezlo_create_file_record(
        cls,
        *,
        access_token: str,
        workspace_id: str,
        name: str,
        mime_type: str,
        path: str,
        meta: dict[str, object] | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": str(name or "").strip(),
            "mime_type": str(mime_type or "").strip(),
            "path": str(path or "").strip(),
        }
        if isinstance(meta, dict) and meta:
            payload["meta"] = dict(meta)
        response = cls._crezlo_api_request(
            "POST",
            "/tours/files",
            access_token=access_token,
            payload=payload,
            query={"product_type": "tours", "workspace_id": workspace_id},
            timeout_seconds=180,
        )
        data = response.get("data")
        if not isinstance(data, dict):
            raise ToolExecutionError("crezlo_file_record_missing")
        return dict(data)

    @classmethod
    def _crezlo_create_tour(
        cls,
        *,
        access_token: str,
        workspace_id: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        response = cls._crezlo_api_request(
            "POST",
            "/tours",
            access_token=access_token,
            payload=payload,
            query={"product_type": "tours", "workspace_id": workspace_id},
            timeout_seconds=180,
        )
        data = response.get("data")
        if not isinstance(data, dict):
            raise ToolExecutionError("crezlo_tour_create_missing")
        return dict(data)

    @classmethod
    def _crezlo_create_scenes(
        cls,
        *,
        access_token: str,
        workspace_id: str,
        tour_id: str,
        scenes: list[dict[str, object]],
    ) -> dict[str, object]:
        response = cls._crezlo_api_request(
            "POST",
            f"/tours/{tour_id}/scenes",
            access_token=access_token,
            payload={"scenes": list(scenes)},
            query={"product_type": "tours", "workspace_id": workspace_id},
            timeout_seconds=180,
        )
        data = response.get("data")
        if not isinstance(data, dict):
            raise ToolExecutionError("crezlo_tour_scenes_missing")
        return dict(data)

    @classmethod
    def _create_crezlo_property_tour_direct(
        cls,
        *,
        payload: dict[str, object],
        binding_metadata: dict[str, object],
        requested_inputs: dict[str, object],
        timeout_seconds: int,
    ) -> dict[str, object]:
        workspace = cls._resolve_crezlo_workspace(payload=payload, binding_metadata=binding_metadata)
        login_email = cls._crezlo_login_email(payload, binding_metadata=binding_metadata)
        login_password = cls._crezlo_login_password(payload, binding_metadata=binding_metadata)
        remote_media_urls = cls._crezlo_normalize_url_list(payload.get("media_urls_json"))
        remote_floorplan_urls = cls._crezlo_normalize_url_list(payload.get("floorplan_urls_json"))
        scene_strategy = str(payload.get("scene_strategy") or "compact").strip().lower() or "compact"
        scene_selection_json = dict(payload.get("scene_selection_json") or {})
        selected_assets = cls._crezlo_select_asset_urls(
            media_urls=remote_media_urls,
            floorplan_urls=remote_floorplan_urls,
            scene_strategy=scene_strategy,
            scene_selection_json=scene_selection_json,
        )
        if not selected_assets:
            raise ToolExecutionError("crezlo_media_missing")
        if not login_email or not login_password:
            raise ToolExecutionError("crezlo_login_required_for_direct_create")
        access_token = cls._crezlo_login(
            login_email=login_email,
            login_password=login_password,
            timeout_seconds=min(120, timeout_seconds),
        )
        title = str(payload.get("tour_title") or requested_inputs.get("tour_title") or "").strip()
        if not title:
            raise ToolExecutionError("crezlo_tour_title_missing")
        created_tour = cls._crezlo_create_tour(
            access_token=access_token,
            workspace_id=workspace["workspace_id"],
            payload={
                "title": title,
                # Remote image records are only staging inputs.  They are not
                # an immersive tour until Crezlo has produced spatial assets
                # and the anonymous viewer has passed the governed browser
                # gate, so never publish the shell up front.
                "status": "draft",
            },
        )
        tour_id = str(created_tour.get("id") or "").strip()
        if not tour_id:
            raise ToolExecutionError("crezlo_tour_id_missing_after_create")
        slug = str(created_tour.get("slug") or "").strip()
        file_records: list[dict[str, object]] = []
        for ordinal, (role, asset_url) in enumerate(selected_assets, start=1):
            file_records.append(
                cls._crezlo_create_file_record(
                    access_token=access_token,
                    workspace_id=workspace["workspace_id"],
                    name=cls._crezlo_asset_filename(asset_url=asset_url, ordinal=ordinal, role=role),
                    mime_type=cls._crezlo_asset_mime_type(asset_url=asset_url, role=role),
                    path=asset_url,
                    meta={
                        "role": role,
                        "source_url": asset_url,
                        "property_url": str(payload.get("property_url") or requested_inputs.get("property_url") or "").strip(),
                    },
                )
            )
        cls._crezlo_create_scenes(
            access_token=access_token,
            workspace_id=workspace["workspace_id"],
            tour_id=tour_id,
            scenes=[
                {
                    "name": str(file_record.get("name") or f"scene-{ordinal}").strip(),
                    "order": ordinal - 1,
                    "file_id": str(file_record.get("id") or "").strip(),
                }
                for ordinal, file_record in enumerate(file_records, start=1)
                if str(file_record.get("id") or "").strip()
            ],
        )
        detail_json = cls._crezlo_fetch_tour_detail(
            access_token=access_token,
            workspace_id=workspace["workspace_id"],
            tour_id=tour_id,
        )
        patch_body = cls._crezlo_tour_patch_payload(detail=detail_json, payload=payload)
        if patch_body is not None:
            detail_json = cls._crezlo_update_tour(
                access_token=access_token,
                workspace_id=workspace["workspace_id"],
                tour_id=tour_id,
                body=patch_body,
            )
            if not isinstance(detail_json.get("scenes"), list):
                detail_json = cls._crezlo_fetch_tour_detail(
                    access_token=access_token,
                    workspace_id=workspace["workspace_id"],
                    tour_id=tour_id,
                )
        slug = str(detail_json.get("slug") or slug or "").strip()
        tour_status = str(detail_json.get("status") or created_tour.get("status") or "published").strip() or "published"
        share_url = ""
        editor_url = f"{workspace['workspace_base_url'].rstrip('/')}/admin/tours/{tour_id}" if workspace["workspace_base_url"] else ""
        public_url = cls._crezlo_candidate_public_url(
            workspace_domain=workspace["workspace_domain"],
            slug=slug,
        )
        scene_count = 0
        if isinstance(detail_json.get("scenes"), list):
            scene_count = len([entry for entry in detail_json.get("scenes") or [] if isinstance(entry, dict)])
        requested_url = f"crezlo://api_remote_assets/{workspace['workspace_id']}"
        return {
            "tour_id": tour_id,
            "slug": slug,
            "tour_title": str(detail_json.get("title") or created_tour.get("title") or requested_inputs.get("tour_title") or "").strip(),
            "tour_status": tour_status,
            "share_url": share_url,
            "public_url": public_url,
            "editor_url": editor_url,
            "workspace_id": workspace["workspace_id"],
            "workspace_domain": workspace["workspace_domain"],
            "workspace_base_url": workspace["workspace_base_url"],
            "scene_count": scene_count,
            "creation_mode": "crezlo_api_remote_assets",
            "selected_media_count": len(file_records),
            "scene_strategy": scene_strategy,
            "variant_key": str(payload.get("variant_key") or "").strip(),
            "requested_url": requested_url,
            "source_media_count": len(remote_media_urls),
            "source_floorplan_count": len(remote_floorplan_urls),
            "scene_selection_json": scene_selection_json,
            "file_records_json": file_records,
            "tour_detail_json": detail_json,
            "update_error": "",
        }

    @classmethod
    def _create_crezlo_property_tour_via_ui_worker(
        cls,
        *,
        payload: dict[str, object],
        binding_metadata: dict[str, object],
        requested_inputs: dict[str, object],
        timeout_seconds: int,
    ) -> dict[str, object]:
        workspace = cls._resolve_crezlo_workspace(payload=payload, binding_metadata=binding_metadata)
        packet = cls._build_crezlo_property_tour_worker_packet(
            payload=payload,
            binding_metadata=binding_metadata,
            requested_inputs=requested_inputs,
            workspace=workspace,
            timeout_seconds=timeout_seconds,
        )
        result = cls._run_crezlo_property_tour_worker(packet=packet, timeout_seconds=timeout_seconds)
        if not isinstance(result, dict):
            raise ToolExecutionError("crezlo_worker_invalid_output")
        normalized = dict(result)
        slug = str(normalized.get("slug") or "").strip()
        if slug and not str(normalized.get("public_url") or "").strip():
            normalized["public_url"] = cls._crezlo_candidate_public_url(
                workspace_domain=workspace["workspace_domain"],
                slug=slug,
            )
        normalized.update(
            cls._crezlo_ui_worker_publishable_output(
                result=normalized,
                requested_inputs=requested_inputs,
            )
        )
        normalized.setdefault("workspace_id", workspace["workspace_id"])
        normalized.setdefault("workspace_domain", workspace["workspace_domain"])
        normalized.setdefault("workspace_base_url", workspace["workspace_base_url"])
        normalized["creation_mode"] = "crezlo_ui_worker_upload"
        return normalized

    @classmethod
    def _build_crezlo_property_tour_inputs(
        cls,
        *,
        payload: dict[str, object],
        binding_metadata: dict[str, object],
    ) -> dict[str, object]:
        login_email = cls._crezlo_login_email(payload, binding_metadata=binding_metadata)
        login_password = cls._crezlo_login_password(payload, binding_metadata=binding_metadata)
        property_facts_json = dict(payload.get("property_facts_json") or {})
        media_urls = [str(value or "").strip() for value in (payload.get("media_urls_json") or []) if str(value or "").strip()]
        floorplan_urls = [
            str(value or "").strip() for value in (payload.get("floorplan_urls_json") or []) if str(value or "").strip()
        ]
        inputs: dict[str, object] = {}
        if login_email:
            inputs.update(
                {
                    "login_email": login_email,
                    "crezlo_login_email": login_email,
                    "browseract_username": login_email,
                }
            )
        if login_password:
            inputs.update(
                {
                    "login_password": login_password,
                    "crezlo_login_password": login_password,
                    "browseract_password": login_password,
                }
            )
        direct_fields = (
            "tour_title",
            "property_url",
            "source_virtual_tour_url",
            "panorama_source",
            "creative_brief",
            "variant_key",
            "language",
            "theme_name",
            "tour_style",
            "audience",
            "call_to_action",
            "workspace_id",
            "workspace_domain",
            "workspace_base_url",
            "workspace_tours_url",
            "editor_url",
            "scene_strategy",
            "display_title",
            "tour_visibility",
        )
        for field in direct_fields:
            value = payload.get(field)
            if value not in {None, ""}:
                inputs[field] = cls._browseract_safe_input_value(value)
        for field in ("scene_selection_json", "tour_settings_json", "tour_patch_json", "tour_payload_json"):
            value = payload.get(field)
            if value is not None and (not isinstance(value, str) or bool(value.strip())):
                inputs[field] = cls._browseract_safe_input_value(value)
        if media_urls:
            inputs["media_urls_json"] = media_urls
            inputs["media_urls_text"] = "\n".join(media_urls)
        if floorplan_urls:
            inputs["floorplan_urls_json"] = floorplan_urls
            inputs["floorplan_urls_text"] = "\n".join(floorplan_urls)
        floorplan_analysis_json = cls._crezlo_json_dict(
            payload.get("floorplan_analysis_json")
            or property_facts_json.get("floorplan_analysis_json")
        )
        if floorplan_analysis_json:
            inputs["floorplan_analysis_json"] = cls._browseract_safe_input_value(
                floorplan_analysis_json
            )
        inputs["proxy_result"] = bool(payload.get("proxy_result", True))
        if property_facts_json:
            inputs["property_facts_json"] = cls._browseract_safe_input_value(property_facts_json)
            summary_lines: list[str] = []
            for key in (
                "listing_title",
                "address",
                "district",
                "price_total_rent",
                "rooms",
                "area_sqm",
                "availability",
                "description",
            ):
                value = property_facts_json.get(key)
                if value in {None, ""}:
                    continue
                summary_lines.append(f"{key}: {value}")
            if summary_lines:
                inputs["property_summary_text"] = "\n".join(summary_lines)
            for key, value in property_facts_json.items():
                normalized = cls._normalize_lookup_key(key)
                if not normalized or normalized in inputs:
                    continue
                inputs[normalized] = cls._browseract_safe_input_value(value)
        runtime_inputs_json = dict(payload.get("runtime_inputs_json") or {})
        for key, value in runtime_inputs_json.items():
            normalized = str(key or "").strip()
            if not normalized:
                continue
            inputs[normalized] = cls._browseract_safe_input_value(value)
        return {
            key: value
            for key, value in inputs.items()
            if value is not None and (not isinstance(value, str) or bool(value.strip()))
        }

    @classmethod
    def _crezlo_workflow_inputs(
        cls,
        *,
        workflow_id: str,
        requested_inputs: dict[str, object],
        binding_metadata: dict[str, object],
        editor_url: object = "",
    ) -> dict[str, object]:
        """Build a schema-safe payload for a configured Crezlo workflow.

        The live ``crezlo_property_tour_operator_live`` workflow is an
        inspector for an existing editor, not a tour creator.  Sending the
        creation packet to it makes BrowserAct reject the task before a
        browser opens, so keep its three declared inputs isolated.
        """

        normalized_workflow_id = str(workflow_id or "").strip()
        workflow_kind = str(
            binding_metadata.get("crezlo_property_tour_workflow_kind")
            or binding_metadata.get("browseract_crezlo_property_tour_workflow_kind")
            or ""
        ).strip().lower()
        inspection_workflow = bool(
            normalized_workflow_id == "86048166080352916"
            or workflow_kind in {"inspect", "inspection", "inspect_existing"}
        )
        if inspection_workflow:
            resolved_editor_url = str(editor_url or requested_inputs.get("editor_url") or "").strip()
            if not resolved_editor_url:
                raise ToolExecutionError("crezlo_inspection_editor_url_missing")
            workflow_inputs = {
                "browseract_username": requested_inputs.get("browseract_username"),
                "browseract_password": requested_inputs.get("browseract_password"),
                "editor_url": resolved_editor_url,
            }
        else:
            workflow_inputs = dict(requested_inputs)
            declared_names = cls._crezlo_json_list(
                binding_metadata.get("crezlo_property_tour_workflow_input_names_json")
                or binding_metadata.get("browseract_crezlo_property_tour_workflow_input_names_json")
            )
            if declared_names:
                allowlist = {str(name or "").strip() for name in declared_names if str(name or "").strip()}
                workflow_inputs = {key: value for key, value in workflow_inputs.items() if key in allowlist}
        return {
            key: value
            for key, value in workflow_inputs.items()
            if value is not None and (not isinstance(value, str) or bool(value.strip()))
        }

    @classmethod
    def _normalize_crezlo_property_tour_payload(
        cls,
        *,
        response: dict[str, object],
        workflow_id: str,
        requested_url: str,
        requested_inputs: dict[str, object],
    ) -> dict[str, object]:
        output_json = cls._browseract_task_output(response)
        source_payload: object = output_json if cls._browseract_output_has_content(output_json) else response
        unwrapped = _unwrap_browseract_output_payload(source_payload)
        normalized_payload = dict(unwrapped) if isinstance(unwrapped, dict) else {}
        raw_text = ""
        if unwrapped is not None:
            raw_text = _extract_textish(unwrapped)
        if not raw_text:
            raw_text = _extract_textish(source_payload)
        if not normalized_payload:
            loaded = _load_jsonish(raw_text)
            if isinstance(loaded, dict):
                normalized_payload = dict(loaded)
        share_url = cls._crezlo_maybe_url(
            cls._crezlo_find_matching_scalar(
                normalized_payload or source_payload,
                markers=("share_url", "share_link", "share", "viewer_url", "tour_url", "view_url"),
            )
        )
        public_url = cls._crezlo_maybe_url(
            cls._crezlo_find_matching_scalar(
                normalized_payload or source_payload,
                markers=("public_url", "public_link", "published_url", "live_url"),
            )
        )
        hosted_url = cls._crezlo_maybe_url(
            cls._crezlo_find_matching_scalar(
                normalized_payload or source_payload,
                markers=("hosted_url",),
            )
        )
        crezlo_public_url = cls._crezlo_maybe_url(
            cls._crezlo_find_matching_scalar(
                normalized_payload or source_payload,
                markers=("crezlo_public_url", "vendor_tour_url"),
            )
        )
        editor_url = cls._crezlo_maybe_url(
            cls._crezlo_find_matching_scalar(
                normalized_payload or source_payload,
                markers=("editor_url", "edit_url", "dashboard_url", "admin_url", "builder_url"),
            )
        )
        tour_status = cls._crezlo_find_matching_scalar(
            normalized_payload or source_payload,
            markers=("tour_status", "publish_status", "status", "state"),
        )
        tour_id = cls._crezlo_find_matching_scalar(
            normalized_payload or source_payload,
            markers=("tour_id",),
        )
        slug = cls._crezlo_find_matching_scalar(
            normalized_payload or source_payload,
            markers=("slug",),
        )
        workspace_id = cls._crezlo_find_matching_scalar(
            normalized_payload or source_payload,
            markers=("workspace_id",),
        )
        workspace_domain = cls._crezlo_find_matching_scalar(
            normalized_payload or source_payload,
            markers=("workspace_domain",),
        )
        creation_mode = cls._crezlo_find_matching_scalar(
            normalized_payload or source_payload,
            markers=("creation_mode",),
        )
        scene_count_text = cls._crezlo_find_matching_scalar(
            normalized_payload or source_payload,
            markers=("scene_count",),
        )
        try:
            scene_count = int(scene_count_text) if scene_count_text else 0
        except Exception:
            scene_count = 0
        task_status = cls._browseract_task_status(response)
        if not tour_status:
            tour_status = task_status or "created"
        tour_title = cls._crezlo_find_matching_scalar(
            normalized_payload or source_payload,
            markers=("tour_title", "listing_title", "title", "name"),
        ) or str(requested_inputs.get("tour_title") or "").strip()
        task_id = ""
        try:
            task_id = cls._browseract_task_id(response)
        except Exception:
            task_id = ""
        structured_output_json = {
            "tour_title": tour_title,
            "tour_status": tour_status,
            "tour_id": tour_id,
            "slug": slug,
            "share_url": share_url,
            "public_url": public_url,
            "hosted_url": hosted_url,
            "crezlo_public_url": crezlo_public_url,
            "editor_url": editor_url,
            "workspace_id": workspace_id,
            "workspace_domain": workspace_domain,
            "creation_mode": creation_mode,
            "scene_count": scene_count,
            "workflow_id": workflow_id,
            "task_id": task_id,
            "task_status": task_status or tour_status,
            "requested_url": requested_url,
            "requested_inputs": cls._crezlo_redacted_runtime_inputs(requested_inputs),
            "workflow_output_json": normalized_payload if normalized_payload else {},
            "raw_text": raw_text,
        }
        normalized_text = "\n".join(
            line
            for line in (
                f"Tour title: {tour_title}" if tour_title else "",
                f"Tour status: {tour_status}" if tour_status else "",
                f"Tour ID: {tour_id}" if tour_id else "",
                f"Slug: {slug}" if slug else "",
                f"Share URL: {share_url}" if share_url else "",
                f"Public URL: {public_url}" if public_url else "",
                f"Hosted URL: {hosted_url}" if hosted_url else "",
                f"Crezlo URL: {crezlo_public_url}" if crezlo_public_url else "",
                f"Editor URL: {editor_url}" if editor_url else "",
                f"Requested URL: {requested_url}" if requested_url else "",
                f"Task ID: {task_id}" if task_id else "",
            )
            if line
        )
        return {
            "tour_title": tour_title,
            "tour_status": tour_status,
            "tour_id": tour_id or None,
            "slug": slug or None,
            "share_url": share_url or None,
            "public_url": public_url or None,
            "hosted_url": hosted_url or None,
            "crezlo_public_url": crezlo_public_url or None,
            "editor_url": editor_url or None,
            "workspace_id": workspace_id or None,
            "workspace_domain": workspace_domain or None,
            "creation_mode": creation_mode or None,
            "scene_count": scene_count,
            "workflow_id": workflow_id or None,
            "task_id": task_id or None,
            "requested_url": requested_url,
            "normalized_text": normalized_text or (raw_text[:500] if raw_text else ""),
            "preview_text": artifact_preview_text(normalized_text or raw_text),
            "mime_type": "application/json",
            "structured_output_json": structured_output_json,
        }

    def execute_crezlo_property_tour(self, request: ToolInvocationRequest, definition: ToolDefinition) -> ToolInvocationResult:
        payload = dict(request.payload_json or {})
        principal_id, binding = self._resolve_browseract_binding(
            request=request,
            payload=payload,
            required_input_error="connector_binding_required:browseract.crezlo_property_tour",
            required_scopes=None,
        )
        binding_metadata = dict(binding.auth_metadata_json or {})
        run_url = str(
            payload.get("run_url")
            or binding_metadata.get("crezlo_property_tour_run_url")
            or binding_metadata.get("browseract_crezlo_property_tour_run_url")
            or ""
        ).strip()
        workflow_id = str(
            payload.get("workflow_id")
            or binding_metadata.get("crezlo_property_tour_workflow_id")
            or binding_metadata.get("browseract_crezlo_property_tour_workflow_id")
            or ""
        ).strip()
        tour_title = str(payload.get("tour_title") or "").strip()
        property_url = str(payload.get("property_url") or "").strip()
        if not tour_title:
            raise ToolExecutionError("tour_title_required:browseract.crezlo_property_tour")
        if not property_url:
            raise ToolExecutionError("property_url_required:browseract.crezlo_property_tour")
        try:
            timeout_seconds = max(30, min(1800, int(payload.get("timeout_seconds") or 600)))
        except Exception:
            timeout_seconds = 600
        requested_inputs = self._build_crezlo_property_tour_inputs(
            payload=payload,
            binding_metadata=binding_metadata,
        )
        workspace = self._resolve_crezlo_workspace(payload=payload, binding_metadata=binding_metadata)
        callback = getattr(self, "_crezlo_property_tour", None)
        direct_normalized: dict[str, object] | None = None
        workflow_followup_error = ""
        force_ui_worker = bool(payload.get("force_ui_worker"))
        requested_url = (
            run_url or f"browseract://workflow/{workflow_id}"
            if (run_url or workflow_id)
            else f"crezlo://direct/{workspace['workspace_id'] or workspace['workspace_domain'] or 'workspace'}"
        )
        if callback is not None:
            maybe = callback(
                run_url=run_url,
                workflow_id=workflow_id,
                request_payload=dict(payload),
                requested_inputs=dict(requested_inputs),
            )
            if isinstance(maybe, dict):
                response = maybe
            elif not run_url and not workflow_id:
                direct_result = self._create_crezlo_property_tour_direct(
                    payload=payload,
                    binding_metadata=binding_metadata,
                    requested_inputs=requested_inputs,
                    timeout_seconds=timeout_seconds,
                )
                response = {"status": "completed", "output": {"result": direct_result}}
                direct_normalized = self._normalize_crezlo_property_tour_payload(
                    response=response,
                    workflow_id="",
                    requested_url=requested_url,
                    requested_inputs=requested_inputs,
                )
            elif workflow_id and not run_url:
                workflow_inputs = self._crezlo_workflow_inputs(
                    workflow_id=workflow_id,
                    requested_inputs=requested_inputs,
                    binding_metadata=binding_metadata,
                    editor_url=payload.get("editor_url"),
                )
                started = self._run_browseract_workflow_task_with_inputs(
                    workflow_id=workflow_id,
                    input_values=workflow_inputs,
                )
                response = self._wait_for_browseract_task(
                    task_id=self._browseract_task_id(started),
                    timeout_seconds=timeout_seconds,
                    created_stall_seconds=min(180, timeout_seconds),
                )
            else:
                response = self._post_browseract_json(
                    run_url=run_url,
                    request_payload=dict(requested_inputs),
                    timeout_seconds=timeout_seconds,
                )
        else:
            if force_ui_worker:
                direct_result = self._create_crezlo_property_tour_via_ui_worker(
                    payload=payload,
                    binding_metadata=binding_metadata,
                    requested_inputs=requested_inputs,
                    timeout_seconds=timeout_seconds,
                )
            else:
                direct_result = self._create_crezlo_property_tour_direct(
                    payload=payload,
                    binding_metadata=binding_metadata,
                    requested_inputs=requested_inputs,
                    timeout_seconds=timeout_seconds,
                )
            response = {"status": "completed", "output": {"result": direct_result}}
            direct_normalized = self._normalize_crezlo_property_tour_payload(
                response=response,
                workflow_id="",
                requested_url=requested_url,
                requested_inputs=requested_inputs,
            )
            if workflow_id or run_url:
                workflow_inputs = dict(requested_inputs)
                for key in (
                    "tour_id",
                    "slug",
                    "editor_url",
                    "public_url",
                    "share_url",
                    "workspace_id",
                    "workspace_domain",
                    "creation_mode",
                ):
                    value = direct_result.get(key)
                    if value not in {None, ""}:
                        workflow_inputs[key] = self._browseract_safe_input_value(value)
                try:
                    workflow_inputs = self._crezlo_workflow_inputs(
                        workflow_id=workflow_id,
                        requested_inputs=workflow_inputs,
                        binding_metadata=binding_metadata,
                        editor_url=direct_result.get("editor_url"),
                    )
                    requested_inputs = workflow_inputs
                    if workflow_id and not run_url:
                        started = self._run_browseract_workflow_task_with_inputs(
                            workflow_id=workflow_id,
                            input_values=workflow_inputs,
                        )
                        response = self._wait_for_browseract_task(
                            task_id=self._browseract_task_id(started),
                            timeout_seconds=timeout_seconds,
                            created_stall_seconds=min(180, timeout_seconds),
                        )
                    else:
                        response = self._post_browseract_json(
                            run_url=run_url,
                            request_payload=dict(workflow_inputs),
                            timeout_seconds=timeout_seconds,
                        )
                except ToolExecutionError as exc:
                    workflow_followup_error = str(exc).strip()
                    response = {"status": "completed", "output": {"result": direct_result}}
        normalized = self._normalize_crezlo_property_tour_payload(
            response=response,
            workflow_id=workflow_id,
            requested_url=requested_url,
            requested_inputs=requested_inputs,
        )
        if direct_normalized is not None and response is not None and (workflow_id or run_url):
            merged_structured = dict(direct_normalized.get("structured_output_json") or {})
            merged_structured.update(dict(normalized.get("structured_output_json") or {}))
            merged_structured["direct_create_json"] = dict(direct_normalized.get("structured_output_json") or {})
            for key in ("tour_title", "tour_status", "tour_id", "slug", "share_url", "public_url", "editor_url", "workspace_id", "workspace_domain", "creation_mode", "scene_count"):
                if normalized.get(key) in {None, "", 0} and direct_normalized.get(key) not in {None, ""}:
                    normalized[key] = direct_normalized.get(key)
            if normalized.get("requested_url") in {None, ""}:
                normalized["requested_url"] = direct_normalized.get("requested_url")
            if not str(normalized.get("normalized_text") or "").strip():
                normalized["normalized_text"] = direct_normalized.get("normalized_text")
                normalized["preview_text"] = direct_normalized.get("preview_text")
            normalized["structured_output_json"] = merged_structured
        if workflow_followup_error:
            structured = dict(normalized.get("structured_output_json") or {})
            structured["workflow_followup_error"] = workflow_followup_error
            structured["workflow_followup_status"] = "failed"
            normalized["structured_output_json"] = structured
            base_text = str(normalized.get("normalized_text") or "").strip()
            if base_text:
                normalized["normalized_text"] = f"{base_text}\nWorkflow follow-up error: {workflow_followup_error}"
                normalized["preview_text"] = artifact_preview_text(normalized["normalized_text"])
        acceptance = self._crezlo_immersive_acceptance(normalized)
        structured = dict(normalized.get("structured_output_json") or {})
        structured["immersive_acceptance_json"] = acceptance
        vendor_public_url = str(normalized.get("public_url") or "").strip()
        if vendor_public_url:
            normalized["crezlo_public_url"] = vendor_public_url
            structured["crezlo_public_url"] = vendor_public_url
        hosted_url = ""
        if acceptance.get("accepted") is True:
            hosted_url = str(acceptance.get("first_party_public_url") or "").strip()
            normalized["hosted_url"] = hosted_url or None
            normalized["public_url"] = hosted_url or None
            structured["hosted_url"] = hosted_url
            structured["public_url"] = hosted_url
        else:
            normalized["tour_status"] = "blocked"
            normalized["share_url"] = None
            normalized["hosted_url"] = None
            normalized["public_url"] = None
            structured["quality_gate_status"] = "blocked"
            structured["quality_gate_reason"] = str(acceptance.get("reason") or "crezlo_immersive_evidence_missing")
        normalized["structured_output_json"] = structured
        normalized["normalized_text"] = "\n".join(
            line
            for line in (
                f"Tour title: {normalized.get('tour_title')}" if normalized.get("tour_title") else "",
                f"Tour status: {normalized.get('tour_status')}" if normalized.get("tour_status") else "",
                f"Tour ID: {normalized.get('tour_id')}" if normalized.get("tour_id") else "",
                f"Slug: {normalized.get('slug')}" if normalized.get("slug") else "",
                f"Hosted URL: {hosted_url}" if hosted_url else "",
                (
                    f"Immersive quality gate: {acceptance.get('reason')}"
                    if acceptance.get("accepted") is not True
                    else "Immersive quality gate: pass"
                ),
                f"Editor URL: {normalized.get('editor_url')}" if normalized.get("editor_url") else "",
                f"Requested URL: {normalized.get('requested_url')}" if normalized.get("requested_url") else "",
                f"Task ID: {normalized.get('task_id')}" if normalized.get("task_id") else "",
            )
            if line
        )
        normalized["preview_text"] = artifact_preview_text(normalized["normalized_text"])
        action_kind = str(request.action_kind or "property_tour.create") or "property_tour.create"
        target_ref = str(normalized.get("share_url") or normalized.get("public_url") or normalized.get("editor_url") or "")
        if not target_ref:
            target_ref = f"browseract:{binding.binding_id}:crezlo_property_tour:{self._slugify(tour_title)}"
        return ToolInvocationResult(
            tool_name=definition.tool_name,
            action_kind=action_kind,
            target_ref=target_ref,
            output_json={
                "binding_id": binding.binding_id,
                "connector_name": binding.connector_name,
                "external_account_ref": binding.external_account_ref,
                "tour_title": normalized.get("tour_title"),
                "tour_status": normalized.get("tour_status"),
                "tour_id": normalized.get("tour_id"),
                "slug": normalized.get("slug"),
                "share_url": normalized.get("share_url"),
                "editor_url": normalized.get("editor_url"),
                "public_url": normalized.get("public_url"),
                "hosted_url": normalized.get("hosted_url"),
                "crezlo_public_url": normalized.get("crezlo_public_url"),
                "workspace_id": normalized.get("workspace_id"),
                "workspace_domain": normalized.get("workspace_domain"),
                "creation_mode": normalized.get("creation_mode"),
                "scene_count": normalized.get("scene_count"),
                "workflow_id": normalized.get("workflow_id"),
                "task_id": normalized.get("task_id"),
                "requested_url": normalized.get("requested_url"),
                "normalized_text": normalized.get("normalized_text"),
                "preview_text": normalized.get("preview_text"),
                "mime_type": normalized.get("mime_type"),
                "structured_output_json": normalized.get("structured_output_json"),
                "tool_name": definition.tool_name,
                "action_kind": action_kind,
            },
            receipt_json={
                "binding_id": binding.binding_id,
                "connector_name": binding.connector_name,
                "external_account_ref": binding.external_account_ref,
                "principal_id": principal_id,
                "handler_key": definition.tool_name,
                "invocation_contract": "tool.v1",
                "tool_version": definition.version,
                "tool_name": definition.tool_name,
                "action_kind": action_kind,
                "requested_url": normalized.get("requested_url"),
                "workflow_id": normalized.get("workflow_id"),
                "task_id": normalized.get("task_id"),
                "tour_title": normalized.get("tour_title"),
                "tour_status": normalized.get("tour_status"),
                "tour_id": normalized.get("tour_id"),
                "slug": normalized.get("slug"),
            },
        )

    def execute_ui_service(
        self,
        request: ToolInvocationRequest,
        definition: ToolDefinition,
        *,
        service: BrowserActUiServiceDefinition | None = None,
    ) -> ToolInvocationResult:
        resolved_service = service or browseract_ui_service_by_tool(definition.tool_name) or browseract_ui_service_by_tool(request.tool_name)
        if resolved_service is None:
            raise ToolExecutionError(f"browseract_ui_service_unknown:{definition.tool_name}")
        payload = dict(request.payload_json or {})
        principal_id, binding = self._resolve_browseract_binding(
            request=request,
            payload=payload,
            required_input_error=f"connector_binding_required:{resolved_service.tool_name}",
            required_scopes=None,
        )
        binding_metadata = dict(binding.auth_metadata_json or {})
        callback = (
            self._ui_service_callbacks.get(resolved_service.service_key)
            or self._ui_service_callbacks.get(resolved_service.tool_name)
        )
        if callback is None and resolved_service.template_key:
            callback = self._create_template_backed_ui_service_direct
        run_url, workflow_id = self._resolve_browseract_ui_service_target(
            payload=payload,
            binding_metadata=binding_metadata,
            service=resolved_service,
        )
        if not run_url and not workflow_id and (
            callback is None
            or (bool(payload.get("force_browseract")) and not resolved_service.template_key)
        ):
            raise ToolExecutionError(f"run_url_or_workflow_id_required:{resolved_service.tool_name}")
        requested_inputs = self._build_browseract_ui_runtime_inputs(
            payload=payload,
            service=resolved_service,
        )
        requested_inputs.update(
            self._browseract_ui_service_runtime_credentials(
                payload=payload,
                binding_metadata=binding_metadata,
                service=resolved_service,
            )
        )
        try:
            timeout_seconds = max(30, min(1800, int(payload.get("timeout_seconds") or 900)))
        except Exception:
            timeout_seconds = 900
        requested_url = run_url or (f"browseract://workflow/{workflow_id}" if workflow_id else "")
        if not requested_url and resolved_service.template_key:
            requested_url = f"browseract-template://{resolved_service.template_key}"
        if callback is not None:
            maybe = callback(
                run_url=run_url,
                workflow_id=workflow_id,
                request_payload=dict(payload),
                requested_inputs=dict(requested_inputs),
                binding_metadata=dict(binding_metadata),
                service=resolved_service,
            )
            if isinstance(maybe, dict):
                response = maybe
            elif workflow_id and not run_url:
                started = self._run_browseract_workflow_task_with_inputs(
                    workflow_id=workflow_id,
                    input_values=requested_inputs,
                )
                response = self._wait_for_browseract_task(
                    task_id=self._browseract_task_id(started),
                    timeout_seconds=timeout_seconds,
                    created_stall_seconds=min(180, timeout_seconds),
                )
            else:
                response = self._post_browseract_json(
                    run_url=run_url,
                    request_payload=dict(requested_inputs),
                    timeout_seconds=timeout_seconds,
                )
        elif workflow_id and not run_url:
            started = self._run_browseract_workflow_task_with_inputs(
                workflow_id=workflow_id,
                input_values=requested_inputs,
            )
            response = self._wait_for_browseract_task(
                task_id=self._browseract_task_id(started),
                timeout_seconds=timeout_seconds,
                created_stall_seconds=min(180, timeout_seconds),
            )
        else:
            response = self._post_browseract_json(
                run_url=run_url,
                request_payload=dict(requested_inputs),
                timeout_seconds=timeout_seconds,
            )
        self._raise_for_ui_lane_failure(payload=response, backend=resolved_service.service_key)
        normalized = self._normalize_browseract_ui_service_payload(
            service=resolved_service,
            response=response,
            workflow_id=workflow_id,
            requested_url=requested_url,
            requested_inputs=requested_inputs,
            result_title=self._browseract_service_result_title(payload=payload, service=resolved_service),
        )
        action_kind = str(request.action_kind or resolved_service.action_kind) or resolved_service.action_kind
        target_ref = str(
            normalized.get("public_url")
            or normalized.get("download_url")
            or normalized.get("asset_url")
            or normalized.get("editor_url")
            or ""
        )
        if not target_ref:
            target_ref = f"browseract:{binding.binding_id}:{resolved_service.service_key}:{self._slugify(str(normalized.get('result_title') or resolved_service.service_key))}"
        return ToolInvocationResult(
            tool_name=definition.tool_name,
            action_kind=action_kind,
            target_ref=target_ref,
            output_json={
                "binding_id": binding.binding_id,
                "connector_name": binding.connector_name,
                "external_account_ref": binding.external_account_ref,
                "service_key": normalized.get("service_key"),
                "deliverable_type": resolved_service.deliverable_type,
                "result_title": normalized.get("result_title"),
                "render_status": normalized.get("render_status"),
                "asset_url": normalized.get("asset_url"),
                "download_url": normalized.get("download_url"),
                "public_url": normalized.get("public_url"),
                "editor_url": normalized.get("editor_url"),
                "asset_urls": list(normalized.get("asset_urls") or []),
                "workflow_id": normalized.get("workflow_id"),
                "task_id": normalized.get("task_id"),
                "requested_url": normalized.get("requested_url"),
                "normalized_text": normalized.get("normalized_text"),
                "preview_text": normalized.get("preview_text"),
                "mime_type": normalized.get("mime_type"),
                "structured_output_json": normalized.get("structured_output_json"),
                "tool_name": definition.tool_name,
                "action_kind": action_kind,
            },
            receipt_json={
                "binding_id": binding.binding_id,
                "connector_name": binding.connector_name,
                "external_account_ref": binding.external_account_ref,
                "principal_id": principal_id,
                "handler_key": definition.tool_name,
                "invocation_contract": "tool.v1",
                "tool_version": definition.version,
                "tool_name": definition.tool_name,
                "action_kind": action_kind,
                "service_key": resolved_service.service_key,
                "requested_url": normalized.get("requested_url"),
                "workflow_id": normalized.get("workflow_id"),
                "task_id": normalized.get("task_id"),
                "render_status": normalized.get("render_status"),
                "asset_url": normalized.get("asset_url"),
                "public_url": normalized.get("public_url"),
            },
        )

    def execute_build_workflow_spec(self, request: ToolInvocationRequest, definition: ToolDefinition) -> ToolInvocationResult:
        payload = dict(request.payload_json or {})
        workflow_name = str(payload.get("workflow_name") or "").strip()
        purpose = str(payload.get("purpose") or "").strip()
        login_url = str(payload.get("login_url") or "").strip()
        tool_url = str(payload.get("tool_url") or "").strip()
        if not workflow_name:
            raise ToolExecutionError("workflow_name_required:browseract.build_workflow_spec")
        if not purpose:
            raise ToolExecutionError("purpose_required:browseract.build_workflow_spec")
        if not login_url:
            raise ToolExecutionError("login_url_required:browseract.build_workflow_spec")
        if not tool_url:
            raise ToolExecutionError("tool_url_required:browseract.build_workflow_spec")
        workflow_kind = str(payload.get("workflow_kind") or "prompt_tool").strip().lower() or "prompt_tool"
        if workflow_kind not in {"prompt_tool", "page_extract"}:
            raise ToolExecutionError(f"workflow_kind_invalid:browseract.build_workflow_spec:{workflow_kind}")
        runtime_input_name = str(payload.get("runtime_input_name") or "").strip()
        prompt_selector = str(payload.get("prompt_selector") or "textarea").strip() or "textarea"
        submit_selector = str(payload.get("submit_selector") or "button").strip() or "button"
        result_selector = str(payload.get("result_selector") or "main, body").strip() or "main, body"
        wait_selector = str(payload.get("wait_selector") or result_selector).strip() or result_selector
        title_selector = str(payload.get("title_selector") or "").strip()
        result_field_name = str(payload.get("result_field_name") or ("page_body" if workflow_kind == "page_extract" else "result_text")).strip() or ("page_body" if workflow_kind == "page_extract" else "result_text")
        dismiss_selectors = self._normalize_string_list(payload.get("dismiss_selectors"))
        output_dir = str(payload.get("output_dir") or "/docker/fleet/state/browseract_bootstrap").strip() or "/docker/fleet/state/browseract_bootstrap"
        explicit_spec = payload.get("workflow_spec_json") if isinstance(payload.get("workflow_spec_json"), dict) else None
        if explicit_spec is not None:
            spec = self._normalize_explicit_workflow_spec(
                raw_spec=explicit_spec,
                workflow_name=workflow_name,
                purpose=purpose,
                workflow_kind=workflow_kind,
                output_dir=output_dir,
            )
        else:
            spec = self._build_workflow_spec(
                workflow_name=workflow_name,
                purpose=purpose,
                login_url=login_url,
                tool_url=tool_url,
                workflow_kind=workflow_kind,
                runtime_input_name=runtime_input_name,
                prompt_selector=prompt_selector,
                submit_selector=submit_selector,
                result_selector=result_selector,
                wait_selector=wait_selector,
                title_selector=title_selector,
                dismiss_selectors=dismiss_selectors,
                result_field_name=result_field_name,
                output_dir=output_dir,
            )
        slug = str(((spec.get("meta") or {}).get("slug")) or self._slugify(workflow_name))
        action_kind = str(request.action_kind or "workflow.spec_build") or "workflow.spec_build"
        normalized_text = "\n".join(
            [
                f"Workflow: {workflow_name}",
                f"Purpose: {purpose}",
                f"Kind: {workflow_kind}",
                f"Tool URL: {tool_url}",
                f"Runtime input: {runtime_input_name or '<none>'}",
                f"Prompt selector: {prompt_selector}",
                f"Submit selector: {submit_selector}",
                f"Result selector: {result_selector}",
                f"Wait selector: {wait_selector}",
                f"Title selector: {title_selector or '<none>'}",
                f"Dismiss selectors: {len(dismiss_selectors)}",
                f"Node count: {len(spec.get('nodes') or [])}",
                f"Edge count: {len(spec.get('edges') or [])}",
            ]
        )
        return ToolInvocationResult(
            tool_name=definition.tool_name,
            action_kind=action_kind,
            target_ref=f"browseract:workflow-spec:{slug}",
            output_json={
                "workflow_name": workflow_name,
                "workflow_slug": slug,
                "normalized_text": normalized_text,
                "preview_text": artifact_preview_text(normalized_text),
                "mime_type": "application/json",
                "structured_output_json": spec,
                "tool_name": definition.tool_name,
                "action_kind": action_kind,
            },
            receipt_json={
                "handler_key": definition.tool_name,
                "invocation_contract": "tool.v1",
                "workflow_name": workflow_name,
                "workflow_slug": slug,
                "tool_version": definition.version,
            },
        )

    def execute_repair_workflow_spec(self, request: ToolInvocationRequest, definition: ToolDefinition) -> ToolInvocationResult:
        payload = dict(request.payload_json or {})
        workflow_name = str(payload.get("workflow_name") or "").strip()
        purpose = str(payload.get("purpose") or "").strip()
        login_url = str(payload.get("login_url") or "public").strip() or "public"
        tool_url = str(payload.get("tool_url") or "").strip()
        failure_summary = str(payload.get("failure_summary") or payload.get("diagnosis") or "").strip()
        if not workflow_name:
            raise ToolExecutionError("workflow_name_required:browseract.repair_workflow_spec")
        if not purpose:
            raise ToolExecutionError("purpose_required:browseract.repair_workflow_spec")
        if not tool_url:
            raise ToolExecutionError("tool_url_required:browseract.repair_workflow_spec")
        if not failure_summary:
            raise ToolExecutionError("failure_summary_required:browseract.repair_workflow_spec")
        prompt_selector = str(payload.get("prompt_selector") or "textarea").strip() or "textarea"
        submit_selector = str(payload.get("submit_selector") or "button").strip() or "button"
        result_selector = str(payload.get("result_selector") or "main, body").strip() or "main, body"
        workflow_kind = str(payload.get("workflow_kind") or "prompt_tool").strip().lower() or "prompt_tool"
        runtime_input_name = str(payload.get("runtime_input_name") or "prompt").strip() or "prompt"
        wait_selector = str(payload.get("wait_selector") or result_selector).strip() or result_selector
        title_selector = str(payload.get("title_selector") or "").strip()
        result_field_name = str(payload.get("result_field_name") or ("page_body" if workflow_kind == "page_extract" else "result_text")).strip() or ("page_body" if workflow_kind == "page_extract" else "result_text")
        dismiss_selectors = self._normalize_string_list(payload.get("dismiss_selectors"))
        output_dir = str(payload.get("output_dir") or "/docker/fleet/state/browseract_bootstrap").strip() or "/docker/fleet/state/browseract_bootstrap"
        scaffold = self._build_workflow_spec(
            workflow_name=workflow_name,
            purpose=purpose,
            login_url=login_url,
            tool_url=tool_url,
            workflow_kind=workflow_kind,
            runtime_input_name=runtime_input_name,
            prompt_selector=prompt_selector,
            submit_selector=submit_selector,
            result_selector=result_selector,
            wait_selector=wait_selector,
            title_selector=title_selector,
            dismiss_selectors=dismiss_selectors,
            result_field_name=result_field_name,
            output_dir=output_dir,
        )
        failure_goals = self._normalize_string_list(payload.get("failing_step_goals"))
        current_spec = payload.get("current_workflow_spec_json") if isinstance(payload.get("current_workflow_spec_json"), dict) else {}
        repair_prompt = self._build_workflow_repair_prompt(
            workflow_name=workflow_name,
            purpose=purpose,
            login_url=login_url,
            tool_url=tool_url,
            failure_summary=failure_summary,
            failure_goals=failure_goals,
            current_spec=current_spec if isinstance(current_spec, dict) else {},
            scaffold=scaffold,
        )
        envelope, model = self._run_gemini_repair_prompt(repair_prompt)
        packet = self._normalize_workflow_repair_packet(
            envelope,
            workflow_name=workflow_name,
            purpose=purpose,
            scaffold=scaffold,
            failure_summary=failure_summary,
            failure_goals=failure_goals,
        )
        slug = str((((packet.get("workflow_spec") or {}).get("meta") or {}).get("slug")) or self._slugify(workflow_name))
        normalized_text = "\n".join(
            [
                f"Workflow: {workflow_name}",
                f"Failure: {failure_summary}",
                f"Diagnosis: {packet.get('diagnosis', '')}",
                f"Repair strategy: {packet.get('repair_strategy', '')}",
                f"Node count: {len(((packet.get('workflow_spec') or {}).get('nodes') or []))}",
                f"Edge count: {len(((packet.get('workflow_spec') or {}).get('edges') or []))}",
            ]
        )
        action_kind = str(request.action_kind or "workflow.spec_repair") or "workflow.spec_repair"
        return ToolInvocationResult(
            tool_name=definition.tool_name,
            action_kind=action_kind,
            target_ref=f"browseract:workflow-repair:{slug}:{uuid.uuid4()}",
            output_json={
                "workflow_name": workflow_name,
                "workflow_slug": slug,
                "normalized_text": normalized_text,
                "preview_text": artifact_preview_text(normalized_text),
                "mime_type": "application/json",
                "structured_output_json": packet,
                "tool_name": definition.tool_name,
                "action_kind": action_kind,
            },
            receipt_json={
                "handler_key": definition.tool_name,
                "invocation_contract": "tool.v1",
                "workflow_name": workflow_name,
                "workflow_slug": slug,
                "failure_summary": failure_summary,
                "failure_goals": failure_goals,
                "model": model,
                "tool_version": definition.version,
            },
            model_name=model,
            cost_usd=0.0,
        )

    def _resolve_browseract_binding(
        self,
        *,
        request: ToolInvocationRequest,
        payload: dict[str, object],
        required_input_error: str,
        required_scopes: tuple[str, ...] | None,
    ):
        principal_id, binding = self._connector_dispatch.resolve_connector_binding(
            request=request,
            payload=payload,
            required_connector_name="browseract",
            required_input_error=required_input_error,
        )
        requested_scopes = self._connector_dispatch.normalised_scopes(required_scopes or ())
        if requested_scopes:
            configured_scopes = self._connector_dispatch.normalised_scopes(
                self._configured_service_names(
                    binding_auth_metadata_json=dict(binding.auth_metadata_json or {}),
                    binding_scope_json=dict(binding.scope_json or {}),
                )
            )
            if not set(requested_scopes).issubset(set(configured_scopes)):
                raise ToolExecutionError(
                    f"connector_binding_scope_mismatch:{binding.binding_id}:{','.join(requested_scopes)}"
                )
        return principal_id, binding

    def _requested_fields(self, payload: dict[str, object]) -> tuple[str, ...]:
        raw = payload.get("requested_fields")
        if isinstance(raw, (list, tuple)):
            return tuple(str(value or "").strip() for value in raw if str(value or "").strip())
        if isinstance(raw, str) and raw.strip():
            return tuple(value.strip() for value in raw.split(",") if value.strip())
        return ()

    def _requested_service_names(self, payload: dict[str, object]) -> tuple[str, ...]:
        raw = payload.get("service_names")
        values: list[str] = []
        if isinstance(raw, (list, tuple)):
            values.extend(str(value or "").strip() for value in raw if str(value or "").strip())
        elif isinstance(raw, str) and raw.strip():
            values.extend(value.strip() for value in raw.split(",") if value.strip())
        if not values:
            single = str(payload.get("service_name") or "").strip()
            if single:
                values.append(single)
        ordered: list[str] = []
        seen: set[str] = set()
        for value in values:
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            ordered.append(value)
        return tuple(ordered)

    def _configured_service_names(
        self,
        *,
        binding_auth_metadata_json: dict[str, object],
        binding_scope_json: dict[str, object],
    ) -> tuple[str, ...]:
        return configured_browseract_service_names(
            binding_auth_metadata_json=binding_auth_metadata_json,
            binding_scope_json=binding_scope_json,
        )

    def _slugify(self, value: str) -> str:
        cleaned = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(value or ""))
        while "__" in cleaned:
            cleaned = cleaned.replace("__", "_")
        return cleaned.strip("_") or "adapter"

    def _build_workflow_spec(
        self,
        *,
        workflow_name: str,
        purpose: str,
        login_url: str,
        tool_url: str,
        workflow_kind: str,
        runtime_input_name: str,
        prompt_selector: str,
        submit_selector: str,
        result_selector: str,
        wait_selector: str,
        title_selector: str,
        dismiss_selectors: list[str],
        result_field_name: str,
        output_dir: str,
    ) -> dict[str, object]:
        slug = self._slugify(workflow_name)
        nodes: list[dict[str, object]] = []
        edges: list[list[str]] = []
        inputs: list[dict[str, str]] = []
        if login_url.lower() not in {"", "none", "public", "noauth"}:
            nodes.extend(
                [
                    {"id": "open_login", "type": "visit_page", "label": "Open Login", "config": {"url": login_url}},
                    {"id": "email", "type": "input_text", "label": "Email", "config": {"selector": "input[type=email]", "value_from_secret": "browseract_username"}},
                    {"id": "password", "type": "input_text", "label": "Password", "config": {"selector": "input[type=password]", "value_from_secret": "browseract_password"}},
                    {"id": "submit", "type": "click", "label": "Submit", "config": {"selector": "button[type=submit]"}},
                    {"id": "wait_dashboard", "type": "wait", "label": "Wait Dashboard", "config": {"selector": "body"}},
                ]
            )
            edges.extend(
                [
                    ["open_login", "email"],
                    ["email", "password"],
                    ["password", "submit"],
                    ["submit", "wait_dashboard"],
                    ["wait_dashboard", "open_tool"],
                ]
            )
        if workflow_kind == "page_extract":
            visit_config: dict[str, str] = {"url": tool_url}
            if runtime_input_name:
                visit_config = {"value_from_input": runtime_input_name}
                inputs.append(
                    {
                        "name": runtime_input_name,
                        "description": f"Target page URL for {workflow_name}.",
                    }
                )
            nodes.append({"id": "open_tool", "type": "visit_page", "label": "Open Target Page", "config": visit_config})
            last_node = "open_tool"
            for index, selector in enumerate(dismiss_selectors, start=1):
                node_id = f"dismiss_{index:02d}"
                nodes.append(
                    {
                        "id": node_id,
                        "type": "click",
                        "label": f"Dismiss Overlay {index}",
                        "config": {"selector": selector},
                    }
                )
                edges.append([last_node, node_id])
                last_node = node_id
            nodes.append({"id": "wait_content", "type": "wait", "label": "Wait Content", "config": {"selector": wait_selector}})
            edges.append([last_node, "wait_content"])
            last_node = "wait_content"
            if title_selector:
                nodes.append({"id": "extract_title", "type": "extract", "label": "Extract Title", "config": {"selector": title_selector}})
                edges.append([last_node, "extract_title"])
                last_node = "extract_title"
            nodes.append(
                {
                    "id": "extract_result",
                    "type": "extract",
                    "label": "Extract Result",
                    "config": {"selector": result_selector, "field_name": result_field_name, "mode": "text"},
                }
            )
            edges.append([last_node, "extract_result"])
            nodes.append(
                {
                    "id": "output_result",
                    "type": "output",
                    "label": "Output Result",
                    "config": {
                        "description": f"Publish the {result_field_name} field as the workflow output for API callers.",
                        "field_name": result_field_name,
                    },
                }
            )
            edges.append(["extract_result", "output_result"])
        else:
            inputs.append(
                {
                    "name": "prompt",
                    "description": f"Primary runtime prompt for {workflow_name}.",
                }
            )
            nodes.extend(
                [
                    {"id": "open_tool", "type": "visit_page", "label": "Open Tool", "config": {"url": tool_url}},
                    {"id": "input_prompt", "type": "input_text", "label": "Input Prompt", "config": {"selector": prompt_selector, "value_from_input": "prompt"}},
                    {"id": "generate", "type": "click", "label": "Generate", "config": {"selector": submit_selector}},
                    {
                        "id": "wait_result",
                        "type": "wait",
                        "label": "Wait Result",
                        "config": {
                            "selector": wait_selector,
                            "description": f"Wait until the result target {wait_selector} is visible and ready after submission.",
                            "timeout_ms": 60000,
                        },
                    },
                    {
                        "id": "extract_result",
                        "type": "extract",
                        "label": "Extract Result",
                        "config": {"selector": result_selector, "field_name": result_field_name, "mode": "text"},
                    },
                    {
                        "id": "output_result",
                        "type": "output",
                        "label": "Output Result",
                        "config": {
                            "description": f"Publish the {result_field_name} field as the workflow output for API callers.",
                            "field_name": result_field_name,
                        },
                    },
                ]
            )
            edges.extend(
                [
                    ["open_tool", "input_prompt"],
                    ["input_prompt", "generate"],
                    ["generate", "wait_result"],
                    ["wait_result", "extract_result"],
                    ["extract_result", "output_result"],
                ]
            )
        return {
            "workflow_name": workflow_name,
            "description": purpose,
            "publish": True,
            "mcp_ready": False,
            "inputs": inputs,
            "nodes": nodes,
            "edges": edges,
            "meta": {
                "slug": slug,
                "output_dir": output_dir,
                "status": "pending_browseract_seed",
                "workflow_kind": workflow_kind,
            },
        }

    def _normalize_explicit_workflow_spec(
        self,
        *,
        raw_spec: dict[str, object],
        workflow_name: str,
        purpose: str,
        workflow_kind: str,
        output_dir: str,
    ) -> dict[str, object]:
        nodes = raw_spec.get("nodes")
        edges = raw_spec.get("edges")
        if not isinstance(nodes, list) or not nodes:
            raise ToolExecutionError("workflow_nodes_required:browseract.build_workflow_spec")
        if not isinstance(edges, list) or not edges:
            raise ToolExecutionError("workflow_edges_required:browseract.build_workflow_spec")

        normalized_nodes: list[dict[str, object]] = []
        for index, entry in enumerate(nodes, start=1):
            if not isinstance(entry, dict):
                raise ToolExecutionError("workflow_node_invalid:browseract.build_workflow_spec")
            node_type = str(entry.get("type") or "").strip().lower()
            if not node_type:
                raise ToolExecutionError("workflow_node_type_required:browseract.build_workflow_spec")
            normalized_nodes.append(
                {
                    "id": str(entry.get("id") or f"node_{index:02d}").strip() or f"node_{index:02d}",
                    "label": str(entry.get("label") or f"Step {index}").strip() or f"Step {index}",
                    "type": node_type,
                    "config": dict(entry.get("config") or {}),
                }
            )

        normalized_edges: list[list[str]] = []
        for entry in edges:
            source = ""
            target = ""
            if isinstance(entry, dict):
                source = str(entry.get("source") or "").strip()
                target = str(entry.get("target") or "").strip()
            elif isinstance(entry, list) and len(entry) == 2:
                source = str(entry[0] or "").strip()
                target = str(entry[1] or "").strip()
            if not source or not target:
                raise ToolExecutionError("workflow_edge_invalid:browseract.build_workflow_spec")
            normalized_edges.append([source, target])

        normalized_inputs: list[dict[str, str]] = []
        seen_inputs: set[str] = set()

        def add_input(name: object, *, description: object = "", default_value: object = "") -> None:
            normalized_name = str(name or "").strip()
            if not normalized_name:
                return
            key = normalized_name.casefold()
            if key in seen_inputs:
                return
            seen_inputs.add(key)
            entry: dict[str, str] = {
                "name": normalized_name,
                "description": str(description or "").strip(),
            }
            default_text = str(default_value or "").strip()
            if default_text:
                entry["default_value"] = default_text
            normalized_inputs.append(entry)

        raw_inputs = raw_spec.get("inputs")
        if not isinstance(raw_inputs, list):
            raw_inputs = raw_spec.get("input_parameters")
        if isinstance(raw_inputs, list):
            for entry in raw_inputs:
                if isinstance(entry, dict):
                    add_input(
                        entry.get("name") or entry.get("key") or entry.get("id"),
                        description=entry.get("description") or entry.get("label"),
                        default_value=entry.get("default_value") or entry.get("default") or entry.get("value"),
                    )
                elif isinstance(entry, str):
                    add_input(entry)
        for node in normalized_nodes:
            config = dict(node.get("config") or {})
            add_input(config.get("value_from_input"), description=config.get("description") or f"Runtime input for {node['label']}.")
            add_input(config.get("value_from_secret"), description=config.get("description") or f"Secret input for {node['label']}.")

        meta = dict(raw_spec.get("meta") or {})
        meta["slug"] = str(meta.get("slug") or self._slugify(workflow_name)).strip() or self._slugify(workflow_name)
        meta["output_dir"] = str(meta.get("output_dir") or output_dir).strip() or output_dir
        meta["status"] = str(meta.get("status") or "pending_browseract_seed").strip() or "pending_browseract_seed"
        meta["workflow_kind"] = str(meta.get("workflow_kind") or workflow_kind).strip() or workflow_kind

        return {
            "workflow_name": str(raw_spec.get("workflow_name") or workflow_name).strip() or workflow_name,
            "description": str(raw_spec.get("description") or purpose).strip() or purpose,
            "publish": bool(raw_spec.get("publish", True)),
            "mcp_ready": bool(raw_spec.get("mcp_ready", False)),
            "inputs": normalized_inputs,
            "nodes": normalized_nodes,
            "edges": normalized_edges,
            "meta": meta,
        }

    def _normalize_string_list(self, raw: object) -> list[str]:
        values: list[str] = []
        if isinstance(raw, (list, tuple, set)):
            values.extend(str(value or "").strip() for value in raw if str(value or "").strip())
        elif isinstance(raw, str) and raw.strip():
            values.extend(part.strip() for part in raw.split("|") if part.strip())
        deduped: list[str] = []
        seen: set[str] = set()
        for value in values:
            key = value.casefold()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(value)
        return deduped

    def _gemini_command_base(self) -> list[str]:
        raw = str(os.environ.get("EA_GEMINI_VORTEX_COMMAND") or "gemini").strip() or "gemini"
        return shlex.split(raw)

    def _gemini_model(self) -> str:
        return str(os.environ.get("EA_GEMINI_VORTEX_MODEL") or "gemini-2.5-flash").strip() or "gemini-2.5-flash"

    def _gemini_timeout_seconds(self) -> int:
        raw = str(os.environ.get("EA_GEMINI_VORTEX_TIMEOUT_SECONDS") or "180").strip() or "180"
        try:
            return max(15, int(raw))
        except Exception:
            return 180

    def _strip_fences(self, text: str) -> str:
        raw = str(text or "").strip()
        if raw.startswith("```"):
            raw = raw.removeprefix("```json").removeprefix("```").strip()
        if raw.endswith("```"):
            raw = raw[:-3].strip()
        return raw

    def _run_gemini_repair_prompt(self, prompt: str) -> tuple[dict[str, object], str]:
        model = self._gemini_model()
        command = self._gemini_command_base() + [
            "-p",
            prompt,
            "--output-format",
            "json",
            "--approval-mode",
            "yolo",
        ]
        if model:
            command.extend(["-m", model])
        try:
            completed = subprocess.run(
                command,
                check=True,
                text=True,
                capture_output=True,
                timeout=self._gemini_timeout_seconds(),
            )
        except FileNotFoundError as exc:
            raise ToolExecutionError("gemini_vortex_cli_missing:browseract.repair_workflow_spec") from exc
        except subprocess.TimeoutExpired as exc:
            raise ToolExecutionError("gemini_vortex_timeout:browseract.repair_workflow_spec") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()
            raise ToolExecutionError(f"gemini_vortex_failed:browseract.repair_workflow_spec:{detail[:400]}") from exc
        raw = str(completed.stdout or "").strip()
        if not raw:
            raise ToolExecutionError("gemini_vortex_empty_output:browseract.repair_workflow_spec")
        try:
            envelope = json.loads(raw)
        except Exception:
            envelope = {"response": raw}
        response = envelope.get("response") if isinstance(envelope, dict) else raw
        cleaned = self._strip_fences(str(response or raw))
        try:
            loaded = json.loads(cleaned)
        except Exception as exc:
            raise ToolExecutionError("gemini_vortex_non_json:browseract.repair_workflow_spec") from exc
        if not isinstance(loaded, dict):
            raise ToolExecutionError("gemini_vortex_non_object:browseract.repair_workflow_spec")
        return loaded, model

    def _build_workflow_repair_prompt(
        self,
        *,
        workflow_name: str,
        purpose: str,
        login_url: str,
        tool_url: str,
        failure_summary: str,
        failure_goals: list[str],
        current_spec: dict[str, object],
        scaffold: dict[str, object],
    ) -> str:
        schema = {
            "type": "object",
            "required": ["diagnosis", "repair_strategy", "workflow_spec"],
            "properties": {
                "diagnosis": {"type": "string"},
                "repair_strategy": {"type": "string"},
                "operator_checks": {"type": "array", "items": {"type": "string"}},
                "workflow_spec": {
                    "type": "object",
                    "required": ["workflow_name", "description", "publish", "mcp_ready", "nodes", "edges", "meta"],
                    "properties": {
                        "workflow_name": {"type": "string"},
                        "description": {"type": "string"},
                        "publish": {"type": "boolean"},
                        "mcp_ready": {"type": "boolean"},
                        "nodes": {"type": "array"},
                        "edges": {"type": "array"},
                        "meta": {"type": "object"},
                    },
                },
            },
        }
        return "\n\n".join(
            [
                "Return JSON only. No markdown fences or commentary.",
                "You are repairing a BrowserAct workflow spec after a runtime failure.",
                "Goal: produce a repaired workflow spec packet that keeps the intended workflow name and purpose but fixes the observed execution failure.",
                "Rules:",
                "- use Gemini judgment, not generic filler",
                "- keep the workflow grounded in actual BrowserAct node types like visit_page, input_text, click, wait, extract",
                "- preserve runtime input bindings when present; do not literalize placeholders like /text",
                "- if the evidence says a value_from_input binding was typed literally, repair the node config so BrowserAct treats it as a runtime input",
                "- keep publish true and mcp_ready false unless evidence clearly requires otherwise",
                "- keep nodes and edges compact and executable",
                "- operator_checks should be 2 to 4 short human verification checks",
                "Schema contract:\n" + json.dumps(schema, ensure_ascii=True),
                "Workflow brief:\n"
                + json.dumps(
                    {
                        "workflow_name": workflow_name,
                        "purpose": purpose,
                        "login_url": login_url,
                        "tool_url": tool_url,
                        "failure_summary": failure_summary,
                        "failing_step_goals": failure_goals,
                        "current_workflow_spec_json": current_spec,
                        "fallback_scaffold_spec_json": scaffold,
                    },
                    ensure_ascii=True,
                ),
            ]
        ).strip()

    def _normalize_workflow_repair_packet(
        self,
        raw: dict[str, object],
        *,
        workflow_name: str,
        purpose: str,
        scaffold: dict[str, object],
        failure_summary: str,
        failure_goals: list[str],
    ) -> dict[str, object]:
        packet = dict(raw)
        diagnosis = str(packet.get("diagnosis") or failure_summary).strip() or failure_summary
        repair_strategy = str(packet.get("repair_strategy") or "Repair the BrowserAct workflow spec to preserve runtime input binding and result extraction.").strip()
        operator_checks = self._normalize_string_list(packet.get("operator_checks"))[:4]
        workflow_spec = packet.get("workflow_spec")
        if not isinstance(workflow_spec, dict):
            workflow_spec = packet if isinstance(packet.get("nodes"), list) and isinstance(packet.get("edges"), list) else {}
        spec = dict(scaffold)
        spec.update({key: value for key, value in dict(workflow_spec).items() if key in {"workflow_name", "description", "publish", "mcp_ready", "nodes", "edges", "meta"}})
        spec["workflow_name"] = str(spec.get("workflow_name") or workflow_name).strip() or workflow_name
        spec["description"] = str(spec.get("description") or purpose).strip() or purpose
        spec["publish"] = bool(spec.get("publish", True))
        spec["mcp_ready"] = bool(spec.get("mcp_ready", False))
        nodes = spec.get("nodes")
        edges = spec.get("edges")
        if not isinstance(nodes, list) or not nodes:
            raise ToolExecutionError("workflow_nodes_required:browseract.repair_workflow_spec")
        if not isinstance(edges, list) or not edges:
            raise ToolExecutionError("workflow_edges_required:browseract.repair_workflow_spec")
        meta = dict(spec.get("meta") or {})
        meta["slug"] = str(meta.get("slug") or self._slugify(spec["workflow_name"])).strip() or self._slugify(spec["workflow_name"])
        meta["status"] = str(meta.get("status") or "pending_browseract_repair").strip() or "pending_browseract_repair"
        meta["repair_failure_summary"] = failure_summary
        meta["repair_failure_goals"] = failure_goals
        meta["repair_generated_at"] = now_utc_iso()
        meta["repair_source"] = "gemini_vortex"
        spec["meta"] = meta
        return {
            "diagnosis": diagnosis,
            "repair_strategy": repair_strategy,
            "operator_checks": operator_checks,
            "workflow_spec": spec,
        }

    def _service_facts(self, *, binding_auth_metadata_json: dict[str, object], service_name: str) -> dict[str, object] | None:
        normalized_service_name = str(service_name or "").strip().lower()
        raw = binding_auth_metadata_json.get("service_accounts_json")
        if isinstance(raw, dict):
            for key, value in raw.items():
                if str(key or "").strip().lower() != normalized_service_name:
                    continue
                if isinstance(value, dict):
                    return {str(entry_key): entry_value for entry_key, entry_value in value.items()}
                return {"value": value}
            if str(raw.get("service_name") or raw.get("service") or raw.get("name") or "").strip().lower() == normalized_service_name:
                return {str(key): value for key, value in raw.items()}
        if isinstance(raw, list):
            for value in raw:
                if not isinstance(value, dict):
                    continue
                candidate_name = str(value.get("service_name") or value.get("service") or value.get("name") or "").strip()
                if candidate_name.lower() != normalized_service_name:
                    continue
                return {str(key): entry_value for key, entry_value in value.items()}
        return None

    def _configured_api_key(self) -> str:
        for key_name in ("BROWSERACT_API_KEY", "BROWSERACT_API_KEY_FALLBACK_1", "BROWSERACT_API_KEY_FALLBACK_2", "BROWSERACT_API_KEY_FALLBACK_3"):
            value = str(os.getenv(key_name) or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _resolve_principal_id(request: ToolInvocationRequest, payload: dict[str, object]) -> str:
        request_principal_id = str((request.context_json or {}).get("principal_id") or "").strip()
        if not request_principal_id:
            raise ToolExecutionError("principal_id_required")
        supplied_principal_id = str(payload.get("principal_id") or "").strip()
        if supplied_principal_id and supplied_principal_id != request_principal_id:
            raise ToolExecutionError("principal_scope_mismatch")
        return request_principal_id

    @staticmethod
    def _chatplayground_request_urls(base_url: str) -> tuple[str, ...]:
        seen: set[str] = set()
        candidates: list[str] = []

        def _add_url(raw: str) -> None:
            url = str(raw or "").strip()
            if not url:
                return
            parsed = urlparse(url)
            scheme = str(parsed.scheme or "https").lower()
            netloc = parsed.netloc
            path = parsed.path or "/"
            if path != "/" and path:
                path = path.rstrip("/")
            query = parsed.query or ""
            fragment = parsed.fragment or ""
            if not netloc and "://" in url:
                return
            if not scheme:
                url = f"https://{url}"
                parsed = urlparse(url)
                scheme = "https"
                netloc = parsed.netloc
                path = parsed.path or ""
                query = parsed.query or ""
                fragment = parsed.fragment or ""
            if not netloc:
                return
            normalized = urlunparse((scheme, netloc, path, "", query, fragment)) or url
            if normalized in seen:
                return
            seen.add(normalized)
            candidates.append(normalized)

        parsed = urlparse(base_url or "")
        if not parsed.scheme:
            parsed = urlparse(f"https://{base_url}")
        if parsed.netloc:
            parsed_path = (parsed.path or "").rstrip("/")
            netloc = parsed.netloc
            api_prefixes = (
                "/api/chat/lmsys",
                "/api/chat",
                "/api/chat/completions",
                "/api/v1/chat/lmsys",
                "/api/v1/chat/completions",
            )
            if parsed_path.startswith("/api/"):
                candidate_paths = [parsed_path, *[suffix for suffix in api_prefixes if suffix != parsed_path]]
            else:
                candidate_paths = []
                for suffix in api_prefixes:
                    if not parsed_path or parsed_path == "/":
                        candidate_path = suffix
                    else:
                        candidate_path = f"{parsed_path}{suffix}"
                    candidate_paths.append(candidate_path)
            for candidate_path in candidate_paths:
                _add_url(urlunparse((parsed.scheme or "https", netloc, candidate_path, "", "", "")))
            _add_url(base_url)
            if parsed.netloc.lower() == "web.chatplayground.ai":
                _add_url("https://app.chatplayground.ai/api/chat/lmsys")
                _add_url("https://app.chatplayground.ai/api/v1/chat/lmsys")
        else:
            _add_url(base_url)
        return tuple(candidates)

    @staticmethod
    def _browseract_api_base() -> str:
        return str(os.getenv("BROWSERACT_WORKFLOW_API_BASE") or "https://api.browseract.com/v2/workflow").strip().rstrip("/")

    def _browseract_api_request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, object] | None = None,
        query: dict[str, str] | None = None,
        timeout_seconds: int = 120,
    ) -> dict[str, object]:
        api_key = self._configured_api_key()
        if not api_key:
            raise ToolExecutionError("browseract_api_key_missing")
        url = self._browseract_api_base() + path
        if query:
            url += "?" + urlencode(query)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "EA-BrowserAct/1.0",
        }
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ToolExecutionError(f"browseract_api_http_error:{exc.code}:{detail[:240]}") from exc
        except urllib.error.URLError as exc:
            raise ToolExecutionError(f"browseract_api_transport_error:{exc.reason}") from exc
        try:
            loaded = json.loads(body)
        except Exception as exc:
            raise ToolExecutionError("browseract_api_response_invalid") from exc
        return loaded if isinstance(loaded, dict) else {"data": loaded}

    @staticmethod
    def _browseract_extract_workflow_id(entry: dict[str, object]) -> str:
        for key in ("workflow_id", "id", "_id", "workflowId"):
            value = str(entry.get(key) or "").strip()
            if value:
                return value
        nested = entry.get("data")
        if isinstance(nested, dict):
            for key in ("workflow_id", "id", "_id", "workflowId"):
                value = str(nested.get(key) or "").strip()
                if value:
                    return value
        popup_url = str(entry.get("popup_url") or "").strip()
        if "/workflow/" in popup_url:
            tail = popup_url.split("/workflow/", 1)[1]
            workflow_id = tail.split("/", 1)[0].split("?", 1)[0].strip()
            if workflow_id:
                return workflow_id
        return ""

    def _browseract_list_workflows(self) -> list[dict[str, object]]:
        body = self._browseract_api_request("GET", "/list-workflows", timeout_seconds=120)
        for key in ("workflows", "data", "items", "rows"):
            value = body.get(key)
            if isinstance(value, list):
                return [entry for entry in value if isinstance(entry, dict)]
        return [body] if isinstance(body, dict) else []

    @staticmethod
    def _candidate_chatplayground_workflow_result_paths(
        *,
        payload: dict[str, object],
        binding_metadata: dict[str, object],
    ) -> tuple[Path, ...]:
        candidates: list[Path] = []
        for raw in (
            payload.get("workflow_result_path"),
            payload.get("result_path"),
            binding_metadata.get("chatplayground_workflow_result_path"),
            binding_metadata.get("workflow_result_path"),
            os.getenv("BROWSERACT_CHATPLAYGROUND_AUDIT_RESULT_PATH"),
            "/docker/fleet/state/browseract_bootstrap/runtime/ea_chatplayground_audit_live/result.json",
        ):
            value = str(raw or "").strip()
            if not value:
                continue
            path = Path(value).expanduser()
            if path not in candidates:
                candidates.append(path)
        return tuple(candidates)

    def _resolve_chatplayground_workflow(
        self,
        *,
        payload: dict[str, object],
        binding_metadata: dict[str, object],
    ) -> tuple[str, str]:
        for raw in (
            payload.get("workflow_id"),
            payload.get("browseract_workflow_id"),
            binding_metadata.get("chatplayground_workflow_id"),
            binding_metadata.get("browseract_workflow_id"),
            binding_metadata.get("workflow_id"),
            os.getenv("BROWSERACT_CHATPLAYGROUND_AUDIT_WORKFLOW_ID"),
        ):
            workflow_id = str(raw or "").strip()
            if workflow_id:
                return workflow_id, "explicit"

        for path in self._candidate_chatplayground_workflow_result_paths(payload=payload, binding_metadata=binding_metadata):
            if not path.exists():
                continue
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(loaded, dict):
                workflow_id = self._browseract_extract_workflow_id(loaded)
                if workflow_id:
                    return workflow_id, str(path)

        queries: list[str] = []
        for raw in (
            payload.get("workflow_query"),
            binding_metadata.get("chatplayground_workflow_query"),
            binding_metadata.get("workflow_query"),
            os.getenv("BROWSERACT_CHATPLAYGROUND_AUDIT_WORKFLOW_QUERY"),
            "ea_chatplayground_audit_live",
        ):
            value = str(raw or "").strip().lower()
            if value and value not in queries:
                queries.append(value)
        if not queries:
            return "", ""
        try:
            workflows = self._browseract_list_workflows()
        except ToolExecutionError:
            return "", ""
        for query_value in queries:
            for entry in workflows:
                workflow_id = self._browseract_extract_workflow_id(entry)
                if not workflow_id:
                    continue
                haystack = " ".join(
                    str(entry.get(field) or "")
                    for field in ("name", "title", "description", "slug", "workflow_name")
                ).lower()
                if query_value in haystack:
                    return workflow_id, query_value
        return "", ""

    @staticmethod
    def _browseract_task_id(body: dict[str, object]) -> str:
        for key in ("task_id", "id", "_id"):
            value = str(body.get(key) or "").strip()
            if value:
                return value
        nested = body.get("data")
        if isinstance(nested, dict):
            for key in ("task_id", "id", "_id"):
                value = str(nested.get(key) or "").strip()
                if value:
                    return value
        raise ToolExecutionError("browseract_task_id_missing")

    @staticmethod
    def _browseract_task_status(body: dict[str, object]) -> str:
        for key in ("status", "task_status", "state"):
            value = str(body.get(key) or "").strip()
            if value:
                return value.lower()
        nested = body.get("data")
        if isinstance(nested, dict):
            for key in ("status", "task_status", "state"):
                value = str(nested.get(key) or "").strip()
                if value:
                    return value.lower()
        return ""

    @staticmethod
    def _browseract_task_output(body: dict[str, object]) -> dict[str, object]:
        candidates = [
            body.get("output"),
            (body.get("data") or {}).get("output") if isinstance(body.get("data"), dict) else None,
            (body.get("result") or {}).get("output") if isinstance(body.get("result"), dict) else None,
        ]
        for candidate in candidates:
            if isinstance(candidate, dict):
                return dict(candidate)
        return {}

    @staticmethod
    def _browseract_output_has_content(value: object) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (int, float, bool)):
            return True
        if isinstance(value, dict):
            return any(BrowserActToolAdapter._browseract_output_has_content(nested) for nested in value.values())
        if isinstance(value, (list, tuple, set)):
            return any(BrowserActToolAdapter._browseract_output_has_content(nested) for nested in value)
        return bool(str(value).strip())

    @staticmethod
    def _browseract_task_finished_at(body: dict[str, object]) -> str:
        for key in ("finished_at", "finishedAt", "completed_at", "completedAt"):
            value = str(body.get(key) or "").strip()
            if value:
                return value
        nested = body.get("data")
        if isinstance(nested, dict):
            for key in ("finished_at", "finishedAt", "completed_at", "completedAt"):
                value = str(nested.get(key) or "").strip()
                if value:
                    return value
        return ""

    @staticmethod
    def _browseract_task_steps(body: dict[str, object]) -> list[dict[str, object]]:
        for key in ("steps",):
            value = body.get(key)
            if isinstance(value, list):
                return [entry for entry in value if isinstance(entry, dict)]
        nested = body.get("data")
        if isinstance(nested, dict):
            value = nested.get("steps")
            if isinstance(value, list):
                return [entry for entry in value if isinstance(entry, dict)]
        return []

    @staticmethod
    def _browseract_task_failure_info(body: dict[str, object]) -> dict[str, object]:
        for key in ("task_failure_info", "failure_info", "error"):
            value = body.get(key)
            if isinstance(value, dict):
                return dict(value)
        nested = body.get("data")
        if isinstance(nested, dict):
            for key in ("task_failure_info", "failure_info", "error"):
                value = nested.get(key)
                if isinstance(value, dict):
                    return dict(value)
        return {}

    @classmethod
    def _browseract_task_last_failed_step(cls, body: dict[str, object]) -> dict[str, object]:
        for step in reversed(cls._browseract_task_steps(body)):
            status = str(step.get("status") or step.get("state") or "").strip().lower()
            if status in {"failed", "error"}:
                return dict(step)
        return {}

    @classmethod
    def _browseract_task_failure_is_ignorable(cls, body: dict[str, object]) -> bool:
        failure = cls._browseract_task_failure_info(body)
        if not failure:
            return False
        fragments = list(_collect_text_fragments(failure))
        failed_step = cls._browseract_task_last_failed_step(body)
        fragments.extend(_collect_text_fragments(failed_step))
        fragment_tuple = tuple(fragments)
        if not _has_marker(fragment_tuple, ("target element not found",)):
            return False
        return _has_marker(
            fragment_tuple,
            (
                "dismiss overlay",
                "aria-label='close'",
                'aria-label="close"',
                "title='close'",
                'title="close"',
                "data-testid='close'",
                'data-testid="close"',
                "close modal",
                "close dialog",
                "close popover",
            ),
        )

    @classmethod
    def _browseract_task_can_salvage_failure(cls, body: dict[str, object]) -> bool:
        failure = cls._browseract_task_failure_info(body)
        if not failure:
            return False
        if cls._browseract_output_has_content(cls._browseract_task_output(body)):
            return True
        return cls._browseract_task_failure_is_ignorable(body)

    def _chatplayground_workflow_timeout_seconds(self, payload: dict[str, object]) -> int:
        raw = str(
            payload.get("timeout_seconds")
            or os.getenv("BROWSERACT_CHATPLAYGROUND_AUDIT_TIMEOUT_SECONDS")
            or os.getenv("EA_RESPONSES_CHATPLAYGROUND_TIMEOUT_SECONDS")
            or "600"
        ).strip() or "600"
        try:
            return max(30, min(1800, int(raw)))
        except Exception:
            return 600

    def _browseract_created_stall_seconds(self, payload: dict[str, object]) -> int:
        raw = str(
            payload.get("created_stall_seconds")
            or os.getenv("BROWSERACT_CHATPLAYGROUND_AUDIT_CREATED_STALL_SECONDS")
            or "120"
        ).strip() or "120"
        try:
            return max(30, min(900, int(raw)))
        except Exception:
            return 120

    def _chatplayground_workflow_attempts(self, payload: dict[str, object]) -> int:
        raw = str(
            payload.get("workflow_attempts")
            or os.getenv("BROWSERACT_CHATPLAYGROUND_AUDIT_MAX_ATTEMPTS")
            or "3"
        ).strip() or "3"
        try:
            return max(1, min(4, int(raw)))
        except Exception:
            return 3

    def _run_browseract_workflow_task(
        self,
        *,
        workflow_id: str,
        prompt: str,
    ) -> dict[str, object]:
        return self._run_browseract_workflow_task_with_inputs(
            workflow_id=workflow_id,
            input_values={"prompt": prompt},
        )

    @staticmethod
    def _browseract_workflow_input_variants(input_values: dict[str, object]) -> list[object]:
        values = {str(key or "").strip(): value for key, value in input_values.items() if str(key or "").strip()}
        if not values:
            return []
        ordered = list(values.items())
        return [
            [{"name": key, "value": value} for key, value in ordered],
            [{"key": key, "value": value} for key, value in ordered],
            [{key: value for key, value in ordered}],
            {key: value for key, value in ordered},
        ]

    def _run_browseract_workflow_task_with_inputs(
        self,
        *,
        workflow_id: str,
        input_values: dict[str, object],
    ) -> dict[str, object]:
        payload_variants = [
            {"workflow_id": workflow_id, "input_parameters": candidate}
            for candidate in self._browseract_workflow_input_variants(input_values)
        ]
        last_error = "browseract_run_task_failed"
        for candidate in payload_variants:
            try:
                return self._browseract_api_request("POST", "/run-task", payload=candidate, timeout_seconds=120)
            except ToolExecutionError as exc:
                last_error = str(exc)
                continue
        raise ToolExecutionError(last_error)

    @staticmethod
    def _onemin_browser_password() -> str:
        return str(os.getenv("ONEMIN_DEFAULT_PASSWORD") or os.getenv("BROWSERACT_PASSWORD") or "").strip()

    @staticmethod
    def _onemin_owner_email_for_account(*, account_label: str) -> str:
        from app.services import responses_upstream as upstream

        normalized = str(account_label or "").strip()
        if not normalized:
            return ""
        for row in upstream.onemin_owner_rows():
            if normalized in {
                str(row.get("account_name") or "").strip(),
                str(row.get("slot") or "").strip(),
                str(row.get("owner_label") or "").strip(),
            }:
                return str(row.get("owner_email") or "").strip()
        return ""

    @classmethod
    def _onemin_login_credentials_for_account(
        cls,
        *,
        account_label: str,
        binding_metadata: dict[str, object] | None = None,
    ) -> tuple[str, str]:
        from app.services import responses_upstream as upstream

        credentials = upstream.onemin_account_login_credentials(
            account_name=account_label,
            binding_metadata=dict(binding_metadata or {}),
        )
        login_email = str(credentials.get("login_email") or cls._onemin_owner_email_for_account(account_label=account_label)).strip()
        login_password = str(credentials.get("login_password") or cls._onemin_browser_password()).strip()
        return login_email, login_password

    @staticmethod
    def _onemin_billing_usage_ui_service() -> BrowserActUiServiceDefinition:
        return BrowserActUiServiceDefinition(
            service_key="onemin_billing_usage",
            capability_key="onemin_billing_usage",
            tool_name="browseract.onemin_billing_usage",
            skill_key="onemin_billing_usage",
            task_key="onemin_billing_usage",
            name="1min Billing Usage",
            description="Open the logged-in 1min billing usage page and extract the visible credit, top-up, and billing state.",
            deliverable_type="onemin_billing_usage_packet",
            action_kind="billing.inspect",
            output_label="billing_usage_page",
            browseract_service_names=("BrowserAct", "1min.ai"),
            tags=("browseract", "ui-only", "billing", "template-backed"),
            aliases=("onemin_billing", "1min_billing", "billing_usage"),
            binding_workflow_id_keys=(
                "onemin_billing_usage_workflow_id",
                "browseract_onemin_billing_usage_workflow_id",
            ),
            binding_run_url_keys=(
                "onemin_billing_usage_run_url",
                "browseract_onemin_billing_usage_run_url",
            ),
            required_top_level_inputs=(),
            required_runtime_inputs=(),
            payload_to_runtime_inputs={"page_url": "page_url"},
            input_properties={"page_url": {"type": "string"}},
            worker_script_name="browseract_template_service_worker.py",
            template_key="onemin_billing_usage_reader_live",
        )

    @staticmethod
    def _onemin_member_reconciliation_ui_service() -> BrowserActUiServiceDefinition:
        return BrowserActUiServiceDefinition(
            service_key="onemin_member_reconciliation",
            capability_key="onemin_member_reconciliation",
            tool_name="browseract.onemin_member_reconciliation",
            skill_key="onemin_member_reconciliation",
            task_key="onemin_member_reconciliation",
            name="1min Members Reconciliation",
            description="Open the logged-in 1min members page and extract the visible member roster, statuses, and credit-limit hints for owner reconciliation.",
            deliverable_type="onemin_member_reconciliation_packet",
            action_kind="members.reconcile",
            output_label="members_page",
            browseract_service_names=("BrowserAct", "1min.ai"),
            tags=("browseract", "ui-only", "members", "template-backed"),
            aliases=("onemin_members", "1min_members", "members_reconciliation"),
            binding_workflow_id_keys=(
                "onemin_members_workflow_id",
                "browseract_onemin_members_workflow_id",
            ),
            binding_run_url_keys=(
                "onemin_members_run_url",
                "browseract_onemin_members_run_url",
            ),
            required_top_level_inputs=(),
            required_runtime_inputs=(),
            payload_to_runtime_inputs={"page_url": "page_url"},
            input_properties={"page_url": {"type": "string"}},
            worker_script_name="browseract_template_service_worker.py",
            template_key="onemin_members_reconciliation_live",
        )

    def _run_onemin_workflow_task(
        self,
        *,
        workflow_id: str,
        account_label: str,
        timeout_seconds: int,
        binding_metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        login_email, login_password = self._onemin_login_credentials_for_account(
            account_label=account_label,
            binding_metadata=binding_metadata,
        )
        if not login_email:
            raise ToolExecutionError(f"owner_email_required:onemin:{account_label}")
        if not login_password:
            raise ToolExecutionError("onemin_password_missing")
        started = self._run_browseract_workflow_task_with_inputs(
            workflow_id=workflow_id,
            input_values={
                "browseract_username": login_email,
                "browseract_password": login_password,
            },
        )
        return self._wait_for_browseract_task(
            task_id=self._browseract_task_id(started),
            timeout_seconds=timeout_seconds,
            created_stall_seconds=min(120, timeout_seconds),
        )

    def _wait_for_browseract_task(
        self,
        *,
        task_id: str,
        timeout_seconds: int,
        created_stall_seconds: int = 120,
    ) -> dict[str, object]:
        deadline = time.time() + max(30, timeout_seconds)
        last_status = ""
        created_started_at = time.time()
        inconsistent_started_at: float | None = None
        while time.time() < deadline:
            status_body = self._browseract_api_request(
                "GET",
                "/get-task-status",
                query={"task_id": task_id},
                timeout_seconds=60,
            )
            status = self._browseract_task_status(status_body)
            if status:
                last_status = status
            if status in {"created", "queued", "pending", "running", "processing"}:
                task_body = self._browseract_api_request(
                    "GET",
                    "/get-task",
                    query={"task_id": task_id},
                    timeout_seconds=120,
                )
                task_status = self._browseract_task_status(task_body)
                if task_status:
                    last_status = task_status
                failure_info = self._browseract_task_failure_info(task_body)
                if failure_info:
                    if self._browseract_task_can_salvage_failure(task_body):
                        return task_body
                    detail = json.dumps(failure_info, ensure_ascii=True)[:400]
                    raise ToolExecutionError(f"browseract_task_failed:{detail}")
                if self._browseract_output_has_content(self._browseract_task_output(task_body)):
                    return task_body
                if self._browseract_task_steps(task_body):
                    created_started_at = time.time()
                    inconsistent_started_at = None
                    time.sleep(5)
                    continue
                if self._browseract_task_finished_at(task_body):
                    if inconsistent_started_at is None:
                        inconsistent_started_at = time.time()
                    elif time.time() - inconsistent_started_at >= max(15, min(60, created_stall_seconds // 2 or 15)):
                        raise ToolExecutionError(f"browseract_task_inconsistent_terminal:{task_id}:{status or task_status or 'unknown'}")
                    time.sleep(5)
                    continue
                if time.time() - created_started_at >= max(30, created_stall_seconds):
                    raise ToolExecutionError(f"browseract_task_stuck_created:{task_id}:{status}")
            if status in {"done", "completed", "success", "succeeded", "finished"}:
                task_body = self._browseract_api_request(
                    "GET",
                    "/get-task",
                    query={"task_id": task_id},
                    timeout_seconds=120,
                )
                failure_info = self._browseract_task_failure_info(task_body)
                if failure_info:
                    if self._browseract_task_can_salvage_failure(task_body):
                        return task_body
                    detail = json.dumps(failure_info, ensure_ascii=True)[:400]
                    raise ToolExecutionError(f"browseract_task_failed:{detail}")
                if self._browseract_output_has_content(self._browseract_task_output(task_body)):
                    return task_body
                if self._browseract_task_steps(task_body):
                    inconsistent_started_at = None
                    time.sleep(5)
                    continue
                if self._browseract_task_finished_at(task_body):
                    if inconsistent_started_at is None:
                        inconsistent_started_at = time.time()
                    elif time.time() - inconsistent_started_at >= max(15, min(60, created_stall_seconds // 2 or 15)):
                        raise ToolExecutionError(f"browseract_task_inconsistent_terminal:{task_id}:{status}")
                    time.sleep(5)
                    continue
                return task_body
            if status in {"failed", "error", "cancelled", "canceled"}:
                task_body = self._browseract_api_request(
                    "GET",
                    "/get-task",
                    query={"task_id": task_id},
                    timeout_seconds=120,
                )
                if self._browseract_task_can_salvage_failure(task_body):
                    return task_body
                failure_info = self._browseract_task_failure_info(task_body)
                if failure_info:
                    detail = json.dumps(failure_info, ensure_ascii=True)[:400]
                else:
                    detail = json.dumps(task_body or status_body, ensure_ascii=True)[:400]
                raise ToolExecutionError(f"browseract_task_failed:{detail}")
            time.sleep(5)
        raise ToolExecutionError(f"browseract_task_timeout:{last_status or 'unknown'}")

    def _normalize_chatplayground_workflow_task_payload(
        self,
        *,
        task_body: dict[str, object],
        workflow_id: str,
        workflow_source: str,
        task_id: str,
        roles: list[str],
        audit_scope: str,
        requested_models: list[str],
    ) -> dict[str, object]:
        output_json = self._browseract_task_output(task_body)
        unwrapped = _unwrap_browseract_output_payload(output_json)
        normalized: dict[str, object] = {}
        if isinstance(unwrapped, dict):
            normalized = dict(unwrapped)
        elif isinstance(unwrapped, str):
            normalized = {
                "consensus": unwrapped,
                "recommendation": unwrapped,
                "raw_response_text": unwrapped,
            }
        if not normalized:
            fallback_text = _extract_textish(output_json)
            if fallback_text:
                normalized = {
                    "consensus": fallback_text,
                    "recommendation": fallback_text,
                    "raw_response_text": fallback_text,
                }
        if not normalized:
            raise ToolExecutionError("browseract_chatplayground_empty_output")
        normalized.setdefault("roles", list(roles))
        normalized.setdefault("requested_roles", list(roles))
        normalized.setdefault("audit_scope", audit_scope)
        normalized.setdefault("requested_models", list(requested_models))
        normalized.setdefault("requested_at", now_utc_iso())
        normalized.setdefault("requested_url", f"browseract://workflow/{workflow_id}/task/{task_id}")
        normalized.setdefault("workflow_id", workflow_id)
        normalized.setdefault("task_id", task_id)
        normalized.setdefault("workflow_source", workflow_source)
        normalized.setdefault("task_status", self._browseract_task_status(task_body) or "finished")
        normalized.setdefault("workflow_output_json", output_json)
        return normalized

    @staticmethod
    def _extract_workflow_id_for_service(
        *,
        binding_auth_metadata_json: dict[str, object],
        service_name: str,
    ) -> str:
        service_workflows: object = (
            binding_auth_metadata_json.get("browseract_service_workflows_json")
            or binding_auth_metadata_json.get("service_workflows_json")
            or {}
        )
        if isinstance(service_workflows, str):
            service_workflows = _load_jsonish(service_workflows) or {}
        candidate: object = ""
        if isinstance(service_workflows, dict):
            normalized_service_name = service_name.strip().casefold()
            for key, value in service_workflows.items():
                if str(key or "").strip().casefold() == normalized_service_name:
                    candidate = value
                    break
            if isinstance(candidate, dict):
                candidate = candidate.get("browseract_workflow_id") or candidate.get("workflow_id") or ""
        if not candidate:
            candidate = binding_auth_metadata_json.get("browseract_workflow_id") or ""
        workflow_id = str(candidate or "").strip()
        if not workflow_id or not re.fullmatch(r"[A-Za-z0-9._:-]{1,160}", workflow_id):
            return ""
        return workflow_id

    @classmethod
    def _extract_workflow_facts(cls, value: object, *, depth: int = 0) -> dict[str, object] | None:
        if depth > 5:
            return None
        if isinstance(value, str):
            parsed = _load_jsonish(value)
            if parsed is None or parsed == value:
                return None
            return cls._extract_workflow_facts(parsed, depth=depth + 1)
        if isinstance(value, list):
            rows = [row for row in value if isinstance(row, dict)]
            if len(rows) != 1:
                return None
            return cls._extract_workflow_facts(rows[0], depth=depth + 1)
        if not isinstance(value, dict):
            return None
        facts_json = value.get("facts_json")
        if isinstance(facts_json, dict):
            return {str(key): nested for key, nested in facts_json.items()}
        for key in ("string", "text", "json", "data", "result", "output"):
            if key not in value:
                continue
            facts_json = cls._extract_workflow_facts(value.get(key), depth=depth + 1)
            if facts_json is not None:
                return facts_json
        if any(
            key in value
            for key in (
                "place_name",
                "place_category",
                "final_surface_url",
                "address",
                "plus_code",
            )
        ):
            return {str(key): nested for key, nested in value.items()}
        return None

    @staticmethod
    def _extract_workflow_timeout_seconds(
        *,
        binding_auth_metadata_json: dict[str, object],
        payload: dict[str, object],
    ) -> int:
        raw = str(
            payload.get("workflow_timeout_seconds")
            or binding_auth_metadata_json.get("browseract_workflow_timeout_seconds")
            or "300"
        ).strip() or "300"
        try:
            return max(30, min(600, int(raw)))
        except Exception:
            return 300

    def _live_extract_with_workflow(
        self,
        *,
        binding_auth_metadata_json: dict[str, object],
        payload: dict[str, object],
        service_name: str,
        requested_fields: tuple[str, ...],
        workflow_id: str,
    ) -> dict[str, object]:
        instructions = str(payload.get("instructions") or binding_auth_metadata_json.get("instructions") or "").strip()
        account_hints_json = dict(payload.get("account_hints_json") or {})
        input_values: dict[str, object] = {
            "service_name": service_name,
            "requested_fields_json": json.dumps(list(requested_fields), ensure_ascii=True),
            "instructions": instructions,
            "account_hints_json": json.dumps(account_hints_json, ensure_ascii=True, sort_keys=True),
        }
        for key in ("query_url", "fact_key", "listing_latitude", "listing_longitude", "travel_mode"):
            value = account_hints_json.get(key)
            if isinstance(value, (str, int, float, bool)) and str(value).strip():
                input_values[key] = value
        allowed_workflow_input_names = binding_auth_metadata_json.get(
            "browseract_workflow_input_names_json"
        ) or binding_auth_metadata_json.get("workflow_input_names_json") or ()
        if isinstance(allowed_workflow_input_names, str):
            allowed_workflow_input_names = (
                _load_jsonish(allowed_workflow_input_names)
                or [
                    value.strip()
                    for value in allowed_workflow_input_names.split(",")
                    if value.strip()
                ]
            )
        allowed_names = {
            str(value or "").strip()
            for value in (
                allowed_workflow_input_names
                if isinstance(allowed_workflow_input_names, (list, tuple, set))
                else ()
            )
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,79}", str(value or "").strip())
        }
        requested_workflow_inputs = payload.get("workflow_inputs_json") or {}
        if isinstance(requested_workflow_inputs, str):
            requested_workflow_inputs = _load_jsonish(requested_workflow_inputs) or {}
        if isinstance(requested_workflow_inputs, dict):
            reserved_names = set(input_values)
            for raw_name, value in requested_workflow_inputs.items():
                name = str(raw_name or "").strip()
                if (
                    name not in allowed_names
                    or name in reserved_names
                    or not isinstance(value, (str, int, float, bool))
                    or not str(value).strip()
                ):
                    continue
                input_values[name] = value
        started = self._run_browseract_workflow_task_with_inputs(
            workflow_id=workflow_id,
            input_values=input_values,
        )
        task_id = self._browseract_task_id(started)
        timeout_seconds = self._extract_workflow_timeout_seconds(
            binding_auth_metadata_json=binding_auth_metadata_json,
            payload=payload,
        )
        task_body = self._wait_for_browseract_task(
            task_id=task_id,
            timeout_seconds=timeout_seconds,
            created_stall_seconds=min(120, timeout_seconds),
        )
        facts_json = self._extract_workflow_facts(self._browseract_task_output(task_body))
        if facts_json is None:
            raise ToolExecutionError(f"browseract_workflow_facts_missing:{task_id}")
        return facts_json | {
            "verification_source": "browseract_workflow",
            "_browseract_workflow_id": workflow_id,
            "_browseract_task_id": task_id,
        }

    def _live_extract(
        self,
        *,
        binding_auth_metadata_json: dict[str, object],
        payload: dict[str, object],
        service_name: str,
        requested_fields: tuple[str, ...],
    ) -> dict[str, object] | None:
        run_url = str(payload.get("run_url") or binding_auth_metadata_json.get("browseract_run_url") or binding_auth_metadata_json.get("run_url") or "").strip()
        api_key = self._configured_api_key()
        if not api_key:
            return None
        workflow_id = self._extract_workflow_id_for_service(
            binding_auth_metadata_json=binding_auth_metadata_json,
            service_name=service_name,
        )
        if not run_url:
            if not workflow_id:
                return None
            return self._live_extract_with_workflow(
                binding_auth_metadata_json=binding_auth_metadata_json,
                payload=payload,
                service_name=service_name,
                requested_fields=requested_fields,
                workflow_id=workflow_id,
            )
        request_body = {
            "service_name": service_name,
            "requested_fields": list(requested_fields),
            "instructions": str(payload.get("instructions") or binding_auth_metadata_json.get("instructions") or ""),
            "account_hints_json": dict(payload.get("account_hints_json") or {}),
        }
        request = urllib.request.Request(
            run_url,
            data=json.dumps(request_body).encode("utf-8"),
            headers={"authorization": f"Bearer {api_key}", "content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            raise ToolExecutionError(f"browseract_live_http_error:{exc.code}") from exc
        except urllib.error.URLError as exc:
            raise ToolExecutionError(f"browseract_live_transport_error:{exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise ToolExecutionError("browseract_live_response_invalid") from exc
        candidates = (
            body.get("facts_json") if isinstance(body, dict) else None,
            ((body.get("data") or {}).get("facts_json")) if isinstance(body, dict) and isinstance(body.get("data"), dict) else None,
            ((body.get("result") or {}).get("facts_json")) if isinstance(body, dict) and isinstance(body.get("result"), dict) else None,
            ((body.get("output") or {}).get("facts_json")) if isinstance(body, dict) and isinstance(body.get("output"), dict) else None,
        )
        for candidate in candidates:
            if isinstance(candidate, dict):
                return {str(key): value for key, value in candidate.items()} | {"verification_source": "browseract_live"}
        if isinstance(body, dict):
            return {str(key): value for key, value in body.items()} | {"verification_source": "browseract_live"}
        raise ToolExecutionError("browseract_live_response_invalid")

    def _fact_present(self, value: object) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, tuple, dict, set)):
            return bool(value)
        return True

    def _summary_text(
        self,
        *,
        service_name: str,
        facts_json: dict[str, object],
        requested_fields: tuple[str, ...],
        missing_fields: tuple[str, ...],
        verification_source: str,
        last_verified_at: str,
    ) -> str:
        ordered_keys = requested_fields or tuple(key for key in facts_json.keys() if key not in {"service_name", "verification_source"})
        lines = [f"Service: {service_name}", f"Verification source: {verification_source}", f"Last verified at: {last_verified_at}"]
        for key in ordered_keys:
            value = facts_json.get(key)
            lines.append(f"{key}: {value}" if self._fact_present(value) else f"{key}: <missing>")
        if missing_fields:
            lines.append(f"Missing fields: {', '.join(missing_fields)}")
        return "\n".join(lines)

    def _inventory_summary_text(self, services_json: list[dict[str, object]]) -> str:
        summaries = [str((row.get("normalized_text") or "")).strip() for row in services_json if str((row.get("normalized_text") or "")).strip()]
        if not summaries:
            return "No BrowserAct-backed service inventory facts were discovered."
        return "\n\n".join(summaries)

    def _extract_service_record(
        self,
        *,
        binding_auth_metadata_json: dict[str, object],
        payload: dict[str, object],
        service_name: str,
        requested_fields: tuple[str, ...],
        allow_missing: bool,
    ) -> dict[str, object]:
        facts_json = self._service_facts(binding_auth_metadata_json=binding_auth_metadata_json, service_name=service_name)
        live_discovery_error = ""
        if facts_json is None:
            try:
                live_facts_json = self._live_extract(
                    binding_auth_metadata_json=binding_auth_metadata_json,
                    payload=payload,
                    service_name=service_name,
                    requested_fields=requested_fields,
                )
            except ToolExecutionError as exc:
                live_discovery_error = str(exc)
                live_facts_json = None
            if live_facts_json is not None:
                facts_json = dict(live_facts_json)
        elif requested_fields:
            try:
                live_facts_json = self._live_extract(
                    binding_auth_metadata_json=binding_auth_metadata_json,
                    payload=payload,
                    service_name=service_name,
                    requested_fields=requested_fields,
                )
            except ToolExecutionError as exc:
                live_discovery_error = str(exc)
                live_facts_json = None
            if live_facts_json is not None:
                merged_facts_json = {str(key): value for key, value in facts_json.items()}
                for key, value in live_facts_json.items():
                    if self._fact_present(value):
                        merged_facts_json[str(key)] = value
                facts_json = merged_facts_json
        verification_source = "connector_metadata"
        browseract_workflow_id = ""
        browseract_task_id = ""
        if facts_json is None:
            facts_json = {}
            verification_source = "missing"
        else:
            verification_source = str(facts_json.pop("verification_source", "") or "connector_metadata").strip() or "connector_metadata"
            browseract_workflow_id = str(facts_json.pop("_browseract_workflow_id", "") or "").strip()
            browseract_task_id = str(facts_json.pop("_browseract_task_id", "") or "").strip()
        normalized_facts_json = {str(key): value for key, value in facts_json.items()}
        normalized_facts_json.setdefault("service_name", service_name)
        resolved_requested_fields = requested_fields or tuple(key for key in normalized_facts_json.keys() if key != "service_name")
        if not resolved_requested_fields and allow_missing:
            resolved_requested_fields = ("tier", "account_email", "status")
        missing_fields = tuple(key for key in resolved_requested_fields if not self._fact_present(normalized_facts_json.get(key)))
        account_email = str(normalized_facts_json.get("account_email") or normalized_facts_json.get("email") or normalized_facts_json.get("login_email") or "").strip()
        plan_tier = str(normalized_facts_json.get("tier") or normalized_facts_json.get("plan") or normalized_facts_json.get("plan_tier") or normalized_facts_json.get("license_tier") or "").strip()
        last_verified_at = now_utc_iso()
        discovery_status = "missing" if verification_source == "missing" else ("complete" if resolved_requested_fields and not missing_fields else "partial")
        normalized_text = self._summary_text(
            service_name=service_name,
            facts_json=normalized_facts_json,
            requested_fields=resolved_requested_fields,
            missing_fields=missing_fields,
            verification_source=verification_source,
            last_verified_at=last_verified_at,
        )
        instructions = str(payload.get("instructions") or binding_auth_metadata_json.get("instructions") or "").strip()
        account_hints_json = dict(payload.get("account_hints_json") or {})
        requested_run_url = str(payload.get("run_url") or binding_auth_metadata_json.get("browseract_run_url") or binding_auth_metadata_json.get("run_url") or "").strip()
        requested_workflow_id = self._extract_workflow_id_for_service(
            binding_auth_metadata_json=binding_auth_metadata_json,
            service_name=service_name,
        )
        if browseract_workflow_id:
            requested_workflow_id = browseract_workflow_id
        structured_output_json = {
            "service_name": service_name,
            "facts_json": normalized_facts_json,
            "requested_fields": list(resolved_requested_fields),
            "missing_fields": list(missing_fields),
            "discovery_status": discovery_status,
            "verification_source": verification_source,
            "last_verified_at": last_verified_at,
            "account_email": account_email,
            "plan_tier": plan_tier,
            "instructions": instructions,
            "account_hints_json": account_hints_json,
            "requested_run_url": requested_run_url,
            "requested_workflow_id": requested_workflow_id,
            "browseract_task_id": browseract_task_id,
            "live_discovery_error": live_discovery_error,
        }
        return {
            "service_name": service_name,
            "facts_json": normalized_facts_json,
            "requested_fields": list(resolved_requested_fields),
            "missing_fields": list(missing_fields),
            "account_email": account_email,
            "plan_tier": plan_tier,
            "discovery_status": discovery_status,
            "verification_source": verification_source,
            "last_verified_at": last_verified_at,
            "instructions": instructions,
            "account_hints_json": account_hints_json,
            "requested_run_url": requested_run_url,
            "requested_workflow_id": requested_workflow_id,
            "browseract_task_id": browseract_task_id,
            "live_discovery_error": live_discovery_error,
            "normalized_text": normalized_text,
            "preview_text": artifact_preview_text(normalized_text),
            "mime_type": "text/plain",
            "structured_output_json": structured_output_json,
        }

    def execute_chatplayground_audit(
        self,
        request: ToolInvocationRequest,
        definition: ToolDefinition,
    ) -> ToolInvocationResult:
        payload = dict(request.payload_json or {})
        binding = None
        binding_id = str(payload.get("binding_id") or "").strip()
        if binding_id:
            principal_id, binding = self._resolve_browseract_binding(
                request=request,
                payload=payload,
                required_input_error="connector_binding_required:browseract.chatplayground_audit",
                required_scopes=None,
            )
        else:
            principal_id = self._resolve_principal_id(request, payload)
        prompt = str(
            payload.get("prompt")
            or payload.get("normalized_text")
            or payload.get("source_text")
            or payload.get("diff_text")
            or ""
        ).strip()
        if not prompt:
            raise ToolExecutionError(f"prompt_required:{definition.tool_name}")
        binding_metadata = dict(getattr(binding, "auth_metadata_json", {}) or {})
        resolved_binding_id = str(getattr(binding, "binding_id", "") or "")
        connector_name = str(getattr(binding, "connector_name", "") or "browseract") or "browseract"
        external_account_ref = str(getattr(binding, "external_account_ref", "") or "")
        run_url = str(
            payload.get("run_url")
            or binding_metadata.get("chatplayground_run_url")
            or binding_metadata.get("browseract_run_url")
            or binding_metadata.get("run_url")
            or os.environ.get("BROWSERACT_CHATPLAYGROUND_URL", "https://web.chatplayground.ai/").strip()
            or "https://web.chatplayground.ai/"
        ).strip()
        roles = [str(entry) for entry in (payload.get("roles") or ("factuality", "adversarial", "completeness", "risk")) if str(entry).strip()]
        if not roles:
            roles = ["factuality", "adversarial", "completeness", "risk"]
        audit_scope = str(payload.get("scope") or payload.get("audit_scope") or "").strip().lower()
        if not audit_scope:
            action_kind = str(request.action_kind or "").strip()
            if action_kind and "." in action_kind:
                audit_scope = action_kind.rsplit(".", 1)[-1].strip().lower()
            else:
                audit_scope = "jury"
        callback = getattr(self, "_chatplayground_audit", None)
        if callback is not None:
            callback_result = self._safe_call_chatplayground_audit_callback(
                callback=callback,
                request=request,
                payload=payload,
                definition=definition,
                prompt=prompt,
                roles=tuple(roles),
                audit_scope=audit_scope,
                run_url=run_url,
            )
            if callback_result is not None:
                return callback_result

        request_payload = {
            "prompt": prompt,
            "roles": list(roles),
            "requested_roles": list(roles),
            "audit_scope": audit_scope,
            "model": str(payload.get("model") or "").strip(),
            "requested_models": _normalize_text_list(payload.get("requested_models")),
            "principal_id": principal_id,
            "binding_id": resolved_binding_id,
            "external_account_ref": external_account_ref,
        }
        http_errors: list[str] = []
        workflow_id, workflow_source = self._resolve_chatplayground_workflow(
            payload=payload,
            binding_metadata=binding_metadata,
        )
        if workflow_id and self._configured_api_key():
            workflow_prompt = _render_chatplayground_workflow_prompt(
                prompt=prompt,
                roles=list(roles),
                audit_scope=audit_scope,
                requested_models=list(request_payload["requested_models"]),
            )
            max_attempts = self._chatplayground_workflow_attempts(payload)
            for attempt in range(max_attempts):
                try:
                    started = self._run_browseract_workflow_task(workflow_id=workflow_id, prompt=workflow_prompt or prompt)
                    task_id = self._browseract_task_id(started)
                    task_body = self._wait_for_browseract_task(
                        task_id=task_id,
                        timeout_seconds=self._chatplayground_workflow_timeout_seconds(payload),
                        created_stall_seconds=self._browseract_created_stall_seconds(payload),
                    )
                    response = self._normalize_chatplayground_workflow_task_payload(
                        task_body=task_body,
                        workflow_id=workflow_id,
                        workflow_source=workflow_source,
                        task_id=task_id,
                        roles=list(roles),
                        audit_scope=audit_scope,
                        requested_models=list(request_payload["requested_models"]),
                    )
                    self._raise_for_ui_lane_failure(payload=response, backend="chatplayground")
                    (
                        consensus,
                        recommendation,
                        normalized_roles,
                        disagreements,
                        risks,
                        model_deltas,
                        details,
                    ) = _normalize_chatplayground_audit_payload(response)
                    if consensus or recommendation:
                        safe_payload = {
                            **details,
                            "binding_id": resolved_binding_id,
                            "connector_name": connector_name,
                            "external_account_ref": external_account_ref,
                            "requested_url": str(response.get("requested_url") or f"browseract://workflow/{workflow_id}/task/{task_id}"),
                            "requested_roles": list(roles),
                            "audit_scope": audit_scope,
                            "consensus": consensus,
                            "recommendation": recommendation,
                            "roles": normalized_roles,
                            "disagreements": disagreements,
                            "risks": risks,
                            "model_deltas": model_deltas,
                            "prompt": prompt,
                            "workflow_prompt_chars": len(workflow_prompt or prompt),
                            "workflow_id": workflow_id,
                            "task_id": task_id,
                            "workflow_source": workflow_source,
                        }
                        action_kind = str(request.action_kind or "chatplayground_audit") or "chatplayground_audit"
                        normalized_text = str(safe_payload.get("normalized_text") or json.dumps(safe_payload, ensure_ascii=True, separators=(",", ":")))
                        return ToolInvocationResult(
                            tool_name=definition.tool_name,
                            action_kind=action_kind,
                            target_ref=f"browseract:{resolved_binding_id or 'env'}:chatplayground_audit:{task_id}",
                            output_json={
                                **safe_payload,
                                "tool_name": definition.tool_name,
                                "action_kind": action_kind,
                                "normalized_text": normalized_text,
                                "preview_text": artifact_preview_text(normalized_text),
                                "mime_type": "text/plain",
                                "structured_output_json": safe_payload,
                            },
                            receipt_json={
                                "binding_id": resolved_binding_id,
                                "connector_name": connector_name,
                                "external_account_ref": external_account_ref,
                                "principal_id": principal_id,
                                "handler_key": definition.tool_name,
                                "invocation_contract": "tool.v1",
                                "tool_version": definition.version,
                                "tool_name": definition.tool_name,
                                "action_kind": action_kind,
                                "requested_url": str(response.get("requested_url") or f"browseract://workflow/{workflow_id}/task/{task_id}"),
                                "requested_roles": list(roles),
                                "audit_scope": audit_scope,
                                "route": "browseract.chatplayground_audit",
                                "handler": "workflow_api",
                                "workflow_id": workflow_id,
                                "task_id": task_id,
                                "workflow_source": workflow_source,
                            },
                        )
                    http_errors.append(f"workflow:{workflow_id}:empty_audit")
                    break
                except ToolExecutionError as exc:
                    detail = str(exc)
                    retryable_prefixes = (
                        "browseract_task_inconsistent_terminal:",
                        "browseract_task_stuck_created:",
                    )
                    if detail.startswith(retryable_prefixes) and attempt + 1 < max_attempts:
                        time.sleep(min(10, 3 * (attempt + 1)))
                        continue
                    http_errors.append(f"workflow:{workflow_id}:{detail}")
                    break
        if self._configured_api_key():
            for candidate_url in self._chatplayground_request_urls(run_url):
                try:
                    response = self._post_browseract_json(
                        run_url=candidate_url,
                        request_payload=request_payload,
                        timeout_seconds=60,
                    )
                except ToolExecutionError as exc:
                    http_errors.append(f"{candidate_url}:{exc}")
                    continue
                self._raise_for_ui_lane_failure(payload=response, backend="chatplayground")
                (
                    consensus,
                    recommendation,
                    normalized_roles,
                    disagreements,
                    risks,
                    model_deltas,
                    details,
                ) = _normalize_chatplayground_audit_payload(response)
                if not consensus and not recommendation:
                    http_errors.append(f"{candidate_url}:empty_audit")
                    continue
                safe_payload = {
                    **details,
                    "binding_id": resolved_binding_id,
                    "connector_name": connector_name,
                    "external_account_ref": external_account_ref,
                    "requested_url": candidate_url,
                    "requested_roles": list(roles),
                    "audit_scope": audit_scope,
                    "consensus": consensus,
                    "recommendation": recommendation,
                    "roles": normalized_roles,
                    "disagreements": disagreements,
                    "risks": risks,
                    "model_deltas": model_deltas,
                    "prompt": prompt,
                }
                action_kind = str(request.action_kind or "chatplayground_audit") or "chatplayground_audit"
                normalized_text = str(safe_payload.get("normalized_text") or json.dumps(safe_payload, ensure_ascii=True, separators=(",", ":")))
                return ToolInvocationResult(
                    tool_name=definition.tool_name,
                    action_kind=action_kind,
                    target_ref=f"browseract:{resolved_binding_id or 'env'}:chatplayground_audit:{uuid.uuid4()}",
                    output_json={
                        **safe_payload,
                        "tool_name": definition.tool_name,
                        "action_kind": action_kind,
                        "normalized_text": normalized_text,
                        "preview_text": artifact_preview_text(normalized_text),
                        "mime_type": "text/plain",
                        "structured_output_json": safe_payload,
                    },
                    receipt_json={
                        "binding_id": resolved_binding_id,
                        "connector_name": connector_name,
                        "external_account_ref": external_account_ref,
                        "principal_id": principal_id,
                        "handler_key": definition.tool_name,
                        "invocation_contract": "tool.v1",
                        "tool_version": definition.version,
                        "tool_name": definition.tool_name,
                        "action_kind": action_kind,
                        "requested_url": candidate_url,
                        "requested_roles": list(roles),
                        "audit_scope": audit_scope,
                        "route": "browseract.chatplayground_audit",
                        "handler": "run_url",
                    },
                )

        if not binding_id:
            if http_errors:
                raise ToolExecutionError(f"browseract_chatplayground_audit_unavailable:{'; '.join(http_errors)}")
            raise ToolExecutionError("connector_binding_required:browseract.chatplayground_audit")

        normalized_text = "\n".join(
            [
                "ChatPlayground audit backend unavailable",
                f"run_url: {run_url or '<missing>'}",
                f"roles: {', '.join(roles) if roles else '<none>'}",
                f"errors: {'; '.join(http_errors) if http_errors else 'no_backend'}",
            ]
        )
        action_kind = str(request.action_kind or "chatplayground_audit") or "chatplayground_audit"
        return ToolInvocationResult(
            tool_name=definition.tool_name,
            action_kind=action_kind,
            target_ref=f"browseract:{resolved_binding_id or 'env'}:chatplayground_audit:{uuid.uuid4()}",
            output_json={
                "binding_id": resolved_binding_id,
                "connector_name": connector_name,
                "external_account_ref": external_account_ref,
                "prompt": prompt,
                "requested_url": run_url,
                "roles": roles,
                "requested_roles": roles,
                "audit_scope": audit_scope,
                "principal_id": principal_id,
                "normalized_text": normalized_text,
                "preview_text": artifact_preview_text(normalized_text),
                "mime_type": "text/plain",
                "structured_output_json": {
                    "prompt": prompt,
                    "requested_url": run_url,
                    "run_url": run_url,
                    "requested_roles": roles,
                    "roles": roles,
                    "audit_scope": audit_scope,
                    "principal_id": principal_id,
                    "binding_id": resolved_binding_id,
                    "connector_name": connector_name,
                    "external_account_ref": external_account_ref,
                    "status": "backend_unavailable",
                    "http_errors": list(http_errors),
                },
                "tool_name": definition.tool_name,
                "action_kind": action_kind,
            },
            receipt_json={
                "binding_id": resolved_binding_id,
                "connector_name": connector_name,
                "external_account_ref": external_account_ref,
                "principal_id": principal_id,
                "handler_key": definition.tool_name,
                "invocation_contract": "tool.v1",
                "tool_version": definition.version,
                "tool_name": definition.tool_name,
                "action_kind": action_kind,
                "requested_url": run_url,
                "requested_roles": roles,
                "audit_scope": audit_scope,
                "route": "browseract.chatplayground_audit",
                "handler": "unavailable",
            },
        )

    @staticmethod
    def _safe_call_chatplayground_audit_callback(
        *,
        callback,
        request: ToolInvocationRequest,
        payload: dict[str, object],
        definition: ToolDefinition,
        prompt: str,
        roles: tuple[str, ...],
        audit_scope: str,
        run_url: str,
    ) -> ToolInvocationResult | None:
        candidate = None
        request_payload = dict(payload)
        request_payload["prompt"] = prompt
        request_payload["roles"] = list(roles)
        request_payload["requested_roles"] = list(roles)
        request_payload["audit_scope"] = audit_scope
        request_payload.setdefault("run_url", run_url)
        request_payload["requested_url"] = run_url
        signatures = None
        try:
            signatures = inspect.signature(callback)
        except Exception:
            signatures = None

        call_payload_variants: list[dict[str, object]] = [
            {"payload": request_payload, "run_url": run_url, "request_payload": request_payload},
            {"request_payload": request_payload, "run_url": run_url, "payload": request_payload},
            {"request": request, "payload": request_payload, "run_url": run_url, "audit_scope": audit_scope},
            {"request": request, "request_payload": request_payload, "run_url": run_url, "audit_scope": audit_scope},
            {"request": request, "payload": request_payload},
            {"request": request, "request_payload": request_payload},
            {"payload": request_payload},
            {"request_payload": request_payload},
            {"run_url": run_url, "request_payload": request_payload},
            {"request": request},
            {},
        ]

        def _bind_kwargs(candidates: dict[str, object]) -> dict[str, object]:
            if signatures is None:
                return candidates
            try:
                bound = signatures.bind_partial(**candidates)
            except TypeError:
                bound = {}
            else:
                return dict(bound.arguments)
            if not isinstance(candidates, dict):
                return {}
            fallback: dict[str, object] = {}
            for key, value in candidates.items():
                if key in signatures.parameters:
                    fallback[key] = value
            return fallback

        for call_kwargs in call_payload_variants:
            bound = _bind_kwargs(call_kwargs)
            try:
                if bound:
                    candidate = callback(**bound)
                    if candidate is not None:
                        break
                else:
                    if signatures is not None and len(signatures.parameters) == 0:
                        candidate = callback()
                        if candidate is not None:
                            break
                    if signatures is not None:
                        continue
                    candidate = callback()
                    if candidate is not None:
                        break
            except TypeError as exc:
                message = str(exc)
                if "missing" in message and "required" in message:
                    raise
                continue
            if candidate is not None:
                break
        if candidate is None:
            return None
        if isinstance(candidate, ToolInvocationResult):
            return candidate
        if not isinstance(candidate, dict):
            return None
        safe_payload = dict(candidate)
        BrowserActToolAdapter._raise_for_ui_lane_failure(payload=safe_payload, backend="chatplayground")
        safe_payload.setdefault("requested_url", run_url)
        safe_payload.setdefault("requested_roles", list(roles))
        safe_payload.setdefault("roles", list(roles))
        safe_payload.setdefault("audit_scope", audit_scope)
        safe_payload.setdefault("prompt", prompt)
        action_kind = str(request.action_kind or "chatplayground_audit") or "chatplayground_audit"
        normalized_text = str(safe_payload.get("normalized_text") or json.dumps(safe_payload))
        requested_roles_raw = safe_payload.get("requested_roles") or safe_payload.get("roles") or roles
        try:
            requested_roles = [str(role).strip() for role in list(requested_roles_raw) if str(role).strip()]
        except Exception:
            requested_roles = list(roles)
            if not requested_roles:
                requested_roles = ["factuality", "adversarial", "completeness", "risk"]
        return ToolInvocationResult(
            tool_name=definition.tool_name,
            action_kind=action_kind,
            target_ref=str(safe_payload.get("target_ref") or "browseract:chatplayground_audit:callback"),
            output_json={
                **safe_payload,
                "tool_name": definition.tool_name,
                "action_kind": action_kind,
                "normalized_text": normalized_text,
                "preview_text": artifact_preview_text(normalized_text),
                "requested_url": str(safe_payload.get("requested_url") or run_url),
                "requested_roles": requested_roles,
                "audit_scope": str(safe_payload.get("audit_scope") or audit_scope),
                "mime_type": "text/plain",
                "structured_output_json": safe_payload,
            },
            receipt_json={
                "handler_key": definition.tool_name,
                "invocation_contract": "tool.v1",
                "tool_version": definition.version,
                "tool_name": definition.tool_name,
                "action_kind": action_kind,
                "requested_url": str(safe_payload.get("requested_url") or run_url),
                "requested_roles": requested_roles,
                "audit_scope": str(safe_payload.get("audit_scope") or audit_scope),
                "route": "browseract.chatplayground_audit",
                "handler": "callback",
            },
        )

    def execute_gemini_web_generate(
        self,
        request: ToolInvocationRequest,
        definition: ToolDefinition,
    ) -> ToolInvocationResult:
        payload = dict(request.payload_json or {})
        principal_id, binding = self._resolve_browseract_binding(
            request=request,
            payload=payload,
            required_input_error="connector_binding_required:browseract.gemini_web_generate",
            required_scopes=None,
        )
        packet = payload.get("packet")
        if not isinstance(packet, dict) or not packet:
            raise ToolExecutionError(f"packet_required:{definition.tool_name}")
        binding_metadata = dict(binding.auth_metadata_json or {})
        run_url = str(
            payload.get("run_url")
            or binding_metadata.get("gemini_web_run_url")
            or binding_metadata.get("browseract_gemini_web_run_url")
            or os.environ.get("BROWSERACT_GEMINI_WEB_URL", "").strip()
            or ""
        ).strip()
        mode = str(payload.get("mode") or "thinking").strip().lower() or "thinking"
        if mode not in {"thinking", "fast", "pro"}:
            mode = "thinking"
        deep_think = bool(payload.get("deep_think"))
        try:
            timeout_seconds = max(60, min(1800, int(payload.get("timeout_seconds") or 600)))
        except Exception:
            timeout_seconds = 600

        callback = getattr(self, "_gemini_web_generate", None)
        if callback is not None:
            callback_result = self._safe_call_gemini_web_generate_callback(
                callback=callback,
                request=request,
                payload=payload,
                definition=definition,
                packet=packet,
                mode=mode,
                deep_think=deep_think,
                run_url=run_url,
            )
            if callback_result is not None:
                return callback_result

        if run_url:
            response = self._post_browseract_json(
                run_url=run_url,
                request_payload={
                    "packet": packet,
                    "mode": mode,
                    "deep_think": deep_think,
                    "timeout_seconds": timeout_seconds,
                    "principal_id": principal_id,
                    "binding_id": binding.binding_id,
                    "external_account_ref": binding.external_account_ref,
                },
                timeout_seconds=timeout_seconds,
            )
            self._raise_for_ui_lane_failure(payload=response, backend="gemini_web")
            text = _extract_textish(
                response.get("text")
                or response.get("answer")
                or response.get("result")
                or response.get("normalized_text")
            )
            if text:
                action_kind = str(request.action_kind or "content.generate") or "content.generate"
                return ToolInvocationResult(
                    tool_name=definition.tool_name,
                    action_kind=action_kind,
                    target_ref=f"browseract:{binding.binding_id}:gemini_web_generate:{uuid.uuid4()}",
                    output_json={
                        "binding_id": binding.binding_id,
                        "connector_name": binding.connector_name,
                        "external_account_ref": binding.external_account_ref,
                        "text": text,
                        "mode_used": str(response.get("mode_used") or mode),
                        "deep_think": bool(response.get("deep_think", deep_think)),
                        "requested_url": run_url,
                        "provider_backend": "gemini_web",
                        "citations": list(response.get("citations") or []) if isinstance(response.get("citations"), list) else [],
                        "latency_ms": int(response.get("latency_ms") or 0),
                        "normalized_text": text,
                        "preview_text": artifact_preview_text(text),
                        "mime_type": "text/plain",
                        "structured_output_json": dict(response),
                        "tool_name": definition.tool_name,
                        "action_kind": action_kind,
                    },
                    receipt_json={
                        "binding_id": binding.binding_id,
                        "connector_name": binding.connector_name,
                        "external_account_ref": binding.external_account_ref,
                        "principal_id": principal_id,
                        "handler_key": definition.tool_name,
                        "invocation_contract": "tool.v1",
                        "tool_version": definition.version,
                        "requested_url": run_url,
                        "mode_used": str(response.get("mode_used") or mode),
                        "provider_backend": "gemini_web",
                        "route": "browseract.gemini_web_generate",
                        "handler": "run_url",
                    },
                )

        raise ToolExecutionError("browseract_gemini_web_generate_unavailable")

    def _post_browseract_json(
        self,
        *,
        run_url: str,
        request_payload: dict[str, object],
        timeout_seconds: int,
    ) -> dict[str, object]:
        api_key = self._configured_api_key()
        if not run_url or not api_key:
            raise ToolExecutionError("browseract_run_url_or_key_missing")
        request = urllib.request.Request(
            run_url,
            data=json.dumps(request_payload).encode("utf-8"),
            headers={"authorization": f"Bearer {api_key}", "content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            raise ToolExecutionError(f"browseract_live_http_error:{exc.code}") from exc
        except urllib.error.URLError as exc:
            raise ToolExecutionError(f"browseract_live_transport_error:{exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise ToolExecutionError("browseract_live_response_invalid") from exc
        if isinstance(body, dict):
            return {str(key): value for key, value in body.items()}
        raise ToolExecutionError("browseract_live_response_invalid")

    @staticmethod
    def _safe_call_gemini_web_generate_callback(
        *,
        callback,
        request: ToolInvocationRequest,
        payload: dict[str, object],
        definition: ToolDefinition,
        packet: dict[str, object],
        mode: str,
        deep_think: bool,
        run_url: str,
    ) -> ToolInvocationResult | None:
        request_payload = dict(payload)
        request_payload["packet"] = dict(packet)
        request_payload["mode"] = mode
        request_payload["deep_think"] = deep_think
        request_payload["run_url"] = run_url
        signatures = None
        try:
            signatures = inspect.signature(callback)
        except Exception:
            signatures = None

        def _bind_kwargs(candidates: dict[str, object]) -> dict[str, object]:
            if signatures is None:
                return candidates
            try:
                bound = signatures.bind_partial(**candidates)
            except TypeError:
                bound = {}
            else:
                return dict(bound.arguments)
            fallback: dict[str, object] = {}
            for key, value in candidates.items():
                if key in signatures.parameters:
                    fallback[key] = value
            return fallback

        variants = (
            {"request": request, "payload": request_payload, "run_url": run_url},
            {"request_payload": request_payload, "run_url": run_url},
            {"payload": request_payload},
            {"request": request},
            {},
        )
        candidate = None
        for call_kwargs in variants:
            bound = _bind_kwargs(call_kwargs)
            try:
                if bound:
                    candidate = callback(**bound)
                else:
                    if signatures is not None and len(signatures.parameters) > 0:
                        continue
                    candidate = callback()
            except TypeError as exc:
                message = str(exc)
                if "missing" in message and "required" in message:
                    raise
                continue
            if candidate is not None:
                break
        if candidate is None:
            return None
        if isinstance(candidate, ToolInvocationResult):
            return candidate
        if not isinstance(candidate, dict):
            return None
        safe_payload = dict(candidate)
        BrowserActToolAdapter._raise_for_ui_lane_failure(payload=safe_payload, backend="gemini_web")
        text = _extract_textish(
            safe_payload.get("text")
            or safe_payload.get("answer")
            or safe_payload.get("result")
            or safe_payload.get("normalized_text")
        )
        if not text:
            return None
        action_kind = str(request.action_kind or "content.generate") or "content.generate"
        return ToolInvocationResult(
            tool_name=definition.tool_name,
            action_kind=action_kind,
            target_ref=str(safe_payload.get("target_ref") or "browseract:gemini_web_generate:callback"),
            output_json={
                **safe_payload,
                "text": text,
                "normalized_text": text,
                "preview_text": artifact_preview_text(text),
                "mime_type": "text/plain",
                "mode_used": str(safe_payload.get("mode_used") or mode),
                "deep_think": bool(safe_payload.get("deep_think", deep_think)),
                "requested_url": str(safe_payload.get("requested_url") or run_url),
                "provider_backend": "gemini_web",
                "structured_output_json": safe_payload,
                "tool_name": definition.tool_name,
                "action_kind": action_kind,
            },
            receipt_json={
                "handler_key": definition.tool_name,
                "invocation_contract": "tool.v1",
                "tool_version": definition.version,
                "tool_name": definition.tool_name,
                "action_kind": action_kind,
                "requested_url": str(safe_payload.get("requested_url") or run_url),
                "mode_used": str(safe_payload.get("mode_used") or mode),
                "provider_backend": "gemini_web",
                "route": "browseract.gemini_web_generate",
                "handler": "callback",
            },
        )
