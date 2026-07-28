from __future__ import annotations

from typing import Callable

from app.domain.models import ToolDefinition, ToolInvocationRequest, ToolInvocationResult
from app.services.tool_execution_workllm_adapter import WorkllmToolAdapter
from app.services.tool_execution_workllm_registry import register_builtin_workllm_real_estate_advisory
from app.services.tool_runtime import ToolRuntimeService

ToolExecutionHandler = Callable[[ToolInvocationRequest, ToolDefinition], ToolInvocationResult]


class WorkllmToolExecutionModule:
    def __init__(
        self,
        *,
        tool_runtime: ToolRuntimeService,
    ) -> None:
        self._tool_runtime = tool_runtime
        self._adapter = WorkllmToolAdapter()

    def register_real_estate_advisory(
        self,
        register_handler: Callable[[str, ToolExecutionHandler], None],
    ) -> None:
        register_builtin_workllm_real_estate_advisory(
            tool_runtime=self._tool_runtime,
            register_handler=register_handler,
            workllm_adapter=self._adapter,
        )
