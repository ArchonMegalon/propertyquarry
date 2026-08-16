from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_EA_ROOT = _ROOT / "ea"
for _candidate in (str(_ROOT), str(_EA_ROOT)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

os.environ.setdefault("EA_INLINE_SYNC_HANDLERS", "1")


# pytest-playwright's upstream fixtures keep one sync Playwright runtime alive
# for the full pytest session.  This repository intentionally mixes browser
# tests with tests that call asyncio.run() and sync_playwright() directly; a
# session-long sync runtime leaves its asyncio loop active on the test thread
# and makes those later tests order-dependent.  Mirror the upstream fixture
# contract at function scope so every test has one coherent runtime that is
# fully stopped before the next test starts.
@pytest.fixture()
def playwright() -> Iterator[Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as runtime:
        yield runtime


@pytest.fixture()
def browser_type(playwright: Any, browser_name: str) -> Any:
    return getattr(playwright, browser_name)


@pytest.fixture()
def launch_browser(
    browser_type_launch_args: dict[str, object],
    browser_type: Any,
    connect_options: dict[str, object] | None,
) -> Callable[..., Any]:
    def launch(**kwargs: object) -> Any:
        launch_options = {**browser_type_launch_args, **kwargs}
        if connect_options:
            return browser_type.connect(
                **{
                    **connect_options,
                    "headers": {
                        "x-playwright-launch-options": json.dumps(launch_options),
                        **(connect_options.get("headers") or {}),
                    },
                }
            )
        return browser_type.launch(**launch_options)

    return launch


@pytest.fixture()
def browser(launch_browser: Callable[..., Any]) -> Iterator[Any]:
    runtime_browser = launch_browser()
    try:
        yield runtime_browser
    finally:
        runtime_browser.close()


@pytest.fixture()
def browser_context_args(
    pytestconfig: pytest.Config,
    playwright: Any,
    device: str | None,
    base_url: str | None,
    _pw_artifacts_folder: Any,
) -> dict[str, object]:
    context_args: dict[str, object] = {}
    if device:
        context_args.update(playwright.devices[device])
    if base_url:
        context_args["base_url"] = base_url
    if pytestconfig.getoption("--video") in {"on", "retain-on-failure"}:
        context_args["record_video_dir"] = _pw_artifacts_folder.name
    return context_args


@pytest.fixture(scope="session", autouse=True)
def _disable_external_property_map_tiles() -> Iterator[None]:
    key = "PROPERTYQUARRY_MAP_TILE_NETWORK_ENABLED"
    had_original = key in os.environ
    original = os.environ.get(key)
    os.environ[key] = "0"
    yield
    if had_original and original is not None:
        os.environ[key] = original
    else:
        os.environ.pop(key, None)


def _reset_shared_runtime_state() -> None:
    try:
        from app.services import cloudflare_access

        cloudflare_access._jwks_client.cache_clear()
    except Exception:
        pass
    try:
        from app.services import propertyquarry_registration_identity

        propertyquarry_registration_identity.reset_propertyquarry_registration_identity_memory_for_tests()
    except Exception:
        pass
    try:
        from app.services import responses_upstream

        responses_upstream._test_reset_onemin_states()
        responses_upstream._test_reset_fleet_jury_cache()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _restore_environment_and_shared_runtime_state(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    snapshot = dict(os.environ)
    isolated_ltd_name = hashlib.sha256(
        request.node.nodeid.encode("utf-8")
    ).hexdigest()
    os.environ["EA_LTD_MARKDOWN_PATH"] = str(
        tmp_path_factory.getbasetemp()
        / "ltd-markdown-isolation"
        / f"{isolated_ltd_name}.md"
    )
    _reset_shared_runtime_state()
    yield
    current_keys = set(os.environ.keys())
    original_keys = set(snapshot.keys())
    for key in current_keys - original_keys:
        os.environ.pop(key, None)
    for key in original_keys:
        os.environ[key] = snapshot[key]
    _reset_shared_runtime_state()
