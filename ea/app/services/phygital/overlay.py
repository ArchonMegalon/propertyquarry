from __future__ import annotations

from typing import Any

from app.services.phygital.adapter import candidate_floorplan_url, get_phygital_adapter


def apply_phygital_floorplan_video_to_top_result(
    rows: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Attach a Phygital 3D floorplan video to rank-1 only.

    Fail-closed: ranking is unchanged unless a ready video or poster URL exists.
    Dry-run and disabled modes annotate the candidate without replacing media.
    Search never posts Kling; GENERATE only gates generate_from_floorplan().
    """
    if not isinstance(rows, list) or not rows:
        return list(rows or [])
    updated = [dict(row) if isinstance(row, dict) else row for row in rows]
    top = updated[0]
    if not isinstance(top, dict):
        return updated
    floorplan_url = candidate_floorplan_url(top)
    try:
        artifact = get_phygital_adapter().lookup_or_generate(
            candidate=top,
            floorplan_url=floorplan_url,
        )
    except Exception as exc:
        top["phygital_status"] = "error"
        top["phygital_reason"] = f"phygital_overlay:{type(exc).__name__}"
        updated[0] = top
        return updated
    top["phygital_status"] = artifact.status
    top["phygital_reason"] = artifact.reason
    if artifact.artifact_key:
        top["phygital_artifact_key"] = artifact.artifact_key
    if artifact.task_id:
        top["phygital_task_id"] = artifact.task_id
    if artifact.status != "ready":
        updated[0] = top
        return updated
    if artifact.video_url:
        top["phygital_video_url"] = artifact.video_url
        top["flythrough_url"] = artifact.video_url
        top["flythrough_status"] = "ready"
        scene = dict(top.get("diorama_scene") or {}) if isinstance(top.get("diorama_scene"), dict) else {}
        scene["video_url"] = artifact.video_url
        if artifact.poster_url:
            scene["image_url"] = artifact.poster_url
            scene["preview_image_url"] = artifact.poster_url
        top["diorama_scene"] = scene
        if not artifact.poster_url:
            top["thumbnail_url"] = artifact.video_url
            top["preview_image_url"] = artifact.video_url
            top["diorama_preview_url"] = artifact.video_url
            top["image_url"] = artifact.video_url
    if artifact.poster_url:
        top["phygital_poster_url"] = artifact.poster_url
        top["thumbnail_url"] = artifact.poster_url
        top["preview_image_url"] = artifact.poster_url
        top["diorama_preview_url"] = artifact.poster_url
        top["image_url"] = artifact.poster_url
    updated[0] = top
    return updated
