from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import os
import pathlib
import re

from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import get_container
from app.container import AppContainer
from app.observability import runtime_build_identity
from app.product.property_search_schema import LATEST_PROPERTY_SEARCH_SCHEMA_VERSION
from app.product.property_search_storage import property_search_run_retention_policy
from app.services.id_austria_oidc import id_austria_provider_readiness

router = APIRouter(tags=["system"])

_RELEASE_MANIFEST_SCHEMA = "propertyquarry.release_manifest.v1"
_RELEASE_MANIFEST_JSON_START = "<!-- propertyquarry-release-manifest-json:start -->"
_RELEASE_MANIFEST_JSON_END = "<!-- propertyquarry-release-manifest-json:end -->"
_RELEASE_MANIFEST_FIELDS = (
    "release_manifest_schema",
    "release_product",
    "release_candidate_status",
    "release_repository",
    "release_repository_origin",
    "release_branch",
    "release_commit_sha",
    "release_public_origin",
    "release_artifact_set",
    "release_label",
    "release_generated_at",
    "release_verification_commands",
    "release_deployment_id",
)
_FULL_GIT_SHA = re.compile(r"[0-9a-f]{40}")
_RFC3339_UTC_SECONDS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_RELEASE_ARTIFACT_SET = re.compile(
    r"propertyquarry-generated-release-artifacts-v1@sha256:[0-9a-f]{64}"
)
_RELEASE_DEPLOYMENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_REPLICA_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
RELEASE_IDENTITY_RESPONSE_HEADERS = (
    ("release_commit_sha", "X-PropertyQuarry-Release-Commit"),
    ("release_image_digest", "X-PropertyQuarry-Release-Image"),
    ("release_deployment_id", "X-PropertyQuarry-Release-Deployment"),
    ("release_manifest_status", "X-PropertyQuarry-Release-Manifest-Status"),
    ("release_manifest_sha256", "X-PropertyQuarry-Release-Manifest-SHA256"),
    ("replica_id", "X-PropertyQuarry-Replica-ID"),
)


def _env_value(name: str) -> str:
    return str(os.environ.get(name) or "").strip()


def _release_manifest() -> dict[str, str]:
    manifest_values, manifest_errors = _load_release_manifest_values()
    public_origin = (
        _env_value("PROPERTYQUARRY_RELEASE_PUBLIC_ORIGIN")
        or _env_value("PROPERTYQUARRY_PUBLIC_BASE_URL")
        or _env_value("EA_PUBLIC_APP_BASE_URL")
    ).rstrip("/")
    overrides = {
        "release_repository": _env_value("PROPERTYQUARRY_RELEASE_REPOSITORY"),
        "release_repository_origin": _env_value("PROPERTYQUARRY_RELEASE_REPOSITORY_ORIGIN"),
        "release_branch": _env_value("PROPERTYQUARRY_RELEASE_BRANCH"),
        "release_commit_sha": _env_value("PROPERTYQUARRY_RELEASE_COMMIT_SHA"),
        "release_deployment_id": _env_value("PROPERTYQUARRY_RELEASE_DEPLOYMENT_ID"),
        "release_public_origin": public_origin,
        "release_artifact_set": _env_value("PROPERTYQUARRY_RELEASE_ARTIFACT_SET"),
        "release_label": _env_value("PROPERTYQUARRY_RELEASE_LABEL"),
        "release_generated_at": _env_value("PROPERTYQUARRY_RELEASE_GENERATED_AT"),
        "release_verification_commands": _env_value(
            "PROPERTYQUARRY_RELEASE_VERIFICATION_COMMANDS"
        ),
    }
    # Runtime configuration may corroborate immutable manifest authority but may
    # never synthesize or replace it. A missing tracked field therefore remains
    # missing and fail-closed even when a same-named environment value exists.
    payload = {key: manifest_values.get(key, "") for key in _RELEASE_MANIFEST_FIELDS}
    mismatches = sorted(
        key
        for key, override in overrides.items()
        if override
        and override != manifest_values.get(key)
    )
    manifest_sha256 = ""
    if not manifest_errors:
        manifest_sha256 = _release_manifest_sha256(manifest_values)
    if manifest_errors:
        payload["release_manifest_status"] = "invalid"
    elif mismatches:
        payload["release_manifest_status"] = "mismatch"
    else:
        payload["release_manifest_status"] = "complete"
    payload["release_manifest_sha256"] = manifest_sha256
    payload["release_manifest_mismatch_fields"] = ",".join(mismatches)
    payload["release_manifest_errors"] = ",".join(manifest_errors)
    return payload


@lru_cache(maxsize=1)
def _load_release_manifest_values() -> tuple[dict[str, str], tuple[str, ...]]:
    module_path = pathlib.Path(__file__).resolve()
    manifest_paths = (
        module_path.parents[4] / "docs" / "PROPERTYQUARRY_RELEASE_MANIFEST.md",
        module_path.parents[3] / "docs" / "PROPERTYQUARRY_RELEASE_MANIFEST.md",
    )
    manifest_path = next((path for path in manifest_paths if path.is_file()), None)
    if manifest_path is None:
        return {}, ("manifest_missing",)

    try:
        text = manifest_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {}, ("manifest_unreadable",)
    return _parse_release_manifest_document(text)


def _parse_release_manifest_document(text: str) -> tuple[dict[str, str], tuple[str, ...]]:
    if (
        text.count(_RELEASE_MANIFEST_JSON_START) != 1
        or text.count(_RELEASE_MANIFEST_JSON_END) != 1
    ):
        return {}, ("canonical_json_marker_count_invalid",)
    if text.index(_RELEASE_MANIFEST_JSON_START) > text.index(_RELEASE_MANIFEST_JSON_END):
        return {}, ("canonical_json_marker_order_invalid",)
    before_end, after_end = text.split(_RELEASE_MANIFEST_JSON_END, 1)
    before_start, marked = before_end.split(_RELEASE_MANIFEST_JSON_START, 1)
    if _RELEASE_MANIFEST_JSON_END in before_start or _RELEASE_MANIFEST_JSON_START in after_end:
        return {}, ("canonical_json_marker_order_invalid",)
    fenced = re.fullmatch(r"\s*```json\s*\n(?P<body>.*)\n```\s*", marked, flags=re.DOTALL)
    if fenced is None:
        return {}, ("canonical_json_fence_invalid",)

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        payload: dict[str, object] = {}
        for key, value in pairs:
            if key in payload:
                raise ValueError(f"duplicate_field:{key}")
            payload[key] = value
        return payload

    try:
        raw = json.loads(fenced.group("body"), object_pairs_hook=reject_duplicate_keys)
    except json.JSONDecodeError:
        return {}, ("canonical_json_invalid",)
    except ValueError as exc:
        return {}, (str(exc),)
    if not isinstance(raw, dict):
        return {}, ("canonical_json_root_not_object",)
    expected_fields = set(_RELEASE_MANIFEST_FIELDS)
    missing = sorted(expected_fields - set(raw))
    unexpected = sorted(set(raw) - expected_fields)
    errors = [f"missing_field:{field}" for field in missing]
    errors.extend(f"unexpected_field:{field}" for field in unexpected)
    values: dict[str, str] = {}
    for field in _RELEASE_MANIFEST_FIELDS:
        value = raw.get(field)
        if not isinstance(value, str):
            if field in raw:
                errors.append(f"non_string_field:{field}")
            continue
        normalized = value.strip()
        if not normalized:
            errors.append(f"empty_field:{field}")
        elif normalized != value:
            errors.append(f"surrounding_whitespace_field:{field}")
        elif any(ord(char) < 32 for char in normalized):
            errors.append(f"control_text_field:{field}")
        values[field] = normalized
    if values.get("release_manifest_schema") not in {None, _RELEASE_MANIFEST_SCHEMA}:
        errors.append("manifest_schema_invalid")
    commit_sha = values.get("release_commit_sha", "")
    if commit_sha and _FULL_GIT_SHA.fullmatch(commit_sha) is None:
        errors.append("release_commit_sha_invalid")
    generated_at = values.get("release_generated_at", "")
    if generated_at and _RFC3339_UTC_SECONDS.fullmatch(generated_at) is None:
        errors.append("release_generated_at_invalid")
    artifact_set = values.get("release_artifact_set", "")
    if artifact_set and _RELEASE_ARTIFACT_SET.fullmatch(artifact_set) is None:
        errors.append("release_artifact_set_invalid")
    return values, tuple(dict.fromkeys(errors))


def _release_manifest_sha256(values: dict[str, str]) -> str:
    canonical = json.dumps(
        values,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def release_runtime_identity() -> dict[str, str]:
    """Return the bounded, non-secret identity of this exact runtime replica.

    The same envelope is exposed by ``/version`` and attached to measured
    PropertyQuarry document responses. Invalid runtime or manifest values fail
    closed to an empty field instead of being reflected into an HTTP header.
    """

    runtime = runtime_build_identity()
    manifest = _release_manifest()
    commit_sha = manifest.get("release_commit_sha", "")
    image_digest = runtime.get("release_image_digest", "")
    deployment_id = manifest.get("release_deployment_id", "")
    manifest_status = manifest.get("release_manifest_status", "")
    manifest_sha256 = manifest.get("release_manifest_sha256", "")
    replica_id = runtime.get("replica_id", "")
    return {
        "release_commit_sha": (
            commit_sha
            if type(commit_sha) is str
            and _FULL_GIT_SHA.fullmatch(commit_sha) is not None
            else ""
        ),
        "release_image_digest": (
            image_digest
            if type(image_digest) is str
            and re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest) is not None
            else ""
        ),
        "release_deployment_id": (
            deployment_id
            if type(deployment_id) is str
            and _RELEASE_DEPLOYMENT_ID.fullmatch(deployment_id) is not None
            else ""
        ),
        "release_manifest_status": (
            manifest_status
            if manifest_status in {"complete", "invalid", "mismatch"}
            else "invalid"
        ),
        "release_manifest_sha256": (
            manifest_sha256
            if type(manifest_sha256) is str
            and re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is not None
            else ""
        ),
        "replica_id": (
            replica_id
            if type(replica_id) is str
            and _REPLICA_ID.fullmatch(replica_id) is not None
            else ""
        ),
    }


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return await health()


@router.get("/health/live")
async def health_live() -> dict[str, str]:
    return {"status": "live"}


@router.get("/health/ready")
async def health_ready(
    container: AppContainer = Depends(get_container),
) -> dict[str, str | int]:
    ready, reason = container.readiness.check()
    if not ready:
        raise HTTPException(status_code=503, detail=f"not_ready:{reason}")
    return {
        "status": "ready",
        "reason": reason,
        "property_search_schema_version": LATEST_PROPERTY_SEARCH_SCHEMA_VERSION,
    }


@router.get("/version")
async def version(container: AppContainer = Depends(get_container)) -> dict[str, str]:
    payload = {
        "app_name": container.settings.app_name,
        "version": container.settings.app_version,
        "role": container.settings.role,
        "storage_backend": container.settings.storage_backend,
    }
    payload.update(runtime_build_identity())
    payload.update(property_search_run_retention_policy())
    payload.update(id_austria_provider_readiness())
    # Apply immutable manifest authority last so no runtime helper can silently
    # overwrite a field after completeness and digest validation.
    payload.update(_release_manifest())
    # Reapply the bounded shared envelope last. This keeps /version and the
    # measured document-response headers on one exact identity contract.
    payload.update(release_runtime_identity())
    return payload
