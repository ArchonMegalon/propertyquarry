from __future__ import annotations

from app.services.phygital.spend import SpendLedger

import base64
import hashlib
import http.cookiejar
import json
import mimetypes
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any


_PHYGITAL_APP_ORIGIN = "https://app.phygital.plus"
_PHYGITAL_API_ORIGIN = "https://app-server.phygital.plus"
_FLOORPLAN_KEYS = (
    "floorplan_url",
    "floorplan_preview_url",
    "floorplan_image_url",
    "floorplan_pdf_url",
)
_DEFAULT_PROJECT_NAME = "Property"
_KLING_SCHEMA_ID = 74
_DONE_STATUSES = {"done", "success", "ready", "finished", "complete", "completed"}
_ERROR_STATUSES = {"error", "failed", "cancelled", "canceled", "access_denied"}


_SPEND_LEDGER: SpendLedger | None = None
_SPEND_LEDGER_ENV: tuple[str, ...] = ()


def _module_ledger() -> SpendLedger:
    """Return the module-level spend ledger, rebuilding when its env changes.

    The cache is keyed on the three env vars that parameterize
    SpendLedger.from_env(), so a changed environment (between tests, or an
    operator rotating the cap) always yields a fresh ledger and a stale
    singleton can never outlive the env that created it.
    """
    global _SPEND_LEDGER, _SPEND_LEDGER_ENV
    signature = tuple(
        str(os.environ.get(name) or "")
        for name in (
            "PROPERTYQUARRY_PHYGITAL_STATE_DIR",
            "PROPERTYQUARRY_PHYGITAL_SPEND_LIMIT",
            "PROPERTYQUARRY_PHYGITAL_SPEND_WINDOW_SECONDS",
        )
    )
    if _SPEND_LEDGER is None or _SPEND_LEDGER_ENV != signature:
        _SPEND_LEDGER = SpendLedger.from_env()
        _SPEND_LEDGER_ENV = signature
    return _SPEND_LEDGER


def _env_flag(name: str, default: str = "0") -> bool:
    return str(os.getenv(name) or default).strip().lower() in {"1", "true", "yes", "on"}


def _env(name: str, default: str = "") -> str:
    return str(os.getenv(name) or default).strip()


def phygital_mode() -> str:
    if not _env_flag("PROPERTYQUARRY_PHYGITAL_ENABLED"):
        return "disabled"
    mode = _env("PROPERTYQUARRY_PHYGITAL_MODE", "dry_run").lower() or "dry_run"
    if mode in {"off", "disabled"}:
        return "disabled"
    if mode in {"live", "api"}:
        return "live"
    return "dry_run"


def generation_enabled() -> bool:
    """Paid Kling runs stay off unless this flag is explicitly set."""
    return phygital_mode() == "live" and _env_flag("PROPERTYQUARRY_PHYGITAL_GENERATE")


def candidate_floorplan_url(candidate: dict[str, Any] | None) -> str:
    row = dict(candidate or {})
    facts = dict(row.get("property_facts") or {}) if isinstance(row.get("property_facts"), dict) else {}
    for source in (row, facts):
        for key in _FLOORPLAN_KEYS:
            value = str(source.get(key) or "").strip()
            if value.startswith(("https://", "http://", "/")):
                return value
        nested = source.get("floorplan_urls_json") or source.get("floorplan_urls")
        if isinstance(nested, (list, tuple)):
            for item in nested:
                value = str(item or "").strip()
                if value.startswith(("https://", "http://", "/")):
                    return value
    return ""


def _artifact_key(*, candidate_ref: str, floorplan_url: str) -> str:
    digest = hashlib.sha1(f"{candidate_ref}|{floorplan_url}".encode("utf-8")).hexdigest()[:20]
    return f"phygital-floorplan-{digest}"


def extract_kling_video_file_id(workspace_payload: dict[str, Any] | None) -> str:
    """Return the Kling out_video fileId from a Property workspace graph."""
    root = dict(workspace_payload or {})
    workspace = root.get("workspace") if isinstance(root.get("workspace"), dict) else root
    nodes = workspace.get("nodes") if isinstance(workspace.get("nodes"), list) else []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        global_id = str(node.get("globalId") or "").lower()
        name = str(node.get("name") or "").lower()
        if "kling" not in global_id and name != "kling":
            continue
        outputs = node.get("outputSocketGroup") or []
        if isinstance(outputs, dict):
            outputs = list(outputs.values())
        if not isinstance(outputs, list):
            continue
        for socket in outputs:
            if not isinstance(socket, dict):
                continue
            socket_type = str(socket.get("type") or "").lower()
            socket_name = str(socket.get("name") or "").lower()
            if socket_type != "video" and socket_name != "out_video":
                continue
            value = socket.get("value") or []
            if isinstance(value, dict):
                value = [value]
            if not isinstance(value, list):
                continue
            for item in value:
                if isinstance(item, dict) and item.get("fileId"):
                    return str(item.get("fileId")).strip()
    return ""


def kling_task_schema_from_workspace(workspace_payload: dict[str, Any] | None) -> dict[str, Any]:
    """Copy the live Kling node taskSchema so a new run keeps the Property prompts."""
    root = dict(workspace_payload or {})
    workspace = root.get("workspace") if isinstance(root.get("workspace"), dict) else root
    nodes = workspace.get("nodes") if isinstance(workspace.get("nodes"), list) else []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        global_id = str(node.get("globalId") or "").lower()
        name = str(node.get("name") or "").lower()
        if "kling" not in global_id and name != "kling":
            continue
        meta = node.get("meta") if isinstance(node.get("meta"), dict) else {}
        schema = meta.get("taskSchema") if isinstance(meta.get("taskSchema"), dict) else {}
        if schema:
            return json.loads(json.dumps(schema))
    return {}


def build_kling_task_payload(
    schema: dict[str, Any] | None,
    *,
    floorplan_file_id: int | str,
    workspace_id: int | str,
) -> dict[str, Any]:
    """Swap Kling init_img for the uploaded floorplan. Does not mutate the graph."""
    cloned = json.loads(json.dumps(dict(schema or {})))
    inputs = cloned.get("inputs") if isinstance(cloned.get("inputs"), list) else []
    params = cloned.get("params") if isinstance(cloned.get("params"), list) else []
    outputs = cloned.get("outputs") if isinstance(cloned.get("outputs"), list) else [{"name": "out_video", "type": "video", "value": ""}]
    try:
        numeric_file = int(floorplan_file_id)
    except Exception:
        numeric_file = floorplan_file_id
    try:
        numeric_workspace = int(workspace_id)
    except Exception:
        numeric_workspace = workspace_id
    replaced = False
    for item in inputs:
        if not isinstance(item, dict):
            continue
        if str(item.get("name") or "") != "init_img":
            continue
        item["value"] = numeric_file
        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        meta.pop("dimensions", None)
        item["meta"] = meta
        item["isModified"] = True
        replaced = True
    if not replaced:
        inputs.append(
            {
                "name": "init_img",
                "type": "image",
                "optional": True,
                "isModified": True,
                "value": numeric_file,
                "meta": {},
            }
        )
    schema_id = cloned.get("id") or _KLING_SCHEMA_ID
    try:
        schema_id = int(schema_id)
    except Exception:
        schema_id = _KLING_SCHEMA_ID
    return {
        "id": schema_id,
        "inputs": inputs,
        "params": params,
        "outputs": outputs,
        "workspace_id": numeric_workspace,
    }


def extract_queue_video_file_id(payload: dict[str, Any] | None) -> str:
    """Pull the Kling out_video file id from a queue-position payload."""
    root = dict(payload or {})
    outputs = root.get("outputs") if isinstance(root.get("outputs"), list) else []
    for item in outputs:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").lower()
        if name and name not in {"out_video", "video"}:
            continue
        ids = item.get("id")
        if isinstance(ids, list) and ids:
            return str(ids[0]).strip()
        if isinstance(ids, (int, str)) and str(ids).strip():
            return str(ids).strip()
        value = item.get("value")
        if isinstance(value, list) and value:
            first = value[0]
            if isinstance(first, dict) and first.get("fileId"):
                return str(first.get("fileId")).strip()
            text = str(first or "")
            if "/" in text:
                stem = text.rsplit("/", 1)[-1]
                digits = "".join(ch for ch in stem.split(".")[0] if ch.isdigit())
                if digits:
                    return digits
        if isinstance(value, str) and "/" in value:
            stem = value.rsplit("/", 1)[-1]
            digits = "".join(ch for ch in stem.split(".")[0] if ch.isdigit())
            if digits:
                return digits
    return ""


def select_property_project(
    items: list[dict[str, Any]] | None,
    *,
    project_name: str = _DEFAULT_PROJECT_NAME,
) -> dict[str, Any] | None:
    rows = [row for row in list(items or []) if isinstance(row, dict)]
    wanted = str(project_name or _DEFAULT_PROJECT_NAME).strip().lower() or "property"
    named = [row for row in rows if str(row.get("name") or "").strip().lower() == wanted]
    pool = named or rows
    videos = []
    for row in pool:
        preview = row.get("preview") if isinstance(row.get("preview"), dict) else {}
        mime = str(preview.get("mime_type") or "").lower()
        url = str(preview.get("url") or "").strip()
        if mime.startswith("video/") and url.startswith(("https://", "http://")):
            videos.append(row)
    if videos:
        return videos[0]
    return named[0] if named else None


@dataclass(frozen=True)
class PhygitalFloorplanVideo:
    status: str
    artifact_key: str = ""
    video_url: str = ""
    poster_url: str = ""
    project_id: str = ""
    task_id: str = ""
    reason: str = ""
    raw_response_json: dict[str, Any] = field(default_factory=dict)

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "artifact_key": self.artifact_key,
            "video_url": self.video_url,
            "poster_url": self.poster_url,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "reason": self.reason,
        }


class EnvPhygitalAdapter:
    """Fail-closed Phygital+ client.

    Search overlay reuses the AppSumo "Property" project's Kling MP4. Paid
    generation is a separate path: upload floorplan → POST Kling schema 74 with
    that file as init_img → poll queue-position. Workspace PUT/PATCH is 405 on
    the public API, so the Property graph is never rewritten.
    """

    def __init__(self) -> None:
        self._cache: dict[str, PhygitalFloorplanVideo] = {}
        self._session: _PhygitalSession | None = None

    def lookup_or_generate(
        self,
        *,
        candidate: dict[str, Any],
        floorplan_url: str = "",
    ) -> PhygitalFloorplanVideo:
        mode = phygital_mode()
        row = dict(candidate or {})
        candidate_ref = str(row.get("candidate_ref") or row.get("listing_id") or "").strip()
        resolved_floorplan = str(floorplan_url or candidate_floorplan_url(row)).strip()
        artifact_key = _artifact_key(candidate_ref=candidate_ref, floorplan_url=resolved_floorplan)
        if artifact_key in self._cache:
            return self._cache[artifact_key]
        if mode == "disabled":
            return PhygitalFloorplanVideo(status="disabled", reason="phygital_disabled")
        if mode == "dry_run":
            artifact = PhygitalFloorplanVideo(
                status="dry_run",
                artifact_key=artifact_key,
                reason="phygital_dry_run",
                raw_response_json={"mode": "dry_run", "candidate_ref": candidate_ref},
            )
            self._cache[artifact_key] = artifact
            return artifact
        try:
            # Search ranking never posts Kling. Paid runs go through generate_from_floorplan().
            artifact = self._reuse_property_preview(artifact_key=artifact_key)
        except Exception as exc:
            artifact = PhygitalFloorplanVideo(
                status="error",
                artifact_key=artifact_key,
                reason=f"phygital_error:{type(exc).__name__}",
            )
        self._cache[artifact_key] = artifact
        return artifact

    def generate_from_floorplan(
        self,
        *,
        floorplan_url: str,
        artifact_key: str = "",
        wait_seconds: float | None = None,
    ) -> PhygitalFloorplanVideo:
        """Upload a floorplan and start Kling. Search ranking never calls this."""
        key = artifact_key or _artifact_key(candidate_ref="explicit", floorplan_url=floorplan_url)
        if not generation_enabled():
            return PhygitalFloorplanVideo(
                status="disabled",
                artifact_key=key,
                reason="phygital_generate_disabled",
            )
        if not str(floorplan_url or "").strip():
            return PhygitalFloorplanVideo(
                status="error",
                artifact_key=key,
                reason="phygital_floorplan_missing",
            )
        session = self._ensure_session()
        if isinstance(session, PhygitalFloorplanVideo):
            return session
        items = session.list_projects()
        project = select_property_project(
            items,
            project_name=_env("PROPERTYQUARRY_PHYGITAL_PROJECT_NAME", _DEFAULT_PROJECT_NAME),
        )
        if not project:
            return PhygitalFloorplanVideo(
                status="error",
                artifact_key=key,
                reason="phygital_project_missing",
            )
        project_id = str(project.get("id") or "").strip()
        workspace = session.get_workspace(project_id)
        schema = kling_task_schema_from_workspace(workspace)
        ledger = _module_ledger()
        allowed, spend_reason, _record = ledger.preflight(key)
        if not allowed:
            return PhygitalFloorplanVideo(
                status="error",
                artifact_key=key,
                project_id=project_id,
                reason=spend_reason,
            )
        blob, filename, content_type = session.fetch_bytes(floorplan_url)
        if not blob:
            return PhygitalFloorplanVideo(
                status="error",
                artifact_key=key,
                project_id=project_id,
                reason="phygital_floorplan_fetch_failed",
            )
        file_obj_id = session.upload_fileobject(
            blob,
            filename=filename,
            content_type=content_type,
            workspace_id=project_id,
        )
        if not file_obj_id:
            return PhygitalFloorplanVideo(
                status="error",
                artifact_key=key,
                project_id=project_id,
                reason="phygital_upload_failed",
            )
        payload = build_kling_task_payload(
            schema,
            floorplan_file_id=file_obj_id,
            workspace_id=project_id,
        )
        allowed, spend_reason, _record = ledger.reserve(key)
        if not allowed:
            return PhygitalFloorplanVideo(
                status="error",
                artifact_key=key,
                project_id=project_id,
                reason=spend_reason,
            )
        try:
            started = session.start_kling_task(payload)
        except Exception:
            try:
                ledger.set_outcome(key, "refunded")
            except Exception:
                pass
            raise
        task_id = str(started.get("task_id") or started.get("id") or "").strip()
        if not task_id:
            try:
                ledger.set_outcome(key, "refunded")
            except Exception:
                pass
            return PhygitalFloorplanVideo(
                status="error",
                artifact_key=key,
                project_id=project_id,
                reason="phygital_task_start_failed",
                raw_response_json={"start": started, "file_obj_id": file_obj_id},
            )
        timeout = wait_seconds
        if timeout is None:
            try:
                timeout = float(_env("PROPERTYQUARRY_PHYGITAL_GENERATE_WAIT_SECONDS", "0") or "0")
            except Exception:
                timeout = 0.0
        try:
            ledger.attach_task(key, task_id)
        except Exception:
            pass
        if timeout and timeout > 0:
            polled = session.poll_task(task_id, timeout_seconds=timeout)
            status = str(polled.get("status") or "").strip().lower()
            file_id = extract_queue_video_file_id(polled)
            if status in _ERROR_STATUSES:
                return PhygitalFloorplanVideo(
                    status="error",
                    artifact_key=key,
                    project_id=project_id,
                    task_id=task_id,
                    reason=f"phygital_task_{status or 'error'}",
                    raw_response_json={"poll": polled, "file_obj_id": file_obj_id},
                )
            if file_id:
                video_url = session.download_link(file_id)
                if video_url:
                    try:
                        ledger.set_outcome(key, "completed")
                    except Exception:
                        pass
                    return PhygitalFloorplanVideo(
                        status="ready",
                        artifact_key=key,
                        video_url=video_url,
                        project_id=project_id,
                        task_id=task_id,
                        reason="phygital_generated",
                        raw_response_json={"file_obj_id": file_obj_id, "kling_file_id": file_id},
                    )
            return PhygitalFloorplanVideo(
                status="pending",
                artifact_key=key,
                project_id=project_id,
                task_id=task_id,
                reason="phygital_generating",
                raw_response_json={"poll": polled, "file_obj_id": file_obj_id},
            )
        return PhygitalFloorplanVideo(
            status="pending",
            artifact_key=key,
            project_id=project_id,
            task_id=task_id,
            reason="phygital_task_started",
            raw_response_json={"file_obj_id": file_obj_id, "start": started},
        )

    def _ensure_session(self) -> _PhygitalSession | PhygitalFloorplanVideo:
        email = _env("PHYGITAL_PLUS_EMAIL")
        password = _env("PHYGITAL_PLUS_PASSWORD")
        if not email or not password:
            return PhygitalFloorplanVideo(status="error", reason="phygital_credentials_missing")
        session = self._session or _PhygitalSession(email=email, password=password)
        login = session.sign_in()
        if login.get("status") != "OK":
            return PhygitalFloorplanVideo(
                status="error",
                reason=f"phygital_login:{login.get('status') or 'failed'}",
                raw_response_json={"login_status": login.get("status")},
            )
        self._session = session
        return session

    def _reuse_property_preview(self, *, artifact_key: str) -> PhygitalFloorplanVideo:
        session = self._ensure_session()
        if isinstance(session, PhygitalFloorplanVideo):
            return PhygitalFloorplanVideo(
                status=session.status,
                artifact_key=artifact_key,
                reason=session.reason,
                raw_response_json=session.raw_response_json,
            )
        items = session.list_projects()
        project = select_property_project(
            items,
            project_name=_env("PROPERTYQUARRY_PHYGITAL_PROJECT_NAME", _DEFAULT_PROJECT_NAME),
        )
        if not project:
            return PhygitalFloorplanVideo(
                status="error",
                artifact_key=artifact_key,
                reason="phygital_project_missing",
            )
        preview = project.get("preview") if isinstance(project.get("preview"), dict) else {}
        preview_url = str(preview.get("url") or "").strip()
        poster_url = str(preview.get("poster_url") or preview.get("thumbnail_url") or "").strip()
        project_id = str(project.get("id") or "").strip()
        video_url = ""
        workspace = session.get_workspace(project_id)
        file_id = extract_kling_video_file_id(workspace)
        if file_id:
            video_url = session.download_link(file_id)
        if not video_url:
            video_url = preview_url
        if not video_url:
            return PhygitalFloorplanVideo(
                status="pending",
                artifact_key=artifact_key,
                project_id=project_id,
                reason="phygital_preview_missing",
            )
        return PhygitalFloorplanVideo(
            status="ready",
            artifact_key=artifact_key,
            video_url=video_url,
            poster_url=poster_url,
            project_id=project_id,
            reason="phygital_ready",
        )


class _PhygitalSession:
    def __init__(self, *, email: str, password: str) -> None:
        self.email = email
        self.password = password
        self._jar = http.cookiejar.CookieJar()
        self._ctx = ssl.create_default_context()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self._ctx),
            urllib.request.HTTPCookieProcessor(self._jar),
        )
        self._signed_in = False

    def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        data: dict[str, Any] | list[Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = 25,
        raw_body: bytes | None = None,
    ) -> tuple[int, dict[str, str], str]:
        merged = {
            "User-Agent": "PropertyQuarry/1.0",
            "Origin": _PHYGITAL_APP_ORIGIN,
            "Referer": f"{_PHYGITAL_APP_ORIGIN}/sign-in",
            "Accept": "application/json, text/plain, */*",
        }
        if headers:
            merged.update(headers)
        body = raw_body
        if data is not None and raw_body is None:
            body = json.dumps(data).encode("utf-8")
            merged["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=merged, method=method)
        try:
            with self._opener.open(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", "replace")
                hdrs = {str(k): str(v) for k, v in response.headers.items()}
                return int(response.status), hdrs, raw
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            hdrs = {str(k): str(v) for k, v in exc.headers.items()}
            return int(exc.code), hdrs, raw

    def sign_in(self) -> dict[str, Any]:
        if self._signed_in:
            return {"status": "OK"}
        _, _, raw = self._request(f"{_PHYGITAL_API_ORIGIN}/api/v2/auth/challenge")
        try:
            challenge_body = json.loads(raw)
        except Exception:
            return {"status": "CHALLENGE_FAILED"}
        payload = _solve_altcha(challenge_body)
        if not payload:
            return {"status": "ALTCHA_UNSOLVED"}
        encoded = base64.b64encode(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        code, _, body = self._request(
            f"{_PHYGITAL_API_ORIGIN}/auth/signin",
            method="POST",
            data={
                "formFields": [
                    {"id": "email", "value": self.email},
                    {"id": "password", "value": self.password},
                ]
            },
            headers={
                "rid": "emailpassword",
                "st-auth-mode": "cookie",
                "fdi-version": "1.19",
                "x-captcha-request": encoded,
            },
        )
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {"status": f"HTTP_{code}"}
        if not isinstance(parsed, dict):
            return {"status": f"HTTP_{code}"}
        if parsed.get("status") == "OK":
            self._signed_in = True
        return parsed

    def list_projects(self) -> list[dict[str, Any]]:
        _, _, raw = self._request(
            f"{_PHYGITAL_API_ORIGIN}/api/v2/projects",
            headers={"rid": "session", "Referer": f"{_PHYGITAL_APP_ORIGIN}/"},
        )
        try:
            parsed = json.loads(raw)
        except Exception:
            return []
        items = parsed.get("items") if isinstance(parsed, dict) else parsed
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    def get_workspace(self, project_id: str) -> dict[str, Any]:
        if not project_id:
            return {}
        _, _, raw = self._request(
            f"{_PHYGITAL_API_ORIGIN}/api/v2/workspaces/by_id/{project_id}",
            headers={"rid": "session", "Referer": f"{_PHYGITAL_APP_ORIGIN}/"},
        )
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def download_link(self, file_id: str) -> str:
        if not file_id:
            return ""
        try:
            numeric_id: Any = int(file_id)
        except Exception:
            numeric_id = file_id
        _, _, raw = self._request(
            f"{_PHYGITAL_API_ORIGIN}/api/v2/storage-object/download-links",
            method="POST",
            data={"link_ids": [numeric_id]},
            headers={"rid": "session", "Referer": f"{_PHYGITAL_APP_ORIGIN}/"},
        )
        try:
            parsed = json.loads(raw)
        except Exception:
            return ""
        links = parsed.get("links") if isinstance(parsed, dict) else parsed
        if not isinstance(links, list):
            return ""
        for item in links:
            if not isinstance(item, dict):
                continue
            url = str(item.get("download_link") or item.get("url") or "").strip()
            if url.startswith(("https://", "http://")):
                return url
        return ""

    def fetch_bytes(self, url: str) -> tuple[bytes, str, str]:
        target = str(url or "").strip()
        if not target:
            return b"", "", ""
        if target.startswith("/"):
            target = f"{_PHYGITAL_API_ORIGIN}{target}"
        parsed = urllib.parse.urlparse(target)
        filename = os.path.basename(parsed.path) or "floorplan.png"
        guess = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        request = urllib.request.Request(
            target,
            headers={
                "User-Agent": "PropertyQuarry/1.0",
                "Accept": "image/*,application/pdf,*/*",
            },
        )
        try:
            with self._opener.open(request, timeout=40) as response:
                blob = response.read()
                content_type = str(response.headers.get("Content-Type") or guess).split(";")[0].strip()
                return blob, filename, content_type or guess
        except Exception:
            return b"", filename, guess

    def upload_fileobject(
        self,
        blob: bytes,
        *,
        filename: str,
        content_type: str,
        workspace_id: str = "",
    ) -> str:
        if not blob:
            return ""
        boundary = f"----PQ{uuid.uuid4().hex}"
        name = filename or "floorplan.png"
        mime = content_type or "application/octet-stream"
        chunks: list[bytes] = []
        if workspace_id:
            chunks.append(
                (
                    f"--{boundary}\r\n"
                    'Content-Disposition: form-data; name="workspace_id"\r\n\r\n'
                    f"{workspace_id}\r\n"
                ).encode("utf-8")
            )
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="fileobject"; filename="{name}"\r\n'
                f"Content-Type: {mime}\r\n\r\n"
            ).encode("utf-8")
        )
        chunks.append(blob)
        chunks.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
        code, _, raw = self._request(
            f"{_PHYGITAL_API_ORIGIN}/api/v2/storage-object",
            method="POST",
            raw_body=b"".join(chunks),
            headers={
                "rid": "session",
                "Referer": f"{_PHYGITAL_APP_ORIGIN}/",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            timeout=60,
        )
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {}
        if code >= 400 or not isinstance(parsed, dict):
            return ""
        file_id = parsed.get("file_obj_id") or parsed.get("fileId") or parsed.get("id")
        return str(file_id).strip() if file_id else ""

    def start_kling_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        code, _, raw = self._request(
            f"{_PHYGITAL_API_ORIGIN}/api/v2/tasks/",
            method="POST",
            data=payload,
            headers={"rid": "session", "Referer": f"{_PHYGITAL_APP_ORIGIN}/"},
            timeout=60,
        )
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {"status": f"HTTP_{code}", "raw": raw[:300]}
        if not isinstance(parsed, dict):
            return {"status": f"HTTP_{code}"}
        parsed.setdefault("http_status", code)
        return parsed

    def poll_task(self, task_id: str, *, timeout_seconds: float = 0) -> dict[str, Any]:
        deadline = time.time() + max(0.0, float(timeout_seconds or 0))
        last: dict[str, Any] = {}
        while True:
            _, _, raw = self._request(
                f"{_PHYGITAL_API_ORIGIN}/api/v2/tasks/queue-position/{task_id}",
                headers={"rid": "session", "Referer": f"{_PHYGITAL_APP_ORIGIN}/"},
            )
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = {}
            if isinstance(parsed, dict):
                last = parsed
                status = str(parsed.get("status") or "").strip().lower()
                if status in _DONE_STATUSES or status in _ERROR_STATUSES:
                    return parsed
                if extract_queue_video_file_id(parsed):
                    return parsed
            if time.time() >= deadline:
                return last or {"status": "pending"}
            time.sleep(2.0)


def _solve_altcha(challenge_body: dict[str, Any]) -> dict[str, Any] | None:
    challenge = str(challenge_body.get("challenge") or "")
    salt = str(challenge_body.get("salt") or "")
    if not challenge or not salt:
        return None
    try:
        maxnumber = int(challenge_body.get("maxnumber") or challenge_body.get("maxNumber") or 1_000_000)
    except Exception:
        maxnumber = 1_000_000
    maxnumber = min(max(maxnumber, 1), 5_000_000)
    number = None
    started = time.time()
    for value in range(0, maxnumber + 1):
        digest = hashlib.sha256(f"{salt}{value}".encode("utf-8")).hexdigest()
        if digest == challenge:
            number = value
            break
    if number is None:
        return None
    return {
        "algorithm": str(challenge_body.get("algorithm") or "SHA-256"),
        "challenge": challenge,
        "number": number,
        "salt": salt,
        "signature": challenge_body.get("signature"),
        "took": int((time.time() - started) * 1000),
    }


_ADAPTER: EnvPhygitalAdapter | None = None


def get_phygital_adapter() -> EnvPhygitalAdapter:
    global _ADAPTER
    if _ADAPTER is None:
        _ADAPTER = EnvPhygitalAdapter()
    return _ADAPTER


def reset_phygital_adapter() -> None:
    global _ADAPTER
    _ADAPTER = None
