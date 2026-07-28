#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

_SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(_SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIRECTORY))

from property_tour_governed_reservation import require_dynamic_tour_slug

try:
    from property_magicfit_delivery_contract import (
        DELIVERIES_ROOT_RELPATH,
        MANIFEST_TRANSFORM_CONTRACT,
        PENDING_DELIVERY_CONTRACT,
        PENDING_POINTER_RELPATH,
        PUBLIC_VIDEO_EXTENSIONS,
        STAGING_ROOT_RELPATH,
        accepted_sidecar_relpath as _contract_accepted_sidecar_relpath,
        build_candidate_manifest_bytes,
        canonical_json_bytes,
        coverage_proof_from_receipt,
        delivery_digest as _contract_delivery_digest,
        digest_bound_video_relpath,
        sha256_bytes as _contract_sha256_bytes,
        staged_manifest_relpath as _contract_staged_manifest_relpath,
        staged_video_relpath as _contract_staged_video_relpath,
        strict_json_object_bytes,
        validate_magicfit_source_receipt,
    )
    from property_magicfit_secure_io import (
        MagicFitSecureIOError,
        StableFileSnapshot,
        collect_bounded_magicfit_stage_orphans_at,
        copy_magicfit_stage_video_at,
        create_magicfit_stage_directory_at,
        hash_stable_bounded_file,
        hash_stable_bounded_file_at,
        lexical_absolute_path,
        open_directory_componentwise_no_follow,
        open_regular_file_at,
        read_stable_bounded_bytes,
        read_stable_bounded_bytes_at,
        remove_closed_magicfit_stage_at,
        require_complete_magicfit_stage_at,
        write_magicfit_stage_bytes_at,
        write_regular_file_atomic_at,
    )
    from property_tour_host_safety import (
        TourHostSafetyError,
        bounded_env_int,
        bounded_lane_lock,
        require_free_disk,
        tour_asset_max_bytes,
        tour_manifest_max_bytes,
    )
    from property_tour_publication_lock import property_tour_publication_lock
except ModuleNotFoundError:
    from scripts.property_magicfit_delivery_contract import (
        DELIVERIES_ROOT_RELPATH,
        MANIFEST_TRANSFORM_CONTRACT,
        PENDING_DELIVERY_CONTRACT,
        PENDING_POINTER_RELPATH,
        PUBLIC_VIDEO_EXTENSIONS,
        STAGING_ROOT_RELPATH,
        accepted_sidecar_relpath as _contract_accepted_sidecar_relpath,
        build_candidate_manifest_bytes,
        canonical_json_bytes,
        coverage_proof_from_receipt,
        delivery_digest as _contract_delivery_digest,
        digest_bound_video_relpath,
        sha256_bytes as _contract_sha256_bytes,
        staged_manifest_relpath as _contract_staged_manifest_relpath,
        staged_video_relpath as _contract_staged_video_relpath,
        strict_json_object_bytes,
        validate_magicfit_source_receipt,
    )
    from scripts.property_magicfit_secure_io import (
        MagicFitSecureIOError,
        StableFileSnapshot,
        collect_bounded_magicfit_stage_orphans_at,
        copy_magicfit_stage_video_at,
        create_magicfit_stage_directory_at,
        hash_stable_bounded_file,
        hash_stable_bounded_file_at,
        lexical_absolute_path,
        open_directory_componentwise_no_follow,
        open_regular_file_at,
        read_stable_bounded_bytes,
        read_stable_bounded_bytes_at,
        remove_closed_magicfit_stage_at,
        require_complete_magicfit_stage_at,
        write_magicfit_stage_bytes_at,
        write_regular_file_atomic_at,
    )
    from scripts.property_tour_host_safety import (
        TourHostSafetyError,
        bounded_env_int,
        bounded_lane_lock,
        require_free_disk,
        tour_asset_max_bytes,
        tour_manifest_max_bytes,
    )
    from scripts.property_tour_publication_lock import (
        property_tour_publication_lock,
    )


ACTIVATION_LOCK_RELPATH = ".magicfit-activation.lock"


class _DuplicateJsonKey(ValueError):
    pass


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"nonfinite:{value}")


class _DuplicateJsonKey(ValueError):
    pass


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"nonfinite:{value}")


def _public_tour_dir() -> Path:
    return Path(os.getenv("EA_PUBLIC_TOUR_DIR") or "/data/public_property_tours").expanduser().resolve()


def _safe_relpath(value: object) -> str:
    """Return only an already-canonical, filesystem-safe relative path."""

    if not isinstance(value, str) or not value or value != value.strip():
        return ""
    if "\\" in value or value.startswith("/"):
        return ""
    if any(part in {"", ".", ".."} for part in value.split("/")):
        return ""
    if any(
        ord(character) < 0x20
        or ord(character) == 0x7F
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        return ""
    if PurePosixPath(value).as_posix() != value:
        return ""
    return value


def _sha256_bytes(body: bytes) -> str:
    return _contract_sha256_bytes(body)


def _stable_bytes(
    path: Path, *, reason: str, maximum_bytes: int
) -> StableFileSnapshot:
    try:
        return read_stable_bounded_bytes(
            path,
            reason=reason,
            maximum_bytes=maximum_bytes,
        )
    except MagicFitSecureIOError as exc:
        raise SystemExit(str(exc)) from exc


def _json_bytes(payload: dict[str, object]) -> bytes:
    return canonical_json_bytes(payload)


@contextlib.contextmanager
def _activation_lock(public_root: Path, slug: str):
    """Hold the public root, named bundle, and bundle-local activation lock."""

    try:
        public_root_fd = open_directory_componentwise_no_follow(
            public_root, reason="tour_bundle_invalid"
        )
        bundle_fd = os.open(
            slug,
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=public_root_fd,
        )
    except (MagicFitSecureIOError, OSError) as exc:
        if "public_root_fd" in locals():
            os.close(public_root_fd)
        raise SystemExit("tour_bundle_invalid") from exc
    flags = (
        os.O_CREAT
        | os.O_RDWR
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(ACTIVATION_LOCK_RELPATH, flags, 0o600, dir_fd=bundle_fd)
    except BaseException:
        os.close(bundle_fd)
        os.close(public_root_fd)
        raise
    try:
        bundle_metadata = os.fstat(bundle_fd)
        metadata = os.fstat(fd)
        if (
            not stat.S_ISDIR(bundle_metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise SystemExit("magicfit_activation_lock_invalid")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield public_root_fd, bundle_fd
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
            os.close(bundle_fd)
            os.close(public_root_fd)


def _confirm_named_bundle_identity(
    public_root_fd: int,
    slug: str,
    bundle_fd: int,
) -> None:
    held = os.fstat(bundle_fd)
    try:
        named = os.stat(slug, dir_fd=public_root_fd, follow_symlinks=False)
    except OSError as exc:
        raise SystemExit("magicfit_import_bundle_changed") from exc
    if (
        not stat.S_ISDIR(held.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino)
    ):
        raise SystemExit("magicfit_import_bundle_changed")


def _digest_bound_video_relpath(requested_relpath: str, video_sha256: str) -> str:
    try:
        return digest_bound_video_relpath(requested_relpath, video_sha256)
    except ValueError as exc:
        raise SystemExit("invalid_magicfit_target") from exc


def _video_header_is_valid(*, suffix: str, header: bytes) -> bool:
    suffix = suffix.lower()
    if suffix not in PUBLIC_VIDEO_EXTENSIONS:
        return False
    if len(header) < 12:
        return False
    if suffix in {".mp4", ".m4v", ".mov"}:
        return b"ftyp" in header[:32]
    return header.startswith(b"\x1aE\xdf\xa3")


def _video_is_playable_at(
    bundle_fd: int,
    relpath: str,
    *,
    expected_sha256: str,
    expected_size_bytes: int,
) -> bool:
    suffix = PurePosixPath(relpath).suffix.lower()
    descriptor = -1
    try:
        descriptor = open_regular_file_at(
            bundle_fd,
            relpath,
            reason="magicfit_staged_video_conflict",
            maximum_bytes=tour_asset_max_bytes(),
        )
        before = os.fstat(descriptor)
        header = os.read(descriptor, 64)
        os.lseek(descriptor, 0, os.SEEK_SET)
    except (MagicFitSecureIOError, OSError):
        if descriptor >= 0:
            os.close(descriptor)
        return False
    if not _video_header_is_valid(suffix=suffix, header=header):
        os.close(descriptor)
        return False
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        os.close(descriptor)
        return False
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_type,duration:format=duration",
                "-of",
                "json",
                f"/proc/self/fd/{descriptor}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
            pass_fds=(descriptor,),
        )
    except Exception:
        os.close(descriptor)
        return False
    if result.returncode != 0:
        os.close(descriptor)
        return False
    try:
        payload = json.loads(result.stdout or "{}")
    except Exception:
        os.close(descriptor)
        return False
    streams = [row for row in list(payload.get("streams") or []) if isinstance(row, dict)]
    if not any(str(row.get("codec_type") or "").strip().lower() == "video" for row in streams):
        os.close(descriptor)
        return False
    durations: list[float] = []
    if isinstance(payload.get("format"), dict):
        try:
            durations.append(float(payload["format"].get("duration")))
        except Exception:
            pass
    for row in streams:
        try:
            durations.append(float(row.get("duration")))
        except Exception:
            pass
    try:
        after = os.fstat(descriptor)
        snapshot = hash_stable_bounded_file_at(
            bundle_fd,
            relpath,
            reason="magicfit_staged_video_conflict",
            maximum_bytes=tour_asset_max_bytes(),
        )
    except (MagicFitSecureIOError, OSError):
        os.close(descriptor)
        return False
    os.close(descriptor)
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_nlink,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_nlink,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    return bool(
        durations
        and max(durations) > 0.0
        and before_identity == after_identity
        and snapshot.identity == before_identity
        and snapshot.sha256 == expected_sha256
        and snapshot.size_bytes == expected_size_bytes
    )


def _load_magicfit_receipt(
    path_value: str,
    *,
    source: Path,
    slug: str,
    allow_unreceipted: bool,
) -> tuple[dict[str, object], str, str]:
    if allow_unreceipted:
        # This explicit test-only lane may exercise private staging and host
        # safety, but its unforgeable marker digest can never satisfy the
        # acceptance command's required, valid source-receipt bytes.
        marker = b"propertyquarry:magicfit-unreceipted-test-asset:v1\n"
        return {}, "", hashlib.sha256(marker).hexdigest()
    if not str(path_value or "").strip():
        raise SystemExit("magicfit_receipt_missing")
    try:
        receipt_path = lexical_absolute_path(path_value or "")
    except MagicFitSecureIOError as exc:
        raise SystemExit("magicfit_receipt_missing") from exc
    try:
        receipt_snapshot = read_stable_bounded_bytes(
            receipt_path,
            reason="magicfit_receipt",
            maximum_bytes=bounded_env_int(
                "PROPERTYQUARRY_MAGICFIT_RECEIPT_MAX_BYTES",
                default=1024 * 1024,
                minimum=1_024,
                maximum=8 * 1024 * 1024,
            ),
        )
        assert receipt_snapshot.body is not None
        receipt_bytes = receipt_snapshot.body
        payload = json.loads(
            receipt_bytes.decode("utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_nonfinite_json,
        )
    except MagicFitSecureIOError as exc:
        if exc.missing:
            raise SystemExit("magicfit_receipt_missing") from exc
        raise SystemExit(str(exc)) from exc
    except Exception as exc:
        raise SystemExit(f"magicfit_receipt_invalid:{type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("magicfit_receipt_invalid")
    try:
        validate_magicfit_source_receipt(payload, slug=slug)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    output_file_value = payload.get("output_file")
    if "output_file" in payload and (
        not isinstance(output_file_value, str)
        or not output_file_value
        or output_file_value != output_file_value.strip()
    ):
        raise SystemExit("magicfit_receipt_output_invalid:type")
    output_file = output_file_value if isinstance(output_file_value, str) else ""
    if output_file:
        try:
            if lexical_absolute_path(output_file) != source:
                raise SystemExit("magicfit_receipt_output_mismatch")
        except MagicFitSecureIOError as exc:
            raise SystemExit(f"magicfit_receipt_output_invalid:{type(exc).__name__}") from exc
    return payload, str(receipt_path), receipt_snapshot.sha256


def _coverage_proof_from_receipt(payload: dict[str, object]) -> dict[str, object]:
    try:
        return coverage_proof_from_receipt(payload)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def _selected_pending_digest(bundle_fd: int) -> str:
    """Return only an exactly shaped selected stage digest, if readable."""

    try:
        snapshot = read_stable_bounded_bytes_at(
            bundle_fd,
            PENDING_POINTER_RELPATH,
            reason="magicfit_pending_pointer",
            maximum_bytes=tour_manifest_max_bytes(),
        )
        assert snapshot.body is not None
        pending = strict_json_object_bytes(
            snapshot.body, reason="magicfit_pending_pointer_invalid"
        )
    except (MagicFitSecureIOError, ValueError):
        return ""
    for field, filename in (
        ("staged_manifest_relpath", "tour.json"),
        ("staged_video_relpath", ""),
    ):
        relpath = _safe_relpath(pending.get(field))
        parts = PurePosixPath(relpath).parts
        if (
            len(parts) != 3
            or parts[0] != STAGING_ROOT_RELPATH
            or (filename and parts[2] != filename)
        ):
            return ""
        if field == "staged_manifest_relpath":
            digest = parts[1]
        elif parts[1] != digest:
            return ""
    return digest if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest) else ""


def _main_unlocked() -> int:
    parser = argparse.ArgumentParser(description="Import a verified MagicFit walkthrough video into a public tour bundle.")
    parser.add_argument("--slug", required=True, help="Existing PropertyQuarry public tour slug.")
    parser.add_argument("--video-path", required=True, help="Playable MagicFit MP4/M4V/MOV/WebM render.")
    parser.add_argument("--target-relpath", default="", help="Optional target path inside the tour bundle.")
    parser.add_argument("--source-receipt", default="", help="MagicFit render receipt path to reference without embedding secrets.")
    parser.add_argument(
        "--allow-unreceipted-test-asset",
        action="store_true",
        help="Allow a playable local fixture without MagicFit provenance. Intended for tests only.",
    )
    args = parser.parse_args()

    slug = _safe_relpath(args.slug)
    if "/" in slug or not slug:
        raise SystemExit("invalid_tour_slug")
    try:
        require_dynamic_tour_slug(slug)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    try:
        source = lexical_absolute_path(args.video_path)
        source_snapshot = hash_stable_bounded_file(
            source,
            reason="magicfit_video",
            maximum_bytes=tour_asset_max_bytes(),
            prefix_bytes=64,
        )
    except MagicFitSecureIOError as exc:
        if exc.missing:
            raise SystemExit("magicfit_video_missing") from exc
        raise SystemExit(str(exc)) from exc
    # Reject obvious placeholders from the prefix captured by the same stable
    # descriptor read. Full semantic playability is checked after the exact
    # digest-verified bytes have been copied into private staging below.
    if not _video_header_is_valid(
        suffix=source.suffix,
        header=source_snapshot.prefix,
    ):
        raise SystemExit("magicfit_video_unverified")
    receipt_payload, _receipt_path, source_receipt_sha256 = _load_magicfit_receipt(
        args.source_receipt,
        source=source,
        slug=slug,
        allow_unreceipted=bool(args.allow_unreceipted_test_asset),
    )

    public_root = _public_tour_dir()

    if args.target_relpath:
        target_relpath = _safe_relpath(args.target_relpath)
        if not target_relpath:
            raise SystemExit("invalid_magicfit_target")
    else:
        target_relpath = f"magicfit-walkthrough{source.suffix.lower()}"
    if PurePosixPath(target_relpath).suffix.lower() not in PUBLIC_VIDEO_EXTENSIONS:
        raise SystemExit("invalid_magicfit_target")
    video_sha256 = source_snapshot.sha256
    final_video_relpath = _digest_bound_video_relpath(target_relpath, video_sha256)
    imported_at = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

    # Global import lane -> canonical slug publication lock -> bundle-local
    # activation lock is the only lock order used by this command.  Web
    # publish/revoke takes the same canonical slug lock and never waits on the
    # import lane, so the ordering cannot form a cycle.
    with property_tour_publication_lock(
        public_dir=public_root,
        slug=slug,
    ), _activation_lock(public_root, slug) as (public_root_fd, bundle_fd):
        previous_pending_digest = _selected_pending_digest(bundle_fd)
        try:
            manifest_snapshot = read_stable_bounded_bytes_at(
                bundle_fd,
                "tour.json",
                reason="tour_manifest",
                maximum_bytes=tour_manifest_max_bytes(),
            )
            assert manifest_snapshot.body is not None
            manifest_bytes = manifest_snapshot.body
            payload = json.loads(
                manifest_bytes.decode("utf-8"),
                object_pairs_hook=_strict_json_object,
                parse_constant=_reject_nonfinite_json,
            )
        except MagicFitSecureIOError as exc:
            if exc.missing:
                raise SystemExit("tour_manifest_missing") from exc
            raise SystemExit(str(exc)) from exc
        except Exception as exc:
            raise SystemExit(f"invalid_tour_manifest:{type(exc).__name__}") from exc
        if not isinstance(payload, dict):
            raise SystemExit("invalid_tour_manifest")
        if payload.get("slug") != slug:
            raise SystemExit("invalid_tour_manifest_slug")

        base_manifest_sha256 = _sha256_bytes(manifest_bytes)
        video_size_bytes = source_snapshot.size_bytes
        coverage_proof = _coverage_proof_from_receipt(receipt_payload)
        try:
            delivery_digest = _contract_delivery_digest(
                slug=slug,
                requested_target_relpath=target_relpath,
                video_relpath=final_video_relpath,
                video_sha256=video_sha256,
                video_size_bytes=video_size_bytes,
                source_receipt_sha256=source_receipt_sha256,
                base_manifest_sha256=base_manifest_sha256,
                generated_at=imported_at,
                coverage_proof=coverage_proof,
            )
        except ValueError as exc:
            raise SystemExit("magicfit_import_delivery_subject_invalid") from exc
        staged_video_relpath = _contract_staged_video_relpath(
            delivery_digest, source.suffix
        )
        stage_video_name = PurePosixPath(staged_video_relpath).name
        stage_created = False
        pointer_committed = False
        pending_body = b""
        try:
            try:
                stage_created = create_magicfit_stage_directory_at(
                    bundle_fd, delivery_digest
                )
                require_free_disk(
                    Path(f"/proc/self/fd/{bundle_fd}"),
                    reason_prefix="magicfit_import",
                    expected_write_bytes=source_snapshot.size_bytes,
                )
                copied = copy_magicfit_stage_video_at(
                    source,
                    bundle_fd,
                    delivery_digest,
                    name=stage_video_name,
                    expected_sha256=video_sha256,
                    maximum_bytes=tour_asset_max_bytes(),
                )
            except MagicFitSecureIOError as exc:
                raise SystemExit(str(exc)) from exc
            except TourHostSafetyError as exc:
                raise SystemExit(str(exc)) from exc
            if copied.sha256 != video_sha256 or not _video_is_playable_at(
                bundle_fd,
                staged_video_relpath,
                expected_sha256=video_sha256,
                expected_size_bytes=source_snapshot.size_bytes,
            ):
                raise SystemExit("magicfit_video_unverified")

            accepted_sidecar_relpath = _contract_accepted_sidecar_relpath(
                delivery_digest
            )
            staged_manifest_relpath = _contract_staged_manifest_relpath(
                delivery_digest
            )
            # Build the exact future public manifest in private staging.  It is
            # not reachable through the public asset allowlist, and tour.json
            # remains byte-for-byte unchanged until acceptance commits it last.
            try:
                staged_manifest_bytes = build_candidate_manifest_bytes(
                    base_manifest_bytes=manifest_bytes,
                    slug=slug,
                    requested_target_relpath=target_relpath,
                    video_relpath=final_video_relpath,
                    video_sha256=video_sha256,
                    video_size_bytes=video_size_bytes,
                    source_receipt_sha256=source_receipt_sha256,
                    generated_at=imported_at,
                    coverage_proof=coverage_proof,
                )
            except ValueError as exc:
                raise SystemExit("magicfit_import_manifest_transform_invalid") from exc
            try:
                write_magicfit_stage_bytes_at(
                    bundle_fd,
                    delivery_digest,
                    name="tour.json",
                    body=staged_manifest_bytes,
                    maximum_bytes=tour_manifest_max_bytes(),
                )
                require_complete_magicfit_stage_at(
                    bundle_fd,
                    delivery_digest,
                    video_name=stage_video_name,
                )
            except MagicFitSecureIOError as exc:
                raise SystemExit(str(exc)) from exc

            pending_delivery = {
                "contract_name": PENDING_DELIVERY_CONTRACT,
                "provider": "magicfit",
                "provider_key": "magicfit",
                "provider_backend_key": "magicfit",
                "render_status": "completed",
                "status": "rendered_pending_delivery_acceptance",
                "acceptance_status": "pending",
                "launch_eligible": False,
                "manifest_transform_contract": MANIFEST_TRANSFORM_CONTRACT,
                "tour_slug": slug,
                "requested_target_relpath": target_relpath,
                "video_relpath": final_video_relpath,
                "video_sha256": video_sha256,
                "video_size_bytes": video_size_bytes,
                "source_receipt_sha256": source_receipt_sha256,
                "coverage_proof": coverage_proof,
                "generated_at": imported_at,
                "base_manifest_sha256": base_manifest_sha256,
                "staged_video_relpath": staged_video_relpath,
                "staged_manifest_relpath": staged_manifest_relpath,
                "staged_manifest_sha256": _sha256_bytes(staged_manifest_bytes),
                "accepted_sidecar_relpath": accepted_sidecar_relpath,
            }
            # The pointer is the import commit point.  A crash before this
            # replace leaves the previous selection intact; afterwards only a
            # complete digest-bound stage is selected.
            pending_body = _json_bytes(pending_delivery)
            _confirm_named_bundle_identity(
                public_root_fd,
                slug,
                bundle_fd,
            )
            write_regular_file_atomic_at(
                bundle_fd,
                PENDING_POINTER_RELPATH,
                pending_body,
                reason="magicfit_pending_pointer_commit_failed",
                mode=0o600,
            )
            pointer_committed = True
        except BaseException as original:
            if pending_body and not pointer_committed:
                try:
                    selected = read_stable_bounded_bytes_at(
                        bundle_fd,
                        PENDING_POINTER_RELPATH,
                        reason="magicfit_pending_pointer",
                        maximum_bytes=tour_manifest_max_bytes(),
                    )
                    pointer_committed = selected.body == pending_body
                except MagicFitSecureIOError:
                    pointer_committed = False
            if stage_created and not pointer_committed:
                try:
                    cleaned = remove_closed_magicfit_stage_at(
                        bundle_fd, delivery_digest
                    )
                    if not cleaned:
                        raise MagicFitSecureIOError(
                            "magicfit_staging_cleanup_incomplete"
                        )
                except MagicFitSecureIOError as cleanup_error:
                    raise SystemExit(str(cleanup_error)) from original
            raise

        # The new pointer is durable before any previous or orphan stage can be
        # retired.  Unknown entries and the selected digest are never touched.
        try:
            if (
                previous_pending_digest
                and previous_pending_digest != delivery_digest
            ):
                remove_closed_magicfit_stage_at(
                    bundle_fd, previous_pending_digest
                )
            collect_bounded_magicfit_stage_orphans_at(
                bundle_fd,
                protected_digests={delivery_digest},
            )
        except MagicFitSecureIOError as exc:
            raise SystemExit(str(exc)) from exc
    print(
        json.dumps(
            {
                "status": "staged_pending_delivery_acceptance",
                "slug": slug,
                "video_relpath_after_acceptance": final_video_relpath,
                "public_video_url_after_acceptance": (
                    f"/tours/files/{slug}/{final_video_relpath}"
                ),
                "provider": "magicfit",
                "provider_backend_key": "magicfit",
                "acceptance_status": "pending",
                "launch_eligible": False,
            },
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    try:
        with bounded_lane_lock("magicfit-import"):
            return _main_unlocked()
    except (TourHostSafetyError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
