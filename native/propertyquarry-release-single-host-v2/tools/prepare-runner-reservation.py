#!/usr/bin/env python3
"""Create the one-shot signed reservation used to dispatch a release runner."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
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
PREREQUISITE_APPROVAL_ROOT = (
    RESERVATION_PARENT / "single-host-v2-runner-prerequisite-approvals"
)
SOURCE_CHECKOUT_ROOT = RESERVATION_PARENT / "single-host-v2-release-checkouts"
SOURCE_CHECKOUT_STAGE_PREFIX = ".single-host-v2-release-checkout.preparing."
RESERVATION_SCHEMA = (
    "propertyquarry.release-control.single-host-runner-reservation.v2"
)
RESERVATION_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-runner-reservation-signature.v2\0"
)
PREREQUISITE_INTENT_SCHEMA = (
    "propertyquarry.release-control.single-host-runner-prerequisite-intent.v2"
)
PREREQUISITE_INTENT_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-runner-prerequisite-intent-signature.v2\0"
)
PREREQUISITE_INTENT_V3_SCHEMA = (
    "propertyquarry.release-control.single-host-runner-prerequisite-intent.v3"
)
PREREQUISITE_INTENT_V3_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-runner-prerequisite-intent-signature.v3\0"
)
PREREQUISITE_POST_ATTEMPT_SCHEMA = (
    "propertyquarry.release-control.single-host-runner-prerequisite-post-attempt.v3"
)
PREREQUISITE_POST_ATTEMPT_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-runner-prerequisite-post-attempt-signature.v3\0"
)
PREREQUISITE_APPROVAL_SCHEMA = (
    "propertyquarry.release-control.single-host-runner-prerequisite-approval.v2"
)
PREREQUISITE_APPROVAL_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-runner-prerequisite-approval-signature.v2\0"
)
PREREQUISITE_APPROVAL_V3_SCHEMA = (
    "propertyquarry.release-control.single-host-runner-prerequisite-approval.v3"
)
PREREQUISITE_APPROVAL_V3_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-runner-prerequisite-approval-signature.v3\0"
)
RETIREMENT_TERMINAL_SCHEMA = (
    "propertyquarry.release-control.single-host-runner-retirement-terminal.v2"
)
RETIREMENT_TERMINAL_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-runner-retirement-terminal-signature.v2\0"
)
ABANDONMENT_SCHEMA = (
    "propertyquarry.release-control.single-host-runner-reservation-abandonment.v2"
)
ABANDONMENT_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-runner-reservation-abandonment-signature.v2\0"
)
RUNNER_LABEL_DOMAIN = (
    b"propertyquarry.release-control.single-host-runner-label.v2\0"
)
RESERVATION_TTL_SECONDS = 6 * 60 * 60
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
HEX64_PATTERN = re.compile(r"^[0-9a-f]{64}$")
LABEL_PATTERN = re.compile(r"^pqrelease-[0-9a-f]{32}$")
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
NUMERIC_ID_PATTERN = re.compile(r"^[1-9][0-9]{0,19}$")
PREREQUISITE_JOB_KEY = "propertyquarry-protected-dispatch-inputs"


class ReservationFailure(ValueError):
    """A secret-free reservation rejection."""


def fail(code: str) -> NoReturn:
    raise ReservationFailure(code)


def _checkpoint(_name: str) -> None:
    """Test-only crash boundary."""


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _canonical_utc_timestamp(value: object) -> bool:
    if type(value) is not str or re.fullmatch(
        r"20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
        value,
    ) is None:
        return False
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return False
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ") == value


def _prerequisite_job_name(
    *, runner_label: str, reservation_sha256: str
) -> str:
    return (
        f"{PREREQUISITE_JOB_KEY} | {runner_label} | "
        f"{reservation_sha256}"
    )


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


def _tracked_checkout_entries(
    checkout: Path,
) -> tuple[list[tuple[str, int, str]], bytes]:
    tree_raw = _run_git_bytes(
        checkout,
        "ls-tree",
        "-r",
        "--full-tree",
        "-z",
        "HEAD",
        allow_nul=True,
    )
    index_raw = _run_git_bytes(
        checkout,
        "ls-files",
        "--cached",
        "--stage",
        "--full-name",
        "-z",
        allow_nul=True,
    )

    def parse(raw: bytes, *, index: bool) -> list[tuple[str, int, str]]:
        if not raw or not raw.endswith(b"\0"):
            fail("runner-reservation-checkout-tree-invalid")
        result: list[tuple[str, int, str]] = []
        previous = b""
        for record in raw.removesuffix(b"\0").split(b"\0"):
            if record.count(b"\t") != 1:
                fail("runner-reservation-checkout-tree-invalid")
            header, path_raw = record.split(b"\t", 1)
            fields = header.split(b" ")
            if len(fields) != 3:
                fail("runner-reservation-checkout-tree-invalid")
            mode_raw, kind_or_oid, oid_or_stage = fields
            if mode_raw not in {b"100644", b"100755"}:
                fail("runner-reservation-checkout-tree-mode-invalid")
            if index:
                oid_raw, stage_raw = kind_or_oid, oid_or_stage
                if stage_raw != b"0":
                    fail("runner-reservation-checkout-index-invalid")
            else:
                kind_raw, oid_raw = kind_or_oid, oid_or_stage
                if kind_raw != b"blob":
                    fail("runner-reservation-checkout-tree-mode-invalid")
            if re.fullmatch(rb"[0-9a-f]{40}", oid_raw) is None:
                fail("runner-reservation-checkout-tree-invalid")
            if (
                not path_raw
                or path_raw <= previous
                or path_raw.startswith(b"/")
                or b"\\" in path_raw
                or any(character < 0x20 or character == 0x7F for character in path_raw)
            ):
                fail("runner-reservation-checkout-tree-path-invalid")
            try:
                relative = path_raw.decode("ascii")
            except UnicodeDecodeError:
                fail("runner-reservation-checkout-tree-path-invalid")
            parts = relative.split("/")
            if any(not part or part in {".", ".."} for part in parts):
                fail("runner-reservation-checkout-tree-path-invalid")
            result.append(
                (relative, int(mode_raw, 8) & 0o777, oid_raw.decode("ascii"))
            )
            previous = path_raw
        return result

    tree = parse(tree_raw, index=False)
    tracked = parse(index_raw, index=True)
    if tree != tracked:
        fail("runner-reservation-checkout-index-invalid")
    return tree, tree_raw


def _tracked_checkout_filesystem(
    checkout: Path,
    entries: list[tuple[str, int, str]],
    *,
    normalize: bool,
) -> None:
    try:
        root_metadata = checkout.lstat()
        root_descriptor = os.open(
            checkout,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError:
        fail("runner-reservation-checkout-worktree-invalid")
    try:
        root_observed = os.fstat(root_descriptor)
        if (
            stat.S_ISLNK(root_metadata.st_mode)
            or not stat.S_ISDIR(root_metadata.st_mode)
            or not stat.S_ISDIR(root_observed.st_mode)
            or root_metadata.st_dev != root_observed.st_dev
            or root_metadata.st_ino != root_observed.st_ino
            or stat.S_IMODE(root_observed.st_mode) != 0o700
            or root_observed.st_uid != 1000
            or root_observed.st_gid != 1000
        ):
            fail("runner-reservation-checkout-worktree-invalid")

        for relative, mode, oid in entries:
            parts = relative.split("/")
            parent_descriptor = os.dup(root_descriptor)
            try:
                for component in parts[:-1]:
                    try:
                        metadata = os.stat(
                            component,
                            dir_fd=parent_descriptor,
                            follow_symlinks=False,
                        )
                        child_descriptor = os.open(
                            component,
                            os.O_RDONLY
                            | os.O_DIRECTORY
                            | os.O_NOFOLLOW
                            | os.O_CLOEXEC,
                            dir_fd=parent_descriptor,
                        )
                    except OSError:
                        fail("runner-reservation-checkout-worktree-invalid")
                    observed = os.fstat(child_descriptor)
                    if (
                        stat.S_ISLNK(metadata.st_mode)
                        or not stat.S_ISDIR(metadata.st_mode)
                        or not stat.S_ISDIR(observed.st_mode)
                        or metadata.st_dev != observed.st_dev
                        or metadata.st_ino != observed.st_ino
                        or observed.st_uid != 1000
                        or observed.st_gid != 1000
                    ):
                        os.close(child_descriptor)
                        fail("runner-reservation-checkout-worktree-invalid")
                    if normalize:
                        os.fchmod(child_descriptor, 0o755)
                        os.fsync(child_descriptor)
                    elif stat.S_IMODE(observed.st_mode) != 0o755:
                        os.close(child_descriptor)
                        fail("runner-reservation-checkout-worktree-mode-invalid")
                    os.close(parent_descriptor)
                    parent_descriptor = child_descriptor

                try:
                    metadata = os.stat(
                        parts[-1],
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                    descriptor = os.open(
                        parts[-1],
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=parent_descriptor,
                    )
                except OSError:
                    fail("runner-reservation-checkout-worktree-invalid")
                try:
                    observed = os.fstat(descriptor)
                    if (
                        stat.S_ISLNK(metadata.st_mode)
                        or not stat.S_ISREG(metadata.st_mode)
                        or not stat.S_ISREG(observed.st_mode)
                        or metadata.st_dev != observed.st_dev
                        or metadata.st_ino != observed.st_ino
                        or observed.st_uid != 1000
                        or observed.st_gid != 1000
                        or observed.st_nlink != 1
                    ):
                        fail("runner-reservation-checkout-worktree-invalid")
                    if normalize:
                        os.fchmod(descriptor, mode)
                        os.fsync(descriptor)
                        observed = os.fstat(descriptor)
                    if (
                        stat.S_IMODE(observed.st_mode) != mode
                        or observed.st_nlink != 1
                    ):
                        fail("runner-reservation-checkout-worktree-mode-invalid")
                    blob = hashlib.sha1(usedforsecurity=False)
                    blob.update(f"blob {observed.st_size}\0".encode("ascii"))
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    while True:
                        chunk = os.read(descriptor, 1024 * 1024)
                        if not chunk:
                            break
                        blob.update(chunk)
                    after = os.fstat(descriptor)
                    if (
                        blob.hexdigest() != oid
                        or after.st_dev != observed.st_dev
                        or after.st_ino != observed.st_ino
                        or after.st_size != observed.st_size
                        or after.st_mtime_ns != observed.st_mtime_ns
                        or after.st_nlink != 1
                    ):
                        fail("runner-reservation-checkout-worktree-content-invalid")
                finally:
                    os.close(descriptor)
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
        os.fsync(root_descriptor)
    finally:
        os.close(root_descriptor)


def _normalize_checkout_worktree(checkout: Path, workflow_sha: str) -> None:
    try:
        observed = _validate_checkout(checkout, refresh=False)
    except ReservationFailure as exc:
        if str(exc) != "runner-reservation-checkout-worktree-mode-invalid":
            raise
    else:
        if observed["workflow_sha"] != workflow_sha:
            fail("runner-reservation-checkout-normalization-invalid")
    if (
        not SHA_PATTERN.fullmatch(workflow_sha)
        or _run_git(checkout, "rev-parse", "--verify", "HEAD") != workflow_sha
        or _run_git(
            checkout,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=matching",
        )
        != ""
    ):
        fail("runner-reservation-checkout-normalization-invalid")
    entries, tree_raw = _tracked_checkout_entries(checkout)
    _tracked_checkout_filesystem(checkout, entries, normalize=True)
    observed_entries, observed_tree_raw = _tracked_checkout_entries(checkout)
    if (
        observed_entries != entries
        or observed_tree_raw != tree_raw
        or _run_git(checkout, "rev-parse", "--verify", "HEAD") != workflow_sha
        or _run_git(
            checkout,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=matching",
        )
        != ""
    ):
        fail("runner-reservation-checkout-normalization-invalid")
    _tracked_checkout_filesystem(checkout, entries, normalize=False)


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
        or _run_git(
            checkout,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=matching",
        )
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
    entries, tree_raw = _tracked_checkout_entries(checkout)
    _tracked_checkout_filesystem(checkout, entries, normalize=False)
    identity = _checkout_identity(checkout, workflow_sha)
    if (
        _run_git(checkout, "rev-parse", "--verify", "HEAD") != workflow_sha
        or _run_git(
            checkout,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=matching",
        )
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
            _normalize_checkout_worktree(target, workflow_sha)
            observed = _validate_checkout(target, refresh=True)
            if observed["workflow_sha"] != workflow_sha:
                fail("runner-reservation-checkout-existing-conflict")
            return observed
        materialize._rename_noreplace(stage, target)
        published = True
        _directory_sync(SOURCE_CHECKOUT_ROOT)
        _normalize_checkout_worktree(target, workflow_sha)
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


def _signed_record(
    payload: dict[str, Any],
    receipt_private,
    receipt_id: str,
    domain: bytes,
) -> bytes:
    canonical = materialize.package.canonical_json(payload)
    signature = receipt_private.sign(
        materialize._framed(domain, canonical)
    )
    return materialize.package.canonical_json(
        {
            "payload": payload,
            "signature": base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii"),
            "signature_key_id": receipt_id,
        }
    )


def _wire(payload: dict[str, Any], receipt_private, receipt_id: str) -> bytes:
    return _signed_record(
        payload,
        receipt_private,
        receipt_id,
        RESERVATION_SIGNATURE_DOMAIN,
    )


def _verify_governed_record(
    raw: bytes,
    *,
    receipt_public,
    receipt_id: str,
    schema: str,
    domain: bytes,
    label: str,
) -> dict[str, Any]:
    expected_version = 3 if schema.endswith(".v3") else 2
    try:
        wrapper = materialize.package.parse_strict_json(raw, label)
    except materialize.package.PackageFailure:
        fail(f"{label}-wire-invalid")
    payload = wrapper.get("payload") if isinstance(wrapper, dict) else None
    signature_text = wrapper.get("signature") if isinstance(wrapper, dict) else None
    if (
        not isinstance(wrapper, dict)
        or set(wrapper) != {"payload", "signature", "signature_key_id"}
        or type(payload) is not dict
        or type(signature_text) is not str
        or wrapper.get("signature_key_id") != receipt_id
    ):
        fail(f"{label}-wrapper-invalid")
    try:
        signature = base64.b64decode(
            signature_text.encode("ascii") + b"=" * (-len(signature_text) % 4),
            altchars=b"-_",
            validate=True,
        )
        receipt_public.verify(
            signature,
            materialize._framed(
                domain, materialize.package.canonical_json(payload)
            ),
        )
    except (UnicodeEncodeError, ValueError, InvalidSignature):
        fail(f"{label}-signature-invalid")
    if (
        len(signature) != 64
        or signature_text
        != base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        or materialize.package.canonical_json(wrapper) != raw
        or payload.get("schema") != schema
        or payload.get("version") != expected_version
    ):
        fail(f"{label}-binding-invalid")
    return payload


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


def _governed_record_paths(
    reservation_raw: bytes,
) -> tuple[Path, Path, Path, Path]:
    identity = _digest(reservation_raw).removeprefix("sha256:")
    return (
        PREREQUISITE_APPROVAL_ROOT / f"{identity}.intent.v3.json",
        PREREQUISITE_APPROVAL_ROOT / f"{identity}.approved.v3.json",
        PREREQUISITE_APPROVAL_ROOT / f"{identity}.post-attempt.v3.json",
        PREREQUISITE_APPROVAL_ROOT / f"{identity}.retire-terminal.v2.json",
    )


def _legacy_governed_record_paths(
    reservation_raw: bytes,
) -> tuple[Path, Path]:
    identity = _digest(reservation_raw).removeprefix("sha256:")
    return (
        PREREQUISITE_APPROVAL_ROOT / f"{identity}.intent.v2.json",
        PREREQUISITE_APPROVAL_ROOT / f"{identity}.approved.v2.json",
    )


def _read_governed_file(path: Path) -> bytes:
    before = _exact_metadata(path, kind="file", mode=0o600)
    if not 1 <= before.st_size <= materialize.package.MAX_JSON_BYTES:
        fail("runner-reservation-governed-record-size-invalid")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        opened = os.fstat(descriptor)
        raw = b""
        while len(raw) <= materialize.package.MAX_JSON_BYTES:
            chunk = os.read(
                descriptor,
                min(
                    65_536,
                    materialize.package.MAX_JSON_BYTES + 1 - len(raw),
                ),
            )
            if not chunk:
                break
            raw += chunk
        after = os.fstat(descriptor)
    except OSError:
        fail("runner-reservation-governed-record-read-failed")
    finally:
        if "descriptor" in locals():
            os.close(descriptor)
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if (
        len(raw) != before.st_size
        or identity
        != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        or identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        fail("runner-reservation-governed-record-mutated")
    return raw


def _validate_prerequisite_intent(
    payload: dict[str, Any],
    *,
    reservation_raw: bytes,
    reservation_payload: dict[str, Any],
    receipt_id: str,
) -> None:
    expected_keys = {
        "authority_profile",
        "comment",
        "discovered_at_epoch",
        "environment_id",
        "environment_name",
        "initial_jobs_sha256",
        "initial_pending_deployments_sha256",
        "initial_runs_index_sha256",
        "prerequisite_job_id",
        "prerequisite_job_name",
        "receipt_authority_key_id",
        "release_job",
        "repository",
        "repository_id",
        "repository_owner_id",
        "reservation_expires_at_epoch",
        "reservation_sha256",
        "run_attempt",
        "run_id",
        "runner_label",
        "schema",
        "version",
        "workflow_path",
        "workflow_ref",
        "workflow_sha",
    }
    discovered = payload.get("discovered_at_epoch")
    if (
        set(payload) != expected_keys
        or payload.get("schema") != PREREQUISITE_INTENT_SCHEMA
        or payload.get("version") != 2
        or payload.get("authority_profile") != "single-host-production-v2"
        or payload.get("repository") != "ArchonMegalon/propertyquarry"
        or payload.get("repository_id") != "1257593732"
        or payload.get("repository_owner_id") != "11421547"
        or payload.get("workflow_path")
        != ".github/workflows/smoke-runtime.yml"
        or payload.get("workflow_ref")
        != (
            "ArchonMegalon/propertyquarry/.github/workflows/"
            "smoke-runtime.yml@refs/heads/main"
        )
        or payload.get("workflow_sha") != reservation_payload["workflow_sha"]
        or payload.get("receipt_authority_key_id") != receipt_id
        or payload.get("reservation_sha256") != _digest(reservation_raw)
        or payload.get("reservation_expires_at_epoch")
        != reservation_payload["expires_at_epoch"]
        or payload.get("runner_label") != reservation_payload["runner_label"]
        or payload.get("environment_name") != "propertyquarry-production"
        or type(payload.get("environment_id")) is not str
        or NUMERIC_ID_PATTERN.fullmatch(payload["environment_id"]) is None
        or payload.get("prerequisite_job_name")
        != PREREQUISITE_JOB_KEY
        or type(payload.get("prerequisite_job_id")) is not str
        or NUMERIC_ID_PATTERN.fullmatch(payload["prerequisite_job_id"]) is None
        or payload.get("release_job") != "propertyquarry-release-v2"
        or type(payload.get("run_id")) is not str
        or NUMERIC_ID_PATTERN.fullmatch(payload["run_id"]) is None
        or type(payload.get("run_attempt")) is not int
        or isinstance(payload.get("run_attempt"), bool)
        or not 1 <= payload["run_attempt"] < 1 << 31
        or type(discovered) is not int
        or isinstance(discovered, bool)
        or not reservation_payload["created_at_epoch"]
        <= discovered
        <= reservation_payload["expires_at_epoch"]
        or payload.get("comment")
        != (
            "PropertyQuarry governed prerequisite approval "
            + _digest(reservation_raw)
        )
        or any(
            type(payload.get(field)) is not str
            or DIGEST_PATTERN.fullmatch(payload[field]) is None
            for field in (
                "initial_jobs_sha256",
                "initial_pending_deployments_sha256",
                "initial_runs_index_sha256",
            )
        )
    ):
        fail("runner-reservation-prerequisite-intent-invalid")


def _validate_prerequisite_intent_v3(
    payload: dict[str, Any],
    *,
    reservation_raw: bytes,
    reservation_payload: dict[str, Any],
    receipt_id: str,
) -> None:
    if (
        payload.get("schema") != PREREQUISITE_INTENT_V3_SCHEMA
        or payload.get("version") != 3
        or payload.get("prerequisite_job_key") != PREREQUISITE_JOB_KEY
        or payload.get("prerequisite_job_name")
        != _prerequisite_job_name(
            runner_label=reservation_payload["runner_label"],
            reservation_sha256=_digest(reservation_raw),
        )
    ):
        fail("runner-reservation-prerequisite-intent-v3-invalid")
    legacy_shape = dict(payload)
    legacy_shape.pop("prerequisite_job_key", None)
    legacy_shape["schema"] = PREREQUISITE_INTENT_SCHEMA
    legacy_shape["version"] = 2
    legacy_shape["prerequisite_job_name"] = PREREQUISITE_JOB_KEY
    try:
        _validate_prerequisite_intent(
            legacy_shape,
            reservation_raw=reservation_raw,
            reservation_payload=reservation_payload,
            receipt_id=receipt_id,
        )
    except ReservationFailure:
        fail("runner-reservation-prerequisite-intent-v3-invalid")


def _validate_prerequisite_post_attempt(
    payload: dict[str, Any],
    *,
    intent_raw: bytes,
    intent: dict[str, Any],
) -> None:
    expected_keys = {
        "attempted_at_epoch",
        "authority_profile",
        "comment",
        "environment_id",
        "environment_name",
        "github_api_path",
        "http_method",
        "intent_sha256",
        "pre_post_pending_deployments_sha256",
        "pre_post_review_history_sha256",
        "pre_post_jobs_sha256",
        "pre_post_pending_deployments_count",
        "pre_post_release_job_present",
        "pre_post_review_match_count",
        "pre_post_review_scope",
        "pre_post_run_sha256",
        "prerequisite_job_id",
        "prerequisite_job_key",
        "prerequisite_job_name",
        "receipt_authority_key_id",
        "repository",
        "repository_id",
        "repository_owner_id",
        "request_sha256",
        "reservation_expires_at_epoch",
        "reservation_sha256",
        "run_attempt",
        "run_id",
        "runner_label",
        "schema",
        "version",
        "workflow_path",
        "workflow_ref",
        "workflow_sha",
    }
    request_raw = materialize.package.canonical_json(
        {
            "comment": intent["comment"],
            "environment_ids": [int(intent["environment_id"])],
            "state": "approved",
        }
    )
    attempted = payload.get("attempted_at_epoch")
    if (
        set(payload) != expected_keys
        or payload.get("schema") != PREREQUISITE_POST_ATTEMPT_SCHEMA
        or payload.get("version") != 3
        or payload.get("authority_profile") != "single-host-production-v2"
        or payload.get("intent_sha256") != _digest(intent_raw)
        or payload.get("reservation_sha256") != intent["reservation_sha256"]
        or payload.get("reservation_expires_at_epoch")
        != intent["reservation_expires_at_epoch"]
        or payload.get("runner_label") != intent["runner_label"]
        or payload.get("run_id") != intent["run_id"]
        or payload.get("run_attempt") != intent["run_attempt"]
        or payload.get("prerequisite_job_id") != intent["prerequisite_job_id"]
        or payload.get("prerequisite_job_name")
        != intent["prerequisite_job_name"]
        or payload.get("prerequisite_job_key")
        != intent["prerequisite_job_key"]
        or payload.get("environment_id") != intent["environment_id"]
        or payload.get("environment_name") != "propertyquarry-production"
        or payload.get("receipt_authority_key_id")
        != intent["receipt_authority_key_id"]
        or payload.get("repository") != "ArchonMegalon/propertyquarry"
        or payload.get("repository_id") != "1257593732"
        or payload.get("repository_owner_id") != "11421547"
        or payload.get("workflow_path")
        != ".github/workflows/smoke-runtime.yml"
        or payload.get("workflow_ref")
        != (
            "ArchonMegalon/propertyquarry/.github/workflows/"
            "smoke-runtime.yml@refs/heads/main"
        )
        or payload.get("workflow_sha") != intent["workflow_sha"]
        or payload.get("http_method") != "POST"
        or payload.get("github_api_path")
        != (
            "/repos/ArchonMegalon/propertyquarry/actions/runs/"
            + intent["run_id"]
            + "/pending_deployments"
        )
        or payload.get("comment") != intent["comment"]
        or payload.get("request_sha256") != _digest(request_raw)
        or any(
            type(payload.get(field)) is not str
            or DIGEST_PATTERN.fullmatch(payload[field]) is None
            for field in (
                "pre_post_pending_deployments_sha256",
                "pre_post_review_history_sha256",
            )
        )
        or payload.get("pre_post_release_job_present") is not False
        or payload.get("pre_post_pending_deployments_count") != 1
        or payload.get("pre_post_review_match_count") != 0
        or payload.get("pre_post_review_scope")
        != "any-approved-target-environment"
        or any(
            type(payload.get(field)) is not str
            or DIGEST_PATTERN.fullmatch(payload[field]) is None
            for field in (
                "pre_post_jobs_sha256",
                "pre_post_run_sha256",
            )
        )
        or type(attempted) is not int
        or isinstance(attempted, bool)
        or not intent["discovered_at_epoch"]
        <= attempted
        <= intent["reservation_expires_at_epoch"]
    ):
        fail("runner-reservation-prerequisite-post-attempt-invalid")


def _validate_prerequisite_approval(
    payload: dict[str, Any],
    *,
    intent_raw: bytes,
    intent: dict[str, Any],
) -> None:
    expected_keys = {
        "approval_api_disposition",
        "approval_response_sha256",
        "approved_at_epoch",
        "completed_jobs_sha256",
        "environment_id",
        "environment_name",
        "intent_sha256",
        "post_pending_deployments_sha256",
        "prerequisite_conclusion",
        "prerequisite_job_id",
        "prerequisite_job_name",
        "receipt_authority_key_id",
        "release_job",
        "repository",
        "repository_id",
        "repository_owner_id",
        "reservation_expires_at_epoch",
        "reservation_sha256",
        "review_history_sha256",
        "run_attempt",
        "run_id",
        "runner_label",
        "schema",
        "version",
        "workflow_path",
        "workflow_ref",
        "workflow_sha",
    }
    disposition = payload.get("approval_api_disposition")
    response_digest = payload.get("approval_response_sha256")
    approved = payload.get("approved_at_epoch")
    if (
        set(payload) != expected_keys
        or payload.get("schema") != PREREQUISITE_APPROVAL_SCHEMA
        or payload.get("version") != 2
        or payload.get("intent_sha256") != _digest(intent_raw)
        or payload.get("reservation_sha256") != intent["reservation_sha256"]
        or payload.get("runner_label") != intent["runner_label"]
        or payload.get("run_id") != intent["run_id"]
        or payload.get("run_attempt") != intent["run_attempt"]
        or payload.get("prerequisite_job_id") != intent["prerequisite_job_id"]
        or payload.get("prerequisite_job_name")
        != PREREQUISITE_JOB_KEY
        or payload.get("prerequisite_conclusion") != "success"
        or payload.get("environment_id") != intent["environment_id"]
        or payload.get("environment_name") != "propertyquarry-production"
        or payload.get("receipt_authority_key_id")
        != intent["receipt_authority_key_id"]
        or payload.get("repository") != "ArchonMegalon/propertyquarry"
        or payload.get("repository_id") != "1257593732"
        or payload.get("repository_owner_id") != "11421547"
        or payload.get("workflow_path")
        != ".github/workflows/smoke-runtime.yml"
        or payload.get("workflow_ref")
        != (
            "ArchonMegalon/propertyquarry/.github/workflows/"
            "smoke-runtime.yml@refs/heads/main"
        )
        or payload.get("workflow_sha") != intent["workflow_sha"]
        or payload.get("release_job") != "propertyquarry-release-v2"
        or payload.get("reservation_expires_at_epoch")
        != intent["reservation_expires_at_epoch"]
        or disposition not in {"approved", "post-approved-recovered"}
        or (
            disposition == "approved"
            and (
                type(response_digest) is not str
                or DIGEST_PATTERN.fullmatch(response_digest) is None
            )
        )
        or (
            disposition == "post-approved-recovered"
            and response_digest is not None
        )
        or any(
            type(payload.get(field)) is not str
            or DIGEST_PATTERN.fullmatch(payload[field]) is None
            for field in (
                "completed_jobs_sha256",
                "post_pending_deployments_sha256",
                "review_history_sha256",
            )
        )
        or type(approved) is not int
        or isinstance(approved, bool)
        or not intent["discovered_at_epoch"]
        <= approved
        <= intent["reservation_expires_at_epoch"]
    ):
        fail("runner-reservation-prerequisite-approval-invalid")


def _validate_prerequisite_approval_v3(
    payload: dict[str, Any],
    *,
    intent_raw: bytes,
    intent: dict[str, Any],
) -> None:
    if (
        payload.get("schema") != PREREQUISITE_APPROVAL_V3_SCHEMA
        or payload.get("version") != 3
        or payload.get("intent_sha256") != _digest(intent_raw)
        or payload.get("prerequisite_job_key")
        != intent.get("prerequisite_job_key")
        or payload.get("prerequisite_job_name")
        != intent.get("prerequisite_job_name")
    ):
        fail("runner-reservation-prerequisite-approval-v3-invalid")
    legacy_intent = dict(intent)
    legacy_intent.pop("prerequisite_job_key", None)
    legacy_intent["schema"] = PREREQUISITE_INTENT_SCHEMA
    legacy_intent["version"] = 2
    legacy_intent["prerequisite_job_name"] = PREREQUISITE_JOB_KEY
    legacy_payload = dict(payload)
    legacy_payload.pop("prerequisite_job_key", None)
    legacy_payload["schema"] = PREREQUISITE_APPROVAL_SCHEMA
    legacy_payload["version"] = 2
    legacy_payload["prerequisite_job_name"] = PREREQUISITE_JOB_KEY
    # The intent digest changes when projected to the frozen v2 shape; retain
    # the v3 digest for the common binding check.
    projected_intent_raw = materialize.package.canonical_json(
        {"payload": legacy_intent}
    )
    legacy_payload["intent_sha256"] = _digest(projected_intent_raw)
    try:
        _validate_prerequisite_approval(
            legacy_payload,
            intent_raw=projected_intent_raw,
            intent=legacy_intent,
        )
    except ReservationFailure:
        fail("runner-reservation-prerequisite-approval-v3-invalid")


def _validate_retirement_terminal(
    payload: dict[str, Any],
    *,
    intent_raw: bytes,
    intent: dict[str, Any],
    post_attempt_raw: bytes | None,
) -> None:
    expected_keys = {
        "adopted_at_epoch",
        "adoption_disposition",
        "approval_post_attempt_present",
        "approval_post_attempt_sha256",
        "authority_profile",
        "environment_id",
        "environment_name",
        "final_exact_review_match_count",
        "final_exact_review_set_sha256",
        "final_jobs_sha256",
        "final_pending_deployments_count",
        "final_pending_deployments_sha256",
        "final_review_history_sha256",
        "final_review_match_count",
        "final_review_scope",
        "final_review_set_sha256",
        "final_run_sha256",
        "prerequisite_conclusion",
        "prerequisite_intent_sha256",
        "prerequisite_job_id",
        "prerequisite_job_key",
        "prerequisite_job_name",
        "receipt_authority_key_id",
        "release_job",
        "release_job_completed_at",
        "release_job_conclusion",
        "release_job_disposition",
        "release_job_id",
        "release_job_labels",
        "release_job_present",
        "release_job_run_attempt",
        "release_job_runner_group_id",
        "release_job_runner_group_name",
        "release_job_runner_id",
        "release_job_runner_name",
        "release_job_started_at",
        "release_job_steps_count",
        "repository",
        "repository_id",
        "repository_owner_id",
        "reservation_expires_at_epoch",
        "reservation_sha256",
        "run_attempt",
        "run_conclusion",
        "run_id",
        "runner_label",
        "schema",
        "terminal_jobs_verification_sha256",
        "terminal_run_verification_sha256",
        "version",
        "workflow_path",
        "workflow_ref",
        "workflow_sha",
    }
    adopted = payload.get("adopted_at_epoch")
    review_count = payload.get("final_review_match_count")
    exact_review_count = payload.get("final_exact_review_match_count")
    release_disposition = payload.get("release_job_disposition")
    post_attempt_present = post_attempt_raw is not None
    digest_fields = (
        "final_exact_review_set_sha256",
        "final_jobs_sha256",
        "final_pending_deployments_sha256",
        "final_review_history_sha256",
        "final_review_set_sha256",
        "final_run_sha256",
        "terminal_jobs_verification_sha256",
        "terminal_run_verification_sha256",
    )
    if (
        set(payload) != expected_keys
        or payload.get("schema") != RETIREMENT_TERMINAL_SCHEMA
        or payload.get("version") != 2
        or payload.get("authority_profile") != "single-host-production-v2"
        or payload.get("adoption_disposition")
        != "get-only-terminal-adoption"
        or payload.get("prerequisite_intent_sha256") != _digest(intent_raw)
        or payload.get("reservation_sha256")
        != intent["reservation_sha256"]
        or payload.get("reservation_expires_at_epoch")
        != intent["reservation_expires_at_epoch"]
        or payload.get("runner_label") != intent["runner_label"]
        or payload.get("run_id") != intent["run_id"]
        or payload.get("run_attempt") != intent["run_attempt"]
        or payload.get("prerequisite_job_id")
        != intent["prerequisite_job_id"]
        or payload.get("prerequisite_job_key") != PREREQUISITE_JOB_KEY
        or payload.get("prerequisite_job_name")
        != intent["prerequisite_job_name"]
        or payload.get("prerequisite_conclusion")
        not in {"cancelled", "failure", "success"}
        or payload.get("run_conclusion") not in {"cancelled", "failure"}
        or payload.get("environment_id") != intent["environment_id"]
        or payload.get("environment_name")
        != "propertyquarry-production"
        or payload.get("receipt_authority_key_id")
        != intent["receipt_authority_key_id"]
        or payload.get("repository") != "ArchonMegalon/propertyquarry"
        or payload.get("repository_id") != "1257593732"
        or payload.get("repository_owner_id") != "11421547"
        or payload.get("workflow_path")
        != ".github/workflows/smoke-runtime.yml"
        or payload.get("workflow_ref")
        != (
            "ArchonMegalon/propertyquarry/.github/workflows/"
            "smoke-runtime.yml@refs/heads/main"
        )
        or payload.get("workflow_sha") != intent["workflow_sha"]
        or payload.get("release_job") != "propertyquarry-release-v2"
        or payload.get("approval_post_attempt_present")
        is not post_attempt_present
        or (
            post_attempt_present
            and payload.get("approval_post_attempt_sha256")
            != _digest(post_attempt_raw)
        )
        or (
            not post_attempt_present
            and payload.get("approval_post_attempt_sha256") is not None
        )
        or payload.get("final_pending_deployments_count") != 0
        or payload.get("final_review_scope")
        != "any-approved-target-environment-complete-set"
        or type(review_count) is not int
        or isinstance(review_count, bool)
        or not 0 <= review_count <= 100
        or type(exact_review_count) is not int
        or isinstance(exact_review_count, bool)
        or not 0 <= exact_review_count <= review_count
        or any(
            type(payload.get(field)) is not str
            or DIGEST_PATTERN.fullmatch(payload[field]) is None
            for field in digest_fields
        )
        or payload.get("terminal_jobs_verification_sha256")
        != payload.get("final_jobs_sha256")
        or payload.get("terminal_run_verification_sha256")
        != payload.get("final_run_sha256")
        or type(adopted) is not int
        or isinstance(adopted, bool)
        or adopted < intent["discovered_at_epoch"]
        or release_disposition not in {"absent", "inert-terminal"}
    ):
        fail("runner-reservation-retirement-terminal-invalid")
    if release_disposition == "absent":
        if (
            payload.get("release_job_present") is not False
            or payload.get("release_job_id") is not None
            or payload.get("release_job_labels") != []
            or payload.get("release_job_run_attempt") is not None
            or payload.get("release_job_runner_id") is not None
            or payload.get("release_job_runner_name") is not None
            or payload.get("release_job_runner_group_id") is not None
            or payload.get("release_job_runner_group_name") is not None
            or payload.get("release_job_started_at") is not None
            or payload.get("release_job_completed_at") is not None
            or payload.get("release_job_conclusion") is not None
            or payload.get("release_job_steps_count") != 0
        ):
            fail("runner-reservation-retirement-terminal-invalid")
    else:
        release_started_at = payload.get("release_job_started_at")
        release_completed_at = payload.get("release_job_completed_at")
        release_labels = [
            "propertyquarry-release-controller-v2",
            intent["runner_label"],
        ]
        observed_release_labels = payload.get("release_job_labels")
        labels_are_bound = observed_release_labels in (
            release_labels,
            ["self-hosted", *release_labels],
        )
        labels_are_unevaluated = (
            observed_release_labels == []
            and payload.get("approval_post_attempt_present") is True
            and payload.get("prerequisite_conclusion") == "failure"
            and payload.get("run_conclusion") == "cancelled"
            and payload.get("release_job_conclusion") == "cancelled"
            and _canonical_utc_timestamp(
                payload.get("release_job_started_at")
            )
            and payload.get("release_job_started_at")
            == payload.get("release_job_completed_at")
        )
        if (
            payload.get("release_job_present") is not True
            or type(payload.get("release_job_id")) is not str
            or NUMERIC_ID_PATTERN.fullmatch(payload["release_job_id"]) is None
            or not (labels_are_bound or labels_are_unevaluated)
            or payload.get("release_job_run_attempt")
            != intent["run_attempt"]
            or payload.get("release_job_runner_id") not in {None, 0}
            or payload.get("release_job_runner_name") not in {None, ""}
            or payload.get("release_job_runner_group_id") not in {None, 0}
            or payload.get("release_job_runner_group_name")
            not in {None, ""}
            or type(release_completed_at) is not str
            or (
                release_started_at is not None
                and release_started_at != release_completed_at
            )
            or payload.get("release_job_conclusion")
            not in {"cancelled", "skipped"}
            or payload.get("release_job_steps_count") != 0
        ):
            fail("runner-reservation-retirement-terminal-invalid")


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


def _abandonment_path(reservation_raw: bytes) -> Path:
    return RESERVATION_TERMINAL_ROOT / (
        _digest(reservation_raw).removeprefix("sha256:")
        + ".abandonment.v2"
    )


def _load_terminal_evidence(
    reservation_raw: bytes,
    reservation_payload: dict[str, Any],
    *,
    receipt_public,
    receipt_id: str,
) -> dict[str, Any]:
    _exact_metadata(
        PREREQUISITE_APPROVAL_ROOT, kind="directory", mode=0o700
    )
    (
        _intent_path,
        approval_path,
        post_attempt_path,
        retirement_terminal_path,
    ) = _governed_record_paths(reservation_raw)
    _legacy_intent_path, legacy_approval_path = (
        _legacy_governed_record_paths(reservation_raw)
    )
    generation, intent_raw, intent = _load_prerequisite_intent_generation(
        reservation_raw,
        reservation_payload,
        receipt_public=receipt_public,
        receipt_id=receipt_id,
    )
    if approval_path.exists() or legacy_approval_path.exists():
        fail("runner-reservation-abandonment-approval-present")
    post_attempt_raw: bytes | None = None
    if post_attempt_path.exists():
        if generation != "v3":
            fail("runner-reservation-abandonment-branch-conflict")
        post_attempt_raw = _read_governed_file(post_attempt_path)
        post_attempt = _verify_governed_record(
            post_attempt_raw,
            receipt_public=receipt_public,
            receipt_id=receipt_id,
            schema=PREREQUISITE_POST_ATTEMPT_SCHEMA,
            domain=PREREQUISITE_POST_ATTEMPT_SIGNATURE_DOMAIN,
            label="runner-reservation-prerequisite-post-attempt",
        )
        _validate_prerequisite_post_attempt(
            post_attempt,
            intent_raw=intent_raw,
            intent=intent,
        )
    if not retirement_terminal_path.exists():
        fail("runner-reservation-abandonment-evidence-missing")
    terminal_raw = _read_governed_file(retirement_terminal_path)
    terminal = _verify_governed_record(
        terminal_raw,
        receipt_public=receipt_public,
        receipt_id=receipt_id,
        schema=RETIREMENT_TERMINAL_SCHEMA,
        domain=RETIREMENT_TERMINAL_SIGNATURE_DOMAIN,
        label="runner-reservation-retirement-terminal",
    )
    _validate_retirement_terminal(
        terminal,
        intent_raw=intent_raw,
        intent=intent,
        post_attempt_raw=post_attempt_raw,
    )
    return {
        "generation": generation,
        "intent": intent,
        "intent_raw": intent_raw,
        "kind": "retirement-adoption",
        "observed_at": terminal["adopted_at_epoch"],
        "release_disposition": terminal["release_job_disposition"],
        "terminal": terminal,
        "terminal_raw": terminal_raw,
    }


def _validate_terminal_abandonment(
    payload: dict[str, Any],
    *,
    reservation_raw: bytes,
    reservation_payload: dict[str, Any],
    evidence: dict[str, Any],
    receipt_id: str,
) -> None:
    intent = evidence["intent"]
    intent_raw = evidence["intent_raw"]
    terminal = evidence["terminal"]
    terminal_raw = evidence["terminal_raw"]
    expected_keys = {
        "abandoned_at_epoch",
        "authority_profile",
        "environment",
        "final_jobs_sha256",
        "final_pending_deployments_count",
        "final_pending_deployments_sha256",
        "final_review_history_sha256",
        "final_review_match_count",
        "final_review_scope",
        "final_run_sha256",
        "materialization_record_present",
        "prerequisite_approval_present",
        "prerequisite_intent_generation",
        "prerequisite_intent_sha256",
        "prerequisite_job_id",
        "prerequisite_job_key",
        "prerequisite_job_name",
        "receipt_authority_key_id",
        "release_job",
        "release_job_disposition",
        "release_job_present",
        "repository",
        "reservation_sha256",
        "run_attempt",
        "run_conclusion",
        "run_id",
        "runner_label",
        "schema",
        "terminal_evidence_kind",
        "terminal_evidence_payload_sha256",
        "terminal_evidence_sha256",
        "terminal_observed_at_epoch",
        "version",
        "workflow_path",
        "workflow_ref",
        "workflow_sha",
    }
    abandoned = payload.get("abandoned_at_epoch")
    if (
        set(payload) != expected_keys
        or payload.get("schema") != ABANDONMENT_SCHEMA
        or payload.get("version") != 2
        or payload.get("authority_profile") != "single-host-production-v2"
        or payload.get("reservation_sha256") != _digest(reservation_raw)
        or payload.get("runner_label") != reservation_payload["runner_label"]
        or payload.get("workflow_sha") != reservation_payload["workflow_sha"]
        or payload.get("workflow_path")
        != ".github/workflows/smoke-runtime.yml"
        or payload.get("workflow_ref")
        != (
            "ArchonMegalon/propertyquarry/.github/workflows/"
            "smoke-runtime.yml@refs/heads/main"
        )
        or payload.get("repository") != "ArchonMegalon/propertyquarry"
        or payload.get("environment") != "propertyquarry-production"
        or payload.get("release_job") != "propertyquarry-release-v2"
        or payload.get("release_job_present")
        != terminal["release_job_present"]
        or payload.get("release_job_disposition")
        != evidence["release_disposition"]
        or payload.get("prerequisite_approval_present") is not False
        or payload.get("materialization_record_present") is not False
        or payload.get("prerequisite_intent_generation")
        != evidence["generation"]
        or payload.get("prerequisite_intent_sha256") != _digest(intent_raw)
        or payload.get("prerequisite_job_id")
        != terminal["prerequisite_job_id"]
        or payload.get("prerequisite_job_key")
        != PREREQUISITE_JOB_KEY
        or payload.get("prerequisite_job_name")
        != intent["prerequisite_job_name"]
        or payload.get("terminal_evidence_kind") != evidence["kind"]
        or payload.get("terminal_evidence_sha256") != _digest(terminal_raw)
        or payload.get("terminal_evidence_payload_sha256")
        != _digest(materialize.package.canonical_json(terminal))
        or payload.get("terminal_observed_at_epoch")
        != evidence["observed_at"]
        or payload.get("final_jobs_sha256")
        != terminal["final_jobs_sha256"]
        or payload.get("final_pending_deployments_count")
        != terminal["final_pending_deployments_count"]
        or payload.get("final_pending_deployments_sha256")
        != terminal["final_pending_deployments_sha256"]
        or payload.get("final_review_history_sha256")
        != terminal["final_review_history_sha256"]
        or payload.get("final_review_match_count")
        != terminal["final_review_match_count"]
        or payload.get("final_review_scope")
        != terminal["final_review_scope"]
        or payload.get("final_run_sha256") != terminal["final_run_sha256"]
        or payload.get("receipt_authority_key_id") != receipt_id
        or payload.get("run_attempt") != terminal["run_attempt"]
        or payload.get("run_conclusion") != terminal["run_conclusion"]
        or payload.get("run_id") != terminal["run_id"]
        or type(abandoned) is not int
        or isinstance(abandoned, bool)
        or abandoned < evidence["observed_at"]
    ):
        fail("runner-reservation-abandonment-record-invalid")


def _load_prerequisite_intent_generation(
    reservation_raw: bytes,
    reservation_payload: dict[str, Any],
    *,
    receipt_public,
    receipt_id: str,
) -> tuple[str, bytes, dict[str, Any]]:
    intent_path, _approval_path, _post_attempt_path, _retirement_path = (
        _governed_record_paths(reservation_raw)
    )
    legacy_intent_path, _legacy_approval_path = (
        _legacy_governed_record_paths(reservation_raw)
    )
    if intent_path.exists() == legacy_intent_path.exists():
        fail("runner-reservation-governed-state-invalid")
    generation = "v3" if intent_path.exists() else "v2"
    if generation == "v2":
        intent_path = legacy_intent_path
    intent_raw = _read_governed_file(intent_path)
    intent = _verify_governed_record(
        intent_raw,
        receipt_public=receipt_public,
        receipt_id=receipt_id,
        schema=(
            PREREQUISITE_INTENT_V3_SCHEMA
            if generation == "v3"
            else PREREQUISITE_INTENT_SCHEMA
        ),
        domain=(
            PREREQUISITE_INTENT_V3_SIGNATURE_DOMAIN
            if generation == "v3"
            else PREREQUISITE_INTENT_SIGNATURE_DOMAIN
        ),
        label="runner-reservation-prerequisite-intent",
    )
    validator = (
        _validate_prerequisite_intent_v3
        if generation == "v3"
        else _validate_prerequisite_intent
    )
    validator(
        intent,
        reservation_raw=reservation_raw,
        reservation_payload=reservation_payload,
        receipt_id=receipt_id,
    )
    return generation, intent_raw, intent


def _active_governed_state(
    reservation_raw: bytes,
    reservation_payload: dict[str, Any],
    *,
    receipt_public,
    receipt_id: str,
) -> str | None:
    if not PREREQUISITE_APPROVAL_ROOT.exists():
        return None
    _exact_metadata(PREREQUISITE_APPROVAL_ROOT, kind="directory", mode=0o700)
    intent_path, approval_path, post_attempt_path, retirement_path = (
        _governed_record_paths(reservation_raw)
    )
    legacy_intent_path, legacy_approval_path = (
        _legacy_governed_record_paths(reservation_raw)
    )
    paths = (
        intent_path, approval_path, post_attempt_path, retirement_path,
        legacy_intent_path, legacy_approval_path,
    )
    if not any(path.exists() for path in paths):
        return None
    generation, intent_raw, intent = _load_prerequisite_intent_generation(
        reservation_raw,
        reservation_payload,
        receipt_public=receipt_public,
        receipt_id=receipt_id,
    )
    if generation == "v2":
        if approval_path.exists() or post_attempt_path.exists():
            fail("runner-reservation-governed-branches-conflict")
        if legacy_approval_path.exists():
            approval_raw = _read_governed_file(legacy_approval_path)
            approval = _verify_governed_record(
                approval_raw,
                receipt_public=receipt_public,
                receipt_id=receipt_id,
                schema=PREREQUISITE_APPROVAL_SCHEMA,
                domain=PREREQUISITE_APPROVAL_SIGNATURE_DOMAIN,
                label="runner-reservation-prerequisite-approval",
            )
            _validate_prerequisite_approval(
                approval, intent_raw=intent_raw, intent=intent
            )
            if retirement_path.exists():
                fail("runner-reservation-governed-branches-conflict")
            return "legacy-v2-approved"
        if retirement_path.exists():
            retirement_raw = _read_governed_file(retirement_path)
            retirement = _verify_governed_record(
                retirement_raw,
                receipt_public=receipt_public,
                receipt_id=receipt_id,
                schema=RETIREMENT_TERMINAL_SCHEMA,
                domain=RETIREMENT_TERMINAL_SIGNATURE_DOMAIN,
                label="runner-reservation-retirement-terminal",
            )
            _validate_retirement_terminal(
                retirement,
                intent_raw=intent_raw,
                intent=intent,
                post_attempt_raw=None,
            )
            return "retirement-terminal"
        return "legacy-v2-discovered"
    if legacy_approval_path.exists():
        fail("runner-reservation-governed-branches-conflict")
    post_attempt_raw: bytes | None = None
    post_attempt: dict[str, Any] | None = None
    if post_attempt_path.exists():
        post_attempt_raw = _read_governed_file(post_attempt_path)
        post_attempt = _verify_governed_record(
            post_attempt_raw,
            receipt_public=receipt_public,
            receipt_id=receipt_id,
            schema=PREREQUISITE_POST_ATTEMPT_SCHEMA,
            domain=PREREQUISITE_POST_ATTEMPT_SIGNATURE_DOMAIN,
            label="runner-reservation-prerequisite-post-attempt",
        )
        _validate_prerequisite_post_attempt(
            post_attempt, intent_raw=intent_raw, intent=intent
        )
    approval: dict[str, Any] | None = None
    if approval_path.exists():
        approval_raw = _read_governed_file(approval_path)
        approval = _verify_governed_record(
            approval_raw,
            receipt_public=receipt_public,
            receipt_id=receipt_id,
            schema=PREREQUISITE_APPROVAL_V3_SCHEMA,
            domain=PREREQUISITE_APPROVAL_V3_SIGNATURE_DOMAIN,
            label="runner-reservation-prerequisite-approval",
        )
        _validate_prerequisite_approval_v3(
            approval, intent_raw=intent_raw, intent=intent
        )
        if post_attempt_raw is None:
            fail("runner-reservation-governed-state-invalid")
        if (
            post_attempt is None
            or approval["approved_at_epoch"]
            != post_attempt["attempted_at_epoch"]
        ):
            fail("runner-reservation-governed-state-invalid")
    if retirement_path.exists():
        if approval is not None:
            fail("runner-reservation-governed-branches-conflict")
        retirement_raw = _read_governed_file(retirement_path)
        retirement = _verify_governed_record(
            retirement_raw,
            receipt_public=receipt_public,
            receipt_id=receipt_id,
            schema=RETIREMENT_TERMINAL_SCHEMA,
            domain=RETIREMENT_TERMINAL_SIGNATURE_DOMAIN,
            label="runner-reservation-retirement-terminal",
        )
        _validate_retirement_terminal(
            retirement,
            intent_raw=intent_raw,
            intent=intent,
            post_attempt_raw=post_attempt_raw,
        )
        return "retirement-terminal"
    if approval is not None:
        return "approved"
    if post_attempt_raw is not None:
        return "approval-post-attempt"
    return "discovered"


def _recover_removal_tombstones(receipt_public, receipt_id: str) -> None:
    if not RESERVATION_TERMINAL_ROOT.exists():
        return
    _exact_metadata(
        RESERVATION_TERMINAL_ROOT, kind="directory", mode=0o700
    )
    try:
        candidates = sorted(RESERVATION_TERMINAL_ROOT.iterdir())
    except OSError:
        fail("runner-reservation-removal-scan-failed")
    for tombstone in candidates:
        matched = re.fullmatch(
            r"(?P<digest>[0-9a-f]{64})\.removing\.v2",
            tombstone.name,
        )
        if matched is None:
            continue
        _exact_metadata(tombstone, kind="directory", mode=0o700)
        identity = matched.group("digest")
        expired = RESERVATION_TERMINAL_ROOT / (
            identity + ".expired.v2"
        )
        abandoned = RESERVATION_TERMINAL_ROOT / (
            identity + ".abandoned.v2"
        )
        if expired.exists() == abandoned.exists():
            fail("runner-reservation-removal-terminal-invalid")
        terminal = expired if expired.exists() else abandoned
        terminal_raw = _read_wire_directory(terminal)
        terminal_payload = _validate_wire(
            terminal_raw,
            workflow_sha=None,
            receipt_public=receipt_public,
            receipt_id=receipt_id,
        )
        if _digest(terminal_raw).removeprefix("sha256:") != identity:
            fail("runner-reservation-removal-terminal-invalid")
        try:
            entries = list(tombstone.iterdir())
        except OSError:
            fail("runner-reservation-removal-scan-failed")
        if entries:
            if (
                len(entries) != 1
                or entries[0].name != RESERVATION_NAME
            ):
                fail("runner-reservation-removal-tombstone-invalid")
            tombstone_raw = _read_wire_directory(tombstone)
            tombstone_payload = _validate_wire(
                tombstone_raw,
                workflow_sha=None,
                receipt_public=receipt_public,
                receipt_id=receipt_id,
            )
            if (
                tombstone_raw != terminal_raw
                or tombstone_payload != terminal_payload
            ):
                fail("runner-reservation-removal-tombstone-invalid")
            try:
                entries[0].unlink()
            except OSError:
                fail("runner-reservation-removal-cleanup-failed")
            _directory_sync(tombstone)
            _checkpoint("after-runner-reservation-removal-ticket-unlink")
        try:
            tombstone.rmdir()
        except OSError:
            fail("runner-reservation-removal-cleanup-failed")
        _directory_sync(RESERVATION_TERMINAL_ROOT)


def _remove_duplicate_active_root(
    identity: str,
    *,
    receipt_public,
    receipt_id: str,
) -> None:
    tombstone = RESERVATION_TERMINAL_ROOT / (
        identity + ".removing.v2"
    )
    if tombstone.exists():
        fail("runner-reservation-removal-tombstone-conflict")
    materialize._rename_noreplace(RESERVATION_ROOT, tombstone)
    _directory_sync(RESERVATION_TERMINAL_ROOT)
    _directory_sync(RESERVATION_PARENT)
    _checkpoint("after-runner-reservation-removal-tombstone")
    _recover_removal_tombstones(receipt_public, receipt_id)


def _scan_terminals(receipt_public, receipt_id: str, current: int):
    if not RESERVATION_TERMINAL_ROOT.exists():
        return {
            "abandoned": [],
            "abandonment_records": {},
            "expired": [],
            "materialization_digests": set(),
        }
    _exact_metadata(RESERVATION_TERMINAL_ROOT, kind="directory", mode=0o700)
    try:
        candidates = sorted(RESERVATION_TERMINAL_ROOT.iterdir())
    except OSError:
        fail("runner-reservation-terminal-scan-failed")
    expired_terminals = []
    abandoned_terminals = []
    abandonment_records: dict[str, tuple[Path, bytes, dict[str, Any]]] = {}
    pending_abandonment_records: dict[
        str, tuple[Path, bytes, dict[str, Any]]
    ] = {}
    materialization_digests: set[str] = set()
    for terminal in candidates:
        expired_match = re.fullmatch(
            r"(?P<digest>[0-9a-f]{64})\.expired\.v2", terminal.name
        )
        abandoned_match = re.fullmatch(
            r"(?P<digest>[0-9a-f]{64})\.abandoned\.v2", terminal.name
        )
        record_match = re.fullmatch(
            r"(?P<digest>[0-9a-f]{64})\.(?P<kind>claim|bound)\.v2(?P<pending>\.pending)?",
            terminal.name,
        )
        abandonment_match = re.fullmatch(
            r"(?P<digest>[0-9a-f]{64})\.abandonment\.v2(?P<pending>\.pending)?",
            terminal.name,
        )
        if expired_match is not None or abandoned_match is not None:
            raw = _read_wire_directory(terminal)
            payload = _validate_wire(
                raw,
                workflow_sha=None,
                receipt_public=receipt_public,
                receipt_id=receipt_id,
            )
            matched = expired_match or abandoned_match
            if matched is None or matched.group("digest") != _digest(
                raw
            ).removeprefix("sha256:"):
                fail("runner-reservation-terminal-binding-invalid")
            if (
                expired_match is not None
                and current <= payload["expires_at_epoch"]
            ):
                fail("runner-reservation-terminal-binding-invalid")
            target = (
                expired_terminals
                if expired_match is not None
                else abandoned_terminals
            )
            target.append(
                (
                    payload["expires_at_epoch"],
                    terminal.name,
                    terminal,
                    raw,
                    payload,
                )
            )
            continue
        if abandonment_match is not None:
            raw = _read_governed_file(terminal)
            payload = _verify_governed_record(
                raw,
                receipt_public=receipt_public,
                receipt_id=receipt_id,
                schema=ABANDONMENT_SCHEMA,
                domain=ABANDONMENT_SIGNATURE_DOMAIN,
                label="runner-reservation-abandonment",
            )
            target_records = (
                pending_abandonment_records
                if abandonment_match.group("pending") is not None
                else abandonment_records
            )
            digest = abandonment_match.group("digest")
            if digest in target_records:
                fail("runner-reservation-terminal-name-invalid")
            target_records[digest] = (terminal, raw, payload)
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
        materialization_digests.add(record_match.group("digest"))
    for digest, pending_record in pending_abandonment_records.items():
        if digest in abandonment_records:
            if abandonment_records[digest][1] != pending_record[1]:
                fail("runner-reservation-abandonment-record-conflict")
        else:
            abandonment_records[digest] = pending_record
    abandoned_digests = {
        _digest(item[3]).removeprefix("sha256:")
        for item in abandoned_terminals
    }
    expired_digests = {
        _digest(item[3]).removeprefix("sha256:")
        for item in expired_terminals
    }
    if expired_digests & abandoned_digests:
        fail("runner-reservation-terminal-state-conflict")
    if expired_digests & materialization_digests:
        fail("runner-reservation-expired-materialization-present")
    if abandoned_digests & materialization_digests:
        fail("runner-reservation-abandonment-materialization-present")
    for _expires, _name, _path, raw, payload in expired_terminals:
        if (
            _active_governed_state(
                raw,
                payload,
                receipt_public=receipt_public,
                receipt_id=receipt_id,
            )
            is not None
        ):
            fail("runner-reservation-expired-governed-state-present")
    if abandoned_digests != set(abandonment_records):
        active_digest = None
        if RESERVATION_ROOT.exists():
            active_digest = _digest(_read_wire()).removeprefix("sha256:")
        unresolved = set(abandonment_records) - abandoned_digests
        if unresolved != ({active_digest} if active_digest in unresolved else set()):
            fail("runner-reservation-abandonment-terminal-missing")
        if abandoned_digests - set(abandonment_records):
            fail("runner-reservation-abandonment-record-missing")
    for _expires, _name, _path, raw, payload in abandoned_terminals:
        digest = _digest(raw).removeprefix("sha256:")
        evidence = _load_terminal_evidence(
            raw,
            payload,
            receipt_public=receipt_public,
            receipt_id=receipt_id,
        )
        _record_path, _record_raw, record_payload = abandonment_records[digest]
        _validate_terminal_abandonment(
            record_payload,
            reservation_raw=raw,
            reservation_payload=payload,
            evidence=evidence,
            receipt_id=receipt_id,
        )
    return {
        "abandoned": abandoned_terminals,
        "abandonment_records": abandonment_records,
        "expired": expired_terminals,
        "materialization_digests": materialization_digests,
    }


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


def _publish_abandonment_record(path: Path, raw: bytes) -> str:
    if not RESERVATION_TERMINAL_ROOT.exists():
        os.mkdir(RESERVATION_TERMINAL_ROOT, 0o700)
        os.chmod(RESERVATION_TERMINAL_ROOT, 0o700)
        _directory_sync(RESERVATION_PARENT)
    _exact_metadata(
        RESERVATION_TERMINAL_ROOT, kind="directory", mode=0o700
    )
    pending = path.with_name(path.name + ".pending")
    if path.exists():
        if _read_governed_file(path) != raw:
            fail("runner-reservation-abandonment-record-conflict")
        if pending.exists() and _read_governed_file(pending) != raw:
            fail("runner-reservation-abandonment-record-conflict")
        return "already-published"
    if pending.exists():
        if _read_governed_file(pending) != raw:
            fail("runner-reservation-abandonment-record-conflict")
    else:
        try:
            descriptor = os.open(
                pending,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | os.O_CLOEXEC,
                0o600,
            )
            os.fchmod(descriptor, 0o600)
            offset = 0
            while offset < len(raw):
                written = os.write(descriptor, raw[offset:])
                if written < 1:
                    fail("runner-reservation-abandonment-record-write-failed")
                offset += written
            os.fsync(descriptor)
        except OSError:
            fail("runner-reservation-abandonment-record-write-failed")
        finally:
            if "descriptor" in locals():
                os.close(descriptor)
        _checkpoint("after-runner-reservation-abandonment-record-fsync")
        _directory_sync(RESERVATION_TERMINAL_ROOT)
    _checkpoint("before-runner-reservation-abandonment-record-promote")
    try:
        materialize._rename_noreplace(pending, path)
    except materialize.MaterializeFailure as error:
        if str(error) != "output-exists" or _read_governed_file(path) != raw:
            fail("runner-reservation-abandonment-record-publish-failed")
    _directory_sync(RESERVATION_TERMINAL_ROOT)
    return "published"


def _abandonment_result(
    raw: bytes,
    payload: dict[str, Any],
    abandonment_raw: bytes,
    terminal: Path,
    disposition: str,
) -> dict[str, Any]:
    return {
        "abandonment_sha256": _digest(abandonment_raw),
        "dispatch_ticket_sha256": _digest(raw),
        "disposition": disposition,
        "runner_label": payload["runner_label"],
        "schema": (
            "propertyquarry.release-control."
            "single-host-runner-reservation-abandonment-result.v2"
        ),
        "terminal_path": os.fspath(terminal / RESERVATION_NAME),
        "version": 2,
    }


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
        _recover_removal_tombstones(receipt_public, receipt_id)
        terminal_state = _scan_terminals(
            receipt_public, receipt_id, created
        )
        if RESERVATION_ROOT.exists():
            raw = _read_wire()
            payload = _validate_wire(
                raw,
                workflow_sha=None,
                receipt_public=receipt_public,
                receipt_id=receipt_id,
            )
            identity = _digest(raw).removeprefix("sha256:")
            if identity in terminal_state["abandonment_records"]:
                fail("runner-reservation-abandonment-recovery-required")
            if identity in terminal_state["materialization_digests"]:
                fail("runner-reservation-materialization-present")
            governed_state = _active_governed_state(
                raw,
                payload,
                receipt_public=receipt_public,
                receipt_id=receipt_id,
            )
            if governed_state is not None:
                fail(
                    "runner-reservation-governed-transition-nondispatchable"
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
    receipt_public = receipt_private.public_key()
    lock = _acquire_lock()
    try:
        _recover_staging_directories()
        _recover_removal_tombstones(receipt_public, receipt_id)
        terminal_state = _scan_terminals(receipt_public, receipt_id, current)
        terminals = terminal_state["expired"]
        if not RESERVATION_ROOT.exists():
            if not terminals:
                fail("runner-reservation-recovery-state-missing")
            _expires, _name, terminal, raw, payload = terminals[-1]
            return _recovery_result(
                raw, payload, terminal, "expired-terminal-already-published"
            )
        raw = _read_wire()
        payload = _validate_wire(
            raw,
            workflow_sha=None,
            receipt_public=receipt_public,
            receipt_id=receipt_id,
        )
        identity = _digest(raw).removeprefix("sha256:")
        if identity in terminal_state["abandonment_records"]:
            fail("runner-reservation-abandonment-recovery-required")
        if identity in terminal_state["materialization_digests"]:
            fail("runner-reservation-materialization-present")
        governed_state = _active_governed_state(
            raw,
            payload,
            receipt_public=receipt_public,
            receipt_id=receipt_id,
        )
        if governed_state == "approval-post-attempt":
            fail("runner-reservation-prerequisite-post-attempt-present")
        if governed_state in {"approved", "legacy-v2-approved"}:
            fail("runner-reservation-prerequisite-approval-present")
        if governed_state in {"discovered", "legacy-v2-discovered"}:
            fail("runner-reservation-discovered-terminal-required")
        if governed_state == "retirement-terminal":
            fail("runner-reservation-abandonment-recovery-required")
        if governed_state is not None:
            fail("runner-reservation-governed-state-invalid")
        if current <= payload["expires_at_epoch"]:
            fail("runner-reservation-not-expired")
        if not RESERVATION_TERMINAL_ROOT.exists():
            os.mkdir(RESERVATION_TERMINAL_ROOT, 0o700)
            _directory_sync(RESERVATION_PARENT)
        _exact_metadata(
            RESERVATION_TERMINAL_ROOT, kind="directory", mode=0o700
        )
        terminal = RESERVATION_TERMINAL_ROOT / (
            identity + ".expired.v2"
        )
        if terminal.exists():
            terminal_raw = _read_wire_directory(terminal)
            terminal_payload = _validate_wire(
                terminal_raw,
                workflow_sha=None,
                receipt_public=receipt_public,
                receipt_id=receipt_id,
            )
            if terminal_raw != raw or terminal_payload != payload:
                fail("runner-reservation-terminal-conflict")
            _remove_duplicate_active_root(
                identity,
                receipt_public=receipt_public,
                receipt_id=receipt_id,
            )
            return _recovery_result(
                raw, payload, terminal, "expired-duplicate-converged"
            )
        materialize._rename_noreplace(RESERVATION_ROOT, terminal)
        _directory_sync(RESERVATION_TERMINAL_ROOT)
        _directory_sync(RESERVATION_PARENT)
        return _recovery_result(
            raw, payload, terminal, "expired-terminal-published"
        )
    finally:
        os.close(lock)


def abandon_terminal(*, now: int | None = None) -> dict[str, Any]:
    current = int(time.time()) if now is None else now
    if type(current) is not int or current < 1:
        fail("runner-reservation-abandonment-time-invalid")
    receipt_private, receipt_id = _load_receipt_authority()
    receipt_public = receipt_private.public_key()
    lock = _acquire_lock()
    try:
        _recover_staging_directories()
        _recover_removal_tombstones(receipt_public, receipt_id)
        terminal_state = _scan_terminals(
            receipt_public, receipt_id, current
        )
        if not RESERVATION_ROOT.exists():
            abandoned = terminal_state["abandoned"]
            if not abandoned:
                fail("runner-reservation-abandonment-state-missing")
            _expires, _name, terminal, raw, payload = abandoned[-1]
            identity = _digest(raw).removeprefix("sha256:")
            _record_path, abandonment_raw, _record_payload = terminal_state[
                "abandonment_records"
            ][identity]
            return _abandonment_result(
                raw,
                payload,
                abandonment_raw,
                terminal,
                "abandoned-terminal-already-published",
            )

        reservation_raw = _read_wire()
        reservation_payload = _validate_wire(
            reservation_raw,
            workflow_sha=None,
            receipt_public=receipt_public,
            receipt_id=receipt_id,
        )
        identity = _digest(reservation_raw).removeprefix("sha256:")
        if identity in terminal_state["materialization_digests"]:
            fail("runner-reservation-abandonment-materialization-present")
        evidence = _load_terminal_evidence(
            reservation_raw,
            reservation_payload,
            receipt_public=receipt_public,
            receipt_id=receipt_id,
        )
        intent = evidence["intent"]
        intent_raw = evidence["intent_raw"]
        terminal_evidence = evidence["terminal"]
        terminal_evidence_raw = evidence["terminal_raw"]
        record_path = _abandonment_path(reservation_raw)
        existing_record = terminal_state["abandonment_records"].get(identity)
        if existing_record is not None:
            _existing_path, abandonment_raw, abandonment = existing_record
            _validate_terminal_abandonment(
                abandonment,
                reservation_raw=reservation_raw,
                reservation_payload=reservation_payload,
                evidence=evidence,
                receipt_id=receipt_id,
            )
        else:
            abandonment = {
                "abandoned_at_epoch": current,
                "authority_profile": "single-host-production-v2",
                "environment": "propertyquarry-production",
                "final_jobs_sha256": terminal_evidence[
                    "final_jobs_sha256"
                ],
                "final_pending_deployments_count": terminal_evidence[
                    "final_pending_deployments_count"
                ],
                "final_pending_deployments_sha256": terminal_evidence[
                    "final_pending_deployments_sha256"
                ],
                "final_review_history_sha256": terminal_evidence[
                    "final_review_history_sha256"
                ],
                "final_review_match_count": terminal_evidence[
                    "final_review_match_count"
                ],
                "final_review_scope": terminal_evidence[
                    "final_review_scope"
                ],
                "final_run_sha256": terminal_evidence[
                    "final_run_sha256"
                ],
                "materialization_record_present": False,
                "prerequisite_approval_present": False,
                "prerequisite_intent_generation": evidence["generation"],
                "prerequisite_intent_sha256": _digest(intent_raw),
                "prerequisite_job_id": terminal_evidence[
                    "prerequisite_job_id"
                ],
                "prerequisite_job_key": PREREQUISITE_JOB_KEY,
                "prerequisite_job_name": intent["prerequisite_job_name"],
                "receipt_authority_key_id": receipt_id,
                "release_job": "propertyquarry-release-v2",
                "release_job_disposition": evidence[
                    "release_disposition"
                ],
                "release_job_present": terminal_evidence[
                    "release_job_present"
                ],
                "repository": "ArchonMegalon/propertyquarry",
                "reservation_sha256": _digest(reservation_raw),
                "run_attempt": terminal_evidence["run_attempt"],
                "run_conclusion": terminal_evidence["run_conclusion"],
                "run_id": terminal_evidence["run_id"],
                "runner_label": reservation_payload["runner_label"],
                "schema": ABANDONMENT_SCHEMA,
                "terminal_evidence_kind": evidence["kind"],
                "terminal_evidence_payload_sha256": _digest(
                    materialize.package.canonical_json(terminal_evidence)
                ),
                "terminal_evidence_sha256": _digest(
                    terminal_evidence_raw
                ),
                "terminal_observed_at_epoch": evidence["observed_at"],
                "version": 2,
                "workflow_path": ".github/workflows/smoke-runtime.yml",
                "workflow_ref": (
                    "ArchonMegalon/propertyquarry/.github/workflows/"
                    "smoke-runtime.yml@refs/heads/main"
                ),
                "workflow_sha": reservation_payload["workflow_sha"],
            }
            _validate_terminal_abandonment(
                abandonment,
                reservation_raw=reservation_raw,
                reservation_payload=reservation_payload,
                evidence=evidence,
                receipt_id=receipt_id,
            )
            abandonment_raw = _signed_record(
                abandonment,
                receipt_private,
                receipt_id,
                ABANDONMENT_SIGNATURE_DOMAIN,
            )
        record_disposition = _publish_abandonment_record(
            record_path, abandonment_raw
        )
        _checkpoint("after-runner-reservation-abandonment-record")

        terminal = RESERVATION_TERMINAL_ROOT / (
            identity + ".abandoned.v2"
        )
        if terminal.exists():
            terminal_raw = _read_wire_directory(terminal)
            terminal_payload = _validate_wire(
                terminal_raw,
                workflow_sha=None,
                receipt_public=receipt_public,
                receipt_id=receipt_id,
            )
            if (
                terminal_raw != reservation_raw
                or terminal_payload != reservation_payload
            ):
                fail("runner-reservation-abandonment-terminal-conflict")
            if _read_wire() != reservation_raw:
                fail("runner-reservation-abandonment-active-conflict")
            _remove_duplicate_active_root(
                identity,
                receipt_public=receipt_public,
                receipt_id=receipt_id,
            )
            disposition = "abandoned-duplicate-converged"
        else:
            materialize._rename_noreplace(RESERVATION_ROOT, terminal)
            _directory_sync(RESERVATION_TERMINAL_ROOT)
            _directory_sync(RESERVATION_PARENT)
            disposition = (
                "abandoned-terminal-published"
                if record_disposition == "published"
                else "abandoned-terminal-recovered"
            )
        _checkpoint("after-runner-reservation-abandonment-terminal")
        return _abandonment_result(
            reservation_raw,
            reservation_payload,
            abandonment_raw,
            terminal,
            disposition,
        )
    finally:
        os.close(lock)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "command", choices=("prepare", "recover-expired", "abandon-terminal")
    )
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "prepare":
            result = prepare()
        elif arguments.command == "recover-expired":
            result = recover_expired()
        else:
            result = abandon_terminal()
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
