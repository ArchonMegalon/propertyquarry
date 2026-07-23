from __future__ import annotations

import contextlib
import copy
import hashlib
import json
from pathlib import Path

import pytest

from app.product import (
    property_search_storage,
    property_tour_hosting,
    service as product_service,
)
from app.api.routes import landing_property_research as research_routes
from app.api.routes.public_tour_payloads import redacted_public_tour_payload
from app.product.property_research_packet_links import property_research_candidate_ref
from app.product.property_search_tour_binding import (
    PropertySearchTourBindingError,
    exact_property_search_candidate_tour_binding_receipt,
    plan_property_search_candidate_tour_binding,
    property_search_run_record_sha256,
)
from scripts import bind_property_search_candidate_tour as binding_script


PRINCIPAL_ID = "tenant-tour-binding-test"
RUN_ID = "98bed75e984549c6bd4371d602662ab8"
CANDIDATE_REF = "053ad185e1c44b2e"
LISTING_ID = "1807240910"
PROPERTY_URL = (
    "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/"
    "wien-1020-leopoldstadt/naehe-prater-und-messe-wien-i-u1-u2-i-ruhelage-i-"
    "garage-i-maisonette-i-voll-moebliert-i-in-der-vorgartenstrasse-1807240910/"
)
PROPERTY_URL_SHA256 = "f451d904167c5b1a2b27f698ec38c18f6760fe55b79cca32c99bc986f8293d8e"
TOUR_BASE_URL = "https://propertyquarry.com/tours/prater-messe-ai-360-053ad185e1c44b2e"
TOUR_URL = f"{TOUR_BASE_URL}/control"
TOUR_CONTROL_PATH = "/tours/prater-messe-ai-360-053ad185e1c44b2e/control"
OTHER_TOUR_BASE_URL = "https://propertyquarry.com/tours/other-exact-generated-reconstruction"
OTHER_TOUR_URL = f"{OTHER_TOUR_BASE_URL}/control"


def _bundle_identity(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "owner_verified": True,
        "search_run_id": RUN_ID,
        "candidate_ref": CANDIDATE_REF,
        "property_url": PROPERTY_URL,
        "listing_url": PROPERTY_URL,
        "property_url_sha256": PROPERTY_URL_SHA256,
        "source_ref": f"property-scout:{LISTING_ID}",
        "external_id": LISTING_ID,
    }
    payload.update(updates)
    return payload


def _candidate(*, property_url: str | None = None) -> dict[str, object]:
    return {
        "candidate_ref": CANDIDATE_REF,
        "title": "Maisonette near Prater and Messe Wien",
        "property_url": property_url or PROPERTY_URL,
        "review_url": "https://propertyquarry.com/workspace-access/redacted-test-token",
        "listing_id": LISTING_ID,
        "external_id": LISTING_ID,
        "property_facts": {"listing_id": LISTING_ID, "has_360": True},
        "source_ref": f"property-scout:{LISTING_ID}",
        "platform": "willhaben",
        "provider_family": "marketplace",
        "source_label": "Willhaben",
        "tour_status": "blocked",
        "blocked_reason": "listing_360_media_missing",
        "tour_reason_key": "listing_360_media_missing",
        "tour": {
            "status": "blocked",
            "reason": "listing_360_media_missing",
            "reason_key": "listing_360_media_missing",
        },
    }


def _record() -> dict[str, object]:
    candidate = _candidate()
    return {
        "run_id": RUN_ID,
        "principal_id": PRINCIPAL_ID,
        "status": "processed",
        "created_at": "2026-07-04T09:21:58+00:00",
        "updated_at": "2026-07-04T09:22:58+00:00",
        "summary": {
            "ranked_candidates": [copy.deepcopy(candidate)],
            "results": [copy.deepcopy(candidate)],
            "sources": [
                {
                    "source_label": "Willhaben",
                    "top_candidates": [copy.deepcopy(candidate)],
                }
            ],
        },
    }


def _ready_bundle(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reconstruction_kind: str = "ai_panorama_360",
) -> None:
    expected_url = (
        TOUR_BASE_URL if reconstruction_kind == "layout_preview" else TOUR_URL
    )
    monkeypatch.setattr(product_service, "_property_search_run_database_url", lambda: "postgresql://configured")
    monkeypatch.setattr(
        property_tour_hosting,
        "_property_public_tour_base_url",
        lambda: "https://propertyquarry.com/tours",
    )
    monkeypatch.setattr(
        property_tour_hosting,
        "_is_branded_public_tour_url",
        lambda value: str(value) in {TOUR_URL, TOUR_BASE_URL},
    )
    monkeypatch.setattr(
        property_tour_hosting,
        "_hosted_property_tour_reconstruction_kind",
        lambda value, *, principal_id="": (
            reconstruction_kind
            if str(value) == expected_url and str(principal_id) == PRINCIPAL_ID
            else ""
        ),
    )
    monkeypatch.setattr(
        property_tour_hosting,
        "_hosted_property_tour_first_party_open_url",
        lambda value, *, principal_id="": (
            expected_url
            if str(value) == expected_url and str(principal_id) == PRINCIPAL_ID
            else ""
        ),
    )
    monkeypatch.setattr(
        property_tour_hosting,
        "_owned_hosted_property_tour_binding_identity",
        lambda value, *, principal_id="": (
            _bundle_identity()
            if str(value) == expected_url and str(principal_id) == PRINCIPAL_ID
            else {}
        ),
    )


def _bound_record(
    *,
    generated_reconstruction_url: str = TOUR_URL,
    reconstruction_kind: str = "ai_panorama_360",
    disclosure: str = "",
) -> dict[str, object]:
    updated, _receipt = plan_property_search_candidate_tour_binding(
        _record(),
        principal_id=PRINCIPAL_ID,
        run_id=RUN_ID,
        candidate_ref=CANDIDATE_REF,
        expected_listing_id=LISTING_ID,
        generated_reconstruction_url=generated_reconstruction_url,
        bundle_identity=_bundle_identity(),
        reconstruction_kind=reconstruction_kind,
        disclosure=disclosure,
        bound_at="2026-07-20T12:00:00+00:00",
    )
    return updated


def test_binding_plan_updates_every_exact_candidate_occurrence_without_mutating_input() -> None:
    original = _record()
    before = copy.deepcopy(original)
    assert hashlib.sha256(PROPERTY_URL.encode("utf-8")).hexdigest() == PROPERTY_URL_SHA256

    updated, receipt = plan_property_search_candidate_tour_binding(
        original,
        principal_id=PRINCIPAL_ID,
        run_id=RUN_ID,
        candidate_ref=CANDIDATE_REF,
        expected_listing_id=LISTING_ID,
        generated_reconstruction_url=TOUR_URL,
        bundle_identity=_bundle_identity(),
        disclosure="AI-reconstructed from listing photos; not a captured 360 or measured survey.",
        bound_at="2026-07-20T12:00:00+00:00",
    )

    assert original == before
    assert receipt["changed"] is True
    assert receipt["occurrences_matched"] == 3
    assert receipt["occurrences_updated"] == 3
    rows = [
        updated["summary"]["ranked_candidates"][0],
        updated["summary"]["results"][0],
        updated["summary"]["sources"][0]["top_candidates"][0],
    ]
    for row in rows:
        assert row["candidate_ref"] == CANDIDATE_REF
        assert row["generated_reconstruction_url"] == TOUR_URL
        assert row["generated_reconstruction_kind"] == "ai_panorama_360"
        assert row["tour_status"] == "ready"
        assert row["tour_progress_pct"] == 100
        assert row["tour_provider"] == "propertyquarry_ai_360"
        assert "blocked_reason" not in row
        assert "tour_reason_key" not in row
        assert row["tour"]["reconstruction_kind"] == "ai_panorama_360"
        assert "reason" not in row["tour"]


def test_binding_plan_repairs_partial_binding_with_one_consistent_timestamp() -> None:
    partial = _bound_record(disclosure="exact disclosure")
    partial["summary"]["results"][0].pop("generated_reconstruction_url")

    repaired, receipt = plan_property_search_candidate_tour_binding(
        partial,
        principal_id=PRINCIPAL_ID,
        run_id=RUN_ID,
        candidate_ref=CANDIDATE_REF,
        expected_listing_id=LISTING_ID,
        generated_reconstruction_url=TOUR_URL,
        bundle_identity=_bundle_identity(),
        disclosure="exact disclosure",
        bound_at="2026-07-21T12:00:00+00:00",
    )

    rows = [
        repaired["summary"]["ranked_candidates"][0],
        repaired["summary"]["results"][0],
        repaired["summary"]["sources"][0]["top_candidates"][0],
    ]
    assert receipt["changed"] is True
    assert receipt["occurrences_updated"] == 3
    assert {row["tour_status_updated_at"] for row in rows} == {
        "2026-07-21T12:00:00+00:00"
    }
    assert (
        exact_property_search_candidate_tour_binding_receipt(
            repaired,
            principal_id=PRINCIPAL_ID,
            run_id=RUN_ID,
            candidate_ref=CANDIDATE_REF,
            expected_listing_id=LISTING_ID,
            generated_reconstruction_url=TOUR_URL,
            disclosure="exact disclosure",
        )
        is not None
    )


def test_binding_plan_clears_stale_disclosure_for_exact_empty_disclosure() -> None:
    stale = _bound_record(disclosure="stale disclosure")

    repaired, receipt = plan_property_search_candidate_tour_binding(
        stale,
        principal_id=PRINCIPAL_ID,
        run_id=RUN_ID,
        candidate_ref=CANDIDATE_REF,
        expected_listing_id=LISTING_ID,
        generated_reconstruction_url=TOUR_URL,
        bundle_identity=_bundle_identity(),
        disclosure="",
        bound_at="2026-07-21T12:00:00+00:00",
    )

    rows = [
        repaired["summary"]["ranked_candidates"][0],
        repaired["summary"]["results"][0],
        repaired["summary"]["sources"][0]["top_candidates"][0],
    ]
    assert receipt["changed"] is True
    assert receipt["occurrences_updated"] == 3
    for row in rows:
        assert "generated_reconstruction_disclosure" not in row
        assert "disclosure" not in row["tour"]
    assert (
        exact_property_search_candidate_tour_binding_receipt(
            repaired,
            principal_id=PRINCIPAL_ID,
            run_id=RUN_ID,
            candidate_ref=CANDIDATE_REF,
            expected_listing_id=LISTING_ID,
            generated_reconstruction_url=TOUR_URL,
            disclosure="",
        )
        is not None
    )


def test_exact_saved_binding_receipt_rejects_a_nonterminal_run_directly() -> None:
    stored = _bound_record()
    stored["status"] = "running"

    with pytest.raises(
        PropertySearchTourBindingError,
        match="property_search_tour_run_not_terminal",
    ):
        exact_property_search_candidate_tour_binding_receipt(
            stored,
            principal_id=PRINCIPAL_ID,
            run_id=RUN_ID,
            candidate_ref=CANDIDATE_REF,
            expected_listing_id=LISTING_ID,
            generated_reconstruction_url=TOUR_URL,
        )


def test_binding_plan_resolves_and_stabilizes_a_derived_candidate_ref() -> None:
    record = _record()
    ranked = record["summary"]["ranked_candidates"][0]
    results = record["summary"]["results"][0]
    source_row = record["summary"]["sources"][0]["top_candidates"][0]
    ranked.pop("candidate_ref")
    results.pop("candidate_ref")
    source_row.pop("candidate_ref")
    derived_ref = property_research_candidate_ref(ranked)

    updated, receipt = plan_property_search_candidate_tour_binding(
        record,
        principal_id=PRINCIPAL_ID,
        run_id=RUN_ID,
        candidate_ref=derived_ref,
        expected_listing_id=LISTING_ID,
        generated_reconstruction_url=TOUR_URL,
        bundle_identity=_bundle_identity(candidate_ref=""),
        bound_at="2026-07-20T12:00:00+00:00",
    )

    assert receipt["occurrences_matched"] == 3
    assert updated["summary"]["ranked_candidates"][0]["candidate_ref"] == derived_ref
    assert updated["summary"]["results"][0]["candidate_ref"] == derived_ref
    assert updated["summary"]["sources"][0]["top_candidates"][0]["candidate_ref"] == derived_ref


def test_binding_plan_fails_closed_on_listing_or_candidate_url_identity_drift() -> None:
    wrong_listing = _record()
    wrong_listing["summary"]["ranked_candidates"][0]["listing_id"] = "9999999999"
    with pytest.raises(PropertySearchTourBindingError, match="property_search_tour_listing_id_mismatch"):
        plan_property_search_candidate_tour_binding(
            wrong_listing,
            principal_id=PRINCIPAL_ID,
            run_id=RUN_ID,
            candidate_ref=CANDIDATE_REF,
            expected_listing_id=LISTING_ID,
            generated_reconstruction_url=TOUR_URL,
            bundle_identity=_bundle_identity(),
        )

    wrong_external_id = _record()
    wrong_external_id["summary"]["results"][0]["external_id"] = "9999999999"
    with pytest.raises(PropertySearchTourBindingError, match="property_search_tour_listing_id_mismatch"):
        plan_property_search_candidate_tour_binding(
            wrong_external_id,
            principal_id=PRINCIPAL_ID,
            run_id=RUN_ID,
            candidate_ref=CANDIDATE_REF,
            expected_listing_id=LISTING_ID,
            generated_reconstruction_url=TOUR_URL,
            bundle_identity=_bundle_identity(),
        )


@pytest.mark.parametrize(
    ("bundle_updates", "error_code"),
    (
        ({"owner_verified": False}, "property_search_tour_bundle_owner_mismatch"),
        (
            {"candidate_ref": "9e526826d43cdc9a"},
            "property_search_tour_bundle_candidate_ref_mismatch",
        ),
        (
            {"external_id": "974574134"},
            "property_search_tour_bundle_listing_identity_mismatch",
        ),
        (
            {"property_url_sha256": "0" * 64},
            "property_search_tour_bundle_property_url_sha256_mismatch",
        ),
    ),
)
def test_binding_plan_fails_closed_on_owned_bundle_identity_drift(
    bundle_updates: dict[str, object],
    error_code: str,
) -> None:
    with pytest.raises(PropertySearchTourBindingError, match=error_code):
        plan_property_search_candidate_tour_binding(
            _record(),
            principal_id=PRINCIPAL_ID,
            run_id=RUN_ID,
            candidate_ref=CANDIDATE_REF,
            expected_listing_id=LISTING_ID,
            generated_reconstruction_url=TOUR_URL,
            bundle_identity=_bundle_identity(**bundle_updates),
        )


def test_binding_plan_rejects_provider_source_contradictions_and_candidate_ref_drift() -> None:
    contradictory = _record()
    contradictory["summary"]["ranked_candidates"][0]["platform"] = "findmyhome"
    with pytest.raises(
        PropertySearchTourBindingError,
        match="property_search_tour_provider_identity_conflict",
    ):
        plan_property_search_candidate_tour_binding(
            contradictory,
            principal_id=PRINCIPAL_ID,
            run_id=RUN_ID,
            candidate_ref=CANDIDATE_REF,
            expected_listing_id=LISTING_ID,
            generated_reconstruction_url=TOUR_URL,
            bundle_identity=_bundle_identity(),
        )

    drifted = _record()
    duplicate = _candidate()
    duplicate["candidate_ref"] = "different-ref-for-same-property"
    drifted["summary"]["results"] = [duplicate]
    with pytest.raises(
        PropertySearchTourBindingError,
        match="property_search_tour_candidate_ref_identity_conflict",
    ):
        plan_property_search_candidate_tour_binding(
            drifted,
            principal_id=PRINCIPAL_ID,
            run_id=RUN_ID,
            candidate_ref=CANDIDATE_REF,
            expected_listing_id=LISTING_ID,
            generated_reconstruction_url=TOUR_URL,
            bundle_identity=_bundle_identity(),
        )

    conflicting_url = _record()
    conflicting_url["summary"]["sources"][0]["top_candidates"][0]["property_url"] = (
        "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/another-property-1807240910/"
    )
    with pytest.raises(PropertySearchTourBindingError, match="property_search_tour_candidate_url_mismatch"):
        plan_property_search_candidate_tour_binding(
            conflicting_url,
            principal_id=PRINCIPAL_ID,
            run_id=RUN_ID,
            candidate_ref=CANDIDATE_REF,
            expected_listing_id=LISTING_ID,
            generated_reconstruction_url=TOUR_URL,
            bundle_identity=_bundle_identity(),
        )


def test_service_binding_defaults_to_dry_run_and_redacts_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_bundle(monkeypatch)
    stored = _record()
    monkeypatch.setattr(
        product_service,
        "_load_property_search_run_record",
        lambda *, run_id, principal_id: copy.deepcopy(stored),
    )
    monkeypatch.setattr(
        product_service,
        "_compare_and_swap_property_search_run_record",
        lambda **_kwargs: pytest.fail("dry-run must not write"),
    )

    receipt = product_service.bind_property_search_candidate_generated_reconstruction(
        principal_id=PRINCIPAL_ID,
        run_id=RUN_ID,
        candidate_ref=CANDIDATE_REF,
        expected_listing_id=LISTING_ID,
        generated_reconstruction_url=TOUR_URL,
    )

    assert receipt["mode"] == "dry_run"
    assert receipt["status"] == "change_required"
    assert receipt["before_sha256"]
    assert PRINCIPAL_ID not in json.dumps(receipt)


def test_service_binding_normalizes_base_url_to_strict_control_url_and_requires_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_bundle(monkeypatch)
    stored = _record()
    monkeypatch.setattr(
        product_service,
        "_load_property_search_run_record",
        lambda *, run_id, principal_id: copy.deepcopy(stored),
    )

    receipt = product_service.bind_property_search_candidate_generated_reconstruction(
        principal_id=PRINCIPAL_ID,
        run_id=RUN_ID,
        candidate_ref=CANDIDATE_REF,
        expected_listing_id=LISTING_ID,
        generated_reconstruction_url=TOUR_BASE_URL,
    )
    assert receipt["generated_reconstruction_url"] == TOUR_URL

    monkeypatch.setattr(
        property_tour_hosting,
        "_owned_hosted_property_tour_binding_identity",
        lambda *_args, **_kwargs: {},
    )
    with pytest.raises(
        PropertySearchTourBindingError,
        match="property_search_tour_bundle_owner_mismatch",
    ):
        product_service.bind_property_search_candidate_generated_reconstruction(
            principal_id=PRINCIPAL_ID,
            run_id=RUN_ID,
            candidate_ref=CANDIDATE_REF,
            expected_listing_id=LISTING_ID,
            generated_reconstruction_url=TOUR_URL,
        )


def test_lightweight_research_candidate_retains_canonical_ai_360_control_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updated, _receipt = plan_property_search_candidate_tour_binding(
        _record(),
        principal_id=PRINCIPAL_ID,
        run_id=RUN_ID,
        candidate_ref=CANDIDATE_REF,
        expected_listing_id=LISTING_ID,
        generated_reconstruction_url=TOUR_URL,
        bundle_identity=_bundle_identity(),
        disclosure="AI-reconstructed from listing photos; not a captured 360 or measured survey.",
        bound_at="2026-07-20T12:00:00+00:00",
    )
    compact = property_search_storage._compact_property_search_run_record(updated)
    candidate = compact["summary"]["ranked_candidates"][0]

    assert candidate["candidate_ref"] == CANDIDATE_REF
    assert candidate["generated_reconstruction_url"] == TOUR_URL
    assert candidate["generated_reconstruction_kind"] == "ai_panorama_360"
    assert "not a captured 360" in candidate["generated_reconstruction_disclosure"]

    monkeypatch.setattr(
        property_tour_hosting,
        "_hosted_property_tour_reconstruction_kind",
        lambda value, *, principal_id="": (
            "ai_panorama_360"
            if str(value) == TOUR_URL and str(principal_id) == PRINCIPAL_ID
            else ""
        ),
    )
    monkeypatch.setattr(
        research_routes,
        "_property_tour_first_party_open_url",
        lambda value, *, principal_id="": (
            TOUR_URL
            if str(value) == TOUR_URL and str(principal_id) == PRINCIPAL_ID
            else ""
        ),
    )
    monkeypatch.setattr(
        research_routes,
        "_property_hosted_tour_disabled_fallback",
        lambda _value: False,
    )

    media = research_routes._property_tour_media_payload(
        candidate,
        principal_id=PRINCIPAL_ID,
    )

    assert media["generated_reconstruction_kind"] == "ai_panorama_360"
    assert media["ai_360_ready"] is True
    assert media["generated_reconstruction_href"] == TOUR_CONTROL_PATH
    assert media["embed_href"] == TOUR_CONTROL_PATH
    assert media["primary_href"] == TOUR_CONTROL_PATH


def test_owned_bundle_identity_never_falls_back_to_public_manifest_for_wrong_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    slug = "prater-messe-ai-360-053ad185e1c44b2e"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir()
    (bundle_dir / "tour.json").write_text(
        json.dumps({"property_url_sha256": _bundle_identity()["property_url_sha256"]}),
        encoding="utf-8",
    )
    (bundle_dir / "tour.private.json").write_text(
        json.dumps(
            {
                "principal_id": "different-tenant",
                "search_run_id": RUN_ID,
                "candidate_ref": CANDIDATE_REF,
                "listing_url": PROPERTY_URL,
                "property_url": PROPERTY_URL,
                "source_ref": f"property-scout:{LISTING_ID}",
                "external_id": LISTING_ID,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(property_tour_hosting, "_public_tour_dir", lambda: tmp_path)

    assert (
        property_tour_hosting._owned_hosted_property_tour_binding_identity(
            TOUR_URL,
            principal_id=PRINCIPAL_ID,
        )
        == {}
    )
    owned = property_tour_hosting._owned_hosted_property_tour_binding_identity(
        TOUR_URL,
        principal_id="different-tenant",
    )
    assert owned == {
        "owner_verified": True,
        "slug": slug,
        "search_run_id": RUN_ID,
        "candidate_ref": CANDIDATE_REF,
        "listing_url": PROPERTY_URL,
        "property_url": PROPERTY_URL,
        "source_ref": f"property-scout:{LISTING_ID}",
        "external_id": LISTING_ID,
        "property_url_sha256": _bundle_identity()["property_url_sha256"],
    }


def test_owned_bundle_identity_derives_legacy_hash_only_from_matching_private_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    slug = "prater-messe-ai-360-053ad185e1c44b2e"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir()
    public_manifest_path = bundle_dir / "tour.json"
    private_manifest_path = bundle_dir / "tour.private.json"
    public_manifest_path.write_text(
        json.dumps({"slug": slug}),
        encoding="utf-8",
    )
    private_payload = {
        "principal_id": PRINCIPAL_ID,
        "search_run_id": RUN_ID,
        "candidate_ref": CANDIDATE_REF,
        "listing_url": PROPERTY_URL,
        "property_url": PROPERTY_URL,
        "source_ref": f"property-scout:{LISTING_ID}",
        "external_id": LISTING_ID,
    }
    private_manifest_path.write_text(
        json.dumps(private_payload),
        encoding="utf-8",
    )
    monkeypatch.setattr(property_tour_hosting, "_public_tour_dir", lambda: tmp_path)

    owned = property_tour_hosting._owned_hosted_property_tour_binding_identity(
        TOUR_URL,
        principal_id=PRINCIPAL_ID,
    )
    assert owned["owner_verified"] is True
    assert owned["property_url_sha256"] == PROPERTY_URL_SHA256

    public_manifest_path.write_text(
        json.dumps({"slug": slug, "property_url_sha256": "0" * 64}),
        encoding="utf-8",
    )
    assert (
        property_tour_hosting._owned_hosted_property_tour_binding_identity(
            TOUR_URL,
            principal_id=PRINCIPAL_ID,
        )
        == {}
    )

    public_manifest_path.write_text(
        json.dumps({"slug": slug}),
        encoding="utf-8",
    )
    private_payload["listing_url"] = "https://findmyhome.at/conflicting-listing"
    private_manifest_path.write_text(
        json.dumps(private_payload),
        encoding="utf-8",
    )
    assert (
        property_tour_hosting._owned_hosted_property_tour_binding_identity(
            TOUR_URL,
            principal_id=PRINCIPAL_ID,
        )
        == {}
    )


def test_public_manifest_exposes_only_a_valid_property_url_digest() -> None:
    def _redact(value: object) -> dict[str, object]:
        return redacted_public_tour_payload(
            {
                "slug": "safe-tour",
                "property_url_sha256": value,
            },
            url_allowed=lambda _value: False,
            bundle_dir_resolver=lambda _slug: None,
        )

    assert _redact(PROPERTY_URL_SHA256)["property_url_sha256"] == PROPERTY_URL_SHA256
    assert "property_url_sha256" not in _redact(PROPERTY_URL)


def test_service_binding_applies_with_fresh_fingerprint_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_bundle(monkeypatch)
    state = {"record": _record()}
    writes: list[dict[str, object]] = []
    monkeypatch.setattr(product_service, "_PROPERTY_SEARCH_RUN_REGISTRY", {})
    monkeypatch.setattr(
        product_service,
        "_load_property_search_run_record",
        lambda *, run_id, principal_id: copy.deepcopy(state["record"]),
    )
    monkeypatch.setattr(
        product_service,
        "_load_property_search_run_record_storage",
        lambda *, run_id, principal_id: copy.deepcopy(state["record"]),
    )

    def _cas(**kwargs: object) -> dict[str, object]:
        assert kwargs["principal_id"] == PRINCIPAL_ID
        assert kwargs["run_id"] == RUN_ID
        assert kwargs["expected_record_sha256"] == property_search_run_record_sha256(
            state["record"]
        )
        record = copy.deepcopy(kwargs["updated_record"])
        writes.append(record)
        state["record"] = copy.deepcopy(record)
        return {
            "status": "applied",
            "record": copy.deepcopy(record),
            "record_sha256": property_search_run_record_sha256(record),
        }

    monkeypatch.setattr(product_service, "_compare_and_swap_property_search_run_record", _cas)
    dry_run = product_service.bind_property_search_candidate_generated_reconstruction(
        principal_id=PRINCIPAL_ID,
        run_id=RUN_ID,
        candidate_ref=CANDIDATE_REF,
        expected_listing_id=LISTING_ID,
        generated_reconstruction_url=TOUR_URL,
    )
    applied = product_service.bind_property_search_candidate_generated_reconstruction(
        principal_id=PRINCIPAL_ID,
        run_id=RUN_ID,
        candidate_ref=CANDIDATE_REF,
        expected_listing_id=LISTING_ID,
        generated_reconstruction_url=TOUR_URL,
        expected_record_sha256=dry_run["before_sha256"],
        apply=True,
    )
    repeated = product_service.bind_property_search_candidate_generated_reconstruction(
        principal_id=PRINCIPAL_ID,
        run_id=RUN_ID,
        candidate_ref=CANDIDATE_REF,
        expected_listing_id=LISTING_ID,
        generated_reconstruction_url=TOUR_URL,
        expected_record_sha256=dry_run["before_sha256"],
        apply=True,
    )

    assert applied["status"] == "applied"
    assert applied["persisted_sha256"]
    assert repeated["status"] == "already_bound"
    assert len(writes) == 1


@pytest.mark.parametrize(
    ("reconstruction_kind", "stored_url", "requested_url"),
    (
        ("layout_preview", TOUR_BASE_URL, TOUR_BASE_URL),
        ("ai_panorama_360", TOUR_URL, TOUR_BASE_URL),
    ),
)
@pytest.mark.parametrize("apply", (False, True))
def test_service_exact_binding_survives_public_only_publish_without_private_receipt(
    monkeypatch: pytest.MonkeyPatch,
    reconstruction_kind: str,
    stored_url: str,
    requested_url: str,
    apply: bool,
) -> None:
    _ready_bundle(monkeypatch, reconstruction_kind=reconstruction_kind)
    stored = _bound_record(
        generated_reconstruction_url=stored_url,
        reconstruction_kind=reconstruction_kind,
    )
    owner_calls: list[str] = []
    monkeypatch.setattr(
        product_service,
        "_load_property_search_run_record",
        lambda *, run_id, principal_id: copy.deepcopy(stored),
    )
    monkeypatch.setattr(
        property_tour_hosting,
        "_owned_hosted_property_tour_binding_identity",
        lambda value, *, principal_id="": owner_calls.append(str(value))
        or pytest.fail("an exact persisted binding must not need tour.private.json"),
    )
    monkeypatch.setattr(
        product_service,
        "_compare_and_swap_property_search_run_record",
        lambda **_kwargs: pytest.fail("an exact persisted binding must not CAS"),
    )

    receipt = product_service.bind_property_search_candidate_generated_reconstruction(
        principal_id=PRINCIPAL_ID,
        run_id=RUN_ID,
        candidate_ref=CANDIDATE_REF,
        expected_listing_id=LISTING_ID,
        generated_reconstruction_url=requested_url,
        reconstruction_kind=reconstruction_kind,
        apply=apply,
    )

    assert receipt["status"] == "already_bound"
    assert receipt["mode"] == ("apply" if apply else "dry_run")
    assert receipt["changed"] is False
    assert receipt["occurrences_matched"] == 3
    assert receipt["occurrences_updated"] == 0
    assert receipt["binding_verified_from"] == "principal_scoped_terminal_run"
    assert owner_calls == []


def test_service_exact_binding_still_requires_a_terminal_principal_scoped_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_bundle(monkeypatch)
    stored = _bound_record()
    stored["status"] = "running"
    monkeypatch.setattr(
        product_service,
        "_load_property_search_run_record",
        lambda *, run_id, principal_id: copy.deepcopy(stored),
    )
    monkeypatch.setattr(
        property_tour_hosting,
        "_hosted_property_tour_reconstruction_kind",
        lambda *_args, **_kwargs: pytest.fail(
            "a nonterminal run must fail before hosted-bundle inspection"
        ),
    )
    monkeypatch.setattr(
        property_tour_hosting,
        "_owned_hosted_property_tour_binding_identity",
        lambda *_args, **_kwargs: pytest.fail(
            "a nonterminal run must fail before owner lookup"
        ),
    )

    with pytest.raises(
        PropertySearchTourBindingError,
        match="property_search_tour_run_not_terminal",
    ):
        product_service.bind_property_search_candidate_generated_reconstruction(
            principal_id=PRINCIPAL_ID,
            run_id=RUN_ID,
            candidate_ref=CANDIDATE_REF,
            expected_listing_id=LISTING_ID,
            generated_reconstruction_url=TOUR_URL,
        )


@pytest.mark.parametrize(
    ("public_failure", "error_code"),
    (
        ("kind", "property_search_tour_bundle_kind_mismatch"),
        ("not_ready", "property_search_tour_bundle_not_ready"),
    ),
)
def test_service_exact_binding_does_not_bypass_public_bundle_verification(
    monkeypatch: pytest.MonkeyPatch,
    public_failure: str,
    error_code: str,
) -> None:
    _ready_bundle(monkeypatch)
    stored = _bound_record()
    monkeypatch.setattr(
        product_service,
        "_load_property_search_run_record",
        lambda *, run_id, principal_id: copy.deepcopy(stored),
    )
    if public_failure == "kind":
        monkeypatch.setattr(
            property_tour_hosting,
            "_hosted_property_tour_reconstruction_kind",
            lambda *_args, **_kwargs: "layout_preview",
        )
    else:
        monkeypatch.setattr(
            property_tour_hosting,
            "_hosted_property_tour_first_party_open_url",
            lambda *_args, **_kwargs: "",
        )
    monkeypatch.setattr(
        property_tour_hosting,
        "_owned_hosted_property_tour_binding_identity",
        lambda *_args, **_kwargs: pytest.fail(
            "public verification failure must precede owner lookup"
        ),
    )

    with pytest.raises(PropertySearchTourBindingError, match=error_code):
        product_service.bind_property_search_candidate_generated_reconstruction(
            principal_id=PRINCIPAL_ID,
            run_id=RUN_ID,
            candidate_ref=CANDIDATE_REF,
            expected_listing_id=LISTING_ID,
            generated_reconstruction_url=TOUR_URL,
        )


def test_service_first_binding_still_requires_live_private_owner_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_bundle(monkeypatch)
    monkeypatch.setattr(
        product_service,
        "_load_property_search_run_record",
        lambda *, run_id, principal_id: _record(),
    )
    monkeypatch.setattr(
        property_tour_hosting,
        "_owned_hosted_property_tour_binding_identity",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        product_service,
        "_compare_and_swap_property_search_run_record",
        lambda **_kwargs: pytest.fail("a missing owner receipt must not CAS"),
    )

    with pytest.raises(
        PropertySearchTourBindingError,
        match="property_search_tour_bundle_owner_mismatch",
    ):
        product_service.bind_property_search_candidate_generated_reconstruction(
            principal_id=PRINCIPAL_ID,
            run_id=RUN_ID,
            candidate_ref=CANDIDATE_REF,
            expected_listing_id=LISTING_ID,
            generated_reconstruction_url=TOUR_URL,
        )


@pytest.mark.parametrize(
    ("drift", "error_code", "owner_lookup_required"),
    (
        ("principal", "property_search_tour_principal_mismatch", False),
        ("run", "property_search_tour_run_id_mismatch", False),
        ("candidate", "property_search_tour_candidate_not_found", False),
        ("listing", "property_search_tour_listing_id_mismatch", False),
        ("url", "property_search_tour_bundle_owner_mismatch", True),
        ("kind", "property_search_tour_bundle_owner_mismatch", True),
        ("disclosure", "property_search_tour_bundle_owner_mismatch", True),
    ),
)
def test_service_missing_private_receipt_does_not_accept_nonexact_existing_binding(
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
    error_code: str,
    owner_lookup_required: bool,
) -> None:
    _ready_bundle(monkeypatch)
    stored = _bound_record(disclosure="exact disclosure")
    owner_calls: list[str] = []
    monkeypatch.setattr(
        product_service,
        "_load_property_search_run_record",
        lambda *, run_id, principal_id: copy.deepcopy(stored),
    )
    monkeypatch.setattr(
        property_tour_hosting,
        "_owned_hosted_property_tour_binding_identity",
        lambda value, **_kwargs: owner_calls.append(str(value)) or {},
    )
    monkeypatch.setattr(
        product_service,
        "_compare_and_swap_property_search_run_record",
        lambda **_kwargs: pytest.fail("identity drift must not CAS"),
    )
    kwargs: dict[str, object] = {
        "principal_id": PRINCIPAL_ID,
        "run_id": RUN_ID,
        "candidate_ref": CANDIDATE_REF,
        "expected_listing_id": LISTING_ID,
        "generated_reconstruction_url": TOUR_URL,
        "reconstruction_kind": "ai_panorama_360",
        "disclosure": "exact disclosure",
    }
    if drift == "principal":
        kwargs["principal_id"] = "different-principal"
    elif drift == "run":
        kwargs["run_id"] = "different-run"
    elif drift == "candidate":
        kwargs["candidate_ref"] = "different-candidate"
    elif drift == "listing":
        kwargs["expected_listing_id"] = "974574134"
    elif drift == "url":
        kwargs["generated_reconstruction_url"] = OTHER_TOUR_URL
        monkeypatch.setattr(
            property_tour_hosting,
            "_is_branded_public_tour_url",
            lambda value: str(value) == OTHER_TOUR_URL,
        )
        monkeypatch.setattr(
            property_tour_hosting,
            "_hosted_property_tour_reconstruction_kind",
            lambda value, *, principal_id="": (
                "ai_panorama_360"
                if str(value) == OTHER_TOUR_URL and str(principal_id) == PRINCIPAL_ID
                else ""
            ),
        )
        monkeypatch.setattr(
            property_tour_hosting,
            "_hosted_property_tour_first_party_open_url",
            lambda value, *, principal_id="": (
                OTHER_TOUR_URL
                if str(value) == OTHER_TOUR_URL and str(principal_id) == PRINCIPAL_ID
                else ""
            ),
        )
    elif drift == "kind":
        kwargs["generated_reconstruction_url"] = TOUR_BASE_URL
        kwargs["reconstruction_kind"] = "layout_preview"
        monkeypatch.setattr(
            property_tour_hosting,
            "_hosted_property_tour_reconstruction_kind",
            lambda value, *, principal_id="": (
                "layout_preview"
                if str(value) == TOUR_BASE_URL and str(principal_id) == PRINCIPAL_ID
                else ""
            ),
        )
        monkeypatch.setattr(
            property_tour_hosting,
            "_hosted_property_tour_first_party_open_url",
            lambda value, *, principal_id="": (
                TOUR_BASE_URL
                if str(value) == TOUR_BASE_URL and str(principal_id) == PRINCIPAL_ID
                else ""
            ),
        )
    elif drift == "disclosure":
        kwargs["disclosure"] = "different disclosure"

    with pytest.raises(PropertySearchTourBindingError, match=error_code):
        product_service.bind_property_search_candidate_generated_reconstruction(
            **kwargs,
        )
    assert bool(owner_calls) is owner_lookup_required


@pytest.mark.parametrize("drift", ("partial", "mixed"))
def test_service_missing_private_receipt_rejects_partial_or_mixed_occurrence_binding(
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    _ready_bundle(monkeypatch)
    stored = _bound_record()
    if drift == "partial":
        stored["summary"]["results"][0].pop("generated_reconstruction_url")
    else:
        stored["summary"]["sources"][0]["top_candidates"][0][
            "generated_reconstruction_url"
        ] = OTHER_TOUR_URL
    monkeypatch.setattr(
        product_service,
        "_load_property_search_run_record",
        lambda *, run_id, principal_id: copy.deepcopy(stored),
    )
    monkeypatch.setattr(
        property_tour_hosting,
        "_owned_hosted_property_tour_binding_identity",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        product_service,
        "_compare_and_swap_property_search_run_record",
        lambda **_kwargs: pytest.fail("partial or mixed binding must not CAS"),
    )

    with pytest.raises(
        PropertySearchTourBindingError,
        match="property_search_tour_bundle_owner_mismatch",
    ):
        product_service.bind_property_search_candidate_generated_reconstruction(
            principal_id=PRINCIPAL_ID,
            run_id=RUN_ID,
            candidate_ref=CANDIDATE_REF,
            expected_listing_id=LISTING_ID,
            generated_reconstruction_url=TOUR_URL,
        )


def test_service_binding_rechecks_locked_race_and_accepts_only_same_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_bundle(monkeypatch)
    initial = _record()
    state = {"record": copy.deepcopy(initial)}
    monkeypatch.setattr(
        product_service,
        "_load_property_search_run_record",
        lambda *, run_id, principal_id: copy.deepcopy(initial),
    )
    monkeypatch.setattr(
        product_service,
        "_load_property_search_run_record_storage",
        lambda *, run_id, principal_id: copy.deepcopy(state["record"]),
    )
    dry_run = product_service.bind_property_search_candidate_generated_reconstruction(
        principal_id=PRINCIPAL_ID,
        run_id=RUN_ID,
        candidate_ref=CANDIDATE_REF,
        expected_listing_id=LISTING_ID,
        generated_reconstruction_url=TOUR_URL,
    )
    monkeypatch.setattr(
        product_service,
        "_compare_and_swap_property_search_run_record",
        lambda **_kwargs: {"status": "record_changed", "record_sha256": "f" * 64},
    )
    state["record"]["summary"]["concurrent_note"] = "must not be overwritten"
    with pytest.raises(
        PropertySearchTourBindingError,
        match="property_search_tour_record_changed_since_dry_run",
    ):
        product_service.bind_property_search_candidate_generated_reconstruction(
            principal_id=PRINCIPAL_ID,
            run_id=RUN_ID,
            candidate_ref=CANDIDATE_REF,
            expected_listing_id=LISTING_ID,
            generated_reconstruction_url=TOUR_URL,
            expected_record_sha256=dry_run["before_sha256"],
            apply=True,
        )

    state["record"], _receipt = plan_property_search_candidate_tour_binding(
        state["record"],
        principal_id=PRINCIPAL_ID,
        run_id=RUN_ID,
        candidate_ref=CANDIDATE_REF,
        expected_listing_id=LISTING_ID,
        generated_reconstruction_url=TOUR_URL,
        bundle_identity=_bundle_identity(),
    )
    state["record"]["status"] = "running"
    with pytest.raises(
        PropertySearchTourBindingError,
        match="property_search_tour_record_changed_since_dry_run",
    ):
        product_service.bind_property_search_candidate_generated_reconstruction(
            principal_id=PRINCIPAL_ID,
            run_id=RUN_ID,
            candidate_ref=CANDIDATE_REF,
            expected_listing_id=LISTING_ID,
            generated_reconstruction_url=TOUR_URL,
            expected_record_sha256=dry_run["before_sha256"],
            apply=True,
        )
    state["record"]["status"] = "processed"
    result = product_service.bind_property_search_candidate_generated_reconstruction(
        principal_id=PRINCIPAL_ID,
        run_id=RUN_ID,
        candidate_ref=CANDIDATE_REF,
        expected_listing_id=LISTING_ID,
        generated_reconstruction_url=TOUR_URL,
        expected_record_sha256=dry_run["before_sha256"],
        apply=True,
    )
    assert result["status"] == "already_bound"
    assert result["mode"] == "apply"
    assert result["changed"] is False
    assert result["occurrences_updated"] == 0
    assert result["changed_paths"] == []
    assert result["binding_verified_from"] == "principal_scoped_terminal_run"
    assert state["record"]["summary"]["concurrent_note"] == "must not be overwritten"


def test_service_binding_rejects_blind_apply_and_noncanonical_tour_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ready_bundle(monkeypatch)
    stored = _record()
    monkeypatch.setattr(
        product_service,
        "_load_property_search_run_record",
        lambda *, run_id, principal_id: copy.deepcopy(stored),
    )
    monkeypatch.setattr(
        product_service,
        "_compare_and_swap_property_search_run_record",
        lambda **_kwargs: pytest.fail("guarded apply must not write"),
    )
    with pytest.raises(
        PropertySearchTourBindingError,
        match="property_search_tour_expected_record_sha256_required",
    ):
        product_service.bind_property_search_candidate_generated_reconstruction(
            principal_id=PRINCIPAL_ID,
            run_id=RUN_ID,
            candidate_ref=CANDIDATE_REF,
            expected_listing_id=LISTING_ID,
            generated_reconstruction_url=TOUR_URL,
            apply=True,
        )
    with pytest.raises(
        PropertySearchTourBindingError,
        match="property_search_tour_url_not_first_party_base",
    ):
        product_service.bind_property_search_candidate_generated_reconstruction(
            principal_id=PRINCIPAL_ID,
            run_id=RUN_ID,
            candidate_ref=CANDIDATE_REF,
            expected_listing_id=LISTING_ID,
            generated_reconstruction_url=f"{TOUR_URL}/control",
        )

    monkeypatch.setattr(
        property_tour_hosting,
        "_property_public_tour_base_url",
        lambda: "https://embedded-user@propertyquarry.com/tours",
    )
    with pytest.raises(
        PropertySearchTourBindingError,
        match="property_search_tour_url_not_first_party_base",
    ):
        product_service.bind_property_search_candidate_generated_reconstruction(
            principal_id=PRINCIPAL_ID,
            run_id=RUN_ID,
            candidate_ref=CANDIDATE_REF,
            expected_listing_id=LISTING_ID,
            generated_reconstruction_url=TOUR_URL,
        )


def test_storage_compare_and_swap_locks_before_write_and_rejects_stale_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locked = {"record": _record()}
    updated, _receipt = plan_property_search_candidate_tour_binding(
        locked["record"],
        principal_id=PRINCIPAL_ID,
        run_id=RUN_ID,
        candidate_ref=CANDIDATE_REF,
        expected_listing_id=LISTING_ID,
        generated_reconstruction_url=TOUR_URL,
        bundle_identity=_bundle_identity(),
        bound_at="2026-07-20T12:00:00+00:00",
    )
    events: list[str] = []

    class _Cursor:
        def __init__(self) -> None:
            self.result: tuple[object, ...] | None = None

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, query: str, _params: object = None) -> None:
            normalized = " ".join(str(query).split()).upper()
            if normalized.startswith("SELECT PAYLOAD_JSON") and "FOR UPDATE" in normalized:
                events.append("select_for_update")
                self.result = (copy.deepcopy(locked["record"]),)
                return
            if normalized.startswith("UPDATE PROPERTY_SEARCH_RUNS"):
                events.append("update")
                self.result = (copy.deepcopy(updated),)
                return
            raise AssertionError(normalized)

        def fetchone(self) -> tuple[object, ...] | None:
            return self.result

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def cursor(self) -> _Cursor:
            return _Cursor()

    @contextlib.contextmanager
    def _transaction(_connection: object):
        events.append("transaction_enter")
        try:
            yield
        finally:
            events.append("transaction_exit")

    monkeypatch.setattr(property_search_storage, "_property_search_run_database_url", lambda: "postgresql://configured")
    monkeypatch.setattr(property_search_storage, "_require_property_search_run_schema", lambda: None)
    monkeypatch.setattr(property_search_storage, "_property_search_run_connect", lambda: _Connection())
    monkeypatch.setattr(property_search_storage, "_property_search_run_transaction", _transaction)
    monkeypatch.setattr(
        property_search_storage,
        "_property_search_run_canonicalize_record",
        lambda record: copy.deepcopy(record),
    )
    monkeypatch.setattr(
        property_search_storage,
        "_set_property_search_writer_contract",
        lambda _cursor: events.append("writer_contract"),
    )
    monkeypatch.setattr(
        property_search_storage,
        "_compact_property_search_run_record",
        lambda record: {"status": record.get("status"), "delivery_pending": False},
    )
    monkeypatch.setattr(
        property_search_storage,
        "project_property_research_packet_links",
        lambda _record: (),
    )
    monkeypatch.setattr(
        property_search_storage,
        "upsert_property_research_packet_links",
        lambda _cursor, _links: events.append("packet_links"),
    )
    monkeypatch.setattr(
        property_search_storage,
        "sync_property_research_packet_run_memberships",
        lambda _cursor, **_kwargs: events.append("memberships"),
    )

    before_sha256 = property_search_run_record_sha256(locked["record"])
    result = property_search_storage._compare_and_swap_property_search_run_record(
        principal_id=PRINCIPAL_ID,
        run_id=RUN_ID,
        expected_record_sha256=before_sha256,
        updated_record=updated,
    )
    assert result["status"] == "applied"
    assert events == [
        "transaction_enter",
        "writer_contract",
        "select_for_update",
        "update",
        "packet_links",
        "memberships",
        "transaction_exit",
    ]

    events.clear()
    locked["record"]["summary"]["concurrent_note"] = "newer revision"
    stale = property_search_storage._compare_and_swap_property_search_run_record(
        principal_id=PRINCIPAL_ID,
        run_id=RUN_ID,
        expected_record_sha256=before_sha256,
        updated_record=updated,
    )
    assert stale["status"] == "record_changed"
    assert "update" not in events
    assert "packet_links" not in events


def test_operator_cli_is_dry_run_by_default_and_does_not_echo_principal(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observed: dict[str, object] = {}

    def _bind(**kwargs: object) -> dict[str, object]:
        observed.update(kwargs)
        return {
            "contract": "property_search_candidate_tour_binding_v1",
            "mode": "dry_run",
            "status": "change_required",
            "before_sha256": "a" * 64,
        }

    monkeypatch.setattr(binding_script, "bind_property_search_candidate_generated_reconstruction", _bind)
    exit_code = binding_script.main(
        [
            "--principal-id",
            PRINCIPAL_ID,
            "--run-id",
            RUN_ID,
            "--candidate-ref",
            CANDIDATE_REF,
            "--listing-id",
            LISTING_ID,
            "--tour-url",
            TOUR_URL,
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert observed["apply"] is False
    assert observed["principal_id"] == PRINCIPAL_ID
    assert PRINCIPAL_ID not in captured.out


def _write_private_install_request(path: Path, *, mode: int = 0o600) -> None:
    path.write_text(
        json.dumps(
            {
                "contract": "propertyquarry.ai_panorama_sealed_install_request.v1",
                "source_bundle": str(path.parent / "sealed-bundle"),
                "public_tour_dir": str(path.parent / "public-tours"),
                "expected_slug": "prater-messe-ai-360-053ad185e1c44b2e",
                "expected_source_tree_sha256": "1" * 64,
                "expected_tour_sha256": "2" * 64,
                "principal_id": PRINCIPAL_ID,
                "search_run_id": RUN_ID,
                "candidate_ref": CANDIDATE_REF,
                "listing_url": PROPERTY_URL,
                "provider_key": "willhaben",
                "source_ref": f"property-scout:{LISTING_ID}",
                "external_id": LISTING_ID,
            }
        ),
        encoding="utf-8",
    )
    path.chmod(mode)


def test_operator_cli_reads_sealed_request_and_derives_canonical_control_url(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    request_path = tmp_path / "install-request.json"
    _write_private_install_request(request_path)
    observed: dict[str, object] = {}
    monkeypatch.delenv("PROPERTYQUARRY_TOUR_BINDING_PRINCIPAL_ID", raising=False)
    monkeypatch.setattr(
        binding_script,
        "_property_public_tour_base_url",
        lambda: "https://propertyquarry.com/tours",
    )

    def _bind(**kwargs: object) -> dict[str, object]:
        observed.update(kwargs)
        return {
            "contract": "property_search_candidate_tour_binding_v1",
            "mode": "dry_run",
            "status": "change_required",
            "before_sha256": "a" * 64,
        }

    monkeypatch.setattr(
        binding_script,
        "bind_property_search_candidate_generated_reconstruction",
        _bind,
    )
    exit_code = binding_script.main(["--request-file", str(request_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert observed["principal_id"] == PRINCIPAL_ID
    assert observed["run_id"] == RUN_ID
    assert observed["candidate_ref"] == CANDIDATE_REF
    assert observed["expected_listing_id"] == LISTING_ID
    assert observed["generated_reconstruction_url"] == TOUR_URL
    assert observed["apply"] is False
    assert PRINCIPAL_ID not in captured.out
    assert PROPERTY_URL not in captured.out


def test_operator_cli_request_identity_and_permissions_fail_closed_without_pii(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    request_path = tmp_path / "install-request.json"
    _write_private_install_request(request_path)
    monkeypatch.delenv("PROPERTYQUARRY_TOUR_BINDING_PRINCIPAL_ID", raising=False)
    monkeypatch.setattr(
        binding_script,
        "bind_property_search_candidate_generated_reconstruction",
        lambda **_kwargs: pytest.fail("invalid request must not bind"),
    )

    mismatch_exit = binding_script.main(
        [
            "--request-file",
            str(request_path),
            "--candidate-ref",
            "different-candidate",
        ]
    )
    mismatch_output = capsys.readouterr()
    assert mismatch_exit == 1
    assert mismatch_output.err.strip() == "error:property_search_tour_request_identity_mismatch"
    assert PRINCIPAL_ID not in mismatch_output.err
    assert CANDIDATE_REF not in mismatch_output.err

    request_path.chmod(0o644)
    permissions_exit = binding_script.main(["--request-file", str(request_path)])
    permissions_output = capsys.readouterr()
    assert permissions_exit == 1
    assert permissions_output.err.strip() == "error:ai_panorama_request_permissions_invalid"
    assert PRINCIPAL_ID not in permissions_output.err
