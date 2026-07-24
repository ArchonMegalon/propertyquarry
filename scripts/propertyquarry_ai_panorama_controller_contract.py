#!/usr/bin/env python3
"""Emit the inert source contract for an external panorama controller package.

This script performs no release effect and creates no authority.  Its output is
the file manifest an independent, signed, root-installed controller package
must bind before it may invoke the governed Prater operation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from pathlib import Path


SCHEMA = "propertyquarry.ai-panorama-controller-source-contract.v1"
COMPONENT = "propertyquarry-prater-ai-panorama-controller-reference"
ROOT = Path(__file__).resolve().parents[1]
SOURCE_RELPATHS = (
    "docker-compose.property.yml",
    "docs/PROPERTYQUARRY_AI_PANORAMA_INSTALL_CONTROLLER_V1.md",
    "ea/Dockerfile.property",
    "ea/Dockerfile.property-web",
    "ea/app/api/routes/public_tours.py",
    "ea/app/product/service.py",
    "ea/app/product/property_search_storage.py",
    "ea/app/product/property_search_tour_binding.py",
    "ea/app/product/property_tour_ai_panorama_admission.py",
    "ea/app/product/property_tour_ai_panorama_intake.py",
    "ea/app/product/property_tour_ai_panorama_operation_journal.py",
    "ea/app/product/property_tour_ai_panorama_prater_release.py",
    "ea/app/product/property_tour_governed_reservations.py",
    "ea/app/product/property_tour_hosting.py",
    "scripts/propertyquarry_ai_panorama_controller_contract.py",
    "scripts/propertyquarry_prater_ai_panorama_closeout.py",
    "scripts/propertyquarry_prater_ai_panorama_controller.py",
    "scripts/propertyquarry_prater_governed_volume_bootstrap.py",
    "scripts/property_tour_governed_reservation.py",
    "scripts/attach_provider_tour_layer.py",
    "scripts/generate_property_reconstruction.py",
    "scripts/import_3dvista_export.py",
    "scripts/import_krpano_walkable_scene.py",
    "scripts/import_magicfit_walkthrough.py",
    "scripts/import_pano2vr_export.py",
    "scripts/property_reconstruction_render_bridge.py",
    "scripts/publish_property_tour_live.py",
    "scripts/refresh_3dvista_private_viewer_runtime.py",
)
MAX_SOURCE_BYTES = 4 * 1024 * 1024
STATE_AUTHORITY = "propertyquarry-release-control"
CONSUMPTION_LEDGER_SCHEMA = (
    "propertyquarry.ai-panorama-install-consumption-ledger.v2"
)
OPERATION_JOURNAL_SCHEMA = (
    "propertyquarry.ai-panorama-install-operation-journal.v1"
)
STATE_FILE_MODE = 0o600
STATE_DIRECTORY_MODE = 0o700
STATE_LOCK_BYTES = b"lock\n"
_INSTANCE_ID_RE = re.compile(r"[0-9a-f]{32}\Z")


class ControllerContractError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


def _fail(code: str) -> None:
    raise ControllerContractError(code)


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ControllerContractError(
            "ai-panorama-controller-source-contract-invalid"
        ) from exc


def _stable_source(path: Path) -> tuple[bytes, os.stat_result]:
    descriptor = -1
    try:
        before = path.stat(follow_symlinks=False)
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > MAX_SOURCE_BYTES
        ):
            _fail("ai-panorama-controller-source-file-invalid")
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        identity = (
            int(opened.st_dev),
            int(opened.st_ino),
            int(opened.st_mode),
            int(opened.st_size),
            int(opened.st_mtime_ns),
            int(opened.st_ctime_ns),
        )
        if identity != (
            int(before.st_dev),
            int(before.st_ino),
            int(before.st_mode),
            int(before.st_size),
            int(before.st_mtime_ns),
            int(before.st_ctime_ns),
        ):
            _fail("ai-panorama-controller-source-file-changed")
        chunks: list[bytes] = []
        remaining = int(opened.st_size)
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                _fail("ai-panorama-controller-source-file-changed")
            chunks.append(chunk)
            remaining -= len(chunk)
        closed = os.fstat(descriptor)
        after = path.stat(follow_symlinks=False)
        for observed in (closed, after):
            if (
                int(observed.st_dev),
                int(observed.st_ino),
                int(observed.st_mode),
                int(observed.st_size),
                int(observed.st_mtime_ns),
                int(observed.st_ctime_ns),
            ) != identity:
                _fail("ai-panorama-controller-source-file-changed")
        return b"".join(chunks), after
    except ControllerContractError:
        raise
    except OSError as exc:
        raise ControllerContractError(
            "ai-panorama-controller-source-file-unavailable"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def build_state_genesis(
    *,
    consumption_instance_id: str,
    operation_instance_id: str,
) -> dict[str, bytes]:
    """Return exact bytes for external one-time controller provisioning."""

    if (
        type(consumption_instance_id) is not str
        or type(operation_instance_id) is not str
        or _INSTANCE_ID_RE.fullmatch(consumption_instance_id) is None
        or _INSTANCE_ID_RE.fullmatch(operation_instance_id) is None
        or consumption_instance_id == operation_instance_id
    ):
        _fail("ai-panorama-controller-state-instance-invalid")
    genesis = {
        "consumption-ledger.v2.json": {
            "schema": CONSUMPTION_LEDGER_SCHEMA,
            "authority": STATE_AUTHORITY,
            "instance_id": consumption_instance_id,
            "sequence": 0,
            "tip_sha256": "0" * 64,
            "entries": [],
        },
        "operation-journal.v1.json": {
            "schema": OPERATION_JOURNAL_SCHEMA,
            "authority": STATE_AUTHORITY,
            "instance_id": operation_instance_id,
            "sequence": 0,
            "tip_sha256": "0" * 64,
            "entries": [],
        },
    }
    return {
        "consumption-ledger.v2.json": (
            _canonical(genesis["consumption-ledger.v2.json"]) + b"\n"
        ),
        "consumption-ledger.v2.lock": STATE_LOCK_BYTES,
        "operation-journal.v1.json": (
            _canonical(genesis["operation-journal.v1.json"]) + b"\n"
        ),
        "operation-journal.v1.lock": STATE_LOCK_BYTES,
    }


def build_info() -> dict[str, object]:
    files: list[dict[str, object]] = []
    for relpath in SOURCE_RELPATHS:
        raw, details = _stable_source(ROOT / relpath)
        files.append(
            {
                "relpath": relpath,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
                "mode": stat.S_IMODE(details.st_mode),
            }
        )
    source_manifest_sha256 = hashlib.sha256(_canonical(files)).hexdigest()
    return {
        "schema": SCHEMA,
        "version": 1,
        "component": COMPONENT,
        "status": "reference-only",
        "authoritative": False,
        "production_ready": False,
        "performs_release_effects": False,
        "source_manifest_sha256": source_manifest_sha256,
        "files": files,
        "state_genesis": {
            "status": "external-one-time-provisioning-only",
            "required_uid": 0,
            "directory_mode": STATE_DIRECTORY_MODE,
            "file_mode": STATE_FILE_MODE,
            "instance_id": "independent-csprng-128-bit-lowercase-hex",
            "recreation_after_activation": "forbidden",
            "files": [
                "consumption-ledger.v2.json",
                "consumption-ledger.v2.lock",
                "operation-journal.v1.json",
                "operation-journal.v1.lock",
            ],
        },
        "required_external_controls": [
            "independently-built-and-signed-controller-package",
            "root-owned-digest-bound-installed-file-manifest",
            "external-purpose-specific-ai-panorama-keyring",
            "fixed-root-canonical-release-context",
            "fixed-root-public-volume-profile",
            "fixed-root-consumption-ledger-and-tombstones",
            "fixed-root-operation-journal",
            "separately-fenced-root-controller-process",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Emit the inert AI panorama controller source contract."
    )
    parser.add_argument(
        "--build-info-json",
        action="store_true",
        help="Print canonical source-contract JSON; this never performs release effects.",
    )
    args = parser.parse_args()
    if not args.build_info_json:
        parser.error("--build-info-json is required; this source hook cannot release")
    try:
        print(_canonical(build_info()).decode("ascii"))
    except ControllerContractError as exc:
        print(
            _canonical(
                {
                    "schema": SCHEMA,
                    "status": "failed",
                    "error": exc.code,
                    "authoritative": False,
                    "performs_release_effects": False,
                }
            ).decode("ascii")
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
