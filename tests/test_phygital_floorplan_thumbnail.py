from __future__ import annotations

import pytest

from app.services.phygital.adapter import (
    EnvPhygitalAdapter,
    PhygitalFloorplanVideo,
    build_kling_task_payload,
    candidate_floorplan_url,
    extract_kling_video_file_id,
    extract_queue_video_file_id,
    generation_enabled,
    kling_task_schema_from_workspace,
    reset_phygital_adapter,
    select_property_project,
)
from app.services.phygital.overlay import apply_phygital_floorplan_video_to_top_result


def _candidate(*, ref: str, score: float, floorplan: str = "") -> dict[str, object]:
    facts: dict[str, object] = {"postal_name": "1200 Wien"}
    if floorplan:
        facts["floorplan_url"] = floorplan
    return {
        "candidate_ref": ref,
        "title": f"Home {ref}",
        "property_url": f"https://example.test/{ref}",
        "source_label": "Willhaben",
        "fit_score": score,
        "ranking_score": score,
        "property_facts": facts,
        "preview_image_url": f"https://cdn.example.test/{ref}.jpg",
    }


def test_phygital_disabled_does_not_replace_top_thumbnail(monkeypatch) -> None:
    monkeypatch.delenv("PROPERTYQUARRY_PHYGITAL_ENABLED", raising=False)
    reset_phygital_adapter()
    rows = apply_phygital_floorplan_video_to_top_result(
        [_candidate(ref="top", score=90, floorplan="https://cdn.example.test/plan.png")]
    )
    assert rows[0]["phygital_status"] == "disabled"
    assert rows[0]["preview_image_url"] == "https://cdn.example.test/top.jpg"
    assert "phygital_video_url" not in rows[0]


def test_phygital_dry_run_annotates_without_replacing_media(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_PHYGITAL_ENABLED", "1")
    monkeypatch.setenv("PROPERTYQUARRY_PHYGITAL_MODE", "dry_run")
    reset_phygital_adapter()
    rows = apply_phygital_floorplan_video_to_top_result(
        [
            _candidate(ref="top", score=90, floorplan="https://cdn.example.test/plan.png"),
            _candidate(ref="second", score=80),
        ]
    )
    assert rows[0]["phygital_status"] == "dry_run"
    assert rows[0]["preview_image_url"] == "https://cdn.example.test/top.jpg"
    assert rows[1]["preview_image_url"] == "https://cdn.example.test/second.jpg"
    assert "phygital_status" not in rows[1]


def test_phygital_ready_video_replaces_top_thumbnail_only(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_PHYGITAL_ENABLED", "1")
    monkeypatch.setenv("PROPERTYQUARRY_PHYGITAL_MODE", "live")
    reset_phygital_adapter()

    def _ready(self, *, candidate, floorplan_url=""):
        return PhygitalFloorplanVideo(
            status="ready",
            artifact_key="phygital-floorplan-test",
            video_url="https://cdn.phygital.test/walk.mp4",
            poster_url="https://cdn.phygital.test/poster.webp",
            reason="phygital_ready",
        )

    monkeypatch.setattr(EnvPhygitalAdapter, "lookup_or_generate", _ready)
    rows = apply_phygital_floorplan_video_to_top_result(
        [
            _candidate(ref="top", score=90, floorplan="https://cdn.example.test/plan.png"),
            _candidate(ref="second", score=80),
        ]
    )
    assert rows[0]["thumbnail_url"] == "https://cdn.phygital.test/poster.webp"
    assert rows[0]["diorama_preview_url"] == "https://cdn.phygital.test/poster.webp"
    assert rows[0]["phygital_video_url"] == "https://cdn.phygital.test/walk.mp4"
    assert rows[0]["flythrough_url"] == "https://cdn.phygital.test/walk.mp4"
    assert rows[1]["preview_image_url"] == "https://cdn.example.test/second.jpg"
    assert "phygital_video_url" not in rows[1]


def test_phygital_ready_video_without_poster_still_sets_thumbnail(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_PHYGITAL_ENABLED", "1")
    monkeypatch.setenv("PROPERTYQUARRY_PHYGITAL_MODE", "live")
    reset_phygital_adapter()

    def _ready(self, *, candidate, floorplan_url=""):
        return PhygitalFloorplanVideo(
            status="ready",
            artifact_key="phygital-floorplan-test",
            video_url="https://cdn.phygital.test/walk.mp4",
            poster_url="",
            reason="phygital_ready",
        )

    monkeypatch.setattr(EnvPhygitalAdapter, "lookup_or_generate", _ready)
    rows = apply_phygital_floorplan_video_to_top_result(
        [_candidate(ref="top", score=90, floorplan="https://cdn.example.test/plan.png")]
    )
    assert rows[0]["phygital_video_url"] == "https://cdn.phygital.test/walk.mp4"
    assert rows[0]["diorama_preview_url"] == "https://cdn.phygital.test/walk.mp4"
    assert rows[0]["thumbnail_url"] == "https://cdn.phygital.test/walk.mp4"


def test_candidate_floorplan_url_reads_facts() -> None:
    row = _candidate(ref="top", score=90, floorplan="https://cdn.example.test/plan.png")
    assert candidate_floorplan_url(row).endswith("plan.png")


def test_select_property_project_prefers_named_video() -> None:
    items = [
        {
            "id": 1,
            "name": "learn",
            "preview": {"url": "https://cdn.example.test/learn.mp4", "mime_type": "video/mp4"},
        },
        {
            "id": 129912,
            "name": "Property",
            "preview": {"url": "https://cdn.example.test/property.mp4", "mime_type": "video/mp4"},
        },
    ]
    chosen = select_property_project(items)
    assert chosen is not None
    assert chosen["id"] == 129912
    assert chosen["preview"]["url"].endswith("property.mp4")


def test_extract_kling_video_file_id_from_workspace_graph() -> None:
    payload = {
        "workspace": {
            "nodes": [
                {
                    "name": "Kling",
                    "globalId": "kling-3-0",
                    "outputSocketGroup": [
                        {
                            "name": "out_video",
                            "type": "video",
                            "value": [{"fileId": 23594402}],
                        }
                    ],
                }
            ]
        }
    }
    assert extract_kling_video_file_id(payload) == "23594402"


@pytest.mark.skipif(__import__("sys").platform.startswith("win"), reason="ProductService imports fcntl")
def test_ranked_candidates_call_phygital_overlay(monkeypatch) -> None:
    from app.product.service import _property_search_ranked_candidates_from_sources

    monkeypatch.setenv("PROPERTYQUARRY_PHYGITAL_ENABLED", "1")
    monkeypatch.setenv("PROPERTYQUARRY_PHYGITAL_MODE", "dry_run")
    reset_phygital_adapter()
    sources = [
        {
            "source_label": "Willhaben",
            "top_candidates": [
                _candidate(ref="beta", score=40, floorplan="https://cdn.example.test/plan.png"),
                _candidate(ref="alpha", score=95, floorplan="https://cdn.example.test/plan.png"),
            ],
        }
    ]
    ranked = _property_search_ranked_candidates_from_sources(sources)
    assert ranked[0]["candidate_ref"] == "alpha"
    assert ranked[0]["rank"] == 1
    assert ranked[0]["phygital_status"] == "dry_run"


def test_generation_stays_off_without_explicit_flag(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_PHYGITAL_ENABLED", "1")
    monkeypatch.setenv("PROPERTYQUARRY_PHYGITAL_MODE", "live")
    monkeypatch.delenv("PROPERTYQUARRY_PHYGITAL_GENERATE", raising=False)
    assert generation_enabled() is False


def test_generation_requires_live_and_flag(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_PHYGITAL_ENABLED", "1")
    monkeypatch.setenv("PROPERTYQUARRY_PHYGITAL_MODE", "dry_run")
    monkeypatch.setenv("PROPERTYQUARRY_PHYGITAL_GENERATE", "1")
    assert generation_enabled() is False
    monkeypatch.setenv("PROPERTYQUARRY_PHYGITAL_MODE", "live")
    assert generation_enabled() is True


def test_build_kling_task_payload_swaps_init_img() -> None:
    schema = {
        "id": 74,
        "workspace_id": 129912,
        "inputs": [
            {"name": "text_prompt", "type": "text", "value": "orbit", "meta": {}},
            {"name": "init_img", "type": "image", "value": 23594232, "meta": {"dimensions": {"width": 1200}}},
            {"name": "image_tail", "type": "image", "value": 23594295, "meta": {}},
        ],
        "params": [{"name": "model_name", "type": "enum", "value": "kling_v3"}],
        "outputs": [{"name": "out_video", "type": "video", "value": ""}],
    }
    payload = build_kling_task_payload(schema, floorplan_file_id=23796125, workspace_id="129912")
    assert payload["id"] == 74
    assert payload["workspace_id"] == 129912
    init_img = next(item for item in payload["inputs"] if item["name"] == "init_img")
    assert init_img["value"] == 23796125
    assert init_img["isModified"] is True
    assert schema["inputs"][1]["value"] == 23594232


def test_extract_queue_video_file_id() -> None:
    payload = {
        "status": "done",
        "position": -1,
        "outputs": [
            {
                "name": "out_video",
                "type": "array",
                "value": "s3://aws/phygital-plus-prod/results/user/413282/23594402.mp4",
                "id": [23594402],
            }
        ],
    }
    assert extract_queue_video_file_id(payload) == "23594402"


def test_kling_schema_from_workspace() -> None:
    payload = {
        "workspace": {
            "nodes": [
                {
                    "name": "Kling",
                    "globalId": "Phygital Creator/phygc-rnd-api-kling",
                    "meta": {"taskSchema": {"id": 74, "inputs": [], "params": [], "outputs": []}},
                }
            ]
        }
    }
    schema = kling_task_schema_from_workspace(payload)
    assert schema["id"] == 74


def test_live_without_generate_does_not_start_kling(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_PHYGITAL_ENABLED", "1")
    monkeypatch.setenv("PROPERTYQUARRY_PHYGITAL_MODE", "live")
    monkeypatch.delenv("PROPERTYQUARRY_PHYGITAL_GENERATE", raising=False)
    reset_phygital_adapter()
    calls: list[str] = []

    def _reuse(self, *, artifact_key: str):
        calls.append("reuse")
        return PhygitalFloorplanVideo(status="ready", artifact_key=artifact_key, video_url="https://cdn.phygital.test/walk.mp4")

    def _generate(self, *, floorplan_url: str, artifact_key: str = "", wait_seconds=None):
        calls.append("generate")
        raise AssertionError("generate must not run without GENERATE=1")

    monkeypatch.setattr(EnvPhygitalAdapter, "_reuse_property_preview", _reuse)
    monkeypatch.setattr(EnvPhygitalAdapter, "generate_from_floorplan", _generate)
    rows = apply_phygital_floorplan_video_to_top_result(
        [_candidate(ref="top", score=90, floorplan="https://cdn.example.test/plan.png")]
    )
    assert calls == ["reuse"]
    assert rows[0]["phygital_video_url"] == "https://cdn.phygital.test/walk.mp4"


class _FakePhygitalSession:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str]] = []
        self.started: list[dict] = []

    def list_projects(self):
        return [
            {
                "id": 129912,
                "name": "Property",
                "preview": {"url": "https://cdn.phygital.test/property.mp4", "mime_type": "video/mp4"},
            }
        ]

    def get_workspace(self, project_id: str):
        assert str(project_id) == "129912"
        return {
            "workspace": {
                "nodes": [
                    {
                        "name": "Kling",
                        "globalId": "Phygital Creator/phygc-rnd-api-kling",
                        "meta": {
                            "taskSchema": {
                                "id": 74,
                                "inputs": [
                                    {"name": "text_prompt", "type": "text", "value": "orbit", "meta": {}},
                                    {"name": "init_img", "type": "image", "value": 23594232, "meta": {}},
                                ],
                                "params": [{"name": "model_name", "type": "enum", "value": "kling_v3"}],
                                "outputs": [{"name": "out_video", "type": "video", "value": ""}],
                            }
                        },
                    }
                ]
            }
        }

    def fetch_bytes(self, url: str):
        return b"\x89PNG\r\n", "plan.png", "image/png"

    def upload_fileobject(self, blob, *, filename, content_type, workspace_id=""):
        self.uploads.append((filename, str(workspace_id)))
        assert blob
        return "23796125"

    def start_kling_task(self, payload):
        self.started.append(payload)
        init_img = next(item for item in payload["inputs"] if item["name"] == "init_img")
        assert init_img["value"] == 23796125
        assert payload["id"] == 74
        return {"task_id": 10009999, "http_status": 200}

    def poll_task(self, task_id, *, timeout_seconds=0):
        assert str(task_id) == "10009999"
        return {
            "status": "done",
            "position": -1,
            "outputs": [{"name": "out_video", "id": [23594402], "value": "s3://x/23594402.mp4"}],
        }

    def download_link(self, file_id):
        assert str(file_id) == "23594402"
        return "https://cdn.phygital.test/generated.mp4"


def test_generate_from_floorplan_disabled_without_flag(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_PHYGITAL_ENABLED", "1")
    monkeypatch.setenv("PROPERTYQUARRY_PHYGITAL_MODE", "live")
    monkeypatch.delenv("PROPERTYQUARRY_PHYGITAL_GENERATE", raising=False)
    reset_phygital_adapter()
    artifact = EnvPhygitalAdapter().generate_from_floorplan(
        floorplan_url="https://cdn.example.test/plan.png"
    )
    assert artifact.status == "disabled"
    assert artifact.reason == "phygital_generate_disabled"


def test_generate_from_floorplan_uploads_starts_and_polls(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_PHYGITAL_ENABLED", "1")
    monkeypatch.setenv("PROPERTYQUARRY_PHYGITAL_MODE", "live")
    monkeypatch.setenv("PROPERTYQUARRY_PHYGITAL_GENERATE", "1")
    reset_phygital_adapter()
    adapter = EnvPhygitalAdapter()
    fake = _FakePhygitalSession()
    monkeypatch.setattr(adapter, "_ensure_session", lambda: fake)
    artifact = adapter.generate_from_floorplan(
        floorplan_url="https://cdn.example.test/plan.png",
        wait_seconds=5,
    )
    assert fake.uploads == [("plan.png", "129912")]
    assert len(fake.started) == 1
    assert artifact.status == "ready"
    assert artifact.video_url == "https://cdn.phygital.test/generated.mp4"
    assert artifact.task_id == "10009999"
    assert artifact.reason == "phygital_generated"


def test_generate_from_floorplan_returns_pending_without_wait(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_PHYGITAL_ENABLED", "1")
    monkeypatch.setenv("PROPERTYQUARRY_PHYGITAL_MODE", "live")
    monkeypatch.setenv("PROPERTYQUARRY_PHYGITAL_GENERATE", "1")
    monkeypatch.setenv("PROPERTYQUARRY_PHYGITAL_GENERATE_WAIT_SECONDS", "0")
    reset_phygital_adapter()
    adapter = EnvPhygitalAdapter()
    fake = _FakePhygitalSession()
    monkeypatch.setattr(adapter, "_ensure_session", lambda: fake)
    artifact = adapter.generate_from_floorplan(
        floorplan_url="https://cdn.example.test/plan.png"
    )
    assert artifact.status == "pending"
    assert artifact.reason == "phygital_task_started"
    assert artifact.task_id == "10009999"


def test_overlay_lookup_exception_keeps_ranked_media(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_PHYGITAL_ENABLED", "1")
    monkeypatch.setenv("PROPERTYQUARRY_PHYGITAL_MODE", "live")
    reset_phygital_adapter()

    def _boom(self, *, candidate, floorplan_url=""):
        raise RuntimeError("session exploded")

    monkeypatch.setattr(EnvPhygitalAdapter, "lookup_or_generate", _boom)
    rows = apply_phygital_floorplan_video_to_top_result(
        [
            _candidate(ref="top", score=90, floorplan="https://cdn.example.test/plan.png"),
            _candidate(ref="second", score=80),
        ]
    )
    assert rows[0]["phygital_status"] == "error"
    assert rows[0]["phygital_reason"] == "phygital_overlay:RuntimeError"
    assert rows[0]["preview_image_url"] == "https://cdn.example.test/top.jpg"
    assert "phygital_video_url" not in rows[0]
    assert "phygital_status" not in rows[1]


def test_overlay_generate_flag_still_reuses_not_kling(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_PHYGITAL_ENABLED", "1")
    monkeypatch.setenv("PROPERTYQUARRY_PHYGITAL_MODE", "live")
    monkeypatch.setenv("PROPERTYQUARRY_PHYGITAL_GENERATE", "1")
    reset_phygital_adapter()
    calls: list[str] = []

    def _reuse(self, *, artifact_key: str):
        calls.append("reuse")
        return PhygitalFloorplanVideo(
            status="ready",
            artifact_key=artifact_key,
            video_url="https://cdn.phygital.test/walk.mp4",
            reason="phygital_ready",
        )

    def _generate(self, *, floorplan_url: str, artifact_key: str = "", wait_seconds=None):
        calls.append("generate")
        raise AssertionError("search overlay must not post Kling")

    monkeypatch.setattr(EnvPhygitalAdapter, "_reuse_property_preview", _reuse)
    monkeypatch.setattr(EnvPhygitalAdapter, "generate_from_floorplan", _generate)
    rows = apply_phygital_floorplan_video_to_top_result(
        [
            _candidate(ref="top", score=90, floorplan="https://cdn.example.test/plan.png"),
            _candidate(ref="second", score=80),
        ]
    )
    assert calls == ["reuse"]
    assert rows[0]["phygital_video_url"] == "https://cdn.phygital.test/walk.mp4"
    assert rows[0]["preview_image_url"] == "https://cdn.phygital.test/walk.mp4" or rows[0]["phygital_status"] == "ready"
    assert "phygital_status" not in rows[1]
