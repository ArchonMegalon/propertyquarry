from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from app.product.propertyquarry_google_identity_schema import (
    require_propertyquarry_google_identity_schema_ready,
)


REGISTRATION_CHALLENGE_PREFIX = "pqrv2_"
REGISTRATION_CHALLENGE_TTL_SECONDS = 15 * 60
REGISTRATION_RESEND_COOLDOWN_SECONDS = 60
REGISTRATION_SEND_WINDOW_SECONDS = 60 * 60
REGISTRATION_MAX_SENDS_PER_WINDOW = 5
REGISTRATION_MAX_VERIFY_ATTEMPTS = 5


@dataclass(frozen=True)
class IssuedRegistrationChallenge:
    email: str
    return_to: str
    token: str
    verification_code: str
    expires_at: int
    resend_available_at: int
    resend_cooldown_seconds: int


@dataclass(frozen=True)
class VerifiedRegistrationChallenge:
    email: str
    return_to: str
    expires_at: int
    grant: str
    finalized: bool = False


class RegistrationChallengeError(RuntimeError):
    def __init__(self, code: str, *, retry_after_seconds: int = 0) -> None:
        super().__init__(code)
        self.code = str(code or "registration_verification_invalid")
        self.retry_after_seconds = max(0, int(retry_after_seconds or 0))


_MEMORY_LOCK = threading.RLock()
_MEMORY_CHALLENGES: dict[str, dict[str, object]] = {}


def _connect(database_url: str):  # type: ignore[no-untyped-def]
    try:
        import psycopg
    except Exception as exc:  # pragma: no cover - import guard
        raise RuntimeError("propertyquarry_registration_storage_unavailable") from exc
    return psycopg.connect(str(database_url or "").strip())


def _email_hash(email: str) -> str:
    return hashlib.sha256(str(email or "").strip().lower().encode("utf-8")).hexdigest()


def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").strip().encode("utf-8")).hexdigest()


def _code_digest(*, secret: str, challenge_id: str, verification_code: str) -> str:
    material = (
        "propertyquarry-registration-code-v2\0"
        f"{challenge_id}\0{str(verification_code or '').strip()}"
    ).encode("utf-8")
    return hmac.new(str(secret or "").encode("utf-8"), material, hashlib.sha256).hexdigest()


def _verification_grant(*, secret: str, challenge_id: str, token_hash: str) -> str:
    material = (
        "propertyquarry-registration-verified-grant-v2\0"
        f"{challenge_id}\0{token_hash}"
    ).encode("utf-8")
    digest = hmac.new(
        str(secret or "").encode("utf-8"),
        material,
        hashlib.sha256,
    ).hexdigest()
    return f"pqrg2_{digest}"


def _unix_seconds(value: object) -> int:
    if isinstance(value, datetime):
        normalized = value
        if normalized.tzinfo is None:
            normalized = normalized.replace(tzinfo=timezone.utc)
        return int(normalized.timestamp())
    return int(value or 0)


def _retry_after(*, now: int, available_at: int) -> int:
    return max(1, int(available_at) - int(now))


def _next_send_window(
    *,
    previous: dict[str, object] | None,
    now: int,
) -> tuple[int, int]:
    if previous:
        last_sent_at = _unix_seconds(previous.get("last_sent_at"))
        if last_sent_at > 0 and now < last_sent_at + REGISTRATION_RESEND_COOLDOWN_SECONDS:
            raise RegistrationChallengeError(
                "registration_verification_resend_too_soon",
                retry_after_seconds=_retry_after(
                    now=now,
                    available_at=last_sent_at + REGISTRATION_RESEND_COOLDOWN_SECONDS,
                ),
            )
        window_started_at = _unix_seconds(previous.get("window_started_at"))
        send_count = int(previous.get("send_count") or 0)
        if window_started_at > 0 and now < window_started_at + REGISTRATION_SEND_WINDOW_SECONDS:
            if send_count >= REGISTRATION_MAX_SENDS_PER_WINDOW:
                raise RegistrationChallengeError(
                    "registration_verification_rate_limited",
                    retry_after_seconds=_retry_after(
                        now=now,
                        available_at=window_started_at + REGISTRATION_SEND_WINDOW_SECONDS,
                    ),
                )
            return window_started_at, send_count + 1
    return now, 1


def issue_registration_challenge(
    *,
    email: str,
    return_to: str,
    secret: str,
    database_url: str = "",
    now: int | None = None,
) -> IssuedRegistrationChallenge:
    normalized_email = str(email or "").strip().lower()
    normalized_return_to = str(return_to or "").strip() or "/app/search"
    issued_at = int(time.time()) if now is None else int(now)
    expires_at = issued_at + REGISTRATION_CHALLENGE_TTL_SECONDS
    challenge_id = f"pqrc_{secrets.token_urlsafe(24)}"
    token = f"{REGISTRATION_CHALLENGE_PREFIX}{secrets.token_urlsafe(32)}"
    verification_code = f"{secrets.randbelow(1_000_000):06d}"
    email_hash = _email_hash(normalized_email)
    record = {
        "email_hash": email_hash,
        "challenge_id": challenge_id,
        "token_hash": _token_hash(token),
        "email": normalized_email,
        "return_to": normalized_return_to,
        "code_digest": _code_digest(
            secret=secret,
            challenge_id=challenge_id,
            verification_code=verification_code,
        ),
        "status": "active",
        "attempt_count": 0,
        "last_sent_at": issued_at,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "verified_at": None,
        "finalized_at": None,
    }
    normalized_database_url = str(database_url or "").strip()
    if normalized_database_url:
        require_propertyquarry_google_identity_schema_ready(normalized_database_url)
        with _connect(normalized_database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"propertyquarry-registration:{email_hash}",),
                )
                cursor.execute(
                    """
                    SELECT status, send_count, window_started_at, last_sent_at
                    FROM propertyquarry_registration_challenges
                    WHERE email_hash = %s
                    FOR UPDATE
                    """,
                    (email_hash,),
                )
                row = cursor.fetchone()
                previous = None
                if row:
                    previous = {
                        "status": row[0],
                        "send_count": row[1],
                        "window_started_at": row[2],
                        "last_sent_at": row[3],
                    }
                window_started_at, send_count = _next_send_window(
                    previous=previous,
                    now=issued_at,
                )
                record["window_started_at"] = window_started_at
                record["send_count"] = send_count
                cursor.execute(
                    """
                    INSERT INTO propertyquarry_registration_challenges (
                        email_hash, challenge_id, token_hash, email, return_to,
                        code_digest, status, attempt_count, send_count,
                        window_started_at, last_sent_at, issued_at, expires_at,
                        verified_at, finalized_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, 'active', 0, %s,
                        %s, %s, %s, %s, NULL, NULL
                    )
                    ON CONFLICT (email_hash) DO UPDATE SET
                        challenge_id = EXCLUDED.challenge_id,
                        token_hash = EXCLUDED.token_hash,
                        email = EXCLUDED.email,
                        return_to = EXCLUDED.return_to,
                        code_digest = EXCLUDED.code_digest,
                        status = 'active',
                        attempt_count = 0,
                        send_count = EXCLUDED.send_count,
                        window_started_at = EXCLUDED.window_started_at,
                        last_sent_at = EXCLUDED.last_sent_at,
                        issued_at = EXCLUDED.issued_at,
                        expires_at = EXCLUDED.expires_at,
                        verified_at = NULL,
                        finalized_at = NULL
                    """,
                    (
                        email_hash,
                        challenge_id,
                        record["token_hash"],
                        normalized_email,
                        normalized_return_to,
                        record["code_digest"],
                        send_count,
                        datetime.fromtimestamp(window_started_at, tz=timezone.utc),
                        datetime.fromtimestamp(issued_at, tz=timezone.utc),
                        datetime.fromtimestamp(issued_at, tz=timezone.utc),
                        datetime.fromtimestamp(expires_at, tz=timezone.utc),
                    ),
                )
            connection.commit()
    else:
        with _MEMORY_LOCK:
            previous = _MEMORY_CHALLENGES.get(email_hash)
            window_started_at, send_count = _next_send_window(
                previous=previous,
                now=issued_at,
            )
            record["window_started_at"] = window_started_at
            record["send_count"] = send_count
            _MEMORY_CHALLENGES[email_hash] = record
    return IssuedRegistrationChallenge(
        email=normalized_email,
        return_to=normalized_return_to,
        token=token,
        verification_code=verification_code,
        expires_at=expires_at,
        resend_available_at=issued_at + REGISTRATION_RESEND_COOLDOWN_SECONDS,
        resend_cooldown_seconds=REGISTRATION_RESEND_COOLDOWN_SECONDS,
    )


def _validated_token_hash(token: str) -> str:
    normalized = str(token or "").strip()
    if (
        not normalized.startswith(REGISTRATION_CHALLENGE_PREFIX)
        or len(normalized) < len(REGISTRATION_CHALLENGE_PREFIX) + 32
        or len(normalized) > 160
    ):
        raise RegistrationChallengeError("registration_verification_invalid")
    return _token_hash(normalized)


def _verify_record(
    *,
    record: dict[str, object],
    verification_code: str,
    secret: str,
    now: int,
) -> tuple[dict[str, object], RegistrationChallengeError | None]:
    status = str(record.get("status") or "").strip()
    if status == "locked":
        return record, RegistrationChallengeError("registration_verification_locked")
    expires_at = _unix_seconds(record.get("expires_at"))
    if status not in {"active", "verified", "finalized"} or expires_at <= now:
        record["status"] = "expired"
        return record, RegistrationChallengeError("registration_verification_expired")
    provided_digest = _code_digest(
        secret=secret,
        challenge_id=str(record.get("challenge_id") or ""),
        verification_code=str(verification_code or "").strip(),
    )
    if not str(verification_code or "").strip() or not hmac.compare_digest(
        str(record.get("code_digest") or ""),
        provided_digest,
    ):
        if status in {"verified", "finalized"}:
            return record, RegistrationChallengeError(
                "registration_verification_code_invalid"
            )
        attempt_count = int(record.get("attempt_count") or 0) + 1
        record["attempt_count"] = attempt_count
        if attempt_count >= REGISTRATION_MAX_VERIFY_ATTEMPTS:
            record["status"] = "locked"
            return record, RegistrationChallengeError("registration_verification_locked")
        return record, RegistrationChallengeError(
            "registration_verification_code_invalid"
        )
    if status == "active":
        record["status"] = "verified"
        record["verified_at"] = now
    return record, None


def verify_registration_challenge(
    *,
    token: str,
    verification_code: str,
    secret: str,
    database_url: str = "",
    now: int | None = None,
) -> VerifiedRegistrationChallenge:
    token_hash = _validated_token_hash(token)
    verified_at = int(time.time()) if now is None else int(now)
    normalized_database_url = str(database_url or "").strip()
    if normalized_database_url:
        require_propertyquarry_google_identity_schema_ready(normalized_database_url)
        record: dict[str, object] | None = None
        verification_error: RegistrationChallengeError | None = None
        with _connect(normalized_database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT email_hash, challenge_id, token_hash, email, return_to,
                           code_digest, status, attempt_count, send_count,
                           window_started_at, last_sent_at, issued_at, expires_at,
                           verified_at, finalized_at
                    FROM propertyquarry_registration_challenges
                    WHERE token_hash = %s
                    FOR UPDATE
                    """,
                    (token_hash,),
                )
                row = cursor.fetchone()
                if row:
                    names = (
                        "email_hash",
                        "challenge_id",
                        "token_hash",
                        "email",
                        "return_to",
                        "code_digest",
                        "status",
                        "attempt_count",
                        "send_count",
                        "window_started_at",
                        "last_sent_at",
                        "issued_at",
                        "expires_at",
                        "verified_at",
                        "finalized_at",
                    )
                    record = dict(zip(names, row))
                    record, verification_error = _verify_record(
                        record=record,
                        verification_code=verification_code,
                        secret=secret,
                        now=verified_at,
                    )
                    cursor.execute(
                        """
                        UPDATE propertyquarry_registration_challenges
                        SET status = %s, attempt_count = %s, verified_at = %s,
                            finalized_at = %s
                        WHERE token_hash = %s
                        """,
                        (
                            record["status"],
                            int(record.get("attempt_count") or 0),
                            (
                                datetime.fromtimestamp(verified_at, tz=timezone.utc)
                                if record.get("verified_at")
                                else None
                            ),
                            (
                                datetime.fromtimestamp(
                                    _unix_seconds(record.get("finalized_at")),
                                    tz=timezone.utc,
                                )
                                if record.get("finalized_at")
                                else None
                            ),
                            token_hash,
                        ),
                    )
            connection.commit()
        if record is None:
            raise RegistrationChallengeError("registration_verification_invalid")
        if verification_error is not None:
            raise verification_error
    else:
        record = None
        verification_error = None
        with _MEMORY_LOCK:
            for current in _MEMORY_CHALLENGES.values():
                if hmac.compare_digest(str(current.get("token_hash") or ""), token_hash):
                    record = current
                    break
            if record is not None:
                record, verification_error = _verify_record(
                    record=record,
                    verification_code=verification_code,
                    secret=secret,
                    now=verified_at,
                )
        if record is None:
            raise RegistrationChallengeError("registration_verification_invalid")
        if verification_error is not None:
            raise verification_error
    return VerifiedRegistrationChallenge(
        email=str(record.get("email") or "").strip().lower(),
        return_to=str(record.get("return_to") or "").strip() or "/app/search",
        expires_at=_unix_seconds(record.get("expires_at")),
        grant=_verification_grant(
            secret=secret,
            challenge_id=str(record.get("challenge_id") or ""),
            token_hash=token_hash,
        ),
        finalized=str(record.get("status") or "").strip() == "finalized",
    )


def _finalize_record(
    *,
    record: dict[str, object],
    grant: str,
    secret: str,
    now: int,
) -> tuple[dict[str, object], RegistrationChallengeError | None]:
    status = str(record.get("status") or "").strip()
    if status in {"expired", "locked"}:
        return record, RegistrationChallengeError(
            "registration_verification_expired"
            if status == "expired"
            else "registration_verification_locked"
        )
    expected_grant = _verification_grant(
        secret=secret,
        challenge_id=str(record.get("challenge_id") or ""),
        token_hash=str(record.get("token_hash") or ""),
    )
    if not hmac.compare_digest(str(grant or "").strip(), expected_grant):
        return record, RegistrationChallengeError(
            "registration_verification_grant_invalid"
        )
    if status == "finalized":
        return record, None
    if status != "verified":
        return record, RegistrationChallengeError(
            "registration_verification_not_verified"
        )
    record["status"] = "finalized"
    record["finalized_at"] = now
    return record, None


def finalize_registration_challenge(
    *,
    token: str,
    grant: str,
    secret: str,
    database_url: str = "",
    now: int | None = None,
) -> VerifiedRegistrationChallenge:
    token_hash = _validated_token_hash(token)
    finalized_at = int(time.time()) if now is None else int(now)
    normalized_database_url = str(database_url or "").strip()
    if normalized_database_url:
        require_propertyquarry_google_identity_schema_ready(normalized_database_url)
        record: dict[str, object] | None = None
        finalize_error: RegistrationChallengeError | None = None
        with _connect(normalized_database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT email_hash, challenge_id, token_hash, email, return_to,
                           code_digest, status, attempt_count, send_count,
                           window_started_at, last_sent_at, issued_at, expires_at,
                           verified_at, finalized_at
                    FROM propertyquarry_registration_challenges
                    WHERE token_hash = %s
                    FOR UPDATE
                    """,
                    (token_hash,),
                )
                row = cursor.fetchone()
                if row:
                    names = (
                        "email_hash",
                        "challenge_id",
                        "token_hash",
                        "email",
                        "return_to",
                        "code_digest",
                        "status",
                        "attempt_count",
                        "send_count",
                        "window_started_at",
                        "last_sent_at",
                        "issued_at",
                        "expires_at",
                        "verified_at",
                        "finalized_at",
                    )
                    record = dict(zip(names, row))
                    record, finalize_error = _finalize_record(
                        record=record,
                        grant=grant,
                        secret=secret,
                        now=finalized_at,
                    )
                    if finalize_error is None:
                        cursor.execute(
                            """
                            UPDATE propertyquarry_registration_challenges
                            SET status = %s, finalized_at = %s
                            WHERE token_hash = %s
                            """,
                            (
                                record["status"],
                                datetime.fromtimestamp(
                                    _unix_seconds(record.get("finalized_at")),
                                    tz=timezone.utc,
                                ),
                                token_hash,
                            ),
                        )
            connection.commit()
        if record is None:
            raise RegistrationChallengeError("registration_verification_invalid")
        if finalize_error is not None:
            raise finalize_error
    else:
        record = None
        finalize_error = None
        with _MEMORY_LOCK:
            for current in _MEMORY_CHALLENGES.values():
                if hmac.compare_digest(str(current.get("token_hash") or ""), token_hash):
                    record = current
                    break
            if record is not None:
                record, finalize_error = _finalize_record(
                    record=record,
                    grant=grant,
                    secret=secret,
                    now=finalized_at,
                )
        if record is None:
            raise RegistrationChallengeError("registration_verification_invalid")
        if finalize_error is not None:
            raise finalize_error
    return VerifiedRegistrationChallenge(
        email=str(record.get("email") or "").strip().lower(),
        return_to=str(record.get("return_to") or "").strip() or "/app/search",
        expires_at=_unix_seconds(record.get("expires_at")),
        grant=_verification_grant(
            secret=secret,
            challenge_id=str(record.get("challenge_id") or ""),
            token_hash=token_hash,
        ),
        finalized=True,
    )


def reset_propertyquarry_registration_identity_memory_for_tests() -> None:
    with _MEMORY_LOCK:
        _MEMORY_CHALLENGES.clear()
