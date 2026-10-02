#!/usr/bin/env python3
"""Prove whether scheduler activation is ready for a separate consent request."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml


ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "ea"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts import propertyquarry_ooda_approved_signals as approved
from scripts import propertyquarry_ooda_scheduler_continuity as continuity
from scripts.propertyquarry_secure_file_io import atomic_write_bytes


OBSERVATION_SCHEMA = "propertyquarry.ooda_scheduler_activation_preflight.v1"
CANDIDATE_DROP_SCHEMA = (
    "propertyquarry.ooda_scheduler_activation_candidate_drop_posture.v1"
)
SCHEMA = "propertyquarry.ooda_scheduler_activation_readiness.v1"
VERIFY_SCHEMA = (
    "propertyquarry.ooda_scheduler_activation_readiness_verification.v1"
)
DEFAULT_RECEIPT_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "scheduler-activation-readiness.json"
)
DEFAULT_VERIFICATION_PATH = Path(
    "_completion/propertyquarry_ooda_notification_cycle/"
    "scheduler-activation-readiness-verification.json"
)
DEFAULT_MAX_AGE_SECONDS = 300.0
DEFAULT_TIMEOUT_SECONDS = 90
MAX_CAPTURE_BYTES = 64 * 1024
MAX_WORKTREE_STATUS_BYTES = 4 * 1024 * 1024
RELEASE_TREE_TIMEOUT_SECONDS = 10
DEPLOY_SCRIPT = ROOT / "scripts/deploy_propertyquarry.sh"
COMPOSE_PATH = ROOT / "docker-compose.property.yml"
WEB_DOCKERFILE = ROOT / "ea/Dockerfile.property-web"
DEFAULT_CANDIDATE_DROP = ROOT / "state/incoming_propertyquarry_trust"
CANDIDATE_DROP_ENV = "PROPERTYQUARRY_OODA_TRUST_CANDIDATE_DROP_DIR"
CANDIDATE_DROP_GID_ENV = "PROPERTYQUARRY_OODA_TRUST_CANDIDATE_DROP_GID"
CANDIDATE_DROP_COMPOSE_SOURCE = (
    "${PROPERTYQUARRY_OODA_TRUST_CANDIDATE_DROP_DIR:-"
    "./state/incoming_propertyquarry_trust}"
)
CANDIDATE_DROP_COMPOSE_GID = (
    "${PROPERTYQUARRY_OODA_TRUST_CANDIDATE_DROP_GID:-1000}"
)
CANDIDATE_DROP_RUNTIME_TARGET = (
    "/run/propertyquarry/ooda-producer-trust-source"
)
SCHEDULER_RUNTIME_UID = 10001
SCHEDULER_RUNTIME_GID = 10001
MAX_SOURCE_CONFIG_BYTES = 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PROJECT = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}\Z")
_REASON = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
_READY = re.compile(
    rb"READY local Docker deployment "
    rb"runtime=([0-9a-f]{40}) "
    rb"envelope=([0-9a-f]{40}) "
    rb"web=(sha256:[0-9a-f]{64}) "
    rb"render=(sha256:[0-9a-f]{64})\n?\Z"
)


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _canonical(value: Mapping[str, Any]) -> bytes:
    return approved.canonical_json_bytes(dict(value))


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _parse_fresh_timestamp(
    value: object,
    *,
    now: datetime,
    max_age_seconds: float,
) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    normalized = parsed.astimezone(timezone.utc)
    age = (now - normalized).total_seconds()
    if age < 0 or age > float(max_age_seconds):
        return None
    return normalized.isoformat()


def _safe_reason(value: object, *, default: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if _REASON.fullmatch(normalized) else default


def _bounded_count(value: object, *, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field}_not_admissible")
    if value < 0 or value > maximum:
        raise ValueError(f"{field}_not_admissible")
    return value


def _project_name(value: object) -> str:
    normalized = str(value or "").strip()
    if _PROJECT.fullmatch(normalized) is None:
        raise ValueError("scheduler_activation_project_not_admissible")
    return normalized


def _continuity_projection(
    value: Mapping[str, Any],
    *,
    now: datetime,
    max_age_seconds: float,
) -> dict[str, Any]:
    progress = value.get("progress")
    source_evidence = value.get("source_evidence")
    if not isinstance(progress, Mapping) or not isinstance(
        source_evidence, Mapping
    ):
        raise ValueError("scheduler_activation_continuity_not_admissible")
    runtime = source_evidence.get("runtime_observation")
    witness = source_evidence.get("scheduler_iteration_witness")
    if not isinstance(runtime, Mapping) or not isinstance(witness, Mapping):
        raise ValueError("scheduler_activation_continuity_not_admissible")
    updated_at = _parse_fresh_timestamp(
        value.get("updated_at"),
        now=now,
        max_age_seconds=max_age_seconds,
    )
    observed_at = _parse_fresh_timestamp(
        value.get("continuity_observed_at"),
        now=now,
        max_age_seconds=max_age_seconds,
    )
    state = str(value.get("continuity_state") or "").strip()
    receipt_digest = str(value.get("continuity_receipt_sha256") or "").strip()
    runtime_digest = str(runtime.get("sha256") or "").strip()
    witness_digest = str(witness.get("iteration_witness_sha256") or "").strip()
    cycle_digest = str(witness.get("cycle_receipt_sha256") or "").strip()
    persistent = value.get("persistent_reevaluation_verified") is True
    if not (
        value.get("schema") == continuity.VERIFY_SCHEMA
        and value.get("status") == "verified"
        and state in {"active", "inactive", "degraded"}
        and updated_at is not None
        and observed_at is not None
        and _SHA256.fullmatch(receipt_digest)
        and _SHA256.fullmatch(runtime_digest)
        and progress.get("current_evidence_verified") is True
        and progress.get("receipt_integrity_verified") is True
        and value.get("verification_receipt_persisted") is True
        and persistent is (state == "active")
        and value.get("automatic_execution_allowed") is False
        and value.get("execution_authorized") is False
        and value.get("deployment_or_restart_authorized") is False
        and value.get("protected_operation_executed") is False
        and value.get("provider_quota_consumption_allowed") is False
        and value.get("delivery_authorized") is False
        and (
            state != "active"
            or (
                _SHA256.fullmatch(witness_digest)
                and _SHA256.fullmatch(cycle_digest)
                and witness.get("cycle_binding_verified") is True
                and witness.get("persistent_reevaluation_verified") is True
            )
        )
        and (
            state != "inactive"
            or (
                runtime.get("scheduler_condition") == "absent"
                and runtime.get("scheduler_container_count") == 0
                and witness.get("status") == "absent"
            )
        )
    ):
        raise ValueError("scheduler_activation_continuity_not_admissible")
    return {
        "status": "verified",
        "updated_at": updated_at,
        "observed_at": observed_at,
        "continuity_state": state,
        "continuity_receipt_sha256": receipt_digest,
        "runtime_observation_sha256": runtime_digest,
        "scheduler_iteration_witness_sha256": witness_digest,
        "cycle_receipt_sha256": cycle_digest,
        "persistent_reevaluation_verified": persistent,
    }


def _script_digest(path: Path = DEPLOY_SCRIPT) -> str:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise ValueError("scheduler_activation_preflight_script_not_admissible")
    return _sha256(path.read_bytes())


def _trusted_source_bytes(path: Path, *, field: str) -> tuple[bytes, str]:
    target = Path(path).absolute()
    descriptor = -1
    try:
        descriptor = os.open(
            target,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        opened = os.fstat(descriptor)
        identity = (
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
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid not in {0, os.geteuid()}
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) & 0o022
            or not 0 < opened.st_size <= MAX_SOURCE_CONFIG_BYTES
        ):
            raise ValueError(f"{field}_not_admissible")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(
                descriptor,
                min(64 * 1024, MAX_SOURCE_CONFIG_BYTES + 1 - total),
            )
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_SOURCE_CONFIG_BYTES:
                raise ValueError(f"{field}_not_admissible")
            chunks.append(chunk)
        raw = b"".join(chunks)
        after_opened = os.fstat(descriptor)
        after_named = target.lstat()
        for metadata in (after_opened, after_named):
            if (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mode,
                metadata.st_uid,
                metadata.st_gid,
                metadata.st_nlink,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            ) != identity:
                raise ValueError(f"{field}_changed_while_read")
        if len(raw) != opened.st_size:
            raise ValueError(f"{field}_changed_while_read")
        return raw, _sha256(raw)
    except (OSError, ValueError) as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError(f"{field}_not_admissible") from exc
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _candidate_drop_observation(
    *,
    status: str,
    observed_at: datetime,
    blocking_reason: str,
    source_path_sha256: str,
    compose_sha256: str = "",
    runtime_image_spec_sha256: str = "",
    configured_group_gid: int = 0,
    directory_present: bool = False,
    directory_mode: str = "",
    directory_uid: int = 0,
    directory_gid: int = 0,
    directory_type_verified: bool = False,
    directory_owner_verified: bool = False,
    directory_group_verified: bool = False,
    directory_permissions_verified: bool = False,
    compose_read_only_bind_verified: bool = False,
    compose_create_host_path_disabled: bool = False,
    compose_supplemental_group_verified: bool = False,
    scheduler_non_root_identity_verified: bool = False,
) -> dict[str, Any]:
    ready = status == "ready"
    return {
        "schema": CANDIDATE_DROP_SCHEMA,
        "status": status,
        "observed_at": observed_at.isoformat(),
        "blocking_reason": blocking_reason,
        "source_path_sha256": source_path_sha256,
        "source_path_recorded": False,
        "entry_names_recorded": False,
        "compose_sha256": compose_sha256,
        "runtime_image_spec_sha256": runtime_image_spec_sha256,
        "configured_group_gid": configured_group_gid,
        "directory_present": directory_present,
        "directory_mode": directory_mode,
        "directory_uid": directory_uid,
        "directory_gid": directory_gid,
        "directory_type_verified": directory_type_verified,
        "directory_owner_verified": directory_owner_verified,
        "directory_group_verified": directory_group_verified,
        "directory_permissions_verified": directory_permissions_verified,
        "compose_read_only_bind_verified": compose_read_only_bind_verified,
        "compose_create_host_path_disabled": (
            compose_create_host_path_disabled
        ),
        "compose_supplemental_group_verified": (
            compose_supplemental_group_verified
        ),
        "scheduler_runtime_uid": SCHEDULER_RUNTIME_UID,
        "scheduler_runtime_gid": SCHEDULER_RUNTIME_GID,
        "scheduler_non_root_identity_verified": (
            scheduler_non_root_identity_verified
        ),
        "candidate_drop_ready": ready,
        "read_only_observation": True,
        "directory_created": False,
        "directory_modified": False,
        "build_performed": False,
        "deployment_or_restart_performed": False,
        "provider_quota_consumed": False,
        "delivery_attempted": False,
    }


def _candidate_drop_not_run(
    *,
    reason: str,
    now: datetime,
) -> dict[str, Any]:
    return _candidate_drop_observation(
        status="not_run",
        observed_at=now,
        blocking_reason=reason,
        source_path_sha256="",
    )


def observe_candidate_drop_posture(
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Prove the non-root scheduler can read, but never write, the host drop."""

    observed_at = _now(now)
    raw_path = str(os.getenv(CANDIDATE_DROP_ENV) or "").strip()
    source = Path(raw_path) if raw_path else DEFAULT_CANDIDATE_DROP
    target = source if source.is_absolute() else ROOT / source
    target = target.absolute()
    source_path_sha256 = _sha256(os.fsencode(str(target)))
    configured_gid_text = str(
        os.getenv(CANDIDATE_DROP_GID_ENV) or "1000"
    ).strip()
    configured_gid = 0
    compose_digest = ""
    dockerfile_digest = ""
    directory_present = False
    directory_mode = ""
    directory_uid = 0
    directory_gid = 0
    directory_type_verified = False
    directory_owner_verified = False
    directory_group_verified = False
    directory_permissions_verified = False
    group_verified = False
    read_only_bind = False
    create_disabled = False
    non_root_identity = False
    try:
        if not configured_gid_text.isascii() or not configured_gid_text.isdigit():
            raise ValueError("candidate_drop_group_not_admissible")
        configured_gid = int(configured_gid_text)
        if not 1 <= configured_gid <= 2**31 - 1:
            raise ValueError("candidate_drop_group_not_admissible")
        compose_raw, compose_digest = _trusted_source_bytes(
            COMPOSE_PATH,
            field="candidate_drop_compose_source",
        )
        dockerfile_raw, dockerfile_digest = _trusted_source_bytes(
            WEB_DOCKERFILE,
            field="candidate_drop_runtime_image_source",
        )
        compose = yaml.safe_load(compose_raw)
        services = compose.get("services") if isinstance(compose, Mapping) else None
        scheduler = (
            services.get("propertyquarry-scheduler")
            if isinstance(services, Mapping)
            else None
        )
        if not isinstance(scheduler, Mapping):
            raise ValueError("candidate_drop_scheduler_service_not_admissible")
        group_add = scheduler.get("group_add")
        group_verified = bool(
            isinstance(group_add, list)
            and group_add == [CANDIDATE_DROP_COMPOSE_GID]
        )
        matching_mounts = [
            row
            for row in list(scheduler.get("volumes") or [])
            if isinstance(row, Mapping)
            and row.get("target") == CANDIDATE_DROP_RUNTIME_TARGET
        ]
        mount = matching_mounts[0] if len(matching_mounts) == 1 else {}
        bind = mount.get("bind") if isinstance(mount, Mapping) else None
        read_only_bind = bool(
            mount.get("type") == "bind"
            and mount.get("source") == CANDIDATE_DROP_COMPOSE_SOURCE
            and mount.get("read_only") is True
        )
        create_disabled = bool(
            isinstance(bind, Mapping)
            and bind.get("create_host_path") is False
        )
        dockerfile_lines = dockerfile_raw.decode(
            "utf-8", errors="strict"
        ).splitlines()
        user_lines = [
            line.strip() for line in dockerfile_lines if line.startswith("USER ")
        ]
        non_root_identity = bool(
            scheduler.get("user") in {None, ""}
            and user_lines
            and user_lines[-1]
            == f"USER {SCHEDULER_RUNTIME_UID}:{SCHEDULER_RUNTIME_GID}"
        )
        if not group_verified:
            raise ValueError("candidate_drop_supplemental_group_not_admissible")
        if not read_only_bind or not create_disabled:
            raise ValueError("candidate_drop_bind_mount_not_admissible")
        if not non_root_identity:
            raise ValueError("candidate_drop_runtime_identity_not_admissible")
        try:
            metadata = target.lstat()
        except FileNotFoundError as exc:
            raise ValueError("candidate_drop_directory_absent") from exc
        directory_present = True
        directory_mode = f"{stat.S_IMODE(metadata.st_mode):04o}"
        directory_uid = metadata.st_uid
        directory_gid = metadata.st_gid
        directory_type_verified = stat.S_ISDIR(metadata.st_mode)
        directory_owner_verified = metadata.st_uid in {0, os.geteuid()}
        directory_group_verified = metadata.st_gid == configured_gid
        directory_permissions_verified = (
            stat.S_IMODE(metadata.st_mode) == 0o750
        )
        if not directory_type_verified:
            raise ValueError("candidate_drop_directory_type_not_admissible")
        if not directory_owner_verified:
            raise ValueError("candidate_drop_directory_owner_not_admissible")
        if not directory_group_verified:
            raise ValueError("candidate_drop_directory_group_not_admissible")
        if not directory_permissions_verified:
            raise ValueError("candidate_drop_directory_mode_not_admissible")
        return _candidate_drop_observation(
            status="ready",
            observed_at=observed_at,
            blocking_reason="",
            source_path_sha256=source_path_sha256,
            compose_sha256=compose_digest,
            runtime_image_spec_sha256=dockerfile_digest,
            configured_group_gid=configured_gid,
            directory_present=True,
            directory_mode=directory_mode,
            directory_uid=directory_uid,
            directory_gid=directory_gid,
            directory_type_verified=True,
            directory_owner_verified=True,
            directory_group_verified=True,
            directory_permissions_verified=True,
            compose_read_only_bind_verified=True,
            compose_create_host_path_disabled=True,
            compose_supplemental_group_verified=True,
            scheduler_non_root_identity_verified=True,
        )
    except (OSError, TypeError, UnicodeError, ValueError, yaml.YAMLError) as exc:
        reason = _safe_reason(
            str(exc),
            default="candidate_drop_posture_not_admissible",
        )
        return _candidate_drop_observation(
            status="blocked",
            observed_at=observed_at,
            blocking_reason=reason,
            source_path_sha256=source_path_sha256,
            compose_sha256=compose_digest,
            runtime_image_spec_sha256=dockerfile_digest,
            configured_group_gid=configured_gid,
            directory_present=directory_present,
            directory_mode=directory_mode,
            directory_uid=directory_uid,
            directory_gid=directory_gid,
            directory_type_verified=directory_type_verified,
            directory_owner_verified=directory_owner_verified,
            directory_group_verified=directory_group_verified,
            directory_permissions_verified=directory_permissions_verified,
            compose_read_only_bind_verified=read_only_bind,
            compose_create_host_path_disabled=create_disabled,
            compose_supplemental_group_verified=group_verified,
            scheduler_non_root_identity_verified=non_root_identity,
        )


def _candidate_drop_projection(
    value: Mapping[str, Any],
    *,
    now: datetime,
    max_age_seconds: float,
) -> dict[str, Any]:
    expected_keys = {
        "schema",
        "status",
        "observed_at",
        "blocking_reason",
        "source_path_sha256",
        "source_path_recorded",
        "entry_names_recorded",
        "compose_sha256",
        "runtime_image_spec_sha256",
        "configured_group_gid",
        "directory_present",
        "directory_mode",
        "directory_uid",
        "directory_gid",
        "directory_type_verified",
        "directory_owner_verified",
        "directory_group_verified",
        "directory_permissions_verified",
        "compose_read_only_bind_verified",
        "compose_create_host_path_disabled",
        "compose_supplemental_group_verified",
        "scheduler_runtime_uid",
        "scheduler_runtime_gid",
        "scheduler_non_root_identity_verified",
        "candidate_drop_ready",
        "read_only_observation",
        "directory_created",
        "directory_modified",
        "build_performed",
        "deployment_or_restart_performed",
        "provider_quota_consumed",
        "delivery_attempted",
    }
    observed_at = _parse_fresh_timestamp(
        value.get("observed_at"),
        now=now,
        max_age_seconds=max_age_seconds,
    )
    status_value = str(value.get("status") or "")
    reason = str(value.get("blocking_reason") or "")
    source_path_sha256 = str(value.get("source_path_sha256") or "")
    compose_sha256 = str(value.get("compose_sha256") or "")
    image_sha256 = str(value.get("runtime_image_spec_sha256") or "")
    configured_gid = value.get("configured_group_gid")
    directory_uid = value.get("directory_uid")
    directory_gid = value.get("directory_gid")
    counts = (configured_gid, directory_uid, directory_gid)
    ready_checks = (
        "directory_type_verified",
        "directory_owner_verified",
        "directory_group_verified",
        "directory_permissions_verified",
        "compose_read_only_bind_verified",
        "compose_create_host_path_disabled",
        "compose_supplemental_group_verified",
        "scheduler_non_root_identity_verified",
        "candidate_drop_ready",
    )
    if not (
        set(value) == expected_keys
        and value.get("schema") == CANDIDATE_DROP_SCHEMA
        and status_value in {"ready", "blocked", "not_run"}
        and observed_at is not None
        and value.get("source_path_recorded") is False
        and value.get("entry_names_recorded") is False
        and all(
            isinstance(item, int)
            and not isinstance(item, bool)
            and 0 <= item <= 2**31 - 1
            for item in counts
        )
        and value.get("scheduler_runtime_uid") == SCHEDULER_RUNTIME_UID
        and value.get("scheduler_runtime_gid") == SCHEDULER_RUNTIME_GID
        and value.get("read_only_observation") is True
        and value.get("directory_created") is False
        and value.get("directory_modified") is False
        and value.get("build_performed") is False
        and value.get("deployment_or_restart_performed") is False
        and value.get("provider_quota_consumed") is False
        and value.get("delivery_attempted") is False
        and all(isinstance(value.get(key), bool) for key in ready_checks)
        and isinstance(value.get("directory_present"), bool)
        and re.fullmatch(r"[0-7]{4}|", str(value.get("directory_mode") or ""))
        and (
            status_value == "ready"
            or (
                _REASON.fullmatch(reason)
                and value.get("candidate_drop_ready") is False
            )
        )
        and (
            status_value != "ready"
            or (
                not reason
                and _SHA256.fullmatch(source_path_sha256)
                and _SHA256.fullmatch(compose_sha256)
                and _SHA256.fullmatch(image_sha256)
                and 1 <= configured_gid
                and value.get("directory_present") is True
                and value.get("directory_mode") == "0750"
                and directory_gid == configured_gid
                and all(value.get(key) is True for key in ready_checks)
            )
        )
        and (
            status_value != "not_run"
            or (
                not source_path_sha256
                and not compose_sha256
                and not image_sha256
                and configured_gid == 0
                and value.get("directory_present") is False
                and not str(value.get("directory_mode") or "")
                and directory_uid == 0
                and directory_gid == 0
                and all(value.get(key) is False for key in ready_checks)
            )
        )
    ):
        raise ValueError("scheduler_activation_candidate_drop_not_admissible")
    return {**dict(value), "observed_at": observed_at}


def _release_tree_identity(
    *, timeout_seconds: int = RELEASE_TREE_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """Return a path-free identity for the exact release worktree observed."""
    try:
        head_result = subprocess.run(
            [
                "/usr/bin/git",
                "rev-parse",
                "--verify",
                "HEAD^{commit}",
            ],
            cwd=ROOT,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        status_result = subprocess.run(
            [
                "/usr/bin/git",
                "-c",
                "core.quotepath=false",
                "-c",
                "status.renames=false",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ],
            cwd=ROOT,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("scheduler_activation_release_tree_unavailable") from exc
    head_stdout = _output_bytes(head_result.stdout)
    head_stderr = _output_bytes(head_result.stderr)
    status_stdout = _output_bytes(status_result.stdout)
    status_stderr = _output_bytes(status_result.stderr)
    head_commit = head_stdout.decode("ascii", errors="replace").strip()
    if not (
        head_result.returncode == 0
        and status_result.returncode == 0
        and not head_stderr
        and not status_stderr
        and _COMMIT.fullmatch(head_commit)
        and len(status_stdout) <= MAX_WORKTREE_STATUS_BYTES
    ):
        raise ValueError("scheduler_activation_release_tree_unavailable")
    return {
        "release_tree_identity_verified": True,
        "release_head_commit_sha": head_commit,
        "release_worktree_status_sha256": _sha256(status_stdout),
        "release_worktree_status_bytes": len(status_stdout),
        "release_worktree_clean": not status_stdout,
        "changed_paths_recorded": False,
    }


def _output_bytes(value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    return value.encode("utf-8", errors="replace")


def _preflight_reason(output: bytes) -> str:
    text = output.decode("utf-8", errors="replace")
    if "worktree_not_clean" in text:
        return "release_worktree_not_clean"
    if "Required local deployment value is missing:" in text:
        return "required_runtime_configuration_missing"
    if "Expected local web image is unavailable:" in text or (
        "Expected local render image is unavailable:" in text
    ):
        return "release_image_unavailable"
    if "repository-role audit failed:" in text:
        return "release_repository_role_not_admissible"
    if "Release manifest" in text or "release manifest" in text:
        return "release_manifest_not_admissible"
    if "OODA source" in text or "OODA signal ingress" in text:
        return "ooda_source_not_admissible"
    if "Docker Unix socket" in text or "Docker context" in text:
        return "docker_release_authority_not_admissible"
    if "env file must be owned" in text:
        return "runtime_environment_file_not_admissible"
    return "activation_preflight_failed"


def _observation(
    *,
    status: str,
    observed_at: datetime,
    blocking_reason: str,
    duration_ms: int,
    exit_code: int,
    script_sha256: str,
    stdout: bytes,
    stderr: bytes,
    project: str,
    runtime_commit_sha: str = "",
    envelope_commit_sha: str = "",
    web_image_digest: str = "",
    render_image_digest: str = "",
    release_tree_identity: Mapping[str, Any] | None = None,
    candidate_drop_posture: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    tree_identity = dict(release_tree_identity or {})
    candidate_drop = dict(
        candidate_drop_posture
        or _candidate_drop_not_run(
            reason="candidate_drop_posture_not_observed",
            now=observed_at,
        )
    )
    return {
        "schema": OBSERVATION_SCHEMA,
        "status": status,
        "observed_at": observed_at.isoformat(),
        "blocking_reason": blocking_reason,
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "preflight_script_sha256": script_sha256,
        "stdout_sha256": _sha256(stdout),
        "stderr_sha256": _sha256(stderr),
        "stdout_bytes": len(stdout),
        "stderr_bytes": len(stderr),
        "runtime_commit_sha": runtime_commit_sha,
        "envelope_commit_sha": envelope_commit_sha,
        "web_image_digest": web_image_digest,
        "render_image_digest": render_image_digest,
        "release_tree_identity_verified": (
            tree_identity.get("release_tree_identity_verified") is True
        ),
        "release_head_commit_sha": str(
            tree_identity.get("release_head_commit_sha") or ""
        ),
        "release_worktree_status_sha256": str(
            tree_identity.get("release_worktree_status_sha256") or ""
        ),
        "release_worktree_status_bytes": int(
            tree_identity.get("release_worktree_status_bytes") or 0
        ),
        "release_worktree_clean": (
            tree_identity.get("release_worktree_clean") is True
        ),
        "changed_paths_recorded": False,
        "candidate_drop": candidate_drop,
        "compose_project": project,
        "preflight_only": True,
        "no_build": True,
        "build_performed": False,
        "deployment_or_restart_performed": False,
        "provider_quota_consumed": False,
        "delivery_attempted": False,
    }


def observe_scheduler_activation_preflight(
    *,
    project: str = "property",
    now: datetime | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    observed_at = _now(now)
    normalized_project = _project_name(project)
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 5 <= timeout_seconds <= 300
    ):
        raise ValueError("scheduler_activation_preflight_timeout_not_admissible")
    script_digest = _script_digest()
    candidate_drop_posture = observe_candidate_drop_posture(now=observed_at)
    try:
        release_tree_identity = _release_tree_identity()
    except ValueError:
        return _observation(
            status="failed",
            observed_at=observed_at,
            blocking_reason="release_tree_identity_unavailable",
            duration_ms=0,
            exit_code=126,
            script_sha256=script_digest,
            stdout=b"",
            stderr=b"",
            project=normalized_project,
            candidate_drop_posture=candidate_drop_posture,
        )
    if candidate_drop_posture.get("status") != "ready":
        return _observation(
            status="blocked",
            observed_at=observed_at,
            blocking_reason=str(
                candidate_drop_posture.get("blocking_reason")
                or "candidate_drop_posture_not_admissible"
            ),
            duration_ms=0,
            exit_code=126,
            script_sha256=script_digest,
            stdout=b"",
            stderr=b"",
            project=normalized_project,
            release_tree_identity=release_tree_identity,
            candidate_drop_posture=candidate_drop_posture,
        )
    environment = os.environ.copy()
    environment["PROPERTYQUARRY_COMPOSE_PROJECT_NAME"] = normalized_project
    started = time.monotonic()
    try:
        result = subprocess.run(
            [
                "/usr/bin/bash",
                str(DEPLOY_SCRIPT),
                "--preflight-only",
                "--no-build",
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = _output_bytes(exc.stdout)[:MAX_CAPTURE_BYTES]
        stderr = _output_bytes(exc.stderr)[:MAX_CAPTURE_BYTES]
        return _observation(
            status="timeout",
            observed_at=observed_at,
            blocking_reason="activation_preflight_timeout",
            duration_ms=min(
                timeout_seconds * 1000,
                max(0, int((time.monotonic() - started) * 1000)),
            ),
            exit_code=124,
            script_sha256=script_digest,
            stdout=stdout,
            stderr=stderr,
            project=normalized_project,
            release_tree_identity=release_tree_identity,
            candidate_drop_posture=candidate_drop_posture,
        )
    except OSError:
        return _observation(
            status="failed",
            observed_at=observed_at,
            blocking_reason="activation_preflight_process_unavailable",
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            exit_code=126,
            script_sha256=script_digest,
            stdout=b"",
            stderr=b"",
            project=normalized_project,
            release_tree_identity=release_tree_identity,
            candidate_drop_posture=candidate_drop_posture,
        )
    stdout = _output_bytes(result.stdout)
    stderr = _output_bytes(result.stderr)
    duration_ms = max(0, int((time.monotonic() - started) * 1000))
    if len(stdout) > MAX_CAPTURE_BYTES or len(stderr) > MAX_CAPTURE_BYTES:
        return _observation(
            status="failed",
            observed_at=observed_at,
            blocking_reason="activation_preflight_output_too_large",
            duration_ms=duration_ms,
            exit_code=int(result.returncode),
            script_sha256=script_digest,
            stdout=stdout[:MAX_CAPTURE_BYTES],
            stderr=stderr[:MAX_CAPTURE_BYTES],
            project=normalized_project,
            release_tree_identity=release_tree_identity,
            candidate_drop_posture=candidate_drop_posture,
        )
    match = _READY.fullmatch(stdout)
    if result.returncode == 0 and match is not None and not stderr:
        return _observation(
            status="ready",
            observed_at=observed_at,
            blocking_reason="",
            duration_ms=duration_ms,
            exit_code=0,
            script_sha256=script_digest,
            stdout=stdout,
            stderr=stderr,
            project=normalized_project,
            runtime_commit_sha=match.group(1).decode("ascii"),
            envelope_commit_sha=match.group(2).decode("ascii"),
            web_image_digest=match.group(3).decode("ascii"),
            render_image_digest=match.group(4).decode("ascii"),
            release_tree_identity=release_tree_identity,
            candidate_drop_posture=candidate_drop_posture,
        )
    combined = stdout + b"\n" + stderr
    return _observation(
        status="blocked" if result.returncode != 0 else "failed",
        observed_at=observed_at,
        blocking_reason=(
            _preflight_reason(combined)
            if result.returncode != 0
            else "activation_preflight_output_not_admissible"
        ),
        duration_ms=duration_ms,
        exit_code=int(result.returncode),
        script_sha256=script_digest,
        stdout=stdout,
        stderr=stderr,
        project=normalized_project,
        release_tree_identity=release_tree_identity,
        candidate_drop_posture=candidate_drop_posture,
    )


def _not_run_observation(
    *,
    project: str,
    reason: str,
    now: datetime,
) -> dict[str, Any]:
    return _observation(
        status="not_run",
        observed_at=now,
        blocking_reason=reason,
        duration_ms=0,
        exit_code=-1,
        script_sha256=_script_digest(),
        stdout=b"",
        stderr=b"",
        project=project,
        candidate_drop_posture=_candidate_drop_not_run(
            reason=reason,
            now=now,
        ),
    )


def _preflight_projection(
    value: Mapping[str, Any],
    *,
    now: datetime,
    max_age_seconds: float,
    project: str,
) -> dict[str, Any]:
    expected_keys = {
        "schema",
        "status",
        "observed_at",
        "blocking_reason",
        "duration_ms",
        "exit_code",
        "preflight_script_sha256",
        "stdout_sha256",
        "stderr_sha256",
        "stdout_bytes",
        "stderr_bytes",
        "runtime_commit_sha",
        "envelope_commit_sha",
        "web_image_digest",
        "render_image_digest",
        "release_tree_identity_verified",
        "release_head_commit_sha",
        "release_worktree_status_sha256",
        "release_worktree_status_bytes",
        "release_worktree_clean",
        "changed_paths_recorded",
        "candidate_drop",
        "compose_project",
        "preflight_only",
        "no_build",
        "build_performed",
        "deployment_or_restart_performed",
        "provider_quota_consumed",
        "delivery_attempted",
    }
    observed_at = _parse_fresh_timestamp(
        value.get("observed_at"),
        now=now,
        max_age_seconds=max_age_seconds,
    )
    status_value = str(value.get("status") or "")
    reason = _safe_reason(
        value.get("blocking_reason"),
        default="activation_preflight_not_admissible",
    )
    duration_ms = _bounded_count(
        value.get("duration_ms"), field="duration_ms", maximum=300_000
    )
    exit_code = value.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise ValueError("activation_preflight_exit_code_not_admissible")
    stdout_bytes = _bounded_count(
        value.get("stdout_bytes"),
        field="stdout_bytes",
        maximum=MAX_CAPTURE_BYTES,
    )
    stderr_bytes = _bounded_count(
        value.get("stderr_bytes"),
        field="stderr_bytes",
        maximum=MAX_CAPTURE_BYTES,
    )
    runtime_commit = str(value.get("runtime_commit_sha") or "")
    envelope_commit = str(value.get("envelope_commit_sha") or "")
    web_image = str(value.get("web_image_digest") or "")
    render_image = str(value.get("render_image_digest") or "")
    tree_identity_verified = value.get("release_tree_identity_verified") is True
    release_head_commit = str(value.get("release_head_commit_sha") or "")
    worktree_digest = str(value.get("release_worktree_status_sha256") or "")
    worktree_bytes = _bounded_count(
        value.get("release_worktree_status_bytes"),
        field="release_worktree_status_bytes",
        maximum=MAX_WORKTREE_STATUS_BYTES,
    )
    worktree_clean = value.get("release_worktree_clean") is True
    candidate_drop = _candidate_drop_projection(
        value.get("candidate_drop")
        if isinstance(value.get("candidate_drop"), Mapping)
        else {},
        now=now,
        max_age_seconds=max_age_seconds,
    )
    if not (
        set(value) == expected_keys
        and value.get("schema") == OBSERVATION_SCHEMA
        and status_value in {"ready", "blocked", "timeout", "failed", "not_run"}
        and observed_at is not None
        and value.get("compose_project") == project
        and _SHA256.fullmatch(str(value.get("preflight_script_sha256") or ""))
        and value.get("preflight_script_sha256") == _script_digest()
        and _SHA256.fullmatch(str(value.get("stdout_sha256") or ""))
        and _SHA256.fullmatch(str(value.get("stderr_sha256") or ""))
        and value.get("preflight_only") is True
        and value.get("no_build") is True
        and value.get("build_performed") is False
        and value.get("deployment_or_restart_performed") is False
        and value.get("provider_quota_consumed") is False
        and value.get("delivery_attempted") is False
        and value.get("changed_paths_recorded") is False
        and (
            not tree_identity_verified
            or (
                _COMMIT.fullmatch(release_head_commit)
                and _SHA256.fullmatch(worktree_digest)
                and worktree_clean is (worktree_bytes == 0)
                and (
                    not worktree_clean
                    or worktree_digest == _sha256(b"")
                )
            )
        )
        and (
            tree_identity_verified
            or (
                status_value in {"failed", "not_run"}
                and not release_head_commit
                and not worktree_digest
                and worktree_bytes == 0
                and worktree_clean is False
            )
        )
        and (
            status_value != "ready"
            or (
                exit_code == 0
                and reason == "activation_preflight_not_admissible"
                and value.get("blocking_reason") == ""
                and _COMMIT.fullmatch(runtime_commit)
                and _COMMIT.fullmatch(envelope_commit)
                and _IMAGE.fullmatch(web_image)
                and _IMAGE.fullmatch(render_image)
                and tree_identity_verified
                and worktree_clean
                and release_head_commit == envelope_commit
            )
        )
        and (
            status_value == "ready"
            or (
                _REASON.fullmatch(reason)
                and not runtime_commit
                and not envelope_commit
                and not web_image
                and not render_image
            )
        )
        and (status_value != "not_run" or exit_code == -1)
        and (status_value == "not_run" or 0 <= exit_code <= 255)
        and (
            reason != "release_worktree_not_clean"
            or (tree_identity_verified and not worktree_clean)
        )
        and (
            reason != "release_tree_identity_unavailable"
            or not tree_identity_verified
        )
        and (status_value != "ready" or candidate_drop["status"] == "ready")
        and (
            candidate_drop["status"] != "blocked"
            or (
                status_value == "blocked"
                and reason == candidate_drop["blocking_reason"]
            )
        )
        and (
            status_value != "not_run"
            or candidate_drop["status"] == "not_run"
        )
        and (
            status_value == "not_run"
            or candidate_drop["status"] != "not_run"
        )
    ):
        raise ValueError("scheduler_activation_preflight_not_admissible")
    return {
        **dict(value),
        "observed_at": observed_at,
        "duration_ms": duration_ms,
        "stdout_bytes": stdout_bytes,
        "stderr_bytes": stderr_bytes,
        "release_worktree_status_bytes": worktree_bytes,
        "candidate_drop": candidate_drop,
    }


def _readiness_state(
    continuity_source: Mapping[str, Any],
    preflight: Mapping[str, Any],
) -> tuple[str, str, str, bool]:
    continuity_state = str(continuity_source.get("continuity_state") or "")
    if continuity_state == "active":
        return (
            "not_required",
            "scheduler_continuity_active",
            "continue fresh continuity verification; no activation action is required",
            False,
        )
    if continuity_state != "inactive":
        return (
            "blocked",
            "scheduler_continuity_not_eligible_for_activation",
            "repair or reobserve degraded scheduler continuity before considering activation",
            False,
        )
    if preflight.get("status") != "ready":
        return (
            "blocked",
            str(
                preflight.get("blocking_reason")
                or "activation_preflight_not_ready"
            ),
            "resolve the named non-mutating activation preflight blocker, then regenerate this receipt",
            False,
        )
    return (
        "ready_for_authorization",
        "explicit_scheduler_activation_authorization_required",
        "stage an exact, expiring full-release activation authorization request; do not deploy or restart",
        True,
    )


def build_scheduler_activation_readiness_receipt(
    *,
    scheduler_continuity: Mapping[str, Any],
    preflight_observation: Mapping[str, Any],
    project: str = "property",
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    normalized_project = _project_name(project)
    continuity_source = _continuity_projection(
        scheduler_continuity,
        now=observed_now,
        max_age_seconds=max_age_seconds,
    )
    preflight = _preflight_projection(
        preflight_observation,
        now=observed_now,
        max_age_seconds=max_age_seconds,
        project=normalized_project,
    )
    candidate_drop = dict(preflight["candidate_drop"])
    state, blocking_reason, next_action, authorization_needed = _readiness_state(
        continuity_source, preflight
    )
    if continuity_source["continuity_state"] == "active" and not (
        preflight["status"] == "not_run"
        and preflight["blocking_reason"] == "scheduler_continuity_active"
    ):
        raise ValueError("scheduler_activation_active_preflight_not_admissible")
    if continuity_source["continuity_state"] != "inactive" and (
        preflight["status"] != "not_run"
    ):
        raise ValueError("scheduler_activation_preflight_scope_not_admissible")
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "status": state,
        "updated_at": observed_now.isoformat(),
        "blocking_reason": blocking_reason,
        "next_action": next_action,
        "action_required": authorization_needed,
        "interrupt_operator": False,
        "scope": {
            "operation": "authoritative_local_propertyquarry_deployment",
            "compose_project": normalized_project,
            "activation_target": (
                "propertyquarry_scheduler_via_full_release_envelope"
            ),
            "preflight_command": (
                "bash scripts/deploy_propertyquarry.sh --preflight-only --no-build"
            ),
            "deployment_command": (
                "bash scripts/deploy_propertyquarry.sh --no-build"
            ),
            "requested_execution_mode": (
                "manual_after_separate_explicit_authorization"
            ),
        },
        "source_evidence": {
            "scheduler_continuity": continuity_source,
            "activation_preflight": preflight,
        },
        "source_bindings": {
            "continuity_receipt_sha256": continuity_source[
                "continuity_receipt_sha256"
            ],
            "runtime_observation_sha256": continuity_source[
                "runtime_observation_sha256"
            ],
            "preflight_observation_sha256": _sha256(_canonical(preflight)),
            "preflight_script_sha256": preflight["preflight_script_sha256"],
            "release_head_commit_sha": preflight["release_head_commit_sha"],
            "release_worktree_status_sha256": preflight[
                "release_worktree_status_sha256"
            ],
            "candidate_drop_observation_sha256": _sha256(
                _canonical(candidate_drop)
            ),
            "candidate_drop_source_path_sha256": candidate_drop[
                "source_path_sha256"
            ],
            "candidate_drop_compose_sha256": candidate_drop[
                "compose_sha256"
            ],
            "candidate_drop_runtime_image_spec_sha256": candidate_drop[
                "runtime_image_spec_sha256"
            ],
        },
        "progress": {
            "scheduler_continuity_current": True,
            "activation_preflight_current": True,
            "activation_preflight_passed": preflight["status"] == "ready",
            "release_tree_identity_verified": preflight[
                "release_tree_identity_verified"
            ],
            "release_worktree_clean": preflight["release_worktree_clean"],
            "candidate_drop_current": True,
            "candidate_drop_ready": candidate_drop["status"] == "ready",
            "candidate_drop_activation_prerequisite_verified": (
                candidate_drop["status"] == "ready"
                if continuity_source["continuity_state"] == "inactive"
                else candidate_drop["status"] == "not_run"
            ),
            "authorization_request_staged": False,
            "current_evidence_verified": True,
        },
        "authorization_required": authorization_needed,
        "authorization_recorded": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "deployment_or_restart_performed": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "receipt_persisted": True,
        "secret_values_recorded": False,
    }
    receipt["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": _sha256(_canonical(receipt)),
    }
    return receipt


def _blocked(reason: str, *, now: datetime | None = None) -> dict[str, Any]:
    return {
        "schema": VERIFY_SCHEMA,
        "status": "blocked",
        "readiness_state": "blocked",
        "updated_at": _now(now).isoformat(),
        "blocking_reason": reason,
        "next_action": "regenerate activation readiness from fresh continuity and non-mutating preflight evidence",
        "action_required": False,
        "interrupt_operator": False,
        "progress": {"current_evidence_verified": False},
        "authorization_required": False,
        "authorization_recorded": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "deployment_or_restart_performed": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }


def verify_scheduler_activation_readiness_receipt(
    receipt: Mapping[str, Any],
    *,
    scheduler_continuity: Mapping[str, Any],
    preflight_observation: Mapping[str, Any],
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    observed_now = _now(now)
    updated_at = _parse_fresh_timestamp(
        receipt.get("updated_at"),
        now=observed_now,
        max_age_seconds=max_age_seconds,
    )
    if updated_at is None:
        return _blocked(
            "scheduler_activation_readiness_receipt_not_fresh",
            now=observed_now,
        )
    normalized = dict(receipt)
    integrity = normalized.pop("integrity", None)
    if not (
        isinstance(integrity, dict)
        and integrity.get("algorithm") == "sha256"
        and _SHA256.fullmatch(
            str(integrity.get("canonical_payload_sha256") or "")
        )
        and integrity.get("canonical_payload_sha256")
        == _sha256(_canonical(normalized))
    ):
        return _blocked(
            "scheduler_activation_readiness_integrity_invalid",
            now=observed_now,
        )
    scope = receipt.get("scope")
    try:
        rebuilt = build_scheduler_activation_readiness_receipt(
            scheduler_continuity=scheduler_continuity,
            preflight_observation=preflight_observation,
            project=str(
                scope.get("compose_project")
                if isinstance(scope, Mapping)
                else ""
            ),
            now=datetime.fromisoformat(updated_at),
            max_age_seconds=max_age_seconds,
        )
    except (TypeError, ValueError):
        return _blocked(
            "scheduler_activation_readiness_source_not_admissible",
            now=observed_now,
        )
    if dict(receipt) != rebuilt:
        return _blocked(
            "scheduler_activation_readiness_source_binding_mismatch",
            now=observed_now,
        )
    return {
        "schema": VERIFY_SCHEMA,
        "status": "verified",
        "readiness_state": str(receipt.get("status") or ""),
        "updated_at": observed_now.isoformat(),
        "readiness_observed_at": updated_at,
        "readiness_receipt_sha256": _sha256(_canonical(receipt)),
        "blocking_reason": str(receipt.get("blocking_reason") or ""),
        "next_action": str(receipt.get("next_action") or ""),
        "scope": dict(receipt.get("scope") or {}),
        "source_evidence": dict(receipt.get("source_evidence") or {}),
        "source_bindings": dict(receipt.get("source_bindings") or {}),
        "action_required": receipt.get("action_required") is True,
        "interrupt_operator": False,
        "progress": {
            **dict(receipt.get("progress") or {}),
            "receipt_integrity_verified": True,
        },
        "authorization_required": (
            receipt.get("authorization_required") is True
        ),
        "authorization_recorded": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "deployment_or_restart_performed": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }


def materialize_current_scheduler_activation_readiness_bundle(
    *,
    scheduler_continuity: Mapping[str, Any],
    project: str = "property",
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    verification_path: Path = DEFAULT_VERIFICATION_PATH,
    now: datetime | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
    preflight_observation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    observed_now = _now(now)
    normalized_project = _project_name(project)
    continuity_source = _continuity_projection(
        scheduler_continuity,
        now=observed_now,
        max_age_seconds=max_age_seconds,
    )
    if preflight_observation is None:
        if continuity_source["continuity_state"] == "inactive":
            observed_preflight = observe_scheduler_activation_preflight(
                project=normalized_project,
                now=observed_now,
            )
        else:
            observed_preflight = _not_run_observation(
                project=normalized_project,
                reason=(
                    "scheduler_continuity_active"
                    if continuity_source["continuity_state"] == "active"
                    else "scheduler_continuity_not_eligible_for_activation"
                ),
                now=observed_now,
            )
    else:
        observed_preflight = dict(preflight_observation)
    receipt = build_scheduler_activation_readiness_receipt(
        scheduler_continuity=scheduler_continuity,
        preflight_observation=observed_preflight,
        project=normalized_project,
        now=observed_now,
        max_age_seconds=max_age_seconds,
    )
    atomic_write_bytes(
        Path(receipt_path).absolute(),
        _canonical(receipt),
        overwrite=True,
    )
    verification = verify_scheduler_activation_readiness_receipt(
        receipt,
        scheduler_continuity=scheduler_continuity,
        preflight_observation=observed_preflight,
        now=observed_now,
        max_age_seconds=max_age_seconds,
    )
    verification["receipt_path"] = str(Path(receipt_path).absolute())
    verification["verification_path"] = str(
        Path(verification_path).absolute()
    )
    verification["verification_receipt_persisted"] = True
    try:
        atomic_write_bytes(
            Path(verification_path).absolute(),
            _canonical(verification),
            overwrite=True,
        )
    except Exception:
        verification.update(
            {
                "status": "blocked",
                "readiness_state": "blocked",
                "blocking_reason": (
                    "scheduler_activation_readiness_verification_persistence_failed"
                ),
                "action_required": False,
                "authorization_required": False,
                "verification_receipt_persisted": False,
            }
        )
        progress = dict(verification.get("progress") or {})
        progress["current_evidence_verified"] = False
        verification["progress"] = progress
    return verification
