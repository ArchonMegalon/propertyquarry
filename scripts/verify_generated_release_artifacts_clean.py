#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
RELEASE_GIT_COMMAND = (
    "/usr/bin/git",
    "--no-pager",
    "--no-replace-objects",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.untrackedCache=false",
    "-c",
    "core.hooksPath=/dev/null",
)
GENERATED_ARTIFACTS = (
    Path(".codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json"),
    Path(".codex-design/product/WEEKLY_PRODUCT_PULSE.generated.json"),
    Path(".codex-studio/published/EA_BROWSER_WORKFLOW_PROOF.generated.json"),
)
RELEASE_MANIFEST_PATH = Path("docs/PROPERTYQUARRY_RELEASE_MANIFEST.md")
RELEASE_ARTIFACT_SET_PREFIX = "propertyquarry-generated-release-artifacts-v1@sha256:"
RELEASE_MANIFEST_SCHEMA = "propertyquarry.release_manifest.v1"
RELEASE_MANIFEST_JSON_START = "<!-- propertyquarry-release-manifest-json:start -->"
RELEASE_MANIFEST_JSON_END = "<!-- propertyquarry-release-manifest-json:end -->"
RELEASE_MANIFEST_VERIFICATION_COMMANDS = (
    "./scripts/propertyquarry_release_python.sh "
    "scripts/propertyquarry_release_make_dispatch.py release-preflight"
)
RELEASE_MANIFEST_FIELDS = (
    "release_manifest_schema",
    "release_product",
    "release_candidate_status",
    "release_repository",
    "release_repository_origin",
    "release_mirror_repository",
    "release_mirror_origin",
    "release_branch",
    "release_commit_sha",
    "release_public_origin",
    "release_artifact_set",
    "release_label",
    "release_generated_at",
    "release_verification_commands",
    "release_deployment_id",
)
RELEASE_MANIFEST_STATIC_VALUES = {
    "release_manifest_schema": RELEASE_MANIFEST_SCHEMA,
    "release_product": "PropertyQuarry",
    "release_candidate_status": "source-browser-candidate-pending-protected-live-evidence",
    "release_repository": "ArchonMegalon/property",
    "release_repository_origin": "https://github.com/ArchonMegalon/property.git",
    "release_mirror_repository": "ArchonMegalon/propertyquarry",
    "release_mirror_origin": "https://github.com/ArchonMegalon/propertyquarry.git",
    "release_branch": "main",
    "release_public_origin": "https://propertyquarry.com",
    "release_verification_commands": RELEASE_MANIFEST_VERIFICATION_COMMANDS,
}
_RFC3339_UTC_SECONDS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_FULL_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_ARTIFACT_SET = re.compile(
    rf"^{re.escape(RELEASE_ARTIFACT_SET_PREFIX)}[0-9a-f]{{64}}$"
)
VOLATILE_KEYS = {
    "generated_at",
    "as_of",
    "created_at",
    "mtime_utc",
    "size_bytes",
    "duration_seconds",
    "git_branch",
    "git_head",
    "source_path",
    "resolved_path",
    "git_repo_root",
    "command",
    "cwd",
    "output_excerpt",
    "python_bin",
    "review_due",
}
RENAME_EXCHANGE = 2


class _DuplicateJSONKey(ValueError):
    pass


class _PreserveRestoreStagingError(RuntimeError):
    pass


def release_git_environment() -> dict[str, str]:
    return {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": "/nonexistent",
        "XDG_CONFIG_HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise _DuplicateJSONKey(key)
        payload[key] = value
    return payload


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _is_valid_rfc3339_utc_seconds(value: object) -> bool:
    if type(value) is not str or _RFC3339_UTC_SECONDS.fullmatch(value) is None:
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def _load_json_object(payload: bytes, *, label: str) -> dict[str, Any]:
    try:
        decoded = payload.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{label} is not strict UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} JSON root is not an object")
    return dict(value)


def _json_values_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return set(left) == set(right) and all(
            _json_values_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_values_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return bool(left == right)


def _normalize(value: Any, path: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            source_binding_field = "source_binding" in path
            if (
                not source_binding_field
                and (key in VOLATILE_KEYS or str(key).endswith("_git_head"))
            ):
                continue
            if (
                key in {"git_blob_oid", "sha256"}
                and path[-1:] == ("browser_receipt_binding",)
            ):
                continue
            normalized[key] = _normalize(item, (*path, str(key)))
        return normalized
    if isinstance(value, list):
        return [_normalize(item, path) for item in value]
    return value


def _load_worktree(path: Path) -> Any:
    return _load_json_object(
        (ROOT / path).read_bytes(),
        label=f"worktree artifact {path.as_posix()}",
    )


def _load_head(path: Path) -> Any:
    return _load_json_object(
        _git_show_bytes(ROOT, "HEAD", path),
        label=f"HEAD artifact {path.as_posix()}",
    )


def _git_show_bytes(root: Path, revision: str, path: Path) -> bytes:
    result = subprocess.run(
        [
            *RELEASE_GIT_COMMAND,
            "-C",
            str(root),
            f"--work-tree={root}",
            "show",
            f"{revision}:{path.as_posix()}",
        ],
        check=True,
        capture_output=True,
        env=release_git_environment(),
    )
    return bytes(result.stdout)


def _git_show_index_bytes(root: Path, path: Path) -> bytes:
    result = subprocess.run(
        [
            *RELEASE_GIT_COMMAND,
            "-C",
            str(root),
            f"--work-tree={root}",
            "show",
            f":{path.as_posix()}",
        ],
        check=True,
        capture_output=True,
        env=release_git_environment(),
    )
    return bytes(result.stdout)


def _git_tree_entry(root: Path, revision: str, path: Path) -> tuple[str, str]:
    result = subprocess.run(
        [
            *RELEASE_GIT_COMMAND,
            "-C",
            str(root),
            f"--work-tree={root}",
            "ls-tree",
            "-z",
            revision,
            "--",
            path.as_posix(),
        ],
        check=True,
        capture_output=True,
        env=release_git_environment(),
    )
    records = bytes(result.stdout).split(b"\0")
    if records[-1:] == [b""]:
        records.pop()
    if len(records) != 1:
        raise ValueError(f"HEAD has {len(records)} entries for {path.as_posix()}")
    metadata, separator, observed_path = records[0].partition(b"\t")
    fields = metadata.split()
    if separator != b"\t" or observed_path != path.as_posix().encode("utf-8"):
        raise ValueError(f"HEAD entry path is ambiguous for {path.as_posix()}")
    if len(fields) != 3 or fields[1] != b"blob":
        raise ValueError(f"HEAD entry is not a blob for {path.as_posix()}")
    mode, _kind, oid = fields
    return mode.decode("ascii"), oid.decode("ascii")


def _git_index_entry(root: Path, path: Path) -> tuple[str, str]:
    result = subprocess.run(
        [
            *RELEASE_GIT_COMMAND,
            "-C",
            str(root),
            f"--work-tree={root}",
            "ls-files",
            "--stage",
            "-z",
            "--",
            path.as_posix(),
        ],
        check=True,
        capture_output=True,
        env=release_git_environment(),
    )
    records = bytes(result.stdout).split(b"\0")
    if records[-1:] == [b""]:
        records.pop()
    if len(records) != 1:
        raise ValueError(f"index has {len(records)} entries for {path.as_posix()}")
    metadata, separator, observed_path = records[0].partition(b"\t")
    fields = metadata.split()
    if separator != b"\t" or observed_path != path.as_posix().encode("utf-8"):
        raise ValueError(f"index entry path is ambiguous for {path.as_posix()}")
    if len(fields) != 3 or fields[2] != b"0":
        raise ValueError(f"index entry is conflicted for {path.as_posix()}")
    mode, oid, _stage = fields
    return mode.decode("ascii"), oid.decode("ascii")


def _checkout_regular_file_failure(root: Path, path: Path, *, label: str) -> str | None:
    if path.is_absolute() or not path.parts or ".." in path.parts:
        return f"{label} path is not a safe repository-relative path"
    current = root
    try:
        root_mode = current.lstat().st_mode
    except OSError as exc:
        return f"{label} repository root is unreadable: {exc}"
    if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
        return f"{label} repository root is symlinked or not a directory"
    for part in path.parts[:-1]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            return f"{label} parent is missing or unreadable: {exc}"
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            return f"{label} parent is symlinked or not a directory"
    try:
        mode = (root / path).lstat().st_mode
    except OSError as exc:
        return f"{label} is missing or unreadable: {exc}"
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        return f"{label} is symlinked or not a regular file"
    return None


def _checkout_permissions_are_safe(mode: int) -> bool:
    permissions = stat.S_IMODE(mode)
    return bool(permissions & stat.S_IRUSR) and not (
        permissions
        & (
            stat.S_IWGRP
            | stat.S_IWOTH
            | stat.S_IXUSR
            | stat.S_IXGRP
            | stat.S_IXOTH
        )
    )


def _open_checkout_parent(root: Path, path: Path) -> int:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    current_fd = os.open(root, directory_flags)
    try:
        for part in path.parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


_ApprovedWorktreeSnapshot = tuple[int, int, int, int, int, int, bytes]


def _same_snapshot_across_rename(
    left: _ApprovedWorktreeSnapshot,
    right: _ApprovedWorktreeSnapshot,
) -> bool:
    # Linux updates ctime when renameat2 moves an inode.  Device, inode, mode,
    # size, mtime, and bytes must remain stable across the exchange.
    return left[:5] == right[:5] and left[6] == right[6]


def _entry_identity_no_follow(
    parent_fd: int,
    name: str,
) -> tuple[int, int, int, int, int, int] | None:
    try:
        observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return (
        observed.st_dev,
        observed.st_ino,
        observed.st_mode,
        observed.st_size,
        observed.st_mtime_ns,
        observed.st_ctime_ns,
    )


def _read_regular_snapshot_at(
    parent_fd: int,
    name: str,
) -> _ApprovedWorktreeSnapshot:
    try:
        fd = os.open(
            name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise ValueError(
            f"generated artifact is missing, symlinked, or unreadable: {name}"
        ) from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"generated artifact is not a regular file: {name}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if before_identity != after_identity:
            raise RuntimeError(f"generated artifact changed while being read: {name}")
        return (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            b"".join(chunks),
        )
    finally:
        os.close(fd)


def _read_checkout_snapshot(root: Path, path: Path) -> _ApprovedWorktreeSnapshot:
    parent_fd = _open_checkout_parent(root, path)
    try:
        return _read_regular_snapshot_at(parent_fd, path.name)
    finally:
        os.close(parent_fd)


def _rename_exchange(parent_fd: int, source: str, destination: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable")
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    if (
        renameat2(
            parent_fd,
            os.fsencode(source),
            parent_fd,
            os.fsencode(destination),
            RENAME_EXCHANGE,
        )
        != 0
    ):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _rollback_exchanged_restore(
    parent_fd: int,
    temporary_name: str,
    destination_name: str,
    *,
    approved_worktree: _ApprovedWorktreeSnapshot,
    staged: _ApprovedWorktreeSnapshot,
    displaced: _ApprovedWorktreeSnapshot | None,
    published: _ApprovedWorktreeSnapshot | None,
    display_path: Path,
    validation_error: Exception | None,
) -> bool:
    try:
        _rename_exchange(parent_fd, temporary_name, destination_name)
    except OSError as rollback_error:
        raise _PreserveRestoreStagingError(
            "generated artifact exchange rollback failed; "
            f"displaced worktree data preserved at {display_path.parent / temporary_name}"
        ) from rollback_error

    try:
        restored = _read_regular_snapshot_at(parent_fd, destination_name)
        expected_restored = (
            displaced if displaced is not None else approved_worktree
        )
        if not _same_snapshot_across_rename(restored, expected_restored):
            raise _PreserveRestoreStagingError(
                "generated artifact rollback could not restore the displaced worktree output; "
                f"recovery entry preserved at {display_path.parent / temporary_name}"
            )
        confirmed_restored = _read_regular_snapshot_at(parent_fd, destination_name)
        if confirmed_restored != restored:
            raise _PreserveRestoreStagingError(
                "generated artifact restored output changed during validation; "
                f"recovery entry preserved at {display_path.parent / temporary_name}"
            )
    except _PreserveRestoreStagingError:
        raise
    except Exception as restore_error:
        raise _PreserveRestoreStagingError(
            "generated artifact rollback could not validate the restored worktree output; "
            f"recovery entry preserved at {display_path.parent / temporary_name}"
        ) from restore_error

    try:
        if published is None:
            if _entry_identity_no_follow(parent_fd, temporary_name) is None:
                raise _PreserveRestoreStagingError(
                    "generated artifact rollback lost the unexpected restore entry"
                ) from validation_error
            raise _PreserveRestoreStagingError(
                "generated artifact restore publication was invalid; worktree output restored "
                f"and unexpected entry preserved at {display_path.parent / temporary_name}"
            ) from validation_error

        try:
            returned_staged = _read_regular_snapshot_at(parent_fd, temporary_name)
        except Exception as recovery_error:
            if _entry_identity_no_follow(parent_fd, temporary_name) is None:
                raise _PreserveRestoreStagingError(
                    "generated artifact rollback lost the unexpected restore entry"
                ) from recovery_error
            raise _PreserveRestoreStagingError(
                "generated artifact worktree output was restored; invalid restore entry "
                f"preserved at {display_path.parent / temporary_name}"
            ) from recovery_error
        if not _same_snapshot_across_rename(returned_staged, published):
            raise _PreserveRestoreStagingError(
                "generated artifact rollback could not verify the recovery entry; "
                f"entry preserved at {display_path.parent / temporary_name}"
            ) from validation_error
        confirmed_returned = _read_regular_snapshot_at(parent_fd, temporary_name)
        if confirmed_returned != returned_staged:
            raise _PreserveRestoreStagingError(
                "generated artifact recovery entry changed during validation; "
                f"entry preserved at {display_path.parent / temporary_name}"
            ) from validation_error
        if validation_error is not None or not _same_snapshot_across_rename(
            returned_staged, staged
        ):
            raise _PreserveRestoreStagingError(
                "generated artifact restore staging path changed at publication; "
                f"unexpected staged data preserved at {display_path.parent / temporary_name}"
            ) from validation_error
        os.unlink(temporary_name, dir_fd=parent_fd)
        return False
    except _PreserveRestoreStagingError:
        raise
    except Exception as verification_error:
        raise _PreserveRestoreStagingError(
            "generated artifact post-rollback verification failed; "
            f"recovery entry preserved at {display_path.parent / temporary_name}"
        ) from verification_error


def _publish_staged_restore(
    parent_fd: int,
    temporary_name: str,
    destination_name: str,
    *,
    approved_worktree: _ApprovedWorktreeSnapshot,
    staged: _ApprovedWorktreeSnapshot,
    display_path: Path,
) -> bool:
    _rename_exchange(parent_fd, temporary_name, destination_name)
    displaced = None
    published = None
    try:
        displaced = _read_regular_snapshot_at(parent_fd, temporary_name)
        published = _read_regular_snapshot_at(parent_fd, destination_name)
    except Exception as validation_error:
        return _rollback_exchanged_restore(
            parent_fd,
            temporary_name,
            destination_name,
            approved_worktree=approved_worktree,
            staged=staged,
            displaced=displaced,
            published=published,
            display_path=display_path,
            validation_error=validation_error,
        )

    if _same_snapshot_across_rename(
        displaced, approved_worktree
    ) and _same_snapshot_across_rename(published, staged):
        try:
            confirmed_displaced = _read_regular_snapshot_at(parent_fd, temporary_name)
            confirmed_published = _read_regular_snapshot_at(parent_fd, destination_name)
        except Exception as validation_error:
            return _rollback_exchanged_restore(
                parent_fd,
                temporary_name,
                destination_name,
                approved_worktree=approved_worktree,
                staged=staged,
                displaced=displaced,
                published=published,
                display_path=display_path,
                validation_error=validation_error,
            )
        if confirmed_displaced != displaced or confirmed_published != published:
            return _rollback_exchanged_restore(
                parent_fd,
                temporary_name,
                destination_name,
                approved_worktree=approved_worktree,
                staged=staged,
                displaced=displaced,
                published=published,
                display_path=display_path,
                validation_error=RuntimeError(
                    "generated artifact exchange changed during validation"
                ),
            )
        try:
            os.unlink(temporary_name, dir_fd=parent_fd)
        except Exception as cleanup_error:
            raise _PreserveRestoreStagingError(
                "restored generated artifact, but displaced output could not be removed; "
                f"preserved at {display_path.parent / temporary_name}"
            ) from cleanup_error
        return True

    return _rollback_exchanged_restore(
        parent_fd,
        temporary_name,
        destination_name,
        approved_worktree=approved_worktree,
        staged=staged,
        displaced=displaced,
        published=published,
        display_path=display_path,
        validation_error=None,
    )


def _restore_exact_head_bytes(
    root: Path,
    path: Path,
    payload: bytes,
    *,
    approved_worktree: _ApprovedWorktreeSnapshot,
) -> None:
    parent_fd = _open_checkout_parent(root, path)
    temporary_name = ""
    try:
        for _attempt in range(10):
            temporary_name = f".{path.name}.release-restore-{secrets.token_hex(8)}"
            try:
                temporary_fd = os.open(
                    temporary_name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_NOFOLLOW
                    | os.O_CLOEXEC,
                    0o600,
                    dir_fd=parent_fd,
                )
                break
            except FileExistsError:
                temporary_name = ""
        else:
            raise FileExistsError(f"unable to allocate restore file for {path.as_posix()}")

        try:
            remaining = memoryview(payload)
            while remaining:
                written = os.write(temporary_fd, remaining)
                if written <= 0:
                    raise OSError(f"short write while restoring {path.as_posix()}")
                remaining = remaining[written:]
            os.fchmod(temporary_fd, 0o644)
            os.fsync(temporary_fd)
        finally:
            os.close(temporary_fd)

        staged = _read_regular_snapshot_at(parent_fd, temporary_name)
        try:
            consumed = _publish_staged_restore(
                parent_fd,
                temporary_name,
                path.name,
                approved_worktree=approved_worktree,
                staged=staged,
                display_path=path,
            )
        except _PreserveRestoreStagingError:
            temporary_name = ""
            try:
                os.fsync(parent_fd)
            finally:
                raise
        temporary_name = ""
        os.fsync(parent_fd)
        if not consumed:
            raise RuntimeError(
                f"{path.as_posix()} changed after semantic approval; refusing restoration"
            )
    finally:
        try:
            if temporary_name:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
        finally:
            os.close(parent_fd)


def _release_manifest_checkout_failures(root: Path = ROOT) -> list[str]:
    manifest = root / RELEASE_MANIFEST_PATH
    type_failure = _checkout_regular_file_failure(
        root,
        RELEASE_MANIFEST_PATH,
        label="release manifest checkout",
    )
    if type_failure is not None:
        return [type_failure]
    try:
        head_entry = _git_tree_entry(root, "HEAD", RELEASE_MANIFEST_PATH)
        index_entry = _git_index_entry(root, RELEASE_MANIFEST_PATH)
        head_bytes = _git_show_bytes(root, "HEAD", RELEASE_MANIFEST_PATH)
        index_bytes = _git_show_index_bytes(root, RELEASE_MANIFEST_PATH)
        worktree_bytes = manifest.read_bytes()
    except Exception as exc:
        return [f"release manifest checkout could not be verified: {exc}"]
    failures: list[str] = []
    if head_entry[0] != "100644":
        failures.append("release manifest HEAD mode is not 100644")
    if index_entry != head_entry or index_bytes != head_bytes:
        failures.append("release manifest staged content differs from HEAD")
    if worktree_bytes != head_bytes:
        failures.append("release manifest worktree content differs from HEAD")
    if not _checkout_permissions_are_safe(manifest.lstat().st_mode):
        failures.append("release manifest worktree mode is unsafe")
    return failures


def _release_artifact_set_identity(
    root: Path = ROOT,
    *,
    revision: str | None = None,
) -> str:
    entries: list[dict[str, object]] = []
    for path in GENERATED_ARTIFACTS:
        payload = (
            _git_show_bytes(root, revision, path)
            if revision is not None
            else (root / path).read_bytes()
        )
        entries.append(
            {
                "path": path.as_posix(),
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    canonical = json.dumps(
        entries,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"{RELEASE_ARTIFACT_SET_PREFIX}{hashlib.sha256(canonical).hexdigest()}"


def _release_manifest_expected_values(
    root: Path = ROOT,
    *,
    revision: str | None = None,
) -> tuple[dict[str, str], list[str]]:
    issues: list[str] = []
    receipt_path = root / GENERATED_ARTIFACTS[0]
    try:
        receipt_bytes = (
            _git_show_bytes(root, revision, GENERATED_ARTIFACTS[0])
            if revision is not None
            else receipt_path.read_bytes()
        )
        receipt = _load_json_object(
            receipt_bytes,
            label="release authority receipt",
        )
    except Exception as exc:
        return {}, [f"release authority receipt is missing or invalid: {exc}"]

    source_binding = receipt.get("source_binding")
    if not isinstance(source_binding, dict):
        source_binding = {}
    raw_runtime_commit_sha = source_binding.get("code_commit")
    runtime_commit_sha = (
        raw_runtime_commit_sha
        if type(raw_runtime_commit_sha) is str
        and raw_runtime_commit_sha == raw_runtime_commit_sha.strip()
        else ""
    )
    raw_generated_at = receipt.get("generated_at")
    generated_at = (
        raw_generated_at
        if type(raw_generated_at) is str
        and raw_generated_at == raw_generated_at.strip()
        else ""
    )
    if not _FULL_GIT_SHA.fullmatch(runtime_commit_sha):
        issues.append("release authority receipt runtime commit SHA is missing or invalid")
    if not _is_valid_rfc3339_utc_seconds(generated_at):
        issues.append("release authority receipt generated_at is missing or not UTC RFC3339 seconds")

    try:
        artifact_set = _release_artifact_set_identity(root, revision=revision)
    except Exception as exc:
        issues.append(f"release artifact set is missing or unreadable: {exc}")
        artifact_set = ""

    expected = dict(RELEASE_MANIFEST_STATIC_VALUES)
    expected.update(
        {
            "release_commit_sha": runtime_commit_sha,
            "release_artifact_set": artifact_set,
            "release_label": (
                f"propertyquarry-source-browser-candidate-{runtime_commit_sha[:12]}"
                if runtime_commit_sha
                else ""
            ),
            "release_deployment_id": (
                f"propertyquarry-governed-deploy-{runtime_commit_sha[:12]}"
                if runtime_commit_sha
                else ""
            ),
            "release_generated_at": generated_at,
        }
    )
    return expected, issues


def _parse_release_manifest(text: str) -> tuple[dict[str, str], list[str]]:
    if (
        text.count(RELEASE_MANIFEST_JSON_START) != 1
        or text.count(RELEASE_MANIFEST_JSON_END) != 1
    ):
        return {}, ["release manifest must contain exactly one marked canonical JSON authority"]
    if text.index(RELEASE_MANIFEST_JSON_START) > text.index(RELEASE_MANIFEST_JSON_END):
        return {}, ["release manifest canonical JSON markers are out of order"]
    before_end, after_end = text.split(RELEASE_MANIFEST_JSON_END, 1)
    before_start, marked = before_end.split(RELEASE_MANIFEST_JSON_START, 1)
    if RELEASE_MANIFEST_JSON_END in before_start or RELEASE_MANIFEST_JSON_START in after_end:
        return {}, ["release manifest canonical JSON markers are out of order"]
    fenced = re.fullmatch(r"\s*```json\s*\n(?P<body>.*)\n```\s*", marked, flags=re.DOTALL)
    if fenced is None:
        return {}, ["release manifest canonical authority must be one exact JSON code fence"]

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        payload: dict[str, object] = {}
        for key, value in pairs:
            if key in payload:
                raise ValueError(f"release manifest authority field is duplicated: {key}")
            payload[key] = value
        return payload

    try:
        raw = json.loads(
            fenced.group("body"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as exc:
        return {}, [f"release manifest canonical JSON is invalid: {exc.msg}"]
    except ValueError as exc:
        return {}, [str(exc)]
    if not isinstance(raw, dict):
        return {}, ["release manifest canonical JSON root must be an object"]
    values: dict[str, str] = {}
    issues: list[str] = []
    for key, value in raw.items():
        if not isinstance(value, str):
            continue
        normalized = value.strip()
        values[key] = normalized
        if normalized != value:
            issues.append(
                f"release manifest authority field contains surrounding whitespace: {key}"
            )
    non_string = sorted(str(key) for key, value in raw.items() if not isinstance(value, str))
    issues.extend(
        f"release manifest authority field must be a string: {key}"
        for key in non_string
    )
    return values, issues


def _release_manifest_shape_issues(values: dict[str, str]) -> list[str]:
    issues: list[str] = []
    expected_fields = set(RELEASE_MANIFEST_FIELDS)
    for field in RELEASE_MANIFEST_FIELDS:
        if field not in values:
            issues.append(f"release manifest authority field is missing: {field}")
        elif not values[field]:
            issues.append(f"release manifest authority field is empty: {field}")
        elif values[field] != values[field].strip():
            issues.append(
                f"release manifest authority field contains surrounding whitespace: {field}"
            )
        elif any(ord(char) < 32 for char in values[field]):
            issues.append(f"release manifest authority field contains control text: {field}")
    for field in sorted(set(values) - expected_fields):
        issues.append(f"release manifest authority field is unexpected: {field}")
    if values.get("release_manifest_schema") not in {None, "", RELEASE_MANIFEST_SCHEMA}:
        issues.append("release manifest schema is invalid")
    commit_sha = values.get("release_commit_sha", "")
    if commit_sha and _FULL_GIT_SHA.fullmatch(commit_sha) is None:
        issues.append("release manifest runtime commit SHA is invalid")
    generated_at = values.get("release_generated_at", "")
    if generated_at and not _is_valid_rfc3339_utc_seconds(generated_at):
        issues.append("release manifest generated_at is not UTC RFC3339 seconds")
    artifact_set = values.get("release_artifact_set", "")
    if artifact_set and _ARTIFACT_SET.fullmatch(artifact_set) is None:
        issues.append("release manifest artifact set identity is invalid")
    return issues


def release_manifest_sha256(values: dict[str, str]) -> str:
    issues = _release_manifest_shape_issues(values)
    if issues:
        raise ValueError("; ".join(issues))
    canonical = json.dumps(
        values,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def load_release_manifest(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"release manifest is missing or unreadable: {type(exc).__name__}") from exc
    values, issues = _parse_release_manifest(text)
    issues.extend(_release_manifest_shape_issues(values))
    if issues:
        raise ValueError("; ".join(dict.fromkeys(issues)))
    return values


def _validate_release_manifest_values(
    observed: dict[str, str],
    expected: dict[str, str],
) -> list[str]:
    issues: list[str] = []
    for label, expected_value in expected.items():
        observed_value = observed.get(label)
        if observed_value is None:
            issues.append(f"release manifest authority field is missing: {label}")
        elif not observed_value:
            issues.append(f"release manifest authority field is empty: {label}")
        elif observed_value != expected_value:
            issues.append(f"release manifest authority field mismatches current evidence: {label}")
    for label in sorted(set(observed) - set(expected)):
        issues.append(f"release manifest authority field is unexpected: {label}")
    return issues


def verify_release_manifest(
    root: Path = ROOT,
    *,
    generated_artifact_revision: str | None = None,
) -> list[str]:
    expected, issues = _release_manifest_expected_values(
        root,
        revision=generated_artifact_revision,
    )
    manifest_path = root / RELEASE_MANIFEST_PATH
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except Exception as exc:
        return [*issues, f"release manifest is missing or unreadable: {exc}"]
    observed, parse_issues = _parse_release_manifest(text)
    issues.extend(parse_issues)
    issues.extend(_release_manifest_shape_issues(observed))
    issues.extend(_validate_release_manifest_values(observed, expected))
    return list(dict.fromkeys(issues))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify generated PropertyQuarry release artifacts against HEAD "
            "without changing the checkout."
        )
    )
    parser.add_argument(
        "--restore-exact-head",
        action="store_true",
        help=(
            "Explicitly restore semantically equivalent byte or mode drift to "
            "the exact HEAD artifact after all manifest checks pass."
        ),
    )
    return parser


def main(argv: Sequence[str] = ()) -> int:
    args = _parser().parse_args(list(argv))
    failures: list[str] = []
    paths_to_restore: list[Path] = []
    head_bytes_by_path: dict[Path, bytes] = {}
    approved_worktree_by_path: dict[Path, _ApprovedWorktreeSnapshot] = {}
    for path in GENERATED_ARTIFACTS:
        type_failure = _checkout_regular_file_failure(
            ROOT,
            path,
            label=f"generated artifact {path.as_posix()}",
        )
        if type_failure is not None:
            failures.append(type_failure)
            continue
        try:
            head_entry = _git_tree_entry(ROOT, "HEAD", path)
            index_entry = _git_index_entry(ROOT, path)
            head_bytes = _git_show_bytes(ROOT, "HEAD", path)
            index_bytes = _git_show_index_bytes(ROOT, path)
            worktree_snapshot = _read_checkout_snapshot(ROOT, path)
            worktree_bytes = worktree_snapshot[6]
            head_payload = _load_json_object(
                head_bytes,
                label=f"HEAD artifact {path.as_posix()}",
            )
            worktree_payload = _load_json_object(
                worktree_bytes,
                label=f"worktree artifact {path.as_posix()}",
            )
        except Exception as exc:
            failures.append(f"{path}: unable to load generated artifact: {exc}")
            continue
        if head_entry[0] != "100644":
            failures.append(f"{path}: HEAD artifact mode is not 100644")
            continue
        if index_entry != head_entry or index_bytes != head_bytes:
            failures.append(f"{path}: staged artifact differs from HEAD")
            continue
        if not _json_values_equal(
            _normalize(head_payload),
            _normalize(worktree_payload),
        ):
            failures.append(f"{path}: semantic drift from HEAD")
        else:
            head_bytes_by_path[path] = head_bytes
            worktree_mode = stat.S_IMODE(worktree_snapshot[2])
            byte_drift = worktree_bytes != head_bytes
            unsafe_mode = not _checkout_permissions_are_safe(worktree_mode)
            if byte_drift or unsafe_mode:
                if args.restore_exact_head:
                    paths_to_restore.append(path)
                    approved_worktree_by_path[path] = worktree_snapshot
                else:
                    drift = []
                    if byte_drift:
                        drift.append("bytes")
                    if unsafe_mode:
                        drift.append("mode")
                    failures.append(
                        f"{path}: exact HEAD {' and '.join(drift)} drift; "
                        "verification is read-only"
                    )

    failures.extend(_release_manifest_checkout_failures(ROOT))
    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1

    manifest_failures = verify_release_manifest(
        ROOT,
        generated_artifact_revision="HEAD",
    )
    if manifest_failures:
        for failure in manifest_failures:
            print(failure, file=sys.stderr)
        return 1
    for path in paths_to_restore:
        try:
            _restore_exact_head_bytes(
                ROOT,
                path,
                head_bytes_by_path[path],
                approved_worktree=approved_worktree_by_path[path],
            )
        except Exception as exc:
            failures.append(f"{path}: exact restoration failed: {exc}")
            continue
        type_failure = _checkout_regular_file_failure(
            ROOT,
            path,
            label=f"restored generated artifact {path.as_posix()}",
        )
        if type_failure is not None:
            failures.append(type_failure)
            continue
        if (ROOT / path).read_bytes() != head_bytes_by_path[path]:
            failures.append(f"{path}: restored artifact bytes differ from HEAD")
        if not _checkout_permissions_are_safe((ROOT / path).stat().st_mode):
            failures.append(f"{path}: restored artifact mode is unsafe")
    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1
    print("generated release artifacts are semantically clean")
    print("immutable release manifest authority is exact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
