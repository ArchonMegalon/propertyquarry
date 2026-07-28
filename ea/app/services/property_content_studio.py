from __future__ import annotations

import hashlib
import json
import os
from uuid import uuid4

from app.domain.property.content_source_packet import canonical_json, now_utc_iso, sha256_json, source_packet_sha256
from app.services.property_content_job_ledger import PropertyContentJobLedger
from app.services.property_content_validation import (
    script_receipt_validation,
    validate_property_content_script,
    validate_property_content_source_packet,
)
from app.services.subscribr_client import SubscribrClient, redacted_subscribr_error, subscribr_enabled


def script_markdown_sha256(markdown: str) -> str:
    return hashlib.sha256(str(markdown or "").encode("utf-8")).hexdigest()


def property_content_job_lease_seconds() -> int:
    try:
        value = int(str(os.getenv("PROPERTYQUARRY_CONTENT_JOB_LEASE_SECONDS") or "900").strip() or "900")
    except (TypeError, ValueError):
        value = 900
    return max(60, min(value, 3600))


class PropertyContentStudio:
    def __init__(
        self,
        *,
        ledger: PropertyContentJobLedger | None = None,
        client: SubscribrClient | None = None,
    ) -> None:
        self._ledger = ledger or PropertyContentJobLedger()
        self._client = client or SubscribrClient()

    @property
    def ledger(self) -> PropertyContentJobLedger:
        return self._ledger

    def validate_source_packet(self, packet: dict[str, object]) -> dict[str, object]:
        return validate_property_content_source_packet(packet)

    @staticmethod
    def _assert_packet_ownership(
        packet: dict[str, object],
        *,
        ownership_scope: str,
        search_run_id: str,
    ) -> None:
        normalized_scope = str(ownership_scope or "").strip().lower()
        normalized_run_id = str(search_run_id or "").strip()
        snapshot = packet.get("property_snapshot")
        packet_run_id = str(
            dict(snapshot).get("run_id") if isinstance(snapshot, dict) else ""
        ).strip()
        if normalized_scope == "search_run" and (
            not normalized_run_id or packet_run_id != normalized_run_id
        ):
            raise ValueError("property_content_packet_run_owner_mismatch")

    def prepare_source_packet(
        self,
        packet: dict[str, object],
        *,
        principal_id: str,
        ownership_scope: str,
        search_run_id: str,
    ) -> dict[str, object]:
        self._assert_packet_ownership(
            packet,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
        )
        report = self.validate_source_packet(packet)
        status = "SOURCE_PACKET_APPROVED" if report["status"] == "pass" else "SOURCE_REJECTED"
        return self._ledger.upsert_job(
            packet,
            principal_id=principal_id,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
            status=status,
            extra={
                "validation_status": str(report["status"]),
                "validation_report": report,
                "provider_status": "not_requested",
            },
        )

    def request_subscribr_script(
        self,
        packet: dict[str, object],
        *,
        principal_id: str,
        ownership_scope: str,
        search_run_id: str,
        channel_id: str | int = "",
    ) -> dict[str, object]:
        self._assert_packet_ownership(
            packet,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
        )
        packet_id = str(packet.get("packet_id") or "").strip()
        ownership = {
            "principal_id": principal_id,
            "ownership_scope": ownership_scope,
            "search_run_id": search_run_id,
        }
        existing = self._ledger.get_job(packet_id, **ownership)
        if existing and str(existing.get("provider_script_id") or ""):
            return {**existing, "idempotent": True}
        if existing and (
            str(existing.get("status") or "") == "PROVIDER_RECONCILIATION_REQUIRED"
            or str(existing.get("provider_status") or "") == "outcome_unknown"
        ):
            return {
                **existing,
                "idempotent": True,
                "claim_status": "manual_reconciliation_required",
            }
        report = self.validate_source_packet(packet)
        if report["status"] != "pass":
            return self._ledger.upsert_job(
                packet,
                **ownership,
                status="SOURCE_REJECTED",
                extra={"validation_status": "fail", "validation_report": report, "provider_status": "blocked"},
            )
        if not subscribr_enabled():
            return self._ledger.upsert_job(
                packet,
                **ownership,
                status="SOURCE_PACKET_APPROVED",
                extra={
                    "validation_status": "pass",
                    "validation_report": report,
                    "provider_status": "disabled",
                    "provider_disabled_reason": "PROPERTYQUARRY_SUBSCRIBR_ENABLED and PROPERTYQUARRY_SUBSCRIBR_API_ENABLED are required",
                },
            )
        if not self._client.configured:
            return self._ledger.upsert_job(
                packet,
                **ownership,
                status="PROVIDER_FAILED",
                extra={
                    "validation_status": "pass",
                    "validation_report": report,
                    "provider_status": "blocked",
                    "provider_error": {"detail": "subscribr_token_not_configured"},
                },
            )
        queued = self._ledger.upsert_job(
            packet,
            **ownership,
            status="PROVIDER_REQUEST_QUEUED",
            extra={
                "validation_status": "pass",
                "validation_report": report,
                "provider_status": "queued",
            },
        )
        lease_owner = f"subscribr-request:{os.getpid()}:{uuid4().hex}"
        claimed = self._ledger.claim_job(
            packet_id,
            **ownership,
            lease_owner=lease_owner,
            lease_seconds=property_content_job_lease_seconds(),
        )
        if claimed is None:
            current = self._ledger.get_job(packet_id, **ownership) or queued
            return {**current, "idempotent": True, "claim_status": "owned_by_other_replica"}
        if str(claimed.get("provider_script_id") or "").strip():
            released = self._ledger.update_claimed_job(
                packet_id,
                **ownership,
                lease_owner=lease_owner,
                status=str(claimed.get("status") or "PROVIDER_JOB_CREATED"),
                release=True,
            )
            return {**released, "idempotent": True}
        if bool(claimed.get("claim_recovered")) and str(claimed.get("provider_dispatch_started_at") or "").strip():
            reconciled = self._ledger.update_claimed_job(
                packet_id,
                **ownership,
                lease_owner=lease_owner,
                status="PROVIDER_RECONCILIATION_REQUIRED",
                extra={
                    "provider_status": "outcome_unknown",
                    "provider_error": {"detail": "subscribr_dispatch_outcome_unknown_after_crash"},
                },
                release=True,
            )
            return {**reconciled, "idempotent": True, "claim_status": "recovered_without_resend"}
        provider_idempotency_key = "subscribr-property-content:" + hashlib.sha256(
            str(claimed.get("idempotency_key") or "").encode("utf-8")
        ).hexdigest()
        with self._ledger.publication_authority(
            principal_id=principal_id,
            search_run_id=search_run_id,
        ) as authority_connection:
            self._ledger.update_claimed_job(
                packet_id,
                **ownership,
                lease_owner=lease_owner,
                status="PROVIDER_DISPATCHING",
                extra={
                    "provider_status": "dispatching",
                    "provider_dispatch_started_at": now_utc_iso(),
                    "provider_idempotency_key": provider_idempotency_key,
                },
                release=False,
                authority_connection=authority_connection,
            )
            idea_id = ""
            script_id = ""
            try:
                idea = self._client.create_idea(
                    channel_id=channel_id or str(packet.get("subscribr_channel_key") or ""),
                    payload={
                        "title": str(packet.get("title") or ""),
                        "description": canonical_json(packet)[:8000],
                    },
                )
                idea_id = idea.get("id") or idea.get("idea_id") or dict(idea.get("idea") or {}).get("id")
                self._ledger.update_claimed_job(
                    packet_id,
                    **ownership,
                    lease_owner=lease_owner,
                    status="PROVIDER_IDEA_CREATED",
                    extra={
                        "provider_status": "dispatching",
                        "provider_dispatch_stage": "create_script",
                        "provider_idea_id": str(idea_id or ""),
                    },
                    release=False,
                    authority_connection=authority_connection,
                )
                if not idea_id:
                    raise RuntimeError("subscribr_idea_id_missing")
                script = self._client.create_script(
                    channel_id=channel_id or str(packet.get("subscribr_channel_key") or ""),
                    payload={
                        "title": str(packet.get("title") or ""),
                        "brief": canonical_json(packet),
                        "target_words": packet.get("target_words") or 750,
                    },
                )
                script_id = script.get("id") or script.get("script_id") or dict(script.get("script") or {}).get("id")
                self._ledger.update_claimed_job(
                    packet_id,
                    **ownership,
                    lease_owner=lease_owner,
                    status="PROVIDER_SCRIPT_CREATED",
                    extra={
                        "provider_status": "dispatching",
                        "provider_dispatch_stage": "generate_script",
                        "provider_idea_id": str(idea_id or ""),
                        "provider_script_id": str(script_id or ""),
                    },
                    release=False,
                    authority_connection=authority_connection,
                )
                if not script_id:
                    raise RuntimeError("subscribr_script_id_missing")
                self._client.generate_script(script_id=script_id, payload={"research": False})
                return self._ledger.record_provider_ids(
                    packet_id=packet_id,
                    **ownership,
                    provider_channel_id=channel_id or str(packet.get("subscribr_channel_key") or ""),
                    provider_idea_id=idea_id,
                    provider_script_id=script_id,
                    status="PROVIDER_GENERATING",
                    lease_owner=lease_owner,
                    authority_connection=authority_connection,
                )
            except Exception as exc:
                return self._ledger.update_claimed_job(
                    packet_id,
                    **ownership,
                    lease_owner=lease_owner,
                    status="PROVIDER_RECONCILIATION_REQUIRED",
                    extra={
                        "validation_status": "pass",
                        "validation_report": report,
                        "provider_status": "outcome_unknown",
                        "provider_idea_id": str(idea_id or ""),
                        "provider_script_id": str(script_id or ""),
                        "provider_error": redacted_subscribr_error(exc),
                    },
                    release=True,
                    authority_connection=authority_connection,
                )

    def materialize_script_receipt(
        self,
        *,
        packet: dict[str, object],
        markdown: str,
        principal_id: str,
        ownership_scope: str,
        search_run_id: str,
        provider_channel_id: object = "",
        provider_idea_id: object = "",
        provider_script_id: object = "",
    ) -> dict[str, object]:
        self._assert_packet_ownership(
            packet,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
        )
        source_report = validate_property_content_source_packet(packet)
        script_report = validate_property_content_script(packet, markdown)
        receipt = {
            "contract_name": "propertyquarry.subscribr_script_draft.v1",
            "status": "review_required" if source_report["status"] == "pass" and script_report["status"] == "pass" else "validation_failed",
            "provider": "subscribr",
            "account_tier": "AppSumo Tier 7 / Scale 3",
            "packet_id": str(packet.get("packet_id") or ""),
            "content_mode": str(packet.get("content_mode") or ""),
            "jurisdiction": str(packet.get("jurisdiction") or ""),
            "channel_key": str(packet.get("subscribr_channel_key") or ""),
            "provider_channel_id": provider_channel_id,
            "provider_idea_id": provider_idea_id,
            "provider_script_id": provider_script_id,
            "source_packet_sha256": str(packet.get("source_packet_sha256") or source_packet_sha256(packet)),
            "listing_snapshot_sha256": str(dict(packet.get("property_snapshot") or {}).get("snapshot_sha256") or ""),
            "research_packet_sha256": sha256_json(packet.get("sources") or []),
            "run_id": str(dict(packet.get("property_snapshot") or {}).get("run_id") or ""),
            "candidate_ref": str(dict(packet.get("property_snapshot") or {}).get("candidate_ref") or ""),
            "export_format": "markdown",
            "script_sha256": script_markdown_sha256(markdown),
            "validation": script_receipt_validation(packet, markdown),
            "validation_report": {"source": source_report, "script": script_report},
            "human_review": {"status": "pending", "reviewer": "", "reviewed_at": ""},
            "production_allowed": False,
            "publication_allowed": False,
            "created_at": now_utc_iso(),
        }
        path = self._ledger.write_receipt(
            packet=packet,
            receipt=receipt,
            principal_id=principal_id,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
            status=(
                "HUMAN_REVIEW_REQUIRED"
                if receipt["status"] == "review_required"
                else "PROPERTY_VALIDATION"
            ),
            extra={
                "script_sha256": receipt["script_sha256"],
                "validation_status": receipt["status"],
                "validation_report": receipt["validation_report"],
                "provider_channel_id": provider_channel_id,
                "provider_idea_id": provider_idea_id,
                "provider_script_id": provider_script_id,
            },
        )
        return {**receipt, "receipt_path": str(path)}

    def studio_snapshot(self, *, principal_id: str) -> dict[str, object]:
        rows = self._ledger.list_jobs(principal_id=principal_id, limit=100)
        return {
            "enabled": subscribr_enabled(),
            "operator_ui_enabled": str(os.getenv("PROPERTYQUARRY_SUBSCRIBR_OPERATOR_UI_ENABLED") or "").strip().lower()
            in {"1", "true", "yes", "on"},
            "jobs": rows[:100],
            "ledger_path": str(self._ledger.path),
            "ledger_backend": self._ledger.backend,
            "job_count": len(rows),
        }

    def ingest_completed_script(
        self,
        *,
        packet: dict[str, object],
        event_payload: dict[str, object],
        markdown: str,
        principal_id: str,
        ownership_scope: str,
        search_run_id: str,
    ) -> dict[str, object]:
        return self.materialize_script_receipt(
            packet=packet,
            markdown=markdown,
            principal_id=principal_id,
            ownership_scope=ownership_scope,
            search_run_id=search_run_id,
            provider_channel_id=event_payload.get("channel_id") or event_payload.get("channelId") or "",
            provider_idea_id=event_payload.get("idea_id") or event_payload.get("ideaId") or "",
            provider_script_id=event_payload.get("script_id") or event_payload.get("scriptId") or "",
        )
