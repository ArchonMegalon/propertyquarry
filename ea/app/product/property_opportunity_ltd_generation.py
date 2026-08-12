from __future__ import annotations

from io import BytesIO
import hashlib
import ipaddress
import os
from pathlib import Path
import tempfile
from typing import Callable
from urllib.parse import quote, urlsplit

from PIL import Image, UnidentifiedImageError

from app.domain.models import ToolInvocationRequest


TOOL_NAME = "provider.onemin.image_generate"
ACTION_KIND = "image_generate"
_MAX_ASSET_BYTES = 8 * 1024 * 1024
_MAX_IMAGE_PIXELS = 16_777_216
_ASSET_SUBDIRECTORY = "property-opportunity-covers"
_ALLOWED_IMAGE_MEDIA_TYPES = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}


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


def property_opportunity_concept_cover_asset_url(generation_id: str) -> str:
    normalized = str(generation_id or "").strip()
    if not normalized:
        return ""
    return (
        "/app/api/property/opportunities/generations/"
        f"{quote(normalized, safe='')}/asset"
    )


def _concept_cover_asset_path(
    generation_id: str,
    *,
    artifact_root: Path | None = None,
) -> Path:
    root = artifact_root or Path("/data/artifacts")
    token = hashlib.sha256(
        f"property-opportunity-cover-v1\0{str(generation_id or '').strip()}".encode(
            "utf-8"
        )
    ).hexdigest()
    return root / _ASSET_SUBDIRECTORY / f"{token}.image"


def _validated_image_metadata(data: bytes) -> dict[str, object]:
    if not data or len(data) > _MAX_ASSET_BYTES:
        raise RuntimeError("property_opportunity_ltd_asset_size_invalid")
    try:
        with Image.open(BytesIO(data)) as image:
            image_format = str(image.format or "").upper()
            width, height = image.size
            frames = int(getattr(image, "n_frames", 1) or 1)
            image.verify()
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise RuntimeError("property_opportunity_ltd_asset_image_invalid") from exc
    media_type = _ALLOWED_IMAGE_MEDIA_TYPES.get(image_format, "")
    if (
        not media_type
        or width < 1
        or height < 1
        or width * height > _MAX_IMAGE_PIXELS
        or frames != 1
    ):
        raise RuntimeError("property_opportunity_ltd_asset_image_invalid")
    return {
        "media_type": media_type,
        "byte_length": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "width": int(width),
        "height": int(height),
    }


def _default_asset_fetcher(url: str) -> tuple[bytes, str]:
    import requests

    try:
        with requests.get(
            url,
            allow_redirects=False,
            stream=True,
            timeout=(5.0, 30.0),
            headers={"Accept": "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.2"},
        ) as response:
            if response.status_code != 200:
                raise RuntimeError("property_opportunity_ltd_asset_fetch_failed")
            content_type = str(response.headers.get("content-type") or "").split(
                ";", 1
            )[0].strip().lower()
            if content_type not in {
                "application/octet-stream",
                "image/jpeg",
                "image/png",
                "image/webp",
            }:
                raise RuntimeError("property_opportunity_ltd_asset_content_type_invalid")
            declared_length = str(response.headers.get("content-length") or "").strip()
            if declared_length:
                try:
                    if int(declared_length) > _MAX_ASSET_BYTES:
                        raise RuntimeError("property_opportunity_ltd_asset_size_invalid")
                except ValueError as exc:
                    raise RuntimeError(
                        "property_opportunity_ltd_asset_content_length_invalid"
                    ) from exc
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > _MAX_ASSET_BYTES:
                    raise RuntimeError("property_opportunity_ltd_asset_size_invalid")
                chunks.append(bytes(chunk))
            return b"".join(chunks), content_type
    except requests.RequestException as exc:
        raise RuntimeError("property_opportunity_ltd_asset_fetch_failed") from exc


def _existing_materialized_asset(
    generation_id: str,
    *,
    artifact_root: Path | None = None,
) -> dict[str, object] | None:
    path = _concept_cover_asset_path(generation_id, artifact_root=artifact_root)
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if path.stat().st_size > _MAX_ASSET_BYTES:
            return None
        data = path.read_bytes()
        metadata = _validated_image_metadata(data)
    except (OSError, RuntimeError):
        return None
    return {**metadata, "path": str(path)}


def materialize_property_opportunity_concept_cover_asset(
    *,
    provider_asset_url: str,
    generation_id: str,
    artifact_root: Path | None = None,
    asset_fetcher: Callable[[str], tuple[bytes, str]] | None = None,
) -> dict[str, object]:
    """Store a bounded verified provider image in the shared private volume."""

    normalized_url = _safe_provider_asset_url(provider_asset_url)
    normalized_generation_id = str(generation_id or "").strip()
    if not normalized_url or not normalized_generation_id:
        raise RuntimeError("property_opportunity_ltd_asset_identity_invalid")
    existing = _existing_materialized_asset(
        normalized_generation_id,
        artifact_root=artifact_root,
    )
    if existing is not None:
        return existing
    fetcher = asset_fetcher or _default_asset_fetcher
    data, _declared_media_type = fetcher(normalized_url)
    metadata = _validated_image_metadata(bytes(data))
    target = _concept_cover_asset_path(
        normalized_generation_id,
        artifact_root=artifact_root,
    )
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    temporary_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            delete=False,
        ) as handle:
            temporary_path = handle.name
            os.chmod(temporary_path, 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, target)
        os.chmod(target, 0o600)
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)
    return {**metadata, "path": str(target)}


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
    expected_asset_url = property_opportunity_concept_cover_asset_url(
        normalized_generation_id
    )
    raw_asset_url = str(artifact.get("asset_url") or "").strip()
    legacy_provider_asset_url = _safe_provider_asset_url(raw_asset_url)
    asset_sha256 = str(artifact.get("sha256") or "").strip().lower()
    media_type = str(artifact.get("media_type") or "").strip().lower()
    try:
        byte_length = int(artifact.get("byte_length") or 0)
        width = int(artifact.get("width") or 0)
        height = int(artifact.get("height") or 0)
    except (TypeError, ValueError):
        byte_length = width = height = 0
    materialized = bool(
        raw_asset_url == expected_asset_url
        and len(asset_sha256) == 64
        and all(character in "0123456789abcdef" for character in asset_sha256)
        and media_type in set(_ALLOWED_IMAGE_MEDIA_TYPES.values())
        and 0 < byte_length <= _MAX_ASSET_BYTES
        and width > 0
        and height > 0
        and width * height <= _MAX_IMAGE_PIXELS
        and receipt.get("asset_materialized") is True
        and receipt.get("asset_sha256") == asset_sha256
        and receipt.get("asset_byte_length") == byte_length
    )
    receipt_model = str(receipt.get("model") or "").strip()
    generation_model = str(generation.get("model") or "").strip()
    valid = bool(
        normalized_generation_id
        and normalized_opportunity_id
        and source.get("status") == "ready"
        and str(source.get("generation_id") or "").strip() == normalized_generation_id
        and str(source.get("opportunity_id") or "").strip() == normalized_opportunity_id
        and artifact.get("kind") == "private_concept_cover"
        and (materialized or legacy_provider_asset_url)
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
            "asset_url": expected_asset_url,
            "label": "AI concept cover",
            "disclaimer": "Synthetic mood illustration · not listing photography",
            **(
                {
                    "media_type": media_type,
                    "sha256": asset_sha256,
                    "byte_length": byte_length,
                    "width": width,
                    "height": height,
                }
                if materialized
                else {}
            ),
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
            "asset_materialized": materialized,
            **(
                {
                    "asset_sha256": asset_sha256,
                    "asset_byte_length": byte_length,
                }
                if materialized
                else {}
            ),
        },
        "publication": {
            "scope": "private_generated_asset",
            "status": "not_published",
            "external_publication_verified": False,
        },
    }


def property_opportunity_concept_cover_asset_descriptor(
    value: object,
    *,
    generation_id: str,
    opportunity_id: str,
    artifact_root: Path | None = None,
    asset_fetcher: Callable[[str], tuple[bytes, str]] | None = None,
) -> dict[str, object]:
    """Resolve a verified private file, lazily migrating legacy provider URLs."""

    projection = property_opportunity_concept_cover_public_projection(
        value,
        generation_id=generation_id,
        opportunity_id=opportunity_id,
    )
    if not projection:
        raise RuntimeError("property_opportunity_ltd_asset_receipt_invalid")
    source = dict(value) if isinstance(value, dict) else {}
    artifact = (
        dict(source.get("artifact"))
        if isinstance(source.get("artifact"), dict)
        else {}
    )
    raw_asset_url = str(artifact.get("asset_url") or "").strip()
    expected_asset_url = property_opportunity_concept_cover_asset_url(generation_id)
    if raw_asset_url == expected_asset_url:
        materialized = _existing_materialized_asset(
            generation_id,
            artifact_root=artifact_root,
        )
        if materialized is None:
            raise RuntimeError("property_opportunity_ltd_asset_missing")
        if (
            materialized.get("sha256") != artifact.get("sha256")
            or materialized.get("byte_length") != artifact.get("byte_length")
            or materialized.get("media_type") != artifact.get("media_type")
        ):
            raise RuntimeError("property_opportunity_ltd_asset_digest_mismatch")
        return materialized
    return materialize_property_opportunity_concept_cover_asset(
        provider_asset_url=raw_asset_url,
        generation_id=generation_id,
        artifact_root=artifact_root,
        asset_fetcher=asset_fetcher,
    )


def execute_property_opportunity_concept_cover(
    *,
    tool_execution: object,
    principal_id: str,
    job_id: str,
    opportunity: dict[str, object],
    artifact_root: Path | None = None,
    asset_fetcher: Callable[[str], tuple[bytes, str]] | None = None,
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
    materialized = materialize_property_opportunity_concept_cover_asset(
        provider_asset_url=asset_urls[0],
        generation_id=job_id,
        artifact_root=artifact_root,
        asset_fetcher=asset_fetcher,
    )
    asset_url = property_opportunity_concept_cover_asset_url(job_id)
    return {
        "status": "ready",
        "generation_id": str(job_id),
        "opportunity_id": opportunity_id,
        "artifact": {
            "kind": "private_concept_cover",
            "asset_url": asset_url,
            "label": "AI concept cover",
            "disclaimer": "Synthetic mood illustration · not listing photography",
            "media_type": materialized["media_type"],
            "sha256": materialized["sha256"],
            "byte_length": materialized["byte_length"],
            "width": materialized["width"],
            "height": materialized["height"],
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
            "asset_materialized": True,
            "asset_sha256": materialized["sha256"],
            "asset_byte_length": materialized["byte_length"],
        },
        "publication": {
            "scope": "private_generated_asset",
            "status": "not_published",
            "external_publication_verified": False,
        },
    }
