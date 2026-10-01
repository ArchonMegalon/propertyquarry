from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import propertyquarry_ooda_approved_signals as approved
from scripts import propertyquarry_ooda_public_origin_observation as observation


NOW = datetime(2026, 8, 27, 8, 30, tzinfo=timezone.utc)


def test_cloudflare_1033_is_normalized_without_retaining_response_data() -> None:
    request: dict[str, object] = {}

    def http_get(url: str, **kwargs: object) -> dict[str, object]:
        request.update({"url": url, **kwargs})
        return {
            "status_code": 530,
            "headers": {"server": "cloudflare", "set-cookie": "secret-cookie"},
            "text": "secret response body; Error code: 1033",
            "url": url,
        }

    receipt = observation.build_public_origin_observation(
        now=NOW,
        http_get=http_get,
    )

    assert request == {
        "url": "https://propertyquarry.com/",
        "headers": {
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": "PropertyQuarry-live-mobile-surface-smoke/1.0",
        },
        "timeout_seconds": 10.0,
        "follow_redirects": False,
        "authorized_origin": "https://propertyquarry.com",
    }
    assert receipt["status"] == "blocked"
    assert receipt["observation"] == {
        "http_status": 530,
        "edge_provider": "cloudflare",
        "error_code": 1033,
        "reason": "cloudflare_tunnel_unavailable",
    }
    serialized = json.dumps(receipt)
    assert "secret-cookie" not in serialized
    assert "secret response body" not in serialized
    assert receipt["request"]["credentials_sent"] is False
    assert receipt["response_content_recorded"] is False
    assert receipt["response_headers_recorded"] is False
    assert receipt["action_required"] is False
    assert receipt["execution_authorized"] is False
    assert receipt["deployment_or_restart_authorized"] is False
    assert receipt["provider_quota_consumption_allowed"] is False
    assert receipt["delivery_authorized"] is False

    projection = approved.sanitize_public_origin_observation(receipt)
    action = approved.public_origin_operator_action_summary(projection, now=NOW)
    assert action["action_required"] is True
    assert action["interrupt_operator"] is True
    assert action["reason"] == "live_runtime_tunnel_unavailable"
    assert action["consent_gate"] == {
        "required": True,
        "automatic_execution_allowed": False,
        "protected_operations": [
            "runtime_configuration_change",
            "deployment_or_restart",
        ],
    }


def test_reachable_observation_is_evidence_only_and_requests_no_action() -> None:
    receipt = observation.build_public_origin_observation(
        now=NOW,
        http_get=lambda _url, **_kwargs: {
            "status_code": 204,
            "headers": {},
            "text": "",
        },
    )

    assert receipt["status"] == "reachable"
    assert receipt["observation"] == {
        "http_status": 204,
        "edge_provider": "",
        "error_code": 0,
        "reason": "public_origin_reachable",
    }
    action = approved.public_origin_operator_action_summary(receipt, now=NOW)
    assert action == {
        "status": "none",
        "action_required": False,
        "interrupt_operator": False,
        "reason": "public_origin_reachable",
        "source_generated_at": NOW.isoformat(),
    }


def test_network_failure_is_indeterminate_without_recording_exception_text() -> None:
    def fail(_url: str, **_kwargs: object) -> dict[str, object]:
        raise OSError("secret proxy address")

    receipt = observation.build_public_origin_observation(
        now=NOW,
        http_get=fail,
    )

    assert receipt["status"] == "indeterminate"
    assert receipt["observation"] == {
        "http_status": 0,
        "edge_provider": "",
        "error_code": 0,
        "reason": "network_error",
    }
    assert "secret proxy address" not in json.dumps(receipt)
    assert approved.public_origin_operator_action_summary(receipt, now=NOW)[
        "action_required"
    ] is False


@pytest.mark.parametrize(
    "origin",
    [
        "http://propertyquarry.com",
        "https://user:pass@propertyquarry.com",
        "https://propertyquarry.com/private",
        "https://propertyquarry.com/?token=secret",
        "https://propertyquarry.com:8443",
    ],
)
def test_public_origin_scope_rejects_unsafe_or_expansive_urls(origin: str) -> None:
    with pytest.raises(ValueError, match="public_origin_not_admissible"):
        observation.normalized_public_origin(origin)


def test_writer_persists_a_private_receipt(tmp_path: Path) -> None:
    path = tmp_path / "observation.json"
    receipt = observation.write_public_origin_observation(
        receipt_path=path,
        now=NOW,
        http_get=lambda _url, **_kwargs: {
            "status_code": 200,
            "headers": {},
            "text": "",
        },
    )

    assert json.loads(path.read_text(encoding="utf-8")) == receipt
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_health_accepts_only_a_fresh_private_receipt(tmp_path: Path) -> None:
    path = tmp_path / "observation.json"
    observation.write_public_origin_observation(
        receipt_path=path,
        now=NOW,
        http_get=lambda _url, **_kwargs: {
            "status_code": 200,
            "headers": {},
            "text": "",
        },
    )

    report = observation.inspect_public_origin_observation_receipt(
        receipt_path=path,
        max_age_seconds=900,
        now=NOW + timedelta(seconds=899),
    )

    assert report["status"] == "ready"
    assert report["source_status"] == "reachable"
    assert report["progress"] == {
        "receipt_integrity_verified": True,
        "source_contract_verified": True,
        "source_freshness_verified": True,
        "current_evidence_verified": True,
    }
    for field in (
        "action_required",
        "interrupt_operator",
        "automatic_execution_allowed",
        "execution_authorized",
        "deployment_or_restart_authorized",
        "protected_operation_executed",
        "provider_quota_consumption_allowed",
        "delivery_authorized",
        "delivery_attempted",
        "sent",
    ):
        assert report[field] is False


def test_health_rejects_stale_peer_readable_and_linked_receipts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "observation.json"
    observation.write_public_origin_observation(
        receipt_path=path,
        now=NOW,
        http_get=lambda _url, **_kwargs: {
            "status_code": 200,
            "headers": {},
            "text": "",
        },
    )

    with pytest.raises(ValueError, match="receipt_not_fresh"):
        observation.inspect_public_origin_observation_receipt(
            receipt_path=path,
            max_age_seconds=900,
            now=NOW + timedelta(seconds=901),
        )

    os.chmod(path, 0o640)
    with pytest.raises(ValueError, match="receipt_not_admissible"):
        observation.inspect_public_origin_observation_receipt(
            receipt_path=path,
            now=NOW,
        )

    os.chmod(path, 0o600)
    linked = tmp_path / "linked-observation.json"
    linked.symlink_to(path)
    with pytest.raises(ValueError, match="receipt_not_admissible"):
        observation.inspect_public_origin_observation_receipt(
            receipt_path=linked,
            now=NOW,
        )


@pytest.mark.parametrize("interval", [59, 1801, float("nan"), float("inf")])
def test_daemon_rejects_unbounded_intervals(interval: float, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="public_origin_interval_not_admissible"):
        observation.run_observation_daemon(
            receipt_path=tmp_path / "observation.json",
            origin=observation.DEFAULT_ORIGIN,
            timeout_seconds=10,
            interval_seconds=interval,
        )
