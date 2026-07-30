#!/usr/bin/env python3
"""Authenticated, bounded PropertyQuarry search E2E and soak gate.

The gate consumes only the short-lived internal-CI receipt produced by
``propertyquarry_postgres_browser_bootstrap.py``.  Secrets remain in memory and
the public receipt intentionally contains neither account identifiers nor
credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
for _python_root in (ROOT, ROOT / "ea"):
    if str(_python_root) not in sys.path:
        sys.path.insert(0, str(_python_root))

try:
    from scripts.propertyquarry_playwright_runtime import (
        playwright_engine_launch_browser,
    )
except ModuleNotFoundError:
    from propertyquarry_playwright_runtime import (  # type: ignore[no-redef]
        playwright_engine_launch_browser,
    )


CONTRACT_NAME = "propertyquarry.long_running_e2e.v1"
SESSION_CONTRACT_NAME = "propertyquarry.postgres_browser_internal_session"
THREE_D_CONTRACT_NAME = "propertyquarry.3d_browser_gate.v1"
PRODUCTION_ORIGIN = "https://propertyquarry.com"
SESSION_COOKIE_NAME = "ea_workspace_session"
SEARCH_ROUTE = "/app/search"
RUN_STATUS_ROUTE_TEMPLATE = "/app/api/property/search-runs/{run_id}"
DEFAULT_THREE_D_SLUG = (
    "danubeflats-urban-jungle-layout-first-a43055be7b58de51447e"
)
MAX_SESSION_RECEIPT_BYTES = 64 * 1024
MAX_THREE_D_RECEIPT_BYTES = 2 * 1024 * 1024
MAX_SOAK_ITERATIONS = 6
QUICK_ITERATIONS = 2
SUCCESS_TERMINAL_STATUSES = frozenset(
    {
        "completed",
        "completed_partial",
        "completed_no_results",
        "processed",
        "ready",
    }
)
FAILURE_TERMINAL_STATUSES = frozenset(
    {
        "blocked",
        "canceled",
        "cancelled",
        "error",
        "failed",
        "timed_out",
        "timeout",
    }
)
TERMINAL_STATUSES = SUCCESS_TERMINAL_STATUSES | FAILURE_TERMINAL_STATUSES
ACTIVE_STATUSES = frozenset(
    {
        "in_progress",
        "pending",
        "processing",
        "queued",
        "repairing",
        "running",
        "starting",
        "warming",
        "working",
    }
)
CRITICAL_RESOURCE_TYPES = frozenset({"document", "fetch", "script", "xhr"})
CONSOLE_FAILURE_PATTERNS = (
    "content security policy",
    "err_blocked_by_response",
    "failed to fetch",
    "refused to",
    "uncaught",
)
SENSITIVE_KEY_FRAGMENTS = (
    "access_token",
    "authorization",
    "cookie",
    "email",
    "principal",
    "set-cookie",
)
EMAIL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9.-])"
)
BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class GateSafetyError(RuntimeError):
    """A stable, non-secret safety failure."""


@dataclass(frozen=True)
class GateConfig:
    origin: str
    session_file: Path
    mode: str
    iterations: int
    run_timeout_seconds: int
    poll_seconds: float
    browser_timeout_ms: int
    screenshots_dir: Path
    private_har_path: Path | None
    three_d_receipt_path: Path | None
    three_d_slug: str
    confirm_live: bool
    confirm_search_side_effects: bool


@dataclass(frozen=True)
class InternalCISession:
    access_token: str
    expires_at: str
    receipt_sha256: str
    redaction_values: tuple[str, ...]


@dataclass
class NetworkJournal:
    requests: list[dict[str, object]]
    responses: list[dict[str, object]]
    request_failures: list[dict[str, object]]
    console_messages: list[dict[str, object]]
    page_errors: list[str]
    expect_offline: bool = False

    @classmethod
    def empty(cls) -> "NetworkJournal":
        return cls([], [], [], [], [])

    def attach(self, page: Any) -> None:
        page.on(
            "request",
            lambda request: self.requests.append(
                {
                    "method": str(request.method or "").upper(),
                    "url": str(request.url or ""),
                    "resource_type": str(request.resource_type or ""),
                }
            ),
        )
        page.on(
            "response",
            lambda response: self.responses.append(
                {
                    "status": int(response.status or 0),
                    "url": str(response.url or ""),
                    "resource_type": str(response.request.resource_type or ""),
                }
            ),
        )
        page.on(
            "requestfailed",
            lambda request: self.request_failures.append(
                {
                    "url": str(request.url or ""),
                    "resource_type": str(request.resource_type or ""),
                    "failure": str(request.failure or "")[:1_000],
                    "expected_offline": bool(self.expect_offline),
                }
            ),
        )
        page.on(
            "console",
            lambda message: self.console_messages.append(
                {
                    "type": str(message.type or ""),
                    "text": str(message.text or "")[:2_000],
                }
            ),
        )
        page.on("pageerror", lambda error: self.page_errors.append(str(error or "")[:2_000]))


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: object) -> str:
    return _sha256_bytes(str(value or "").encode("utf-8", errors="replace"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _check(name: str, ok: bool, **extra: object) -> dict[str, object]:
    return {"name": str(name), "ok": bool(ok), **extra}


def _parse_utc(value: object) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise GateSafetyError("internal_ci_session_expiry_missing")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise GateSafetyError("internal_ci_session_expiry_invalid") from exc
    if parsed.tzinfo is None:
        raise GateSafetyError("internal_ci_session_expiry_timezone_required")
    return parsed.astimezone(timezone.utc)


def normalize_gate_origin(
    value: object,
    *,
    allow_loopback_for_tests: bool = False,
) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise GateSafetyError("origin_invalid") from exc
    if parsed.username or parsed.password:
        raise GateSafetyError("origin_userinfo_forbidden")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise GateSafetyError("origin_must_not_include_path_query_or_fragment")
    host = str(parsed.hostname or "").strip().lower()
    if raw.rstrip("/") == PRODUCTION_ORIGIN:
        if parsed.scheme != "https" or host != "propertyquarry.com" or port is not None:
            raise GateSafetyError("exact_propertyquarry_https_origin_required")
        return PRODUCTION_ORIGIN
    if allow_loopback_for_tests:
        if parsed.scheme not in {"http", "https"}:
            raise GateSafetyError("test_loopback_http_or_https_required")
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise GateSafetyError("test_loopback_host_required")
        if port is None:
            raise GateSafetyError("test_loopback_explicit_port_required")
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, "", "", "")
        ).rstrip("/")
    raise GateSafetyError("exact_propertyquarry_https_origin_required")


def _absolute_path(value: object, *, field: str) -> Path:
    path = Path(str(value or "").strip()).expanduser()
    if not path.is_absolute():
        raise GateSafetyError(f"{field}_must_be_absolute")
    return path


def validate_gate_config(
    *,
    origin: object,
    session_file: object,
    mode: object = "quick",
    iterations: object = QUICK_ITERATIONS,
    run_timeout_seconds: object = 900,
    poll_seconds: object = 5.0,
    browser_timeout_ms: object = 45_000,
    screenshots_dir: object,
    private_har_path: object = "",
    three_d_receipt_path: object = "",
    three_d_slug: object = DEFAULT_THREE_D_SLUG,
    confirm_live: bool = False,
    confirm_search_side_effects: bool = False,
    allow_loopback_for_tests: bool = False,
) -> GateConfig:
    normalized_origin = normalize_gate_origin(
        origin,
        allow_loopback_for_tests=allow_loopback_for_tests,
    )
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in {"quick", "soak"}:
        raise GateSafetyError("mode_must_be_quick_or_soak")
    try:
        normalized_iterations = int(iterations)
        normalized_run_timeout = int(run_timeout_seconds)
        normalized_poll = float(poll_seconds)
        normalized_browser_timeout = int(browser_timeout_ms)
    except (TypeError, ValueError) as exc:
        raise GateSafetyError("numeric_configuration_invalid") from exc
    if normalized_mode == "quick" and normalized_iterations != QUICK_ITERATIONS:
        raise GateSafetyError("quick_mode_requires_exactly_two_launches")
    if normalized_mode == "soak" and not (
        2 <= normalized_iterations <= MAX_SOAK_ITERATIONS
    ):
        raise GateSafetyError("soak_iterations_out_of_bounds")
    timeout_upper_bound = 900 if normalized_mode == "quick" else 3_600
    if not 60 <= normalized_run_timeout <= timeout_upper_bound:
        raise GateSafetyError("run_timeout_out_of_bounds")
    if not 0.5 <= normalized_poll <= 60:
        raise GateSafetyError("poll_interval_out_of_bounds")
    if not 5_000 <= normalized_browser_timeout <= 120_000:
        raise GateSafetyError("browser_timeout_out_of_bounds")
    if normalized_origin == PRODUCTION_ORIGIN and not confirm_live:
        raise GateSafetyError("production_requires_confirm_live")
    if not confirm_search_side_effects:
        raise GateSafetyError("search_side_effects_require_confirmation")
    normalized_session = _absolute_path(session_file, field="session_file")
    normalized_screenshots = _absolute_path(
        screenshots_dir,
        field="screenshots_dir",
    )
    if normalized_screenshots == Path("/"):
        raise GateSafetyError("screenshots_dir_too_broad")
    har_path = (
        _absolute_path(private_har_path, field="private_har_path")
        if str(private_har_path or "").strip()
        else None
    )
    if har_path is not None and har_path.suffix.lower() != ".har":
        raise GateSafetyError("private_har_path_must_end_in_har")
    three_d_path = (
        _absolute_path(three_d_receipt_path, field="three_d_receipt_path")
        if str(three_d_receipt_path or "").strip()
        else None
    )
    slug = str(three_d_slug or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,159}", slug):
        raise GateSafetyError("three_d_slug_invalid")
    return GateConfig(
        origin=normalized_origin,
        session_file=normalized_session,
        mode=normalized_mode,
        iterations=normalized_iterations,
        run_timeout_seconds=normalized_run_timeout,
        poll_seconds=normalized_poll,
        browser_timeout_ms=normalized_browser_timeout,
        screenshots_dir=normalized_screenshots,
        private_har_path=har_path,
        three_d_receipt_path=three_d_path,
        three_d_slug=slug,
        confirm_live=bool(confirm_live),
        confirm_search_side_effects=bool(confirm_search_side_effects),
    )


def _read_strict_json_file(
    path: Path,
    *,
    maximum_bytes: int,
    require_mode_0600: bool,
    error_prefix: str,
) -> tuple[dict[str, object], bytes]:
    if not path.is_absolute():
        raise GateSafetyError(f"{error_prefix}_path_must_be_absolute")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise GateSafetyError(f"{error_prefix}_not_found") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise GateSafetyError(f"{error_prefix}_symlink_forbidden")
    if not stat.S_ISREG(metadata.st_mode):
        raise GateSafetyError(f"{error_prefix}_regular_file_required")
    if require_mode_0600 and stat.S_IMODE(metadata.st_mode) != 0o600:
        raise GateSafetyError(f"{error_prefix}_mode_must_be_0600")
    if metadata.st_size <= 0 or metadata.st_size > maximum_bytes:
        raise GateSafetyError(f"{error_prefix}_size_invalid")
    try:
        encoded = path.read_bytes()
        loaded = json.loads(encoded)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise GateSafetyError(f"{error_prefix}_json_invalid") from exc
    if not isinstance(loaded, dict):
        raise GateSafetyError(f"{error_prefix}_object_required")
    return dict(loaded), encoded


def load_internal_ci_session(
    path: Path,
    *,
    now: datetime | None = None,
    minimum_valid_for_seconds: int = 0,
) -> InternalCISession:
    payload, encoded = _read_strict_json_file(
        path,
        maximum_bytes=MAX_SESSION_RECEIPT_BYTES,
        require_mode_0600=True,
        error_prefix="internal_ci_session",
    )
    expected = {
        "contract_name": SESSION_CONTRACT_NAME,
        "version": 1,
        "status": "pass",
        "provisioning_scope": "internal_ci_only",
        "runtime_mode": "prod",
        "storage_backend": "postgres",
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise GateSafetyError("internal_ci_session_contract_invalid")
    access_token = str(payload.get("access_token") or "").strip()
    if len(access_token) < 24 or len(access_token) > 8_192:
        raise GateSafetyError("internal_ci_session_access_token_invalid")
    expires_at = _parse_utc(payload.get("expires_at"))
    effective_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    minimum_expiry = effective_now + timedelta(
        seconds=max(0, int(minimum_valid_for_seconds))
    )
    if expires_at <= minimum_expiry:
        raise GateSafetyError("internal_ci_session_validity_insufficient")
    email = str(payload.get("email") or "").strip()
    principal = str(payload.get("principal_id") or "").strip()
    if not email or not principal:
        raise GateSafetyError("internal_ci_session_identity_fields_missing")
    return InternalCISession(
        access_token=access_token,
        expires_at=expires_at.isoformat().replace("+00:00", "Z"),
        receipt_sha256=_sha256_bytes(encoded),
        redaction_values=tuple(
            value for value in (access_token, email, principal) if value
        ),
    )


def _redact_string(value: object, *, sensitive_values: Iterable[str]) -> str:
    text = str(value or "")
    for sensitive in sorted(
        {str(item) for item in sensitive_values if str(item)},
        key=len,
        reverse=True,
    ):
        text = text.replace(sensitive, "[redacted]")
    text = BEARER_PATTERN.sub("Bearer [redacted]", text)
    return EMAIL_PATTERN.sub("[redacted-email]", text)


def redact_public_receipt(
    value: object,
    *,
    sensitive_values: Iterable[str] = (),
) -> object:
    if isinstance(value, Mapping):
        redacted: dict[str, object] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            lowered = key.lower()
            if any(fragment in lowered for fragment in SENSITIVE_KEY_FRAGMENTS):
                continue
            redacted[key] = redact_public_receipt(
                raw_value,
                sensitive_values=sensitive_values,
            )
        return redacted
    if isinstance(value, (list, tuple, set)):
        return [
            redact_public_receipt(item, sensitive_values=sensitive_values)
            for item in value
        ]
    if isinstance(value, str):
        return _redact_string(value, sensitive_values=sensitive_values)
    return value


def _safe_path_from_url(value: object) -> str:
    try:
        parsed = urllib.parse.urlsplit(str(value or ""))
    except ValueError:
        return "/invalid-url"
    return urllib.parse.urlunsplit(("", "", parsed.path or "/", "", ""))


def _endpoint_url(origin: str, value: object, *, field: str) -> str:
    resolved = urllib.parse.urljoin(origin + "/", str(value or "").strip())
    try:
        parsed = urllib.parse.urlsplit(resolved)
    except ValueError as exc:
        raise GateSafetyError(f"{field}_invalid") from exc
    endpoint_origin = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, "", "", "")
    ).rstrip("/")
    if endpoint_origin != origin:
        raise GateSafetyError(f"{field}_must_be_same_origin")
    if parsed.username or parsed.password or parsed.fragment or not parsed.path:
        raise GateSafetyError(f"{field}_invalid")
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.query, "")
    )


def evaluate_launch_cardinality(
    requests: Sequence[Mapping[str, object]],
    *,
    preferences_url: str,
    start_url: str,
) -> dict[str, object]:
    def count(target: str) -> int:
        return sum(
            1
            for row in requests
            if str(row.get("method") or "").upper() == "POST"
            and str(row.get("url") or "") == target
        )

    preferences_count = count(preferences_url)
    start_count = count(start_url)
    return {
        "ok": preferences_count == 1 and start_count == 1,
        "preferences_post_count": preferences_count,
        "start_post_count": start_count,
    }


def evaluate_unique_launch_accounting(
    launches: Sequence[Mapping[str, object]],
    *,
    expected_iterations: int,
) -> dict[str, object]:
    iterations = [int(row.get("iteration") or 0) for row in launches]
    run_digests = [str(row.get("run_id_sha256") or "") for row in launches]
    modes = [str(row.get("mode") or "") for row in launches]
    expected_sequence = list(range(1, int(expected_iterations) + 1))
    expected_modes = [
        "immediate" if index % 2 == 0 else "hydrated"
        for index in range(int(expected_iterations))
    ]
    cardinalities_ok = all(
        isinstance(row.get("request_cardinality"), Mapping)
        and dict(row.get("request_cardinality") or {}).get("ok") is True
        for row in launches
    )
    return {
        "ok": bool(
            len(launches) == expected_iterations
            and iterations == expected_sequence
            and modes == expected_modes
            and all(run_digests)
            and len(set(run_digests)) == expected_iterations
            and cardinalities_ok
        ),
        "launch_count": len(launches),
        "unique_run_count": len({value for value in run_digests if value}),
        "iteration_sequence_ok": iterations == expected_sequence,
        "mode_sequence_ok": modes == expected_modes,
        "all_request_cardinalities_ok": cardinalities_ok,
    }


def normalize_poll_snapshot(payload: Mapping[str, object]) -> dict[str, object]:
    root = (
        dict(payload.get("run") or {})
        if isinstance(payload.get("run"), Mapping)
        else dict(payload)
    )
    summary = (
        dict(root.get("summary") or {})
        if isinstance(root.get("summary"), Mapping)
        else {}
    )
    status = str(
        root.get("status")
        or summary.get("status")
        or payload.get("status")
        or ""
    ).strip().lower()
    progress_value: float | None = None
    for candidate in (
        root.get("progress"),
        root.get("progress_percent"),
        summary.get("progress"),
        summary.get("progress_percent"),
        payload.get("progress"),
    ):
        if candidate in (None, ""):
            continue
        try:
            progress_value = max(0.0, min(100.0, float(candidate)))
        except (TypeError, ValueError):
            continue
        break
    return {
        "status": status,
        "progress": progress_value,
        "terminal": status in TERMINAL_STATUSES,
        "successful_terminal": status in SUCCESS_TERMINAL_STATUSES,
    }


def evaluate_poll_history(
    history: Sequence[Mapping[str, object]],
    *,
    regression_tolerance: float = 5.0,
) -> dict[str, object]:
    normalized = [normalize_poll_snapshot(row) for row in history]
    progress_values = [
        float(row["progress"])
        for row in normalized
        if row.get("progress") is not None
    ]
    regressions = [
        {
            "from": previous,
            "to": current,
        }
        for previous, current in zip(progress_values, progress_values[1:])
        if current + float(regression_tolerance) < previous
    ]
    final = normalized[-1] if normalized else {}
    terminal = bool(final.get("terminal"))
    successful_terminal = bool(final.get("successful_terminal"))
    final_progress = final.get("progress")
    terminal_progress_ok = (
        successful_terminal
        and final_progress is not None
        and float(final_progress) >= 100.0
    )
    return {
        "ok": bool(
            normalized
            and terminal
            and successful_terminal
            and terminal_progress_ok
            and not regressions
        ),
        "sample_count": len(normalized),
        "final_status": str(final.get("status") or ""),
        "final_progress": final_progress,
        "terminal": terminal,
        "successful_terminal": successful_terminal,
        "terminal_progress_ok": terminal_progress_ok,
        "progress_regression_count": len(regressions),
        "progress_regressions": regressions,
    }


def _artifact_summary(path: Path, *, include_mode: bool = False) -> dict[str, object]:
    metadata = path.lstat()
    result: dict[str, object] = {
        "path": str(path),
        "sha256": _sha256_file(path),
        "bytes": metadata.st_size,
    }
    if include_mode:
        result["mode"] = f"{stat.S_IMODE(metadata.st_mode):04o}"
    return result


def _prepare_artifact_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o750)
    if path.is_symlink() or not path.is_dir():
        raise GateSafetyError("artifact_directory_invalid")


def prepare_private_har(path: Path) -> None:
    _prepare_artifact_directory(path.parent)
    if path.exists() or path.is_symlink():
        raise GateSafetyError("private_har_target_must_not_exist")


def finalize_private_har(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise GateSafetyError("private_har_missing")
    path.chmod(0o600)
    if stat.S_IMODE(path.lstat().st_mode) != 0o600:
        raise GateSafetyError("private_har_mode_must_be_0600")
    return _artifact_summary(path, include_mode=True)


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    if not path.is_absolute():
        raise GateSafetyError("public_receipt_path_must_be_absolute")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    if path.is_symlink():
        raise GateSafetyError("public_receipt_symlink_forbidden")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _capture_screenshot(
    page: Any,
    *,
    path: Path,
    full_page: bool = True,
) -> dict[str, object]:
    _prepare_artifact_directory(path.parent)
    masks = [
        page.locator(
            "[data-account-label], .pqx-account-menu, "
            "[data-property-account-email], [data-workspace-principal]"
        )
    ]
    page.screenshot(
        path=str(path),
        full_page=bool(full_page),
        caret="hide",
        animations="disabled",
        mask=masks,
        mask_color="#6f7782",
    )
    path.chmod(0o644)
    return _artifact_summary(path)


def _is_report_only_csp_information(row: Mapping[str, object]) -> bool:
    text = str(row.get("text") or "").strip().lower()
    return (
        str(row.get("type") or "").strip().lower() == "info"
        and "violates the following content security policy" in text
        and "the policy is report-only" in text
    )


def evaluate_network_blockers(
    journal: NetworkJournal,
    *,
    origin: str,
    ignored_http_urls: Iterable[str] = (),
    ignored_console_patterns: Iterable[str] = (),
    allow_recovered_tour_images: bool = False,
) -> dict[str, object]:
    ignored = {str(value) for value in ignored_http_urls}
    ignored_console = {
        str(value or "").strip().lower()
        for value in ignored_console_patterns
        if str(value or "").strip()
    }

    def canonical_tour_image_url(value: object) -> str:
        raw = str(value or "")
        try:
            parsed = urllib.parse.urlsplit(raw)
            query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            normalized_query = urllib.parse.urlencode(
                [
                    (key, item)
                    for key, item in query
                    if key != "pq_asset_retry"
                ]
            )
            return urllib.parse.urlunsplit(
                (
                    parsed.scheme,
                    parsed.netloc,
                    parsed.path,
                    normalized_query,
                    parsed.fragment,
                )
            )
        except (TypeError, ValueError):
            return raw

    recovered_tour_image_urls = {
        canonical_tour_image_url(row.get("url"))
        for row in journal.responses
        if (
            allow_recovered_tour_images
            and str(row.get("resource_type") or "").lower() == "image"
            and 100 <= int(row.get("status") or 0) < 400
            and "pq_asset_retry=" in str(row.get("url") or "")
        )
    }
    recovered_tour_image_failure_count = 0
    console = [
        row
        for row in journal.console_messages
        if not _is_report_only_csp_information(row)
        and not any(
            pattern in str(row.get("text") or "").lower()
            for pattern in ignored_console
        )
        and (
            str(row.get("type") or "").lower() in {"assert", "error"}
            or any(
                pattern in str(row.get("text") or "").lower()
                for pattern in CONSOLE_FAILURE_PATTERNS
            )
        )
    ]
    successful_urls = {
        str(row.get("url") or "")
        for row in journal.responses
        if 100 <= int(row.get("status") or 0) < 400
    }
    request_failures: list[dict[str, object]] = []
    for row in journal.request_failures:
        if row.get("expected_offline") is True:
            continue
        url = str(row.get("url") or "")
        resource_type = str(row.get("resource_type") or "")
        failure = str(row.get("failure") or "").strip().lower()
        if "net::err_aborted" in failure and url in successful_urls:
            continue
        if (
            url.startswith(origin)
            or resource_type in CRITICAL_RESOURCE_TYPES
        ):
            request_failures.append(row)
    bad_http: list[dict[str, object]] = []
    for row in journal.responses:
        status_code = int(row.get("status") or 0)
        url = str(row.get("url") or "")
        resource_type = str(row.get("resource_type") or "")
        if status_code < 400 or url in ignored:
            continue
        if (
            allow_recovered_tour_images
            and status_code in {502, 503, 504}
            and resource_type.lower() == "image"
            and canonical_tour_image_url(url) in recovered_tour_image_urls
        ):
            recovered_tour_image_failure_count += 1
            continue
        if url.startswith(origin) or resource_type in CRITICAL_RESOURCE_TYPES:
            bad_http.append(row)
    recovered_console_allowances = recovered_tour_image_failure_count
    remaining_console: list[dict[str, object]] = []
    for row in console:
        text = str(row.get("text") or "").lower()
        recovered_transient_console = (
            recovered_console_allowances > 0
            and "failed to load resource" in text
            and any(marker in text for marker in ("502", "503", "504"))
        )
        if recovered_transient_console:
            recovered_console_allowances -= 1
            continue
        remaining_console.append(row)
    console = remaining_console
    page_errors = list(journal.page_errors)

    def digests(rows: Iterable[object]) -> list[str]:
        return [_sha256_text(row) for row in list(rows)[:12]]

    return {
        "ok": not console and not request_failures and not bad_http and not page_errors,
        "console_blocker_count": len(console),
        "page_error_count": len(page_errors),
        "request_failure_count": len(request_failures),
        "bad_http_count": len(bad_http),
        "recovered_tour_image_failure_count": recovered_tour_image_failure_count,
        "console_digests": digests(console),
        "page_error_digests": digests(page_errors),
        "request_failure_digests": digests(request_failures),
        "bad_http_digests": digests(bad_http),
    }


def _install_launch_state_monitor(page: Any, *, storage_key: str) -> None:
    encoded_key = json.dumps(storage_key)
    page.evaluate(
        f"""() => {{
          const storageKey = {encoded_key};
          const root = document.querySelector('[data-property-decision-workbench]');
          const button = root?.querySelector('[data-property-start-top]');
          if (!(root instanceof HTMLElement) || !(button instanceof HTMLElement)) return;
          const read = () => {{
            try {{
              return JSON.parse(sessionStorage.getItem(storageKey) || '{{}}');
            }} catch (_error) {{
              return {{}};
            }}
          }};
          const capture = () => {{
            const state = read();
            const statusText = Array.from(root.querySelectorAll(
              '[data-property-top-launch-status], [data-property-launch-status], [data-property-inline-status], [data-property-inline-error]'
            )).map((node) => String(node.textContent || '').trim()).filter(Boolean).join(' | ');
            const busy = button.getAttribute('aria-busy') === 'true'
              || button.hasAttribute('disabled')
              || /preparing|saving|starting|launching/i.test(statusText);
            const error = /could not|did not load|stopped|retry|reload|failed|failure|unavailable|too long/i.test(statusText);
            state.busy_observed = Boolean(state.busy_observed || busy);
            state.error_observed = Boolean(state.error_observed || error);
            state.queued_observed = Boolean(
              state.queued_observed
              || button.dataset.pqLaunchQueued === 'true'
              || button.dataset.pqHydrationPending === 'true'
            );
            state.capture_count = Number(state.capture_count || 0) + 1;
            sessionStorage.setItem(storageKey, JSON.stringify(state));
          }};
          new MutationObserver(capture).observe(root, {{
            subtree: true,
            childList: true,
            characterData: true,
            attributes: true,
            attributeFilter: ['aria-busy', 'aria-disabled', 'disabled', 'data-pq-launch-queued', 'data-pq-hydration-pending'],
          }});
          capture();
        }}"""
    )


def _read_launch_state(page: Any, *, storage_key: str) -> dict[str, object]:
    return dict(
        page.evaluate(
            "(key) => { try { return JSON.parse(sessionStorage.getItem(key) || '{}'); } "
            "catch (_error) { return {}; } }",
            storage_key,
        )
        or {}
    )


def _discover_workbench_endpoints(
    page: Any,
    *,
    origin: str,
    timeout_ms: int,
) -> tuple[str, str]:
    page.locator(
        '[data-console-form-variant="property_search"] input, '
        '[data-console-form-variant="property_search"] select'
    ).first.focus(timeout=timeout_ms)
    page.wait_for_function(
        "() => document.querySelector('[data-property-decision-workbench]')"
        "?.dataset.pqWorkbenchController === 'loaded'",
        timeout=timeout_ms,
    )
    endpoints = dict(
        page.evaluate(
            """() => {
              const root = document.querySelector('[data-property-decision-workbench]');
              const metaNode = root?.querySelector('[data-property-workspace-meta]');
              const dataNode = root?.querySelector('[data-property-workbench-json]');
              let meta = {};
              let data = {};
              try { meta = JSON.parse(metaNode?.getAttribute('data-property-workspace-meta') || '{}'); }
              catch (_error) {}
              try { data = JSON.parse(dataNode?.textContent || '{}'); }
              catch (_error) {}
              return {
                preferences: String(meta.preferences_endpoint || data.endpoints?.preferences || ''),
                start: String(meta.start_endpoint || data.endpoints?.start || ''),
              };
            }"""
        )
        or {}
    )
    preferences_url = _endpoint_url(
        origin,
        endpoints.get("preferences"),
        field="preferences_endpoint",
    )
    start_url = _endpoint_url(
        origin,
        endpoints.get("start"),
        field="start_endpoint",
    )
    if not urllib.parse.urlsplit(preferences_url).path.rstrip("/").endswith(
        "/onboarding/property-search/preferences"
    ):
        raise GateSafetyError("preferences_endpoint_contract_invalid")
    start_path = urllib.parse.urlsplit(start_url).path.rstrip("/")
    if "property" not in start_path or "search" not in start_path:
        raise GateSafetyError("start_endpoint_contract_invalid")
    return preferences_url, start_url


def _extract_run_id(url: object) -> str:
    try:
        parsed = urllib.parse.urlsplit(str(url or ""))
        run_id = str(
            urllib.parse.parse_qs(parsed.query, keep_blank_values=True).get(
                "run_id",
                [""],
            )[0]
            or ""
        ).strip()
    except (TypeError, ValueError):
        return ""
    return run_id if RUN_ID_PATTERN.fullmatch(run_id) else ""


def _poll_search_run(
    *,
    context: Any,
    page: Any,
    config: GateConfig,
    run_id: str,
) -> tuple[list[dict[str, object]], dict[str, object], bool]:
    quoted_run_id = urllib.parse.quote(run_id, safe="")
    status_url = (
        config.origin
        + RUN_STATUS_ROUTE_TEMPLATE.format(run_id=quoted_run_id)
    )
    deadline = time.monotonic() + config.run_timeout_seconds
    history: list[dict[str, object]] = []
    reload_recovered = False
    reload_attempted = False
    started = time.monotonic()
    while time.monotonic() < deadline:
        response = context.request.get(
            status_url,
            timeout=config.browser_timeout_ms,
            fail_on_status_code=False,
        )
        if int(response.status or 0) != 200:
            history.append(
                {
                    "status": "http_error",
                    "progress": None,
                    "http_status": int(response.status or 0),
                }
            )
            break
        try:
            payload = response.json()
        except Exception:
            payload = {}
        snapshot = normalize_poll_snapshot(
            dict(payload) if isinstance(payload, Mapping) else {}
        )
        history.append(
            {
                "status": snapshot["status"],
                "progress": snapshot["progress"],
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        )
        if not reload_attempted:
            reload_attempted = True
            reload_response = page.reload(
                wait_until="domcontentloaded",
                timeout=config.browser_timeout_ms,
            )
            reload_recovered = bool(
                reload_response
                and reload_response.ok
                and _extract_run_id(page.url) == run_id
                and page.locator(
                    "[data-property-decision-workbench], main"
                ).count()
            )
        if snapshot["terminal"]:
            break
        page.wait_for_timeout(int(config.poll_seconds * 1_000))
    return history, evaluate_poll_history(history), reload_recovered


def _authenticated_page_ok(page: Any, *, origin: str) -> bool:
    return (
        str(page.url or "").startswith(origin + "/app/")
        and "/sign-in" not in str(page.url or "")
        and page.locator("[data-property-decision-workbench], main").count() > 0
    )


def _exercise_launch(
    *,
    context: Any,
    config: GateConfig,
    iteration: int,
    mode: str,
    preferences_url: str,
    start_url: str,
    checks: list[dict[str, object]],
    screenshots: list[dict[str, object]],
) -> tuple[dict[str, object], str]:
    page = context.new_page()
    journal = NetworkJournal.empty()
    journal.attach(page)
    storage_key = f"pq-long-e2e-launch-{iteration}"
    try:
        response = page.goto(
            config.origin + SEARCH_ROUTE,
            wait_until="domcontentloaded",
            timeout=config.browser_timeout_ms,
        )
        checks.append(
            _check(
                f"launch_{iteration}_search_surface",
                bool(response and response.ok and _authenticated_page_ok(page, origin=config.origin)),
                mode=mode,
                status=int(response.status if response else 0),
            )
        )
        root = page.locator("[data-property-decision-workbench]")
        button = page.locator("[data-property-start-top]").first
        root.wait_for(state="visible", timeout=config.browser_timeout_ms)
        button.wait_for(state="visible", timeout=config.browser_timeout_ms)
        if mode == "hydrated":
            page.locator(
                '[data-console-form-variant="property_search"] input, '
                '[data-console-form-variant="property_search"] select'
            ).first.focus(timeout=config.browser_timeout_ms)
            page.wait_for_function(
                "() => document.querySelector('[data-property-decision-workbench]')"
                "?.dataset.pqWorkbenchController === 'loaded'",
                timeout=config.browser_timeout_ms,
            )
        controller_before = str(
            root.get_attribute("data-pq-workbench-controller") or ""
        ).strip()
        expected_controller_state = (
            controller_before == "loaded"
            if mode == "hydrated"
            else controller_before != "loaded"
        )
        checks.append(
            _check(
                f"launch_{iteration}_{mode}_state_observed",
                expected_controller_state,
                controller_loaded_before_click=controller_before == "loaded",
            )
        )
        _install_launch_state_monitor(page, storage_key=storage_key)
        button.click(timeout=config.browser_timeout_ms, no_wait_after=True)
        page.wait_for_url(
            "**/app/properties?*run_id=*",
            wait_until="domcontentloaded",
            timeout=min(
                config.run_timeout_seconds * 1_000,
                max(config.browser_timeout_ms, 180_000),
            ),
        )
        run_id = _extract_run_id(page.url)
        launch_state = _read_launch_state(page, storage_key=storage_key)
        cardinality = evaluate_launch_cardinality(
            journal.requests,
            preferences_url=preferences_url,
            start_url=start_url,
        )
        checks.extend(
            [
                _check(
                    f"launch_{iteration}_visible_busy_state",
                    launch_state.get("busy_observed") is True,
                    mode=mode,
                ),
                _check(
                    f"launch_{iteration}_request_cardinality",
                    bool(cardinality["ok"]),
                    **{
                        key: value
                        for key, value in cardinality.items()
                        if key != "ok"
                    },
                ),
                _check(
                    f"launch_{iteration}_redirect_with_run_id",
                    bool(run_id),
                ),
            ]
        )
        history: list[dict[str, object]] = []
        poll_evaluation: dict[str, object] = {
            "ok": False,
            "sample_count": 0,
            "final_status": "",
        }
        reload_recovered = False
        if run_id:
            history, poll_evaluation, reload_recovered = _poll_search_run(
                context=context,
                page=page,
                config=config,
                run_id=run_id,
            )
        checks.extend(
            [
                _check(
                    f"launch_{iteration}_poll_terminal_success",
                    bool(poll_evaluation.get("ok")),
                    sample_count=int(poll_evaluation.get("sample_count") or 0),
                    final_status=str(poll_evaluation.get("final_status") or ""),
                    final_progress=poll_evaluation.get("final_progress"),
                    progress_regression_count=int(
                        poll_evaluation.get("progress_regression_count") or 0
                    ),
                ),
                _check(
                    f"launch_{iteration}_reload_recovery",
                    reload_recovered,
                ),
            ]
        )
        screenshot = _capture_screenshot(
            page,
            path=config.screenshots_dir / f"launch-{iteration:02d}-{mode}.png",
        )
        screenshots.append(screenshot)
        network = evaluate_network_blockers(journal, origin=config.origin)
        checks.append(
            _check(
                f"launch_{iteration}_no_browser_blockers",
                bool(network["ok"]),
                **{
                    key: value
                    for key, value in network.items()
                    if key != "ok"
                },
            )
        )
        return (
            {
                "iteration": iteration,
                "mode": mode,
                "status": "pass"
                if cardinality["ok"]
                and run_id
                and poll_evaluation.get("ok")
                and reload_recovered
                and network["ok"]
                else "fail",
                "run_id_sha256": _sha256_text(run_id) if run_id else "",
                "launch_state": {
                    "busy_observed": launch_state.get("busy_observed") is True,
                    "queued_observed": launch_state.get("queued_observed") is True,
                },
                "request_cardinality": cardinality,
                "poll": {
                    key: value
                    for key, value in poll_evaluation.items()
                    if key != "progress_regressions"
                },
                "poll_samples": history,
                "reload_recovered": reload_recovered,
                "network": network,
                "screenshot": screenshot,
            },
            run_id,
        )
    finally:
        page.close()


def _exercise_visible_error_probe(
    *,
    context: Any,
    config: GateConfig,
    preferences_url: str,
    start_url: str,
    checks: list[dict[str, object]],
    screenshots: list[dict[str, object]],
) -> dict[str, object]:
    page = context.new_page()
    journal = NetworkJournal.empty()
    journal.attach(page)
    storage_key = "pq-long-e2e-synthetic-error"

    def synthetic_start_failure(route: Any) -> None:
        route.fulfill(
            status=503,
            content_type="application/json",
            body=json.dumps({"detail": "e2e synthetic start failure"}),
        )

    try:
        page.route(start_url, synthetic_start_failure)
        response = page.goto(
            config.origin + SEARCH_ROUTE,
            wait_until="domcontentloaded",
            timeout=config.browser_timeout_ms,
        )
        page.locator(
            '[data-console-form-variant="property_search"] input, '
            '[data-console-form-variant="property_search"] select'
        ).first.focus(timeout=config.browser_timeout_ms)
        page.wait_for_function(
            "() => document.querySelector('[data-property-decision-workbench]')"
            "?.dataset.pqWorkbenchController === 'loaded'",
            timeout=config.browser_timeout_ms,
        )
        _install_launch_state_monitor(page, storage_key=storage_key)
        button = page.locator("[data-property-start-top]").first
        button.click(timeout=config.browser_timeout_ms, no_wait_after=True)
        page.wait_for_function(
            """() => {
              const root = document.querySelector('[data-property-decision-workbench]');
              const button = root?.querySelector('[data-property-start-top]');
              const text = Array.from(root?.querySelectorAll(
                '[data-property-inline-error], [data-property-top-launch-status]'
              ) || []).map((node) => String(node.textContent || '').trim()).join(' ');
              return Boolean(text)
                && button?.getAttribute('aria-busy') === 'false'
                && !button?.hasAttribute('disabled');
            }""",
            timeout=config.browser_timeout_ms,
        )
        launch_state = _read_launch_state(page, storage_key=storage_key)
        cardinality = evaluate_launch_cardinality(
            journal.requests,
            preferences_url=preferences_url,
            start_url=start_url,
        )
        error_visible = bool(
            page.locator(
                "[data-property-inline-error], [data-property-top-launch-status]"
            ).evaluate_all(
                """(nodes) => nodes.some((node) => {
                  const style = getComputedStyle(node);
                  const box = node.getBoundingClientRect();
                  return String(node.textContent || '').trim()
                    && style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && box.width > 0
                    && box.height > 0;
                })"""
            )
        )
        checks.extend(
            [
                _check(
                    "synthetic_error_search_surface",
                    bool(response and response.ok and _authenticated_page_ok(page, origin=config.origin)),
                ),
                _check(
                    "synthetic_error_visible_and_action_recovered",
                    error_visible
                    and launch_state.get("busy_observed") is True
                    and launch_state.get("error_observed") is True,
                ),
                _check(
                    "synthetic_error_request_cardinality",
                    bool(cardinality["ok"]),
                    **{
                        key: value
                        for key, value in cardinality.items()
                        if key != "ok"
                    },
                ),
            ]
        )
        screenshot = _capture_screenshot(
            page,
            path=config.screenshots_dir / "synthetic-start-error.png",
        )
        screenshots.append(screenshot)
        network = evaluate_network_blockers(
            journal,
            origin=config.origin,
            ignored_http_urls=(start_url,),
            ignored_console_patterns=(
                "failed to load resource: the server responded with a status of 503",
            ),
        )
        checks.append(
            _check(
                "synthetic_error_no_unexpected_browser_blockers",
                bool(network["ok"]),
                **{
                    key: value
                    for key, value in network.items()
                    if key != "ok"
                },
            )
        )
        return {
            "status": "pass"
            if error_visible and cardinality["ok"] and network["ok"]
            else "fail",
            "synthetic_http_status": 503,
            "provider_start_forwarded": False,
            "request_cardinality": cardinality,
            "busy_observed": launch_state.get("busy_observed") is True,
            "error_observed": launch_state.get("error_observed") is True,
            "network": network,
            "screenshot": screenshot,
        }
    finally:
        page.close()


def _surface_metrics(page: Any) -> dict[str, object]:
    return dict(
        page.evaluate(
            """() => {
              const visible = (node) => {
                const style = getComputedStyle(node);
                const box = node.getBoundingClientRect();
                return !node.hidden
                  && style.display !== 'none'
                  && style.visibility !== 'hidden'
                  && box.width > 0
                  && box.height > 0;
              };
              const controls = Array.from(document.querySelectorAll(
                'button, summary, [role="button"], .pqx-button'
              )).filter(visible);
              const undersized = controls.filter((node) => {
                const box = node.getBoundingClientRect();
                return box.width < 44 || box.height < 44;
              });
              const animations = document.getAnimations().filter((animation) => {
                const timing = animation.effect?.getComputedTiming?.() || {};
                return animation.playState === 'running'
                  && Number(timing.duration || 0) > 0;
              });
              return {
                viewport_width: window.innerWidth,
                scroll_width: document.documentElement.scrollWidth,
                visible_control_count: controls.length,
                undersized_touch_target_count: undersized.length,
                reduced_motion: matchMedia('(prefers-reduced-motion: reduce)').matches,
                active_animation_count: animations.length,
                main_count: document.querySelectorAll('main').length,
              };
            }"""
        )
        or {}
    )


def _exercise_surfaces_and_recovery(
    *,
    context: Any,
    config: GateConfig,
    run_id: str,
    checks: list[dict[str, object]],
    screenshots: list[dict[str, object]],
) -> dict[str, object]:
    quoted_run_id = urllib.parse.quote(run_id, safe="")
    routes = (
        ("results", f"/app/properties?run_id={quoted_run_id}"),
        ("shortlist", f"/app/shortlist?run_id={quoted_run_id}"),
        ("research", f"/app/research?run_id={quoted_run_id}"),
    )
    rows: list[dict[str, object]] = []
    research_detail_route = ""
    for name, route in routes:
        page = context.new_page()
        journal = NetworkJournal.empty()
        journal.attach(page)
        try:
            response = page.goto(
                config.origin + route,
                wait_until="domcontentloaded",
                timeout=config.browser_timeout_ms,
            )
            authenticated = _authenticated_page_ok(page, origin=config.origin)
            if name == "results":
                detail = page.locator('a[href^="/app/research/"]').first
                if detail.count():
                    research_detail_route = str(detail.get_attribute("href") or "")
            network = evaluate_network_blockers(journal, origin=config.origin)
            ok = bool(response and response.ok and authenticated and network["ok"])
            checks.append(
                _check(
                    f"{name}_surface_available",
                    ok,
                    status=int(response.status if response else 0),
                    network_blocker_count=(
                        int(network["console_blocker_count"])
                        + int(network["page_error_count"])
                        + int(network["request_failure_count"])
                        + int(network["bad_http_count"])
                    ),
                )
            )
            screenshot = _capture_screenshot(
                page,
                path=config.screenshots_dir / f"surface-{name}.png",
            )
            screenshots.append(screenshot)
            rows.append(
                {
                    "name": name,
                    "route": _safe_path_from_url(route),
                    "status": "pass" if ok else "fail",
                    "http_status": int(response.status if response else 0),
                    "network": network,
                    "screenshot": screenshot,
                }
            )
        finally:
            page.close()

    detail_row: dict[str, object] = {
        "available": bool(research_detail_route),
        "status": "not_applicable",
    }
    if research_detail_route:
        page = context.new_page()
        journal = NetworkJournal.empty()
        journal.attach(page)
        try:
            detail_url = _endpoint_url(
                config.origin,
                research_detail_route,
                field="research_detail_route",
            )
            response = page.goto(
                detail_url,
                wait_until="domcontentloaded",
                timeout=config.browser_timeout_ms,
            )
            network = evaluate_network_blockers(journal, origin=config.origin)
            ok = bool(
                response
                and response.ok
                and _authenticated_page_ok(page, origin=config.origin)
                and network["ok"]
            )
            checks.append(
                _check(
                    "research_detail_when_available",
                    ok,
                    status=int(response.status if response else 0),
                )
            )
            screenshot = _capture_screenshot(
                page,
                path=config.screenshots_dir / "surface-research-detail.png",
            )
            screenshots.append(screenshot)
            detail_row = {
                "available": True,
                "status": "pass" if ok else "fail",
                "route": _safe_path_from_url(research_detail_route),
                "network": network,
                "screenshot": screenshot,
            }
        finally:
            page.close()

    page = context.new_page()
    journal = NetworkJournal.empty()
    journal.attach(page)
    offline_observed = False
    online_recovered = False
    mobile_metrics: dict[str, object] = {}
    try:
        page.set_viewport_size({"width": 390, "height": 844})
        results_url = (
            f"{config.origin}/app/properties?run_id={quoted_run_id}"
        )
        response = page.goto(
            results_url,
            wait_until="domcontentloaded",
            timeout=config.browser_timeout_ms,
        )
        mobile_metrics = _surface_metrics(page)
        mobile_ok = bool(
            response
            and response.ok
            and int(mobile_metrics.get("scroll_width") or 0)
            <= int(mobile_metrics.get("viewport_width") or 0) + 1
            and int(mobile_metrics.get("undersized_touch_target_count") or 0)
            == 0
            and mobile_metrics.get("reduced_motion") is True
            and int(mobile_metrics.get("active_animation_count") or 0) == 0
            and int(mobile_metrics.get("main_count") or 0) == 1
        )
        checks.append(
            _check(
                "mobile_touch_reduced_motion",
                mobile_ok,
                **mobile_metrics,
            )
        )
        screenshot = _capture_screenshot(
            page,
            path=config.screenshots_dir / "mobile-results.png",
        )
        screenshots.append(screenshot)
        journal.expect_offline = True
        context.set_offline(True)
        try:
            offline_response = page.reload(
                wait_until="domcontentloaded",
                timeout=min(config.browser_timeout_ms, 10_000),
            )
            offline_observed = offline_response is None or not offline_response.ok
        except Exception:
            offline_observed = True
        try:
            screenshot = _capture_screenshot(
                page,
                path=config.screenshots_dir / "offline-state.png",
                full_page=False,
            )
            screenshots.append(screenshot)
        except Exception:
            pass
        finally:
            context.set_offline(False)
            journal.expect_offline = False
        retry_response = page.goto(
            results_url,
            wait_until="domcontentloaded",
            timeout=config.browser_timeout_ms,
        )
        online_recovered = bool(
            retry_response
            and retry_response.ok
            and _authenticated_page_ok(page, origin=config.origin)
        )
        checks.extend(
            [
                _check("offline_failure_observed", offline_observed),
                _check("online_retry_recovered", online_recovered),
            ]
        )
        screenshot = _capture_screenshot(
            page,
            path=config.screenshots_dir / "online-retry-recovered.png",
        )
        screenshots.append(screenshot)
        network = evaluate_network_blockers(journal, origin=config.origin)
        checks.append(
            _check(
                "mobile_recovery_no_unexpected_browser_blockers",
                bool(network["ok"]),
                **{
                    key: value
                    for key, value in network.items()
                    if key != "ok"
                },
            )
        )
    finally:
        if journal.expect_offline:
            context.set_offline(False)
        page.close()
    return {
        "surfaces": rows,
        "research_detail": detail_row,
        "mobile": mobile_metrics,
        "offline_observed": offline_observed,
        "online_recovered": online_recovered,
    }


def _load_three_d_checkpoint(
    path: Path,
    *,
    origin: str,
) -> dict[str, object]:
    payload, encoded = _read_strict_json_file(
        path,
        maximum_bytes=MAX_THREE_D_RECEIPT_BYTES,
        require_mode_0600=False,
        error_prefix="three_d_checkpoint",
    )
    base = str(
        payload.get("browser_base_url")
        or payload.get("base_url")
        or ""
    ).strip().rstrip("/")
    provider_results = [
        dict(row)
        for row in list(payload.get("provider_results") or [])
        if isinstance(row, Mapping)
    ]
    provider_pass = any(
        str(row.get("provider") or "").strip().lower() == "3dvista"
        and str(row.get("status") or "").strip().lower() == "pass"
        and (
            not isinstance(row.get("state"), Mapping)
            or bool(dict(row.get("state") or {}).get("provider_frame_url"))
        )
        for row in provider_results
    )
    checks = [
        dict(row)
        for row in list(payload.get("checks") or [])
        if isinstance(row, Mapping)
    ]
    valid = bool(
        payload.get("contract_name") == THREE_D_CONTRACT_NAME
        and str(payload.get("status") or "").lower() == "pass"
        and int(payload.get("failed_count") or 0) == 0
        and base == origin
        and checks
        and all(row.get("ok") is True for row in checks)
        and provider_pass
    )
    return {
        "ok": valid,
        "contract_name": str(payload.get("contract_name") or ""),
        "status": str(payload.get("status") or ""),
        "check_count": len(checks),
        "provider_pass": provider_pass,
        "receipt_sha256": _sha256_bytes(encoded),
        "path": str(path),
    }


def _exercise_three_d_cutaway_checkpoint(
    *,
    browser: Any,
    config: GateConfig,
    checks: list[dict[str, object]],
    screenshots: list[dict[str, object]],
) -> dict[str, object]:
    composed: dict[str, object] = {
        "configured": config.three_d_receipt_path is not None,
        "ok": True,
    }
    if config.three_d_receipt_path is not None:
        composed = _load_three_d_checkpoint(
            config.three_d_receipt_path,
            origin=config.origin,
        )
        checks.append(
            _check(
                "three_d_composed_checkpoint",
                bool(composed["ok"]),
                contract_name=composed["contract_name"],
                checkpoint_status=composed["status"],
                checkpoint_sha256=composed["receipt_sha256"],
                provider_pass=composed["provider_pass"],
            )
        )
    context = browser.new_context(
        viewport={"width": 1440, "height": 1000},
        reduced_motion="reduce",
        service_workers="block",
    )
    page = context.new_page()
    journal = NetworkJournal.empty()
    journal.attach(page)
    try:
        route = f"/tours/{urllib.parse.quote(config.three_d_slug, safe='')}"
        response = page.goto(
            config.origin + route,
            wait_until="networkidle",
            timeout=config.browser_timeout_ms,
        )
        provider_hook = page.locator(
            "#load-provider, .provider-frame, iframe:not([src*='generated-reconstruction']), "
            "[data-provider-status]"
        ).count() > 0
        viewer_hook = page.locator(
            "iframe[src*='/generated-reconstruction/viewer.html'], "
            "a[href*='/generated-reconstruction/viewer.html'], "
            "[data-generated-reconstruction-viewer], "
            "[data-pq-reconstruction-viewer]"
        ).count() > 0
        cutaway_hook = bool(
            page.locator(
                'img[src*="diorama"], img[alt*="cutaway" i], '
                '[data-diorama], [data-cutaway], a[href*="diorama"]'
            ).count()
        )
        body_text = page.locator("body").inner_text(timeout=config.browser_timeout_ms)
        truthful_disclosure = any(
            phrase in body_text.lower()
            for phrase in (
                "layout aid",
                "not a captured tour",
                "not a captured or measured tour",
                "planning reconstruction",
                "planning preview",
            )
        )
        page.wait_for_function(
            """() => !Array.from(document.images).some(
              (image) => image.getAttribute('data-pq-asset-status') === 'retrying'
            )""",
            timeout=config.browser_timeout_ms,
        )
        asset_health = dict(
            page.evaluate(
                """() => {
                  const images = Array.from(document.images).filter((image) => {
                    try {
                      return new URL(image.src, location.href).pathname.startsWith('/tours/files/');
                    } catch (_error) {
                      return false;
                    }
                  });
                  const recovered = images.filter(
                    (image) => image.getAttribute('data-pq-asset-status') === 'recovered'
                  );
                  const unavailable = images.filter(
                    (image) => image.getAttribute('data-pq-asset-status') === 'unavailable'
                  );
                  const broken = images.filter(
                    (image) => image.complete && image.naturalWidth === 0
                  );
                  return {
                    image_count: images.length,
                    recovered_count: recovered.length,
                    unavailable_count: unavailable.length,
                    broken_count: broken.length,
                  };
                }"""
            )
            or {}
        )
        asset_health_ok = bool(
            int(asset_health.get("image_count") or 0) > 0
            and int(asset_health.get("unavailable_count") or 0) == 0
            and int(asset_health.get("broken_count") or 0) == 0
        )
        network = evaluate_network_blockers(
            journal,
            origin=config.origin,
            allow_recovered_tour_images=bool(
                int(asset_health.get("recovered_count") or 0) > 0
                and asset_health_ok
            ),
        )
        route_ok = bool(
            response
            and response.ok
            and viewer_hook
            and cutaway_hook
            and truthful_disclosure
            and asset_health_ok
            and network["ok"]
        )
        checks.append(
            _check(
                "flagship_three_d_cutaway_route_hooks",
                route_ok,
                status=int(response.status if response else 0),
                provider_hook=provider_hook,
                viewer_hook=viewer_hook,
                cutaway_hook=cutaway_hook,
                truthful_disclosure=truthful_disclosure,
                asset_health_ok=asset_health_ok,
                recovered_asset_count=int(asset_health.get("recovered_count") or 0),
                unavailable_asset_count=int(asset_health.get("unavailable_count") or 0),
                broken_asset_count=int(asset_health.get("broken_count") or 0),
            )
        )
        screenshot = _capture_screenshot(
            page,
            path=config.screenshots_dir / "flagship-3d-cutaway-hooks.png",
        )
        screenshots.append(screenshot)
        return {
            "status": "pass"
            if route_ok and bool(composed.get("ok", True))
            else "fail",
            "route": route,
            "provider_hook": provider_hook,
            "viewer_hook": viewer_hook,
            "cutaway_hook": cutaway_hook,
            "truthful_disclosure": truthful_disclosure,
            "asset_health": asset_health,
            "composed_checkpoint": composed,
            "network": network,
            "screenshot": screenshot,
        }
    finally:
        page.close()
        context.close()


def _exercise_public_sign_in(
    *,
    browser: Any,
    config: GateConfig,
    checks: list[dict[str, object]],
    screenshots: list[dict[str, object]],
) -> dict[str, object]:
    context = browser.new_context(
        viewport={"width": 1280, "height": 900},
        reduced_motion="reduce",
        service_workers="block",
    )
    page = context.new_page()
    journal = NetworkJournal.empty()
    journal.attach(page)
    try:
        response = page.goto(
            config.origin + "/sign-in",
            wait_until="domcontentloaded",
            timeout=config.browser_timeout_ms,
        )
        body_text = page.locator("body").inner_text(timeout=config.browser_timeout_ms)
        identity_action = page.locator(
            'a[href^="/sign-in/"], form[action*="sign-in"], '
            'a[href*="/google"], a[href*="/facebook"]'
        ).count() > 0
        network = evaluate_network_blockers(journal, origin=config.origin)
        ok = bool(
            response
            and response.ok
            and page.locator("main").count() == 1
            and "sign in" in body_text.lower()
            and identity_action
            and network["ok"]
        )
        checks.append(
            _check(
                "public_sign_in_surface",
                ok,
                status=int(response.status if response else 0),
                identity_action=identity_action,
            )
        )
        screenshot = _capture_screenshot(
            page,
            path=config.screenshots_dir / "public-sign-in.png",
        )
        screenshots.append(screenshot)
        return {
            "status": "pass" if ok else "fail",
            "network": network,
            "screenshot": screenshot,
        }
    finally:
        page.close()
        context.close()


def build_long_running_e2e_receipt(config: GateConfig) -> dict[str, object]:
    minimum_validity = (
        config.iterations * config.run_timeout_seconds
        + config.browser_timeout_ms // 1_000
        + 600
    )
    session = load_internal_ci_session(
        config.session_file,
        minimum_valid_for_seconds=minimum_validity,
    )
    sensitive_values = session.redaction_values
    checks: list[dict[str, object]] = []
    screenshots: list[dict[str, object]] = []
    launches: list[dict[str, object]] = []
    har_summary: dict[str, object] | None = None
    receipt: dict[str, object] = {
        "contract_name": CONTRACT_NAME,
        "generated_at": utc_now(),
        "status": "running",
        "origin": config.origin,
        "execution": {
            "mode": config.mode,
            "iterations": config.iterations,
            "launch_modes": [
                "immediate" if index % 2 == 0 else "hydrated"
                for index in range(config.iterations)
            ],
            "successful_search_launch_budget": config.iterations,
            "synthetic_failure_probe_start_forwarded": False,
            "preferences_write_budget": config.iterations + 1,
            "provider_quota_side_effects": "bounded_search_launches",
            "confirm_live": config.confirm_live,
            "confirm_search_side_effects": config.confirm_search_side_effects,
            "run_timeout_seconds": config.run_timeout_seconds,
            "poll_seconds": config.poll_seconds,
        },
        "session": {
            "contract_name": SESSION_CONTRACT_NAME,
            "version": 1,
            "status": "pass",
            "receipt_sha256": session.receipt_sha256,
            "expires_at": session.expires_at,
        },
        "checks": checks,
        "launches": launches,
        "artifacts": {
            "screenshots": screenshots,
            "private_har": None,
        },
    }
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        checks.append(
            _check(
                "playwright_available",
                False,
                error_type=type(exc).__name__,
                error_detail=_redact_string(
                    exc,
                    sensitive_values=sensitive_values,
                )[:500],
            )
        )
        return finalize_public_receipt(
            receipt,
            sensitive_values=sensitive_values,
        )

    _prepare_artifact_directory(config.screenshots_dir)
    if config.private_har_path is not None:
        prepare_private_har(config.private_har_path)
    old_umask = os.umask(0o077)
    try:
        with sync_playwright() as playwright:
            browser = playwright_engine_launch_browser(
                playwright,
                engine="chromium",
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            try:
                receipt["public_sign_in"] = _exercise_public_sign_in(
                    browser=browser,
                    config=config,
                    checks=checks,
                    screenshots=screenshots,
                )
                context_options: dict[str, object] = {
                    "viewport": {"width": 1440, "height": 1000},
                    "reduced_motion": "reduce",
                    "service_workers": "block",
                }
                if config.private_har_path is not None:
                    context_options.update(
                        {
                            "record_har_path": str(config.private_har_path),
                            "record_har_mode": "minimal",
                            "record_har_content": "omit",
                        }
                    )
                context = browser.new_context(**context_options)
                context.add_cookies(
                    [
                        {
                            "name": SESSION_COOKIE_NAME,
                            "value": session.access_token,
                            "url": config.origin,
                            "httpOnly": True,
                            "secure": config.origin.startswith("https://"),
                            "sameSite": "Lax",
                        }
                    ]
                )
                try:
                    discovery_page = context.new_page()
                    discovery_journal = NetworkJournal.empty()
                    discovery_journal.attach(discovery_page)
                    try:
                        discovery_response = discovery_page.goto(
                            config.origin + SEARCH_ROUTE,
                            wait_until="domcontentloaded",
                            timeout=config.browser_timeout_ms,
                        )
                        preferences_url, start_url = _discover_workbench_endpoints(
                            discovery_page,
                            origin=config.origin,
                            timeout_ms=config.browser_timeout_ms,
                        )
                        discovery_network = evaluate_network_blockers(
                            discovery_journal,
                            origin=config.origin,
                        )
                        authenticated_ok = bool(
                            discovery_response
                            and discovery_response.ok
                            and _authenticated_page_ok(
                                discovery_page,
                                origin=config.origin,
                            )
                            and discovery_network["ok"]
                        )
                        checks.append(
                            _check(
                                "authenticated_search_surface_and_endpoints",
                                authenticated_ok,
                                status=int(
                                    discovery_response.status
                                    if discovery_response
                                    else 0
                                ),
                                preferences_path=_safe_path_from_url(
                                    preferences_url
                                ),
                                start_path=_safe_path_from_url(start_url),
                            )
                        )
                        screenshot = _capture_screenshot(
                            discovery_page,
                            path=config.screenshots_dir
                            / "authenticated-search.png",
                        )
                        screenshots.append(screenshot)
                    finally:
                        discovery_page.close()

                    last_run_id = ""
                    for index in range(config.iterations):
                        launch_mode = (
                            "immediate" if index % 2 == 0 else "hydrated"
                        )
                        launch, run_id = _exercise_launch(
                            context=context,
                            config=config,
                            iteration=index + 1,
                            mode=launch_mode,
                            preferences_url=preferences_url,
                            start_url=start_url,
                            checks=checks,
                            screenshots=screenshots,
                        )
                        launches.append(launch)
                        if run_id:
                            last_run_id = run_id

                    launch_accounting = evaluate_unique_launch_accounting(
                        launches,
                        expected_iterations=config.iterations,
                    )
                    checks.append(
                        _check(
                            "unique_launch_and_request_accounting",
                            bool(launch_accounting["ok"]),
                            **{
                                key: value
                                for key, value in launch_accounting.items()
                                if key != "ok"
                            },
                        )
                    )
                    receipt["synthetic_error_probe"] = (
                        _exercise_visible_error_probe(
                            context=context,
                            config=config,
                            preferences_url=preferences_url,
                            start_url=start_url,
                            checks=checks,
                            screenshots=screenshots,
                        )
                    )
                    if last_run_id:
                        receipt["authenticated_surfaces"] = (
                            _exercise_surfaces_and_recovery(
                                context=context,
                                config=config,
                                run_id=last_run_id,
                                checks=checks,
                                screenshots=screenshots,
                            )
                        )
                    else:
                        checks.append(
                            _check(
                                "authenticated_results_available_for_polish",
                                False,
                            )
                        )
                finally:
                    if config.private_har_path is not None:
                        # Closing flushes HAR content before its mode is checked.
                        context.close()
                    else:
                        context.close()

                receipt["three_d_cutaway"] = (
                    _exercise_three_d_cutaway_checkpoint(
                        browser=browser,
                        config=config,
                        checks=checks,
                        screenshots=screenshots,
                    )
                )
            finally:
                browser.close()
    except Exception as exc:
        checks.append(
            _check(
                "gate_completed_without_unhandled_error",
                False,
                error_type=type(exc).__name__,
                error_detail=_redact_string(
                    exc,
                    sensitive_values=sensitive_values,
                )[:800],
            )
        )
    finally:
        os.umask(old_umask)
        if config.private_har_path is not None:
            try:
                har_summary = finalize_private_har(config.private_har_path)
            except GateSafetyError as exc:
                checks.append(
                    _check(
                        "private_har_secure",
                        False,
                        error_code=str(exc),
                    )
                )
            else:
                checks.append(
                    _check(
                        "private_har_secure",
                        har_summary.get("mode") == "0600",
                        sha256=har_summary.get("sha256"),
                        bytes=har_summary.get("bytes"),
                        mode=har_summary.get("mode"),
                    )
                )
    receipt["artifacts"] = {
        "screenshots": screenshots,
        "private_har": har_summary,
    }
    return finalize_public_receipt(
        receipt,
        sensitive_values=sensitive_values,
    )


def finalize_public_receipt(
    receipt: Mapping[str, object],
    *,
    sensitive_values: Iterable[str] = (),
) -> dict[str, object]:
    copied = dict(receipt)
    checks = [
        dict(row)
        for row in list(copied.get("checks") or [])
        if isinstance(row, Mapping)
    ]
    failed_count = sum(1 for row in checks if row.get("ok") is not True)
    copied["check_count"] = len(checks)
    copied["failed_count"] = failed_count
    copied["status"] = "pass" if checks and failed_count == 0 else "fail"
    redacted = redact_public_receipt(
        copied,
        sensitive_values=sensitive_values,
    )
    if not isinstance(redacted, dict):
        raise GateSafetyError("public_receipt_redaction_failed")
    encoded = json.dumps(redacted, sort_keys=True)
    for secret in sensitive_values:
        if secret and secret in encoded:
            raise GateSafetyError("public_receipt_secret_leak")
    return redacted


def _default_artifact_root() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return (ROOT / "artifacts" / "propertyquarry-long-e2e" / stamp).resolve()


def main() -> int:
    artifact_root = _default_artifact_root()
    parser = argparse.ArgumentParser(
        description=(
            "Run the authenticated, bounded PropertyQuarry search E2E/soak gate "
            "with an internal-CI session receipt."
        )
    )
    parser.add_argument("--base-url", default=PRODUCTION_ORIGIN)
    parser.add_argument("--session-file", required=True)
    parser.add_argument("--mode", choices=("quick", "soak"), default="quick")
    parser.add_argument(
        "--iterations",
        type=int,
        default=QUICK_ITERATIONS,
        help="Quick requires exactly 2; soak accepts 2-6.",
    )
    parser.add_argument("--run-timeout-seconds", type=int, default=900)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--browser-timeout-ms", type=int, default=45_000)
    parser.add_argument(
        "--screenshots-dir",
        default=str(artifact_root / "screenshots"),
    )
    parser.add_argument(
        "--private-har",
        default="",
        help="Optional new absolute .har path; written with mode 0600.",
    )
    parser.add_argument(
        "--three-d-receipt",
        default="",
        help="Optional existing propertyquarry.3d_browser_gate.v1 checkpoint.",
    )
    parser.add_argument("--three-d-slug", default=DEFAULT_THREE_D_SLUG)
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="Required for the exact external production origin.",
    )
    parser.add_argument(
        "--confirm-search-side-effects",
        action="store_true",
        help="Acknowledge the bounded preference writes and provider searches.",
    )
    parser.add_argument(
        "--write",
        default=str(artifact_root / "receipt.json"),
        help="Absolute public receipt output path.",
    )
    args = parser.parse_args()
    try:
        config = validate_gate_config(
            origin=args.base_url,
            session_file=args.session_file,
            mode=args.mode,
            iterations=args.iterations,
            run_timeout_seconds=args.run_timeout_seconds,
            poll_seconds=args.poll_seconds,
            browser_timeout_ms=args.browser_timeout_ms,
            screenshots_dir=args.screenshots_dir,
            private_har_path=args.private_har,
            three_d_receipt_path=args.three_d_receipt,
            three_d_slug=args.three_d_slug,
            confirm_live=args.confirm_live,
            confirm_search_side_effects=args.confirm_search_side_effects,
        )
        receipt = build_long_running_e2e_receipt(config)
    except GateSafetyError as exc:
        receipt = finalize_public_receipt(
            {
                "contract_name": CONTRACT_NAME,
                "generated_at": utc_now(),
                "status": "fail",
                "checks": [
                    _check(
                        "configuration_and_session_safety",
                        False,
                        error_code=str(exc),
                    )
                ],
            }
        )
    output_path = _absolute_path(args.write, field="public_receipt_path")
    _write_json_atomic(output_path, receipt)
    print(
        json.dumps(
            {
                "contract_name": receipt["contract_name"],
                "status": receipt["status"],
                "check_count": receipt["check_count"],
                "failed_count": receipt["failed_count"],
            },
            sort_keys=True,
        )
    )
    return 0 if receipt["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
