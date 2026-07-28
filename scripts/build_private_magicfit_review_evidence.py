#!/usr/bin/env python3
"""Build private, token-gated MagicFit browser and evidence receipts.

This command is deliberately not an importer or an acceptance command.  It only
reviews an exact pending MagicFit bundle outside every configured public tour
root.  Subjective checklist claims must arrive in a separate operator-authored
visual-review receipt; this command never creates reviewer authority.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import json
import math
import os
import secrets
import shutil
import stat
import sys
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

# The authenticated release launcher enables PYTHONSAFEPATH, so direct script
# execution must opt into only this script's immutable sibling-module directory.
SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

try:
    from accept_magicfit_delivery import (
        BROWSER_RECEIPT_CONTRACT,
        DELIVERY_CONTRACT,
        EVIDENCE_CONTRACT,
        EVIDENCE_FIELDS,
        EVIDENCE_VIDEO_FIELDS,
        MAX_FUTURE_SKEW,
        PENDING_SIDECAR_FIELDS,
        REVIEW_CHECKS,
        _canonical_relpath,
        _load_json_bytes,
        _local_file,
        _sha256_bytes,
        _sha256_file,
        _source_receipt_valid,
        _strict_utc,
        _validate_browser_receipt,
        _validate_evidence,
        _valid_sha256,
        _video_probe,
    )
    from property_tour_host_safety import (
        TourHostSafetyError,
        bounded_env_int,
        bounded_lane_lock,
        require_bounded_file,
        require_free_disk,
        run_bounded_subprocess,
        tour_asset_max_bytes,
        tour_manifest_max_bytes,
    )
except ModuleNotFoundError:
    from scripts.accept_magicfit_delivery import (
        BROWSER_RECEIPT_CONTRACT,
        DELIVERY_CONTRACT,
        EVIDENCE_CONTRACT,
        EVIDENCE_FIELDS,
        EVIDENCE_VIDEO_FIELDS,
        MAX_FUTURE_SKEW,
        PENDING_SIDECAR_FIELDS,
        REVIEW_CHECKS,
        _canonical_relpath,
        _load_json_bytes,
        _local_file,
        _sha256_bytes,
        _sha256_file,
        _source_receipt_valid,
        _strict_utc,
        _validate_browser_receipt,
        _validate_evidence,
        _valid_sha256,
        _video_probe,
    )
    from scripts.property_tour_host_safety import (
        TourHostSafetyError,
        bounded_env_int,
        bounded_lane_lock,
        require_bounded_file,
        require_free_disk,
        run_bounded_subprocess,
        tour_asset_max_bytes,
        tour_manifest_max_bytes,
    )


VISUAL_REVIEW_CONTRACT = "propertyquarry.magicfit_private_visual_review.v1"
VISUAL_REVIEW_FIELDS = frozenset(
    {
        "schema",
        "status",
        "provider",
        "target_slug",
        "observed_at",
        "video_sha256",
        "checklist",
    }
)
REVIEW_PAGE_ROUTE = "/_private/magicfit-review"
DEFAULT_PUBLIC_TOUR_ROOT = Path("/data/public_property_tours")
TOKEN_MAX_BYTES = 512
WORKER_RESULT_MAX_BYTES = 64 * 1024
WORKER_MEMORY_MAX_BYTES = 1024 * 1024 * 1024
WORKER_SWAP_MAX_BYTES = 0
WORKER_TASKS_MAX = 128
WORKER_CPU_MAX_RATIO = 1.0


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _json_bytes(payload: dict[str, object]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _write_private_file_exclusive(path: Path, body: bytes) -> None:
    """Atomically create one mode-0600 file without replacing existing evidence."""

    temporary: Path | None = None
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise SystemExit("magicfit_private_review_output_exists") from exc
        path.chmod(0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_receipt_pair(
    *,
    browser_path: Path,
    browser_body: bytes,
    evidence_path: Path,
    evidence_body: bytes,
) -> None:
    browser_created = False
    try:
        _write_private_file_exclusive(browser_path, browser_body)
        browser_created = True
        _write_private_file_exclusive(evidence_path, evidence_body)
    except BaseException:
        if browser_created:
            browser_path.unlink(missing_ok=True)
        raise


def _path_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _forbidden_public_roots() -> tuple[Path, ...]:
    roots = {DEFAULT_PUBLIC_TOUR_ROOT.expanduser().resolve()}
    configured = str(os.getenv("EA_PUBLIC_TOUR_DIR") or "").strip()
    if configured:
        roots.add(Path(configured).expanduser().resolve())
    return tuple(sorted(roots, key=str))


def _require_private_path(path: Path, *, reason: str) -> None:
    if any(_path_within(path, root) for root in _forbidden_public_roots()):
        raise SystemExit(reason)


def _require_existing_regular(path: Path, *, reason: str, maximum_bytes: int) -> int:
    try:
        return require_bounded_file(
            path,
            reason_prefix=reason,
            maximum_bytes=maximum_bytes,
        )
    except TourHostSafetyError as exc:
        raise SystemExit(str(exc)) from exc


def _current_cgroup_path(
    *,
    proc_cgroup_path: Path = Path("/proc/self/cgroup"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> Path:
    try:
        rows = proc_cgroup_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SystemExit("magicfit_private_review_cgroup_unavailable") from exc
    relative = ""
    for row in rows:
        fields = row.split(":", 2)
        if len(fields) == 3 and fields[0] == "0" and fields[1] == "":
            relative = fields[2]
            break
    if not relative.startswith("/") or ".." in relative.split("/"):
        raise SystemExit("magicfit_private_review_cgroup_unavailable")
    root = cgroup_root.resolve()
    resolved = (root / relative.lstrip("/")).resolve()
    if resolved != root and root not in resolved.parents:
        raise SystemExit("magicfit_private_review_cgroup_unavailable")
    return resolved


def _finite_cgroup_limit(path: Path, *, error: str) -> int:
    try:
        raw = path.read_text(encoding="ascii").strip()
        value = int(raw)
    except (OSError, TypeError, ValueError) as exc:
        raise SystemExit(error) from exc
    if value < 0:
        raise SystemExit(error)
    return value


def _require_worker_cgroup_limits(
    *,
    proc_cgroup_path: Path = Path("/proc/self/cgroup"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> dict[str, object]:
    """Prove the browser worker is in a finite cgroup no looser than policy."""

    current = _current_cgroup_path(
        proc_cgroup_path=proc_cgroup_path,
        cgroup_root=cgroup_root,
    )
    memory_max = _finite_cgroup_limit(
        current / "memory.max",
        error="magicfit_private_review_cgroup_memory_uncapped",
    )
    swap_max = _finite_cgroup_limit(
        current / "memory.swap.max",
        error="magicfit_private_review_cgroup_swap_uncapped",
    )
    tasks_max = _finite_cgroup_limit(
        current / "pids.max",
        error="magicfit_private_review_cgroup_tasks_uncapped",
    )
    try:
        cpu_fields = (current / "cpu.max").read_text(encoding="ascii").split()
        quota = int(cpu_fields[0])
        period = int(cpu_fields[1])
    except (OSError, IndexError, TypeError, ValueError) as exc:
        raise SystemExit("magicfit_private_review_cgroup_cpu_uncapped") from exc
    if quota <= 0 or period <= 0:
        raise SystemExit("magicfit_private_review_cgroup_cpu_uncapped")
    cpu_ratio = quota / period
    if (
        memory_max > WORKER_MEMORY_MAX_BYTES
        or swap_max > WORKER_SWAP_MAX_BYTES
        or tasks_max > WORKER_TASKS_MAX
        or not math.isfinite(cpu_ratio)
        or cpu_ratio > WORKER_CPU_MAX_RATIO
    ):
        raise SystemExit("magicfit_private_review_cgroup_limits_too_loose")
    return {
        "memory_max_bytes": memory_max,
        "memory_swap_max_bytes": swap_max,
        "tasks_max": tasks_max,
        "cpu_quota_percent": cpu_ratio * 100.0,
    }


def _capped_worker_command(
    command: list[str],
    *,
    runtime_max_seconds: int,
    systemd_run_path: str | None = None,
    unit_suffix: str | None = None,
) -> list[str]:
    executable = str(systemd_run_path or shutil.which("systemd-run") or "").strip()
    if not executable or not Path(executable).is_file():
        raise SystemExit("magicfit_private_review_cgroup_runner_missing")
    bounded_runtime = max(30, min(int(runtime_max_seconds), 200))
    suffix = str(unit_suffix or secrets.token_hex(8)).strip().lower()
    if not suffix or any(character not in "0123456789abcdef" for character in suffix):
        raise SystemExit("magicfit_private_review_cgroup_unit_invalid")
    return [
        executable,
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        f"--unit=propertyquarry-magicfit-review-{suffix}",
        f"--property=MemoryMax={WORKER_MEMORY_MAX_BYTES}",
        f"--property=MemorySwapMax={WORKER_SWAP_MAX_BYTES}",
        f"--property=TasksMax={WORKER_TASKS_MAX}",
        "--property=CPUQuota=100%",
        f"--property=RuntimeMaxSec={bounded_runtime}s",
        "--",
        *command,
    ]


def _pending_bundle(
    *, bundle_dir: Path, slug: str, source_receipt_path: Path
) -> dict[str, object]:
    manifest_path = bundle_dir / "tour.json"
    sidecar_path = bundle_dir / "tour.magicfit.json"
    _require_existing_regular(
        manifest_path,
        reason="magicfit_private_review_manifest",
        maximum_bytes=tour_manifest_max_bytes(),
    )
    _require_existing_regular(
        sidecar_path,
        reason="magicfit_private_review_sidecar",
        maximum_bytes=tour_manifest_max_bytes(),
    )
    manifest, manifest_bytes = _load_json_bytes(
        manifest_path, error="magicfit_private_review_manifest_invalid"
    )
    if (
        manifest.get("slug") != slug
        or manifest.get("video_provider") != "magicfit"
        or manifest.get("video_provider_backend_key") != "magicfit"
        or manifest.get("video_sidecar_relpath") != "tour.magicfit.json"
    ):
        raise SystemExit("magicfit_private_review_manifest_binding_invalid")

    video_relpath = _canonical_relpath(manifest.get("video_relpath"))
    if not video_relpath:
        raise SystemExit("magicfit_private_review_manifest_binding_invalid")
    video_path = _local_file(bundle_dir, video_relpath)
    if video_path is None:
        raise SystemExit("magicfit_private_review_video_missing")
    _require_existing_regular(
        video_path,
        reason="magicfit_private_review_video",
        maximum_bytes=tour_asset_max_bytes(),
    )

    sidecar, sidecar_bytes = _load_json_bytes(
        sidecar_path, error="magicfit_private_review_pending_contract_invalid"
    )
    expected = {
        "contract_name": DELIVERY_CONTRACT,
        "provider": "magicfit",
        "provider_key": "magicfit",
        "provider_backend_key": "magicfit",
        "render_status": "completed",
        "status": "rendered_pending_delivery_acceptance",
        "acceptance_status": "pending",
        "launch_eligible": False,
    }
    if set(sidecar) != PENDING_SIDECAR_FIELDS or any(
        sidecar.get(key) != value for key, value in expected.items()
    ):
        raise SystemExit("magicfit_private_review_pending_contract_invalid")
    if sidecar.get("video_relpath") != video_relpath:
        raise SystemExit("magicfit_private_review_pending_video_mismatch")

    generated_at = _strict_utc(sidecar.get("generated_at"), require_z=False)
    video_sha256 = _valid_sha256(sidecar.get("video_sha256"))
    source_receipt_sha256 = _valid_sha256(sidecar.get("source_receipt_sha256"))
    now = datetime.now(timezone.utc)
    if (
        generated_at is None
        or generated_at > now + MAX_FUTURE_SKEW
        or not video_sha256
        or not source_receipt_sha256
    ):
        raise SystemExit("magicfit_private_review_pending_contract_invalid")
    if _sha256_file(video_path) != video_sha256:
        raise SystemExit("magicfit_private_review_video_digest_mismatch")

    receipt_limit = bounded_env_int(
        "PROPERTYQUARRY_MAGICFIT_RECEIPT_MAX_BYTES",
        default=1024 * 1024,
        minimum=1_024,
        maximum=8 * 1024 * 1024,
    )
    _require_existing_regular(
        source_receipt_path,
        reason="magicfit_private_review_source_receipt",
        maximum_bytes=receipt_limit,
    )
    source_receipt, source_bytes = _load_json_bytes(
        source_receipt_path,
        error="magicfit_private_review_source_receipt_invalid",
    )
    if (
        _sha256_bytes(source_bytes) != source_receipt_sha256
        or not _source_receipt_valid(source_receipt, slug=slug)
    ):
        raise SystemExit("magicfit_private_review_source_receipt_mismatch")

    probe = _video_probe(video_path)
    duration = float(probe["duration_seconds"])
    size_bytes = int(probe["size_bytes"])
    if not math.isfinite(duration) or duration <= 0.0 or size_bytes <= 0:
        raise SystemExit("magicfit_private_review_video_probe_invalid")
    return {
        "generated_at": generated_at,
        "manifest_path": manifest_path,
        "manifest_sha256": _sha256_bytes(manifest_bytes),
        "sidecar_path": sidecar_path,
        "sidecar_sha256": _sha256_bytes(sidecar_bytes),
        "source_receipt_path": source_receipt_path,
        "source_receipt_sha256": source_receipt_sha256,
        "video_path": video_path,
        "video_sha256": video_sha256,
        "video_duration": duration,
        "video_size_bytes": size_bytes,
    }


def _visual_review(
    *,
    path: Path,
    slug: str,
    generated_at: datetime,
    video_sha256: str,
) -> dict[str, bool]:
    _require_existing_regular(
        path,
        reason="magicfit_private_visual_review",
        maximum_bytes=64 * 1024,
    )
    payload, _body = _load_json_bytes(
        path, error="magicfit_private_visual_review_invalid"
    )
    if (
        set(payload) != VISUAL_REVIEW_FIELDS
        or payload.get("schema") != VISUAL_REVIEW_CONTRACT
        or payload.get("status") != "pass"
        or payload.get("provider") != "magicfit"
        or payload.get("target_slug") != slug
        or payload.get("video_sha256") != video_sha256
    ):
        raise SystemExit("magicfit_private_visual_review_contract_invalid")
    observed_at = _strict_utc(payload.get("observed_at"), require_z=True)
    now = datetime.now(timezone.utc)
    if (
        observed_at is None
        or observed_at < generated_at
        or observed_at > now + MAX_FUTURE_SKEW
    ):
        raise SystemExit("magicfit_private_visual_review_timestamp_invalid")
    checklist = payload.get("checklist")
    if not isinstance(checklist, dict) or set(checklist) != REVIEW_CHECKS:
        raise SystemExit("magicfit_private_visual_review_checklist_invalid")
    if not all(checklist.get(key) is True for key in REVIEW_CHECKS):
        raise SystemExit("magicfit_private_visual_review_checklist_failed")
    return {key: True for key in sorted(REVIEW_CHECKS)}


class _PrivateReviewServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False


def _review_handler(
    *, video_path: Path, route: str, token_path: Path
) -> type[BaseHTTPRequestHandler]:
    content_type = {
        ".mp4": "video/mp4",
        ".m4v": "video/mp4",
        ".mov": "video/quicktime",
        ".webm": "video/webm",
    }.get(video_path.suffix.lower(), "application/octet-stream")
    page_body = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Private walkthrough review</title></head>"
        "<body><video id='tour-video' controls muted preload='auto' "
        f"src='{html.escape(route, quote=True)}' "
        "style='width:100%;height:auto'></video></body></html>"
    ).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        server_version = ""
        sys_version = ""

        def log_message(self, _format: str, *args: object) -> None:
            return

        def _authorized(self) -> bool:
            host = str(self.headers.get("Host") or "")
            if not (host == "127.0.0.1" or host.startswith("127.0.0.1:")):
                return False
            try:
                details = token_path.stat(follow_symlinks=False)
                if (
                    not stat.S_ISREG(details.st_mode)
                    or details.st_size <= 0
                    or details.st_size > TOKEN_MAX_BYTES
                    or stat.S_IMODE(details.st_mode) != 0o600
                ):
                    return False
                token = token_path.read_text(encoding="ascii")
            except (OSError, UnicodeError):
                return False
            supplied = str(self.headers.get("Authorization") or "")
            return hmac.compare_digest(supplied, f"Bearer {token}")

        def _headers(self, *, status_code: int, length: int, media: bool) -> None:
            self.send_response(status_code)
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Type", content_type if media else "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            if not media:
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'none'; media-src 'self'; style-src 'unsafe-inline'",
                )
            self.end_headers()

        def _dispatch(self, *, head_only: bool) -> None:
            if not self._authorized():
                self.send_error(404)
                return
            parsed = urlsplit(self.path)
            if parsed.query or parsed.fragment:
                self.send_error(404)
                return
            if parsed.path == REVIEW_PAGE_ROUTE:
                self._headers(status_code=200, length=len(page_body), media=False)
                if not head_only:
                    self.wfile.write(page_body)
                return
            if parsed.path != route:
                self.send_error(404)
                return
            try:
                size = int(video_path.stat(follow_symlinks=False).st_size)
            except OSError:
                self.send_error(404)
                return
            # Deliberately serve one bounded full response.  The proof route has
            # no seek UI, and a stable 200 is the public acceptance contract.
            self._headers(status_code=200, length=size, media=True)
            if head_only:
                return
            try:
                with video_path.open("rb") as handle:
                    while True:
                        chunk = handle.read(1024 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                return

        def do_HEAD(self) -> None:  # noqa: N802
            self._dispatch(head_only=True)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch(head_only=False)

    return Handler


@contextmanager
def _private_review_server(
    *, video_path: Path, route: str, token_path: Path
) -> Iterator[str]:
    server = _PrivateReviewServer(
        ("127.0.0.1", 0),
        _review_handler(video_path=video_path, route=route, token_path=token_path),
    )
    address, port = server.server_address[:2]
    if address != "127.0.0.1":
        server.server_close()
        raise SystemExit("magicfit_private_review_loopback_binding_failed")
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.05},
        name="magicfit-private-review-loopback",
        daemon=True,
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}{REVIEW_PAGE_ROUTE}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def _safe_browser_text(value: object) -> str:
    text = " ".join(str(value or "").split())
    return text[:160]


def _worker_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != REVIEW_PAGE_ROUTE
        or parsed.query
        or parsed.fragment
    ):
        raise SystemExit("magicfit_private_review_worker_url_invalid")
    return value


def _playback_worker(args: argparse.Namespace) -> int:
    _require_worker_cgroup_limits()
    slug = _canonical_relpath(args.slug)
    if not slug or "/" in slug:
        raise SystemExit("magicfit_private_review_worker_slug_invalid")
    route = f"/tours/{slug}/walkthrough"
    review_url = _worker_url(args.review_url)
    token_path = Path(args.token_file).expanduser().resolve()
    result_path = Path(args.result_file).expanduser().resolve()
    if token_path.parent != result_path.parent or result_path.exists():
        raise SystemExit("magicfit_private_review_worker_runtime_invalid")
    _require_existing_regular(
        token_path,
        reason="magicfit_private_review_worker_token",
        maximum_bytes=TOKEN_MAX_BYTES,
    )
    if stat.S_IMODE(token_path.stat(follow_symlinks=False).st_mode) != 0o600:
        raise SystemExit("magicfit_private_review_worker_token_mode_invalid")
    token = token_path.read_text(encoding="ascii")
    if not token or len(token.encode("ascii")) > TOKEN_MAX_BYTES:
        raise SystemExit("magicfit_private_review_worker_token_invalid")
    timeout_ms = max(10_000, min(int(args.timeout_ms), 180_000))

    try:
        from playwright.sync_api import sync_playwright

        try:
            from propertyquarry_playwright_runtime import playwright_engine_launch_browser
        except ModuleNotFoundError:
            from scripts.propertyquarry_playwright_runtime import (
                playwright_engine_launch_browser,
            )
    except Exception as exc:
        raise SystemExit(
            f"magicfit_private_review_playwright_unavailable:{type(exc).__name__}"
        ) from exc

    console_errors: list[str] = []
    request_failures: list[dict[str, object]] = []
    benign_request_aborts: list[dict[str, object]] = []
    bad_responses: list[dict[str, object]] = []
    route_statuses: list[int] = []

    expected_benign = {
        "failure": "net::ERR_ABORTED",
        "method": "GET",
        "resource_type": "media",
        "route": route,
    }

    with sync_playwright() as playwright:
        browser = playwright_engine_launch_browser(
            playwright,
            engine="chromium",
            args=[
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-extensions",
                "--disable-sync",
                "--no-first-run",
                "--js-flags=--max-old-space-size=256",
            ],
        )
        try:
            context = browser.new_context(
                extra_http_headers={"Authorization": f"Bearer {token}"},
                service_workers="block",
            )
            page = context.new_page()
            page.set_default_timeout(timeout_ms)
            page.on(
                "console",
                lambda message: console_errors.append(_safe_browser_text(message.text))
                if message.type == "error" and len(console_errors) < 20
                else None,
            )

            def request_failed(request: Any) -> None:
                failure = _safe_browser_text(request.failure)
                row = {
                    "failure": failure,
                    "method": str(request.method or ""),
                    "resource_type": str(request.resource_type or ""),
                    "route": urlsplit(str(request.url or "")).path,
                }
                if row == expected_benign:
                    if row not in benign_request_aborts:
                        benign_request_aborts.append(row)
                elif len(request_failures) < 20:
                    request_failures.append(row)

            def response_seen(response: Any) -> None:
                response_route = urlsplit(str(response.url or "")).path
                status_code = int(response.status)
                if response_route == route:
                    route_statuses.append(status_code)
                if status_code >= 400 and len(bad_responses) < 20:
                    bad_responses.append(
                        {"route": response_route, "status": status_code}
                    )

            page.on("requestfailed", request_failed)
            page.on("response", response_seen)
            page.goto(review_url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.locator("#tour-video").wait_for(state="attached", timeout=timeout_ms)
            metrics = page.evaluate(
                """async ({ timeoutMs }) => {
                    const video = document.getElementById('tour-video');
                    if (!(video instanceof HTMLVideoElement)) {
                        return { duration: 0, currentTime: 0, ended: false, error: 'video_missing' };
                    }
                    video.muted = true;
                    const metadata = await Promise.race([
                        new Promise((resolve) => {
                            if (video.readyState >= 1) return resolve('ready');
                            video.addEventListener('loadedmetadata', () => resolve('ready'), { once: true });
                            video.addEventListener('error', () => resolve('error'), { once: true });
                        }),
                        new Promise((resolve) => window.setTimeout(() => resolve('timeout'), timeoutMs)),
                    ]);
                    if (metadata !== 'ready') {
                        return {
                            duration: Number(video.duration || 0),
                            currentTime: Number(video.currentTime || 0),
                            ended: false,
                            error: metadata === 'timeout' ? 'metadata_timeout' : `media_${Number(video.error?.code || 0)}`,
                        };
                    }
                    let playError = null;
                    const terminal = new Promise((resolve) => {
                        video.addEventListener('ended', () => resolve('ended'), { once: true });
                        video.addEventListener('error', () => resolve('error'), { once: true });
                    });
                    try {
                        await video.play();
                    } catch (error) {
                        playError = String(error?.name || 'play_failed');
                    }
                    const outcome = playError || await Promise.race([
                        terminal,
                        new Promise((resolve) => window.setTimeout(() => resolve('playback_timeout'), timeoutMs)),
                    ]);
                    return {
                        duration: Number(video.duration || 0),
                        currentTime: Number(video.currentTime || 0),
                        ended: Boolean(video.ended && outcome === 'ended'),
                        error: outcome === 'ended' ? null : outcome === 'error' ? `media_${Number(video.error?.code || 0)}` : String(outcome),
                    };
                }""",
                {"timeoutMs": timeout_ms},
            )
            page.wait_for_timeout(100)
            context.close()
        finally:
            browser.close()

    duration = float(metrics.get("duration") or 0.0)
    final_current_time = float(metrics.get("currentTime") or 0.0)
    finite_metrics = math.isfinite(duration) and math.isfinite(final_current_time)
    playback_to_end = bool(metrics.get("ended")) and finite_metrics
    video_error = _safe_browser_text(metrics.get("error")) or None
    http_status = route_statuses[0] if route_statuses else 0
    status = "pass" if (
        playback_to_end
        and video_error is None
        and http_status == 200
        and not console_errors
        and not request_failures
        and not bad_responses
        and duration > 0.0
        and final_current_time >= duration - 0.25
    ) else "fail"
    payload: dict[str, object] = {
        "schema": BROWSER_RECEIPT_CONTRACT,
        "status": status,
        "provider": "magicfit",
        "target_slug": slug,
        "observed_at": _utc_now(),
        "route": route,
        "http_status": http_status,
        "video_sha256": args.video_sha256,
        "duration_seconds": duration if finite_metrics else 0.0,
        "final_current_time": final_current_time if finite_metrics else 0.0,
        "playback_to_end": playback_to_end,
        "video_error": video_error,
        "console_errors": console_errors,
        "request_failures": request_failures,
        "benign_request_aborts": benign_request_aborts,
        "bad_responses": bad_responses,
    }
    _write_private_file_exclusive(result_path, _json_bytes(payload))
    return 0 if status == "pass" else 2


def _run_browser_playback(
    *,
    slug: str,
    video_path: Path,
    video_sha256: str,
    runtime_dir: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    token_path = runtime_dir / "access-token"
    result_path = runtime_dir / "browser-result.json"
    token = secrets.token_urlsafe(48)
    descriptor = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(token)
            handle.flush()
            os.fsync(handle.fileno())
        token_path.chmod(0o600)
        route = f"/tours/{slug}/walkthrough"
        with _private_review_server(
            video_path=video_path,
            route=route,
            token_path=token_path,
        ) as review_url:
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--_playback-worker",
                "--slug",
                slug,
                "--review-url",
                review_url,
                "--token-file",
                str(token_path),
                "--result-file",
                str(result_path),
                "--video-sha256",
                video_sha256,
                "--timeout-ms",
                str(timeout_seconds * 1000),
            ]
            capped_command = _capped_worker_command(
                command,
                runtime_max_seconds=timeout_seconds + 20,
            )
            result = run_bounded_subprocess(
                capped_command,
                cwd=Path(__file__).resolve().parents[1],
                env=dict(os.environ),
                timeout_seconds=min(timeout_seconds + 20, 200),
                maximum_output_bytes=64 * 1024,
            )
        if result.returncode != 0:
            raise SystemExit("magicfit_private_review_browser_failed")
        _require_existing_regular(
            result_path,
            reason="magicfit_private_review_browser_result",
            maximum_bytes=WORKER_RESULT_MAX_BYTES,
        )
        payload, _body = _load_json_bytes(
            result_path, error="magicfit_private_review_browser_result_invalid"
        )
        return payload
    finally:
        token_path.unlink(missing_ok=True)
        result_path.unlink(missing_ok=True)


def _main(args: argparse.Namespace) -> int:
    if not args.allow_private_review:
        raise SystemExit("magicfit_private_review_not_authorized")
    slug = _canonical_relpath(args.slug)
    if not slug or "/" in slug:
        raise SystemExit("magicfit_private_review_slug_invalid")

    raw_bundle = Path(args.bundle_dir).expanduser()
    try:
        raw_details = raw_bundle.stat(follow_symlinks=False)
    except OSError as exc:
        raise SystemExit("magicfit_private_review_bundle_missing") from exc
    if stat.S_ISLNK(raw_details.st_mode) or not stat.S_ISDIR(raw_details.st_mode):
        raise SystemExit("magicfit_private_review_bundle_invalid")
    bundle_dir = raw_bundle.resolve()
    if bundle_dir.name != slug:
        raise SystemExit("magicfit_private_review_bundle_slug_mismatch")
    _require_private_path(
        bundle_dir, reason="magicfit_private_review_public_root_forbidden"
    )

    browser_only = bool(args.browser_only)
    browser_path = Path(args.browser_receipt_out).expanduser().resolve()
    evidence_value = str(args.evidence_receipt_out or "").strip()
    if browser_only and evidence_value:
        evidence_candidate = Path(evidence_value).expanduser().resolve()
        if browser_path == evidence_candidate:
            raise SystemExit("magicfit_private_review_output_collision")
        raise SystemExit("magicfit_private_review_browser_only_pair_arguments_forbidden")
    if browser_only and (
        str(args.contact_sheet or "").strip()
        or str(args.visual_review or "").strip()
    ):
        raise SystemExit("magicfit_private_review_browser_only_pair_arguments_forbidden")
    evidence_path = (
        None if browser_only else Path(evidence_value).expanduser().resolve()
    )
    if evidence_path is not None and browser_path == evidence_path:
        raise SystemExit("magicfit_private_review_output_collision")
    output_paths = (browser_path,) if evidence_path is None else (browser_path, evidence_path)
    for path in output_paths:
        _require_private_path(
            path, reason="magicfit_private_review_public_output_forbidden"
        )
        if not path.parent.is_dir() or path.parent.is_symlink():
            raise SystemExit("magicfit_private_review_output_parent_invalid")
        if path.exists() or path.is_symlink():
            raise SystemExit("magicfit_private_review_output_exists")

    source_receipt = Path(args.source_receipt).expanduser().resolve()
    pending = _pending_bundle(
        bundle_dir=bundle_dir,
        slug=slug,
        source_receipt_path=source_receipt,
    )
    contact_sheet: Path | None = None
    visual_review: Path | None = None
    contact_sheet_sha256 = ""
    visual_review_sha256 = ""
    checklist: dict[str, bool] = {}
    if not browser_only:
        contact_sheet = Path(args.contact_sheet).expanduser().resolve()
        visual_review = Path(args.visual_review).expanduser().resolve()
        contact_limit = bounded_env_int(
            "PROPERTYQUARRY_MAGICFIT_CONTACT_SHEET_MAX_BYTES",
            default=32 * 1024 * 1024,
            minimum=1_024,
            maximum=128 * 1024 * 1024,
        )
        _require_existing_regular(
            contact_sheet,
            reason="magicfit_private_review_contact_sheet",
            maximum_bytes=contact_limit,
        )
        try:
            image_header = contact_sheet.read_bytes()[:12]
        except OSError as exc:
            raise SystemExit("magicfit_private_review_contact_sheet_invalid") from exc
        if not (
            image_header.startswith(b"\x89PNG\r\n\x1a\n")
            or image_header.startswith(b"\xff\xd8\xff")
        ):
            raise SystemExit("magicfit_private_review_contact_sheet_invalid")
        contact_sheet_sha256 = _sha256_file(contact_sheet)
        visual_review_sha256 = _sha256_file(visual_review)
        checklist = _visual_review(
            path=visual_review,
            slug=slug,
            generated_at=pending["generated_at"],  # type: ignore[arg-type]
            video_sha256=str(pending["video_sha256"]),
        )
    timeout_seconds = max(15, min(int(args.timeout_seconds), 180))
    try:
        require_free_disk(
            browser_path.parent,
            reason_prefix="magicfit_private_review",
            expected_write_bytes=128 * 1024,
        )
    except TourHostSafetyError as exc:
        raise SystemExit(str(exc)) from exc

    with tempfile.TemporaryDirectory(
        prefix=".magicfit-private-review-", dir=browser_path.parent
    ) as runtime_name:
        runtime_dir = Path(runtime_name)
        runtime_dir.chmod(0o700)
        browser_payload = _run_browser_playback(
            slug=slug,
            video_path=pending["video_path"],  # type: ignore[arg-type]
            video_sha256=str(pending["video_sha256"]),
            runtime_dir=runtime_dir,
            timeout_seconds=timeout_seconds,
        )

    unchanged_files: list[tuple[Path, object]] = [
        (pending["manifest_path"], pending["manifest_sha256"]),
        (pending["sidecar_path"], pending["sidecar_sha256"]),
        (pending["source_receipt_path"], pending["source_receipt_sha256"]),
        (pending["video_path"], pending["video_sha256"]),
    ]  # type: ignore[list-item]
    if contact_sheet is not None and visual_review is not None:
        unchanged_files.extend(
            (
                (contact_sheet, contact_sheet_sha256),
                (visual_review, visual_review_sha256),
            )
        )
    try:
        changed = any(
            _sha256_file(path) != expected
            for path, expected in unchanged_files
        )
    except OSError as exc:
        raise SystemExit("magicfit_private_review_input_changed") from exc
    if changed:
        raise SystemExit("magicfit_private_review_input_changed")

    _validate_browser_receipt(
        browser_payload,
        slug=slug,
        generated_at=pending["generated_at"],  # type: ignore[arg-type]
        video_sha256=str(pending["video_sha256"]),
        video_duration=float(pending["video_duration"]),
    )
    browser_body = _json_bytes(browser_payload)
    common_result: dict[str, object] = {
        "provider": "magicfit",
        "target_slug": slug,
        "proof_transport": "ephemeral_token_gated_loopback_review_server",
        "public_route_proof": False,
        "acceptance_status": "pending",
        "launch_eligible": False,
        "reviewer_authority_generated": False,
        "published": False,
        "execution_boundary": {
            "kind": "transient_user_systemd_scope",
            "memory_max_bytes": WORKER_MEMORY_MAX_BYTES,
            "memory_swap_max_bytes": WORKER_SWAP_MAX_BYTES,
            "tasks_max": WORKER_TASKS_MAX,
            "cpu_quota_percent": WORKER_CPU_MAX_RATIO * 100.0,
            "runtime_max_seconds": timeout_seconds + 20,
        },
        "browser_receipt_sha256": hashlib.sha256(browser_body).hexdigest(),
    }
    if browser_only:
        _write_private_file_exclusive(browser_path, browser_body)
        print(
            json.dumps(
                {
                    "status": "private_browser_playback_ready",
                    "proof_scope": "private_technical_playback_only",
                    **common_result,
                },
                ensure_ascii=False,
            )
        )
        return 0

    if contact_sheet is None or evidence_path is None:
        raise SystemExit("magicfit_private_review_internal_contract_invalid")
    evidence_payload: dict[str, object] = {
        "schema": EVIDENCE_CONTRACT,
        "status": "pass",
        "provider": "magicfit",
        "target_slug": slug,
        "observed_at": _utc_now(),
        "source_receipt_sha256": str(pending["source_receipt_sha256"]),
        "video": {
            "sha256": str(pending["video_sha256"]),
            "size_bytes": int(pending["video_size_bytes"]),
            "duration_seconds": float(pending["video_duration"]),
        },
        "checklist": checklist,
        "artifacts": {
            "contact_sheet_sha256": contact_sheet_sha256,
            "browser_receipt_sha256": hashlib.sha256(browser_body).hexdigest(),
        },
    }
    if set(evidence_payload) != EVIDENCE_FIELDS or set(
        evidence_payload["video"]  # type: ignore[arg-type]
    ) != EVIDENCE_VIDEO_FIELDS:
        raise SystemExit("magicfit_private_review_internal_contract_invalid")
    _validate_evidence(
        evidence_payload,
        slug=slug,
        generated_at=pending["generated_at"],  # type: ignore[arg-type]
        video_sha256=str(pending["video_sha256"]),
        video_probe={
            "size_bytes": int(pending["video_size_bytes"]),
            "duration_seconds": float(pending["video_duration"]),
        },
        source_receipt_sha256=str(pending["source_receipt_sha256"]),
        contact_sheet_sha256=contact_sheet_sha256,
        browser_receipt_sha256=hashlib.sha256(browser_body).hexdigest(),
    )
    evidence_body = _json_bytes(evidence_payload)
    _write_receipt_pair(
        browser_path=browser_path,
        browser_body=browser_body,
        evidence_path=evidence_path,
        evidence_body=evidence_body,
    )
    print(
        json.dumps(
            {
                "status": "private_review_evidence_ready",
                "proof_scope": "private_technical_and_operator_review_evidence",
                **common_result,
                "evidence_receipt_sha256": hashlib.sha256(evidence_body).hexdigest(),
            },
            ensure_ascii=False,
        )
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build private MagicFit playback/evidence receipts without accepting or "
            "publishing the pending delivery."
        )
    )
    parser.add_argument("--allow-private-review", action="store_true")
    parser.add_argument(
        "--browser-only",
        action="store_true",
        help=(
            "Write only private technical browser playback proof; do not require or "
            "write visual evidence."
        ),
    )
    parser.add_argument("--slug")
    parser.add_argument("--bundle-dir")
    parser.add_argument("--source-receipt")
    parser.add_argument("--contact-sheet")
    parser.add_argument("--visual-review")
    parser.add_argument("--browser-receipt-out")
    parser.add_argument("--evidence-receipt-out")
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument("--_playback-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--review-url", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--token-file", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--result-file", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--video-sha256", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--timeout-ms", type=int, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args._playback_worker:
        return _playback_worker(args)
    required = [
        "slug",
        "bundle_dir",
        "source_receipt",
        "browser_receipt_out",
    ]
    if not args.browser_only:
        required.extend(("contact_sheet", "visual_review", "evidence_receipt_out"))
    missing = [name for name in required if not str(getattr(args, name) or "").strip()]
    if missing:
        raise SystemExit(f"magicfit_private_review_argument_missing:{missing[0]}")
    try:
        with bounded_lane_lock("magicfit-private-review"):
            return _main(args)
    except TourHostSafetyError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
