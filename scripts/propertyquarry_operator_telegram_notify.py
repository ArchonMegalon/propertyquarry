#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_TELEGRAM_TABLE_NAME = "ea_telegram_conversation_messages"
DEFAULT_ENV_FILES = (Path(".env"), Path("/docker/EA/.env"))


@dataclass(frozen=True)
class TelegramChatCandidate:
    chat_ref: str
    principal_id: str
    latest_at: str
    record_count: int

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.chat_ref.encode("utf-8")).hexdigest()[:12]


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_env_defaults(paths: list[Path]) -> None:
    for path in paths:
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key and key not in os.environ:
                os.environ[key] = value.strip().strip('"').strip("'")


def request_json(
    url: str,
    *,
    api_key: str = "",
    method: str = "GET",
    payload: dict[str, object] | None = None,
    opener: object | None = None,
    user_agent: str = "PropertyQuarryOperatorTelegramNotify/1.0",
    timeout_seconds: float = 20.0,
) -> Any:
    body = None
    headers = {
        "Accept": "application/json",
        "User-Agent": user_agent,
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        if opener is not None:
            response = opener.open(request, timeout=timeout_seconds)
        else:
            response = urllib.request.urlopen(request, timeout=timeout_seconds)
        with response:
            raw_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:240]
        raise RuntimeError(f"http_{exc.code}:{detail}") from exc
    except Exception as exc:
        raise RuntimeError(f"{type(exc).__name__}:{str(exc)[:240]}") from exc
    try:
        loaded = json.loads(raw_body or "{}")
    except Exception as exc:
        raise RuntimeError("json_decode_failed") from exc
    return loaded


def discover_teable_table_id(
    *,
    base_url: str,
    api_key: str,
    base_id: str,
    table_name: str = DEFAULT_TELEGRAM_TABLE_NAME,
    opener: object | None = None,
) -> str:
    normalized_base = str(base_url or "https://app.teable.ai").strip().rstrip("/")
    normalized_base_id = str(base_id or "").strip()
    normalized_table = str(table_name or "").strip().lower()
    if not normalized_base_id or not normalized_table:
        return ""
    payload = request_json(
        f"{normalized_base}/api/base/{urllib.parse.quote(normalized_base_id)}/table",
        api_key=api_key,
        opener=opener,
    )
    tables: list[dict[str, object]]
    if isinstance(payload, list):
        tables = [dict(row) for row in payload if isinstance(row, dict)]
    else:
        rows = payload.get("data") or payload.get("tables") or payload.get("items") or []
        tables = [dict(row) for row in rows if isinstance(row, dict)]
    for row in tables:
        candidate_name = str(row.get("name") or row.get("tableName") or "").strip().lower()
        if candidate_name == normalized_table:
            return str(row.get("id") or row.get("tableId") or "").strip()
    return ""


def fetch_teable_records(
    *,
    base_url: str,
    api_key: str,
    table_id: str,
    opener: object | None = None,
    take: int = 500,
) -> list[dict[str, object]]:
    normalized_base = str(base_url or "https://app.teable.ai").strip().rstrip("/")
    normalized_table_id = str(table_id or "").strip()
    if not normalized_table_id:
        return []
    rows: list[dict[str, object]] = []
    skip = 0
    page_size = max(1, min(int(take or 500), 1000))
    while True:
        query = urllib.parse.urlencode({"take": page_size, "skip": skip})
        payload = request_json(
            f"{normalized_base}/api/table/{urllib.parse.quote(normalized_table_id)}/record?{query}",
            api_key=api_key,
            opener=opener,
        )
        if not isinstance(payload, dict):
            break
        records = payload.get("records") or payload.get("data") or []
        page = [dict(record) for record in records if isinstance(record, dict)]
        rows.extend(page)
        if len(page) < page_size:
            break
        skip += page_size
    return rows


def chat_candidates_from_records(records: list[dict[str, object]], *, principal_id: str) -> list[TelegramChatCandidate]:
    normalized_principal = str(principal_id or "").strip()
    by_chat: dict[str, TelegramChatCandidate] = {}
    for record in records:
        fields = record.get("fields") if isinstance(record, dict) else {}
        if not isinstance(fields, dict):
            continue
        row_principal = str(fields.get("principal_id") or "").strip()
        if normalized_principal and row_principal != normalized_principal:
            continue
        chat_ref = str(fields.get("chat_ref") or fields.get("chat_id") or "").strip()
        if not _looks_like_chat_ref(chat_ref):
            continue
        latest_at = str(fields.get("message_timestamp") or fields.get("event_created_at") or fields.get("synced_at") or "").strip()
        existing = by_chat.get(chat_ref)
        if existing is None:
            by_chat[chat_ref] = TelegramChatCandidate(
                chat_ref=chat_ref,
                principal_id=row_principal,
                latest_at=latest_at,
                record_count=1,
            )
            continue
        by_chat[chat_ref] = TelegramChatCandidate(
            chat_ref=chat_ref,
            principal_id=existing.principal_id,
            latest_at=max(existing.latest_at, latest_at),
            record_count=existing.record_count + 1,
        )
    return sorted(
        by_chat.values(),
        key=lambda item: (_chat_rank(item.chat_ref), item.latest_at, item.record_count),
        reverse=True,
    )


def _looks_like_chat_ref(value: str) -> bool:
    stripped = str(value or "").strip()
    if not stripped:
        return False
    numeric = stripped[1:] if stripped.startswith("-") else stripped
    return numeric.isdigit() and len(numeric) >= 5


def _chat_rank(chat_ref: str) -> int:
    stripped = str(chat_ref or "").strip()
    numeric = stripped[1:] if stripped.startswith("-") else stripped
    if numeric.isdigit() and len(numeric) >= 8:
        return 2
    if numeric.isdigit():
        return 1
    return 0


def telegram_chat_reachable(*, bot_token: str, chat_ref: str, opener: object | None = None) -> tuple[bool, str, str]:
    token = str(bot_token or "").strip()
    chat = str(chat_ref or "").strip()
    if not token or not chat:
        return False, "", "telegram_token_or_chat_missing"
    url = f"https://api.telegram.org/bot{token}/getChat?chat_id={urllib.parse.quote(chat, safe='')}"
    try:
        payload = request_json(url, opener=opener)
    except RuntimeError as exc:
        return False, "", str(exc)
    if not isinstance(payload, dict):
        return False, "", "telegram_get_chat_invalid_response"
    if payload.get("ok") is True:
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        return True, str(result.get("type") or "").strip(), ""
    return False, "", str(payload.get("description") or "telegram_get_chat_failed")


def resolve_reachable_chat(
    *,
    candidates: list[TelegramChatCandidate],
    bot_token: str,
    opener: object | None = None,
) -> tuple[TelegramChatCandidate | None, list[dict[str, object]]]:
    attempts: list[dict[str, object]] = []
    for candidate in candidates:
        reachable, chat_type, reason = telegram_chat_reachable(
            bot_token=bot_token,
            chat_ref=candidate.chat_ref,
            opener=opener,
        )
        attempts.append(
            {
                "chat_hash": candidate.hash,
                "principal_id": candidate.principal_id,
                "latest_at": candidate.latest_at,
                "record_count": candidate.record_count,
                "reachable": reachable,
                "chat_type": chat_type,
                "reason": "" if reachable else reason,
            }
        )
        if reachable:
            return candidate, attempts
    return None, attempts


def send_telegram_message(
    *,
    bot_token: str,
    chat_ref: str,
    text: str,
    opener: object | None = None,
) -> dict[str, object]:
    token = str(bot_token or "").strip()
    chat = str(chat_ref or "").strip()
    message = str(text or "").strip()
    if not token or not chat or not message:
        return {"sent": False, "error": "telegram_token_chat_or_text_missing"}
    payload = {
        "chat_id": chat,
        "text": message,
        "disable_web_page_preview": True,
    }
    try:
        response = request_json(
            f"https://api.telegram.org/bot{token}/sendMessage",
            method="POST",
            payload=payload,
            opener=opener,
        )
    except RuntimeError as exc:
        return {"sent": False, "error": str(exc)}
    if not isinstance(response, dict):
        return {"sent": False, "error": "telegram_send_invalid_response"}
    result = response.get("result") if isinstance(response.get("result"), dict) else {}
    return {
        "sent": response.get("ok") is True,
        "error": "" if response.get("ok") is True else str(response.get("description") or "telegram_send_failed"),
        "message_id_present": bool(result.get("message_id")),
    }


def build_notification_receipt(
    *,
    principal_id: str,
    text: str,
    dry_run: bool,
    env_files: list[Path],
    teable_base_url: str,
    teable_api_key: str,
    teable_base_id: str,
    teable_table_id: str,
    teable_table_name: str,
    bot_token: str,
    opener: object | None = None,
) -> dict[str, object]:
    table_id = teable_table_id or discover_teable_table_id(
        base_url=teable_base_url,
        api_key=teable_api_key,
        base_id=teable_base_id,
        table_name=teable_table_name,
        opener=opener,
    )
    records = fetch_teable_records(
        base_url=teable_base_url,
        api_key=teable_api_key,
        table_id=table_id,
        opener=opener,
    )
    candidates = chat_candidates_from_records(records, principal_id=principal_id)
    selected, attempts = resolve_reachable_chat(
        candidates=candidates,
        bot_token=bot_token,
        opener=opener,
    )
    send_result = {"sent": False, "error": "dry_run"} if dry_run else {"sent": False, "error": "no_reachable_chat"}
    if selected is not None and not dry_run:
        send_result = send_telegram_message(
            bot_token=bot_token,
            chat_ref=selected.chat_ref,
            text=text,
            opener=opener,
        )
    status = "sent" if send_result.get("sent") is True else ("ready" if selected is not None and dry_run else "blocked")
    return {
        "contract_name": "propertyquarry.operator_telegram_notification.v1",
        "generated_at": _utc_now(),
        "status": status,
        "principal_id": str(principal_id or "").strip(),
        "dry_run": dry_run,
        "teable": {
            "base_url_host": urllib.parse.urlparse(teable_base_url).hostname or "",
            "base_id_present": bool(teable_base_id),
            "table_name": teable_table_name,
            "table_id_present": bool(table_id),
            "record_count": len(records),
            "candidate_count": len(candidates),
        },
        "telegram": {
            "bot_token_present": bool(bot_token),
            "selected_chat_hash": selected.hash if selected is not None else "",
            "selected_chat_latest_at": selected.latest_at if selected is not None else "",
            "selected_chat_record_count": selected.record_count if selected is not None else 0,
            "attempts": attempts[:8],
            **send_result,
        },
        "message": {
            "length": len(str(text or "")),
            "sha256": hashlib.sha256(str(text or "").encode("utf-8")).hexdigest(),
        },
        "env_files_checked": [str(path) for path in env_files],
    }


def _default_principal_id() -> str:
    return str(
        os.getenv("PROPERTYQUARRY_OPERATOR_PRINCIPAL_ID")
        or os.getenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID")
        or os.getenv("EA_DEFAULT_PRINCIPAL_ID")
        or "cf-email:tibor.girschele@gmail.com"
    ).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send a redacted operator Telegram notification using Teable chat history.")
    parser.add_argument("--principal-id", default="")
    parser.add_argument("--text", default="")
    parser.add_argument("--text-file", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--write", default="")
    parser.add_argument("--env-file", action="append", default=[])
    parser.add_argument("--teable-base-url", default="")
    parser.add_argument("--teable-api-key", default="")
    parser.add_argument("--teable-base-id", default="")
    parser.add_argument("--teable-table-id", default="")
    parser.add_argument("--teable-table-name", default=DEFAULT_TELEGRAM_TABLE_NAME)
    args = parser.parse_args(argv)

    env_files = [Path(path) for path in args.env_file] if args.env_file else list(DEFAULT_ENV_FILES)
    load_env_defaults(env_files)
    text = str(args.text or "").strip()
    if args.text_file:
        text = Path(args.text_file).read_text(encoding="utf-8").strip()
    if not text:
        raise SystemExit("missing --text or --text-file")
    receipt = build_notification_receipt(
        principal_id=str(args.principal_id or _default_principal_id()).strip(),
        text=text,
        dry_run=bool(args.dry_run),
        env_files=env_files,
        teable_base_url=str(args.teable_base_url or os.getenv("TEABLE_BASE_URL") or os.getenv("TEABLE_RUNTIME_BASE_URL") or "https://app.teable.ai").strip(),
        teable_api_key=str(args.teable_api_key or os.getenv("TEABLE_API_KEY") or "").strip(),
        teable_base_id=str(args.teable_base_id or os.getenv("EA_ENV_TEABLE_BASE_ID") or "").strip(),
        teable_table_id=str(args.teable_table_id or os.getenv("EA_TELEGRAM_CONVERSATION_MESSAGES_TEABLE_TABLE_ID") or "").strip(),
        teable_table_name=str(args.teable_table_name or DEFAULT_TELEGRAM_TABLE_NAME).strip(),
        bot_token=str(os.getenv("EA_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip(),
    )
    if args.write:
        output = Path(args.write)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(receipt, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=True, sort_keys=True))
    return 0 if receipt.get("status") in {"sent", "ready"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
