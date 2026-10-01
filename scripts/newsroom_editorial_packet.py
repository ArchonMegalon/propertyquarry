#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EA_PYTHON_ROOT = ROOT / "ea"
if str(EA_PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(EA_PYTHON_ROOT))

from app.yaml_inputs import load_yaml_dict

DOCS_ROOT = ROOT / "docs" / "black_ledger_newsroom"
EPISODE_PATH = DOCS_ROOT / "SAMPLE_TURN1_EPISODE.yaml"
ANCHOR_PATH = DOCS_ROOT / "ANCHOR_BIBLE.yaml"
MEDIA_FACTORY_PATH = DOCS_ROOT / "MEDIA_FACTORY_RUNTIME_CONTRACTS.yaml"
OUTPUT_PATH = ROOT / ".codex-studio" / "published" / "NEWSROOM_EDITORIAL_PACKET.generated.json"

HOST_PROMPTS = {
    "mara_voss": "Photorealistic cyberpunk newsroom broadcast with a believable elf anchor, natural skin detail, subtle head movement, realistic mouth movement, professional desk posture, and no watermark.",
    "brack_kade": "Photorealistic cyberpunk field report with a believable orc correspondent, realistic skin and tusks, subtle body movement, controlled delivery, and no caricature or watermark.",
}

BROLL_LIBRARY: dict[str, dict[str, Any]] = {
    "geoscape_turn1": {
        "scene_type": "geoscape",
        "duration_seconds": 6,
        "prompt": "High-end cinematic geoscape insert with faction pressure pulses, route arcs, heat bars, and source receipt markers.",
        "public_safety_notes": "No private map overlays, no false product claims, no licensed real-world map texture.",
    },
    "rain_city": {
        "scene_type": "street_camera",
        "duration_seconds": 5,
        "prompt": "Photorealistic rainy cyberpunk city block at night with patrol lights, puddle reflections, and restrained street movement.",
        "public_safety_notes": "Atmosphere only; no private player identities or readable copyrighted text.",
    },
    "facility_exterior": {
        "scene_type": "facility_exterior",
        "duration_seconds": 6,
        "prompt": "Photorealistic public-safe reconstruction of a research facility exterior at night during a security incident.",
        "public_safety_notes": "No logos, no floorplans, no private GM map details.",
    },
    "matrix_trace_room": {
        "scene_type": "matrix_trace",
        "duration_seconds": 6,
        "prompt": "Photorealistic cyberpunk operations room with a decker tracing a signal across translucent screens.",
        "public_safety_notes": "Abstract node graphs only; no sourcebook text or private campaign topology.",
    },
    "newsroom_globe_close": {
        "scene_type": "dossier_graphic",
        "duration_seconds": 4,
        "prompt": "Close cinematic newsroom globe insert with controlled amber alert bars and a final receipt-backed signoff composition.",
        "public_safety_notes": "Editorial graphic only; no hidden evidence revealed.",
    },
}


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _summary(segments: list[dict[str, Any]]) -> str:
    headlines = [str(segment.get("headline") or "").strip() for segment in segments if str(segment.get("headline") or "").strip()]
    return " ".join(headlines[:3]).strip()


def _anchor_index(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = payload.get("anchors") or []
    return {str(row.get("id") or "").strip(): dict(row) for row in rows if isinstance(row, dict)}


def _build_broll_packet(cue_id: str) -> dict[str, Any]:
    row = dict(BROLL_LIBRARY.get(cue_id) or {})
    return {
        "cue_id": cue_id,
        "duration_seconds": int(row.get("duration_seconds") or 5),
        "scene_type": str(row.get("scene_type") or "editorial_graphic"),
        "prompt": str(row.get("prompt") or f"Photorealistic public-safe newsroom insert for {cue_id}."),
        "public_safety_notes": str(row.get("public_safety_notes") or "Public-safe reconstruction only."),
        "render_status": "pending",
    }


def build_payload() -> dict[str, Any]:
    episode_payload = load_yaml_dict(EPISODE_PATH)
    anchor_payload = load_yaml_dict(ANCHOR_PATH)
    media_factory_payload = load_yaml_dict(MEDIA_FACTORY_PATH)

    episode = dict(episode_payload.get("episode") or {})
    segments = [dict(row) for row in episode.get("segments") or [] if isinstance(row, dict)]
    anchors = _anchor_index(anchor_payload)
    anchor_id = str(episode.get("anchor_id") or "").strip()
    anchor = dict(anchors.get(anchor_id) or {})
    source_receipt_ids = [str(item) for item in episode.get("source_receipt_ids") or [] if str(item).strip()]

    segment_packets: list[dict[str, Any]] = []
    for segment in segments:
        cue_ids = [str(item) for item in segment.get("broll_cues") or [] if str(item).strip()]
        segment_packets.append(
            {
                "segment_id": str(segment.get("segment_id") or "").strip(),
                "order": int(segment.get("order") or 0),
                "headline": str(segment.get("headline") or "").strip(),
                "script_lines": [str(item) for item in segment.get("anchor_script") or [] if str(item).strip()],
                "lower_third": str(segment.get("lower_third") or "").strip(),
                "ticker_lines": [str(item) for item in segment.get("ticker") or [] if str(item).strip()],
                "visual_truth": "public_safe_reconstruction",
                "source_receipt_ids": source_receipt_ids,
                "broll_cues": [_build_broll_packet(cue_id) for cue_id in cue_ids],
            }
        )

    return {
        "contract_name": "black_ledger_newsroom.editorial_packet.v1",
        "generated_at": _utc_now(),
        "episode": {
            "episode_id": str(episode.get("episode_id") or "").strip(),
            "episode_type": str(episode.get("episode_type") or "").strip(),
            "title": str(episode.get("title") or "").strip(),
            "duration_target_seconds": int(episode.get("duration_target_seconds") or 0),
            "publish_scope": str(episode.get("publish_scope") or "").strip(),
            "truth_layer": str(episode.get("truth_layer") or "").strip(),
            "source_receipt_ids": source_receipt_ids,
            "public_safe_summary": _summary(segment_packets),
            "status": "editorial_ready",
        },
        "anchor": {
            "anchor_id": anchor_id,
            "display_name": str(anchor.get("display_name") or "").strip(),
            "role": str(anchor.get("role") or "").strip(),
            "visual_mode": str(anchor.get("visual_mode") or "").strip(),
            "voice": dict(anchor.get("voice") or {}),
            "host_performance_prompt": HOST_PROMPTS.get(anchor_id, HOST_PROMPTS["mara_voss"]),
        },
        "segments": segment_packets,
        "render_contract": {
            "media_factory_contract_version": str(media_factory_payload.get("version") or "").strip(),
            "jobs": list(dict(media_factory_payload.get("jobs") or {}).keys()),
            "quality_metrics": dict(media_factory_payload.get("quality_metrics") or {}),
        },
        "watch_page_contract": {
            "required_sections": ["video", "transcript", "source_receipts", "public_safety_note", "reconstruction_note", "publish_timestamp", "episode_type", "feedback_link"],
            "public_disclosure": "Some visuals are public-safe reconstructions generated from Chummer receipts. Private table details stay private.",
        },
        "provider_posture": {
            "editorial_truth_owner": "hub",
            "render_owner": "media_factory",
            "ea_role": "headline_variants_safety_rewrites_provider_inventory",
        },
    }


def write_payload(path: Path) -> Path:
    payload = build_payload()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Materialize a Black Ledger newsroom editorial packet.")
    parser.add_argument("--write", default=str(OUTPUT_PATH), help="Output JSON path.")
    args = parser.parse_args()
    path = write_payload(Path(args.write))
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
