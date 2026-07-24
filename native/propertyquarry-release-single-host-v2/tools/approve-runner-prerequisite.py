#!/usr/bin/env python3
"""Approve the exact protected prerequisite for one governed runner reservation."""

from __future__ import annotations

import argparse
import base64
import fcntl
import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import stat
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable, NoReturn

from cryptography.exceptions import InvalidSignature


TOOLS = Path(__file__).resolve().parent
_RESERVATION_SPEC = importlib.util.spec_from_file_location(
    "propertyquarry_runner_prerequisite_reservation_v2",
    TOOLS / "prepare-runner-reservation.py",
)
if _RESERVATION_SPEC is None or _RESERVATION_SPEC.loader is None:  # pragma: no cover
    raise RuntimeError("runner-prerequisite-reservation-module-unavailable")
reservation = importlib.util.module_from_spec(_RESERVATION_SPEC)
sys.modules[_RESERVATION_SPEC.name] = reservation
_RESERVATION_SPEC.loader.exec_module(reservation)


APPROVAL_ROOT = (
    reservation.RESERVATION_PARENT / "single-host-v2-runner-prerequisite-approvals"
)
LEGACY_INTENT_SCHEMA = (
    "propertyquarry.release-control.single-host-runner-prerequisite-intent.v2"
)
INTENT_SCHEMA = (
    "propertyquarry.release-control.single-host-runner-prerequisite-intent.v3"
)
APPROVAL_SCHEMA = (
    "propertyquarry.release-control.single-host-runner-prerequisite-approval.v3"
)
POST_ATTEMPT_SCHEMA = (
    "propertyquarry.release-control.single-host-runner-prerequisite-post-attempt.v3"
)
RETIREMENT_TERMINAL_SCHEMA = (
    "propertyquarry.release-control.single-host-runner-retirement-terminal.v2"
)
RESULT_SCHEMA = (
    "propertyquarry.release-control.single-host-runner-prerequisite-result.v3"
)
RETIREMENT_RESULT_SCHEMA = (
    "propertyquarry.release-control.single-host-runner-retirement-result.v2"
)
LEGACY_INTENT_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-runner-prerequisite-intent-signature.v2\0"
)
INTENT_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-runner-prerequisite-intent-signature.v3\0"
)
APPROVAL_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-runner-prerequisite-approval-signature.v3\0"
)
POST_ATTEMPT_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-runner-prerequisite-post-attempt-signature.v3\0"
)
RETIREMENT_TERMINAL_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-runner-retirement-terminal-signature.v2\0"
)
GITHUB_API_HOST = "api.github.com"
GITHUB_API_VERSION = "2022-11-28"
GITHUB_USER_AGENT = "propertyquarry-runner-prerequisite-controller-v2"
REPOSITORY = "ArchonMegalon/propertyquarry"
REPOSITORY_API = "repos/ArchonMegalon/propertyquarry"
REPOSITORY_ID = "1257593732"
REPOSITORY_OWNER_ID = "11421547"
WORKFLOW_PATH = ".github/workflows/smoke-runtime.yml"
WORKFLOW_REF = (
    "ArchonMegalon/propertyquarry/.github/workflows/smoke-runtime.yml@refs/heads/main"
)
ENVIRONMENT = "propertyquarry-production"
PREREQUISITE_JOB_KEY = "propertyquarry-protected-dispatch-inputs"
RELEASE_JOB = "propertyquarry-release-v2"
MAXIMUM_RESPONSE_BYTES = 4 * 1024 * 1024
MAXIMUM_TOKEN_BYTES = 2048
MAXIMUM_COMPLETION_WAIT_SECONDS = 900
MAXIMUM_RECONCILIATION_POLLS = 451
NUMERIC_ID = re.compile(r"^[1-9][0-9]{0,19}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
STAGE_NAME = re.compile(r"^\.runner-prerequisite-[0-9a-f]{64}\.tmp$")
RECORD_NAME = re.compile(
    r"^[0-9a-f]{64}\.(?:(?:intent|approved)\.v2|(?:intent|approved|post-attempt)\.v3|retire-terminal\.v2)\.json$"
)


class ApprovalFailure(RuntimeError):
    pass


def fail(code: str) -> NoReturn:
    raise ApprovalFailure(code)


def _checkpoint(_name: str) -> None:
    """Test-only crash boundary."""


def _canonical(value: Any) -> bytes:
    return reservation.materialize.package.canonical_json(value)


def _digest(raw: bytes) -> str:
    return reservation._digest(raw)


def _wire(payload: dict[str, Any], private, key_id: str, domain: bytes) -> bytes:
    canonical = _canonical(payload)
    signature = private.sign(reservation.materialize._framed(domain, canonical))
    return _canonical(
        {
            "payload": payload,
            "signature": base64.urlsafe_b64encode(signature)
            .rstrip(b"=")
            .decode("ascii"),
            "signature_key_id": key_id,
        }
    )


def _verify_wire(
    raw: bytes, *, public, key_id: str, schema: str, domain: bytes
) -> dict[str, Any]:
    expected_version = 3 if schema.endswith(".v3") else 2
    try:
        wrapper = reservation.materialize.package.parse_strict_json(
            raw, "runner-prerequisite-wire"
        )
    except reservation.materialize.package.PackageFailure:
        fail("runner-prerequisite-wire-invalid")
    payload = wrapper.get("payload") if isinstance(wrapper, dict) else None
    signature_text = wrapper.get("signature") if isinstance(wrapper, dict) else None
    if (
        not isinstance(wrapper, dict)
        or set(wrapper) != {"payload", "signature", "signature_key_id"}
        or type(payload) is not dict
        or type(signature_text) is not str
        or wrapper.get("signature_key_id") != key_id
    ):
        fail("runner-prerequisite-wrapper-invalid")
    try:
        signature = base64.b64decode(
            signature_text.encode("ascii") + b"=" * (-len(signature_text) % 4),
            altchars=b"-_",
            validate=True,
        )
        public.verify(
            signature,
            reservation.materialize._framed(domain, _canonical(payload)),
        )
    except (UnicodeEncodeError, ValueError, InvalidSignature):
        fail("runner-prerequisite-signature-invalid")
    if (
        len(signature) != 64
        or signature_text
        != base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        or payload.get("schema") != schema
        or payload.get("version") != expected_version
        or _canonical(wrapper) != raw
    ):
        fail("runner-prerequisite-wire-binding-invalid")
    return payload


def _metadata(path: Path, *, directory: bool, mode: int) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError:
        fail("runner-prerequisite-path-unavailable")
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not expected(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != mode
        or metadata.st_uid != 1000
        or metadata.st_gid != 1000
        or path.resolve() != path
        or (not directory and metadata.st_nlink != 1)
    ):
        fail("runner-prerequisite-path-metadata-invalid")
    return metadata


def _ensure_approval_root() -> None:
    if not APPROVAL_ROOT.exists():
        try:
            os.mkdir(APPROVAL_ROOT, 0o700)
            os.chmod(APPROVAL_ROOT, 0o700)
            reservation._directory_sync(reservation.RESERVATION_PARENT)
        except OSError:
            fail("runner-prerequisite-root-create-failed")
    _metadata(APPROVAL_ROOT, directory=True, mode=0o700)
    try:
        entries = list(APPROVAL_ROOT.iterdir())
    except OSError:
        fail("runner-prerequisite-root-scan-failed")
    removed = False
    for entry in entries:
        if RECORD_NAME.fullmatch(entry.name):
            _metadata(entry, directory=False, mode=0o600)
            continue
        if not STAGE_NAME.fullmatch(entry.name):
            fail("runner-prerequisite-root-entry-invalid")
        _metadata(entry, directory=False, mode=0o600)
        try:
            entry.unlink()
        except OSError:
            fail("runner-prerequisite-stage-recovery-failed")
        removed = True
    if removed:
        reservation._directory_sync(APPROVAL_ROOT)


def _record_paths(reservation_raw: bytes) -> tuple[Path, Path]:
    identity = _digest(reservation_raw).removeprefix("sha256:")
    return (
        APPROVAL_ROOT / f"{identity}.intent.v3.json",
        APPROVAL_ROOT / f"{identity}.approved.v3.json",
    )


def _legacy_record_paths(reservation_raw: bytes) -> tuple[Path, Path]:
    identity = _digest(reservation_raw).removeprefix("sha256:")
    return (
        APPROVAL_ROOT / f"{identity}.intent.v2.json",
        APPROVAL_ROOT / f"{identity}.approved.v2.json",
    )


def _post_attempt_path(reservation_raw: bytes) -> Path:
    identity = _digest(reservation_raw).removeprefix("sha256:")
    return APPROVAL_ROOT / f"{identity}.post-attempt.v3.json"


def _retirement_terminal_path(reservation_raw: bytes) -> Path:
    identity = _digest(reservation_raw).removeprefix("sha256:")
    return APPROVAL_ROOT / f"{identity}.retire-terminal.v2.json"


def _read_record(path: Path) -> bytes:
    before = _metadata(path, directory=False, mode=0o600)
    if not 1 <= before.st_size <= reservation.materialize.package.MAX_JSON_BYTES:
        fail("runner-prerequisite-record-size-invalid")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        opened = os.fstat(descriptor)
        chunks: list[bytes] = []
        total = 0
        while total <= reservation.materialize.package.MAX_JSON_BYTES:
            chunk = os.read(
                descriptor,
                min(
                    65536,
                    reservation.materialize.package.MAX_JSON_BYTES + 1 - total,
                ),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(descriptor)
    except OSError:
        fail("runner-prerequisite-record-read-failed")
    finally:
        if "descriptor" in locals():
            os.close(descriptor)
    raw = b"".join(chunks)
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if (
        not 1 <= len(raw) <= reservation.materialize.package.MAX_JSON_BYTES
        or identity
        != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        or identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        fail("runner-prerequisite-record-mutated")
    return raw


def _publish_record(path: Path, raw: bytes) -> str:
    if path.exists():
        if _read_record(path) != raw:
            fail("runner-prerequisite-record-conflict")
        return "already-published"
    stage = APPROVAL_ROOT / f".runner-prerequisite-{secrets.token_hex(32)}.tmp"
    descriptor = -1
    published = False
    try:
        descriptor = os.open(
            stage,
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
                fail("runner-prerequisite-record-write-failed")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        _checkpoint("after-runner-prerequisite-stage-fsync")
        try:
            reservation.materialize._rename_noreplace(stage, path)
        except reservation.materialize.MaterializeFailure as error:
            if str(error) != "output-exists" or _read_record(path) != raw:
                fail("runner-prerequisite-record-publish-failed")
        reservation._directory_sync(APPROVAL_ROOT)
        published = True
        return "published"
    except OSError:
        fail("runner-prerequisite-record-publish-failed")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not published and stage.exists():
            try:
                stage.unlink()
            except OSError:
                pass


def _timestamp(value: Any, code: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(
        r"20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value
    ):
        fail(code)
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        fail(code)
    return int(parsed.timestamp())


def _numeric(value: Any) -> str | None:
    if type(value) is int and 1 <= value <= 9_999_999_999_999_999_999:
        return str(value)
    if type(value) is str and NUMERIC_ID.fullmatch(value):
        return value
    return None


Requester = Callable[[str, str, bytes | None], tuple[int, bytes]]


def _read_admin_token() -> bytearray:
    if os.environ.get("PROPERTYQUARRY_RUNNER_PREREQUISITE_TOKEN_FD") != "8":
        fail("runner-prerequisite-token-fd-invalid")
    try:
        metadata = os.fstat(8)
    except OSError:
        fail("runner-prerequisite-token-fd-invalid")
    if not stat.S_ISFIFO(metadata.st_mode):
        fail("runner-prerequisite-token-fd-invalid")
    raw = bytearray()
    try:
        while len(raw) <= MAXIMUM_TOKEN_BYTES:
            chunk = os.read(8, MAXIMUM_TOKEN_BYTES + 1 - len(raw))
            if not chunk:
                break
            raw.extend(chunk)
        if raw.endswith(b"\n"):
            raw.pop()
        if (
            not 20 <= len(raw) <= MAXIMUM_TOKEN_BYTES
            or b"\n" in raw
            or b"\r" in raw
            or re.fullmatch(rb"[A-Za-z0-9_]+", raw) is None
        ):
            fail("runner-prerequisite-token-invalid")
        return raw
    except BaseException:
        for index in range(len(raw)):
            raw[index] = 0
        raise


def _production_requester(token: bytearray) -> Requester:
    def request(method: str, path: str, body: bytes | None) -> tuple[int, bytes]:
        if (
            method not in {"GET", "POST"}
            or not path.startswith(f"/{REPOSITORY_API}/actions/")
            or "#" in path
            or (method == "GET" and body is not None)
            or (method == "POST" and (body is None or len(body) < 2))
        ):
            fail("runner-prerequisite-github-request-invalid")
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        connection = http.client.HTTPSConnection(
            GITHUB_API_HOST, 443, timeout=30, context=context
        )
        try:
            headers = {
                "Accept": "application/vnd.github+json",
                "Authorization": "Bearer " + token.decode("ascii"),
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
                "User-Agent": GITHUB_USER_AGENT,
            }
            if body is not None:
                headers["Content-Type"] = "application/json"
            connection.request(method, path, body=body, headers=headers)
            headers["Authorization"] = ""
            response = connection.getresponse()
            lengths = response.headers.get_all("Content-Length") or []
            observed_type = (
                response.getheader("Content-Type", "")
                .split(";", 1)[0]
                .strip()
                .lower()
            )
            response_shape_invalid = (
                response.status != 200
                or response.reason != "OK"
                or observed_type != "application/json"
                or response.getheader("Location") is not None
                or response.getheader("Content-Encoding") is not None
                or len(lengths) > 1
                or (
                    lengths
                    and (
                        not lengths[0].isdigit()
                        or int(lengths[0]) > MAXIMUM_RESPONSE_BYTES
                    )
                )
            )
            if response_shape_invalid:
                fail("runner-prerequisite-github-response-rejected")
            raw = response.read(MAXIMUM_RESPONSE_BYTES + 1)
            if (
                not 1 <= len(raw) <= MAXIMUM_RESPONSE_BYTES
                or response.getheader("Location") is not None
                or (lengths and len(raw) != int(lengths[0]))
            ):
                fail("runner-prerequisite-github-response-invalid")
            return response.status, raw
        except (OSError, ssl.SSLError, http.client.HTTPException, TimeoutError):
            fail("runner-prerequisite-github-request-failed")
        finally:
            connection.close()

    return request


def _json_request(
    requester: Requester, method: str, path: str, body: dict[str, Any] | None = None
) -> tuple[Any, bytes]:
    status, raw = requester(method, path, None if body is None else _canonical(body))
    if status != 200 or not raw:
        fail("runner-prerequisite-github-status-invalid")
    try:
        value = reservation.materialize._strict_decode_json(
            raw, "runner-prerequisite-github-json-invalid"
        )
    except reservation.materialize.MaterializeFailure:
        fail("runner-prerequisite-github-json-invalid")
    return value, raw


def _prerequisite_job_name(
    *, runner_label: str, reservation_sha256: str
) -> str:
    return (
        f"{PREREQUISITE_JOB_KEY} | {runner_label} | "
        f"{reservation_sha256}"
    )


def _validate_run(
    run: dict[str, Any],
    payload: dict[str, Any],
    *,
    expected_run_id: str | None = None,
    terminal: bool = False,
) -> tuple[str, int]:
    run_id = _numeric(run.get("id"))
    attempt = run.get("run_attempt")
    repository_value = run.get("repository")
    head_repository = run.get("head_repository")
    created = _timestamp(run.get("created_at"), "runner-prerequisite-run-time-invalid")
    created_lower = payload.get("created_at_epoch")
    if type(created_lower) is not int:
        created_lower = 1
    created_upper = payload.get("expires_at_epoch")
    if type(created_upper) is not int:
        created_upper = payload.get("reservation_expires_at_epoch")
    if type(created_upper) is not int:
        fail("runner-prerequisite-run-time-invalid")
    if (
        run_id is None
        or (expected_run_id is not None and run_id != expected_run_id)
        or type(attempt) is not int
        or not 1 <= attempt < 1 << 31
        or run.get("event") != "workflow_dispatch"
        or run.get("head_branch") != "main"
        or run.get("head_sha") != payload["workflow_sha"]
        or run.get("path") != WORKFLOW_PATH
        or (
            not terminal
            and (
                run.get("status")
                not in {"queued", "in_progress", "waiting", "pending", "requested"}
                or run.get("conclusion") is not None
            )
        )
        or (
            terminal
            and (
                run.get("status") != "completed"
                or run.get("conclusion") not in {"cancelled", "failure"}
            )
        )
        or not isinstance(repository_value, dict)
        or _numeric(repository_value.get("id")) != REPOSITORY_ID
        or repository_value.get("full_name") != REPOSITORY
        or not isinstance(repository_value.get("owner"), dict)
        or _numeric(repository_value["owner"].get("id")) != REPOSITORY_OWNER_ID
        or not isinstance(head_repository, dict)
        or _numeric(head_repository.get("id")) != REPOSITORY_ID
        or created < created_lower - 60
        or created > created_upper
        or (
            "discovered_at_epoch" in payload
            and type(payload.get("discovered_at_epoch")) is int
            and created > payload["discovered_at_epoch"]
        )
    ):
        fail("runner-prerequisite-run-binding-invalid")
    return run_id, attempt


def _run_for_id(
    requester: Requester,
    intent: dict[str, Any],
    *,
    terminal: bool,
) -> tuple[dict[str, Any], bytes]:
    value, raw = _json_request(
        requester,
        "GET",
        f"/{REPOSITORY_API}/actions/runs/{intent['run_id']}",
    )
    if not isinstance(value, dict):
        fail("runner-prerequisite-run-invalid")
    run_id, attempt = _validate_run(
        value,
        intent,
        expected_run_id=intent["run_id"],
        terminal=terminal,
    )
    if run_id != intent["run_id"] or attempt != intent["run_attempt"]:
        fail("runner-prerequisite-run-binding-invalid")
    return value, raw


def _jobs_for_run(
    requester: Requester, run_id: str, attempt: int
) -> tuple[list[dict[str, Any]], bytes]:
    value, raw = _json_request(
        requester,
        "GET",
        f"/{REPOSITORY_API}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100",
    )
    jobs = value.get("jobs") if isinstance(value, dict) else None
    if (
        not isinstance(jobs, list)
        or value.get("total_count") != len(jobs)
        or not 1 <= len(jobs) <= 100
        or any(not isinstance(job, dict) for job in jobs)
    ):
        fail("runner-prerequisite-jobs-invalid")
    return jobs, raw


def _prerequisite_job(
    jobs: list[dict[str, Any]],
    *,
    expected_name: str,
    run_id: str,
    workflow_sha: str,
    waiting: bool,
) -> tuple[str, dict[str, Any]]:
    if any(job.get("name") == RELEASE_JOB for job in jobs) and waiting:
        fail("runner-prerequisite-release-job-already-present")
    matches = [job for job in jobs if job.get("name") == expected_name]
    if len(matches) != 1:
        fail("runner-prerequisite-job-selection-invalid")
    job = matches[0]
    job_id = _numeric(job.get("id"))
    status = job.get("status")
    if (
        job_id is None
        or job.get("head_sha") != workflow_sha
        or job.get("labels") != ["ubuntu-latest"]
        or job.get("run_url")
        != f"https://api.github.com/repos/{REPOSITORY}/actions/runs/{run_id}"
        or (waiting and (status not in {"queued", "waiting", "pending"} or job.get("conclusion") is not None))
        or (
            not waiting
            and (status != "completed" or job.get("conclusion") != "success")
        )
    ):
        fail("runner-prerequisite-job-binding-invalid")
    return job_id, job


def _bound_prerequisite_job(
    jobs: list[dict[str, Any]], intent: dict[str, Any]
) -> dict[str, Any]:
    matches = [
        job
        for job in jobs
        if job.get("name") == intent["prerequisite_job_name"]
    ]
    if len(matches) != 1:
        fail("runner-prerequisite-completion-job-invalid")
    job = matches[0]
    if (
        _numeric(job.get("id")) != intent["prerequisite_job_id"]
        or job.get("head_sha") != intent["workflow_sha"]
        or job.get("labels") != ["ubuntu-latest"]
        or job.get("run_url")
        != (
            f"https://api.github.com/repos/{REPOSITORY}/actions/runs/"
            f"{intent['run_id']}"
        )
    ):
        fail("runner-prerequisite-completion-job-invalid")
    return job


def _terminal_release_job_evidence(
    jobs: list[dict[str, Any]], intent: dict[str, Any]
) -> dict[str, Any]:
    matches = [job for job in jobs if job.get("name") == RELEASE_JOB]
    if len(matches) > 1:
        fail("runner-retirement-release-job-ambiguous")
    if not matches:
        return {
            "release_job_completed_at": None,
            "release_job_conclusion": None,
            "release_job_disposition": "absent",
            "release_job_id": None,
            "release_job_labels": [],
            "release_job_present": False,
            "release_job_run_attempt": None,
            "release_job_runner_group_id": None,
            "release_job_runner_group_name": None,
            "release_job_runner_id": None,
            "release_job_runner_name": None,
            "release_job_started_at": None,
            "release_job_steps_count": 0,
        }
    job = matches[0]
    job_id = _numeric(job.get("id"))
    labels = [
        "self-hosted",
        "propertyquarry-release-controller-v2",
        intent["runner_label"],
    ]
    if (
        job_id is None
        or job.get("status") != "completed"
        or job.get("conclusion") not in {"cancelled", "skipped"}
        or job.get("head_sha") != intent["workflow_sha"]
        or job.get("run_url")
        != f"https://api.github.com/repos/{REPOSITORY}/actions/runs/{intent['run_id']}"
        or job.get("labels") != labels
        or job.get("run_attempt") != intent["run_attempt"]
        or job.get("runner_id") not in {None, 0}
        or job.get("runner_name") not in {None, ""}
        or job.get("runner_group_id") not in {None, 0}
        or job.get("runner_group_name") not in {None, ""}
        or job.get("steps") != []
        or job.get("started_at") is not None
        or not isinstance(job.get("completed_at"), str)
    ):
        fail("runner-retirement-release-job-executed")
    _timestamp(
        job["completed_at"], "runner-retirement-release-job-time-invalid"
    )
    return {
        "release_job_completed_at": job["completed_at"],
        "release_job_conclusion": job["conclusion"],
        "release_job_disposition": "inert-terminal",
        "release_job_id": job_id,
        "release_job_labels": labels,
        "release_job_present": True,
        "release_job_run_attempt": intent["run_attempt"],
        "release_job_runner_group_id": job.get("runner_group_id"),
        "release_job_runner_group_name": job.get("runner_group_name"),
        "release_job_runner_id": job.get("runner_id"),
        "release_job_runner_name": job.get("runner_name"),
        "release_job_started_at": None,
        "release_job_steps_count": 0,
    }


def _pending_for_run(
    requester: Requester, run_id: str
) -> tuple[list[Any], bytes]:
    value, raw = _json_request(
        requester,
        "GET",
        f"/{REPOSITORY_API}/actions/runs/{run_id}/pending_deployments",
    )
    if not isinstance(value, list) or len(value) > 1:
        fail("runner-prerequisite-pending-invalid")
    return value, raw


def _pending_environment(item: Any) -> tuple[str, str]:
    environment = item.get("environment") if isinstance(item, dict) else None
    environment_id = _numeric(environment.get("id")) if isinstance(environment, dict) else None
    if (
        not isinstance(item, dict)
        or not isinstance(environment, dict)
        or environment_id is None
        or environment.get("name") != ENVIRONMENT
        or item.get("current_user_can_approve") is not True
    ):
        fail("runner-prerequisite-environment-binding-invalid")
    return environment_id, environment["name"]


def _discover_intent(
    requester: Requester,
    reservation_raw: bytes,
    payload: dict[str, Any],
    receipt_id: str,
    current: int,
) -> dict[str, Any]:
    value, runs_raw = _json_request(
        requester,
        "GET",
        (
            f"/{REPOSITORY_API}/actions/workflows/smoke-runtime.yml/runs"
            f"?event=workflow_dispatch&branch=main"
            f"&head_sha={payload['workflow_sha']}&per_page=100"
        ),
    )
    runs = value.get("workflow_runs") if isinstance(value, dict) else None
    if (
        not isinstance(runs, list)
        or type(value.get("total_count")) is not int
        or value["total_count"] != len(runs)
        or not 1 <= len(runs) <= 100
    ):
        fail("runner-prerequisite-run-index-invalid")
    candidates: list[dict[str, Any]] = []
    for raw_run in runs:
        if not isinstance(raw_run, dict):
            fail("runner-prerequisite-run-index-invalid")
        try:
            run_id, attempt = _validate_run(raw_run, payload)
            jobs, jobs_raw = _jobs_for_run(requester, run_id, attempt)
            job_id, _job = _prerequisite_job(
                jobs,
                expected_name=_prerequisite_job_name(
                    runner_label=payload["runner_label"],
                    reservation_sha256=_digest(reservation_raw),
                ),
                run_id=run_id,
                workflow_sha=payload["workflow_sha"],
                waiting=True,
            )
            pending, pending_raw = _pending_for_run(requester, run_id)
            if len(pending) != 1:
                continue
            environment_id, environment_name = _pending_environment(pending[0])
        except ApprovalFailure:
            continue
        comment = (
            "PropertyQuarry governed prerequisite approval "
            + _digest(reservation_raw)
        )
        candidates.append(
            {
                "authority_profile": "single-host-production-v2",
                "comment": comment,
                "discovered_at_epoch": current,
                "environment_id": environment_id,
                "environment_name": environment_name,
                "initial_jobs_sha256": _digest(jobs_raw),
                "initial_pending_deployments_sha256": _digest(pending_raw),
                "initial_runs_index_sha256": _digest(runs_raw),
                "prerequisite_job_id": job_id,
                "prerequisite_job_key": PREREQUISITE_JOB_KEY,
                "prerequisite_job_name": _prerequisite_job_name(
                    runner_label=payload["runner_label"],
                    reservation_sha256=_digest(reservation_raw),
                ),
                "receipt_authority_key_id": receipt_id,
                "release_job": RELEASE_JOB,
                "repository": REPOSITORY,
                "repository_id": REPOSITORY_ID,
                "repository_owner_id": REPOSITORY_OWNER_ID,
                "reservation_expires_at_epoch": payload["expires_at_epoch"],
                "reservation_sha256": _digest(reservation_raw),
                "run_attempt": attempt,
                "run_id": run_id,
                "runner_label": payload["runner_label"],
                "schema": INTENT_SCHEMA,
                "version": 3,
                "workflow_path": WORKFLOW_PATH,
                "workflow_ref": WORKFLOW_REF,
                "workflow_sha": payload["workflow_sha"],
            }
        )
    if len(candidates) != 1:
        fail("runner-prerequisite-dispatch-selection-invalid")
    return candidates[0]


def _validate_intent(
    payload: dict[str, Any],
    reservation_raw: bytes,
    reservation_payload: dict[str, Any],
    receipt_id: str,
) -> None:
    try:
        reservation._validate_prerequisite_intent_v3(
            payload,
            reservation_raw=reservation_raw,
            reservation_payload=reservation_payload,
            receipt_id=receipt_id,
        )
    except reservation.ReservationFailure:
        fail("runner-prerequisite-intent-binding-invalid")


def _approval_request(intent: dict[str, Any]) -> dict[str, Any]:
    return {
        "comment": intent["comment"],
        "environment_ids": [int(intent["environment_id"])],
        "state": "approved",
    }


def _validate_post_attempt(
    payload: dict[str, Any],
    *,
    intent_raw: bytes,
    intent: dict[str, Any],
) -> None:
    try:
        reservation._validate_prerequisite_post_attempt(
            payload, intent_raw=intent_raw, intent=intent
        )
    except reservation.ReservationFailure:
        fail("runner-prerequisite-post-attempt-binding-invalid")


def _review_history_observation(
    requester: Requester, intent: dict[str, Any]
) -> tuple[bytes, list[dict[str, Any]], list[dict[str, Any]]]:
    value, raw = _json_request(
        requester,
        "GET",
        f"/{REPOSITORY_API}/actions/runs/{intent['run_id']}/approvals",
    )
    if not isinstance(value, list):
        fail("runner-prerequisite-review-history-invalid")
    exact_matches: list[dict[str, Any]] = []
    environment_approvals: list[dict[str, Any]] = []
    for review in value:
        if not isinstance(review, dict):
            fail("runner-prerequisite-review-history-invalid")
        environments = review.get("environments")
        if review.get("state") == "approved":
            if not isinstance(environments, list):
                fail("runner-prerequisite-review-history-invalid")
            targets = 0
            for environment in environments:
                if not isinstance(environment, dict):
                    fail("runner-prerequisite-review-history-invalid")
                environment_id = _numeric(environment.get("id"))
                environment_name = environment.get("name")
                id_matches = environment_id == intent["environment_id"]
                name_matches = environment_name == ENVIRONMENT
                if id_matches != name_matches:
                    fail("runner-prerequisite-review-environment-rebound")
                if id_matches:
                    targets += 1
            if targets > 1:
                fail("runner-prerequisite-review-environment-duplicated")
            if targets == 1:
                environment_approvals.append(review)
        if (
            review.get("state") == "approved"
            and review.get("comment") == intent["comment"]
            and isinstance(environments, list)
            and len(environments) == 1
            and isinstance(environments[0], dict)
            and _numeric(environments[0].get("id")) == intent["environment_id"]
            and environments[0].get("name") == ENVIRONMENT
        ):
            exact_matches.append(review)
    return raw, exact_matches, environment_approvals


def _review_history_matches(
    requester: Requester, intent: dict[str, Any]
) -> tuple[bytes, list[dict[str, Any]]]:
    raw, exact_matches, environment_approvals = (
        _review_history_observation(requester, intent)
    )
    if len(exact_matches) > 1:
        fail("runner-prerequisite-review-duplicated")
    if len(environment_approvals) != len(exact_matches):
        fail("runner-prerequisite-review-conflict")
    return raw, exact_matches


def _approval_pre_attempt_observation(
    requester: Requester,
    intent: dict[str, Any],
    reservation_raw: bytes,
    *,
    receipt_public,
    receipt_id: str,
) -> tuple[
    bytes,
    bytes,
    bytes,
    bytes,
    list[dict[str, Any]],
]:
    _run, run_raw = _run_for_id(requester, intent, terminal=False)
    jobs, jobs_raw = _jobs_for_run(
        requester, intent["run_id"], intent["run_attempt"]
    )
    pending, pending_raw = _pending_for_run(requester, intent["run_id"])
    if len(pending) == 1:
        environment_id, environment_name = _pending_environment(pending[0])
        if (
            environment_id != intent["environment_id"]
            or environment_name != intent["environment_name"]
        ):
            fail("runner-prerequisite-pending-rebound")
    elif pending != []:
        fail("runner-prerequisite-pending-invalid")
    review_raw, exact_reviews, environment_approvals = (
        _review_history_observation(requester, intent)
    )
    if len(exact_reviews) > 1:
        fail("runner-prerequisite-review-duplicated")
    if len(environment_approvals) != len(exact_reviews):
        fail("runner-prerequisite-review-conflict")
    if exact_reviews:
        job = _bound_prerequisite_job(jobs, intent)
        if not (
            (
                job.get("status")
                in {
                    "queued",
                    "in_progress",
                    "waiting",
                    "pending",
                    "requested",
                }
                and job.get("conclusion") is None
            )
            or (
                job.get("status") == "completed"
                and job.get("conclusion") == "success"
            )
        ):
            fail("runner-prerequisite-job-binding-invalid")
    else:
        job_id, _job = _prerequisite_job(
            jobs,
            expected_name=intent["prerequisite_job_name"],
            run_id=intent["run_id"],
            workflow_sha=intent["workflow_sha"],
            waiting=True,
        )
        if job_id != intent["prerequisite_job_id"]:
            fail("runner-prerequisite-job-rebound")
        if (
            len(pending) != 1
            or _digest(pending_raw)
            != intent["initial_pending_deployments_sha256"]
        ):
            fail("runner-prerequisite-pending-rebound")
    _require_no_materialization(
        reservation_raw,
        receipt_public=receipt_public,
        receipt_id=receipt_id,
    )
    return run_raw, jobs_raw, pending_raw, review_raw, exact_reviews


def _reconcile_prerequisite_approval(
    requester: Requester,
    intent: dict[str, Any],
    *,
    current_time: Callable[[], int],
    sleeper: Callable[[float], None],
) -> tuple[bytes, bytes, bytes]:
    deadline = current_time() + MAXIMUM_COMPLETION_WAIT_SECONDS
    polls = 0
    while (
        polls < MAXIMUM_RECONCILIATION_POLLS
        and current_time() <= deadline
    ):
        polls += 1
        _run, _run_raw = _run_for_id(
            requester, intent, terminal=False
        )
        pending, pending_raw = _pending_for_run(requester, intent["run_id"])
        if len(pending) == 1:
            environment_id, environment_name = _pending_environment(pending[0])
            if (
                environment_id != intent["environment_id"]
                or environment_name != intent["environment_name"]
            ):
                fail("runner-prerequisite-pending-rebound")
        elif pending != []:
            fail("runner-prerequisite-pending-invalid")
        review_raw, reviews = _review_history_matches(requester, intent)
        jobs, jobs_raw = _jobs_for_run(
            requester, intent["run_id"], intent["run_attempt"]
        )
        job = _bound_prerequisite_job(jobs, intent)
        if job.get("status") == "completed":
            job_id, job = _prerequisite_job(
                jobs,
                expected_name=intent["prerequisite_job_name"],
                run_id=intent["run_id"],
                workflow_sha=intent["workflow_sha"],
                waiting=False,
            )
            if job_id != intent["prerequisite_job_id"]:
                fail("runner-prerequisite-completion-job-invalid")
            if reviews and pending == []:
                return pending_raw, review_raw, jobs_raw
        elif (
            job.get("status")
            not in {"queued", "in_progress", "waiting", "pending", "requested"}
            or job.get("conclusion") is not None
        ):
            fail("runner-prerequisite-completion-failed")
        sleeper(2.0)
    fail("runner-prerequisite-completion-timeout")


def _validate_approval(
    payload: dict[str, Any], *, intent_raw: bytes, intent: dict[str, Any]
) -> None:
    try:
        reservation._validate_prerequisite_approval_v3(
            payload, intent_raw=intent_raw, intent=intent
        )
    except reservation.ReservationFailure:
        fail("runner-prerequisite-approval-binding-invalid")


def _require_no_materialization(
    reservation_raw: bytes,
    *,
    receipt_public,
    receipt_id: str,
) -> None:
    terminal_root = reservation.RESERVATION_TERMINAL_ROOT
    if not terminal_root.exists():
        return
    reservation._exact_metadata(terminal_root, kind="directory", mode=0o700)
    identity = _digest(reservation_raw).removeprefix("sha256:")
    for kind in ("claim", "bound"):
        for suffix in (f".{kind}.v2", f".{kind}.v2.pending"):
            path = terminal_root / (identity + suffix)
            if not path.exists():
                continue
            try:
                raw = reservation.materialize._read_runner_terminal_file(path)
                if kind == "claim":
                    payload = (
                        reservation.materialize._validate_runner_materialization_claim(
                            raw,
                            receipt_public=receipt_public,
                            receipt_id=receipt_id,
                        )
                    )
                else:
                    payload = reservation.materialize._validate_runner_materialization_binding(
                        raw,
                        receipt_public=receipt_public,
                        receipt_id=receipt_id,
                    )
            except reservation.materialize.MaterializeFailure:
                fail("runner-governed-materialization-record-invalid")
            if payload.get("reservation_sha256") != _digest(reservation_raw):
                fail("runner-governed-materialization-record-invalid")
            fail("runner-governed-materialization-present")


def _validate_retirement_terminal(
    payload: dict[str, Any],
    *,
    intent_raw: bytes,
    intent: dict[str, Any],
    post_attempt_raw: bytes | None,
) -> None:
    try:
        reservation._validate_retirement_terminal(
            payload,
            intent_raw=intent_raw,
            intent=intent,
            post_attempt_raw=post_attempt_raw,
        )
    except reservation.ReservationFailure:
        fail("runner-retirement-terminal-binding-invalid")


def _retirement_observation(
    requester: Requester,
    intent: dict[str, Any],
) -> dict[str, Any]:
    run, run_raw = _run_for_id(requester, intent, terminal=True)
    jobs, jobs_raw = _jobs_for_run(
        requester, intent["run_id"], intent["run_attempt"]
    )
    prerequisite = _bound_prerequisite_job(jobs, intent)
    if (
        prerequisite.get("status") != "completed"
        or prerequisite.get("conclusion")
        not in {"cancelled", "failure", "success"}
    ):
        fail("runner-retirement-prerequisite-state-invalid")
    release = _terminal_release_job_evidence(jobs, intent)
    pending, pending_raw = _pending_for_run(requester, intent["run_id"])
    if pending != []:
        fail("runner-retirement-pending-present")
    review_raw, exact_reviews, environment_approvals = (
        _review_history_observation(requester, intent)
    )
    if len(environment_approvals) > 100:
        fail("runner-retirement-review-history-oversized")
    exact_set_raw = _canonical(exact_reviews)
    environment_set_raw = _canonical(environment_approvals)

    # Re-read every mutable observation, with run and jobs last, before
    # signing the GET-only adoption evidence.
    verified_pending, verified_pending_raw = _pending_for_run(
        requester, intent["run_id"]
    )
    (
        verified_review_raw,
        verified_exact_reviews,
        verified_environment_approvals,
    ) = _review_history_observation(requester, intent)
    verified_run, verified_run_raw = _run_for_id(
        requester, intent, terminal=True
    )
    verified_jobs, verified_jobs_raw = _jobs_for_run(
        requester, intent["run_id"], intent["run_attempt"]
    )
    verified_prerequisite = _bound_prerequisite_job(
        verified_jobs, intent
    )
    verified_release = _terminal_release_job_evidence(
        verified_jobs, intent
    )
    if (
        verified_pending != []
        or pending_raw != verified_pending_raw
        or review_raw != verified_review_raw
        or exact_reviews != verified_exact_reviews
        or environment_approvals != verified_environment_approvals
        or run_raw != verified_run_raw
        or jobs_raw != verified_jobs_raw
        or run != verified_run
        or prerequisite != verified_prerequisite
        or release != verified_release
    ):
        fail("runner-retirement-observation-drift")
    return {
        "exact_review_count": len(exact_reviews),
        "exact_review_set_sha256": _digest(exact_set_raw),
        "jobs_raw": verified_jobs_raw,
        "pending_raw": verified_pending_raw,
        "prerequisite_conclusion": prerequisite["conclusion"],
        "release": release,
        "review_count": len(environment_approvals),
        "review_raw": verified_review_raw,
        "review_set_sha256": _digest(environment_set_raw),
        "run_conclusion": run["conclusion"],
        "run_raw": verified_run_raw,
    }


def _result(payload: dict[str, Any], approval_raw: bytes, disposition: str) -> dict[str, Any]:
    return {
        "approval_sha256": _digest(approval_raw),
        "disposition": disposition,
        "prerequisite_job_id": payload["prerequisite_job_id"],
        "reservation_sha256": payload["reservation_sha256"],
        "run_attempt": payload["run_attempt"],
        "run_id": payload["run_id"],
        "runner_label": payload["runner_label"],
        "schema": RESULT_SCHEMA,
        "version": 3,
    }


def _retirement_result(
    payload: dict[str, Any], terminal_raw: bytes, disposition: str
) -> dict[str, Any]:
    return {
        "disposition": disposition,
        "reservation_sha256": payload["reservation_sha256"],
        "retirement_terminal_sha256": _digest(terminal_raw),
        "run_attempt": payload["run_attempt"],
        "run_conclusion": payload["run_conclusion"],
        "run_id": payload["run_id"],
        "runner_label": payload["runner_label"],
        "schema": RETIREMENT_RESULT_SCHEMA,
        "version": 2,
    }


def approve(
    *,
    now: int | None = None,
    requester: Requester | None = None,
    current_time: Callable[[], int] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    current = int(time.time()) if now is None else now
    if (
        type(current) is not int
        or current < 1
        or os.geteuid() != 1000
        or os.getegid() != 1000
    ):
        fail("runner-prerequisite-process-invalid")
    clock = current_time or (lambda: int(time.time()))
    token: bytearray | None = None

    def require_requester() -> Requester:
        nonlocal requester, token
        if requester is None:
            token = _read_admin_token()
            requester = _production_requester(token)
        return requester

    receipt_private, receipt_id = reservation._load_receipt_authority()
    receipt_public = receipt_private.public_key()
    lock = reservation._acquire_lock()
    try:
        if not reservation.RESERVATION_ROOT.exists():
            fail("runner-prerequisite-active-reservation-missing")
        reservation_raw = reservation._read_wire()
        reservation_payload = reservation._validate_wire(
            reservation_raw,
            workflow_sha=None,
            receipt_public=receipt_public,
            receipt_id=receipt_id,
        )
        if current < reservation_payload["created_at_epoch"]:
            fail("runner-prerequisite-reservation-time-invalid")
        _ensure_approval_root()
        intent_path, approval_path = _record_paths(reservation_raw)
        try:
            governed_state = reservation._active_governed_state(
                reservation_raw,
                reservation_payload,
                receipt_public=receipt_public,
                receipt_id=receipt_id,
            )
        except reservation.ReservationFailure:
            fail("runner-prerequisite-governed-state-invalid")
        if governed_state in {
            "legacy-v2-discovered",
            "legacy-v2-approved",
        }:
            fail("runner-prerequisite-legacy-v2-nondispatchable")
        if governed_state == "retirement-terminal":
            fail("runner-prerequisite-retirement-terminal-present")
        attempt_path = _post_attempt_path(reservation_raw)
        if approval_path.exists():
            if not intent_path.exists():
                fail("runner-prerequisite-approved-without-intent")
            intent_raw = _read_record(intent_path)
            intent = _verify_wire(
                intent_raw,
                public=receipt_public,
                key_id=receipt_id,
                schema=INTENT_SCHEMA,
                domain=INTENT_SIGNATURE_DOMAIN,
            )
            _validate_intent(intent, reservation_raw, reservation_payload, receipt_id)
            approval_raw = _read_record(approval_path)
            approval = _verify_wire(
                approval_raw,
                public=receipt_public,
                key_id=receipt_id,
                schema=APPROVAL_SCHEMA,
                domain=APPROVAL_SIGNATURE_DOMAIN,
            )
            _validate_approval(approval, intent_raw=intent_raw, intent=intent)
            if not attempt_path.exists():
                fail("runner-prerequisite-approved-without-post-attempt")
            attempt_raw = _read_record(attempt_path)
            attempt = _verify_wire(
                attempt_raw,
                public=receipt_public,
                key_id=receipt_id,
                schema=POST_ATTEMPT_SCHEMA,
                domain=POST_ATTEMPT_SIGNATURE_DOMAIN,
            )
            _validate_post_attempt(
                attempt,
                intent_raw=intent_raw,
                intent=intent,
            )
            if (
                approval["approved_at_epoch"]
                != attempt["attempted_at_epoch"]
            ):
                fail("runner-prerequisite-approval-attempt-time-mismatch")
            return _result(approval, approval_raw, "already-approved")

        if intent_path.exists():
            intent_raw = _read_record(intent_path)
            intent = _verify_wire(
                intent_raw,
                public=receipt_public,
                key_id=receipt_id,
                schema=INTENT_SCHEMA,
                domain=INTENT_SIGNATURE_DOMAIN,
            )
            _validate_intent(intent, reservation_raw, reservation_payload, receipt_id)
        else:
            if current > reservation_payload["expires_at_epoch"]:
                fail("runner-prerequisite-reservation-time-invalid")
            intent = _discover_intent(
                require_requester(),
                reservation_raw,
                reservation_payload,
                receipt_id,
                current,
            )
            intent_raw = _wire(
                intent, receipt_private, receipt_id, INTENT_SIGNATURE_DOMAIN
            )
            _publish_record(intent_path, intent_raw)
            _checkpoint("after-runner-prerequisite-intent")

        requester = require_requester()
        review_before_raw, reviews_before = _review_history_matches(
            requester, intent
        )
        if reviews_before and not attempt_path.exists():
            fail("runner-prerequisite-unarmed-review-present")
        approval_response_digest: str | None = None
        api_disposition = "post-approved-recovered"
        attempt: dict[str, Any] | None = None
        if reviews_before and attempt_path.exists():
            attempt_raw = _read_record(attempt_path)
            attempt = _verify_wire(
                attempt_raw,
                public=receipt_public,
                key_id=receipt_id,
                schema=POST_ATTEMPT_SCHEMA,
                domain=POST_ATTEMPT_SIGNATURE_DOMAIN,
            )
            _validate_post_attempt(
                attempt,
                intent_raw=intent_raw,
                intent=intent,
            )
        if not reviews_before:
            post_now = False
            if attempt_path.exists():
                attempt_raw = _read_record(attempt_path)
                attempt = _verify_wire(
                    attempt_raw,
                    public=receipt_public,
                    key_id=receipt_id,
                    schema=POST_ATTEMPT_SCHEMA,
                    domain=POST_ATTEMPT_SIGNATURE_DOMAIN,
                )
                _validate_post_attempt(
                    attempt,
                    intent_raw=intent_raw,
                    intent=intent,
                )
            else:
                if current > intent["reservation_expires_at_epoch"]:
                    fail("runner-prerequisite-reservation-time-invalid")
                _checkpoint("before-runner-prerequisite-post-attempt-gate")
                (
                    pre_post_run_raw,
                    pre_post_jobs_raw,
                    pending_raw,
                    pre_post_review_raw,
                    pre_post_exact_reviews,
                ) = _approval_pre_attempt_observation(
                    requester,
                    intent,
                    reservation_raw,
                    receipt_public=receipt_public,
                    receipt_id=receipt_id,
                )
                if pre_post_exact_reviews:
                    fail("runner-prerequisite-unarmed-review-present")
                attempted_at = clock()
                if attempted_at > intent["reservation_expires_at_epoch"]:
                    fail("runner-prerequisite-reservation-time-invalid")
                attempt = {
                    "attempted_at_epoch": attempted_at,
                    "authority_profile": "single-host-production-v2",
                    "comment": intent["comment"],
                    "environment_id": intent["environment_id"],
                    "environment_name": intent["environment_name"],
                    "github_api_path": (
                        f"/{REPOSITORY_API}/actions/runs/{intent['run_id']}/"
                        "pending_deployments"
                    ),
                    "http_method": "POST",
                    "intent_sha256": _digest(intent_raw),
                    "pre_post_jobs_sha256": _digest(pre_post_jobs_raw),
                    "pre_post_pending_deployments_count": 1,
                    "pre_post_pending_deployments_sha256": _digest(
                        pending_raw
                    ),
                    "pre_post_release_job_present": False,
                    "pre_post_review_history_sha256": _digest(
                        pre_post_review_raw
                    ),
                    "pre_post_review_match_count": 0,
                    "pre_post_review_scope": (
                        "any-approved-target-environment"
                    ),
                    "pre_post_run_sha256": _digest(pre_post_run_raw),
                    "prerequisite_job_id": intent["prerequisite_job_id"],
                    "prerequisite_job_key": intent[
                        "prerequisite_job_key"
                    ],
                    "prerequisite_job_name": intent[
                        "prerequisite_job_name"
                    ],
                    "receipt_authority_key_id": receipt_id,
                    "repository": REPOSITORY,
                    "repository_id": REPOSITORY_ID,
                    "repository_owner_id": REPOSITORY_OWNER_ID,
                    "request_sha256": _digest(
                        _canonical(_approval_request(intent))
                    ),
                    "reservation_expires_at_epoch": intent[
                        "reservation_expires_at_epoch"
                    ],
                    "reservation_sha256": intent["reservation_sha256"],
                    "run_attempt": intent["run_attempt"],
                    "run_id": intent["run_id"],
                    "runner_label": intent["runner_label"],
                    "schema": POST_ATTEMPT_SCHEMA,
                    "version": 3,
                    "workflow_path": WORKFLOW_PATH,
                    "workflow_ref": WORKFLOW_REF,
                    "workflow_sha": intent["workflow_sha"],
                }
                _validate_post_attempt(
                    attempt,
                    intent_raw=intent_raw,
                    intent=intent,
                )
                attempt_raw = _wire(
                    attempt,
                    receipt_private,
                    receipt_id,
                    POST_ATTEMPT_SIGNATURE_DOMAIN,
                )
                _publish_record(attempt_path, attempt_raw)
                _checkpoint("after-runner-prerequisite-post-attempt")
                post_now = True
            if post_now:
                # GitHub exposes this POST only at run scope: there is no
                # attempt selector or ETag precondition. The release protocol
                # therefore requires quiescent, trusted Actions write/rerun
                # authority from this final observation through this POST.
                # Reconciliation still fails closed if the attempt drifts.
                response, response_raw = _json_request(
                    requester,
                    "POST",
                    (
                        f"/{REPOSITORY_API}/actions/runs/{intent['run_id']}/"
                        "pending_deployments"
                    ),
                    _approval_request(intent),
                )
                if not isinstance(response, list) or len(response) < 1:
                    fail("runner-prerequisite-approval-response-invalid")
                approval_response_digest = _digest(response_raw)
                api_disposition = "approved"
                _checkpoint("after-runner-prerequisite-approval-post")
        post_pending_raw, review_raw, completed_jobs_raw = (
            _reconcile_prerequisite_approval(
                requester, intent, current_time=clock, sleeper=sleeper
            )
        )
        approved_at = (
            attempt["attempted_at_epoch"]
            if attempt is not None
            else min(clock(), intent["reservation_expires_at_epoch"])
        )
        approval = {
            "approval_api_disposition": api_disposition,
            "approval_response_sha256": approval_response_digest,
            "approved_at_epoch": approved_at,
            "completed_jobs_sha256": _digest(completed_jobs_raw),
            "environment_id": intent["environment_id"],
            "environment_name": intent["environment_name"],
            "intent_sha256": _digest(intent_raw),
            "post_pending_deployments_sha256": _digest(post_pending_raw),
            "prerequisite_conclusion": "success",
            "prerequisite_job_id": intent["prerequisite_job_id"],
            "prerequisite_job_key": intent["prerequisite_job_key"],
            "prerequisite_job_name": intent["prerequisite_job_name"],
            "receipt_authority_key_id": receipt_id,
            "release_job": RELEASE_JOB,
            "repository": REPOSITORY,
            "repository_id": REPOSITORY_ID,
            "repository_owner_id": REPOSITORY_OWNER_ID,
            "reservation_expires_at_epoch": intent["reservation_expires_at_epoch"],
            "reservation_sha256": intent["reservation_sha256"],
            "review_history_sha256": _digest(review_raw),
            "run_attempt": intent["run_attempt"],
            "run_id": intent["run_id"],
            "runner_label": intent["runner_label"],
            "schema": APPROVAL_SCHEMA,
            "version": 3,
            "workflow_path": WORKFLOW_PATH,
            "workflow_ref": WORKFLOW_REF,
            "workflow_sha": intent["workflow_sha"],
        }
        _validate_approval(approval, intent_raw=intent_raw, intent=intent)
        approval_raw = _wire(
            approval, receipt_private, receipt_id, APPROVAL_SIGNATURE_DOMAIN
        )
        _publish_record(approval_path, approval_raw)
        return _result(approval, approval_raw, api_disposition)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)
        if token is not None:
            for index in range(len(token)):
                token[index] = 0


def retire_terminal(
    *,
    now: int | None = None,
    requester: Requester | None = None,
    current_time: Callable[[], int] | None = None,
) -> dict[str, Any]:
    current = int(time.time()) if now is None else now
    if (
        type(current) is not int
        or current < 1
        or os.geteuid() != 1000
        or os.getegid() != 1000
    ):
        fail("runner-retirement-process-invalid")
    clock = current_time or (lambda: int(time.time()))
    token: bytearray | None = None
    receipt_private, receipt_id = reservation._load_receipt_authority()
    receipt_public = receipt_private.public_key()
    lock = reservation._acquire_lock()
    try:
        if not reservation.RESERVATION_ROOT.exists():
            fail("runner-retirement-active-reservation-missing")
        reservation_raw = reservation._read_wire()
        reservation_payload = reservation._validate_wire(
            reservation_raw,
            workflow_sha=None,
            receipt_public=receipt_public,
            receipt_id=receipt_id,
        )
        if current < reservation_payload["created_at_epoch"]:
            fail("runner-retirement-reservation-time-invalid")
        _ensure_approval_root()
        intent_path, approval_path = _record_paths(reservation_raw)
        legacy_intent_path, legacy_approval_path = _legacy_record_paths(
            reservation_raw
        )
        if intent_path.exists() and legacy_intent_path.exists():
            fail("runner-retirement-prerequisite-generation-conflict")
        if not intent_path.exists() and not legacy_intent_path.exists():
            fail("runner-retirement-prerequisite-intent-missing")
        intent_legacy = legacy_intent_path.exists()
        if intent_legacy:
            intent_path = legacy_intent_path
        intent_raw = _read_record(intent_path)
        intent = _verify_wire(
            intent_raw,
            public=receipt_public,
            key_id=receipt_id,
            schema=(
                LEGACY_INTENT_SCHEMA if intent_legacy else INTENT_SCHEMA
            ),
            domain=(
                LEGACY_INTENT_SIGNATURE_DOMAIN
                if intent_legacy
                else INTENT_SIGNATURE_DOMAIN
            ),
        )
        try:
            validator = (
                reservation._validate_prerequisite_intent
                if intent_legacy
                else reservation._validate_prerequisite_intent_v3
            )
            validator(
                intent,
                reservation_raw=reservation_raw,
                reservation_payload=reservation_payload,
                receipt_id=receipt_id,
            )
        except reservation.ReservationFailure:
            fail("runner-retirement-prerequisite-intent-invalid")
        if approval_path.exists() or legacy_approval_path.exists():
            fail("runner-retirement-prerequisite-approval-present")
        _require_no_materialization(
            reservation_raw,
            receipt_public=receipt_public,
            receipt_id=receipt_id,
        )
        post_attempt_path = _post_attempt_path(reservation_raw)
        post_attempt_raw: bytes | None = None
        if post_attempt_path.exists():
            if intent_legacy:
                fail("runner-retirement-post-attempt-generation-mismatch")
            post_attempt_raw = _read_record(post_attempt_path)
            post_attempt = _verify_wire(
                post_attempt_raw,
                public=receipt_public,
                key_id=receipt_id,
                schema=POST_ATTEMPT_SCHEMA,
                domain=POST_ATTEMPT_SIGNATURE_DOMAIN,
            )
            try:
                reservation._validate_prerequisite_post_attempt(
                    post_attempt,
                    intent_raw=intent_raw,
                    intent=intent,
                )
            except reservation.ReservationFailure:
                fail("runner-retirement-post-attempt-invalid")
        terminal_path = _retirement_terminal_path(reservation_raw)
        if terminal_path.exists():
            terminal_raw = _read_record(terminal_path)
            terminal = _verify_wire(
                terminal_raw,
                public=receipt_public,
                key_id=receipt_id,
                schema=RETIREMENT_TERMINAL_SCHEMA,
                domain=RETIREMENT_TERMINAL_SIGNATURE_DOMAIN,
            )
            _validate_retirement_terminal(
                terminal,
                intent_raw=intent_raw,
                intent=intent,
                post_attempt_raw=post_attempt_raw,
            )
            return _retirement_result(
                terminal, terminal_raw, "already-terminal-adopted"
            )
        if requester is None:
            token = _read_admin_token()
            requester = _production_requester(token)
        observation = _retirement_observation(requester, intent)
        _require_no_materialization(
            reservation_raw,
            receipt_public=receipt_public,
            receipt_id=receipt_id,
        )
        adopted_at = clock()
        terminal = {
            "adopted_at_epoch": adopted_at,
            "adoption_disposition": "get-only-terminal-adoption",
            "approval_post_attempt_present": post_attempt_raw is not None,
            "approval_post_attempt_sha256": (
                _digest(post_attempt_raw)
                if post_attempt_raw is not None
                else None
            ),
            "authority_profile": "single-host-production-v2",
            "environment_id": intent["environment_id"],
            "environment_name": intent["environment_name"],
            "final_exact_review_match_count": observation[
                "exact_review_count"
            ],
            "final_exact_review_set_sha256": observation[
                "exact_review_set_sha256"
            ],
            "final_jobs_sha256": _digest(observation["jobs_raw"]),
            "final_pending_deployments_count": 0,
            "final_pending_deployments_sha256": _digest(
                observation["pending_raw"]
            ),
            "final_review_history_sha256": _digest(
                observation["review_raw"]
            ),
            "final_review_match_count": observation["review_count"],
            "final_review_scope": (
                "any-approved-target-environment-complete-set"
            ),
            "final_review_set_sha256": observation[
                "review_set_sha256"
            ],
            "final_run_sha256": _digest(observation["run_raw"]),
            "prerequisite_conclusion": observation[
                "prerequisite_conclusion"
            ],
            "prerequisite_intent_sha256": _digest(intent_raw),
            "prerequisite_job_id": intent["prerequisite_job_id"],
            "prerequisite_job_key": PREREQUISITE_JOB_KEY,
            "prerequisite_job_name": intent["prerequisite_job_name"],
            "receipt_authority_key_id": receipt_id,
            "release_job": RELEASE_JOB,
            **observation["release"],
            "repository": REPOSITORY,
            "repository_id": REPOSITORY_ID,
            "repository_owner_id": REPOSITORY_OWNER_ID,
            "reservation_expires_at_epoch": intent[
                "reservation_expires_at_epoch"
            ],
            "reservation_sha256": intent["reservation_sha256"],
            "run_attempt": intent["run_attempt"],
            "run_conclusion": observation["run_conclusion"],
            "run_id": intent["run_id"],
            "runner_label": intent["runner_label"],
            "schema": RETIREMENT_TERMINAL_SCHEMA,
            "terminal_jobs_verification_sha256": _digest(
                observation["jobs_raw"]
            ),
            "terminal_run_verification_sha256": _digest(
                observation["run_raw"]
            ),
            "version": 2,
            "workflow_path": WORKFLOW_PATH,
            "workflow_ref": WORKFLOW_REF,
            "workflow_sha": intent["workflow_sha"],
        }
        _validate_retirement_terminal(
            terminal,
            intent_raw=intent_raw,
            intent=intent,
            post_attempt_raw=post_attempt_raw,
        )
        terminal_raw = _wire(
            terminal,
            receipt_private,
            receipt_id,
            RETIREMENT_TERMINAL_SIGNATURE_DOMAIN,
        )
        _checkpoint("before-runner-retirement-terminal-publish")
        _publish_record(terminal_path, terminal_raw)
        return _retirement_result(
            terminal, terminal_raw, "terminal-adopted-get-only"
        )
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)
        if token is not None:
            for index in range(len(token)):
                token[index] = 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("command", choices=("approve", "retire-terminal"))
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "approve":
            result = approve()
        else:
            result = retire_terminal()
        if result is None:  # pragma: no cover
            fail("runner-prerequisite-command-invalid")
        sys.stdout.buffer.write(_canonical(result) + b"\n")
        return 0
    except ApprovalFailure as error:
        sys.stderr.write(f"propertyquarry-runner-prerequisite-rejected:{error}\n")
        return 50
    except KeyboardInterrupt:
        sys.stderr.write("propertyquarry-runner-prerequisite-rejected:interrupted\n")
        return 50
    except Exception:
        sys.stderr.write("propertyquarry-runner-prerequisite-rejected:internal-failure\n")
        return 50


if __name__ == "__main__":
    raise SystemExit(main())
