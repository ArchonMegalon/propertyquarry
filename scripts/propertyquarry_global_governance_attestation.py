#!/usr/bin/env python3
"""Verify detached authority attestations used by global launch gates.

The receipt being evaluated may select a key identifier, but it cannot provide
or redirect the corresponding trust material.  Trust stores and public-key
records are read only from a configured external, root-owned file hierarchy.
Every signature is domain-separated and binds one exact gate contract,
release, set of governed source digests, and unsigned receipt payload digest.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Mapping


ATTESTATION_CONTRACT = "propertyquarry.global_governance_attestation.v1"
TRUST_STORE_CONTRACT = "propertyquarry.global_governance_trust_store.v1"
PUBLIC_KEY_CONTRACT = "propertyquarry.global_governance_public_key.v1"
ATTESTATION_ALGORITHM = "Ed25519"
ATTESTATION_DOMAIN = b"PropertyQuarry\x00GlobalGovernanceAttestation\x00v1\x00"

TRUST_STORE_ENV = "PROPERTYQUARRY_GLOBAL_GOVERNANCE_TRUST_STORE_FILE"

GLOBAL_MARKET_GATE_ID = "global_market_envelope"
INCIDENT_SUPPORT_GATE_ID = "incident_support"
GLOBAL_EXPERIENCE_GATE_ID = "global_experience"
JURISDICTION_PRIVACY_RIGHTS_GATE_ID = "jurisdiction_privacy_rights"
ALLOWED_GATE_IDS = frozenset(
    {
        GLOBAL_MARKET_GATE_ID,
        INCIDENT_SUPPORT_GATE_ID,
        GLOBAL_EXPERIENCE_GATE_ID,
        JURISDICTION_PRIVACY_RIGHTS_GATE_ID,
    }
)

DEFAULT_MAXIMUM_TTL_SECONDS = 24 * 60 * 60
ATTESTATION_MAX_BYTES = 64 * 1024
TRUST_STORE_MAX_BYTES = 1024 * 1024
PUBLIC_KEY_RECORD_MAX_BYTES = 16 * 1024
TRUST_STORE_MAX_KEYS = 64
SOURCE_DIGEST_MAXIMUM = 8

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_SHA256_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_GIT_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_STABLE_ID_RE = re.compile(r"\A[A-Za-z0-9](?:[A-Za-z0-9._:@/-]{0,127})?\Z")
_SOURCE_NAME_RE = re.compile(r"\A[a-z][a-z0-9_]{0,63}\Z")
_UTC_SECONDS_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")

_SUBJECT_FIELDS = frozenset(
    {
        "gate_id",
        "receipt_contract",
        "release_commit_sha",
        "release_image_digest",
        "source_digests",
        "payload_sha256",
    }
)
_UNSIGNED_ATTESTATION_FIELDS = frozenset(
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
_ATTESTATION_FIELDS = frozenset(
    {*_UNSIGNED_ATTESTATION_FIELDS, "signature_base64"}
)
_TRUST_STORE_FIELDS = frozenset({"contract_name", "keys"})
_TRUST_STORE_KEY_FIELDS = frozenset(
    {
        "algorithm",
        "key_id",
        "authority_id",
        "public_key_relpath",
        "public_key_record_sha256",
        "valid_from",
        "valid_until",
        "revoked_at",
        "allowed_gate_ids",
    }
)
_PUBLIC_KEY_FIELDS = frozenset(
    {
        "contract_name",
        "algorithm",
        "key_id",
        "authority_id",
        "public_key_base64",
    }
)


class GlobalGovernanceAttestationError(ValueError):
    """Fail-closed verification error with a stable, non-secret reason."""

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
class _TrustedKey:
    key_id: str
    authority_id: str
    public_key_path: Path
    public_key_record_sha256: str
    valid_from: datetime
    valid_until: datetime
    revoked_at: datetime | None
    allowed_gate_ids: frozenset[str]


@dataclass(frozen=True)
class VerifiedGlobalGovernanceAttestation:
    """Non-secret verification projection suitable for audit receipts."""

    contract_name: str
    algorithm: str
    gate_id: str
    receipt_contract: str
    key_id: str
    authority_id: str
    issued_at: str
    expires_at: str
    payload_sha256: str
    subject_sha256: str
    attestation_sha256: str
    signing_payload_sha256: str
    trust_store_sha256: str
    public_key_record_sha256: str
    public_key_sha256: str

    def as_dict(self) -> dict[str, str]:
        return {
            "contract_name": self.contract_name,
            "algorithm": self.algorithm,
            "gate_id": self.gate_id,
            "receipt_contract": self.receipt_contract,
            "key_id": self.key_id,
            "authority_id": self.authority_id,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "payload_sha256": self.payload_sha256,
            "subject_sha256": self.subject_sha256,
            "attestation_sha256": self.attestation_sha256,
            "signing_payload_sha256": self.signing_payload_sha256,
            "trust_store_sha256": self.trust_store_sha256,
            "public_key_record_sha256": self.public_key_record_sha256,
            "public_key_sha256": self.public_key_sha256,
        }


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"nonfinite:{value}")


def _strict_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"nonfinite:{value}")
    return parsed


def _strict_json_bytes(body: bytes, *, reason: str) -> dict[str, object]:
    try:
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_nonfinite_json,
            parse_float=_strict_json_float,
        )
    except Exception as exc:
        raise GlobalGovernanceAttestationError(reason) from exc
    if not isinstance(payload, dict):
        raise GlobalGovernanceAttestationError(reason)
    return dict(payload)


def _canonical_json_bytes(payload: object) -> bytes:
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise GlobalGovernanceAttestationError(
            "global_governance_attestation_json_invalid"
        ) from exc


def _stable_id(value: object, *, reason: str) -> str:
    if not isinstance(value, str) or _STABLE_ID_RE.fullmatch(value) is None:
        raise GlobalGovernanceAttestationError(reason)
    return value


def _sha256(value: object, *, reason: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise GlobalGovernanceAttestationError(reason)
    return value


def _timestamp(value: object, *, reason: str) -> tuple[str, datetime]:
    if not isinstance(value, str) or _UTC_SECONDS_RE.fullmatch(value) is None:
        raise GlobalGovernanceAttestationError(reason)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GlobalGovernanceAttestationError(reason) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise GlobalGovernanceAttestationError(reason)
    return value, parsed.astimezone(timezone.utc)


def _normalized_source_digests(value: object) -> dict[str, str]:
    if (
        not isinstance(value, Mapping)
        or not value
        or len(value) > SOURCE_DIGEST_MAXIMUM
    ):
        raise GlobalGovernanceAttestationError(
            "global_governance_attestation_subject_invalid"
        )
    normalized: dict[str, str] = {}
    for name, digest in value.items():
        if not isinstance(name, str) or _SOURCE_NAME_RE.fullmatch(name) is None:
            raise GlobalGovernanceAttestationError(
                "global_governance_attestation_subject_invalid"
            )
        normalized[name] = _sha256(
            digest,
            reason="global_governance_attestation_subject_invalid",
        )
    return {name: normalized[name] for name in sorted(normalized)}


def _normalized_subject(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != _SUBJECT_FIELDS:
        raise GlobalGovernanceAttestationError(
            "global_governance_attestation_subject_invalid"
        )
    payload = dict(value)
    gate_id = _stable_id(
        payload.get("gate_id"),
        reason="global_governance_attestation_subject_invalid",
    )
    if gate_id not in ALLOWED_GATE_IDS:
        raise GlobalGovernanceAttestationError(
            "global_governance_attestation_subject_invalid"
        )
    receipt_contract = _stable_id(
        payload.get("receipt_contract"),
        reason="global_governance_attestation_subject_invalid",
    )
    release_commit_sha = payload.get("release_commit_sha")
    if not isinstance(release_commit_sha, str) or _GIT_SHA_RE.fullmatch(release_commit_sha) is None:
        raise GlobalGovernanceAttestationError(
            "global_governance_attestation_subject_invalid"
        )
    return {
        "gate_id": gate_id,
        "receipt_contract": receipt_contract,
        "release_commit_sha": release_commit_sha,
        "release_image_digest": _sha256(
            payload.get("release_image_digest"),
            reason="global_governance_attestation_subject_invalid",
        ),
        "source_digests": _normalized_source_digests(
            payload.get("source_digests")
        ),
        "payload_sha256": _sha256(
            payload.get("payload_sha256"),
            reason="global_governance_attestation_subject_invalid",
        ),
    }


def _normalized_unsigned_attestation(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != _UNSIGNED_ATTESTATION_FIELDS:
        raise GlobalGovernanceAttestationError(
            "global_governance_attestation_contract_invalid"
        )
    payload = dict(value)
    if (
        payload.get("contract_name") != ATTESTATION_CONTRACT
        or payload.get("algorithm") != ATTESTATION_ALGORITHM
    ):
        raise GlobalGovernanceAttestationError(
            "global_governance_attestation_contract_invalid"
        )
    issued_at, _issued = _timestamp(
        payload.get("issued_at"),
        reason="global_governance_attestation_timestamp_invalid",
    )
    expires_at, _expires = _timestamp(
        payload.get("expires_at"),
        reason="global_governance_attestation_timestamp_invalid",
    )
    return {
        "contract_name": ATTESTATION_CONTRACT,
        "algorithm": ATTESTATION_ALGORITHM,
        "key_id": _stable_id(
            payload.get("key_id"),
            reason="global_governance_attestation_identity_invalid",
        ),
        "authority_id": _stable_id(
            payload.get("authority_id"),
            reason="global_governance_attestation_identity_invalid",
        ),
        "issued_at": issued_at,
        "expires_at": expires_at,
        "subject": _normalized_subject(payload.get("subject")),
    }


def global_governance_attestation_signing_bytes(
    unsigned_attestation: Mapping[str, object],
) -> bytes:
    """Return the sole domain-separated byte sequence authorities must sign."""

    normalized = _normalized_unsigned_attestation(unsigned_attestation)
    return ATTESTATION_DOMAIN + _canonical_json_bytes(normalized)


def _strict_base64(value: object, *, length: int, reason: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise GlobalGovernanceAttestationError(reason)
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeError, ValueError) as exc:
        raise GlobalGovernanceAttestationError(reason) from exc
    if len(decoded) != length or base64.b64encode(decoded).decode("ascii") != value:
        raise GlobalGovernanceAttestationError(reason)
    return decoded


def _absolute_lexical_path(
    value: str | os.PathLike[str] | Path,
    *,
    reason: str,
) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts[1:]):
        raise GlobalGovernanceAttestationError(reason)
    return Path(os.path.abspath(os.fspath(path)))


def _path_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _trusted_owner_uids() -> frozenset[int]:
    """Return the immutable production trust-owner set.

    This deliberately has no argument, environment lookup, CLI surface, or
    public override.  Tests that cannot create root-owned fixtures monkeypatch
    this private function in-process; environment injection can never expand
    the production owner set.
    """

    return frozenset({0})


def _validate_directory(
    details: os.stat_result,
    *,
    final_parent: bool,
    allowed_owner_uids: frozenset[int],
    reason: str,
) -> None:
    mode = stat.S_IMODE(details.st_mode)
    if not stat.S_ISDIR(details.st_mode) or details.st_uid not in allowed_owner_uids:
        raise GlobalGovernanceAttestationError(reason)
    writable_by_untrusted = bool(mode & 0o022)
    root_sticky_ancestor = bool(mode & stat.S_ISVTX) and details.st_uid == 0
    if writable_by_untrusted and (final_parent or not root_sticky_ancestor):
        raise GlobalGovernanceAttestationError(reason)


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
        raise GlobalGovernanceAttestationError(reason)
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
        parents = parts[1:-1]
        for index, component in enumerate(parents):
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
            _validate_directory(
                os.fstat(directory_fd),
                final_parent=index == len(parents) - 1,
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
            raise GlobalGovernanceAttestationError(reason)
        chunks: list[bytes] = []
        remaining = int(before.st_size)
        while remaining:
            chunk = os.read(descriptor, min(remaining, 65_536))
            if not chunk:
                raise GlobalGovernanceAttestationError(reason)
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise GlobalGovernanceAttestationError(reason)
        after = os.fstat(descriptor)
        if _secure_file_identity(before) != _secure_file_identity(after):
            raise GlobalGovernanceAttestationError(reason)
        body = b"".join(chunks)
        return _SecureFileSnapshot(
            body=body,
            sha256=hashlib.sha256(body).hexdigest(),
            identity=_secure_file_identity(after),
        )
    except GlobalGovernanceAttestationError:
        raise
    except OSError as exc:
        raise GlobalGovernanceAttestationError(reason) from exc
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
) -> dict[str, _TrustedKey]:
    payload = _strict_json_bytes(
        snapshot.body,
        reason="global_governance_trust_store_invalid",
    )
    keys = payload.get("keys")
    if (
        set(payload) != _TRUST_STORE_FIELDS
        or payload.get("contract_name") != TRUST_STORE_CONTRACT
        or not isinstance(keys, list)
        or not keys
        or len(keys) > TRUST_STORE_MAX_KEYS
    ):
        raise GlobalGovernanceAttestationError(
            "global_governance_trust_store_invalid"
        )
    trust_root = trust_store_path.parent
    result: dict[str, _TrustedKey] = {}
    for raw_entry in keys:
        if not isinstance(raw_entry, Mapping) or set(raw_entry) != _TRUST_STORE_KEY_FIELDS:
            raise GlobalGovernanceAttestationError(
                "global_governance_trust_store_invalid"
            )
        entry = dict(raw_entry)
        if entry.get("algorithm") != ATTESTATION_ALGORITHM:
            raise GlobalGovernanceAttestationError(
                "global_governance_trust_store_invalid"
            )
        key_id = _stable_id(
            entry.get("key_id"),
            reason="global_governance_trust_store_invalid",
        )
        authority_id = _stable_id(
            entry.get("authority_id"),
            reason="global_governance_trust_store_invalid",
        )
        relpath = _canonical_key_relpath(entry.get("public_key_relpath"))
        record_sha256 = _sha256(
            entry.get("public_key_record_sha256"),
            reason="global_governance_trust_store_invalid",
        )
        _valid_from_text, valid_from = _timestamp(
            entry.get("valid_from"),
            reason="global_governance_trust_store_invalid",
        )
        _valid_until_text, valid_until = _timestamp(
            entry.get("valid_until"),
            reason="global_governance_trust_store_invalid",
        )
        revoked_value = entry.get("revoked_at")
        revoked_at = None
        if revoked_value is not None:
            _revoked_text, revoked_at = _timestamp(
                revoked_value,
                reason="global_governance_trust_store_invalid",
            )
        gate_ids_value = entry.get("allowed_gate_ids")
        if (
            not relpath
            or valid_until <= valid_from
            or key_id in result
            or not isinstance(gate_ids_value, list)
            or not gate_ids_value
            or any(not isinstance(gate_id, str) for gate_id in gate_ids_value)
            or len(gate_ids_value) != len(set(gate_ids_value))
            or not set(gate_ids_value).issubset(ALLOWED_GATE_IDS)
            or (
                revoked_at is not None
                and not valid_from <= revoked_at <= valid_until
            )
        ):
            raise GlobalGovernanceAttestationError(
                "global_governance_trust_store_invalid"
            )
        public_key_path = _absolute_lexical_path(
            trust_root / PurePosixPath(relpath),
            reason="global_governance_trust_store_invalid",
        )
        if (
            not _path_within(public_key_path, trust_root)
            or _path_within(public_key_path, _REPOSITORY_ROOT)
        ):
            raise GlobalGovernanceAttestationError(
                "global_governance_trust_store_invalid"
            )
        result[key_id] = _TrustedKey(
            key_id=key_id,
            authority_id=authority_id,
            public_key_path=public_key_path,
            public_key_record_sha256=record_sha256,
            valid_from=valid_from,
            valid_until=valid_until,
            revoked_at=revoked_at,
            allowed_gate_ids=frozenset(gate_ids_value),
        )
    return result


def _load_public_key(
    snapshot: _SecureFileSnapshot,
    *,
    expected: _TrustedKey,
) -> bytes:
    if f"sha256:{snapshot.sha256}" != expected.public_key_record_sha256:
        raise GlobalGovernanceAttestationError(
            "global_governance_public_key_invalid"
        )
    payload = _strict_json_bytes(
        snapshot.body,
        reason="global_governance_public_key_invalid",
    )
    if (
        set(payload) != _PUBLIC_KEY_FIELDS
        or payload.get("contract_name") != PUBLIC_KEY_CONTRACT
        or payload.get("algorithm") != ATTESTATION_ALGORITHM
        or payload.get("key_id") != expected.key_id
        or payload.get("authority_id") != expected.authority_id
    ):
        raise GlobalGovernanceAttestationError(
            "global_governance_public_key_invalid"
        )
    return _strict_base64(
        payload.get("public_key_base64"),
        length=32,
        reason="global_governance_public_key_invalid",
    )


def _attestation_object(value: object) -> tuple[dict[str, object], bytes]:
    if isinstance(value, bytes):
        if not value or len(value) > ATTESTATION_MAX_BYTES:
            raise GlobalGovernanceAttestationError(
                "global_governance_attestation_contract_invalid"
            )
        body = value
    elif isinstance(value, Mapping):
        body = _canonical_json_bytes(value)
        if not body or len(body) > ATTESTATION_MAX_BYTES:
            raise GlobalGovernanceAttestationError(
                "global_governance_attestation_contract_invalid"
            )
    else:
        raise GlobalGovernanceAttestationError(
            "global_governance_attestation_contract_invalid"
        )
    return (
        _strict_json_bytes(
            body,
            reason="global_governance_attestation_contract_invalid",
        ),
        body,
    )


def verify_global_governance_attestation(
    attestation: Mapping[str, object] | bytes,
    *,
    expected_subject: Mapping[str, object],
    observed_at: datetime | None = None,
) -> VerifiedGlobalGovernanceAttestation:
    """Verify one exact global-governance attestation against external trust."""

    now = observed_at or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise GlobalGovernanceAttestationError(
            "global_governance_attestation_clock_invalid"
        )
    now = now.astimezone(timezone.utc)
    owners = _trusted_owner_uids()
    configured_trust_store = str(os.getenv(TRUST_STORE_ENV) or "").strip()
    if not configured_trust_store:
        raise GlobalGovernanceAttestationError(
            "global_governance_trust_store_missing"
        )
    trust_path = _absolute_lexical_path(
        configured_trust_store,
        reason="global_governance_trust_store_invalid",
    )
    if _path_within(trust_path, _REPOSITORY_ROOT):
        raise GlobalGovernanceAttestationError(
            "global_governance_trust_store_repository_forbidden"
        )

    payload, attestation_body = _attestation_object(attestation)
    if set(payload) != _ATTESTATION_FIELDS:
        raise GlobalGovernanceAttestationError(
            "global_governance_attestation_contract_invalid"
        )
    signature = _strict_base64(
        payload.get("signature_base64"),
        length=64,
        reason="global_governance_attestation_signature_invalid",
    )
    unsigned = {
        name: value
        for name, value in payload.items()
        if name != "signature_base64"
    }
    normalized_unsigned = _normalized_unsigned_attestation(unsigned)
    normalized_expected_subject = _normalized_subject(expected_subject)
    if normalized_unsigned["subject"] != normalized_expected_subject:
        raise GlobalGovernanceAttestationError(
            "global_governance_attestation_subject_mismatch"
        )

    trust_snapshot = _read_secure_file(
        trust_path,
        maximum_bytes=TRUST_STORE_MAX_BYTES,
        allowed_owner_uids=owners,
        reason="global_governance_trust_store_invalid",
    )
    trusted_keys = _load_trust_store(
        trust_snapshot,
        trust_store_path=trust_path,
    )
    key_id = str(normalized_unsigned["key_id"])
    authority_id = str(normalized_unsigned["authority_id"])
    trusted_key = trusted_keys.get(key_id)
    if trusted_key is None:
        raise GlobalGovernanceAttestationError(
            "global_governance_attestation_key_unknown"
        )
    if trusted_key.authority_id != authority_id:
        raise GlobalGovernanceAttestationError(
            "global_governance_attestation_authority_unknown"
        )

    issued_at_text, issued_at = _timestamp(
        normalized_unsigned["issued_at"],
        reason="global_governance_attestation_timestamp_invalid",
    )
    expires_at_text, expires_at = _timestamp(
        normalized_unsigned["expires_at"],
        reason="global_governance_attestation_timestamp_invalid",
    )
    if (
        issued_at > now
        or expires_at <= now
        or expires_at <= issued_at
        or expires_at - issued_at
        > timedelta(seconds=DEFAULT_MAXIMUM_TTL_SECONDS)
    ):
        raise GlobalGovernanceAttestationError(
            "global_governance_attestation_expired_or_invalid"
        )
    if not (
        trusted_key.valid_from
        <= issued_at
        < expires_at
        <= trusted_key.valid_until
    ):
        raise GlobalGovernanceAttestationError(
            "global_governance_attestation_authority_window_invalid"
        )
    if trusted_key.revoked_at is not None:
        if trusted_key.revoked_at <= now:
            raise GlobalGovernanceAttestationError(
                "global_governance_attestation_key_revoked"
            )
        if expires_at > trusted_key.revoked_at:
            raise GlobalGovernanceAttestationError(
                "global_governance_attestation_authority_window_invalid"
            )
    gate_id = str(normalized_expected_subject["gate_id"])
    if gate_id not in trusted_key.allowed_gate_ids:
        raise GlobalGovernanceAttestationError(
            "global_governance_attestation_gate_unauthorized"
        )

    public_key_snapshot = _read_secure_file(
        trusted_key.public_key_path,
        maximum_bytes=PUBLIC_KEY_RECORD_MAX_BYTES,
        allowed_owner_uids=owners,
        reason="global_governance_public_key_invalid",
    )
    public_key_bytes = _load_public_key(
        public_key_snapshot,
        expected=trusted_key,
    )
    signing_bytes = global_governance_attestation_signing_bytes(
        normalized_unsigned
    )
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
    except (ImportError, ModuleNotFoundError) as exc:
        raise GlobalGovernanceAttestationError(
            "global_governance_attestation_crypto_unavailable"
        ) from exc
    try:
        Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(
            signature,
            signing_bytes,
        )
    except InvalidSignature as exc:
        raise GlobalGovernanceAttestationError(
            "global_governance_attestation_signature_invalid"
        ) from exc
    except ValueError as exc:
        raise GlobalGovernanceAttestationError(
            "global_governance_public_key_invalid"
        ) from exc

    subject_bytes = _canonical_json_bytes(normalized_expected_subject)
    return VerifiedGlobalGovernanceAttestation(
        contract_name=ATTESTATION_CONTRACT,
        algorithm=ATTESTATION_ALGORITHM,
        gate_id=gate_id,
        receipt_contract=str(normalized_expected_subject["receipt_contract"]),
        key_id=key_id,
        authority_id=authority_id,
        issued_at=issued_at_text,
        expires_at=expires_at_text,
        payload_sha256=str(normalized_expected_subject["payload_sha256"]),
        subject_sha256=hashlib.sha256(subject_bytes).hexdigest(),
        attestation_sha256=hashlib.sha256(attestation_body).hexdigest(),
        signing_payload_sha256=hashlib.sha256(signing_bytes).hexdigest(),
        trust_store_sha256=trust_snapshot.sha256,
        public_key_record_sha256=public_key_snapshot.sha256,
        public_key_sha256=hashlib.sha256(public_key_bytes).hexdigest(),
    )
