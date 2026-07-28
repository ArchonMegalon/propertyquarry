from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_EA_ROOT = _ROOT / "ea"
for _candidate in (str(_ROOT), str(_EA_ROOT)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

os.environ.setdefault("EA_INLINE_SYNC_HANDLERS", "1")


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
