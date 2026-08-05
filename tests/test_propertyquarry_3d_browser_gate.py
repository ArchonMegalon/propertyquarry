from __future__ import annotations

import inspect
import json
import subprocess

import pytest

from scripts import propertyquarry_3d_browser_gate as gate


SLUG = "verified-3dvista-provider-only"


def test_default_demo_is_the_published_karl_3dvista_tour() -> None:
    assert gate.DEFAULT_DEMO_SLUG == "karl-czerny-gasse-2-urban-jungle"


def _accepted_walkthrough_state() -> dict[str, object]:
    return {
        "video_count": 1,
        "sources": [
            {
                "src": "/tours/files/verified-3dvista-provider-only/"
                "walkthrough-mobile-720p60.mp4",
                "media": "(max-width: 760px)",
                "type": "video/mp4",
            },
            {
                "src": f"/tours/{SLUG}/walkthrough",
                "media": "",
                "type": "video/mp4",
            },
        ],
        "current_src": f"https://propertyquarry.com/tours/{SLUG}/walkthrough",
        "ready_state": 1,
        "duration_seconds": 32.0,
        "current_time_seconds": 7.0,
        "video_width": 1920,
        "video_height": 1080,
        "rendered_width": 1280.0,
        "rendered_height": 720.0,
        "body_scroll_width": 1440,
        "viewport_width": 1440,
        "mobile_media_matches": False,
        "metadata_error": "",
    }


def _provider_receipt(*, provider_status: str, rendered: bool) -> dict[str, object]:
    return {
        "contract_name": "propertyquarry.3d_browser_gate.v1",
        "generated_at": "2026-07-27T12:00:00Z",
        "browser_base_url": "https://propertyquarry.com",
        "demo_slug": SLUG,
        "providers": ["3dvista"],
        "checks": [
            {
                "name": "3dvista_rendered_viewer",
                "ok": rendered,
            },
            {
                "name": "3dvista_drag_changes_view",
                "ok": rendered,
            },
        ],
        "provider_results": [
            {
                "provider": "3dvista",
                "status": provider_status,
                "state": {
                    "provider_frame_url": (
                        f"https://propertyquarry.com/tours/3dvista/{SLUG}/index.htm"
                    )
                },
            }
        ],
    }


def test_report_only_csp_information_is_not_a_console_blocker() -> None:
    message = {
        "type": "info",
        "text": (
            "Connecting to 'https://example.invalid/' violates the following "
            "Content Security Policy directive: \"connect-src 'self'\". "
            "The policy is report-only, so the violation has been logged but "
            "no further action has been taken."
        ),
    }

    assert gate._bad_console_messages([message]) == []


def test_enforced_csp_violation_remains_a_console_blocker() -> None:
    message = {
        "type": "error",
        "text": (
            "Refused to load the script because it violates the following "
            "Content Security Policy directive: \"script-src 'self'\"."
        ),
    }

    assert gate._bad_console_messages([message]) == [message]


def test_report_only_wording_does_not_exempt_console_errors() -> None:
    message = {
        "type": "error",
        "text": (
            "Connecting to 'https://example.invalid/' violates the following "
            "Content Security Policy directive: \"connect-src 'self'\". "
            "The policy is report-only."
        ),
    }

    assert gate._bad_console_messages([message]) == [message]


def test_non_csp_failure_is_not_exempted_by_report_only_words() -> None:
    message = {
        "type": "info",
        "text": "Failed to fetch provider asset. The policy is report-only.",
    }

    assert gate._bad_console_messages([message]) == [message]


@pytest.mark.parametrize(
    ("url", "resource_type"),
    [
        (
            "https://propertyquarry.com/tours/3dvista/home/3dvista/media/map_en_3.webp?v=1",
            "xhr",
        ),
        (
            "https://propertyquarry.com/tours/3dvista/home/3dvista/skin/IconButton.png?v=1",
            "image",
        ),
    ],
)
def test_3dvista_speculative_image_abort_is_not_a_transport_failure(
    url: str,
    resource_type: str,
) -> None:
    failures = [
        {
            "url": url,
            "resource_type": resource_type,
            "failure": "net::ERR_ABORTED",
        }
    ]

    assert gate._bad_request_failures(
        failures, browser_base_url="https://propertyquarry.com"
    ) == []


def test_same_origin_non_image_xhr_abort_remains_a_browser_gate_failure() -> None:
    failures = [
        {
            "url": "https://propertyquarry.com/tours/3dvista/home/3dvista/media/config.json",
            "resource_type": "xhr",
            "failure": "net::ERR_ABORTED",
        }
    ]

    assert gate._bad_request_failures(
        failures, browser_base_url="https://propertyquarry.com"
    ) == failures


@pytest.mark.parametrize(
    ("provider_status", "rendered"),
    [
        ("fail", True),
        ("pass", False),
    ],
)
def test_failed_or_unrendered_provider_receipt_never_mutates_proof(
    monkeypatch: pytest.MonkeyPatch,
    provider_status: str,
    rendered: bool,
) -> None:
    def unexpected_mutation(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("persistence target must not be touched")

    monkeypatch.setattr(gate, "_candidate_public_tour_roots", unexpected_mutation)
    monkeypatch.setattr(
        gate,
        "persist_hosted_property_tour_browser_render_proof",
        unexpected_mutation,
    )
    monkeypatch.setattr(
        gate,
        "_persist_3dvista_browser_render_proof_in_runtime_container",
        unexpected_mutation,
    )

    result = gate.persist_3dvista_browser_render_proof_from_receipt(
        _provider_receipt(provider_status=provider_status, rendered=rendered),
        runtime_container="propertyquarry-api",
    )

    assert result == {
        "status": "provider_result_not_pass_rendered",
        "provider": "3dvista",
        "slug": SLUG,
    }


def test_rendered_but_noninteractive_provider_receipt_never_mutates_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_mutation(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("noninteractive viewer proof must not be persisted")

    receipt = _provider_receipt(provider_status="pass", rendered=True)
    next(
        row
        for row in receipt["checks"]
        if row["name"] == "3dvista_drag_changes_view"
    )["ok"] = False
    monkeypatch.setattr(gate, "_candidate_public_tour_roots", unexpected_mutation)
    monkeypatch.setattr(
        gate,
        "persist_hosted_property_tour_browser_render_proof",
        unexpected_mutation,
    )
    monkeypatch.setattr(
        gate,
        "_persist_3dvista_browser_render_proof_in_runtime_container",
        unexpected_mutation,
    )

    result = gate.persist_3dvista_browser_render_proof_from_receipt(
        receipt,
        runtime_container="propertyquarry-api",
    )

    assert result == {
        "status": "provider_result_not_pass_rendered",
        "provider": "3dvista",
        "slug": SLUG,
    }


def test_runtime_proof_persistence_executes_as_configured_container_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    proof = {
        "provider": "3dvista",
        "status": "pass",
        "rendered_viewer": True,
        "interactive_viewer": True,
        "checks": [
            {"name": "3dvista_rendered_viewer", "ok": True},
            {"name": "3dvista_drag_changes_view", "ok": True},
        ],
    }

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((list(command), dict(kwargs)))
        if command[1] == "inspect":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps([{"Config": {"User": "10001:10001"}}]),
                stderr="",
            )
        assert command[1] == "exec"
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "status": "updated",
                    "provider": "3dvista",
                    "slug": SLUG,
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(gate.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(gate.subprocess, "run", fake_run)

    result = gate._persist_3dvista_browser_render_proof_in_runtime_container(
        SLUG,
        proof,
        runtime_container="propertyquarry-api",
    )

    assert result == {
        "status": "updated",
        "slug": SLUG,
        "provider": "3dvista",
        "container": "propertyquarry-api",
        "runtime_user": "10001:10001",
    }
    assert len(calls) == 2
    inspect_command, inspect_kwargs = calls[0]
    exec_command, exec_kwargs = calls[1]
    assert inspect_command == [
        "/usr/bin/docker",
        "inspect",
        "propertyquarry-api",
    ]
    assert inspect_kwargs == {
        "check": False,
        "capture_output": True,
        "text": True,
        "timeout": 10,
    }
    assert exec_command[:8] == [
        "/usr/bin/docker",
        "exec",
        "--interactive",
        "--user",
        "10001:10001",
        "propertyquarry-api",
        "python",
        "-c",
    ]
    assert (
        "_write_hosted_property_tour_private_receipt_atomic"
        in exec_command[8]
    )
    assert all(command[1] != "cp" for command, _kwargs in calls)
    assert json.loads(str(exec_kwargs["input"])) == {
        "slug": SLUG,
        "proof": proof,
    }
    assert {key: value for key, value in exec_kwargs.items() if key != "input"} == {
        "check": False,
        "capture_output": True,
        "text": True,
        "timeout": 20,
    }


def test_runtime_proof_persistence_refuses_unconfigured_container_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps([{"Config": {"User": ""}}]),
            stderr="",
        )

    monkeypatch.setattr(gate.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(gate.subprocess, "run", fake_run)

    result = gate._persist_3dvista_browser_render_proof_in_runtime_container(
        SLUG,
        {
            "provider": "3dvista",
            "status": "pass",
            "rendered_viewer": True,
        },
        runtime_container="propertyquarry-api",
    )

    assert result["status"] == "runtime_container_user_not_configured"
    assert calls == [["/usr/bin/docker", "inspect", "propertyquarry-api"]]


def test_verified_provider_only_tour_passes_without_advertised_walkthrough() -> None:
    advertised, checks = gate._walkthrough_gate_checks(
        response_ok=True,
        response_status=200,
        provider_verified=True,
        walkthrough_state={
            "error": "walkthrough_video_missing",
            "video_count": 0,
        },
        slug=SLUG,
        viewport_width=1440,
    )

    assert advertised is False
    assert checks == [
        {
            "name": "walkthrough_optional_when_not_advertised",
            "ok": True,
            "status": 200,
            "provider_verified": True,
            "advertised": False,
            "video_count": 0,
            "reason": "accepted_walkthrough_not_advertised",
        }
    ]


def test_missing_walkthrough_does_not_cover_an_unverified_provider() -> None:
    advertised, checks = gate._walkthrough_gate_checks(
        response_ok=True,
        response_status=200,
        provider_verified=False,
        walkthrough_state={
            "error": "walkthrough_video_missing",
            "video_count": 0,
        },
        slug=SLUG,
        viewport_width=1440,
    )

    assert advertised is False
    assert checks[0]["ok"] is False
    assert checks[0]["provider_verified"] is False


def test_advertised_walkthrough_retains_every_strict_check() -> None:
    advertised, checks = gate._walkthrough_gate_checks(
        response_ok=True,
        response_status=200,
        provider_verified=True,
        walkthrough_state=_accepted_walkthrough_state(),
        slug=SLUG,
        viewport_width=1440,
    )

    assert advertised is True
    assert [row["name"] for row in checks] == [
        "walkthrough_control_page_ok",
        "walkthrough_responsive_sources_present",
        "walkthrough_current_source_matches_viewport",
        "walkthrough_metadata_decoded",
        "walkthrough_responsive_layout",
    ]
    assert all(row["ok"] for row in checks)


def test_browser_gate_initializes_walkthrough_contract_before_rendering_result() -> None:
    source = inspect.getsource(gate.build_browser_gate_receipt)
    contract_call = (
        "walkthrough_advertised, walkthrough_checks = "
        "_walkthrough_gate_checks("
    )

    assert source.count(contract_call) == 1
    assert source.index(contract_call) < source.index(
        "if walkthrough_screenshot_path and walkthrough_advertised:"
    )


def test_advertised_walkthrough_still_fails_strict_metadata_check() -> None:
    state = _accepted_walkthrough_state()
    state["duration_seconds"] = 15.0

    advertised, checks = gate._walkthrough_gate_checks(
        response_ok=True,
        response_status=200,
        provider_verified=True,
        walkthrough_state=state,
        slug=SLUG,
        viewport_width=1440,
    )

    assert advertised is True
    checks_by_name = {str(row["name"]): row for row in checks}
    assert checks_by_name["walkthrough_metadata_decoded"]["ok"] is False
    assert checks_by_name["walkthrough_responsive_sources_present"]["ok"] is True
    assert checks_by_name["walkthrough_current_source_matches_viewport"]["ok"] is True
    assert checks_by_name["walkthrough_responsive_layout"]["ok"] is True
