from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse

from app.api.dependencies import get_container, require_runtime_metrics_auth
from app.api.ingress_admission import (
    AdmissionBackend,
    AdmissionOperation,
    AdmissionOutcome,
    IngressAdmissionError,
    PostgresIngressAdmissionStore,
)
from app.container import AppContainer
from app.observability import get_runtime_metrics

router = APIRouter(tags=["system"])


@router.get(
    "/internal/metrics",
    response_class=PlainTextResponse,
    include_in_schema=False,
    dependencies=[Depends(require_runtime_metrics_auth)],
)
async def runtime_metrics(
    request: Request,
    container: AppContainer = Depends(get_container),
) -> PlainTextResponse:
    ready, _reason = await asyncio.to_thread(container.readiness.check)
    effective_ready = bool(ready)
    registry = get_runtime_metrics(request.app)
    admission_store = getattr(
        request.app.state,
        "ingress_admission_store",
        None,
    )
    if admission_store is not None:
        inferred_backend = (
            AdmissionBackend.POSTGRES
            if isinstance(admission_store, PostgresIngressAdmissionStore)
            else AdmissionBackend.MEMORY
        )
        try:
            snapshot = await asyncio.to_thread(admission_store.capacity_snapshot)
        except IngressAdmissionError as exc:
            effective_ready = False
            registry.record_ingress_admission_operation(
                backend=exc.backend.value,
                operation=exc.operation.value,
                outcome=exc.outcome.value,
            )
            registry.record_ingress_admission_capacity(
                backend=exc.backend.value,
                contract_valid=False,
                rows={},
            )
        except Exception:
            effective_ready = False
            registry.record_ingress_admission_operation(
                backend=inferred_backend.value,
                operation=AdmissionOperation.SNAPSHOT.value,
                outcome=AdmissionOutcome.BACKEND_UNAVAILABLE.value,
            )
            registry.record_ingress_admission_capacity(
                backend=inferred_backend.value,
                contract_valid=False,
                rows={},
            )
        else:
            effective_ready = effective_ready and snapshot.contract_valid
            registry.record_ingress_admission_operation(
                backend=snapshot.backend.value,
                operation=AdmissionOperation.SNAPSHOT.value,
                outcome=(
                    AdmissionOutcome.ALLOWED.value
                    if snapshot.contract_valid
                    else AdmissionOutcome.BACKEND_UNAVAILABLE.value
                ),
            )
            registry.record_ingress_admission_capacity(
                backend=snapshot.backend.value,
                contract_valid=snapshot.contract_valid,
                rows={
                    row.capacity_key.value: (row.row_count, row.hard_limit)
                    for row in snapshot.rows
                },
            )
    payload = registry.render_prometheus(readiness_ready=effective_ready)
    return PlainTextResponse(
        payload,
        media_type="text/plain; version=0.0.4",
        headers={"Cache-Control": "no-store"},
    )
