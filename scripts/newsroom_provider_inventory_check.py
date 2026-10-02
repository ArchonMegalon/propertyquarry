#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LTD_PATH = ROOT / "LTDs.md"
OUTPUT_PATH = Path("/docker/fleet/state/chummer6/NEWSROOM_PROVIDER_VERIFICATION.generated.json")


PROVIDER_SPECS = [
    {"provider": "Blip AI", "service_key": "blipai.app", "role": "operator_voice_capture", "status": "pilot", "commercial_use_allowed": False, "watermark_free": True, "max_duration": "n/a", "max_resolution": "n/a", "api_available": False, "manual_workflow_allowed": True, "privacy_terms_reviewed": False, "source_data_allowed": False},
    {"provider": "Syllabbles", "service_key": "Syllabbles", "role": "script_variants", "status": "pilot", "commercial_use_allowed": False, "watermark_free": True, "max_duration": "n/a", "max_resolution": "n/a", "api_available": False, "manual_workflow_allowed": True, "privacy_terms_reviewed": False, "source_data_allowed": False},
    {"provider": "VidBoard", "service_key": "VidBoard.ai", "role": "host_renderer_pilot", "status": "pilot", "commercial_use_allowed": False, "watermark_free": False, "max_duration": "unknown", "max_resolution": "unknown", "api_available": False, "manual_workflow_allowed": True, "privacy_terms_reviewed": False, "source_data_allowed": False},
    {"provider": "Nonverbia", "service_key": "Nonverbia", "role": "host_renderer_pilot", "status": "pilot", "commercial_use_allowed": False, "watermark_free": False, "max_duration": "unknown", "max_resolution": "unknown", "api_available": False, "manual_workflow_allowed": True, "privacy_terms_reviewed": False, "source_data_allowed": False},
    {"provider": "Mootion", "service_key": "Mootion", "role": "host_motion_renderer_pilot", "status": "pilot", "commercial_use_allowed": False, "watermark_free": False, "max_duration": "unknown", "max_resolution": "unknown", "api_available": False, "manual_workflow_allowed": True, "privacy_terms_reviewed": False, "source_data_allowed": False},
    {"provider": "AvoMap", "service_key": "AvoMap", "role": "map_broll_renderer_pilot", "status": "pilot", "commercial_use_allowed": False, "watermark_free": False, "max_duration": "unknown", "max_resolution": "unknown", "api_available": False, "manual_workflow_allowed": True, "privacy_terms_reviewed": False, "source_data_allowed": False},
    {"provider": "Unmixr AI", "service_key": "Unmixr AI", "role": "voice_narration_and_cleanup", "status": "pilot", "commercial_use_allowed": False, "watermark_free": True, "max_duration": "n/a", "max_resolution": "audio_only", "api_available": True, "manual_workflow_allowed": True, "privacy_terms_reviewed": False, "source_data_allowed": False},
    {"provider": "Soundmadeseen", "service_key": "Soundmadeseen", "role": "music_and_sound_design", "status": "pilot", "commercial_use_allowed": False, "watermark_free": True, "max_duration": "n/a", "max_resolution": "audio_only", "api_available": True, "manual_workflow_allowed": False, "privacy_terms_reviewed": False, "source_data_allowed": False},
    {"provider": "FineTuning.ai", "service_key": "FineTuning.ai", "role": "cue_beds_and_narration_variants", "status": "pilot", "commercial_use_allowed": False, "watermark_free": True, "max_duration": "n/a", "max_resolution": "audio_only", "api_available": False, "manual_workflow_allowed": True, "privacy_terms_reviewed": False, "source_data_allowed": False},
    {"provider": "MarkupGo", "service_key": "MarkupGo", "role": "poster_frames_and_contact_sheets", "status": "pilot", "commercial_use_allowed": False, "watermark_free": True, "max_duration": "still_only", "max_resolution": "unknown", "api_available": False, "manual_workflow_allowed": True, "privacy_terms_reviewed": False, "source_data_allowed": False},
    {"provider": "PeekShot", "service_key": "PeekShot", "role": "thumbnails_and_preview_cards", "status": "pilot", "commercial_use_allowed": False, "watermark_free": True, "max_duration": "still_only", "max_resolution": "unknown", "api_available": False, "manual_workflow_allowed": True, "privacy_terms_reviewed": False, "source_data_allowed": False},
    {"provider": "BrowserAct", "service_key": "BrowserAct", "role": "provider_verification_and_route_qa", "status": "verified", "commercial_use_allowed": True, "watermark_free": True, "max_duration": "n/a", "max_resolution": "n/a", "api_available": True, "manual_workflow_allowed": True, "privacy_terms_reviewed": True, "source_data_allowed": False},
    {"provider": "Teable", "service_key": "Teable", "role": "production_tracker_only", "status": "verified", "commercial_use_allowed": True, "watermark_free": True, "max_duration": "n/a", "max_resolution": "n/a", "api_available": True, "manual_workflow_allowed": True, "privacy_terms_reviewed": True, "source_data_allowed": False},
    {"provider": "Emailit", "service_key": "Emailit", "role": "delivery_after_episode_proof", "status": "verified", "commercial_use_allowed": True, "watermark_free": True, "max_duration": "n/a", "max_resolution": "n/a", "api_available": True, "manual_workflow_allowed": True, "privacy_terms_reviewed": True, "source_data_allowed": False},
]


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_ltd_rows() -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    if not LTD_PATH.is_file():
        return rows
    headers = ["service", "plan_tier", "holding", "status", "redeem_by", "workspace_integration_tier", "local_integration", "notes"]
    for raw_line in LTD_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line.startswith("| `"):
            continue
        parts = [part.strip() for part in line.strip("|").split("|")]
        if len(parts) != len(headers):
            continue
        row = {header: value.strip(" `") for header, value in zip(headers, parts)}
        rows[row["service"]] = row
    return rows


def build_payload() -> dict[str, object]:
    ltd_rows = _parse_ltd_rows()
    providers: list[dict[str, object]] = []
    photoreal_ready = False
    for spec in PROVIDER_SPECS:
        row = dict(ltd_rows.get(spec["service_key"]) or {})
        provider_row = {
            "provider": spec["provider"],
            "role": spec["role"],
            "account_status": str(row.get("status") or "tracked"),
            "tier": str(row.get("plan_tier") or "unknown"),
            "commercial_use_allowed": bool(spec["commercial_use_allowed"]),
            "watermark_free": bool(spec["watermark_free"]),
            "max_duration": spec["max_duration"],
            "max_resolution": spec["max_resolution"],
            "api_available": bool(spec["api_available"]),
            "manual_workflow_allowed": bool(spec["manual_workflow_allowed"]),
            "privacy_terms_reviewed": bool(spec["privacy_terms_reviewed"]),
            "source_data_allowed": bool(spec["source_data_allowed"]),
            "status": str(spec["status"]),
            "workspace_integration_tier": str(row.get("workspace_integration_tier") or "unknown"),
            "notes": str(row.get("notes") or ""),
        }
        if provider_row["status"] == "verified" and provider_row["role"] in {"host_renderer_pilot", "host_motion_renderer_pilot"}:
            photoreal_ready = True
        providers.append(provider_row)
    return {
        "generated_at": _utc_now(),
        "contract_name": "black_ledger_newsroom.provider_verification.v1",
        "verdict": "READY" if photoreal_ready else "NOT_READY",
        "photoreal_host_render_ready": photoreal_ready,
        "summary": "Fail closed until a host-render provider proves commercial, watermark-free newsroom readiness.",
        "providers": providers,
    }


def write_payload(path: Path) -> Path:
    payload = build_payload()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Materialize Black Ledger newsroom provider verification proof.")
    parser.add_argument("--write", default=str(OUTPUT_PATH), help="Output JSON path.")
    args = parser.parse_args()
    path = write_payload(Path(args.write))
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
