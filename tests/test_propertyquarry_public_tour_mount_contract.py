from __future__ import annotations

from pathlib import Path

import yaml


COMPOSE_PATH = (
    Path(__file__).resolve().parents[1] / "docker-compose.property.yml"
)
DYNAMIC_VOLUME = "propertyquarry_public_tours"
DYNAMIC_TARGET = "/data/public_property_tours"
GOVERNED_VOLUME = "propertyquarry_governed_public_tours"
GOVERNED_DOCKER_NAME = "property_propertyquarry_governed_public_tours"
GOVERNED_TARGET = "/data/governed_public_property_tours"
EXPECTED_SERVICES = {
    "propertyquarry-api",
    "propertyquarry-scheduler",
    "propertyquarry-render-tools",
}


def _compose() -> dict[str, object]:
    compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    assert isinstance(compose, dict)
    return compose


def _volume_mounts(
    compose: dict[str, object],
    volume_name: str,
) -> dict[str, list[object]]:
    services = compose.get("services")
    assert isinstance(services, dict)
    result: dict[str, list[object]] = {}
    for service_name, raw_service in services.items():
        assert isinstance(raw_service, dict)
        matches: list[object] = []
        for mount in raw_service.get("volumes") or []:
            if isinstance(mount, str):
                if mount.split(":", 1)[0] == volume_name:
                    matches.append(mount)
            elif (
                isinstance(mount, dict)
                and mount.get("source") == volume_name
            ):
                matches.append(mount)
        if matches:
            result[str(service_name)] = matches
    return result


def test_dynamic_public_tour_writers_retain_read_write_mounts() -> None:
    mounts = _volume_mounts(_compose(), DYNAMIC_VOLUME)

    assert set(mounts) == EXPECTED_SERVICES
    for service_name, service_mounts in mounts.items():
        assert service_mounts == [
            f"{DYNAMIC_VOLUME}:{DYNAMIC_TARGET}"
        ], service_name


def test_governed_public_tour_consumers_are_read_only() -> None:
    compose = _compose()
    mounts = _volume_mounts(compose, GOVERNED_VOLUME)

    assert set(mounts) == EXPECTED_SERVICES
    for service_name, service_mounts in mounts.items():
        assert service_mounts == [
            f"{GOVERNED_VOLUME}:{GOVERNED_TARGET}:ro"
        ], service_name

    volumes = compose.get("volumes")
    assert isinstance(volumes, dict)
    assert volumes[GOVERNED_VOLUME] == {"name": GOVERNED_DOCKER_NAME}

    services = compose["services"]
    assert isinstance(services, dict)
    api = services["propertyquarry-api"]
    assert isinstance(api, dict)
    environment = api.get("environment")
    assert isinstance(environment, dict)
    assert environment["EA_GOVERNED_PUBLIC_TOUR_DIR"] == GOVERNED_TARGET
