from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_whole_project_scope_tracks_gold_blocker_extensions() -> None:
    scope = (ROOT / "docs/PROPERTYQUARRY_WHOLE_PROJECT_SCOPE.md").read_text(encoding="utf-8")
    manifest = (ROOT / "docs/PROPERTYQUARRY_RELEASE_MANIFEST.md").read_text(encoding="utf-8")

    required_scope_tokens = (
        "Whole-project gold must include implemented, customer-visible evidence overlays",
        "Teable ingestion table",
        "media-attention statistics with article links",
        "fiber/broadband coverage",
        "PROPERTYQUARRY_EVIDENCE_OVERLAY_TEMPORAL_CONTRACT.md",
        "cache recency must never be presented as source freshness",
        "Rybbit dashboard receipts",
        "visual quality and accessibility",
        "runtime security, supply chain, and authorization",
    )
    required_manifest_tokens = (
        "Evidence-map overlay source and browser UI proof is green for unavailable, stale, and verified states.",
        "Whole-project Gold remains blocked until local live authenticated source coverage and candidate-bound cache-recency, source-time/reference-period, and performance receipts cover",
        "Rybbit remains a whole-project gold blocker",
        "The authenticated local candidate gates and deployed/live receipts remain required before launch authority can be granted.",
        "Production security remains a whole-project gold blocker",
        "SBOM",
        "durable RBAC/session revocation",
    )

    for token in required_scope_tokens:
        assert token in scope
    for token in required_manifest_tokens:
        assert token in manifest


def test_evidence_overlay_registry_tracks_required_gold_layers() -> None:
    registry = json.loads((ROOT / "docs/PROPERTYQUARRY_EVIDENCE_OVERLAY_REGISTRY.json").read_text(encoding="utf-8"))
    layers = {row["layer_key"]: row for row in registry["layers"]}

    assert registry["contract_name"] == "propertyquarry.evidence_overlay_registry.v2"
    assert (
        registry["gold_policy"]["launch_receipt_schema"]
        == "propertyquarry.evidence_overlay_read_model_receipt.v3"
    )

    assert set(layers) == {
        "environmental_quality",
        "summer_heat",
        "traffic_noise",
        "public_mobility",
        "school_context",
        "official_safety_context",
        "media_attention",
        "fiber_broadband",
    }
    for layer in layers.values():
        assert layer["ingestion_mode"] == "async_teable_job"
        assert layer["read_model"] == "cached_postgres_geo_rollup"
        assert layer["search_policy"] == "read_cached_rollup_only_no_inline_fetch"
        assert {"unavailable", "stale", "verified"}.issubset(set(layer["ui_states"]))
        assert str(layer["teable_table"]).startswith("pq_geo_")
        assert "source_temporality" in layer["provenance_fields"]
        age_modes = set(layer["allowed_source_temporalities"]) & {
            "live",
            "current_feed",
        }
        assert set(layer["source_max_age_hours_by_temporality"]) == age_modes
        expected_sla_fields = {
            mode: "source_checked_at" if mode == "current_feed" else "source_updated_at"
            for mode in age_modes
        }
        assert layer["source_sla_timestamp_field_by_temporality"] == expected_sla_fields
        if "reference" in layer["allowed_source_temporalities"]:
            assert layer["reference_period_required_for"] == ["reference"]
            assert "reference_period" in layer["provenance_fields"]
    assert layers["media_attention"]["article_links_required"] is True
    assert "article_url" in layers["media_attention"]["provenance_fields"]
    assert "source_checked_at" in layers["media_attention"]["provenance_fields"]
    assert layers["media_attention"]["municipal_rss_independent_press"] is False
    assert "never property or person scoring" in layers["official_safety_context"]["customer_framing"]
    assert layers["official_safety_context"]["property_scoring"] is False
    assert layers["official_safety_context"]["person_scoring"] is False
    assert layers["official_safety_context"]["rights_caveat_required"] is True
    assert "provider address checks only as secondary verified jobs" in layers["fiber_broadband"]["customer_framing"]


def test_whole_project_scope_checker_enforces_overlay_registry() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/check_property_whole_project_scope.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "ok: property whole-project scope" in result.stdout


def test_whole_project_scope_checker_writes_gold_receipt(tmp_path: Path) -> None:
    receipt_path = tmp_path / "whole-project-scope.json"
    result = subprocess.run(
        [sys.executable, "scripts/check_property_whole_project_scope.py", "--write", str(receipt_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["schema"] == "propertyquarry.whole_project_scope_receipt.v1"
    assert receipt["status"] == "pass"
    assert receipt["failures"] == []
    assert set(receipt["required_overlay_layers"]) == {
        "environmental_quality",
        "summer_heat",
        "traffic_noise",
        "public_mobility",
        "school_context",
        "official_safety_context",
        "media_attention",
        "fiber_broadband",
    }
