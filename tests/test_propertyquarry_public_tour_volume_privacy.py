from __future__ import annotations

import json
import stat
from pathlib import Path

from scripts.propertyquarry_public_tour_volume_privacy import audit_or_repair


def _write_bundle(root: Path, slug: str) -> Path:
    bundle = root / slug
    bundle.mkdir(parents=True)
    (bundle / "scene.jpg").write_bytes(b"scene")
    (bundle / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Safe title",
                "principal_id": "private-principal",
                "listing_url": "https://private.invalid/listing",
                "facts": {
                    "city": "Vienna",
                    "street_address": "Private Street 4",
                },
                "scenes": [
                    {
                        "name": "Scene",
                        "asset_relpath": "scene.jpg",
                        "recipient_email": "private@example.invalid",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (bundle / "tour.private.json").write_text(
        json.dumps({"existing_private_proof": {"status": "retained"}}),
        encoding="utf-8",
    )
    (bundle / "tour.private.json").chmod(0o644)
    return bundle


def test_audit_fails_closed_without_mutating(tmp_path: Path) -> None:
    root = tmp_path / "public"
    root.mkdir()
    bundle = _write_bundle(root, "tour-one")
    before = (bundle / "tour.json").read_bytes()

    receipt = audit_or_repair(root)

    assert receipt["status"] == "fail"
    assert receipt["counts"]["private_key_manifests"] == 1
    assert receipt["counts"]["private_mode_violations"] == 1
    assert (bundle / "tour.json").read_bytes() == before


def test_repair_snapshots_then_splits_public_and_private_payloads(
    tmp_path: Path,
) -> None:
    root = tmp_path / "public"
    backup = tmp_path / "backup"
    root.mkdir()
    bundle = _write_bundle(root, "tour-one")
    original = (bundle / "tour.json").read_bytes()

    receipt = audit_or_repair(root, apply=True, backup_root=backup)

    assert receipt["status"] == "pass"
    assert receipt["secret_values_recorded"] is False
    assert receipt["snapshot"]["tree_sha256"].startswith("sha256:")
    assert (
        backup / "public_property_tours" / "tour-one" / "tour.json"
    ).read_bytes() == original

    public = json.loads((bundle / "tour.json").read_text(encoding="utf-8"))
    serialized_public = json.dumps(public).lower()
    assert "private-principal" not in serialized_public
    assert "private.invalid" not in serialized_public
    assert "private@example.invalid" not in serialized_public
    assert "private street" not in serialized_public

    private_path = bundle / "tour.private.json"
    private = json.loads(private_path.read_text(encoding="utf-8"))
    assert private["existing_private_proof"]["status"] == "retained"
    assert private["principal_id"] == "private-principal"
    assert private["listing_url"] == "https://private.invalid/listing"
    assert private["legacy_private_fields"]["facts"]["street_address"] == (
        "Private Street 4"
    )
    assert stat.S_IMODE(private_path.stat().st_mode) == 0o600

    verification = audit_or_repair(root)
    assert verification["status"] == "pass"
    assert verification["counts"]["private_key_manifests"] == 0
    assert verification["counts"]["private_mode_violations"] == 0
