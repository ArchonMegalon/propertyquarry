from __future__ import annotations

import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
BACKUP_SCRIPT = ROOT / "scripts" / "propertyquarry_postgres_backup.sh"
COMPOSE_FILE = ROOT / "docker-compose.property.yml"


def test_propertyquarry_backup_script_is_valid_and_guards_its_delete_scope() -> None:
    checked = subprocess.run(
        ["/bin/sh", "-n", str(BACKUP_SCRIPT)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert checked.returncode == 0, checked.stderr
    source = BACKUP_SCRIPT.read_text(encoding="utf-8")
    assert 'if [ "$backup_dir" = "/" ]' in source
    assert "propertyquarry-*.dump" in source
    assert "pg_restore --list" in source
    assert ".last-success" in source
    assert "sha256sum -c" in source
    assert "propertyquarry:public-cache" in source
    assert "VACUUM (ANALYZE) observation_events" in source


def test_propertyquarry_compose_runs_verified_bounded_database_backups() -> None:
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    service = compose["services"]["propertyquarry-backup"]

    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert service["restart"] == "${PROPERTYQUARRY_BACKUP_RESTART_POLICY:-unless-stopped}"
    assert service["environment"]["PROPERTYQUARRY_BACKUP_RETENTION_DAYS"] == (
        "${PROPERTYQUARRY_BACKUP_RETENTION_DAYS:-7}"
    )
    assert service["environment"]["PROPERTYQUARRY_BACKUP_MAX_FILES"] == (
        "${PROPERTYQUARRY_BACKUP_MAX_FILES:-8}"
    )
    assert service["environment"]["PROPERTYQUARRY_PUBLIC_CACHE_EVENT_RETENTION_DAYS"] == (
        "${PROPERTYQUARRY_PUBLIC_CACHE_EVENT_RETENTION_DAYS:-7}"
    )
    assert service["healthcheck"]["test"][-1] == "health"
    assert "propertyquarry_backups:/backups" in service["volumes"]
    assert service["depends_on"]["propertyquarry-db"]["condition"] == "service_healthy"


def test_propertyquarry_compose_uses_tighter_run_and_membership_retention_defaults() -> None:
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))

    for service_name in (
        "propertyquarry-api",
        "propertyquarry-worker",
        "propertyquarry-scheduler",
    ):
        environment = compose["services"][service_name]["environment"]
        assert environment["EA_PROPERTY_SEARCH_RUN_RETENTION_SECONDS"] == (
            "${EA_PROPERTY_SEARCH_RUN_RETENTION_SECONDS:-2592000}"
        )
        assert environment["EA_PROPERTY_SEARCH_MEMBERSHIP_RETENTION_SECONDS"] == (
            "${EA_PROPERTY_SEARCH_MEMBERSHIP_RETENTION_SECONDS:-1209600}"
        )
        assert environment["EA_PROPERTY_SEARCH_RUN_MAX_ROWS_PER_PRINCIPAL"] == (
            "${EA_PROPERTY_SEARCH_RUN_MAX_ROWS_PER_PRINCIPAL:-100}"
        )
