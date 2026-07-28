"""Durable, fail-closed admission accounting for public ingress.

This module deliberately contains no ASGI wiring.  It defines the small,
closed contract shared by the in-memory development implementation and the
PostgreSQL production implementation.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterator, Protocol, Sequence, runtime_checkable
from uuid import UUID, uuid4

INGRESS_ADMISSION_CONTRACT_VERSION = 1
INGRESS_ADMISSION_QUOTA_HARD_LIMIT = 1_000_000
INGRESS_ADMISSION_LEASE_HARD_LIMIT = 100_000
INGRESS_ADMISSION_MAX_WINDOW_SECONDS = 2_678_400
INGRESS_ADMISSION_MAX_LEASE_SECONDS = 3_600
INGRESS_ADMISSION_MAX_CLEANUP_BATCH = 10_000

_INT64_MAX = (1 << 63) - 1
_MAX_SUBJECT_BYTES = 1_024
_HMAC_PREFIX = "hmac-sha256:"
_HMAC_DOMAIN = b"propertyquarry:ingress-admission:v1\x00"
_ADVISORY_LOCK_SEED = 1_226_920_241
_CLEANUP_LOCK_MATERIAL = "propertyquarry:ingress-admission:v1:cleanup"
_SCHEMA_SQLSTATES = frozenset({"3F000", "42P01", "42703", "42883"})


class AdmissionBackend(str, Enum):
    MEMORY = "memory"
    POSTGRES = "postgres"


class AdmissionDimension(str, Enum):
    IP = "ip"
    ACCOUNT = "account"


class QuotaKind(str, Enum):
    REQUEST = "request"
    COST = "cost"


class AdmissionOperation(str, Enum):
    IP_REQUEST = "ip_request"
    ADMIT = "admit"
    RENEW = "renew"
    RELEASE = "release"
    CLEANUP = "cleanup"
    SNAPSHOT = "snapshot"


class AdmissionOutcome(str, Enum):
    ALLOWED = "allowed"
    QUOTA_LIMITED = "quota_limited"
    LEASE_LIMITED = "lease_limited"
    CAPACITY_EXHAUSTED = "capacity_exhausted"
    BACKEND_UNAVAILABLE = "backend_unavailable"


class AdmissionCapacityKey(str, Enum):
    QUOTA = "quota"
    LEASE = "lease"


_CAPACITY_ORDER = (
    AdmissionCapacityKey.QUOTA,
    AdmissionCapacityKey.LEASE,
)
_CAPACITY_LIMITS = {
    AdmissionCapacityKey.QUOTA: INGRESS_ADMISSION_QUOTA_HARD_LIMIT,
    AdmissionCapacityKey.LEASE: INGRESS_ADMISSION_LEASE_HARD_LIMIT,
}


class IngressAdmissionError(RuntimeError):
    """Base class for bounded admission backend failures."""

    def __init__(
        self,
        code: str,
        *,
        backend: AdmissionBackend,
        operation: AdmissionOperation,
        outcome: AdmissionOutcome = AdmissionOutcome.BACKEND_UNAVAILABLE,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.backend = backend
        self.operation = operation
        self.outcome = outcome


class IngressAdmissionConfigurationError(IngressAdmissionError):
    """The store was constructed with an unsafe or incomplete configuration."""


class IngressAdmissionContractError(IngressAdmissionError):
    """The deployed database does not satisfy the admission schema contract."""


class IngressAdmissionUnavailable(IngressAdmissionError):
    """The backend could not complete within its bounded resource envelope."""


def _require_int(
    value: object,
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name}_must_be_an_integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{name}_out_of_range")
    return value


def _validated_subject(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("admission_subject_required")
    if value != value.strip():
        raise ValueError("admission_subject_not_canonical")
    encoded = value.encode("utf-8")
    if len(encoded) > _MAX_SUBJECT_BYTES:
        raise ValueError("admission_subject_too_long")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("admission_subject_contains_control_character")
    return value


def _validated_secret(value: bytes | str) -> bytes:
    if isinstance(value, str):
        encoded = value.encode("utf-8")
    elif isinstance(value, bytes):
        encoded = bytes(value)
    else:
        raise ValueError("ingress_admission_hmac_secret_type_invalid")
    if len(encoded) < 32:
        raise ValueError("ingress_admission_hmac_secret_too_short")
    if len(encoded) > 4_096:
        raise ValueError("ingress_admission_hmac_secret_too_long")
    return encoded


def _validated_key_id(value: object) -> str:
    normalized = str(value or "").strip()
    if (
        len(normalized) != 64
        or normalized != normalized.lower()
        or any(character not in "0123456789abcdef" for character in normalized)
    ):
        raise ValueError("ingress_admission_erasure_key_id_invalid")
    return normalized


def _subject_digest(
    secret: bytes,
    *,
    purpose: str,
    dimension: AdmissionDimension,
    subject: str,
) -> str:
    message = (
        _HMAC_DOMAIN
        + purpose.encode("ascii")
        + b"\x00"
        + dimension.value.encode("ascii")
        + b"\x00"
        + subject.encode("utf-8")
    )
    return _HMAC_PREFIX + hmac.new(secret, message, hashlib.sha256).hexdigest()


def _canonical_token(value: object) -> str:
    try:
        return UUID(str(value)).hex
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("ingress_admission_lease_token_invalid") from exc


@dataclass(frozen=True, slots=True)
class QuotaCharge:
    kind: QuotaKind
    dimension: AdmissionDimension
    subject: str = field(repr=False)
    units: int
    limit: int
    window_seconds: int

    def __post_init__(self) -> None:
        if not isinstance(self.kind, QuotaKind):
            raise ValueError("quota_kind_invalid")
        if not isinstance(self.dimension, AdmissionDimension):
            raise ValueError("admission_dimension_invalid")
        _validated_subject(self.subject)
        _require_int(self.units, "quota_units", minimum=1, maximum=_INT64_MAX)
        _require_int(self.limit, "quota_limit", minimum=1, maximum=_INT64_MAX)
        _require_int(
            self.window_seconds,
            "quota_window_seconds",
            minimum=1,
            maximum=INGRESS_ADMISSION_MAX_WINDOW_SECONDS,
        )
        if self.units > self.limit:
            raise ValueError("quota_units_exceed_limit")


@dataclass(frozen=True, slots=True)
class LeaseScope:
    dimension: AdmissionDimension
    subject: str = field(repr=False)
    limit: int

    def __post_init__(self) -> None:
        if not isinstance(self.dimension, AdmissionDimension):
            raise ValueError("admission_dimension_invalid")
        _validated_subject(self.subject)
        _require_int(self.limit, "lease_limit", minimum=1, maximum=_INT64_MAX)


@dataclass(frozen=True, slots=True)
class AdmissionRequest:
    quota_charges: tuple[QuotaCharge, ...]
    lease_scopes: tuple[LeaseScope, ...] = ()
    lease_seconds: int = 30

    def __post_init__(self) -> None:
        if not isinstance(self.quota_charges, tuple):
            raise ValueError("quota_charges_must_be_a_tuple")
        if not isinstance(self.lease_scopes, tuple):
            raise ValueError("lease_scopes_must_be_a_tuple")
        if len(self.quota_charges) > 3:
            raise ValueError("too_many_quota_charges")
        if len(self.lease_scopes) > 2:
            raise ValueError("too_many_lease_scopes")
        if not self.quota_charges and not self.lease_scopes:
            raise ValueError("empty_admission_request")
        if any(not isinstance(charge, QuotaCharge) for charge in self.quota_charges):
            raise ValueError("quota_charge_invalid")
        if any(not isinstance(scope, LeaseScope) for scope in self.lease_scopes):
            raise ValueError("lease_scope_invalid")
        _require_int(
            self.lease_seconds,
            "lease_seconds",
            minimum=1,
            maximum=INGRESS_ADMISSION_MAX_LEASE_SECONDS,
        )

        quota_identities = {
            (charge.kind, charge.dimension, charge.subject)
            for charge in self.quota_charges
        }
        if len(quota_identities) != len(self.quota_charges):
            raise ValueError("duplicate_quota_charge")
        lease_dimensions = {scope.dimension for scope in self.lease_scopes}
        if len(lease_dimensions) != len(self.lease_scopes):
            raise ValueError("duplicate_lease_dimension")
        if self.lease_scopes and AdmissionDimension.IP not in lease_dimensions:
            raise ValueError("ip_lease_scope_required")


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    backend: AdmissionBackend
    operation: AdmissionOperation
    outcome: AdmissionOutcome
    allowed: bool
    retry_after_seconds: int = 0
    rejected_dimension: AdmissionDimension | None = None
    rejected_kind: QuotaKind | None = None
    capacity_key: AdmissionCapacityKey | None = None
    lease_token: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.backend, AdmissionBackend):
            raise ValueError("admission_backend_invalid")
        if self.operation not in {
            AdmissionOperation.IP_REQUEST,
            AdmissionOperation.ADMIT,
        }:
            raise ValueError("admission_result_operation_invalid")
        if not isinstance(self.outcome, AdmissionOutcome):
            raise ValueError("admission_outcome_invalid")
        if not isinstance(self.allowed, bool):
            raise ValueError("admission_allowed_invalid")
        _require_int(
            self.retry_after_seconds,
            "retry_after_seconds",
            minimum=0,
            maximum=INGRESS_ADMISSION_MAX_WINDOW_SECONDS,
        )
        if self.allowed != (self.outcome is AdmissionOutcome.ALLOWED):
            raise ValueError("admission_result_outcome_inconsistent")
        if self.allowed:
            if (
                self.retry_after_seconds
                or self.rejected_dimension is not None
                or self.rejected_kind is not None
                or self.capacity_key is not None
            ):
                raise ValueError("allowed_admission_contains_rejection")
        elif self.outcome is AdmissionOutcome.QUOTA_LIMITED:
            if (
                self.rejected_dimension is None
                or self.rejected_kind is None
                or self.retry_after_seconds < 1
                or self.capacity_key is not None
            ):
                raise ValueError("quota_rejection_incomplete")
        elif self.outcome is AdmissionOutcome.LEASE_LIMITED:
            if (
                self.rejected_dimension is None
                or self.rejected_kind is not None
                or self.retry_after_seconds < 1
                or self.capacity_key is not None
            ):
                raise ValueError("lease_rejection_incomplete")
        elif self.outcome is AdmissionOutcome.CAPACITY_EXHAUSTED:
            if self.capacity_key is None or self.retry_after_seconds < 1:
                raise ValueError("capacity_rejection_incomplete")
        else:
            raise ValueError("admission_result_outcome_invalid")
        if self.lease_token:
            canonical = _canonical_token(self.lease_token)
            if canonical != self.lease_token:
                raise ValueError("ingress_admission_lease_token_not_canonical")
        if self.operation is AdmissionOperation.IP_REQUEST and self.lease_token:
            raise ValueError("ip_request_result_contains_lease")


@dataclass(frozen=True, slots=True)
class CleanupResult:
    backend: AdmissionBackend
    quota_deleted: int
    lease_deleted: int

    def __post_init__(self) -> None:
        if not isinstance(self.backend, AdmissionBackend):
            raise ValueError("admission_backend_invalid")
        _require_int(
            self.quota_deleted,
            "quota_deleted",
            minimum=0,
            maximum=INGRESS_ADMISSION_MAX_CLEANUP_BATCH,
        )
        _require_int(
            self.lease_deleted,
            "lease_deleted",
            minimum=0,
            maximum=INGRESS_ADMISSION_MAX_CLEANUP_BATCH,
        )


@dataclass(frozen=True, slots=True)
class CapacityRow:
    capacity_key: AdmissionCapacityKey
    row_count: int
    hard_limit: int
    contract_version: int

    def __post_init__(self) -> None:
        if not isinstance(self.capacity_key, AdmissionCapacityKey):
            raise ValueError("admission_capacity_key_invalid")
        _require_int(self.row_count, "capacity_row_count", minimum=0, maximum=_INT64_MAX)
        _require_int(self.hard_limit, "capacity_hard_limit", minimum=1, maximum=_INT64_MAX)
        _require_int(
            self.contract_version,
            "capacity_contract_version",
            minimum=1,
            maximum=_INT64_MAX,
        )


@dataclass(frozen=True, slots=True)
class AdmissionCapacitySnapshot:
    backend: AdmissionBackend
    contract_valid: bool
    rows: tuple[CapacityRow, ...]
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.backend, AdmissionBackend):
            raise ValueError("admission_backend_invalid")
        if not isinstance(self.contract_valid, bool):
            raise ValueError("capacity_contract_valid_invalid")
        if not isinstance(self.rows, tuple):
            raise ValueError("capacity_rows_must_be_a_tuple")
        if any(not isinstance(row, CapacityRow) for row in self.rows):
            raise ValueError("capacity_row_invalid")
        if len(self.reason) > 128:
            raise ValueError("capacity_reason_too_long")
        if self.contract_valid:
            if self.reason:
                raise ValueError("valid_capacity_snapshot_has_reason")
            if tuple(row.capacity_key for row in self.rows) != _CAPACITY_ORDER:
                raise ValueError("capacity_rows_not_canonical")
            for row in self.rows:
                expected_limit = _CAPACITY_LIMITS[row.capacity_key]
                if (
                    (
                        self.backend is AdmissionBackend.POSTGRES
                        and row.hard_limit != expected_limit
                    )
                    or (
                        self.backend is AdmissionBackend.MEMORY
                        and row.hard_limit > expected_limit
                    )
                    or row.contract_version != INGRESS_ADMISSION_CONTRACT_VERSION
                    or row.row_count > row.hard_limit
                ):
                    raise ValueError("capacity_contract_invalid")
        elif not self.reason:
            raise ValueError("invalid_capacity_snapshot_reason_required")


@runtime_checkable
class IngressAdmissionStore(Protocol):
    def consume_ip_request(
        self,
        *,
        subject: str,
        units: int,
        limit: int,
        window_seconds: int,
    ) -> AdmissionResult: ...

    def admit(self, request: AdmissionRequest) -> AdmissionResult: ...

    def renew_lease(self, lease_token: str, *, lease_seconds: int) -> bool: ...

    def release_lease(self, lease_token: str) -> bool: ...

    def cleanup_expired(self, *, batch_size: int) -> CleanupResult: ...

    def capacity_snapshot(self) -> AdmissionCapacitySnapshot: ...


@dataclass(frozen=True, slots=True)
class _MemoryQuotaBucket:
    used_units: int
    expires_at: float
    window_seconds: int


@dataclass(frozen=True, slots=True)
class _MemoryLease:
    scopes: tuple[tuple[AdmissionDimension, str], ...]
    expires_at: float


class InMemoryIngressAdmissionStore:
    """Deterministic, process-local implementation for development and tests."""

    def __init__(
        self,
        *,
        hmac_secret: bytes | str = b"propertyquarry-in-memory-admission-secret-v1",
        clock: Callable[[], float] = time.time,
        token_factory: Callable[[], object] = uuid4,
        quota_hard_limit: int = INGRESS_ADMISSION_QUOTA_HARD_LIMIT,
        lease_hard_limit: int = INGRESS_ADMISSION_LEASE_HARD_LIMIT,
        cleanup_batch_size: int = 256,
    ) -> None:
        self._secret = _validated_secret(hmac_secret)
        self._clock = clock
        self._token_factory = token_factory
        self._quota_hard_limit = _require_int(
            quota_hard_limit,
            "quota_hard_limit",
            minimum=1,
            maximum=INGRESS_ADMISSION_QUOTA_HARD_LIMIT,
        )
        self._lease_hard_limit = _require_int(
            lease_hard_limit,
            "lease_hard_limit",
            minimum=1,
            maximum=INGRESS_ADMISSION_LEASE_HARD_LIMIT,
        )
        self._cleanup_batch_size = _require_int(
            cleanup_batch_size,
            "cleanup_batch_size",
            minimum=1,
            maximum=INGRESS_ADMISSION_MAX_CLEANUP_BATCH,
        )
        self._lock = threading.Lock()
        self._buckets: dict[
            tuple[QuotaKind, AdmissionDimension, str, int],
            _MemoryQuotaBucket,
        ] = {}
        self._leases: dict[str, _MemoryLease] = {}

    def _now(self) -> float:
        now = float(self._clock())
        if not math.isfinite(now) or now < 0:
            raise IngressAdmissionContractError(
                "ingress_admission_clock_invalid",
                backend=AdmissionBackend.MEMORY,
                operation=AdmissionOperation.ADMIT,
            )
        return now

    def _cleanup_locked(self, *, now: float, batch_size: int) -> CleanupResult:
        expired_buckets = sorted(
            (
                (bucket.expires_at, key)
                for key, bucket in self._buckets.items()
                if bucket.expires_at <= now
            ),
            key=lambda item: (
                item[0],
                item[1][0].value,
                item[1][1].value,
                item[1][2],
                item[1][3],
            ),
        )[:batch_size]
        for _, key in expired_buckets:
            self._buckets.pop(key, None)

        expired_leases = sorted(
            (
                (lease.expires_at, token)
                for token, lease in self._leases.items()
                if lease.expires_at <= now
            )
        )[:batch_size]
        for _, token in expired_leases:
            self._leases.pop(token, None)
        return CleanupResult(
            backend=AdmissionBackend.MEMORY,
            quota_deleted=len(expired_buckets),
            lease_deleted=len(expired_leases),
        )

    def _new_token_locked(self) -> str:
        for _ in range(4):
            token = _canonical_token(self._token_factory())
            if token not in self._leases:
                return token
        raise IngressAdmissionContractError(
            "ingress_admission_lease_token_collision",
            backend=AdmissionBackend.MEMORY,
            operation=AdmissionOperation.ADMIT,
        )

    def consume_ip_request(
        self,
        *,
        subject: str,
        units: int,
        limit: int,
        window_seconds: int,
    ) -> AdmissionResult:
        charge = QuotaCharge(
            kind=QuotaKind.REQUEST,
            dimension=AdmissionDimension.IP,
            subject=subject,
            units=units,
            limit=limit,
            window_seconds=window_seconds,
        )
        return self._admit(
            AdmissionRequest(quota_charges=(charge,)),
            operation=AdmissionOperation.IP_REQUEST,
        )

    def admit(self, request: AdmissionRequest) -> AdmissionResult:
        if not isinstance(request, AdmissionRequest):
            raise ValueError("admission_request_invalid")
        return self._admit(request, operation=AdmissionOperation.ADMIT)

    def _admit(
        self,
        request: AdmissionRequest,
        *,
        operation: AdmissionOperation,
    ) -> AdmissionResult:
        with self._lock:
            now = self._now()
            self._cleanup_locked(
                now=now,
                batch_size=self._cleanup_batch_size,
            )
            prepared_charges: list[
                tuple[
                    QuotaCharge,
                    tuple[QuotaKind, AdmissionDimension, str, int],
                    int,
                    float,
                ]
            ] = []
            missing_quota_rows = 0
            for charge in request.quota_charges:
                digest = _subject_digest(
                    self._secret,
                    purpose=f"quota:{charge.kind.value}",
                    dimension=charge.dimension,
                    subject=charge.subject,
                )
                window_id = int(now // charge.window_seconds)
                expires_at = float((window_id + 1) * charge.window_seconds)
                key = (charge.kind, charge.dimension, digest, window_id)
                bucket = self._buckets.get(key)
                used_units = int(bucket.used_units) if bucket is not None else 0
                if bucket is None:
                    missing_quota_rows += 1
                if used_units + charge.units > charge.limit:
                    return AdmissionResult(
                        backend=AdmissionBackend.MEMORY,
                        operation=operation,
                        outcome=AdmissionOutcome.QUOTA_LIMITED,
                        allowed=False,
                        retry_after_seconds=max(1, math.ceil(expires_at - now)),
                        rejected_dimension=charge.dimension,
                        rejected_kind=charge.kind,
                    )
                prepared_charges.append(
                    (charge, key, used_units, expires_at)
                )

            prepared_scopes: list[tuple[LeaseScope, str]] = []
            for scope in request.lease_scopes:
                digest = _subject_digest(
                    self._secret,
                    purpose="lease",
                    dimension=scope.dimension,
                    subject=scope.subject,
                )
                matching_expiries = [
                    lease.expires_at
                    for lease in self._leases.values()
                    if lease.expires_at > now
                    and (scope.dimension, digest) in lease.scopes
                ]
                if len(matching_expiries) >= scope.limit:
                    return AdmissionResult(
                        backend=AdmissionBackend.MEMORY,
                        operation=operation,
                        outcome=AdmissionOutcome.LEASE_LIMITED,
                        allowed=False,
                        retry_after_seconds=max(
                            1,
                            math.ceil(min(matching_expiries) - now),
                        ),
                        rejected_dimension=scope.dimension,
                    )
                prepared_scopes.append((scope, digest))

            if len(self._buckets) + missing_quota_rows > self._quota_hard_limit:
                return AdmissionResult(
                    backend=AdmissionBackend.MEMORY,
                    operation=operation,
                    outcome=AdmissionOutcome.CAPACITY_EXHAUSTED,
                    allowed=False,
                    retry_after_seconds=1,
                    capacity_key=AdmissionCapacityKey.QUOTA,
                )
            if (
                prepared_scopes
                and len(self._leases) + 1 > self._lease_hard_limit
            ):
                return AdmissionResult(
                    backend=AdmissionBackend.MEMORY,
                    operation=operation,
                    outcome=AdmissionOutcome.CAPACITY_EXHAUSTED,
                    allowed=False,
                    retry_after_seconds=1,
                    capacity_key=AdmissionCapacityKey.LEASE,
                )

            lease_token = self._new_token_locked() if prepared_scopes else ""
            for charge, key, used_units, expires_at in prepared_charges:
                self._buckets[key] = _MemoryQuotaBucket(
                    used_units=used_units + charge.units,
                    expires_at=expires_at,
                    window_seconds=charge.window_seconds,
                )
            if prepared_scopes:
                self._leases[lease_token] = _MemoryLease(
                    scopes=tuple(
                        (scope.dimension, digest)
                        for scope, digest in prepared_scopes
                    ),
                    expires_at=now + request.lease_seconds,
                )
            return AdmissionResult(
                backend=AdmissionBackend.MEMORY,
                operation=operation,
                outcome=AdmissionOutcome.ALLOWED,
                allowed=True,
                lease_token=lease_token,
            )

    def renew_lease(self, lease_token: str, *, lease_seconds: int) -> bool:
        token = _canonical_token(lease_token)
        ttl = _require_int(
            lease_seconds,
            "lease_seconds",
            minimum=1,
            maximum=INGRESS_ADMISSION_MAX_LEASE_SECONDS,
        )
        with self._lock:
            now = self._now()
            lease = self._leases.get(token)
            if lease is None or lease.expires_at <= now:
                return False
            self._leases[token] = _MemoryLease(
                scopes=lease.scopes,
                expires_at=now + ttl,
            )
            return True

    def release_lease(self, lease_token: str) -> bool:
        token = _canonical_token(lease_token)
        with self._lock:
            return self._leases.pop(token, None) is not None

    def cleanup_expired(self, *, batch_size: int) -> CleanupResult:
        bounded_batch = _require_int(
            batch_size,
            "cleanup_batch_size",
            minimum=1,
            maximum=INGRESS_ADMISSION_MAX_CLEANUP_BATCH,
        )
        with self._lock:
            now = self._now()
            return self._cleanup_locked(now=now, batch_size=bounded_batch)

    def capacity_snapshot(self) -> AdmissionCapacitySnapshot:
        with self._lock:
            return AdmissionCapacitySnapshot(
                backend=AdmissionBackend.MEMORY,
                contract_valid=True,
                rows=(
                    CapacityRow(
                        capacity_key=AdmissionCapacityKey.QUOTA,
                        row_count=len(self._buckets),
                        hard_limit=self._quota_hard_limit,
                        contract_version=INGRESS_ADMISSION_CONTRACT_VERSION,
                    ),
                    CapacityRow(
                        capacity_key=AdmissionCapacityKey.LEASE,
                        row_count=len(self._leases),
                        hard_limit=self._lease_hard_limit,
                        contract_version=INGRESS_ADMISSION_CONTRACT_VERSION,
                    ),
                ),
            )


@dataclass(frozen=True, slots=True)
class _PreparedPostgresCharge:
    charge: QuotaCharge
    digest: str
    window_id: int
    expires_epoch: int
    used_units: int
    exists: bool


@dataclass(frozen=True, slots=True)
class _PreparedPostgresScope:
    scope: LeaseScope
    digest: str


class PostgresIngressAdmissionStore:
    """PostgreSQL-backed admission accounting with bounded resource use."""

    def __init__(
        self,
        database_url: str,
        *,
        hmac_secret: bytes | str,
        erasure_key_id: str,
        connect: Callable[..., Any] | None = None,
        connect_timeout_seconds: int = 3,
        statement_timeout_ms: int = 2_000,
        lock_timeout_ms: int = 250,
        idle_transaction_timeout_ms: int = 3_000,
        max_connections: int = 8,
        acquire_timeout_seconds: float = 0.25,
        cleanup_batch_size: int = 128,
        cleanup_interval_seconds: float = 1.0,
        cleanup_clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], object] = uuid4,
        verify_schema: bool = True,
    ) -> None:
        self._database_url = str(database_url or "").strip()
        if not self._database_url:
            raise IngressAdmissionConfigurationError(
                "ingress_admission_database_url_required",
                backend=AdmissionBackend.POSTGRES,
                operation=AdmissionOperation.SNAPSHOT,
            )
        self._secret = _validated_secret(hmac_secret)
        self._erasure_key_id = _validated_key_id(erasure_key_id)
        self._connect_timeout_seconds = _require_int(
            connect_timeout_seconds,
            "connect_timeout_seconds",
            minimum=1,
            maximum=30,
        )
        self._statement_timeout_ms = _require_int(
            statement_timeout_ms,
            "statement_timeout_ms",
            minimum=10,
            maximum=30_000,
        )
        self._lock_timeout_ms = _require_int(
            lock_timeout_ms,
            "lock_timeout_ms",
            minimum=10,
            maximum=self._statement_timeout_ms,
        )
        self._idle_transaction_timeout_ms = _require_int(
            idle_transaction_timeout_ms,
            "idle_transaction_timeout_ms",
            minimum=self._statement_timeout_ms,
            maximum=60_000,
        )
        connection_limit = _require_int(
            max_connections,
            "max_connections",
            minimum=1,
            maximum=32,
        )
        acquire_timeout = float(acquire_timeout_seconds)
        if (
            not math.isfinite(acquire_timeout)
            or acquire_timeout <= 0
            or acquire_timeout > 10
        ):
            raise ValueError("acquire_timeout_seconds_out_of_range")
        self._acquire_timeout_seconds = acquire_timeout
        self._cleanup_batch_size = _require_int(
            cleanup_batch_size,
            "cleanup_batch_size",
            minimum=1,
            maximum=INGRESS_ADMISSION_MAX_CLEANUP_BATCH,
        )
        cleanup_interval = float(cleanup_interval_seconds)
        if (
            not math.isfinite(cleanup_interval)
            or cleanup_interval < 0.1
            or cleanup_interval > 3_600
        ):
            raise ValueError("cleanup_interval_seconds_out_of_range")
        if not callable(cleanup_clock):
            raise ValueError("cleanup_clock_invalid")
        self._cleanup_interval_seconds = cleanup_interval
        self._cleanup_clock = cleanup_clock
        self._cleanup_state_lock = threading.Lock()
        self._next_cleanup_at = (
            self._cleanup_now() + self._cleanup_interval_seconds
        )
        self._token_factory = token_factory
        self._semaphore = threading.BoundedSemaphore(connection_limit)
        self._connect = connect or self._default_connect
        if verify_schema:
            self.require_ready()

    def _default_connect(self, database_url: str, *, autocommit: bool) -> Any:
        import psycopg

        return psycopg.connect(
            database_url,
            autocommit=autocommit,
            connect_timeout=self._connect_timeout_seconds,
        )

    @staticmethod
    def _database_error(
        exc: BaseException,
        *,
        operation: AdmissionOperation,
    ) -> IngressAdmissionError:
        sqlstate = getattr(exc, "sqlstate", None)
        if not sqlstate:
            sqlstate = getattr(getattr(exc, "diag", None), "sqlstate", None)
        if str(sqlstate or "") in _SCHEMA_SQLSTATES:
            return IngressAdmissionContractError(
                "ingress_admission_schema_not_ready",
                backend=AdmissionBackend.POSTGRES,
                operation=operation,
            )
        return IngressAdmissionUnavailable(
            "ingress_admission_postgres_unavailable",
            backend=AdmissionBackend.POSTGRES,
            operation=operation,
        )

    @contextmanager
    def _transaction(self, operation: AdmissionOperation) -> Iterator[Any]:
        if not self._semaphore.acquire(timeout=self._acquire_timeout_seconds):
            raise IngressAdmissionUnavailable(
                "ingress_admission_connection_capacity_exhausted",
                backend=AdmissionBackend.POSTGRES,
                operation=operation,
            )
        connection: Any | None = None
        try:
            connection = self._connect(self._database_url, autocommit=True)
            transaction_factory = getattr(connection, "transaction", None)
            if not callable(transaction_factory):
                raise IngressAdmissionContractError(
                    "ingress_admission_transaction_contract_missing",
                    backend=AdmissionBackend.POSTGRES,
                    operation=operation,
                )
            with transaction_factory():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            set_config('statement_timeout', %s, TRUE),
                            set_config('lock_timeout', %s, TRUE),
                            set_config(
                                'idle_in_transaction_session_timeout',
                                %s,
                                TRUE
                            )
                        """,
                        (
                            f"{self._statement_timeout_ms}ms",
                            f"{self._lock_timeout_ms}ms",
                            f"{self._idle_transaction_timeout_ms}ms",
                        ),
                    )
                    yield cursor
        except IngressAdmissionError:
            raise
        except Exception as exc:
            raise self._database_error(exc, operation=operation) from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            self._semaphore.release()

    @staticmethod
    def _acquire_locks(cursor: Any, lock_materials: Sequence[str]) -> None:
        for material in sorted(set(lock_materials)):
            cursor.execute(
                """
                SELECT pg_advisory_xact_lock(hashtextextended(%s, %s))
                """,
                (material, _ADVISORY_LOCK_SEED),
            )

    @staticmethod
    def _cleanup(cursor: Any, *, batch_size: int) -> tuple[int, int]:
        cursor.execute(
            """
            SELECT pg_try_advisory_xact_lock(hashtextextended(%s, %s))
            """,
            (_CLEANUP_LOCK_MATERIAL, _ADVISORY_LOCK_SEED),
        )
        leader_row = cursor.fetchone()
        if leader_row is None or not bool(leader_row[0]):
            return 0, 0
        # Quota DML precedes lease DML everywhere to match trigger lock order.
        cursor.execute(
            """
            WITH expired AS (
                SELECT quota_kind, dimension, subject_digest, window_id
                FROM propertyquarry_ingress_quota_buckets
                WHERE expires_at <= NOW()
                ORDER BY
                    expires_at,
                    quota_kind,
                    dimension,
                    subject_digest,
                    window_id
                FOR UPDATE SKIP LOCKED
                LIMIT %s
            )
            DELETE FROM propertyquarry_ingress_quota_buckets AS bucket
            USING expired
            WHERE bucket.quota_kind = expired.quota_kind
              AND bucket.dimension = expired.dimension
              AND bucket.subject_digest = expired.subject_digest
              AND bucket.window_id = expired.window_id
            RETURNING bucket.window_id
            """,
            (batch_size,),
        )
        quota_deleted = len(cursor.fetchall())
        cursor.execute(
            """
            WITH expired AS (
                SELECT lease_token
                FROM propertyquarry_ingress_leases
                WHERE expires_at <= NOW()
                ORDER BY expires_at, lease_token
                FOR UPDATE SKIP LOCKED
                LIMIT %s
            )
            DELETE FROM propertyquarry_ingress_leases AS lease
            USING expired
            WHERE lease.lease_token = expired.lease_token
            RETURNING lease.lease_token
            """,
            (batch_size,),
        )
        lease_deleted = len(cursor.fetchall())
        return quota_deleted, lease_deleted

    def _cleanup_now(self) -> float:
        try:
            now = float(self._cleanup_clock())
        except (TypeError, ValueError) as exc:
            raise IngressAdmissionConfigurationError(
                "ingress_admission_cleanup_clock_invalid",
                backend=AdmissionBackend.POSTGRES,
                operation=AdmissionOperation.CLEANUP,
            ) from exc
        if not math.isfinite(now) or now < 0:
            raise IngressAdmissionConfigurationError(
                "ingress_admission_cleanup_clock_invalid",
                backend=AdmissionBackend.POSTGRES,
                operation=AdmissionOperation.CLEANUP,
            )
        return now

    def _cleanup_if_due(self) -> None:
        now = self._cleanup_now()
        with self._cleanup_state_lock:
            if now < self._next_cleanup_at:
                return
            self._next_cleanup_at = now + self._cleanup_interval_seconds
        try:
            with self._transaction(AdmissionOperation.CLEANUP) as cursor:
                self._cleanup(
                    cursor,
                    batch_size=self._cleanup_batch_size,
                )
        except Exception:
            # Permit a bounded retry without allowing every rejected request
            # to amplify a transient cleanup failure.
            with self._cleanup_state_lock:
                self._next_cleanup_at = min(
                    self._next_cleanup_at,
                    now + self._cleanup_interval_seconds,
                )
            raise

    @staticmethod
    def _capacity_row(
        cursor: Any,
        capacity_key: AdmissionCapacityKey,
        *,
        lock: bool,
        operation: AdmissionOperation,
    ) -> CapacityRow:
        lock_clause = " FOR UPDATE" if lock else ""
        cursor.execute(
            (
                "SELECT row_count, hard_limit, contract_version "
                "FROM propertyquarry_ingress_admission_capacity "
                "WHERE capacity_key = %s" + lock_clause
            ),
            (capacity_key.value,),
        )
        row = cursor.fetchone()
        if row is None:
            raise IngressAdmissionContractError(
                "ingress_admission_capacity_row_missing",
                backend=AdmissionBackend.POSTGRES,
                operation=operation,
            )
        try:
            capacity = CapacityRow(
                capacity_key=capacity_key,
                row_count=int(row[0]),
                hard_limit=int(row[1]),
                contract_version=int(row[2]),
            )
        except (TypeError, ValueError) as exc:
            raise IngressAdmissionContractError(
                "ingress_admission_capacity_row_invalid",
                backend=AdmissionBackend.POSTGRES,
                operation=operation,
            ) from exc
        if (
            capacity.hard_limit != _CAPACITY_LIMITS[capacity_key]
            or capacity.contract_version != INGRESS_ADMISSION_CONTRACT_VERSION
            or capacity.row_count > capacity.hard_limit
        ):
            raise IngressAdmissionContractError(
                "ingress_admission_capacity_contract_invalid",
                backend=AdmissionBackend.POSTGRES,
                operation=operation,
            )
        return capacity

    def consume_ip_request(
        self,
        *,
        subject: str,
        units: int,
        limit: int,
        window_seconds: int,
    ) -> AdmissionResult:
        charge = QuotaCharge(
            kind=QuotaKind.REQUEST,
            dimension=AdmissionDimension.IP,
            subject=subject,
            units=units,
            limit=limit,
            window_seconds=window_seconds,
        )
        return self._admit(
            AdmissionRequest(quota_charges=(charge,)),
            operation=AdmissionOperation.IP_REQUEST,
        )

    def admit(self, request: AdmissionRequest) -> AdmissionResult:
        if not isinstance(request, AdmissionRequest):
            raise ValueError("admission_request_invalid")
        return self._admit(request, operation=AdmissionOperation.ADMIT)

    def _admit(
        self,
        request: AdmissionRequest,
        *,
        operation: AdmissionOperation,
    ) -> AdmissionResult:
        digested_charges = [
            (
                charge,
                _subject_digest(
                    self._secret,
                    purpose=f"quota:{charge.kind.value}",
                    dimension=charge.dimension,
                    subject=charge.subject,
                ),
            )
            for charge in request.quota_charges
        ]
        digested_scopes = [
            _PreparedPostgresScope(
                scope=scope,
                digest=_subject_digest(
                    self._secret,
                    purpose="lease",
                    dimension=scope.dimension,
                    subject=scope.subject,
                ),
            )
            for scope in request.lease_scopes
        ]
        lock_materials = [
            (
                "propertyquarry:ingress-admission:v1:"
                f"quota:{charge.kind.value}:{charge.dimension.value}:{digest}"
            )
            for charge, digest in digested_charges
        ]
        lock_materials.extend(
            (
                "propertyquarry:ingress-admission:v1:"
                f"lease:{prepared.scope.dimension.value}:{prepared.digest}"
            )
            for prepared in digested_scopes
        )

        # A process-local cadence and a cluster-wide try-lock bound cleanup
        # amplification. Cleanup still commits before subject/capacity locks,
        # preserving the global lock order.
        self._cleanup_if_due()

        with self._transaction(operation) as cursor:
            self._acquire_locks(cursor, lock_materials)
            cursor.execute(
                """
                WITH observed AS MATERIALIZED (
                    SELECT clock_timestamp() AS observed_at
                )
                SELECT
                    observed_at,
                    FLOOR(EXTRACT(EPOCH FROM observed_at))::BIGINT
                FROM observed
                """
            )
            epoch_row = cursor.fetchone()
            if epoch_row is None or epoch_row[0] is None:
                raise IngressAdmissionContractError(
                    "ingress_admission_server_clock_unavailable",
                    backend=AdmissionBackend.POSTGRES,
                    operation=operation,
                )
            server_now = epoch_row[0]
            server_epoch = int(epoch_row[1])
            prepared_charges: list[_PreparedPostgresCharge] = []
            for charge, digest in digested_charges:
                window_id = server_epoch // charge.window_seconds
                expires_epoch = (window_id + 1) * charge.window_seconds
                cursor.execute(
                    """
                    SELECT
                        used_units,
                        window_seconds,
                        FLOOR(EXTRACT(EPOCH FROM expires_at))::BIGINT
                    FROM propertyquarry_ingress_quota_buckets
                    WHERE quota_kind = %s
                      AND dimension = %s
                      AND subject_digest = %s
                      AND window_id = %s
                    FOR UPDATE
                    """,
                    (
                        charge.kind.value,
                        charge.dimension.value,
                        digest,
                        window_id,
                    ),
                )
                row = cursor.fetchone()
                exists = row is not None
                used_units = 0
                if row is not None:
                    stored_used = int(row[0])
                    stored_window_seconds = int(row[1])
                    stored_expiry = int(row[2])
                    if stored_used < 0:
                        raise IngressAdmissionContractError(
                            "ingress_admission_quota_row_invalid",
                            backend=AdmissionBackend.POSTGRES,
                            operation=operation,
                        )
                    if stored_window_seconds != charge.window_seconds:
                        raise IngressAdmissionContractError(
                            "ingress_admission_quota_window_conflict",
                            backend=AdmissionBackend.POSTGRES,
                            operation=operation,
                        )
                    if stored_expiry != expires_epoch:
                        raise IngressAdmissionContractError(
                            "ingress_admission_quota_expiry_conflict",
                            backend=AdmissionBackend.POSTGRES,
                            operation=operation,
                        )
                    used_units = stored_used
                if used_units + charge.units > charge.limit:
                    return AdmissionResult(
                        backend=AdmissionBackend.POSTGRES,
                        operation=operation,
                        outcome=AdmissionOutcome.QUOTA_LIMITED,
                        allowed=False,
                        retry_after_seconds=max(
                            1,
                            expires_epoch - server_epoch,
                        ),
                        rejected_dimension=charge.dimension,
                        rejected_kind=charge.kind,
                    )
                prepared_charges.append(
                    _PreparedPostgresCharge(
                        charge=charge,
                        digest=digest,
                        window_id=window_id,
                        expires_epoch=expires_epoch,
                        used_units=used_units,
                        exists=exists,
                    )
                )

            for prepared in digested_scopes:
                column = (
                    "ip_subject_digest"
                    if prepared.scope.dimension is AdmissionDimension.IP
                    else "account_subject_digest"
                )
                cursor.execute(
                    (
                        "SELECT COUNT(*), "
                        "COALESCE("
                        "CEIL(EXTRACT(EPOCH FROM (MIN(expires_at) - %s)))"
                        "::BIGINT, 0) "
                        "FROM propertyquarry_ingress_leases "
                        "WHERE expires_at > %s AND " + column + " = %s"
                    ),
                    (server_now, server_now, prepared.digest),
                )
                row = cursor.fetchone()
                active_count = int(row[0]) if row is not None else 0
                retry_after = int(row[1]) if row is not None else 0
                if active_count >= prepared.scope.limit:
                    return AdmissionResult(
                        backend=AdmissionBackend.POSTGRES,
                        operation=operation,
                        outcome=AdmissionOutcome.LEASE_LIMITED,
                        allowed=False,
                        retry_after_seconds=max(1, retry_after),
                        rejected_dimension=prepared.scope.dimension,
                    )

            missing_quota_rows = sum(
                1 for prepared in prepared_charges if not prepared.exists
            )
            quota_capacity: CapacityRow | None = None
            lease_capacity: CapacityRow | None = None
            if missing_quota_rows:
                quota_capacity = self._capacity_row(
                    cursor,
                    AdmissionCapacityKey.QUOTA,
                    lock=True,
                    operation=operation,
                )
            if digested_scopes:
                if quota_capacity is None:
                    # Preserve the global quota-before-lease capacity lock order.
                    quota_capacity = self._capacity_row(
                        cursor,
                        AdmissionCapacityKey.QUOTA,
                        lock=True,
                        operation=operation,
                    )
                lease_capacity = self._capacity_row(
                    cursor,
                    AdmissionCapacityKey.LEASE,
                    lock=True,
                    operation=operation,
                )
            if (
                missing_quota_rows
                and quota_capacity is not None
                and quota_capacity.row_count + missing_quota_rows
                > quota_capacity.hard_limit
            ):
                return AdmissionResult(
                    backend=AdmissionBackend.POSTGRES,
                    operation=operation,
                    outcome=AdmissionOutcome.CAPACITY_EXHAUSTED,
                    allowed=False,
                    retry_after_seconds=1,
                    capacity_key=AdmissionCapacityKey.QUOTA,
                )
            if (
                digested_scopes
                and lease_capacity is not None
                and lease_capacity.row_count + 1 > lease_capacity.hard_limit
            ):
                return AdmissionResult(
                    backend=AdmissionBackend.POSTGRES,
                    operation=operation,
                    outcome=AdmissionOutcome.CAPACITY_EXHAUSTED,
                    allowed=False,
                    retry_after_seconds=1,
                    capacity_key=AdmissionCapacityKey.LEASE,
                )

            lease_token = (
                _canonical_token(self._token_factory())
                if digested_scopes
                else ""
            )
            for prepared in prepared_charges:
                if prepared.exists:
                    cursor.execute(
                        """
                        UPDATE propertyquarry_ingress_quota_buckets
                        SET window_seconds = %s,
                            used_units = %s,
                            expires_at = TO_TIMESTAMP(%s),
                            updated_at = %s
                        WHERE quota_kind = %s
                          AND dimension = %s
                          AND subject_digest = %s
                          AND window_id = %s
                        """,
                        (
                            prepared.charge.window_seconds,
                            prepared.used_units + prepared.charge.units,
                            prepared.expires_epoch,
                            server_now,
                            prepared.charge.kind.value,
                            prepared.charge.dimension.value,
                            prepared.digest,
                            prepared.window_id,
                        ),
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO propertyquarry_ingress_quota_buckets (
                            quota_kind,
                            dimension,
                            subject_digest,
                            window_id,
                            window_seconds,
                            used_units,
                            expires_at,
                            updated_at
                        )
                        VALUES (
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            TO_TIMESTAMP(%s),
                            %s
                        )
                        """,
                        (
                            prepared.charge.kind.value,
                            prepared.charge.dimension.value,
                            prepared.digest,
                            prepared.window_id,
                            prepared.charge.window_seconds,
                            prepared.used_units + prepared.charge.units,
                            prepared.expires_epoch,
                            server_now,
                        ),
                    )
            if digested_scopes:
                by_dimension = {
                    prepared.scope.dimension: prepared.digest
                    for prepared in digested_scopes
                }
                cursor.execute(
                    """
                    INSERT INTO propertyquarry_ingress_leases (
                        lease_token,
                        ip_subject_digest,
                        account_subject_digest,
                        expires_at,
                        heartbeat_at,
                        created_at
                    )
                    VALUES (
                        %s,
                        %s,
                        %s,
                        %s + (%s * INTERVAL '1 second'),
                        %s,
                        %s
                    )
                    """,
                    (
                        UUID(hex=lease_token),
                        by_dimension[AdmissionDimension.IP],
                        by_dimension.get(AdmissionDimension.ACCOUNT),
                        server_now,
                        request.lease_seconds,
                        server_now,
                        server_now,
                    ),
                )
            return AdmissionResult(
                backend=AdmissionBackend.POSTGRES,
                operation=operation,
                outcome=AdmissionOutcome.ALLOWED,
                allowed=True,
                lease_token=lease_token,
            )

    def renew_lease(self, lease_token: str, *, lease_seconds: int) -> bool:
        token = UUID(hex=_canonical_token(lease_token))
        ttl = _require_int(
            lease_seconds,
            "lease_seconds",
            minimum=1,
            maximum=INGRESS_ADMISSION_MAX_LEASE_SECONDS,
        )
        with self._transaction(AdmissionOperation.RENEW) as cursor:
            cursor.execute(
                """
                SELECT lease_token
                FROM propertyquarry_ingress_leases
                WHERE lease_token = %s
                FOR UPDATE
                """,
                (token,),
            )
            if cursor.fetchone() is None:
                return False
            cursor.execute(
                """
                WITH observed AS MATERIALIZED (
                    SELECT clock_timestamp() AS observed_at
                )
                UPDATE propertyquarry_ingress_leases
                SET expires_at = observed.observed_at
                        + (%s * INTERVAL '1 second'),
                    heartbeat_at = observed.observed_at
                FROM observed
                WHERE lease_token = %s
                  AND expires_at > observed.observed_at
                RETURNING lease_token
                """,
                (ttl, token),
            )
            return cursor.fetchone() is not None

    def release_lease(self, lease_token: str) -> bool:
        token = UUID(hex=_canonical_token(lease_token))
        with self._transaction(AdmissionOperation.RELEASE) as cursor:
            cursor.execute(
                """
                DELETE FROM propertyquarry_ingress_leases
                WHERE lease_token = %s
                RETURNING lease_token
                """,
                (token,),
            )
            return cursor.fetchone() is not None

    def cleanup_expired(self, *, batch_size: int) -> CleanupResult:
        bounded_batch = _require_int(
            batch_size,
            "cleanup_batch_size",
            minimum=1,
            maximum=INGRESS_ADMISSION_MAX_CLEANUP_BATCH,
        )
        with self._transaction(AdmissionOperation.CLEANUP) as cursor:
            quota_deleted, lease_deleted = self._cleanup(
                cursor,
                batch_size=bounded_batch,
            )
            return CleanupResult(
                backend=AdmissionBackend.POSTGRES,
                quota_deleted=quota_deleted,
                lease_deleted=lease_deleted,
            )

    def capacity_snapshot(self) -> AdmissionCapacitySnapshot:
        with self._transaction(AdmissionOperation.SNAPSHOT) as cursor:
            cursor.execute(
                """
                SELECT
                    capacity_key,
                    row_count,
                    hard_limit,
                    contract_version
                FROM propertyquarry_ingress_admission_capacity
                ORDER BY capacity_key
                """
            )
            raw_rows = cursor.fetchall()
            parsed: dict[AdmissionCapacityKey, CapacityRow] = {}
            try:
                for raw_row in raw_rows:
                    capacity_key = AdmissionCapacityKey(str(raw_row[0]))
                    if capacity_key in parsed:
                        raise ValueError("duplicate_capacity_key")
                    parsed[capacity_key] = CapacityRow(
                        capacity_key=capacity_key,
                        row_count=int(raw_row[1]),
                        hard_limit=int(raw_row[2]),
                        contract_version=int(raw_row[3]),
                    )
            except (TypeError, ValueError) as exc:
                raise IngressAdmissionContractError(
                    "ingress_admission_capacity_row_invalid",
                    backend=AdmissionBackend.POSTGRES,
                    operation=AdmissionOperation.SNAPSHOT,
                ) from exc
            rows = tuple(
                parsed[capacity_key]
                for capacity_key in _CAPACITY_ORDER
                if capacity_key in parsed
            )
            if set(parsed) != set(_CAPACITY_ORDER):
                return AdmissionCapacitySnapshot(
                    backend=AdmissionBackend.POSTGRES,
                    contract_valid=False,
                    rows=rows,
                    reason="ingress_admission_capacity_keyset_invalid",
                )
            for row in rows:
                if (
                    row.hard_limit != _CAPACITY_LIMITS[row.capacity_key]
                    or row.contract_version
                    != INGRESS_ADMISSION_CONTRACT_VERSION
                    or row.row_count > row.hard_limit
                ):
                    raise IngressAdmissionContractError(
                        "ingress_admission_capacity_contract_invalid",
                        backend=AdmissionBackend.POSTGRES,
                        operation=AdmissionOperation.SNAPSHOT,
                    )
            return AdmissionCapacitySnapshot(
                backend=AdmissionBackend.POSTGRES,
                contract_valid=True,
                rows=rows,
            )

    def require_ready(self) -> None:
        # The production admission database deliberately contains only the
        # quota, lease, and capacity relations. It must not require or acquire
        # authority over the primary property-search migration ledger.
        snapshot = self.capacity_snapshot()
        if not snapshot.contract_valid:
            raise IngressAdmissionContractError(
                "ingress_admission_capacity_contract_invalid",
                backend=AdmissionBackend.POSTGRES,
                operation=AdmissionOperation.SNAPSHOT,
            )


__all__ = [
    "AdmissionBackend",
    "AdmissionCapacityKey",
    "AdmissionCapacitySnapshot",
    "AdmissionDimension",
    "AdmissionOperation",
    "AdmissionOutcome",
    "AdmissionRequest",
    "AdmissionResult",
    "CapacityRow",
    "CleanupResult",
    "INGRESS_ADMISSION_CONTRACT_VERSION",
    "INGRESS_ADMISSION_LEASE_HARD_LIMIT",
    "INGRESS_ADMISSION_MAX_CLEANUP_BATCH",
    "INGRESS_ADMISSION_MAX_LEASE_SECONDS",
    "INGRESS_ADMISSION_MAX_WINDOW_SECONDS",
    "INGRESS_ADMISSION_QUOTA_HARD_LIMIT",
    "InMemoryIngressAdmissionStore",
    "IngressAdmissionConfigurationError",
    "IngressAdmissionContractError",
    "IngressAdmissionError",
    "IngressAdmissionStore",
    "IngressAdmissionUnavailable",
    "LeaseScope",
    "PostgresIngressAdmissionStore",
    "QuotaCharge",
    "QuotaKind",
]
