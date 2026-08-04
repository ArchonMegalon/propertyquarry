from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest

from app.api.routes import landing as landing_routes
from app.services.property_curated_diorama import (
    build_curated_diorama_entry_index,
    build_curated_diorama_preview_index,
    curated_diorama_governance_subject_sha256,
)
from tests.product_test_helpers import build_property_client


def _approved_review(status: str, *, subject_sha256: str) -> dict[str, str]:
    return {
        "status": status,
        "basis": "Reviewed source permissions and release evidence.",
        "reviewed_by": "release-reviewer",
        "reviewed_at": "2026-07-12T20:00:00Z",
        "subject_sha256": subject_sha256,
        "evidence_sha256": hashlib.sha256(f"{status}-review-evidence".encode()).hexdigest(),
    }


def _manifest_for(asset: Path, *, asset_url: str = "/static/property/research/approved.png") -> dict[str, object]:
    asset_sha256 = hashlib.sha256(asset.read_bytes()).hexdigest()
    source_asset_sha256s = [hashlib.sha256(b"licensed-source-input").hexdigest()]
    governance_subject_sha256 = curated_diorama_governance_subject_sha256(
        asset_sha256=asset_sha256,
        source_asset_sha256s=source_asset_sha256s,
    )
    return {
        "contract_name": "propertyquarry.curated_diorama_previews.v2",
        "entries": [
            {
                "preview_kind": "rendered_diorama",
                "asset_url": asset_url,
                "asset_sha256": asset_sha256,
                "source_asset_sha256s": source_asset_sha256s,
                "candidate_refs": ["Candidate-A"],
                "listing_ids": ["123456"],
                "governance": {
                    "rights": _approved_review("approved", subject_sha256=governance_subject_sha256),
                    "privacy": _approved_review("approved", subject_sha256=governance_subject_sha256),
                    "provenance": _approved_review("verified", subject_sha256=governance_subject_sha256),
                },
            }
        ],
    }


def test_curated_diorama_v2_requires_complete_approved_governance(tmp_path: Path) -> None:
    static_root = tmp_path / "static"
    asset = static_root / "property" / "research" / "approved.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"approved-render")

    assert build_curated_diorama_preview_index(_manifest_for(asset), static_root=static_root) == {
        "candidate:candidate-a": "/static/property/research/approved.png",
        "listing:123456": "/static/property/research/approved.png",
    }


def test_curated_drawn_diorama_preserves_illustrative_truth_metadata(
    tmp_path: Path,
) -> None:
    static_root = tmp_path / "static"
    asset = static_root / "property" / "research" / "approved.webp"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"approved-drawn-illustration")
    payload = _manifest_for(
        asset,
        asset_url="/static/property/research/approved.webp",
    )
    entry = payload["entries"][0]
    entry.update(
        {
            "preview_kind": "illustrative_drawn_diorama",
            "representation": "illustrative",
            "source_basis": "listing_metadata_only",
            "truth_boundary": (
                "Illustrative concept only, not listing evidence or measured geometry."
            ),
            "alt": "Illustrative hand-drawn property diorama",
        }
    )

    index = build_curated_diorama_entry_index(payload, static_root=static_root)

    assert index["candidate:candidate-a"] == index["listing:123456"]
    assert index["candidate:candidate-a"]["representation"] == "illustrative"
    assert (
        index["candidate:candidate-a"]["truth_boundary"]
        == "Illustrative concept only, not listing evidence or measured geometry."
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("representation", ""),
        ("representation", "reconstruction"),
        ("source_basis", ""),
        ("truth_boundary", ""),
    ],
)
def test_curated_drawn_diorama_rejects_missing_truth_boundary(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    static_root = tmp_path / "static"
    asset = static_root / "property" / "research" / "approved.webp"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"approved-drawn-illustration")
    payload = _manifest_for(
        asset,
        asset_url="/static/property/research/approved.webp",
    )
    payload["entries"][0].update(
        {
            "preview_kind": "illustrative_drawn_diorama",
            "representation": "illustrative",
            "source_basis": "listing_reference_image",
            "truth_boundary": "Illustrative orientation; geometry is not measured.",
        }
    )
    payload["entries"][0][field] = value

    assert build_curated_diorama_entry_index(payload, static_root=static_root) == {}


@pytest.mark.parametrize(
    ("review_name", "field", "value"),
    [
        ("rights", "status", "pending"),
        ("privacy", "basis", ""),
        ("provenance", "reviewed_by", ""),
        ("rights", "reviewed_at", "2026-07-12"),
        ("privacy", "reviewed_at", "2099-01-01T00:00:00Z"),
        ("rights", "subject_sha256", "0" * 64),
        ("provenance", "evidence_sha256", ""),
    ],
)
def test_curated_diorama_rejects_incomplete_or_unapproved_review(
    tmp_path: Path,
    review_name: str,
    field: str,
    value: str,
) -> None:
    static_root = tmp_path / "static"
    asset = static_root / "property" / "research" / "approved.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"approved-render")
    payload = _manifest_for(asset)
    payload["entries"][0]["governance"][review_name][field] = value

    assert build_curated_diorama_preview_index(payload, static_root=static_root) == {}


def test_curated_diorama_rejects_v1_hash_mismatch_and_path_escape(tmp_path: Path) -> None:
    static_root = tmp_path / "static"
    asset = static_root / "property" / "research" / "approved.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"approved-render")
    payload = _manifest_for(asset)

    payload["contract_name"] = "propertyquarry.curated_diorama_previews.v1"
    assert build_curated_diorama_preview_index(payload, static_root=static_root) == {}

    payload = _manifest_for(asset)
    payload["entries"][0]["asset_sha256"] = "0" * 64
    assert build_curated_diorama_preview_index(payload, static_root=static_root) == {}

    payload = _manifest_for(asset, asset_url="/static/../outside.png")
    assert build_curated_diorama_preview_index(payload, static_root=static_root) == {}


def test_curated_diorama_rejects_string_identifiers_and_collisions(tmp_path: Path) -> None:
    static_root = tmp_path / "static"
    first_asset = static_root / "property" / "research" / "first.png"
    second_asset = static_root / "property" / "research" / "second.png"
    first_asset.parent.mkdir(parents=True)
    first_asset.write_bytes(b"first-approved-render")
    second_asset.write_bytes(b"second-approved-render")

    payload = _manifest_for(first_asset, asset_url="/static/property/research/first.png")
    payload["entries"][0]["candidate_refs"] = "candidate-a"
    assert build_curated_diorama_preview_index(payload, static_root=static_root) == {}

    first_entry = _manifest_for(first_asset, asset_url="/static/property/research/first.png")["entries"][0]
    second_entry = _manifest_for(second_asset, asset_url="/static/property/research/second.png")["entries"][0]
    second_entry["candidate_refs"] = ["candidate-a"]
    payload = {
        "contract_name": "propertyquarry.curated_diorama_previews.v2",
        "entries": [first_entry, second_entry],
    }
    assert build_curated_diorama_preview_index(payload, static_root=static_root) == {}


def test_curated_diorama_rejects_symlinked_asset(tmp_path: Path) -> None:
    static_root = tmp_path / "static"
    real_asset = tmp_path / "real.png"
    real_asset.write_bytes(b"approved-render")
    asset = static_root / "property" / "research" / "approved.png"
    asset.parent.mkdir(parents=True)
    try:
        asset.symlink_to(real_asset)
    except OSError:
        pytest.skip("symlinks unavailable")

    assert build_curated_diorama_preview_index(_manifest_for(asset), static_root=static_root) == {}


@pytest.mark.parametrize(
    "contract_name",
    [
        "propertyquarry.curated_diorama_previews.v1",
        "propertyquarry.curated_diorama_previews.v2",
    ],
)
def test_landing_curated_diorama_loader_rejects_legacy_or_unapproved_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contract_name: str,
) -> None:
    static_root = tmp_path / "static"
    asset = static_root / "property" / "research" / "unapproved.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"unapproved-render")
    manifest_path = tmp_path / "property_diorama_previews.json"
    manifest_path.write_text(
        json.dumps(
            {
                "contract_name": contract_name,
                "entries": [
                    {
                        "preview_kind": "rendered_diorama",
                        "asset_url": "/static/property/research/unapproved.png",
                        "asset_sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
                        "candidate_refs": ["candidate-unapproved"],
                        "listing_ids": ["123456"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(landing_routes, "_PROPERTY_CURATED_DIORAMA_MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(landing_routes, "_PROPERTY_CURATED_DIORAMA_STATIC_ROOT", static_root)
    landing_routes._property_curated_diorama_preview_index.cache_clear()
    landing_routes._property_curated_diorama_entry_index.cache_clear()
    try:
        assert landing_routes._property_curated_diorama_preview_index() == {}
        assert landing_routes._property_curated_diorama_entry_index() == {}
    finally:
        landing_routes._property_curated_diorama_preview_index.cache_clear()
        landing_routes._property_curated_diorama_entry_index.cache_clear()


def test_landing_curated_drawn_diorama_overrides_stale_runtime_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    static_root = tmp_path / "static"
    asset = static_root / "property" / "research" / "approved.webp"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"approved-drawn-illustration")
    payload = _manifest_for(
        asset,
        asset_url="/static/property/research/approved.webp",
    )
    payload["entries"][0].update(
        {
            "preview_kind": "illustrative_drawn_diorama",
            "representation": "illustrative",
            "source_basis": "listing_reference_image",
            "truth_boundary": (
                "Hand-drawn orientation only; listing media remains the evidence."
            ),
        }
    )
    manifest_path = tmp_path / "property_diorama_previews.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        landing_routes,
        "_PROPERTY_CURATED_DIORAMA_MANIFEST_PATH",
        manifest_path,
    )
    monkeypatch.setattr(
        landing_routes,
        "_PROPERTY_CURATED_DIORAMA_STATIC_ROOT",
        static_root,
    )
    landing_routes._property_curated_diorama_preview_index.cache_clear()
    landing_routes._property_curated_diorama_entry_index.cache_clear()
    candidate: dict[str, object] = {
        "candidate_ref": "candidate-a",
        "title": "A city home",
        "diorama_preview_url": "/static/property/research/stale-missing.png",
    }
    try:
        entry = landing_routes._property_curated_diorama_preview_entry(candidate)
        landing_routes._property_apply_curated_diorama_preview(
            candidate,
            entry=entry,
        )
    finally:
        landing_routes._property_curated_diorama_preview_index.cache_clear()
        landing_routes._property_curated_diorama_entry_index.cache_clear()

    assert candidate["diorama_preview_url"] == "/static/property/research/approved.webp"
    assert candidate["diorama_representation"] == "illustrative"
    assert candidate["diorama_alt"] == "Illustrative hand-drawn diorama of A city home"
    assert candidate["diorama_scene"] == {
        "image_url": "/static/property/research/approved.webp",
        "alt": "Illustrative hand-drawn diorama of A city home",
        "representation": "illustrative",
        "source_basis": "listing_reference_image",
        "truth_boundary": (
            "Hand-drawn orientation only; listing media remains the evidence."
        ),
        "preview_kind": "illustrative_drawn_diorama",
    }


def test_exact_shortlist_run_renders_all_drawn_diorama_thumbnails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    manifest = json.loads(
        (
            repo_root
            / "ea"
            / "app"
            / "data"
            / "property_diorama_previews.json"
        ).read_text(encoding="utf-8")
    )
    run_id = "9dd6a4993d7245a0acf48aeb50c44a9b"
    candidates: list[dict[str, object]] = []
    for entry in list(manifest.get("entries") or []):
        candidate_ref = str(entry["candidate_refs"][0])
        listing_ids = list(entry.get("listing_ids") or [])
        candidates.append(
            {
                "candidate_ref": candidate_ref,
                "listing_id": str(listing_ids[0]) if listing_ids else "",
                "title": f"Shortlist home {entry['rank']}",
                "property_url": f"https://listings.invalid/{candidate_ref}",
                "source_label": "Shortlist source",
            }
        )
    candidates[0]["diorama_preview_url"] = (
        "/static/property/research/stale-runtime-preview.png"
    )
    run_payload = {
        "run_id": run_id,
        "status": "processed",
        "summary": {
            "run_id": run_id,
            "ranked_candidates": candidates,
        },
    }

    class _RunProduct:
        def get_property_search_run_status(self, **_kwargs: object) -> dict[str, object]:
            return run_payload

    monkeypatch.setattr(
        landing_routes,
        "build_product_service",
        lambda _container: _RunProduct(),
    )
    monkeypatch.setattr(
        landing_routes,
        "_propertyquarry_shortlist_run_is_recent",
        lambda **_kwargs: True,
    )
    landing_routes._property_curated_diorama_preview_index.cache_clear()
    landing_routes._property_curated_diorama_entry_index.cache_clear()
    client = build_property_client(principal_id="pq-drawn-diorama-run")

    response = client.get(
        f"/app/shortlist/run/{run_id}",
        headers={"host": "propertyquarry.com"},
    )

    assert response.status_code == 200
    asset_urls = re.findall(
        r'<img src="(/static/property/research/[^"]+-drawn-diorama\.webp)"',
        response.text,
    )
    assert len(asset_urls) == 40
    assert len(set(asset_urls)) == 40
    assert "stale-runtime-preview" not in response.text
    drawn_rows = [
        row
        for row in re.findall(
            r'<article class="pq-fast-row">(.*?)</article>',
            response.text,
            re.DOTALL,
        )
        if "-drawn-diorama.webp" in row
    ]
    assert len(drawn_rows) == 40
    assert all(">Illustrative</span>" in row for row in drawn_rows)
    assert (
        response.text.count('alt="Illustrative hand-drawn property diorama"')
        == 40
    )


def test_tracked_curated_diorama_assets_are_not_orphaned() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    tracked_assets = {
        line.strip()
        for line in subprocess.check_output(
            ["git", "ls-files", "--", "ea/app/static/property/research"],
            cwd=repo_root,
            text=True,
        ).splitlines()
        if Path(line.strip()).suffix.lower() in {".avif", ".jpeg", ".jpg", ".png", ".webp"}
    }
    manifest_path = repo_root / "ea" / "app" / "data" / "property_diorama_previews.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    index = build_curated_diorama_preview_index(
        payload,
        static_root=repo_root / "ea" / "app" / "static",
    )
    approved_assets = {
        f"ea/app/static/{asset_url.removeprefix('/static/')}"
        for asset_url in index.values()
    }
    assert tracked_assets == approved_assets


def test_tracked_drawn_diorama_manifest_is_complete_and_truthful() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    manifest_path = (
        repo_root / "ea" / "app" / "data" / "property_diorama_previews.json"
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = list(payload.get("entries") or [])

    assert payload["contract_name"] == "propertyquarry.curated_diorama_previews.v2"
    assert payload["run_binding"] == {
        "run_id": "9dd6a4993d7245a0acf48aeb50c44a9b",
        "candidate_count": len(entries),
    }
    assert len(entries) == 41
    assert {entry["rank"] for entry in entries} == set(range(1, 42))
    assert sum(
        entry["preview_kind"] == "illustrative_drawn_diorama"
        for entry in entries
    ) == 40
    assert sum(entry["preview_kind"] == "rendered_diorama" for entry in entries) == 1
    rendered_entry = next(
        entry for entry in entries if entry["preview_kind"] == "rendered_diorama"
    )
    assert rendered_entry["candidate_refs"] == [
        "ad48357be22535c1",
        "cece2dad814fdf68",
    ]
    assert all(
        entry["preview_kind"]
        in {"illustrative_drawn_diorama", "rendered_diorama"}
        and entry["representation"] == "illustrative"
        and "not listing evidence" in entry["truth_boundary"]
        and entry["source_basis"]
        in {
            "listing_reference_image",
            "listing_metadata_only",
            "listing_floorplan_and_reference_images",
        }
        for entry in entries
    )
