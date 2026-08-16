from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.e2e import test_propertyquarry_greenfield_browser as greenfield


class _FakeBrowser:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("configured_engine", "expected_engine"),
    (
        (None, "chromium"),
        (" Firefox ", "firefox"),
        ("WEBKIT", "webkit"),
    ),
)
def test_greenfield_browser_fixture_honors_configured_engine(
    monkeypatch: pytest.MonkeyPatch,
    configured_engine: str | None,
    expected_engine: str,
) -> None:
    if configured_engine is None:
        monkeypatch.delenv(
            greenfield._PROPERTYQUARRY_CORE_BROWSER_ENGINE_ENV,
            raising=False,
        )
    else:
        monkeypatch.setenv(
            greenfield._PROPERTYQUARRY_CORE_BROWSER_ENGINE_ENV,
            configured_engine,
        )

    fake_playwright = SimpleNamespace()
    fake_browser = _FakeBrowser()
    observed: dict[str, object] = {}

    def _launch_browser(
        playwright: object,
        *,
        engine: str,
        args: list[str] | None = None,
    ) -> _FakeBrowser:
        observed.update(playwright=playwright, engine=engine, args=args)
        return fake_browser

    monkeypatch.setattr(
        greenfield,
        "playwright_engine_launch_browser",
        _launch_browser,
    )

    fixture = greenfield.browser.__wrapped__(fake_playwright)
    assert next(fixture) is fake_browser
    with pytest.raises(StopIteration):
        next(fixture)

    assert observed == {
        "playwright": fake_playwright,
        "engine": expected_engine,
        "args": list(greenfield._PROPERTYQUARRY_CHROMIUM_LAUNCH_ARGS),
    }
    assert fake_browser.closed is True
