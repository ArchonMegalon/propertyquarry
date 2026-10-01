#!/usr/bin/env python3
"""Verify independent reviewer authorization for MagicFit delivery evidence.

The verifier is deliberately independent from acceptance and public-serving
code.  A detached Ed25519 signature authorizes one exact delivery/evidence
subject, while the verification key is resolved only through a configured,
locally trusted store.  Authorization input can name a key but can never
provide or redirect its trust anchor.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Mapping, Sequence


REVIEWER_AUTHORIZATION_CONTRACT = (
    "propertyquarry.magicfit_reviewer_authorization.v1"
)
REVIEWER_TRUST_STORE_CONTRACT = (
    "propertyquarry.magicfit_reviewer_trust_store.v1"
)
REVIEWER_PUBLIC_KEY_CONTRACT = (
    "propertyquarry.magicfit_reviewer_public_key.v1"
)
REVIEWER_AUTHORIZATION_ALGORITHM = "Ed25519"
REVIEWER_AUTHORIZATION_DOMAIN = (
    b"PropertyQuarry\x00MagicFitReviewerAuthorization\x00v1\x00"
)

REVIEWER_TRUST_STORE_ENV = "PROPERTYQUARRY_MAGICFIT_REVIEWER_TRUST_STORE_FILE"
PUBLIC_TOUR_ROOT_ENV = "EA_PUBLIC_TOUR_DIR"
AUTHORIZATION_MAX_TTL_ENV = (
    "PROPERTYQUARRY_MAGICFIT_REVIEWER_AUTH_MAX_TTL_SECONDS"
)
REVIEWER_TEST_OWNER_UID_ENV = (
    "PROPERTYQUARRY_MAGICFIT_TEST_TRUST_OWNER_UID"
)

DEFAULT_PUBLIC_TOUR_ROOT = "/data/public_property_tours"
DEFAULT_AUTHORIZATION_MAX_TTL_SECONDS = 24 * 60 * 60
HARD_AUTHORIZATION_MAX_TTL_SECONDS = 7 * 24 * 60 * 60
AUTHORIZATION_MAX_BYTES = 64 * 1024
TRUST_STORE_MAX_BYTES = 1024 * 1024
PUBLIC_KEY_RECORD_MAX_BYTES = 16 * 1024
TRUST_STORE_MAX_KEYS = 64

_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_STABLE_ID_RE = re.compile(r"\A[A-Za-z0-9](?:[A-Za-z0-9._:-]{0,127})?\Z")
_UTC_SECONDS_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")

_SUBJECT_FIELDS = frozenset(
    {
        "delivery_digest",
        "video_sha256",
        "staged_manifest_sha256",
        "browser_receipt_sha256",
        "evidence_receipt_sha256",
        "visual_review_sha256",
        "contact_sheet_sha256",
        "reviewed_at",
    }
)
_UNSIGNED_AUTHORIZATION_FIELDS = frozenset(
    {
        "contract_name",
        "algorithm",
        "key_id",
        "authority_id",
        "issued_at",
        "expires_at",
        "subject",
    }
)
_AUTHORIZATION_FIELDS = frozenset(
    {*_UNSIGNED_AUTHORIZATION_FIELDS, "signature_base64"}
)
_TRUST_STORE_FIELDS = frozenset({"contract_name", "keys"})
_TRUST_STORE_KEY_FIELDS = frozenset(
    {
        "key_id",
        "authority_id",
        "public_key_relpath",
        "valid_from",
        "valid_until",
        "revoked",
    }
)
_PUBLIC_KEY_RECORD_FIELDS = frozenset(
    {
        "contract_name",
        "algorithm",
        "key_id",
        "authority_id",
        "public_key_base64",
    }
)


class MagicFitReviewerAuthorityError(ValueError):
    """Fail-closed reviewer authorization error with a stable reason."""

    def __init__(self, reason: str) -> None:
        self.reason = str(reason)
        super().__init__(self.reason)


class _DuplicateJsonKey(ValueError):
    pass


@dataclass(frozen=True)
class _SecureFileSnapshot:
    body: bytes
    sha256: str
    identity: tuple[int, int, int, int, int, int, int, int]


@dataclass(frozen=True)
class _TrustedReviewerKey:
    key_id: str
    authority_id: str
    public_key_path: Path
    valid_from: datetime
    valid_until: datetime
    revoked: bool


@dataclass(frozen=True)
class VerifiedMagicFitReviewerAuthorization:
    """Small, non-secret projection suitable for accepted-v4 receipts."""

    contract_name: str
    algorithm: str
    key_id: str
    authority_id: str
    delivery_digest: str
    reviewed_at: str
    issued_at: str
    expires_at: str
    authorization_sha256: str
    signing_payload_sha256: str
    subject_sha256: str
    trust_store_sha256: str
    public_key_record_sha256: str
    public_key_sha256: str

    def as_dict(self) -> dict[str, str]:
        return {
            "contract_name": self.contract_name,
            "algorithm": self.algorithm,
            "key_id": self.key_id,
            "authority_id": self.authority_id,
            "delivery_digest": self.delivery_digest,
            "reviewed_at": self.reviewed_at,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "authorization_sha256": self.authorization_sha256,
            "signing_payload_sha256": self.signing_payload_sha256,
            "subject_sha256": self.subject_sha256,
            "trust_store_sha256": self.trust_store_sha256,
            "public_key_record_sha256": self.public_key_record_sha256,
            "public_key_sha256": self.public_key_sha256,
        }


def _strict_json_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"nonfinite:{value}")


def _strict_json_bytes(body: bytes, *, reason: str) -> dict[str, object]:
    try:
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_nonfinite_json,
        )
    except Exception as exc:
        raise MagicFitReviewerAuthorityError(reason) from exc
    if not isinstance(payload, dict):
        raise MagicFitReviewerAuthorityError(reason)
    return dict(payload)


def _canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MagicFitReviewerAuthorityError(
            "magicfit_reviewer_authorization_json_invalid"
        ) from exc


def _stable_id(value: object, *, reason: str) -> str:
    if not isinstance(value, str) or _STABLE_ID_RE.fullmatch(value) is None:
        raise MagicFitReviewerAuthorityError(reason)
    return value


def _sha256(value: object, *, reason: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise MagicFitReviewerAuthorityError(reason)
    return value


def _utc_timestamp(value: object, *, reason: str) -> tuple[str, datetime]:
    if not isinstance(value, str) or _UTC_SECONDS_RE.fullmatch(value) is None:
        raise MagicFitReviewerAuthorityError(reason)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MagicFitReviewerAuthorityError(reason) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise MagicFitReviewerAuthorityError(reason)
    return value, parsed.astimezone(timezone.utc)


def _normalized_subject(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != _SUBJECT_FIELDS:
        raise MagicFitReviewerAuthorityError(
            "magicfit_reviewer_authorization_subject_invalid"
        )
    payload = dict(value)
    normalized = {
        field: _sha256(
            payload.get(field),
            reason="magicfit_reviewer_authorization_subject_invalid",
        )
        for field in _SUBJECT_FIELDS
        if field != "reviewed_at"
    }
    reviewed_at, _reviewed = _utc_timestamp(
        payload.get("reviewed_at"),
        reason="magicfit_reviewer_authorization_subject_invalid",
    )
    normalized["reviewed_at"] = reviewed_at
    return normalized


def _normalized_unsigned_authorization(
    value: Mapping[str, object] | object,
) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != _UNSIGNED_AUTHORIZATION_FIELDS:
        raise MagicFitReviewerAuthorityError(
            "magicfit_reviewer_authorization_contract_invalid"
        )
    payload = dict(value)
    if (
        payload.get("contract_name") != REVIEWER_AUTHORIZATION_CONTRACT
        or payload.get("algorithm") != REVIEWER_AUTHORIZATION_ALGORITHM
    ):
        raise MagicFitReviewerAuthorityError(
            "magicfit_reviewer_authorization_contract_invalid"
        )
    key_id = _stable_id(
        payload.get("key_id"),
        reason="magicfit_reviewer_authorization_identity_invalid",
    )
    authority_id = _stable_id(
        payload.get("authority_id"),
        reason="magicfit_reviewer_authorization_identity_invalid",
    )
    issued_at, _issued = _utc_timestamp(
        payload.get("issued_at"),
        reason="magicfit_reviewer_authorization_timestamp_invalid",
    )
    expires_at, _expires = _utc_timestamp(
        payload.get("expires_at"),
        reason="magicfit_reviewer_authorization_timestamp_invalid",
    )
    return {
        "contract_name": REVIEWER_AUTHORIZATION_CONTRACT,
        "algorithm": REVIEWER_AUTHORIZATION_ALGORITHM,
        "key_id": key_id,
        "authority_id": authority_id,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "subject": _normalized_subject(payload.get("subject")),
    }


def magicfit_reviewer_authorization_signing_bytes(
    unsigned_authorization: Mapping[str, object],
) -> bytes:
    """Return the sole domain-separated byte sequence reviewers must sign."""

    normalized = _normalized_unsigned_authorization(unsigned_authorization)
    return REVIEWER_AUTHORIZATION_DOMAIN + _canonical_json_bytes(normalized)


def _absolute_lexical_path(value: str | os.PathLike[str] | Path, *, reason: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts[1:]):
        raise MagicFitReviewerAuthorityError(reason)
    return Path(os.path.abspath(os.fspath(path)))


def _path_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _trusted_owner_uids(
    configured: Sequence[int] | None,
) -> frozenset[int]:
    if configured is None:
        # The process consuming an authorization must not implicitly become a
        # trust-store administrator.  Development callers may opt into an
        # explicit owner set, but production defaults to root-provisioned
        # authority material only.
        return frozenset({0})
    if (
        not configured
        or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in configured)
    ):
        raise MagicFitReviewerAuthorityError(
            "magicfit_reviewer_trusted_owner_invalid"
        )
    return frozenset({0, *configured})


def _validate_directory(
    details: os.stat_result,
    *,
    final_parent: bool,
    allowed_owner_uids: frozenset[int],
    reason: str,
) -> None:
    mode = stat.S_IMODE(details.st_mode)
    if not stat.S_ISDIR(details.st_mode) or details.st_uid not in allowed_owner_uids:
        raise MagicFitReviewerAuthorityError(reason)
    writable_by_untrusted = bool(mode & 0o022)
    root_sticky_ancestor = bool(mode & stat.S_ISVTX) and details.st_uid == 0
    if writable_by_untrusted and (final_parent or not root_sticky_ancestor):
        raise MagicFitReviewerAuthorityError(reason)


def _secure_file_identity(
    details: os.stat_result,
) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        int(details.st_dev),
        int(details.st_ino),
        int(details.st_mode),
        int(details.st_nlink),
        int(details.st_size),
        int(details.st_mtime_ns),
        int(details.st_ctime_ns),
        int(details.st_uid),
    )


def _read_secure_file(
    path: Path,
    *,
    maximum_bytes: int,
    allowed_owner_uids: frozenset[int],
    reason: str,
) -> _SecureFileSnapshot:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise MagicFitReviewerAuthorityError(reason)
    absolute = _absolute_lexical_path(path, reason=reason)
    parts = absolute.parts
    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    file_flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    directory_fd = -1
    descriptor = -1
    try:
        directory_fd = os.open(absolute.anchor, directory_flags)
        _validate_directory(
            os.fstat(directory_fd),
            final_parent=len(parts) == 1,
            allowed_owner_uids=allowed_owner_uids,
            reason=reason,
        )
        parent_components = parts[1:-1]
        for index, component in enumerate(parent_components):
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
            _validate_directory(
                os.fstat(directory_fd),
                final_parent=index == len(parent_components) - 1,
                allowed_owner_uids=allowed_owner_uids,
                reason=reason,
            )
        descriptor = os.open(parts[-1], file_flags, dir_fd=directory_fd)
        before = os.fstat(descriptor)
        mode = stat.S_IMODE(before.st_mode)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid not in allowed_owner_uids
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > maximum_bytes
            or mode & 0o022
            or mode & 0o111
            or mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
        ):
            raise MagicFitReviewerAuthorityError(reason)
        chunks: list[bytes] = []
        remaining = int(before.st_size)
        while remaining:
            chunk = os.read(descriptor, min(remaining, 65_536))
            if not chunk:
                raise MagicFitReviewerAuthorityError(reason)
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise MagicFitReviewerAuthorityError(reason)
        after = os.fstat(descriptor)
        if _secure_file_identity(before) != _secure_file_identity(after):
            raise MagicFitReviewerAuthorityError(reason)
        body = b"".join(chunks)
        return _SecureFileSnapshot(
            body=body,
            sha256=hashlib.sha256(body).hexdigest(),
            identity=_secure_file_identity(after),
        )
    except MagicFitReviewerAuthorityError:
        raise
    except OSError as exc:
        raise MagicFitReviewerAuthorityError(reason) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory_fd >= 0:
            os.close(directory_fd)


def _canonical_key_relpath(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(
            ord(character) < 0x20
            or ord(character) == 0x7F
            or 0xD800 <= ord(character) <= 0xDFFF
            for character in value
        )
    ):
        return ""
    return value if PurePosixPath(value).as_posix() == value else ""


def _load_trust_store(
    snapshot: _SecureFileSnapshot,
    *,
    trust_store_path: Path,
    public_tour_root: Path,
) -> dict[str, _TrustedReviewerKey]:
    payload = _strict_json_bytes(
        snapshot.body,
        reason="magicfit_reviewer_trust_store_invalid",
    )
    if (
        set(payload) != _TRUST_STORE_FIELDS
        or payload.get("contract_name") != REVIEWER_TRUST_STORE_CONTRACT
        or not isinstance(payload.get("keys"), list)
        or not payload["keys"]
        or len(payload["keys"]) > TRUST_STORE_MAX_KEYS
    ):
        raise MagicFitReviewerAuthorityError(
            "magicfit_reviewer_trust_store_invalid"
        )
    result: dict[str, _TrustedReviewerKey] = {}
    trust_root = trust_store_path.parent
    for raw_entry in payload["keys"]:
        if not isinstance(raw_entry, Mapping) or set(raw_entry) != _TRUST_STORE_KEY_FIELDS:
            raise MagicFitReviewerAuthorityError(
                "magicfit_reviewer_trust_store_invalid"
            )
        entry = dict(raw_entry)
        key_id = _stable_id(
            entry.get("key_id"),
            reason="magicfit_reviewer_trust_store_invalid",
        )
        authority_id = _stable_id(
            entry.get("authority_id"),
            reason="magicfit_reviewer_trust_store_invalid",
        )
        relpath = _canonical_key_relpath(entry.get("public_key_relpath"))
        valid_from_text, valid_from = _utc_timestamp(
            entry.get("valid_from"),
            reason="magicfit_reviewer_trust_store_invalid",
        )
        valid_until_text, valid_until = _utc_timestamp(
            entry.get("valid_until"),
            reason="magicfit_reviewer_trust_store_invalid",
        )
        del valid_from_text, valid_until_text
        revoked = entry.get("revoked")
        if (
            not relpath
            or not isinstance(revoked, bool)
            or valid_until <= valid_from
            or key_id in result
        ):
            raise MagicFitReviewerAuthorityError(
                "magicfit_reviewer_trust_store_invalid"
            )
        public_key_path = _absolute_lexical_path(
            trust_root / PurePosixPath(relpath),
            reason="magicfit_reviewer_trust_store_invalid",
        )
        if (
            not _path_within(public_key_path, trust_root)
            or _path_within(public_key_path, public_tour_root)
        ):
            raise MagicFitReviewerAuthorityError(
                "magicfit_reviewer_trust_store_invalid"
            )
        result[key_id] = _TrustedReviewerKey(
            key_id=key_id,
            authority_id=authority_id,
            public_key_path=public_key_path,
            valid_from=valid_from,
            valid_until=valid_until,
            revoked=revoked,
        )
    return result


def _strict_base64(value: object, *, length: int, reason: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise MagicFitReviewerAuthorityError(reason)
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeError, ValueError) as exc:
        raise MagicFitReviewerAuthorityError(reason) from exc
    if (
        len(decoded) != length
        or base64.b64encode(decoded).decode("ascii") != value
    ):
        raise MagicFitReviewerAuthorityError(reason)
    return decoded


def _load_public_key(
    snapshot: _SecureFileSnapshot,
    *,
    expected: _TrustedReviewerKey,
) -> bytes:
    payload = _strict_json_bytes(
        snapshot.body,
        reason="magicfit_reviewer_public_key_invalid",
    )
    if (
        set(payload) != _PUBLIC_KEY_RECORD_FIELDS
        or payload.get("contract_name") != REVIEWER_PUBLIC_KEY_CONTRACT
        or payload.get("algorithm") != REVIEWER_AUTHORIZATION_ALGORITHM
        or payload.get("key_id") != expected.key_id
        or payload.get("authority_id") != expected.authority_id
    ):
        raise MagicFitReviewerAuthorityError(
            "magicfit_reviewer_public_key_invalid"
        )
    return _strict_base64(
        payload.get("public_key_base64"),
        length=32,
        reason="magicfit_reviewer_public_key_invalid",
    )


def _configured_max_ttl(value: int | None) -> int:
    if value is not None:
        parsed = value
    else:
        raw = os.getenv(AUTHORIZATION_MAX_TTL_ENV)
        if raw is None:
            parsed = DEFAULT_AUTHORIZATION_MAX_TTL_SECONDS
        else:
            try:
                parsed = int(raw.strip())
            except (AttributeError, TypeError, ValueError) as exc:
                raise MagicFitReviewerAuthorityError(
                    "magicfit_reviewer_authorization_ttl_policy_invalid"
                ) from exc
    if (
        isinstance(parsed, bool)
        or not isinstance(parsed, int)
        or parsed <= 0
        or parsed > HARD_AUTHORIZATION_MAX_TTL_SECONDS
    ):
        raise MagicFitReviewerAuthorityError(
            "magicfit_reviewer_authorization_ttl_policy_invalid"
        )
    return parsed


def _observed_at(value: datetime | None) -> datetime:
    observed = value or datetime.now(timezone.utc)
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise MagicFitReviewerAuthorityError(
            "magicfit_reviewer_authorization_clock_invalid"
        )
    return observed.astimezone(timezone.utc)


def magicfit_reviewer_test_allowed_owner_uids() -> list[int] | None:
    """Return an explicit non-root owner override only inside non-prod pytest.

    Production callers have no general owner configuration: reviewer trust
    material defaults to root ownership.  Subprocess integration tests must
    opt in with both pytest's per-test marker and an exact current-uid value;
    merely injecting a pytest-like environment variable is insufficient.
    """

    raw = os.getenv(REVIEWER_TEST_OWNER_UID_ENV)
    if raw is None:
        return None
    runtime_mode = str(os.getenv("EA_RUNTIME_MODE") or "").strip().lower()
    expected = str(os.geteuid())
    if (
        not os.getenv("PYTEST_CURRENT_TEST")
        or runtime_mode in {"prod", "production"}
        or raw != expected
    ):
        raise MagicFitReviewerAuthorityError(
            "magicfit_reviewer_test_owner_override_invalid"
        )
    return [os.geteuid()]


def verify_magicfit_reviewer_authorization(
    authorization_path: str | os.PathLike[str] | Path,
    *,
    expected_subject: Mapping[str, object],
    trust_store_path: str | os.PathLike[str] | Path | None = None,
    public_tour_root: str | os.PathLike[str] | Path | None = None,
    observed_at: datetime | None = None,
    maximum_ttl_seconds: int | None = None,
    allowed_owner_uids: Sequence[int] | None = None,
) -> VerifiedMagicFitReviewerAuthorization:
    """Verify one exact authorization and return a non-secret audit projection."""

    owners = _trusted_owner_uids(allowed_owner_uids)
    now = _observed_at(observed_at)
    ttl_limit = _configured_max_ttl(maximum_ttl_seconds)
    configured_trust_store = (
        trust_store_path
        if trust_store_path is not None
        else str(os.getenv(REVIEWER_TRUST_STORE_ENV) or "").strip()
    )
    if not configured_trust_store:
        raise MagicFitReviewerAuthorityError(
            "magicfit_reviewer_trust_store_missing"
        )
    configured_public_root = (
        public_tour_root
        if public_tour_root is not None
        else str(os.getenv(PUBLIC_TOUR_ROOT_ENV) or DEFAULT_PUBLIC_TOUR_ROOT).strip()
    )
    trust_path = _absolute_lexical_path(
        configured_trust_store,
        reason="magicfit_reviewer_trust_store_invalid",
    )
    public_root = _absolute_lexical_path(
        configured_public_root,
        reason="magicfit_reviewer_public_root_invalid",
    )
    auth_path = _absolute_lexical_path(
        authorization_path,
        reason="magicfit_reviewer_authorization_file_invalid",
    )
    if _path_within(trust_path, public_root):
        raise MagicFitReviewerAuthorityError(
            "magicfit_reviewer_trust_store_public_root_forbidden"
        )
    if _path_within(auth_path, public_root):
        raise MagicFitReviewerAuthorityError(
            "magicfit_reviewer_authorization_public_root_forbidden"
        )

    authorization_snapshot = _read_secure_file(
        auth_path,
        maximum_bytes=AUTHORIZATION_MAX_BYTES,
        allowed_owner_uids=owners,
        reason="magicfit_reviewer_authorization_file_invalid",
    )
    return _verify_magicfit_reviewer_authorization_snapshot(
        authorization_snapshot,
        expected_subject=expected_subject,
        trust_path=trust_path,
        public_root=public_root,
        now=now,
        ttl_limit=ttl_limit,
        owners=owners,
    )


def _verify_magicfit_reviewer_authorization_snapshot(
    authorization_snapshot: _SecureFileSnapshot,
    *,
    expected_subject: Mapping[str, object],
    trust_path: Path,
    public_root: Path,
    now: datetime,
    ttl_limit: int,
    owners: frozenset[int],
) -> VerifiedMagicFitReviewerAuthorization:
    authorization = _strict_json_bytes(
        authorization_snapshot.body,
        reason="magicfit_reviewer_authorization_contract_invalid",
    )
    if set(authorization) != _AUTHORIZATION_FIELDS:
        raise MagicFitReviewerAuthorityError(
            "magicfit_reviewer_authorization_contract_invalid"
        )
    signature = _strict_base64(
        authorization.get("signature_base64"),
        length=64,
        reason="magicfit_reviewer_authorization_signature_invalid",
    )
    unsigned = {
        key: value
        for key, value in authorization.items()
        if key != "signature_base64"
    }
    normalized_unsigned = _normalized_unsigned_authorization(unsigned)
    normalized_expected_subject = _normalized_subject(expected_subject)
    if normalized_unsigned["subject"] != normalized_expected_subject:
        raise MagicFitReviewerAuthorityError(
            "magicfit_reviewer_authorization_subject_mismatch"
        )

    trust_snapshot = _read_secure_file(
        trust_path,
        maximum_bytes=TRUST_STORE_MAX_BYTES,
        allowed_owner_uids=owners,
        reason="magicfit_reviewer_trust_store_invalid",
    )
    trusted_keys = _load_trust_store(
        trust_snapshot,
        trust_store_path=trust_path,
        public_tour_root=public_root,
    )
    key_id = str(normalized_unsigned["key_id"])
    authority_id = str(normalized_unsigned["authority_id"])
    trusted_key = trusted_keys.get(key_id)
    if trusted_key is None:
        raise MagicFitReviewerAuthorityError(
            "magicfit_reviewer_authorization_key_unknown"
        )
    if trusted_key.authority_id != authority_id:
        raise MagicFitReviewerAuthorityError(
            "magicfit_reviewer_authorization_authority_unknown"
        )
    if trusted_key.revoked:
        raise MagicFitReviewerAuthorityError(
            "magicfit_reviewer_authorization_key_revoked"
        )

    issued_at_text, issued_at = _utc_timestamp(
        normalized_unsigned["issued_at"],
        reason="magicfit_reviewer_authorization_timestamp_invalid",
    )
    expires_at_text, expires_at = _utc_timestamp(
        normalized_unsigned["expires_at"],
        reason="magicfit_reviewer_authorization_timestamp_invalid",
    )
    reviewed_at_text, reviewed_at_value = _utc_timestamp(
        normalized_expected_subject["reviewed_at"],
        reason="magicfit_reviewer_authorization_subject_invalid",
    )
    if (
        issued_at > now
        or expires_at <= now
        or expires_at <= issued_at
        or expires_at - issued_at > timedelta(seconds=ttl_limit)
    ):
        raise MagicFitReviewerAuthorityError(
            "magicfit_reviewer_authorization_expired_or_invalid"
        )
    if not (
        trusted_key.valid_from
        <= reviewed_at_value
        <= issued_at
        < expires_at
        <= trusted_key.valid_until
    ):
        raise MagicFitReviewerAuthorityError(
            "magicfit_reviewer_authorization_authority_window_invalid"
        )

    public_key_snapshot = _read_secure_file(
        trusted_key.public_key_path,
        maximum_bytes=PUBLIC_KEY_RECORD_MAX_BYTES,
        allowed_owner_uids=owners,
        reason="magicfit_reviewer_public_key_invalid",
    )
    public_key_bytes = _load_public_key(
        public_key_snapshot,
        expected=trusted_key,
    )
    signing_bytes = magicfit_reviewer_authorization_signing_bytes(
        normalized_unsigned
    )
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
    except (ImportError, ModuleNotFoundError) as exc:
        raise MagicFitReviewerAuthorityError(
            "magicfit_reviewer_authorization_crypto_unavailable"
        ) from exc
    try:
        Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(
            signature,
            signing_bytes,
        )
    except InvalidSignature as exc:
        raise MagicFitReviewerAuthorityError(
            "magicfit_reviewer_authorization_signature_invalid"
        ) from exc
    except ValueError as exc:
        raise MagicFitReviewerAuthorityError(
            "magicfit_reviewer_public_key_invalid"
        ) from exc

    subject_bytes = _canonical_json_bytes(normalized_expected_subject)
    return VerifiedMagicFitReviewerAuthorization(
        contract_name=REVIEWER_AUTHORIZATION_CONTRACT,
        algorithm=REVIEWER_AUTHORIZATION_ALGORITHM,
        key_id=key_id,
        authority_id=authority_id,
        delivery_digest=normalized_expected_subject["delivery_digest"],
        reviewed_at=reviewed_at_text,
        issued_at=issued_at_text,
        expires_at=expires_at_text,
        authorization_sha256=authorization_snapshot.sha256,
        signing_payload_sha256=hashlib.sha256(signing_bytes).hexdigest(),
        subject_sha256=hashlib.sha256(subject_bytes).hexdigest(),
        trust_store_sha256=trust_snapshot.sha256,
        public_key_record_sha256=public_key_snapshot.sha256,
        public_key_sha256=hashlib.sha256(public_key_bytes).hexdigest(),
    )


def verify_magicfit_reviewer_authorization_bytes(
    authorization_bytes: bytes,
    *,
    expected_subject: Mapping[str, object],
    trust_store_path: str | os.PathLike[str] | Path | None = None,
    public_tour_root: str | os.PathLike[str] | Path | None = None,
    observed_at: datetime | None = None,
    maximum_ttl_seconds: int | None = None,
    allowed_owner_uids: Sequence[int] | None = None,
) -> VerifiedMagicFitReviewerAuthorization:
    """Verify bounded authorization bytes read by a trusted descriptor lane.

    This entry point is intended for serve-time re-verification after the
    caller has already performed its own descriptor-relative, no-follow read
    of a persisted public-bundle artifact.  Only authorization bytes cross
    this API: the trust store and referenced key are still independently read
    through this module's strict external-path checks.
    """

    if (
        not isinstance(authorization_bytes, bytes)
        or not authorization_bytes
        or len(authorization_bytes) > AUTHORIZATION_MAX_BYTES
    ):
        raise MagicFitReviewerAuthorityError(
            "magicfit_reviewer_authorization_bytes_invalid"
        )
    owners = _trusted_owner_uids(allowed_owner_uids)
    now = _observed_at(observed_at)
    ttl_limit = _configured_max_ttl(maximum_ttl_seconds)
    configured_trust_store = (
        trust_store_path
        if trust_store_path is not None
        else str(os.getenv(REVIEWER_TRUST_STORE_ENV) or "").strip()
    )
    if not configured_trust_store:
        raise MagicFitReviewerAuthorityError(
            "magicfit_reviewer_trust_store_missing"
        )
    configured_public_root = (
        public_tour_root
        if public_tour_root is not None
        else str(os.getenv(PUBLIC_TOUR_ROOT_ENV) or DEFAULT_PUBLIC_TOUR_ROOT).strip()
    )
    trust_path = _absolute_lexical_path(
        configured_trust_store,
        reason="magicfit_reviewer_trust_store_invalid",
    )
    public_root = _absolute_lexical_path(
        configured_public_root,
        reason="magicfit_reviewer_public_root_invalid",
    )
    if _path_within(trust_path, public_root):
        raise MagicFitReviewerAuthorityError(
            "magicfit_reviewer_trust_store_public_root_forbidden"
        )
    authorization_snapshot = _SecureFileSnapshot(
        body=authorization_bytes,
        sha256=hashlib.sha256(authorization_bytes).hexdigest(),
        identity=(0, 0, 0, 0, len(authorization_bytes), 0, 0, 0),
    )
    return _verify_magicfit_reviewer_authorization_snapshot(
        authorization_snapshot,
        expected_subject=expected_subject,
        trust_path=trust_path,
        public_root=public_root,
        now=now,
        ttl_limit=ttl_limit,
        owners=owners,
    )
