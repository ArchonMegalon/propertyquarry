#!/usr/bin/env python3
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath


EA_RUNTIME_ROOT = Path(__file__).resolve().parents[1] / "ea"
if str(EA_RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(EA_RUNTIME_ROOT))
SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from app.services.admission_control import (  # noqa: E402
    AdmissionBackend,
    AdmissionBackendUnavailable,
    ConcurrencyDimension,
    QuotaCharge,
    build_admission_backend,
)
from app.observability import (  # noqa: E402
    bind_runtime_trace_context,
    bounded_correlation_id,
    new_server_trace_context,
    outbound_observability_headers,
)
from scripts.property_tour_governed_reservation import (  # noqa: E402
    require_dynamic_tour_slug,
)

try:
    from scripts.property_reconstruction_styles import reconstruction_style
except ModuleNotFoundError:
    from property_reconstruction_styles import reconstruction_style  # type: ignore[no-redef]


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8091
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30
MIN_REQUEST_TIMEOUT_SECONDS = 1
MAX_REQUEST_TIMEOUT_SECONDS = 300
DEFAULT_MAX_GENERATION_SECONDS = 1_800
MIN_MAX_GENERATION_SECONDS = 120
MAX_MAX_GENERATION_SECONDS = 7_200
PROCESS_CLOSE_MARGIN_SECONDS = 30
_GENERATED_BUNDLE_SLUG_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_GENERATED_BUNDLE_COMMIT_MARKER = ".propertyquarry-render-commit.json"
_GENERATED_BUNDLE_STAGE_PREFIX = ".propertyquarry-stage-"
_GENERATION_SLUG_LOCKS_GUARD = threading.Lock()
_GENERATION_SLUG_LOCKS: dict[str, dict[str, object]] = {}
_PLAYWRIGHT_BROWSERS_PATH = "/ms-playwright"
_PLAYWRIGHT_EXECUTION_CONTROL_ENV = {
    "PLAYWRIGHT_NODEJS_PATH",
    "_PLAYWRIGHT_DRIVER_CLI_PATH",
    "_PLAYWRIGHT_DRIVER_EXECUTABLE_PATH",
}


class _GeneratedBundlePublishError(RuntimeError):
    """Stable, path-free failure raised while publishing a generated bundle."""


def _valid_generated_bundle_slug(value: object) -> str:
    normalized = str(value or "").strip()
    if (
        not _GENERATED_BUNDLE_SLUG_PATTERN.fullmatch(normalized)
        or ".." in normalized
        or normalized.startswith(_GENERATED_BUNDLE_STAGE_PREFIX)
    ):
        return ""
    try:
        require_dynamic_tour_slug(normalized)
    except RuntimeError:
        return ""
    return normalized


@contextmanager
def _serialized_generation_slug(slug: str):
    normalized_slug = _valid_generated_bundle_slug(slug)
    if not normalized_slug:
        raise ValueError("slug_invalid")
    with _GENERATION_SLUG_LOCKS_GUARD:
        entry = _GENERATION_SLUG_LOCKS.get(normalized_slug)
        if entry is None:
            entry = {"lock": threading.Lock(), "users": 0}
            _GENERATION_SLUG_LOCKS[normalized_slug] = entry
        entry["users"] = int(entry.get("users") or 0) + 1
        slug_lock = entry["lock"]
    if not isinstance(slug_lock, type(threading.Lock())):
        raise RuntimeError("generation_slug_lock_invalid")
    slug_lock.acquire()
    try:
        yield
    finally:
        slug_lock.release()
        with _GENERATION_SLUG_LOCKS_GUARD:
            entry["users"] = max(0, int(entry.get("users") or 0) - 1)
            if (
                int(entry.get("users") or 0) == 0
                and _GENERATION_SLUG_LOCKS.get(normalized_slug) is entry
            ):
                _GENERATION_SLUG_LOCKS.pop(normalized_slug, None)


def _generated_bundle_publish_failure(detail: str) -> dict[str, object]:
    normalized_detail = str(detail or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{1,160}", normalized_detail):
        normalized_detail = "generated_bundle_publish_failed"
    return {
        "status": "failed",
        "reason": "generated_bundle_publish_failed",
        "detail": normalized_detail,
    }


def _stable_generator_reason(value: object) -> str:
    normalized = str(value or "generator_reported_non_generated_status").strip().lower()
    if re.fullmatch(r"[a-z0-9_:-]{1,160}", normalized):
        return normalized
    return "generator_reported_non_generated_status"


def _stable_validation_reason(value: object) -> str:
    normalized = str(value or "request_validation_failed").strip().lower()
    if re.fullmatch(r"[a-z0-9_]{1,160}", normalized):
        return normalized
    return "request_validation_failed"


def _generator_environment(*, transaction_id: str) -> dict[str, str]:
    environment = dict(os.environ)
    observability_headers = outbound_observability_headers()
    if observability_headers.get("traceparent"):
        environment["PROPERTYQUARRY_TRACEPARENT"] = observability_headers["traceparent"]
    if observability_headers.get("x-correlation-id"):
        environment["PROPERTYQUARRY_CORRELATION_ID"] = observability_headers[
            "x-correlation-id"
        ]
    for variable in tuple(environment):
        if (
            variable.startswith("LD_")
            or variable.startswith("NODE_")
            or variable in {"GCONV_PATH", "GLIBC_TUNABLES"}
            or variable in _PLAYWRIGHT_EXECUTION_CONTROL_ENV
        ):
            environment.pop(variable, None)
    environment.pop("PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN", None)
    environment["EA_PUBLIC_TOUR_DIR"] = str(_public_tour_dir())
    environment["PLAYWRIGHT_BROWSERS_PATH"] = _PLAYWRIGHT_BROWSERS_PATH
    environment["PROPERTYQUARRY_RECONSTRUCTION_TRANSACTION_ID"] = transaction_id
    return environment


def _safe_public_relpath(value: str) -> str | None:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized.encode("utf-8", errors="replace")) > 500
        or normalized.startswith("/")
        or "\\" in normalized
        or ":" in normalized
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        return None
    relpath = PurePosixPath(normalized)
    if relpath.is_absolute() or not relpath.parts:
        return None
    if any(
        part in {"", ".", ".."}
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", part)
        for part in relpath.parts
    ):
        return None
    return relpath.as_posix()


def _safe_generator_success_result(result: dict[str, object]) -> dict[str, object]:
    safe: dict[str, object] = {}
    scalar_keys = (
        "slug",
        "provider",
        "viewer_relpath",
        "model_relpath",
        "diorama_preview_relpath",
        "telegram_preview_relpath",
        "public_tour_url",
        "satisfies_verified_tour_gate",
        "walkthrough_status",
        "verified_provider_capture",
        "runtime_publish_required",
        "publication_durability",
        "replaced_bundle_cleanup",
    )
    for key in scalar_keys:
        value = result.get(key)
        if isinstance(value, bool):
            safe[key] = value
            continue
        if not isinstance(value, str):
            continue
        normalized = value.strip()
        if len(normalized) > 500 or "\\" in normalized or "\x00" in normalized:
            continue
        if key.endswith("_relpath"):
            safe_relpath = _safe_public_relpath(normalized)
            if safe_relpath is None:
                continue
            relpath_parts = PurePosixPath(safe_relpath).parts
            if not (
                relpath_parts[0] == "generated-reconstruction"
                or safe_relpath
                in {"diorama-preview.png", "telegram-preview.png"}
            ):
                continue
            normalized = safe_relpath
        elif key == "public_tour_url":
            if normalized:
                continue
        elif not re.fullmatch(r"[A-Za-z0-9._:-]{0,160}", normalized):
            continue
        safe[key] = normalized
    runtime_publish = result.get("runtime_publish")
    if isinstance(runtime_publish, dict):
        safe["runtime_publish"] = {
            key: value
            for key, value in runtime_publish.items()
            if key in {"status", "slug"}
            and isinstance(value, str)
            and re.fullmatch(r"[A-Za-z0-9._-]{1,160}", value.strip())
        }
    return safe


def _required_container_stop_grace_seconds(
    *,
    max_generation_seconds: int,
    request_timeout_seconds: int,
) -> int:
    return max_generation_seconds + request_timeout_seconds + PROCESS_CLOSE_MARGIN_SECONDS


DEFAULT_CONTAINER_STOP_GRACE_SECONDS = _required_container_stop_grace_seconds(
    max_generation_seconds=DEFAULT_MAX_GENERATION_SECONDS,
    request_timeout_seconds=DEFAULT_REQUEST_TIMEOUT_SECONDS,
)
MIN_CONTAINER_STOP_GRACE_SECONDS = _required_container_stop_grace_seconds(
    max_generation_seconds=MIN_MAX_GENERATION_SECONDS,
    request_timeout_seconds=MIN_REQUEST_TIMEOUT_SECONDS,
)
MAX_CONTAINER_STOP_GRACE_SECONDS = _required_container_stop_grace_seconds(
    max_generation_seconds=MAX_MAX_GENERATION_SECONDS,
    request_timeout_seconds=MAX_REQUEST_TIMEOUT_SECONDS,
)


@dataclass(frozen=True)
class BridgeConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    auth_token: str = ""
    dev_mode: bool = False
    max_body_bytes: int = 131_072
    request_timeout_seconds: int = DEFAULT_REQUEST_TIMEOUT_SECONDS
    max_concurrency: int = 1
    rate_limit_requests: int = 12
    rate_limit_window_seconds: int = 60
    max_photo_count: int = 64
    max_route_labels: int = 64
    max_room_count: int = 64
    max_source_bytes: int = 1_073_741_824
    max_generation_seconds: int = DEFAULT_MAX_GENERATION_SECONDS
    max_walkthrough_seconds_per_stop: float = 30.0
    container_stop_grace_seconds: int = DEFAULT_CONTAINER_STOP_GRACE_SECONDS

    @property
    def shutdown_grace_seconds(self) -> int:
        return self.container_stop_grace_seconds - PROCESS_CLOSE_MARGIN_SECONDS


def _env_bool(name: str, *, default: bool = False) -> bool:
    raw = str(os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name.lower()}_invalid")


def _env_int(name: str, *, default: int, minimum: int, maximum: int) -> int:
    raw = str(os.getenv(name) or "").strip()
    try:
        parsed = int(raw or str(default))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name.lower()}_invalid") from exc
    if parsed < minimum or parsed > maximum:
        raise RuntimeError(f"{name.lower()}_out_of_range")
    return parsed


def _env_float(name: str, *, default: float, minimum: float, maximum: float) -> float:
    raw = str(os.getenv(name) or "").strip()
    try:
        parsed = float(raw or str(default))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name.lower()}_invalid") from exc
    if parsed < minimum or parsed > maximum:
        raise RuntimeError(f"{name.lower()}_out_of_range")
    return parsed


def _is_loopback_host(host: str) -> bool:
    normalized = str(host or "").strip().lower().strip("[]")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _load_bridge_config() -> BridgeConfig:
    request_timeout_seconds = _env_int(
        "PROPERTYQUARRY_RECONSTRUCTION_RENDER_REQUEST_TIMEOUT_SECONDS",
        default=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        minimum=MIN_REQUEST_TIMEOUT_SECONDS,
        maximum=MAX_REQUEST_TIMEOUT_SECONDS,
    )
    max_generation_seconds = _env_int(
        "PROPERTYQUARRY_RECONSTRUCTION_RENDER_MAX_GENERATION_SECONDS",
        default=DEFAULT_MAX_GENERATION_SECONDS,
        minimum=MIN_MAX_GENERATION_SECONDS,
        maximum=MAX_MAX_GENERATION_SECONDS,
    )
    return BridgeConfig(
        host=str(os.getenv("PROPERTYQUARRY_RECONSTRUCTION_RENDER_HOST") or DEFAULT_HOST).strip() or DEFAULT_HOST,
        port=_env_int(
            "PROPERTYQUARRY_RECONSTRUCTION_RENDER_PORT",
            default=DEFAULT_PORT,
            minimum=1,
            maximum=65_535,
        ),
        auth_token=_bridge_token(),
        dev_mode=_env_bool("PROPERTYQUARRY_RECONSTRUCTION_RENDER_DEV_MODE"),
        max_body_bytes=_env_int(
            "PROPERTYQUARRY_RECONSTRUCTION_RENDER_MAX_BODY_BYTES",
            default=131_072,
            minimum=1_024,
            maximum=16_777_216,
        ),
        request_timeout_seconds=request_timeout_seconds,
        max_concurrency=_env_int(
            "PROPERTYQUARRY_RECONSTRUCTION_RENDER_MAX_CONCURRENCY",
            default=1,
            minimum=1,
            maximum=16,
        ),
        rate_limit_requests=_env_int(
            "PROPERTYQUARRY_RECONSTRUCTION_RENDER_RATE_LIMIT_REQUESTS",
            default=12,
            minimum=1,
            maximum=10_000,
        ),
        rate_limit_window_seconds=_env_int(
            "PROPERTYQUARRY_RECONSTRUCTION_RENDER_RATE_LIMIT_WINDOW_SECONDS",
            default=60,
            minimum=1,
            maximum=3_600,
        ),
        max_photo_count=_env_int(
            "PROPERTYQUARRY_RECONSTRUCTION_RENDER_MAX_PHOTOS",
            default=64,
            minimum=1,
            maximum=1_000,
        ),
        max_route_labels=_env_int(
            "PROPERTYQUARRY_RECONSTRUCTION_RENDER_MAX_ROUTE_LABELS",
            default=64,
            minimum=1,
            maximum=1_000,
        ),
        max_room_count=_env_int(
            "PROPERTYQUARRY_RECONSTRUCTION_RENDER_MAX_ROOMS",
            default=64,
            minimum=1,
            maximum=1_000,
        ),
        max_source_bytes=_env_int(
            "PROPERTYQUARRY_RECONSTRUCTION_RENDER_MAX_SOURCE_BYTES",
            default=1_073_741_824,
            minimum=1_048_576,
            maximum=1_099_511_627_776,
        ),
        max_generation_seconds=max_generation_seconds,
        max_walkthrough_seconds_per_stop=_env_float(
            "PROPERTYQUARRY_RECONSTRUCTION_RENDER_MAX_WALKTHROUGH_SECONDS_PER_STOP",
            default=30.0,
            minimum=1.0,
            maximum=300.0,
        ),
        container_stop_grace_seconds=_env_int(
            "PROPERTYQUARRY_RENDER_STOP_GRACE_SECONDS",
            default=DEFAULT_CONTAINER_STOP_GRACE_SECONDS,
            minimum=MIN_CONTAINER_STOP_GRACE_SECONDS,
            maximum=MAX_CONTAINER_STOP_GRACE_SECONDS,
        ),
    )


def _validate_bridge_config(config: BridgeConfig) -> None:
    loopback = _is_loopback_host(config.host)
    has_token = bool(str(config.auth_token or "").strip())
    if config.dev_mode and not loopback:
        raise RuntimeError("property_reconstruction_render_dev_mode_requires_loopback")
    if not has_token and not (config.dev_mode and loopback):
        raise RuntimeError("property_reconstruction_render_bridge_token_required")
    if config.port < 0 or config.port > 65_535:
        raise RuntimeError("property_reconstruction_render_port_out_of_range")
    if min(
        config.max_body_bytes,
        config.request_timeout_seconds,
        config.max_concurrency,
        config.rate_limit_requests,
        config.rate_limit_window_seconds,
        config.max_photo_count,
        config.max_route_labels,
        config.max_room_count,
        config.max_source_bytes,
        config.max_generation_seconds,
        config.max_walkthrough_seconds_per_stop,
        config.container_stop_grace_seconds,
    ) <= 0:
        raise RuntimeError("property_reconstruction_render_limit_out_of_range")
    minimum_container_stop_grace_seconds = _required_container_stop_grace_seconds(
        max_generation_seconds=config.max_generation_seconds,
        request_timeout_seconds=config.request_timeout_seconds,
    )
    if config.container_stop_grace_seconds < minimum_container_stop_grace_seconds:
        raise RuntimeError("property_reconstruction_render_container_stop_grace_insufficient")


def _bridge_admission_backend(config: BridgeConfig) -> AdmissionBackend:
    return build_admission_backend(
        runtime_mode="dev" if config.dev_mode else "prod",
        database_url=str(os.getenv("DATABASE_URL") or "").strip(),
    )


def _public_tour_dir() -> Path:
    return Path(str(os.getenv("EA_PUBLIC_TOUR_DIR") or "/data/public_property_tours")).expanduser().resolve()


def _generated_bundle_target(slug: object, *, require_exists: bool) -> Path:
    normalized_slug = _valid_generated_bundle_slug(slug)
    if not normalized_slug:
        raise _GeneratedBundlePublishError("generated_bundle_slug_invalid")
    root = _public_tour_dir()
    try:
        root_stat = root.stat(follow_symlinks=False)
    except OSError as exc:
        raise _GeneratedBundlePublishError("generated_bundle_root_invalid") from exc
    if not stat.S_ISDIR(root_stat.st_mode):
        raise _GeneratedBundlePublishError("generated_bundle_root_invalid")
    bundle_dir = root / normalized_slug
    if bundle_dir.parent != root:
        raise _GeneratedBundlePublishError("generated_bundle_target_invalid")
    try:
        bundle_stat = bundle_dir.stat(follow_symlinks=False)
    except FileNotFoundError:
        if require_exists:
            raise _GeneratedBundlePublishError("generated_bundle_missing")
        return bundle_dir
    except OSError as exc:
        raise _GeneratedBundlePublishError("generated_bundle_target_invalid") from exc
    if stat.S_ISLNK(bundle_stat.st_mode):
        raise _GeneratedBundlePublishError("generated_bundle_symlink_forbidden")
    if not stat.S_ISDIR(bundle_stat.st_mode):
        raise _GeneratedBundlePublishError("generated_bundle_target_invalid")
    return bundle_dir


def _open_generated_bundle_entry(
    name: str,
    *,
    directory_fd: int,
    expected_stat: os.stat_result,
    directory: bool,
) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        opened_stat = os.fstat(descriptor)
        expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
        if (
            not expected_kind(opened_stat.st_mode)
            or opened_stat.st_dev != expected_stat.st_dev
            or opened_stat.st_ino != expected_stat.st_ino
        ):
            raise _GeneratedBundlePublishError("generated_bundle_entry_changed")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _require_generated_bundle_mode(descriptor: int, expected_mode: int) -> None:
    current_mode = stat.S_IMODE(os.fstat(descriptor).st_mode)
    if current_mode != expected_mode:
        raise _GeneratedBundlePublishError("generated_bundle_permissions_invalid")


def _validate_generated_bundle_tree(
    directory_fd: int,
    *,
    require_public_modes: bool,
) -> None:
    if require_public_modes:
        _require_generated_bundle_mode(directory_fd, 0o755)
    try:
        names = sorted(os.listdir(directory_fd))
    except OSError as exc:
        raise _GeneratedBundlePublishError("generated_reconstruction_directory_invalid") from exc
    for name in names:
        try:
            entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise _GeneratedBundlePublishError("generated_reconstruction_asset_invalid") from exc
        if stat.S_ISLNK(entry_stat.st_mode):
            raise _GeneratedBundlePublishError("generated_reconstruction_asset_symlink_forbidden")
        if stat.S_ISDIR(entry_stat.st_mode):
            try:
                child_fd = _open_generated_bundle_entry(
                    name,
                    directory_fd=directory_fd,
                    expected_stat=entry_stat,
                    directory=True,
                )
            except OSError as exc:
                raise _GeneratedBundlePublishError("generated_reconstruction_asset_invalid") from exc
            try:
                _validate_generated_bundle_tree(
                    child_fd,
                    require_public_modes=require_public_modes,
                )
            finally:
                os.close(child_fd)
            continue
        if not stat.S_ISREG(entry_stat.st_mode):
            raise _GeneratedBundlePublishError("generated_reconstruction_asset_invalid")
        try:
            asset_fd = _open_generated_bundle_entry(
                name,
                directory_fd=directory_fd,
                expected_stat=entry_stat,
                directory=False,
            )
        except OSError as exc:
            raise _GeneratedBundlePublishError("generated_reconstruction_asset_invalid") from exc
        try:
            if require_public_modes:
                _require_generated_bundle_mode(asset_fd, 0o644)
        finally:
            os.close(asset_fd)


def _validate_generated_bundle_publication(slug: object) -> None:
    bundle_dir = _generated_bundle_target(slug, require_exists=True)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        bundle_fd = os.open(bundle_dir, flags | getattr(os, "O_DIRECTORY", 0))
    except OSError as exc:
        raise _GeneratedBundlePublishError("generated_bundle_target_invalid") from exc
    manifest_fd = -1
    reconstruction_fd = -1
    try:
        _require_generated_bundle_mode(bundle_fd, 0o755)
        _validate_generated_bundle_tree(
            bundle_fd,
            require_public_modes=False,
        )
        try:
            manifest_stat = os.stat("tour.json", dir_fd=bundle_fd, follow_symlinks=False)
        except OSError as exc:
            raise _GeneratedBundlePublishError("generated_bundle_manifest_invalid") from exc
        if stat.S_ISLNK(manifest_stat.st_mode) or not stat.S_ISREG(manifest_stat.st_mode):
            raise _GeneratedBundlePublishError("generated_bundle_manifest_invalid")
        try:
            manifest_fd = _open_generated_bundle_entry(
                "tour.json",
                directory_fd=bundle_fd,
                expected_stat=manifest_stat,
                directory=False,
            )
        except OSError as exc:
            raise _GeneratedBundlePublishError("generated_bundle_manifest_invalid") from exc
        _require_generated_bundle_mode(manifest_fd, 0o644)

        try:
            reconstruction_stat = os.stat(
                "generated-reconstruction",
                dir_fd=bundle_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise _GeneratedBundlePublishError("generated_reconstruction_directory_invalid") from exc
        if stat.S_ISLNK(reconstruction_stat.st_mode) or not stat.S_ISDIR(reconstruction_stat.st_mode):
            raise _GeneratedBundlePublishError("generated_reconstruction_directory_invalid")
        try:
            reconstruction_fd = _open_generated_bundle_entry(
                "generated-reconstruction",
                directory_fd=bundle_fd,
                expected_stat=reconstruction_stat,
                directory=True,
            )
        except OSError as exc:
            raise _GeneratedBundlePublishError("generated_reconstruction_directory_invalid") from exc
        _validate_generated_bundle_tree(
            reconstruction_fd,
            require_public_modes=True,
        )
        for preview_name in ("diorama-preview.png", "telegram-preview.png"):
            try:
                preview_stat = os.stat(
                    preview_name,
                    dir_fd=bundle_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise _GeneratedBundlePublishError(
                    "generated_bundle_preview_invalid"
                ) from exc
            if not stat.S_ISREG(preview_stat.st_mode):
                raise _GeneratedBundlePublishError(
                    "generated_bundle_preview_invalid"
                )
            preview_fd = _open_generated_bundle_entry(
                preview_name,
                directory_fd=bundle_fd,
                expected_stat=preview_stat,
                directory=False,
            )
            try:
                _require_generated_bundle_mode(preview_fd, 0o644)
            finally:
                os.close(preview_fd)
    finally:
        if reconstruction_fd >= 0:
            os.close(reconstruction_fd)
        if manifest_fd >= 0:
            os.close(manifest_fd)
        os.close(bundle_fd)


def _read_bounded_json_descriptor(
    descriptor: int,
    *,
    maximum_bytes: int,
) -> tuple[bytes, dict[str, object]]:
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_size < 2
        or opened.st_size > maximum_bytes
    ):
        raise _GeneratedBundlePublishError("generated_bundle_json_invalid")
    payload = bytearray()
    while len(payload) <= maximum_bytes:
        chunk = os.read(
            descriptor,
            min(1024 * 1024, maximum_bytes + 1 - len(payload)),
        )
        if not chunk:
            break
        payload.extend(chunk)
    final = os.fstat(descriptor)
    if (
        len(payload) != opened.st_size
        or len(payload) > maximum_bytes
        or final.st_dev != opened.st_dev
        or final.st_ino != opened.st_ino
        or final.st_size != opened.st_size
        or final.st_mtime_ns != opened.st_mtime_ns
        or final.st_ctime_ns != opened.st_ctime_ns
    ):
        raise _GeneratedBundlePublishError("generated_bundle_json_changed")
    decoded = json.loads(bytes(payload).decode("utf-8"))
    if not isinstance(decoded, dict):
        raise _GeneratedBundlePublishError("generated_bundle_json_invalid")
    return bytes(payload), decoded


def _probe_committed_transaction(
    *,
    slug: str,
    transaction_id: str,
) -> dict[str, object] | None:
    root_fd = -1
    bundle_fd = -1
    marker_fd = -1
    manifest_fd = -1
    reconstruction_fd = -1
    receipt_fd = -1
    try:
        normalized_slug = _valid_generated_bundle_slug(slug)
        if not normalized_slug or not re.fullmatch(r"[a-f0-9]{32}", transaction_id):
            return None
        _generated_bundle_target(normalized_slug, require_exists=True)
        root_fd = os.open(
            _public_tour_dir(),
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        expected_bundle = os.stat(
            normalized_slug,
            dir_fd=root_fd,
            follow_symlinks=False,
        )
        bundle_fd = _open_generated_bundle_entry(
            normalized_slug,
            directory_fd=root_fd,
            expected_stat=expected_bundle,
            directory=True,
        )
        marker_stat = os.stat(
            _GENERATED_BUNDLE_COMMIT_MARKER,
            dir_fd=bundle_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(marker_stat.st_mode)
            or stat.S_IMODE(marker_stat.st_mode) != 0o600
        ):
            return None
        marker_fd = _open_generated_bundle_entry(
            _GENERATED_BUNDLE_COMMIT_MARKER,
            directory_fd=bundle_fd,
            expected_stat=marker_stat,
            directory=False,
        )
        _marker_bytes, marker = _read_bounded_json_descriptor(
            marker_fd,
            maximum_bytes=4096,
        )
        manifest_hash = str(marker.get("tour_manifest_sha256") or "")
        if not re.fullmatch(r"[a-f0-9]{64}", manifest_hash):
            return None
        if marker != {
            "schema": "propertyquarry.render_bundle_commit.v1",
            "slug": normalized_slug,
            "tour_manifest_sha256": manifest_hash,
            "transaction_id": transaction_id,
        }:
            return None
        manifest_stat = os.stat("tour.json", dir_fd=bundle_fd, follow_symlinks=False)
        manifest_fd = _open_generated_bundle_entry(
            "tour.json",
            directory_fd=bundle_fd,
            expected_stat=manifest_stat,
            directory=False,
        )
        manifest_bytes, manifest = _read_bounded_json_descriptor(
            manifest_fd,
            maximum_bytes=8 * 1024 * 1024,
        )
        if (
            hashlib.sha256(manifest_bytes).hexdigest() != manifest_hash
            or str(manifest.get("slug") or "").strip() != normalized_slug
        ):
            return None
        reconstruction_stat = os.stat(
            "generated-reconstruction",
            dir_fd=bundle_fd,
            follow_symlinks=False,
        )
        reconstruction_fd = _open_generated_bundle_entry(
            "generated-reconstruction",
            directory_fd=bundle_fd,
            expected_stat=reconstruction_stat,
            directory=True,
        )
        receipt_stat = os.stat(
            "reconstruction.json",
            dir_fd=reconstruction_fd,
            follow_symlinks=False,
        )
        receipt_fd = _open_generated_bundle_entry(
            "reconstruction.json",
            directory_fd=reconstruction_fd,
            expected_stat=receipt_stat,
            directory=False,
        )
        _receipt_bytes, receipt = _read_bounded_json_descriptor(
            receipt_fd,
            maximum_bytes=8 * 1024 * 1024,
        )
        if str(receipt.get("slug") or "").strip() != normalized_slug:
            return None
        runtime_publish_required = receipt.get("runtime_publish_required")
        runtime_publish_ok = receipt.get("runtime_publish_ok")
        runtime_publish = receipt.get("runtime_publish")
        if (
            not isinstance(runtime_publish_required, bool)
            or not isinstance(runtime_publish_ok, bool)
            or not isinstance(runtime_publish, dict)
        ):
            return None
        runtime_publish_status = str(runtime_publish.get("status") or "").strip()
        if not re.fullmatch(r"[a-z0-9_]{1,160}", runtime_publish_status):
            return None
        runtime_publication_proven = (
            runtime_publish_required is False
            or (
                runtime_publish_ok is True
                and runtime_publish_status
                in {"updated", "skipped_shared_public_root"}
            )
        )
        _validate_generated_bundle_publication(normalized_slug)
        final_bundle = os.stat(
            normalized_slug,
            dir_fd=root_fd,
            follow_symlinks=False,
        )
        opened_bundle = os.fstat(bundle_fd)
        if (
            final_bundle.st_dev != opened_bundle.st_dev
            or final_bundle.st_ino != opened_bundle.st_ino
        ):
            return None
        os.fsync(root_fd)
    except (
        OSError,
        UnicodeDecodeError,
        ValueError,
        RecursionError,
        json.JSONDecodeError,
        _GeneratedBundlePublishError,
    ):
        return None
    finally:
        for descriptor in (
            receipt_fd,
            reconstruction_fd,
            manifest_fd,
            marker_fd,
            bundle_fd,
            root_fd,
        ):
            if descriptor >= 0:
                os.close(descriptor)
    return {
        "slug": normalized_slug,
        "transaction_id_bound": True,
        "publication_durability": "fsynced_by_bridge_recovery",
        "runtime_publish_required": runtime_publish_required,
        "runtime_publish_ok": runtime_publish_ok,
        "runtime_publish_status": runtime_publish_status,
        "runtime_publication_proven": runtime_publication_proven,
    }


def _recovered_timeout_result(recovery: dict[str, object]) -> dict[str, object]:
    runtime_fields = {
        "runtime_publish_required": bool(
            recovery.get("runtime_publish_required")
        ),
        "runtime_publish_ok": bool(recovery.get("runtime_publish_ok")),
        "runtime_publish_status": str(
            recovery.get("runtime_publish_status") or "unverified"
        ),
        "runtime_publication_proven": bool(
            recovery.get("runtime_publication_proven")
        ),
    }
    if not runtime_fields["runtime_publication_proven"]:
        return {
            "status": "failed",
            "reason": "runtime_publication_unverified_after_timeout",
            "slug": str(recovery.get("slug") or ""),
            "local_commit_applied": True,
            "generator_completion_observed": False,
            "transaction_id_bound": bool(recovery.get("transaction_id_bound")),
            "publication_durability": str(
                recovery.get("publication_durability") or "unverified"
            ),
            "replaced_bundle_cleanup": "unverified_after_generator_exit",
            **runtime_fields,
        }
    return {
        "status": "generated",
        "result": {
            "slug": str(recovery.get("slug") or ""),
            "local_commit_applied": True,
            "generator_completion_observed": False,
            "recovery_state": "local_commit_recovered_after_generator_timeout",
            "transaction_id_bound": bool(recovery.get("transaction_id_bound")),
            "publication_durability": str(
                recovery.get("publication_durability") or "unverified"
            ),
            "replaced_bundle_cleanup": "unverified_after_generator_exit",
            **runtime_fields,
        },
    }


def _postcommit_failure(
    *,
    reason: str,
    recovery: dict[str, object],
    detail: str | None = None,
    returncode: int | None = None,
    diagnostic: bytes | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "status": "failed",
        "reason": reason,
        "slug": str(recovery.get("slug") or ""),
        "local_commit_applied": True,
        "transaction_id_bound": bool(recovery.get("transaction_id_bound")),
        "publication_durability": str(
            recovery.get("publication_durability") or "unverified"
        ),
        "replaced_bundle_cleanup": "unverified_after_generator_exit",
        "runtime_publish_required": bool(
            recovery.get("runtime_publish_required")
        ),
        "runtime_publish_ok": bool(recovery.get("runtime_publish_ok")),
        "runtime_publish_status": str(
            recovery.get("runtime_publish_status") or "unverified"
        ),
        "runtime_publication_proven": bool(
            recovery.get("runtime_publication_proven")
        ),
    }
    if detail is not None:
        result["detail"] = _stable_generator_reason(detail)
    if returncode is not None:
        result["returncode"] = int(returncode)
    if diagnostic is not None:
        result["diagnostic_sha256"] = hashlib.sha256(diagnostic).hexdigest()
        result["diagnostic_size_bytes"] = len(diagnostic)
    return result


def _script_path() -> Path:
    return Path("/app/scripts/generate_property_reconstruction.py").resolve()


def _bridge_token() -> str:
    return str(os.getenv("PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN") or "").strip()


def _generation_timeout_seconds(raw_value: object = "", *, maximum: int = 1_800) -> int:
    requested_value = str(raw_value or "").strip()
    raw_value = str(os.getenv("PROPERTYQUARRY_RECONSTRUCTION_TIMEOUT_SECONDS") or "").strip()
    try:
        parsed = int(float(requested_value or raw_value or "420"))
    except Exception:
        parsed = 420
    return min(max(parsed, 120), max(120, int(maximum)))


def _safe_shared_file(raw_path: object, *, root: Path) -> Path:
    candidate = Path(str(raw_path or "")).expanduser().resolve()
    if root != candidate and root not in candidate.parents:
        raise ValueError("path_outside_public_tour_dir")
    if not candidate.is_file():
        raise ValueError("shared_input_missing")
    return candidate


def _validate_generation_cost(payload: dict[str, object], *, config: BridgeConfig) -> None:
    slug = str(payload.get("slug") or "").strip()
    if not slug:
        raise ValueError("slug_missing")
    if not _valid_generated_bundle_slug(slug):
        raise ValueError("slug_invalid")
    if "skip_video" in payload and not isinstance(payload.get("skip_video"), bool):
        raise ValueError("skip_video_invalid")

    photo_paths = payload.get("photo_paths")
    if photo_paths is not None and not isinstance(photo_paths, list):
        raise ValueError("photo_paths_invalid")
    normalized_photos = list(photo_paths or []) if isinstance(photo_paths, list) else []
    if len(normalized_photos) > config.max_photo_count:
        raise ValueError("photo_count_exceeds_limit")

    route_labels = payload.get("route_labels")
    if route_labels is not None and not isinstance(route_labels, list):
        raise ValueError("route_labels_invalid")
    normalized_labels = list(route_labels or []) if isinstance(route_labels, list) else []
    if len(normalized_labels) > config.max_route_labels:
        raise ValueError("route_label_count_exceeds_limit")
    if any(len(str(label or "").strip()) > 160 for label in normalized_labels):
        raise ValueError("route_label_too_long")

    style_label = str(payload.get("style_label") or "").strip()
    if len(style_label) > 512:
        raise ValueError("style_label_too_long")
    try:
        selected_style = reconstruction_style(
            style_label,
            style_id=payload.get("style_id"),
        )
    except ValueError as exc:
        raise ValueError("style_unsupported") from exc
    supplied_style_signature = str(payload.get("style_signature") or "").strip()
    if supplied_style_signature and supplied_style_signature != str(selected_style.get("signature") or ""):
        raise ValueError("style_signature_mismatch")
    try:
        room_count = int(payload.get("room_count") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("room_count_invalid") from exc
    if room_count < 0:
        raise ValueError("room_count_invalid")
    if room_count > config.max_room_count:
        raise ValueError("room_count_exceeds_limit")

    requested_timeout = payload.get("timeout_seconds")
    if requested_timeout is not None and requested_timeout != "":
        try:
            timeout_seconds = float(requested_timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError("timeout_seconds_invalid") from exc
        if (
            not math.isfinite(timeout_seconds)
            or timeout_seconds < 1
            or timeout_seconds > config.max_generation_seconds
        ):
            raise ValueError("timeout_seconds_exceeds_limit")

    requested_walkthrough = payload.get("walkthrough_seconds_per_stop")
    if requested_walkthrough is not None and requested_walkthrough != "":
        try:
            walkthrough_seconds = float(requested_walkthrough)
        except (TypeError, ValueError) as exc:
            raise ValueError("walkthrough_seconds_per_stop_invalid") from exc
        if (
            not math.isfinite(walkthrough_seconds)
            or walkthrough_seconds < 0
            or walkthrough_seconds > config.max_walkthrough_seconds_per_stop
        ):
            raise ValueError("walkthrough_seconds_per_stop_exceeds_limit")

    root = _public_tour_dir()
    source_paths: list[object] = list(normalized_photos)
    if str(payload.get("floorplan_path") or "").strip():
        source_paths.append(payload.get("floorplan_path") or "")
    source_bytes = 0
    seen: set[Path] = set()
    for raw_path in source_paths:
        if len(str(raw_path or "")) > 4_096:
            raise ValueError("source_path_too_long")
        resolved = _safe_shared_file(raw_path, root=root)
        if resolved in seen:
            continue
        seen.add(resolved)
        source_bytes += resolved.stat().st_size
        if source_bytes > config.max_source_bytes:
            raise ValueError("source_bytes_exceed_limit")


def _bridge_readiness(
    config: BridgeConfig,
    *,
    draining: bool = False,
    admission_backend: AdmissionBackend | None = None,
) -> tuple[bool, dict[str, object]]:
    try:
        _validate_bridge_config(config)
        backend = admission_backend or _bridge_admission_backend(config)
        backend.probe()
    except AdmissionBackendUnavailable:
        return False, {
            "status": "not_ready",
            "reason": "admission_backend_unavailable",
            "bridge": "property_reconstruction_render_bridge",
            "accepting_requests": False,
            "provider_readiness_claimed": False,
        }
    except Exception as exc:
        return False, {
            "status": "not_ready",
            "reason": "bridge_configuration_invalid",
            "detail": type(exc).__name__,
        }
    script_path = _script_path()
    public_tour_dir = _public_tour_dir()
    script_ready = script_path.is_file() and os.access(script_path, os.R_OK)
    storage_ready = (
        public_tour_dir.is_dir()
        and os.access(public_tour_dir, os.R_OK)
        and os.access(public_tour_dir, os.W_OK)
        and os.access(public_tour_dir, os.X_OK)
    )
    payload: dict[str, object] = {
        "status": "ready" if script_ready and storage_ready and not draining else "not_ready",
        "bridge": "property_reconstruction_render_bridge",
        "script_ready": script_ready,
        "storage_ready": storage_ready,
        "security_mode": "loopback_dev" if config.dev_mode else "authenticated",
        "accepting_requests": not draining,
        "provider_readiness_claimed": False,
    }
    if draining:
        payload["reason"] = "bridge_draining"
    elif not script_ready:
        payload["reason"] = "generator_script_unavailable"
    elif not storage_ready:
        payload["reason"] = "public_tour_storage_unavailable"
    else:
        payload["reason"] = "bridge_ready"
    return bool(script_ready and storage_ready and not draining), payload


def _build_generator_command(payload: dict[str, object]) -> list[str]:
    slug = str(payload.get("slug") or "").strip()
    if not slug:
        raise ValueError("slug_missing")
    script_path = _script_path()
    if not script_path.is_file():
        raise ValueError("generator_script_missing")
    root = _public_tour_dir()
    command = [sys.executable, str(script_path), "--slug", slug]
    if bool(payload.get("skip_video")):
        command.append("--skip-video")
    floorplan_path = str(payload.get("floorplan_path") or "").strip()
    if floorplan_path:
        command.extend(["--floorplan", str(_safe_shared_file(floorplan_path, root=root))])
    else:
        command.append("--infer-floorplan-from-photos")
    photo_paths = payload.get("photo_paths")
    if not isinstance(photo_paths, list):
        photo_paths = []
    for photo_path in photo_paths:
        command.extend(["--photo", str(_safe_shared_file(photo_path, root=root))])
    style_label = str(payload.get("style_label") or "").strip()
    selected_style = reconstruction_style(
        style_label,
        style_id=payload.get("style_id"),
    )
    if style_label:
        command.extend(["--style-label", style_label])
    command.extend(
        [
            "--style-id",
            str(selected_style.get("id") or ""),
            "--style-signature",
            str(selected_style.get("signature") or ""),
        ]
    )
    room_count = max(0, int(payload.get("room_count") or 0))
    if room_count > 0:
        command.extend(["--room-count", str(room_count)])
    route_labels = payload.get("route_labels")
    if isinstance(route_labels, list):
        for route_label in route_labels:
            normalized_label = str(route_label or "").strip()
            if normalized_label:
                command.extend(["--room-label", normalized_label])
    return command


def _run_generator_process(
    command: list[str],
    *,
    timeout_seconds: int,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        cwd="/app",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(
            command,
            timeout_seconds,
            output=stdout,
            stderr=stderr,
        ) from exc
    except Exception:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.communicate()
        except Exception:
            pass
        raise RuntimeError("generator_process_communication_failed") from None
    return subprocess.CompletedProcess(
        command,
        int(process.returncode or 0),
        stdout=stdout,
        stderr=stderr,
    )


def _run_generation_request_locked(
    payload: dict[str, object],
    *,
    runtime_config: BridgeConfig,
) -> dict[str, object]:
    try:
        _generated_bundle_target(payload.get("slug"), require_exists=False)
    except _GeneratedBundlePublishError as exc:
        return _generated_bundle_publish_failure(str(exc))
    except Exception:
        return _generated_bundle_publish_failure("generated_bundle_target_invalid")
    command = _build_generator_command(payload)
    timeout_seconds = _generation_timeout_seconds(
        payload.get("timeout_seconds"),
        maximum=runtime_config.max_generation_seconds,
    )
    slug = _valid_generated_bundle_slug(payload.get("slug"))
    transaction_id = secrets.token_hex(16)
    env = _generator_environment(transaction_id=transaction_id)
    # The generator is the authoritative snapshot boundary. Propagate the
    # request's configured aggregate source cap so its fd-bound copy enforces
    # the same limit after all bridge validation races have closed.
    env["PROPERTYQUARRY_RECONSTRUCTION_MAX_SOURCE_BYTES"] = str(
        runtime_config.max_source_bytes
    )
    try:
        walkthrough_seconds_per_stop = float(payload.get("walkthrough_seconds_per_stop") or 0.0)
    except Exception:
        walkthrough_seconds_per_stop = 0.0
    if walkthrough_seconds_per_stop > 0.0:
        env["PROPERTYQUARRY_RECONSTRUCTION_WALKTHROUGH_SECONDS_PER_STOP"] = str(walkthrough_seconds_per_stop)
    try:
        completed = _run_generator_process(
            command,
            timeout_seconds=timeout_seconds,
            env=env,
        )
    except subprocess.TimeoutExpired:
        recovery = _probe_committed_transaction(
            slug=slug,
            transaction_id=transaction_id,
        )
        if recovery is not None:
            return _recovered_timeout_result(recovery)
        return {
            "status": "failed",
            "reason": "generator_timeout",
            "timeout_seconds": timeout_seconds,
        }
    if completed.returncode != 0:
        diagnostic = str(completed.stderr or completed.stdout or "").encode(
            "utf-8",
            errors="replace",
        )
        recovery = _probe_committed_transaction(
            slug=slug,
            transaction_id=transaction_id,
        )
        if recovery is not None:
            return _postcommit_failure(
                reason="generator_exit_nonzero_after_local_commit",
                recovery=recovery,
                returncode=int(completed.returncode),
                diagnostic=diagnostic,
            )
        return {
            "status": "failed",
            "reason": "generator_exit_nonzero",
            "returncode": int(completed.returncode),
            "diagnostic_sha256": hashlib.sha256(diagnostic).hexdigest(),
            "diagnostic_size_bytes": len(diagnostic),
        }
    raw_stdout = str(completed.stdout or "").strip()
    try:
        result = json.loads(raw_stdout or "{}")
    except Exception:
        result = None
    if not isinstance(result, dict):
        recovery = _probe_committed_transaction(
            slug=slug,
            transaction_id=transaction_id,
        )
        if recovery is not None:
            return _postcommit_failure(
                reason="generator_unparseable_after_local_commit",
                recovery=recovery,
                detail="generator_result_not_object",
            )
        return {
            "status": "failed",
            "reason": "generator_unparseable",
            "detail": "generator_result_not_object",
        }
    if str(result.get("status") or "").strip() != "generated":
        recovery = _probe_committed_transaction(
            slug=slug,
            transaction_id=transaction_id,
        )
        if recovery is not None:
            return _postcommit_failure(
                reason="generator_reported_failure_after_local_commit",
                recovery=recovery,
                detail=str(
                    result.get("reason")
                    or result.get("status")
                    or "generator_reported_non_generated_status"
                ),
            )
        return {
            "status": "failed",
            "reason": "generator_reported_failure",
            "detail": _stable_generator_reason(
                result.get("reason")
                or result.get("status")
                or "generator_reported_non_generated_status"
            ),
        }
    try:
        _validate_generated_bundle_publication(payload.get("slug"))
    except PermissionError:
        return _generated_bundle_publish_failure("generated_bundle_permissions_denied")
    except _GeneratedBundlePublishError as exc:
        return _generated_bundle_publish_failure(str(exc))
    except OSError:
        return _generated_bundle_publish_failure("generated_bundle_permissions_failed")
    except Exception:
        return _generated_bundle_publish_failure("generated_bundle_publish_failed")
    recovery = _probe_committed_transaction(
        slug=slug,
        transaction_id=transaction_id,
    )
    if recovery is None:
        return _generated_bundle_publish_failure(
            "generated_bundle_transaction_binding_invalid"
        )
    if not bool(recovery.get("runtime_publication_proven")):
        return _postcommit_failure(
            reason="runtime_publication_unverified_after_generator_success",
            recovery=recovery,
        )
    safe_result = _safe_generator_success_result(result)
    safe_result.update(
        {
            "slug": slug,
            "local_commit_applied": True,
            "transaction_id_bound": True,
            "publication_durability": str(
                recovery.get("publication_durability") or "unverified"
            ),
            "runtime_publish_required": bool(
                recovery.get("runtime_publish_required")
            ),
            "runtime_publish_ok": bool(recovery.get("runtime_publish_ok")),
            "runtime_publish_status": str(
                recovery.get("runtime_publish_status") or "unverified"
            ),
            "runtime_publication_proven": True,
        }
    )
    return {
        "status": "generated",
        "result": safe_result,
    }


def run_generation_request(
    payload: dict[str, object],
    *,
    config: BridgeConfig | None = None,
) -> dict[str, object]:
    runtime_config = config or _load_bridge_config()
    _validate_generation_cost(payload, config=runtime_config)
    slug = _valid_generated_bundle_slug(payload.get("slug"))
    if not slug:
        raise ValueError("slug_invalid")
    with _serialized_generation_slug(slug):
        return _run_generation_request_locked(
            payload,
            runtime_config=runtime_config,
        )


class _Handler(BaseHTTPRequestHandler):
    server_version = "PropertyReconstructionRenderBridge"
    sys_version = ""

    def version_string(self) -> str:
        return self.server_version

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(self._config.request_timeout_seconds)

    @property
    def _config(self) -> BridgeConfig:
        return getattr(self.server, "bridge_config")

    @property
    def _admission_backend(self) -> AdmissionBackend:
        return getattr(self.server, "admission_backend")

    @property
    def _bridge_server(self) -> "ReconstructionRenderBridgeServer":
        return getattr(self.server, "bridge_server", self.server)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return

    def _request_observability(self):  # type: ignore[no-untyped-def]
        trace_context = getattr(self, "_runtime_trace_context", None)
        if trace_context is None:
            trace_context = new_server_trace_context(self.headers.get("traceparent"))
            self._runtime_trace_context = trace_context
        correlation_id = str(getattr(self, "_runtime_correlation_id", "") or "")
        if not correlation_id:
            correlation_id = bounded_correlation_id(
                self.headers.get("x-correlation-id")
            )
            self._runtime_correlation_id = correlation_id
        return trace_context, correlation_id

    def _write_json(self, status_code: int, payload: dict[str, object]) -> None:
        trace_context, correlation_id = self._request_observability()
        self.close_connection = True
        encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("traceparent", trace_context.traceparent)
        self.send_header("x-correlation-id", correlation_id)
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(encoded)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def _allow_rate_limited_request(self) -> bool:
        client_key = str(self.client_address[0] if self.client_address else "unknown")
        try:
            decision = self._admission_backend.consume(
                QuotaCharge(
                    key=f"reconstruction_render:request:ip:{client_key}",
                    units=1,
                    limit=self._config.rate_limit_requests,
                    window_seconds=self._config.rate_limit_window_seconds,
                    dimension="ip",
                )
            )
        except AdmissionBackendUnavailable:
            self._write_json(
                503,
                {"status": "unavailable", "reason": "admission_backend_unavailable"},
            )
            return False
        if decision.allowed:
            return True
        self._write_json(429, {"status": "rejected", "reason": "request_rate_limit_exceeded"})
        return False

    def _authorized(self) -> bool:
        expected = str(self._config.auth_token or "").strip()
        if not expected:
            return True
        header = str(self.headers.get("Authorization") or "").strip()
        provided = header[7:].strip() if header.lower().startswith("bearer ") else ""
        return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))

    def _read_payload(self) -> dict[str, object] | None:
        if str(self.headers.get("Transfer-Encoding") or "").strip():
            self._write_json(400, {"status": "rejected", "reason": "transfer_encoding_unsupported"})
            return None
        raw_content_length = str(self.headers.get("Content-Length") or "").strip()
        if not raw_content_length:
            self._write_json(411, {"status": "rejected", "reason": "content_length_required"})
            return None
        try:
            content_length = int(raw_content_length)
        except ValueError:
            self._write_json(400, {"status": "rejected", "reason": "content_length_invalid"})
            return None
        if content_length < 1:
            self._write_json(400, {"status": "rejected", "reason": "request_body_required"})
            return None
        if content_length > self._config.max_body_bytes:
            self._write_json(413, {"status": "rejected", "reason": "request_body_too_large"})
            return None
        try:
            raw_body = self.rfile.read(content_length)
        except TimeoutError:
            self._write_json(408, {"status": "rejected", "reason": "request_body_timeout"})
            return None
        if len(raw_body) != content_length:
            self._write_json(400, {"status": "rejected", "reason": "request_body_incomplete"})
            return None
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            self._write_json(400, {"status": "rejected", "reason": "invalid_json"})
            return None
        if not isinstance(payload, dict):
            self._write_json(400, {"status": "rejected", "reason": "json_root_not_object"})
            return None
        return payload

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/health/live":
            draining = self._bridge_server.is_draining()
            self._write_json(
                200,
                {
                    "status": "draining" if draining else "live",
                    "bridge": "property_reconstruction_render_bridge",
                    "accepting_requests": not draining,
                },
            )
            return
        if path not in {"/health", "/health/ready"}:
            self._write_json(404, {"status": "not_found"})
            return
        ready, payload = _bridge_readiness(
            self._config,
            draining=self._bridge_server.is_draining(),
            admission_backend=self._admission_backend,
        )
        self._write_json(200 if ready else 503, payload)

    def do_POST(self) -> None:  # noqa: N802
        if urllib.parse.urlparse(self.path).path != "/generate-reconstruction":
            self._write_json(404, {"status": "not_found"})
            return
        if self._bridge_server.is_draining():
            self._write_json(503, {"status": "unavailable", "reason": "bridge_draining"})
            return
        if not self._allow_rate_limited_request():
            return
        if not self._authorized():
            self._write_json(401, {"status": "forbidden", "reason": "invalid_bridge_token"})
            return
        payload = self._read_payload()
        if payload is None:
            return
        try:
            _validate_generation_cost(payload, config=self._config)
        except ValueError as exc:
            self._write_json(
                422,
                {
                    "status": "rejected",
                    "reason": _stable_validation_reason(exc),
                },
            )
            return
        try:
            concurrency = self._admission_backend.acquire(
                (
                    ConcurrencyDimension(
                        key="reconstruction_render:concurrency:global",
                        limit=self._config.max_concurrency,
                        dimension="global",
                    ),
                ),
                lease_seconds=(
                    self._config.max_generation_seconds
                    + self._config.request_timeout_seconds
                    + PROCESS_CLOSE_MARGIN_SECONDS
                ),
            )
        except AdmissionBackendUnavailable:
            self._write_json(
                503,
                {"status": "unavailable", "reason": "admission_backend_unavailable"},
            )
            return
        if not concurrency.allowed:
            self._write_json(503, {"status": "busy", "reason": "generation_concurrency_limit"})
            return
        try:
            try:
                trace_context, correlation_id = self._request_observability()
                with bind_runtime_trace_context(
                    trace_context,
                    correlation_id=correlation_id,
                ):
                    result = run_generation_request(payload, config=self._config)
            except ValueError as exc:
                self._write_json(
                    422,
                    {
                        "status": "rejected",
                        "reason": _stable_validation_reason(exc),
                    },
                )
                return
            except Exception as exc:
                self._write_json(
                    500,
                    {
                        "status": "failed",
                        "reason": "internal_generation_failure",
                        "error_class": type(exc).__name__,
                    },
                )
                return
        finally:
            try:
                self._admission_backend.release(concurrency.lease_id)
            except AdmissionBackendUnavailable:
                # The bounded lease expires even when the authoritative store
                # cannot confirm release during a partition.
                pass
        status = str(result.get("status") or "").strip()
        self._write_json(200 if status == "generated" else 502, result)


class ReconstructionRenderBridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        *,
        config: BridgeConfig,
        admission_backend: AdmissionBackend | None = None,
    ) -> None:
        _validate_bridge_config(config)
        self.bridge_config = config
        self.admission_backend = admission_backend or _bridge_admission_backend(config)
        self.bridge_server = self
        self._drain_condition = threading.Condition()
        self._draining = False
        self._active_request_count = 0
        super().__init__(server_address, handler_class)

    def is_draining(self) -> bool:
        with self._drain_condition:
            return self._draining

    @property
    def active_request_count(self) -> int:
        with self._drain_condition:
            return self._active_request_count

    def begin_draining(self) -> bool:
        with self._drain_condition:
            if self._draining:
                return False
            self._draining = True
            self._drain_condition.notify_all()
            return True

    def wait_for_drain(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        with self._drain_condition:
            while self._active_request_count > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return False
                self._drain_condition.wait(timeout=remaining)
            return True

    def _request_started(self) -> None:
        with self._drain_condition:
            self._active_request_count += 1

    def _request_finished(self) -> None:
        with self._drain_condition:
            self._active_request_count = max(0, self._active_request_count - 1)
            self._drain_condition.notify_all()

    def process_request(self, request: object, client_address: tuple[str, int]) -> None:
        self._request_started()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_finished()
            raise

    def process_request_thread(self, request: object, client_address: tuple[str, int]) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_finished()


def _begin_graceful_shutdown(server: ReconstructionRenderBridgeServer) -> bool:
    if not server.begin_draining():
        return False
    threading.Thread(
        target=server.shutdown,
        name="propertyquarry-render-bridge-shutdown",
        daemon=True,
    ).start()
    return True


def main() -> int:
    config = _load_bridge_config()
    _validate_bridge_config(config)
    server = ReconstructionRenderBridgeServer((config.host, config.port), _Handler, config=config)
    previous_signal_handlers: dict[int, object] = {}
    if threading.current_thread() is threading.main_thread():
        for signal_number in (signal.SIGTERM, signal.SIGINT):
            previous_signal_handlers[signal_number] = signal.getsignal(signal_number)
            signal.signal(signal_number, lambda _signum, _frame: _begin_graceful_shutdown(server))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.begin_draining()
    finally:
        server.begin_draining()
        server.wait_for_drain(config.shutdown_grace_seconds)
        server.server_close()
        for signal_number, previous_handler in previous_signal_handlers.items():
            signal.signal(signal_number, previous_handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
