from __future__ import annotations

from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import secrets
import stat
import threading
from typing import Protocol

from app.observability import runtime_build_identity


TRACEPARENT_HEADER = "traceparent"
TELEMETRY_PARENT_KEY = "_telemetry_parent"
_TRACEPARENT_VERSION = "00"
_TRACEPARENT_RE = re.compile(
    r"^00-(?P<trace_id>[0-9a-f]{32})-(?P<span_id>[0-9a-f]{16})-(?P<trace_flags>[0-9a-f]{2})$"
)
_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_BOUNDARIES = frozenset(
    {
        "customer_api",
        "durable_search_worker",
        "provider_or_render_boundary",
    }
)
_FORBIDDEN_PROPAGATION_HEADERS = frozenset({"baggage", "tracestate"})
_MAX_SECURE_ID_ATTEMPTS = 8
LOCAL_SPAN_SCHEMA = "propertyquarry.local-span-evidence.v1"
LOCAL_SPAN_EVIDENCE_SCOPE = "repo_local_deterministic_only"
LOCAL_SPAN_EXPORT_ENABLED_ENV = "PROPERTYQUARRY_LOCAL_SPAN_EXPORT_ENABLED"
LOCAL_SPAN_EXPORT_PATH_ENV = "PROPERTYQUARRY_LOCAL_SPAN_EXPORT_PATH"
LOCAL_SPAN_EXPORT_MAX_BYTES_ENV = "PROPERTYQUARRY_LOCAL_SPAN_EXPORT_MAX_BYTES"
LOCAL_SPAN_EXPORT_BACKUP_COUNT_ENV = (
    "PROPERTYQUARRY_LOCAL_SPAN_EXPORT_BACKUP_COUNT"
)
_DEFAULT_LOCAL_SPAN_MAX_BYTES = 4 * 1024 * 1024
_MIN_LOCAL_SPAN_MAX_BYTES = 4 * 1024
_MAX_LOCAL_SPAN_MAX_BYTES = 64 * 1024 * 1024
_DEFAULT_LOCAL_SPAN_BACKUP_COUNT = 3
_MAX_LOCAL_SPAN_BACKUP_COUNT = 10
_MAX_LOCAL_SPAN_LINE_BYTES = 4096
_RELEASE_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_RELEASE_IMAGE_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REPLICA_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_BOUNDARY_ORDER = {
    "customer_api": 0,
    "durable_search_worker": 1,
    "provider_or_render_boundary": 2,
}


class TraceContextError(ValueError):
    """Raised when untrusted trace context does not match the bounded contract."""


class SpanExportError(RuntimeError):
    """The local sink could not safely validate, persist, or query a span."""


class SpanExportConfigurationError(SpanExportError):
    """Explicit local span-export configuration is invalid or incomplete."""


@dataclass(frozen=True)
class TraceParent:
    trace_id: str
    span_id: str
    trace_flags: str = "01"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.trace_id, str)
            or not _TRACE_ID_RE.fullmatch(self.trace_id)
            or self.trace_id == "0" * 32
        ):
            raise TraceContextError("traceparent_trace_id_invalid")
        if (
            not isinstance(self.span_id, str)
            or not _SPAN_ID_RE.fullmatch(self.span_id)
            or self.span_id == "0" * 16
        ):
            raise TraceContextError("traceparent_span_id_invalid")
        if (
            not isinstance(self.trace_flags, str)
            or re.fullmatch(r"[0-9a-f]{2}", self.trace_flags) is None
        ):
            raise TraceContextError("traceparent_flags_invalid")


@dataclass(frozen=True)
class PersistedTraceParent:
    trace_parent: TraceParent
    correlation_id: str = ""


@dataclass(frozen=True)
class TelemetryContext:
    trace_id: str
    span_id: str
    parent_span_id: str
    trace_flags: str
    boundary: str
    correlation_id: str
    release_commit_sha: str
    release_image_digest: str
    replica_id: str

    @property
    def trace_parent(self) -> TraceParent:
        return TraceParent(
            trace_id=self.trace_id,
            span_id=self.span_id,
            trace_flags=self.trace_flags,
        )


@dataclass(frozen=True)
class SpanRecord:
    boundary: str
    trace_id: str
    span_id: str
    parent_span_id: str
    release_commit_sha: str
    release_image_digest: str
    replica_id: str
    started_at: str
    ended_at: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class SpanExporter(Protocol):
    def export(self, span: SpanRecord) -> None:
        """Consume one completed local span without mutating application state."""


class NullSpanExporter:
    """The production default: deliberately performs no I/O."""

    def export(self, span: SpanRecord) -> None:
        del span


class InMemorySpanExporter:
    """Thread-safe deterministic-order sink for tests and local diagnostics."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._spans: list[SpanRecord] = []

    def export(self, span: SpanRecord) -> None:
        with self._lock:
            self._spans.append(span)

    @property
    def spans(self) -> tuple[SpanRecord, ...]:
        with self._lock:
            return tuple(self._spans)

    def clear(self) -> None:
        with self._lock:
            self._spans.clear()


def _parse_span_timestamp(value: object, *, field: str) -> datetime:
    if (
        not isinstance(value, str)
        or not 20 <= len(value) <= 40
        or not value.endswith("Z")
    ):
        raise SpanExportError(f"local_span_{field}_invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise SpanExportError(f"local_span_{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SpanExportError(f"local_span_{field}_invalid")
    normalized = parsed.astimezone(timezone.utc)
    canonical = (
        normalized.isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    if value != canonical:
        raise SpanExportError(f"local_span_{field}_invalid")
    return normalized


def _validate_span_record(
    span: object,
    *,
    expected_identity: Mapping[str, str] | None = None,
) -> SpanRecord:
    if type(span) is not SpanRecord:
        raise SpanExportError("local_span_record_invalid")
    assert isinstance(span, SpanRecord)
    if span.boundary not in _BOUNDARIES:
        raise SpanExportError("local_span_boundary_invalid")
    if (
        not isinstance(span.trace_id, str)
        or not _TRACE_ID_RE.fullmatch(span.trace_id)
        or span.trace_id == "0" * 32
        or not isinstance(span.span_id, str)
        or not _SPAN_ID_RE.fullmatch(span.span_id)
        or span.span_id == "0" * 16
    ):
        raise SpanExportError("local_span_identity_invalid")
    if (
        not isinstance(span.parent_span_id, str)
        or (
            span.parent_span_id
            and (
                not _SPAN_ID_RE.fullmatch(span.parent_span_id)
                or span.parent_span_id == "0" * 16
                or span.parent_span_id == span.span_id
            )
        )
    ):
        raise SpanExportError("local_span_parent_invalid")
    if (
        not isinstance(span.release_commit_sha, str)
        or not _RELEASE_COMMIT_RE.fullmatch(span.release_commit_sha)
        or not isinstance(span.release_image_digest, str)
        or not _RELEASE_IMAGE_RE.fullmatch(span.release_image_digest)
        or not isinstance(span.replica_id, str)
        or not _REPLICA_ID_RE.fullmatch(span.replica_id)
    ):
        raise SpanExportError("local_span_release_identity_invalid")
    started_at = _parse_span_timestamp(span.started_at, field="started_at")
    ended_at = _parse_span_timestamp(span.ended_at, field="ended_at")
    if started_at > ended_at:
        raise SpanExportError("local_span_timestamp_order_invalid")
    if expected_identity is not None and (
        span.release_commit_sha != expected_identity.get("release_commit_sha")
        or span.release_image_digest
        != expected_identity.get("release_image_digest")
        or span.replica_id != expected_identity.get("replica_id")
    ):
        raise SpanExportError("local_span_runtime_identity_mismatch")
    return span


def _normal_local_span_path(value: object) -> Path:
    try:
        raw = os.fspath(value)
    except TypeError as exc:
        raise SpanExportConfigurationError(
            "local_span_export_path_invalid"
        ) from exc
    if (
        not isinstance(raw, str)
        or not raw
        or "\x00" in raw
        or any(ord(character) < 32 for character in raw)
    ):
        raise SpanExportConfigurationError("local_span_export_path_invalid")
    path = Path(raw)
    if (
        not path.is_absolute()
        or path == Path("/")
        or path.parent == Path("/")
        or os.path.normpath(raw) != raw
        or ".." in path.parts
        or path.suffix != ".jsonl"
        or not path.name
        or len(path.name.encode("utf-8")) > 180
    ):
        raise SpanExportConfigurationError("local_span_export_path_invalid")
    return path


class BoundedJsonlSpanExporter:
    """Private, bounded local JSONL evidence; never a live receipt producer."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        max_bytes: int = _DEFAULT_LOCAL_SPAN_MAX_BYTES,
        backup_count: int = _DEFAULT_LOCAL_SPAN_BACKUP_COUNT,
    ) -> None:
        if (
            type(max_bytes) is not int
            or not _MIN_LOCAL_SPAN_MAX_BYTES
            <= max_bytes
            <= _MAX_LOCAL_SPAN_MAX_BYTES
        ):
            raise SpanExportConfigurationError(
                "local_span_export_max_bytes_invalid"
            )
        if (
            type(backup_count) is not int
            or not 1 <= backup_count <= _MAX_LOCAL_SPAN_BACKUP_COUNT
        ):
            raise SpanExportConfigurationError(
                "local_span_export_backup_count_invalid"
            )
        self._path = _normal_local_span_path(path)
        self._directory = self._path.parent
        self._filename = self._path.name
        self._lock_filename = f".{self._filename}.lock"
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._thread_lock = threading.Lock()
        self._runtime_identity = runtime_build_identity()
        if not all(self._runtime_identity.values()):
            raise SpanExportConfigurationError(
                "local_span_export_release_identity_required"
            )
        with self._locked_directory() as directory_fd:
            if self._private_stat(directory_fd, self._filename) is not None:
                data_fd, _ = self._open_private_regular(
                    directory_fd,
                    self._filename,
                    access_flags=os.O_RDWR | os.O_APPEND,
                    create=False,
                )
                try:
                    self._validated_append_size(data_fd)
                finally:
                    os.close(data_fd)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @property
    def backup_count(self) -> int:
        return self._backup_count

    @staticmethod
    def _validate_private_directory(fd: int) -> None:
        info = os.fstat(fd)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise SpanExportError("local_span_export_directory_invalid")

    @staticmethod
    def _validate_private_regular_info(info: os.stat_result) -> None:
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise SpanExportError("local_span_export_file_invalid")

    def _open_private_directory(self) -> int:
        if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
            raise SpanExportError("local_span_export_nofollow_unavailable")
        created = False
        try:
            os.mkdir(self._directory, mode=0o700)
            created = True
        except FileExistsError:
            pass
        except OSError as exc:
            raise SpanExportError(
                "local_span_export_directory_open_failed"
            ) from exc
        flags = (
            os.O_RDONLY
            | os.O_CLOEXEC
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
        )
        try:
            fd = os.open(self._directory, flags)
        except OSError as exc:
            raise SpanExportError(
                "local_span_export_directory_open_failed"
            ) from exc
        try:
            if created:
                os.fchmod(fd, 0o700)
            self._validate_private_directory(fd)
            if created:
                os.fsync(fd)
            return fd
        except Exception:
            os.close(fd)
            raise

    def _open_private_regular(
        self,
        directory_fd: int,
        name: str,
        *,
        access_flags: int,
        create: bool,
    ) -> tuple[int, bool]:
        base_flags = access_flags | os.O_CLOEXEC | os.O_NOFOLLOW
        created = False
        try:
            if create:
                try:
                    fd = os.open(
                        name,
                        base_flags | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=directory_fd,
                    )
                    created = True
                except FileExistsError:
                    fd = os.open(name, base_flags, dir_fd=directory_fd)
            else:
                fd = os.open(name, base_flags, dir_fd=directory_fd)
        except OSError as exc:
            raise SpanExportError("local_span_export_file_open_failed") from exc
        try:
            if created:
                os.fchmod(fd, 0o600)
            self._validate_private_regular_info(os.fstat(fd))
            if created:
                os.fsync(directory_fd)
            return fd, created
        except Exception:
            os.close(fd)
            raise

    def _private_stat(
        self,
        directory_fd: int,
        name: str,
    ) -> os.stat_result | None:
        try:
            info = os.stat(
                name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SpanExportError("local_span_export_file_stat_failed") from exc
        self._validate_private_regular_info(info)
        return info

    @contextmanager
    def _locked_directory(self) -> Iterator[int]:
        with self._thread_lock:
            directory_fd = self._open_private_directory()
            lock_fd = -1
            try:
                lock_fd, _ = self._open_private_regular(
                    directory_fd,
                    self._lock_filename,
                    access_flags=os.O_RDWR,
                    create=True,
                )
                if os.fstat(lock_fd).st_size != 0:
                    raise SpanExportError("local_span_export_lock_invalid")
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX)
                except OSError as exc:
                    raise SpanExportError(
                        "local_span_export_lock_failed"
                    ) from exc
                yield directory_fd
            finally:
                if lock_fd >= 0:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    finally:
                        os.close(lock_fd)
                os.close(directory_fd)

    def _safe_unlink(self, directory_fd: int, name: str) -> None:
        if self._private_stat(directory_fd, name) is None:
            return
        try:
            os.unlink(name, dir_fd=directory_fd)
        except OSError as exc:
            raise SpanExportError("local_span_export_rotation_failed") from exc

    def _safe_replace(
        self,
        directory_fd: int,
        source: str,
        destination: str,
    ) -> None:
        if self._private_stat(directory_fd, source) is None:
            return
        self._safe_unlink(directory_fd, destination)
        try:
            os.replace(
                source,
                destination,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
        except OSError as exc:
            raise SpanExportError("local_span_export_rotation_failed") from exc

    def _rotate(self, directory_fd: int) -> None:
        self._safe_unlink(
            directory_fd,
            f"{self._filename}.{self._backup_count}",
        )
        for index in range(self._backup_count - 1, 0, -1):
            self._safe_replace(
                directory_fd,
                f"{self._filename}.{index}",
                f"{self._filename}.{index + 1}",
            )
        self._safe_replace(
            directory_fd,
            self._filename,
            f"{self._filename}.1",
        )
        os.fsync(directory_fd)

    @staticmethod
    def _write_all(fd: int, raw: bytes) -> None:
        remaining = memoryview(raw)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise SpanExportError("local_span_export_write_failed")
            remaining = remaining[written:]

    @staticmethod
    def _last_complete_line_offset(fd: int, size: int) -> int:
        cursor = size
        while cursor > 0:
            chunk_size = min(64 * 1024, cursor)
            offset = cursor - chunk_size
            try:
                chunk = os.pread(fd, chunk_size, offset)
            except OSError as exc:
                raise SpanExportError(
                    "local_span_export_file_read_failed"
                ) from exc
            if len(chunk) != chunk_size:
                raise SpanExportError(
                    "local_span_export_file_read_failed"
                )
            newline = chunk.rfind(b"\n")
            if newline >= 0:
                return offset + newline + 1
            cursor = offset
        return 0

    def _validated_append_size(self, fd: int) -> int:
        info = os.fstat(fd)
        self._validate_private_regular_info(info)
        if info.st_size < 0 or info.st_size > self._max_bytes:
            raise SpanExportError("local_span_export_size_invalid")
        if info.st_size:
            try:
                final_byte = os.pread(fd, 1, info.st_size - 1)
            except OSError as exc:
                raise SpanExportError(
                    "local_span_export_file_read_failed"
                ) from exc
            if final_byte != b"\n":
                complete_size = self._last_complete_line_offset(
                    fd,
                    info.st_size,
                )
                try:
                    os.ftruncate(fd, complete_size)
                    os.fsync(fd)
                except OSError as exc:
                    raise SpanExportError(
                        "local_span_export_recovery_failed"
                    ) from exc
                _record_span_export_recovery(
                    discarded_bytes=info.st_size - complete_size,
                )
                return complete_size
        return info.st_size

    @staticmethod
    def _rollback_append(fd: int, initial_size: int) -> None:
        try:
            os.ftruncate(fd, initial_size)
            os.fsync(fd)
        except OSError as exc:
            raise SpanExportError(
                "local_span_export_rollback_failed"
            ) from exc

    def _encoded_line(
        self,
        span: SpanRecord,
        *,
        enforce_runtime_identity: bool = True,
    ) -> bytes:
        validated = _validate_span_record(
            span,
            expected_identity=(
                self._runtime_identity
                if enforce_runtime_identity
                else None
            ),
        )
        span_payload = validated.to_dict()
        if not validated.parent_span_id:
            span_payload["parent_span_id"] = None
        payload = {
            "schema": LOCAL_SPAN_SCHEMA,
            "evidence_scope": LOCAL_SPAN_EVIDENCE_SCOPE,
            "live_receipt_eligible": False,
            "span": span_payload,
        }
        try:
            raw = (
                json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError) as exc:
            raise SpanExportError("local_span_export_json_invalid") from exc
        if not 0 < len(raw) <= _MAX_LOCAL_SPAN_LINE_BYTES:
            raise SpanExportError("local_span_export_line_size_invalid")
        return raw

    def export(self, span: SpanRecord) -> None:
        raw = self._encoded_line(span)
        with self._locked_directory() as directory_fd:
            data_fd, _ = self._open_private_regular(
                directory_fd,
                self._filename,
                access_flags=os.O_RDWR | os.O_APPEND,
                create=True,
            )
            try:
                initial_size = self._validated_append_size(data_fd)
                if initial_size + len(raw) > self._max_bytes:
                    os.close(data_fd)
                    data_fd = -1
                    self._rotate(directory_fd)
                    data_fd, _ = self._open_private_regular(
                        directory_fd,
                        self._filename,
                        access_flags=os.O_RDWR | os.O_APPEND,
                        create=True,
                    )
                    if self._validated_append_size(data_fd) != 0:
                        raise SpanExportError(
                            "local_span_export_rotation_failed"
                        )
                    initial_size = 0
                try:
                    self._write_all(data_fd, raw)
                    if os.fstat(data_fd).st_size > self._max_bytes:
                        raise SpanExportError(
                            "local_span_export_size_invalid"
                        )
                    os.fsync(data_fd)
                except Exception as exc:
                    try:
                        self._rollback_append(data_fd, initial_size)
                    except SpanExportError as rollback_exc:
                        raise rollback_exc from exc
                    if isinstance(exc, SpanExportError):
                        raise
                    raise SpanExportError(
                        "local_span_export_write_failed"
                    ) from exc
            finally:
                if data_fd >= 0:
                    os.close(data_fd)

    @staticmethod
    def _reject_json_constant(_value: str) -> object:
        raise SpanExportError("local_span_export_json_invalid")

    @staticmethod
    def _reject_duplicate_pairs(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        payload: dict[str, object] = {}
        for key, value in pairs:
            if key in payload:
                raise SpanExportError(
                    "local_span_export_json_duplicate_key"
                )
            payload[key] = value
        return payload

    def _decode_line(self, raw: bytes) -> SpanRecord:
        if not 0 < len(raw) <= _MAX_LOCAL_SPAN_LINE_BYTES:
            raise SpanExportError("local_span_export_line_size_invalid")
        try:
            payload = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=self._reject_duplicate_pairs,
                parse_constant=self._reject_json_constant,
            )
        except SpanExportError:
            raise
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            RecursionError,
        ) as exc:
            raise SpanExportError("local_span_export_json_invalid") from exc
        if (
            not isinstance(payload, Mapping)
            or set(payload)
            != {
                "schema",
                "evidence_scope",
                "live_receipt_eligible",
                "span",
            }
            or payload["schema"] != LOCAL_SPAN_SCHEMA
            or payload["evidence_scope"] != LOCAL_SPAN_EVIDENCE_SCOPE
            or payload["live_receipt_eligible"] is not False
        ):
            raise SpanExportError("local_span_export_envelope_invalid")
        raw_span = payload["span"]
        span_fields = set(SpanRecord.__dataclass_fields__)
        if not isinstance(raw_span, Mapping) or set(raw_span) != span_fields:
            raise SpanExportError("local_span_export_record_invalid")
        parent_span_id = raw_span["parent_span_id"]
        if parent_span_id is None:
            parent_span_id = ""
        values = {
            key: value
            for key, value in raw_span.items()
            if key != "parent_span_id"
        }
        if any(not isinstance(value, str) for value in values.values()):
            raise SpanExportError("local_span_export_record_invalid")
        if not isinstance(parent_span_id, str):
            raise SpanExportError("local_span_export_record_invalid")
        span = SpanRecord(
            boundary=values["boundary"],
            trace_id=values["trace_id"],
            span_id=values["span_id"],
            parent_span_id=parent_span_id,
            release_commit_sha=values["release_commit_sha"],
            release_image_digest=values["release_image_digest"],
            replica_id=values["replica_id"],
            started_at=values["started_at"],
            ended_at=values["ended_at"],
        )
        validated = _validate_span_record(span)
        if (
            self._encoded_line(
                validated,
                enforce_runtime_identity=False,
            )
            != raw + b"\n"
        ):
            raise SpanExportError("local_span_export_json_not_canonical")
        return validated

    def _read_private_file(self, directory_fd: int, name: str) -> bytes:
        if self._private_stat(directory_fd, name) is None:
            return b""
        fd, _ = self._open_private_regular(
            directory_fd,
            name,
            access_flags=os.O_RDONLY,
            create=False,
        )
        try:
            info = os.fstat(fd)
            if info.st_size > self._max_bytes:
                raise SpanExportError("local_span_export_size_invalid")
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = os.read(fd, min(64 * 1024, self._max_bytes + 1))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > self._max_bytes:
                    raise SpanExportError("local_span_export_size_invalid")
            return b"".join(chunks)
        finally:
            os.close(fd)

    def query_spans(self, *, trace_id: str = "") -> tuple[SpanRecord, ...]:
        if not isinstance(trace_id, str) or (
            trace_id
            and (
                not _TRACE_ID_RE.fullmatch(trace_id)
                or trace_id == "0" * 32
            )
        ):
            raise SpanExportError("local_span_query_trace_id_invalid")
        records: list[SpanRecord] = []
        names = [
            f"{self._filename}.{index}"
            for index in range(self._backup_count, 0, -1)
        ]
        names.append(self._filename)
        with self._locked_directory() as directory_fd:
            for name in names:
                raw = self._read_private_file(directory_fd, name)
                if not raw:
                    continue
                if not raw.endswith(b"\n"):
                    raise SpanExportError("local_span_export_partial_line")
                for line in raw[:-1].split(b"\n"):
                    span = self._decode_line(line)
                    if not trace_id or span.trace_id == trace_id:
                        records.append(span)
        records.sort(
            key=lambda span: (
                span.started_at,
                _BOUNDARY_ORDER[span.boundary],
                span.ended_at,
                span.span_id,
            )
        )
        return tuple(records)


_CURRENT_CONTEXT: ContextVar[TelemetryContext | None] = ContextVar(
    "propertyquarry_telemetry_context",
    default=None,
)
_EXPORTER_LOCK = threading.Lock()
_SPAN_EXPORTER: SpanExporter = NullSpanExporter()
_SPAN_EXPORT_HEALTH_LOCK = threading.Lock()
_SPAN_EXPORT_FAILURE_COUNT = 0
_SPAN_EXPORT_RECOVERY_COUNT = 0
_SPAN_EXPORT_LAST_FAILURE_REASON = ""
_SPAN_EXPORT_LAST_FAILURE_AT = ""


def _utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _secure_nonzero_hex(byte_count: int, *, field: str) -> str:
    for _ in range(_MAX_SECURE_ID_ATTEMPTS):
        value = secrets.token_bytes(byte_count).hex()
        if value != "0" * (byte_count * 2):
            return value
    raise RuntimeError(f"{field}_generation_failed")


def generate_trace_id() -> str:
    return _secure_nonzero_hex(16, field="trace_id")


def generate_span_id() -> str:
    return _secure_nonzero_hex(8, field="span_id")


def parse_traceparent(value: object) -> TraceParent:
    if not isinstance(value, str):
        raise TraceContextError("traceparent_string_required")
    match = _TRACEPARENT_RE.fullmatch(value)
    if match is None:
        raise TraceContextError("traceparent_v00_invalid")
    return TraceParent(
        trace_id=match.group("trace_id"),
        span_id=match.group("span_id"),
        trace_flags=match.group("trace_flags"),
    )


def try_parse_traceparent(value: object) -> TraceParent | None:
    if value is None or value == "":
        return None
    try:
        return parse_traceparent(value)
    except TraceContextError:
        return None


def format_traceparent(
    value: TraceParent | TelemetryContext | PersistedTraceParent,
) -> str:
    if isinstance(value, PersistedTraceParent):
        parent = value.trace_parent
    elif isinstance(value, TelemetryContext):
        parent = value.trace_parent
    else:
        parent = value
    validated = TraceParent(
        trace_id=parent.trace_id,
        span_id=parent.span_id,
        trace_flags=parent.trace_flags,
    )
    outgoing_flags = f"{int(validated.trace_flags, 16) & 0x01:02x}"
    return (
        f"{_TRACEPARENT_VERSION}-{validated.trace_id}-"
        f"{validated.span_id}-{outgoing_flags}"
    )


def extract_traceparent(headers: Mapping[str, object]) -> TraceParent | None:
    """Read only traceparent; tracestate and baggage are intentionally ignored."""

    raw_values = tuple(
        value
        for key, value in headers.items()
        if str(key).lower() == TRACEPARENT_HEADER
    )
    if not raw_values:
        return None
    if len(raw_values) != 1:
        raise TraceContextError("traceparent_multiple")
    return parse_traceparent(raw_values[0])


def inject_traceparent(
    headers: MutableMapping[str, str],
    context: TraceParent | TelemetryContext | PersistedTraceParent | None = None,
) -> str:
    """Inject the one allowed propagation header and strip unbounded context."""

    selected = context or current_telemetry_context()
    if selected is None:
        raise TraceContextError("active_trace_context_required")
    for key in tuple(headers):
        normalized_key = str(key).lower()
        if (
            normalized_key == TRACEPARENT_HEADER
            or normalized_key in _FORBIDDEN_PROPAGATION_HEADERS
        ):
            del headers[key]
    value = format_traceparent(selected)
    headers[TRACEPARENT_HEADER] = value
    return value


def current_telemetry_context() -> TelemetryContext | None:
    return _CURRENT_CONTEXT.get()


def current_telemetry_log_fields() -> dict[str, str]:
    context = current_telemetry_context()
    if context is None:
        return {}
    return {
        "correlation_id": context.correlation_id,
        "trace_id": context.trace_id,
        "span_id": context.span_id,
        "release_commit_sha": context.release_commit_sha,
        "release_image_digest": context.release_image_digest,
        "replica_id": context.replica_id,
    }


def serialize_current_trace_parent() -> dict[str, str]:
    context = current_telemetry_context()
    if context is None:
        return {}
    payload = {"traceparent": format_traceparent(context)}
    if _CORRELATION_ID_RE.fullmatch(context.correlation_id):
        payload["correlation_id"] = context.correlation_id
    return payload


def normalize_persisted_trace_parent(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise TraceContextError("persisted_trace_parent_object_required")
    normalized_keys = {str(key) for key in value}
    if normalized_keys - {"traceparent", "correlation_id"}:
        raise TraceContextError("persisted_trace_parent_fields_invalid")
    traceparent = format_traceparent(parse_traceparent(value.get("traceparent")))
    raw_correlation_id = value.get("correlation_id")
    if raw_correlation_id is not None and not isinstance(raw_correlation_id, str):
        raise TraceContextError("persisted_correlation_id_invalid")
    correlation_id = raw_correlation_id or ""
    if correlation_id and not _CORRELATION_ID_RE.fullmatch(correlation_id):
        raise TraceContextError("persisted_correlation_id_invalid")
    payload = {"traceparent": traceparent}
    if correlation_id:
        payload["correlation_id"] = correlation_id
    return payload


def persisted_trace_parent_from_payload(
    payload: Mapping[str, object],
) -> PersistedTraceParent | None:
    if TELEMETRY_PARENT_KEY not in payload:
        return None
    normalized = normalize_persisted_trace_parent(payload[TELEMETRY_PARENT_KEY])
    return PersistedTraceParent(
        trace_parent=parse_traceparent(normalized["traceparent"]),
        correlation_id=normalized.get("correlation_id", ""),
    )


def _local_span_export_enabled() -> bool:
    raw = str(os.environ.get(LOCAL_SPAN_EXPORT_ENABLED_ENV) or "").strip().lower()
    if raw in {"", "0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    raise SpanExportConfigurationError(
        "local_span_export_enabled_invalid"
    )


def _local_span_export_int(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = str(os.environ.get(name) or default).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise SpanExportConfigurationError(
            f"{name.lower()}_invalid"
        ) from exc
    if not minimum <= value <= maximum:
        raise SpanExportConfigurationError(f"{name.lower()}_invalid")
    return value


def span_exporter_from_environment() -> SpanExporter:
    """Resolve the explicit local sink; disabled always means no I/O."""

    if not _local_span_export_enabled():
        return NullSpanExporter()
    raw_path = str(
        os.environ.get(LOCAL_SPAN_EXPORT_PATH_ENV) or ""
    ).strip()
    if not raw_path:
        raise SpanExportConfigurationError(
            "local_span_export_path_required"
        )
    return BoundedJsonlSpanExporter(
        raw_path,
        max_bytes=_local_span_export_int(
            LOCAL_SPAN_EXPORT_MAX_BYTES_ENV,
            _DEFAULT_LOCAL_SPAN_MAX_BYTES,
            minimum=_MIN_LOCAL_SPAN_MAX_BYTES,
            maximum=_MAX_LOCAL_SPAN_MAX_BYTES,
        ),
        backup_count=_local_span_export_int(
            LOCAL_SPAN_EXPORT_BACKUP_COUNT_ENV,
            _DEFAULT_LOCAL_SPAN_BACKUP_COUNT,
            minimum=1,
            maximum=_MAX_LOCAL_SPAN_BACKUP_COUNT,
        ),
    )


def _selected_exporter() -> SpanExporter:
    with _EXPORTER_LOCK:
        return _SPAN_EXPORTER


def span_export_health_snapshot() -> dict[str, object]:
    with _SPAN_EXPORT_HEALTH_LOCK:
        return {
            "failure_count": _SPAN_EXPORT_FAILURE_COUNT,
            "recovery_count": _SPAN_EXPORT_RECOVERY_COUNT,
            "last_failure_reason": _SPAN_EXPORT_LAST_FAILURE_REASON,
            "last_failure_at": _SPAN_EXPORT_LAST_FAILURE_AT,
        }


def _span_exporter_kind(exporter: SpanExporter) -> str:
    if isinstance(exporter, BoundedJsonlSpanExporter):
        return "bounded_jsonl"
    if isinstance(exporter, InMemorySpanExporter):
        return "memory"
    if isinstance(exporter, NullSpanExporter):
        return "null"
    return "custom"


def _span_export_failure_reason(exc: Exception) -> str:
    if isinstance(exc, SpanExportError):
        reason = str(exc)
        if re.fullmatch(r"[a-z0-9_]{1,128}", reason):
            return reason
    return "unexpected_span_export_error"


def _record_span_export_failure(
    exporter: SpanExporter,
    exc: Exception,
) -> None:
    global _SPAN_EXPORT_FAILURE_COUNT
    global _SPAN_EXPORT_LAST_FAILURE_AT
    global _SPAN_EXPORT_LAST_FAILURE_REASON
    reason = _span_export_failure_reason(exc)
    failed_at = _utc_now_iso()
    with _SPAN_EXPORT_HEALTH_LOCK:
        _SPAN_EXPORT_FAILURE_COUNT += 1
        _SPAN_EXPORT_LAST_FAILURE_REASON = reason
        _SPAN_EXPORT_LAST_FAILURE_AT = failed_at
    logging.getLogger("ea.telemetry").error(
        "span export failed exporter=%s reason=%s",
        _span_exporter_kind(exporter),
        reason,
    )


def _record_span_export_recovery(*, discarded_bytes: int) -> None:
    global _SPAN_EXPORT_RECOVERY_COUNT
    bounded_discarded = max(0, int(discarded_bytes))
    with _SPAN_EXPORT_HEALTH_LOCK:
        _SPAN_EXPORT_RECOVERY_COUNT += 1
    logging.getLogger("ea.telemetry").warning(
        "span export recovered partial tail discarded_bytes=%s",
        bounded_discarded,
    )


def set_span_exporter(exporter: SpanExporter | None) -> SpanExporter:
    global _SPAN_EXPORTER
    selected: SpanExporter = exporter if exporter is not None else NullSpanExporter()
    if not callable(getattr(selected, "export", None)):
        raise TypeError("span_exporter_export_required")
    with _EXPORTER_LOCK:
        previous = _SPAN_EXPORTER
        _SPAN_EXPORTER = selected
    return previous


def configure_span_exporter_from_environment() -> SpanExporter:
    exporter = span_exporter_from_environment()
    set_span_exporter(exporter)
    return exporter


@contextmanager
def use_span_exporter(exporter: SpanExporter | None) -> Iterator[SpanExporter]:
    selected: SpanExporter = exporter if exporter is not None else NullSpanExporter()
    previous = set_span_exporter(selected)
    try:
        yield selected
    finally:
        set_span_exporter(previous)


def _parent_parts(
    parent: TraceParent | TelemetryContext | PersistedTraceParent | None,
) -> tuple[TraceParent | None, str]:
    if isinstance(parent, PersistedTraceParent):
        return parent.trace_parent, parent.correlation_id
    if isinstance(parent, TelemetryContext):
        return parent.trace_parent, parent.correlation_id
    return parent, ""


@contextmanager
def start_span(
    boundary: str,
    *,
    parent: TraceParent | TelemetryContext | PersistedTraceParent | None = None,
    correlation_id: str = "",
) -> Iterator[TelemetryContext]:
    normalized_boundary = str(boundary or "").strip()
    if normalized_boundary not in _BOUNDARIES:
        raise ValueError("telemetry_boundary_invalid")
    selected_parent = parent
    if selected_parent is None:
        selected_parent = current_telemetry_context()
    parent_trace, inherited_correlation = _parent_parts(selected_parent)
    raw_correlation = correlation_id or inherited_correlation or ""
    if not isinstance(raw_correlation, str):
        raise TraceContextError("telemetry_correlation_id_invalid")
    normalized_correlation = raw_correlation.strip()
    if normalized_correlation and not _CORRELATION_ID_RE.fullmatch(normalized_correlation):
        raise TraceContextError("telemetry_correlation_id_invalid")
    identity = runtime_build_identity()
    context = TelemetryContext(
        trace_id=parent_trace.trace_id if parent_trace is not None else generate_trace_id(),
        span_id=generate_span_id(),
        parent_span_id=parent_trace.span_id if parent_trace is not None else "",
        trace_flags=parent_trace.trace_flags if parent_trace is not None else "01",
        boundary=normalized_boundary,
        correlation_id=normalized_correlation,
        release_commit_sha=identity["release_commit_sha"],
        release_image_digest=identity["release_image_digest"],
        replica_id=identity["replica_id"],
    )
    started_at = _utc_now_iso()
    token = _CURRENT_CONTEXT.set(context)
    try:
        yield context
    finally:
        ended_at = _utc_now_iso()
        span = SpanRecord(
            boundary=context.boundary,
            trace_id=context.trace_id,
            span_id=context.span_id,
            parent_span_id=context.parent_span_id,
            release_commit_sha=context.release_commit_sha,
            release_image_digest=context.release_image_digest,
            replica_id=context.replica_id,
            started_at=started_at,
            ended_at=ended_at,
        )
        exporter = _selected_exporter()
        try:
            exporter.export(span)
        except Exception as exc:
            # Telemetry export is observational and must never alter request/job
            # outcomes. Failures are exposed through bounded logs and health.
            _record_span_export_failure(exporter, exc)
        finally:
            _CURRENT_CONTEXT.reset(token)
