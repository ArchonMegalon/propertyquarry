from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import math
import os
from queue import Empty, Full, LifoQueue
import re
import threading
import time
from typing import Any, Protocol
from uuid import uuid4


_QUOTA_TABLE = "propertyquarry_admission_quota_buckets"
_LEASE_TABLE = "propertyquarry_admission_leases"
ADMISSION_CAPACITY_STATE_TABLE = "propertyquarry_admission_capacity_state"
ADMISSION_CAPACITY_OWNER_ROLE_DEFAULT = "propertyquarry_admission_capacity_owner"
ADMISSION_QUOTA_ROW_LIMIT = 1_000_000
ADMISSION_LEASE_ROW_LIMIT = 100_000
ADMISSION_CAPACITY_INSERT_FUNCTION = (
    "propertyquarry_admission_capacity_after_insert"
)
ADMISSION_CAPACITY_DELETE_FUNCTION = (
    "propertyquarry_admission_capacity_after_delete"
)
ADMISSION_CAPACITY_TRUNCATE_FUNCTION = (
    "propertyquarry_admission_capacity_after_truncate"
)
ADMISSION_CAPACITY_INSERT_FUNCTION_SOURCE = r"""
DECLARE
    capacity_key TEXT;
    capacity_limit BIGINT;
    inserted_count BIGINT;
    updated_count BIGINT;
BEGIN
    IF TG_TABLE_NAME = 'propertyquarry_admission_quota_buckets' THEN
        capacity_key := 'quota';
        capacity_limit := 1000000;
    ELSIF TG_TABLE_NAME = 'propertyquarry_admission_leases' THEN
        capacity_key := 'lease';
        capacity_limit := 100000;
    ELSE
        RAISE EXCEPTION 'propertyquarry_admission_capacity_trigger_relation_drift'
            USING ERRCODE = '55000';
    END IF;

    IF TG_OP <> 'INSERT'
       OR TG_NARGS <> 1
       OR TG_ARGV[0] IS DISTINCT FROM capacity_key THEN
        RAISE EXCEPTION 'propertyquarry_admission_capacity_trigger_contract_drift'
            USING ERRCODE = '55000';
    END IF;

    SELECT pg_catalog.count(*)
    INTO inserted_count
    FROM propertyquarry_admission_inserted_rows;
    IF inserted_count = 0 THEN
        RETURN NULL;
    END IF;

    EXECUTE pg_catalog.format(
        'UPDATE %I.propertyquarry_admission_capacity_state '
        'SET row_count = row_count + $1, '
        'updated_at = pg_catalog.statement_timestamp() '
        'WHERE capacity_key = $2 AND row_limit = $3 '
        'AND row_count >= 0 AND row_count <= row_limit - $1 '
        'RETURNING row_count',
        TG_TABLE_SCHEMA
    )
    INTO updated_count
    USING inserted_count, capacity_key, capacity_limit;

    IF updated_count IS NULL THEN
        RAISE EXCEPTION 'propertyquarry_admission_capacity_exhausted:%', capacity_key
            USING ERRCODE = '54000';
    END IF;
    RETURN NULL;
END
""".strip()
ADMISSION_CAPACITY_DELETE_FUNCTION_SOURCE = r"""
DECLARE
    capacity_key TEXT;
    capacity_limit BIGINT;
    deleted_count BIGINT;
    updated_count BIGINT;
BEGIN
    IF TG_TABLE_NAME = 'propertyquarry_admission_quota_buckets' THEN
        capacity_key := 'quota';
        capacity_limit := 1000000;
    ELSIF TG_TABLE_NAME = 'propertyquarry_admission_leases' THEN
        capacity_key := 'lease';
        capacity_limit := 100000;
    ELSE
        RAISE EXCEPTION 'propertyquarry_admission_capacity_trigger_relation_drift'
            USING ERRCODE = '55000';
    END IF;

    IF TG_OP <> 'DELETE'
       OR TG_NARGS <> 1
       OR TG_ARGV[0] IS DISTINCT FROM capacity_key THEN
        RAISE EXCEPTION 'propertyquarry_admission_capacity_trigger_contract_drift'
            USING ERRCODE = '55000';
    END IF;

    SELECT pg_catalog.count(*)
    INTO deleted_count
    FROM propertyquarry_admission_deleted_rows;
    IF deleted_count = 0 THEN
        RETURN NULL;
    END IF;

    EXECUTE pg_catalog.format(
        'UPDATE %I.propertyquarry_admission_capacity_state '
        'SET row_count = row_count - $1, '
        'updated_at = pg_catalog.statement_timestamp() '
        'WHERE capacity_key = $2 AND row_limit = $3 '
        'AND row_count >= $1 AND row_count <= row_limit '
        'RETURNING row_count',
        TG_TABLE_SCHEMA
    )
    INTO updated_count
    USING deleted_count, capacity_key, capacity_limit;

    IF updated_count IS NULL THEN
        RAISE EXCEPTION 'propertyquarry_admission_capacity_counter_drift:%', capacity_key
            USING ERRCODE = '55000';
    END IF;
    RETURN NULL;
END
""".strip()
ADMISSION_CAPACITY_TRUNCATE_FUNCTION_SOURCE = r"""
DECLARE
    capacity_key TEXT;
    capacity_limit BIGINT;
    updated_count BIGINT;
BEGIN
    IF TG_TABLE_NAME = 'propertyquarry_admission_quota_buckets' THEN
        capacity_key := 'quota';
        capacity_limit := 1000000;
    ELSIF TG_TABLE_NAME = 'propertyquarry_admission_leases' THEN
        capacity_key := 'lease';
        capacity_limit := 100000;
    ELSE
        RAISE EXCEPTION 'propertyquarry_admission_capacity_trigger_relation_drift'
            USING ERRCODE = '55000';
    END IF;

    IF TG_OP <> 'TRUNCATE'
       OR TG_NARGS <> 1
       OR TG_ARGV[0] IS DISTINCT FROM capacity_key THEN
        RAISE EXCEPTION 'propertyquarry_admission_capacity_trigger_contract_drift'
            USING ERRCODE = '55000';
    END IF;

    EXECUTE pg_catalog.format(
        'UPDATE %I.propertyquarry_admission_capacity_state '
        'SET row_count = 0, updated_at = pg_catalog.statement_timestamp() '
        'WHERE capacity_key = $1 AND row_limit = $2 '
        'RETURNING row_count',
        TG_TABLE_SCHEMA
    )
    INTO updated_count
    USING capacity_key, capacity_limit;

    IF updated_count IS NULL THEN
        RAISE EXCEPTION 'propertyquarry_admission_capacity_counter_drift:%', capacity_key
            USING ERRCODE = '55000';
    END IF;
    RETURN NULL;
END
""".strip()
ADMISSION_CAPACITY_FUNCTION_SOURCES = {
    ADMISSION_CAPACITY_INSERT_FUNCTION: ADMISSION_CAPACITY_INSERT_FUNCTION_SOURCE,
    ADMISSION_CAPACITY_DELETE_FUNCTION: ADMISSION_CAPACITY_DELETE_FUNCTION_SOURCE,
    ADMISSION_CAPACITY_TRUNCATE_FUNCTION: ADMISSION_CAPACITY_TRUNCATE_FUNCTION_SOURCE,
}
ADMISSION_CAPACITY_TRIGGER_CONTRACTS = {
    _QUOTA_TABLE: (
        (
            "propertyquarry_admission_quota_capacity_after_insert",
            4,
            ADMISSION_CAPACITY_INSERT_FUNCTION,
            "quota",
            "",
            "propertyquarry_admission_inserted_rows",
        ),
        (
            "propertyquarry_admission_quota_capacity_after_delete",
            8,
            ADMISSION_CAPACITY_DELETE_FUNCTION,
            "quota",
            "propertyquarry_admission_deleted_rows",
            "",
        ),
        (
            "propertyquarry_admission_quota_capacity_after_truncate",
            32,
            ADMISSION_CAPACITY_TRUNCATE_FUNCTION,
            "quota",
            "",
            "",
        ),
    ),
    _LEASE_TABLE: (
        (
            "propertyquarry_admission_lease_capacity_after_insert",
            4,
            ADMISSION_CAPACITY_INSERT_FUNCTION,
            "lease",
            "",
            "propertyquarry_admission_inserted_rows",
        ),
        (
            "propertyquarry_admission_lease_capacity_after_delete",
            8,
            ADMISSION_CAPACITY_DELETE_FUNCTION,
            "lease",
            "propertyquarry_admission_deleted_rows",
            "",
        ),
        (
            "propertyquarry_admission_lease_capacity_after_truncate",
            32,
            ADMISSION_CAPACITY_TRUNCATE_FUNCTION,
            "lease",
            "",
            "",
        ),
    ),
}
_MAX_KEY_LENGTH = 512
_MAX_DIMENSION_LENGTH = 64
_MAX_PG_BIGINT = (1 << 63) - 1
_MAX_QUOTA_WINDOW_SECONDS = 86_400
_MAX_LEASE_SECONDS = 86_400
_MAX_QUOTA_CHARGES = 16
_MAX_CONCURRENCY_DIMENSIONS = 16
_DEFAULT_LOCK_TIMEOUT_MS = 2_000
_DEFAULT_STATEMENT_TIMEOUT_MS = 5_000
_DEFAULT_IDLE_TRANSACTION_TIMEOUT_MS = 10_000
_MIN_TIMEOUT_MS = 50
_MAX_LOCK_TIMEOUT_MS = 10_000
_MAX_STATEMENT_TIMEOUT_MS = 30_000
_MAX_IDLE_TRANSACTION_TIMEOUT_MS = 60_000
_ROLE_NAME_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class AdmissionBackendUnavailable(RuntimeError):
    """The authoritative admission store could not make a safe decision."""


@dataclass(frozen=True)
class QuotaCharge:
    key: str
    units: int
    limit: int
    window_seconds: int
    dimension: str = ""


@dataclass(frozen=True)
class ConcurrencyDimension:
    key: str
    limit: int
    dimension: str = ""


@dataclass(frozen=True)
class AdmissionDecision:
    allowed: bool
    retry_after_seconds: int = 1
    dimension: str = ""
    lease_id: str = ""


class AdmissionBackend(Protocol):
    backend_name: str

    def consume(self, charge: QuotaCharge) -> AdmissionDecision: ...

    def consume_many(self, charges: Sequence[QuotaCharge]) -> AdmissionDecision: ...

    def acquire(
        self,
        dimensions: Sequence[ConcurrencyDimension],
        *,
        lease_seconds: int,
    ) -> AdmissionDecision: ...

    def release(self, lease_id: str) -> None: ...

    def renew(self, lease_id: str, *, lease_seconds: int) -> bool: ...

    def capacity_snapshot(self) -> tuple[tuple[str, int, int], ...]: ...

    def probe(self) -> None: ...


def _normalized_key(value: object) -> str:
    if type(value) is not str:
        raise AdmissionBackendUnavailable("admission_key_invalid")
    key = value.strip()
    if not key or len(key) > _MAX_KEY_LENGTH or "\x00" in key:
        raise AdmissionBackendUnavailable("admission_key_invalid")
    return key


def _normalized_dimension(value: object, *, error_code: str) -> str:
    if type(value) is not str:
        raise AdmissionBackendUnavailable(error_code)
    dimension = value.strip()
    if len(dimension) > _MAX_DIMENSION_LENGTH or "\x00" in dimension:
        raise AdmissionBackendUnavailable(error_code)
    return dimension


def _normalized_int(
    value: object,
    *,
    minimum: int,
    maximum: int,
    error_code: str,
) -> int:
    if type(value) is not int:
        raise AdmissionBackendUnavailable(error_code)
    parsed = value
    if parsed < minimum or parsed > maximum:
        raise AdmissionBackendUnavailable(error_code)
    return parsed


def _configured_int(
    value: object,
    *,
    minimum: int,
    maximum: int,
    error_code: str,
) -> int:
    if isinstance(value, bool):
        raise RuntimeError(error_code)
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError(error_code) from exc
    if isinstance(value, float) and (not math.isfinite(value) or value != parsed):
        raise RuntimeError(error_code)
    if parsed < minimum or parsed > maximum:
        raise RuntimeError(error_code)
    return parsed


def normalize_admission_capacity_owner_role(value: object) -> str:
    role_name = str(value or ADMISSION_CAPACITY_OWNER_ROLE_DEFAULT).strip()
    if not _ROLE_NAME_PATTERN.fullmatch(role_name):
        raise RuntimeError("propertyquarry_admission_capacity_owner_role_invalid")
    return role_name


def _validated_admission_capacity_rows(
    rows: Sequence[Sequence[object]],
) -> tuple[tuple[str, int, int], ...]:
    expected_limits = {
        "lease": ADMISSION_LEASE_ROW_LIMIT,
        "quota": ADMISSION_QUOTA_ROW_LIMIT,
    }
    if len(rows) != len(expected_limits):
        raise AdmissionBackendUnavailable(
            "admission_backend_capacity_state_drift"
        )
    normalized: list[tuple[str, int, int]] = []
    for row in rows:
        if len(row) != 3:
            raise AdmissionBackendUnavailable(
                "admission_backend_capacity_state_drift"
            )
        capacity_key = str(row[0] or "")
        row_count = row[1]
        row_limit = row[2]
        expected_limit = expected_limits.get(capacity_key)
        if (
            expected_limit is None
            or type(row_count) is not int
            or type(row_limit) is not int
            or row_limit != expected_limit
            or row_count < 0
            or row_count > row_limit
        ):
            raise AdmissionBackendUnavailable(
                "admission_backend_capacity_state_drift"
            )
        normalized.append((capacity_key, row_count, row_limit))
    normalized.sort()
    if {row[0] for row in normalized} != set(expected_limits):
        raise AdmissionBackendUnavailable(
            "admission_backend_capacity_state_drift"
        )
    return tuple(normalized)


def _normalized_charge(charge: QuotaCharge) -> QuotaCharge:
    key = _normalized_key(charge.key)
    units = _normalized_int(
        charge.units,
        minimum=1,
        maximum=_MAX_PG_BIGINT,
        error_code="admission_quota_invalid",
    )
    limit = _normalized_int(
        charge.limit,
        minimum=1,
        maximum=_MAX_PG_BIGINT,
        error_code="admission_quota_invalid",
    )
    window_seconds = _normalized_int(
        charge.window_seconds,
        minimum=1,
        maximum=_MAX_QUOTA_WINDOW_SECONDS,
        error_code="admission_quota_invalid",
    )
    return QuotaCharge(
        key=key,
        units=units,
        limit=limit,
        window_seconds=window_seconds,
        dimension=_normalized_dimension(
            charge.dimension,
            error_code="admission_quota_invalid",
        ),
    )


def _normalized_dimensions(
    dimensions: Iterable[ConcurrencyDimension],
) -> tuple[ConcurrencyDimension, ...]:
    by_key: dict[str, ConcurrencyDimension] = {}
    for index, raw in enumerate(dimensions):
        if index >= _MAX_CONCURRENCY_DIMENSIONS:
            raise AdmissionBackendUnavailable("admission_concurrency_too_many_dimensions")
        key = _normalized_key(raw.key)
        limit = _normalized_int(
            raw.limit,
            minimum=1,
            maximum=_MAX_PG_BIGINT,
            error_code="admission_concurrency_invalid",
        )
        candidate = ConcurrencyDimension(
            key=key,
            limit=limit,
            dimension=_normalized_dimension(
                raw.dimension,
                error_code="admission_concurrency_invalid",
            ),
        )
        previous = by_key.get(key)
        if previous is None or candidate.limit < previous.limit:
            by_key[key] = candidate
    if not by_key:
        raise AdmissionBackendUnavailable("admission_concurrency_empty")
    return tuple(by_key[key] for key in sorted(by_key))


def _normalized_lease_seconds(value: object) -> int:
    return _normalized_int(
        value,
        minimum=1,
        maximum=_MAX_LEASE_SECONDS,
        error_code="admission_lease_seconds_invalid",
    )


def _normalized_clock_value(value: object) -> float:
    try:
        now = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AdmissionBackendUnavailable("admission_clock_invalid") from exc
    if not math.isfinite(now) or now < 0.0:
        raise AdmissionBackendUnavailable("admission_clock_invalid")
    return now


class MemoryAdmissionBackend:
    """Bounded single-process backend for explicit development and tests only."""

    backend_name = "memory"

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        max_quota_keys: int = 8_192,
        max_leases: int = 2_048,
        cleanup_batch: int = 64,
    ) -> None:
        self._clock = clock
        self._max_quota_keys = max(1, int(max_quota_keys))
        self._max_leases = max(1, int(max_leases))
        self._cleanup_batch = max(1, min(1_024, int(cleanup_batch)))
        self._quota_buckets: OrderedDict[str, tuple[int, int, int, int]] = OrderedDict()
        self._leases: OrderedDict[str, tuple[float, tuple[str, ...]]] = OrderedDict()
        self._lease_counts: dict[str, int] = {}
        self._lease_policy_limits: dict[str, int] = {}
        self._lock = threading.Lock()

    def _cleanup_quota_buckets(self, *, now: float) -> None:
        for _index in range(min(self._cleanup_batch, len(self._quota_buckets))):
            key, bucket = self._quota_buckets.popitem(last=False)
            window, _used, window_seconds, _limit = bucket
            if now < (window + 1) * window_seconds:
                self._quota_buckets[key] = bucket

    def _cleanup_leases(self, *, now: float) -> None:
        for _index in range(min(self._cleanup_batch, len(self._leases))):
            lease_id, lease = self._leases.popitem(last=False)
            expires_at, keys = lease
            if expires_at > now:
                self._leases[lease_id] = lease
                continue
            for key in keys:
                updated = self._lease_counts.get(key, 0) - 1
                if updated > 0:
                    self._lease_counts[key] = updated
                else:
                    self._lease_counts.pop(key, None)
                    self._lease_policy_limits.pop(key, None)

    def consume(self, charge: QuotaCharge) -> AdmissionDecision:
        return self.consume_many((charge,))

    def consume_many(self, charges: Sequence[QuotaCharge]) -> AdmissionDecision:
        if len(charges) > _MAX_QUOTA_CHARGES:
            raise AdmissionBackendUnavailable("admission_quota_too_many_charges")
        normalized = tuple(_normalized_charge(charge) for charge in charges)
        if not normalized:
            raise AdmissionBackendUnavailable("admission_quota_empty")
        keys = [charge.key for charge in normalized]
        if len(set(keys)) != len(keys):
            raise AdmissionBackendUnavailable("admission_quota_key_duplicate")
        now = _normalized_clock_value(self._clock())
        with self._lock:
            self._cleanup_quota_buckets(now=now)
            projected: list[tuple[QuotaCharge, int, int, int, int, int]] = []
            new_keys = 0
            for charge in normalized:
                window_seconds = charge.window_seconds
                window = int(now // window_seconds)
                effective_limit = charge.limit
                previous = self._quota_buckets.get(charge.key)
                used = 0
                if previous is not None:
                    previous_window, previous_used, previous_seconds, previous_limit = previous
                    previous_expiry = (previous_window + 1) * previous_seconds
                    if now < previous_expiry:
                        window = previous_window
                        window_seconds = previous_seconds
                        used = previous_used
                        effective_limit = min(previous_limit, charge.limit)
                else:
                    new_keys += 1
                retry_after = max(
                    1,
                    int(math.ceil(((window + 1) * window_seconds) - now)),
                )
                if used + charge.units > effective_limit:
                    if previous is not None and now < (window + 1) * window_seconds:
                        self._quota_buckets[charge.key] = (
                            window,
                            used,
                            window_seconds,
                            effective_limit,
                        )
                        self._quota_buckets.move_to_end(charge.key)
                    return AdmissionDecision(
                        False,
                        retry_after_seconds=retry_after,
                        dimension=charge.dimension,
                    )
                projected.append(
                    (
                        charge,
                        window,
                        used + charge.units,
                        retry_after,
                        window_seconds,
                        effective_limit,
                    )
                )
            if len(self._quota_buckets) + new_keys > self._max_quota_keys:
                retry_after = min(item[3] for item in projected)
                return AdmissionDecision(False, retry_after_seconds=retry_after, dimension="capacity")
            for charge, window, used, _retry_after, window_seconds, effective_limit in projected:
                self._quota_buckets[charge.key] = (
                    window,
                    used,
                    window_seconds,
                    effective_limit,
                )
                self._quota_buckets.move_to_end(charge.key)
            return AdmissionDecision(
                True,
                retry_after_seconds=min(item[3] for item in projected),
            )

    def acquire(
        self,
        dimensions: Sequence[ConcurrencyDimension],
        *,
        lease_seconds: int,
    ) -> AdmissionDecision:
        normalized = _normalized_dimensions(dimensions)
        ttl = _normalized_lease_seconds(lease_seconds)
        now = _normalized_clock_value(self._clock())
        with self._lock:
            self._cleanup_leases(now=now)
            effective_limits: dict[str, int] = {}
            for dimension in normalized:
                active_count = self._lease_counts.get(dimension.key, 0)
                active_limit = self._lease_policy_limits.get(
                    dimension.key,
                    dimension.limit,
                )
                effective_limit = min(active_limit, dimension.limit)
                effective_limits[dimension.key] = effective_limit
                if active_count > 0:
                    # Once a stricter replica observes a live dimension, every
                    # subsequent acquisition honors that bound until the live
                    # lease set drains. A rolling configuration change cannot
                    # therefore loosen distributed capacity mid-flight.
                    self._lease_policy_limits[dimension.key] = effective_limit
                if active_count >= effective_limit:
                    retry_after = 1
                    expiries = [
                        expiry
                        for expiry, keys in self._leases.values()
                        if dimension.key in keys and expiry > now
                    ]
                    if expiries:
                        retry_after = max(1, int(math.ceil(min(expiries) - now)))
                    return AdmissionDecision(
                        False,
                        retry_after_seconds=retry_after,
                        dimension=dimension.dimension,
                    )
            if len(self._leases) >= self._max_leases:
                return AdmissionDecision(False, retry_after_seconds=1, dimension="capacity")
            lease_id = str(uuid4())
            keys = tuple(dimension.key for dimension in normalized)
            self._leases[lease_id] = (now + ttl, keys)
            for key in keys:
                self._lease_counts[key] = self._lease_counts.get(key, 0) + 1
                self._lease_policy_limits[key] = effective_limits[key]
            return AdmissionDecision(True, lease_id=lease_id)

    def release(self, lease_id: str) -> None:
        normalized = str(lease_id or "").strip()
        if not normalized:
            return
        with self._lock:
            lease = self._leases.pop(normalized, None)
            if lease is None:
                return
            _expires_at, keys = lease
            for key in keys:
                updated = self._lease_counts.get(key, 0) - 1
                if updated > 0:
                    self._lease_counts[key] = updated
                else:
                    self._lease_counts.pop(key, None)
                    self._lease_policy_limits.pop(key, None)

    def renew(self, lease_id: str, *, lease_seconds: int) -> bool:
        normalized = str(lease_id or "").strip()
        if not normalized:
            return False
        ttl = _normalized_lease_seconds(lease_seconds)
        now = _normalized_clock_value(self._clock())
        with self._lock:
            self._cleanup_leases(now=now)
            lease = self._leases.get(normalized)
            if lease is None:
                return False
            _expires_at, keys = lease
            self._leases[normalized] = (now + ttl, keys)
            self._leases.move_to_end(normalized)
            return True

    def capacity_snapshot(self) -> tuple[tuple[str, int, int], ...]:
        # The process-local development backend is intentionally not evidence
        # for the shared PostgreSQL v17 capacity contract.
        return ()

    def probe(self) -> None:
        return


def _configure_admission_transaction(
    cursor: Any,
    *,
    lock_timeout_ms: int,
    statement_timeout_ms: int,
    idle_transaction_timeout_ms: int,
) -> None:
    cursor.execute(
        """
        SELECT set_config('lock_timeout', %s, TRUE),
               set_config('statement_timeout', %s, TRUE),
               set_config('idle_in_transaction_session_timeout', %s, TRUE)
        """,
        (
            f"{lock_timeout_ms}ms",
            f"{statement_timeout_ms}ms",
            f"{idle_transaction_timeout_ms}ms",
        ),
    )


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _probe_strict_admission_capacity_contract(
    cursor: Any,
    *,
    schema_name: str,
    write_relation_oids: Sequence[object],
    capacity_relation_oid: object,
    capacity_owner_role: str,
) -> None:
    cursor.execute(
        """
        SELECT attribute.attname,
               pg_catalog.format_type(attribute.atttypid, attribute.atttypmod),
               attribute.attnotnull,
               COALESCE(
                   pg_catalog.pg_get_expr(
                       default_value.adbin,
                       default_value.adrelid
                   ),
                   ''
               ),
               attribute.attidentity,
               attribute.attgenerated
        FROM pg_catalog.pg_attribute AS attribute
        LEFT JOIN pg_catalog.pg_attrdef AS default_value
          ON default_value.adrelid = attribute.attrelid
         AND default_value.adnum = attribute.attnum
        WHERE attribute.attrelid = %s
          AND attribute.attnum > 0
          AND NOT attribute.attisdropped
        ORDER BY attribute.attnum
        """,
        (capacity_relation_oid,),
    )
    capacity_columns = cursor.fetchall()
    if capacity_columns != [
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
    ]:
        raise AdmissionBackendUnavailable(
            "admission_backend_capacity_schema_drift"
        )

    cursor.execute(
        """
        SELECT constraint_row.conname,
               constraint_row.contype,
               constraint_row.convalidated,
               constraint_row.condeferrable,
               constraint_row.condeferred
        FROM pg_catalog.pg_constraint AS constraint_row
        WHERE constraint_row.conrelid = %s
        ORDER BY constraint_row.conname
        """,
        (capacity_relation_oid,),
    )
    if cursor.fetchall() != [
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
    ]:
        raise AdmissionBackendUnavailable(
            "admission_backend_capacity_schema_drift"
        )

    qualified_capacity_table = (
        f"{_quoted_identifier(schema_name)}."
        f"{_quoted_identifier(ADMISSION_CAPACITY_STATE_TABLE)}"
    )
    cursor.execute(
        """
        SELECT has_table_privilege(current_user, %s, 'SELECT'),
               has_table_privilege(current_user, %s, 'INSERT'),
               has_table_privilege(current_user, %s, 'UPDATE'),
               has_table_privilege(current_user, %s, 'DELETE'),
               has_table_privilege(current_user, %s, 'TRUNCATE'),
               has_table_privilege(current_user, %s, 'REFERENCES'),
               has_table_privilege(current_user, %s, 'TRIGGER')
        """,
        (capacity_relation_oid,) * 7,
    )
    if cursor.fetchone() != (True, False, False, False, False, False, False):
        raise AdmissionBackendUnavailable(
            "admission_backend_capacity_authority_drift"
        )
    cursor.execute(
        f"""
        SELECT capacity_key, row_count, row_limit
        FROM {qualified_capacity_table}
        ORDER BY capacity_key
        """
    )
    _validated_admission_capacity_rows(cursor.fetchall())

    for function_name, expected_source in ADMISSION_CAPACITY_FUNCTION_SOURCES.items():
        cursor.execute(
            """
            SELECT function_row.oid,
                   function_namespace.nspname,
                   owner_role.rolname,
                   owner_role.rolcanlogin,
                   owner_role.rolsuper,
                   owner_role.rolcreaterole,
                   owner_role.rolcreatedb,
                   owner_role.rolreplication,
                   owner_role.rolbypassrls,
                   EXISTS (
                       SELECT 1
                       FROM pg_catalog.pg_auth_members AS membership
                       WHERE membership.member = owner_role.oid
                   ),
                   function_row.prokind,
                   function_row.prosecdef,
                   function_row.provolatile,
                   function_row.proparallel,
                   function_row.proleakproof,
                   function_row.pronargs,
                   function_row.prorettype = 'pg_catalog.trigger'::regtype,
                   language_row.lanname,
                   function_row.proconfig,
                   function_row.prosrc,
                   has_function_privilege(
                       current_user,
                       function_row.oid,
                       'EXECUTE'
                   ),
                   EXISTS (
                       SELECT 1
                       FROM pg_catalog.aclexplode(
                           COALESCE(
                               function_row.proacl,
                               pg_catalog.acldefault(
                                   'f',
                                   function_row.proowner
                               )
                           )
                       ) AS function_acl
                       WHERE function_acl.privilege_type = 'EXECUTE'
                         AND function_acl.grantee <> function_row.proowner
                   )
            FROM pg_catalog.pg_proc AS function_row
            JOIN pg_catalog.pg_namespace AS function_namespace
              ON function_namespace.oid = function_row.pronamespace
            JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid = function_row.proowner
            JOIN pg_catalog.pg_language AS language_row
              ON language_row.oid = function_row.prolang
            WHERE function_namespace.nspname = %s
              AND function_row.proname = %s
            ORDER BY function_row.oid
            """,
            (schema_name, function_name),
        )
        function_rows = cursor.fetchall()
        if len(function_rows) != 1:
            raise AdmissionBackendUnavailable(
                "admission_backend_capacity_function_drift"
            )
        function_row = function_rows[0]
        if (
            str(function_row[1]) != schema_name
            or str(function_row[2]) != capacity_owner_role
            or function_row[3] is not False
            or any(value is True for value in function_row[4:10])
            or str(function_row[10]) != "f"
            or function_row[11] is not True
            or str(function_row[12]) != "v"
            or str(function_row[13]) != "u"
            or function_row[14] is not False
            or int(function_row[15]) != 0
            or function_row[16] is not True
            or str(function_row[17]) != "plpgsql"
            or tuple(function_row[18] or ()) != ("search_path=pg_catalog",)
            or str(function_row[19] or "").strip() != expected_source
            or function_row[20] is not False
            or function_row[21] is not False
        ):
            raise AdmissionBackendUnavailable(
                "admission_backend_capacity_function_drift"
            )
    cursor.execute(
        """
        SELECT has_schema_privilege(%s, namespace.oid, 'USAGE'),
               has_schema_privilege(%s, namespace.oid, 'CREATE'),
               has_table_privilege(%s, %s, 'SELECT'),
               has_table_privilege(%s, %s, 'INSERT'),
               has_table_privilege(%s, %s, 'UPDATE'),
               has_table_privilege(%s, %s, 'DELETE'),
               has_table_privilege(%s, %s, 'TRUNCATE'),
               has_table_privilege(%s, %s, 'REFERENCES'),
               has_table_privilege(%s, %s, 'TRIGGER'),
               has_table_privilege(
                   %s,
                   %s,
                   'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
               ),
               has_table_privilege(
                   %s,
                   %s,
                   'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
               )
        FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname = %s
        """,
        (
            capacity_owner_role,
            capacity_owner_role,
            capacity_owner_role,
            capacity_relation_oid,
            capacity_owner_role,
            capacity_relation_oid,
            capacity_owner_role,
            capacity_relation_oid,
            capacity_owner_role,
            capacity_relation_oid,
            capacity_owner_role,
            capacity_relation_oid,
            capacity_owner_role,
            capacity_relation_oid,
            capacity_owner_role,
            capacity_relation_oid,
            capacity_owner_role,
            write_relation_oids[0],
            capacity_owner_role,
            write_relation_oids[1],
            schema_name,
        ),
    )
    if cursor.fetchone() != (
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
    ):
        raise AdmissionBackendUnavailable(
            "admission_backend_capacity_owner_authority_drift"
        )

    relation_contract = zip(
        (_QUOTA_TABLE, _LEASE_TABLE),
        write_relation_oids,
        strict=True,
    )
    for table_name, relation_oid in relation_contract:
        cursor.execute(
            """
            SELECT trigger_row.tgname,
                   trigger_row.tgenabled,
                   trigger_row.tgtype::integer,
                   function_row.proname,
                   function_namespace.nspname,
                   trigger_row.tgnargs,
                   pg_catalog.encode(trigger_row.tgargs, 'hex'),
                   COALESCE(trigger_row.tgoldtable, ''),
                   COALESCE(trigger_row.tgnewtable, ''),
                   trigger_row.tgqual IS NULL,
                   trigger_row.tgconstraint = 0,
                   NOT trigger_row.tgdeferrable,
                   NOT trigger_row.tginitdeferred,
                   pg_catalog.cardinality(
                       trigger_row.tgattr::smallint[]
                   ) = 0
            FROM pg_catalog.pg_trigger AS trigger_row
            JOIN pg_catalog.pg_proc AS function_row
              ON function_row.oid = trigger_row.tgfoid
            JOIN pg_catalog.pg_namespace AS function_namespace
              ON function_namespace.oid = function_row.pronamespace
            WHERE trigger_row.tgrelid = %s
              AND NOT trigger_row.tgisinternal
            ORDER BY trigger_row.tgname
            """,
            (relation_oid,),
        )
        trigger_rows = cursor.fetchall()
        expected_rows = sorted(
            (
                trigger_name,
                "O",
                trigger_type,
                function_name,
                schema_name,
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
        if trigger_rows != expected_rows:
            raise AdmissionBackendUnavailable(
                "admission_backend_capacity_trigger_drift"
            )


def _probe_strict_admission_authority(
    cursor: Any,
    *,
    write_relation_oids: Sequence[object],
    capacity_relation_oid: object,
    capacity_owner_role: str,
) -> None:
    cursor.execute(
        """
        SELECT current_user,
               session_user,
               role.rolcanlogin,
               role.rolsuper,
               role.rolcreaterole,
               role.rolcreatedb,
               role.rolreplication,
               role.rolbypassrls,
               EXISTS (
                   SELECT 1
                   FROM pg_catalog.pg_roles AS inherited
                   WHERE inherited.oid <> role.oid
                     AND pg_catalog.pg_has_role(
                         current_user,
                         inherited.oid,
                         'MEMBER'
                     )
               )
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_user
        """
    )
    role_row = cursor.fetchone()
    if (
        role_row is None
        or str(role_row[0]) != str(role_row[1])
        or role_row[2] is not True
        or any(value is True for value in role_row[3:9])
    ):
        raise AdmissionBackendUnavailable("admission_backend_role_elevated")

    cursor.execute(
        """
        SELECT has_database_privilege(
                   current_user,
                   database.oid,
                   'CONNECT'
               ),
               has_database_privilege(
                   current_user,
                   database.oid,
                   'CREATE'
               ),
               has_database_privilege(
                   current_user,
                   database.oid,
                   'TEMPORARY'
               ),
               pg_catalog.pg_has_role(
                   current_user,
                   database.datdba,
                   'MEMBER'
               )
        FROM pg_catalog.pg_database AS database
        WHERE database.datname = current_database()
        """
    )
    database_row = cursor.fetchone()
    if database_row is None or database_row[0] is not True:
        raise AdmissionBackendUnavailable(
            "admission_backend_database_authority_missing"
        )
    if any(value is True for value in database_row[1:4]):
        raise AdmissionBackendUnavailable(
            "admission_backend_database_authority_excess"
        )

    cursor.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM unnest(current_schemas(TRUE)) AS search_schema(schema_name)
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.nspname = search_schema.schema_name
            WHERE namespace.nspname <> 'pg_catalog'
              AND has_schema_privilege(
                  current_user,
                  namespace.oid,
                  'CREATE'
              )
        )
        """
    )
    writable_search_path_row = cursor.fetchone()
    if (
        writable_search_path_row is None
        or writable_search_path_row[0] is not False
    ):
        raise AdmissionBackendUnavailable(
            "admission_backend_search_path_authority_excess"
        )

    resolved_schemas: set[str] = set()
    all_relation_oids = (*write_relation_oids, capacity_relation_oid)
    for relation_oid in all_relation_oids:
        cursor.execute(
            """
            SELECT relation.relkind,
                   namespace.nspname,
                   relation.relrowsecurity,
                   relation.relforcerowsecurity,
                   pg_catalog.pg_has_role(
                       current_user,
                       relation.relowner,
                       'MEMBER'
                   ),
                   pg_catalog.pg_has_role(
                       current_user,
                       namespace.nspowner,
                       'MEMBER'
                   ),
                   has_schema_privilege(
                       current_user,
                       namespace.oid,
                       'USAGE'
                   ),
                   has_schema_privilege(
                       current_user,
                       namespace.oid,
                       'CREATE'
                   ),
                   EXISTS (
                       SELECT 1
                       FROM pg_catalog.pg_trigger AS trigger
                       WHERE trigger.tgrelid = relation.oid
                         AND NOT trigger.tgisinternal
                   )
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE relation.oid = %s
            """,
            (relation_oid,),
        )
        relation_row = cursor.fetchone()
        if (
            relation_row is None
            or str(relation_row[0]) != "r"
            or not str(relation_row[1] or "").strip()
            or str(relation_row[1]).startswith("pg_")
        ):
            raise AdmissionBackendUnavailable("admission_backend_schema_invalid")
        if relation_row[2] is True or relation_row[3] is True:
            raise AdmissionBackendUnavailable("admission_backend_rls_unsupported")
        if relation_row[4] is True or relation_row[5] is True:
            raise AdmissionBackendUnavailable(
                "admission_backend_ownership_authority_excess"
            )
        if relation_row[6] is not True:
            raise AdmissionBackendUnavailable(
                "admission_backend_schema_authority_missing"
            )
        if relation_row[7] is True:
            raise AdmissionBackendUnavailable(
                "admission_backend_schema_authority_excess"
            )
        resolved_schemas.add(str(relation_row[1]))
    if len(resolved_schemas) != 1:
        raise AdmissionBackendUnavailable("admission_backend_schema_split")

    _probe_strict_admission_capacity_contract(
        cursor,
        schema_name=next(iter(resolved_schemas)),
        write_relation_oids=write_relation_oids,
        capacity_relation_oid=capacity_relation_oid,
        capacity_owner_role=capacity_owner_role,
    )

    cursor.execute(
        """
        SELECT EXISTS (
                   SELECT 1
                   FROM pg_catalog.pg_class AS other_relation
                   JOIN pg_catalog.pg_namespace AS other_namespace
                     ON other_namespace.oid = other_relation.relnamespace
                   WHERE other_relation.oid <> ALL(%s::oid[])
                     AND other_namespace.nspname <> 'information_schema'
                     AND LEFT(other_namespace.nspname, 3) <> 'pg_'
                     AND (
                         (
                             other_relation.relkind IN ('r', 'p', 'v', 'm', 'f')
                             AND (
                                 has_table_privilege(
                                     current_user,
                                     other_relation.oid,
                                     'SELECT'
                                 )
                                 OR has_table_privilege(
                                     current_user,
                                     other_relation.oid,
                                     'INSERT'
                                 )
                                 OR has_table_privilege(
                                     current_user,
                                     other_relation.oid,
                                     'UPDATE'
                                 )
                                 OR has_table_privilege(
                                     current_user,
                                     other_relation.oid,
                                     'DELETE'
                                 )
                                 OR has_table_privilege(
                                     current_user,
                                     other_relation.oid,
                                     'TRUNCATE'
                                 )
                                 OR has_table_privilege(
                                     current_user,
                                     other_relation.oid,
                                     'REFERENCES'
                                 )
                                 OR has_table_privilege(
                                     current_user,
                                     other_relation.oid,
                                     'TRIGGER'
                                 )
                             )
                         )
                         OR (
                             other_relation.relkind = 'S'
                             AND (
                                 has_sequence_privilege(
                                     current_user,
                                     other_relation.oid,
                                     'USAGE'
                                 )
                                 OR has_sequence_privilege(
                                     current_user,
                                     other_relation.oid,
                                     'SELECT'
                                 )
                                 OR has_sequence_privilege(
                                     current_user,
                                     other_relation.oid,
                                     'UPDATE'
                                 )
                             )
                         )
                     )
               ),
               EXISTS (
                   SELECT 1
                   FROM pg_catalog.pg_proc AS function
                   JOIN pg_catalog.pg_namespace AS function_namespace
                     ON function_namespace.oid = function.pronamespace
                   WHERE function.prosecdef
                     AND function_namespace.nspname <> 'information_schema'
                     AND LEFT(function_namespace.nspname, 3) <> 'pg_'
                     AND has_function_privilege(
                         current_user,
                         function.oid,
                         'EXECUTE'
                     )
               )
        """,
        (list(all_relation_oids),),
    )
    cross_surface_row = cursor.fetchone()
    if (
        cross_surface_row is None
        or cross_surface_row[0] is not False
        or cross_surface_row[1] is not False
    ):
        raise AdmissionBackendUnavailable(
            "admission_backend_cross_surface_authority_excess"
        )


def probe_admission_cursor(
    cursor: Any,
    *,
    require_least_privilege: bool = True,
    capacity_owner_role: str = ADMISSION_CAPACITY_OWNER_ROLE_DEFAULT,
    lock_timeout_ms: int = _DEFAULT_LOCK_TIMEOUT_MS,
    statement_timeout_ms: int = _DEFAULT_STATEMENT_TIMEOUT_MS,
    idle_transaction_timeout_ms: int = _DEFAULT_IDLE_TRANSACTION_TIMEOUT_MS,
) -> None:
    """Verify the closed admission schema and transactional write authority.

    The probe intentionally performs no DML: explicit privilege checks plus
    ``FOR UPDATE`` validate the write transaction boundary without charging a
    quota, creating a lease, or leaving rolled-back tuple churn behind. In
    production mode it additionally rejects credentials capable of escaping
    the two-write-table plus read-only-capacity boundary. An autocommit caller
    is wrapped in an explicit read-only-effect transaction so every probe
    statement shares the local timeout policy.
    """

    configured_lock_timeout = _configured_int(
        lock_timeout_ms,
        minimum=_MIN_TIMEOUT_MS,
        maximum=_MAX_LOCK_TIMEOUT_MS,
        error_code="propertyquarry_admission_lock_timeout_invalid",
    )
    configured_statement_timeout = _configured_int(
        statement_timeout_ms,
        minimum=_MIN_TIMEOUT_MS,
        maximum=_MAX_STATEMENT_TIMEOUT_MS,
        error_code="propertyquarry_admission_statement_timeout_invalid",
    )
    configured_idle_timeout = _configured_int(
        idle_transaction_timeout_ms,
        minimum=_MIN_TIMEOUT_MS,
        maximum=_MAX_IDLE_TRANSACTION_TIMEOUT_MS,
        error_code="propertyquarry_admission_idle_transaction_timeout_invalid",
    )
    if configured_lock_timeout > configured_statement_timeout:
        raise RuntimeError("propertyquarry_admission_timeout_order_invalid")
    configured_capacity_owner_role = normalize_admission_capacity_owner_role(
        capacity_owner_role
    )

    connection = getattr(cursor, "connection", None)
    owns_transaction = bool(getattr(connection, "autocommit", False))
    if owns_transaction:
        cursor.execute("BEGIN")
    probe_succeeded = False
    try:
        _configure_admission_transaction(
            cursor,
            lock_timeout_ms=configured_lock_timeout,
            statement_timeout_ms=configured_statement_timeout,
            idle_transaction_timeout_ms=configured_idle_timeout,
        )
        cursor.execute(
            "SELECT to_regclass(%s)::oid, to_regclass(%s)::oid",
            (_QUOTA_TABLE, _LEASE_TABLE),
        )
        row = cursor.fetchone()
        if row is None or row[0] is None or row[1] is None:
            raise AdmissionBackendUnavailable("admission_backend_schema_missing")
        relation_oids = (row[0], row[1])
        if require_least_privilege:
            cursor.execute(
                "SELECT to_regclass(%s)::oid",
                (ADMISSION_CAPACITY_STATE_TABLE,),
            )
            capacity_row = cursor.fetchone()
            if capacity_row is None or capacity_row[0] is None:
                raise AdmissionBackendUnavailable(
                    "admission_backend_capacity_schema_missing"
                )
            _probe_strict_admission_authority(
                cursor,
                write_relation_oids=relation_oids,
                capacity_relation_oid=capacity_row[0],
                capacity_owner_role=configured_capacity_owner_role,
            )
        for relation_oid in relation_oids:
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                # PostgreSQL treats a comma-separated privilege request as
                # "any of", so verify every operation needed by admission DML
                # independently.
                cursor.execute(
                    "SELECT has_table_privilege(current_user, %s, %s)",
                    (relation_oid, privilege),
                )
                privilege_row = cursor.fetchone()
                if privilege_row is None or privilege_row[0] is not True:
                    raise AdmissionBackendUnavailable(
                        "admission_backend_write_authority_missing"
                    )
            if require_least_privilege:
                for privilege in ("TRUNCATE", "REFERENCES", "TRIGGER"):
                    cursor.execute(
                        "SELECT has_table_privilege(current_user, %s, %s)",
                        (relation_oid, privilege),
                    )
                    privilege_row = cursor.fetchone()
                    if privilege_row is None or privilege_row[0] is not False:
                        raise AdmissionBackendUnavailable(
                            "admission_backend_table_authority_excess"
                        )
        cursor.execute(
            f"""
            SELECT bucket_key,
                   window_index,
                   window_seconds,
                   used_units,
                   limit_units,
                   expires_at,
                   updated_at
            FROM {_QUOTA_TABLE}
            ORDER BY bucket_key
            LIMIT 1
            FOR UPDATE
            """
        )
        cursor.fetchone()
        cursor.execute(
            f"""
            SELECT lease_id,
                   dimension_key,
                   limit_units,
                   acquired_at,
                   expires_at
            FROM {_LEASE_TABLE}
            ORDER BY lease_id, dimension_key
            LIMIT 1
            FOR UPDATE
            """
        )
        cursor.fetchone()
        probe_succeeded = True
    finally:
        if owns_transaction:
            try:
                cursor.execute("ROLLBACK")
            except Exception as exc:
                if probe_succeeded:
                    raise AdmissionBackendUnavailable(
                        "admission_backend_probe_rollback_failed"
                    ) from exc
                # Preserve the authoritative probe failure; the owning
                # autocommit connection will discard any failed transaction
                # when its surrounding context closes.


class _ConnectionPool:
    def __init__(
        self,
        *,
        database_url: str,
        connect: Callable[..., Any] | None,
        max_size: int,
        acquire_timeout_seconds: float,
    ) -> None:
        self._database_url = database_url
        self._connect_override = connect
        self._max_size = max(1, min(32, int(max_size)))
        self._acquire_timeout_seconds = max(0.1, min(10.0, float(acquire_timeout_seconds)))
        self._idle: LifoQueue[Any] = LifoQueue(maxsize=self._max_size)
        self._lock = threading.Lock()
        self._created = 0

    def _reserve_connection(self) -> bool:
        with self._lock:
            if self._created >= self._max_size:
                return False
            self._created += 1
            return True

    def _discard(self, connection: Any | None) -> None:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        with self._lock:
            self._created = max(0, self._created - 1)

    def _connect(self) -> Any:
        try:
            if self._connect_override is not None:
                return self._connect_override(self._database_url, autocommit=False)
            import psycopg

            return psycopg.connect(
                self._database_url,
                autocommit=False,
                connect_timeout=5,
                application_name="propertyquarry-admission",
            )
        except Exception as exc:
            raise AdmissionBackendUnavailable("admission_backend_connect_failed") from exc

    @contextmanager
    def connection(self):
        connection: Any | None = None
        reserved = False
        try:
            try:
                connection = self._idle.get_nowait()
            except Empty:
                reserved = self._reserve_connection()
                if reserved:
                    try:
                        connection = self._connect()
                    except Exception:
                        self._discard(None)
                        raise
                else:
                    try:
                        connection = self._idle.get(timeout=self._acquire_timeout_seconds)
                    except Empty as exc:
                        raise AdmissionBackendUnavailable(
                            "admission_backend_pool_exhausted"
                        ) from exc
            if connection is None or bool(getattr(connection, "closed", False)):
                self._discard(connection)
                connection = None
                raise AdmissionBackendUnavailable("admission_backend_connection_closed")
            yield connection
        except AdmissionBackendUnavailable:
            if connection is not None:
                self._discard(connection)
                connection = None
            raise
        except Exception as exc:
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    pass
                self._discard(connection)
                connection = None
            raise AdmissionBackendUnavailable("admission_backend_operation_failed") from exc
        finally:
            if connection is not None:
                try:
                    connection.rollback()
                    self._idle.put_nowait(connection)
                except (Exception, Full):
                    self._discard(connection)


class PostgresAdmissionBackend:
    """Atomic replica-safe quota and concurrency decisions backed by PostgreSQL."""

    backend_name = "postgres"

    def __init__(
        self,
        database_url: str,
        *,
        connect: Callable[..., Any] | None = None,
        pool_size: int = 8,
        pool_acquire_timeout_seconds: float = 2.0,
        cleanup_batch: int = 64,
        require_least_privilege: bool = False,
        capacity_owner_role: str = ADMISSION_CAPACITY_OWNER_ROLE_DEFAULT,
        lock_timeout_ms: int = _DEFAULT_LOCK_TIMEOUT_MS,
        statement_timeout_ms: int = _DEFAULT_STATEMENT_TIMEOUT_MS,
        idle_transaction_timeout_ms: int = _DEFAULT_IDLE_TRANSACTION_TIMEOUT_MS,
    ) -> None:
        normalized_url = str(database_url or "").strip()
        if not normalized_url:
            raise RuntimeError("propertyquarry_admission_database_url_required")
        self._cleanup_batch = max(1, min(1_024, int(cleanup_batch)))
        self._require_least_privilege = bool(require_least_privilege)
        self._capacity_owner_role = normalize_admission_capacity_owner_role(
            capacity_owner_role
        )
        self._lock_timeout_ms = _configured_int(
            lock_timeout_ms,
            minimum=_MIN_TIMEOUT_MS,
            maximum=_MAX_LOCK_TIMEOUT_MS,
            error_code="propertyquarry_admission_lock_timeout_invalid",
        )
        self._statement_timeout_ms = _configured_int(
            statement_timeout_ms,
            minimum=_MIN_TIMEOUT_MS,
            maximum=_MAX_STATEMENT_TIMEOUT_MS,
            error_code="propertyquarry_admission_statement_timeout_invalid",
        )
        self._idle_transaction_timeout_ms = _configured_int(
            idle_transaction_timeout_ms,
            minimum=_MIN_TIMEOUT_MS,
            maximum=_MAX_IDLE_TRANSACTION_TIMEOUT_MS,
            error_code="propertyquarry_admission_idle_transaction_timeout_invalid",
        )
        if self._lock_timeout_ms > self._statement_timeout_ms:
            raise RuntimeError("propertyquarry_admission_timeout_order_invalid")
        self._pool = _ConnectionPool(
            database_url=normalized_url,
            connect=connect,
            max_size=pool_size,
            acquire_timeout_seconds=pool_acquire_timeout_seconds,
        )

    def _configure_transaction(self, cursor: Any) -> None:
        _configure_admission_transaction(
            cursor,
            lock_timeout_ms=self._lock_timeout_ms,
            statement_timeout_ms=self._statement_timeout_ms,
            idle_transaction_timeout_ms=self._idle_transaction_timeout_ms,
        )

    @staticmethod
    def _advisory_lock(cursor: Any, *, prefix: str, key: str) -> None:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"propertyquarry_admission:{prefix}:{key}",),
        )

    def _cleanup_quota(self, cursor: Any) -> None:
        cursor.execute(
            f"""
            WITH expired AS (
                SELECT bucket_key
                FROM {_QUOTA_TABLE}
                WHERE expires_at <= clock_timestamp()
                ORDER BY expires_at, bucket_key
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            DELETE FROM {_QUOTA_TABLE} AS target
            USING expired
            WHERE target.bucket_key = expired.bucket_key
              AND target.expires_at <= clock_timestamp()
            """,
            (self._cleanup_batch,),
        )

    def _cleanup_leases(self, cursor: Any) -> None:
        cursor.execute(
            f"""
            WITH expired AS (
                SELECT lease_id, dimension_key
                FROM {_LEASE_TABLE}
                WHERE expires_at <= clock_timestamp()
                ORDER BY expires_at, lease_id, dimension_key
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            DELETE FROM {_LEASE_TABLE} AS target
            USING expired
            WHERE target.lease_id = expired.lease_id
              AND target.dimension_key = expired.dimension_key
              AND target.expires_at <= clock_timestamp()
            """,
            (self._cleanup_batch,),
        )

    def consume(self, charge: QuotaCharge) -> AdmissionDecision:
        return self.consume_many((charge,))

    def consume_many(self, charges: Sequence[QuotaCharge]) -> AdmissionDecision:
        if len(charges) > _MAX_QUOTA_CHARGES:
            raise AdmissionBackendUnavailable("admission_quota_too_many_charges")
        normalized = tuple(_normalized_charge(charge) for charge in charges)
        if not normalized:
            raise AdmissionBackendUnavailable("admission_quota_empty")
        keys = [charge.key for charge in normalized]
        if len(set(keys)) != len(keys):
            raise AdmissionBackendUnavailable("admission_quota_key_duplicate")
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                self._configure_transaction(cursor)
                for charge in sorted(normalized, key=lambda item: item.key):
                    self._advisory_lock(cursor, prefix="quota", key=charge.key)
                cursor.execute("SELECT EXTRACT(EPOCH FROM clock_timestamp())")
                now_epoch = float(cursor.fetchone()[0])
                projected: list[
                    tuple[QuotaCharge, int, int, int, int, int, float]
                ] = []
                for charge in normalized:
                    window = int(now_epoch // charge.window_seconds)
                    retry_after = max(
                        1,
                        int(
                            math.ceil(
                                ((window + 1) * charge.window_seconds) - now_epoch
                            )
                        ),
                    )
                    cursor.execute(
                        f"""
                        SELECT window_index,
                               window_seconds,
                               used_units,
                               limit_units,
                               EXTRACT(EPOCH FROM expires_at)
                        FROM {_QUOTA_TABLE}
                        WHERE bucket_key = %s
                        FOR UPDATE
                        """,
                        (charge.key,),
                    )
                    row = cursor.fetchone()
                    used = 0
                    effective_limit = charge.limit
                    effective_window = window
                    effective_window_seconds = charge.window_seconds
                    expires_epoch = (window + 1) * charge.window_seconds
                    if row is not None:
                        previous_window = int(row[0])
                        previous_seconds = int(row[1])
                        previous_expiry = float(row[4])
                        if now_epoch < previous_expiry:
                            effective_window = previous_window
                            effective_window_seconds = previous_seconds
                            used = int(row[2])
                            effective_limit = min(int(row[3]), charge.limit)
                            expires_epoch = previous_expiry
                            retry_after = max(
                                1,
                                int(math.ceil(previous_expiry - now_epoch)),
                            )
                    if used + charge.units > effective_limit:
                        if row is not None and effective_limit < int(row[3]):
                            cursor.execute(
                                f"""
                                UPDATE {_QUOTA_TABLE}
                                SET limit_units = %s,
                                    updated_at = clock_timestamp()
                                WHERE bucket_key = %s
                                  AND expires_at > clock_timestamp()
                                """,
                                (effective_limit, charge.key),
                            )
                        self._cleanup_quota(cursor)
                        connection.commit()
                        return AdmissionDecision(
                            False,
                            retry_after_seconds=retry_after,
                            dimension=charge.dimension,
                        )
                    projected.append(
                        (
                            charge,
                            effective_window,
                            used + charge.units,
                            retry_after,
                            effective_window_seconds,
                            effective_limit,
                            expires_epoch,
                        )
                    )
                for (
                    charge,
                    window,
                    used,
                    _retry_after,
                    window_seconds,
                    effective_limit,
                    expires_epoch,
                ) in projected:
                    cursor.execute(
                        f"""
                        INSERT INTO {_QUOTA_TABLE} (
                            bucket_key,
                            window_index,
                            window_seconds,
                            used_units,
                            limit_units,
                            expires_at,
                            updated_at
                        ) VALUES (%s, %s, %s, %s, %s, to_timestamp(%s), clock_timestamp())
                        ON CONFLICT (bucket_key) DO UPDATE SET
                            window_index = EXCLUDED.window_index,
                            window_seconds = EXCLUDED.window_seconds,
                            used_units = EXCLUDED.used_units,
                            limit_units = EXCLUDED.limit_units,
                            expires_at = EXCLUDED.expires_at,
                            updated_at = EXCLUDED.updated_at
                        """,
                        (
                            charge.key,
                            window,
                            window_seconds,
                            used,
                            effective_limit,
                            expires_epoch,
                        ),
                    )
                self._cleanup_quota(cursor)
                connection.commit()
                return AdmissionDecision(
                    True,
                    retry_after_seconds=min(item[3] for item in projected),
                )

    def acquire(
        self,
        dimensions: Sequence[ConcurrencyDimension],
        *,
        lease_seconds: int,
    ) -> AdmissionDecision:
        normalized = _normalized_dimensions(dimensions)
        ttl = _normalized_lease_seconds(lease_seconds)
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                self._configure_transaction(cursor)
                for dimension in normalized:
                    self._advisory_lock(cursor, prefix="lease", key=dimension.key)
                self._cleanup_leases(cursor)
                effective_limits: dict[str, int] = {}
                for dimension in normalized:
                    cursor.execute(
                        f"""
                        SELECT COUNT(*),
                               MIN(limit_units),
                               COALESCE(
                                   CEIL(EXTRACT(EPOCH FROM MIN(expires_at) - clock_timestamp())),
                                   1
                               )
                        FROM {_LEASE_TABLE}
                        WHERE dimension_key = %s
                          AND expires_at > clock_timestamp()
                        """,
                        (dimension.key,),
                    )
                    row = cursor.fetchone()
                    active_count = int(row[0] or 0)
                    active_limit = int(row[1] or dimension.limit)
                    effective_limit = min(active_limit, dimension.limit)
                    effective_limits[dimension.key] = effective_limit
                    if active_count > 0 and effective_limit < active_limit:
                        cursor.execute(
                            f"""
                            UPDATE {_LEASE_TABLE}
                            SET limit_units = LEAST(limit_units, %s)
                            WHERE dimension_key = %s
                              AND expires_at > clock_timestamp()
                            """,
                            (effective_limit, dimension.key),
                        )
                    if active_count >= effective_limit:
                        # Commit a stricter observed live policy even though no
                        # new lease is admitted. Otherwise a lax rolling replica
                        # could immediately exceed the newly observed bound.
                        connection.commit()
                        return AdmissionDecision(
                            False,
                            retry_after_seconds=max(1, int(row[2] or 1)),
                            dimension=dimension.dimension,
                        )
                lease_id = str(uuid4())
                for dimension in normalized:
                    cursor.execute(
                        f"""
                        INSERT INTO {_LEASE_TABLE} (
                            lease_id,
                            dimension_key,
                            limit_units,
                            acquired_at,
                            expires_at
                        ) VALUES (
                            %s::uuid,
                            %s,
                            %s,
                            clock_timestamp(),
                            clock_timestamp() + make_interval(secs => %s)
                        )
                        """,
                        (
                            lease_id,
                            dimension.key,
                            effective_limits[dimension.key],
                            ttl,
                        ),
                    )
                connection.commit()
                return AdmissionDecision(True, lease_id=lease_id)

    def release(self, lease_id: str) -> None:
        normalized = str(lease_id or "").strip()
        if not normalized:
            return
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                self._configure_transaction(cursor)
                cursor.execute(
                    f"DELETE FROM {_LEASE_TABLE} WHERE lease_id = %s::uuid",
                    (normalized,),
                )
                self._cleanup_leases(cursor)
                connection.commit()

    def renew(self, lease_id: str, *, lease_seconds: int) -> bool:
        normalized = str(lease_id or "").strip()
        if not normalized:
            return False
        ttl = _normalized_lease_seconds(lease_seconds)
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                self._configure_transaction(cursor)
                cursor.execute(
                    f"""
                    SELECT expires_at > clock_timestamp()
                    FROM {_LEASE_TABLE}
                    WHERE lease_id = %s::uuid
                    ORDER BY dimension_key
                    FOR UPDATE
                    """,
                    (normalized,),
                )
                rows = cursor.fetchall()
                if not rows or not all(bool(row[0]) for row in rows):
                    if rows:
                        cursor.execute(
                            f"DELETE FROM {_LEASE_TABLE} WHERE lease_id = %s::uuid",
                            (normalized,),
                        )
                    self._cleanup_leases(cursor)
                    connection.commit()
                    return False
                cursor.execute(
                    f"""
                    UPDATE {_LEASE_TABLE}
                    SET expires_at = clock_timestamp() + make_interval(secs => %s)
                    WHERE lease_id = %s::uuid
                    """,
                    (ttl, normalized),
                )
                self._cleanup_leases(cursor)
                connection.commit()
                return True

    def capacity_snapshot(self) -> tuple[tuple[str, int, int], ...]:
        """Read the exact shared v17 capacity state without taking DML authority."""

        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                self._configure_transaction(cursor)
                cursor.execute(
                    f"""
                    SELECT capacity_key, row_count, row_limit
                    FROM {ADMISSION_CAPACITY_STATE_TABLE}
                    ORDER BY capacity_key
                    """
                )
                return _validated_admission_capacity_rows(cursor.fetchall())

    def probe(self) -> None:
        with self._pool.connection() as connection:
            with connection.cursor() as cursor:
                probe_admission_cursor(
                    cursor,
                    require_least_privilege=self._require_least_privilege,
                    capacity_owner_role=self._capacity_owner_role,
                    lock_timeout_ms=self._lock_timeout_ms,
                    statement_timeout_ms=self._statement_timeout_ms,
                    idle_transaction_timeout_ms=self._idle_transaction_timeout_ms,
                )
                connection.rollback()


def resolve_api_admission_database_url(
    *,
    runtime_mode: str,
    primary_database_url: str,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve the API admission store without silently reusing app authority.

    Production requires a separately named DSN whose role is verified by the
    strict admission probe. Development and tests keep the process-local
    backend by default; reusing ``DATABASE_URL`` for an explicitly selected
    PostgreSQL backend requires a separate, conspicuous opt-in.
    """

    env = environ if environ is not None else os.environ
    mode = str(runtime_mode or "").strip().lower()
    primary_url = str(primary_database_url or "").strip()
    dedicated_url = str(
        env.get("PROPERTYQUARRY_API_ADMISSION_DATABASE_URL") or ""
    ).strip()
    backend_name = str(
        env.get("PROPERTYQUARRY_ADMISSION_BACKEND") or ""
    ).strip().lower()
    if not backend_name:
        backend_name = "memory" if mode in {"dev", "test"} else "postgres"

    if mode not in {"dev", "test"}:
        if not dedicated_url:
            raise RuntimeError(
                "propertyquarry_api_admission_database_url_required"
            )
        if primary_url and dedicated_url == primary_url:
            raise RuntimeError(
                "propertyquarry_api_admission_database_url_must_be_dedicated"
            )
        return dedicated_url

    if dedicated_url:
        return dedicated_url
    if backend_name != "postgres":
        return ""

    fallback_raw = str(
        env.get("PROPERTYQUARRY_DEV_ALLOW_PRIMARY_ADMISSION_DATABASE_URL")
        or ""
    ).strip().lower()
    if fallback_raw in {"1", "true", "yes", "on"} and primary_url:
        return primary_url
    if fallback_raw and fallback_raw not in {
        "0",
        "false",
        "no",
        "off",
    }:
        raise RuntimeError(
            "propertyquarry_dev_allow_primary_admission_database_url_invalid"
        )
    raise RuntimeError("propertyquarry_api_admission_database_url_required")


def resolve_api_ingress_database_url(
    *,
    runtime_mode: str,
    primary_database_url: str,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve public-ingress accounting without sharing another authority.

    The API's internal admission backend and its internet-facing ingress
    accounting use different schemas and different roles.  Production must
    provide the ingress-specific DSN and may not reuse either the primary
    application DSN or the internal admission DSN.
    """

    env = environ if environ is not None else os.environ
    mode = str(runtime_mode or "").strip().lower()
    primary_url = str(primary_database_url or "").strip()
    dedicated_url = str(
        env.get("PROPERTYQUARRY_API_INGRESS_DATABASE_URL") or ""
    ).strip()
    internal_admission_url = str(
        env.get("PROPERTYQUARRY_API_ADMISSION_DATABASE_URL") or ""
    ).strip()

    if mode not in {"dev", "test"}:
        if not dedicated_url:
            raise RuntimeError(
                "propertyquarry_api_ingress_database_url_required"
            )
        if primary_url and dedicated_url == primary_url:
            raise RuntimeError(
                "propertyquarry_api_ingress_database_url_must_be_dedicated"
            )
        if internal_admission_url and dedicated_url == internal_admission_url:
            raise RuntimeError(
                "propertyquarry_api_ingress_database_url_must_be_role_separated"
            )
        return dedicated_url

    if dedicated_url:
        return dedicated_url
    return primary_url


def build_admission_backend(
    *,
    runtime_mode: str,
    database_url: str,
    environ: Mapping[str, str] | None = None,
    memory_clock: Callable[[], float] = time.time,
) -> AdmissionBackend:
    env = environ if environ is not None else os.environ
    mode = str(runtime_mode or "").strip().lower()
    backend_name = str(env.get("PROPERTYQUARRY_ADMISSION_BACKEND") or "").strip().lower()
    if not backend_name:
        backend_name = "memory" if mode in {"dev", "test"} else "postgres"
    if backend_name == "memory":
        if mode not in {"dev", "test"}:
            raise RuntimeError("propertyquarry_admission_memory_backend_forbidden")
        return MemoryAdmissionBackend(clock=memory_clock)
    if backend_name != "postgres":
        raise RuntimeError("propertyquarry_admission_backend_invalid")
    normalized_url = str(database_url or "").strip()
    if not normalized_url:
        raise RuntimeError("propertyquarry_admission_database_url_required")
    pool_size_raw = str(env.get("PROPERTYQUARRY_ADMISSION_POOL_SIZE") or "8").strip()
    try:
        pool_size = int(pool_size_raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("propertyquarry_admission_pool_size_invalid") from exc
    if pool_size < 1 or pool_size > 32:
        raise RuntimeError("propertyquarry_admission_pool_size_out_of_range")
    lock_timeout_ms = _configured_int(
        str(
            env.get("PROPERTYQUARRY_ADMISSION_LOCK_TIMEOUT_MS")
            or _DEFAULT_LOCK_TIMEOUT_MS
        ).strip(),
        minimum=_MIN_TIMEOUT_MS,
        maximum=_MAX_LOCK_TIMEOUT_MS,
        error_code="propertyquarry_admission_lock_timeout_invalid",
    )
    statement_timeout_ms = _configured_int(
        str(
            env.get("PROPERTYQUARRY_ADMISSION_STATEMENT_TIMEOUT_MS")
            or _DEFAULT_STATEMENT_TIMEOUT_MS
        ).strip(),
        minimum=_MIN_TIMEOUT_MS,
        maximum=_MAX_STATEMENT_TIMEOUT_MS,
        error_code="propertyquarry_admission_statement_timeout_invalid",
    )
    idle_transaction_timeout_ms = _configured_int(
        str(
            env.get("PROPERTYQUARRY_ADMISSION_IDLE_TRANSACTION_TIMEOUT_MS")
            or _DEFAULT_IDLE_TRANSACTION_TIMEOUT_MS
        ).strip(),
        minimum=_MIN_TIMEOUT_MS,
        maximum=_MAX_IDLE_TRANSACTION_TIMEOUT_MS,
        error_code="propertyquarry_admission_idle_transaction_timeout_invalid",
    )
    if lock_timeout_ms > statement_timeout_ms:
        raise RuntimeError("propertyquarry_admission_timeout_order_invalid")
    capacity_owner_role = normalize_admission_capacity_owner_role(
        env.get("PROPERTYQUARRY_ADMISSION_CAPACITY_OWNER_ROLE")
        or ADMISSION_CAPACITY_OWNER_ROLE_DEFAULT
    )
    return PostgresAdmissionBackend(
        normalized_url,
        pool_size=pool_size,
        require_least_privilege=mode not in {"dev", "test"},
        capacity_owner_role=capacity_owner_role,
        lock_timeout_ms=lock_timeout_ms,
        statement_timeout_ms=statement_timeout_ms,
        idle_transaction_timeout_ms=idle_transaction_timeout_ms,
    )
