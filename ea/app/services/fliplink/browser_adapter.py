from __future__ import annotations

from app.services.fliplink.models import fliplink_settings_from_env


BROWSERACT_FLIPLINK_PUBLISH_TASK_NAME = "browseract.fliplink_publish_property_packet"
BROWSERACT_FLIPLINK_PUBLISH_CONTRACT_VERSION = "fliplink_browseract_publish_v1"
BROWSERACT_FLIPLINK_REQUIRED_OUTPUTS = ("fliplink_url", "screenshot_proof_ref")


def _text(request: dict[str, object], key: str) -> str:
    return str(request.get(key) or "").strip()


def _completion_payload_schema() -> dict[str, object]:
    return {
        "type": "object",
        "required": ["fliplink_url", "screenshot_proof_ref"],
        "properties": {
            "fliplink_url": {"type": "string", "minLength": 8, "maxLength": 500},
            "embed_code": {"type": "string", "maxLength": 4000},
            "qr_url": {"type": "string", "maxLength": 500},
            "screenshot_proof_ref": {"type": "string", "minLength": 1, "maxLength": 500},
            "lead_capture_enabled": {"type": "boolean"},
            "password_required": {"type": "boolean"},
            "sale_mode_enabled": {"type": "boolean"},
        },
    }


def browseract_fliplink_publish_requested(request: dict[str, object]) -> dict[str, object]:
    settings = fliplink_settings_from_env()
    if not settings.browseract_enabled:
        raise RuntimeError("fliplink_browseract_disabled")
    if not settings.login_email or not settings.login_password_present:
        raise RuntimeError("fliplink_credentials_unconfigured")
    if not _text(request, "publication_id"):
        raise ValueError("publication_id_required")
    if not _text(request, "pdf_artifact_ref"):
        raise ValueError("pdf_artifact_ref_required")
    if not _text(request, "source_pdf_sha256"):
        raise ValueError("source_pdf_sha256_required")
    if not bool(request.get("redaction_receipt_present")):
        raise ValueError("redaction_receipt_required")
    if not _text(request, "completion_endpoint"):
        raise ValueError("completion_endpoint_required")
    if _text(request, "privacy_mode") == "owner_private" and not bool(request.get("password_required")):
        raise ValueError("owner_private_requires_password")
    completion_schema = _completion_payload_schema()
    runner_payload = {
        "contract_version": BROWSERACT_FLIPLINK_PUBLISH_CONTRACT_VERSION,
        "task_name": BROWSERACT_FLIPLINK_PUBLISH_TASK_NAME,
        "provider": "fliplink",
        "publication_id": _text(request, "publication_id"),
        "packet_kind": _text(request, "packet_kind"),
        "privacy_mode": _text(request, "privacy_mode"),
        "fliplink_format": _text(request, "fliplink_format"),
        "recommended_title": _text(request, "recommended_title"),
        "recommended_folder": _text(request, "recommended_folder"),
        "custom_domain": _text(request, "custom_domain"),
        "pdf_artifact_ref": _text(request, "pdf_artifact_ref"),
        "receipt_artifact_ref": _text(request, "receipt_artifact_ref"),
        "source_pdf_sha256": _text(request, "source_pdf_sha256"),
        "source_pdf_size_bytes": int(request.get("source_pdf_size_bytes") or 0),
        "redaction_receipt_present": True,
        "lead_capture_enabled": bool(request.get("lead_capture_enabled")),
        "password_required": bool(request.get("password_required")),
        "completion": {
            "method": "POST",
            "endpoint": _text(request, "completion_endpoint"),
            "payload_schema": completion_schema,
        },
        "required_outputs": list(BROWSERACT_FLIPLINK_REQUIRED_OUTPUTS),
        "proof_policy": {
            "screenshot_proof_ref_required": True,
            "must_verify_pdf_sha256": True,
            "must_not_upload_unredacted_source_payload": True,
        },
    }
    return {
        "status": "queued_operator_assist",
        "task_name": BROWSERACT_FLIPLINK_PUBLISH_TASK_NAME,
        "contract_version": BROWSERACT_FLIPLINK_PUBLISH_CONTRACT_VERSION,
        "provider": "fliplink",
        "required_outputs": list(BROWSERACT_FLIPLINK_REQUIRED_OUTPUTS),
        "completion_payload_schema": completion_schema,
        "runner_payload": runner_payload,
    }
