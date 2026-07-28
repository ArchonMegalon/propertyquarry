from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.admission_control import (
    ADMISSION_CAPACITY_FUNCTION_SOURCES,
    ADMISSION_CAPACITY_OWNER_ROLE_DEFAULT,
    ADMISSION_CAPACITY_TRIGGER_CONTRACTS,
    ADMISSION_LEASE_ROW_LIMIT,
    ADMISSION_QUOTA_ROW_LIMIT,
    AdmissionBackendUnavailable,
    ConcurrencyDimension,
    MemoryAdmissionBackend,
    PostgresAdmissionBackend,
    QuotaCharge,
    build_admission_backend,
    probe_admission_cursor,
    resolve_api_admission_database_url,
    resolve_api_ingress_database_url,
)


def _charge(key: str, *, limit: int = 2, units: int = 1) -> QuotaCharge:
    return QuotaCharge(
        key=key,
        units=units,
        limit=limit,
        window_seconds=60,
        dimension=key,
    )


def test_memory_quota_batch_is_atomic_and_rolls_at_the_window_boundary() -> None:
    now = [1.0]
    backend = MemoryAdmissionBackend(clock=lambda: now[0])

    assert backend.consume(_charge("full", limit=1)).allowed is True
    denied = backend.consume_many((_charge("fresh", limit=1), _charge("full", limit=1)))

    assert denied.allowed is False
    assert denied.dimension == "full"
    assert backend.consume(_charge("fresh", limit=1)).allowed is True
    assert backend.consume(_charge("fresh", limit=1)).allowed is False

    now[0] = 61.0
    assert backend.consume(_charge("fresh", limit=1)).allowed is True


def test_live_quota_cannot_be_reset_or_loosened_by_replica_config_drift() -> None:
    backend = MemoryAdmissionBackend(clock=lambda: 1.0)

    assert backend.consume(_charge("shared", limit=2)).allowed is True
    assert backend.consume(
        QuotaCharge("shared", units=1, limit=100, window_seconds=5)
    ).allowed is True
    denied = backend.consume(
        QuotaCharge("shared", units=1, limit=100, window_seconds=5)
    )

    assert denied.allowed is False
    assert denied.retry_after_seconds == 59

    strict_backend = MemoryAdmissionBackend(clock=lambda: 1.0)
    assert strict_backend.consume(_charge("lowered", limit=2)).allowed is True
    assert strict_backend.consume(_charge("lowered", limit=1)).allowed is False
    assert strict_backend.consume(_charge("lowered", limit=100)).allowed is False


def test_memory_concurrency_is_atomic_across_dimensions_and_expiry_is_bounded() -> None:
    now = [10.0]
    backend = MemoryAdmissionBackend(clock=lambda: now[0])
    dimensions = (
        ConcurrencyDimension("service:ip:one", 2, "ip"),
        ConcurrencyDimension("service:account:one", 1, "account"),
    )

    first = backend.acquire(dimensions, lease_seconds=30)
    second = backend.acquire(dimensions, lease_seconds=30)

    assert first.allowed is True
    assert first.lease_id
    assert second.allowed is False
    assert second.dimension == "account"

    backend.release(first.lease_id)
    replacement = backend.acquire(dimensions, lease_seconds=30)
    assert replacement.allowed is True

    now[0] = 41.0
    assert backend.acquire(dimensions, lease_seconds=30).allowed is True


def test_memory_concurrency_preserves_live_strict_policy_and_renews_atomically() -> None:
    now = [10.0]
    backend = MemoryAdmissionBackend(clock=lambda: now[0])
    strict = backend.acquire(
        (ConcurrencyDimension("service:shared", 1, "global"),),
        lease_seconds=5,
    )

    assert strict.allowed is True
    assert backend.acquire(
        (ConcurrencyDimension("service:shared", 100, "global"),),
        lease_seconds=5,
    ).allowed is False
    assert backend.renew(strict.lease_id, lease_seconds=30) is True

    now[0] = 16.0
    assert backend.acquire(
        (ConcurrencyDimension("service:shared", 100, "global"),),
        lease_seconds=5,
    ).allowed is False
    backend.release(strict.lease_id)
    assert backend.renew(strict.lease_id, lease_seconds=30) is False
    assert backend.acquire(
        (ConcurrencyDimension("service:shared", 100, "global"),),
        lease_seconds=5,
    ).allowed is True

    drift_backend = MemoryAdmissionBackend(clock=lambda: now[0])
    lax = drift_backend.acquire(
        (ConcurrencyDimension("service:rolling", 10, "global"),),
        lease_seconds=30,
    )
    assert lax.allowed is True
    assert drift_backend.acquire(
        (ConcurrencyDimension("service:rolling", 1, "global"),),
        lease_seconds=30,
    ).allowed is False
    assert drift_backend.acquire(
        (ConcurrencyDimension("service:rolling", 10, "global"),),
        lease_seconds=30,
    ).allowed is False


def test_memory_capacity_fails_closed_without_evicting_live_quota_state() -> None:
    backend = MemoryAdmissionBackend(clock=lambda: 1.0, max_quota_keys=1)

    assert backend.consume(_charge("caller-a", limit=2)).allowed is True
    assert backend.consume(_charge("caller-b", limit=2)).allowed is False
    assert backend.consume(_charge("caller-a", limit=2)).allowed is True
    assert backend.consume(_charge("caller-a", limit=2)).allowed is False


def test_backend_selection_forbids_process_local_production_admission() -> None:
    backend = build_admission_backend(
        runtime_mode="test",
        database_url="",
        environ={},
    )
    assert isinstance(backend, MemoryAdmissionBackend)

    with pytest.raises(RuntimeError, match="memory_backend_forbidden"):
        build_admission_backend(
            runtime_mode="prod",
            database_url="postgresql://test/property",
            environ={"PROPERTYQUARRY_ADMISSION_BACKEND": "memory"},
        )
    with pytest.raises(RuntimeError, match="database_url_required"):
        build_admission_backend(runtime_mode="prod", database_url="", environ={})

    production = build_admission_backend(
        runtime_mode="prod",
        database_url="postgresql://test/property",
        environ={
            "PROPERTYQUARRY_ADMISSION_POOL_SIZE": "2",
            "PROPERTYQUARRY_ADMISSION_LOCK_TIMEOUT_MS": "750",
            "PROPERTYQUARRY_ADMISSION_STATEMENT_TIMEOUT_MS": "2500",
            "PROPERTYQUARRY_ADMISSION_IDLE_TRANSACTION_TIMEOUT_MS": "7000",
        },
    )
    assert isinstance(production, PostgresAdmissionBackend)
    assert production._require_least_privilege is True
    assert production._lock_timeout_ms == 750
    assert production._statement_timeout_ms == 2_500
    assert production._idle_transaction_timeout_ms == 7_000

    development = build_admission_backend(
        runtime_mode="test",
        database_url="postgresql://test/property",
        environ={"PROPERTYQUARRY_ADMISSION_BACKEND": "postgres"},
    )
    assert isinstance(development, PostgresAdmissionBackend)
    assert development._require_least_privilege is False

    with pytest.raises(RuntimeError, match="timeout_order_invalid"):
        build_admission_backend(
            runtime_mode="prod",
            database_url="postgresql://test/property",
            environ={
                "PROPERTYQUARRY_ADMISSION_LOCK_TIMEOUT_MS": "5000",
                "PROPERTYQUARRY_ADMISSION_STATEMENT_TIMEOUT_MS": "1000",
            },
        )


def test_api_admission_database_resolution_is_dedicated_in_production() -> None:
    primary = "postgresql://api-runtime/property"
    admission = "postgresql://api-admission/property"

    with pytest.raises(
        RuntimeError,
        match="propertyquarry_api_admission_database_url_required",
    ):
        resolve_api_admission_database_url(
            runtime_mode="prod",
            primary_database_url=primary,
            environ={},
        )
    with pytest.raises(
        RuntimeError,
        match="propertyquarry_api_admission_database_url_must_be_dedicated",
    ):
        resolve_api_admission_database_url(
            runtime_mode="prod",
            primary_database_url=primary,
            environ={
                "PROPERTYQUARRY_API_ADMISSION_DATABASE_URL": primary,
            },
        )

    assert resolve_api_admission_database_url(
        runtime_mode="prod",
        primary_database_url=primary,
        environ={
            "PROPERTYQUARRY_API_ADMISSION_DATABASE_URL": admission,
        },
    ) == admission


def test_api_admission_primary_database_fallback_is_explicit_and_non_prod_only(
) -> None:
    primary = "postgresql://development/property"

    assert resolve_api_admission_database_url(
        runtime_mode="test",
        primary_database_url=primary,
        environ={},
    ) == ""
    with pytest.raises(
        RuntimeError,
        match="propertyquarry_api_admission_database_url_required",
    ):
        resolve_api_admission_database_url(
            runtime_mode="test",
            primary_database_url=primary,
            environ={"PROPERTYQUARRY_ADMISSION_BACKEND": "postgres"},
        )
    assert resolve_api_admission_database_url(
        runtime_mode="dev",
        primary_database_url=primary,
        environ={
            "PROPERTYQUARRY_ADMISSION_BACKEND": "postgres",
            "PROPERTYQUARRY_DEV_ALLOW_PRIMARY_ADMISSION_DATABASE_URL": "true",
        },
    ) == primary
    with pytest.raises(
        RuntimeError,
        match="propertyquarry_dev_allow_primary_admission_database_url_invalid",
    ):
        resolve_api_admission_database_url(
            runtime_mode="test",
            primary_database_url=primary,
            environ={
                "PROPERTYQUARRY_ADMISSION_BACKEND": "postgres",
                "PROPERTYQUARRY_DEV_ALLOW_PRIMARY_ADMISSION_DATABASE_URL": "sometimes",
            },
        )


def test_api_ingress_database_resolution_is_role_separated_in_production() -> (
    None
):
    primary = "postgresql://api-runtime/property"
    internal = "postgresql://api-admission/propertyquarry_admission"
    ingress = "postgresql://api-ingress/propertyquarry_admission"

    assert (
        resolve_api_ingress_database_url(
            runtime_mode="prod",
            primary_database_url=primary,
            environ={
                "PROPERTYQUARRY_API_ADMISSION_DATABASE_URL": internal,
                "PROPERTYQUARRY_API_INGRESS_DATABASE_URL": ingress,
            },
        )
        == ingress
    )
    with pytest.raises(
        RuntimeError,
        match="propertyquarry_api_ingress_database_url_must_be_role_separated",
    ):
        resolve_api_ingress_database_url(
            runtime_mode="prod",
            primary_database_url=primary,
            environ={
                "PROPERTYQUARRY_API_ADMISSION_DATABASE_URL": internal,
                "PROPERTYQUARRY_API_INGRESS_DATABASE_URL": internal,
            },
        )
def test_invalid_admission_keys_fail_closed() -> None:
    backend = MemoryAdmissionBackend()

    with pytest.raises(AdmissionBackendUnavailable, match="admission_key_invalid"):
        backend.consume(_charge(""))
    with pytest.raises(AdmissionBackendUnavailable, match="admission_key_invalid"):
        backend.acquire((ConcurrencyDimension("x" * 513, 1),), lease_seconds=30)


def test_admission_inputs_are_bounded_to_the_postgres_domain_before_io() -> None:
    class _StringCoercible:
        def __str__(self) -> str:
            return "coerced-key"

    class _IntCoercible:
        def __int__(self) -> int:
            return 1

    def forbidden_connect(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        pytest.fail("invalid admission input reached PostgreSQL")

    backends = (
        MemoryAdmissionBackend(),
        PostgresAdmissionBackend(
            "postgresql://test/property",
            connect=forbidden_connect,
        ),
    )
    for backend in backends:
        for key in (1, _StringCoercible()):
            with pytest.raises(
                AdmissionBackendUnavailable,
                match="admission_key_invalid",
            ):
                backend.consume(QuotaCharge(key, 1, 2, 60))  # type: ignore[arg-type]
        for value in (True, 1.0, "1", Decimal("1"), _IntCoercible()):
            for charge in (
                QuotaCharge("units", value, 2, 60),
                QuotaCharge("limit", 1, value, 60),
                QuotaCharge("window", 1, 2, value),
            ):
                with pytest.raises(
                    AdmissionBackendUnavailable,
                    match="admission_quota_invalid",
                ):
                    backend.consume(charge)  # type: ignore[arg-type]
        for charge in (
            QuotaCharge("bool-units", True, 1, 60),
            QuotaCharge("huge-units", 1 << 63, 1 << 63, 60),
            QuotaCharge("huge-window", 1, 1, 86_401),
            QuotaCharge("fractional", 1.5, 2, 60),
            QuotaCharge("dimension", 1, 2, 60, "x" * 65),
        ):
            with pytest.raises(
                AdmissionBackendUnavailable,
                match="admission_quota_invalid",
            ):
                backend.consume(charge)
        with pytest.raises(
            AdmissionBackendUnavailable,
            match="admission_quota_invalid",
        ):
            backend.consume(
                QuotaCharge(  # type: ignore[arg-type]
                    "dimension-type",
                    1,
                    2,
                    60,
                    _StringCoercible(),
                )
            )
        for value in (True, 1.0, "1", Decimal("1"), _IntCoercible()):
            with pytest.raises(
                AdmissionBackendUnavailable,
                match="admission_concurrency_invalid",
            ):
                backend.acquire(
                    (
                        ConcurrencyDimension(  # type: ignore[arg-type]
                            "concurrency",
                            value,
                        ),
                    ),
                    lease_seconds=30,
                )
            with pytest.raises(
                AdmissionBackendUnavailable,
                match="admission_lease_seconds_invalid",
            ):
                backend.acquire(
                    (ConcurrencyDimension("lease", 1),),
                    lease_seconds=value,  # type: ignore[arg-type]
                )
        with pytest.raises(
            AdmissionBackendUnavailable,
            match="admission_concurrency_invalid",
        ):
            backend.acquire(
                (
                    ConcurrencyDimension(
                        "dimension-type",
                        1,
                        _StringCoercible(),  # type: ignore[arg-type]
                    ),
                ),
                lease_seconds=30,
            )
        with pytest.raises(
            AdmissionBackendUnavailable,
            match="admission_quota_too_many_charges",
        ):
            backend.consume_many(tuple(_charge(f"charge-{index}") for index in range(17)))
        with pytest.raises(
            AdmissionBackendUnavailable,
            match="admission_concurrency_too_many_dimensions",
        ):
            backend.acquire(
                tuple(
                    ConcurrencyDimension(f"dimension-{index}", 1)
                    for index in range(17)
                ),
                lease_seconds=30,
            )
        with pytest.raises(
            AdmissionBackendUnavailable,
            match="admission_lease_seconds_invalid",
        ):
            backend.acquire(
                (ConcurrencyDimension("lease", 1),),
                lease_seconds=86_401,
            )

    invalid_clock = MemoryAdmissionBackend(clock=lambda: float("nan"))
    with pytest.raises(
        AdmissionBackendUnavailable,
        match="admission_clock_invalid",
    ):
        invalid_clock.consume(_charge("invalid-clock"))


class _StrictProbeCursor:
    class _Connection:
        autocommit = True

    def __init__(
        self,
        *,
        role_row: tuple[object, ...] | None = None,
        database_row: tuple[object, ...] = (True, False, False, False),
        writable_search_path: bool = False,
        cross_surface_row: tuple[bool, bool] = (False, False),
        relation_row: tuple[object, ...] = (
            "r",
            "propertyquarry_runtime",
            False,
            False,
            False,
            False,
            True,
            False,
            False,
        ),
        capacity_rows: list[tuple[object, ...]] | None = None,
        function_owner_role: str = ADMISSION_CAPACITY_OWNER_ROLE_DEFAULT,
        extra_trigger: bool = False,
    ) -> None:
        self.connection = self._Connection()
        self.role_row = role_row or (
            "propertyquarry_runtime",
            "propertyquarry_runtime",
            True,
            False,
            False,
            False,
            False,
            False,
            False,
        )
        self.database_row = database_row
        self.writable_search_path = writable_search_path
        self.cross_surface_row = cross_surface_row
        self.relation_row = relation_row
        self.capacity_rows = capacity_rows or [
            ("lease", 0, ADMISSION_LEASE_ROW_LIMIT),
            ("quota", 0, ADMISSION_QUOTA_ROW_LIMIT),
        ]
        self.function_owner_role = function_owner_role
        self.extra_trigger = extra_trigger
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.timeout_params: tuple[object, ...] = ()
        self._row = None
        self._rows: list[tuple[object, ...]] = []

    def execute(self, sql: str, params=()) -> None:  # noqa: ANN001
        normalized = " ".join(sql.split())
        normalized_params = tuple(params)
        self.executed.append((normalized, normalized_params))
        self._rows = []
        if normalized in {"BEGIN", "ROLLBACK"}:
            self._row = None
        elif "set_config('lock_timeout'" in normalized:
            self.timeout_params = normalized_params
            self._row = normalized_params
        elif "to_regclass" in normalized:
            self._row = (101, 102) if len(normalized_params) == 2 else (103,)
        elif "FROM pg_catalog.pg_roles AS role" in normalized:
            self._row = self.role_row
        elif "FROM pg_catalog.pg_database AS database" in normalized:
            self._row = self.database_row
        elif "unnest(current_schemas(TRUE))" in normalized:
            self._row = (self.writable_search_path,)
        elif "FROM pg_catalog.pg_class AS relation" in normalized:
            self._row = self.relation_row
        elif "FROM pg_catalog.pg_attribute AS attribute" in normalized:
            self._rows = [
                ("capacity_key", "text", True, "", "", ""),
                ("row_count", "bigint", True, "", "", ""),
                ("row_limit", "bigint", True, "", "", ""),
                (
                    "updated_at",
                    "timestamp with time zone",
                    True,
                    "statement_timestamp()",
                    "",
                    "",
                ),
            ]
            self._row = self._rows[0]
        elif "FROM pg_catalog.pg_constraint AS constraint_row" in normalized:
            self._rows = [
                ("pq_admission_capacity_count_check", "c", True, False, False),
                ("pq_admission_capacity_key_check", "c", True, False, False),
                ("pq_admission_capacity_limit_check", "c", True, False, False),
                (
                    "propertyquarry_admission_capacity_state_pkey",
                    "p",
                    True,
                    False,
                    False,
                ),
            ]
            self._row = self._rows[0]
        elif normalized.startswith(
            "SELECT has_table_privilege(current_user, %s, 'SELECT'),"
        ):
            self._row = (True, False, False, False, False, False, False)
        elif "propertyquarry_admission_capacity_state\" ORDER BY" in normalized:
            self._rows = list(self.capacity_rows)
            self._row = self._rows[0] if self._rows else None
        elif "FROM pg_catalog.pg_proc AS function_row" in normalized:
            function_name = str(normalized_params[1])
            source = ADMISSION_CAPACITY_FUNCTION_SOURCES[function_name]
            self._rows = [
                (
                    201,
                    "propertyquarry_runtime",
                    self.function_owner_role,
                    False,
                    False,
                    False,
                    False,
                    False,
                    False,
                    False,
                    "f",
                    True,
                    "v",
                    "u",
                    False,
                    0,
                    True,
                    "plpgsql",
                    ["search_path=pg_catalog"],
                    source,
                    False,
                    False,
                )
            ]
            self._row = self._rows[0]
        elif normalized.startswith(
            "SELECT has_schema_privilege(%s, namespace.oid, 'USAGE')"
        ):
            self._row = (
                True,
                False,
                True,
                False,
                True,
                False,
                False,
                False,
                False,
                False,
                False,
            )
        elif "FROM pg_catalog.pg_trigger AS trigger_row" in normalized:
            relation_oid = normalized_params[0]
            table_name = (
                "propertyquarry_admission_quota_buckets"
                if relation_oid == 101
                else "propertyquarry_admission_leases"
            )
            self._rows = sorted(
                (
                    trigger_name,
                    "O",
                    trigger_type,
                    function_name,
                    "propertyquarry_runtime",
                    1,
                    trigger_argument.encode("utf-8").hex() + "00",
                    old_table,
                    new_table,
                    True,
                    True,
                    True,
                    True,
                    True,
                )
                for (
                    trigger_name,
                    trigger_type,
                    function_name,
                    trigger_argument,
                    old_table,
                    new_table,
                ) in ADMISSION_CAPACITY_TRIGGER_CONTRACTS[table_name]
            )
            if self.extra_trigger:
                self._rows.append(
                    (
                        "unexpected_trigger",
                        "O",
                        4,
                        "unexpected_function",
                        "propertyquarry_runtime",
                        0,
                        "",
                        "",
                        "",
                        True,
                        True,
                        True,
                        True,
                        True,
                    )
                )
            self._row = self._rows[0]
        elif "FROM pg_catalog.pg_class AS other_relation" in normalized:
            self._row = self.cross_surface_row
        elif "has_table_privilege" in normalized:
            privilege = str(normalized_params[1])
            self._row = (
                privilege in {"SELECT", "INSERT", "UPDATE", "DELETE"},
            )
        else:
            self._row = None

    def fetchone(self):  # noqa: ANN201
        return self._row

    def fetchall(self):  # noqa: ANN201
        return list(self._rows)


def test_strict_probe_is_timeout_bounded_transactional_and_least_privilege() -> None:
    cursor = _StrictProbeCursor()

    probe_admission_cursor(
        cursor,
        lock_timeout_ms=700,
        statement_timeout_ms=2_000,
        idle_transaction_timeout_ms=6_000,
    )

    assert cursor.executed[0][0] == "BEGIN"
    assert cursor.executed[-1][0] == "ROLLBACK"
    assert cursor.timeout_params == ("700ms", "2000ms", "6000ms")


@pytest.mark.parametrize(
    ("cursor", "reason"),
    (
        (
            _StrictProbeCursor(
                role_row=(
                    "postgres",
                    "postgres",
                    True,
                    True,
                    True,
                    True,
                    True,
                    True,
                    False,
                )
            ),
            "admission_backend_role_elevated",
        ),
        (
            _StrictProbeCursor(
                role_row=(
                    "propertyquarry_runtime",
                    "postgres",
                    True,
                    False,
                    False,
                    False,
                    False,
                    False,
                    False,
                )
            ),
            "admission_backend_role_elevated",
        ),
        (
            _StrictProbeCursor(database_row=(True, False, True, False)),
            "admission_backend_database_authority_excess",
        ),
        (
            _StrictProbeCursor(writable_search_path=True),
            "admission_backend_search_path_authority_excess",
        ),
        (
            _StrictProbeCursor(
                relation_row=(
                    "r",
                    "propertyquarry_runtime",
                    False,
                    False,
                    True,
                    False,
                    True,
                    False,
                    False,
                )
            ),
            "admission_backend_ownership_authority_excess",
        ),
        (
            _StrictProbeCursor(cross_surface_row=(True, False)),
            "admission_backend_cross_surface_authority_excess",
        ),
        (
            _StrictProbeCursor(cross_surface_row=(False, True)),
            "admission_backend_cross_surface_authority_excess",
        ),
    ),
)
def test_strict_probe_rejects_elevated_or_shadowing_authority(
    cursor: _StrictProbeCursor,
    reason: str,
) -> None:
    with pytest.raises(AdmissionBackendUnavailable, match=reason):
        probe_admission_cursor(cursor)
    assert cursor.executed[-1][0] == "ROLLBACK"


@pytest.mark.parametrize(
    ("cursor", "reason"),
    (
        (
            _StrictProbeCursor(
                capacity_rows=[
                    ("lease", 0, ADMISSION_LEASE_ROW_LIMIT),
                    ("quota", 0, ADMISSION_QUOTA_ROW_LIMIT + 1),
                ]
            ),
            "admission_backend_capacity_state_drift",
        ),
        (
            _StrictProbeCursor(function_owner_role="unexpected_owner"),
            "admission_backend_capacity_function_drift",
        ),
        (
            _StrictProbeCursor(extra_trigger=True),
            "admission_backend_capacity_trigger_drift",
        ),
    ),
)
def test_strict_probe_rejects_capacity_state_or_catalog_drift(
    cursor: _StrictProbeCursor,
    reason: str,
) -> None:
    with pytest.raises(AdmissionBackendUnavailable, match=reason):
        probe_admission_cursor(cursor)
    assert cursor.executed[-1][0] == "ROLLBACK"


def test_admission_probe_requires_every_dml_privilege_individually() -> None:
    class _ProbeCursor:
        def __init__(self, allowed_privileges: set[str]) -> None:
            self.allowed_privileges = allowed_privileges
            self.checked_privileges: list[str] = []
            self._row = None

        def execute(self, sql: str, params=()) -> None:  # noqa: ANN001
            normalized = " ".join(sql.split())
            if "to_regclass" in normalized:
                self._row = tuple(params)
            elif "has_table_privilege" in normalized:
                privilege = str(params[1])
                self.checked_privileges.append(privilege)
                self._row = (privilege in self.allowed_privileges,)
            else:
                self._row = None

        def fetchone(self):  # noqa: ANN201
            return self._row

    partial = _ProbeCursor({"SELECT", "INSERT", "UPDATE"})
    with pytest.raises(
        AdmissionBackendUnavailable,
        match="admission_backend_write_authority_missing",
    ):
        probe_admission_cursor(partial, require_least_privilege=False)
    assert partial.checked_privileges == ["SELECT", "INSERT", "UPDATE", "DELETE"]

    complete = _ProbeCursor({"SELECT", "INSERT", "UPDATE", "DELETE"})
    probe_admission_cursor(complete, require_least_privilege=False)
    assert complete.checked_privileges == [
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
    ] * 2
