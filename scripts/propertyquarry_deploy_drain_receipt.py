#!/usr/bin/env python3
"""Reference verifier/cache contract for signed deploy drain receipts.

This module deliberately has no issue/sign command. Production CLI operations
delegate to the independently installed controller, which owns the external
keyring, database-bound challenge, monotonic single-use CAS and migration
result chain. The Python functions remain deterministic reference helpers for
contract tests and cannot authorize the production wrapper.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

if __package__:
    from scripts import propertyquarry_deploy_controller_guard as controller_guard
else:
    import propertyquarry_deploy_controller_guard as controller_guard


SCHEMA = "propertyquarry.deploy-drain-receipt.v2"
ISSUER = "propertyquarry-release-control"
ACTION = "drain_and_promote"
VERIFY_SCHEMA = "propertyquarry.deploy-drain-verification.v2"
CONSUMPTION_SCHEMA = "propertyquarry.deploy-drain-consumption.v2"
LEDGER_SCHEMA = "propertyquarry.deploy-drain-consumption-ledger.v2"
PRODUCER = "propertyquarry-deploy-drain-receipt-verifier"
MAX_TTL_SECONDS = 300
MAX_FUTURE_SKEW_SECONDS = 30
TRUST_ANCHOR_SCHEMA = "propertyquarry.deploy-drain-trust-anchor.v1"
TRACKED_TRUST_ANCHOR_PATH = (
    Path(__file__).resolve().parents[1]
    / "config"
    / "release"
    / "propertyquarry_deploy_drain_trust_anchor.v1.json"
)
EXTERNAL_TRUST_ANCHOR_PATH = Path(
    "/etc/propertyquarry/release-control/deploy-drain-trust-anchor.v1.json"
)
# Production is intentionally blocked until release control provisions the
# external root and performs an explicit reviewed rotation of these metadata
# pins. The tracked manifest is expected metadata only and can never authorize
# without an identical, root-owned, immutable external trust-store entry.
TRUST_ANCHOR_STATUS = "UNCONFIGURED"
TRUST_ANCHOR_ROTATION_EPOCH = 0
TRUST_ANCHOR_KEY_ID = "UNCONFIGURED"
TRUST_ANCHOR_PUBLIC_KEY_SHA256 = "UNCONFIGURED"
TRUST_ANCHOR_MANIFEST_SHA256 = (
    "dfb8841d9c3ea30f8ca2dd6e4984a6dc79109376677372cf86b31232c11f2472"
)
TRUST_ANCHOR_REQUIRED_UID = 0
TRUST_ANCHOR_REQUIRED_MODE = 0o444
CONSUMPTION_LEDGER_PATH = Path(
    "/var/lib/propertyquarry/release-control/deploy-drain-consumption-ledger.v1.json"
)
LEGACY_CALLER_TRUST_ENV = (
    "PROPERTYQUARRY_DRAIN_RECEIPT_KEY_ID",
    "PROPERTYQUARRY_DRAIN_RECEIPT_ED25519_PUBLIC_KEY",
)
FORBIDDEN_SIGNER_ENV = (
    "PROPERTYQUARRY_DRAIN_RECEIPT_ED25519_PRIVATE_KEY",
    "PROPERTYQUARRY_DRAIN_RECEIPT_SIGNING_KEY",
    "PROPERTYQUARRY_DRAIN_RECEIPT_SIGNING_SEED",
)

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")
CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class DrainReceiptError(ValueError):
    """Raised when a drain receipt or its consumption ledger is invalid."""


@dataclass(frozen=True)
class DrainReceiptBindings:
    release_sha: str
    image_digest: str
    deployment_id: str
    target_id: str
    compose_project: str
    public_origin: str
    api_container: str
    worker_container: str
    scheduler_container: str
    render_container: str
    ingress_container: str
    writer_topology_sha256: str
    actor_id: str

    def target(self) -> dict[str, str]:
        return {
            "id": self.target_id,
            "compose_project": self.compose_project,
            "public_origin": self.public_origin,
            "api_container": self.api_container,
            "worker_container": self.worker_container,
            "scheduler_container": self.scheduler_container,
            "render_container": self.render_container,
            "ingress_container": self.ingress_container,
            "writer_topology_sha256": self.writer_topology_sha256,
        }


def _canonical_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise DrainReceiptError(f"value is not canonical JSON: {exc}") from exc
    return encoded.encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unexpected:
            details.append("unexpected " + ", ".join(unexpected))
        raise DrainReceiptError(f"{label} fields are invalid ({'; '.join(details)})")


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise DrainReceiptError(f"{label} must be a non-empty string")
    return value


def _require_pattern(value: Any, pattern: re.Pattern[str], label: str) -> str:
    text = _require_string(value, label)
    if not pattern.fullmatch(text):
        raise DrainReceiptError(f"{label} has an invalid format")
    return text


def _parse_utc_timestamp(value: Any, label: str) -> datetime:
    text = _require_string(value, label)
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z", text):
        raise DrainReceiptError(f"{label} must be an RFC3339 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise DrainReceiptError(f"{label} is not a valid RFC3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise DrainReceiptError(f"{label} must be UTC")
    if not math.isfinite(parsed.timestamp()):
        raise DrainReceiptError(f"{label} is outside the supported range")
    return parsed.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _decode_base64url(value: str, *, expected_bytes: int, label: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise DrainReceiptError(f"{label} must be unpadded base64url")
    padding = "=" * ((4 - len(value) % 4) % 4)
    try:
        decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise DrainReceiptError(f"{label} is not valid base64url") from exc
    if len(decoded) != expected_bytes:
        raise DrainReceiptError(f"{label} must decode to exactly {expected_bytes} bytes")
    if base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != value:
        raise DrainReceiptError(f"{label} is not canonical base64url")
    return decoded


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise DrainReceiptError(f"could not read {label} {path}: {exc}") from exc
    try:
        value = json.loads(
            raw,
            object_pairs_hook=lambda pairs: _unique_json_object(pairs, label),
            parse_constant=lambda value: _reject_json_constant(value, label),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DrainReceiptError(f"{label} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise DrainReceiptError(f"{label} must be a JSON object")
    return value


def _unique_json_object(pairs: list[tuple[str, Any]], label: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DrainReceiptError(f"{label} contains duplicate JSON key {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str, label: str) -> Any:
    raise DrainReceiptError(f"{label} contains forbidden non-finite JSON constant {value}")


def _validate_public_origin(value: Any, label: str) -> str:
    origin = _require_string(value, label)
    if len(origin) > 2048 or origin.endswith("/"):
        raise DrainReceiptError(f"{label} must be a canonical HTTPS origin without a trailing slash")
    try:
        parsed = urllib.parse.urlsplit(origin)
        port = parsed.port
    except ValueError as exc:
        raise DrainReceiptError(f"{label} is not a valid HTTPS origin") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise DrainReceiptError(f"{label} must be a canonical HTTPS origin without credentials, path, query, or fragment")
    return origin


def _atomic_write_json(path: Path, value: Mapping[str, Any], *, overwrite: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not overwrite and path.exists():
        raise DrainReceiptError(f"refusing to overwrite existing output: {path}")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(_canonical_bytes(value) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _trusted_key_from_release_control() -> tuple[str, Ed25519PublicKey, dict[str, Any]]:
    caller_selected = [name for name in LEGACY_CALLER_TRUST_ENV if os.environ.get(name)]
    if caller_selected:
        raise DrainReceiptError(
            "deploy caller trust-anchor overrides are forbidden: " + ", ".join(caller_selected)
        )
    signer_material = [name for name in FORBIDDEN_SIGNER_ENV if os.environ.get(name)]
    if signer_material:
        raise DrainReceiptError(
            "private drain signer material is forbidden in deploy runtime: "
            + ", ".join(signer_material)
        )
    try:
        tracked_stat = TRACKED_TRUST_ANCHOR_PATH.lstat()
    except OSError as exc:
        raise DrainReceiptError(f"tracked drain trust metadata is unavailable: {exc}") from exc
    if stat.S_ISLNK(tracked_stat.st_mode) or not stat.S_ISREG(tracked_stat.st_mode):
        raise DrainReceiptError("tracked drain trust metadata must be a regular non-symlink file")
    anchor = _load_json_object(TRACKED_TRUST_ANCHOR_PATH, "tracked drain trust metadata")
    _require_exact_keys(
        anchor,
        {
            "schema",
            "authority",
            "algorithm",
            "rotation_epoch",
            "key_id",
            "public_key",
            "public_key_sha256",
            "status",
        },
        "tracked drain trust metadata",
    )
    canonical_hash = _sha256_bytes(_canonical_bytes(anchor))
    if canonical_hash != TRUST_ANCHOR_MANIFEST_SHA256:
        raise DrainReceiptError(
            "release-controlled drain trust anchor does not match the compiled manifest pin"
        )
    if TRUST_ANCHOR_STATUS != "active" or anchor["status"] != "active":
        raise DrainReceiptError(
            "release-control drain trust anchor is UNCONFIGURED; production promotion is blocked"
        )
    if (
        anchor["schema"] != TRUST_ANCHOR_SCHEMA
        or anchor["authority"] != ISSUER
        or anchor["algorithm"] != "Ed25519"
        or isinstance(anchor["rotation_epoch"], bool)
        or anchor["rotation_epoch"] != TRUST_ANCHOR_ROTATION_EPOCH
        or anchor["key_id"] != TRUST_ANCHOR_KEY_ID
        or anchor["public_key_sha256"] != TRUST_ANCHOR_PUBLIC_KEY_SHA256
    ):
        raise DrainReceiptError("release-controlled drain trust anchor identity is invalid")

    try:
        external_parent_stat = EXTERNAL_TRUST_ANCHOR_PATH.parent.lstat()
        external_stat = EXTERNAL_TRUST_ANCHOR_PATH.lstat()
    except OSError as exc:
        raise DrainReceiptError(
            f"external release-control drain trust anchor is unavailable: {exc}"
        ) from exc
    if (
        stat.S_ISLNK(external_parent_stat.st_mode)
        or not stat.S_ISDIR(external_parent_stat.st_mode)
        or external_parent_stat.st_uid != TRUST_ANCHOR_REQUIRED_UID
        or stat.S_IMODE(external_parent_stat.st_mode) & 0o022
    ):
        raise DrainReceiptError(
            "external release-control trust-store directory ownership or mode is unsafe"
        )
    if (
        stat.S_ISLNK(external_stat.st_mode)
        or not stat.S_ISREG(external_stat.st_mode)
        or external_stat.st_uid != TRUST_ANCHOR_REQUIRED_UID
        or stat.S_IMODE(external_stat.st_mode) != TRUST_ANCHOR_REQUIRED_MODE
        or external_stat.st_nlink != 1
    ):
        raise DrainReceiptError(
            "external release-control drain trust anchor must be single-link, non-symlink, "
            "root-owned, and immutable mode 0444"
        )
    external_anchor = _load_json_object(
        EXTERNAL_TRUST_ANCHOR_PATH,
        "external release-control drain trust anchor",
    )
    if _canonical_bytes(external_anchor) != _canonical_bytes(anchor):
        raise DrainReceiptError(
            "external release-control drain trust anchor does not match reviewed metadata"
        )
    key_id = _require_pattern(
        external_anchor["key_id"], KEY_ID_RE, "drain trust anchor key_id"
    )
    key_text = _require_string(
        external_anchor["public_key"], "drain trust anchor public_key"
    )
    key_bytes = _decode_base64url(
        key_text,
        expected_bytes=32,
        label="drain trust anchor public_key",
    )
    if _sha256_bytes(key_bytes) != TRUST_ANCHOR_PUBLIC_KEY_SHA256:
        raise DrainReceiptError("release-controlled drain trust anchor public-key pin is invalid")
    try:
        public_key = Ed25519PublicKey.from_public_bytes(key_bytes)
    except ValueError as exc:
        raise DrainReceiptError("trusted Ed25519 public key is invalid") from exc
    return key_id, public_key, {
        "schema": TRUST_ANCHOR_SCHEMA,
        "authority": ISSUER,
        "algorithm": "Ed25519",
        "rotation_epoch": TRUST_ANCHOR_ROTATION_EPOCH,
        "key_id": key_id,
        "public_key_sha256": TRUST_ANCHOR_PUBLIC_KEY_SHA256,
        "manifest_sha256": TRUST_ANCHOR_MANIFEST_SHA256,
    }


def _validate_bindings(bindings: DrainReceiptBindings) -> None:
    _require_pattern(bindings.release_sha, SHA_RE, "expected release SHA")
    _require_pattern(bindings.image_digest, DIGEST_RE, "expected image digest")
    for label, value in (
        ("expected deployment id", bindings.deployment_id),
        ("expected target id", bindings.target_id),
        ("expected Compose project", bindings.compose_project),
        ("expected actor id", bindings.actor_id),
    ):
        _require_pattern(value, IDENTIFIER_RE, label)
    for label, value in (
        ("expected API container", bindings.api_container),
        ("expected worker container", bindings.worker_container),
        ("expected scheduler container", bindings.scheduler_container),
        ("expected render container", bindings.render_container),
        ("expected ingress container", bindings.ingress_container),
    ):
        _require_pattern(value, CONTAINER_RE, label)
    _validate_public_origin(bindings.public_origin, "expected public origin")
    _require_pattern(
        bindings.writer_topology_sha256,
        re.compile(r"^[0-9a-f]{64}$"),
        "expected writer topology SHA-256",
    )


def verify_drain_receipt(
    receipt_path: str | Path,
    bindings: DrainReceiptBindings,
    *,
    now: datetime | None = None,
    max_age_seconds: int = MAX_TTL_SECONDS,
) -> dict[str, Any]:
    """Verify a signed receipt against exact deploy bindings."""

    if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, int):
        raise DrainReceiptError("max age must be an integer")
    if max_age_seconds < 1 or max_age_seconds > MAX_TTL_SECONDS:
        raise DrainReceiptError(f"max age must be between 1 and {MAX_TTL_SECONDS} seconds")
    _validate_bindings(bindings)
    trusted_key_id, public_key, trust_anchor = _trusted_key_from_release_control()
    receipt = _load_json_object(Path(receipt_path), "drain receipt")
    _require_exact_keys(
        receipt,
        {
            "schema",
            "issuer",
            "key_id",
            "release",
            "deployment_id",
            "target",
            "actor_id",
            "action",
            "issued_at",
            "expires_at",
            "nonce",
            "signature",
        },
        "drain receipt",
    )
    if receipt["schema"] != SCHEMA:
        raise DrainReceiptError(f"drain receipt schema must be {SCHEMA}")
    if receipt["issuer"] != ISSUER:
        raise DrainReceiptError(f"drain receipt issuer must be {ISSUER}")
    if receipt["action"] != ACTION:
        raise DrainReceiptError(f"drain receipt action must be {ACTION}")
    if receipt["key_id"] != trusted_key_id:
        raise DrainReceiptError("drain receipt key_id is not the configured trusted key")

    release = receipt["release"]
    if not isinstance(release, dict):
        raise DrainReceiptError("drain receipt release must be an object")
    _require_exact_keys(release, {"commit_sha", "image_digest"}, "drain receipt release")
    _require_pattern(release["commit_sha"], SHA_RE, "drain receipt release.commit_sha")
    _require_pattern(release["image_digest"], DIGEST_RE, "drain receipt release.image_digest")

    target = receipt["target"]
    if not isinstance(target, dict):
        raise DrainReceiptError("drain receipt target must be an object")
    _require_exact_keys(
        target,
        {
            "id",
            "compose_project",
            "public_origin",
            "api_container",
            "worker_container",
            "scheduler_container",
            "render_container",
            "ingress_container",
            "writer_topology_sha256",
        },
        "drain receipt target",
    )
    _require_pattern(receipt["deployment_id"], IDENTIFIER_RE, "drain receipt deployment_id")
    _require_pattern(receipt["actor_id"], IDENTIFIER_RE, "drain receipt actor_id")
    _require_pattern(target["id"], IDENTIFIER_RE, "drain receipt target.id")
    _require_pattern(target["compose_project"], IDENTIFIER_RE, "drain receipt target.compose_project")
    for key in (
        "api_container",
        "worker_container",
        "scheduler_container",
        "render_container",
        "ingress_container",
    ):
        _require_pattern(target[key], CONTAINER_RE, f"drain receipt target.{key}")
    _require_pattern(
        target["writer_topology_sha256"],
        re.compile(r"^[0-9a-f]{64}$"),
        "drain receipt target.writer_topology_sha256",
    )
    _validate_public_origin(target["public_origin"], "drain receipt target.public_origin")
    _require_pattern(receipt["nonce"], NONCE_RE, "drain receipt nonce")

    expected_release = {
        "commit_sha": bindings.release_sha,
        "image_digest": bindings.image_digest,
    }
    if release != expected_release:
        raise DrainReceiptError("drain receipt release binding does not match the candidate")
    if receipt["deployment_id"] != bindings.deployment_id:
        raise DrainReceiptError("drain receipt deployment_id does not match the candidate")
    if target != bindings.target():
        raise DrainReceiptError("drain receipt target binding does not match the candidate")
    if receipt["actor_id"] != bindings.actor_id:
        raise DrainReceiptError("drain receipt actor_id does not match the deploy actor")

    issued_at = _parse_utc_timestamp(receipt["issued_at"], "drain receipt issued_at")
    expires_at = _parse_utc_timestamp(receipt["expires_at"], "drain receipt expires_at")
    if expires_at <= issued_at:
        raise DrainReceiptError("drain receipt expires_at must be after issued_at")
    if (expires_at - issued_at).total_seconds() > MAX_TTL_SECONDS:
        raise DrainReceiptError(f"drain receipt lifetime exceeds {MAX_TTL_SECONDS} seconds")
    checked_at = (now or _utc_now()).astimezone(timezone.utc)
    if issued_at > checked_at + timedelta(seconds=MAX_FUTURE_SKEW_SECONDS):
        raise DrainReceiptError("drain receipt issued_at is too far in the future")
    if checked_at >= expires_at:
        raise DrainReceiptError("drain receipt has expired")
    if checked_at - issued_at > timedelta(seconds=max_age_seconds):
        raise DrainReceiptError("drain receipt exceeds the configured maximum age")

    signature_text = _require_string(receipt["signature"], "drain receipt signature")
    signature = _decode_base64url(signature_text, expected_bytes=64, label="drain receipt signature")
    signed_payload = dict(receipt)
    del signed_payload["signature"]
    payload_bytes = _canonical_bytes(signed_payload)
    try:
        public_key.verify(signature, payload_bytes)
    except InvalidSignature as exc:
        raise DrainReceiptError("drain receipt signature is invalid") from exc

    return {
        "schema": VERIFY_SCHEMA,
        "producer": PRODUCER,
        "verified": True,
        "verified_at": _format_timestamp(checked_at),
        "receipt_schema": SCHEMA,
        "issuer": ISSUER,
        "key_id": trusted_key_id,
        "trust_anchor": trust_anchor,
        "action": ACTION,
        "release": expected_release,
        "deployment_id": bindings.deployment_id,
        "target": bindings.target(),
        "actor_id": bindings.actor_id,
        "issued_at": receipt["issued_at"],
        "expires_at": receipt["expires_at"],
        "nonce_sha256": _sha256_bytes(receipt["nonce"].encode("ascii")),
        "signed_payload_sha256": _sha256_bytes(payload_bytes),
    }


def _empty_ledger() -> dict[str, Any]:
    return {"schema": LEDGER_SCHEMA, "producer": PRODUCER, "entries": []}


def _load_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise DrainReceiptError(
            "externally provisioned drain consumption ledger is unavailable; refusing implicit reset"
        )
    ledger = _load_json_object(path, "drain consumption ledger")
    _require_exact_keys(ledger, {"schema", "producer", "entries"}, "drain consumption ledger")
    if ledger["schema"] != LEDGER_SCHEMA or ledger["producer"] != PRODUCER:
        raise DrainReceiptError("drain consumption ledger identity is invalid")
    entries = ledger["entries"]
    if not isinstance(entries, list):
        raise DrainReceiptError("drain consumption ledger entries must be an array")
    seen_nonces: set[str] = set()
    seen_payloads: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise DrainReceiptError(f"drain consumption ledger entry {index} must be an object")
        _require_exact_keys(
            entry,
            {
                "nonce_sha256",
                "signed_payload_sha256",
                "release_commit_sha",
                "release_image_digest",
                "deployment_id",
                "target_id",
                "actor_id",
                "consumed_at",
            },
            f"drain consumption ledger entry {index}",
        )
        nonce_hash = _require_pattern(entry["nonce_sha256"], re.compile(r"^[0-9a-f]{64}$"), "ledger nonce hash")
        payload_hash = _require_pattern(
            entry["signed_payload_sha256"], re.compile(r"^[0-9a-f]{64}$"), "ledger payload hash"
        )
        if nonce_hash in seen_nonces or payload_hash in seen_payloads:
            raise DrainReceiptError("drain consumption ledger contains duplicate entries")
        seen_nonces.add(nonce_hash)
        seen_payloads.add(payload_hash)
        _parse_utc_timestamp(entry["consumed_at"], "ledger consumed_at")
    return ledger


def consume_drain_receipt(
    receipt_path: str | Path,
    bindings: DrainReceiptBindings,
    *,
    now: datetime | None = None,
    max_age_seconds: int = MAX_TTL_SECONDS,
) -> dict[str, Any]:
    """Revalidate and consume a receipt exactly once while holding a file lock."""

    ledger = CONSUMPTION_LEDGER_PATH
    ledger.parent.mkdir(parents=True, exist_ok=True)
    lock_path = ledger.with_name(ledger.name + ".lock")
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(lock_fd, 0o600)
        with os.fdopen(lock_fd, "a+b", closefd=False) as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            # Verification intentionally happens under the same lock as the
            # ledger mutation so expiry and every binding are checked at the
            # final authorization boundary.
            verification = verify_drain_receipt(
                receipt_path,
                bindings,
                now=now,
                max_age_seconds=max_age_seconds,
            )
            current = _load_ledger(ledger)
            nonce_hash = verification["nonce_sha256"]
            payload_hash = verification["signed_payload_sha256"]
            if any(
                entry["nonce_sha256"] == nonce_hash or entry["signed_payload_sha256"] == payload_hash
                for entry in current["entries"]
            ):
                raise DrainReceiptError("drain receipt has already been consumed")
            consumed_at = _format_timestamp((now or _utc_now()).astimezone(timezone.utc))
            entry = {
                "nonce_sha256": nonce_hash,
                "signed_payload_sha256": payload_hash,
                "release_commit_sha": bindings.release_sha,
                "release_image_digest": bindings.image_digest,
                "deployment_id": bindings.deployment_id,
                "target_id": bindings.target_id,
                "actor_id": bindings.actor_id,
                "consumed_at": consumed_at,
            }
            current["entries"].append(entry)
            _atomic_write_json(ledger, current)
            return {
                "schema": CONSUMPTION_SCHEMA,
                "producer": PRODUCER,
                "verified": True,
                "single_use_consumed": True,
                "consumed_at": consumed_at,
                "receipt": verification,
                "ledger": {
                    "path": str(ledger),
                    "entry_count": len(current["entries"]),
                    "entry_sha256": _sha256_bytes(_canonical_bytes(entry)),
                },
            }
    finally:
        os.close(lock_fd)


def _add_binding_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--target-id", required=True)
    parser.add_argument("--compose-project", required=True)
    parser.add_argument("--public-origin", required=True)
    parser.add_argument("--api-container", required=True)
    parser.add_argument("--worker-container", required=True)
    parser.add_argument("--scheduler-container", required=True)
    parser.add_argument("--render-container", required=True)
    parser.add_argument("--ingress-container", required=True)
    parser.add_argument("--writer-topology-sha256", required=True)
    parser.add_argument("--actor-id", required=True)
    parser.add_argument("--max-age-seconds", type=int, default=MAX_TTL_SECONDS)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--overwrite", action="store_true")


def _bindings_from_args(args: argparse.Namespace) -> DrainReceiptBindings:
    return DrainReceiptBindings(
        release_sha=args.release_sha,
        image_digest=args.image_digest,
        deployment_id=args.deployment_id,
        target_id=args.target_id,
        compose_project=args.compose_project,
        public_origin=args.public_origin,
        api_container=args.api_container,
        worker_container=args.worker_container,
        scheduler_container=args.scheduler_container,
        render_container=args.render_container,
        ingress_container=args.ingress_container,
        writer_topology_sha256=args.writer_topology_sha256,
        actor_id=args.actor_id,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify_parser = subparsers.add_parser("verify", help="verify a signed receipt without consuming it")
    _add_binding_arguments(verify_parser)
    consume_parser = subparsers.add_parser("consume", help="atomically verify and consume a signed receipt")
    _add_binding_arguments(consume_parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(argv if argv is not None else os.sys.argv[1:])
    args = build_parser().parse_args(raw_argv)
    try:
        return controller_guard.invoke_controller(
            "drain-verify" if args.command == "verify" else "promotion-consume",
            raw_argv[1:],
        )
    except (DrainReceiptError, controller_guard.ControllerGuardError) as exc:
        print(f"drain receipt rejected: {exc}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
