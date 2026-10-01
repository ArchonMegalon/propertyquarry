#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EA_ROOT = ROOT / "ea"
if str(EA_ROOT) not in sys.path:
    sys.path.insert(0, str(EA_ROOT))

from app.services.telegram_delivery import (
    _chunk_telegram_text,
    _telegram_bot_registry,
    _telegram_html_with_titled_links,
    _telegram_send_json,
    _telegram_visible_button_label,
    send_telegram_message_for_principal,
)
from app.services.tool_runtime import build_tool_runtime

if __package__:
    from scripts.propertyquarry_operator_action import (
        GOVERNED_NOTIFICATION_MAX_SOURCE_AGE_SECONDS,
        propertyquarry_operator_action_summary,
    )
else:
    from propertyquarry_operator_action import (
        GOVERNED_NOTIFICATION_MAX_SOURCE_AGE_SECONDS,
        propertyquarry_operator_action_summary,
    )

_FALLBACK_ENV_PATHS = (
    ROOT / ".env",
    Path("/docker/EA/.env"),
)
_CANONICAL_GOLD_RECEIPT_PATHS = (
    "_completion/property_gold_status/latest.json",
    "_completion/propertyquarry-gold-status-latest.json",
)
_DIRECT_CHAT_ENV_KEYS = (
    "PROPERTYQUARRY_GOLD_NOTIFY_TELEGRAM_CHAT_ID",
    "EA_PROACTIVE_OODA_TELEGRAM_CHAT_ID",
    "EA_TELEGRAM_DEFAULT_CHAT_ID",
)
_PREFER_CONTAINER_RUNTIME_ENV_KEYS = (
    "PROPERTYQUARRY_NOTIFICATION_PREFER_CONTAINER_RUNTIME",
    "PROPERTYQUARRY_GOLD_NOTIFICATION_PREFER_CONTAINER_RUNTIME",
    "PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_REFRESH_NOTIFICATION_PREFER_CONTAINER_RUNTIME",
)
_RUNTIME_CONTAINER_ENV_KEYS = (
    "PROPERTYQUARRY_API_CONTAINER_NAME",
    "PROPERTYQUARRY_GOLD_NOTIFICATION_RUNTIME_CONTAINER",
)
_MAX_NOTIFICATION_STATE_BYTES = 64 * 1024


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("json_root_not_object")
    return payload


def _load_notification_state(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return {}
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > _MAX_NOTIFICATION_STATE_BYTES
        ):
            return {}
        with os.fdopen(fd, encoding="utf-8") as stream:
            fd = -1
            payload = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    finally:
        if fd >= 0:
            os.close(fd)
    return payload if isinstance(payload, dict) else {}


def _write_notification_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o600)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _payload_digest(payload: dict[str, Any]) -> str:
    normalized = dict(payload)
    normalized.pop("generated_at", None)
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_dotenv_defaults(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


def _load_local_env_defaults() -> None:
    for path in _FALLBACK_ENV_PATHS:
        _load_dotenv_defaults(path)


def _env_truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on", "enabled"}


def _direct_chat_id() -> str:
    for key in _DIRECT_CHAT_ENV_KEYS:
        value = str(os.getenv(key) or "").strip()
        if value:
            return value
    return ""


def _runtime_container_name() -> str:
    for key in _RUNTIME_CONTAINER_ENV_KEYS:
        value = str(os.getenv(key) or "").strip()
        if value:
            return value
    return "propertyquarry-api"


def _prefer_container_runtime() -> bool:
    for key in _PREFER_CONTAINER_RUNTIME_ENV_KEYS:
        if key not in os.environ:
            continue
        value = str(os.getenv(key) or "").strip()
        if value:
            return _env_truthy(value)
    return False


def _resolve_receipt_path(raw_path: str) -> Path:
    requested = Path(str(raw_path or "").strip() or _CANONICAL_GOLD_RECEIPT_PATHS[0]).expanduser().resolve()
    if requested.is_file():
        return requested
    canonical_targets = {Path(path).expanduser().resolve() for path in _CANONICAL_GOLD_RECEIPT_PATHS}
    if requested in canonical_targets:
        for candidate_raw in _CANONICAL_GOLD_RECEIPT_PATHS:
            candidate = Path(candidate_raw).expanduser().resolve()
            if candidate.is_file():
                return candidate
    return requested


def _send_direct_telegram_message(
    *,
    chat_id: str,
    text: str,
    url_buttons: list[list[tuple[str, str]]] | None = None,
) -> dict[str, Any]:
    config = dict(_telegram_bot_registry().get("default") or {})
    token = str(config.get("token") or "").strip()
    if not token:
        raise RuntimeError("telegram_bot_token_missing")
    message_ids: list[str] = []
    rendered_text = _telegram_html_with_titled_links(text)
    for chunk in _chunk_telegram_text(rendered_text):
        payload: dict[str, object] = {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"}
        keyboard_rows: list[list[dict[str, str]]] = []
        for row in list(url_buttons or []):
            buttons = [
                {"text": _telegram_visible_button_label(str(label or ""), url=str(url or "")), "url": str(url or "").strip()}
                for label, url in row
                if (str(label or "").strip() or str(url or "").strip()) and str(url or "").strip()
            ]
            if buttons:
                keyboard_rows.append(buttons)
        if keyboard_rows:
            payload["reply_markup"] = {"inline_keyboard": keyboard_rows}
        result = _telegram_send_json(
            token=token,
            method="sendMessage",
            payload=payload,
        )
        message_ids.append(str(result.get("message_id") or ""))
    return {
        "bot_handle": str(config.get("handle") or "").strip(),
        "bot_key": "default",
        "chat_id": chat_id,
        "message_ids": [value for value in message_ids if value],
    }


def _send_container_runtime_telegram_message(
    *,
    principal_id: str,
    text: str,
    url_buttons: list[list[tuple[str, str]]] | None = None,
) -> dict[str, Any]:
    container_name = _runtime_container_name()
    runtime_script = "\n".join(
        (
            "from __future__ import annotations",
            "import json",
            "from app.services.telegram_delivery import send_telegram_message_for_principal",
            "from app.services.tool_runtime import build_tool_runtime",
            f"principal_id = {json.dumps(str(principal_id or '').strip())}",
            f"text = {json.dumps(str(text or ''))}",
            f"url_buttons = {json.dumps(list(url_buttons or []))}",
            "runtime = build_tool_runtime()",
            "receipt = send_telegram_message_for_principal(",
            "    runtime,",
            "    principal_id=principal_id,",
            "    text=text,",
            "    url_buttons=url_buttons,",
            ")",
            "print(json.dumps({'message_ids': list(receipt.message_ids)}))",
        )
    )
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            container_name,
            "/bin/sh",
            "-lc",
            "cd /app && PYTHONPATH=/app python -",
        ],
        input=runtime_script,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        stderr = str(result.stderr or "").strip()
        stdout = str(result.stdout or "").strip()
        detail = stderr or stdout or f"container_runtime_exit_{result.returncode}"
        raise RuntimeError(f"container_runtime_send_failed:{detail}")
    payload = json.loads(str(result.stdout or "{}").strip() or "{}")
    if not isinstance(payload, dict):
        raise RuntimeError("container_runtime_invalid_json")
    return {
        "container_name": container_name,
        "message_ids": [str(value) for value in list(payload.get("message_ids") or []) if str(value or "").strip()],
    }


def _build_message(*, action: dict[str, Any], receipt_path: Path, base_url: str) -> str:
    consent_gate = dict(action.get("consent_gate") or {})
    protected_operations = ", ".join(
        str(value).strip()
        for value in list(consent_gate.get("protected_operations") or [])
        if str(value).strip()
    )
    lines = [
        "PropertyQuarry operator action required.",
        f"Site: {base_url}",
        f"Reason: {str(action.get('reason') or '').strip()}",
        f"Source generated: {str(action.get('source_generated_at') or '').strip()}",
        f"Safe next action: {str(action.get('reversible_next_action') or '').strip()}",
        (
            "Consent gate: required; automatic execution disabled; "
            f"protected={protected_operations}"
        ),
        "Provider quota: disabled",
        f"Receipt: {receipt_path}",
    ]
    return "\n".join(lines)


def _receipt_ready_for_notification(
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    action = propertyquarry_operator_action_summary(
        payload,
        now=now,
        max_source_age_seconds=GOVERNED_NOTIFICATION_MAX_SOURCE_AGE_SECONDS,
    )
    return (
        action.get("action_required") is True
        and action.get("interrupt_operator") is True
        and action.get("notification_policy") == "action_required_only"
    )


def deliver_notification_for_principal(
    *,
    principal_id: str,
    text: str,
    url_buttons: list[list[tuple[str, str]]] | None = None,
    prefer_container_runtime: bool | None = None,
) -> dict[str, Any]:
    prefer_container = _prefer_container_runtime() if prefer_container_runtime is None else bool(prefer_container_runtime)
    runtime_error = ""
    container_runtime_error = ""
    delivery_mode = ""
    message_ids: list[str] = []

    if prefer_container:
        try:
            receipt = _send_container_runtime_telegram_message(
                principal_id=principal_id,
                text=text,
                url_buttons=url_buttons,
            )
            delivery_mode = "container_runtime_preferred"
            message_ids = [str(value) for value in list(receipt.get("message_ids") or []) if str(value or "").strip()]
        except Exception as exc:
            container_runtime_error = f"{type(exc).__name__}: {exc}"
        else:
            report: dict[str, Any] = {
                "delivery_mode": delivery_mode,
                "message_ids": message_ids,
            }
            if container_runtime_error:
                report["container_runtime_error"] = container_runtime_error
            return report

    try:
        runtime = build_tool_runtime()
    except Exception as exc:
        runtime_error = f"{type(exc).__name__}: {exc}"
        if not prefer_container:
            try:
                receipt = _send_container_runtime_telegram_message(
                    principal_id=principal_id,
                    text=text,
                    url_buttons=url_buttons,
                )
                delivery_mode = "container_runtime_fallback"
                message_ids = [str(value) for value in list(receipt.get("message_ids") or []) if str(value or "").strip()]
            except Exception as container_exc:
                container_runtime_error = f"{type(container_exc).__name__}: {container_exc}"
                chat_id = _direct_chat_id()
                if not chat_id:
                    raise
                receipt = _send_direct_telegram_message(
                    chat_id=chat_id,
                    text=text,
                    url_buttons=url_buttons,
                )
                delivery_mode = "direct_chat_fallback"
                message_ids = [str(value) for value in list(receipt.get("message_ids") or []) if str(value or "").strip()]
        else:
            chat_id = _direct_chat_id()
            if not chat_id:
                raise
            receipt = _send_direct_telegram_message(
                chat_id=chat_id,
                text=text,
                url_buttons=url_buttons,
            )
            delivery_mode = "direct_chat_fallback"
            message_ids = [str(value) for value in list(receipt.get("message_ids") or []) if str(value or "").strip()]
    else:
        try:
            receipt = send_telegram_message_for_principal(
                runtime,
                principal_id=principal_id,
                text=text,
                url_buttons=url_buttons,
            )
            delivery_mode = "principal_binding"
            message_ids = [str(value) for value in list(receipt.message_ids) if str(value or "").strip()]
        except Exception as exc:
            runtime_error = f"{type(exc).__name__}: {exc}"
            chat_id = _direct_chat_id()
            if not chat_id:
                raise
            receipt = _send_direct_telegram_message(
                chat_id=chat_id,
                text=text,
                url_buttons=url_buttons,
            )
            delivery_mode = "direct_chat_fallback"
            message_ids = [str(value) for value in list(receipt.get("message_ids") or []) if str(value or "").strip()]

    report = {
        "delivery_mode": delivery_mode,
        "message_ids": message_ids,
    }
    if runtime_error:
        report["runtime_error"] = runtime_error
    if container_runtime_error:
        report["container_runtime_error"] = container_runtime_error
    return report


def build_notification_report(
    *,
    payload: dict[str, Any],
    receipt_path: Path,
    state_path: Path,
    principal_id: str,
    base_url: str,
    force: bool,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    status = str(payload.get("status") or "").strip().lower()
    generated_at = str(payload.get("generated_at") or "").strip()
    action = propertyquarry_operator_action_summary(
        payload,
        now=now,
        max_source_age_seconds=GOVERNED_NOTIFICATION_MAX_SOURCE_AGE_SECONDS,
    )
    ready_for_notification = (
        action.get("action_required") is True
        and action.get("interrupt_operator") is True
        and action.get("notification_policy") == "action_required_only"
    )
    receipt_digest = _payload_digest(payload)
    action_digest = _payload_digest(action)
    consent_gate = dict(action.get("consent_gate") or {})
    report: dict[str, Any] = {
        "notification_kind": "operator_action_required",
        "receipt_path": str(receipt_path),
        "state_path": str(state_path),
        "principal_id": principal_id,
        "base_url": base_url,
        "status": status,
        "generated_at": generated_at,
        "ready_for_notification": ready_for_notification,
        "receipt_digest": receipt_digest,
        "action_digest": action_digest,
        "action_status": str(action.get("status") or "").strip(),
        "action_required": action.get("action_required") is True,
        "interrupt_operator": action.get("interrupt_operator") is True,
        "action_reason": str(action.get("reason") or "").strip(),
        "action_source_generated_at": str(action.get("source_generated_at") or "").strip(),
        "consent_required": consent_gate.get("required") is True,
        "automatic_execution_allowed": consent_gate.get("automatic_execution_allowed") is True,
        "provider_quota_consumption_allowed": action.get("provider_quota_consumption_allowed") is True,
        "sent": False,
        "would_send": False,
        "skipped_reason": "",
        "message_ids": [],
        "checked_at": _utc_now_iso(),
        "delivery_mode": "",
    }
    if not ready_for_notification:
        report["skipped_reason"] = "no_fresh_operator_action"
        return report

    if not force and state_path.is_file():
        prior = _load_notification_state(state_path)
        if str(prior.get("last_notified_digest") or "").strip() == action_digest:
            report["skipped_reason"] = "already_notified_same_digest"
            return report

    message = _build_message(action=action, receipt_path=receipt_path, base_url=base_url)
    report["would_send"] = True
    if dry_run:
        report["skipped_reason"] = "dry_run"
        report["message_preview"] = message
        return report

    url_buttons = [[("Open PropertyQuarry", base_url)]]
    delivery = deliver_notification_for_principal(
        principal_id=principal_id,
        text=message,
        url_buttons=url_buttons,
    )
    report["delivery_mode"] = str(delivery.get("delivery_mode") or "").strip()
    report["message_ids"] = [str(value) for value in list(delivery.get("message_ids") or []) if str(value or "").strip()]
    if delivery.get("runtime_error"):
        report["runtime_error"] = str(delivery.get("runtime_error") or "").strip()
    if delivery.get("container_runtime_error"):
        report["container_runtime_error"] = str(delivery.get("container_runtime_error") or "").strip()
    _write_notification_state(
        state_path,
        {
            "last_notified_at": _utc_now_iso(),
            "last_notified_digest": action_digest,
            "last_notified_status": "action_required",
            "last_receipt_status": status,
            "notification_kind": "operator_action_required",
            "last_action_reason": str(action.get("reason") or "").strip(),
            "last_action_source_generated_at": str(action.get("source_generated_at") or "").strip(),
            "last_receipt_path": str(receipt_path),
            "last_generated_at": generated_at,
            "principal_id": principal_id,
            "base_url": base_url,
            "message_ids": list(report["message_ids"]),
            "delivery_mode": report["delivery_mode"],
        },
    )
    report["sent"] = True
    return report


def main(argv: list[str] | None = None) -> int:
    _load_local_env_defaults()
    parser = argparse.ArgumentParser(
        description="Send a Telegram message only for a fresh PropertyQuarry operator action."
    )
    parser.add_argument(
        "--receipt",
        default="_completion/property_gold_status/latest.json",
        help="Gold receipt path to inspect.",
    )
    parser.add_argument(
        "--state-file",
        default="_completion/propertyquarry-gold-notification-state.json",
        help="Deduplication state file path.",
    )
    parser.add_argument(
        "--principal-id",
        default="cf-email:tibor.girschele@gmail.com",
        help="Principal id whose Telegram binding should receive the notification.",
    )
    parser.add_argument(
        "--base-url",
        default="https://propertyquarry.com",
        help="Public site URL to include in the message and button.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Send even if the same action digest was already notified.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate and print the sanitized notification without sending or writing state.",
    )
    parser.add_argument(
        "--write",
        default="",
        help="Optional JSON report path.",
    )
    args = parser.parse_args(argv)

    receipt_path = _resolve_receipt_path(str(args.receipt or ""))
    state_path = Path(args.state_file).expanduser().resolve()
    if not receipt_path.is_file():
        raise SystemExit(
            "Gold receipt not found: "
            f"{receipt_path} "
            f"(checked canonical aliases: {', '.join(_CANONICAL_GOLD_RECEIPT_PATHS)})"
        )

    payload = _load_json(receipt_path)
    report = build_notification_report(
        payload=payload,
        receipt_path=receipt_path,
        state_path=state_path,
        principal_id=str(args.principal_id or "").strip() or "cf-email:tibor.girschele@gmail.com",
        base_url=str(args.base_url or "").strip() or "https://propertyquarry.com",
        force=bool(args.force),
        dry_run=bool(args.dry_run),
    )
    output = json.dumps(report, indent=2, sort_keys=True)
    if args.write:
        out_path = Path(args.write)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
