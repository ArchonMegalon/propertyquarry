from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from app.product import service as product_service
from app.product.service import ProductService


def test_omagic_keys_available_accepts_magic_accounts_json(monkeypatch) -> None:
    for key in (
        "OMAGIC_API_KEY",
        "PROPERTYQUARRY_OMAGIC_API_KEY",
        "OMAGIC_EMAIL",
        "PROPERTYQUARRY_OMAGIC_EMAIL",
        "OMAGIC_ACCOUNTS_JSON",
        "PROPERTYQUARRY_OMAGIC_ACCOUNTS_JSON",
        "MAGIC_API_KEY",
        "PROPERTYQUARRY_MAGIC_API_KEY",
        "MAGIC_EMAIL",
        "PROPERTYQUARRY_MAGIC_EMAIL",
        "MAGIC_ACCOUNTS_JSON",
        "PROPERTYQUARRY_MAGIC_ACCOUNTS_JSON",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MAGIC_ACCOUNTS_JSON", json.dumps([{"email": "magic@example.com", "password": "secret"}]))

    assert product_service._omagic_keys_available() is True


def test_omagic_keys_available_accepts_suffix_magic_accounts_json(monkeypatch) -> None:
    for key in (
        "OMAGIC_API_KEY",
        "PROPERTYQUARRY_OMAGIC_API_KEY",
        "OMAGIC_EMAIL",
        "PROPERTYQUARRY_OMAGIC_EMAIL",
        "OMAGIC_ACCOUNTS_JSON",
        "PROPERTYQUARRY_OMAGIC_ACCOUNTS_JSON",
        "MAGIC_API_KEY",
        "PROPERTYQUARRY_MAGIC_API_KEY",
        "MAGIC_EMAIL",
        "PROPERTYQUARRY_MAGIC_EMAIL",
        "MAGIC_ACCOUNTS_JSON",
        "PROPERTYQUARRY_MAGIC_ACCOUNTS_JSON",
        "TEAM_MAGIC_ACCOUNTS_JSON",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TEAM_MAGIC_ACCOUNTS_JSON", json.dumps([{"email": "magic@example.com", "password": "secret"}]))

    assert product_service._omagic_keys_available() is True


def test_omagic_keys_available_accepts_magic_accounts_json_file(monkeypatch, tmp_path: Path) -> None:
    for key in (
        "OMAGIC_API_KEY",
        "PROPERTYQUARRY_OMAGIC_API_KEY",
        "OMAGIC_EMAIL",
        "PROPERTYQUARRY_OMAGIC_EMAIL",
        "OMAGIC_ACCOUNTS_JSON",
        "PROPERTYQUARRY_OMAGIC_ACCOUNTS_JSON",
        "OMAGIC_ACCOUNTS_JSON_FILE",
        "PROPERTYQUARRY_OMAGIC_ACCOUNTS_JSON_FILE",
        "MAGIC_API_KEY",
        "PROPERTYQUARRY_MAGIC_API_KEY",
        "MAGIC_EMAIL",
        "PROPERTYQUARRY_MAGIC_EMAIL",
        "MAGIC_ACCOUNTS_JSON",
        "PROPERTYQUARRY_MAGIC_ACCOUNTS_JSON",
        "MAGIC_ACCOUNTS_JSON_FILE",
        "PROPERTYQUARRY_MAGIC_ACCOUNTS_JSON_FILE",
    ):
        monkeypatch.delenv(key, raising=False)
    accounts_path = tmp_path / "magic-accounts.json"
    accounts_path.write_text(json.dumps([{"email": "magic@example.com", "password": "secret"}]), encoding="utf-8")
    monkeypatch.setenv("MAGIC_ACCOUNTS_JSON_FILE", str(accounts_path))

    assert product_service._omagic_keys_available() is True


def test_property_walkthrough_scene_video_context_collects_verified_controls_and_route_labels(tmp_path, monkeypatch) -> None:
    public_dir = tmp_path / "public_tours"
    bundle_dir = public_dir / "sample-flat"
    export_dir = bundle_dir / "3dvista"
    export_dir.mkdir(parents=True)
    (export_dir / "index.htm").write_text("<script src='lib/tdvplayer.js'></script>", encoding="utf-8")
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "title": "Sample Flat",
                "display_title": "Sample Flat",
                "control_mode": "3dvista",
                "scene_strategy": "walkable_3d",
                "scene_count": 3,
                "source_virtual_tour_url": "https://example.3dvista.com/tour/sample-flat",
                "three_d_vista_entry_relpath": "3dvista/index.htm",
                "three_d_vista_white_label_proof": {
                    "source_project": "propertyquarry",
                    "private_viewer_verified": True,
                    "non_trial_export_verified": True,
                    "propertyquarry_tour_metadata": True,
                    "trial_branding_checked": True,
                    "trial_branding_present": False,
                },
                "three_d_vista_browser_render_proof": {
                    "provider": "3dvista",
                    "status": "pass",
                    "rendered_viewer": True,
                },
                "walkable_scene": {
                    "route": [
                        {"label": "Entry"},
                        {"label": "Kitchen"},
                        {"label": "Balcony"},
                    ]
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(public_dir))

    context = product_service._property_walkthrough_scene_video_context("/tours/sample-flat")

    assert context["tour_url"] == "/tours/sample-flat"
    assert context["reference_provider"] == "3dvista"
    assert context["first_party_open_url"].endswith("/tours/sample-flat/control/3dvista")
    assert context["verified_provider"] == "3dvista"
    assert context["verified_open_url"].endswith("/tours/sample-flat/control/3dvista")
    assert context["control_url"].endswith("/tours/sample-flat/control/3dvista")
    assert context["control_urls"]["3dvista"].endswith("/tours/sample-flat/control/3dvista")
    assert context["provider_exports"] == ["3dvista"]
    assert context["route_labels"] == ["Entry", "Kitchen", "Balcony"]


def test_property_walkthrough_scene_video_context_uses_generated_reconstruction_as_primary_reference(tmp_path, monkeypatch) -> None:
    public_dir = tmp_path / "public_tours"
    bundle_dir = public_dir / "sample-generated-flat"
    generated_dir = bundle_dir / "generated-reconstruction"
    generated_dir.mkdir(parents=True)
    (generated_dir / "viewer.html").write_text("<!doctype html><title>viewer</title>", encoding="utf-8")
    (generated_dir / "model.glb").write_bytes(b"glTF")
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "title": "Generated Flat",
                "display_title": "Generated Flat",
                "scene_strategy": "generated_reconstruction",
                "creation_mode": "generated_reconstruction_tour",
                "generated_reconstruction": {
                    "provider": "propertyquarry_generated_reconstruction",
                    "viewer_version": "propertyquarry_3d_tour_viewer_v4",
                    "viewer_relpath": "generated-reconstruction/viewer.html",
                    "glb_model_relpath": "generated-reconstruction/model.glb",
                    "route_labels": ["Entry", "Living room", "Balcony"],
                    "walkable_scene": {
                        "kind": "generated_reconstruction_layout",
                        "route": [
                            {"label": "Entry", "kind": "entry"},
                            {"label": "Living room", "kind": "living"},
                            {"label": "Balcony", "kind": "outdoor"},
                        ],
                        "rooms": [
                            {"label": "Entry", "kind": "entry"},
                            {"label": "Living room", "kind": "living"},
                            {"label": "Balcony", "kind": "outdoor"},
                        ],
                    },
                    "room_stop_count": 3,
                    "verified_provider_capture": False,
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(public_dir))

    context = product_service._property_walkthrough_scene_video_context("/tours/sample-generated-flat")

    assert context["tour_url"] == "/tours/sample-generated-flat"
    assert context["reference_provider"] == ""
    assert context["verified_provider"] == ""
    assert context["verified_open_url"] == ""
    assert context["first_party_open_url"] == ""
    assert context["control_url"] == ""
    assert "viewer_url" not in context["generated_reconstruction"]
    assert context["generated_reconstruction"]["glb_model_url"].endswith(
        "/tours/files/sample-generated-flat/generated-reconstruction/model.glb"
    )
    assert context["route_labels"] == ["Entry", "Living room", "Balcony"]

    reference_text = product_service._property_walkthrough_context_reference_text(context)
    assert "prepared PropertyQuarry 3D tour" not in reference_text
    assert "primary spatial reference" not in reference_text
    assert "secondary, non-authoritative layout hint" in reference_text

    enriched = product_service._property_walkthrough_enrich_facts_with_context({}, tour_context_json=context)
    assert "tour_control_provider" not in enriched
    assert "tour_control_url" not in enriched
    assert enriched["room_visit_plan"] == ["Entry", "Living room", "Balcony"]


def test_render_property_flythrough_does_not_silent_fallback_from_magicfit(monkeypatch) -> None:
    monkeypatch.setattr(
        product_service,
        "_render_magicfit_property_flythrough_in_render_tools",
        lambda **kwargs: {
            "status": "failed",
            "reason": "magicfit_segment_render_failed",
            "provider_key": "magicfit",
        },
    )
    monkeypatch.setattr(
        product_service,
        "_render_onemin_property_flythrough_into_hosted_tour",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("unexpected_onemin_fallback")),
    )

    monkeypatch.setenv("EA_ROLE", "render-tools")
    result = product_service._render_property_flythrough_in_render_tools(
        tour_url="/tours/sample-flat",
        title="Sample Flat",
        preferred_provider_key="magicfit",
    )

    assert result["status"] == "failed"
    assert result["reason"] == "magicfit_segment_render_failed"
    assert result["media_route_provider_key"] == "magicfit"
    assert "media_route_fallback_provider_key" not in result


def test_render_property_flythrough_routes_omagic_without_onemin_or_magicfit_fallback(tmp_path, monkeypatch) -> None:
    public_dir = tmp_path / "public_tours"
    bundle_dir = public_dir / "sample-flat"
    model_dir = bundle_dir / "generated-reconstruction"
    model_dir.mkdir(parents=True)
    (model_dir / "model.glb").write_bytes(b"glTF")
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": "sample-flat",
                "generated_reconstruction": {
                    "glb_model_relpath": "generated-reconstruction/model.glb",
                    "verified_provider_capture": False,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(public_dir))
    monkeypatch.setenv("OMAGIC_API_KEY", "test-key")
    monkeypatch.delenv("PROPERTYQUARRY_OMAGIC_MODEL_UPLOAD_ENABLED", raising=False)
    monkeypatch.delenv("PROPERTYQUARRY_OMAGIC_RENDER_COMMAND", raising=False)
    monkeypatch.delenv("PROPERTYQUARRY_OMAGIC_RENDER_ENDPOINT", raising=False)
    monkeypatch.setattr(
        product_service,
        "_render_onemin_property_flythrough_into_hosted_tour",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("unexpected_onemin_fallback")),
    )
    monkeypatch.setattr(
        product_service,
        "_render_magicfit_property_flythrough_in_render_tools",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("unexpected_magicfit_fallback")),
    )

    monkeypatch.setenv("EA_ROLE", "render-tools")
    result = product_service._render_property_flythrough_in_render_tools(
        tour_url="/tours/sample-flat",
        title="Sample Flat",
        preferred_provider_key="omagic",
    )

    assert result["status"] == "failed"
    assert result["reason"] == "omagic_model_upload_adapter_disabled"
    assert result["provider_key"] == "omagic"
    assert result["media_route_provider_key"] == "omagic"
    assert result["model_input_required"] is True
    assert result["model_asset_kind"] == "glb"
    assert result["model_path"].endswith("generated-reconstruction/model.glb")


def test_render_property_flythrough_omagic_command_adapter_writes_hosted_video(tmp_path, monkeypatch) -> None:
    public_dir = tmp_path / "public_tours"
    bundle_dir = public_dir / "sample-flat"
    model_dir = bundle_dir / "generated-reconstruction"
    model_dir.mkdir(parents=True)
    (model_dir / "model.glb").write_bytes(b"glTF")
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": "sample-flat",
                "generated_reconstruction": {
                    "glb_model_relpath": "generated-reconstruction/model.glb",
                    "verified_provider_capture": False,
                },
            }
        ),
        encoding="utf-8",
    )
    fake_adapter = tmp_path / "fake_omagic_adapter.py"
    fake_adapter.write_text(
        """
from __future__ import annotations

import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--request-json", required=True)
parser.add_argument("--out", required=True)
parser.add_argument("--state-json", required=True)
args = parser.parse_args()
request = json.loads(Path(args.request_json).read_text(encoding="utf-8"))
assert request["provider_key"] == "omagic"
assert request["model_path"].endswith("generated-reconstruction/model.glb")
Path(args.out).write_bytes(b"fake-omagic-video")
state = {
    "render_status": "completed",
    "video_path": args.out,
    "model_input_consumed": True,
    "model_input_consumption_proof": "fake-product-command-adapter",
}
Path(args.state_json).write_text(json.dumps(state), encoding="utf-8")
print(json.dumps(state))
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(public_dir))
    monkeypatch.setenv("PROPERTYQUARRY_OMAGIC_MODEL_UPLOAD_ENABLED", "1")
    monkeypatch.setenv("PROPERTYQUARRY_OMAGIC_API_KEY", "test-key")
    monkeypatch.setenv("PROPERTYQUARRY_OMAGIC_RENDER_COMMAND", f"{sys.executable} {fake_adapter}")
    monkeypatch.setattr(
        product_service,
        "_render_onemin_property_flythrough_into_hosted_tour",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("unexpected_onemin_fallback")),
    )
    monkeypatch.setattr(
        product_service,
        "_render_magicfit_property_flythrough_in_render_tools",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("unexpected_magicfit_fallback")),
    )

    monkeypatch.setenv("EA_ROLE", "render-tools")
    result = product_service._render_property_flythrough_in_render_tools(
        tour_url="/tours/sample-flat",
        title="Sample Flat",
        preferred_provider_key="omagic",
    )

    assert result["status"] == "rendered"
    assert result["provider_key"] == "omagic"
    assert result["media_route_provider_key"] == "omagic"
    assert result["model_input_consumed"] is True
    video_path = Path(str(result["video_file_path"]))
    assert video_path.read_bytes() == b"fake-omagic-video"
    sidecar = json.loads((bundle_dir / "tour.omagic.json").read_text(encoding="utf-8"))
    assert sidecar["model_input_consumption_proof"] == "fake-product-command-adapter"
    assert sidecar["provider_backend_key"] == "omagic"
    manifest = json.loads((bundle_dir / "tour.json").read_text(encoding="utf-8"))
    assert manifest["video_provider_key"] == "omagic"
    assert manifest["video_provider"] == "omagic"
    assert manifest["video_relpath"] == result["video_relpath"]
    assert manifest["video_sidecar_relpath"] == "tour.omagic.json"
    assert manifest["video_coverage_proof"] == "omagic_model_upload"


def test_render_property_flythrough_blank_provider_uses_runtime_selected_magicfit(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.scene_video_contract.resolve_property_walkthrough_runtime_provider",
        lambda value, allow_non_final_fallback=True: {
            "provider_key": "magicfit",
            "provider_backend_key": "magicfit",
            "runtime_readiness_json": {
                "provider_key": "magicfit",
                "provider_backend_key": "magicfit",
                "ready": True,
                "status": "ready",
                "blockers": [],
                "checks": {},
            },
            "checked": [
                {"provider_key": "omagic", "ready": False, "status": "blocked", "blockers": ["omagic_model_upload_adapter_missing"]},
                {"provider_key": "magicfit", "ready": True, "status": "ready", "blockers": []},
            ],
            "selected_via": "auto_final_ready",
            "explicit_requested": False,
        },
    )
    monkeypatch.setattr(
        product_service,
        "_render_magicfit_property_flythrough_in_render_tools",
        lambda **kwargs: {
            "status": "rendered",
            "reason": "",
            "provider_key": "magicfit",
            "video_url": "https://cdn.example/property/walkthrough.mp4",
        },
    )
    monkeypatch.setattr(
        product_service,
        "_render_omagic_property_flythrough_into_hosted_tour",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("unexpected_omagic_path")),
    )

    monkeypatch.setenv("EA_ROLE", "render-tools")
    result = product_service._render_property_flythrough_in_render_tools(
        tour_url="/tours/sample-flat",
        title="Sample Flat",
    )

    assert result["status"] == "rendered"
    assert result["media_route_provider_key"] == "magicfit"
    assert result["media_route_auto_selected_provider_key"] == "magicfit"


def test_render_property_flythrough_blank_provider_fails_closed_when_no_runtime_provider_is_ready(monkeypatch) -> None:
    progress_updates: list[dict[str, object]] = []
    monkeypatch.setattr(
        "app.services.scene_video_contract.resolve_property_walkthrough_runtime_provider",
        lambda value, allow_non_final_fallback=False: {
            "provider_key": "magicfit",
            "provider_backend_key": "magicfit",
            "runtime_readiness_json": {
                "provider_key": "magicfit",
                "provider_backend_key": "magicfit",
                "ready": False,
                "status": "blocked",
                "blockers": ["magicfit_insufficient_credits"],
                "checks": {"credit_state": "insufficient"},
            },
            "checked": [
                {"provider_key": "omagic", "ready": False, "status": "blocked", "blockers": ["omagic_model_upload_adapter_missing"]},
                {"provider_key": "magicfit", "ready": False, "status": "blocked", "blockers": ["magicfit_insufficient_credits"]},
                {"provider_key": "onemin_i2v", "ready": False, "status": "blocked", "blockers": ["onemin_i2v_insufficient_credits"]},
            ],
            "selected_via": "auto_no_ready_provider",
            "explicit_requested": False,
        },
    )
    monkeypatch.setattr(
        product_service,
        "_write_hosted_property_visual_progress",
        lambda **kwargs: progress_updates.append(dict(kwargs)) or dict(kwargs),
    )
    monkeypatch.setattr(
        product_service,
        "_render_magicfit_property_flythrough_in_render_tools",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("unexpected_magicfit_path")),
    )
    monkeypatch.setattr(
        product_service,
        "_render_onemin_property_flythrough_into_hosted_tour",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("unexpected_onemin_path")),
    )
    monkeypatch.setattr(
        product_service,
        "_render_omagic_property_flythrough_into_hosted_tour",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("unexpected_omagic_path")),
    )

    monkeypatch.setenv("EA_ROLE", "render-tools")
    result = product_service._render_property_flythrough_in_render_tools(
        tour_url="/tours/sample-flat",
        title="Sample Flat",
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "magicfit_insufficient_credits"
    assert result["media_route_status"] == "blocked"
    assert result["media_route_provider_key"] == "magicfit"
    assert result["media_route_reason"] == "magicfit_insufficient_credits"
    assert result["media_route_auto_selected_provider_key"] == "magicfit"
    assert result["media_route_candidates"] == ["omagic", "magicfit", "onemin_i2v"]
    assert result["media_route_runtime_provider_resolution_json"]["selected_via"] == "auto_no_ready_provider"
    assert result["media_route_runtime_readiness_json"]["status"] == "blocked"
    assert (
        product_service._property_visual_terminal_status_for_reason(
            request_kind="flythrough",
            reason="magicfit_insufficient_credits",
        )
        == "skipped"
    )
    assert progress_updates == [
        {
            "tour_url": "/tours/sample-flat",
            "request_kind": "flythrough",
            "status": "blocked",
            "progress_pct": 0,
            "detail": "Walkthrough rendering is paused until render credits are available.",
            "reason": "magicfit_insufficient_credits",
            "provider_key": "magicfit",
        }
    ]


def test_render_property_flythrough_blank_provider_can_publish_with_runtime_selected_onemin(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.scene_video_contract.resolve_property_walkthrough_runtime_provider",
        lambda value, allow_non_final_fallback=False: {
            "provider_key": "onemin_i2v",
            "provider_backend_key": "onemin_i2v",
            "runtime_readiness_json": {
                "provider_key": "onemin_i2v",
                "provider_backend_key": "onemin_i2v",
                "ready": True,
                "status": "ready",
                "blockers": [],
                "checks": {},
            },
            "checked": [
                {"provider_key": "omagic", "ready": False, "status": "blocked", "blockers": ["omagic_model_upload_adapter_missing"]},
                {"provider_key": "magicfit", "ready": False, "status": "blocked", "blockers": ["magicfit_insufficient_credits"]},
                {"provider_key": "onemin_i2v", "ready": True, "status": "ready", "blockers": []},
            ],
            "selected_via": "auto_final_ready",
            "explicit_requested": False,
        },
    )
    monkeypatch.setattr(
        product_service,
        "_render_onemin_property_flythrough_into_hosted_tour",
        lambda **kwargs: {
            "status": "rendered",
            "reason": "",
            "provider_key": "onemin_i2v",
            "video_url": "https://cdn.example/property/onemin.mp4",
        },
    )
    monkeypatch.setattr(
        product_service,
        "_render_magicfit_property_flythrough_in_render_tools",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("unexpected_magicfit_path")),
    )
    monkeypatch.setattr(
        product_service,
        "_render_omagic_property_flythrough_into_hosted_tour",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("unexpected_omagic_path")),
    )

    monkeypatch.setenv("EA_ROLE", "render-tools")
    result = product_service._render_property_flythrough_in_render_tools(
        tour_url="/tours/sample-flat",
        title="Sample Flat",
    )

    assert result["status"] == "rendered"
    assert result["media_route_provider_key"] == "onemin_i2v"
    assert result["media_route_auto_selected_provider_key"] == "onemin_i2v"


def test_magicfit_room_visit_plan_treats_maisonette_staircase_as_walkable_stop() -> None:
    room_count, route_labels = product_service._magicfit_property_room_visit_plan(
        title="Maisonette with balcony",
        property_facts={
            "rooms": 3,
            "description": "Maisonette mit Treppe, Balkon und separatem WC.",
        },
    )

    assert "staircase" in route_labels
    assert "balcony/terrace" in route_labels
    assert room_count == len(route_labels)
    assert product_service._magicfit_flythrough_minimum_duration_seconds(
        title="Maisonette with balcony",
        property_facts={
            "rooms": 3,
            "description": "Maisonette mit Treppe, Balkon und separatem WC.",
        },
    ) == float(len(route_labels) * 15)


def test_magicfit_room_visit_plan_keeps_explicit_route_labels_authoritative_over_scene_count() -> None:
    room_count, route_labels = product_service._magicfit_property_room_visit_plan(
        title="Runtime reconstruction smoke onemin 20260705",
        property_facts={
            "magicfit_route_labels": ["entry/hall"],
            "room_count": 1,
            "tour_scene_count": 12,
        },
    )

    assert room_count == 2
    assert route_labels == ["entry/hall", "living room"]
    assert not any(label.startswith("walkthrough stop") for label in route_labels)


def test_magicfit_room_visit_plan_extends_partial_explicit_route_labels_to_full_walkable_plan() -> None:
    room_count, route_labels = product_service._magicfit_property_room_visit_plan(
        title="2 room apartment",
        property_facts={
            "magicfit_route_labels": ["entry/hall", "living room"],
            "room_count": 2,
        },
    )

    assert room_count == 3
    assert route_labels == ["entry/hall", "living room", "bedroom"]
    assert product_service._magicfit_flythrough_minimum_duration_seconds(
        title="2 room apartment",
        property_facts={
            "magicfit_route_labels": ["entry/hall", "living room"],
            "room_count": 2,
        },
    ) == 45.0


def test_property_walkthrough_onemin_timeouts_use_walkthrough_floor(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_ONEMIN_FEATURE_TIMEOUT_SECONDS", "90")
    monkeypatch.setenv("PROPERTYQUARRY_ONEMIN_SEGMENT_SUBPROCESS_TIMEOUT_SECONDS", "240")
    monkeypatch.delenv("PROPERTYQUARRY_ONEMIN_WALKTHROUGH_FEATURE_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("PROPERTYQUARRY_ONEMIN_WALKTHROUGH_SEGMENT_SUBPROCESS_TIMEOUT_SECONDS", raising=False)

    assert product_service._property_walkthrough_onemin_feature_timeout_seconds() == 120
    assert product_service._property_walkthrough_onemin_segment_subprocess_timeout_seconds() == 420


def test_onemin_walkthrough_defaults_to_15_second_room_segments(tmp_path, monkeypatch) -> None:
    public_dir = tmp_path / "public_tours"
    bundle_dir = public_dir / "sample-flat"
    bundle_dir.mkdir(parents=True)
    preview_path = bundle_dir / "preview.jpg"
    preview_path.write_bytes(b"jpg")
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": "sample-flat",
                "preview_relpath": "preview.jpg",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(public_dir))
    monkeypatch.setenv("PROPERTYQUARRY_FLYTHROUGH_SECONDS_PER_ROUTE_STOP", "15")
    monkeypatch.delenv("PROPERTYQUARRY_ONEMIN_SEGMENT_DURATION_SECONDS", raising=False)
    monkeypatch.setattr(product_service, "_onemin_i2v_keys_available", lambda: True)
    monkeypatch.setattr(product_service, "_repo_root", lambda: tmp_path)
    captured_principal_ids: list[str] = []

    def _fake_scene_video_context(_tour_url, *, principal_id: str = ""):
        captured_principal_ids.append(principal_id)
        return {}

    monkeypatch.setattr(
        product_service,
        "_property_walkthrough_scene_video_context",
        _fake_scene_video_context,
    )
    monkeypatch.setattr(
        product_service,
        "_property_walkthrough_enrich_facts_with_context",
        lambda facts, tour_context_json=None: dict(facts or {}),
    )
    monkeypatch.setattr(product_service, "_hosted_property_tour_scene_count", lambda _tour_url: 0)
    monkeypatch.setattr(product_service, "_default_magicfit_property_flythrough_prompt", lambda **kwargs: "Walk through the home.")
    monkeypatch.setattr(product_service, "_write_hosted_property_visual_progress", lambda **kwargs: None)
    monkeypatch.setattr(product_service, "_video_continuous_shot_gate", lambda _path: (True, "", {}))
    monkeypatch.setattr(product_service, "_video_segment_boundary_gate", lambda _paths: (True, "", {}))
    monkeypatch.setattr(
        product_service,
        "_extract_video_boundary_frame",
        lambda _video, output_path, position="last": output_path.write_bytes(b"jpg"),
    )
    monkeypatch.setattr(
        product_service,
        "_concat_video_segments",
        lambda _segment_paths, output_path: (output_path.write_bytes(b"mp4"), {})[1],
    )
    monkeypatch.setattr(
        product_service,
        "_update_hosted_property_tour_video_manifest",
        lambda **kwargs: None,
    )
    render_script = tmp_path / "scripts" / "render_onemin_property_i2v_segment.py"
    render_script.parent.mkdir(parents=True, exist_ok=True)
    render_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    captured_durations: list[str] = []
    recorded_video_durations: dict[str, float] = {}

    def _fake_video_duration_seconds(video_ref: str) -> float:
        return float(recorded_video_durations.get(str(video_ref), 0.0))

    monkeypatch.setattr(product_service, "_video_duration_seconds", _fake_video_duration_seconds)

    def _fake_run(command, cwd=None, capture_output=None, text=None, timeout=None, check=None):
        duration_index = command.index("--duration") + 1
        captured_durations.append(str(command[duration_index]))
        requested_duration = float(command[duration_index])
        output_path = command[command.index("--out") + 1]
        state_json_path = command[command.index("--state-json") + 1]
        with open(output_path, "wb") as handle:
            handle.write(b"mp4")
        recorded_video_durations[str(output_path)] = requested_duration
        with open(state_json_path, "w", encoding="utf-8") as handle:
            json.dump({"status": "completed"}, handle)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    def _fake_concat_video_segments(segment_paths, output_path):
        output_path.write_bytes(b"mp4")
        recorded_video_durations[str(output_path)] = sum(recorded_video_durations.get(str(path), 0.0) for path in segment_paths)
        return {}

    monkeypatch.setattr(product_service, "_concat_video_segments", _fake_concat_video_segments)
    monkeypatch.setattr(product_service.subprocess, "run", _fake_run)

    result = product_service._render_onemin_property_flythrough_into_hosted_tour(
        tour_url="/tours/sample-flat",
        title="2 room apartment",
        principal_id="principal-onemin-test",
        property_facts={
            "magicfit_route_labels": ["entry/hall", "living room"],
            "room_count": 2,
        },
    )

    assert result["status"] == "rendered"
    assert result["provider_key"] == "onemin_i2v"
    assert captured_principal_ids == ["principal-onemin-test"]
    assert captured_durations == ["10", "5", "10", "5", "10", "5"]
    assert result["combined_duration_seconds"] == 45.0


def test_run_scene_video_skill_uses_principal_context_and_scrubs_payload_principal() -> None:
    captured_request = None

    class _FakeOrchestrator:
        def execute_task_artifact(self, request):
            nonlocal captured_request
            captured_request = request
            return SimpleNamespace(
                structured_output_json={
                    "deliverable_type": "scene_video_packet",
                    "provider_key": "magicfit",
                    "render_status": "completed",
                    "video_url": "https://cdn.example/property/walkthrough.mp4",
                },
                content="",
            )

    service = ProductService(
        SimpleNamespace(
            orchestrator=_FakeOrchestrator(),
            preference_profiles=SimpleNamespace(),
        )
    )

    result = service._run_scene_video_skill(
        title="Sample Flat",
        actor="property-worker",
        provider_key="magicfit",
        task_principal_id="cf-email:operator@example.test",
        input_json={
            "principal_id": "should-not-leak",
            "context_kind": "property_walkthrough",
            "tour_url": "/tours/sample-flat",
        },
    )

    assert result["provider_key"] == "magicfit"
    assert captured_request.principal_id == "cf-email:operator@example.test"
    assert captured_request.input_json["provider_key"] == "magicfit"
    assert "principal_id" not in captured_request.input_json


def test_hosted_property_visual_progress_snapshot_roundtrip(tmp_path, monkeypatch) -> None:
    public_dir = tmp_path / "public_tours"
    bundle_dir = public_dir / "sample-flat"
    bundle_dir.mkdir(parents=True)
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(public_dir))

    product_service._write_hosted_property_visual_progress(
        tour_url="/tours/sample-flat",
        request_kind="flythrough",
        status="processing",
        progress_pct=44,
        detail="Rendering walkthrough segment 2 of 4.",
        reason="",
        provider_key="magicfit",
        step_index=2,
        step_total=4,
        updated_at="2026-06-29T10:15:00+00:00",
    )

    snapshot = product_service._hosted_property_visual_progress_snapshot(
        "/tours/sample-flat",
        request_kind="flythrough",
    )

    assert snapshot["status"] == "processing"
    assert snapshot["progress_pct"] == 44
    assert snapshot["detail"] == "Rendering walkthrough segment 2 of 4."
    assert snapshot["provider_key"] == "magicfit"
    assert snapshot["step_index"] == 2
    assert snapshot["step_total"] == 4
    assert product_service._hosted_property_visual_progress_stage_label(snapshot) == "segment 2 of 4"
