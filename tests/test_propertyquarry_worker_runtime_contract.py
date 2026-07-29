from __future__ import annotations

from pathlib import Path

import yaml


def test_property_search_worker_is_self_healing_and_sized_for_browser_lanes() -> None:
    compose = yaml.safe_load(Path("docker-compose.property.yml").read_text(encoding="utf-8"))
    worker = compose["services"]["propertyquarry-worker"]

    assert worker["restart"] == "${PROPERTYQUARRY_WORKER_RESTART_POLICY:-unless-stopped}"
    assert worker["mem_limit"] == "${PROPERTYQUARRY_WORKER_MEMORY_LIMIT:-4g}"
    assert worker["mem_reservation"] == "${PROPERTYQUARRY_WORKER_MEMORY_RESERVATION:-1g}"
    assert worker["memswap_limit"] == "${PROPERTYQUARRY_WORKER_MEMORY_SWAP_LIMIT:-6g}"
    assert (
        worker["environment"]["PROPERTYQUARRY_SEARCH_HTML_MAX_BYTES"]
        == "${PROPERTYQUARRY_SEARCH_HTML_MAX_BYTES:-8388608}"
    )
