from __future__ import annotations

import json
from pathlib import Path

from app.api.routes import public_tours
from app.api.routes.public_tour_payloads import build_public_tour_manifest
from app.product import property_tour_hosting


PRIVATE_MARKERS = (
    "principal_id",
    "search_run_id",
    "candidate_ref",
    "listing_url",
    "property_url",
    "source_ref",
    "external_id",
    "recipient_email",
    "source_virtual_tour_url",
    "video_provider",
    "video_provider_key",
    "video_render_provider",
    "video_coverage_proof",
)


def _write_bundle(monkeypatch, tmp_path: Path, *, slug: str = "raw-privacy") -> Path:
    bundle = tmp_path / slug
    bundle.mkdir()
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    property_tour_hosting._write_hosted_property_tour_payload(  # noqa: SLF001
        bundle,
        {
            "slug": slug,
            "title": "Private Street 7 penthouse",
            "principal_id": "cf-email:owner@example.test",
            "search_run_id": "run-private-1",
            "candidate_ref": "candidate-private-1",
            "listing_url": "https://listing.example.test/private/1",
            "property_url": "https://broker.example.test/private/1",
            "source_ref": "property-scout:private-1",
            "external_id": "external-private-1",
            "recipient_email": "owner@example.test",
            "source_virtual_tour_url": "https://vendor.example.test/private-tour",
            "video_provider": "internal_renderer",
            "video_provider_key": "internal_renderer_key",
            "video_render_provider": "internal_render_lane",
            "video_coverage_proof": "internal_acceptance_receipt",
            "facts": {
                "rooms": 4,
                "area_sqm": 180,
                "floor": "penthouse",
                "postal_name": "Smalltown 1010",
                "municipality": "Smalltown",
                "purchase_price_eur": 2_900_000,
                "municipality_population": 8_000,
                "exact_address": "Private Street 7, Smalltown 1010",
                "street_address": "Private Street 7",
                "latitude": 48.21,
                "longitude": 16.37,
            },
            "scenes": [],
        },
    )
    return bundle


def test_raw_public_tour_json_contains_no_private_receipt_fields(
    monkeypatch,
    tmp_path: Path,
) -> None:
    bundle = _write_bundle(monkeypatch, tmp_path)
    public_payload = json.loads(
        (bundle / "tour.json").read_text(encoding="utf-8")
    )
    serialized = json.dumps(public_payload, sort_keys=True)

    for marker in PRIVATE_MARKERS:
        assert marker not in public_payload
        assert marker not in serialized


def test_raw_public_tour_json_contains_no_exact_location_keys(
    monkeypatch,
    tmp_path: Path,
) -> None:
    bundle = _write_bundle(monkeypatch, tmp_path)
    public_payload = json.loads(
        (bundle / "tour.json").read_text(encoding="utf-8")
    )
    serialized = json.dumps(public_payload, sort_keys=True).lower()

    for marker in (
        "exact_address",
        "street_address",
        "latitude",
        "longitude",
        "private street 7",
    ):
        assert marker not in serialized


def test_raw_public_tour_json_contains_no_listing_url_property_url_source_url(
    monkeypatch,
    tmp_path: Path,
) -> None:
    bundle = _write_bundle(monkeypatch, tmp_path)
    serialized = (bundle / "tour.json").read_text(encoding="utf-8").lower()

    assert "listing.example.test" not in serialized
    assert "broker.example.test" not in serialized
    assert "vendor.example.test" not in serialized
    assert "listing_url" not in serialized
    assert "property_url" not in serialized
    assert "source_virtual_tour_url" not in serialized


def test_private_tour_receipt_contains_private_fields_with_0600_permissions(
    monkeypatch,
    tmp_path: Path,
) -> None:
    bundle = _write_bundle(monkeypatch, tmp_path)
    path = bundle / "tour.private.json"
    private_payload = json.loads(path.read_text(encoding="utf-8"))

    assert path.stat().st_mode & 0o777 == 0o600
    assert private_payload["principal_id"] == "cf-email:owner@example.test"
    assert private_payload["search_run_id"] == "run-private-1"
    assert private_payload["candidate_ref"] == "candidate-private-1"
    assert private_payload["listing_url"].startswith("https://listing.example.test/")
    assert private_payload["property_url"].startswith("https://broker.example.test/")
    assert private_payload["source_virtual_tour_url"].startswith(
        "https://vendor.example.test/"
    )
    assert private_payload["video_provider"] == "internal_renderer"
    assert private_payload["video_provider_key"] == "internal_renderer_key"
    assert private_payload["video_render_provider"] == "internal_render_lane"
    assert private_payload["video_coverage_proof"] == (
        "internal_acceptance_receipt"
    )
    assert private_payload["private_exact_location"]


def test_internal_tour_loader_restores_private_video_contract_fields(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _write_bundle(monkeypatch, tmp_path)

    public_payload = public_tours._load_tour("raw-privacy")
    internal_payload = public_tours._load_tour_with_private_receipt(
        "raw-privacy"
    )

    for marker in PRIVATE_MARKERS:
        assert marker not in public_payload
    assert internal_payload["video_provider"] == "internal_renderer"
    assert internal_payload["video_provider_key"] == "internal_renderer_key"
    assert internal_payload["video_render_provider"] == "internal_render_lane"
    assert internal_payload["video_coverage_proof"] == (
        "internal_acceptance_receipt"
    )


def test_public_payload_endpoint_never_merges_private_receipt_without_owner_principal(
    monkeypatch,
) -> None:
    payload = {
        "slug": "public-endpoint-no-private",
        "title": "Public tour",
        "tour_privacy_mode": "anonymous_public",
        "facts": {"rooms": 2},
        "scenes": [],
    }
    monkeypatch.setattr(public_tours, "_load_tour", lambda slug: dict(payload))
    monkeypatch.setattr(
        public_tours,
        "_load_private_tour_receipt",
        lambda slug: (_ for _ in ()).throw(
            AssertionError("public payload endpoint read private receipt")
        ),
    )
    monkeypatch.setattr(
        public_tours,
        "_load_tour_with_private_receipt",
        lambda slug: (_ for _ in ()).throw(
            AssertionError("public payload endpoint merged private receipt")
        ),
    )

    response = public_tours.public_tour_payload("public-endpoint-no-private")
    body = json.loads(bytes(response.body))

    assert body["slug"] == "public-endpoint-no-private"
    assert body["facts"] == {"rooms": 2}
    assert all(marker not in body for marker in PRIVATE_MARKERS)


def test_private_receipt_merges_only_for_exact_owner_principal(
    monkeypatch,
    tmp_path: Path,
) -> None:
    bundle = _write_bundle(monkeypatch, tmp_path)

    public_only = property_tour_hosting._load_hosted_property_tour_payload(  # noqa: SLF001
        bundle,
        principal_id="cf-email:other@example.test",
    )
    owned = property_tour_hosting._load_hosted_property_tour_payload(  # noqa: SLF001
        bundle,
        principal_id="cf-email:owner@example.test",
    )

    assert "principal_id" not in public_only
    assert "listing_url" not in public_only
    assert owned["principal_id"] == "cf-email:owner@example.test"
    assert owned["listing_url"].startswith("https://listing.example.test/")


def test_anonymous_rare_listing_reduces_uniquely_identifying_fact_combination() -> None:
    manifest = build_public_tour_manifest(
        {
            "slug": "rare-listing",
            "tour_privacy_mode": "anonymous_public",
            "public_uniqueness_risk": "rare_listing",
            "facts": {
                "property_type": "villa",
                "rooms": 7,
                "area_sqm": 413,
                "floor": "top floor",
                "district": "Tiny District",
                "municipality": "Smalltown",
                "postal_name": "Smalltown 1010",
                "purchase_price_eur": 3_900_000,
            },
            "scenes": [],
        },
        url_allowed=lambda _value: False,
        bundle_dir_resolver=lambda _slug: None,
    ).as_dict()

    assert manifest["facts"] == {
        "property_type": "villa",
        "rooms": 7,
        "public_fact_precision": "reduced_for_uniqueness",
    }


def test_anonymous_ordinary_urban_listing_retains_coarse_public_facts() -> None:
    manifest = build_public_tour_manifest(
        {
            "slug": "ordinary-listing",
            "tour_privacy_mode": "anonymous_public",
            "facts": {
                "rooms": 3,
                "area_sqm": 84,
                "floor": 2,
                "city": "Vienna",
                "postal_name": "1020 Vienna",
                "purchase_price_eur": 790_000,
                "municipality_population": 2_000_000,
            },
            "scenes": [],
        },
        url_allowed=lambda _value: False,
        bundle_dir_resolver=lambda _slug: None,
    ).as_dict()

    assert manifest["facts"]["area_sqm"] == 84
    assert manifest["facts"]["floor"] == 2
    assert manifest["facts"]["postal_name"] == "1020 Vienna"
    assert manifest["facts"]["purchase_price_eur"] == 790_000
    assert "public_fact_precision" not in manifest["facts"]


def test_unbound_external_walkthrough_url_never_becomes_customer_ready(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))

    assert (
        property_tour_hosting._hosted_property_tour_walkthrough_open_url(  # noqa: SLF001
            "",
            "https://media.example.test/unbound-walkthrough.mp4",
        )
        == ""
    )
