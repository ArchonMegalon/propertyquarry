from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import propertyquarry_deploy_writer_topology as topology


def _container(
    *,
    container_id: str,
    name: str,
    database_url: str | None,
    running: bool = True,
    extra_environment: list[str] | None = None,
) -> dict[str, object]:
    environment = list(extra_environment or [])
    if database_url is not None:
        environment.append(f"DATABASE_URL={database_url}")
    return {
        "Id": container_id,
        "Name": f"/{name}",
        "Config": {"Env": environment},
        "State": {"Running": running},
    }


def test_pinned_topology_declares_every_target_role_and_no_implicit_external_writer() -> None:
    payload = topology.load_topology()

    assert topology.topology_digest() == topology.TOPOLOGY_SHA256
    assert set(payload["target"]) == set(topology.ROLE_NAMES)
    assert topology.allowed_database_writer_names(payload) == [
        "propertyquarry-api",
        "propertyquarry-worker",
        "propertyquarry-scheduler",
        "propertyquarry-migrate",
    ]
    assert payload["external_database_writers"] == []


def test_production_target_cannot_diverge_from_pinned_topology() -> None:
    payload = topology.load_topology()
    values = {"compose_project": payload["compose_project"]}
    for role, row in payload["target"].items():
        values[f"{role}_service"] = row["service"]
        values[f"{role}_container"] = row["container"]
    topology.validate_production_target(payload, values)
    values["render_container"] = "attacker-render"

    with pytest.raises(topology.WriterTopologyError, match="render_container"):
        topology.validate_production_target(payload, values)


def test_inventory_discovers_pinned_writers_without_trusting_literal_dsn() -> None:
    database_url = "postgresql://writer:secret@db.internal/property"
    payload = [
        _container(
            container_id="a" * 64,
            name="propertyquarry-api",
            database_url=database_url,
        ),
        _container(
            container_id="b" * 64,
            name="propertyquarry-worker",
            database_url=database_url,
        ),
        _container(
            container_id="c" * 64,
            name="propertyquarry-migrate",
            database_url=None,
            extra_environment=["DATABASE_URL_FILE=/run/secrets/database-url"],
        ),
        _container(
            container_id="d" * 64,
            name="other-database",
            database_url="postgresql://writer:secret@other.internal/property",
        ),
        _container(
            container_id="e" * 64,
            name="stopped-writer",
            database_url=database_url,
            running=False,
        ),
    ]

    assert topology._inventory_from_inspect(
        payload,
        [
            "propertyquarry-api",
            "propertyquarry-worker",
            "propertyquarry-migrate",
        ],
    ) == [
        ("a" * 64, "propertyquarry-api"),
        ("c" * 64, "propertyquarry-migrate"),
        ("b" * 64, "propertyquarry-worker"),
    ]


def test_duplicate_or_invalid_pinned_writer_inventory_fails_closed() -> None:
    row = _container(
        container_id="a" * 64,
        name="propertyquarry-api",
        database_url=None,
    )
    duplicate = dict(row)
    duplicate["Id"] = "b" * 64

    with pytest.raises(topology.WriterTopologyError, match="duplicate"):
        topology._inventory_from_inspect(
            [row, duplicate],
            ["propertyquarry-api"],
        )
    with pytest.raises(topology.WriterTopologyError, match="allowlist"):
        topology._inventory_from_inspect([row], ["unsafe/name"])


def test_topology_file_change_requires_compiled_pin_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = topology.load_topology()
    payload["external_database_writers"] = [
        {"container": "new-cross-stack-writer", "restore_precommit": False}
    ]
    path = tmp_path / "changed-topology.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(topology, "TOPOLOGY_PATH", path)

    with pytest.raises(topology.WriterTopologyError, match="compiled release pin"):
        topology.load_topology()
