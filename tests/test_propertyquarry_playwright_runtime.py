from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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

    class _PlaywrightContext:
        def __enter__(self) -> object:
            return playwright

        def __exit__(self, *_args: object) -> None:
            return None

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
    monkeypatch.setattr(greenfield, "sync_playwright", _PlaywrightContext)
    monkeypatch.setattr(greenfield, "playwright_engine_launch_browser", _launch)

    fixture = greenfield.browser.__wrapped__()
    assert next(fixture) is browser
    fixture.close()

    assert observed["playwright"] is playwright
    assert observed["engine"] == "firefox"
    assert observed["closed"] is True
    assert "--no-sandbox" in observed["args"]


def test_smoke_runtime_matrix_installs_and_selects_every_core_browser_engine() -> None:
    workflow = (ROOT / ".github/workflows/smoke-runtime.yml").read_text(
        encoding="utf-8"
    )

    assert "browser-engine: [chromium, firefox, webkit]" in workflow
    assert (
        'python -m playwright install --with-deps "${{ matrix.browser-engine }}"'
        in workflow
    )
    assert (
        "PROPERTYQUARRY_CORE_BROWSER_ENGINE: ${{ matrix.browser-engine }}"
        in workflow
    )
    assert (
        "tests/e2e/test_propertyquarry_greenfield_browser.py::"
        "test_propertyquarry_workbench_candidate_history_stays_in_place"
        in workflow
    )
    assert (
        "tests/e2e/test_propertyquarry_greenfield_browser.py::"
        "test_propertyquarry_flagship_operating_loop_in_browser"
        in workflow
    )
