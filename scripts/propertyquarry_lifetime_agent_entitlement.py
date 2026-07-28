#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EA_ROOT = ROOT / "ea"
for candidate in (ROOT, EA_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from app.services.property_billing import normalize_property_commercial


SCHEMA = "propertyquarry.lifetime_agent_entitlement.v1"
ROLLBACK_SCHEMA = "propertyquarry.lifetime_agent_entitlement.rollback.v2"
LIFETIME_EXPIRY = "2999-01-01T00:00:00+00:00"
_GRANT_CONTROLLED_COMMERCIAL_KEYS = (
    "active_plan_key",
    "status",
    "active_until",
    "entitlement_kind",
    "entitlement_plan_key",
    "entitlement_source",
    "entitlement_grant_id",
    "entitlement_granted_at",
    "entitlement_reason_digest",
)
_GRANT_NORMALIZATION_DEFAULTS = {
    key: value
    for key, value in normalize_property_commercial({}).items()
    if key not in _GRANT_CONTROLLED_COMMERCIAL_KEYS
}


class EntitlementGrantError(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _digest(value: object) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _json_digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _normalize_email(value: object) -> str:
    email = str(value or "").strip().lower()
    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        raise EntitlementGrantError("target_email_invalid")
    return email


def _registration_principal_id(email: str) -> str:
    return f"user-{hashlib.sha256(email.encode('utf-8')).hexdigest()[:16]}"


def _principal_digest(principal_id: object) -> str:
    return _digest(principal_id)[:16]


def build_lifetime_agent_commercial(
    existing: dict[str, object] | None,
    *,
    target_email_digest: str,
    granted_at: str,
    reason: str,
) -> dict[str, object]:
    current = dict(existing or {})
    grant_id = f"pq-lifetime-agent-{target_email_digest[:16]}"
    original_granted_at = (
        str(current.get("entitlement_granted_at") or "").strip()
        if str(current.get("entitlement_grant_id") or "").strip() == grant_id
        else ""
    )
    current.update(
        {
            "active_plan_key": "agent",
            "status": "active",
            "active_until": LIFETIME_EXPIRY,
            "entitlement_kind": "lifetime",
            "entitlement_plan_key": "agent",
            "entitlement_source": "operator_grant",
            "entitlement_grant_id": grant_id,
            "entitlement_granted_at": original_granted_at or granted_at,
            "entitlement_reason_digest": _digest(reason),
        }
    )
    normalized = normalize_property_commercial(current)
    # Keep the durable authority marker even when this operational script is
    # run inside an older release whose normalizer only understands the
    # canonical Agent + far-future projection.
    normalized.update(
        {
            "entitlement_kind": "lifetime",
            "entitlement_plan_key": "agent",
            "entitlement_source": "operator_grant",
            "entitlement_grant_id": grant_id,
            "entitlement_granted_at": original_granted_at or granted_at,
            "entitlement_reason_digest": _digest(reason),
        }
    )
    return normalized


def _connect(database_url: str):  # type: ignore[no-untyped-def]
    try:
        import psycopg
    except Exception as exc:  # pragma: no cover
        raise EntitlementGrantError("psycopg_required") from exc
    try:
        return psycopg.connect(database_url)
    except Exception as exc:
        raise EntitlementGrantError("database_connection_failed") from exc


def _email_for_md5(conn: Any, email_md5: str) -> str:
    normalized_digest = str(email_md5 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", normalized_digest):
        raise EntitlementGrantError("target_email_md5_invalid")
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH known_emails AS (
                SELECT lower(email) AS email FROM principals WHERE email <> ''
                UNION
                SELECT lower(email) FROM propertyquarry_google_identity_accounts WHERE email <> ''
            )
            SELECT email
            FROM known_emails
            WHERE md5(email) = %s
            ORDER BY email
            """,
            (normalized_digest,),
        )
        emails = [str(row[0] or "").strip().lower() for row in cur.fetchall()]
    if not emails:
        raise EntitlementGrantError("target_account_not_found")
    if len(emails) != 1:
        raise EntitlementGrantError("target_email_digest_ambiguous")
    return _normalize_email(emails[0])


def _resolved_accounts(conn: Any, email: str, *, lock: bool) -> list[tuple[str, dict[str, object]]]:
    registration_principal = _registration_principal_id(email)
    cloudflare_principal = f"cf-email:{email}"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH evidence AS (
                SELECT principal_id FROM principals WHERE lower(email) = %s
                UNION
                SELECT principal_id FROM propertyquarry_google_identity_accounts WHERE lower(email) = %s
            ),
            candidates AS (
                SELECT principal_id FROM evidence
                UNION SELECT %s
                UNION SELECT %s
            )
            SELECT state.principal_id, state.property_search_preferences_json
            FROM onboarding_states AS state
            JOIN candidates USING (principal_id)
            ORDER BY state.principal_id
            {"FOR UPDATE" if lock else ""}
            """,
            (
                email,
                email,
                registration_principal,
                cloudflare_principal,
            ),
        )
        rows = [
            (str(row[0] or "").strip(), dict(row[1] or {}))
            for row in cur.fetchall()
            if str(row[0] or "").strip()
        ]
        for principal_id, _ in rows:
            cur.execute(
                """
                WITH linked_emails AS (
                    SELECT lower(email) AS email FROM principals WHERE principal_id = %s AND email <> ''
                    UNION
                    SELECT lower(email) FROM propertyquarry_google_identity_accounts
                    WHERE principal_id = %s AND email <> ''
                )
                SELECT count(*) FROM linked_emails WHERE email <> %s
                """,
                (principal_id, principal_id, email),
            )
            conflicting_email_count = int(cur.fetchone()[0] or 0)
            deterministic_alias = principal_id in {
                registration_principal,
                cloudflare_principal,
            }
            if conflicting_email_count and not deterministic_alias:
                raise EntitlementGrantError("target_principal_identity_conflict")
    if not rows:
        raise EntitlementGrantError("target_account_not_found")
    return rows


def _updated_preferences(
    preferences: dict[str, object],
    *,
    email_digest: str,
    granted_at: str,
    reason: str,
) -> dict[str, object]:
    updated = dict(preferences or {})
    raw = dict(updated.get("raw_preferences") or {}) if isinstance(updated.get("raw_preferences"), dict) else {}
    raw_commercial = (
        dict(raw.get("property_commercial") or {})
        if isinstance(raw.get("property_commercial"), dict)
        else {}
    )
    top_commercial = (
        dict(updated.get("property_commercial") or {})
        if isinstance(updated.get("property_commercial"), dict)
        else {}
    )
    existing = {**raw_commercial, **top_commercial}
    commercial = build_lifetime_agent_commercial(
        existing,
        target_email_digest=email_digest,
        granted_at=granted_at,
        reason=reason,
    )
    updated["property_commercial"] = commercial
    raw["property_commercial"] = commercial
    updated["raw_preferences"] = raw
    return updated


def _commercial_subtrees(
    preferences: dict[str, object],
) -> tuple[bool, dict[str, object], bool, dict[str, object]]:
    top_present = isinstance(preferences.get("property_commercial"), dict)
    top = (
        dict(preferences.get("property_commercial") or {})
        if top_present
        else {}
    )
    raw_preferences = (
        dict(preferences.get("raw_preferences") or {})
        if isinstance(preferences.get("raw_preferences"), dict)
        else {}
    )
    raw_present = isinstance(raw_preferences.get("property_commercial"), dict)
    raw = (
        dict(raw_preferences.get("property_commercial") or {})
        if raw_present
        else {}
    )
    return top_present, top, raw_present, raw


def _controlled_commercial(value: dict[str, object]) -> dict[str, object]:
    return {
        key: value[key]
        for key in _GRANT_CONTROLLED_COMMERCIAL_KEYS
        if key in value
    }


def _rollback_row(
    *,
    principal_id: str,
    before: dict[str, object],
    after: dict[str, object],
) -> dict[str, object]:
    before_top_present, before_top, before_raw_present, before_raw = (
        _commercial_subtrees(before)
    )
    _, after_top, _, after_raw = _commercial_subtrees(after)
    before_top_controlled = _controlled_commercial(before_top)
    before_raw_controlled = _controlled_commercial(before_raw)
    after_top_controlled = _controlled_commercial(after_top)
    after_raw_controlled = _controlled_commercial(after_raw)
    return {
        "principal_id": principal_id,
        "before_top_commercial_present": before_top_present,
        "before_top_controlled": before_top_controlled,
        "before_raw_commercial_present": before_raw_present,
        "before_raw_controlled": before_raw_controlled,
        "expected_after_top_controlled_sha256": _json_digest(
            after_top_controlled
        ),
        "expected_after_raw_controlled_sha256": _json_digest(
            after_raw_controlled
        ),
    }


def _rollback_payload(
    *,
    generated_at: str,
    target_email_digest: str,
    rows: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "schema": ROLLBACK_SCHEMA,
        "created_at": generated_at,
        "target_email_sha256": target_email_digest,
        "rows": rows,
    }


def _read_private_json(path: Path) -> dict[str, object]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise EntitlementGrantError("rollback_snapshot_not_regular")
    if metadata.st_mode & 0o077:
        raise EntitlementGrantError("rollback_snapshot_permissions_invalid")
    if metadata.st_size <= 0 or metadata.st_size > 32 * 1024 * 1024:
        raise EntitlementGrantError("rollback_snapshot_size_invalid")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EntitlementGrantError("rollback_snapshot_invalid") from exc
    if not isinstance(payload, dict):
        raise EntitlementGrantError("rollback_snapshot_invalid")
    return payload


def _atomic_create_private_json(path: Path, payload: object) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=str(path.parent),
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_path, path)
        except FileExistsError:
            return False
        with contextlib.suppress(OSError):
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return True
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary_path.unlink()


def _legacy_rollback_rows(payload: dict[str, object]) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    for item in list(payload.get("rows") or []):
        if not isinstance(item, dict):
            raise EntitlementGrantError("rollback_snapshot_invalid")
        before = dict(item.get("before_preferences") or {})
        principal_id = str(item.get("principal_id") or "").strip()
        if not principal_id:
            raise EntitlementGrantError("rollback_snapshot_invalid")
        top_present, top, raw_present, raw = _commercial_subtrees(before)
        normalized.append(
            {
                "principal_id": principal_id,
                "before_top_commercial_present": top_present,
                "before_top_controlled": _controlled_commercial(top),
                "before_raw_commercial_present": raw_present,
                "before_raw_controlled": _controlled_commercial(raw),
                "legacy": True,
            }
        )
    return normalized


def _normalized_rollback_rows(
    payload: dict[str, object],
) -> list[dict[str, object]]:
    schema = str(payload.get("schema") or "").strip()
    if schema == f"{SCHEMA}.rollback":
        return _legacy_rollback_rows(payload)
    if schema != ROLLBACK_SCHEMA:
        raise EntitlementGrantError("rollback_snapshot_schema_invalid")
    rows: list[dict[str, object]] = []
    for item in list(payload.get("rows") or []):
        if not isinstance(item, dict):
            raise EntitlementGrantError("rollback_snapshot_invalid")
        row = dict(item)
        principal_id = str(row.get("principal_id") or "").strip()
        top_sha = str(
            row.get("expected_after_top_controlled_sha256") or ""
        ).strip()
        raw_sha = str(
            row.get("expected_after_raw_controlled_sha256") or ""
        ).strip()
        if (
            not principal_id
            or not re.fullmatch(r"[0-9a-f]{64}", top_sha)
            or not re.fullmatch(r"[0-9a-f]{64}", raw_sha)
        ):
            raise EntitlementGrantError("rollback_snapshot_invalid")
        rows.append(row)
    return rows


def _validate_existing_rollback_snapshot(
    payload: dict[str, object],
    *,
    expected_payload: dict[str, object],
    current_by_principal: dict[str, dict[str, object]],
) -> None:
    if (
        str(payload.get("target_email_sha256") or "").strip()
        != str(expected_payload.get("target_email_sha256") or "").strip()
    ):
        raise EntitlementGrantError("rollback_snapshot_conflict")
    stored_rows = {
        str(row.get("principal_id") or "").strip(): row
        for row in _normalized_rollback_rows(payload)
    }
    expected_rows = {
        str(row.get("principal_id") or "").strip(): row
        for row in _normalized_rollback_rows(expected_payload)
    }
    if not stored_rows or set(stored_rows) != set(expected_rows):
        raise EntitlementGrantError("rollback_snapshot_conflict")
    for principal_id, expected in expected_rows.items():
        stored = stored_rows[principal_id]
        current = current_by_principal[principal_id]
        _, current_top, _, current_raw = _commercial_subtrees(current)
        current_top_sha = _json_digest(_controlled_commercial(current_top))
        current_raw_sha = _json_digest(_controlled_commercial(current_raw))
        expected_top_sha = str(
            expected.get("expected_after_top_controlled_sha256") or ""
        )
        expected_raw_sha = str(
            expected.get("expected_after_raw_controlled_sha256") or ""
        )
        if bool(stored.get("legacy")):
            # Legacy snapshots predate subtree hashes. They are accepted only
            # for an already-applied, no-change rerun.
            if (
                current_top_sha != expected_top_sha
                or current_raw_sha != expected_raw_sha
            ):
                raise EntitlementGrantError("rollback_snapshot_conflict")
            continue
        stored_top_sha = str(
            stored.get("expected_after_top_controlled_sha256") or ""
        )
        stored_raw_sha = str(
            stored.get("expected_after_raw_controlled_sha256") or ""
        )
        if stored_top_sha != expected_top_sha or stored_raw_sha != expected_raw_sha:
            raise EntitlementGrantError("rollback_snapshot_conflict")
        stored_before_top_sha = _json_digest(
            dict(stored.get("before_top_controlled") or {})
        )
        stored_before_raw_sha = _json_digest(
            dict(stored.get("before_raw_controlled") or {})
        )
        if current_top_sha not in {stored_before_top_sha, stored_top_sha}:
            raise EntitlementGrantError("rollback_snapshot_conflict")
        if current_raw_sha not in {stored_before_raw_sha, stored_raw_sha}:
            raise EntitlementGrantError("rollback_snapshot_conflict")


def _prepare_rollback_snapshot(
    path: Path,
    *,
    payload: dict[str, object],
    current_by_principal: dict[str, dict[str, object]],
) -> dict[str, object]:
    created = _atomic_create_private_json(path, payload)
    stored = payload if created else _read_private_json(path)
    if not created:
        _validate_existing_rollback_snapshot(
            stored,
            expected_payload=payload,
            current_by_principal=current_by_principal,
        )
    return {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "mode": oct(path.stat().st_mode & 0o777),
        "created": created,
        "preserved_existing": not created,
    }


def _controlled_restore(
    current: dict[str, object],
    before: dict[str, object],
) -> dict[str, object]:
    restored = dict(current)
    for key in _GRANT_CONTROLLED_COMMERCIAL_KEYS:
        restored.pop(key, None)
    restored.update(dict(before or {}))
    return restored


def _prune_unchanged_grant_defaults(
    restored: dict[str, object],
) -> dict[str, object]:
    compact = dict(restored)
    for key, default_value in _GRANT_NORMALIZATION_DEFAULTS.items():
        if key in compact and compact[key] == default_value:
            compact.pop(key)
    return compact


def _legacy_lifetime_projection_matches(
    controlled: dict[str, object],
    *,
    target_email_digest: str,
) -> bool:
    return (
        str(controlled.get("active_plan_key") or "") == "agent"
        and str(controlled.get("status") or "") == "active"
        and str(controlled.get("active_until") or "") == LIFETIME_EXPIRY
        and str(controlled.get("entitlement_kind") or "") == "lifetime"
        and str(controlled.get("entitlement_plan_key") or "") == "agent"
        and str(controlled.get("entitlement_source") or "") == "operator_grant"
        and str(controlled.get("entitlement_grant_id") or "")
        == f"pq-lifetime-agent-{target_email_digest[:16]}"
    )


def _restored_preferences(
    current: dict[str, object],
    *,
    rollback_row: dict[str, object],
    target_email_digest: str,
) -> dict[str, object]:
    _, current_top, _, current_raw = _commercial_subtrees(current)
    current_top_controlled = _controlled_commercial(current_top)
    current_raw_controlled = _controlled_commercial(current_raw)
    if bool(rollback_row.get("legacy")):
        if not _legacy_lifetime_projection_matches(
            current_top_controlled,
            target_email_digest=target_email_digest,
        ) or not _legacy_lifetime_projection_matches(
            current_raw_controlled,
            target_email_digest=target_email_digest,
        ):
            raise EntitlementGrantError("rollback_current_entitlement_conflict")
    else:
        if _json_digest(current_top_controlled) != str(
            rollback_row.get("expected_after_top_controlled_sha256") or ""
        ):
            raise EntitlementGrantError("rollback_current_entitlement_conflict")
        if _json_digest(current_raw_controlled) != str(
            rollback_row.get("expected_after_raw_controlled_sha256") or ""
        ):
            raise EntitlementGrantError("rollback_current_entitlement_conflict")
    restored = dict(current)
    restored_top = _controlled_restore(
        current_top,
        dict(rollback_row.get("before_top_controlled") or {}),
    )
    if not bool(rollback_row.get("before_top_commercial_present")):
        restored_top = _prune_unchanged_grant_defaults(restored_top)
    if (
        bool(rollback_row.get("before_top_commercial_present"))
        or restored_top
    ):
        restored["property_commercial"] = restored_top
    else:
        restored.pop("property_commercial", None)
    raw_preferences = (
        dict(restored.get("raw_preferences") or {})
        if isinstance(restored.get("raw_preferences"), dict)
        else {}
    )
    restored_raw = _controlled_restore(
        current_raw,
        dict(rollback_row.get("before_raw_controlled") or {}),
    )
    if not bool(rollback_row.get("before_raw_commercial_present")):
        restored_raw = _prune_unchanged_grant_defaults(restored_raw)
    if (
        bool(rollback_row.get("before_raw_commercial_present"))
        or restored_raw
    ):
        raw_preferences["property_commercial"] = restored_raw
    else:
        raw_preferences.pop("property_commercial", None)
    restored["raw_preferences"] = raw_preferences
    return restored


def restore(
    *,
    database_url: str,
    snapshot_path: Path,
    apply: bool,
    receipt_path: Path | None,
) -> dict[str, object]:
    if (
        receipt_path is not None
        and receipt_path.resolve() == snapshot_path.resolve()
    ):
        raise EntitlementGrantError("receipt_and_rollback_paths_must_differ")
    payload = _read_private_json(snapshot_path)
    target_email_digest = str(
        payload.get("target_email_sha256") or ""
    ).strip()
    if not re.fullmatch(r"[0-9a-f]{64}", target_email_digest):
        raise EntitlementGrantError("rollback_snapshot_invalid")
    rollback_rows = _normalized_rollback_rows(payload)
    row_by_principal = {
        str(row.get("principal_id") or "").strip(): row
        for row in rollback_rows
    }
    if not row_by_principal:
        raise EntitlementGrantError("rollback_snapshot_invalid")
    changes: list[dict[str, object]] = []
    with _connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT principal_id, property_search_preferences_json
                FROM onboarding_states
                WHERE principal_id = ANY(%s)
                ORDER BY principal_id
                FOR UPDATE
                """,
                (list(row_by_principal),),
            )
            current_rows = {
                str(row[0] or "").strip(): dict(row[1] or {})
                for row in cur.fetchall()
            }
        if set(current_rows) != set(row_by_principal):
            raise EntitlementGrantError("rollback_target_account_missing")
        planned: list[
            tuple[str, dict[str, object], dict[str, object]]
        ] = []
        for principal_id, current in current_rows.items():
            restored = _restored_preferences(
                current,
                rollback_row=row_by_principal[principal_id],
                target_email_digest=target_email_digest,
            )
            planned.append((principal_id, current, restored))
            changes.append(
                {
                    "principal_digest": _principal_digest(principal_id),
                    "before_sha256": _json_digest(current),
                    "after_sha256": _json_digest(restored),
                    "changed": _json_digest(current)
                    != _json_digest(restored),
                }
            )
        if apply:
            with conn.cursor() as cur:
                for principal_id, current, restored in planned:
                    if _json_digest(current) == _json_digest(restored):
                        continue
                    cur.execute(
                        """
                        UPDATE onboarding_states
                        SET property_search_preferences_json = %s::jsonb,
                            updated_at = NOW()
                        WHERE principal_id = %s
                          AND property_search_preferences_json = %s::jsonb
                        """,
                        (
                            json.dumps(restored),
                            principal_id,
                            json.dumps(current),
                        ),
                    )
                    if cur.rowcount != 1:
                        raise EntitlementGrantError(
                            "rollback_target_concurrent_update"
                        )
            conn.commit()
        else:
            conn.rollback()
    receipt: dict[str, object] = {
        "schema": f"{SCHEMA}.restore",
        "generated_at": _now_iso(),
        "mode": "apply" if apply else "plan",
        "target_email_sha256": target_email_digest,
        "resolved_principal_count": len(changes),
        "changes": changes,
        "commercial_subtree_only": True,
        "provider_calls": 0,
        "status": "restored" if apply else "planned",
        "source_snapshot_sha256": hashlib.sha256(
            snapshot_path.read_bytes()
        ).hexdigest(),
    }
    if receipt_path is not None:
        _write_receipt(receipt_path, receipt)
    return receipt


def _write_receipt(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def grant(
    *,
    database_url: str,
    email_md5: str,
    apply: bool,
    reason: str,
    receipt_path: Path | None,
    rollback_path: Path | None,
) -> dict[str, object]:
    if apply and rollback_path is None:
        raise EntitlementGrantError("rollback_snapshot_required")
    if (
        apply
        and receipt_path is not None
        and rollback_path is not None
        and receipt_path.resolve() == rollback_path.resolve()
    ):
        raise EntitlementGrantError("receipt_and_rollback_paths_must_differ")
    generated_at = _now_iso()
    with _connect(database_url) as conn:
        email = _email_for_md5(conn, email_md5)
        email_digest = _digest(email)
        rows = _resolved_accounts(conn, email, lock=apply)
        changes: list[dict[str, object]] = []
        planned_rows: list[
            tuple[str, dict[str, object], dict[str, object]]
        ] = []
        for principal_id, before in rows:
            after = _updated_preferences(
                before,
                email_digest=email_digest,
                granted_at=generated_at,
                reason=reason,
            )
            before_sha = _json_digest(before)
            after_sha = _json_digest(after)
            changed = before_sha != after_sha
            planned_rows.append((principal_id, before, after))
            changes.append(
                {
                    "principal_digest": _principal_digest(principal_id),
                    "before_sha256": before_sha,
                    "after_sha256": after_sha,
                    "changed": changed,
                }
            )
        rollback_receipt: dict[str, object] | None = None
        if apply:
            rollback_payload = _rollback_payload(
                generated_at=generated_at,
                target_email_digest=email_digest,
                rows=[
                    _rollback_row(
                        principal_id=principal_id,
                        before=before,
                        after=after,
                    )
                    for principal_id, before, after in planned_rows
                ],
            )
            rollback_receipt = _prepare_rollback_snapshot(
                rollback_path,
                payload=rollback_payload,
                current_by_principal={
                    principal_id: before
                    for principal_id, before, _ in planned_rows
                },
            )
            for principal_id, before, after in planned_rows:
                if _json_digest(before) == _json_digest(after):
                    continue
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE onboarding_states
                        SET property_search_preferences_json = %s::jsonb,
                            updated_at = NOW()
                        WHERE principal_id = %s
                          AND property_search_preferences_json = %s::jsonb
                        """,
                        (json.dumps(after), principal_id, json.dumps(before)),
                    )
                    if cur.rowcount != 1:
                        raise EntitlementGrantError("target_account_concurrent_update")
            with conn.cursor() as cur:
                for principal_id, _ in rows:
                    cur.execute(
                        """
                        SELECT
                            property_search_preferences_json->'property_commercial'->>'active_plan_key',
                            property_search_preferences_json->'property_commercial'->>'active_until',
                            property_search_preferences_json->'property_commercial'->>'entitlement_kind',
                            property_search_preferences_json->'raw_preferences'->'property_commercial'->>'active_plan_key',
                            property_search_preferences_json->'raw_preferences'->'property_commercial'->>'entitlement_kind'
                        FROM onboarding_states
                        WHERE principal_id = %s
                        """,
                        (principal_id,),
                    )
                    verification = cur.fetchone()
                    if verification != ("agent", LIFETIME_EXPIRY, "lifetime", "agent", "lifetime"):
                        raise EntitlementGrantError("entitlement_verification_failed")
                    principal_hash = _principal_digest(principal_id)
                    next(item for item in changes if item["principal_digest"] == principal_hash)["verified"] = True
            conn.commit()
        else:
            conn.rollback()
    receipt: dict[str, object] = {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "mode": "apply" if apply else "plan",
        "target_email_sha256": email_digest,
        "resolved_principal_count": len(changes),
        "changes": changes,
        "entitlement": {
            "plan_key": "agent",
            "kind": "lifetime",
            "active_until": LIFETIME_EXPIRY,
            "provider_calls": 0,
        },
        "status": "applied" if apply else "planned",
    }
    if rollback_receipt is not None:
        receipt["rollback"] = rollback_receipt
    if receipt_path is not None:
        _write_receipt(receipt_path, receipt)
    return receipt


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Grant a local, provider-neutral lifetime PropertyQuarry Agent entitlement.")
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--email-md5", help="MD5 lookup digest of the normalized existing account email; never written to receipts.")
    operation.add_argument(
        "--restore-snapshot",
        type=Path,
        help="Restore only grant-controlled commercial fields from a private rollback snapshot.",
    )
    parser.add_argument("--database-url", default=str(os.getenv("DATABASE_URL") or "").strip())
    parser.add_argument("--reason", default="User-authorized lifetime Agent entitlement")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--rollback-snapshot", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.database_url:
        print(json.dumps({"schema": SCHEMA, "status": "error", "error": "database_url_required"}))
        return 2
    try:
        if args.restore_snapshot is not None:
            receipt = restore(
                database_url=args.database_url,
                snapshot_path=args.restore_snapshot,
                apply=bool(args.apply),
                receipt_path=args.receipt,
            )
        else:
            receipt = grant(
                database_url=args.database_url,
                email_md5=args.email_md5,
                apply=bool(args.apply),
                reason=args.reason,
                receipt_path=args.receipt,
                rollback_path=args.rollback_snapshot,
            )
    except EntitlementGrantError as exc:
        print(json.dumps({"schema": SCHEMA, "status": "error", "error": str(exc)}))
        return 1
    print(json.dumps(receipt, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
