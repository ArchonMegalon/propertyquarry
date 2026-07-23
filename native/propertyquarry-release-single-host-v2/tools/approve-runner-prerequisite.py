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
INTENT_SCHEMA = (
    "propertyquarry.release-control.single-host-runner-prerequisite-intent.v2"
)
APPROVAL_SCHEMA = (
    "propertyquarry.release-control.single-host-runner-prerequisite-approval.v2"
)
RESULT_SCHEMA = (
    "propertyquarry.release-control.single-host-runner-prerequisite-result.v2"
)
INTENT_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-runner-prerequisite-intent-signature.v2\0"
)
APPROVAL_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-runner-prerequisite-approval-signature.v2\0"
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
PREREQUISITE_JOB = "propertyquarry-protected-dispatch-inputs"
RELEASE_JOB = "propertyquarry-release-v2"
MAXIMUM_RESPONSE_BYTES = 4 * 1024 * 1024
MAXIMUM_TOKEN_BYTES = 2048
MAXIMUM_COMPLETION_WAIT_SECONDS = 900
NUMERIC_ID = re.compile(r"^[1-9][0-9]{0,19}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
STAGE_NAME = re.compile(r"^\.runner-prerequisite-[0-9a-f]{64}\.tmp$")
RECORD_NAME = re.compile(
    r"^[0-9a-f]{64}\.(?:intent|approved)\.v2\.json$"
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
        or payload.get("version") != 2
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
        APPROVAL_ROOT / f"{identity}.intent.v2.json",
        APPROVAL_ROOT / f"{identity}.approved.v2.json",
    )


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
        for index in range(len(raw)):
            raw[index] = 0
        fail("runner-prerequisite-token-invalid")
    return raw


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
            if (
                response.status != 200
                or response.reason != "OK"
                or response.getheader("Location") is not None
                or response.getheader("Content-Encoding") is not None
                or observed_type != "application/json"
                or len(lengths) > 1
                or (
                    lengths
                    and (
                        not lengths[0].isdigit()
                        or int(lengths[0]) > MAXIMUM_RESPONSE_BYTES
                    )
                )
            ):
                fail("runner-prerequisite-github-response-rejected")
            raw = response.read(MAXIMUM_RESPONSE_BYTES + 1)
            if (
                not 1 <= len(raw) <= MAXIMUM_RESPONSE_BYTES
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


def _validate_run(
    run: dict[str, Any], payload: dict[str, Any], *, expected_run_id: str | None = None
) -> tuple[str, int]:
    run_id = _numeric(run.get("id"))
    attempt = run.get("run_attempt")
    repository_value = run.get("repository")
    head_repository = run.get("head_repository")
    created = _timestamp(run.get("created_at"), "runner-prerequisite-run-time-invalid")
    if (
        run_id is None
        or (expected_run_id is not None and run_id != expected_run_id)
        or type(attempt) is not int
        or not 1 <= attempt < 1 << 31
        or run.get("event") != "workflow_dispatch"
        or run.get("head_branch") != "main"
        or run.get("head_sha") != payload["workflow_sha"]
        or run.get("path") != WORKFLOW_PATH
        or run.get("status")
        not in {"queued", "in_progress", "waiting", "pending", "requested"}
        or run.get("conclusion") is not None
        or not isinstance(repository_value, dict)
        or _numeric(repository_value.get("id")) != REPOSITORY_ID
        or repository_value.get("full_name") != REPOSITORY
        or not isinstance(repository_value.get("owner"), dict)
        or _numeric(repository_value["owner"].get("id")) != REPOSITORY_OWNER_ID
        or not isinstance(head_repository, dict)
        or _numeric(head_repository.get("id")) != REPOSITORY_ID
        or created < payload["created_at_epoch"] - 60
        or created > payload["expires_at_epoch"]
    ):
        fail("runner-prerequisite-run-binding-invalid")
    return run_id, attempt


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
    jobs: list[dict[str, Any]], *, run_id: str, workflow_sha: str, waiting: bool
) -> tuple[str, dict[str, Any]]:
    if any(job.get("name") == RELEASE_JOB for job in jobs) and waiting:
        fail("runner-prerequisite-release-job-already-present")
    matches = [job for job in jobs if job.get("name") == PREREQUISITE_JOB]
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
        f"/{REPOSITORY_API}/actions/workflows/smoke-runtime.yml/runs?event=workflow_dispatch&branch=main&per_page=100",
    )
    runs = value.get("workflow_runs") if isinstance(value, dict) else None
    if (
        not isinstance(runs, list)
        or type(value.get("total_count")) is not int
        or value["total_count"] < len(runs)
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
                jobs, run_id=run_id, workflow_sha=payload["workflow_sha"], waiting=True
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
                "prerequisite_job_name": PREREQUISITE_JOB,
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
                "version": 2,
                "workflow_path": WORKFLOW_PATH,
                "workflow_ref": WORKFLOW_REF,
                "workflow_sha": payload["workflow_sha"],
            }
        )
    if len(candidates) != 1:
        fail("runner-prerequisite-dispatch-selection-invalid")
    return candidates[0]


def _validate_intent(
    payload: dict[str, Any], reservation_raw: bytes, reservation_payload: dict[str, Any], receipt_id: str
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
    if (
        set(payload) != expected_keys
        or payload.get("schema") != INTENT_SCHEMA
        or payload.get("version") != 2
        or payload.get("authority_profile") != "single-host-production-v2"
        or payload.get("repository") != REPOSITORY
        or payload.get("repository_id") != REPOSITORY_ID
        or payload.get("repository_owner_id") != REPOSITORY_OWNER_ID
        or payload.get("workflow_path") != WORKFLOW_PATH
        or payload.get("workflow_ref") != WORKFLOW_REF
        or payload.get("workflow_sha") != reservation_payload["workflow_sha"]
        or payload.get("receipt_authority_key_id") != receipt_id
        or payload.get("reservation_sha256") != _digest(reservation_raw)
        or payload.get("reservation_expires_at_epoch")
        != reservation_payload["expires_at_epoch"]
        or payload.get("runner_label") != reservation_payload["runner_label"]
        or payload.get("environment_name") != ENVIRONMENT
        or _numeric(payload.get("environment_id")) is None
        or payload.get("prerequisite_job_name") != PREREQUISITE_JOB
        or _numeric(payload.get("prerequisite_job_id")) is None
        or payload.get("release_job") != RELEASE_JOB
        or _numeric(payload.get("run_id")) is None
        or type(payload.get("run_attempt")) is not int
        or not 1 <= payload["run_attempt"] < 1 << 31
        or type(payload.get("discovered_at_epoch")) is not int
        or not reservation_payload["created_at_epoch"] <= payload["discovered_at_epoch"] <= reservation_payload["expires_at_epoch"]
        or payload.get("comment")
        != "PropertyQuarry governed prerequisite approval " + _digest(reservation_raw)
        or any(
            not isinstance(payload.get(field), str)
            or DIGEST.fullmatch(payload[field]) is None
            for field in (
                "initial_jobs_sha256",
                "initial_pending_deployments_sha256",
                "initial_runs_index_sha256",
            )
        )
    ):
        fail("runner-prerequisite-intent-binding-invalid")


def _review_history(
    requester: Requester, intent: dict[str, Any]
) -> tuple[bytes, dict[str, Any]]:
    value, raw = _json_request(
        requester,
        "GET",
        f"/{REPOSITORY_API}/actions/runs/{intent['run_id']}/approvals",
    )
    if not isinstance(value, list):
        fail("runner-prerequisite-review-history-invalid")
    matches: list[dict[str, Any]] = []
    for review in value:
        if not isinstance(review, dict):
            fail("runner-prerequisite-review-history-invalid")
        environments = review.get("environments")
        if (
            review.get("state") == "approved"
            and review.get("comment") == intent["comment"]
            and isinstance(environments, list)
            and len(environments) == 1
            and isinstance(environments[0], dict)
            and _numeric(environments[0].get("id")) == intent["environment_id"]
            and environments[0].get("name") == ENVIRONMENT
        ):
            matches.append(review)
    if len(matches) != 1:
        fail("runner-prerequisite-review-unconfirmed")
    return raw, matches[0]


def _wait_prerequisite_success(
    requester: Requester,
    intent: dict[str, Any],
    *,
    current_time: Callable[[], int],
    sleeper: Callable[[float], None],
) -> tuple[bytes, dict[str, Any]]:
    deadline = min(
        intent["reservation_expires_at_epoch"],
        current_time() + MAXIMUM_COMPLETION_WAIT_SECONDS,
    )
    while current_time() <= deadline:
        jobs, raw = _jobs_for_run(
            requester, intent["run_id"], intent["run_attempt"]
        )
        matches = [job for job in jobs if job.get("name") == PREREQUISITE_JOB]
        if len(matches) != 1 or _numeric(matches[0].get("id")) != intent["prerequisite_job_id"]:
            fail("runner-prerequisite-completion-job-invalid")
        if matches[0].get("status") == "completed":
            job_id, job = _prerequisite_job(
                jobs,
                run_id=intent["run_id"],
                workflow_sha=intent["workflow_sha"],
                waiting=False,
            )
            if job_id != intent["prerequisite_job_id"]:
                fail("runner-prerequisite-completion-job-invalid")
            return raw, job
        if matches[0].get("status") not in {"queued", "in_progress", "waiting", "pending"} or matches[0].get("conclusion") is not None:
            fail("runner-prerequisite-completion-failed")
        sleeper(2.0)
    fail("runner-prerequisite-completion-timeout")


def _validate_approval(
    payload: dict[str, Any], *, intent_raw: bytes, intent: dict[str, Any]
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
    if (
        set(payload) != expected_keys
        or payload.get("schema") != APPROVAL_SCHEMA
        or payload.get("version") != 2
        or payload.get("intent_sha256") != _digest(intent_raw)
        or payload.get("reservation_sha256") != intent["reservation_sha256"]
        or payload.get("runner_label") != intent["runner_label"]
        or payload.get("run_id") != intent["run_id"]
        or payload.get("run_attempt") != intent["run_attempt"]
        or payload.get("prerequisite_job_id") != intent["prerequisite_job_id"]
        or payload.get("prerequisite_job_name") != PREREQUISITE_JOB
        or payload.get("prerequisite_conclusion") != "success"
        or payload.get("environment_id") != intent["environment_id"]
        or payload.get("environment_name") != ENVIRONMENT
        or payload.get("receipt_authority_key_id")
        != intent["receipt_authority_key_id"]
        or payload.get("repository") != REPOSITORY
        or payload.get("repository_id") != REPOSITORY_ID
        or payload.get("repository_owner_id") != REPOSITORY_OWNER_ID
        or payload.get("workflow_path") != WORKFLOW_PATH
        or payload.get("workflow_ref") != WORKFLOW_REF
        or payload.get("workflow_sha") != intent["workflow_sha"]
        or payload.get("release_job") != RELEASE_JOB
        or payload.get("reservation_expires_at_epoch")
        != intent["reservation_expires_at_epoch"]
        or payload.get("approval_api_disposition")
        not in {"approved", "post-approved-recovered"}
        or (
            payload.get("approval_api_disposition") == "approved"
            and (
                not isinstance(payload.get("approval_response_sha256"), str)
                or DIGEST.fullmatch(payload["approval_response_sha256"]) is None
            )
        )
        or (
            payload.get("approval_api_disposition") == "post-approved-recovered"
            and payload.get("approval_response_sha256") is not None
        )
        or any(
            not isinstance(payload.get(field), str)
            or DIGEST.fullmatch(payload[field]) is None
            for field in (
                "completed_jobs_sha256",
                "post_pending_deployments_sha256",
                "review_history_sha256",
            )
        )
        or type(payload.get("approved_at_epoch")) is not int
        or not intent["discovered_at_epoch"] <= payload["approved_at_epoch"] <= intent["reservation_expires_at_epoch"]
    ):
        fail("runner-prerequisite-approval-binding-invalid")


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
    if type(current) is not int or current < 1 or os.geteuid() != 1000 or os.getegid() != 1000:
        fail("runner-prerequisite-process-invalid")
    clock = current_time or (lambda: int(time.time()))
    token: bytearray | None = None
    if requester is None:
        token = _read_admin_token()
        requester = _production_requester(token)
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
        if current < reservation_payload["created_at_epoch"] or current > reservation_payload["expires_at_epoch"]:
            fail("runner-prerequisite-reservation-time-invalid")
        _ensure_approval_root()
        intent_path, approval_path = _record_paths(reservation_raw)
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
            intent = _discover_intent(
                requester, reservation_raw, reservation_payload, receipt_id, current
            )
            intent_raw = _wire(
                intent, receipt_private, receipt_id, INTENT_SIGNATURE_DOMAIN
            )
            _publish_record(intent_path, intent_raw)
            _checkpoint("after-runner-prerequisite-intent")

        pending, pending_raw = _pending_for_run(requester, intent["run_id"])
        approval_response_digest: str | None = None
        api_disposition = "post-approved-recovered"
        if len(pending) == 1:
            environment_id, environment_name = _pending_environment(pending[0])
            if (
                environment_id != intent["environment_id"]
                or environment_name != intent["environment_name"]
                or _digest(pending_raw)
                != intent["initial_pending_deployments_sha256"]
            ):
                fail("runner-prerequisite-pending-rebound")
            response, response_raw = _json_request(
                requester,
                "POST",
                f"/{REPOSITORY_API}/actions/runs/{intent['run_id']}/pending_deployments",
                {
                    "comment": intent["comment"],
                    "environment_ids": [int(intent["environment_id"])],
                    "state": "approved",
                },
            )
            if not isinstance(response, list) or len(response) < 1:
                fail("runner-prerequisite-approval-response-invalid")
            approval_response_digest = _digest(response_raw)
            api_disposition = "approved"
            _checkpoint("after-runner-prerequisite-approval-post")
        elif len(pending) != 0:
            fail("runner-prerequisite-pending-invalid")

        post_pending, post_pending_raw = _pending_for_run(requester, intent["run_id"])
        if post_pending != []:
            fail("runner-prerequisite-approval-unconfirmed")
        review_raw, _review = _review_history(requester, intent)
        completed_jobs_raw, _job = _wait_prerequisite_success(
            requester, intent, current_time=clock, sleeper=sleeper
        )
        approved_at = clock()
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
            "prerequisite_job_name": PREREQUISITE_JOB,
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
            "version": 2,
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


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("command", choices=("approve",))
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        result = approve() if arguments.command == "approve" else None
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
