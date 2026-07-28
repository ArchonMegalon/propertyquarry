from __future__ import annotations

import hashlib
import http.cookiejar
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


WORKLLM_DEFAULT_BASE_URL = "https://girschele-workspace.workllm.io"
WORKLLM_DEFAULT_ALLOWED_HOSTS = frozenset({"girschele-workspace.workllm.io"})
WORKLLM_MAX_JSON_RESPONSE_BYTES = 2 * 1024 * 1024
WORKLLM_MAX_STREAM_RESPONSE_BYTES = 4 * 1024 * 1024


def _env_flag(name: str, *, default: bool = False) -> bool:
    raw = str(os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def workllm_runtime_enabled() -> bool:
    return _env_flag("WORKLLM_PROVIDER_VERIFIED") and _env_flag("WORKLLM_RUNTIME_ENABLED")


def workllm_enabled() -> bool:
    required = (
        "WORKLLM_BASE_URL",
        "WORKLLM_EMAIL",
        "WORKLLM_PASSWORD",
        "WORKLLM_REAL_ESTATE_AGENT_ID",
    )
    return workllm_runtime_enabled() and all(str(os.getenv(name) or "").strip() for name in required)


def _workllm_allowed_hosts() -> frozenset[str]:
    configured = {
        str(item or "").strip().lower()
        for item in str(os.getenv("WORKLLM_ALLOWED_HOSTS") or "").split(",")
        if str(item or "").strip()
    }
    return frozenset(configured or WORKLLM_DEFAULT_ALLOWED_HOSTS)


def _validated_workllm_base_url(raw_base_url: str) -> str:
    normalized = str(raw_base_url or WORKLLM_DEFAULT_BASE_URL).strip().rstrip("/")
    parsed = urllib.parse.urlparse(normalized)
    host = str(parsed.hostname or "").strip().lower()
    if parsed.scheme != "https":
        raise WorkllmApiError(400, "workllm_https_required")
    if parsed.username or parsed.password:
        raise WorkllmApiError(400, "workllm_base_url_credentials_forbidden")
    if not host or host not in _workllm_allowed_hosts():
        raise WorkllmApiError(400, "workllm_host_not_allowed")
    return urllib.parse.urlunparse(("https", parsed.netloc, parsed.path.rstrip("/"), "", "", "")).rstrip("/")


class _WorkllmNoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise urllib.error.HTTPError(req.full_url, code, "workllm_redirect_blocked", headers, fp)


@dataclass(frozen=True)
class WorkllmApiError(RuntimeError):
    status_code: int
    detail: str
    retry_after_seconds: float = 0.0

    def __str__(self) -> str:
        return self.detail


def _provider_error_detail(status_code: int, body: bytes, reason: str) -> str:
    if status_code in {301, 302, 303, 307, 308} and "workllm_redirect_blocked" in reason:
        return "workllm_redirect_blocked"
    message = ""
    try:
        parsed = json.loads(body.decode("utf-8"))
        if isinstance(parsed, dict):
            message = str(parsed.get("message") or parsed.get("error") or "").strip().lower()
    except Exception:
        message = ""
    if "not available on your current plan" in message:
        return "workllm_feature_unavailable_current_plan"
    if "superadmin access required" in message:
        return "workllm_superadmin_required"
    if status_code in {401, 403} and any(token in message for token in ("login", "credential", "password", "auth")):
        return "workllm_authentication_failed"
    return f"workllm_http_{status_code}"


class WorkllmClient:
    def __init__(
        self,
        *,
        email: str = "",
        password: str = "",
        base_url: str = "",
        timeout_seconds: float = 30.0,
        opener: object | None = None,
    ) -> None:
        self._email = str(email or os.getenv("WORKLLM_EMAIL") or "").strip()
        self._password = str(password or os.getenv("WORKLLM_PASSWORD") or "").strip()
        self._base_url = _validated_workllm_base_url(
            str(base_url or os.getenv("WORKLLM_BASE_URL") or WORKLLM_DEFAULT_BASE_URL)
        )
        self._timeout_seconds = float(timeout_seconds or 30.0)
        self._authenticated = False
        if opener is None:
            cookie_jar = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(
                _WorkllmNoRedirectHandler(),
                urllib.request.HTTPCookieProcessor(cookie_jar),
            )
        self._opener = opener

    @property
    def configured(self) -> bool:
        return bool(self._email and self._password)

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def workspace_host(self) -> str:
        return str(urllib.parse.urlparse(self._base_url).hostname or "")

    @property
    def account_hash(self) -> str:
        if not self._email:
            return ""
        return hashlib.sha256(self._email.lower().encode("utf-8")).hexdigest()[:12]

    def _headers(self, *, accept: str = "application/json") -> dict[str, str]:
        return {
            "Accept": accept,
            "Content-Type": "application/json",
            "User-Agent": "PropertyQuarry-WorkLLM/1.0",
        }

    @staticmethod
    def _read_bounded(response: object, *, limit: int) -> bytes:
        try:
            body = response.read(limit + 1)  # type: ignore[attr-defined]
        except TypeError:
            body = response.read()  # type: ignore[attr-defined]
        if len(body) > limit:
            raise WorkllmApiError(502, "workllm_response_too_large")
        return body

    def _open(self, request: urllib.request.Request) -> object:
        try:
            return self._opener.open(request, timeout=self._timeout_seconds)  # type: ignore[attr-defined]
        except urllib.error.HTTPError as exc:
            retry_after = 0.0
            try:
                retry_after = float(exc.headers.get("Retry-After") or 0)
            except Exception:
                retry_after = 0.0
            try:
                body = exc.read(WORKLLM_MAX_JSON_RESPONSE_BYTES + 1)
            except Exception:
                body = b""
            reason = str(getattr(exc, "reason", "") or getattr(exc, "msg", "") or exc or "")
            detail = _provider_error_detail(int(exc.code), body, reason)
            raise WorkllmApiError(int(exc.code), detail, retry_after_seconds=retry_after) from exc
        except urllib.error.URLError as exc:
            raise WorkllmApiError(502, "workllm_unreachable") from exc

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, object] | None = None,
        require_auth: bool = True,
    ) -> dict[str, object]:
        if require_auth:
            self._ensure_authenticated()
        normalized_path = "/" + str(path or "").lstrip("/")
        data = None if payload is None else json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}{normalized_path}",
            data=data,
            headers=self._headers(),
            method=method.upper(),
        )
        response = self._open(request)
        content_type = ""
        try:
            content_type = str(response.getheader("Content-Type", "") or "").lower()  # type: ignore[attr-defined]
        except Exception:
            content_type = "application/json"
        if content_type and "json" not in content_type:
            raise WorkllmApiError(502, "workllm_unexpected_content_type")
        body = self._read_bounded(response, limit=WORKLLM_MAX_JSON_RESPONSE_BYTES)
        if not body:
            return {}
        try:
            parsed = json.loads(body.decode("utf-8"))
        except Exception as exc:
            raise WorkllmApiError(502, "workllm_invalid_json") from exc
        return parsed if isinstance(parsed, dict) else {"items": parsed}

    def authenticate(self) -> dict[str, object]:
        if not self.configured:
            raise WorkllmApiError(503, "workllm_credentials_not_configured")
        response = self._request_json(
            "POST",
            "/api/login/authenticate",
            payload={"email": self._email, "password": self._password},
            require_auth=False,
        )
        if response.get("success") is False:
            raise WorkllmApiError(401, "workllm_authentication_failed")
        self._authenticated = True
        return response

    def _ensure_authenticated(self) -> None:
        if not self._authenticated:
            self.authenticate()

    def list_tools(self) -> dict[str, object]:
        return self._request_json("GET", "/api/tools")

    def list_agents(self) -> dict[str, object]:
        return self._request_json("GET", "/api/agents")

    def list_agent_templates(self) -> dict[str, object]:
        return self._request_json("GET", "/api/agent-templates/catalog")

    def get_agent_template(self, template_id: str) -> dict[str, object]:
        normalized = str(template_id or "").strip()
        if not normalized:
            raise WorkllmApiError(400, "workllm_template_id_required")
        encoded = urllib.parse.quote(normalized, safe="")
        return self._request_json("GET", f"/api/agent-templates/{encoded}/detail")

    @staticmethod
    def _template_rows(payload: dict[str, object]) -> list[dict[str, Any]]:
        candidates: list[object] = [payload.get("templates")]
        data = payload.get("data")
        if isinstance(data, dict):
            candidates.append(data.get("templates"))
        for candidate in candidates:
            if isinstance(candidate, list):
                return [dict(item) for item in candidate if isinstance(item, dict)]
        return []

    def list_real_estate_templates(self) -> list[dict[str, Any]]:
        rows = self._template_rows(self.list_agent_templates())
        return [
            row
            for row in rows
            if "real-estate-agency" in json.dumps(row, ensure_ascii=True, sort_keys=True).lower()
            or "real estate" in json.dumps(row, ensure_ascii=True, sort_keys=True).lower()
        ]

    def run_real_estate_advisory(
        self,
        *,
        agent_id: str,
        prompt: str,
        model: str = "",
        user_timezone: str = "Europe/Vienna",
    ) -> dict[str, object]:
        self._ensure_authenticated()
        normalized_agent_id = str(agent_id or "").strip()
        normalized_prompt = str(prompt or "").strip()
        if not normalized_agent_id:
            raise WorkllmApiError(400, "workllm_agent_id_required")
        if not normalized_prompt:
            raise WorkllmApiError(400, "workllm_prompt_required")
        payload: dict[str, object] = {
            "content": normalized_prompt,
            "web_search_mode": "OFF",
            "memory_mode": "OFF",
            "default_assistant_id": normalized_agent_id,
            "agent_id": f"assistant:{normalized_agent_id}",
            "agent_kind": "rag_assistant",
            "user_timezone": str(user_timezone or "Europe/Vienna").strip() or "Europe/Vienna",
            "team_ai_mode": True,
        }
        normalized_model = str(model or "").strip()
        if normalized_model:
            payload["model"] = normalized_model
        request = urllib.request.Request(
            f"{self._base_url}/api/llm/threads/messages",
            data=json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8"),
            headers=self._headers(accept="text/event-stream"),
            method="POST",
        )
        response = self._open(request)
        body = self._read_bounded(response, limit=WORKLLM_MAX_STREAM_RESPONSE_BYTES)
        return self._parse_advisory_stream(response=response, body=body)

    @staticmethod
    def _parse_advisory_stream(*, response: object, body: bytes) -> dict[str, object]:
        text_parts: list[str] = []
        citations: list[object] = []
        document_sources: list[object] = []
        thread_id = ""
        message_id = ""
        model = ""
        try:
            thread_id = str(response.getheader("x-thread-id", "") or "")  # type: ignore[attr-defined]
            message_id = str(response.getheader("x-message-id", "") or "")  # type: ignore[attr-defined]
            model = str(response.getheader("x-model", "") or "")  # type: ignore[attr-defined]
        except Exception:
            pass
        decoded = body.decode("utf-8", errors="replace")
        for raw_line in decoded.splitlines():
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                event = json.loads(data)
            except Exception:
                text_parts.append(data)
                continue
            if not isinstance(event, dict):
                continue
            if event.get("t") is not None:
                text_parts.append(str(event.get("t") or ""))
            if not thread_id:
                thread_id = str(event.get("threadId") or event.get("thread_id") or "")
            if not message_id:
                message_id = str(event.get("messageId") or event.get("message_id") or "")
            if not model:
                model = str(event.get("model") or event.get("turn_model") or "")
            event_citations = event.get("sources") or event.get("citations")
            if isinstance(event_citations, list):
                citations = event_citations[:100]
            if isinstance(event.get("document_sources"), list):
                document_sources = list(event.get("document_sources") or [])[:100]
        return {
            "text": "".join(text_parts).strip(),
            "citations": citations,
            "document_sources": document_sources,
            "thread_id": thread_id,
            "message_id": message_id,
            "model": model,
            "memory_mode": "OFF",
            "web_search_mode": "OFF",
        }


def redacted_workllm_error(error: BaseException) -> dict[str, object]:
    if isinstance(error, WorkllmApiError):
        return {
            "status_code": error.status_code,
            "detail": error.detail,
            "retry_after_seconds": error.retry_after_seconds,
        }
    return {"status_code": 500, "detail": error.__class__.__name__}
