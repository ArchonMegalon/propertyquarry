from __future__ import annotations

import json
import subprocess

import pytest

from scripts import property_service_generated_reconstruction_smoke as smoke


@pytest.fixture(autouse=True)
def _stub_render_bridge_runtime(monkeypatch) -> None:
    monkeypatch.setattr(
        smoke,
        "build_render_bridge_runtime_receipt",
        lambda **_: {"status": "pass", "action": "already_ready"},
    )


def _inspection_payload(
    *,
    top_level_video_relpath: str = "generated-reconstruction/generated-walkthrough.mp4",
    top_level_video_provider: str = "propertyquarry_generated_reconstruction",
    top_level_video_provider_key: str = "propertyquarry_generated_reconstruction",
    top_level_video_coverage_proof: str = "boundary_verified_frame_continuation",
    top_level_video_sidecar_relpath: str = "generated-reconstruction/generated-walkthrough.quality.json",
    route_labels: list[str] | None = None,
    walkthrough_route_labels: list[str] | None = None,
    context_route_labels: list[str] | None = None,
    delivery_route_labels: list[str] | None = None,
    walkthrough_asset_suffix: str = "/generated-reconstruction/generated-walkthrough.mp4",
    delivery_provider_key: str = "propertyquarry_generated_reconstruction",
    delivery_duration_seconds: float = 32.0,
    delivery_coverage_proof: str = "boundary_verified_frame_continuation",
    public_paths: bool = True,
    photo_count: int = 5,
) -> dict[str, object]:
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
    context_route_labels = context_route_labels or list(route_labels)
    delivery_route_labels = delivery_route_labels or list(walkthrough_route_labels)
    return {
        "slug": "runtime-service-generated-reconstruction-smoke",
        "tour_url": "https://propertyquarry.com/tours/runtime-service-generated-reconstruction-smoke",
        "top_level_video_relpath": top_level_video_relpath,
        "top_level_video_provider": top_level_video_provider,
        "top_level_video_provider_key": top_level_video_provider_key,
        "top_level_video_coverage_proof": top_level_video_coverage_proof,
        "top_level_video_sidecar_relpath": top_level_video_sidecar_relpath,
        "generated_route_labels": route_labels,
        "generated_walkthrough_route_labels": walkthrough_route_labels,
        "generated_room_stop_count": len(route_labels),
        "generated_walkthrough_stop_count": len(walkthrough_route_labels),
        "generated_photo_count": photo_count,
        "generated_walkthrough_video_relpath": "generated-reconstruction/generated-walkthrough.mp4",
        "generated_walkthrough_sidecar_relpath": "generated-reconstruction/generated-walkthrough.quality.json",
        "generated_walkthrough_coverage_proof": {
            "status": "pass",
            "segments_expected": walkthrough_route_labels,
            "segments_visited": walkthrough_route_labels,
            "coverage_segments": [
                {"segment": label, "index": index}
                for index, label in enumerate(walkthrough_route_labels, start=1)
            ],
        },
        "walkable_scene_route_labels": list(route_labels),
        "video_delivery": {
            "video_url": f"https://propertyquarry.com/tours/files/runtime-service-generated-reconstruction-smoke{walkthrough_asset_suffix}",
            "provider_key": delivery_provider_key,
            "duration_seconds": delivery_duration_seconds,
            "coverage_proof": delivery_coverage_proof,
            "covered_route_labels": delivery_route_labels,
        },
        "context_route_labels": context_route_labels,
        "walkthrough_asset_url": f"https://propertyquarry.com/tours/files/runtime-service-generated-reconstruction-smoke{walkthrough_asset_suffix}",
        "paths": {
            "viewer": {"exists": public_paths, "size_bytes": 10},
            "obj": {"exists": public_paths, "size_bytes": 10},
            "mtl": {"exists": public_paths, "size_bytes": 10},
            "glb": {"exists": True, "size_bytes": 32},
            "receipt": {"exists": public_paths, "size_bytes": 10},
            "walkthrough_video": {"exists": public_paths, "size_bytes": 256},
            "walkthrough_sidecar": {"exists": public_paths, "size_bytes": 128},
        },
    }


def test_service_generated_reconstruction_smoke_passes_when_contract_is_complete(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)
    calls: list[list[str]] = []

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(_inspection_payload()) + "\n", stderr="")

    monkeypatch.setattr(smoke, "_run", _fake_run)
    monkeypatch.setattr(smoke, "_check_generated_reconstruction_browser_shell", lambda **_: {"status": "pass"})
    monkeypatch.setattr(smoke, "_check_generated_reconstruction_public_contract", lambda **_: {"status": "pass"})

    receipt = smoke.build_service_generated_reconstruction_receipt(
        container="propertyquarry-api",
        slug="runtime-service-generated-reconstruction-smoke",
        public_base_url="https://propertyquarry.com",
        require_browser_shell=True,
    )

    assert receipt["status"] == "pass"
    assert receipt["required_paths_ok"] is True
    assert receipt["top_level_video_contract_ok"] is True
    assert receipt["route_label_quality_ok"] is True
    assert receipt["walkthrough_generated_ok"] is True
    assert receipt["delivery_contract_ok"] is True
    assert receipt["browser_shell_ok"] is True
    assert receipt["minimum_walkthrough_duration_seconds"] == 30.0
    assert calls[0][:4] == ["docker", "exec", "propertyquarry-api", "python"]
    assert "sh" not in calls[0]
    assert "src.mkdir(parents=True, exist_ok=True)" in calls[0][-1]


def test_service_generated_reconstruction_smoke_fails_when_top_level_video_contract_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        payload = _inspection_payload(top_level_video_relpath="")
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload) + "\n", stderr="")

    monkeypatch.setattr(smoke, "_run", _fake_run)
    monkeypatch.setattr(smoke, "_check_generated_reconstruction_browser_shell", lambda **_: {"status": "pass"})
    monkeypatch.setattr(smoke, "_check_generated_reconstruction_public_contract", lambda **_: {"status": "pass"})

    receipt = smoke.build_service_generated_reconstruction_receipt(
        container="propertyquarry-api",
        slug="runtime-service-generated-reconstruction-smoke",
        public_base_url="https://propertyquarry.com",
        require_browser_shell=True,
    )

    assert receipt["status"] == "failed"
    assert receipt["top_level_video_contract_ok"] is False


def test_service_generated_reconstruction_smoke_fails_when_delivery_contract_misses_expanded_walkthrough(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        payload = _inspection_payload(
            delivery_route_labels=[
                "entry/hall",
                "living area",
                "sleeping area",
                "balcony/terrace",
            ]
        )
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload) + "\n", stderr="")

    monkeypatch.setattr(smoke, "_run", _fake_run)
    monkeypatch.setattr(smoke, "_check_generated_reconstruction_browser_shell", lambda **_: {"status": "pass"})
    monkeypatch.setattr(smoke, "_check_generated_reconstruction_public_contract", lambda **_: {"status": "pass"})

    receipt = smoke.build_service_generated_reconstruction_receipt(
        container="propertyquarry-api",
        slug="runtime-service-generated-reconstruction-smoke",
        public_base_url="https://propertyquarry.com",
        require_browser_shell=True,
    )

    assert receipt["status"] == "failed"
    assert receipt["delivery_contract_ok"] is False


def test_service_generated_reconstruction_smoke_fails_when_delivery_duration_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        payload = _inspection_payload(delivery_duration_seconds=0.0)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload) + "\n", stderr="")

    monkeypatch.setattr(smoke, "_run", _fake_run)
    monkeypatch.setattr(smoke, "_check_generated_reconstruction_browser_shell", lambda **_: {"status": "pass"})
    monkeypatch.setattr(smoke, "_check_generated_reconstruction_public_contract", lambda **_: {"status": "pass"})

    receipt = smoke.build_service_generated_reconstruction_receipt(
        container="propertyquarry-api",
        slug="runtime-service-generated-reconstruction-smoke",
        public_base_url="https://propertyquarry.com",
        require_browser_shell=True,
    )

    assert receipt["status"] == "failed"
    assert receipt["delivery_contract_ok"] is False


def test_service_generated_reconstruction_smoke_fails_when_delivery_duration_is_too_short_for_route_coverage(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        payload = _inspection_payload(delivery_duration_seconds=18.0)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload) + "\n", stderr="")

    monkeypatch.setattr(smoke, "_run", _fake_run)
    monkeypatch.setattr(smoke, "_check_generated_reconstruction_browser_shell", lambda **_: {"status": "pass"})
    monkeypatch.setattr(smoke, "_check_generated_reconstruction_public_contract", lambda **_: {"status": "pass"})

    receipt = smoke.build_service_generated_reconstruction_receipt(
        container="propertyquarry-api",
        slug="runtime-service-generated-reconstruction-smoke",
        public_base_url="https://propertyquarry.com",
        require_browser_shell=True,
    )

    assert receipt["status"] == "failed"
    assert receipt["delivery_contract_ok"] is False
    assert receipt["minimum_walkthrough_duration_seconds"] == 30.0


def test_service_generated_reconstruction_smoke_fails_when_coverage_proof_does_not_prove_visited_segments(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        payload = _inspection_payload()
        payload["generated_walkthrough_coverage_proof"] = {
            "status": "pass",
            "segments_expected": list(payload["generated_walkthrough_route_labels"]),
        }
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload) + "\n", stderr="")

    monkeypatch.setattr(smoke, "_run", _fake_run)
    monkeypatch.setattr(smoke, "_check_generated_reconstruction_browser_shell", lambda **_: {"status": "pass"})
    monkeypatch.setattr(smoke, "_check_generated_reconstruction_public_contract", lambda **_: {"status": "pass"})

    receipt = smoke.build_service_generated_reconstruction_receipt(
        container="propertyquarry-api",
        slug="runtime-service-generated-reconstruction-smoke",
        public_base_url="https://propertyquarry.com",
        require_browser_shell=True,
    )

    assert receipt["status"] == "failed"
    assert receipt["walkthrough_generated_ok"] is False


def test_service_generated_reconstruction_smoke_fails_when_route_labels_collapse_to_generic_placeholders(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        route_labels = ["room stop 1", "room stop 2", "room stop 3", "room stop 4"]
        payload = _inspection_payload(
            route_labels=route_labels,
            context_route_labels=route_labels,
        )
        payload["walkable_scene_route_labels"] = list(route_labels)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload) + "\n", stderr="")

    monkeypatch.setattr(smoke, "_run", _fake_run)
    monkeypatch.setattr(smoke, "_check_generated_reconstruction_browser_shell", lambda **_: {"status": "pass"})
    monkeypatch.setattr(smoke, "_check_generated_reconstruction_public_contract", lambda **_: {"status": "pass"})

    receipt = smoke.build_service_generated_reconstruction_receipt(
        container="propertyquarry-api",
        slug="runtime-service-generated-reconstruction-smoke",
        public_base_url="https://propertyquarry.com",
        require_browser_shell=True,
    )

    assert receipt["status"] == "failed"
    assert receipt["route_label_quality_ok"] is False


def test_service_generated_reconstruction_smoke_requires_public_contract_when_requested(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(_inspection_payload()) + "\n", stderr="")

    monkeypatch.setattr(smoke, "_run", _fake_run)

    receipt = smoke.build_service_generated_reconstruction_receipt(
        container="propertyquarry-api",
        slug="runtime-service-generated-reconstruction-smoke",
        require_public_contract=True,
    )

    assert receipt["status"] == "failed"
    assert receipt["public_route_contract_ok"] is False
    assert receipt["public_route_contract"]["reason"] == "public_base_url_missing"


def test_service_generated_reconstruction_smoke_forwards_host_header_to_public_contract(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(_inspection_payload()) + "\n", stderr="")

    observed: dict[str, str] = {}

    def _fake_public_contract(*, public_base_url: str, slug: str, host_header: str = "") -> dict[str, object]:
        observed["public_base_url"] = public_base_url
        observed["slug"] = slug
        observed["host_header"] = host_header
        return {"status": "failed", "failures": ["canonical_not_shell_or_control"]}

    monkeypatch.setattr(smoke, "_run", _fake_run)
    monkeypatch.setattr(
        smoke,
        "_resolved_local_public_base_url",
        lambda public_base_url, *, public_container: "http://127.0.0.1:8097",
    )
    monkeypatch.setattr(smoke, "_sync_container_tour_to_host_root", lambda *_, **__: {"status": "pass"})
    monkeypatch.setattr(smoke, "_check_generated_reconstruction_public_contract", _fake_public_contract)

    receipt = smoke.build_service_generated_reconstruction_receipt(
        container="propertyquarry-api",
        slug="runtime-service-generated-reconstruction-smoke",
        public_base_url="http://127.0.0.1:8090",
        host_header="propertyquarry.com",
        require_public_contract=True,
    )

    assert observed == {
        "public_base_url": "http://127.0.0.1:8097",
        "slug": "runtime-service-generated-reconstruction-smoke",
        "host_header": "propertyquarry.com",
    }
    assert receipt["status"] == "failed"
    assert receipt["resolved_public_base_url"] == "http://127.0.0.1:8097"
    assert receipt["host_public_tour_sync"]["status"] == "pass"
    assert receipt["public_route_contract_ok"] is False
    assert receipt["public_route_contract"]["failures"] == ["canonical_not_shell_or_control"]


def test_service_generated_reconstruction_smoke_syncs_container_tour_before_local_public_probes(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(_inspection_payload()) + "\n", stderr="")

    observed: dict[str, str] = {}

    def _fake_sync(container: str, *, slug: str, public_base_url: str) -> dict[str, object]:
        observed["container"] = container
        observed["slug"] = slug
        observed["public_base_url"] = public_base_url
        return {"status": "pass", "destination": f"/tmp/public-tours/{slug}"}

    monkeypatch.setattr(smoke, "_run", _fake_run)
    monkeypatch.setattr(smoke, "_sync_container_tour_to_host_root", _fake_sync)
    monkeypatch.setattr(smoke, "_check_generated_reconstruction_public_contract", lambda **_: {"status": "pass"})
    monkeypatch.setattr(smoke, "_check_generated_reconstruction_browser_shell", lambda **_: {"status": "pass"})

    receipt = smoke.build_service_generated_reconstruction_receipt(
        container="propertyquarry-api",
        slug="runtime-service-generated-reconstruction-smoke",
        public_base_url="http://127.0.0.1:8099",
        host_header="propertyquarry.com",
        require_public_contract=True,
        require_browser_shell=True,
    )

    assert receipt["status"] == "pass"
    assert observed == {
        "container": "propertyquarry-api",
        "slug": "runtime-service-generated-reconstruction-smoke",
        "public_base_url": "http://127.0.0.1:8099",
    }
    assert receipt["host_public_tour_sync"]["status"] == "pass"
    assert receipt["host_public_tour_sync"]["destination"].endswith("/runtime-service-generated-reconstruction-smoke")


def test_service_generated_reconstruction_smoke_requires_browser_shell_when_requested(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(_inspection_payload()) + "\n", stderr="")

    monkeypatch.setattr(smoke, "_run", _fake_run)

    receipt = smoke.build_service_generated_reconstruction_receipt(
        container="propertyquarry-api",
        slug="runtime-service-generated-reconstruction-smoke",
        require_browser_shell=True,
    )

    assert receipt["status"] == "failed"
    assert receipt["browser_shell_ok"] is False
    assert receipt["browser_shell"]["reason"] == "public_base_url_missing"


def test_service_generated_reconstruction_smoke_fails_when_browser_shell_proof_fails(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(_inspection_payload()) + "\n", stderr="")

    monkeypatch.setattr(smoke, "_run", _fake_run)
    monkeypatch.setattr(
        smoke,
        "_check_generated_reconstruction_browser_shell",
        lambda **_: {"status": "failed", "reason": "launch_shell_media_grid_map_label_present"},
    )
    monkeypatch.setattr(smoke, "_check_generated_reconstruction_public_contract", lambda **_: {"status": "pass"})

    receipt = smoke.build_service_generated_reconstruction_receipt(
        container="propertyquarry-api",
        slug="runtime-service-generated-reconstruction-smoke",
        public_base_url="https://propertyquarry.com",
        require_browser_shell=True,
    )

    assert receipt["status"] == "failed"
    assert receipt["browser_shell_ok"] is False
    assert receipt["browser_shell"]["reason"] == "launch_shell_media_grid_map_label_present"


def test_service_generated_reconstruction_smoke_accepts_staircase_led_route_without_detail_when_photo_count_is_covered(
    monkeypatch,
) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
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
            context_route_labels=list(route_labels),
            delivery_route_labels=list(route_labels),
            delivery_duration_seconds=42.0,
            photo_count=5,
        )
        payload["walkable_scene_route_labels"] = list(route_labels)
        payload["generated_walkthrough_coverage_proof"] = {
            "status": "pass",
            "segments_expected": list(route_labels),
            "segments_visited": list(route_labels),
            "coverage_segments": [
                {"segment": label, "index": index}
                for index, label in enumerate(route_labels, start=1)
            ],
        }
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload) + "\n", stderr="")

    monkeypatch.setattr(smoke, "_run", _fake_run)
    monkeypatch.setattr(smoke, "_check_generated_reconstruction_browser_shell", lambda **_: {"status": "pass"})
    monkeypatch.setattr(smoke, "_check_generated_reconstruction_public_contract", lambda **_: {"status": "pass"})

    receipt = smoke.build_service_generated_reconstruction_receipt(
        container="propertyquarry-api",
        slug="runtime-service-generated-reconstruction-smoke",
        public_base_url="https://propertyquarry.com",
        require_browser_shell=True,
    )

    assert receipt["status"] == "pass"
    assert receipt["route_label_quality_ok"] is True
    assert receipt["walkthrough_generated_ok"] is True
    assert receipt["browser_shell_ok"] is True


def test_service_generated_reconstruction_smoke_clears_materialized_bundle_slug_before_generation(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)
    observed: dict[str, object] = {}

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        observed["command"] = list(command)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(_inspection_payload()) + "\n", stderr="")

    monkeypatch.setattr(smoke, "_run", _fake_run)
    monkeypatch.setattr(smoke, "_check_generated_reconstruction_browser_shell", lambda **_: {"status": "pass"})
    monkeypatch.setattr(smoke, "_check_generated_reconstruction_public_contract", lambda **_: {"status": "pass"})

    receipt = smoke.build_service_generated_reconstruction_receipt(
        container="propertyquarry-api",
        slug="runtime-service-generated-reconstruction-smoke",
        public_base_url="https://propertyquarry.com",
        require_browser_shell=True,
    )

    assert receipt["status"] == "pass"
    setup_script = str(list(observed.get("command") or [])[-1])
    assert "_make_hosted_property_tour_slug(" in setup_script
    assert "materialized_slug" in setup_script
    assert "shutil.rmtree(Path('/data/public_property_tours') / materialized_slug, ignore_errors=True)" in setup_script
    assert "PROPERTYQUARRY_RECONSTRUCTION_WALKTHROUGH_SECONDS_PER_STOP'] = '8'" in setup_script


def test_service_generated_reconstruction_smoke_uses_extended_command_timeout(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)
    monkeypatch.setenv("PROPERTYQUARRY_RECONSTRUCTION_TIMEOUT_SECONDS", "480")
    monkeypatch.delenv("PROPERTYQUARRY_RECONSTRUCTION_REQUEST_TIMEOUT_SECONDS", raising=False)
    observed: dict[str, object] = {}

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        observed["timeout"] = timeout
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(_inspection_payload()) + "\n", stderr="")

    monkeypatch.setattr(smoke, "_run", _fake_run)
    monkeypatch.setattr(smoke, "_check_generated_reconstruction_browser_shell", lambda **_: {"status": "pass"})
    monkeypatch.setattr(smoke, "_check_generated_reconstruction_public_contract", lambda **_: {"status": "pass"})

    receipt = smoke.build_service_generated_reconstruction_receipt(
        container="propertyquarry-api",
        slug="runtime-service-generated-reconstruction-smoke",
        public_base_url="https://propertyquarry.com",
        require_browser_shell=True,
    )

    assert receipt["status"] == "pass"
    assert observed["timeout"] == 600


def test_service_generated_reconstruction_smoke_returns_timeout_receipt_when_generation_command_times_out(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, timeout=timeout)

    monkeypatch.setattr(smoke, "_run", _fake_run)

    receipt = smoke.build_service_generated_reconstruction_receipt(
        container="propertyquarry-api",
        slug="runtime-service-generated-reconstruction-smoke",
        command_timeout_seconds=510,
    )

    assert receipt["status"] == "failed"
    assert receipt["reason"] == "service_generated_reconstruction_command_timeout"
    assert receipt["timeout_seconds"] == 510


def test_service_generated_reconstruction_smoke_fails_when_render_bridge_runtime_cannot_be_materialized(monkeypatch) -> None:
    monkeypatch.setattr(smoke.shutil, "which", lambda command: "/usr/bin/docker" if command == "docker" else None)
    monkeypatch.setattr(
        smoke,
        "build_render_bridge_runtime_receipt",
        lambda **_: {"status": "failed", "reason": "render_bridge_runtime_not_ready"},
    )

    receipt = smoke.build_service_generated_reconstruction_receipt(
        container="propertyquarry-api",
        slug="runtime-service-generated-reconstruction-smoke",
    )

    assert receipt["status"] == "failed"
    assert receipt["reason"] == "render_bridge_runtime_unavailable"
    assert receipt["render_bridge_runtime"]["reason"] == "render_bridge_runtime_not_ready"
