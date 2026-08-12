from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.dependencies import RequestContext, get_container, get_request_context, require_operator_context
from app.container import AppContainer
from app.domain.models import ToolInvocationRequest
from app.services.ltd_runtime_catalog import LtdRuntimeAction, LtdRuntimeCatalogService
from app.services.ltd_runtime_skill_projection import infer_onemin_media_feature_type
from app.services.tool_execution import ToolExecutionError

router = APIRouter(
    prefix="/v1/ltds/runtime-catalog",
    tags=["ltd-runtime"],
    dependencies=[Depends(require_operator_context)],
)
log = logging.getLogger("ea.api.ltd_runtime")


class LtdDiscoverAccountIn(BaseModel):
    binding_id: str = Field(min_length=1, max_length=200)
    requested_fields: list[str] = Field(default_factory=list)
    instructions: str = Field(default="", max_length=4000)
    run_url: str = Field(default="", max_length=4000)


class LtdActionExecutionOut(BaseModel):
    service_name: str
    action_key: str
    tool_name: str
    action_kind: str
    target_ref: str
    output_json: dict[str, object]
    receipt_json: dict[str, object]


_PRIVATE_LTD_OUTPUT_KEYS = {
    "api_key",
    "binding_id",
    "connector_name",
    "credential_id",
    "external_account_ref",
    "provider_account_name",
    "provider_key_slot",
    "raw_response",
    "requested_url",
    "run_url",
    "secret_env_name",
    "task_id",
    "workflow_id",
}
_CUSTOMER_LTD_RECEIPT_KEYS = (
    "handler_key",
    "invocation_contract",
    "provider_key",
    "provider_backend",
    "model",
    "feature_type",
    "tool_version",
    "tool_name",
    "action_kind",
    "service_key",
    "render_status",
)


def _customer_ltd_output(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): _customer_ltd_output(nested)
            for key, nested in value.items()
            if str(key).strip().lower() not in _PRIVATE_LTD_OUTPUT_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_customer_ltd_output(nested) for nested in value]
    return value


def _customer_ltd_target_ref(*, target_ref: object, action: LtdRuntimeAction) -> str:
    normalized = str(target_ref or "").strip()
    if normalized.startswith(("https://", "http://", "onemin:", "provider://")):
        return normalized
    if normalized.startswith("browseract:"):
        return f"browseract:{action.action_key}"
    return f"tool:{action.action_key}"


def _verified_customer_ltd_receipt(
    *,
    result: object,
    action: LtdRuntimeAction,
    principal_id: str,
) -> dict[str, object]:
    receipt = dict(getattr(result, "receipt_json", {}) or {})
    result_tool_name = str(getattr(result, "tool_name", "") or "").strip()
    result_action_kind = str(getattr(result, "action_kind", "") or "").strip()
    target_ref = str(getattr(result, "target_ref", "") or "").strip()
    receipt_verified = bool(
        principal_id
        and receipt.get("principal_id") == principal_id
        and receipt.get("handler_key") == action.tool_name
        and receipt.get("invocation_contract") == "tool.v1"
        and result_tool_name == action.tool_name
        and result_action_kind == action.action_kind
        and target_ref
    )
    if action.tool_name.startswith("provider."):
        receipt_verified = bool(
            receipt_verified
            and str(receipt.get("provider_key") or "").strip()
            and str(receipt.get("provider_backend") or "").strip()
        )
    if not receipt_verified:
        log.error(
            "ltd_runtime_receipt_unverified action=%s principal=%s tool=%s",
            action.action_key,
            principal_id,
            result_tool_name,
        )
        raise HTTPException(
            status_code=502,
            detail="ltd_runtime_execution_receipt_unverified",
        )
    if str(receipt.get("provider_key") or "").strip():
        proof_scope = "provider_call"
    elif any(str(receipt.get(key) or "").strip() for key in ("task_id", "workflow_id", "requested_url")):
        proof_scope = "browser_session_call"
    else:
        proof_scope = "principal_bound_tool_invocation"
    projected: dict[str, object] = {
        "status": "verified",
        "principal_bound": True,
        "proof_scope": proof_scope,
    }
    for key in _CUSTOMER_LTD_RECEIPT_KEYS:
        value = receipt.get(key)
        if value not in (None, "", [], {}):
            projected[key] = value
    return projected


def _catalog(container: AppContainer) -> LtdRuntimeCatalogService:
    return LtdRuntimeCatalogService(provider_registry=container.provider_registry)


def _http_status_for_tool_error(detail: str) -> int:
    if detail.startswith("tool_not_registered:") or detail.startswith("connector_binding_not_found:"):
        return 404
    if detail == "principal_scope_mismatch" or detail.startswith("connector_binding_scope_mismatch:"):
        return 403
    if (
        detail.startswith("connector_binding_required:")
        or detail in {"tool_name_required", "principal_id_required", "connector_dispatch_channel_required"}
        or detail.startswith("run_url_or_workflow_id_required:")
        or detail.startswith("prompt_required:")
    ):
        return 400
    return 409


def _resolved_action_or_404(
    *,
    container: AppContainer,
    service_name: str,
    action_key: str,
) -> tuple[str, LtdRuntimeAction]:
    catalog = _catalog(container)
    profile = catalog.get_profile(service_name)
    if profile is None:
        raise HTTPException(status_code=404, detail="ltd_service_not_found")
    action = catalog.get_action(profile.service_name, action_key)
    if action is None:
        raise HTTPException(status_code=404, detail="ltd_runtime_action_not_found")
    return profile.service_name, action


def _execute_catalog_action(
    *,
    container: AppContainer,
    context: RequestContext,
    service_name: str,
    action: LtdRuntimeAction,
    payload_json: dict[str, object],
) -> LtdActionExecutionOut:
    if not action.executable or action.execution_mode != "tool_execution":
        raise HTTPException(status_code=409, detail="ltd_runtime_action_not_executable")
    payload = dict(payload_json or {})
    payload.setdefault("action_key", action.action_key)
    if action.action_key == "discover_account":
        payload["service_name"] = service_name
    if action.tool_name == "provider.onemin.media_transform" and not str(payload.get("feature_type") or "").strip():
        inferred_feature_type = infer_onemin_media_feature_type(input_json=payload)
        if inferred_feature_type:
            payload["feature_type"] = inferred_feature_type
    invocation = ToolInvocationRequest(
        session_id=f"ltd-runtime:{uuid.uuid4()}",
        step_id=f"ltd-runtime-step:{uuid.uuid4()}",
        tool_name=action.tool_name,
        action_kind=action.action_kind,
        payload_json=payload,
        context_json={"principal_id": context.principal_id},
    )
    try:
        result = container.tool_execution.execute_invocation(invocation)
    except ToolExecutionError as exc:
        detail = str(exc or "tool_execution_failed")
        log.warning(
            "ltd_runtime_action_failed service=%s action=%s principal=%s detail=%s",
            service_name,
            action.action_key,
            context.principal_id,
            detail,
        )
        raise HTTPException(status_code=_http_status_for_tool_error(detail), detail=detail) from exc
    receipt_json = _verified_customer_ltd_receipt(
        result=result,
        action=action,
        principal_id=context.principal_id,
    )
    output_json = _customer_ltd_output(dict(result.output_json or {}))
    if not isinstance(output_json, dict):
        output_json = {}
    return LtdActionExecutionOut(
        service_name=service_name,
        action_key=action.action_key,
        tool_name=result.tool_name,
        action_kind=result.action_kind,
        target_ref=_customer_ltd_target_ref(
            target_ref=result.target_ref,
            action=action,
        ),
        output_json=output_json,
        receipt_json=receipt_json,
    )


@router.get("")
def list_runtime_catalog(
    container: AppContainer = Depends(get_container),
) -> list[dict[str, object]]:
    return [profile.as_dict() for profile in _catalog(container).list_profiles()]


@router.get("/{service_name}")
def get_runtime_profile(
    service_name: str,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    profile = _catalog(container).get_profile(service_name)
    if profile is None:
        raise HTTPException(status_code=404, detail="ltd_service_not_found")
    return profile.as_dict()


@router.post("/{service_name}/discover-account")
def discover_account(
    service_name: str,
    body: LtdDiscoverAccountIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> LtdActionExecutionOut:
    resolved_service_name, action = _resolved_action_or_404(
        container=container,
        service_name=service_name,
        action_key="discover_account",
    )
    payload_json: dict[str, object] = {
        "binding_id": body.binding_id,
        "requested_fields": list(body.requested_fields or []),
    }
    if str(body.instructions or "").strip():
        payload_json["instructions"] = body.instructions
    if str(body.run_url or "").strip():
        payload_json["run_url"] = body.run_url
    return _execute_catalog_action(
        container=container,
        context=context,
        service_name=resolved_service_name,
        action=action,
        payload_json=payload_json,
    )


@router.post("/{service_name}/inspect-workspace")
def inspect_workspace(
    service_name: str,
    body: dict[str, object] = Body(default_factory=dict),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> LtdActionExecutionOut:
    resolved_service_name, action = _resolved_action_or_404(
        container=container,
        service_name=service_name,
        action_key="inspect_workspace",
    )
    return _execute_catalog_action(
        container=container,
        context=context,
        service_name=resolved_service_name,
        action=action,
        payload_json=body,
    )


@router.post("/{service_name}/actions/{action_key}")
def execute_action(
    service_name: str,
    action_key: str,
    body: dict[str, object] = Body(default_factory=dict),
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> LtdActionExecutionOut:
    resolved_service_name, action = _resolved_action_or_404(
        container=container,
        service_name=service_name,
        action_key=action_key,
    )
    return _execute_catalog_action(
        container=container,
        context=context,
        service_name=resolved_service_name,
        action=action,
        payload_json=body,
    )
