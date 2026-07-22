from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.product.propertyquarry_google_identity_schema import (
    GOOGLE_IDENTITY_TABLES,
    inspect_propertyquarry_google_identity_schema,
    require_propertyquarry_google_identity_schema_ready,
    reset_propertyquarry_google_identity_schema_cache_for_tests,
)


GOOGLE_AUTHORIZE_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_IDENTITY_COOKIE_NAME = "propertyquarry_identity_session"
GOOGLE_IDENTITY_FLOW_COOKIE_NAME = "propertyquarry_google_identity_flow"
GOOGLE_IDENTITY_STATE_PREFIX = "pqg1"
GOOGLE_IDENTITY_SESSION_PREFIX = "pqis1"
GOOGLE_IDENTITY_LANE = "propertyquarry_google_identity"
GOOGLE_IDENTITY_SCOPES = ("openid", "email", "profile")
@dataclass(frozen=True)
class PropertyQuarryGoogleIdentityConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    state_secret: str
    session_secret: str
    state_ttl_seconds: int
    session_ttl_seconds: int


@dataclass(frozen=True)
class PropertyQuarryGoogleIdentityStart:
    auth_url: str
    state: str
    return_to: str
    flow_nonce: str
    max_age_seconds: int


@dataclass(frozen=True)
class PropertyQuarryGoogleIdentitySession:
    principal_id: str
    session_id: str
    email: str
    display_name: str
    token: str
    return_to: str
    expires_at: str
    max_age_seconds: int


_MEMORY_LOCK = threading.RLock()
_MEMORY_ACCOUNTS: dict[str, dict[str, object]] = {}
_MEMORY_SESSIONS: dict[str, dict[str, object]] = {}
_MEMORY_AUDIT: list[dict[str, object]] = []
_MEMORY_CONSUMED_STATES: dict[str, int] = {}


def _env(name: str) -> str:
    return str(os.environ.get(name) or "").strip()


def _bounded_env_int(name: str, *, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(float(_env(name) or str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _required_env(name: str, error: str) -> str:
    value = _env(name)
    if not value:
        raise RuntimeError(error)
    return value


def _required_secret(name: str, *, missing_error: str, weak_error: str) -> str:
    value = _required_env(name, missing_error)
    if len(value.encode("utf-8")) < 32 or len(set(value)) < 8:
        raise RuntimeError(weak_error)
    return value


def load_propertyquarry_google_identity_config() -> PropertyQuarryGoogleIdentityConfig:
    state_secret = _required_secret(
        "PROPERTYQUARRY_GOOGLE_OAUTH_STATE_SECRET",
        missing_error="google_oauth_propertyquarry_state_secret_missing",
        weak_error="google_oauth_propertyquarry_state_secret_weak",
    )
    session_secret = _required_secret(
        "PROPERTYQUARRY_IDENTITY_SESSION_SECRET",
        missing_error="google_oauth_propertyquarry_session_secret_missing",
        weak_error="google_oauth_propertyquarry_session_secret_weak",
    )
    if hmac.compare_digest(state_secret, session_secret):
        raise RuntimeError("google_oauth_propertyquarry_secrets_must_differ")
    return PropertyQuarryGoogleIdentityConfig(
        client_id=_required_env(
            "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID",
            "google_oauth_propertyquarry_client_id_missing",
        ),
        client_secret=_required_env(
            "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET",
            "google_oauth_propertyquarry_client_secret_missing",
        ),
        redirect_uri=_env("PROPERTYQUARRY_GOOGLE_OAUTH_REDIRECT_URI")
        or "https://propertyquarry.com/google/callback",
        state_secret=state_secret,
        session_secret=session_secret,
        state_ttl_seconds=_bounded_env_int(
            "PROPERTYQUARRY_GOOGLE_OAUTH_STATE_MAX_AGE_SECONDS",
            default=600,
            minimum=300,
            maximum=3600,
        ),
        session_ttl_seconds=_bounded_env_int(
            "PROPERTYQUARRY_IDENTITY_SESSION_TTL_SECONDS",
            default=72 * 3600,
            minimum=300,
            maximum=30 * 24 * 3600,
        ),
    )


def propertyquarry_google_identity_configured() -> bool:
    try:
        load_propertyquarry_google_identity_config()
    except RuntimeError:
        return False
    return True


def propertyquarry_identity_host_allowed(hostname: str | None) -> bool:
    host = str(hostname or "").strip().lower().rstrip(".")
    return host in {
        "propertyquarry.com",
        "www.propertyquarry.com",
        "localhost",
        "127.0.0.1",
        "::1",
        "testserver",
    } or host.endswith(".localhost")


def _safe_return_to(value: object, *, default: str = "/app/search") -> str:
    raw = str(value or "")
    if (
        not raw
        or raw != raw.strip()
        or len(urllib.parse.quote(raw, safe="")) > 2048
        or any(ord(character) < 32 or ord(character) == 127 for character in raw)
    ):
        return default
    candidate = raw
    for _ in range(4):
        if "\\" in candidate or candidate.startswith("//") or not candidate.startswith("/"):
            return default
        parsed = urllib.parse.urlparse(candidate)
        if parsed.scheme or parsed.netloc:
            return default
        decoded = urllib.parse.unquote(candidate)
        if decoded == candidate:
            return raw
        candidate = decoded
    return default


def _validate_redirect_uri(value: str, *, expected: str) -> str:
    redirect_uri = str(value or "").strip()
    if not redirect_uri or not hmac.compare_digest(redirect_uri, str(expected or "").strip()):
        raise RuntimeError("google_oauth_propertyquarry_redirect_uri_invalid")
    parsed = urllib.parse.urlparse(redirect_uri)
    hostname = str(parsed.hostname or "").strip().lower()
    local_http = parsed.scheme == "http" and propertyquarry_identity_host_allowed(hostname) and hostname not in {
        "propertyquarry.com",
        "www.propertyquarry.com",
    }
    if (parsed.scheme != "https" and not local_http) or not propertyquarry_identity_host_allowed(hostname):
        raise RuntimeError("google_oauth_propertyquarry_redirect_uri_invalid")
    if parsed.path != "/google/callback" or parsed.params or parsed.query or parsed.fragment:
        raise RuntimeError("google_oauth_propertyquarry_redirect_uri_invalid")
    return redirect_uri


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 8192:
        raise RuntimeError("google_oauth_propertyquarry_state_invalid")
    try:
        return base64.b64decode(
            normalized + "=" * (-len(normalized) % 4),
            altchars=b"-_",
            validate=True,
        )
    except Exception as exc:
        raise RuntimeError("google_oauth_propertyquarry_state_invalid") from exc


def _encode_prefixed_payload(*, prefix: str, payload: dict[str, object], secret: str) -> str:
    body = _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = _b64url_encode(hmac.new(secret.encode("utf-8"), f"{prefix}.{body}".encode("ascii"), hashlib.sha256).digest())
    return f"{prefix}.{body}.{signature}"


def _decode_prefixed_payload(*, prefix: str, token: str, secret: str) -> dict[str, object]:
    raw = str(token or "").strip()
    if len(raw) > 12_000:
        raise RuntimeError("google_oauth_propertyquarry_state_invalid")
    pieces = raw.split(".")
    if len(pieces) != 3 or not hmac.compare_digest(pieces[0], prefix):
        raise RuntimeError("google_oauth_propertyquarry_state_invalid")
    expected = hmac.new(
        secret.encode("utf-8"),
        f"{pieces[0]}.{pieces[1]}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    provided = _b64url_decode(pieces[2])
    if not hmac.compare_digest(expected, provided):
        raise RuntimeError("google_oauth_propertyquarry_state_signature_invalid")
    try:
        payload = json.loads(_b64url_decode(pieces[1]).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("google_oauth_propertyquarry_state_invalid") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("google_oauth_propertyquarry_state_invalid")
    return payload


def is_propertyquarry_google_identity_state(state: str) -> bool:
    return str(state or "").strip().startswith(f"{GOOGLE_IDENTITY_STATE_PREFIX}.")


def build_propertyquarry_google_identity_start(
    *,
    redirect_uri: str,
    return_to: str,
) -> PropertyQuarryGoogleIdentityStart:
    config = load_propertyquarry_google_identity_config()
    resolved_redirect_uri = _validate_redirect_uri(redirect_uri, expected=config.redirect_uri)
    resolved_return_to = _safe_return_to(return_to)
    issued_at = int(time.time())
    flow_nonce = secrets.token_urlsafe(32)
    state = _encode_prefixed_payload(
        prefix=GOOGLE_IDENTITY_STATE_PREFIX,
        secret=config.state_secret,
        payload={
            "expires_at": issued_at + config.state_ttl_seconds,
            "flow_nonce_hash": hashlib.sha256(flow_nonce.encode("utf-8")).hexdigest(),
            "issued_at": issued_at,
            "lane": GOOGLE_IDENTITY_LANE,
            "nonce": secrets.token_urlsafe(24),
            "redirect_uri": resolved_redirect_uri,
            "return_to": resolved_return_to,
            "version": 1,
        },
    )
    query = urllib.parse.urlencode(
        {
            "client_id": config.client_id,
            "redirect_uri": resolved_redirect_uri,
            "response_type": "code",
            "scope": " ".join(GOOGLE_IDENTITY_SCOPES),
            "state": state,
            "prompt": "select_account",
            "include_granted_scopes": "false",
        }
    )
    return PropertyQuarryGoogleIdentityStart(
        auth_url=f"{GOOGLE_AUTHORIZE_ENDPOINT}?{query}",
        state=state,
        return_to=resolved_return_to,
        flow_nonce=flow_nonce,
        max_age_seconds=config.state_ttl_seconds,
    )


def read_propertyquarry_google_identity_state(
    state: str,
    *,
    flow_nonce: str = "",
    require_flow_nonce: bool = False,
) -> dict[str, object]:
    config = load_propertyquarry_google_identity_config()
    payload = _decode_prefixed_payload(
        prefix=GOOGLE_IDENTITY_STATE_PREFIX,
        token=state,
        secret=config.state_secret,
    )
    if payload.get("lane") != GOOGLE_IDENTITY_LANE or payload.get("version") != 1:
        raise RuntimeError("google_oauth_propertyquarry_state_invalid")
    try:
        issued_at = int(payload.get("issued_at") or 0)
        expires_at = int(payload.get("expires_at") or 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("google_oauth_propertyquarry_state_invalid") from exc
    now = int(time.time())
    if issued_at <= 0 or expires_at <= issued_at or now > expires_at or expires_at - issued_at > config.state_ttl_seconds:
        raise RuntimeError("google_oauth_propertyquarry_state_expired")
    nonce = str(payload.get("nonce") or "").strip()
    if len(nonce) < 24:
        raise RuntimeError("google_oauth_propertyquarry_state_invalid")
    expected_flow_nonce_hash = str(payload.get("flow_nonce_hash") or "").strip()
    supplied_flow_nonce = str(flow_nonce or "").strip()
    supplied_flow_nonce_hash = hashlib.sha256(supplied_flow_nonce.encode("utf-8")).hexdigest()
    if not expected_flow_nonce_hash or (
        (require_flow_nonce or supplied_flow_nonce)
        and (
            not supplied_flow_nonce
            or not hmac.compare_digest(expected_flow_nonce_hash, supplied_flow_nonce_hash)
        )
    ):
        raise RuntimeError("google_oauth_propertyquarry_flow_mismatch")
    payload["redirect_uri"] = _validate_redirect_uri(
        str(payload.get("redirect_uri") or ""),
        expected=config.redirect_uri,
    )
    payload["return_to"] = _safe_return_to(payload.get("return_to"))
    return payload


def _connect(database_url: str):  # type: ignore[no-untyped-def]
    try:
        import psycopg
    except Exception as exc:  # pragma: no cover - import guard
        raise RuntimeError("propertyquarry_google_identity_storage_unavailable") from exc
    return psycopg.connect(str(database_url or "").strip())


def _require_schema(database_url: str) -> None:
    normalized = str(database_url or "").strip()
    if not normalized:
        return
    require_propertyquarry_google_identity_schema_ready(normalized)


def propertyquarry_google_identity_schema_preflight(database_url: str = "") -> dict[str, object]:
    normalized_database_url = str(database_url or "").strip()
    backend = "memory"
    ready = True
    if normalized_database_url:
        backend = "postgres"
        ready = inspect_propertyquarry_google_identity_schema(
            normalized_database_url
        ).ready
    expected_tables = sorted(GOOGLE_IDENTITY_TABLES)
    schema_material = json.dumps(
        {
            "contract_name": "propertyquarry.google_identity_schema.v1",
            "tables": expected_tables,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return {
        "backend": backend,
        "contract_name": "propertyquarry.google_identity_schema_preflight.v1",
        "generic_product_records_written": False,
        "provider_tokens_persisted": False,
        "ready": ready,
        "schema_digest": f"sha256:{hashlib.sha256(schema_material.encode('utf-8')).hexdigest()}",
        "tables": expected_tables,
    }


def _consume_state(
    *,
    state: str,
    expires_at: int,
    database_url: str,
    replay_error: str = "google_oauth_propertyquarry_state_replayed",
) -> None:
    state_hash = hashlib.sha256(str(state or "").encode("utf-8")).hexdigest()
    normalized_database_url = str(database_url or "").strip()
    if normalized_database_url:
        _require_schema(normalized_database_url)
        with _connect(normalized_database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM propertyquarry_google_identity_consumed_states WHERE expires_at < NOW()"
                )
                cursor.execute(
                    """
                    INSERT INTO propertyquarry_google_identity_consumed_states (state_hash, consumed_at, expires_at)
                    VALUES (%s, NOW(), %s)
                    ON CONFLICT (state_hash) DO NOTHING
                    """,
                    (state_hash, datetime.fromtimestamp(expires_at, tz=timezone.utc)),
                )
                inserted = cursor.rowcount == 1
            connection.commit()
        if not inserted:
            raise RuntimeError(replay_error)
        return
    with _MEMORY_LOCK:
        now = int(time.time())
        for key, current_expiry in tuple(_MEMORY_CONSUMED_STATES.items()):
            if current_expiry < now:
                _MEMORY_CONSUMED_STATES.pop(key, None)
        if state_hash in _MEMORY_CONSUMED_STATES:
            raise RuntimeError(replay_error)
        _MEMORY_CONSUMED_STATES[state_hash] = int(expires_at)


def consume_propertyquarry_identity_verification_challenge(
    *,
    token: str,
    expires_at: int,
    database_url: str = "",
) -> None:
    _consume_state(
        state=f"propertyquarry-email-verification-v1:{str(token or '').strip()}",
        expires_at=expires_at,
        database_url=database_url,
        replay_error="propertyquarry_identity_verification_replayed",
    )


def consume_propertyquarry_google_identity_state(
    *,
    state: str,
    flow_nonce: str,
    database_url: str = "",
) -> dict[str, object]:
    state_payload = read_propertyquarry_google_identity_state(
        state,
        flow_nonce=flow_nonce,
        require_flow_nonce=True,
    )
    _consume_state(
        state=state,
        expires_at=int(state_payload.get("expires_at") or 0),
        database_url=database_url,
    )
    return state_payload


def _exchange_google_code_for_tokens(
    *,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> dict[str, Any]:
    if not str(code or "").strip():
        raise RuntimeError("google_oauth_propertyquarry_code_missing")
    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": str(code).strip(),
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        GOOGLE_TOKEN_ENDPOINT,
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("google_oauth_propertyquarry_token_exchange_failed") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("google_oauth_propertyquarry_token_exchange_failed")
    return payload


def _fetch_google_userinfo(access_token: str) -> dict[str, Any]:
    normalized = str(access_token or "").strip()
    if not normalized:
        raise RuntimeError("google_oauth_propertyquarry_access_token_missing")
    request = urllib.request.Request(
        GOOGLE_USERINFO_ENDPOINT,
        headers={"Accept": "application/json", "Authorization": f"Bearer {normalized}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("google_oauth_propertyquarry_userinfo_failed") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("google_oauth_propertyquarry_userinfo_failed")
    return payload


def _subject_hash(subject: str) -> str:
    return hashlib.sha256(str(subject or "").strip().encode("utf-8")).hexdigest()


def _claim_verified_identity_account(
    *,
    subject_hash: str,
    email: str,
    display_name: str,
    issued_at: datetime,
    database_url: str,
) -> str:
    email_principal = f"user-{hashlib.sha256(email.encode('utf-8')).hexdigest()[:16]}"
    normalized_database_url = str(database_url or "").strip()
    if normalized_database_url:
        _require_schema(normalized_database_url)
        with _connect(normalized_database_url) as connection:
            with connection.cursor() as cursor:
                for lock_key in sorted({f"email:{email_principal}", f"subject:{subject_hash}"}):
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"propertyquarry-google-identity:{lock_key}",),
                    )
                cursor.execute(
                    "SELECT principal_id FROM propertyquarry_google_identity_accounts WHERE subject_hash = %s LIMIT 1 FOR UPDATE",
                    (subject_hash,),
                )
                subject_row = cursor.fetchone()
                if subject_row and str(subject_row[0] or "").strip():
                    principal_id = str(subject_row[0]).strip()
                else:
                    cursor.execute(
                        "SELECT subject_hash FROM propertyquarry_google_identity_accounts WHERE principal_id = %s LIMIT 1 FOR UPDATE",
                        (email_principal,),
                    )
                    email_row = cursor.fetchone()
                    if email_row and not hmac.compare_digest(str(email_row[0] or ""), subject_hash):
                        principal_id = f"pq-user-{subject_hash[:24]}"
                    else:
                        principal_id = email_principal
                cursor.execute(
                    "SELECT subject_hash FROM propertyquarry_google_identity_accounts WHERE principal_id = %s LIMIT 1 FOR UPDATE",
                    (principal_id,),
                )
                claimed_row = cursor.fetchone()
                if claimed_row and not hmac.compare_digest(str(claimed_row[0] or ""), subject_hash):
                    raise RuntimeError("google_oauth_propertyquarry_account_subject_conflict")
                cursor.execute(
                    """
                    INSERT INTO propertyquarry_google_identity_accounts (
                        principal_id, subject_hash, email, display_name, email_verified, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, TRUE, %s, %s)
                    ON CONFLICT (principal_id) DO UPDATE SET
                        email = EXCLUDED.email,
                        display_name = EXCLUDED.display_name,
                        email_verified = TRUE,
                        updated_at = EXCLUDED.updated_at
                    WHERE propertyquarry_google_identity_accounts.subject_hash = EXCLUDED.subject_hash
                    """,
                    (principal_id, subject_hash, email, display_name, issued_at, issued_at),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("google_oauth_propertyquarry_account_subject_conflict")
            connection.commit()
        return principal_id
    with _MEMORY_LOCK:
        principal_id = ""
        for current_principal_id, account in _MEMORY_ACCOUNTS.items():
            if hmac.compare_digest(str(account.get("subject_hash") or ""), subject_hash):
                principal_id = current_principal_id
                break
        if not principal_id:
            existing_email_principal = _MEMORY_ACCOUNTS.get(email_principal)
            if existing_email_principal and not hmac.compare_digest(
                str(existing_email_principal.get("subject_hash") or ""),
                subject_hash,
            ):
                principal_id = f"pq-user-{subject_hash[:24]}"
            else:
                principal_id = email_principal
        existing = _MEMORY_ACCOUNTS.get(principal_id)
        if existing and not hmac.compare_digest(str(existing.get("subject_hash") or ""), subject_hash):
            raise RuntimeError("google_oauth_propertyquarry_account_subject_conflict")
        _MEMORY_ACCOUNTS[principal_id] = {
            "principal_id": principal_id,
            "subject_hash": subject_hash,
            "email": email,
            "display_name": display_name,
            "email_verified": True,
            "created_at": (existing or {}).get("created_at") or issued_at.isoformat(),
            "updated_at": issued_at.isoformat(),
        }
        return principal_id


def _token_hash(token: str, *, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), str(token or "").encode("utf-8"), hashlib.sha256).hexdigest()


def _persist_identity_session(
    *,
    principal_id: str,
    subject_hash: str,
    email: str,
    display_name: str,
    session_id: str,
    token_hash: str,
    issued_at: datetime,
    expires_at: datetime,
    database_url: str,
) -> None:
    session = {
        "session_id": session_id,
        "principal_id": principal_id,
        "token_hash": token_hash,
        "status": "active",
        "issued_at": issued_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "last_seen_at": issued_at.isoformat(),
        "revoked_at": "",
    }
    audits = (
        {
            "audit_id": f"pqia_{uuid4().hex}",
            "principal_id": principal_id,
            "session_id": "",
            "event_type": "google_identity_verified",
            "metadata_json": {"email_verified": True, "subject_hash": subject_hash},
            "occurred_at": issued_at.isoformat(),
        },
        {
            "audit_id": f"pqia_{uuid4().hex}",
            "principal_id": principal_id,
            "session_id": session_id,
            "event_type": "propertyquarry_identity_session_issued",
            "metadata_json": {"session_expires_at": expires_at.isoformat()},
            "occurred_at": issued_at.isoformat(),
        },
    )
    normalized_database_url = str(database_url or "").strip()
    if normalized_database_url:
        _require_schema(normalized_database_url)
        try:
            from psycopg.types.json import Json
        except Exception as exc:  # pragma: no cover - import guard
            raise RuntimeError("propertyquarry_google_identity_storage_unavailable") from exc
        with _connect(normalized_database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT subject_hash FROM propertyquarry_google_identity_accounts WHERE principal_id = %s LIMIT 1 FOR UPDATE",
                    (principal_id,),
                )
                claimed_row = cursor.fetchone()
                if not claimed_row or not hmac.compare_digest(str(claimed_row[0] or ""), subject_hash):
                    raise RuntimeError("google_oauth_propertyquarry_account_subject_conflict")
                cursor.execute(
                    """
                    INSERT INTO propertyquarry_google_identity_sessions (
                        session_id, principal_id, token_hash, status, issued_at, expires_at, last_seen_at
                    ) VALUES (%s, %s, %s, 'active', %s, %s, %s)
                    """,
                    (session_id, principal_id, token_hash, issued_at, expires_at, issued_at),
                )
                for audit in audits:
                    cursor.execute(
                        """
                        INSERT INTO propertyquarry_google_identity_audit (
                            audit_id, principal_id, session_id, event_type, metadata_json, occurred_at
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            audit["audit_id"],
                            principal_id,
                            audit["session_id"],
                            audit["event_type"],
                            Json(audit["metadata_json"]),
                            issued_at,
                        ),
                    )
            connection.commit()
        return
    with _MEMORY_LOCK:
        claimed_account = _MEMORY_ACCOUNTS.get(principal_id)
        if not claimed_account or not hmac.compare_digest(
            str(claimed_account.get("subject_hash") or ""),
            subject_hash,
        ):
            raise RuntimeError("google_oauth_propertyquarry_account_subject_conflict")
        _MEMORY_SESSIONS[session_id] = session
        _MEMORY_AUDIT.extend(dict(audit) for audit in audits)


def complete_propertyquarry_google_identity_callback(
    *,
    code: str,
    state: str,
    flow_nonce: str,
    database_url: str = "",
) -> PropertyQuarryGoogleIdentitySession:
    config = load_propertyquarry_google_identity_config()
    state_payload = consume_propertyquarry_google_identity_state(
        state=state,
        flow_nonce=flow_nonce,
        database_url=database_url,
    )
    token_payload: dict[str, Any] = {}
    access_token = ""
    try:
        token_payload = _exchange_google_code_for_tokens(
            code=code,
            client_id=config.client_id,
            client_secret=config.client_secret,
            redirect_uri=str(state_payload["redirect_uri"]),
        )
        access_token = str(token_payload.get("access_token") or "").strip()
        userinfo = _fetch_google_userinfo(access_token)
    finally:
        access_token = ""
        token_payload.clear()
    subject = str(userinfo.get("sub") or "").strip()
    email = str(userinfo.get("email") or "").strip().lower()
    if not subject or "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        raise RuntimeError("google_oauth_propertyquarry_userinfo_incomplete")
    if userinfo.get("email_verified") is not True:
        raise RuntimeError("google_oauth_propertyquarry_email_unverified")
    display_name = str(userinfo.get("name") or email.split("@", 1)[0] or "PropertyQuarry").strip()[:200]
    subject_hash = _subject_hash(subject)
    issued_at_unix = int(time.time())
    issued_at = datetime.fromtimestamp(issued_at_unix, tz=timezone.utc)
    principal_id = _claim_verified_identity_account(
        subject_hash=subject_hash,
        email=email,
        display_name=display_name,
        issued_at=issued_at,
        database_url=database_url,
    )
    expires_at_unix = issued_at_unix + config.session_ttl_seconds
    session_id = f"pqis_{uuid4().hex}"
    session_payload = {
        "display_name": display_name,
        "email": email,
        "expires_at": expires_at_unix,
        "issued_at": issued_at_unix,
        "kind": "propertyquarry_google_identity_session",
        "principal_id": principal_id,
        "session_id": session_id,
        "version": 1,
    }
    session_token = _encode_prefixed_payload(
        prefix=GOOGLE_IDENTITY_SESSION_PREFIX,
        payload=session_payload,
        secret=config.session_secret,
    )
    expires_at = datetime.fromtimestamp(expires_at_unix, tz=timezone.utc)
    _persist_identity_session(
        principal_id=principal_id,
        subject_hash=subject_hash,
        email=email,
        display_name=display_name,
        session_id=session_id,
        token_hash=_token_hash(session_token, secret=config.session_secret),
        issued_at=issued_at,
        expires_at=expires_at,
        database_url=database_url,
    )
    return PropertyQuarryGoogleIdentitySession(
        principal_id=principal_id,
        session_id=session_id,
        email=email,
        display_name=display_name,
        token=session_token,
        return_to=_safe_return_to(state_payload.get("return_to")),
        expires_at=expires_at.isoformat(),
        max_age_seconds=config.session_ttl_seconds,
    )


def _load_session_record(*, session_id: str, database_url: str) -> dict[str, object] | None:
    normalized_database_url = str(database_url or "").strip()
    if normalized_database_url:
        _require_schema(normalized_database_url)
        with _connect(normalized_database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT s.principal_id, s.token_hash, s.status, s.issued_at, s.expires_at,
                           s.last_seen_at, s.revoked_at, a.email, a.display_name, a.email_verified
                    FROM propertyquarry_google_identity_sessions AS s
                    JOIN propertyquarry_google_identity_accounts AS a ON a.principal_id = s.principal_id
                    WHERE s.session_id = %s
                    LIMIT 1
                    """,
                    (session_id,),
                )
                row = cursor.fetchone()
        if not row:
            return None
        return {
            "principal_id": row[0],
            "token_hash": row[1],
            "status": row[2],
            "issued_at": row[3].isoformat() if row[3] else "",
            "expires_at": row[4].isoformat() if row[4] else "",
            "last_seen_at": row[5].isoformat() if row[5] else "",
            "revoked_at": row[6].isoformat() if row[6] else "",
            "email": row[7],
            "display_name": row[8],
            "email_verified": row[9] is True,
        }
    with _MEMORY_LOCK:
        session = _MEMORY_SESSIONS.get(session_id)
        if not session:
            return None
        account = _MEMORY_ACCOUNTS.get(str(session.get("principal_id") or "")) or {}
        return {**dict(session), **dict(account)}


def resolve_propertyquarry_identity_session(
    *,
    token: str,
    database_url: str = "",
    touch: bool = True,
) -> dict[str, object] | None:
    if not str(token or "").strip().startswith(f"{GOOGLE_IDENTITY_SESSION_PREFIX}."):
        return None
    try:
        config = load_propertyquarry_google_identity_config()
        payload = _decode_prefixed_payload(
            prefix=GOOGLE_IDENTITY_SESSION_PREFIX,
            token=token,
            secret=config.session_secret,
        )
    except RuntimeError:
        return None
    if payload.get("kind") != "propertyquarry_google_identity_session" or payload.get("version") != 1:
        return None
    try:
        expires_at_unix = int(payload.get("expires_at") or 0)
    except (TypeError, ValueError):
        return None
    if expires_at_unix <= int(time.time()):
        return None
    session_id = str(payload.get("session_id") or "").strip()
    principal_id = str(payload.get("principal_id") or "").strip()
    if not session_id or not principal_id:
        return None
    stored = _load_session_record(session_id=session_id, database_url=database_url)
    if not stored or str(stored.get("status") or "").strip() != "active":
        return None
    if not hmac.compare_digest(str(stored.get("principal_id") or ""), principal_id):
        return None
    if stored.get("email_verified") is not True:
        return None
    if not hmac.compare_digest(
        str(stored.get("token_hash") or ""),
        _token_hash(token, secret=config.session_secret),
    ):
        return None
    try:
        stored_expiry = datetime.fromisoformat(str(stored.get("expires_at") or ""))
    except ValueError:
        return None
    if stored_expiry.tzinfo is None:
        stored_expiry = stored_expiry.replace(tzinfo=timezone.utc)
    if stored_expiry <= datetime.now(timezone.utc):
        return None
    if touch:
        _touch_identity_session(session_id=session_id, database_url=database_url)
    return {
        "display_name": str(stored.get("display_name") or payload.get("display_name") or "").strip(),
        "email": str(stored.get("email") or payload.get("email") or "").strip().lower(),
        "expires_at": stored_expiry.isoformat(),
        "principal_id": principal_id,
        "role": "principal",
        "session_id": session_id,
        "source_kind": "propertyquarry_google_identity",
    }


def _touch_identity_session(*, session_id: str, database_url: str) -> None:
    now = datetime.now(timezone.utc)
    normalized_database_url = str(database_url or "").strip()
    if normalized_database_url:
        with _connect(normalized_database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE propertyquarry_google_identity_sessions SET last_seen_at = %s WHERE session_id = %s AND status = 'active'",
                    (now, session_id),
                )
            connection.commit()
        return
    with _MEMORY_LOCK:
        if session_id in _MEMORY_SESSIONS:
            _MEMORY_SESSIONS[session_id]["last_seen_at"] = now.isoformat()


def revoke_propertyquarry_identity_session(
    *,
    token: str,
    database_url: str = "",
    actor: str = "browser",
) -> bool:
    payload = resolve_propertyquarry_identity_session(token=token, database_url=database_url, touch=False)
    if not payload:
        return False
    session_id = str(payload["session_id"])
    principal_id = str(payload["principal_id"])
    now = datetime.now(timezone.utc)
    audit = {
        "audit_id": f"pqia_{uuid4().hex}",
        "principal_id": principal_id,
        "session_id": session_id,
        "event_type": "propertyquarry_identity_session_revoked",
        "metadata_json": {"actor": str(actor or "browser").strip()[:200]},
        "occurred_at": now.isoformat(),
    }
    normalized_database_url = str(database_url or "").strip()
    if normalized_database_url:
        try:
            from psycopg.types.json import Json
        except Exception as exc:  # pragma: no cover - import guard
            raise RuntimeError("propertyquarry_google_identity_storage_unavailable") from exc
        with _connect(normalized_database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE propertyquarry_google_identity_sessions SET status = 'revoked', revoked_at = %s WHERE session_id = %s",
                    (now, session_id),
                )
                cursor.execute(
                    """
                    INSERT INTO propertyquarry_google_identity_audit (
                        audit_id, principal_id, session_id, event_type, metadata_json, occurred_at
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        audit["audit_id"],
                        principal_id,
                        session_id,
                        audit["event_type"],
                        Json(audit["metadata_json"]),
                        now,
                    ),
                )
            connection.commit()
        return True
    with _MEMORY_LOCK:
        if session_id in _MEMORY_SESSIONS:
            _MEMORY_SESSIONS[session_id]["status"] = "revoked"
            _MEMORY_SESSIONS[session_id]["revoked_at"] = now.isoformat()
        _MEMORY_AUDIT.append(audit)
    return True


def reset_propertyquarry_google_identity_memory_for_tests() -> None:
    reset_propertyquarry_google_identity_schema_cache_for_tests()
    with _MEMORY_LOCK:
        _MEMORY_ACCOUNTS.clear()
        _MEMORY_SESSIONS.clear()
        _MEMORY_AUDIT.clear()
        _MEMORY_CONSUMED_STATES.clear()
