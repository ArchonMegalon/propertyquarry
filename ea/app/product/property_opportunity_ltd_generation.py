from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from app.domain.models import ToolInvocationRequest


TOOL_NAME = "provider.onemin.image_generate"
ACTION_KIND = "image_generate"


def property_opportunity_concept_cover_prompt(
    opportunity: dict[str, object],
) -> str:
    """Build a useful provider prompt without sending listing or customer identity."""

    recommendation_key = str(opportunity.get("recommendation") or "review").strip().lower()
    recommendation = {
        "shortlist": "shortlist",
        "ask_for_clarification": "ask for clarification",
        "reject": "reject",
        "review": "review",
    }.get(recommendation_key, "review")
    try:
        fit_score = max(0, min(100, round(float(opportunity.get("fit_score") or 0))))
    except (TypeError, ValueError):
        fit_score = 0
    fit_band = "strong" if fit_score >= 70 else "balanced" if fit_score >= 45 else "exploratory"
    return (
        "Create one square, premium minimal editorial concept cover for a private Austrian "
        "home-search opportunity brief. Use warm natural light, restrained stone, oak and "
        "soft terracotta tones, generous negative space, and clean architectural geometry. "
        "It must be visibly an abstract mood illustration, never documentary listing "
        "photography. No text, logos, people, addresses, maps, floor plans, watermarks, or "
        f"identifying details. Decision tone: {fit_band} fit; recommendation: {recommendation}."
    )


def _safe_provider_asset_url(value: object) -> str:
    candidate = str(value or "").strip()
    if not candidate or len(candidate) > 4000:
        return ""
    parsed = urlsplit(candidate)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return ""
    host = parsed.hostname.strip().lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local")):
        return ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            return ""
    return candidate


def _result_asset_urls(output: dict[str, object]) -> list[str]:
    raw_urls = output.get("asset_urls")
    if not isinstance(raw_urls, list):
        structured = output.get("structured_output_json")
        raw_urls = (
            dict(structured).get("asset_urls")
            if isinstance(structured, dict)
            else []
        )
    return [
        safe
        for value in list(raw_urls or [])[:4]
        if (safe := _safe_provider_asset_url(value))
    ]


def property_opportunity_concept_cover_public_projection(
    value: object,
    *,
    generation_id: str,
    opportunity_id: str,
) -> dict[str, object]:
    """Revalidate and allowlist a persisted worker result before browser delivery."""

    source = dict(value) if isinstance(value, dict) else {}
    artifact = (
        dict(source.get("artifact"))
        if isinstance(source.get("artifact"), dict)
        else {}
    )
    generation = (
        dict(source.get("generation"))
        if isinstance(source.get("generation"), dict)
        else {}
    )
    receipt = (
        dict(source.get("receipt"))
        if isinstance(source.get("receipt"), dict)
        else {}
    )
    publication = (
        dict(source.get("publication"))
        if isinstance(source.get("publication"), dict)
        else {}
    )
    normalized_generation_id = str(generation_id or "").strip()
    normalized_opportunity_id = str(opportunity_id or "").strip()
    asset_url = _safe_provider_asset_url(artifact.get("asset_url"))
    receipt_model = str(receipt.get("model") or "").strip()
    generation_model = str(generation.get("model") or "").strip()
    valid = bool(
        normalized_generation_id
        and normalized_opportunity_id
        and source.get("status") == "ready"
        and str(source.get("generation_id") or "").strip() == normalized_generation_id
        and str(source.get("opportunity_id") or "").strip() == normalized_opportunity_id
        and artifact.get("kind") == "private_concept_cover"
        and asset_url
        and generation.get("provider") == "1min.AI"
        and generation.get("provider_key") == "onemin"
        and generation.get("provider_backend") == "1min"
        and generation.get("action") == ACTION_KIND
        and generation.get("privacy_scope")
        == "no_listing_or_customer_identifiers_sent"
        and receipt.get("status") == "verified"
        and receipt.get("principal_bound") is True
        and receipt.get("proof_scope") == "provider_call"
        and receipt.get("handler_key") == TOOL_NAME
        and receipt.get("invocation_contract") == "tool.v1"
        and receipt.get("provider_key") == "onemin"
        and receipt.get("provider_backend") == "1min"
        and receipt.get("feature_type") == "IMAGE_GENERATOR"
        and receipt_model
        and generation_model == receipt_model
        and publication.get("scope") == "private_generated_asset"
        and publication.get("status") == "not_published"
        and publication.get("external_publication_verified") is False
    )
    if not valid:
        return {}
    return {
        "status": "ready",
        "generation_id": normalized_generation_id,
        "opportunity_id": normalized_opportunity_id,
        "artifact": {
            "kind": "private_concept_cover",
            "asset_url": asset_url,
            "label": "AI concept cover",
            "disclaimer": "Synthetic mood illustration · not listing photography",
        },
        "generation": {
            "provider": "1min.AI",
            "provider_key": "onemin",
            "provider_backend": "1min",
            "action": ACTION_KIND,
            "model": receipt_model[:160],
            "privacy_scope": "no_listing_or_customer_identifiers_sent",
        },
        "receipt": {
            "status": "verified",
            "principal_bound": True,
            "proof_scope": "provider_call",
            "handler_key": TOOL_NAME,
            "invocation_contract": "tool.v1",
            "provider_key": "onemin",
            "provider_backend": "1min",
            "feature_type": "IMAGE_GENERATOR",
            "model": receipt_model[:160],
        },
        "publication": {
            "scope": "private_generated_asset",
            "status": "not_published",
            "external_publication_verified": False,
        },
    }


def execute_property_opportunity_concept_cover(
    *,
    tool_execution: object,
    principal_id: str,
    job_id: str,
    opportunity: dict[str, object],
) -> dict[str, object]:
    normalized_principal = str(principal_id or "").strip()
    opportunity_id = str(opportunity.get("opportunity_id") or "").strip()
    if not normalized_principal or not job_id or not opportunity_id:
        raise RuntimeError("property_opportunity_ltd_generation_identity_invalid")
    request = ToolInvocationRequest(
        session_id=f"property-opportunity:{job_id}",
        step_id=f"property-opportunity-cover:{job_id}",
        tool_name=TOOL_NAME,
        action_kind=ACTION_KIND,
        payload_json={
            "prompt": property_opportunity_concept_cover_prompt(opportunity),
            "n": 1,
            "size": "1024x1024",
            "quality": "low",
            "output_format": "png",
        },
        context_json={
            "principal_id": normalized_principal,
            "suppress_telegram_delivery": True,
        },
    )
    result = tool_execution.execute_invocation(request)
    output = dict(getattr(result, "output_json", {}) or {})
    receipt = dict(getattr(result, "receipt_json", {}) or {})
    asset_urls = _result_asset_urls(output)
    receipt_model = str(receipt.get("model") or "").strip()
    output_model = str(output.get("model") or "").strip()
    model = receipt_model or output_model
    verified = bool(
        getattr(result, "tool_name", "") == TOOL_NAME
        and getattr(result, "action_kind", "") == ACTION_KIND
        and str(getattr(result, "target_ref", "") or "").strip()
        and receipt.get("handler_key") == TOOL_NAME
        and receipt.get("invocation_contract") == "tool.v1"
        and receipt.get("principal_id") == normalized_principal
        and receipt.get("provider_key") == "onemin"
        and receipt.get("provider_backend") == "1min"
        and receipt.get("feature_type") == "IMAGE_GENERATOR"
        and str(output.get("provider_backend") or "").strip() == "1min"
        and model
        and (not receipt_model or not output_model or receipt_model == output_model)
        and asset_urls
    )
    if not verified:
        raise RuntimeError("property_opportunity_ltd_generation_receipt_unverified")
    return {
        "status": "ready",
        "generation_id": str(job_id),
        "opportunity_id": opportunity_id,
        "artifact": {
            "kind": "private_concept_cover",
            "asset_url": asset_urls[0],
            "label": "AI concept cover",
            "disclaimer": "Synthetic mood illustration · not listing photography",
        },
        "generation": {
            "provider": "1min.AI",
            "provider_key": "onemin",
            "provider_backend": "1min",
            "action": "image_generate",
            "model": model,
            "privacy_scope": "no_listing_or_customer_identifiers_sent",
        },
        "receipt": {
            "status": "verified",
            "principal_bound": True,
            "proof_scope": "provider_call",
            "handler_key": TOOL_NAME,
            "invocation_contract": "tool.v1",
            "provider_key": "onemin",
            "provider_backend": "1min",
            "feature_type": "IMAGE_GENERATOR",
            "model": model,
        },
        "publication": {
            "scope": "private_generated_asset",
            "status": "not_published",
            "external_publication_verified": False,
        },
    }
