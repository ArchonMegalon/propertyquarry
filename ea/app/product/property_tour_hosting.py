from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import re
import shutil
import stat
import threading
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Iterator, Mapping
from uuid import uuid4

import fcntl

from app.product.projections import compact_text
from app.product.property_search_storage import property_account_publication_authority

_PROPERTY_SCOUT_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0 Safari/537.36"
_PROPERTY_SCOUT_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
_PROPERTY_SCOUT_FLOORPLAN_ASSET_EXTENSIONS = (*_PROPERTY_SCOUT_IMAGE_EXTENSIONS, ".pdf")
_PROPERTY_PUBLIC_TOUR_PRIVATE_MANIFEST = "tour.private.json"
_PROPERTY_PUBLIC_TOUR_PRIVATE_RECEIPT_MERGE_KEYS = frozenset(
    {
        "principal_id",
        "search_run_id",
        "listing_url",
        "property_url",
        "source_ref",
        "external_id",
        "recipient_email",
        "crezlo_public_url",
        "source_virtual_tour_url",
        "source_virtual_tour_origin",
        "panorama_source",
        "three_d_vista_import",
        "three_d_vista_white_label_proof",
        "three_d_vista_browser_render_proof",
        "three_d_vista_entry_relpath",
        "three_d_vista_url",
        "matterport_url",
    }
)
_3DVISTA_EXPORT_MARKERS = ("tdvplayer", "tdvplayerapi", "tourviewer")
_PANO2VR_EXPORT_MARKERS = ("ggpkg", "ggskin", "pano.xml", "tour.js")
_KRPANO_PANORAMA_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
_KRPANO_FORBIDDEN_SCENE_STRATEGIES = {"generated_listing_summary", "photo_gallery_hosted", "floorplan_hosted", "pure_360_cube"}
_KRPANO_FORBIDDEN_CREATION_MODES = {"hosted_listing_fallback", "hosted_photo_gallery_tour"}
_CUSTOMER_FACING_TOUR_PROVIDERS = ("3dvista",)
_PROPERTY_GENERATED_RECONSTRUCTION_VIEWER_VERSION = "propertyquarry_3d_tour_viewer_v3"
_3DVISTA_FORBIDDEN_PUBLIC_MARKERS = (
    "created with the trial of 3dvista",
    "created with 3dvista",
    "3dvista virtual tour suite",
    "immocontract",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _first_non_empty_text(*values: object) -> str:
    for value in values:
        normalized = str(value or "").strip()
        if normalized:
            return normalized
    return ""


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on", "verified", "ready", "pass"}


def _hosted_property_tour_has_propertyquarry_3dvista_private_viewer_proof(payload: dict[str, object]) -> bool:
    proof = payload.get("three_d_vista_white_label_proof")
    proof_payload = dict(proof) if isinstance(proof, dict) else {}
    import_payload = payload.get("three_d_vista_import")
    import_payload = dict(import_payload) if isinstance(import_payload, dict) else {}
    source_project = str(
        proof_payload.get("source_project")
        or import_payload.get("source_project")
        or proof_payload.get("project")
        or import_payload.get("project")
        or ""
    ).strip().lower()
    source_project = re.sub(r"[^a-z0-9]+", "", source_project)
    if source_project not in {"propertyquarry", "propertyquarrycom"}:
        return False
    if _truthy(proof_payload.get("trial_branding_present")):
        return False
    return (
        _truthy(proof_payload.get("private_viewer_verified") or proof_payload.get("private_viewer_delivered"))
        and _truthy(proof_payload.get("non_trial_export_verified") or proof_payload.get("licensed_export_verified"))
        and _truthy(proof_payload.get("propertyquarry_tour_metadata") or proof_payload.get("property_tour_metadata_verified"))
        and _truthy(proof_payload.get("trial_branding_checked"))
    )


def _hosted_property_tour_has_3dvista_browser_render_proof(payload: dict[str, object]) -> bool:
    for key in (
        "three_d_vista_browser_render_proof",
        "threedvista_browser_render_proof",
        "3dvista_browser_render_proof",
        "browser_render_proof",
    ):
        proof = payload.get(key)
        if not isinstance(proof, dict):
            continue
        provider = str(proof.get("provider") or proof.get("viewer_provider") or "3dvista").strip().lower()
        if provider not in {"3dvista", "3d_vista", "three_d_vista"}:
            continue
        status = str(proof.get("status") or proof.get("result") or "").strip().lower()
        if status not in {"pass", "ready", "rendered"}:
            continue
        if _truthy(proof.get("rendered_viewer") or proof.get("viewer_rendered") or proof.get("browser_rendered")):
            return True
        checks = list(proof.get("checks") or [])
        if checks and all(isinstance(row, dict) and row.get("ok") is True for row in checks):
            return True
    return False


def _public_tour_dir() -> Path:
    raw_value = str(os.getenv("EA_PUBLIC_TOUR_DIR") or "").strip()
    if raw_value:
        return Path(raw_value).expanduser()
    try:
        from scripts.property_tour_runtime_paths import preferred_public_tour_root, running_container_public_tour_dir
    except Exception:
        try:
            from property_tour_runtime_paths import preferred_public_tour_root, running_container_public_tour_dir  # type: ignore[no-redef]
        except Exception:
            preferred_public_tour_root = None  # type: ignore[assignment]
            running_container_public_tour_dir = None  # type: ignore[assignment]
    if running_container_public_tour_dir is not None:
        runtime_root = running_container_public_tour_dir(os.getenv("PROPERTYQUARRY_RUNTIME_CONTAINER") or "")
        if isinstance(runtime_root, Path):
            return runtime_root.expanduser()
    if preferred_public_tour_root is not None:
        with_runtime_context = preferred_public_tour_root(
            configured_root="",
            repo_root=Path(__file__).resolve().parents[3],
            fallback_root="/docker/property/state/public_property_tours",
            runtime_container=os.getenv("PROPERTYQUARRY_RUNTIME_CONTAINER") or "",
        )
        if isinstance(with_runtime_context, Path):
            return with_runtime_context.expanduser()
    return Path("/docker/property/state/public_property_tours").expanduser()


def _public_tour_private_manifest_path(bundle_dir: Path) -> Path:
    return bundle_dir / _PROPERTY_PUBLIC_TOUR_PRIVATE_MANIFEST


def _public_tour_asset_max_bytes() -> int:
    raw_value = str(os.getenv("PROPERTYQUARRY_TOUR_ASSET_MAX_BYTES") or "").strip()
    if not raw_value:
        return 25_000_000
    try:
        parsed = int(raw_value)
    except Exception:
        return 25_000_000
    return max(1, min(parsed, 250_000_000))


def _public_tour_asset_content_type_allowed(content_type: str) -> bool:
    normalized = str(content_type or "").strip().lower()
    if not normalized:
        return True
    return (
        normalized.startswith("image/")
        or normalized.startswith("video/")
        or normalized in {"application/pdf", "application/octet-stream"}
    )


def _public_tour_public_payload(payload: dict[str, object]) -> dict[str, object]:
    from app.api.routes.public_tour_payloads import build_public_tour_manifest

    normalized_payload = dict(payload or {})
    slug = str(normalized_payload.get("slug") or "").strip()
    bundle_dir = _public_tour_dir() / slug if slug else None
    public_payload = build_public_tour_manifest(
        normalized_payload,
        expose_asset_relpaths=True,
        url_allowed=lambda _url: False,
        bundle_dir_resolver=lambda requested_slug: bundle_dir if bundle_dir and str(requested_slug or "").strip() == slug else None,
    ).as_dict()
    live_url = _safe_live_property_tour_url(
        normalized_payload.get("source_virtual_tour_url")
        or normalized_payload.get("source_virtual_tour_origin")
    )
    live_provider = _property_tour_provider_host_kind(live_url)
    if str(public_payload.get("control_mode") or "").strip().lower() not in _CUSTOMER_FACING_TOUR_PROVIDERS:
        public_payload.pop("control_mode", None)
    if live_provider in _CUSTOMER_FACING_TOUR_PROVIDERS:
        public_payload["control_mode"] = live_provider
        if not public_payload.get("scenes"):
            public_payload["scenes"] = [
                {
                    "name": "3D tour",
                    "role": "live_360",
                    "image_url": "",
                    "mime_type": "image/jpeg",
                }
            ]
    return public_payload


def _public_tour_private_receipt(payload: dict[str, object]) -> dict[str, object]:
    from app.api.routes.public_tour_payloads import PrivateTourReceipt

    return PrivateTourReceipt.from_payload(payload).as_dict()


def _validate_hosted_property_tour_private_receipt_target(private_manifest_path: Path) -> None:
    try:
        target_stat = private_manifest_path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeError("hosted_property_tour_private_receipt_invalid") from exc
    if stat.S_ISLNK(target_stat.st_mode) or not stat.S_ISREG(target_stat.st_mode):
        raise RuntimeError("hosted_property_tour_private_receipt_invalid")


def _write_hosted_property_tour_private_receipt_atomic(
    bundle_dir: Path,
    private_payload: dict[str, object],
) -> None:
    private_name = _PROPERTY_PUBLIC_TOUR_PRIVATE_MANIFEST
    temporary_name = f".{private_name}.{uuid4().hex}.tmp"
    directory_fd = -1
    temporary_fd = -1
    final_fd = -1
    temporary_created = False
    replaced = False
    try:
        directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        directory_fd = os.open(bundle_dir, directory_flags)
        try:
            existing_stat = os.stat(private_name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing_stat = None
        if existing_stat is not None and (
            stat.S_ISLNK(existing_stat.st_mode) or not stat.S_ISREG(existing_stat.st_mode)
        ):
            raise RuntimeError("hosted_property_tour_private_receipt_invalid")

        temporary_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        temporary_flags |= getattr(os, "O_NOFOLLOW", 0)
        temporary_fd = os.open(temporary_name, temporary_flags, 0o600, dir_fd=directory_fd)
        temporary_created = True
        os.fchmod(temporary_fd, 0o600)
        encoded = json.dumps(private_payload, ensure_ascii=False, indent=2).encode("utf-8")
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(temporary_fd, remaining)
            if written <= 0:
                raise OSError("private_receipt_short_write")
            remaining = remaining[written:]
        os.fsync(temporary_fd)
        temporary_stat = os.fstat(temporary_fd)

        try:
            replacement_stat = os.stat(private_name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            replacement_stat = None
        if replacement_stat is not None and (
            stat.S_ISLNK(replacement_stat.st_mode) or not stat.S_ISREG(replacement_stat.st_mode)
        ):
            raise RuntimeError("hosted_property_tour_private_receipt_invalid")
        os.replace(
            temporary_name,
            private_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        replaced = True
        final_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        final_fd = os.open(private_name, final_flags, dir_fd=directory_fd)
        final_stat = os.fstat(final_fd)
        if (
            not stat.S_ISREG(final_stat.st_mode)
            or final_stat.st_dev != temporary_stat.st_dev
            or final_stat.st_ino != temporary_stat.st_ino
            or stat.S_IMODE(final_stat.st_mode) != 0o600
        ):
            raise RuntimeError("hosted_property_tour_private_receipt_verification_failed")
        os.fsync(directory_fd)
    except RuntimeError:
        raise
    except OSError as exc:
        raise RuntimeError("hosted_property_tour_private_receipt_write_failed") from exc
    finally:
        if final_fd >= 0:
            os.close(final_fd)
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if directory_fd >= 0:
            if temporary_created and not replaced:
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
            os.close(directory_fd)


def _normalize_generated_reconstruction_bundle_permissions(bundle_dir: Path) -> None:
    reconstruction_dir = bundle_dir / "generated-reconstruction"
    public_manifest_path = bundle_dir / "tour.json"

    def _chmod_if_needed(path: Path, expected_mode: int) -> None:
        current_mode = stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)
        if current_mode != expected_mode:
            path.chmod(expected_mode, follow_symlinks=False)

    try:
        if bundle_dir.is_symlink() or not bundle_dir.is_dir():
            raise RuntimeError("property_reconstruction_bundle_invalid")
        if public_manifest_path.is_symlink() or not public_manifest_path.is_file():
            raise RuntimeError("property_reconstruction_manifest_invalid")
        if reconstruction_dir.is_symlink() or not reconstruction_dir.is_dir():
            raise RuntimeError("property_reconstruction_directory_invalid")

        _chmod_if_needed(bundle_dir, 0o755)
        _chmod_if_needed(public_manifest_path, 0o644)
        for root, directory_names, filenames in os.walk(reconstruction_dir, followlinks=False):
            root_path = Path(root)
            if root_path.is_symlink():
                raise RuntimeError("property_reconstruction_asset_symlink_forbidden")
            _chmod_if_needed(root_path, 0o755)
            for directory_name in directory_names:
                directory_path = root_path / directory_name
                if directory_path.is_symlink() or not directory_path.is_dir():
                    raise RuntimeError("property_reconstruction_asset_symlink_forbidden")
                _chmod_if_needed(directory_path, 0o755)
            for filename in filenames:
                asset_path = root_path / filename
                if asset_path.is_symlink() or not asset_path.is_file():
                    raise RuntimeError("property_reconstruction_asset_symlink_forbidden")
                _chmod_if_needed(asset_path, 0o644)
    except OSError as exc:
        raise RuntimeError("property_reconstruction_permissions_failed") from exc


def _write_hosted_property_tour_payload(bundle_dir: Path, payload: dict[str, object]) -> None:
    incoming_owner = str(payload.get("principal_id") or "").strip()
    search_run_id = str(payload.get("search_run_id") or "").strip()
    private_manifest_path = _public_tour_private_manifest_path(bundle_dir)
    _validate_hosted_property_tour_private_receipt_target(private_manifest_path)
    if (bundle_dir / "tour.json").exists() or _public_tour_private_manifest_path(bundle_dir).exists():
        existing_private = _load_hosted_property_tour_private_receipt(bundle_dir)
        existing_owner = str(existing_private.get("principal_id") or "").strip()
        if existing_owner:
            if not incoming_owner or not hmac.compare_digest(existing_owner, incoming_owner):
                raise RuntimeError("hosted_property_tour_owner_mismatch")
        elif incoming_owner:
            raise RuntimeError("hosted_property_tour_legacy_owner_missing")
    public_payload = _public_tour_public_payload(payload)
    private_payload = _public_tour_private_receipt(payload)

    def _commit_payload() -> None:
        bundle_dir.mkdir(parents=True, exist_ok=True)
        _write_hosted_property_tour_private_receipt_atomic(bundle_dir, private_payload)
        (bundle_dir / "tour.json").write_text(
            json.dumps(public_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    if incoming_owner:
        with property_account_publication_authority(
            incoming_owner,
            run_id=search_run_id,
        ):
            _commit_payload()
    else:
        _commit_payload()


def _load_hosted_property_tour_private_receipt(bundle_dir: Path) -> dict[str, object]:
    private_manifest_path = _public_tour_private_manifest_path(bundle_dir)
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(private_manifest_path, flags)
        descriptor_stat = os.fstat(descriptor)
        if not stat.S_ISREG(descriptor_stat.st_mode) or descriptor_stat.st_size > 1_048_576:
            return {}
        chunks: list[bytes] = []
        remaining = int(descriptor_stat.st_size)
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 65_536))
            if not chunk:
                return {}
            chunks.append(chunk)
            remaining -= len(chunk)
        private_payload = json.loads(b"".join(chunks).decode("utf-8"))
    except Exception:
        return {}
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(private_payload, dict):
        return {}
    return {
        str(key): value
        for key, value in dict(private_payload).items()
        if str(key) in _PROPERTY_PUBLIC_TOUR_PRIVATE_RECEIPT_MERGE_KEYS
    }


def _owned_hosted_property_tour_private_receipt(bundle_dir: Path, *, principal_id: str) -> dict[str, object]:
    requested_principal = str(principal_id or "").strip()
    if not requested_principal:
        return {}
    private_payload = _load_hosted_property_tour_private_receipt(bundle_dir)
    owner_principal = str(private_payload.get("principal_id") or "").strip()
    if not owner_principal or not hmac.compare_digest(owner_principal, requested_principal):
        return {}
    return private_payload


def _load_hosted_property_tour_payload(bundle_dir: Path, *, principal_id: str = "") -> dict[str, object]:
    manifest_path = bundle_dir / "tour.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    requested_principal = str(principal_id or "").strip()
    if requested_principal:
        private_payload = _owned_hosted_property_tour_private_receipt(
            bundle_dir,
            principal_id=requested_principal,
        )
        if private_payload:
            payload = {**dict(payload), **private_payload}
    return payload


def _public_hosted_property_tour_live_source_url(bundle_dir: Path) -> str:
    """Return only the provider URL intentionally exposed by a public share."""

    private_payload = _load_hosted_property_tour_private_receipt(bundle_dir)
    live_url = _safe_live_property_tour_url(
        private_payload.get("source_virtual_tour_url")
        or private_payload.get("source_virtual_tour_origin")
    )
    if _property_tour_provider_host_kind(live_url) not in _CUSTOMER_FACING_TOUR_PROVIDERS:
        return ""
    return live_url


def persist_hosted_property_tour_browser_render_proof(
    *,
    slug: str,
    provider: str,
    proof: dict[str, object],
    public_roots: Iterable[Path | str] | None = None,
) -> dict[str, object]:
    normalized_slug = str(slug or "").strip()
    normalized_provider = str(provider or "").strip().lower()
    if not normalized_slug or "/" in normalized_slug or ".." in normalized_slug:
        return {"status": "invalid_slug", "slug": normalized_slug, "provider": normalized_provider}
    if normalized_provider != "3dvista":
        return {"status": "unsupported_provider", "slug": normalized_slug, "provider": normalized_provider}
    proof_payload = dict(proof or {})
    proof_payload.setdefault("provider", "3dvista")
    candidate_roots = list(public_roots or [_public_tour_dir()])
    seen_roots: set[str] = set()
    updated_private_manifests: list[str] = []
    for raw_root in candidate_roots:
        try:
            root = Path(raw_root).expanduser().resolve()
        except OSError:
            continue
        root_key = str(root)
        if root_key in seen_roots or not root.exists() or not root.is_dir():
            continue
        seen_roots.add(root_key)
        bundle_dir = root / normalized_slug
        manifest_path = bundle_dir / "tour.json"
        if not manifest_path.is_file():
            continue
        private_manifest_path = _public_tour_private_manifest_path(bundle_dir)
        private_payload: dict[str, object] = {}
        if private_manifest_path.is_file():
            try:
                loaded_private = json.loads(private_manifest_path.read_text(encoding="utf-8"))
            except Exception:
                loaded_private = {}
            if isinstance(loaded_private, dict):
                private_payload = dict(loaded_private)
        private_payload["three_d_vista_browser_render_proof"] = proof_payload
        private_manifest_path.write_text(
            json.dumps(private_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        updated_private_manifests.append(str(private_manifest_path))
    if not updated_private_manifests:
        return {"status": "tour_bundle_not_found", "slug": normalized_slug, "provider": normalized_provider}
    return {
        "status": "updated",
        "slug": normalized_slug,
        "provider": normalized_provider,
        "updated_private_manifests": updated_private_manifests,
    }


def _hosted_property_tour_revocation_path(slug: str) -> Path:
    return _public_tour_dir() / ".revocations" / f"{str(slug or '').strip()}.json"


def hosted_property_tour_revocation_receipt(slug: str) -> dict[str, object]:
    normalized_slug = str(slug or "").strip()
    if not normalized_slug or "/" in normalized_slug or ".." in normalized_slug:
        return {}
    receipt_path = _hosted_property_tour_revocation_path(normalized_slug)
    try:
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _enqueue_hosted_property_tour_cdn_purge(*, slug: str, revoked_at: str) -> dict[str, object]:
    normalized_slug = str(slug or "").strip()
    purge_dir = _public_tour_dir() / ".cdn-purge-outbox"
    purge_dir.mkdir(parents=True, exist_ok=True)
    purge_path = purge_dir / f"{normalized_slug}.json"
    if purge_path.exists():
        try:
            existing = json.loads(purge_path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
        if isinstance(existing, dict) and existing:
            return dict(existing)
    public_base = _hosted_property_tour_public_base_url().rstrip("/")
    receipt: dict[str, object] = {
        "purge_request_id": f"tour-purge-{hashlib.sha256(normalized_slug.encode('utf-8')).hexdigest()[:20]}",
        "slug": normalized_slug,
        "status": "queued",
        "queued_at": revoked_at,
        "attempt_count": 0,
        "provider_invoked": False,
        "surrogate_keys": [f"propertyquarry-tour-{normalized_slug}"],
        "urls": [
            f"{public_base}/{normalized_slug}",
            f"{public_base}/{normalized_slug}.json",
            f"{public_base}/files/{normalized_slug}/*",
            f"{public_base}/3dvista/{normalized_slug}/*",
            f"{public_base}/pano2vr/{normalized_slug}/*",
        ],
        "next_action": "cdn_worker_purge_then_record_receipt",
    }
    purge_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    return receipt


def list_hosted_property_tours_for_principal(*, principal_id: str) -> tuple[dict[str, object], ...]:
    normalized_principal = str(principal_id or "").strip()
    if not normalized_principal:
        return ()
    public_dir = _public_tour_dir()
    rows: list[dict[str, object]] = []
    try:
        candidates = tuple(public_dir.iterdir()) if public_dir.exists() and public_dir.is_dir() else ()
    except OSError:
        candidates = ()
    for bundle_dir in candidates:
        if not bundle_dir.is_dir() or bundle_dir.name.startswith("."):
            continue
        private_receipt = _owned_hosted_property_tour_private_receipt(
            bundle_dir,
            principal_id=normalized_principal,
        )
        if not private_receipt:
            continue
        public_manifest = _load_hosted_property_tour_payload(bundle_dir)
        rows.append(
            {
                "slug": bundle_dir.name,
                "status": "active",
                "public_manifest": public_manifest,
                "private_receipt": private_receipt,
                "revocation": {},
            }
        )
    principal_digest = hashlib.sha256(normalized_principal.encode("utf-8")).hexdigest()
    revocation_dir = public_dir / ".revocations"
    try:
        revocation_paths = tuple(revocation_dir.iterdir()) if revocation_dir.exists() and revocation_dir.is_dir() else ()
    except OSError:
        revocation_paths = ()
    for receipt_path in revocation_paths:
        if not receipt_path.is_file() or receipt_path.suffix.lower() != ".json":
            continue
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(receipt, dict) or not hmac.compare_digest(
            str(receipt.get("principal_id_sha256") or ""),
            principal_digest,
        ):
            continue
        rows.append(
            {
                "slug": str(receipt.get("slug") or receipt_path.stem).strip(),
                "status": "revoked",
                "public_manifest": {},
                "private_receipt": {},
                "revocation": dict(receipt),
            }
        )
    rows.sort(key=lambda row: (str(row.get("slug") or ""), str(row.get("status") or "")))
    return tuple(rows)


@contextmanager
def _hosted_property_tour_publication_lock(
    *,
    public_dir: Path,
    slug: str,
) -> Iterator[None]:
    """Join the generated publisher's exact per-slug filesystem lock."""

    # service owns the dependency-neutral lock implementation today and also
    # imports this hosting module. Import lazily so both paths share one lock
    # derivation and inode without creating a module-import cycle.
    from app.product.service import _property_reconstruction_publication_lock

    with _property_reconstruction_publication_lock(
        public_dir=public_dir,
        slug=slug,
    ):
        yield


def _remove_revoked_hosted_property_tour_bundle(bundle_dir: Path) -> None:
    if bundle_dir.is_symlink() or bundle_dir.is_file():
        bundle_dir.unlink(missing_ok=True)
    elif bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    if bundle_dir.exists() or bundle_dir.is_symlink():
        raise RuntimeError("hosted_property_tour_revocation_removal_failed")


def _revoke_hosted_property_tour_bundle_with_lock_held(
    *,
    normalized_slug: str,
    requested_principal: str,
    actor: str,
    public_dir: Path,
) -> dict[str, object]:
    existing_revocation = hosted_property_tour_revocation_receipt(normalized_slug)
    requested_digest = hashlib.sha256(requested_principal.encode("utf-8")).hexdigest() if requested_principal else ""
    if (
        existing_revocation
        and requested_digest
        and hmac.compare_digest(str(existing_revocation.get("principal_id_sha256") or ""), requested_digest)
    ):
        _remove_revoked_hosted_property_tour_bundle(public_dir / normalized_slug)
        return {
            "status": "revoked",
            "slug": normalized_slug,
            "revoked_at": str(existing_revocation.get("revoked_at") or ""),
            "removed_file_count": int(existing_revocation.get("removed_file_count") or 0),
            "already_revoked": True,
            "cdn_purge": dict(existing_revocation.get("cdn_purge") or {}),
        }
    root = public_dir.resolve()
    canonical_bundle_dir = public_dir / normalized_slug
    if canonical_bundle_dir.is_symlink():
        return {"status": "not_found", "slug": normalized_slug}
    bundle_dir = canonical_bundle_dir.resolve()
    if bundle_dir == root or root not in bundle_dir.parents or not bundle_dir.exists() or not bundle_dir.is_dir():
        return {"status": "not_found", "slug": normalized_slug}
    manifest_path = bundle_dir / "tour.json"
    if not manifest_path.exists():
        return {"status": "not_found", "slug": normalized_slug}
    private_payload = _owned_hosted_property_tour_private_receipt(
        bundle_dir,
        principal_id=requested_principal,
    )
    owner_principal = str(private_payload.get("principal_id") or "").strip()
    if not requested_principal or not owner_principal:
        return {"status": "not_found", "slug": normalized_slug}
    payload = _load_hosted_property_tour_payload(bundle_dir)
    revoked_at = _now_iso()
    file_count = sum(1 for path in bundle_dir.rglob("*") if path.is_file())
    receipt_dir = public_dir / ".revocations"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    cdn_purge = _enqueue_hosted_property_tour_cdn_purge(slug=normalized_slug, revoked_at=revoked_at)
    (receipt_dir / f"{normalized_slug}.json").write_text(
        json.dumps(
            {
                "slug": normalized_slug,
                "status": "revoked",
                "revoked_at": revoked_at,
                "principal_id_sha256": hashlib.sha256(owner_principal.encode("utf-8")).hexdigest() if owner_principal else "",
                "actor": str(actor or "").strip()[:120],
                "removed_file_count": file_count,
                "previous_public_url": f"{_hosted_property_tour_public_base_url()}/{normalized_slug}",
                "previous_title": str(payload.get("display_title") or payload.get("title") or "").strip()[:220],
                "cdn_purge": cdn_purge,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    _remove_revoked_hosted_property_tour_bundle(canonical_bundle_dir)
    return {
        "status": "revoked",
        "slug": normalized_slug,
        "revoked_at": revoked_at,
        "removed_file_count": file_count,
        "already_revoked": False,
        "cdn_purge": cdn_purge,
    }


def revoke_hosted_property_tour_bundle(*, slug: str, principal_id: str = "", actor: str = "") -> dict[str, object]:
    normalized_slug = str(slug or "").strip()
    if not normalized_slug or "/" in normalized_slug or ".." in normalized_slug:
        return {"status": "not_found", "slug": normalized_slug}
    requested_principal = str(principal_id or "").strip()
    public_dir = _public_tour_dir()
    with _hosted_property_tour_publication_lock(
        public_dir=public_dir,
        slug=normalized_slug,
    ):
        return _revoke_hosted_property_tour_bundle_with_lock_held(
            normalized_slug=normalized_slug,
            requested_principal=requested_principal,
            actor=actor,
            public_dir=public_dir,
        )


def _configured_public_tour_hosts() -> tuple[str, ...]:
    hosts: list[str] = []
    for raw in (
        str(os.getenv("PROPERTYQUARRY_PUBLIC_TOUR_BASE_URL") or "").strip(),
        str(os.getenv("PROPERTYQUARRY_PUBLIC_BASE_URL") or "").strip(),
        str(os.getenv("EA_PUBLIC_TOUR_BASE_URL") or "").strip(),
        str(os.getenv("EA_PUBLIC_APP_BASE_URL") or "").strip(),
    ):
        if not raw:
            continue
        parsed = urllib.parse.urlparse(raw if "://" in raw else f"https://{raw}")
        host = str(parsed.hostname or "").strip().lower()
        if host and host not in hosts:
            hosts.append(host)
    return tuple(hosts)

def _public_app_base_url() -> str:
    return str(os.getenv("EA_PUBLIC_APP_BASE_URL") or "https://propertyquarry.com").strip().rstrip("/")

def _property_public_app_base_url() -> str:
    explicit = str(os.getenv("PROPERTYQUARRY_PUBLIC_APP_BASE_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    explicit = str(os.getenv("PROPERTYQUARRY_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    return "https://propertyquarry.com"

def _property_public_tour_base_url() -> str:
    explicit = str(os.getenv("PROPERTYQUARRY_PUBLIC_TOUR_BASE_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    return f"{_property_public_app_base_url()}/tours"

def _hosted_property_tour_public_base_url() -> str:
    explicit = str(os.getenv("EA_PUBLIC_TOUR_BASE_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    public_app = str(os.getenv("EA_PUBLIC_APP_BASE_URL") or "").strip().rstrip("/")
    if public_app:
        return f"{public_app}/tours"
    return _property_public_tour_base_url()

def _workspace_access_public_base_url() -> str:
    explicit = str(os.getenv("EA_PUBLIC_APP_BASE_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    redirect_uri = str(os.getenv("EA_GOOGLE_OAUTH_REDIRECT_URI") or "").strip()
    if redirect_uri:
        parsed = urllib.parse.urlparse(redirect_uri)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    return _public_app_base_url()

def _is_crezlo_tour_host(value: object) -> bool:
    normalized = str(value or "").strip()
    if not normalized:
        return False
    parsed = urllib.parse.urlparse(normalized if "://" in normalized else f"https://{normalized}")
    host = str(parsed.hostname or "").strip().lower()
    return "crezlo" in host

def _is_branded_public_tour_url(value: object) -> bool:
    normalized = str(value or "").strip()
    if not normalized:
        return False
    parsed = urllib.parse.urlparse(normalized if "://" in normalized else f"https://{normalized}")
    host = str(parsed.hostname or "").strip().lower()
    if not host:
        return False
    configured_hosts = _configured_public_tour_hosts()
    if configured_hosts:
        return host in configured_hosts
    branded_domains = ("myexternalbrain.com", "propertyquarry.com")
    branded_host = any(host == domain or host.endswith(f".{domain}") for domain in branded_domains)
    if branded_host and str(parsed.path or "").startswith("/tours/"):
        return not _is_crezlo_tour_host(normalized)
    return False

def _resolve_property_tour_urls(
    structured_output: dict[str, object],
    *,
    allow_unverified_branded: bool = False,
) -> tuple[str, str]:
    hosted_url = _first_non_empty_text(structured_output.get("hosted_url"))
    public_url = _first_non_empty_text(structured_output.get("public_url"))
    share_url = _first_non_empty_text(structured_output.get("share_url"))
    crezlo_public_url = _first_non_empty_text(structured_output.get("crezlo_public_url"))
    source_live_360_url = _embedded_live_360_source_url(structured_output)
    source_live_360_provider = _property_tour_provider_host_kind(source_live_360_url)
    if source_live_360_provider not in _CUSTOMER_FACING_TOUR_PROVIDERS:
        source_live_360_url = ""
    branded_candidates = [
        candidate
        for candidate in (
            hosted_url,
            public_url,
            crezlo_public_url,
            share_url,
        )
        if _is_branded_public_tour_url(candidate)
    ]
    branded_tour_url = ""
    for candidate in branded_candidates:
        verified_provider = _hosted_property_tour_verified_provider(candidate)
        if verified_provider in _CUSTOMER_FACING_TOUR_PROVIDERS:
            branded_tour_url = candidate
            break
        if allow_unverified_branded:
            if source_live_360_provider and source_live_360_provider not in _CUSTOMER_FACING_TOUR_PROVIDERS:
                continue
            candidate_payload = _hosted_property_tour_payload_for_url(candidate)
            candidate_source_provider = _property_tour_provider_host_kind(
                _embedded_live_360_source_url(candidate_payload)
            )
            if not candidate_source_provider:
                branded_tour_url = candidate
                break
    vendor_tour_url = _first_non_empty_text(
        source_live_360_url,
        public_url
        if (
            public_url
            and not _is_branded_public_tour_url(public_url)
            and public_url != branded_tour_url
            and _property_tour_provider_host_kind(public_url) in _CUSTOMER_FACING_TOUR_PROVIDERS
        )
        else "",
        share_url
        if (
            share_url
            and not _is_branded_public_tour_url(share_url)
            and share_url != branded_tour_url
            and _property_tour_provider_host_kind(share_url) in _CUSTOMER_FACING_TOUR_PROVIDERS
        )
        else "",
        crezlo_public_url
        if (
            crezlo_public_url
            and not _is_branded_public_tour_url(crezlo_public_url)
            and crezlo_public_url != branded_tour_url
            and _property_tour_provider_host_kind(crezlo_public_url) in _CUSTOMER_FACING_TOUR_PROVIDERS
        )
        else "",
    )
    return branded_tour_url, vendor_tour_url

def _property_tour_payload_is_disabled_fallback(structured_output: dict[str, object]) -> bool:
    normalized = dict(structured_output or {})
    scene_strategy = str(normalized.get("scene_strategy") or "").strip().lower()
    creation_mode = str(normalized.get("creation_mode") or "").strip().lower()
    control_mode = str(normalized.get("control_mode") or "").strip().lower()
    if scene_strategy in {"generated_listing_summary", "photo_gallery_hosted", "floorplan_hosted", "pure_360_cube"}:
        return True
    if creation_mode in {"hosted_listing_fallback", "hosted_floorplan_tour", "hosted_photo_gallery_tour"}:
        return True
    if control_mode in {"walkable_3d", "internal_walkable_3d"}:
        return True
    scenes = [dict(entry) for entry in (normalized.get("scenes") or []) if isinstance(entry, dict)]
    if any(str(scene.get("role") or "").strip() == "generated_overview" for scene in scenes):
        return True
    return False


def _hosted_property_tour_slug_from_url(value: object) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    try:
        parsed = urllib.parse.urlparse(normalized)
    except Exception:
        return ""
    path_parts = [part for part in str(parsed.path or "").split("/") if part]
    for index, part in enumerate(path_parts):
        if part == "tours" and index + 1 < len(path_parts):
            if path_parts[index + 1] == "files" and index + 2 < len(path_parts):
                return str(path_parts[index + 2] or "").strip()
            return str(path_parts[index + 1] or "").strip()
    return ""


def _hosted_property_tour_payload_for_url(
    tour_url: object,
    *,
    principal_id: object = "",
) -> dict[str, object]:
    normalized_url = str(tour_url or "").strip()
    if not normalized_url:
        return {}
    parsed = urllib.parse.urlparse(normalized_url)
    if parsed.scheme or parsed.netloc:
        if not _is_branded_public_tour_url(normalized_url):
            return {}
    elif not str(parsed.path or "").startswith("/tours/"):
        return {}
    slug = _hosted_property_tour_slug_from_url(normalized_url)
    if not slug:
        return {}
    payload = _load_hosted_property_tour_payload(
        _public_tour_dir() / slug,
        principal_id=str(principal_id or "").strip(),
    )
    return dict(payload) if isinstance(payload, dict) else {}


def _hosted_property_tour_control_url(tour_url: object, *, viewer: str = "") -> str:
    normalized = str(tour_url or "").strip()
    if not normalized:
        return ""
    viewer_slug = str(viewer or "").strip().lower()
    if viewer_slug == "metaport":
        viewer_slug = "matterport"
    if viewer_slug in {"pano_2_vr", "pano-2-vr"}:
        viewer_slug = "pano2vr"
    if viewer_slug in {"kr_pano", "kr-pano"}:
        viewer_slug = "krpano"
    if viewer_slug not in {"", "matterport", "3dvista", "pano2vr", "krpano"}:
        viewer_slug = ""
    try:
        parsed = urllib.parse.urlparse(normalized)
        path = str(parsed.path or "").rstrip("/")
        if any(path.endswith(f"/control/{mode}") for mode in ("matterport", "3dvista", "pano2vr", "krpano")):
            path = path.rsplit("/control/", 1)[0]
        elif path.endswith("/control"):
            path = path[: -len("/control")]
        path = f"{path}/control/{viewer_slug}" if viewer_slug else f"{path}/control"
        return urllib.parse.urlunparse(parsed._replace(path=path, query="", fragment=""))
    except Exception:
        base = normalized.rstrip("/")
        return f"{base}/control/{viewer_slug}" if viewer_slug else f"{base}/control"


def _hosted_property_tour_has_matterport_export(tour_url: object) -> bool:
    payload = _hosted_property_tour_payload_for_url(tour_url)
    for key in ("matterport_url", "source_virtual_tour_url", "crezlo_public_url"):
        value = str(payload.get(key) or "").strip()
        if value and _property_tour_provider_host_kind(value) == "matterport":
            return True
    return False


def _hosted_property_tour_entry_has_marker(bundle_dir: Path, relpath: object, *, markers: tuple[str, ...]) -> bool:
    raw_relpath = str(relpath or "").strip().replace("\\", "/")
    if not raw_relpath or raw_relpath.startswith("/") or "://" in raw_relpath or "\x00" in raw_relpath:
        return False
    path = PurePosixPath(raw_relpath)
    if any(part in {"", ".", ".."} for part in path.parts):
        return False
    if path.suffix.lower() not in {".htm", ".html"}:
        return False
    try:
        candidate = (bundle_dir / "/".join(path.parts)).resolve()
        resolved_bundle = bundle_dir.resolve()
        if candidate == resolved_bundle or resolved_bundle not in candidate.parents or not candidate.is_file():
            return False
        body = candidate.read_text(encoding="utf-8", errors="replace")[:200_000].lower()
    except OSError:
        return False
    return any(marker in body for marker in markers)


def _hosted_property_tour_has_3dvista_export(
    tour_url: object,
    *,
    principal_id: object = "",
) -> bool:
    payload = _hosted_property_tour_payload_for_url(tour_url, principal_id=principal_id)
    if not _hosted_property_tour_has_propertyquarry_3dvista_private_viewer_proof(payload):
        return False
    if not _hosted_property_tour_has_3dvista_browser_render_proof(payload):
        return False
    for key in ("three_d_vista_url", "threedvista_url", "3dvista_url", "source_virtual_tour_url", "crezlo_public_url"):
        value = str(payload.get(key) or "").strip()
        if value and _property_tour_provider_host_kind(value) == "3dvista":
            return True
    slug = _hosted_property_tour_slug_from_url(tour_url)
    if not slug:
        return False
    bundle_dir = (_public_tour_dir() / slug).resolve()
    for key in ("three_d_vista_entry_relpath", "threedvista_entry_relpath", "3dvista_entry_relpath"):
        entry_relpath = str(payload.get(key) or "").strip().lstrip("/")
        if not entry_relpath:
            continue
        if _hosted_property_tour_entry_has_marker(bundle_dir, entry_relpath, markers=_3DVISTA_FORBIDDEN_PUBLIC_MARKERS):
            return False
        if _hosted_property_tour_entry_has_marker(bundle_dir, entry_relpath, markers=_3DVISTA_EXPORT_MARKERS):
            return True
    return False


def _hosted_property_tour_has_pano2vr_export(tour_url: object) -> bool:
    payload = _hosted_property_tour_payload_for_url(tour_url)
    slug = _hosted_property_tour_slug_from_url(tour_url)
    if not payload or not slug:
        return False
    bundle_dir = (_public_tour_dir() / slug).resolve()
    for key in ("pano2vr_entry_relpath", "pano2vr_export_entry_relpath"):
        if _hosted_property_tour_entry_has_marker(bundle_dir, payload.get(key), markers=_PANO2VR_EXPORT_MARKERS):
            return True
    return False


def _hosted_property_tour_file_exists(bundle_dir: Path, relpath: object) -> bool:
    return _hosted_property_tour_asset_path(bundle_dir, relpath) is not None


def _hosted_property_tour_asset_path(bundle_dir: Path, relpath: object) -> Path | None:
    normalized = str(relpath or "").strip().replace("\\", "/").lstrip("/")
    if not normalized:
        return None
    parts = [part for part in normalized.split("/") if part and part not in {".", ".."}]
    if not parts or any(part in {".propertyquarry-publish-token", ".publish..staging"} for part in parts):
        return None
    safe_relpath = "/".join(parts)
    try:
        candidate = (bundle_dir / safe_relpath).resolve()
        resolved_bundle = bundle_dir.resolve()
        if resolved_bundle not in candidate.parents or not candidate.is_file():
            return None
    except OSError:
        return None
    return candidate


def _hosted_property_tour_image_dimensions(path: Path) -> tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(path) as image:
            return int(image.width), int(image.height)
    except Exception:
        return (0, 0)


def _hosted_property_tour_is_equirectangular_image(bundle_dir: Path, relpath: object) -> bool:
    candidate = _hosted_property_tour_asset_path(bundle_dir, relpath)
    if candidate is None or PurePosixPath(str(relpath or "")).suffix.lower() not in _KRPANO_PANORAMA_IMAGE_EXTENSIONS:
        return False
    width, height = _hosted_property_tour_image_dimensions(candidate)
    if width < 1024 or height < 512:
        return False
    ratio = width / height if height else 0
    return 1.75 <= ratio <= 2.25


def _hosted_property_tour_is_cube_face_image(bundle_dir: Path, relpath: object) -> bool:
    candidate = _hosted_property_tour_asset_path(bundle_dir, relpath)
    if candidate is None or PurePosixPath(str(relpath or "")).suffix.lower() not in _KRPANO_PANORAMA_IMAGE_EXTENSIONS:
        return False
    width, height = _hosted_property_tour_image_dimensions(candidate)
    if width < 512 or height < 512:
        return False
    ratio = width / height if height else 0
    return 0.9 <= ratio <= 1.1


def _hosted_property_tour_has_walkable_360_asset(*, bundle_dir: Path, payload: dict[str, object]) -> bool:
    scene_strategy = str(payload.get("scene_strategy") or "").strip().lower()
    creation_mode = str(payload.get("creation_mode") or "").strip().lower()
    if scene_strategy in _KRPANO_FORBIDDEN_SCENE_STRATEGIES or creation_mode in _KRPANO_FORBIDDEN_CREATION_MODES:
        return False
    walkable_scene = payload.get("walkable_scene")
    if not isinstance(walkable_scene, dict) or not walkable_scene:
        return False
    projection = str(walkable_scene.get("projection") or walkable_scene.get("type") or "").strip().lower()
    if projection and projection not in {"equirectangular", "panorama", "cubemap", "cube"}:
        return False
    for key in ("panorama_relpath", "equirect_relpath", "image_relpath", "asset_relpath"):
        relpath = str(walkable_scene.get(key) or "").strip()
        if relpath and _hosted_property_tour_is_equirectangular_image(bundle_dir, relpath):
            return True
    cube_faces = walkable_scene.get("cube_faces")
    if isinstance(cube_faces, dict):
        values = list(cube_faces.values())
    elif isinstance(cube_faces, list):
        values = cube_faces
    else:
        values = []
    valid_faces = [
        value
        for value in values
        if _hosted_property_tour_is_cube_face_image(bundle_dir, value)
    ]
    return len(valid_faces) >= 6


def _krpano_license_runtime_config() -> dict[str, str]:
    domain = str(os.getenv("KRPANO_LICENSE_DOMAIN") or "").strip()
    key = str(os.getenv("KRPANO_LICENSE_KEY") or "").strip()
    if not domain or not key:
        return {}
    return {"domain": domain, "key": key}


def _hosted_property_tour_has_krpano_control(tour_url: object) -> bool:
    payload = _hosted_property_tour_payload_for_url(tour_url)
    slug = _hosted_property_tour_slug_from_url(tour_url)
    if not payload or not slug or not _krpano_license_runtime_config():
        return False
    scenes = [dict(entry) for entry in (payload.get("scenes") or []) if isinstance(entry, dict)]
    if any(str(scene.get("role") or "").strip() == "generated_overview" for scene in scenes):
        return False
    bundle_dir = (_public_tour_dir() / slug).resolve()
    return _hosted_property_tour_has_walkable_360_asset(bundle_dir=bundle_dir, payload=payload)


def _hosted_property_tour_verified_provider(
    tour_url: object,
    *,
    principal_id: object = "",
) -> str:
    normalized_url = str(tour_url or "").strip()
    if not normalized_url:
        return ""
    direct_provider = _property_tour_provider_host_kind(normalized_url)
    if direct_provider in _CUSTOMER_FACING_TOUR_PROVIDERS:
        return direct_provider
    payload = _hosted_property_tour_payload_for_url(normalized_url, principal_id=principal_id)
    if not payload:
        return ""
    # A hosted bundle can be floorplan/layout-first while still carrying a
    # verified provider control. Provider evidence must win over fallback shape.
    if _hosted_property_tour_has_3dvista_export(normalized_url, principal_id=principal_id):
        return "3dvista"
    if _property_tour_payload_is_disabled_fallback(payload):
        return ""
    return ""


def _hosted_property_tour_verified_open_url(
    tour_url: object,
    *,
    principal_id: object = "",
) -> str:
    normalized_url = str(tour_url or "").strip()
    if not normalized_url:
        return ""
    provider = _hosted_property_tour_verified_provider(normalized_url, principal_id=principal_id)
    if not provider:
        return ""
    if _property_tour_provider_host_kind(normalized_url) == provider:
        return normalized_url
    return _hosted_property_tour_control_url(normalized_url, viewer=provider)


def _hosted_property_tour_generated_reconstruction_open_url(tour_url: object) -> str:
    normalized_url = str(tour_url or "").strip()
    if not normalized_url:
        return ""
    slug = _hosted_property_tour_slug_from_url(normalized_url)
    if not slug:
        return ""
    bundle_dir = _public_tour_dir() / slug
    manifest_path = bundle_dir / "tour.json"
    if not manifest_path.exists():
        return ""
    payload = _load_hosted_property_tour_payload(bundle_dir)
    if not payload or not isinstance(payload, dict):
        return ""
    generated_reconstruction = payload.get("generated_reconstruction")
    if not isinstance(generated_reconstruction, dict):
        return ""
    contract = _hosted_property_tour_generated_reconstruction_contract(bundle_dir=bundle_dir, payload=payload)
    if not contract.get("ready"):
        return ""
    return normalized_url


def _hosted_property_tour_first_party_open_url(
    tour_url: object,
    *,
    principal_id: object = "",
) -> str:
    normalized_url = str(tour_url or "").strip()
    if not normalized_url:
        return ""
    verified_url = _hosted_property_tour_verified_open_url(
        normalized_url,
        principal_id=principal_id,
    )
    if verified_url:
        return verified_url
    generated_reconstruction_url = _hosted_property_tour_generated_reconstruction_open_url(normalized_url)
    if generated_reconstruction_url:
        parsed = urllib.parse.urlparse(normalized_url)
        slug = _hosted_property_tour_slug_from_url(normalized_url)
        if not slug:
            return generated_reconstruction_url
        if not parsed.scheme and not parsed.netloc and str(parsed.path or "").startswith("/tours/"):
            return f"/tours/{slug}"
        if parsed.scheme and parsed.netloc:
            return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, f"/tours/{slug}", "", "", ""))
    return ""


def _hosted_property_tour_walkthrough_asset_url(tour_url: object) -> str:
    normalized_url = str(tour_url or "").strip()
    if not normalized_url:
        return ""
    slug = _hosted_property_tour_slug_from_url(normalized_url)
    if not slug:
        return ""
    bundle_dir = _public_tour_dir() / slug
    manifest_path = bundle_dir / "tour.json"
    if not manifest_path.exists():
        return ""
    payload = _load_hosted_property_tour_payload(bundle_dir)
    if not payload or not isinstance(payload, dict):
        return ""
    generated_reconstruction = (
        dict(payload.get("generated_reconstruction") or {})
        if isinstance(payload.get("generated_reconstruction"), dict)
        else {}
    )
    video_relpath = str(payload.get("video_relpath") or "").strip().lstrip("/")
    if video_relpath:
        video_path = (bundle_dir / video_relpath).resolve()
        if bundle_dir.resolve() in video_path.parents and video_path.exists() and video_path.is_file():
            provider_key = str(
                payload.get("video_provider")
                or payload.get("video_provider_key")
                or payload.get("video_render_provider")
                or ""
            ).strip().lower()
            coverage_proof = str(payload.get("video_coverage_proof") or "").strip()
            generated_video_providers = {"magicfit", "onemin_i2v", "ea_one_manager_onemin_i2v", "poppy_ai"}
            if provider_key and (
                provider_key not in generated_video_providers
                or coverage_proof == "boundary_verified_frame_continuation"
            ):
                asset_url = _hosted_public_tour_asset_url(normalized_url, slug=slug, asset_relpath=video_relpath)
                if asset_url:
                    return asset_url
    generated_walkthrough_url = _hosted_property_tour_generated_reconstruction_asset_url(
        normalized_url,
        asset_key="walkthrough_video_relpath",
    )
    generated_coverage = (
        dict(generated_reconstruction.get("walkthrough_coverage_proof") or {})
        if isinstance(generated_reconstruction.get("walkthrough_coverage_proof"), dict)
        else {}
    )
    if generated_walkthrough_url and str(generated_coverage.get("status") or "").strip().lower() == "pass":
        return generated_walkthrough_url
    return ""


def _hosted_property_tour_walkthrough_open_url(tour_url: object, walkthrough_url: object = "") -> str:
    normalized_tour_url = str(tour_url or "").strip()
    normalized_walkthrough_url = str(walkthrough_url or "").strip()
    verified_walkthrough_url = _hosted_property_tour_walkthrough_asset_url(normalized_tour_url) if normalized_tour_url else ""
    published_walkthrough_url = _published_walkthrough_asset_url(normalized_walkthrough_url)
    if not published_walkthrough_url and normalized_walkthrough_url.startswith("/tours/"):
        published_walkthrough_url = normalized_walkthrough_url
    canonical_walkthrough_url = verified_walkthrough_url or published_walkthrough_url
    if not canonical_walkthrough_url:
        return ""
    slug = _hosted_property_tour_slug_from_url(normalized_tour_url) or _hosted_property_tour_slug_from_url(canonical_walkthrough_url)
    if not slug:
        return canonical_walkthrough_url
    for source_url in (normalized_tour_url, canonical_walkthrough_url):
        normalized_source = str(source_url or "").strip()
        if not normalized_source:
            continue
        parsed = urllib.parse.urlparse(normalized_source)
        if not parsed.scheme and not parsed.netloc and str(parsed.path or "").startswith("/tours/"):
            return f"/tours/{slug}?pane=flythrough-pane&autoplay=1"
        if not parsed.scheme or not parsed.netloc:
            continue
        branded_tour_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, f"/tours/{slug}", "", "", ""))
        if _is_branded_public_tour_url(branded_tour_url):
            return urllib.parse.urlunparse(
                (
                    parsed.scheme,
                    parsed.netloc,
                    f"/tours/{slug}",
                    "",
                    "pane=flythrough-pane&autoplay=1",
                    "",
                )
            )
    return canonical_walkthrough_url


def _generated_reconstruction_relpath_file(bundle_dir: Path, relpath: object) -> Path | None:
    normalized = str(relpath or "").strip().lstrip("/")
    if not normalized or ".propertyquarry-publish-token" in normalized.replace("\\", "/").split("/"):
        return None
    try:
        resolved_bundle = bundle_dir.resolve()
        asset_path = (bundle_dir / normalized).resolve()
        if resolved_bundle not in asset_path.parents or not asset_path.is_file():
            return None
    except OSError:
        return None
    return asset_path


def _hosted_property_tour_generated_reconstruction_contract(
    *,
    bundle_dir: Path,
    payload: dict[str, object],
) -> dict[str, object]:
    if "publication_status" in payload and payload.get("publication_status") != "ready":
        return {"ready": False}
    generated_reconstruction = payload.get("generated_reconstruction")
    if not isinstance(generated_reconstruction, dict):
        return {"ready": False}
    provider = str(generated_reconstruction.get("provider") or "").strip().lower()
    if provider != "propertyquarry_generated_reconstruction":
        return {"ready": False}
    if _truthy(generated_reconstruction.get("verified_provider_capture")):
        return {"ready": False}
    viewer_version = str(generated_reconstruction.get("viewer_version") or "").strip()
    if viewer_version != _PROPERTY_GENERATED_RECONSTRUCTION_VIEWER_VERSION:
        return {"ready": False}
    viewer_relpath = str(generated_reconstruction.get("viewer_relpath") or "").strip().lstrip("/")
    viewer_path = _generated_reconstruction_relpath_file(bundle_dir, viewer_relpath)
    if not viewer_path:
        return {"ready": False}
    manifest_relpath = str(generated_reconstruction.get("manifest_relpath") or "").strip().lstrip("/")
    manifest_path = _generated_reconstruction_relpath_file(bundle_dir, manifest_relpath)
    if not manifest_path:
        return {"ready": False}
    try:
        receipt = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return {"ready": False}
    if not isinstance(receipt, dict):
        return {"ready": False}
    if str(receipt.get("provider") or "").strip().lower() != "propertyquarry_generated_reconstruction":
        return {"ready": False}
    if _truthy(receipt.get("verified_provider_capture")) or _truthy(receipt.get("satisfies_verified_tour_gate")):
        return {"ready": False}
    receipt_viewer = dict(receipt.get("viewer") or {}) if isinstance(receipt.get("viewer"), dict) else {}
    if str(receipt_viewer.get("version") or "").strip() != viewer_version:
        return {"ready": False}
    model_relpath = str(generated_reconstruction.get("model_relpath") or "").strip().lstrip("/")
    material_relpath = str(generated_reconstruction.get("material_relpath") or "").strip().lstrip("/")
    model_path = _generated_reconstruction_relpath_file(bundle_dir, model_relpath)
    material_path = _generated_reconstruction_relpath_file(bundle_dir, material_relpath)
    if not model_path or not material_path:
        return {"ready": False}
    model_receipt = dict(receipt.get("model") or {}) if isinstance(receipt.get("model"), dict) else {}
    glb_export = dict(model_receipt.get("glb_export") or {}) if isinstance(model_receipt.get("glb_export"), dict) else {}
    glb_model_relpath = str(generated_reconstruction.get("glb_model_relpath") or "").strip().lstrip("/")
    glb_model_path: Path | None = None
    if (
        str(generated_reconstruction.get("glb_export_status") or "").strip().lower() == "generated"
        and str(glb_export.get("status") or "").strip().lower() == "generated"
        and glb_model_relpath
    ):
        candidate_glb_path = _generated_reconstruction_relpath_file(bundle_dir, glb_model_relpath)
        if candidate_glb_path is not None:
            try:
                if candidate_glb_path.stat().st_size > 1024:
                    glb_model_path = candidate_glb_path
                else:
                    glb_model_relpath = ""
            except OSError:
                glb_model_relpath = ""
        else:
            glb_model_relpath = ""
    else:
        glb_model_relpath = ""
    photo_paths = [
        path
        for path in (
            _generated_reconstruction_relpath_file(bundle_dir, raw_relpath)
            for raw_relpath in list(generated_reconstruction.get("photo_relpaths") or [])
        )
        if path is not None
    ]
    # Flagship-quality generated tours need at least a small reference deck, not a single still.
    if len(photo_paths) < 2:
        return {"ready": False}
    floorplan_relpath = str(generated_reconstruction.get("floorplan_relpath") or "").strip().lstrip("/")
    floorplan_path = _generated_reconstruction_relpath_file(bundle_dir, floorplan_relpath)
    if not floorplan_path and len(photo_paths) < 3:
        return {"ready": False}
    geometry = dict(receipt.get("geometry") or {}) if isinstance(receipt.get("geometry"), dict) else {}
    try:
        wall_rect_count = int(geometry.get("wall_rect_count") or 0)
    except Exception:
        wall_rect_count = 0
    if wall_rect_count < 4:
        return {"ready": False}
    room_dimensions = (
        dict(receipt.get("room_dimensions_m") or {})
        if isinstance(receipt.get("room_dimensions_m"), dict)
        else {}
    )
    try:
        width_m = float(room_dimensions.get("width") or 0.0)
        depth_m = float(room_dimensions.get("depth") or 0.0)
        height_m = float(room_dimensions.get("height") or 0.0)
    except Exception:
        return {"ready": False}
    if width_m <= 0.0 or depth_m <= 0.0 or height_m <= 0.0:
        return {"ready": False}
    route_labels = [
        str(label or "").strip()
        for label in list(generated_reconstruction.get("route_labels") or receipt.get("route_labels") or [])
        if str(label or "").strip()
    ]
    if not route_labels:
        return {"ready": False}
    try:
        room_stop_count = int(generated_reconstruction.get("room_stop_count") or len(route_labels))
    except Exception:
        return {"ready": False}
    if room_stop_count <= 0 or room_stop_count != len(route_labels):
        return {"ready": False}
    walkthrough_relpath = str(generated_reconstruction.get("walkthrough_video_relpath") or "").strip().lstrip("/")
    walkthrough_path = _generated_reconstruction_relpath_file(bundle_dir, walkthrough_relpath)
    if not walkthrough_path:
        return {"ready": False}
    walkthrough_sidecar_relpath = str(generated_reconstruction.get("walkthrough_sidecar_relpath") or "").strip().lstrip("/")
    walkthrough_sidecar_path = _generated_reconstruction_relpath_file(bundle_dir, walkthrough_sidecar_relpath)
    if not walkthrough_sidecar_path:
        return {"ready": False}
    walkthrough_route_labels = [
        str(label or "").strip()
        for label in list(generated_reconstruction.get("walkthrough_route_labels") or receipt.get("walkthrough_route_labels") or [])
        if str(label or "").strip()
    ]
    try:
        walkthrough_stop_count = int(generated_reconstruction.get("walkthrough_stop_count") or len(walkthrough_route_labels))
    except Exception:
        return {"ready": False}
    if not walkthrough_route_labels or walkthrough_stop_count <= 0 or walkthrough_stop_count != len(walkthrough_route_labels):
        return {"ready": False}
    if walkthrough_stop_count < room_stop_count:
        return {"ready": False}
    walkthrough_coverage = (
        dict(generated_reconstruction.get("walkthrough_coverage_proof") or {})
        if isinstance(generated_reconstruction.get("walkthrough_coverage_proof"), dict)
        else {}
    )
    if not walkthrough_coverage and isinstance(receipt.get("walkthrough"), dict):
        walkthrough_payload = dict(receipt.get("walkthrough") or {})
        walkthrough_coverage = (
            dict(walkthrough_payload.get("coverage_proof") or {})
            if isinstance(walkthrough_payload.get("coverage_proof"), dict)
            else {}
        )
    if str(walkthrough_coverage.get("status") or "").strip().lower() != "pass":
        return {"ready": False}
    if str(payload.get("video_relpath") or "").strip().lstrip("/") != walkthrough_relpath:
        return {"ready": False}
    if str(payload.get("video_sidecar_relpath") or "").strip().lstrip("/") != walkthrough_sidecar_relpath:
        return {"ready": False}
    if str(payload.get("video_provider") or "").strip() != "propertyquarry_generated_reconstruction":
        return {"ready": False}
    if str(payload.get("video_provider_key") or "").strip() != "propertyquarry_generated_reconstruction":
        return {"ready": False}
    if str(payload.get("video_coverage_proof") or "").strip() != "boundary_verified_frame_continuation":
        return {"ready": False}
    walkable_scene = (
        dict(generated_reconstruction.get("walkable_scene") or {})
        if isinstance(generated_reconstruction.get("walkable_scene"), dict)
        else {}
    )
    if not walkable_scene and isinstance(receipt.get("walkable_scene"), dict):
        walkable_scene = dict(receipt.get("walkable_scene") or {})
    if str(walkable_scene.get("kind") or "").strip() != "generated_reconstruction_layout":
        return {"ready": False}
    route_stops = list(walkable_scene.get("route") or []) if isinstance(walkable_scene.get("route"), list) else []
    room_stops = list(walkable_scene.get("rooms") or []) if isinstance(walkable_scene.get("rooms"), list) else []
    if len(route_stops) != room_stop_count or len(room_stops) != room_stop_count:
        return {"ready": False}
    route_stop_labels: list[str] = []
    room_stop_labels: list[str] = []
    for stop in route_stops:
        if not isinstance(stop, dict):
            return {"ready": False}
        label = str(stop.get("label") or stop.get("room") or stop.get("name") or "").strip()
        focus = dict(stop.get("focus") or {}) if isinstance(stop.get("focus"), dict) else {}
        camera = dict(stop.get("camera") or {}) if isinstance(stop.get("camera"), dict) else {}
        if not label or not focus or not camera:
            return {"ready": False}
        route_stop_labels.append(label)
    for room in room_stops:
        if not isinstance(room, dict):
            return {"ready": False}
        label = str(room.get("label") or room.get("room") or room.get("name") or "").strip()
        position = dict(room.get("position") or {}) if isinstance(room.get("position"), dict) else {}
        focus = dict(room.get("focus") or {}) if isinstance(room.get("focus"), dict) else {}
        if not label or not position or not focus:
            return {"ready": False}
        room_stop_labels.append(label)
    if [label.lower() for label in route_stop_labels] != [label.lower() for label in route_labels]:
        return {"ready": False}
    if [label.lower() for label in room_stop_labels] != [label.lower() for label in route_labels]:
        return {"ready": False}
    return {
        "ready": True,
        "viewer_relpath": viewer_relpath,
        "manifest_relpath": manifest_relpath,
        "model_relpath": model_relpath,
        "material_relpath": material_relpath,
        "glb_model_relpath": glb_model_relpath,
        "floorplan_relpath": floorplan_relpath,
        "photo_relpaths": [str(path.relative_to(bundle_dir)).replace("\\", "/") for path in photo_paths],
        "route_labels": route_labels,
        "room_stop_count": room_stop_count,
        "walkthrough_video_relpath": walkthrough_relpath,
        "walkthrough_sidecar_relpath": walkthrough_sidecar_relpath,
        "walkthrough_route_labels": walkthrough_route_labels,
        "walkthrough_stop_count": walkthrough_stop_count,
    }


def _hosted_property_tour_generated_reconstruction_asset_url(tour_url: object, *, asset_key: str = "viewer_relpath") -> str:
    normalized_url = str(tour_url or "").strip()
    normalized_key = str(asset_key or "viewer_relpath").strip()
    if not normalized_url or normalized_key not in {
        "viewer_relpath",
        "model_relpath",
        "material_relpath",
        "glb_model_relpath",
        "floorplan_relpath",
        "walkthrough_video_relpath",
    }:
        return ""
    slug = _hosted_property_tour_slug_from_url(normalized_url)
    if not slug:
        return ""
    bundle_dir = _public_tour_dir() / slug
    manifest_path = bundle_dir / "tour.json"
    if not manifest_path.exists():
        return ""
    payload = _load_hosted_property_tour_payload(bundle_dir)
    if not payload or not isinstance(payload, dict):
        return ""
    generated_reconstruction = payload.get("generated_reconstruction")
    if not isinstance(generated_reconstruction, dict):
        return ""
    provider = str(generated_reconstruction.get("provider") or "").strip().lower()
    if provider != "propertyquarry_generated_reconstruction":
        return ""
    if bool(generated_reconstruction.get("verified_provider_capture")):
        return ""
    if normalized_key == "viewer_relpath" and (
        generated_reconstruction.get("verified_provider_capture") is not False
        or generated_reconstruction.get("satisfies_verified_tour_gate") is not False
    ):
        return ""
    viewer_version = str(generated_reconstruction.get("viewer_version") or "").strip()
    if viewer_version != _PROPERTY_GENERATED_RECONSTRUCTION_VIEWER_VERSION:
        return ""
    relpath = str(generated_reconstruction.get(normalized_key) or "").strip().lstrip("/")
    if not relpath:
        return ""
    if normalized_key == "viewer_relpath" and not relpath.startswith("generated-reconstruction/"):
        return ""
    unresolved_asset_path = bundle_dir
    for part in PurePosixPath(relpath).parts:
        if part in {"", ".", ".."}:
            return ""
        unresolved_asset_path = unresolved_asset_path / part
        if unresolved_asset_path.is_symlink():
            return ""
    asset_path = unresolved_asset_path.resolve()
    if bundle_dir.resolve() not in asset_path.parents or not asset_path.exists() or not asset_path.is_file():
        return ""
    public_asset_url = _hosted_public_tour_asset_url(normalized_url, slug=slug, asset_relpath=relpath)
    if normalized_key == "viewer_relpath":
        return public_asset_url.replace("/tours/files/", "/tours/viewer/", 1)
    return public_asset_url


def _hosted_property_tour_generated_reconstruction_bundle_ready(tour_url: object) -> bool:
    normalized_url = str(tour_url or "").strip()
    if not normalized_url:
        return False
    slug = _hosted_property_tour_slug_from_url(normalized_url)
    if not slug:
        return False
    bundle_dir = _public_tour_dir() / slug
    manifest_path = bundle_dir / "tour.json"
    if not manifest_path.exists():
        return False
    payload = _load_hosted_property_tour_payload(bundle_dir)
    if not payload or not isinstance(payload, dict):
        return False
    return bool(_hosted_property_tour_generated_reconstruction_contract(bundle_dir=bundle_dir, payload=payload).get("ready"))


def _hosted_property_tour_generated_reconstruction_asset_urls(
    tour_url: object,
    *,
    asset_key: str = "photo_relpaths",
) -> tuple[str, ...]:
    normalized_url = str(tour_url or "").strip()
    normalized_key = str(asset_key or "photo_relpaths").strip()
    if not normalized_url or normalized_key not in {"photo_relpaths"}:
        return ()
    slug = _hosted_property_tour_slug_from_url(normalized_url)
    if not slug:
        return ()
    bundle_dir = _public_tour_dir() / slug
    manifest_path = bundle_dir / "tour.json"
    if not manifest_path.exists():
        return ()
    payload = _load_hosted_property_tour_payload(bundle_dir)
    if not payload or not isinstance(payload, dict):
        return ()
    generated_reconstruction = payload.get("generated_reconstruction")
    if not isinstance(generated_reconstruction, dict):
        return ()
    provider = str(generated_reconstruction.get("provider") or "").strip().lower()
    if provider != "propertyquarry_generated_reconstruction":
        return ()
    if bool(generated_reconstruction.get("verified_provider_capture")):
        return ()
    viewer_version = str(generated_reconstruction.get("viewer_version") or "").strip()
    if viewer_version != _PROPERTY_GENERATED_RECONSTRUCTION_VIEWER_VERSION:
        return ()
    urls: list[str] = []
    for raw_relpath in list(generated_reconstruction.get(normalized_key) or []):
        relpath = str(raw_relpath or "").strip().lstrip("/")
        if not relpath:
            continue
        asset_path = (bundle_dir / relpath).resolve()
        if bundle_dir.resolve() not in asset_path.parents or not asset_path.exists() or not asset_path.is_file():
            continue
        asset_url = _hosted_public_tour_asset_url(normalized_url, slug=slug, asset_relpath=relpath)
        if asset_url and asset_url not in urls:
            urls.append(asset_url)
    return tuple(urls)


def _published_walkthrough_asset_url(value: object) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    try:
        parsed = urllib.parse.urlparse(normalized)
    except Exception:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    if Path(str(parsed.path or "")).suffix.lower() not in {".mp4", ".m4v", ".mov", ".webm"}:
        return ""
    path_parts = [part for part in str(parsed.path or "").split("/") if part]
    if len(path_parts) >= 4 and path_parts[0] == "tours" and path_parts[1] == "files":
        slug = str(path_parts[2] or "").strip()
        hosted_tour_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, f"/tours/{slug}", "", "", ""))
        if _is_branded_public_tour_url(hosted_tour_url):
            manifest_payload = _hosted_property_tour_payload_for_url(hosted_tour_url)
            verified_asset_url = _hosted_property_tour_walkthrough_asset_url(hosted_tour_url)
            canonical_candidate_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
            if manifest_payload and (not verified_asset_url or canonical_candidate_url != verified_asset_url):
                return ""
            if not manifest_payload:
                return canonical_candidate_url
            return verified_asset_url
    return normalized

def _existing_hosted_property_tour_url(structured_output: dict[str, object]) -> str:
    slug = str(structured_output.get("slug") or "").strip()
    if not slug:
        return ""
    base_url = _hosted_property_tour_public_base_url()
    public_dir = _public_tour_dir()
    bundle_dir = public_dir / slug
    bundle_manifest = public_dir / slug / "tour.json"
    if not bundle_manifest.exists():
        return ""
    payload = _load_hosted_property_tour_payload(bundle_dir)
    if not payload:
        return ""
    scenes = [dict(entry) for entry in (payload.get("scenes") or []) if isinstance(entry, dict)]
    generated_reconstruction = (
        payload.get("generated_reconstruction")
        if isinstance(payload.get("generated_reconstruction"), dict)
        else {}
    )
    generated_viewer_relpath = str(generated_reconstruction.get("viewer_relpath") or "").strip().replace("\\", "/").lstrip("/")
    source_virtual_tour_url = str(
        payload.get("source_virtual_tour_url")
        or payload.get("source_virtual_tour_origin")
        or ""
    ).strip() or _public_hosted_property_tour_live_source_url(bundle_dir)
    hosted_url = f"{base_url}/{slug}"
    if generated_viewer_relpath:
        generated_viewer_path = (bundle_dir / generated_viewer_relpath).resolve()
        if bundle_dir.resolve() in generated_viewer_path.parents and generated_viewer_path.exists() and generated_viewer_path.is_file():
            return ""
    if source_virtual_tour_url and not scenes:
        return f"{hosted_url}#live-360"
    if not scenes:
        return ""
    has_asset = False
    for scene in scenes:
        asset_relpath = str(scene.get("asset_relpath") or "").strip()
        if not asset_relpath:
            continue
        candidate = (bundle_dir / asset_relpath).resolve()
        if bundle_dir.resolve() not in candidate.parents:
            continue
        if candidate.exists() and candidate.is_file():
            has_asset = True
            break
    if not has_asset:
        if source_virtual_tour_url:
            return f"{hosted_url}#live-360"
        return ""
    if source_virtual_tour_url:
        return f"{hosted_url}#live-360"
    return hosted_url

def _existing_hosted_property_tour_payload(slug: str, *, principal_id: str = "") -> dict[str, object]:
    normalized_slug = str(slug or "").strip()
    requested_principal = str(principal_id or "").strip()
    if not normalized_slug or not requested_principal:
        return {}
    public_dir = _public_tour_dir()
    payload = _load_hosted_property_tour_payload(
        public_dir / normalized_slug,
        principal_id=requested_principal,
    )
    payload_owner = str(payload.get("principal_id") or "").strip()
    if not payload_owner or not hmac.compare_digest(payload_owner, requested_principal):
        return {}
    hosted_url = _existing_hosted_property_tour_url({"slug": normalized_slug})
    canonical_url = f"{_hosted_property_tour_public_base_url()}/{normalized_slug}"
    if not hosted_url and not _hosted_property_tour_generated_reconstruction_bundle_ready(canonical_url):
        return {}
    payload = dict(payload)
    payload["slug"] = normalized_slug
    payload["hosted_url"] = hosted_url or canonical_url
    payload["public_url"] = hosted_url or canonical_url
    payload["tour_cache_status"] = "existing"
    payload.setdefault("creation_mode", "hosted_property_tour")
    return payload


def _normalized_property_tour_identity_url(value: object) -> str:
    return urllib.parse.urldefrag(str(value or "").strip())[0]


def _existing_hosted_property_tour_url_for_identity(
    *,
    property_url: object = "",
    source_ref: object = "",
    external_id: object = "",
    slug: object = "",
    principal_id: object = "",
) -> str:
    normalized_slug = str(slug or "").strip()
    if normalized_slug:
        hosted_url = _existing_hosted_property_tour_url({"slug": normalized_slug})
        if hosted_url:
            return hosted_url
    normalized_property_url = _normalized_property_tour_identity_url(property_url)
    normalized_source_ref = str(source_ref or "").strip()
    normalized_external_id = str(external_id or "").strip()
    normalized_principal = str(principal_id or "").strip()
    if not normalized_property_url and not normalized_source_ref and not normalized_external_id:
        return ""
    if not normalized_principal:
        return ""
    public_dir = _public_tour_dir()
    try:
        bundle_dirs = sorted(
            (path for path in public_dir.iterdir() if path.is_dir() and not path.name.startswith(".")),
            key=lambda path: path.name,
        )
    except Exception:
        return ""
    for bundle_dir in bundle_dirs:
        payload = _load_hosted_property_tour_payload(bundle_dir, principal_id=normalized_principal)
        payload_owner = str(payload.get("principal_id") or "").strip()
        if not payload_owner or not hmac.compare_digest(payload_owner, normalized_principal):
            continue
        payload_property_urls = {
            _normalized_property_tour_identity_url(payload.get("property_url")),
            _normalized_property_tour_identity_url(payload.get("listing_url")),
        }
        payload_property_urls.discard("")
        payload_source_ref = str(payload.get("source_ref") or "").strip()
        payload_external_id = str(payload.get("external_id") or "").strip()
        if normalized_property_url and normalized_property_url in payload_property_urls:
            hosted_url = _existing_hosted_property_tour_url({"slug": bundle_dir.name})
            if hosted_url:
                return hosted_url
        if normalized_source_ref and normalized_source_ref == payload_source_ref:
            hosted_url = _existing_hosted_property_tour_url({"slug": bundle_dir.name})
            if hosted_url:
                return hosted_url
        if normalized_external_id and normalized_external_id == payload_external_id:
            hosted_url = _existing_hosted_property_tour_url({"slug": bundle_dir.name})
            if hosted_url:
                return hosted_url
    return ""


def _existing_generated_reconstruction_tour_url_for_identity(
    *,
    property_url: object = "",
    source_ref: object = "",
    external_id: object = "",
    principal_id: object = "",
) -> str:
    normalized_property_url = _normalized_property_tour_identity_url(property_url)
    normalized_source_ref = str(source_ref or "").strip()
    normalized_external_id = str(external_id or "").strip()
    normalized_principal = str(principal_id or "").strip()
    if not normalized_property_url and not normalized_source_ref and not normalized_external_id:
        return ""
    if not normalized_principal:
        return ""
    public_dir = _public_tour_dir()
    try:
        bundle_dirs = sorted(
            (path for path in public_dir.iterdir() if path.is_dir() and not path.name.startswith(".")),
            key=lambda path: path.name,
        )
    except Exception:
        return ""
    for bundle_dir in bundle_dirs:
        payload = _load_hosted_property_tour_payload(bundle_dir, principal_id=normalized_principal)
        payload_owner = str(payload.get("principal_id") or "").strip()
        if not payload_owner or not hmac.compare_digest(payload_owner, normalized_principal):
            continue
        payload_property_urls = {
            _normalized_property_tour_identity_url(payload.get("property_url")),
            _normalized_property_tour_identity_url(payload.get("listing_url")),
        }
        payload_property_urls.discard("")
        payload_source_ref = str(payload.get("source_ref") or "").strip()
        payload_external_id = str(payload.get("external_id") or "").strip()
        matches_identity = False
        if normalized_property_url and normalized_property_url in payload_property_urls:
            matches_identity = True
        if normalized_source_ref and normalized_source_ref == payload_source_ref:
            matches_identity = True
        if normalized_external_id and normalized_external_id == payload_external_id:
            matches_identity = True
        if not matches_identity:
            continue
        canonical_url = f"{_hosted_property_tour_public_base_url()}/{bundle_dir.name}"
        if _hosted_property_tour_generated_reconstruction_bundle_ready(canonical_url):
            return canonical_url
    return ""

def _safe_live_property_tour_url(value: object) -> str:
    from .outbound_url_security import OutboundUrlRejected, validate_http_url

    normalized = str(value or "").strip()
    if not normalized:
        return ""
    try:
        validate_http_url(normalized)
    except OutboundUrlRejected:
        return ""
    return normalized

def _property_tour_provider_host_kind(value: object) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    try:
        parsed = urllib.parse.urlparse(normalized)
    except Exception:
        return ""
    if parsed.scheme.lower() not in {"http", "https"}:
        return ""
    host = str(parsed.hostname or "").strip().lower().rstrip(".")
    if host == "matterport.com" or host.endswith(".matterport.com"):
        return "matterport"
    if host == "3dvista.com" or host.endswith(".3dvista.com"):
        return "3dvista"
    return ""

def _prefer_hosted_live_360_embed(source_virtual_tour_url: object) -> bool:
    normalized = _safe_live_property_tour_url(source_virtual_tour_url)
    if not normalized:
        return False
    return bool(_property_tour_provider_host_kind(normalized))

def _hosted_property_tour_identity_secret() -> bytes:
    from app.settings import get_settings, resolve_signing_secret

    return resolve_signing_secret(
        get_settings(),
        purpose="property-tour-private-identity",
    ).encode("utf-8")


def _hosted_property_tour_slug(
    *,
    title: str,
    listing_id: str,
    property_url: str,
    variant_key: str,
    principal_id: str = "",
) -> str:
    seed = _first_non_empty_text(title, listing_id, property_url, "property tour")
    normalized = seed.encode("ascii", "ignore").decode("ascii").lower()
    base = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-") or "property-tour"
    variant = re.sub(r"[^a-z0-9]+", "-", str(variant_key or "layout_first").lower()).strip("-") or "layout-first"
    normalized_principal = str(principal_id or "").strip()
    identity_material = f"{property_url}|{listing_id}|{variant}".encode("utf-8")
    if normalized_principal:
        digest = hmac.new(
            _hosted_property_tour_identity_secret(),
            normalized_principal.encode("utf-8") + b"\x00" + identity_material,
            hashlib.sha256,
        ).hexdigest()[:20]
    else:
        # Legacy slugs remain computable for exact-owner migration-safe reuse.
        digest = hashlib.sha256(identity_material).hexdigest()[:10]
    return f"{base[:96].strip('-') or 'property-tour'}-{variant}-{digest}"


def _assert_hosted_property_tour_bundle_write_owner(bundle_dir: Path, *, principal_id: str) -> None:
    if not bundle_dir.exists():
        return
    requested_principal = str(principal_id or "").strip()
    private_payload = _owned_hosted_property_tour_private_receipt(
        bundle_dir,
        principal_id=requested_principal,
    )
    if not requested_principal or not private_payload:
        raise RuntimeError("hosted_property_tour_owner_mismatch")


def _existing_owned_hosted_property_tour_payload(
    *,
    slug: str,
    legacy_slug: str,
    principal_id: str,
) -> dict[str, object]:
    current_bundle_dir = _public_tour_dir() / slug
    if current_bundle_dir.exists():
        _assert_hosted_property_tour_bundle_write_owner(
            current_bundle_dir,
            principal_id=principal_id,
        )
    existing_payload = _existing_hosted_property_tour_payload(slug, principal_id=principal_id)
    if existing_payload:
        return existing_payload
    if legacy_slug and legacy_slug != slug:
        return _existing_hosted_property_tour_payload(legacy_slug, principal_id=principal_id)
    return {}

def _download_public_tour_asset_with_type(url: str, target: Path) -> str:
    from .outbound_url_security import open_guarded_url

    request = urllib.request.Request(str(url), headers={"User-Agent": _PROPERTY_SCOUT_USER_AGENT})
    content_type = ""
    total_bytes = 0
    max_bytes = _public_tour_asset_max_bytes()
    target.parent.mkdir(parents=True, exist_ok=True)
    with open_guarded_url(request, timeout=180) as response:
        content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if not _public_tour_asset_content_type_allowed(content_type):
            raise RuntimeError("tour_asset_content_type_unsupported")
        try:
            content_length = int(str(response.headers.get("Content-Length") or "0").strip() or "0")
        except Exception:
            content_length = 0
        if content_length > max_bytes:
            raise RuntimeError("tour_asset_too_large")
        with target.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise RuntimeError("tour_asset_too_large")
                handle.write(chunk)
    if total_bytes <= 0 or not target.exists():
        raise RuntimeError("tour_asset_empty")
    return content_type


def _download_public_tour_asset(url: str, target: Path) -> None:
    _download_public_tour_asset_with_type(url, target)

def _hosted_property_tour_asset_suffix(*, url: str, content_type: str) -> str:
    suffix = Path(urllib.parse.urlparse(str(url or "")).path).suffix.lower()
    normalized_type = str(content_type or "").split(";", 1)[0].strip().lower()
    if normalized_type in {"application/octet-stream", "binary/octet-stream"} and suffix:
        return suffix
    guessed = mimetypes.guess_extension(normalized_type)
    if guessed:
        return guessed
    return suffix or ".bin"

def _write_hosted_floorplan_property_tour_bundle(
    *,
    principal_id: str,
    title: str,
    listing_id: str,
    property_url: str,
    variant_key: str,
    floorplan_urls: list[str] | tuple[str, ...],
    property_facts_json: dict[str, object],
    source_host: str,
    source_ref: str = "",
    search_run_id: str = "",
    external_id: str = "",
    recipient_email: str = "",
) -> dict[str, object]:
    normalized_principal = str(principal_id or "").strip()
    if not normalized_principal:
        raise RuntimeError("hosted_property_tour_principal_required")
    normalized_urls = [
        _safe_live_property_tour_url(value)
        for value in list(floorplan_urls or [])
        if _safe_live_property_tour_url(value)
    ]
    if not normalized_urls:
        raise RuntimeError("floorplan_assets_missing")
    base_url = _hosted_property_tour_public_base_url()
    public_dir = _public_tour_dir()
    slug = _hosted_property_tour_slug(
        title=title,
        listing_id=listing_id,
        property_url=property_url,
        variant_key=variant_key,
        principal_id=normalized_principal,
    )
    legacy_slug = _hosted_property_tour_slug(
        title=title,
        listing_id=listing_id,
        property_url=property_url,
        variant_key=variant_key,
    )
    existing_payload = _existing_owned_hosted_property_tour_payload(
        slug=slug,
        legacy_slug=legacy_slug,
        principal_id=normalized_principal,
    )
    if existing_payload:
        return existing_payload
    bundle_dir = public_dir / slug
    staging_dir = public_dir / f".{slug}.tmp-{uuid4().hex}"
    staging_dir.mkdir(parents=True, exist_ok=True)
    scenes: list[dict[str, object]] = []
    try:
        for ordinal, asset_url in enumerate(normalized_urls[:12], start=1):
            try:
                suffix = _hosted_property_tour_asset_suffix(url=asset_url, content_type="")
                if suffix.lower() not in _PROPERTY_SCOUT_FLOORPLAN_ASSET_EXTENSIONS:
                    suffix = ".pdf"
                relpath = f"floorplan-{ordinal:02d}{suffix}"
                content_type = _download_public_tour_asset_with_type(asset_url, staging_dir / relpath)
                suffix = _hosted_property_tour_asset_suffix(url=asset_url, content_type=content_type)
                if suffix.lower() not in _PROPERTY_SCOUT_FLOORPLAN_ASSET_EXTENSIONS:
                    (staging_dir / relpath).unlink(missing_ok=True)
                    continue
                if suffix and not relpath.endswith(suffix):
                    corrected_relpath = f"floorplan-{ordinal:02d}{suffix}"
                    (staging_dir / relpath).rename(staging_dir / corrected_relpath)
                    relpath = corrected_relpath
                scenes.append(
                    {
                        "ordinal": ordinal,
                        "name": f"Floorplan {ordinal}",
                        "role": "floorplan",
                        "privacy_class": "floorplan_pdf_public" if relpath.lower().endswith(".pdf") else "public",
                        "asset_relpath": relpath,
                        "source_url": asset_url,
                        "property_url": property_url,
                        "mime_type": content_type or mimetypes.guess_type(relpath)[0] or "application/octet-stream",
                    }
                )
            except Exception:
                continue
        if not scenes:
            raise RuntimeError("floorplan_assets_unavailable")
        facts = dict(property_facts_json or {})
        existing_address_lines = [str(value or "").strip() for value in list(facts.get("address_lines") or []) if str(value or "").strip()]
        existing_teasers = [str(value or "").strip() for value in list(facts.get("teaser_attributes") or []) if str(value or "").strip()]
        facts.update(
            {
                "has_floorplan": True,
                "floorplan_count": max(int(facts.get("floorplan_count") or 0), len(scenes)),
                "floorplan_urls_json": normalized_urls,
                "tour_media_mode": "floorplan_hosted",
                "address_lines": existing_address_lines or ([source_host] if source_host else []),
                "teaser_attributes": existing_teasers or ["Hosted floorplan review", f"{len(scenes)} floorplan document(s)"],
            }
        )
        display_title = compact_text(title, fallback="Property Floorplan Tour", limit=180)
        payload = {
            "slug": slug,
            "hosted_url": f"{base_url}/{slug}",
            "public_url": f"{base_url}/{slug}",
            "principal_id": normalized_principal,
            "search_run_id": str(search_run_id or "").strip(),
            "listing_url": property_url,
            "property_url": property_url,
            "source_ref": str(source_ref or "").strip(),
            "external_id": str(external_id or "").strip(),
            "recipient_email": str(recipient_email or "").strip().lower(),
            "title": f"{display_title} - floorplan tour",
            "display_title": display_title,
            "tour_title": f"{display_title} - floorplan tour",
            "tour_id": None,
            "variant_key": variant_key,
            "variant_label": "floorplan",
            "scene_strategy": "floorplan_hosted",
            "scene_count": len(scenes),
            "facts": facts,
            "brief": {
                "theme_name": "clean_light",
                "tour_style": "hosted_floorplan_review",
                "audience": "property_screening",
                "creative_brief": "Render source floorplan documents directly inside the PropertyQuarry hosted tour page.",
                "call_to_action": "Review the floorplan.",
            },
            "editor_url": "",
            "crezlo_public_url": "",
            "scenes": scenes,
            "generated_at": _now_iso(),
            "creation_mode": "hosted_floorplan_tour",
        }
        if bundle_dir.exists():
            shutil.rmtree(bundle_dir)
        staging_dir.rename(bundle_dir)
        _write_hosted_property_tour_payload(bundle_dir, payload)
        return payload
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

def _write_hosted_photo_gallery_property_tour_bundle(
    *,
    principal_id: str,
    title: str,
    listing_id: str,
    property_url: str,
    variant_key: str,
    media_urls: list[str] | tuple[str, ...],
    property_facts_json: dict[str, object],
    source_host: str,
    source_ref: str = "",
    search_run_id: str = "",
    external_id: str = "",
    recipient_email: str = "",
) -> dict[str, object]:
    normalized_principal = str(principal_id or "").strip()
    if not normalized_principal:
        raise RuntimeError("hosted_property_tour_principal_required")
    normalized_urls = [
        _safe_live_property_tour_url(value)
        for value in list(media_urls or [])
        if _safe_live_property_tour_url(value)
    ]
    if not normalized_urls:
        raise RuntimeError("gallery_assets_missing")
    base_url = _hosted_property_tour_public_base_url()
    public_dir = _public_tour_dir()
    slug = _hosted_property_tour_slug(
        title=title,
        listing_id=listing_id,
        property_url=property_url,
        variant_key=variant_key,
        principal_id=normalized_principal,
    )
    legacy_slug = _hosted_property_tour_slug(
        title=title,
        listing_id=listing_id,
        property_url=property_url,
        variant_key=variant_key,
    )
    existing_payload = _existing_owned_hosted_property_tour_payload(
        slug=slug,
        legacy_slug=legacy_slug,
        principal_id=normalized_principal,
    )
    if existing_payload:
        return existing_payload
    bundle_dir = public_dir / slug
    staging_dir = public_dir / f".{slug}.tmp-{uuid4().hex}"
    staging_dir.mkdir(parents=True, exist_ok=True)
    scenes: list[dict[str, object]] = []
    try:
        for ordinal, asset_url in enumerate(normalized_urls[:12], start=1):
            try:
                suffix = _hosted_property_tour_asset_suffix(url=asset_url, content_type="")
                if suffix.lower() not in _PROPERTY_SCOUT_IMAGE_EXTENSIONS:
                    suffix = ".jpg"
                relpath = f"photo-{ordinal:02d}{suffix}"
                content_type = _download_public_tour_asset_with_type(asset_url, staging_dir / relpath)
                suffix = _hosted_property_tour_asset_suffix(url=asset_url, content_type=content_type)
                if suffix.lower() not in _PROPERTY_SCOUT_IMAGE_EXTENSIONS:
                    (staging_dir / relpath).unlink(missing_ok=True)
                    continue
                if suffix and not relpath.endswith(suffix):
                    corrected_relpath = f"photo-{ordinal:02d}{suffix}"
                    (staging_dir / relpath).rename(staging_dir / corrected_relpath)
                    relpath = corrected_relpath
                scenes.append(
                    {
                        "ordinal": ordinal,
                        "name": f"Photo {ordinal}",
                        "role": "photo",
                        "privacy_class": "public",
                        "asset_relpath": relpath,
                        "source_url": asset_url,
                        "property_url": property_url,
                        "mime_type": content_type or mimetypes.guess_type(relpath)[0] or "application/octet-stream",
                    }
                )
            except Exception:
                continue
        if not scenes:
            raise RuntimeError("gallery_assets_unavailable")
        facts = dict(property_facts_json or {})
        existing_address_lines = [str(value or "").strip() for value in list(facts.get("address_lines") or []) if str(value or "").strip()]
        existing_teasers = [str(value or "").strip() for value in list(facts.get("teaser_attributes") or []) if str(value or "").strip()]
        facts.update(
            {
                "tour_media_mode": "flat_images",
                "media_count": max(int(facts.get("media_count") or 0), len(normalized_urls), len(scenes)),
                "gallery_image_count": len(scenes),
                "media_urls_json": normalized_urls,
                "address_lines": existing_address_lines or ([source_host] if source_host else []),
                "teaser_attributes": existing_teasers or ["Hosted photo tour", f"{len(scenes)} listing photo(s)"],
            }
        )
        display_title = compact_text(title, fallback="Property Photo Tour", limit=180)
        payload = {
            "slug": slug,
            "hosted_url": f"{base_url}/{slug}",
            "public_url": f"{base_url}/{slug}",
            "principal_id": normalized_principal,
            "search_run_id": str(search_run_id or "").strip(),
            "listing_url": property_url,
            "property_url": property_url,
            "source_ref": str(source_ref or "").strip(),
            "external_id": str(external_id or "").strip(),
            "recipient_email": str(recipient_email or "").strip().lower(),
            "title": f"{display_title} - photo tour",
            "display_title": display_title,
            "tour_title": f"{display_title} - photo tour",
            "tour_id": None,
            "variant_key": variant_key,
            "variant_label": "gallery",
            "scene_strategy": "photo_gallery_hosted",
            "scene_count": len(scenes),
            "facts": facts,
            "brief": {
                "theme_name": "clean_light",
                "tour_style": "hosted_photo_gallery",
                "audience": "property_screening",
                "creative_brief": "Render listing photos directly inside the PropertyQuarry hosted tour page.",
                "call_to_action": "Review the listing photos.",
            },
            "editor_url": "",
            "crezlo_public_url": "",
            "scenes": scenes,
            "generated_at": _now_iso(),
            "creation_mode": "hosted_photo_gallery_tour",
        }
        if bundle_dir.exists():
            shutil.rmtree(bundle_dir)
        staging_dir.rename(bundle_dir)
        _write_hosted_property_tour_payload(bundle_dir, payload)
        return payload
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

def _write_hosted_feelestate_pure_360_property_tour_bundle(
    *,
    principal_id: str,
    title: str,
    listing_id: str,
    property_url: str,
    variant_key: str,
    source_virtual_tour_url: str,
    floorplan_urls: list[str] | tuple[str, ...] = (),
    property_facts_json: dict[str, object],
    source_host: str,
    source_ref: str = "",
    search_run_id: str = "",
    external_id: str = "",
    recipient_email: str = "",
) -> dict[str, object]:
    normalized_principal = str(principal_id or "").strip()
    if not normalized_principal:
        raise RuntimeError("hosted_property_tour_principal_required")
    live_url = _safe_live_property_tour_url(source_virtual_tour_url)
    parsed_live = urllib.parse.urlparse(live_url)
    live_host = str(parsed_live.hostname or "").strip().lower()
    live_provider = _property_tour_provider_host_kind(live_url)
    if live_provider in _CUSTOMER_FACING_TOUR_PROVIDERS:
        base_url = _hosted_property_tour_public_base_url()
        public_dir = _public_tour_dir()
        slug = _hosted_property_tour_slug(
            title=title,
            listing_id=listing_id,
            property_url=property_url,
            variant_key=variant_key,
            principal_id=normalized_principal,
        )
        legacy_slug = _hosted_property_tour_slug(
            title=title,
            listing_id=listing_id,
            property_url=property_url,
            variant_key=variant_key,
        )
        existing_payload = _existing_owned_hosted_property_tour_payload(
            slug=slug,
            legacy_slug=legacy_slug,
            principal_id=normalized_principal,
        )
        if existing_payload:
            return existing_payload
        bundle_dir = public_dir / slug
        bundle_dir.mkdir(parents=True, exist_ok=True)
        facts = dict(property_facts_json or {})
        existing_address_lines = [str(value or "").strip() for value in list(facts.get("address_lines") or []) if str(value or "").strip()]
        existing_teasers = [str(value or "").strip() for value in list(facts.get("teaser_attributes") or []) if str(value or "").strip()]
        facts.update(
            {
                "has_360": True,
                "tour_media_mode": "panorama_360",
                "source_virtual_tour_url": live_url,
                "panorama_source": live_host,
                "address_lines": existing_address_lines or ([source_host] if source_host else []),
                "teaser_attributes": existing_teasers or ["Live 360 tour", "Embedded external panorama"],
            }
        )
        display_title = compact_text(title, fallback="Live 360 Property Tour", limit=180)
        payload = {
            "slug": slug,
            "hosted_url": f"{base_url}/{slug}",
            "public_url": f"{base_url}/{slug}",
            "principal_id": normalized_principal,
            "search_run_id": str(search_run_id or "").strip(),
            "listing_url": property_url,
            "property_url": property_url,
            "source_ref": str(source_ref or "").strip(),
            "external_id": str(external_id or "").strip(),
            "recipient_email": str(recipient_email or "").strip().lower(),
            "source_virtual_tour_url": live_url,
            "source_virtual_tour_origin": live_url,
            "title": f"{display_title} - live 360",
            "display_title": display_title,
            "tour_title": f"{display_title} - live 360",
            "tour_id": None,
            "variant_key": variant_key,
            "variant_label": "3D tour",
            "scene_strategy": "live_360_embed",
            "control_mode": live_provider,
            "scene_count": 1,
            "facts": facts,
            "brief": {
                "theme_name": "Branded 3D tour",
                "tour_style": "embedded_3d_tour",
                "audience": "tenant_screening",
                "creative_brief": "Render the hosted 3D tour directly inside the PropertyQuarry tour page.",
                "call_to_action": "Open 3D tour.",
            },
            "editor_url": "",
            "crezlo_public_url": live_url,
            "three_d_vista_url": live_url,
            "scenes": [
                {
                    "ordinal": 1,
                    "name": "3D tour",
                    "role": "live_360",
                    "image_url": "",
                    "source_url": live_url,
                    "property_url": property_url,
                    "mime_type": "image/jpeg",
                }
            ],
            "generated_at": _now_iso(),
            "creation_mode": "embedded_live_360",
        }
        _write_hosted_property_tour_payload(bundle_dir, payload)
        return payload
    if "360.kalandra.at" not in live_host and "feelestate" not in live_host:
        raise RuntimeError("pure_360_source_unsupported")
    raise RuntimeError("property_tour_cube_fallback_disabled")

def _embedded_live_360_source_url(payload: dict[str, object]) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("source_virtual_tour_url", "source_virtual_tour_origin"):
        normalized = _safe_live_property_tour_url(str(payload.get(key) or "").strip())
        if normalized:
            return normalized
    return ""

def _hosted_property_tour_direct_360_url(tour_url: str) -> str:
    normalized_url = str(tour_url or "").strip()
    if not normalized_url:
        return ""
    parsed = urllib.parse.urlparse(normalized_url)
    path_parts = [part for part in str(parsed.path or "").split("/") if part]
    if len(path_parts) < 2 or path_parts[-2] != "tours":
        return ""
    slug = str(path_parts[-1] or "").strip()
    if not slug:
        return ""
    public_dir = _public_tour_dir()
    manifest_path = public_dir / slug / "tour.json"
    if not manifest_path.exists():
        return ""
    direct_url = _public_hosted_property_tour_live_source_url(public_dir / slug)
    if _property_tour_provider_host_kind(direct_url) not in _CUSTOMER_FACING_TOUR_PROVIDERS:
        return ""
    return direct_url

def _matterport_thumb_url(source_virtual_tour_url: str) -> str:
    normalized = str(source_virtual_tour_url or "").strip()
    if not normalized:
        return ""
    parsed = urllib.parse.urlparse(normalized)
    if _property_tour_provider_host_kind(normalized) != "matterport":
        return ""
    model_id = str(urllib.parse.parse_qs(parsed.query).get("m", [""])[0] or "").strip()
    if not model_id:
        return ""
    return f"https://my.matterport.com/api/v2/player/models/{model_id}/thumb/"

def _property_tour_generated_preview_url(value: object) -> bool:
    normalized = str(value or "").strip()
    if not normalized:
        return False
    path = urllib.parse.urlparse(normalized).path.lower()
    filename = Path(path).name
    return (
        filename.startswith("telegram-preview")
        or filename.startswith("diorama-preview")
    )

def _hosted_public_tour_asset_url(tour_url: str, *, slug: str, asset_relpath: str) -> str:
    normalized_url = str(tour_url or "").strip()
    safe_slug = str(slug or "").strip()
    safe_relpath = str(asset_relpath or "").strip().lstrip("/")
    if not normalized_url or not safe_slug or not safe_relpath:
        return ""
    parsed = urllib.parse.urlparse(normalized_url)
    if not parsed.scheme and not parsed.netloc and str(parsed.path or "").startswith("/tours/"):
        return f"/tours/files/{safe_slug}/{safe_relpath}"
    if not parsed.scheme or not parsed.netloc:
        return ""
    return urllib.parse.urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            f"/tours/files/{safe_slug}/{safe_relpath}",
            "",
            "",
            "",
        )
    )

def _hosted_property_tour_preview_image_url(tour_url: str) -> str:
    normalized_url = str(tour_url or "").strip()
    if not normalized_url:
        return ""
    parsed = urllib.parse.urlparse(normalized_url)
    path_parts = [part for part in str(parsed.path or "").split("/") if part]
    if len(path_parts) < 2 or path_parts[-2] != "tours":
        return ""
    slug = str(path_parts[-1] or "").strip()
    if not slug:
        return ""
    public_dir = _public_tour_dir()
    bundle_dir = public_dir / slug
    manifest_path = bundle_dir / "tour.json"
    if not manifest_path.exists():
        return ""
    payload = _load_hosted_property_tour_payload(bundle_dir)
    if not payload:
        return ""

    preview_relpaths: list[str] = []
    for key in ("diorama_preview_relpath", "preview_relpath"):
        value = str(payload.get(key) or "").strip().lstrip("/")
        if value:
            preview_relpaths.append(value)
    preview_relpaths.extend(("diorama-preview.png", "diorama-preview.jpg", "diorama-preview.jpeg", "diorama-preview.webp"))
    for relpath in preview_relpaths:
        asset_path = (bundle_dir / relpath).resolve()
        if bundle_dir.resolve() in asset_path.parents and asset_path.exists() and asset_path.is_file():
            return _hosted_public_tour_asset_url(normalized_url, slug=slug, asset_relpath=relpath)

    for image_url in _hosted_property_tour_generated_reconstruction_asset_urls(normalized_url):
        return image_url
    floorplan_url = _hosted_property_tour_generated_reconstruction_asset_url(
        normalized_url,
        asset_key="floorplan_relpath",
    )
    if floorplan_url:
        return floorplan_url

    role_priority = {
        "diorama": 0,
        "generated_overview": 1,
        "overview": 2,
        "floorplan": 3,
        "panorama_360": 4,
    }
    scenes = list(payload.get("scenes") or []) if isinstance(payload.get("scenes"), list) else []
    ranked_scenes = sorted(
        (scene for scene in scenes if isinstance(scene, dict)),
        key=lambda scene: (
            role_priority.get(str(scene.get("role") or "").strip().lower(), 10),
            int(scene.get("ordinal") or 9999),
        ),
    )
    for scene in ranked_scenes:
        image_url = _safe_live_property_tour_url(str(scene.get("image_url") or "").strip())
        if image_url and image_url.lower().split("?", 1)[0].endswith((".jpg", ".jpeg", ".png", ".webp")):
            return image_url
        asset_relpath = str(scene.get("asset_relpath") or "").strip()
        if asset_relpath and asset_relpath.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            asset_path = (bundle_dir / asset_relpath).resolve()
            if bundle_dir.resolve() in asset_path.parents and asset_path.exists() and asset_path.is_file():
                return _hosted_public_tour_asset_url(normalized_url, slug=slug, asset_relpath=asset_relpath)
    return ""


_GOVERNED_SPATIAL_INPUT_CONTRACT = "propertyquarry.governed_spatial_tour_input.v1"
_GOVERNED_SPATIAL_INPUT_VERSION = "1.0.0"
_GOVERNED_SPATIAL_INPUT_VERSION_1_1 = "1.1.0"
_GOVERNED_SPATIAL_POLICY_CONTRACT = "propertyquarry.governed_spatial_retention_policy.v1"
_GOVERNED_SPATIAL_LIFECYCLE_CONTRACT = "propertyquarry.governed_spatial_lifecycle.v1"
_GOVERNED_SPATIAL_INDEX_CONTRACT = "propertyquarry.governed_spatial_lifecycle_index.v1"
_GOVERNED_SPATIAL_LEGAL_HOLD_CONTRACT = "propertyquarry.governed_spatial_legal_hold.v1"
_GOVERNED_SPATIAL_MAX_RETENTION_DAYS = 3650
_GOVERNED_SPATIAL_MAX_RAW_BYTES = 2 * 1024 * 1024
_GOVERNED_SPATIAL_SAFE_INTEGER = 9_007_199_254_740_991
_GOVERNED_SPATIAL_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_GOVERNED_SPATIAL_SEMVER_RE = re.compile(r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")
_GOVERNED_SPATIAL_ROOM_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_GOVERNED_SPATIAL_INPUT_FIELDS = frozenset(
    {
        "contract_name",
        "contract_version",
        "source_owner_ref",
        "source_authority_ref",
        "source_authority_receipt_digest",
        "source_packet_ref",
        "source_digest",
        "tenant_ref",
        "subject_ref",
        "purpose",
        "locale",
        "privacy_policy_ref",
        "privacy_policy_version",
        "rights_authorization_ref",
        "consent_authorization_ref",
        "publication_authorization_ref",
        "truth_refs",
        "evidence_refs",
        "normalized_floorplan_ref",
        "room_graph_ref",
        "walkable_mesh_ref",
        "portal_graph_ref",
        "scale_m_per_unit",
        "orientation_degrees",
        "source_retrieved_at",
        "license_provenance_refs",
        "source_media_assignments",
        "inaccessible_rooms",
        "route_exclusions",
        "rooms",
        "portals",
        "route_room_ids",
    }
)
_GOVERNED_SPATIAL_INPUT_FIELDS_1_1 = frozenset(
    (_GOVERNED_SPATIAL_INPUT_FIELDS - {"route_room_ids"})
    | {"route_priority_room_ids", "route_start_room_id"}
)
_GOVERNED_SPATIAL_ROOM_FIELDS = frozenset(
    {
        "room_id",
        "room_type",
        "walkable",
        "boundary_ref",
        "ceiling_height_m",
        "geometry_anchor_ref",
        "texture_anchor_refs",
        "exterior_classification",
        "accessible",
    }
)
_GOVERNED_SPATIAL_PORTAL_FIELDS = frozenset(
    {"portal_id", "from_room_id", "to_room_id", "walkable"}
)
_GOVERNED_SPATIAL_INACCESSIBLE_FIELDS = frozenset({"room_id", "reason", "provenance_ref"})
_GOVERNED_SPATIAL_MEDIA_FIELDS = frozenset(
    {"media_ref", "room_id", "geometry_anchor_ref", "license_provenance_ref", "captured_at"}
)
_GOVERNED_SPATIAL_FORBIDDEN_KEY_PARTS = frozenset(
    {
        "account",
        "admin",
        "api_key",
        "authorization_header",
        "combat",
        "credential",
        "damage",
        "dice",
        "effect_resolution",
        "email",
        "exact_address",
        "initiative",
        "password",
        "private_url",
        "provider",
        "rules_result",
        "secret",
        "session",
        "tactical",
        "token",
        "vendor",
        "vtt",
    }
)


class GovernedPropertyTourContractError(ValueError):
    pass


class GovernedPropertyTourIntegrityError(ValueError):
    pass


def _governed_spatial_compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _governed_spatial_digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_governed_spatial_compact_json(value).encode("utf-8")).hexdigest()


def _governed_spatial_observed(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise GovernedPropertyTourContractError("observed_at_timezone_required")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _governed_spatial_timestamp(value: object, *, field: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise GovernedPropertyTourContractError(f"{field}_required")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GovernedPropertyTourContractError(f"{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GovernedPropertyTourContractError(f"{field}_timezone_required")
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def _governed_spatial_iso(value: datetime) -> str:
    return _governed_spatial_observed(value).isoformat().replace("+00:00", "Z")


def _governed_spatial_digest_value(value: object, *, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _GOVERNED_SPATIAL_DIGEST_RE.fullmatch(normalized):
        raise GovernedPropertyTourContractError(f"{field}_sha256_required")
    return normalized


def _governed_spatial_ref(value: object) -> str:
    normalized = str(value or "").strip()
    lowered = normalized.lower()
    if (
        not normalized
        or any(character.isspace() for character in normalized)
        or "://" in normalized
        or "@" in normalized
        or len(normalized) > 256
        or lowered.startswith(("provider:", "vendor:"))
        or ":provider:" in lowered
        or ":vendor:" in lowered
    ):
        raise GovernedPropertyTourContractError("governed_spatial_ref_invalid")
    return normalized


def _governed_spatial_exact_fields(
    value: object,
    *,
    allowed: frozenset[str],
    required: frozenset[str] | None = None,
    path: str,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise GovernedPropertyTourContractError(f"{path}_object_required")
    payload = dict(value)
    unknown = sorted(set(payload).difference(allowed))
    if unknown:
        raise GovernedPropertyTourContractError(f"{path}_unknown_field:{unknown[0]}")
    missing = sorted((required if required is not None else allowed).difference(payload))
    if missing:
        raise GovernedPropertyTourContractError(f"{path}_missing_field:{missing[0]}")
    return payload


def _governed_spatial_reject_unsafe_shape(value: object, *, path: str = "property_packet") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).strip().lower()
            if any(marker in normalized for marker in _GOVERNED_SPATIAL_FORBIDDEN_KEY_PARTS):
                raise GovernedPropertyTourContractError(f"unsafe_field:{path}.{key}")
            _governed_spatial_reject_unsafe_shape(nested, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _governed_spatial_reject_unsafe_shape(nested, path=f"{path}[{index}]")
        return
    if isinstance(value, str):
        lowered = value.strip().lower()
        if "://" in lowered or lowered.startswith("www.") or re.search(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise GovernedPropertyTourContractError(f"unsafe_value:{path}")


def _governed_spatial_valid_unicode(value: object, *, path: str = "$" ) -> None:
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise GovernedPropertyTourContractError("invalid_unicode")
    elif isinstance(value, Mapping):
        for key, nested in value.items():
            _governed_spatial_valid_unicode(str(key), path=path)
            _governed_spatial_valid_unicode(nested, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _governed_spatial_valid_unicode(nested, path=f"{path}[{index}]")


def parse_governed_property_tour_raw_json(raw: bytes | bytearray | memoryview | str) -> dict[str, object]:
    if isinstance(raw, str):
        encoded = raw.encode("utf-8")
    elif isinstance(raw, (bytes, bytearray, memoryview)):
        encoded = bytes(raw)
    else:
        raise GovernedPropertyTourContractError("raw_json_bytes_required")
    if not encoded or len(encoded) > _GOVERNED_SPATIAL_MAX_RAW_BYTES:
        raise GovernedPropertyTourContractError("raw_json_empty_or_too_large")
    if encoded.startswith(b"\xef\xbb\xbf"):
        raise GovernedPropertyTourContractError("bom_forbidden")
    try:
        text = encoded.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise GovernedPropertyTourContractError("invalid_utf8") from exc

    def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, nested in pairs:
            if key in result:
                raise GovernedPropertyTourContractError("duplicate_member")
            result[key] = nested
        return result

    def safe_integer(token: str) -> int:
        parsed = int(token)
        if abs(parsed) > _GOVERNED_SPATIAL_SAFE_INTEGER:
            raise GovernedPropertyTourContractError("unsafe_integer")
        return parsed

    def finite_number(token: str) -> float:
        parsed = float(token)
        if not (-float("inf") < parsed < float("inf")):
            raise GovernedPropertyTourContractError("non_finite_number")
        return parsed

    def reject_constant(_token: str) -> object:
        raise GovernedPropertyTourContractError("non_finite_number")

    try:
        payload = json.loads(
            text,
            object_pairs_hook=unique_pairs,
            parse_int=safe_integer,
            parse_float=finite_number,
            parse_constant=reject_constant,
        )
    except GovernedPropertyTourContractError:
        raise
    except (json.JSONDecodeError, ValueError) as exc:
        raise GovernedPropertyTourContractError("malformed_json") from exc
    if not isinstance(payload, dict):
        raise GovernedPropertyTourContractError("root_object_required")
    _governed_spatial_valid_unicode(payload)
    return payload


def _governed_spatial_finite_number(value: object, *, field: str, minimum: float, maximum: float) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GovernedPropertyTourContractError(f"{field}_finite_number_required")
    rendered = float(value)
    if not (-float("inf") < rendered < float("inf")) or not minimum <= rendered <= maximum:
        raise GovernedPropertyTourContractError(f"{field}_out_of_range")
    return value


def _governed_spatial_unique_refs(value: object, *, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise GovernedPropertyTourContractError(f"{field}_nonempty_list_required")
    refs = [_governed_spatial_ref(item) for item in value]
    if len(refs) != len(set(refs)):
        raise GovernedPropertyTourContractError(f"{field}_unique_required")
    return refs


@dataclass(frozen=True, slots=True)
class VerifiedPropertyTourSourceAuthority:
    owner_ref: str
    authority_ref: str
    authority_receipt_digest: str
    source_packet_ref: str
    source_digest: str
    tenant_ref: str
    subject_ref: str
    rights_authorization_ref: str
    consent_authorization_ref: str
    publication_authorization_ref: str
    privacy_policy_ref: str
    privacy_policy_version: str
    issued_at: str
    expires_at: str

    def validate_binding(self, packet: Mapping[str, object], *, observed_at: datetime) -> None:
        observed = _governed_spatial_observed(observed_at)
        issued = _governed_spatial_timestamp(self.issued_at, field="source_authority_issued_at")
        expires = _governed_spatial_timestamp(self.expires_at, field="source_authority_expires_at")
        if issued >= expires or not issued <= observed <= expires:
            raise GovernedPropertyTourContractError("source_authority_not_current")
        bindings = {
            "source_owner_ref": self.owner_ref,
            "source_authority_ref": self.authority_ref,
            "source_authority_receipt_digest": self.authority_receipt_digest,
            "source_packet_ref": self.source_packet_ref,
            "source_digest": self.source_digest,
            "tenant_ref": self.tenant_ref,
            "subject_ref": self.subject_ref,
            "rights_authorization_ref": self.rights_authorization_ref,
            "consent_authorization_ref": self.consent_authorization_ref,
            "publication_authorization_ref": self.publication_authorization_ref,
            "privacy_policy_ref": self.privacy_policy_ref,
            "privacy_policy_version": self.privacy_policy_version,
        }
        for field, expected in bindings.items():
            if packet.get(field) != expected:
                raise GovernedPropertyTourContractError(f"source_authority_binding_mismatch:{field}")
        _governed_spatial_digest_value(self.authority_receipt_digest, field="source_authority_receipt_digest")


@dataclass(frozen=True, slots=True)
class VerifiedGovernedPropertyTourPublication:
    tour_scope_digest: str
    composition_digest: str
    composition_receipt_digest: str
    artifact_digest: str
    artifact_receipt_digest: str
    quality_receipt_digest: str
    rights_provenance_digest: str
    capability_receipt_digest: str
    publication_authorization_digest: str
    privacy_policy_digest: str
    issued_at: str
    expires_at: str
    decision_digest: str

    def material(self) -> dict[str, object]:
        return {
            field: getattr(self, field)
            for field in (
                "tour_scope_digest",
                "composition_digest",
                "composition_receipt_digest",
                "artifact_digest",
                "artifact_receipt_digest",
                "quality_receipt_digest",
                "rights_provenance_digest",
                "capability_receipt_digest",
                "publication_authorization_digest",
                "privacy_policy_digest",
                "issued_at",
                "expires_at",
            )
        }

    def validate_binding(
        self,
        *,
        tour_scope_digest: str,
        composition_digest: str,
        privacy_policy_digest: str,
        observed_at: datetime,
    ) -> None:
        for field in self.material():
            if field.endswith("_digest"):
                _governed_spatial_digest_value(getattr(self, field), field=field)
        _governed_spatial_digest_value(self.decision_digest, field="decision_digest")
        if self.decision_digest != _governed_spatial_digest(self.material()):
            raise GovernedPropertyTourContractError("publication_decision_digest_invalid")
        issued = _governed_spatial_timestamp(self.issued_at, field="publication_issued_at")
        expires = _governed_spatial_timestamp(self.expires_at, field="publication_expires_at")
        observed = _governed_spatial_observed(observed_at)
        if issued >= expires or not issued <= observed <= expires:
            raise GovernedPropertyTourContractError("publication_authority_not_current")
        if self.tour_scope_digest != tour_scope_digest:
            raise GovernedPropertyTourContractError("publication_scope_binding_mismatch")
        if self.composition_digest != composition_digest:
            raise GovernedPropertyTourContractError("publication_composition_binding_mismatch")
        if self.privacy_policy_digest != privacy_policy_digest:
            raise GovernedPropertyTourContractError("publication_privacy_binding_mismatch")


@dataclass(frozen=True, slots=True)
class GovernedPropertyTourRetentionPolicy:
    policy_id: str
    policy_digest: str
    approval_ref: str
    approved_at: datetime
    expires_at: datetime
    source_retention_days: int
    receipt_retention_days: int
    tombstone_retention_days: int

    def as_payload(self) -> dict[str, object]:
        return {
            "contract_name": _GOVERNED_SPATIAL_POLICY_CONTRACT,
            "policy_id": self.policy_id,
            "approval_ref": self.approval_ref,
            "approved_at": _governed_spatial_iso(self.approved_at),
            "expires_at": _governed_spatial_iso(self.expires_at),
            "source_retention_days": self.source_retention_days,
            "receipt_retention_days": self.receipt_retention_days,
            "tombstone_retention_days": self.tombstone_retention_days,
            "policy_digest": self.policy_digest,
        }

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, object],
        *,
        observed_at: datetime,
    ) -> "GovernedPropertyTourRetentionPolicy":
        required = frozenset(
            {
                "contract_name",
                "policy_id",
                "approval_ref",
                "approved_at",
                "expires_at",
                "source_retention_days",
                "receipt_retention_days",
                "tombstone_retention_days",
                "policy_digest",
            }
        )
        policy_payload = _governed_spatial_exact_fields(
            payload,
            allowed=required,
            path="retention_policy",
        )
        if policy_payload["contract_name"] != _GOVERNED_SPATIAL_POLICY_CONTRACT:
            raise GovernedPropertyTourContractError("retention_policy_contract_invalid")
        policy_id = _governed_spatial_ref(policy_payload["policy_id"])
        approval_ref = _governed_spatial_ref(policy_payload["approval_ref"])
        approved_at = _governed_spatial_timestamp(policy_payload["approved_at"], field="approved_at")
        expires_at = _governed_spatial_timestamp(policy_payload["expires_at"], field="expires_at")
        now = _governed_spatial_observed(observed_at)
        if approved_at > now or expires_at <= now or expires_at <= approved_at:
            raise GovernedPropertyTourContractError("retention_policy_chronology_invalid")
        values: dict[str, int] = {}
        for field in ("source_retention_days", "receipt_retention_days", "tombstone_retention_days"):
            raw_value = policy_payload[field]
            if type(raw_value) is not int or not 1 <= raw_value <= _GOVERNED_SPATIAL_MAX_RETENTION_DAYS:
                raise GovernedPropertyTourContractError(f"retention_policy_{field}_invalid")
            values[field] = raw_value
        material = {
            "contract_name": _GOVERNED_SPATIAL_POLICY_CONTRACT,
            "policy_id": policy_id,
            "approval_ref": approval_ref,
            "approved_at": _governed_spatial_iso(approved_at),
            "expires_at": _governed_spatial_iso(expires_at),
            **values,
        }
        policy_digest = _governed_spatial_digest_value(policy_payload["policy_digest"], field="policy_digest")
        if policy_digest != _governed_spatial_digest(material):
            raise GovernedPropertyTourContractError("retention_policy_digest_invalid")
        return cls(
            policy_id=policy_id,
            policy_digest=policy_digest,
            approval_ref=approval_ref,
            approved_at=approved_at,
            expires_at=expires_at,
            **values,
        )


def _governed_property_route_plan(
    *,
    priority_room_ids: list[str],
    start_room_id: str,
    portals: list[dict[str, object]],
) -> list[str]:
    priority_rank = {room_id: index for index, room_id in enumerate(priority_room_ids)}
    adjacency: dict[str, set[str]] = {room_id: set() for room_id in priority_room_ids}
    for portal in portals:
        left = str(portal["from_room_id"])
        right = str(portal["to_room_id"])
        if left not in adjacency or right not in adjacency:
            continue
        adjacency[left].add(right)
        adjacency[right].add(left)
    ordered_adjacency = {
        room_id: sorted(neighbors, key=lambda neighbor: (priority_rank[neighbor], neighbor))
        for room_id, neighbors in adjacency.items()
    }

    visited = {start_room_id}
    route = [start_room_id]
    if len(priority_room_ids) == 1:
        return route

    stack: list[tuple[str, int]] = [(start_room_id, 0)]
    while stack and len(visited) < len(priority_room_ids):
        room_id, neighbor_index = stack[-1]
        neighbors = ordered_adjacency[room_id]
        if neighbor_index >= len(neighbors):
            stack.pop()
            if stack:
                route.append(stack[-1][0])
            continue
        neighbor = neighbors[neighbor_index]
        stack[-1] = (room_id, neighbor_index + 1)
        if neighbor in visited:
            continue
        visited.add(neighbor)
        route.append(neighbor)
        if len(visited) == len(priority_room_ids):
            break
        stack.append((neighbor, 0))

    if visited != set(priority_room_ids):
        raise GovernedPropertyTourContractError("property_walkable_graph_disconnected")
    if len(route) > 2 * len(priority_room_ids) - 1:
        raise GovernedPropertyTourContractError("property_route_visit_count_exceeds_2n_minus_1")
    if any(left == right for left, right in zip(route, route[1:])):
        raise GovernedPropertyTourContractError("property_route_consecutive_duplicate")
    return route


def _governed_property_packet(
    property_packet: Mapping[str, object],
    *,
    verified_source_authority: VerifiedPropertyTourSourceAuthority,
    observed_at: datetime,
) -> tuple[dict[str, object], list[str], list[dict[str, object]], list[dict[str, object]]]:
    if not isinstance(property_packet, Mapping):
        raise GovernedPropertyTourContractError("property_packet_object_required")
    contract_version = property_packet.get("contract_version")
    if contract_version == _GOVERNED_SPATIAL_INPUT_VERSION:
        allowed_fields = _GOVERNED_SPATIAL_INPUT_FIELDS
    elif contract_version == _GOVERNED_SPATIAL_INPUT_VERSION_1_1:
        allowed_fields = _GOVERNED_SPATIAL_INPUT_FIELDS_1_1
    else:
        raise GovernedPropertyTourContractError("property_packet_version_unsupported")
    packet = _governed_spatial_exact_fields(
        property_packet,
        allowed=allowed_fields,
        path="property_packet",
    )
    _governed_spatial_reject_unsafe_shape(packet)
    if packet["contract_name"] != _GOVERNED_SPATIAL_INPUT_CONTRACT:
        raise GovernedPropertyTourContractError("property_packet_contract_invalid")
    if packet["purpose"] != "walkthrough":
        raise GovernedPropertyTourContractError("property_packet_purpose_invalid")
    if not isinstance(packet["locale"], str) or not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?", packet["locale"]):
        raise GovernedPropertyTourContractError("property_packet_locale_invalid")
    if not isinstance(packet["privacy_policy_version"], str) or not _GOVERNED_SPATIAL_SEMVER_RE.fullmatch(
        packet["privacy_policy_version"]
    ):
        raise GovernedPropertyTourContractError("property_packet_privacy_policy_version_invalid")
    source_digest = str(packet["source_digest"] or "").strip().lower().removeprefix("sha256:")
    if not re.fullmatch(r"[a-f0-9]{64}", source_digest):
        raise GovernedPropertyTourContractError("property_packet_source_digest_invalid")
    packet["source_digest"] = source_digest
    for field in (
        "source_owner_ref",
        "source_authority_ref",
        "source_packet_ref",
        "tenant_ref",
        "subject_ref",
        "privacy_policy_ref",
        "rights_authorization_ref",
        "consent_authorization_ref",
        "publication_authorization_ref",
        "normalized_floorplan_ref",
        "room_graph_ref",
        "walkable_mesh_ref",
        "portal_graph_ref",
    ):
        packet[field] = _governed_spatial_ref(packet[field])
    packet["source_authority_receipt_digest"] = _governed_spatial_digest_value(
        packet["source_authority_receipt_digest"], field="source_authority_receipt_digest"
    )
    _governed_spatial_unique_refs(packet["truth_refs"], field="truth_refs")
    _governed_spatial_unique_refs(packet["evidence_refs"], field="evidence_refs")
    _governed_spatial_unique_refs(packet["license_provenance_refs"], field="license_provenance_refs")
    _governed_spatial_finite_number(packet["scale_m_per_unit"], field="scale_m_per_unit", minimum=0.0001, maximum=1000)
    _governed_spatial_finite_number(
        packet["orientation_degrees"], field="orientation_degrees", minimum=-360, maximum=360
    )
    retrieved = _governed_spatial_timestamp(packet["source_retrieved_at"], field="source_retrieved_at")
    if retrieved > _governed_spatial_observed(observed_at):
        raise GovernedPropertyTourContractError("source_retrieved_at_future")
    verified_source_authority.validate_binding(packet, observed_at=observed_at)
    if packet["route_exclusions"] != []:
        raise GovernedPropertyTourContractError("property_route_exclusions_forbidden")

    raw_rooms = packet["rooms"]
    if not isinstance(raw_rooms, list) or not raw_rooms:
        raise GovernedPropertyTourContractError("property_packet_rooms_required")
    rooms: list[dict[str, object]] = []
    room_ids: set[str] = set()
    walkable_room_ids: list[str] = []
    nonwalkable_room_ids: set[str] = set()
    for raw_room in raw_rooms:
        room = _governed_spatial_exact_fields(
            raw_room,
            allowed=_GOVERNED_SPATIAL_ROOM_FIELDS,
            required=frozenset(
                {
                    "room_id",
                    "room_type",
                    "walkable",
                    "boundary_ref",
                    "ceiling_height_m",
                    "geometry_anchor_ref",
                    "texture_anchor_refs",
                    "accessible",
                }
            ),
            path="property_packet.rooms",
        )
        room_id = _governed_spatial_ref(room["room_id"])
        if room_id in room_ids:
            raise GovernedPropertyTourContractError("property_room_ids_unique_required")
        room_ids.add(room_id)
        room["room_id"] = room_id
        room["room_type"] = _governed_spatial_ref(room["room_type"])
        room["boundary_ref"] = _governed_spatial_ref(room["boundary_ref"])
        room["geometry_anchor_ref"] = _governed_spatial_ref(room["geometry_anchor_ref"])
        room["texture_anchor_refs"] = _governed_spatial_unique_refs(
            room["texture_anchor_refs"], field="texture_anchor_refs"
        )
        _governed_spatial_finite_number(
            room["ceiling_height_m"], field="ceiling_height_m", minimum=1.5, maximum=20
        )
        if type(room["walkable"]) is not bool or type(room["accessible"]) is not bool:
            raise GovernedPropertyTourContractError("property_room_classification_boolean_required")
        if room["walkable"] is True:
            if room["accessible"] is not True:
                raise GovernedPropertyTourContractError("walkable_room_must_be_accessible")
            walkable_room_ids.append(room_id)
        else:
            if room["accessible"] is not False:
                raise GovernedPropertyTourContractError("nonwalkable_room_must_be_inaccessible")
            nonwalkable_room_ids.add(room_id)
        rooms.append(room)
    if not walkable_room_ids:
        raise GovernedPropertyTourContractError("property_packet_walkable_rooms_required")

    raw_inaccessible = packet["inaccessible_rooms"]
    if not isinstance(raw_inaccessible, list):
        raise GovernedPropertyTourContractError("inaccessible_rooms_list_required")
    inaccessible: list[dict[str, object]] = []
    inaccessible_ids: set[str] = set()
    for raw_row in raw_inaccessible:
        row = _governed_spatial_exact_fields(
            raw_row,
            allowed=_GOVERNED_SPATIAL_INACCESSIBLE_FIELDS,
            path="property_packet.inaccessible_rooms",
        )
        row = {field: _governed_spatial_ref(row[field]) for field in _GOVERNED_SPATIAL_INACCESSIBLE_FIELDS}
        if row["room_id"] in inaccessible_ids:
            raise GovernedPropertyTourContractError("inaccessible_room_ids_unique_required")
        inaccessible_ids.add(str(row["room_id"]))
        inaccessible.append(row)
    if inaccessible_ids != nonwalkable_room_ids:
        raise GovernedPropertyTourContractError("inaccessible_rooms_must_equal_nonwalkable_source_set")

    raw_portals = packet["portals"]
    if not isinstance(raw_portals, list):
        raise GovernedPropertyTourContractError("property_portals_list_required")
    portals: list[dict[str, object]] = []
    portal_ids: set[str] = set()
    portal_pairs: set[tuple[str, str]] = set()
    walkable_room_set = set(walkable_room_ids)
    for raw_portal in raw_portals:
        portal = _governed_spatial_exact_fields(
            raw_portal,
            allowed=_GOVERNED_SPATIAL_PORTAL_FIELDS,
            path="property_packet.portals",
        )
        portal_id = _governed_spatial_ref(portal["portal_id"])
        left = _governed_spatial_ref(portal["from_room_id"])
        right = _governed_spatial_ref(portal["to_room_id"])
        if portal_id in portal_ids or left == right or left not in room_ids or right not in room_ids:
            raise GovernedPropertyTourContractError("property_portal_truth_invalid")
        if portal["walkable"] is not True:
            raise GovernedPropertyTourContractError("property_portal_walkable_truth_required")
        portal_ids.add(portal_id)
        portal_pairs.add((left, right))
        portals.append(
            {"portal_id": portal_id, "from_room_id": left, "to_room_id": right, "walkable": True}
        )

    if contract_version == _GOVERNED_SPATIAL_INPUT_VERSION:
        raw_route = packet["route_room_ids"]
        if not isinstance(raw_route, list):
            raise GovernedPropertyTourContractError("property_route_room_ids_list_required")
        route_room_ids = [_governed_spatial_ref(room_id) for room_id in raw_route]
        if len(route_room_ids) != len(set(route_room_ids)):
            raise GovernedPropertyTourContractError("property_route_room_ids_unique_required")
        if set(route_room_ids) != walkable_room_set:
            raise GovernedPropertyTourContractError("property_route_must_equal_full_walkable_room_set")
        for left, right in zip(route_room_ids, route_room_ids[1:]):
            if (left, right) not in portal_pairs:
                raise GovernedPropertyTourContractError("property_route_portal_truth_mismatch")
    else:
        raw_priority = packet["route_priority_room_ids"]
        if not isinstance(raw_priority, list) or not raw_priority:
            raise GovernedPropertyTourContractError("property_route_priority_room_ids_nonempty_list_required")
        priority_room_ids = [_governed_spatial_ref(room_id) for room_id in raw_priority]
        if any(not _GOVERNED_SPATIAL_ROOM_TOKEN_RE.fullmatch(room_id) for room_id in priority_room_ids):
            raise GovernedPropertyTourContractError("property_route_priority_room_token_required")
        if len(priority_room_ids) != len(set(priority_room_ids)):
            raise GovernedPropertyTourContractError("property_route_priority_room_ids_unique_required")
        if set(priority_room_ids) != walkable_room_set:
            raise GovernedPropertyTourContractError("property_route_priority_must_equal_walkable_room_set")
        start_room_id = _governed_spatial_ref(packet["route_start_room_id"])
        if start_room_id != priority_room_ids[0]:
            raise GovernedPropertyTourContractError("property_route_start_must_equal_first_priority_room")
        packet["route_priority_room_ids"] = priority_room_ids
        packet["route_start_room_id"] = start_room_id
        route_room_ids = _governed_property_route_plan(
            priority_room_ids=priority_room_ids,
            start_room_id=start_room_id,
            portals=portals,
        )
        walkable_room_ids = priority_room_ids
        rooms = sorted(rooms, key=lambda room: str(room["room_id"]))
        portals = sorted(
            (
                {
                    **portal,
                    "from_room_id": min(str(portal["from_room_id"]), str(portal["to_room_id"])),
                    "to_room_id": max(str(portal["from_room_id"]), str(portal["to_room_id"])),
                }
                for portal in portals
            ),
            key=lambda portal: (
                str(portal["portal_id"]),
                str(portal["from_room_id"]),
                str(portal["to_room_id"]),
            ),
        )

    raw_media = packet["source_media_assignments"]
    if not isinstance(raw_media, list):
        raise GovernedPropertyTourContractError("source_media_assignments_list_required")
    media: list[dict[str, object]] = []
    for raw_assignment in raw_media:
        assignment = _governed_spatial_exact_fields(
            raw_assignment,
            allowed=_GOVERNED_SPATIAL_MEDIA_FIELDS,
            path="property_packet.source_media_assignments",
        )
        normalized_assignment = {
            field: _governed_spatial_ref(assignment[field])
            for field in _GOVERNED_SPATIAL_MEDIA_FIELDS
            if field != "captured_at"
        }
        captured = _governed_spatial_timestamp(assignment["captured_at"], field="captured_at")
        if captured > _governed_spatial_observed(observed_at):
            raise GovernedPropertyTourContractError("source_media_captured_at_future")
        normalized_assignment["captured_at"] = _governed_spatial_iso(captured)
        if normalized_assignment["room_id"] not in room_ids:
            raise GovernedPropertyTourContractError("source_media_room_binding_invalid")
        media.append(normalized_assignment)
    packet["source_media_assignments"] = media
    packet["inaccessible_rooms"] = inaccessible
    packet["rooms"] = rooms
    packet["portals"] = portals
    packet["route_room_ids"] = route_room_ids
    return packet, walkable_room_ids, portals, rooms


def build_governed_property_tour_request(
    *,
    property_packet: Mapping[str, object],
    request_id: str,
    idempotency_key: str,
    style_pack_id: str,
    product_event_ref: str,
    verified_source_authority: VerifiedPropertyTourSourceAuthority,
    observed_at: datetime,
) -> dict[str, object]:
    packet, walkable_room_ids, portals, rooms = _governed_property_packet(
        property_packet,
        verified_source_authority=verified_source_authority,
        observed_at=observed_at,
    )
    source_packet_ref = str(packet["source_packet_ref"])
    refs = {
        field: str(packet[field])
        for field in ("room_graph_ref", "walkable_mesh_ref", "portal_graph_ref")
    }
    truth_refs = list(
        dict.fromkeys(
            [
                *_governed_spatial_unique_refs(packet["truth_refs"], field="truth_refs"),
                str(packet["source_authority_ref"]),
                str(packet["rights_authorization_ref"]),
                str(packet["consent_authorization_ref"]),
                str(packet["publication_authorization_ref"]),
            ]
        )
    )
    evidence_refs = list(
        dict.fromkeys(
            [
                *_governed_spatial_unique_refs(packet["evidence_refs"], field="evidence_refs"),
                str(packet["source_authority_receipt_digest"]),
                str(packet["privacy_policy_ref"]),
                *_governed_spatial_unique_refs(
                    packet["license_provenance_refs"], field="license_provenance_refs"
                ),
            ]
        )
    )
    contract_version = str(packet["contract_version"])
    route_room_ids = list(packet["route_room_ids"])
    allow_revisit = len(route_room_ids) != len(set(route_room_ids))
    if contract_version == _GOVERNED_SPATIAL_INPUT_VERSION_1_1:
        route_binding_material = {
            "contract_name": _GOVERNED_SPATIAL_INPUT_CONTRACT,
            "contract_version": contract_version,
            "route_priority_room_ids": list(packet["route_priority_room_ids"]),
            "route_start_room_id": str(packet["route_start_room_id"]),
            "expanded_route_room_ids": route_room_ids,
            "allow_revisit": allow_revisit,
        }
        evidence_refs.append(
            "property-route-plan:"
            + _governed_spatial_digest(route_binding_material).removeprefix("sha256:")
        )

    if contract_version == _GOVERNED_SPATIAL_INPUT_VERSION:
        request_portal_edges = [
            {"from_room_id": row["from_room_id"], "to_room_id": row["to_room_id"]}
            for row in portals
            if row["from_room_id"] in walkable_room_ids and row["to_room_id"] in walkable_room_ids
        ]
    else:
        unique_portal_edges = {
            tuple(sorted((str(row["from_room_id"]), str(row["to_room_id"]))))
            for row in portals
            if row["from_room_id"] in walkable_room_ids and row["to_room_id"] in walkable_room_ids
        }
        request_portal_edges = [
            {"from_room_id": left, "to_room_id": right}
            for left, right in sorted(unique_portal_edges)
        ]
    request = {
        "contract_name": "ea.governed_spatial_render_request.v1",
        "request_id": str(request_id or "").strip(),
        "idempotency_key": _governed_spatial_ref(idempotency_key),
        "consumer": {
            "product": "propertyquarry",
            "tenant_ref": str(packet["tenant_ref"]),
            "subject_ref": str(packet["subject_ref"]),
        },
        "artifact": {
            "kind": "continuous_walkthrough",
            "purpose": "walkthrough",
            "locale": str(packet["locale"]),
        },
        "source_packet_ref": source_packet_ref,
        "truth_refs": truth_refs,
        "evidence_refs": evidence_refs,
        "spatial_plan": {
            **refs,
            "required_room_ids": list(walkable_room_ids),
            "route_room_ids": route_room_ids,
            "portal_edges": request_portal_edges,
            "route_policy": "continuous_all_walkable_rooms",
            "allow_revisit": allow_revisit,
        },
        "style": {
            "style_pack_id": _governed_spatial_ref(style_pack_id),
            "room_overrides": {},
            "asset_license_policy": "verified_reuse_only",
            "brand_claim_policy": "truthful_no_affiliation_claim",
        },
        "scene_overlays": [],
        "camera": {
            "height_m": 1.62,
            "target_delivery_fps": 60,
            "minimum_effective_motion_fps": 30,
            "motion_profile": "slow_inspection",
            "cuts_allowed": False,
            "teleports_allowed": False,
            "collision_avoidance": True,
            "rotation_smoothing": True,
        },
        "output": {
            "desktop": True,
            "mobile": True,
            "video_codec": "h264",
            "interactive_package": True,
            "poster_frame": True,
            "contact_sheet": True,
        },
        "content_policy": {
            "rating": "general",
            "graphic_injury": False,
            "real_person_likeness": False,
            "minor_combatants": False,
        },
        "quota": {"consume_quota": False, "maximum_provider_attempts": 0},
        "callback": {"product_event_ref": _governed_spatial_ref(product_event_ref)},
    }
    source_packet = {
        "contract_name": "ea.governed_spatial_source_packet.v1",
        "source_packet_ref": source_packet_ref,
        "source_digest": packet["source_digest"],
        "source_retrieved_at": _governed_spatial_iso(
            _governed_spatial_timestamp(packet["source_retrieved_at"], field="source_retrieved_at")
        ),
        "normalized_floorplan_ref": packet["normalized_floorplan_ref"],
        **refs,
        "scale_m_per_unit": packet["scale_m_per_unit"],
        "orientation_degrees": packet["orientation_degrees"],
        "license_provenance_refs": list(packet["license_provenance_refs"]),
        "source_media_assignments": list(packet["source_media_assignments"]),
        "inaccessible_rooms": list(packet["inaccessible_rooms"]),
        "route_exclusions": [],
        "rooms": rooms,
        "portals": portals,
        "route_room_ids": route_room_ids,
        "existing_artifacts": {},
    }
    bridge_material = {
        "contract_name": _GOVERNED_SPATIAL_INPUT_CONTRACT,
        "contract_version": contract_version,
        "request": request,
        "source_packet": source_packet,
    }
    return {**bridge_material, "bridge_digest": _governed_spatial_digest(bridge_material)}


def governed_property_tour_public_projection(
    *,
    composition_receipt: Mapping[str, object],
    lifecycle_state: Mapping[str, object],
) -> dict[str, object]:
    eligible = (
        composition_receipt.get("status") == "accepted"
        and lifecycle_state.get("status") in {"active", "active_private"}
        and lifecycle_state.get("revoked") is False
        and lifecycle_state.get("deleted") is False
    )
    return {
        "state": "composed_private" if eligible else "blocked",
        "composition_digest": str(composition_receipt.get("composition_digest") or ""),
        "public_ready": False,
        "serving_allowed": False,
        "provider_details_exposed": False,
        "artifact_ref": "",
        "reason": "verified_publication_authority_required" if eligible else "privacy_or_composition_blocked",
    }


class GovernedPropertyTourLifecycleStore:
    _PRIVATE_DIR = ".governed-spatial-lifecycle"
    _FAMILIES = frozenset({"states", "tombstones", "transactions"})

    def __init__(self, public_root: Path) -> None:
        self.public_root = Path(os.path.abspath(os.path.expanduser(os.fspath(public_root))))
        self.private_root = self.public_root / self._PRIVATE_DIR
        self.state_root = self.private_root / "states"
        self.tombstone_root = self.private_root / "tombstones"
        self.transaction_root = self.private_root / "transactions"
        self.index_path = self.private_root / "index.json"
        self._thread_lock = threading.RLock()
        self._lock_descriptor: int | None = None
        self._prepare_directories()
        private_fd = self._directory_fd()
        try:
            lock_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            self._lock_descriptor = os.open(".lifecycle.lock", lock_flags, 0o600, dir_fd=private_fd)
            lock_details = os.fstat(self._lock_descriptor)
            if not stat.S_ISREG(lock_details.st_mode):
                raise GovernedPropertyTourIntegrityError("lifecycle_lock_not_regular")
            os.fchmod(self._lock_descriptor, 0o600)
        finally:
            os.close(private_fd)
        with self._guard():
            index = self._read_private(None, "index.json", allow_missing=True)
            if index is None:
                self._write_private(None, "index.json", self._empty_index())
            self._recover_closeout_transactions()
            current_index = self._read_private(None, "index.json", allow_missing=False)
            if current_index is None:
                raise GovernedPropertyTourIntegrityError("lifecycle_index_missing")
            self._validate_index(current_index)

    def __del__(self) -> None:
        descriptor = getattr(self, "_lock_descriptor", None)
        if isinstance(descriptor, int):
            try:
                os.close(descriptor)
            except OSError:
                pass
            self._lock_descriptor = None

    @staticmethod
    def _slug(value: object) -> str:
        normalized = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", normalized):
            raise GovernedPropertyTourContractError("governed_tour_slug_invalid")
        return normalized

    @staticmethod
    def _slug_digest(slug: str) -> str:
        return "sha256:" + hashlib.sha256(slug.encode("utf-8")).hexdigest()

    @staticmethod
    def _owner_principal_digest(value: object) -> str:
        normalized = str(value or "").strip()
        if not normalized or len(normalized) > 256:
            raise GovernedPropertyTourContractError("owner_principal_ref_required")
        return _governed_spatial_digest({"owner_principal_ref": normalized})

    @staticmethod
    def _record_name(scope_digest: str) -> str:
        return f"{scope_digest.removeprefix('sha256:')}.json"

    def state_path(self, slug: str) -> Path:
        return self.state_root / self._record_name(self._slug_digest(self._slug(slug)))

    def tombstone_path(self, slug: str) -> Path:
        return self.tombstone_root / self._record_name(self._slug_digest(self._slug(slug)))

    def _prepare_directories(self) -> None:
        current = Path(self.public_root.anchor)
        for part in self.public_root.parts[1:]:
            current /= part
            if not os.path.lexists(current):
                raise GovernedPropertyTourIntegrityError("public_tour_root_missing")
            details = os.lstat(current)
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
                raise GovernedPropertyTourIntegrityError("public_tour_root_component_invalid")
        descriptor = os.open(
            self.public_root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            try:
                private_details = os.stat(self._PRIVATE_DIR, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                try:
                    os.mkdir(self._PRIVATE_DIR, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                private_details = os.stat(self._PRIVATE_DIR, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(private_details.st_mode) or not stat.S_ISDIR(private_details.st_mode):
                raise GovernedPropertyTourIntegrityError("lifecycle_directory_component_invalid")
            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            private_fd = os.open(self._PRIVATE_DIR, flags, dir_fd=descriptor)
            try:
                os.fchmod(private_fd, 0o700)
                for name in ("states", "tombstones", "transactions"):
                    try:
                        details = os.stat(name, dir_fd=private_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        try:
                            os.mkdir(name, mode=0o700, dir_fd=private_fd)
                        except FileExistsError:
                            pass
                        details = os.stat(name, dir_fd=private_fd, follow_symlinks=False)
                    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
                        raise GovernedPropertyTourIntegrityError("lifecycle_directory_component_invalid")
                    child = os.open(name, flags, dir_fd=private_fd)
                    try:
                        os.fchmod(child, 0o700)
                    finally:
                        os.close(child)
            finally:
                os.close(private_fd)
        finally:
            os.close(descriptor)

    def _directory_fd(self, family: str | None = None) -> int:
        if family is not None and family not in self._FAMILIES:
            raise GovernedPropertyTourIntegrityError("lifecycle_family_invalid")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        root_fd = os.open(self.public_root, flags)
        try:
            private_fd = os.open(self._PRIVATE_DIR, flags, dir_fd=root_fd)
        finally:
            os.close(root_fd)
        if family is None:
            return private_fd
        try:
            family_fd = os.open(family, flags, dir_fd=private_fd)
        finally:
            os.close(private_fd)
        return family_fd

    @contextmanager
    def _guard(self) -> Iterator[None]:
        with self._thread_lock:
            if self._lock_descriptor is not None:
                fcntl.flock(self._lock_descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if self._lock_descriptor is not None:
                    fcntl.flock(self._lock_descriptor, fcntl.LOCK_UN)

    @staticmethod
    def _sealed(payload: Mapping[str, object]) -> dict[str, object]:
        material = dict(payload)
        material.pop("integrity_digest", None)
        return {**material, "integrity_digest": _governed_spatial_digest(material)}

    @staticmethod
    def _parse_persisted(raw: bytes) -> dict[str, object]:
        def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise GovernedPropertyTourIntegrityError("lifecycle_duplicate_persisted_member")
                result[key] = value
            return result

        try:
            payload = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=unique_pairs)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            if isinstance(exc, GovernedPropertyTourIntegrityError):
                raise
            raise GovernedPropertyTourIntegrityError("lifecycle_record_json_invalid") from exc
        if not isinstance(payload, dict):
            raise GovernedPropertyTourIntegrityError("lifecycle_record_object_required")
        supplied = payload.get("integrity_digest")
        material = dict(payload)
        material.pop("integrity_digest", None)
        if supplied != _governed_spatial_digest(material):
            raise GovernedPropertyTourIntegrityError("lifecycle_record_integrity_invalid")
        return payload

    def _write_private(
        self,
        family: str | None,
        name: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        if not re.fullmatch(r"(?:[a-f0-9]{64}|index)\.json", name):
            raise GovernedPropertyTourIntegrityError("lifecycle_record_name_invalid")
        sealed = self._sealed(payload)
        encoded = (_governed_spatial_compact_json(sealed) + "\n").encode("utf-8")
        directory_fd = self._directory_fd(family)
        temporary = f".{name}.{uuid4().hex}.tmp"
        descriptor: int | None = None
        created = False
        try:
            try:
                existing = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and (stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)):
                raise GovernedPropertyTourIntegrityError("lifecycle_record_path_invalid")
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
            created = True
            os.fchmod(descriptor, 0o600)
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            created = False
            os.fsync(directory_fd)
            return sealed
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if created:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
            os.close(directory_fd)

    def _read_private(
        self,
        family: str | None,
        name: str,
        *,
        allow_missing: bool,
    ) -> dict[str, object] | None:
        directory_fd = self._directory_fd(family)
        descriptor: int | None = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(name, flags, dir_fd=directory_fd)
            except FileNotFoundError:
                if allow_missing:
                    return None
                raise GovernedPropertyTourIntegrityError("lifecycle_record_missing")
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode) or details.st_mode & 0o077:
                raise GovernedPropertyTourIntegrityError("lifecycle_record_permissions_invalid")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > _GOVERNED_SPATIAL_MAX_RAW_BYTES:
                    raise GovernedPropertyTourIntegrityError("lifecycle_record_too_large")
                chunks.append(chunk)
            return self._parse_persisted(b"".join(chunks))
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(directory_fd)

    def _unlink_private(self, family: str, name: str) -> None:
        descriptor = self._directory_fd(family)
        try:
            try:
                details = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
                raise GovernedPropertyTourIntegrityError("lifecycle_record_path_invalid")
            os.unlink(name, dir_fd=descriptor)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _empty_index() -> dict[str, object]:
        return {"contract_name": _GOVERNED_SPATIAL_INDEX_CONTRACT, "entries": {}}

    def _load_index(self) -> dict[str, object]:
        index = self._read_private(None, "index.json", allow_missing=False)
        if index is None:
            raise GovernedPropertyTourIntegrityError("lifecycle_index_missing")
        self._validate_index(index)
        return index

    def _family_files(self, family: str) -> set[str]:
        descriptor = self._directory_fd(family)
        try:
            names = set(os.listdir(descriptor))
        finally:
            os.close(descriptor)
        if any(not re.fullmatch(r"[a-f0-9]{64}\.json", name) for name in names):
            raise GovernedPropertyTourIntegrityError("lifecycle_orphan_or_temporary_record")
        return names

    def _validate_index(self, index: Mapping[str, object], *, scan_records: bool = True) -> None:
        if index.get("contract_name") != _GOVERNED_SPATIAL_INDEX_CONTRACT:
            raise GovernedPropertyTourIntegrityError("lifecycle_index_contract_invalid")
        entries = index.get("entries")
        if not isinstance(entries, Mapping):
            raise GovernedPropertyTourIntegrityError("lifecycle_index_entries_invalid")
        expected_states: set[str] = set()
        expected_tombstones: set[str] = set()
        for scope_digest, raw_entry in entries.items():
            if not isinstance(scope_digest, str) or not _GOVERNED_SPATIAL_DIGEST_RE.fullmatch(scope_digest):
                raise GovernedPropertyTourIntegrityError("lifecycle_index_scope_invalid")
            entry = _governed_spatial_exact_fields(
                raw_entry,
                allowed=frozenset({"state_file", "state_digest", "tombstone_file", "tombstone_digest"}),
                path="lifecycle_index_entry",
            )
            expected_name = self._record_name(scope_digest)
            state_file = entry["state_file"]
            if state_file != expected_name:
                raise GovernedPropertyTourIntegrityError("lifecycle_index_state_path_invalid")
            state = self._read_private("states", expected_name, allow_missing=False)
            if state is None or state.get("integrity_digest") != entry["state_digest"]:
                raise GovernedPropertyTourIntegrityError("lifecycle_index_state_digest_invalid")
            if state.get("tour_scope_digest") != scope_digest:
                raise GovernedPropertyTourIntegrityError("lifecycle_state_scope_invalid")
            expected_states.add(expected_name)
            tombstone_file = entry["tombstone_file"]
            tombstone_digest = entry["tombstone_digest"]
            if tombstone_file is None and tombstone_digest is None:
                continue
            if tombstone_file != expected_name or not isinstance(tombstone_digest, str):
                raise GovernedPropertyTourIntegrityError("lifecycle_index_tombstone_path_invalid")
            tombstone = self._read_private("tombstones", expected_name, allow_missing=False)
            if tombstone is None or tombstone.get("integrity_digest") != tombstone_digest:
                raise GovernedPropertyTourIntegrityError("lifecycle_index_tombstone_digest_invalid")
            expected_tombstones.add(expected_name)
        if scan_records:
            if (
                self._family_files("states") != expected_states
                or self._family_files("tombstones") != expected_tombstones
                or self._family_files("transactions")
            ):
                raise GovernedPropertyTourIntegrityError("lifecycle_orphan_record_detected")

    def _closeout_fault(self, stage: str) -> None:
        del stage

    def _intake_fault(self, stage: str) -> None:
        del stage

    def _recover_intake_transaction(self, name: str, intent: Mapping[str, object]) -> None:
        required = {
            "contract_name",
            "tour_scope_digest",
            "material_digest",
            "state_material",
            "state_digest",
            "integrity_digest",
        }
        if set(intent) != required:
            raise GovernedPropertyTourIntegrityError("intake_intent_members_invalid")
        scope_digest = intent.get("tour_scope_digest")
        if not isinstance(scope_digest, str) or name != self._record_name(scope_digest):
            raise GovernedPropertyTourIntegrityError("intake_intent_scope_invalid")
        state_material = intent.get("state_material")
        if not isinstance(state_material, Mapping):
            raise GovernedPropertyTourIntegrityError("intake_intent_state_invalid")
        expected_state = self._sealed(state_material)
        if (
            expected_state.get("integrity_digest") != intent.get("state_digest")
            or state_material.get("material_digest") != intent.get("material_digest")
        ):
            raise GovernedPropertyTourIntegrityError("intake_intent_digest_invalid")
        existing_state = self._read_private("states", name, allow_missing=True)
        if existing_state is None:
            sealed_state = self._write_private("states", name, state_material)
        elif existing_state == expected_state:
            sealed_state = existing_state
        else:
            raise GovernedPropertyTourIntegrityError("intake_recovery_state_conflict")
        index = self._read_private(None, "index.json", allow_missing=False)
        if index is None:
            raise GovernedPropertyTourIntegrityError("lifecycle_index_missing")
        self._validate_index(index, scan_records=False)
        entries = dict(index["entries"])
        existing_entry = entries.get(scope_digest)
        if existing_entry is not None:
            if not isinstance(existing_entry, Mapping) or existing_entry.get("state_digest") != sealed_state["integrity_digest"]:
                raise GovernedPropertyTourIntegrityError("intake_recovery_index_conflict")
        entries[scope_digest] = {
            "state_file": name,
            "state_digest": sealed_state["integrity_digest"],
            "tombstone_file": None,
            "tombstone_digest": None,
        }
        self._write_private(
            None,
            "index.json",
            {"contract_name": _GOVERNED_SPATIAL_INDEX_CONTRACT, "entries": entries},
        )
        self._unlink_private("transactions", name)

    def _recover_closeout_transactions(self) -> None:
        for name in sorted(self._family_files("transactions")):
            intent = self._read_private("transactions", name, allow_missing=False)
            if intent is None:
                raise GovernedPropertyTourIntegrityError("closeout_intent_missing")
            if intent.get("contract_name") == "propertyquarry.governed_spatial_intake_intent.v1":
                self._recover_intake_transaction(name, intent)
                continue
            required = frozenset(
                {
                    "contract_name",
                    "slug",
                    "tour_scope_digest",
                    "state_digest",
                    "material_digest",
                    "tombstone_material",
                    "tombstone_digest",
                    "integrity_digest",
                }
            )
            if set(intent) != required or intent.get("contract_name") != "propertyquarry.governed_spatial_closeout_intent.v1":
                raise GovernedPropertyTourIntegrityError("closeout_intent_contract_invalid")
            slug = self._slug(intent.get("slug"))
            scope_digest = self._slug_digest(slug)
            if intent.get("tour_scope_digest") != scope_digest or name != self._record_name(scope_digest):
                raise GovernedPropertyTourIntegrityError("closeout_intent_scope_invalid")
            state = self._read_private("states", name, allow_missing=False)
            if state is None or state.get("integrity_digest") != intent.get("state_digest"):
                raise GovernedPropertyTourIntegrityError("closeout_intent_state_changed")
            tombstone_material = intent.get("tombstone_material")
            if not isinstance(tombstone_material, Mapping):
                raise GovernedPropertyTourIntegrityError("closeout_intent_tombstone_invalid")
            expected_tombstone = self._sealed(tombstone_material)
            if (
                expected_tombstone.get("integrity_digest") != intent.get("tombstone_digest")
                or tombstone_material.get("material_digest") != intent.get("material_digest")
            ):
                raise GovernedPropertyTourIntegrityError("closeout_intent_digest_invalid")

            self._remove_public_bundle(slug)
            existing_tombstone = self._read_private("tombstones", name, allow_missing=True)
            if existing_tombstone is None:
                sealed_tombstone = self._write_private("tombstones", name, tombstone_material)
            elif existing_tombstone == expected_tombstone:
                sealed_tombstone = existing_tombstone
            else:
                raise GovernedPropertyTourIntegrityError("closeout_recovery_tombstone_conflict")

            index = self._read_private(None, "index.json", allow_missing=False)
            if index is None:
                raise GovernedPropertyTourIntegrityError("lifecycle_index_missing")
            self._validate_index(index, scan_records=False)
            entries = dict(index["entries"])
            entry = entries.get(scope_digest)
            if not isinstance(entry, Mapping) or entry.get("state_digest") != state.get("integrity_digest"):
                raise GovernedPropertyTourIntegrityError("closeout_recovery_index_lineage_invalid")
            existing_tombstone_digest = entry.get("tombstone_digest")
            if existing_tombstone_digest not in {None, sealed_tombstone["integrity_digest"]}:
                raise GovernedPropertyTourIntegrityError("closeout_recovery_index_conflict")
            entries[scope_digest] = {
                "state_file": name,
                "state_digest": state["integrity_digest"],
                "tombstone_file": name,
                "tombstone_digest": sealed_tombstone["integrity_digest"],
            }
            self._write_private(
                None,
                "index.json",
                {"contract_name": _GOVERNED_SPATIAL_INDEX_CONTRACT, "entries": entries},
            )
            self._unlink_private("transactions", name)

    @staticmethod
    def _safe_public_state(
        *,
        status: str,
        reason: str,
        serving_allowed: bool,
        deleted: bool,
        revoked: bool,
        retention_expires_at_epoch: int = 0,
        lifecycle_receipt_digest: str = "",
        composition_digest: str = "",
        artifact_digest: str = "",
        publication_decision_digest: str = "",
    ) -> dict[str, object]:
        return {
            "status": status,
            "reason": reason,
            "serving_allowed": serving_allowed,
            "deleted": deleted,
            "revoked": revoked,
            "retention_expires_at_epoch": retention_expires_at_epoch,
            "lifecycle_receipt_digest": lifecycle_receipt_digest,
            "composition_digest": composition_digest,
            "artifact_digest": artifact_digest,
            "publication_decision_digest": publication_decision_digest,
            "provider_details_exposed": False,
            "artifact_ref": "",
        }

    def intake(
        self,
        *,
        slug: str,
        policy_payload: dict[str, object] | None,
        observed_at: datetime,
        bridge_digest: str | None = None,
        composition_digest: str | None = None,
        composition_receipt_digest: str | None = None,
        publication_authority: VerifiedGovernedPropertyTourPublication | None = None,
        owner_principal_ref: str | None = None,
        tenant_ref: str | None = None,
        subject_ref: str | None = None,
    ) -> dict[str, object]:
        normalized_slug = self._slug(slug)
        observed = _governed_spatial_observed(observed_at)
        if not policy_payload:
            return self._safe_public_state(
                status="blocked",
                reason="approved_numeric_retention_policy_required",
                serving_allowed=False,
                deleted=False,
                revoked=False,
            )
        policy = GovernedPropertyTourRetentionPolicy.from_payload(policy_payload, observed_at=observed)
        checked_bridge_digest = _governed_spatial_digest_value(bridge_digest, field="bridge_digest")
        checked_composition_digest = _governed_spatial_digest_value(
            composition_digest, field="composition_digest"
        )
        checked_receipt_digest = _governed_spatial_digest_value(
            composition_receipt_digest, field="composition_receipt_digest"
        )
        owner_principal_digest = self._owner_principal_digest(owner_principal_ref)
        tenant_ref_digest = _governed_spatial_digest(_governed_spatial_ref(tenant_ref))
        subject_ref_digest = _governed_spatial_digest(_governed_spatial_ref(subject_ref))
        scope_digest = self._slug_digest(normalized_slug)
        record_name = self._record_name(scope_digest)
        if publication_authority is not None:
            publication_authority.validate_binding(
                tour_scope_digest=scope_digest,
                composition_digest=checked_composition_digest,
                privacy_policy_digest=policy.policy_digest,
                observed_at=observed,
            )
        material_digest = _governed_spatial_digest(
            {
                "scope_digest": scope_digest,
                "policy_digest": policy.policy_digest,
                "bridge_digest": checked_bridge_digest,
                "composition_digest": checked_composition_digest,
                "composition_receipt_digest": checked_receipt_digest,
                "owner_principal_digest": owner_principal_digest,
                "tenant_ref_digest": tenant_ref_digest,
                "subject_ref_digest": subject_ref_digest,
                "publication_decision_digest": publication_authority.decision_digest
                if publication_authority is not None
                else None,
            }
        )
        with self._guard():
            index = self._load_index()
            tombstone = self._read_private("tombstones", record_name, allow_missing=True)
            if tombstone is not None:
                return tombstone
            existing = self._read_private("states", record_name, allow_missing=True)
            if existing is not None:
                if existing.get("material_digest") != material_digest:
                    raise GovernedPropertyTourContractError("governed_tour_intake_conflict")
                return existing
            state_material = {
                "contract_name": _GOVERNED_SPATIAL_LIFECYCLE_CONTRACT,
                "contract_version": "1.0.0",
                "tour_scope_digest": scope_digest,
                "status": "active_private",
                "material_digest": material_digest,
                "policy_digest": policy.policy_digest,
                "bridge_digest": checked_bridge_digest,
                "composition_digest": checked_composition_digest,
                "composition_receipt_digest": checked_receipt_digest,
                "owner_principal_digest": owner_principal_digest,
                "tenant_ref_digest": tenant_ref_digest,
                "subject_ref_digest": subject_ref_digest,
                "publication_authority": publication_authority.material()
                | {"decision_digest": publication_authority.decision_digest}
                if publication_authority is not None
                else None,
                "authority_state": "verified_bound" if publication_authority is not None else "absent_blocked",
                "intake_at": _governed_spatial_iso(observed),
                "source_delete_after_epoch": int(observed.timestamp()) + policy.source_retention_days * 86400,
                "receipt_delete_after_epoch": int(observed.timestamp()) + policy.receipt_retention_days * 86400,
                "tombstone_delete_after_epoch": int(observed.timestamp()) + policy.tombstone_retention_days * 86400,
                "deleted": False,
                "revoked": False,
                "serving_allowed": False,
                "restoration_allowed": False,
            }
            expected_state = self._sealed(state_material)
            intent_material = {
                "contract_name": "propertyquarry.governed_spatial_intake_intent.v1",
                "tour_scope_digest": scope_digest,
                "material_digest": material_digest,
                "state_material": state_material,
                "state_digest": expected_state["integrity_digest"],
            }
            self._write_private("transactions", record_name, intent_material)
            self._intake_fault("after_intake_intent")
            sealed = self._write_private("states", record_name, state_material)
            self._intake_fault("after_intake_state")
            entries = dict(index["entries"])
            entries[scope_digest] = {
                "state_file": record_name,
                "state_digest": sealed["integrity_digest"],
                "tombstone_file": None,
                "tombstone_digest": None,
            }
            self._write_private(
                None,
                "index.json",
                {"contract_name": _GOVERNED_SPATIAL_INDEX_CONTRACT, "entries": entries},
            )
            self._intake_fault("after_intake_index")
            self._unlink_private("transactions", record_name)
            return sealed

    def private_state(self, *, slug: str) -> dict[str, object] | None:
        normalized = self._slug(slug)
        with self._guard():
            self._load_index()
            return self._read_private(
                "states",
                self._record_name(self._slug_digest(normalized)),
                allow_missing=True,
            )

    def require_owner(self, *, slug: str, owner_principal_ref: str) -> None:
        normalized = self._slug(slug)
        supplied_digest = self._owner_principal_digest(owner_principal_ref)
        with self._guard():
            self._load_index()
            state = self._read_private(
                "states",
                self._record_name(self._slug_digest(normalized)),
                allow_missing=True,
            )
            if state is None:
                raise GovernedPropertyTourContractError("governed_tour_scope_not_found")
            owner_digest = state.get("owner_principal_digest")
            tenant_digest = state.get("tenant_ref_digest")
            subject_digest = state.get("subject_ref_digest")
            bindings_valid = all(
                isinstance(value, str) and _GOVERNED_SPATIAL_DIGEST_RE.fullmatch(value)
                for value in (owner_digest, tenant_digest, subject_digest)
            )
            if (
                not bindings_valid
                or not isinstance(owner_digest, str)
                or not hmac.compare_digest(owner_digest, supplied_digest)
            ):
                raise GovernedPropertyTourContractError("governed_tour_scope_owner_mismatch")

    def public_state(self, *, slug: str, observed_at: datetime) -> dict[str, object]:
        normalized = self._slug(slug)
        observed = _governed_spatial_observed(observed_at)
        scope_digest = self._slug_digest(normalized)
        record_name = self._record_name(scope_digest)
        with self._guard():
            self._load_index()
            tombstone = self._read_private("tombstones", record_name, allow_missing=True)
            if tombstone is not None:
                return self._safe_public_state(
                    status="blocked",
                    reason="tour_deleted_or_revoked",
                    serving_allowed=False,
                    deleted=tombstone.get("deleted") is True,
                    revoked=True,
                    lifecycle_receipt_digest=str(tombstone.get("integrity_digest") or ""),
                )
            state = self._read_private("states", record_name, allow_missing=True)
            if state is None:
                return self._safe_public_state(
                    status="blocked",
                    reason="privacy_lifecycle_missing",
                    serving_allowed=False,
                    deleted=False,
                    revoked=False,
                )
            deadline = int(state.get("source_delete_after_epoch") or 0)
            if state.get("deleted") is True or state.get("revoked") is True:
                reason = "tour_deleted_or_revoked"
            elif deadline <= int(observed.timestamp()):
                reason = "retention_expired"
            elif state.get("authority_state") != "verified_bound":
                reason = "publication_authority_missing"
            else:
                raw_authority = state.get("publication_authority")
                if not isinstance(raw_authority, Mapping):
                    reason = "publication_authority_invalid"
                else:
                    try:
                        authority = VerifiedGovernedPropertyTourPublication(**dict(raw_authority))
                        authority.validate_binding(
                            tour_scope_digest=scope_digest,
                            composition_digest=str(state.get("composition_digest") or ""),
                            privacy_policy_digest=str(state.get("policy_digest") or ""),
                            observed_at=observed,
                        )
                    except (TypeError, GovernedPropertyTourContractError):
                        reason = "publication_authority_invalid_or_expired"
                    else:
                        return self._safe_public_state(
                            status="active",
                            reason="",
                            serving_allowed=True,
                            deleted=False,
                            revoked=False,
                            retention_expires_at_epoch=deadline,
                            lifecycle_receipt_digest=str(state.get("integrity_digest") or ""),
                            composition_digest=authority.composition_digest,
                            artifact_digest=authority.artifact_digest,
                            publication_decision_digest=authority.decision_digest,
                        )
            return self._safe_public_state(
                status="blocked",
                reason=reason,
                serving_allowed=False,
                deleted=state.get("deleted") is True,
                revoked=state.get("revoked") is True,
                retention_expires_at_epoch=deadline,
                lifecycle_receipt_digest=str(state.get("integrity_digest") or ""),
            )

    def _legal_hold(
        self,
        value: Mapping[str, object] | None,
        *,
        scope_digest: str,
        observed_at: datetime,
    ) -> dict[str, object] | None:
        if value is None:
            return None
        fields = frozenset(
            {
                "contract_name",
                "scope_digest",
                "case_ref_digest",
                "authority_ref_digest",
                "issued_at",
                "expires_at",
                "review_due_at",
                "hold_digest",
            }
        )
        hold = _governed_spatial_exact_fields(value, allowed=fields, path="legal_hold")
        material = {field: hold[field] for field in fields if field != "hold_digest"}
        if hold["contract_name"] != _GOVERNED_SPATIAL_LEGAL_HOLD_CONTRACT:
            raise GovernedPropertyTourContractError("legal_hold_contract_invalid")
        for field in ("scope_digest", "case_ref_digest", "authority_ref_digest", "hold_digest"):
            _governed_spatial_digest_value(hold[field], field=field)
        if hold["hold_digest"] != _governed_spatial_digest(material):
            raise GovernedPropertyTourContractError("legal_hold_digest_invalid")
        issued = _governed_spatial_timestamp(hold["issued_at"], field="legal_hold_issued_at")
        expires = _governed_spatial_timestamp(hold["expires_at"], field="legal_hold_expires_at")
        review_due = _governed_spatial_timestamp(hold["review_due_at"], field="legal_hold_review_due_at")
        if hold["scope_digest"] != scope_digest or issued >= expires or not issued <= observed_at < expires:
            raise GovernedPropertyTourContractError("legal_hold_not_current_or_bound")
        if review_due < observed_at or review_due > expires:
            raise GovernedPropertyTourContractError("legal_hold_review_due_invalid")
        return {
            "state": "valid_retain_evidence_only",
            "hold_digest": hold["hold_digest"],
            "expires_at": _governed_spatial_iso(expires),
            "review_due_at": _governed_spatial_iso(review_due),
            "serving_allowed": False,
            "restoration_allowed": False,
        }

    def _remove_public_bundle(self, slug: str) -> tuple[bool, str]:
        public_fd = os.open(
            self.public_root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        private_fd = self._directory_fd()
        quarantine_name = f".delete-{uuid4().hex}"
        removed = True
        try:
            try:
                details = os.stat(slug, dir_fd=public_fd, follow_symlinks=False)
            except FileNotFoundError:
                details = None
            if details is None:
                pass
            elif stat.S_ISLNK(details.st_mode) or stat.S_ISREG(details.st_mode):
                os.unlink(slug, dir_fd=public_fd)
            elif stat.S_ISDIR(details.st_mode):
                os.rename(slug, quarantine_name, src_dir_fd=public_fd, dst_dir_fd=private_fd)
                quarantine_path = self.private_root / quarantine_name
                quarantine_details = os.lstat(quarantine_path)
                if stat.S_ISLNK(quarantine_details.st_mode) or not stat.S_ISDIR(quarantine_details.st_mode):
                    raise GovernedPropertyTourIntegrityError("quarantined_bundle_invalid")
                shutil.rmtree(quarantine_path)
            else:
                raise GovernedPropertyTourIntegrityError("public_bundle_type_invalid")
            os.fsync(public_fd)
            os.fsync(private_fd)
        finally:
            os.close(public_fd)
            os.close(private_fd)
        evidence = self._local_deletion_evidence(slug)
        return removed, evidence

    def _local_deletion_evidence(self, slug: str) -> str:
        return _governed_spatial_digest(
            {
                "tour_scope_digest": self._slug_digest(self._slug(slug)),
                "operation": "delete_or_confirm_absent_local_public_bundle",
                "result": "deleted_or_already_absent",
            }
        )

    def closeout(
        self,
        *,
        slug: str,
        action: str,
        reason_digest: str,
        observed_at: datetime,
        cascade_evidence_digests: Iterable[str] = (),
        legal_hold: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        normalized = self._slug(slug)
        if action not in {"revoked", "deleted", "withdrawn"}:
            raise GovernedPropertyTourContractError("privacy_closeout_action_invalid")
        checked_reason = _governed_spatial_digest_value(reason_digest, field="reason_digest")
        observed = _governed_spatial_observed(observed_at)
        supplied_evidence = sorted(
            {_governed_spatial_digest_value(value, field="cascade_evidence_digest") for value in cascade_evidence_digests}
        )
        scope_digest = self._slug_digest(normalized)
        record_name = self._record_name(scope_digest)
        try:
            hold_projection = self._legal_hold(
                legal_hold,
                scope_digest=scope_digest,
                observed_at=observed,
            )
        except GovernedPropertyTourContractError as exc:
            hold_projection = {
                "state": "invalid_fail_closed",
                "reason_code": str(exc).split(":", 1)[0],
                "serving_allowed": False,
                "restoration_allowed": False,
            }
        local_evidence = self._local_deletion_evidence(normalized)
        evidence = sorted({*supplied_evidence, local_evidence})
        material_digest = _governed_spatial_digest(
            {
                "tour_scope_digest": scope_digest,
                "action": action,
                "reason_digest": checked_reason,
                "cascade_evidence_digests": evidence,
                "legal_hold": hold_projection,
            }
        )
        with self._guard():
            index = self._load_index()
            existing = self._read_private("tombstones", record_name, allow_missing=True)
            if existing is not None:
                if existing.get("material_digest") != material_digest:
                    raise GovernedPropertyTourContractError("privacy_closeout_conflict")
                return existing
            state = self._read_private("states", record_name, allow_missing=True)
            if state is None:
                return self._safe_public_state(
                    status="blocked",
                    reason="privacy_lifecycle_missing",
                    serving_allowed=False,
                    deleted=False,
                    revoked=False,
                )
            tombstone_material = {
                "contract_name": _GOVERNED_SPATIAL_LIFECYCLE_CONTRACT,
                "contract_version": "1.0.0",
                "tour_scope_digest": scope_digest,
                "status": action,
                "action": action,
                "material_digest": material_digest,
                "reason_digest": checked_reason,
                "closed_at": _governed_spatial_iso(observed),
                "policy_digest": state.get("policy_digest"),
                "composition_digest": state.get("composition_digest"),
                "owner_principal_digest": state.get("owner_principal_digest"),
                "tenant_ref_digest": state.get("tenant_ref_digest"),
                "subject_ref_digest": state.get("subject_ref_digest"),
                "cascade_evidence_digests": evidence,
                "local_deletion_complete": True,
                "provider_deletion_state": "not_configured_no_provider_action",
                "legal_hold": hold_projection,
                "deleted": True,
                "revoked": True,
                "serving_allowed": False,
                "build_allowed": False,
                "restoration_allowed": False,
                "public_projection": {"state": "unavailable", "artifact_ref": ""},
            }
            expected_tombstone = self._sealed(tombstone_material)
            intent_material = {
                "contract_name": "propertyquarry.governed_spatial_closeout_intent.v1",
                "slug": normalized,
                "tour_scope_digest": scope_digest,
                "state_digest": state["integrity_digest"],
                "material_digest": material_digest,
                "tombstone_material": tombstone_material,
                "tombstone_digest": expected_tombstone["integrity_digest"],
            }
            self._write_private("transactions", record_name, intent_material)
            self._closeout_fault("after_intent")
            deleted, deletion_evidence = self._remove_public_bundle(normalized)
            if not deleted or deletion_evidence != local_evidence:
                raise GovernedPropertyTourIntegrityError("local_bundle_deletion_evidence_invalid")
            self._closeout_fault("after_bundle_delete")
            sealed_tombstone = self._write_private("tombstones", record_name, tombstone_material)
            self._closeout_fault("after_tombstone")
            entries = dict(index["entries"])
            entries[scope_digest] = {
                "state_file": record_name,
                "state_digest": state["integrity_digest"],
                "tombstone_file": record_name,
                "tombstone_digest": sealed_tombstone["integrity_digest"],
            }
            self._write_private(
                None,
                "index.json",
                {"contract_name": _GOVERNED_SPATIAL_INDEX_CONTRACT, "entries": entries},
            )
            self._closeout_fault("after_index")
            self._unlink_private("transactions", record_name)
            return sealed_tombstone

    def enforce_retention(
        self,
        *,
        slug: str,
        observed_at: datetime,
        cascade_evidence_digests: Iterable[str] = (),
    ) -> dict[str, object]:
        normalized = self._slug(slug)
        with self._guard():
            self._load_index()
            tombstone = self._read_private(
                "tombstones",
                self._record_name(self._slug_digest(normalized)),
                allow_missing=True,
            )
            if tombstone is not None:
                return tombstone
        state = self.private_state(slug=slug)
        observed = _governed_spatial_observed(observed_at)
        if state is None:
            return self.public_state(slug=slug, observed_at=observed)
        if state.get("revoked") is True or state.get("deleted") is True:
            return self.public_state(slug=slug, observed_at=observed)
        if int(state.get("source_delete_after_epoch") or 0) > int(observed.timestamp()):
            return self.public_state(slug=slug, observed_at=observed)
        return self.closeout(
            slug=slug,
            action="deleted",
            reason_digest=_governed_spatial_digest(
                {"reason_code": "numeric_retention_expired", "tour_scope_digest": self._slug_digest(self._slug(slug))}
            ),
            observed_at=observed,
            cascade_evidence_digests=cascade_evidence_digests,
        )

    def restore(self, *, slug: str) -> None:
        del slug
        raise GovernedPropertyTourContractError("privacy_self_restoration_forbidden")
