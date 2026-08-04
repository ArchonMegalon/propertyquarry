from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest

from app.api.routes import landing as landing_routes
from app.api.routes import landing_property_workspace_payload as workspace_payload_routes
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


def _approved_hosted_tour_binding(
    *,
    candidate_refs: list[str] | None = None,
    listing_ids: list[str] | None = None,
    reviewed_at: str = "2026-08-04T15:45:33Z",
    evidence_sha256: str = "f653f438caafe3c4f4802c3692f2ae0919cc457d61b1602bfcd9a01be0279e38",
) -> dict[str, object]:
    resolved_candidate_refs = sorted(candidate_refs or ["candidate-a"])
    resolved_listing_ids = sorted(listing_ids or ["123456"])
    binding: dict[str, object] = {
        "binding_contract": "propertyquarry.curated_hosted_tour_binding.v1",
        "slug": "karl-czerny-gasse-2-urban-jungle",
        "provider": "3dvista",
        "default_mode": "camera_walkthrough",
        "hosted_tour_url": "/tours/karl-czerny-gasse-2-urban-jungle",
        "walkthrough_url": "/tours/karl-czerny-gasse-2-urban-jungle/walkthrough",
        "spatial_tour_url": "/tours/3dvista/karl-czerny-gasse-2-urban-jungle/3dvista/index.htm",
        "property_url_sha256": "c20cc5d801fa85982874524703514160d3aa6003456738ba0c816d6d4a825431",
    }
    binding_payload = {
        **binding,
        "candidate_refs": resolved_candidate_refs,
        "listing_ids": resolved_listing_ids,
    }
    binding["binding_sha256"] = hashlib.sha256(
        json.dumps(
            binding_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    binding["review"] = {
        "status": "approved",
        "reviewed_by": "codex-release-owner",
        "reviewed_at": reviewed_at,
        "evidence_sha256": evidence_sha256,
    }
    return binding


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


def test_landing_curated_diorama_applies_validated_camera_first_tour_binding() -> None:
    candidate: dict[str, object] = {
        "candidate_ref": "candidate-a",
        "title": "A city home",
    }
    landing_routes._property_apply_curated_diorama_preview(
        candidate,
        entry={
            "asset_url": "/static/property/research/approved.webp",
            "representation": "illustrative",
            "candidate_refs": ["candidate-a"],
            "listing_ids": ["123456"],
            "hosted_tour": _approved_hosted_tour_binding(),
        },
    )

    assert candidate["flythrough_url"] == (
        "/tours/karl-czerny-gasse-2-urban-jungle/walkthrough"
    )
    assert candidate["flythrough_status"] == "ready"
    assert candidate["tour_url"] == (
        "/tours/karl-czerny-gasse-2-urban-jungle"
    )
    assert candidate["tour_status"] == "ready"
    assert candidate["tour_provider"] == "3dvista"


def test_workspace_tour_and_walkthrough_readiness_are_owner_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        workspace_payload_routes.property_tour_hosting,
        "_hosted_property_tour_verified_provider",
        lambda _url, *, principal_id="": principal_calls.append(
            ("provider", principal_id)
        )
        or "3dvista",
    )
    monkeypatch.setattr(
        workspace_payload_routes.property_tour_hosting,
        "_hosted_property_tour_verified_open_url",
        lambda _url, *, principal_id="": principal_calls.append(
            ("tour", principal_id)
        )
        or "/tours/karl/control/3dvista",
    )
    monkeypatch.setattr(
        workspace_payload_routes.property_tour_hosting,
        "_hosted_property_tour_walkthrough_open_url",
        lambda _url, _walkthrough="", *, principal_id="": principal_calls.append(
            ("walkthrough", principal_id)
        )
        or "/tours/karl?pane=flythrough-pane&autoplay=1",
    )
    candidate = {
        "tour_url": "/tours/karl",
        "flythrough_url": "/tours/karl/walkthrough",
    }

    tour_url = workspace_payload_routes._property_workbench_candidate_ready_tour_url(
        candidate,
        principal_id="user-owner",
    )
    walkthrough_url = (
        workspace_payload_routes._property_workbench_candidate_flythrough_url(
            candidate,
            ready_tour_url=tour_url,
            principal_id="user-owner",
        )
    )

    assert tour_url == "/tours/karl/control/3dvista"
    assert walkthrough_url == "/tours/karl?pane=flythrough-pane&autoplay=1"
    assert [call for call in principal_calls if call[1]] == [
        ("provider", "user-owner"),
        ("tour", "user-owner"),
        ("walkthrough", "user-owner"),
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("default_mode", "stereoscopic"),
        ("provider", "unverified-viewer"),
        ("hosted_tour_url", "/tours/another-home"),
        ("walkthrough_url", "/tours/another-home/walkthrough"),
        ("spatial_tour_url", "https://vendor.invalid/tour"),
    ],
)
def test_landing_curated_diorama_rejects_invalid_tour_binding(
    field: str,
    value: str,
) -> None:
    hosted_tour = _approved_hosted_tour_binding()
    hosted_tour[field] = value
    candidate: dict[str, object] = {"candidate_ref": "candidate-a"}

    landing_routes._property_apply_curated_diorama_preview(
        candidate,
        entry={
            "asset_url": "/static/property/research/approved.webp",
            "candidate_refs": ["candidate-a"],
            "listing_ids": ["123456"],
            "hosted_tour": hosted_tour,
        },
    )

    assert candidate["diorama_preview_url"] == (
        "/static/property/research/approved.webp"
    )
    assert "tour_url" not in candidate
    assert "flythrough_url" not in candidate


def test_legacy_curated_alias_restores_and_marks_ranked_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical_ref = "ad48357be22535c1"
    legacy_ref = "cece2dad814fdf68"
    candidate = {
        "candidate_ref": canonical_ref,
        "title": "Karl candidate",
        "property_url": "https://listing.invalid/1536069684",
    }
    property_context: dict[str, object] = {
        "run": {
            "run_id": "owned-run",
            "summary": {"ranked_candidates": [candidate]},
        }
    }
    monkeypatch.setattr(
        landing_routes,
        "_property_candidate_ref",
        lambda row: str(row.get("candidate_ref") or ""),
    )
    monkeypatch.setattr(
        landing_routes,
        "_property_curated_diorama_candidate_refs",
        lambda _candidate_ref: (canonical_ref, legacy_ref),
    )
    monkeypatch.setattr(
        landing_routes,
        "_property_curated_diorama_preview_entry",
        lambda _candidate: {
            "asset_url": "/static/property/research/ad48357be22535c1-ai-diorama.webp",
            "representation": "illustrative",
            "candidate_refs": [canonical_ref, legacy_ref],
            "listing_ids": ["1536069684"],
            "hosted_tour": _approved_hosted_tour_binding(
                candidate_refs=[canonical_ref, legacy_ref],
                listing_ids=["1536069684"],
            ),
        },
    )

    selected_ref = landing_routes._property_resolve_scoped_curated_candidate_ref(
        requested_candidate_ref=legacy_ref,
        property_context=property_context,
    )

    assert selected_ref == canonical_ref
    restored = property_context["run"]["summary"]["ranked_candidates"][0]
    assert restored["_explicitly_selected_source_candidate"] is True
    assert restored["_selected_candidate_ref"] == canonical_ref
    assert restored["diorama_preview_url"].endswith(
        "ad48357be22535c1-ai-diorama.webp"
    )
    assert restored["flythrough_url"].endswith("/walkthrough")
    assert restored["tour_provider"] == "3dvista"


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
    assert rendered_entry["hosted_tour"] == _approved_hosted_tour_binding(
        candidate_refs=["ad48357be22535c1", "cece2dad814fdf68"],
        listing_ids=["1536069684"],
    )
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
