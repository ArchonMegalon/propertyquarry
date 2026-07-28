#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import secrets
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

if __package__:
    from .propertyquarry_release_receipt_binding import (
        ReleaseBindingError,
        StableFileSnapshot,
        read_stable_regular_file,
        sha256_bytes,
    )
else:
    from propertyquarry_release_receipt_binding import (
        ReleaseBindingError,
        StableFileSnapshot,
        read_stable_regular_file,
        sha256_bytes,
    )


DEFAULT_OUTPUT = Path(".codex-design/product/WEEKLY_PRODUCT_PULSE.generated.json")
DEFAULT_SCORECARD = Path(".codex-design/product/PRODUCT_HEALTH_SCORECARD.yaml")
DEFAULT_JOURNEY_GATES = Path("/docker/fleet/.codex-studio/published/JOURNEY_GATES.generated.json")
DEFAULT_FLAGSHIP_RECEIPT = Path(".codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json")
DEFAULT_GOVERNOR_LOOP = Path(".codex-design/product/PRODUCT_GOVERNOR_AND_AUTOPILOT_LOOP.md")
DEFAULT_CONTROL_LOOP = Path(".codex-design/product/PRODUCT_CONTROL_AND_GOVERNOR_LOOP.md")
DEFAULT_RELEASE_PIPELINE = Path(".codex-design/product/RELEASE_PIPELINE.md")
DEFAULT_RELEASE_CHECKLIST = Path("RELEASE_CHECKLIST.md")
DEFAULT_ROOT = Path(__file__).resolve().parents[1]
_RELEASE_TRUTH_HEAD_KEYS = {
    "release_truth_provenance.git_head",
    "supporting_signals.flagship_release_receipt_git_head",
}
_PROVENANCE_REFRESH_ALLOWED_PREFIXES = (
    ".codex-design/product/",
    ".codex-studio/published/",
    ".codex-design/repo/",
)
_PROVENANCE_REFRESH_ALLOWED_EXACT = {
    "README.md",
    "RUNBOOK.md",
    "RELEASE_CHECKLIST.md",
    "PRODUCT_RELEASE_CHECKLIST.md",
    "docs/PROPERTYQUARRY_RELEASE_MANIFEST.md",
    "Makefile",
    "CHANGELOG.md",
    "LTDs.md",
    ".github/workflows/smoke-runtime.yml",
    "ea/app/api/routes/plans.py",
    "ea/app/services/execution_approval_pause_service.py",
    "scripts/materialize_ea_browser_workflow_proof.py",
    "scripts/materialize_weekly_product_pulse.py",
    "scripts/operator_summary.sh",
    "scripts/smoke_api.sh",
    "scripts/smoke_postgres.sh",
    "scripts/test_postgres_contracts.sh",
    "scripts/verify_generated_release_artifacts_clean.py",
    "scripts/verify_flagship_release_readiness.py",
    "scripts/verify_release_assets.sh",
    "tests/e2e/visual_baselines/admin-community-page.png",
    "tests/test_chummer5a_parity_lab_pack.py",
    "tests/test_ea_browser_workflow_proof_materializer.py",
    "tests/e2e/test_product_workflows.py",
    "tests/test_execution_runtime_services.py",
    "tests/test_flagship_release_readiness_gate.py",
    "tests/test_migration_contracts.py",
    "tests/test_operator_contracts.py",
    "tests/test_providers_api_contracts.py",
    "tests/test_skills.py",
    "tests/smoke_runtime_api_suite_3.py",
    "tests/test_weekly_product_pulse_materializer.py",
}
RENAME_NOREPLACE = 1
RENAME_EXCHANGE = 2


class _PreserveStagedOutputError(RuntimeError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"duplicate JSON key: {key}")
        payload[key] = value
    return payload


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except Exception:
        return None
    return dict(payload) if type(payload) is dict else None


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(payload or {}) if isinstance(payload, dict) else {}


def _object(value: object) -> dict[str, Any]:
    return value if type(value) is dict else {}


def _items(value: object) -> list[Any]:
    return value if type(value) is list else []


def _normalize_release_value(value: Any) -> Any:
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if key in {
                "generated_at",
                "as_of",
                "created_at",
                "mtime_utc",
                "duration_seconds",
                "git_branch",
                "git_head",
                "source_path",
                "resolved_path",
                "git_repo_root",
            }:
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


def _provenance_heads(value: Any, *, prefix: str = "") -> dict[str, str]:
    if isinstance(value, dict):
        heads: dict[str, str] = {}
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if key == "git_head" or str(key).endswith("_git_head"):
                heads[path] = str(item or "").strip()
                continue
            heads.update(_provenance_heads(item, prefix=path))
        return heads
    if isinstance(value, list):
        heads: dict[str, str] = {}
        for index, item in enumerate(value):
            heads.update(_provenance_heads(item, prefix=f"{prefix}[{index}]"))
        return heads
    return {}


def _changed_paths_between_heads(old_head: str, new_head: str, *, repo_root: Path = DEFAULT_ROOT) -> list[str] | None:
    try:
        output = subprocess.run(
            ["git", "-C", str(repo_root), "diff", "--name-only", f"{old_head}..{new_head}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except Exception:
        return None
    return [line.strip() for line in output.splitlines() if line.strip()]


def _allowed_provenance_refresh_path(path: str) -> bool:
    return path in _PROVENANCE_REFRESH_ALLOWED_EXACT or any(
        path.startswith(prefix) for prefix in _PROVENANCE_REFRESH_ALLOWED_PREFIXES
    )


def _provenance_refresh_required(existing_heads: dict[str, str], payload_heads: dict[str, str]) -> bool:
    if existing_heads == payload_heads:
        return False

    differing_keys = {
        key
        for key in set(existing_heads) | set(payload_heads)
        if existing_heads.get(key) != payload_heads.get(key)
    }
    if not differing_keys <= _RELEASE_TRUTH_HEAD_KEYS:
        return True

    old_head = existing_heads.get("release_truth_provenance.git_head") or existing_heads.get(
        "supporting_signals.flagship_release_receipt_git_head"
    )
    new_head = payload_heads.get("release_truth_provenance.git_head") or payload_heads.get(
        "supporting_signals.flagship_release_receipt_git_head"
    )
    if not old_head or not new_head:
        return True

    changed_paths = _changed_paths_between_heads(old_head, new_head)
    if changed_paths is None:
        return True
    return any(not _allowed_provenance_refresh_path(path) for path in changed_paths)


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
                and not _provenance_refresh_required(
                    _provenance_heads(existing),
                    _provenance_heads(payload),
                )
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


def _compact(value: object, *, fallback: str = "", limit: int = 220) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        text = fallback
    if len(text) > limit:
        return text[: max(limit - 1, 0)].rstrip() + "…"
    return text


def _receipt_product_label(receipt: dict[str, Any]) -> str:
    explicit_label = _compact(receipt.get("product_label") or receipt.get("product_name") or "")
    if explicit_label:
        return explicit_label

    product = _compact(receipt.get("product") or "", fallback="Current product")
    normalized = product.casefold().replace("_", "-").replace(" ", "-")
    return {
        "executive-assistant": "Executive Assistant",
        "property-quarry": "PropertyQuarry",
        "propertyquarry": "PropertyQuarry",
    }.get(normalized, product)


def _optional_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_int(*values: object, fallback: int = 0) -> int:
    for value in values:
        parsed = _optional_int(value)
        if parsed is not None:
            return parsed
    return fallback


def _existing_cited_signal_int(existing: dict[str, Any], signal_name: str) -> int | None:
    prefix = f"{signal_name}="
    signal_sources = [
        _items(existing.get("governor_decisions")),
        _items(_object(existing.get("snapshot")).get("governor_decisions")),
    ]
    for decisions in signal_sources:
        for decision in decisions:
            if not isinstance(decision, dict):
                continue
            for signal in _items(decision.get("cited_signals")):
                signal_text = str(signal or "")
                if not signal_text.startswith(prefix):
                    continue
                try:
                    return int(signal_text[len(prefix) :])
                except ValueError:
                    continue
    return None


def _resolve_for_read(root: Path, path: Path) -> Path:
    return path if path.is_absolute() else root / path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_metadata_for_path(path: Path) -> dict[str, str]:
    candidate = path if path.is_dir() else path.parent
    try:
        repo_root = subprocess.run(
            ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        git_head = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        git_branch = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except Exception:
        return {}

    metadata: dict[str, str] = {
        "git_repo_root": repo_root,
        "git_head": git_head,
        "git_branch": git_branch,
    }
    try:
        metadata["repo_relative_path"] = str(path.resolve().relative_to(Path(repo_root).resolve()))
    except Exception:
        pass
    return metadata


def _source_provenance(
    path: Path,
    *,
    snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    if snapshot is not None:
        snapshot.assert_unchanged()
        payload = {
            "source_path": path.as_posix(),
            "resolved_path": snapshot.path.as_posix(),
            "present": True,
            "size_bytes": len(snapshot.payload),
            "mtime_utc": datetime.fromtimestamp(
                snapshot.identity[4] / 1_000_000_000,
                timezone.utc,
            ).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "sha256": sha256_bytes(snapshot.payload),
        }
        payload.update(_git_metadata_for_path(path))
        snapshot.assert_unchanged()
        return payload

    resolved = path.resolve() if path.exists() else path
    payload: dict[str, Any] = {
        "source_path": path.as_posix(),
        "resolved_path": resolved.as_posix(),
        "present": path.exists(),
    }
    if not path.exists():
        return payload
    try:
        stat = path.stat()
        payload["size_bytes"] = stat.st_size
        payload["mtime_utc"] = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        payload["sha256"] = _sha256_file(path)
    except Exception:
        pass
    payload.update(_git_metadata_for_path(path))
    return payload


def _journey_gate_source(root: Path, journey_path: Path) -> dict[str, Any]:
    resolved = _resolve_for_read(root, journey_path)
    if not resolved.exists():
        existing = _load_json(root / DEFAULT_OUTPUT) or {}
        existing_health = _object(existing.get("journey_gate_health"))
        existing_provenance = _object(existing.get("journey_gate_provenance"))
        if existing_health or existing_provenance:
            existing_signals = _object(existing.get("supporting_signals"))
            blocked = _first_int(existing_health.get("blocked_count"))
            warning = _first_int(existing_health.get("warning_count"))
            state = str(existing_health.get("state") or "missing").strip() or "missing"
            recommended_action = _compact(
                existing_health.get("recommended_action") or existing_health.get("reason") or "",
                fallback="Journey-gate posture is preserved from the last committed external snapshot.",
            )
            ready_hint = _first_int(
                existing_health.get("ready_count"),
                _existing_cited_signal_int(existing, "supporting_external_fleet_ready_count"),
                _existing_cited_signal_int(existing, "journey_gate_ready_count"),
                fallback=0,
            )
            total = _first_int(
                existing_health.get("total_count"),
                _existing_cited_signal_int(existing, "supporting_external_fleet_total_count"),
                _existing_cited_signal_int(existing, "journey_gate_total_count"),
                fallback=ready_hint + blocked + warning,
            )
            ready = _first_int(
                existing_health.get("ready_count"),
                _existing_cited_signal_int(existing, "supporting_external_fleet_ready_count"),
                _existing_cited_signal_int(existing, "journey_gate_ready_count"),
                fallback=0,
            )
            ready_share = _first_int(
                existing_signals.get("external_fleet_journey_ready_share_percent"),
                _existing_cited_signal_int(existing, "supporting_external_fleet_ready_share"),
                existing_signals.get("overall_progress_percent"),
                _existing_cited_signal_int(existing, "ready_share"),
            )
            has_snapshot_provenance = (
                existing_provenance.get("present") is True
                and bool(str(existing_provenance.get("source_path") or "").strip())
                and bool(str(existing_provenance.get("sha256") or "").strip())
                and bool(str(existing_provenance.get("git_head") or "").strip())
            )
            if not has_snapshot_provenance:
                ready = 0
                total = max(blocked + warning, 0)
                ready_share = 0
            if state == "ready" and not (
                has_snapshot_provenance
                and total > 0
                and ready == total
                and blocked == 0
                and warning == 0
            ):
                state = "unavailable"
            preserved_path = Path(str(existing.get("journey_gate_source") or journey_path.as_posix()))
            provenance = existing_provenance or _source_provenance(resolved)
            if not existing_provenance:
                provenance["present"] = False
            return {
                "journey": {},
                "summary": {},
                "journeys": [],
                "path": preserved_path,
                "state": state,
                "recommended_action": recommended_action,
                "blocked": blocked,
                "ready": ready,
                "warning": warning,
                "total": total,
                "ready_share": int(round((ready / total) * 100)) if total else ready_share,
                "provenance": provenance,
            }
    journey = _load_json(resolved) or {}
    summary = _object(journey.get("summary"))
    journeys = [
        dict(row) for row in _items(journey.get("journeys")) if type(row) is dict
    ]
    blocked = max(_first_int(summary.get("blocked_count")), 0)
    ready = max(_first_int(summary.get("ready_count")), 0)
    parsed_total = _optional_int(summary.get("total_journey_count"))
    total = max(
        parsed_total
        if parsed_total is not None
        else (len(journeys) or (blocked + ready)),
        0,
    )
    warning = max(_first_int(summary.get("warning_count")), 0)
    journey_state = str(summary.get("overall_state") or "missing").strip() or "missing"
    recommended_action = _compact(summary.get("recommended_action") or "", fallback="Journey-gate posture is not available.")
    return {
        "journey": journey,
        "summary": summary,
        "journeys": journeys,
        "path": journey_path,
        "state": journey_state,
        "recommended_action": recommended_action,
        "blocked": blocked,
        "ready": ready,
        "warning": warning,
        "total": total,
        "ready_share": int(round((ready / total) * 100)) if total else 0,
        "provenance": _source_provenance(resolved),
    }


def _flagship_receipt_source(root: Path, receipt_path: Path) -> dict[str, Any]:
    resolved = _resolve_for_read(root, receipt_path)
    receipt_snapshot = None
    try:
        receipt_snapshot = read_stable_regular_file(resolved)
        receipt = _strict_json_object_bytes(receipt_snapshot.payload) or {}
    except (OSError, ReleaseBindingError):
        receipt = {}
    status = str(receipt.get("status") or "missing").strip() or "missing"
    malformed_nested = any(
        type(receipt.get(key)) is not dict
        for key in ("truth_plane", "browser_workflow_proof", "live_readiness")
    )
    truth_plane = _object(receipt.get("truth_plane"))
    browser = _object(receipt.get("browser_workflow_proof"))
    live_readiness = _object(receipt.get("live_readiness"))
    if malformed_nested:
        status = "blocked"
    result = {
        "receipt": receipt,
        "product_label": _receipt_product_label(receipt),
        "path": receipt_path,
        "status": status,
        "truth_plane": truth_plane,
        "readiness_scope": str(receipt.get("readiness_scope") or "source_and_browser_proof").strip()
        or "source_and_browser_proof",
        "live_readiness": live_readiness,
        "live_readiness_status": str(live_readiness.get("status") or "not_evaluated").strip()
        or "not_evaluated",
        "browser_present": bool(browser.get("published_receipt_present")),
        "browser_receipt": str(browser.get("published_receipt") or "").strip(),
        "limitations": [
            str(item)
            for item in _items(receipt.get("current_limitations"))
            if str(item).strip()
        ]
        + (
            ["flagship receipt contains invalid nested release evidence"]
            if malformed_nested
            else []
        ),
        "provenance": _source_provenance(
            resolved,
            snapshot=receipt_snapshot,
        ),
    }
    if receipt_snapshot is not None:
        receipt_snapshot.assert_unchanged()
    return result


def build_pulse(
    root: Path,
    *,
    scorecard_path: Path = DEFAULT_SCORECARD,
    journey_gates_path: Path = DEFAULT_JOURNEY_GATES,
    flagship_receipt_path: Path = DEFAULT_FLAGSHIP_RECEIPT,
    governor_loop_path: Path = DEFAULT_GOVERNOR_LOOP,
    control_loop_path: Path = DEFAULT_CONTROL_LOOP,
    release_pipeline_path: Path = DEFAULT_RELEASE_PIPELINE,
    release_checklist_path: Path = DEFAULT_RELEASE_CHECKLIST,
) -> dict[str, Any]:
    scorecard = _load_yaml(root / scorecard_path)
    cadence = _object(scorecard.get("cadence"))
    journey_info = _journey_gate_source(root, journey_gates_path)
    journey_source_path = Path(str(journey_info.get("path") or journey_gates_path.as_posix()))
    receipt_info = _flagship_receipt_source(root, flagship_receipt_path)
    now = _utcnow()
    generated_at = _format_utc(now)
    review_due = _format_utc(now + timedelta(days=7))

    scorecard_metrics = _items(scorecard.get("scorecards"))
    scorecard_metric_count = sum(
        len(_items(row.get("metrics")))
        for row in scorecard_metrics
        if type(row) is dict
    )
    product_label = str(receipt_info["product_label"])
    release_truth_state = receipt_info["status"]
    readiness_scope = str(receipt_info["readiness_scope"])
    live_readiness = dict(receipt_info["live_readiness"])
    live_readiness_status = str(receipt_info["live_readiness_status"])
    live_readiness_pass = live_readiness_status == "pass"
    journey_state = journey_info["state"]
    blocked_count = int(journey_info["blocked"])
    ready_count = int(journey_info["ready"])
    total_count = int(journey_info["total"])
    readiness_share = int(journey_info["ready_share"])
    candidate_readiness_state = (
        "clear" if release_truth_state == "pass" else "watch" if release_truth_state == "preview_only" else "blocked"
    )
    reported_live_readiness_state = "pass_unverified" if live_readiness_pass else "not_passed"
    # This pulse does not consume deployment-controller or public /version reconciliation evidence.
    # It therefore cannot authorize production even when a nested protected-live status reports pass.
    production_launch_state = "blocked"

    if release_truth_state == "pass" and live_readiness_pass:
        summary = (
            f"{product_label} source/browser candidate proof is green and its nested live-readiness signal reports "
            f"{live_readiness_status}, but this pulse does not validate that authority or authorize production."
        )
    elif release_truth_state == "pass":
        summary = (
            f"{product_label} source/browser candidate proof is green, but protected live readiness is "
            f"{live_readiness_status}; this pulse does not support a production launch claim."
        )
    elif release_truth_state == "preview_only":
        summary = (
            f"{product_label} source/browser candidate proof remains {release_truth_state}; protected live readiness is "
            f"{live_readiness_status}, so production launch remains blocked."
        )
    else:
        summary = (
            f"{product_label} source/browser candidate proof is {release_truth_state}; protected live readiness is "
            f"{live_readiness_status}, so production launch remains blocked."
        )

    launch_readiness = (
        "Hold production launch pending complete source/browser candidate proof and protected live evidence."
        if release_truth_state != "pass"
        else "Source/browser candidate proof is green; hold production launch until protected live readiness passes."
        if not live_readiness_pass
        else "Source/browser candidate and protected live readiness report pass; reconcile the current deployment before widening production claims."
    )
    canary_status = (
        "Source/browser candidate proof is still missing or incomplete."
        if release_truth_state != "pass"
        else "Browser execution proof is published for the source/browser candidate; protected live evidence remains separate."
    )
    next_decision = (
        "Publish complete browser execution proof, then re-materialize the weekly pulse and release receipt."
        if release_truth_state != "pass"
        else "Complete the protected launch profile and live receipts, then re-materialize this pulse."
        if not live_readiness_pass
        else "Reconcile the deployed runtime and re-materialize after the next meaningful release-truth change."
    )
    provider_route_stewardship = {
        "default_status": f"{product_label} routes are governed by local truth surfaces.",
        "canary_status": canary_status,
        "review_due": review_due,
        "next_decision": next_decision,
    }
    external_tuple_coverage = (
        "ready" if journey_state == "ready" else "blocked" if journey_state == "blocked" else "unavailable"
    )

    governor_decisions = [
        {
            "decision_id": "2026-04-10-focus-ea-flagship-receipt-closeout",
            "action": "focus_shift",
            "reason": (
                f"Keep the weekly pulse anchored to the {product_label} source/browser receipt without treating external "
                f"fleet journey context as launch authority. The receipt is {release_truth_state} and protected live readiness is {live_readiness_status}."
            ),
            "cited_signals": [
                f"flagship_receipt_status={release_truth_state}",
                f"supporting_external_fleet_journey_state={journey_state}",
                f"supporting_external_fleet_blocked_count={blocked_count}",
                f"supporting_external_fleet_ready_count={ready_count}",
                f"supporting_external_fleet_total_count={total_count}",
                f"supporting_external_fleet_ready_share={readiness_share}",
                "journey_gate_authority=non_authoritative_for_propertyquarry_launch",
                f"readiness_scope={readiness_scope}",
                f"live_readiness_status={live_readiness_status}",
            ],
        },
        {
            "decision_id": "2026-04-10-freeze-launch-expansion",
            "action": "freeze_launch",
            "reason": (
                "Freeze production launch until source/browser candidate proof and protected live readiness both pass."
                if release_truth_state != "pass"
                else "Freeze production launch until protected live readiness passes."
                if not live_readiness_pass
                else "Keep production claims constrained until the current deployed runtime is reconciled to the approved candidate."
            ),
            "cited_signals": [
                f"flagship_receipt_status={release_truth_state}",
                f"browser_execution_receipt_present={receipt_info['browser_present']}",
                f"supporting_external_fleet_blocked_count={blocked_count}",
                f"live_readiness_status={live_readiness_status}",
                f"supporting_external_fleet_tuple_coverage={external_tuple_coverage}",
            ],
        },
    ]

    if journey_state == "blocked":
        fleet_cluster_summary = (
            "The last published external Fleet journey snapshot is blocked, but it is carried as supporting-only "
            "context and is not PropertyQuarry launch authority."
        )
    elif journey_state == "ready":
        fleet_cluster_summary = (
            "The last published external Fleet journey snapshot reports ready; it is carried as supporting-only "
            "context and cannot prove PropertyQuarry launch readiness."
        )
    else:
        fleet_cluster_summary = (
            "The external Fleet journey snapshot is unavailable or incomplete; it remains supporting-only context "
            "and cannot prove PropertyQuarry launch readiness."
        )

    blocked_reason = journey_info["recommended_action"] or "Resolve the blocking journey gaps before widening publish claims."
    pulse: dict[str, Any] = {
        "contract_name": "ea.weekly_product_pulse",
        "contract_version": 2,
        "generated_at": generated_at,
        "as_of": generated_at[:10],
        "scorecard_source": scorecard_path.as_posix(),
        "release_truth_source": flagship_receipt_path.as_posix(),
        "release_truth_provenance": receipt_info["provenance"],
        "readiness_scope": readiness_scope,
        "live_readiness": live_readiness,
        "journey_gate_source": journey_source_path.as_posix(),
        "journey_gate_provenance": journey_info["provenance"],
        "journey_gate_scope": "supporting_external_fleet_context",
        "journey_gate_authority": "non_authoritative_for_propertyquarry_launch",
        "journey_gate_snapshot_policy": "carry_forward_committed_snapshot_only",
        "summary": summary,
        "active_wave": f"{product_label} flagship receipt closeout",
        "active_wave_status": "active",
        "release_health": {
            "state": "blocked",
            "scope": "production_launch",
            "candidate_state": candidate_readiness_state,
            "reported_live_readiness_state": reported_live_readiness_state,
            "production_launch_state": production_launch_state,
            "reason": (
                f"The {product_label} source/browser candidate receipt is published, but protected live and deployment authority are not validated by this pulse."
                if release_truth_state == "pass"
                else f"The {product_label} flagship receipt is materialized, but it is still preview_only until browser execution proof is published."
                if release_truth_state == "preview_only"
                else f"The {product_label} flagship receipt is blocked by the current browser workflow proof or release evidence."
            ),
            "flagship_receipt_status": release_truth_state,
        },
        "flagship_readiness": {
            "state": "blocked",
            "scope": "production_launch",
            "candidate_state": candidate_readiness_state,
            "reported_live_readiness_state": reported_live_readiness_state,
            "production_launch_state": production_launch_state,
            "reason": (
                "Source/browser candidate receipt and browser proof are aligned; protected live and deployment authority are not validated by this pulse."
                if release_truth_state == "pass"
                else "Browser execution proof is missing or incomplete, so the flagship receipt cannot yet claim pass status."
                if release_truth_state == "preview_only"
                else "Browser workflow proof is currently blocked, so the flagship receipt cannot support wider release claims."
            ),
        },
        "rule_environment_trust": {
            "state": "monitor",
            "reason": (
                "External Fleet journey context is supporting-only and does not determine PropertyQuarry rule-environment trust."
            ),
        },
        "edition_authorship_and_import_confidence": {
            "state": "monitor",
            "reason": f"The weekly pulse now uses local {product_label} release truth rather than a Chummer mirror.",
        },
        "journey_gate_health": {
            "state": journey_state,
            "scope": "supporting_external_fleet_context",
            "authority": "non_authoritative_for_propertyquarry_launch",
            "reason": _compact(blocked_reason, fallback="Journey-gate posture is not available."),
            "blocked_count": blocked_count,
            "warning_count": int(journey_info["warning"]),
            "recommended_action": _compact(blocked_reason, fallback="Journey-gate posture is not available."),
        },
        "top_support_or_feedback_clusters": [
            {
                "cluster_id": "ea_flagship_receipt_closeout",
                "summary": (
                    f"The weekly pulse now anchors to the {product_label} flagship receipt generated from local truth surfaces, "
                    + (
                        "and the browser execution proof is published."
                        if release_truth_state == "pass"
                        else "but browser execution proof is still pending."
                    )
                ),
                "source_paths": [
                    flagship_receipt_path.as_posix(),
                    "README.md",
                    "RUNBOOK.md",
                ],
            },
            {
                "cluster_id": "fleet_journey_coverage",
                "summary": fleet_cluster_summary,
                "source_paths": [
                    journey_source_path.as_posix(),
                    "scripts/verify_release_assets.sh",
                ],
            },
            {
                "cluster_id": "governor_truth_alignment",
                "summary": (
                    "Product governor and support surfaces should keep quoting the same release truth instead of drifting "
                    "back to a mirrored Chummer pulse."
                ),
                "source_paths": [
                    "PRODUCT_GOVERNOR_AND_AUTOPILOT_LOOP.md",
                    "PRODUCT_CONTROL_AND_GOVERNOR_LOOP.md",
                    "PRODUCT_HEALTH_SCORECARD.yaml",
                ],
            },
        ],
        "oldest_blocker_days": None,
        "oldest_blocker_days_status": "not_evaluated",
        "oldest_blocker_scope": "protected_live_blocker_age_not_evaluated",
        "design_drift_count": None,
        "public_promise_drift_count": None,
        "drift_count_status": "not_evaluated",
        "governor_decisions": governor_decisions,
        "next_checkpoint_question": (
            "Which protected production receipt or deployment input is the next blocker to clear?"
            if release_truth_state == "pass"
            else f"What source/browser evidence is still needed to promote the {product_label} candidate receipt to pass?"
        ),
        "supporting_signals": {
            "current_recommended_wave": f"{product_label} flagship receipt closeout",
            "overall_progress_percent": None,
            "overall_progress_status": "production_launch_progress_not_evaluated",
            "external_fleet_journey_ready_share_percent": readiness_share,
            "phase_label": "Protected live evidence closeout" if release_truth_state == "pass" else "Source/browser candidate closeout",
            "history_snapshot_count": 1,
            "longest_pole": "protected live production evidence" if release_truth_state == "pass" and not live_readiness_pass else "deployment reconciliation" if release_truth_state == "pass" else "browser execution proof",
            "launch_readiness": launch_readiness,
            "readiness_scope": readiness_scope,
            "live_readiness_status": live_readiness_status,
            "provider_route_stewardship": provider_route_stewardship,
            "journey_gate_source": journey_source_path.as_posix(),
            "journey_gate_git_head": str(journey_info["provenance"].get("git_head") or "").strip(),
            "flagship_release_receipt_source": flagship_receipt_path.as_posix(),
            "flagship_release_receipt_git_head": str(receipt_info["provenance"].get("git_head") or "").strip(),
            "scorecard_source": scorecard_path.as_posix(),
            "release_pipeline_source": release_pipeline_path.as_posix(),
            "governor_loop_source": governor_loop_path.as_posix(),
            "control_loop_source": control_loop_path.as_posix(),
            "release_checklist_source": release_checklist_path.as_posix(),
            "scorecard_metric_count": scorecard_metric_count,
        },
        "snapshot": {
            "release_health": {
                "state": "blocked",
                "scope": "production_launch",
                "candidate_state": candidate_readiness_state,
                "reported_live_readiness_state": reported_live_readiness_state,
                "production_launch_state": production_launch_state,
                "reason": (
                    f"The {product_label} flagship receipt is materialized, but it is still preview_only until browser execution proof is published."
                    if release_truth_state != "pass"
                    else f"The {product_label} source/browser candidate receipt is published, but protected live and deployment authority are not validated by this pulse."
                ),
                "flagship_receipt_status": release_truth_state,
            },
            "flagship_readiness": {
                "state": "blocked",
                "scope": "production_launch",
                "candidate_state": candidate_readiness_state,
                "reported_live_readiness_state": reported_live_readiness_state,
                "production_launch_state": production_launch_state,
                "reason": (
                    "Browser execution proof is missing or incomplete, so the flagship receipt cannot yet claim pass status."
                    if release_truth_state != "pass"
                    else "Source/browser candidate receipt and browser proof are aligned; protected live and deployment authority are not validated by this pulse."
                ),
            },
            "rule_environment_trust": {
                "state": "monitor",
                "reason": (
                    "External Fleet journey context is supporting-only and does not determine PropertyQuarry rule-environment trust."
                ),
            },
            "edition_authorship_and_import_confidence": {
                "state": "monitor",
                "reason": f"The weekly pulse now uses local {product_label} release truth rather than a Chummer mirror.",
            },
            "journey_gate_health": {
                "state": journey_state,
                "scope": "supporting_external_fleet_context",
                "authority": "non_authoritative_for_propertyquarry_launch",
                "reason": _compact(blocked_reason, fallback="Journey-gate posture is not available."),
                "blocked_count": blocked_count,
                "warning_count": int(journey_info["warning"]),
                "recommended_action": _compact(blocked_reason, fallback="Journey-gate posture is not available."),
            },
            "top_support_or_feedback_clusters": [
                {
                    "cluster_id": "ea_flagship_receipt_closeout",
                    "summary": (
                        f"The weekly pulse now anchors to the {product_label} flagship receipt generated from local truth surfaces, "
                        + (
                            "and the browser execution proof is published."
                            if release_truth_state == "pass"
                            else "but browser execution proof is still pending."
                        )
                    ),
                    "source_paths": [
                        flagship_receipt_path.as_posix(),
                        "README.md",
                        "RUNBOOK.md",
                    ],
                },
                {
                    "cluster_id": "fleet_journey_coverage",
                    "summary": fleet_cluster_summary,
                    "source_paths": [
                        journey_source_path.as_posix(),
                        "scripts/verify_release_assets.sh",
                    ],
                },
                {
                    "cluster_id": "governor_truth_alignment",
                    "summary": (
                        "Product governor and support surfaces should keep quoting the same release truth instead of drifting "
                        "back to a mirrored pulse."
                    ),
                    "source_paths": [
                        "PRODUCT_GOVERNOR_AND_AUTOPILOT_LOOP.md",
                        "PRODUCT_CONTROL_AND_GOVERNOR_LOOP.md",
                        "PRODUCT_HEALTH_SCORECARD.yaml",
                    ],
                },
            ],
            "oldest_blocker_days": None,
            "oldest_blocker_days_status": "not_evaluated",
            "oldest_blocker_scope": "protected_live_blocker_age_not_evaluated",
            "design_drift_count": None,
            "public_promise_drift_count": None,
            "drift_count_status": "not_evaluated",
            "governor_decisions": governor_decisions,
            "next_checkpoint_question": (
                "Which protected production receipt or deployment input is the next blocker to clear?"
                if release_truth_state == "pass"
                else f"What source/browser evidence is still needed to promote the {product_label} candidate receipt to pass?"
            ),
        },
        "release_wave": {
            "current_recommended_wave": f"{product_label} flagship receipt closeout",
            "active_wave_registry": scorecard_path.as_posix(),
        },
        "review_cadence": {
            "review": str(cadence.get("review") or "weekly").strip() or "weekly",
            "snapshot_owner": str(cadence.get("snapshot_owner") or "product_governor").strip() or "product_governor",
            "publication": str(cadence.get("publication") or "internal_canon_first").strip() or "internal_canon_first",
        },
    }
    return pulse


def main() -> int:
    parser = argparse.ArgumentParser(description="Materialize the EA weekly product pulse.")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="EA repository root.")
    parser.add_argument("--scorecard", type=Path, default=DEFAULT_SCORECARD, help="Path to the EA product health scorecard.")
    parser.add_argument(
        "--journey-gates",
        type=Path,
        default=DEFAULT_JOURNEY_GATES,
        help="Path to the fleet published journey-gates receipt.",
    )
    parser.add_argument(
        "--flagship-receipt",
        type=Path,
        default=DEFAULT_FLAGSHIP_RECEIPT,
        help="Path to the EA flagship release receipt.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Path to write the weekly pulse receipt.",
    )
    parser.add_argument("--stdout", action="store_true", help="Print the generated pulse to stdout.")
    args = parser.parse_args()

    root = args.root.resolve()
    pulse = build_pulse(
        root,
        scorecard_path=args.scorecard,
        journey_gates_path=args.journey_gates,
        flagship_receipt_path=args.flagship_receipt,
    )

    output_path = root / args.output
    _write_json_stable(args.output, pulse, root=root)
    if args.stdout:
        print(json.dumps(pulse, indent=2, ensure_ascii=False))
    else:
        print(json.dumps({"status": "ok", "output": output_path.as_posix(), "contract_name": pulse["contract_name"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
