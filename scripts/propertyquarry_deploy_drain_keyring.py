#!/usr/bin/env python3
"""Validate the externally controlled drain signing keyring and rotations."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "propertyquarry.deploy-drain-keyring.v2"
AUTHORITY = "propertyquarry-release-control"
TRACKED_KEYRING_PATH = (
    Path(__file__).resolve().parents[1]
    / "config"
    / "release"
    / "propertyquarry_deploy_drain_keyring.v2.json"
)
EXTERNAL_KEYRING_PATH = Path(
    "/etc/propertyquarry/release-control/deploy-drain-keyring.v2.json"
)
KEYRING_STATUS = "UNCONFIGURED"
KEYRING_MANIFEST_SHA256 = "f4e8ff3ddc2a027832e49901b86ea9047bdf5da2c4a240cd09e2ce6c22a39f1f"
KEYRING_ROTATION_EPOCH = 0
KEYRING_MINIMUM_ACCEPTED_EPOCH = 0
REQUIRED_UID = 0
REQUIRED_MODE = 0o444
SECURE_PATH_ROOT = Path("/")
KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class DrainKeyringError(ValueError):
    pass


@dataclass(frozen=True)
class TrustedDrainKey:
    key_id: str
    epoch: int
    public_key: bytes
    public_key_sha256: str


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DrainKeyringError(f"drain keyring contains duplicate key {key}")
        result[key] = value
    return result


def _load_json(path: Path, label: str) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DrainKeyringError(f"{label} is unavailable: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise DrainKeyringError(f"{label} must be a regular file")
        raw = os.read(descriptor, 1024 * 1024 + 1)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if len(raw) > 1024 * 1024 or (before.st_dev, before.st_ino, before.st_size) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
    ):
        raise DrainKeyringError(f"{label} changed while read")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                DrainKeyringError(f"{label} contains non-finite constant {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DrainKeyringError(f"{label} is not strict JSON") from exc
    if not isinstance(payload, dict):
        raise DrainKeyringError(f"{label} must be an object")
    return payload


def _secure_external_file(path: Path) -> None:
    try:
        relative = path.relative_to(SECURE_PATH_ROOT)
    except ValueError as exc:
        raise DrainKeyringError("external keyring escapes fixed secure root") from exc
    current = SECURE_PATH_ROOT
    for part in relative.parts[:-1]:
        current /= part
        metadata = current.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != REQUIRED_UID
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise DrainKeyringError("external keyring parent ownership or mode is unsafe")
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != REQUIRED_UID
        or stat.S_IMODE(metadata.st_mode) != REQUIRED_MODE
        or metadata.st_nlink != 1
    ):
        raise DrainKeyringError(
            "external keyring must be single-link, root-owned, non-symlink mode 0444"
        )


def _timestamp(value: object, field: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.endswith("Z"):
        raise DrainKeyringError(f"{field} must be null or a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise DrainKeyringError(f"{field} is invalid") from exc
    return parsed.astimezone(timezone.utc)


def _decode_key(value: object, field: str) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise DrainKeyringError(f"{field} must be unpadded base64url")
    try:
        decoded = base64.b64decode(
            value + "=" * ((4 - len(value) % 4) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise DrainKeyringError(f"{field} is invalid base64url") from exc
    if len(decoded) != 32:
        raise DrainKeyringError(f"{field} must decode to 32 bytes")
    return decoded


def validate_keyring(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    expected = {
        "schema",
        "authority",
        "algorithm",
        "status",
        "rotation_epoch",
        "minimum_accepted_epoch",
        "keys",
    }
    if set(payload) != expected:
        raise DrainKeyringError("drain keyring fields do not match v2")
    if (
        payload["schema"] != SCHEMA
        or payload["authority"] != AUTHORITY
        or payload["algorithm"] != "Ed25519"
        or payload["status"] != "active"
        or isinstance(payload["rotation_epoch"], bool)
        or not isinstance(payload["rotation_epoch"], int)
        or isinstance(payload["minimum_accepted_epoch"], bool)
        or not isinstance(payload["minimum_accepted_epoch"], int)
        or payload["minimum_accepted_epoch"] < 1
        or payload["minimum_accepted_epoch"] > payload["rotation_epoch"]
    ):
        raise DrainKeyringError("drain keyring identity or epoch policy is invalid")
    keys = payload["keys"]
    if not isinstance(keys, list) or not keys:
        raise DrainKeyringError("active drain keyring must contain keys")
    epochs: list[int] = []
    ids: set[str] = set()
    for index, item in enumerate(keys):
        if not isinstance(item, dict) or set(item) != {
            "key_id",
            "epoch",
            "public_key",
            "public_key_sha256",
            "activates_at",
            "accept_until",
            "revoked_at",
        }:
            raise DrainKeyringError(f"drain keyring key {index} fields are invalid")
        key_id = str(item["key_id"] or "")
        epoch = item["epoch"]
        if (
            not KEY_ID_RE.fullmatch(key_id)
            or key_id in ids
            or isinstance(epoch, bool)
            or not isinstance(epoch, int)
            or epoch < 1
        ):
            raise DrainKeyringError("drain keyring key identity or epoch is invalid")
        ids.add(key_id)
        epochs.append(epoch)
        key_bytes = _decode_key(item["public_key"], f"keys[{index}].public_key")
        key_hash = str(item["public_key_sha256"] or "")
        if not SHA256_RE.fullmatch(key_hash) or hashlib.sha256(key_bytes).hexdigest() != key_hash:
            raise DrainKeyringError("drain keyring public-key hash is invalid")
        activates = _timestamp(item["activates_at"], f"keys[{index}].activates_at")
        accept_until = _timestamp(item["accept_until"], f"keys[{index}].accept_until")
        revoked_at = _timestamp(item["revoked_at"], f"keys[{index}].revoked_at")
        if activates is None or (accept_until is not None and accept_until <= activates):
            raise DrainKeyringError("drain key activation/acceptance window is invalid")
        if revoked_at is not None and revoked_at < activates:
            raise DrainKeyringError("drain key revocation predates activation")
    if epochs != sorted(set(epochs)) or max(epochs) != payload["rotation_epoch"]:
        raise DrainKeyringError("drain key epochs must be unique, ordered and reach rotation_epoch")
    return keys


def load_keyring() -> dict[str, Any]:
    tracked = _load_json(TRACKED_KEYRING_PATH, "tracked drain keyring")
    if hashlib.sha256(_canonical_bytes(tracked)).hexdigest() != KEYRING_MANIFEST_SHA256:
        raise DrainKeyringError("tracked drain keyring does not match compiled manifest pin")
    if KEYRING_STATUS != "active" or tracked.get("status") != "active":
        raise DrainKeyringError("drain keyring is UNCONFIGURED")
    validate_keyring(tracked)
    if (
        tracked["rotation_epoch"] != KEYRING_ROTATION_EPOCH
        or tracked["minimum_accepted_epoch"] != KEYRING_MINIMUM_ACCEPTED_EPOCH
    ):
        raise DrainKeyringError("drain keyring epoch rollback or unreviewed rotation detected")
    _secure_external_file(EXTERNAL_KEYRING_PATH)
    external = _load_json(EXTERNAL_KEYRING_PATH, "external drain keyring")
    if _canonical_bytes(external) != _canonical_bytes(tracked):
        raise DrainKeyringError("external drain keyring does not match reviewed metadata")
    return dict(external)


def select_trusted_key(key_id: str, *, at: datetime) -> TrustedDrainKey:
    payload = load_keyring()
    checked_at = at.astimezone(timezone.utc)
    keys = validate_keyring(payload)
    matches = [item for item in keys if item["key_id"] == key_id]
    if len(matches) != 1:
        raise DrainKeyringError("drain signing key is not in the reviewed keyring")
    item = matches[0]
    epoch = int(item["epoch"])
    if epoch < int(payload["minimum_accepted_epoch"]):
        raise DrainKeyringError("drain signing key epoch is below the accepted floor")
    activates = _timestamp(item["activates_at"], "key.activates_at")
    accept_until = _timestamp(item["accept_until"], "key.accept_until")
    revoked_at = _timestamp(item["revoked_at"], "key.revoked_at")
    assert activates is not None
    if checked_at < activates:
        raise DrainKeyringError("drain signing key is not active yet")
    if revoked_at is not None and checked_at >= revoked_at:
        raise DrainKeyringError("drain signing key is revoked")
    if accept_until is not None and checked_at >= accept_until:
        raise DrainKeyringError("drain signing key overlap window has ended")
    public_key = _decode_key(item["public_key"], "key.public_key")
    return TrustedDrainKey(
        key_id=key_id,
        epoch=epoch,
        public_key=public_key,
        public_key_sha256=str(item["public_key_sha256"]),
    )
