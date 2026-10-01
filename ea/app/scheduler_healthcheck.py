from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time


def _float_env(name: str, default: float) -> float:
    raw = str(os.environ.get(name) or "").strip()
    try:
        value = float(raw) if raw else default
    except Exception:
        value = default
    return max(1.0, value)


def _bool_env(name: str, default: bool) -> bool:
    raw = str(os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _sha256_text(value: object) -> bool:
    normalized = str(value or "")
    return len(normalized) == 64 and all(
        character in "0123456789abcdef" for character in normalized
    )


def _propertyquarry_ooda_witness_required(*, role: str) -> bool:
    return bool(
        role == "scheduler"
        and str(
            os.environ.get("PROPERTYQUARRY_SCHEDULER_PROFILE") or ""
        ).strip().lower()
        == "property_only"
        and _bool_env("PROPERTYQUARRY_OODA_NOTIFICATION_ENABLED", True)
    )


def _verify_propertyquarry_ooda_witness() -> dict[str, object]:
    from scripts import propertyquarry_ooda_scheduler_witness as witness

    return witness.verify_current_scheduler_iteration(
        receipt_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SCHEDULER_ITERATION_RECEIPT_PATH"
                )
                or witness.DEFAULT_RECEIPT_PATH
            ).strip()
        ),
        cycle_receipt_path=Path(
            str(
                os.environ.get("PROPERTYQUARRY_OODA_NOTIFICATION_RECEIPT_PATH")
                or witness.DEFAULT_CYCLE_RECEIPT_PATH
            ).strip()
        ),
        max_age_seconds=witness.configured_max_age_seconds(),
    )


def _verify_propertyquarry_ooda_source_refresh_request() -> dict[str, object]:
    from scripts import propertyquarry_ooda_source_refresh_request as source_refresh

    cycle_receipt_path = Path(
        str(
            os.environ.get("PROPERTYQUARRY_OODA_NOTIFICATION_RECEIPT_PATH")
            or "/data/artifacts/propertyquarry-ooda-notification/latest.json"
        ).strip()
    )
    approval_manifest_path = Path(
        str(
            os.environ.get("PROPERTYQUARRY_OODA_APPROVAL_MANIFEST_PATH")
            or "/run/propertyquarry/ooda-signals/manifest.json"
        ).strip()
    )
    max_age_seconds = max(
        60.0,
        min(
            _float_env(
                "EA_SCHEDULER_PROPERTYQUARRY_OODA_APPROVAL_MAX_AGE_SECONDS",
                source_refresh.DEFAULT_MAX_AGE_SECONDS,
            ),
            86400.0,
        ),
    )
    return source_refresh.inspect_current_source_refresh_request_bundle(
        cycle_receipt_path=cycle_receipt_path,
        signal_dir=approval_manifest_path.parent,
        request_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_REQUEST_PATH"
                )
                or source_refresh.DEFAULT_REQUEST_PATH
            ).strip()
        ),
        verification_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_VERIFICATION_PATH"
                )
                or source_refresh.DEFAULT_VERIFICATION_PATH
            ).strip()
        ),
        max_age_seconds=max_age_seconds,
    )


def _verify_propertyquarry_ooda_source_refresh_handoff() -> dict[str, object]:
    from scripts import propertyquarry_ooda_source_refresh_handoff as source_handoff
    from scripts import propertyquarry_ooda_source_refresh_request as source_refresh

    cycle_receipt_path = Path(
        str(
            os.environ.get("PROPERTYQUARRY_OODA_NOTIFICATION_RECEIPT_PATH")
            or "/data/artifacts/propertyquarry-ooda-notification/latest.json"
        ).strip()
    )
    approval_manifest_path = Path(
        str(
            os.environ.get("PROPERTYQUARRY_OODA_APPROVAL_MANIFEST_PATH")
            or "/run/propertyquarry/ooda-signals/manifest.json"
        ).strip()
    )
    max_age_seconds = max(
        60.0,
        min(
            _float_env(
                "EA_SCHEDULER_PROPERTYQUARRY_OODA_APPROVAL_MAX_AGE_SECONDS",
                source_handoff.DEFAULT_MAX_AGE_SECONDS,
            ),
            86400.0,
        ),
    )
    return source_handoff.inspect_current_source_refresh_handoff_bundle(
        cycle_receipt_path=cycle_receipt_path,
        signal_dir=approval_manifest_path.parent,
        request_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_REQUEST_PATH"
                )
                or source_refresh.DEFAULT_REQUEST_PATH
            ).strip()
        ),
        request_verification_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_VERIFICATION_PATH"
                )
                or source_refresh.DEFAULT_VERIFICATION_PATH
            ).strip()
        ),
        handoff_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_HANDOFF_PATH"
                )
                or source_handoff.DEFAULT_HANDOFF_PATH
            ).strip()
        ),
        verification_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_HANDOFF_VERIFICATION_PATH"
                )
                or source_handoff.DEFAULT_VERIFICATION_PATH
            ).strip()
        ),
        max_age_seconds=max_age_seconds,
    )


def _verify_propertyquarry_ooda_source_refresh_claims() -> dict[str, object]:
    from scripts import propertyquarry_ooda_source_refresh_claims as source_claims
    from scripts import propertyquarry_ooda_source_refresh_handoff as source_handoff
    from scripts import propertyquarry_ooda_source_refresh_request as source_refresh

    cycle_receipt_path = Path(
        str(
            os.environ.get("PROPERTYQUARRY_OODA_NOTIFICATION_RECEIPT_PATH")
            or "/data/artifacts/propertyquarry-ooda-notification/latest.json"
        ).strip()
    )
    approval_manifest_path = Path(
        str(
            os.environ.get("PROPERTYQUARRY_OODA_APPROVAL_MANIFEST_PATH")
            or "/run/propertyquarry/ooda-signals/manifest.json"
        ).strip()
    )
    max_age_seconds = max(
        60.0,
        min(
            _float_env(
                "EA_SCHEDULER_PROPERTYQUARRY_OODA_APPROVAL_MAX_AGE_SECONDS",
                source_claims.DEFAULT_MAX_AGE_SECONDS,
            ),
            86400.0,
        ),
    )
    return source_claims.inspect_current_claim_lifecycle_bundle(
        cycle_receipt_path=cycle_receipt_path,
        signal_dir=approval_manifest_path.parent,
        request_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_REQUEST_PATH"
                )
                or source_refresh.DEFAULT_REQUEST_PATH
            ).strip()
        ),
        request_verification_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_VERIFICATION_PATH"
                )
                or source_refresh.DEFAULT_VERIFICATION_PATH
            ).strip()
        ),
        handoff_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_HANDOFF_PATH"
                )
                or source_handoff.DEFAULT_HANDOFF_PATH
            ).strip()
        ),
        handoff_verification_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_HANDOFF_VERIFICATION_PATH"
                )
                or source_handoff.DEFAULT_VERIFICATION_PATH
            ).strip()
        ),
        trust_registry_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_CLAIM_TRUST_REGISTRY_PATH"
                )
                or "/config/"
                "propertyquarry_ooda_source_refresh_producer_trust.v1.json"
            ).strip()
        ),
        claim_dir=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_CLAIM_DIR"
                )
                or "/run/propertyquarry/ooda-producer-claims"
            ).strip()
        ),
        receipt_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_CLAIMS_RECEIPT_PATH"
                )
                or "/data/artifacts/propertyquarry-ooda-notification/"
                "source-refresh-claims.json"
            ).strip()
        ),
        verification_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_CLAIMS_VERIFICATION_PATH"
                )
                or "/data/artifacts/propertyquarry-ooda-notification/"
                "source-refresh-claims-verification.json"
            ).strip()
        ),
        require_claim_dir=True,
        max_age_seconds=max_age_seconds,
    )


def _verify_propertyquarry_ooda_source_refresh_settlement() -> dict[str, object]:
    from scripts import propertyquarry_ooda_source_refresh_settlement as settlement

    cycle_receipt_path = Path(
        str(
            os.environ.get("PROPERTYQUARRY_OODA_NOTIFICATION_RECEIPT_PATH")
            or "/data/artifacts/propertyquarry-ooda-notification/latest.json"
        ).strip()
    )
    approval_manifest_path = Path(
        str(
            os.environ.get("PROPERTYQUARRY_OODA_APPROVAL_MANIFEST_PATH")
            or "/run/propertyquarry/ooda-signals/manifest.json"
        ).strip()
    )
    max_age_seconds = max(
        60.0,
        min(
            _float_env(
                "EA_SCHEDULER_PROPERTYQUARRY_OODA_APPROVAL_MAX_AGE_SECONDS",
                settlement.DEFAULT_MAX_AGE_SECONDS,
            ),
            86400.0,
        ),
    )
    return settlement.inspect_current_settlement_bundle(
        cycle_receipt_path=cycle_receipt_path,
        signal_dir=approval_manifest_path.parent,
        trust_registry_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_CLAIM_TRUST_REGISTRY_PATH"
                )
                or "/config/"
                "propertyquarry_ooda_source_refresh_producer_trust.v1.json"
            ).strip()
        ),
        completion_dir=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_COMPLETION_DIR"
                )
                or "/run/propertyquarry/ooda-producer-completions"
            ).strip()
        ),
        receipt_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_SETTLEMENT_RECEIPT_PATH"
                )
                or "/data/artifacts/propertyquarry-ooda-notification/"
                "source-refresh-settlement.json"
            ).strip()
        ),
        verification_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_SETTLEMENT_VERIFICATION_PATH"
                )
                or "/data/artifacts/propertyquarry-ooda-notification/"
                "source-refresh-settlement-verification.json"
            ).strip()
        ),
        require_completion_dir=True,
        max_age_seconds=max_age_seconds,
    )


def _verify_propertyquarry_ooda_source_refresh_trust_intake() -> dict[str, object]:
    from scripts import propertyquarry_ooda_source_refresh_claims as source_claims
    from scripts import propertyquarry_ooda_source_refresh_trust_intake as trust_intake

    max_age_seconds = max(
        60.0,
        min(
            _float_env(
                "EA_SCHEDULER_PROPERTYQUARRY_OODA_APPROVAL_MAX_AGE_SECONDS",
                trust_intake.DEFAULT_MAX_AGE_SECONDS,
            ),
            86400.0,
        ),
    )
    return trust_intake.inspect_trust_intake_bundle(
        claim_verification_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_CLAIMS_VERIFICATION_PATH"
                )
                or source_claims.DEFAULT_VERIFICATION_PATH
            ).strip()
        ),
        trust_registry_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_CLAIM_TRUST_REGISTRY_PATH"
                )
                or source_claims.DEFAULT_TRUST_REGISTRY_PATH
            ).strip()
        ),
        candidate_dir=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_CANDIDATE_DIR"
                )
                or trust_intake.DEFAULT_CANDIDATE_DIR
            ).strip()
        ),
        receipt_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_RECEIPT_PATH"
                )
                or trust_intake.DEFAULT_RECEIPT_PATH
            ).strip()
        ),
        verification_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_VERIFICATION_PATH"
                )
                or trust_intake.DEFAULT_VERIFICATION_PATH
            ).strip()
        ),
        max_age_seconds=max_age_seconds,
    )


def _verify_propertyquarry_ooda_source_refresh_trust_candidate_import(
    intake_report: dict[str, object],
) -> dict[str, object]:
    from scripts import propertyquarry_ooda_source_refresh_trust_candidate_import as candidate_import
    from scripts import propertyquarry_ooda_source_refresh_trust_intake as trust_intake

    report = candidate_import.inspect_candidate_import_readiness(
        intake_report,
        candidate_dir=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_CANDIDATE_DIR"
                )
                or trust_intake.DEFAULT_CANDIDATE_DIR
            ).strip()
        ),
        import_dir=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_CANDIDATE_IMPORT_DIR"
                )
                or candidate_import.DEFAULT_IMPORT_DIR
            ).strip()
        ),
        source_discovery_dir=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_CANDIDATE_SOURCE_DIR"
                )
                or candidate_import.DEFAULT_SOURCE_DISCOVERY_DIR
            ).strip()
        ),
    )
    return candidate_import.apply_candidate_import_presentation_state(
        report,
        state_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_CANDIDATE_IMPORT_PRESENTATION_STATE_PATH"
                )
                or candidate_import.DEFAULT_PRESENTATION_STATE_PATH
            ).strip()
        ),
    )


def _verify_propertyquarry_ooda_source_refresh_trust_candidate_artifact_request(
    candidate_import_report: dict[str, object],
) -> dict[str, object]:
    from scripts import propertyquarry_ooda_source_refresh_trust_candidate_artifact_request as artifact_request

    return artifact_request.inspect_candidate_artifact_request(
        candidate_import_report,
        receipt_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_CANDIDATE_ARTIFACT_REQUEST_RECEIPT_PATH"
                )
                or artifact_request.DEFAULT_RECEIPT_PATH
            ).strip()
        ),
        max_age_seconds=max(
            60.0,
            min(
                _float_env(
                    "EA_SCHEDULER_PROPERTYQUARRY_OODA_APPROVAL_MAX_AGE_SECONDS",
                    artifact_request.DEFAULT_MAX_AGE_SECONDS,
                ),
                86400.0,
            ),
        ),
    )


def _verify_propertyquarry_ooda_source_refresh_trust_candidate_manual_action(
    candidate_import_report: dict[str, object],
    artifact_request_verification: dict[str, object],
) -> dict[str, object]:
    from scripts import propertyquarry_ooda_source_refresh_trust_candidate_manual_action as manual_action

    return manual_action.inspect_candidate_manual_action(
        candidate_import_report,
        artifact_request_verification,
        receipt_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_CANDIDATE_MANUAL_ACTION_RECEIPT_PATH"
                )
                or manual_action.DEFAULT_RECEIPT_PATH
            ).strip()
        ),
        max_age_seconds=max(
            60.0,
            min(
                _float_env(
                    "EA_SCHEDULER_PROPERTYQUARRY_OODA_APPROVAL_MAX_AGE_SECONDS",
                    manual_action.DEFAULT_MAX_AGE_SECONDS,
                ),
                86400.0,
            ),
        ),
    )


def _verify_propertyquarry_ooda_source_refresh_trust_candidate_artifact_notification(
    candidate_import_report: dict[str, object],
    artifact_request_verification: dict[str, object],
    manual_action_verification: dict[str, object],
) -> dict[str, object]:
    from scripts import propertyquarry_ooda_source_refresh_trust_candidate_artifact_notification as notification

    return notification.inspect_candidate_artifact_notification(
        candidate_import_report,
        artifact_request_verification,
        manual_action_verification,
        receipt_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_CANDIDATE_ARTIFACT_NOTIFICATION_RECEIPT_PATH"
                )
                or notification.DEFAULT_RECEIPT_PATH
            ).strip()
        ),
        max_age_seconds=max(
            60.0,
            min(
                _float_env(
                    "EA_SCHEDULER_PROPERTYQUARRY_OODA_APPROVAL_MAX_AGE_SECONDS",
                    notification.DEFAULT_MAX_AGE_SECONDS,
                ),
                86400.0,
            ),
        ),
    )


def _verify_propertyquarry_ooda_source_refresh_trust_decision() -> dict[str, object]:
    from scripts import propertyquarry_ooda_source_refresh_claims as source_claims
    from scripts import propertyquarry_ooda_source_refresh_trust_decision as trust_decision
    from scripts import propertyquarry_ooda_source_refresh_trust_intake as trust_intake

    return trust_decision.verify_current_candidate_review_decision(
        decision_dir=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_DECISION_DIR"
                )
                or trust_decision.DEFAULT_DECISION_DIR
            ).strip()
        ),
        claim_verification_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_CLAIMS_VERIFICATION_PATH"
                )
                or source_claims.DEFAULT_VERIFICATION_PATH
            ).strip()
        ),
        trust_registry_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_CLAIM_TRUST_REGISTRY_PATH"
                )
                or source_claims.DEFAULT_TRUST_REGISTRY_PATH
            ).strip()
        ),
        candidate_dir=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_CANDIDATE_DIR"
                )
                or trust_intake.DEFAULT_CANDIDATE_DIR
            ).strip()
        ),
        intake_receipt_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_RECEIPT_PATH"
                )
                or trust_intake.DEFAULT_RECEIPT_PATH
            ).strip()
        ),
        intake_verification_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_VERIFICATION_PATH"
                )
                or trust_intake.DEFAULT_VERIFICATION_PATH
            ).strip()
        ),
    )


def _verify_propertyquarry_ooda_source_refresh_trust_notification() -> dict[str, object]:
    from scripts import propertyquarry_ooda_source_refresh_claims as source_claims
    from scripts import propertyquarry_ooda_source_refresh_trust_decision as trust_decision
    from scripts import propertyquarry_ooda_source_refresh_trust_intake as trust_intake
    from scripts import propertyquarry_ooda_source_refresh_trust_notification as trust_notification

    max_age_seconds = max(
        60.0,
        min(
            _float_env(
                "EA_SCHEDULER_PROPERTYQUARRY_OODA_APPROVAL_MAX_AGE_SECONDS",
                trust_notification.DEFAULT_MAX_AGE_SECONDS,
            ),
            86400.0,
        ),
    )
    return trust_notification.inspect_current_candidate_notification(
        decision_dir=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_DECISION_DIR"
                )
                or trust_decision.DEFAULT_DECISION_DIR
            ).strip()
        ),
        claim_verification_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_CLAIMS_VERIFICATION_PATH"
                )
                or source_claims.DEFAULT_VERIFICATION_PATH
            ).strip()
        ),
        trust_registry_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_CLAIM_TRUST_REGISTRY_PATH"
                )
                or source_claims.DEFAULT_TRUST_REGISTRY_PATH
            ).strip()
        ),
        candidate_dir=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_CANDIDATE_DIR"
                )
                or trust_intake.DEFAULT_CANDIDATE_DIR
            ).strip()
        ),
        intake_receipt_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_RECEIPT_PATH"
                )
                or trust_intake.DEFAULT_RECEIPT_PATH
            ).strip()
        ),
        intake_verification_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_VERIFICATION_PATH"
                )
                or trust_intake.DEFAULT_VERIFICATION_PATH
            ).strip()
        ),
        presentation_state_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_PRESENTATION_STATE_PATH"
                )
                or trust_notification.DEFAULT_PRESENTATION_STATE_PATH
            ).strip()
        ),
        receipt_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_NOTIFICATION_RECEIPT_PATH"
                )
                or trust_notification.DEFAULT_RECEIPT_PATH
            ).strip()
        ),
        max_age_seconds=max_age_seconds,
    )


def _verify_propertyquarry_ooda_source_refresh_trust_enrollment_preview() -> dict[str, object]:
    from scripts import propertyquarry_ooda_source_refresh_claims as source_claims
    from scripts import propertyquarry_ooda_source_refresh_trust_decision as trust_decision
    from scripts import propertyquarry_ooda_source_refresh_trust_enrollment_preview as trust_enrollment_preview
    from scripts import propertyquarry_ooda_source_refresh_trust_intake as trust_intake

    max_age_seconds = max(
        60.0,
        min(
            _float_env(
                "EA_SCHEDULER_PROPERTYQUARRY_OODA_APPROVAL_MAX_AGE_SECONDS",
                trust_enrollment_preview.DEFAULT_MAX_AGE_SECONDS,
            ),
            86400.0,
        ),
    )
    return trust_enrollment_preview.inspect_current_enrollment_preview(
        decision_dir=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_DECISION_DIR"
                )
                or trust_decision.DEFAULT_DECISION_DIR
            ).strip()
        ),
        claim_verification_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_CLAIMS_VERIFICATION_PATH"
                )
                or source_claims.DEFAULT_VERIFICATION_PATH
            ).strip()
        ),
        trust_registry_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_CLAIM_TRUST_REGISTRY_PATH"
                )
                or source_claims.DEFAULT_TRUST_REGISTRY_PATH
            ).strip()
        ),
        candidate_dir=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_CANDIDATE_DIR"
                )
                or trust_intake.DEFAULT_CANDIDATE_DIR
            ).strip()
        ),
        intake_receipt_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_RECEIPT_PATH"
                )
                or trust_intake.DEFAULT_RECEIPT_PATH
            ).strip()
        ),
        intake_verification_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_VERIFICATION_PATH"
                )
                or trust_intake.DEFAULT_VERIFICATION_PATH
            ).strip()
        ),
        receipt_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_ENROLLMENT_PREVIEW_RECEIPT_PATH"
                )
                or trust_enrollment_preview.DEFAULT_RECEIPT_PATH
            ).strip()
        ),
        verification_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_ENROLLMENT_PREVIEW_VERIFICATION_PATH"
                )
                or trust_enrollment_preview.DEFAULT_VERIFICATION_PATH
            ).strip()
        ),
        max_age_seconds=max_age_seconds,
    )


def _verify_propertyquarry_ooda_source_refresh_trust_enrollment_authorization() -> dict[str, object]:
    from scripts import propertyquarry_ooda_source_refresh_claims as source_claims
    from scripts import propertyquarry_ooda_source_refresh_trust_decision as trust_decision
    from scripts import propertyquarry_ooda_source_refresh_trust_enrollment_authorization as trust_enrollment_authorization
    from scripts import propertyquarry_ooda_source_refresh_trust_enrollment_preview as trust_enrollment_preview
    from scripts import propertyquarry_ooda_source_refresh_trust_intake as trust_intake

    max_age_seconds = max(
        60.0,
        min(
            _float_env(
                "EA_SCHEDULER_PROPERTYQUARRY_OODA_APPROVAL_MAX_AGE_SECONDS",
                trust_enrollment_preview.DEFAULT_MAX_AGE_SECONDS,
            ),
            86400.0,
        ),
    )
    return trust_enrollment_authorization.verify_current_enrollment_authorization(
        decision_dir=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_ENROLLMENT_AUTHORIZATION_DIR"
                )
                or trust_enrollment_authorization.DEFAULT_DECISION_DIR
            ).strip()
        ),
        preview_decision_dir=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_DECISION_DIR"
                )
                or trust_decision.DEFAULT_DECISION_DIR
            ).strip()
        ),
        claim_verification_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_CLAIMS_VERIFICATION_PATH"
                )
                or source_claims.DEFAULT_VERIFICATION_PATH
            ).strip()
        ),
        trust_registry_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_CLAIM_TRUST_REGISTRY_PATH"
                )
                or source_claims.DEFAULT_TRUST_REGISTRY_PATH
            ).strip()
        ),
        candidate_dir=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_CANDIDATE_DIR"
                )
                or trust_intake.DEFAULT_CANDIDATE_DIR
            ).strip()
        ),
        intake_receipt_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_RECEIPT_PATH"
                )
                or trust_intake.DEFAULT_RECEIPT_PATH
            ).strip()
        ),
        intake_verification_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_VERIFICATION_PATH"
                )
                or trust_intake.DEFAULT_VERIFICATION_PATH
            ).strip()
        ),
        preview_receipt_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_ENROLLMENT_PREVIEW_RECEIPT_PATH"
                )
                or trust_enrollment_preview.DEFAULT_RECEIPT_PATH
            ).strip()
        ),
        preview_verification_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_ENROLLMENT_PREVIEW_VERIFICATION_PATH"
                )
                or trust_enrollment_preview.DEFAULT_VERIFICATION_PATH
            ).strip()
        ),
        max_age_seconds=max_age_seconds,
    )


def _verify_propertyquarry_ooda_source_refresh_trust_enrollment_execution_readiness() -> dict[str, object]:
    from scripts import propertyquarry_ooda_source_refresh_claims as source_claims
    from scripts import propertyquarry_ooda_source_refresh_trust_decision as trust_decision
    from scripts import propertyquarry_ooda_source_refresh_trust_enrollment_authorization as trust_enrollment_authorization
    from scripts import propertyquarry_ooda_source_refresh_trust_enrollment_execution_readiness as execution_readiness
    from scripts import propertyquarry_ooda_source_refresh_trust_enrollment_preview as trust_enrollment_preview
    from scripts import propertyquarry_ooda_source_refresh_trust_intake as trust_intake

    max_age_seconds = max(
        60.0,
        min(
            _float_env(
                "EA_SCHEDULER_PROPERTYQUARRY_OODA_APPROVAL_MAX_AGE_SECONDS",
                execution_readiness.DEFAULT_MAX_AGE_SECONDS,
            ),
            86400.0,
        ),
    )
    return execution_readiness.inspect_current_execution_readiness(
        authorization_dir=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_ENROLLMENT_AUTHORIZATION_DIR"
                )
                or trust_enrollment_authorization.DEFAULT_DECISION_DIR
            ).strip()
        ),
        preview_decision_dir=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_DECISION_DIR"
                )
                or trust_decision.DEFAULT_DECISION_DIR
            ).strip()
        ),
        claim_verification_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_CLAIMS_VERIFICATION_PATH"
                )
                or source_claims.DEFAULT_VERIFICATION_PATH
            ).strip()
        ),
        trust_registry_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_CLAIM_TRUST_REGISTRY_PATH"
                )
                or source_claims.DEFAULT_TRUST_REGISTRY_PATH
            ).strip()
        ),
        candidate_dir=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_CANDIDATE_DIR"
                )
                or trust_intake.DEFAULT_CANDIDATE_DIR
            ).strip()
        ),
        intake_receipt_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_RECEIPT_PATH"
                )
                or trust_intake.DEFAULT_RECEIPT_PATH
            ).strip()
        ),
        intake_verification_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_INTAKE_VERIFICATION_PATH"
                )
                or trust_intake.DEFAULT_VERIFICATION_PATH
            ).strip()
        ),
        preview_receipt_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_ENROLLMENT_PREVIEW_RECEIPT_PATH"
                )
                or trust_enrollment_preview.DEFAULT_RECEIPT_PATH
            ).strip()
        ),
        preview_verification_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_ENROLLMENT_PREVIEW_VERIFICATION_PATH"
                )
                or trust_enrollment_preview.DEFAULT_VERIFICATION_PATH
            ).strip()
        ),
        receipt_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_ENROLLMENT_EXECUTION_READINESS_RECEIPT_PATH"
                )
                or execution_readiness.DEFAULT_RECEIPT_PATH
            ).strip()
        ),
        verification_path=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_ENROLLMENT_EXECUTION_READINESS_VERIFICATION_PATH"
                )
                or execution_readiness.DEFAULT_VERIFICATION_PATH
            ).strip()
        ),
        max_age_seconds=max_age_seconds,
    )


def _verify_propertyquarry_ooda_source_refresh_trust_enrollment_execution(
    readiness_report: dict[str, object],
) -> dict[str, object]:
    from scripts import propertyquarry_ooda_source_refresh_trust_enrollment_execution as execution

    return execution.inspect_execution_for_readiness(
        readiness_report,
        execution_dir=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_ENROLLMENT_EXECUTION_DIR"
                )
                or execution.DEFAULT_EXECUTION_DIR
            ).strip()
        ),
        backup_dir=Path(
            str(
                os.environ.get(
                    "PROPERTYQUARRY_OODA_SOURCE_REFRESH_TRUST_ENROLLMENT_BACKUP_DIR"
                )
                or execution.DEFAULT_BACKUP_DIR
            ).strip()
        ),
    )


def main() -> int:
    role = str(os.environ.get("EA_ROLE") or "").strip().lower()
    if role not in {"scheduler", "worker"}:
        return 0
    ooda_iteration_status = ""
    ooda_source_refresh_status = ""
    ooda_source_handoff_status = ""
    ooda_source_claims_status = ""
    ooda_source_settlement_status = ""
    ooda_source_trust_intake_status = ""
    ooda_source_trust_candidate_import_status = ""
    ooda_source_trust_candidate_artifact_request_status = ""
    ooda_source_trust_candidate_manual_action_status = ""
    ooda_source_trust_candidate_artifact_notification_status = ""
    ooda_source_trust_decision_status = ""
    ooda_source_trust_notification_status = ""
    ooda_source_trust_enrollment_preview_status = ""
    ooda_source_trust_enrollment_authorization_status = ""
    ooda_source_trust_enrollment_execution_readiness_status = ""
    ooda_source_trust_enrollment_execution_status = ""
    env_prefix = "SCHEDULER" if role == "scheduler" else "WORKER"
    default_path = f"/data/artifacts/propertyquarry-{role}-heartbeat.json"
    path = Path(
        str(
            os.environ.get(f"EA_{env_prefix}_HEARTBEAT_PATH")
            or default_path
        ).strip()
    )
    max_age_seconds = _float_env(f"EA_{env_prefix}_HEARTBEAT_MAX_AGE_SECONDS", 900.0)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        observed_epoch = float(payload.get("epoch") or 0.0)
        observed_role = str(payload.get("role") or "").strip().lower()
        observed_pid = int(payload.get("pid") or 0)
    except Exception:
        print(f"{role} heartbeat unavailable: {path}", file=sys.stderr)
        return 1
    if observed_role != role:
        print(
            f"{role} heartbeat role mismatch: observed={observed_role or 'missing'} path={path}",
            file=sys.stderr,
        )
        return 1
    if observed_pid <= 0:
        print(f"{role} heartbeat pid missing: path={path}", file=sys.stderr)
        return 1
    try:
        os.kill(observed_pid, 0)
    except (OSError, ValueError):
        print(f"{role} heartbeat process unavailable: pid={observed_pid} path={path}", file=sys.stderr)
        return 1
    age_seconds = time.time() - observed_epoch
    if age_seconds < 0 or age_seconds > max_age_seconds:
        print(
            f"{role} heartbeat stale: age={age_seconds:.1f}s max={max_age_seconds:.1f}s path={path}",
            file=sys.stderr,
        )
        return 1
    if _propertyquarry_ooda_witness_required(role=role):
        try:
            witness = _verify_propertyquarry_ooda_witness()
        except Exception:
            print(
                "scheduler PropertyQuarry OODA iteration witness unavailable",
                file=sys.stderr,
            )
            return 1
        if not (
            witness.get("status") == "verified"
            and witness.get("persistent_reevaluation_verified") is True
            and dict(witness.get("progress") or {}).get(
                "current_evidence_verified"
            )
            is True
            and dict(witness.get("progress") or {}).get(
                "cycle_binding_verified"
            )
            is True
            and witness.get("deployment_or_restart_authorized") is False
            and witness.get("protected_operation_executed") is False
            and witness.get("provider_quota_consumption_allowed") is False
        ):
            print(
                "scheduler PropertyQuarry OODA iteration witness not current: "
                f"status={witness.get('status') or 'blocked'} "
                f"iteration={witness.get('iteration_status') or 'unavailable'} "
                f"reason={witness.get('blocking_reason') or 'not_completed'}",
                file=sys.stderr,
            )
            return 1
        ooda_iteration_status = str(
            witness.get("iteration_status") or "completed"
        )
        try:
            source_settlement = (
                _verify_propertyquarry_ooda_source_refresh_settlement()
            )
        except Exception:
            print(
                "scheduler PropertyQuarry OODA source refresh settlement unavailable",
                file=sys.stderr,
            )
            return 1
        completion_directory = source_settlement.get("completion_directory")
        if not (
            source_settlement.get("status") == "verified"
            and source_settlement.get("settlement_state")
            in {
                "no_prior_work",
                "settled_attributed",
                "completion_verified_awaiting_current_evidence",
                "claimed_awaiting_completion",
                "current_evidence_verified_unattributed",
                "unclaimed",
                "producer_trust_unconfigured",
                "producer_trust_incomplete",
            }
            and dict(source_settlement.get("progress") or {}).get(
                "current_evidence_verified"
            )
            is True
            and dict(source_settlement.get("progress") or {}).get(
                "settlement_integrity_verified"
            )
            is True
            and dict(source_settlement.get("progress") or {}).get(
                "current_source_binding_verified"
            )
            is True
            and dict(source_settlement.get("progress") or {}).get(
                "completion_directory_binding_verified"
            )
            is True
            and source_settlement.get("settlement_receipt_persisted") is True
            and source_settlement.get("verification_receipt_persisted") is True
            and isinstance(completion_directory, dict)
            and completion_directory.get("required") is True
            and completion_directory.get("present") is True
            and source_settlement.get("action_required") is False
            and source_settlement.get("interrupt_operator") is False
            and source_settlement.get("completion_confers_authority") is False
            and source_settlement.get("claim_confers_authority") is False
            and source_settlement.get("producer_dispatch_authorized") is False
            and source_settlement.get("producer_refresh_authorized") is False
            and source_settlement.get("automatic_source_refresh_allowed") is False
            and source_settlement.get("automatic_execution_allowed") is False
            and source_settlement.get("execution_authorized") is False
            and source_settlement.get("deployment_or_restart_authorized") is False
            and source_settlement.get("protected_operation_executed") is False
            and source_settlement.get("provider_quota_consumption_allowed") is False
            and source_settlement.get("delivery_authorized") is False
            and source_settlement.get("delivery_attempted") is False
            and source_settlement.get("sent") is False
        ):
            print(
                "scheduler PropertyQuarry OODA source refresh settlement not current: "
                f"status={source_settlement.get('status') or 'blocked'} "
                f"state={source_settlement.get('settlement_state') or 'unavailable'} "
                f"reason={source_settlement.get('blocking_reason') or 'not_verified'}",
                file=sys.stderr,
            )
            return 1
        ooda_source_settlement_status = str(
            source_settlement.get("settlement_state") or "verified"
        )
        try:
            source_refresh = (
                _verify_propertyquarry_ooda_source_refresh_request()
            )
        except Exception:
            print(
                "scheduler PropertyQuarry OODA source refresh request unavailable",
                file=sys.stderr,
            )
            return 1
        if not (
            source_refresh.get("status") == "verified"
            and source_refresh.get("request_state")
            in {"producer_refresh_staged", "current_sources_verified"}
            and dict(source_refresh.get("progress") or {}).get(
                "current_evidence_verified"
            )
            is True
            and dict(source_refresh.get("progress") or {}).get(
                "request_integrity_verified"
            )
            is True
            and dict(source_refresh.get("progress") or {}).get(
                "source_binding_verified"
            )
            is True
            and source_refresh.get("request_receipt_persisted") is True
            and source_refresh.get("verification_receipt_persisted") is True
            and source_refresh.get("action_required") is False
            and source_refresh.get("interrupt_operator") is False
            and source_refresh.get("producer_dispatch_authorized") is False
            and source_refresh.get("producer_refresh_authorized") is False
            and source_refresh.get("deployment_or_restart_authorized") is False
            and source_refresh.get("protected_operation_executed") is False
            and source_refresh.get("provider_quota_consumption_allowed") is False
            and source_refresh.get("delivery_authorized") is False
        ):
            print(
                "scheduler PropertyQuarry OODA source refresh request not current: "
                f"status={source_refresh.get('status') or 'blocked'} "
                f"state={source_refresh.get('request_state') or 'unavailable'} "
                f"reason={source_refresh.get('blocking_reason') or 'not_verified'}",
                file=sys.stderr,
            )
            return 1
        ooda_source_refresh_status = str(
            source_refresh.get("request_state") or "verified"
        )
        try:
            source_handoff = (
                _verify_propertyquarry_ooda_source_refresh_handoff()
            )
        except Exception:
            print(
                "scheduler PropertyQuarry OODA source refresh handoff unavailable",
                file=sys.stderr,
            )
            return 1
        if not (
            source_handoff.get("status") == "verified"
            and source_handoff.get("handoff_state")
            in {"producer_pickup_available", "current_sources_verified"}
            and dict(source_handoff.get("progress") or {}).get(
                "current_evidence_verified"
            )
            is True
            and dict(source_handoff.get("progress") or {}).get(
                "handoff_integrity_verified"
            )
            is True
            and dict(source_handoff.get("progress") or {}).get(
                "request_binding_verified"
            )
            is True
            and source_handoff.get("handoff_receipt_persisted") is True
            and source_handoff.get("verification_receipt_persisted") is True
            and source_handoff.get("action_required") is False
            and source_handoff.get("interrupt_operator") is False
            and source_handoff.get("handoff_confers_authority") is False
            and source_handoff.get("producer_claim_recorded") is False
            and source_handoff.get("producer_dispatch_authorized") is False
            and source_handoff.get("producer_refresh_authorized") is False
            and source_handoff.get("deployment_or_restart_authorized") is False
            and source_handoff.get("protected_operation_executed") is False
            and source_handoff.get("provider_quota_consumption_allowed") is False
            and source_handoff.get("delivery_authorized") is False
        ):
            print(
                "scheduler PropertyQuarry OODA source refresh handoff not current: "
                f"status={source_handoff.get('status') or 'blocked'} "
                f"state={source_handoff.get('handoff_state') or 'unavailable'} "
                f"reason={source_handoff.get('blocking_reason') or 'not_verified'}",
                file=sys.stderr,
            )
            return 1
        ooda_source_handoff_status = str(
            source_handoff.get("handoff_state") or "verified"
        )
        try:
            source_claims = (
                _verify_propertyquarry_ooda_source_refresh_claims()
            )
        except Exception:
            print(
                "scheduler PropertyQuarry OODA source refresh claims unavailable",
                file=sys.stderr,
            )
            return 1
        claim_directory = source_claims.get("claim_directory")
        if not (
            source_claims.get("status") == "verified"
            and source_claims.get("claim_state")
            in {
                "unclaimed",
                "partially_claimed",
                "claimed",
                "not_required",
                "producer_trust_unconfigured",
                "producer_trust_incomplete",
            }
            and source_claims.get("settlement_state")
            in {
                "awaiting_current_evidence",
                "awaiting_producer_trust",
                "current_evidence_verified",
            }
            and dict(source_claims.get("progress") or {}).get(
                "current_evidence_verified"
            )
            is True
            and dict(source_claims.get("progress") or {}).get(
                "lifecycle_integrity_verified"
            )
            is True
            and dict(source_claims.get("progress") or {}).get(
                "handoff_binding_verified"
            )
            is True
            and source_claims.get("lifecycle_receipt_persisted") is True
            and source_claims.get("verification_receipt_persisted") is True
            and isinstance(claim_directory, dict)
            and claim_directory.get("required") is True
            and claim_directory.get("present") is True
            and source_claims.get("action_required") is False
            and source_claims.get("interrupt_operator") is False
            and source_claims.get("claim_confers_authority") is False
            and source_claims.get("producer_dispatch_authorized") is False
            and source_claims.get("producer_refresh_authorized") is False
            and source_claims.get("automatic_source_refresh_allowed") is False
            and source_claims.get("automatic_execution_allowed") is False
            and source_claims.get("execution_authorized") is False
            and source_claims.get("deployment_or_restart_authorized") is False
            and source_claims.get("protected_operation_executed") is False
            and source_claims.get("provider_quota_consumption_allowed") is False
            and source_claims.get("delivery_authorized") is False
            and source_claims.get("delivery_attempted") is False
            and source_claims.get("sent") is False
        ):
            print(
                "scheduler PropertyQuarry OODA source refresh claims not current: "
                f"status={source_claims.get('status') or 'blocked'} "
                f"state={source_claims.get('claim_state') or 'unavailable'} "
                f"reason={source_claims.get('blocking_reason') or 'not_verified'}",
                file=sys.stderr,
            )
            return 1
        ooda_source_claims_status = str(
            source_claims.get("claim_state") or "verified"
        )
        try:
            source_trust_intake = (
                _verify_propertyquarry_ooda_source_refresh_trust_intake()
            )
        except Exception:
            print(
                "scheduler PropertyQuarry OODA source refresh trust intake unavailable",
                file=sys.stderr,
            )
            return 1
        candidates = source_trust_intake.get("candidates")
        candidate_directory = source_trust_intake.get("candidate_directory")
        candidate_action_required = bool(candidates)
        if not (
            source_trust_intake.get("status") == "verified"
            and source_trust_intake.get("intake_state")
            in {
                "not_required",
                "awaiting_producer_public_key_evidence",
                "candidate_ready_for_operator_review",
            }
            and isinstance(candidates, list)
            and isinstance(candidate_directory, dict)
            and candidate_directory.get("present") is True
            and candidate_directory.get("current_candidate_file_count")
            == len(candidates)
            and dict(source_trust_intake.get("progress") or {}).get(
                "current_evidence_verified"
            )
            is True
            and dict(source_trust_intake.get("progress") or {}).get(
                "claim_binding_verified"
            )
            is True
            and dict(source_trust_intake.get("progress") or {}).get(
                "trust_registry_binding_verified"
            )
            is True
            and dict(source_trust_intake.get("progress") or {}).get(
                "intake_integrity_verified"
            )
            is True
            and source_trust_intake.get("intake_receipt_persisted") is True
            and source_trust_intake.get("verification_receipt_persisted") is True
            and source_trust_intake.get("action_required")
            is candidate_action_required
            and source_trust_intake.get("interrupt_operator")
            is candidate_action_required
            and source_trust_intake.get("operator_review_required")
            is candidate_action_required
            and source_trust_intake.get("public_key_candidate_recorded")
            is candidate_action_required
            and source_trust_intake.get("private_key_material_requested") is False
            and source_trust_intake.get("private_key_material_recorded") is False
            and source_trust_intake.get("trust_enrollment_authorized") is False
            and source_trust_intake.get("trust_registry_modified") is False
            and source_trust_intake.get("producer_dispatch_authorized") is False
            and source_trust_intake.get("producer_refresh_authorized") is False
            and source_trust_intake.get("automatic_execution_allowed") is False
            and source_trust_intake.get("execution_authorized") is False
            and source_trust_intake.get("deployment_or_restart_authorized") is False
            and source_trust_intake.get("protected_operation_executed") is False
            and source_trust_intake.get("provider_quota_consumption_allowed") is False
            and source_trust_intake.get("delivery_authorized") is False
            and source_trust_intake.get("delivery_attempted") is False
            and source_trust_intake.get("sent") is False
        ):
            print(
                "scheduler PropertyQuarry OODA source refresh trust intake not current: "
                f"status={source_trust_intake.get('status') or 'blocked'} "
                f"state={source_trust_intake.get('intake_state') or 'unavailable'} "
                f"reason={source_trust_intake.get('blocking_reason') or 'not_verified'}",
                file=sys.stderr,
            )
            return 1
        ooda_source_trust_intake_status = str(
            source_trust_intake.get("intake_state") or "verified"
        )
        try:
            source_trust_candidate_import = (
                _verify_propertyquarry_ooda_source_refresh_trust_candidate_import(
                    source_trust_intake
                )
            )
        except Exception:
            print(
                "scheduler PropertyQuarry OODA source refresh trust candidate import inspection unavailable",
                file=sys.stderr,
            )
            return 1
        candidate_import_state = str(
            source_trust_candidate_import.get("import_state") or ""
        )
        artifact_action_states = {
            "awaiting_external_artifact",
            "external_artifact_not_admissible",
            "ready_for_manual_import",
            "recovery_required",
        }
        candidate_import_action = candidate_import_state in {
            *artifact_action_states,
            "succeeded",
            "recovery_required",
        }
        candidate_import_presentation = dict(
            source_trust_candidate_import.get("presentation") or {}
        )
        candidate_import_presentation_state = str(
            candidate_import_presentation.get("state") or ""
        )
        candidate_import_interrupt = candidate_import_presentation_state in {
            "novel",
            "reminder_due",
        }
        candidate_import_review = candidate_import_state == "succeeded"
        if not (
            source_trust_candidate_import.get("status") == "verified"
            and candidate_import_state
            in {
                "awaiting_external_artifact",
                "external_artifact_not_admissible",
                "ready_for_manual_import",
                "not_required",
                "succeeded",
                "recovery_required",
            }
            and dict(source_trust_candidate_import.get("progress") or {}).get(
                "current_evidence_verified"
            )
            is True
            and source_trust_candidate_import.get("action_required")
            is candidate_import_action
            and source_trust_candidate_import.get("interrupt_operator")
            is (
                candidate_import_interrupt
                if candidate_import_state in artifact_action_states
                else False
            )
            and (
                candidate_import_state in artifact_action_states
                and candidate_import_presentation_state
                in {"novel", "reminder_due", "already_presented"}
                and candidate_import_presentation.get("already_presented")
                is (candidate_import_presentation_state == "already_presented")
                and candidate_import_presentation.get("reminder_due")
                is (candidate_import_presentation_state == "reminder_due")
                or candidate_import_state not in artifact_action_states
                and candidate_import_presentation_state == "not_required"
            )
            and source_trust_candidate_import.get("operator_review_required")
            is candidate_import_review
            and source_trust_candidate_import.get(
                "candidate_import_authorized"
            )
            is False
            and source_trust_candidate_import.get("trust_enrollment_authorized")
            is False
            and source_trust_candidate_import.get("trust_registry_modified")
            is False
            and source_trust_candidate_import.get(
                "private_key_material_requested"
            )
            is False
            and source_trust_candidate_import.get(
                "private_key_material_recorded"
            )
            is False
            and source_trust_candidate_import.get(
                "provider_quota_consumption_allowed"
            )
            is False
            and source_trust_candidate_import.get("delivery_authorized")
            is False
            and source_trust_candidate_import.get("delivery_attempted")
            is False
            and source_trust_candidate_import.get("sent") is False
        ):
            print(
                "scheduler PropertyQuarry OODA source refresh trust candidate import not current: "
                f"status={source_trust_candidate_import.get('status') or 'blocked'} "
                f"state={candidate_import_state or 'unavailable'} "
                f"reason={source_trust_candidate_import.get('blocking_reason') or 'not_verified'}",
                file=sys.stderr,
            )
            return 1
        ooda_source_trust_candidate_import_status = candidate_import_state
        try:
            source_trust_candidate_artifact_request = (
                _verify_propertyquarry_ooda_source_refresh_trust_candidate_artifact_request(
                    source_trust_candidate_import
                )
            )
        except Exception:
            print(
                "scheduler PropertyQuarry OODA source refresh trust candidate "
                "artifact request unavailable",
                file=sys.stderr,
            )
            return 1
        artifact_request_state = str(
            source_trust_candidate_artifact_request.get("request_state")
            or ""
        )
        artifact_request_staged = candidate_import_state in {
            "awaiting_external_artifact",
            "external_artifact_not_admissible",
        }
        artifact_request_progress = dict(
            source_trust_candidate_artifact_request.get("progress") or {}
        )
        if not (
            source_trust_candidate_artifact_request.get("status")
            == "verified"
            and artifact_request_state == candidate_import_state
            and source_trust_candidate_artifact_request.get(
                "artifact_request_staged"
            )
            is artifact_request_staged
            and source_trust_candidate_artifact_request.get("action_required")
            is artifact_request_staged
            and source_trust_candidate_artifact_request.get(
                "interrupt_operator"
            )
            is False
            and _sha256_text(
                source_trust_candidate_artifact_request.get(
                    "artifact_request_receipt_sha256"
                )
            )
            and source_trust_candidate_artifact_request.get(
                "receipt_persisted"
            )
            is True
            and artifact_request_progress.get("current_evidence_verified")
            is True
            and artifact_request_progress.get("receipt_integrity_verified")
            is True
            and artifact_request_progress.get("path_free_projection_verified")
            is True
            and artifact_request_progress.get("public_only_projection_verified")
            is True
            and source_trust_candidate_artifact_request.get(
                "producer_contacted"
            )
            is False
            and source_trust_candidate_artifact_request.get(
                "transport_delivery_attempted"
            )
            is False
            and source_trust_candidate_artifact_request.get(
                "candidate_import_authorized"
            )
            is False
            and source_trust_candidate_artifact_request.get(
                "candidate_import_attempted"
            )
            is False
            and source_trust_candidate_artifact_request.get(
                "trust_registry_modified"
            )
            is False
            and source_trust_candidate_artifact_request.get(
                "provider_quota_consumption_allowed"
            )
            is False
            and source_trust_candidate_artifact_request.get(
                "delivery_authorized"
            )
            is False
        ):
            print(
                "scheduler PropertyQuarry OODA source refresh trust candidate "
                "artifact request not current",
                file=sys.stderr,
            )
            return 1
        ooda_source_trust_candidate_artifact_request_status = (
            artifact_request_state
        )
        try:
            source_trust_candidate_manual_action = (
                _verify_propertyquarry_ooda_source_refresh_trust_candidate_manual_action(
                    source_trust_candidate_import,
                    source_trust_candidate_artifact_request,
                )
            )
        except Exception:
            print(
                "scheduler PropertyQuarry OODA source refresh trust candidate "
                "manual action unavailable",
                file=sys.stderr,
            )
            return 1
        manual_action_state = str(
            source_trust_candidate_manual_action.get("action_state") or ""
        )
        manual_action_staged = manual_action_state in artifact_action_states
        manual_action_progress = dict(
            source_trust_candidate_manual_action.get("progress") or {}
        )
        if not (
            source_trust_candidate_manual_action.get("status") == "verified"
            and manual_action_state == candidate_import_state
            and source_trust_candidate_manual_action.get(
                "operator_action_receipt_staged"
            )
            is manual_action_staged
            and source_trust_candidate_manual_action.get("action_required")
            is manual_action_staged
            and source_trust_candidate_manual_action.get(
                "interrupt_operator"
            )
            is (
                source_trust_candidate_import.get("interrupt_operator")
                is True
            )
            and _sha256_text(
                source_trust_candidate_manual_action.get(
                    "operator_action_receipt_sha256"
                )
            )
            and source_trust_candidate_manual_action.get("receipt_persisted")
            is True
            and manual_action_progress.get("current_evidence_verified") is True
            and manual_action_progress.get("receipt_integrity_verified") is True
            and manual_action_progress.get(
                "artifact_request_binding_verified"
            )
            is True
            and manual_action_progress.get("presentation_binding_verified")
            is True
            and manual_action_progress.get("path_free_projection_verified")
            is True
            and manual_action_progress.get("public_only_projection_verified")
            is True
            and source_trust_candidate_manual_action.get("producer_contacted")
            is False
            and source_trust_candidate_manual_action.get(
                "transport_delivery_authorized"
            )
            is False
            and source_trust_candidate_manual_action.get(
                "transport_delivery_attempted"
            )
            is False
            and source_trust_candidate_manual_action.get("notification_sent")
            is False
            and source_trust_candidate_manual_action.get(
                "candidate_import_authorized"
            )
            is False
            and source_trust_candidate_manual_action.get(
                "candidate_import_attempted"
            )
            is False
            and source_trust_candidate_manual_action.get(
                "trust_registry_modified"
            )
            is False
            and source_trust_candidate_manual_action.get(
                "provider_quota_consumption_allowed"
            )
            is False
            and source_trust_candidate_manual_action.get(
                "delivery_authorized"
            )
            is False
        ):
            print(
                "scheduler PropertyQuarry OODA source refresh trust candidate "
                "manual action not current",
                file=sys.stderr,
            )
            return 1
        ooda_source_trust_candidate_manual_action_status = manual_action_state
        try:
            source_trust_candidate_artifact_notification = (
                _verify_propertyquarry_ooda_source_refresh_trust_candidate_artifact_notification(
                    source_trust_candidate_import,
                    source_trust_candidate_artifact_request,
                    source_trust_candidate_manual_action,
                )
            )
        except Exception:
            print(
                "scheduler PropertyQuarry OODA source refresh trust candidate "
                "artifact notification unavailable",
                file=sys.stderr,
            )
            return 1
        artifact_notification_status = str(
            source_trust_candidate_artifact_notification.get(
                "notification_status"
            )
            or ""
        )
        artifact_notification_completed = (
            artifact_notification_status == "completed"
        )
        artifact_notification_action = (
            artifact_notification_status == "action_required"
        )
        expected_artifact_notification_statuses = (
            {"action_required"}
            if source_trust_candidate_manual_action.get(
                "interrupt_operator"
            )
            is True
            else {"deduplicated", "completed"}
            if manual_action_staged
            else {"not_required"}
        )
        artifact_notification_progress = dict(
            source_trust_candidate_artifact_notification.get("progress")
            or {}
        )
        if not (
            source_trust_candidate_artifact_notification.get("status")
            == "verified"
            and artifact_notification_status
            in expected_artifact_notification_statuses
            and source_trust_candidate_artifact_notification.get(
                "action_required"
            )
            is manual_action_staged
            and source_trust_candidate_artifact_notification.get(
                "interrupt_operator"
            )
            is artifact_notification_action
            and source_trust_candidate_artifact_notification.get(
                "delivery_authorized"
            )
            is artifact_notification_completed
            and source_trust_candidate_artifact_notification.get(
                "delivery_attempted"
            )
            is artifact_notification_completed
            and source_trust_candidate_artifact_notification.get("sent")
            is artifact_notification_completed
            and source_trust_candidate_artifact_notification.get(
                "presentation_recorded"
            )
            is artifact_notification_completed
            and source_trust_candidate_artifact_notification.get(
                "receipt_persisted"
            )
            is True
            and artifact_notification_progress.get(
                "current_evidence_verified"
            )
            is True
            and artifact_notification_progress.get(
                "notification_integrity_verified"
            )
            is True
            and artifact_notification_progress.get(
                "artifact_request_binding_verified"
            )
            is True
            and artifact_notification_progress.get(
                "operator_action_binding_verified"
            )
            is True
            and artifact_notification_progress.get(
                "presentation_binding_verified"
            )
            is True
            and artifact_notification_progress.get(
                "path_free_projection_verified"
            )
            is True
            and source_trust_candidate_artifact_notification.get(
                "producer_contacted"
            )
            is False
            and source_trust_candidate_artifact_notification.get(
                "artifact_transport_authorized"
            )
            is False
            and source_trust_candidate_artifact_notification.get(
                "artifact_transport_attempted"
            )
            is False
            and source_trust_candidate_artifact_notification.get(
                "candidate_import_authorized"
            )
            is False
            and source_trust_candidate_artifact_notification.get(
                "candidate_import_attempted"
            )
            is False
            and source_trust_candidate_artifact_notification.get(
                "trust_registry_modified"
            )
            is False
            and source_trust_candidate_artifact_notification.get(
                "provider_quota_consumption_allowed"
            )
            is False
        ):
            print(
                "scheduler PropertyQuarry OODA source refresh trust candidate "
                "artifact notification not current",
                file=sys.stderr,
            )
            return 1
        ooda_source_trust_candidate_artifact_notification_status = (
            artifact_notification_status
        )
        try:
            source_trust_decision = (
                _verify_propertyquarry_ooda_source_refresh_trust_decision()
            )
        except Exception:
            print(
                "scheduler PropertyQuarry OODA source refresh trust decision unavailable",
                file=sys.stderr,
            )
            return 1
        decision_status = str(source_trust_decision.get("status") or "")
        decision_pending = decision_status == "pending"
        if not (
            decision_status in {"not_required", "pending", "verified"}
            and source_trust_decision.get("review_state")
            in {
                "not_required",
                "candidate_review_pending",
                "identity_verified",
                "candidate_rejected",
                "deferred",
            }
            and dict(source_trust_decision.get("progress") or {}).get(
                "current_evidence_verified"
            )
            is True
            and dict(source_trust_decision.get("progress") or {}).get(
                "candidate_review_verified"
            )
            is True
            and source_trust_decision.get("action_required") is decision_pending
            and source_trust_decision.get("operator_review_required")
            is decision_pending
            and source_trust_decision.get("interrupt_operator") is False
            and source_trust_decision.get("trust_enrollment_authorized") is False
            and source_trust_decision.get("trust_registry_modified") is False
            and source_trust_decision.get("decision_confers_trust") is False
            and source_trust_decision.get("private_key_material_requested") is False
            and source_trust_decision.get("private_key_material_recorded") is False
            and source_trust_decision.get("producer_dispatch_authorized") is False
            and source_trust_decision.get("producer_refresh_authorized") is False
            and source_trust_decision.get("automatic_execution_allowed") is False
            and source_trust_decision.get("execution_authorized") is False
            and source_trust_decision.get("deployment_or_restart_authorized") is False
            and source_trust_decision.get("protected_operation_executed") is False
            and source_trust_decision.get("provider_quota_consumption_allowed") is False
            and source_trust_decision.get("delivery_authorized") is False
            and source_trust_decision.get("delivery_attempted") is False
            and source_trust_decision.get("sent") is False
        ):
            print(
                "scheduler PropertyQuarry OODA source refresh trust decision not current: "
                f"status={source_trust_decision.get('status') or 'blocked'} "
                f"state={source_trust_decision.get('review_state') or 'unavailable'} "
                f"reason={source_trust_decision.get('blocking_reason') or 'not_verified'}",
                file=sys.stderr,
            )
            return 1
        ooda_source_trust_decision_status = decision_status
        try:
            source_trust_notification = (
                _verify_propertyquarry_ooda_source_refresh_trust_notification()
            )
        except Exception:
            print(
                "scheduler PropertyQuarry OODA source refresh trust notification unavailable",
                file=sys.stderr,
            )
            return 1
        notification_status = str(
            source_trust_notification.get("notification_status") or ""
        )
        notification_completed = notification_status == "completed"
        notification_action = notification_status == "action_required"
        notification_preview = (
            notification_status == "resolved"
            and source_trust_decision.get(
                "trust_enrollment_preview_authorized"
            )
            is True
        )
        if not (
            source_trust_notification.get("status") == "verified"
            and notification_status
            in {
                "not_required",
                "resolved",
                "action_required",
                "deduplicated",
                "completed",
            }
            and (
                decision_status == "not_required"
                and notification_status == "not_required"
                or decision_status == "verified"
                and notification_status == "resolved"
                or decision_status == "pending"
                and notification_status
                in {"action_required", "deduplicated", "completed"}
            )
            and dict(source_trust_notification.get("progress") or {}).get(
                "current_evidence_verified"
            )
            is True
            and dict(source_trust_notification.get("progress") or {}).get(
                "notification_integrity_verified"
            )
            is True
            and dict(source_trust_notification.get("progress") or {}).get(
                "candidate_review_binding_verified"
            )
            is True
            and source_trust_notification.get("action_required")
            is (
                notification_status
                in {"action_required", "deduplicated", "completed"}
            )
            and source_trust_notification.get("interrupt_operator")
            is (notification_action or notification_completed)
            and source_trust_notification.get("delivery_authorized")
            is notification_completed
            and source_trust_notification.get("delivery_attempted")
            is notification_completed
            and source_trust_notification.get("sent")
            is notification_completed
            and source_trust_notification.get("presentation_recorded")
            is notification_completed
            and source_trust_notification.get(
                "trust_enrollment_preview_authorized"
            )
            is notification_preview
            and source_trust_notification.get("trust_enrollment_authorized")
            is False
            and source_trust_notification.get("trust_registry_modified") is False
            and source_trust_notification.get("decision_confers_trust") is False
            and source_trust_notification.get("private_key_material_requested")
            is False
            and source_trust_notification.get("private_key_material_recorded")
            is False
            and source_trust_notification.get("producer_dispatch_authorized")
            is False
            and source_trust_notification.get("producer_refresh_authorized")
            is False
            and source_trust_notification.get("automatic_execution_allowed")
            is False
            and source_trust_notification.get("execution_authorized") is False
            and source_trust_notification.get("deployment_or_restart_authorized")
            is False
            and source_trust_notification.get("protected_operation_executed")
            is False
            and source_trust_notification.get(
                "provider_quota_consumption_allowed"
            )
            is False
        ):
            print(
                "scheduler PropertyQuarry OODA source refresh trust notification not current: "
                f"status={source_trust_notification.get('status') or 'blocked'} "
                f"state={notification_status or 'unavailable'} "
                f"reason={source_trust_notification.get('blocking_reason') or 'not_verified'}",
                file=sys.stderr,
            )
            return 1
        ooda_source_trust_notification_status = notification_status
        try:
            source_trust_enrollment_preview = (
                _verify_propertyquarry_ooda_source_refresh_trust_enrollment_preview()
            )
        except Exception:
            print(
                "scheduler PropertyQuarry OODA source refresh trust enrollment preview unavailable",
                file=sys.stderr,
            )
            return 1
        preview_state = str(
            source_trust_enrollment_preview.get("preview_state") or ""
        )
        preview_staged = preview_state == "preview_staged"
        expected_preview_state = (
            "not_required"
            if decision_status == "not_required"
            or source_trust_decision.get("decision")
            in {"reject_candidate", "defer"}
            else "awaiting_decision"
            if decision_status == "pending"
            else "preview_staged"
        )
        authorization_scope = source_trust_enrollment_preview.get(
            "authorization_scope"
        )
        if not (
            source_trust_enrollment_preview.get("status") == "verified"
            and preview_state == expected_preview_state
            and dict(source_trust_enrollment_preview.get("progress") or {}).get(
                "current_evidence_verified"
            )
            is True
            and dict(source_trust_enrollment_preview.get("progress") or {}).get(
                "intake_binding_verified"
            )
            is True
            and dict(source_trust_enrollment_preview.get("progress") or {}).get(
                "decision_binding_verified"
            )
            is True
            and dict(source_trust_enrollment_preview.get("progress") or {}).get(
                "trust_registry_binding_verified"
            )
            is True
            and dict(source_trust_enrollment_preview.get("progress") or {}).get(
                "preview_integrity_verified"
            )
            is True
            and source_trust_enrollment_preview.get(
                "preview_receipt_persisted"
            )
            is True
            and source_trust_enrollment_preview.get(
                "verification_receipt_persisted"
            )
            is True
            and source_trust_enrollment_preview.get("action_required")
            is preview_staged
            and source_trust_enrollment_preview.get("interrupt_operator")
            is preview_staged
            and source_trust_enrollment_preview.get("operator_review_required")
            is preview_staged
            and source_trust_enrollment_preview.get("preview_staged")
            is preview_staged
            and source_trust_enrollment_preview.get(
                "identity_verification_asserted"
            )
            is preview_staged
            and source_trust_enrollment_preview.get(
                "trust_enrollment_preview_authorized"
            )
            is preview_staged
            and source_trust_enrollment_preview.get("trust_enrollment_authorized")
            is False
            and source_trust_enrollment_preview.get("trust_registry_modified")
            is False
            and source_trust_enrollment_preview.get("decision_confers_trust")
            is False
            and source_trust_enrollment_preview.get(
                "private_key_material_requested"
            )
            is False
            and source_trust_enrollment_preview.get(
                "private_key_material_recorded"
            )
            is False
            and source_trust_enrollment_preview.get("producer_dispatch_authorized")
            is False
            and source_trust_enrollment_preview.get("producer_refresh_authorized")
            is False
            and source_trust_enrollment_preview.get("automatic_execution_allowed")
            is False
            and source_trust_enrollment_preview.get("execution_authorized")
            is False
            and source_trust_enrollment_preview.get(
                "deployment_or_restart_authorized"
            )
            is False
            and source_trust_enrollment_preview.get("protected_operation_executed")
            is False
            and source_trust_enrollment_preview.get(
                "provider_quota_consumption_allowed"
            )
            is False
            and source_trust_enrollment_preview.get("delivery_authorized") is False
            and source_trust_enrollment_preview.get("delivery_attempted") is False
            and source_trust_enrollment_preview.get("sent") is False
            and (
                preview_staged
                and bool(source_trust_enrollment_preview.get("preview_id"))
                and isinstance(authorization_scope, dict)
                and authorization_scope.get("operation")
                == "replace_trust_registry_with_exact_preview"
                and authorization_scope.get("authorization_recorded") is False
                or not preview_staged
                and not source_trust_enrollment_preview.get("preview_id")
                and authorization_scope == {}
            )
        ):
            print(
                "scheduler PropertyQuarry OODA source refresh trust enrollment preview not current: "
                f"status={source_trust_enrollment_preview.get('status') or 'blocked'} "
                f"state={preview_state or 'unavailable'} "
                f"reason={source_trust_enrollment_preview.get('blocking_reason') or 'not_verified'}",
                file=sys.stderr,
            )
            return 1
        ooda_source_trust_enrollment_preview_status = preview_state
        try:
            source_trust_enrollment_authorization = (
                _verify_propertyquarry_ooda_source_refresh_trust_enrollment_authorization()
            )
        except Exception:
            print(
                "scheduler PropertyQuarry OODA source refresh trust enrollment authorization unavailable",
                file=sys.stderr,
            )
            return 1
        authorization_status = str(
            source_trust_enrollment_authorization.get("status") or ""
        )
        authorization_state = str(
            source_trust_enrollment_authorization.get("authorization_state")
            or ""
        )
        authorization_pending = authorization_status == "pending"
        authorization_recorded = authorization_status == "verified"
        exact_authorized = bool(
            authorization_recorded
            and source_trust_enrollment_authorization.get("decision")
            == "authorize_exact_preview"
        )
        expected_authorization_statuses = (
            {"not_required"}
            if preview_state in {"not_required", "awaiting_decision"}
            else {"pending", "verified"}
        )
        if not (
            authorization_status in expected_authorization_statuses
            and authorization_state
            in {
                "not_required",
                "exact_preview_authorization_pending",
                "exact_preview_authorized",
                "exact_preview_rejected",
                "deferred",
            }
            and dict(source_trust_enrollment_authorization.get("progress") or {}).get(
                "current_evidence_verified"
            )
            is True
            and dict(source_trust_enrollment_authorization.get("progress") or {}).get(
                "preview_binding_verified"
            )
            is True
            and dict(source_trust_enrollment_authorization.get("progress") or {}).get(
                "authorization_decision_recorded"
            )
            is authorization_recorded
            and source_trust_enrollment_authorization.get("action_required")
            is authorization_pending
            and source_trust_enrollment_authorization.get("operator_review_required")
            is authorization_pending
            and source_trust_enrollment_authorization.get("interrupt_operator")
            is False
            and source_trust_enrollment_authorization.get(
                "explicit_authorization_recorded"
            )
            is authorization_recorded
            and source_trust_enrollment_authorization.get(
                "exact_preview_authorized"
            )
            is exact_authorized
            and source_trust_enrollment_authorization.get(
                "trust_enrollment_authorized"
            )
            is exact_authorized
            and source_trust_enrollment_authorization.get(
                "trust_registry_modified"
            )
            is False
            and source_trust_enrollment_authorization.get(
                "decision_confers_trust"
            )
            is False
            and source_trust_enrollment_authorization.get(
                "private_key_material_requested"
            )
            is False
            and source_trust_enrollment_authorization.get(
                "private_key_material_recorded"
            )
            is False
            and source_trust_enrollment_authorization.get(
                "producer_dispatch_authorized"
            )
            is False
            and source_trust_enrollment_authorization.get(
                "producer_refresh_authorized"
            )
            is False
            and source_trust_enrollment_authorization.get(
                "automatic_execution_allowed"
            )
            is False
            and source_trust_enrollment_authorization.get("execution_authorized")
            is False
            and source_trust_enrollment_authorization.get(
                "deployment_or_restart_authorized"
            )
            is False
            and source_trust_enrollment_authorization.get(
                "protected_operation_executed"
            )
            is False
            and source_trust_enrollment_authorization.get(
                "provider_quota_consumption_allowed"
            )
            is False
            and source_trust_enrollment_authorization.get("delivery_authorized")
            is False
            and source_trust_enrollment_authorization.get("delivery_attempted")
            is False
            and source_trust_enrollment_authorization.get("sent") is False
        ):
            print(
                "scheduler PropertyQuarry OODA source refresh trust enrollment authorization not current: "
                f"status={authorization_status or 'blocked'} "
                f"state={authorization_state or 'unavailable'} "
                f"reason={source_trust_enrollment_authorization.get('blocking_reason') or 'not_verified'}",
                file=sys.stderr,
            )
            return 1
        ooda_source_trust_enrollment_authorization_status = (
            authorization_state
        )
        try:
            source_trust_enrollment_execution_readiness = (
                _verify_propertyquarry_ooda_source_refresh_trust_enrollment_execution_readiness()
            )
        except Exception:
            print(
                "scheduler PropertyQuarry OODA source refresh trust enrollment execution readiness unavailable",
                file=sys.stderr,
            )
            return 1
        readiness_state = str(
            source_trust_enrollment_execution_readiness.get(
                "readiness_state"
            )
            or ""
        )
        expected_readiness_state = (
            "not_required"
            if authorization_status == "not_required"
            else "awaiting_exact_preview_authorization"
            if authorization_status == "pending"
            else "ready_for_governed_execution"
            if exact_authorized
            else "authorization_rejected"
            if source_trust_enrollment_authorization.get("decision")
            == "reject"
            else "deferred"
        )
        readiness_ready = (
            readiness_state == "ready_for_governed_execution"
        )
        readiness_progress = dict(
            source_trust_enrollment_execution_readiness.get("progress") or {}
        )
        if not (
            source_trust_enrollment_execution_readiness.get("status")
            == "verified"
            and readiness_state == expected_readiness_state
            and bool(
                source_trust_enrollment_execution_readiness.get(
                    "readiness_id"
                )
            )
            and readiness_progress.get("current_evidence_verified") is True
            and readiness_progress.get("preview_binding_verified") is True
            and readiness_progress.get("authorization_binding_verified")
            is True
            and readiness_progress.get("registry_binding_verified") is True
            and readiness_progress.get("readiness_receipt_persisted") is True
            and readiness_progress.get("verification_receipt_persisted")
            is True
            and source_trust_enrollment_execution_readiness.get(
                "readiness_receipt_persisted"
            )
            is True
            and source_trust_enrollment_execution_readiness.get(
                "verification_receipt_persisted"
            )
            is True
            and source_trust_enrollment_execution_readiness.get(
                "action_required"
            )
            is readiness_ready
            and source_trust_enrollment_execution_readiness.get(
                "interrupt_operator"
            )
            is False
            and source_trust_enrollment_execution_readiness.get(
                "operator_review_required"
            )
            is readiness_ready
            and source_trust_enrollment_execution_readiness.get(
                "explicit_authorization_recorded"
            )
            is authorization_recorded
            and source_trust_enrollment_execution_readiness.get(
                "exact_preview_authorized"
            )
            is readiness_ready
            and source_trust_enrollment_execution_readiness.get(
                "trust_enrollment_authorized"
            )
            is readiness_ready
            and source_trust_enrollment_execution_readiness.get(
                "execution_request_staged"
            )
            is readiness_ready
            and source_trust_enrollment_execution_readiness.get(
                "execution_readiness_verified"
            )
            is readiness_ready
            and source_trust_enrollment_execution_readiness.get(
                "governed_execution_available"
            )
            is readiness_ready
            and source_trust_enrollment_execution_readiness.get(
                "trust_registry_modified"
            )
            is False
            and source_trust_enrollment_execution_readiness.get(
                "automatic_execution_allowed"
            )
            is False
            and source_trust_enrollment_execution_readiness.get(
                "execution_authorized"
            )
            is False
            and source_trust_enrollment_execution_readiness.get(
                "protected_operation_executed"
            )
            is False
            and source_trust_enrollment_execution_readiness.get(
                "provider_quota_consumption_allowed"
            )
            is False
            and source_trust_enrollment_execution_readiness.get(
                "delivery_authorized"
            )
            is False
            and source_trust_enrollment_execution_readiness.get(
                "delivery_attempted"
            )
            is False
            and source_trust_enrollment_execution_readiness.get("sent")
            is False
        ):
            print(
                "scheduler PropertyQuarry OODA source refresh trust enrollment execution readiness not current: "
                f"status={source_trust_enrollment_execution_readiness.get('status') or 'blocked'} "
                f"state={readiness_state or 'unavailable'} "
                f"reason={source_trust_enrollment_execution_readiness.get('blocking_reason') or 'not_verified'}",
                file=sys.stderr,
            )
            return 1
        ooda_source_trust_enrollment_execution_readiness_status = (
            readiness_state
        )
        try:
            source_trust_enrollment_execution = (
                _verify_propertyquarry_ooda_source_refresh_trust_enrollment_execution(
                    source_trust_enrollment_execution_readiness
                )
            )
        except Exception:
            print(
                "scheduler PropertyQuarry OODA source refresh trust enrollment execution history unavailable",
                file=sys.stderr,
            )
            return 1
        execution_state = str(
            source_trust_enrollment_execution.get("execution_state") or ""
        )
        expected_execution_states = {
            "not_required": {"not_required", "succeeded"},
            "awaiting_exact_preview_authorization": {
                "awaiting_authorization"
            },
            "authorization_rejected": {"authorization_rejected"},
            "deferred": {"deferred"},
            "ready_for_governed_execution": {
                "ready_for_manual_execution",
                "succeeded",
            },
        }.get(readiness_state, set())
        execution_action_required = execution_state in {
            "ready_for_manual_execution",
            "recovery_required",
        }
        if not (
            source_trust_enrollment_execution.get("status") == "verified"
            and (
                execution_state in expected_execution_states
                or execution_state == "recovery_required"
            )
            and dict(
                source_trust_enrollment_execution.get("progress") or {}
            ).get("current_evidence_verified")
            is True
            and source_trust_enrollment_execution.get("action_required")
            is execution_action_required
            and source_trust_enrollment_execution.get("interrupt_operator")
            is False
            and source_trust_enrollment_execution.get(
                "manual_execution_authorized"
            )
            is (execution_state == "ready_for_manual_execution")
            and source_trust_enrollment_execution.get(
                "automatic_execution_allowed"
            )
            is False
            and source_trust_enrollment_execution.get("execution_authorized")
            is False
            and source_trust_enrollment_execution.get(
                "deployment_or_restart_authorized"
            )
            is False
            and source_trust_enrollment_execution.get(
                "protected_operation_executed"
            )
            is (
                source_trust_enrollment_execution.get(
                    "trust_registry_modified"
                )
                is True
            )
            and source_trust_enrollment_execution.get(
                "provider_quota_consumption_allowed"
            )
            is False
            and source_trust_enrollment_execution.get("delivery_authorized")
            is False
            and source_trust_enrollment_execution.get("delivery_attempted")
            is False
            and source_trust_enrollment_execution.get("sent") is False
        ):
            print(
                "scheduler PropertyQuarry OODA source refresh trust enrollment execution not current: "
                f"status={source_trust_enrollment_execution.get('status') or 'blocked'} "
                f"state={execution_state or 'unavailable'} "
                f"reason={source_trust_enrollment_execution.get('blocking_reason') or 'not_verified'}",
                file=sys.stderr,
            )
            return 1
        ooda_source_trust_enrollment_execution_status = execution_state
    suffix = (
        f" ooda_iteration={ooda_iteration_status}"
        if ooda_iteration_status
        else ""
    )
    if ooda_source_refresh_status:
        suffix += f" source_refresh={ooda_source_refresh_status}"
    if ooda_source_handoff_status:
        suffix += f" source_handoff={ooda_source_handoff_status}"
    if ooda_source_claims_status:
        suffix += f" source_claims={ooda_source_claims_status}"
    if ooda_source_settlement_status:
        suffix += f" source_settlement={ooda_source_settlement_status}"
    if ooda_source_trust_intake_status:
        suffix += f" source_trust_intake={ooda_source_trust_intake_status}"
    if ooda_source_trust_candidate_import_status:
        suffix += (
            " source_trust_candidate_import="
            f"{ooda_source_trust_candidate_import_status}"
        )
    if ooda_source_trust_candidate_artifact_request_status:
        suffix += (
            " source_trust_candidate_artifact_request="
            f"{ooda_source_trust_candidate_artifact_request_status}"
        )
    if ooda_source_trust_candidate_manual_action_status:
        suffix += (
            " source_trust_candidate_manual_action="
            f"{ooda_source_trust_candidate_manual_action_status}"
        )
    if ooda_source_trust_candidate_artifact_notification_status:
        suffix += (
            " source_trust_candidate_artifact_notification="
            f"{ooda_source_trust_candidate_artifact_notification_status}"
        )
    if ooda_source_trust_decision_status:
        suffix += f" source_trust_decision={ooda_source_trust_decision_status}"
    if ooda_source_trust_notification_status:
        suffix += (
            " source_trust_notification="
            f"{ooda_source_trust_notification_status}"
        )
    if ooda_source_trust_enrollment_preview_status:
        suffix += (
            " source_trust_enrollment_preview="
            f"{ooda_source_trust_enrollment_preview_status}"
        )
    if ooda_source_trust_enrollment_authorization_status:
        suffix += (
            " source_trust_enrollment_authorization="
            f"{ooda_source_trust_enrollment_authorization_status}"
        )
    if ooda_source_trust_enrollment_execution_readiness_status:
        suffix += (
            " source_trust_enrollment_execution_readiness="
            f"{ooda_source_trust_enrollment_execution_readiness_status}"
        )
    if ooda_source_trust_enrollment_execution_status:
        suffix += (
            " source_trust_enrollment_execution="
            f"{ooda_source_trust_enrollment_execution_status}"
        )
    print(
        f"{role} heartbeat ok: age={age_seconds:.1f}s "
        f"pid={observed_pid}{suffix}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
