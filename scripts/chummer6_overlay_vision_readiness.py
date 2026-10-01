#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from chummer6_runtime_config import load_local_env, load_runtime_overrides

LOCAL_ENV = load_local_env()
POLICY_ENV = load_runtime_overrides()
DEFAULT_MODEL = "llama3.2-vision:11b"


def env_value(name: str) -> str:
    return str(os.environ.get(name) or LOCAL_ENV.get(name) or POLICY_ENV.get(name) or "").strip()


def _boolish(value: object, *, default: bool = False) -> bool:
    if value is None:
        return default
    token = str(value).strip().lower()
    if not token:
        return default
    if token in {"1", "true", "yes", "on"}:
        return True
    if token in {"0", "false", "no", "off"}:
        return False
    return default


def overlay_model() -> str:
    configured = (
        env_value("CHUMMER6_OVERLAY_VISION_MODEL")
        or env_value("OLLAMA_VISION_MODEL")
        or env_value("CHUMMER6_OLLAMA_MODEL")
    )
    return str(configured or DEFAULT_MODEL).strip() or DEFAULT_MODEL


def _probe_timeout_seconds() -> float:
    raw = env_value("CHUMMER6_OLLAMA_PROBE_TIMEOUT_SECONDS") or "3"
    try:
        return max(0.5, min(30.0, float(raw)))
    except Exception:
        return 3.0


def _pull_timeout_seconds() -> float:
    raw = env_value("CHUMMER6_OLLAMA_PULL_TIMEOUT_SECONDS") or "900"
    try:
        return max(30.0, min(3600.0, float(raw)))
    except Exception:
        return 900.0


def _normalize_http_base_url(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if not re.match(r"^https?://", raw, re.IGNORECASE):
        raw = f"http://{raw}"
    parsed = urllib.parse.urlparse(raw)
    scheme = parsed.scheme or "http"
    netloc = parsed.netloc or parsed.path
    path = parsed.path if parsed.netloc else ""
    cleaned_path = path.rstrip("/")
    if cleaned_path.endswith("/api"):
        cleaned_path = cleaned_path[: -len("/api")]
    return urllib.parse.urlunparse((scheme, netloc, cleaned_path, "", "", "")).rstrip("/")


def overlay_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "EA-Chummer6-OllamaReadiness/1.0",
    }
    client_id = (
        env_value("OLLAMA_CF_ACCESS_CLIENT_ID")
        or env_value("COMFYUI_CF_ACCESS_CLIENT_ID")
        or env_value("CF_ACCESS_CLIENT_ID")
    )
    client_secret = (
        env_value("OLLAMA_CF_ACCESS_CLIENT_SECRET")
        or env_value("COMFYUI_CF_ACCESS_CLIENT_SECRET")
        or env_value("CF_ACCESS_CLIENT_SECRET")
    )
    if client_id and client_secret:
        headers["CF-Access-Client-Id"] = client_id
        headers["CF-Access-Client-Secret"] = client_secret
    return headers


def candidate_base_urls() -> list[str]:
    candidates: list[str] = []

    def _add(value: object) -> None:
        normalized = _normalize_http_base_url(value)
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    for value in (
        env_value("CHUMMER6_OLLAMA_URL"),
        env_value("OLLAMA_URL"),
        env_value("OLLAMA_HOST"),
    ):
        _add(value)

    comfyui_url = env_value("COMFYUI_URL")
    if comfyui_url:
        parsed = urllib.parse.urlparse(comfyui_url)
        host = str(parsed.hostname or "").strip()
        scheme = str(parsed.scheme or "https").strip() or "https"
        if host:
            _add(f"http://{host}:11434")
            _add(f"{scheme}://{host}/ollama")
            _add(f"{scheme}://{host}/ollama/api")
    return candidates


def json_request(
    *,
    base_url: str,
    path: str,
    payload: dict[str, object] | None,
    method: str,
    timeout_seconds: float,
) -> tuple[object | None, str]:
    url = f"{str(base_url or '').rstrip('/')}/{str(path or '').lstrip('/')}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        headers=overlay_headers(),
        data=data,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8", errors="replace").strip()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace").strip()
        return None, f"http_{exc.code}:{body[:220]}"
    except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        detail = getattr(exc, "reason", exc)
        return None, f"urlerror:{str(detail)[:220]}"
    if not body:
        return None, "empty_response"
    try:
        return json.loads(body), ""
    except Exception:
        return None, f"invalid_json:{body[:220]}"


def list_models(base_url: str, *, timeout_seconds: float | None = None) -> tuple[list[str] | None, str]:
    payload, detail = json_request(
        base_url=base_url,
        path="/api/tags",
        payload=None,
        method="GET",
        timeout_seconds=timeout_seconds or _probe_timeout_seconds(),
    )
    if payload is None:
        return None, detail
    if not isinstance(payload, dict):
        return None, "invalid_tags_payload"
    rows = payload.get("models")
    if not isinstance(rows, list):
        return [], ""
    models: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if name and name not in models:
            models.append(name)
    return models, ""


def model_is_ready(*, model: str, models: list[str]) -> bool:
    normalized = str(model or "").strip()
    if not normalized:
        return False
    wanted_base = normalized.split(":", 1)[0]
    return any(
        str(entry or "").strip() == normalized
        or str(entry or "").strip().split(":", 1)[0] == wanted_base
        for entry in models
    )


def pull_model(*, base_url: str, model: str, timeout_seconds: float | None = None) -> tuple[bool, str]:
    payload, detail = json_request(
        base_url=base_url,
        path="/api/pull",
        payload={"name": str(model or "").strip(), "stream": False},
        method="POST",
        timeout_seconds=timeout_seconds or _pull_timeout_seconds(),
    )
    if payload is None:
        return False, detail or "pull_failed"
    return True, ""


def overlay_vision_readiness(*, model: str | None = None, pull: bool = False) -> dict[str, object]:
    target_model = str(model or overlay_model()).strip() or DEFAULT_MODEL
    candidates = candidate_base_urls()
    attempts: list[dict[str, object]] = []
    chosen_base_url = ""
    models: list[str] = []
    endpoint_error = ""

    for base_url in candidates:
        discovered_models, detail = list_models(base_url)
        attempts.append(
            {
                "base_url": base_url,
                "reachable": discovered_models is not None and not detail,
                "detail": detail,
                "models": list(discovered_models or []),
            }
        )
        if discovered_models is not None and not detail:
            chosen_base_url = base_url
            models = list(discovered_models)
            break
        if detail and not endpoint_error:
            endpoint_error = detail

    report: dict[str, object] = {
        "enabled": _boolish(env_value("CHUMMER6_OVERLAY_VISION_ENABLED"), default=False),
        "model": target_model,
        "candidate_base_urls": candidates,
        "attempts": attempts,
        "base_url": chosen_base_url,
        "endpoint_reachable": bool(chosen_base_url),
        "model_ready": False,
        "pull_attempted": False,
        "pull_succeeded": False,
        "status": "endpoint_unreachable",
        "detail": endpoint_error or ("no_candidate_urls" if not candidates else ""),
    }

    if not chosen_base_url:
        return report

    ready = model_is_ready(model=target_model, models=models)
    report["model_ready"] = ready
    report["status"] = "ready" if ready else "model_missing"
    report["detail"] = ""
    report["models"] = models

    if ready or not pull:
        return report

    report["pull_attempted"] = True
    ok, detail = pull_model(base_url=chosen_base_url, model=target_model)
    report["pull_succeeded"] = ok
    if not ok:
        report["detail"] = detail
        return report

    discovered_models, relist_detail = list_models(chosen_base_url, timeout_seconds=_probe_timeout_seconds())
    if discovered_models is not None and not relist_detail:
        models = list(discovered_models)
        report["models"] = models
        ready = model_is_ready(model=target_model, models=models)
        report["model_ready"] = ready
        report["status"] = "ready" if ready else "model_missing"
        report["detail"] = "" if ready else "model_not_visible_after_pull"
        return report

    report["status"] = "ready"
    report["model_ready"] = True
    report["detail"] = ""
    return report


def _human_summary(report: dict[str, object]) -> str:
    if report.get("status") == "ready":
        return (
            f"ready: {report.get('base_url')} model={report.get('model')} "
            f"pull_attempted={bool(report.get('pull_attempted'))}"
        )
    if report.get("status") == "model_missing":
        return f"model_missing: {report.get('base_url')} model={report.get('model')}"
    detail = str(report.get("detail") or "endpoint_unreachable").strip()
    return f"endpoint_unreachable: {detail}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe Chummer6 second-pass overlay vision readiness.")
    parser.add_argument("--model", default=overlay_model(), help="Vision model to require or pull.")
    parser.add_argument("--pull", action="store_true", help="Pull the configured model if the endpoint is reachable but the model is missing.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of a one-line summary.")
    args = parser.parse_args(argv)

    report = overlay_vision_readiness(model=args.model, pull=args.pull)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_human_summary(report))

    if report.get("status") == "ready":
        return 0
    if report.get("status") == "model_missing":
        return 3
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
