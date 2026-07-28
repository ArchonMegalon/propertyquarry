from __future__ import annotations

import io
import json
import urllib.error

import pytest

from app.domain.models import ToolDefinition, ToolInvocationRequest
from app.services.provider_registry import ProviderRegistryService
from app.services.tool_execution_common import ToolExecutionError
from app.services.tool_execution_workllm_adapter import WorkllmToolAdapter
from app.services.workllm_client import WorkllmApiError, WorkllmClient, workllm_enabled


class _FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        content_type: str = "application/json",
        headers: dict[str, str] | None = None,
    ) -> None:
        self._body = body
        self._headers = {"content-type": content_type, **{str(k).lower(): str(v) for k, v in (headers or {}).items()}}

    def read(self, size: int = -1) -> bytes:
        return self._body if size is None or size < 0 else self._body[:size]

    def getheader(self, name: str, default: str = "") -> str:
        return self._headers.get(str(name).lower(), default)


class _FakeOpener:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[object, float]] = []

    def open(self, request, timeout: float):  # noqa: ANN001
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _login_response() -> _FakeResponse:
    return _FakeResponse(b'{"success":true,"data":{"message":"Login successful"}}')


def _set_ready_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKLLM_BASE_URL", "https://girschele-workspace.workllm.io")
    monkeypatch.setenv("WORKLLM_EMAIL", "operator@example.test")
    monkeypatch.setenv("WORKLLM_PASSWORD", "secret-password")
    monkeypatch.setenv("WORKLLM_REAL_ESTATE_AGENT_ID", "agent-real-estate")
    monkeypatch.setenv("WORKLLM_PROVIDER_VERIFIED", "true")
    monkeypatch.setenv("WORKLLM_RUNTIME_ENABLED", "true")


def test_workllm_client_authenticates_then_filters_real_estate_templates() -> None:
    catalog = {
        "data": {
            "templates": [
                {"id": "real-1", "name": "Market Knowledge Agent", "industry_key": "real-estate-agency"},
                {"id": "other-1", "name": "Support Agent", "industry_key": "customer-support"},
            ]
        }
    }
    opener = _FakeOpener([_login_response(), _FakeResponse(json.dumps(catalog).encode("utf-8"))])
    client = WorkllmClient(
        email="operator@example.test",
        password="secret-password",
        opener=opener,
    )

    assert [item["id"] for item in client.list_real_estate_templates()] == ["real-1"]
    login_request, _ = opener.requests[0]
    assert login_request.full_url.endswith("/api/login/authenticate")
    login_payload = json.loads(login_request.data.decode("utf-8"))
    assert login_payload == {"email": "operator@example.test", "password": "secret-password"}
    catalog_request, _ = opener.requests[1]
    assert catalog_request.full_url.endswith("/api/agent-templates/catalog")


def test_workllm_client_maps_plan_entitlement_denial_without_leaking_provider_body() -> None:
    denied = urllib.error.HTTPError(
        "https://girschele-workspace.workllm.io/api/agent-templates/catalog",
        403,
        "Forbidden",
        {},
        io.BytesIO(
            b"{\"success\":false,\"message\":\"Feature 'ai_assistant' is not available on your current plan (NONE)\"}"
        ),
    )
    client = WorkllmClient(
        email="operator@example.test",
        password="secret-password",
        opener=_FakeOpener([_login_response(), denied]),
    )

    with pytest.raises(WorkllmApiError) as exc:
        client.list_agent_templates()

    assert exc.value.status_code == 403
    assert str(exc.value) == "workllm_feature_unavailable_current_plan"
    assert "NONE" not in str(exc.value)
    assert "operator@example.test" not in str(exc.value)


def test_workllm_client_rejects_non_https_and_unapproved_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(WorkllmApiError, match="workllm_https_required"):
        WorkllmClient(email="x", password="y", base_url="http://girschele-workspace.workllm.io")

    monkeypatch.delenv("WORKLLM_ALLOWED_HOSTS", raising=False)
    with pytest.raises(WorkllmApiError, match="workllm_host_not_allowed"):
        WorkllmClient(email="x", password="y", base_url="https://evil.example")


def test_workllm_client_blocks_redirects_without_leaking_credentials() -> None:
    redirect = urllib.error.HTTPError(
        "https://girschele-workspace.workllm.io/api/login/authenticate",
        302,
        "workllm_redirect_blocked",
        {},
        io.BytesIO(b""),
    )
    client = WorkllmClient(
        email="operator@example.test",
        password="secret-password",
        opener=_FakeOpener([redirect]),
    )

    with pytest.raises(WorkllmApiError) as exc:
        client.authenticate()

    assert str(exc.value) == "workllm_redirect_blocked"
    assert "secret-password" not in str(exc.value)
    assert "operator@example.test" not in str(exc.value)


def test_workllm_client_parses_advisory_stream_with_memory_and_web_off() -> None:
    event = json.dumps(
        {
            "t": json.dumps({"summary": "Useful"}, separators=(",", ":")),
            "threadId": "thread-1",
            "messageId": "message-1",
            "model": "model-1",
        },
        separators=(",", ":"),
    ).encode("utf-8")
    stream = b"data: " + event + b"\ndata: [DONE]\n"
    opener = _FakeOpener(
        [
            _login_response(),
            _FakeResponse(
                stream,
                content_type="text/event-stream",
                headers={"x-thread-id": "thread-1", "x-message-id": "message-1"},
            ),
        ]
    )
    client = WorkllmClient(email="operator@example.test", password="secret-password", opener=opener)

    result = client.run_real_estate_advisory(agent_id="agent-1", prompt="Review this packet")

    assert result["text"] == '{"summary":"Useful"}'
    assert result["thread_id"] == "thread-1"
    assert result["memory_mode"] == "OFF"
    assert result["web_search_mode"] == "OFF"
    run_request, _ = opener.requests[1]
    sent = json.loads(run_request.data.decode("utf-8"))
    assert sent["default_assistant_id"] == "agent-1"
    assert sent["agent_id"] == "assistant:agent-1"
    assert sent["memory_mode"] == "OFF"
    assert sent["web_search_mode"] == "OFF"


def test_workllm_provider_registry_fails_closed_until_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "WORKLLM_BASE_URL",
        "WORKLLM_EMAIL",
        "WORKLLM_PASSWORD",
        "WORKLLM_REAL_ESTATE_AGENT_ID",
        "WORKLLM_PROVIDER_VERIFIED",
        "WORKLLM_RUNTIME_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    state = ProviderRegistryService().binding_state("workllm")
    assert state is not None
    assert state.auth_mode == "workspace_password"
    assert state.state == "unconfigured"
    assert state.secret_configured is False
    assert "real_estate_advisory" in state.capabilities

    _set_ready_env(monkeypatch)
    ready = ProviderRegistryService().binding_state("workllm")
    assert ready is not None
    assert ready.state == "ready"
    assert ready.secret_configured is True
    assert workllm_enabled() is True
    route = ProviderRegistryService().route_tool("provider.workllm.real_estate_advisory")
    assert route.provider_key == "workllm"
    assert route.capability_key == "real_estate_advisory"


def test_workllm_adapter_redacts_contacts_and_emits_review_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_ready_env(monkeypatch)

    class StubClient:
        workspace_host = "girschele-workspace.workllm.io"
        account_hash = "account-hash"
        prompt = ""

        def run_real_estate_advisory(self, *, agent_id: str, prompt: str, model: str, user_timezone: str):
            self.prompt = prompt
            assert agent_id == "agent-real-estate"
            return {
                "text": '{"summary":"Check the service charges"}',
                "citations": [],
                "document_sources": [],
                "thread_id": "thread-1",
                "message_id": "message-1",
                "model": "model-1",
            }

    client = StubClient()
    adapter = WorkllmToolAdapter(client=client)  # type: ignore[arg-type]
    definition = ToolDefinition(
        tool_name="provider.workllm.real_estate_advisory",
        version="v1",
        input_schema_json={},
        output_schema_json={},
        policy_json={},
        allowed_channels=(),
        approval_default="required",
        enabled=True,
        updated_at="",
    )
    result = adapter.execute_real_estate_advisory(
        ToolInvocationRequest(
            session_id="session-1",
            step_id="step-1",
            tool_name=definition.tool_name,
            action_kind="property.advisory",
            payload_json={
                "property_packet": {
                    "address": "Demo Street 1",
                    "asking_price": 300000,
                    "broker_email": "broker@example.test",
                    "phone": "+1 555 0100",
                }
            },
            context_json={"principal_id": "principal-1"},
        ),
        definition,
    )

    assert "broker@example.test" not in client.prompt
    assert "+1 555 0100" not in client.prompt
    assert "<redacted>" in client.prompt
    assert result.output_json["review_required"] is True
    assert result.receipt_json["memory_mode"] == "OFF"
    assert result.receipt_json["direct_contact_allowed"] is False
    serialized = json.dumps(result.receipt_json)
    assert "secret-password" not in serialized
    assert "operator@example.test" not in serialized


def test_workllm_adapter_rejects_unverified_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_ready_env(monkeypatch)
    monkeypatch.setenv("WORKLLM_PROVIDER_VERIFIED", "false")
    adapter = WorkllmToolAdapter()
    definition = ToolDefinition(
        tool_name="provider.workllm.real_estate_advisory",
        version="v1",
        input_schema_json={},
        output_schema_json={},
        policy_json={},
        allowed_channels=(),
        approval_default="required",
        enabled=True,
        updated_at="",
    )
    with pytest.raises(ToolExecutionError, match="workllm_runtime_not_verified"):
        adapter.execute_real_estate_advisory(
            ToolInvocationRequest(
                session_id="session-1",
                step_id="step-1",
                tool_name=definition.tool_name,
                action_kind="property.advisory",
                payload_json={"property_packet": {"address": "Demo Street 1"}},
                context_json={},
            ),
            definition,
        )
