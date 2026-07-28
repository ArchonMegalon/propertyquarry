#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
import re
import secrets
import shlex
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

if __package__:
    from .propertyquarry_release_receipt_binding import ReleaseBindingError, build_source_binding
else:
    from propertyquarry_release_receipt_binding import ReleaseBindingError, build_source_binding


DEFAULT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SEED = Path(".codex-design/repo/EA_FLAGSHIP_RELEASE_GATE.json")
DEFAULT_OUTPUT = Path(".codex-studio/published/EA_BROWSER_WORKFLOW_PROOF.generated.json")
SOURCE_BACKED_TEST_FILE = "tests/test_propertyquarry_workspace_redesign.py"
SOURCE_BACKED_CASES = [
    "test_propertyquarry_workspace_routes_render_greenfield_surfaces",
    "test_propertyquarry_failed_run_stays_on_activity_surface",
    "test_property_workspace_sign_out_clears_workspace_session_cookie",
    "test_property_saved_shortlist_candidates_persist_across_runs",
    "test_propertyquarry_account_exposes_working_lifecycle_controls",
    "test_propertyquarry_pricing_checkout_failure_copy_is_safe_and_accessible",
    "test_propertyquarry_public_home_survives_unreadable_optional_tour_media",
]
REAL_BROWSER_TEST_FILE = "tests/e2e/test_propertyquarry_greenfield_browser.py"
REAL_BROWSER_CASES = [
    "test_propertyquarry_greenfield_workspace_in_real_browser",
    "test_propertyquarry_greenfield_workspace_is_mobile_usable",
    "test_propertyquarry_expired_session_next_action_moves_keyboard_focus_to_sign_in_options",
    "test_propertyquarry_workbench_candidate_history_stays_in_place",
    "test_propertyquarry_flagship_operating_loop_in_browser",
    "test_propertyquarry_decision_to_clippy_to_packet_followup_flow_in_browser",
    "test_propertyquarry_packet_tracks_followup_state_in_browser",
    "test_propertyquarry_account_notifications_save_multi_channel_preferences_in_real_browser",
    "test_propertyquarry_browser_alert_button_toggles_enabled_state",
]
REQUIRED_JOURNEY_IDS = (
    "public_entry",
    "onboarding_auth",
    "search_ranking",
    "shortlist_research_revisit",
    "account_pricing_privacy_recovery",
    "packets_tours",
    "feedback",
    "notifications",
)
PYTEST_OUTCOME_KEYS = ("passed", "failed", "skipped", "errors", "xfailed", "xpassed")
RENAME_NOREPLACE = 1
RENAME_EXCHANGE = 2
VOLATILE_EXECUTION_KEYS = frozenset(
    {
        "as_of",
        "command",
        "created_at",
        "cwd",
        "duration_seconds",
        "generated_at",
        "git_branch",
        "git_head",
        "git_repo_root",
        "mtime_utc",
        "python_bin",
        "resolved_path",
        "review_due",
        "size_bytes",
        "source_path",
    }
)
PYTEST_OUTCOME_RE = re.compile(
    r"\b(?P<count>\d+)\s+(?P<outcome>passed|failed|skipped|errors?|xfailed|xpassed)\b",
    re.IGNORECASE,
)
PYTEST_ISOLATED_ENV_KEYS = (
    "DATABASE_URL",
    "EA_ALLOW_AUTHENTICATED_PRINCIPAL_HEADER",
    "EA_ALLOW_LOOPBACK_NO_AUTH",
    "EA_API_TOKEN",
    "EA_ARTIFACTS_DIR",
    "EA_CF_ACCESS_AUD",
    "EA_CF_ACCESS_TEAM_DOMAIN",
    "EA_DATABASE_URL",
    "EA_DEFAULT_PRINCIPAL_ID",
    "EA_HOST_PORT",
    "EA_LEDGER_BACKEND",
    "EA_MISMATCH_PRINCIPAL_ID",
    "EA_OPERATOR_PRINCIPAL_ID",
    "EA_OPERATOR_PRINCIPAL_IDS",
    "EA_OPERATOR_PRINCIPALS",
    "EA_PRINCIPAL_ID",
    "EA_RUNTIME_MODE",
    "EA_SIGNING_SECRET",
    "EA_STORAGE_BACKEND",
    "EA_STORAGE_FALLBACK_ALLOWED",
    "EA_TRUST_API_TOKEN_PRINCIPAL_HEADER",
    "EA_TRUST_AUTHENTICATED_PRINCIPAL_HEADER",
)


class _PreserveStagedOutputError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"duplicate JSON key: {key}")
        payload[key] = value
    return payload


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


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
            if key in VOLATILE_EXECUTION_KEYS:
                continue
            if key.endswith("_git_head"):
                continue
            if key == "output_excerpt":
                normalized[key] = []
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


def _resolve_python_bin(root: Path) -> str:
    # The caller chooses and authenticates the materializer interpreter.  In
    # particular, the release entrypoint runs this script with the pinned
    # PropertyQuarry release Python.  Do not abandon that trust boundary for a
    # checkout-local virtualenv merely because one happens to exist.
    del root
    return sys.executable


def _with_pythonpath(existing: str, root: Path) -> str:
    entries = [item for item in existing.split(os.pathsep) if item]
    for candidate in ("ea", (root / "ea").as_posix()):
        if candidate not in entries:
            entries.insert(0, candidate)
    return os.pathsep.join(entries)


def _truncate_output(text: str, *, limit: int = 40) -> list[str]:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return lines[:limit]


def _pytest_outcome_counts(text: str) -> dict[str, int]:
    counts = {key: 0 for key in PYTEST_OUTCOME_KEYS}
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


def _extract_limitations(text: str, *, real_browser: bool) -> list[str]:
    lowered = text.lower()
    limitations: list[str] = []
    if "no module named pytest" in lowered:
        limitations.append("pytest is not installed in the selected Python environment")
    if "no module named 'uvicorn'" in lowered or 'no module named "uvicorn"' in lowered:
        limitations.append("uvicorn is not installed in the selected Python environment")
    if "no module named 'playwright'" in lowered or 'no module named "playwright"' in lowered:
        limitations.append("playwright is not installed in the selected Python environment")
    if "executable doesn't exist" in lowered or "browser_type.launch" in lowered:
        limitations.append("playwright browser binaries are not installed")
    if "skipped" in lowered and not limitations:
        limitations.append(
            "real browser E2E did not run to completion"
            if real_browser
            else "source-backed browser journey proof did not run to completion"
        )
    return limitations


def _lane_completion(
    result: dict[str, Any],
    *,
    required_cases: list[str],
    real_browser: bool,
) -> dict[str, Any]:
    normalized = dict(result)
    raw_counts = normalized.get("outcome_counts")
    if type(raw_counts) is dict:
        counts = {
            key: (
                raw_counts.get(key)
                if type(raw_counts.get(key)) is int and raw_counts.get(key) >= 0
                else 0
            )
            for key in PYTEST_OUTCOME_KEYS
        }
        invalid_counts = (
            set(raw_counts) != set(PYTEST_OUTCOME_KEYS)
            or any(
                type(raw_counts.get(key)) is not int or raw_counts.get(key) < 0
                for key in PYTEST_OUTCOME_KEYS
            )
        )
    else:
        counts = _pytest_outcome_counts("\n".join(str(line) for line in normalized.get("output_excerpt") or []))
        invalid_counts = False

    required_case_count = len(required_cases)
    executed_count = sum(counts[key] for key in ("passed", "failed", "errors", "xfailed", "xpassed"))
    selected_count = executed_count + counts["skipped"]
    all_required_cases_passed = (
        type(normalized.get("exit_code")) is int
        and normalized.get("exit_code") == 0
        and not invalid_counts
        and required_case_count > 0
        and counts["passed"] >= required_case_count
        and counts["failed"] == 0
        and counts["skipped"] == 0
        and counts["errors"] == 0
        and counts["xfailed"] == 0
        and counts["xpassed"] == 0
    )

    reported_status = str(normalized.get("status") or "blocked").strip().lower()
    if reported_status == "pass" and not all_required_cases_passed:
        has_hard_failure = any(counts[key] for key in ("failed", "errors", "xfailed", "xpassed"))
        normalized["status"] = "preview_only" if real_browser and counts["skipped"] and not has_hard_failure else "blocked"
        limitations = _string_list(normalized.get("limitations"))
        if counts["skipped"]:
            limitations.append(
                "real browser E2E did not run to completion"
                if real_browser
                else "source-backed browser journey proof did not run to completion"
            )
        elif executed_count == 0:
            limitations.append(
                "required real browser E2E lane reported zero executed cases"
                if real_browser
                else "required source-backed browser journey lane reported zero executed cases"
            )
        else:
            limitations.append(
                "required real browser E2E cases did not all pass"
                if real_browser
                else "required source-backed browser journey cases did not all pass"
            )
        normalized["limitations"] = list(dict.fromkeys(limitations))

    normalized["required_case_count"] = required_case_count
    normalized["selected_count"] = selected_count
    normalized["executed_count"] = executed_count
    normalized["outcome_counts"] = counts
    normalized["limitations"] = _string_list(normalized.get("limitations"))
    return normalized


def _build_journey_evidence_matrix(
    seed: dict[str, Any],
    *,
    source_backed: dict[str, Any],
    real_browser: dict[str, Any],
    source_binding: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    raw_matrix = seed.get("journey_evidence_matrix")
    blockers: list[str] = []
    if not isinstance(raw_matrix, dict):
        return {
            "version": 1,
            "status": "blocked",
            "runtime_commit_sha": str((source_binding or {}).get("code_commit") or ""),
            "required_journey_ids": list(REQUIRED_JOURNEY_IDS),
            "rows": [],
        }, ["journey evidence matrix is missing"]

    raw_version = raw_matrix.get("version")
    version = raw_version if type(raw_version) is int else 0
    if version != 1:
        blockers.append("journey evidence matrix version must be 1")
    readiness_scope = str(raw_matrix.get("readiness_scope") or "").strip()
    if readiness_scope != "candidate_source_and_browser_proof":
        blockers.append("journey evidence matrix has the wrong readiness scope")
    raw_required_ids = raw_matrix.get("required_journey_ids")
    required_ids = _strict_string_list(raw_required_ids)
    if required_ids is None:
        required_ids = []
        blockers.append(
            "journey evidence matrix required IDs must contain only non-empty strings"
        )
    if required_ids != list(REQUIRED_JOURNEY_IDS):
        blockers.append("journey evidence matrix required IDs are missing, reordered, or unexpected")

    raw_row_items = raw_matrix.get("rows")
    if not isinstance(raw_row_items, list):
        raw_row_items = []
        blockers.append("journey evidence matrix rows must be a list")
    raw_rows = [row for row in raw_row_items if isinstance(row, dict)]
    if len(raw_rows) != len(raw_row_items):
        blockers.append("journey evidence matrix rows must contain only objects")
    rows_by_id: dict[str, dict[str, Any]] = {}
    duplicate_ids: set[str] = set()
    for row in raw_rows:
        journey_id = str(row.get("journey_id") or "").strip()
        if journey_id in rows_by_id:
            duplicate_ids.add(journey_id)
        elif journey_id:
            rows_by_id[journey_id] = row
    if duplicate_ids:
        blockers.append("journey evidence matrix has duplicate IDs: " + ", ".join(sorted(duplicate_ids)))
    if set(rows_by_id) != set(REQUIRED_JOURNEY_IDS):
        blockers.append("journey evidence matrix rows do not exactly cover the required journeys")

    lanes = {
        SOURCE_BACKED_TEST_FILE: source_backed,
        REAL_BROWSER_TEST_FILE: real_browser,
    }
    allowed_cases = {
        SOURCE_BACKED_TEST_FILE: set(SOURCE_BACKED_CASES),
        REAL_BROWSER_TEST_FILE: set(REAL_BROWSER_CASES),
    }
    mapped_cases = {path: set() for path in allowed_cases}
    rendered_rows: list[dict[str, Any]] = []
    for journey_id in REQUIRED_JOURNEY_IDS:
        row = rows_by_id.get(journey_id, {})
        row_blockers: list[str] = []
        label = str(row.get("label") or "").strip()
        if not label:
            row_blockers.append("label is missing")
        raw_evidence_sources = row.get("evidence_sources")
        if not isinstance(raw_evidence_sources, list):
            raw_evidence_sources = []
            row_blockers.append("evidence sources must be a list")
        evidence_sources = [entry for entry in raw_evidence_sources if isinstance(entry, dict)]
        if len(evidence_sources) != len(raw_evidence_sources):
            row_blockers.append("evidence sources must contain only objects")
        if not evidence_sources:
            row_blockers.append("evidence sources are missing")
        lane_statuses: list[str] = []
        rendered_sources: list[dict[str, Any]] = []
        for entry in evidence_sources:
            test_file = str(entry.get("file") or "").strip()
            raw_cases = entry.get("cases")
            cases = _strict_string_list(raw_cases)
            if cases is None:
                row_blockers.append(
                    "evidence source cases must be a list of non-empty strings: "
                    f"{test_file or 'missing'}"
                )
                continue
            lane = lanes.get(test_file)
            if lane is None:
                row_blockers.append(f"unsupported evidence source: {test_file or 'missing'}")
                continue
            if not cases:
                row_blockers.append(f"evidence source lacks cases: {test_file}")
                continue
            unexpected_cases = sorted(set(cases) - allowed_cases[test_file])
            if unexpected_cases:
                row_blockers.append(f"evidence source has ungoverned cases: {', '.join(unexpected_cases)}")
            mapped_cases[test_file].update(cases)
            lane_status = str(lane.get("status") or "blocked").strip().lower()
            lane_statuses.append(lane_status)
            rendered_sources.append(
                {
                    "file": test_file,
                    "cases": cases,
                    "lane_status": lane_status,
                }
            )

        live_requirement = row.get("live_requirement")
        if not isinstance(live_requirement, dict):
            live_requirement = {}
            row_blockers.append("live requirement is missing")
        live_status = str(live_requirement.get("status") or "").strip().lower()
        live_authority = str(live_requirement.get("authority") or "").strip()
        live_profile = str(live_requirement.get("required_profile") or "").strip()
        if live_status != "not_evaluated" or not live_authority or live_profile != "launch":
            row_blockers.append("live requirement must remain not_evaluated with a named launch authority")

        if row_blockers or any(status not in {"pass", "preview_only"} for status in lane_statuses):
            proof_status = "blocked"
        elif any(status == "preview_only" for status in lane_statuses):
            proof_status = "preview_only"
        else:
            proof_status = "pass"
        if row_blockers:
            blockers.extend(f"journey {journey_id}: {reason}" for reason in row_blockers)
        rendered_rows.append(
            {
                "journey_id": journey_id,
                "label": label,
                "proof_status": proof_status,
                "evidence_sources": rendered_sources,
                "live_requirement": {
                    "status": live_status,
                    "authority": live_authority,
                    "required_profile": live_profile,
                },
                "blocking_reasons": row_blockers,
            }
        )

    for test_file, expected_cases in allowed_cases.items():
        missing_cases = sorted(expected_cases - mapped_cases[test_file])
        extra_cases = sorted(mapped_cases[test_file] - expected_cases)
        if missing_cases or extra_cases:
            blockers.append(
                f"journey evidence matrix does not exactly map {test_file}: "
                f"missing={','.join(missing_cases) or 'none'}; extra={','.join(extra_cases) or 'none'}"
            )

    if blockers or any(row["proof_status"] == "blocked" for row in rendered_rows):
        status = "blocked"
    elif any(row["proof_status"] == "preview_only" for row in rendered_rows):
        status = "preview_only"
    else:
        status = "pass"
    return {
        "version": version,
        "status": status,
        "readiness_scope": readiness_scope,
        "runtime_commit_sha": str((source_binding or {}).get("code_commit") or ""),
        "required_journey_ids": list(REQUIRED_JOURNEY_IDS),
        "rows": rendered_rows,
    }, blockers


def _run_pytest_cases(
    root: Path,
    *,
    python_bin: str,
    test_file: str,
    cases: list[str],
    real_browser: bool,
) -> dict[str, Any]:
    cmd = [
        python_bin,
        "-m",
        "pytest",
        "-q",
        *(f"{test_file}::{case}" for case in cases),
    ]
    if real_browser:
        cmd.append("-rs")
    env = os.environ.copy()
    for key in PYTEST_ISOLATED_ENV_KEYS:
        env.pop(key, None)
    env["PYTHONPATH"] = _with_pythonpath(str(env.get("PYTHONPATH") or ""), root)
    started = time.monotonic()
    result = subprocess.run(
        cmd,
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    duration_seconds = round(time.monotonic() - started, 3)
    combined_output = "\n".join(
        part for part in (str(result.stdout or "").strip(), str(result.stderr or "").strip()) if part
    )
    limitations = _extract_limitations(combined_output, real_browser=real_browser)
    outcome_counts = _pytest_outcome_counts(combined_output)
    required_case_count = len(cases)
    all_required_cases_passed = (
        result.returncode == 0
        and outcome_counts["passed"] >= required_case_count
        and outcome_counts["failed"] == 0
        and outcome_counts["skipped"] == 0
        and outcome_counts["errors"] == 0
        and outcome_counts["xfailed"] == 0
        and outcome_counts["xpassed"] == 0
    )
    if all_required_cases_passed:
        status = "pass"
    elif real_browser and outcome_counts["skipped"] and not any(
        outcome_counts[key] for key in ("failed", "errors", "xfailed", "xpassed")
    ):
        status = "preview_only"
    else:
        status = "blocked"
    return _lane_completion({
        "status": status,
        "command": shlex.join(cmd),
        "cwd": root.as_posix(),
        "python_bin": python_bin,
        "test_file": test_file,
        "cases": cases,
        "exit_code": result.returncode,
        "duration_seconds": duration_seconds,
        "output_excerpt": _truncate_output(combined_output),
        "limitations": limitations,
        "outcome_counts": outcome_counts,
    }, required_cases=cases, real_browser=real_browser)


def build_receipt(
    root: Path,
    *,
    seed_path: Path = DEFAULT_SEED,
    runner: Callable[..., dict[str, Any]] = _run_pytest_cases,
    require_source_binding: bool = False,
) -> dict[str, Any]:
    seed = _load_json(root / seed_path)
    proof_contract = _object(seed.get("browser_workflow_proof"))
    proof_target = str(proof_contract.get("proof_target") or "executive-assistant").strip()
    proof_label = "PropertyQuarry" if proof_target == "propertyquarry" else "EA"
    python_bin = _resolve_python_bin(root)
    source_backed = _lane_completion(runner(
        root,
        python_bin=python_bin,
        test_file=SOURCE_BACKED_TEST_FILE,
        cases=SOURCE_BACKED_CASES,
        real_browser=False,
    ), required_cases=SOURCE_BACKED_CASES, real_browser=False)
    real_browser = _lane_completion(runner(
        root,
        python_bin=python_bin,
        test_file=REAL_BROWSER_TEST_FILE,
        cases=REAL_BROWSER_CASES,
        real_browser=True,
    ), required_cases=REAL_BROWSER_CASES, real_browser=True)

    blocking_reasons: list[str] = []
    current_limitations: list[str] = []
    if not seed:
        blocking_reasons.append("flagship gate seed is missing or invalid")
    if type(seed.get("browser_workflow_proof")) is not dict:
        blocking_reasons.append(
            "flagship gate seed browser_workflow_proof is missing or invalid"
        )
    if type(seed.get("release_claim")) is not dict:
        blocking_reasons.append("flagship gate seed release_claim is missing or invalid")
    if _strict_string_list(proof_contract.get("expected_browser_signals")) is None:
        blocking_reasons.append(
            "flagship gate seed expected_browser_signals is missing or invalid"
        )
    source_binding: dict[str, Any] | None = None
    if require_source_binding:
        try:
            source_binding = build_source_binding(
                root,
                seed_path=seed_path,
                evidence_sources=proof_contract.get("evidence_sources"),
            )
        except (OSError, TypeError, ReleaseBindingError) as exc:
            blocking_reasons.append(f"immutable source binding failed: {exc}")
    journey_evidence_matrix, journey_matrix_blockers = _build_journey_evidence_matrix(
        seed,
        source_backed=source_backed,
        real_browser=real_browser,
        source_binding=source_binding,
    )
    blocking_reasons.extend(journey_matrix_blockers)
    if source_backed["status"] != "pass":
        blocking_reasons.append("source-backed browser journey proof is not passing")
        current_limitations.extend(source_backed.get("limitations") or [])
    if real_browser["status"] == "blocked":
        blocking_reasons.append("real browser E2E proof is not passing")
        current_limitations.extend(real_browser.get("limitations") or [])
    elif real_browser["status"] == "preview_only":
        current_limitations.extend(real_browser.get("limitations") or [])

    if not blocking_reasons and real_browser["status"] == "pass":
        status = "pass"
        operator_summary = (
            f"{proof_label} browser workflow proof is published and green across the required journey matrix, "
            "source-backed contracts, and real-browser E2E."
        )
    elif not blocking_reasons:
        status = "preview_only"
        operator_summary = f"{proof_label} browser workflow proof is published, but it remains preview_only until the real-browser E2E slice runs cleanly."
    else:
        status = "blocked"
        operator_summary = f"{proof_label} browser workflow proof is published, but it is blocked by failing or unavailable proof lanes."

    receipt = {
        "contract_name": "ea.browser_workflow_proof",
        "product": str(seed.get("product") or "propertyquarry"),
        "surface": "browser_workflow_proof",
        "proof_target": proof_target,
        "version": 1,
        "kind": "proof_receipt",
        "generated_at": _utc_now(),
        "generated_by": "scripts/materialize_ea_browser_workflow_proof.py",
        "status": status,
        "operator_summary": operator_summary,
        "seed_source": seed_path.as_posix(),
        "release_claim_summary": str(_object(seed.get("release_claim")).get("summary") or "").strip(),
        "expected_browser_signals": _string_list(
            proof_contract.get("expected_browser_signals")
        ),
        "source_binding": source_binding,
        "source_backed_journey_proof": source_backed,
        "real_browser_e2e_proof": real_browser,
        "journey_evidence_matrix": journey_evidence_matrix,
        "blocking_reasons": blocking_reasons,
        "current_limitations": sorted(set(current_limitations)),
    }
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description="Materialize the EA browser workflow proof receipt.")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="EA repository root.")
    parser.add_argument("--seed", type=Path, default=DEFAULT_SEED, help="EA flagship release seed.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Path to write the generated receipt.")
    parser.add_argument("--stdout", action="store_true", help="Print the receipt JSON to stdout.")
    args = parser.parse_args()

    root = args.root.resolve()
    receipt = build_receipt(root, seed_path=args.seed, require_source_binding=True)
    output_path = root / args.output
    _write_json_stable(args.output, receipt, root=root)
    if args.stdout:
        print(json.dumps(receipt, indent=2, ensure_ascii=False))
    else:
        print(json.dumps({"status": "ok", "output": output_path.as_posix(), "receipt_status": receipt["status"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
