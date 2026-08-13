from __future__ import annotations

import json
import mimetypes
import os
import re
import signal
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import requests

from app.domain.models import ToolDefinition, ToolInvocationRequest, ToolInvocationResult
from app.services.tool_execution_common import ToolExecutionError


def _env_value(name: str) -> str:
    return str(os.environ.get(name) or "").strip()


def _preview_text(text: str, *, limit: int = 280) -> str:
    cleaned = " ".join(str(text or "").split()).strip()
    return cleaned[:limit]


def _strip_fences(text: str) -> str:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = raw.removeprefix("```json").removeprefix("```").strip()
    if raw.endswith("```"):
        raw = raw[:-3].strip()
    return raw


def _parse_structured(text: str) -> tuple[str, dict[str, Any], str]:
    cleaned = _strip_fences(text)
    try:
        loaded = json.loads(cleaned)
    except Exception:
        return cleaned, {}, "text/plain"
    if isinstance(loaded, dict):
        return json.dumps(loaded, indent=2, ensure_ascii=True), loaded, "application/json"
    return json.dumps(loaded, indent=2, ensure_ascii=True), {"result": loaded}, "application/json"


def _extract_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("text", "prompt", "source_text", "normalized_text", "diff_text", "instructions", "goal"):
            text = _extract_text(value.get(key))
            if text:
                return text
        return ""
    if isinstance(value, (list, tuple)):
        parts = [_extract_text(item) for item in value]
        return "\n".join(part for part in parts if part).strip()
    return str(value).strip()


def _first_nonempty(*values: object) -> str:
    for value in values:
        text = _extract_text(value)
        if text:
            return text
    return ""


def _bool_flag(value: object, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    cleaned = str(value).strip().lower()
    if not cleaned:
        return default
    if cleaned in {"1", "true", "yes", "on", "allow", "allowed"}:
        return True
    if cleaned in {"0", "false", "no", "off", "deny", "denied", "forbid", "forbidden"}:
        return False
    return default


_ONEMIN_VIDEO_PROVIDER_TIMEOUT_STATUSES = {504, 520, 522, 524}


def _onemin_video_gateway_timeout_retry_allowed() -> bool:
    return _bool_flag(_env_value("PROPERTYQUARRY_ONEMIN_VIDEO_RETRY_GATEWAY_TIMEOUTS"), default=False)


def _onemin_video_provider_timeout_status(status: int) -> bool:
    return int(status or 0) in _ONEMIN_VIDEO_PROVIDER_TIMEOUT_STATUSES


def _onemin_video_provider_timeout_error(value: object) -> bool:
    text = str(value or "").lower()
    return any(f"http_{status}" in text for status in _ONEMIN_VIDEO_PROVIDER_TIMEOUT_STATUSES)


def _parse_onemin_image_size(value: object) -> tuple[int, int] | None:
    text = str(value or "").strip().lower().replace("×", "x")
    if not text:
        return None
    match = re.fullmatch(r"(?P<width>\d{2,5})x(?P<height>\d{2,5})", text)
    if match is None:
        return None
    width = max(1, int(match.group("width")))
    height = max(1, int(match.group("height")))
    return width, height


def _estimate_onemin_feature_credits(feature_payload: dict[str, object], *, capability: str) -> int | None:
    normalized_capability = str(capability or "").strip().lower()
    if normalized_capability == "property_walkthrough_video":
        model = str(feature_payload.get("model") or "").strip().lower()
        prompt_object = dict(feature_payload.get("promptObject") or {})
        duration = prompt_object.get("duration") or prompt_object.get("veo3_duration") or 5
        try:
            seconds = int(float(str(duration).rstrip("s")))
        except Exception:
            seconds = 5
        base = {
            "qubico/skyreels": 450000,
            "skyreels": 450000,
            "pika": 600000,
            "hailuo": 750000,
            "kling": 780000,
            "veo3": 900000,
        }.get(model, 780000)
        return int(round(base * max(1.0, min(2.0, seconds / 5.0))))
    if normalized_capability not in {"image_generate", "media_transform"}:
        return None
    raw_override = _env_value("EA_ONEMIN_TOOL_ESTIMATED_IMAGE_CREDITS")
    if raw_override:
        try:
            return max(0, int(float(raw_override)))
        except Exception:
            pass
    prompt_object = dict(feature_payload.get("promptObject") or {})
    parsed_size = _parse_onemin_image_size(prompt_object.get("size"))
    if parsed_size is None:
        return 1200
    width, height = parsed_size
    megapixels = max(1.0, (float(width) * float(height)) / 1_000_000.0)
    return int(round(1200.0 * megapixels))


def _collect_asset_urls(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        candidate = value.strip()
        lowered = candidate.lower()
        if candidate.startswith("http://") or candidate.startswith("https://"):
            found.append(candidate)
        elif (
            candidate.startswith("/")
            and any(token in lowered for token in ("/asset/", "/image/", "/render/", "/download/"))
        ):
            found.append("https://api.1min.ai" + candidate)
        elif candidate.startswith("/") and lowered.endswith((".mp4", ".webm", ".mov", ".m4v")):
            found.append("https://api.1min.ai" + candidate)
        elif lowered.endswith((".mp4", ".webm", ".mov", ".m4v")) and not candidate.startswith(("..", "./")):
            found.append("https://api.1min.ai/" + candidate.lstrip("/"))
    elif isinstance(value, dict):
        for key in ("url", "image_url", "download_url", "image", "imageUrl", "asset_url", "assetUrl"):
            if key in value:
                found.extend(_collect_asset_urls(value.get(key)))
        for nested in value.values():
            found.extend(_collect_asset_urls(nested))
    elif isinstance(value, (list, tuple, set)):
        for nested in value:
            found.extend(_collect_asset_urls(nested))
    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in found:
        if candidate in seen:
            continue
        seen.add(candidate)
        deduped.append(candidate)
    return deduped


def _collect_video_urls(value: object) -> list[str]:
    prioritized: list[str] = []
    if isinstance(value, dict):
        for key in ("temporaryUrl", "temporary_url", "download_url", "downloadUrl", "video_url", "videoUrl"):
            if key in value:
                prioritized.extend(_collect_video_urls(value.get(key)))
        for nested in value.values():
            prioritized.extend(_collect_video_urls(nested))
    elif isinstance(value, (list, tuple, set)):
        for nested in value:
            prioritized.extend(_collect_video_urls(nested))
    elif isinstance(value, str):
        prioritized.extend(_collect_asset_urls(value))
    else:
        prioritized.extend(_collect_asset_urls(value))
    urls = [
        url
        for url in prioritized
        if str(url or "").lower().split("?", 1)[0].endswith((".mp4", ".webm", ".mov", ".m4v"))
    ]
    return list(dict.fromkeys(urls))


def _onemin_upload_asset(*, api_key: str, image_path: Path, timeout_seconds: int = 45) -> str:
    if not image_path.exists() or not image_path.is_file():
        raise ToolExecutionError("first_frame_path_missing:provider.onemin.property_walkthrough_video")
    content_type = mimetypes.guess_type(str(image_path))[0] or "image/jpeg"
    try:
        with image_path.open("rb") as handle:
            response = requests.post(
                "https://api.1min.ai/api/assets",
                headers={"API-KEY": api_key, "Accept": "application/json"},
                files={"asset": (image_path.name, handle, content_type)},
                timeout=(10, max(10, int(timeout_seconds or 45))),
            )
    except requests.Timeout as exc:
        raise ToolExecutionError("onemin_asset_upload_timeout") from exc
    except Exception as exc:
        raise ToolExecutionError(f"onemin_asset_upload_failed:{str(exc)[:220]}") from exc
    if response.status_code < 200 or response.status_code >= 300:
        raise ToolExecutionError(f"onemin_asset_upload_http_{response.status_code}:{response.text[:240]}")
    try:
        payload = response.json()
    except Exception as exc:
        raise ToolExecutionError("onemin_asset_upload_invalid_json") from exc
    asset = payload.get("asset") if isinstance(payload, dict) else {}
    file_content = payload.get("fileContent") if isinstance(payload, dict) else {}
    if not isinstance(asset, dict):
        asset = {}
    if not isinstance(file_content, dict):
        file_content = {}
    image_url = str(file_content.get("path") or asset.get("key") or "").strip()
    if not image_url:
        raise ToolExecutionError("onemin_asset_upload_missing_path")
    return image_url


class _OneminDeadline:
    def __init__(self, seconds: int, reason: str) -> None:
        self._seconds = max(1, int(seconds or 1))
        self._reason = str(reason or "onemin_deadline_exceeded")
        self._previous_handler = None
        self._enabled = False

    def __enter__(self):
        if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "SIGALRM"):
            return self
        self._previous_handler = signal.getsignal(signal.SIGALRM)

        def _handle_timeout(_signum, _frame):  # noqa: ANN001
            raise TimeoutError(self._reason)

        signal.signal(signal.SIGALRM, _handle_timeout)
        signal.alarm(self._seconds)
        self._enabled = True
        return self

    def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
        if self._enabled:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, self._previous_handler)
        return False


_PROMPT_OPTIONAL_MEDIA_FEATURES = {
    "BACKGROUND_REMOVER",
    "IMAGE_UPSCALER",
}


def _normalize_media_prompt_object(payload: dict[str, Any]) -> dict[str, object]:
    prompt_object = dict(payload.get("prompt_object") or {})
    image_url = _first_nonempty(
        prompt_object.get("imageUrl"),
        prompt_object.get("image_url"),
        payload.get("image_url"),
        payload.get("imageUrl"),
        payload.get("asset_url"),
        payload.get("assetUrl"),
        payload.get("source_image_url"),
        payload.get("url"),
    )
    if image_url and "imageUrl" not in prompt_object:
        prompt_object["imageUrl"] = image_url
    output_format = _first_nonempty(prompt_object.get("output_format"), payload.get("output_format"))
    if output_format and "output_format" not in prompt_object:
        prompt_object["output_format"] = output_format
    search_prompt = _first_nonempty(prompt_object.get("search_prompt"), payload.get("search_prompt"))
    if search_prompt and "search_prompt" not in prompt_object:
        prompt_object["search_prompt"] = search_prompt
    for key in ("background", "size", "quality"):
        value = _first_nonempty(prompt_object.get(key), payload.get(key))
        if value and key not in prompt_object:
            prompt_object[key] = value
    if "n" not in prompt_object and payload.get("n") is not None:
        prompt_object["n"] = payload.get("n")
    return prompt_object


def _infer_media_feature_type(payload: dict[str, Any]) -> str:
    from app.services.ltd_runtime_skill_projection import infer_onemin_media_feature_type

    return infer_onemin_media_feature_type(
        goal=_first_nonempty(payload.get("goal")),
        input_json=payload,
    )


class OneminToolAdapter:
    def _mark_onemin_feature_failure(self, upstream: Any, *, api_key: str, detail: str) -> None:
        cleaned_detail = str(detail or "").strip() or "onemin_feature_failed"
        if upstream._is_auth_error(cleaned_detail):
            quarantine_seconds = (
                upstream._deleted_onemin_key_quarantine_seconds()
                if upstream._is_deleted_onemin_key_error(cleaned_detail)
                else None
            )
            upstream._mark_onemin_failure(
                api_key,
                cleaned_detail,
                temporary_quarantine=True,
                quarantine_seconds=quarantine_seconds,
            )
            return
        upstream._mark_onemin_failure(api_key, cleaned_detail, temporary_quarantine=False)

    def _request_principal_id(self, request: ToolInvocationRequest) -> str:
        context_json = dict(request.context_json or {})
        return str(context_json.get("principal_id") or "").strip()

    def _manager_allow_reserve(self, request: ToolInvocationRequest) -> bool:
        payload = dict(request.payload_json or {})
        context_json = dict(request.context_json or {})
        for value in (
            payload.get("manager_allow_reserve"),
            payload.get("allow_reserve"),
            payload.get("use_reserve_slots"),
            context_json.get("manager_allow_reserve"),
            context_json.get("allow_reserve"),
            context_json.get("use_reserve_slots"),
        ):
            if value is not None:
                return _bool_flag(value, default=False)
        return False

    def _manager_candidates(
        self,
        *,
        upstream: Any,
        key_names: tuple[str, ...],
        filtered_key_names: tuple[str, ...],
        active_key_names: tuple[str, ...],
    ) -> list[dict[str, object]]:
        provider_health = upstream._provider_health_report()
        onemin_slots = list((((provider_health.get("providers") or {}).get("onemin") or {}).get("slots") or []))
        slot_by_account = {
            str(slot.get("account_name") or "").strip(): dict(slot)
            for slot in onemin_slots
            if isinstance(slot, dict) and str(slot.get("account_name") or "").strip()
        }
        slot_by_name = {
            str(slot.get("slot") or "").strip(): dict(slot)
            for slot in onemin_slots
            if isinstance(slot, dict) and str(slot.get("slot") or "").strip()
        }
        state_snapshot = upstream._onemin_states_snapshot(filtered_key_names)
        manager_candidates: list[dict[str, object]] = []
        for selected_key in filtered_key_names:
            account_name = upstream._provider_account_name("onemin", key_names=key_names, key=selected_key)
            slot_name = upstream._onemin_key_slot(selected_key, key_names=key_names)
            slot_row = slot_by_account.get(account_name) or slot_by_name.get(slot_name) or {}
            state = state_snapshot.get(selected_key)
            if state is None:
                state = upstream.OneminKeyState(key=selected_key)
            manager_candidates.append(
                {
                    "api_key": selected_key,
                    "account_id": account_name,
                    "account_name": account_name,
                    "credential_id": slot_name or account_name,
                    "slot_name": slot_name,
                    "secret_env_name": str(slot_row.get("slot_env_name") or account_name or ""),
                    "slot_role": slot_row.get("slot_role")
                    or upstream._onemin_slot_role_for_key(
                        selected_key,
                        active_keys=active_key_names,
                        reserve_keys=upstream._onemin_reserve_keys(),
                    ),
                    "state": slot_row.get("state") or upstream._onemin_key_state_label(state, now=upstream._now_epoch()),
                    "remaining_credits": slot_row.get("remaining_credits"),
                    "estimated_remaining_credits": slot_row.get("estimated_remaining_credits"),
                    "required_credits": slot_row.get("required_credits"),
                    "billing_remaining_credits": slot_row.get("billing_remaining_credits"),
                    "billing_max_credits": slot_row.get("billing_max_credits"),
                    "billing_next_topup_at": slot_row.get("billing_next_topup_at"),
                    "billing_team_mismatch": slot_row.get("billing_team_mismatch"),
                    "failure_count": state.failure_count,
                    "last_success_at": state.last_success_at,
                    "last_used_at": state.last_used_at,
                    "last_error": state.last_error,
                    "last_probe_result": slot_row.get("last_probe_result"),
                    "last_probe_detail": slot_row.get("last_probe_detail"),
                }
            )
        return manager_candidates

    def _default_code_model(self) -> str:
        from app.services import responses_upstream as upstream

        return _env_value("EA_ONEMIN_TOOL_CODE_MODEL") or next(iter(upstream._onemin_hard_models()), "gpt-5")

    def _default_review_model(self) -> str:
        from app.services import responses_upstream as upstream

        return _env_value("EA_ONEMIN_TOOL_REVIEW_MODEL") or next(iter(upstream._onemin_review_models()), "deepseek-chat")

    def _default_image_model(self) -> str:
        return _env_value("EA_ONEMIN_TOOL_IMAGE_MODEL") or "gpt-image-1-mini"

    def _default_media_model(self) -> str:
        return _env_value("EA_ONEMIN_TOOL_MEDIA_MODEL") or self._default_image_model()

    def _build_code_prompt(self, payload: dict[str, Any]) -> str:
        prompt = _extract_text(payload.get("prompt") or payload.get("source_text") or payload.get("normalized_text"))
        if not prompt:
            raise ToolExecutionError("prompt_required:provider.onemin.code_generate")
        instructions = _extract_text(payload.get("instructions"))
        goal = _extract_text(payload.get("goal"))
        context_pack = payload.get("context_pack")
        parts: list[str] = []
        if instructions:
            parts.append(instructions)
        if goal:
            parts.append(f"Goal: {goal}")
        if isinstance(context_pack, dict) and context_pack:
            parts.append("Context:\n" + json.dumps(context_pack, ensure_ascii=True))
        parts.append(prompt)
        return "\n\n".join(part for part in parts if part).strip()

    def _build_review_prompt(self, payload: dict[str, Any]) -> str:
        diff_text = _extract_text(payload.get("diff_text"))
        source_text = _extract_text(payload.get("source_text") or payload.get("normalized_text") or payload.get("prompt"))
        if not diff_text and not source_text:
            raise ToolExecutionError("review_material_required:provider.onemin.reasoned_patch_review")
        focus = _extract_text(payload.get("review_focus"))
        instructions = _extract_text(payload.get("instructions"))
        goal = _extract_text(payload.get("goal")) or "Review the proposed patch and call out concrete risks."
        parts = [
            instructions or "Perform a bounded technical review and prioritize concrete defects, regressions, and missing guards.",
            f"Goal: {goal}",
            f"Focus: {focus}" if focus else "",
            "Diff:\n" + diff_text if diff_text else "",
            "Additional material:\n" + source_text if source_text else "",
            "Return a concise review. Prefer findings first.",
        ]
        return "\n\n".join(part for part in parts if part).strip()

    def _call_text(
        self,
        *,
        prompt: str,
        model: str,
        lane: str,
        principal_id: str = "",
    ):
        from app.services import responses_upstream as upstream

        config = upstream._provider_configs().get("onemin")
        if config is None or not config.api_keys:
            raise ToolExecutionError("onemin_missing_api_key")
        try:
            return upstream._call_onemin(
                config,
                prompt=prompt,
                messages=None,
                model=model,
                max_output_tokens=None,
                lane=lane,
                principal_id=principal_id,
            )
        except upstream.ResponsesUpstreamError as exc:
            raise ToolExecutionError(f"onemin_failed:{str(exc)[:400]}") from exc

    def _call_feature(
        self,
        *,
        feature_payload: dict[str, object],
        lane: str,
        capability: str,
        principal_id: str = "",
        allow_reserve: bool = False,
    ) -> tuple[dict[str, Any], str, str, str, int, int]:
        from app.services import responses_upstream as upstream
        from app.services.onemin_manager import active_onemin_manager

        config = upstream._provider_configs().get("onemin")
        if config is None or not config.api_keys:
            raise ToolExecutionError("onemin_missing_api_key")

        key_names = tuple(config.api_keys)
        active_key_names = upstream._ordered_onemin_keys_allow_reserve(False)
        all_key_names = upstream._ordered_onemin_keys_allow_reserve(True)
        allow_reserve = bool(allow_reserve)
        tested: set[str] = set()
        errors: list[str] = []
        selection_request_id = f"onemin-tool-{uuid.uuid4().hex[:16]}"
        manager = active_onemin_manager()
        estimated_feature_credits = _estimate_onemin_feature_credits(
            feature_payload,
            capability=capability,
        )

        while len(tested) < len(all_key_names):
            candidate_key_names = active_key_names if not allow_reserve else all_key_names
            filtered_key_names = tuple(key for key in candidate_key_names if key not in tested)
            manager_lease_id = ""
            manager_selection: dict[str, object] | None = None
            if manager is not None and filtered_key_names:
                manager_selection = manager.reserve_for_candidates(
                    candidates=self._manager_candidates(
                        upstream=upstream,
                        key_names=key_names,
                        filtered_key_names=filtered_key_names,
                        active_key_names=active_key_names,
                    ),
                    lane=lane,
                    capability=capability,
                    principal_id=principal_id,
                    request_id=selection_request_id,
                    estimated_credits=estimated_feature_credits,
                    allow_reserve=allow_reserve,
                )
            if manager_selection is not None:
                api_key = str(manager_selection.get("api_key") or "")
                wait_until = 0.0
                manager_lease_id = str(manager_selection.get("lease_id") or "")
            else:
                key_pick = upstream._pick_onemin_key(allow_reserve=allow_reserve)
                if key_pick is None:
                    if not allow_reserve and len(all_key_names) > len(active_key_names):
                        allow_reserve = True
                        continue
                    break
                api_key, wait_until, _ = key_pick
            if api_key in tested:
                if (
                    not allow_reserve
                    and len(all_key_names) > len(active_key_names)
                    and all(key in tested for key in active_key_names)
                ):
                    allow_reserve = True
                upstream._rotate_onemin_cursor_after_key_usage(api_key)
                continue
            tested.add(api_key)
            if wait_until > 0:
                errors.append(f"cooldown_until_{int(wait_until)}")
                upstream._rotate_onemin_cursor_after_key_usage(api_key)
                continue

            def _release_failed(error_text: str) -> None:
                nonlocal manager_lease_id
                if manager is not None and manager_lease_id:
                    manager.release_lease(
                        lease_id=manager_lease_id,
                        status="failed",
                        error=str(error_text or "").strip() or "onemin_feature_failed",
                    )
                    manager_lease_id = ""

            upstream._mark_onemin_request_start(api_key)
            key_slot = upstream._onemin_key_slot(api_key, key_names=key_names)
            account_name = upstream._provider_account_name("onemin", key_names=key_names, key=api_key)
            started_at = upstream._now_ms()
            try:
                status, payload = upstream._post_json(
                    url=upstream._onemin_code_url(),
                    headers={"API-KEY": api_key},
                    payload=feature_payload,
                    timeout_seconds=config.timeout_seconds,
                )
            except upstream.ResponsesUpstreamError as exc:
                detail = str(exc or "").strip() or "request_failed"
                upstream._mark_onemin_failure(
                    api_key,
                    detail,
                    temporary_quarantine=False,
                )
                _release_failed(detail)
                errors.append(f"{key_slot}:{detail}")
                continue
            except Exception as exc:
                _release_failed(str(exc))
                raise
            latency_ms = upstream._now_ms() - started_at
            if status < 200 or status >= 300:
                detail = upstream._trim_error_payload(payload)
                self._mark_onemin_feature_failure(upstream, api_key=api_key, detail=detail)
                _release_failed(detail)
                errors.append(f"{key_slot}:http_{status}:{detail}")
                continue
            if not isinstance(payload, dict):
                upstream._mark_onemin_failure(api_key, "invalid_payload", temporary_quarantine=False)
                _release_failed("invalid_payload")
                errors.append(f"{key_slot}:invalid_payload")
                continue

            onemin_error = upstream._extract_onemin_error(payload)
            if onemin_error:
                self._mark_onemin_feature_failure(upstream, api_key=api_key, detail=onemin_error)
                _release_failed(onemin_error)
                errors.append(f"{key_slot}:{onemin_error}")
                continue

            resolved_model = upstream._extract_onemin_model(payload) or str(feature_payload.get("model") or "").strip()
            tokens_in = 0
            tokens_out = 0
            usage = payload.get("usage")
            if isinstance(usage, dict):
                tokens_in = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
                tokens_out = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
            measured_credits_delta, _usage_basis = upstream._record_onemin_usage_and_measure_delta(
                api_key=api_key,
                model=resolved_model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                lane=lane,
            )
            upstream._mark_onemin_success(api_key)
            if manager is not None and manager_lease_id:
                manager.record_usage(
                    lease_id=manager_lease_id,
                    actual_credits_delta=measured_credits_delta
                    if measured_credits_delta is not None
                    else estimated_feature_credits,
                    status="success",
                )
                manager.release_lease(lease_id=manager_lease_id, status="released")
            return payload, account_name, key_slot, resolved_model, tokens_in, tokens_out

        raise ToolExecutionError(f"onemin_feature_failed:{'; '.join(errors)[:400] or 'unavailable'}")

    def execute_code_generate(self, request: ToolInvocationRequest, definition: ToolDefinition) -> ToolInvocationResult:
        payload = dict(request.payload_json or {})
        principal_id = self._request_principal_id(request)
        prompt = self._build_code_prompt(payload)
        model = str(payload.get("model") or self._default_code_model()).strip() or self._default_code_model()
        result = self._call_text(
            prompt=prompt,
            model=model,
            lane="hard",
            principal_id=principal_id,
        )
        normalized_text, structured_output_json, mime_type = _parse_structured(result.text)
        action_kind = str(request.action_kind or "code.generate") or "code.generate"
        return ToolInvocationResult(
            tool_name=definition.tool_name,
            action_kind=action_kind,
            target_ref=f"onemin:{uuid.uuid4()}",
            output_json={
                "normalized_text": normalized_text,
                "structured_output_json": structured_output_json,
                "preview_text": _preview_text(normalized_text),
                "mime_type": mime_type,
                "model": result.model,
                "provider_backend": result.provider_backend or "1min",
                "provider_account_name": result.provider_account_name,
                "provider_key_slot": result.provider_key_slot,
                "tool_name": definition.tool_name,
                "action_kind": action_kind,
            },
            receipt_json={
                "handler_key": definition.tool_name,
                "invocation_contract": "tool.v1",
                "provider_key": "onemin",
                "provider_backend": result.provider_backend or "1min",
                "provider_account_name": result.provider_account_name,
                "provider_key_slot": result.provider_key_slot,
                "model": result.model,
                "principal_id": principal_id,
                "tool_version": definition.version,
            },
            model_name=result.model,
            tokens_in=int(result.tokens_in or 0),
            tokens_out=int(result.tokens_out or 0),
            cost_usd=0.0,
        )

    def execute_reasoned_patch_review(self, request: ToolInvocationRequest, definition: ToolDefinition) -> ToolInvocationResult:
        payload = dict(request.payload_json or {})
        principal_id = self._request_principal_id(request)
        prompt = self._build_review_prompt(payload)
        model = str(payload.get("model") or self._default_review_model()).strip() or self._default_review_model()
        result = self._call_text(
            prompt=prompt,
            model=model,
            lane="review",
            principal_id=principal_id,
        )
        normalized_text, structured_output_json, mime_type = _parse_structured(result.text)
        action_kind = str(request.action_kind or "code.review") or "code.review"
        return ToolInvocationResult(
            tool_name=definition.tool_name,
            action_kind=action_kind,
            target_ref=f"onemin:{uuid.uuid4()}",
            output_json={
                "normalized_text": normalized_text,
                "structured_output_json": structured_output_json,
                "preview_text": _preview_text(normalized_text),
                "mime_type": mime_type,
                "model": result.model,
                "provider_backend": result.provider_backend or "1min",
                "provider_account_name": result.provider_account_name,
                "provider_key_slot": result.provider_key_slot,
                "tool_name": definition.tool_name,
                "action_kind": action_kind,
            },
            receipt_json={
                "handler_key": definition.tool_name,
                "invocation_contract": "tool.v1",
                "provider_key": "onemin",
                "provider_backend": result.provider_backend or "1min",
                "provider_account_name": result.provider_account_name,
                "provider_key_slot": result.provider_key_slot,
                "model": result.model,
                "principal_id": principal_id,
                "tool_version": definition.version,
            },
            model_name=result.model,
            tokens_in=int(result.tokens_in or 0),
            tokens_out=int(result.tokens_out or 0),
            cost_usd=0.0,
        )

    def execute_image_generate(self, request: ToolInvocationRequest, definition: ToolDefinition) -> ToolInvocationResult:
        payload = dict(request.payload_json or {})
        principal_id = self._request_principal_id(request)
        prompt = _extract_text(payload.get("prompt") or payload.get("source_text") or payload.get("normalized_text"))
        if not prompt:
            raise ToolExecutionError("prompt_required:provider.onemin.image_generate")
        model = str(payload.get("model") or self._default_image_model()).strip() or self._default_image_model()
        prompt_object: dict[str, object] = {
            "prompt": prompt,
            "n": int(payload.get("n") or 1),
            "quality": str(payload.get("quality") or "low"),
            "output_format": str(payload.get("output_format") or "png"),
        }
        size = str(payload.get("size") or "").strip()
        aspect_ratio = str(payload.get("aspect_ratio") or "").strip()
        if size:
            prompt_object["size"] = size
        if aspect_ratio:
            prompt_object["aspect_ratio"] = aspect_ratio
        feature_payload = {
            "type": "IMAGE_GENERATOR",
            "model": model,
            "promptObject": prompt_object,
        }
        raw_response, account_name, key_slot, resolved_model, tokens_in, tokens_out = self._call_feature(
            feature_payload=feature_payload,
            lane="hard",
            capability="image_generate",
            principal_id=principal_id,
            allow_reserve=self._manager_allow_reserve(request),
        )
        asset_urls = _collect_asset_urls(raw_response)
        normalized_text = json.dumps(
            {
                "asset_urls": asset_urls,
                "model": resolved_model,
            },
            ensure_ascii=True,
        )
        action_kind = str(request.action_kind or "image.generate") or "image.generate"
        return ToolInvocationResult(
            tool_name=definition.tool_name,
            action_kind=action_kind,
            target_ref=f"onemin:{uuid.uuid4()}",
            output_json={
                "normalized_text": normalized_text,
                "structured_output_json": {
                    "asset_urls": asset_urls,
                    "raw_response": raw_response,
                },
                "preview_text": _preview_text(asset_urls[0] if asset_urls else normalized_text),
                "mime_type": "application/json",
                "model": resolved_model,
                "asset_urls": asset_urls,
                "provider_backend": "1min",
                "provider_account_name": account_name,
                "provider_key_slot": key_slot,
                "tool_name": definition.tool_name,
                "action_kind": action_kind,
            },
            receipt_json={
                "handler_key": definition.tool_name,
                "invocation_contract": "tool.v1",
                "provider_key": "onemin",
                "provider_backend": "1min",
                "provider_account_name": account_name,
                "provider_key_slot": key_slot,
                "model": resolved_model,
                "feature_type": "IMAGE_GENERATOR",
                "principal_id": principal_id,
                "tool_version": definition.version,
            },
            model_name=resolved_model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=0.0,
        )

    def execute_media_transform(self, request: ToolInvocationRequest, definition: ToolDefinition) -> ToolInvocationResult:
        payload = dict(request.payload_json or {})
        principal_id = self._request_principal_id(request)
        feature_type = _infer_media_feature_type(payload)
        prompt = _extract_text(payload.get("prompt") or payload.get("source_text"))
        prompt_object = _normalize_media_prompt_object(payload)
        if not feature_type:
            raise ToolExecutionError("feature_type_required:provider.onemin.media_transform")
        if "imageUrl" not in prompt_object:
            raise ToolExecutionError("image_url_required:provider.onemin.media_transform")
        if feature_type not in _PROMPT_OPTIONAL_MEDIA_FEATURES and not prompt:
            raise ToolExecutionError("prompt_required:provider.onemin.media_transform")
        model = str(payload.get("model") or self._default_media_model()).strip() or self._default_media_model()
        if prompt:
            prompt_object.setdefault("prompt", prompt)
        feature_payload = {
            "type": feature_type,
            "model": model,
            "promptObject": prompt_object,
        }
        raw_response, account_name, key_slot, resolved_model, tokens_in, tokens_out = self._call_feature(
            feature_payload=feature_payload,
            lane="hard",
            capability="media_transform",
            principal_id=principal_id,
            allow_reserve=self._manager_allow_reserve(request),
        )
        asset_urls = _collect_asset_urls(raw_response)
        response_text = _extract_text(raw_response)
        normalized_text = json.dumps(
            {
                "feature_type": feature_type,
                "asset_urls": asset_urls,
                "text": response_text,
                "model": resolved_model,
            },
            ensure_ascii=True,
        )
        action_kind = str(request.action_kind or "media.transform") or "media.transform"
        return ToolInvocationResult(
            tool_name=definition.tool_name,
            action_kind=action_kind,
            target_ref=f"onemin:{uuid.uuid4()}",
            output_json={
                "normalized_text": normalized_text,
                "structured_output_json": {
                    "feature_type": feature_type,
                    "asset_urls": asset_urls,
                    "text": response_text,
                    "raw_response": raw_response,
                },
                "preview_text": _preview_text(response_text or (asset_urls[0] if asset_urls else normalized_text)),
                "mime_type": "application/json",
                "model": resolved_model,
                "asset_urls": asset_urls,
                "provider_backend": "1min",
                "provider_account_name": account_name,
                "provider_key_slot": key_slot,
                "tool_name": definition.tool_name,
                "action_kind": action_kind,
            },
            receipt_json={
                "handler_key": definition.tool_name,
                "invocation_contract": "tool.v1",
                "provider_key": "onemin",
                "provider_backend": "1min",
                "provider_account_name": account_name,
                "provider_key_slot": key_slot,
                "model": resolved_model,
                "feature_type": feature_type,
                "principal_id": principal_id,
                "tool_version": definition.version,
            },
            model_name=resolved_model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=0.0,
        )

    def _property_walkthrough_prompt_object(self, *, image_url: str, prompt: str, model: str, duration: int) -> tuple[str, dict[str, object]]:
        normalized_model = str(model or "").strip().lower() or "pika"
        safe_duration = max(5, min(10, int(duration or 5)))
        if normalized_model == "skyreels":
            return "Qubico/skyreels", {
                "imageUrl": image_url,
                "prompt": prompt,
                "negative_prompt": "cuts, transitions, speed ramps, morphing, flicker, text, watermark, blur, low quality",
                "aspect_ratio": "16:9",
                "guidance_scale": float(_env_value("PROPERTYQUARRY_ONEMIN_SKYREELS_GUIDANCE_SCALE") or "3.5"),
            }
        if normalized_model == "hailuo":
            return "hailuo", {
                "imageUrl": image_url,
                "taskType": _env_value("PROPERTYQUARRY_ONEMIN_HAILUO_TASK_TYPE") or "i2v-02",
                "prompt": prompt,
                "duration": 6 if safe_duration <= 6 else 10,
                "resolution": int(_env_value("PROPERTYQUARRY_ONEMIN_HAILUO_RESOLUTION") or "768"),
                "expand_prompt": False,
            }
        if normalized_model == "kling":
            return "kling", {
                "imageUrl": image_url,
                "prompt": prompt,
                "duration": 5 if safe_duration <= 5 else 10,
                "aspect_ratio": "16:9",
                "mode": _env_value("PROPERTYQUARRY_ONEMIN_KLING_MODE") or "std",
                "version": _env_value("PROPERTYQUARRY_ONEMIN_KLING_VERSION") or "1.6",
                "cfg_scale": 0.5,
                "negative_prompt": "cuts, transitions, speed ramps, morphing, flicker, text, watermark",
                "camera_control_type": "default",
            }
        if normalized_model == "veo3":
            return "veo3", {
                "imageUrl": image_url,
                "prompt": prompt,
                "task_type": _env_value("PROPERTYQUARRY_ONEMIN_VEO3_TASK_TYPE") or "veo3.1-video-fast",
                "generate_audio": False,
                "aspect_ratio": "16:9",
                "veo3_duration": "8s",
                "resolution": _env_value("PROPERTYQUARRY_ONEMIN_VEO3_RESOLUTION") or "720p",
            }
        return "pika", {
            "imageUrl": image_url,
            "task_type": _env_value("PROPERTYQUARRY_ONEMIN_PIKA_TASK_TYPE") or "pika-v2.2",
            "prompt": prompt,
            "duration": 5 if safe_duration <= 5 else 10,
            "resolution": _env_value("PROPERTYQUARRY_ONEMIN_PIKA_RESOLUTION") or "720p",
            "negative_prompt": "cuts, transitions, speed ramps, morphing, flicker, text, watermark, blur, low quality",
        }

    def _call_property_walkthrough_feature(
        self,
        *,
        first_frame_path: str,
        image_url: str,
        feature_model: str,
        prompt_object: dict[str, object],
        principal_id: str,
        allow_reserve: bool,
        timeout_seconds: int,
    ) -> tuple[dict[str, Any], str, str, str, int, int]:
        from app.services import responses_upstream as upstream
        from app.services.onemin_manager import active_onemin_manager

        config = upstream._provider_configs().get("onemin")
        if config is None or not config.api_keys:
            raise ToolExecutionError("onemin_missing_api_key")
        manager = active_onemin_manager()
        if manager is None and image_url:
            return self._call_feature(
                feature_payload={
                    "type": "IMAGE_TO_VIDEO",
                    "model": feature_model,
                    "conversationId": "IMAGE_TO_VIDEO",
                    "promptObject": {**prompt_object, "imageUrl": image_url},
                },
                lane="media",
                capability="property_walkthrough_video",
                principal_id=principal_id,
                allow_reserve=allow_reserve,
            )
        if manager is None:
            raise ToolExecutionError("onemin_manager_unavailable")

        key_names = tuple(config.api_keys)
        active_key_names = upstream._ordered_onemin_keys_allow_reserve(False)
        all_key_names = upstream._ordered_onemin_keys_allow_reserve(True)
        tested: set[str] = set()
        errors: list[str] = []
        estimated_credits = _estimate_onemin_feature_credits(
            {
                "type": "IMAGE_TO_VIDEO",
                "model": feature_model,
                "promptObject": prompt_object,
            },
            capability="property_walkthrough_video",
        )
        request_id = f"onemin-property-video-{uuid.uuid4().hex[:16]}"
        while len(tested) < len(all_key_names):
            candidate_key_names = active_key_names if not allow_reserve else all_key_names
            filtered_key_names = tuple(key for key in candidate_key_names if key not in tested)
            if not filtered_key_names:
                if not allow_reserve and len(all_key_names) > len(active_key_names):
                    break
                break
            selection = manager.reserve_for_candidates(
                candidates=self._manager_candidates(
                    upstream=upstream,
                    key_names=key_names,
                    filtered_key_names=filtered_key_names,
                    active_key_names=active_key_names,
                ),
                lane="media",
                capability="property_walkthrough_video",
                principal_id=principal_id,
                request_id=request_id,
                estimated_credits=estimated_credits,
                allow_reserve=allow_reserve,
            )
            if selection is None:
                if not allow_reserve and len(all_key_names) > len(active_key_names):
                    break
                break
            api_key = str(selection.get("api_key") or "").strip()
            lease_id = str(selection.get("lease_id") or "").strip()
            key_slot = str(selection.get("slot_name") or selection.get("credential_id") or "").strip()
            account_name = str(selection.get("account_name") or "").strip()
            print(
                json.dumps(
                    {
                        "event": "ea_one_manager_property_video_selected",
                        "model": feature_model,
                        "key_slot": key_slot,
                        "account_name": account_name,
                        "timeout_seconds": int(timeout_seconds or config.timeout_seconds),
                    }
                ),
                flush=True,
            )
            if not api_key or api_key in tested:
                if lease_id:
                    manager.release_lease(lease_id=lease_id, status="failed", error="empty_or_duplicate_api_key")
                continue
            tested.add(api_key)
            try:
                effective_timeout = max(15, int(timeout_seconds or config.timeout_seconds))
                with _OneminDeadline(effective_timeout + 30, f"onemin_property_video_timeout:{feature_model}:{effective_timeout}s"):
                    if image_url:
                        resolved_image_url = image_url
                    else:
                        print(
                            json.dumps({"event": "ea_one_manager_property_video_upload_start", "model": feature_model, "key_slot": key_slot}),
                            flush=True,
                        )
                        resolved_image_url = _onemin_upload_asset(
                            api_key=api_key,
                            image_path=Path(first_frame_path).expanduser().resolve(),
                            timeout_seconds=min(45, effective_timeout),
                        )
                        print(
                            json.dumps({"event": "ea_one_manager_property_video_upload_done", "model": feature_model, "key_slot": key_slot}),
                            flush=True,
                        )
                    feature_payload = {
                        "type": "IMAGE_TO_VIDEO",
                        "model": feature_model,
                        "conversationId": "IMAGE_TO_VIDEO",
                        "promptObject": {**prompt_object, "imageUrl": resolved_image_url},
                    }
                    response = requests.post(
                        upstream._onemin_code_url(),
                        headers={"API-KEY": api_key, "Content-Type": "application/json", "Accept": "application/json"},
                        json=feature_payload,
                        timeout=(10, effective_timeout),
                    )
                    print(
                        json.dumps(
                            {
                                "event": "ea_one_manager_property_video_feature_done",
                                "model": feature_model,
                                "key_slot": key_slot,
                                "status_code": int(response.status_code),
                            }
                        ),
                        flush=True,
                    )
                    status = int(response.status_code)
                    try:
                        raw_response = response.json()
                    except Exception:
                        raw_response = response.text
                if status < 200 or status >= 300:
                    detail = upstream._trim_error_payload(raw_response)
                    self._mark_onemin_feature_failure(upstream, api_key=api_key, detail=detail)
                    manager.release_lease(lease_id=lease_id, status="failed", error=detail)
                    errors.append(f"{key_slot}:http_{status}:{detail}")
                    if _onemin_video_provider_timeout_status(status) and not _onemin_video_gateway_timeout_retry_allowed():
                        break
                    continue
                if not isinstance(raw_response, dict):
                    upstream._mark_onemin_failure(api_key, "invalid_payload", temporary_quarantine=False)
                    manager.release_lease(lease_id=lease_id, status="failed", error="invalid_payload")
                    errors.append(f"{key_slot}:invalid_payload")
                    continue
                onemin_error = upstream._extract_onemin_error(raw_response)
                if onemin_error:
                    self._mark_onemin_feature_failure(upstream, api_key=api_key, detail=onemin_error)
                    manager.release_lease(lease_id=lease_id, status="failed", error=onemin_error)
                    errors.append(f"{key_slot}:{onemin_error}")
                    continue
                resolved_model = upstream._extract_onemin_model(raw_response) or feature_model
                manager.record_usage(lease_id=lease_id, actual_credits_delta=estimated_credits, status="success")
                manager.release_lease(lease_id=lease_id, status="released")
                return raw_response, account_name, key_slot, resolved_model, 0, 0
            except Exception as exc:  # noqa: BLE001
                detail = str(exc or "").strip() or "property_walkthrough_video_failed"
                upstream._mark_onemin_failure(api_key, detail, temporary_quarantine=False)
                if lease_id:
                    manager.release_lease(lease_id=lease_id, status="failed", error=detail)
                errors.append(f"{key_slot}:{detail}")
                continue
        raise ToolExecutionError(f"onemin_property_walkthrough_video_failed:{'; '.join(errors)[:400] or 'unavailable'}")

    def execute_property_walkthrough_video(self, request: ToolInvocationRequest, definition: ToolDefinition) -> ToolInvocationResult:
        payload = dict(request.payload_json or {})
        prompt = _extract_text(payload.get("prompt") or payload.get("source_text"))
        if not prompt:
            raise ToolExecutionError("prompt_required:provider.onemin.property_walkthrough_video")
        image_url = _first_nonempty(payload.get("image_url"), payload.get("imageUrl"), payload.get("asset_url"), payload.get("assetUrl"))
        first_frame_path = _first_nonempty(payload.get("first_frame_path"), payload.get("firstFramePath"))
        model_url = _first_nonempty(payload.get("model_url"), payload.get("modelUrl"), payload.get("model_asset_url"), payload.get("modelAssetUrl"))
        model_path = _first_nonempty(payload.get("model_path"), payload.get("modelPath"))
        model_asset_kind = _first_nonempty(payload.get("model_asset_kind"), payload.get("modelAssetKind"))
        model_input_required = bool(payload.get("model_input_required") or payload.get("modelInputRequired"))
        if not image_url and not first_frame_path:
            raise ToolExecutionError("image_url_required:provider.onemin.property_walkthrough_video")
        model_order = [
            str(item or "").strip()
            for item in (
                payload.get("model_order")
                if isinstance(payload.get("model_order"), list)
                else str(payload.get("model_order") or payload.get("model") or "pika,skyreels,kling,hailuo").split(",")
            )
            if str(item or "").strip()
        ] or ["pika", "skyreels", "kling", "hailuo"]
        duration = int(payload.get("duration") or 5)
        attempts: list[dict[str, object]] = []
        last_error = ""

        for model_name in model_order:
            feature_model, prompt_object = self._property_walkthrough_prompt_object(
                image_url=image_url,
                prompt=prompt,
                model=model_name,
                duration=duration,
            )
            feature_payload = {
                "type": "IMAGE_TO_VIDEO",
                "model": feature_model,
                "conversationId": "IMAGE_TO_VIDEO",
                "promptObject": prompt_object,
            }
            try:
                raw_response, account_name, key_slot, resolved_model, tokens_in, tokens_out = self._call_property_walkthrough_feature(
                    first_frame_path=first_frame_path,
                    image_url=image_url,
                    feature_model=feature_model,
                    prompt_object=prompt_object,
                    principal_id=self._request_principal_id(request),
                    allow_reserve=True if payload.get("allow_reserve") is None else self._manager_allow_reserve(request),
                    timeout_seconds=int(payload.get("timeout_seconds") or _env_value("PROPERTYQUARRY_ONEMIN_FEATURE_TIMEOUT_SECONDS") or 180),
                )
                video_urls = _collect_video_urls(raw_response)
                if not video_urls:
                    raise ToolExecutionError("onemin_property_walkthrough_video_url_missing")
                video_url = video_urls[0]
                normalized_text = json.dumps(
                    {
                        "video_url": video_url,
                        "model": resolved_model,
                        "provider_account_name": account_name,
                    },
                    ensure_ascii=True,
                )
                action_kind = str(request.action_kind or "video.generate") or "video.generate"
                return ToolInvocationResult(
                    tool_name=definition.tool_name,
                    action_kind=action_kind,
                    target_ref=f"onemin-property-video:{uuid.uuid4()}",
                    output_json={
                        "normalized_text": normalized_text,
                        "structured_output_json": {
                            "video_url": video_url,
                            "asset_urls": video_urls,
                            "raw_response": raw_response,
                            "attempts": attempts,
                            "model_url": model_url,
                            "model_path": model_path,
                            "model_asset_kind": model_asset_kind,
                            "model_input_required": model_input_required,
                            "model_input_consumed": False,
                            "model_input_reason": "onemin_i2v_adapter_accepts_model_metadata_but_current_feature_payload_is_image_to_video",
                        },
                        "preview_text": _preview_text(video_url),
                        "mime_type": "application/json",
                        "model": resolved_model,
                        "video_url": video_url,
                        "asset_url": video_url,
                        "asset_urls": video_urls,
                        "provider_backend": "1min",
                        "provider_account_name": account_name,
                        "provider_key_slot": key_slot,
                        "tool_name": definition.tool_name,
                        "action_kind": action_kind,
                    },
                    receipt_json={
                        "handler_key": definition.tool_name,
                        "invocation_contract": "tool.v1",
                        "provider_key": "onemin",
                        "provider_backend": "1min",
                        "provider_account_name": account_name,
                        "provider_key_slot": key_slot,
                        "model": resolved_model,
                        "feature_type": "IMAGE_TO_VIDEO",
                        "attempts": attempts,
                        "model_url": model_url,
                        "model_path": model_path,
                        "model_asset_kind": model_asset_kind,
                        "model_input_required": model_input_required,
                        "model_input_consumed": False,
                        "model_input_reason": "onemin_i2v_adapter_accepts_model_metadata_but_current_feature_payload_is_image_to_video",
                        "tool_version": definition.version,
                    },
                    model_name=resolved_model,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_usd=0.0,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                attempts.append({"model": model_name, "status": "failed", "error": last_error[:500]})
                if _onemin_video_provider_timeout_error(last_error) and not _onemin_video_gateway_timeout_retry_allowed():
                    break
                continue
        raise ToolExecutionError(f"onemin_property_walkthrough_video_failed:{last_error[:400] or 'unavailable'}")
