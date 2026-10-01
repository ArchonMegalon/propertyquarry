from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ea"))

from app.services.propertyquarry_teable_projection import (  # noqa: E402
    PROPERTYQUARRY_TEABLE_TABLE_FIELDS,
    PROPERTYQUARRY_TEABLE_TABLE_NAMES,
)
from restore_propertyquarry_from_teable import (  # noqa: E402
    INTENTIONALLY_LOSSY_TEABLE_TABLES,
    RECOVERABLE_TEABLE_TABLES,
    TEABLE_RESTORE_CONTRACT_VERSION,
)


REQUIRED_TABLE_FIELDS: dict[str, set[str]] = {
    "propertyquarry_users": {
        "principal_id",
        "workspace_name",
        "workspace_mode",
        "selected_channels_json",
        "current_plan_key",
    },
    "propertyquarry_delivery_settings": {
        "principal_id",
        "preferred_channel",
        "preferred_label",
        "notification_scope",
        "selected_channels_json",
        "email_enabled",
        "telegram_enabled",
        "telegram_bot_status",
        "whatsapp_enabled",
        "whatsapp_notification_opt_in",
        "whatsapp_ai_support_enabled",
        "whatsapp_ai_support_phone",
        "whatsapp_ai_support_phone_last4",
        "whatsapp_ai_support_purpose",
        "signal_status",
        "settings_json",
    },
    "propertyquarry_subscriptions": {
        "principal_id",
        "current_plan_key",
        "status",
        "active_until",
        "last_order_id",
        "last_payment_status",
    },
    "propertyquarry_preferences": {
        "principal_id",
        "country_code",
        "listing_mode",
        "property_type",
        "location_query",
        "selected_platforms_json",
        "preferences_json",
    },
    "propertyquarry_search_agents": {
        "principal_id",
        "agent_id",
        "name",
        "enabled",
        "location_query",
        "preferences_json",
    },
    "propertyquarry_saved_shortlist": {
        "principal_id",
        "property_ref",
        "candidate_ref",
        "property_url",
        "title",
        "fit_score",
        "saved_from_run_id",
        "facts_json",
        "candidate_json",
    },
    "propertyquarry_search_runs": {
        "principal_id",
        "run_id",
        "status",
        "listing_total",
        "high_fit_total",
        "summary_json",
    },
    "propertyquarry_provider_sources": {
        "principal_id",
        "run_id",
        "source_label",
        "source_url",
        "provider_cache_key",
        "source_json",
    },
    "propertyquarry_properties": {
        "property_ref",
        "property_url",
        "listing_id",
        "title",
        "facts_json",
        "last_seen_run_id",
    },
    "propertyquarry_property_evaluations": {
        "principal_id",
        "run_id",
        "property_ref",
        "fit_score",
        "review_url",
        "tour_url",
        "facts_json",
    },
    "propertyquarry_review_artifacts": {
        "principal_id",
        "run_id",
        "property_ref",
        "review_url",
        "review_status",
        "tour_status",
        "artifact_json",
    },
    "propertyquarry_shared_artifacts": {
        "principal_id",
        "run_id",
        "property_ref",
        "artifact_kind",
        "artifact_url",
        "artifact_status",
        "visibility",
        "artifact_json",
    },
    "propertyquarry_research_tasks": {
        "principal_id",
        "run_id",
        "task_id",
        "status",
        "property_ref",
        "task_json",
    },
    "propertyquarry_decision_ledger": {
        "principal_id",
        "decision_id",
        "property_ref",
        "decision_state",
        "reason_keys_json",
    },
    "propertyquarry_evidence_claims": {
        "principal_id",
        "claim_id",
        "property_ref",
        "claim_type",
        "claim_text",
        "verification_state",
    },
    "propertyquarry_agent_questions": {
        "principal_id",
        "task_id",
        "property_ref",
        "question_text",
        "status",
    },
    "propertyquarry_documents": {
        "principal_id",
        "document_id",
        "property_ref",
        "document_type",
        "verification_state",
        "extracted_claims_json",
    },
}


def _field_names(table_name: str) -> set[str]:
    return {
        str(field.get("name") or "").strip()
        for field in PROPERTYQUARRY_TEABLE_TABLE_FIELDS.get(table_name, [])
        if isinstance(field, dict) and str(field.get("name") or "").strip()
    }


def main() -> int:
    declared_tables = set(PROPERTYQUARRY_TEABLE_TABLE_NAMES)
    recoverable_tables = set(RECOVERABLE_TEABLE_TABLES)
    intentionally_lossy_tables = set(INTENTIONALLY_LOSSY_TEABLE_TABLES)
    failures: list[str] = []
    for table_name, required_fields in sorted(REQUIRED_TABLE_FIELDS.items()):
        if table_name not in declared_tables:
            failures.append(f"missing_table:{table_name}")
            continue
        missing_fields = sorted(required_fields - _field_names(table_name))
        if missing_fields:
            failures.append(f"missing_fields:{table_name}:{','.join(missing_fields)}")
    extra_missing_config = sorted(declared_tables - set(PROPERTYQUARRY_TEABLE_TABLE_FIELDS))
    for table_name in extra_missing_config:
        failures.append(f"missing_field_config:{table_name}")
    uncategorized_tables = sorted(declared_tables - recoverable_tables - intentionally_lossy_tables)
    for table_name in uncategorized_tables:
        failures.append(f"uncategorized_restore_table:{table_name}")
    missing_declared_tables = sorted((recoverable_tables | intentionally_lossy_tables) - declared_tables)
    for table_name in missing_declared_tables:
        failures.append(f"restore_contract_unknown_table:{table_name}")
    overlap_tables = sorted(recoverable_tables & intentionally_lossy_tables)
    for table_name in overlap_tables:
        failures.append(f"restore_contract_ambiguous_table:{table_name}")
    missing_recoverable_contract = sorted((set(REQUIRED_TABLE_FIELDS) - intentionally_lossy_tables) - recoverable_tables)
    for table_name in missing_recoverable_contract:
        failures.append(f"required_table_not_recoverable:{table_name}")
    payload = {
        "contract_name": "propertyquarry.teable_portability.v1",
        "status": "pass" if not failures else "fail",
        "required_table_count": len(REQUIRED_TABLE_FIELDS),
        "declared_table_count": len(declared_tables),
        "restore_contract_version": TEABLE_RESTORE_CONTRACT_VERSION,
        "recoverable_table_count": len(recoverable_tables),
        "intentionally_lossy_tables": dict(sorted(INTENTIONALLY_LOSSY_TEABLE_TABLES.items())),
        "new_host_resume": {
            "operator_edits": [
                "PROPERTYQUARRY_TEABLE_API_KEY",
            ],
            "accepted_credential_aliases": [
                "TEABLE_API_KEY",
            ],
            "optional_overrides": [
                "TEABLE_BASE_URL",
                "PROPERTYQUARRY_TEABLE_BASE_ID",
                "PROPERTYQUARRY_TEABLE_TENANT_NAME",
            ],
            "base_discovery": (
                "When PROPERTYQUARRY_TEABLE_BASE_ID is unset, the restore command discovers the default "
                "PropertyQuarry Teable base by name."
            ),
            "restore_command": (
                "PYTHONPATH=ea python3 scripts/restore_propertyquarry_from_teable.py "
                "--principal-id <principal-id> --apply"
            ),
            "recoverable": sorted(recoverable_tables),
            "intentionally_lost": dict(sorted(INTENTIONALLY_LOSSY_TEABLE_TABLES.items())),
            "result_policy": "saved results, review/share artifacts, decisions, documents, agents, preferences, delivery settings, and subscriptions must restore; live runs and provider-source diagnostics may be lost",
            "delivery_recovery_fields": sorted(REQUIRED_TABLE_FIELDS["propertyquarry_delivery_settings"]),
        },
        "failures": failures,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
