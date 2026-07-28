from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _compose_services(path: str) -> dict[str, dict[str, object]]:
    document = yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))
    assert type(document) is dict
    services = document.get("services")
    assert type(services) is dict
    return services


def _assert_bounded_service(
    service: dict[str, object],
    *,
    memory_default: str,
    pids_default: int,
    restart_default: str,
    independent_memswap_limit: str | None = None,
) -> None:
    assert service["init"] is True
    assert str(service["cpus"]).strip()
    assert str(service["mem_limit"]).endswith(f":-{memory_default}}}")
    if independent_memswap_limit is None:
        assert service["memswap_limit"] == service["mem_limit"]
    else:
        assert service["memswap_limit"] == independent_memswap_limit
    assert str(service["mem_reservation"]).strip()
    assert str(service["pids_limit"]).endswith(f":-{pids_default}}}")
    assert int(service["oom_score_adj"]) > 0
    assert service["restart"] == restart_default
    logging = service["logging"]
    assert type(logging) is dict
    assert logging == {
        "driver": "json-file",
        "options": {"max-size": "10m", "max-file": "3"},
    }


def test_primary_propertyquarry_compose_bounds_every_process() -> None:
    services = _compose_services("docker-compose.property.yml")
    assert set(services) == {
        "propertyquarry-api",
        "propertyquarry-migrate",
        "propertyquarry-worker",
        "propertyquarry-scheduler",
        "propertyquarry-render-tools",
        "propertyquarry-db",
    }

    expected = {
        "propertyquarry-api": ("1536m", 128, "${PROPERTYQUARRY_API_RESTART_POLICY:-on-failure:3}"),
        "propertyquarry-migrate": ("768m", 64, "no"),
        "propertyquarry-worker": (
            "1536m",
            128,
            "${PROPERTYQUARRY_WORKER_RESTART_POLICY:-on-failure:3}",
        ),
        "propertyquarry-scheduler": (
            "1536m",
            128,
            "${PROPERTYQUARRY_SCHEDULER_RESTART_POLICY:-on-failure:3}",
        ),
        "propertyquarry-render-tools": (
            "4g",
            256,
            "${PROPERTYQUARRY_RENDER_RESTART_POLICY:-on-failure:3}",
        ),
        "propertyquarry-db": (
            "1536m",
            128,
            "${PROPERTYQUARRY_DB_RESTART_POLICY:-on-failure:3}",
        ),
    }
    for name, (memory_default, pids_default, restart_default) in expected.items():
        _assert_bounded_service(
            services[name],
            memory_default=memory_default,
            pids_default=pids_default,
            restart_default=restart_default,
            independent_memswap_limit=(
                "${PROPERTYQUARRY_RENDER_MEMORY_SWAP_LIMIT:-4g}"
                if name == "propertyquarry-render-tools"
                else None
            ),
        )

    for name in (
        "propertyquarry-api",
        "propertyquarry-migrate",
        "propertyquarry-worker",
        "propertyquarry-scheduler",
        "propertyquarry-render-tools",
    ):
        assert services[name]["tmpfs"]
    render = services["propertyquarry-render-tools"]
    assert render["shm_size"] == "${PROPERTYQUARRY_RENDER_SHM_SIZE:-256m}"
    assert render["tmpfs"] == [
        "/tmp:size=${PROPERTYQUARRY_RENDER_TMPFS_LIMIT:-512m},mode=1777",
    ]
    assert services["propertyquarry-db"]["shm_size"] == "${PROPERTYQUARRY_DB_SHM_LIMIT:-256m}"
    scheduler_environment = services["propertyquarry-scheduler"]["environment"]
    assert type(scheduler_environment) is dict
    assert scheduler_environment["EA_SCHEDULER_STEP_CONCURRENCY_LIMIT"] == "1"


def test_propertyquarry_worker_is_a_hardened_property_only_durable_consumer() -> None:
    services = _compose_services("docker-compose.property.yml")
    worker = services["propertyquarry-worker"]
    api = services["propertyquarry-api"]
    environment = worker["environment"]
    assert type(environment) is dict

    assert worker["image"] == api["image"]
    assert worker["read_only"] is True
    assert worker["cap_drop"] == ["ALL"]
    assert worker["security_opt"] == ["no-new-privileges:true"]
    assert "ports" not in worker
    assert "networks" not in worker
    assert "env_file" not in worker
    assert worker["volumes"] == [
        "./config:/config:ro",
        "./config:/app/config:ro",
        "propertyquarry_artifacts:/data/artifacts",
        "propertyquarry_provider_ledger:/data/provider-ledger",
    ]
    assert worker["depends_on"] == {
        "propertyquarry-db": {"condition": "service_healthy"},
        "propertyquarry-migrate": {"condition": "service_completed_successfully"},
    }
    assert worker["healthcheck"]["test"] == [
        "CMD",
        "/usr/local/bin/python",
        "-m",
        "app.scheduler_healthcheck",
    ]
    assert environment["EA_ROLE"] == "worker"
    assert environment["EA_STORAGE_BACKEND"] == "postgres"
    assert environment["PROPERTYQUARRY_WORKER_PROFILE"] == "property_only"
    assert environment["PROPERTYQUARRY_SEARCH_SCHEMA_READINESS_REQUIRED"] == "1"
    assert environment["EA_WORKER_HEARTBEAT_PATH"] == (
        "/data/artifacts/propertyquarry-worker-heartbeat.json"
    )
    assert environment["EA_WORKER_HEARTBEAT_MAX_AGE_SECONDS"] == (
        "${EA_WORKER_HEARTBEAT_MAX_AGE_SECONDS:-120}"
    )
    for key in (
        "DATABASE_URL",
        "PROPERTYQUARRY_RELEASE_COMMIT_SHA",
        "PROPERTYQUARRY_RELEASE_IMAGE_DIGEST",
        "PROPERTYQUARRY_RELEASE_DEPLOYMENT_ID",
    ):
        assert str(environment[key]).strip()
    assert not any(
        token in key
        for key in environment
        for token in ("MAGICFIT", "OMAGIC", "SCENE_VIDEO", "RECONSTRUCTION_RENDER")
    )


def test_propertyquarry_tour_lanes_share_persistent_fail_closed_host_limits() -> None:
    services = _compose_services("docker-compose.property.yml")
    shared_lock_dir = "/data/artifacts/propertyquarry-tour-locks"
    shared_artifact_mount = "propertyquarry_artifacts:/data/artifacts"
    tour_min_free_bytes = "${PROPERTYQUARRY_TOUR_MIN_FREE_BYTES:-10737418240}"

    for name in ("propertyquarry-api", "propertyquarry-scheduler"):
        service = services[name]
        environment = service["environment"]
        assert type(environment) is dict
        assert environment["PROPERTYQUARRY_TOUR_MIN_FREE_BYTES"] == tour_min_free_bytes
        assert environment["PROPERTYQUARRY_TOUR_LOCK_DIR"] == shared_lock_dir
        assert shared_artifact_mount in service["volumes"]

    scheduler_environment = services["propertyquarry-scheduler"]["environment"]
    assert type(scheduler_environment) is dict
    assert scheduler_environment["PROPERTYQUARRY_MAGICFIT_RENDER_MIN_FREE_BYTES"] == (
        "${PROPERTYQUARRY_MAGICFIT_RENDER_MIN_FREE_BYTES:-10737418240}"
    )
    assert scheduler_environment["PROPERTYQUARRY_MAGICFIT_RENDER_MAX_RETRIES"] == (
        "${PROPERTYQUARRY_MAGICFIT_RENDER_MAX_RETRIES:-3}"
    )
    assert scheduler_environment["PROPERTYQUARRY_MAGICFIT_RENDER_MAX_TIMEOUT_MINUTES"] == (
        "${PROPERTYQUARRY_MAGICFIT_RENDER_MAX_TIMEOUT_MINUTES:-30}"
    )

    render = services["propertyquarry-render-tools"]
    render_environment = render["environment"]
    assert type(render_environment) is dict
    for name in (
        "EA_ARTIFACTS_DIR",
        "PROPERTYQUARRY_TOUR_MIN_FREE_BYTES",
        "PROPERTYQUARRY_TOUR_LOCK_DIR",
        "PROPERTYQUARRY_MAGICFIT_RENDER_MIN_FREE_BYTES",
        "PROPERTYQUARRY_MAGICFIT_RENDER_MAX_RETRIES",
        "PROPERTYQUARRY_MAGICFIT_RENDER_MAX_TIMEOUT_MINUTES",
    ):
        assert name not in render_environment
    render_volumes = render["volumes"]
    assert type(render_volumes) is list
    assert render_volumes == [
        "propertyquarry_artifacts:/data/artifacts",
        "propertyquarry_public_tours:/data/public_property_tours",
    ]
    assert not render_volumes[1].endswith(":ro")


def test_propertyquarry_tunnel_is_bounded_and_read_only() -> None:
    services = _compose_services("docker-compose.cloudflared.yml")
    service = services["propertyquarry-cloudflared"]
    _assert_bounded_service(
        service,
        memory_default="128m",
        pids_default=64,
        restart_default="${PROPERTYQUARRY_CLOUDFLARED_RESTART_POLICY:-on-failure:3}",
    )
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["tmpfs"]
    assert service["command"] == "tunnel --no-autoupdate run"
    assert service["stop_grace_period"] == "45s"
