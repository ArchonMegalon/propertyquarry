#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
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
API_BASE_URL_ENV_NAMES = (
    "PROPERTYQUARRY_OMAGIC_API_BASE_URL",
    "OMAGIC_API_BASE_URL",
    "PROPERTYQUARRY_MAGIC_API_BASE_URL",
    "MAGIC_API_BASE_URL",
    "PROPERTYQUARRY_MAGICAI_API_BASE_URL",
    "MAGICAI_API_BASE_URL",
)
TEMPLATE_VARIANT_ID_ENV_NAMES = (
    "PROPERTYQUARRY_OMAGIC_TEMPLATE_VARIANT_ID",
    "OMAGIC_TEMPLATE_VARIANT_ID",
    "PROPERTYQUARRY_MAGIC_TEMPLATE_VARIANT_ID",
    "MAGIC_TEMPLATE_VARIANT_ID",
)
TEMPLATE_ARGUMENT_NAME_ENV_NAMES = (
    "PROPERTYQUARRY_OMAGIC_TEMPLATE_ARGUMENT_NAME",
    "OMAGIC_TEMPLATE_ARGUMENT_NAME",
    "PROPERTYQUARRY_MAGIC_TEMPLATE_ARGUMENT_NAME",
    "MAGIC_TEMPLATE_ARGUMENT_NAME",
)
TEMPLATE_TEXT_ARGUMENT_NAME_ENV_NAMES = (
    "PROPERTYQUARRY_OMAGIC_TEMPLATE_TEXT_ARGUMENT_NAME",
    "OMAGIC_TEMPLATE_TEXT_ARGUMENT_NAME",
    "PROPERTYQUARRY_MAGIC_TEMPLATE_TEXT_ARGUMENT_NAME",
    "MAGIC_TEMPLATE_TEXT_ARGUMENT_NAME",
)
TEMPLATE_ASPECT_RATIO_ARGUMENT_NAME_ENV_NAMES = (
    "PROPERTYQUARRY_OMAGIC_TEMPLATE_ASPECT_RATIO_ARGUMENT_NAME",
    "OMAGIC_TEMPLATE_ASPECT_RATIO_ARGUMENT_NAME",
    "PROPERTYQUARRY_MAGIC_TEMPLATE_ASPECT_RATIO_ARGUMENT_NAME",
    "MAGIC_TEMPLATE_ASPECT_RATIO_ARGUMENT_NAME",
)
MODEL_ROTATION_ENV_NAMES = (
    "PROPERTYQUARRY_OMAGIC_MODEL_ROTATION_DEGREES",
    "OMAGIC_MODEL_ROTATION_DEGREES",
    "PROPERTYQUARRY_MAGIC_MODEL_ROTATION_DEGREES",
    "MAGIC_MODEL_ROTATION_DEGREES",
)
DEFAULT_API_BASE_URL = "https://api.omagic.ai"
DEFAULT_TEMPLATES_PAGE_SIZE = 100
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_ASPECT_RATIO = "16:9"
SUCCESS_STATUSES = {"done", "ready", "completed"}
PENDING_STATUSES = {"pending", "pending_upload", "processing", "rendering", "queued", "in_progress"}
FAILED_STATUSES = {"failed", "error", "cancelled", "canceled"}


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
    print(json.dumps(payload, sort_keys=True), file=os.sys.stderr)
    return 1


def _load_request(path: str) -> dict[str, Any]:
    request_path = Path(path).expanduser()
    loaded = json.loads(request_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("magicai_request_json_invalid")
    return loaded


def _normalize_api_base_url(value: str) -> str:
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return DEFAULT_API_BASE_URL
    host = parsed.netloc.lower()
    if host in {"app.omagic.ai", "platform.omagic.ai"}:
        return DEFAULT_API_BASE_URL
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def _resolve_api_base_url() -> tuple[str, str]:
    name, value = _first_env(API_BASE_URL_ENV_NAMES)
    if value:
        return name, _normalize_api_base_url(value)
    return "", DEFAULT_API_BASE_URL


def _load_model_file(request_payload: dict[str, Any]) -> tuple[Path | None, str]:
    model_path = str(request_payload.get("model_path") or "").strip()
    model_url = str(request_payload.get("model_url") or "").strip()
    if model_path:
        resolved = Path(model_path).expanduser()
        if not resolved.is_file():
            raise FileNotFoundError("magicai_model_path_missing")
        return resolved, ""
    if model_url:
        parsed = urlparse(model_url)
        if parsed.scheme in {"http", "https"}:
            return None, model_url
        resolved = Path(model_url).expanduser()
        if resolved.is_file():
            return resolved, ""
    raise FileNotFoundError("magicai_model_input_missing")


def _request_headers(api_key: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Origin": "https://platform.omagic.ai",
        "Referer": "https://platform.omagic.ai/platform/docs",
        "User-Agent": "Mozilla/5.0",
        "X-Api-Key": api_key,
    }


def _extract_template_field(field_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    field_type = str(spec.get("input_type") or spec.get("type") or "").strip().lower()
    label = str(spec.get("label") or "").strip()
    required = bool(spec.get("required") is True)
    options = list(spec.get("options") or []) if isinstance(spec.get("options"), list) else []
    return {
        "id": field_id,
        "type": field_type,
        "label": label,
        "required": required,
        "options": options,
    }


def _score_template_candidate(candidate: dict[str, Any]) -> int:
    text = " ".join(
        [
            str(candidate.get("title") or ""),
            str(candidate.get("slug") or ""),
            str(candidate.get("description") or ""),
            " ".join(str(value) for value in (candidate.get("categories") or [])),
        ]
    ).lower()
    score = 0
    preferred_terms = (
        ("apartment", 140),
        ("interior", 130),
        ("living", 120),
        ("bedroom", 120),
        ("kitchen", 120),
        ("house", 120),
        ("home", 120),
        ("property", 120),
        ("real estate", 120),
        ("architecture", 110),
        ("architectural", 110),
        ("showroom", 100),
        ("studio", 80),
        ("podium", 60),
        ("white background", 55),
        ("black background", 50),
    )
    avoided_terms = (
        ("times square", -120),
        ("billboard", -120),
        ("street", -90),
        ("crowd", -90),
        ("balloon", -80),
        ("beach", -80),
        ("canal", -80),
        ("amphitheater", -80),
        ("metro", -80),
        ("bridge", -70),
        ("city", -60),
        ("manhattan", -60),
    )
    for token, weight in preferred_terms:
        if token in text:
            score += weight
    for token, weight in avoided_terms:
        if token in text:
            score += weight
    if candidate.get("preview_video"):
        score += 5
    d3_args = list(candidate.get("d3_args") or [])
    score += 40 if len(d3_args) == 1 else 10 if d3_args else -1000
    if not candidate.get("required_other_args"):
        score += 60
    else:
        score -= 200 * len(candidate["required_other_args"])
    if candidate.get("aspect_ratio_arg"):
        score += 10
    if candidate.get("text_args"):
        score += 5
    return score


def _discover_template(session: requests.Session, *, api_base_url: str) -> dict[str, Any]:
    configured_variant_name, configured_variant_id = _first_env(TEMPLATE_VARIANT_ID_ENV_NAMES)
    configured_arg_name_name, configured_argument_name = _first_env(TEMPLATE_ARGUMENT_NAME_ENV_NAMES)
    configured_text_name_name, configured_text_argument_name = _first_env(TEMPLATE_TEXT_ARGUMENT_NAME_ENV_NAMES)
    configured_aspect_name_name, configured_aspect_argument_name = _first_env(TEMPLATE_ASPECT_RATIO_ARGUMENT_NAME_ENV_NAMES)
    if configured_variant_id and configured_argument_name:
        return {
            "template_variant_id": configured_variant_id,
            "d3_argument_name": configured_argument_name,
            "text_argument_name": configured_text_argument_name,
            "aspect_ratio_argument_name": configured_aspect_argument_name,
            "selection_source": "env_override",
            "selection_env_names": [
                name
                for name in (
                    configured_variant_name,
                    configured_arg_name_name,
                    configured_text_name_name,
                    configured_aspect_name_name,
                )
                if name
            ],
        }

    candidates: list[dict[str, Any]] = []
    page = 1
    hits = None
    while True:
        response = session.get(
            f"{api_base_url}/api/templates/v2/",
            params={"page_size": DEFAULT_TEMPLATES_PAGE_SIZE, "page": page},
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            break
        templates = list(payload.get("templates") or payload.get("results") or [])
        if not templates:
            break
        hits = int(payload.get("hits") or hits or 0)
        for template in templates:
            variants = list(template.get("variants") or [])
            for variant in variants:
                template_args = variant.get("template_args") or {}
                if not isinstance(template_args, dict):
                    continue
                fields = [_extract_template_field(str(field_id), dict(spec or {})) for field_id, spec in template_args.items() if isinstance(spec, dict)]
                d3_args = [field["id"] for field in fields if field["type"] in {"d3", "model_3d"}]
                if not d3_args:
                    continue
                text_args = [field["id"] for field in fields if field["type"] == "text"]
                aspect_ratio_arg = next(
                    (
                        field["id"]
                        for field in fields
                        if field["type"] == "enum"
                        and "aspect" in str(field["label"] or "").lower()
                        and DEFAULT_ASPECT_RATIO in field["options"]
                    ),
                    "",
                )
                required_other_args = [
                    field["id"]
                    for field in fields
                    if field["required"]
                    and field["id"] not in d3_args
                    and field["id"] not in text_args
                    and field["id"] != aspect_ratio_arg
                ]
                candidate = {
                    "template_id": str(template.get("id") or ""),
                    "template_variant_id": str(variant.get("id") or ""),
                    "title": str(template.get("title") or ""),
                    "slug": str(template.get("slug") or ""),
                    "description": str(template.get("description") or ""),
                    "categories": list(template.get("category_names") or []),
                    "preview_video": str(variant.get("preview_video") or template.get("preview_video") or ""),
                    "d3_args": d3_args,
                    "text_args": text_args,
                    "aspect_ratio_arg": aspect_ratio_arg,
                    "required_other_args": required_other_args,
                }
                candidate["score"] = _score_template_candidate(candidate)
                candidates.append(candidate)
        if hits is not None and page * DEFAULT_TEMPLATES_PAGE_SIZE >= hits:
            break
        page += 1

    filtered = [candidate for candidate in candidates if not candidate.get("required_other_args")]
    pool = filtered or candidates
    if not pool:
        raise RuntimeError("magicai_template_discovery_failed")
    best = max(
        pool,
        key=lambda candidate: (
            int(candidate.get("score") or 0),
            bool(candidate.get("aspect_ratio_arg")),
            bool(candidate.get("text_args")),
            candidate.get("title") or "",
        ),
    )
    return {
        "template_id": best.get("template_id") or "",
        "template_title": best.get("title") or "",
        "template_slug": best.get("slug") or "",
        "template_variant_id": best.get("template_variant_id") or "",
        "d3_argument_name": (best.get("d3_args") or [""])[0],
        "text_argument_name": (best.get("text_args") or [""])[0],
        "aspect_ratio_argument_name": best.get("aspect_ratio_arg") or "",
        "selection_source": "catalog_discovery",
        "candidate_score": int(best.get("score") or 0),
    }


def _model_rotation_degrees() -> str:
    _, raw = _first_env(MODEL_ROTATION_ENV_NAMES)
    if not raw:
        return "0"
    try:
        return str(float(raw))
    except Exception:
        return "0"


def _prompt_value(request_payload: dict[str, Any]) -> str:
    prompt = str(request_payload.get("prompt") or "").strip()
    title = str(request_payload.get("title") or "").strip()
    if title and prompt:
        return f"{title}: {prompt}"[:240]
    return (title or prompt)[:240]


def _prepare_library_upload(
    session: requests.Session,
    *,
    api_base_url: str,
    model_file_path: Path,
) -> dict[str, Any]:
    extension = model_file_path.suffix.lstrip(".").lower() or "glb"
    response = session.post(
        f"{api_base_url}/api/library/items/",
        json={
            "extension": extension,
            "original_filename": model_file_path.name,
        },
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("magicai_library_prepare_response_invalid")
    return payload


def _upload_library_file(*, upload_url: str, model_file_path: Path) -> None:
    with model_file_path.open("rb") as handle:
        response = requests.put(
            upload_url,
            data=handle,
            headers={"Content-Type": "application/octet-stream"},
            timeout=180,
        )
    response.raise_for_status()


def _complete_library_upload(
    session: requests.Session,
    *,
    api_base_url: str,
    library_item_id: int,
) -> dict[str, Any]:
    response = session.post(f"{api_base_url}/api/library/items/{library_item_id}/", timeout=60)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("magicai_library_complete_response_invalid")
    return payload


def _poll_library_item(
    session: requests.Session,
    *,
    api_base_url: str,
    library_item_id: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    deadline = time.time() + max(30, timeout_seconds)
    last_payload: dict[str, Any] = {}
    while time.time() < deadline:
        response = session.get(f"{api_base_url}/api/library/items/{library_item_id}/", timeout=60)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            last_payload = payload
        status = str(last_payload.get("status") or "").strip().lower()
        if status in SUCCESS_STATUSES:
            return last_payload
        if status in FAILED_STATUSES:
            raise RuntimeError(str(last_payload.get("error_message") or last_payload.get("error_description") or "magicai_render_failed"))
        time.sleep(DEFAULT_POLL_INTERVAL_SECONDS)
    raise TimeoutError("magicai_library_item_timeout")


def _submit_render_task_v2(
    session: requests.Session,
    *,
    api_base_url: str,
    request_payload: dict[str, Any],
    selection: dict[str, Any],
    input_library_item_id: int,
) -> dict[str, Any]:
    arguments: dict[str, dict[str, object]] = {
        str(selection.get("d3_argument_name") or ""): {
            "value": int(input_library_item_id),
            "rotation": float(_model_rotation_degrees()),
        }
    }
    if selection.get("aspect_ratio_argument_name"):
        arguments[str(selection["aspect_ratio_argument_name"])] = {"value": DEFAULT_ASPECT_RATIO}
    prompt_value = _prompt_value(request_payload)
    text_argument_name = str(selection.get("text_argument_name") or "").strip()
    if prompt_value and text_argument_name:
        arguments[text_argument_name] = {"value": prompt_value}
    response = session.post(
        f"{api_base_url}/api/render/tasks/v2/new/",
        json={
            "template_variant_id": str(selection.get("template_variant_id") or ""),
            "arguments": arguments,
        },
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("magicai_render_task_v2_response_invalid")
    return payload


def _materialize_remote_model(url: str) -> Path:
    response = requests.get(url, timeout=180, stream=True)
    response.raise_for_status()
    suffix = Path(urlparse(url).path).suffix or ".bin"
    fd, raw_path = tempfile.mkstemp(prefix="magicai-model-", suffix=suffix)
    os.close(fd)
    target = Path(raw_path)
    with target.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=1024 * 256):
            if chunk:
                handle.write(chunk)
    return target


def _request_output_path(payload: dict[str, Any], fallback_out: str) -> str:
    return str(payload.get("output_path") or fallback_out or "").strip()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render a PropertyQuarry model walkthrough through the MagicAI/OMagic API.")
    parser.add_argument("--request-json", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--state-json", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    state_json = str(args.state_json or "").strip()
    api_key_name, api_key = _first_env(API_KEY_ENV_NAMES)
    if not api_key:
        return _fail("magicai_api_key_missing", state_json=state_json)
    api_base_name, api_base_url = _resolve_api_base_url()
    request_payload = _load_request(args.request_json)
    model_file_path = None
    downloaded_model_path = None
    try:
        local_model_path, model_url = _load_model_file(request_payload)
        if local_model_path is None and model_url:
            downloaded_model_path = _materialize_remote_model(model_url)
            model_file_path = downloaded_model_path
            model_url = ""
        else:
            model_file_path = local_model_path
        session = requests.Session()
        session.headers.update(_request_headers(api_key))
        selection = _discover_template(session, api_base_url=api_base_url)
        if model_file_path is None:
            raise RuntimeError("magicai_model_file_missing")
        prepared_upload = _prepare_library_upload(
            session,
            api_base_url=api_base_url,
            model_file_path=model_file_path,
        )
        input_library_item_id = int(prepared_upload.get("id") or 0)
        upload_url = str(prepared_upload.get("upload_url") or "").strip()
        if input_library_item_id <= 0 or not upload_url:
            raise RuntimeError("magicai_library_upload_prepare_missing_fields")
        _upload_library_file(upload_url=upload_url, model_file_path=model_file_path)
        completed_upload = _complete_library_upload(
            session,
            api_base_url=api_base_url,
            library_item_id=input_library_item_id,
        )
        input_status = str(completed_upload.get("status") or "").strip().lower()
        if input_status not in SUCCESS_STATUSES:
            if input_status in FAILED_STATUSES:
                raise RuntimeError(str(completed_upload.get("error_message") or completed_upload.get("error_description") or "magicai_library_upload_failed"))
            completed_upload = _poll_library_item(
                session,
                api_base_url=api_base_url,
                library_item_id=input_library_item_id,
                timeout_seconds=300,
            )
        created = _submit_render_task_v2(
            session,
            api_base_url=api_base_url,
            request_payload=request_payload,
            selection=selection,
            input_library_item_id=input_library_item_id,
        )
        output_library_item_id = int(created.get("id") or 0)
        if output_library_item_id <= 0:
            raise RuntimeError("magicai_render_output_item_id_missing")
        initial_status = str(created.get("status") or "").strip().lower()
        final_payload = created
        if initial_status not in SUCCESS_STATUSES:
            if initial_status in FAILED_STATUSES:
                raise RuntimeError(str(created.get("error_message") or created.get("error_description") or "magicai_render_failed"))
            final_payload = _poll_library_item(
                session,
                api_base_url=api_base_url,
                library_item_id=output_library_item_id,
                timeout_seconds=int(request_payload.get("timeout_seconds") or 900),
            )
        result = {
            "provider_key": "omagic",
            "provider_backend_key": "omagic",
            "render_status": "completed",
            "task_id": output_library_item_id,
            "task_status": str(final_payload.get("status") or ""),
            "download_url": str(final_payload.get("download_url") or final_payload.get("result") or final_payload.get("file") or "").strip(),
            "preview_url": str(final_payload.get("preview") or final_payload.get("result_preview") or "").strip(),
            "page_url": str(final_payload.get("page_url") or "").strip(),
            "video_output_path": _request_output_path(request_payload, args.out),
            "model_input_consumed": True,
            "model_input_consumption_proof": "magicai_library_upload_then_render_task_v2",
            "input_library_item_id": input_library_item_id,
            "output_library_item_id": output_library_item_id,
            "template_variant_id": str(selection.get("template_variant_id") or ""),
            "template_id": str(selection.get("template_id") or ""),
            "template_title": str(selection.get("template_title") or ""),
            "template_slug": str(selection.get("template_slug") or ""),
            "template_d3_argument_name": str(selection.get("d3_argument_name") or ""),
            "template_text_argument_name": str(selection.get("text_argument_name") or ""),
            "template_aspect_ratio_argument_name": str(selection.get("aspect_ratio_argument_name") or ""),
            "template_selection_source": str(selection.get("selection_source") or ""),
            "candidate_score": int(selection.get("candidate_score") or 0),
            "api_key_env_name": api_key_name,
            "api_base_url_env_name": api_base_name,
            "api_base_url": api_base_url,
        }
        if not result["download_url"]:
            raise RuntimeError("magicai_download_url_missing")
        _write_state(state_json, result)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001
        return _fail(str(exc or exc.__class__.__name__)[:500] or "magicai_adapter_error", state_json=state_json)
    finally:
        if downloaded_model_path is not None and downloaded_model_path.exists():
            downloaded_model_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
