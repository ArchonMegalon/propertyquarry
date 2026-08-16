from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import propertyquarry_playwright_runtime as runtime


ROOT = Path(__file__).resolve().parents[1]


def test_playwright_runtime_normalizes_only_supported_engines() -> None:
    assert runtime.normalize_playwright_engine("") == "chromium"
    assert runtime.normalize_playwright_engine(" Firefox ") == "firefox"
    assert runtime.normalize_playwright_engine("WEBKIT") == "webkit"
    with pytest.raises(ValueError, match="unsupported_playwright_browser_engine:edge"):
        runtime.normalize_playwright_engine("edge")


def test_playwright_runtime_selects_requested_engine_without_chromium_arg_leakage(tmp_path) -> None:
    executable = tmp_path / "browser"
    executable.write_text("test", encoding="utf-8")
    browser_type = SimpleNamespace(executable_path=str(executable), launch=lambda **_kwargs: None)
    playwright = SimpleNamespace(chromium=browser_type, firefox=browser_type, webkit=browser_type)

    assert runtime.playwright_browser_type(playwright, engine="firefox") is browser_type
    assert runtime.playwright_engine_launch_kwargs(
        playwright,
        engine="chromium",
        args=["--no-sandbox"],
    ) == {
        "headless": True,
        "executable_path": str(executable),
        "args": ["--no-sandbox"],
    }
    assert runtime.playwright_engine_launch_kwargs(
        playwright,
        engine="firefox",
        args=["--chromium-only"],
    ) == {
        "headless": True,
        "executable_path": str(executable),
        "firefox_user_prefs": {
            "dom.ipc.processCount": 1,
            "dom.ipc.processCount.webIsolated": 1,
            "dom.ipc.processPrelaunch.enabled": False,
        },
    }
    assert runtime.playwright_engine_launch_kwargs(
        playwright,
        engine="webkit",
        args=["--chromium-only"],
        headless=False,
    ) == {
        "headless": False,
        "executable_path": str(executable),
    }

    with pytest.raises(ValueError, match="playwright_headless_mode_invalid"):
        runtime.playwright_engine_launch_kwargs(
            playwright,
            engine="chromium",
            headless=1,  # type: ignore[arg-type]
        )


def test_playwright_runtime_explicit_executable_overrides_discovery_and_is_launched(
    tmp_path,
) -> None:
    discovered = tmp_path / "discovered-chrome"
    explicit = tmp_path / "controller-chrome"
    discovered.write_text("discovered", encoding="utf-8")
    explicit.write_text("controller", encoding="utf-8")
    launches: list[dict[str, object]] = []
    launched_browser = object()

    def launch(**kwargs: object) -> object:
        launches.append(dict(kwargs))
        return launched_browser

    playwright = SimpleNamespace(
        chromium=SimpleNamespace(
            executable_path=str(discovered),
            launch=launch,
        )
    )

    assert runtime.playwright_engine_launch_browser(
        playwright,
        engine="chromium",
        executable_path=str(explicit),
    ) is launched_browser
    assert launches == [
        {"headless": True, "executable_path": str(explicit)}
    ]

    with pytest.raises(ValueError, match="playwright_executable_path_invalid"):
        runtime.playwright_engine_launch_browser(
            playwright,
            executable_path=str(tmp_path / "missing"),
        )


def test_playwright_runtime_firefox_ci_process_profile_is_exact_immutable_and_fission_preserving(
) -> None:
    assert runtime.FIREFOX_CI_REDUCED_CONTENT_PROCESS_PROFILE_NAME == (
        "firefox-reduced-content-process-v1"
    )
    assert dict(runtime.FIREFOX_CI_REDUCED_CONTENT_PROCESS_USER_PREFS) == {
        "dom.ipc.processCount": 1,
        "dom.ipc.processCount.webIsolated": 1,
        "dom.ipc.processPrelaunch.enabled": False,
    }
    assert (
        "fission.autostart"
        not in runtime.FIREFOX_CI_REDUCED_CONTENT_PROCESS_USER_PREFS
    )
    assert "browser.tabs.remote.autostart" not in (
        runtime.FIREFOX_CI_REDUCED_CONTENT_PROCESS_USER_PREFS
    )
    assert not any(
        pref.startswith(("gfx.", "layers.", "media.", "network."))
        for pref in runtime.FIREFOX_CI_REDUCED_CONTENT_PROCESS_USER_PREFS
    )
    immutable_profile: Any = runtime.FIREFOX_CI_REDUCED_CONTENT_PROCESS_USER_PREFS
    with pytest.raises(TypeError):
        immutable_profile["dom.ipc.processCount"] = 2


def test_playwright_runtime_returns_a_fresh_firefox_preference_mapping(tmp_path) -> None:
    executable = tmp_path / "firefox"
    executable.write_text("test", encoding="utf-8")
    browser_type = SimpleNamespace(
        executable_path=str(executable),
        launch=lambda **_kwargs: None,
    )
    playwright = SimpleNamespace(firefox=browser_type)

    first = runtime.playwright_engine_launch_kwargs(playwright, engine="firefox")
    second = runtime.playwright_engine_launch_kwargs(playwright, engine="firefox")

    assert first["firefox_user_prefs"] == second["firefox_user_prefs"]
    assert first["firefox_user_prefs"] is not second["firefox_user_prefs"]
    first_prefs: Any = first["firefox_user_prefs"]
    first_prefs["dom.ipc.processCount"] = 2
    assert dict(runtime.FIREFOX_CI_REDUCED_CONTENT_PROCESS_USER_PREFS) == {
        "dom.ipc.processCount": 1,
        "dom.ipc.processCount.webIsolated": 1,
        "dom.ipc.processPrelaunch.enabled": False,
    }


def test_playwright_runtime_fails_clearly_when_required_engine_is_missing() -> None:
    with pytest.raises(RuntimeError, match="playwright_browser_engine_unavailable:webkit"):
        runtime.playwright_browser_type(SimpleNamespace(chromium=object()), engine="webkit")


def test_playwright_runtime_does_not_fallback_from_invalid_configured_executable(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_PLAYWRIGHT_FIREFOX_EXECUTABLE", "/missing/firefox")
    with pytest.raises(FileNotFoundError, match="PROPERTYQUARRY_PLAYWRIGHT_FIREFOX_EXECUTABLE"):
        runtime.playwright_engine_executable(SimpleNamespace(), engine="firefox")


class TargetClosedError(Exception):
    pass


@pytest.mark.parametrize(
    ("configured_limit", "expected_limit"),
    (
        (None, 1),
        ("invalid", 1),
        ("0", 1),
        ("3", 3),
        ("99", 4),
    ),
)
def test_playwright_runtime_webkit_cpu_affinity_limit_is_strictly_clamped(
    monkeypatch: pytest.MonkeyPatch,
    configured_limit: str | None,
    expected_limit: int,
) -> None:
    if configured_limit is None:
        monkeypatch.delenv(runtime.WEBKIT_CI_CPU_AFFINITY_LIMIT_ENV, raising=False)
    else:
        monkeypatch.setenv(
            runtime.WEBKIT_CI_CPU_AFFINITY_LIMIT_ENV,
            configured_limit,
        )

    assert runtime.playwright_webkit_cpu_affinity_limit() == expected_limit


def test_playwright_runtime_webkit_launch_selects_lowest_allowed_cpus_and_restores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(runtime.WEBKIT_CI_CPU_AFFINITY_LIMIT_ENV, "3")
    original_affinity = frozenset({17, 3, 11, 5, 29})
    current_affinity = set(original_affinity)
    transitions: list[tuple[int, frozenset[int]]] = []
    launch_affinities: list[frozenset[int]] = []
    launched_browser = object()

    def _get_affinity(pid: int) -> set[int]:
        assert pid == 0
        return set(current_affinity)

    def _set_affinity(pid: int, affinity: object) -> None:
        nonlocal current_affinity
        assert pid == 0
        current_affinity = set(affinity)  # type: ignore[arg-type]
        transitions.append((pid, frozenset(current_affinity)))

    def _launch(**_kwargs: object) -> object:
        launch_affinities.append(frozenset(current_affinity))
        return launched_browser

    monkeypatch.setattr(runtime.os, "sched_getaffinity", _get_affinity)
    monkeypatch.setattr(runtime.os, "sched_setaffinity", _set_affinity)
    browser_type = SimpleNamespace(executable_path="", launch=_launch)
    playwright = SimpleNamespace(webkit=browser_type)

    assert runtime.playwright_engine_launch_browser(
        playwright,
        engine="webkit",
    ) is launched_browser
    assert launch_affinities == [frozenset({3, 5, 11})]
    assert transitions == [
        (0, frozenset({3, 5, 11})),
        (0, original_affinity),
    ]
    assert current_affinity == set(original_affinity)


def test_playwright_runtime_webkit_launch_failure_restores_exact_affinity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_affinity = frozenset({8, 2, 13})
    current_affinity = set(original_affinity)
    transitions: list[frozenset[int]] = []

    def _set_affinity(_pid: int, affinity: object) -> None:
        nonlocal current_affinity
        current_affinity = set(affinity)  # type: ignore[arg-type]
        transitions.append(frozenset(current_affinity))

    def _launch(**_kwargs: object) -> object:
        assert current_affinity == {2}
        raise ValueError("browser launch failed")

    monkeypatch.delenv(runtime.WEBKIT_CI_CPU_AFFINITY_LIMIT_ENV, raising=False)
    monkeypatch.setattr(runtime.os, "sched_getaffinity", lambda _pid: set(current_affinity))
    monkeypatch.setattr(runtime.os, "sched_setaffinity", _set_affinity)
    browser_type = SimpleNamespace(executable_path="", launch=_launch)
    playwright = SimpleNamespace(webkit=browser_type)

    with pytest.raises(ValueError, match="browser launch failed"):
        runtime.playwright_engine_launch_browser(playwright, engine="webkit")
    assert transitions == [frozenset({2}), original_affinity]
    assert current_affinity == set(original_affinity)


def test_playwright_runtime_webkit_native_retry_restores_between_launches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_affinity = frozenset({14, 6, 21})
    current_affinity = set(original_affinity)
    transitions: list[frozenset[int]] = []
    launches = 0
    launched_browser = object()

    def _set_affinity(_pid: int, affinity: object) -> None:
        nonlocal current_affinity
        current_affinity = set(affinity)  # type: ignore[arg-type]
        transitions.append(frozenset(current_affinity))

    def _launch(**_kwargs: object) -> object:
        nonlocal launches
        launches += 1
        assert current_affinity == {6}
        if launches == 1:
            raise TargetClosedError("Received signal 11; signal=SIGSEGV")
        return launched_browser

    monkeypatch.setattr(runtime.os, "sched_getaffinity", lambda _pid: set(current_affinity))
    monkeypatch.setattr(runtime.os, "sched_setaffinity", _set_affinity)
    browser_type = SimpleNamespace(executable_path="", launch=_launch)
    playwright = SimpleNamespace(webkit=browser_type)

    assert runtime.playwright_engine_launch_browser(
        playwright,
        engine="webkit",
    ) is launched_browser
    assert transitions == [
        frozenset({6}),
        original_affinity,
        frozenset({6}),
        original_affinity,
    ]
    assert current_affinity == set(original_affinity)


def test_playwright_runtime_non_webkit_launch_does_not_touch_cpu_affinity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unexpected_affinity_call(*_args: object) -> object:
        raise AssertionError("non-WebKit launch touched CPU affinity")

    monkeypatch.setattr(runtime.os, "sched_getaffinity", _unexpected_affinity_call)
    monkeypatch.setattr(runtime.os, "sched_setaffinity", _unexpected_affinity_call)
    launched_browser = object()
    browser_type = SimpleNamespace(executable_path="", launch=lambda **_kwargs: launched_browser)
    playwright = SimpleNamespace(chromium=browser_type)

    assert runtime.playwright_engine_launch_browser(playwright) is launched_browser


def test_playwright_runtime_webkit_launch_without_affinity_capability_is_a_safe_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(runtime.os, "sched_getaffinity", raising=False)
    monkeypatch.delattr(runtime.os, "sched_setaffinity", raising=False)
    launched_browser = object()
    browser_type = SimpleNamespace(executable_path="", launch=lambda **_kwargs: launched_browser)
    playwright = SimpleNamespace(webkit=browser_type)

    assert runtime.playwright_engine_launch_browser(
        playwright,
        engine="webkit",
    ) is launched_browser


@pytest.mark.parametrize("invalid_affinity", (set(), {-1}, {"cpu"}))
def test_playwright_runtime_webkit_invalid_allowed_affinity_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    invalid_affinity: set[object],
) -> None:
    launches = 0

    def _launch(**_kwargs: object) -> object:
        nonlocal launches
        launches += 1
        return object()

    def _unexpected_set_affinity(*_args: object) -> None:
        raise AssertionError("invalid mask was applied")

    monkeypatch.setattr(runtime.os, "sched_getaffinity", lambda _pid: invalid_affinity)
    monkeypatch.setattr(runtime.os, "sched_setaffinity", _unexpected_set_affinity)
    browser_type = SimpleNamespace(executable_path="", launch=_launch)
    playwright = SimpleNamespace(webkit=browser_type)

    with pytest.raises(RuntimeError, match="playwright_webkit_cpu_affinity_invalid"):
        runtime.playwright_engine_launch_browser(playwright, engine="webkit")
    assert launches == 0


def test_playwright_runtime_relaunches_once_after_native_browser_crash() -> None:
    launched_browser = object()
    launches: list[dict[str, object]] = []

    def _launch(**kwargs: object) -> object:
        launches.append(kwargs)
        if len(launches) == 1:
            raise TargetClosedError("Received signal 11; process did exit: signal=SIGSEGV")
        return launched_browser

    browser_type = SimpleNamespace(executable_path="", launch=_launch)
    playwright = SimpleNamespace(chromium=browser_type)

    assert runtime.playwright_engine_launch_browser(
        playwright,
        args=["--no-sandbox"],
    ) is launched_browser
    assert launches == [
        {"headless": True, "args": ["--no-sandbox"]},
        {"headless": True, "args": ["--no-sandbox"]},
    ]


def test_playwright_runtime_recognizes_installed_target_closed_error() -> None:
    playwright_errors = pytest.importorskip("playwright._impl._errors")
    launches = 0

    def _launch(**_kwargs: object) -> object:
        nonlocal launches
        launches += 1
        if launches == 1:
            raise playwright_errors.TargetClosedError("Received signal 11; signal=SIGSEGV")
        return object()

    browser_type = SimpleNamespace(executable_path="", launch=_launch)
    playwright = SimpleNamespace(chromium=browser_type)

    runtime.playwright_engine_launch_browser(playwright)
    assert launches == 2


def test_playwright_runtime_second_native_browser_crash_still_fails() -> None:
    launches = 0

    def _launch(**_kwargs: object) -> object:
        nonlocal launches
        launches += 1
        raise TargetClosedError("General protection fault; signal=SIGSEGV")

    browser_type = SimpleNamespace(executable_path="", launch=_launch)
    playwright = SimpleNamespace(chromium=browser_type)

    with pytest.raises(TargetClosedError, match="signal=SIGSEGV"):
        runtime.playwright_engine_launch_browser(playwright)
    assert launches == 2


@pytest.mark.parametrize(
    "error",
    [
        TargetClosedError("Target page, context or browser has been closed"),
        RuntimeError("Received signal 11; signal=SIGSEGV"),
    ],
)
def test_playwright_runtime_does_not_relaunch_unrelated_failures(error: Exception) -> None:
    launches = 0

    def _launch(**_kwargs: object) -> object:
        nonlocal launches
        launches += 1
        raise error

    browser_type = SimpleNamespace(executable_path="", launch=_launch)
    playwright = SimpleNamespace(chromium=browser_type)

    with pytest.raises(type(error)):
        runtime.playwright_engine_launch_browser(playwright)
    assert launches == 1


def test_greenfield_browser_fixture_honors_requested_core_engine(monkeypatch) -> None:
    from tests.e2e import test_propertyquarry_greenfield_browser as greenfield

    observed: dict[str, object] = {}
    browser = SimpleNamespace(close=lambda: observed.__setitem__("closed", True))
    playwright = SimpleNamespace()

    def _launch(
        observed_playwright: object,
        *,
        engine: str,
        args: list[str] | None = None,
    ) -> object:
        observed["playwright"] = observed_playwright
        observed["engine"] = engine
        observed["args"] = list(args or ())
        return browser

    monkeypatch.setenv("PROPERTYQUARRY_CORE_BROWSER_ENGINE", " Firefox ")
    monkeypatch.setattr(greenfield, "playwright_engine_launch_browser", _launch)

    fixture = greenfield.browser.__wrapped__(playwright)
    assert next(fixture) is browser
    fixture.close()

    assert observed["playwright"] is playwright
    assert observed["engine"] == "firefox"
    assert observed["closed"] is True
    assert "--no-sandbox" in observed["args"]


def test_local_playwright_runtime_supports_every_core_browser_engine() -> None:
    runtime = (ROOT / "scripts/propertyquarry_playwright_runtime.py").read_text(
        encoding="utf-8"
    )

    for engine in ("chromium", "firefox", "webkit"):
        assert engine in runtime
    assert 'SUPPORTED_PLAYWRIGHT_ENGINES = ("chromium", "firefox", "webkit")' in runtime
