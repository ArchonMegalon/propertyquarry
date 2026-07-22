#!/usr/bin/env python3
"""Create the encrypted, release-bound PropertyQuarry pre-deploy backup.

The production command is intentionally narrow.  It writes no plaintext
artifact to disk: database dumps, role dumps, and tar streams are encrypted in
bounded chunks as they are produced.  Every encrypted artifact is decrypted
and structurally validated before the pCloud candidate directory is fsynced and
renamed into its immutable final path.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import BinaryIO, Callable, Mapping, Sequence

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


RECEIPT_SCHEMA = "propertyquarry.predeploy-backup-receipt.v2"
REMOTE_MANIFEST_SCHEMA = "propertyquarry.predeploy-backup-remote-manifest.v2"
ENCRYPTED_STREAM_SCHEMA = "propertyquarry.encrypted-backup-stream.v2"
RECEIPT_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-predeploy-backup-receipt-signature.v2\x00"
)
ENCRYPTED_STREAM_MAGIC = b"PQBACKUPV2\x00"
ENCRYPTED_STREAM_DATA_DOMAIN = b"propertyquarry.backup-stream.data.v2\x00"
ENCRYPTED_STREAM_FOOTER_DOMAIN = b"propertyquarry.backup-stream.footer.v2\x00"
ENCRYPTED_STREAM_KDF_INFO = b"propertyquarry.backup-stream.key.v2\x00"
CHUNK_SIZE = 4 * 1024 * 1024
MAX_HEADER_BYTES = 64 * 1024
MAX_FOOTER_BYTES = 64 * 1024
MAX_CAPTURE_BYTES = 8 * 1024 * 1024

INSTALL_ROOT = Path("/etc/propertyquarry-release-single-host-v2")
AUTHORITY_PATH = INSTALL_ROOT / "authority.v2.json"
AUTHORITY_SIGNATURE_PATH = INSTALL_ROOT / "authority.v2.sig"
TRANSACTION_PLAN_PATH = INSTALL_ROOT / "transaction-plan.v2.json"
PACKAGE_MANIFEST_PATH = INSTALL_ROOT / "package-manifest.v2.json"
PACKAGE_MANIFEST_SIGNATURE_PATH = INSTALL_ROOT / "package-manifest.v2.sig"
RECEIPT_PRIVATE_KEY_PATH = INSTALL_ROOT / "receipt-authority-v2.key"
RECEIPT_PUBLIC_KEY_PATH = INSTALL_ROOT / "receipt-authority-v2.pem"
RECEIPT_ROOT = Path(
    "/var/lib/propertyquarry-release-single-host-v2/backup-receipts"
)
REMOTE_ROOT = Path("/mnt/pcloud/propertyquarry/releases/backups/v2")
REMOTE_DIRECTORY_MODE = 0o775
REMOTE_DIRECTORY_NLINK = 1
REMOTE_FILE_MODE = 0o664
REMOTE_UID = 1000
REMOTE_GID = 1000
EXPECTED_ENCRYPTION_KEY_PATH = Path(
    "/home/tibor/.local/share/propertyquarry-backup-keys/"
    "propertyquarry-predeploy-backup-v2.key"
)
MACHINE_ID_PATH = Path("/etc/machine-id")

DOCKER_BIN = "/usr/bin/docker"
TAR_BIN = "/usr/bin/tar"
DATABASE_CONTAINER = "propertyquarry-db-live"
DATABASE_NAME = "propertyquarry"
DATABASE_USER = "postgres"

RUNTIME_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_RE = re.compile(r"^[a-z0-9./_-]+@sha256:[0-9a-f]{64}$")
KEY_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
KEY_FILE_RE = re.compile(rb"^[0-9a-f]{64}\n$")


class BackupError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ArtifactSpec:
    name: str
    kind: str
    producer: tuple[str, ...]
    verification: str
    coverage: tuple[str, ...]
    required_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class BackupPaths:
    install_root: Path = INSTALL_ROOT
    receipt_root: Path = RECEIPT_ROOT
    remote_root: Path = REMOTE_ROOT
    machine_id: Path = MACHINE_ID_PATH
    receipt_private_key: Path = RECEIPT_PRIVATE_KEY_PATH
    receipt_public_key: Path = RECEIPT_PUBLIC_KEY_PATH

    @property
    def authority(self) -> Path:
        return self.install_root / "authority.v2.json"

    @property
    def authority_signature(self) -> Path:
        return self.install_root / "authority.v2.sig"

    @property
    def transaction_plan(self) -> Path:
        return self.install_root / "transaction-plan.v2.json"

    @property
    def package_manifest(self) -> Path:
        return self.install_root / "package-manifest.v2.json"

    @property
    def package_manifest_signature(self) -> Path:
        return self.install_root / "package-manifest.v2.sig"


@dataclass(frozen=True)
class BackupRequest:
    runtime_sha: str
    envelope_sha: str
    web_image: str
    render_image: str
    receipt_path: Path
    encryption_key_path: Path


@dataclass(frozen=True)
class ProcessCapture:
    data: bytes
    total_bytes: int
    line_count: int


class _CaptureThread:
    def __init__(self, stream: BinaryIO, *, limit: int = MAX_CAPTURE_BYTES) -> None:
        self._stream = stream
        self._limit = limit
        self._buffer = bytearray()
        self.total_bytes = 0
        self.line_count = 0
        self._error: Exception | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            while True:
                chunk = self._stream.read(64 * 1024)
                if not chunk:
                    break
                self.total_bytes += len(chunk)
                self.line_count += chunk.count(b"\n")
                remaining = self._limit - len(self._buffer)
                if remaining > 0:
                    self._buffer.extend(chunk[:remaining])
        except Exception as exc:  # pragma: no cover - defensive pipe guard
            self._error = exc

    def start(self) -> None:
        self._thread.start()

    def finish(self) -> ProcessCapture:
        self._thread.join()
        if self._error is not None:
            raise BackupError("process_capture_failed", self._error.__class__.__name__)
        return ProcessCapture(
            bytes(self._buffer),
            self.total_bytes,
            self.line_count,
        )


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _read_exact(stream: BinaryIO, length: int) -> bytes:
    if length < 0:
        raise BackupError("encrypted_stream_length_invalid")
    chunks = bytearray()
    while len(chunks) < length:
        chunk = stream.read(length - len(chunks))
        if not chunk:
            raise BackupError("encrypted_stream_truncated")
        chunks.extend(chunk)
    return bytes(chunks)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _lstat_regular(
    path: Path,
    *,
    mode: int | None = None,
    uid: int | None = None,
    gid: int | None = None,
    nlink: int = 1,
) -> os.stat_result:
    try:
        observed = path.lstat()
    except FileNotFoundError as exc:
        raise BackupError("required_file_missing", str(path)) from exc
    if not stat.S_ISREG(observed.st_mode) or stat.S_ISLNK(observed.st_mode):
        raise BackupError("required_file_not_regular", str(path))
    if observed.st_nlink != nlink:
        raise BackupError("required_file_link_count_invalid", str(path))
    if mode is not None and stat.S_IMODE(observed.st_mode) != mode:
        raise BackupError("required_file_mode_invalid", str(path))
    if uid is not None and observed.st_uid != uid:
        raise BackupError("required_file_uid_invalid", str(path))
    if gid is not None and observed.st_gid != gid:
        raise BackupError("required_file_gid_invalid", str(path))
    return observed


def _read_regular_nofollow(path: Path, *, max_bytes: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
            raise BackupError("required_file_metadata_invalid", str(path))
        if observed.st_size < 1 or observed.st_size > max_bytes:
            raise BackupError("required_file_size_invalid", str(path))
        payload = bytearray()
        while len(payload) <= max_bytes:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > max_bytes:
            raise BackupError("required_file_size_invalid", str(path))
        return bytes(payload)
    finally:
        os.close(descriptor)


def _load_or_create_encryption_key(
    path: Path,
    *,
    expected_parent_uid: int = 1000,
    expected_parent_gid: int = 1000,
    expected_file_uid: int = 1000,
    expected_file_gid: int = 1000,
) -> tuple[bytes, str, bool]:
    parent = path.parent
    try:
        parent_stat = parent.lstat()
    except FileNotFoundError as exc:
        raise BackupError("encryption_key_parent_missing", str(parent)) from exc
    if not stat.S_ISDIR(parent_stat.st_mode) or stat.S_ISLNK(parent_stat.st_mode):
        raise BackupError("encryption_key_parent_invalid", str(parent))
    if stat.S_IMODE(parent_stat.st_mode) != 0o700:
        raise BackupError("encryption_key_parent_mode_invalid", str(parent))
    if (
        parent_stat.st_uid != expected_parent_uid
        or parent_stat.st_gid != expected_parent_gid
    ):
        raise BackupError("encryption_key_parent_owner_invalid", str(parent))

    created = False
    if not path.exists():
        encoded = secrets.token_bytes(32).hex().encode("ascii") + b"\n"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            descriptor = -1
        if descriptor >= 0:
            try:
                os.fchmod(descriptor, 0o600)
                os.fchown(descriptor, expected_file_uid, expected_file_gid)
                written = 0
                while written < len(encoded):
                    written += os.write(descriptor, encoded[written:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            _fsync_directory(parent)
            created = True

    _lstat_regular(
        path,
        mode=0o600,
        uid=expected_file_uid,
        gid=expected_file_gid,
    )
    encoded = _read_regular_nofollow(path, max_bytes=65)
    if not KEY_FILE_RE.fullmatch(encoded):
        raise BackupError("encryption_key_format_invalid", str(path))
    key = bytes.fromhex(encoded.decode("ascii").strip())
    return key, "sha256:" + _sha256_bytes(key), created


def _derive_artifact_key(master_key: bytes, *, salt: bytes, name: str) -> bytes:
    if len(master_key) != 32 or len(salt) != 16:
        raise BackupError("encryption_key_material_invalid")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=ENCRYPTED_STREAM_KDF_INFO + name.encode("utf-8"),
    ).derive(master_key)


def _write_hashed(stream: BinaryIO, digest: object, payload: bytes) -> int:
    stream.write(payload)
    digest.update(payload)  # type: ignore[attr-defined]
    return len(payload)


def encrypt_stream(
    plaintext: BinaryIO,
    destination: Path,
    *,
    master_key: bytes,
    runtime_sha: str,
    artifact_name: str,
    artifact_kind: str,
) -> dict[str, object]:
    salt = secrets.token_bytes(16)
    nonce_prefix = secrets.token_bytes(8)
    header = {
        "artifact_kind": artifact_kind,
        "artifact_name": artifact_name,
        "chunk_size": CHUNK_SIZE,
        "cipher": "AES-256-GCM",
        "kdf": "HKDF-SHA256",
        "nonce_prefix": nonce_prefix.hex(),
        "runtime_sha": runtime_sha,
        "salt": salt.hex(),
        "schema": ENCRYPTED_STREAM_SCHEMA,
    }
    header_bytes = _canonical_json_bytes(header)
    if len(header_bytes) > MAX_HEADER_BYTES:
        raise BackupError("encrypted_stream_header_too_large")
    header_digest = hashlib.sha256(header_bytes).digest()
    cipher = AESGCM(
        _derive_artifact_key(master_key, salt=salt, name=artifact_name)
    )
    ciphertext_digest = hashlib.sha256()
    plaintext_digest = hashlib.sha256()
    plaintext_bytes = 0
    ciphertext_bytes = 0
    chunks = 0

    descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = -1
            prefix = ENCRYPTED_STREAM_MAGIC + struct.pack(">I", len(header_bytes))
            ciphertext_bytes += _write_hashed(output, ciphertext_digest, prefix)
            ciphertext_bytes += _write_hashed(
                output,
                ciphertext_digest,
                header_bytes,
            )
            while True:
                chunk = plaintext.read(CHUNK_SIZE)
                if not chunk:
                    break
                if chunks >= (2**32 - 1):
                    raise BackupError("encrypted_stream_chunk_limit_exceeded")
                plaintext_digest.update(chunk)
                plaintext_bytes += len(chunk)
                record = b"D" + struct.pack(">II", chunks, len(chunk))
                nonce = nonce_prefix + struct.pack(">I", chunks)
                encrypted = cipher.encrypt(
                    nonce,
                    chunk,
                    ENCRYPTED_STREAM_DATA_DOMAIN + header_digest + record,
                )
                ciphertext_bytes += _write_hashed(
                    output,
                    ciphertext_digest,
                    record + encrypted,
                )
                chunks += 1
            footer = {
                "chunk_count": chunks,
                "plaintext_bytes": plaintext_bytes,
                "plaintext_sha256": plaintext_digest.hexdigest(),
            }
            footer_bytes = _canonical_json_bytes(footer)
            footer_record = b"F" + struct.pack(">II", chunks, len(footer_bytes))
            footer_nonce = nonce_prefix + struct.pack(">I", chunks)
            encrypted_footer = cipher.encrypt(
                footer_nonce,
                footer_bytes,
                ENCRYPTED_STREAM_FOOTER_DOMAIN
                + header_digest
                + footer_record,
            )
            ciphertext_bytes += _write_hashed(
                output,
                ciphertext_digest,
                footer_record + encrypted_footer,
            )
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    return {
        "chunk_count": chunks,
        "ciphertext_bytes": ciphertext_bytes,
        "ciphertext_sha256": ciphertext_digest.hexdigest(),
        "plaintext_bytes": plaintext_bytes,
        "plaintext_sha256": plaintext_digest.hexdigest(),
    }


def decrypt_stream(
    source: Path,
    sink: Callable[[bytes], None],
    *,
    master_key: bytes,
    expected_runtime_sha: str,
    expected_artifact_name: str,
    expected_artifact_kind: str,
) -> dict[str, object]:
    ciphertext_digest = hashlib.sha256()
    ciphertext_bytes = 0
    plaintext_digest = hashlib.sha256()
    plaintext_bytes = 0
    expected_counter = 0

    with source.open("rb") as stream:
        prefix = _read_exact(stream, len(ENCRYPTED_STREAM_MAGIC) + 4)
        ciphertext_digest.update(prefix)
        ciphertext_bytes += len(prefix)
        if prefix[: len(ENCRYPTED_STREAM_MAGIC)] != ENCRYPTED_STREAM_MAGIC:
            raise BackupError("encrypted_stream_magic_invalid")
        header_length = struct.unpack(">I", prefix[-4:])[0]
        if header_length < 2 or header_length > MAX_HEADER_BYTES:
            raise BackupError("encrypted_stream_header_length_invalid")
        header_bytes = _read_exact(stream, header_length)
        ciphertext_digest.update(header_bytes)
        ciphertext_bytes += len(header_bytes)
        try:
            header = json.loads(header_bytes)
        except (TypeError, ValueError) as exc:
            raise BackupError("encrypted_stream_header_invalid") from exc
        expected_header = {
            "artifact_kind": expected_artifact_kind,
            "artifact_name": expected_artifact_name,
            "chunk_size": CHUNK_SIZE,
            "cipher": "AES-256-GCM",
            "kdf": "HKDF-SHA256",
            "runtime_sha": expected_runtime_sha,
            "schema": ENCRYPTED_STREAM_SCHEMA,
        }
        for key, value in expected_header.items():
            if header.get(key) != value:
                raise BackupError("encrypted_stream_header_binding_invalid", key)
        try:
            salt = bytes.fromhex(str(header["salt"]))
            nonce_prefix = bytes.fromhex(str(header["nonce_prefix"]))
        except (KeyError, ValueError) as exc:
            raise BackupError("encrypted_stream_header_key_material_invalid") from exc
        if len(salt) != 16 or len(nonce_prefix) != 8:
            raise BackupError("encrypted_stream_header_key_material_invalid")
        header_digest = hashlib.sha256(header_bytes).digest()
        cipher = AESGCM(
            _derive_artifact_key(
                master_key,
                salt=salt,
                name=expected_artifact_name,
            )
        )

        while True:
            record = _read_exact(stream, 9)
            ciphertext_digest.update(record)
            ciphertext_bytes += len(record)
            record_type = record[:1]
            counter, length = struct.unpack(">II", record[1:])
            if counter != expected_counter:
                raise BackupError("encrypted_stream_counter_invalid")
            if record_type == b"D":
                if length < 1 or length > CHUNK_SIZE:
                    raise BackupError("encrypted_stream_chunk_length_invalid")
                encrypted = _read_exact(stream, length + 16)
                ciphertext_digest.update(encrypted)
                ciphertext_bytes += len(encrypted)
                plaintext = cipher.decrypt(
                    nonce_prefix + struct.pack(">I", counter),
                    encrypted,
                    ENCRYPTED_STREAM_DATA_DOMAIN + header_digest + record,
                )
                if len(plaintext) != length:
                    raise BackupError("encrypted_stream_plaintext_length_invalid")
                plaintext_digest.update(plaintext)
                plaintext_bytes += len(plaintext)
                sink(plaintext)
                expected_counter += 1
                continue
            if record_type != b"F":
                raise BackupError("encrypted_stream_record_type_invalid")
            if length < 2 or length > MAX_FOOTER_BYTES:
                raise BackupError("encrypted_stream_footer_length_invalid")
            encrypted_footer = _read_exact(stream, length + 16)
            ciphertext_digest.update(encrypted_footer)
            ciphertext_bytes += len(encrypted_footer)
            footer_bytes = cipher.decrypt(
                nonce_prefix + struct.pack(">I", counter),
                encrypted_footer,
                ENCRYPTED_STREAM_FOOTER_DOMAIN + header_digest + record,
            )
            try:
                footer = json.loads(footer_bytes)
            except (TypeError, ValueError) as exc:
                raise BackupError("encrypted_stream_footer_invalid") from exc
            if stream.read(1):
                raise BackupError("encrypted_stream_trailing_data")
            expected_footer = {
                "chunk_count": expected_counter,
                "plaintext_bytes": plaintext_bytes,
                "plaintext_sha256": plaintext_digest.hexdigest(),
            }
            if footer != expected_footer:
                raise BackupError("encrypted_stream_footer_binding_invalid")
            break

    return {
        "chunk_count": expected_counter,
        "ciphertext_bytes": ciphertext_bytes,
        "ciphertext_sha256": ciphertext_digest.hexdigest(),
        "plaintext_bytes": plaintext_bytes,
        "plaintext_sha256": plaintext_digest.hexdigest(),
    }


def _command_error_detail(capture: ProcessCapture) -> str:
    text = capture.data.decode("utf-8", errors="replace").strip()
    if not text:
        return ""
    return text[-2000:]


def _encrypt_command(
    spec: ArtifactSpec,
    destination: Path,
    *,
    master_key: bytes,
    runtime_sha: str,
) -> dict[str, object]:
    process = subprocess.Popen(
        spec.producer,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    if process.stdout is None or process.stderr is None:
        process.kill()
        raise BackupError("producer_pipe_unavailable", spec.name)
    stderr_capture = _CaptureThread(process.stderr)
    stderr_capture.start()
    try:
        encrypted = encrypt_stream(
            process.stdout,
            destination,
            master_key=master_key,
            runtime_sha=runtime_sha,
            artifact_name=spec.name,
            artifact_kind=spec.kind,
        )
    except Exception:
        process.kill()
        process.wait()
        stderr_capture.finish()
        raise
    finally:
        process.stdout.close()
    return_code = process.wait()
    stderr = stderr_capture.finish()
    if return_code != 0:
        destination.unlink(missing_ok=True)
        raise BackupError(
            "producer_failed",
            f"{spec.name}:{return_code}:{_command_error_detail(stderr)}",
        )
    if int(encrypted["plaintext_bytes"]) <= 0:
        destination.unlink(missing_ok=True)
        raise BackupError("producer_empty", spec.name)
    return encrypted


def _decrypt_to_process(
    source: Path,
    command: Sequence[str],
    *,
    master_key: bytes,
    runtime_sha: str,
    spec: ArtifactSpec,
) -> tuple[dict[str, object], ProcessCapture, ProcessCapture]:
    process = subprocess.Popen(
        tuple(command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    if process.stdin is None or process.stdout is None or process.stderr is None:
        process.kill()
        raise BackupError("verification_pipe_unavailable", spec.name)
    stdout_capture = _CaptureThread(process.stdout)
    stderr_capture = _CaptureThread(process.stderr)
    stdout_capture.start()
    stderr_capture.start()
    try:
        decrypted = decrypt_stream(
            source,
            process.stdin.write,
            master_key=master_key,
            expected_runtime_sha=runtime_sha,
            expected_artifact_name=spec.name,
            expected_artifact_kind=spec.kind,
        )
        process.stdin.close()
        return_code = process.wait()
    except Exception:
        try:
            process.stdin.close()
        except Exception:
            pass
        process.kill()
        process.wait()
        stdout_capture.finish()
        stderr_capture.finish()
        raise
    stdout = stdout_capture.finish()
    stderr = stderr_capture.finish()
    if return_code != 0:
        raise BackupError(
            "artifact_verification_failed",
            f"{spec.name}:{return_code}:{_command_error_detail(stderr)}",
        )
    return decrypted, stdout, stderr


def _verify_artifact(
    source: Path,
    spec: ArtifactSpec,
    *,
    master_key: bytes,
    runtime_sha: str,
) -> tuple[dict[str, object], dict[str, object]]:
    if spec.verification == "pg_restore_list":
        decrypted, stdout, _stderr = _decrypt_to_process(
            source,
            (
                DOCKER_BIN,
                "exec",
                "-i",
                DATABASE_CONTAINER,
                "pg_restore",
                "--list",
            ),
            master_key=master_key,
            runtime_sha=runtime_sha,
            spec=spec,
        )
        listing = stdout.data.decode("utf-8", errors="replace")
        table_data_count = sum(
            1 for line in listing.splitlines() if " TABLE DATA " in line
        )
        if table_data_count < 1:
            raise BackupError("pg_restore_table_data_missing", spec.name)
        return decrypted, {
            "method": "decrypt-pg_restore-list",
            "table_data_entries": table_data_count,
            "toc_lines": stdout.line_count,
        }
    if spec.verification == "tar_gzip_list":
        decrypted, stdout, _stderr = _decrypt_to_process(
            source,
            (TAR_BIN, "--gzip", "--list", "--file", "-"),
            master_key=master_key,
            runtime_sha=runtime_sha,
            spec=spec,
        )
        names = [line.strip() for line in stdout.data.decode("utf-8", errors="replace").splitlines()]
        if stdout.line_count < 1 or not any(names):
            raise BackupError("tar_archive_empty", spec.name)
        if any(
            name.startswith("/")
            or name == ".."
            or name.startswith("../")
            or "/../" in name
            for name in names
        ):
            raise BackupError("tar_archive_path_unsafe", spec.name)
        return decrypted, {
            "entries": stdout.line_count,
            "method": "decrypt-tar-gzip-list",
        }
    if spec.verification == "roles_sql":
        captured = bytearray()

        def sink(chunk: bytes) -> None:
            remaining = MAX_CAPTURE_BYTES - len(captured)
            if remaining > 0:
                captured.extend(chunk[:remaining])

        decrypted = decrypt_stream(
            source,
            sink,
            master_key=master_key,
            expected_runtime_sha=runtime_sha,
            expected_artifact_name=spec.name,
            expected_artifact_kind=spec.kind,
        )
        text = bytes(captured).decode("utf-8", errors="replace")
        if (
            "PostgreSQL database cluster dump" not in text
            or ("CREATE ROLE" not in text and "ALTER ROLE" not in text)
        ):
            raise BackupError("roles_dump_structure_invalid", spec.name)
        return decrypted, {"method": "decrypt-roles-sql-structure"}
    raise BackupError("verification_method_unsupported", spec.verification)


def _required_source(path: Path, *, expect_directory: bool) -> None:
    try:
        observed = path.lstat()
    except FileNotFoundError as exc:
        raise BackupError("backup_source_missing", str(path)) from exc
    if stat.S_ISLNK(observed.st_mode):
        raise BackupError("backup_source_symlink_forbidden", str(path))
    if expect_directory and not stat.S_ISDIR(observed.st_mode):
        raise BackupError("backup_source_directory_required", str(path))
    if not expect_directory and not stat.S_ISREG(observed.st_mode):
        raise BackupError("backup_source_file_required", str(path))


def production_artifact_specs() -> tuple[ArtifactSpec, ...]:
    volume_sources = (
        (
            "provider-ledger",
            Path("/var/lib/docker/volumes/property_propertyquarry_provider_ledger/_data"),
        ),
        (
            "artifacts",
            Path("/var/lib/docker/volumes/property_propertyquarry_artifacts/_data"),
        ),
        (
            "governed-render-consents",
            Path(
                "/var/lib/docker/volumes/"
                "property_propertyquarry_governed_render_consents/_data"
            ),
        ),
        (
            "public-tours",
            Path("/var/lib/docker/volumes/property_propertyquarry_public_tours/_data"),
        ),
    )
    specs: list[ArtifactSpec] = [
        ArtifactSpec(
            "database",
            "postgres-custom",
            (
                DOCKER_BIN,
                "exec",
                DATABASE_CONTAINER,
                "pg_dump",
                "--format=custom",
                "--compress=6",
                "--no-owner",
                "--no-acl",
                f"--username={DATABASE_USER}",
                f"--dbname={DATABASE_NAME}",
            ),
            "pg_restore_list",
            (DATABASE_NAME,),
        ),
        ArtifactSpec(
            "roles",
            "postgres-roles-sql",
            (
                DOCKER_BIN,
                "exec",
                DATABASE_CONTAINER,
                "pg_dumpall",
                "--roles-only",
                f"--username={DATABASE_USER}",
            ),
            "roles_sql",
            ("postgres-cluster-roles",),
        ),
    ]
    for name, source in volume_sources:
        specs.append(
            ArtifactSpec(
                f"volume-{name}",
                "tar-gzip",
                (
                    TAR_BIN,
                    "--gzip",
                    "--create",
                    "--file",
                    "-",
                    "--format=pax",
                    "--numeric-owner",
                    "--xattrs",
                    "--acls",
                    "--one-file-system",
                    "--warning=no-file-changed",
                    "--directory",
                    str(source),
                    ".",
                ),
                "tar_gzip_list",
                (str(source),),
                (source,),
            )
        )
    for name, source in (
        ("config", Path("/docker/property/config")),
        (
            "incoming-property-tours",
            Path("/docker/property/state/incoming_property_tours"),
        ),
    ):
        specs.append(
            ArtifactSpec(
                f"bind-{name}",
                "tar-gzip",
                (
                    TAR_BIN,
                    "--gzip",
                    "--create",
                    "--file",
                    "-",
                    "--format=pax",
                    "--numeric-owner",
                    "--xattrs",
                    "--acls",
                    "--one-file-system",
                    "--warning=no-file-changed",
                    "--directory",
                    str(source),
                    ".",
                ),
                "tar_gzip_list",
                (str(source),),
                (source,),
            )
        )
    runtime_files = (
        Path("/docker/property/.env"),
        Path(
            "/docker/property/state/runtime/"
            "property_scene_video_shared.env"
        ),
        Path(
            "/docker/property/state/runtime/"
            "propertyquarry_database_roles.env"
        ),
        Path(
            "/docker/property/state/runtime/"
            "propertyquarry_admission.env"
        ),
        Path("/docker/property/state/runtime/propertyquarry_google_identity.env"),
        Path(
            "/docker/property/state/runtime/"
            "propertyquarry_registration_email.env"
        ),
    )
    specs.append(
        ArtifactSpec(
            "runtime-identity-config",
            "tar-gzip",
            (
                TAR_BIN,
                "--gzip",
                "--create",
                "--file",
                "-",
                "--format=pax",
                "--numeric-owner",
                "--one-file-system",
                "--directory",
                "/",
                *(str(path).lstrip("/") for path in runtime_files),
            ),
            "tar_gzip_list",
            tuple(str(path) for path in runtime_files),
            runtime_files,
        )
    )
    return tuple(specs)


def _load_json_file(path: Path) -> tuple[dict[str, object], bytes, str]:
    payload = _read_regular_nofollow(path, max_bytes=8 * 1024 * 1024)
    try:
        decoded = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise BackupError("installed_json_invalid", str(path)) from exc
    if not isinstance(decoded, dict):
        raise BackupError("installed_json_object_required", str(path))
    return decoded, payload, _sha256_bytes(payload)


def _installed_bindings(
    paths: BackupPaths,
    request: BackupRequest,
    *,
    receipt_key_id: str,
    require_root_owner: bool = True,
) -> dict[str, object]:
    installed = (
        paths.authority,
        paths.authority_signature,
        paths.transaction_plan,
        paths.package_manifest,
        paths.package_manifest_signature,
    )
    for path in installed:
        _lstat_regular(
            path,
            uid=0 if require_root_owner else None,
            gid=0 if require_root_owner else None,
        )
    authority, authority_raw, authority_digest = _load_json_file(paths.authority)
    plan, plan_raw, plan_digest = _load_json_file(paths.transaction_plan)
    manifest, manifest_raw, manifest_digest = _load_json_file(paths.package_manifest)
    authority_signature = _read_regular_nofollow(
        paths.authority_signature,
        max_bytes=64 * 1024,
    )
    manifest_signature = _read_regular_nofollow(
        paths.package_manifest_signature,
        max_bytes=64 * 1024,
    )
    if authority.get("schema") != "propertyquarry.release-control.single-host-profile.v2":
        raise BackupError("installed_authority_schema_invalid")
    if plan.get("schema") != "propertyquarry.release-control.single-host-transaction-plan.v2":
        raise BackupError("installed_plan_schema_invalid")
    if manifest.get("schema") != "propertyquarry.release-control.single-host-package.v2":
        raise BackupError("installed_manifest_schema_invalid")
    for document_name, document, required in (
        (
            "authority",
            authority,
            {
                "runtime_sha": request.runtime_sha,
                "envelope_sha": request.envelope_sha,
                "web_image": request.web_image,
                "render_image": request.render_image,
            },
        ),
        (
            "plan",
            plan,
            {
                "runtime_sha": request.runtime_sha,
                "envelope_sha": request.envelope_sha,
                "web_image": request.web_image,
                "render_image": request.render_image,
            },
        ),
        (
            "manifest",
            manifest,
            {
                "runtime_sha": request.runtime_sha,
                "envelope_sha": request.envelope_sha,
            },
        ),
    ):
        for field_name, expected in required.items():
            if document.get(field_name) != expected:
                raise BackupError(
                    "installed_binding_invalid",
                    f"{document_name}:{field_name}",
                )
    package_key_id = str(authority.get("package_authority_key_id") or "")
    if not KEY_ID_RE.fullmatch(package_key_id):
        raise BackupError("package_authority_key_id_invalid")
    if manifest.get("package_authority_key_id") != package_key_id:
        raise BackupError("installed_package_authority_key_id_mismatch")
    if authority.get("receipt_authority_key_id") != receipt_key_id:
        raise BackupError("installed_receipt_authority_key_id_mismatch")
    authority_digest_id = f"sha256:{authority_digest}"
    plan_digest_id = f"sha256:{plan_digest}"
    if authority.get("plan_digest") != plan_digest_id:
        raise BackupError("installed_authority_plan_digest_mismatch")
    if manifest.get("config_digest") != authority_digest_id:
        raise BackupError("installed_manifest_config_digest_mismatch")
    if manifest.get("plan_digest") != plan_digest_id:
        raise BackupError("installed_manifest_plan_digest_mismatch")
    return {
        "authority_digest": authority_digest_id,
        "authority_signature_digest": "sha256:"
        + _sha256_bytes(authority_signature),
        "config_digest": authority_digest_id,
        "package_authority_key_id": package_key_id,
        "package_manifest_digest": f"sha256:{manifest_digest}",
        "package_manifest_signature_digest": "sha256:"
        + _sha256_bytes(manifest_signature),
        "plan_digest": plan_digest_id,
    }


def _load_receipt_authority(
    paths: BackupPaths,
    *,
    require_root_owner: bool = True,
) -> tuple[Ed25519PrivateKey, Ed25519PublicKey, str]:
    expected_uid = 0 if require_root_owner else None
    expected_gid = 0 if require_root_owner else None
    _lstat_regular(
        paths.receipt_private_key,
        mode=0o400,
        uid=expected_uid,
        gid=expected_gid,
    )
    _lstat_regular(
        paths.receipt_public_key,
        mode=0o444,
        uid=expected_uid,
        gid=expected_gid,
    )
    private_bytes = _read_regular_nofollow(
        paths.receipt_private_key,
        max_bytes=64 * 1024,
    )
    public_bytes = _read_regular_nofollow(
        paths.receipt_public_key,
        max_bytes=64 * 1024,
    )
    try:
        private = serialization.load_pem_private_key(private_bytes, password=None)
        public = serialization.load_pem_public_key(public_bytes)
    except (TypeError, ValueError) as exc:
        raise BackupError("receipt_authority_pem_invalid") from exc
    if not isinstance(private, Ed25519PrivateKey) or not isinstance(
        public,
        Ed25519PublicKey,
    ):
        raise BackupError("receipt_authority_algorithm_invalid")
    expected_public = private.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    observed_public = public.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if not secrets.compare_digest(expected_public, observed_public):
        raise BackupError("receipt_authority_keypair_mismatch")
    return private, public, "sha256:" + _sha256_bytes(observed_public)


def _sign_receipt(
    payload: Mapping[str, object],
    private: Ed25519PrivateKey,
    key_id: str,
) -> dict[str, object]:
    payload_object = dict(payload)
    payload_bytes = _canonical_json_bytes(payload_object)
    signature_input = (
        RECEIPT_SIGNATURE_DOMAIN
        + len(payload_bytes).to_bytes(8, byteorder="big", signed=False)
        + payload_bytes
    )
    signature = private.sign(signature_input)
    return {
        "payload": payload_object,
        "signature": base64.urlsafe_b64encode(signature).decode("ascii").rstrip("="),
        "signature_key_id": key_id,
    }


def _verify_receipt_wrapper(
    wrapper: object,
    public: Ed25519PublicKey,
    key_id: str,
) -> dict[str, object]:
    if not isinstance(wrapper, dict) or set(wrapper) != {
        "payload",
        "signature",
        "signature_key_id",
    }:
        raise BackupError("receipt_wrapper_invalid")
    if wrapper.get("signature_key_id") != key_id:
        raise BackupError("receipt_signature_metadata_invalid")
    payload = wrapper.get("payload")
    if not isinstance(payload, dict):
        raise BackupError("receipt_payload_invalid")
    payload_bytes = _canonical_json_bytes(payload)
    signature_text = str(wrapper.get("signature") or "")
    try:
        signature = base64.urlsafe_b64decode(
            signature_text + "=" * (-len(signature_text) % 4)
        )
        public.verify(
            signature,
            RECEIPT_SIGNATURE_DOMAIN
            + len(payload_bytes).to_bytes(8, byteorder="big", signed=False)
            + payload_bytes,
        )
    except Exception as exc:
        raise BackupError("receipt_signature_invalid") from exc
    return dict(payload)


def _write_exclusive_receipt(
    path: Path,
    wrapper: Mapping[str, object],
    *,
    uid: int = 0,
    gid: int = 0,
) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    encoded = _canonical_json_bytes(dict(wrapper)) + b"\n"
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, uid, gid)
        written = 0
        while written < len(encoded):
            written += os.write(descriptor, encoded[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _write_remote_manifest(path: Path, manifest: Mapping[str, object]) -> str:
    encoded = _canonical_json_bytes(dict(manifest)) + b"\n"
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        written = 0
        while written < len(encoded):
            written += os.write(descriptor, encoded[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return "sha256:" + _sha256_bytes(encoded)


def _load_remote_manifest(path: Path) -> tuple[dict[str, object], str]:
    encoded = _read_regular_nofollow(path, max_bytes=16 * 1024 * 1024)
    try:
        manifest = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise BackupError("remote_manifest_invalid") from exc
    if not isinstance(manifest, dict):
        raise BackupError("remote_manifest_object_required")
    if manifest.get("schema") != REMOTE_MANIFEST_SCHEMA:
        raise BackupError("remote_manifest_schema_invalid")
    return manifest, "sha256:" + _sha256_bytes(encoded)


def _validate_remote_directory_shape(
    final_path: Path,
    specs: Sequence[ArtifactSpec],
) -> None:
    try:
        observed = final_path.lstat()
    except FileNotFoundError as exc:
        raise BackupError("remote_final_missing", str(final_path)) from exc
    if not stat.S_ISDIR(observed.st_mode) or stat.S_ISLNK(observed.st_mode):
        raise BackupError("remote_final_directory_invalid", str(final_path))
    if (
        stat.S_IMODE(observed.st_mode) != REMOTE_DIRECTORY_MODE
        or observed.st_uid != REMOTE_UID
        or observed.st_gid != REMOTE_GID
        or observed.st_nlink != REMOTE_DIRECTORY_NLINK
    ):
        raise BackupError("remote_final_directory_metadata_invalid", str(final_path))
    expected_names = {"manifest.v2.json"} | {
        f"{spec.name}.pqenc" for spec in specs
    }
    observed_names = {entry.name for entry in os.scandir(final_path)}
    if observed_names != expected_names:
        raise BackupError("remote_final_shape_invalid", str(final_path))
    _lstat_regular(
        final_path / "manifest.v2.json",
        mode=REMOTE_FILE_MODE,
        uid=REMOTE_UID,
        gid=REMOTE_GID,
    )


def _recover_remote_final(
    final_path: Path,
    specs: Sequence[ArtifactSpec],
    *,
    request: BackupRequest,
    bindings: Mapping[str, object],
    master_key: bytes,
    encryption_key_id: str,
) -> tuple[list[dict[str, object]], str]:
    _validate_remote_directory_shape(final_path, specs)
    manifest, manifest_digest = _load_remote_manifest(
        final_path / "manifest.v2.json"
    )
    expected_bindings = {
        **bindings,
        "envelope_sha": request.envelope_sha,
        "render_image": request.render_image,
        "runtime_sha": request.runtime_sha,
        "web_image": request.web_image,
    }
    if (
        manifest.get("bindings") != expected_bindings
        or manifest.get("encryption_key_id") != encryption_key_id
        or manifest.get("plaintext_retained") is not False
        or manifest.get("verification_complete") is not True
    ):
        raise BackupError("remote_manifest_binding_invalid")
    raw_artifacts = manifest.get("artifacts")
    if not isinstance(raw_artifacts, list) or len(raw_artifacts) != len(specs):
        raise BackupError("remote_manifest_artifacts_invalid")
    indexed = {
        str(item.get("name") or ""): item
        for item in raw_artifacts
        if isinstance(item, dict)
    }
    if set(indexed) != {spec.name for spec in specs}:
        raise BackupError("remote_manifest_artifact_names_invalid")

    artifacts: list[dict[str, object]] = []
    for spec in specs:
        recorded = indexed[spec.name]
        expected_filename = f"{spec.name}.pqenc"
        if (
            recorded.get("filename") != expected_filename
            or recorded.get("kind") != spec.kind
            or recorded.get("coverage") != list(spec.coverage)
        ):
            raise BackupError("remote_manifest_artifact_binding_invalid", spec.name)
        artifact_path = final_path / expected_filename
        _lstat_regular(
            artifact_path,
            mode=REMOTE_FILE_MODE,
            uid=REMOTE_UID,
            gid=REMOTE_GID,
        )
        ciphertext_sha256, ciphertext_bytes = _sha256_file(artifact_path)
        if (
            recorded.get("ciphertext_sha256") != f"sha256:{ciphertext_sha256}"
            or recorded.get("ciphertext_bytes") != ciphertext_bytes
        ):
            raise BackupError("remote_artifact_ciphertext_mismatch", spec.name)
        decrypted, verification = _verify_artifact(
            artifact_path,
            spec,
            master_key=master_key,
            runtime_sha=request.runtime_sha,
        )
        for key in ("chunk_count", "ciphertext_bytes", "plaintext_bytes"):
            if recorded.get(key) != decrypted.get(key):
                raise BackupError(
                    "remote_artifact_roundtrip_mismatch",
                    f"{spec.name}:{key}",
                )
        for key in ("ciphertext_sha256", "plaintext_sha256"):
            if recorded.get(key) != f"sha256:{decrypted.get(key)}":
                raise BackupError(
                    "remote_artifact_roundtrip_mismatch",
                    f"{spec.name}:{key}",
                )
        if recorded.get("verification") != verification:
            raise BackupError("remote_artifact_verification_mismatch", spec.name)
        artifacts.append(dict(recorded))
    return artifacts, manifest_digest


def _validate_signed_receipt_remote(
    payload: Mapping[str, object],
    final_path: Path,
    specs: Sequence[ArtifactSpec],
) -> None:
    _validate_remote_directory_shape(final_path, specs)
    remote = payload.get("remote")
    artifacts = payload.get("artifacts")
    if not isinstance(remote, dict) or not isinstance(artifacts, list):
        raise BackupError("existing_receipt_remote_invalid")
    _manifest, manifest_digest = _load_remote_manifest(
        final_path / "manifest.v2.json"
    )
    if remote.get("manifest_sha256") != manifest_digest:
        raise BackupError("existing_receipt_manifest_mismatch")
    indexed = {
        str(item.get("name") or ""): item
        for item in artifacts
        if isinstance(item, dict)
    }
    if set(indexed) != {spec.name for spec in specs}:
        raise BackupError("existing_receipt_artifacts_invalid")
    for spec in specs:
        item = indexed[spec.name]
        artifact_path = final_path / f"{spec.name}.pqenc"
        _lstat_regular(
            artifact_path,
            mode=REMOTE_FILE_MODE,
            uid=REMOTE_UID,
            gid=REMOTE_GID,
        )
        ciphertext_sha256, ciphertext_bytes = _sha256_file(artifact_path)
        if (
            item.get("filename") != artifact_path.name
            or item.get("ciphertext_bytes") != ciphertext_bytes
            or item.get("ciphertext_sha256") != f"sha256:{ciphertext_sha256}"
        ):
            raise BackupError("existing_receipt_artifact_digest_mismatch", spec.name)


def _machine_id_digest(path: Path) -> str:
    value = _read_regular_nofollow(path, max_bytes=1024).strip()
    if not value:
        raise BackupError("machine_id_missing")
    return "sha256:" + _sha256_bytes(value)


def _validate_request(request: BackupRequest, paths: BackupPaths) -> None:
    if not RUNTIME_SHA_RE.fullmatch(request.runtime_sha):
        raise BackupError("runtime_sha_invalid")
    if not SHA256_HEX_RE.fullmatch(request.envelope_sha):
        raise BackupError("envelope_sha_invalid")
    if not IMAGE_RE.fullmatch(request.web_image):
        raise BackupError("web_image_invalid")
    if not IMAGE_RE.fullmatch(request.render_image):
        raise BackupError("render_image_invalid")
    expected_receipt = paths.receipt_root / f"{request.runtime_sha}.json"
    if request.receipt_path != expected_receipt:
        raise BackupError("receipt_path_invalid")
    if request.encryption_key_path != EXPECTED_ENCRYPTION_KEY_PATH:
        raise BackupError("encryption_key_path_invalid")


def _receipt_payload_matches(
    payload: Mapping[str, object],
    request: BackupRequest,
    final_path: Path,
) -> bool:
    remote = payload.get("remote")
    return bool(
        payload.get("schema") == RECEIPT_SCHEMA
        and payload.get("runtime_sha") == request.runtime_sha
        and payload.get("envelope_sha") == request.envelope_sha
        and payload.get("web_image") == request.web_image
        and payload.get("render_image") == request.render_image
        and payload.get("disposition") == "verified-and-published"
        and payload.get("plaintext_retained") is False
        and isinstance(remote, dict)
        and remote.get("path") == str(final_path)
        and final_path.is_dir()
    )


def create_backup(
    request: BackupRequest,
    *,
    paths: BackupPaths = BackupPaths(),
    artifact_specs: Sequence[ArtifactSpec] | None = None,
    require_root: bool = True,
    key_owner: tuple[int, int] = (1000, 1000),
    clock: Callable[[], float] = time.time,
) -> dict[str, object]:
    _validate_request(request, paths)
    if require_root and os.geteuid() != 0:
        raise BackupError("root_required")
    for binary in (DOCKER_BIN, TAR_BIN):
        if not Path(binary).is_file() or not os.access(binary, os.X_OK):
            raise BackupError("required_binary_missing", binary)
    if paths.remote_root.resolve() != paths.remote_root:
        raise BackupError("remote_root_not_canonical")
    if require_root and not Path("/mnt/pcloud").is_mount():
        raise BackupError("pcloud_mount_required", "/mnt/pcloud")
    paths.remote_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    final_path = paths.remote_root / request.runtime_sha
    private, public, receipt_key_id = _load_receipt_authority(
        paths,
        require_root_owner=require_root,
    )
    specs = tuple(artifact_specs or production_artifact_specs())
    if not specs or len({spec.name for spec in specs}) != len(specs):
        raise BackupError("artifact_specs_invalid")

    if request.receipt_path.exists():
        _lstat_regular(
            request.receipt_path,
            mode=0o600,
            uid=0 if require_root else None,
            gid=0 if require_root else None,
        )
        raw = _read_regular_nofollow(request.receipt_path, max_bytes=16 * 1024 * 1024)
        try:
            wrapper = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise BackupError("existing_receipt_invalid") from exc
        payload = _verify_receipt_wrapper(wrapper, public, receipt_key_id)
        if not _receipt_payload_matches(payload, request, final_path):
            raise BackupError("existing_receipt_binding_invalid")
        _validate_signed_receipt_remote(payload, final_path, specs)
        return dict(wrapper)

    started = int(clock())
    bindings = _installed_bindings(
        paths,
        request,
        receipt_key_id=receipt_key_id,
        require_root_owner=require_root,
    )
    master_key, encryption_key_id, key_created = _load_or_create_encryption_key(
        request.encryption_key_path,
        expected_parent_uid=key_owner[0],
        expected_parent_gid=key_owner[1],
        expected_file_uid=key_owner[0],
        expected_file_gid=key_owner[1],
    )
    artifacts: list[dict[str, object]] = []
    partial_path: Path | None = None
    remote_manifest_digest = ""
    if final_path.exists():
        artifacts, remote_manifest_digest = _recover_remote_final(
            final_path,
            specs,
            request=request,
            bindings=bindings,
            master_key=master_key,
            encryption_key_id=encryption_key_id,
        )
    else:
        partial_path = paths.remote_root / (
            f".{request.runtime_sha}.partial.{os.getpid()}.{secrets.token_hex(6)}"
        )
        partial_path.mkdir(mode=0o700)
        try:
            for spec in specs:
                for source_path in spec.required_paths:
                    _required_source(
                        source_path,
                        expect_directory=not source_path.name.endswith(".env"),
                    )
                artifact_path = partial_path / f"{spec.name}.pqenc"
                encrypted = _encrypt_command(
                    spec,
                    artifact_path,
                    master_key=master_key,
                    runtime_sha=request.runtime_sha,
                )
                decrypted, verification = _verify_artifact(
                    artifact_path,
                    spec,
                    master_key=master_key,
                    runtime_sha=request.runtime_sha,
                )
                for key in (
                    "ciphertext_bytes",
                    "ciphertext_sha256",
                    "plaintext_bytes",
                    "plaintext_sha256",
                    "chunk_count",
                ):
                    if encrypted.get(key) != decrypted.get(key):
                        raise BackupError(
                            "artifact_roundtrip_mismatch",
                            f"{spec.name}:{key}",
                        )
                artifacts.append(
                    {
                        "chunk_count": int(encrypted["chunk_count"]),
                        "ciphertext_bytes": int(encrypted["ciphertext_bytes"]),
                        "ciphertext_sha256": "sha256:"
                        + str(encrypted["ciphertext_sha256"]),
                        "coverage": list(spec.coverage),
                        "filename": artifact_path.name,
                        "kind": spec.kind,
                        "name": spec.name,
                        "plaintext_bytes": int(encrypted["plaintext_bytes"]),
                        "plaintext_sha256": "sha256:"
                        + str(encrypted["plaintext_sha256"]),
                        "verification": verification,
                    }
                )

            manifest = {
                "artifacts": artifacts,
                "bindings": {
                    **bindings,
                    "envelope_sha": request.envelope_sha,
                    "render_image": request.render_image,
                    "runtime_sha": request.runtime_sha,
                    "web_image": request.web_image,
                },
                "encryption_key_id": encryption_key_id,
                "plaintext_retained": False,
                "schema": REMOTE_MANIFEST_SCHEMA,
                "verification_complete": True,
            }
            remote_manifest_digest = _write_remote_manifest(
                partial_path / "manifest.v2.json",
                manifest,
            )
            _fsync_directory(partial_path)
            os.rename(partial_path, final_path)
            partial_path = None
            _fsync_directory(paths.remote_root)
            _validate_remote_directory_shape(final_path, specs)
        except Exception:
            if partial_path is not None and partial_path.exists():
                shutil.rmtree(partial_path)
                _fsync_directory(paths.remote_root)
            raise

    finished = int(clock())
    coverage = {
        "config": sorted(
            {
                item
                for artifact in artifacts
                for item in artifact["coverage"]
                if str(item).startswith("/docker/property/config")
                or str(item).endswith(".env")
            }
        ),
        "database": [DATABASE_NAME],
        "roles": ["postgres-cluster-roles"],
        "binds": sorted(
            {
                item
                for artifact in artifacts
                for item in artifact["coverage"]
                if str(item).startswith("/docker/property/state/incoming_property_tours")
            }
        ),
        "volumes": sorted(
            {
                item
                for artifact in artifacts
                for item in artifact["coverage"]
                if "/var/lib/docker/volumes/" in str(item)
            }
        ),
    }
    payload = {
        "artifacts": artifacts,
        "atomic_finalize": True,
        **bindings,
        "coverage": coverage,
        "disposition": "verified-and-published",
        "encryption_key_created": key_created,
        "encryption_key_id": encryption_key_id,
        "envelope_sha": request.envelope_sha,
        "finished_at_epoch": finished,
        "fsync_artifacts": True,
        "fsync_directories": True,
        "host_machine_id_digest": _machine_id_digest(paths.machine_id),
        "package_authority_key_id": bindings["package_authority_key_id"],
        "plaintext_retained": False,
        "production_ready": False,
        "receipt_authority_key_id": receipt_key_id,
        "remote": {
            "manifest_sha256": remote_manifest_digest,
            "path": str(final_path),
            "provider": "pcloud-rclone",
            "version": "v2",
        },
        "render_image": request.render_image,
        "runtime_sha": request.runtime_sha,
        "schema": RECEIPT_SCHEMA,
        "started_at_epoch": started,
        "web_image": request.web_image,
    }
    wrapper = _sign_receipt(payload, private, receipt_key_id)
    _write_exclusive_receipt(
        request.receipt_path,
        wrapper,
        uid=0 if require_root else os.getuid(),
        gid=0 if require_root else os.getgid(),
    )
    return wrapper


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--runtime-sha", required=True)
    create.add_argument("--envelope-sha", required=True)
    create.add_argument("--web-image", required=True)
    create.add_argument("--render-image", required=True)
    create.add_argument("--receipt", required=True)
    create.add_argument("--encryption-key", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command != "create":  # pragma: no cover - argparse guard
        return 2
    request = BackupRequest(
        runtime_sha=str(args.runtime_sha or "").strip().lower(),
        envelope_sha=str(args.envelope_sha or "").strip().lower(),
        web_image=str(args.web_image or "").strip().lower(),
        render_image=str(args.render_image or "").strip().lower(),
        receipt_path=Path(args.receipt).expanduser().resolve(),
        encryption_key_path=Path(args.encryption_key).expanduser().resolve(),
    )
    try:
        wrapper = create_backup(request)
    except BackupError as exc:
        print(
            json.dumps(
                {"detail": exc.detail, "reason": exc.code, "status": "failed"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "disposition": wrapper["payload"]["disposition"],
                "receipt": str(request.receipt_path),
                "status": "ok",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
