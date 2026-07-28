from __future__ import annotations

import json
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]


def test_3d_browser_gate_honors_configured_chromium_executable(monkeypatch, tmp_path: Path) -> None:
    from scripts import propertyquarry_3d_browser_gate as gate

    chromium_path = tmp_path / "chromium"
    chromium_path.write_bytes(b"browser")
    monkeypatch.setenv("PROPERTYQUARRY_PLAYWRIGHT_CHROMIUM_EXECUTABLE", str(chromium_path))
    playwright = type(
        "PlaywrightStub",
        (),
        {"chromium": type("ChromiumStub", (), {"executable_path": "/missing/chromium"})()},
    )()

    launch_kwargs = gate.playwright_chromium_launch_kwargs(
        playwright,
        args=["--no-sandbox", "--disable-gpu"],
    )

    assert launch_kwargs == {
        "headless": True,
        "executable_path": str(chromium_path),
        "args": ["--no-sandbox", "--disable-gpu"],
    }


def test_3d_browser_gate_treats_csp_and_frame_blockers_as_failures() -> None:
    from scripts import propertyquarry_3d_browser_gate as gate

    blockers = gate._bad_console_messages(
        [
            {
                "type": "pageerror",
                "text": "WebAssembly.instantiate(): violates the following Content Security Policy directive",
            },
            {
                "type": "error",
                "text": "Refused to display 'https://discover.matterport.com/' in a frame because it set X-Frame-Options",
            },
            {"type": "warning", "text": "A harmless preload warning"},
        ]
    )

    assert len(blockers) == 2


def test_3d_browser_gate_requires_real_canvas_and_no_loading_state() -> None:
    from scripts import propertyquarry_3d_browser_gate as gate

    assert gate._provider_rendered_ok(
        "3dvista",
        {
            "provider_frame_url": "https://propertyquarry.com/tours/demo/3dvista/index.htm",
            "visible_canvas_count": 2,
            "loading_indicator_count": 0,
            "frame_text": "",
        },
    )
    assert not gate._provider_rendered_ok(
        "3dvista",
        {
            "provider_frame_url": "https://propertyquarry.com/tours/demo/3dvista/index.htm",
            "visible_canvas_count": 2,
            "loading_indicator_count": 1,
            "frame_text": "",
        },
    )
    assert not gate._provider_rendered_ok(
        "pano2vr",
        {
            "provider_frame_url": "https://propertyquarry.com/tours/demo/pano2vr/index.html",
            "visible_canvas_count": 0,
            "loading_indicator_count": 0,
            "frame_text": "",
        },
    )


def test_3d_browser_gate_requires_matterport_embeddable_show_url() -> None:
    from scripts import propertyquarry_3d_browser_gate as gate

    assert gate._provider_rendered_ok(
        "matterport",
        {
            "provider_frame_url": "https://my.matterport.com/show/?m=uoRT7VqgY7E",
            "external_embedded_target_ok": True,
        },
    )
    assert not gate._provider_rendered_ok(
        "matterport",
        {
            "provider_frame_url": "https://discover.matterport.com/space/uoRT7VqgY7E",
            "external_embedded_target_ok": False,
        },
    )


def test_3d_browser_gate_ignores_noncritical_external_provider_asset_failures() -> None:
    from scripts import propertyquarry_3d_browser_gate as gate

    failures = gate._bad_request_failures(
        [
            {
                "url": "https://cdn-2.matterport.com/model/preview.jpg",
                "resource_type": "image",
                "failure": "net::ERR_BLOCKED_BY_ORB",
            },
            {
                "url": "http://propertyquarry.com:8097/app.js",
                "resource_type": "script",
                "failure": "net::ERR_FAILED",
            },
            {
                "url": "https://my.matterport.com/show/?m=demo",
                "resource_type": "document",
                "failure": "net::ERR_BLOCKED_BY_RESPONSE",
            },
        ],
        browser_base_url="http://propertyquarry.com:8097",
    )

    assert [row["resource_type"] for row in failures] == ["script", "document"]


def test_3d_browser_gate_persists_3dvista_browser_render_proof_in_private_receipt(monkeypatch, tmp_path: Path) -> None:
    from scripts import propertyquarry_3d_browser_gate as gate
    from scripts.property_tour_3dvista_provenance import export_tree_sha256
    from scripts.verify_property_tour_controls import build_property_tour_control_receipt

    slug = "browser-proof-demo"
    bundle = tmp_path / slug
    bundle.mkdir(parents=True)
    (bundle / "tour.json").write_text(
        json.dumps(
                {
                    "slug": slug,
                    "display_title": "Browser proof demo",
                    "three_d_vista_entry_relpath": "3dvista/index.html",
                    "three_d_vista_import": {
                        "source_project": "propertyquarry",
                        "entry_relpath": "3dvista/index.html",
                    },
                }
            ),
            encoding="utf-8",
        )
    (bundle / "3dvista").mkdir()
    (bundle / "3dvista" / "index.html").write_text(
        "<!doctype html><script>window.tdvplayer = true;</script><div>viewer</div>",
        encoding="utf-8",
    )
    (bundle / "tour.private.json").write_text(
        json.dumps(
            {
                "three_d_vista_white_label_proof": {
                    "source_project": "propertyquarry",
                    "private_viewer_verified": True,
                    "non_trial_export_verified": True,
                    "propertyquarry_tour_metadata": True,
                    "trial_branding_checked": True,
                    "trial_branding_present": False,
                },
                "three_d_vista_target_provenance": {
                    "schema": "propertyquarry.3dvista_target_provenance.v1",
                    "status": "pass",
                    "provider": "3dvista",
                    "target_slug": slug,
                    "target_subdir": "3dvista",
                    "artifact": {
                        "kind": "local_export",
                        "sha256": export_tree_sha256(bundle / "3dvista"),
                        "entry_relpath": "index.html",
                    },
                    "authorization": {
                        "status": "approved",
                        "reference": "test-reviewed-export",
                    },
                    "review": {
                        "property_match": "pass",
                        "visual_match": "pass",
                        "reviewed_by": "propertyquarry-test-reviewer",
                        "reviewed_at": "2026-07-04T09:45:00Z",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        gate,
        "_persist_3dvista_browser_render_proof_in_runtime_container",
        lambda slug, proof, runtime_container="": {
            "status": "runtime_container_not_configured",
            "slug": slug,
            "provider": proof.get("provider"),
        },
    )

    receipt = {
        "contract_name": "propertyquarry.3d_browser_gate.v1",
        "generated_at": "2026-07-04T10:00:00Z",
        "base_url": "https://propertyquarry.com",
        "browser_base_url": "https://propertyquarry.com",
        "demo_slug": slug,
        "providers": ["matterport", "3dvista"],
        "checks": [
            {"name": "3dvista_rendered_viewer", "ok": True},
            {"name": "3dvista_control_page_ok", "ok": True},
        ],
        "provider_results": [
            {
                "provider": "3dvista",
                "status": "pass",
                "state": {"provider_frame_url": "https://propertyquarry.com/tours/demo/3dvista/index.html"},
            }
        ],
    }

    persistence = gate.persist_3dvista_browser_render_proof_from_receipt(
        receipt,
        public_roots=(tmp_path,),
    )

    assert persistence["status"] == "updated"
    public_manifest = json.loads((bundle / "tour.json").read_text(encoding="utf-8"))
    assert "three_d_vista_browser_render_proof" not in public_manifest
    private_manifest = json.loads((bundle / "tour.private.json").read_text(encoding="utf-8"))
    assert private_manifest["three_d_vista_browser_render_proof"]["status"] == "pass"
    control_receipt = build_property_tour_control_receipt(tour_root=tmp_path)
    assert "3dvista" in control_receipt["ready_provider_modes"]


def test_3d_browser_gate_requires_explicit_persistence_target() -> None:
    from scripts import propertyquarry_3d_browser_gate as gate

    receipt = {
        "contract_name": "propertyquarry.3d_browser_gate.v1",
        "generated_at": "2026-07-10T08:57:09Z",
        "base_url": "http://localhost:18097",
        "browser_base_url": "http://propertyquarry.com:18097",
        "demo_slug": "candidate-demo",
        "providers": ["3dvista"],
        "checks": [{"name": "3dvista_rendered_viewer", "ok": True}],
        "provider_results": [{"provider": "3dvista", "status": "pass", "state": {}}],
    }

    persistence = gate.persist_3dvista_browser_render_proof_from_receipt(receipt)

    assert persistence["status"] == "persistence_target_not_configured"
    assert persistence["container_result"]["status"] == "runtime_container_not_configured"
    assert persistence["host_result"]["status"] == "public_tour_root_not_configured"


def test_3d_browser_gate_host_roots_are_explicit_and_not_derived_from_runtime_container(
    tmp_path: Path,
) -> None:
    from scripts import propertyquarry_3d_browser_gate as gate

    candidate_volume = tmp_path / "candidate-volume"
    candidate_volume.mkdir()

    assert gate._candidate_public_tour_roots() == []

    roots = gate._candidate_public_tour_roots(public_roots=(candidate_volume,))
    assert roots == [candidate_volume.resolve()]


def test_walkthrough_quality_gate_fails_without_room_coverage_receipt(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from scripts import propertyquarry_walkthrough_quality_gate as gate

    slug = "demo"
    bundle = tmp_path / slug
    bundle.mkdir()
    (bundle / "walkthrough.mp4").write_bytes(b"not-a-real-video")
    (bundle / "tour.json").write_text(
        json.dumps({"slug": slug, "video_relpath": "walkthrough.mp4"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "_video_metadata", lambda _path, *, timeout_seconds=None: {"format": {"duration": "45"}})
    monkeypatch.setattr(
        gate,
        "_frame_delta_stats",
        lambda _path, *, timeout_seconds=None: {"ok": True, "sampled_frame_count": 20, "delta_count": 19, "max_delta": 12.0},
    )

    receipt = gate.build_walkthrough_quality_receipt(
        tour_root=str(tmp_path),
        demo_slug=slug,
        max_jump_delta=42.0,
        min_duration_seconds=30.0,
    )

    assert receipt["status"] == "fail"
    failed = {row["name"] for row in receipt["checks"] if not row["ok"]}
    assert "walkthrough_room_coverage_receipt_present" in failed
    assert "walkthrough_room_coverage_complete" in failed


def test_walkthrough_quality_gate_accepts_complete_scene_segment_coverage(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from scripts import propertyquarry_walkthrough_quality_gate as gate

    slug = "demo"
    bundle = tmp_path / slug
    bundle.mkdir()
    (bundle / "walkthrough.mp4").write_bytes(b"not-a-real-video")
    (bundle / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "video_relpath": "walkthrough.mp4",
                "walkthrough_coverage_proof": {
                    "status": "pass",
                    "segments_expected": ["entry", "living", "kitchen", "bathroom"],
                    "segments_visited": ["entry", "living", "kitchen", "bathroom"],
                    "coverage_segments": [
                        {"segment": "entry", "start": 0, "end": 8},
                        {"segment": "living", "start": 8, "end": 18},
                        {"segment": "kitchen", "start": 18, "end": 30},
                        {"segment": "bathroom", "start": 30, "end": 42},
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "_video_metadata", lambda _path, *, timeout_seconds=None: {"format": {"duration": "45"}})
    monkeypatch.setattr(
        gate,
        "_frame_delta_stats",
        lambda _path, *, timeout_seconds=None: {"ok": True, "sampled_frame_count": 20, "delta_count": 19, "max_delta": 12.0},
    )

    receipt = gate.build_walkthrough_quality_receipt(
        tour_root=str(tmp_path),
        demo_slug=slug,
        max_jump_delta=42.0,
        min_duration_seconds=30.0,
    )

    assert receipt["status"] == "pass"


def test_walkthrough_quality_gate_reads_magicfit_sidecar_route_coverage(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from scripts import propertyquarry_walkthrough_quality_gate as gate

    slug = "demo"
    bundle = tmp_path / slug
    bundle.mkdir()
    (bundle / "walkthrough.mp4").write_bytes(b"not-a-real-video")
    (bundle / "tour.magicfit.json").write_text(
        json.dumps(
            {
                "provider": "MagicFit",
                "route_labels": ["entry", "living", "kitchen"],
                "covered_route_labels": ["entry", "living", "kitchen"],
            }
        ),
        encoding="utf-8",
    )
    (bundle / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "video_relpath": "walkthrough.mp4",
                "video_sidecar_relpath": "tour.magicfit.json",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "_video_metadata", lambda _path, *, timeout_seconds=None: {"format": {"duration": "45"}})
    monkeypatch.setattr(
        gate,
        "_frame_delta_stats",
        lambda _path, *, timeout_seconds=None: {"ok": True, "sampled_frame_count": 20, "delta_count": 19, "max_delta": 12.0},
    )

    receipt = gate.build_walkthrough_quality_receipt(
        tour_root=str(tmp_path),
        demo_slug=slug,
        max_jump_delta=42.0,
        min_duration_seconds=30.0,
    )

    assert receipt["status"] == "pass"


def test_walkthrough_quality_gate_passes_declared_transition_timing_to_sampler(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from scripts import propertyquarry_walkthrough_quality_gate as gate

    slug = "demo"
    bundle = tmp_path / slug
    bundle.mkdir()
    (bundle / "walkthrough.mp4").write_bytes(b"not-a-real-video")
    (bundle / "tour.magicfit.json").write_text(
        json.dumps(
            {
                "provider": "MagicFit",
                "route_labels": ["entry", "living"],
                "covered_route_labels": ["entry", "living"],
                "transition_offsets_seconds": [14.093, 28.186],
                "transition_seconds": 1.0,
            }
        ),
        encoding="utf-8",
    )
    (bundle / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "video_relpath": "walkthrough.mp4",
                "video_sidecar_relpath": "tour.magicfit.json",
            }
        ),
        encoding="utf-8",
    )
    observed: dict[str, object] = {}

    def _fake_delta_stats(_path, **kwargs):
        observed.update(kwargs)
        return {"ok": True, "sampled_frame_count": 20, "delta_count": 19, "max_delta": 12.0}

    monkeypatch.setattr(gate, "_video_metadata", lambda _path, *, timeout_seconds=None: {"format": {"duration": "45"}})
    monkeypatch.setattr(gate, "_frame_delta_stats", _fake_delta_stats)

    receipt = gate.build_walkthrough_quality_receipt(
        tour_root=str(tmp_path),
        demo_slug=slug,
        max_jump_delta=42.0,
        min_duration_seconds=30.0,
    )

    assert receipt["status"] == "pass"
    assert observed["transition_offsets_seconds"] == [14.093, 28.186]
    assert observed["transition_seconds"] == 1.0
    assert receipt["continuity_sampling_context"]["source"] == "video_sidecar"


def test_walkthrough_frame_sampler_compares_local_cadence_not_distant_rooms(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from scripts import propertyquarry_walkthrough_quality_gate as gate

    video_path = tmp_path / "walkthrough.mp4"
    video_path.write_bytes(b"not-a-real-video")
    monkeypatch.setattr(gate, "_video_metadata", lambda _path, *, timeout_seconds=None: {"format": {"duration": "45"}})

    def _fake_run(command, *args, **kwargs):
        assert command[-1] == "pipe:1"
        frames = []
        for index in range(226):
            level = min(255, index * 4)
            frames.append(Image.new("RGB", (160, 90), (level, level, level)).tobytes())
        return subprocess.CompletedProcess(command, 0, b"".join(frames), b"")

    monkeypatch.setattr(gate.subprocess, "run", _fake_run)

    stats = gate._frame_delta_stats(video_path, timeout_seconds=7.0)

    assert stats["ok"] is True
    assert stats["sampling_mode"] == "local_cadence_and_declared_transition_boundaries"
    assert stats["sampling_fps"] == 5.0
    assert stats["sample_interval_seconds"] == 0.2
    assert stats["sampled_frame_count"] == 226
    assert stats["local_delta_count"] == 225
    assert stats["max_delta"] == 4.0


def test_walkthrough_quality_gate_can_select_generated_reconstruction_candidate(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from scripts import propertyquarry_walkthrough_quality_gate as gate

    slug = "demo"
    bundle = tmp_path / slug
    generated_dir = bundle / "generated-reconstruction"
    generated_dir.mkdir(parents=True)
    (bundle / "stale-magicfit.mp4").write_bytes(b"not-a-real-video")
    (generated_dir / "generated-walkthrough.mp4").write_bytes(b"not-a-real-video")
    (generated_dir / "generated-walkthrough.quality.json").write_text(
        json.dumps(
            {
                "route_labels": ["floorplan overview", "source photo 01"],
                "covered_route_labels": ["floorplan overview", "source photo 01"],
            }
        ),
        encoding="utf-8",
    )
    (bundle / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "video_relpath": "stale-magicfit.mp4",
                "generated_reconstruction": {
                    "provider": "propertyquarry_generated_reconstruction",
                    "verified_provider_capture": False,
                    "walkthrough_video_relpath": "generated-reconstruction/generated-walkthrough.mp4",
                    "walkthrough_sidecar_relpath": "generated-reconstruction/generated-walkthrough.quality.json",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "_video_metadata", lambda _path, *, timeout_seconds=None: {"format": {"duration": "45"}})
    monkeypatch.setattr(
        gate,
        "_frame_delta_stats",
        lambda _path, *, timeout_seconds=None: {"ok": True, "sampled_frame_count": 20, "delta_count": 19, "max_delta": 12.0},
    )

    receipt = gate.build_walkthrough_quality_receipt(
        tour_root=str(tmp_path),
        demo_slug=slug,
        max_jump_delta=42.0,
        min_duration_seconds=30.0,
    )

    assert receipt["status"] == "pass"
    assert receipt["walkthrough_candidate"] == "generated_reconstruction"
    assert receipt["video_relpath"] == "generated-reconstruction/generated-walkthrough.mp4"


def test_walkthrough_quality_gate_prefers_service_generated_reconstruction_receipt_slug_and_candidate(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from scripts import propertyquarry_walkthrough_quality_gate as gate

    slug = "service-generated"
    bundle = tmp_path / slug
    generated_dir = bundle / "generated-reconstruction"
    generated_dir.mkdir(parents=True)
    (bundle / "published.mp4").write_bytes(b"published-video")
    (generated_dir / "generated-walkthrough.mp4").write_bytes(b"generated-video")
    (generated_dir / "generated-walkthrough.quality.json").write_text(
        json.dumps(
            {
                "route_labels": ["entry/hall", "living room", "kitchen", "bedroom"],
                "covered_route_labels": ["entry/hall", "living room", "kitchen", "bedroom"],
                "walkthrough_coverage_proof": {
                    "status": "pass",
                    "segments_expected": ["entry/hall", "living room", "kitchen", "bedroom"],
                    "segments_visited": ["entry/hall", "living room", "kitchen", "bedroom"],
                    "coverage_segments": [
                        {"segment": "entry/hall", "index": 1},
                        {"segment": "living room", "index": 2},
                        {"segment": "kitchen", "index": 3},
                        {"segment": "bedroom", "index": 4},
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    (bundle / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "video_relpath": "published.mp4",
                "walkthrough_coverage_proof": {
                    "status": "pass",
                    "segments_expected": ["published room"],
                    "segments_visited": ["published room"],
                    "coverage_segments": [{"segment": "published room", "start": 0, "end": 12}],
                },
                "generated_reconstruction": {
                    "provider": "propertyquarry_generated_reconstruction",
                    "verified_provider_capture": False,
                    "walkthrough_video_relpath": "generated-reconstruction/generated-walkthrough.mp4",
                    "walkthrough_sidecar_relpath": "generated-reconstruction/generated-walkthrough.quality.json",
                    "walkthrough_coverage_proof": {
                        "status": "pass",
                        "segments_expected": ["entry/hall", "living room", "kitchen", "bedroom"],
                        "segments_visited": ["entry/hall", "living room", "kitchen", "bedroom"],
                        "coverage_segments": [
                            {"segment": "entry/hall", "start": 0, "end": 10},
                            {"segment": "living room", "start": 10, "end": 22},
                            {"segment": "kitchen", "start": 22, "end": 34},
                            {"segment": "bedroom", "start": 34, "end": 45},
                        ],
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    service_receipt_path = tmp_path / "service-generated-reconstruction.json"
    service_receipt_path.write_text(
        json.dumps(
            {
                "status": "pass",
                "slug": slug,
                "details": {"slug": slug},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "_video_metadata", lambda _path, *, timeout_seconds=None: {"format": {"duration": "45"}})
    monkeypatch.setattr(
        gate,
        "_frame_delta_stats",
        lambda _path, *, timeout_seconds=None: {"ok": True, "sampled_frame_count": 20, "delta_count": 19, "max_delta": 12.0},
    )

    receipt = gate.build_walkthrough_quality_receipt(
        tour_root=str(tmp_path),
        demo_slug=gate.DEFAULT_DEMO_SLUG,
        max_jump_delta=42.0,
        min_duration_seconds=30.0,
        service_generated_reconstruction_receipt_path=str(service_receipt_path),
    )

    assert receipt["status"] == "pass"
    assert receipt["demo_slug"] == slug
    assert receipt["requested_demo_slug"] == gate.DEFAULT_DEMO_SLUG
    assert receipt["selection_source"] == "service_generated_reconstruction_receipt"
    assert receipt["service_generated_reconstruction_slug"] == slug
    assert receipt["walkthrough_candidate"] == "generated_reconstruction"
    assert receipt["video_relpath"] == "generated-reconstruction/generated-walkthrough.mp4"
    service_checks = {
        row["name"]: row["ok"]
        for row in receipt["checks"]
        if row["name"].startswith("service_generated_reconstruction")
        or row["name"] == "walkthrough_candidate_matches_service_generated_reconstruction"
    }
    assert service_checks == {
        "service_generated_reconstruction_receipt_present": True,
        "service_generated_reconstruction_bundle_selected": True,
        "walkthrough_candidate_matches_service_generated_reconstruction": True,
    }


def test_walkthrough_quality_gate_resolves_runtime_root_when_configured_root_misses_selected_slug(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from scripts import propertyquarry_walkthrough_quality_gate as gate

    configured_root = tmp_path / "configured"
    configured_root.mkdir()
    runtime_root = tmp_path / "runtime"
    slug = "service-generated-runtime-root"
    bundle = runtime_root / slug
    generated_dir = bundle / "generated-reconstruction"
    generated_dir.mkdir(parents=True)
    (generated_dir / "generated-walkthrough.mp4").write_bytes(b"generated-video")
    (generated_dir / "generated-walkthrough.quality.json").write_text(
        json.dumps(
            {
                "route_labels": ["entry/hall", "living room", "kitchen", "bedroom"],
                "covered_route_labels": ["entry/hall", "living room", "kitchen", "bedroom"],
            }
        ),
        encoding="utf-8",
    )
    (bundle / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "generated_reconstruction": {
                    "provider": "propertyquarry_generated_reconstruction",
                    "verified_provider_capture": False,
                    "walkthrough_video_relpath": "generated-reconstruction/generated-walkthrough.mp4",
                    "walkthrough_sidecar_relpath": "generated-reconstruction/generated-walkthrough.quality.json",
                        "walkthrough_coverage_proof": {
                            "status": "pass",
                            "segments_expected": ["entry/hall", "living room", "kitchen", "bedroom"],
                            "segments_visited": ["entry/hall", "living room", "kitchen", "bedroom"],
                            "coverage_segments": [
                                {"segment": "entry/hall", "index": 1},
                                {"segment": "living room", "index": 2},
                                {"segment": "kitchen", "index": 3},
                                {"segment": "bedroom", "index": 4},
                            ],
                        },
                    },
                }
            ),
        encoding="utf-8",
    )
    service_receipt_path = tmp_path / "service-generated-runtime-root.json"
    service_receipt_path.write_text(
        json.dumps(
            {
                "status": "pass",
                "slug": slug,
                "details": {"slug": slug},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PROPERTYQUARRY_RUNTIME_CONTAINER", "propertyquarry-api-candidate")
    monkeypatch.setattr(gate, "preferred_public_tour_root", lambda **_kwargs: runtime_root)
    monkeypatch.setattr(gate, "_video_metadata", lambda _path, *, timeout_seconds=None: {"format": {"duration": "45"}})
    monkeypatch.setattr(
        gate,
        "_frame_delta_stats",
        lambda _path, *, timeout_seconds=None: {"ok": True, "sampled_frame_count": 20, "delta_count": 19, "max_delta": 12.0},
    )

    receipt = gate.build_walkthrough_quality_receipt(
        tour_root=str(configured_root),
        demo_slug=gate.DEFAULT_DEMO_SLUG,
        max_jump_delta=42.0,
        min_duration_seconds=30.0,
        service_generated_reconstruction_receipt_path=str(service_receipt_path),
    )

    assert receipt["status"] == "pass"
    assert receipt["tour_root"] == str(runtime_root.resolve())
    assert receipt["demo_slug"] == slug
    assert receipt["walkthrough_candidate"] == "generated_reconstruction"


def test_walkthrough_quality_gate_keeps_explicit_root_authoritative(monkeypatch, tmp_path: Path) -> None:
    from scripts import propertyquarry_walkthrough_quality_gate as gate

    slug = "explicit-candidate-root"
    configured_root = tmp_path / "candidate"
    bundle = configured_root / slug
    bundle.mkdir(parents=True)
    (bundle / "tour.json").write_text(json.dumps({"slug": slug}), encoding="utf-8")
    monkeypatch.setenv("PROPERTYQUARRY_RUNTIME_CONTAINER", "propertyquarry-api")
    monkeypatch.setattr(
        gate,
        "preferred_public_tour_root",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("explicit root was replaced")),
    )

    resolved = gate._resolve_walkthrough_tour_root(str(configured_root), slug=slug)

    assert resolved == configured_root.resolve()


def test_walkthrough_quality_gate_passes_with_complete_coverage_and_continuity(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from scripts import propertyquarry_walkthrough_quality_gate as gate

    slug = "demo"
    bundle = tmp_path / slug
    bundle.mkdir()
    (bundle / "walkthrough.mp4").write_bytes(b"not-a-real-video")
    (bundle / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "video_relpath": "walkthrough.mp4",
                "walkthrough_coverage_proof": {
                    "status": "pass",
                    "rooms_expected": ["entry", "bathroom", "kitchen"],
                    "rooms_visited": ["entry", "bathroom", "kitchen"],
                    "room_segments": [
                        {"room": "entry", "start": 0, "end": 8},
                        {"room": "bathroom", "start": 8, "end": 18},
                        {"room": "kitchen", "start": 18, "end": 35},
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "_video_metadata", lambda _path, *, timeout_seconds=None: {"format": {"duration": "45"}})
    monkeypatch.setattr(
        gate,
        "_frame_delta_stats",
        lambda _path, *, timeout_seconds=None: {"ok": True, "sampled_frame_count": 20, "delta_count": 19, "max_delta": 12.0},
    )

    receipt = gate.build_walkthrough_quality_receipt(
        tour_root=str(tmp_path),
        demo_slug=slug,
        max_jump_delta=42.0,
        min_duration_seconds=30.0,
    )

    assert receipt["status"] == "pass"
    assert all(row["ok"] for row in receipt["checks"])


def test_walkthrough_quality_gate_reports_frame_sampling_timeout(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from scripts import propertyquarry_walkthrough_quality_gate as gate

    slug = "demo"
    bundle = tmp_path / slug
    bundle.mkdir()
    walkthrough_path = bundle / "walkthrough.mp4"
    walkthrough_path.write_bytes(b"not-a-real-video")
    (bundle / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "video_relpath": "walkthrough.mp4",
                "walkthrough_coverage_proof": {
                    "status": "pass",
                    "rooms_expected": ["entry"],
                    "rooms_visited": ["entry"],
                    "room_segments": [{"room": "entry", "start": 0, "end": 8}],
                },
            }
        ),
        encoding="utf-8",
    )

    original_run = gate.subprocess.run

    def _fake_run(command, *args, **kwargs):
        if command and command[0] == "ffmpeg":
            raise subprocess.TimeoutExpired(command, timeout=kwargs.get("timeout"))
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(gate.subprocess, "run", _fake_run)
    monkeypatch.setattr(gate, "_video_metadata", lambda _path, *, timeout_seconds=None: {"format": {"duration": "45"}})

    receipt = gate.build_walkthrough_quality_receipt(
        tour_root=str(tmp_path),
        demo_slug=slug,
        max_jump_delta=42.0,
        min_duration_seconds=30.0,
        ffprobe_timeout_seconds=20.0,
        frame_sample_timeout_seconds=7.0,
    )

    assert receipt["status"] == "fail"
    frame_checks = {row["name"]: row for row in receipt["checks"]}
    assert frame_checks["walkthrough_frame_samples_available"]["ok"] is False
    assert frame_checks["walkthrough_frame_samples_available"]["frame_delta_stats"]["error"] == "ffmpeg_frame_sampling_timeout:7s"
    assert frame_checks["walkthrough_frame_jump_limit"]["ok"] is False


def test_map_preview_flagship_gate_rejects_harsh_raw_overlay(tmp_path: Path) -> None:
    from scripts import propertyquarry_map_preview_flagship_gate as gate

    image = Image.new("RGB", (640, 368), (238, 232, 222))
    draw = ImageDraw.Draw(image, "RGBA")
    for index in range(0, 640, 24):
        draw.line([(index, 0), (index + 180, 368)], fill=(185, 180, 172, 180), width=5)
    harsh = [(70, 52), (570, 36), (606, 290), (104, 326)]
    draw.polygon(harsh, fill=(215, 22, 28, 170))
    draw.line(harsh + [harsh[0]], fill=(112, 18, 24, 255), width=8)
    path = tmp_path / "harsh.png"
    image.save(path, format="PNG", compress_level=0)

    receipt = gate.build_map_preview_flagship_receipt(
        base_url="http://localhost",
        host_header="",
        api_token="",
        principal_id="",
        image_urls=[path.as_uri()],
        discover_routes=[],
        timeout_seconds=1.0,
        settle_seconds=0.0,
        min_preview_count=1,
    )

    assert receipt["status"] == "fail"
    failed_names = {
        check["name"]
        for result in receipt["preview_results"]
        for check in result["checks"]
        if not check["ok"]
    }
    assert "red_overlay_not_aggressive" in failed_names
    assert "border_noise_not_heavy" in failed_names


def test_map_preview_flagship_gate_rejects_washed_out_map_backdrop(tmp_path: Path) -> None:
    from scripts import propertyquarry_map_preview_flagship_gate as gate

    image = Image.new("RGB", (640, 368), (246, 244, 239))
    draw = ImageDraw.Draw(image, "RGBA")
    for index in range(-80, 720, 84):
        draw.line([(index, 0), (index + 210, 368)], fill=(212, 210, 204, 84), width=5)
    selected = [(190, 90), (455, 72), (524, 184), (450, 292), (216, 288), (128, 190)]
    draw.polygon(selected, fill=(218, 150, 150, 58))
    draw.line(selected + [selected[0]], fill=(132, 30, 36, 118), width=2)
    path = tmp_path / "washed-out.png"
    image.save(path, format="PNG", compress_level=0)

    receipt = gate.build_map_preview_flagship_receipt(
        base_url="http://localhost",
        host_header="",
        api_token="",
        principal_id="",
        image_urls=[path.as_uri()],
        discover_routes=[],
        timeout_seconds=1.0,
        settle_seconds=0.0,
        min_preview_count=1,
    )

    assert receipt["status"] == "fail"
    failed_names = {
        check["name"]
        for result in receipt["preview_results"]
        for check in result["checks"]
        if not check["ok"]
    }
    assert "map_backdrop_visible" in failed_names


def test_map_preview_flagship_gate_accepts_calm_premium_thumbnail(tmp_path: Path) -> None:
    from scripts import propertyquarry_map_preview_flagship_gate as gate

    image = Image.new("RGB", (640, 368), (226, 222, 214))
    draw = ImageDraw.Draw(image, "RGBA")
    draw.polygon([(0, 24), (210, 0), (640, 80), (640, 128), (260, 104), (0, 120)], fill=(178, 204, 168, 150))
    draw.polygon([(0, 280), (190, 248), (412, 286), (640, 264), (640, 368), (0, 368)], fill=(164, 194, 208, 150))
    for index in range(-200, 760, 58):
        draw.line([(index, 0), (index + 250, 368)], fill=(162, 154, 143, 180), width=7)
        draw.line([(index, 0), (index + 250, 368)], fill=(255, 253, 247, 130), width=2)
    for index in range(-100, 760, 78):
        draw.line([(index, 368), (index + 190, 0)], fill=(178, 170, 158, 150), width=5)
    for y in range(34, 370, 42):
        draw.line([(0, y), (640, y - 28)], fill=(164, 156, 145, 170), width=6)
        draw.line([(0, y), (640, y - 28)], fill=(255, 253, 247, 105), width=2)
    selected = [(190, 90), (455, 72), (524, 184), (450, 292), (216, 288), (128, 190)]
    draw.polygon(selected, fill=(218, 150, 150, 70))
    draw.line(selected + [selected[0]], fill=(255, 250, 242, 155), width=4)
    draw.line(selected + [selected[0]], fill=(132, 30, 36, 126), width=2)
    path = tmp_path / "calm.png"
    image.save(path, format="PNG", compress_level=0)

    receipt = gate.build_map_preview_flagship_receipt(
        base_url="http://localhost",
        host_header="",
        api_token="",
        principal_id="",
        image_urls=[path.as_uri()],
        discover_routes=[],
        timeout_seconds=1.0,
        settle_seconds=0.0,
        min_preview_count=1,
    )

    assert receipt["status"] == "pass"
    assert receipt["preview_results"][0]["metrics"]["strong_red_ratio"] < 0.20
    assert receipt["preview_results"][0]["metrics"]["stddev_mean"] >= 18.0


def test_map_preview_flagship_gate_uses_canonical_renderer_when_live_state_has_no_preview(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from scripts import propertyquarry_map_preview_flagship_gate as gate

    image = Image.new("RGB", (640, 368), (226, 222, 214))
    draw = ImageDraw.Draw(image, "RGBA")
    draw.polygon([(0, 24), (210, 0), (640, 80), (640, 128), (260, 104), (0, 120)], fill=(178, 204, 168, 150))
    draw.polygon([(0, 280), (190, 248), (412, 286), (640, 264), (640, 368), (0, 368)], fill=(164, 194, 208, 150))
    for index in range(-200, 760, 58):
        draw.line([(index, 0), (index + 250, 368)], fill=(162, 154, 143, 180), width=7)
        draw.line([(index, 0), (index + 250, 368)], fill=(255, 253, 247, 130), width=2)
    for y in range(34, 370, 42):
        draw.line([(0, y), (640, y - 28)], fill=(164, 156, 145, 170), width=6)
        draw.line([(0, y), (640, y - 28)], fill=(255, 253, 247, 105), width=2)
    selected = [(190, 90), (455, 72), (524, 184), (450, 292), (216, 288), (128, 190)]
    draw.polygon(selected, fill=(218, 150, 150, 70))
    draw.line(selected + [selected[0]], fill=(255, 250, 242, 155), width=4)
    draw.line(selected + [selected[0]], fill=(132, 30, 36, 126), width=2)
    path = tmp_path / "canonical.png"
    image.save(path, format="PNG", compress_level=0)

    monkeypatch.setattr(gate, "_discover_preview_urls", lambda **_kwargs: [])
    monkeypatch.setattr(
        gate,
        "_canonical_renderer_preview_sources",
        lambda: [
            {
                "source": "canonical_renderer",
                "label": "vienna_radius_overlay",
                "status": "ready",
                "query": "1020 Vienna",
                "url": path.as_uri(),
            }
        ],
    )

    receipt = gate.build_map_preview_flagship_receipt(
        base_url="http://localhost",
        host_header="",
        api_token="",
        principal_id="",
        image_urls=[],
        discover_routes=["/app/search"],
        timeout_seconds=1.0,
        settle_seconds=0.0,
        min_preview_count=1,
    )

    assert receipt["status"] == "pass"
    assert receipt["canonical_fallback"] is True
    assert receipt["preview_sources"] == [
        {
            "url": path.as_uri(),
            "source": "canonical_renderer",
            "label": "vienna_radius_overlay",
            "query": "1020 Vienna",
        }
    ]
    assert receipt["preview_results"][0]["source"] == "canonical_renderer"


def test_deploy_and_release_scripts_wire_3d_walkthrough_and_map_preview_as_exit_gates() -> None:
    deploy = (ROOT / "scripts" / "deploy_propertyquarry.sh").read_text(encoding="utf-8")
    release = (ROOT / "scripts" / "property_release_gates.sh").read_text(encoding="utf-8")

    assert 'PROPERTYQUARRY_EXTERNAL_CONTROLLER_PATH="/usr/libexec/propertyquarry-release-control/propertyquarry-deploy-controller"' in deploy
    assert 'PROPERTYQUARRY_EXTERNAL_CONTROLLER_MANIFEST="/etc/propertyquarry/release-control/external-deploy-controller.v1.json"' in deploy
    assert "--controller-owns-all-privileged-actions" in deploy
    assert "--require-controller-self-attestation" in deploy
    assert "--forbid-candidate-output-authority" in deploy
    for candidate_owned_gate in (
        "scripts/propertyquarry_3d_browser_gate.py",
        "scripts/propertyquarry_walkthrough_quality_gate.py",
        "scripts/propertyquarry_walkthrough_provider_proof_gate.py",
        "scripts/propertyquarry_map_preview_flagship_gate.py",
    ):
        assert candidate_owned_gate not in deploy
    assert "scripts/propertyquarry_3d_browser_gate.py" in release
    assert "scripts/propertyquarry_walkthrough_quality_gate.py" in release
    assert "scripts/propertyquarry_walkthrough_provider_proof_gate.py" in release
    assert 'if ! PYTHONPATH=ea timeout "${walkthrough_quality_process_timeout_seconds}" "${PYTHON_BIN}" scripts/propertyquarry_walkthrough_quality_gate.py' in release
    assert "--service-generated-reconstruction-receipt _completion/tours/property-service-generated-reconstruction-release-gate.json" in release
    assert "--walkthrough-provider-proof-receipt _completion/smoke/property-live-walkthrough-provider-proof-release-gate.json" in release
    assert "scripts/propertyquarry_map_preview_flagship_gate.py" in release
    assert "scripts/property_runtime_reconstruction_smoke.py" in release
    assert "--require-glb" in release
    assert "--map-preview-flagship-receipt _completion/smoke/property-live-map-preview-flagship-release-gate.json" in release
    assert "--browser-3d-gate-receipt _completion/smoke/property-live-3d-browser-gate-release-gate.json" in release
    assert "--runtime-reconstruction-receipt _completion/tours/property-runtime-reconstruction-release-gate.json" in release
    assert "--walkthrough-quality-receipt _completion/smoke/property-live-walkthrough-quality-release-gate.json" in release
    assert "_completion/tours/property-runtime-reconstruction-release-gate.json" in release
    assert "PROPERTYQUARRY_GOLD_NOTIFICATION_ENABLED" in release
    assert "PROPERTYQUARRY_GOLD_NOTIFICATION_ENABLED_not_set" in release
    assert "PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_REFRESH_NOTIFICATION_ENABLED" in release
    assert "PROPERTYQUARRY_SCENE_VIDEO_PROVIDER_REFRESH_NOTIFICATION_ENABLED_not_set" in release
