from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_SIZE = ROOT / "scripts/db_size.sh"
DB_STATUS = ROOT / "scripts/db_status.sh"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_property_database_operator_scripts_default_to_the_live_standalone_database() -> None:
    for script in (DB_SIZE, DB_STATUS):
        source = _source(script)
        assert '${EA_DB_SERVICE:-propertyquarry-db}' in source
        assert 'DB_CONTAINER="propertyquarry-db-live"' in source
        assert 'DB_NAME="propertyquarry"' in source


def test_property_database_operator_scripts_keep_explicit_legacy_overrides() -> None:
    for script in (DB_SIZE, DB_STATUS):
        source = _source(script)
        assert 'EA_DB_CONTAINER' in source
        assert 'PROPERTYQUARRY_DB_CONTAINER_NAME' in source
        assert 'POSTGRES_DB' in source
        assert 'DB_NAME="ea"' in source


def test_database_status_readiness_is_bound_to_the_selected_database() -> None:
    source = _source(DB_STATUS)
    assert 'pg_isready -U "${DB_USER}" -d "${DB_NAME}"' in source
