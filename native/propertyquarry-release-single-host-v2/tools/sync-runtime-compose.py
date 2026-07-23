#!/usr/bin/env python3
"""Transactionally publish the release-bound PropertyQuarry Compose files."""

from __future__ import annotations

import argparse
import base64
import ctypes
import errno
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
from typing import Any, Callable, NoReturn

import fcntl
from cryptography.exceptions import InvalidSignature


TOOLS = Path(__file__).resolve().parent
_PACKAGE_SPEC = importlib.util.spec_from_file_location(
    "propertyquarry_runtime_compose_sync_package_v2", TOOLS / "package.py"
)
if _PACKAGE_SPEC is None or _PACKAGE_SPEC.loader is None:  # pragma: no cover
    raise RuntimeError("runtime-compose-sync-package-unavailable")
package = importlib.util.module_from_spec(_PACKAGE_SPEC)
sys.modules[_PACKAGE_SPEC.name] = package
_PACKAGE_SPEC.loader.exec_module(package)


AUTHORITY_PARENT = Path(
    "/docker/property/state/runtime/propertyquarry-release-authority-v2.private"
)
RESERVATION_ROOT = AUTHORITY_PARENT / "single-host-v2-runner-reservation"
RESERVATION_LOCK = AUTHORITY_PARENT / ".single-host-v2-runner-reservation.lock"
RECEIPT_AUTHORITY_ROOT = AUTHORITY_PARENT / "single-host-v2-receipt-authority"
COMPOSE_SYNC_ROOT = AUTHORITY_PARENT / "single-host-v2-runtime-compose-sync"

COMPOSE_FILES = (
    ("docker-compose.property.yml", Path(package.PROPERTY_COMPOSE_PATH)),
    ("docker-compose.cloudflared.yml", Path(package.CLOUDFLARED_COMPOSE_PATH)),
)
INTENT_NAME = "intent.v2.json"
TERMINAL_NAME = "terminal.v2.json"
INTENT_STAGE_NAME = ".intent.v2.json.stage"
TERMINAL_STAGE_NAME = ".terminal.v2.json.stage"
BACKUP_DIRECTORY_NAME = "backups"
CANDIDATE_DIRECTORY_NAME = "candidates"
INTENT_SCHEMA = (
    "propertyquarry.release-control.single-host-runtime-compose-sync-intent.v2"
)
TERMINAL_SCHEMA = (
    "propertyquarry.release-control.single-host-runtime-compose-sync-terminal.v2"
)
RESULT_SCHEMA = (
    "propertyquarry.release-control.single-host-runtime-compose-sync-result.v2"
)
RECOVERY_RESULT_SCHEMA = (
    "propertyquarry.release-control.single-host-runtime-compose-sync-recovery-result.v2"
)
INTENT_SIGNATURE_DOMAIN = INTENT_SCHEMA.encode("ascii") + b"\0"
TERMINAL_SIGNATURE_DOMAIN = TERMINAL_SCHEMA.encode("ascii") + b"\0"
TRANSACTION_DOMAIN = (
    b"propertyquarry.release-control.single-host-runtime-compose-sync-transaction.v2\0"
)
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
TRANSACTION_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MAXIMUM_COMPOSE_BYTES = 2 * 1024 * 1024
MAXIMUM_ATTEMPTS = 128
TARGET_OLD_MODES = frozenset({0o600, 0o640, 0o644, 0o660, 0o664})
TARGET_FINAL_MODE = 0o644


class ComposeSyncFailure(ValueError):
    """A secret-free fixed-path Compose transaction rejection."""


def fail(code: str) -> NoReturn:
    raise ComposeSyncFailure(code)


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _materialize_module():
    name = "propertyquarry_runtime_compose_sync_materialize_v2"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, TOOLS / "materialize.py")
    if spec is None or spec.loader is None:
        fail("runtime-compose-sync-materializer-unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _exact_directory(
    path: Path,
    *,
    mode: int,
    uid: int = 1000,
    gid: int = 1000,
) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError:
        fail("runtime-compose-sync-directory-unavailable")
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != mode
        or metadata.st_uid != uid
        or metadata.st_gid != gid
        or path.resolve() != path
    ):
        fail("runtime-compose-sync-directory-metadata-invalid")
    return metadata


def _sync_directory(path: Path) -> None:
    try:
        descriptor = os.open(
            path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
    except OSError:
        fail("runtime-compose-sync-directory-sync-failed")
    try:
        os.fsync(descriptor)
    except OSError:
        fail("runtime-compose-sync-directory-sync-failed")
    finally:
        os.close(descriptor)


def _ensure_sync_root() -> None:
    _exact_directory(AUTHORITY_PARENT, mode=0o700)
    if not COMPOSE_SYNC_ROOT.exists():
        try:
            os.mkdir(COMPOSE_SYNC_ROOT, 0o700)
            os.chmod(COMPOSE_SYNC_ROOT, 0o700)
            _sync_directory(AUTHORITY_PARENT)
        except OSError:
            fail("runtime-compose-sync-root-create-failed")
    _exact_directory(COMPOSE_SYNC_ROOT, mode=0o700)


def _validate_target_paths() -> None:
    if (
        len(COMPOSE_FILES) != 2
        or [source for source, _target in COMPOSE_FILES]
        != [
            "docker-compose.property.yml",
            "docker-compose.cloudflared.yml",
        ]
        or any(
            not target.is_absolute()
            or target.name != source
            or target.parent != COMPOSE_FILES[0][1].parent
            for source, target in COMPOSE_FILES
        )
    ):
        fail("runtime-compose-sync-target-set-invalid")
    _exact_directory(COMPOSE_FILES[0][1].parent, mode=0o755)


def _acquire_lock() -> int:
    _exact_directory(AUTHORITY_PARENT, mode=0o700)
    try:
        descriptor = os.open(
            RESERVATION_LOCK,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
    except OSError:
        fail("runtime-compose-sync-lock-unavailable")
    try:
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != 1000
            or metadata.st_gid != 1000
            or metadata.st_nlink != 1
        ):
            fail("runtime-compose-sync-lock-invalid")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail("runtime-compose-sync-busy")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _read_regular(
    path: Path,
    *,
    maximum: int,
    modes: frozenset[int],
    uid: int = 1000,
    gid: int = 1000,
) -> tuple[bytes, dict[str, Any]]:
    try:
        before = path.lstat()
    except OSError:
        fail("runtime-compose-sync-file-unavailable")
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or stat.S_IMODE(before.st_mode) not in modes
        or before.st_uid != uid
        or before.st_gid != gid
        or before.st_nlink != 1
        or not 1 <= before.st_size <= maximum
        or path.resolve() != path
    ):
        fail("runtime-compose-sync-file-metadata-invalid")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        fail("runtime-compose-sync-file-read-failed")
    try:
        opened = os.fstat(descriptor)
        chunks: list[bytes] = []
        total = 0
        while total <= maximum:
            chunk = os.read(descriptor, min(65_536, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    identity = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_uid,
        before.st_gid,
        before.st_nlink,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    if (
        len(raw) != before.st_size
        or identity
        != (
            opened.st_dev,
            opened.st_ino,
            opened.st_mode,
            opened.st_uid,
            opened.st_gid,
            opened.st_nlink,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        )
        or identity
        != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_uid,
            after.st_gid,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
    ):
        fail("runtime-compose-sync-file-mutated")
    return raw, {
        "ctime_ns": before.st_ctime_ns,
        "device": before.st_dev,
        "gid": before.st_gid,
        "inode": before.st_ino,
        "mode": f"{stat.S_IMODE(before.st_mode):04o}",
        "mtime_ns": before.st_mtime_ns,
        "sha256": _digest(raw),
        "size": len(raw),
        "uid": before.st_uid,
    }


def _write_new_file(path: Path, raw: bytes, mode: int) -> None:
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | os.O_CLOEXEC,
            mode,
        )
    except OSError:
        fail("runtime-compose-sync-file-create-failed")
    succeeded = False
    try:
        os.fchmod(descriptor, mode)
        os.fchown(descriptor, 1000, 1000)
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written < 1:
                fail("runtime-compose-sync-file-write-failed")
            offset += written
        os.fsync(descriptor)
        succeeded = True
    except OSError:
        fail("runtime-compose-sync-file-write-failed")
    finally:
        os.close(descriptor)
        if not succeeded:
            try:
                path.unlink()
            except OSError:
                pass


def _rename_noreplace(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        fail("runtime-compose-sync-renameat2-unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if (
        renameat2(
            -100,
            os.fsencode(source),
            -100,
            os.fsencode(destination),
            1,
        )
        != 0
    ):
        failure = ctypes.get_errno()
        if failure == errno.EEXIST:
            fail("runtime-compose-sync-publish-conflict")
        fail("runtime-compose-sync-publish-failed")


def _exchange(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        fail("runtime-compose-sync-renameat2-unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if (
        renameat2(
            -100,
            os.fsencode(source),
            -100,
            os.fsencode(destination),
            2,
        )
        != 0
    ):
        fail("runtime-compose-sync-exchange-failed")


def _publish_atomic(path: Path, raw: bytes, mode: int, stage_name: str) -> None:
    stage = path.parent / stage_name
    if stage.exists() or stage.is_symlink():
        fail("runtime-compose-sync-record-stage-present")
    _write_new_file(stage, raw, mode)
    _sync_directory(path.parent)
    _rename_noreplace(stage, path)
    _sync_directory(path.parent)


def _signed_record(
    payload: dict[str, Any],
    *,
    private,
    key_id: str,
    domain: bytes,
) -> bytes:
    canonical = package.canonical_json(payload)
    signature = private.sign(package.framed(domain, canonical))
    return package.canonical_json(
        {
            "payload": payload,
            "signature": base64.urlsafe_b64encode(signature)
            .rstrip(b"=")
            .decode("ascii"),
            "signature_key_id": key_id,
        }
    )


def _decode_signed_record(
    raw: bytes,
    *,
    public,
    key_id: str,
    schema: str,
    domain: bytes,
) -> dict[str, Any]:
    try:
        wrapper = package.parse_strict_json(raw, "runtime-compose-sync-record")
    except package.PackageFailure:
        fail("runtime-compose-sync-record-invalid")
    if (
        not isinstance(wrapper, dict)
        or set(wrapper) != {"payload", "signature", "signature_key_id"}
        or wrapper.get("signature_key_id") != key_id
        or not isinstance(wrapper.get("payload"), dict)
        or not isinstance(wrapper.get("signature"), str)
    ):
        fail("runtime-compose-sync-record-shape-invalid")
    payload = wrapper["payload"]
    signature_text = wrapper["signature"]
    try:
        signature = base64.b64decode(
            signature_text.encode("ascii") + b"=" * (-len(signature_text) % 4),
            altchars=b"-_",
            validate=True,
        )
        public.verify(
            signature,
            package.framed(domain, package.canonical_json(payload)),
        )
    except (UnicodeEncodeError, ValueError, InvalidSignature):
        fail("runtime-compose-sync-record-signature-invalid")
    if (
        len(signature) != 64
        or signature_text
        != base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        or payload.get("schema") != schema
        or payload.get("version") != 2
    ):
        fail("runtime-compose-sync-record-binding-invalid")
    return payload


def _git(checkout: Path, *arguments: str) -> bytes:
    try:
        metadata = os.stat("/usr/bin/git", follow_symlinks=False)
    except OSError:
        fail("runtime-compose-sync-git-unavailable")
    if (
        os.path.realpath("/usr/bin/git") != "/usr/bin/git"
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o755
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_nlink != 1
    ):
        fail("runtime-compose-sync-git-invalid")
    try:
        completed = subprocess.run(
            ["/usr/bin/git", "-C", os.fspath(checkout), *arguments],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            env={
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "HOME": "/nonexistent",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": "/usr/bin:/bin",
                "TZ": "UTC",
            },
        )
    except (OSError, subprocess.SubprocessError):
        fail("runtime-compose-sync-git-failed")
    if completed.returncode != 0 or len(completed.stdout) > MAXIMUM_COMPOSE_BYTES:
        fail("runtime-compose-sync-git-failed")
    return completed.stdout


def _source_blobs(reservation_payload: dict[str, Any]) -> dict[str, bytes]:
    workflow_sha = reservation_payload.get("workflow_sha")
    checkout_text = reservation_payload.get("source_checkout_path")
    if (
        not isinstance(workflow_sha, str)
        or re.fullmatch(r"[0-9a-f]{40}", workflow_sha) is None
        or not isinstance(checkout_text, str)
    ):
        fail("runtime-compose-sync-reservation-binding-invalid")
    checkout = Path(checkout_text)
    blobs: dict[str, bytes] = {}
    for source_path, _target in COMPOSE_FILES:
        tree = _git(checkout, "ls-tree", workflow_sha, "--", source_path)
        fields = tree.rstrip(b"\n").split()
        if (
            len(fields) != 4
            or fields[0] != b"100644"
            or fields[1] != b"blob"
            or not re.fullmatch(rb"[0-9a-f]{40}", fields[2])
            or fields[3] != source_path.encode("ascii")
        ):
            fail("runtime-compose-sync-source-mode-invalid")
        raw = _git(checkout, "show", f"{workflow_sha}:{source_path}")
        if not 1 <= len(raw) <= MAXIMUM_COMPOSE_BYTES:
            fail("runtime-compose-sync-source-invalid")
        blobs[source_path] = raw
    if (
        _git(checkout, "rev-parse", "--verify", "HEAD").strip()
        != workflow_sha.encode("ascii")
        or _git(checkout, "status", "--porcelain=v1", "--untracked-files=all")
    ):
        fail("runtime-compose-sync-source-mutated")
    return blobs


def _intent_entry(
    *,
    transaction: Path,
    transaction_id: str,
    index: int,
    source_path: str,
    target: Path,
    old: dict[str, Any],
    new_raw: bytes,
) -> dict[str, Any]:
    backup = transaction / BACKUP_DIRECTORY_NAME / f"{index}.old"
    candidate = transaction / CANDIDATE_DIRECTORY_NAME / f"{index}.new"
    stage = target.parent / (
        f".propertyquarry-runtime-compose-sync-v2.{transaction_id}.{index}.stage"
    )
    return {
        "backup_path": os.fspath(backup),
        "candidate_path": os.fspath(candidate),
        "new_mode": "0644",
        "new_sha256": _digest(new_raw),
        "new_size": len(new_raw),
        "old_ctime_ns": old["ctime_ns"],
        "old_device": old["device"],
        "old_gid": old["gid"],
        "old_inode": old["inode"],
        "old_mode": old["mode"],
        "old_mtime_ns": old["mtime_ns"],
        "old_sha256": old["sha256"],
        "old_size": old["size"],
        "old_uid": old["uid"],
        "source_path": source_path,
        "stage_path": os.fspath(stage),
        "target_path": os.fspath(target),
    }


def _validate_intent(
    payload: dict[str, Any],
    *,
    attempt: Path,
    receipt_id: str,
) -> None:
    expected_keys = {
        "authority_profile",
        "created_at_epoch",
        "environment",
        "files",
        "receipt_authority_key_id",
        "repository",
        "reservation_sha256",
        "runner_label",
        "schema",
        "source_checkout_identity_sha256",
        "source_tree_sha256",
        "transaction_id",
        "version",
        "workflow_sha",
    }
    transaction_id = payload.get("transaction_id")
    created = payload.get("created_at_epoch")
    if (
        set(payload) != expected_keys
        or payload.get("authority_profile") != package.PROFILE
        or payload.get("environment") != package.ENVIRONMENT
        or payload.get("repository") != package.REPOSITORY
        or payload.get("receipt_authority_key_id") != receipt_id
        or not isinstance(transaction_id, str)
        or TRANSACTION_PATTERN.fullmatch(transaction_id) is None
        or attempt.name != transaction_id
        or type(created) is not int
        or isinstance(created, bool)
        or created < 1
        or not isinstance(payload.get("reservation_sha256"), str)
        or SHA256_PATTERN.fullmatch(payload["reservation_sha256"]) is None
        or not isinstance(payload.get("workflow_sha"), str)
        or re.fullmatch(r"[0-9a-f]{40}", payload["workflow_sha"]) is None
        or not isinstance(payload.get("runner_label"), str)
        or re.fullmatch(r"pqrelease-[0-9a-f]{32}", payload["runner_label"]) is None
        or not isinstance(payload.get("source_checkout_identity_sha256"), str)
        or SHA256_PATTERN.fullmatch(payload["source_checkout_identity_sha256"])
        is None
        or not isinstance(payload.get("source_tree_sha256"), str)
        or SHA256_PATTERN.fullmatch(payload["source_tree_sha256"]) is None
        or not isinstance(payload.get("files"), list)
        or len(payload["files"]) != len(COMPOSE_FILES)
    ):
        fail("runtime-compose-sync-intent-invalid")
    for index, ((source_path, target), entry) in enumerate(
        zip(COMPOSE_FILES, payload["files"], strict=True)
    ):
        expected_entry_keys = {
            "backup_path",
            "candidate_path",
            "new_mode",
            "new_sha256",
            "new_size",
            "old_ctime_ns",
            "old_device",
            "old_gid",
            "old_inode",
            "old_mode",
            "old_mtime_ns",
            "old_sha256",
            "old_size",
            "old_uid",
            "source_path",
            "stage_path",
            "target_path",
        }
        expected_backup = attempt / BACKUP_DIRECTORY_NAME / f"{index}.old"
        expected_candidate = attempt / CANDIDATE_DIRECTORY_NAME / f"{index}.new"
        expected_stage = target.parent / (
            f".propertyquarry-runtime-compose-sync-v2.{transaction_id}.{index}.stage"
        )
        if (
            not isinstance(entry, dict)
            or set(entry) != expected_entry_keys
            or entry.get("source_path") != source_path
            or entry.get("target_path") != os.fspath(target)
            or entry.get("backup_path") != os.fspath(expected_backup)
            or entry.get("candidate_path") != os.fspath(expected_candidate)
            or entry.get("stage_path") != os.fspath(expected_stage)
            or entry.get("new_mode") != "0644"
            or not isinstance(entry.get("new_sha256"), str)
            or SHA256_PATTERN.fullmatch(entry["new_sha256"]) is None
            or type(entry.get("new_size")) is not int
            or not 1 <= entry["new_size"] <= MAXIMUM_COMPOSE_BYTES
            or not isinstance(entry.get("old_sha256"), str)
            or SHA256_PATTERN.fullmatch(entry["old_sha256"]) is None
            or type(entry.get("old_size")) is not int
            or not 1 <= entry["old_size"] <= MAXIMUM_COMPOSE_BYTES
            or entry.get("old_mode")
            not in {f"{mode:04o}" for mode in TARGET_OLD_MODES}
            or entry.get("old_uid") != 1000
            or entry.get("old_gid") != 1000
            or any(
                type(entry.get(name)) is not int
                or isinstance(entry.get(name), bool)
                or entry[name] < 0
                for name in (
                    "old_ctime_ns",
                    "old_device",
                    "old_inode",
                    "old_mtime_ns",
                )
            )
        ):
            fail("runtime-compose-sync-intent-file-invalid")


def _validate_terminal(
    payload: dict[str, Any],
    *,
    intent: dict[str, Any],
    intent_raw: bytes,
    receipt_id: str,
) -> None:
    expected_keys = {
        "authority_profile",
        "completed_at_epoch",
        "disposition",
        "environment",
        "files",
        "intent_sha256",
        "receipt_authority_key_id",
        "recovered",
        "repository",
        "reservation_sha256",
        "schema",
        "transaction_id",
        "version",
        "workflow_sha",
    }
    disposition = payload.get("disposition")
    completed = payload.get("completed_at_epoch")
    if (
        set(payload) != expected_keys
        or payload.get("authority_profile") != package.PROFILE
        or payload.get("environment") != package.ENVIRONMENT
        or payload.get("repository") != package.REPOSITORY
        or payload.get("receipt_authority_key_id") != receipt_id
        or payload.get("reservation_sha256") != intent["reservation_sha256"]
        or payload.get("workflow_sha") != intent["workflow_sha"]
        or payload.get("transaction_id") != intent["transaction_id"]
        or payload.get("intent_sha256") != _digest(intent_raw)
        or disposition not in {"committed", "rolled-back"}
        or type(payload.get("recovered")) is not bool
        or type(completed) is not int
        or isinstance(completed, bool)
        or completed < intent["created_at_epoch"]
        or not isinstance(payload.get("files"), list)
        or len(payload["files"]) != len(intent["files"])
    ):
        fail("runtime-compose-sync-terminal-invalid")
    for intent_entry, terminal_entry in zip(
        intent["files"], payload["files"], strict=True
    ):
        expected_digest = (
            intent_entry["new_sha256"]
            if disposition == "committed"
            else intent_entry["old_sha256"]
        )
        expected_mode = (
            "0644" if disposition == "committed" else intent_entry["old_mode"]
        )
        if terminal_entry != {
            "final_mode": expected_mode,
            "final_sha256": expected_digest,
            "target_path": intent_entry["target_path"],
        }:
            fail("runtime-compose-sync-terminal-file-invalid")


def _read_record(path: Path) -> bytes:
    raw, _metadata = _read_regular(
        path,
        maximum=package.MAX_JSON_BYTES,
        modes=frozenset({0o600}),
    )
    return raw


def _attempt_directories() -> list[Path]:
    if not COMPOSE_SYNC_ROOT.exists():
        return []
    _exact_directory(COMPOSE_SYNC_ROOT, mode=0o700)
    try:
        attempts = sorted(COMPOSE_SYNC_ROOT.iterdir(), key=lambda item: item.name)
    except OSError:
        fail("runtime-compose-sync-root-read-failed")
    if len(attempts) > MAXIMUM_ATTEMPTS:
        fail("runtime-compose-sync-attempt-limit-exceeded")
    for attempt in attempts:
        if TRANSACTION_PATTERN.fullmatch(attempt.name) is None:
            fail("runtime-compose-sync-attempt-name-invalid")
        _exact_directory(attempt, mode=0o700)
    return attempts


def _attempt_records(
    attempt: Path,
    *,
    receipt_public,
    receipt_id: str,
    allow_pending: bool,
) -> tuple[bytes, dict[str, Any], bytes | None, dict[str, Any] | None]:
    try:
        names = {item.name for item in attempt.iterdir()}
    except OSError:
        fail("runtime-compose-sync-attempt-read-failed")
    required = {INTENT_NAME, BACKUP_DIRECTORY_NAME, CANDIDATE_DIRECTORY_NAME}
    allowed = required | {TERMINAL_NAME, TERMINAL_STAGE_NAME}
    if not required.issubset(names) or not names.issubset(allowed):
        fail("runtime-compose-sync-attempt-file-set-invalid")
    backup_directory = attempt / BACKUP_DIRECTORY_NAME
    candidate_directory = attempt / CANDIDATE_DIRECTORY_NAME
    _exact_directory(backup_directory, mode=0o700)
    _exact_directory(candidate_directory, mode=0o700)
    try:
        backup_names = {item.name for item in backup_directory.iterdir()}
        candidate_names = {item.name for item in candidate_directory.iterdir()}
    except OSError:
        fail("runtime-compose-sync-private-payload-read-failed")
    expected_backups = {f"{index}.old" for index in range(len(COMPOSE_FILES))}
    expected_candidates = {
        f"{index}.new" for index in range(len(COMPOSE_FILES))
    }
    if (
        backup_names != expected_backups
        or candidate_names != expected_candidates
    ):
        fail("runtime-compose-sync-private-payload-set-invalid")
    intent_raw = _read_record(attempt / INTENT_NAME)
    intent = _decode_signed_record(
        intent_raw,
        public=receipt_public,
        key_id=receipt_id,
        schema=INTENT_SCHEMA,
        domain=INTENT_SIGNATURE_DOMAIN,
    )
    _validate_intent(intent, attempt=attempt, receipt_id=receipt_id)
    terminal_path = attempt / TERMINAL_NAME
    terminal_stage = attempt / TERMINAL_STAGE_NAME
    terminal_raw: bytes | None = None
    terminal: dict[str, Any] | None = None
    if terminal_path.exists():
        if terminal_stage.exists() or terminal_stage.is_symlink():
            fail("runtime-compose-sync-terminal-stage-residue")
        terminal_raw = _read_record(terminal_path)
    elif terminal_stage.exists():
        terminal_raw = _read_record(terminal_stage)
        terminal = _decode_signed_record(
            terminal_raw,
            public=receipt_public,
            key_id=receipt_id,
            schema=TERMINAL_SCHEMA,
            domain=TERMINAL_SIGNATURE_DOMAIN,
        )
        _validate_terminal(
            terminal,
            intent=intent,
            intent_raw=intent_raw,
            receipt_id=receipt_id,
        )
        if not allow_pending:
            fail("runtime-compose-sync-pending")
        expected_state = (
            "new" if terminal["disposition"] == "committed" else "old"
        )
        if any(
            _target_state(entry)[0] != expected_state
            for entry in intent["files"]
        ):
            fail("runtime-compose-sync-terminal-stage-state-invalid")
        _require_no_stages(intent)
        _rename_noreplace(terminal_stage, terminal_path)
        _sync_directory(attempt)
    elif not allow_pending:
        fail("runtime-compose-sync-pending")
    if terminal_raw is not None and terminal is None:
        terminal = _decode_signed_record(
            terminal_raw,
            public=receipt_public,
            key_id=receipt_id,
            schema=TERMINAL_SCHEMA,
            domain=TERMINAL_SIGNATURE_DOMAIN,
        )
        _validate_terminal(
            terminal,
            intent=intent,
            intent_raw=intent_raw,
            receipt_id=receipt_id,
        )
    return intent_raw, intent, terminal_raw, terminal


def _payload_files(
    intent: dict[str, Any],
) -> tuple[list[bytes | None], list[bytes | None]]:
    backups: list[bytes | None] = []
    candidates: list[bytes | None] = []
    for entry in intent["files"]:
        try:
            backup, _ = _read_regular(
                Path(entry["backup_path"]),
                maximum=MAXIMUM_COMPOSE_BYTES,
                modes=frozenset({0o400}),
            )
        except ComposeSyncFailure:
            backup = None
        try:
            candidate, _ = _read_regular(
                Path(entry["candidate_path"]),
                maximum=MAXIMUM_COMPOSE_BYTES,
                modes=frozenset({0o400}),
            )
        except ComposeSyncFailure:
            candidate = None
        if (
            backup is not None
            and (
                _digest(backup) != entry["old_sha256"]
                or len(backup) != entry["old_size"]
            )
        ):
            backup = None
        if (
            candidate is not None
            and (
                _digest(candidate) != entry["new_sha256"]
                or len(candidate) != entry["new_size"]
            )
        ):
            candidate = None
        backups.append(backup)
        candidates.append(candidate)
    return backups, candidates


def _target_state(
    entry: dict[str, Any],
) -> tuple[str, bytes, dict[str, Any]]:
    raw, observed = _read_regular(
        Path(entry["target_path"]),
        maximum=MAXIMUM_COMPOSE_BYTES,
        modes=TARGET_OLD_MODES | frozenset({TARGET_FINAL_MODE}),
    )
    if (
        observed["sha256"] == entry["new_sha256"]
        and observed["size"] == entry["new_size"]
        and observed["mode"] == "0644"
    ):
        return "new", raw, observed
    if (
        observed["sha256"] == entry["old_sha256"]
        and observed["size"] == entry["old_size"]
        and observed["mode"] == entry["old_mode"]
    ):
        return "old", raw, observed
    fail("runtime-compose-sync-target-diverged")


def _stage_payload(path: Path, raw: bytes, mode: int) -> None:
    if path.exists() or path.is_symlink():
        try:
            metadata = path.lstat()
        except OSError:
            fail("runtime-compose-sync-stage-unavailable")
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode)
            not in TARGET_OLD_MODES | frozenset({TARGET_FINAL_MODE})
            or metadata.st_uid != 1000
            or metadata.st_gid != 1000
            or metadata.st_nlink != 1
            or not 0 <= metadata.st_size <= MAXIMUM_COMPOSE_BYTES
            or path.resolve() != path
        ):
            fail("runtime-compose-sync-stage-conflict")
        path.unlink()
        _sync_directory(path.parent)
    _write_new_file(path, raw, mode)
    _sync_directory(path.parent)


def _same_observation(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return left == right


def _publish_payload(
    entry: dict[str, Any],
    *,
    payload: bytes,
    mode: int,
    expected_state: str,
    require_original_identity: bool,
) -> None:
    target = Path(entry["target_path"])
    stage = Path(entry["stage_path"])
    state, _current_raw, before = _target_state(entry)
    if state != expected_state:
        fail("runtime-compose-sync-cas-mismatch")
    if require_original_identity:
        expected_original = {
            "ctime_ns": entry["old_ctime_ns"],
            "device": entry["old_device"],
            "gid": entry["old_gid"],
            "inode": entry["old_inode"],
            "mode": entry["old_mode"],
            "mtime_ns": entry["old_mtime_ns"],
            "sha256": entry["old_sha256"],
            "size": entry["old_size"],
            "uid": entry["old_uid"],
        }
        if not _same_observation(before, expected_original):
            fail("runtime-compose-sync-old-identity-changed")
    _stage_payload(stage, payload, mode)
    state_again, _raw_again, immediately_before = _target_state(entry)
    if state_again != expected_state or not _same_observation(
        before, immediately_before
    ):
        fail("runtime-compose-sync-double-cas-mismatch")
    _exchange(stage, target)
    _sync_directory(target.parent)
    displaced, displaced_observation = _read_regular(
        stage,
        maximum=MAXIMUM_COMPOSE_BYTES,
        modes=TARGET_OLD_MODES | frozenset({TARGET_FINAL_MODE}),
    )
    published, published_observation = _read_regular(
        target,
        maximum=MAXIMUM_COMPOSE_BYTES,
        modes=frozenset({mode}),
    )
    if (
        displaced_observation["device"] != before["device"]
        or displaced_observation["inode"] != before["inode"]
        or displaced_observation["sha256"] != before["sha256"]
        or displaced_observation["size"] != before["size"]
        or displaced_observation["mode"] != before["mode"]
    ):
        try:
            if (
                published == payload
                and published_observation["sha256"] == _digest(payload)
                and published_observation["mode"] == f"{mode:04o}"
            ):
                _exchange(stage, target)
                _sync_directory(target.parent)
                restored, _restored_observation = _read_regular(
                    target,
                    maximum=MAXIMUM_COMPOSE_BYTES,
                    modes=TARGET_OLD_MODES
                    | frozenset({TARGET_FINAL_MODE}),
                )
                candidate_stage, _candidate_stage_observation = _read_regular(
                    stage,
                    maximum=MAXIMUM_COMPOSE_BYTES,
                    modes=frozenset({mode}),
                )
                if restored == displaced and candidate_stage == payload:
                    stage.unlink()
                    _sync_directory(target.parent)
        except ComposeSyncFailure:
            pass
        fail("runtime-compose-sync-exchange-cas-mismatch")
    if (
        published != payload
        or published_observation["sha256"] != _digest(payload)
        or published_observation["mode"] != f"{mode:04o}"
    ):
        fail("runtime-compose-sync-publish-verification-failed")
    if _digest(displaced) != before["sha256"]:
        fail("runtime-compose-sync-displaced-content-invalid")
    stage.unlink()
    _sync_directory(target.parent)


def _terminal_payload(
    *,
    intent: dict[str, Any],
    intent_raw: bytes,
    disposition: str,
    recovered: bool,
    completed_at: int,
) -> dict[str, Any]:
    return {
        "authority_profile": package.PROFILE,
        "completed_at_epoch": completed_at,
        "disposition": disposition,
        "environment": package.ENVIRONMENT,
        "files": [
            {
                "final_mode": (
                    "0644" if disposition == "committed" else entry["old_mode"]
                ),
                "final_sha256": (
                    entry["new_sha256"]
                    if disposition == "committed"
                    else entry["old_sha256"]
                ),
                "target_path": entry["target_path"],
            }
            for entry in intent["files"]
        ],
        "intent_sha256": _digest(intent_raw),
        "receipt_authority_key_id": intent["receipt_authority_key_id"],
        "recovered": recovered,
        "repository": package.REPOSITORY,
        "reservation_sha256": intent["reservation_sha256"],
        "schema": TERMINAL_SCHEMA,
        "transaction_id": intent["transaction_id"],
        "version": 2,
        "workflow_sha": intent["workflow_sha"],
    }


def _remove_stages(intent: dict[str, Any]) -> None:
    for entry in intent["files"]:
        stage = Path(entry["stage_path"])
        if not stage.exists() and not stage.is_symlink():
            continue
        try:
            metadata = stage.lstat()
        except OSError:
            fail("runtime-compose-sync-stage-residue-invalid")
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode)
            not in TARGET_OLD_MODES | frozenset({TARGET_FINAL_MODE})
            or metadata.st_uid != 1000
            or metadata.st_gid != 1000
            or metadata.st_nlink != 1
            or not 0 <= metadata.st_size <= MAXIMUM_COMPOSE_BYTES
            or stage.resolve() != stage
        ):
            fail("runtime-compose-sync-stage-residue-invalid")
        stage.unlink()
        _sync_directory(stage.parent)


def _require_no_stages(intent: dict[str, Any]) -> None:
    if any(
        Path(entry["stage_path"]).exists()
        or Path(entry["stage_path"]).is_symlink()
        for entry in intent["files"]
    ):
        fail("runtime-compose-sync-stage-residue")


def _publish_terminal(
    attempt: Path,
    *,
    intent: dict[str, Any],
    intent_raw: bytes,
    disposition: str,
    recovered: bool,
    completed_at: int,
    receipt_private,
    receipt_id: str,
) -> bytes:
    _remove_stages(intent)
    payload = _terminal_payload(
        intent=intent,
        intent_raw=intent_raw,
        disposition=disposition,
        recovered=recovered,
        completed_at=completed_at,
    )
    raw = _signed_record(
        payload,
        private=receipt_private,
        key_id=receipt_id,
        domain=TERMINAL_SIGNATURE_DOMAIN,
    )
    _publish_atomic(
        attempt / TERMINAL_NAME,
        raw,
        0o600,
        TERMINAL_STAGE_NAME,
    )
    return raw


def _recover_attempt(
    attempt: Path,
    *,
    intent_raw: bytes,
    intent: dict[str, Any],
    receipt_private,
    receipt_id: str,
    now: int,
) -> str:
    states = [_target_state(entry)[0] for entry in intent["files"]]
    backups, candidates = _payload_files(intent)
    if any(item is None for item in backups):
        fail("runtime-compose-sync-backup-invalid")
    if all(state == "old" for state in states):
        disposition = "rolled-back"
    elif all(item is not None for item in candidates):
        for entry, state, candidate in zip(
            intent["files"], states, candidates, strict=True
        ):
            if state == "old":
                if candidate is None:
                    fail("runtime-compose-sync-candidate-invalid")
                _publish_payload(
                    entry,
                    payload=candidate,
                    mode=TARGET_FINAL_MODE,
                    expected_state="old",
                    require_original_identity=False,
                )
        if any(_target_state(entry)[0] != "new" for entry in intent["files"]):
            fail("runtime-compose-sync-recovery-complete-failed")
        disposition = "committed"
    else:
        for entry, state, backup in zip(
            reversed(intent["files"]),
            reversed(states),
            reversed(backups),
            strict=True,
        ):
            if state == "new":
                if backup is None:
                    fail("runtime-compose-sync-backup-invalid")
                _publish_payload(
                    entry,
                    payload=backup,
                    mode=int(entry["old_mode"], 8),
                    expected_state="new",
                    require_original_identity=False,
                )
        if any(_target_state(entry)[0] != "old" for entry in intent["files"]):
            fail("runtime-compose-sync-recovery-rollback-failed")
        disposition = "rolled-back"
    _publish_terminal(
        attempt,
        intent=intent,
        intent_raw=intent_raw,
        disposition=disposition,
        recovered=True,
        completed_at=max(now, intent["created_at_epoch"]),
        receipt_private=receipt_private,
        receipt_id=receipt_id,
    )
    return disposition


def _recover_pending(
    *,
    receipt_private,
    receipt_public,
    receipt_id: str,
    now: int,
) -> dict[str, int]:
    committed = 0
    rolled_back = 0
    for attempt in _attempt_directories():
        try:
            names = {item.name for item in attempt.iterdir()}
        except OSError:
            fail("runtime-compose-sync-attempt-read-failed")
        if INTENT_NAME not in names:
            if any(
                (
                    target.parent
                    / f".propertyquarry-runtime-compose-sync-v2.{attempt.name}.{index}.stage"
                ).exists()
                or (
                    target.parent
                    / f".propertyquarry-runtime-compose-sync-v2.{attempt.name}.{index}.stage"
                ).is_symlink()
                for index, (_source, target) in enumerate(COMPOSE_FILES)
            ):
                fail("runtime-compose-sync-unsigned-stage-present")
            shutil.rmtree(attempt)
            _sync_directory(COMPOSE_SYNC_ROOT)
            continue
        terminal_path = attempt / TERMINAL_NAME
        terminal_stage = attempt / TERMINAL_STAGE_NAME
        if (
            not terminal_path.exists()
            and not terminal_path.is_symlink()
            and (terminal_stage.exists() or terminal_stage.is_symlink())
        ):
            try:
                metadata = terminal_stage.lstat()
            except OSError:
                fail("runtime-compose-sync-terminal-stage-invalid")
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != 1000
                or metadata.st_gid != 1000
                or metadata.st_nlink != 1
                or not 0 <= metadata.st_size <= package.MAX_JSON_BYTES
                or terminal_stage.resolve() != terminal_stage
            ):
                fail("runtime-compose-sync-terminal-stage-invalid")
            terminal_stage.unlink()
            _sync_directory(attempt)
        intent_raw, intent, terminal_raw, terminal = _attempt_records(
            attempt,
            receipt_public=receipt_public,
            receipt_id=receipt_id,
            allow_pending=True,
        )
        if terminal_raw is not None:
            if terminal is None:
                fail("runtime-compose-sync-terminal-invalid")
            _remove_stages(intent)
            continue
        disposition = _recover_attempt(
            attempt,
            intent_raw=intent_raw,
            intent=intent,
            receipt_private=receipt_private,
            receipt_id=receipt_id,
            now=now,
        )
        if disposition == "committed":
            committed += 1
        else:
            rolled_back += 1
    return {"committed": committed, "rolled_back": rolled_back}


def _load_active_context(now: int):
    if os.geteuid() != 1000 or os.getegid() != 1000:
        fail("runtime-compose-sync-identity-invalid")
    materialize = _materialize_module()
    (
        _package_anchor,
        _package_private,
        _package_public,
        receipt_private,
        receipt_public,
        _package_id,
        receipt_id,
    ) = materialize._load_authority(os.fspath(RECEIPT_AUTHORITY_ROOT))
    if not RESERVATION_ROOT.exists():
        fail("runtime-compose-sync-reservation-missing")
    reservation_raw = materialize._read_runner_reservation_directory(
        RESERVATION_ROOT
    )
    try:
        wrapper = package.parse_strict_json(
            reservation_raw, "runtime-compose-sync-reservation"
        )
        workflow_sha = wrapper["payload"]["workflow_sha"]
    except (KeyError, TypeError, package.PackageFailure):
        fail("runtime-compose-sync-reservation-invalid")
    reservation_payload, reservation_binding = (
        materialize._validate_runner_reservation(
            reservation_raw,
            receipt_public=receipt_public,
            receipt_id=receipt_id,
            workflow_sha=workflow_sha,
            current=now,
        )
    )
    return (
        reservation_raw,
        reservation_payload,
        reservation_binding,
        receipt_private,
        receipt_public,
        receipt_id,
    )


def _matching_committed(
    *,
    reservation_raw: bytes,
    reservation_payload: dict[str, Any],
    reservation_binding: dict[str, Any],
    receipt_public,
    receipt_id: str,
) -> list[tuple[bytes, dict[str, Any]]]:
    matches: list[tuple[bytes, dict[str, Any]]] = []
    reservation_digest = _digest(reservation_raw)
    for attempt in _attempt_directories():
        intent_raw, intent, terminal_raw, terminal = _attempt_records(
            attempt,
            receipt_public=receipt_public,
            receipt_id=receipt_id,
            allow_pending=False,
        )
        del intent_raw
        if terminal_raw is None or terminal is None:
            fail("runtime-compose-sync-terminal-invalid")
        if (
            intent["reservation_sha256"] == reservation_digest
            and terminal["disposition"] == "committed"
        ):
            if (
                intent["workflow_sha"] != reservation_payload["workflow_sha"]
                or intent["runner_label"] != reservation_payload["runner_label"]
                or intent["source_checkout_identity_sha256"]
                != reservation_binding["source_checkout_identity_sha256"]
                or intent["source_tree_sha256"]
                != reservation_binding["source_tree_sha256"]
            ):
                fail("runtime-compose-sync-reservation-rebound")
            matches.append((terminal_raw, intent))
    return matches


def sync_runtime_compose(
    *,
    expected_property_sha256: str,
    expected_cloudflared_sha256: str,
    now: int | None = None,
    random_source: Callable[[int], bytes] = os.urandom,
    checkpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    expected = (expected_property_sha256, expected_cloudflared_sha256)
    if any(
        not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None
        for value in expected
    ):
        fail("runtime-compose-sync-expected-sha-invalid")
    current = int(time.time()) if now is None else now
    if type(current) is not int or isinstance(current, bool) or current < 1:
        fail("runtime-compose-sync-time-invalid")
    _validate_target_paths()
    lock = _acquire_lock()
    try:
        (
            reservation_raw,
            reservation_payload,
            reservation_binding,
            receipt_private,
            receipt_public,
            receipt_id,
        ) = _load_active_context(current)
        _ensure_sync_root()
        _recover_pending(
            receipt_private=receipt_private,
            receipt_public=receipt_public,
            receipt_id=receipt_id,
            now=current,
        )
        existing = _matching_committed(
            reservation_raw=reservation_raw,
            reservation_payload=reservation_payload,
            reservation_binding=reservation_binding,
            receipt_public=receipt_public,
            receipt_id=receipt_id,
        )
        if existing:
            if len(existing) != 1:
                fail("runtime-compose-sync-committed-ambiguous")
            existing_old = tuple(
                entry["old_sha256"] for entry in existing[0][1]["files"]
            )
            if existing_old != expected:
                fail("runtime-compose-sync-old-sha-cas-mismatch")
            verification = verify_for_materialization(
                reservation_raw=reservation_raw,
                reservation_payload=reservation_payload,
                reservation_binding=reservation_binding,
                receipt_public=receipt_public,
                receipt_id=receipt_id,
            )
            return {
                "disposition": "already-committed",
                "reservation_sha256": _digest(reservation_raw),
                "schema": RESULT_SCHEMA,
                "terminal_sha256": verification["terminal_sha256"],
                "transaction_id": verification["transaction_id"],
                "version": 2,
                "workflow_sha": reservation_payload["workflow_sha"],
            }
        source_blobs = _source_blobs(reservation_payload)
        old_records: list[tuple[bytes, dict[str, Any]]] = []
        for expected_digest, (_source_path, target) in zip(
            expected, COMPOSE_FILES, strict=True
        ):
            raw, observation = _read_regular(
                target,
                maximum=MAXIMUM_COMPOSE_BYTES,
                modes=TARGET_OLD_MODES,
            )
            if observation["sha256"] != expected_digest:
                fail("runtime-compose-sync-old-sha-cas-mismatch")
            old_records.append((raw, observation))
        entropy = random_source(32)
        if not isinstance(entropy, bytes) or len(entropy) != 32:
            fail("runtime-compose-sync-random-invalid")
        transaction_id = hashlib.sha256(
            TRANSACTION_DOMAIN
            + reservation_raw
            + package.canonical_json({"expected_old_sha256": list(expected)})
            + entropy
        ).hexdigest()
        attempt = COMPOSE_SYNC_ROOT / transaction_id
        try:
            os.mkdir(attempt, 0o700)
            os.chmod(attempt, 0o700)
            os.mkdir(attempt / BACKUP_DIRECTORY_NAME, 0o700)
            os.chmod(attempt / BACKUP_DIRECTORY_NAME, 0o700)
            os.mkdir(attempt / CANDIDATE_DIRECTORY_NAME, 0o700)
            os.chmod(attempt / CANDIDATE_DIRECTORY_NAME, 0o700)
            _sync_directory(COMPOSE_SYNC_ROOT)
        except OSError:
            fail("runtime-compose-sync-attempt-create-failed")
        entries: list[dict[str, Any]] = []
        for index, ((source_path, target), (old_raw, old), expected_digest) in enumerate(
            zip(COMPOSE_FILES, old_records, expected, strict=True)
        ):
            if old["sha256"] != expected_digest:
                fail("runtime-compose-sync-old-sha-cas-mismatch")
            new_raw = source_blobs[source_path]
            _write_new_file(
                attempt / BACKUP_DIRECTORY_NAME / f"{index}.old",
                old_raw,
                0o400,
            )
            _write_new_file(
                attempt / CANDIDATE_DIRECTORY_NAME / f"{index}.new",
                new_raw,
                0o400,
            )
            entries.append(
                _intent_entry(
                    transaction=attempt,
                    transaction_id=transaction_id,
                    index=index,
                    source_path=source_path,
                    target=target,
                    old=old,
                    new_raw=new_raw,
                )
            )
        _sync_directory(attempt / BACKUP_DIRECTORY_NAME)
        _sync_directory(attempt / CANDIDATE_DIRECTORY_NAME)
        intent = {
            "authority_profile": package.PROFILE,
            "created_at_epoch": current,
            "environment": package.ENVIRONMENT,
            "files": entries,
            "receipt_authority_key_id": receipt_id,
            "repository": package.REPOSITORY,
            "reservation_sha256": _digest(reservation_raw),
            "runner_label": reservation_payload["runner_label"],
            "schema": INTENT_SCHEMA,
            "source_checkout_identity_sha256": reservation_binding[
                "source_checkout_identity_sha256"
            ],
            "source_tree_sha256": reservation_binding["source_tree_sha256"],
            "transaction_id": transaction_id,
            "version": 2,
            "workflow_sha": reservation_payload["workflow_sha"],
        }
        intent_raw = _signed_record(
            intent,
            private=receipt_private,
            key_id=receipt_id,
            domain=INTENT_SIGNATURE_DOMAIN,
        )
        _publish_atomic(
            attempt / INTENT_NAME,
            intent_raw,
            0o600,
            INTENT_STAGE_NAME,
        )
        if checkpoint is not None:
            checkpoint("after-intent")
        candidates = [source_blobs[source] for source, _target in COMPOSE_FILES]
        for index, (entry, candidate) in enumerate(
            zip(entries, candidates, strict=True)
        ):
            _publish_payload(
                entry,
                payload=candidate,
                mode=TARGET_FINAL_MODE,
                expected_state="old",
                require_original_identity=True,
            )
            if checkpoint is not None:
                checkpoint(f"after-target-{index}")
        if any(_target_state(entry)[0] != "new" for entry in entries):
            fail("runtime-compose-sync-final-verification-failed")
        materialize = _materialize_module()
        materialize._validate_runner_checkout(reservation_payload)
        terminal_raw = _publish_terminal(
            attempt,
            intent=intent,
            intent_raw=intent_raw,
            disposition="committed",
            recovered=False,
            completed_at=current,
            receipt_private=receipt_private,
            receipt_id=receipt_id,
        )
        verification = verify_for_materialization(
            reservation_raw=reservation_raw,
            reservation_payload=reservation_payload,
            reservation_binding=reservation_binding,
            receipt_public=receipt_public,
            receipt_id=receipt_id,
        )
        if verification["terminal_sha256"] != _digest(terminal_raw):
            fail("runtime-compose-sync-terminal-verification-failed")
        return {
            "disposition": "committed",
            "reservation_sha256": _digest(reservation_raw),
            "schema": RESULT_SCHEMA,
            "terminal_sha256": _digest(terminal_raw),
            "transaction_id": transaction_id,
            "version": 2,
            "workflow_sha": reservation_payload["workflow_sha"],
        }
    finally:
        os.close(lock)


def recover_runtime_compose(
    *,
    now: int | None = None,
) -> dict[str, Any]:
    current = int(time.time()) if now is None else now
    if type(current) is not int or isinstance(current, bool) or current < 1:
        fail("runtime-compose-sync-time-invalid")
    if os.geteuid() != 1000 or os.getegid() != 1000:
        fail("runtime-compose-sync-identity-invalid")
    _validate_target_paths()
    lock = _acquire_lock()
    try:
        materialize = _materialize_module()
        (
            _package_anchor,
            _package_private,
            _package_public,
            receipt_private,
            receipt_public,
            _package_id,
            receipt_id,
        ) = materialize._load_authority(os.fspath(RECEIPT_AUTHORITY_ROOT))
        _ensure_sync_root()
        recovered = _recover_pending(
            receipt_private=receipt_private,
            receipt_public=receipt_public,
            receipt_id=receipt_id,
            now=current,
        )
        return {
            "committed_transactions": recovered["committed"],
            "disposition": "recovered",
            "rolled_back_transactions": recovered["rolled_back"],
            "schema": RECOVERY_RESULT_SCHEMA,
            "version": 2,
        }
    finally:
        os.close(lock)


def verify_for_materialization(
    *,
    reservation_raw: bytes,
    reservation_payload: dict[str, Any],
    reservation_binding: dict[str, Any],
    receipt_public,
    receipt_id: str,
) -> dict[str, Any]:
    if (
        not isinstance(reservation_raw, bytes)
        or not isinstance(reservation_payload, dict)
        or not isinstance(reservation_binding, dict)
        or not isinstance(receipt_id, str)
        or SHA256_PATTERN.fullmatch(receipt_id) is None
    ):
        fail("runtime-compose-sync-verification-input-invalid")
    _validate_target_paths()
    attempts = _attempt_directories()
    if not attempts:
        fail("runtime-compose-sync-committed-missing")
    source_blobs = _source_blobs(reservation_payload)
    reservation_digest = _digest(reservation_raw)
    matching: list[tuple[bytes, dict[str, Any], dict[str, Any]]] = []
    for attempt in attempts:
        intent_raw, intent, terminal_raw, terminal = _attempt_records(
            attempt,
            receipt_public=receipt_public,
            receipt_id=receipt_id,
            allow_pending=False,
        )
        del intent_raw
        if terminal_raw is None or terminal is None:
            fail("runtime-compose-sync-terminal-invalid")
        _require_no_stages(intent)
        if (
            intent["reservation_sha256"] != reservation_digest
            or terminal["disposition"] != "committed"
        ):
            continue
        if (
            intent["workflow_sha"] != reservation_payload.get("workflow_sha")
            or intent["runner_label"] != reservation_payload.get("runner_label")
            or intent["source_checkout_identity_sha256"]
            != reservation_binding.get("source_checkout_identity_sha256")
            or intent["source_tree_sha256"]
            != reservation_binding.get("source_tree_sha256")
        ):
            fail("runtime-compose-sync-reservation-rebound")
        backups, candidates = _payload_files(intent)
        if any(value is None for value in backups + candidates):
            fail("runtime-compose-sync-private-payload-invalid")
        for (source_path, _target), entry, candidate in zip(
            COMPOSE_FILES, intent["files"], candidates, strict=True
        ):
            if (
                candidate != source_blobs[source_path]
                or entry["new_sha256"] != _digest(source_blobs[source_path])
                or entry["new_size"] != len(source_blobs[source_path])
                or _target_state(entry)[0] != "new"
            ):
                fail("runtime-compose-sync-live-source-mismatch")
        matching.append((terminal_raw, intent, terminal))
    if len(matching) != 1:
        fail(
            "runtime-compose-sync-committed-missing"
            if not matching
            else "runtime-compose-sync-committed-ambiguous"
        )
    terminal_raw, intent, _terminal = matching[0]
    return {
        "reservation_sha256": reservation_digest,
        "schema": "propertyquarry.release-control.single-host-runtime-compose-sync-verify-result.v2",
        "terminal_sha256": _digest(terminal_raw),
        "transaction_id": intent["transaction_id"],
        "version": 2,
        "workflow_sha": intent["workflow_sha"],
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync", allow_abbrev=False)
    sync.add_argument("--expected-property-sha256", required=True)
    sync.add_argument("--expected-cloudflared-sha256", required=True)
    commands.add_parser("recover", allow_abbrev=False)
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "sync":
            result = sync_runtime_compose(
                expected_property_sha256=arguments.expected_property_sha256,
                expected_cloudflared_sha256=(
                    arguments.expected_cloudflared_sha256
                ),
            )
        else:
            result = recover_runtime_compose()
        sys.stdout.buffer.write(package.canonical_json(result) + b"\n")
        return 0
    except (ComposeSyncFailure, package.PackageFailure) as error:
        sys.stderr.write(f"propertyquarry-runtime-compose-sync-rejected:{error}\n")
        return 50
    except KeyboardInterrupt:
        sys.stderr.write(
            "propertyquarry-runtime-compose-sync-rejected:interrupted\n"
        )
        return 50
    except Exception:
        sys.stderr.write(
            "propertyquarry-runtime-compose-sync-rejected:internal-failure\n"
        )
        return 50


if __name__ == "__main__":
    raise SystemExit(main())
