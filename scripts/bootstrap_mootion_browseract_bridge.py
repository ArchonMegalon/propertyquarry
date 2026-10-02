#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


EA_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = EA_ROOT / ".env"
STATE_DIR = EA_ROOT / "state" / "browseract" / "mootion_movie_bridge"
API_BASE = "https://api.browseract.com/v2/workflow"


def load_local_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_FILE.exists():
        return values
    for raw in ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


LOCAL_ENV = load_local_env()


def env_value(name: str) -> str:
    return str(os.environ.get(name) or LOCAL_ENV.get(name) or "").strip()


def load_env_into_process() -> None:
    for key, value in LOCAL_ENV.items():
        os.environ.setdefault(key, value)


def browseract_key() -> str:
    for key_name in (
        "BROWSERACT_API_KEY",
        "BROWSERACT_API_KEY_FALLBACK_1",
        "BROWSERACT_API_KEY_FALLBACK_2",
        "BROWSERACT_API_KEY_FALLBACK_3",
    ):
        value = env_value(key_name)
        if value:
            return value
    return ""


def api_request(
    method: str,
    path: str,
    *,
    payload: dict[str, object] | None = None,
    query: dict[str, str] | None = None,
    timeout_seconds: int = 120,
) -> dict[str, object]:
    key = browseract_key()
    if not key:
        raise RuntimeError("browseract_api_key_missing")
    url = API_BASE.rstrip("/") + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    headers = {
        "Authorization": f"Bearer {key}",
        "User-Agent": "EA-Mootion-Bridge-Bootstrap/1.0",
    }
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, method=method.upper(), headers=headers, data=data)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"browseract_http_{exc.code}:{detail[:240]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"browseract_transport:{exc.reason}") from exc
    try:
        loaded = json.loads(body)
    except Exception as exc:
        raise RuntimeError(f"browseract_non_json:{body[:240]}") from exc
    return loaded if isinstance(loaded, dict) else {"data": loaded}


def build_workflow_spec() -> dict[str, object]:
    return {
        "workflow_name": "ea_mootion_scene_video_bridge",
        "description": (
            "EA bridge workflow for Mootion scene-video generation. Accepts a script prompt plus style/runtime hints, "
            "logs into Mootion when needed, starts a short-video project, waits for a result, and returns JSON with "
            "video/download/editor/public URLs."
        ),
        "publish": True,
        "mcp_ready": False,
        "meta": {
            "service_key": "mootion_movie",
            "tool_name": "browseract.mootion_movie",
            "provider_key": "mootion",
            "surfaces": ["runsite", "propertyquarry", "scene_video"],
            "credential_boundary": "does_not_modify_onemin_credentials",
        },
        "inputs": [
            {"name": "prompt", "description": "Scene script or storyboard prompt."},
            {"name": "title", "description": "Human title for the generated Mootion project.", "default_value": "EA scene video"},
            {"name": "visual_style", "description": "Visual style hint.", "default_value": "grounded cinematic realism"},
            {"name": "camera_style", "description": "Camera motion hint.", "default_value": "steady cinematic motion"},
            {"name": "aspect_ratio", "description": "Target aspect ratio.", "default_value": "16:9"},
            {"name": "duration_seconds", "description": "Approximate target duration.", "default_value": "18"},
            {"name": "scene_count", "description": "Approximate scene count.", "default_value": "4"},
            {"name": "shot_pacing", "description": "Shot pacing hint.", "default_value": "steady"},
            {"name": "audience", "description": "Audience or product surface."},
            {"name": "hook_line", "description": "Opening hook line."},
            {"name": "closing_line", "description": "Closing line or CTA."},
            {"name": "platform_target", "description": "Target surface or platform."},
            {"name": "cta", "description": "Optional call to action."},
            {"name": "browseract_username", "description": "Mootion login email supplied by EA binding/env."},
            {"name": "browseract_password", "description": "Mootion login password supplied by EA binding/env."},
        ],
        "nodes": [
            {
                "id": "open_mootion",
                "label": "Open Mootion short-video creator",
                "type": "visit_page",
                "config": {
                    "url": "https://storyteller.mootion.com/project/new?type=short_video&prompt_type=idea&input=",
                    "goal": "Open the Mootion short-video prompt creator. If redirected to login, continue through login first.",
                },
            },
            {
                "id": "login_if_needed",
                "label": "Log into Mootion if required",
                "type": "auth",
                "config": {
                    "when": "Only when login or sign-in UI is visible.",
                    "username_from_input": "browseract_username",
                    "password_from_input": "browseract_password",
                    "submit_goal": "Complete login and return to the short-video creator.",
                },
            },
            {
                "id": "compose_prompt",
                "label": "Compose the Mootion prompt",
                "type": "fill_prompt",
                "config": {
                    "value_from_input": "prompt",
                    "append_inputs": [
                        "title",
                        "visual_style",
                        "camera_style",
                        "aspect_ratio",
                        "duration_seconds",
                        "scene_count",
                        "shot_pacing",
                        "audience",
                        "hook_line",
                        "closing_line",
                        "platform_target",
                        "cta",
                    ],
                    "goal": "Fill the main idea/script field with a compact video brief preserving all scene requirements.",
                },
            },
            {
                "id": "set_options",
                "label": "Set style, ratio, and pacing options",
                "type": "configure_options",
                "config": {
                    "aspect_ratio_from_input": "aspect_ratio",
                    "visual_style_from_input": "visual_style",
                    "camera_style_from_input": "camera_style",
                    "shot_pacing_from_input": "shot_pacing",
                    "goal": "Use available Mootion controls only; skip absent controls without failing.",
                },
            },
            {
                "id": "submit_generation",
                "label": "Start video generation",
                "type": "click",
                "config": {
                    "target": "Generate/Create/Start button",
                    "goal": "Start the Mootion generation once the prompt and options are set.",
                },
            },
            {
                "id": "wait_for_result",
                "label": "Wait for Mootion render result",
                "type": "wait_extract",
                "config": {
                    "timeout_seconds": 900,
                    "success_markers": ["video element", "download button", "export button", "project URL"],
                    "goal": "Wait until a result, project editor, or downloadable asset is available.",
                },
            },
            {
                "id": "output_result",
                "label": "Return normalized video packet",
                "type": "output",
                "config": {
                    "field_name": "mootion_movie_packet",
                    "json_schema": {
                        "render_status": "completed|rendered|pending|blocked|failed",
                        "video_url": "direct video URL when available",
                        "download_url": "download/export URL when available",
                        "asset_url": "best playable asset URL",
                        "editor_url": "Mootion project/editor URL",
                        "public_url": "public/share URL when available",
                        "reason": "short failure or pending reason when not completed",
                    },
                },
            },
        ],
        "edges": [
            {"source": "open_mootion", "target": "login_if_needed"},
            {"source": "login_if_needed", "target": "compose_prompt"},
            {"source": "compose_prompt", "target": "set_options"},
            {"source": "set_options", "target": "submit_generation"},
            {"source": "submit_generation", "target": "wait_for_result"},
            {"source": "wait_for_result", "target": "output_result"},
        ],
    }


def builder_packet(spec: dict[str, object]) -> dict[str, object]:
    sys.path.insert(0, str(EA_ROOT / "scripts"))
    import browseract_architect as architect  # type: ignore[import-not-found]

    normalized = architect.normalize_spec(spec)
    return architect.builder_packet(normalized)


def write_bridge_files(spec: dict[str, object], packet: dict[str, object]) -> tuple[Path, Path]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    spec_path = STATE_DIR / "ea_mootion_scene_video_bridge.workflow.json"
    packet_path = STATE_DIR / "ea_mootion_scene_video_bridge.builder.packet.json"
    spec_path.write_text(json.dumps(spec, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    packet_path.write_text(json.dumps(packet, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return spec_path, packet_path


def input_variants(input_values: dict[str, object]) -> list[object]:
    ordered = [(str(key), value) for key, value in input_values.items() if str(key).strip()]
    return [
        [{"name": key, "value": value} for key, value in ordered],
        [{"key": key, "value": value} for key, value in ordered],
        [{key: value for key, value in ordered}],
        {key: value for key, value in ordered},
    ]


def task_id(body: dict[str, object]) -> str:
    for key in ("task_id", "id", "_id", "taskId"):
        value = str(body.get(key) or "").strip()
        if value:
            return value
    nested = body.get("data")
    if isinstance(nested, dict):
        return task_id(nested)
    return ""


def task_status(body: dict[str, object]) -> str:
    for key in ("status", "state", "task_status", "taskStatus"):
        value = str(body.get(key) or "").strip().lower()
        if value:
            return value
    nested = body.get("data")
    if isinstance(nested, dict):
        return task_status(nested)
    return ""


def wait_for_task(tid: str, *, timeout_seconds: int) -> dict[str, object]:
    deadline = time.time() + max(30, int(timeout_seconds))
    last_status = ""
    while time.time() < deadline:
        status_body = api_request("GET", "/get-task-status", query={"task_id": tid}, timeout_seconds=60)
        status = task_status(status_body)
        if status:
            last_status = status
        if status in {"done", "completed", "success", "succeeded", "finished"}:
            return api_request("GET", "/get-task", query={"task_id": tid}, timeout_seconds=120)
        if status in {"failed", "error", "cancelled", "canceled"}:
            try:
                detail = api_request("GET", "/get-task", query={"task_id": tid}, timeout_seconds=120)
            except Exception:
                detail = status_body
            raise RuntimeError(f"browseract_architect_task_failed:{json.dumps(redacted_task_summary(detail), ensure_ascii=True)[:500]}")
        time.sleep(5)
    raise RuntimeError(f"browseract_architect_task_timeout:{last_status or 'unknown'}")


def collect_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(collect_text(item) for item in value.values())
    if isinstance(value, list):
        return "\n".join(collect_text(item) for item in value)
    return str(value or "")


def extract_workflow_id(value: object) -> str:
    if isinstance(value, dict):
        for key in ("workflow_id", "workflowId", "browseract_workflow_id", "mootion_workflow_id"):
            candidate = str(value.get(key) or "").strip()
            if candidate and re.fullmatch(r"[A-Za-z0-9_-]{6,}", candidate):
                return candidate
        for item in value.values():
            candidate = extract_workflow_id(item)
            if candidate:
                return candidate
    if isinstance(value, list):
        for item in value:
            candidate = extract_workflow_id(item)
            if candidate:
                return candidate
    text = collect_text(value)
    for pattern in (
        r"browseract://workflow/([A-Za-z0-9_-]{6,})",
        r"/workflow/([A-Za-z0-9_-]{6,})",
        r"workflow[_ -]?id['\"`:= ]+([A-Za-z0-9_-]{6,})",
    ):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def redacted_task_summary(body: dict[str, object]) -> dict[str, object]:
    return {
        "task_id": task_id(body),
        "status": task_status(body),
        "has_output": bool(body.get("output") or body.get("output_json") or body.get("result")),
        "workflow_id": extract_workflow_id(body) or "",
    }


def submit_architect(packet: dict[str, object], spec: dict[str, object], *, timeout_seconds: int) -> dict[str, object]:
    architect_workflow_id = env_value("BROWSERACT_ARCHITECT_WORKFLOW_ID")
    if not architect_workflow_id:
        return {"status": "skipped", "reason": "architect_workflow_id_missing"}
    packet_text = json.dumps(packet, ensure_ascii=True)
    spec_text = json.dumps(spec, ensure_ascii=True)
    prompt = (
        "Create and publish the BrowserAct workflow described by this builder packet. "
        "Return strict JSON with workflow_id, workflow_name, run_url if available, and status. "
        "Do not run Mootion video generation; only build/publish the workflow."
    )
    input_values = {
        "prompt": prompt,
        "workflow_name": str(packet.get("workflow_name") or spec.get("workflow_name") or ""),
        "target_domain": "mootion.com",
        "builder_packet": packet_text,
        "workflow_packet": packet_text,
        "workflow_spec": spec_text,
    }
    last_error = ""
    started: dict[str, object] | None = None
    for candidate in input_variants(input_values):
        try:
            started = api_request(
                "POST",
                "/run-task",
                payload={"workflow_id": architect_workflow_id, "input_parameters": candidate},
                timeout_seconds=120,
            )
            break
        except Exception as exc:
            last_error = str(exc)
    if started is None:
        return {"status": "failed", "reason": last_error or "architect_run_task_failed"}
    tid = task_id(started)
    if not tid:
        return {"status": "started_without_task_id", "start_response": redacted_task_summary(started)}
    body = wait_for_task(tid, timeout_seconds=timeout_seconds)
    workflow_id = extract_workflow_id(body)
    return {
        "status": "submitted",
        "architect_workflow_id": architect_workflow_id,
        "architect_task_id": tid,
        "mootion_workflow_id": workflow_id,
        "task_summary": redacted_task_summary(body),
    }


def upsert_bridge_binding(
    *,
    principal_id: str,
    workflow_id: str,
    run_url: str,
    spec_path: Path,
    packet_path: Path,
    architect_result: dict[str, object],
) -> dict[str, object]:
    load_env_into_process()
    for candidate in (EA_ROOT / "ea", EA_ROOT):
        if (candidate / "app").exists():
            sys.path.insert(0, str(candidate))
    from app.services.tool_runtime import build_tool_runtime

    login_email = (
        env_value("MOOTION_EMAIL")
        or env_value("PROPERTYQUARRY_MOOTION_EMAIL")
        or env_value("EA_UI_SERVICE_LOGIN_EMAIL")
        or env_value("BROWSERACT_USERNAME")
    )
    auth_metadata: dict[str, object] = {
        "mootion_browseract_bridge": True,
        "service_key": "mootion_movie",
        "browseract_service_key": "mootion_movie",
        "capability_key": "mootion_movie",
        "tool_name": "browseract.mootion_movie",
        "bridge_status": "ready" if (workflow_id or run_url) else "pending_workflow",
        "workflow_spec_path": str(spec_path),
        "bridge_packet_path": str(packet_path),
        "architect_status": str(architect_result.get("status") or ""),
        "architect_task_id": str(architect_result.get("architect_task_id") or ""),
        "credential_boundary": "does_not_modify_onemin_credentials",
        "service_accounts_json": {
            "Mootion": {
                "account_email": login_email,
                "credential_source": "binding_or_env",
            }
        },
    }
    if workflow_id:
        auth_metadata["mootion_movie_workflow_id"] = workflow_id
        auth_metadata["browseract_mootion_movie_workflow_id"] = workflow_id
    if run_url:
        auth_metadata["mootion_movie_run_url"] = run_url
        auth_metadata["browseract_mootion_movie_run_url"] = run_url
    scope_json = {
        "services": ["mootion", "mootion_movie", "browseract.mootion_movie"],
        "assistant_surfaces": ["scene_video", "runsite", "propertyquarry"],
        "credential_boundary": "does_not_modify_onemin_credentials",
    }
    status = "enabled" if (workflow_id or run_url) else "pending_workflow"
    body = {
        "principal_id": principal_id,
        "connector_name": "browseract",
        "external_account_ref": "mootion-scene-video-bridge",
        "scope_json": scope_json,
        "auth_metadata_json": auth_metadata,
        "status": status,
    }
    try:
        runtime = build_tool_runtime()
        row = runtime.upsert_connector_binding(
            principal_id=principal_id,
            connector_name="browseract",
            external_account_ref="mootion-scene-video-bridge",
            scope_json=scope_json,
            auth_metadata_json=auth_metadata,
            status=status,
        )
        return {
            "binding_id": row.binding_id,
            "principal_id": row.principal_id,
            "connector_name": row.connector_name,
            "external_account_ref": row.external_account_ref,
            "status": row.status,
            "bridge_status": auth_metadata["bridge_status"],
            "workflow_id_configured": bool(workflow_id),
            "run_url_configured": bool(run_url),
            "upsert_path": "direct_runtime",
        }
    except Exception as direct_exc:
        token = env_value("EA_API_TOKEN")
        if not token:
            raise RuntimeError(f"bridge_binding_upsert_failed:{direct_exc}") from direct_exc
        host = str(os.environ.get("EA_SKILL_HOST") or env_value("EA_SKILL_HOST") or "http://127.0.0.1:8090").rstrip("/")
        request = urllib.request.Request(
            f"{host}/v1/connectors/bindings",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            data=json.dumps(body).encode("utf-8"),
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                loaded = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"bridge_binding_api_http_{exc.code}:{detail[:240]}") from direct_exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"bridge_binding_api_transport:{exc.reason}") from direct_exc
    return {
        "binding_id": str(loaded.get("binding_id") or ""),
        "principal_id": str(loaded.get("principal_id") or principal_id),
        "connector_name": str(loaded.get("connector_name") or "browseract"),
        "external_account_ref": str(loaded.get("external_account_ref") or "mootion-scene-video-bridge"),
        "status": str(loaded.get("status") or status),
        "bridge_status": auth_metadata["bridge_status"],
        "workflow_id_configured": bool(workflow_id),
        "run_url_configured": bool(run_url),
        "upsert_path": "api",
    }


def write_env_value(key: str, value: str) -> bool:
    if not key or not value:
        return False
    if key.startswith("ONEMIN_"):
        raise RuntimeError("refusing_to_write_onemin_key")
    lines = ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines() if ENV_FILE.exists() else []
    out: list[str] = []
    replaced = False
    for raw in lines:
        if raw.strip().startswith("#") or "=" not in raw:
            out.append(raw)
            continue
        existing_key = raw.split("=", 1)[0].strip()
        if existing_key == key:
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(raw)
    if not replaced:
        out.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")
    return True


def default_principal() -> str:
    return (
        env_value("CHUMMER6_RUNSITE_VIDEO_PRINCIPAL_ID")
        or env_value("EA_RUNSITE_VIDEO_PRINCIPAL_ID")
        or "propertyquarry-operator"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap the EA Mootion BrowserAct scene-video bridge.")
    parser.add_argument("--principal-id", default=default_principal())
    parser.add_argument("--workflow-id", default="")
    parser.add_argument("--run-url", default="")
    parser.add_argument("--submit-architect", action="store_true")
    parser.add_argument("--architect-timeout-seconds", type=int, default=900)
    parser.add_argument("--write-env", action="store_true")
    args = parser.parse_args()

    spec = build_workflow_spec()
    packet = builder_packet(spec)
    spec_path, packet_path = write_bridge_files(spec, packet)
    workflow_id = str(args.workflow_id or "").strip()
    run_url = str(args.run_url or "").strip()
    architect_result: dict[str, object] = {"status": "not_submitted"}
    if args.submit_architect and not workflow_id and not run_url:
        architect_result = submit_architect(packet, spec, timeout_seconds=args.architect_timeout_seconds)
        workflow_id = str(architect_result.get("mootion_workflow_id") or "").strip()
    binding = upsert_bridge_binding(
        principal_id=str(args.principal_id or "").strip(),
        workflow_id=workflow_id,
        run_url=run_url,
        spec_path=spec_path,
        packet_path=packet_path,
        architect_result=architect_result,
    )
    env_written = False
    if args.write_env and str(binding.get("status") or "").strip() == "enabled" and str(binding.get("binding_id") or "").strip():
        env_written = write_env_value("CHUMMER6_RUNSITE_VIDEO_BINDING_ID", str(binding["binding_id"]))
    print(
        json.dumps(
            {
                "status": "ok",
                "spec_path": str(spec_path),
                "packet_path": str(packet_path),
                "architect": architect_result,
                "binding": binding,
                "env_written": env_written,
                "onemin_credentials_touched": False,
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
