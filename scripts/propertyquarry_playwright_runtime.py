from __future__ import annotations

import os
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Callable


if TYPE_CHECKING:
    from playwright.sync_api import Browser
else:
    Browser = Any


CHROMIUM_EXECUTABLE_ENV = "PROPERTYQUARRY_PLAYWRIGHT_CHROMIUM_EXECUTABLE"
SUPPORTED_PLAYWRIGHT_ENGINES = ("chromium", "firefox", "webkit")
PLAYWRIGHT_EXECUTABLE_ENV_BY_ENGINE = {
    "chromium": CHROMIUM_EXECUTABLE_ENV,
    "firefox": "PROPERTYQUARRY_PLAYWRIGHT_FIREFOX_EXECUTABLE",
    "webkit": "PROPERTYQUARRY_PLAYWRIGHT_WEBKIT_EXECUTABLE",
}
WEBKIT_CI_CPU_AFFINITY_LIMIT_ENV = "PROPERTYQUARRY_PLAYWRIGHT_WEBKIT_CPU_AFFINITY_LIMIT"
WEBKIT_CI_CPU_AFFINITY_LIMIT_DEFAULT = 1
WEBKIT_CI_CPU_AFFINITY_LIMIT_MAXIMUM = 4
FIREFOX_CI_REDUCED_CONTENT_PROCESS_PROFILE_NAME = "firefox-reduced-content-process-v1"
# Bound Firefox renderer concurrency for host-safe CI while preserving e10s/Fission.
# Do not add security/isolation, GPU, network, or media process disables here.
FIREFOX_CI_REDUCED_CONTENT_PROCESS_USER_PREFS = MappingProxyType(
    {
        "dom.ipc.processCount": 1,
        "dom.ipc.processCount.webIsolated": 1,
        "dom.ipc.processPrelaunch.enabled": False,
    }
)
_PLAYWRIGHT_NATIVE_LAUNCH_CRASH_SIGNATURES = (
    "general protection fault",
    "received signal 11",
    "segmentation fault",
    "signal=sigsegv",
)


def normalize_playwright_engine(value: object) -> str:
    engine = str(value or "chromium").strip().lower()
    if engine not in SUPPORTED_PLAYWRIGHT_ENGINES:
        raise ValueError(f"unsupported_playwright_browser_engine:{engine}")
    return engine


def playwright_browser_type(playwright: object, *, engine: str = "chromium") -> object:
    normalized_engine = normalize_playwright_engine(engine)
    browser_type = getattr(playwright, normalized_engine, None)
    if browser_type is None or not callable(getattr(browser_type, "launch", None)):
        raise RuntimeError(f"playwright_browser_engine_unavailable:{normalized_engine}")
    return browser_type


def playwright_engine_executable(
    playwright: object | None = None,
    *,
    engine: str = "chromium",
) -> str:
    normalized_engine = normalize_playwright_engine(engine)
    executable_env = PLAYWRIGHT_EXECUTABLE_ENV_BY_ENGINE[normalized_engine]
    configured = str(os.getenv(executable_env) or "").strip()
    if configured:
        if not Path(configured).is_file():
            raise FileNotFoundError(f"{executable_env} does not point to a file: {configured}")
        return configured
    try:
        browser_type = getattr(playwright, normalized_engine) if playwright is not None else None
        candidate = str(getattr(browser_type, "executable_path", "") or "").strip()
    except Exception:
        candidate = ""
    return candidate if candidate and Path(candidate).is_file() else ""


def playwright_engine_launch_kwargs(
    playwright: object,
    *,
    engine: str = "chromium",
    args: list[str] | None = None,
    headless: bool = True,
    executable_path: str | None = None,
) -> dict[str, object]:
    normalized_engine = normalize_playwright_engine(engine)
    if type(headless) is not bool:
        raise ValueError("playwright_headless_mode_invalid")
    launch_kwargs: dict[str, object] = {"headless": headless}
    if executable_path is not None:
        if type(executable_path) is not str or executable_path != executable_path.strip():
            raise ValueError("playwright_executable_path_invalid")
        candidate_path = Path(executable_path)
        if not candidate_path.is_absolute() or not candidate_path.is_file():
            raise ValueError("playwright_executable_path_invalid")
        executable = executable_path
    else:
        executable = playwright_engine_executable(playwright, engine=normalized_engine)
    if executable:
        launch_kwargs["executable_path"] = executable
    if args and normalized_engine == "chromium":
        launch_kwargs["args"] = list(args)
    if normalized_engine == "firefox":
        launch_kwargs["firefox_user_prefs"] = dict(
            FIREFOX_CI_REDUCED_CONTENT_PROCESS_USER_PREFS
        )
    return launch_kwargs


def _is_retryable_playwright_native_launch_crash(exc: Exception) -> bool:
    if type(exc).__name__ != "TargetClosedError":
        return False
    message = str(exc).lower()
    return any(signature in message for signature in _PLAYWRIGHT_NATIVE_LAUNCH_CRASH_SIGNATURES)


def playwright_webkit_cpu_affinity_limit() -> int:
    raw_limit = str(
        os.getenv(WEBKIT_CI_CPU_AFFINITY_LIMIT_ENV)
        or WEBKIT_CI_CPU_AFFINITY_LIMIT_DEFAULT
    ).strip()
    try:
        requested_limit = int(raw_limit)
    except (TypeError, ValueError):
        requested_limit = WEBKIT_CI_CPU_AFFINITY_LIMIT_DEFAULT
    return max(1, min(WEBKIT_CI_CPU_AFFINITY_LIMIT_MAXIMUM, requested_limit))


def _launch_webkit_with_bounded_cpu_affinity(
    browser_type: Any,
    launch_kwargs: dict[str, object],
) -> Browser:
    get_affinity = getattr(os, "sched_getaffinity", None)
    set_affinity = getattr(os, "sched_setaffinity", None)
    if not callable(get_affinity) or not callable(set_affinity):
        return browser_type.launch(**launch_kwargs)

    try:
        raw_affinity = get_affinity(0)
        original_affinity = frozenset(raw_affinity)
    except (OSError, TypeError, ValueError) as exc:
        raise RuntimeError("playwright_webkit_cpu_affinity_unavailable") from exc
    if not original_affinity or any(
        type(cpu) is not int or cpu < 0 for cpu in original_affinity
    ):
        raise RuntimeError("playwright_webkit_cpu_affinity_invalid")

    bounded_affinity = frozenset(
        sorted(original_affinity)[:playwright_webkit_cpu_affinity_limit()]
    )
    try:
        set_affinity(0, bounded_affinity)
    except (OSError, TypeError, ValueError) as exc:
        raise RuntimeError("playwright_webkit_cpu_affinity_apply_failed") from exc
    try:
        return browser_type.launch(**launch_kwargs)
    finally:
        try:
            set_affinity(0, original_affinity)
        except (OSError, TypeError, ValueError) as exc:
            raise RuntimeError("playwright_webkit_cpu_affinity_restore_failed") from exc


def playwright_engine_launch_browser(
    playwright: object,
    *,
    engine: str = "chromium",
    args: list[str] | None = None,
    headless: bool = True,
    executable_path: str | None = None,
) -> Browser:
    """Launch once more only when the first browser process dies from a native crash."""

    normalized_engine = normalize_playwright_engine(engine)
    browser_type: Any = playwright_browser_type(playwright, engine=normalized_engine)
    launch_kwargs = playwright_engine_launch_kwargs(
        playwright,
        engine=normalized_engine,
        args=args,
        headless=headless,
        executable_path=executable_path,
    )

    def _launch_once() -> Browser:
        if normalized_engine == "webkit":
            return _launch_webkit_with_bounded_cpu_affinity(
                browser_type,
                launch_kwargs,
            )
        return browser_type.launch(**launch_kwargs)

    try:
        return _launch_once()
    except Exception as exc:
        if not _is_retryable_playwright_native_launch_crash(exc):
            raise
    return _launch_once()


def playwright_engine_capture_available(
    *,
    engine: str = "chromium",
    sync_playwright_factory: Callable[[], object] | None = None,
) -> bool:
    normalized_engine = normalize_playwright_engine(engine)
    if sync_playwright_factory is None:
        try:
            from playwright.sync_api import sync_playwright as sync_playwright_factory
        except Exception:
            return False
    try:
        with sync_playwright_factory() as playwright:  # type: ignore[attr-defined]
            playwright_browser_type(playwright, engine=normalized_engine)
            return bool(playwright_engine_executable(playwright, engine=normalized_engine))
    except Exception:
        return False


def playwright_chromium_executable(playwright: object | None = None) -> str:
    return playwright_engine_executable(playwright, engine="chromium")


def playwright_chromium_launch_kwargs(
    playwright: object,
    *,
    args: list[str] | None = None,
) -> dict[str, object]:
    return playwright_engine_launch_kwargs(playwright, engine="chromium", args=args)


def playwright_chromium_capture_available(
    sync_playwright_factory: Callable[[], object] | None = None,
) -> bool:
    return playwright_engine_capture_available(
        engine="chromium",
        sync_playwright_factory=sync_playwright_factory,
    )
