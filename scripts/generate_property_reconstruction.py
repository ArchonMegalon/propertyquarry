#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import ctypes
import fcntl
import hashlib
import html
import io
import json
import math
import os
import re
import secrets
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import warnings
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterable, Iterator

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIRECTORY = Path(__file__).resolve().parent
EA_ROOT = ROOT / "ea"
for _dependency_root in (ROOT, SCRIPT_DIRECTORY, EA_ROOT):
    if str(_dependency_root) not in sys.path:
        sys.path.insert(0, str(_dependency_root))

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright
except Exception:
    PlaywrightTimeoutError = RuntimeError  # type: ignore[assignment]
    sync_playwright = None  # type: ignore[assignment]

from scripts.property_tour_governed_reservation import (
    require_dynamic_tour_slug,
)

try:
    from scripts.property_tour_runtime_paths import preferred_public_tour_root, running_container_public_tour_dir
except Exception:
    preferred_public_tour_root = None  # type: ignore[assignment]
    running_container_public_tour_dir = None  # type: ignore[assignment]

try:
    from scripts.propertyquarry_playwright_runtime import (
        playwright_chromium_capture_available as _playwright_chromium_capture_available,
        playwright_chromium_executable as _playwright_chromium_executable,
        playwright_chromium_launch_kwargs as _playwright_chromium_launch_kwargs,
    )
except ModuleNotFoundError:
    from propertyquarry_playwright_runtime import (  # type: ignore[no-redef]
        playwright_chromium_capture_available as _playwright_chromium_capture_available,
        playwright_chromium_executable as _playwright_chromium_executable,
        playwright_chromium_launch_kwargs as _playwright_chromium_launch_kwargs,
    )

try:
    from scripts.property_reconstruction_styles import (
        FLOORPLAN_DISPLAY_MODE,
        GENERATED_RECONSTRUCTION_VIEWER_VERSION,
        STYLE_SCENE_CONTRACT_VERSION,
        build_style_scene,
        reconstruction_style,
        validate_style_scene,
    )
except ModuleNotFoundError:
    from property_reconstruction_styles import (  # type: ignore[no-redef]
        FLOORPLAN_DISPLAY_MODE,
        GENERATED_RECONSTRUCTION_VIEWER_VERSION,
        STYLE_SCENE_CONTRACT_VERSION,
        build_style_scene,
        reconstruction_style,
        validate_style_scene,
    )


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
VIEWER_VERSION = GENERATED_RECONSTRUCTION_VIEWER_VERSION
DISCLOSURE = "Planning preview built from supplied source material. Use it as a layout aid, not as a captured tour."
WALKTHROUGH_VIEWPORT_SIZE = (1280, 720)
WALKTHROUGH_CARD_SIZE = (1440, 810)
WALKTHROUGH_MAP_BOX = (988, 222, 1332, 452)
WALKTHROUGH_OUTPUT_FPS = 12
MIN_WALKTHROUGH_DURATION_SECONDS = 30.0
MAX_WALKTHROUGH_DURATION_SECONDS = 240.0
MAX_WALKTHROUGH_ENCODED_FRAMES = int(
    MAX_WALKTHROUGH_DURATION_SECONDS * WALKTHROUGH_OUTPUT_FPS
)
THREE_VENDOR_VERSION = "0.167.1"
THREE_VENDOR_ROOT = ROOT / "vendor" / "three" / THREE_VENDOR_VERSION
THREE_MODULE_SOURCE = THREE_VENDOR_ROOT / "three.module.js"
ORBIT_CONTROLS_SOURCE = THREE_VENDOR_ROOT / "examples" / "jsm" / "controls" / "OrbitControls.js"
THREE_LICENSE_SOURCE = THREE_VENDOR_ROOT / "LICENSE"
THREE_UPSTREAM_GIT_HEAD = "42a2f6aac8cffebb29524d68eb7136a756f15960"
THREE_UPSTREAM_DIST_INTEGRITY = "sha512-gYTLJA/UQip6J/tJvl91YYqlZF47+D/kxiWrbTon35ZHlXEN0VOo+Qke2walF1/x92v55H6enomymg4Dak52kw=="
THREE_UPSTREAM_DIST_SHASUM = "3fe4ba2b0a03fd662afe4977a56803d955b61689"
THREE_MODULE_SOURCE_SHA256 = "5289ca2dfde8572bd7715b9fa2ca929db12bae87e9a2cb53e431662df7039506"
ORBIT_CONTROLS_SOURCE_SHA256 = "f260591ef315aa04888152e7f121865214e33fb54727145cf4e4445058db1297"
THREE_LICENSE_SOURCE_SHA256 = "4c40a1ef62450b857c3b2aaf294936304cd552d965fbcd9d32d4c5bcf4ba4454"
THREE_LICENSE_NOTICE_SHA256 = "0fc0f3407d472c50739a5339896a5b704dcb35b9d1fa6985cf8800ca6debba23"
THREE_MODULE_EMITTED_SHA256 = "2fdbd590b5a285d9a9b1aa39dcba2d41fd8b7749361a84fcef1fc422696996ed"
ORBIT_CONTROLS_TRANSFORMED_SHA256 = "f70d0bcb05e03d18b1ebd4e63599fc6c11957e9703c7310fcd02e8cf76aa6e6f"
ORBIT_CONTROLS_EMITTED_SHA256 = "b15a310c930ed4ba3e26cae34931f145a9d3fb82741339563dcb623d1eedd18b"
ORBIT_CONTROLS_BARE_IMPORT = "} from 'three';"
ORBIT_CONTROLS_RELATIVE_IMPORT = "} from '../../../three.module.js';"
GLB_EXPORTER_VERSION = "propertyquarry_deterministic_glb_v1"
GLB_MAX_OBJ_BYTES = 64 * 1024 * 1024
GLB_MAX_MTL_BYTES = 1024 * 1024
GLB_MAX_SOURCE_VERTICES = 1_000_000
GLB_MAX_TRIANGLES = 2_000_000
_PREVIEW_FONT_PATHS = {
    ("sans", False): Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ("sans", True): Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("serif", False): Path("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"),
    ("serif", True): Path("/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"),
}
_PREVIEW_FONT_CACHE: dict[tuple[str, bool, int], ImageFont.ImageFont] = {}
_RENAME_EXCHANGE = 2
_RENAME_NOREPLACE = 1
_PUBLIC_BUNDLE_STAGE_PREFIX = ".propertyquarry-stage-"
_PUBLIC_BUNDLE_QUARANTINE_PREFIX = ".propertyquarry-quarantine-"
_PUBLIC_BUNDLE_INPUT_DIR = ".propertyquarry-inputs"
_PUBLIC_BUNDLE_COMMIT_MARKER = ".propertyquarry-render-commit.json"
_PUBLIC_BUNDLE_STAGE_OWNER_MARKER = ".propertyquarry-stage-owner.json"
_PUBLIC_TOUR_SLUG_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_MAX_TRANSACTION_TREE_ENTRIES = 50_000
_MAX_TRANSACTION_TREE_BYTES = 4 * 1024 * 1024 * 1024
_MAX_TRANSACTION_FILE_BYTES = 1024 * 1024 * 1024
_MAX_TRANSACTION_TREE_DEPTH = 64
_MAX_TRANSACTION_INPUTS = 65
_MAX_SOURCE_IMAGE_COMPRESSED_BYTES = 128 * 1024 * 1024
_MIN_SOURCE_IMAGE_DIMENSION = 16
_MAX_SOURCE_IMAGE_DIMENSION = 16_384
_MAX_SOURCE_IMAGE_PIXELS = 64_000_000
_MAX_SOURCE_IMAGE_ASPECT_RATIO = 32.0
_MAX_FLOORPLAN_DERIVED_DIMENSION = 4_096
_MAX_FLOORPLAN_DERIVED_PIXELS = 4_000_000


class _PublicBundleTransactionError(RuntimeError):
    """Stable, path-free transactional publication failure."""


class _SourceImageInvalid(RuntimeError):
    """Stable, path-free rejection for an unsafe or invalid image."""


def _directory_path_matches(
    *,
    parent_fd: int,
    name: str,
    expected: os.stat_result,
) -> bool:
    try:
        observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    return bool(
        stat.S_ISDIR(observed.st_mode)
        and not stat.S_ISLNK(observed.st_mode)
        and observed.st_dev == expected.st_dev
        and observed.st_ino == expected.st_ino
        and stat.S_IMODE(observed.st_mode) == stat.S_IMODE(expected.st_mode)
    )


def _directory_path_identity_matches(
    *,
    parent_fd: int,
    name: str,
    expected: os.stat_result,
) -> bool:
    try:
        observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    return bool(
        stat.S_ISDIR(observed.st_mode)
        and not stat.S_ISLNK(observed.st_mode)
        and observed.st_dev == expected.st_dev
        and observed.st_ino == expected.st_ino
    )


class _PublicBundleTransaction:
    def __init__(
        self,
        *,
        root: Path,
        root_fd: int,
        slug: str,
        stage_name: str,
        source_fingerprint: str,
        stage_identity: tuple[int, int],
        stage_fd: int,
    ) -> None:
        self.root = root
        self.root_fd = root_fd
        self.slug = slug
        self.stage_name = stage_name
        self.stage_dir = root / stage_name
        self.live_dir = root / slug
        self.source_fingerprint = source_fingerprint
        self.stage_identity = stage_identity
        self.stage_fd = stage_fd
        self.input_snapshot_identity: tuple[int, int] | None = None
        self.input_snapshot_fd = -1
        self.published = False
        self.preserve_stage_on_failure = False
        self.durability_status = "not_started"
        self.cleanup_status = "not_started"
        self._replaced_bundle_identity: tuple[int, int] | None = None

    def publish(
        self,
        *,
        reconstruction_subdir: str,
        require_walkthrough: bool = False,
    ) -> None:
        if self.published:
            raise _PublicBundleTransactionError("bundle_transaction_already_published")
        try:
            stage_fd = os.dup(self.stage_fd)
            retained_stage = os.fstat(stage_fd)
        except OSError as exc:
            raise _PublicBundleTransactionError("bundle_stage_invalid") from exc
        if self.stage_identity != (
            int(retained_stage.st_dev),
            int(retained_stage.st_ino),
        ) or not _directory_path_identity_matches(
            parent_fd=self.root_fd,
            name=self.stage_name,
            expected=retained_stage,
        ):
            os.close(stage_fd)
            raise _PublicBundleTransactionError("bundle_stage_changed_before_exchange")
        try:
            _disarm_transaction_stage_reaper(
                stage_fd,
                stage_name=self.stage_name,
            )
            _normalize_public_generation_surface(
                stage_fd,
                reconstruction_subdir=reconstruction_subdir,
            )
            _validate_candidate_generation_manifest(
                stage_fd,
                slug=self.slug,
                reconstruction_subdir=reconstruction_subdir,
                require_walkthrough=require_walkthrough,
            )
            _sync_and_validate_bundle_tree(stage_fd)
            candidate_fingerprint = _public_tree_fingerprint(stage_fd)
            opened_stage = os.fstat(stage_fd)
            if not _directory_path_matches(
                parent_fd=self.root_fd,
                name=self.stage_name,
                expected=opened_stage,
            ):
                raise _PublicBundleTransactionError("bundle_stage_changed_before_exchange")

            # Candidate work is complete before the compare-and-exchange
            # check. Keep both descriptors pinned through the exchange and its
            # postconditions so the names can never be mistaken for the trees
            # that were actually checked.
            live_fd = _open_public_bundle_directory(
                self.slug,
                parent_fd=self.root_fd,
                failure="tour_bundle_invalid",
            )
            try:
                opened_live = os.fstat(live_fd)
                if _public_tree_fingerprint(live_fd) != self.source_fingerprint:
                    raise _PublicBundleTransactionError(
                        "live_bundle_changed_during_generation"
                    )
                if not _directory_path_matches(
                    parent_fd=self.root_fd,
                    name=self.slug,
                    expected=opened_live,
                ):
                    raise _PublicBundleTransactionError(
                        "live_bundle_changed_during_generation"
                    )

                # Once renameat2 is entered, preserve both names on every
                # exceptional path. A failed/ambiguous rollback must never fall
                # through to generic stage cleanup.
                self.preserve_stage_on_failure = True
                self.cleanup_status = "preserved_after_exchange_failure"
                _exchange_public_bundle_directories(
                    root_fd=self.root_fd,
                    stage_name=self.stage_name,
                    slug=self.slug,
                )

                try:
                    exchange_verified = bool(
                        _directory_path_matches(
                            parent_fd=self.root_fd,
                            name=self.slug,
                            expected=opened_stage,
                        )
                        and _directory_path_matches(
                            parent_fd=self.root_fd,
                            name=self.stage_name,
                            expected=opened_live,
                        )
                        and _public_tree_fingerprint(stage_fd)
                        == candidate_fingerprint
                        and _public_tree_fingerprint(live_fd)
                        == self.source_fingerprint
                        and _directory_path_matches(
                            parent_fd=self.root_fd,
                            name=self.slug,
                            expected=os.fstat(stage_fd),
                        )
                        and _directory_path_matches(
                            parent_fd=self.root_fd,
                            name=self.stage_name,
                            expected=os.fstat(live_fd),
                        )
                    )
                except (OSError, _PublicBundleTransactionError):
                    exchange_verified = False

                if not exchange_verified:
                    # Invert the exchange only when at least one name still
                    # anchors a known pre-exchange inode. If live is the exact
                    # candidate, pin whatever arrived at stage (a last-window
                    # live replacement). Symmetrically, if stage is the exact
                    # old live tree, pin whatever arrived at live (a last-window
                    # stage replacement). With neither anchor, touching either
                    # name could exchange an unrelated concurrent tree.
                    try:
                        if _directory_path_matches(
                            parent_fd=self.root_fd,
                            name=self.slug,
                            expected=opened_stage,
                        ):
                            rollback_live = os.stat(
                                self.stage_name,
                                dir_fd=self.root_fd,
                                follow_symlinks=False,
                            )
                            rollback_stage = opened_stage
                        elif _directory_path_matches(
                            parent_fd=self.root_fd,
                            name=self.stage_name,
                            expected=opened_live,
                        ):
                            rollback_live = opened_live
                            rollback_stage = os.stat(
                                self.slug,
                                dir_fd=self.root_fd,
                                follow_symlinks=False,
                            )
                        else:
                            raise _PublicBundleTransactionError(
                                "atomic_bundle_exchange_rollback_refused"
                            )
                    except OSError as exc:
                        raise _PublicBundleTransactionError(
                            "atomic_bundle_exchange_rollback_refused"
                        ) from exc
                    if not (
                        stat.S_ISDIR(rollback_live.st_mode)
                        and stat.S_ISDIR(rollback_stage.st_mode)
                    ):
                        raise _PublicBundleTransactionError(
                            "atomic_bundle_exchange_rollback_refused"
                        )
                    try:
                        _exchange_public_bundle_directories(
                            root_fd=self.root_fd,
                            stage_name=self.stage_name,
                            slug=self.slug,
                        )
                    except _PublicBundleTransactionError as exc:
                        raise _PublicBundleTransactionError(
                            "atomic_bundle_exchange_rollback_failed"
                        ) from exc
                    if not (
                        _directory_path_matches(
                            parent_fd=self.root_fd,
                            name=self.slug,
                            expected=rollback_live,
                        )
                        and _directory_path_matches(
                            parent_fd=self.root_fd,
                            name=self.stage_name,
                            expected=rollback_stage,
                        )
                    ):
                        raise _PublicBundleTransactionError(
                            "atomic_bundle_exchange_rollback_unverified"
                        )
                    try:
                        os.fsync(self.root_fd)
                    except OSError as exc:
                        raise _PublicBundleTransactionError(
                            "atomic_bundle_exchange_rollback_durability_unverified"
                        ) from exc
                    raise _PublicBundleTransactionError(
                        "live_bundle_changed_during_atomic_exchange"
                    )

                # Set the commit state before fallible descriptor cleanup. The
                # old live identity is retained so cleanup will refuse a stage
                # name that was replaced after the verified exchange.
                self._replaced_bundle_identity = (
                    int(opened_live.st_dev),
                    int(opened_live.st_ino),
                )
                self.published = True
                self.preserve_stage_on_failure = False
                self.cleanup_status = "not_started"
            finally:
                os.close(live_fd)
        finally:
            os.close(stage_fd)
        try:
            os.fsync(self.root_fd)
        except OSError:
            self.durability_status = "unverified"
        else:
            self.durability_status = "fsynced"

    def cleanup_replaced_bundle(self) -> None:
        if not self.published:
            raise _PublicBundleTransactionError("bundle_cleanup_before_publish")
        if self.preserve_stage_on_failure:
            raise _PublicBundleTransactionError("bundle_cleanup_preservation_required")
        try:
            current_stage = os.stat(
                self.stage_name,
                dir_fd=self.root_fd,
                follow_symlinks=False,
            )
        except OSError:
            self.cleanup_status = "preserved_after_stage_change"
            return
        if self._replaced_bundle_identity != (
            int(current_stage.st_dev),
            int(current_stage.st_ino),
        ):
            self.cleanup_status = "preserved_after_stage_change"
            return
        try:
            _remove_transaction_stage(
                self.root_fd,
                self.stage_name,
                expected_identity=self._replaced_bundle_identity,
            )
        except _PublicBundleTransactionError:
            self.cleanup_status = "deferred"
            return
        self.cleanup_status = "removed"

    def quarantine_preserved_stage(self) -> None:
        if not self.preserve_stage_on_failure:
            return
        if not self.stage_name.startswith(_PUBLIC_BUNDLE_STAGE_PREFIX):
            return
        quarantine_name = self.stage_name.replace(
            _PUBLIC_BUNDLE_STAGE_PREFIX,
            _PUBLIC_BUNDLE_QUARANTINE_PREFIX,
            1,
        )
        try:
            _rename_public_bundle_directory_noreplace(
                root_fd=self.root_fd,
                source_name=self.stage_name,
                destination_name=quarantine_name,
            )
            os.fsync(self.root_fd)
        except (OSError, _PublicBundleTransactionError):
            # The stage-owner marker was disarmed before the exchange, so even
            # a quarantine failure remains ineligible for ordinary reaping.
            self.cleanup_status = "preservation_unverified"
            return
        self.stage_name = quarantine_name
        self.stage_dir = self.root / quarantine_name
        self.cleanup_status = "quarantined_after_exchange_failure"

    @property
    def anchored_stage_dir(self) -> Path:
        return Path(f"/proc/self/fd/{self.stage_fd}")


def _open_public_bundle_directory(
    name: str,
    *,
    parent_fd: int,
    failure: str,
) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        expected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        descriptor = os.open(name, flags, dir_fd=parent_fd)
        observed = os.fstat(descriptor)
    except OSError as exc:
        raise _PublicBundleTransactionError(failure) from exc
    if (
        not stat.S_ISDIR(expected.st_mode)
        or not stat.S_ISDIR(observed.st_mode)
        or expected.st_dev != observed.st_dev
        or expected.st_ino != observed.st_ino
    ):
        os.close(descriptor)
        raise _PublicBundleTransactionError(failure)
    return descriptor


def _open_directory_anchor(path: Path) -> tuple[int, os.stat_result]:
    """Open a directory without following a caller-controlled final symlink.

    Exact ``/proc/self/fd/N`` paths are internal retained-descriptor anchors, so
    duplicate the descriptor instead of reopening the procfs symlink.  Ordinary
    paths retain the existing lstat/open/fstat identity check.
    """

    proc_match = re.fullmatch(r"/proc/self/fd/([0-9]+)", os.fspath(path))
    if proc_match is not None:
        descriptor = os.dup(int(proc_match.group(1)))
        observed = os.fstat(descriptor)
        if not stat.S_ISDIR(observed.st_mode):
            os.close(descriptor)
            raise NotADirectoryError(os.fspath(path))
        return descriptor, observed

    expected = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(expected.st_mode):
        raise NotADirectoryError(os.fspath(path))
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags)
    observed = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(observed.st_mode)
        or observed.st_dev != expected.st_dev
        or observed.st_ino != expected.st_ino
    ):
        os.close(descriptor)
        raise OSError("directory_anchor_changed")
    return descriptor, observed


def _copy_regular_file_between_directories(
    name: str,
    *,
    source_fd: int,
    target_fd: int,
    expected: os.stat_result,
) -> None:
    read_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    write_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    source_handle = -1
    target_handle = -1
    try:
        source_handle = os.open(name, read_flags, dir_fd=source_fd)
        observed = os.fstat(source_handle)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_dev != expected.st_dev
            or observed.st_ino != expected.st_ino
        ):
            raise _PublicBundleTransactionError("bundle_source_entry_changed")
        target_handle = os.open(name, write_flags, 0o600, dir_fd=target_fd)
        while True:
            chunk = os.read(source_handle, 1024 * 1024)
            if not chunk:
                break
            offset = 0
            while offset < len(chunk):
                written = os.write(target_handle, chunk[offset:])
                if written <= 0:
                    raise _PublicBundleTransactionError("bundle_stage_copy_failed")
                offset += written
        os.fchmod(target_handle, stat.S_IMODE(expected.st_mode))
        os.fsync(target_handle)
    except _PublicBundleTransactionError:
        raise
    except OSError as exc:
        raise _PublicBundleTransactionError("bundle_stage_copy_failed") from exc
    finally:
        if target_handle >= 0:
            os.close(target_handle)
        if source_handle >= 0:
            os.close(source_handle)


def _consume_transaction_tree_budget(
    budget: dict[str, int],
    *,
    size_bytes: int,
) -> None:
    budget["entries"] = int(budget.get("entries") or 0) + 1
    budget["bytes"] = int(budget.get("bytes") or 0) + max(0, int(size_bytes))
    if budget["entries"] > _MAX_TRANSACTION_TREE_ENTRIES:
        raise _PublicBundleTransactionError("bundle_tree_entry_limit_exceeded")
    if budget["bytes"] > _MAX_TRANSACTION_TREE_BYTES:
        raise _PublicBundleTransactionError("bundle_tree_size_limit_exceeded")


def _clone_public_tree(
    source_fd: int,
    target_fd: int,
    *,
    depth: int = 0,
    budget: dict[str, int] | None = None,
) -> None:
    if depth > _MAX_TRANSACTION_TREE_DEPTH:
        raise _PublicBundleTransactionError("bundle_tree_depth_limit_exceeded")
    tree_budget = budget if budget is not None else {"entries": 0, "bytes": 0}
    try:
        names = sorted(os.listdir(source_fd))
    except OSError as exc:
        raise _PublicBundleTransactionError("bundle_source_invalid") from exc
    for name in names:
        if not name or len(os.fsencode(name)) > 255:
            raise _PublicBundleTransactionError("bundle_source_entry_name_invalid")
        try:
            expected = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        except OSError as exc:
            raise _PublicBundleTransactionError("bundle_source_entry_invalid") from exc
        if stat.S_ISLNK(expected.st_mode):
            raise _PublicBundleTransactionError("bundle_source_symlink_forbidden")
        if stat.S_ISREG(expected.st_mode):
            if expected.st_size > _MAX_TRANSACTION_FILE_BYTES:
                raise _PublicBundleTransactionError(
                    "bundle_source_file_size_limit_exceeded"
                )
            _consume_transaction_tree_budget(
                tree_budget,
                size_bytes=expected.st_size,
            )
            _copy_regular_file_between_directories(
                name,
                source_fd=source_fd,
                target_fd=target_fd,
                expected=expected,
            )
            continue
        if not stat.S_ISDIR(expected.st_mode):
            raise _PublicBundleTransactionError("bundle_source_special_entry_forbidden")
        _consume_transaction_tree_budget(tree_budget, size_bytes=0)
        try:
            os.mkdir(name, 0o700, dir_fd=target_fd)
        except OSError as exc:
            raise _PublicBundleTransactionError("bundle_stage_copy_failed") from exc
        source_child = _open_public_bundle_directory(
            name,
            parent_fd=source_fd,
            failure="bundle_source_entry_changed",
        )
        target_child = _open_public_bundle_directory(
            name,
            parent_fd=target_fd,
            failure="bundle_stage_copy_failed",
        )
        try:
            os.fchmod(target_child, stat.S_IMODE(expected.st_mode))
            _clone_public_tree(
                source_child,
                target_child,
                depth=depth + 1,
                budget=tree_budget,
            )
            os.fsync(target_child)
        finally:
            os.close(target_child)
            os.close(source_child)


def _update_public_tree_fingerprint(
    directory_fd: int,
    digest: Any,
    *,
    prefix: str = "",
    depth: int = 0,
    budget: dict[str, int] | None = None,
) -> None:
    if depth > _MAX_TRANSACTION_TREE_DEPTH:
        raise _PublicBundleTransactionError("bundle_tree_depth_limit_exceeded")
    tree_budget = budget if budget is not None else {"entries": 0, "bytes": 0}
    try:
        opened_directory = os.fstat(directory_fd)
        if not stat.S_ISDIR(opened_directory.st_mode):
            raise _PublicBundleTransactionError("bundle_tree_fingerprint_failed")
        names = sorted(os.listdir(directory_fd))
    except OSError as exc:
        raise _PublicBundleTransactionError("bundle_tree_fingerprint_failed") from exc
    for name in names:
        encoded_name = os.fsencode(name)
        if not name or len(encoded_name) > 255:
            raise _PublicBundleTransactionError("bundle_tree_entry_name_invalid")
        relpath = f"{prefix}/{name}" if prefix else name
        encoded_relpath = os.fsencode(relpath)
        try:
            expected = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise _PublicBundleTransactionError("bundle_tree_fingerprint_failed") from exc
        if stat.S_ISLNK(expected.st_mode):
            raise _PublicBundleTransactionError("bundle_tree_symlink_forbidden")
        if stat.S_ISDIR(expected.st_mode):
            _consume_transaction_tree_budget(tree_budget, size_bytes=0)
            digest.update(b"D\0")
            digest.update(len(encoded_relpath).to_bytes(4, "big"))
            digest.update(encoded_relpath)
            digest.update(stat.S_IMODE(expected.st_mode).to_bytes(2, "big"))
            child_fd = _open_public_bundle_directory(
                name,
                parent_fd=directory_fd,
                failure="bundle_tree_entry_changed",
            )
            try:
                _update_public_tree_fingerprint(
                    child_fd,
                    digest,
                    prefix=relpath,
                    depth=depth + 1,
                    budget=tree_budget,
                )
            finally:
                os.close(child_fd)
            try:
                final_entry = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise _PublicBundleTransactionError(
                    "bundle_tree_entry_changed"
                ) from exc
            if (
                final_entry.st_dev != expected.st_dev
                or final_entry.st_ino != expected.st_ino
                or stat.S_IMODE(final_entry.st_mode)
                != stat.S_IMODE(expected.st_mode)
            ):
                raise _PublicBundleTransactionError("bundle_tree_entry_changed")
            continue
        if not stat.S_ISREG(expected.st_mode):
            raise _PublicBundleTransactionError("bundle_tree_special_entry_forbidden")
        if expected.st_size > _MAX_TRANSACTION_FILE_BYTES:
            raise _PublicBundleTransactionError("bundle_tree_file_size_limit_exceeded")
        _consume_transaction_tree_budget(tree_budget, size_bytes=expected.st_size)
        digest.update(b"F\0")
        digest.update(len(encoded_relpath).to_bytes(4, "big"))
        digest.update(encoded_relpath)
        digest.update(stat.S_IMODE(expected.st_mode).to_bytes(2, "big"))
        digest.update(int(expected.st_size).to_bytes(8, "big"))
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            asset_fd = os.open(name, flags, dir_fd=directory_fd)
            opened = os.fstat(asset_fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != expected.st_dev
                or opened.st_ino != expected.st_ino
                or opened.st_size != expected.st_size
            ):
                raise _PublicBundleTransactionError("bundle_tree_entry_changed")
            while True:
                chunk = os.read(asset_fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            final = os.fstat(asset_fd)
            if (
                final.st_size != opened.st_size
                or final.st_mtime_ns != opened.st_mtime_ns
                or final.st_ctime_ns != opened.st_ctime_ns
            ):
                raise _PublicBundleTransactionError("bundle_tree_entry_changed")
            final_entry = os.stat(
                name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if (
                final_entry.st_dev != expected.st_dev
                or final_entry.st_ino != expected.st_ino
                or stat.S_IMODE(final_entry.st_mode)
                != stat.S_IMODE(expected.st_mode)
            ):
                raise _PublicBundleTransactionError("bundle_tree_entry_changed")
        except _PublicBundleTransactionError:
            raise
        except OSError as exc:
            raise _PublicBundleTransactionError("bundle_tree_fingerprint_failed") from exc
        finally:
            if "asset_fd" in locals():
                os.close(asset_fd)
                del asset_fd
    try:
        final_names = sorted(os.listdir(directory_fd))
        final_directory = os.fstat(directory_fd)
    except OSError as exc:
        raise _PublicBundleTransactionError("bundle_tree_fingerprint_failed") from exc
    if (
        final_names != names
        or final_directory.st_dev != opened_directory.st_dev
        or final_directory.st_ino != opened_directory.st_ino
        or final_directory.st_mtime_ns != opened_directory.st_mtime_ns
        or final_directory.st_ctime_ns != opened_directory.st_ctime_ns
        or stat.S_IMODE(final_directory.st_mode)
        != stat.S_IMODE(opened_directory.st_mode)
    ):
        raise _PublicBundleTransactionError("bundle_tree_changed_during_fingerprint")


def _public_tree_fingerprint(directory_fd: int) -> str:
    digest = hashlib.sha256()
    _update_public_tree_fingerprint(directory_fd, digest)
    return digest.hexdigest()


def _sync_and_validate_bundle_tree(directory_fd: int) -> None:
    try:
        names = sorted(os.listdir(directory_fd))
    except OSError as exc:
        raise _PublicBundleTransactionError("bundle_stage_invalid") from exc
    for name in names:
        try:
            expected = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise _PublicBundleTransactionError("bundle_stage_entry_invalid") from exc
        if stat.S_ISLNK(expected.st_mode):
            raise _PublicBundleTransactionError("bundle_stage_symlink_forbidden")
        if stat.S_ISDIR(expected.st_mode):
            child_fd = _open_public_bundle_directory(
                name,
                parent_fd=directory_fd,
                failure="bundle_stage_entry_invalid",
            )
            try:
                _sync_and_validate_bundle_tree(child_fd)
            finally:
                os.close(child_fd)
            continue
        if not stat.S_ISREG(expected.st_mode):
            raise _PublicBundleTransactionError("bundle_stage_special_entry_forbidden")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            asset_fd = os.open(name, flags, dir_fd=directory_fd)
            observed = os.fstat(asset_fd)
            if (
                not stat.S_ISREG(observed.st_mode)
                or observed.st_dev != expected.st_dev
                or observed.st_ino != expected.st_ino
            ):
                raise _PublicBundleTransactionError("bundle_stage_entry_changed")
            os.fsync(asset_fd)
        except _PublicBundleTransactionError:
            raise
        except OSError as exc:
            raise _PublicBundleTransactionError("bundle_stage_entry_invalid") from exc
        finally:
            if "asset_fd" in locals():
                os.close(asset_fd)
                del asset_fd
    os.fsync(directory_fd)


def _open_regular_public_entry(
    directory_fd: int,
    name: str,
    *,
    failure: str,
) -> int:
    try:
        expected = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        observed = os.fstat(descriptor)
    except OSError as exc:
        raise _PublicBundleTransactionError(failure) from exc
    if (
        not stat.S_ISREG(expected.st_mode)
        or not stat.S_ISREG(observed.st_mode)
        or expected.st_dev != observed.st_dev
        or expected.st_ino != observed.st_ino
    ):
        os.close(descriptor)
        raise _PublicBundleTransactionError(failure)
    return descriptor


def _open_public_directory_path(directory_fd: int, relpath: str) -> int:
    normalized = PurePosixPath(str(relpath or ""))
    parts = normalized.parts
    if (
        normalized.is_absolute()
        or not parts
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise _PublicBundleTransactionError("bundle_reconstruction_invalid")
    current_fd = os.dup(directory_fd)
    for part in parts:
        try:
            child_fd = _open_public_bundle_directory(
                part,
                parent_fd=current_fd,
                failure="bundle_reconstruction_invalid",
            )
        except Exception:
            os.close(current_fd)
            raise
        os.close(current_fd)
        current_fd = child_fd
    return current_fd


def _normalize_public_subtree(directory_fd: int) -> None:
    try:
        os.fchmod(directory_fd, 0o755)
        names = sorted(os.listdir(directory_fd))
    except OSError as exc:
        raise _PublicBundleTransactionError("public_generation_tree_invalid") from exc
    for name in names:
        try:
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise _PublicBundleTransactionError("public_generation_entry_invalid") from exc
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = _open_public_bundle_directory(
                name,
                parent_fd=directory_fd,
                failure="public_generation_entry_invalid",
            )
            try:
                _normalize_public_subtree(child_fd)
            finally:
                os.close(child_fd)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise _PublicBundleTransactionError("public_generation_entry_invalid")
        asset_fd = _open_regular_public_entry(
            directory_fd,
            name,
            failure="public_generation_entry_invalid",
        )
        try:
            os.fchmod(asset_fd, 0o644)
            os.fsync(asset_fd)
        finally:
            os.close(asset_fd)
    os.fsync(directory_fd)


def _normalize_public_generation_surface(
    bundle_fd: int,
    *,
    reconstruction_subdir: str,
) -> None:
    try:
        os.fchmod(bundle_fd, 0o755)
    except OSError as exc:
        raise _PublicBundleTransactionError("bundle_stage_invalid") from exc
    manifest_fd = _open_regular_public_entry(
        bundle_fd,
        "tour.json",
        failure="bundle_manifest_invalid",
    )
    try:
        os.fchmod(manifest_fd, 0o644)
        os.fsync(manifest_fd)
    finally:
        os.close(manifest_fd)
    reconstruction_fd = _open_public_directory_path(
        bundle_fd,
        reconstruction_subdir,
    )
    try:
        _normalize_public_subtree(reconstruction_fd)
    finally:
        os.close(reconstruction_fd)
    for preview_name in ("diorama-preview.png", "telegram-preview.png"):
        try:
            preview_fd = _open_regular_public_entry(
                bundle_fd,
                preview_name,
                failure="bundle_preview_invalid",
            )
        except _PublicBundleTransactionError:
            try:
                os.stat(preview_name, dir_fd=bundle_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise _PublicBundleTransactionError(
                    "bundle_preview_invalid"
                ) from exc
            raise
        try:
            os.fchmod(preview_fd, 0o644)
            os.fsync(preview_fd)
        finally:
            os.close(preview_fd)
    os.fsync(bundle_fd)


def _read_bounded_json_object(
    descriptor: int,
    *,
    failure: str,
    maximum_bytes: int = 8 * 1024 * 1024,
) -> dict[str, object]:
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_size < 2 or metadata.st_size > maximum_bytes:
            raise _PublicBundleTransactionError(failure)
        os.lseek(descriptor, 0, os.SEEK_SET)
        payload = bytearray()
        while len(payload) <= maximum_bytes:
            chunk = os.read(descriptor, min(1024 * 1024, maximum_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > maximum_bytes:
            raise _PublicBundleTransactionError(failure)
        final_metadata = os.fstat(descriptor)
        if (
            final_metadata.st_dev != metadata.st_dev
            or final_metadata.st_ino != metadata.st_ino
            or final_metadata.st_size != metadata.st_size
            or final_metadata.st_mtime_ns != metadata.st_mtime_ns
            or final_metadata.st_ctime_ns != metadata.st_ctime_ns
        ):
            raise _PublicBundleTransactionError(failure)
        decoded = json.loads(bytes(payload).decode("utf-8"))
    except _PublicBundleTransactionError:
        raise
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise _PublicBundleTransactionError(failure) from exc
    if not isinstance(decoded, dict):
        raise _PublicBundleTransactionError(failure)
    return decoded


def _write_transaction_stage_owner_marker(
    stage_fd: int,
    *,
    stage_name: str,
) -> None:
    opened_stage = os.fstat(stage_fd)
    payload = (
        json.dumps(
            {
                "schema": "propertyquarry.transaction_stage_owner.v1",
                "stage_name": stage_name,
                "stage_dev": int(opened_stage.st_dev),
                "stage_ino": int(opened_stage.st_ino),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    descriptor = -1
    try:
        descriptor = os.open(
            _PUBLIC_BUNDLE_STAGE_OWNER_MARKER,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=stage_fd,
        )
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("stage_owner_marker_short_write")
            offset += written
        os.fsync(descriptor)
        os.fsync(stage_fd)
    except OSError as exc:
        raise _PublicBundleTransactionError("bundle_stage_owner_marker_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _transaction_stage_owner_marker_matches(
    stage_fd: int,
    *,
    stage_name: str,
) -> bool:
    marker_fd = -1
    try:
        marker_fd = _open_regular_public_entry(
            stage_fd,
            _PUBLIC_BUNDLE_STAGE_OWNER_MARKER,
            failure="bundle_stage_owner_marker_invalid",
        )
        marker = _read_bounded_json_object(
            marker_fd,
            failure="bundle_stage_owner_marker_invalid",
            maximum_bytes=4_096,
        )
        opened_stage = os.fstat(stage_fd)
    except (OSError, _PublicBundleTransactionError):
        return False
    finally:
        if marker_fd >= 0:
            os.close(marker_fd)
    return marker == {
        "schema": "propertyquarry.transaction_stage_owner.v1",
        "stage_name": stage_name,
        "stage_dev": int(opened_stage.st_dev),
        "stage_ino": int(opened_stage.st_ino),
    }


def _disarm_transaction_stage_reaper(
    stage_fd: int,
    *,
    stage_name: str,
) -> None:
    if not _transaction_stage_owner_marker_matches(
        stage_fd,
        stage_name=stage_name,
    ):
        raise _PublicBundleTransactionError("bundle_stage_owner_marker_invalid")
    try:
        os.unlink(_PUBLIC_BUNDLE_STAGE_OWNER_MARKER, dir_fd=stage_fd)
        os.fsync(stage_fd)
    except OSError as exc:
        raise _PublicBundleTransactionError("bundle_stage_owner_marker_failed") from exc


def _require_regular_generated_asset(
    reconstruction_fd: int,
    name: str,
) -> None:
    descriptor = _open_regular_public_entry(
        reconstruction_fd,
        name,
        failure="candidate_generated_asset_invalid",
    )
    os.close(descriptor)


def _validate_candidate_generation_manifest(
    bundle_fd: int,
    *,
    slug: str,
    reconstruction_subdir: str,
    require_walkthrough: bool,
) -> None:
    manifest_fd = _open_regular_public_entry(
        bundle_fd,
        "tour.json",
        failure="candidate_manifest_invalid",
    )
    try:
        manifest = _read_bounded_json_object(
            manifest_fd,
            failure="candidate_manifest_invalid",
        )
    finally:
        os.close(manifest_fd)
    if str(manifest.get("slug") or "").strip() != slug:
        raise _PublicBundleTransactionError("candidate_manifest_slug_mismatch")
    generated = manifest.get("generated_reconstruction")
    if not isinstance(generated, dict) or str(generated.get("provider") or "") != (
        "propertyquarry_generated_reconstruction"
    ):
        raise _PublicBundleTransactionError("candidate_manifest_generation_invalid")
    base = PurePosixPath(reconstruction_subdir).as_posix()
    required_relpaths = {
        "viewer_relpath": f"{base}/viewer.html",
        "model_relpath": f"{base}/model.obj",
        "material_relpath": f"{base}/model.mtl",
        "manifest_relpath": f"{base}/reconstruction.json",
        "glb_model_relpath": f"{base}/model.glb",
    }
    if require_walkthrough:
        required_relpaths["walkthrough_video_relpath"] = (
            f"{base}/generated-walkthrough.mp4"
        )
    for key, expected in required_relpaths.items():
        if str(generated.get(key) or "").strip() != expected:
            raise _PublicBundleTransactionError(
                "candidate_manifest_generated_relpath_invalid"
            )
    reconstruction_fd = _open_public_directory_path(bundle_fd, base)
    try:
        for name in (
            "viewer.html",
            "model.obj",
            "model.mtl",
            "reconstruction.json",
            "model.glb",
        ):
            _require_regular_generated_asset(reconstruction_fd, name)
        if require_walkthrough:
            _require_regular_generated_asset(
                reconstruction_fd,
                "generated-walkthrough.mp4",
            )
        receipt_fd = _open_regular_public_entry(
            reconstruction_fd,
            "reconstruction.json",
            failure="candidate_reconstruction_receipt_invalid",
        )
        try:
            receipt = _read_bounded_json_object(
                receipt_fd,
                failure="candidate_reconstruction_receipt_invalid",
            )
        finally:
            os.close(receipt_fd)
        if str(receipt.get("slug") or "").strip() != slug:
            raise _PublicBundleTransactionError(
                "candidate_reconstruction_receipt_slug_mismatch"
            )
        requested_style = (
            receipt.get("requested_style")
            if isinstance(receipt.get("requested_style"), dict)
            else {}
        )
        try:
            canonical_style = reconstruction_style(
                requested_style.get("id"),
                style_id=requested_style.get("id"),
            )
        except ValueError as exc:
            raise _PublicBundleTransactionError(
                "candidate_reconstruction_style_invalid"
            ) from exc
        if any(
            str(requested_style.get(key) or "")
            != str(canonical_style.get(key) or "")
            for key in ("id", "label", "prompt", "signature")
        ):
            raise _PublicBundleTransactionError(
                "candidate_reconstruction_style_identity_mismatch"
            )
        style_scene = (
            receipt.get("style_scene")
            if isinstance(receipt.get("style_scene"), dict)
            else {}
        )
        style_ready, _style_reason = validate_style_scene(
            style_scene,
            expected_style=canonical_style,
        )
        if not style_ready:
            raise _PublicBundleTransactionError(
                "candidate_reconstruction_style_evidence_invalid"
            )
        expected_generated_style = {
            "viewer_version": VIEWER_VERSION,
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
        if any(generated.get(key) != value for key, value in expected_generated_style.items()):
            raise _PublicBundleTransactionError(
                "candidate_manifest_style_contract_mismatch"
            )
        viewer = receipt.get("viewer") if isinstance(receipt.get("viewer"), dict) else {}
        if (
            viewer.get("version") != VIEWER_VERSION
            or viewer.get("style_id") != canonical_style["id"]
            or viewer.get("style_signature") != canonical_style["signature"]
            or viewer.get("style_scene_signature") != style_scene.get("scene_signature")
            or viewer.get("floorplan_display_mode") != FLOORPLAN_DISPLAY_MODE
        ):
            raise _PublicBundleTransactionError(
                "candidate_reconstruction_viewer_style_mismatch"
            )
    finally:
        os.close(reconstruction_fd)
    if require_walkthrough and str(manifest.get("video_relpath") or "").strip() != (
        f"{base}/generated-walkthrough.mp4"
    ):
        raise _PublicBundleTransactionError("candidate_manifest_video_invalid")


def _exchange_public_bundle_directories(
    *,
    root_fd: int,
    stage_name: str,
    slug: str,
) -> None:
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except (AttributeError, OSError) as exc:
        raise _PublicBundleTransactionError("atomic_bundle_exchange_unavailable") from exc
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        root_fd,
        os.fsencode(stage_name),
        root_fd,
        os.fsencode(slug),
        _RENAME_EXCHANGE,
    )
    if result != 0:
        raise _PublicBundleTransactionError("atomic_bundle_exchange_failed")


def _rename_public_bundle_directory_noreplace(
    *,
    root_fd: int,
    source_name: str,
    destination_name: str,
) -> None:
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except (AttributeError, OSError) as exc:
        raise _PublicBundleTransactionError("bundle_quarantine_unavailable") from exc
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        root_fd,
        os.fsencode(source_name),
        root_fd,
        os.fsencode(destination_name),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        raise _PublicBundleTransactionError("bundle_quarantine_failed")


def _remove_tree_contents(directory_fd: int) -> None:
    try:
        os.fchmod(directory_fd, 0o700)
        names = sorted(os.listdir(directory_fd))
    except OSError as exc:
        raise _PublicBundleTransactionError("bundle_stage_cleanup_failed") from exc
    for name in names:
        try:
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise _PublicBundleTransactionError("bundle_stage_cleanup_failed") from exc
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = _open_public_bundle_directory(
                name,
                parent_fd=directory_fd,
                failure="bundle_stage_cleanup_failed",
            )
            try:
                _remove_tree_contents(child_fd)
            finally:
                os.close(child_fd)
            try:
                os.rmdir(name, dir_fd=directory_fd)
            except OSError as exc:
                raise _PublicBundleTransactionError(
                    "bundle_stage_cleanup_failed"
                ) from exc
            continue
        try:
            os.unlink(name, dir_fd=directory_fd)
        except OSError as exc:
            raise _PublicBundleTransactionError("bundle_stage_cleanup_failed") from exc
    os.fsync(directory_fd)


def _remove_transaction_stage(
    root_fd: int,
    stage_name: str,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> None:
    if not stage_name.startswith(_PUBLIC_BUNDLE_STAGE_PREFIX):
        raise _PublicBundleTransactionError("bundle_stage_name_invalid")
    try:
        stage_fd = _open_public_bundle_directory(
            stage_name,
            parent_fd=root_fd,
            failure="bundle_stage_cleanup_failed",
        )
    except _PublicBundleTransactionError:
        if expected_identity is None:
            try:
                os.stat(stage_name, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            except OSError:
                pass
        raise
    try:
        opened_stage = os.fstat(stage_fd)
        opened_identity = (int(opened_stage.st_dev), int(opened_stage.st_ino))
        if expected_identity is not None and opened_identity != expected_identity:
            raise _PublicBundleTransactionError("bundle_stage_cleanup_refused")
        if not _directory_path_identity_matches(
            parent_fd=root_fd,
            name=stage_name,
            expected=opened_stage,
        ):
            raise _PublicBundleTransactionError("bundle_stage_cleanup_refused")
        _remove_tree_contents(stage_fd)
        if not _directory_path_identity_matches(
            parent_fd=root_fd,
            name=stage_name,
            expected=opened_stage,
        ):
            raise _PublicBundleTransactionError("bundle_stage_cleanup_refused")
        os.rmdir(stage_name, dir_fd=root_fd)
        os.fsync(root_fd)
    except _PublicBundleTransactionError:
        raise
    except OSError as exc:
        raise _PublicBundleTransactionError("bundle_stage_cleanup_failed") from exc
    finally:
        os.close(stage_fd)


def _reap_orphaned_transaction_stages(root_fd: int) -> None:
    try:
        names = sorted(os.listdir(root_fd))
    except OSError as exc:
        raise _PublicBundleTransactionError("bundle_stage_reaper_failed") from exc
    for name in names:
        if not name.startswith(_PUBLIC_BUNDLE_STAGE_PREFIX):
            continue
        try:
            stage_fd = _open_public_bundle_directory(
                name,
                parent_fd=root_fd,
                failure="bundle_stage_reaper_entry_invalid",
            )
        except _PublicBundleTransactionError:
            continue
        try:
            opened_stage = os.fstat(stage_fd)
            cleanup_allowed = _transaction_stage_owner_marker_matches(
                stage_fd,
                stage_name=name,
            )
        finally:
            os.close(stage_fd)
        if not cleanup_allowed:
            continue
        _remove_transaction_stage(
            root_fd,
            name,
            expected_identity=(
                int(opened_stage.st_dev),
                int(opened_stage.st_ino),
            ),
        )


@contextmanager
def _staged_public_bundle(root: Path, slug: str) -> Iterator[_PublicBundleTransaction]:
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        root_fd = os.open(root, directory_flags)
    except OSError as exc:
        raise _PublicBundleTransactionError("public_tour_root_invalid") from exc
    stage_name = f"{_PUBLIC_BUNDLE_STAGE_PREFIX}{secrets.token_hex(16)}"
    stage_created = False
    stage_identity: tuple[int, int] | None = None
    retained_stage_fd = -1
    transaction: _PublicBundleTransaction | None = None
    try:
        fcntl.flock(root_fd, fcntl.LOCK_EX)
        _reap_orphaned_transaction_stages(root_fd)
        source_fd = _open_public_bundle_directory(
            slug,
            parent_fd=root_fd,
            failure="tour_bundle_invalid",
        )
        try:
            os.mkdir(stage_name, 0o700, dir_fd=root_fd)
            stage_created = True
            stage_fd = _open_public_bundle_directory(
                stage_name,
                parent_fd=root_fd,
                failure="bundle_stage_invalid",
            )
            try:
                created_stage = os.fstat(stage_fd)
                stage_identity = (
                    int(created_stage.st_dev),
                    int(created_stage.st_ino),
                )
                _clone_public_tree(source_fd, stage_fd)
                os.fsync(stage_fd)
                staged_fingerprint = _public_tree_fingerprint(stage_fd)
                source_fingerprint = _public_tree_fingerprint(source_fd)
                if staged_fingerprint != source_fingerprint:
                    raise _PublicBundleTransactionError(
                        "bundle_source_changed_during_snapshot"
                    )
                _write_transaction_stage_owner_marker(
                    stage_fd,
                    stage_name=stage_name,
                )
                retained_stage_fd = os.dup(stage_fd)
            finally:
                os.close(stage_fd)
        finally:
            os.close(source_fd)
        if stage_identity is None:
            raise _PublicBundleTransactionError("bundle_stage_identity_unavailable")
        transaction = _PublicBundleTransaction(
            root=root,
            root_fd=root_fd,
            slug=slug,
            stage_name=stage_name,
            source_fingerprint=source_fingerprint,
            stage_identity=stage_identity,
            stage_fd=retained_stage_fd,
        )
        retained_stage_fd = -1
        yield transaction
    finally:
        try:
            if stage_created and stage_identity is not None and not (
                transaction is not None and transaction.preserve_stage_on_failure
            ):
                if transaction is not None and transaction.published:
                    if transaction.cleanup_status == "not_started":
                        transaction.cleanup_replaced_bundle()
                else:
                    _remove_transaction_stage(
                        root_fd,
                        stage_name,
                        expected_identity=stage_identity,
                    )
            elif transaction is not None and transaction.preserve_stage_on_failure:
                transaction.quarantine_preserved_stage()
        finally:
            if transaction is not None:
                if transaction.input_snapshot_fd >= 0:
                    os.close(transaction.input_snapshot_fd)
                    transaction.input_snapshot_fd = -1
                os.close(transaction.stage_fd)
            elif retained_stage_fd >= 0:
                os.close(retained_stage_fd)
            fcntl.flock(root_fd, fcntl.LOCK_UN)
            os.close(root_fd)


def _copy_generation_input_snapshot(
    source: Path,
    *,
    transaction: _PublicBundleTransaction,
    target_fd: int,
    target_name: str,
    budget: dict[str, int],
) -> None:
    target_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    source_fd = -1
    destination_fd = -1
    try:
        source_fd = _open_generation_source_descriptor(
            source,
            transaction=transaction,
        )
        observed = os.fstat(source_fd)
        if not stat.S_ISREG(observed.st_mode):
            raise _PublicBundleTransactionError("generation_source_invalid")
        if observed.st_size > min(
            _MAX_TRANSACTION_FILE_BYTES,
            _MAX_SOURCE_IMAGE_COMPRESSED_BYTES,
        ):
            raise _PublicBundleTransactionError(
                "generation_source_file_size_limit_exceeded"
            )
        if int(budget.get("bytes") or 0) + observed.st_size > (
            _generation_source_total_limit_bytes()
        ):
            raise _PublicBundleTransactionError(
                "generation_source_total_size_limit_exceeded"
            )
        _consume_transaction_tree_budget(budget, size_bytes=observed.st_size)
        destination_fd = os.open(
            target_name,
            target_flags,
            0o600,
            dir_fd=target_fd,
        )
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            offset = 0
            while offset < len(chunk):
                written = os.write(destination_fd, chunk[offset:])
                if written <= 0:
                    raise _PublicBundleTransactionError(
                        "generation_source_snapshot_failed"
                    )
                offset += written
        final_source = os.fstat(source_fd)
        if (
            final_source.st_size != observed.st_size
            or final_source.st_mtime_ns != observed.st_mtime_ns
            or final_source.st_ctime_ns != observed.st_ctime_ns
        ):
            raise _PublicBundleTransactionError(
                "generation_source_changed_during_snapshot"
            )
        os.fsync(destination_fd)
    except _PublicBundleTransactionError:
        raise
    except (OSError, ValueError) as exc:
        raise _PublicBundleTransactionError("generation_source_snapshot_failed") from exc
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        if source_fd >= 0:
            os.close(source_fd)


def _generation_source_total_limit_bytes() -> int:
    raw = str(
        os.getenv("PROPERTYQUARRY_RECONSTRUCTION_MAX_SOURCE_BYTES")
        or str(1024 * 1024 * 1024)
    ).strip()
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise _PublicBundleTransactionError(
            "generation_source_limit_invalid"
        ) from exc
    if parsed <= 0 or parsed > _MAX_TRANSACTION_TREE_BYTES:
        raise _PublicBundleTransactionError("generation_source_limit_invalid")
    return parsed


def _open_relative_regular_file_no_symlinks(
    anchor_fd: int,
    parts: tuple[str, ...],
    *,
    failure: str,
) -> int:
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise _PublicBundleTransactionError(failure)
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    current_fd = -1
    try:
        current_fd = os.dup(anchor_fd)
        for part in parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=current_fd)
            opened = os.fstat(next_fd)
            if not stat.S_ISDIR(opened.st_mode):
                os.close(next_fd)
                raise _PublicBundleTransactionError(failure)
            os.close(current_fd)
            current_fd = next_fd
        source_fd = os.open(parts[-1], file_flags, dir_fd=current_fd)
        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
            os.close(source_fd)
            raise _PublicBundleTransactionError(failure)
        return source_fd
    except _PublicBundleTransactionError:
        raise
    except OSError as exc:
        raise _PublicBundleTransactionError(failure) from exc
    finally:
        if current_fd >= 0:
            os.close(current_fd)


def _open_absolute_regular_file_no_symlinks(
    path: Path,
    *,
    failure: str,
) -> int:
    absolute = Path(os.path.abspath(os.fspath(path)))
    absolute_parts = absolute.parts
    if (
        len(absolute_parts) >= 6
        and absolute_parts[:4] == ("/", "proc", "self", "fd")
        and absolute_parts[4].isdigit()
    ):
        anchor_fd = int(absolute_parts[4])
        try:
            if not stat.S_ISDIR(os.fstat(anchor_fd).st_mode):
                raise _PublicBundleTransactionError(failure)
        except OSError as exc:
            raise _PublicBundleTransactionError(failure) from exc
        return _open_relative_regular_file_no_symlinks(
            anchor_fd,
            absolute_parts[5:],
            failure=failure,
        )
    root_fd = -1
    try:
        root_fd = os.open(
            "/",
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        return _open_relative_regular_file_no_symlinks(
            root_fd,
            absolute.parts[1:],
            failure=failure,
        )
    except _PublicBundleTransactionError:
        raise
    except OSError as exc:
        raise _PublicBundleTransactionError(failure) from exc
    finally:
        if root_fd >= 0:
            os.close(root_fd)


def _open_generation_source_descriptor(
    source: Path,
    *,
    transaction: _PublicBundleTransaction,
) -> int:
    # Do not resolve through the pathname: each ancestor is opened relative to
    # an already-pinned directory with O_NOFOLLOW. Shared-volume sources use
    # the transaction's public-root descriptor; local CLI sources use a pinned
    # filesystem root and the same component walk.
    absolute_source = Path(os.path.abspath(os.fspath(source)))
    absolute_root = Path(os.path.abspath(os.fspath(transaction.root)))
    try:
        relative = absolute_source.relative_to(absolute_root)
    except ValueError:
        return _open_absolute_regular_file_no_symlinks(
            absolute_source,
            failure="generation_source_invalid",
        )
    else:
        return _open_relative_regular_file_no_symlinks(
            transaction.root_fd,
            relative.parts,
            failure="generation_source_invalid",
        )


def _snapshot_generation_inputs(
    args: argparse.Namespace,
    transaction: _PublicBundleTransaction,
) -> argparse.Namespace:
    raw_floorplan = str(args.floorplan or "").strip()
    raw_photos = [str(value or "").strip() for value in list(args.photo or [])]
    source_rows: list[tuple[str, str]] = []
    if raw_floorplan:
        source_rows.append(("floorplan", raw_floorplan))
    source_rows.extend(
        (f"photo-{index:04d}", value)
        for index, value in enumerate(raw_photos, start=1)
        if value
    )
    if len(source_rows) > _MAX_TRANSACTION_INPUTS:
        raise _PublicBundleTransactionError("generation_source_count_limit_exceeded")
    if not source_rows:
        return argparse.Namespace(**vars(args))
    stage_fd = _open_public_bundle_directory(
        transaction.stage_name,
        parent_fd=transaction.root_fd,
        failure="bundle_stage_invalid",
    )
    inputs_fd = -1
    try:
        try:
            os.mkdir(_PUBLIC_BUNDLE_INPUT_DIR, 0o700, dir_fd=stage_fd)
        except OSError as exc:
            raise _PublicBundleTransactionError(
                "generation_source_snapshot_directory_invalid"
            ) from exc
        inputs_fd = _open_public_bundle_directory(
            _PUBLIC_BUNDLE_INPUT_DIR,
            parent_fd=stage_fd,
            failure="generation_source_snapshot_directory_invalid",
        )
        opened_inputs = os.fstat(inputs_fd)
        transaction.input_snapshot_identity = (
            int(opened_inputs.st_dev),
            int(opened_inputs.st_ino),
        )
        if transaction.input_snapshot_fd >= 0:
            os.close(transaction.input_snapshot_fd)
        transaction.input_snapshot_fd = os.dup(inputs_fd)
        budget = {"entries": 0, "bytes": 0}
        snapshots: dict[str, str] = {}
        photo_snapshots: list[str] = []
        for label, raw_source in source_rows:
            source = Path(raw_source).expanduser()
            if not source.is_absolute():
                source = Path.cwd() / source
            suffix = source.suffix.lower()
            if len(suffix) > 16 or not re.fullmatch(r"\.[a-z0-9]+", suffix):
                suffix = ".bin"
            target_name = f"{label}{suffix}"
            _copy_generation_input_snapshot(
                source,
                transaction=transaction,
                target_fd=inputs_fd,
                target_name=target_name,
                budget=budget,
            )
            snapshot_path = str(
                Path(f"/proc/self/fd/{transaction.input_snapshot_fd}")
                / target_name
            )
            if label == "floorplan":
                snapshots["floorplan"] = snapshot_path
            else:
                photo_snapshots.append(snapshot_path)
        os.fsync(inputs_fd)
        os.fsync(stage_fd)
    finally:
        if inputs_fd >= 0:
            os.close(inputs_fd)
        os.close(stage_fd)
    snapshot_args = argparse.Namespace(**vars(args))
    snapshot_args.floorplan = snapshots.get("floorplan", "")
    snapshot_args.photo = photo_snapshots
    return snapshot_args


def _remove_generation_input_snapshot(
    transaction: _PublicBundleTransaction,
) -> None:
    stage_fd = -1
    inputs_fd = -1
    try:
        stage_fd = os.dup(transaction.stage_fd)
        opened_stage = os.fstat(stage_fd)
        if transaction.stage_identity != (
            int(opened_stage.st_dev),
            int(opened_stage.st_ino),
        ) or not _directory_path_identity_matches(
            parent_fd=transaction.root_fd,
            name=transaction.stage_name,
            expected=opened_stage,
        ):
            raise _PublicBundleTransactionError(
                "generation_source_snapshot_cleanup_refused"
            )
        if transaction.input_snapshot_fd < 0:
            try:
                os.stat(
                    _PUBLIC_BUNDLE_INPUT_DIR,
                    dir_fd=stage_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return
            raise _PublicBundleTransactionError(
                "generation_source_snapshot_cleanup_refused"
            )
        inputs_fd = os.dup(transaction.input_snapshot_fd)
        opened_inputs = os.fstat(inputs_fd)
        if transaction.input_snapshot_identity != (
            int(opened_inputs.st_dev),
            int(opened_inputs.st_ino),
        ) or not _directory_path_identity_matches(
            parent_fd=stage_fd,
            name=_PUBLIC_BUNDLE_INPUT_DIR,
            expected=opened_inputs,
        ):
            raise _PublicBundleTransactionError(
                "generation_source_snapshot_cleanup_refused"
            )
        _remove_tree_contents(inputs_fd)
        if not _directory_path_matches(
            parent_fd=stage_fd,
            name=_PUBLIC_BUNDLE_INPUT_DIR,
            expected=opened_inputs,
        ):
            raise _PublicBundleTransactionError(
                "generation_source_snapshot_cleanup_refused"
            )
        os.rmdir(_PUBLIC_BUNDLE_INPUT_DIR, dir_fd=stage_fd)
        os.fsync(stage_fd)
        transaction.input_snapshot_identity = None
        os.close(transaction.input_snapshot_fd)
        transaction.input_snapshot_fd = -1
    except _PublicBundleTransactionError:
        raise
    except OSError as exc:
        raise _PublicBundleTransactionError(
            "generation_source_snapshot_cleanup_invalid"
        ) from exc
    finally:
        if inputs_fd >= 0:
            os.close(inputs_fd)
        if stage_fd >= 0:
            os.close(stage_fd)


def _generated_reconstruction_disclosure(
    *,
    photo_count: int,
    floorplan_inferred: bool = False,
) -> str:
    if floorplan_inferred:
        return (
            "Generated planning preview built from an inferred schematic and listing photos. "
            "The layout and dimensions are not verified; use it as an orientation aid, not as a captured tour."
        )
    source_noun = "the floor plan and listing photos" if max(0, int(photo_count)) else "the floor plan"
    return f"Planning preview built from {source_noun}. Use it as a layout aid, not as a captured tour."


def _compact_route_label(value: object, *, fallback: str = "", limit: int = 80) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return fallback
    return text[:limit].strip() or fallback


def _route_detail_base_label(value: object) -> str:
    label = _compact_route_label(value)
    if not label:
        return ""
    return re.sub(r"\s+detail\s+\d+\s*$", "", label, flags=re.IGNORECASE).strip()


def _env_flag(name: str) -> bool:
    return str(os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _ffmpeg_thread_limit() -> int:
    """Keep PropertyQuarry reconstruction encodes from fanning out across the host."""

    raw_limit = str(os.getenv("PROPERTYQUARRY_RECONSTRUCTION_FFMPEG_THREADS") or "1").strip()
    try:
        requested_limit = int(raw_limit)
    except (TypeError, ValueError):
        requested_limit = 1
    return max(1, min(4, requested_limit))


def _ffmpeg_thread_options() -> tuple[tuple[str, str], tuple[str, str]]:
    thread_limit = str(_ffmpeg_thread_limit())
    return (("-filter_threads", thread_limit), ("-threads", thread_limit))


def _numeric_room_count(value: object) -> int:
    try:
        if value in (None, "", False):
            return 0
        parsed = float(str(value).replace(",", ".").strip())
    except Exception:
        return 0
    if parsed <= 0:
        return 0
    return max(1, min(25, int(parsed) if float(parsed).is_integer() else int(math.ceil(parsed))))


def _positive_item_count(value: object) -> int:
    try:
        if value in (None, "", False):
            return 0
        parsed = int(float(str(value).strip()))
    except Exception:
        return 0
    return parsed if parsed > 0 else 0


def _extract_room_count_from_text(text: object) -> int:
    normalized = str(text or "").strip()
    if not normalized:
        return 0
    patterns = (
        r"\b(\d+(?:[.,]\d+)?)\s*[- ]?\s*(?:zimmer|room|rooms|bedroom|bedrooms)\b",
        r"\b(?:zimmer|rooms?|bedrooms?)\s*[:：]?\s*(\d+(?:[.,]\d+)?)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if not match:
            continue
        count = _numeric_room_count(match.group(1))
        if count > 0:
            return count
    return 0


def _append_unique_route_label(labels: list[str], value: object) -> None:
    label = _compact_route_label(value)
    if not label:
        return
    lowered = label.lower()
    if lowered in {item.lower() for item in labels}:
        return
    labels.append(label)


def _reconstruction_walkthrough_route_labels(
    payload: dict[str, object],
    *,
    explicit_labels: list[str] | tuple[str, ...] = (),
    explicit_room_count: int = 0,
) -> list[str]:
    labels: list[str] = []
    for raw_label in list(explicit_labels or []):
        _append_unique_route_label(labels, raw_label)
    if labels:
        return labels

    walkable_scene = dict(payload.get("walkable_scene") or {}) if isinstance(payload.get("walkable_scene"), dict) else {}
    for collection_key in ("route", "rooms"):
        for raw_item in list(walkable_scene.get(collection_key) or []):
            if not isinstance(raw_item, dict):
                continue
            _append_unique_route_label(labels, raw_item.get("label") or raw_item.get("room") or raw_item.get("name"))
    for collection_key in ("room_visit_plan", "covered_route_labels"):
        for raw_label in list(payload.get(collection_key) or []):
            _append_unique_route_label(labels, raw_label)
    if labels:
        return labels

    facts = dict(payload.get("facts") or {}) if isinstance(payload.get("facts"), dict) else {}
    teaser_attributes = [
        str(item).strip()
        for item in list(facts.get("teaser_attributes") or [])
        if str(item).strip()
    ]
    text_blob = " ".join(
        part
        for part in (
            payload.get("title"),
            payload.get("display_title"),
            payload.get("tour_title"),
            facts.get("title"),
            facts.get("listing_title"),
            facts.get("summary"),
            facts.get("description"),
            facts.get("rooms_label"),
            " ".join(teaser_attributes),
        )
        if str(part or "").strip()
    )
    lowered_text = text_blob.lower()
    media = dict(payload.get("media") or {}) if isinstance(payload.get("media"), dict) else {}
    source_photos = dict(media.get("source_photos") or {}) if isinstance(media.get("source_photos"), dict) else {}
    source_photo_count = max(
        _positive_item_count(source_photos.get("count")),
        _positive_item_count(payload.get("photo_count")),
    )
    scenes = list(payload.get("scenes") or [])
    if scenes:
        source_photo_count = max(
            source_photo_count,
            len(
                [
                    row
                    for row in scenes
                    if isinstance(row, dict) and str(row.get("role") or "").strip().lower() == "photo"
                ]
            ),
        )
    base_room_count = _numeric_room_count(facts.get("room_count") or facts.get("rooms")) or _extract_room_count_from_text(text_blob)
    if explicit_room_count > 0:
        base_room_count = max(base_room_count, _numeric_room_count(explicit_room_count))

    has_hall = bool(re.search(r"\b(hallway|hall|foyer|entry|entryway|eingang|vorraum|flur)\b", lowered_text))
    has_kitchen = bool(re.search(r"\b(wohnkueche|wohnküche|küche|kueche|kitchen)\b", lowered_text))
    has_bathroom = bool(re.search(r"\b(bathroom|badezimmer|bad)\b", lowered_text))
    has_toilet = bool(re.search(r"\b(separate[rsn]*\s+wc|separate[rsn]*\s+toilet|wc|w\.?c\.?|toilette|toilet)\b", lowered_text))
    has_storage = bool(re.search(r"\b(storage|storeroom|store room|utility room|abstellraum)\b", lowered_text))
    has_dining = bool(re.search(r"\b(dining room|dining|esszimmer)\b", lowered_text))
    has_outdoor = bool(re.search(r"\b(balcony|balkon|loggia|terrace|terrasse|dachterrasse)\b", lowered_text))
    has_outdoor = has_outdoor or any(
        bool(facts.get(key))
        for key in ("has_balcony", "has_terrace", "has_loggia", "balcony", "terrace", "loggia")
    )
    has_staircase = bool(
        re.search(
            r"\b(maisonette|duplex|split[- ]level|mezzanine|gallery|gallerie|stairs?|staircase|treppe|stiege|two floors?|two levels?|2 stockwerke|zwei stockwerke)\b",
            lowered_text,
        )
    )

    if has_hall or base_room_count > 0 or any((has_kitchen, has_bathroom, has_toilet, has_storage, has_outdoor, has_staircase)):
        _append_unique_route_label(labels, "entry/hall")
    if has_staircase:
        _append_unique_route_label(labels, "staircase")
    if has_storage:
        _append_unique_route_label(labels, "storage room")
    if has_bathroom:
        _append_unique_route_label(labels, "bath/WC")
    separate_toilet = has_toilet and ("separate" in lowered_text or "extra wc" in lowered_text or "gäste wc" in lowered_text or not has_bathroom)
    if separate_toilet:
        _append_unique_route_label(labels, "toilet")
    if has_kitchen:
        _append_unique_route_label(labels, "living kitchen")
    if base_room_count >= 1:
        _append_unique_route_label(labels, "living room")
    if base_room_count >= 2:
        _append_unique_route_label(labels, "bedroom")
    for bedroom_index in range(2, max(1, base_room_count - 1) + 1):
        _append_unique_route_label(labels, f"bedroom {bedroom_index}")
    if has_dining and not has_kitchen:
        _append_unique_route_label(labels, "dining room")
    if has_outdoor:
        _append_unique_route_label(labels, "balcony/terrace")
    if labels and source_photo_count > 0:
        route_kinds = {_route_label_kind(label) for label in labels}
        has_core_interior = bool(route_kinds & {"living", "kitchen", "dining", "bedroom", "generic"})
        if not has_core_interior:
            enriched_labels: list[str] = []
            if "entry" in route_kinds or source_photo_count >= 2 or bool(facts.get("has_floorplan")) or has_outdoor:
                _append_unique_route_label(enriched_labels, "entry/hall")
            if "stairs" in route_kinds:
                _append_unique_route_label(enriched_labels, "staircase")
            if "storage" in route_kinds:
                _append_unique_route_label(enriched_labels, "storage room")
            _append_unique_route_label(enriched_labels, "living area")
            if source_photo_count >= 4:
                _append_unique_route_label(enriched_labels, "sleeping area")
            if "bath" in route_kinds or source_photo_count >= 6:
                _append_unique_route_label(enriched_labels, "bath/WC")
            if "toilet" in route_kinds:
                _append_unique_route_label(enriched_labels, "toilet")
            if "outdoor" in route_kinds or has_outdoor:
                _append_unique_route_label(enriched_labels, "balcony/terrace")
            labels = enriched_labels
    if labels:
        return labels

    if source_photo_count > 0:
        if source_photo_count >= 2 or bool(facts.get("has_floorplan")) or has_outdoor:
            _append_unique_route_label(labels, "entry/hall")
        _append_unique_route_label(labels, "living area")
        if source_photo_count >= 4:
            _append_unique_route_label(labels, "sleeping area")
        if source_photo_count >= 6:
            _append_unique_route_label(labels, "bath/WC")
        if has_outdoor:
            _append_unique_route_label(labels, "balcony/terrace")
    if labels:
        return labels

    fallback_room_count = max(base_room_count, _numeric_room_count(explicit_room_count))
    if fallback_room_count <= 0:
        fallback_room_count = 1
    return [f"room stop {index}" for index in range(1, fallback_room_count + 1)]


def _route_label_kind(label: object) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", str(label or "").strip().lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return "generic"
    if any(token in normalized for token in ("entry", "hall", "foyer", "vorraum", "flur")):
        return "entry"
    if any(token in normalized for token in ("stair", "treppe", "stiege", "duplex", "maisonette", "mezzanine", "split level")):
        return "stairs"
    if any(token in normalized for token in ("bath", "bad", "badezimmer")):
        return "bath"
    if any(token in normalized for token in ("toilet", "wc")):
        return "toilet"
    if any(token in normalized for token in ("storage", "abstell")):
        return "storage"
    if any(token in normalized for token in ("balcony", "terrace", "balkon", "terrasse", "loggia")):
        return "outdoor"
    if any(token in normalized for token in ("kitchen", "kuche", "küche", "wohnkuche", "wohnküche")):
        return "kitchen"
    if any(token in normalized for token in ("dining", "esszimmer")):
        return "dining"
    if any(token in normalized for token in ("bedroom", "schlaf")):
        return "bedroom"
    if "living" in normalized or "wohn" in normalized:
        return "living"
    return "generic"


def _walkthrough_stop_labels(
    route_labels: list[str] | tuple[str, ...],
    *,
    target_stop_count: int = 0,
) -> list[str]:
    base_labels = [
        _compact_route_label(label)
        for label in list(route_labels or [])
        if _compact_route_label(label)
    ]
    if not base_labels:
        base_labels = ["room stop 1"]
    target_count = max(len(base_labels), max(0, int(target_stop_count or 0)))
    if target_count <= len(base_labels):
        return base_labels

    labels = list(base_labels)
    repeatable_kinds = {"living", "kitchen", "dining", "bedroom", "bath", "outdoor", "generic"}
    repeat_pool = [label for label in base_labels if _route_label_kind(label) in repeatable_kinds]
    if not repeat_pool:
        repeat_pool = [label for label in base_labels if _route_label_kind(label) not in {"entry", "stairs", "storage", "toilet"}]
    if not repeat_pool:
        repeat_pool = list(base_labels)
    label_counts = {label: 1 for label in base_labels}
    repeat_index = 0
    while len(labels) < target_count:
        base_label = repeat_pool[repeat_index % len(repeat_pool)]
        repeat_index += 1
        label_counts[base_label] = label_counts.get(base_label, 1) + 1
        labels.append(f"{base_label} detail {label_counts[base_label]}")
    return labels


def _viewer_storyboard_steps(
    expected_segments: list[str] | tuple[str, ...],
    *,
    route_stops: list[dict[str, object]],
) -> list[dict[str, object]]:
    normalized_segments = [
        _compact_route_label(label, fallback=f"Stop {index + 1}")
        for index, label in enumerate(list(expected_segments or []))
    ]
    if not normalized_segments or not route_stops:
        return []

    route_index_by_label: dict[str, int] = {}
    for index, stop in enumerate(route_stops):
        label = _compact_route_label(stop.get("label") or stop.get("room") or stop.get("name"))
        if not label:
            continue
        route_index_by_label.setdefault(label.lower(), index)
        base_label = _route_detail_base_label(label)
        if base_label:
            route_index_by_label.setdefault(base_label.lower(), index)

    steps: list[dict[str, object]] = []
    visit_counts: dict[int, int] = {}
    last_route_index = 0
    total = len(normalized_segments)
    for sequence, label in enumerate(normalized_segments, start=1):
        normalized_label = label.lower()
        route_index = route_index_by_label.get(normalized_label)
        if route_index is None:
            route_index = route_index_by_label.get(_route_detail_base_label(label).lower())
        if route_index is None:
            route_index = min(max(0, sequence - 1), len(route_stops) - 1)
        last_route_index = route_index if route_index >= 0 else last_route_index
        variant = visit_counts.get(last_route_index, 0)
        visit_counts[last_route_index] = variant + 1
        steps.append(
            {
                "label": label,
                "sequence": sequence,
                "total": total,
                "state": {
                    "viewMode": "room",
                    "routeIndex": last_route_index,
                    "variant": variant,
                },
            }
        )
    return steps


def _clamp_float(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _floorplan_walkable_cells(wall_mask: list[list[int]]) -> list[tuple[int, int]]:
    """Return deterministic open cells that are plausibly inside the floor-plan shell.

    Floor plans commonly contain door gaps, so a plain edge flood fill can classify
    the entire apartment as exterior.  Closed regions are always safe candidates;
    the row/column wall-envelope intersection recovers rooms connected through door
    gaps without admitting the large white canvas around an irregular plan.
    """

    rows = len(wall_mask)
    cols = min((len(row) for row in wall_mask), default=0)
    if rows < 3 or cols < 3:
        return []

    def _is_wall(row: int, col: int) -> bool:
        return bool(wall_mask[row][col])

    outside: set[tuple[int, int]] = set()
    queue: list[tuple[int, int]] = []
    for row in range(rows):
        for col in (0, cols - 1):
            if not _is_wall(row, col) and (row, col) not in outside:
                outside.add((row, col))
                queue.append((row, col))
    for col in range(cols):
        for row in (0, rows - 1):
            if not _is_wall(row, col) and (row, col) not in outside:
                outside.add((row, col))
                queue.append((row, col))

    queue_index = 0
    while queue_index < len(queue):
        row, col = queue[queue_index]
        queue_index += 1
        for near_row, near_col in ((row - 1, col), (row + 1, col), (row, col - 1), (row, col + 1)):
            if not (0 <= near_row < rows and 0 <= near_col < cols):
                continue
            if _is_wall(near_row, near_col) or (near_row, near_col) in outside:
                continue
            outside.add((near_row, near_col))
            queue.append((near_row, near_col))

    enclosed = {
        (row, col)
        for row in range(1, rows - 1)
        for col in range(1, cols - 1)
        if not _is_wall(row, col) and (row, col) not in outside
    }
    row_wall_extents: dict[int, tuple[int, int]] = {}
    for row in range(rows):
        wall_columns = [col for col in range(cols) if _is_wall(row, col)]
        if len(wall_columns) >= 2 and wall_columns[-1] - wall_columns[0] >= 2:
            row_wall_extents[row] = (wall_columns[0], wall_columns[-1])
    column_wall_extents: dict[int, tuple[int, int]] = {}
    for col in range(cols):
        wall_rows = [row for row in range(rows) if _is_wall(row, col)]
        if len(wall_rows) >= 2 and wall_rows[-1] - wall_rows[0] >= 2:
            column_wall_extents[col] = (wall_rows[0], wall_rows[-1])

    envelope: set[tuple[int, int]] = set()
    for row, (left_wall, right_wall) in row_wall_extents.items():
        for col in range(left_wall + 1, right_wall):
            column_extent = column_wall_extents.get(col)
            if column_extent is None:
                continue
            top_wall, bottom_wall = column_extent
            if top_wall < row < bottom_wall and not _is_wall(row, col):
                envelope.add((row, col))

    return sorted(enclosed | envelope)


def _route_anchor_from_floorplan_mask(
    *,
    desired_x: float,
    desired_z: float,
    wall_mask: list[list[int]],
    used_cells: set[tuple[int, int]],
) -> tuple[float, float] | None:
    rows = len(wall_mask)
    cols = min((len(row) for row in wall_mask), default=0)
    if not rows or not cols:
        return None

    def _open_neighbor_count(row: int, col: int, radius: int = 2) -> int:
        open_neighbors = 0
        for near_row in range(max(0, row - radius), min(rows, row + radius + 1)):
            for near_col in range(max(0, col - radius), min(cols, col + radius + 1)):
                if near_row == row and near_col == col:
                    continue
                if not wall_mask[near_row][near_col]:
                    open_neighbors += 1
        return open_neighbors

    candidates: list[tuple[float, float, int, int]] = []
    for row, col in _floorplan_walkable_cells(wall_mask):
        if (row, col) in used_cells:
            continue
        candidate_x = ((col + 0.5) / cols) - 0.5
        candidate_z = ((row + 0.5) / rows) - 0.5
        semantic_distance = math.dist((candidate_x, candidate_z), (desired_x, desired_z))
        clearance_bonus = _open_neighbor_count(row, col) * 0.0015
        if used_cells:
            coverage_distance = min(
                math.dist(
                    (candidate_x, candidate_z),
                    (((used_col + 0.5) / cols) - 0.5, ((used_row + 0.5) / rows) - 0.5),
                )
                for used_row, used_col in used_cells
            )
            score = -(coverage_distance * 1.0) + (semantic_distance * 0.02) - clearance_bonus
        else:
            score = semantic_distance - clearance_bonus
        candidates.append((score, semantic_distance, row, col))
    if not candidates:
        return None
    _, _, selected_row, selected_col = min(candidates)
    used_cells.add((selected_row, selected_col))
    return (((selected_col + 0.5) / cols) - 0.5, ((selected_row + 0.5) / rows) - 0.5)


def _reconstruction_walkable_scene(
    *,
    route_labels: list[str] | tuple[str, ...],
    width_m: float,
    depth_m: float,
    height_m: float,
    geometry: dict[str, object] | None = None,
) -> dict[str, object]:
    normalized_labels = [
        _compact_route_label(label)
        for label in list(route_labels or [])
        if _compact_route_label(label)
    ]
    if not normalized_labels:
        normalized_labels = ["entry/hall"]
    semantic_anchors: dict[str, tuple[float, float]] = {
        "entry": (-0.28, 0.30),
        "stairs": (-0.08, 0.10),
        "bath": (-0.26, -0.26),
        "toilet": (-0.12, -0.28),
        "storage": (-0.30, -0.10),
        "kitchen": (0.10, 0.14),
        "living": (0.20, 0.00),
        "dining": (0.28, -0.04),
        "bedroom": (0.24, -0.24),
        "outdoor": (0.34, -0.34),
        "generic": (0.00, 0.00),
    }
    fallback_anchors: list[tuple[float, float]] = [
        (-0.28, 0.26),
        (-0.10, 0.18),
        (0.14, 0.12),
        (0.26, 0.00),
        (0.22, -0.18),
        (0.00, -0.26),
        (-0.22, -0.18),
    ]
    wall_mask = (
        [list(row) for row in list((geometry or {}).get("wall_mask") or []) if isinstance(row, list)]
        if isinstance(geometry, dict)
        else []
    )
    floorplan_walkable_cells = _floorplan_walkable_cells(wall_mask)
    has_floorplan_walkable_region = bool(floorplan_walkable_cells)
    inner_width = max(1.2, width_m * (0.88 if has_floorplan_walkable_region else 0.34))
    inner_depth = max(1.2, depth_m * (0.88 if has_floorplan_walkable_region else 0.34))
    stop_positions: list[tuple[float, float]] = []
    used_positions: list[tuple[float, float]] = []
    used_floorplan_cells: set[tuple[int, int]] = set()
    for index, label in enumerate(normalized_labels):
        kind = _route_label_kind(label)
        base_x, base_z = semantic_anchors.get(kind, semantic_anchors["generic"])
        if kind == "bedroom":
            bedroom_offset = min(0.18, 0.12 * sum(1 for prior in normalized_labels[:index] if _route_label_kind(prior) == "bedroom"))
            base_x = max(-0.32, base_x - bedroom_offset)
        elif kind == "generic":
            base_x, base_z = fallback_anchors[index % len(fallback_anchors)]
        floorplan_anchor = _route_anchor_from_floorplan_mask(
            desired_x=base_x,
            desired_z=base_z,
            wall_mask=wall_mask,
            used_cells=used_floorplan_cells,
        )
        if floorplan_anchor is not None:
            base_x, base_z = floorplan_anchor
        candidate = (base_x, base_z)
        if any(abs(candidate[0] - used_x) < 0.06 and abs(candidate[1] - used_z) < 0.06 for used_x, used_z in used_positions):
            fallback_x, fallback_z = fallback_anchors[index % len(fallback_anchors)]
            candidate = (fallback_x, fallback_z)
        used_positions.append(candidate)
        stop_positions.append(candidate)

    def _clamp(value: float, lower: float, upper: float) -> float:
        return max(lower, min(upper, value))

    route: list[dict[str, object]] = []
    rooms: list[dict[str, object]] = []
    eye_y = round(max(1.45, min(height_m * 0.58, height_m - 0.3)), 3)
    target_y = round(max(1.2, min(height_m * 0.5, eye_y - 0.18)), 3)
    for index, (label, (nx, nz)) in enumerate(zip(normalized_labels, stop_positions), start=1):
        kind = _route_label_kind(label)
        focus_x = round(nx * inner_width, 3)
        focus_z = round(nz * inner_depth, 3)
        offset_x = 0.72 if focus_x < 0 else -0.72
        offset_z = 0.92 if focus_z < 0.18 else 0.58
        camera_x = round(_clamp(focus_x + offset_x, -(width_m * 0.42), width_m * 0.42), 3)
        camera_z = round(_clamp(focus_z + offset_z, -(depth_m * 0.42), depth_m * 0.42), 3)
        stop = {
            "label": label,
            "room": label,
            "name": label,
            "kind": kind,
            "sequence": index,
            "focus": {"x": focus_x, "y": target_y, "z": focus_z},
            "camera": {"x": camera_x, "y": eye_y, "z": camera_z},
        }
        route.append(stop)
        rooms.append(
            {
                "label": label,
                "name": label,
                "kind": kind,
                "sequence": index,
                "position": {"x": focus_x, "y": 0.0, "z": focus_z},
                "focus": {"x": focus_x, "y": target_y, "z": focus_z},
            }
        )
    return {
        "kind": "generated_reconstruction_layout",
        "route_anchor_method": (
            "coverage_aware_floorplan_open_cell_sampling"
            if has_floorplan_walkable_region
            else "semantic_layout_fallback"
        ),
        "route_label_binding": "operator_supplied_labels_without_pixel_semantic_inference",
        "bounds": {"width_m": round(width_m, 3), "depth_m": round(depth_m, 3), "height_m": round(height_m, 3)},
        "rooms": rooms,
        "route": route,
    }


def _generated_reconstruction_photo_reference_panels(
    *,
    photos: list[dict[str, object]],
    walkable_scene: dict[str, object],
    width_m: float,
    depth_m: float,
    height_m: float,
) -> list[dict[str, object]]:
    if not photos:
        return []
    route_stops = [dict(stop) for stop in list(walkable_scene.get("route") or []) if isinstance(stop, dict)]
    panels: list[dict[str, object]] = []
    side_counts = {"north": 0, "south": 0, "east": 0, "west": 0}
    secondary_offsets = (0.0, -0.95, 0.95, -1.7, 1.7, -2.45, 2.45)
    mount_y = round(max(1.42, min(height_m * 0.58, height_m - 0.62)), 3)

    for index, row in enumerate(photos):
        if not isinstance(row, dict):
            continue
        relpath = str(row.get("relpath") or "").strip()
        if not relpath:
            continue
        if route_stops:
            stop_index = min(
                max(0, int((index * len(route_stops)) / max(1, len(photos)))),
                max(0, len(route_stops) - 1),
            )
        else:
            stop_index = -1
        stop = route_stops[stop_index] if stop_index >= 0 else {}
        focus = dict(stop.get("focus") or {}) if isinstance(stop.get("focus"), dict) else {}
        focus_x = _clamp_float(float(focus.get("x") or 0.0), -(width_m * 0.28), width_m * 0.28)
        focus_z = _clamp_float(float(focus.get("z") or 0.0), -(depth_m * 0.28), depth_m * 0.28)
        label = _compact_route_label(
            stop.get("label") or stop.get("room") or stop.get("name"),
            fallback=f"Room reference {len(panels) + 1}",
        )
        kind = _route_label_kind(label)
        photo_width_px = max(1, int(row.get("width") or 1))
        photo_height_px = max(1, int(row.get("height") or 1))
        aspect_ratio = _clamp_float(photo_width_px / photo_height_px, 0.75, 1.85)
        photo_height = round(_clamp_float(1.04, 0.92, 1.18), 3)
        photo_width = round(_clamp_float(photo_height * aspect_ratio, 1.0, 1.82), 3)
        frame_width = round(photo_width + 0.18, 3)
        frame_height = round(photo_height + 0.26, 3)

        if abs(focus_x / max(width_m, 1.0)) > abs(focus_z / max(depth_m, 1.0)):
            wall_side = "east" if focus_x >= 0 else "west"
        else:
            wall_side = "south" if focus_z >= 0 else "north"
        side_index = side_counts[wall_side]
        side_counts[wall_side] += 1
        offset = secondary_offsets[side_index % len(secondary_offsets)]

        if wall_side in {"east", "west"}:
            secondary_span = max(0.0, (depth_m * 0.34) - (photo_width * 0.42))
            panel_z = round(_clamp_float(focus_z + offset, -secondary_span, secondary_span), 3)
            panel_x = round((width_m * 0.5) - 0.08, 3) * (1 if wall_side == "east" else -1)
            rotation_y = round(-math.pi / 2 if wall_side == "east" else math.pi / 2, 6)
            panel_position = {"x": panel_x, "y": mount_y, "z": panel_z}
        else:
            secondary_span = max(0.0, (width_m * 0.34) - (photo_width * 0.42))
            panel_x = round(_clamp_float(focus_x + offset, -secondary_span, secondary_span), 3)
            panel_z = round((depth_m * 0.5) - 0.08, 3) * (1 if wall_side == "south" else -1)
            rotation_y = round(math.pi if wall_side == "south" else 0.0, 6)
            panel_position = {"x": panel_x, "y": mount_y, "z": panel_z}

        panels.append(
            {
                "index": len(panels) + 1,
                "label": label,
                "kind": kind,
                "route_index": stop_index,
                "photo_relpath": relpath,
                "photo_width": photo_width,
                "photo_height": photo_height,
                "frame_width": frame_width,
                "frame_height": frame_height,
                "wall_side": wall_side,
                "position": panel_position,
                "rotation_y": rotation_y,
            }
        )
    return panels


def _generated_reconstruction_diorama_palette(style_label: str) -> dict[str, tuple[int, int, int]]:
    normalized = str(style_label or "").strip().lower()
    if normalized and any(
        marker in normalized
        for marker in ("moody", "dark", "night", "charcoal", "walnut", "cinematic", "industrial")
    ):
        return {
            "wash": (50, 45, 43),
            "floorplan_wash": (70, 66, 63),
            "matte": (247, 242, 234),
            "accent": (189, 145, 63),
            "accent_soft": (255, 248, 236),
        }
    if normalized and any(
        marker in normalized
        for marker in ("scandi", "minimal", "minimalist", "nordic", "airy", "cool", "blue", "modern")
    ):
        return {
            "wash": (226, 233, 240),
            "floorplan_wash": (238, 241, 244),
            "matte": (248, 249, 247),
            "accent": (111, 140, 172),
            "accent_soft": (242, 247, 252),
        }
    if normalized and any(
        marker in normalized
        for marker in ("vintage", "mid century", "mid-century", "earth", "terracotta", "retro")
    ):
        return {
            "wash": (232, 219, 202),
            "floorplan_wash": (241, 232, 221),
            "matte": (249, 242, 235),
            "accent": (170, 109, 72),
            "accent_soft": (255, 246, 238),
        }
    return {
        "wash": (245, 240, 232),
        "floorplan_wash": (242, 235, 226),
        "matte": (251, 247, 241),
        "accent": (186, 139, 51),
        "accent_soft": (255, 250, 240),
    }


def _style_scene_preview_palette(
    style_scene: dict[str, object] | None,
    *,
    style_label: str,
) -> dict[str, tuple[int, int, int]]:
    fallback = _generated_reconstruction_diorama_palette(style_label)
    scene_palette = (
        dict(style_scene.get("material_palette") or {})
        if isinstance(style_scene, dict)
        else {}
    )

    def color(key: str, default: tuple[int, int, int]) -> tuple[int, int, int]:
        raw = str(scene_palette.get(key) or "").strip().lstrip("#")
        if not re.fullmatch(r"[0-9a-fA-F]{6}", raw):
            return default
        return tuple(int(raw[index : index + 2], 16) for index in (0, 2, 4))

    floor = color("floor", fallback["floorplan_wash"])
    wall = color("wall", fallback["matte"])
    accent = color("accent", fallback["accent"])
    return {
        **fallback,
        "wash": tuple(int(round((wall[index] * 0.62) + (floor[index] * 0.38))) for index in range(3)),
        "floorplan_wash": floor,
        "matte": wall,
        "accent": accent,
        "accent_soft": tuple(min(255, int(round((channel * 0.34) + 168))) for channel in accent),
    }


def _draw_style_scene_preview_vignette(
    canvas: Image.Image,
    *,
    style_scene: dict[str, object],
    box: tuple[int, int, int, int],
) -> dict[str, object]:
    palette = dict(style_scene.get("material_palette") or {})

    def color(key: str, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
        raw = str(palette.get(key) or "").strip().lstrip("#")
        if not re.fullmatch(r"[0-9a-fA-F]{6}", raw):
            return fallback
        return tuple(int(raw[index : index + 2], 16) for index in (0, 2, 4))

    draw = ImageDraw.Draw(canvas, "RGBA")
    left, top, right, bottom = box
    surface_floor = color("floor", (220, 204, 178))
    surface_accent = color("accent", (72, 104, 80))
    surface_fill = tuple(
        max(
            0,
            min(
                255,
                int(round((surface_floor[index] * 0.72) + (surface_accent[index] * 0.28))),
            ),
        )
        for index in range(3)
    )
    draw.rounded_rectangle(
        box,
        radius=30,
        fill=(*surface_fill, 244),
        outline=(*color("edge", (128, 103, 72)), 255),
        width=3,
    )
    heading_font = _preview_font(22, bold=True)
    label_font = _preview_font(16, bold=True)
    detail_font = _preview_font(13)
    draw.text((left + 24, top + 20), "Styled 3D reconstruction", font=heading_font, fill=(35, 39, 35))
    draw.text(
        (left + 24, top + 51),
        str(style_scene.get("style_label") or "Selected style"),
        font=label_font,
        fill=color("accent", (72, 104, 80)),
    )
    floor_polygon = (
        (left + 30, bottom - 72),
        (left + 112, top + 98),
        (right - 38, top + 116),
        (right - 22, bottom - 54),
    )
    floor_color = color("floor", (220, 204, 178))
    floor_preview_color = tuple(max(0, int(round(channel * 0.82))) for channel in floor_color)
    draw.polygon(floor_polygon, fill=(*floor_preview_color, 255))
    draw.line((*floor_polygon, floor_polygon[0]), fill=(*color("edge", (121, 94, 62)), 255), width=3)

    first_route_instances = [
        dict(row)
        for row in list(style_scene.get("instances") or [])
        if isinstance(row, dict) and int(row.get("route_index") or 0) == 0
    ]
    anchors = (
        (left + 94, top + 166),
        (left + 202, top + 156),
        (left + 116, top + 278),
        (left + 254, top + 274),
    )
    rendered_cues: list[str] = []
    for index, instance in enumerate(first_route_instances[:4]):
        anchor_x, anchor_y = anchors[index]
        shape = str(instance.get("shape") or "box")
        material_color = color(str(instance.get("material") or ""), color("accent", (78, 110, 85)))
        cue = str(instance.get("cue") or "").replace("_", " ")
        if shape == "plant":
            draw.rounded_rectangle((anchor_x - 18, anchor_y + 22, anchor_x + 18, anchor_y + 64), radius=8, fill=(*color("rattan", (181, 130, 75)), 255))
            draw.line((anchor_x, anchor_y + 24, anchor_x, anchor_y - 24), fill=(*color("timber", (113, 77, 48)), 255), width=7)
            for offset_x, offset_y in ((-25, -12), (23, -18), (-18, 8), (19, 2), (0, -36)):
                draw.ellipse((anchor_x + offset_x - 14, anchor_y + offset_y - 9, anchor_x + offset_x + 14, anchor_y + offset_y + 9), fill=(*color("foliage", (61, 112, 73)), 255))
        elif shape == "rattan_chair":
            rattan = color("rattan", material_color)
            draw.rounded_rectangle((anchor_x - 34, anchor_y - 28, anchor_x + 34, anchor_y + 5), radius=10, outline=(*rattan, 255), width=8)
            draw.rounded_rectangle((anchor_x - 37, anchor_y + 2, anchor_x + 37, anchor_y + 30), radius=8, fill=(*rattan, 255))
            draw.line((anchor_x - 28, anchor_y + 28, anchor_x - 34, anchor_y + 64), fill=(*rattan, 255), width=7)
            draw.line((anchor_x + 28, anchor_y + 28, anchor_x + 34, anchor_y + 64), fill=(*rattan, 255), width=7)
        elif shape == "rug":
            draw.rounded_rectangle((anchor_x - 58, anchor_y - 28, anchor_x + 58, anchor_y + 32), radius=18, fill=(*material_color, 232), outline=(*color("edge", (120, 99, 72)), 255), width=3)
        elif shape in {"table", "bench"}:
            draw.rounded_rectangle((anchor_x - 48, anchor_y - 5, anchor_x + 48, anchor_y + 12), radius=5, fill=(*material_color, 255))
            draw.line((anchor_x - 39, anchor_y + 10, anchor_x - 43, anchor_y + 53), fill=(*material_color, 255), width=7)
            draw.line((anchor_x + 39, anchor_y + 10, anchor_x + 43, anchor_y + 53), fill=(*material_color, 255), width=7)
            if shape == "bench":
                draw.rounded_rectangle((anchor_x - 48, anchor_y - 42, anchor_x + 48, anchor_y - 7), radius=7, outline=(*material_color, 255), width=7)
        elif shape in {"cylinder", "vessels"}:
            for vessel_offset, scale in ((-22, 0.72), (8, 1.0), (34, 0.62)):
                vessel_height = int(round(52 * scale))
                draw.rounded_rectangle((anchor_x + vessel_offset - 12, anchor_y + 30 - vessel_height, anchor_x + vessel_offset + 12, anchor_y + 30), radius=9, fill=(*material_color, 255))
        elif shape == "shelf":
            draw.rectangle((anchor_x - 42, anchor_y - 46, anchor_x + 42, anchor_y + 48), fill=(*material_color, 255), outline=(*color("edge", (120, 99, 72)), 255), width=4)
            for shelf_y in (anchor_y - 18, anchor_y + 12):
                draw.line((anchor_x - 38, shelf_y, anchor_x + 38, shelf_y), fill=(255, 255, 255, 210), width=4)
        else:
            draw.rounded_rectangle((anchor_x - 48, anchor_y - 25, anchor_x + 48, anchor_y + 34), radius=9, fill=(*material_color, 255), outline=(*color("edge", (120, 99, 72)), 255), width=3)
        draw.text(
            (anchor_x - 54, min(bottom - 29, anchor_y + 68)),
            _preview_fit_text(cue, font=detail_font, max_width=108),
            font=detail_font,
            fill=(49, 48, 42),
        )
        rendered_cues.append(str(instance.get("cue") or ""))
    return {
        "box": list(box),
        "rendered_cues": rendered_cues,
        "rendered_instance_count": len(rendered_cues),
    }


def _preview_font(size: int, *, bold: bool = False, serif: bool = False) -> ImageFont.ImageFont:
    family = "serif" if serif else "sans"
    normalized_size = max(10, int(size))
    cache_key = (family, bool(bold), normalized_size)
    cached = _PREVIEW_FONT_CACHE.get(cache_key)
    if cached is not None:
        return cached
    font_path = _PREVIEW_FONT_PATHS[(family, bool(bold))]
    try:
        if font_path.is_file():
            font = ImageFont.truetype(str(font_path), normalized_size)
        else:
            font = ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()
    _PREVIEW_FONT_CACHE[cache_key] = font
    return font


def _text_width(font: ImageFont.ImageFont, text: object) -> float:
    normalized = str(text or "")
    try:
        return float(font.getlength(normalized))  # type: ignore[attr-defined]
    except Exception:
        bbox = font.getbbox(normalized)
        return float(max(0, bbox[2] - bbox[0]))


def _line_height(font: ImageFont.ImageFont, *, leading: float = 1.18) -> int:
    try:
        bbox = font.getbbox("Ag")
        height = max(1, bbox[3] - bbox[1])
    except Exception:
        height = max(1, int(getattr(font, "size", 12) or 12))
    return max(1, int(round(height * leading)))


def _wrap_text(text: object, *, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    words = [segment for segment in re.split(r"\s+", str(text or "").strip()) if segment]
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if _text_width(font, candidate) <= max_width:
            current = candidate
            continue
        lines.append(current)
        current = word
    if current:
        lines.append(current)
    return lines


def _draw_wrapped_text(
    draw: ImageDraw.ImageDraw,
    origin: tuple[int, int],
    text: object,
    *,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int] | tuple[int, int, int, int],
    max_width: int,
    line_gap: int = 4,
) -> int:
    lines = _wrap_text(text, font=font, max_width=max_width)
    x, y = origin
    step = _line_height(font)
    for index, line in enumerate(lines):
        draw.text((x, y), line, font=font, fill=fill)
        y += step + (line_gap if index + 1 < len(lines) else 0)
    return y


def _draw_centered_text(
    draw: ImageDraw.ImageDraw,
    center: tuple[float, float],
    text: object,
    *,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int] | tuple[int, int, int, int],
) -> None:
    bbox = draw.textbbox((0, 0), str(text or ""), font=font)
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    draw.text(
        (center[0] - (width / 2) - bbox[0], center[1] - (height / 2) - bbox[1]),
        str(text or ""),
        font=font,
        fill=fill,
    )


def _draw_text_chip(
    draw: ImageDraw.ImageDraw,
    origin: tuple[int, int],
    text: object,
    *,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int] | tuple[int, int, int, int],
    outline: tuple[int, int, int] | tuple[int, int, int, int],
    text_fill: tuple[int, int, int] | tuple[int, int, int, int],
    radius: int = 18,
    pad_x: int = 14,
    pad_y: int = 8,
    outline_width: int = 2,
) -> tuple[int, int, int, int]:
    label = str(text or "").strip()
    bbox = draw.textbbox((0, 0), label, font=font)
    width = (bbox[2] - bbox[0]) + (pad_x * 2)
    height = (bbox[3] - bbox[1]) + (pad_y * 2)
    x, y = origin
    box = (x, y, x + width, y + height)
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=outline_width)
    draw.text((x + pad_x, y + pad_y - bbox[1]), label, font=font, fill=text_fill)
    return box


def _preview_rect_contains(
    outer: tuple[int, int, int, int],
    inner: tuple[int, int, int, int],
    *,
    padding: int = 0,
) -> bool:
    return bool(
        inner[0] >= outer[0] + padding
        and inner[1] >= outer[1] + padding
        and inner[2] <= outer[2] - padding
        and inner[3] <= outer[3] - padding
    )


def _preview_rects_overlap(
    first: tuple[int, int, int, int],
    second: tuple[int, int, int, int],
) -> bool:
    return not (
        first[2] <= second[0]
        or second[2] <= first[0]
        or first[3] <= second[1]
        or second[3] <= first[1]
    )


def _preview_wrapped_text_box(
    origin: tuple[int, int],
    text: object,
    *,
    font: ImageFont.ImageFont,
    max_width: int,
    line_gap: int = 4,
) -> tuple[int, int, int, int]:
    lines = _wrap_text(text, font=font, max_width=max_width)
    x, y = origin
    if not lines:
        return (x, y, x, y)
    width = int(math.ceil(max(_text_width(font, line) for line in lines)))
    height = (len(lines) * _line_height(font)) + (max(0, len(lines) - 1) * line_gap)
    return (x, y, x + width, y + height)


def _preview_fit_text(text: object, *, font: ImageFont.ImageFont, max_width: int) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "").strip())
    if not normalized or _text_width(font, normalized) <= max_width:
        return normalized
    ellipsis = "…"
    if _text_width(font, ellipsis) > max_width:
        return ""
    low = 0
    high = len(normalized)
    while low < high:
        midpoint = (low + high + 1) // 2
        candidate = normalized[:midpoint].rstrip() + ellipsis
        if _text_width(font, candidate) <= max_width:
            low = midpoint
        else:
            high = midpoint - 1
    return normalized[:low].rstrip() + ellipsis


@contextmanager
def _serve_directory(root: Path):
    class _QuietHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), _QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _wait_for_playwright_condition(page, predicate: str, *, timeout_ms: int = 15_000) -> None:
    deadline = time.monotonic() + (max(int(timeout_ms), 1) / 1000)
    while time.monotonic() < deadline:
        try:
            if bool(page.evaluate(predicate)):
                return
        except Exception:
            pass
        page.wait_for_timeout(250)
    raise TimeoutError("playwright_condition_timeout")


def _decorate_viewer_walkthrough_frame(
    image: Image.Image,
    *,
    label: str,
    sequence: int,
    total: int,
    style_label: str = "",
    floorplan_thumb: Image.Image | None = None,
    route_markers: list[dict[str, object]] | None = None,
) -> Image.Image:
    canvas = image.convert("RGBA")
    draw = ImageDraw.Draw(canvas, "RGBA")
    palette = _generated_reconstruction_diorama_palette(style_label)
    eyebrow_font = _preview_font(16, bold=True)
    label_font = _preview_font(30, serif=True, bold=True)
    meta_font = _preview_font(15)
    chip_font = _preview_font(14, bold=True)
    panel_heading_font = _preview_font(16, bold=True)
    progress_number_font = _preview_font(14, bold=True)

    top_box = (26, 24, 332, 116)
    draw.rounded_rectangle(top_box, radius=22, fill=(247, 241, 232, 216), outline=(216, 202, 176, 255), width=2)
    draw.text((46, 42), "Layout flythrough", font=eyebrow_font, fill=(66, 52, 31))
    draw.text((46, 66), "Planning preview", font=label_font, fill=(48, 38, 24))

    progress_box = (canvas.width - 388, canvas.height - 138, canvas.width - 28, canvas.height - 28)
    draw.rounded_rectangle(progress_box, radius=24, fill=(24, 19, 14, 196), outline=(255, 255, 255, 48), width=2)
    draw.text((progress_box[0] + 20, progress_box[1] + 18), "Room route", font=eyebrow_font, fill=(250, 244, 235, 212))
    _draw_wrapped_text(
        draw,
        (progress_box[0] + 20, progress_box[1] + 42),
        label,
        font=label_font,
        fill=(251, 246, 239),
        max_width=progress_box[2] - progress_box[0] - 40,
        line_gap=0,
    )
    draw.text(
        (progress_box[0] + 20, progress_box[3] - 34),
        f"Stop {max(1, sequence)} of {max(1, total)}",
        font=meta_font,
        fill=(250, 244, 235, 214),
    )
    if style_label:
        _draw_text_chip(
            draw,
            (progress_box[0] + 20, progress_box[1] - 28),
            style_label,
            font=chip_font,
            fill=(255, 252, 245, 228),
            outline=(216, 202, 176, 255),
            text_fill=(96, 72, 38),
            radius=16,
            pad_x=12,
            pad_y=6,
            outline_width=2,
        )

    normalized_markers = [dict(marker) for marker in list(route_markers or []) if isinstance(marker, dict)]
    if floorplan_thumb is not None and normalized_markers:
        route_panel_box = (canvas.width - 392, 24, canvas.width - 28, 350)
        draw.rounded_rectangle(
            route_panel_box,
            radius=28,
            fill=(251, 247, 241, 228),
            outline=(216, 202, 176, 255),
            width=2,
        )
        draw.text((route_panel_box[0] + 20, route_panel_box[1] + 18), "Route map", font=panel_heading_font, fill=(45, 38, 28))
        route_map = _walkthrough_route_map_inset(
            floorplan_thumb=floorplan_thumb,
            route_markers=normalized_markers,
            active_index=max(0, min(max(0, sequence) - 1, max(0, len(normalized_markers) - 1))),
            palette=palette,
        )
        route_map_anchor = (route_panel_box[0] + 18, route_panel_box[1] + 48)
        draw.rounded_rectangle(
            (
                route_map_anchor[0] - 6,
                route_map_anchor[1] - 6,
                route_map_anchor[0] + route_map.width + 6,
                route_map_anchor[1] + route_map.height + 6,
            ),
            radius=22,
            fill=(255, 252, 245, 234),
            outline=(216, 202, 176, 255),
            width=2,
        )
        canvas.alpha_composite(route_map.convert("RGBA"), route_map_anchor)
        draw.text((route_panel_box[0] + 20, route_panel_box[1] + 292), "Current stop", font=panel_heading_font, fill=(45, 38, 28))
        _draw_wrapped_text(
            draw,
            (route_panel_box[0] + 20, route_panel_box[1] + 314),
            label,
            font=meta_font,
            fill=(96, 72, 38),
            max_width=route_panel_box[2] - route_panel_box[0] - 40,
            line_gap=0,
        )
        progress_left = route_panel_box[0] + 20
        progress_right = route_panel_box[2] - 24
        progress_y = route_panel_box[3] - 30
        if total > 1:
            draw.line((progress_left, progress_y, progress_right, progress_y), fill=(214, 202, 180), width=4)
        for index in range(max(1, total)):
            if total <= 1:
                cx = (progress_left + progress_right) / 2
            else:
                cx = progress_left + ((progress_right - progress_left) * index / max(total - 1, 1))
            fill = palette["accent"] if index < max(1, sequence) else (255, 252, 245)
            outline = (138, 97, 23) if index + 1 == max(1, sequence) else (167, 124, 43)
            radius = 8 if index + 1 == max(1, sequence) else 6
            draw.ellipse((cx - radius, progress_y - radius, cx + radius, progress_y + radius), fill=fill, outline=outline, width=3)
            _draw_centered_text(
                draw,
                (cx, progress_y - 18),
                str(index + 1),
                font=progress_number_font,
                fill=(75, 57, 35),
            )
    return canvas.convert("RGB")


def _generated_reconstruction_fit_cover(path: Path, size: tuple[int, int]) -> Image.Image:
    with Image.open(path) as image:
        normalized = ImageOps.exif_transpose(image).convert("RGB")
        return ImageOps.fit(normalized, size, Image.Resampling.LANCZOS)


def _generated_reconstruction_mount_card(
    image: Image.Image,
    *,
    max_size: tuple[int, int],
    frame_px: int,
    rotate_degrees: float = 0.0,
    matte_color: tuple[int, int, int] = (250, 247, 242),
    shadow_alpha: int = 84,
) -> Image.Image:
    rendered = image.convert("RGB")
    rendered.thumbnail(max_size, Image.Resampling.LANCZOS)
    framed = Image.new("RGBA", (rendered.width + (frame_px * 2), rendered.height + (frame_px * 2)), (0, 0, 0, 0))
    matte = Image.new("RGBA", framed.size, (*matte_color, 255))
    framed.alpha_composite(matte)
    framed.paste(rendered, (frame_px, frame_px))
    shadow = Image.new("RGBA", (framed.width + 64, framed.height + 64), (0, 0, 0, 0))
    shadow_block = Image.new("RGBA", framed.size, (24, 22, 20, shadow_alpha))
    shadow.alpha_composite(shadow_block, (26, 30))
    shadow = shadow.filter(ImageFilter.GaussianBlur(20))
    composite = Image.new(
        "RGBA",
        (max(shadow.width, framed.width), max(shadow.height, framed.height)),
        (0, 0, 0, 0),
    )
    composite.alpha_composite(shadow)
    composite.alpha_composite(framed)
    if rotate_degrees:
        composite = composite.rotate(rotate_degrees, resample=Image.Resampling.BICUBIC, expand=True)
    return composite


def _generated_reconstruction_floorplan_with_route(
    floorplan_path: Path,
    *,
    walkable_scene: dict[str, object],
    size: tuple[int, int],
    palette: dict[str, tuple[int, int, int]],
) -> Image.Image:
    floorplan = _generated_reconstruction_fit_cover(floorplan_path, size)
    floorplan = Image.blend(floorplan, Image.new("RGB", floorplan.size, palette["floorplan_wash"]), 0.12)
    draw = ImageDraw.Draw(floorplan)
    marker_font = _preview_font(max(16, int(round(min(size) * 0.026))), bold=True)
    route_stops = [dict(stop) for stop in list(walkable_scene.get("route") or []) if isinstance(stop, dict)]
    bounds = dict(walkable_scene.get("bounds") or {}) if isinstance(walkable_scene, dict) else {}
    width_m = max(0.001, float(bounds.get("width_m") or 0.0))
    depth_m = max(0.001, float(bounds.get("depth_m") or 0.0))
    if not route_stops:
        return floorplan
    margin_x = max(46, int(round(size[0] * 0.065)))
    margin_y = max(38, int(round(size[1] * 0.075)))
    radius = max(12, int(round(min(size) * 0.02)))
    previous_point: tuple[int, int] | None = None
    for index, stop in enumerate(route_stops[:8], start=1):
        focus = dict(stop.get("focus") or {}) if isinstance(stop.get("focus"), dict) else {}
        x_pct = _clamp_float((((float(focus.get("x") or 0.0) / width_m) + 0.5) * 100.0), 8.0, 92.0)
        y_pct = _clamp_float((((float(focus.get("z") or 0.0) / depth_m) + 0.5) * 100.0), 10.0, 90.0)
        marker_x = int(round(margin_x + ((size[0] - (margin_x * 2)) * x_pct / 100.0)))
        marker_y = int(round(margin_y + ((size[1] - (margin_y * 2)) * y_pct / 100.0)))
        point = (marker_x, marker_y)
        if previous_point is not None:
            draw.line((previous_point[0], previous_point[1], point[0], point[1]), fill=palette["accent"], width=6)
        draw.ellipse(
            (marker_x - radius, marker_y - radius, marker_x + radius, marker_y + radius),
            fill=palette["accent_soft"],
            outline=palette["accent"],
            width=5,
        )
        _draw_centered_text(
            draw,
            (marker_x, marker_y),
            str(index),
            font=marker_font,
            fill=(96, 72, 38),
        )
        previous_point = point
    return floorplan


def _write_public_png(image: Image.Image, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    proc_parent = re.fullmatch(
        r"/proc/self/fd/([0-9]+)",
        os.fspath(output_path.parent),
    )
    ordinary_parent_was_valid = False
    if proc_parent is None:
        try:
            ordinary_parent_was_valid = stat.S_ISDIR(
                output_path.parent.stat(follow_symlinks=False).st_mode
            )
        except OSError:
            ordinary_parent_was_valid = False
        if not ordinary_parent_was_valid:
            raise RuntimeError("public_preview_parent_invalid")
    try:
        directory_fd, parent_metadata = _open_directory_anchor(
            output_path.parent
        )
    except OSError:
        raise RuntimeError(
            "public_preview_parent_changed"
            if ordinary_parent_was_valid
            else "public_preview_parent_invalid"
        ) from None
    temporary_fd = -1
    temporary_name = ""
    backup_name = ""
    published_identity: tuple[int, int] | None = None
    published_to_directory = False
    committed = False
    try:
        opened_parent = os.fstat(directory_fd)
        if (
            opened_parent.st_dev != parent_metadata.st_dev
            or opened_parent.st_ino != parent_metadata.st_ino
            or not stat.S_ISDIR(opened_parent.st_mode)
        ):
            raise RuntimeError("public_preview_parent_changed")
        temporary_name = (
            f".{output_path.name}.{secrets.token_hex(16)}.tmp"
        )
        temporary_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        temporary_fd = os.open(
            temporary_name,
            temporary_flags,
            0o600,
            dir_fd=directory_fd,
        )
        with os.fdopen(temporary_fd, "wb") as handle:
            temporary_fd = -1
            try:
                image.save(handle, format="PNG", optimize=True)
            except (OSError, ValueError):
                raise RuntimeError("public_preview_encode_failed") from None
            handle.flush()
            os.fchmod(handle.fileno(), 0o644)
            os.fsync(handle.fileno())
            temporary_metadata = os.fstat(handle.fileno())
            published_identity = (
                temporary_metadata.st_dev,
                temporary_metadata.st_ino,
            )
        try:
            existing = os.stat(
                output_path.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not (
                stat.S_ISREG(existing.st_mode)
                or stat.S_ISLNK(existing.st_mode)
            ):
                raise RuntimeError("public_preview_existing_target_invalid")
            backup_name = (
                f".{output_path.name}.{secrets.token_hex(16)}.backup"
            )
            os.rename(
                output_path.name,
                backup_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
        os.replace(
            temporary_name,
            output_path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temporary_name = ""
        published_to_directory = True
        published = os.stat(
            output_path.name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(published.st_mode)
            or stat.S_IMODE(published.st_mode) != 0o644
            or published_identity != (published.st_dev, published.st_ino)
        ):
            raise RuntimeError("public_preview_publish_invalid")
        os.fsync(directory_fd)
        current_directory_fd = -1
        try:
            current_directory_fd, current_parent = _open_directory_anchor(
                output_path.parent
            )
            current_target = os.stat(
                output_path.name,
                dir_fd=current_directory_fd,
                follow_symlinks=False,
            )
            if (
                current_parent.st_dev != opened_parent.st_dev
                or current_parent.st_ino != opened_parent.st_ino
                or published_identity
                != (current_target.st_dev, current_target.st_ino)
            ):
                raise RuntimeError("public_preview_parent_changed")
        except OSError:
            raise RuntimeError("public_preview_parent_changed") from None
        finally:
            if current_directory_fd >= 0:
                os.close(current_directory_fd)
        if backup_name:
            os.unlink(backup_name, dir_fd=directory_fd)
            backup_name = ""
            os.fsync(directory_fd)
        committed = True
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if temporary_name:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        if published_to_directory and not committed:
            try:
                current = os.stat(
                    output_path.name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                current = None
            if (
                current is not None
                and published_identity == (current.st_dev, current.st_ino)
            ):
                try:
                    os.unlink(output_path.name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
            if backup_name:
                try:
                    os.replace(
                        backup_name,
                        output_path.name,
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                    )
                    backup_name = ""
                except OSError:
                    pass
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
        if backup_name and not committed:
            try:
                os.replace(
                    backup_name,
                    output_path.name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                )
                backup_name = ""
                os.fsync(directory_fd)
            except OSError:
                pass
        os.close(directory_fd)


def _write_generated_reconstruction_diorama_preview(
    output_path: Path,
    *,
    floorplan_path: Path,
    photo_paths: list[Path],
    walkable_scene: dict[str, object],
    style_label: str = "",
    style_scene: dict[str, object] | None = None,
    floorplan_inferred: bool = False,
) -> dict[str, object]:
    route_stops = [dict(stop) for stop in list(walkable_scene.get("route") or []) if isinstance(stop, dict)]
    applied_style_scene = style_scene or build_style_scene(
        reconstruction_style(),
        route_stop_count=max(1, len(route_stops)),
    )
    style_preview_ready, style_preview_reason = validate_style_scene(applied_style_scene)
    if not style_preview_ready:
        return {
            "status": "failed",
            "reason": "diorama_preview_style_evidence_invalid",
            "detail": style_preview_reason,
        }
    palette = _style_scene_preview_palette(
        applied_style_scene,
        style_label=style_label,
    )
    renderer_palette = _generated_reconstruction_diorama_palette(style_label)
    max_route_rows = 12
    route_labels = [
        _compact_route_label(
            stop.get("label") or stop.get("room") or stop.get("name"),
            fallback=f"Stop {index + 1}",
            limit=24,
        )
        for index, stop in enumerate(route_stops[:max_route_rows])
    ]
    route_stop_count = max(1, len(route_stops))
    photo_count = len(photo_paths)
    if floorplan_inferred:
        source_mode = "inferred_schematic_and_listing_photos"
        source_noun = "an inferred schematic and listing photos"
        source_disclosure = _generated_reconstruction_disclosure(
            photo_count=photo_count,
            floorplan_inferred=True,
        )
        body_copy = "Perspective staging from an inferred photo schematic."
        footer_copy = (
            "Inferred from listing photos; layout and dimensions are unverified. "
            "Not a captured tour."
        )
    else:
        source_mode = "floorplan_and_listing_photos" if photo_count else "floorplan_only"
        source_noun = "the floor plan and listing photos" if photo_count else "the floor plan"
        source_disclosure = (
            f"Generated from {source_noun}. Use it as a layout-first briefing image, not as a captured tour."
        )
        body_copy = f"Perspective staging from {source_noun}."
        footer_copy = source_disclosure
    preview_sources = list(photo_paths[:3]) or [floorplan_path]
    try:
        from app.product.property_diorama_preview import (
            render_bright_apartment_diorama,
        )

        rendered_diorama = render_bright_apartment_diorama(
            floorplan_path=floorplan_path,
            walkable_scene=walkable_scene,
            palette=renderer_palette,
            hero_path=photo_paths[0] if photo_paths else None,
            source_photo_count=photo_count,
            canvas_size=(1600, 1100),
        )
        if rendered_diorama is not None:
            canvas, composition = rendered_diorama
            canvas = canvas.convert("RGBA")
            source_panel_specs = (
                {"size": (250, 172), "rotate": -7.0, "anchor": (146, 370)},
                {"size": (330, 222), "rotate": 1.6, "anchor": (784, 86)},
                {"size": (250, 172), "rotate": 6.4, "anchor": (1260, 190)},
            )
            source_panel_boxes: list[list[int]] = []
            for index, source_path in enumerate(
                preview_sources[: len(source_panel_specs)]
            ):
                panel_spec = source_panel_specs[index]
                panel_size = tuple(panel_spec["size"])
                panel_card = _generated_reconstruction_mount_card(
                    _generated_reconstruction_fit_cover(source_path, panel_size),
                    max_size=panel_size,
                    frame_px=16 if index == 1 else 14,
                    rotate_degrees=float(panel_spec["rotate"]),
                    matte_color=renderer_palette["matte"],
                    shadow_alpha=92 if index == 1 else 78,
                )
                anchor_x, anchor_y = panel_spec["anchor"]
                anchor_x = min(
                    int(anchor_x),
                    canvas.width - panel_card.width - 24,
                )
                anchor_y = min(
                    int(anchor_y),
                    canvas.height - panel_card.height - 24,
                )
                canvas.alpha_composite(panel_card, (anchor_x, anchor_y))
                source_panel_boxes.append(
                    [
                        anchor_x,
                        anchor_y,
                        anchor_x + panel_card.width,
                        anchor_y + panel_card.height,
                    ]
                )
            style_vignette = _draw_style_scene_preview_vignette(
                canvas,
                style_scene=applied_style_scene,
                box=(760, 330, 1120, 790),
            )
            style_vignette_box = [
                int(value) for value in list(style_vignette.get("box") or [])
            ]
            composition_boxes = dict(composition.get("boxes") or {})
            composition_boxes["style_vignette"] = style_vignette_box
            composition_boxes["source_panels"] = source_panel_boxes
            composition["boxes"] = composition_boxes
            layout_checks = dict(composition.get("checks") or {})
            layout_checks["source_panels_fit_canvas"] = all(
                _preview_rect_contains(
                    (0, 0, canvas.width, canvas.height),
                    tuple(panel_box),
                )
                for panel_box in source_panel_boxes
            )
            layout_checks["style_vignette_fits_canvas"] = (
                len(style_vignette_box) == 4
                and _preview_rect_contains(
                    (0, 0, canvas.width, canvas.height),
                    tuple(style_vignette_box),
                )
            )
            rendered_style_cues = [
                str(value)
                for value in list(style_vignette.get("rendered_cues") or [])
            ]
            layout_checks["style_vignette_covers_required_cues"] = set(
                rendered_style_cues
            ) == set(
                str(value)
                for value in list(applied_style_scene.get("required_cues") or [])
            )
            composition["checks"] = layout_checks
            failed_layout_checks = [name for name, passed in layout_checks.items() if not passed]
            if failed_layout_checks:
                raise RuntimeError(
                    f"diorama_thumbnail_contract_failed:{','.join(failed_layout_checks)}"
                )
            output_path.parent.mkdir(parents=True, exist_ok=True)
            canvas.save(output_path, format="PNG", optimize=True)
            layout = {
                "status": "pass",
                **composition,
                "displayed_route_stop_count": len(route_labels),
                "displayed_route_labels": route_labels,
                "route_sequence_complete": len(route_labels) == len(route_stops),
            }
            return {
                "status": "generated",
                "bundle_relpath": output_path.name,
                "sha256": _sha256(output_path),
                "size_bytes": output_path.stat().st_size,
                "source_mode": source_mode,
                "source_photo_count": photo_count,
                "source_disclosure": source_disclosure,
                "preview_kind": "bright_playful_cutaway_with_style_scene",
                "style_id": str(applied_style_scene.get("style_id") or ""),
                "style_signature": str(applied_style_scene.get("style_signature") or ""),
                "style_scene_signature": str(applied_style_scene.get("scene_signature") or ""),
                "style_evidence_status": "ready",
                "rendered_style_cues": rendered_style_cues,
                "layout": layout,
            }
    except Exception:
        # Retain the established scrapbook composition as a safe local fallback.
        pass
    try:
        eyebrow_font = _preview_font(22, bold=True)
        title_font = _preview_font(52, serif=True, bold=True)
        body_font = _preview_font(20)
        chip_font = _preview_font(16, bold=True)
        rail_heading_font = _preview_font(24, bold=True)
        rail_label_font = _preview_font(17, bold=True)
        rail_copy_font = _preview_font(16)
        footer_font = _preview_font(18)
        hero_background = _generated_reconstruction_fit_cover(preview_sources[0], (1600, 1100))
        floorplan_stage_image = _generated_reconstruction_floorplan_with_route(
            floorplan_path,
            walkable_scene=walkable_scene,
            size=(930, 560),
            palette=palette,
        )
        canvas = Image.new("RGBA", (1600, 1100), (*palette["wash"], 255))
        background = hero_background.filter(ImageFilter.GaussianBlur(18))
        background = Image.blend(background, Image.new("RGB", canvas.size, palette["wash"]), 0.82)
        canvas.alpha_composite(background.convert("RGBA"))
        draw = ImageDraw.Draw(canvas)
        draw.ellipse((62, 54, 612, 412), fill=(255, 255, 255, 42))
        draw.ellipse((980, 88, 1538, 468), fill=(255, 255, 255, 28))

        stage_surface = Image.new(
            "RGBA",
            (floorplan_stage_image.width + 64, floorplan_stage_image.height + 64),
            (*palette["matte"], 255),
        )
        stage_surface.paste(floorplan_stage_image, (32, 32))
        stage_draw = ImageDraw.Draw(stage_surface)
        stage_draw.rounded_rectangle(
            (10, 10, stage_surface.width - 11, stage_surface.height - 11),
            radius=34,
            outline=(216, 202, 176, 255),
            width=3,
        )

        stage_depth_fill = Image.blend(
            stage_surface.convert("RGB"),
            Image.new("RGB", stage_surface.size, (126, 108, 82)),
            0.58,
        ).convert("RGBA")
        stage_depth_draw = ImageDraw.Draw(stage_depth_fill)
        stage_depth_draw.rectangle(
            (0, stage_surface.height - 16, stage_surface.width, stage_surface.height),
            fill=(*palette["accent"], 235),
        )
        stage_shadow = Image.new(
            "RGBA",
            (stage_surface.width + 96, stage_surface.height + 96),
            (0, 0, 0, 0),
        )
        stage_shadow.alpha_composite(
            Image.new("RGBA", stage_surface.size, (28, 24, 20, 72)),
            (32, 40),
        )
        stage_shadow = stage_shadow.filter(ImageFilter.GaussianBlur(24))
        stage_stack = Image.new("RGBA", stage_shadow.size, (0, 0, 0, 0))
        stage_stack.alpha_composite(stage_shadow)
        stage_stack.alpha_composite(stage_depth_fill, (20, 26))
        stage_stack.alpha_composite(stage_surface, (0, 0))
        rotated_stage = stage_stack.rotate(-5.5, resample=Image.Resampling.BICUBIC, expand=True)
        stage_anchor = (94, min(268, max(220, canvas.height - rotated_stage.height - 8)))
        canvas.alpha_composite(rotated_stage, stage_anchor)
        stage_box = (
            stage_anchor[0],
            stage_anchor[1],
            stage_anchor[0] + rotated_stage.width,
            stage_anchor[1] + rotated_stage.height,
        )

        panel_specs = [
            {"size": (250, 172), "rotate": -7.0, "anchor": (146, 370)},
            {"size": (330, 222), "rotate": 1.6, "anchor": (784, 86)},
            {"size": (250, 172), "rotate": 6.4, "anchor": (1260, 190)},
        ]
        panel_boxes: list[tuple[int, int, int, int]] = []
        for index, source_path in enumerate(preview_sources[: len(panel_specs)]):
            spec = panel_specs[index]
            panel_card = _generated_reconstruction_mount_card(
                _generated_reconstruction_fit_cover(source_path, spec["size"]),
                max_size=spec["size"],
                frame_px=16 if index == 1 else 14,
                rotate_degrees=float(spec["rotate"]),
                matte_color=palette["matte"],
                shadow_alpha=92 if index == 1 else 78,
            )
            anchor_x, anchor_y = spec["anchor"]
            anchor_x = min(int(anchor_x), canvas.width - panel_card.width - 24)
            anchor_y = min(int(anchor_y), canvas.height - panel_card.height - 24)
            canvas.alpha_composite(panel_card, (anchor_x, anchor_y))
            panel_boxes.append((anchor_x, anchor_y, anchor_x + panel_card.width, anchor_y + panel_card.height))

        style_vignette = _draw_style_scene_preview_vignette(
            canvas,
            style_scene=applied_style_scene,
            box=(760, 330, 1120, 790),
        )
        style_vignette_box = tuple(int(value) for value in list(style_vignette.get("box") or []))

        title_box = (70, 64, 752, 360)
        draw.rounded_rectangle(
            title_box,
            radius=34,
            fill=(255, 252, 245, 232),
            outline=(216, 202, 176, 255),
            width=2,
        )
        eyebrow_origin = (100, 91)
        first_title_origin = (100, 121)
        second_title_origin = (100, 172)
        body_origin = (102, 234)
        draw.text(eyebrow_origin, "Generated diorama", font=eyebrow_font, fill=(96, 72, 38))
        draw.text(first_title_origin, "Layout-first", font=title_font, fill=(42, 35, 26))
        draw.text(second_title_origin, "room route", font=title_font, fill=(42, 35, 26))
        body_bottom = _draw_wrapped_text(
            draw,
            body_origin,
            body_copy,
            font=body_font,
            fill=(84, 66, 40),
            max_width=610,
            line_gap=2,
        )
        chip_y = max(292, body_bottom + 10)
        first_chip = _draw_text_chip(
            draw,
            (100, chip_y),
            f"{route_stop_count} route stops",
            font=chip_font,
            fill=(255, 248, 236, 238),
            outline=(216, 202, 176, 255),
            text_fill=(96, 72, 38),
            pad_x=12,
            pad_y=6,
        )
        second_chip = _draw_text_chip(
            draw,
            (first_chip[2] + 8, chip_y),
            f"{photo_count} source photos",
            font=chip_font,
            fill=(255, 252, 245, 236),
            outline=(216, 202, 176, 255),
            text_fill=(96, 72, 38),
            pad_x=12,
            pad_y=6,
        )
        title_content_boxes: list[tuple[int, int, int, int]] = [
            draw.textbbox(eyebrow_origin, "Generated diorama", font=eyebrow_font),
            draw.textbbox(first_title_origin, "Layout-first", font=title_font),
            draw.textbbox(second_title_origin, "room route", font=title_font),
            _preview_wrapped_text_box(body_origin, body_copy, font=body_font, max_width=610, line_gap=2),
            first_chip,
            second_chip,
        ]
        if style_label:
            style_chip_label = str(style_label).rsplit("·", 1)[-1].strip() or str(style_label).strip()
            style_chip = _draw_text_chip(
                draw,
                (second_chip[2] + 8, chip_y),
                _compact_route_label(style_chip_label, limit=28),
                font=chip_font,
                fill=(*palette["accent_soft"], 242),
                outline=palette["accent"],
                text_fill=(96, 72, 38),
                pad_x=12,
                pad_y=6,
            )
            title_content_boxes.append(style_chip)

        route_rail_box = (1138, 452, 1526, 992)
        draw.rounded_rectangle(
            route_rail_box,
            radius=34,
            fill=(255, 252, 245, 236),
            outline=(216, 202, 176, 255),
            width=2,
        )
        rail_heading_origin = (1164, 478)
        rail_copy_origin = (1164, 510)
        route_sequence_complete = len(route_labels) == len(route_stops)
        rail_copy = (
            f"Guided sequence · all {route_stop_count} stops"
            if route_sequence_complete
            else f"Guided sequence · first {len(route_labels)} of {route_stop_count}"
        )
        draw.text(rail_heading_origin, "Route sequence", font=rail_heading_font, fill=(45, 38, 28))
        draw.text(rail_copy_origin, rail_copy, font=rail_copy_font, fill=(96, 72, 38))
        displayed_route_labels = route_labels or ["Route overview"]
        route_row_top = 548
        route_row_gap = 6
        route_rows_bottom = route_rail_box[3] - 24
        route_row_height = min(
            38,
            max(
                28,
                int(
                    (route_rows_bottom - route_row_top - (route_row_gap * (len(displayed_route_labels) - 1)))
                    / max(1, len(displayed_route_labels))
                ),
            ),
        )
        route_row_boxes: list[tuple[int, int, int, int]] = []
        route_label_boxes: list[tuple[int, int, int, int]] = []
        fitted_route_labels: list[str] = []
        for index, label in enumerate(displayed_route_labels):
            row_top = route_row_top + (index * (route_row_height + route_row_gap))
            row_box = (1162, row_top, 1502, row_top + route_row_height)
            draw.rounded_rectangle(
                row_box,
                radius=16,
                fill=(255, 252, 245, 244 if index == 0 else 218),
                outline=(216, 202, 176, 255),
                width=2,
            )
            badge_center = (1184, row_top + (route_row_height // 2))
            badge_radius = min(13, max(10, (route_row_height // 2) - 4))
            draw.ellipse(
                (
                    badge_center[0] - badge_radius,
                    badge_center[1] - badge_radius,
                    badge_center[0] + badge_radius,
                    badge_center[1] + badge_radius,
                ),
                fill=(*palette["accent_soft"], 255),
                outline=palette["accent"],
                width=2,
            )
            _draw_centered_text(draw, badge_center, str(index + 1), font=chip_font, fill=(96, 72, 38))
            fitted_label = _preview_fit_text(label, font=rail_label_font, max_width=row_box[2] - 1210 - 12)
            label_bbox = draw.textbbox((0, 0), fitted_label, font=rail_label_font)
            label_y = row_top + max(2, (route_row_height - (label_bbox[3] - label_bbox[1])) // 2 - label_bbox[1])
            label_origin = (1210, label_y)
            draw.text(label_origin, fitted_label, font=rail_label_font, fill=(45, 38, 28))
            route_row_boxes.append(row_box)
            route_label_boxes.append(draw.textbbox(label_origin, fitted_label, font=rail_label_font))
            fitted_route_labels.append(fitted_label)

        footer_box = (148, 1016, 1452, 1076)
        draw.rounded_rectangle(
            footer_box,
            radius=24,
            fill=(255, 252, 245, 238),
            outline=(216, 202, 176, 255),
            width=2,
        )
        footer_origin = (footer_box[0] + 24, footer_box[1] + 17)
        _draw_wrapped_text(
            draw,
            footer_origin,
            footer_copy,
            font=footer_font,
            fill=(84, 66, 40),
            max_width=(footer_box[2] - footer_box[0]) - 48,
            line_gap=2,
        )
        footer_text_box = _preview_wrapped_text_box(
            footer_origin,
            footer_copy,
            font=footer_font,
            max_width=(footer_box[2] - footer_box[0]) - 48,
            line_gap=2,
        )

        canvas_box = (0, 0, canvas.width, canvas.height)
        key_region_boxes = [title_box, route_rail_box, footer_box]
        layout_checks = {
            "stage_fits_canvas": _preview_rect_contains(canvas_box, stage_box),
            "panels_fit_canvas": all(_preview_rect_contains(canvas_box, box) for box in panel_boxes),
            "title_content_fits_card": all(
                _preview_rect_contains(title_box, box, padding=12) for box in title_content_boxes
            ),
            "route_rows_fit_rail": all(_preview_rect_contains(route_rail_box, box, padding=12) for box in route_row_boxes),
            "route_labels_fit_rows": all(
                _preview_rect_contains(row_box, label_box, padding=6)
                for row_box, label_box in zip(route_row_boxes, route_label_boxes)
            ),
            "route_rows_do_not_overlap": all(
                not _preview_rects_overlap(first, second)
                for first, second in zip(route_row_boxes, route_row_boxes[1:])
            ),
            "footer_copy_fits_card": _preview_rect_contains(footer_box, footer_text_box, padding=12),
            "key_regions_fit_canvas": all(_preview_rect_contains(canvas_box, box) for box in key_region_boxes),
            "key_regions_do_not_overlap": all(
                not _preview_rects_overlap(first, second)
                for index, first in enumerate(key_region_boxes)
                for second in key_region_boxes[index + 1 :]
            ),
            "style_vignette_fits_canvas": len(style_vignette_box) == 4
            and _preview_rect_contains(canvas_box, style_vignette_box),
            "style_vignette_covers_required_cues": set(
                str(value) for value in list(style_vignette.get("rendered_cues") or [])
            )
            == set(str(value) for value in list(applied_style_scene.get("required_cues") or [])),
        }
        failed_layout_checks = [name for name, passed in layout_checks.items() if not passed]
        if failed_layout_checks:
            raise RuntimeError(f"preview_layout_contract_failed:{','.join(failed_layout_checks)}")

        public_preview = canvas.convert("RGB")
        try:
            _write_public_png(public_preview, output_path)
        finally:
            public_preview.close()
        return {
            "status": "generated",
            "bundle_relpath": output_path.name,
            "sha256": _sha256(output_path),
            "size_bytes": output_path.stat().st_size,
            "source_mode": source_mode,
            "source_photo_count": photo_count,
            "source_disclosure": source_disclosure,
            "preview_kind": "deterministic_style_scene_vignette",
            "style_id": str(applied_style_scene.get("style_id") or ""),
            "style_signature": str(applied_style_scene.get("style_signature") or ""),
            "style_scene_signature": str(applied_style_scene.get("scene_signature") or ""),
            "style_evidence_status": "ready",
            "rendered_style_cues": list(style_vignette.get("rendered_cues") or []),
            "layout": {
                "status": "pass",
                "canvas_size_px": {"width": canvas.width, "height": canvas.height},
                "checks": layout_checks,
                "boxes": {
                    "title": list(title_box),
                    "stage": list(stage_box),
                    "route_rail": list(route_rail_box),
                    "route_rows": [list(box) for box in route_row_boxes],
                    "route_labels": [list(box) for box in route_label_boxes],
                    "footer": list(footer_box),
                    "source_panels": [list(box) for box in panel_boxes],
                    "style_vignette": list(style_vignette_box),
                },
                "displayed_route_stop_count": len(route_labels),
                "displayed_route_labels": fitted_route_labels,
                "route_sequence_complete": route_sequence_complete,
            },
        }
    except Exception as exc:
        return {
            "status": "failed",
            "reason": "diorama_preview_generation_failed",
            "error_class": type(exc).__name__,
        }


def _write_generated_reconstruction_telegram_preview(
    output_path: Path,
    *,
    source_path: Path,
    style_label: str = "",
    style_scene: dict[str, object] | None = None,
) -> dict[str, object]:
    palette = _style_scene_preview_palette(style_scene, style_label=style_label)
    try:
        with Image.open(source_path) as image:
            base = ImageOps.exif_transpose(image).convert("RGB")
            width, height = base.size
            canvas_width = 1600
            canvas_height = 1000
            background = ImageOps.fit(base, (canvas_width, canvas_height), Image.Resampling.LANCZOS)
            background = background.filter(ImageFilter.GaussianBlur(24))
            background = Image.blend(background, Image.new("RGB", background.size, palette["wash"]), 0.72)
            scale = min(1460 / max(width, 1), 900 / max(height, 1))
            scaled = base.resize(
                (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
                Image.Resampling.LANCZOS,
            )
            canvas = background.convert("RGBA")
            offset_x = (canvas_width - scaled.size[0]) // 2
            offset_y = (canvas_height - scaled.size[1]) // 2
            image_box = (offset_x, offset_y, offset_x + scaled.width, offset_y + scaled.height)
            frame_box = (image_box[0] - 12, image_box[1] - 12, image_box[2] + 12, image_box[3] + 12)
            shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
            shadow_draw = ImageDraw.Draw(shadow, "RGBA")
            shadow_draw.rounded_rectangle(
                (frame_box[0] + 14, frame_box[1] + 18, frame_box[2] + 14, frame_box[3] + 18),
                radius=24,
                fill=(28, 24, 20, 96),
            )
            shadow = shadow.filter(ImageFilter.GaussianBlur(22))
            canvas.alpha_composite(shadow)
            canvas_draw = ImageDraw.Draw(canvas, "RGBA")
            canvas_draw.rounded_rectangle(
                frame_box,
                radius=20,
                fill=(*palette["matte"], 255),
                outline=(216, 202, 176, 255),
                width=3,
            )
            canvas.alpha_composite(scaled.convert("RGBA"), (offset_x, offset_y))
            canvas_box = (0, 0, canvas_width, canvas_height)
            layout_checks = {
                "frame_fits_canvas": _preview_rect_contains(canvas_box, frame_box),
                "full_source_image_visible": _preview_rect_contains(frame_box, image_box),
                "source_occupies_useful_height": (scaled.height / canvas_height) >= 0.84,
                "source_aspect_ratio_preserved": abs((scaled.width / scaled.height) - (width / height)) <= 0.002,
            }
            failed_layout_checks = [name for name, passed in layout_checks.items() if not passed]
            if failed_layout_checks:
                raise RuntimeError(f"telegram_preview_layout_contract_failed:{','.join(failed_layout_checks)}")
            public_preview = canvas.convert("RGB")
            try:
                _write_public_png(public_preview, output_path)
            finally:
                public_preview.close()
        return {
            "status": "generated",
            "bundle_relpath": output_path.name,
            "sha256": _sha256(output_path),
            "size_bytes": output_path.stat().st_size,
            "source_sha256": _sha256(source_path),
            "composition": "telegram_share_fit_full_diorama",
            "style_id": str((style_scene or {}).get("style_id") or ""),
            "style_signature": str((style_scene or {}).get("style_signature") or ""),
            "style_scene_signature": str((style_scene or {}).get("scene_signature") or ""),
            "style_evidence_status": str((style_scene or {}).get("evidence_status") or ""),
            "layout": {
                "status": "pass",
                "canvas_size_px": {"width": canvas_width, "height": canvas_height},
                "checks": layout_checks,
                "boxes": {"frame": list(frame_box), "source_image": list(image_box)},
            },
        }
    except Exception as exc:
        return {
            "status": "failed",
            "reason": "telegram_preview_generation_failed",
            "error_class": type(exc).__name__,
        }


def _walkthrough_route_map_inset(
    *,
    floorplan_thumb: Image.Image | None,
    route_markers: list[dict[str, object]],
    active_index: int,
    palette: dict[str, tuple[int, int, int]],
) -> Image.Image:
    map_width = max(1, WALKTHROUGH_MAP_BOX[2] - WALKTHROUGH_MAP_BOX[0])
    map_height = max(1, WALKTHROUGH_MAP_BOX[3] - WALKTHROUGH_MAP_BOX[1])
    if floorplan_thumb is not None:
        inset = ImageOps.fit(floorplan_thumb.copy(), (map_width, map_height), Image.Resampling.LANCZOS)
        inset = Image.blend(inset, Image.new("RGB", inset.size, palette["floorplan_wash"]), 0.12)
    else:
        inset = Image.new("RGB", (map_width, map_height), palette["floorplan_wash"])
    draw = ImageDraw.Draw(inset)
    previous_point: tuple[int, int] | None = None
    for index, marker in enumerate(route_markers):
        marker_x = int(round((_clamp_float(float(marker.get("x_pct") or 0.0), 0.0, 100.0) / 100.0) * (map_width - 1)))
        marker_y = int(round((_clamp_float(float(marker.get("y_pct") or 0.0), 0.0, 100.0) / 100.0) * (map_height - 1)))
        point = (marker_x, marker_y)
        if previous_point is not None:
            draw.line((previous_point[0], previous_point[1], point[0], point[1]), fill=palette["accent"], width=5)
        radius = 11 if index == active_index else 8
        fill = palette["accent_soft"] if index <= active_index else (255, 252, 245)
        outline = palette["accent"] if index == active_index else (167, 124, 43)
        draw.ellipse(
            (marker_x - radius, marker_y - radius, marker_x + radius, marker_y + radius),
            fill=fill,
            outline=outline,
            width=4,
        )
        draw.text((marker_x - 5, marker_y - 7), str(index + 1), fill=palette["accent"])
        previous_point = point
    return inset


def _walkthrough_floorplan_thumb(floorplan_image: Path | None) -> Image.Image | None:
    if floorplan_image is None or not floorplan_image.exists():
        return None
    map_width = max(1, WALKTHROUGH_MAP_BOX[2] - WALKTHROUGH_MAP_BOX[0])
    map_height = max(1, WALKTHROUGH_MAP_BOX[3] - WALKTHROUGH_MAP_BOX[1])
    with Image.open(floorplan_image) as image:
        floorplan_thumb = ImageOps.exif_transpose(image).convert("RGB")
        return ImageOps.fit(floorplan_thumb, (map_width, map_height), Image.Resampling.LANCZOS)


def _walkthrough_route_markers(
    expected_segments: list[str] | tuple[str, ...],
    *,
    walkable_scene: dict[str, object] | None,
) -> list[dict[str, object]]:
    route_stops = (
        [dict(stop) for stop in list((walkable_scene or {}).get("route") or []) if isinstance(stop, dict)]
        if isinstance(walkable_scene, dict)
        else []
    )
    bounds = dict((walkable_scene or {}).get("bounds") or {}) if isinstance(walkable_scene, dict) else {}
    bound_width = max(0.001, float(bounds.get("width_m") or 0.0))
    bound_depth = max(0.001, float(bounds.get("depth_m") or 0.0))
    route_markers: list[dict[str, object]] = []
    for index, label in enumerate(list(expected_segments or [])):
        stop = route_stops[index] if index < len(route_stops) else {}
        focus = dict(stop.get("focus") or {}) if isinstance(stop.get("focus"), dict) else {}
        if focus:
            marker_x = _clamp_float((((float(focus.get("x") or 0.0) / bound_width) + 0.5) * 100.0), 10.0, 90.0)
            marker_y = _clamp_float((((float(focus.get("z") or 0.0) / bound_depth) + 0.5) * 100.0), 12.0, 88.0)
        else:
            denominator = max(1, len(expected_segments) - 1)
            marker_x = 16.0 + ((68.0 / denominator) * index if denominator else 34.0)
            marker_y = 52.0 + (8.0 if index % 2 else -8.0)
        route_markers.append({"label": label, "x_pct": marker_x, "y_pct": marker_y})
    return route_markers


def _render_walkthrough_stop_card(
    *,
    stop_index: int,
    label: str,
    expected_segments: list[str],
    source_path: Path,
    supporting_path: Path | None,
    floorplan_thumb: Image.Image | None,
    route_markers: list[dict[str, object]],
    style_label: str = "",
) -> Image.Image:
    card_w, card_h = WALKTHROUGH_CARD_SIZE
    palette = _generated_reconstruction_diorama_palette(style_label)
    eyebrow_font = _preview_font(22, bold=True)
    headline_font = _preview_font(52, serif=True, bold=True)
    meta_font = _preview_font(20, bold=True)
    body_font = _preview_font(20)
    panel_heading_font = _preview_font(23, bold=True)
    panel_label_font = _preview_font(26, serif=True, bold=True)
    footer_font = _preview_font(18)
    chip_font = _preview_font(18, bold=True)
    progress_number_font = _preview_font(16, bold=True)
    hero_source = _generated_reconstruction_fit_cover(source_path, (card_w, card_h))
    background = hero_source.filter(ImageFilter.GaussianBlur(24))
    background = Image.blend(background, Image.new("RGB", (card_w, card_h), palette["wash"]), 0.76)
    card = background.convert("RGBA")
    draw = ImageDraw.Draw(card)
    draw.rectangle((0, 0, card_w - 1, 128), fill=(251, 247, 241, 218))
    draw.rectangle((0, card_h - 104, card_w - 1, card_h - 1), fill=(247, 241, 232, 228))
    draw.rectangle((0, 0, card_w - 1, card_h - 1), outline=(216, 202, 176, 255), width=3)
    header_box = (52, 28, 720, 142)
    draw.rounded_rectangle(
        header_box,
        radius=30,
        fill=(251, 247, 241, 230),
        outline=(216, 202, 176, 255),
        width=2,
    )
    draw.text((76, 48), "Layout walkthrough", font=eyebrow_font, fill=(45, 38, 28))
    draw.text((76, 74), label, font=headline_font, fill=(45, 38, 28))
    stop_chip = _draw_text_chip(
        draw,
        (530, 46),
        f"Stop {stop_index + 1} of {len(expected_segments)}",
        font=chip_font,
        fill=(*palette["accent_soft"], 236),
        outline=palette["accent"],
        text_fill=(96, 72, 38),
    )
    if style_label:
        _draw_text_chip(
            draw,
            (stop_chip[0], stop_chip[3] + 10),
            style_label,
            font=chip_font,
            fill=(255, 252, 245, 224),
            outline=(216, 202, 176, 255),
            text_fill=(96, 72, 38),
        )

    hero_card = _generated_reconstruction_mount_card(
        _generated_reconstruction_fit_cover(source_path, (820, 500)),
        max_size=(820, 500),
        frame_px=18,
        rotate_degrees=-2.8 if stop_index % 2 == 0 else 2.8,
        matte_color=palette["matte"],
        shadow_alpha=92,
    )
    hero_anchor_x = 72
    hero_anchor_y = 170
    card.alpha_composite(hero_card, (hero_anchor_x, hero_anchor_y))

    if supporting_path is not None and supporting_path.exists():
        supporting_card = _generated_reconstruction_mount_card(
            _generated_reconstruction_fit_cover(supporting_path, (276, 188)),
            max_size=(276, 188),
            frame_px=12,
            rotate_degrees=7.5 if stop_index % 2 == 0 else -7.5,
            matte_color=palette["matte"],
            shadow_alpha=78,
        )
        support_x = 664
        support_y = 470
        card.alpha_composite(supporting_card, (support_x, support_y))

    panel_box = (952, 146, 1368, 638)
    draw.rounded_rectangle(panel_box, radius=30, fill=(255, 252, 245, 228), outline=(216, 202, 176, 255), width=3)
    draw.text((984, 176), "Route map", font=panel_heading_font, fill=(45, 38, 28))
    route_map = _walkthrough_route_map_inset(
        floorplan_thumb=floorplan_thumb,
        route_markers=route_markers,
        active_index=stop_index,
        palette=palette,
    )
    route_map_anchor = (WALKTHROUGH_MAP_BOX[0], WALKTHROUGH_MAP_BOX[1])
    draw.rounded_rectangle(
        (WALKTHROUGH_MAP_BOX[0] - 8, WALKTHROUGH_MAP_BOX[1] - 8, WALKTHROUGH_MAP_BOX[2] + 8, WALKTHROUGH_MAP_BOX[3] + 8),
        radius=24,
        fill=(251, 247, 241, 230),
        outline=(216, 202, 176, 255),
        width=2,
    )
    card.alpha_composite(route_map.convert("RGBA"), route_map_anchor)

    draw.text((984, 476), "Current stop", font=panel_heading_font, fill=(45, 38, 28))
    draw.text((984, 508), label, font=panel_label_font, fill=(96, 72, 38))

    progress_left = 984
    progress_right = 1334
    progress_y = 596
    if len(expected_segments) > 1:
        draw.line((progress_left, progress_y, progress_right, progress_y), fill=(214, 202, 180), width=4)
    for index, _segment in enumerate(expected_segments):
        if len(expected_segments) == 1:
            cx = (progress_left + progress_right) / 2
        else:
            cx = progress_left + ((progress_right - progress_left) * index / max(len(expected_segments) - 1, 1))
        fill = palette["accent"] if index <= stop_index else (255, 252, 245)
        outline = (138, 97, 23) if index == stop_index else (167, 124, 43)
        radius = 10 if index == stop_index else 7
        draw.ellipse((cx - radius, progress_y - radius, cx + radius, progress_y + radius), fill=fill, outline=outline, width=3)
        _draw_centered_text(draw, (cx, progress_y + 26), str(index + 1), font=progress_number_font, fill=(75, 57, 35))

    previous_label = expected_segments[stop_index - 1] if stop_index > 0 else "Arrival"
    next_label = expected_segments[stop_index + 1] if stop_index + 1 < len(expected_segments) else "Tour complete"
    info_box = (984, 648, 1340, 744)
    draw.rounded_rectangle(info_box, radius=20, fill=(255, 252, 245, 220), outline=(216, 202, 176, 255), width=2)
    draw.text((info_box[0] + 16, info_box[1] + 14), f"From: {previous_label}", font=meta_font, fill=(96, 72, 38))
    draw.text((info_box[0] + 16, info_box[1] + 42), f"Next: {next_label}", font=meta_font, fill=(96, 72, 38))
    _draw_wrapped_text(
        draw,
        (info_box[0] + 16, info_box[1] + 72),
        "Guided by the floor plan and listing photos.",
        font=body_font,
        fill=(96, 72, 38),
        max_width=info_box[2] - info_box[0] - 32,
        line_gap=2,
    )

    _draw_wrapped_text(
        draw,
        (76, card_h - 72),
        "Planning preview from source media. Confirm exact finishes, size, and sightlines at the viewing.",
        font=footer_font,
        fill=(96, 72, 38),
        max_width=1020,
        line_gap=2,
    )
    return card.convert("RGB")


def _upsert_public_asset(
    payload: dict[str, object],
    *,
    relpath: str,
    role: str,
    privacy_class: str = "public",
    mime_type: str = "",
    sha256: str = "",
    size_bytes: int | None = None,
) -> None:
    normalized_relpath = _safe_relpath(relpath)
    if not normalized_relpath:
        return
    normalized_sha256 = str(sha256 or "").strip().lower()
    if normalized_sha256 and not re.fullmatch(r"[0-9a-f]{64}", normalized_sha256):
        raise RuntimeError("public_asset_sha256_invalid")
    if size_bytes is not None and (
        isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes <= 0
    ):
        raise RuntimeError("public_asset_size_invalid")

    def _apply_binding(row: dict[str, object]) -> None:
        if normalized_sha256:
            row["sha256"] = normalized_sha256
        if size_bytes is not None:
            row["size_bytes"] = size_bytes

    public_assets = list(payload.get("public_assets") or []) if isinstance(payload.get("public_assets"), list) else []
    for row in public_assets:
        if isinstance(row, dict) and any(
            str(row.get(key) or "").strip() == normalized_relpath
            for key in ("path", "relpath", "asset_relpath")
        ):
            row["path"] = normalized_relpath
            row.pop("relpath", None)
            row.pop("asset_relpath", None)
            row.pop("privacy", None)
            row.pop("asset_role", None)
            row.pop("content_type", None)
            row["privacy_class"] = privacy_class
            row["role"] = role
            if mime_type:
                row["mime_type"] = mime_type
            _apply_binding(row)
            payload["public_assets"] = public_assets
            return
    row: dict[str, object] = {
        "path": normalized_relpath,
        "privacy_class": privacy_class,
        "role": role,
        **({"mime_type": mime_type} if mime_type else {}),
    }
    _apply_binding(row)
    public_assets.append(row)
    payload["public_assets"] = public_assets


def _public_generated_model_binding(path: Path) -> tuple[str, int]:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or details.st_size <= 0
            or details.st_size > 8 * 1024 * 1024
        ):
            raise RuntimeError("public_generated_model_asset_unsafe")
        digest = hashlib.sha256()
        total_bytes = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            total_bytes += len(chunk)
        confirmed = os.fstat(descriptor)
        if (
            total_bytes != details.st_size
            or (
                confirmed.st_dev,
                confirmed.st_ino,
                confirmed.st_mode,
                confirmed.st_nlink,
                confirmed.st_size,
                confirmed.st_mtime_ns,
                confirmed.st_ctime_ns,
            )
            != (
                details.st_dev,
                details.st_ino,
                details.st_mode,
                details.st_nlink,
                details.st_size,
                details.st_mtime_ns,
                details.st_ctime_ns,
            )
        ):
            raise RuntimeError("public_generated_model_asset_unsafe")
        return digest.hexdigest(), int(details.st_size)
    except OSError as exc:
        raise RuntimeError("public_generated_model_asset_unsafe") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _upsert_public_generated_model_assets(
    payload: dict[str, object],
    *,
    output_dir: Path,
    base_relpath: str,
    glb_export: dict[str, object],
) -> None:
    normalized_base_relpath = _safe_relpath(base_relpath)
    if not normalized_base_relpath or normalized_base_relpath != base_relpath:
        raise RuntimeError("public_generated_model_relpath_invalid")
    generated_model_assets: list[tuple[str, Path, str, str]] = [
        (
            f"{normalized_base_relpath}/model.obj",
            output_dir / "model.obj",
            "generated_reconstruction_model",
            "model/obj",
        ),
        (
            f"{normalized_base_relpath}/model.mtl",
            output_dir / "model.mtl",
            "generated_reconstruction_material",
            "model/mtl",
        ),
    ]
    if str(glb_export.get("status") or "").strip() == "generated":
        glb_relpath = _safe_relpath(
            str(glb_export.get("glb_relpath") or "model.glb").strip()
        )
        if not glb_relpath or "/" in glb_relpath:
            raise RuntimeError("public_generated_model_relpath_invalid")
        generated_model_assets.append(
            (
                f"{normalized_base_relpath}/{glb_relpath}",
                output_dir / glb_relpath,
                "generated_reconstruction_model",
                "model/gltf-binary",
            )
        )
    for relpath, model_path, role, mime_type in generated_model_assets:
        model_sha256, model_size_bytes = _public_generated_model_binding(model_path)
        _upsert_public_asset(
            payload,
            relpath=relpath,
            role=role,
            privacy_class="generated_reconstruction_public",
            mime_type=mime_type,
            sha256=model_sha256,
            size_bytes=model_size_bytes,
        )


def _upsert_diorama_scene(payload: dict[str, object], *, relpath: str) -> None:
    normalized_relpath = _safe_relpath(relpath)
    if not normalized_relpath:
        return
    scenes = [dict(scene) for scene in list(payload.get("scenes") or []) if isinstance(scene, dict)]
    inserted = False
    for scene in scenes:
        if str(scene.get("role") or "").strip().lower() != "diorama":
            continue
        scene["name"] = "Layout diorama"
        scene["role"] = "diorama"
        scene["ordinal"] = 0
        scene["mime_type"] = "image/png"
        scene["asset_relpath"] = normalized_relpath
        inserted = True
        break
    if not inserted:
        scenes.insert(
            0,
            {
                "name": "Layout diorama",
                "role": "diorama",
                "ordinal": 0,
                "mime_type": "image/png",
                "asset_relpath": normalized_relpath,
            },
        )
    payload["scenes"] = scenes


def _public_tour_dir() -> Path:
    configured = str(os.getenv("EA_PUBLIC_TOUR_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if running_container_public_tour_dir is not None:
        runtime_root = running_container_public_tour_dir(os.getenv("PROPERTYQUARRY_RUNTIME_CONTAINER") or "")
        if isinstance(runtime_root, Path):
            return runtime_root.expanduser().resolve()
    if preferred_public_tour_root is not None:
        return preferred_public_tour_root(
            configured_root="",
            repo_root=ROOT,
            fallback_root=ROOT / "state" / "public_property_tours",
            runtime_container=os.getenv("PROPERTYQUARRY_RUNTIME_CONTAINER") or "",
        )
    cwd = Path.cwd().resolve()
    if cwd.name == "property" and (cwd / "state" / "public_property_tours").exists():
        return (cwd / "state" / "public_property_tours").resolve()
    return Path("/data/public_property_tours").expanduser().resolve()


def _runtime_publish_required() -> bool:
    if _env_flag("PROPERTYQUARRY_RECONSTRUCTION_ALLOW_LOCAL_ONLY"):
        return False
    if _env_flag("PROPERTYQUARRY_RECONSTRUCTION_REQUIRE_RUNTIME_PUBLISH"):
        return True
    if str(os.getenv("EA_ROLE") or "").strip().lower() != "render-tools":
        return False
    try:
        configured_public_root = Path(str(os.getenv("EA_PUBLIC_TOUR_DIR") or "").strip() or "/data/public_property_tours").expanduser().resolve()
    except OSError:
        configured_public_root = Path("/data/public_property_tours")
    return configured_public_root != Path("/data/public_property_tours").resolve()


def _runtime_publish_requested() -> bool:
    if _env_flag("PROPERTYQUARRY_RECONSTRUCTION_ALLOW_LOCAL_ONLY"):
        return False
    if _env_flag("PROPERTYQUARRY_RECONSTRUCTION_PUBLISH_RUNTIME"):
        return True
    return _runtime_publish_required()


def _bundle_uses_shared_runtime_root(bundle_dir: Path) -> bool:
    try:
        bundle_root = bundle_dir.expanduser().resolve().parent
    except OSError:
        return False
    return bundle_root == Path("/data/public_property_tours").resolve()


def _runtime_publish_succeeded(receipt: dict[str, object]) -> bool:
    return str(receipt.get("status") or "").strip() in {
        "updated",
        "skipped_not_requested",
        "skipped_shared_public_root",
    }


def _runtime_publish_token(slug: str) -> str:
    del slug
    return secrets.token_hex(16)


_RUNTIME_PUBLISH_FINALIZE_SCRIPT = r"""
import ctypes
import fcntl
import hashlib
import json
import os
import pwd
import shutil
import stat
import sys
import tarfile
import tempfile
import time

stage, live, backup = sys.argv[1:4]
phase = "validation"
promoted = False
stage_created = False

def fsync_directory(path):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        directory_stat = os.fstat(descriptor)
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise RuntimeError("fsync_directory_invalid")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

try:
    owner_ref = sys.argv[4]
    if owner_ref.isdecimal():
        target_uid = int(owner_ref)
        target_gid = os.getegid()
    else:
        owner = pwd.getpwnam(owner_ref)
        target_uid = owner.pw_uid
        target_gid = owner.pw_gid
    deadline = time.monotonic() + float(sys.argv[5])
    publish_token = sys.argv[6]
    expected_entries = int(sys.argv[7])
    expected_bytes = int(sys.argv[8])
    expected_archive_bytes = int(sys.argv[9])
    expected_archive_sha256 = sys.argv[10]
    if target_uid == 0 or os.geteuid() != target_uid or os.getegid() != target_gid:
        raise RuntimeError("unprivileged_runtime_owner_required")
    if len(publish_token) != 32 or any(character not in "0123456789abcdef" for character in publish_token):
        raise RuntimeError("publish_token_invalid")
    if len(expected_archive_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in expected_archive_sha256
    ):
        raise RuntimeError("archive_digest_invalid")
    hard_max_entries = 100000
    hard_max_logical_bytes = 8 * 1024**3
    hard_max_file_bytes = 2 * 1024**3
    hard_max_manifest_bytes = 4 * 1024**2
    hard_max_archive_bytes = 9 * 1024**3
    hard_max_path_bytes = 1024
    hard_max_path_depth = 32
    if (
        expected_entries < 1
        or expected_entries > hard_max_entries
        or expected_bytes < 1
        or expected_bytes > hard_max_logical_bytes
        or expected_archive_bytes < 1
        or expected_archive_bytes > hard_max_archive_bytes
    ):
        raise RuntimeError("archive_limits_invalid")
    public_root = os.path.dirname(live)
    staging_root = os.path.join(public_root, ".publish..staging")
    if (
        os.path.dirname(stage) != staging_root
        or os.path.dirname(backup) != staging_root
        or backup != f"{stage}.previous"
    ):
        raise RuntimeError("staging_path_invalid")
    public_stat = os.lstat(public_root)
    if (
        stat.S_ISLNK(public_stat.st_mode)
        or not stat.S_ISDIR(public_stat.st_mode)
        or public_stat.st_uid != target_uid
        or public_stat.st_gid != target_gid
    ):
        raise RuntimeError("public_root_invalid")
    if os.path.lexists(staging_root):
        staging_stat = os.lstat(staging_root)
        if (
            stat.S_ISLNK(staging_stat.st_mode)
            or not stat.S_ISDIR(staging_stat.st_mode)
            or staging_stat.st_uid != target_uid
            or staging_stat.st_gid != target_gid
            or stat.S_IMODE(staging_stat.st_mode) != 0o700
        ):
            raise RuntimeError("staging_root_invalid")
    else:
        os.mkdir(staging_root, mode=0o700)
    lock_path = os.path.join(staging_root, f".{os.path.basename(live)}.lock")
    lock_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    lock_descriptor = os.open(lock_path, lock_flags, 0o600)
    os.fchmod(lock_descriptor, 0o600)
    lock_stat = os.fstat(lock_descriptor)
    if (
        not stat.S_ISREG(lock_stat.st_mode)
        or lock_stat.st_uid != target_uid
        or lock_stat.st_gid != target_gid
        or stat.S_IMODE(lock_stat.st_mode) != 0o600
    ):
        os.close(lock_descriptor)
        raise RuntimeError("publish_lock_invalid")
    while True:
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise TimeoutError("publish_lock_deadline_exceeded")
            time.sleep(0.02)

    if os.path.lexists(stage):
        raise RuntimeError("staging_collision")
    if os.path.lexists(backup):
        raise RuntimeError("publish_backup_collision")
    os.mkdir(stage, mode=0o700)
    os.chmod(stage, 0o700, follow_symlinks=False)
    stage_created = True

    phase = "transport"
    archive_spool = tempfile.TemporaryFile(mode="w+b")
    archive_digest = hashlib.sha256()
    archive_remaining = expected_archive_bytes
    while archive_remaining:
        if time.monotonic() >= deadline:
            raise TimeoutError("finalize_deadline_exceeded")
        chunk = sys.stdin.buffer.read(min(1024 * 1024, archive_remaining))
        if not chunk:
            raise RuntimeError("archive_transport_truncated")
        archive_spool.write(chunk)
        archive_digest.update(chunk)
        archive_remaining -= len(chunk)
    if sys.stdin.buffer.read(1):
        raise RuntimeError("archive_transport_trailing_data")
    if archive_digest.hexdigest() != expected_archive_sha256:
        raise RuntimeError("archive_digest_mismatch")
    archive_spool.flush()
    os.fsync(archive_spool.fileno())
    archive_spool.seek(0)

    phase = "extraction"
    observed_entries = 0
    observed_bytes = 0
    observed_names = set()
    observed_types = {}
    required_manifest = False
    archive = tarfile.open(fileobj=archive_spool, mode="r:")
    for member in archive:
        if time.monotonic() >= deadline:
            raise TimeoutError("finalize_deadline_exceeded")
        name = str(member.name or "")
        parts = name.split("/")
        encoded_name = name.encode("utf-8", "strict")
        if (
            not name
            or name.startswith("/")
            or any(part in {"", ".", ".."} for part in parts)
            or any(ord(character) < 32 or ord(character) == 127 for character in name)
            or len(encoded_name) > hard_max_path_bytes
            or len(parts) > hard_max_path_depth
            or name in observed_names
            or any(part in {".propertyquarry-publish-token", ".publish..staging"} for part in parts)
        ):
            raise RuntimeError("archive_member_path_invalid")
        if member.sparse is not None or member.type not in {tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE}:
            raise RuntimeError("archive_member_type_invalid")
        is_directory = member.type == tarfile.DIRTYPE
        if any(
            observed_types.get("/".join(parts[:index])) == "file"
            for index in range(1, len(parts))
        ) or (
            not is_directory
            and any(observed_name.startswith(f"{name}/") for observed_name in observed_names)
        ):
            raise RuntimeError("archive_member_prefix_conflict")
        observed_names.add(name)
        observed_types[name] = "directory" if is_directory else "file"
        observed_entries += 1
        observed_bytes += int(member.size or 0)
        if (
            int(member.size or 0) < 0
            or int(member.size or 0) > hard_max_file_bytes
            or observed_entries > expected_entries
            or observed_bytes > expected_bytes
        ):
            raise RuntimeError("archive_limits_exceeded")
        target = os.path.join(stage, *parts)
        parent = stage
        for part in parts[:-1]:
            parent = os.path.join(parent, part)
            if os.path.lexists(parent):
                parent_stat = os.lstat(parent)
                if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
                    raise RuntimeError("archive_parent_invalid")
            else:
                os.mkdir(parent, mode=0o700)
        if is_directory:
            if os.path.lexists(target):
                target_stat = os.lstat(target)
                if stat.S_ISLNK(target_stat.st_mode) or not stat.S_ISDIR(target_stat.st_mode):
                    raise RuntimeError("archive_directory_invalid")
            else:
                os.mkdir(target, mode=0o700)
            os.chmod(target, 0o700, follow_symlinks=False)
            continue
        if os.path.lexists(target):
            raise RuntimeError("archive_file_collision")
        source = archive.extractfile(member)
        if source is None:
            raise RuntimeError("archive_file_missing")
        file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target, file_flags, 0o600)
        remaining = int(member.size)
        try:
            while remaining:
                if time.monotonic() >= deadline:
                    raise TimeoutError("finalize_deadline_exceeded")
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise RuntimeError("archive_file_truncated")
                pending = memoryview(chunk)
                while pending:
                    written = os.write(descriptor, pending)
                    if written <= 0:
                        raise RuntimeError("archive_file_write_failed")
                    pending = pending[written:]
                remaining -= len(chunk)
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
            source.close()
        os.chmod(target, 0o600, follow_symlinks=False)
        required_manifest = required_manifest or name == "tour.json"
    archive.close()
    archive_end = int(archive.offset)
    archive_spool.seek(archive_end)
    while True:
        padding = archive_spool.read(1024 * 1024)
        if not padding:
            break
        if any(padding):
            raise RuntimeError("archive_nonzero_trailing_data")
    archive_spool.close()
    if observed_entries != expected_entries or observed_bytes != expected_bytes:
        raise RuntimeError("archive_totals_mismatch")
    if not required_manifest:
        raise RuntimeError("public_manifest_missing")
    manifest_path = os.path.join(stage, "tour.json")
    manifest_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    manifest_descriptor = os.open(manifest_path, manifest_flags)
    try:
        manifest_stat = os.fstat(manifest_descriptor)
        if not stat.S_ISREG(manifest_stat.st_mode) or manifest_stat.st_size > hard_max_manifest_bytes:
            raise RuntimeError("public_manifest_invalid")
        manifest_payload = json.loads(
            os.read(manifest_descriptor, int(manifest_stat.st_size) + 1).decode("utf-8", "strict")
        )
    finally:
        os.close(manifest_descriptor)
    if not isinstance(manifest_payload, dict) or str(manifest_payload.get("slug") or "") != os.path.basename(live):
        raise RuntimeError("public_manifest_slug_mismatch")

    phase = "verification"
    stage_stat = os.lstat(stage)
    if (
        stat.S_ISLNK(stage_stat.st_mode)
        or not stat.S_ISDIR(stage_stat.st_mode)
        or stage_stat.st_uid != target_uid
        or stage_stat.st_gid != target_gid
        or stat.S_IMODE(stage_stat.st_mode) != 0o700
    ):
        raise RuntimeError("runtime_stage_mismatch")
    for current, directories, filenames in os.walk(stage, topdown=True, followlinks=False):
        if time.monotonic() >= deadline:
            raise TimeoutError("finalize_deadline_exceeded")
        for name, is_directory in [(name, True) for name in directories] + [(name, False) for name in filenames]:
            path = os.path.join(current, name)
            file_stat = os.lstat(path)
            expected_mode = 0o700 if is_directory else 0o600
            expected_type = stat.S_ISDIR if is_directory else stat.S_ISREG
            if stat.S_ISLNK(file_stat.st_mode) or not expected_type(file_stat.st_mode):
                raise RuntimeError("runtime_type_mismatch")
            if file_stat.st_uid != target_uid or file_stat.st_gid != target_gid:
                raise RuntimeError("runtime_owner_mismatch")
            if stat.S_IMODE(file_stat.st_mode) != expected_mode:
                raise RuntimeError("runtime_mode_mismatch")
        for name in filenames:
            path = os.path.join(current, name)
            with open(path, "rb") as handle:
                handle.read(1)

    marker = os.path.join(stage, ".propertyquarry-publish-token")
    with open(marker, "x", encoding="ascii") as handle:
        handle.write(publish_token + "\n")
        handle.flush()
        os.fchmod(handle.fileno(), 0o600)
        os.fsync(handle.fileno())
    for current, _directories, _filenames in os.walk(stage, topdown=False, followlinks=False):
        fsync_directory(current)
    fsync_directory(staging_root)

    phase = "promotion"
    if time.monotonic() >= deadline:
        raise TimeoutError("finalize_deadline_exceeded")
    if os.path.lexists(live):
        if os.path.islink(live) or not os.path.isdir(live):
            raise RuntimeError("live_bundle_invalid")
        libc = ctypes.CDLL(None, use_errno=True)
        rename_exchange = getattr(libc, "renameat2", None)
        if rename_exchange is None:
            raise RuntimeError("atomic_exchange_unavailable")
        rename_exchange.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename_exchange.restype = ctypes.c_int
        result = rename_exchange(-100, os.fsencode(stage), -100, os.fsencode(live), 2)
        if result != 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number), live)
        promoted = True
    else:
        os.rename(stage, live)
        promoted = True
    fsync_directory(public_root)
    fsync_directory(staging_root)
    print("state=committed", flush=True)
except Exception as exc:
    if not promoted and stage_created and os.path.lexists(stage):
        cleanup_stat = os.lstat(stage)
        if stat.S_ISDIR(cleanup_stat.st_mode) and not stat.S_ISLNK(cleanup_stat.st_mode):
            shutil.rmtree(stage, ignore_errors=True)
    print(f"phase={phase} error={type(exc).__name__}:{exc}", file=sys.stderr)
    raise SystemExit(1)
""".strip()


_RUNTIME_PUBLISH_RECOVER_SCRIPT = r"""
import fcntl
import os
import pwd
import shutil
import stat
import sys
import time

public_root, stage, live, backup, publish_token = sys.argv[1:6]
deadline = time.monotonic() + float(sys.argv[6])
owner_ref = sys.argv[7]
if owner_ref.isdecimal():
    target_uid = int(owner_ref)
    target_gid = os.getegid()
else:
    owner = pwd.getpwnam(owner_ref)
    target_uid = owner.pw_uid
    target_gid = owner.pw_gid
staging_root = os.path.dirname(stage)

def marker_for(bundle):
    if not os.path.lexists(bundle):
        return "absent", ""
    bundle_stat = os.lstat(bundle)
    if stat.S_ISLNK(bundle_stat.st_mode) or not stat.S_ISDIR(bundle_stat.st_mode):
        return "invalid", ""
    marker = os.path.join(bundle, ".propertyquarry-publish-token")
    if not os.path.lexists(marker):
        return "absent", ""
    marker_stat = os.lstat(marker)
    if (
        stat.S_ISLNK(marker_stat.st_mode)
        or not stat.S_ISREG(marker_stat.st_mode)
        or marker_stat.st_uid != target_uid
        or marker_stat.st_gid != target_gid
        or stat.S_IMODE(marker_stat.st_mode) != 0o600
    ):
        return "invalid", ""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(marker, flags)
    try:
        value = os.read(descriptor, 64).decode("ascii", "strict").strip()
    finally:
        os.close(descriptor)
    if len(value) != 32 or any(character not in "0123456789abcdef" for character in value):
        return "invalid", ""
    return "valid", value

def remove_staged_tree(path):
    if not os.path.lexists(path):
        return
    if os.path.dirname(path) != staging_root:
        raise RuntimeError("cleanup_path_invalid")
    path_stat = os.lstat(path)
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
        raise RuntimeError("cleanup_target_invalid")
    shutil.rmtree(path)

try:
    if target_uid == 0 or os.geteuid() != target_uid or os.getegid() != target_gid:
        raise RuntimeError("unprivileged_runtime_owner_required")
    if (
        os.path.dirname(staging_root) != public_root
        or os.path.basename(staging_root) != ".publish..staging"
        or os.path.dirname(live) != public_root
        or os.path.dirname(backup) != staging_root
        or backup != f"{stage}.previous"
    ):
        raise RuntimeError("recovery_path_invalid")
    public_stat = os.lstat(public_root)
    if (
        stat.S_ISLNK(public_stat.st_mode)
        or not stat.S_ISDIR(public_stat.st_mode)
        or public_stat.st_uid != target_uid
        or public_stat.st_gid != target_gid
    ):
        raise RuntimeError("public_root_invalid")
    if not os.path.lexists(staging_root):
        marker_state, marker_value = marker_for(live)
        if marker_state == "invalid":
            raise RuntimeError("live_publish_marker_invalid")
        state = "committed" if marker_value == publish_token else "superseded" if marker_state == "valid" else "not_committed"
        print(f"state={state}", flush=True)
        raise SystemExit(0)
    staging_stat = os.lstat(staging_root)
    if (
        stat.S_ISLNK(staging_stat.st_mode)
        or not stat.S_ISDIR(staging_stat.st_mode)
        or staging_stat.st_uid != target_uid
        or staging_stat.st_gid != target_gid
        or stat.S_IMODE(staging_stat.st_mode) != 0o700
    ):
        raise RuntimeError("staging_root_invalid")
    lock_path = os.path.join(staging_root, f".{os.path.basename(live)}.lock")
    lock_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    lock_descriptor = os.open(lock_path, lock_flags, 0o600)
    os.fchmod(lock_descriptor, 0o600)
    lock_stat = os.fstat(lock_descriptor)
    if (
        not stat.S_ISREG(lock_stat.st_mode)
        or lock_stat.st_uid != target_uid
        or lock_stat.st_gid != target_gid
        or stat.S_IMODE(lock_stat.st_mode) != 0o600
    ):
        os.close(lock_descriptor)
        raise RuntimeError("publish_lock_invalid")
    while True:
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise TimeoutError("recovery_lock_deadline_exceeded")
            time.sleep(0.02)

    marker_state, marker_value = marker_for(live)
    if marker_state == "invalid":
        raise RuntimeError("live_publish_marker_invalid")
    if marker_value == publish_token:
        print("state=committed", flush=True)
        remove_staged_tree(stage)
        remove_staged_tree(backup)
    elif marker_state == "valid":
        print("state=superseded", flush=True)
        remove_staged_tree(stage)
        remove_staged_tree(backup)
    else:
        if not os.path.lexists(live) and os.path.lexists(backup):
            backup_stat = os.lstat(backup)
            if stat.S_ISLNK(backup_stat.st_mode) or not stat.S_ISDIR(backup_stat.st_mode):
                raise RuntimeError("publish_backup_invalid")
            os.rename(backup, live)
        print("state=not_committed", flush=True)
        remove_staged_tree(stage)
        if os.path.lexists(live):
            remove_staged_tree(backup)
except SystemExit:
    raise
except Exception as exc:
    print(f"state=ambiguous error={type(exc).__name__}:{exc}", file=sys.stderr, flush=True)
    raise SystemExit(1)
""".strip()


def _write_runtime_publish_archive(bundle_dir: Path, archive_file: io.BufferedRandom) -> tuple[int, int, int, str]:
    hard_max_entries = 100_000
    hard_max_bytes = 8 * 1024**3
    hard_max_file_bytes = 2 * 1024**3
    hard_max_archive_bytes = 9 * 1024**3
    hard_max_path_bytes = 1024
    hard_max_path_depth = 32
    try:
        max_entries = min(
            hard_max_entries,
            max(
                1,
                int(str(os.getenv("PROPERTYQUARRY_RECONSTRUCTION_RUNTIME_MAX_ARCHIVE_ENTRIES") or "100000")),
            ),
        )
    except Exception:
        max_entries = hard_max_entries
    try:
        max_bytes = min(
            hard_max_bytes,
            max(
                1,
                int(str(os.getenv("PROPERTYQUARRY_RECONSTRUCTION_RUNTIME_MAX_LOGICAL_BYTES") or str(8 * 1024**3))),
            ),
        )
    except Exception:
        max_bytes = hard_max_bytes

    directories: list[tuple[Path, str, os.stat_result]] = []
    files: list[tuple[Path, str, os.stat_result]] = []
    total_bytes = 0
    for current, directory_names, file_names in os.walk(bundle_dir, topdown=True, followlinks=False):
        current_path = Path(current)
        directory_names.sort()
        file_names.sort()
        for name in directory_names:
            path = current_path / name
            file_stat = path.lstat()
            if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISDIR(file_stat.st_mode):
                raise ValueError(f"runtime archive directory is unsafe: {path}")
            relpath = path.relative_to(bundle_dir).as_posix()
            relpath_parts = PurePosixPath(relpath).parts
            if (
                any(part in {".propertyquarry-publish-token", ".publish..staging"} for part in relpath_parts)
                or len(relpath.encode("utf-8")) > hard_max_path_bytes
                or len(relpath_parts) > hard_max_path_depth
                or any(ord(character) < 32 or ord(character) == 127 for character in relpath)
            ):
                raise ValueError("runtime archive contains reserved publish marker")
            directories.append((path, relpath, file_stat))
        for name in file_names:
            path = current_path / name
            file_stat = path.lstat()
            if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
                raise ValueError(f"runtime archive file is unsafe: {path}")
            relpath = path.relative_to(bundle_dir).as_posix()
            relpath_parts = PurePosixPath(relpath).parts
            if (
                any(part in {".propertyquarry-publish-token", ".publish..staging"} for part in relpath_parts)
                or len(relpath.encode("utf-8")) > hard_max_path_bytes
                or len(relpath_parts) > hard_max_path_depth
                or any(ord(character) < 32 or ord(character) == 127 for character in relpath)
            ):
                raise ValueError("runtime archive contains reserved publish marker")
            if int(file_stat.st_size) > hard_max_file_bytes:
                raise ValueError("runtime archive file exceeds hard publication limit")
            total_bytes += int(file_stat.st_size)
            files.append((path, relpath, file_stat))
    entry_count = len(directories) + len(files)
    if entry_count > max_entries or total_bytes > max_bytes:
        raise ValueError("runtime archive exceeds configured publication limits")
    if not any(relpath == "tour.json" for _path, relpath, _file_stat in files):
        raise ValueError("runtime archive is missing tour.json")

    with tarfile.open(fileobj=archive_file, mode="w") as archive:
        for _path, relpath, _file_stat in sorted(directories, key=lambda item: (item[1].count("/"), item[1])):
            member = tarfile.TarInfo(relpath)
            member.type = tarfile.DIRTYPE
            member.mode = 0o700
            member.mtime = 0
            archive.addfile(member)
        for path, relpath, expected_stat in files:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            try:
                actual_stat = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(actual_stat.st_mode)
                    or actual_stat.st_dev != expected_stat.st_dev
                    or actual_stat.st_ino != expected_stat.st_ino
                    or actual_stat.st_size != expected_stat.st_size
                ):
                    raise ValueError(f"runtime archive file changed while publishing: {path}")
                member = tarfile.TarInfo(relpath)
                member.size = int(actual_stat.st_size)
                member.mode = 0o600
                member.mtime = 0
                with os.fdopen(descriptor, "rb", closefd=False) as source:
                    archive.addfile(member, source)
            finally:
                os.close(descriptor)
    archive_file.flush()
    archive_file.seek(0, os.SEEK_END)
    archive_bytes = archive_file.tell()
    if archive_bytes > hard_max_archive_bytes:
        raise ValueError("runtime archive exceeds hard transport limit")
    archive_file.seek(0)
    archive_digest = hashlib.sha256()
    while True:
        chunk = archive_file.read(1024 * 1024)
        if not chunk:
            break
        archive_digest.update(chunk)
    archive_file.seek(0)
    return entry_count, total_bytes, archive_bytes, archive_digest.hexdigest()


def _recover_runtime_publish(
    *,
    docker_bin: str,
    container: str,
    public_root: str,
    remote_staging: str,
    remote_bundle: str,
    remote_backup: str,
    publish_token: str,
    timeout_seconds: float,
) -> dict[str, str]:
    command = [
        docker_bin,
        "exec",
        "--user",
        "ea",
        container,
        "python",
        "-c",
        _RUNTIME_PUBLISH_RECOVER_SCRIPT,
        public_root,
        remote_staging,
        remote_bundle,
        remote_backup,
        publish_token,
        str(max(1.0, timeout_seconds - 0.5)),
        "ea",
    ]

    def _text(value: object) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        return str(value or "")

    def _diagnostic(value: object) -> dict[str, object]:
        payload = _text(value).encode("utf-8", errors="replace")
        return {
            "diagnostic_sha256": hashlib.sha256(payload).hexdigest(),
            "diagnostic_size_bytes": len(payload),
        }

    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        output = _text(exc.stdout or exc.output)
        state_match = re.search(r"state=(committed|not_committed|superseded)", output)
        return {
            "state": str(state_match.group(1) if state_match else "ambiguous"),
            "cleanup": "timeout",
        }
    except Exception as exc:
        return {
            "state": "ambiguous",
            "cleanup": "failed",
            "error_class": type(exc).__name__,
        }
    output = _text(result.stdout)
    state_match = re.search(r"state=(committed|not_committed|superseded)", output)
    state = str(state_match.group(1) if state_match else "ambiguous")
    return {
        "state": state,
        "cleanup": "complete" if result.returncode == 0 else "failed",
        **(_diagnostic(result.stderr) if result.returncode != 0 else {}),
    }


def _sync_bundle_to_runtime_container(bundle_dir: Path, *, slug: str) -> dict[str, object]:
    docker_bin = shutil.which("docker")
    if not docker_bin:
        return {"status": "docker_unavailable", "slug": slug}
    container = str(os.getenv("PROPERTYQUARRY_RUNTIME_CONTAINER") or "propertyquarry-api").strip()
    if not container:
        return {"status": "runtime_container_missing", "slug": slug}
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", container):
        return {"status": "runtime_container_invalid", "slug": slug}
    normalized_slug = str(slug or "").strip()
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}", normalized_slug)
        or ".." in normalized_slug
    ):
        return {"status": "runtime_slug_invalid"}
    normalized_bundle = bundle_dir.expanduser().resolve()
    if not normalized_bundle.is_dir():
        return {"status": "bundle_missing", "slug": normalized_slug}
    remote_root = "/data/public_property_tours"
    remote_bundle = f"{remote_root}/{normalized_slug}"
    publish_token = _runtime_publish_token(normalized_slug)
    remote_staging = f"{remote_root}/.publish..staging/{normalized_slug}-{publish_token}"
    remote_backup = f"{remote_staging}.previous"
    try:
        copy_timeout_seconds = max(
            5.0,
            float(str(os.getenv("PROPERTYQUARRY_RECONSTRUCTION_RUNTIME_COPY_TIMEOUT_SECONDS") or "30").strip() or "30"),
        )
    except Exception:
        copy_timeout_seconds = 30.0
    try:
        permission_timeout_seconds = max(
            2.0,
            float(
                str(os.getenv("PROPERTYQUARRY_RECONSTRUCTION_RUNTIME_PERMISSION_TIMEOUT_SECONDS") or "8").strip()
                or "8"
            ),
        )
    except Exception:
        permission_timeout_seconds = 8.0
    try:
        finalize_grace_seconds = max(
            5.0,
            float(
                str(os.getenv("PROPERTYQUARRY_RECONSTRUCTION_RUNTIME_FINALIZE_GRACE_SECONDS") or "15").strip()
                or "15"
            ),
        )
    except Exception:
        finalize_grace_seconds = 15.0

    def _recovery() -> dict[str, str]:
        return _recover_runtime_publish(
            docker_bin=docker_bin,
            container=container,
            public_root=remote_root,
            remote_staging=remote_staging,
            remote_bundle=remote_bundle,
            remote_backup=remote_backup,
            publish_token=publish_token,
            timeout_seconds=finalize_grace_seconds,
        )

    def _failure(status: str, **details: object) -> dict[str, object]:
        return {
            "status": status,
            "slug": normalized_slug,
            "container": container,
            "recovery": _recovery(),
            **details,
        }

    finalize_deadline_seconds = copy_timeout_seconds + permission_timeout_seconds
    finalize_outer_timeout_seconds = finalize_deadline_seconds + finalize_grace_seconds
    try:
        with tempfile.TemporaryFile(mode="w+b") as archive_file:
            try:
                archive_entries, logical_bytes, archive_bytes, archive_sha256 = _write_runtime_publish_archive(
                    normalized_bundle,
                    archive_file,
                )
            except Exception as exc:
                return {
                    "status": "runtime_archive_failed",
                    "slug": normalized_slug,
                    "container": container,
                    "error_class": type(exc).__name__,
                }
            try:
                finalize_result = subprocess.run(
                    [
                        docker_bin,
                        "exec",
                        "-i",
                        "--user",
                        "ea",
                        container,
                        "python",
                        "-c",
                        _RUNTIME_PUBLISH_FINALIZE_SCRIPT,
                        remote_staging,
                        remote_bundle,
                        remote_backup,
                        "ea",
                        str(finalize_deadline_seconds),
                        publish_token,
                        str(archive_entries),
                        str(logical_bytes),
                        str(archive_bytes),
                        archive_sha256,
                    ],
                    check=False,
                    stdin=archive_file,
                    capture_output=True,
                    text=True,
                    timeout=finalize_outer_timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                recovery = _recovery()
                if recovery.get("state") == "committed":
                    return {
                        "status": "updated",
                        "slug": normalized_slug,
                        "container": container,
                        "recovered_after_finalize_timeout": True,
                        "staging_cleanup": recovery.get("cleanup", ""),
                    }
                return {
                    "status": "runtime_finalize_timeout",
                    "slug": normalized_slug,
                    "container": container,
                    "timeout_seconds": exc.timeout,
                    "recovery": recovery,
                }
    except OSError as exc:
        return _failure(
            "runtime_publish_io_failed",
            error_class=type(exc).__name__,
        )
    if finalize_result.returncode != 0:
        stderr = (finalize_result.stderr or "").strip()[-400:]
        phase_match = re.search(r"phase=([a-z_]+)", stderr)
        phase = str(phase_match.group(1) if phase_match else "finalize")
        status_by_phase = {
            "extraction": "runtime_copy_failed",
            "promotion": "runtime_promotion_failed",
            "validation": "runtime_verification_failed",
            "verification": "runtime_verification_failed",
        }
        recovery = _recovery()
        if recovery.get("state") == "committed":
            return {
                "status": "updated",
                "slug": normalized_slug,
                "container": container,
                "recovered_after_finalize_failure": True,
                "staging_cleanup": recovery.get("cleanup", ""),
            }
        return {
            "status": status_by_phase.get(phase, "runtime_finalize_failed"),
            "slug": normalized_slug,
            "container": container,
            "diagnostic_sha256": hashlib.sha256(
                stderr.encode("utf-8", errors="replace")
            ).hexdigest(),
            "diagnostic_size_bytes": len(
                stderr.encode("utf-8", errors="replace")
            ),
            "recovery": recovery,
        }
    recovery = _recovery()
    if recovery.get("state") != "committed":
        return {
            "status": "runtime_recovery_failed",
            "slug": normalized_slug,
            "container": container,
            "recovery": recovery,
        }
    return {
        "status": "updated",
        "slug": normalized_slug,
        "container": container,
        **({"staging_cleanup": recovery.get("cleanup", "")} if recovery.get("cleanup") != "complete" else {}),
    }


def _safe_relpath(value: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/").lstrip("/")
    parts = [part for part in normalized.split("/") if part and part not in {".", ".."}]
    return "/".join(parts)


def _validated_tour_slug(value: object) -> str:
    normalized = str(value or "").strip()
    if (
        not _PUBLIC_TOUR_SLUG_PATTERN.fullmatch(normalized)
        or ".." in normalized
        or normalized.startswith(_PUBLIC_BUNDLE_STAGE_PREFIX)
    ):
        raise SystemExit("invalid_tour_slug")
    try:
        require_dynamic_tour_slug(normalized)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    return normalized


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _web_safe_image_suffix(source: Path) -> str:
    suffix = source.suffix.lower()
    if suffix in {".tif", ".tiff"}:
        return ".jpg"
    return suffix or ".jpg"


def _image_file_metadata_matches(
    expected: os.stat_result,
    observed: os.stat_result,
) -> bool:
    return bool(
        stat.S_ISREG(expected.st_mode)
        and stat.S_ISREG(observed.st_mode)
        and expected.st_dev == observed.st_dev
        and expected.st_ino == observed.st_ino
        and expected.st_size == observed.st_size
        and expected.st_mtime_ns == observed.st_mtime_ns
        and expected.st_ctime_ns == observed.st_ctime_ns
    )


def _validate_source_image_dimensions(
    width: int,
    height: int,
    *,
    minimum_dimension: int | None = None,
) -> None:
    effective_minimum_dimension = (
        _MIN_SOURCE_IMAGE_DIMENSION
        if minimum_dimension is None
        else max(1, int(minimum_dimension))
    )
    smaller = min(width, height)
    larger = max(width, height)
    if (
        smaller < effective_minimum_dimension
        or larger > _MAX_SOURCE_IMAGE_DIMENSION
        or width * height > _MAX_SOURCE_IMAGE_PIXELS
        or larger / max(smaller, 1) > _MAX_SOURCE_IMAGE_ASPECT_RATIO
    ):
        raise _SourceImageInvalid("source_image_invalid")


def _validate_floorplan_derived_allocation(width: int, height: int) -> None:
    if (
        width <= 0
        or height <= 0
        or max(width, height) > _MAX_FLOORPLAN_DERIVED_DIMENSION
        or width * height > _MAX_FLOORPLAN_DERIVED_PIXELS
    ):
        raise _SourceImageInvalid("source_image_invalid")


@contextmanager
def _open_bounded_source_image(
    path: Path,
    *,
    minimum_dimension: int | None = None,
) -> Iterator[Image.Image]:
    descriptor = -1
    try:
        descriptor = _open_absolute_regular_file_no_symlinks(
            path,
            failure="source_image_invalid",
        )
        opened = os.fstat(descriptor)
        if (
            opened.st_size <= 0
            or opened.st_size > _MAX_SOURCE_IMAGE_COMPRESSED_BYTES
        ):
            raise _SourceImageInvalid("source_image_invalid")
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(handle) as image:
                    _validate_source_image_dimensions(
                        int(image.width),
                        int(image.height),
                        minimum_dimension=minimum_dimension,
                    )
                    yield image
                    if not _image_file_metadata_matches(opened, os.fstat(handle.fileno())):
                        raise _SourceImageInvalid("source_image_invalid")
    except _SourceImageInvalid:
        raise
    except _PublicBundleTransactionError as exc:
        raise _SourceImageInvalid("source_image_invalid") from exc
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        MemoryError,
        OSError,
        ValueError,
    ) as exc:
        raise _SourceImageInvalid("source_image_invalid") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _image_metadata(path: Path) -> dict[str, object]:
    with _open_bounded_source_image(path) as image:
        return {
            "width": int(image.width),
            "height": int(image.height),
            "mode": str(image.mode),
        }


def _copy_normalized_image(source: Path, target: Path) -> dict[str, object]:
    if source.suffix.lower() not in IMAGE_EXTENSIONS:
        raise SystemExit("unsupported_image_extension")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with _open_bounded_source_image(source) as image:
            normalized = ImageOps.exif_transpose(image).convert("RGB")
            try:
                normalized.save(
                    target,
                    format="JPEG"
                    if target.suffix.lower() in {".jpg", ".jpeg"}
                    else None,
                    quality=90,
                )
            finally:
                normalized.close()
        metadata = _image_metadata(target)
    except (_SourceImageInvalid, MemoryError, OSError, ValueError):
        target.unlink(missing_ok=True)
        raise SystemExit("source_image_invalid") from None
    return {
        "source_path": "<provided-image>",
        "source_origin": "provided_image",
        "relpath": target.name,
        "sha256": _sha256(target),
        "size_bytes": target.stat().st_size,
        **metadata,
    }


def _connected_components(mask: list[list[int]]) -> list[dict[str, object]]:
    rows = len(mask)
    cols = len(mask[0]) if rows else 0
    visited = [[False for _ in range(cols)] for _ in range(rows)]
    components: list[dict[str, object]] = []
    for row in range(rows):
        for col in range(cols):
            if not mask[row][col] or visited[row][col]:
                continue
            queue = [(col, row)]
            visited[row][col] = True
            area = 0
            min_col = max_col = col
            min_row = max_row = row
            touches_edge = col in {0, cols - 1} or row in {0, rows - 1}
            while queue:
                current_col, current_row = queue.pop()
                area += 1
                min_col = min(min_col, current_col)
                max_col = max(max_col, current_col)
                min_row = min(min_row, current_row)
                max_row = max(max_row, current_row)
                for next_col, next_row in (
                    (current_col + 1, current_row),
                    (current_col - 1, current_row),
                    (current_col, current_row + 1),
                    (current_col, current_row - 1),
                ):
                    if (
                        0 <= next_col < cols
                        and 0 <= next_row < rows
                        and mask[next_row][next_col]
                        and not visited[next_row][next_col]
                    ):
                        visited[next_row][next_col] = True
                        queue.append((next_col, next_row))
                        if next_col in {0, cols - 1} or next_row in {0, rows - 1}:
                            touches_edge = True
            components.append(
                {
                    "area": area,
                    "bbox": (min_col, min_row, max_col, max_row),
                    "touches_edge": touches_edge,
                }
            )
    return components


def _bbox_axis_gap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> tuple[int, int]:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    dx = max(0, max(ax0 - bx1, bx0 - ax1) - 1)
    dy = max(0, max(ay0 - by1, by0 - ay1) - 1)
    return dx, dy


def _floorplan_content_bbox(path: Path) -> tuple[int, int, int, int]:
    with _open_bounded_source_image(path) as floorplan_image:
        normalized = ImageOps.exif_transpose(floorplan_image).convert("L")
        width, height = normalized.size
        preview_width = min(240, max(120, width // 4))
        preview_height = max(120, int(round(height * preview_width / max(width, 1))))
        _validate_floorplan_derived_allocation(preview_width, preview_height)
        preview = ImageOps.autocontrast(
            normalized.resize((preview_width, preview_height), Image.Resampling.LANCZOS),
            cutoff=1,
        )
        preview_pixels = preview.load()
        binary = [
            [1 if preview_pixels[col, row] < 180 else 0 for col in range(preview_width)]
            for row in range(preview_height)
        ]
    components = [
        component
        for component in _connected_components(binary)
        if int(component.get("area") or 0) >= 20
    ]
    if not components:
        return (0, 0, width, height)
    components.sort(key=lambda component: int(component.get("area") or 0), reverse=True)
    main_bbox = tuple(components[0]["bbox"])
    main_area = int(components[0].get("area") or 0)
    kept = []
    for component in components:
        area = int(component.get("area") or 0)
        bbox = tuple(component.get("bbox") or main_bbox)
        gap_x, gap_y = _bbox_axis_gap(bbox, main_bbox)
        substantial = area >= max(20, int(round(main_area * 0.025)))
        aligned_with_plan = max(gap_x, gap_y) <= 6 and (gap_x == 0 or gap_y == 0)
        if bbox == main_bbox or (substantial and max(gap_x, gap_y) <= 12) or aligned_with_plan:
            kept.append(bbox)
    min_col = min(bbox[0] for bbox in kept)
    min_row = min(bbox[1] for bbox in kept)
    max_col = max(bbox[2] for bbox in kept)
    max_row = max(bbox[3] for bbox in kept)
    scale_x = width / preview_width
    scale_y = height / preview_height
    padding = 16
    left = max(0, int(min_col * scale_x) - padding)
    top = max(0, int(min_row * scale_y) - padding)
    right = min(width, int(round((max_col + 1) * scale_x)) + padding)
    bottom = min(height, int(round((max_row + 1) * scale_y)) + padding)
    if right - left < 40 or bottom - top < 40:
        return (0, 0, width, height)
    return (left, top, right, bottom)


def _extract_floorplan_geometry(
    path: Path,
    *,
    max_grid_width: int = 120,
) -> dict[str, object]:
    with _open_bounded_source_image(path) as floorplan_image:
        normalized = ImageOps.exif_transpose(floorplan_image).convert("L")
        bbox = _floorplan_content_bbox(path)
        cropped = normalized.crop(bbox)
    crop_width, crop_height = cropped.size
    grid_width = max(96, min(max_grid_width, int(round(crop_width / 7.0))))
    grid_height = max(72, int(round(crop_height * grid_width / max(crop_width, 1))))
    _validate_floorplan_derived_allocation(grid_width, grid_height)
    def _opened_geometry_mask(*, scale: int, threshold: int) -> Image.Image:
        _validate_floorplan_derived_allocation(
            grid_width * scale,
            grid_height * scale,
        )
        reduced = ImageOps.autocontrast(
            cropped.resize((grid_width * scale, grid_height * scale), Image.Resampling.LANCZOS),
            cutoff=1,
        )
        ink_mask = reduced.point(lambda value: 255 if value < threshold else 0)
        opened = ink_mask.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))
        return opened.resize((grid_width, grid_height), Image.Resampling.BOX)

    # The broad 3x lane keeps heavy light-grey walls; the dark 6x lane recovers
    # legitimate 6-8 px architectural strokes without retaining equally thin
    # coloured room annotations. Fine labels and hatching fail both openings.
    broad_structural_mask = _opened_geometry_mask(scale=3, threshold=140)
    thin_dark_structural_mask = _opened_geometry_mask(scale=6, threshold=50)
    broad_pixels = broad_structural_mask.load()
    thin_dark_pixels = thin_dark_structural_mask.load()
    filtered_mask = [
        [
            1 if max(broad_pixels[col, row], thin_dark_pixels[col, row]) >= 64 else 0
            for col in range(grid_width)
        ]
        for row in range(grid_height)
    ]
    for component in _connected_components(filtered_mask):
        area = int(component.get("area") or 0)
        min_col, min_row, max_col, max_row = tuple(component.get("bbox") or (0, 0, 0, 0))
        width_cells = max_col - min_col + 1
        height_cells = max_row - min_row + 1
        if area < 6 or (width_cells <= 2 and height_cells <= 2):
            for row in range(min_row, max_row + 1):
                for col in range(min_col, max_col + 1):
                    if filtered_mask[row][col]:
                        filtered_mask[row][col] = 0
    return {
        "content_bbox_px": {
            "left": int(bbox[0]),
            "top": int(bbox[1]),
            "right": int(bbox[2]),
            "bottom": int(bbox[3]),
        },
        "content_size_px": {"width": int(crop_width), "height": int(crop_height)},
        "mask_size_cells": {"width": int(grid_width), "height": int(grid_height)},
        "extraction_method": "autocontrast_geometry_mask_directional_segments_v1",
        "wall_mask": filtered_mask,
    }


def _room_dimensions(width: int, height: int, *, max_width_m: float) -> tuple[float, float, float]:
    ratio = height / width if width else 0.7
    room_width = float(max_width_m)
    room_depth = max(3.0, min(18.0, room_width * ratio))
    room_height = 2.75
    return round(room_width, 3), round(room_depth, 3), room_height


def _floorplan_texture_crop(
    geometry: dict[str, object],
    floorplan: dict[str, object],
) -> dict[str, float]:
    try:
        source_width = max(1, int(floorplan.get("width") or 1))
        source_height = max(1, int(floorplan.get("height") or 1))
        bbox = dict(geometry.get("content_bbox_px") or {})
        left = max(0, min(source_width - 1, int(bbox.get("left") or 0)))
        top = max(0, min(source_height - 1, int(bbox.get("top") or 0)))
        right = max(left + 1, min(source_width, int(bbox.get("right") or source_width)))
        bottom = max(top + 1, min(source_height, int(bbox.get("bottom") or source_height)))
    except Exception:
        return {"offset_x": 0.0, "offset_y": 0.0, "repeat_x": 1.0, "repeat_y": 1.0}
    return {
        "offset_x": round(left / source_width, 8),
        "offset_y": round(1.0 - (bottom / source_height), 8),
        "repeat_x": round((right - left) / source_width, 8),
        "repeat_y": round((bottom - top) / source_height, 8),
    }


def _write_inferred_floorplan(target: Path, *, photo_count: int) -> dict[str, object]:
    room_count = max(2, min(6, photo_count or 3))
    width, height = 1400, 940
    image = Image.new("RGB", (width, height), color=(248, 244, 235))
    from PIL import ImageDraw

    draw = ImageDraw.Draw(image)
    ink = (45, 39, 30)
    muted = (184, 138, 50)
    draw.rectangle((90, 90, width - 90, height - 90), outline=ink, width=14)
    if room_count <= 3:
        splits = [0.52]
    else:
        splits = [0.42, 0.68]
    for split in splits:
        x = int(90 + (width - 180) * split)
        draw.line((x, 90, x, height - 90), fill=ink, width=8)
    if room_count >= 4:
        y = int(90 + (height - 180) * 0.55)
        draw.line((90, y, int(width * 0.68), y), fill=ink, width=8)
    draw.arc((width - 260, height - 250, width - 90, height - 80), 180, 270, fill=muted, width=6)
    draw.text((120, 120), "Inferred schematic from source photos", fill=muted)
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, format="JPEG", quality=90)
    metadata = _image_metadata(target)
    return {
        "source_path": "generated_from_photo_set",
        "relpath": target.name,
        "sha256": _sha256(target),
        "size_bytes": target.stat().st_size,
        "inferred": True,
        "inference_method": "room_count_heuristic_from_photo_count",
        **metadata,
    }


def _axis_aligned_wall_rectangles_from_mask(
    wall_mask: list[list[int]],
    *,
    width_m: float,
    depth_m: float,
) -> list[dict[str, float]]:
    rows = len(wall_mask)
    cols = len(wall_mask[0]) if rows else 0
    if not rows or not cols:
        return []
    active: dict[tuple[int, int], dict[str, int]] = {}
    merged: list[dict[str, int]] = []
    for row_index, row in enumerate(wall_mask):
        next_active: dict[tuple[int, int], dict[str, int]] = {}
        run_start: int | None = None
        for col_index in range(cols + 1):
            filled = col_index < cols and bool(row[col_index])
            if filled and run_start is None:
                run_start = col_index
            elif not filled and run_start is not None:
                key = (run_start, col_index - 1)
                current = active.get(key)
                if current is None:
                    current = {
                        "x0": run_start,
                        "x1": col_index - 1,
                        "y0": row_index,
                        "y1": row_index,
                    }
                else:
                    current["y1"] = row_index
                next_active[key] = current
                run_start = None
        for key, rectangle in active.items():
            if key not in next_active:
                merged.append(rectangle)
        active = next_active
    merged.extend(active.values())
    cell_width = width_m / cols
    cell_depth = depth_m / rows
    half_width = width_m / 2
    half_depth = depth_m / 2
    rectangles: list[dict[str, float]] = []
    for rectangle in merged:
        span_cols = rectangle["x1"] - rectangle["x0"] + 1
        span_rows = rectangle["y1"] - rectangle["y0"] + 1
        if span_cols <= 1 and span_rows <= 1:
            continue
        rect_width = round(span_cols * cell_width, 4)
        rect_depth = round(span_rows * cell_depth, 4)
        center_x = round(-half_width + (rectangle["x0"] + span_cols / 2) * cell_width, 4)
        center_z = round(-half_depth + (rectangle["y0"] + span_rows / 2) * cell_depth, 4)
        rectangles.append(
            {
                "center_x": center_x,
                "center_z": center_z,
                "width": rect_width,
                "depth": rect_depth,
            }
        )
    return rectangles


def _directional_run_mask(
    wall_mask: list[list[int]],
    *,
    axis: str,
    minimum_run_cells: int = 5,
) -> list[list[int]]:
    rows = len(wall_mask)
    cols = len(wall_mask[0]) if rows else 0
    directional = [[0 for _ in range(cols)] for _ in range(rows)]
    minimum_run = max(2, int(minimum_run_cells))
    if axis == "horizontal":
        for row_index, row in enumerate(wall_mask):
            run_start: int | None = None
            for col_index in range(cols + 1):
                filled = col_index < cols and bool(row[col_index])
                if filled and run_start is None:
                    run_start = col_index
                elif not filled and run_start is not None:
                    if col_index - run_start >= minimum_run:
                        for run_col in range(run_start, col_index):
                            directional[row_index][run_col] = 1
                    run_start = None
        return directional
    if axis != "vertical":
        raise ValueError("invalid_directional_run_axis")
    for col_index in range(cols):
        run_start = None
        for row_index in range(rows + 1):
            filled = row_index < rows and bool(wall_mask[row_index][col_index])
            if filled and run_start is None:
                run_start = row_index
            elif not filled and run_start is not None:
                if row_index - run_start >= minimum_run:
                    for run_row in range(run_start, row_index):
                        directional[run_row][col_index] = 1
                run_start = None
    return directional


def _oriented_wall_segment_from_component(
    directional_mask: list[list[int]],
    component: dict[str, object],
    *,
    width_m: float,
    depth_m: float,
) -> dict[str, object] | None:
    rows = len(directional_mask)
    cols = len(directional_mask[0]) if rows else 0
    if not rows or not cols:
        return None
    min_col, min_row, max_col, max_row = tuple(component.get("bbox") or (0, 0, 0, 0))
    cell_width = width_m / cols
    cell_depth = depth_m / rows
    half_width = width_m / 2
    half_depth = depth_m / 2
    points = [
        (
            -half_width + ((col + 0.5) * cell_width),
            -half_depth + ((row + 0.5) * cell_depth),
        )
        for row in range(min_row, max_row + 1)
        for col in range(min_col, max_col + 1)
        if directional_mask[row][col]
    ]
    if len(points) < 5:
        return None
    mean_x = sum(point[0] for point in points) / len(points)
    mean_z = sum(point[1] for point in points) / len(points)
    covariance_xx = sum((point[0] - mean_x) ** 2 for point in points) / len(points)
    covariance_xz = sum((point[0] - mean_x) * (point[1] - mean_z) for point in points) / len(points)
    covariance_zz = sum((point[1] - mean_z) ** 2 for point in points) / len(points)
    angle = 0.5 * math.atan2(2.0 * covariance_xz, covariance_xx - covariance_zz)
    axis_x = math.cos(angle)
    axis_z = math.sin(angle)
    normal_x = -axis_z
    normal_z = axis_x
    along = [(point[0] * axis_x) + (point[1] * axis_z) for point in points]
    across = [(point[0] * normal_x) + (point[1] * normal_z) for point in points]
    cell_span = max(cell_width, cell_depth)
    length = (max(along) - min(along)) + cell_span
    measured_thickness = (max(across) - min(across)) + min(cell_width, cell_depth)
    maximum_thickness = max(0.24, min(0.48, cell_span * 3.5))
    thickness = max(min(cell_width, cell_depth) * 1.35, min(measured_thickness, maximum_thickness))
    minimum_wall_length = max(0.9, cell_span * 6.0)
    if length < minimum_wall_length or length / max(thickness, 0.001) < 1.45:
        return None
    center_along = (max(along) + min(along)) / 2
    center_across = (max(across) + min(across)) / 2
    center_x = (center_along * axis_x) + (center_across * normal_x)
    center_z = (center_along * axis_z) + (center_across * normal_z)
    span_cols = max_col - min_col + 1
    span_rows = max_row - min_row + 1
    return {
        "center_x": round(center_x, 4),
        "center_z": round(center_z, 4),
        "width": round(length, 4),
        "depth": round(thickness, 4),
        # Three.js rotates local +X toward +Z with a negative Y angle.
        "rotation_y": round(-angle, 6),
        "_bbox_cells": (min_col, min_row, max_col, max_row),
        "_length_cells": float(max(span_cols, span_rows)),
    }


def _wall_segment_networks(
    segments: list[dict[str, object]],
    *,
    connection_gap_cells: int = 3,
) -> list[list[int]]:
    parents = list(range(len(segments)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(first: int, second: int) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root != second_root:
            parents[second_root] = first_root

    for index, segment in enumerate(segments):
        first_bbox = tuple(segment.get("_bbox_cells") or (0, 0, 0, 0))
        for other_index in range(index + 1, len(segments)):
            other_bbox = tuple(segments[other_index].get("_bbox_cells") or (0, 0, 0, 0))
            gap_x, gap_y = _bbox_axis_gap(first_bbox, other_bbox)
            if gap_x <= connection_gap_cells and gap_y <= connection_gap_cells:
                union(index, other_index)
    networks: dict[int, list[int]] = {}
    for index in range(len(segments)):
        networks.setdefault(find(index), []).append(index)
    return list(networks.values())


def _wall_rectangles_from_mask(
    wall_mask: list[list[int]],
    *,
    width_m: float,
    depth_m: float,
) -> list[dict[str, float]]:
    rows = len(wall_mask)
    cols = len(wall_mask[0]) if rows else 0
    if not rows or not cols:
        return []
    candidates: list[dict[str, object]] = []
    for axis in ("horizontal", "vertical"):
        directional_mask = _directional_run_mask(wall_mask, axis=axis)
        for component in _connected_components(directional_mask):
            segment = _oriented_wall_segment_from_component(
                directional_mask,
                component,
                width_m=width_m,
                depth_m=depth_m,
            )
            if segment is not None:
                candidates.append(segment)
    minimum_network_span = max(24.0, min(36.0, (rows + cols) * 0.13))
    accepted_indexes = {
        index
        for network in _wall_segment_networks(candidates)
        if sum(float(candidates[index].get("_length_cells") or 0.0) for index in network) >= minimum_network_span
        for index in network
    }
    oriented = [
        {
            "center_x": float(segment["center_x"]),
            "center_z": float(segment["center_z"]),
            "width": float(segment["width"]),
            "depth": float(segment["depth"]),
            "rotation_y": float(segment["rotation_y"]),
        }
        for index, segment in enumerate(candidates)
        if index in accepted_indexes
    ]
    oriented.sort(key=lambda row: (row["center_z"], row["center_x"], row["rotation_y"]))
    if oriented:
        return oriented
    return _axis_aligned_wall_rectangles_from_mask(wall_mask, width_m=width_m, depth_m=depth_m)


def _wall_rectangles_with_boundary_completion(
    wall_rectangles: list[dict[str, float]],
    *,
    width_m: float,
    depth_m: float,
) -> tuple[list[dict[str, float]], dict[str, object]]:
    """Close sparse extracted geometry with a disclosed room envelope.

    Some listing floorplans use furniture, fills, or fine antialiased strokes
    that leave fewer than four connected wall segments after annotation
    filtering.  A walkable preview still needs a closed outer envelope.  Keep
    every extracted segment and add four clearly disclosed boundary walls;
    this remains a generated planning aid and is never treated as a verified
    provider capture.
    """

    extracted = [dict(row) for row in wall_rectangles]
    extracted_count = len(extracted)
    if extracted_count >= 4:
        return extracted, {
            "status": "not_needed",
            "method": "none",
            "reason": "extracted_wall_count_sufficient",
            "extracted_wall_count": extracted_count,
            "added_wall_count": 0,
        }
    normalized_width = max(3.0, float(width_m or 0.0))
    normalized_depth = max(3.0, float(depth_m or 0.0))
    thickness = round(
        max(0.12, min(0.28, min(normalized_width, normalized_depth) * 0.018)),
        4,
    )
    half_width = normalized_width / 2.0
    half_depth = normalized_depth / 2.0
    boundary_walls = [
        {
            "center_x": 0.0,
            "center_z": round(-half_depth, 4),
            "width": round(normalized_width, 4),
            "depth": thickness,
            "rotation_y": 0.0,
        },
        {
            "center_x": 0.0,
            "center_z": round(half_depth, 4),
            "width": round(normalized_width, 4),
            "depth": thickness,
            "rotation_y": 0.0,
        },
        {
            "center_x": round(-half_width, 4),
            "center_z": 0.0,
            "width": round(normalized_depth, 4),
            "depth": thickness,
            "rotation_y": round(-math.pi / 2.0, 6),
        },
        {
            "center_x": round(half_width, 4),
            "center_z": 0.0,
            "width": round(normalized_depth, 4),
            "depth": thickness,
            "rotation_y": round(-math.pi / 2.0, 6),
        },
    ]
    return [*extracted, *boundary_walls], {
        "status": "applied",
        "method": "generated_room_envelope_v1",
        "reason": "extracted_wall_count_below_four",
        "extracted_wall_count": extracted_count,
        "added_wall_count": len(boundary_walls),
        "boundary_width_m": round(normalized_width, 4),
        "boundary_depth_m": round(normalized_depth, 4),
        "boundary_thickness_m": thickness,
    }


def _write_obj(
    target_dir: Path,
    *,
    width_m: float,
    depth_m: float,
    height_m: float,
    wall_rectangles: list[dict[str, float]],
    style_scene: dict[str, object] | None = None,
) -> None:
    applied_style_scene = style_scene or build_style_scene(
        reconstruction_style(),
        route_stop_count=1,
    )
    style_palette = dict(applied_style_scene.get("material_palette") or {})

    def material_rgb(key: str, fallback: str) -> tuple[float, float, float]:
        raw = str(style_palette.get(key) or fallback).strip().lstrip("#")
        if not re.fullmatch(r"[0-9a-fA-F]{6}", raw):
            raw = fallback.lstrip("#")
        return tuple(round(int(raw[index : index + 2], 16) / 255.0, 4) for index in (0, 2, 4))

    floor_rgb = material_rgb("floor", "#f0e6d6")
    wall_rgb = material_rgb("wall", "#e6ded0")
    obj_lines = [
        f"# style_id {str(applied_style_scene.get('style_id') or '')}",
        f"# style_signature {str(applied_style_scene.get('style_signature') or '')}",
        f"# style_scene_signature {str(applied_style_scene.get('scene_signature') or '')}",
        "mtllib model.mtl",
        "o propertyquarry_generated_layout",
    ]
    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[str, str, tuple[int, int, int, int]]] = []

    def add_quad(material: str, group: str, points: tuple[tuple[float, float, float], ...]) -> None:
        start_index = len(vertices) + 1
        vertices.extend(points)
        faces.append((material, group, (start_index, start_index + 1, start_index + 2, start_index + 3)))

    def add_box(
        material: str,
        group: str,
        *,
        center_x: float,
        center_z: float,
        box_width: float,
        box_depth: float,
        box_height: float,
        rotation_y: float = 0.0,
    ) -> None:
        half_box_width = box_width / 2
        half_box_depth = box_depth / 2
        min_y = 0.0
        max_y = box_height
        rotation_cos = math.cos(rotation_y)
        rotation_sin = math.sin(rotation_y)

        def rotated_point(local_x: float, y: float, local_z: float) -> tuple[float, float, float]:
            return (
                center_x + (local_x * rotation_cos) + (local_z * rotation_sin),
                y,
                center_z - (local_x * rotation_sin) + (local_z * rotation_cos),
            )

        points = [
            rotated_point(-half_box_width, min_y, -half_box_depth),
            rotated_point(half_box_width, min_y, -half_box_depth),
            rotated_point(half_box_width, min_y, half_box_depth),
            rotated_point(-half_box_width, min_y, half_box_depth),
            rotated_point(-half_box_width, max_y, -half_box_depth),
            rotated_point(half_box_width, max_y, -half_box_depth),
            rotated_point(half_box_width, max_y, half_box_depth),
            rotated_point(-half_box_width, max_y, half_box_depth),
        ]
        start_index = len(vertices) + 1
        vertices.extend(points)
        faces.extend(
            [
                (material, f"{group}_floor", (start_index, start_index + 1, start_index + 2, start_index + 3)),
                (material, f"{group}_north", (start_index, start_index + 4, start_index + 5, start_index + 1)),
                (material, f"{group}_east", (start_index + 1, start_index + 5, start_index + 6, start_index + 2)),
                (material, f"{group}_south", (start_index + 2, start_index + 6, start_index + 7, start_index + 3)),
                (material, f"{group}_west", (start_index + 3, start_index + 7, start_index + 4, start_index)),
                (material, f"{group}_ceiling", (start_index + 4, start_index + 7, start_index + 6, start_index + 5)),
            ]
        )

    add_quad(
        "warm_floor",
        "floor_plate",
        (
            (-width_m / 2, 0.0, -depth_m / 2),
            (width_m / 2, 0.0, -depth_m / 2),
            (width_m / 2, 0.0, depth_m / 2),
            (-width_m / 2, 0.0, depth_m / 2),
        ),
    )
    for index, rectangle in enumerate(wall_rectangles, start=1):
        add_box(
            "warm_plaster",
            f"wall_{index:03d}",
            center_x=float(rectangle["center_x"]),
            center_z=float(rectangle["center_z"]),
            box_width=float(rectangle["width"]),
            box_depth=float(rectangle["depth"]),
            box_height=height_m,
            rotation_y=float(rectangle.get("rotation_y") or 0.0),
        )
    for x, y, z in vertices:
        obj_lines.append(f"v {x:.4f} {y:.4f} {z:.4f}")
    current_material = ""
    for material, group, indexes in faces:
        if material != current_material:
            obj_lines.append(f"usemtl {material}")
            current_material = material
        obj_lines.append(f"g {group}")
        obj_lines.append("f " + " ".join(str(index) for index in indexes))
    (target_dir / "model.obj").write_text("\n".join(obj_lines) + "\n", encoding="utf-8")
    (target_dir / "model.mtl").write_text(
        "\n".join(
            [
                f"# style_id {str(applied_style_scene.get('style_id') or '')}",
                f"# style_signature {str(applied_style_scene.get('style_signature') or '')}",
                f"# style_scene_signature {str(applied_style_scene.get('scene_signature') or '')}",
                "newmtl warm_floor",
                f"Ka {floor_rgb[0]:.4f} {floor_rgb[1]:.4f} {floor_rgb[2]:.4f}",
                f"Kd {floor_rgb[0]:.4f} {floor_rgb[1]:.4f} {floor_rgb[2]:.4f}",
                "Ks 0.01 0.01 0.01",
                "Ns 8",
                "newmtl warm_plaster",
                f"Ka {wall_rgb[0]:.4f} {wall_rgb[1]:.4f} {wall_rgb[2]:.4f}",
                f"Kd {wall_rgb[0]:.4f} {wall_rgb[1]:.4f} {wall_rgb[2]:.4f}",
                "Ks 0.04 0.04 0.04",
                "Ns 14",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _copy_viewer_vendor_assets(target_dir: Path) -> dict[str, object]:
    source_specs = (
        ("three.module.js", THREE_MODULE_SOURCE, THREE_MODULE_SOURCE_SHA256),
        ("OrbitControls.js", ORBIT_CONTROLS_SOURCE, ORBIT_CONTROLS_SOURCE_SHA256),
        ("LICENSE", THREE_LICENSE_SOURCE, THREE_LICENSE_SOURCE_SHA256),
    )
    if any(not source_path.is_file() for _, source_path, _ in source_specs):
        raise FileNotFoundError("viewer_vendor_assets_missing")
    source_hashes: dict[str, str] = {}
    for source_name, source_path, expected_sha256 in source_specs:
        actual_sha256 = _sha256(source_path)
        source_hashes[source_name] = actual_sha256
        if actual_sha256 != expected_sha256:
            raise RuntimeError(
                f"viewer_vendor_integrity_mismatch:{source_name}:expected={expected_sha256}:actual={actual_sha256}"
            )

    license_text = THREE_LICENSE_SOURCE.read_text(encoding="utf-8")
    license_notice = f"/*!\n{license_text.rstrip()}\n*/\n"
    license_notice_sha256 = hashlib.sha256(license_notice.encode("utf-8")).hexdigest()
    if license_notice_sha256 != THREE_LICENSE_NOTICE_SHA256:
        raise RuntimeError(
            "viewer_vendor_integrity_mismatch:embedded_license_notice:"
            f"expected={THREE_LICENSE_NOTICE_SHA256}:actual={license_notice_sha256}"
        )

    vendor_dir = target_dir / "vendor"
    three_target = vendor_dir / "three.module.js"
    orbit_target = vendor_dir / "examples" / "jsm" / "controls" / "OrbitControls.js"
    orbit_target.parent.mkdir(parents=True, exist_ok=True)
    three_source = THREE_MODULE_SOURCE.read_text(encoding="utf-8")
    emitted_three_source = license_notice + three_source
    emitted_three_sha256 = hashlib.sha256(emitted_three_source.encode("utf-8")).hexdigest()
    if emitted_three_sha256 != THREE_MODULE_EMITTED_SHA256:
        raise RuntimeError(
            "viewer_vendor_integrity_mismatch:emitted_three.module.js:"
            f"expected={THREE_MODULE_EMITTED_SHA256}:actual={emitted_three_sha256}"
        )
    orbit_source = ORBIT_CONTROLS_SOURCE.read_text(encoding="utf-8")
    if orbit_source.count(ORBIT_CONTROLS_BARE_IMPORT) != 1:
        raise RuntimeError("viewer_orbit_controls_bare_import_count_mismatch")
    transformed_orbit_source = orbit_source.replace(
        ORBIT_CONTROLS_BARE_IMPORT,
        ORBIT_CONTROLS_RELATIVE_IMPORT,
        1,
    )
    if transformed_orbit_source == orbit_source:
        raise RuntimeError("viewer_orbit_controls_bare_import_missing")
    transformed_orbit_sha256 = hashlib.sha256(transformed_orbit_source.encode("utf-8")).hexdigest()
    if transformed_orbit_sha256 != ORBIT_CONTROLS_TRANSFORMED_SHA256:
        raise RuntimeError(
            "viewer_vendor_integrity_mismatch:transformed_OrbitControls.js:"
            f"expected={ORBIT_CONTROLS_TRANSFORMED_SHA256}:actual={transformed_orbit_sha256}"
        )
    emitted_orbit_source = license_notice + transformed_orbit_source
    emitted_orbit_sha256 = hashlib.sha256(emitted_orbit_source.encode("utf-8")).hexdigest()
    if emitted_orbit_sha256 != ORBIT_CONTROLS_EMITTED_SHA256:
        raise RuntimeError(
            "viewer_vendor_integrity_mismatch:emitted_OrbitControls.js:"
            f"expected={ORBIT_CONTROLS_EMITTED_SHA256}:actual={emitted_orbit_sha256}"
        )

    three_target.write_text(emitted_three_source, encoding="utf-8")
    orbit_target.write_text(emitted_orbit_source, encoding="utf-8")
    return {
        "three_relpath": "vendor/three.module.js",
        "orbit_controls_relpath": "vendor/examples/jsm/controls/OrbitControls.js",
        "provenance": {
            "name": "three",
            "package": "three",
            "version": THREE_VENDOR_VERSION,
            "license": "MIT",
            "upstream_git_head": THREE_UPSTREAM_GIT_HEAD,
            "upstream_dist_integrity": THREE_UPSTREAM_DIST_INTEGRITY,
            "upstream_dist_shasum": THREE_UPSTREAM_DIST_SHASUM,
            "source": {
                "three_module_sha256": source_hashes["three.module.js"],
                "orbit_controls_sha256": source_hashes["OrbitControls.js"],
                "license_sha256": source_hashes["LICENSE"],
            },
            "license_notice": {
                "spdx": "MIT",
                "source_relpath": f"vendor/three/{THREE_VENDOR_VERSION}/LICENSE",
                "source_sha256": source_hashes["LICENSE"],
                "embedded_notice_sha256": license_notice_sha256,
                "embedded_in_all_emitted_modules": True,
            },
            "sources": {
                "three_module": {
                    "source_relpath": f"vendor/three/{THREE_VENDOR_VERSION}/three.module.js",
                    "source_sha256": source_hashes["three.module.js"],
                },
                "orbit_controls": {
                    "source_relpath": f"vendor/three/{THREE_VENDOR_VERSION}/examples/jsm/controls/OrbitControls.js",
                    "source_sha256": source_hashes["OrbitControls.js"],
                },
            },
            "transform": {
                "id": "orbit_controls_relative_import_v1",
                "asset": "OrbitControls.js",
                "operation": "single_exact_string_replacement",
                "from": ORBIT_CONTROLS_BARE_IMPORT,
                "to": ORBIT_CONTROLS_RELATIVE_IMPORT,
                "replacement_count": 1,
                "transformed_before_notice_sha256": transformed_orbit_sha256,
                "derived_three_module_sha256": emitted_three_sha256,
                "derived_orbit_controls_sha256": emitted_orbit_sha256,
                "notice_embedding": "full_mit_in_each_emitted_module",
            },
            "emitted": {
                "three_module": {
                    "relpath": "vendor/three.module.js",
                    "sha256": emitted_three_sha256,
                },
                "orbit_controls": {
                    "relpath": "vendor/examples/jsm/controls/OrbitControls.js",
                    "sha256": emitted_orbit_sha256,
                },
            },
        },
    }


def _write_glb(
    target_dir: Path,
    *,
    style_scene: dict[str, object] | None = None,
) -> dict[str, object]:
    obj_path = target_dir / "model.obj"
    glb_path = target_dir / "model.glb"
    temporary_glb_path = target_dir / f".model.glb.{secrets.token_hex(8)}.tmp"
    if not obj_path.is_file():
        glb_path.unlink(missing_ok=True)
        return {"status": "skipped", "reason": "obj_missing"}

    applied_style_scene = style_scene or build_style_scene(
        reconstruction_style(),
        route_stop_count=1,
    )
    style_palette = dict(applied_style_scene.get("material_palette") or {})

    def glb_color(key: str, fallback: str) -> tuple[float, float, float, float]:
        raw = str(style_palette.get(key) or fallback).strip().lstrip("#")
        if not re.fullmatch(r"[0-9a-fA-F]{6}", raw):
            raw = fallback.lstrip("#")
        rgb = tuple(round(int(raw[index : index + 2], 16) / 255.0, 6) for index in (0, 2, 4))
        return (*rgb, 1.0)

    material_specs = (
        ("warm_floor", glb_color("floor", "#f0e6d6"), 0.88),
        ("warm_plaster", glb_color("wall", "#e6ded0"), 0.82),
    )
    material_names = tuple(name for name, _color, _roughness in material_specs)

    try:
        source_vertices: list[tuple[float, float, float]] = []
        faces_by_material: dict[str, list[tuple[int, ...]]] = {name: [] for name in material_names}
        current_material = ""
        for line_number, raw_line in enumerate(obj_path.read_text(encoding="utf-8").splitlines(), start=1):
            fields = raw_line.strip().split()
            if not fields or fields[0].startswith("#"):
                continue
            directive = fields[0]
            if directive == "v":
                if len(fields) < 4:
                    raise ValueError(f"obj_vertex_invalid:{line_number}")
                point = (float(fields[1]), float(fields[2]), float(fields[3]))
                if not all(math.isfinite(value) for value in point):
                    raise ValueError(f"obj_vertex_non_finite:{line_number}")
                source_vertices.append(point)
                continue
            if directive == "usemtl":
                if len(fields) != 2 or fields[1] not in material_names:
                    raise ValueError(f"obj_material_unsupported:{line_number}")
                current_material = fields[1]
                continue
            if directive != "f":
                continue
            if current_material not in material_names:
                raise ValueError(f"obj_face_material_missing:{line_number}")
            if len(fields) < 4:
                raise ValueError(f"obj_face_invalid:{line_number}")
            indexes: list[int] = []
            for field in fields[1:]:
                vertex_ref = field.split("/", 1)[0]
                try:
                    obj_index = int(vertex_ref)
                except ValueError as exc:
                    raise ValueError(f"obj_face_index_invalid:{line_number}") from exc
                if obj_index == 0:
                    raise ValueError(f"obj_face_index_zero:{line_number}")
                vertex_index = obj_index - 1 if obj_index > 0 else len(source_vertices) + obj_index
                if vertex_index < 0 or vertex_index >= len(source_vertices):
                    raise ValueError(f"obj_face_index_out_of_range:{line_number}")
                indexes.append(vertex_index)
            faces_by_material[current_material].append(tuple(indexes))
        if not source_vertices:
            raise ValueError("obj_vertices_missing")
        if not any(faces_by_material.values()):
            raise ValueError("obj_faces_missing")
        generated_vertex_count = len(source_vertices)
        generated_triangle_count = sum(
            max(0, len(face) - 2)
            for material_faces in faces_by_material.values()
            for face in material_faces
        )

        binary = bytearray()
        buffer_views: list[dict[str, object]] = []
        accessors: list[dict[str, object]] = []
        primitives: list[dict[str, object]] = []

        def append_binary(payload: bytes, *, target: int) -> int:
            binary.extend(b"\x00" * (-len(binary) % 4))
            byte_offset = len(binary)
            binary.extend(payload)
            buffer_views.append(
                {
                    "buffer": 0,
                    "byteLength": len(payload),
                    "byteOffset": byte_offset,
                    "target": target,
                }
            )
            return len(buffer_views) - 1

        def float32(value: float) -> float:
            return struct.unpack("<f", struct.pack("<f", value))[0]

        for material_index, (material_name, _color, _roughness) in enumerate(material_specs):
            material_faces = faces_by_material[material_name]
            if not material_faces:
                continue
            positions: list[tuple[float, float, float]] = []
            normals: list[tuple[float, float, float]] = []
            indices: list[int] = []
            for face in material_faces:
                points = [source_vertices[index] for index in face]
                normal_x = normal_y = normal_z = 0.0
                for point_index, point in enumerate(points):
                    next_point = points[(point_index + 1) % len(points)]
                    normal_x += (point[1] - next_point[1]) * (point[2] + next_point[2])
                    normal_y += (point[2] - next_point[2]) * (point[0] + next_point[0])
                    normal_z += (point[0] - next_point[0]) * (point[1] + next_point[1])
                normal_length = math.sqrt((normal_x * normal_x) + (normal_y * normal_y) + (normal_z * normal_z))
                if normal_length <= 1e-12:
                    raise ValueError("obj_face_degenerate")
                normal = (
                    normal_x / normal_length,
                    normal_y / normal_length,
                    normal_z / normal_length,
                )
                base_index = len(positions)
                positions.extend(points)
                normals.extend([normal] * len(points))
                for triangle_index in range(1, len(points) - 1):
                    indices.extend((base_index, base_index + triangle_index, base_index + triangle_index + 1))

            packed_positions = [tuple(float32(value) for value in point) for point in positions]
            packed_normals = [tuple(float32(value) for value in normal) for normal in normals]
            position_payload = b"".join(struct.pack("<3f", *point) for point in packed_positions)
            normal_payload = b"".join(struct.pack("<3f", *normal) for normal in packed_normals)
            position_view = append_binary(position_payload, target=34962)
            normal_view = append_binary(normal_payload, target=34962)
            position_accessor = len(accessors)
            accessors.append(
                {
                    "bufferView": position_view,
                    "componentType": 5126,
                    "count": len(packed_positions),
                    "max": [max(point[axis] for point in packed_positions) for axis in range(3)],
                    "min": [min(point[axis] for point in packed_positions) for axis in range(3)],
                    "type": "VEC3",
                }
            )
            normal_accessor = len(accessors)
            accessors.append(
                {
                    "bufferView": normal_view,
                    "componentType": 5126,
                    "count": len(packed_normals),
                    "type": "VEC3",
                }
            )
            index_component_type = 5123 if max(indices) <= 65535 else 5125
            index_format = "<H" if index_component_type == 5123 else "<I"
            index_payload = b"".join(struct.pack(index_format, index) for index in indices)
            index_view = append_binary(index_payload, target=34963)
            index_accessor = len(accessors)
            accessors.append(
                {
                    "bufferView": index_view,
                    "componentType": index_component_type,
                    "count": len(indices),
                    "max": [max(indices)],
                    "min": [min(indices)],
                    "type": "SCALAR",
                }
            )
            primitives.append(
                {
                    "attributes": {"NORMAL": normal_accessor, "POSITION": position_accessor},
                    "indices": index_accessor,
                    "material": material_index,
                    "mode": 4,
                }
            )

        binary.extend(b"\x00" * (-len(binary) % 4))
        document = {
            "accessors": accessors,
            "asset": {"generator": "PropertyQuarry deterministic GLB writer", "version": "2.0"},
            "bufferViews": buffer_views,
            "buffers": [{"byteLength": len(binary)}],
            "materials": [
                {
                    "doubleSided": True,
                    "name": name,
                    "pbrMetallicRoughness": {
                        "baseColorFactor": list(color),
                        "metallicFactor": 0.0,
                        "roughnessFactor": roughness,
                    },
                }
                for name, color, roughness in material_specs
            ],
            "meshes": [{"name": "propertyquarry_generated_layout", "primitives": primitives}],
            "nodes": [{"mesh": 0, "name": "propertyquarry_generated_layout"}],
            "scene": 0,
            "scenes": [{"nodes": [0]}],
        }
        json_payload = json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        json_payload += b" " * (-len(json_payload) % 4)
        total_length = 12 + 8 + len(json_payload) + 8 + len(binary)
        glb_payload = b"".join(
            (
                struct.pack("<III", 0x46546C67, 2, total_length),
                struct.pack("<II", len(json_payload), 0x4E4F534A),
                json_payload,
                struct.pack("<II", len(binary), 0x004E4942),
                bytes(binary),
            )
        )
        with temporary_glb_path.open("xb") as handle:
            handle.write(glb_payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_glb_path, glb_path)
    except (OSError, OverflowError, ValueError, struct.error) as exc:
        temporary_glb_path.unlink(missing_ok=True)
        glb_path.unlink(missing_ok=True)
        return {
            "status": "failed",
            "reason": "glb_export_failed",
            "error_class": type(exc).__name__,
        }
    return {
        "status": "generated",
        "exporter": GLB_EXPORTER_VERSION,
        "glb_relpath": glb_path.name,
        "glb_sha256": _sha256(glb_path),
        "glb_size_bytes": glb_path.stat().st_size,
        "material_count": len(material_specs),
        "source_obj_sha256": _sha256(obj_path),
        "triangle_count": generated_triangle_count,
        "vertex_count": generated_vertex_count,
    }


def _iter_mp4_boxes(
    handle: BinaryIO,
    *,
    start: int,
    end: int,
) -> Iterator[tuple[bytes, int, int]]:
    position = start
    while position + 8 <= end:
        handle.seek(position)
        header = handle.read(8)
        if len(header) != 8:
            raise ValueError("mp4_box_header_truncated")
        size32, box_type = struct.unpack(">I4s", header)
        header_size = 8
        if size32 == 1:
            extended = handle.read(8)
            if len(extended) != 8:
                raise ValueError("mp4_extended_size_truncated")
            box_size = struct.unpack(">Q", extended)[0]
            header_size = 16
        elif size32 == 0:
            box_size = end - position
        else:
            box_size = size32
        if box_size < header_size:
            raise ValueError("mp4_box_size_invalid")
        box_end = position + box_size
        if box_end > end:
            raise ValueError("mp4_box_out_of_bounds")
        yield box_type, position + header_size, box_end
        position = box_end
    if position != end:
        raise ValueError("mp4_box_trailing_bytes")


def _video_duration_seconds(path: Path) -> float:
    if not path.is_file():
        return 0.0
    try:
        file_size = path.stat().st_size
        with path.open("rb") as handle:
            top_level = list(_iter_mp4_boxes(handle, start=0, end=file_size))
            moov_index = next(
                (index for index, row in enumerate(top_level) if row[0] == b"moov"),
                None,
            )
            mdat_index = next(
                (index for index, row in enumerate(top_level) if row[0] == b"mdat"),
                None,
            )
            if moov_index is None or mdat_index is None or moov_index >= mdat_index:
                return 0.0
            _box_type, payload_start, box_end = top_level[moov_index]
            for child_type, child_start, child_end in _iter_mp4_boxes(
                handle,
                start=payload_start,
                end=box_end,
            ):
                if child_type != b"mvhd":
                    continue
                handle.seek(child_start)
                payload = handle.read(min(32, child_end - child_start))
                if len(payload) < 20:
                    raise ValueError("mp4_mvhd_truncated")
                version = payload[0]
                if version == 0:
                    timescale, duration = struct.unpack_from(">II", payload, 12)
                    unknown_duration = 0xFFFFFFFF
                elif version == 1:
                    if len(payload) < 32:
                        raise ValueError("mp4_mvhd_v1_truncated")
                    timescale, duration = struct.unpack_from(">IQ", payload, 20)
                    unknown_duration = 0xFFFFFFFFFFFFFFFF
                else:
                    raise ValueError("mp4_mvhd_version_unsupported")
                if timescale <= 0 or duration == unknown_duration:
                    return 0.0
                parsed = duration / timescale
                return parsed if math.isfinite(parsed) and parsed >= 0.0 else 0.0
    except (OSError, ValueError, struct.error):
        return 0.0
    return 0.0


def _rgb24_frame_bytes(
    frame: Image.Image | Path,
    *,
    frame_size: tuple[int, int],
) -> bytes:
    if isinstance(frame, Path):
        # Frame paths here are renderer-owned intermediates whose dimensions
        # are checked exactly below.  Permit tiny test/internal frames without
        # weakening the stricter floorplan and listing-photo intake default.
        observed_size: tuple[int, int] | None = None
        with _open_bounded_source_image(frame, minimum_dimension=1) as source:
            source.load()
            if source.size == frame_size:
                return _rgb24_frame_bytes(source, frame_size=frame_size)
            observed_size = (int(source.width), int(source.height))
        raise ValueError(
            f"raw_video_frame_size_invalid:{observed_size[0]}x{observed_size[1]}"
        )
    if frame.size != frame_size:
        raise ValueError(
            f"raw_video_frame_size_invalid:{frame.size[0]}x{frame.size[1]}"
        )
    rgb_frame = frame.convert("RGB")
    try:
        payload = rgb_frame.tobytes()
    finally:
        rgb_frame.close()
    expected_size = frame_size[0] * frame_size[1] * 3
    if len(payload) != expected_size:
        raise ValueError("raw_video_frame_payload_invalid")
    return payload


def _encode_rgb24_mp4(
    *,
    frames: Iterable[Image.Image | Path],
    target: Path,
    frame_size: tuple[int, int],
    input_fps: float,
    output_fps: int,
    expected_input_frame_count: int,
    expected_frame_count: int,
    crf: int,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[bytes]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise FileNotFoundError("ffmpeg_missing")
    width, height = frame_size
    if (
        input_fps <= 0.0
        or output_fps <= 0
        or expected_input_frame_count <= 0
        or expected_frame_count <= 0
        or expected_frame_count > MAX_WALKTHROUGH_ENCODED_FRAMES
    ):
        raise ValueError("raw_video_timing_invalid")
    temporary_target = target.with_name(
        f".{target.name}.{secrets.token_hex(8)}.tmp"
    )
    filter_thread_options, encoder_thread_options = _ffmpeg_thread_options()
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostats",
        "-y",
        *filter_thread_options,
        "-f",
        "rawvideo",
        "-pixel_format",
        "rgb24",
        "-video_size",
        f"{width}x{height}",
        "-framerate",
        f"{input_fps:.6f}",
        "-i",
        "pipe:0",
        "-map",
        "0:v:0",
        "-vf",
        f"fps={output_fps},format=yuv420p",
        "-frames:v",
        str(expected_frame_count),
        "-an",
        "-sn",
        "-dn",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        *encoder_thread_options,
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(temporary_target),
    ]
    inherited_anchor_fds = tuple(
        sorted(
            {
                int(match.group(1))
                for candidate in (target, temporary_target)
                if (
                    match := re.match(
                        r"^/proc/self/fd/([0-9]+)(?:/|$)",
                        os.fspath(candidate),
                    )
                )
            }
        )
    )
    process: subprocess.Popen[bytes] | None = None
    timer: threading.Timer | None = None
    timed_out = threading.Event()
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            pass_fds=inherited_anchor_fds,
        )

        def terminate_on_timeout() -> None:
            timed_out.set()
            try:
                process.kill()
            except OSError:
                pass

        timer = threading.Timer(timeout_seconds, terminate_on_timeout)
        timer.daemon = True
        timer.start()
        input_frame_count = 0
        try:
            if process.stdin is None:
                raise RuntimeError("ffmpeg_stdin_unavailable")
            for frame in frames:
                process.stdin.write(_rgb24_frame_bytes(frame, frame_size=frame_size))
                input_frame_count += 1
                if input_frame_count > expected_input_frame_count:
                    raise ValueError("raw_video_input_frame_count_exceeded")
            if input_frame_count != expected_input_frame_count:
                raise ValueError("raw_video_input_frame_count_invalid")
            process.stdin.close()
            process.stdin = None
            _stdout, stderr = process.communicate()
        except BrokenPipeError:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
                process.stdin = None
            _stdout, stderr = process.communicate()
        if timed_out.is_set():
            raise subprocess.TimeoutExpired(command, timeout_seconds)
        completed = subprocess.CompletedProcess(
            command,
            process.returncode,
            stdout=b"",
            stderr=stderr or b"",
        )
        if completed.returncode == 0:
            duration = _validated_mp4_duration(
                temporary_target,
                expected_frame_count=expected_frame_count,
                fps=output_fps,
            )
            if duration <= 0.0:
                raise ValueError("mp4_duration_validation_failed")
            os.replace(temporary_target, target)
        return completed
    finally:
        if timer is not None:
            timer.cancel()
        if process is not None:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
                process.stdin = None
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                process.communicate()
            except (BrokenPipeError, OSError, ValueError):
                pass
        temporary_target.unlink(missing_ok=True)


def _ffmpeg_failure_receipt(
    result: subprocess.CompletedProcess[bytes],
) -> dict[str, object]:
    diagnostic = bytes(result.stderr or b"")
    return {
        "status": "failed",
        "reason": "ffmpeg_exit_nonzero",
        "returncode": int(result.returncode),
        "diagnostic_sha256": hashlib.sha256(diagnostic).hexdigest(),
        "diagnostic_size_bytes": len(diagnostic),
    }


def _validated_mp4_duration(
    path: Path,
    *,
    expected_frame_count: int,
    fps: int,
) -> float:
    if expected_frame_count <= 0 or fps <= 0:
        return 0.0
    duration = _video_duration_seconds(path)
    expected_duration = expected_frame_count / fps
    tolerance = (1.0 / fps) + 1e-6
    if duration <= 0.0 or abs(duration - expected_duration) > tolerance:
        return 0.0
    return duration


def _declutter_floorplan_stop_positions(
    positions: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Keep 44px route targets separate on the viewer's 4:3 floorplan map."""
    placed: list[tuple[float, float]] = []
    x_step = 16.0
    y_step = 21.0
    for raw_left, raw_top in positions:
        candidates: list[tuple[float, float, float]] = []
        seen: set[tuple[float, float]] = set()
        for grid_x in range(-6, 7):
            for grid_y in range(-5, 6):
                left = round(_clamp_float(raw_left + (grid_x * x_step), 8.0, 92.0), 2)
                top = round(_clamp_float(raw_top + (grid_y * y_step), 10.0, 90.0), 2)
                key = (left, top)
                if key in seen:
                    continue
                seen.add(key)
                distance = ((left - raw_left) ** 2) + ((top - raw_top) ** 2)
                candidates.append((distance, left, top))
        candidates.sort(key=lambda row: (row[0], abs(row[2] - raw_top), abs(row[1] - raw_left)))
        selected = (round(raw_left, 2), round(raw_top, 2))
        for _distance, left, top in candidates:
            if all(abs(left - other_left) >= 15.5 or abs(top - other_top) >= 20.0 for other_left, other_top in placed):
                selected = (left, top)
                break
        placed.append(selected)
    return placed


def _html_script_safe_json(value: object) -> str:
    """Serialize JSON without allowing data to terminate an HTML script."""
    serialized = json.dumps(value, ensure_ascii=False)
    return (
        serialized.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _viewer_html(*, manifest: dict[str, object], three_relpath: str, orbit_controls_relpath: str) -> str:
    width_m = manifest["room_dimensions_m"]["width"]
    depth_m = manifest["room_dimensions_m"]["depth"]
    height_m = manifest["room_dimensions_m"]["height"]
    photos = manifest.get("photos") if isinstance(manifest.get("photos"), list) else []
    geometry = dict(manifest.get("geometry") or {}) if isinstance(manifest.get("geometry"), dict) else {}
    wall_rectangles = geometry.get("wall_rectangles") if isinstance(geometry.get("wall_rectangles"), list) else []
    floor_texture_crop = (
        dict(geometry.get("floor_texture_crop") or {})
        if isinstance(geometry.get("floor_texture_crop"), dict)
        else {}
    )
    walkable_scene = dict(manifest.get("walkable_scene") or {}) if isinstance(manifest.get("walkable_scene"), dict) else {}
    route_stops = list(walkable_scene.get("route") or []) if isinstance(walkable_scene.get("route"), list) else []
    photo_reference_panels = (
        list(manifest.get("photo_reference_panels") or [])
        if isinstance(manifest.get("photo_reference_panels"), list)
        else []
    )
    style_scene = dict(manifest.get("style_scene") or {}) if isinstance(manifest.get("style_scene"), dict) else {}
    requested_style = (
        dict(manifest.get("requested_style") or {})
        if isinstance(manifest.get("requested_style"), dict)
        else {}
    )
    if not style_scene:
        try:
            fallback_style = reconstruction_style(manifest.get("style_label"))
        except ValueError:
            fallback_style = reconstruction_style()
        style_scene = build_style_scene(
            fallback_style,
            route_stop_count=max(1, len(route_stops)),
        )
    style_label = str(style_scene.get("style_label") or manifest.get("style_label") or "").strip()
    style_id = str(style_scene.get("style_id") or requested_style.get("id") or "").strip()
    style_signature = str(style_scene.get("style_signature") or requested_style.get("signature") or "").strip()
    style_scene_signature = str(style_scene.get("scene_signature") or "").strip()
    escaped_style = html.escape(style_label)
    style_copy = f'<span>{escaped_style}</span>' if escaped_style else ""
    source_description = "the floorplan and listing photos" if photos else "the floorplan"
    floorplan_relpath = html.escape(str(dict(manifest.get("floorplan") or {}).get("relpath") or "source-floorplan.jpg"))
    floorplan_stop_rows: list[dict[str, object]] = []
    for index, stop in enumerate(route_stops):
        if not isinstance(stop, dict):
            continue
        focus = dict(stop.get("focus") or {}) if isinstance(stop.get("focus"), dict) else {}
        label = html.escape(str(stop.get("label") or stop.get("room") or stop.get("name") or f"Stop {index + 1}"))
        left_pct = round(
            _clamp_float((((float(focus.get("x") or 0.0) / max(float(width_m), 0.001)) + 0.5) * 100.0), 8.0, 92.0),
            2,
        )
        top_pct = round(
            _clamp_float((((float(focus.get("z") or 0.0) / max(float(depth_m), 0.001)) + 0.5) * 100.0), 10.0, 90.0),
            2,
        )
        floorplan_stop_rows.append(
            {
                "index": index,
                "label": label,
                "source_left_pct": left_pct,
                "source_top_pct": top_pct,
            }
        )
    display_positions = _declutter_floorplan_stop_positions(
        [
            (float(row["source_left_pct"]), float(row["source_top_pct"]))
            for row in floorplan_stop_rows
        ]
    )
    floorplan_stop_items: list[str] = []
    floorplan_leader_items: list[str] = []
    floorplan_anchor_items: list[str] = []
    for row, (display_left_pct, display_top_pct) in zip(floorplan_stop_rows, display_positions):
        index = int(row["index"])
        label = str(row["label"])
        source_left_pct = float(row["source_left_pct"])
        source_top_pct = float(row["source_top_pct"])
        if abs(display_left_pct - source_left_pct) > 0.5 or abs(display_top_pct - source_top_pct) > 0.5:
            floorplan_leader_items.append(
                f'<line x1="{source_left_pct}" y1="{source_top_pct}" x2="{display_left_pct}" y2="{display_top_pct}" />'
            )
        floorplan_anchor_items.append(
            f'<circle cx="{source_left_pct}" cy="{source_top_pct}" r="1.15" />'
        )
        floorplan_stop_items.append(
            f"""
        <button class="floorplan-stop" type="button" data-route-index="{index}" aria-label="Go to {label}" aria-current="false" style="left:{display_left_pct}%;top:{display_top_pct}%;">
          <span class="floorplan-stop-index">{index + 1}</span>
          <span class="floorplan-stop-label">{label}</span>
        </button>"""
        )
    floorplan_stop_markup = "".join(floorplan_stop_items)
    floorplan_route_overlay = (
        f"""
        <svg class="floorplan-route-overlay" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
          <polyline points="{' '.join(f'{row["source_left_pct"]},{row["source_top_pct"]}' for row in floorplan_stop_rows)}" />
          {''.join(floorplan_leader_items)}
          {''.join(floorplan_anchor_items)}
        </svg>"""
        if floorplan_stop_rows
        else ""
    )
    route_items = "\n".join(
        f'<button class="route-button" type="button" data-route-index="{index}" aria-current="false">{html.escape(str(stop.get("label") or stop.get("room") or stop.get("name") or f"Stop {index + 1}"))}</button>'
        for index, stop in enumerate(route_stops)
        if isinstance(stop, dict)
    )
    route_section = (
        f"""
    <section class="panel" aria-label="Room route">
      <div class="panel-head">
        <p>Room route</p>
        <span>{len(route_stops)}</span>
      </div>
      <div class="route-buttons">{route_items}</div>
    </section>"""
        if route_items
        else ""
    )
    photo_items = "\n".join(
        f'<img src="{html.escape(str(row["relpath"]))}" alt="Room photo {index}" loading="lazy">'
        for index, row in enumerate(photos, start=1)
        if isinstance(row, dict) and row.get("relpath")
    )
    photo_section = (
        f"""
    <section class="panel" aria-label="Listing photos">
      <div class="panel-head">
        <p>Photos</p>
        <span>{len(photos)}</span>
      </div>
      <div class="photos">{photo_items}</div>
    </section>"""
        if photo_items
        else ""
    )
    return f"""<!doctype html>
<html lang="en" data-pq-preview-kind="styled-3d-reconstruction" data-pq-verified-provider-capture="false" data-pq-style-id="{html.escape(style_id)}" data-pq-style-signature="{html.escape(style_signature)}" data-pq-style-scene-signature="{html.escape(style_scene_signature)}" data-pq-floorplan-display-mode="{FLOORPLAN_DISPLAY_MODE}" data-viewer-status="loading">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="data:,">
  <title>Styled 3D reconstruction | PropertyQuarry</title>
  <style>
    :root {{
      color-scheme: light;
      --ink:#17201c;
      --muted:#5f6b65;
      --canvas:#e8eeeb;
      --surface:#ffffff;
      --panel:rgba(255,255,255,.94);
      --line:#d5ddd8;
      --accent:#146b5d;
      --accent-soft:#e4f1ed;
      --signal:#c85f43;
      --shadow:0 12px 32px rgba(23,32,28,.1);
    }}
    * {{ box-sizing: border-box; }}
    .sr-only {{
      position:absolute;
      width:1px;
      height:1px;
      padding:0;
      margin:-1px;
      overflow:hidden;
      clip:rect(0,0,0,0);
      clip-path:inset(50%);
      white-space:nowrap;
      border:0;
    }}
    body {{
      margin:0;
      min-height:100vh;
      font-family:Aptos, ui-sans-serif, system-ui, sans-serif;
      background:#f2f5f3;
      color:var(--ink);
    }}
    main {{ min-height:100vh; display:grid; grid-template-columns:minmax(0,1fr) 340px; gap:0; padding:0; }}
    .stage {{ position:relative; min-height:100vh; }}
    .viewport {{
      width:100%;
      height:100vh;
      min-height:560px;
      border:0;
      border-radius:0;
      background:var(--canvas);
      box-shadow:none;
      overflow:hidden;
      touch-action:none;
    }}
    .viewport canvas {{
      display:block;
      width:100%;
      height:100%;
    }}
    .viewer-fallback {{
      position:absolute;
      top:50%;
      left:50%;
      z-index:5;
      width:min(440px,calc(100% - 40px));
      transform:translate(-50%,-50%);
      padding:20px 22px;
      border:1px solid var(--line);
      border-left:4px solid var(--signal);
      border-radius:6px;
      background:rgba(255,255,255,.97);
      box-shadow:var(--shadow);
    }}
    .viewer-fallback[hidden] {{ display:none; }}
    .viewer-fallback strong {{ display:block; font-size:18px; line-height:1.25; }}
    .viewer-fallback p {{ margin:8px 0 0; color:var(--muted); font-size:14px; line-height:1.45; }}
    .viewport[data-render-status="unavailable"] {{
      background:linear-gradient(145deg,#edf2ef,#dfe8e3);
    }}
    .stage-hotspots {{
      position:absolute;
      inset:0;
      pointer-events:none;
      z-index:2;
      overflow:hidden;
    }}
    .route-hotspot {{
      position:absolute;
      transform:translate(-50%,-50%);
      width:44px;
      height:44px;
      border:0;
      border-radius:50%;
      padding:0;
      background:transparent;
      color:var(--accent);
      display:grid;
      place-items:center;
      font:inherit;
      font-size:12px;
      font-weight:700;
      cursor:pointer;
      pointer-events:auto;
    }}
    .route-hotspot[hidden] {{ display:none; }}
    .route-hotspot::before {{
      content:"";
      position:absolute;
      inset:8px;
      z-index:-1;
      border:1px solid rgba(20,107,93,.38);
      border-radius:50%;
      background:rgba(255,255,255,.94);
      box-shadow:0 8px 20px rgba(23,32,28,.14);
    }}
    .route-hotspot-label {{
      position:absolute;
      top:calc(50% + 20px);
      left:50%;
      max-width:min(260px,calc(100vw - 24px));
      transform:translateX(calc(-50% + var(--hotspot-label-shift-x,0px))) translateY(var(--hotspot-label-shift-y,0px));
      padding:5px 8px;
      border:1px solid var(--line);
      border-radius:4px;
      background:rgba(255,255,255,.96);
      color:var(--ink);
      box-shadow:0 8px 20px rgba(23,32,28,.12);
      white-space:nowrap;
      opacity:0;
      pointer-events:none;
    }}
    .route-hotspot-label[data-placement="above"] {{
      top:auto;
      bottom:calc(50% + 20px);
    }}
    .route-hotspot:hover .route-hotspot-label,
    .route-hotspot:focus-visible .route-hotspot-label,
    .route-hotspot[data-active="true"] .route-hotspot-label {{ opacity:1; }}
    .route-hotspot[data-active="true"]::before {{
      border-color:var(--signal);
      background:#fff3ef;
    }}
    .hud {{
      position:absolute;
      top:18px;
      left:18px;
      right:18px;
      display:flex;
      align-items:flex-start;
      justify-content:space-between;
      gap:12px;
      pointer-events:none;
    }}
    .title-card, .hint-pill {{
      border:1px solid var(--line);
      border-radius:6px;
      background:rgba(255,255,255,.84);
      backdrop-filter:blur(14px);
      box-shadow:0 10px 28px rgba(23,32,28,.1);
    }}
    .title-card {{ padding:14px 16px; max-width:min(390px,70vw); }}
    h1 {{ margin:0; font-size:30px; line-height:1.05; letter-spacing:0; }}
    .title-card p {{ margin:8px 0 0; color:var(--muted); font-size:14px; line-height:1.35; }}
    .hint-pill {{ padding:10px 13px; color:var(--muted); font-size:13px; white-space:nowrap; }}
    .viewer-actions {{
      position:absolute;
      left:18px;
      bottom:18px;
      display:flex;
      gap:2px;
      padding:4px;
      max-width:calc(100% - 36px);
      border:1px solid var(--line);
      border-radius:6px;
      background:rgba(255,255,255,.88);
      box-shadow:0 10px 28px rgba(23,32,28,.12);
      backdrop-filter:blur(14px);
      z-index:2;
    }}
    .capture-route-card {{
      position:absolute;
      right:24px;
      bottom:24px;
      display:none;
      min-width:min(320px,56vw);
      padding:14px 16px;
      border:1px solid rgba(255,255,255,.2);
      border-radius:6px;
      background:rgba(23,32,28,.88);
      color:#fbf6ef;
      backdrop-filter:blur(18px);
      box-shadow:0 18px 48px rgba(23,32,28,.24);
      z-index:2;
    }}
    .capture-route-kicker {{
      display:block;
      font-size:12px;
      font-weight:700;
      letter-spacing:0;
      text-transform:uppercase;
      color:rgba(251,246,239,.72);
    }}
    .capture-route-label {{
      display:block;
      margin-top:8px;
      font-size:32px;
      line-height:1;
      letter-spacing:0;
    }}
    .capture-route-progress {{
      display:block;
      margin-top:8px;
      font-size:13px;
      color:rgba(251,246,239,.78);
    }}
    .viewer-chip {{
      border:0;
      border-radius:4px;
      background:transparent;
      color:var(--ink);
      min-height:44px;
      min-width:44px;
      padding:0 14px;
      font:inherit;
      font-size:13px;
      font-weight:600;
      box-shadow:none;
      cursor:pointer;
    }}
    .viewer-chip:hover {{
      background:#f0f4f2;
    }}
    .viewer-chip[data-active="true"] {{
      background:var(--accent-soft);
      color:var(--accent);
    }}
    .route-buttons {{
      display:flex;
      flex-wrap:wrap;
      gap:8px;
    }}
    .route-button {{
      border:1px solid var(--line);
      border-radius:4px;
      background:var(--surface);
      color:var(--ink);
      min-height:44px;
      min-width:44px;
      padding:0 12px;
      font:inherit;
      font-size:13px;
      font-weight:600;
      cursor:pointer;
    }}
    .route-button[data-active="true"] {{
      border-color:rgba(20,107,93,.45);
      background:var(--accent-soft);
      color:var(--accent);
    }}
    button:focus-visible {{ outline:3px solid rgba(20,107,93,.38); outline-offset:2px; }}
    aside {{
      display:flex;
      flex-direction:column;
      gap:10px;
      min-width:0;
      height:100vh;
      overflow-y:auto;
      padding:12px;
      border-left:1px solid var(--line);
      background:#f4f7f5;
    }}
    .panel {{ border:1px solid var(--line); border-radius:6px; background:var(--panel); padding:14px; box-shadow:0 6px 18px rgba(23,32,28,.05); }}
    .panel-head {{ display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom:10px; }}
    .panel-head p, .panel-head span {{ margin:0; color:var(--muted); font-size:13px; }}
    .panel-head p {{ color:var(--ink); font-weight:700; }}
    .facts {{ display:grid; grid-template-columns:repeat(3,1fr); gap:8px; }}
    .facts div {{ border:0; border-left:1px solid var(--line); border-radius:0; padding:8px 10px; background:transparent; }}
    .facts div:first-child {{ border-left:0; }}
    .facts b {{ display:block; font-size:20px; line-height:1; letter-spacing:0; }}
    .facts span {{ display:block; margin-top:5px; color:var(--muted); font-size:12px; }}
    .style-pill {{ display:inline-flex; margin-top:10px; padding:8px 10px; border-radius:4px; background:var(--accent-soft); color:var(--accent); font-size:13px; }}
    .floorplan, .photos img {{ width:100%; border:1px solid var(--line); border-radius:4px; object-fit:cover; background:white; }}
    .floorplan {{ aspect-ratio:4/3; }}
    .floorplan-map {{ position:relative; overflow:visible; }}
    .floorplan-route-overlay {{
      position:absolute;
      inset:0;
      width:100%;
      height:100%;
      overflow:visible;
      pointer-events:none;
    }}
    .floorplan-route-overlay polyline,
    .floorplan-route-overlay line {{
      fill:none;
      stroke:rgba(20,107,93,.48);
      stroke-width:1.5;
      stroke-linecap:round;
      stroke-linejoin:round;
      vector-effect:non-scaling-stroke;
    }}
    .floorplan-route-overlay line {{ stroke:rgba(95,107,101,.44); stroke-dasharray:3 3; }}
    .floorplan-route-overlay circle {{ fill:var(--signal); vector-effect:non-scaling-stroke; }}
    .floorplan-stop {{
      position:absolute;
      transform:translate(-50%,-50%);
      width:44px;
      height:44px;
      border:0;
      border-radius:50%;
      background:transparent;
      color:var(--accent);
      display:grid;
      place-items:center;
      font:inherit;
      font-size:12px;
      font-weight:700;
      cursor:pointer;
      overflow:visible;
    }}
    .floorplan-stop::before {{
      content:"";
      position:absolute;
      inset:8px;
      border:1px solid rgba(20,107,93,.4);
      border-radius:50%;
      background:rgba(255,255,255,.96);
      box-shadow:0 7px 18px rgba(23,32,28,.13);
    }}
    .floorplan-stop:hover::before,
    .floorplan-stop[data-active="true"]::before {{
      border-color:var(--signal);
      background:#fff3ef;
    }}
    .floorplan-stop-index {{ position:relative; z-index:1; }}
    .floorplan-stop-label {{
      position:absolute;
      left:50%;
      top:calc(50% + 20px);
      transform:translateX(-50%);
      z-index:2;
      padding:5px 8px;
      border:1px solid var(--line);
      border-radius:4px;
      background:rgba(255,255,255,.97);
      color:var(--ink);
      font-size:11px;
      font-weight:600;
      line-height:1;
      white-space:nowrap;
      box-shadow:0 8px 20px rgba(23,32,28,.1);
      opacity:0;
      pointer-events:none;
    }}
    .floorplan-stop:hover .floorplan-stop-label,
    .floorplan-stop:focus-visible .floorplan-stop-label,
    .floorplan-stop[data-active="true"] .floorplan-stop-label {{
      opacity:1;
    }}
    .floorplan-stop[data-active="true"] .floorplan-stop-label {{
      border-color:rgba(200,95,67,.4);
      background:#fff3ef;
      color:#8f3f2b;
    }}
    .floorplan-note {{ margin-top:10px; }}
    .photos {{ display:grid; grid-template-columns:repeat(2,1fr); gap:8px; }}
    .photos img {{ aspect-ratio:1; }}
    .note {{ margin:0; color:var(--muted); font-size:13px; line-height:1.4; }}
    html[data-capture-mode="true"] body {{ background:#dfe7e3; }}
    html[data-capture-mode="true"] main {{
      display:block;
      min-height:100vh;
      padding:0;
    }}
    html[data-capture-mode="true"] aside {{
      display:none;
    }}
    html[data-capture-mode="true"] .stage {{
      min-height:100vh;
    }}
    html[data-capture-mode="true"] .viewport {{
      height:100vh;
      min-height:100vh;
      border:none;
      border-radius:0;
      box-shadow:none;
    }}
    html[data-capture-mode="true"] .stage-hotspots,
    html[data-capture-mode="true"] .viewer-actions,
    html[data-capture-mode="true"] .hint-pill {{
      display:none;
    }}
    html[data-capture-mode="true"] .capture-route-card {{
      display:block;
    }}
    @media (max-width: 880px) {{
      main {{ display:block; padding:0; }}
      .stage {{ min-height:0; }}
      .viewport {{ height:70svh; min-height:500px; border-radius:0; }}
      aside {{
        height:auto;
        overflow:visible;
        margin:0;
        padding:10px;
        border-top:1px solid var(--line);
        border-left:0;
      }}
      .hud {{ top:12px; left:12px; right:12px; }}
      .hint-pill {{ display:none; }}
      .viewer-actions {{
        left:12px;
        right:12px;
        bottom:12px;
        display:grid;
        grid-template-columns:repeat(2,minmax(0,1fr));
        max-width:none;
      }}
      .title-card {{ max-width:calc(100vw - 24px); padding:12px 13px; }}
      h1 {{ font-size:27px; }}
      .title-card p {{ font-size:13px; }}
      .floorplan-stop-label {{ font-size:10px; }}
    }}
  </style>
</head>
<body>
<main>
  <section class="stage">
    <div class="viewport" id="viewport" aria-label="Interactive 3D layout preview" aria-busy="true"></div>
    <p class="sr-only" id="viewer-live-status" role="status" aria-live="polite" aria-atomic="true">Loading interactive 3D layout preview.</p>
    <div class="viewer-fallback" id="viewer-fallback" role="alert" aria-live="assertive" aria-atomic="true" hidden>
      <strong>3D preview is unavailable</strong>
      <p>Your browser could not start the interactive view. Use the floorplan and listing photos to review the layout.</p>
    </div>
    <div class="stage-hotspots" id="stage-hotspots" aria-label="Room hotspots"></div>
    <div class="hud">
      <div class="title-card">
        <h1>Styled 3D reconstruction</h1>
        <p>Explore generated furnishings in the floorplan-derived layout before deciding whether to visit.</p>
      </div>
      <div class="hint-pill">Drag, zoom, then inspect the plan beside it.</div>
    </div>
    <div class="viewer-actions">
      <button class="viewer-chip" id="view-overview" type="button" aria-pressed="false">Overview</button>
      <button class="viewer-chip" id="view-dollhouse" type="button" aria-pressed="false">Dollhouse</button>
      <button class="viewer-chip" id="view-inside" type="button" aria-pressed="false">Room view</button>
      <button class="viewer-chip" id="view-guided-route" type="button" aria-pressed="false">Guide me</button>
      <button class="viewer-chip" id="view-floorplan-reference" type="button" aria-pressed="false">Plan overlay</button>
    </div>
    <div class="capture-route-card" id="capture-route-card" hidden>
      <span class="capture-route-kicker" id="capture-route-kicker">Layout flythrough</span>
      <strong class="capture-route-label" id="capture-route-label">Layout overview</strong>
      <span class="capture-route-progress" id="capture-route-progress">Planning preview</span>
    </div>
  </section>
  <aside>
    <section class="panel">
      <div class="panel-head">
        <p>Styled 3D reconstruction</p>
        <span>generated</span>
      </div>
      <p class="note">Generated planning reconstruction built from {source_description}; it is not a captured or measured tour. Confirm dimensions and finishes at the viewing.</p>
      {f'<span class="style-pill">{style_copy}</span>' if style_copy else ''}
    </section>
    <section class="panel facts" aria-label="Approximate room dimensions">
      <div><b>{width_m}</b><span>m wide</span></div>
      <div><b>{depth_m}</b><span>m deep</span></div>
      <div><b>{height_m}</b><span>m high</span></div>
    </section>
    {route_section}
    <section class="panel">
      <div class="panel-head">
        <p>Floorplan</p>
        <span>{'route map' if floorplan_stop_markup else 'source'}</span>
      </div>
      <div class="floorplan-map">
        <img class="floorplan" src="{floorplan_relpath}" alt="Floorplan">
        {floorplan_route_overlay}
        {floorplan_stop_markup}
      </div>
      {'<p class="note floorplan-note">Tap a numbered stop on the plan to move through the route.</p>' if floorplan_stop_markup else ''}
    </section>
    {photo_section}
  </aside>
</main>
<script type="module">
import * as THREE from "./{three_relpath}";
import {{ OrbitControls }} from "./{orbit_controls_relpath}";

const viewport = document.getElementById("viewport");
const hotspotLayer = document.getElementById("stage-hotspots");
const viewerLiveStatus = document.getElementById("viewer-live-status");
const viewerFallback = document.getElementById("viewer-fallback");
const overviewButton = document.getElementById("view-overview");
const dollhouseButton = document.getElementById("view-dollhouse");
const insideButton = document.getElementById("view-inside");
const guideButton = document.getElementById("view-guided-route");
const floorplanReferenceButton = document.getElementById("view-floorplan-reference");
const wallRectangles = {_html_script_safe_json(wall_rectangles)};
const walkableScene = {_html_script_safe_json(walkable_scene)};
const styleScene = {_html_script_safe_json(style_scene)};
const stylePalette = styleScene && typeof styleScene.material_palette === "object" ? styleScene.material_palette : {{}};
const styleInstances = Array.isArray(styleScene.instances) ? styleScene.instances.filter((row) => row && typeof row === "object") : [];
const requiredStyleCues = Array.isArray(styleScene.required_cues) ? styleScene.required_cues.map((value) => String(value || "")).filter(Boolean) : [];
const styleKey = String(styleScene.style_id || "");
const styleEvidenceDigest = String(styleScene.scene_signature || "");
const styleSceneContractReady =
  String(styleScene.contract_version || "") === {_html_script_safe_json(STYLE_SCENE_CONTRACT_VERSION)}
  && String(styleScene.evidence_status || "") === "ready"
  && styleScene.materially_applied === true
  && String(styleScene.floorplan_display_mode || "") === {_html_script_safe_json(FLOORPLAN_DISPLAY_MODE)}
  && Boolean(styleKey)
  && Boolean(String(styleScene.style_signature || ""))
  && Boolean(styleEvidenceDigest)
  && styleInstances.length >= requiredStyleCues.length;
function styleColor(key, fallback) {{
  const value = String(stylePalette[key] || fallback || "").trim();
  return /^#[0-9a-f]{{6}}$/i.test(value) ? value : fallback;
}}
const routeStops = Array.isArray(walkableScene.route) ? walkableScene.route.filter((stop) => stop && typeof stop === "object") : [];
const maxStagedRouteStops = 12;
const stagedRouteStops = routeStops.slice(0, maxStagedRouteStops);
const photoPanelSpecs = {_html_script_safe_json(photo_reference_panels)};
const routeButtons = Array.from(document.querySelectorAll(".route-button"));
const floorplanStopButtons = Array.from(document.querySelectorAll(".floorplan-stop"));
const roomWidth = {_html_script_safe_json(width_m)};
const roomDepth = {_html_script_safe_json(depth_m)};
const roomHeight = {_html_script_safe_json(height_m)};
const routeQuery = new URLSearchParams(window.location.search);
const captureMode = routeQuery.get("capture") === "1";
const guidedQueryEnabled = routeQuery.get("guided") === "1";
const shellProbeMode = routeQuery.get("shell_probe") === "1";
const reducedMotionMedia = window.matchMedia("(prefers-reduced-motion: reduce)");
let prefersReducedMotion = Boolean(reducedMotionMedia.matches);
if (captureMode) {{
  document.documentElement.dataset.captureMode = "true";
}}
const captureRouteCard = document.getElementById("capture-route-card");
const captureRouteKicker = document.getElementById("capture-route-kicker");
const captureRouteLabel = document.getElementById("capture-route-label");
const captureRouteProgress = document.getElementById("capture-route-progress");

function announceViewerState(message) {{
  if (viewerLiveStatus) {{
    viewerLiveStatus.textContent = String(message || "").trim();
  }}
}}

function webglSupported() {{
  try {{
    const probe = document.createElement("canvas");
    return Boolean(probe.getContext("webgl2") || probe.getContext("webgl"));
  }} catch (_error) {{
    return false;
  }}
}}

function showViewerFallback() {{
  document.documentElement.dataset.viewerStatus = "unavailable";
  viewport.dataset.renderStatus = "unavailable";
  viewport.setAttribute("aria-busy", "false");
  hotspotLayer && (hotspotLayer.hidden = true);
  if (viewerFallback) {{
    viewerFallback.hidden = false;
  }}
  document.querySelectorAll(".viewer-chip, .route-button, .floorplan-stop").forEach((button) => {{
    button.disabled = true;
  }});
  announceViewerState("The interactive 3D preview is unavailable. Use the floorplan and listing photos instead.");
  window.__pqReconstructionDebug = {{
    getRenderMetrics: () => ({{
      ready: false,
      reason: "webgl_unavailable",
      routeStopCount: Number(routeStops.length || 0),
      guidedQueryEnabled: Boolean(guidedQueryEnabled),
      guidedRouteActive: false,
      prefersReducedMotion: Boolean(prefersReducedMotion),
      frameCount: 0,
      styleKey,
      styleEvidenceDigest,
      styledObjectCount: 0,
      visibleStyledObjectCount: 0,
      projectedStyledCoveragePct: 0,
      floorplanLayerState: "off",
    }}),
  }};
}}

const scene = new THREE.Scene();
scene.background = new THREE.Color(styleColor("background", "#e8eeeb"));
scene.fog = new THREE.Fog(styleColor("background", "#e8eeeb"), 13, 34);
let renderFrameCount = 0;
let animationFrameId = 0;
let viewerDisposed = false;
let contextLost = false;
let resizeObserver = null;

const camera = new THREE.PerspectiveCamera(48, 1, 0.1, 100);
let renderer = null;
if (webglSupported()) {{
  try {{
    renderer = new THREE.WebGLRenderer({{ antialias: true, alpha: true, preserveDrawingBuffer: true }});
  }} catch (_error) {{
    renderer = null;
  }}
}}
if (!renderer) {{
  showViewerFallback();
}}
if (renderer) {{
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.08;
const constrainedDevice =
  Number(navigator.deviceMemory || 8) <= 4
  || window.matchMedia("(max-width: 520px)").matches;
const renderQualityTier = constrainedDevice ? "balanced" : "high";
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, constrainedDevice ? 1.5 : 2));
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
renderer.domElement.setAttribute("role", "img");
renderer.domElement.setAttribute("aria-label", "Interactive 3D layout preview. Use the view and room route controls to navigate.");
viewport.appendChild(renderer.domElement);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.enablePan = true;
controls.maxPolarAngle = Math.PI * 0.49;
controls.minPolarAngle = Math.PI * 0.14;
controls.minDistance = Math.max(roomWidth, roomDepth) * 0.32;
controls.maxDistance = Math.max(roomWidth, roomDepth) * 2.4;

const hemisphereLight = new THREE.HemisphereLight(0xfffbf5, 0xd7c4aa, 1.35);
scene.add(hemisphereLight);
const keyLight = new THREE.DirectionalLight(0xffffff, 1.05);
keyLight.position.set(roomWidth * 0.7, roomHeight * 3.2, roomDepth * 0.9);
keyLight.castShadow = true;
keyLight.shadow.mapSize.width = constrainedDevice ? 1024 : 2048;
keyLight.shadow.mapSize.height = constrainedDevice ? 1024 : 2048;
keyLight.shadow.camera.near = 0.1;
keyLight.shadow.camera.far = 40;
keyLight.shadow.bias = -0.00015;
keyLight.shadow.normalBias = 0.025;
scene.add(keyLight);
const windowLight = new THREE.DirectionalLight(0xffead1, 0.62);
windowLight.position.set(-roomWidth * 0.8, roomHeight * 1.8, -roomDepth * 0.65);
scene.add(windowLight);
const interiorFill = new THREE.PointLight(0xfff3df, 0.72, Math.max(roomWidth, roomDepth) * 1.8, 1.8);
interiorFill.position.set(0, roomHeight * 0.82, 0);
interiorFill.castShadow = false;
scene.add(interiorFill);

const textureLoader = new THREE.TextureLoader();
const floorTexture = textureLoader.load({_html_script_safe_json(str(dict(manifest.get("floorplan") or {}).get("relpath") or "source-floorplan.jpg"))});
floorTexture.colorSpace = THREE.SRGBColorSpace;
floorTexture.anisotropy = 8;
const floorTextureCrop = {_html_script_safe_json(floor_texture_crop)};
floorTexture.offset.set(Number(floorTextureCrop.offset_x || 0), Number(floorTextureCrop.offset_y || 0));
floorTexture.repeat.set(Number(floorTextureCrop.repeat_x || 1), Number(floorTextureCrop.repeat_y || 1));
floorTexture.needsUpdate = true;
const floor = new THREE.Mesh(
  new THREE.PlaneGeometry(roomWidth, roomDepth),
  new THREE.MeshStandardMaterial({{
    color: styleColor("floor", "#f8f4eb"),
    map: null,
    roughness: 0.96,
    metalness: 0.0,
  }})
);
floor.rotation.x = -Math.PI / 2;
floor.receiveShadow = true;
scene.add(floor);
const apartmentPlinth = new THREE.Mesh(
  new THREE.BoxGeometry(roomWidth * 1.015, 0.12, roomDepth * 1.015),
  new THREE.MeshPhysicalMaterial({{
    color: styleColor("timber", "#d8c8b3"),
    roughness: 0.76,
    metalness: 0.0,
    clearcoat: 0.08,
    clearcoatRoughness: 0.8,
  }})
);
apartmentPlinth.position.y = -0.065;
apartmentPlinth.castShadow = true;
apartmentPlinth.receiveShadow = true;
scene.add(apartmentPlinth);
let floorplanLayerActive = false;
function setFloorplanLayer(active) {{
  floorplanLayerActive = Boolean(active);
  floor.material.map = floorplanLayerActive ? floorTexture : null;
  floor.material.color.set(styleColor("floor", "#f8f4eb"));
  floor.material.needsUpdate = true;
  if (floorplanReferenceButton) {{
    floorplanReferenceButton.dataset.active = floorplanLayerActive ? "true" : "false";
    floorplanReferenceButton.setAttribute("aria-pressed", floorplanLayerActive ? "true" : "false");
  }}
  return floorplanLayerActive;
}}
if (floorplanReferenceButton) {{
  floorplanReferenceButton.addEventListener("click", () => setFloorplanLayer(!floorplanLayerActive));
}}
setFloorplanLayer(false);

const wallMaterial = new THREE.MeshStandardMaterial({{
  color: styleColor("wall", "#f4efe4"),
  roughness: 0.88,
  metalness: 0.02,
  clearcoat: 0.08,
  clearcoatRoughness: 0.82,
  side: THREE.DoubleSide,
  transparent: true,
  opacity: 1.0,
}});
const wallEdgeMaterial = new THREE.LineBasicMaterial({{
  color: styleColor("edge", "#c2ab83"),
  transparent: true,
  opacity: 0.62,
}});
const wallMeshes = [];
const wallEdgeMeshes = [];
const wallMeshPairs = [];
const overviewCutawayBoundaryX = Math.max(0.22, Math.min(roomWidth * 0.08, 0.48));
const overviewCutawayBoundaryZ = Math.max(0.22, Math.min(roomDepth * 0.08, 0.48));
for (const wall of wallRectangles) {{
  const wallWidth = Number(wall.width || 0);
  const wallDepth = Number(wall.depth || 0);
  const centerX = Number(wall.center_x || 0);
  const centerZ = Number(wall.center_z || 0);
  const rotationY = Number(wall.rotation_y || 0);
  const extentX = (Math.abs(Math.cos(rotationY)) * wallWidth * 0.5) + (Math.abs(Math.sin(rotationY)) * wallDepth * 0.5);
  const extentZ = (Math.abs(Math.sin(rotationY)) * wallWidth * 0.5) + (Math.abs(Math.cos(rotationY)) * wallDepth * 0.5);
  const lengthAxisRotation = rotationY + (wallDepth > wallWidth ? Math.PI * 0.5 : 0);
  const runsMostlyEastWest = Math.abs(Math.cos(lengthAxisRotation)) >= Math.abs(Math.sin(lengthAxisRotation));
  // A wall endpoint touching a boundary does not make the whole wall part of
  // that shell. Cut away only walls whose thickness-normal sits on east/south.
  const touchesEastShell = !runsMostlyEastWest && centerX + extentX >= (roomWidth * 0.5) - overviewCutawayBoundaryX;
  const touchesSouthShell = runsMostlyEastWest && centerZ + extentZ >= (roomDepth * 0.5) - overviewCutawayBoundaryZ;
  const cutawayEligible = Boolean(touchesEastShell || touchesSouthShell);
  const mesh = new THREE.Mesh(
    new THREE.BoxGeometry(wall.width, roomHeight, wall.depth),
    wallMaterial,
  );
  mesh.position.set(centerX, roomHeight / 2, centerZ);
  mesh.rotation.y = rotationY;
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  mesh.userData.baseCenterY = roomHeight / 2;
  mesh.userData.cutawayEligible = cutawayEligible;
  wallMeshes.push(mesh);
  scene.add(mesh);
  const edges = new THREE.LineSegments(new THREE.EdgesGeometry(mesh.geometry), wallEdgeMaterial);
  edges.position.copy(mesh.position);
  edges.rotation.copy(mesh.rotation);
  edges.userData.baseCenterY = roomHeight / 2;
  edges.userData.cutawayEligible = cutawayEligible;
  wallEdgeMeshes.push(edges);
  scene.add(edges);
  wallMeshPairs.push({{ mesh, edges, cutawayEligible }});
}}

const routeMarkerGroup = new THREE.Group();
scene.add(routeMarkerGroup);
const routeMarkers = [];
const routeHotspots = [];
const routeMarkerMaterial = new THREE.MeshStandardMaterial({{
  color: 0xa77c2b,
  roughness: 0.36,
  metalness: 0.04,
}});
const routeLinePoints = [];
for (const stop of routeStops) {{
  const focus = stop.focus && typeof stop.focus === "object" ? stop.focus : null;
  if (!focus) continue;
  const marker = new THREE.Mesh(
    new THREE.CylinderGeometry(0.16, 0.16, 0.028, 28),
    routeMarkerMaterial.clone(),
  );
  marker.position.set(Number(focus.x || 0), 0.016, Number(focus.z || 0));
  marker.receiveShadow = true;
  routeMarkerGroup.add(marker);
  routeMarkers.push(marker);
  routeLinePoints.push(new THREE.Vector3(Number(focus.x || 0), 0.018, Number(focus.z || 0)));
  if (hotspotLayer) {{
    const button = document.createElement("button");
    button.type = "button";
    button.className = "route-hotspot";
    button.dataset.routeIndex = String(routeHotspots.length);
    const hotspotLabel = String(stop.label || stop.room || stop.name || `Stop ${{routeHotspots.length + 1}}`);
    button.dataset.label = hotspotLabel;
    button.setAttribute("aria-label", `Go to ${{hotspotLabel}}`);
    button.setAttribute("aria-current", "false");
    button.textContent = String(routeHotspots.length + 1);
    const label = document.createElement("span");
    label.className = "route-hotspot-label";
    label.textContent = hotspotLabel;
    button.appendChild(label);
    button.addEventListener("click", () => setRouteView(Number(button.dataset.routeIndex || 0)));
    hotspotLayer.appendChild(button);
    routeHotspots.push({{
      button,
      label,
      focus: new THREE.Vector3(Number(focus.x || 0), Number(focus.y || roomHeight * 0.5), Number(focus.z || 0)),
    }});
  }}
}}
if (routeLinePoints.length >= 2) {{
  const routeLine = new THREE.Line(
    new THREE.BufferGeometry().setFromPoints(routeLinePoints),
    new THREE.LineBasicMaterial({{
      color: 0xc2ab83,
      transparent: true,
      opacity: 0.7,
    }})
  );
  routeMarkerGroup.add(routeLine);
}}

const stagingGroup = new THREE.Group();
scene.add(stagingGroup);
const semanticStagingGroup = new THREE.Group();
semanticStagingGroup.name = "generated-semantic-staging";
stagingGroup.add(semanticStagingGroup);
const styleStagingGroup = new THREE.Group();
styleStagingGroup.name = "generated-style-staging";
stagingGroup.add(styleStagingGroup);
const stagingObjects = [];
const semanticStagingObjects = [];
const stagingDetailObjects = [];
const stagingMaterials = {{
  textile: new THREE.MeshStandardMaterial({{ color: styleColor("textile", "#b58f73"), roughness: 0.86, metalness: 0.0 }}),
  paleTextile: new THREE.MeshStandardMaterial({{ color: styleColor("paleTextile", "#e4dacd"), roughness: 0.9, metalness: 0.0 }}),
  timber: new THREE.MeshStandardMaterial({{ color: styleColor("timber", "#9b7650"), roughness: 0.72, metalness: 0.02 }}),
  stone: new THREE.MeshStandardMaterial({{ color: styleColor("stone", "#d7d0c3"), roughness: 0.82, metalness: 0.01 }}),
  accent: new THREE.MeshStandardMaterial({{ color: styleColor("accent", "#a77c2b"), roughness: 0.58, metalness: 0.03 }}),
  foliage: new THREE.MeshStandardMaterial({{ color: styleColor("foliage", "#6f8561"), roughness: 0.92, metalness: 0.0 }}),
  rattan: new THREE.MeshStandardMaterial({{ color: styleColor("rattan", "#b78249"), roughness: 0.76, metalness: 0.0 }}),
  darkMetal: new THREE.MeshStandardMaterial({{ color: styleColor("metal", "#b59445"), roughness: 0.32, metalness: 0.72 }}),
}};

function stagingKind(stop) {{
  const raw = String(stop?.kind || stop?.label || stop?.room || stop?.name || "").toLowerCase();
  if (raw.includes("kitchen") || raw.includes("kuche") || raw.includes("kueche") || raw.includes("küche")) return "kitchen";
  if (raw.includes("bed") || raw.includes("schlaf")) return "bedroom";
  if (raw.includes("bath") || raw.includes("bad") || raw.includes("wc") || raw.includes("toilet")) return "bath";
  if (raw.includes("balcony") || raw.includes("terrace") || raw.includes("balkon") || raw.includes("terrasse") || raw.includes("loggia")) return "outdoor";
  if (raw.includes("entry") || raw.includes("hall") || raw.includes("foyer") || raw.includes("flur") || raw.includes("vorraum")) return "entry";
  if (raw.includes("dining") || raw.includes("esszimmer")) return "dining";
  if (raw.includes("living") || raw.includes("wohn")) return "living";
  return "generic";
}}

function addStagingBox(group, name, dimensions, position, material, rotationY = 0) {{
  const mesh = new THREE.Mesh(
    new THREE.BoxGeometry(
      Math.max(0.04, Number(dimensions.x || 0.2)),
      Math.max(0.04, Number(dimensions.y || 0.2)),
      Math.max(0.04, Number(dimensions.z || 0.2)),
    ),
    material,
  );
  mesh.name = String(name || "generated-staging-object");
  mesh.position.set(Number(position.x || 0), Number(position.y || 0), Number(position.z || 0));
  mesh.rotation.y = Number(rotationY || 0);
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  group.add(mesh);
  stagingObjects.push(mesh);
  semanticStagingObjects.push(mesh);
  return mesh;
}}

function addStagingRug(group, dimensions, position, material) {{
  const rug = new THREE.Mesh(
    new THREE.PlaneGeometry(Math.max(0.2, Number(dimensions.x || 1)), Math.max(0.2, Number(dimensions.z || 1))),
    material,
  );
  rug.name = "generated-staging-rug";
  rug.rotation.x = -Math.PI / 2;
  rug.position.set(Number(position.x || 0), 0.024, Number(position.z || 0));
  rug.receiveShadow = true;
  group.add(rug);
  stagingObjects.push(rug);
  semanticStagingObjects.push(rug);
  return rug;
}}

function addStagingDetail(group, name, geometry, position, material, rotation = null) {{
  const mesh = new THREE.Mesh(geometry, material);
  mesh.name = String(name || "generated-staging-detail");
  mesh.position.set(Number(position.x || 0), Number(position.y || 0), Number(position.z || 0));
  if (rotation) {{
    mesh.rotation.set(Number(rotation.x || 0), Number(rotation.y || 0), Number(rotation.z || 0));
  }}
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  group.add(mesh);
  stagingObjects.push(mesh);
  stagingDetailObjects.push(mesh);
  return mesh;
}}

function addGeneratedStagingForStop(stop, index) {{
  const focus = stop?.focus && typeof stop.focus === "object" ? stop.focus : null;
  if (!focus) return null;
  const group = new THREE.Group();
  const baseX = Math.max(-(roomWidth * 0.38), Math.min(roomWidth * 0.38, Number(focus.x || 0)));
  const baseZ = Math.max(-(roomDepth * 0.38), Math.min(roomDepth * 0.38, Number(focus.z || 0)));
  group.position.set(baseX, 0, baseZ);
  group.rotation.y = (Number(index || 0) % 2 === 0 ? -0.22 : 0.18);
  const kind = stagingKind(stop);
  if (kind === "living" || kind === "generic") {{
    addStagingRug(group, {{ x: 1.62, z: 1.1 }}, {{ x: 0.05, z: 0.02 }}, stagingMaterials.paleTextile);
    addStagingBox(group, "generated-sofa-seat", {{ x: 1.24, y: 0.28, z: 0.52 }}, {{ x: -0.22, y: 0.14, z: -0.22 }}, stagingMaterials.textile);
    addStagingBox(group, "generated-sofa-back", {{ x: 1.24, y: 0.48, z: 0.12 }}, {{ x: -0.22, y: 0.38, z: -0.53 }}, stagingMaterials.textile);
    addStagingBox(group, "generated-sofa-arm-left", {{ x: 0.12, y: 0.38, z: 0.58 }}, {{ x: -0.84, y: 0.25, z: -0.22 }}, stagingMaterials.textile);
    addStagingBox(group, "generated-sofa-arm-right", {{ x: 0.12, y: 0.38, z: 0.58 }}, {{ x: 0.40, y: 0.25, z: -0.22 }}, stagingMaterials.textile);
    addStagingDetail(group, "generated-sofa-cushion-left", new THREE.SphereGeometry(0.22, 20, 14), {{ x: -0.52, y: 0.38, z: -0.24 }}, stagingMaterials.paleTextile, {{ x: 0, y: 0, z: -0.12 }});
    addStagingDetail(group, "generated-sofa-cushion-right", new THREE.SphereGeometry(0.22, 20, 14), {{ x: 0.08, y: 0.38, z: -0.24 }}, stagingMaterials.paleTextile, {{ x: 0, y: 0, z: 0.12 }});
    addStagingBox(group, "generated-coffee-table", {{ x: 0.72, y: 0.2, z: 0.42 }}, {{ x: 0.26, y: 0.1, z: 0.34 }}, stagingMaterials.timber);
    for (const [legX, legZ] of [[-0.28, -0.13], [0.28, -0.13], [-0.28, 0.13], [0.28, 0.13]]) {{
      addStagingDetail(group, "generated-coffee-table-leg", new THREE.CylinderGeometry(0.025, 0.025, 0.28, 12), {{ x: 0.26 + legX, y: 0.14, z: 0.34 + legZ }}, stagingMaterials.darkMetal);
    }}
    addStagingDetail(group, "generated-floor-lamp", new THREE.CylinderGeometry(0.035, 0.055, 1.05, 16), {{ x: 0.66, y: 0.525, z: -0.42 }}, stagingMaterials.darkMetal);
    addStagingDetail(group, "generated-floor-lamp-shade", new THREE.CylinderGeometry(0.16, 0.25, 0.28, 24), {{ x: 0.66, y: 1.08, z: -0.42 }}, stagingMaterials.paleTextile);
  }} else if (kind === "bedroom") {{
    addStagingRug(group, {{ x: 1.74, z: 1.22 }}, {{ x: 0.02, z: 0.02 }}, stagingMaterials.paleTextile);
    addStagingBox(group, "generated-bed-base", {{ x: 1.42, y: 0.34, z: 1.02 }}, {{ x: 0, y: 0.17, z: -0.02 }}, stagingMaterials.paleTextile);
    addStagingBox(group, "generated-bed-headboard", {{ x: 1.48, y: 0.72, z: 0.12 }}, {{ x: 0, y: 0.44, z: -0.62 }}, stagingMaterials.timber);
    addStagingBox(group, "generated-bed-pillow", {{ x: 0.64, y: 0.16, z: 0.22 }}, {{ x: -0.26, y: 0.44, z: -0.42 }}, stagingMaterials.stone);
    addStagingBox(group, "generated-bed-pillow-right", {{ x: 0.64, y: 0.16, z: 0.22 }}, {{ x: 0.38, y: 0.44, z: -0.42 }}, stagingMaterials.stone);
    addStagingBox(group, "generated-bed-throw", {{ x: 1.28, y: 0.055, z: 0.3 }}, {{ x: 0, y: 0.385, z: 0.28 }}, stagingMaterials.textile);
    addStagingBox(group, "generated-nightstand", {{ x: 0.34, y: 0.38, z: 0.34 }}, {{ x: -0.9, y: 0.19, z: -0.34 }}, stagingMaterials.timber);
    addStagingDetail(group, "generated-bedside-lamp", new THREE.CylinderGeometry(0.08, 0.12, 0.26, 18), {{ x: -0.9, y: 0.51, z: -0.34 }}, stagingMaterials.paleTextile);
  }} else if (kind === "kitchen" || kind === "dining") {{
    addStagingBox(group, "generated-kitchen-counter", {{ x: 1.48, y: 0.68, z: 0.42 }}, {{ x: -0.08, y: 0.34, z: -0.38 }}, stagingMaterials.stone);
    addStagingBox(group, "generated-kitchen-island", {{ x: 0.9, y: 0.42, z: 0.5 }}, {{ x: 0.28, y: 0.21, z: 0.28 }}, stagingMaterials.timber);
    addStagingBox(group, "generated-dining-surface", {{ x: 0.86, y: 0.18, z: 0.58 }}, {{ x: -0.48, y: 0.46, z: 0.36 }}, stagingMaterials.timber);
    addStagingBox(group, "generated-counter-splash", {{ x: 1.48, y: 0.42, z: 0.05 }}, {{ x: -0.08, y: 0.76, z: -0.59 }}, stagingMaterials.glass);
    addStagingDetail(group, "generated-kitchen-pendant", new THREE.CylinderGeometry(0.1, 0.18, 0.24, 20), {{ x: 0.28, y: 1.28, z: 0.28 }}, stagingMaterials.darkMetal);
    for (const chairZ of [0.1, 0.62]) {{
      addStagingBox(group, "generated-dining-chair-seat", {{ x: 0.34, y: 0.12, z: 0.34 }}, {{ x: -0.96, y: 0.34, z: chairZ }}, stagingMaterials.timber);
      addStagingBox(group, "generated-dining-chair-back", {{ x: 0.34, y: 0.52, z: 0.08 }}, {{ x: -1.1, y: 0.58, z: chairZ }}, stagingMaterials.timber);
    }}
  }} else if (kind === "bath") {{
    addStagingBox(group, "generated-bath-vanity", {{ x: 0.72, y: 0.52, z: 0.36 }}, {{ x: -0.22, y: 0.26, z: -0.18 }}, stagingMaterials.stone);
    addStagingBox(group, "generated-bath-tub", {{ x: 1.08, y: 0.36, z: 0.54 }}, {{ x: 0.28, y: 0.18, z: 0.28 }}, stagingMaterials.paleTextile);
    addStagingDetail(group, "generated-bath-basin", new THREE.CylinderGeometry(0.2, 0.24, 0.12, 24), {{ x: -0.22, y: 0.58, z: -0.18 }}, stagingMaterials.paleTextile);
    addStagingDetail(group, "generated-bath-mirror", new THREE.CircleGeometry(0.34, 32), {{ x: -0.22, y: 1.08, z: -0.39 }}, stagingMaterials.glass);
    addStagingDetail(group, "generated-bath-tap", new THREE.CylinderGeometry(0.025, 0.025, 0.28, 12), {{ x: -0.02, y: 0.72, z: -0.18 }}, stagingMaterials.darkMetal, {{ x: 0, y: 0, z: Math.PI * 0.5 }});
  }} else if (kind === "entry") {{
    addStagingBox(group, "generated-entry-bench", {{ x: 1.0, y: 0.28, z: 0.34 }}, {{ x: -0.1, y: 0.14, z: -0.18 }}, stagingMaterials.timber);
    addStagingBox(group, "generated-entry-console", {{ x: 0.78, y: 0.64, z: 0.22 }}, {{ x: 0.34, y: 0.32, z: 0.24 }}, stagingMaterials.stone);
    addStagingDetail(group, "generated-entry-mirror", new THREE.CircleGeometry(0.31, 32), {{ x: 0.34, y: 1.08, z: 0.12 }}, stagingMaterials.glass);
    addStagingDetail(group, "generated-entry-vase", new THREE.CylinderGeometry(0.08, 0.12, 0.3, 18), {{ x: 0.5, y: 0.79, z: 0.24 }}, stagingMaterials.accent);
  }} else if (kind === "outdoor") {{
    addStagingBox(group, "generated-outdoor-table", {{ x: 0.72, y: 0.26, z: 0.56 }}, {{ x: 0.0, y: 0.13, z: 0.08 }}, stagingMaterials.timber);
    addStagingBox(group, "generated-planter", {{ x: 0.34, y: 0.4, z: 0.34 }}, {{ x: -0.52, y: 0.2, z: -0.28 }}, stagingMaterials.accent);
    addStagingDetail(group, "generated-foliage", new THREE.IcosahedronGeometry(0.34, 2), {{ x: -0.52, y: 0.62, z: -0.28 }}, stagingMaterials.foliage);
    addStagingBox(group, "generated-outdoor-chair", {{ x: 0.42, y: 0.14, z: 0.42 }}, {{ x: 0.58, y: 0.28, z: 0.08 }}, stagingMaterials.timber);
    addStagingBox(group, "generated-outdoor-chair-back", {{ x: 0.42, y: 0.5, z: 0.1 }}, {{ x: 0.74, y: 0.53, z: 0.08 }}, stagingMaterials.timber);
  }}
  semanticStagingGroup.add(group);
  return group;
}}

const styledSceneObjects = [];
const observedStyleCues = new Set();

function addStyleMesh(group, geometry, material, name, position = {{ x: 0, y: 0, z: 0 }}) {{
  const mesh = new THREE.Mesh(geometry, material);
  mesh.name = String(name || "generated-style-mesh");
  mesh.position.set(Number(position.x || 0), Number(position.y || 0), Number(position.z || 0));
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  group.add(mesh);
  stagingObjects.push(mesh);
  return mesh;
}}

function styledRouteAnchor(routeIndex, focus) {{
  const routeInstances = styleInstances.filter(
    (candidate) => Number(candidate?.route_index ?? -1) === Number(routeIndex),
  );
  let minX = Infinity;
  let maxX = -Infinity;
  let minZ = Infinity;
  let maxZ = -Infinity;
  for (const candidate of routeInstances) {{
    const candidatePosition = candidate?.position && typeof candidate.position === "object"
      ? candidate.position
      : {{}};
    const candidateDimensions = candidate?.dimensions && typeof candidate.dimensions === "object"
      ? candidate.dimensions
      : {{}};
    const halfWidth = Math.max(0.08, Number(candidateDimensions.x || 0.4)) * 0.5;
    const halfDepth = Math.max(0.08, Number(candidateDimensions.z || 0.4)) * 0.5;
    const offsetX = Number(candidatePosition.x || 0);
    const offsetZ = Number(candidatePosition.z || 0);
    minX = Math.min(minX, offsetX - halfWidth);
    maxX = Math.max(maxX, offsetX + halfWidth);
    minZ = Math.min(minZ, offsetZ - halfDepth);
    maxZ = Math.max(maxZ, offsetZ + halfDepth);
  }}
  if (!Number.isFinite(minX) || !Number.isFinite(maxX)) {{
    minX = -0.2;
    maxX = 0.2;
  }}
  if (!Number.isFinite(minZ) || !Number.isFinite(maxZ)) {{
    minZ = -0.2;
    maxZ = 0.2;
  }}
  const limitX = roomWidth * 0.40;
  const limitZ = roomDepth * 0.40;
  const minimumAnchorX = -limitX - minX;
  const maximumAnchorX = limitX - maxX;
  const minimumAnchorZ = -limitZ - minZ;
  const maximumAnchorZ = limitZ - maxZ;
  const requestedX = Number(focus?.x || 0);
  const requestedZ = Number(focus?.z || 0);
  return {{
    x: minimumAnchorX <= maximumAnchorX
      ? Math.max(minimumAnchorX, Math.min(maximumAnchorX, requestedX))
      : 0,
    z: minimumAnchorZ <= maximumAnchorZ
      ? Math.max(minimumAnchorZ, Math.min(maximumAnchorZ, requestedZ))
      : 0,
  }};
}}

function addStyledSceneInstance(instance) {{
  const routeIndex = Number(instance?.route_index ?? -1);
  const routeStop = routeStops[routeIndex];
  const focus = routeStop?.focus && typeof routeStop.focus === "object" ? routeStop.focus : null;
  if (!focus) return null;
  const dimensions = instance?.dimensions && typeof instance.dimensions === "object" ? instance.dimensions : {{}};
  const position = instance?.position && typeof instance.position === "object" ? instance.position : {{}};
  const width = Math.max(0.08, Number(dimensions.x || 0.4));
  const height = Math.max(0.08, Number(dimensions.y || 0.4));
  const depth = Math.max(0.08, Number(dimensions.z || 0.4));
  const material = stagingMaterials[String(instance.material || "")] || stagingMaterials.accent;
  const group = new THREE.Group();
  group.name = `generated-style-instance-${{String(instance.id || styledSceneObjects.length + 1)}}`;
  group.userData.styleInstanceId = String(instance.id || "");
  group.userData.styleCue = String(instance.cue || "");
  group.userData.styleKey = styleKey;
  group.userData.routeIndex = routeIndex;
  const routeAnchor = styledRouteAnchor(routeIndex, focus);
  group.position.set(
    routeAnchor.x + Number(position.x || 0),
    0,
    routeAnchor.z + Number(position.z || 0),
  );
  group.rotation.y = (routeIndex % 2 === 0 ? -0.16 : 0.16);
  const shape = String(instance.shape || "box");
  const baseName = `generated-style-${{String(instance.kind || shape)}}`;
  if (shape === "rug") {{
    addStyleMesh(
      group,
      new THREE.BoxGeometry(width, 0.045, depth),
      material,
      baseName,
      {{ x: 0, y: 0.045, z: 0 }},
    );
    const borderThickness = Math.max(0.035, Math.min(width, depth) * 0.045);
    for (const [borderName, borderWidth, borderDepth, borderX, borderZ] of [
      ["north", width, borderThickness, 0, -((depth - borderThickness) * 0.5)],
      ["south", width, borderThickness, 0, (depth - borderThickness) * 0.5],
      ["west", borderThickness, depth, -((width - borderThickness) * 0.5), 0],
      ["east", borderThickness, depth, (width - borderThickness) * 0.5, 0],
    ]) {{
      addStyleMesh(
        group,
        new THREE.BoxGeometry(borderWidth, 0.018, borderDepth),
        stagingMaterials.paleTextile,
        `${{baseName}}-linen-border-${{borderName}}`,
        {{ x: borderX, y: 0.076, z: borderZ }},
      );
    }}
  }} else if (shape === "table" || shape === "bench") {{
    addStyleMesh(
      group,
      new THREE.BoxGeometry(width, Math.max(0.07, height * 0.14), depth),
      material,
      `${{baseName}}-surface`,
      {{ x: 0, y: height * 0.84, z: 0 }},
    );
    for (const [legX, legZ] of [[-0.40, -0.36], [0.40, -0.36], [-0.40, 0.36], [0.40, 0.36]]) {{
      addStyleMesh(
        group,
        new THREE.CylinderGeometry(width * 0.035, width * 0.045, height * 0.78, 12),
        material,
        `${{baseName}}-leg`,
        {{ x: width * legX, y: height * 0.39, z: depth * legZ }},
      );
    }}
    if (shape === "bench") {{
      addStyleMesh(
        group,
        new THREE.BoxGeometry(width, height * 0.52, depth * 0.12),
        material,
        `${{baseName}}-back`,
        {{ x: 0, y: height * 0.88, z: -depth * 0.44 }},
      );
    }}
  }} else if (shape === "cylinder") {{
    addStyleMesh(
      group,
      new THREE.CylinderGeometry(width * 0.5, width * 0.5, height, 28),
      material,
      baseName,
      {{ x: 0, y: height * 0.5, z: 0 }},
    );
  }} else if (shape === "plant") {{
    const plantCluster = [
      {{ x: -width * 0.22, z: width * 0.10, scale: 0.82, phase: 0.0 }},
      {{ x: width * 0.18, z: -width * 0.08, scale: 1.0, phase: 0.9 }},
      {{ x: width * 0.08, z: width * 0.24, scale: 0.70, phase: 1.8 }},
    ];
    plantCluster.forEach((plant, plantIndex) => {{
      const plantHeight = height * plant.scale;
      const potRadius = width * (plantIndex === 1 ? 0.24 : 0.19);
      addStyleMesh(
        group,
        new THREE.CylinderGeometry(potRadius * 0.78, potRadius, plantHeight * 0.24, 24),
        plantIndex === 2 ? stagingMaterials.stone : stagingMaterials.rattan,
        `${{baseName}}-planter-${{plantIndex + 1}}`,
        {{ x: plant.x, y: plantHeight * 0.12, z: plant.z }},
      );
      addStyleMesh(
        group,
        new THREE.CylinderGeometry(width * 0.025, width * 0.035, plantHeight * 0.64, 12),
        stagingMaterials.timber,
        `${{baseName}}-stem-${{plantIndex + 1}}`,
        {{ x: plant.x, y: plantHeight * 0.48, z: plant.z }},
      );
      for (let leafIndex = 0; leafIndex < 6; leafIndex += 1) {{
        const angle = plant.phase + ((leafIndex / 6) * Math.PI * 2);
        const radius = width * (leafIndex % 2 ? 0.25 : 0.16) * plant.scale;
        const leaf = addStyleMesh(
          group,
          new THREE.SphereGeometry(width * 0.15 * plant.scale, 16, 10),
          stagingMaterials.foliage,
          `${{baseName}}-plant-${{plantIndex + 1}}-leaf-${{leafIndex + 1}}`,
          {{
            x: plant.x + (Math.cos(angle) * radius),
            y: plantHeight * (0.56 + ((leafIndex % 3) * 0.11)),
            z: plant.z + (Math.sin(angle) * radius),
          }},
        );
        leaf.scale.set(1.08, 0.48, 0.72);
        leaf.rotation.y = angle;
      }}
    }});
  }} else if (shape === "rattan_chair") {{
    addStyleMesh(group, new THREE.BoxGeometry(width, height * 0.16, depth), material, `${{baseName}}-seat`, {{ x: 0, y: height * 0.46, z: 0 }});
    for (let slatIndex = 0; slatIndex < 6; slatIndex += 1) {{
      addStyleMesh(
        group,
        new THREE.CylinderGeometry(width * 0.018, width * 0.022, height * 0.50, 10),
        material,
        `${{baseName}}-woven-back-slat-${{slatIndex + 1}}`,
        {{
          x: width * (-0.38 + (slatIndex * 0.152)),
          y: height * 0.72,
          z: -depth * 0.44,
        }},
      );
    }}
    addStyleMesh(group, new THREE.BoxGeometry(width, height * 0.055, depth * 0.08), material, `${{baseName}}-back-rail`, {{ x: 0, y: height * 0.94, z: -depth * 0.44 }});
    for (const [legX, legZ] of [[-0.38, -0.34], [0.38, -0.34], [-0.38, 0.34], [0.38, 0.34]]) {{
      addStyleMesh(
        group,
        new THREE.CylinderGeometry(width * 0.035, width * 0.045, height * 0.44, 12),
        material,
        `${{baseName}}-leg`,
        {{ x: width * legX, y: height * 0.22, z: depth * legZ }},
      );
    }}
  }} else if (shape === "shelf") {{
    addStyleMesh(group, new THREE.BoxGeometry(width, height, depth * 0.18), material, `${{baseName}}-back`, {{ x: 0, y: height * 0.5, z: -depth * 0.40 }});
    for (let shelfIndex = 0; shelfIndex < 4; shelfIndex += 1) {{
      addStyleMesh(
        group,
        new THREE.BoxGeometry(width, height * 0.055, depth),
        shelfIndex === 1 ? stagingMaterials.accent : material,
        `${{baseName}}-shelf-${{shelfIndex + 1}}`,
        {{ x: 0, y: height * (0.08 + (shelfIndex * 0.28)), z: 0 }},
      );
    }}
  }} else if (shape === "vessels") {{
    for (let vesselIndex = 0; vesselIndex < 3; vesselIndex += 1) {{
      const vesselHeight = height * (0.52 + (vesselIndex * 0.18));
      addStyleMesh(
        group,
        new THREE.CylinderGeometry(width * (0.13 + (vesselIndex * 0.025)), width * (0.19 + (vesselIndex * 0.025)), vesselHeight, 24),
        material,
        `${{baseName}}-${{vesselIndex + 1}}`,
        {{ x: width * ((vesselIndex - 1) * 0.30), y: vesselHeight * 0.5, z: (vesselIndex % 2) * depth * 0.16 }},
      );
    }}
  }} else {{
    addStyleMesh(
      group,
      new THREE.BoxGeometry(width, height, depth),
      material,
      baseName,
      {{ x: 0, y: height * 0.5, z: 0 }},
    );
  }}
  styleStagingGroup.add(group);
  styledSceneObjects.push(group);
  observedStyleCues.add(String(instance.cue || ""));
  return group;
}}

stagedRouteStops.forEach((stop, index) => addGeneratedStagingForStop(stop, index));
styleInstances.forEach((instance) => addStyledSceneInstance(instance));
const styleEvidenceReady =
  styleSceneContractReady
  && styledSceneObjects.length === styleInstances.length
  && requiredStyleCues.every((cue) => observedStyleCues.has(cue));

function styledObjectsForRoute(routeIndex) {{
  const boundedRouteIndex = Math.max(0, Number(routeIndex || 0));
  return styledSceneObjects.filter(
    (object) => Number(object.userData?.routeIndex ?? -1) === boundedRouteIndex,
  );
}}

function styledBoundsForRoute(routeIndex) {{
  const bounds = new THREE.Box3();
  let hasBounds = false;
  for (const object of styledObjectsForRoute(routeIndex)) {{
    const objectBounds = new THREE.Box3().setFromObject(object);
    if (objectBounds.isEmpty()) continue;
    if (!hasBounds) {{
      bounds.copy(objectBounds);
      hasBounds = true;
    }} else {{
      bounds.union(objectBounds);
    }}
  }}
  return hasBounds ? bounds : null;
}}

function objectEffectivelyVisible(object) {{
  let current = object;
  while (current) {{
    if (current.visible === false) return false;
    current = current.parent;
  }}
  return true;
}}

function syncGeneratedSceneModeVisibility(mode = activeViewMode, routeIndex = activeRouteIndex) {{
  const roomMode = mode === "room";
  const boundedRouteIndex = routeStops.length
    ? Math.max(0, Math.min(Number(routeIndex ?? 0), routeStops.length - 1))
    : 0;
  semanticStagingGroup.visible = !roomMode;
  routeMarkerGroup.visible = !roomMode;
  styledSceneObjects.forEach((object) => {{
    object.visible =
      !roomMode
      || Number(object.userData?.routeIndex ?? -1) === boundedRouteIndex;
  }});
  if (hotspotLayer) {{
    hotspotLayer.style.opacity = roomMode ? "0" : (mode === "dollhouse" ? "0.78" : "1");
    hotspotLayer.style.pointerEvents = roomMode ? "none" : "auto";
  }}
}}

function buildPanelLabelTexture(label) {{
  const canvas = document.createElement("canvas");
  canvas.width = 720;
  canvas.height = 140;
  const context = canvas.getContext("2d");
  if (!context) return null;
  context.clearRect(0, 0, canvas.width, canvas.height);
  context.fillStyle = "rgba(255,252,245,0.98)";
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.strokeStyle = "rgba(194,171,131,0.78)";
  context.lineWidth = 4;
  context.strokeRect(2, 2, canvas.width - 4, canvas.height - 4);
  context.fillStyle = "#4b3923";
  context.font = "600 40px Aptos, ui-sans-serif, system-ui, sans-serif";
  context.textBaseline = "middle";
  context.fillText(String(label || "Room reference"), 28, canvas.height / 2, canvas.width - 56);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}}

const photoPanelGroup = new THREE.Group();
scene.add(photoPanelGroup);
const photoPanelCards = [];
const photoPanelPlanes = [];
let loadedPhotoTextureCount = 0;
const liveViewerState = {{
  ready: false,
  routeStopCount: Number(routeStops.length || 0),
  activeRouteIndex: -1,
  viewMode: "overview",
  prefersReducedMotion: Boolean(prefersReducedMotion),
  photoPanelCount: Number(photoPanelSpecs.length || 0),
  loadedPhotoTextureCount: 0,
  frameCount: 0,
  renderCalls: 0,
  renderTriangles: 0,
}};
for (const spec of photoPanelSpecs) {{
  if (!spec || typeof spec !== "object" || !spec.photo_relpath) continue;
  const panelGroup = new THREE.Group();
  panelGroup.position.set(
    Number(spec.position?.x || 0),
    Number(spec.position?.y || 1.5),
    Number(spec.position?.z || 0),
  );
  panelGroup.rotation.y = Number(spec.rotation_y || 0);

  const shadow = new THREE.Mesh(
    new THREE.PlaneGeometry(Number(spec.frame_width || 1.3) + 0.08, Number(spec.frame_height || 1.2) + 0.1),
    new THREE.MeshBasicMaterial({{
      color: 0x000000,
      transparent: true,
      opacity: 0.12,
      side: THREE.DoubleSide,
    }})
  );
  shadow.position.set(0.03, -0.02, -0.035);
  panelGroup.add(shadow);

  const matte = new THREE.Mesh(
    new THREE.PlaneGeometry(Number(spec.frame_width || 1.3), Number(spec.frame_height || 1.2)),
    new THREE.MeshStandardMaterial({{
      color: 0xfffbf3,
      roughness: 0.72,
      metalness: 0.02,
      side: THREE.DoubleSide,
    }})
  );
  matte.position.z = -0.012;
  matte.castShadow = true;
  matte.receiveShadow = true;
  panelGroup.add(matte);

  const photoPanel = new THREE.Mesh(
    new THREE.PlaneGeometry(Number(spec.photo_width || 1.12), Number(spec.photo_height || 0.92)),
    new THREE.MeshStandardMaterial({{
      color: 0xf6efe5,
      roughness: 0.82,
      metalness: 0.01,
      side: THREE.DoubleSide,
    }})
  );
  photoPanel.position.set(0, 0.05, 0.006);
  photoPanel.castShadow = true;
  photoPanel.receiveShadow = true;
  panelGroup.add(photoPanel);
  photoPanelPlanes.push(photoPanel);

  const labelTexture = buildPanelLabelTexture(String(spec.label || "Room reference"));
  if (labelTexture) {{
    const labelWidth = Math.max(0.82, Math.min(Number(spec.frame_width || 1.3), 1.72));
    const labelPlaque = new THREE.Mesh(
      new THREE.PlaneGeometry(labelWidth, 0.24),
      new THREE.MeshBasicMaterial({{
        map: labelTexture,
        transparent: true,
        side: THREE.DoubleSide,
      }})
    );
    labelPlaque.position.set(0, -((Number(spec.frame_height || 1.2) * 0.5) - 0.15), 0.018);
    panelGroup.add(labelPlaque);
  }}

  textureLoader.load(
    String(spec.photo_relpath || ""),
    (texture) => {{
      texture.colorSpace = THREE.SRGBColorSpace;
      texture.anisotropy = 8;
      photoPanel.material.map = texture;
      photoPanel.material.needsUpdate = true;
      loadedPhotoTextureCount += 1;
      liveViewerState.loadedPhotoTextureCount = loadedPhotoTextureCount;
    }},
    undefined,
    () => null,
  );

  photoPanelCards.push({{
    group: panelGroup,
    routeIndex: Number(spec.route_index ?? -1),
    matteMaterial: matte.material,
  }});
  photoPanelGroup.add(panelGroup);
}}

const outline = new THREE.Mesh(
  new THREE.PlaneGeometry(roomWidth * 1.01, roomDepth * 1.01),
  new THREE.MeshBasicMaterial({{
    color: 0xffffff,
    opacity: 0.08,
    transparent: true,
    side: THREE.DoubleSide,
  }})
);
outline.rotation.x = -Math.PI / 2;
outline.position.y = 0.002;
scene.add(outline);

function easeInOutCubic(value) {{
  const bounded = Math.max(0, Math.min(1, Number(value || 0)));
  if (bounded < 0.5) {{
    return 4 * bounded * bounded * bounded;
  }}
  return 1 - Math.pow(-2 * bounded + 2, 3) / 2;
}}

function overviewCameraState() {{
  const firstFocus = routeStops[0]?.focus && typeof routeStops[0].focus === "object"
    ? routeStops[0].focus
    : {{ x: 0, y: roomHeight * 0.46, z: 0 }};
  const overviewSpan = Math.max(2.6, Math.min(5.6, Math.min(roomWidth, roomDepth) * 0.68));
  const overviewTarget = new THREE.Vector3(
    Number(firstFocus.x || 0),
    Math.max(roomHeight * 0.38, Number(firstFocus.y || 0)),
    Number(firstFocus.z || 0),
  );
  return {{
    position: new THREE.Vector3(
      overviewTarget.x + (overviewSpan * 0.68),
      Math.max(roomHeight * 1.18, 3.1),
      overviewTarget.z + (overviewSpan * 0.72),
    ),
    target: overviewTarget,
    viewMode: "overview",
    routeIndex: activeRouteIndex >= 0 ? activeRouteIndex : (routeStops.length ? 0 : -1),
  }};
}}

function dollhouseCameraState() {{
  return {{
    position: new THREE.Vector3(
      roomWidth * 0.12,
      Math.max(roomHeight * 2.18, Math.max(roomWidth, roomDepth) * 1.08),
      roomDepth * 0.94,
    ),
    target: new THREE.Vector3(0, roomHeight * 0.24, -roomDepth * 0.03),
    viewMode: "dollhouse",
    routeIndex: activeRouteIndex >= 0 ? activeRouteIndex : (routeStops.length ? 0 : -1),
  }};
}}

function roomWallLocalPoint(point, wall) {{
  const rotationY = Number(wall?.rotation_y || 0);
  const cosine = Math.cos(rotationY);
  const sine = Math.sin(rotationY);
  const deltaX = Number(point?.x || 0) - Number(wall?.center_x || 0);
  const deltaZ = Number(point?.z || 0) - Number(wall?.center_z || 0);
  return {{
    x: (cosine * deltaX) - (sine * deltaZ),
    z: (sine * deltaX) + (cosine * deltaZ),
  }};
}}

function roomPointInsideWall(point, wall, padding = 0) {{
  const local = roomWallLocalPoint(point, wall);
  const halfWidth = (Math.max(0, Number(wall?.width || 0)) * 0.5) + Math.max(0, Number(padding || 0));
  const halfDepth = (Math.max(0, Number(wall?.depth || 0)) * 0.5) + Math.max(0, Number(padding || 0));
  return Math.abs(local.x) <= halfWidth && Math.abs(local.z) <= halfDepth;
}}

function roomSegmentIntersectsWall(start, end, wall, padding = 0.06) {{
  const localStart = roomWallLocalPoint(start, wall);
  const localEnd = roomWallLocalPoint(end, wall);
  const deltaX = localEnd.x - localStart.x;
  const deltaZ = localEnd.z - localStart.z;
  const halfWidth = (Math.max(0, Number(wall?.width || 0)) * 0.5) + Math.max(0, Number(padding || 0));
  const halfDepth = (Math.max(0, Number(wall?.depth || 0)) * 0.5) + Math.max(0, Number(padding || 0));
  let entry = 0;
  let exit = 1;
  for (const [origin, delta, extent] of [[localStart.x, deltaX, halfWidth], [localStart.z, deltaZ, halfDepth]]) {{
    if (Math.abs(delta) < 1e-7) {{
      if (origin < -extent || origin > extent) return false;
      continue;
    }}
    const first = (-extent - origin) / delta;
    const second = (extent - origin) / delta;
    entry = Math.max(entry, Math.min(first, second));
    exit = Math.min(exit, Math.max(first, second));
    if (entry > exit) return false;
  }}
  return exit >= 0.02 && entry <= 0.98;
}}

function roomCameraWallIndexes(position, target, options = {{}}) {{
  const spread = Boolean(options && options.spread);
  const probeRadius = Math.max(0.34, Math.min(0.68, Math.max(roomWidth, roomDepth) * 0.052));
  const probeTargets = [target];
  if (spread) {{
    probeTargets.push(
      target.clone().add(new THREE.Vector3(probeRadius, 0, 0)),
      target.clone().add(new THREE.Vector3(-probeRadius, 0, 0)),
      target.clone().add(new THREE.Vector3(0, 0, probeRadius)),
      target.clone().add(new THREE.Vector3(0, 0, -probeRadius)),
    );
  }}
  const indexes = new Set();
  for (const probeTarget of probeTargets) {{
    wallRectangles.forEach((wall, wallIndex) => {{
      if (roomSegmentIntersectsWall(position, probeTarget, wall)) {{
        indexes.add(wallIndex);
      }}
    }});
  }}
  if (spread) {{
    const targetCutawayPadding = Math.max(
      0.52,
      Math.min(0.92, Math.max(roomWidth, roomDepth) * 0.075),
    );
    wallRectangles.forEach((wall, wallIndex) => {{
      if (roomPointInsideWall(target, wall, targetCutawayPadding)) {{
        indexes.add(wallIndex);
      }}
    }});
  }}
  return Array.from(indexes).sort((first, second) => first - second);
}}

function routePhotoPanelPosition(routeIndex) {{
  const activePhotoPanelSpec = photoPanelSpecs.find(
    (spec) => spec && Number(spec.route_index ?? -1) === Number(routeIndex ?? -1),
  );
  return activePhotoPanelSpec?.position
    ? new THREE.Vector3(
        Number(activePhotoPanelSpec.position.x || 0),
        Number(activePhotoPanelSpec.position.y || roomHeight * 0.58),
        Number(activePhotoPanelSpec.position.z || 0),
      )
    : null;
}}

function styledSelfOccluderCount(position, routeIndex) {{
  const routeObjects = styledObjectsForRoute(routeIndex);
  const routeMeshes = [];
  for (const object of routeObjects) {{
    object.traverse((child) => {{
      if (child?.isMesh) routeMeshes.push(child);
    }});
  }}
  if (!routeMeshes.length) return routeObjects.length;
  const raycaster = new THREE.Raycaster();
  let hiddenObjectCount = 0;
  for (const object of routeObjects) {{
    const ownMeshes = [];
    object.traverse((child) => {{
      if (child?.isMesh) ownMeshes.push(child);
    }});
    const ownMeshSet = new Set(ownMeshes);
    const sampleMeshes = ownMeshes.length <= 8
      ? ownMeshes
      : ownMeshes.filter((_, index) => index % Math.ceil(ownMeshes.length / 8) === 0).slice(0, 8);
    let hasVisibleSample = false;
    for (const mesh of sampleMeshes) {{
      const bounds = new THREE.Box3().setFromObject(mesh);
      if (bounds.isEmpty()) continue;
      const sampleTarget = bounds.getCenter(new THREE.Vector3());
      const direction = sampleTarget.clone().sub(position);
      const targetDistance = direction.length();
      if (targetDistance <= 1e-5) continue;
      raycaster.set(position, direction.normalize());
      raycaster.near = 0.01;
      raycaster.far = targetDistance + 0.12;
      const firstHit = raycaster.intersectObjects(routeMeshes, false)[0];
      if (firstHit?.object && ownMeshSet.has(firstHit.object)) {{
        hasVisibleSample = true;
        break;
      }}
    }}
    if (!hasVisibleSample) hiddenObjectCount += 1;
  }}
  return hiddenObjectCount;
}}

function roomCameraPosition(target, cameraStop, routeIndex, variant, styledBounds = null) {{
  const maximumSpan = Math.max(roomWidth, roomDepth);
  const styledSize = styledBounds instanceof THREE.Box3
    ? styledBounds.getSize(new THREE.Vector3())
    : new THREE.Vector3(2.4, 1.2, 2.2);
  const styledHorizontalSpan = Math.max(
    2.2,
    styledSize.x,
    styledSize.z,
    Math.hypot(styledSize.x, styledSize.z) * 0.82,
  );
  const radius = Math.max(3.0, Math.min(4.6, styledHorizontalSpan * 1.38));
  const preferredX = Number(cameraStop.x ?? target.x + 1) - target.x;
  const preferredZ = Number(cameraStop.z ?? target.z + 1) - target.z;
  const fallbackAngle = ((Math.max(0, Number(routeIndex || 0)) % 8) / 8) * Math.PI * 2;
  const preferredAngle = Math.abs(preferredX) + Math.abs(preferredZ) > 0.01
    ? Math.atan2(preferredZ, preferredX)
    : fallbackAngle;
  const variantOffset = Math.max(0, Number(variant || 0)) * (Math.PI / 16) * (Number(routeIndex || 0) % 2 ? 1 : -1);
  const angleOffsets = [0, Math.PI / 4, -Math.PI / 4, Math.PI / 2, -Math.PI / 2, Math.PI * 0.75, -Math.PI * 0.75, Math.PI];
  const halfWidth = roomWidth * 0.5;
  const halfDepth = roomDepth * 0.5;
  const boundaryInset = Math.max(0.28, Math.min(0.48, maximumSpan * 0.035));
  let best = null;
  for (let candidateIndex = 0; candidateIndex < angleOffsets.length; candidateIndex += 1) {{
    const angle = preferredAngle + variantOffset + angleOffsets[candidateIndex];
    const position = new THREE.Vector3(
      Math.max(-halfWidth + boundaryInset, Math.min(halfWidth - boundaryInset, target.x + (Math.cos(angle) * radius))),
      Math.max(1.38, Math.min(roomHeight * 0.72, target.y + 0.96)),
      Math.max(-halfDepth + boundaryInset, Math.min(halfDepth - boundaryInset, target.z + (Math.sin(angle) * radius))),
    );
    const horizontalDistance = Math.hypot(position.x - target.x, position.z - target.z);
    const insideWall = wallRectangles.some((wall) => roomPointInsideWall(position, wall, 0.12));
    const insideGeneratedDecor = stagingObjects.some((object) => {{
      const decorBounds = new THREE.Box3().setFromObject(object).expandByScalar(0.28);
      return !decorBounds.isEmpty() && decorBounds.containsPoint(position);
    }});
    const occluderCount = roomCameraWallIndexes(position, target).length;
    const styledOccluderCount = styledObjectsForRoute(routeIndex).reduce((count, object) => {{
      const objectBounds = new THREE.Box3().setFromObject(object);
      if (objectBounds.isEmpty()) return count;
      const objectTarget = objectBounds.getCenter(new THREE.Vector3());
      return count + (roomCameraWallIndexes(position, objectTarget).length > 0 ? 1 : 0);
    }}, 0);
    const styledSelfOcclusionCount = styledSelfOccluderCount(position, routeIndex);
    const score =
      (insideWall ? 10000 : 0)
      + (insideGeneratedDecor ? 9000 : 0)
      + (occluderCount * 1000)
      + (styledOccluderCount * 1400)
      + (styledSelfOcclusionCount * 2200)
      + (Math.max(0, radius - horizontalDistance) * 120)
      + candidateIndex;
    if (!best || score < best.score) {{
      best = {{ position, score }};
    }}
  }}
  return best?.position || new THREE.Vector3(target.x + 1.4, roomHeight * 0.9, target.z + 1.4);
}}

function routeCameraState(index = 0, variant = 0) {{
  const boundedIndex = routeStops.length
    ? Math.max(0, Math.min(Number(index || 0), routeStops.length - 1))
    : -1;
  const stop = boundedIndex >= 0 ? (routeStops[boundedIndex] || routeStops[0]) : null;
  const focus = stop?.focus && typeof stop.focus === "object" ? stop.focus : {{}};
  const cameraStop = stop?.camera && typeof stop.camera === "object" ? stop.camera : {{}};
  const styledBounds = styledBoundsForRoute(boundedIndex);
  const styledCenter = styledBounds
    ? styledBounds.getCenter(new THREE.Vector3())
    : null;
  const target = new THREE.Vector3(
    styledCenter ? styledCenter.x : Number(focus.x || 0),
    Math.max(0.48, Math.min(0.68, styledCenter ? styledCenter.y : Number(focus.y || 0.56))),
    styledCenter ? styledCenter.z : Number(focus.z || 0),
  );
  const visitVariant = Math.max(0, Number(variant || 0));
  const position = roomCameraPosition(target, cameraStop, boundedIndex, visitVariant, styledBounds);
  return {{
    position,
    target,
    viewMode: "room",
    routeIndex: boundedIndex,
  }};
}}

function insideCameraState() {{
  if (routeStops.length) {{
    return routeCameraState(activeRouteIndex >= 0 ? activeRouteIndex : 0, 0);
  }}
  return {{
    position: new THREE.Vector3(-roomWidth * 0.12, roomHeight * 0.78, roomDepth * 0.18),
    target: new THREE.Vector3(roomWidth * 0.2, roomHeight * 0.66, -roomDepth * 0.28),
    viewMode: "room",
    routeIndex: activeRouteIndex,
  }};
}}

function resolveViewState(request = {{}}) {{
  const viewMode = request && typeof request === "object" ? String(request.viewMode || "").trim().toLowerCase() : "";
  if (viewMode === "dollhouse") {{
    return dollhouseCameraState();
  }}
  if (viewMode === "room") {{
    return routeStops.length
      ? routeCameraState(Number(request.routeIndex || 0), Number(request.variant || 0))
      : insideCameraState();
  }}
  if (viewMode === "inside") {{
    return insideCameraState();
  }}
  return overviewCameraState();
}}

function captureRouteCopy(overlay = {{}}) {{
  const normalizedOverlay = overlay && typeof overlay === "object" ? overlay : {{}};
  const routeIndexValue = Number.isFinite(Number(normalizedOverlay.routeIndex))
    ? Number(normalizedOverlay.routeIndex)
    : activeRouteIndex;
  const activeStop = routeIndexValue >= 0 ? routeStops[routeIndexValue] : null;
  const activeLabel = activeStop
    ? String(activeStop.label || activeStop.room || activeStop.name || `Stop ${{routeIndexValue + 1}}`)
    : "";
  const sequence = Number.isFinite(Number(normalizedOverlay.sequence))
    ? Math.max(0, Number(normalizedOverlay.sequence))
    : (routeIndexValue >= 0 ? routeIndexValue + 1 : 0);
  const total = Number.isFinite(Number(normalizedOverlay.total))
    ? Math.max(1, Number(normalizedOverlay.total))
    : Math.max(1, Number(routeStops.length || 1));
  const label = String(
    normalizedOverlay.label
    || (activeViewMode === "dollhouse" ? "Dollhouse route view" : activeViewMode === "room" ? activeLabel || "Room view" : "Layout overview")
  ).trim();
  const kicker = String(normalizedOverlay.kicker || (activeViewMode === "room" ? "Room route" : "Layout flythrough")).trim();
  const progress = String(
    normalizedOverlay.progress
    || (sequence > 0 ? `Stop ${{sequence}} of ${{total}}` : `${{Math.max(1, routeStops.length || 1)}} route stops`)
  ).trim();
  return {{
    kicker: kicker || "Layout flythrough",
    label: label || "Layout overview",
    progress: progress || "Planning preview",
  }};
}}

function syncCaptureRouteCard(overlay = {{}}) {{
  if (!captureRouteCard || !captureRouteKicker || !captureRouteLabel || !captureRouteProgress) {{
    return null;
  }}
  const copy = captureRouteCopy(overlay);
  captureRouteCard.hidden = !captureMode;
  captureRouteKicker.textContent = copy.kicker;
  captureRouteLabel.textContent = copy.label;
  captureRouteProgress.textContent = copy.progress;
  return copy;
}}

function travelDistanceForTransition(nextPosition, nextTarget) {{
  const cameraDistance = camera.position.distanceTo(nextPosition);
  const targetDistance = controls.target.distanceTo(nextTarget);
  return Math.max(cameraDistance, targetDistance * 1.15);
}}

const routeCameraTransition = {{
  active: false,
  startedAt: 0,
  durationMs: 0,
  progress: 1,
  targetRouteIndex: routeStops.length ? 0 : -1,
  targetViewMode: "overview",
  fromPosition: new THREE.Vector3(),
  toPosition: new THREE.Vector3(),
  fromTarget: new THREE.Vector3(),
  toTarget: new THREE.Vector3(),
}};
const guidedRouteState = {{
  active: false,
  currentIndex: -1,
  dwellMs: 2200,
  timerId: 0,
}};

function clearGuidedRouteTimer() {{
  if (guidedRouteState.timerId) {{
    window.clearTimeout(guidedRouteState.timerId);
    guidedRouteState.timerId = 0;
  }}
}}

function setGuideChipState(active) {{
  if (!guideButton) {{
    return;
  }}
  guideButton.dataset.active = active ? "true" : "false";
  guideButton.setAttribute("aria-pressed", active ? "true" : "false");
  guideButton.disabled = Boolean(prefersReducedMotion);
  if (prefersReducedMotion) {{
    guideButton.title = "Guided autoplay is off while reduced motion is enabled.";
  }} else {{
    guideButton.removeAttribute("title");
  }}
  guideButton.textContent = active ? "Stop guide" : "Guide me";
}}

function stopGuidedRoute() {{
  const wasActive = Boolean(guidedRouteState.active);
  clearGuidedRouteTimer();
  guidedRouteState.active = false;
  setGuideChipState(false);
  if (wasActive) {{
    announceViewerState("Guided room route stopped.");
  }}
}}

function queueGuidedRouteIndex(index, delayMs) {{
  clearGuidedRouteTimer();
  if (!guidedRouteState.active) {{
    return;
  }}
  guidedRouteState.timerId = window.setTimeout(() => {{
    runGuidedRouteIndex(index);
  }}, Math.max(240, Number(delayMs || guidedRouteState.dwellMs)));
}}

function runGuidedRouteIndex(index) {{
  if (!guidedRouteState.active || !routeStops.length) {{
    return;
  }}
  const boundedIndex = Math.max(0, Math.min(Number(index || 0), routeStops.length - 1));
  guidedRouteState.currentIndex = boundedIndex;
  setRouteView(boundedIndex, {{ guided: true, variant: boundedIndex > 0 ? 1 : 0 }});
  const transitionDelay = Math.max(guidedRouteState.dwellMs, Number(routeCameraTransition.durationMs || 0) + guidedRouteState.dwellMs);
  if (boundedIndex + 1 < routeStops.length) {{
    queueGuidedRouteIndex(boundedIndex + 1, transitionDelay);
    return;
  }}
  guidedRouteState.timerId = window.setTimeout(() => {{
    stopGuidedRoute();
  }}, transitionDelay);
}}

function startGuidedRoute(options = {{}}) {{
  if (!routeStops.length || prefersReducedMotion) {{
    if (prefersReducedMotion) {{
      stopGuidedRoute();
      announceViewerState("Guided autoplay is off because reduced motion is enabled.");
    }}
    return false;
  }}
  guidedRouteState.active = true;
  const requestedIndex = Number.isFinite(Number(options && options.startIndex))
    ? Number(options.startIndex)
    : (activeRouteIndex >= 0 ? activeRouteIndex : 0);
  guidedRouteState.currentIndex = Math.max(0, Math.min(requestedIndex, routeStops.length - 1));
  setGuideChipState(true);
  announceViewerState("Guided room route started.");
  runGuidedRouteIndex(guidedRouteState.currentIndex);
  return true;
}}

function commitCameraView(position, target) {{
  camera.position.copy(position);
  controls.target.copy(target);
  controls.update();
}}

function completeCameraTransition() {{
  commitCameraView(routeCameraTransition.toPosition, routeCameraTransition.toTarget);
  routeCameraTransition.active = false;
  routeCameraTransition.startedAt = 0;
  routeCameraTransition.progress = 1;
  if (activeViewMode === "room") {{
    updateRoomCameraCutaway(camera.position, controls.target, {{ force: true }});
  }}
}}

function handleReducedMotionChange(event) {{
  prefersReducedMotion = Boolean(event && event.matches);
  liveViewerState.prefersReducedMotion = Boolean(prefersReducedMotion);
  if (prefersReducedMotion) {{
    if (routeCameraTransition.active) {{
      routeCameraTransition.durationMs = 0;
      completeCameraTransition();
    }}
    stopGuidedRoute();
    announceViewerState("Reduced motion enabled. Camera transitions and guided autoplay are off.");
  }} else {{
    announceViewerState("Reduced motion disabled. Camera transitions are available.");
  }}
  setGuideChipState(guidedRouteState.active);
}}

function startCameraTransition({{ position, target, viewMode, routeIndex = activeRouteIndex, immediate = false }}) {{
  const nextPosition = position.clone();
  const nextTarget = target.clone();
  const travelDistance = travelDistanceForTransition(nextPosition, nextTarget);
  applyViewMode(viewMode);
  const normalizedRouteIndex = routeStops.length
    ? Math.max(-1, Math.min(Number(routeIndex ?? activeRouteIndex ?? -1), routeStops.length - 1))
    : -1;
  if (normalizedRouteIndex >= 0) {{
    setActiveRouteButton(normalizedRouteIndex);
  }}
  routeCameraTransition.targetRouteIndex = normalizedRouteIndex;
  routeCameraTransition.targetViewMode = activeViewMode;
  routeCameraTransition.toPosition.copy(nextPosition);
  routeCameraTransition.toTarget.copy(nextTarget);
  if (immediate || prefersReducedMotion || travelDistance < 0.08) {{
    routeCameraTransition.durationMs = 0;
    completeCameraTransition();
    return;
  }}
  routeCameraTransition.fromPosition.copy(camera.position);
  routeCameraTransition.fromTarget.copy(controls.target);
  routeCameraTransition.durationMs = Math.round(Math.max(650, Math.min(1550, 560 + (travelDistance * 120))));
  routeCameraTransition.startedAt = performance.now();
  routeCameraTransition.progress = 0;
  routeCameraTransition.active = true;
}}

function stepCameraTransition(now) {{
  if (!routeCameraTransition.active) {{
    return false;
  }}
  const elapsed = Math.max(0, Number(now || performance.now()) - Number(routeCameraTransition.startedAt || 0));
  const durationMs = Math.max(1, Number(routeCameraTransition.durationMs || 1));
  const linearProgress = Math.max(0, Math.min(1, elapsed / durationMs));
  const easedProgress = easeInOutCubic(linearProgress);
  routeCameraTransition.progress = linearProgress;
  camera.position.lerpVectors(routeCameraTransition.fromPosition, routeCameraTransition.toPosition, easedProgress);
  controls.target.lerpVectors(routeCameraTransition.fromTarget, routeCameraTransition.toTarget, easedProgress);
  controls.update();
  if (linearProgress >= 1) {{
    completeCameraTransition();
  }}
  return true;
}}

function setOverviewView(options = {{}}) {{
  if (!Boolean(options && options.guided)) {{
    stopGuidedRoute();
  }}
  const state = overviewCameraState();
  startCameraTransition({{
    position: state.position,
    target: state.target,
    viewMode: state.viewMode,
    routeIndex: state.routeIndex,
    immediate: renderFrameCount < 1,
  }});
}}

function setDollhouseView(options = {{}}) {{
  if (!Boolean(options && options.guided)) {{
    stopGuidedRoute();
  }}
  const state = dollhouseCameraState();
  startCameraTransition({{
    position: state.position,
    target: state.target,
    viewMode: state.viewMode,
    routeIndex: state.routeIndex,
    immediate: renderFrameCount < 1,
  }});
}}

function setInsideView(options = {{}}) {{
  if (!Boolean(options && options.guided)) {{
    stopGuidedRoute();
  }}
  if (routeStops.length) {{
    setRouteView(0, {{ immediate: renderFrameCount < 1 && activeRouteIndex < 0, guided: Boolean(options && options.guided) }});
    return;
  }}
  const state = insideCameraState();
  startCameraTransition({{
    position: state.position,
    target: state.target,
    viewMode: state.viewMode,
    routeIndex: state.routeIndex,
    immediate: renderFrameCount < 1,
  }});
}}

let activeRouteIndex = -1;
let activeViewMode = "overview";
function setActiveViewChip(mode) {{
  const viewButtons = [
    [overviewButton, mode === "overview"],
    [dollhouseButton, mode === "dollhouse"],
    [insideButton, mode === "room"],
  ];
  viewButtons.forEach(([button, active]) => {{
    if (!button) return;
    button.dataset.active = active ? "true" : "false";
    button.setAttribute("aria-pressed", active ? "true" : "false");
  }});
  setGuideChipState(guidedRouteState.active);
  const viewLabel = mode === "dollhouse" ? "Dollhouse view" : mode === "room" ? "Room view" : "Overview";
  announceViewerState(`${{viewLabel}} selected.`);
}}

const cutawayWallCount = wallMeshPairs.filter((pair) => Boolean(pair.cutawayEligible)).length;
const roomOccludingWallIndexes = new Set();
const roomCutawayState = {{
  cameraPosition: new THREE.Vector3(),
  target: new THREE.Vector3(),
  evaluationCount: 0,
  initialized: false,
  evaluatedAt: 0,
}};
function applyCutawayWallVisibility(active) {{
  const hideCutawayWalls = Boolean(active);
  wallMeshPairs.forEach((pair, wallIndex) => {{
    const hideRoomOccluder = activeViewMode === "room" && roomOccludingWallIndexes.has(wallIndex);
    const visible = !(hideCutawayWalls && Boolean(pair.cutawayEligible)) && !hideRoomOccluder;
    pair.mesh.visible = visible;
    pair.edges.visible = visible;
  }});
}}

function updateRoomCameraCutaway(position, target, options = {{}}) {{
  if (activeViewMode === "room") {{
    const unchangedPose =
      roomCutawayState.initialized
      && roomCutawayState.cameraPosition.distanceToSquared(position) < 1e-8
      && roomCutawayState.target.distanceToSquared(target) < 1e-8;
    if (unchangedPose) {{
      return;
    }}
    const evaluatedAt = performance.now();
    if (
      !Boolean(options && options.force)
      && roomCutawayState.initialized
      && evaluatedAt - Number(roomCutawayState.evaluatedAt || 0) < 80
    ) {{
      return;
    }}
    roomCutawayState.cameraPosition.copy(position);
    roomCutawayState.target.copy(target);
    roomCutawayState.evaluationCount += 1;
    roomCutawayState.initialized = true;
    roomCutawayState.evaluatedAt = evaluatedAt;
  }}
  const nextIndexes = activeViewMode === "room"
    ? roomCameraWallIndexes(position, target, {{ spread: true }})
    : [];
  const unchanged =
    nextIndexes.length === roomOccludingWallIndexes.size
    && nextIndexes.every((wallIndex) => roomOccludingWallIndexes.has(wallIndex));
  if (unchanged) {{
    return;
  }}
  roomOccludingWallIndexes.clear();
  nextIndexes.forEach((wallIndex) => roomOccludingWallIndexes.add(wallIndex));
  applyCutawayWallVisibility(activeViewMode === "overview" || activeViewMode === "dollhouse");
}}

function setWallHeightScale(scale) {{
  const boundedScale = Math.max(0.42, Math.min(1.0, Number(scale || 1)));
  for (const mesh of wallMeshes) {{
    mesh.scale.y = boundedScale;
    mesh.position.y = roomHeight * boundedScale * 0.5;
  }}
  for (const edge of wallEdgeMeshes) {{
    edge.scale.y = boundedScale;
    edge.position.y = roomHeight * boundedScale * 0.5;
  }}
}}

function applyViewMode(mode) {{
  const normalizedMode = mode === "dollhouse" ? "dollhouse" : mode === "room" ? "room" : "overview";
  const isOverview = normalizedMode === "overview";
  const isDollhouse = normalizedMode === "dollhouse";
  const cutawayActive = isOverview || isDollhouse;
  activeViewMode = normalizedMode;
  liveViewerState.viewMode = normalizedMode;
  wallMaterial.opacity = isDollhouse ? 0.3 : isOverview ? 0.66 : 0.24;
  wallMaterial.depthWrite = !cutawayActive && normalizedMode !== "room";
  wallMaterial.needsUpdate = true;
  wallEdgeMaterial.opacity = isDollhouse ? 0.88 : isOverview ? 0.54 : 0.22;
  floor.material.color.set(styleColor("floor", "#cbb998"));
  photoPanelGroup.visible = isOverview;
  controls.minPolarAngle = isDollhouse ? Math.PI * 0.02 : isOverview ? Math.PI * 0.1 : Math.PI * 0.14;
  controls.maxPolarAngle = isDollhouse ? Math.PI * 0.34 : isOverview ? Math.PI * 0.42 : Math.PI * 0.49;
  controls.minDistance = normalizedMode === "room"
    ? Math.max(0.82, Math.max(roomWidth, roomDepth) * 0.085)
    : Math.max(roomWidth, roomDepth) * 0.32;
  setWallHeightScale(isDollhouse ? 0.42 : isOverview ? 0.62 : 0.58);
  if (normalizedMode !== "room") {{
    roomOccludingWallIndexes.clear();
    roomCutawayState.initialized = false;
  }}
  syncGeneratedSceneModeVisibility(normalizedMode, activeRouteIndex);
  applyCutawayWallVisibility(cutawayActive);
  setActiveViewChip(normalizedMode);
  syncCaptureRouteCard();
}}

function setActiveRouteButton(index) {{
  routeButtons.forEach((button, buttonIndex) => {{
    const active = buttonIndex === index;
    button.dataset.active = active ? "true" : "false";
    button.setAttribute("aria-current", active ? "step" : "false");
  }});
  floorplanStopButtons.forEach((button) => {{
    const active = Number(button.dataset.routeIndex || -1) === index;
    button.dataset.active = active ? "true" : "false";
    button.setAttribute("aria-current", active ? "step" : "false");
  }});
  routeMarkers.forEach((marker, markerIndex) => {{
    marker.scale.setScalar(markerIndex === index ? 1.28 : 1.0);
    marker.material.color.set(markerIndex === index ? 0xb9892f : 0xa77c2b);
  }});
  routeHotspots.forEach((entry, entryIndex) => {{
    const active = entryIndex === index;
    entry.button.dataset.active = active ? "true" : "false";
    entry.button.setAttribute("aria-current", active ? "step" : "false");
  }});
  photoPanelCards.forEach((card) => {{
    const active = Number(card.routeIndex) === index;
    card.group.scale.setScalar(active ? 1.035 : 1.0);
    card.matteMaterial.color.set(active ? 0xfff4df : 0xfffbf3);
  }});
  activeRouteIndex = index;
  liveViewerState.activeRouteIndex = index;
  syncGeneratedSceneModeVisibility(activeViewMode, activeRouteIndex);
  syncCaptureRouteCard({{ routeIndex: index }});
  const activeStop = index >= 0 ? routeStops[index] : null;
  const activeLabel = String(activeStop?.label || activeStop?.room || activeStop?.name || `Stop ${{index + 1}}`);
  if (index >= 0) {{
    announceViewerState(`Room route: ${{activeLabel}}, stop ${{index + 1}} of ${{routeStops.length}}.`);
  }}
}}

function setRouteView(index, options = {{}}) {{
  if (!routeStops.length) {{
    return;
  }}
  if (!Boolean(options && options.guided)) {{
    stopGuidedRoute();
  }}
  const state = routeCameraState(index, Number(options?.variant || 0));
  startCameraTransition({{
    position: state.position,
    target: state.target,
    viewMode: state.viewMode,
    routeIndex: state.routeIndex,
    immediate: Boolean(options && options.immediate),
  }});
}}

overviewButton?.addEventListener("click", setOverviewView);
dollhouseButton?.addEventListener("click", setDollhouseView);
insideButton?.addEventListener("click", setInsideView);
guideButton?.addEventListener("click", () => {{
  if (guidedRouteState.active) {{
    stopGuidedRoute();
    return;
  }}
  startGuidedRoute({{ startIndex: activeRouteIndex >= 0 ? activeRouteIndex : 0 }});
}});
routeButtons.forEach((button, index) => button.addEventListener("click", () => setRouteView(index)));
floorplanStopButtons.forEach((button) => button.addEventListener("click", () => setRouteView(Number(button.dataset.routeIndex || 0))));
renderer.domElement.addEventListener("pointerdown", () => {{
  if (guidedRouteState.active) {{
    stopGuidedRoute();
  }}
}});
renderer.domElement.addEventListener("wheel", () => {{
  if (guidedRouteState.active) {{
    stopGuidedRoute();
  }}
}}, {{ passive: true }});
controls.addEventListener("end", () => {{
  if (activeViewMode === "room") {{
    updateRoomCameraCutaway(camera.position, controls.target, {{ force: true }});
  }}
}});

function resize() {{
  const width = Math.max(320, viewport.clientWidth || 320);
  const height = Math.max(420, viewport.clientHeight || 420);
  camera.aspect = width / height;
  camera.updateProjectionMatrix();
  renderer.setSize(width, height, false);
  syncRouteHotspots();
}}

function syncRouteHotspots() {{
  if (!hotspotLayer) {{
    return 0;
  }}
  const width = Math.max(1, viewport.clientWidth || renderer.domElement.clientWidth || 1);
  const height = Math.max(1, viewport.clientHeight || renderer.domElement.clientHeight || 1);
  let visibleCount = 0;
  for (const entry of routeHotspots) {{
    const routeIndex = Number(entry.button.dataset.routeIndex ?? -1);
    if (activeViewMode === "room") {{
      entry.button.hidden = true;
      continue;
    }}
    const projected = entry.focus.clone().project(camera);
    const visible =
      Number.isFinite(projected.x) &&
      Number.isFinite(projected.y) &&
      Number.isFinite(projected.z) &&
      projected.z > -1 &&
      projected.z < 1;
    if (!visible) {{
      entry.button.hidden = true;
      continue;
    }}
    const left = (projected.x * 0.5 + 0.5) * width;
    const top = (-projected.y * 0.5 + 0.5) * height;
    if (left < -24 || left > width + 24 || top < -24 || top > height + 24) {{
      entry.button.hidden = true;
      continue;
    }}
    entry.button.hidden = false;
    entry.button.style.left = `${{left.toFixed(1)}}px`;
    entry.button.style.top = `${{top.toFixed(1)}}px`;
    entry.label.dataset.placement = "below";
    entry.label.style.setProperty("--hotspot-label-shift-x", "0px");
    entry.label.style.setProperty("--hotspot-label-shift-y", "0px");
    const layerBounds = hotspotLayer.getBoundingClientRect();
    let labelBounds = entry.label.getBoundingClientRect();
    const inset = 8;
    if (labelBounds.bottom > layerBounds.bottom - inset) {{
      entry.label.dataset.placement = "above";
      labelBounds = entry.label.getBoundingClientRect();
    }}
    let shiftX = 0;
    if (labelBounds.left < layerBounds.left + inset) {{
      shiftX += (layerBounds.left + inset) - labelBounds.left;
    }}
    if (labelBounds.right + shiftX > layerBounds.right - inset) {{
      shiftX -= (labelBounds.right + shiftX) - (layerBounds.right - inset);
    }}
    let shiftY = 0;
    if (labelBounds.top < layerBounds.top + inset) {{
      shiftY += (layerBounds.top + inset) - labelBounds.top;
    }}
    if (labelBounds.bottom + shiftY > layerBounds.bottom - inset) {{
      shiftY -= (labelBounds.bottom + shiftY) - (layerBounds.bottom - inset);
    }}
    entry.label.style.setProperty("--hotspot-label-shift-x", `${{shiftX.toFixed(1)}}px`);
    entry.label.style.setProperty("--hotspot-label-shift-y", `${{shiftY.toFixed(1)}}px`);
    labelBounds = entry.label.getBoundingClientRect();
    if (labelBounds.left < layerBounds.left + inset) {{
      shiftX += (layerBounds.left + inset) - labelBounds.left;
    }}
    if (labelBounds.right > layerBounds.right - inset) {{
      shiftX -= labelBounds.right - (layerBounds.right - inset);
    }}
    if (labelBounds.top < layerBounds.top + inset) {{
      shiftY += (layerBounds.top + inset) - labelBounds.top;
    }}
    if (labelBounds.bottom > layerBounds.bottom - inset) {{
      shiftY -= labelBounds.bottom - (layerBounds.bottom - inset);
    }}
    entry.label.style.setProperty("--hotspot-label-shift-x", `${{shiftX.toFixed(1)}}px`);
    entry.label.style.setProperty("--hotspot-label-shift-y", `${{shiftY.toFixed(1)}}px`);
    visibleCount += 1;
  }}
  return visibleCount;
}}

function getVisibleHotspotLabelBounds() {{
  if (!hotspotLayer) return [];
  const viewportBounds = hotspotLayer.getBoundingClientRect();
  const inset = 7;
  return routeHotspots
    .filter((entry) => !entry.button.hidden && Number.parseFloat(getComputedStyle(entry.label).opacity || "0") > 0)
    .map((entry) => {{
      const bounds = entry.label.getBoundingClientRect();
      return {{
        label: String(entry.label.textContent || "").trim(),
        left: Number(bounds.left.toFixed(2)),
        top: Number(bounds.top.toFixed(2)),
        right: Number(bounds.right.toFixed(2)),
        bottom: Number(bounds.bottom.toFixed(2)),
        insideViewport:
          bounds.left >= viewportBounds.left + inset &&
          bounds.right <= viewportBounds.right - inset &&
          bounds.top >= viewportBounds.top + inset &&
          bounds.bottom <= viewportBounds.bottom - inset,
      }};
    }});
}}

function renderCaptureFrame(payload = {{}}) {{
  const normalizedPayload = payload && typeof payload === "object" ? payload : {{}};
  const fromState = resolveViewState(normalizedPayload.from || {{}});
  const toState = resolveViewState(normalizedPayload.to || {{}});
  const progress = easeInOutCubic(Math.max(0, Math.min(1, Number(normalizedPayload.progress || 0))));
  const routeIndex = Number.isFinite(Number(normalizedPayload.routeIndex))
    ? Number(normalizedPayload.routeIndex)
    : Number(toState.routeIndex ?? -1);
  applyViewMode(String(toState.viewMode || "overview"));
  camera.position.lerpVectors(fromState.position, toState.position, progress);
  controls.target.lerpVectors(fromState.target, toState.target, progress);
  updateRoomCameraCutaway(camera.position, controls.target, {{ force: true }});
  controls.update();
  setActiveRouteButton(routeIndex);
  syncCaptureRouteCard(
    normalizedPayload.overlay && typeof normalizedPayload.overlay === "object"
      ? {{ ...normalizedPayload.overlay, routeIndex }}
      : {{ routeIndex }}
  );
  renderer.render(scene, camera);
  renderFrameCount += 1;
  syncRouteHotspots();
  return getRenderMetrics();
}}

window.addEventListener("resize", resize);
if (typeof ResizeObserver === "function") {{
  resizeObserver = new ResizeObserver(() => resize());
  resizeObserver.observe(viewport);
}}
resize();
setInsideView();
syncCaptureRouteCard();
setGuideChipState(false);
if (typeof reducedMotionMedia.addEventListener === "function") {{
  reducedMotionMedia.addEventListener("change", handleReducedMotionChange);
}} else if (typeof reducedMotionMedia.addListener === "function") {{
  reducedMotionMedia.addListener(handleReducedMotionChange);
}}
if (guidedQueryEnabled && routeStops.length && !captureMode && !prefersReducedMotion) {{
  window.setTimeout(() => {{
    startGuidedRoute({{ startIndex: 0 }});
  }}, 820);
}}

const obstructionRaycaster = new THREE.Raycaster();
const styleVisibilityRaycaster = new THREE.Raycaster();
const obstructionSamplePoint = new THREE.Vector2();
const obstructionWallMeshes = new Set(wallMeshes);
const obstructionStagingObjects = new Set(stagingObjects);
let latestObstructionMetrics = {{ wallFirstHitPct: 0, wallVisualObstructionPct: 0, stagingFirstHitPct: 0 }};

function getRaycastObstructionMetrics() {{
  if (activeViewMode !== "room") {{
    return {{ wallFirstHitPct: 0, wallVisualObstructionPct: 0, stagingFirstHitPct: 0 }};
  }}
  const sampleColumns = 7;
  const sampleRows = 5;
  const sampleObjects = [
    floor,
    ...wallMeshes.filter((mesh) => objectEffectivelyVisible(mesh)),
    ...stagingObjects.filter((object) => objectEffectivelyVisible(object)),
  ];
  let wallFirstHits = 0;
  let stagingFirstHits = 0;
  const sampleCount = sampleColumns * sampleRows;
  for (let row = 0; row < sampleRows; row += 1) {{
    const normalizedY = -0.76 + ((row / Math.max(1, sampleRows - 1)) * 1.52);
    for (let column = 0; column < sampleColumns; column += 1) {{
      const normalizedX = -0.78 + ((column / Math.max(1, sampleColumns - 1)) * 1.56);
      obstructionSamplePoint.set(normalizedX, normalizedY);
      obstructionRaycaster.setFromCamera(obstructionSamplePoint, camera);
      const firstHit = obstructionRaycaster.intersectObjects(sampleObjects, false)[0];
      if (!firstHit?.object) continue;
      if (obstructionWallMeshes.has(firstHit.object)) {{
        wallFirstHits += 1;
      }} else if (obstructionStagingObjects.has(firstHit.object)) {{
        stagingFirstHits += 1;
      }}
    }}
  }}
  const wallFirstHitRatio = wallFirstHits / Math.max(1, sampleCount);
  return {{
    wallFirstHitPct: Number((wallFirstHitRatio * 100).toFixed(2)),
    wallVisualObstructionPct: Number((wallFirstHitRatio * Number(wallMaterial.opacity || 0) * 100).toFixed(2)),
    stagingFirstHitPct: Number(((stagingFirstHits / Math.max(1, sampleCount)) * 100).toFixed(2)),
  }};
}}

function styleObjectRayVisibilityPct(object) {{
  if (!objectEffectivelyVisible(object)) return 0;
  const ownMeshes = [];
  object.traverse((child) => {{
    if (child?.isMesh && objectEffectivelyVisible(child)) ownMeshes.push(child);
  }});
  if (!ownMeshes.length) return 0;
  const ownMeshSet = new Set(ownMeshes);
  const sceneMeshes = [
    ...wallMeshes.filter((mesh) => objectEffectivelyVisible(mesh)),
    ...stagingObjects.filter((mesh) => objectEffectivelyVisible(mesh)),
  ];
  const sampleMeshes = ownMeshes.length <= 14
    ? ownMeshes
    : ownMeshes.filter((_, index) => index % Math.ceil(ownMeshes.length / 14) === 0).slice(0, 14);
  let visibleSamples = 0;
  for (const mesh of sampleMeshes) {{
    const bounds = new THREE.Box3().setFromObject(mesh);
    if (bounds.isEmpty()) continue;
    const sampleTarget = bounds.getCenter(new THREE.Vector3());
    const direction = sampleTarget.clone().sub(camera.position);
    const targetDistance = direction.length();
    if (targetDistance <= 1e-5) continue;
    styleVisibilityRaycaster.set(camera.position, direction.normalize());
    styleVisibilityRaycaster.near = 0.01;
    styleVisibilityRaycaster.far = targetDistance + 0.12;
    const firstHit = styleVisibilityRaycaster.intersectObjects(sceneMeshes, false)[0];
    if (firstHit?.object && ownMeshSet.has(firstHit.object)) {{
      visibleSamples += 1;
    }}
  }}
  styleVisibilityRaycaster.far = Infinity;
  return Number(((visibleSamples / Math.max(1, sampleMeshes.length)) * 100).toFixed(2));
}}

function getRenderMetrics(options = {{}}) {{
    const canvas = renderer.domElement;
    if (!canvas) {{
      return {{
        ready: false,
        reason: "canvas_unavailable",
        frameCount: Number(renderFrameCount || 0),
        wallRectCount: Number(wallRectangles.length || 0),
      }};
    }}
    scene.updateMatrixWorld(true);
    camera.updateMatrixWorld(true);
    const projectionMatrix = new THREE.Matrix4().multiplyMatrices(
      camera.projectionMatrix,
      camera.matrixWorldInverse,
    );
    const frustum = new THREE.Frustum().setFromProjectionMatrix(projectionMatrix);
    const corner = new THREE.Vector3();
    let visibleWallCount = 0;
    let hiddenCutawayWallCount = 0;
    let hiddenRoomOccluderWallCount = 0;
    let projectedCoverage = 0;
    let maxProjectedArea = 0;
    for (const [wallIndex, mesh] of wallMeshes.entries()) {{
      if (!mesh.visible) {{
        if (Boolean(mesh.userData?.cutawayEligible)) {{
          hiddenCutawayWallCount += 1;
        }}
        if (roomOccludingWallIndexes.has(wallIndex)) {{
          hiddenRoomOccluderWallCount += 1;
        }}
        continue;
      }}
      const box = new THREE.Box3().setFromObject(mesh);
      if (!frustum.intersectsBox(box)) continue;
      visibleWallCount += 1;
      const corners = [
        [box.min.x, box.min.y, box.min.z],
        [box.min.x, box.min.y, box.max.z],
        [box.min.x, box.max.y, box.min.z],
        [box.min.x, box.max.y, box.max.z],
        [box.max.x, box.min.y, box.min.z],
        [box.max.x, box.min.y, box.max.z],
        [box.max.x, box.max.y, box.min.z],
        [box.max.x, box.max.y, box.max.z],
      ];
      let minX = 1;
      let maxX = -1;
      let minY = 1;
      let maxY = -1;
      let hasProjectedCorner = false;
      for (const [x, y, z] of corners) {{
        corner.set(x, y, z).project(camera);
        if (!Number.isFinite(corner.x) || !Number.isFinite(corner.y)) continue;
        minX = Math.min(minX, Math.max(-1, Math.min(1, corner.x)));
        maxX = Math.max(maxX, Math.max(-1, Math.min(1, corner.x)));
        minY = Math.min(minY, Math.max(-1, Math.min(1, corner.y)));
        maxY = Math.max(maxY, Math.max(-1, Math.min(1, corner.y)));
        hasProjectedCorner = true;
      }}
      if (!hasProjectedCorner) continue;
      const projectedWidth = Math.max(0, (maxX - minX) / 2);
      const projectedHeight = Math.max(0, (maxY - minY) / 2);
      const projectedArea = projectedWidth * projectedHeight;
      projectedCoverage += projectedArea;
      maxProjectedArea = Math.max(maxProjectedArea, projectedArea);
    }}
    let visiblePhotoPanelCount = 0;
    let projectedPhotoCoverage = 0;
    for (const panel of photoPanelPlanes) {{
      if (!photoPanelGroup.visible || !panel.visible) continue;
      const box = new THREE.Box3().setFromObject(panel);
      if (!frustum.intersectsBox(box)) continue;
      visiblePhotoPanelCount += 1;
      const corners = [
        [box.min.x, box.min.y, box.min.z],
        [box.min.x, box.min.y, box.max.z],
        [box.min.x, box.max.y, box.min.z],
        [box.min.x, box.max.y, box.max.z],
        [box.max.x, box.min.y, box.min.z],
        [box.max.x, box.min.y, box.max.z],
        [box.max.x, box.max.y, box.min.z],
        [box.max.x, box.max.y, box.max.z],
      ];
      let minX = 1;
      let maxX = -1;
      let minY = 1;
      let maxY = -1;
      let hasProjectedCorner = false;
      for (const [x, y, z] of corners) {{
        corner.set(x, y, z).project(camera);
        if (!Number.isFinite(corner.x) || !Number.isFinite(corner.y)) continue;
        minX = Math.min(minX, Math.max(-1, Math.min(1, corner.x)));
        maxX = Math.max(maxX, Math.max(-1, Math.min(1, corner.x)));
        minY = Math.min(minY, Math.max(-1, Math.min(1, corner.y)));
        maxY = Math.max(maxY, Math.max(-1, Math.min(1, corner.y)));
        hasProjectedCorner = true;
      }}
      if (!hasProjectedCorner) continue;
      const projectedWidth = Math.max(0, (maxX - minX) / 2);
      const projectedHeight = Math.max(0, (maxY - minY) / 2);
      projectedPhotoCoverage += projectedWidth * projectedHeight;
    }}
    let visibleStagingObjectCount = 0;
    let projectedStagingCoverage = 0;
    for (const object of stagingObjects) {{
      if (!objectEffectivelyVisible(object)) continue;
      const box = new THREE.Box3().setFromObject(object);
      if (!frustum.intersectsBox(box)) continue;
      visibleStagingObjectCount += 1;
      const corners = [
        [box.min.x, box.min.y, box.min.z],
        [box.min.x, box.min.y, box.max.z],
        [box.min.x, box.max.y, box.min.z],
        [box.min.x, box.max.y, box.max.z],
        [box.max.x, box.min.y, box.min.z],
        [box.max.x, box.min.y, box.max.z],
        [box.max.x, box.max.y, box.min.z],
        [box.max.x, box.max.y, box.max.z],
      ];
      let minX = 1;
      let maxX = -1;
      let minY = 1;
      let maxY = -1;
      let hasProjectedCorner = false;
      for (const [x, y, z] of corners) {{
        corner.set(x, y, z).project(camera);
        if (!Number.isFinite(corner.x) || !Number.isFinite(corner.y)) continue;
        minX = Math.min(minX, Math.max(-1, Math.min(1, corner.x)));
        maxX = Math.max(maxX, Math.max(-1, Math.min(1, corner.x)));
        minY = Math.min(minY, Math.max(-1, Math.min(1, corner.y)));
        maxY = Math.max(maxY, Math.max(-1, Math.min(1, corner.y)));
        hasProjectedCorner = true;
      }}
      if (!hasProjectedCorner) continue;
      const projectedWidth = Math.max(0, (maxX - minX) / 2);
      const projectedHeight = Math.max(0, (maxY - minY) / 2);
      projectedStagingCoverage += projectedWidth * projectedHeight;
    }}
    let visibleStyledObjectCount = 0;
    let projectedStyledCoverage = 0;
    const visibleStyledInstanceIds = [];
    const visibleStyleCueInstanceIds = Object.fromEntries(
      requiredStyleCues.map((cue) => [cue, []]),
    );
    const projectedStyleCueCoverage = Object.fromEntries(
      requiredStyleCues.map((cue) => [cue, 0]),
    );
    const visibleStyleCueRayPct = Object.fromEntries(
      requiredStyleCues.map((cue) => [cue, 0]),
    );
    for (const object of styledSceneObjects) {{
      if (!objectEffectivelyVisible(object)) continue;
      const box = new THREE.Box3().setFromObject(object);
      if (box.isEmpty() || !frustum.intersectsBox(box)) continue;
      const corners = [
        [box.min.x, box.min.y, box.min.z],
        [box.min.x, box.min.y, box.max.z],
        [box.min.x, box.max.y, box.min.z],
        [box.min.x, box.max.y, box.max.z],
        [box.max.x, box.min.y, box.min.z],
        [box.max.x, box.min.y, box.max.z],
        [box.max.x, box.max.y, box.min.z],
        [box.max.x, box.max.y, box.max.z],
      ];
      let minX = 1;
      let maxX = -1;
      let minY = 1;
      let maxY = -1;
      let hasProjectedCorner = false;
      for (const [x, y, z] of corners) {{
        corner.set(x, y, z).project(camera);
        if (!Number.isFinite(corner.x) || !Number.isFinite(corner.y)) continue;
        minX = Math.min(minX, Math.max(-1, Math.min(1, corner.x)));
        maxX = Math.max(maxX, Math.max(-1, Math.min(1, corner.x)));
        minY = Math.min(minY, Math.max(-1, Math.min(1, corner.y)));
        maxY = Math.max(maxY, Math.max(-1, Math.min(1, corner.y)));
        hasProjectedCorner = true;
      }}
      if (!hasProjectedCorner) continue;
      const objectProjectedCoverage =
        Math.max(0, (maxX - minX) / 2)
        * Math.max(0, (maxY - minY) / 2);
      const rayVisibilityPct = styleObjectRayVisibilityPct(object);
      if (objectProjectedCoverage <= 0 || rayVisibilityPct <= 0) continue;
      const instanceId = String(object.userData?.styleInstanceId || "");
      const cue = String(object.userData?.styleCue || "");
      visibleStyledObjectCount += 1;
      visibleStyledInstanceIds.push(instanceId);
      projectedStyledCoverage += objectProjectedCoverage;
      if (Object.prototype.hasOwnProperty.call(projectedStyleCueCoverage, cue)) {{
        projectedStyleCueCoverage[cue] += objectProjectedCoverage;
        visibleStyleCueInstanceIds[cue].push(instanceId);
        visibleStyleCueRayPct[cue] = Math.max(Number(visibleStyleCueRayPct[cue] || 0), rayVisibilityPct);
      }}
    }}
    const cameraInsideGeneratedDecor = stagingObjects.some((object) => {{
      const decorBounds = new THREE.Box3().setFromObject(object).expandByScalar(0.08);
      return !decorBounds.isEmpty() && decorBounds.containsPoint(camera.position);
    }});
      if (Boolean(options && options.includeObstruction)) {{
        latestObstructionMetrics = getRaycastObstructionMetrics();
      }}
      const obstructionMetrics = latestObstructionMetrics;
      const projectedStyledCoveragePct = Number((Math.min(1, projectedStyledCoverage) * 100).toFixed(2));
      const minimumStyledCoveragePct = activeViewMode === "room" ? 5.0 : 3.0;
      const minimumStyleCueCoveragePct = activeViewMode === "room" ? 0.35 : 0.15;
      const projectedStyleCueCoveragePct = Object.fromEntries(
        requiredStyleCues.map((cue) => [
          cue,
          Number((Math.min(1, Number(projectedStyleCueCoverage[cue] || 0)) * 100).toFixed(2)),
        ]),
      );
      const missingVisibleStyleCues = requiredStyleCues.filter(
        (cue) =>
          !Array.isArray(visibleStyleCueInstanceIds[cue])
          || visibleStyleCueInstanceIds[cue].length === 0
          || Number(projectedStyleCueCoveragePct[cue] || 0) < minimumStyleCueCoveragePct
          || Number(visibleStyleCueRayPct[cue] || 0) <= 0,
      );
      const styleCueVisibilityReady = missingVisibleStyleCues.length === 0;
      const visibleStyleReady =
        Boolean(styleEvidenceReady)
        && visibleStyledObjectCount > 0
        && projectedStyledCoveragePct >= minimumStyledCoveragePct
        && (activeViewMode !== "room" || styleCueVisibilityReady);
      return {{
        ready: visibleStyleReady,
        contextLost: Boolean(contextLost),
        viewerDisposed: Boolean(viewerDisposed),
        frameCount: Number(renderFrameCount || 0),
        wallRectCount: Number(wallRectangles.length || 0),
        wallMeshCount: Number(wallMeshes.length || 0),
        cutawayWallCount: Number(cutawayWallCount || 0),
        hiddenCutawayWallCount: Number(hiddenCutawayWallCount || 0),
        hiddenRoomOccluderWallCount: Number(hiddenRoomOccluderWallCount || 0),
        roomCutawayEvaluationCount: Number(roomCutawayState.evaluationCount || 0),
        roomCutawayCameraDelta: activeViewMode === "room"
          ? Number(camera.position.distanceTo(roomCutawayState.cameraPosition).toFixed(6))
          : 0,
        visibleWallCount: Number(visibleWallCount || 0),
        routeStopCount: Number(routeStops.length || 0),
        activeRouteIndex: Number(activeRouteIndex ?? -1),
        viewMode: activeViewMode,
        captureMode: Boolean(captureMode),
        guidedQueryEnabled: Boolean(guidedQueryEnabled),
        guidedRouteActive: Boolean(guidedRouteState.active),
        prefersReducedMotion: Boolean(prefersReducedMotion),
        guidedRouteCurrentIndex: Number(guidedRouteState.currentIndex ?? -1),
        guidedRouteDwellMs: Number(guidedRouteState.dwellMs || 0),
        isTransitioning: Boolean(routeCameraTransition.active),
        transitionProgressPct: Number((Math.max(0, Math.min(1, routeCameraTransition.progress || 0)) * 100).toFixed(1)),
        transitionTargetRouteIndex: Number(routeCameraTransition.targetRouteIndex ?? -1),
        transitionDurationMs: Number(routeCameraTransition.durationMs || 0),
        transitionTargetViewMode: String(routeCameraTransition.targetViewMode || activeViewMode),
        wallOpacity: Number(wallMaterial.opacity.toFixed(3)),
        wallHeightScale: Number((wallMeshes[0]?.scale.y || 0).toFixed(3)),
        photoPanelGroupVisible: Boolean(photoPanelGroup.visible),
        hotspotCount: Number(routeHotspots.length || 0),
      visibleHotspotCount: Number(syncRouteHotspots() || 0),
      captureOverlayVisible: Boolean(captureRouteCard && !captureRouteCard.hidden),
      captureRouteLabel: String(captureRouteLabel?.textContent || "").trim(),
      stagingObjectCount: Number(stagingObjects.length || 0),
      stagingDetailObjectCount: Number(stagingDetailObjects.length || 0),
      stagedRouteStopCount: Number(stagedRouteStops.length || 0),
      maxStagedRouteStops: Number(maxStagedRouteStops),
      visibleStagingObjectCount: Number(visibleStagingObjectCount || 0),
      lightCount: Number(scene.children.filter((child) => Boolean(child && child.isLight)).length || 0),
      physicallyBasedToneMapping: renderer.toneMapping === THREE.ACESFilmicToneMapping,
      shadowMapEnabled: Boolean(renderer.shadowMap.enabled),
      renderQualityTier: String(renderQualityTier),
      rendererPixelRatio: Number(renderer.getPixelRatio().toFixed(2)),
      shadowMapSize: Number(keyLight.shadow.mapSize.width || 0),
      apartmentPlinthVisible: Boolean(apartmentPlinth.visible),
      semanticStagingGroupVisible: Boolean(semanticStagingGroup.visible),
      visibleSemanticStagingObjectCount: Number(
        semanticStagingObjects.filter((object) => objectEffectivelyVisible(object)).length,
      ),
      routeMarkerGroupVisible: Boolean(routeMarkerGroup.visible),
      styleKey,
      styleSignature: String(styleScene.style_signature || ""),
      styleEvidenceDigest,
      styleEvidenceReady: Boolean(styleEvidenceReady),
      styleCueKinds: Array.from(observedStyleCues).sort(),
      styledObjectCount: Number(styledSceneObjects.length || 0),
      styledInstanceIds: styledSceneObjects.map((object) => String(object.userData?.styleInstanceId || "")),
      visibleStyledObjectCount: Number(visibleStyledObjectCount || 0),
      visibleStyledInstanceIds,
      projectedStyledCoveragePct,
      minimumStyledCoveragePct,
      visibleStyleCueInstanceIds,
      projectedStyleCueCoveragePct,
      visibleStyleCueRayPct,
      minimumStyleCueCoveragePct,
      missingVisibleStyleCues,
      styleCueVisibilityReady,
      floorplanLayerState: floorplanLayerActive ? "on" : "off",
      floorColorHex: `#${{floor.material.color.getHexString()}}`,
      cameraInsideGeneratedDecor,
      photoPanelCount: Number(photoPanelSpecs.length || 0),
      loadedPhotoTextureCount: Number(loadedPhotoTextureCount || 0),
      visiblePhotoPanelCount: Number(visiblePhotoPanelCount || 0),
      sceneChildCount: Number(scene.children.length || 0),
      sampleWidth: Number(canvas.width || 0),
      sampleHeight: Number(canvas.height || 0),
      projectedCoveragePct: Number((Math.min(1, projectedCoverage) * 100).toFixed(2)),
      projectedPhotoCoveragePct: Number((Math.min(1, projectedPhotoCoverage) * 100).toFixed(2)),
      projectedStagingCoveragePct: Number((Math.min(1, projectedStagingCoverage) * 100).toFixed(2)),
      maxProjectedWallPct: Number((Math.min(1, maxProjectedArea) * 100).toFixed(2)),
      raycastWallFirstHitPct: Number(obstructionMetrics.wallFirstHitPct || 0),
      raycastWallObstructionPct: Number(obstructionMetrics.wallVisualObstructionPct || 0),
      raycastStagingFirstHitPct: Number(obstructionMetrics.stagingFirstHitPct || 0),
      raycastObstructionSampled: Boolean(options && options.includeObstruction),
      cameraTargetDistance: Number(camera.position.distanceTo(controls.target).toFixed(3)),
      renderCalls: Number(renderer.info.render.calls || 0),
      renderTriangles: Number(renderer.info.render.triangles || 0),
      cameraPosition: {{
        x: Number(camera.position.x.toFixed(3)),
        y: Number(camera.position.y.toFixed(3)),
        z: Number(camera.position.z.toFixed(3)),
      }},
      cameraTarget: {{
        x: Number(controls.target.x.toFixed(3)),
        y: Number(controls.target.y.toFixed(3)),
        z: Number(controls.target.z.toFixed(3)),
      }},
    }};
}}

window.__pqReconstructionDebug = {{
  setOverviewView,
  setDollhouseView,
  setInsideView,
  setRouteView,
  startGuidedRoute,
  stopGuidedRoute,
  setFloorplanLayer,
  renderCaptureFrame,
  getLiveState: () => ({{ ...liveViewerState }}),
  getRenderMetrics,
  getVisibleHotspotLabelBounds,
}};

    function handleWebGLContextLost(event) {{
      event?.preventDefault?.();
      contextLost = true;
      if (animationFrameId) {{
        window.cancelAnimationFrame(animationFrameId);
        animationFrameId = 0;
      }}
      stopGuidedRoute();
      showViewerFallback();
      announceViewerState("The interactive 3D preview stopped unexpectedly. Reload the page or use the floorplan and listing photos.");
    }}

    function disposeViewer() {{
      if (viewerDisposed) return;
      viewerDisposed = true;
      if (animationFrameId) {{
        window.cancelAnimationFrame(animationFrameId);
        animationFrameId = 0;
      }}
      resizeObserver?.disconnect?.();
      stopGuidedRoute();
      controls.dispose();
      scene.traverse((object) => {{
        object.geometry?.dispose?.();
        const materials = Array.isArray(object.material) ? object.material : [object.material];
        for (const material of materials) {{
          if (!material) continue;
          for (const value of Object.values(material)) {{
            if (value && typeof value === "object" && value.isTexture && typeof value.dispose === "function") {{
              value.dispose();
            }}
          }}
          material.dispose?.();
        }}
      }});
      renderer.dispose();
    }}

    renderer.domElement.addEventListener("webglcontextlost", handleWebGLContextLost, false);
    renderer.domElement.addEventListener("webglcontextrestored", () => {{
      if (!viewerDisposed) window.location.reload();
    }}, false);
    window.addEventListener("pagehide", disposeViewer, {{ once: true }});
    document.addEventListener("visibilitychange", () => {{
      if (
        document.visibilityState === "visible"
        && !viewerDisposed
        && !contextLost
        && !animationFrameId
        && !(shellProbeMode && renderFrameCount > 12)
      ) {{
        animationFrameId = window.requestAnimationFrame(renderFrame);
      }}
    }});
    window.__pqReconstructionDebug.simulateContextLoss = () => handleWebGLContextLost({{ preventDefault() {{}} }});
    window.__pqReconstructionDebug.disposeViewer = disposeViewer;

    function renderFrame(now = 0) {{
      animationFrameId = 0;
      if (viewerDisposed || contextLost) return;
      const transitioned = stepCameraTransition(now);
      if (!transitioned) {{
        controls.update();
      }}
      if (activeViewMode === "room") {{
        updateRoomCameraCutaway(camera.position, controls.target);
      }}
      renderer.render(scene, camera);
      syncRouteHotspots();
      renderFrameCount += 1;
      const readinessMetrics = liveViewerState.ready ? null : getRenderMetrics();
      const visibleStyleReady = Boolean(readinessMetrics?.ready);
      if (!liveViewerState.ready && visibleStyleReady) {{
        document.documentElement.dataset.viewerStatus = "ready";
        viewport.setAttribute("aria-busy", "false");
      }}
      liveViewerState.ready = Boolean(liveViewerState.ready || visibleStyleReady);
      liveViewerState.frameCount = Number(renderFrameCount || 0);
      liveViewerState.routeStopCount = Number(routeStops.length || 0);
      liveViewerState.renderCalls = Number(renderer.info.render.calls || 0);
      liveViewerState.renderTriangles = Number(renderer.info.render.triangles || 0);
      if (shellProbeMode && !routeCameraTransition.active && renderFrameCount > 12) {{
        return;
      }}
      if (document.visibilityState === "visible") {{
        animationFrameId = window.requestAnimationFrame(renderFrame);
      }}
    }}

    animationFrameId = window.requestAnimationFrame(renderFrame);
}}
</script>
</body>
</html>
"""


def _walkthrough_coverage_segments(
    labels: list[str] | tuple[str, ...],
    *,
    duration_seconds: float,
    step_seconds: float | None = None,
    segment_seconds: float | None = None,
) -> list[dict[str, object]]:
    normalized_labels = [
        _compact_route_label(label)
        for label in list(labels or [])
        if _compact_route_label(label)
    ]
    duration = float(duration_seconds or 0.0)
    if not normalized_labels or not math.isfinite(duration) or duration <= 0.0:
        return []
    step = (
        float(step_seconds)
        if step_seconds is not None
        else duration / len(normalized_labels)
    )
    segment = float(segment_seconds) if segment_seconds is not None else step
    if (
        not math.isfinite(step)
        or not math.isfinite(segment)
        or step <= 0.0
        or segment <= 0.0
    ):
        return []
    rows: list[dict[str, object]] = []
    for index, label in enumerate(normalized_labels):
        start = round(min(duration, index * step), 3)
        end = round(min(duration, (index * step) + segment), 3)
        if end <= start:
            return []
        rows.append(
            {
                "segment": label,
                "index": index + 1,
                "start": start,
                "end": end,
            }
        )
    return rows


def _walkthrough_crossfade_duration(
    seconds_per_stop: float,
    *,
    stop_count: int,
) -> float:
    if int(stop_count or 0) <= 1:
        return 0.0
    return min(0.8, max(0.35, float(seconds_per_stop) / 20.0))


def _stop_card_walkthrough_encoded_frame_count(
    *,
    stop_count: int,
    seconds_per_stop: float,
) -> int:
    normalized_stop_count = max(1, int(stop_count or 0))
    segment_frame_count = max(
        1,
        int(round(float(seconds_per_stop) * WALKTHROUGH_OUTPUT_FPS)),
    )
    transition_frame_count = int(
        round(
            _walkthrough_crossfade_duration(
                seconds_per_stop,
                stop_count=normalized_stop_count,
            )
            * WALKTHROUGH_OUTPUT_FPS
        )
    )
    return max(
        1,
        (normalized_stop_count * segment_frame_count)
        - ((normalized_stop_count - 1) * transition_frame_count),
    )


def _quality_safe_walkthrough_seconds_per_stop(
    requested_seconds_per_stop: float,
    *,
    stop_count: int,
    crossfade: bool,
) -> float:
    normalized_stop_count = max(1, int(stop_count or 0))
    seconds_per_stop = max(5.0, min(30.0, float(requested_seconds_per_stop)))
    required_frame_count = int(
        math.ceil(MIN_WALKTHROUGH_DURATION_SECONDS * WALKTHROUGH_OUTPUT_FPS)
    )
    if not crossfade:
        return min(
            30.0,
            max(
                seconds_per_stop,
                MIN_WALKTHROUGH_DURATION_SECONDS / normalized_stop_count,
            ),
        )
    frame_step_seconds = 1.0 / WALKTHROUGH_OUTPUT_FPS
    while (
        _stop_card_walkthrough_encoded_frame_count(
            stop_count=normalized_stop_count,
            seconds_per_stop=seconds_per_stop,
        )
        < required_frame_count
        and seconds_per_stop < 30.0
    ):
        seconds_per_stop = min(30.0, seconds_per_stop + frame_step_seconds)
    return seconds_per_stop


def _write_viewer_walkthrough(
    target: Path,
    *,
    viewer_path: Path,
    expected_segments: list[str],
    route_stops: list[dict[str, object]],
    seconds_per_stop: float,
    style_label: str = "",
    floorplan_thumb: Image.Image | None = None,
    route_markers: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    if sync_playwright is None:
        return {"status": "skipped", "reason": "playwright_missing"}
    if not viewer_path.is_file():
        return {"status": "skipped", "reason": "viewer_html_missing"}
    storyboard_steps = _viewer_storyboard_steps(expected_segments, route_stops=route_stops)
    if not storyboard_steps:
        return {"status": "skipped", "reason": "viewer_storyboard_unavailable"}

    fps = WALKTHROUGH_OUTPUT_FPS
    capture_frame_count = 6
    segment_frame_count = max(1, capture_frame_count)
    total_frame_count = max(1, segment_frame_count * len(storyboard_steps))
    duration_seconds = len(storyboard_steps) * seconds_per_stop
    if duration_seconds > MAX_WALKTHROUGH_DURATION_SECONDS:
        return {"status": "failed", "reason": "walkthrough_duration_limit_exceeded"}
    input_fps = max(1.0, segment_frame_count / max(seconds_per_stop, 0.001))
    sidecar_path = target.with_suffix(".quality.json")
    target.unlink(missing_ok=True)
    sidecar_path.unlink(missing_ok=True)
    last_metrics: dict[str, object] = {}
    move_phase_ratio = 0.42
    target.parent.mkdir(parents=True, exist_ok=True)
    viewer_root = viewer_path.parent

    with tempfile.TemporaryDirectory(prefix="propertyquarry-viewer-walkthrough-", dir=str(target.parent)) as tempdir:
        working_dir = Path(tempdir)
        frames_dir = working_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
        frame_paths: list[Path] = []
        try:
            with _serve_directory(viewer_root) as base_url:
                viewer_relpath = "viewer.html"
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(**_playwright_chromium_launch_kwargs(playwright))
                    page = browser.new_page(
                        viewport={"width": WALKTHROUGH_VIEWPORT_SIZE[0], "height": WALKTHROUGH_VIEWPORT_SIZE[1]},
                        device_scale_factor=1,
                    )
                    try:
                        page.goto(f"{base_url}/{viewer_relpath}?capture=1", wait_until="domcontentloaded")
                        _wait_for_playwright_condition(
                            page,
                            """() => {
                              const debug = window.__pqReconstructionDebug;
                              if (!debug || typeof debug.getRenderMetrics !== 'function') {
                                return false;
                              }
                              const metrics = debug.getRenderMetrics();
                              const photoPanelCount = Number(metrics.photoPanelCount || 0);
                              const loadedPhotoTextureCount = Number(metrics.loadedPhotoTextureCount || 0);
                              return Boolean(metrics.ready)
                                && Number(metrics.frameCount || 0) >= 2
                                && Number(metrics.renderCalls || 0) > 0
                                && Number(metrics.renderTriangles || 0) > 0
                                && (photoPanelCount === 0 || loadedPhotoTextureCount >= Math.min(photoPanelCount, 2));
                            }""",
                            timeout_ms=45_000,
                        )
                        page.wait_for_timeout(180)
                        previous_state: dict[str, object] = {"viewMode": "overview"}
                        frame_index = 0
                        for step in storyboard_steps:
                            next_state = dict(step.get("state") or {})
                            for segment_index in range(segment_frame_count):
                                phase = segment_index / max(segment_frame_count - 1, 1)
                                progress = min(1.0, phase / move_phase_ratio) if move_phase_ratio > 0 else 1.0
                                frame_payload = page.evaluate(
                                    """(payload) => {
                                      const debug = window.__pqReconstructionDebug;
                                      if (!debug || typeof debug.renderCaptureFrame !== 'function') {
                                        return {
                                          metrics: { ready: false, reason: 'render_capture_frame_unavailable' },
                                          imageDataUrl: '',
                                        };
                                      }
                                      const metrics = debug.renderCaptureFrame(payload);
                                      const canvas = document.querySelector('#viewport canvas');
                                      return {
                                        metrics,
                                        imageDataUrl: canvas && typeof canvas.toDataURL === 'function'
                                          ? canvas.toDataURL('image/jpeg', 0.92)
                                          : '',
                                      };
                                    }""",
                                    {
                                        "from": previous_state,
                                        "to": next_state,
                                        "routeIndex": next_state.get("routeIndex"),
                                        "progress": progress,
                                        "overlay": {
                                            "kicker": "Room route",
                                            "label": str(step.get("label") or ""),
                                            "sequence": int(step.get("sequence") or 0),
                                            "total": int(step.get("total") or 0),
                                        },
                                    },
                                )
                                frame_row = dict(frame_payload or {}) if isinstance(frame_payload, dict) else {}
                                metrics = frame_row.get("metrics") if isinstance(frame_row.get("metrics"), dict) else {}
                                last_metrics = dict(metrics or {}) if isinstance(metrics, dict) else {}
                                transition_frame_ready = bool(
                                    progress < 0.999
                                    and last_metrics.get("styleEvidenceReady") is True
                                    and str(last_metrics.get("styleKey") or "").strip()
                                    and str(last_metrics.get("styleEvidenceDigest") or "").strip()
                                    and str(last_metrics.get("floorplanLayerState") or "") == "off"
                                    and last_metrics.get("cameraInsideGeneratedDecor") is False
                                    and int(last_metrics.get("renderCalls") or 0) > 0
                                    and int(last_metrics.get("renderTriangles") or 0) > 0
                                )
                                if (
                                    last_metrics.get("ready") is not True
                                    and not transition_frame_ready
                                ):
                                    raise RuntimeError(
                                        (
                                            f"{str(last_metrics.get('reason') or 'viewer_render_not_ready')}"
                                            f":step={str(step.get('label') or '')}"
                                            f":progress={round(progress, 4)}"
                                            ":missing_style_cues="
                                            + ",".join(
                                                str(value)
                                                for value in list(
                                                    last_metrics.get("missingVisibleStyleCues") or []
                                                )
                                            )
                                        )
                                    )
                                image_data_url = str(frame_row.get("imageDataUrl") or "").strip()
                                if not image_data_url.startswith("data:image/jpeg;base64,"):
                                    raise RuntimeError("viewer_frame_capture_unavailable")
                                frame_path = frames_dir / f"frame-{frame_index:05d}.jpg"
                                frame_bytes = base64.b64decode(image_data_url.split(",", 1)[1])
                                with Image.open(io.BytesIO(frame_bytes)) as rendered:
                                    decorated = _decorate_viewer_walkthrough_frame(
                                        rendered,
                                        label=str(step.get("label") or ""),
                                        sequence=int(step.get("sequence") or 0),
                                        total=int(step.get("total") or 0),
                                        style_label=style_label,
                                        floorplan_thumb=floorplan_thumb,
                                        route_markers=route_markers,
                                    )
                                    decorated.save(frame_path, format="JPEG", quality=92, optimize=True)
                                    decorated.close()
                                frame_paths.append(frame_path)
                                frame_index += 1
                            previous_state = next_state
                    finally:
                        browser.close()
        except PlaywrightTimeoutError:
            if target.exists():
                target.unlink()
            return {"status": "failed", "reason": "viewer_capture_timeout"}
        except Exception as exc:
            if target.exists():
                target.unlink()
            return {"status": "failed", "reason": f"viewer_capture_failed:{exc.__class__.__name__}"}

        try:
            expected_output_frame_count = max(
                1,
                int(round(total_frame_count * fps / input_fps)),
            )
            result = _encode_rgb24_mp4(
                frames=(*frame_paths, frame_paths[-1]),
                target=target,
                frame_size=WALKTHROUGH_VIEWPORT_SIZE,
                input_fps=input_fps,
                output_fps=fps,
                expected_input_frame_count=total_frame_count + 1,
                expected_frame_count=expected_output_frame_count,
                crf=18,
                timeout_seconds=max(120, int(duration_seconds * 2.5)),
            )
        except FileNotFoundError:
            return {"status": "skipped", "reason": "ffmpeg_missing"}
        except subprocess.TimeoutExpired:
            if target.exists():
                target.unlink()
            return {"status": "failed", "reason": "ffmpeg_timeout"}
        except (OSError, ValueError):
            if target.exists():
                target.unlink()
            return {"status": "failed", "reason": "raw_video_failed"}
    if result.returncode != 0:
        if target.exists():
            target.unlink()
        return _ffmpeg_failure_receipt(result)

    duration = _validated_mp4_duration(
        target,
        expected_frame_count=expected_output_frame_count,
        fps=fps,
    )
    if duration <= 0.0:
        target.unlink(missing_ok=True)
        return {"status": "failed", "reason": "mp4_duration_validation_failed"}
    coverage_segments = _walkthrough_coverage_segments(
        expected_segments,
        duration_seconds=duration,
    )
    if len(coverage_segments) != len(expected_segments):
        target.unlink(missing_ok=True)
        sidecar_path.unlink(missing_ok=True)
        return {"status": "failed", "reason": "walkthrough_coverage_timing_invalid"}
    effective_seconds_per_stop = duration / len(expected_segments)
    coverage = {
        "status": "pass",
        "source": "propertyquarry_generated_reconstruction_viewer_capture",
        "segments_expected": expected_segments,
        "segments_visited": expected_segments,
        "coverage_segments": coverage_segments,
    }
    sidecar = {
        "provider": "PropertyQuarry generated reconstruction",
        "provider_key": "propertyquarry_generated_reconstruction",
        "composition": "viewer_route_storyboard",
        "motion_style": "threejs_layout_flythrough",
        "style_label": style_label,
        "duration_seconds": round(duration, 3),
        "seconds_per_stop": round(effective_seconds_per_stop, 3),
        "requested_seconds_per_stop": seconds_per_stop,
        "fps": fps,
        "transition_style": "dolly_then_hold",
        "transition_duration_seconds": round(effective_seconds_per_stop * move_phase_ratio, 3),
        "room_stop_count": len(expected_segments),
        "walkthrough_card_count": len(expected_segments),
        "route_map_embedded": floorplan_thumb is not None and bool(route_markers),
        "route_context_mode": (
            "viewer_capture_floorplan_inset_active_stop"
            if floorplan_thumb is not None and bool(route_markers)
            else "capture_overlay_route_progress"
        ),
        "route_labels": expected_segments,
        "covered_route_labels": expected_segments,
        "viewer_capture_mode": True,
        "viewer_capture_metrics": {
            key: value
            for key, value in last_metrics.items()
            if key
            in {
                "wallMeshCount",
                "visibleWallCount",
                "photoPanelCount",
                "loadedPhotoTextureCount",
                "captureMode",
                "captureRouteLabel",
                "projectedCoveragePct",
                "projectedPhotoCoveragePct",
                "renderTriangles",
            }
        },
        "walkthrough_coverage_proof": coverage,
        "disclosure": DISCLOSURE,
    }
    sidecar_path.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "status": "generated",
        "relpath": target.name,
        "sidecar_relpath": sidecar_path.name,
        "sha256": _sha256(target),
        "sidecar_sha256": _sha256(sidecar_path),
        "size_bytes": target.stat().st_size,
        "duration_seconds": round(duration, 3),
        "composition": str(sidecar.get("composition") or ""),
        "motion_style": str(sidecar.get("motion_style") or ""),
        "coverage_proof": coverage,
    }


def _stop_card_motion_window(
    *,
    stop_index: int,
    label: str,
    card_size: tuple[int, int],
    viewport_size: tuple[int, int],
) -> tuple[float, float, float, float]:
    card_w, card_h = card_size
    viewport_w, viewport_h = viewport_size
    x_margin = max(0, card_w - viewport_w)
    y_margin = max(0, card_h - viewport_h)
    label_kind = _route_label_kind(label)
    x_span = min(float(x_margin), 36.0 if label_kind in {"living", "dining", "kitchen"} else 28.0)
    y_span = min(float(y_margin), 18.0 if label_kind in {"bath", "toilet", "storage"} else 12.0)
    if stop_index % 2 == 0:
        x_start = min(float(x_margin), 18.0)
        x_end = min(float(x_margin), x_start + x_span)
    else:
        x_end = max(0.0, float(x_margin) - 18.0)
        x_start = max(0.0, x_end - x_span)
    y_start = min(float(y_margin), 12.0 + ((stop_index % 3) * 4.0))
    y_end = min(float(y_margin), y_start + y_span)
    return x_start, x_end, y_start, y_end


def _render_stop_card_motion_frame(
    card_image: Image.Image,
    *,
    motion_window: tuple[float, float, float, float],
    progress: float,
    viewport_size: tuple[int, int],
) -> Image.Image:
    viewport_w, viewport_h = viewport_size
    x_start, x_end, y_start, y_end = motion_window
    x = x_start + ((x_end - x_start) * progress)
    y = y_start + ((y_end - y_start) * progress)
    max_x = max(0, card_image.width - viewport_w)
    max_y = max(0, card_image.height - viewport_h)
    left = int(round(_clamp_float(x, 0.0, float(max_x))))
    top = int(round(_clamp_float(y, 0.0, float(max_y))))
    return card_image.crop((left, top, left + viewport_w, top + viewport_h)).convert("RGB")


def _write_stop_card_walkthrough(
    target: Path,
    images: list[Path],
    *,
    style_label: str = "",
    route_labels: list[str] | tuple[str, ...] = (),
    room_count: int = 0,
    walkable_scene: dict[str, object] | None = None,
) -> dict[str, object]:
    sidecar_path = target.with_suffix(".quality.json")
    target.unlink(missing_ok=True)
    sidecar_path.unlink(missing_ok=True)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return {"status": "skipped", "reason": "ffmpeg_missing"}
    if not images:
        return {"status": "skipped", "reason": "source_images_missing"}
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="propertyquarry-reconstruction-", dir=str(target.parent)) as tempdir:
        working_dir = Path(tempdir)
        try:
            seconds_per_stop = float(
                os.getenv("PROPERTYQUARRY_RECONSTRUCTION_WALKTHROUGH_SECONDS_PER_STOP")
                or os.getenv("PROPERTYQUARRY_FLYTHROUGH_SECONDS_PER_ROUTE_STOP")
                or "5"
            )
        except Exception:
            seconds_per_stop = 5.0
        seconds_per_stop = max(5.0, min(30.0, seconds_per_stop))
        normalized_route_labels = [
            _compact_route_label(label)
            for label in list(route_labels or [])
            if _compact_route_label(label)
        ]
        floorplan_image = images[0] if len(images) > 1 else None
        photo_images = list(images[1:]) if len(images) > 1 else list(images)
        expected_segments = list(normalized_route_labels)
        if not expected_segments:
            fallback_stop_count = max(_numeric_room_count(room_count), len(photo_images) or len(images))
            if fallback_stop_count <= 0:
                fallback_stop_count = 1
            expected_segments = [f"Room view {index:02d}" for index in range(1, fallback_stop_count + 1)]
        requested_seconds_per_stop = seconds_per_stop
        seconds_per_stop = _quality_safe_walkthrough_seconds_per_stop(
            requested_seconds_per_stop,
            stop_count=len(expected_segments),
            crossfade=True,
        )
        duration_seconds = max(
            seconds_per_stop,
            len(expected_segments) * seconds_per_stop,
        )
        if duration_seconds > MAX_WALKTHROUGH_DURATION_SECONDS:
            return {"status": "failed", "reason": "walkthrough_duration_limit_exceeded"}
        fps = WALKTHROUGH_OUTPUT_FPS
        segment_frame_count = max(1, int(round(seconds_per_stop * fps)))
        viewport_w, viewport_h = WALKTHROUGH_VIEWPORT_SIZE
        card_w, card_h = WALKTHROUGH_CARD_SIZE

        floorplan_thumb = _walkthrough_floorplan_thumb(floorplan_image)
        route_markers = _walkthrough_route_markers(expected_segments, walkable_scene=walkable_scene)

        stop_card_paths: list[Path] = []
        source_images = photo_images or list(images)
        for index, label in enumerate(expected_segments):
            image_path = source_images[index % max(len(source_images), 1)]
            supporting_path = None
            if len(photo_images) > 1:
                supporting_index = (index + 1) % len(photo_images)
                candidate = photo_images[supporting_index]
                if candidate != image_path:
                    supporting_path = candidate
            card_path = working_dir / f"walkthrough-stop-{index + 1:02d}.jpg"
            stop_card = _render_walkthrough_stop_card(
                stop_index=index,
                label=label,
                expected_segments=expected_segments,
                source_path=image_path,
                supporting_path=supporting_path,
                floorplan_thumb=floorplan_thumb,
                route_markers=route_markers,
                style_label=style_label,
            )
            stop_card.save(card_path, format="JPEG", quality=92)
            stop_card_paths.append(card_path)

        try:
            configured_timeout_seconds = int(
                float(str(os.getenv("PROPERTYQUARRY_RECONSTRUCTION_FFMPEG_TIMEOUT_SECONDS") or "0").strip() or "0")
            )
        except Exception:
            configured_timeout_seconds = 0
        timeout_seconds = max(300, int(duration_seconds * 8), configured_timeout_seconds)
        transition_duration = _walkthrough_crossfade_duration(
            seconds_per_stop,
            stop_count=len(stop_card_paths),
        )
        transition_frame_count = int(round(transition_duration * fps)) if transition_duration > 0 else 0
        encoded_frame_count = 0
        for index in range(len(stop_card_paths)):
            incoming_transition_frames = transition_frame_count if index > 0 else 0
            outgoing_transition_frames = (
                transition_frame_count if index + 1 < len(stop_card_paths) else 0
            )
            encoded_frame_count += max(
                1,
                segment_frame_count
                - incoming_transition_frames
                - outgoing_transition_frames,
            )
            encoded_frame_count += outgoing_transition_frames
        if encoded_frame_count > MAX_WALKTHROUGH_ENCODED_FRAMES:
            return {"status": "failed", "reason": "walkthrough_frame_limit_exceeded"}

        def iter_motion_frames() -> Iterator[Image.Image]:
            card_images: list[Image.Image] = []
            for path in stop_card_paths:
                with Image.open(path) as source:
                    source.load()
                    card_images.append(source.convert("RGB"))
            motion_windows = [
                _stop_card_motion_window(
                    stop_index=index,
                    label=expected_segments[index],
                    card_size=(card_w, card_h),
                    viewport_size=(viewport_w, viewport_h),
                )
                for index in range(len(stop_card_paths))
            ]
            try:
                for index, card_image in enumerate(card_images):
                    next_image = (
                        card_images[index + 1]
                        if index + 1 < len(card_images)
                        else None
                    )
                    incoming_transition_frames = transition_frame_count if index > 0 else 0
                    transition_frames_for_segment = (
                        transition_frame_count if next_image is not None else 0
                    )
                    steady_frame_count = max(
                        1,
                        segment_frame_count
                        - incoming_transition_frames
                        - transition_frames_for_segment,
                    )
                    for steady_index in range(steady_frame_count):
                        progress = steady_index / max(steady_frame_count - 1, 1)
                        frame = _render_stop_card_motion_frame(
                            card_image,
                            motion_window=motion_windows[index],
                            progress=progress,
                            viewport_size=(viewport_w, viewport_h),
                        )
                        try:
                            yield frame
                        finally:
                            frame.close()
                    if next_image is None or transition_frames_for_segment <= 0:
                        continue
                    current_frame = _render_stop_card_motion_frame(
                        card_image,
                        motion_window=motion_windows[index],
                        progress=1.0,
                        viewport_size=(viewport_w, viewport_h),
                    )
                    next_frame = _render_stop_card_motion_frame(
                        next_image,
                        motion_window=motion_windows[index + 1],
                        progress=0.0,
                        viewport_size=(viewport_w, viewport_h),
                    )
                    try:
                        for transition_index in range(transition_frames_for_segment):
                            alpha = (transition_index + 1) / max(
                                transition_frames_for_segment + 1,
                                1,
                            )
                            frame = Image.blend(current_frame, next_frame, alpha)
                            try:
                                yield frame
                            finally:
                                frame.close()
                    finally:
                        current_frame.close()
                        next_frame.close()
            finally:
                for card_image in card_images:
                    card_image.close()

        try:
            result = _encode_rgb24_mp4(
                frames=iter_motion_frames(),
                target=target,
                frame_size=WALKTHROUGH_VIEWPORT_SIZE,
                input_fps=float(fps),
                output_fps=fps,
                expected_input_frame_count=encoded_frame_count,
                expected_frame_count=encoded_frame_count,
                crf=20,
                timeout_seconds=timeout_seconds,
            )
        except FileNotFoundError:
            return {"status": "skipped", "reason": "ffmpeg_missing"}
        except subprocess.TimeoutExpired:
            if target.exists():
                target.unlink()
            return {"status": "failed", "reason": "ffmpeg_timeout"}
        except (OSError, ValueError):
            if target.exists():
                target.unlink()
            return {"status": "failed", "reason": "raw_video_failed"}
    if result.returncode != 0:
        if target.exists():
            target.unlink()
        return _ffmpeg_failure_receipt(result)
    duration = _validated_mp4_duration(
        target,
        expected_frame_count=encoded_frame_count,
        fps=fps,
    )
    if duration <= 0.0:
        target.unlink(missing_ok=True)
        return {"status": "failed", "reason": "mp4_duration_validation_failed"}
    coverage_step_seconds = max(0.0, seconds_per_stop - transition_duration)
    coverage_segments = _walkthrough_coverage_segments(
        expected_segments,
        duration_seconds=duration,
        step_seconds=coverage_step_seconds,
        segment_seconds=seconds_per_stop,
    )
    if len(coverage_segments) != len(expected_segments):
        target.unlink(missing_ok=True)
        sidecar_path.unlink(missing_ok=True)
        return {"status": "failed", "reason": "walkthrough_coverage_timing_invalid"}
    coverage = {
        "status": "pass",
        "source": "propertyquarry_generated_reconstruction_stop_cards",
        "segments_expected": expected_segments,
        "segments_visited": expected_segments,
        "coverage_segments": coverage_segments,
    }
    sidecar = {
        "provider": "PropertyQuarry generated reconstruction",
        "provider_key": "propertyquarry_generated_reconstruction",
        "composition": "route_focused_stop_cards",
        "motion_style": "ken_burns_route_cards",
        "style_label": style_label,
        "duration_seconds": round(duration, 3),
        "seconds_per_stop": seconds_per_stop,
        "requested_seconds_per_stop": requested_seconds_per_stop,
        "transition_style": "crossfade" if transition_duration > 0 else "hard_cut",
        "transition_duration_seconds": round(transition_duration, 3),
        "room_stop_count": len(expected_segments),
        "walkthrough_card_count": len(stop_card_paths),
        "route_map_embedded": floorplan_thumb is not None and bool(route_markers),
        "route_context_mode": "floorplan_inset_active_stop" if floorplan_thumb is not None and bool(route_markers) else "text_only_progress",
        "route_labels": expected_segments,
        "covered_route_labels": expected_segments,
        "walkthrough_coverage_proof": coverage,
        "disclosure": DISCLOSURE,
    }
    sidecar_path.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "status": "generated",
        "relpath": target.name,
        "sidecar_relpath": sidecar_path.name,
        "sha256": _sha256(target),
        "sidecar_sha256": _sha256(sidecar_path),
        "size_bytes": target.stat().st_size,
        "duration_seconds": round(duration, 3),
        "composition": str(sidecar.get("composition") or ""),
        "motion_style": str(sidecar.get("motion_style") or ""),
        "coverage_proof": coverage,
    }


def _write_walkthrough(
    target: Path,
    images: list[Path],
    *,
    style_label: str = "",
    route_labels: list[str] | tuple[str, ...] = (),
    room_count: int = 0,
    walkable_scene: dict[str, object] | None = None,
    viewer_path: Path | None = None,
) -> dict[str, object]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return {"status": "skipped", "reason": "ffmpeg_missing"}
    if not images:
        return {"status": "skipped", "reason": "source_images_missing"}

    try:
        seconds_per_stop = float(
            os.getenv("PROPERTYQUARRY_RECONSTRUCTION_WALKTHROUGH_SECONDS_PER_STOP")
            or os.getenv("PROPERTYQUARRY_FLYTHROUGH_SECONDS_PER_ROUTE_STOP")
            or "5"
        )
    except Exception:
        seconds_per_stop = 5.0
    seconds_per_stop = max(5.0, min(30.0, seconds_per_stop))
    normalized_route_labels = [
        _compact_route_label(label)
        for label in list(route_labels or [])
        if _compact_route_label(label)
    ]
    expected_segments = list(normalized_route_labels)
    if not expected_segments:
        photo_images = list(images[1:]) if len(images) > 1 else list(images)
        fallback_stop_count = max(_numeric_room_count(room_count), len(photo_images) or len(images))
        if fallback_stop_count <= 0:
            fallback_stop_count = 1
        expected_segments = [f"Room view {index:02d}" for index in range(1, fallback_stop_count + 1)]
    seconds_per_stop = _quality_safe_walkthrough_seconds_per_stop(
        seconds_per_stop,
        stop_count=len(expected_segments),
        crossfade=False,
    )

    route_stops = (
        [dict(stop) for stop in list((walkable_scene or {}).get("route") or []) if isinstance(stop, dict)]
        if isinstance(walkable_scene, dict)
        else []
    )
    floorplan_image = images[0] if len(images) > 1 else None
    floorplan_thumb = _walkthrough_floorplan_thumb(floorplan_image)
    route_markers = _walkthrough_route_markers(expected_segments, walkable_scene=walkable_scene)
    viewer_walkthrough_required = _env_flag("PROPERTYQUARRY_RECONSTRUCTION_VIEWER_WALKTHROUGH_REQUIRED")
    viewer_walkthrough_disabled = _env_flag("PROPERTYQUARRY_RECONSTRUCTION_DISABLE_VIEWER_WALKTHROUGH")
    viewer_walkthrough_runtime_default_disabled = (
        str(os.getenv("EA_ROLE") or "").strip().lower() == "render-tools"
        and not viewer_walkthrough_required
        and not _env_flag("PROPERTYQUARRY_RECONSTRUCTION_ENABLE_VIEWER_WALKTHROUGH")
    )
    viewer_walkthrough_enabled = (
        viewer_walkthrough_required
        or _env_flag("PROPERTYQUARRY_RECONSTRUCTION_ENABLE_VIEWER_WALKTHROUGH")
        or (
            viewer_path is not None
            and sync_playwright is not None
            and not viewer_walkthrough_disabled
            and not viewer_walkthrough_runtime_default_disabled
        )
    )
    if viewer_walkthrough_enabled and viewer_path is not None:
        viewer_receipt = _write_viewer_walkthrough(
            target,
            viewer_path=viewer_path,
            expected_segments=expected_segments,
            route_stops=route_stops,
            seconds_per_stop=seconds_per_stop,
            style_label=style_label,
            floorplan_thumb=floorplan_thumb,
            route_markers=route_markers,
        )
        if viewer_receipt.get("status") == "generated":
            return viewer_receipt
        if viewer_walkthrough_required:
            return viewer_receipt

    return _write_stop_card_walkthrough(
        target,
        images,
        style_label=style_label,
        route_labels=expected_segments,
        room_count=room_count,
        walkable_scene=walkable_scene,
    )


def _open_relative_directory_components(
    anchor_fd: int,
    parts: tuple[str, ...],
    *,
    create_missing: bool,
    failure: str,
) -> int:
    current_fd = -1
    try:
        current_fd = os.dup(anchor_fd)
        for part in parts:
            if part in {"", ".", ".."}:
                raise _PublicBundleTransactionError(failure)
            if create_missing:
                try:
                    os.mkdir(part, 0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
            next_fd = _open_public_bundle_directory(
                part,
                parent_fd=current_fd,
                failure=failure,
            )
            os.close(current_fd)
            current_fd = next_fd
        result_fd = current_fd
        current_fd = -1
        return result_fd
    except _PublicBundleTransactionError:
        raise
    except OSError as exc:
        raise _PublicBundleTransactionError(failure) from exc
    finally:
        if current_fd >= 0:
            os.close(current_fd)


def _reset_owned_generation_surface(
    *,
    bundle_fd: int,
    target_subdir: str,
) -> None:
    parts = PurePosixPath(target_subdir).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise _PublicBundleTransactionError("owned_generation_surface_invalid")
    parent_fd = _open_relative_directory_components(
        bundle_fd,
        parts[:-1],
        create_missing=True,
        failure="owned_generation_surface_invalid",
    )
    output_fd = -1
    try:
        output_metadata = os.stat(
            parts[-1],
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        output_metadata = None
    except OSError as exc:
        os.close(parent_fd)
        raise _PublicBundleTransactionError(
            "owned_generation_surface_invalid"
        ) from exc
    try:
        if output_metadata is not None:
            if not stat.S_ISDIR(output_metadata.st_mode) or stat.S_ISLNK(
                output_metadata.st_mode
            ):
                raise _PublicBundleTransactionError(
                    "owned_generation_surface_invalid"
                )
            output_fd = _open_public_bundle_directory(
                parts[-1],
                parent_fd=parent_fd,
                failure="owned_generation_surface_invalid",
            )
            opened_output = os.fstat(output_fd)
            _remove_tree_contents(output_fd)
            if not _directory_path_identity_matches(
                parent_fd=parent_fd,
                name=parts[-1],
                expected=opened_output,
            ):
                raise _PublicBundleTransactionError(
                    "owned_generation_surface_reset_refused"
                )
            os.rmdir(parts[-1], dir_fd=parent_fd)
            os.close(output_fd)
            output_fd = -1
            os.fsync(parent_fd)
        for preview_name in ("diorama-preview.png", "telegram-preview.png"):
            try:
                preview_metadata = os.stat(
                    preview_name,
                    dir_fd=bundle_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise _PublicBundleTransactionError(
                    "owned_generation_preview_invalid"
                ) from exc
            if not stat.S_ISREG(preview_metadata.st_mode):
                raise _PublicBundleTransactionError(
                    "owned_generation_preview_invalid"
                )
            preview_fd = _open_regular_public_entry(
                bundle_fd,
                preview_name,
                failure="owned_generation_preview_invalid",
            )
            try:
                opened_preview = os.fstat(preview_fd)
            finally:
                os.close(preview_fd)
            current_preview = os.stat(
                preview_name,
                dir_fd=bundle_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(current_preview.st_mode)
                or current_preview.st_dev != opened_preview.st_dev
                or current_preview.st_ino != opened_preview.st_ino
            ):
                raise _PublicBundleTransactionError(
                    "owned_generation_preview_changed"
                )
            os.unlink(preview_name, dir_fd=bundle_fd)
        os.fsync(bundle_fd)
    except _PublicBundleTransactionError:
        raise
    except OSError as exc:
        raise _PublicBundleTransactionError(
            "owned_generation_surface_reset_failed"
        ) from exc
    finally:
        if output_fd >= 0:
            os.close(output_fd)
        os.close(parent_fd)


def _create_owned_generation_surface(
    *,
    bundle_fd: int,
    target_subdir: str,
) -> int:
    parts = PurePosixPath(target_subdir).parts
    parent_fd = _open_relative_directory_components(
        bundle_fd,
        parts[:-1],
        create_missing=True,
        failure="owned_generation_surface_invalid",
    )
    try:
        os.mkdir(parts[-1], 0o700, dir_fd=parent_fd)
        output_fd = _open_public_bundle_directory(
            parts[-1],
            parent_fd=parent_fd,
            failure="owned_generation_surface_invalid",
        )
        os.fsync(parent_fd)
        return output_fd
    except _PublicBundleTransactionError:
        raise
    except OSError as exc:
        raise _PublicBundleTransactionError(
            "owned_generation_surface_create_failed"
        ) from exc
    finally:
        os.close(parent_fd)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a PropertyQuarry reconstruction from a floorplan image and photos.")
    parser.add_argument("--slug", required=True, help="Existing PropertyQuarry public tour slug.")
    parser.add_argument("--floorplan", default="", help="Floorplan image. PDF support is intentionally not implied here.")
    parser.add_argument("--photo", action="append", default=[], help="Source property photo. Can be provided multiple times.")
    parser.add_argument("--target-subdir", default="generated-reconstruction")
    parser.add_argument("--max-width-m", type=float, default=10.0)
    parser.add_argument("--style-label", default="", help="Human-readable staging style label for receipts and walkthrough overlays.")
    parser.add_argument("--style-id", default="", help="Canonical PropertyQuarry furnishing style identifier.")
    parser.add_argument("--style-signature", default="", help="Expected canonical style signature; mismatches fail closed.")
    parser.add_argument("--room-label", action="append", default=[], help="Optional explicit walkthrough stop label. Can be provided multiple times.")
    parser.add_argument("--room-count", type=int, default=0, help="Optional explicit walkthrough stop count when no labels are available.")
    parser.add_argument(
        "--infer-floorplan-from-photos",
        action="store_true",
        help="Generate a disclosed schematic floorplan when no real floorplan image is available.",
    )
    parser.add_argument("--skip-video", action="store_true")
    return parser.parse_args(argv)


def _generate_reconstruction(
    args: argparse.Namespace,
    *,
    public_root: Path,
    bundle_dir: Path,
    bundle_uses_shared_runtime_root: bool,
) -> tuple[int, dict[str, object] | None]:
    slug = _validated_tour_slug(args.slug)
    target_subdir = _safe_relpath(args.target_subdir) or "generated-reconstruction"
    bundle_fd = -1
    output_fd = -1
    manifest_fd = -1
    try:
        try:
            bundle_fd, _ = _open_directory_anchor(bundle_dir)
        except OSError as exc:
            raise _PublicBundleTransactionError(
                "generation_bundle_anchor_invalid"
            ) from exc
        manifest_fd = _open_regular_public_entry(
            bundle_fd,
            "tour.json",
            failure="tour_manifest_missing",
        )
        os.close(manifest_fd)
        manifest_fd = -1
        _reset_owned_generation_surface(
            bundle_fd=bundle_fd,
            target_subdir=target_subdir,
        )
        output_fd = _create_owned_generation_surface(
            bundle_fd=bundle_fd,
            target_subdir=target_subdir,
        )
        return _generate_reconstruction_on_anchored_surface(
            args,
            public_root=public_root,
            bundle_dir=Path(f"/proc/self/fd/{bundle_fd}"),
            output_dir=Path(f"/proc/self/fd/{output_fd}"),
            target_subdir=target_subdir,
            slug=slug,
            bundle_uses_shared_runtime_root=bundle_uses_shared_runtime_root,
        )
    finally:
        for descriptor in (manifest_fd, output_fd, bundle_fd):
            if descriptor >= 0:
                os.close(descriptor)


def _generate_reconstruction_on_anchored_surface(
    args: argparse.Namespace,
    *,
    public_root: Path,
    bundle_dir: Path,
    output_dir: Path,
    target_subdir: str,
    slug: str,
    bundle_uses_shared_runtime_root: bool,
) -> tuple[int, dict[str, object] | None]:
    manifest_path = bundle_dir / "tour.json"

    photo_sources = [Path(value).expanduser() for value in args.photo or []]
    floorplan_arg = str(args.floorplan or "").strip()
    if floorplan_arg:
        floorplan_source = Path(floorplan_arg).expanduser()
        if not floorplan_source.is_file():
            raise SystemExit("floorplan_missing")
        floorplan_target = output_dir / f"source-floorplan{_web_safe_image_suffix(floorplan_source)}"
        floorplan_meta = _copy_normalized_image(floorplan_source, floorplan_target)
        floorplan_meta["relpath"] = floorplan_target.name
    elif args.infer_floorplan_from_photos:
        if not photo_sources:
            raise SystemExit("floorplan_or_photos_required")
        floorplan_target = output_dir / "source-floorplan-inferred.jpg"
        floorplan_meta = _write_inferred_floorplan(floorplan_target, photo_count=len(photo_sources))
    else:
        raise SystemExit("floorplan_missing")

    photo_rows: list[dict[str, object]] = []
    photo_paths: list[Path] = []
    for index, source in enumerate(photo_sources, start=1):
        if not source.is_file():
            raise SystemExit(f"photo_missing:{index}")
        target = output_dir / f"photo-{index:02d}{_web_safe_image_suffix(source)}"
        row = _copy_normalized_image(source, target)
        row["relpath"] = target.name
        row["index"] = index
        photo_rows.append(row)
        photo_paths.append(target)

    geometry = _extract_floorplan_geometry(floorplan_target)
    geometry_content_size = dict(geometry.get("content_size_px") or {})
    width_m, depth_m, height_m = _room_dimensions(
        int(geometry_content_size.get("width") or floorplan_meta["width"]),
        int(geometry_content_size.get("height") or floorplan_meta["height"]),
        max_width_m=max(3.0, float(args.max_width_m)),
    )
    extracted_wall_rectangles = _wall_rectangles_from_mask(
        list(geometry.get("wall_mask") or []),
        width_m=width_m,
        depth_m=depth_m,
    )
    wall_rectangles, boundary_completion = _wall_rectangles_with_boundary_completion(
        extracted_wall_rectangles,
        width_m=width_m,
        depth_m=depth_m,
    )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit("invalid_tour_manifest")
    payload.pop("generated_reconstruction", None)
    for preview_key in (
        "diorama_preview_relpath",
        "telegram_preview_relpath",
    ):
        if str(payload.get(preview_key) or "").strip() in {
            "diorama-preview.png",
            "telegram-preview.png",
        }:
            payload.pop(preview_key, None)
    if str(payload.get("preview_relpath") or "").strip() in {
        "diorama-preview.png",
        "telegram-preview.png",
    }:
        payload.pop("preview_relpath", None)
    public_assets = payload.get("public_assets")
    if isinstance(public_assets, list):
        payload["public_assets"] = [
            row
            for row in public_assets
            if not (
                isinstance(row, dict)
                and (
                    str(row.get("relpath") or "").strip().startswith(
                        f"{target_subdir}/"
                    )
                    or str(row.get("relpath") or "").strip()
                    in {"diorama-preview.png", "telegram-preview.png"}
                )
            )
        ]
    source_images = [floorplan_target, *photo_paths]
    route_labels = _reconstruction_walkthrough_route_labels(
        payload,
        explicit_labels=list(args.room_label or []),
        explicit_room_count=int(args.room_count or 0),
    )
    walkthrough_route_labels = _walkthrough_stop_labels(route_labels, target_stop_count=len(photo_rows))
    walkable_scene = _reconstruction_walkable_scene(
        route_labels=route_labels,
        width_m=width_m,
        depth_m=depth_m,
        height_m=height_m,
        geometry=geometry,
    )
    photo_reference_panels = _generated_reconstruction_photo_reference_panels(
        photos=photo_rows,
        walkable_scene=walkable_scene,
        width_m=width_m,
        depth_m=depth_m,
        height_m=height_m,
    )
    try:
        selected_style = reconstruction_style(
            str(args.style_label or "").strip(),
            style_id=str(getattr(args, "style_id", "") or "").strip(),
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    expected_style_signature = str(getattr(args, "style_signature", "") or "").strip()
    if expected_style_signature and expected_style_signature != str(selected_style.get("signature") or ""):
        raise SystemExit("property_reconstruction_style_signature_mismatch")
    style_label = str(selected_style.get("label") or "").strip()
    style_scene = build_style_scene(
        selected_style,
        route_stop_count=len(route_labels),
    )
    style_scene_valid, style_scene_reason = validate_style_scene(
        style_scene,
        expected_style=selected_style,
    )
    if not style_scene_valid:
        raise SystemExit(style_scene_reason)
    _write_obj(
        output_dir,
        width_m=width_m,
        depth_m=depth_m,
        height_m=height_m,
        wall_rectangles=wall_rectangles,
        style_scene=style_scene,
    )
    glb_export = _write_glb(output_dir, style_scene=style_scene)
    floorplan_inferred = bool(floorplan_meta.get("inferred"))
    source_disclosure = _generated_reconstruction_disclosure(
        photo_count=len(photo_rows),
        floorplan_inferred=floorplan_inferred,
    )
    diorama_preview = _write_generated_reconstruction_diorama_preview(
        bundle_dir / "diorama-preview.png",
        floorplan_path=floorplan_target,
        photo_paths=photo_paths,
        walkable_scene=walkable_scene,
        style_label=style_label,
        style_scene=style_scene,
        floorplan_inferred=floorplan_inferred,
    )
    telegram_preview = (
        _write_generated_reconstruction_telegram_preview(
            bundle_dir / "telegram-preview.png",
            source_path=bundle_dir / "diorama-preview.png",
            style_label=style_label,
            style_scene=style_scene,
        )
        if str(diorama_preview.get("status") or "").strip() == "generated"
        else {"status": "skipped", "reason": "diorama_preview_unavailable"}
    )
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    receipt: dict[str, object] = {
        "provider": "propertyquarry_generated_reconstruction",
        "generated_at": generated_at,
        "slug": slug,
        "disclosure": source_disclosure,
        "verified_provider_capture": False,
        "satisfies_verified_tour_gate": False,
        "style_label": style_label,
        "requested_style": selected_style,
        "style_scene": style_scene,
        "method": (
            "photo_inferred_schematic_with_source_photo_reference_panels"
            if floorplan_inferred
            else "floorplan_directional_wall_segments_with_source_photo_reference_panels"
        ),
        "room_dimensions_m": {"width": width_m, "depth": depth_m, "height": height_m},
        "geometry": {
            "content_bbox_px": dict(geometry.get("content_bbox_px") or {}),
            "content_size_px": dict(geometry.get("content_size_px") or {}),
            "mask_size_cells": dict(geometry.get("mask_size_cells") or {}),
            "extraction_method": str(geometry.get("extraction_method") or ""),
            "floor_texture_crop": _floorplan_texture_crop(geometry, floorplan_meta),
            "floorplan_inferred": floorplan_inferred,
            "wall_rectangles": wall_rectangles,
            "wall_rect_count": len(wall_rectangles),
            "extracted_wall_rect_count": len(extracted_wall_rectangles),
            "boundary_completion": boundary_completion,
        },
        "floorplan": floorplan_meta,
        "photos": photo_rows,
        "walkable_scene": walkable_scene,
        "photo_reference_panels": photo_reference_panels,
        "model": {
            "obj_relpath": "model.obj",
            "mtl_relpath": "model.mtl",
            "obj_sha256": _sha256(output_dir / "model.obj"),
            "mtl_sha256": _sha256(output_dir / "model.mtl"),
            "glb_export": glb_export,
        },
        "viewer": {
            "relpath": "viewer.html",
            "version": VIEWER_VERSION,
            "photo_reference_panel_count": len(photo_reference_panels),
            "style_id": str(selected_style.get("id") or ""),
            "style_signature": str(selected_style.get("signature") or ""),
            "style_scene_signature": str(style_scene.get("scene_signature") or ""),
            "floorplan_display_mode": FLOORPLAN_DISPLAY_MODE,
        },
        "bundle_preview_assets": {
            "diorama": diorama_preview,
            "telegram": telegram_preview,
        },
        "walkthrough": {"status": "pending", "reason": "viewer_render_not_started"},
        "route_labels": route_labels,
        "walkthrough_route_labels": walkthrough_route_labels,
    }
    if glb_export.get("status") == "generated":
        receipt["model"]["glb_relpath"] = str(glb_export.get("glb_relpath") or "model.glb")
        receipt["model"]["glb_sha256"] = str(glb_export.get("glb_sha256") or "")
        receipt["model"]["glb_size_bytes"] = int(glb_export.get("glb_size_bytes") or 0)
    vendor_assets = _copy_viewer_vendor_assets(output_dir)
    receipt["viewer"]["vendor"] = dict(vendor_assets.get("provenance") or {})
    viewer_path = output_dir / "viewer.html"
    viewer_path.write_text(
        _viewer_html(
            manifest=receipt,
            three_relpath=str(vendor_assets.get("three_relpath") or "vendor/three.module.js"),
            orbit_controls_relpath=str(vendor_assets.get("orbit_controls_relpath") or "vendor/examples/jsm/controls/OrbitControls.js"),
        ),
        encoding="utf-8",
    )
    if args.skip_video:
        (output_dir / "generated-walkthrough.mp4").unlink(missing_ok=True)
        (output_dir / "generated-walkthrough.quality.json").unlink(missing_ok=True)
    walkthrough = (
        {"status": "skipped", "reason": "skip_video_requested"}
        if args.skip_video
        else _write_walkthrough(
            output_dir / "generated-walkthrough.mp4",
            source_images,
            style_label=style_label,
            route_labels=walkthrough_route_labels,
            room_count=int(args.room_count or 0),
            walkable_scene=walkable_scene,
            viewer_path=viewer_path,
        )
    )
    receipt["walkthrough"] = walkthrough
    required_artifact_failures: list[str] = []
    if not args.skip_video and walkthrough.get("status") != "generated":
        required_artifact_failures.append("walkthrough")
    if required_artifact_failures:
        receipt["status"] = "failed"
        receipt["reason"] = "required_render_artifact_failed"
        receipt["failed_artifacts"] = required_artifact_failures
        (output_dir / "reconstruction.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": "required_render_artifact_failed",
                    "failed_artifacts": required_artifact_failures,
                    "glb_status": glb_export.get("status"),
                    "walkthrough_status": walkthrough.get("status"),
                },
                ensure_ascii=False,
            )
        )
        return 1, None
    (output_dir / "reconstruction.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    receipt["viewer"]["sha256"] = _sha256(output_dir / "viewer.html")
    (output_dir / "reconstruction.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    base_relpath = PurePosixPath(target_subdir).as_posix()
    generated_reconstruction = {
        "provider": "propertyquarry_generated_reconstruction",
        "generated_at": generated_at,
        "viewer_version": VIEWER_VERSION,
        "viewer_relpath": f"{base_relpath}/viewer.html",
        "model_relpath": f"{base_relpath}/model.obj",
        "material_relpath": f"{base_relpath}/model.mtl",
        "manifest_relpath": f"{base_relpath}/reconstruction.json",
        "glb_export_status": str(glb_export.get("status") or ""),
        "verified_provider_capture": False,
        "satisfies_verified_tour_gate": False,
        "disclosure": source_disclosure,
        "style_contract_version": STYLE_SCENE_CONTRACT_VERSION,
        "style_id": str(selected_style.get("id") or ""),
        "style_label": style_label,
        "style_signature": str(selected_style.get("signature") or ""),
        "style_scene_signature": str(style_scene.get("scene_signature") or ""),
        "style_evidence_status": str(style_scene.get("evidence_status") or ""),
        "styled_scene_instance_count": len(list(style_scene.get("instances") or [])),
        "style_cue_kinds": list(style_scene.get("required_cues") or []),
        "floorplan_display_mode": FLOORPLAN_DISPLAY_MODE,
        "route_labels": route_labels,
        "room_stop_count": len(route_labels),
        "walkthrough_route_labels": walkthrough_route_labels,
        "walkthrough_stop_count": len(walkthrough_route_labels),
        "photo_reference_panel_count": len(photo_reference_panels),
        "walkable_scene": walkable_scene,
        "walkable_scene_kind": str(walkable_scene.get("kind") or "").strip(),
    }
    floorplan_relpath = str(dict(receipt.get("floorplan") or {}).get("relpath") or "").strip()
    if floorplan_relpath:
        generated_reconstruction["floorplan_relpath"] = f"{base_relpath}/{floorplan_relpath}"
    photo_relpaths = [
        f"{base_relpath}/{str(row.get('relpath') or '').strip()}"
        for row in list(receipt.get("photos") or [])
        if isinstance(row, dict) and str(row.get("relpath") or "").strip()
    ]
    if photo_relpaths:
        generated_reconstruction["photo_relpaths"] = photo_relpaths
    if glb_export.get("status") == "generated":
        generated_reconstruction["glb_model_relpath"] = f"{base_relpath}/{glb_export.get('glb_relpath') or 'model.glb'}"
    if str(diorama_preview.get("status") or "").strip() == "generated":
        generated_reconstruction["diorama_preview_bundle_relpath"] = str(diorama_preview.get("bundle_relpath") or "diorama-preview.png")
    if str(telegram_preview.get("status") or "").strip() == "generated":
        generated_reconstruction["telegram_preview_bundle_relpath"] = str(telegram_preview.get("bundle_relpath") or "telegram-preview.png")
    if walkthrough.get("status") == "generated":
        generated_reconstruction["walkthrough_video_relpath"] = f"{base_relpath}/generated-walkthrough.mp4"
        if str(args.style_label or "").strip():
            generated_reconstruction["walkthrough_style_label"] = str(args.style_label or "").strip()
        if str(walkthrough.get("composition") or "").strip():
            generated_reconstruction["walkthrough_composition"] = str(walkthrough.get("composition") or "").strip()
        if str(walkthrough.get("motion_style") or "").strip():
            generated_reconstruction["walkthrough_motion_style"] = str(walkthrough.get("motion_style") or "").strip()
        if walkthrough.get("sidecar_relpath"):
            generated_reconstruction["walkthrough_sidecar_relpath"] = f"{base_relpath}/{walkthrough.get('sidecar_relpath')}"
        if isinstance(walkthrough.get("coverage_proof"), dict):
            generated_reconstruction["walkthrough_coverage_proof"] = walkthrough["coverage_proof"]
    if route_labels:
        payload["room_visit_plan"] = route_labels
        payload["covered_route_labels"] = route_labels
    for key in (
        "video_relpath",
        "video_provider",
        "video_provider_key",
        "video_render_provider",
        "video_source",
        "video_sidecar_relpath",
        "video_coverage_proof",
    ):
        payload.pop(key, None)
    payload["generated_reconstruction"] = generated_reconstruction
    _upsert_public_generated_model_assets(
        payload,
        output_dir=output_dir,
        base_relpath=base_relpath,
        glb_export=glb_export,
    )
    for relpath in dict.fromkeys(
        (
            f"{base_relpath}/{str(vendor_assets.get('three_relpath') or 'vendor/three.module.js')}",
            f"{base_relpath}/{str(vendor_assets.get('orbit_controls_relpath') or 'vendor/examples/jsm/controls/OrbitControls.js')}",
        )
    ):
        _upsert_public_asset(
            payload,
            relpath=relpath,
            role="generated_reconstruction_viewer_asset",
            privacy_class="generated_reconstruction_public",
            mime_type="text/javascript",
        )
    if str(diorama_preview.get("status") or "").strip() == "generated":
        payload["diorama_preview_relpath"] = str(diorama_preview.get("bundle_relpath") or "diorama-preview.png")
        payload["preview_relpath"] = str(diorama_preview.get("bundle_relpath") or "diorama-preview.png")
        _upsert_diorama_scene(payload, relpath=payload["diorama_preview_relpath"])
        _upsert_public_asset(
            payload,
            relpath=payload["diorama_preview_relpath"],
            role="diorama",
            mime_type="image/png",
        )
    if str(telegram_preview.get("status") or "").strip() == "generated":
        payload["telegram_preview_relpath"] = str(telegram_preview.get("bundle_relpath") or "telegram-preview.png")
        _upsert_public_asset(
            payload,
            relpath=payload["telegram_preview_relpath"],
            role="preview",
            mime_type="image/png",
        )
    if walkthrough.get("status") == "generated":
        payload["video_relpath"] = f"{base_relpath}/generated-walkthrough.mp4"
        payload["video_provider"] = "propertyquarry_generated_reconstruction"
        payload["video_provider_key"] = "propertyquarry_generated_reconstruction"
        payload["video_render_provider"] = "propertyquarry_generated_reconstruction"
        payload["video_source"] = "propertyquarry_generated_reconstruction"
        payload["video_coverage_proof"] = "boundary_verified_frame_continuation"
        if walkthrough.get("sidecar_relpath"):
            payload["video_sidecar_relpath"] = f"{base_relpath}/{walkthrough.get('sidecar_relpath')}"
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    runtime_publish_required = _runtime_publish_required()
    runtime_publish_requested = _runtime_publish_requested()
    if bundle_uses_shared_runtime_root:
        planned_runtime_publish = {"status": "skipped_shared_public_root", "slug": slug}
    elif runtime_publish_requested:
        planned_runtime_publish = {
            "status": "pending_local_commit",
            "slug": slug,
            "container": str(os.getenv("PROPERTYQUARRY_RUNTIME_CONTAINER") or "propertyquarry-api").strip(),
        }
    else:
        planned_runtime_publish = {"status": "skipped_not_requested", "slug": slug}
    # The staged bundle carries the success receipt that becomes true only if
    # the finalizer promotes it. A failed publish never replaces the old live
    # bundle; the local receipt is then rewritten with the exact failure.
    receipt["runtime_publish"] = planned_runtime_publish
    receipt["runtime_publish_required"] = runtime_publish_required
    receipt["runtime_publish_ok"] = _runtime_publish_succeeded(planned_runtime_publish)
    (output_dir / "reconstruction.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    deferred_runtime_publish = bool(
        runtime_publish_requested and not bundle_uses_shared_runtime_root
    )
    runtime_publish = planned_runtime_publish
    runtime_publish_ok = _runtime_publish_succeeded(runtime_publish)
    receipt["runtime_publish"] = runtime_publish
    receipt["runtime_publish_required"] = runtime_publish_required
    receipt["runtime_publish_ok"] = runtime_publish_ok
    (output_dir / "reconstruction.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    response = {
        "slug": slug,
        "provider": "propertyquarry_generated_reconstruction",
        "viewer_relpath": f"{base_relpath}/viewer.html",
        "model_relpath": f"{base_relpath}/model.obj",
        "diorama_preview_relpath": str(payload.get("diorama_preview_relpath") or ""),
        "telegram_preview_relpath": str(payload.get("telegram_preview_relpath") or ""),
        "public_tour_url": "",
        "satisfies_verified_tour_gate": False,
        "walkthrough_status": walkthrough.get("status"),
        "verified_provider_capture": False,
        "runtime_publish": runtime_publish,
        "runtime_publish_required": runtime_publish_required,
        "_deferred_runtime_publish": deferred_runtime_publish,
    }

    if (
        runtime_publish_required
        and not runtime_publish_ok
        and not deferred_runtime_publish
    ):
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": "runtime_publish_failed",
                    "staged_bundle_generated": True,
                    "local_bundle_generated": False,
                    "live_bundle_preserved": True,
                    **response,
                },
                ensure_ascii=False,
            )
        )
        return 1, None

    return 0, {
        "status": "generated",
        **response,
    }


def _stable_generation_failure_reason(value: object) -> str:
    reason = str(value or "generation_failed").strip().lower()
    if re.fullmatch(r"[a-z0-9_:-]{1,160}", reason):
        return reason
    return "generation_failed"


def _render_transaction_id() -> str:
    supplied = str(
        os.getenv("PROPERTYQUARRY_RECONSTRUCTION_TRANSACTION_ID") or ""
    ).strip().lower()
    if supplied:
        if not re.fullmatch(r"[a-f0-9]{32}", supplied):
            raise _PublicBundleTransactionError("render_transaction_id_invalid")
        return supplied
    return secrets.token_hex(16)


def _write_candidate_commit_marker(
    *,
    bundle_dir: Path,
    slug: str,
    transaction_id: str,
) -> None:
    marker_path = bundle_dir / _PUBLIC_BUNDLE_COMMIT_MARKER
    payload = {
        "schema": "propertyquarry.render_bundle_commit.v1",
        "transaction_id": transaction_id,
        "slug": slug,
        "tour_manifest_sha256": _sha256(bundle_dir / "tour.json"),
    }
    directory_fd = -1
    temporary_fd = -1
    temporary_name = (
        f".{_PUBLIC_BUNDLE_COMMIT_MARKER}.{secrets.token_hex(16)}.tmp"
    )
    try:
        directory_fd, _ = _open_directory_anchor(bundle_dir)
        encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        offset = 0
        while offset < len(encoded):
            written = os.write(temporary_fd, encoded[offset:])
            if written <= 0:
                raise OSError("render commit marker short write")
            offset += written
        os.fchmod(temporary_fd, 0o600)
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = -1
        os.replace(
            temporary_name,
            _PUBLIC_BUNDLE_COMMIT_MARKER,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temporary_name = ""
        os.fsync(directory_fd)
    except OSError as exc:
        raise _PublicBundleTransactionError(
            "render_commit_marker_write_failed"
        ) from exc
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if directory_fd >= 0:
            if temporary_name:
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
            os.close(directory_fd)


def _update_committed_runtime_publish_receipt(
    *,
    bundle_dir: Path,
    reconstruction_subdir: str,
    runtime_publish: dict[str, object],
    runtime_publish_required: bool,
) -> None:
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    bundle_fd = -1
    reconstruction_fd = -1
    receipt_fd = -1
    temporary_fd = -1
    temporary_name = f".reconstruction.json.{secrets.token_hex(16)}.tmp"
    try:
        expected_bundle = bundle_dir.stat(follow_symlinks=False)
        bundle_fd = os.open(bundle_dir, directory_flags)
        opened_bundle = os.fstat(bundle_fd)
        if (
            not stat.S_ISDIR(expected_bundle.st_mode)
            or not stat.S_ISDIR(opened_bundle.st_mode)
            or opened_bundle.st_dev != expected_bundle.st_dev
            or opened_bundle.st_ino != expected_bundle.st_ino
        ):
            raise _PublicBundleTransactionError(
                "committed_bundle_changed"
            )
        reconstruction_fd = _open_public_directory_path(
            bundle_fd,
            reconstruction_subdir,
        )
        receipt_fd = _open_regular_public_entry(
            reconstruction_fd,
            "reconstruction.json",
            failure="committed_runtime_receipt_invalid",
        )
        receipt_metadata = os.fstat(receipt_fd)
        receipt = _read_bounded_json_object(
            receipt_fd,
            failure="committed_runtime_receipt_invalid",
        )
        receipt["runtime_publish"] = runtime_publish
        receipt["runtime_publish_required"] = bool(runtime_publish_required)
        receipt["runtime_publish_ok"] = _runtime_publish_succeeded(runtime_publish)
        encoded = (
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        if len(encoded) > 8 * 1024 * 1024:
            raise _PublicBundleTransactionError(
                "committed_runtime_receipt_size_limit_exceeded"
            )
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=reconstruction_fd,
        )
        offset = 0
        while offset < len(encoded):
            written = os.write(temporary_fd, encoded[offset:])
            if written <= 0:
                raise _PublicBundleTransactionError(
                    "committed_runtime_receipt_write_failed"
                )
            offset += written
        os.fchmod(temporary_fd, 0o644)
        os.fsync(temporary_fd)
        temporary_metadata = os.fstat(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = -1
        current_receipt = os.stat(
            "reconstruction.json",
            dir_fd=reconstruction_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(current_receipt.st_mode)
            or current_receipt.st_dev != receipt_metadata.st_dev
            or current_receipt.st_ino != receipt_metadata.st_ino
            or current_receipt.st_size != receipt_metadata.st_size
            or current_receipt.st_mtime_ns != receipt_metadata.st_mtime_ns
            or current_receipt.st_ctime_ns != receipt_metadata.st_ctime_ns
            or stat.S_IMODE(current_receipt.st_mode)
            != stat.S_IMODE(receipt_metadata.st_mode)
        ):
            raise _PublicBundleTransactionError(
                "committed_runtime_receipt_changed"
            )
        os.replace(
            temporary_name,
            "reconstruction.json",
            src_dir_fd=reconstruction_fd,
            dst_dir_fd=reconstruction_fd,
        )
        temporary_name = ""
        published_receipt = os.stat(
            "reconstruction.json",
            dir_fd=reconstruction_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(published_receipt.st_mode)
            or published_receipt.st_dev != temporary_metadata.st_dev
            or published_receipt.st_ino != temporary_metadata.st_ino
            or published_receipt.st_size != temporary_metadata.st_size
            or stat.S_IMODE(published_receipt.st_mode) != 0o644
        ):
            raise _PublicBundleTransactionError(
                "committed_runtime_receipt_publish_invalid"
            )
        os.fsync(reconstruction_fd)
        final_bundle = bundle_dir.stat(follow_symlinks=False)
        if (
            final_bundle.st_dev != opened_bundle.st_dev
            or final_bundle.st_ino != opened_bundle.st_ino
        ):
            raise _PublicBundleTransactionError("committed_bundle_changed")
        os.fsync(bundle_fd)
    except _PublicBundleTransactionError:
        raise
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise _PublicBundleTransactionError(
            "committed_runtime_receipt_write_failed"
        ) from exc
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if reconstruction_fd >= 0:
            if temporary_name:
                try:
                    os.unlink(temporary_name, dir_fd=reconstruction_fd)
                except OSError:
                    pass
        for descriptor in (receipt_fd, reconstruction_fd, bundle_fd):
            if descriptor >= 0:
                os.close(descriptor)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    slug = _validated_tour_slug(args.slug)
    public_root = _public_tour_dir()
    live_bundle_dir = public_root / slug
    if not live_bundle_dir.is_dir():
        raise SystemExit("tour_bundle_missing")
    bundle_uses_shared_runtime_root = _bundle_uses_shared_runtime_root(
        live_bundle_dir
    )
    final_response: dict[str, object] | None = None
    final_returncode = 0
    try:
        transaction_id = _render_transaction_id()
        with _staged_public_bundle(public_root, slug) as transaction:
            try:
                snapshot_args = _snapshot_generation_inputs(args, transaction)
                try:
                    returncode, response = _generate_reconstruction(
                        snapshot_args,
                        public_root=public_root,
                        bundle_dir=transaction.anchored_stage_dir,
                        bundle_uses_shared_runtime_root=bundle_uses_shared_runtime_root,
                    )
                finally:
                    _remove_generation_input_snapshot(transaction)
            except SystemExit as exc:
                print(
                    json.dumps(
                        {
                            "status": "failed",
                            "reason": _stable_generation_failure_reason(exc.code),
                        },
                        ensure_ascii=False,
                    )
                )
                return 1
            except _PublicBundleTransactionError:
                raise
            except Exception as exc:
                print(
                    json.dumps(
                        {
                            "status": "failed",
                            "reason": "generation_internal_error",
                            "error_class": type(exc).__name__,
                        },
                        ensure_ascii=False,
                    )
                )
                return 1
            if returncode != 0 or response is None:
                return max(1, int(returncode))
            deferred_runtime_publish = bool(
                response.pop("_deferred_runtime_publish", False)
            )
            _write_candidate_commit_marker(
                bundle_dir=transaction.anchored_stage_dir,
                slug=slug,
                transaction_id=transaction_id,
            )
            transaction.publish(
                reconstruction_subdir=(
                    _safe_relpath(args.target_subdir)
                    or "generated-reconstruction"
                ),
                require_walkthrough=not bool(args.skip_video),
            )
            transaction.cleanup_replaced_bundle()
            postcommit_failures: list[str] = []
            postcommit_error_class = ""
            try:
                if deferred_runtime_publish:
                    runtime_publish = _sync_bundle_to_runtime_container(
                        live_bundle_dir,
                        slug=slug,
                    )
                    runtime_publish_required = bool(
                        response.get("runtime_publish_required")
                    )
                    # Preserve the observed runtime outcome even if the
                    # receipt update itself fails after the local exchange.
                    response["runtime_publish"] = runtime_publish
                    _update_committed_runtime_publish_receipt(
                        bundle_dir=live_bundle_dir,
                        reconstruction_subdir=(
                            _safe_relpath(args.target_subdir)
                            or "generated-reconstruction"
                        ),
                        runtime_publish=runtime_publish,
                        runtime_publish_required=runtime_publish_required,
                    )
                    if (
                        runtime_publish_required
                        and not _runtime_publish_succeeded(runtime_publish)
                    ):
                        postcommit_failures.append(
                            "runtime_publish_failed_after_local_commit"
                        )
            except _PublicBundleTransactionError as exc:
                postcommit_failures.append(
                    _stable_generation_failure_reason(str(exc))
                )
            except Exception as exc:
                postcommit_failures.append("postcommit_internal_error")
                postcommit_error_class = type(exc).__name__
            response["publication_durability"] = transaction.durability_status
            response["replaced_bundle_cleanup"] = transaction.cleanup_status
            if transaction.durability_status != "fsynced":
                postcommit_failures.append("publication_durability_unverified")
            if transaction.cleanup_status != "removed":
                postcommit_failures.append("replaced_bundle_cleanup_deferred")
            if postcommit_failures:
                unique_failures = list(dict.fromkeys(postcommit_failures))
                response["status"] = "failed"
                response["reason"] = unique_failures[0]
                if len(unique_failures) > 1:
                    response["blocking_reasons"] = unique_failures
                if postcommit_error_class:
                    response["error_class"] = postcommit_error_class
                response["local_bundle_generated"] = True
                response["local_commit_applied"] = True
                final_returncode = 1
            final_response = response
    except _PublicBundleTransactionError as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": _stable_generation_failure_reason(str(exc)),
                },
                ensure_ascii=False,
            )
        )
        return 1
    if final_response is None:
        print(
            json.dumps(
                {"status": "failed", "reason": "generation_result_missing"},
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(final_response, ensure_ascii=False))
    return final_returncode


if __name__ == "__main__":
    raise SystemExit(main())
