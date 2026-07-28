from __future__ import annotations

import hashlib
import hmac
import json
import math
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
from app.product.property_tour_governed_reservations import (
    governed_prater_slug_reserved,
)

try:
    from scripts.property_magicfit_public_eligibility import (
        evaluate_magicfit_public_eligibility,
        magicfit_provider_declared,
    )
except ModuleNotFoundError:
    from property_magicfit_public_eligibility import (  # type: ignore[no-redef]
        evaluate_magicfit_public_eligibility,
        magicfit_provider_declared,
    )

try:
    from scripts.property_reconstruction_styles import (
        FLOORPLAN_DISPLAY_MODE,
        GENERATED_RECONSTRUCTION_VIEWER_VERSION,
        STYLE_SCENE_CONTRACT_VERSION,
        reconstruction_style,
        validate_style_scene,
    )
except ModuleNotFoundError:
    from property_reconstruction_styles import (  # type: ignore[no-redef]
        FLOORPLAN_DISPLAY_MODE,
        GENERATED_RECONSTRUCTION_VIEWER_VERSION,
        STYLE_SCENE_CONTRACT_VERSION,
        reconstruction_style,
        validate_style_scene,
    )

_PROPERTY_SCOUT_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0 Safari/537.36"
_PROPERTY_SCOUT_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
_PROPERTY_SCOUT_FLOORPLAN_ASSET_EXTENSIONS = (*_PROPERTY_SCOUT_IMAGE_EXTENSIONS, ".pdf")
_PROPERTY_PUBLIC_TOUR_MANIFEST = "tour.json"
_PROPERTY_PUBLIC_TOUR_PRIVATE_MANIFEST = "tour.private.json"
_HOSTED_PROPERTY_TOUR_PUBLICATION_LOCK_STATE = threading.local()
_PROPERTY_PUBLIC_TOUR_PRIVATE_RECEIPT_MERGE_KEYS = frozenset(
    {
        "principal_id",
        "search_run_id",
        "candidate_ref",
        "listing_url",
        "property_url",
        "source_ref",
        "external_id",
        "recipient_email",
        "crezlo_public_url",
        "source_virtual_tour_url",
        "source_virtual_tour_origin",
        "panorama_source",
        "pano2vr_spatial_provenance",
        "three_d_vista_import",
        "three_d_vista_white_label_proof",
        "three_d_vista_browser_render_proof",
        "three_d_vista_entry_relpath",
        "three_d_vista_url",
        "matterport_url",
        "private_exact_location",
    }
)
_3DVISTA_EXPORT_MARKERS = ("tdvplayer", "tdvplayerapi", "tourviewer")
_PANO2VR_EXPORT_MARKERS = ("ggpkg", "ggskin", "pano.xml", "tour.js")
_KRPANO_PANORAMA_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
_KRPANO_FORBIDDEN_SCENE_STRATEGIES = {"generated_listing_summary", "photo_gallery_hosted", "floorplan_hosted", "pure_360_cube"}
_KRPANO_FORBIDDEN_CREATION_MODES = {"hosted_listing_fallback", "hosted_photo_gallery_tour"}
_CUSTOMER_FACING_TOUR_PROVIDERS = ("3dvista",)
_HOSTED_PROPERTY_TOUR_SLUG_MAX_LENGTH = 120
_PROPERTY_PUBLIC_TOUR_SLUG_RE = re.compile(
    rf"[A-Za-z0-9][A-Za-z0-9._-]{{0,{_HOSTED_PROPERTY_TOUR_SLUG_MAX_LENGTH - 1}}}"
)
_PROPERTY_GENERATED_RECONSTRUCTION_VIEWER_VERSION = GENERATED_RECONSTRUCTION_VIEWER_VERSION
_AI_PANORAMA_CANONICAL_DISCLOSURE = (
    "AI-reconstructed from listing photos; not a captured 360 or measured survey."
)


def _assert_governed_property_tour_slug_not_reserved(slug: object) -> None:
    if governed_prater_slug_reserved(slug):
        raise RuntimeError("hosted_property_tour_governed_slug_reserved")


_AI_PANORAMA_THREE_MODULE_PATH = "/tours/runtime/three-0.167.1.module.js"
_AI_PANORAMA_THREE_MODULE_SHA256 = (
    "5289ca2dfde8572bd7715b9fa2ca929db12bae87e9a2cb53e431662df7039506"
)
_AI_PANORAMA_CORE_MANIFEST_EXCLUDED_ACCEPTANCE_FIELDS = frozenset(
    {
        # These fields are browser-proof lifecycle metadata.  Excluding only
        # them lets a preflight manifest be hashed before its browser receipt
        # exists, then sealed without changing the functional tour digest.
        "proof_status",
        "browser_receipt_relpath",
        "browser_receipt_sha256",
        "core_manifest_sha256",
    }
)
_3DVISTA_FORBIDDEN_PUBLIC_MARKERS = (
    "created with the trial of 3dvista",
    "created with 3dvista",
    "3dvista virtual tour suite",
    "immocontract",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hosted_property_tour_ai_panorama_browser_proof_current(
    browser_receipt: object,
    *,
    now: datetime | None = None,
) -> bool:
    """Apply the browser-proof clock window independently of asset caching."""

    if not isinstance(browser_receipt, Mapping):
        return False
    try:
        observed_at = datetime.fromisoformat(
            str(browser_receipt.get("observed_at") or "")
            .strip()
            .replace("Z", "+00:00")
        )
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            return False
        age_seconds = (
            current.astimezone(timezone.utc)
            - observed_at.astimezone(timezone.utc)
        ).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return False
    return -300.0 <= age_seconds <= 30 * 86400


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
    from app.product.property_search_tour_binding import (
        canonical_property_source_url,
        property_search_source_url_sha256,
    )

    normalized_payload = dict(payload or {})
    canonical_property_urls = {
        canonical
        for key in ("property_url", "listing_url")
        if str(normalized_payload.get(key) or "").strip()
        and (
            canonical := canonical_property_source_url(
                normalized_payload.get(key)
            )
        )
    }
    if len(canonical_property_urls) == 1:
        property_url_sha256 = property_search_source_url_sha256(
            next(iter(canonical_property_urls))
        )
        if property_url_sha256:
            normalized_payload["property_url_sha256"] = property_url_sha256
    else:
        # Never preserve a caller-supplied identity hash when the private
        # property/listing URL is absent, invalid, or internally conflicting.
        normalized_payload.pop("property_url_sha256", None)
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
    live_provider = (
        _property_tour_provider_host_kind(live_url)
        if _property_tour_provider_url_shape_valid(live_url)
        else ""
    )
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


class HostedPropertyTourManifestError(RuntimeError):
    """A public/private tour manifest failed a fail-closed filesystem check."""


class HostedPropertyTourManifestMissing(HostedPropertyTourManifestError):
    """The requested manifest or its containing directory does not exist."""


_HOSTED_PROPERTY_TOUR_TARGET_UNSET = object()


def _hosted_property_tour_manifest_max_bytes() -> int:
    raw_value = str(os.getenv("PROPERTYQUARRY_TOUR_MANIFEST_MAX_BYTES") or "").strip()
    try:
        parsed = int(raw_value) if raw_value else 2 * 1024 * 1024
    except (TypeError, ValueError):
        parsed = 2 * 1024 * 1024
    return max(1_024, min(parsed, 16 * 1024 * 1024))


def _hosted_property_tour_manifest_name(value: object) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or normalized in {".", ".."}
        or "/" in normalized
        or "\\" in normalized
        or "\x00" in normalized
    ):
        raise HostedPropertyTourManifestError("hosted_property_tour_manifest_name_invalid")
    return normalized


def _open_hosted_property_tour_directory(bundle_dir: Path) -> int:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise HostedPropertyTourManifestError("hosted_property_tour_manifest_nofollow_unavailable")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(bundle_dir, flags)
    except FileNotFoundError as exc:
        raise HostedPropertyTourManifestMissing("hosted_property_tour_bundle_missing") from exc
    except OSError as exc:
        raise HostedPropertyTourManifestError("hosted_property_tour_bundle_invalid") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISDIR(details.st_mode):
            raise HostedPropertyTourManifestError("hosted_property_tour_bundle_invalid")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _read_hosted_property_tour_regular_file_at(
    directory_fd: int,
    name: str,
    *,
    missing_ok: bool = False,
) -> tuple[bytes, os.stat_result] | None:
    normalized_name = _hosted_property_tour_manifest_name(name)
    flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(normalized_name, flags, dir_fd=directory_fd)
    except FileNotFoundError as exc:
        if missing_ok:
            return None
        raise HostedPropertyTourManifestMissing("hosted_property_tour_manifest_missing") from exc
    except OSError as exc:
        raise HostedPropertyTourManifestError("hosted_property_tour_manifest_open_failed") from exc
    try:
        before = os.fstat(descriptor)
        maximum_bytes = _hosted_property_tour_manifest_max_bytes()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > maximum_bytes
        ):
            raise HostedPropertyTourManifestError("hosted_property_tour_manifest_invalid")
        chunks: list[bytes] = []
        remaining = int(before.st_size)
        while remaining:
            chunk = os.read(descriptor, min(remaining, 65_536))
            if not chunk:
                raise HostedPropertyTourManifestError("hosted_property_tour_manifest_short_read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise HostedPropertyTourManifestError("hosted_property_tour_manifest_changed")
        after = os.fstat(descriptor)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_mode != after.st_mode
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
        ):
            raise HostedPropertyTourManifestError("hosted_property_tour_manifest_changed")
        return b"".join(chunks), before
    except HostedPropertyTourManifestError:
        raise
    except OSError as exc:
        raise HostedPropertyTourManifestError("hosted_property_tour_manifest_read_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_hosted_property_tour_json_file_with_stat(
    bundle_dir: Path,
    name: str,
    *,
    missing_ok: bool = False,
) -> tuple[dict[str, object], os.stat_result] | None:
    directory_fd = _open_hosted_property_tour_directory(bundle_dir)
    try:
        result = _read_hosted_property_tour_regular_file_at(
            directory_fd,
            name,
            missing_ok=missing_ok,
        )
    finally:
        os.close(directory_fd)
    if result is None:
        return None
    raw, details = result
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise HostedPropertyTourManifestError("hosted_property_tour_manifest_json_invalid") from exc
    if not isinstance(payload, dict):
        raise HostedPropertyTourManifestError("hosted_property_tour_manifest_json_invalid")
    return dict(payload), details


def _read_hosted_property_tour_json_file(
    bundle_dir: Path,
    name: str,
    *,
    missing_ok: bool = False,
) -> dict[str, object]:
    result = _read_hosted_property_tour_json_file_with_stat(
        bundle_dir,
        name,
        missing_ok=missing_ok,
    )
    return dict(result[0]) if result is not None else {}


def _write_hosted_property_tour_stage_at(
    directory_fd: int,
    *,
    final_name: str,
    encoded: bytes,
    mode: int,
    purpose: str,
) -> tuple[str, os.stat_result]:
    if not encoded or len(encoded) > _hosted_property_tour_manifest_max_bytes():
        raise HostedPropertyTourManifestError("hosted_property_tour_manifest_too_large")
    normalized_final_name = _hosted_property_tour_manifest_name(final_name)
    temporary_name = f".{normalized_final_name}.{purpose}.{uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = -1
    created = False
    completed = False
    try:
        descriptor = os.open(temporary_name, flags, mode, dir_fd=directory_fd)
        created = True
        os.fchmod(descriptor, mode)
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("hosted_property_tour_manifest_short_write")
            remaining = remaining[written:]
        os.fsync(descriptor)
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_size != len(encoded)
            or stat.S_IMODE(details.st_mode) != mode
        ):
            raise HostedPropertyTourManifestError("hosted_property_tour_manifest_stage_invalid")
        completed = True
        return temporary_name, details
    except HostedPropertyTourManifestError:
        raise
    except OSError as exc:
        raise HostedPropertyTourManifestError("hosted_property_tour_manifest_stage_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if created and not completed:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except OSError:
                pass


def _hosted_property_tour_stat_unchanged(
    expected: os.stat_result,
    observed: os.stat_result,
) -> bool:
    return (
        stat.S_ISREG(observed.st_mode)
        and not stat.S_ISLNK(observed.st_mode)
        and observed.st_dev == expected.st_dev
        and observed.st_ino == expected.st_ino
        and observed.st_mode == expected.st_mode
        and observed.st_size == expected.st_size
        and observed.st_mtime_ns == expected.st_mtime_ns
        and observed.st_ctime_ns == expected.st_ctime_ns
    )


def _hosted_property_tour_target_unchanged(
    directory_fd: int,
    name: str,
    expected: os.stat_result | None,
) -> None:
    try:
        observed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        if expected is None:
            return
        raise HostedPropertyTourManifestError("hosted_property_tour_manifest_target_changed")
    except OSError as exc:
        raise HostedPropertyTourManifestError("hosted_property_tour_manifest_target_invalid") from exc
    if expected is None:
        raise HostedPropertyTourManifestError("hosted_property_tour_manifest_target_changed")
    if not _hosted_property_tour_stat_unchanged(expected, observed):
        raise HostedPropertyTourManifestError("hosted_property_tour_manifest_target_changed")


def _write_hosted_property_tour_manifests_atomic(
    bundle_dir: Path,
    *,
    public_payload: dict[str, object],
    private_payload: dict[str, object],
) -> None:
    public_encoded = json.dumps(public_payload, ensure_ascii=False, indent=2).encode("utf-8")
    private_encoded = json.dumps(private_payload, ensure_ascii=False, indent=2).encode("utf-8")
    maximum_bytes = _hosted_property_tour_manifest_max_bytes()
    if (
        not public_encoded
        or not private_encoded
        or len(public_encoded) > maximum_bytes
        or len(private_encoded) > maximum_bytes
    ):
        raise HostedPropertyTourManifestError("hosted_property_tour_manifest_too_large")

    directory_fd = _open_hosted_property_tour_directory(bundle_dir)
    temporary_names: set[str] = set()
    committed: list[tuple[str, str | None, os.stat_result]] = []
    try:
        public_existing = _read_hosted_property_tour_regular_file_at(
            directory_fd,
            _PROPERTY_PUBLIC_TOUR_MANIFEST,
            missing_ok=True,
        )
        private_existing = _read_hosted_property_tour_regular_file_at(
            directory_fd,
            _PROPERTY_PUBLIC_TOUR_PRIVATE_MANIFEST,
            missing_ok=True,
        )
        public_old_bytes, public_old_stat = public_existing or (None, None)
        private_old_bytes, private_old_stat = private_existing or (None, None)

        public_stage, public_stage_stat = _write_hosted_property_tour_stage_at(
            directory_fd,
            final_name=_PROPERTY_PUBLIC_TOUR_MANIFEST,
            encoded=public_encoded,
            mode=0o644,
            purpose="stage",
        )
        temporary_names.add(public_stage)
        private_stage, private_stage_stat = _write_hosted_property_tour_stage_at(
            directory_fd,
            final_name=_PROPERTY_PUBLIC_TOUR_PRIVATE_MANIFEST,
            encoded=private_encoded,
            mode=0o600,
            purpose="stage",
        )
        temporary_names.add(private_stage)

        public_backup: str | None = None
        if public_old_bytes is not None and public_old_stat is not None:
            public_backup, _ = _write_hosted_property_tour_stage_at(
                directory_fd,
                final_name=_PROPERTY_PUBLIC_TOUR_MANIFEST,
                encoded=public_old_bytes,
                mode=stat.S_IMODE(public_old_stat.st_mode),
                purpose="rollback",
            )
            temporary_names.add(public_backup)
        private_backup: str | None = None
        if private_old_bytes is not None and private_old_stat is not None:
            private_backup, _ = _write_hosted_property_tour_stage_at(
                directory_fd,
                final_name=_PROPERTY_PUBLIC_TOUR_PRIVATE_MANIFEST,
                encoded=private_old_bytes,
                mode=stat.S_IMODE(private_old_stat.st_mode),
                purpose="rollback",
            )
            temporary_names.add(private_backup)

        _hosted_property_tour_target_unchanged(
            directory_fd,
            _PROPERTY_PUBLIC_TOUR_MANIFEST,
            public_old_stat,
        )
        _hosted_property_tour_target_unchanged(
            directory_fd,
            _PROPERTY_PUBLIC_TOUR_PRIVATE_MANIFEST,
            private_old_stat,
        )
        os.fsync(directory_fd)

        try:
            os.replace(
                public_stage,
                _PROPERTY_PUBLIC_TOUR_MANIFEST,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            temporary_names.discard(public_stage)
            committed.append((_PROPERTY_PUBLIC_TOUR_MANIFEST, public_backup, public_stage_stat))
            _hosted_property_tour_target_unchanged(
                directory_fd,
                _PROPERTY_PUBLIC_TOUR_PRIVATE_MANIFEST,
                private_old_stat,
            )
            os.replace(
                private_stage,
                _PROPERTY_PUBLIC_TOUR_PRIVATE_MANIFEST,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            temporary_names.discard(private_stage)
            committed.append((_PROPERTY_PUBLIC_TOUR_PRIVATE_MANIFEST, private_backup, private_stage_stat))

            public_written = _read_hosted_property_tour_regular_file_at(
                directory_fd,
                _PROPERTY_PUBLIC_TOUR_MANIFEST,
            )
            private_written = _read_hosted_property_tour_regular_file_at(
                directory_fd,
                _PROPERTY_PUBLIC_TOUR_PRIVATE_MANIFEST,
            )
            if (
                public_written is None
                or private_written is None
                or public_written[0] != public_encoded
                or private_written[0] != private_encoded
                or stat.S_IMODE(public_written[1].st_mode) != 0o644
                or stat.S_IMODE(private_written[1].st_mode) != 0o600
            ):
                raise HostedPropertyTourManifestError("hosted_property_tour_manifest_verification_failed")
            os.fsync(directory_fd)
            for backup_name in (public_backup, private_backup):
                if not backup_name:
                    continue
                try:
                    os.unlink(backup_name, dir_fd=directory_fd)
                    temporary_names.discard(backup_name)
                except OSError:
                    pass
        except Exception as exc:
            rollback_failed = False
            for final_name, backup_name, committed_stat in reversed(committed):
                try:
                    observed = os.stat(final_name, dir_fd=directory_fd, follow_symlinks=False)
                    if (
                        not stat.S_ISREG(observed.st_mode)
                        or observed.st_dev != committed_stat.st_dev
                        or observed.st_ino != committed_stat.st_ino
                    ):
                        raise OSError("committed_manifest_changed")
                    if backup_name:
                        os.replace(
                            backup_name,
                            final_name,
                            src_dir_fd=directory_fd,
                            dst_dir_fd=directory_fd,
                        )
                        temporary_names.discard(backup_name)
                    else:
                        os.unlink(final_name, dir_fd=directory_fd)
                except OSError:
                    rollback_failed = True
            try:
                os.fsync(directory_fd)
            except OSError:
                rollback_failed = True
            if rollback_failed:
                raise HostedPropertyTourManifestError(
                    "hosted_property_tour_manifest_rollback_failed"
                ) from exc
            if isinstance(exc, HostedPropertyTourManifestError):
                raise
            raise HostedPropertyTourManifestError(
                "hosted_property_tour_manifest_write_failed"
            ) from exc
    finally:
        for temporary_name in tuple(temporary_names):
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except OSError:
                pass
        os.close(directory_fd)


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
    *,
    expected_target: os.stat_result | None | object = _HOSTED_PROPERTY_TOUR_TARGET_UNSET,
) -> None:
    private_name = _PROPERTY_PUBLIC_TOUR_PRIVATE_MANIFEST
    encoded = json.dumps(private_payload, ensure_ascii=False, indent=2).encode("utf-8")
    if not encoded or len(encoded) > _hosted_property_tour_manifest_max_bytes():
        raise RuntimeError("hosted_property_tour_private_receipt_too_large")
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
        if expected_target is not _HOSTED_PROPERTY_TOUR_TARGET_UNSET:
            if (
                expected_target is None
                or existing_stat is None
                or not isinstance(expected_target, os.stat_result)
                or not _hosted_property_tour_stat_unchanged(
                    expected_target,
                    existing_stat,
                )
            ):
                raise RuntimeError("hosted_property_tour_private_receipt_target_changed")
        if existing_stat is not None and (
            stat.S_ISLNK(existing_stat.st_mode)
            or not stat.S_ISREG(existing_stat.st_mode)
            or existing_stat.st_size <= 0
            or existing_stat.st_size > _hosted_property_tour_manifest_max_bytes()
        ):
            raise RuntimeError("hosted_property_tour_private_receipt_invalid")

        temporary_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        temporary_flags |= getattr(os, "O_NOFOLLOW", 0)
        temporary_fd = os.open(temporary_name, temporary_flags, 0o600, dir_fd=directory_fd)
        temporary_created = True
        os.fchmod(temporary_fd, 0o600)
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
        if (existing_stat is None) != (replacement_stat is None) or (
            existing_stat is not None
            and replacement_stat is not None
            and not _hosted_property_tour_stat_unchanged(
                existing_stat,
                replacement_stat,
            )
        ):
            raise RuntimeError("hosted_property_tour_private_receipt_target_changed")
        if replacement_stat is not None and (
            stat.S_ISLNK(replacement_stat.st_mode)
            or not stat.S_ISREG(replacement_stat.st_mode)
            or replacement_stat.st_size <= 0
            or replacement_stat.st_size > _hosted_property_tour_manifest_max_bytes()
        ):
            raise RuntimeError("hosted_property_tour_private_receipt_invalid")
        os.replace(
            temporary_name,
            private_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        replaced = True
        final_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        final_fd = os.open(private_name, final_flags, dir_fd=directory_fd)
        final_stat = os.fstat(final_fd)
        if (
            not stat.S_ISREG(final_stat.st_mode)
            or final_stat.st_dev != temporary_stat.st_dev
            or final_stat.st_ino != temporary_stat.st_ino
            or final_stat.st_size != len(encoded)
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


def _write_hosted_property_tour_payload_with_slug_lock_held(
    bundle_dir: Path,
    payload: dict[str, object],
) -> None:
    _assert_governed_property_tour_slug_not_reserved(bundle_dir.name)
    _assert_governed_property_tour_slug_not_reserved(payload.get("slug"))
    incoming_owner = str(payload.get("principal_id") or "").strip()
    search_run_id = str(payload.get("search_run_id") or "").strip()
    existing_any = False
    existing_private: dict[str, object] = {}
    try:
        directory_fd = _open_hosted_property_tour_directory(bundle_dir)
    except HostedPropertyTourManifestMissing:
        directory_fd = -1
    except HostedPropertyTourManifestError as exc:
        raise RuntimeError("hosted_property_tour_bundle_invalid") from exc
    if directory_fd >= 0:
        try:
            existing_public_result = _read_hosted_property_tour_regular_file_at(
                directory_fd,
                _PROPERTY_PUBLIC_TOUR_MANIFEST,
                missing_ok=True,
            )
        except HostedPropertyTourManifestError as exc:
            raise RuntimeError("hosted_property_tour_manifest_invalid") from exc
        try:
            existing_private_result = _read_hosted_property_tour_regular_file_at(
                directory_fd,
                _PROPERTY_PUBLIC_TOUR_PRIVATE_MANIFEST,
                missing_ok=True,
            )
        except HostedPropertyTourManifestError as exc:
            raise RuntimeError("hosted_property_tour_private_receipt_invalid") from exc
        try:
            existing_any = existing_public_result is not None or existing_private_result is not None
            if existing_private_result is not None:
                try:
                    loaded_private = json.loads(existing_private_result[0].decode("utf-8"))
                except (UnicodeError, ValueError) as exc:
                    raise HostedPropertyTourManifestError(
                        "hosted_property_tour_private_receipt_invalid"
                    ) from exc
                if not isinstance(loaded_private, dict):
                    raise HostedPropertyTourManifestError(
                        "hosted_property_tour_private_receipt_invalid"
                    )
                existing_private = dict(loaded_private)
        except HostedPropertyTourManifestError as exc:
            raise RuntimeError("hosted_property_tour_private_receipt_invalid") from exc
        finally:
            os.close(directory_fd)
    if existing_any:
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
        _write_hosted_property_tour_manifests_atomic(
            bundle_dir,
            public_payload=public_payload,
            private_payload=private_payload,
        )

    if incoming_owner:
        with property_account_publication_authority(
            incoming_owner,
            run_id=search_run_id,
        ):
            _commit_payload()
    else:
        _commit_payload()


def _write_hosted_property_tour_payload(
    bundle_dir: Path,
    payload: dict[str, object],
) -> None:
    _assert_governed_property_tour_slug_not_reserved(bundle_dir.name)
    _assert_governed_property_tour_slug_not_reserved(payload.get("slug"))
    with _hosted_property_tour_publication_lock(
        public_dir=bundle_dir.parent,
        slug=bundle_dir.name,
    ):
        _write_hosted_property_tour_payload_with_slug_lock_held(
            bundle_dir,
            payload,
        )


def _load_hosted_property_tour_private_receipt(bundle_dir: Path) -> dict[str, object]:
    try:
        private_payload = _read_hosted_property_tour_json_file(
            bundle_dir,
            _PROPERTY_PUBLIC_TOUR_PRIVATE_MANIFEST,
            missing_ok=True,
        )
    except HostedPropertyTourManifestError:
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


def _load_hosted_property_tour_payload_with_slug_lock_held(
    bundle_dir: Path,
    *,
    principal_id: str,
) -> dict[str, object]:
    try:
        payload = _read_hosted_property_tour_json_file(
            bundle_dir,
            _PROPERTY_PUBLIC_TOUR_MANIFEST,
        )
    except HostedPropertyTourManifestError:
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


def _load_hosted_property_tour_payload(bundle_dir: Path, *, principal_id: str = "") -> dict[str, object]:
    requested_principal = str(principal_id or "").strip()
    if not requested_principal:
        return _load_hosted_property_tour_payload_with_slug_lock_held(
            bundle_dir,
            principal_id="",
        )
    with _hosted_property_tour_publication_lock(
        public_dir=bundle_dir.parent,
        slug=bundle_dir.name,
    ):
        return _load_hosted_property_tour_payload_with_slug_lock_held(
            bundle_dir,
            principal_id=requested_principal,
        )


def _owned_hosted_property_tour_binding_identity(
    tour_url: object,
    *,
    principal_id: object,
) -> dict[str, object]:
    """Return only an owner-verified source identity for an exact hosted bundle."""

    normalized_url = str(tour_url or "").strip()
    requested_principal = str(principal_id or "").strip()
    slug = _hosted_property_tour_slug_from_url(normalized_url)
    if (
        not normalized_url
        or not requested_principal
        or not slug
        or slug in {".", ".."}
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", slug)
    ):
        return {}
    public_dir = _public_tour_dir()
    bundle_dir = public_dir / slug
    with _hosted_property_tour_publication_lock(public_dir=public_dir, slug=slug):
        public_payload = _load_hosted_property_tour_payload_with_slug_lock_held(
            bundle_dir,
            principal_id="",
        )
        private_payload = _owned_hosted_property_tour_private_receipt(
            bundle_dir,
            principal_id=requested_principal,
        )
    if not public_payload or not private_payload:
        return {}
    from app.product.property_search_tour_binding import (
        canonical_property_source_url,
        property_search_source_url_sha256,
    )

    canonical_property_urls: set[str] = set()
    for key in ("property_url", "listing_url"):
        raw_property_url = str(private_payload.get(key) or "").strip()
        if not raw_property_url:
            continue
        canonical_property_url = canonical_property_source_url(raw_property_url)
        if not canonical_property_url:
            return {}
        canonical_property_urls.add(canonical_property_url)
    if len(canonical_property_urls) != 1:
        return {}
    property_url_sha256 = property_search_source_url_sha256(
        next(iter(canonical_property_urls))
    )
    declared_property_url_sha256 = str(
        public_payload.get("property_url_sha256") or ""
    ).strip().lower()
    if (
        not property_url_sha256
        or (
            declared_property_url_sha256
            and (
                not re.fullmatch(r"[0-9a-f]{64}", declared_property_url_sha256)
                or not hmac.compare_digest(
                    declared_property_url_sha256,
                    property_url_sha256,
                )
            )
        )
    ):
        return {}
    return {
        "owner_verified": True,
        "slug": slug,
        "search_run_id": str(private_payload.get("search_run_id") or "").strip(),
        "candidate_ref": str(private_payload.get("candidate_ref") or "").strip(),
        "listing_url": str(private_payload.get("listing_url") or "").strip(),
        "property_url": str(private_payload.get("property_url") or "").strip(),
        "source_ref": str(private_payload.get("source_ref") or "").strip(),
        "external_id": str(private_payload.get("external_id") or "").strip(),
        "property_url_sha256": property_url_sha256,
    }


def _public_hosted_property_tour_live_source_url(bundle_dir: Path) -> str:
    """Return only the provider URL intentionally exposed by a public share."""

    private_payload = _load_hosted_property_tour_private_receipt(bundle_dir)
    live_url = _safe_live_property_tour_url(
        private_payload.get("source_virtual_tour_url")
        or private_payload.get("source_virtual_tour_origin")
    )
    if (
        _property_tour_provider_host_kind(live_url) not in _CUSTOMER_FACING_TOUR_PROVIDERS
        or not _property_tour_provider_url_shape_valid(live_url)
    ):
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
    if governed_prater_slug_reserved(normalized_slug):
        return {
            "status": "governed_tour_reserved",
            "slug": normalized_slug,
            "provider": normalized_provider,
        }
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
        with _hosted_property_tour_publication_lock(
            public_dir=root,
            slug=normalized_slug,
        ):
            try:
                _read_hosted_property_tour_json_file(
                    bundle_dir,
                    _PROPERTY_PUBLIC_TOUR_MANIFEST,
                )
                private_result = _read_hosted_property_tour_json_file_with_stat(
                    bundle_dir,
                    _PROPERTY_PUBLIC_TOUR_PRIVATE_MANIFEST,
                    missing_ok=True,
                )
            except HostedPropertyTourManifestError:
                continue
            if private_result is None:
                continue
            private_payload, private_stat = private_result
            owner_principal = str(private_payload.get("principal_id") or "").strip()
            search_run_id = str(private_payload.get("search_run_id") or "").strip()
            private_manifest_path = _public_tour_private_manifest_path(bundle_dir)
            private_payload["three_d_vista_browser_render_proof"] = proof_payload

            def _commit_current_private_receipt() -> bool:
                try:
                    _write_hosted_property_tour_private_receipt_atomic(
                        bundle_dir,
                        private_payload,
                        expected_target=private_stat,
                    )
                except RuntimeError:
                    return False
                return True

            if owner_principal:
                with property_account_publication_authority(
                    owner_principal,
                    run_id=search_run_id,
                ):
                    updated = _commit_current_private_receipt()
            else:
                # Legacy imported exports predate owner receipts.  Keep their
                # browser proof lane compatible, but only as an inode-bound CAS
                # while holding the shared slug lock; it cannot overwrite a
                # newer private manifest or cross a paired publication.
                updated = _commit_current_private_receipt()
            if not updated:
                continue
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
    """Join the generated publisher's exact per-slug filesystem lock.

    Generated publication already owns this lock while it calls the shared
    hosted writer and private-aware loader.  Track ownership per thread so
    those internal calls are reentrant without reopening and self-deadlocking
    the same flock.  Every outermost path still uses the single service-owned
    lock inode.
    """

    try:
        normalized_public_dir = str(Path(public_dir).expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        normalized_public_dir = os.path.abspath(os.fspath(Path(public_dir).expanduser()))
    normalized_slug = str(slug or "").strip()
    lock_key = (normalized_public_dir, normalized_slug)
    held_keys = getattr(
        _HOSTED_PROPERTY_TOUR_PUBLICATION_LOCK_STATE,
        "held_keys",
        None,
    )
    if held_keys is None:
        held_keys = set()
        _HOSTED_PROPERTY_TOUR_PUBLICATION_LOCK_STATE.held_keys = held_keys
    if lock_key in held_keys:
        yield
        return

    # service owns the dependency-neutral lock implementation today and also
    # imports this hosting module. Import lazily so both paths share one lock
    # derivation and inode without creating a module-import cycle.
    from app.product.service import _property_reconstruction_publication_lock

    with _property_reconstruction_publication_lock(
        public_dir=Path(normalized_public_dir),
        slug=normalized_slug,
    ):
        held_keys.add(lock_key)
        try:
            yield
        finally:
            held_keys.discard(lock_key)


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
    if governed_prater_slug_reserved(normalized_slug):
        return {"status": "governed_tour_reserved", "slug": normalized_slug}
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
    """Return hosts from syntactically valid first-party public base URLs.

    App bases own the origin root while tour bases own the ``/tours`` mount.
    Treating either value as a loose hostname allowlist would let malformed
    deployment configuration silently widen local-bundle lookup authority.
    """

    hosts: list[str] = []
    for environment_name, expected_path in (
        ("PROPERTYQUARRY_PUBLIC_TOUR_BASE_URL", "/tours"),
        ("PROPERTYQUARRY_PUBLIC_BASE_URL", "/"),
    ):
        raw = str(os.getenv(environment_name) or "").strip()
        if not raw:
            continue
        try:
            parsed = urllib.parse.urlsplit(raw)
            port = parsed.port
        except (TypeError, ValueError):
            continue
        raw_path = str(parsed.path or "")
        normalized_path = raw_path.rstrip("/") or "/"
        if (
            parsed.scheme.lower() != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or port not in {None, 443}
            or parsed.query
            or parsed.fragment
            or "%" in raw_path
            or "\\" in raw_path
            or normalized_path != expected_path
        ):
            continue
        host = str(parsed.hostname or "").strip().lower().rstrip(".")
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
    branded_domains = ("propertyquarry.com",)
    branded_host = any(host == domain or host.endswith(f".{domain}") for domain in branded_domains)
    if branded_host and str(parsed.path or "").startswith("/tours/"):
        return not _is_crezlo_tour_host(normalized)
    return False

def _resolve_property_tour_urls(
    structured_output: dict[str, object],
    *,
    allow_unverified_branded: bool = False,
    principal_id: object = "",
) -> tuple[str, str]:
    hosted_url = _first_non_empty_text(structured_output.get("hosted_url"))
    public_url = _first_non_empty_text(structured_output.get("public_url"))
    share_url = _first_non_empty_text(structured_output.get("share_url"))
    crezlo_public_url = _first_non_empty_text(structured_output.get("crezlo_public_url"))
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
    verified_provider = ""
    normalized_principal = str(principal_id or "").strip()
    for candidate in branded_candidates:
        candidate_provider = (
            _hosted_property_tour_verified_provider(
                candidate,
                principal_id=normalized_principal,
            )
            if normalized_principal
            else _hosted_property_tour_verified_provider(candidate)
        )
        if candidate_provider in _CUSTOMER_FACING_TOUR_PROVIDERS:
            branded_tour_url = candidate
            verified_provider = candidate_provider
            break
        if allow_unverified_branded:
            candidate_payload = _hosted_property_tour_payload_for_url(candidate)
            if (
                candidate_payload
                and not _property_tour_payload_is_disabled_fallback(candidate_payload)
                and _hosted_property_tour_generated_reconstruction_open_url(candidate)
            ):
                branded_tour_url = candidate
                break
    vendor_tour_url = ""
    if branded_tour_url and verified_provider:
        verified_payload = _hosted_property_tour_payload_for_url(
            branded_tour_url,
            principal_id=normalized_principal,
        )
        for key in (
            "three_d_vista_url",
            "threedvista_url",
            "3dvista_url",
            "source_virtual_tour_url",
            "crezlo_public_url",
        ):
            candidate = str(verified_payload.get(key) or "").strip()
            if (
                _property_tour_provider_host_kind(candidate) == verified_provider
                and _property_tour_provider_url_shape_valid(candidate)
            ):
                vendor_tour_url = candidate
                break
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


def _hosted_property_tour_reference_parts(
    value: object,
) -> tuple[urllib.parse.SplitResult, str] | None:
    """Validate a local public-tour reference before deriving a bundle slug.

    A path that merely *looks* like a PropertyQuarry route must not grant an
    arbitrary origin access to a same-named local bundle.  Keeping this check
    at the shared slug boundary protects every manifest/asset helper that
    resolves ``/tours/<slug>`` into ``EA_PUBLIC_TOUR_DIR``.
    """

    normalized = str(value or "").strip()
    if not normalized:
        return None
    try:
        parsed = urllib.parse.urlsplit(normalized)
        port = parsed.port
    except (TypeError, ValueError):
        return None

    if parsed.scheme or parsed.netloc:
        if (
            parsed.scheme.lower() != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or port not in {None, 443}
            or not _is_branded_public_tour_url(normalized)
        ):
            return None
    elif not str(parsed.path or "").startswith("/tours/"):
        return None

    raw_path = str(parsed.path or "")
    # Public tour routes do not need alternate path encodings. Rejecting them
    # closes encoded separators, encoded dot segments, and multi-decode aliases
    # before a filesystem slug is selected.
    if "%" in raw_path or "\\" in raw_path or "\x00" in raw_path:
        return None
    path = raw_path.rstrip("/")
    if not path.startswith("/tours/"):
        return None
    path_parts = path[1:].split("/")
    if (
        len(path_parts) < 2
        or path_parts[0] != "tours"
        or any(not part or part in {".", ".."} for part in path_parts)
    ):
        return None
    slug_index = 2 if path_parts[1] == "files" else 1
    if slug_index >= len(path_parts):
        return None
    slug = str(path_parts[slug_index] or "").strip()
    if not _PROPERTY_PUBLIC_TOUR_SLUG_RE.fullmatch(slug):
        return None
    return parsed, slug


def _hosted_property_tour_slug_from_url(value: object) -> str:
    reference = _hosted_property_tour_reference_parts(value)
    return reference[1] if reference is not None else ""


def _hosted_property_tour_payload_for_url(
    tour_url: object,
    *,
    principal_id: object = "",
) -> dict[str, object]:
    normalized_url = str(tour_url or "").strip()
    if not normalized_url:
        return {}
    reference = _hosted_property_tour_reference_parts(normalized_url)
    if reference is None:
        return {}
    _parsed, slug = reference
    payload = _load_hosted_property_tour_payload(
        _public_tour_dir() / slug,
        principal_id=str(principal_id or "").strip(),
    )
    return dict(payload) if isinstance(payload, dict) else {}


def _hosted_property_tour_control_url(tour_url: object, *, viewer: str = "") -> str:
    normalized = str(tour_url or "").strip()
    reference = _hosted_property_tour_reference_parts(normalized)
    if reference is None:
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
        parsed, _slug = reference
        path = str(parsed.path or "").rstrip("/")
        if any(path.endswith(f"/control/{mode}") for mode in ("matterport", "3dvista", "pano2vr", "krpano")):
            path = path.rsplit("/control/", 1)[0]
        elif path.endswith("/control"):
            path = path[: -len("/control")]
        path = f"{path}/control/{viewer_slug}" if viewer_slug else f"{path}/control"
        return urllib.parse.urlunsplit(parsed._replace(path=path, query="", fragment=""))
    except (TypeError, ValueError):
        return ""


def _hosted_property_tour_has_matterport_export(tour_url: object) -> bool:
    payload = _hosted_property_tour_payload_for_url(tour_url)
    for key in ("matterport_url", "source_virtual_tour_url", "crezlo_public_url"):
        value = str(payload.get(key) or "").strip()
        if (
            value
            and _property_tour_provider_host_kind(value) == "matterport"
            and _property_tour_provider_url_shape_valid(value)
        ):
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
    candidates = [walkable_scene]
    raw_scenes = walkable_scene.get("scenes")
    if isinstance(raw_scenes, dict):
        candidates.extend(value for value in raw_scenes.values() if isinstance(value, dict))
    elif isinstance(raw_scenes, list):
        candidates.extend(value for value in raw_scenes if isinstance(value, dict))
    for candidate in candidates:
        projection = str(candidate.get("projection") or candidate.get("type") or "").strip().lower()
        if projection and projection not in {"equirectangular", "equirect", "panorama", "spherical", "360", "cubemap", "cube"}:
            continue
        for key in ("panorama_relpath", "equirect_relpath", "image_relpath", "asset_relpath"):
            relpath = str(candidate.get(key) or "").strip()
            if relpath and _hosted_property_tour_is_equirectangular_image(bundle_dir, relpath):
                return True
        cube_faces = candidate.get("cube_faces")
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
        if len(valid_faces) >= 6:
            return True
    return False


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
    walkable_scene = payload.get("walkable_scene")
    representation_kind = (
        str(walkable_scene.get("representation_kind") or "").strip().lower()
        if isinstance(walkable_scene, dict)
        else ""
    )
    if representation_kind == "ai_reconstruction":
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
    if (
        _property_tour_provider_host_kind(normalized_url) == provider
        and _property_tour_provider_url_shape_valid(normalized_url)
    ):
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


def _hosted_property_tour_file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _hosted_property_tour_public_asset_relpath(value: object) -> str:
    """Normalize only paths that the public asset route can serve verbatim."""

    raw = str(value or "").strip().replace("\\", "/")
    if (
        not raw
        or "\x00" in raw
        or "://" in raw
        or raw.startswith("/")
        or any(character in ":?#[]@!$&'()*+,;=%" for character in raw)
    ):
        return ""
    path = PurePosixPath(raw)
    if path.is_absolute() or any(
        part in {"", ".", ".."} or part.startswith(".")
        for part in path.parts
    ):
        return ""
    return "/".join(path.parts)


def _hosted_property_tour_ai_panorama_core_manifest_sha256(
    payload: Mapping[str, object],
) -> str:
    """Hash the complete functional AI-tour manifest without its proof loop."""

    core_payload = dict(payload)
    walkable_scene = core_payload.get("walkable_scene")
    if isinstance(walkable_scene, dict):
        core_walkable_scene = dict(walkable_scene)
        acceptance = core_walkable_scene.get("acceptance")
        if isinstance(acceptance, dict):
            core_walkable_scene["acceptance"] = {
                str(key): value
                for key, value in acceptance.items()
                if str(key)
                not in _AI_PANORAMA_CORE_MANIFEST_EXCLUDED_ACCEPTANCE_FIELDS
            }
        core_payload["walkable_scene"] = core_walkable_scene
    try:
        canonical = json.dumps(
            core_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return ""
    return hashlib.sha256(canonical).hexdigest()


def _hosted_property_tour_ai_panorama_contract(
    *,
    bundle_dir: Path,
    payload: dict[str, object],
    mode: str = "full",
) -> dict[str, object]:
    """Validate a disclosed, immutable, browser-proven AI panorama bundle.

    Passing this contract means the first-party spherical viewer is usable. It
    never upgrades the reconstruction into a captured or measured property tour.
    """

    validation_mode = str(mode or "").strip().lower()

    def _blocked(reason: str) -> dict[str, object]:
        result: dict[str, object] = {
            "ready": False,
            "representation_kind": "ai_panorama_360",
            "reason": reason,
        }
        if validation_mode == "preflight":
            result.update(
                {
                    "preflight": True,
                    "preflight_ready": False,
                    "proof_pending": False,
                }
            )
        return result

    if validation_mode not in {"full", "preflight"}:
        return _blocked("validation_mode_invalid")

    if payload.get("publication_status") != "ready":
        return _blocked("publication_not_ready")
    walkable_scene = payload.get("walkable_scene")
    if not isinstance(walkable_scene, dict):
        return _blocked("walkable_scene_missing")
    if str(walkable_scene.get("representation_kind") or "").strip().lower() != "ai_reconstruction":
        return _blocked("representation_kind_invalid")
    disclosure = str(walkable_scene.get("representation_disclosure") or "").strip()
    if disclosure != _AI_PANORAMA_CANONICAL_DISCLOSURE:
        return _blocked("representation_disclosure_missing")

    raw_scenes = walkable_scene.get("scenes")
    if isinstance(raw_scenes, dict):
        scene_rows = [
            (str(key or "").strip(), dict(value))
            for key, value in raw_scenes.items()
            if isinstance(value, dict)
        ]
    elif isinstance(raw_scenes, list):
        scene_rows = [
            (str(index + 1), dict(value))
            for index, value in enumerate(raw_scenes)
            if isinstance(value, dict)
        ]
    else:
        scene_rows = []
    try:
        expected_scene_count = int(
            walkable_scene.get("expected_scene_count") or len(scene_rows)
        )
    except (TypeError, ValueError):
        return _blocked("expected_scene_count_invalid")
    if (
        len(scene_rows) < 3
        or expected_scene_count < 3
        or len(scene_rows) != expected_scene_count
    ):
        return _blocked("scene_coverage_incomplete")

    acceptance = walkable_scene.get("acceptance")
    if not isinstance(acceptance, dict):
        return _blocked("acceptance_missing")
    proof_status = str(acceptance.get("proof_status") or "").strip().lower()
    if acceptance.get("contract_name") != "propertyquarry.ai_panorama_acceptance.v1":
        return _blocked("acceptance_invalid")
    if validation_mode == "full" and proof_status != "pass":
        return _blocked("acceptance_invalid")
    if validation_mode == "preflight" and proof_status not in {"pending", "pass"}:
        return _blocked("acceptance_invalid")
    digest_pattern = re.compile(r"[0-9a-f]{64}")
    property_binding_sha256 = str(
        acceptance.get("property_url_sha256") or ""
    ).strip().lower()
    if (
        not digest_pattern.fullmatch(property_binding_sha256)
        or str(payload.get("property_url_sha256") or "").strip().lower()
        != property_binding_sha256
    ):
        return _blocked("property_binding_invalid")
    declared_asset_hashes = acceptance.get("panorama_asset_sha256")
    if not isinstance(declared_asset_hashes, dict):
        return _blocked("panorama_asset_hashes_missing")

    scene_ids: list[str] = []
    actual_asset_hashes: dict[str, str] = {}
    edges: dict[str, set[str]] = {}
    total_panorama_bytes = 0
    largest_panorama_bytes = 0
    for fallback_id, scene in scene_rows:
        scene_id = str(
            scene.get("id")
            or scene.get("node_id")
            or scene.get("scene_id")
            or fallback_id
        ).strip()
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", scene_id)
            or scene_id in scene_ids
        ):
            return _blocked("scene_id_invalid")
        projection = str(
            scene.get("projection") or scene.get("type") or ""
        ).strip().lower()
        if projection not in {
            "equirectangular",
            "equirect",
            "panorama",
            "spherical",
            "360",
        }:
            return _blocked("scene_projection_invalid")
        raw_asset_relpaths = [
            str(scene.get(key) or "").strip()
            for key in (
                "asset_relpath",
                "panorama_relpath",
                "equirect_relpath",
                "image_relpath",
            )
            if str(scene.get(key) or "").strip()
        ]
        canonical_asset_relpaths = {
            _hosted_property_tour_public_asset_relpath(value)
            for value in raw_asset_relpaths
        }
        if "" in canonical_asset_relpaths:
            return _blocked("scene_asset_relpath_invalid")
        if len(canonical_asset_relpaths) != 1:
            return _blocked("scene_asset_relpath_ambiguous")
        relpath = next(iter(canonical_asset_relpaths))
        asset_path = _hosted_property_tour_asset_path(bundle_dir, relpath)
        if asset_path is None:
            return _blocked("scene_asset_missing")
        width, height = _hosted_property_tour_image_dimensions(asset_path)
        ratio = width / height if height else 0.0
        if width < 4096 or height < 2048 or not 1.98 <= ratio <= 2.02:
            return _blocked("scene_asset_not_release_resolution")
        asset_sha256 = _hosted_property_tour_file_sha256(asset_path)
        if (
            not digest_pattern.fullmatch(asset_sha256)
            or str(declared_asset_hashes.get(scene_id) or "").strip().lower()
            != asset_sha256
        ):
            return _blocked("scene_asset_hash_mismatch")
        asset_size_bytes = int(asset_path.stat(follow_symlinks=False).st_size)
        total_panorama_bytes += asset_size_bytes
        largest_panorama_bytes = max(largest_panorama_bytes, asset_size_bytes)
        try:
            floorplan_x = float(scene.get("floorplan_x_pct"))
            floorplan_y = float(scene.get("floorplan_y_pct"))
        except (TypeError, ValueError):
            return _blocked("floorplan_alignment_missing")
        if not (0.0 <= floorplan_x <= 100.0 and 0.0 <= floorplan_y <= 100.0):
            return _blocked("floorplan_alignment_invalid")
        scene_ids.append(scene_id)
        actual_asset_hashes[scene_id] = asset_sha256
        edges[scene_id] = set()
    if len(set(actual_asset_hashes.values())) != len(scene_ids):
        return _blocked("duplicate_panorama_assets")
    if total_panorama_bytes > 12_000_000 or largest_panorama_bytes > 2_500_000:
        return _blocked("panorama_performance_budget_exceeded")

    scene_id_set = set(scene_ids)
    hotspot_count = 0
    hotspot_edges: set[str] = set()
    for fallback_id, scene in scene_rows:
        scene_id = str(
            scene.get("id")
            or scene.get("node_id")
            or scene.get("scene_id")
            or fallback_id
        ).strip()
        for key in ("hotspots", "transitions", "links"):
            raw_hotspots = scene.get(key)
            if not isinstance(raw_hotspots, list):
                continue
            for hotspot in raw_hotspots:
                if not isinstance(hotspot, dict):
                    continue
                target = _first_non_empty_text(
                    hotspot.get("target"),
                    hotspot.get("target_scene_id"),
                    hotspot.get("target_node_id"),
                    hotspot.get("target_scene"),
                    hotspot.get("scene"),
                )
                if target in scene_id_set and target != scene_id:
                    edges[scene_id].add(target)
                    hotspot_edges.add(f"{scene_id}->{target}")
                    hotspot_count += 1
    if hotspot_count < len(scene_ids) - 1:
        return _blocked("navigation_hotspots_incomplete")
    initial_scene_id = str(
        walkable_scene.get("initial_scene_id") or scene_ids[0]
    ).strip()
    if initial_scene_id not in scene_id_set:
        return _blocked("initial_scene_invalid")
    reachable = {initial_scene_id}
    frontier = [initial_scene_id]
    while frontier:
        current = frontier.pop()
        for target in edges.get(current, set()):
            if target not in reachable:
                reachable.add(target)
                frontier.append(target)
    if reachable != scene_id_set:
        return _blocked("scene_graph_disconnected")

    spatial_model = walkable_scene.get("spatial_model")
    if not isinstance(spatial_model, dict):
        return _blocked("spatial_model_missing")
    if (
        str(spatial_model.get("source_basis") or "").strip().lower()
        != "floorplan_scaled_approximation"
        or spatial_model.get("measured") is not False
    ):
        return _blocked("spatial_model_provenance_invalid")
    spatial_rooms = spatial_model.get("rooms")
    if not isinstance(spatial_rooms, list) or len(spatial_rooms) < len(scene_ids):
        return _blocked("spatial_model_coverage_incomplete")
    spatial_room_ids: set[str] = set()
    spatial_scene_ids: list[str] = []
    for raw_room in spatial_rooms:
        if not isinstance(raw_room, dict):
            return _blocked("spatial_room_invalid")
        room_id = str(raw_room.get("id") or "").strip()
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", room_id)
            or room_id in spatial_room_ids
        ):
            return _blocked("spatial_room_id_invalid")
        try:
            room_values = tuple(
                float(raw_room.get(key))
                for key in ("x", "z", "width", "depth", "height")
            )
        except (TypeError, ValueError):
            return _blocked("spatial_room_geometry_invalid")
        x, z, width, depth, height = room_values
        if (
            not all(math.isfinite(value) for value in room_values)
            or not (-40.0 <= x <= 40.0 and -40.0 <= z <= 40.0)
            or not (0.2 <= width <= 40.0 and 0.2 <= depth <= 40.0)
            or not (1.8 <= height <= 6.0)
        ):
            return _blocked("spatial_room_geometry_invalid")
        kind = str(raw_room.get("kind") or "interior").strip().lower()
        if kind not in {"interior", "exterior", "unavailable"}:
            return _blocked("spatial_room_kind_invalid")
        scene_id = str(raw_room.get("scene_id") or "").strip()
        if scene_id:
            if scene_id not in scene_id_set or scene_id in spatial_scene_ids:
                return _blocked("spatial_scene_binding_invalid")
            spatial_scene_ids.append(scene_id)
        elif kind != "unavailable":
            return _blocked("spatial_scene_binding_missing")
        spatial_room_ids.add(room_id)
    if set(spatial_scene_ids) != scene_id_set:
        return _blocked("spatial_model_coverage_incomplete")

    floorplan_relpath = _hosted_property_tour_public_asset_relpath(
        walkable_scene.get("floorplan_relpath")
    )
    if not floorplan_relpath:
        return _blocked("floorplan_relpath_invalid")
    floorplan_path = _hosted_property_tour_asset_path(bundle_dir, floorplan_relpath)
    if floorplan_path is None:
        return _blocked("floorplan_missing")
    floorplan_width, floorplan_height = _hosted_property_tour_image_dimensions(
        floorplan_path
    )
    if floorplan_width < 800 or floorplan_height < 800:
        return _blocked("floorplan_resolution_invalid")

    def _verified_json_receipt(
        *,
        relpath_key: str,
        digest_key: str,
        contract_name: str,
    ) -> dict[str, object] | None:
        relpath = str(acceptance.get(relpath_key) or "").strip()
        expected_sha256 = str(acceptance.get(digest_key) or "").strip().lower()
        receipt_path = _hosted_property_tour_asset_path(bundle_dir, relpath)
        if (
            receipt_path is None
            or not digest_pattern.fullmatch(expected_sha256)
            or _hosted_property_tour_file_sha256(receipt_path) != expected_sha256
        ):
            return None
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if (
            not isinstance(receipt, dict)
            or receipt.get("contract_name") != contract_name
        ):
            return None
        return dict(receipt)

    provenance = _verified_json_receipt(
        relpath_key="provenance_relpath",
        digest_key="provenance_sha256",
        contract_name="propertyquarry.ai_panorama_provenance.v1",
    )
    if provenance is None:
        return _blocked("provenance_invalid")
    source_hashes = provenance.get("source_image_sha256")
    if (
        provenance.get("generation_method") != "ai_image_reconstruction"
        or provenance.get("captured_360") is not False
        or provenance.get("measured_survey") is not False
        or str(provenance.get("representation_disclosure") or "").strip()
        != disclosure
        or str(provenance.get("property_url_sha256") or "").strip().lower()
        != property_binding_sha256
        or not isinstance(source_hashes, list)
        or len(source_hashes) < len(scene_ids)
        or any(
            not digest_pattern.fullmatch(str(value or "").strip().lower())
            for value in source_hashes
        )
        or str(provenance.get("floorplan_sha256") or "").strip().lower()
        != _hosted_property_tour_file_sha256(floorplan_path)
        or dict(provenance.get("panorama_asset_sha256") or {})
        != actual_asset_hashes
        or provenance.get("spatial_model_basis")
        != "floorplan_scaled_approximation"
        or provenance.get("spatial_model_measured") is not False
        or set(provenance.get("spatial_scene_ids") or []) != scene_id_set
    ):
        return _blocked("provenance_content_invalid")

    core_manifest_sha256 = (
        _hosted_property_tour_ai_panorama_core_manifest_sha256(payload)
    )
    if not digest_pattern.fullmatch(core_manifest_sha256):
        return _blocked("core_manifest_invalid")

    common_result: dict[str, object] = {
        "representation_kind": "ai_panorama_360",
        "representation_disclosure": disclosure,
        "scene_count": len(scene_ids),
        "scene_ids": scene_ids,
        "initial_scene_id": initial_scene_id,
        "hotspot_count": hotspot_count,
        "spatial_room_count": len(spatial_rooms),
        "floorplan_relpath": floorplan_relpath,
        "total_panorama_bytes": total_panorama_bytes,
        "largest_panorama_bytes": largest_panorama_bytes,
        "property_url_sha256": property_binding_sha256,
        "panorama_asset_sha256": actual_asset_hashes,
        "core_manifest_sha256": core_manifest_sha256,
    }
    if validation_mode == "preflight":
        return {
            "ready": False,
            "preflight": True,
            "preflight_ready": True,
            "proof_pending": True,
            **common_result,
            "reason": "browser_proof_pending",
        }

    browser_receipt = _verified_json_receipt(
        relpath_key="browser_receipt_relpath",
        digest_key="browser_receipt_sha256",
        contract_name="propertyquarry.ai_panorama_browser_proof.v1",
    )
    slug = str(payload.get("slug") or "").strip()
    expected_control_path = (
        f"/tours/{urllib.parse.quote(slug, safe='')}/control"
    )
    required_browser_checks = (
        "anonymous_http_200",
        "drag_navigation_verified",
        "scene_navigation_verified",
        "all_hotspots_verified",
        "dollhouse_verified",
        "desktop_verified",
        "mobile_verified",
        "touch_verified",
        "first_party_viewer_verified",
        "first_party_renderer_verified",
        "slow_network_verified",
        "performance_budget_verified",
        "disclosure_verified",
    )
    browser_scene_ids = (
        browser_receipt.get("scene_ids")
        if isinstance(browser_receipt, dict)
        and isinstance(browser_receipt.get("scene_ids"), list)
        else []
    )
    browser_observed_at_valid = (
        _hosted_property_tour_ai_panorama_browser_proof_current(browser_receipt)
    )
    if browser_receipt is None:
        return _blocked("browser_proof_invalid")
    if (
        str(browser_receipt.get("core_manifest_sha256") or "").strip().lower()
        != core_manifest_sha256
    ):
        return _blocked("browser_core_manifest_binding_invalid")

    try:
        configured_tour_base = urllib.parse.urlsplit(
            _property_public_tour_base_url()
        )
        _ = configured_tour_base.port
    except ValueError:
        return _blocked("browser_tested_origin_invalid")
    if (
        configured_tour_base.scheme not in {"http", "https"}
        or not configured_tour_base.netloc
        or configured_tour_base.username is not None
        or configured_tour_base.password is not None
        or configured_tour_base.path.rstrip("/") != "/tours"
        or configured_tour_base.query
        or configured_tour_base.fragment
    ):
        return _blocked("browser_tested_origin_invalid")
    expected_origin = urllib.parse.urlunsplit(
        (
            configured_tour_base.scheme,
            configured_tour_base.netloc,
            "",
            "",
            "",
        )
    )
    expected_tested_url = f"{expected_origin}{expected_control_path}"
    tested_url = str(browser_receipt.get("tested_url") or "")
    tested_origin = str(browser_receipt.get("tested_origin") or "")
    tested_path = str(browser_receipt.get("tour_path") or "")
    try:
        tested_url_parts = urllib.parse.urlsplit(tested_url)
        _ = tested_url_parts.port
    except ValueError:
        return _blocked("browser_tested_url_invalid")
    if tested_origin != expected_origin:
        return _blocked("browser_tested_origin_invalid")
    if tested_path != expected_control_path:
        return _blocked("browser_tested_path_invalid")
    if (
        tested_url != expected_tested_url
        or tested_url_parts.scheme != configured_tour_base.scheme
        or tested_url_parts.netloc != configured_tour_base.netloc
        or tested_url_parts.path != expected_control_path
        or tested_url_parts.query
        or tested_url_parts.fragment
        or tested_url_parts.username is not None
        or tested_url_parts.password is not None
    ):
        return _blocked("browser_tested_url_invalid")
    if (
        str(browser_receipt.get("proof_status") or "").strip().lower()
        != "pass"
        or any(browser_receipt.get(key) is not True for key in required_browser_checks)
        or set(browser_scene_ids) != scene_id_set
        or str(browser_receipt.get("representation_disclosure") or "").strip()
        != disclosure
        or browser_receipt.get("viewer_implementation")
        != "app.api.routes.public_tours._tour_control_panorama_html"
        or browser_receipt.get("route_stack") != "fastapi_public_route"
        or browser_receipt.get("renderer_module_path")
        != _AI_PANORAMA_THREE_MODULE_PATH
        or str(browser_receipt.get("renderer_module_sha256") or "").strip().lower()
        != _AI_PANORAMA_THREE_MODULE_SHA256
        or browser_receipt.get("renderer_http_status") != 200
        or list(browser_receipt.get("external_script_requests") or [])
        or set(browser_receipt.get("verified_hotspot_edges") or [])
        != hotspot_edges
        or browser_receipt.get("dollhouse_room_count") != len(spatial_rooms)
        or not browser_observed_at_valid
    ):
        return _blocked("browser_proof_invalid")
    performance_receipt = browser_receipt.get("performance")
    if not isinstance(performance_receipt, dict):
        return _blocked("browser_performance_proof_invalid")
    try:
        initial_scene_loaded_ms = float(
            performance_receipt.get("initial_scene_loaded_ms")
        )
        slow_network_initial_scene_loaded_ms = float(
            performance_receipt.get("slow_network_initial_scene_loaded_ms")
        )
    except (TypeError, ValueError):
        return _blocked("browser_performance_proof_invalid")
    if (
        not math.isfinite(initial_scene_loaded_ms)
        or not math.isfinite(slow_network_initial_scene_loaded_ms)
        or not (0.0 < initial_scene_loaded_ms <= 12_000.0)
        or not (0.0 < slow_network_initial_scene_loaded_ms <= 20_000.0)
        or performance_receipt.get("total_panorama_bytes")
        != total_panorama_bytes
        or performance_receipt.get("largest_panorama_bytes")
        != largest_panorama_bytes
        or performance_receipt.get("slow_network_profile")
        != "150ms-latency-4mbps"
        or performance_receipt.get("slow_network_all_scenes_loaded") is not True
    ):
        return _blocked("browser_performance_proof_invalid")
    screenshot_hashes: set[str] = set()
    for surface in ("desktop", "mobile", "dollhouse"):
        surface_receipt = browser_receipt.get(surface)
        if not isinstance(surface_receipt, dict):
            return _blocked("browser_surface_proof_invalid")
        screenshot_relpath = _hosted_property_tour_public_asset_relpath(
            surface_receipt.get("screenshot_relpath")
        )
        screenshot_path = _hosted_property_tour_asset_path(
            bundle_dir,
            screenshot_relpath,
        )
        screenshot_sha256 = str(
            surface_receipt.get("screenshot_sha256") or ""
        ).strip().lower()
        if (
            screenshot_path is None
            or not screenshot_relpath
            or not digest_pattern.fullmatch(screenshot_sha256)
            or _hosted_property_tour_file_sha256(screenshot_path)
            != screenshot_sha256
            or list(surface_receipt.get("page_errors") or [])
            or list(surface_receipt.get("failed_requests") or [])
        ):
            return _blocked("browser_surface_proof_invalid")
        screenshot_width, screenshot_height = _hosted_property_tour_image_dimensions(
            screenshot_path
        )
        expected_viewport = "390x844" if surface == "mobile" else "1440x960"
        expected_canvas = "780x1688" if surface == "mobile" else "1440x960"
        if (
            str(surface_receipt.get("viewport") or "") != expected_viewport
            or str(surface_receipt.get("canvas") or "") != expected_canvas
            or screenshot_sha256 in screenshot_hashes
            or (
                surface in {"desktop", "dollhouse"}
                and not (
                    screenshot_width >= 1280
                    and screenshot_height >= 720
                    and screenshot_width > screenshot_height
                )
            )
            or (
                surface == "mobile"
                and not (
                    screenshot_width >= 700
                    and screenshot_height >= 1200
                    and screenshot_height > screenshot_width
                )
            )
        ):
            return _blocked("browser_surface_image_invalid")
        screenshot_hashes.add(screenshot_sha256)

    return {
        "ready": True,
        "preflight": False,
        "preflight_ready": False,
        "proof_pending": False,
        **common_result,
        "reason": "",
    }


def _hosted_property_tour_ai_panorama_open_url(
    tour_url: object,
    *,
    principal_id: object = "",
) -> str:
    normalized_url = str(tour_url or "").strip()
    if not normalized_url:
        return ""
    reference = _hosted_property_tour_reference_parts(normalized_url)
    if reference is None:
        return ""
    parsed, slug = reference
    payload = _hosted_property_tour_payload_for_url(
        normalized_url,
        principal_id=principal_id,
    )
    if not payload:
        return ""
    contract = _hosted_property_tour_ai_panorama_contract(
        bundle_dir=_public_tour_dir() / slug,
        payload=payload,
    )
    if not contract.get("ready"):
        return ""
    control_path = f"/tours/{urllib.parse.quote(slug, safe='')}/control"
    if not parsed.scheme and not parsed.netloc:
        return control_path
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, control_path, "", "")
    )


def _hosted_property_tour_reconstruction_kind(
    tour_url: object,
    *,
    principal_id: object = "",
) -> str:
    if _hosted_property_tour_ai_panorama_open_url(
        tour_url,
        principal_id=principal_id,
    ):
        return "ai_panorama_360"
    if _hosted_property_tour_generated_reconstruction_open_url(tour_url):
        return "layout_preview"
    return ""


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
    ai_panorama_url = _hosted_property_tour_ai_panorama_open_url(
        normalized_url,
        principal_id=principal_id,
    )
    if ai_panorama_url:
        return ai_panorama_url
    generated_reconstruction_url = _hosted_property_tour_generated_reconstruction_open_url(normalized_url)
    if generated_reconstruction_url:
        reference = _hosted_property_tour_reference_parts(normalized_url)
        if reference is None:
            return ""
        parsed, slug = reference
        if not parsed.scheme and not parsed.netloc:
            return f"/tours/{slug}"
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, f"/tours/{slug}", "", ""))
    return ""


def _hosted_property_tour_walkthrough_asset_url(tour_url: object) -> str:
    normalized_url = str(tour_url or "").strip()
    reference = _hosted_property_tour_reference_parts(normalized_url)
    if reference is None:
        return ""
    parsed, slug = reference
    bundle_dir = _public_tour_dir() / slug
    manifest_path = bundle_dir / "tour.json"
    if not manifest_path.exists():
        return ""
    payload = _load_hosted_property_tour_payload(bundle_dir)
    if not payload or not isinstance(payload, dict):
        return ""
    magicfit_footprint = bool(
        magicfit_provider_declared(payload)
        or isinstance(payload.get("magicfit_import"), dict)
        or str(payload.get("video_sidecar_relpath") or "").strip().startswith(".magicfit-deliveries/")
        or str(payload.get("video_relpath") or "").strip().startswith("magicfit-media/")
    )
    if magicfit_footprint:
        eligibility = evaluate_magicfit_public_eligibility(bundle_dir, payload)
        if not (eligibility.declared and eligibility.eligible and eligibility.video_relpath):
            return ""
        route_path = f"/tours/{urllib.parse.quote(slug, safe='')}/walkthrough"
        if not parsed.scheme and not parsed.netloc:
            return route_path
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, route_path, "", ""))
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
    verified_tour_source = normalized_tour_url
    if not verified_tour_source and normalized_walkthrough_url:
        walkthrough_slug = _hosted_property_tour_slug_from_url(
            normalized_walkthrough_url
        )
        if walkthrough_slug:
            parsed_walkthrough = urllib.parse.urlparse(
                normalized_walkthrough_url
            )
            verified_tour_source = (
                urllib.parse.urlunparse(
                    (
                        parsed_walkthrough.scheme,
                        parsed_walkthrough.netloc,
                        f"/tours/{walkthrough_slug}",
                        "",
                        "",
                        "",
                    )
                )
                if parsed_walkthrough.scheme and parsed_walkthrough.netloc
                else f"/tours/{walkthrough_slug}"
            )
    # A caller-provided MP4 URL is not a customer-ready claim. Readiness must
    # come from the exact hosted bundle, whose provider receipt, playable asset,
    # coverage proof, and publication posture are revalidated above.
    canonical_walkthrough_url = (
        _hosted_property_tour_walkthrough_asset_url(verified_tour_source)
        if verified_tour_source
        else ""
    )
    if not canonical_walkthrough_url:
        return ""
    slug = _hosted_property_tour_slug_from_url(verified_tour_source) or _hosted_property_tour_slug_from_url(canonical_walkthrough_url)
    if not slug:
        return canonical_walkthrough_url
    for source_url in (verified_tour_source, canonical_walkthrough_url):
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
    requested_style = (
        dict(receipt.get("requested_style") or {})
        if isinstance(receipt.get("requested_style"), dict)
        else {}
    )
    try:
        canonical_style = reconstruction_style(
            requested_style.get("id"),
            style_id=requested_style.get("id"),
        )
    except ValueError:
        return {"ready": False}
    if any(
        str(requested_style.get(key) or "") != str(canonical_style.get(key) or "")
        for key in ("id", "label", "prompt", "signature")
    ):
        return {"ready": False}
    style_scene = dict(receipt.get("style_scene") or {}) if isinstance(receipt.get("style_scene"), dict) else {}
    style_scene_ready, _style_scene_reason = validate_style_scene(
        style_scene,
        expected_style=canonical_style,
    )
    if not style_scene_ready:
        return {"ready": False}
    generated_style_contract = {
        "style_contract_version": STYLE_SCENE_CONTRACT_VERSION,
        "style_id": canonical_style["id"],
        "style_label": canonical_style["label"],
        "style_signature": canonical_style["signature"],
        "style_scene_signature": style_scene.get("scene_signature"),
        "style_evidence_status": "ready",
        "styled_scene_instance_count": len(list(style_scene.get("instances") or [])),
        "style_cue_kinds": list(style_scene.get("required_cues") or []),
        "floorplan_display_mode": FLOORPLAN_DISPLAY_MODE,
    }
    if any(
        generated_reconstruction.get(key) != expected
        for key, expected in generated_style_contract.items()
    ):
        return {"ready": False}
    if any(
        receipt_viewer.get(key) != expected
        for key, expected in {
            "style_id": canonical_style["id"],
            "style_signature": canonical_style["signature"],
            "style_scene_signature": style_scene.get("scene_signature"),
            "floorplan_display_mode": FLOORPLAN_DISPLAY_MODE,
        }.items()
    ):
        return {"ready": False}
    viewer_sha256 = str(receipt_viewer.get("sha256") or "").strip()
    if not viewer_sha256 or _hosted_property_tour_file_sha256(viewer_path) != viewer_sha256:
        return {"ready": False}
    model_relpath = str(generated_reconstruction.get("model_relpath") or "").strip().lstrip("/")
    material_relpath = str(generated_reconstruction.get("material_relpath") or "").strip().lstrip("/")
    model_path = _generated_reconstruction_relpath_file(bundle_dir, model_relpath)
    material_path = _generated_reconstruction_relpath_file(bundle_dir, material_relpath)
    if not model_path or not material_path:
        return {"ready": False}
    model_receipt = dict(receipt.get("model") or {}) if isinstance(receipt.get("model"), dict) else {}
    if (
        str(model_receipt.get("obj_sha256") or "").strip()
        != _hosted_property_tour_file_sha256(model_path)
        or str(model_receipt.get("mtl_sha256") or "").strip()
        != _hosted_property_tour_file_sha256(material_path)
    ):
        return {"ready": False}
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
                    if (
                        str(glb_export.get("glb_sha256") or "").strip()
                        != _hosted_property_tour_file_sha256(candidate_glb_path)
                    ):
                        return {"ready": False}
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
    if host == "storage.net-fs.com":
        return "3dvista"
    return ""


def _property_tour_provider_url_shape_valid(value: object) -> bool:
    normalized = _safe_live_property_tour_url(value)
    provider = _property_tour_provider_host_kind(normalized)
    if not normalized or not provider:
        return False
    try:
        parsed = urllib.parse.urlsplit(normalized)
        if (
            parsed.scheme.lower() != "https"
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
            or parsed.fragment
        ):
            return False
    except (TypeError, ValueError):
        return False
    decoded_path = str(parsed.path or "")
    for _ in range(4):
        expanded = urllib.parse.unquote(decoded_path)
        if expanded == decoded_path:
            break
        decoded_path = expanded
    if "\\" in decoded_path:
        return False
    credential_query_keys = {
        "access_token",
        "accesstoken",
        "token",
        "api_key",
        "apikey",
        "key",
        "signature",
        "sig",
        "auth",
        "auth_token",
        "authorization",
        "password",
        "client_secret",
        "clientsecret",
        "x_access_token",
        "xaccesstoken",
        "x_amz_signature",
        "xamzsignature",
        "x_amz_credential",
        "xamzcredential",
        "credential",
        "secret",
        "jwt",
        "bearer",
        "session_token",
        "sessiontoken",
    }
    credential_query_suffixes = (
        "_access_token",
        "accesstoken",
        "_api_key",
        "_auth_token",
        "_client_secret",
        "clientsecret",
        "_amz_signature",
        "amzsignature",
        "_amz_credential",
        "amzcredential",
        "_credential",
        "_secret",
        "_jwt",
        "_bearer",
        "_session_token",
        "sessiontoken",
    )
    for raw_key, _value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        decoded_key = str(raw_key or "")
        for _ in range(4):
            expanded_key = urllib.parse.unquote(decoded_key)
            if expanded_key == decoded_key:
                break
            decoded_key = expanded_key
        normalized_key = re.sub(r"[^a-z0-9]+", "_", decoded_key.lower()).strip("_")
        if normalized_key in credential_query_keys or any(
            normalized_key.endswith(suffix)
            for suffix in credential_query_suffixes
        ):
            return False
    segments = [segment for segment in decoded_path.split("/") if segment]
    if any(segment in {".", ".."} for segment in segments):
        return False
    if provider == "matterport":
        if decoded_path.rstrip("/").lower() != "/show":
            return False
        model_values = urllib.parse.parse_qs(parsed.query, keep_blank_values=True).get("m", [])
        return bool(
            len(model_values) == 1
            and re.fullmatch(r"[A-Za-z0-9_-]{6,64}", str(model_values[0] or "").strip())
        )
    if provider == "3dvista":
        host = str(parsed.hostname or "").strip().lower().rstrip(".")
        if host == "storage.net-fs.com":
            if len(segments) not in {3, 4} or segments[0].lower() != "hosting":
                return False
            if not all(re.fullmatch(r"[0-9]{1,20}", segment) for segment in segments[1:3]):
                return False
            return len(segments) == 3 or segments[3].lower() in {"index.htm", "index.html"}
        if len(segments) < 2 or segments[0].lower() not in {"360", "share", "tour", "tours"}:
            return False
        identifier = "/".join(segments[1:])
        return bool(
            len(identifier) >= 3
            and not any(segment.lower() in {"admin", "auth", "login", "signin"} for segment in segments[1:])
            and re.fullmatch(r"[A-Za-z0-9._~/-]+", identifier)
        )
    return False


def _canonical_matterport_tour_url(value: object) -> str:
    """Reduce an accepted Matterport share URL to its public model identity."""

    normalized = str(value or "").strip()
    if (
        _property_tour_provider_host_kind(normalized) != "matterport"
        or not _property_tour_provider_url_shape_valid(normalized)
    ):
        return ""
    try:
        parsed = urllib.parse.urlsplit(normalized)
        raw_path = str(parsed.path or "")
        raw_query = str(parsed.query or "")
        if (
            "%" in raw_path
            or "\\" in raw_path
            or raw_path.rstrip("/").lower() != "/show"
            or "%" in raw_query
        ):
            return ""
        model_values = urllib.parse.parse_qs(
            raw_query,
            keep_blank_values=True,
        ).get("m", [])
    except (TypeError, ValueError):
        return ""
    if len(model_values) != 1:
        return ""
    model_id = str(model_values[0] or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{6,64}", model_id) is None:
        return ""
    return (
        "https://my.matterport.com/show?m="
        + urllib.parse.quote(model_id, safe="-_")
    )


def _prefer_hosted_live_360_embed(source_virtual_tour_url: object) -> bool:
    return _property_tour_provider_url_shape_valid(source_virtual_tour_url)

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
    normalized_variant = re.sub(
        r"[^a-z0-9]+",
        "-",
        str(variant_key or "layout_first").lower(),
    ).strip("-") or "layout-first"
    # The public route, governed render bridge, and lifecycle store share the
    # same 120-character slug contract. Keep the full normalized variant in
    # the identity digest, but shrink only long title segments so existing
    # short slugs remain stable.
    variant = normalized_variant[:32].strip("-") or "layout-first"
    normalized_principal = str(principal_id or "").strip()
    identity_material = (
        f"{property_url}|{listing_id}|{normalized_variant}".encode("utf-8")
    )
    if normalized_principal:
        digest = hmac.new(
            _hosted_property_tour_identity_secret(),
            normalized_principal.encode("utf-8") + b"\x00" + identity_material,
            hashlib.sha256,
        ).hexdigest()[:20]
    else:
        # Legacy slugs remain computable for exact-owner migration-safe reuse.
        digest = hashlib.sha256(identity_material).hexdigest()[:10]
    suffix = f"-{variant}-{digest}"
    base_limit = min(96, _HOSTED_PROPERTY_TOUR_SLUG_MAX_LENGTH - len(suffix))
    bounded_base = base[:base_limit].strip("-") or "property-tour"[:base_limit]
    slug = f"{bounded_base}{suffix}"
    _assert_governed_property_tour_slug_not_reserved(slug)
    return slug


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
    if live_provider and not _property_tour_provider_url_shape_valid(live_url):
        raise RuntimeError("pure_360_source_invalid")
    if live_provider in _CUSTOMER_FACING_TOUR_PROVIDERS:
        raise RuntimeError("property_tour_output_unverified")
    if "360.kalandra.at" not in live_host and "feelestate" not in live_host:
        raise RuntimeError("pure_360_source_unsupported")
    raise RuntimeError("property_tour_cube_fallback_disabled")

def _embedded_live_360_source_url(payload: dict[str, object]) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("source_virtual_tour_url", "source_virtual_tour_origin"):
        normalized = _safe_live_property_tour_url(str(payload.get(key) or "").strip())
        if normalized and _property_tour_provider_url_shape_valid(normalized):
            return normalized
    return ""

def _hosted_property_tour_direct_360_url(tour_url: str) -> str:
    normalized_url = str(tour_url or "").strip()
    reference = _hosted_property_tour_reference_parts(normalized_url)
    if reference is None:
        return ""
    _parsed, slug = reference
    public_dir = _public_tour_dir()
    manifest_path = public_dir / slug / "tour.json"
    if not manifest_path.exists():
        return ""
    direct_url = _public_hosted_property_tour_live_source_url(public_dir / slug)
    if (
        _property_tour_provider_host_kind(direct_url) not in _CUSTOMER_FACING_TOUR_PROVIDERS
        or not _property_tour_provider_url_shape_valid(direct_url)
    ):
        return ""
    return direct_url

def _matterport_thumb_url(source_virtual_tour_url: str) -> str:
    normalized = str(source_virtual_tour_url or "").strip()
    if not normalized:
        return ""
    parsed = urllib.parse.urlparse(normalized)
    if (
        _property_tour_provider_host_kind(normalized) != "matterport"
        or not _property_tour_provider_url_shape_valid(normalized)
    ):
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
    reference = _hosted_property_tour_reference_parts(normalized_url)
    if (
        reference is None
        or not safe_slug
        or safe_slug != reference[1]
        or not safe_relpath
    ):
        return ""
    # Keep producer-side URLs byte-for-byte aligned with the public route's
    # component validation and encoding.  In particular, URI delimiters can
    # never be reinterpreted as query/fragment syntax after publication.
    from app.api.routes.public_tour_payloads import public_tour_file_url

    relative_asset_url = public_tour_file_url(safe_slug, safe_relpath)
    if not relative_asset_url:
        return ""
    parsed, _reference_slug = reference
    if not parsed.scheme and not parsed.netloc:
        return relative_asset_url
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            relative_asset_url,
            "",
            "",
        )
    )

def _hosted_property_tour_preview_image_url(tour_url: str) -> str:
    normalized_url = str(tour_url or "").strip()
    reference = _hosted_property_tour_reference_parts(normalized_url)
    if reference is None:
        return ""
    _parsed, slug = reference
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
        if not re.fullmatch(
            rf"[A-Za-z0-9][A-Za-z0-9._-]{{0,{_HOSTED_PROPERTY_TOUR_SLUG_MAX_LENGTH - 1}}}",
            normalized,
        ):
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
