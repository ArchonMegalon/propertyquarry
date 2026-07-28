from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import pytest

from scripts import propertyquarry_activation_to_value_live as activation


ROOT = Path(__file__).resolve().parents[1]
RELEASE_SHA = "a" * 40


def _valid_config(tmp_path: Path, **overrides) -> activation.ActivationJourneyConfig:
    values = {
        "base_url": "https://staging.propertyquarry.com",
        "persona_id": "activation-persona-01",
        "persona_email": "activation@example.com",
        "run_key": "activation-run-20260713-01",
        "release_commit_sha": RELEASE_SHA,
        "auth_mode": "google",
        "provider_password": "provider-password-secret",
        "state_path": tmp_path / "activation-state.json",
        "live_authorized": True,
    }
    values.update(overrides)
    return activation.ActivationJourneyConfig(**values)


def _passing_result() -> dict[str, object]:
    return {
        "browser_engine": "chromium",
        "cleanup_ok": True,
        "steps": [
            {"name": name, "ok": True}
            for name in activation.REQUIRED_JOURNEY_STEPS
        ],
    }


def test_activation_journey_blocks_local_or_unconfirmed_runs_before_runner(tmp_path: Path) -> None:
    called = False

    def forbidden_runner(_config):
        nonlocal called
        called = True
        raise AssertionError("local blocked run must never reach browser or providers")

    config = _valid_config(
        tmp_path,
        base_url="http://127.0.0.1:8097",
        live_authorized=False,
    )
    receipt = activation.build_activation_to_value_receipt(
        config=config,
        journey_runner=forbidden_runner,
    )

    assert receipt["status"] == "blocked"
    assert called is False
    assert config.state_path.exists() is False
    reasons = {row["reason"] for row in receipt["checks"]}
    assert "activation_live_base_url_must_be_https" in reasons
    assert "activation_live_run_not_explicitly_authorized" in reasons


def test_activation_journey_rejects_unapproved_external_host_before_runner(tmp_path: Path) -> None:
    called = False

    def forbidden_runner(_config):
        nonlocal called
        called = True
        raise AssertionError("unapproved host must never reach browser or providers")

    receipt = activation.build_activation_to_value_receipt(
        config=_valid_config(tmp_path, base_url="https://example.com"),
        journey_runner=forbidden_runner,
    )

    assert receipt["status"] == "blocked"
    assert called is False
    assert any(
        row["reason"] == "activation_live_host_not_explicitly_allowed"
        for row in receipt["checks"]
    )


def test_activation_journey_mock_boundary_builds_secret_safe_passing_receipt(tmp_path: Path) -> None:
    config = _valid_config(tmp_path)

    def passing_runner(_config):
        reserved = json.loads(config.state_path.read_text(encoding="utf-8"))
        assert reserved["status"] == "reserved"
        assert reserved["release_commit_sha"] == RELEASE_SHA
        return _passing_result()

    receipt = activation.build_activation_to_value_receipt(
        config=config,
        journey_runner=passing_runner,
    )
    serialized = json.dumps(receipt, sort_keys=True)

    assert receipt["status"] == "pass"
    assert receipt["failed_count"] == 0
    assert receipt["proof_mode"] == "contract_mock"
    assert receipt["release_commit_sha"] == RELEASE_SHA
    assert [row["name"] for row in receipt["steps"]] == list(activation.REQUIRED_JOURNEY_STEPS)
    assert receipt["live_contract"] == {
        "explicit_persona": True,
        "principal_headers_forbidden": True,
        "session_injection_forbidden": True,
        "provider_response_mocking_forbidden": False,
        "local_execution_forbidden": True,
        "deployed_playwright_runner": False,
    }
    assert config.persona_id not in serialized
    assert config.persona_email not in serialized
    assert config.provider_password not in serialized
    state = json.loads(config.state_path.read_text(encoding="utf-8"))
    assert state["status"] == "pass"
    assert state["release_commit_sha"] == RELEASE_SHA
    assert state["persona_digest"] == config.persona_digest


@pytest.mark.parametrize(
    "release_sha",
    ("", "HEAD", "a" * 39, "A" * 40, "g" * 40),
)
def test_activation_journey_requires_exact_full_lowercase_release_sha_before_runner(
    tmp_path: Path,
    release_sha: str,
) -> None:
    called = False

    def forbidden_runner(_config):
        nonlocal called
        called = True
        raise AssertionError("invalid candidate must never reach browser or providers")

    receipt = activation.build_activation_to_value_receipt(
        config=_valid_config(tmp_path, release_commit_sha=release_sha),
        journey_runner=forbidden_runner,
    )

    assert receipt["status"] == "blocked"
    assert receipt["release_commit_sha"] == release_sha
    assert called is False
    assert receipt["checks"] == [
        {
            "name": "protected_live_configuration",
            "ok": False,
            "reason": "activation_release_commit_sha_missing_or_invalid",
        }
    ]


def test_activation_journey_run_key_is_fail_closed_against_duplicate_external_actions(tmp_path: Path) -> None:
    config = _valid_config(tmp_path)
    first = activation.build_activation_to_value_receipt(
        config=config,
        journey_runner=lambda _config: _passing_result(),
    )
    called = False

    def duplicate_runner(_config):
        nonlocal called
        called = True
        return _passing_result()

    second = activation.build_activation_to_value_receipt(
        config=config,
        journey_runner=duplicate_runner,
    )

    assert first["status"] == "pass"
    assert second["status"] == "blocked"
    assert called is False
    assert second["checks"][0]["name"] == "idempotent_run_reservation"
    assert "activation_run_key_already_used" in second["checks"][0]["reason"]


def test_activation_journey_run_key_cannot_be_reused_for_a_different_persona(tmp_path: Path) -> None:
    first_config = _valid_config(tmp_path)
    activation.build_activation_to_value_receipt(
        config=first_config,
        journey_runner=lambda _config: _passing_result(),
    )
    called = False

    def duplicate_runner(_config):
        nonlocal called
        called = True
        return _passing_result()

    second = activation.build_activation_to_value_receipt(
        config=_valid_config(
            tmp_path,
            persona_id="activation-persona-02",
            persona_email="activation-two@example.com",
        ),
        journey_runner=duplicate_runner,
    )

    assert second["status"] == "blocked"
    assert called is False
    assert "activation_run_key_already_used" in second["checks"][0]["reason"]


def test_activation_journey_run_key_cannot_cross_release_candidates(tmp_path: Path) -> None:
    first_config = _valid_config(tmp_path)
    activation.build_activation_to_value_receipt(
        config=first_config,
        journey_runner=lambda _config: _passing_result(),
    )
    called = False

    def duplicate_runner(_config):
        nonlocal called
        called = True
        return _passing_result()

    second = activation.build_activation_to_value_receipt(
        config=_valid_config(tmp_path, release_commit_sha="b" * 40),
        journey_runner=duplicate_runner,
    )

    assert second["status"] == "blocked"
    assert called is False
    assert second["release_commit_sha"] == "b" * 40
    assert "activation_run_key_candidate_mismatch" in second["checks"][0]["reason"]


def test_activation_journey_corrupt_idempotency_state_fails_closed(tmp_path: Path) -> None:
    config = _valid_config(tmp_path)
    config.state_path.write_text("not-json", encoding="utf-8")
    called = False

    def forbidden_runner(_config):
        nonlocal called
        called = True
        return _passing_result()

    receipt = activation.build_activation_to_value_receipt(
        config=config,
        journey_runner=forbidden_runner,
    )

    assert receipt["status"] == "blocked"
    assert called is False
    assert "activation_run_state_invalid" in receipt["checks"][0]["reason"]


def test_activation_journey_rejects_missing_value_step_even_when_mock_runner_claims_cleanup(tmp_path: Path) -> None:
    config = _valid_config(tmp_path)
    result = _passing_result()
    result["steps"] = [
        row for row in result["steps"]
        if row["name"] != "walkthrough_ready"
    ]

    receipt = activation.build_activation_to_value_receipt(
        config=config,
        journey_runner=lambda _config: result,
    )

    assert receipt["status"] == "fail"
    matrix = next(row for row in receipt["checks"] if row["name"] == "activation_step_matrix_complete")
    assert matrix["missing_steps"] == ["walkthrough_ready"]


def test_activation_journey_only_allows_preprovisioned_persona_until_account_cleanup_exists(tmp_path: Path) -> None:
    config = _valid_config(
        tmp_path,
        expected_account_state="new",
        allow_account_create=True,
    )

    failures = activation.validate_live_config(config)

    assert failures == ["activation_new_account_cleanup_not_supported_use_preprovisioned_persona"]


def test_activation_email_link_parser_accepts_only_fresh_same_origin_token_link() -> None:
    message = EmailMessage()
    message["Date"] = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")
    message.set_content(
        "Ignore https://evil.example/sign-in?token=wrong\n"
        "Open https://staging.propertyquarry.com/workspace/sign-in?token=real-token"
    )

    link = activation._extract_same_origin_sign_in_link(
        message.as_bytes(),
        base_url="https://staging.propertyquarry.com",
        not_before=datetime.now(timezone.utc),
    )

    assert link == "https://staging.propertyquarry.com/workspace/sign-in?token=real-token"


def test_activation_email_link_parser_rejects_message_without_verifiable_fresh_date() -> None:
    message = EmailMessage()
    message.set_content("https://staging.propertyquarry.com/workspace/sign-in?token=unverifiable")

    assert activation._extract_same_origin_sign_in_link(
        message.as_bytes(),
        base_url="https://staging.propertyquarry.com",
        not_before=datetime.now(timezone.utc),
    ) == ""


def test_activation_walkthrough_readiness_rejects_placeholder_links() -> None:
    assert activation._walkthrough_href_ready("") is False
    assert activation._walkthrough_href_ready("#") is False
    assert activation._walkthrough_href_ready("javascript:void(0)") is False
    assert activation._walkthrough_href_ready("/public/tours/ready") is True
    assert activation._walkthrough_href_ready("https://tour-provider.example/ready") is True


def test_activation_cli_release_sha_precedence_and_explicit_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str] = []

    def receipt_builder(*, config):
        observed.append(config.release_commit_sha)
        return {"status": "pass"}

    monkeypatch.setattr(activation, "build_activation_to_value_receipt", receipt_builder)
    monkeypatch.setenv("PROPERTYQUARRY_RELEASE_COMMIT_SHA", "a" * 40)
    monkeypatch.setenv("PROPERTYQUARRY_EXPECTED_RELEASE_COMMIT_SHA", "b" * 40)
    monkeypatch.setattr(
        sys,
        "argv",
        ["propertyquarry_activation_to_value_live.py", "--write", ""],
    )
    assert activation.main() == 0

    monkeypatch.delenv("PROPERTYQUARRY_RELEASE_COMMIT_SHA")
    assert activation.main() == 0

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "propertyquarry_activation_to_value_live.py",
            "--release-sha",
            "c" * 40,
            "--write",
            "",
        ],
    )
    assert activation.main() == 0

    assert observed == ["a" * 40, "b" * 40, "c" * 40]


def test_activation_live_source_forbids_test_auth_and_provider_mock_boundaries() -> None:
    source = (ROOT / "scripts/propertyquarry_activation_to_value_live.py").read_text(encoding="utf-8")

    assert "X-EA-Principal-ID" not in source
    assert "extra_http_headers" not in source
    assert ".add_cookies(" not in source
    assert "storage_state" not in source
    assert "route.fulfill" not in source
    assert "request_workspace_sign_in_email_links" not in source


def test_activation_workflow_is_contract_only_by_default_and_live_only_by_explicit_approval() -> None:
    source = (ROOT / ".github/workflows/smoke-runtime.yml").read_text(encoding="utf-8")
    contract_job = source.split("  propertyquarry-activation-contracts:", 1)[1].split(
        "  propertyquarry-live-release-gates:", 1
    )[0]
    live_job = source.split("  propertyquarry-live-activation-to-value:", 1)[1].split(
        "  propertyquarry-launch-controller-preflight:", 1
    )[0]
    live_release_job = source.split("  propertyquarry-live-release-gates:", 1)[1].split(
        "  propertyquarry-live-activation-to-value:", 1
    )[0]
    release_v2_job = source.split("  propertyquarry-release-v2:", 1)[1].split(
        "  propertyquarry-live-release-gates:", 1
    )[0]

    assert "tests/test_propertyquarry_activation_to_value_live.py" in contract_job
    assert "PROPERTYQUARRY_ACTIVATION_PERSONA_EMAIL" not in contract_job
    assert "if: ${{ false }}" in live_job
    assert "github.event_name == 'workflow_dispatch'" not in live_job
    assert "inputs.run_activation_journey == true" not in live_job
    assert "name: propertyquarry-production" in live_job
    assert "needs: propertyquarry-live-release-gates" in live_job
    assert "PROPERTYQUARRY_ACTIVATION_LIVE_RUN: \"1\"" in live_job
    assert "PROPERTYQUARRY_ACTIVATION_ALLOW_ACCOUNT_CREATE: \"0\"" in live_job
    assert "PROPERTYQUARRY_RELEASE_COMMIT_SHA=${runtime_sha}" in live_job
    assert "--confirm-live" in live_job
    assert '--release-sha "${PROPERTYQUARRY_RELEASE_COMMIT_SHA}"' in live_job
    assert re.search(r"actions/cache/restore@[0-9a-f]{40}\s+# v4", live_job)
    assert re.search(r"actions/cache/save@[0-9a-f]{40}\s+# v4", live_job)
    assert "X-EA-Principal-ID" not in live_job
    assert "PROPERTYQUARRY_LIVE_PRINCIPAL_ID" not in live_job
    assert "github.event_name == 'workflow_dispatch'" in release_v2_job
    assert "inputs.run_launch_authority == true" in release_v2_job
    assert "name: propertyquarry-production" in release_v2_job
    assert (
        "/usr/libexec/propertyquarry-release-control/"
        "propertyquarry-release-supervisor-v2"
    ) in release_v2_job
    assert "release-run" in release_v2_job
    assert "actions/checkout@" not in release_v2_job
    assert "  propertyquarry-flagship-security:" in source
    assert "if: ${{ false }}" in live_release_job
    assert "propertyquarry-flagship-security" in live_release_job
    assert "propertyquarry-continuous-ux" in live_release_job
