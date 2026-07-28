from __future__ import annotations

import os
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts import property_runtime_reconstruction_smoke as smoke


@pytest.fixture(autouse=True)
def _stub_render_bridge_runtime(monkeypatch) -> None:
    monkeypatch.setattr(
        smoke,
        "build_render_bridge_runtime_receipt",
        lambda **_: {"status": "pass", "action": "already_ready"},
    )


def _inspection_payload(
    *,
    slug: str = "runtime-smoke",
    glb_status: str = "generated",
    verified_provider_capture: bool = False,
    satisfies_verified_tour_gate: bool = False,
    public_paths: bool = True,
    route_labels: list[str] | None = None,
    walkthrough_route_labels: list[str] | None = None,
) -> dict[str, object]:
    glb_generated = glb_status == "generated"
    route_labels = route_labels or [
        "entry/hall",
        "living area",
        "sleeping area",
        "balcony/terrace",
    ]
    walkthrough_route_labels = walkthrough_route_labels or [
        "entry/hall",
        "living area",
        "sleeping area",
        "balcony/terrace",
        "living area detail 2",
    ]
    return {
        "slug": slug,
        "manifest_generated_reconstruction": {
            "provider": "propertyquarry_generated_reconstruction",
            "verified_provider_capture": verified_provider_capture,
            "satisfies_verified_tour_gate": satisfies_verified_tour_gate,
            "glb_export_status": glb_status,
            **({"glb_model_relpath": "generated-reconstruction/model.glb"} if glb_generated else {}),
            "room_stop_count": 4,
            "walkthrough_stop_count": 5,
            "photo_reference_panel_count": 5,
        },
        "receipt_provider": "propertyquarry_generated_reconstruction",
        "verified_provider_capture": verified_provider_capture,
        "satisfies_verified_tour_gate": satisfies_verified_tour_gate,
        "glb_export_status": glb_status,
        "photo_count": 5,
        "route_labels": route_labels,
        "walkthrough_route_labels": walkthrough_route_labels,
        "walkable_scene_route_labels": list(route_labels),
        "generated_room_stop_count": len(route_labels),
        "generated_walkthrough_stop_count": len(walkthrough_route_labels),
        "generated_photo_reference_panel_count": 5,
        "receipt_photo_reference_panel_count": 5,
        "walkthrough_status": "generated",
        "walkthrough_coverage_proof": {
            "status": "pass",
            "segments_expected": walkthrough_route_labels,
            "segments_visited": walkthrough_route_labels,
            "coverage_segments": [
                {"segment": label, "index": index}
                for index, label in enumerate(walkthrough_route_labels, start=1)
            ],
        },
        "paths": {
            "viewer": {"exists": public_paths, "size_bytes": 10},
            "obj": {"exists": public_paths, "size_bytes": 10},
            "mtl": {"exists": public_paths, "size_bytes": 10},
            "glb": {"exists": glb_generated, "size_bytes": 128 if glb_generated else 0},
            "receipt": {"exists": public_paths, "size_bytes": 10},
            "walkthrough_video": {"exists": public_paths, "size_bytes": 256},
            "walkthrough_sidecar": {"exists": public_paths, "size_bytes": 128},
        },
    }


def _uses_hosted_bundle_writer(script: object) -> bool:
    return "_write_generated_reconstruction_property_tour_bundle(" in str(script or "")


def _layout_viewer_quality_snapshot(
    *,
    route_stop_count: int = 4,
    view_mode: str = "overview",
    hidden_cutaway_wall_count: int = 1,
) -> dict[str, object]:
    return {
        "layout_viewer_view_mode": view_mode,
        "layout_viewer_cutaway_wall_count": 2,
        "layout_viewer_hidden_cutaway_wall_count": hidden_cutaway_wall_count,
        "layout_viewer_wall_opacity": 0.82,
        "layout_viewer_wall_height_scale": 0.82 if view_mode in {"overview", "dollhouse"} else 1.0,
        "layout_viewer_staging_object_count": route_stop_count * 2,
        "layout_viewer_visible_staging_object_count": route_stop_count,
        "layout_viewer_projected_staging_coverage_pct": 0.05,
    }


def test_runtime_reconstruction_smoke_script_imports_with_ea_pythonpath() -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = "ea"

    result = subprocess.run(
        [sys.executable, "scripts/property_runtime_reconstruction_smoke.py", "--help"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert "property_runtime_reconstruction_smoke.py" in result.stdout
    assert "--fail-on-error" in result.stdout


def test_hosted_tour_slug_matches_governed_lifecycle_boundary() -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = ".:ea"
    probe = """
import json
from app.product.property_tour_hosting import (
    GovernedPropertyTourContractError,
    GovernedPropertyTourLifecycleStore,
    _hosted_property_tour_slug,
)

identity = {
    "listing_id": "listing-42",
    "property_url": "https://example.test/listing/42",
    "variant_key": "layout_first",
}
short_slug = _hosted_property_tour_slug(title="Vienna home", **identity)
long_slug = _hosted_property_tour_slug(title="Luxury residence " * 40, **identity)
rejection = ""
try:
    GovernedPropertyTourLifecycleStore._slug(long_slug + "x")
except GovernedPropertyTourContractError as exc:
    rejection = str(exc)
print(json.dumps({
    "long_slug": long_slug,
    "normalized": GovernedPropertyTourLifecycleStore._slug(long_slug),
    "rejection": rejection,
    "short_slug": short_slug,
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert len(payload["long_slug"]) == 120
    assert payload["normalized"] == payload["long_slug"]
    assert payload["rejection"] == "governed_tour_slug_invalid"
    assert payload["short_slug"].startswith("vienna-home-layout-first-")
    assert payload["long_slug"].endswith(
        payload["short_slug"].removeprefix("vienna-home")
    )


def test_layout_viewer_wait_polls_snapshots_without_route_index_requirement(monkeypatch) -> None:
    waits: list[int] = []

    class FakePage:
        def wait_for_timeout(self, timeout_ms: int) -> None:
            waits.append(timeout_ms)

    monkeypatch.setattr(
        smoke,
        "_generated_reconstruction_layout_viewer_snapshot",
        lambda page: {
            "layout_viewer_ready": True,
            "layout_viewer_metrics_ready": True,
            "layout_viewer_route_stop_count": 4,
            "layout_viewer_route_button_count": 4,
            "layout_viewer_floorplan_stop_count": 4,
            "layout_viewer_render_calls": 2,
            "layout_viewer_render_triangles": 2,
            "layout_viewer_photo_panel_count": 5,
            "layout_viewer_loaded_photo_texture_count": 5,
            "layout_viewer_active_route_index": 2,
            **_layout_viewer_quality_snapshot(),
        },
    )

    smoke._wait_for_generated_reconstruction_layout_viewer_state(
        FakePage(),
        expected_route_stop_count=4,
        expected_photo_count=5,
        expected_active_route_index=None,
        timeout=1234,
    )

    assert waits == []


def test_freeze_tour_video_at_start_pauses_and_syncs_route() -> None:
    calls: list[str] = []

    class FakePage:
        def evaluate(self, expression: str) -> bool:
            calls.append(expression)
            return True

    assert smoke._freeze_tour_video_at_start(FakePage()) is True
    assert calls
    assert "video.pause()" in calls[0]
    assert "video.currentTime = 0" in calls[0]
    assert "dispatchEvent" not in calls[0]


def test_stabilize_generated_reconstruction_viewer_bootstrap_uses_fixed_settle_window() -> None:
    calls: list[int] = []

    class FakePage:
        def wait_for_timeout(self, timeout_ms: int) -> None:
            calls.append(timeout_ms)

    smoke._stabilize_generated_reconstruction_viewer_bootstrap(FakePage())

    assert calls == [5_000]


def test_layout_viewer_wait_polls_snapshots_for_active_route_changes(monkeypatch) -> None:
    waits: list[int] = []
    snapshots = iter(
        [
            {
                "layout_viewer_ready": True,
                "layout_viewer_metrics_ready": True,
                "layout_viewer_route_stop_count": 4,
                "layout_viewer_route_button_count": 4,
                "layout_viewer_floorplan_stop_count": 4,
                "layout_viewer_render_calls": 1,
                "layout_viewer_render_triangles": 1,
                "layout_viewer_photo_panel_count": 5,
                "layout_viewer_loaded_photo_texture_count": 5,
                "layout_viewer_active_route_index": 0,
                **_layout_viewer_quality_snapshot(view_mode="room", hidden_cutaway_wall_count=0),
            },
            {
                "layout_viewer_ready": True,
                "layout_viewer_metrics_ready": True,
                "layout_viewer_route_stop_count": 4,
                "layout_viewer_route_button_count": 4,
                "layout_viewer_floorplan_stop_count": 4,
                "layout_viewer_render_calls": 2,
                "layout_viewer_render_triangles": 2,
                "layout_viewer_photo_panel_count": 5,
                "layout_viewer_loaded_photo_texture_count": 5,
                "layout_viewer_active_route_index": 1,
                **_layout_viewer_quality_snapshot(view_mode="room", hidden_cutaway_wall_count=0),
            },
        ]
    )

    class FakePage:
        def wait_for_timeout(self, timeout_ms: int) -> None:
            waits.append(timeout_ms)

    monkeypatch.setattr(smoke, "_generated_reconstruction_layout_viewer_snapshot", lambda page: next(snapshots))

    smoke._wait_for_generated_reconstruction_layout_viewer_state(
        FakePage(),
        expected_route_stop_count=4,
        expected_photo_count=5,
        expected_active_route_index=1,
        timeout=1000,
    )

    assert waits == [250]


def test_layout_viewer_state_matches_accepts_route_index_zero() -> None:
    assert smoke._generated_reconstruction_layout_viewer_state_matches(
        {
            "layout_viewer_ready": True,
            "layout_viewer_metrics_ready": True,
            "layout_viewer_route_stop_count": 4,
            "layout_viewer_route_button_count": 4,
            "layout_viewer_floorplan_stop_count": 4,
            "layout_viewer_render_calls": 1,
            "layout_viewer_render_triangles": 1,
            "layout_viewer_photo_panel_count": 5,
            "layout_viewer_loaded_photo_texture_count": 5,
            "layout_viewer_active_route_index": 0,
            **_layout_viewer_quality_snapshot(),
        },
        expected_route_stop_count=4,
        expected_photo_count=5,
        expected_active_route_index=0,
    )


def test_layout_viewer_state_matches_rejects_missing_staging_quality() -> None:
    assert not smoke._generated_reconstruction_layout_viewer_state_matches(
        {
            "layout_viewer_ready": True,
            "layout_viewer_metrics_ready": True,
            "layout_viewer_route_stop_count": 4,
            "layout_viewer_route_button_count": 4,
            "layout_viewer_floorplan_stop_count": 4,
            "layout_viewer_render_calls": 1,
            "layout_viewer_render_triangles": 1,
            "layout_viewer_photo_panel_count": 5,
            "layout_viewer_loaded_photo_texture_count": 5,
            "layout_viewer_active_route_index": 0,
            "layout_viewer_cutaway_wall_count": 2,
            "layout_viewer_staging_object_count": 2,
            "layout_viewer_visible_staging_object_count": 1,
            "layout_viewer_projected_staging_coverage_pct": 0.0,
        },
        expected_route_stop_count=4,
        expected_photo_count=5,
        expected_active_route_index=0,
    )


def test_runtime_reconstruction_smoke_passes_when_container_generates_glb(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)

    calls: list[list[str]] = []

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        script = command[-1]
        if _uses_hosted_bundle_writer(script):
            return subprocess.CompletedProcess(command, 0, stdout='{"slug":"runtime-smoke"}\n', stderr="")
        payload = _inspection_payload()
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload) + "\n", stderr="")

    monkeypatch.setattr(smoke, "_run", _fake_run)

    receipt = smoke.build_runtime_reconstruction_receipt(container="propertyquarry-api", slug="runtime-smoke")

    assert receipt["status"] == "pass"
    assert receipt["required_paths_ok"] is True
    assert receipt["glb_non_empty"] is True
    assert receipt["honest_disclosure_ok"] is True
    assert receipt["glb_manifest_ok"] is True
    assert len(calls) == 2
    assert all(call[:4] == ["docker", "exec", "propertyquarry-api", "python"] for call in calls)
    assert all("sh" not in call for call in calls)


def test_runtime_reconstruction_smoke_uses_hosted_bundle_writer_and_clears_materialized_slug(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)
    observed: dict[str, object] = {}

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        script = command[-1]
        if _uses_hosted_bundle_writer(script):
            observed["script"] = script
            return subprocess.CompletedProcess(command, 0, stdout='{"slug":"runtime-smoke"}\n', stderr="")
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(_inspection_payload()) + "\n", stderr="")

    monkeypatch.setattr(smoke, "_run", _fake_run)

    receipt = smoke.build_runtime_reconstruction_receipt(container="propertyquarry-api", slug="runtime-smoke")

    assert receipt["status"] == "pass"
    setup_script = str(observed.get("script") or "")
    assert "_write_generated_reconstruction_property_tour_bundle(" in setup_script
    assert "_make_hosted_property_tour_slug(" in setup_script
    assert "materialized_slug" in setup_script
    assert "shutil.rmtree(Path('/data/public_property_tours') / materialized_slug, ignore_errors=True)" in setup_script
    assert "src.mkdir(parents=True, exist_ok=True)" in setup_script
    assert "PROPERTYQUARRY_RECONSTRUCTION_WALKTHROUGH_SECONDS_PER_STOP'] = '5'" in setup_script
    assert "PROPERTYQUARRY_RECONSTRUCTION_FFMPEG_TIMEOUT_SECONDS'] = '420'" in setup_script


def test_runtime_reconstruction_smoke_uses_materialized_slug_for_receipt_urls(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        script = command[-1]
        if _uses_hosted_bundle_writer(script):
            return subprocess.CompletedProcess(command, 0, stdout='{"slug":"materialized-runtime-smoke"}\n', stderr="")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(_inspection_payload(slug="materialized-runtime-smoke")) + "\n",
            stderr="",
        )

    monkeypatch.setattr(smoke, "_run", _fake_run)
    monkeypatch.setattr(smoke, "_check_generated_reconstruction_public_contract", lambda **_: {"status": "pass"})

    receipt = smoke.build_runtime_reconstruction_receipt(
        container="propertyquarry-api",
        slug="requested-runtime-smoke",
        public_base_url="https://propertyquarry.com",
        require_public_contract=True,
    )

    assert receipt["status"] == "pass"
    assert receipt["requested_slug"] == "requested-runtime-smoke"
    assert receipt["slug"] == "materialized-runtime-smoke"
    assert receipt["viewer_url"] == "https://propertyquarry.com/tours/files/materialized-runtime-smoke/generated-reconstruction/viewer.html"


def test_runtime_reconstruction_smoke_uses_longer_timeout_for_render_tools(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)

    observed: list[int] = []

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        observed.append(timeout)
        script = command[-1]
        if _uses_hosted_bundle_writer(script):
            return subprocess.CompletedProcess(command, 0, stdout='{"slug":"runtime-smoke"}\n', stderr="")
        payload = _inspection_payload()
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload) + "\n", stderr="")

    monkeypatch.setattr(smoke, "_run", _fake_run)

    receipt = smoke.build_runtime_reconstruction_receipt(container="propertyquarry-render-tools", slug="runtime-smoke")

    assert receipt["status"] == "pass"
    assert observed[0] == 420


def test_runtime_reconstruction_smoke_reports_generation_timeout_cleanly(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, timeout=timeout, output="partial-stdout", stderr="partial-stderr")

    monkeypatch.setattr(smoke, "_run", _fake_run)

    receipt = smoke.build_runtime_reconstruction_receipt(container="propertyquarry-render-tools", slug="runtime-smoke")

    assert receipt["status"] == "failed"
    assert receipt["reason"] == "runtime_reconstruction_command_timeout"
    assert receipt["timeout_seconds"] == 420
    assert "partial-stdout" in receipt["stdout_tail"]
    assert "partial-stderr" in receipt["stderr_tail"]


def test_runtime_reconstruction_smoke_fails_when_render_bridge_runtime_cannot_be_materialized(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)
    monkeypatch.setattr(
        smoke,
        "build_render_bridge_runtime_receipt",
        lambda **_: {"status": "failed", "reason": "render_bridge_runtime_not_ready"},
    )

    receipt = smoke.build_runtime_reconstruction_receipt(container="propertyquarry-render-tools", slug="runtime-smoke")

    assert receipt["status"] == "failed"
    assert receipt["reason"] == "render_bridge_runtime_unavailable"
    assert receipt["render_bridge_runtime"]["reason"] == "render_bridge_runtime_not_ready"


def test_runtime_reconstruction_smoke_passes_without_glb_when_glb_is_not_required(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        script = command[-1]
        if _uses_hosted_bundle_writer(script):
            return subprocess.CompletedProcess(command, 0, stdout='{"slug":"runtime-smoke"}\n', stderr="")
        payload = _inspection_payload(glb_status="skipped")
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload) + "\n", stderr="")

    monkeypatch.setattr(smoke, "_run", _fake_run)

    receipt = smoke.build_runtime_reconstruction_receipt(container="propertyquarry-api", slug="runtime-smoke")

    assert receipt["status"] == "pass"
    assert receipt["required_paths_ok"] is True
    assert receipt["glb_non_empty"] is False
    assert receipt["glb_manifest_ok"] is False
    assert receipt["glb_required"] is False
    assert receipt["glb_capability_ok"] is True


def test_runtime_reconstruction_smoke_fails_without_glb_when_glb_is_required(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        script = command[-1]
        if _uses_hosted_bundle_writer(script):
            return subprocess.CompletedProcess(command, 0, stdout='{"slug":"runtime-smoke"}\n', stderr="")
        payload = _inspection_payload(glb_status="skipped")
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload) + "\n", stderr="")

    monkeypatch.setattr(smoke, "_run", _fake_run)

    receipt = smoke.build_runtime_reconstruction_receipt(
        container="propertyquarry-api",
        slug="runtime-smoke",
        require_glb=True,
    )

    assert receipt["status"] == "failed"
    assert receipt["required_paths_ok"] is False
    assert receipt["glb_required"] is True
    assert receipt["glb_capability_ok"] is False


def test_runtime_reconstruction_smoke_fails_when_generated_asset_claims_verified(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        script = command[-1]
        if _uses_hosted_bundle_writer(script):
            return subprocess.CompletedProcess(command, 0, stdout='{"slug":"runtime-smoke"}\n', stderr="")
        payload = _inspection_payload(
            verified_provider_capture=True,
            satisfies_verified_tour_gate=True,
        )
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload) + "\n", stderr="")

    monkeypatch.setattr(smoke, "_run", _fake_run)

    receipt = smoke.build_runtime_reconstruction_receipt(container="propertyquarry-api", slug="runtime-smoke")

    assert receipt["status"] == "failed"
    assert receipt["honest_disclosure_ok"] is False


def test_runtime_reconstruction_smoke_fails_when_required_public_contract_base_url_missing(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        script = command[-1]
        if _uses_hosted_bundle_writer(script):
            return subprocess.CompletedProcess(command, 0, stdout='{"slug":"runtime-smoke"}\n', stderr="")
        payload = _inspection_payload()
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload) + "\n", stderr="")

    monkeypatch.setattr(smoke, "_run", _fake_run)

    receipt = smoke.build_runtime_reconstruction_receipt(
        container="propertyquarry-api",
        slug="runtime-smoke",
        require_browser=True,
    )

    assert receipt["status"] == "failed"
    assert receipt["public_route_contract_ok"] is False
    assert receipt["public_route_contract"]["reason"] == "public_base_url_missing"


def test_resolved_local_public_base_url_rewrites_internal_loopback_port_to_published_host_port(monkeypatch) -> None:
    monkeypatch.setattr(smoke, "_docker_published_host_port", lambda container, *, container_port=8090: 8097)

    resolved = smoke._resolved_local_public_base_url(
        "http://127.0.0.1:8090",
        public_container="propertyquarry-api",
    )

    assert resolved == "http://127.0.0.1:8097"


def test_runtime_reconstruction_smoke_requires_public_route_rejection_when_public_base_url_is_set(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        script = command[-1]
        if _uses_hosted_bundle_writer(script):
            return subprocess.CompletedProcess(command, 0, stdout='{"slug":"runtime-smoke"}\n', stderr="")
        payload = _inspection_payload()
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload) + "\n", stderr="")

    observed: dict[str, str] = {}

    def _fake_public_contract(*, public_base_url: str, slug: str, host_header: str = "") -> dict[str, object]:
        observed["public_base_url"] = public_base_url
        observed["slug"] = slug
        observed["host_header"] = host_header
        return {"status": "failed", "failures": ["viewer_not_routed_to_clean_shell"]}

    monkeypatch.setattr(smoke, "_run", _fake_run)
    monkeypatch.setattr(smoke, "_check_generated_reconstruction_public_contract", _fake_public_contract)

    receipt = smoke.build_runtime_reconstruction_receipt(
        container="propertyquarry-api",
        slug="runtime-smoke",
        public_base_url="https://propertyquarry.com",
        host_header="propertyquarry.com",
        require_public_contract=True,
    )

    assert observed == {
        "public_base_url": "https://propertyquarry.com",
        "slug": "runtime-smoke",
        "host_header": "propertyquarry.com",
    }
    assert receipt["status"] == "failed"
    assert receipt["public_route_contract_ok"] is False
    assert receipt["public_route_contract"]["failures"] == ["viewer_not_routed_to_clean_shell"]


def test_runtime_reconstruction_smoke_rewrites_loopback_public_base_url_before_public_contract_probe(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        script = command[-1]
        if _uses_hosted_bundle_writer(script):
            return subprocess.CompletedProcess(command, 0, stdout='{"slug":"runtime-smoke"}\n', stderr="")
        payload = _inspection_payload()
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload) + "\n", stderr="")

    observed: dict[str, str] = {}

    def _fake_public_contract(*, public_base_url: str, slug: str, host_header: str = "") -> dict[str, object]:
        observed["public_base_url"] = public_base_url
        observed["slug"] = slug
        observed["host_header"] = host_header
        return {"status": "failed", "failures": ["viewer_not_routed_to_clean_shell"]}

    monkeypatch.setattr(smoke, "_run", _fake_run)
    monkeypatch.setattr(smoke, "_docker_published_host_port", lambda container, *, container_port=8090: 8097)
    monkeypatch.setattr(smoke, "_check_generated_reconstruction_public_contract", _fake_public_contract)

    receipt = smoke.build_runtime_reconstruction_receipt(
        container="propertyquarry-api",
        slug="runtime-smoke",
        public_base_url="http://127.0.0.1:8090",
        host_header="propertyquarry.com",
        require_public_contract=True,
    )

    assert observed == {
        "public_base_url": "http://127.0.0.1:8097",
        "slug": "runtime-smoke",
        "host_header": "propertyquarry.com",
    }
    assert receipt["status"] == "failed"
    assert receipt["resolved_public_base_url"] == "http://127.0.0.1:8097"
    assert receipt["public_route_contract_ok"] is False


def test_runtime_reconstruction_smoke_syncs_container_tour_to_local_public_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    slug = "runtime-smoke-layout-first"
    host_root = tmp_path / "public-tours"
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(host_root))

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        assert command[:2] == ["docker", "cp"]
        copied = Path(command[-1]) / slug
        copied.mkdir(parents=True)
        (copied / "tour.json").write_text('{"slug":"runtime-smoke-layout-first"}\n', encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(smoke, "_run", _fake_run)

    receipt = smoke._sync_container_tour_to_host_root(
        "propertyquarry-render-tools",
        slug=slug,
        public_base_url="http://127.0.0.1:8099",
    )

    assert receipt["status"] == "pass"
    assert receipt["destination"] == str(host_root / slug)
    assert (host_root / slug / "tour.json").is_file()


def test_runtime_reconstruction_smoke_fails_when_required_browser_shell_base_url_missing(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        script = command[-1]
        if _uses_hosted_bundle_writer(script):
            return subprocess.CompletedProcess(command, 0, stdout='{"slug":"runtime-smoke"}\n', stderr="")
        payload = _inspection_payload()
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload) + "\n", stderr="")

    monkeypatch.setattr(smoke, "_run", _fake_run)

    receipt = smoke.build_runtime_reconstruction_receipt(
        container="propertyquarry-api",
        slug="runtime-smoke",
        require_browser_shell=True,
    )

    assert receipt["status"] == "failed"
    assert receipt["browser_shell_ok"] is False
    assert receipt["browser_shell"]["reason"] == "public_base_url_missing"


def test_runtime_reconstruction_smoke_requires_browser_shell_when_requested(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        script = command[-1]
        if _uses_hosted_bundle_writer(script):
            return subprocess.CompletedProcess(command, 0, stdout='{"slug":"runtime-smoke"}\n', stderr="")
        payload = _inspection_payload()
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload) + "\n", stderr="")

    observed: dict[str, object] = {}

    def _fake_browser_shell(
        *,
        public_base_url: str,
        slug: str,
        host_header: str,
        expected_route_stop_count: int,
        expected_photo_count: int,
        expected_route_labels: list[str] | None = None,
    ) -> dict[str, object]:
        observed.update(
            {
                "public_base_url": public_base_url,
                "slug": slug,
                "host_header": host_header,
                "expected_route_stop_count": expected_route_stop_count,
                "expected_photo_count": expected_photo_count,
                "expected_route_labels": list(expected_route_labels or []),
            }
        )
        return {"status": "failed", "failures": ["launch_shell_video_source_wrong"]}

    monkeypatch.setattr(smoke, "_run", _fake_run)
    monkeypatch.setattr(smoke, "_run_bounded_browser_shell_probe", _fake_browser_shell)

    receipt = smoke.build_runtime_reconstruction_receipt(
        container="propertyquarry-api",
        slug="runtime-smoke",
        public_base_url="http://127.0.0.1:8097",
        host_header="propertyquarry.com",
        require_browser_shell=True,
    )

    assert observed == {
        "public_base_url": "http://127.0.0.1:8097",
        "slug": "runtime-smoke",
        "host_header": "propertyquarry.com",
        "expected_route_stop_count": 4,
        "expected_photo_count": 5,
        "expected_route_labels": [
            "entry/hall",
            "living area",
            "sleeping area",
            "balcony/terrace",
        ],
    }
    assert receipt["status"] == "failed"
    assert receipt["browser_shell_ok"] is False
    assert receipt["browser_shell"]["failures"] == ["launch_shell_video_source_wrong"]


def test_runtime_reconstruction_smoke_bounds_browser_shell_probe_timeout(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_RUNTIME_RECONSTRUCTION_BROWSER_SHELL_TIMEOUT_SECONDS", "1")
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        script = command[-1]
        if _uses_hosted_bundle_writer(script):
            return subprocess.CompletedProcess(command, 0, stdout='{"slug":"runtime-smoke"}\n', stderr="")
        payload = _inspection_payload()
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload) + "\n", stderr="")

    def _hung_browser_shell(**kwargs) -> dict[str, object]:
        time.sleep(5)
        return {"status": "pass"}

    monkeypatch.setattr(smoke, "_run", _fake_run)
    monkeypatch.setattr(smoke, "_check_generated_reconstruction_browser_shell", _hung_browser_shell)

    receipt = smoke.build_runtime_reconstruction_receipt(
        container="propertyquarry-api",
        slug="runtime-smoke",
        public_base_url="http://127.0.0.1:8097",
        host_header="propertyquarry.com",
        require_browser_shell=True,
    )

    assert receipt["status"] == "failed"
    assert receipt["browser_shell_ok"] is False
    assert receipt["browser_shell"]["status"] == "failed"
    assert receipt["browser_shell"]["reason"] == "browser_shell_probe_timeout"
    assert receipt["browser_shell"]["timeout_seconds"] == 1
    assert receipt["browser_shell"]["failures"] == ["browser_shell_probe_timeout"]


def test_runtime_reconstruction_smoke_passes_when_required_browser_shell_is_green(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        script = command[-1]
        if _uses_hosted_bundle_writer(script):
            return subprocess.CompletedProcess(command, 0, stdout='{"slug":"runtime-smoke"}\n', stderr="")
        payload = _inspection_payload()
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload) + "\n", stderr="")

    monkeypatch.setattr(smoke, "_run", _fake_run)
    monkeypatch.setattr(
        smoke,
        "_run_bounded_browser_shell_probe",
        lambda **kwargs: {"status": "pass", "browser_base_url": "http://propertyquarry.com:8097"},
    )

    receipt = smoke.build_runtime_reconstruction_receipt(
        container="propertyquarry-api",
        slug="runtime-smoke",
        public_base_url="http://127.0.0.1:8097",
        host_header="propertyquarry.com",
        require_browser_shell=True,
    )

    assert receipt["status"] == "pass"
    assert receipt["browser_shell_ok"] is True
    assert receipt["browser_shell"]["status"] == "pass"


def test_generated_reconstruction_browser_shell_layout_failures_pass_with_diorama_and_viewer_sync() -> None:
    launch_shell = {
        "lead_preview_src": "/tours/files/runtime-smoke/diorama-preview.png",
        "layout_viewer_ready": True,
        "layout_viewer_route_button_count": 4,
        "layout_viewer_floorplan_stop_count": 4,
        "layout_viewer_route_stop_count": 4,
        "layout_viewer_photo_panel_count": 5,
        "layout_viewer_loaded_photo_texture_count": 5,
        "layout_viewer_active_route_index": 0,
        "layout_viewer_active_route_index_after_route_click": 1,
        "layout_viewer_active_route_index_after_last_route_click": 3,
        "layout_viewer_active_route_index_after_timeupdate_sync": 3,
        **_layout_viewer_quality_snapshot(),
    }
    layout_preview = {
        "lead_preview_src": "/tours/files/runtime-smoke/diorama-preview.png",
        "layout_viewer_ready": True,
        "layout_viewer_route_button_count": 4,
        "layout_viewer_floorplan_stop_count": 4,
        "layout_viewer_route_stop_count": 4,
        "layout_viewer_photo_panel_count": 5,
        "layout_viewer_loaded_photo_texture_count": 5,
        "layout_viewer_active_route_index": 0,
        **_layout_viewer_quality_snapshot(),
    }

    failures = smoke._generated_reconstruction_browser_shell_layout_failures(
        slug="runtime-smoke",
        launch_shell=launch_shell,
        layout_preview=layout_preview,
        expected_route_stop_count=4,
        expected_photo_count=5,
    )

    assert failures == []


def test_generated_reconstruction_browser_shell_layout_failures_report_diorama_and_sync_gaps() -> None:
    failures = smoke._generated_reconstruction_browser_shell_layout_failures(
        slug="runtime-smoke",
        launch_shell={
            "lead_preview_src": "/tours/files/runtime-smoke/listing-photo.png",
            "layout_viewer_ready": False,
            "layout_viewer_route_button_count": 2,
            "layout_viewer_floorplan_stop_count": 1,
            "layout_viewer_route_stop_count": 2,
            "layout_viewer_photo_panel_count": 3,
            "layout_viewer_loaded_photo_texture_count": 1,
            "layout_viewer_active_route_index": -1,
            "layout_viewer_active_route_index_after_route_click": 0,
            "layout_viewer_active_route_index_after_last_route_click": 1,
            "layout_viewer_active_route_index_after_timeupdate_sync": 1,
            "layout_viewer_view_mode": "overview",
            "layout_viewer_cutaway_wall_count": 0,
            "layout_viewer_hidden_cutaway_wall_count": 0,
            "layout_viewer_wall_height_scale": 1.0,
            "layout_viewer_staging_object_count": 1,
            "layout_viewer_visible_staging_object_count": 0,
            "layout_viewer_projected_staging_coverage_pct": 0.0,
        },
        layout_preview={
            "lead_preview_src": "/tours/files/runtime-smoke/listing-photo.png",
            "layout_viewer_ready": False,
            "layout_viewer_route_button_count": 2,
            "layout_viewer_floorplan_stop_count": 2,
            "layout_viewer_route_stop_count": 2,
            "layout_viewer_photo_panel_count": 4,
            "layout_viewer_loaded_photo_texture_count": 2,
            "layout_viewer_active_route_index": -1,
            "layout_viewer_view_mode": "overview",
            "layout_viewer_cutaway_wall_count": 0,
            "layout_viewer_hidden_cutaway_wall_count": 0,
            "layout_viewer_wall_height_scale": 1.0,
            "layout_viewer_staging_object_count": 1,
            "layout_viewer_visible_staging_object_count": 0,
            "layout_viewer_projected_staging_coverage_pct": 0.0,
        },
        expected_route_stop_count=4,
        expected_photo_count=5,
    )

    assert "launch_shell_lead_preview_not_diorama" in failures
    assert "launch_shell_layout_viewer_not_ready" in failures
    assert "launch_shell_layout_viewer_route_button_count_wrong" in failures
    assert "launch_shell_layout_viewer_floorplan_stop_count_wrong" in failures
    assert "launch_shell_layout_viewer_route_stop_count_wrong" in failures
    assert "launch_shell_layout_viewer_photo_panel_count_wrong" in failures
    assert "launch_shell_layout_viewer_photo_textures_incomplete" in failures
    assert "launch_shell_layout_viewer_initial_route_wrong" in failures
    assert "launch_shell_layout_viewer_route_click_sync_wrong" in failures
    assert "launch_shell_layout_viewer_last_route_sync_wrong" in failures
    assert "launch_shell_layout_viewer_timeupdate_sync_wrong" in failures
    assert "launch_shell_layout_viewer_staging_object_count_low" in failures
    assert "launch_shell_layout_viewer_visible_staging_missing" in failures
    assert "launch_shell_layout_viewer_projected_staging_coverage_low" in failures
    assert "launch_shell_layout_viewer_cutaway_wall_count_missing" in failures
    assert "launch_shell_layout_viewer_hidden_cutaway_wall_count_missing" in failures
    assert "launch_shell_layout_viewer_wall_height_scale_not_cutaway" in failures
    assert "layout_preview_lead_preview_not_diorama" in failures
    assert "layout_preview_layout_viewer_not_ready" in failures
    assert "layout_preview_layout_viewer_route_button_count_wrong" in failures
    assert "layout_preview_layout_viewer_route_stop_count_wrong" in failures
    assert "layout_preview_layout_viewer_photo_panel_count_wrong" in failures
    assert "layout_preview_layout_viewer_photo_textures_incomplete" in failures
    assert "layout_preview_layout_viewer_initial_route_wrong" in failures
    assert "layout_preview_layout_viewer_staging_object_count_low" in failures
    assert "layout_preview_layout_viewer_visible_staging_missing" in failures
    assert "layout_preview_layout_viewer_projected_staging_coverage_low" in failures
    assert "layout_preview_layout_viewer_cutaway_wall_count_missing" in failures
    assert "layout_preview_layout_viewer_hidden_cutaway_wall_count_missing" in failures
    assert "layout_preview_layout_viewer_wall_height_scale_not_cutaway" in failures


def test_generated_reconstruction_shell_variant_failures_accept_launch_and_layout_preview_contracts() -> None:
    assert (
        smoke._generated_reconstruction_shell_variant_failures(
            shell_name="launch_shell",
            snapshot={
                "launch_mode": "tour_public_launch",
                "hero_eyebrow_text": "PropertyQuarry styled 3D reconstruction",
                "primary_cta_href": "#layout-viewer",
                "secondary_cta_href": "#walkthrough",
            },
        )
        == []
    )
    assert (
        smoke._generated_reconstruction_shell_variant_failures(
            shell_name="layout_preview",
            snapshot={
                "launch_mode": "layout_preview",
                "hero_eyebrow_text": "PropertyQuarry styled 3D reconstruction",
                "primary_cta_href": "#layout-viewer",
                "secondary_cta_href": "#walkthrough",
            },
        )
        == []
    )


def test_generated_reconstruction_shell_variant_failures_report_layout_preview_contract_gaps() -> None:
    failures = smoke._generated_reconstruction_shell_variant_failures(
        shell_name="layout_preview",
        snapshot={
            "launch_mode": "tour_public_launch",
            "hero_eyebrow_text": "PropertyQuarry layout preview",
            "primary_cta_href": "#walkthrough",
            "secondary_cta_href": "#reference-focus",
        },
    )

    assert "layout_preview_launch_mode_wrong" in failures
    assert "layout_preview_heading_wrong" in failures
    assert "layout_preview_primary_cta_wrong" in failures
    assert "layout_preview_secondary_cta_wrong" in failures


def test_runtime_reconstruction_smoke_fails_when_route_labels_collapse_to_generic_placeholders(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        script = command[-1]
        if _uses_hosted_bundle_writer(script):
            return subprocess.CompletedProcess(command, 0, stdout='{"slug":"runtime-smoke"}\n', stderr="")
        payload = _inspection_payload()
        payload["route_labels"] = ["room stop 1", "room stop 2", "room stop 3", "room stop 4"]
        payload["walkable_scene_route_labels"] = list(payload["route_labels"])
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload) + "\n", stderr="")

    monkeypatch.setattr(smoke, "_run", _fake_run)

    receipt = smoke.build_runtime_reconstruction_receipt(container="propertyquarry-api", slug="runtime-smoke")

    assert receipt["status"] == "failed"
    assert receipt["route_label_quality_ok"] is False


def test_runtime_reconstruction_smoke_fails_when_walkthrough_does_not_expand_to_cover_all_photos(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        script = command[-1]
        if _uses_hosted_bundle_writer(script):
            return subprocess.CompletedProcess(command, 0, stdout='{"slug":"runtime-smoke"}\n', stderr="")
        payload = _inspection_payload()
        payload["walkthrough_route_labels"] = list(payload["route_labels"])
        payload["generated_walkthrough_stop_count"] = len(payload["walkthrough_route_labels"])
        payload["walkthrough_coverage_proof"] = {
            "status": "pass",
            "segments_expected": list(payload["walkthrough_route_labels"]),
        }
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload) + "\n", stderr="")

    monkeypatch.setattr(smoke, "_run", _fake_run)

    receipt = smoke.build_runtime_reconstruction_receipt(container="propertyquarry-api", slug="runtime-smoke")

    assert receipt["status"] == "failed"
    assert receipt["walkthrough_label_quality_ok"] is False


def test_runtime_reconstruction_smoke_fails_when_coverage_proof_does_not_prove_visited_segments(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        script = command[-1]
        if _uses_hosted_bundle_writer(script):
            return subprocess.CompletedProcess(command, 0, stdout='{"slug":"runtime-smoke"}\n', stderr="")
        payload = _inspection_payload()
        payload["walkthrough_coverage_proof"] = {
            "status": "pass",
            "segments_expected": list(payload["walkthrough_route_labels"]),
        }
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload) + "\n", stderr="")

    monkeypatch.setattr(smoke, "_run", _fake_run)

    receipt = smoke.build_runtime_reconstruction_receipt(container="propertyquarry-api", slug="runtime-smoke")

    assert receipt["status"] == "failed"
    assert receipt["walkthrough_generated_ok"] is False


def test_runtime_reconstruction_smoke_accepts_staircase_led_route_without_detail_when_photo_count_is_covered(
    monkeypatch,
) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        script = command[-1]
        if _uses_hosted_bundle_writer(script):
            return subprocess.CompletedProcess(command, 0, stdout='{"slug":"runtime-smoke"}\n', stderr="")
        route_labels = [
            "staircase",
            "living kitchen",
            "living room",
            "bedroom",
            "bedroom 2",
            "bedroom 3",
            "balcony/terrace",
        ]
        payload = _inspection_payload(
            route_labels=route_labels,
            walkthrough_route_labels=list(route_labels),
        )
        payload["walkable_scene_route_labels"] = list(route_labels)
        payload["generated_walkthrough_coverage_proof"] = payload["walkthrough_coverage_proof"]
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload) + "\n", stderr="")

    monkeypatch.setattr(smoke, "_run", _fake_run)

    receipt = smoke.build_runtime_reconstruction_receipt(container="propertyquarry-api", slug="runtime-smoke")

    assert receipt["status"] == "pass"
    assert receipt["route_label_quality_ok"] is True
    assert receipt["walkthrough_label_quality_ok"] is True
    assert receipt["walkthrough_generated_ok"] is True


def test_runtime_reconstruction_smoke_accepts_bedroom_led_route_without_hall_when_labels_are_still_specific(
    monkeypatch,
) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        script = command[-1]
        if _uses_hosted_bundle_writer(script):
            return subprocess.CompletedProcess(command, 0, stdout='{"slug":"runtime-smoke"}\n', stderr="")
        route_labels = [
            "living kitchen",
            "living room",
            "bedroom",
            "bedroom 2",
            "bedroom 3",
            "balcony/terrace",
        ]
        payload = _inspection_payload(
            route_labels=route_labels,
            walkthrough_route_labels=list(route_labels),
        )
        payload["walkable_scene_route_labels"] = list(route_labels)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload) + "\n", stderr="")

    monkeypatch.setattr(smoke, "_run", _fake_run)

    receipt = smoke.build_runtime_reconstruction_receipt(container="propertyquarry-api", slug="runtime-smoke")

    assert receipt["status"] == "pass"
    assert receipt["route_label_quality_ok"] is True
    assert receipt["walkthrough_label_quality_ok"] is True
    assert receipt["walkthrough_generated_ok"] is True


def test_generated_reconstruction_public_contract_requires_clean_shell_redirect_and_private_model_gone(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def _fake_probe(url: str, *, host_header: str = "") -> dict[str, object]:
        calls.append((url, host_header))
        if url.endswith("/generated-reconstruction/viewer.html"):
            return {"status_code": 302, "location": "/tours/runtime-smoke", "body_excerpt": ""}
        if url.endswith("/generated-reconstruction/model.obj"):
            return {"status_code": 410, "location": "", "body_excerpt": "This generated model is not a public 3D tour."}
        if url.endswith("/runtime-smoke.json"):
            return {"status_code": 200, "location": "", "body_excerpt": '{"slug":"runtime-smoke"}'}
        return {
            "status_code": 200,
            "location": "",
            "body_excerpt": "PropertyQuarry styled 3D reconstruction Generated reconstruction Room route Reference deck",
        }

    monkeypatch.setattr(smoke, "_http_probe", _fake_probe)

    receipt = smoke._check_generated_reconstruction_public_contract(
        public_base_url="https://propertyquarry.com",
        slug="runtime-smoke",
    )

    assert receipt["status"] == "pass"
    assert len(calls) == 4


def test_generated_reconstruction_public_contract_allows_direct_provider_control_redirect(monkeypatch) -> None:
    def _fake_probe(url: str, *, host_header: str = "") -> dict[str, object]:
        if url.endswith("/generated-reconstruction/viewer.html"):
            return {"status_code": 302, "location": "/tours/runtime-smoke/control/matterport", "body_excerpt": ""}
        if url.endswith("/generated-reconstruction/model.obj"):
            return {"status_code": 410, "location": "", "body_excerpt": "This generated model is not a public 3D tour."}
        if url.endswith("/runtime-smoke.json"):
            return {"status_code": 200, "location": "", "body_excerpt": '{"slug":"runtime-smoke"}'}
        return {"status_code": 302, "location": "/tours/runtime-smoke/control/matterport", "body_excerpt": ""}

    monkeypatch.setattr(smoke, "_http_probe", _fake_probe)

    receipt = smoke._check_generated_reconstruction_public_contract(
        public_base_url="https://propertyquarry.com",
        slug="runtime-smoke",
    )

    assert receipt["status"] == "pass"


def test_generated_reconstruction_public_contract_allows_first_party_embedded_viewer_shell(monkeypatch) -> None:
    def _fake_probe(url: str, *, host_header: str = "") -> dict[str, object]:
        if url.endswith("/generated-reconstruction/viewer.html"):
            return {
                "status_code": 200,
                "location": "",
                "body_excerpt": '<html><head><title>Styled 3D reconstruction | PropertyQuarry</title></head><body><div id="viewport"></div></body></html>',
            }
        if url.endswith("/generated-reconstruction/model.obj"):
            return {
                "status_code": 200,
                "location": "",
                "body_excerpt": "mtllib model.mtl\no propertyquarry_generated_layout\nv 0 0 0\nf 1 2 3\n",
            }
        if url.endswith("/runtime-smoke.json"):
            return {
                "status_code": 200,
                "location": "",
                "body_excerpt": '{"slug":"runtime-smoke","property_url_sha256":"sha256:safe"}',
            }
        return {
            "status_code": 200,
            "location": "",
            "body_excerpt": (
                'PropertyQuarry styled 3D reconstruction Generated reconstruction '
                '<section class="layout-viewer-shell"><iframe src="/tours/files/runtime-smoke/generated-reconstruction/viewer.html"></iframe></section>'
            ),
        }

    monkeypatch.setattr(smoke, "_http_probe", _fake_probe)

    receipt = smoke._check_generated_reconstruction_public_contract(
        public_base_url="https://propertyquarry.com",
        slug="runtime-smoke",
    )

    assert receipt["status"] == "pass"
    assert receipt["private_marker_leaks"] == []


def test_generated_reconstruction_public_contract_rejects_raw_viewer_404(monkeypatch) -> None:
    def _fake_probe(url: str, *, host_header: str = "") -> dict[str, object]:
        if url.endswith("/generated-reconstruction/viewer.html"):
            return {"status_code": 404, "location": "", "body_excerpt": "This 3D tour is no longer available."}
        if url.endswith("/generated-reconstruction/model.obj"):
            return {"status_code": 410, "location": "", "body_excerpt": "This generated model is not a public 3D tour."}
        return {
            "status_code": 200,
            "location": "",
            "body_excerpt": "PropertyQuarry styled 3D reconstruction Generated reconstruction Room route Reference deck",
        }

    monkeypatch.setattr(smoke, "_http_probe", _fake_probe)

    receipt = smoke._check_generated_reconstruction_public_contract(
        public_base_url="https://propertyquarry.com",
        slug="runtime-smoke",
    )

    assert receipt["status"] == "failed"
    assert "viewer_not_routed_to_clean_shell" in receipt["failures"]


def test_generated_reconstruction_public_contract_forwards_host_header_to_loopback_probes(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def _fake_probe(url: str, *, host_header: str = "") -> dict[str, object]:
        calls.append((url, host_header))
        if url.endswith("/generated-reconstruction/model.obj"):
            return {"status_code": 410, "location": "", "body_excerpt": "This generated model is not a public 3D tour."}
        return {
            "status_code": 200,
            "location": "",
            "body_excerpt": "PropertyQuarry styled 3D reconstruction Generated reconstruction Room route Reference deck",
        }

    monkeypatch.setattr(smoke, "_http_probe", _fake_probe)

    receipt = smoke._check_generated_reconstruction_public_contract(
        public_base_url="http://127.0.0.1:8090",
        slug="runtime-smoke",
        host_header="propertyquarry.com",
    )

    assert receipt["status"] == "failed"
    assert receipt["host_header"] == "propertyquarry.com"
    assert calls == [
        ("http://127.0.0.1:8090/tours/files/runtime-smoke/generated-reconstruction/viewer.html", "propertyquarry.com"),
        ("http://127.0.0.1:8090/tours/runtime-smoke", "propertyquarry.com"),
        ("http://127.0.0.1:8090/tours/runtime-smoke.json", "propertyquarry.com"),
        ("http://127.0.0.1:8090/tours/files/runtime-smoke/generated-reconstruction/model.obj", "propertyquarry.com"),
    ]


def test_generated_reconstruction_public_contract_rejects_private_markers_in_payload(monkeypatch) -> None:
    def _fake_probe(url: str, *, host_header: str = "") -> dict[str, object]:
        if url.endswith("/generated-reconstruction/viewer.html"):
            return {"status_code": 302, "location": "/tours/runtime-smoke", "body_excerpt": ""}
        if url.endswith("/generated-reconstruction/model.obj"):
            return {"status_code": 410, "location": "", "body_excerpt": "This generated model is not a public 3D tour."}
        if url.endswith("/runtime-smoke.json"):
            return {
                "status_code": 200,
                "location": "",
                "body_excerpt": '{"slug":"runtime-smoke","recipient_email":"owner@example.test"}',
            }
        return {
            "status_code": 200,
            "location": "",
            "body_excerpt": "PropertyQuarry styled 3D reconstruction Generated reconstruction Room route Reference deck",
        }

    monkeypatch.setattr(smoke, "_http_probe", _fake_probe)

    receipt = smoke._check_generated_reconstruction_public_contract(
        public_base_url="https://propertyquarry.com",
        slug="runtime-smoke",
    )

    assert receipt["status"] == "failed"
    assert "public_payload_private_markers_present" in receipt["failures"]
    assert "owner@example.test" in receipt["private_marker_leaks"]
