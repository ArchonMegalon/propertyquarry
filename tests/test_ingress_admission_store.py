from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from uuid import UUID, uuid4

import pytest
from app.api.ingress_admission import (
    AdmissionBackend,
    AdmissionCapacityKey,
    AdmissionDimension,
    AdmissionOperation,
    AdmissionOutcome,
    AdmissionRequest,
    AdmissionResult,
    IngressAdmissionContractError,
    IngressAdmissionStore,
    InMemoryIngressAdmissionStore,
    LeaseScope,
    PostgresIngressAdmissionStore,
    QuotaCharge,
    QuotaKind,
)

_TEST_ERASURE_KEY_ID = "0" * 64


class _Clock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _Tokens:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> UUID:
        self.value += 1
        return UUID(int=self.value)


class _GatedLock:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.proceed = threading.Event()

    def __enter__(self) -> _GatedLock:
        self.entered.set()
        if not self.proceed.wait(timeout=2):
            raise TimeoutError("test_lock_gate_timeout")
        return self

    def __exit__(
        self,
        _exc_type: object,
        _exc_value: object,
        _traceback: object,
    ) -> None:
        return None


class _CleanupCursor:
    def __init__(self) -> None:
        self._row: tuple[bool] | None = None

    def execute(self, sql: str, _params: object = None) -> None:
        if "pg_try_advisory_xact_lock" in sql:
            self._row = (True,)

    def fetchone(self) -> tuple[bool] | None:
        row = self._row
        self._row = None
        return row

    @staticmethod
    def fetchall() -> list[object]:
        return []


class _CadencedPostgresStore(PostgresIngressAdmissionStore):
    def __init__(self, clock: _Clock) -> None:
        self.cleanup_transactions = 0
        super().__init__(
            "postgresql://not-used",
            hmac_secret="x" * 32,
            erasure_key_id=_TEST_ERASURE_KEY_ID,
            cleanup_interval_seconds=1,
            cleanup_clock=clock,
            verify_schema=False,
        )

    @contextmanager
    def _transaction(self, operation: AdmissionOperation):  # type: ignore[no-untyped-def]
        assert operation is AdmissionOperation.CLEANUP
        self.cleanup_transactions += 1
        yield _CleanupCursor()


def _charge(
    *,
    kind: QuotaKind = QuotaKind.REQUEST,
    dimension: AdmissionDimension = AdmissionDimension.ACCOUNT,
    subject: str = "account-a",
    units: int = 1,
    limit: int = 10,
    window_seconds: int = 10,
) -> QuotaCharge:
    return QuotaCharge(
        kind=kind,
        dimension=dimension,
        subject=subject,
        units=units,
        limit=limit,
        window_seconds=window_seconds,
    )


def test_admission_contract_is_closed_bounded_and_subject_safe() -> None:
    charge = _charge(subject="sensitive-account-id")
    assert "sensitive-account-id" not in repr(charge)
    with pytest.raises(ValueError, match="quota_kind_invalid"):
        QuotaCharge(  # type: ignore[arg-type]
            kind="request",
            dimension=AdmissionDimension.IP,
            subject="192.0.2.1",
            units=1,
            limit=1,
            window_seconds=60,
        )
    with pytest.raises(ValueError, match="quota_charges_must_be_a_tuple"):
        AdmissionRequest(quota_charges=[charge])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="empty_admission_request"):
        AdmissionRequest(quota_charges=())
    with pytest.raises(ValueError, match="duplicate_quota_charge"):
        AdmissionRequest(quota_charges=(charge, charge))
    with pytest.raises(ValueError, match="ip_lease_scope_required"):
        AdmissionRequest(
            quota_charges=(),
            lease_scopes=(
                LeaseScope(
                    dimension=AdmissionDimension.ACCOUNT,
                    subject="account-a",
                    limit=1,
                ),
            ),
        )


def test_memory_ip_request_is_atomic_and_rolls_fixed_windows() -> None:
    clock = _Clock(100.0)
    store = InMemoryIngressAdmissionStore(clock=clock, token_factory=_Tokens())
    assert isinstance(store, IngressAdmissionStore)

    first = store.consume_ip_request(
        subject="192.0.2.8",
        units=1,
        limit=2,
        window_seconds=10,
    )
    second = store.consume_ip_request(
        subject="192.0.2.8",
        units=1,
        limit=2,
        window_seconds=10,
    )
    limited = store.consume_ip_request(
        subject="192.0.2.8",
        units=1,
        limit=2,
        window_seconds=10,
    )

    assert first.allowed and second.allowed
    assert first.backend is AdmissionBackend.MEMORY
    assert first.operation is AdmissionOperation.IP_REQUEST
    assert limited.outcome is AdmissionOutcome.QUOTA_LIMITED
    assert limited.rejected_dimension is AdmissionDimension.IP
    assert limited.rejected_kind is QuotaKind.REQUEST
    assert limited.retry_after_seconds == 10

    clock.advance(10)
    assert store.consume_ip_request(
        subject="192.0.2.8",
        units=1,
        limit=2,
        window_seconds=10,
    ).allowed


def test_memory_samples_time_only_after_entering_the_store_mutex() -> None:
    clock = _Clock(9.0)
    store = InMemoryIngressAdmissionStore(clock=clock)
    gate = _GatedLock()
    store._lock = gate  # type: ignore[assignment]

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            store.consume_ip_request,
            subject="192.0.2.9",
            units=1,
            limit=1,
            window_seconds=10,
        )
        assert gate.entered.wait(timeout=2)
        clock.value = 10.0
        gate.proceed.set()
        assert future.result(timeout=2).allowed

    assert {key[3] for key in store._buckets} == {1}


def test_memory_concurrent_quota_never_overadmits() -> None:
    store = InMemoryIngressAdmissionStore(clock=_Clock())

    def consume(_: int) -> AdmissionOutcome:
        return store.consume_ip_request(
            subject="203.0.113.19",
            units=1,
            limit=5,
            window_seconds=60,
        ).outcome

    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = tuple(pool.map(consume, range(40)))

    assert outcomes.count(AdmissionOutcome.ALLOWED) == 5
    assert outcomes.count(AdmissionOutcome.QUOTA_LIMITED) == 35


def test_memory_multi_quota_rejection_does_not_partially_consume() -> None:
    store = InMemoryIngressAdmissionStore(
        clock=_Clock(),
        token_factory=_Tokens(),
    )
    blocked = _charge(
        kind=QuotaKind.COST,
        subject="already-at-cost-limit",
        units=2,
        limit=2,
    )
    assert store.admit(AdmissionRequest(quota_charges=(blocked,))).allowed

    candidate = _charge(
        kind=QuotaKind.REQUEST,
        subject="must-not-be-partially-charged",
        limit=1,
    )
    result = store.admit(
        AdmissionRequest(
            quota_charges=(
                candidate,
                _charge(
                    kind=QuotaKind.COST,
                    subject="already-at-cost-limit",
                    units=1,
                    limit=2,
                ),
            )
        )
    )
    assert result.outcome is AdmissionOutcome.QUOTA_LIMITED
    assert result.rejected_kind is QuotaKind.COST

    assert store.admit(
        AdmissionRequest(quota_charges=(candidate,))
    ).allowed
    assert (
        store.admit(AdmissionRequest(quota_charges=(candidate,))).outcome
        is AdmissionOutcome.QUOTA_LIMITED
    )
    assert "must-not-be-partially-charged" not in repr(store._buckets)


def test_memory_lease_rejection_is_atomic_and_tokens_cannot_be_resurrected() -> None:
    clock = _Clock()
    store = InMemoryIngressAdmissionStore(
        clock=clock,
        token_factory=_Tokens(),
    )
    request = AdmissionRequest(
        quota_charges=(
            _charge(
                kind=QuotaKind.COST,
                dimension=AdmissionDimension.IP,
                subject="198.51.100.9",
                limit=2,
            ),
        ),
        lease_scopes=(
            LeaseScope(
                dimension=AdmissionDimension.IP,
                subject="198.51.100.9",
                limit=1,
            ),
            LeaseScope(
                dimension=AdmissionDimension.ACCOUNT,
                subject="account-lease",
                limit=1,
            ),
        ),
        lease_seconds=10,
    )

    first = store.admit(request)
    assert first.allowed
    assert first.lease_token == UUID(int=1).hex
    limited = store.admit(request)
    assert limited.outcome is AdmissionOutcome.LEASE_LIMITED
    assert limited.rejected_dimension is AdmissionDimension.IP
    assert limited.retry_after_seconds == 10

    assert store.release_lease(first.lease_token)
    third = store.admit(request)
    assert third.allowed
    assert store.renew_lease(third.lease_token, lease_seconds=20)
    clock.advance(19)
    assert store.renew_lease(third.lease_token, lease_seconds=5)
    clock.advance(6)
    assert not store.renew_lease(third.lease_token, lease_seconds=5)
    assert store.release_lease(third.lease_token)
    assert not store.release_lease(third.lease_token)


def test_memory_capacity_fails_closed_then_bounded_cleanup_frees_rows() -> None:
    clock = _Clock(0.0)
    store = InMemoryIngressAdmissionStore(
        clock=clock,
        token_factory=_Tokens(),
        quota_hard_limit=1,
        lease_hard_limit=1,
        cleanup_batch_size=1,
    )
    first_quota = _charge(subject="account-one", window_seconds=10)
    second_quota = _charge(subject="account-two", window_seconds=10)
    assert store.admit(AdmissionRequest(quota_charges=(first_quota,))).allowed
    exhausted = store.admit(AdmissionRequest(quota_charges=(second_quota,)))
    assert exhausted.outcome is AdmissionOutcome.CAPACITY_EXHAUSTED
    assert exhausted.capacity_key is AdmissionCapacityKey.QUOTA

    clock.advance(10)
    cleanup = store.cleanup_expired(batch_size=1)
    assert cleanup.quota_deleted == 1
    assert store.admit(AdmissionRequest(quota_charges=(second_quota,))).allowed

    first_lease = store.admit(
        AdmissionRequest(
            quota_charges=(),
            lease_scopes=(
                LeaseScope(
                    dimension=AdmissionDimension.IP,
                    subject="203.0.113.1",
                    limit=1,
                ),
            ),
        )
    )
    assert first_lease.allowed
    second_lease_request = AdmissionRequest(
        quota_charges=(),
        lease_scopes=(
            LeaseScope(
                dimension=AdmissionDimension.IP,
                subject="203.0.113.2",
                limit=1,
            ),
        ),
    )
    exhausted = store.admit(second_lease_request)
    assert exhausted.outcome is AdmissionOutcome.CAPACITY_EXHAUSTED
    assert exhausted.capacity_key is AdmissionCapacityKey.LEASE
    assert store.release_lease(first_lease.lease_token)
    assert store.admit(second_lease_request).allowed

    snapshot = store.capacity_snapshot()
    assert snapshot.contract_valid
    assert [(row.capacity_key, row.row_count, row.hard_limit) for row in snapshot.rows] == [
        (AdmissionCapacityKey.QUOTA, 1, 1),
        (AdmissionCapacityKey.LEASE, 1, 1),
    ]


def test_memory_cleanup_batch_is_bounded_per_relation() -> None:
    clock = _Clock(0.0)
    store = InMemoryIngressAdmissionStore(
        clock=clock,
        token_factory=_Tokens(),
        quota_hard_limit=3,
    )
    for index in range(3):
        assert store.admit(
            AdmissionRequest(
                quota_charges=(
                    _charge(
                        subject=f"account-{index}",
                        window_seconds=1,
                    ),
                )
            )
        ).allowed
    clock.advance(1)
    result = store.cleanup_expired(batch_size=1)
    assert result.quota_deleted == 1
    assert store.capacity_snapshot().rows[0].row_count == 2


def test_postgres_store_maps_missing_schema_to_closed_contract_error() -> None:
    class _UndefinedTable(Exception):
        sqlstate = "42P01"

    def missing_schema_connect(
        _database_url: str,
        *,
        autocommit: bool,
    ) -> object:
        assert autocommit
        raise _UndefinedTable()

    with pytest.raises(
        IngressAdmissionContractError,
        match="ingress_admission_schema_not_ready",
    ) as raised:
        PostgresIngressAdmissionStore(
            "postgresql://not-used",
            hmac_secret="x" * 32,
            erasure_key_id=_TEST_ERASURE_KEY_ID,
            connect=missing_schema_connect,
        )
    assert raised.value.backend is AdmissionBackend.POSTGRES
    assert raised.value.operation is AdmissionOperation.SNAPSHOT
    assert raised.value.outcome is AdmissionOutcome.BACKEND_UNAVAILABLE


def test_postgres_cleanup_is_cadenced_instead_of_per_admission() -> None:
    clock = _Clock(100.0)
    store = _CadencedPostgresStore(clock)

    store._cleanup_if_due()
    clock.advance(0.99)
    store._cleanup_if_due()
    assert store.cleanup_transactions == 0

    clock.advance(0.01)
    store._cleanup_if_due()
    store._cleanup_if_due()
    assert store.cleanup_transactions == 1

    clock.advance(1)
    store._cleanup_if_due()
    assert store.cleanup_transactions == 2


def _postgres_url() -> str:
    database_url = str(
        os.environ.get("EA_TEST_PROPERTY_DATABASE_URL")
        or os.environ.get("EA_TEST_DATABASE_URL")
        or ""
    ).strip()
    if not database_url:
        pytest.skip(
            "EA_TEST_PROPERTY_DATABASE_URL or EA_TEST_DATABASE_URL is not set"
        )
    return database_url


def test_postgres_admission_is_atomic_and_persists_only_hmac_subjects() -> None:
    psycopg = pytest.importorskip("psycopg")
    from app.product.property_search_schema import migrate_property_search_schema
    from app.product.property_search_storage import (
        _property_search_erasure_key_id,
    )
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    database_url = _postgres_url()
    namespace = "test_ingress_admission_" + uuid4().hex
    with psycopg.connect(database_url, autocommit=True, connect_timeout=5) as admin:
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(namespace))
            )
    scoped_url = make_conninfo(
        database_url,
        options=f"-csearch_path={namespace},public",
    )
    raw_ip = "198.51.100.77"
    raw_account = "postgres-account-sensitive"
    try:
        migrate_property_search_schema(
            scoped_url,
            applied_by="ingress-admission-store-test",
        )
        store = PostgresIngressAdmissionStore(
            scoped_url,
            hmac_secret="integration-admission-secret-" + ("x" * 32),
            erasure_key_id=_property_search_erasure_key_id(),
            statement_timeout_ms=15_000,
            lock_timeout_ms=10_000,
            idle_transaction_timeout_ms=15_000,
            max_connections=16,
            acquire_timeout_seconds=5,
        )
        mismatched_key_id = (
            "1" * 64
            if _property_search_erasure_key_id() == "0" * 64
            else "0" * 64
        )
        with pytest.raises(
            IngressAdmissionContractError,
            match="ingress_admission_erasure_key_id_mismatch",
        ):
            PostgresIngressAdmissionStore(
                scoped_url,
                hmac_secret="different-admission-secret-" + ("y" * 32),
                erasure_key_id=mismatched_key_id,
            )

        assert store.consume_ip_request(
            subject=raw_ip,
            units=1,
            limit=1,
            window_seconds=60,
        ).allowed
        assert (
            store.consume_ip_request(
                subject=raw_ip,
                units=1,
                limit=1,
                window_seconds=60,
            ).outcome
            is AdmissionOutcome.QUOTA_LIMITED
        )

        blocker = _charge(
            kind=QuotaKind.COST,
            subject="postgres-blocker",
            limit=1,
        )
        assert store.admit(AdmissionRequest(quota_charges=(blocker,))).allowed
        candidate = _charge(
            kind=QuotaKind.REQUEST,
            subject=raw_account,
            limit=1,
        )
        rejected = store.admit(
            AdmissionRequest(
                quota_charges=(
                    candidate,
                    _charge(
                        kind=QuotaKind.COST,
                        subject="postgres-blocker",
                        limit=1,
                    ),
                )
            )
        )
        assert rejected.outcome is AdmissionOutcome.QUOTA_LIMITED
        assert store.admit(
            AdmissionRequest(quota_charges=(candidate,))
        ).allowed

        leased_request = AdmissionRequest(
            quota_charges=(
                _charge(
                    kind=QuotaKind.COST,
                    dimension=AdmissionDimension.IP,
                    subject=raw_ip,
                    limit=2,
                ),
            ),
            lease_scopes=(
                LeaseScope(
                    dimension=AdmissionDimension.IP,
                    subject=raw_ip,
                    limit=1,
                ),
                LeaseScope(
                    dimension=AdmissionDimension.ACCOUNT,
                    subject=raw_account,
                    limit=1,
                ),
            ),
            lease_seconds=30,
        )
        first_lease = store.admit(leased_request)
        assert first_lease.allowed
        assert store.admit(leased_request).outcome is AdmissionOutcome.LEASE_LIMITED
        assert store.release_lease(first_lease.lease_token)
        live_lease = store.admit(leased_request)
        assert live_lease.allowed
        assert store.renew_lease(live_lease.lease_token, lease_seconds=30)

        quota_worker_count = 12
        quota_limit = 5
        quota_gate = threading.Barrier(quota_worker_count)

        def charge_same_ip(_: int) -> AdmissionResult:
            quota_gate.wait(timeout=10)
            return store.consume_ip_request(
                subject="postgres-concurrent-quota-ip",
                units=1,
                limit=quota_limit,
                window_seconds=2_678_400,
            )

        with ThreadPoolExecutor(max_workers=quota_worker_count) as pool:
            futures = tuple(
                pool.submit(charge_same_ip, index)
                for index in range(quota_worker_count)
            )
            quota_results = tuple(
                future.result(timeout=20) for future in futures
            )
        assert sum(result.allowed for result in quota_results) == quota_limit
        assert (
            sum(
                result.outcome is AdmissionOutcome.QUOTA_LIMITED
                for result in quota_results
            )
            == quota_worker_count - quota_limit
        )

        lease_worker_count = 12
        lease_limit = 3
        lease_gate = threading.Barrier(lease_worker_count)
        concurrent_lease_request = AdmissionRequest(
            quota_charges=(),
            lease_scopes=(
                LeaseScope(
                    dimension=AdmissionDimension.IP,
                    subject="postgres-concurrent-lease-ip",
                    limit=lease_limit,
                ),
            ),
            lease_seconds=30,
        )

        def acquire_same_subject_lease(_: int) -> AdmissionResult:
            lease_gate.wait(timeout=10)
            return store.admit(concurrent_lease_request)

        with ThreadPoolExecutor(max_workers=lease_worker_count) as pool:
            futures = tuple(
                pool.submit(acquire_same_subject_lease, index)
                for index in range(lease_worker_count)
            )
            lease_results = tuple(
                future.result(timeout=20) for future in futures
            )
        admitted_lease_tokens = tuple(
            result.lease_token for result in lease_results if result.allowed
        )
        assert len(admitted_lease_tokens) == lease_limit
        assert (
            sum(
                result.outcome is AdmissionOutcome.LEASE_LIMITED
                for result in lease_results
            )
            == lease_worker_count - lease_limit
        )

        snapshot = store.capacity_snapshot()
        assert snapshot.contract_valid
        with psycopg.connect(scoped_url, connect_timeout=5) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT subject_digest
                    FROM propertyquarry_ingress_quota_buckets
                    """
                )
                quota_digests = [str(row[0]) for row in cursor.fetchall()]
                cursor.execute(
                    """
                    SELECT ip_subject_digest, account_subject_digest
                    FROM propertyquarry_ingress_leases
                    """
                )
                lease_digests = [
                    str(value)
                    for row in cursor.fetchall()
                    for value in row
                    if value is not None
                ]
        assert quota_digests
        assert lease_digests
        assert all(value.startswith("hmac-sha256:") for value in quota_digests)
        assert all(value.startswith("hmac-sha256:") for value in lease_digests)
        assert raw_ip not in repr((quota_digests, lease_digests))
        assert raw_account not in repr((quota_digests, lease_digests))
        assert store.release_lease(live_lease.lease_token)
        assert all(
            store.release_lease(token) for token in admitted_lease_tokens
        )

        with psycopg.connect(
            scoped_url,
            autocommit=True,
            connect_timeout=5,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    DROP TRIGGER propertyquarry_ingress_quota_capacity_guard
                    ON propertyquarry_ingress_quota_buckets
                    """
                )
        with pytest.raises(
            IngressAdmissionContractError,
            match=(
                "required_trigger_missing:"
                "propertyquarry_ingress_quota_capacity_guard"
            ),
        ):
            PostgresIngressAdmissionStore(
                scoped_url,
                hmac_secret="integration-admission-secret-" + ("x" * 32),
                erasure_key_id=_property_search_erasure_key_id(),
            )
    finally:
        with psycopg.connect(
            database_url,
            autocommit=True,
            connect_timeout=5,
        ) as admin:
            with admin.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        sql.Identifier(namespace)
                    )
                )
