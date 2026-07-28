from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
import yaml

from scripts import ensure_propertyquarry_render_bridge_runtime as runtime
from scripts import property_reconstruction_render_bridge as bridge
from app.services.admission_control import MemoryAdmissionBackend


def test_property_compose_wires_protected_bounded_restart_render_bridge() -> None:
    compose = yaml.safe_load(Path("docker-compose.property.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    api = services["propertyquarry-api"]
    render = services["propertyquarry-render-tools"]

    assert api["environment"]["PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_URL"] == (
        "${PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_URL:-"
        "http://propertyquarry-render-tools:8091/generate-reconstruction}"
    )
    assert api["environment"]["PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN"] == (
        "${PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN:?"
        "Set PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN for the render bridge}"
    )
    assert render["restart"] == "${PROPERTYQUARRY_RENDER_RESTART_POLICY:-on-failure:3}"
    assert render["stop_grace_period"] == "${PROPERTYQUARRY_RENDER_STOP_GRACE_SECONDS:-1860}s"
    assert render["environment"]["PROPERTYQUARRY_RECONSTRUCTION_RENDER_REQUEST_TIMEOUT_SECONDS"] == (
        "${PROPERTYQUARRY_RECONSTRUCTION_RENDER_REQUEST_TIMEOUT_SECONDS:-30}"
    )
    assert render["environment"]["PROPERTYQUARRY_RECONSTRUCTION_RENDER_MAX_GENERATION_SECONDS"] == (
        "${PROPERTYQUARRY_RECONSTRUCTION_RENDER_MAX_GENERATION_SECONDS:-1800}"
    )
    assert render["environment"]["PROPERTYQUARRY_RENDER_STOP_GRACE_SECONDS"] == (
        "${PROPERTYQUARRY_RENDER_STOP_GRACE_SECONDS:-1860}"
    )
    assert render["environment"]["DATABASE_URL"] == (
        "${PROPERTYQUARRY_RENDER_DATABASE_URL:?Set a least-privilege "
        "PROPERTYQUARRY_RENDER_DATABASE_URL for admission state}"
    )
    assert render["networks"] == ["propertyquarry_render_internal"]
    assert "propertyquarry_render_internal" in services["propertyquarry-db"]["networks"]
    assert render["depends_on"]["propertyquarry-db"]["condition"] == "service_healthy"
    assert render["depends_on"]["propertyquarry-migrate"]["condition"] == (
        "service_completed_successfully"
    )


def test_render_bridge_derives_drain_bound_from_authoritative_container_stop_grace(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN", "test-bridge-token")
    monkeypatch.setenv("PROPERTYQUARRY_RECONSTRUCTION_RENDER_REQUEST_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("PROPERTYQUARRY_RECONSTRUCTION_RENDER_MAX_GENERATION_SECONDS", "900")
    monkeypatch.setenv("PROPERTYQUARRY_RENDER_STOP_GRACE_SECONDS", "975")

    config = bridge._load_bridge_config()

    assert config.container_stop_grace_seconds == bridge._required_container_stop_grace_seconds(
        max_generation_seconds=900,
        request_timeout_seconds=45,
    )
    assert config.container_stop_grace_seconds == 975
    assert config.shutdown_grace_seconds == 945
    bridge._validate_bridge_config(config)


def test_render_bridge_accepts_maximum_timeout_contract_when_container_bound_matches(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN", "test-bridge-token")
    monkeypatch.setenv("PROPERTYQUARRY_RECONSTRUCTION_RENDER_REQUEST_TIMEOUT_SECONDS", "300")
    monkeypatch.setenv("PROPERTYQUARRY_RECONSTRUCTION_RENDER_MAX_GENERATION_SECONDS", "7200")
    monkeypatch.setenv("PROPERTYQUARRY_RENDER_STOP_GRACE_SECONDS", "7530")

    config = bridge._load_bridge_config()

    assert config.container_stop_grace_seconds == 7530
    assert config.shutdown_grace_seconds == 7500
    bridge._validate_bridge_config(config)


def test_render_bridge_rejects_default_container_bound_for_maximum_generation(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN", "test-bridge-token")
    monkeypatch.setenv("PROPERTYQUARRY_RECONSTRUCTION_RENDER_REQUEST_TIMEOUT_SECONDS", "300")
    monkeypatch.setenv("PROPERTYQUARRY_RECONSTRUCTION_RENDER_MAX_GENERATION_SECONDS", "7200")
    monkeypatch.delenv("PROPERTYQUARRY_RENDER_STOP_GRACE_SECONDS", raising=False)

    config = bridge._load_bridge_config()

    assert config.container_stop_grace_seconds == 1860
    with pytest.raises(
        RuntimeError,
        match="property_reconstruction_render_container_stop_grace_insufficient",
    ):
        bridge._validate_bridge_config(config)


def test_render_bridge_process_fails_closed_for_unsafe_timeout_override() -> None:
    env = {
        **os.environ,
        "PROPERTYQUARRY_RECONSTRUCTION_RENDER_HOST": "127.0.0.1",
        "PROPERTYQUARRY_RECONSTRUCTION_RENDER_DEV_MODE": "1",
        "PROPERTYQUARRY_RECONSTRUCTION_RENDER_REQUEST_TIMEOUT_SECONDS": "300",
        "PROPERTYQUARRY_RECONSTRUCTION_RENDER_MAX_GENERATION_SECONDS": "7200",
        "PROPERTYQUARRY_RENDER_STOP_GRACE_SECONDS": "1860",
    }
    env.pop("PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN", None)

    result = subprocess.run(
        [sys.executable, "scripts/property_reconstruction_render_bridge.py"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode != 0
    assert "property_reconstruction_render_container_stop_grace_insufficient" in result.stderr


def test_render_bridge_rejects_container_stop_grace_shorter_than_request_contract() -> None:
    config = bridge.BridgeConfig(
        auth_token="test-bridge-token",
        request_timeout_seconds=30,
        max_generation_seconds=120,
        container_stop_grace_seconds=179,
    )

    with pytest.raises(
        RuntimeError,
        match="property_reconstruction_render_container_stop_grace_insufficient",
    ):
        bridge._validate_bridge_config(config)


def test_render_bridge_readiness_fails_closed_while_draining_without_provider_claims(
    tmp_path: Path,
    monkeypatch,
) -> None:
    script_path = tmp_path / "generate_property_reconstruction.py"
    script_path.write_text("# test generator\n", encoding="utf-8")
    public_tour_dir = tmp_path / "public-tours"
    public_tour_dir.mkdir()
    monkeypatch.setattr(bridge, "_script_path", lambda: script_path)
    monkeypatch.setattr(bridge, "_public_tour_dir", lambda: public_tour_dir)
    config = bridge.BridgeConfig(auth_token="test-bridge-token")

    admission_backend = MemoryAdmissionBackend()
    ready, ready_payload = bridge._bridge_readiness(
        config,
        admission_backend=admission_backend,
    )
    draining_ready, draining_payload = bridge._bridge_readiness(
        config,
        draining=True,
        admission_backend=admission_backend,
    )

    assert ready is True
    assert ready_payload["provider_readiness_claimed"] is False
    assert ready_payload["accepting_requests"] is True
    assert draining_ready is False
    assert draining_payload["status"] == "not_ready"
    assert draining_payload["reason"] == "bridge_draining"
    assert draining_payload["accepting_requests"] is False
    assert draining_payload["provider_readiness_claimed"] is False


def test_render_bridge_server_waits_for_active_requests_with_a_bound() -> None:
    config = bridge.BridgeConfig(
        host="127.0.0.1",
        port=0,
        auth_token="test-bridge-token",
        request_timeout_seconds=1,
        max_generation_seconds=120,
        container_stop_grace_seconds=151,
    )
    server = bridge.ReconstructionRenderBridgeServer(
        (config.host, config.port),
        bridge._Handler,
        config=config,
        admission_backend=MemoryAdmissionBackend(),
    )
    try:
        server._request_started()
        assert server.active_request_count == 1
        assert server.begin_draining() is True
        assert server.begin_draining() is False
        assert server.wait_for_drain(0) is False

        server._request_finished()

        assert server.wait_for_drain(0.1) is True
        assert server.active_request_count == 0
        assert server.is_draining() is True
    finally:
        server.server_close()


def test_render_bridge_process_handles_sigterm_and_exits_cleanly() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as socket_probe:
        socket_probe.bind(("127.0.0.1", 0))
        port = int(socket_probe.getsockname()[1])
    env = {
        **os.environ,
        "PROPERTYQUARRY_RECONSTRUCTION_RENDER_HOST": "127.0.0.1",
        "PROPERTYQUARRY_RECONSTRUCTION_RENDER_PORT": str(port),
        "PROPERTYQUARRY_RECONSTRUCTION_RENDER_DEV_MODE": "1",
        "PROPERTYQUARRY_RECONSTRUCTION_RENDER_REQUEST_TIMEOUT_SECONDS": "1",
        "PROPERTYQUARRY_RECONSTRUCTION_RENDER_MAX_GENERATION_SECONDS": "120",
        "PROPERTYQUARRY_RENDER_STOP_GRACE_SECONDS": "151",
    }
    env.pop("PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN", None)
    process = subprocess.Popen(
        [sys.executable, "scripts/property_reconstruction_render_bridge.py"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5.0
        while True:
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=1)
                pytest.fail(f"render bridge exited before signal: {stdout[-500:]} {stderr[-500:]}")
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health/live",
                    timeout=0.2,
                ) as response:
                    assert response.status == 200
                    break
            except Exception:
                if time.monotonic() >= deadline:
                    pytest.fail("render bridge did not become live before SIGTERM test deadline")
                time.sleep(0.05)

        process.send_signal(signal.SIGTERM)

        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_render_bridge_runtime_receipt_passes_when_container_is_already_healthy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    compose = tmp_path / "docker-compose.property.yml"
    compose.write_text("services:\n", encoding="utf-8")
    monkeypatch.setattr(runtime, "_docker_available", lambda: True)
    monkeypatch.setattr(
        runtime,
        "_container_state",
        lambda container: {"exists": True, "status": "running", "health": "healthy", "detail": ""},
    )
    monkeypatch.setattr(
        runtime,
        "_container_health_probe",
        lambda container, *, health_url: {"status": "pass", "url": health_url},
    )
    monkeypatch.setattr(runtime, "_runtime_code_parity", lambda container: {"status": "pass", "checks": []})

    receipt = runtime.build_render_bridge_runtime_receipt(
        container="propertyquarry-render-tools",
        service="propertyquarry-render-tools",
        compose_file=str(compose),
    )

    assert receipt["status"] == "pass"
    assert receipt["action"] == "already_ready"
    assert receipt["health_probe"]["status"] == "pass"


def test_render_bridge_runtime_receipt_recreates_runtime_when_container_is_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    compose = tmp_path / "docker-compose.property.yml"
    compose.write_text("services:\n", encoding="utf-8")
    monkeypatch.setattr(runtime, "_docker_available", lambda: True)
    monkeypatch.setattr(runtime, "_compose_command", lambda **_: ["docker", "compose", "-f", str(compose)])

    states = iter(
        [
            {"exists": False, "status": "", "health": "", "detail": ""},
            {"exists": False, "status": "", "health": "", "detail": ""},
            {"exists": True, "status": "running", "health": "healthy", "detail": ""},
        ]
    )
    monkeypatch.setattr(runtime, "_container_state", lambda container: next(states))
    monkeypatch.setattr(
        runtime,
        "_container_health_probe",
        lambda container, *, health_url: {"status": "pass", "url": health_url},
    )
    monkeypatch.setattr(runtime, "_runtime_code_parity", lambda container: {"status": "pass", "checks": []})

    observed: dict[str, object] = {}

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        observed["command"] = list(command)
        observed["timeout"] = timeout
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(runtime, "_run", _fake_run)

    receipt = runtime.build_render_bridge_runtime_receipt(
        container="propertyquarry-render-tools",
        service="propertyquarry-render-tools",
        compose_file=str(compose),
    )

    assert receipt["status"] == "pass"
    assert receipt["action"] == "recreated_runtime"
    assert observed["command"][-6:] == ["up", "-d", "--build", "--no-deps", "--force-recreate", "propertyquarry-render-tools"]
    assert receipt["compose_up"]["build_requested"] is True


def test_render_bridge_runtime_receipt_syncs_when_running_container_code_is_stale(
    tmp_path: Path,
    monkeypatch,
) -> None:
    compose = tmp_path / "docker-compose.property.yml"
    compose.write_text("services:\n", encoding="utf-8")
    monkeypatch.setattr(runtime, "_docker_available", lambda: True)
    monkeypatch.setattr(runtime, "_compose_command", lambda **_: ["docker", "compose", "-f", str(compose)])

    states = iter(
        [
            {"exists": True, "status": "running", "health": "healthy", "detail": ""},
            {"exists": True, "status": "running", "health": "healthy", "detail": ""},
            {"exists": True, "status": "running", "health": "healthy", "detail": ""},
        ]
    )
    monkeypatch.setattr(runtime, "_container_state", lambda container: next(states))
    monkeypatch.setattr(
        runtime,
        "_container_health_probe",
        lambda container, *, health_url: {"status": "pass", "url": health_url},
    )

    parity_rows = iter(
        [
            {"status": "failed", "mismatched_paths": ["/app/scripts/generate_property_reconstruction.py"]},
            {"status": "pass", "checks": []},
        ]
    )
    monkeypatch.setattr(runtime, "_runtime_code_parity", lambda container: next(parity_rows))
    monkeypatch.setattr(
        runtime,
        "_copy_path_to_container",
        lambda container, local_path, container_path: {
            "status": "pass",
            "local_path": str(local_path),
            "container_path": container_path,
        },
    )

    receipt = runtime.build_render_bridge_runtime_receipt(
        container="propertyquarry-render-tools",
        service="propertyquarry-render-tools",
        compose_file=str(compose),
    )

    assert receipt["status"] == "pass"
    assert receipt["action"] == "synced_runtime_files"
    assert receipt["initial_code_parity"]["status"] == "failed"
    assert receipt["post_code_parity"]["status"] == "pass"
    assert receipt["runtime_file_sync"]["status"] == "pass"
    assert receipt["runtime_file_sync"]["copied"][0]["container_path"] == "/app/scripts/generate_property_reconstruction.py"


def test_render_bridge_runtime_receipt_rebuilds_when_runtime_file_sync_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    compose = tmp_path / "docker-compose.property.yml"
    compose.write_text("services:\n", encoding="utf-8")
    monkeypatch.setattr(runtime, "_docker_available", lambda: True)
    monkeypatch.setattr(runtime, "_compose_command", lambda **_: ["docker", "compose", "-f", str(compose)])

    states = iter(
        [
            {"exists": True, "status": "running", "health": "healthy", "detail": ""},
            {"exists": True, "status": "running", "health": "healthy", "detail": ""},
            {"exists": True, "status": "running", "health": "healthy", "detail": ""},
        ]
    )
    monkeypatch.setattr(runtime, "_container_state", lambda container: next(states))
    monkeypatch.setattr(
        runtime,
        "_container_health_probe",
        lambda container, *, health_url: {"status": "pass", "url": health_url},
    )

    parity_rows = iter(
        [
            {"status": "failed", "mismatched_paths": ["/app/scripts/generate_property_reconstruction.py"]},
            {"status": "pass", "checks": []},
        ]
    )
    monkeypatch.setattr(runtime, "_runtime_code_parity", lambda container: next(parity_rows))
    monkeypatch.setattr(
        runtime,
        "_copy_path_to_container",
        lambda container, local_path, container_path: {"status": "failed", "reason": "copy_failed"},
    )

    observed: dict[str, object] = {}

    def _fake_run(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        observed["command"] = list(command)
        observed["timeout"] = timeout
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(runtime, "_run", _fake_run)

    receipt = runtime.build_render_bridge_runtime_receipt(
        container="propertyquarry-render-tools",
        service="propertyquarry-render-tools",
        compose_file=str(compose),
    )

    assert receipt["status"] == "pass"
    assert receipt["action"] == "recreated_runtime"
    assert receipt["runtime_file_sync"]["status"] == "failed"
    assert observed["command"][-6:] == ["up", "-d", "--build", "--no-deps", "--force-recreate", "propertyquarry-render-tools"]
    assert receipt["compose_up"]["build_requested"] is True


def test_render_bridge_runtime_receipt_blocks_when_compose_file_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(runtime, "_docker_available", lambda: True)

    receipt = runtime.build_render_bridge_runtime_receipt(compose_file="/tmp/propertyquarry-missing-compose.yml")

    assert receipt["status"] == "blocked"
    assert receipt["reason"] == "compose_file_missing"


def test_property_runtime_images_copy_three_vendor_assets() -> None:
    dockerfile = Path("ea/Dockerfile.property").read_text(encoding="utf-8")
    dockerfile_web = Path("ea/Dockerfile.property-web").read_text(encoding="utf-8")

    assert "COPY vendor/three /app/vendor/three" in dockerfile
    assert "COPY vendor/three /app/vendor/three" in dockerfile_web
