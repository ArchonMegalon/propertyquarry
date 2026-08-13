#!/usr/bin/env python3
"""Bind and prove PropertyQuarry's governed 1min evaluation lane.

The script is intended to run inside the PropertyQuarry worker, where provider
credentials already live.  It never reads or serializes a credential value.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
EA_ROOT = ROOT / "ea"
if str(EA_ROOT) not in sys.path:
    sys.path.insert(0, str(EA_ROOT))

CONTRACT_NAME = "propertyquarry.onemin_live_binding.v1"
PROVIDER_KEY = "onemin"
TOOL_NAME = "provider.onemin.code_generate"
CAPABILITY_KEY = "code_generate"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _positive_int(value: object, *, default: int = 0) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return default


def _safe_text(value: object, *, limit: int = 160) -> str:
    return str(value or "").strip()[:limit]


def _stable_binding_id(principal_id: str) -> str:
    digest = hashlib.sha256(principal_id.encode("utf-8")).hexdigest()[:24]
    return f"propertyquarry-onemin-{digest}"


def safe_onemin_health_snapshot(
    health: Mapping[str, object],
    *,
    observed_at: str | None = None,
) -> dict[str, object]:
    """Project provider health through a strict, secret-free allowlist."""

    providers = health.get("providers")
    provider_rows = dict(providers) if isinstance(providers, Mapping) else {}
    raw_provider = provider_rows.get(PROVIDER_KEY)
    provider = dict(raw_provider) if isinstance(raw_provider, Mapping) else {}
    raw_slots = provider.get("slots")
    slots = list(raw_slots) if isinstance(raw_slots, (list, tuple)) else []

    safe_slots: list[dict[str, str]] = []
    state_counts: dict[str, int] = {}
    for raw_slot in slots:
        if not isinstance(raw_slot, Mapping):
            continue
        state = _safe_text(raw_slot.get("state"), limit=32).lower() or "unknown"
        state_counts[state] = state_counts.get(state, 0) + 1
        row = {
            "slot": _safe_text(raw_slot.get("slot"), limit=80),
            "account_name": _safe_text(raw_slot.get("account_name"), limit=120),
            "slot_role": _safe_text(raw_slot.get("slot_role"), limit=32),
            "state": state,
        }
        if state in {"ready", "healthy"}:
            safe_slots.append(row)

    configured_slots = _positive_int(provider.get("configured_slots"), default=len(slots))
    if configured_slots == 0:
        configured_slots = len(slots)
    ready_slots = len(safe_slots)
    provider_state = _safe_text(provider.get("state"), limit=32).lower()
    if provider_state not in {"ready", "healthy", "degraded", "unavailable"}:
        provider_state = "ready" if ready_slots else "unavailable"
    if ready_slots and configured_slots > ready_slots:
        provider_state = "degraded"

    return {
        "observed_at": observed_at or _utc_now(),
        "source": "worker:responses_upstream._provider_health_report(lightweight=True)",
        "provider": PROVIDER_KEY,
        "backend": _safe_text(provider.get("backend"), limit=80) or "1min",
        "state": provider_state,
        "configured_slots": configured_slots,
        "ready_slots": ready_slots,
        "slot_state_counts": dict(sorted(state_counts.items())),
        "ready_credentials": safe_slots,
        "balance": {
            "state": "unknown",
            "reason": "no_authoritative_balance_snapshot",
        },
    }


def _binding_scope() -> dict[str, object]:
    return {
        "product": "propertyquarry",
        "actions": ["property.evaluate"],
        "allowed_capabilities": [CAPABILITY_KEY],
        "allowed_tools": [TOOL_NAME],
    }


def _binding_auth_metadata() -> dict[str, object]:
    return {
        "auth_mode": "worker_managed_api_key_pool",
        "credential_boundary": "propertyquarry-worker",
        "credential_selector": "onemin_manager",
        "secret_persisted": False,
    }


def _record_projection(record: object) -> dict[str, object]:
    return {
        "binding_id": _safe_text(getattr(record, "binding_id", ""), limit=160),
        "principal_id": _safe_text(getattr(record, "principal_id", ""), limit=320),
        "provider_key": _safe_text(getattr(record, "provider_key", ""), limit=80),
        "status": _safe_text(getattr(record, "status", ""), limit=32),
        "priority": _positive_int(getattr(record, "priority", 0)),
        "probe_state": _safe_text(getattr(record, "probe_state", ""), limit=32),
        "updated_at": _safe_text(getattr(record, "updated_at", ""), limit=80),
    }


def bind_onemin_provider(
    *,
    registry: object,
    principal_id: str,
    health_snapshot: Mapping[str, object],
) -> object:
    normalized_principal = str(principal_id or "").strip()
    if not normalized_principal:
        raise ValueError("principal_id_required")
    if _positive_int(health_snapshot.get("ready_slots")) < 1:
        raise RuntimeError("onemin_no_ready_credential")

    existing = next(
        (
            row
            for row in registry.list_persisted_binding_records(
                principal_id=normalized_principal,
                limit=100,
            )
            if str(getattr(row, "provider_key", "") or "").strip().lower()
            == PROVIDER_KEY
        ),
        None,
    )
    binding_id = (
        str(getattr(existing, "binding_id", "") or "").strip()
        or _stable_binding_id(normalized_principal)
    )
    probe_state = str(health_snapshot.get("state") or "degraded").strip().lower()
    if probe_state == "healthy":
        probe_state = "ready"
    if probe_state not in {"ready", "degraded"}:
        probe_state = "degraded"
    return registry.upsert_binding_record(
        binding_id=binding_id,
        principal_id=normalized_principal,
        provider_key=PROVIDER_KEY,
        status="enabled",
        priority=10,
        probe_state=probe_state,
        probe_details_json={
            "contract_name": CONTRACT_NAME,
            "health": dict(health_snapshot),
        },
        scope_json=_binding_scope(),
        auth_metadata_json=_binding_auth_metadata(),
    )


def exercise_onemin_route(
    *,
    tool_execution: object,
    principal_id: str,
    model: str,
) -> dict[str, object]:
    from app.domain.models import ToolInvocationRequest

    invocation_id = uuid.uuid4().hex[:20]
    started = time.monotonic()
    result = tool_execution.execute_invocation(
        ToolInvocationRequest(
            session_id=f"propertyquarry-onemin-live-ops-{invocation_id}",
            step_id=f"onemin-check-{invocation_id}",
            tool_name=TOOL_NAME,
            action_kind="property.evaluate.health_check",
            payload_json={
                "model": str(model or "gpt-5.4").strip() or "gpt-5.4",
                "prompt": "Return one JSON object only: {\"status\":\"ok\"}.",
            },
            context_json={
                "principal_id": str(principal_id or "").strip(),
                "suppress_telegram_delivery": True,
            },
        )
    )
    elapsed_ms = max(0, int(round((time.monotonic() - started) * 1000)))
    output = dict(getattr(result, "output_json", {}) or {})
    receipt = dict(getattr(result, "receipt_json", {}) or {})
    return {
        "status": "succeeded",
        "observed_at": _utc_now(),
        "provider": PROVIDER_KEY,
        "backend": _safe_text(
            receipt.get("provider_backend") or output.get("provider_backend"),
            limit=80,
        )
        or "1min",
        "account_name": _safe_text(
            receipt.get("provider_account_name")
            or output.get("provider_account_name"),
            limit=120,
        ),
        "slot": _safe_text(
            receipt.get("provider_key_slot") or output.get("provider_key_slot"),
            limit=80,
        ),
        "model": _safe_text(
            receipt.get("model")
            or output.get("model")
            or getattr(result, "model_name", ""),
            limit=120,
        ),
        "tokens_in": _positive_int(getattr(result, "tokens_in", 0)),
        "tokens_out": _positive_int(getattr(result, "tokens_out", 0)),
        "latency_ms": elapsed_ms,
        "response_body_persisted": False,
        "balance_state": "unknown",
    }


def run_live_ops(
    *,
    container: object,
    health: Mapping[str, object],
    principal_id: str,
    exercise: bool,
    model: str,
) -> dict[str, object]:
    observed_at = _utc_now()
    snapshot = safe_onemin_health_snapshot(health, observed_at=observed_at)
    record = bind_onemin_provider(
        registry=container.provider_registry,
        principal_id=principal_id,
        health_snapshot=snapshot,
    )
    route = container.provider_registry.route_tool_with_context(
        TOOL_NAME,
        principal_id=principal_id,
    )
    live_call: dict[str, object] = {"status": "not_requested"}
    if exercise:
        try:
            live_call = exercise_onemin_route(
                tool_execution=container.tool_execution,
                principal_id=principal_id,
                model=model,
            )
        except Exception:
            live_call = {
                "status": "failed",
                "observed_at": _utc_now(),
                "failure_code": "onemin_live_call_failed",
                "response_body_persisted": False,
                "balance_state": "unknown",
            }
            container.provider_registry.set_persisted_binding_probe(
                binding_id=getattr(record, "binding_id", ""),
                principal_id=principal_id,
                probe_state="degraded",
                probe_details_json={
                    "contract_name": CONTRACT_NAME,
                    "health": snapshot,
                    "live_call": live_call,
                },
            )
            raise RuntimeError("onemin_live_call_failed") from None

        record = container.provider_registry.set_persisted_binding_probe(
            binding_id=getattr(record, "binding_id", ""),
            principal_id=principal_id,
            probe_state=str(snapshot.get("state") or "degraded"),
            probe_details_json={
                "contract_name": CONTRACT_NAME,
                "health": snapshot,
                "live_call": live_call,
            },
        ) or record

    payload = {
        "contract_name": CONTRACT_NAME,
        "observed_at": observed_at,
        "principal_id": str(principal_id or "").strip(),
        "binding": _record_projection(record),
        "health": snapshot,
        "route": {
            "verified": True,
            "provider_key": _safe_text(getattr(route, "provider_key", ""), limit=80),
            "capability_key": _safe_text(getattr(route, "capability_key", ""), limit=80),
            "tool_name": _safe_text(getattr(route, "tool_name", ""), limit=160),
        },
        "live_call": live_call,
        "secret_material_persisted": False,
    }
    digest_source = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload["receipt_sha256"] = hashlib.sha256(
        digest_source.encode("utf-8")
    ).hexdigest()
    return payload


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bind and prove PropertyQuarry's worker-managed 1min evaluation lane."
    )
    parser.add_argument("--principal-id", required=True)
    parser.add_argument("--exercise", action="store_true")
    parser.add_argument("--model", default="gpt-5.4")
    parser.add_argument("--receipt", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    from app.container import build_container
    from app.services import responses_upstream

    container = build_container()
    health = responses_upstream._provider_health_report(lightweight=True)
    payload = run_live_ops(
        container=container,
        health=health,
        principal_id=args.principal_id,
        exercise=bool(args.exercise),
        model=args.model,
    )
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.receipt is not None:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
