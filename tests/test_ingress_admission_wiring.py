from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from app.api.dependencies import RequestContext
from app.api.ingress import (
    IngressAbuseMiddleware,
    IngressPolicy,
    parse_trusted_proxy_cidrs,
)
from app.api.ingress_admission import (
    AdmissionBackend,
    AdmissionCapacityKey,
    AdmissionCapacitySnapshot,
    AdmissionOperation,
    AdmissionOutcome,
    AdmissionRequest,
    AdmissionResult,
    CapacityRow,
    CleanupResult,
    IngressAdmissionUnavailable,
)
from app.observability import RuntimeMetrics
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _policy(**overrides: object) -> IngressPolicy:
    policy = IngressPolicy(
        quotas_enabled=True,
        max_body_bytes=8_388_608,
        max_upload_body_bytes=40_000_000,
        window_seconds=60,
        ip_request_limit=600,
        account_request_limit=240,
        ip_cost_limit=1_000,
        account_cost_limit=300,
        high_cost_ip_concurrency=8,
        high_cost_account_concurrency=2,
        active_search_limit=1,
        lease_seconds=3,
        trusted_proxy_cidrs=parse_trusted_proxy_cidrs(
            "127.0.0.0/8,::1/128"
        ),
    )
    return replace(policy, **overrides)


class _UnavailableStore:
    def consume_ip_request(
        self,
        *,
        subject: str,
        units: int,
        limit: int,
        window_seconds: int,
    ) -> AdmissionResult:
        del subject, units, limit, window_seconds
        raise IngressAdmissionUnavailable(
            "test_backend_unavailable",
            backend=AdmissionBackend.POSTGRES,
            operation=AdmissionOperation.IP_REQUEST,
        )

    def admit(self, request: AdmissionRequest) -> AdmissionResult:
        raise AssertionError(request)

    def renew_lease(self, lease_token: str, *, lease_seconds: int) -> bool:
        raise AssertionError((lease_token, lease_seconds))

    def release_lease(self, lease_token: str) -> bool:
        raise AssertionError(lease_token)

    def cleanup_expired(self, *, batch_size: int) -> CleanupResult:
        raise AssertionError(batch_size)

    def capacity_snapshot(self) -> AdmissionCapacitySnapshot:
        raise AssertionError


class _LosingLeaseStore:
    token = "0123456789abcdef0123456789abcdef"

    def __init__(self) -> None:
        self.released = False
        self.admitted: AdmissionRequest | None = None

    def consume_ip_request(
        self,
        *,
        subject: str,
        units: int,
        limit: int,
        window_seconds: int,
    ) -> AdmissionResult:
        assert subject
        assert (units, limit, window_seconds) == (1, 600, 60)
        return AdmissionResult(
            backend=AdmissionBackend.POSTGRES,
            operation=AdmissionOperation.IP_REQUEST,
            outcome=AdmissionOutcome.ALLOWED,
            allowed=True,
        )

    def admit(self, request: AdmissionRequest) -> AdmissionResult:
        self.admitted = request
        return AdmissionResult(
            backend=AdmissionBackend.POSTGRES,
            operation=AdmissionOperation.ADMIT,
            outcome=AdmissionOutcome.ALLOWED,
            allowed=True,
            lease_token=self.token,
        )

    def renew_lease(self, lease_token: str, *, lease_seconds: int) -> bool:
        assert lease_token == self.token
        assert lease_seconds == 3
        return False

    def release_lease(self, lease_token: str) -> bool:
        assert lease_token == self.token
        self.released = True
        return True

    def cleanup_expired(self, *, batch_size: int) -> CleanupResult:
        return CleanupResult(
            backend=AdmissionBackend.POSTGRES,
            quota_deleted=0,
            lease_deleted=0,
        )

    def capacity_snapshot(self) -> AdmissionCapacitySnapshot:
        return _postgres_capacity_snapshot()


class _RenewingLeaseStore(_LosingLeaseStore):
    def __init__(self) -> None:
        super().__init__()
        self.renewed = threading.Event()
        self.renewal_count = 0

    def renew_lease(self, lease_token: str, *, lease_seconds: int) -> bool:
        assert lease_token == self.token
        assert lease_seconds == 3
        self.renewal_count += 1
        self.renewed.set()
        return True


def _postgres_capacity_snapshot() -> AdmissionCapacitySnapshot:
    return AdmissionCapacitySnapshot(
        backend=AdmissionBackend.POSTGRES,
        contract_valid=True,
        rows=(
            CapacityRow(
                capacity_key=AdmissionCapacityKey.QUOTA,
                row_count=0,
                hard_limit=1_000_000,
                contract_version=1,
            ),
            CapacityRow(
                capacity_key=AdmissionCapacityKey.LEASE,
                row_count=0,
                hard_limit=100_000,
                contract_version=1,
            ),
        ),
    )


def test_production_cannot_disable_authoritative_ingress_quotas() -> None:
    with pytest.raises(
        RuntimeError,
        match="propertyquarry_prod_requires_ingress_quotas",
    ):
        IngressPolicy.from_environ(
            runtime_mode="prod",
            environ={"PROPERTYQUARRY_INGRESS_QUOTAS_ENABLED": "false"},
        )


@pytest.mark.parametrize("runtime_mode", ("prod", "production"))
def test_production_policy_v1_cannot_drift_between_replicas(
    runtime_mode: str,
) -> None:
    with pytest.raises(
        RuntimeError,
        match=(
            "propertyquarry_prod_ingress_policy_v1_mismatch:"
            "window_seconds"
        ),
    ):
        IngressPolicy.from_environ(
            runtime_mode=runtime_mode,
            environ={
                "PROPERTYQUARRY_INGRESS_QUOTAS_ENABLED": "true",
                "PROPERTYQUARRY_INGRESS_QUOTA_WINDOW_SECONDS": "61",
            },
        )


@pytest.mark.parametrize("runtime_mode", ("prod", "production"))
def test_production_proxy_policy_v1_cannot_drift_between_replicas(
    runtime_mode: str,
) -> None:
    with pytest.raises(
        RuntimeError,
        match=(
            "propertyquarry_prod_ingress_policy_v1_mismatch:"
            "trusted_proxy_cidrs"
        ),
    ):
        IngressPolicy.from_environ(
            runtime_mode=runtime_mode,
            environ={
                "PROPERTYQUARRY_INGRESS_QUOTAS_ENABLED": "true",
                "PROPERTYQUARRY_TRUSTED_PROXY_CIDRS": "10.0.0.0/8",
            },
        )


def test_backend_unavailability_fails_closed_and_is_counted() -> None:
    app = FastAPI()
    app.state.runtime_metrics = RuntimeMetrics()
    app.add_middleware(
        IngressAbuseMiddleware,
        policy=_policy(),
        admission_store=_UnavailableStore(),
    )

    @app.get("/limited")
    async def limited() -> dict[str, bool]:
        return {"ok": True}

    response = TestClient(app).get("/limited")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json()["error"]["code"] == "ingress_admission_unavailable"
    metrics = app.state.runtime_metrics.render_prometheus(readiness_ready=False)
    assert (
        "propertyquarry_ingress_admission_operations_total"
        '{backend="postgres",operation="ip_request",'
        'outcome="backend_unavailable"} 1'
    ) in metrics


def test_lost_distributed_lease_cancels_downstream_and_releases() -> None:
    store = _LosingLeaseStore()
    cancelled = asyncio.Event()
    app = FastAPI()
    app.state.runtime_metrics = RuntimeMetrics()
    app.add_middleware(
        IngressAbuseMiddleware,
        policy=_policy(),
        admission_store=store,
        context_resolver=lambda request: RequestContext(
            principal_id="lease-test-account",
            authenticated=True,
            auth_source="test",
        ),
    )

    @app.post("/app/api/property/decision-copilot")
    async def expensive() -> dict[str, bool]:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.set()
            raise RuntimeError("downstream_cancel_cleanup_failed") from None
        return {"ok": True}

    response = TestClient(app).post(
        "/app/api/property/decision-copilot",
        json={},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ingress_admission_lease_lost"
    assert cancelled.is_set()
    assert store.released is True
    assert store.admitted is not None
    assert {scope.dimension.value for scope in store.admitted.lease_scopes} == {
        "ip",
        "account",
    }


def test_distributed_lease_renewal_covers_slow_active_search_preflight() -> None:
    store = _RenewingLeaseStore()

    def slow_active_search_check(
        _request,
        _context,
        _limit,
    ) -> int:  # type: ignore[no-untyped-def]
        assert store.renewed.wait(timeout=2.5)
        return 0

    app = FastAPI()
    app.state.runtime_metrics = RuntimeMetrics()
    app.add_middleware(
        IngressAbuseMiddleware,
        policy=_policy(),
        admission_store=store,
        context_resolver=lambda request: RequestContext(
            principal_id="lease-preflight-account",
            authenticated=True,
            auth_source="test",
        ),
        active_search_counter=slow_active_search_check,
    )

    @app.post("/app/api/property/search-runs")
    async def start_search() -> dict[str, bool]:
        return {"ok": True}

    response = TestClient(app).post(
        "/app/api/property/search-runs",
        json={},
    )

    assert response.status_code == 200
    assert store.renewal_count >= 1
    assert store.released is True


def test_app_store_builder_uses_postgres_without_memory_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import app as app_module

    calls: list[tuple[str, object]] = []

    class _PostgresStore:
        def __init__(
            self,
            database_url: str,
            *,
            hmac_secret: bytes,
            erasure_key_id: str,
            verify_schema: bool,
        ) -> None:
            calls.append(
                (
                    "postgres",
                    (
                        database_url,
                        len(hmac_secret),
                        len(erasure_key_id),
                        verify_schema,
                    ),
                )
            )

        def capacity_snapshot(self) -> AdmissionCapacitySnapshot:
            return _postgres_capacity_snapshot()

    monkeypatch.setattr(
        app_module,
        "PostgresIngressAdmissionStore",
        _PostgresStore,
    )
    monkeypatch.setattr(
        app_module,
        "InMemoryIngressAdmissionStore",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError(("memory_fallback", kwargs))
        ),
    )
    monkeypatch.setenv("EA_RUNTIME_MODE", "prod")
    monkeypatch.setenv(
        "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET",
        "z" * 64,
    )
    settings = SimpleNamespace(
        auth=SimpleNamespace(signing_secret="x" * 64),
        database_url="postgresql://db/property",
        storage_backend="postgres",
    )
    container = SimpleNamespace(
        runtime_profile=SimpleNamespace(storage_backend="postgres"),
        settings=settings,
    )
    metrics = RuntimeMetrics()

    store = app_module._build_ingress_admission_store(
        settings=settings,
        container=container,
        policy=_policy(),
        metrics=metrics,
    )

    assert isinstance(store, _PostgresStore)
    assert calls == [
        ("postgres", ("postgresql://db/property", 32, 64, True))
    ]
    rendered = metrics.render_prometheus(readiness_ready=True)
    assert (
        'propertyquarry_admission_capacity_contract_valid{backend="postgres"} 1'
        in rendered
    )


def test_postgres_store_builder_rejects_weak_admission_key_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import app as app_module

    monkeypatch.setenv("EA_RUNTIME_MODE", "prod")
    monkeypatch.setenv(
        "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET",
        "too-short",
    )
    settings = SimpleNamespace(
        database_url="postgresql://db/property",
        storage_backend="postgres",
    )
    container = SimpleNamespace(
        runtime_profile=SimpleNamespace(storage_backend="postgres"),
        settings=settings,
    )

    with pytest.raises(
        RuntimeError,
        match="property_search_erasure_secret_too_short",
    ):
        app_module._build_ingress_admission_store(
            settings=settings,
            container=container,
            policy=_policy(),
            metrics=RuntimeMetrics(),
        )


def test_disabled_quotas_construct_no_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import app as app_module

    monkeypatch.setattr(
        app_module,
        "PostgresIngressAdmissionStore",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError(("postgres_io", args, kwargs))
        ),
    )
    monkeypatch.setattr(
        app_module,
        "InMemoryIngressAdmissionStore",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError(("memory_store", args, kwargs))
        ),
    )

    assert (
        app_module._build_ingress_admission_store(
            settings=SimpleNamespace(),
            container=SimpleNamespace(),
            policy=_policy(quotas_enabled=False),
            metrics=RuntimeMetrics(),
        )
        is None
    )
