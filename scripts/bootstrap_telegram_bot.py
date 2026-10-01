from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path


def _env(name: str) -> str:
    return str(os.environ.get(name) or "").strip()


def _load_env_file(path: str) -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip()


def _telegram_bot_token() -> str:
    return _env("EA_TELEGRAM_BOT_TOKEN")


def _telegram_ingest_secret() -> str:
    return _env("EA_TELEGRAM_INGEST_SECRET")


def _public_app_base_url() -> str:
    return _env("EA_PUBLIC_APP_BASE_URL").rstrip("/")


def _telegram_bot_registry() -> dict[str, dict[str, str]]:
    registry: dict[str, dict[str, str]] = {}
    raw_registry = _env("EA_TELEGRAM_BOT_REGISTRY_JSON")
    if raw_registry:
        try:
            parsed = json.loads(raw_registry)
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            for raw_key, raw_value in parsed.items():
                key = str(raw_key or "").strip()
                if not key or not isinstance(raw_value, dict):
                    continue
                token = str(raw_value.get("token") or "").strip()
                if not token:
                    continue
                registry[key] = {
                    "token": token,
                    "handle": str(raw_value.get("handle") or "").strip(),
                    "secret": str(raw_value.get("secret") or "").strip(),
                }
    default_token = _telegram_bot_token()
    if default_token:
        registry.setdefault(
            "default",
            {
                "token": default_token,
                "handle": _env("EA_TELEGRAM_BOT_HANDLE"),
                "secret": _telegram_ingest_secret(),
            },
        )
    return registry


def _resolve_bot_config(bot_key: str) -> tuple[str, dict[str, str]]:
    registry = _telegram_bot_registry()
    normalized = str(bot_key or "").strip()
    if normalized:
        config = dict(registry.get(normalized) or {})
        if not config:
            raise SystemExit(f"telegram_bot_not_found:{normalized}")
        return normalized, config
    if "default" in registry:
        return "default", dict(registry["default"])
    if registry:
        resolved_key = next(iter(registry))
        return resolved_key, dict(registry[resolved_key])
    token = _telegram_bot_token()
    if not token:
        raise SystemExit("EA_TELEGRAM_BOT_TOKEN_missing")
    return "default", {"token": token, "handle": _env("EA_TELEGRAM_BOT_HANDLE"), "secret": _telegram_ingest_secret()}


def _webhook_url(bot_key: str = "") -> str:
    base = _public_app_base_url()
    if not base:
        raise SystemExit("EA_PUBLIC_APP_BASE_URL_missing")
    normalized = str(bot_key or "").strip()
    if normalized and normalized != "default":
        return f"{base}/v1/channels/telegram/ingest/{normalized}"
    return f"{base}/v1/channels/telegram/ingest"


def _telegram_api(method: str, payload: dict[str, object] | None = None, *, token: str = "") -> dict[str, object]:
    token = str(token or _telegram_bot_token()).strip()
    if not token:
        raise SystemExit("EA_TELEGRAM_BOT_TOKEN_missing")
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=data,
        headers=headers,
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap the EA Telegram bot webhook.")
    parser.add_argument("--env-file", default="/docker/property/.env", help="Optional dotenv file to load before bootstrapping.")
    parser.add_argument("--bot-key", default="", help="Optional bot registry key to target.")
    parser.add_argument("--all-bots", action="store_true", help="Apply the action to every configured bot registry entry.")
    parser.add_argument("--set-webhook", action="store_true", help="Register the Telegram webhook.")
    parser.add_argument("--drop-webhook", action="store_true", help="Delete the Telegram webhook.")
    parser.add_argument("--show", action="store_true", help="Show bot and webhook info.")
    args = parser.parse_args()

    _load_env_file(str(args.env_file or "").strip())

    actions = [args.set_webhook, args.drop_webhook, args.show]
    if not any(actions):
        args.show = True
    selected: list[tuple[str, dict[str, str]]]
    if args.all_bots:
        selected = [(key, dict(value)) for key, value in _telegram_bot_registry().items()]
        if not selected:
            raise SystemExit("telegram_bot_registry_empty")
    else:
        selected = [_resolve_bot_config(str(args.bot_key or "").strip())]

    for selected_key, config in selected:
        token = str(config.get("token") or "").strip()
        secret = str(config.get("secret") or "").strip()
        if args.show:
            print(
                json.dumps(
                    {
                        "bot_key": selected_key,
                        "webhook_url": _webhook_url(selected_key),
                        "getMe": _telegram_api("getMe", token=token),
                        "getWebhookInfo": _telegram_api("getWebhookInfo", token=token),
                    },
                    indent=2,
                )
            )

        if args.drop_webhook:
            print(json.dumps({"bot_key": selected_key, "result": _telegram_api("deleteWebhook", {"drop_pending_updates": False}, token=token)}, indent=2))

        if args.set_webhook:
            if not secret:
                raise SystemExit(f"EA_TELEGRAM_INGEST_SECRET_missing:{selected_key}")
            payload = {
                "url": _webhook_url(selected_key),
                "secret_token": secret,
                "allowed_updates": [
                    "message",
                    "edited_message",
                    "callback_query",
                    "my_chat_member",
                    "chat_member",
                ],
                "drop_pending_updates": False,
            }
            print(json.dumps({"bot_key": selected_key, "result": _telegram_api("setWebhook", payload, token=token)}, indent=2))
            print(json.dumps({"bot_key": selected_key, "webhook": _telegram_api("getWebhookInfo", token=token)}, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
