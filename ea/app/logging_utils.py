from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
import re
import traceback
from typing import Any, Mapping


_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:authorization|cookie|token|password|passwd|secret|api[_-]?key|database[_-]?url)"
)
_URI_CREDENTIAL_RE = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/@:]+(?::[^\s/@]*)?@"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_EMAIL_ADDRESS_RE = re.compile(
    r"(?i)(?<![a-z0-9._%+-])[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,63}(?![a-z0-9.-])"
)
_SENSITIVE_INLINE_RE = re.compile(
    r"(?i)\b(?P<key>authorization|cookie|set-cookie|[a-z0-9_.-]*(?:token|password|passwd|secret|api[_-]?key|database[_-]?url)[a-z0-9_.-]*)"
    r"(?P<separator>\s*[:=]\s*)(?P<value>\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_STRUCTURED_FIELDS = {
    "event",
    "correlation_id",
    "trace_id",
    "span_id",
    "parent_span_id",
    "trace_flags",
    "trace_source",
    "method",
    "route",
    "status_code",
    "status_class",
    "duration_seconds",
    "error_type",
    "error_detail",
    "role",
    "component",
    "operation",
    "outcome",
    "job_id",
    "run_id",
    "replica_id",
    "release_commit_sha",
    "release_image_digest",
    "exception_type",
    "exception_message",
    "exception_stack",
}
_TELEMETRY_BOUND_FIELDS = {
    "correlation_id",
    "trace_id",
    "span_id",
    "release_commit_sha",
    "release_image_digest",
    "replica_id",
}


def redact_log_text(value: object) -> str:
    text = str(value or "")
    text = _URI_CREDENTIAL_RE.sub(r"\g<scheme>***@", text)
    text = _BEARER_RE.sub("Bearer ***", text)

    def _replace_sensitive(match: re.Match[str]) -> str:
        return f"{match.group('key')}{match.group('separator')}***"

    text = _SENSITIVE_INLINE_RE.sub(_replace_sensitive, text)
    return _EMAIL_ADDRESS_RE.sub("[redacted-email]", text)


def redact_log_value(value: Any, *, key: str = "") -> Any:
    if key and _SENSITIVE_KEY_RE.search(str(key)):
        return "***"
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_log_value(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [redact_log_value(item) for item in value]
    if isinstance(value, str):
        return redact_log_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_log_text(value)


class RedactingJsonFormatter(logging.Formatter):
    """One-line JSON formatter that never serializes unreviewed LogRecord fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "level": str(record.levelname or "INFO").lower(),
            "logger": str(record.name or "root"),
            "message": redact_log_text(record.getMessage()),
            "service": "propertyquarry",
        }
        fields = getattr(record, "propertyquarry_fields", {})
        bound_telemetry_fields = (
            {
                key: fields[key]
                for key in _TELEMETRY_BOUND_FIELDS
            }
            if isinstance(fields, Mapping)
            and all(key in fields for key in _TELEMETRY_BOUND_FIELDS)
            else {}
        )
        if bound_telemetry_fields:
            telemetry_fields = bound_telemetry_fields
        else:
            try:
                from app.telemetry import current_telemetry_log_fields

                telemetry_fields = current_telemetry_log_fields()
            except Exception:
                telemetry_fields = {}
        for key in _TELEMETRY_BOUND_FIELDS:
            if key in telemetry_fields:
                payload[key] = redact_log_value(
                    telemetry_fields[key],
                    key=key,
                )
        if isinstance(fields, Mapping):
            for key, value in fields.items():
                normalized_key = str(key or "").strip()
                if (
                    normalized_key in _STRUCTURED_FIELDS
                    and not normalized_key.startswith("exception_")
                    and not (
                        normalized_key in _TELEMETRY_BOUND_FIELDS
                        and normalized_key in payload
                    )
                ):
                    payload[normalized_key] = redact_log_value(value, key=normalized_key)
            structured_exception_type = str(fields.get("exception_type") or "").strip()
            structured_exception_stack = str(fields.get("exception_stack") or "").strip()
            if structured_exception_type or structured_exception_stack:
                payload["exception"] = {
                    "type": structured_exception_type,
                    "message": redact_log_text(fields.get("exception_message") or ""),
                    "stack": redact_log_text(structured_exception_stack),
                }
        if record.exc_info:
            exception_type = ""
            exception_message = ""
            if record.exc_info[0] is not None:
                exception_type = str(record.exc_info[0].__name__)
            if record.exc_info[1] is not None:
                exception_message = redact_log_text(record.exc_info[1])
            payload["exception"] = {
                "type": exception_type,
                "message": exception_message,
                "stack": redact_log_text(self.formatException(record.exc_info)),
            }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def exception_log_fields(exc: BaseException) -> dict[str, str]:
    """Return a pre-redacted traceback safe for every attached handler."""

    return {
        "exception_type": exc.__class__.__name__,
        "exception_message": redact_log_text(exc),
        "exception_stack": redact_log_text(
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        ),
    }


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    exc_info: Any = None,
    **fields: Any,
) -> None:
    structured_fields = {"event": str(event or "runtime_event"), **fields}
    try:
        from app.telemetry import current_telemetry_log_fields

        structured_fields.update(current_telemetry_log_fields())
    except Exception:
        pass
    logger.log(
        level,
        str(event or "runtime_event"),
        extra={"propertyquarry_fields": structured_fields},
        exc_info=exc_info,
    )


def configure_logging(level: str = "INFO") -> None:
    configured_level = getattr(logging, str(level or "INFO").upper(), logging.INFO)
    runtime_mode = str(os.environ.get("EA_RUNTIME_MODE") or "dev").strip().lower()
    requested_format = str(os.environ.get("EA_LOG_FORMAT") or "").strip().lower()
    use_json = runtime_mode == "prod" or requested_format in {"json", "structured"}
    handler = logging.StreamHandler()
    if use_json:
        handler.setFormatter(RedactingJsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
    logging.basicConfig(level=configured_level, handlers=[handler], force=True)
