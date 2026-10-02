#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests


API_KEY_ENV_NAMES = (
    "PROPERTYQUARRY_OMAGIC_API_KEY",
    "OMAGIC_API_KEY",
    "PROPERTYQUARRY_MAGIC_API_KEY",
    "MAGIC_API_KEY",
)
ENDPOINT_ENV_NAMES = (
    "PROPERTYQUARRY_OMAGIC_RENDER_ENDPOINT",
    "OMAGIC_RENDER_ENDPOINT",
    "PROPERTYQUARRY_MAGIC_RENDER_ENDPOINT",
    "MAGIC_RENDER_ENDPOINT",
)
COMMAND_ENV_NAMES = (
    "PROPERTYQUARRY_OMAGIC_RENDER_COMMAND",
    "OMAGIC_RENDER_COMMAND",
    "PROPERTYQUARRY_MAGIC_RENDER_COMMAND",
    "MAGIC_RENDER_COMMAND",
)


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _first_env(names: tuple[str, ...]) -> tuple[str, str]:
    for name in names:
        value = str(os.getenv(name) or "").strip()
        if value:
            return name, value
    return "", ""


def _write_state(path: str, payload: dict[str, Any]) -> None:
    if not path:
        return
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _fail(reason: str, *, state_json: str = "", **extra: Any) -> int:
    payload = {
        "provider_key": "omagic",
        "provider_backend_key": "omagic",
        "render_status": "failed",
        "reason": reason,
        **extra,
    }
    _write_state(state_json, payload)
    print(json.dumps(payload, sort_keys=True), file=sys.stderr)
    return 1


def _load_json_line(text: str) -> dict[str, Any]:
    for raw_line in reversed(str(text or "").splitlines()):
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        try:
            loaded = json.loads(line)
        except Exception:
            continue
        if isinstance(loaded, dict):
            return loaded
    return {}


def _download_video(url: str, out_path: Path) -> None:
    response = requests.get(url, timeout=180, stream=True)
    response.raise_for_status()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=1024 * 128):
            if chunk:
                handle.write(chunk)


def _materialize_video_from_result(result: dict[str, Any], out_path: Path) -> str:
    for key in ("video_path", "output_path", "asset_path"):
        candidate = str(result.get(key) or "").strip()
        if not candidate:
            continue
        source = Path(candidate).expanduser()
        if source.is_file():
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(source.read_bytes())
            return str(out_path)
    for key in ("video_url", "asset_url", "download_url"):
        candidate = str(result.get(key) or "").strip()
        if not candidate:
            continue
        parsed = urlparse(candidate)
        if parsed.scheme in {"http", "https"}:
            _download_video(candidate, out_path)
            return candidate
        source = Path(candidate).expanduser()
        if source.is_file():
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(source.read_bytes())
            return str(out_path)
    return ""


def _run_command_adapter(*, command_text: str, request_payload: dict[str, Any], out_path: Path, state_json: str, timeout_seconds: int) -> dict[str, Any]:
    command = shlex.split(command_text)
    if not command:
        raise RuntimeError("omagic_render_command_missing")
    with tempfile.TemporaryDirectory(prefix="omagic-adapter-") as tmp_dir:
        request_path = Path(tmp_dir) / "request.json"
        request_path.write_text(json.dumps(request_payload, sort_keys=True), encoding="utf-8")
        adapter_state_path = Path(tmp_dir) / "state.json"
        env = {
            **os.environ,
            "OMAGIC_REQUEST_JSON": str(request_path),
            "OMAGIC_OUTPUT_PATH": str(out_path),
            "OMAGIC_STATE_JSON": str(adapter_state_path),
        }
        completed = subprocess.run(
            [
                *command,
                "--request-json",
                str(request_path),
                "--out",
                str(out_path),
                "--state-json",
                str(adapter_state_path),
            ],
            capture_output=True,
            text=True,
            timeout=max(30, timeout_seconds),
            check=False,
            env=env,
        )
        loaded_state: dict[str, Any] = {}
        if adapter_state_path.exists():
            with adapter_state_path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                loaded_state = loaded
        stdout_state = _load_json_line(completed.stdout)
        result = {**stdout_state, **loaded_state}
        result["adapter_returncode"] = int(completed.returncode or 0)
        if completed.returncode != 0:
            tail = str(completed.stderr or completed.stdout or "").strip().replace("\n", " ")
            result["adapter_error"] = tail[-500:]
            result.setdefault("reason", "omagic_command_adapter_failed")
        return result


def _run_endpoint_adapter(*, endpoint: str, api_key: str, request_payload: dict[str, Any], out_path: Path, timeout_seconds: int) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    model_path = str(request_payload.get("model_path") or "").strip()
    files = None
    data = None
    json_payload = request_payload
    opened = None
    try:
        if model_path and Path(model_path).is_file():
            opened = Path(model_path).open("rb")
            files = {"model_file": (Path(model_path).name, opened, "application/octet-stream")}
            data = {"metadata": json.dumps(request_payload, sort_keys=True)}
            json_payload = None
        response = requests.post(
            endpoint,
            headers=headers,
            json=json_payload,
            data=data,
            files=files,
            timeout=max(30, timeout_seconds),
        )
        response.raise_for_status()
        content_type = str(response.headers.get("content-type") or "").lower()
        if content_type.startswith("video/"):
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(response.content)
            return {
                "video_path": str(out_path),
                "video_url": "",
                "model_input_consumed": True,
                "model_input_consumption_proof": "omagic_http_multipart" if files else "omagic_http_json",
            }
        loaded = response.json()
        if not isinstance(loaded, dict):
            raise RuntimeError("omagic_endpoint_response_not_object")
        return loaded
    finally:
        if opened is not None:
            opened.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render an OMagic model-backed PropertyQuarry walkthrough.")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--state-json", default="")
    parser.add_argument("--model-path", default="")
    parser.add_argument("--model-url", default="")
    parser.add_argument("--model-asset-kind", default="model")
    parser.add_argument("--duration", type=int, default=15)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--title", default="")
    parser.add_argument("--tour-url", default="")
    return parser


def main() -> int:
    args = _parser().parse_args()
    out_path = Path(args.out).expanduser().resolve()
    state_json = str(args.state_json or "").strip()
    if not _truthy(os.getenv("PROPERTYQUARRY_OMAGIC_MODEL_UPLOAD_ENABLED")):
        return _fail("omagic_model_upload_adapter_disabled", state_json=state_json)
    model_path = str(args.model_path or "").strip()
    model_url = str(args.model_url or "").strip()
    if model_path and not Path(model_path).expanduser().is_file():
        return _fail("omagic_model_path_missing", state_json=state_json, model_path=model_path)
    if not model_path and not model_url:
        return _fail("omagic_model_input_missing", state_json=state_json)
    api_key_name, api_key = _first_env(API_KEY_ENV_NAMES)
    endpoint_name, endpoint = _first_env(ENDPOINT_ENV_NAMES)
    command_name, command = _first_env(COMMAND_ENV_NAMES)
    if not endpoint and not command:
        return _fail(
            "omagic_model_upload_endpoint_missing",
            state_json=state_json,
            endpoint_env_names=list(ENDPOINT_ENV_NAMES),
            command_env_names=list(COMMAND_ENV_NAMES),
        )
    request_payload = {
        "provider_key": "omagic",
        "provider_backend_key": "omagic",
        "prompt": str(args.prompt or "").strip(),
        "title": str(args.title or "").strip(),
        "tour_url": str(args.tour_url or "").strip(),
        "model_path": model_path,
        "model_url": model_url,
        "model_asset_kind": str(args.model_asset_kind or "model").strip() or "model",
        "duration_seconds": max(1, int(args.duration or 15)),
        "output_path": str(out_path),
    }
    try:
        if command:
            result = _run_command_adapter(
                command_text=command,
                request_payload=request_payload,
                out_path=out_path,
                state_json=state_json,
                timeout_seconds=int(args.timeout_seconds or 900),
            )
        else:
            result = _run_endpoint_adapter(
                endpoint=endpoint,
                api_key=api_key,
                request_payload=request_payload,
                out_path=out_path,
                timeout_seconds=int(args.timeout_seconds or 900),
            )
        if int(result.get("adapter_returncode") or 0) != 0:
            return _fail(
                str(result.get("reason") or "omagic_adapter_failed"),
                state_json=state_json,
                adapter_error=str(result.get("adapter_error") or "")[:500],
            )
        video_ref = _materialize_video_from_result(result, out_path)
        if not video_ref and not out_path.is_file():
            return _fail("omagic_video_output_missing", state_json=state_json, adapter_result_keys=sorted(result.keys()))
        if result.get("model_input_consumed") is not True:
            return _fail("omagic_model_input_not_consumed", state_json=state_json)
        state = {
            **result,
            "provider_key": "omagic",
            "provider_backend_key": "omagic",
            "render_status": str(result.get("render_status") or "completed").strip().lower() or "completed",
            "video_output_path": str(out_path) if out_path.is_file() else "",
            "video_output_url": str(result.get("video_url") or result.get("asset_url") or result.get("download_url") or video_ref or "").strip(),
            "model_input_consumed": True,
            "model_input_consumption_proof": str(
                result.get("model_input_consumption_proof")
                or ("omagic_command_adapter" if command else "omagic_http_adapter")
            ),
            "adapter_mode": "command" if command else "http",
            "adapter_config_env_name": command_name or endpoint_name,
            "api_key_env_name": api_key_name,
        }
        _write_state(state_json, state)
        print(json.dumps(state, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001
        return _fail(str(exc or exc.__class__.__name__)[:500] or "omagic_adapter_error", state_json=state_json)


if __name__ == "__main__":
    raise SystemExit(main())
