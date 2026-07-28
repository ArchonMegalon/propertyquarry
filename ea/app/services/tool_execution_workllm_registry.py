from __future__ import annotations

from typing import Callable

from app.domain.models import ToolInvocationRequest, ToolInvocationResult
from app.services.tool_execution_workllm_adapter import WorkllmToolAdapter
from app.services.tool_runtime import ToolRuntimeService

ToolExecutionHandler = Callable[[ToolInvocationRequest, object], ToolInvocationResult]


def register_builtin_workllm_real_estate_advisory(
    *,
    tool_runtime: ToolRuntimeService,
    register_handler: Callable[[str, ToolExecutionHandler], None],
    workllm_adapter: WorkllmToolAdapter,
) -> None:
    tool_name = "provider.workllm.real_estate_advisory"
    if tool_runtime.get_tool(tool_name) is None:
        tool_runtime.upsert_tool(
            tool_name=tool_name,
            version="v1",
            input_schema_json={
                "type": "object",
                "required": ["property_packet"],
                "properties": {
                    "property_packet": {"type": "object"},
                    "extra_instructions": {"type": "string", "maxLength": 2000},
                    "model": {"type": "string"},
                    "user_timezone": {"type": "string"},
                },
            },
            output_schema_json={
                "type": "object",
                "required": [
                    "normalized_text",
                    "structured_output_json",
                    "provider_key",
                    "review_required",
                ],
            },
            policy_json={
                "builtin": True,
                "provider": "workllm",
                "action_kind": "property.advisory",
                "quota_consuming": True,
                "review_required": True,
                "memory_write_allowed": False,
                "web_search_allowed": False,
                "direct_external_action_allowed": False,
            },
            approval_default="required",
            enabled=True,
        )
    register_handler(tool_name, workllm_adapter.execute_real_estate_advisory)
