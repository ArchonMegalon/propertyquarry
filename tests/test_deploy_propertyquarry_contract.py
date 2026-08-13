from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_authoritative_deploy_preserves_canonical_backup_service() -> None:
    deploy = (ROOT / "scripts" / "deploy_propertyquarry.sh").read_text(encoding="utf-8")

    assert "|propertyquarry-backup|" in deploy
    assert "Refusing --remove-orphans with unexpected project services" in deploy
    assert "up --detach --remove-orphans --wait --wait-timeout 420" in deploy
