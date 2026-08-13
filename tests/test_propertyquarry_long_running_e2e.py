from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import propertyquarry_long_running_e2e as gate


TOKEN = "internal-ci-token-" + ("x" * 48)
EMAIL = "postgres-browser-lane@example.com"
PRINCIPAL = "propertyquarry-postgres-browser"
NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


def _write_session(
    path: Path,
    *,
    mode: int = 0o600,
    expires_at: datetime | None = None,
    **patch: object,
) -> Path:
    payload: dict[str, object] = {
        "contract_name": gate.SESSION_CONTRACT_NAME,
        "version": 1,
        "status": "pass",
        "provisioning_scope": "internal_ci_only",
        "runtime_mode": "prod",
        "storage_backend": "postgres",
        "principal_id": PRINCIPAL,
        "email": EMAIL,
        "access_token": TOKEN,
        "expires_at": (
            expires_at or (NOW + timedelta(hours=2))
        ).isoformat(),
    }
    payload.update(patch)
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(mode)
    return path


def _config_kwargs(tmp_path: Path) -> dict[str, object]:
    return {
        "origin": gate.PRODUCTION_ORIGIN,
        "session_file": tmp_path / "session.json",
        "mode": "quick",
        "iterations": 2,
        "run_timeout_seconds": 300,
        "poll_seconds": 1,
        "browser_timeout_ms": 10_000,
        "screenshots_dir": tmp_path / "shots",
        "private_har_path": tmp_path / "private.har",
        "confirm_live": True,
        "confirm_search_side_effects": True,
    }


@pytest.mark.parametrize(
    "origin",
    [
        "http://propertyquarry.com",
        "https://www.propertyquarry.com",
        "https://propertyquarry.com:443",
        "https://propertyquarry.com/app/search",
        "https://propertyquarry.com?next=/app/search",
        "https://user:password@propertyquarry.com",
        "https://propertyquarry.com.evil.test",
    ],
)
def test_origin_requires_exact_https_propertyquarry_origin(origin: str) -> None:
    with pytest.raises(gate.GateSafetyError):
        gate.normalize_gate_origin(origin)


def test_origin_accepts_exact_production_and_test_only_loopback() -> None:
    assert (
        gate.normalize_gate_origin("https://propertyquarry.com/")
        == gate.PRODUCTION_ORIGIN
    )
    with pytest.raises(gate.GateSafetyError):
        gate.normalize_gate_origin("http://127.0.0.1:8097")
    assert (
        gate.normalize_gate_origin(
            "http://127.0.0.1:8097",
            allow_loopback_for_tests=True,
        )
        == "http://127.0.0.1:8097"
    )


def test_config_requires_both_live_and_side_effect_confirmations(
    tmp_path: Path,
) -> None:
    kwargs = _config_kwargs(tmp_path)
    kwargs["confirm_live"] = False
    with pytest.raises(
        gate.GateSafetyError,
        match="production_requires_confirm_live",
    ):
        gate.validate_gate_config(**kwargs)
    kwargs["confirm_live"] = True
    kwargs["confirm_search_side_effects"] = False
    with pytest.raises(
        gate.GateSafetyError,
        match="search_side_effects_require_confirmation",
    ):
        gate.validate_gate_config(**kwargs)


def test_quick_and_soak_iteration_budgets_are_bounded(tmp_path: Path) -> None:
    kwargs = _config_kwargs(tmp_path)
    assert gate.validate_gate_config(**kwargs).iterations == 2
    kwargs.update({"mode": "quick", "iterations": 3})
    with pytest.raises(
        gate.GateSafetyError,
        match="quick_mode_requires_exactly_two_launches",
    ):
        gate.validate_gate_config(**kwargs)
    kwargs.update({"mode": "soak", "iterations": 1})
    with pytest.raises(
        gate.GateSafetyError,
        match="soak_iterations_out_of_bounds",
    ):
        gate.validate_gate_config(**kwargs)
    kwargs["iterations"] = gate.MAX_SOAK_ITERATIONS + 1
    with pytest.raises(
        gate.GateSafetyError,
        match="soak_iterations_out_of_bounds",
    ):
        gate.validate_gate_config(**kwargs)
    kwargs["iterations"] = gate.MAX_SOAK_ITERATIONS
    assert gate.validate_gate_config(**kwargs).mode == "soak"


def test_config_requires_absolute_artifact_and_session_paths(
    tmp_path: Path,
) -> None:
    kwargs = _config_kwargs(tmp_path)
    kwargs["session_file"] = "relative-session.json"
    with pytest.raises(
        gate.GateSafetyError,
        match="session_file_must_be_absolute",
    ):
        gate.validate_gate_config(**kwargs)
    kwargs["session_file"] = tmp_path / "session.json"
    kwargs["screenshots_dir"] = "relative-shots"
    with pytest.raises(
        gate.GateSafetyError,
        match="screenshots_dir_must_be_absolute",
    ):
        gate.validate_gate_config(**kwargs)


def test_strict_session_contract_loads_without_exposing_identity(
    tmp_path: Path,
) -> None:
    path = _write_session(tmp_path / "session.json")
    session = gate.load_internal_ci_session(
        path,
        now=NOW,
        minimum_valid_for_seconds=600,
    )
    assert session.access_token == TOKEN
    assert session.expires_at.endswith("Z")
    assert len(session.receipt_sha256) == 64


@pytest.mark.parametrize("mode", [0o644, 0o400, 0o660, 0o777])
def test_session_file_requires_exact_mode_0600(
    tmp_path: Path,
    mode: int,
) -> None:
    path = _write_session(tmp_path / "session.json", mode=mode)
    with pytest.raises(
        gate.GateSafetyError,
        match="internal_ci_session_mode_must_be_0600",
    ):
        gate.load_internal_ci_session(path, now=NOW)


def test_session_file_rejects_symlink(tmp_path: Path) -> None:
    target = _write_session(tmp_path / "target.json")
    symlink = tmp_path / "session.json"
    symlink.symlink_to(target)
    with pytest.raises(
        gate.GateSafetyError,
        match="internal_ci_session_symlink_forbidden",
    ):
        gate.load_internal_ci_session(symlink, now=NOW)


@pytest.mark.parametrize(
    ("patch", "error"),
    [
        (
            {"contract_name": "propertyquarry.wrong"},
            "internal_ci_session_contract_invalid",
        ),
        (
            {"provisioning_scope": "customer"},
            "internal_ci_session_contract_invalid",
        ),
        (
            {"storage_backend": "memory"},
            "internal_ci_session_contract_invalid",
        ),
        (
            {"access_token": "short"},
            "internal_ci_session_access_token_invalid",
        ),
        (
            {"email": ""},
            "internal_ci_session_identity_fields_missing",
        ),
    ],
)
def test_session_file_rejects_contract_drift(
    tmp_path: Path,
    patch: dict[str, object],
    error: str,
) -> None:
    path = _write_session(tmp_path / "session.json", **patch)
    with pytest.raises(gate.GateSafetyError, match=error):
        gate.load_internal_ci_session(path, now=NOW)


def test_session_must_cover_configured_long_run_window(
    tmp_path: Path,
) -> None:
    path = _write_session(
        tmp_path / "session.json",
        expires_at=NOW + timedelta(minutes=20),
    )
    with pytest.raises(
        gate.GateSafetyError,
        match="internal_ci_session_validity_insufficient",
    ):
        gate.load_internal_ci_session(
            path,
            now=NOW,
            minimum_valid_for_seconds=1_800,
        )


def test_exact_request_cardinality_accepts_one_of_each() -> None:
    preferences = (
        "https://propertyquarry.com/v1/onboarding/property-search/preferences"
    )
    start = "https://propertyquarry.com/app/api/property/search-runs"
    result = gate.evaluate_launch_cardinality(
        [
            {"method": "GET", "url": preferences},
            {"method": "POST", "url": preferences},
            {"method": "POST", "url": start},
            {
                "method": "POST",
                "url": "https://analytics.example.test/event",
            },
        ],
        preferences_url=preferences,
        start_url=start,
    )
    assert result == {
        "ok": True,
        "preferences_post_count": 1,
        "start_post_count": 1,
    }


@pytest.mark.parametrize(
    "requests",
    [
        [],
        [
            {
                "method": "POST",
                "url": "https://propertyquarry.com/app/api/property/search-runs",
            }
        ],
        [
            {
                "method": "POST",
                "url": "https://propertyquarry.com/v1/onboarding/property-search/preferences",
            },
            {
                "method": "POST",
                "url": "https://propertyquarry.com/v1/onboarding/property-search/preferences",
            },
            {
                "method": "POST",
                "url": "https://propertyquarry.com/app/api/property/search-runs",
            },
        ],
    ],
)
def test_request_cardinality_fails_missing_or_duplicate_posts(
    requests: list[dict[str, object]],
) -> None:
    result = gate.evaluate_launch_cardinality(
        requests,
        preferences_url=(
            "https://propertyquarry.com/v1/onboarding/property-search/preferences"
        ),
        start_url=(
            "https://propertyquarry.com/app/api/property/search-runs"
        ),
    )
    assert result["ok"] is False


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://propertyquarry.com/app/search?run_id=run-42",
            "run-42",
        ),
        (
            "https://propertyquarry.com/app/properties?run_id=legacy-run",
            "legacy-run",
        ),
        ("https://propertyquarry.com/app/search", ""),
        (
            "https://propertyquarry.com/app/search?run_id=run-42&extra=1",
            "",
        ),
        (
            "https://propertyquarry.com/app/search?run_id=run-42#fragment",
            "",
        ),
        (
            "https://evil.example/app/search?run_id=run-42",
            "",
        ),
        (
            "https://propertyquarry.com/app/shortlist?run_id=run-42",
            "",
        ),
    ],
)
def test_search_run_navigation_accepts_canonical_and_legacy_workbench_routes(
    url: str,
    expected: str,
) -> None:
    assert gate._search_run_navigation_id(
        url,
        origin=gate.PRODUCTION_ORIGIN,
    ) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://propertyquarry.com/tours/viewer/demo-tour/"
            "generated-reconstruction/viewer.html",
            True,
        ),
        (
            "https://propertyquarry.com/tours/files/demo-tour/"
            "generated-reconstruction/viewer.html",
            True,
        ),
        (
            "https://propertyquarry.com/tours/viewer/other-tour/"
            "generated-reconstruction/viewer.html",
            False,
        ),
        (
            "https://evil.example/tours/viewer/demo-tour/"
            "generated-reconstruction/viewer.html",
            False,
        ),
        (
            "https://propertyquarry.com/tours/viewer/demo-tour/"
            "generated-reconstruction/viewer.html?embed=1",
            False,
        ),
    ],
)
def test_generated_reconstruction_checkpoint_requires_exact_same_origin_viewer_path(
    url: str,
    expected: bool,
) -> None:
    assert gate._generated_reconstruction_viewer_path_ok(
        url,
        origin=gate.PRODUCTION_ORIGIN,
        slug="demo-tour",
    ) is expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://propertyquarry.com/tours/karl-czerny-gasse-2-urban-jungle/control",
            True,
        ),
        (
            "https://propertyquarry.com/tours/karl-czerny-gasse-2-urban-jungle",
            False,
        ),
        (
            "https://propertyquarry.com/tours/karl-czerny-gasse-2-urban-jungle/control?fullscreen=1",
            False,
        ),
        (
            "https://evil.example/tours/karl-czerny-gasse-2-urban-jungle/control",
            False,
        ),
    ],
)
def test_ai_panorama_checkpoint_requires_exact_same_origin_control_path(
    url: str,
    expected: bool,
) -> None:
    assert gate._ai_panorama_control_path_ok(
        url,
        origin=gate.PRODUCTION_ORIGIN,
        slug="karl-czerny-gasse-2-urban-jungle",
    ) is expected


def test_default_three_d_checkpoint_targets_current_ai_panorama_flagship() -> None:
    assert gate.DEFAULT_THREE_D_SLUG == "karl-czerny-gasse-2-urban-jungle"


def test_unique_launch_accounting_requires_sequence_modes_runs_and_cardinality() -> None:
    launches = [
        {
            "iteration": 1,
            "mode": "immediate",
            "run_id_sha256": "a" * 64,
            "request_cardinality": {"ok": True},
        },
        {
            "iteration": 2,
            "mode": "hydrated",
            "run_id_sha256": "b" * 64,
            "request_cardinality": {"ok": True},
        },
    ]
    assert gate.evaluate_unique_launch_accounting(
        launches,
        expected_iterations=2,
    ) == {
        "ok": True,
        "launch_count": 2,
        "unique_run_count": 2,
        "created_run_count": 2,
        "active_run_reuse_count": 0,
        "run_dispositions": ["new_run", "new_run"],
        "run_accounting_ok": True,
        "iteration_sequence_ok": True,
        "mode_sequence_ok": True,
        "all_request_cardinalities_ok": True,
    }
    duplicate = [dict(launches[0]), dict(launches[1])]
    duplicate[1]["run_id_sha256"] = duplicate[0]["run_id_sha256"]
    assert (
        gate.evaluate_unique_launch_accounting(
            duplicate,
            expected_iterations=2,
        )["ok"]
        is False
    )


def test_launch_accounting_accepts_only_explicitly_active_run_reuse() -> None:
    active_poll = {
        "terminal": False,
        "successful_terminal": False,
        "final_status": "in_progress",
    }
    launches = [
        {
            "iteration": 1,
            "mode": "immediate",
            "run_id_sha256": "a" * 64,
            "request_cardinality": {"ok": True},
            "poll": active_poll,
        },
        {
            "iteration": 2,
            "mode": "hydrated",
            "run_id_sha256": "a" * 64,
            "request_cardinality": {"ok": True},
            "poll": active_poll,
        },
    ]

    result = gate.evaluate_unique_launch_accounting(
        launches,
        expected_iterations=2,
    )

    assert result["ok"] is True
    assert result["unique_run_count"] == 1
    assert result["created_run_count"] == 1
    assert result["active_run_reuse_count"] == 1
    assert result["run_dispositions"] == ["new_run", "active_run_reused"]

    terminal_reuse = [dict(launches[0]), dict(launches[1])]
    terminal_reuse[0]["poll"] = {
        "terminal": True,
        "successful_terminal": True,
        "final_status": "processed",
    }
    rejected = gate.evaluate_unique_launch_accounting(
        terminal_reuse,
        expected_iterations=2,
    )
    assert rejected["ok"] is False
    assert rejected["run_dispositions"] == ["new_run", "invalid_run_reuse"]


def test_poll_history_accepts_tolerated_jitter_and_success_terminal() -> None:
    evaluation = gate.evaluate_poll_history(
        [
            {"status": "queued", "progress": 0},
            {"status": "running", "progress": 35},
            {"status": "running", "progress": 32},
            {"status": "completed_partial", "progress": 100},
        ]
    )
    assert evaluation["ok"] is True
    assert evaluation["progress_regression_count"] == 0
    assert evaluation["final_status"] == "completed_partial"


def test_poll_history_accepts_live_product_processed_terminal() -> None:
    evaluation = gate.evaluate_poll_history(
        [
            {"status": "queued", "progress": 0},
            {"status": "in_progress", "progress": 56},
            {"status": "processed", "progress": 100},
        ]
    )
    assert evaluation["ok"] is True
    assert evaluation["terminal"] is True
    assert evaluation["successful_terminal"] is True
    assert evaluation["final_status"] == "processed"


def test_network_blockers_ignore_recovered_navigation_abort() -> None:
    journal = gate.NetworkJournal.empty()
    url = "https://propertyquarry.com/app/properties?run_id=fixture"
    journal.request_failures.append(
        {
            "url": url,
            "resource_type": "document",
            "failure": "net::ERR_ABORTED",
            "expected_offline": False,
        }
    )
    journal.responses.append(
        {
            "url": url,
            "resource_type": "document",
            "status": 200,
        }
    )
    assert gate.evaluate_network_blockers(
        journal,
        origin=gate.PRODUCTION_ORIGIN,
    )["ok"] is True


def test_network_blockers_only_ignore_declared_expected_console_error() -> None:
    journal = gate.NetworkJournal.empty()
    journal.console_messages.append(
        {
            "type": "error",
            "text": (
                "Failed to load resource: the server responded with a status "
                "of 503 (Service Unavailable)"
            ),
        }
    )
    assert gate.evaluate_network_blockers(
        journal,
        origin=gate.PRODUCTION_ORIGIN,
    )["ok"] is False
    assert gate.evaluate_network_blockers(
        journal,
        origin=gate.PRODUCTION_ORIGIN,
        ignored_console_patterns=(
            "failed to load resource: the server responded with a status of 503",
        ),
    )["ok"] is True


def test_network_blockers_accept_only_successfully_recovered_tour_images() -> None:
    journal = gate.NetworkJournal.empty()
    original_url = (
        "https://propertyquarry.com/tours/files/urban-jungle/"
        "generated-reconstruction/photo-04.jpg"
    )
    journal.responses.extend(
        [
            {
                "url": original_url,
                "resource_type": "image",
                "status": 503,
            },
            {
                "url": original_url + "?pq_asset_retry=1",
                "resource_type": "image",
                "status": 200,
            },
        ]
    )
    journal.console_messages.append(
        {
            "type": "error",
            "text": (
                "Failed to load resource: the server responded with a status "
                "of 503 (Service Unavailable)"
            ),
        }
    )

    strict = gate.evaluate_network_blockers(
        journal,
        origin=gate.PRODUCTION_ORIGIN,
    )
    recovered = gate.evaluate_network_blockers(
        journal,
        origin=gate.PRODUCTION_ORIGIN,
        allow_recovered_tour_images=True,
    )

    assert strict["ok"] is False
    assert recovered["ok"] is True
    assert recovered["recovered_tour_image_failure_count"] == 1


def test_network_blockers_reject_failed_tour_image_without_successful_retry() -> None:
    journal = gate.NetworkJournal.empty()
    journal.responses.append(
        {
            "url": (
                "https://propertyquarry.com/tours/files/urban-jungle/"
                "generated-reconstruction/photo-04.jpg"
            ),
            "resource_type": "image",
            "status": 503,
        }
    )

    result = gate.evaluate_network_blockers(
        journal,
        origin=gate.PRODUCTION_ORIGIN,
        allow_recovered_tour_images=True,
    )

    assert result["ok"] is False
    assert result["bad_http_count"] == 1
    assert result["recovered_tour_image_failure_count"] == 0


def test_network_blockers_do_not_hide_extra_console_failures_after_image_recovery() -> None:
    journal = gate.NetworkJournal.empty()
    original_url = (
        "https://propertyquarry.com/tours/files/urban-jungle/"
        "generated-reconstruction/photo-04.jpg"
    )
    journal.responses.extend(
        [
            {
                "url": original_url,
                "resource_type": "image",
                "status": 503,
            },
            {
                "url": original_url + "?pq_asset_retry=1",
                "resource_type": "image",
                "status": 200,
            },
        ]
    )
    journal.console_messages.extend(
        [
            {
                "type": "error",
                "text": (
                    "Failed to load resource: the server responded with a status "
                    "of 503 (Service Unavailable)"
                ),
            },
            {
                "type": "error",
                "text": (
                    "Failed to load resource: the server responded with a status "
                    "of 503 (Service Unavailable)"
                ),
            },
        ]
    )

    result = gate.evaluate_network_blockers(
        journal,
        origin=gate.PRODUCTION_ORIGIN,
        allow_recovered_tour_images=True,
    )

    assert result["ok"] is False
    assert result["console_blocker_count"] == 1


@pytest.mark.parametrize(
    "history",
    [
        [],
        [{"status": "running", "progress": 80}],
        [
            {"status": "running", "progress": 80},
            {"status": "running", "progress": 50},
            {"status": "completed", "progress": 100},
        ],
        [{"status": "failed", "progress": 100}],
        [{"status": "completed", "progress": 99}],
    ],
)
def test_poll_history_fails_nonterminal_failure_regression_or_incomplete(
    history: list[dict[str, object]],
) -> None:
    assert gate.evaluate_poll_history(history)["ok"] is False


def test_receipt_redaction_removes_sensitive_keys_values_and_emails() -> None:
    raw = {
        "access_token": TOKEN,
        "nested": {
            "email": EMAIL,
            "principal_id": PRINCIPAL,
            "message": (
                f"Authorization: Bearer {TOKEN}; user={EMAIL}; "
                f"owner={PRINCIPAL}"
            ),
        },
        "safe": "keep",
    }
    redacted = gate.redact_public_receipt(
        raw,
        sensitive_values=(TOKEN, EMAIL, PRINCIPAL),
    )
    encoded = json.dumps(redacted)
    assert TOKEN not in encoded
    assert EMAIL not in encoded
    assert PRINCIPAL not in encoded
    assert "access_token" not in encoded
    assert '"email"' not in encoded
    assert '"principal_id"' not in encoded
    assert redacted["safe"] == "keep"  # type: ignore[index]


def test_final_receipt_never_leaks_session_material() -> None:
    receipt = gate.finalize_public_receipt(
        {
            "contract_name": gate.CONTRACT_NAME,
            "checks": [
                {
                    "name": "secret_failure",
                    "ok": False,
                    "error": (
                        f"{TOKEN} {EMAIL} {PRINCIPAL} "
                        f"Bearer {TOKEN}"
                    ),
                }
            ],
            "cookie": f"{gate.SESSION_COOKIE_NAME}={TOKEN}",
        },
        sensitive_values=(TOKEN, EMAIL, PRINCIPAL),
    )
    encoded = json.dumps(receipt, sort_keys=True)
    assert receipt["status"] == "fail"
    assert receipt["failed_count"] == 1
    assert TOKEN not in encoded
    assert EMAIL not in encoded
    assert PRINCIPAL not in encoded
    assert gate.SESSION_COOKIE_NAME not in encoded


def test_private_har_summary_forces_mode_0600(tmp_path: Path) -> None:
    path = tmp_path / "network.har"
    path.write_text('{"log":{"entries":[]}}', encoding="utf-8")
    path.chmod(0o644)
    summary = gate.finalize_private_har(path)
    assert summary["mode"] == "0600"
    assert len(str(summary["sha256"])) == 64
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_prepare_private_har_refuses_existing_target(tmp_path: Path) -> None:
    path = tmp_path / "network.har"
    path.write_text("existing", encoding="utf-8")
    with pytest.raises(
        gate.GateSafetyError,
        match="private_har_target_must_not_exist",
    ):
        gate.prepare_private_har(path)


def test_session_reader_never_changes_file_mode(tmp_path: Path) -> None:
    path = _write_session(tmp_path / "session.json")
    before = stat.S_IMODE(os.lstat(path).st_mode)
    gate.load_internal_ci_session(path, now=NOW)
    assert stat.S_IMODE(os.lstat(path).st_mode) == before == 0o600
