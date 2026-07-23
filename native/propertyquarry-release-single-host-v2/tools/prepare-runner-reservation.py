#!/usr/bin/env python3
"""Create the one-shot signed reservation used to dispatch a release runner."""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, NoReturn

import fcntl
from cryptography.exceptions import InvalidSignature


TOOLS = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
_MATERIALIZE_SPEC = importlib.util.spec_from_file_location(
    "propertyquarry_single_host_materialize_v2", TOOLS / "materialize.py"
)
if _MATERIALIZE_SPEC is None or _MATERIALIZE_SPEC.loader is None:  # pragma: no cover
    raise RuntimeError("materialize-module-unavailable")
materialize = importlib.util.module_from_spec(_MATERIALIZE_SPEC)
sys.modules[_MATERIALIZE_SPEC.name] = materialize
_MATERIALIZE_SPEC.loader.exec_module(materialize)


RESERVATION_ROOT = Path(
    "/docker/property/state/runtime/propertyquarry-release-authority-v2.private/"
    "single-host-v2-runner-reservation"
)
RESERVATION_NAME = "runner-reservation.v2.json"
RESERVATION_PARENT = RESERVATION_ROOT.parent
RESERVATION_LOCK = RESERVATION_PARENT / ".single-host-v2-runner-reservation.lock"
RESERVATION_STAGE = RESERVATION_PARENT / ".single-host-v2-runner-reservation.preparing.v2"
RESERVATION_TERMINAL_ROOT = RESERVATION_PARENT / "single-host-v2-runner-reservation-terminal"
SOURCE_CHECKOUT_ROOT = RESERVATION_PARENT / "single-host-v2-release-checkouts"
SOURCE_CHECKOUT_STAGE_PREFIX = ".single-host-v2-release-checkout.preparing."
RESERVATION_SCHEMA = (
    "propertyquarry.release-control.single-host-runner-reservation.v2"
)
RESERVATION_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-runner-reservation-signature.v2\0"
)
RUNNER_LABEL_DOMAIN = (
    b"propertyquarry.release-control.single-host-runner-label.v2\0"
)
RESERVATION_TTL_SECONDS = 6 * 60 * 60
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
HEX64_PATTERN = re.compile(r"^[0-9a-f]{64}$")
LABEL_PATTERN = re.compile(r"^pqrelease-[0-9a-f]{32}$")


class ReservationFailure(ValueError):
    """A secret-free reservation rejection."""


def fail(code: str) -> NoReturn:
    raise ReservationFailure(code)


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _exact_metadata(path: Path, *, kind: str, mode: int) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError:
        fail("runner-reservation-path-unavailable")
    expected_kind = stat.S_ISDIR if kind == "directory" else stat.S_ISREG
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not expected_kind(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != mode
        or metadata.st_uid != 1000
        or metadata.st_gid != 1000
        or path.resolve() != path
    ):
        fail("runner-reservation-path-metadata-invalid")
    if kind == "file" and metadata.st_nlink != 1:
        fail("runner-reservation-path-metadata-invalid")
    return metadata


def _directory_sync(path: Path) -> None:
    descriptor = os.open(
        path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _acquire_lock() -> int:
    _exact_metadata(RESERVATION_PARENT, kind="directory", mode=0o700)
    descriptor = os.open(
        RESERVATION_LOCK,
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
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
            fail("runner-reservation-lock-invalid")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail("runner-reservation-busy")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _recover_staging_directories() -> None:
    prefix = ".single-host-v2-runner-reservation.preparing."
    try:
        candidates = sorted(
            item for item in RESERVATION_PARENT.iterdir() if item.name.startswith(prefix)
        )
    except OSError:
        fail("runner-reservation-stage-scan-failed")
    for candidate in candidates:
        _exact_metadata(candidate, kind="directory", mode=0o700)
        shutil.rmtree(candidate)
    if candidates:
        _directory_sync(RESERVATION_PARENT)


def _run_git_bytes(checkout: Path, *arguments: str, allow_nul: bool = False) -> bytes:
    if (
        os.path.realpath("/usr/bin/git") != "/usr/bin/git"
        or os.stat("/usr/bin/git", follow_symlinks=False).st_mode & 0o7777 != 0o755
        or os.stat("/usr/bin/git", follow_symlinks=False).st_uid != 0
        or os.stat("/usr/bin/git", follow_symlinks=False).st_gid != 0
    ):
        fail("runner-reservation-git-invalid")
    try:
        completed = subprocess.run(
            ["/usr/bin/git", "-C", os.fspath(checkout), *arguments],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": "/bin/false",
                "SSH_ASKPASS": "/bin/false",
                "GCM_INTERACTIVE": "never",
                "HOME": "/nonexistent",
                "PATH": "/usr/bin:/bin",
                "LANG": "C",
                "LC_ALL": "C",
                "TZ": "UTC",
            },
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        fail("runner-reservation-checkout-query-failed")
    value = completed.stdout
    if (not allow_nul and b"\x00" in value) or b"\r" in value:
        fail("runner-reservation-checkout-query-invalid")
    return value


def _run_git(checkout: Path, *arguments: str) -> str:
    raw = _run_git_bytes(checkout, *arguments)
    try:
        value = raw.decode("ascii")
    except UnicodeDecodeError:
        fail("runner-reservation-checkout-query-invalid")
    return value.removesuffix("\n")


def _ensure_checkout_root() -> None:
    _exact_metadata(RESERVATION_PARENT, kind="directory", mode=0o700)
    if not SOURCE_CHECKOUT_ROOT.exists():
        try:
            os.mkdir(SOURCE_CHECKOUT_ROOT, 0o700)
            os.chmod(SOURCE_CHECKOUT_ROOT, 0o700)
            _directory_sync(RESERVATION_PARENT)
        except OSError:
            fail("runner-reservation-checkout-root-create-failed")
    _exact_metadata(SOURCE_CHECKOUT_ROOT, kind="directory", mode=0o700)


def _recover_checkout_stages() -> None:
    _ensure_checkout_root()
    try:
        stages = sorted(
            item
            for item in SOURCE_CHECKOUT_ROOT.iterdir()
            if item.name.startswith(SOURCE_CHECKOUT_STAGE_PREFIX)
        )
    except OSError:
        fail("runner-reservation-checkout-stage-scan-failed")
    for stage in stages:
        _exact_metadata(stage, kind="directory", mode=0o700)
        shutil.rmtree(stage)
    if stages:
        _directory_sync(SOURCE_CHECKOUT_ROOT)


def _checkout_identity(checkout: Path, workflow_sha: str) -> dict[str, Any]:
    metadata = _exact_metadata(checkout, kind="directory", mode=0o700)
    value = {
        "device": metadata.st_dev,
        "gid": 1000,
        "inode": metadata.st_ino,
        "mode": "0700",
        "path": os.fspath(checkout),
        "uid": 1000,
        "workflow_sha": workflow_sha,
    }
    return {
        "source_checkout_identity_sha256": _digest(
            materialize.package.canonical_json(value)
        ),
        "source_checkout_path": os.fspath(checkout),
    }


def _validate_checkout(checkout: Path, *, refresh: bool) -> dict[str, Any]:
    try:
        resolved = checkout.resolve(strict=True)
    except OSError:
        fail("runner-reservation-checkout-unavailable")
    if resolved != checkout or checkout.parent != SOURCE_CHECKOUT_ROOT:
        fail("runner-reservation-checkout-invalid")
    _exact_metadata(checkout, kind="directory", mode=0o700)
    git_directory = checkout / ".git"
    git_metadata = _exact_metadata(git_directory, kind="directory", mode=0o700)
    if git_metadata.st_nlink < 2:
        fail("runner-reservation-checkout-git-invalid")
    if (
        _run_git(checkout, "rev-parse", "--show-toplevel") != os.fspath(checkout)
        or _run_git(checkout, "rev-parse", "--is-inside-work-tree") != "true"
        or _run_git(checkout, "rev-parse", "--is-shallow-repository") != "false"
        or _run_git(checkout, "remote", "get-url", "origin")
        != "https://github.com/ArchonMegalon/propertyquarry.git"
    ):
        fail("runner-reservation-checkout-binding-invalid")
    dangerous_configuration = {
        key.lower()
        for key in _run_git(
            checkout, "config", "--includes", "--list", "--name-only"
        ).splitlines()
        if key
    }
    if any(
        key.startswith("url.") and (
            key.endswith(".insteadof") or key.endswith(".pushinsteadof")
        )
        or key.startswith("credential.")
        or key.startswith("include.")
        or key.startswith("includeif.")
        or key.startswith("filter.")
        or key.startswith("submodule.")
        or key.startswith("alias.")
        or key.startswith("protocol.")
        or key.startswith("merge.") and key.endswith(".driver")
        or key.startswith("diff.")
        and (key.endswith(".command") or key.endswith(".textconv"))
        or "proxy" in key
        or key in {
            "core.askpass",
            "core.fsmonitor",
            "core.hookspath",
            "core.sshcommand",
            "http.extraheader",
            "remote.origin.pushurl",
            "remote.origin.uploadpack",
        }
        for key in dangerous_configuration
    ):
        fail("runner-reservation-git-configuration-invalid")
    if refresh:
        _run_git(
            checkout,
            "-c",
            "credential.helper=",
            "-c",
            "core.askPass=/bin/false",
            "-c",
            "protocol.file.allow=never",
            "fetch",
            "--quiet",
            "--force",
            "--no-tags",
            "--no-recurse-submodules",
            "https://github.com/ArchonMegalon/propertyquarry.git",
            "+refs/heads/main:refs/remotes/origin/main",
        )
    workflow_sha = _run_git(checkout, "rev-parse", "--verify", "HEAD")
    if (
        _run_git(checkout, "rev-parse", "--abbrev-ref", "HEAD") != "HEAD"
        or _run_git(checkout, "rev-parse", "--verify", "refs/remotes/origin/main")
        != workflow_sha
        or _run_git(checkout, "status", "--porcelain=v1", "--untracked-files=all")
        != ""
        or not SHA_PATTERN.fullmatch(workflow_sha)
        or checkout.name != workflow_sha
    ):
        fail("runner-reservation-checkout-binding-invalid")
    _run_git(
        checkout,
        "cat-file",
        "-e",
        f"{workflow_sha}:.github/workflows/smoke-runtime.yml",
    )
    tree_raw = _run_git_bytes(
        checkout, "ls-tree", "-r", "--full-tree", "-z", "HEAD", allow_nul=True
    )
    if not tree_raw or not tree_raw.endswith(b"\0"):
        fail("runner-reservation-checkout-tree-invalid")
    for record in tree_raw.removesuffix(b"\0").split(b"\0"):
        if not (record.startswith(b"100644 ") or record.startswith(b"100755 ")):
            fail("runner-reservation-checkout-tree-mode-invalid")
    identity = _checkout_identity(checkout, workflow_sha)
    if (
        _run_git(checkout, "rev-parse", "--verify", "HEAD") != workflow_sha
        or _run_git(checkout, "status", "--porcelain=v1", "--untracked-files=all")
        != ""
        or _checkout_identity(checkout, workflow_sha) != identity
    ):
        fail("runner-reservation-checkout-mutated")
    return {
        **identity,
        "source_tree_sha256": _digest(tree_raw),
        "workflow_sha": workflow_sha,
    }


def _create_source_checkout() -> dict[str, Any]:
    _recover_checkout_stages()
    try:
        stage = Path(
            tempfile.mkdtemp(
                prefix=SOURCE_CHECKOUT_STAGE_PREFIX,
                dir=os.fspath(SOURCE_CHECKOUT_ROOT),
            )
        )
        os.chmod(stage, 0o700)
    except OSError:
        fail("runner-reservation-checkout-stage-create-failed")
    published = False
    try:
        _run_git(stage, "init", "--quiet")
        os.chmod(stage / ".git", 0o700)
        _run_git(
            stage,
            "remote",
            "add",
            "origin",
            "https://github.com/ArchonMegalon/propertyquarry.git",
        )
        _run_git(
            stage,
            "-c",
            "credential.helper=",
            "-c",
            "core.askPass=/bin/false",
            "-c",
            "protocol.file.allow=never",
            "fetch",
            "--quiet",
            "--force",
            "--no-tags",
            "--no-recurse-submodules",
            "https://github.com/ArchonMegalon/propertyquarry.git",
            "+refs/heads/main:refs/remotes/origin/main",
        )
        workflow_sha = _run_git(
            stage, "rev-parse", "--verify", "refs/remotes/origin/main"
        )
        if not SHA_PATTERN.fullmatch(workflow_sha):
            fail("runner-reservation-checkout-head-invalid")
        _run_git(stage, "checkout", "--quiet", "--detach", "--force", workflow_sha)
        target = SOURCE_CHECKOUT_ROOT / workflow_sha
        if target.exists():
            observed = _validate_checkout(target, refresh=True)
            if observed["workflow_sha"] != workflow_sha:
                fail("runner-reservation-checkout-existing-conflict")
            return observed
        materialize._rename_noreplace(stage, target)
        published = True
        _directory_sync(SOURCE_CHECKOUT_ROOT)
        return _validate_checkout(target, refresh=False)
    finally:
        if not published and stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def _revalidate_source_checkout(payload: dict[str, Any]) -> dict[str, Any]:
    workflow_sha = payload.get("workflow_sha")
    if type(workflow_sha) is not str or not SHA_PATTERN.fullmatch(workflow_sha):
        fail("runner-reservation-checkout-payload-invalid")
    observed = _validate_checkout(SOURCE_CHECKOUT_ROOT / workflow_sha, refresh=True)
    for key in (
        "source_checkout_path",
        "source_checkout_identity_sha256",
        "source_tree_sha256",
        "workflow_sha",
    ):
        if payload.get(key) != observed[key]:
            fail("runner-reservation-checkout-revalidation-invalid")
    return observed


def _load_receipt_authority():
    if os.geteuid() != 1000 or os.getegid() != 1000:
        fail("runner-reservation-uid-invalid")
    (
        _package_anchor,
        _package_private,
        _package_public,
        receipt_private,
        _receipt_public,
        _package_id,
        receipt_id,
    ) = materialize._load_authority(
        os.fspath(materialize.PRODUCTION_RECEIPT_AUTHORITY_ROOT)
    )
    return receipt_private, receipt_id


def _wire(payload: dict[str, Any], receipt_private, receipt_id: str) -> bytes:
    canonical = materialize.package.canonical_json(payload)
    signature = receipt_private.sign(
        materialize._framed(RESERVATION_SIGNATURE_DOMAIN, canonical)
    )
    return materialize.package.canonical_json(
        {
            "payload": payload,
            "signature": base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii"),
            "signature_key_id": receipt_id,
        }
    )


def _read_wire_directory(directory: Path) -> bytes:
    _exact_metadata(directory, kind="directory", mode=0o700)
    try:
        names = {item.name for item in directory.iterdir()}
    except OSError:
        fail("runner-reservation-read-failed")
    if names != {RESERVATION_NAME}:
        fail("runner-reservation-file-set-invalid")
    path = directory / RESERVATION_NAME
    before = _exact_metadata(path, kind="file", mode=0o600)
    if not 1 <= before.st_size <= materialize.package.MAX_JSON_BYTES:
        fail("runner-reservation-size-invalid")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(descriptor)
        raw = b""
        while len(raw) <= materialize.package.MAX_JSON_BYTES:
            chunk = os.read(
                descriptor,
                min(65_536, materialize.package.MAX_JSON_BYTES + 1 - len(raw)),
            )
            if not chunk:
                break
            raw += chunk
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if (
        len(raw) != before.st_size
        or identity
        != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        or identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        fail("runner-reservation-mutated")
    return raw


def _read_wire() -> bytes:
    return _read_wire_directory(RESERVATION_ROOT)


def _validate_wire(
    raw: bytes,
    *,
    workflow_sha: str | None,
    receipt_public,
    receipt_id: str,
) -> dict[str, Any]:
    try:
        wrapper = materialize.package.parse_strict_json(raw, "runner-reservation")
    except materialize.package.PackageFailure:
        fail("runner-reservation-wire-invalid")
    if set(wrapper) != {"payload", "signature", "signature_key_id"}:
        fail("runner-reservation-wrapper-shape-invalid")
    payload = wrapper.get("payload")
    signature_text = wrapper.get("signature")
    if (
        type(payload) is not dict
        or type(signature_text) is not str
        or wrapper.get("signature_key_id") != receipt_id
    ):
        fail("runner-reservation-wrapper-binding-invalid")
    try:
        signature = base64.b64decode(
            signature_text.encode("ascii") + b"=" * (-len(signature_text) % 4),
            altchars=b"-_",
            validate=True,
        )
        canonical = materialize.package.canonical_json(payload)
        receipt_public.verify(
            signature, materialize._framed(RESERVATION_SIGNATURE_DOMAIN, canonical)
        )
    except (UnicodeEncodeError, ValueError, InvalidSignature):
        fail("runner-reservation-signature-invalid")
    canonical_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    if len(signature) != 64 or signature_text != canonical_signature:
        fail("runner-reservation-signature-encoding-invalid")
    expected_keys = {
        "authority_profile", "created_at_epoch", "environment", "expires_at_epoch",
        "receipt_authority_key_id", "release_job", "repository", "repository_id",
        "repository_owner_id", "reservation_nonce", "runner_label", "runner_label_nonce",
        "schema", "source_checkout_identity_sha256", "source_checkout_path",
        "source_tree_sha256", "version", "workflow_path", "workflow_ref",
        "workflow_sha",
    }
    nonce = payload.get("reservation_nonce")
    created = payload.get("created_at_epoch")
    expires = payload.get("expires_at_epoch")
    label = payload.get("runner_label")
    if (
        set(payload) != expected_keys
        or payload.get("schema") != RESERVATION_SCHEMA
        or payload.get("version") != 2
        or payload.get("authority_profile") != "single-host-production-v2"
        or payload.get("environment") != "propertyquarry-production"
        or payload.get("repository") != "ArchonMegalon/propertyquarry"
        or payload.get("repository_id") != "1257593732"
        or payload.get("repository_owner_id") != "11421547"
        or payload.get("workflow_path") != ".github/workflows/smoke-runtime.yml"
        or payload.get("workflow_ref")
        != "ArchonMegalon/propertyquarry/.github/workflows/smoke-runtime.yml@refs/heads/main"
        or not SHA_PATTERN.fullmatch(str(payload.get("workflow_sha", "")))
        or (workflow_sha is not None and payload.get("workflow_sha") != workflow_sha)
        or payload.get("release_job") != "propertyquarry-release-v2"
        or payload.get("receipt_authority_key_id") != receipt_id
        or payload.get("source_checkout_path")
        != os.fspath(SOURCE_CHECKOUT_ROOT / str(payload.get("workflow_sha", "")))
        or type(payload.get("source_checkout_identity_sha256")) is not str
        or not re.fullmatch(
            r"sha256:[0-9a-f]{64}",
            payload.get("source_checkout_identity_sha256", ""),
        )
        or type(payload.get("source_tree_sha256")) is not str
        or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", payload.get("source_tree_sha256", "")
        )
        or type(nonce) is not str
        or not HEX64_PATTERN.fullmatch(nonce)
        or type(label) is not str
        or not LABEL_PATTERN.fullmatch(label)
        or payload.get("runner_label_nonce") != label.removeprefix("pqrelease-")
        or type(created) is not int
        or isinstance(created, bool)
        or type(expires) is not int
        or isinstance(expires, bool)
        or created < 1
        or expires - created != RESERVATION_TTL_SECONDS
    ):
        fail("runner-reservation-payload-invalid")
    derived = hashlib.sha256(RUNNER_LABEL_DOMAIN + bytes.fromhex(nonce)).hexdigest()[:32]
    if label != "pqrelease-" + derived:
        fail("runner-reservation-label-binding-invalid")
    return payload


def _result(raw: bytes, payload: dict[str, Any], disposition: str) -> dict[str, Any]:
    return {
        "dispatch_ticket_sha256": _digest(raw),
        "disposition": disposition,
        "expires_at_epoch": payload["expires_at_epoch"],
        "reservation_path": os.fspath(RESERVATION_ROOT / RESERVATION_NAME),
        "runner_label": payload["runner_label"],
        "schema": "propertyquarry.release-control.single-host-runner-reservation-result.v2",
        "version": 2,
    }


def _recovery_result(
    raw: bytes, payload: dict[str, Any], terminal: Path, disposition: str
) -> dict[str, Any]:
    return {
        "dispatch_ticket_sha256": _digest(raw),
        "disposition": disposition,
        "runner_label": payload["runner_label"],
        "schema": "propertyquarry.release-control.single-host-runner-reservation-recovery-result.v2",
        "terminal_path": os.fspath(terminal / RESERVATION_NAME),
        "version": 2,
    }


def _expired_terminals(receipt_public, receipt_id: str, current: int):
    if not RESERVATION_TERMINAL_ROOT.exists():
        return []
    _exact_metadata(RESERVATION_TERMINAL_ROOT, kind="directory", mode=0o700)
    try:
        candidates = sorted(RESERVATION_TERMINAL_ROOT.iterdir())
    except OSError:
        fail("runner-reservation-terminal-scan-failed")
    terminals = []
    for terminal in candidates:
        expired_match = re.fullmatch(
            r"(?P<digest>[0-9a-f]{64})\.expired\.v2", terminal.name
        )
        record_match = re.fullmatch(
            r"(?P<digest>[0-9a-f]{64})\.(?P<kind>claim|bound)\.v2(?P<pending>\.pending)?",
            terminal.name,
        )
        if expired_match is not None:
            raw = _read_wire_directory(terminal)
            payload = _validate_wire(
                raw,
                workflow_sha=None,
                receipt_public=receipt_public,
                receipt_id=receipt_id,
            )
            if expired_match.group("digest") != _digest(raw).removeprefix(
                "sha256:"
            ):
                fail("runner-reservation-terminal-binding-invalid")
            if current <= payload["expires_at_epoch"]:
                fail("runner-reservation-terminal-binding-invalid")
            terminals.append(
                (payload["expires_at_epoch"], terminal.name, terminal, raw, payload)
            )
            continue
        if record_match is None or not terminal.is_file() or terminal.is_symlink():
            fail("runner-reservation-terminal-name-invalid")
        try:
            raw = materialize._read_runner_terminal_file(terminal)
            if record_match.group("kind") == "claim":
                payload = materialize._validate_runner_materialization_claim(
                    raw, receipt_public=receipt_public, receipt_id=receipt_id
                )
            else:
                payload = materialize._validate_runner_materialization_binding(
                    raw, receipt_public=receipt_public, receipt_id=receipt_id
                )
        except materialize.MaterializeFailure:
            fail("runner-reservation-terminal-binding-invalid")
        if record_match.group("digest") != payload[
            "reservation_sha256"
        ].removeprefix("sha256:"):
            fail("runner-reservation-terminal-binding-invalid")
    return terminals


def _publish_noreplace(raw: bytes) -> None:
    parent = RESERVATION_PARENT
    _exact_metadata(parent, kind="directory", mode=0o700)
    if RESERVATION_STAGE.exists():
        _exact_metadata(RESERVATION_STAGE, kind="directory", mode=0o700)
        shutil.rmtree(RESERVATION_STAGE)
        _directory_sync(parent)
    os.mkdir(RESERVATION_STAGE, 0o700)
    temporary = RESERVATION_STAGE
    published = False
    try:
        os.chmod(temporary, 0o700)
        descriptor = os.open(
            temporary / RESERVATION_NAME,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            offset = 0
            while offset < len(raw):
                written = os.write(descriptor, raw[offset:])
                if written < 1:
                    fail("runner-reservation-write-failed")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory = os.open(
            temporary, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        materialize._rename_noreplace(temporary, RESERVATION_ROOT)
        published = True
        parent_descriptor = os.open(
            parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    finally:
        if not published:
            shutil.rmtree(temporary, ignore_errors=True)


def prepare(
    *,
    now: int | None = None,
    random_source: Callable[[int], bytes] = secrets.token_bytes,
    source_observer: Callable[[], dict[str, Any]] = _create_source_checkout,
    source_validator: Callable[[dict[str, Any]], dict[str, Any]] = _revalidate_source_checkout,
) -> dict[str, Any]:
    created = int(time.time()) if now is None else now
    if type(created) is not int or created < 1:
        fail("runner-reservation-time-invalid")
    receipt_private, receipt_id = _load_receipt_authority()
    receipt_public = receipt_private.public_key()
    lock = _acquire_lock()
    try:
        _recover_staging_directories()
        if RESERVATION_ROOT.exists():
            raw = _read_wire()
            payload = _validate_wire(
                raw,
                workflow_sha=None,
                receipt_public=receipt_public,
                receipt_id=receipt_id,
            )
            if created > payload["expires_at_epoch"]:
                fail("runner-reservation-expired-recovery-required")
            source_validator(payload)
            return _result(raw, payload, "already-prepared")
        source = source_observer()
        workflow_sha = source.get("workflow_sha")
        if type(workflow_sha) is not str or not SHA_PATTERN.fullmatch(workflow_sha):
            fail("runner-reservation-workflow-sha-invalid")
        if (
            set(source)
            != {
                "source_checkout_identity_sha256",
                "source_checkout_path",
                "source_tree_sha256",
                "workflow_sha",
            }
            or source.get("source_checkout_path")
            != os.fspath(SOURCE_CHECKOUT_ROOT / workflow_sha)
            or not re.fullmatch(
                r"sha256:[0-9a-f]{64}",
                str(source.get("source_checkout_identity_sha256", "")),
            )
            or not re.fullmatch(
                r"sha256:[0-9a-f]{64}",
                str(source.get("source_tree_sha256", "")),
            )
        ):
            fail("runner-reservation-source-observation-invalid")
        nonce_raw = random_source(32)
        if type(nonce_raw) is not bytes or len(nonce_raw) != 32:
            fail("runner-reservation-random-invalid")
        nonce = nonce_raw.hex()
        label_nonce = hashlib.sha256(RUNNER_LABEL_DOMAIN + nonce_raw).hexdigest()[:32]
        payload = {
            "authority_profile": "single-host-production-v2",
            "created_at_epoch": created,
            "environment": "propertyquarry-production",
            "expires_at_epoch": created + RESERVATION_TTL_SECONDS,
            "receipt_authority_key_id": receipt_id,
            "release_job": "propertyquarry-release-v2",
            "repository": "ArchonMegalon/propertyquarry",
            "repository_id": "1257593732",
            "repository_owner_id": "11421547",
            "reservation_nonce": nonce,
            "runner_label": "pqrelease-" + label_nonce,
            "runner_label_nonce": label_nonce,
            "schema": RESERVATION_SCHEMA,
            "source_checkout_identity_sha256": source[
                "source_checkout_identity_sha256"
            ],
            "source_checkout_path": source["source_checkout_path"],
            "source_tree_sha256": source["source_tree_sha256"],
            "version": 2,
            "workflow_path": ".github/workflows/smoke-runtime.yml",
            "workflow_ref": (
                "ArchonMegalon/propertyquarry/.github/workflows/"
                "smoke-runtime.yml@refs/heads/main"
            ),
            "workflow_sha": workflow_sha,
        }
        raw = _wire(payload, receipt_private, receipt_id)
        _publish_noreplace(raw)
        return _result(raw, payload, "prepared")
    finally:
        os.close(lock)


def recover_expired(*, now: int | None = None) -> dict[str, Any]:
    current = int(time.time()) if now is None else now
    receipt_private, receipt_id = _load_receipt_authority()
    lock = _acquire_lock()
    try:
        _recover_staging_directories()
        terminals = _expired_terminals(receipt_private.public_key(), receipt_id, current)
        if not RESERVATION_ROOT.exists():
            if not terminals:
                fail("runner-reservation-recovery-state-missing")
            _expires, _name, terminal, raw, payload = terminals[-1]
            return _recovery_result(raw, payload, terminal, "expired-terminal-already-published")
        raw = _read_wire()
        payload = _validate_wire(
            raw,
            workflow_sha=None,
            receipt_public=receipt_private.public_key(),
            receipt_id=receipt_id,
        )
        if current <= payload["expires_at_epoch"]:
            fail("runner-reservation-not-expired")
        if not RESERVATION_TERMINAL_ROOT.exists():
            os.mkdir(RESERVATION_TERMINAL_ROOT, 0o700)
            _directory_sync(RESERVATION_PARENT)
        _exact_metadata(RESERVATION_TERMINAL_ROOT, kind="directory", mode=0o700)
        terminal = RESERVATION_TERMINAL_ROOT / (
            _digest(raw).removeprefix("sha256:") + ".expired.v2"
        )
        if terminal.exists():
            terminal_raw = _read_wire_directory(terminal)
            terminal_payload = _validate_wire(
                terminal_raw,
                workflow_sha=None,
                receipt_public=receipt_private.public_key(),
                receipt_id=receipt_id,
            )
            if terminal_raw != raw or terminal_payload != payload:
                fail("runner-reservation-terminal-conflict")
            shutil.rmtree(RESERVATION_ROOT)
            _directory_sync(RESERVATION_PARENT)
            return _recovery_result(
                raw, payload, terminal, "expired-duplicate-converged"
            )
        materialize._rename_noreplace(RESERVATION_ROOT, terminal)
        _directory_sync(RESERVATION_TERMINAL_ROOT)
        _directory_sync(RESERVATION_PARENT)
        return _recovery_result(raw, payload, terminal, "expired-terminal-published")
    finally:
        os.close(lock)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("command", choices=("prepare", "recover-expired"))
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        result = prepare() if arguments.command == "prepare" else recover_expired()
        sys.stdout.buffer.write(materialize.package.canonical_json(result) + b"\n")
        return 0
    except (ReservationFailure, materialize.MaterializeFailure) as error:
        sys.stderr.write(f"propertyquarry-runner-reservation-rejected:{error}\n")
        return 50
    except KeyboardInterrupt:
        sys.stderr.write("propertyquarry-runner-reservation-rejected:interrupted\n")
        return 50
    except Exception:
        sys.stderr.write("propertyquarry-runner-reservation-rejected:internal-failure\n")
        return 50


if __name__ == "__main__":
    raise SystemExit(main())
