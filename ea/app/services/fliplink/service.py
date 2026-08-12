from __future__ import annotations

import copy
import hashlib
import hmac
import json
from pathlib import Path
from uuid import uuid4

from app.container import AppContainer
from app.domain.models import HumanTask, IntentSpecV3, now_utc_iso
from app.product.projections.common import compact_text
from app.repositories.property_packet_publications import (
    PropertyPacketPublicationRepository,
    build_property_packet_publication_repository,
)
from app.services.fliplink.adapter import is_custom_fliplink_domain, validate_manual_fliplink_url
from app.services.fliplink.models import (
    FlipLinkFormat,
    PacketPrivacyMode,
    PropertyPacketKind,
    fliplink_settings_from_env,
    normalize_fliplink_format,
    normalize_packet_kind,
    normalize_privacy_mode,
)
from app.services.fliplink.browser_adapter import (
    BROWSERACT_FLIPLINK_PUBLISH_CONTRACT_VERSION,
    BROWSERACT_FLIPLINK_REQUIRED_OUTPUTS,
    browseract_fliplink_publish_requested,
)
from app.services.fliplink.pdf_renderer import render_property_packet_pdf
from app.services.fliplink.webhooks import FlipLinkLeadWebhook, normalize_lead_webhook
from app.services.fliplink.webhooks import safe_custom_fields


ACTIVE_PACKET_STATUSES = {"rendered", "published", "publish_requested", "queued_operator_assist"}
ENGAGEMENT_METADATA_BLOCKED_MARKERS = ("token", "secret", "cookie", "session", "oauth", "internal", "debug", "credential")
ENGAGEMENT_METADATA_ALLOWED_KEYS = {
    "client_ts",
    "dwell_seconds",
    "feedback_channel",
    "journey_step",
    "page",
    "share_context",
    "source",
    "surface",
    "target",
    "ui_surface",
    "variant_key",
    "viewport",
}


def _sanitize_engagement_metadata(payload: dict[str, object] | None) -> dict[str, object]:
    raw_payload = dict(payload or {})
    raw_encoded = json.dumps(raw_payload, ensure_ascii=True, sort_keys=True, default=str)
    if len(raw_encoded.encode("utf-8")) > 4096:
        raise ValueError("packet_engagement_metadata_too_large")
    result: dict[str, object] = {}
    for key, value in raw_payload.items():
        normalized = str(key or "").strip()
        lowered = normalized.lower()
        if normalized not in ENGAGEMENT_METADATA_ALLOWED_KEYS:
            continue
        if any(marker in lowered for marker in ENGAGEMENT_METADATA_BLOCKED_MARKERS):
            continue
        if isinstance(value, dict):
            result[normalized] = {
                str(child_key)[:80]: str(child_value)[:240]
                for child_key, child_value in list(value.items())[:20]
                if not any(marker in str(child_key).lower() for marker in ENGAGEMENT_METADATA_BLOCKED_MARKERS)
            }
        elif isinstance(value, list):
            result[normalized] = [str(item)[:240] for item in value[:20]]
        elif isinstance(value, (str, int, float, bool)) or value is None:
            result[normalized] = value if not isinstance(value, str) else value[:240]
    encoded = json.dumps(result, ensure_ascii=True, sort_keys=True)
    if len(encoded.encode("utf-8")) > 4096:
        raise ValueError("packet_engagement_metadata_too_large")
    return result


class FlipLinkPacketService:
    def __init__(
        self,
        *,
        repo: PropertyPacketPublicationRepository,
        artifact_root: Path,
        orchestrator: object | None = None,
    ) -> None:
        self._repo = repo
        self._artifact_root = artifact_root
        self._orchestrator = orchestrator
        self._event_read_cache: dict[tuple[str, str, str, int], list[dict[str, object]]] = {}
        self._publication_read_cache: dict[tuple[str, int], list[dict[str, object]]] = {}
        self._structured_feedback_read_cache: dict[
            tuple[str, str, str, str, str],
            list[dict[str, object]],
        ] = {}

    def _cached_repo_events(
        self,
        *,
        principal_id: str,
        publication_id: str | None = None,
        event_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        key = (
            str(principal_id or "").strip(),
            str(publication_id or "").strip(),
            str(event_type or "").strip(),
            max(1, min(int(limit or 100), 500)),
        )
        cached = self._event_read_cache.get(key)
        if cached is None:
            cached = [
                dict(row)
                for row in self._repo.list_events(
                    principal_id=key[0],
                    publication_id=key[1] or None,
                    event_type=key[2] or None,
                    limit=key[3],
                )
            ]
            self._event_read_cache[key] = cached
        return [dict(row) for row in cached]

    def _cached_repo_publications(self, *, principal_id: str, limit: int = 100) -> list[dict[str, object]]:
        key = (str(principal_id or "").strip(), max(1, min(int(limit or 100), 500)))
        cached = self._publication_read_cache.get(key)
        if cached is None:
            cached = [dict(row) for row in self._repo.list_publications(principal_id=key[0], limit=key[1])]
            self._publication_read_cache[key] = cached
        return [dict(row) for row in cached]

    def capacity_status(self, *, principal_id: str) -> dict[str, object]:
        settings = fliplink_settings_from_env()
        principal_active = self._repo.count_publications(principal_id=principal_id, statuses=ACTIVE_PACKET_STATUSES)
        global_active = self._repo.count_publications(statuses=ACTIVE_PACKET_STATUSES)
        cap = max(1, int(settings.active_publication_cap or 1))
        warning_at = max(1, int(cap * 0.8))
        global_state = "blocked" if global_active >= cap else "warn" if global_active >= warning_at else "ok"
        principal_state = "blocked" if principal_active >= cap else "warn" if principal_active >= warning_at else "ok"
        return {
            "active": global_active,
            "principal_active": principal_active,
            "global_active": global_active,
            "cap": cap,
            "remaining": max(0, cap - global_active),
            "principal_remaining": max(0, cap - principal_active),
            "global_remaining": max(0, cap - global_active),
            "warning_at": warning_at,
            "state": global_state,
            "principal_state": principal_state,
            "global_state": global_state,
            "account_tier": int(settings.account_tier or 0),
            "custom_domain": settings.custom_domain,
        }

    def _require_capacity(self, *, principal_id: str) -> None:
        capacity = self.capacity_status(principal_id=principal_id)
        if str(capacity.get("state") or "") == "blocked":
            raise ValueError("fliplink_active_publication_cap_reached")

    def render_packet(
        self,
        *,
        principal_id: str,
        person_id: str = "self",
        property_ref: str,
        packet_kind: object = PropertyPacketKind.OWNER_REVIEW,
        privacy_mode: object = "",
        fliplink_format: object = "",
        search_run_id: str = "",
        include_exact_address: bool = False,
        include_floorplan: bool = True,
        include_photos: bool = True,
        source_payload: dict[str, object] | None = None,
        actor: str = "browser",
    ) -> dict[str, object]:
        kind = normalize_packet_kind(packet_kind)
        mode = normalize_privacy_mode(privacy_mode, packet_kind=kind)
        fmt = normalize_fliplink_format(fliplink_format, packet_kind=kind)
        self._require_capacity(principal_id=principal_id)
        publication_id = f"pub_{uuid4().hex}"
        source = dict(source_payload or {})
        source.setdefault("property_ref", str(property_ref or "").strip())
        source.setdefault("search_run_id", str(search_run_id or "").strip())
        source.setdefault("title", str(source.get("property_title") or property_ref or "PropertyQuarry packet").strip())
        rendered = render_property_packet_pdf(
            artifact_root=self._artifact_root,
            publication_id=publication_id,
            principal_id=principal_id,
            source=source,
            packet_kind=kind,
            privacy_mode=mode,
            fliplink_format=fmt,
            include_exact_address=bool(include_exact_address),
            include_floorplan=bool(include_floorplan),
            include_photos=bool(include_photos),
        )
        settings = fliplink_settings_from_env()
        row = self._repo.create_publication(
            {
                "publication_id": publication_id,
                "principal_id": principal_id,
                "person_id": person_id or "self",
                "property_ref": str(property_ref or "").strip(),
                "search_run_id": str(search_run_id or "").strip(),
                "packet_kind": kind.value,
                "privacy_mode": mode.value,
                "fliplink_format": fmt.value,
                "source_packet_ref": f"property:{str(property_ref or '').strip()}",
                "source_pdf_artifact_ref": str(rendered["pdf_path"]),
                "source_pdf_sha256": str(rendered["pdf_sha256"]),
                "source_pdf_size_bytes": int(rendered["pdf_size_bytes"] or 0),
                "redaction_policy_version": str(dict(rendered["receipt"]).get("redaction_policy_version") or "property_packet_v1"),
                "status": "rendered",
                "recommended_title": str(rendered.get("recommended_title") or ""),
                "recommended_format": fmt.value,
                "artifact_download_path": f"/app/api/properties/packets/{publication_id}/pdf",
                "receipt_artifact_ref": str(rendered["receipt_path"]),
                "redaction_receipt_json": dict(rendered["receipt"]),
                "packet_summary_json": {
                    "title": str(dict(rendered["redacted_payload"]).get("title") or ""),
                    "renderer_version": str(dict(rendered["receipt"]).get("renderer_version") or ""),
                    "format_is_permanent": True,
                    "manual_publish_required": True,
                    "recommended_folder": _folder_for(kind),
                    "recommended_custom_domain": settings.custom_domain,
                    "redacted_payload": dict(rendered["redacted_payload"]),
                },
            }
        )
        self._repo.record_event(
            {
                "publication_id": publication_id,
                "principal_id": principal_id,
                "event_type": "packet_rendered",
                "actor": actor,
                "payload_json": {
                    "packet_kind": kind.value,
                    "privacy_mode": mode.value,
                    "fliplink_format": fmt.value,
                    "source_pdf_sha256": str(rendered["pdf_sha256"]),
                    "removed_fields": list(dict(rendered["receipt"]).get("removed_fields") or []),
                    "include_floorplan": bool(include_floorplan),
                    "include_photos": bool(include_photos),
                },
            }
        )
        return row

    def _validate_publish_policy(
        self,
        *,
        publication: dict[str, object],
        validated_url: str,
        password_required: bool,
        sale_mode_enabled: bool,
    ) -> None:
        privacy_mode = str(publication.get("privacy_mode") or "").strip().lower()
        packet_kind = str(publication.get("packet_kind") or "").strip().lower()
        if privacy_mode == PacketPrivacyMode.OWNER_PRIVATE.value and not bool(password_required):
            raise ValueError("owner_private_requires_password")
        if sale_mode_enabled:
            if packet_kind != PropertyPacketKind.PAID_MARKET_REPORT.value or privacy_mode != PacketPrivacyMode.PAID_CUSTOMER.value:
                raise ValueError("sale_mode_requires_paid_market_report")
            if not is_custom_fliplink_domain(validated_url):
                raise ValueError("sale_mode_requires_custom_domain")

    def get_publication(self, *, publication_id: str, principal_id: str | None = None) -> dict[str, object] | None:
        return self._repo.get_publication(publication_id=publication_id, principal_id=principal_id)

    def list_publications(self, *, principal_id: str, limit: int = 100) -> list[dict[str, object]]:
        return self._repo.list_publications(principal_id=principal_id, limit=limit)

    def list_events(
        self,
        *,
        publication_id: str | None = None,
        principal_id: str | None = None,
        event_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        return self._repo.list_events(publication_id=publication_id, principal_id=principal_id, event_type=event_type, limit=limit)

    def record_manual_link(
        self,
        *,
        principal_id: str,
        publication_id: str,
        fliplink_url: str,
        fliplink_format: object = "",
        embed_code: str = "",
        qr_url: str = "",
        lead_capture_enabled: bool = False,
        password_required: bool = False,
        sale_mode_enabled: bool = False,
        actor: str = "browser",
    ) -> dict[str, object]:
        publication = self._repo.get_publication(publication_id=publication_id, principal_id=principal_id)
        if publication is None:
            raise KeyError("property_packet_publication_not_found")
        if str(publication.get("status") or "") == "archived":
            raise ValueError("fliplink_publication_archived")
        validated_url = validate_manual_fliplink_url(fliplink_url)
        existing_format = normalize_fliplink_format(publication.get("fliplink_format"))
        requested_format = normalize_fliplink_format(fliplink_format or existing_format.value)
        if requested_format != existing_format:
            raise ValueError("fliplink_format_is_permanent")
        self._validate_publish_policy(
            publication=publication,
            validated_url=validated_url,
            password_required=bool(password_required),
            sale_mode_enabled=bool(sale_mode_enabled),
        )
        now = now_utc_iso()
        updated = self._repo.update_publication(
            publication_id=publication_id,
            updates={
                "fliplink_url": validated_url,
                "fliplink_custom_domain_url": validated_url if "propertyquarry.com" in validated_url else "",
                "fliplink_embed_code": str(embed_code or "").strip()[:4000],
                "fliplink_qr_url": str(qr_url or "").strip()[:500],
                "lead_capture_enabled": bool(lead_capture_enabled),
                "password_required": bool(password_required),
                "sale_mode_enabled": bool(sale_mode_enabled),
                "status": "published",
                "published_at": now,
            },
        )
        if updated is None:
            raise KeyError("property_packet_publication_not_found")
        self._repo.record_event(
            {
                "publication_id": publication_id,
                "principal_id": principal_id,
                "event_type": "fliplink_manual_publish_completed",
                "actor": actor,
                "payload_json": {
                    "fliplink_url": validated_url,
                    "lead_capture_enabled": bool(lead_capture_enabled),
                    "password_required": bool(password_required),
                    "sale_mode_enabled": bool(sale_mode_enabled),
                },
            }
        )
        return updated

    def complete_browseract_publish(
        self,
        *,
        principal_id: str,
        publication_id: str,
        fliplink_url: str,
        embed_code: str = "",
        qr_url: str = "",
        screenshot_proof_ref: str = "",
        lead_capture_enabled: bool = True,
        password_required: bool = False,
        sale_mode_enabled: bool = False,
        actor: str = "browseract",
    ) -> dict[str, object]:
        publication = self._repo.get_publication(publication_id=publication_id, principal_id=principal_id)
        if publication is None:
            raise KeyError("property_packet_publication_not_found")
        if str(publication.get("status") or "") == "archived":
            raise ValueError("fliplink_publication_archived")
        if not str(screenshot_proof_ref or "").strip():
            raise ValueError("browseract_screenshot_proof_required")
        validated_url = validate_manual_fliplink_url(fliplink_url)
        self._validate_publish_policy(
            publication=publication,
            validated_url=validated_url,
            password_required=bool(password_required),
            sale_mode_enabled=bool(sale_mode_enabled),
        )
        now = now_utc_iso()
        updated = self._repo.update_publication(
            publication_id=publication_id,
            updates={
                "fliplink_url": validated_url,
                "fliplink_custom_domain_url": validated_url if is_custom_fliplink_domain(validated_url) else "",
                "fliplink_embed_code": str(embed_code or "").strip()[:4000],
                "fliplink_qr_url": str(qr_url or "").strip()[:500],
                "lead_capture_enabled": bool(lead_capture_enabled),
                "password_required": bool(password_required),
                "sale_mode_enabled": bool(sale_mode_enabled),
                "status": "published",
                "published_at": now,
            },
        )
        if updated is None:
            raise KeyError("property_packet_publication_not_found")
        self._repo.record_event(
            {
                "publication_id": publication_id,
                "principal_id": principal_id,
                "event_type": "fliplink_browser_publish_completed",
                "actor": actor,
                "payload_json": {
                    "fliplink_url": validated_url,
                    "embed_code_present": bool(str(embed_code or "").strip()),
                    "qr_url": str(qr_url or "").strip()[:500],
                    "screenshot_proof_ref": str(screenshot_proof_ref or "").strip()[:500],
                    "lead_capture_enabled": bool(lead_capture_enabled),
                    "password_required": bool(password_required),
                    "sale_mode_enabled": bool(sale_mode_enabled),
                    "contract_version": BROWSERACT_FLIPLINK_PUBLISH_CONTRACT_VERSION,
                    "published_at": now,
                },
            }
        )
        closed_task = self._close_browseract_publish_task(
            principal_id=principal_id,
            publication_id=publication_id,
            fliplink_url=validated_url,
            screenshot_proof_ref=screenshot_proof_ref,
            actor=actor,
        )
        if closed_task is not None:
            self._repo.record_event(
                {
                    "publication_id": publication_id,
                    "principal_id": principal_id,
                    "event_type": "fliplink_browser_publish_task_closed",
                    "actor": actor,
                    "payload_json": {
                        "human_task_id": str(getattr(closed_task, "human_task_id", "") or ""),
                        "queue_item_ref": f"human_task:{str(getattr(closed_task, 'human_task_id', '') or '')}",
                        "task_status": str(getattr(closed_task, "status", "") or ""),
                        "resolution": str(getattr(closed_task, "resolution", "") or ""),
                        "fliplink_url": validated_url,
                        "screenshot_proof_ref": str(screenshot_proof_ref or "").strip()[:500],
                        "contract_version": BROWSERACT_FLIPLINK_PUBLISH_CONTRACT_VERSION,
                    },
                }
            )
        return updated

    def archive_publication(
        self,
        *,
        principal_id: str,
        publication_id: str,
        actor: str = "browser",
        note: str = "",
    ) -> dict[str, object]:
        publication = self._repo.get_publication(publication_id=publication_id, principal_id=principal_id)
        if publication is None:
            raise KeyError("property_packet_publication_not_found")
        now = now_utc_iso()
        updated = self._repo.update_publication(
            publication_id=publication_id,
            updates={
                "status": "archived",
                "archived_at": now,
            },
        )
        if updated is None:
            raise KeyError("property_packet_publication_not_found")
        self._repo.record_event(
            {
                "publication_id": publication_id,
                "principal_id": principal_id,
                "event_type": "fliplink_publication_archived",
                "actor": actor,
                "payload_json": {
                    "note": str(note or "").strip()[:1000],
                    "previous_status": str(publication.get("status") or ""),
                    "archived_at": now,
                },
            }
        )
        return updated

    def request_browseract_publish(
        self,
        *,
        principal_id: str,
        publication_id: str,
        password_required: bool = False,
        lead_capture_enabled: bool = True,
        actor: str = "browser",
    ) -> dict[str, object]:
        publication = self._repo.get_publication(publication_id=publication_id, principal_id=principal_id)
        if publication is None:
            raise KeyError("property_packet_publication_not_found")
        if str(publication.get("status") or "") == "archived":
            raise ValueError("fliplink_publication_archived")
        existing_fliplink_url = str(publication.get("fliplink_url") or "").strip()
        if str(publication.get("status") or "").strip() == "published" and existing_fliplink_url:
            event = self._repo.record_event(
                {
                    "publication_id": publication_id,
                    "principal_id": principal_id,
                    "event_type": "fliplink_browser_publish_request_skipped_existing",
                    "actor": actor,
                    "payload_json": {
                        "fliplink_url": existing_fliplink_url,
                        "reason": "already_published",
                    },
                }
            )
            return {
                "status": "published_existing",
                "provider": "fliplink",
                "publication_id": publication_id,
                "fliplink_url": existing_fliplink_url,
                "event_id": str(event.get("event_id") or ""),
            }
        summary = dict(publication.get("packet_summary_json") or {})
        completion_endpoint = f"/app/api/properties/packets/{publication_id}/fliplink/browseract-complete"
        existing_task = self._existing_browseract_publish_task(
            principal_id=principal_id,
            publication_id=publication_id,
        )
        result = browseract_fliplink_publish_requested(
            {
                "publication_id": publication_id,
                "packet_kind": str(publication.get("packet_kind") or ""),
                "pdf_artifact_ref": str(publication.get("source_pdf_artifact_ref") or ""),
                "receipt_artifact_ref": str(publication.get("receipt_artifact_ref") or ""),
                "source_pdf_sha256": str(publication.get("source_pdf_sha256") or ""),
                "source_pdf_size_bytes": int(publication.get("source_pdf_size_bytes") or 0),
                "redaction_receipt_present": bool(
                    publication.get("receipt_artifact_ref") or publication.get("redaction_receipt_json")
                ),
                "recommended_title": str(publication.get("recommended_title") or ""),
                "fliplink_format": str(publication.get("fliplink_format") or ""),
                "privacy_mode": str(publication.get("privacy_mode") or ""),
                "recommended_folder": str(summary.get("recommended_folder") or _folder_for(normalize_packet_kind(publication.get("packet_kind")))),
                "custom_domain": str(summary.get("recommended_custom_domain") or fliplink_settings_from_env().custom_domain),
                "lead_capture_enabled": bool(lead_capture_enabled),
                "password_required": bool(password_required),
                "completion_endpoint": completion_endpoint,
            }
        )
        human_task_payload: dict[str, object] = {}
        if existing_task is None:
            created_task = self._create_browseract_publish_task(
                principal_id=principal_id,
                publication=publication,
                request_payload=dict(result),
                lead_capture_enabled=bool(lead_capture_enabled),
                password_required=bool(password_required),
                actor=actor,
                completion_endpoint=completion_endpoint,
            )
            if created_task is not None:
                human_task_payload = self._browseract_publish_task_payload(created_task)
        else:
            human_task_payload = {
                **self._browseract_publish_task_payload(existing_task),
                "deduplicated": True,
            }
        event = self._repo.record_event(
            {
                "publication_id": publication_id,
                "principal_id": principal_id,
                "event_type": "fliplink_browser_publish_requested",
                "actor": actor,
                "payload_json": {
                    **dict(result),
                    **human_task_payload,
                    "completion_endpoint": completion_endpoint,
                    "lead_capture_enabled": bool(lead_capture_enabled),
                    "password_required": bool(password_required),
                },
            }
        )
        self._repo.update_publication(
            publication_id=publication_id,
            updates={
                "status": "queued_operator_assist" if human_task_payload else "publish_requested",
            },
        )
        return {
            **dict(result),
            **human_task_payload,
            "publication_id": publication_id,
            "completion_endpoint": completion_endpoint,
            "event_id": str(event.get("event_id") or ""),
        }

    def _browseract_publish_tasks(self, *, principal_id: str, publication_id: str) -> list[HumanTask]:
        orchestrator = self._orchestrator
        if orchestrator is None or not hasattr(orchestrator, "list_human_tasks"):
            return []
        rows = orchestrator.list_human_tasks(principal_id=principal_id, status=None, limit=500)  # type: ignore[attr-defined]
        matched: list[HumanTask] = []
        for row in rows:
            if str(getattr(row, "task_type", "") or "").strip() != "fliplink_browseract_publish":
                continue
            input_json = dict(getattr(row, "input_json", {}) or {})
            if str(input_json.get("publication_id") or "").strip() == str(publication_id or "").strip():
                matched.append(row)
        return matched

    def _existing_browseract_publish_task(self, *, principal_id: str, publication_id: str) -> HumanTask | None:
        for row in self._browseract_publish_tasks(principal_id=principal_id, publication_id=publication_id):
            if str(getattr(row, "status", "") or "").strip().lower() in {"pending", "claimed"}:
                return row
        return None

    def _browseract_publish_task_payload(self, task: HumanTask) -> dict[str, object]:
        task_id = str(getattr(task, "human_task_id", "") or "").strip()
        if not task_id:
            return {}
        return {
            "human_task_id": task_id,
            "queue_item_ref": f"human_task:{task_id}",
            "browseract_task_status": str(getattr(task, "status", "") or ""),
            "browseract_task_assignment_state": str(getattr(task, "assignment_state", "") or ""),
        }

    def _start_browseract_publish_session(
        self,
        *,
        principal_id: str,
        publication_id: str,
        title: str,
        actor: str,
    ) -> str:
        orchestrator = self._orchestrator
        if orchestrator is None or not hasattr(orchestrator, "_ledger"):
            return ""
        ledger = getattr(orchestrator, "_ledger")
        session = ledger.start_session(
            IntentSpecV3(
                principal_id=principal_id,
                goal=f"Publish PropertyQuarry packet {publication_id} to FlipLink",
                task_type="fliplink_browseract_publish",
                deliverable_type="fliplink_publication",
                risk_class="medium",
                approval_class="operator",
                budget_class="standard",
            )
        )
        ledger.append_event(
            session.session_id,
            "fliplink_browseract_publish_session_started",
            {
                "publication_id": publication_id,
                "title": title,
                "actor": str(actor or "").strip() or "browser",
                "started_at": now_utc_iso(),
            },
        )
        return str(session.session_id or "")

    def _create_browseract_publish_task(
        self,
        *,
        principal_id: str,
        publication: dict[str, object],
        request_payload: dict[str, object],
        lead_capture_enabled: bool,
        password_required: bool,
        actor: str,
        completion_endpoint: str,
    ) -> HumanTask | None:
        orchestrator = self._orchestrator
        if orchestrator is None or not hasattr(orchestrator, "create_human_task"):
            return None
        publication_id = str(publication.get("publication_id") or "").strip()
        summary = dict(publication.get("packet_summary_json") or {})
        title = str(publication.get("recommended_title") or summary.get("title") or publication_id).strip()
        session_id = self._start_browseract_publish_session(
            principal_id=principal_id,
            publication_id=publication_id,
            title=title,
            actor=actor,
        )
        if not session_id:
            return None
        input_json = {
            "publication_id": publication_id,
            "principal_id": principal_id,
            "property_ref": str(publication.get("property_ref") or ""),
            "search_run_id": str(publication.get("search_run_id") or ""),
            "packet_kind": str(publication.get("packet_kind") or ""),
            "privacy_mode": str(publication.get("privacy_mode") or ""),
            "fliplink_format": str(publication.get("fliplink_format") or ""),
            "recommended_title": title,
            "recommended_folder": str(summary.get("recommended_folder") or _folder_for(normalize_packet_kind(publication.get("packet_kind")))),
            "custom_domain": str(summary.get("recommended_custom_domain") or fliplink_settings_from_env().custom_domain),
            "pdf_artifact_ref": str(publication.get("source_pdf_artifact_ref") or ""),
            "receipt_artifact_ref": str(publication.get("receipt_artifact_ref") or ""),
            "source_pdf_sha256": str(publication.get("source_pdf_sha256") or ""),
            "source_pdf_size_bytes": int(publication.get("source_pdf_size_bytes") or 0),
            "redaction_policy_version": str(publication.get("redaction_policy_version") or ""),
            "lead_capture_enabled": bool(lead_capture_enabled),
            "password_required": bool(password_required),
            "completion_endpoint": completion_endpoint,
            "task_name": str(request_payload.get("task_name") or "browseract.fliplink_publish_property_packet"),
            "contract_version": str(request_payload.get("contract_version") or BROWSERACT_FLIPLINK_PUBLISH_CONTRACT_VERSION),
            "required_outputs": list(request_payload.get("required_outputs") or BROWSERACT_FLIPLINK_REQUIRED_OUTPUTS),
            "completion_payload_schema": dict(request_payload.get("completion_payload_schema") or {}),
            "browseract_runner_payload": dict(request_payload.get("runner_payload") or {}),
            "provider": str(request_payload.get("provider") or "fliplink"),
        }
        return orchestrator.create_human_task(  # type: ignore[attr-defined]
            session_id=session_id,
            principal_id=principal_id,
            task_type="fliplink_browseract_publish",
            role_required="operator",
            brief=f"Publish '{title}' to FlipLink and return the created URL.",
            authority_required="operator",
            why_human=(
                "FlipLink publishing uses BrowserAct/operator credentials and must preserve "
                "the already redacted PDF, chosen permanent format, password flag, and custom domain policy."
            ),
            quality_rubric_json={
                "must_follow_contract_version": str(
                    request_payload.get("contract_version") or BROWSERACT_FLIPLINK_PUBLISH_CONTRACT_VERSION
                ),
                "must_use_pdf_artifact_ref": str(publication.get("source_pdf_artifact_ref") or ""),
                "must_verify_pdf_sha256": str(publication.get("source_pdf_sha256") or ""),
                "must_call_completion_endpoint": completion_endpoint,
                "must_capture_screenshot_proof_ref": True,
                "must_return_required_outputs": list(request_payload.get("required_outputs") or BROWSERACT_FLIPLINK_REQUIRED_OUTPUTS),
                "must_not_upload_unredacted_source_payload": True,
            },
            input_json=input_json,
            desired_output_json={
                "resolution": "published",
                "contract_version": str(request_payload.get("contract_version") or BROWSERACT_FLIPLINK_PUBLISH_CONTRACT_VERSION),
                "required_outputs": list(request_payload.get("required_outputs") or BROWSERACT_FLIPLINK_REQUIRED_OUTPUTS),
                "fliplink_url": "",
                "embed_code": "",
                "qr_url": "",
                "screenshot_proof_ref": "",
                "completion_endpoint": completion_endpoint,
            },
            priority="high",
        )

    def _close_browseract_publish_task(
        self,
        *,
        principal_id: str,
        publication_id: str,
        fliplink_url: str,
        screenshot_proof_ref: str,
        actor: str,
    ) -> HumanTask | None:
        orchestrator = self._orchestrator
        if orchestrator is None or not hasattr(orchestrator, "return_human_task"):
            return None
        task = self._existing_browseract_publish_task(principal_id=principal_id, publication_id=publication_id)
        if task is None:
            return None
        task_id = str(getattr(task, "human_task_id", "") or "").strip()
        if not task_id:
            return None
        operator_id = str(actor or "").strip() or "browseract"
        returned = orchestrator.return_human_task(  # type: ignore[attr-defined]
            human_task_id=task_id,
            principal_id=principal_id,
            operator_id=operator_id,
            resolution="published",
            returned_payload_json={
                "publication_id": publication_id,
                "fliplink_url": fliplink_url,
                "screenshot_proof_ref": str(screenshot_proof_ref or "").strip()[:500],
                "contract_version": BROWSERACT_FLIPLINK_PUBLISH_CONTRACT_VERSION,
                "completion_source": "fliplink_browseract_complete",
            },
            provenance_json={
                "source": "fliplink_browseract_complete",
                "actor": operator_id,
            },
        )
        if returned is not None:
            return returned
        human_tasks = getattr(orchestrator, "_human_tasks", None)
        ledger = getattr(orchestrator, "_ledger", None)
        if human_tasks is None or not hasattr(human_tasks, "return_task"):
            return None
        returned = human_tasks.return_task(
            task_id,
            operator_id=operator_id,
            resolution="published",
            returned_payload_json={
                "publication_id": publication_id,
                "fliplink_url": fliplink_url,
                "screenshot_proof_ref": str(screenshot_proof_ref or "").strip()[:500],
                "contract_version": BROWSERACT_FLIPLINK_PUBLISH_CONTRACT_VERSION,
                "completion_source": "fliplink_browseract_complete",
            },
            provenance_json={
                "source": "fliplink_browseract_complete",
                "actor": operator_id,
                "operator_profile_bypass": True,
            },
        )
        if returned is not None and ledger is not None and hasattr(ledger, "append_event"):
            ledger.append_event(
                returned.session_id,
                "human_task_returned",
                {
                    "human_task_id": returned.human_task_id,
                    "operator_id": operator_id,
                    "assigned_operator_id": str(getattr(returned, "assigned_operator_id", "") or ""),
                    "resolution": returned.resolution,
                    "assignment_state": returned.assignment_state,
                    "assignment_source": "browseract_completion",
                    "assigned_at": returned.assigned_at or "",
                    "assigned_by_actor_id": operator_id,
                    "step_id": returned.step_id or "",
                },
            )
        return returned

    def record_analytics_snapshot(
        self,
        *,
        principal_id: str,
        publication_id: str,
        views: int | None = None,
        unique_visitors: int | None = None,
        average_time_seconds: int | None = None,
        top_pages: list[dict[str, object]] | None = None,
        referral_sources: list[dict[str, object]] | None = None,
        device_breakdown: dict[str, int] | None = None,
        geography_breakdown: dict[str, int] | None = None,
        source: str = "manual",
        screenshot_proof_ref: str = "",
        captured_from_url: str = "",
        actor: str = "browser",
    ) -> dict[str, object]:
        publication = self._repo.get_publication(publication_id=publication_id, principal_id=principal_id)
        if publication is None:
            raise KeyError("property_packet_publication_not_found")
        payload = {
            "publication_id": publication_id,
            "views": _non_negative_int(views),
            "unique_visitors": _non_negative_int(unique_visitors),
            "average_time_seconds": _non_negative_int(average_time_seconds),
            "top_pages": _bounded_metric_rows(top_pages),
            "referral_sources": _bounded_metric_rows(referral_sources),
            "device_breakdown": _bounded_int_map(device_breakdown),
            "geography_breakdown": _bounded_int_map(geography_breakdown),
            "captured_at": now_utc_iso(),
            "source": str(source or "manual").strip().lower()[:40] or "manual",
            "screenshot_proof_ref": str(screenshot_proof_ref or "").strip()[:500],
            "captured_from_url": str(captured_from_url or "").strip()[:500],
            "trust": "source_attested" if str(source or "").strip().lower() in {"browseract", "api"} else "operator_entered_or_imported",
        }
        event = self._repo.record_event(
            {
                "publication_id": publication_id,
                "principal_id": principal_id,
                "event_type": "fliplink_analytics_snapshot_recorded",
                "actor": actor,
                "payload_json": payload,
            }
        )
        return {"event": event, "snapshot": payload}

    def latest_analytics_snapshot(self, *, principal_id: str, publication_id: str) -> dict[str, object]:
        events = self._repo.list_events(
            publication_id=publication_id,
            principal_id=principal_id,
            event_type="fliplink_analytics_snapshot_recorded",
            limit=1,
        )
        if not events:
            return {}
        payload = dict(events[0].get("payload_json") or {})
        payload["event_id"] = str(events[0].get("event_id") or "")
        payload["created_at"] = str(events[0].get("created_at") or "")
        return payload

    def analytics_by_publication(
        self,
        *,
        principal_id: str,
        publication_ids: list[str],
    ) -> dict[str, dict[str, object]]:
        wanted = {str(item or "").strip() for item in publication_ids if str(item or "").strip()}
        if not wanted:
            return {}
        events = self._repo.list_events(
            principal_id=principal_id,
            event_type="fliplink_analytics_snapshot_recorded",
            limit=500,
        )
        out: dict[str, dict[str, object]] = {}
        for event in events:
            publication_id = str(event.get("publication_id") or "")
            if publication_id not in wanted or publication_id in out:
                continue
            payload = dict(event.get("payload_json") or {})
            payload["event_id"] = str(event.get("event_id") or "")
            payload["created_at"] = str(event.get("created_at") or "")
            out[publication_id] = payload
        return out

    def verify_webhook_secret(self, *, provided_header: str = "", provided_query: str = "") -> str:
        settings = fliplink_settings_from_env()
        if not settings.webhook_allowed:
            raise PermissionError("fliplink_webhook_disabled")
        expected = settings.webhook_secret
        if not expected:
            raise PermissionError("fliplink_webhook_secret_not_configured")
        header = str(provided_header or "").strip()
        query = str(provided_query or "").strip()
        if header:
            if not hmac.compare_digest(header, expected):
                raise PermissionError("fliplink_webhook_secret_invalid")
            return "header"
        if query and not settings.webhook_allow_query_secret:
            raise PermissionError("fliplink_webhook_query_secret_disabled")
        provided = query
        if not provided or not hmac.compare_digest(provided, expected):
            raise PermissionError("fliplink_webhook_secret_invalid")
        return "query"

    def ingest_lead_webhook(
        self,
        *,
        payload: dict[str, object],
        actor: str = "fliplink_webhook",
        secret_mode: str = "",
    ) -> dict[str, object]:
        lead = normalize_lead_webhook(payload)
        publication = self._repo.find_publication(publication_id=lead.publication_id, fliplink_url=lead.fliplink_url)
        if publication is None:
            lead_url_hash = hashlib.sha256(str(lead.fliplink_url or "").strip().encode("utf-8")).hexdigest() if str(lead.fliplink_url or "").strip() else ""
            custom_fields = safe_custom_fields(payload.get("custom_fields") or {})
            self._repo.record_event(
                {
                    "publication_id": "",
                    "principal_id": "",
                    "event_type": "fliplink_webhook_unmatched",
                    "actor": actor,
                    "payload_json": {
                        "publication_id_present": bool(str(lead.publication_id or "").strip()),
                        "fliplink_url_hash": lead_url_hash,
                        "secret_mode": str(secret_mode or ""),
                        "custom_field_keys": sorted(str(key) for key in custom_fields.keys()),
                        "received_at": now_utc_iso(),
                        "trust": "untrusted_external",
                    },
                }
            )
            return {
                "status": "accepted_unmatched",
                "trust": "untrusted_external",
                "publication_id": "",
                "secret_mode": str(secret_mode or ""),
            }
        safe_payload = lead.safe_payload()
        event = self._repo.record_event(
            {
                "publication_id": str(publication.get("publication_id") or ""),
                "principal_id": str(publication.get("principal_id") or ""),
                "event_type": "fliplink_lead_captured",
                "actor": actor,
                "payload_json": {
                    **safe_payload,
                    "property_ref": str(publication.get("property_ref") or ""),
                    "packet_kind": str(publication.get("packet_kind") or ""),
                    "privacy_mode": str(publication.get("privacy_mode") or ""),
                    "secret_mode": str(secret_mode or ""),
                },
            }
        )
        return {
            "status": "accepted",
            "trust": "untrusted_external",
            "publication_id": str(publication.get("publication_id") or ""),
            "event_id": str(event.get("event_id") or ""),
            "secret_mode": str(secret_mode or ""),
        }

    def feedback_inbox(self, *, principal_id: str, limit: int = 100) -> dict[str, object]:
        lead_events = self._repo.list_events(principal_id=principal_id, event_type="fliplink_lead_captured", limit=limit)
        review_events = self._repo.list_events(principal_id=principal_id, event_type="fliplink_feedback_reviewed", limit=limit)
        reviewed_ids = {
            str(dict(event.get("payload_json") or {}).get("target_event_id") or "").strip()
            for event in review_events
        }
        items: list[dict[str, object]] = []
        for event in lead_events:
            payload = dict(event.get("payload_json") or {})
            event_id = str(event.get("event_id") or "")
            items.append(
                {
                    "event_id": event_id,
                    "publication_id": str(event.get("publication_id") or ""),
                    "property_ref": str(payload.get("property_ref") or ""),
                    "packet_kind": str(payload.get("packet_kind") or ""),
                    "privacy_mode": str(payload.get("privacy_mode") or ""),
                    "reviewer": {
                        "name": str(payload.get("name") or ""),
                        "email_masked": str(payload.get("email_masked") or ""),
                        "company": str(payload.get("company") or ""),
                        "job_title": str(payload.get("job_title") or ""),
                    },
                    "custom_fields": dict(payload.get("custom_fields") or {}),
                    "status": "reviewed" if event_id in reviewed_ids else "pending_owner_review",
                    "trust": "untrusted_external",
                    "created_at": str(event.get("created_at") or ""),
                }
            )
        return {"items": items, "total": len(items)}

    def create_share(
        self,
        *,
        principal_id: str,
        publication_id: str,
        audience_type: str,
        channel: str,
        variant_key: str = "",
        cover_note: str = "",
        recipients: list[dict[str, object]] | None = None,
        actor: str = "browser",
    ) -> dict[str, object]:
        publication = self._repo.get_publication(publication_id=publication_id, principal_id=principal_id)
        if publication is None:
            raise KeyError("property_packet_publication_not_found")
        if str(publication.get("status") or "") == "archived":
            raise ValueError("fliplink_publication_archived")
        normalized_recipients = []
        for item in list(recipients or [])[:25]:
            if not isinstance(item, dict):
                continue
            normalized_recipients.append(
                {
                    "recipient_id": str(item.get("recipient_id") or f"rec_{uuid4().hex}").strip(),
                    "name": str(item.get("name") or "").strip()[:160],
                    "email": str(item.get("email") or "").strip()[:240],
                    "relationship": str(item.get("relationship") or "").strip()[:120],
                    "role_label": str(item.get("role_label") or item.get("relationship") or "").strip()[:120],
                    "created_at": now_utc_iso(),
                }
            )
        if not normalized_recipients:
            raise ValueError("packet_share_requires_recipient")
        share = {
            "share_id": f"shr_{uuid4().hex}",
            "publication_id": publication_id,
            "property_ref": str(publication.get("property_ref") or ""),
            "variant_key": str(variant_key or "default").strip()[:120] or "default",
            "audience_type": str(audience_type or "family").strip()[:80] or "family",
            "channel": str(channel or "link").strip()[:80] or "link",
            "cover_note": str(cover_note or "").strip()[:1000],
            "sent_by": actor,
            "sent_at": now_utc_iso(),
            "status": "shared",
            "recipients": normalized_recipients,
        }
        self._repo.record_event(
            {
                "publication_id": publication_id,
                "principal_id": principal_id,
                "event_type": "packet_share_created",
                "actor": actor,
                "payload_json": copy.deepcopy(share),
            }
        )
        for recipient in normalized_recipients:
            self._repo.record_event(
                {
                    "publication_id": publication_id,
                    "principal_id": principal_id,
                    "event_type": "packet_followup_task_created",
                    "actor": actor,
                    "payload_json": {
                        "task_id": f"fup_{uuid4().hex}",
                        "share_id": str(share["share_id"]),
                        "recipient_id": str(recipient.get("recipient_id") or ""),
                        "recipient_name": str(recipient.get("name") or ""),
                        "stakeholder_id": str(recipient.get("recipient_id") or ""),
                        "property_ref": str(publication.get("property_ref") or ""),
                        "recommended_action": "await_open",
                        "reason": "share_sent",
                        "status": "open",
                        "owner": "",
                        "created_at": now_utc_iso(),
                    },
                }
            )
        return share

    def list_shares(self, *, principal_id: str, publication_id: str) -> list[dict[str, object]]:
        rows = self._repo.list_events(
            principal_id=principal_id,
            publication_id=publication_id,
            event_type="packet_share_created",
            limit=500,
        )
        return [dict(event.get("payload_json") or {}) for event in rows]

    def record_engagement_event(
        self,
        *,
        principal_id: str,
        publication_id: str,
        share_id: str,
        recipient_id: str,
        event_type: str,
        event_value: str = "",
        metadata_json: dict[str, object] | None = None,
        actor: str = "browser",
    ) -> dict[str, object]:
        publication = self._repo.get_publication(publication_id=publication_id, principal_id=principal_id)
        if publication is None:
            raise KeyError("property_packet_publication_not_found")
        normalized_type = str(event_type or "").strip()
        if normalized_type not in {
            "opened",
            "clicked_property",
            "expanded_gallery",
            "saved_property",
            "submitted_feedback",
            "requested_followup",
            "no_activity_48h",
        }:
            raise ValueError("invalid_packet_engagement_event_type")
        safe_metadata_json = _sanitize_engagement_metadata(metadata_json)
        payload = {
            "engagement_id": f"eng_{uuid4().hex}",
            "share_id": str(share_id or "").strip(),
            "recipient_id": str(recipient_id or "").strip(),
            "event_type": normalized_type,
            "event_value": str(event_value or "").strip()[:240],
            "metadata_json": copy.deepcopy(safe_metadata_json),
            "occurred_at": now_utc_iso(),
        }
        return self._repo.record_event(
            {
                "publication_id": publication_id,
                "principal_id": principal_id,
                "event_type": "packet_engagement_event_recorded",
                "actor": actor,
                "payload_json": payload,
            }
        )

    def _engagement_events(self, *, principal_id: str, publication_id: str) -> list[dict[str, object]]:
        rows = self._repo.list_events(
            principal_id=principal_id,
            publication_id=publication_id,
            event_type="packet_engagement_event_recorded",
            limit=1000,
        )
        return [dict(event.get("payload_json") or {}) for event in rows]

    def _feedback_events(self, *, principal_id: str, publication_id: str | None = None) -> list[dict[str, object]]:
        rows = self._cached_repo_events(
            principal_id=principal_id,
            publication_id=publication_id,
            event_type="property_feedback_entry_recorded",
            limit=1000,
        )
        return [dict(event.get("payload_json") or {}) for event in rows]

    def _summary_events(self, *, principal_id: str) -> list[dict[str, object]]:
        rows = self._repo.list_events(
            principal_id=principal_id,
            event_type="property_summary_artifact_generated",
            limit=500,
        )
        return [dict(event.get("payload_json") or {}) for event in rows]

    def list_summary_artifacts(self, *, principal_id: str) -> list[dict[str, object]]:
        return self._summary_events(principal_id=principal_id)

    def export_principal_data(self, *, principal_id: str) -> dict[str, list[dict[str, object]]]:
        method = getattr(self._repo, "export_principal", None)
        if callable(method):
            payload = dict(method(str(principal_id or "").strip()) or {})
            return {
                "publications": [dict(row) for row in list(payload.get("publications") or []) if isinstance(row, dict)],
                "events": [dict(row) for row in list(payload.get("events") or []) if isinstance(row, dict)],
            }
        return {
            "publications": self.list_publications(principal_id=principal_id, limit=500),
            "events": self.list_events(principal_id=principal_id, limit=500),
        }

    def erase_principal_data(self, *, principal_id: str) -> dict[str, int]:
        principal = str(principal_id or "").strip()
        if not principal:
            return {"publications": 0, "events": 0, "artifact_files": 0}
        publications = self._repo.list_publications(principal_id=principal, limit=500)
        artifact_files = 0
        try:
            artifact_root = self._artifact_root.resolve()
        except OSError:
            artifact_root = self._artifact_root
        for publication in publications:
            for key in ("artifact_download_path", "source_pdf_artifact_ref", "receipt_artifact_ref"):
                raw_path = str(publication.get(key) or "").strip()
                if not raw_path:
                    continue
                try:
                    candidate = Path(raw_path).expanduser().resolve()
                except OSError:
                    continue
                if candidate == artifact_root or artifact_root not in candidate.parents or not candidate.is_file():
                    continue
                try:
                    candidate.unlink()
                except OSError:
                    continue
                artifact_files += 1
        eraser = getattr(self._repo, "erase_principal", None)
        counts = dict(eraser(principal) or {}) if callable(eraser) else {}
        return {
            "publications": int(counts.get("publications") or 0),
            "events": int(counts.get("events") or 0),
            "artifact_files": artifact_files,
        }

    def _attached_summary_ids(self, *, principal_id: str, publication_id: str) -> list[str]:
        rows = self._repo.list_events(
            principal_id=principal_id,
            publication_id=publication_id,
            event_type="packet_summary_artifact_attached",
            limit=500,
        )
        return [str(dict(event.get("payload_json") or {}).get("artifact_id") or "") for event in rows]

    def _current_followup_tasks(self, *, principal_id: str, publication_id: str) -> list[dict[str, object]]:
        events = self._repo.list_events(principal_id=principal_id, publication_id=publication_id, limit=2000)
        created: dict[str, dict[str, object]] = {}
        latest_engagement: dict[str, str] = {}
        for row in reversed(events):
            event_type = str(row.get("event_type") or "")
            payload = dict(row.get("payload_json") or {})
            if event_type == "packet_engagement_event_recorded":
                recipient_id = str(payload.get("recipient_id") or "")
                if recipient_id and recipient_id not in latest_engagement:
                    latest_engagement[recipient_id] = str(payload.get("event_type") or "")
            elif event_type == "packet_followup_task_created":
                task_id = str(payload.get("task_id") or "")
                if task_id:
                    created[task_id] = copy.deepcopy(payload)
            elif event_type == "packet_followup_task_assigned":
                task = created.get(str(payload.get("task_id") or ""))
                if task is not None:
                    task["owner"] = str(payload.get("owner") or "")
                    task["status"] = "assigned"
            elif event_type == "packet_followup_task_resolved":
                task = created.get(str(payload.get("task_id") or ""))
                if task is not None:
                    task["status"] = "resolved"
                    task["resolution"] = str(payload.get("resolution") or "")
        rows_out: list[dict[str, object]] = []
        for task in created.values():
            recipient_id = str(task.get("recipient_id") or "")
            if str(task.get("status") or "") != "resolved":
                state = latest_engagement.get(recipient_id, "")
                if state == "submitted_feedback":
                    task["recommended_action"] = "review_feedback"
                elif state in {"opened", "clicked_property", "expanded_gallery", "saved_property"}:
                    task["recommended_action"] = "request_feedback"
                elif state == "requested_followup":
                    task["recommended_action"] = "contact_recipient"
                elif state == "no_activity_48h" or not state:
                    task["recommended_action"] = "resend_or_follow_up"
            rows_out.append(task)
        return sorted(rows_out, key=lambda item: str(item.get("created_at") or ""), reverse=True)

    def engagement_snapshot(self, *, principal_id: str, publication_id: str) -> dict[str, object]:
        shares = self.list_shares(principal_id=principal_id, publication_id=publication_id)
        engagements = self._engagement_events(principal_id=principal_id, publication_id=publication_id)
        latest_state: dict[str, dict[str, object]] = {}
        for share in shares:
            for recipient in list(share.get("recipients") or []):
                if not isinstance(recipient, dict):
                    continue
                latest_state[str(recipient.get("recipient_id") or "")] = {
                    "recipient_id": str(recipient.get("recipient_id") or ""),
                    "recipient_name": str(recipient.get("name") or ""),
                    "share_id": str(share.get("share_id") or ""),
                    "state": "shared",
                    "occurred_at": str(share.get("sent_at") or ""),
                }
        for event in reversed(engagements):
            recipient_id = str(event.get("recipient_id") or "")
            if not recipient_id:
                continue
            latest_state[recipient_id] = {
                **dict(latest_state.get(recipient_id) or {}),
                "recipient_id": recipient_id,
                "share_id": str(event.get("share_id") or ""),
                "state": str(event.get("event_type") or ""),
                "occurred_at": str(event.get("occurred_at") or ""),
            }
        states = list(latest_state.values())
        opens = sum(1 for item in states if str(item.get("state") or "") in {"opened", "clicked_property", "expanded_gallery", "saved_property"})
        responses = sum(1 for item in states if str(item.get("state") or "") in {"submitted_feedback", "requested_followup"})
        inactivity = sum(1 for item in states if str(item.get("state") or "") in {"shared", "no_activity_48h"})
        followups = self._current_followup_tasks(principal_id=principal_id, publication_id=publication_id)
        next_best_action = "share_packet"
        if any(str(item.get("recommended_action") or "") == "review_feedback" for item in followups):
            next_best_action = "review_feedback"
        elif responses:
            next_best_action = "review_feedback"
        elif opens:
            next_best_action = "request_feedback"
        elif inactivity:
            next_best_action = "resend_or_follow_up"
        elif opens >= 2:
            next_best_action = "prepare_variant_or_summary"
        return {
            "shares": shares,
            "summary": {
                "total_shares": len(shares),
                "total_recipients": len(states),
                "opened": opens,
                "responded": responses,
                "no_activity": inactivity,
                "next_best_action": next_best_action,
            },
            "recipients": states,
            "followups": followups,
        }

    def record_structured_feedback(
        self,
        *,
        principal_id: str,
        property_ref: str,
        stakeholder_id: str,
        stakeholder_label: str = "",
        publication_id: str = "",
        share_id: str = "",
        audience_type: str = "",
        category: str,
        sentiment: str = "",
        importance: int = 3,
        text: str = "",
        source: str = "packet",
        source_event_id: str = "",
        decision_state: str = "",
        followup_status: str = "",
        actor: str = "browser",
    ) -> dict[str, object]:
        normalized_category = str(category or "").strip().lower()
        if normalized_category not in {"love", "concern", "dealbreaker", "question", "priority", "compare_request"}:
            raise ValueError("invalid_property_feedback_category")
        normalized_decision_state = str(decision_state or "").strip().lower()
        if normalized_decision_state and normalized_decision_state not in {
            "unseen",
            "seen",
            "interested",
            "maybe",
            "rejected",
            "viewing_requested",
            "documents_requested",
            "offer_candidate",
            "archived",
        }:
            raise ValueError("invalid_property_feedback_decision_state")
        normalized_followup_status = str(followup_status or "").strip().lower()
        if normalized_followup_status and normalized_followup_status not in {
            "suggested",
            "asked",
            "answered",
            "needs_follow_up",
            "confirmed",
            "contradicted",
            "resolved",
        }:
            raise ValueError("invalid_property_feedback_followup_status")
        feedback = {
            "feedback_id": f"fbk_{uuid4().hex}",
            "property_ref": str(property_ref or "").strip(),
            "stakeholder_id": str(stakeholder_id or "").strip() or "stakeholder",
            "stakeholder_label": str(stakeholder_label or stakeholder_id or "Stakeholder").strip()[:160],
            "publication_id": str(publication_id or "").strip(),
            "share_id": str(share_id or "").strip(),
            "audience_type": str(audience_type or "").strip()[:80],
            "category": normalized_category,
            "sentiment": str(sentiment or normalized_category).strip()[:80],
            "importance": max(1, min(int(importance or 3), 5)),
            "text": str(text or "").strip()[:2000],
            "source": str(source or "packet").strip()[:80],
            "source_event_id": str(source_event_id or "").strip()[:160],
            "decision_state": normalized_decision_state,
            "followup_status": normalized_followup_status or ("asked" if normalized_category == "question" else ""),
            "created_at": now_utc_iso(),
        }
        self._repo.record_event(
            {
                "publication_id": str(publication_id or "").strip(),
                "principal_id": principal_id,
                "event_type": "property_feedback_entry_recorded",
                "actor": actor,
                "payload_json": copy.deepcopy(feedback),
            }
        )
        return feedback

    def update_feedback_followup_status(
        self,
        *,
        principal_id: str,
        feedback_id: str,
        followup_status: str,
        note: str = "",
        actor: str = "browser",
    ) -> dict[str, object]:
        target = next(
            (
                row
                for row in self.list_structured_feedback(principal_id=principal_id)
                if str(row.get("feedback_id") or "") == str(feedback_id or "").strip()
            ),
            None,
        )
        if target is None:
            raise KeyError("property_feedback_not_found")
        normalized = str(followup_status or "").strip().lower()
        if normalized not in {"asked", "answered", "needs_follow_up", "confirmed", "contradicted", "resolved"}:
            raise ValueError("invalid_property_feedback_followup_status")
        event = self._repo.record_event(
            {
                "publication_id": str(target.get("publication_id") or ""),
                "principal_id": principal_id,
                "event_type": "property_feedback_followup_updated",
                "actor": actor,
                "payload_json": {
                    "feedback_id": str(feedback_id or "").strip(),
                    "property_ref": str(target.get("property_ref") or ""),
                    "followup_status": normalized,
                    "note": str(note or "").strip()[:500],
                    "updated_at": now_utc_iso(),
                },
            }
        )
        return event

    def list_structured_feedback(
        self,
        *,
        principal_id: str,
        property_ref: str = "",
        stakeholder_id: str = "",
        publication_id: str = "",
        category: str = "",
    ) -> list[dict[str, object]]:
        cache_key = (
            str(principal_id or "").strip(),
            str(property_ref or "").strip(),
            str(stakeholder_id or "").strip(),
            str(publication_id or "").strip(),
            str(category or "").strip(),
        )
        cached = self._structured_feedback_read_cache.get(cache_key)
        if cached is not None:
            return [dict(row) for row in cached]
        rows = self._feedback_events(principal_id=principal_id, publication_id=publication_id or None)
        status_events = [
            row
            for row in self._cached_repo_events(principal_id=principal_id, limit=4000, event_type="property_feedback_followup_updated")
        ]
        latest_status_by_feedback: dict[str, dict[str, object]] = {}
        for row in status_events:
            payload = dict(row.get("payload_json") or {})
            feedback_id = str(payload.get("feedback_id") or "").strip()
            if not feedback_id:
                continue
            latest_status_by_feedback[feedback_id] = payload
        out: list[dict[str, object]] = []
        for row in rows:
            if property_ref and str(row.get("property_ref") or "") != str(property_ref or "").strip():
                continue
            if stakeholder_id and str(row.get("stakeholder_id") or "") != str(stakeholder_id or "").strip():
                continue
            if category and str(row.get("category") or "") != str(category or "").strip():
                continue
            enriched = dict(row)
            status_payload = latest_status_by_feedback.get(str(row.get("feedback_id") or "").strip())
            if status_payload:
                enriched["followup_status"] = str(status_payload.get("followup_status") or enriched.get("followup_status") or "").strip()
                enriched["followup_note"] = str(status_payload.get("note") or "").strip()
            out.append(enriched)
        self._structured_feedback_read_cache[cache_key] = [dict(row) for row in out]
        return out

    def property_household_alignment(self, *, principal_id: str, property_ref: str) -> dict[str, object]:
        rows = self.list_structured_feedback(principal_id=principal_id, property_ref=property_ref)
        stakeholder_rows: list[dict[str, object]] = []
        for stakeholder_id in {str(row.get("stakeholder_id") or "").strip() for row in rows if str(row.get("stakeholder_id") or "").strip()}:
            stakeholder_items = [row for row in rows if str(row.get("stakeholder_id") or "").strip() == stakeholder_id]
            label = str(stakeholder_items[0].get("stakeholder_label") or stakeholder_id).strip()
            categories = {str(row.get("category") or "").strip() for row in stakeholder_items}
            decision = "maybe"
            if "dealbreaker" in categories:
                decision = "no"
            elif "love" in categories or "priority" in categories:
                decision = "yes"
            reason = next((str(row.get("text") or "").strip() for row in stakeholder_items if str(row.get("text") or "").strip()), "")
            stakeholder_rows.append(
                {
                    "stakeholder_id": stakeholder_id,
                    "stakeholder_label": label,
                    "decision": decision,
                    "reason": reason or ", ".join(sorted(categories)) or "No detail yet.",
                }
            )
        if not stakeholder_rows:
            return {
                "alignment_score": 0,
                "alignment_label": "waiting",
                "stakeholders": [],
                "primary_conflicts": [],
                "next_best_question": "",
            }
        yes_total = sum(1 for row in stakeholder_rows if row["decision"] == "yes")
        maybe_total = sum(1 for row in stakeholder_rows if row["decision"] == "maybe")
        no_total = sum(1 for row in stakeholder_rows if row["decision"] == "no")
        total = len(stakeholder_rows)
        alignment_score = int(round(((yes_total + (maybe_total * 0.5)) / max(total, 1)) * 100.0))
        alignment_label = "aligned" if no_total == 0 else ("split" if yes_total > 0 else "blocked")
        conflicts = [row["reason"] for row in stakeholder_rows if row["decision"] in {"maybe", "no"} and str(row["reason"]).strip()]
        next_best_question = ""
        for row in rows:
            if str(row.get("category") or "") == "question" and str(row.get("followup_status") or "asked").strip().lower() not in {"answered", "confirmed", "resolved"}:
                next_best_question = str(row.get("text") or "").strip()
                break
        if not next_best_question:
            next_best_question = next(
                (str(row["reason"]).strip() for row in stakeholder_rows if row["decision"] in {"maybe", "no"} and str(row["reason"]).strip()),
                "",
            )
        return {
            "alignment_score": alignment_score,
            "alignment_label": alignment_label,
            "stakeholders": stakeholder_rows[:8],
            "primary_conflicts": conflicts[:3],
            "next_best_question": next_best_question,
        }

    def property_risk_signal_candidates(self, *, principal_id: str, property_ref: str = "") -> list[dict[str, object]]:
        rows = self.list_structured_feedback(principal_id=principal_id, property_ref=property_ref)
        buckets: dict[tuple[str, str], list[dict[str, object]]] = {}
        for row in rows:
            category = str(row.get("category") or "").strip().lower()
            if category not in {"concern", "dealbreaker", "question"}:
                continue
            text = " ".join(
                [
                    category,
                    str(row.get("text") or ""),
                ]
            ).lower()
            theme = "general"
            for candidate, markers in {
                "price": ("price", "budget", "cost"),
                "noise": ("noise", "street", "traffic"),
                "layout": ("layout", "floorplan", "room"),
                "documents": ("document", "floorplan", "operating cost", "energy certificate"),
                "legal": ("legal", "auction", "title", "lease"),
                "investment": ("yield", "capex", "operating cost", "liquidity"),
                "family": ("school", "playground", "family"),
            }.items():
                if any(marker in text for marker in markers):
                    theme = candidate
                    break
            buckets.setdefault((theme, category), []).append(row)
        results: list[dict[str, object]] = []
        for (theme, category), items in buckets.items():
            stakeholder_total = len({str(item.get("stakeholder_id") or "").strip() for item in items if str(item.get("stakeholder_id") or "").strip()})
            property_total = len({str(item.get("property_ref") or "").strip() for item in items if str(item.get("property_ref") or "").strip()})
            privacy_state = "eligible" if stakeholder_total >= 10 and property_total >= 3 else "suppressed"
            confidence = "high" if len(items) >= 10 else ("medium" if len(items) >= 4 else "low")
            results.append(
                {
                    "scope_type": "property" if property_ref else "portfolio",
                    "scope_ref": property_ref or "portfolio",
                    "theme": theme,
                    "reason_key": category,
                    "count": len(items),
                    "distinct_stakeholder_count": stakeholder_total,
                    "distinct_property_count": property_total,
                    "privacy_state": privacy_state,
                    "confidence": confidence,
                    "summary": compact_text(
                        next((str(item.get("text") or "").strip() for item in items if str(item.get("text") or "").strip()), f"{theme} concern"),
                        fallback=f"{theme} concern",
                        limit=220,
                    ),
                }
            )
        return sorted(results, key=lambda item: (item["privacy_state"] != "eligible", -int(item["count"])))[:8]

    def cluster_feedback(self, *, principal_id: str, property_ref: str) -> list[dict[str, object]]:
        rows = self.list_structured_feedback(principal_id=principal_id, property_ref=property_ref)
        themes: dict[str, list[dict[str, object]]] = {}
        for row in rows:
            text = " ".join(
                [
                    str(row.get("category") or ""),
                    str(row.get("text") or ""),
                ]
            ).lower()
            theme = "general"
            for candidate, markers in {
                "price": ("price", "expensive", "cost", "budget"),
                "location": ("location", "district", "far", "commute", "school"),
                "layout": ("layout", "floorplan", "rooms", "room"),
                "condition": ("condition", "renovation", "repair", "old"),
                "noise": ("noise", "traffic", "quiet"),
                "family_fit": ("family", "kids", "playground"),
            }.items():
                if any(marker in text for marker in markers):
                    theme = candidate
                    break
            themes.setdefault(theme, []).append(row)
        clusters: list[dict[str, object]] = []
        for theme, items in themes.items():
            severity = "high" if any(str(item.get("category") or "") == "dealbreaker" for item in items) else "medium"
            clusters.append(
                {
                    "cluster_id": f"clu_{uuid4().hex}",
                    "property_ref": property_ref,
                    "theme": theme,
                    "severity": severity,
                    "entry_count": len(items),
                    "summary": "; ".join(str(item.get("text") or item.get("category") or "").strip() for item in items[:3]),
                }
            )
        return sorted(clusters, key=lambda item: (item["severity"] != "high", -int(item["entry_count"])))

    def stakeholder_preferences(self, *, principal_id: str, stakeholder_id: str) -> dict[str, object]:
        rows = self.list_structured_feedback(principal_id=principal_id, stakeholder_id=stakeholder_id)
        signals: list[dict[str, object]] = []
        for row in rows:
            category = str(row.get("category") or "")
            text = str(row.get("text") or "").lower()
            if category in {"love", "priority"}:
                signals.append({"dimension": "positive_signal", "value": text or category, "confidence": 0.7, "source": category})
            if category in {"concern", "dealbreaker"}:
                signals.append({"dimension": "negative_signal", "value": text or category, "confidence": 0.85 if category == "dealbreaker" else 0.65, "source": category})
        return {
            "stakeholder_id": stakeholder_id,
            "signals": signals[:20],
            "summary": {
                "signal_total": len(signals),
                "likes": sum(1 for item in signals if item["dimension"] == "positive_signal"),
                "concerns": sum(1 for item in signals if item["dimension"] == "negative_signal"),
            },
        }

    def feedback_summary(self, *, principal_id: str, property_ref: str) -> dict[str, object]:
        rows = self.list_structured_feedback(principal_id=principal_id, property_ref=property_ref)
        counts: dict[str, int] = {}
        stakeholders: dict[str, set[str]] = {}
        decision_states: dict[str, int] = {}
        for row in rows:
            category = str(row.get("category") or "")
            counts[category] = counts.get(category, 0) + 1
            stakeholders.setdefault(str(row.get("stakeholder_id") or ""), set()).add(category)
            decision_state = str(row.get("decision_state") or "").strip().lower()
            if decision_state:
                decision_states[decision_state] = decision_states.get(decision_state, 0) + 1
        disagreement = 0
        if len(stakeholders) >= 2:
            category_sets = [value for value in stakeholders.values() if value]
            disagreement = 1 if len({tuple(sorted(item)) for item in category_sets}) > 1 else 0
        household = self.property_household_alignment(principal_id=principal_id, property_ref=property_ref)
        return {
            "property_ref": property_ref,
            "counts": counts,
            "recent_feedback": rows[:10],
            "dealbreaker_count": counts.get("dealbreaker", 0),
            "open_questions_count": counts.get("question", 0),
            "clusters": self.cluster_feedback(principal_id=principal_id, property_ref=property_ref),
            "disagreement_count": disagreement,
            "family_alignment": household.get("alignment_label") or ("split" if disagreement else "aligned"),
            "household_alignment_score": int(household.get("alignment_score") or 0),
            "household_review": household,
            "decision_state_counts": decision_states,
            "risk_signal_candidates": self.property_risk_signal_candidates(principal_id=principal_id, property_ref=property_ref),
        }

    def generate_summary_artifact(
        self,
        *,
        principal_id: str,
        subject_type: str,
        subject_id: str,
        artifact_type: str,
        audience_type: str = "family",
        actor: str = "browser",
        context_json: dict[str, object] | None = None,
    ) -> dict[str, object]:
        subject = str(subject_id or "").strip()
        artifact_kind = str(artifact_type or "").strip()
        if artifact_kind not in {"why_shortlisted", "tradeoff_summary", "what_changed", "recommended_next_step", "family_review_digest"}:
            raise ValueError("unsupported_property_summary_type")
        title = artifact_kind.replace("_", " ").title()
        property_ref = subject if subject_type == "property" else ""
        summary = self.feedback_summary(principal_id=principal_id, property_ref=property_ref) if property_ref else {}
        context = dict(context_json or {})
        opportunity = (
            dict(context.get("opportunity") or {})
            if isinstance(context.get("opportunity"), dict)
            else {}
        )
        def _brief_fragment(value: object) -> str:
            return str(value or "").strip().rstrip(" .;:")

        match_reasons = [
            _brief_fragment(value)
            for value in list(opportunity.get("match_reasons") or [])
            if _brief_fragment(value)
        ]
        mismatch_reasons = [
            _brief_fragment(value)
            for value in list(opportunity.get("mismatch_reasons") or [])
            if _brief_fragment(value)
        ]
        unknowns = [
            _brief_fragment(value)
            for value in list(opportunity.get("unknowns") or [])
            if _brief_fragment(value)
        ]
        recommendation = str(opportunity.get("recommendation") or "").strip().replace("_", " ")
        opportunity_fit = opportunity.get("fit_score")
        try:
            fit_score = max(0, min(100, round(float(opportunity_fit))))
        except (TypeError, ValueError):
            fit_score = 0
        why_parts = [
            (
                f"Why it fits: {'; '.join(match_reasons[:3])}."
            )
            if match_reasons
            else "",
            f"Preference fit: {fit_score}/100." if fit_score else "",
            f"Recommendation: {recommendation}." if recommendation else "",
            f"Watch: {'; '.join(mismatch_reasons[:2])}." if mismatch_reasons else "",
            f"Verify next: {'; '.join(unknowns[:2])}." if unknowns else "",
        ]
        contextual_why = " ".join(part for part in why_parts if part)
        contextual_tradeoffs = "; ".join((mismatch_reasons + unknowns)[:4])
        body = {
            "why_shortlisted": contextual_why or f"This home is worth sharing now. Feedback so far: {summary.get('dealbreaker_count', 0)} dealbreakers, {summary.get('open_questions_count', 0)} open questions.",
            "tradeoff_summary": (
                f"Main tradeoffs: {contextual_tradeoffs}."
                if contextual_tradeoffs
                else f"Main tradeoffs: {', '.join(cluster.get('theme') for cluster in list(summary.get('clusters') or [])[:3]) or 'No major tradeoffs captured yet.'}"
            ),
            "what_changed": "; ".join(item.get("summary") or item.get("detail") or "Page and reply status updated." for item in self.property_change_log(principal_id=principal_id, property_ref=property_ref)[:3]) or "No major change recorded yet.",
            "recommended_next_step": (
                f"Recommended next step: {recommendation}. Verify {unknowns[0]} before deciding."
                if recommendation and unknowns
                else (
                    f"Recommended next step: {recommendation}."
                    if recommendation
                    else "Check the replies here and send the next focused follow-up."
                )
            ),
            "family_review_digest": "Family digest: keep the current reason, tradeoffs, and open questions in one shared note.",
        }[artifact_kind]
        artifact = {
            "artifact_id": f"sum_{uuid4().hex}",
            "subject_type": str(subject_type or "property").strip(),
            "subject_id": subject,
            "artifact_type": artifact_kind,
            "audience_type": str(audience_type or "family").strip()[:80],
            "title": title,
            "body_markdown": body,
            "status": "ready",
            "created_by": actor,
            "created_at": now_utc_iso(),
        }
        if opportunity:
            artifact["opportunity_id"] = str(opportunity.get("opportunity_id") or "").strip()
            artifact["generation_provider"] = "PropertyQuarry"
            artifact["generation_mode"] = "local_opportunity_brief"
            artifact["generation_basis"] = "durable_preference_assessment"
        self._repo.record_event(
            {
                "publication_id": "",
                "principal_id": principal_id,
                "event_type": "property_summary_artifact_generated",
                "actor": actor,
                "payload_json": copy.deepcopy(artifact),
            }
        )
        return artifact

    def get_summary_artifact(self, *, principal_id: str, artifact_id: str) -> dict[str, object] | None:
        for artifact in self._summary_events(principal_id=principal_id):
            if str(artifact.get("artifact_id") or "") == str(artifact_id or "").strip():
                return artifact
        return None

    def attach_summary_to_packet(
        self,
        *,
        principal_id: str,
        publication_id: str,
        artifact_id: str,
        actor: str = "browser",
    ) -> dict[str, object]:
        artifact = self.get_summary_artifact(principal_id=principal_id, artifact_id=artifact_id)
        if artifact is None:
            raise KeyError("property_summary_artifact_not_found")
        event = self._repo.record_event(
            {
                "publication_id": publication_id,
                "principal_id": principal_id,
                "event_type": "packet_summary_artifact_attached",
                "actor": actor,
                "payload_json": {
                    "publication_id": publication_id,
                    "artifact_id": artifact_id,
                    "attached_at": now_utc_iso(),
                },
            }
        )
        return {"status": "attached", "event": event, "artifact": artifact}

    def attached_summaries(self, *, principal_id: str, publication_id: str) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for artifact_id in self._attached_summary_ids(principal_id=principal_id, publication_id=publication_id):
            artifact = self.get_summary_artifact(principal_id=principal_id, artifact_id=artifact_id)
            if artifact is not None:
                out.append(artifact)
        return out

    def property_change_log(self, *, principal_id: str, property_ref: str) -> list[dict[str, object]]:
        publications = [
            row
            for row in self._repo.list_publications(principal_id=principal_id, limit=500)
            if str(row.get("property_ref") or "") == str(property_ref or "").strip()
        ]
        changes: list[dict[str, object]] = []
        for publication in publications[:5]:
            publication_id = str(publication.get("publication_id") or "")
            events = self._repo.list_events(principal_id=principal_id, publication_id=publication_id, limit=50)
            for event in events[:10]:
                event_type = str(event.get("event_type") or "")
                if event_type in {"fliplink_manual_publish_completed", "fliplink_browser_publish_completed", "packet_republished"}:
                    changes.append(
                        {
                            "change_id": f"chg_{uuid4().hex}",
                            "property_ref": property_ref,
                            "change_type": event_type,
                            "summary": event_type.replace("_", " "),
                            "detail": f"{publication_id} changed at {str(event.get('created_at') or '')[:19]}",
                        }
                    )
        for feedback in self.list_structured_feedback(principal_id=principal_id, property_ref=property_ref)[:5]:
            changes.append(
                {
                    "change_id": f"chg_{uuid4().hex}",
                    "property_ref": property_ref,
                    "change_type": "feedback",
                    "summary": f"Feedback {str(feedback.get('category') or '')}",
                    "detail": str(feedback.get("text") or ""),
                }
            )
        return changes[:20]

    def create_variant(
        self,
        *,
        principal_id: str,
        publication_id: str,
        audience_type: str,
        base_variant_key: str = "default",
        title_override: str = "",
        actor: str = "browser",
    ) -> dict[str, object]:
        publication = self._repo.get_publication(publication_id=publication_id, principal_id=principal_id)
        if publication is None:
            raise KeyError("property_packet_publication_not_found")
        variant_id = f"pub_{uuid4().hex}"
        summary = dict(publication.get("packet_summary_json") or {})
        variant_row = self._repo.create_publication(
            {
                **publication,
                "publication_id": variant_id,
                "fliplink_url": "",
                "fliplink_custom_domain_url": "",
                "fliplink_embed_code": "",
                "fliplink_qr_url": "",
                "status": "rendered",
                "published_at": "",
                "archived_at": "",
                "packet_summary_json": {
                    **summary,
                    "base_publication_id": publication_id,
                    "variant_key": str(base_variant_key or "default").strip()[:120] or "default",
                    "audience_type": str(audience_type or "family").strip()[:80] or "family",
                    "variant_title": str(title_override or publication.get("recommended_title") or "").strip()[:200],
                },
                "recommended_title": str(title_override or publication.get("recommended_title") or "").strip()[:200]
                or str(publication.get("recommended_title") or ""),
            }
        )
        self._repo.record_event(
            {
                "publication_id": variant_id,
                "principal_id": principal_id,
                "event_type": "packet_variant_created",
                "actor": actor,
                "payload_json": {
                    "publication_id": variant_id,
                    "base_publication_id": publication_id,
                    "audience_type": str(audience_type or "family").strip(),
                    "variant_key": str(base_variant_key or "default").strip(),
                    "title_override": str(title_override or "").strip(),
                    "created_at": now_utc_iso(),
                },
            }
        )
        return variant_row

    def republish_publication(
        self,
        *,
        principal_id: str,
        publication_id: str,
        actor: str = "browser",
    ) -> dict[str, object]:
        publication = self._repo.get_publication(publication_id=publication_id, principal_id=principal_id)
        if publication is None:
            raise KeyError("property_packet_publication_not_found")
        summary = dict(publication.get("packet_summary_json") or {})
        revision = int(summary.get("revision_number") or 1) + 1
        updated = self._repo.update_publication(
            publication_id=publication_id,
            updates={
                "packet_summary_json": {**summary, "revision_number": revision, "republished_at": now_utc_iso()},
                "status": "published" if str(publication.get("fliplink_url") or "").strip() else str(publication.get("status") or "rendered"),
            },
        )
        self._repo.record_event(
            {
                "publication_id": publication_id,
                "principal_id": principal_id,
                "event_type": "packet_republished",
                "actor": actor,
                "payload_json": {
                    "publication_id": publication_id,
                    "revision_number": revision,
                    "republished_at": now_utc_iso(),
                },
            }
        )
        if updated is None:
            raise KeyError("property_packet_publication_not_found")
        return updated

    def list_variants(self, *, principal_id: str, publication_id: str) -> list[dict[str, object]]:
        rows = self._repo.list_publications(principal_id=principal_id, limit=500)
        return [
            row
            for row in rows
            if str(dict(row.get("packet_summary_json") or {}).get("base_publication_id") or "") == str(publication_id or "").strip()
        ]

    def share_journey(self, *, principal_id: str, publication_id: str) -> dict[str, object]:
        publication = self._repo.get_publication(publication_id=publication_id, principal_id=principal_id)
        if publication is None:
            raise KeyError("property_packet_publication_not_found")
        snapshot = self.engagement_snapshot(principal_id=principal_id, publication_id=publication_id)
        feedback = self.feedback_summary(principal_id=principal_id, property_ref=str(publication.get("property_ref") or ""))
        state = "drafted"
        if str(publication.get("status") or "") == "published":
            state = "published"
        if int(snapshot["summary"].get("opened") or 0) > 0:
            state = "opened"
        if int(snapshot["summary"].get("responded") or 0) > 0 or int(feedback.get("dealbreaker_count") or 0) > 0:
            state = "feedback_active"
        if any(str(item.get("status") or "") != "resolved" for item in list(snapshot.get("followups") or [])):
            state = "followup_needed"
        if int(feedback.get("open_questions_count") or 0) == 0 and int(snapshot["summary"].get("responded") or 0) > 0:
            state = "decision_ready"
        return {
            "publication_id": publication_id,
            "state": state,
            "variants": [
                {
                    "publication_id": str(item.get("publication_id") or ""),
                    "title": str(item.get("recommended_title") or ""),
                    "audience_type": str(dict(item.get("packet_summary_json") or {}).get("audience_type") or ""),
                    "variant_key": str(dict(item.get("packet_summary_json") or {}).get("variant_key") or ""),
                }
                for item in self.list_variants(principal_id=principal_id, publication_id=publication_id)
            ],
            "next_best_action": str(snapshot["summary"].get("next_best_action") or ""),
        }

    def assign_followup(
        self,
        *,
        principal_id: str,
        followup_id: str,
        owner: str,
        actor: str = "browser",
    ) -> dict[str, object]:
        publication_id = self._followup_publication_id(principal_id=principal_id, followup_id=followup_id)
        if not publication_id:
            raise KeyError("packet_followup_not_found")
        return self._repo.record_event(
            {
                "publication_id": publication_id,
                "principal_id": principal_id,
                "event_type": "packet_followup_task_assigned",
                "actor": actor,
                "payload_json": {
                    "task_id": followup_id,
                    "owner": str(owner or "").strip()[:160],
                    "assigned_at": now_utc_iso(),
                },
            }
        )

    def resolve_followup(
        self,
        *,
        principal_id: str,
        followup_id: str,
        resolution: str,
        actor: str = "browser",
    ) -> dict[str, object]:
        publication_id = self._followup_publication_id(principal_id=principal_id, followup_id=followup_id)
        if not publication_id:
            raise KeyError("packet_followup_not_found")
        return self._repo.record_event(
            {
                "publication_id": publication_id,
                "principal_id": principal_id,
                "event_type": "packet_followup_task_resolved",
                "actor": actor,
                "payload_json": {
                    "task_id": followup_id,
                    "resolution": str(resolution or "").strip()[:240],
                    "resolved_at": now_utc_iso(),
                },
            }
        )

    def _followup_publication_id(self, *, principal_id: str, followup_id: str) -> str:
        rows = self._repo.list_events(principal_id=principal_id, event_type="packet_followup_task_created", limit=2000)
        for row in rows:
            payload = dict(row.get("payload_json") or {})
            if str(payload.get("task_id") or "") == str(followup_id or "").strip():
                return str(row.get("publication_id") or "")
        return ""

    def stakeholder_timeline(self, *, principal_id: str, stakeholder_id: str) -> list[dict[str, object]]:
        rows = self._repo.list_events(principal_id=principal_id, limit=4000)
        out: list[dict[str, object]] = []
        for row in rows:
            payload = dict(row.get("payload_json") or {})
            if str(payload.get("stakeholder_id") or payload.get("recipient_id") or "") != str(stakeholder_id or "").strip():
                continue
            out.append(
                {
                    "event_id": str(row.get("event_id") or ""),
                    "event_type": str(row.get("event_type") or ""),
                    "publication_id": str(row.get("publication_id") or ""),
                    "summary": str(payload.get("text") or payload.get("recommended_action") or payload.get("event_type") or "").strip() or str(row.get("event_type") or ""),
                    "created_at": str(row.get("created_at") or ""),
                }
            )
        return out[:100]

    def property_timeline(self, *, principal_id: str, property_ref: str) -> list[dict[str, object]]:
        rows = self._cached_repo_events(principal_id=principal_id, limit=4000)
        out: list[dict[str, object]] = []
        publication_ids = {
            str(row.get("publication_id") or "")
            for row in self._cached_repo_publications(principal_id=principal_id, limit=500)
            if str(row.get("property_ref") or "") == str(property_ref or "").strip()
        }
        for row in rows:
            payload = dict(row.get("payload_json") or {})
            if str(payload.get("property_ref") or "") != str(property_ref or "").strip() and str(row.get("publication_id") or "") not in publication_ids:
                continue
            out.append(
                {
                    "event_id": str(row.get("event_id") or ""),
                    "event_type": str(row.get("event_type") or ""),
                    "publication_id": str(row.get("publication_id") or ""),
                    "stakeholder_id": str(payload.get("stakeholder_id") or payload.get("recipient_id") or ""),
                    "summary": " | ".join(
                        part
                        for part in (
                            str(payload.get("text") or payload.get("recommended_action") or payload.get("event_type") or "").strip() or str(row.get("event_type") or ""),
                            (
                                f"Decision {str(payload.get('decision_state') or '').strip().replace('_', ' ')}"
                                if str(payload.get("decision_state") or "").strip()
                                else ""
                            ),
                            (
                                f"Follow-up {str(payload.get('followup_status') or '').strip().replace('_', ' ')}"
                                if str(payload.get("followup_status") or "").strip()
                                else ""
                            ),
                        )
                        if str(part or "").strip()
                    ),
                    "created_at": str(row.get("created_at") or ""),
                }
            )
        return out[:200]

    def list_offers(
        self,
        *,
        principal_id: str,
        property_ref: str = "",
        publication_id: str = "",
    ) -> list[dict[str, object]]:
        contextual = bool(property_ref or publication_id)
        return [
            {
                "offer_id": "premium_market_report",
                "offer_type": "premium_report",
                "title": "Premium market report",
                "price_label": "EUR 49",
                "provider": "propertyquarry",
                "contextual": contextual,
                "status": "available",
            },
            {
                "offer_id": "concierge_shortlist_refresh",
                "offer_type": "concierge_refresh",
                "title": "Concierge shortlist refresh",
                "price_label": "EUR 99",
                "provider": "propertyquarry",
                "contextual": contextual,
                "status": "available",
            },
            {
                "offer_id": "agent_ready_export",
                "offer_type": "agent_export",
                "title": "Agent-ready export",
                "price_label": "EUR 19",
                "provider": "propertyquarry",
                "contextual": contextual,
                "status": "available",
            },
        ]

    def start_offer_checkout(
        self,
        *,
        principal_id: str,
        offer_id: str,
        property_ref: str = "",
        publication_id: str = "",
        actor: str = "browser",
    ) -> dict[str, object]:
        offer = next((item for item in self.list_offers(principal_id=principal_id, property_ref=property_ref, publication_id=publication_id) if item["offer_id"] == offer_id), None)
        if offer is None:
            raise KeyError("property_offer_not_found")
        checkout_url = f"/pricing?offer_id={offer_id}"
        event = self._repo.record_event(
            {
                "publication_id": publication_id,
                "principal_id": principal_id,
                "event_type": "property_offer_checkout_started",
                "actor": actor,
                "payload_json": {
                    "offer_id": offer_id,
                    "property_ref": property_ref,
                    "checkout_url": checkout_url,
                    "started_at": now_utc_iso(),
                },
            }
        )
        return {"status": "checkout_started", "offer": offer, "checkout_url": checkout_url, "event_id": str(event.get("event_id") or "")}

    def optimization_recommendations(self, *, principal_id: str, publication_id: str) -> list[dict[str, object]]:
        snapshot = self.engagement_snapshot(principal_id=principal_id, publication_id=publication_id)
        analytics = self.latest_analytics_snapshot(principal_id=principal_id, publication_id=publication_id)
        recommendations: list[dict[str, object]] = []
        if int(analytics.get("views") or 0) > 0 and int(snapshot["summary"].get("responded") or 0) == 0:
            recommendations.append(
                {
                    "recommendation_id": f"opt_{publication_id}_followup",
                    "recommendation_type": "followup",
                    "priority": "high",
                    "reason": "Views are present but no structured response has been captured yet.",
                    "status": "open",
                }
            )
        device_breakdown = dict(analytics.get("device_breakdown") or {})
        if int(device_breakdown.get("mobile") or 0) > int(device_breakdown.get("desktop") or 0):
            recommendations.append(
                {
                    "recommendation_id": f"opt_{publication_id}_mobile",
                    "recommendation_type": "mobile_readability",
                    "priority": "medium",
                    "reason": "Mobile traffic leads, so packet readability should be checked on phone-sized layouts.",
                    "status": "open",
                }
            )
        if not recommendations:
            recommendations.append(
                {
                    "recommendation_id": f"opt_{publication_id}_variant",
                    "recommendation_type": "variant_test",
                    "priority": "low",
                    "reason": "No major issue detected. Try a family or agent variant to learn which packet framing performs better.",
                    "status": "open",
                }
            )
        ack_events = self._repo.list_events(
            principal_id=principal_id,
            publication_id=publication_id,
            event_type="packet_optimization_acknowledged",
            limit=200,
        )
        acknowledged = {str(dict(event.get("payload_json") or {}).get("recommendation_id") or "") for event in ack_events}
        for row in recommendations:
            if row["recommendation_id"] in acknowledged:
                row["status"] = "acknowledged"
        return recommendations

    def acknowledge_optimization(
        self,
        *,
        principal_id: str,
        publication_id: str,
        recommendation_id: str,
        actor: str = "browser",
    ) -> dict[str, object]:
        return self._repo.record_event(
            {
                "publication_id": publication_id,
                "principal_id": principal_id,
                "event_type": "packet_optimization_acknowledged",
                "actor": actor,
                "payload_json": {
                    "recommendation_id": str(recommendation_id or "").strip(),
                    "acknowledged_at": now_utc_iso(),
                },
            }
        )

    def review_feedback(
        self,
        *,
        principal_id: str,
        event_id: str,
        action: str,
        note: str = "",
        actor: str = "browser",
    ) -> dict[str, object]:
        target = next(
            (
                event
                for event in self._repo.list_events(principal_id=principal_id, event_type="fliplink_lead_captured", limit=500)
                if str(event.get("event_id") or "") == str(event_id or "").strip()
            ),
            None,
        )
        if target is None:
            raise KeyError("fliplink_feedback_event_not_found")
        normalized_action = str(action or "").strip().lower()
        if normalized_action not in {
            "accept_as_preference_signal",
            "accept_as_viewing_question",
            "dismiss",
            "block_reviewer",
            "convert_to_hard_rule",
        }:
            raise ValueError("invalid_fliplink_feedback_action")
        event = self._repo.record_event(
            {
                "publication_id": str(target.get("publication_id") or ""),
                "principal_id": principal_id,
                "event_type": "fliplink_feedback_reviewed",
                "actor": actor,
                "payload_json": {
                    "target_event_id": str(event_id or "").strip(),
                    "action": normalized_action,
                    "note": str(note or "").strip()[:1000],
                    "trust": "owner_reviewed",
                    "target_payload": _review_target_payload(target),
                },
            }
        )
        if normalized_action == "accept_as_viewing_question":
            target_payload = _review_target_payload(target)
            question = str(
                dict(target_payload.get("custom_fields") or {}).get("question")
                or note
                or "Ask the agent to clarify this point during the viewing."
            ).strip()[:1000]
            viewing_event = self._repo.record_event(
                {
                    "publication_id": str(target.get("publication_id") or ""),
                    "principal_id": principal_id,
                    "event_type": "fliplink_viewing_question_accepted",
                    "actor": actor,
                    "payload_json": {
                        "target_event_id": str(event_id or "").strip(),
                        "property_ref": str(target_payload.get("property_ref") or ""),
                        "question": question,
                        "trust": "owner_reviewed",
                    },
                }
            )
            event["viewing_question_event"] = viewing_event
        if normalized_action == "block_reviewer":
            target_payload = _review_target_payload(target)
            reviewer = dict(target_payload.get("reviewer") or {})
            blocked_event = self._repo.record_event(
                {
                    "publication_id": str(target.get("publication_id") or ""),
                    "principal_id": principal_id,
                    "event_type": "fliplink_reviewer_blocked",
                    "actor": actor,
                    "payload_json": {
                        "target_event_id": str(event_id or "").strip(),
                        "email_hash": str(reviewer.get("email_hash") or ""),
                        "email_masked": str(reviewer.get("email_masked") or ""),
                        "property_ref": str(target_payload.get("property_ref") or ""),
                        "note": str(note or "").strip()[:1000],
                        "trust": "owner_reviewed",
                    },
                }
            )
            event["block_event"] = blocked_event
        return event


def _folder_for(packet_kind: PropertyPacketKind) -> str:
    return {
        PropertyPacketKind.OWNER_REVIEW: "Owner Review Packets",
        PropertyPacketKind.FAMILY_REVIEW: "Family Review Packets",
        PropertyPacketKind.AGENT_BRIEF: "Agent Briefs",
        PropertyPacketKind.SHORTLIST_BROCHURE: "Family Review Packets",
        PropertyPacketKind.PAID_MARKET_REPORT: "Paid Market Reports",
        PropertyPacketKind.OPEN_HOUSE_QR: "Family Review Packets",
    }.get(packet_kind, "Owner Review Packets")


def _non_negative_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except Exception as exc:
        raise ValueError("fliplink_analytics_metric_invalid") from exc
    if parsed < 0:
        raise ValueError("fliplink_analytics_metric_negative")
    return min(parsed, 10_000_000)


def _bounded_metric_rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, object]] = []
    for item in value[:20]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("page") or item.get("source") or "").strip()[:160]
        if not label:
            continue
        count = _non_negative_int(item.get("count") or item.get("views") or item.get("seconds"))
        rows.append({"label": label, "count": count or 0})
    return rows


def _bounded_int_map(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, int] = {}
    for key, raw in list(value.items())[:30]:
        label = str(key or "").strip()[:80]
        if not label:
            continue
        parsed = _non_negative_int(raw)
        out[label] = parsed or 0
    return out


def _review_target_payload(target: dict[str, object]) -> dict[str, object]:
    payload = dict(target.get("payload_json") or {})
    return {
        "publication_id": str(target.get("publication_id") or payload.get("publication_id") or "")[:160],
        "property_ref": str(payload.get("property_ref") or "")[:500],
        "packet_kind": str(payload.get("packet_kind") or "")[:80],
        "privacy_mode": str(payload.get("privacy_mode") or "")[:80],
        "custom_fields": safe_custom_fields(payload.get("custom_fields") or {}),
        "reviewer": {
            "name": str(payload.get("name") or "")[:160],
            "email_hash": str(payload.get("email_hash") or "")[:80],
            "email_masked": str(payload.get("email_masked") or "")[:160],
            "company": str(payload.get("company") or "")[:160],
            "job_title": str(payload.get("job_title") or "")[:160],
        },
    }


def build_fliplink_packet_service(container: AppContainer) -> FlipLinkPacketService:
    artifact_root = Path(str(container.settings.storage.artifacts_dir)).resolve()
    repo = build_property_packet_publication_repository(container.settings)
    return FlipLinkPacketService(repo=repo, artifact_root=artifact_root, orchestrator=container.orchestrator)
