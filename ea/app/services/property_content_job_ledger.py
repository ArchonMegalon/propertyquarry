from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import threading
from uuid import uuid4

from app.domain.property.content_source_packet import canonical_json, now_utc_iso, sha256_json
from app.product.property_search_storage import (
    _property_search_erasure_key_id,
    _property_search_principal_key,
    property_account_publication_authority,
)


_LEDGER_CONTRACT = "propertyquarry.content_job_ledger.v3"
_WEBHOOK_PROVIDER = "subscribr"
_WEBHOOK_TERMINAL_STATUSES = {
    "completed",
    "duplicate_ignored",
    "ignored",
    "received",
    "replay_conflict",
    "review_required",
}
_CONTENT_LOCK_SEED = 0x5051434A
_CONTENT_WRITER_CONTRACT = "3"
PROPERTY_CONTENT_SYSTEM_PRINCIPAL_ID = "propertyquarry:system:content-studio"


class PropertyContentLedgerError(RuntimeError):
    """Base failure for the governed property-content ledger."""


class PropertyContentLedgerCorruptionError(PropertyContentLedgerError):
    """The file-backed development ledger is unreadable and was preserved."""


class PropertyContentJobClaimLostError(PropertyContentLedgerError):
    """A claimed job or webhook no longer belongs to this worker."""


def default_subscribr_completion_dir() -> Path:
    return Path(os.getenv("PROPERTYQUARRY_SUBSCRIBR_COMPLETION_DIR") or "_completion/subscribr")


def default_property_content_ledger_path() -> Path:
    explicit = str(os.getenv("PROPERTYQUARRY_CONTENT_JOB_LEDGER") or "").strip()
    if explicit:
        return Path(explicit)
    return default_subscribr_completion_dir() / "property_content_jobs.json"


def _empty_ledger() -> dict[str, object]:
    return {
        "contract_name": _LEDGER_CONTRACT,
        "jobs": {},
        "job_events": [],
        "webhook_events": {},
        "next_event_sequence": 1,
    }


def _as_utc(value: datetime | str | None = None) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _lease_active(row: dict[str, object], *, now: datetime) -> bool:
    owner = str(row.get("lease_owner") or "").strip()
    expires_at = str(row.get("lease_expires_at") or "").strip()
    if not owner or not expires_at:
        return False
    try:
        return _as_utc(expires_at) > now
    except (TypeError, ValueError) as exc:
        raise PropertyContentLedgerCorruptionError("property_content_lease_timestamp_invalid") from exc


def _job_idempotency_key(
    *,
    principal_key: str,
    ownership_scope: str,
    search_run_id: str,
    packet_id: str,
) -> str:
    material = "\0".join(
        (
            str(principal_key or "").strip(),
            str(ownership_scope or "").strip(),
            str(search_run_id or "").strip(),
            str(packet_id or "").strip(),
        )
    )
    return f"property-content-job:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def _job_storage_key(
    *,
    principal_key: str,
    ownership_scope: str,
    search_run_id: str,
    packet_id: str,
) -> str:
    return _job_idempotency_key(
        principal_key=principal_key,
        ownership_scope=ownership_scope,
        search_run_id=search_run_id,
        packet_id=packet_id,
    ).removeprefix("property-content-job:")


def _webhook_storage_key(
    *,
    principal_key: str,
    ownership_scope: str,
    search_run_id: str,
    event_id: str,
) -> str:
    material = "\0".join(
        (
            str(principal_key or "").strip(),
            str(ownership_scope or "").strip(),
            str(search_run_id or "").strip(),
            str(event_id or "").strip(),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _event_id(idempotency_key: str) -> str:
    digest = hashlib.sha256(str(idempotency_key or "").encode("utf-8")).hexdigest()
    return f"content_evt_{digest[:24]}"


def _event_packet_id(payload: dict[str, object], event_id: str) -> str:
    packet_id = str(payload.get("packet_id") or payload.get("packetId") or "").strip()
    inline = payload.get("packet") if isinstance(payload.get("packet"), dict) else {}
    return packet_id or str(inline.get("packet_id") or "").strip() or f"webhook:{event_id}"


def _principal_ownership(
    principal_id: object,
    *,
    ownership_scope: object,
    search_run_id: object,
) -> tuple[str, str, str, str]:
    normalized_principal = str(principal_id or "").strip()
    if not normalized_principal:
        raise ValueError("property_content_principal_id_required")
    principal_key = _property_search_principal_key(normalized_principal)
    if not principal_key:
        raise ValueError("property_content_principal_key_required")
    normalized_scope = str(ownership_scope or "").strip().lower()
    normalized_run_id = str(search_run_id or "").strip()
    if len(normalized_run_id) > 256:
        raise ValueError("property_content_search_run_id_invalid")
    if normalized_scope == "search_run":
        if not normalized_run_id:
            raise ValueError("property_content_search_run_id_required")
    elif normalized_scope == "system":
        if normalized_principal != PROPERTY_CONTENT_SYSTEM_PRINCIPAL_ID:
            raise ValueError("property_content_system_owner_required")
        if normalized_run_id:
            raise ValueError("property_content_system_search_run_forbidden")
    else:
        raise ValueError("property_content_ownership_scope_invalid")
    return normalized_principal, principal_key, normalized_scope, normalized_run_id


def _row_ownership(row: dict[str, object]) -> tuple[str, str, str, str]:
    principal_id = str(row.get("principal_id") or "").strip()
    principal_key = str(row.get("principal_key") or "").strip()
    expected_principal, expected_key, ownership_scope, search_run_id = _principal_ownership(
        principal_id,
        ownership_scope=row.get("ownership_scope"),
        search_run_id=row.get("search_run_id"),
    )
    if principal_key != expected_key:
        raise PropertyContentLedgerError("property_content_row_owner_mismatch")
    return expected_principal, expected_key, ownership_scope, search_run_id


def _validate_row_identity(
    row: dict[str, object],
    *,
    principal_key: str,
    ownership_scope: str,
    search_run_id: str,
    packet_id: str,
) -> dict[str, object]:
    _, row_key, row_scope, row_run_id = _row_ownership(row)
    if (
        row_key != str(principal_key or "").strip()
        or row_scope != str(ownership_scope or "").strip()
        or row_run_id != str(search_run_id or "").strip()
        or str(row.get("packet_id") or "").strip() != str(packet_id or "").strip()
    ):
        raise PropertyContentLedgerError("property_content_row_identity_mismatch")
    return row


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _export_job_row(row: dict[str, object]) -> dict[str, object]:
    _row_ownership(row)
    source_packet = row.get("source_packet_json")
    receipt_path = str(row.get("receipt_path") or "").strip()
    return {
        "packet_id": str(row.get("packet_id") or ""),
        "ownership_scope": str(row.get("ownership_scope") or ""),
        "search_run_id": str(row.get("search_run_id") or ""),
        "status": str(row.get("status") or ""),
        "content_mode": str(row.get("content_mode") or ""),
        "channel_key": str(row.get("channel_key") or ""),
        "source_packet_sha256": str(row.get("source_packet_sha256") or ""),
        "source_packet": dict(source_packet) if isinstance(source_packet, dict) else {},
        "provider": str(row.get("provider") or ""),
        "provider_channel_id": str(row.get("provider_channel_id") or ""),
        "provider_idea_id": str(row.get("provider_idea_id") or ""),
        "provider_script_id": str(row.get("provider_script_id") or ""),
        "receipt": {
            "filename": Path(receipt_path).name if receipt_path else "",
            "sha256": str(row.get("receipt_sha256") or ""),
            "status": str(row.get("receipt_status") or ""),
        },
        "created_at": str(row.get("created_at") or ""),
        "updated_at": str(row.get("updated_at") or ""),
    }


def _receipt_path(
    *,
    principal_key: str,
    ownership_scope: str,
    search_run_id: str,
    packet_id: object,
) -> Path:
    raw_packet_id = str(packet_id or "").strip()
    safe_packet = "".join(
        ch if ch.isalnum() or ch in {"-", "_"} else "_"
        for ch in raw_packet_id
    )[:120]
    if not safe_packet:
        raise ValueError("property_content_packet_id_required")
    identity_digest = hashlib.sha256(
        "\0".join(
            (
                str(principal_key or "").strip(),
                str(ownership_scope or "").strip(),
                str(search_run_id or "").strip(),
                raw_packet_id,
            )
        ).encode("utf-8")
    ).hexdigest()[:20]
    return default_subscribr_completion_dir() / (
        f"propertyquarry_{safe_packet}-{identity_digest}.generated.json"
    )


def _delete_receipt_files(ownership_rows: tuple[dict[str, str], ...]) -> int:
    removed = 0
    for row in ownership_rows:
        path = _receipt_path(
            principal_key=row["principal_key"],
            ownership_scope=row["ownership_scope"],
            search_run_id=row["search_run_id"],
            packet_id=row["packet_id"],
        )
        candidates = [path]
        try:
            candidates.extend(
                child
                for child in path.parent.iterdir()
                if child.name.startswith(f".{path.name}.") and child.name.endswith(".tmp")
            )
        except FileNotFoundError:
            pass
        for candidate in candidates:
            try:
                candidate.unlink()
                removed += 1
            except FileNotFoundError:
                pass
    return removed


def _build_job_row(
    packet: dict[str, object],
    *,
    principal_id: str,
    ownership_scope: str,
    search_run_id: str,
    status: str,
    current: dict[str, object] | None = None,
    extra: dict[str, object] | None = None,
    observed_at: str | None = None,
) -> dict[str, object]:
    packet_id = str(packet.get("packet_id") or "").strip()
    if not packet_id:
        raise ValueError("property_content_packet_id_required")
    normalized_principal, principal_key, normalized_scope, normalized_run_id = _principal_ownership(
        principal_id,
        ownership_scope=ownership_scope,
        search_run_id=search_run_id,
    )
    previous = dict(current or {})
    previous_key = str(previous.get("principal_key") or "").strip()
    if previous and not previous_key:
        raise PropertyContentLedgerError("property_content_legacy_owner_unresolved")
    if previous_key and previous_key != principal_key:
        raise PropertyContentLedgerError("property_content_job_owner_mismatch")
    previous_scope = str(previous.get("ownership_scope") or "").strip()
    previous_run_id = str(previous.get("search_run_id") or "").strip()
    if previous and (
        previous_scope != normalized_scope or previous_run_id != normalized_run_id
    ):
        raise PropertyContentLedgerError("property_content_job_owner_run_immutable")
    idempotency_key = _job_idempotency_key(
        principal_key=principal_key,
        ownership_scope=normalized_scope,
        search_run_id=normalized_run_id,
        packet_id=packet_id,
    )
    now = str(observed_at or now_utc_iso())
    row = {
        **previous,
        "packet_id": packet_id,
        "principal_id": normalized_principal,
        "principal_key": principal_key,
        "ownership_scope": normalized_scope,
        "search_run_id": normalized_run_id,
        "idempotency_key": idempotency_key,
        "content_mode": str(packet.get("content_mode") or ""),
        "channel_key": str(packet.get("subscribr_channel_key") or ""),
        "source_packet_json": dict(packet),
        "source_packet_sha256": str(packet.get("source_packet_sha256") or ""),
        "source_packet_canonical_sha256": sha256_json(packet),
        "status": str(status or "").strip() or "UNKNOWN",
        "updated_at": now,
        "created_at": str(previous.get("created_at") or now),
        "version": max(0, int(previous.get("version") or 0)) + 1,
        "production_allowed": False,
        "publication_allowed": False,
        "lease_owner": str(previous.get("lease_owner") or ""),
        "lease_expires_at": previous.get("lease_expires_at"),
        "claimed_at": previous.get("claimed_at"),
    }
    if extra:
        row.update(dict(extra))
    row.update(
        {
            "packet_id": packet_id,
            "principal_id": normalized_principal,
            "principal_key": principal_key,
            "ownership_scope": normalized_scope,
            "search_run_id": normalized_run_id,
            "idempotency_key": idempotency_key,
            "source_packet_json": dict(packet),
            "source_packet_canonical_sha256": sha256_json(packet),
            "production_allowed": False,
            "publication_allowed": False,
        }
    )
    return row


class _FilePropertyContentRepository:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._thread_lock = threading.RLock()
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    @contextmanager
    def _locked(self, *, exclusive: bool):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock:
            with self._lock_path.open("a+", encoding="utf-8") as lock_handle:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                try:
                    yield
                finally:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def _load_unlocked(self) -> dict[str, object]:
        if not self.path.exists():
            return _empty_ledger()
        try:
            parsed = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise PropertyContentLedgerCorruptionError("property_content_ledger_corrupt") from exc
        if not isinstance(parsed, dict):
            raise PropertyContentLedgerCorruptionError("property_content_ledger_not_object")
        if str(parsed.get("contract_name") or "") != _LEDGER_CONTRACT:
            raise PropertyContentLedgerCorruptionError(
                "property_content_legacy_ownership_unresolved"
            )
        jobs = parsed.get("jobs", {})
        webhook_events = parsed.get("webhook_events", {})
        job_events = parsed.get("job_events", [])
        if not isinstance(jobs, dict) or not isinstance(webhook_events, dict) or not isinstance(job_events, list):
            raise PropertyContentLedgerCorruptionError("property_content_ledger_shape_invalid")
        if any(not isinstance(row, dict) for row in jobs.values()):
            raise PropertyContentLedgerCorruptionError("property_content_job_row_invalid")
        if any(not isinstance(row, dict) for row in webhook_events.values()):
            raise PropertyContentLedgerCorruptionError("property_content_webhook_row_invalid")
        for storage_key, row in jobs.items():
            try:
                _, principal_key, ownership_scope, search_run_id = _row_ownership(row)
            except (PropertyContentLedgerError, ValueError) as exc:
                raise PropertyContentLedgerCorruptionError(
                    "property_content_job_owner_invalid"
                ) from exc
            expected_key = _job_storage_key(
                principal_key=principal_key,
                ownership_scope=ownership_scope,
                search_run_id=search_run_id,
                packet_id=str(row.get("packet_id") or ""),
            )
            if str(storage_key) != expected_key:
                raise PropertyContentLedgerCorruptionError(
                    "property_content_job_storage_key_invalid"
                )
        for storage_key, row in webhook_events.items():
            try:
                _, principal_key, ownership_scope, search_run_id = _row_ownership(row)
            except (PropertyContentLedgerError, ValueError) as exc:
                raise PropertyContentLedgerCorruptionError(
                    "property_content_webhook_owner_invalid"
                ) from exc
            expected_key = _webhook_storage_key(
                principal_key=principal_key,
                ownership_scope=ownership_scope,
                search_run_id=search_run_id,
                event_id=str(row.get("event_id") or ""),
            )
            if str(storage_key) != expected_key:
                raise PropertyContentLedgerCorruptionError(
                    "property_content_webhook_storage_key_invalid"
                )
        normalized = dict(parsed)
        normalized["contract_name"] = _LEDGER_CONTRACT
        normalized["jobs"] = jobs
        normalized["webhook_events"] = webhook_events
        normalized["job_events"] = job_events
        try:
            next_sequence = max(1, int(normalized.get("next_event_sequence") or 1))
        except (TypeError, ValueError):
            raise PropertyContentLedgerCorruptionError("property_content_ledger_sequence_invalid")
        observed_sequences: list[int] = []
        observed_event_ids: set[tuple[str, str, str, str]] = set()
        observed_idempotency_keys: set[tuple[str, str, str, str]] = set()
        for event in job_events:
            if not isinstance(event, dict):
                raise PropertyContentLedgerCorruptionError("property_content_job_event_invalid")
            try:
                sequence = int(event.get("event_sequence") or 0)
            except (TypeError, ValueError) as exc:
                raise PropertyContentLedgerCorruptionError("property_content_job_event_sequence_invalid") from exc
            event_id = str(event.get("event_id") or "").strip()
            idempotency_key = str(event.get("idempotency_key") or "").strip()
            if sequence < 1 or not event_id or not idempotency_key:
                raise PropertyContentLedgerCorruptionError("property_content_job_event_invalid")
            try:
                _, principal_key, ownership_scope, search_run_id = _row_ownership(event)
            except (PropertyContentLedgerError, ValueError) as exc:
                raise PropertyContentLedgerCorruptionError(
                    "property_content_job_event_owner_invalid"
                ) from exc
            job_key = _job_storage_key(
                principal_key=principal_key,
                ownership_scope=ownership_scope,
                search_run_id=search_run_id,
                packet_id=str(event.get("packet_id") or ""),
            )
            if job_key not in jobs:
                raise PropertyContentLedgerCorruptionError(
                    "property_content_job_event_owner_packet_missing"
                )
            scoped_event_id = (principal_key, ownership_scope, search_run_id, event_id)
            scoped_idempotency_key = (
                principal_key,
                ownership_scope,
                search_run_id,
                idempotency_key,
            )
            if (
                scoped_event_id in observed_event_ids
                or scoped_idempotency_key in observed_idempotency_keys
            ):
                raise PropertyContentLedgerCorruptionError("property_content_job_event_duplicate")
            observed_sequences.append(sequence)
            observed_event_ids.add(scoped_event_id)
            observed_idempotency_keys.add(scoped_idempotency_key)
        if observed_sequences != sorted(observed_sequences) or len(observed_sequences) != len(set(observed_sequences)):
            raise PropertyContentLedgerCorruptionError("property_content_job_event_order_invalid")
        normalized["next_event_sequence"] = max([next_sequence, *(value + 1 for value in observed_sequences)])
        return normalized

    def _write_unlocked(self, payload: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.{uuid4().hex}.tmp"
        )
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            tmp.replace(self.path)
            directory_fd = os.open(str(self.path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if tmp.exists():
                tmp.unlink()

    def snapshot(self) -> dict[str, object]:
        with self._locked(exclusive=False):
            return self._load_unlocked()

    def _append_event_unlocked(
        self,
        data: dict[str, object],
        *,
        packet_id: str,
        event_type: str,
        status: str,
        payload: dict[str, object],
        principal_id: str,
        principal_key: str,
        ownership_scope: str,
        search_run_id: str,
        idempotency_key: str,
        created_at: str,
    ) -> dict[str, object]:
        events = data.setdefault("job_events", [])
        if not isinstance(events, list):
            raise PropertyContentLedgerCorruptionError("property_content_job_events_invalid")
        existing = next(
            (
                dict(candidate)
                for candidate in events
                if isinstance(candidate, dict)
                and str(candidate.get("principal_key") or "") == principal_key
                and str(candidate.get("ownership_scope") or "") == ownership_scope
                and str(candidate.get("search_run_id") or "") == search_run_id
                and str(candidate.get("idempotency_key") or "") == idempotency_key
            ),
            None,
        )
        if existing is not None:
            return existing
        sequence = max(1, int(data.get("next_event_sequence") or 1))
        row = {
            "event_sequence": sequence,
            "event_id": _event_id(idempotency_key),
            "packet_id": str(packet_id or "").strip(),
            "principal_id": str(principal_id or "").strip(),
            "principal_key": str(principal_key or "").strip(),
            "ownership_scope": str(ownership_scope or "").strip(),
            "search_run_id": str(search_run_id or "").strip(),
            "event_type": str(event_type or "").strip(),
            "status": str(status or "").strip(),
            "idempotency_key": idempotency_key,
            "payload_json": dict(payload or {}),
            "created_at": created_at,
        }
        events.append(row)
        data["next_event_sequence"] = sequence + 1
        return row

    def get_job(
        self,
        packet_id: str,
        *,
        principal_id: str,
        ownership_scope: str,
        search_run_id: str,
    ) -> dict[str, object] | None:
        _, principal_key, normalized_scope, normalized_run_id = _principal_ownership(
            principal_id,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
        )
        data = self.snapshot()
        jobs = data["jobs"]
        row = jobs.get(
            _job_storage_key(
                principal_key=principal_key,
                ownership_scope=normalized_scope,
                search_run_id=normalized_run_id,
                packet_id=str(packet_id or "").strip(),
            )
        )
        return dict(row) if isinstance(row, dict) else None

    def list_jobs(self, *, principal_id: str, limit: int) -> list[dict[str, object]]:
        normalized_principal = str(principal_id or "").strip()
        if not normalized_principal:
            raise ValueError("property_content_principal_id_required")
        principal_key = _property_search_principal_key(normalized_principal)
        jobs = self.snapshot()["jobs"]
        rows = [
            dict(row)
            for row in jobs.values()
            if isinstance(row, dict)
            and str(row.get("principal_key") or "") == principal_key
        ]
        rows.sort(
            key=lambda row: (str(row.get("updated_at") or ""), str(row.get("packet_id") or "")),
            reverse=True,
        )
        return rows[:limit]

    def upsert_job(
        self,
        packet: dict[str, object],
        *,
        principal_id: str,
        ownership_scope: str,
        search_run_id: str,
        status: str,
        extra: dict[str, object] | None = None,
    ) -> dict[str, object]:
        packet_id = str(packet.get("packet_id") or "").strip()
        _, principal_key, normalized_scope, normalized_run_id = _principal_ownership(
            principal_id,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
        )
        storage_key = _job_storage_key(
            principal_key=principal_key,
            ownership_scope=normalized_scope,
            search_run_id=normalized_run_id,
            packet_id=packet_id,
        )
        with self._locked(exclusive=True):
            data = self._load_unlocked()
            jobs = data["jobs"]
            current = dict(jobs.get(storage_key) or {})
            row = _build_job_row(
                packet,
                principal_id=principal_id,
                ownership_scope=normalized_scope,
                search_run_id=normalized_run_id,
                status=status,
                current=current,
                extra=extra,
            )
            jobs[storage_key] = row
            self._append_event_unlocked(
                data,
                packet_id=packet_id,
                event_type="job_upserted",
                status=str(row["status"]),
                payload={"version": row["version"], "status": row["status"]},
                principal_id=str(row["principal_id"]),
                principal_key=str(row["principal_key"]),
                ownership_scope=str(row["ownership_scope"]),
                search_run_id=str(row["search_run_id"]),
                idempotency_key=f"{row['idempotency_key']}:version:{row['version']}",
                created_at=str(row["updated_at"]),
            )
            self._write_unlocked(data)
            return dict(row)

    def record_provider_ids(
        self,
        *,
        packet_id: str,
        principal_id: str,
        ownership_scope: str,
        search_run_id: str,
        provider_channel_id: object = "",
        provider_idea_id: object = "",
        provider_script_id: object = "",
        status: str = "PROVIDER_JOB_CREATED",
        lease_owner: str = "",
    ) -> dict[str, object]:
        normalized_packet = str(packet_id or "").strip()
        _, expected_key, normalized_scope, normalized_run_id = _principal_ownership(
            principal_id,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
        )
        storage_key = _job_storage_key(
            principal_key=expected_key,
            ownership_scope=normalized_scope,
            search_run_id=normalized_run_id,
            packet_id=normalized_packet,
        )
        with self._locked(exclusive=True):
            data = self._load_unlocked()
            jobs = data["jobs"]
            current = dict(jobs.get(storage_key) or {})
            if not current:
                raise ValueError("property_content_job_not_found")
            owner = str(lease_owner or "").strip()
            if owner and str(current.get("lease_owner") or "") != owner:
                raise PropertyContentJobClaimLostError("property_content_job_claim_lost")
            row_principal, principal_key, row_scope, row_run_id = _row_ownership(current)
            now = now_utc_iso()
            row = {
                **current,
                "provider": _WEBHOOK_PROVIDER,
                "provider_channel_id": str(provider_channel_id or current.get("provider_channel_id") or ""),
                "provider_idea_id": str(provider_idea_id or current.get("provider_idea_id") or ""),
                "provider_script_id": str(provider_script_id or current.get("provider_script_id") or ""),
                "status": str(status or "PROVIDER_JOB_CREATED"),
                "updated_at": now,
                "version": max(0, int(current.get("version") or 0)) + 1,
                "lease_owner": "" if owner else str(current.get("lease_owner") or ""),
                "lease_expires_at": None if owner else current.get("lease_expires_at"),
            }
            jobs[storage_key] = row
            self._append_event_unlocked(
                data,
                packet_id=normalized_packet,
                event_type="provider_ids_recorded",
                status=str(row["status"]),
                payload={
                    "version": row["version"],
                    "provider_channel_id": row["provider_channel_id"],
                    "provider_idea_id": row["provider_idea_id"],
                    "provider_script_id": row["provider_script_id"],
                },
                principal_id=row_principal,
                principal_key=principal_key,
                ownership_scope=row_scope,
                search_run_id=row_run_id,
                idempotency_key=f"{row['idempotency_key']}:version:{row['version']}",
                created_at=now,
            )
            self._write_unlocked(data)
            return dict(row)

    def claim_job(
        self,
        packet_id: str,
        *,
        principal_id: str,
        ownership_scope: str,
        search_run_id: str,
        lease_owner: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> dict[str, object] | None:
        normalized_packet = str(packet_id or "").strip()
        owner = str(lease_owner or "").strip()
        if not normalized_packet or not owner:
            raise ValueError("property_content_job_claim_identity_required")
        _, expected_key, normalized_scope, normalized_run_id = _principal_ownership(
            principal_id,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
        )
        storage_key = _job_storage_key(
            principal_key=expected_key,
            ownership_scope=normalized_scope,
            search_run_id=normalized_run_id,
            packet_id=normalized_packet,
        )
        observed = _as_utc(now)
        with self._locked(exclusive=True):
            data = self._load_unlocked()
            jobs = data["jobs"]
            current = dict(jobs.get(storage_key) or {})
            if not current:
                return None
            row_principal, principal_key, row_scope, row_run_id = _row_ownership(current)
            if _lease_active(current, now=observed):
                return dict(current) if str(current.get("lease_owner") or "") == owner else None
            recovered = bool(str(current.get("lease_owner") or ""))
            claimed_at = observed.isoformat()
            row = {
                **current,
                "lease_owner": owner,
                "lease_expires_at": (observed + timedelta(seconds=max(1, int(lease_seconds or 1)))).isoformat(),
                "claimed_at": claimed_at,
                "claim_recovered": recovered,
                "updated_at": claimed_at,
                "version": max(0, int(current.get("version") or 0)) + 1,
            }
            jobs[storage_key] = row
            self._append_event_unlocked(
                data,
                packet_id=normalized_packet,
                event_type="job_claim_recovered" if recovered else "job_claimed",
                status=str(row.get("status") or ""),
                payload={"version": row["version"], "lease_owner": owner},
                principal_id=row_principal,
                principal_key=principal_key,
                ownership_scope=row_scope,
                search_run_id=row_run_id,
                idempotency_key=f"{row['idempotency_key']}:claim:{row['version']}",
                created_at=claimed_at,
            )
            self._write_unlocked(data)
            return dict(row)

    def update_claimed_job(
        self,
        packet_id: str,
        *,
        principal_id: str,
        ownership_scope: str,
        search_run_id: str,
        lease_owner: str,
        status: str,
        extra: dict[str, object] | None = None,
        release: bool = True,
    ) -> dict[str, object]:
        normalized_packet = str(packet_id or "").strip()
        owner = str(lease_owner or "").strip()
        _, expected_key, normalized_scope, normalized_run_id = _principal_ownership(
            principal_id,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
        )
        storage_key = _job_storage_key(
            principal_key=expected_key,
            ownership_scope=normalized_scope,
            search_run_id=normalized_run_id,
            packet_id=normalized_packet,
        )
        with self._locked(exclusive=True):
            data = self._load_unlocked()
            jobs = data["jobs"]
            current = dict(jobs.get(storage_key) or {})
            if not current:
                raise ValueError("property_content_job_not_found")
            if not owner or str(current.get("lease_owner") or "") != owner:
                raise PropertyContentJobClaimLostError("property_content_job_claim_lost")
            row_principal, principal_key, row_scope, row_run_id = _row_ownership(current)
            now = now_utc_iso()
            row = {**current, **dict(extra or {})}
            row.update(
                {
                    "packet_id": normalized_packet,
                    "idempotency_key": current["idempotency_key"],
                    "status": str(status or current.get("status") or "UNKNOWN"),
                    "updated_at": now,
                    "version": max(0, int(current.get("version") or 0)) + 1,
                    "lease_owner": "" if release else owner,
                    "lease_expires_at": None if release else current.get("lease_expires_at"),
                }
            )
            jobs[storage_key] = row
            self._append_event_unlocked(
                data,
                packet_id=normalized_packet,
                event_type="job_claim_completed" if release else "job_claim_updated",
                status=str(row["status"]),
                payload={"version": row["version"], "status": row["status"]},
                principal_id=row_principal,
                principal_key=principal_key,
                ownership_scope=row_scope,
                search_run_id=row_run_id,
                idempotency_key=f"{row['idempotency_key']}:version:{row['version']}",
                created_at=now,
            )
            self._write_unlocked(data)
            return dict(row)

    def webhook_seen(
        self,
        event_id: str,
        *,
        principal_id: str,
        ownership_scope: str,
        search_run_id: str,
    ) -> bool:
        _, principal_key, normalized_scope, normalized_run_id = _principal_ownership(
            principal_id,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
        )
        storage_key = _webhook_storage_key(
            principal_key=principal_key,
            ownership_scope=normalized_scope,
            search_run_id=normalized_run_id,
            event_id=str(event_id or "").strip(),
        )
        events = self.snapshot()["webhook_events"]
        return storage_key in events

    def claim_webhook_event(
        self,
        *,
        event_id: str,
        payload: dict[str, object],
        principal_id: str,
        ownership_scope: str,
        search_run_id: str,
        packet_id: str,
        extra: dict[str, object] | None,
        claim_owner: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> dict[str, object]:
        event_ref = str(event_id or "").strip()
        owner = str(claim_owner or "").strip()
        if not event_ref:
            raise ValueError("subscribr_webhook_event_id_required")
        if not owner:
            raise ValueError("subscribr_webhook_claim_owner_required")
        observed = _as_utc(now)
        observed_iso = observed.isoformat()
        payload_hash = sha256_json(payload)
        normalized_packet = str(packet_id or "").strip()
        normalized_principal, principal_key, normalized_scope, normalized_run_id = _principal_ownership(
            principal_id,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
        )
        job_storage_key = _job_storage_key(
            principal_key=principal_key,
            ownership_scope=normalized_scope,
            search_run_id=normalized_run_id,
            packet_id=normalized_packet,
        )
        webhook_storage_key = _webhook_storage_key(
            principal_key=principal_key,
            ownership_scope=normalized_scope,
            search_run_id=normalized_run_id,
            event_id=event_ref,
        )
        with self._locked(exclusive=True):
            data = self._load_unlocked()
            job = dict(data["jobs"].get(job_storage_key) or {})
            if not job:
                raise ValueError("property_content_job_not_found")
            _validate_row_identity(
                job,
                principal_key=principal_key,
                ownership_scope=normalized_scope,
                search_run_id=normalized_run_id,
                packet_id=normalized_packet,
            )
            events = data["webhook_events"]
            current = dict(events.get(webhook_storage_key) or {})
            if current:
                current_principal, _, _, _ = _row_ownership(current)
                _validate_row_identity(
                    current,
                    principal_key=principal_key,
                    ownership_scope=normalized_scope,
                    search_run_id=normalized_run_id,
                    packet_id=normalized_packet,
                )
                normalized_principal = current_principal
            recovered = False
            duplicate = False
            conflict = False
            if current:
                current["replayed_at"] = observed_iso
                current["replay_count"] = max(0, int(current.get("replay_count") or 0)) + 1
                current["version"] = max(0, int(current.get("version") or 0)) + 1
                if str(current.get("payload_sha256") or "") != payload_hash:
                    current["status"] = "replay_conflict"
                    current["last_error"] = "provider_event_payload_mismatch"
                    current["conflict_at"] = observed_iso
                    current["lease_owner"] = ""
                    current["lease_expires_at"] = None
                    conflict = True
                    duplicate = True
                elif current.get("processed_at") or str(current.get("status") or "") in _WEBHOOK_TERMINAL_STATUSES:
                    duplicate = True
                elif _lease_active(current, now=observed):
                    duplicate = True
                else:
                    recovered = True
            if not current:
                current = {
                    "provider": _WEBHOOK_PROVIDER,
                    "principal_id": normalized_principal,
                    "principal_key": principal_key,
                    "ownership_scope": normalized_scope,
                    "search_run_id": normalized_run_id,
                    "packet_id": normalized_packet,
                    "event_id": event_ref,
                    "event_type": str(payload.get("type") or payload.get("event") or payload.get("event_type") or ""),
                    "payload_sha256": payload_hash,
                    "payload_json": dict(payload),
                    "received_at": observed_iso,
                    "replay_count": 0,
                    "version": 1,
                }
            if not duplicate:
                current.update(
                    {
                        "status": "processing",
                        "lease_owner": owner,
                        "lease_expires_at": (
                            observed + timedelta(seconds=max(1, int(lease_seconds or 1)))
                        ).isoformat(),
                        "claimed_at": observed_iso,
                        "claim_recovered": recovered,
                        "updated_at": observed_iso,
                        **dict(extra or {}),
                    }
                )
            else:
                current["updated_at"] = observed_iso
            current.update(
                {
                    "principal_id": normalized_principal,
                    "principal_key": principal_key,
                    "ownership_scope": normalized_scope,
                    "search_run_id": normalized_run_id,
                    "packet_id": normalized_packet,
                    "event_id": event_ref,
                }
            )
            events[webhook_storage_key] = current
            if conflict:
                self._append_event_unlocked(
                    data,
                    packet_id=normalized_packet,
                    event_type="webhook_replay_conflict",
                    status="replay_conflict",
                    payload={"event_id": event_ref, "replayed_payload_sha256": payload_hash},
                    principal_id=normalized_principal,
                    principal_key=principal_key,
                    ownership_scope=normalized_scope,
                    search_run_id=normalized_run_id,
                    idempotency_key=(
                        f"{job['idempotency_key']}:webhook:{event_ref}:"
                        f"replay-conflict:{payload_hash}"
                    ),
                    created_at=observed_iso,
                )
            elif not duplicate:
                self._append_event_unlocked(
                    data,
                    packet_id=normalized_packet,
                    event_type="webhook_claim_recovered" if recovered else "webhook_received",
                    status="processing",
                    payload={"event_id": event_ref, "payload_sha256": payload_hash},
                    principal_id=normalized_principal,
                    principal_key=principal_key,
                    ownership_scope=normalized_scope,
                    search_run_id=normalized_run_id,
                    idempotency_key=(
                        f"{job['idempotency_key']}:webhook:{event_ref}:"
                        f"recovered:{current['version']}"
                        if recovered
                        else f"{job['idempotency_key']}:webhook:{event_ref}:received"
                    ),
                    created_at=observed_iso,
                )
            self._write_unlocked(data)
            return {
                "claimed": not duplicate,
                "duplicate": duplicate,
                "recovered": recovered,
                "conflict": conflict,
                "row": dict(current),
            }

    def complete_webhook_event(
        self,
        *,
        event_id: str,
        principal_id: str,
        ownership_scope: str,
        search_run_id: str,
        claim_owner: str,
        status: str,
        extra: dict[str, object] | None = None,
    ) -> dict[str, object]:
        event_ref = str(event_id or "").strip()
        owner = str(claim_owner or "").strip()
        _, principal_key, normalized_scope, normalized_run_id = _principal_ownership(
            principal_id,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
        )
        storage_key = _webhook_storage_key(
            principal_key=principal_key,
            ownership_scope=normalized_scope,
            search_run_id=normalized_run_id,
            event_id=event_ref,
        )
        with self._locked(exclusive=True):
            data = self._load_unlocked()
            events = data["webhook_events"]
            current = dict(events.get(storage_key) or {})
            if not current:
                raise ValueError("subscribr_webhook_event_not_found")
            if not owner or str(current.get("lease_owner") or "") != owner:
                raise PropertyContentJobClaimLostError("subscribr_webhook_claim_lost")
            row_principal, _, row_scope, row_run_id = _row_ownership(current)
            packet_id = str(current.get("packet_id") or "").strip()
            _validate_row_identity(
                current,
                principal_key=principal_key,
                ownership_scope=normalized_scope,
                search_run_id=normalized_run_id,
                packet_id=packet_id,
            )
            now = now_utc_iso()
            row = {**current, **dict(extra or {})}
            row.update(
                {
                    "status": str(status or "completed"),
                    "processed_at": now,
                    "updated_at": now,
                    "lease_owner": "",
                    "lease_expires_at": None,
                    "version": max(0, int(current.get("version") or 0)) + 1,
                }
            )
            events[storage_key] = row
            job_key = _job_idempotency_key(
                principal_key=principal_key,
                ownership_scope=row_scope,
                search_run_id=row_run_id,
                packet_id=packet_id,
            )
            self._append_event_unlocked(
                data,
                packet_id=packet_id,
                event_type="webhook_completed",
                status=str(row["status"]),
                payload={"event_id": event_ref, "version": row["version"]},
                principal_id=row_principal,
                principal_key=principal_key,
                ownership_scope=row_scope,
                search_run_id=row_run_id,
                idempotency_key=f"{job_key}:webhook:{event_ref}:completed",
                created_at=now,
            )
            self._write_unlocked(data)
            return dict(row)

    def fail_webhook_event(
        self,
        *,
        event_id: str,
        principal_id: str,
        ownership_scope: str,
        search_run_id: str,
        claim_owner: str,
        error: str,
    ) -> dict[str, object]:
        event_ref = str(event_id or "").strip()
        owner = str(claim_owner or "").strip()
        _, principal_key, normalized_scope, normalized_run_id = _principal_ownership(
            principal_id,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
        )
        storage_key = _webhook_storage_key(
            principal_key=principal_key,
            ownership_scope=normalized_scope,
            search_run_id=normalized_run_id,
            event_id=event_ref,
        )
        with self._locked(exclusive=True):
            data = self._load_unlocked()
            events = data["webhook_events"]
            current = dict(events.get(storage_key) or {})
            if not current or str(current.get("lease_owner") or "") != owner:
                raise PropertyContentJobClaimLostError("subscribr_webhook_claim_lost")
            _validate_row_identity(
                current,
                principal_key=principal_key,
                ownership_scope=normalized_scope,
                search_run_id=normalized_run_id,
                packet_id=str(current.get("packet_id") or ""),
            )
            now = now_utc_iso()
            row = {
                **current,
                "status": "retry",
                "last_error": str(error or "webhook_processing_failed")[:300],
                "updated_at": now,
                "lease_owner": "",
                "lease_expires_at": None,
                "version": max(0, int(current.get("version") or 0)) + 1,
            }
            events[storage_key] = row
            self._write_unlocked(data)
            return dict(row)

    def erase_principal_data(
        self,
        *,
        principal_id: str,
        principal_key: str,
    ) -> dict[str, object]:
        normalized_principal = str(principal_id or "").strip()
        expected_key = _property_search_principal_key(normalized_principal)
        if not normalized_principal or not expected_key:
            raise ValueError("property_content_principal_id_required")
        if expected_key != str(principal_key or "").strip():
            raise PropertyContentLedgerError("property_content_erasure_owner_mismatch")
        with self._locked(exclusive=True):
            data = self._load_unlocked()
            jobs = data["jobs"]
            ownership_rows = tuple(
                {
                    "principal_key": expected_key,
                    "ownership_scope": str(row.get("ownership_scope") or ""),
                    "search_run_id": str(row.get("search_run_id") or ""),
                    "packet_id": str(row.get("packet_id") or ""),
                }
                for row in jobs.values()
                if isinstance(row, dict)
                and str(row.get("principal_key") or "") == expected_key
            )
            receipt_files_deleted = _delete_receipt_files(ownership_rows)
            for storage_key, row in tuple(jobs.items()):
                if isinstance(row, dict) and str(row.get("principal_key") or "") == expected_key:
                    _row_ownership(row)
                    jobs.pop(storage_key, None)
            events = data["job_events"]
            retained_events = [
                row
                for row in events
                if not isinstance(row, dict)
                or str(row.get("principal_key") or "") != expected_key
            ]
            events_deleted = len(events) - len(retained_events)
            data["job_events"] = retained_events
            webhooks = data["webhook_events"]
            webhook_ids = tuple(
                event_id
                for event_id, row in webhooks.items()
                if isinstance(row, dict)
                and str(row.get("principal_key") or "") == expected_key
            )
            for event_id in webhook_ids:
                webhooks.pop(event_id, None)
            self._write_unlocked(data)
        return {
            "principal_id": normalized_principal,
            "ownership_rows": ownership_rows,
            "jobs_deleted": len(ownership_rows),
            "job_events_deleted": events_deleted,
            "webhook_events_deleted": len(webhook_ids),
            "receipt_files_deleted": receipt_files_deleted,
        }

    def resolve_webhook_job(
        self,
        *,
        packet_id: str = "",
        provider_idea_id: str = "",
        provider_script_id: str = "",
    ) -> dict[str, object]:
        normalized_packet = str(packet_id or "").strip()
        normalized_idea = str(provider_idea_id or "").strip()
        normalized_script = str(provider_script_id or "").strip()
        if not any((normalized_packet, normalized_idea, normalized_script)):
            return {"status": "missing", "match_count": 0}
        matches: list[dict[str, object]] = []
        for row in self.snapshot()["jobs"].values():
            if not isinstance(row, dict):
                continue
            _row_ownership(row)
            if normalized_packet and str(row.get("packet_id") or "") != normalized_packet:
                continue
            if normalized_idea and str(row.get("provider_idea_id") or "") != normalized_idea:
                continue
            if normalized_script and str(row.get("provider_script_id") or "") != normalized_script:
                continue
            matches.append(dict(row))
            if len(matches) > 1:
                break
        if not matches:
            return {"status": "missing", "match_count": 0}
        if len(matches) != 1:
            return {"status": "ambiguous", "match_count": len(matches)}
        return {"status": "resolved", "match_count": 1, "job": matches[0]}

    def export_principal_data(
        self,
        *,
        principal_id: str,
        limit: int,
    ) -> dict[str, object]:
        principal_key = _property_search_principal_key(str(principal_id or "").strip())
        data = self.snapshot()
        jobs = [
            dict(row)
            for row in data["jobs"].values()
            if isinstance(row, dict)
            and str(row.get("principal_key") or "") == principal_key
        ]
        jobs.sort(key=lambda row: str(row.get("updated_at") or ""), reverse=True)
        selected = jobs[:limit]
        identities = {
            (
                str(row.get("principal_key") or ""),
                str(row.get("ownership_scope") or ""),
                str(row.get("search_run_id") or ""),
                str(row.get("packet_id") or ""),
            )
            for row in selected
        }
        event_rows = [
            {
                "packet_id": str(row.get("packet_id") or ""),
                "event_type": str(row.get("event_type") or ""),
                "status": str(row.get("status") or ""),
                "created_at": str(row.get("created_at") or ""),
            }
            for row in data["job_events"]
            if isinstance(row, dict)
            and (
                str(row.get("principal_key") or ""),
                str(row.get("ownership_scope") or ""),
                str(row.get("search_run_id") or ""),
                str(row.get("packet_id") or ""),
            )
            in identities
        ][: limit * 10]
        webhook_rows = [
            {
                "packet_id": str(row.get("packet_id") or ""),
                "event_id": str(row.get("event_id") or ""),
                "event_type": str(row.get("event_type") or ""),
                "status": str(row.get("status") or ""),
                "received_at": str(row.get("received_at") or ""),
                "processed_at": str(row.get("processed_at") or ""),
            }
            for row in data["webhook_events"].values()
            if isinstance(row, dict)
            and str(row.get("principal_key") or "") == principal_key
        ][: limit * 5]
        return {
            "jobs": [_export_job_row(row) for row in selected],
            "job_events": event_rows,
            "webhook_events": webhook_rows,
            "job_count": len(jobs),
            "truncated": len(jobs) > limit,
        }

    def materialize_receipt(
        self,
        *,
        staged_path: Path,
        canonical_path: Path,
        backup_path: Path,
        packet: dict[str, object],
        principal_id: str,
        ownership_scope: str,
        search_run_id: str,
        status: str,
        extra: dict[str, object],
        after_promote: Callable[[], None] | None,
    ) -> dict[str, object]:
        packet_id = str(packet.get("packet_id") or "").strip()
        _, principal_key, normalized_scope, normalized_run_id = _principal_ownership(
            principal_id,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
        )
        storage_key = _job_storage_key(
            principal_key=principal_key,
            ownership_scope=normalized_scope,
            search_run_id=normalized_run_id,
            packet_id=packet_id,
        )
        promoted = False
        backed_up = False
        with self._locked(exclusive=True):
            data = self._load_unlocked()
            jobs = data["jobs"]
            row = _build_job_row(
                packet,
                principal_id=principal_id,
                ownership_scope=normalized_scope,
                search_run_id=normalized_run_id,
                status=status,
                current=dict(jobs.get(storage_key) or {}),
                extra=extra,
            )
            jobs[storage_key] = row
            self._append_event_unlocked(
                data,
                packet_id=packet_id,
                event_type="receipt_materialized",
                status=str(row["status"]),
                payload={"version": row["version"], "receipt_sha256": row.get("receipt_sha256")},
                principal_id=str(row["principal_id"]),
                principal_key=str(row["principal_key"]),
                ownership_scope=str(row["ownership_scope"]),
                search_run_id=str(row["search_run_id"]),
                idempotency_key=f"{row['idempotency_key']}:receipt:{row['version']}",
                created_at=str(row["updated_at"]),
            )
            try:
                if canonical_path.exists():
                    canonical_path.replace(backup_path)
                    backed_up = True
                staged_path.replace(canonical_path)
                promoted = True
                _fsync_directory(canonical_path.parent)
                if after_promote is not None:
                    after_promote()
                self._write_unlocked(data)
            except Exception:
                if promoted:
                    canonical_path.unlink(missing_ok=True)
                if backed_up and backup_path.exists():
                    backup_path.replace(canonical_path)
                _fsync_directory(canonical_path.parent)
                raise
        backup_path.unlink(missing_ok=True)
        return dict(row)


class _PostgresPropertyContentRepository:
    def __init__(self, database_url: str) -> None:
        self.database_url = str(database_url or "").strip()
        if not self.database_url:
            raise PropertyContentLedgerError("property_content_database_url_required")
        from app.product.property_search_schema import require_property_search_schema_ready

        require_property_search_schema_ready(self.database_url)

    def _connect(self):  # type: ignore[no-untyped-def]
        import psycopg

        return psycopg.connect(self.database_url, autocommit=True, connect_timeout=5)

    @contextmanager
    def _connection(self, authority_connection=None):  # type: ignore[no-untyped-def]
        if authority_connection is not None:
            yield authority_connection
            return
        with self._connect() as conn:
            yield conn

    @staticmethod
    def _configure_write_cursor(cur) -> None:  # type: ignore[no-untyped-def]
        cur.execute(
            "SELECT set_config('propertyquarry.property_search_erasure_key_id', %s, TRUE)",
            (_property_search_erasure_key_id(),),
        )
        cur.execute(
            "SELECT set_config('propertyquarry.property_content_writer_contract', %s, TRUE)",
            (_CONTENT_WRITER_CONTRACT,),
        )
        cur.execute(
            "SELECT set_config('propertyquarry.property_content_system_principal_key', %s, TRUE)",
            (_property_search_principal_key(PROPERTY_CONTENT_SYSTEM_PRINCIPAL_ID),),
        )

    @staticmethod
    def _json(value: dict[str, object]):  # type: ignore[no-untyped-def]
        from psycopg.types.json import Json

        return Json(value)

    @staticmethod
    def _advisory_key(kind: str, identity: str) -> str:
        return f"property-content:{kind}:{identity}"

    def _lock_cursor(self, cur, *, kind: str, identity: str, try_lock: bool = False) -> bool:  # type: ignore[no-untyped-def]
        function = "pg_try_advisory_xact_lock" if try_lock else "pg_advisory_xact_lock"
        cur.execute(
            f"SELECT {function}(hashtextextended(%s, %s))",
            (self._advisory_key(kind, identity), _CONTENT_LOCK_SEED),
        )
        row = cur.fetchone()
        return bool(row and row[0]) if try_lock else True

    def _append_event_cursor(
        self,
        cur,  # type: ignore[no-untyped-def]
        *,
        packet_id: str,
        event_type: str,
        status: str,
        payload: dict[str, object],
        principal_key: str,
        ownership_scope: str,
        search_run_id: str,
        idempotency_key: str,
        created_at: str,
    ) -> None:
        cur.execute(
            """
            INSERT INTO property_content_job_events
                (event_id, packet_id, principal_key, ownership_scope,
                 search_run_id, event_type, status, idempotency_key,
                 payload_json, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (
                principal_key,
                ownership_scope,
                search_run_id,
                idempotency_key
            ) DO NOTHING
            """,
            (
                _event_id(idempotency_key),
                packet_id,
                principal_key,
                ownership_scope,
                search_run_id,
                event_type,
                status,
                idempotency_key,
                self._json(payload),
                created_at,
            ),
        )

    @staticmethod
    def _validate_sql_row(
        row_json: object,
        *,
        principal_key: object,
        ownership_scope: object,
        search_run_id: object,
        packet_id: object,
    ) -> dict[str, object]:
        row = dict(row_json or {}) if isinstance(row_json, dict) else {}
        if not row:
            raise PropertyContentLedgerError("property_content_row_json_required")
        return _validate_row_identity(
            row,
            principal_key=str(principal_key or "").strip(),
            ownership_scope=str(ownership_scope or "").strip(),
            search_run_id=str(search_run_id or "").strip(),
            packet_id=str(packet_id or "").strip(),
        )

    def _load_job_cursor(
        self,
        cur,  # type: ignore[no-untyped-def]
        *,
        principal_key: str,
        ownership_scope: str,
        search_run_id: str,
        packet_id: str,
        for_update: bool = False,
        skip_locked: bool = False,
    ) -> dict[str, object] | None:
        suffix = " FOR UPDATE SKIP LOCKED" if skip_locked else (" FOR UPDATE" if for_update else "")
        cur.execute(
            """
            SELECT principal_key, ownership_scope, search_run_id, packet_id, row_json
            FROM property_content_jobs
            WHERE principal_key = %s
              AND ownership_scope = %s
              AND search_run_id = %s
              AND packet_id = %s
            """
            + suffix,
            (principal_key, ownership_scope, search_run_id, packet_id),
        )
        found = cur.fetchone()
        if not found:
            return None
        return self._validate_sql_row(
            found[4],
            principal_key=found[0],
            ownership_scope=found[1],
            search_run_id=found[2],
            packet_id=found[3],
        )

    def snapshot(self) -> dict[str, object]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT principal_key, ownership_scope, search_run_id,
                           packet_id, row_json
                    FROM property_content_jobs
                    ORDER BY updated_at DESC
                    """
                )
                jobs: dict[str, dict[str, object]] = {}
                job_principals: dict[tuple[str, str, str, str], str] = {}
                for principal_key, scope, run_id, packet_id, row_json in cur.fetchall():
                    row = self._validate_sql_row(
                        row_json,
                        principal_key=principal_key,
                        ownership_scope=scope,
                        search_run_id=run_id,
                        packet_id=packet_id,
                    )
                    storage_key = _job_storage_key(
                        principal_key=str(principal_key),
                        ownership_scope=str(scope),
                        search_run_id=str(run_id),
                        packet_id=str(packet_id),
                    )
                    jobs[storage_key] = row
                    job_principals[(str(principal_key), str(scope), str(run_id), str(packet_id))] = str(
                        row.get("principal_id") or ""
                    )
                cur.execute(
                    """
                    SELECT event_sequence, event_id, packet_id, principal_key,
                           ownership_scope, search_run_id, event_type, status,
                           idempotency_key, payload_json, created_at
                    FROM property_content_job_events
                    ORDER BY event_sequence
                    """
                )
                job_events: list[dict[str, object]] = []
                for values in cur.fetchall():
                    sequence, event_id, packet_id, principal_key, scope, run_id, event_type, status, idempotency_key, payload, created_at = values
                    identity = (str(principal_key), str(scope), str(run_id), str(packet_id))
                    principal_id = job_principals.get(identity)
                    if not principal_id:
                        raise PropertyContentLedgerError("property_content_job_event_parent_missing")
                    event = {
                        "event_sequence": int(sequence),
                        "event_id": str(event_id),
                        "packet_id": str(packet_id),
                        "principal_id": principal_id,
                        "principal_key": str(principal_key),
                        "ownership_scope": str(scope),
                        "search_run_id": str(run_id),
                        "event_type": str(event_type),
                        "status": str(status),
                        "idempotency_key": str(idempotency_key),
                        "payload_json": dict(payload or {}),
                        "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at),
                    }
                    _row_ownership(event)
                    job_events.append(event)
                cur.execute(
                    """
                    SELECT principal_key, ownership_scope, search_run_id,
                           packet_id, provider_event_id, row_json
                    FROM property_content_webhook_events
                    ORDER BY received_at
                    """
                )
                webhook_events: dict[str, dict[str, object]] = {}
                for principal_key, scope, run_id, packet_id, event_id, row_json in cur.fetchall():
                    row = self._validate_sql_row(
                        row_json,
                        principal_key=principal_key,
                        ownership_scope=scope,
                        search_run_id=run_id,
                        packet_id=packet_id,
                    )
                    storage_key = _webhook_storage_key(
                        principal_key=str(principal_key),
                        ownership_scope=str(scope),
                        search_run_id=str(run_id),
                        event_id=str(event_id),
                    )
                    webhook_events[storage_key] = row
        return {
            "contract_name": _LEDGER_CONTRACT,
            "jobs": jobs,
            "job_events": job_events,
            "webhook_events": webhook_events,
            "next_event_sequence": (job_events[-1]["event_sequence"] + 1) if job_events else 1,
        }

    def get_job(
        self,
        packet_id: str,
        *,
        principal_id: str,
        ownership_scope: str,
        search_run_id: str,
    ) -> dict[str, object] | None:
        _, principal_key, normalized_scope, normalized_run_id = _principal_ownership(
            principal_id,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                row = self._load_job_cursor(
                    cur,
                    principal_key=principal_key,
                    ownership_scope=normalized_scope,
                    search_run_id=normalized_run_id,
                    packet_id=str(packet_id or "").strip(),
                )
        return row

    def list_jobs(self, *, principal_id: str, limit: int) -> list[dict[str, object]]:
        normalized_principal = str(principal_id or "").strip()
        if not normalized_principal:
            raise ValueError("property_content_principal_id_required")
        principal_key = _property_search_principal_key(normalized_principal)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT principal_key, ownership_scope, search_run_id,
                           packet_id, row_json
                    FROM property_content_jobs
                    WHERE principal_key = %s
                    ORDER BY updated_at DESC, packet_id DESC
                    LIMIT %s
                    """,
                    (principal_key, limit),
                )
                rows = [
                    self._validate_sql_row(
                        row_json,
                        principal_key=row_key,
                        ownership_scope=scope,
                        search_run_id=run_id,
                        packet_id=packet_id,
                    )
                    for row_key, scope, run_id, packet_id, row_json in cur.fetchall()
                ]
        return rows

    def upsert_job(
        self,
        packet: dict[str, object],
        *,
        principal_id: str,
        ownership_scope: str,
        search_run_id: str,
        status: str,
        extra=None,
        authority_connection=None,
    ) -> dict[str, object]:  # type: ignore[no-untyped-def]
        packet_id = str(packet.get("packet_id") or "").strip()
        if not packet_id:
            raise ValueError("property_content_packet_id_required")
        _, principal_key, normalized_scope, normalized_run_id = _principal_ownership(
            principal_id,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
        )
        storage_key = _job_storage_key(
            principal_key=principal_key,
            ownership_scope=normalized_scope,
            search_run_id=normalized_run_id,
            packet_id=packet_id,
        )
        with self._connection(authority_connection) as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    self._configure_write_cursor(cur)
                    self._lock_cursor(cur, kind="job", identity=storage_key)
                    current = self._load_job_cursor(
                        cur,
                        principal_key=principal_key,
                        ownership_scope=normalized_scope,
                        search_run_id=normalized_run_id,
                        packet_id=packet_id,
                        for_update=True,
                    )
                    row = _build_job_row(
                        packet,
                        principal_id=principal_id,
                        ownership_scope=normalized_scope,
                        search_run_id=normalized_run_id,
                        status=status,
                        current=current or {},
                        extra=extra,
                    )
                    cur.execute(
                        """
                        INSERT INTO property_content_jobs
                            (packet_id, principal_key, ownership_scope,
                             search_run_id, idempotency_key, status,
                             source_packet_json, source_packet_sha256, row_json,
                             version, lease_owner, lease_expires_at, claimed_at,
                             created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s)
                        ON CONFLICT (
                            principal_key,
                            ownership_scope,
                            search_run_id,
                            packet_id
                        ) DO UPDATE SET
                            status = EXCLUDED.status,
                            source_packet_json = EXCLUDED.source_packet_json,
                            source_packet_sha256 = EXCLUDED.source_packet_sha256,
                            row_json = EXCLUDED.row_json,
                            version = EXCLUDED.version,
                            lease_owner = EXCLUDED.lease_owner,
                            lease_expires_at = EXCLUDED.lease_expires_at,
                            claimed_at = EXCLUDED.claimed_at,
                            updated_at = EXCLUDED.updated_at
                        """,
                        (
                            packet_id,
                            row["principal_key"],
                            row["ownership_scope"],
                            row["search_run_id"],
                            row["idempotency_key"],
                            row["status"],
                            self._json(dict(row["source_packet_json"])),
                            str(row["source_packet_canonical_sha256"]),
                            self._json(row),
                            row["version"],
                            row.get("lease_owner") or "",
                            row.get("lease_expires_at"),
                            row.get("claimed_at"),
                            row["created_at"],
                            row["updated_at"],
                        ),
                    )
                    self._append_event_cursor(
                        cur,
                        packet_id=packet_id,
                        event_type="job_upserted",
                        status=str(row["status"]),
                        payload={"version": row["version"], "status": row["status"]},
                        principal_key=str(row["principal_key"]),
                        ownership_scope=str(row["ownership_scope"]),
                        search_run_id=str(row["search_run_id"]),
                        idempotency_key=f"{row['idempotency_key']}:version:{row['version']}",
                        created_at=str(row["updated_at"]),
                    )
        return row

    def _update_existing_job(
        self,
        *,
        packet_id: str,
        principal_id: str,
        ownership_scope: str,
        search_run_id: str,
        transform,  # type: ignore[no-untyped-def]
        event_type: str,
        lease_owner: str = "",
        authority_connection=None,
    ) -> dict[str, object]:
        normalized_packet = str(packet_id or "").strip()
        row_principal, principal_key, normalized_scope, normalized_run_id = _principal_ownership(
            principal_id,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
        )
        storage_key = _job_storage_key(
            principal_key=principal_key,
            ownership_scope=normalized_scope,
            search_run_id=normalized_run_id,
            packet_id=normalized_packet,
        )
        with self._connection(authority_connection) as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    self._configure_write_cursor(cur)
                    self._lock_cursor(cur, kind="job", identity=storage_key)
                    current = self._load_job_cursor(
                        cur,
                        principal_key=principal_key,
                        ownership_scope=normalized_scope,
                        search_run_id=normalized_run_id,
                        packet_id=normalized_packet,
                        for_update=True,
                    )
                    if current is None:
                        raise ValueError("property_content_job_not_found")
                    owner = str(lease_owner or "").strip()
                    if owner and str(current.get("lease_owner") or "") != owner:
                        raise PropertyContentJobClaimLostError("property_content_job_claim_lost")
                    row = transform(current)
                    row.update(
                        {
                            "packet_id": normalized_packet,
                            "principal_id": row_principal,
                            "principal_key": principal_key,
                            "ownership_scope": normalized_scope,
                            "search_run_id": normalized_run_id,
                            "idempotency_key": current["idempotency_key"],
                        }
                    )
                    cur.execute(
                        """
                        UPDATE property_content_jobs
                        SET status = %s, row_json = %s, version = %s,
                            lease_owner = %s, lease_expires_at = %s,
                            claimed_at = %s, updated_at = %s
                        WHERE principal_key = %s
                          AND ownership_scope = %s
                          AND search_run_id = %s
                          AND packet_id = %s
                        """,
                        (
                            row["status"],
                            self._json(row),
                            row["version"],
                            row.get("lease_owner") or "",
                            row.get("lease_expires_at"),
                            row.get("claimed_at"),
                            row["updated_at"],
                            principal_key,
                            normalized_scope,
                            normalized_run_id,
                            normalized_packet,
                        ),
                    )
                    self._append_event_cursor(
                        cur,
                        packet_id=normalized_packet,
                        event_type=event_type,
                        status=str(row["status"]),
                        payload={"version": row["version"], "status": row["status"]},
                        principal_key=principal_key,
                        ownership_scope=normalized_scope,
                        search_run_id=normalized_run_id,
                        idempotency_key=f"{row['idempotency_key']}:version:{row['version']}",
                        created_at=str(row["updated_at"]),
                    )
        return row

    def record_provider_ids(self, *, packet_id: str, principal_id: str, ownership_scope: str, search_run_id: str, provider_channel_id="", provider_idea_id="", provider_script_id="", status="PROVIDER_JOB_CREATED", lease_owner="", authority_connection=None):  # type: ignore[no-untyped-def]
        normalized_packet = str(packet_id or "").strip()

        def transform(current):  # type: ignore[no-untyped-def]
            now = now_utc_iso()
            owner = str(lease_owner or "").strip()
            return {
                **current,
                "provider": _WEBHOOK_PROVIDER,
                "provider_channel_id": str(provider_channel_id or current.get("provider_channel_id") or ""),
                "provider_idea_id": str(provider_idea_id or current.get("provider_idea_id") or ""),
                "provider_script_id": str(provider_script_id or current.get("provider_script_id") or ""),
                "status": str(status or "PROVIDER_JOB_CREATED"),
                "updated_at": now,
                "version": max(0, int(current.get("version") or 0)) + 1,
                "lease_owner": "" if owner else str(current.get("lease_owner") or ""),
                "lease_expires_at": None if owner else current.get("lease_expires_at"),
            }

        return self._update_existing_job(
            packet_id=normalized_packet,
            principal_id=principal_id,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
            transform=transform,
            event_type="provider_ids_recorded",
            lease_owner=str(lease_owner or ""),
            authority_connection=authority_connection,
        )

    def claim_job(self, packet_id: str, *, principal_id: str, ownership_scope: str, search_run_id: str, lease_owner: str, lease_seconds: int, now=None, authority_connection=None):  # type: ignore[no-untyped-def]
        normalized_packet = str(packet_id or "").strip()
        owner = str(lease_owner or "").strip()
        observed = _as_utc(now)
        if not normalized_packet or not owner:
            raise ValueError("property_content_job_claim_identity_required")
        row_principal, principal_key, normalized_scope, normalized_run_id = _principal_ownership(
            principal_id,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
        )
        storage_key = _job_storage_key(
            principal_key=principal_key,
            ownership_scope=normalized_scope,
            search_run_id=normalized_run_id,
            packet_id=normalized_packet,
        )
        with self._connection(authority_connection) as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    self._configure_write_cursor(cur)
                    if not self._lock_cursor(cur, kind="job-claim", identity=storage_key, try_lock=True):
                        return None
                    current = self._load_job_cursor(
                        cur,
                        principal_key=principal_key,
                        ownership_scope=normalized_scope,
                        search_run_id=normalized_run_id,
                        packet_id=normalized_packet,
                        skip_locked=True,
                    )
                    if current is None:
                        return None
                    if _lease_active(current, now=observed):
                        return current if str(current.get("lease_owner") or "") == owner else None
                    recovered = bool(str(current.get("lease_owner") or ""))
                    claimed_at = observed.isoformat()
                    row = {
                        **current,
                        "lease_owner": owner,
                        "lease_expires_at": (observed + timedelta(seconds=max(1, int(lease_seconds or 1)))).isoformat(),
                        "claimed_at": claimed_at,
                        "claim_recovered": recovered,
                        "updated_at": claimed_at,
                        "version": max(0, int(current.get("version") or 0)) + 1,
                    }
                    cur.execute(
                        """
                        UPDATE property_content_jobs
                        SET row_json = %s, version = %s, lease_owner = %s,
                            lease_expires_at = %s, claimed_at = %s, updated_at = %s
                        WHERE packet_id = %s
                          AND principal_key = %s
                          AND ownership_scope = %s
                          AND search_run_id = %s
                        """,
                        (
                            self._json(row), row["version"], owner, row["lease_expires_at"],
                            claimed_at, claimed_at, normalized_packet,
                            principal_key, normalized_scope, normalized_run_id,
                        ),
                    )
                    self._append_event_cursor(
                        cur,
                        packet_id=normalized_packet,
                        event_type="job_claim_recovered" if recovered else "job_claimed",
                        status=str(row.get("status") or ""),
                        payload={"version": row["version"], "lease_owner": owner},
                        principal_key=principal_key,
                        ownership_scope=normalized_scope,
                        search_run_id=normalized_run_id,
                        idempotency_key=f"{row['idempotency_key']}:claim:{row['version']}",
                        created_at=claimed_at,
                    )
        return row

    def update_claimed_job(self, packet_id: str, *, principal_id: str, ownership_scope: str, search_run_id: str, lease_owner: str, status: str, extra=None, release=True, authority_connection=None):  # type: ignore[no-untyped-def]
        normalized_packet = str(packet_id or "").strip()
        owner = str(lease_owner or "").strip()

        def transform(current):  # type: ignore[no-untyped-def]
            now = now_utc_iso()
            row = {**current, **dict(extra or {})}
            row.update(
                {
                    "packet_id": normalized_packet,
                    "idempotency_key": current["idempotency_key"],
                    "status": str(status or current.get("status") or "UNKNOWN"),
                    "updated_at": now,
                    "version": max(0, int(current.get("version") or 0)) + 1,
                    "lease_owner": "" if release else owner,
                    "lease_expires_at": None if release else current.get("lease_expires_at"),
                }
            )
            return row

        return self._update_existing_job(
            packet_id=normalized_packet,
            principal_id=principal_id,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
            transform=transform,
            event_type="job_claim_completed" if release else "job_claim_updated",
            lease_owner=owner,
            authority_connection=authority_connection,
        )

    def webhook_seen(self, event_id: str, *, principal_id: str, ownership_scope: str, search_run_id: str) -> bool:
        _, principal_key, normalized_scope, normalized_run_id = _principal_ownership(
            principal_id,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1 FROM property_content_webhook_events
                    WHERE principal_key = %s
                      AND ownership_scope = %s
                      AND search_run_id = %s
                      AND provider = %s
                      AND provider_event_id = %s
                    """,
                    (principal_key, normalized_scope, normalized_run_id, _WEBHOOK_PROVIDER, str(event_id or "").strip()),
                )
                return cur.fetchone() is not None

    def claim_webhook_event(self, *, event_id: str, payload: dict[str, object], principal_id: str, ownership_scope: str, search_run_id: str, packet_id: str, extra, claim_owner: str, lease_seconds: int, now=None, authority_connection=None):  # type: ignore[no-untyped-def]
        event_ref = str(event_id or "").strip()
        owner = str(claim_owner or "").strip()
        if not event_ref or not owner:
            raise ValueError("subscribr_webhook_claim_identity_required")
        observed = _as_utc(now)
        observed_iso = observed.isoformat()
        payload_hash = sha256_json(payload)
        normalized_packet = str(packet_id or "").strip()
        normalized_principal, principal_key, normalized_scope, normalized_run_id = _principal_ownership(
            principal_id,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
        )
        job_storage_key = _job_storage_key(
            principal_key=principal_key,
            ownership_scope=normalized_scope,
            search_run_id=normalized_run_id,
            packet_id=normalized_packet,
        )
        webhook_storage_key = _webhook_storage_key(
            principal_key=principal_key,
            ownership_scope=normalized_scope,
            search_run_id=normalized_run_id,
            event_id=event_ref,
        )
        with self._connection(authority_connection) as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    self._configure_write_cursor(cur)
                    job = self._load_job_cursor(
                        cur,
                        principal_key=principal_key,
                        ownership_scope=normalized_scope,
                        search_run_id=normalized_run_id,
                        packet_id=normalized_packet,
                    )
                    if job is None:
                        raise ValueError("property_content_job_not_found")
                    if not self._lock_cursor(cur, kind="webhook", identity=webhook_storage_key, try_lock=True):
                        return {"claimed": False, "duplicate": True, "recovered": False, "conflict": False, "row": {"event_id": event_ref, "status": "claim_contended"}}
                    cur.execute(
                        """
                        SELECT row_json
                        FROM property_content_webhook_events
                        WHERE principal_key = %s
                          AND ownership_scope = %s
                          AND search_run_id = %s
                          AND provider = %s
                          AND provider_event_id = %s
                        FOR UPDATE SKIP LOCKED
                        """,
                        (principal_key, normalized_scope, normalized_run_id, _WEBHOOK_PROVIDER, event_ref),
                    )
                    found = cur.fetchone()
                    current = dict(found[0] or {}) if found else {}
                    if current:
                        current_principal, _, _, _ = _row_ownership(current)
                        _validate_row_identity(
                            current,
                            principal_key=principal_key,
                            ownership_scope=normalized_scope,
                            search_run_id=normalized_run_id,
                            packet_id=normalized_packet,
                        )
                        normalized_principal = current_principal
                    recovered = False
                    duplicate = False
                    conflict = False
                    if current:
                        current["replayed_at"] = observed_iso
                        current["replay_count"] = max(0, int(current.get("replay_count") or 0)) + 1
                        current["version"] = max(0, int(current.get("version") or 0)) + 1
                        if str(current.get("payload_sha256") or "") != payload_hash:
                            current["status"] = "replay_conflict"
                            current["last_error"] = "provider_event_payload_mismatch"
                            current["conflict_at"] = observed_iso
                            current["lease_owner"] = ""
                            current["lease_expires_at"] = None
                            conflict = True
                            duplicate = True
                        elif current.get("processed_at") or str(current.get("status") or "") in _WEBHOOK_TERMINAL_STATUSES:
                            duplicate = True
                        elif _lease_active(current, now=observed):
                            duplicate = True
                        else:
                            recovered = True
                    if not current:
                        current = {
                            "provider": _WEBHOOK_PROVIDER,
                            "principal_id": normalized_principal,
                            "principal_key": principal_key,
                            "ownership_scope": normalized_scope,
                            "search_run_id": normalized_run_id,
                            "packet_id": normalized_packet,
                            "event_id": event_ref,
                            "event_type": str(payload.get("type") or payload.get("event") or payload.get("event_type") or ""),
                            "payload_sha256": payload_hash,
                            "payload_json": dict(payload),
                            "received_at": observed_iso,
                            "replay_count": 0,
                            "version": 1,
                        }
                    if not duplicate:
                        current.update(
                            {
                                "status": "processing",
                                "lease_owner": owner,
                                "lease_expires_at": (observed + timedelta(seconds=max(1, int(lease_seconds or 1)))).isoformat(),
                                "claimed_at": observed_iso,
                                "claim_recovered": recovered,
                                "updated_at": observed_iso,
                                **dict(extra or {}),
                            }
                        )
                    else:
                        current["updated_at"] = observed_iso
                    current.update(
                        {
                            "principal_id": normalized_principal,
                            "principal_key": principal_key,
                            "ownership_scope": normalized_scope,
                            "search_run_id": normalized_run_id,
                            "packet_id": normalized_packet,
                            "event_id": event_ref,
                        }
                    )
                    if found:
                        cur.execute(
                            """
                            UPDATE property_content_webhook_events
                            SET event_type = %s, status = %s, row_json = %s, version = %s,
                                lease_owner = %s, lease_expires_at = %s, claimed_at = %s,
                                replayed_at = %s, updated_at = %s
                            WHERE principal_key = %s
                              AND ownership_scope = %s
                              AND search_run_id = %s
                              AND provider = %s
                              AND provider_event_id = %s
                            """,
                            (
                                current.get("event_type") or "", current.get("status") or "processing",
                                self._json(current), current["version"], current.get("lease_owner") or "",
                                current.get("lease_expires_at"), current.get("claimed_at"), current.get("replayed_at"),
                                current["updated_at"], principal_key, normalized_scope,
                                normalized_run_id, _WEBHOOK_PROVIDER, event_ref,
                            ),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO property_content_webhook_events
                                (provider, provider_event_id, principal_key, ownership_scope,
                                 search_run_id, packet_id, event_type, status, payload_sha256,
                                 row_json, version, lease_owner, lease_expires_at, claimed_at,
                                 received_at, updated_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                    %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (
                                principal_key,
                                ownership_scope,
                                search_run_id,
                                provider,
                                provider_event_id
                            ) DO NOTHING
                            """,
                            (
                                _WEBHOOK_PROVIDER, event_ref, principal_key, normalized_scope,
                                normalized_run_id, normalized_packet, current["event_type"], current["status"],
                                payload_hash, self._json(current), current["version"], owner,
                                current["lease_expires_at"], observed_iso, observed_iso, observed_iso,
                            ),
                        )
                        if cur.rowcount != 1:
                            return {"claimed": False, "duplicate": True, "recovered": False, "conflict": False, "row": {"event_id": event_ref, "status": "claim_contended"}}
                    if conflict:
                        original_payload = dict(current.get("payload_json") or {})
                        self._append_event_cursor(
                            cur,
                            packet_id=normalized_packet,
                            event_type="webhook_replay_conflict",
                            status="replay_conflict",
                            payload={"event_id": event_ref, "replayed_payload_sha256": payload_hash},
                            principal_key=principal_key,
                            ownership_scope=normalized_scope,
                            search_run_id=normalized_run_id,
                            idempotency_key=f"{job['idempotency_key']}:webhook:{event_ref}:replay-conflict:{payload_hash}",
                            created_at=observed_iso,
                        )
                    elif not duplicate:
                        self._append_event_cursor(
                            cur,
                            packet_id=normalized_packet,
                            event_type="webhook_claim_recovered" if recovered else "webhook_received",
                            status="processing",
                            payload={"event_id": event_ref, "payload_sha256": payload_hash},
                            principal_key=principal_key,
                            ownership_scope=normalized_scope,
                            search_run_id=normalized_run_id,
                            idempotency_key=(
                                f"{job['idempotency_key']}:webhook:{event_ref}:recovered:{current['version']}"
                                if recovered
                                else f"{job['idempotency_key']}:webhook:{event_ref}:received"
                            ),
                            created_at=observed_iso,
                        )
        return {"claimed": not duplicate, "duplicate": duplicate, "recovered": recovered, "conflict": conflict, "row": current}

    def _finish_webhook(self, *, event_id: str, principal_id: str, ownership_scope: str, search_run_id: str, claim_owner: str, status: str, extra=None, retry=False, authority_connection=None):  # type: ignore[no-untyped-def]
        event_ref = str(event_id or "").strip()
        owner = str(claim_owner or "").strip()
        row_principal, principal_key, normalized_scope, normalized_run_id = _principal_ownership(
            principal_id,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
        )
        storage_key = _webhook_storage_key(
            principal_key=principal_key,
            ownership_scope=normalized_scope,
            search_run_id=normalized_run_id,
            event_id=event_ref,
        )
        with self._connection(authority_connection) as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    self._configure_write_cursor(cur)
                    self._lock_cursor(cur, kind="webhook", identity=storage_key)
                    cur.execute(
                        """
                        SELECT row_json FROM property_content_webhook_events
                        WHERE principal_key = %s
                          AND ownership_scope = %s
                          AND search_run_id = %s
                          AND provider = %s
                          AND provider_event_id = %s
                        FOR UPDATE
                        """,
                        (principal_key, normalized_scope, normalized_run_id, _WEBHOOK_PROVIDER, event_ref),
                    )
                    found = cur.fetchone()
                    current = dict(found[0] or {}) if found else {}
                    if not current or str(current.get("lease_owner") or "") != owner:
                        raise PropertyContentJobClaimLostError("subscribr_webhook_claim_lost")
                    packet_id = str(current.get("packet_id") or "").strip()
                    _validate_row_identity(
                        current,
                        principal_key=principal_key,
                        ownership_scope=normalized_scope,
                        search_run_id=normalized_run_id,
                        packet_id=packet_id,
                    )
                    now = now_utc_iso()
                    row = {**current, **dict(extra or {})}
                    row.update(
                        {
                            "status": status,
                            "updated_at": now,
                            "lease_owner": "",
                            "lease_expires_at": None,
                            "version": max(0, int(current.get("version") or 0)) + 1,
                        }
                    )
                    if not retry:
                        row["processed_at"] = now
                    cur.execute(
                        """
                        UPDATE property_content_webhook_events
                        SET status = %s, row_json = %s, version = %s,
                            lease_owner = '', lease_expires_at = NULL,
                            processed_at = %s, updated_at = %s
                        WHERE principal_key = %s
                          AND ownership_scope = %s
                          AND search_run_id = %s
                          AND provider = %s
                          AND provider_event_id = %s
                        """,
                        (
                            status, self._json(row), row["version"], row.get("processed_at"), now,
                            principal_key, normalized_scope, normalized_run_id,
                            _WEBHOOK_PROVIDER, event_ref,
                        ),
                    )
                    if not retry:
                        self._append_event_cursor(
                            cur,
                            packet_id=packet_id,
                            event_type="webhook_completed",
                            status=status,
                            payload={"event_id": event_ref, "version": row["version"]},
                            principal_key=principal_key,
                            ownership_scope=normalized_scope,
                            search_run_id=normalized_run_id,
                            idempotency_key=(
                                f"{_job_idempotency_key(principal_key=principal_key, ownership_scope=normalized_scope, search_run_id=normalized_run_id, packet_id=packet_id)}"
                                f":webhook:{event_ref}:completed"
                            ),
                            created_at=now,
                        )
        return row

    def complete_webhook_event(self, *, event_id: str, principal_id: str, ownership_scope: str, search_run_id: str, claim_owner: str, status: str, extra=None, authority_connection=None):  # type: ignore[no-untyped-def]
        return self._finish_webhook(event_id=event_id, principal_id=principal_id, ownership_scope=ownership_scope, search_run_id=search_run_id, claim_owner=claim_owner, status=status, extra=extra, authority_connection=authority_connection)

    def fail_webhook_event(self, *, event_id: str, principal_id: str, ownership_scope: str, search_run_id: str, claim_owner: str, error: str, authority_connection=None):  # type: ignore[no-untyped-def]
        return self._finish_webhook(
            event_id=event_id,
            principal_id=principal_id,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
            claim_owner=claim_owner,
            status="retry",
            extra={"last_error": str(error or "webhook_processing_failed")[:300]},
            retry=True,
            authority_connection=authority_connection,
        )

    def resolve_webhook_job(self, *, packet_id="", provider_idea_id="", provider_script_id=""):  # type: ignore[no-untyped-def]
        normalized_packet = str(packet_id or "").strip()
        normalized_idea = str(provider_idea_id or "").strip()
        normalized_script = str(provider_script_id or "").strip()
        if not any((normalized_packet, normalized_idea, normalized_script)):
            return {"status": "missing", "match_count": 0}
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT principal_key, ownership_scope, search_run_id,
                           packet_id, row_json
                    FROM property_content_jobs
                    WHERE (%s = '' OR packet_id = %s)
                      AND (%s = '' OR row_json->>'provider_idea_id' = %s)
                      AND (%s = '' OR row_json->>'provider_script_id' = %s)
                    ORDER BY updated_at DESC
                    LIMIT 2
                    """,
                    (
                        normalized_packet, normalized_packet,
                        normalized_idea, normalized_idea,
                        normalized_script, normalized_script,
                    ),
                )
                matches = [
                    self._validate_sql_row(
                        row_json,
                        principal_key=principal_key,
                        ownership_scope=scope,
                        search_run_id=run_id,
                        packet_id=found_packet,
                    )
                    for principal_key, scope, run_id, found_packet, row_json in cur.fetchall()
                ]
        if not matches:
            return {"status": "missing", "match_count": 0}
        if len(matches) != 1:
            return {"status": "ambiguous", "match_count": len(matches)}
        return {"status": "resolved", "match_count": 1, "job": matches[0]}

    def erase_principal_data(self, *, principal_id: str, principal_key: str) -> dict[str, object]:
        normalized_principal = str(principal_id or "").strip()
        expected_key = _property_search_principal_key(normalized_principal)
        if not expected_key or expected_key != str(principal_key or "").strip():
            raise PropertyContentLedgerError("property_content_erasure_owner_mismatch")
        with self._connect() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    self._configure_write_cursor(cur)
                    cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended('property_search_erasure:' || %s, 0))",
                        (expected_key,),
                    )
                    cur.execute(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM property_search_erasure_fences
                            WHERE principal_key = %s AND run_id = ''
                        )
                        """,
                        (expected_key,),
                    )
                    fence = cur.fetchone()
                    if not fence or not bool(fence[0]):
                        raise PropertyContentLedgerError("property_content_account_erasure_fence_required")
                    cur.execute(
                        """
                        SELECT principal_key, ownership_scope, search_run_id,
                               packet_id, row_json
                        FROM property_content_jobs
                        WHERE principal_key = %s
                        FOR UPDATE
                        """,
                        (expected_key,),
                    )
                    rows = [
                        self._validate_sql_row(
                            row_json,
                            principal_key=row_key,
                            ownership_scope=scope,
                            search_run_id=run_id,
                            packet_id=packet_id,
                        )
                        for row_key, scope, run_id, packet_id, row_json in cur.fetchall()
                    ]
                    ownership_rows = tuple(
                        {
                            "principal_key": expected_key,
                            "ownership_scope": str(row.get("ownership_scope") or ""),
                            "search_run_id": str(row.get("search_run_id") or ""),
                            "packet_id": str(row.get("packet_id") or ""),
                        }
                        for row in rows
                    )
                    receipt_files_deleted = _delete_receipt_files(ownership_rows)
                    cur.execute(
                        "SELECT COUNT(*) FROM property_content_job_events WHERE principal_key = %s",
                        (expected_key,),
                    )
                    events_deleted = int((cur.fetchone() or (0,))[0] or 0)
                    cur.execute(
                        "SELECT COUNT(*) FROM property_content_webhook_events WHERE principal_key = %s",
                        (expected_key,),
                    )
                    webhooks_deleted = int((cur.fetchone() or (0,))[0] or 0)
                    cur.execute(
                        "DELETE FROM property_content_jobs WHERE principal_key = %s",
                        (expected_key,),
                    )
                    jobs_deleted = max(0, int(cur.rowcount or 0))
        return {
            "principal_id": normalized_principal,
            "ownership_rows": ownership_rows,
            "jobs_deleted": jobs_deleted,
            "job_events_deleted": events_deleted,
            "webhook_events_deleted": webhooks_deleted,
            "receipt_files_deleted": receipt_files_deleted,
        }

    def export_principal_data(self, *, principal_id: str, limit: int) -> dict[str, object]:
        normalized_principal = str(principal_id or "").strip()
        if not normalized_principal:
            raise ValueError("property_content_principal_id_required")
        principal_key = _property_search_principal_key(normalized_principal)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM property_content_jobs WHERE principal_key = %s",
                    (principal_key,),
                )
                job_count = int((cur.fetchone() or (0,))[0] or 0)
                cur.execute(
                    """
                    SELECT principal_key, ownership_scope, search_run_id,
                           packet_id, row_json
                    FROM property_content_jobs
                    WHERE principal_key = %s
                    ORDER BY updated_at DESC, packet_id DESC
                    LIMIT %s
                    """,
                    (principal_key, limit),
                )
                jobs = [
                    self._validate_sql_row(
                        row_json,
                        principal_key=row_key,
                        ownership_scope=scope,
                        search_run_id=run_id,
                        packet_id=packet_id,
                    )
                    for row_key, scope, run_id, packet_id, row_json in cur.fetchall()
                ]
                cur.execute(
                    """
                    SELECT events.packet_id, events.event_type, events.status,
                           events.created_at, jobs.row_json,
                           events.principal_key, events.ownership_scope,
                           events.search_run_id
                    FROM property_content_job_events AS events
                    JOIN property_content_jobs AS jobs
                      ON jobs.principal_key = events.principal_key
                     AND jobs.ownership_scope = events.ownership_scope
                     AND jobs.search_run_id = events.search_run_id
                     AND jobs.packet_id = events.packet_id
                    WHERE events.principal_key = %s
                    ORDER BY events.event_sequence
                    LIMIT %s
                    """,
                    (principal_key, limit * 10),
                )
                event_rows = []
                for packet_id, event_type, status, created_at, parent_json, row_key, scope, run_id in cur.fetchall():
                    self._validate_sql_row(
                        parent_json,
                        principal_key=row_key,
                        ownership_scope=scope,
                        search_run_id=run_id,
                        packet_id=packet_id,
                    )
                    event_rows.append(
                        {
                            "packet_id": str(packet_id),
                            "event_type": str(event_type),
                            "status": str(status),
                            "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at),
                        }
                    )
                cur.execute(
                    """
                    SELECT principal_key, ownership_scope, search_run_id,
                           packet_id, row_json
                    FROM property_content_webhook_events
                    WHERE principal_key = %s
                    ORDER BY received_at
                    LIMIT %s
                    """,
                    (principal_key, limit * 5),
                )
                webhook_rows = []
                for row_key, scope, run_id, packet_id, row_json in cur.fetchall():
                    row = self._validate_sql_row(
                        row_json,
                        principal_key=row_key,
                        ownership_scope=scope,
                        search_run_id=run_id,
                        packet_id=packet_id,
                    )
                    webhook_rows.append(
                        {
                            "packet_id": str(packet_id),
                            "event_id": str(row.get("event_id") or ""),
                            "event_type": str(row.get("event_type") or ""),
                            "status": str(row.get("status") or ""),
                            "received_at": str(row.get("received_at") or ""),
                            "processed_at": str(row.get("processed_at") or ""),
                        }
                    )
        return {
            "jobs": [_export_job_row(row) for row in jobs],
            "job_events": event_rows,
            "webhook_events": webhook_rows,
            "job_count": job_count,
            "truncated": job_count > limit,
        }


class PropertyContentJobLedger:
    """Governed content ledger with durable Postgres and locked file development modes."""

    def __init__(
        self,
        *,
        path: str | Path | None = None,
        database_url: str = "",
        backend: str = "",
    ) -> None:
        resolved_backend = str(backend or os.getenv("EA_STORAGE_BACKEND") or "auto").strip().lower()
        resolved_url = str(database_url or os.getenv("DATABASE_URL") or "").strip()
        runtime_mode = str(
            os.getenv("EA_RUNTIME_MODE")
            or os.getenv("PROPERTYQUARRY_RUNTIME_MODE")
            or os.getenv("ENVIRONMENT")
            or "dev"
        ).strip().lower()
        production = runtime_mode in {"prod", "production"}
        if path is not None:
            if production:
                raise PropertyContentLedgerError("property_content_postgres_required_in_prod")
            self._repository = _FilePropertyContentRepository(Path(path))
            self._backend = "file"
        elif resolved_backend == "postgres" or (resolved_backend == "auto" and resolved_url):
            self._repository = _PostgresPropertyContentRepository(resolved_url)
            self._backend = "postgres"
        else:
            if production or resolved_backend == "file" and production:
                raise PropertyContentLedgerError("property_content_postgres_required_in_prod")
            self._repository = _FilePropertyContentRepository(default_property_content_ledger_path())
            self._backend = "file"

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def path(self) -> Path:
        return getattr(self._repository, "path", Path("postgres-property-content-ledger"))

    def _load(self) -> dict[str, object]:
        return self._repository.snapshot()

    @contextmanager
    def publication_authority(
        self,
        *,
        principal_id: str,
        search_run_id: str,
    ):
        with property_account_publication_authority(
            principal_id,
            run_id=search_run_id,
        ) as connection:
            yield connection

    def _write_repository(
        self,
        method_name: str,
        *,
        authority_principal_id: str,
        authority_run_id: str,
        authority_connection=None,  # type: ignore[no-untyped-def]
        **kwargs: object,
    ):
        method = getattr(self._repository, method_name)
        if authority_connection is not None:
            if self._backend != "postgres":
                raise PropertyContentLedgerError("property_content_authority_connection_backend_mismatch")
            return method(authority_connection=authority_connection, **kwargs)
        with self.publication_authority(
            principal_id=authority_principal_id,
            search_run_id=authority_run_id,
        ) as connection:
            if self._backend == "postgres":
                return method(authority_connection=connection, **kwargs)
            return method(**kwargs)

    def get_job(
        self,
        packet_id: str,
        *,
        principal_id: str,
        ownership_scope: str,
        search_run_id: str,
    ) -> dict[str, object] | None:
        return self._repository.get_job(
            packet_id,
            principal_id=principal_id,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
        )

    def list_jobs(self, *, principal_id: str, limit: int = 100) -> list[dict[str, object]]:
        normalized_limit = max(1, min(1000, int(limit or 100)))
        return self._repository.list_jobs(
            principal_id=principal_id,
            limit=normalized_limit,
        )

    def upsert_job(
        self,
        packet: dict[str, object],
        *,
        principal_id: str,
        ownership_scope: str,
        search_run_id: str,
        status: str,
        extra=None,  # type: ignore[no-untyped-def]
        authority_connection=None,  # type: ignore[no-untyped-def]
    ) -> dict[str, object]:
        return self._write_repository(
            "upsert_job",
            authority_principal_id=principal_id,
            authority_run_id=search_run_id,
            authority_connection=authority_connection,
            packet=packet,
            principal_id=principal_id,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
            status=status,
            extra=extra,
        )

    def record_provider_ids(self, **kwargs):  # type: ignore[no-untyped-def]
        principal_id = str(kwargs.get("principal_id") or "").strip()
        search_run_id = str(kwargs.get("search_run_id") or "").strip()
        authority_connection = kwargs.pop("authority_connection", None)
        return self._write_repository(
            "record_provider_ids",
            authority_principal_id=principal_id,
            authority_run_id=search_run_id,
            authority_connection=authority_connection,
            **kwargs,
        )

    def claim_job(
        self,
        packet_id: str,
        *,
        principal_id: str,
        ownership_scope: str,
        search_run_id: str,
        lease_owner: str,
        lease_seconds: int,
        now=None,  # type: ignore[no-untyped-def]
        authority_connection=None,  # type: ignore[no-untyped-def]
    ):
        return self._write_repository(
            "claim_job",
            authority_principal_id=principal_id,
            authority_run_id=search_run_id,
            authority_connection=authority_connection,
            packet_id=packet_id,
            principal_id=principal_id,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
            lease_owner=lease_owner,
            lease_seconds=lease_seconds,
            now=now,
        )

    def update_claimed_job(self, packet_id: str, **kwargs):  # type: ignore[no-untyped-def]
        principal_id = str(kwargs.get("principal_id") or "").strip()
        search_run_id = str(kwargs.get("search_run_id") or "").strip()
        authority_connection = kwargs.pop("authority_connection", None)
        return self._write_repository(
            "update_claimed_job",
            authority_principal_id=principal_id,
            authority_run_id=search_run_id,
            authority_connection=authority_connection,
            packet_id=packet_id,
            **kwargs,
        )

    def webhook_seen(
        self,
        event_id: str,
        *,
        principal_id: str,
        ownership_scope: str,
        search_run_id: str,
    ) -> bool:
        return self._repository.webhook_seen(
            event_id,
            principal_id=principal_id,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
        )

    def claim_webhook_event(self, **kwargs):  # type: ignore[no-untyped-def]
        principal_id = str(kwargs.get("principal_id") or "").strip()
        search_run_id = str(kwargs.get("search_run_id") or "").strip()
        authority_connection = kwargs.pop("authority_connection", None)
        return self._write_repository(
            "claim_webhook_event",
            authority_principal_id=principal_id,
            authority_run_id=search_run_id,
            authority_connection=authority_connection,
            **kwargs,
        )

    def complete_webhook_event(self, **kwargs):  # type: ignore[no-untyped-def]
        principal_id = str(kwargs.get("principal_id") or "").strip()
        search_run_id = str(kwargs.get("search_run_id") or "").strip()
        authority_connection = kwargs.pop("authority_connection", None)
        return self._write_repository(
            "complete_webhook_event",
            authority_principal_id=principal_id,
            authority_run_id=search_run_id,
            authority_connection=authority_connection,
            **kwargs,
        )

    def fail_webhook_event(self, **kwargs):  # type: ignore[no-untyped-def]
        principal_id = str(kwargs.get("principal_id") or "").strip()
        search_run_id = str(kwargs.get("search_run_id") or "").strip()
        authority_connection = kwargs.pop("authority_connection", None)
        return self._write_repository(
            "fail_webhook_event",
            authority_principal_id=principal_id,
            authority_run_id=search_run_id,
            authority_connection=authority_connection,
            **kwargs,
        )

    def record_webhook_event(
        self,
        *,
        event_id: str,
        payload: dict[str, object],
        principal_id: str,
        ownership_scope: str,
        search_run_id: str,
        packet_id: str,
        status: str,
        extra=None,  # type: ignore[no-untyped-def]
    ):
        owner = f"record:{os.getpid()}:{threading.get_ident()}:{uuid4().hex}"
        claim = self.claim_webhook_event(
            event_id=event_id,
            payload=payload,
            principal_id=principal_id,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
            packet_id=packet_id,
            extra=extra,
            claim_owner=owner,
            lease_seconds=60,
        )
        if not bool(claim.get("claimed")):
            return dict(claim.get("row") or {})
        return self.complete_webhook_event(
            event_id=event_id,
            principal_id=principal_id,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
            claim_owner=owner,
            status=status,
        )

    def resolve_webhook_job(
        self,
        *,
        packet_id: str = "",
        provider_idea_id: str = "",
        provider_script_id: str = "",
    ) -> dict[str, object]:
        return self._repository.resolve_webhook_job(
            packet_id=packet_id,
            provider_idea_id=provider_idea_id,
            provider_script_id=provider_script_id,
        )

    def export_principal_data(
        self,
        *,
        principal_id: str,
        limit: int = 250,
    ) -> dict[str, object]:
        normalized_limit = max(1, min(500, int(limit or 250)))
        return self._repository.export_principal_data(
            principal_id=principal_id,
            limit=normalized_limit,
        )

    def erase_principal_data(self, *, principal_id: str) -> dict[str, object]:
        normalized_principal = str(principal_id or "").strip()
        principal_key = _property_search_principal_key(normalized_principal)
        if not normalized_principal or not principal_key:
            raise ValueError("property_content_principal_id_required")
        result = dict(
            self._repository.erase_principal_data(
                principal_id=normalized_principal,
                principal_key=principal_key,
            )
        )
        ownership_rows = tuple(
            dict(row)
            for row in result.pop("ownership_rows", ())
            if isinstance(row, dict)
        )
        if "receipt_files_deleted" not in result:
            result["receipt_files_deleted"] = _delete_receipt_files(ownership_rows)
        result.pop("principal_id", None)
        return result

    def receipt_path(
        self,
        *,
        packet_id: str,
        principal_id: str,
        ownership_scope: str,
        search_run_id: str,
    ) -> Path:
        _, principal_key, normalized_scope, normalized_run_id = _principal_ownership(
            principal_id,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
        )
        return _receipt_path(
            principal_key=principal_key,
            ownership_scope=normalized_scope,
            search_run_id=normalized_run_id,
            packet_id=packet_id,
        )

    def read_receipt(
        self,
        *,
        packet_id: str,
        principal_id: str,
        ownership_scope: str,
        search_run_id: str,
    ) -> dict[str, object] | None:
        _, principal_key, normalized_scope, normalized_run_id = _principal_ownership(
            principal_id,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
        )
        path = _receipt_path(
            principal_key=principal_key,
            ownership_scope=normalized_scope,
            search_run_id=normalized_run_id,
            packet_id=packet_id,
        )
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception as exc:
            raise PropertyContentLedgerCorruptionError("property_content_receipt_invalid") from exc
        if not isinstance(parsed, dict):
            raise PropertyContentLedgerCorruptionError("property_content_receipt_invalid")
        ownership = parsed.get("_ownership")
        expected = {
            "principal_key": principal_key,
            "ownership_scope": normalized_scope,
            "search_run_id": normalized_run_id,
            "packet_id": str(packet_id or "").strip(),
        }
        if not isinstance(ownership, dict) or dict(ownership) != expected:
            raise PropertyContentLedgerError("property_content_receipt_owner_mismatch")
        return dict(parsed)

    def write_receipt(
        self,
        *,
        packet: dict[str, object],
        receipt: dict[str, object],
        principal_id: str,
        ownership_scope: str,
        search_run_id: str,
        status: str,
        extra: dict[str, object] | None = None,
        _after_promote: Callable[[], None] | None = None,
    ) -> Path:
        packet_id = str(packet.get("packet_id") or "").strip()
        if not packet_id:
            raise ValueError("property_content_packet_id_required")
        normalized_principal, principal_key, normalized_scope, normalized_run_id = _principal_ownership(
            principal_id,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
        )
        path = _receipt_path(
            principal_key=principal_key,
            ownership_scope=normalized_scope,
            search_run_id=normalized_run_id,
            packet_id=packet_id,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        token = f"{os.getpid()}.{threading.get_ident()}.{uuid4().hex}"
        staged_path = path.with_name(f".{path.name}.stage.{token}.tmp")
        backup_path = path.with_name(f".{path.name}.previous.{token}.tmp")
        governed_receipt = {
            **dict(receipt),
            "_ownership": {
                "principal_key": principal_key,
                "ownership_scope": normalized_scope,
                "search_run_id": normalized_run_id,
                "packet_id": packet_id,
            },
        }
        receipt_json = canonical_json(governed_receipt)
        receipt_sha256 = hashlib.sha256(receipt_json.encode("utf-8")).hexdigest()
        row_extra = {
            **dict(extra or {}),
            "receipt_path": str(path),
            "receipt_sha256": receipt_sha256,
            "receipt_status": str(receipt.get("status") or status),
            "publication_allowed": False,
            "production_allowed": False,
        }
        try:
            with staged_path.open("w", encoding="utf-8") as handle:
                handle.write(receipt_json)
                handle.flush()
                os.fsync(handle.fileno())
            if self._backend == "file":
                with self.publication_authority(
                    principal_id=normalized_principal,
                    search_run_id=normalized_run_id,
                ):
                    self._repository.materialize_receipt(
                        staged_path=staged_path,
                        canonical_path=path,
                        backup_path=backup_path,
                        packet=packet,
                        principal_id=normalized_principal,
                        ownership_scope=normalized_scope,
                        search_run_id=normalized_run_id,
                        status=status,
                        extra=row_extra,
                        after_promote=_after_promote,
                    )
                return path

            promoted = False
            backed_up = False
            try:
                with self.publication_authority(
                    principal_id=normalized_principal,
                    search_run_id=normalized_run_id,
                ) as connection:
                    self._repository.upsert_job(
                        packet,
                        principal_id=normalized_principal,
                        ownership_scope=normalized_scope,
                        search_run_id=normalized_run_id,
                        status=status,
                        extra=row_extra,
                        authority_connection=connection,
                    )
                    if path.exists():
                        path.replace(backup_path)
                        backed_up = True
                    staged_path.replace(path)
                    promoted = True
                    _fsync_directory(path.parent)
                    if _after_promote is not None:
                        _after_promote()
            except Exception:
                if promoted:
                    path.unlink(missing_ok=True)
                if backed_up and backup_path.exists():
                    backup_path.replace(path)
                _fsync_directory(path.parent)
                raise
            backup_path.unlink(missing_ok=True)
            return path
        finally:
            staged_path.unlink(missing_ok=True)
            backup_path.unlink(missing_ok=True)
