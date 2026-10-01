#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


EA_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = EA_ROOT / ".env"
DEFAULT_HOST = os.environ.get("EA_HOST", "http://127.0.0.1:8090")
DEFAULT_PRINCIPAL = os.environ.get("EA_DEFAULT_PRINCIPAL_ID", "local-user")
DEFAULT_OUTPUT_DIR = Path("/docker/fleet/state/browseract_bootstrap/runtime/crezlo_property_tour_runs")


def env_value(name: str, default: str = "") -> str:
    direct = str(os.environ.get(name) or "").strip()
    if direct:
        return direct
    if ENV_FILE.exists():
        for raw in ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == name:
                return value.strip()
    return default


def slugify(value: str) -> str:
    lowered = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return lowered or "tour"


def request_json(*, host: str, api_token: str, principal_id: str, payload: dict[str, object], timeout_seconds: int) -> dict[str, object]:
    base = str(host or "").rstrip("/")
    request = urllib.request.Request(
        url=f"{base}/v1/plans/execute",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"content-type": "application/json"},
    )
    if str(api_token or "").strip():
        request.add_header("authorization", f"Bearer {api_token.strip()}")
    if str(principal_id or "").strip():
        request.add_header("x-ea-principal-id", principal_id.strip())
    try:
        with urllib.request.urlopen(request, timeout=max(60, timeout_seconds + 60)) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"crezlo_batch_http_error:{exc.code}:{body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"crezlo_batch_transport_error:{exc.reason}") from exc
    loaded = json.loads(body or "{}")
    if not isinstance(loaded, dict):
        raise RuntimeError("crezlo_batch_response_invalid")
    return loaded


def session_json(*, host: str, api_token: str, principal_id: str, session_id: str, timeout_seconds: int) -> dict[str, object]:
    base = str(host or "").rstrip("/")
    request = urllib.request.Request(
        url=f"{base}/v1/rewrite/sessions/{session_id}",
        method="GET",
    )
    if str(api_token or "").strip():
        request.add_header("authorization", f"Bearer {api_token.strip()}")
    if str(principal_id or "").strip():
        request.add_header("x-ea-principal-id", principal_id.strip())
    try:
        with urllib.request.urlopen(request, timeout=max(60, timeout_seconds)) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"crezlo_batch_session_http_error:{exc.code}:{body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"crezlo_batch_session_transport_error:{exc.reason}") from exc
    loaded = json.loads(body or "{}")
    if not isinstance(loaded, dict):
        raise RuntimeError("crezlo_batch_session_invalid")
    return loaded


def wait_for_session(
    *,
    host: str,
    api_token: str,
    principal_id: str,
    session_id: str,
    timeout_seconds: int,
) -> dict[str, object]:
    started_at = time.time()
    delay_seconds = 2.0
    while True:
        snapshot = session_json(
            host=host,
            api_token=api_token,
            principal_id=principal_id,
            session_id=session_id,
            timeout_seconds=timeout_seconds,
        )
        status = str(snapshot.get("status") or "").strip().lower()
        if status in {"completed", "failed", "cancelled", "canceled", "aborted"}:
            return snapshot
        if time.time() - started_at >= max(60, timeout_seconds):
            raise RuntimeError(f"crezlo_batch_session_timeout:{session_id}:{status or 'unknown'}")
        time.sleep(delay_seconds)


def extract_session_output(session_snapshot: dict[str, object]) -> dict[str, object]:
    steps = session_snapshot.get("steps")
    if isinstance(steps, list):
        for step in reversed(steps):
            if not isinstance(step, dict):
                continue
            if str(step.get("step_kind") or "").strip().lower() != "tool_call":
                continue
            output_json = step.get("output_json")
            if isinstance(output_json, dict) and output_json:
                return dict(output_json)
    artifacts = session_snapshot.get("artifacts")
    if isinstance(artifacts, list):
        for artifact in reversed(artifacts):
            if not isinstance(artifact, dict):
                continue
            output_json = artifact.get("output_json")
            if isinstance(output_json, dict) and output_json:
                return dict(output_json)
    return {}


def load_packets(path: Path) -> list[dict[str, object]]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, list):
        raise RuntimeError("crezlo_batch_packets_invalid")
    packets: list[dict[str, object]] = []
    for entry in loaded:
        if isinstance(entry, dict):
            packets.append(entry)
    if not packets:
        raise RuntimeError("crezlo_batch_packets_empty")
    return packets


def selected_variants(packet: dict[str, object], requested_variant_keys: set[str] | None) -> list[dict[str, object]]:
    variants = packet.get("tour_variants_json")
    if not isinstance(variants, list):
        return []
    selected: list[dict[str, object]] = []
    for entry in variants:
        if not isinstance(entry, dict):
            continue
        variant_key = str(entry.get("variant_key") or "").strip()
        if requested_variant_keys and variant_key not in requested_variant_keys:
            continue
        selected.append(entry)
    return selected


def build_run_input(
    *,
    packet: dict[str, object],
    variant: dict[str, object],
    binding_id: str,
    login_email: str,
    login_password: str,
    timeout_seconds: int,
) -> dict[str, object]:
    facts = dict(packet.get("property_facts_json") or {})
    title = str(packet.get("title") or packet.get("property_url") or "Property tour").strip()
    variant_key = str(variant.get("variant_key") or "variant").strip()
    rent = facts.get("total_rent_eur")
    price_tag = f"EUR {rent:g}" if isinstance(rent, (int, float)) else ""
    display_title = " | ".join(part for part in (title, price_tag, variant_key.replace("_", " ")) if part)
    short_title = " - ".join(part for part in (title, variant_key.replace("_", " ")) if part)
    payload = {
        "binding_id": binding_id,
        "tour_title": short_title[:180],
        "display_title": display_title[:220],
        "property_url": str(packet.get("property_url") or "").strip(),
        "media_urls_json": list(packet.get("media_urls_json") or []),
        "floorplan_urls_json": list(packet.get("floorplan_urls_json") or []),
        "scene_strategy": str(variant.get("scene_strategy") or "compact").strip(),
        "scene_selection_json": dict(variant.get("scene_selection_json") or {}),
        "property_facts_json": facts,
        "creative_brief": str(variant.get("creative_brief") or "").strip(),
        "variant_key": variant_key,
        "language": "de",
        "theme_name": str(variant.get("theme_name") or "").strip(),
        "tour_style": str(variant.get("tour_style") or "").strip(),
        "audience": str(variant.get("audience") or "").strip(),
        "call_to_action": str(variant.get("call_to_action") or "").strip(),
        "tour_visibility": "public",
        "tour_settings_json": dict(variant.get("tour_settings_json") or {}),
        "is_private": False,
        "runtime_inputs_json": {
            "listing_id": packet.get("listing_id"),
            "listing_uuid": packet.get("listing_uuid"),
            "variant_key": variant_key,
            "source": "willhaben",
        },
        "timeout_seconds": timeout_seconds,
        "login_email": login_email,
        "login_password": login_password,
    }
    # Carry the reviewed source geometry alongside the floorplan image.  A
    # provider can display a plan without proving that the tour follows it;
    # the downstream acceptance gate needs the hash-bound analysis receipt to
    # reject that drift instead of silently treating the upload as enough.
    floorplan_analysis = facts.get("floorplan_analysis_json") or facts.get("floorplan_analysis")
    if floorplan_analysis:
        payload["floorplan_analysis_json"] = floorplan_analysis
    floorplan_analysis_path = str(facts.get("floorplan_analysis_path") or "").strip()
    if floorplan_analysis_path:
        payload["floorplan_analysis_path"] = floorplan_analysis_path
    return payload


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Create Property Tour EA skill across Willhaben property packets.")
    parser.add_argument("--packets", required=True, help="Path to willhaben_property_packet.py JSON output.")
    parser.add_argument("--binding-id", required=True)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--api-token", default=env_value("EA_API_TOKEN"))
    parser.add_argument("--principal-id", default="cf-email:tibor.girschele@gmail.com" or DEFAULT_PRINCIPAL)
    parser.add_argument("--skill-key", default="create_property_tour")
    parser.add_argument("--goal-prefix", default="create a steerable property tour variant")
    parser.add_argument("--variant-key", action="append", default=[], help="Restrict to one or more variant keys.")
    parser.add_argument("--listing-id", action="append", default=[], help="Restrict to one or more listing ids.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--login-email", default=os.environ.get("EA_CREZLO_LOGIN_EMAIL", ""))
    parser.add_argument("--login-password", default=os.environ.get("EA_CREZLO_LOGIN_PASSWORD", ""))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    packets = load_packets(Path(args.packets))
    listing_filter = {str(value).strip() for value in args.listing_id if str(value).strip()}
    variant_filter = {str(value).strip() for value in args.variant_key if str(value).strip()}
    manifest: list[dict[str, object]] = []
    if not str(args.login_email or "").strip():
        raise RuntimeError("crezlo_login_email_required")
    if not str(args.login_password or "").strip():
        raise RuntimeError("crezlo_login_password_required")

    for packet in packets:
        listing_id = str(packet.get("listing_id") or "").strip()
        if listing_filter and listing_id not in listing_filter:
            continue
        for variant in selected_variants(packet, variant_filter or None):
            run_key = f"{listing_id or slugify(str(packet.get('title') or 'listing'))}__{slugify(str(variant.get('variant_key') or 'variant'))}"
            payload = {
                "skill_key": args.skill_key,
                "goal": f"{args.goal_prefix}: {packet.get('title')} [{variant.get('variant_key')}]",
                "input_json": build_run_input(
                    packet=packet,
                    variant=variant,
                    binding_id=args.binding_id,
                    login_email=args.login_email,
                    login_password=args.login_password,
                    timeout_seconds=args.timeout_seconds,
                ),
            }
            response_json = request_json(
                host=args.host,
                api_token=args.api_token,
                principal_id=args.principal_id,
                payload=payload,
                timeout_seconds=args.timeout_seconds,
            )
            session_id = str(response_json.get("session_id") or "").strip()
            session_snapshot: dict[str, object] | None = None
            if session_id:
                session_snapshot = wait_for_session(
                    host=args.host,
                    api_token=args.api_token,
                    principal_id=args.principal_id,
                    session_id=session_id,
                    timeout_seconds=args.timeout_seconds,
                )
                run_output = extract_session_output(session_snapshot)
                combined_json = {
                    "accepted_response_json": response_json,
                    "session_json": session_snapshot,
                    "output_json": run_output,
                }
            else:
                run_output = dict(response_json)
                combined_json = {
                    "output_json": run_output,
                }
            run_path = output_dir / f"{run_key}.json"
            run_path.write_text(json.dumps(combined_json, indent=2) + "\n", encoding="utf-8")
            if session_snapshot is not None:
                session_path = output_dir / f"{run_key}.session.json"
                session_path.write_text(json.dumps(session_snapshot, indent=2) + "\n", encoding="utf-8")
            structured = dict(run_output.get("structured_output_json") or {})
            manifest.append(
                {
                    "run_key": run_key,
                    "listing_id": listing_id,
                    "variant_key": variant.get("variant_key"),
                    "title": packet.get("title"),
                    "path": str(run_path),
                    "session_id": session_id or None,
                    "session_status": session_snapshot.get("status") if session_snapshot is not None else "completed_inline",
                    "artifact_id": run_output.get("artifact_id"),
                    "tour_id": run_output.get("tour_id") or structured.get("tour_id"),
                    "public_url": run_output.get("public_url") or structured.get("public_url"),
                    "share_url": run_output.get("share_url") or structured.get("share_url"),
                    "editor_url": run_output.get("editor_url") or structured.get("editor_url"),
                    "tour_status": run_output.get("tour_status") or structured.get("tour_status"),
                }
            )
            manifest_path = output_dir / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    if not manifest:
        raise RuntimeError("crezlo_batch_no_runs_selected")
    print(json.dumps({"status": "ok", "count": len(manifest), "manifest": str(output_dir / 'manifest.json')}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
