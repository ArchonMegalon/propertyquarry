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
    ready, _reason = container.readiness.check()
    admission_backend = getattr(request.app.state, "admission_backend", None)
    backend_name = str(
        getattr(admission_backend, "backend_name", "unknown") or "unknown"
    )
    capacity_rows: tuple[tuple[str, int, int], ...] = ()
    capacity_valid = False
    capacity_snapshot = getattr(admission_backend, "capacity_snapshot", None)
    if callable(capacity_snapshot):
        try:
            capacity_rows = tuple(await asyncio.to_thread(capacity_snapshot))
            capacity_valid = backend_name == "postgres" and len(capacity_rows) == 2
        except Exception:
            capacity_rows = ()
    payload = get_runtime_metrics(request.app).render_prometheus(
        readiness_ready=bool(ready),
        admission_backend=backend_name,
        admission_capacity_rows=capacity_rows,
        admission_capacity_valid=capacity_valid,
    )
    return PlainTextResponse(
        payload,
        media_type="text/plain; version=0.0.4",
        headers={"Cache-Control": "no-store"},
    )
