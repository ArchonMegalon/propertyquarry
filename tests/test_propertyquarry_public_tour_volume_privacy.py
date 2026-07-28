from __future__ import annotations

import json
import stat
from pathlib import Path

from scripts import propertyquarry_public_tour_volume_privacy as privacy
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


def test_minimal_web_image_packages_the_live_volume_auditor() -> None:
    dockerfile = (
        Path(__file__).resolve().parents[1] / "ea" / "Dockerfile.property-web"
    ).read_text(encoding="utf-8")

    assert (
        "COPY --chmod=0555 scripts/propertyquarry_public_tour_volume_privacy.py "
        "/app/scripts/propertyquarry_public_tour_volume_privacy.py"
    ) in dockerfile


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


def test_repair_preserves_legacy_declared_slug_and_binds_public_directory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "public"
    backup = tmp_path / "backup"
    root.mkdir()
    bundle = _write_bundle(root, "served-tour")
    manifest_path = bundle / "tour.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["slug"] = "legacy-tour"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    receipt = audit_or_repair(root, apply=True, backup_root=backup)

    assert receipt["status"] == "pass"
    public = json.loads(manifest_path.read_text(encoding="utf-8"))
    private = json.loads(
        (bundle / "tour.private.json").read_text(encoding="utf-8")
    )
    assert public["slug"] == "served-tour"
    assert private["legacy_declared_slug"] == "legacy-tour"
    assert audit_or_repair(root)["status"] == "pass"


def test_public_projection_converges_before_repair_write(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    class Projection:
        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def as_dict(self) -> dict[str, object]:
            return dict(self.payload)

    def build(payload: dict[str, object], **_kwargs: object) -> Projection:
        calls.append(dict(payload))
        if len(calls) == 1:
            return Projection(
                {"slug": "served-tour", "scenes": [{"name": "stale"}]}
            )
        return Projection({"slug": "served-tour", "scenes": []})

    monkeypatch.setattr(privacy, "build_public_tour_manifest", build)

    result = privacy._canonical_public_payload(
        {"slug": "served-tour"},
        bundle_dir=tmp_path,
    )

    assert result == {"slug": "served-tour", "scenes": []}
    assert len(calls) == 3
