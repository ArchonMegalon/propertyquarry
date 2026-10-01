#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import json
import mimetypes
import os
import re
import signal
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests


ROOT = Path(__file__).resolve().parents[1] if "/app/scripts/" in str(Path(__file__).resolve()) else Path("/docker/property")
ENV_FILES = [
    ROOT / ".env",
    Path("/app/.env"),
    Path("/config/.env"),
]


@contextlib.contextmanager
def deadline(seconds: int, *, reason: str):
    normalized = max(1, int(seconds or 1))
    if not hasattr(signal, "SIGALRM"):
        yield
        return
    previous_handler = signal.getsignal(signal.SIGALRM)

    def _handle_timeout(_signum, _frame):  # noqa: ANN001
        raise TimeoutError(reason)

    signal.signal(signal.SIGALRM, _handle_timeout)
    signal.alarm(normalized)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))


def load_env() -> None:
    for path in ENV_FILES:
        load_env_file(path)


def onemin_keys() -> list[str]:
    keys: list[str] = []
    for name, value in sorted(os.environ.items()):
        if name == "ONEMIN_AI_API_KEY" or name.startswith("ONEMIN_AI_API_KEY_FALLBACK_"):
            normalized = str(value or "").strip()
            if normalized and normalized not in keys:
                keys.append(normalized)
    raw_json = str(os.environ.get("ONEMIN_DIRECT_API_KEYS_JSON") or "").strip()
    json_file = str(os.environ.get("ONEMIN_DIRECT_API_KEYS_JSON_FILE") or "").strip()
    if json_file:
        candidate = Path(json_file).expanduser()
        candidates = [candidate] if candidate.is_absolute() else [
            (ROOT / candidate).resolve(),
            (Path("/config") / candidate.name).resolve(),
            (Path("/app/config") / candidate.name).resolve(),
            candidate.resolve(strict=False),
        ]
        for candidate_path in candidates:
            if candidate_path.exists():
                raw_json = candidate_path.read_text(encoding="utf-8")
                break
    if raw_json:
        try:
            payload = json.loads(raw_json)
        except Exception:
            payload = []
        if isinstance(payload, dict):
            payload = (
                payload.get("slots")
                or payload.get("keys")
                or payload.get("api_keys")
                or payload.get("items")
                or payload.get("accounts")
                or []
            )
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    item = (
                        item.get("api_key")
                        or item.get("key")
                        or item.get("value")
                        or item.get("secret")
                        or item.get("token")
                    )
                normalized = str(item or "").strip()
                if normalized and normalized not in keys:
                    keys.append(normalized)
    return keys


def upload_asset(*, api_key: str, image_path: Path) -> str:
    content_type = mimetypes.guess_type(str(image_path))[0] or "image/jpeg"
    with image_path.open("rb") as handle:
        response = requests.post(
            "https://api.1min.ai/api/assets",
            headers={"API-KEY": api_key, "Accept": "application/json"},
            files={"asset": (image_path.name, handle, content_type)},
            timeout=180,
        )
    response.raise_for_status()
    payload = response.json()
    asset = payload.get("asset") if isinstance(payload, dict) else {}
    file_content = payload.get("fileContent") if isinstance(payload, dict) else {}
    if not isinstance(asset, dict):
        asset = {}
    if not isinstance(file_content, dict):
        file_content = {}
    image_url = str(file_content.get("path") or asset.get("key") or "").strip()
    if not image_url:
        raise RuntimeError("onemin_asset_missing_path")
    return image_url


def extract_urls(value: object) -> list[str]:
    urls: list[str] = []
    if isinstance(value, str):
        if re.match(r"^https?://", value):
            urls.append(value)
        urls.extend(re.findall(r"https?://[^\"'\\s<>]+", value))
    elif isinstance(value, dict):
        for item in value.values():
            urls.extend(extract_urls(item))
    elif isinstance(value, list):
        for item in value:
            urls.extend(extract_urls(item))
    return list(dict.fromkeys(urls))


def _normalize_result_url(value: str) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        return ""
    if re.match(r"^https?://", candidate):
        return candidate
    if candidate.startswith("/"):
        return "https://api.1min.ai" + candidate
    lowered = candidate.lower()
    if lowered.endswith((".mp4", ".webm", ".mov", ".m4v")) and not candidate.startswith(("..", "./")):
        return "https://api.1min.ai/" + candidate.lstrip("/")
    return ""


def extract_media_urls(value: object) -> list[str]:
    urls: list[str] = []
    if isinstance(value, str):
        normalized = _normalize_result_url(value)
        if normalized:
            urls.append(normalized)
        urls.extend(_normalize_result_url(url) for url in re.findall(r"https?://[^\"'\\s<>]+", value))
    elif isinstance(value, dict):
        for key in ("url", "video_url", "videoUrl", "download_url", "downloadUrl", "output", "path"):
            if key in value:
                urls.extend(extract_media_urls(value.get(key)))
        for item in value.values():
            urls.extend(extract_media_urls(item))
    elif isinstance(value, list):
        for item in value:
            urls.extend(extract_media_urls(item))
    return [url for url in dict.fromkeys(urls) if url]


def choose_video_url(payload: object) -> str:
    for url in extract_media_urls(payload) + extract_urls(payload):
        parsed = urlparse(url)
        if parsed.path.lower().endswith((".mp4", ".webm", ".mov", ".m4v")):
            return url
    return ""


def request_i2v(
    *,
    api_key: str,
    image_url: str,
    prompt: str,
    duration: int,
    model: str,
    timeout_seconds: int = 300,
) -> dict[str, object]:
    if model in {"veo3", "veo3-video", "veo3-video-fast"}:
        task_type = str(os.getenv("PROPERTYQUARRY_ONEMIN_VEO3_TASK_TYPE") or "veo3.1-video-fast").strip() or "veo3.1-video-fast"
        prompt_object: dict[str, object] = {
            "imageUrl": image_url,
            "prompt": prompt,
            "task_type": task_type,
            "generate_audio": False,
            "aspect_ratio": "16:9",
            "veo3_duration": "8s",
            "resolution": str(os.getenv("PROPERTYQUARRY_ONEMIN_VEO3_RESOLUTION") or "720p").strip() or "720p",
        }
        model = "veo3"
    elif model == "pika":
        prompt_object = {
            "imageUrl": image_url,
            "task_type": str(os.getenv("PROPERTYQUARRY_ONEMIN_PIKA_TASK_TYPE") or "pika-v2.2").strip() or "pika-v2.2",
            "prompt": prompt,
            "duration": 5 if duration <= 5 else 10,
            "resolution": str(os.getenv("PROPERTYQUARRY_ONEMIN_PIKA_RESOLUTION") or "720p").strip() or "720p",
            "negative_prompt": "cuts, transitions, speed ramps, morphing, flicker, text, watermark, blur, low quality",
        }
    elif model == "skyreels":
        prompt_object = {
            "imageUrl": image_url,
            "prompt": prompt,
            "negative_prompt": "cuts, transitions, speed ramps, morphing, flicker, text, watermark, blur, low quality",
            "aspect_ratio": "16:9",
            "guidance_scale": float(os.getenv("PROPERTYQUARRY_ONEMIN_SKYREELS_GUIDANCE_SCALE") or "3.5"),
        }
        model = "Qubico/skyreels"
    elif model == "hailuo":
        prompt_object = {
            "imageUrl": image_url,
            "taskType": str(os.getenv("PROPERTYQUARRY_ONEMIN_HAILUO_TASK_TYPE") or "i2v-02").strip() or "i2v-02",
            "prompt": prompt,
            "duration": 6 if duration <= 6 else 10,
            "resolution": int(os.getenv("PROPERTYQUARRY_ONEMIN_HAILUO_RESOLUTION") or "768"),
            "expand_prompt": False,
        }
    elif model == "luma":
        prompt_object = {
            "imageUrl": image_url,
            "prompt": prompt,
            "modelName": "ray-v2",
            "duration": "5s" if duration <= 5 else "10s",
            "aspectRatio": "16:9",
            "resolution": "720p",
            "loop": False,
        }
    else:
        prompt_object = {
            "imageUrl": image_url,
            "prompt": prompt,
            "duration": 5 if duration <= 5 else 10,
            "aspect_ratio": "16:9",
            "mode": str(os.getenv("PROPERTYQUARRY_ONEMIN_KLING_MODE") or "std").strip() or "std",
            "version": str(os.getenv("PROPERTYQUARRY_ONEMIN_KLING_VERSION") or "1.6").strip() or "1.6",
            "cfg_scale": 0.5,
            "negative_prompt": "cuts, transitions, speed ramps, morphing, flicker, text, watermark",
            "camera_control_type": "default",
        }
        model = "kling"
    body = {
        "type": "IMAGE_TO_VIDEO",
        "model": model,
        "conversationId": "IMAGE_TO_VIDEO",
        "promptObject": prompt_object,
    }
    response = requests.post(
        "https://api.1min.ai/api/features",
        headers={"API-KEY": api_key, "Content-Type": "application/json", "Accept": "application/json"},
        json=body,
        timeout=max(15, int(timeout_seconds or 300)),
    )
    if response.status_code >= 400:
        raise RuntimeError(f"onemin_i2v_http_{response.status_code}:{response.text[:500]}")
    payload = response.json()
    return payload if isinstance(payload, dict) else {"response": payload}


def download(url: str, out_path: Path) -> None:
    last_error: Exception | None = None
    retry_statuses = {404, 408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524}
    for attempt in range(1, 19):
        try:
            response = requests.get(url, timeout=300, stream=True)
            if response.status_code in retry_statuses and attempt < 18:
                time.sleep(min(60, 5 * attempt))
                continue
            response.raise_for_status()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 128):
                    if chunk:
                        handle.write(chunk)
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt >= 18:
                break
            time.sleep(min(60, 5 * attempt))
    if last_error is not None:
        raise last_error
    raise RuntimeError("download_failed")


def render_with_ea_one_manager(args: argparse.Namespace, *, image_path: Path) -> int:
    root = Path("/app")
    if str(root) not in sys.path and root.exists():
        sys.path.insert(0, str(root))
    try:
        from app.domain.models import ToolDefinition, ToolInvocationRequest
        from app.repositories.onemin_manager import build_onemin_manager_service_repo
        from app.services.onemin_manager import OneminManagerService, register_onemin_manager
        from app.services.tool_execution_onemin_adapter import OneminToolAdapter
    except Exception as exc:
        raise RuntimeError(f"ea_one_manager_import_failed:{exc}") from exc
    register_onemin_manager(OneminManagerService(repo=build_onemin_manager_service_repo()))
    definition = ToolDefinition(
        tool_name="provider.onemin.property_walkthrough_video",
        version="v1",
        input_schema_json={},
        output_schema_json={},
        policy_json={},
        allowed_channels=(),
        approval_default="none",
        enabled=True,
        updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    adapter = OneminToolAdapter()
    allow_reserve = str(os.getenv("PROPERTYQUARRY_ONEMIN_VIDEO_ALLOW_RESERVE") or "1").strip().lower() not in {"0", "false", "no", "off"}
    print(
        json.dumps(
            {
                "event": "ea_one_manager_property_video_start",
                "model_order": [
                    item.strip()
                    for item in str(args.model_order or args.model or "pika,skyreels,kling,hailuo").split(",")
                    if item.strip()
                ],
                "feature_timeout": int(args.feature_timeout or 300),
            }
        ),
        flush=True,
    )
    request = ToolInvocationRequest(
        session_id=f"property-render:{int(time.time())}",
        step_id=f"property-render-step:{int(time.time())}",
        tool_name=definition.tool_name,
        action_kind="video.generate",
        payload_json={
            "prompt": str(args.prompt or "").strip(),
            "first_frame_path": str(image_path),
            "duration": max(5, int(args.duration or 5)),
            "model": str(args.model or "pika").strip(),
            "model_order": [
                item.strip()
                for item in str(args.model_order or args.model or "pika,skyreels,kling,hailuo").split(",")
                if item.strip()
            ],
            "allow_reserve": allow_reserve,
            "timeout_seconds": int(args.feature_timeout or 300),
        },
        context_json={"principal_id": "propertyquarry-renderer"},
    )
    result = adapter.execute_property_walkthrough_video(request, definition)
    video_url = str(result.output_json.get("video_url") or result.output_json.get("asset_url") or "").strip()
    if not video_url:
        raise RuntimeError("ea_one_manager_video_url_missing")
    out_path = Path(args.out).expanduser().resolve()
    print(json.dumps({"event": "ea_one_manager_property_video_download", "url_host": urlparse(video_url).hostname or ""}), flush=True)
    download(video_url, out_path)
    receipt = {
        "provider": "EA One Manager / 1min.AI",
        "provider_key": "ea_one_manager_onemin_i2v",
        "model": str(result.output_json.get("model") or ""),
        "video_output_url": video_url,
        "output_file": str(out_path),
        "prompt": str(args.prompt or "").strip(),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "output_json": result.output_json,
        "receipt_json": result.receipt_json,
    }
    if args.state_json:
        Path(args.state_json).expanduser().resolve().write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps(receipt))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render a 1min.AI image-to-video property segment.")
    parser.add_argument("--first-frame", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--state-json", default="")
    parser.add_argument("--duration", type=int, default=5)
    parser.add_argument("--model", choices=["kling", "hailuo", "luma", "veo3", "pika", "skyreels"], default="kling")
    parser.add_argument(
        "--model-order",
        default="",
        help="Comma-separated model fallback order. Overrides --model when supplied.",
    )
    parser.add_argument("--feature-timeout", type=int, default=300)
    parser.add_argument("--max-keys", type=int, default=0)
    return parser


def main() -> int:
    load_env()
    args = build_parser().parse_args()
    image_path = Path(args.first_frame).expanduser().resolve()
    if not image_path.is_file():
        raise RuntimeError("first_frame_missing")
    use_manager = str(os.getenv("PROPERTYQUARRY_ONEMIN_USE_EA_MANAGER") or "1").strip().lower() not in {"0", "false", "no", "off"}
    if use_manager:
        return render_with_ea_one_manager(args, image_path=image_path)
    keys = onemin_keys()
    if args.max_keys and int(args.max_keys) > 0:
        keys = keys[: int(args.max_keys)]
    if not keys:
        raise RuntimeError("onemin_api_key_missing")
    model_order = [
        str(item or "").strip()
        for item in str(args.model_order or args.model or "kling").split(",")
        if str(item or "").strip()
    ]
    allowed_models = {"kling", "hailuo", "luma", "veo3", "pika", "skyreels"}
    model_order = [item for item in model_order if item in allowed_models] or [str(args.model or "kling").strip()]
    last_error = ""
    attempts: list[dict[str, object]] = []
    for api_key in keys:
        attempt_index = len(attempts) + 1
        attempt: dict[str, object] = {"index": attempt_index, "status": "started", "models": []}
        attempts.append(attempt)
        print(json.dumps({"event": "onemin_i2v_attempt_start", "index": attempt_index}), flush=True)
        try:
            image_url = upload_asset(api_key=api_key, image_path=image_path)
            attempt["asset_uploaded"] = True
            attempt["image_url"] = image_url
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            attempt["status"] = "failed"
            attempt["error"] = last_error[:500]
            print(json.dumps({"event": "onemin_i2v_attempt_failed", "index": attempt_index, "error": last_error[:220]}), flush=True)
            continue
        for model_name in model_order:
            model_attempt: dict[str, object] = {"model": model_name, "status": "started"}
            cast_models = attempt.get("models")
            if isinstance(cast_models, list):
                cast_models.append(model_attempt)
            print(
                json.dumps({"event": "onemin_i2v_model_start", "index": attempt_index, "model": model_name}),
                flush=True,
            )
            try:
                with deadline(int(args.feature_timeout or 300) + 30, reason=f"onemin_i2v_model_timeout:{model_name}"):
                    payload = request_i2v(
                        api_key=api_key,
                        image_url=image_url,
                        prompt=str(args.prompt or "").strip(),
                        duration=max(5, int(args.duration or 5)),
                        model=model_name,
                        timeout_seconds=int(args.feature_timeout or 300),
                    )
                model_attempt["raw_status"] = "returned"
                video_url = choose_video_url(payload)
                if not video_url:
                    raise RuntimeError("onemin_i2v_video_url_missing")
                out_path = Path(args.out).expanduser().resolve()
                download(video_url, out_path)
                model_attempt["status"] = "success"
                model_attempt["video_output_url"] = video_url
                receipt = {
                    "provider": "1min.AI",
                    "provider_key": "onemin_i2v",
                    "model": model_name,
                    "image_url": image_url,
                    "video_output_url": video_url,
                    "output_file": str(out_path),
                    "prompt": str(args.prompt or "").strip(),
                    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "raw_response": payload,
                    "attempt_count": len(attempts),
                    "attempts": attempts,
                }
                if args.state_json:
                    Path(args.state_json).expanduser().resolve().write_text(json.dumps(receipt, indent=2), encoding="utf-8")
                print(json.dumps(receipt))
                return 0
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                model_attempt["status"] = "failed"
                model_attempt["error"] = last_error[:500]
                print(
                    json.dumps(
                        {
                            "event": "onemin_i2v_model_failed",
                            "index": attempt_index,
                            "model": model_name,
                            "error": last_error[:220],
                        }
                    ),
                    flush=True,
                )
            continue
        attempt["status"] = "failed"
        attempt["error"] = last_error[:500]
        print(json.dumps({"event": "onemin_i2v_attempt_failed", "index": attempt_index, "error": last_error[:220]}), flush=True)
    if args.state_json:
        failure_receipt = {
            "provider": "1min.AI",
            "provider_key": "onemin_i2v",
            "status": "failed",
            "model": str(args.model or "kling").strip(),
            "output_file": str(Path(args.out).expanduser().resolve()),
            "attempt_count": len(attempts),
            "attempts": attempts,
            "error": last_error[:1000],
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        Path(args.state_json).expanduser().resolve().write_text(json.dumps(failure_receipt, indent=2), encoding="utf-8")
    raise RuntimeError(last_error or "onemin_i2v_failed")


if __name__ == "__main__":
    raise SystemExit(main())
