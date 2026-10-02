#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes
import errno
import json
import math
import os
import re
import secrets
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__:
    from . import propertyquarry_release_proof_baseline as release_proof_baseline
    from .propertyquarry_release_receipt_binding import (
        ReleaseBindingError,
        SOURCE_BINDING_VERSION,
        build_source_binding,
        file_snapshot_binding,
        read_stable_regular_file,
    )
else:
    import propertyquarry_release_proof_baseline as release_proof_baseline
    from propertyquarry_release_receipt_binding import (
        ReleaseBindingError,
        SOURCE_BINDING_VERSION,
        build_source_binding,
        file_snapshot_binding,
        read_stable_regular_file,
    )


DEFAULT_SEED = Path(".codex-design/repo/EA_FLAGSHIP_RELEASE_GATE.json")
DEFAULT_TRUTH_PLANE = Path(".codex-design/repo/EA_FLAGSHIP_TRUTH_PLANE.md")
DEFAULT_OUTPUT = Path(".codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json")
DEFAULT_BROWSER_PROOF_RECEIPT = Path(".codex-studio/published/EA_BROWSER_WORKFLOW_PROOF.generated.json")
REQUIRED_DOCS = (
    Path("README.md"),
    Path("RUNBOOK.md"),
    Path("RELEASE_CHECKLIST.md"),
    Path("PRODUCT_RELEASE_CHECKLIST.md"),
    Path("docs/PROPERTYQUARRY_GLOBAL_FLAGSHIP_GOAL.md"),
)
PYTEST_OUTCOME_KEYS = ("passed", "failed", "skipped", "errors", "xfailed", "xpassed")
PYTEST_OUTCOME_RE = re.compile(
    r"\b(?P<count>\d+)\s+(?P<outcome>passed|failed|skipped|errors?|xfailed|xpassed)\b",
    re.IGNORECASE,
)
REQUIRED_JOURNEY_IDS = release_proof_baseline.APPROVED_REQUIRED_JOURNEY_IDS
REAL_BROWSER_TEST_FILE = release_proof_baseline.REAL_BROWSER_TEST_FILE
REQUIRED_PACKETS_TOURS_REAL_BROWSER_CASES = release_proof_baseline.PACKETS_TOURS_REAL_BROWSER_CASES
JOURNEY_MATRIX_VERSION = 1
RENAME_NOREPLACE = 1
RENAME_EXCHANGE = 2


class _PreserveStagedOutputError(RuntimeError):
    pass


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"duplicate JSON key: {key}")
        payload[key] = value
    return payload


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return {}
    return dict(payload) if type(payload) is dict else {}


def _object(value: object) -> dict[str, Any]:
    return value if type(value) is dict else {}


def _strict_string_list(value: object) -> list[str] | None:
    if type(value) is not list or any(
        type(item) is not str or not item.strip() for item in value
    ):
        return None
    return [item.strip() for item in value]


def _string_list(value: object) -> list[str]:
    strict = _strict_string_list(value)
    return strict if strict is not None else []


def _normalize_release_value(value: Any) -> Any:
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"generated_at", "created_at", "mtime_utc", "duration_seconds", "git_head"}:
                continue
            if key.endswith("_git_head"):
                continue
            if key == "review_due":
                continue
            normalized[key] = _normalize_release_value(item)
        return normalized
    if isinstance(value, list):
        return [_normalize_release_value(item) for item in value]
    return value


def _safe_output_path(root: Path, relative_path: Path) -> tuple[Path, Path]:
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or any(part in {"", ".", ".."} for part in relative_path.parts)
    ):
        raise ValueError(
            f"release artifact output is not a safe repository-relative path: {relative_path}"
        )
    resolved_root = root.resolve(strict=True)
    if not stat.S_ISDIR(resolved_root.lstat().st_mode):
        raise ValueError(f"release artifact root is not a directory: {root}")
    return resolved_root, relative_path


def _open_output_parent(root: Path, relative_path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    current_fd = os.open(root, flags)
    try:
        for part in relative_path.parts[:-1]:
            try:
                os.mkdir(part, mode=0o755, dir_fd=current_fd)
            except FileExistsError:
                pass
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except OSError as exc:
                raise ValueError(
                    f"release artifact output parent is symlinked or not a directory: {relative_path}"
                ) from exc
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _read_regular_output(
    parent_fd: int,
    name: str,
) -> tuple[tuple[int, int, int, int, int, int], bytes] | None:
    try:
        fd = os.open(
            name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(
            f"release artifact output is symlinked or unreadable: {name}"
        ) from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"release artifact output is not a regular file: {name}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
        identity = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        if identity != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise RuntimeError(f"release artifact output changed while being read: {name}")
        return identity, b"".join(chunks)
    finally:
        os.close(fd)


def _strict_json_object_bytes(payload: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError):
        return None
    return dict(value) if type(value) is dict else None


def _same_snapshot_across_rename(
    left: tuple[tuple[int, int, int, int, int, int], bytes] | None,
    right: tuple[tuple[int, int, int, int, int, int], bytes] | None,
) -> bool:
    if left is None or right is None:
        return False
    left_identity, left_bytes = left
    right_identity, right_bytes = right
    # Linux updates ctime when renameat2 moves an inode.  The other identity
    # fields and the bytes must survive an exchange unchanged.
    return left_identity[:5] == right_identity[:5] and left_bytes == right_bytes


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


def _renameat2(
    parent_fd: int,
    source: str,
    destination: str,
    flags: int,
) -> None:
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
            flags,
        )
        != 0
    ):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _rename_exchange(parent_fd: int, source: str, destination: str) -> None:
    _renameat2(parent_fd, source, destination, RENAME_EXCHANGE)


def _rename_noreplace(parent_fd: int, source: str, destination: str) -> None:
    _renameat2(parent_fd, source, destination, RENAME_NOREPLACE)


def _quarantine_unexpected_new_output(
    parent_fd: int,
    temporary_name: str,
    destination_name: str,
    *,
    published: tuple[tuple[int, int, int, int, int, int], bytes] | None,
    display_path: Path,
    validation_error: Exception,
) -> None:
    try:
        _rename_noreplace(parent_fd, destination_name, temporary_name)
    except OSError as rollback_error:
        raise _PreserveStagedOutputError(
            "unexpected release artifact output could not be quarantined; "
            f"canonical entry remains at {display_path}"
        ) from rollback_error
    try:
        if (
            _entry_identity_no_follow(parent_fd, destination_name) is not None
            or _entry_identity_no_follow(parent_fd, temporary_name) is None
        ):
            raise _PreserveStagedOutputError(
                "unexpected release artifact quarantine could not be verified; "
                f"recovery entry preserved at {display_path.parent / temporary_name}"
            ) from validation_error
        if published is not None:
            recovery = _read_regular_output(parent_fd, temporary_name)
            if not _same_snapshot_across_rename(recovery, published):
                raise _PreserveStagedOutputError(
                    "unexpected release artifact recovery changed during quarantine; "
                    f"entry preserved at {display_path.parent / temporary_name}"
                ) from validation_error
    except _PreserveStagedOutputError:
        raise
    except Exception as verification_error:
        raise _PreserveStagedOutputError(
            "unexpected release artifact quarantine verification failed; "
            f"recovery entry preserved at {display_path.parent / temporary_name}"
        ) from verification_error
    raise _PreserveStagedOutputError(
        "release artifact staging path changed during publication; "
        f"unexpected entry quarantined at {display_path.parent / temporary_name}"
    ) from validation_error


def _rollback_exchanged_output(
    parent_fd: int,
    temporary_name: str,
    destination_name: str,
    *,
    approved: tuple[tuple[int, int, int, int, int, int], bytes],
    staged: tuple[tuple[int, int, int, int, int, int], bytes],
    displaced: tuple[tuple[int, int, int, int, int, int], bytes] | None,
    published: tuple[tuple[int, int, int, int, int, int], bytes] | None,
    display_path: Path,
    validation_error: Exception | None,
) -> bool:
    try:
        _rename_exchange(parent_fd, temporary_name, destination_name)
    except OSError as rollback_error:
        raise _PreserveStagedOutputError(
            "release artifact exchange rollback failed; "
            f"displaced output preserved at {display_path.parent / temporary_name}"
        ) from rollback_error

    try:
        restored = _read_regular_output(parent_fd, destination_name)
        expected_restored = displaced if displaced is not None else approved
        if not _same_snapshot_across_rename(restored, expected_restored):
            raise _PreserveStagedOutputError(
                "release artifact exchange rollback could not restore the displaced output; "
                f"recovery entry preserved at {display_path.parent / temporary_name}"
            )
        confirmed_restored = _read_regular_output(parent_fd, destination_name)
        if confirmed_restored != restored:
            raise _PreserveStagedOutputError(
                "release artifact restored output changed during validation; "
                f"recovery entry preserved at {display_path.parent / temporary_name}"
            )
    except _PreserveStagedOutputError:
        raise
    except Exception as restore_error:
        raise _PreserveStagedOutputError(
            "release artifact exchange rollback could not validate the restored output; "
            f"recovery entry preserved at {display_path.parent / temporary_name}"
        ) from restore_error

    try:
        if published is None:
            if _entry_identity_no_follow(parent_fd, temporary_name) is None:
                raise _PreserveStagedOutputError(
                    "release artifact rollback lost the unexpected publication entry"
                ) from validation_error
            raise _PreserveStagedOutputError(
                "release artifact publication was invalid; canonical output restored and "
                f"unexpected entry preserved at {display_path.parent / temporary_name}"
            ) from validation_error

        try:
            returned_staged = _read_regular_output(parent_fd, temporary_name)
        except Exception as recovery_error:
            if _entry_identity_no_follow(parent_fd, temporary_name) is None:
                raise _PreserveStagedOutputError(
                    "release artifact rollback lost the unexpected publication entry"
                ) from recovery_error
            raise _PreserveStagedOutputError(
                "release artifact canonical output was restored; invalid publication entry "
                f"preserved at {display_path.parent / temporary_name}"
            ) from recovery_error
        if not _same_snapshot_across_rename(returned_staged, published):
            raise _PreserveStagedOutputError(
                "release artifact exchange rollback could not verify the recovery entry; "
                f"entry preserved at {display_path.parent / temporary_name}"
            ) from validation_error
        confirmed_returned = _read_regular_output(parent_fd, temporary_name)
        if confirmed_returned != returned_staged:
            raise _PreserveStagedOutputError(
                "release artifact recovery entry changed during validation; "
                f"entry preserved at {display_path.parent / temporary_name}"
            ) from validation_error
        if validation_error is not None or not _same_snapshot_across_rename(
            returned_staged, staged
        ):
            raise _PreserveStagedOutputError(
                "release artifact staging path changed at publication; "
                f"unexpected staged data preserved at {display_path.parent / temporary_name}"
            ) from validation_error
        os.unlink(temporary_name, dir_fd=parent_fd)
        return False
    except _PreserveStagedOutputError:
        raise
    except Exception as verification_error:
        raise _PreserveStagedOutputError(
            "release artifact post-rollback verification failed; "
            f"recovery entry preserved at {display_path.parent / temporary_name}"
        ) from verification_error


def _publish_staged_output(
    parent_fd: int,
    temporary_name: str,
    destination_name: str,
    *,
    approved: tuple[tuple[int, int, int, int, int, int], bytes] | None,
    staged: tuple[tuple[int, int, int, int, int, int], bytes],
    display_path: Path,
) -> bool:
    if approved is None:
        try:
            _rename_noreplace(parent_fd, temporary_name, destination_name)
        except FileExistsError as exc:
            raise RuntimeError(
                f"release artifact output appeared during publication: {display_path}"
            ) from exc
        published = None
        try:
            published = _read_regular_output(parent_fd, destination_name)
            confirmed = _read_regular_output(parent_fd, destination_name)
            if (
                not _same_snapshot_across_rename(published, staged)
                or confirmed != published
            ):
                raise RuntimeError("published output does not match staged output")
        except Exception as exc:
            _quarantine_unexpected_new_output(
                parent_fd,
                temporary_name,
                destination_name,
                published=published,
                display_path=display_path,
                validation_error=exc,
            )
        return True

    _rename_exchange(parent_fd, temporary_name, destination_name)
    displaced = None
    published = None
    try:
        displaced = _read_regular_output(parent_fd, temporary_name)
        published = _read_regular_output(parent_fd, destination_name)
    except Exception as validation_error:
        return _rollback_exchanged_output(
            parent_fd,
            temporary_name,
            destination_name,
            approved=approved,
            staged=staged,
            displaced=displaced,
            published=published,
            display_path=display_path,
            validation_error=validation_error,
        )

    if _same_snapshot_across_rename(
        displaced, approved
    ) and _same_snapshot_across_rename(published, staged):
        try:
            confirmed_displaced = _read_regular_output(parent_fd, temporary_name)
            confirmed_published = _read_regular_output(parent_fd, destination_name)
        except Exception as validation_error:
            return _rollback_exchanged_output(
                parent_fd,
                temporary_name,
                destination_name,
                approved=approved,
                staged=staged,
                displaced=displaced,
                published=published,
                display_path=display_path,
                validation_error=validation_error,
            )
        if confirmed_displaced != displaced or confirmed_published != published:
            return _rollback_exchanged_output(
                parent_fd,
                temporary_name,
                destination_name,
                approved=approved,
                staged=staged,
                displaced=displaced,
                published=published,
                display_path=display_path,
                validation_error=RuntimeError(
                    "release artifact exchange changed during validation"
                ),
            )
        try:
            os.unlink(temporary_name, dir_fd=parent_fd)
        except Exception as cleanup_error:
            raise _PreserveStagedOutputError(
                "published release artifact, but displaced output could not be removed; "
                f"preserved at {display_path.parent / temporary_name}"
            ) from cleanup_error
        return True

    return _rollback_exchanged_output(
        parent_fd,
        temporary_name,
        destination_name,
        approved=approved,
        staged=staged,
        displaced=displaced,
        published=published,
        display_path=display_path,
        validation_error=None,
    )


def _write_json_stable(
    path: Path,
    payload: dict[str, Any],
    *,
    root: Path | None = None,
) -> None:
    if root is None:
        root = path.parent
        relative_path = Path(path.name)
    else:
        relative_path = path
    root, relative_path = _safe_output_path(root, relative_path)
    serialized = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )
    parent_fd = _open_output_parent(root, relative_path)
    temporary_name = ""
    try:
        approved = _read_regular_output(parent_fd, relative_path.name)
        if approved is not None:
            _approved_identity, existing_bytes = approved
            existing = _strict_json_object_bytes(existing_bytes)
            semantically_equal = (
                existing is not None
                and _normalize_release_value(existing)
                == _normalize_release_value(payload)
            )
            if semantically_equal:
                if stat.S_IMODE(approved[0][2]) == 0o644:
                    return
                # Repair only the unsafe mode; stable writers retain the
                # already-approved representation and volatile metadata.
                serialized = existing_bytes

        for _attempt in range(10):
            temporary_name = (
                f".{relative_path.name}.release-write-{secrets.token_hex(8)}"
            )
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
            raise FileExistsError(
                f"unable to allocate release artifact output: {relative_path}"
            )
        try:
            remaining = memoryview(serialized)
            while remaining:
                written = os.write(temporary_fd, remaining)
                if written <= 0:
                    raise OSError(
                        f"short write while publishing release artifact: {relative_path}"
                    )
                remaining = remaining[written:]
            os.fchmod(temporary_fd, 0o644)
            os.fsync(temporary_fd)
        finally:
            os.close(temporary_fd)

        staged = _read_regular_output(parent_fd, temporary_name)
        if staged is None:
            raise RuntimeError(
                f"release artifact staging file disappeared: {relative_path}"
            )
        try:
            consumed = _publish_staged_output(
                parent_fd,
                temporary_name,
                relative_path.name,
                approved=approved,
                staged=staged,
                display_path=relative_path,
            )
        except _PreserveStagedOutputError:
            temporary_name = ""
            try:
                os.fsync(parent_fd)
            finally:
                raise
        temporary_name = ""
        os.fsync(parent_fd)
        if not consumed:
            raise RuntimeError(
                f"release artifact output changed before publication: {relative_path}"
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


def _present(root: Path, rel: Path) -> bool:
    if (
        rel.is_absolute()
        or not rel.parts
        or any(part in {"", ".", ".."} for part in rel.parts)
    ):
        return False
    try:
        root = root.resolve(strict=True)
        candidate = root / rel
        if candidate.resolve(strict=True) != candidate:
            return False
        return candidate.is_file() and not candidate.is_symlink()
    except OSError:
        return False


def _stringify_path(path: Path) -> str:
    return path.as_posix()


def _pytest_outcome_counts(output_excerpt: object) -> dict[str, int]:
    counts = {key: 0 for key in PYTEST_OUTCOME_KEYS}
    text = "\n".join(str(line) for line in output_excerpt or []) if isinstance(output_excerpt, list) else ""
    for line in reversed(text.splitlines()):
        matches = list(PYTEST_OUTCOME_RE.finditer(line))
        if not matches:
            continue
        for match in matches:
            outcome = match.group("outcome").lower()
            if outcome == "error":
                outcome = "errors"
            counts[outcome] = int(match.group("count"))
        break
    return counts


def _browser_lane_pass_is_supported(
    lane: object,
    *,
    expected_test_file: str,
    expected_cases: list[str],
) -> bool:
    if type(lane) is not dict or type(lane.get("status")) is not str:
        return False
    if lane["status"].strip().lower() != "pass":
        return False

    raw_cases = lane.get("cases")
    raw_counts = lane.get("outcome_counts")
    limitations = lane.get("limitations")
    if (
        type(raw_cases) is not list
        or any(type(item) is not str or not item.strip() for item in raw_cases)
        or type(raw_counts) is not dict
        or set(raw_counts) != set(PYTEST_OUTCOME_KEYS)
        or type(limitations) is not list
        or limitations
    ):
        return False

    integer_fields = ("required_case_count", "selected_count", "executed_count", "exit_code")
    if any(type(lane.get(field)) is not int or lane[field] < 0 for field in integer_fields):
        return False
    counts: dict[str, int] = {}
    for key in PYTEST_OUTCOME_KEYS:
        value = raw_counts.get(key)
        if type(value) is not int or value < 0:
            return False
        counts[key] = value

    duration_seconds = lane.get("duration_seconds")
    if (
        type(duration_seconds) not in {int, float}
        or not math.isfinite(duration_seconds)
        or duration_seconds < 0
    ):
        return False

    required_case_count = lane["required_case_count"]
    selected_count = lane["selected_count"]
    executed_count = lane["executed_count"]
    exit_code = lane["exit_code"]
    executed_outcomes = sum(
        counts[key] for key in ("passed", "failed", "errors", "xfailed", "xpassed")
    )
    return (
        bool(expected_cases)
        and type(lane.get("test_file")) is str
        and lane["test_file"].strip() == expected_test_file
        and raw_cases == expected_cases
        and required_case_count == len(expected_cases)
        and selected_count == required_case_count
        and executed_count == required_case_count
        and executed_count == executed_outcomes
        and selected_count == executed_count + counts["skipped"]
        and exit_code == 0
        and counts["passed"] == required_case_count
        and counts["failed"] == 0
        and counts["skipped"] == 0
        and counts["errors"] == 0
        and counts["xfailed"] == 0
        and counts["xpassed"] == 0
    )


def _journey_matrix_pass_blockers(receipt: dict[str, Any], seed: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    expected = seed.get("journey_evidence_matrix")
    actual = receipt.get("journey_evidence_matrix")
    if not isinstance(expected, dict):
        return ["current gate seed lacks the journey evidence matrix"]
    if not isinstance(actual, dict):
        return ["published pass lacks the journey evidence matrix"]
    expected_version = expected.get("version")
    actual_version = actual.get("version")
    if type(expected_version) is not int or expected_version != JOURNEY_MATRIX_VERSION:
        blockers.append("current gate seed has the wrong journey matrix version")
    if type(actual_version) is not int or actual_version != expected_version:
        blockers.append("published pass journey matrix has the wrong version")
    expected_id_items = expected.get("required_journey_ids")
    actual_id_items = actual.get("required_journey_ids")
    expected_ids = _strict_string_list(expected_id_items)
    actual_ids = _strict_string_list(actual_id_items)
    if expected_ids is None or actual_ids is None:
        blockers.append("published pass journey IDs must be governed lists")
        expected_ids = expected_ids or []
        actual_ids = actual_ids or []
    if expected_ids != list(REQUIRED_JOURNEY_IDS) or actual_ids != expected_ids:
        blockers.append("published pass journey IDs do not match the complete current matrix")
    if str(actual.get("status") or "").strip().lower() != "pass":
        blockers.append("published pass journey matrix is not passing")
    if str(actual.get("readiness_scope") or "").strip() != str(expected.get("readiness_scope") or "").strip():
        blockers.append("published pass journey matrix has the wrong readiness scope")
    source_binding = receipt.get("source_binding") if isinstance(receipt.get("source_binding"), dict) else {}
    runtime_commit = actual.get("runtime_commit_sha")
    source_commit = source_binding.get("code_commit")
    if (
        type(runtime_commit) is not str
        or type(source_commit) is not str
        or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", runtime_commit) is None
        or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", source_commit) is None
        or runtime_commit != source_commit
    ):
        blockers.append("published pass journey matrix is not bound to the browser receipt runtime commit")

    expected_row_items = expected.get("rows")
    actual_row_items = actual.get("rows")
    if not isinstance(expected_row_items, list) or not isinstance(actual_row_items, list):
        blockers.append("published pass journey rows must be governed lists")
        expected_row_items = expected_row_items if isinstance(expected_row_items, list) else []
        actual_row_items = actual_row_items if isinstance(actual_row_items, list) else []
    expected_row_list = [row for row in expected_row_items if isinstance(row, dict)]
    actual_row_list = [row for row in actual_row_items if isinstance(row, dict)]
    expected_rows = {
        str(row.get("journey_id") or "").strip(): row
        for row in expected_row_list
        if str(row.get("journey_id") or "").strip()
    }
    actual_rows = {
        str(row.get("journey_id") or "").strip(): row
        for row in actual_row_list
        if str(row.get("journey_id") or "").strip()
    }
    if (
        len(expected_row_list) != len(REQUIRED_JOURNEY_IDS)
        or len(expected_row_list) != len(expected_row_items)
        or len(expected_rows) != len(expected_row_list)
        or len(actual_row_list) != len(actual_row_items)
        or len(actual_row_list) != len(expected_row_list)
        or len(actual_rows) != len(actual_row_list)
        or set(expected_rows) != set(REQUIRED_JOURNEY_IDS)
        or set(actual_rows) != set(expected_rows)
    ):
        blockers.append("published pass journey rows do not exactly cover the current matrix")
        return blockers
    for journey_id in REQUIRED_JOURNEY_IDS:
        expected_row = expected_rows[journey_id]
        actual_row = actual_rows[journey_id]
        if str(actual_row.get("label") or "").strip() != str(expected_row.get("label") or "").strip():
            blockers.append(f"published pass journey {journey_id} has stale label metadata")
        expected_source_items = expected_row.get("evidence_sources")
        actual_source_items = actual_row.get("evidence_sources")
        if not isinstance(expected_source_items, list) or not isinstance(actual_source_items, list):
            blockers.append(f"published pass journey {journey_id} evidence nodes must be governed lists")
            expected_source_items = expected_source_items if isinstance(expected_source_items, list) else []
            actual_source_items = actual_source_items if isinstance(actual_source_items, list) else []
        expected_sources = [
            {
                "file": str(entry.get("file") or "").strip(),
                "cases": [str(case).strip() for case in entry.get("cases") or [] if str(case).strip()],
            }
            for entry in expected_source_items
            if isinstance(entry, dict)
        ]
        actual_sources = [
            {
                "file": str(entry.get("file") or "").strip(),
                "cases": [str(case).strip() for case in entry.get("cases") or [] if str(case).strip()],
            }
            for entry in actual_source_items
            if isinstance(entry, dict)
        ]
        if journey_id == "packets_tours":
            required_tour_sources = [
                {
                    "file": REAL_BROWSER_TEST_FILE,
                    "cases": list(REQUIRED_PACKETS_TOURS_REAL_BROWSER_CASES),
                }
            ]
            if expected_sources != required_tour_sources:
                blockers.append(
                    "current packets_tours journey does not map the exact ordered required tour cases"
                )
            if actual_sources != required_tour_sources:
                blockers.append(
                    "published pass packets_tours journey does not prove the exact ordered required tour cases"
                )
        if (
            len(expected_sources) != len(expected_source_items)
            or len(actual_sources) != len(actual_source_items)
            or actual_sources != expected_sources
        ):
            blockers.append(f"published pass journey {journey_id} has stale evidence nodes")
        if any(
            str(entry.get("lane_status") or "").strip().lower() != "pass"
            for entry in actual_source_items
            if isinstance(entry, dict)
        ):
            blockers.append(f"published pass journey {journey_id} has an incomplete evidence lane")
        if str(actual_row.get("proof_status") or "").strip().lower() != "pass":
            blockers.append(f"published pass journey {journey_id} did not complete")
        raw_row_blockers = actual_row.get("blocking_reasons")
        if type(raw_row_blockers) is not list or raw_row_blockers:
            blockers.append(f"published pass journey {journey_id} still reports blockers")
        expected_live = expected_row.get("live_requirement")
        if not isinstance(expected_live, dict):
            expected_live = {}
        if (
            str(expected_live.get("status") or "").strip().lower() != "not_evaluated"
            or not str(expected_live.get("authority") or "").strip()
            or str(expected_live.get("required_profile") or "").strip() != "launch"
        ):
            blockers.append(f"current journey {journey_id} lacks a fail-closed live authority")
        if actual_row.get("live_requirement") != expected_live:
            blockers.append(f"published pass journey {journey_id} has stale live requirements")
    return blockers


def browser_receipt_pass_blockers(receipt: dict[str, Any], seed: dict[str, Any]) -> list[str]:
    blockers: list[str] = list(release_proof_baseline.approved_seed_baseline_blockers(seed))
    proof_contract = seed.get("browser_workflow_proof")
    if not isinstance(proof_contract, dict):
        proof_contract = {}
    expected_target = str(proof_contract.get("proof_target") or "").strip()
    expected_product = str(seed.get("product") or "").strip()
    if not expected_target or not expected_product:
        return list(dict.fromkeys([*blockers, "current gate seed lacks a product or browser proof target"]))
    if str(receipt.get("contract_name") or "").strip() != "ea.browser_workflow_proof":
        blockers.append("published pass has the wrong browser proof contract")
    if str(receipt.get("kind") or "").strip() != "proof_receipt":
        blockers.append("published pass has the wrong browser proof receipt kind")
    if str(receipt.get("surface") or "").strip() != "browser_workflow_proof":
        blockers.append("published pass has the wrong browser proof surface")
    receipt_version = receipt.get("version")
    if type(receipt_version) is not int or receipt_version != 1:
        blockers.append("published pass has the wrong browser proof version")
    if str(receipt.get("generated_by") or "").strip() != "scripts/materialize_ea_browser_workflow_proof.py":
        blockers.append("published pass was not produced by the governed browser proof materializer")
    if receipt.get("approved_baseline") != release_proof_baseline.approved_baseline_binding():
        blockers.append("published pass is not bound to the immutable approved release-proof baseline")
    if str(receipt.get("product") or "").strip() != expected_product:
        blockers.append(f"published pass targets product {receipt.get('product') or 'missing'}, expected {expected_product}")
    if str(receipt.get("proof_target") or "").strip() != expected_target:
        blockers.append(
            f"published pass targets {receipt.get('proof_target') or 'missing'}, expected {expected_target}"
        )
    source_binding = receipt.get("source_binding")
    if not isinstance(source_binding, dict):
        blockers.append("published pass lacks an immutable source binding")
    else:
        source_binding_version = source_binding.get("version")
        if (
            type(source_binding_version) is not int
            or source_binding_version != SOURCE_BINDING_VERSION
        ):
            blockers.append("published pass has the wrong immutable source binding version")
        source_commit = source_binding.get("code_commit")
        if (
            type(source_commit) is not str
            or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", source_commit) is None
        ):
            blockers.append("published pass has an invalid immutable source binding commit")
    release_claim = seed.get("release_claim")
    if not isinstance(release_claim, dict):
        release_claim = {}
    expected_claim = str(release_claim.get("summary") or "").strip()
    if str(receipt.get("release_claim_summary") or "").strip() != expected_claim:
        blockers.append("published pass release claim does not match the current gate seed")
    raw_expected_signals = proof_contract.get("expected_browser_signals")
    expected_signals = _strict_string_list(raw_expected_signals)
    if expected_signals is None:
        blockers.append("current gate seed lacks a governed browser signals list")
        expected_signals = []
    raw_actual_signals = receipt.get("expected_browser_signals")
    actual_signals = _strict_string_list(raw_actual_signals)
    if actual_signals is None:
        blockers.append("published pass lacks a governed browser signals list")
        actual_signals = []
    if actual_signals != expected_signals:
        blockers.append("published pass browser signals do not match the current gate seed")
    raw_receipt_blockers = receipt.get("blocking_reasons")
    receipt_blockers = _strict_string_list(raw_receipt_blockers)
    if receipt_blockers is None:
        blockers.append("published pass lacks a governed blocking_reasons list")
        receipt_blockers = []
    if receipt_blockers:
        blockers.append("published pass still reports browser blockers: " + "; ".join(receipt_blockers))
    raw_receipt_limitations = receipt.get("current_limitations")
    receipt_limitations = _strict_string_list(raw_receipt_limitations)
    if receipt_limitations is None:
        blockers.append("published pass lacks a governed current_limitations list")
        receipt_limitations = []
    if receipt_limitations:
        blockers.append("published pass still reports browser limitations: " + "; ".join(receipt_limitations))
    blockers.extend(_journey_matrix_pass_blockers(receipt, seed))

    raw_sources = proof_contract.get("evidence_sources")
    if type(raw_sources) is not list:
        blockers.append(
            "current gate seed browser evidence sources must be a governed list"
        )
        return blockers
    if any(not isinstance(entry, dict) for entry in raw_sources):
        blockers.append("current gate seed browser evidence sources must be a complete governed list")
        return blockers
    sources: list[dict[str, Any]] = []
    for entry in raw_sources:
        test_file = str(entry.get("file") or "").strip()
        raw_cases = entry.get("cases")
        cases = (
            [case.strip() for case in raw_cases if isinstance(case, str) and case.strip()]
            if isinstance(raw_cases, list)
            else []
        )
        if (
            not test_file
            or not isinstance(raw_cases, list)
            or len(cases) != len(raw_cases)
            or not cases
            or len(cases) != len(set(cases))
        ):
            blockers.append("current gate seed contains an incomplete or duplicate browser evidence node")
            return blockers
        sources.append({"file": test_file, "cases": cases})
    source_files = [str(entry["file"]) for entry in sources]
    if len(source_files) != len(set(source_files)):
        blockers.append("current gate seed contains duplicate browser evidence sources")
        return blockers
    source_backed = [entry for entry in sources if "/e2e/" not in str(entry.get("file") or "")]
    real_browser = [entry for entry in sources if "/e2e/" in str(entry.get("file") or "")]
    if not source_backed or len(real_browser) != 1:
        blockers.append("current gate seed must define at least one source-backed and exactly one real-browser proof source")
        return blockers

    raw_source_lanes = receipt.get("source_backed_journey_proofs")
    if not isinstance(raw_source_lanes, list):
        blockers.append("published pass lacks the complete source-backed browser journey proof list")
        source_lanes: list[object] = []
    else:
        source_lanes = list(raw_source_lanes)
    if len(source_lanes) != len(source_backed):
        blockers.append("published pass source-backed proof lanes do not exactly match the current gate seed")
    for index, expected in enumerate(source_backed):
        lane = source_lanes[index] if index < len(source_lanes) else None
        expected_file = str(expected.get("file") or "").strip()
        expected_cases = _strict_string_list(expected.get("cases"))
        if expected_cases is None:
            blockers.append(f"current gate seed has invalid {label} cases")
            expected_cases = []
        if not _browser_lane_pass_is_supported(
            lane,
            expected_test_file=expected_file,
            expected_cases=expected_cases,
        ):
            blockers.append(f"published pass lacks completed source-backed browser journey proof: {expected_file}")
    if source_lanes and receipt.get("source_backed_journey_proof") != source_lanes[0]:
        blockers.append("published pass legacy source-backed proof does not match the primary governed lane")

    expected_browser = real_browser[0]
    expected_browser_file = str(expected_browser.get("file") or "").strip()
    expected_browser_cases = [
        str(item) for item in expected_browser.get("cases") or [] if str(item).strip()
    ]
    if not _browser_lane_pass_is_supported(
        receipt.get("real_browser_e2e_proof"),
        expected_test_file=expected_browser_file,
        expected_cases=expected_browser_cases,
    ):
        blockers.append("published pass lacks completed real browser E2E proof")
    return list(dict.fromkeys(blockers))


def _build_browser_sources(root: Path, seed: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    proof_contract = _object(seed.get("browser_workflow_proof"))
    raw_sources = proof_contract.get("evidence_sources")
    if type(raw_sources) is not list:
        return [], ["invalid browser evidence source list"]
    evidence_sources = raw_sources
    rendered: list[dict[str, Any]] = []
    missing: list[str] = []
    for index, entry in enumerate(evidence_sources):
        if type(entry) is not dict:
            missing.append(f"invalid browser evidence source {index}")
            continue
        rel = Path(str(entry.get("file") or "").strip())
        cases = _strict_string_list(entry.get("cases"))
        if cases is None or not cases:
            missing.append(f"invalid browser evidence cases {index}")
            cases = []
        present = _present(root, rel)
        rendered.append(
            {
                "file": rel.as_posix(),
                "present": present,
                "cases": cases,
            }
        )
        if not present:
            missing.append(rel.as_posix())
    return rendered, missing


def _build_doc_checks(root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    rendered: list[dict[str, Any]] = []
    missing: list[str] = []
    for rel in REQUIRED_DOCS:
        present = _present(root, rel)
        rendered.append({"path": rel.as_posix(), "present": present})
        if not present:
            missing.append(rel.as_posix())
    return rendered, missing


def _build_product_canon(root: Path, seed: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    canon = _object(seed.get("ea_product_canon"))
    source_root = str(canon.get("source_root") or "").strip()
    scope_label = str(canon.get("scope_label") or "EA product canon").strip() or "EA product canon"
    required_docs = _string_list(canon.get("required_docs"))
    if type(canon.get("required_docs")) is not list:
        missing_docs = ["invalid EA product canon required_docs"]
    else:
        missing_docs = []
    docs_present: list[dict[str, Any]] = []
    for doc in required_docs:
        rel = Path(doc)
        present = _present(root, rel)
        docs_present.append({"path": rel.as_posix(), "present": present})
        if not present:
            missing_docs.append(rel.as_posix())
    return {
        "source_root": source_root,
        "scope_label": scope_label,
        "required_docs": required_docs,
        "docs_present": docs_present,
        "all_required_docs_present": not missing_docs,
    }, missing_docs


def _project_journey_evidence_matrix(
    seed: dict[str, Any],
    *,
    published_browser_receipt: dict[str, Any] | None,
    published_browser_receipt_status: str | None,
    source_binding: dict[str, Any] | None,
) -> dict[str, Any]:
    if isinstance(published_browser_receipt, dict) and published_browser_receipt_status == "pass":
        published_matrix = published_browser_receipt.get("journey_evidence_matrix")
        if isinstance(published_matrix, dict):
            return published_matrix

    raw_matrix = seed.get("journey_evidence_matrix")
    if not isinstance(raw_matrix, dict):
        raw_matrix = {}
    rendered_rows: list[dict[str, Any]] = []
    raw_rows = raw_matrix.get("rows")
    if type(raw_rows) is not list:
        raw_rows = []
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        rendered_sources: list[dict[str, Any]] = []
        raw_sources = row.get("evidence_sources")
        if type(raw_sources) is not list:
            raw_sources = []
        for entry in raw_sources:
            if not isinstance(entry, dict):
                continue
            rendered_sources.append(
                {
                    "file": str(entry.get("file") or "").strip(),
                    "cases": _string_list(entry.get("cases")),
                    "lane_status": "not_evaluated",
                }
            )
        rendered_rows.append(
            {
                "journey_id": str(row.get("journey_id") or "").strip(),
                "label": str(row.get("label") or "").strip(),
                "proof_status": "not_evaluated",
                "evidence_sources": rendered_sources,
                "live_requirement": row.get("live_requirement") if isinstance(row.get("live_requirement"), dict) else {},
                "blocking_reasons": [],
            }
        )
    raw_version = raw_matrix.get("version")
    return {
        "version": raw_version if type(raw_version) is int else 0,
        "status": "not_evaluated",
        "readiness_scope": str(raw_matrix.get("readiness_scope") or "").strip(),
        "runtime_commit_sha": str((source_binding or {}).get("code_commit") or ""),
        "required_journey_ids": _string_list(raw_matrix.get("required_journey_ids")),
        "rows": rendered_rows,
    }


def build_receipt(
    root: Path,
    *,
    seed_path: Path = DEFAULT_SEED,
    truth_plane_path: Path = DEFAULT_TRUTH_PLANE,
    browser_proof_receipt_path: Path | None = DEFAULT_BROWSER_PROOF_RECEIPT,
    require_source_binding: bool = False,
) -> dict[str, Any]:
    seed_present = _present(root, seed_path)
    seed = _load_json(root / seed_path) if seed_present else {}
    approved_baseline_blockers = release_proof_baseline.approved_seed_baseline_blockers(seed)
    proof_contract = _object(seed.get("browser_workflow_proof"))
    proof_target = str(
        proof_contract.get("proof_target") or "executive-assistant"
    ).strip()
    proof_label = "PropertyQuarry" if proof_target == "propertyquarry" else "EA"
    truth_plane_present = _present(root, truth_plane_path)
    docs, missing_docs = _build_doc_checks(root)
    browser_sources, missing_browser_sources = _build_browser_sources(root, seed)
    product_canon, missing_canon_docs = _build_product_canon(root, seed)

    published_browser_receipt = None
    browser_receipt_status = None
    browser_receipt_path_value = None
    browser_receipt_blockers: list[str] = []
    browser_receipt_limitations: list[str] = []
    browser_receipt_snapshot = None
    source_binding: dict[str, Any] | None = None
    source_binding_blocker = ""
    if require_source_binding:
        try:
            source_binding = build_source_binding(
                root,
                seed_path=seed_path,
                evidence_sources=proof_contract.get("evidence_sources"),
            )
        except (OSError, TypeError, ReleaseBindingError) as exc:
            source_binding_blocker = f"immutable source binding failed: {exc}"
    browser_receipt_binding: dict[str, str] | None = None
    if browser_proof_receipt_path is not None:
        candidate = root / browser_proof_receipt_path
        browser_receipt_path_value = browser_proof_receipt_path.as_posix()
        if _present(root, browser_proof_receipt_path):
            try:
                if require_source_binding:
                    (
                        browser_receipt_snapshot,
                        browser_receipt_binding,
                    ) = file_snapshot_binding(
                        root,
                        browser_proof_receipt_path,
                    )
                else:
                    browser_receipt_snapshot = read_stable_regular_file(candidate)
                published_browser_receipt = (
                    _strict_json_object_bytes(browser_receipt_snapshot.payload)
                    or {}
                )
            except (OSError, ReleaseBindingError) as exc:
                published_browser_receipt = {}
                source_binding_blocker = (
                    source_binding_blocker
                    or f"browser receipt binding failed: {exc}"
                )
            browser_receipt_status = str(
                published_browser_receipt.get("status")
                or published_browser_receipt.get("state")
                or published_browser_receipt.get("release_truth")
                or ""
            ).strip()
            browser_receipt_blockers = _string_list(
                published_browser_receipt.get("blocking_reasons")
            )
            browser_receipt_limitations = _string_list(
                published_browser_receipt.get("current_limitations")
            )
            inconsistent_pass_blockers = browser_receipt_pass_blockers(published_browser_receipt, seed)
            if require_source_binding and published_browser_receipt.get("source_binding") != source_binding:
                inconsistent_pass_blockers.append(
                    "published pass immutable source binding does not match the current code commit"
                )
            if inconsistent_pass_blockers:
                browser_receipt_status = "blocked"
                browser_receipt_blockers.extend(inconsistent_pass_blockers)
        elif truth_plane_present:
            browser_receipt_status = None

    blockers: list[str] = [
        f"immutable approved release-proof baseline: {reason}"
        for reason in approved_baseline_blockers
    ]
    current_limitations: list[str] = []
    if not truth_plane_present:
        blockers.append(f"missing truth plane: {truth_plane_path.as_posix()}")
    if source_binding_blocker:
        blockers.append(source_binding_blocker)
    if missing_canon_docs:
        blockers.append("missing EA product canon docs: " + ", ".join(missing_canon_docs))
    if missing_docs:
        blockers.append("missing release docs: " + ", ".join(missing_docs))
    if missing_browser_sources:
        blockers.append("missing browser proof sources: " + ", ".join(missing_browser_sources))
    if published_browser_receipt is None:
        current_limitations.append("no published browser execution receipt is attached yet")
    else:
        current_limitations.extend(browser_receipt_limitations)
        if browser_receipt_status in {"blocked", "fail"}:
            if browser_receipt_blockers:
                blockers.extend("browser workflow proof: " + reason for reason in browser_receipt_blockers)
            else:
                blockers.append("browser workflow proof reported blocked status")
        elif browser_receipt_status == "preview_only" and not browser_receipt_limitations:
            current_limitations.append("browser workflow proof remains preview_only")

    status = "blocked" if blockers else "preview_only"
    if published_browser_receipt is not None and not blockers:
        if browser_receipt_status == "pass":
            status = "pass"
        elif browser_receipt_status in {"blocked", "fail"}:
            status = "blocked"

    release_claim = _object(seed.get("release_claim"))
    release_summary = str(release_claim.get("summary") or "").strip()
    blockers = list(dict.fromkeys(blockers))
    current_limitations = list(dict.fromkeys(current_limitations))
    journey_evidence_matrix = _project_journey_evidence_matrix(
        seed,
        published_browser_receipt=published_browser_receipt,
        published_browser_receipt_status=browser_receipt_status,
        source_binding=source_binding,
    )

    if status == "pass":
        operator_summary = (
            f"{proof_label} source/browser checkpoint is published and green; this does not establish global "
            "launch authority, and final live readiness is not evaluated by this receipt."
        )
    elif status == "preview_only":
        operator_summary = (
            f"{proof_label} source/browser flagship proof is materialized, but the current claim is preview_only "
            "until browser execution proof is published; this does not establish global launch authority, and "
            "final live readiness is not evaluated by this receipt."
        )
    else:
        operator_summary = (
            f"{proof_label} source/browser flagship proof is materialized, but the current browser-proof or "
            "release-doc state still blocks the claim; this does not establish global launch authority, and "
            "final live readiness is not evaluated by this receipt."
        )

    receipt: dict[str, Any] = {
        "product": str(seed.get("product") or "propertyquarry"),
        "surface": str(seed.get("surface") or "flagship_release_control"),
        "version": seed.get("version") if type(seed.get("version")) is int else 0,
        "kind": "release_receipt",
        "generated_at": _utc_now(),
        "generated_by": "scripts/materialize_ea_flagship_release_gate.py",
        "approved_baseline": release_proof_baseline.approved_baseline_binding(),
        "status": status,
        "readiness_scope": "source_and_browser_proof",
        "live_readiness": {
            "status": "not_evaluated",
            "authority": "_completion/property_gold_status/release-gate.json",
            "required_profile": "launch",
        },
        "global_launch_readiness": {
            "status": "not_evaluated",
            "market_envelope_authority": release_proof_baseline.GLOBAL_LAUNCH_MARKET_ENVELOPE_AUTHORITY,
            "terminal_command": release_proof_baseline.GLOBAL_LAUNCH_TERMINAL_COMMAND,
            "source_browser_checkpoint_is_sufficient": False,
        },
        "source_binding": source_binding,
        "browser_receipt_binding": browser_receipt_binding,
        "operator_summary": operator_summary,
        "truth_plane": {
            "source": truth_plane_path.as_posix(),
            "present": truth_plane_present,
            "legacy_history": _object(seed.get("truth_plane")).get("legacy_history"),
        },
        "release_claim": seed.get("release_claim") or {},
        "global_launch_contract": seed.get("global_launch_contract") or {},
        "ea_product_canon": product_canon,
        "browser_workflow_proof": {
            "proof_target": proof_target,
            "evidence_sources": (
                proof_contract.get("evidence_sources")
                if type(proof_contract.get("evidence_sources")) is list
                else []
            ),
            "source_files_present": browser_sources,
            "published_receipt": browser_receipt_path_value,
            "published_receipt_present": published_browser_receipt is not None,
        },
        "journey_evidence_matrix": journey_evidence_matrix,
        "verification_binding": {
            "primary_verifier": _object(seed.get("verification_binding")).get("primary_verifier", "scripts/verify_release_assets.sh"),
            "supporting_test": _object(seed.get("verification_binding")).get("supporting_test", "tests/test_flagship_truth_plane.py"),
            "materializer": "scripts/materialize_ea_flagship_release_gate.py",
        },
        "documentation_refs": [
            {"path": rel.as_posix(), "present": present}
            for rel, present in (
                (Path("README.md"), _present(root, Path("README.md"))),
                (Path("RUNBOOK.md"), _present(root, Path("RUNBOOK.md"))),
                (Path("RELEASE_CHECKLIST.md"), _present(root, Path("RELEASE_CHECKLIST.md"))),
                (Path("PRODUCT_RELEASE_CHECKLIST.md"), _present(root, Path("PRODUCT_RELEASE_CHECKLIST.md"))),
            )
        ],
        "release_docs": docs,
        "blocking_reasons": blockers,
        "current_limitations": current_limitations,
        "release_truth": {
            "oracle": truth_plane_path.as_posix(),
            "seed": seed_path.as_posix(),
            "summary": release_summary,
        },
    }
    if browser_receipt_snapshot is not None:
        browser_receipt_snapshot.assert_unchanged()
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description="Materialize the EA flagship release receipt.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help="EA repository root.")
    parser.add_argument("--seed", type=Path, default=DEFAULT_SEED, help="Path to the EA flagship release seed.")
    parser.add_argument("--truth-plane", type=Path, default=DEFAULT_TRUTH_PLANE, help="Path to the EA flagship truth plane.")
    parser.add_argument(
        "--browser-proof-receipt",
        type=Path,
        default=DEFAULT_BROWSER_PROOF_RECEIPT,
        help="Optional browser execution receipt to fold into the current status.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Path to write the generated receipt.")
    parser.add_argument("--stdout", action="store_true", help="Print the receipt to stdout instead of writing only to disk.")
    args = parser.parse_args()

    receipt = build_receipt(
        args.root.resolve(),
        seed_path=args.seed,
        truth_plane_path=args.truth_plane,
        browser_proof_receipt_path=args.browser_proof_receipt,
        require_source_binding=True,
    )

    output_path = args.root.resolve() / args.output
    _write_json_stable(args.output, receipt, root=args.root.resolve())
    if args.stdout:
        print(json.dumps(receipt, indent=2, ensure_ascii=False))
    else:
        print(json.dumps({"status": "ok", "output": output_path.as_posix(), "receipt_status": receipt["status"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
