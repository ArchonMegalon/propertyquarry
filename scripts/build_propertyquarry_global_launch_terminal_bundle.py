#!/usr/bin/env python3
"""Materialize, but never install, the global terminal's pinned runtime bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
INSTALL_ROOT = Path("/usr/libexec/propertyquarry")
INSTALLED_PYTHON = Path("/usr/bin/python3.12")
BUNDLE_SCHEMA = "propertyquarry.global_launch_terminal_bundle.v1"
ENTRYPOINT_SOURCE = ROOT / "scripts/propertyquarry_global_launch_terminal.py"
ENTRYPOINT_RELATIVE = PurePosixPath("propertyquarry-global-launch-terminal")
BUNDLE_MANIFEST_RELATIVE = PurePosixPath(
    "global-launch-terminal-bundle.v1.json"
)
CANONICAL_GOLD_STATIC_SOURCES: dict[PurePosixPath, Path] = {
    PurePosixPath(
        "runtime/schema/propertyquarry-production-capacity-receipt.v2.schema.json"
    ): ROOT
    / "packaging/propertyquarry-global-launch-terminal/"
    "propertyquarry-production-capacity-receipt.v2.schema.json",
    PurePosixPath(
        "runtime/config/compliance/"
        "propertyquarry_jurisdiction_privacy_rights.v1.json"
    ): ROOT
    / "config/compliance/propertyquarry_jurisdiction_privacy_rights.v1.json",
    PurePosixPath(
        "runtime/docs/propertyquarry_global_market_envelope.v1.json"
    ): ROOT / "docs/propertyquarry_global_market_envelope.v1.json",
}
MAX_SOURCE_FILE_BYTES = 32 * 1024 * 1024
MAX_BUNDLE_BYTES = 256 * 1024 * 1024


class BundleBuildError(RuntimeError):
    pass


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _stable_source(path: Path, *, maximum: int) -> bytes:
    if not path.is_absolute() or path != Path(os.path.realpath(path)):
        raise BundleBuildError(f"source path is not canonical: {path}")
    descriptor = -1
    try:
        before_path = path.lstat()
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if (
            stat.S_ISLNK(before_path.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_dev != before_path.st_dev
            or before.st_ino != before_path.st_ino
            or before.st_nlink != 1
            or before.st_mode & 0o002
            or not 0 <= before.st_size <= maximum
        ):
            raise BundleBuildError(f"source file is unsafe: {path}")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        after_path = path.lstat()
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if (
            remaining
            or len(raw) != before.st_size
            or any(getattr(before, name) != getattr(after, name) for name in stable_fields)
            or any(
                getattr(before, name) != getattr(after_path, name)
                for name in stable_fields
            )
        ):
            raise BundleBuildError(f"source file changed while read: {path}")
        return raw
    except OSError as exc:
        raise BundleBuildError(f"source file is unavailable: {path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _walk_regular_files(root: Path, *, suffixes: set[str]) -> Iterable[Path]:
    if not root.is_dir() or root != Path(os.path.realpath(root)):
        raise BundleBuildError(f"source directory is unavailable: {root}")
    for directory, names, filenames in os.walk(root, followlinks=False):
        names[:] = sorted(
            name
            for name in names
            if not (Path(directory) / name).is_symlink()
            and name not in {"__pycache__", ".pytest_cache"}
        )
        for filename in sorted(filenames):
            path = Path(directory) / filename
            if path.suffix in suffixes:
                yield path


def _source_map() -> dict[PurePosixPath, Path]:
    values: dict[PurePosixPath, Path] = {
        ENTRYPOINT_RELATIVE: ENTRYPOINT_SOURCE,
        **CANONICAL_GOLD_STATIC_SOURCES,
    }
    for source in _walk_regular_files(ROOT / "scripts", suffixes={".py"}):
        relative = source.relative_to(ROOT)
        values[PurePosixPath("runtime") / PurePosixPath(relative.as_posix())] = source
    for source in _walk_regular_files(ROOT / "ea/app", suffixes={".py"}):
        relative = source.relative_to(ROOT)
        values[PurePosixPath("runtime") / PurePosixPath(relative.as_posix())] = source
    for source in _walk_regular_files(
        ROOT / "config/monitoring",
        suffixes={".json", ".yml", ".yaml"},
    ):
        relative = source.relative_to(ROOT)
        values[PurePosixPath("runtime") / PurePosixPath(relative.as_posix())] = source
    overlay_registry = ROOT / "docs/PROPERTYQUARRY_EVIDENCE_OVERLAY_REGISTRY.json"
    if overlay_registry.is_file():
        values[
            PurePosixPath("runtime/docs/PROPERTYQUARRY_EVIDENCE_OVERLAY_REGISTRY.json")
        ] = overlay_registry
    return dict(sorted(values.items(), key=lambda item: str(item[0])))


def _write_exclusive(path: Path, raw: bytes, *, mode: int) -> None:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise BundleBuildError(f"bundle write made no progress: {path}")
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, mode)
    except OSError as exc:
        raise BundleBuildError(f"bundle output is unavailable: {path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def artifact_set_sha256(files: dict[str, str]) -> str:
    return _sha256(
        json.dumps(
            files,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    )


def build_bundle(output_root: Path) -> dict[str, object]:
    output_root = output_root.resolve(strict=False)
    if (
        not output_root.is_absolute()
        or output_root in {Path("/"), ROOT, ROOT.parent}
        or output_root.exists()
        or output_root.parent != output_root.parent.resolve(strict=True)
    ):
        raise BundleBuildError("output must be a new canonical dedicated directory")
    python_raw = _stable_source(INSTALLED_PYTHON, maximum=512 * 1024 * 1024)
    sources = _source_map()
    output_root.mkdir(mode=0o700)
    files: dict[str, str] = {}
    total_bytes = 0
    try:
        for relative, source in sources.items():
            raw = _stable_source(source, maximum=MAX_SOURCE_FILE_BYTES)
            total_bytes += len(raw)
            if total_bytes > MAX_BUNDLE_BYTES:
                raise BundleBuildError("bundle exceeds the aggregate byte budget")
            destination = output_root / Path(relative.as_posix())
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            mode = 0o555 if relative == ENTRYPOINT_RELATIVE else 0o444
            _write_exclusive(destination, raw, mode=mode)
            files[str(relative)] = _sha256(raw)
        manifest: dict[str, object] = {
            "schema": BUNDLE_SCHEMA,
            "version": 1,
            "install_root": str(INSTALL_ROOT),
            "python": {
                "path": str(INSTALLED_PYTHON),
                "sha256": _sha256(python_raw),
            },
            "files": files,
            "artifact_set_sha256": artifact_set_sha256(files),
        }
        manifest_raw = json.dumps(
            manifest,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8") + b"\n"
        _write_exclusive(
            output_root / Path(BUNDLE_MANIFEST_RELATIVE.as_posix()),
            manifest_raw,
            mode=0o444,
        )
        for directory, names, _files in os.walk(output_root, topdown=False):
            for name in names:
                (Path(directory) / name).chmod(0o555)
        output_root.chmod(0o555)
        return manifest
    except Exception:
        shutil.rmtree(output_root, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = build_bundle(args.output)
    except BundleBuildError as exc:
        print(
            json.dumps(
                {
                    "schema": BUNDLE_SCHEMA,
                    "status": "blocked",
                    "error": str(exc),
                },
                sort_keys=True,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "schema": BUNDLE_SCHEMA,
                "status": "materialized_not_installed",
                "output": str(args.output.resolve()),
                "artifact_set_sha256": manifest["artifact_set_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
