from __future__ import annotations

import re
from typing import Any


PUBLIC_TOUR_GENERATED_VIEWER_RELEASE_CONTRACT = (
    "ea.public-tour-generated-viewer-release.v1"
)
GENERATED_RECONSTRUCTION_PROVIDER = "propertyquarry_generated_reconstruction"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GENERATED_RECONSTRUCTION_PREFIX = "generated-reconstruction/"
_MAX_PUBLIC_VIEWER_ASSET_SIZE_BYTES = 8 * 1024 * 1024
_VIEWER_MODULE_RELPATHS = (
    "generated-reconstruction/vendor/three.module.js",
    "generated-reconstruction/vendor/examples/jsm/controls/OrbitControls.js",
)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalized_provider(value: object) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", _text(value).lower()).strip("_")


def _safe_relpath(value: object) -> str:
    raw = _text(value).replace("\\", "/")
    if (
        not raw
        or raw.startswith("/")
        or "://" in raw
        or "\x00" in raw
        or any(character in raw for character in "\"'`<>&")
    ):
        return ""
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return ""
    return "/".join(parts)


def _unverified(reason: str = "generated_viewer_release_unverified") -> dict[str, Any]:
    return {
        "released": False,
        "reason": reason,
        "viewer_relpath": "",
        "bindings": {},
    }


def _explicit_public_model_binding_matches(
    payload: dict[str, object],
    *,
    path: str,
    binding: dict[str, object],
    expected_role: str,
    expected_mime_types: set[str],
) -> bool:
    public_assets = payload.get("public_assets")
    if not isinstance(public_assets, list):
        return False
    matches: list[dict[str, object]] = []
    for row in public_assets:
        if not isinstance(row, dict):
            continue
        declared_paths: set[str] = set()
        aliases_valid = True
        for key in ("path", "relpath", "asset_relpath"):
            raw_value = _text(row.get(key))
            if not raw_value:
                continue
            candidate = _safe_relpath(raw_value)
            if not candidate:
                aliases_valid = False
                break
            declared_paths.add(candidate)
        if path not in declared_paths:
            continue
        if not aliases_valid or declared_paths != {path}:
            return False
        matches.append(row)
    if len(matches) != 1:
        return False
    row = matches[0]
    row_sha256 = _text(row.get("sha256"))
    row_size_bytes = row.get("size_bytes")
    row_role = _text(row.get("role")).lower().replace("-", "_")
    row_mime_type = _text(row.get("mime_type")).lower()
    privacy_aliases = {
        _text(row.get(key)).lower()
        for key in ("privacy_class", "privacy")
        if _text(row.get(key))
    }
    role_aliases = {
        _text(row.get(key)).lower().replace("-", "_")
        for key in ("role", "asset_role")
        if _text(row.get(key))
    }
    mime_aliases = {
        _text(row.get(key)).lower()
        for key in ("mime_type", "content_type")
        if _text(row.get(key))
    }
    return (
        _text(row.get("privacy_class")).lower()
        == "generated_reconstruction_public"
        and privacy_aliases == {"generated_reconstruction_public"}
        and row_role == expected_role
        and role_aliases == {expected_role}
        and row_mime_type in expected_mime_types
        and mime_aliases == {row_mime_type}
        and bool(_SHA256_RE.fullmatch(row_sha256))
        and row_sha256 == binding.get("sha256")
        and type(row_size_bytes) is int
        and row_size_bytes == binding.get("size_bytes")
        and row_mime_type == binding.get("mime_type")
    )


def evaluate_public_tour_generated_viewer_release(
    payload: dict[str, object],
) -> dict[str, Any]:
    """Validate the complete, Property-owned public viewer release contract.

    The detached publication authority controls package installation. Runtime
    still validates every public byte binding and every explicit review gate so
    a partial, stale, or hand-edited bundle cannot become serveable.
    """

    generated = payload.get("generated_reconstruction")
    release = payload.get("generated_viewer_release")
    if not isinstance(generated, dict) or not isinstance(release, dict):
        return _unverified("generated_viewer_release_missing")
    if release.get("revoked") is True:
        return {**_unverified("generated_viewer_revoked"), "terminal": True}
    if release.get("disqualified") is True:
        return {**_unverified("generated_viewer_disqualified"), "terminal": True}

    viewer_relpath = _safe_relpath(generated.get("viewer_relpath"))
    manifest_relpath = _safe_relpath(generated.get("manifest_relpath"))
    floorplan_relpath = _safe_relpath(generated.get("floorplan_relpath"))
    raw_optional_model_assets = (
        (
            generated.get("model_relpath"),
            ".obj",
            "generated_reconstruction_model",
            {"model/obj"},
        ),
        (
            generated.get("material_relpath"),
            ".mtl",
            "generated_reconstruction_material",
            {"model/mtl"},
        ),
        (
            generated.get("glb_model_relpath"),
            ".glb",
            "generated_reconstruction_model",
            {"model/gltf-binary"},
        ),
    )
    declared_model_assets: list[tuple[str, str, set[str]]] = []
    for raw_path, suffix, role, mime_types in raw_optional_model_assets:
        if raw_path in (None, ""):
            continue
        model_relpath = _safe_relpath(raw_path)
        if (
            not model_relpath.startswith(_GENERATED_RECONSTRUCTION_PREFIX)
            or not model_relpath.lower().endswith(suffix)
        ):
            return _unverified()
        declared_model_assets.append((model_relpath, role, mime_types))
    optional_model_assets: list[tuple[str, str, set[str]]] = []
    public_assets = payload.get("public_assets")
    public_asset_rows = public_assets if isinstance(public_assets, list) else []
    for model_relpath, role, mime_types in declared_model_assets:
        attempts = [
            row
            for row in public_asset_rows
            if isinstance(row, dict)
            and any(
                _safe_relpath(row.get(key)) == model_relpath
                for key in ("path", "relpath", "asset_relpath")
            )
        ]
        if len(attempts) > 1:
            return _unverified()
        if attempts:
            optional_model_assets.append((model_relpath, role, mime_types))

    raw_photo_relpaths = generated.get("photo_relpaths")
    panel_count = generated.get("photo_reference_panel_count")
    if not isinstance(raw_photo_relpaths, list) or type(panel_count) is not int:
        return _unverified()
    photo_relpaths = [_safe_relpath(value) for value in raw_photo_relpaths]
    if (
        panel_count < 0
        or panel_count != len(raw_photo_relpaths)
        or any(not relpath for relpath in photo_relpaths)
        or any(
            not relpath.startswith(_GENERATED_RECONSTRUCTION_PREFIX)
            for relpath in photo_relpaths
        )
        or len(set(photo_relpaths)) != len(photo_relpaths)
    ):
        return _unverified()
    if (
        not viewer_relpath.startswith(_GENERATED_RECONSTRUCTION_PREFIX)
        or not viewer_relpath.lower().endswith(".html")
        or not manifest_relpath.startswith(_GENERATED_RECONSTRUCTION_PREFIX)
        or not manifest_relpath.lower().endswith(".json")
        or not floorplan_relpath.startswith(_GENERATED_RECONSTRUCTION_PREFIX)
    ):
        return _unverified()

    required_assets = [
        (viewer_relpath, "viewer_document", {"text/html"}),
        (manifest_relpath, "reconstruction_manifest", {"application/json"}),
        (
            floorplan_relpath,
            "floorplan_texture",
            {"image/jpeg", "image/png", "image/webp"},
        ),
        *[
            (path, "viewer_module", {"application/javascript", "text/javascript"})
            for path in _VIEWER_MODULE_RELPATHS
        ],
        *[
            (path, "photo_texture", {"image/jpeg", "image/png", "image/webp"})
            for path in photo_relpaths
        ],
        *optional_model_assets,
    ]
    required_paths = {path for path, _role, _mime_types in required_assets if path}

    raw_bindings = release.get("asset_bindings")
    if not isinstance(raw_bindings, list):
        return _unverified()
    bindings: dict[str, dict[str, object]] = {}
    for row in raw_bindings:
        if not isinstance(row, dict):
            return _unverified()
        path = _safe_relpath(row.get("path"))
        sha256 = _text(row.get("sha256")).lower()
        size_bytes = row.get("size_bytes")
        mime_type = _text(row.get("mime_type")).lower()
        role = _text(row.get("role")).lower()
        if (
            not path
            or path in bindings
            or not _SHA256_RE.fullmatch(sha256)
            or type(size_bytes) is not int
            or size_bytes <= 0
            or size_bytes > _MAX_PUBLIC_VIEWER_ASSET_SIZE_BYTES
            or not mime_type
            or not role
        ):
            return _unverified()
        bindings[path] = {
            "path": path,
            "sha256": sha256,
            "size_bytes": size_bytes,
            "mime_type": mime_type,
            "role": role,
        }

    disclosure = _text(release.get("disclosure"))
    receipt_hash_fields = (
        "browser_receipt_sha256",
        "source_provenance_receipt_sha256",
        "publication_authority_receipt_sha256",
        "security_review_receipt_sha256",
        "accessibility_review_receipt_sha256",
    )
    checks = (
        release.get("contract") == PUBLIC_TOUR_GENERATED_VIEWER_RELEASE_CONTRACT,
        _text(release.get("status")).lower() == "ready",
        release.get("revoked") is False,
        release.get("disqualified") is False,
        _normalized_provider(release.get("provider"))
        == GENERATED_RECONSTRUCTION_PROVIDER,
        _normalized_provider(generated.get("provider"))
        == GENERATED_RECONSTRUCTION_PROVIDER,
        generated.get("capture_mode") is False,
        generated.get("synthetic") is True,
        generated.get("verified_provider_capture") is False,
        generated.get("satisfies_verified_tour_gate") is False,
        release.get("capture_mode") is False,
        release.get("synthetic") is True,
        release.get("verified_provider_capture") is False,
        release.get("satisfies_verified_tour_gate") is False,
        release.get("public_activation_authority") is True,
        _text(generated.get("viewer_version"))
        == "propertyquarry_3d_tour_viewer_v3",
        viewer_relpath == _safe_relpath(release.get("viewer_relpath")),
        bool(viewer_relpath and manifest_relpath and floorplan_relpath),
        len(required_paths) == len(required_assets),
        set(bindings) == required_paths,
        all(
            bindings.get(path, {}).get("role") == role
            and bindings.get(path, {}).get("mime_type") in mime_types
            for path, role, mime_types in required_assets
        ),
        all(
            _explicit_public_model_binding_matches(
                payload,
                path=path,
                binding=bindings.get(path, {}),
                expected_role=role,
                expected_mime_types=mime_types,
            )
            for path, role, mime_types in optional_model_assets
        ),
        all(
            _SHA256_RE.fullmatch(_text(release.get(field)).lower())
            for field in receipt_hash_fields
        ),
        release.get("browser_interaction_verified") is True,
        release.get("visual_quality_review_passed") is True,
        release.get("security_review_passed") is True,
        release.get("accessibility_review_passed") is True,
        release.get("source_provenance_verified") is True,
        release.get("publication_authority_verified") is True,
        bool(_text(release.get("release_revision"))),
        bool(disclosure and disclosure == _text(generated.get("disclosure"))),
    )
    if not all(checks):
        return _unverified()
    return {
        "released": True,
        "reason": "generated_viewer_release_verified",
        "viewer_relpath": viewer_relpath,
        "bindings": bindings,
        "provider": GENERATED_RECONSTRUCTION_PROVIDER,
        "disclosure": disclosure,
        "release_revision": _text(release.get("release_revision")),
        "synthetic": True,
        "verified_provider_capture": False,
        "photo_reference_panel_count": panel_count,
    }
