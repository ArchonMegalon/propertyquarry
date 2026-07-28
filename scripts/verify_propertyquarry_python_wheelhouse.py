#!/usr/bin/env python3
"""Verify the exact offline Python wheel set used by PropertyQuarry images."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import zipfile
from email.parser import BytesParser
from email.policy import default
from pathlib import Path


_PLAIN_REQUIREMENT_RE = re.compile(
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)==(?P<version>[A-Za-z0-9][A-Za-z0-9_.+!-]*)\Z"
)
_HASHED_REQUIREMENT_RE = re.compile(
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)==(?P<version>[A-Za-z0-9][A-Za-z0-9_.+!-]*)"
    r" --hash=sha256:(?P<sha256>[0-9a-f]{64})\Z"
)
_MAX_WHEEL_BYTES = 64 * 1024 * 1024
_MAX_METADATA_BYTES = 1024 * 1024
_MAX_WHEELS = 128
_BOOTSTRAP_REQUIREMENTS = {("pip", "26.1.2")}


class WheelhouseError(RuntimeError):
    pass


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _load_requirements(path: Path, *, hashed: bool) -> dict[tuple[str, str], str]:
    pattern = _HASHED_REQUIREMENT_RE if hashed else _PLAIN_REQUIREMENT_RE
    requirements: dict[tuple[str, str], str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
    except (OSError, UnicodeError) as error:
        raise WheelhouseError("requirements_unreadable") from error
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = pattern.fullmatch(line)
        if match is None:
            raise WheelhouseError("requirements_shape_invalid")
        key = (_canonical_name(match.group("name")), match.group("version"))
        if key in requirements:
            raise WheelhouseError("requirements_duplicate")
        requirements[key] = match.groupdict().get("sha256") or ""
    if not requirements or len(requirements) > _MAX_WHEELS:
        raise WheelhouseError("requirements_count_invalid")
    return requirements


def _wheel_identity(path: Path) -> tuple[str, str]:
    try:
        metadata = os.lstat(path)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size <= 0
            or metadata.st_size > _MAX_WHEEL_BYTES
        ):
            raise WheelhouseError("wheel_file_invalid")
        with zipfile.ZipFile(path) as archive:
            candidates = [
                info
                for info in archive.infolist()
                if info.filename.endswith(".dist-info/METADATA")
                and not info.is_dir()
            ]
            if (
                len(candidates) != 1
                or candidates[0].file_size <= 0
                or candidates[0].file_size > _MAX_METADATA_BYTES
            ):
                raise WheelhouseError("wheel_metadata_invalid")
            message = BytesParser(policy=default).parsebytes(
                archive.read(candidates[0])
            )
    except WheelhouseError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile, KeyError) as error:
        raise WheelhouseError("wheel_metadata_invalid") from error
    name = str(message.get("Name") or "").strip()
    version = str(message.get("Version") or "").strip()
    if not name or not version or "\n" in name or "\n" in version:
        raise WheelhouseError("wheel_metadata_invalid")
    return _canonical_name(name), version


def verify_wheelhouse(
    *,
    requirements_lock: Path,
    hash_lock: Path,
    wheelhouse: Path,
) -> dict[str, object]:
    plain = _load_requirements(requirements_lock, hashed=False)
    expected = _load_requirements(hash_lock, hashed=True)
    if set(plain) | _BOOTSTRAP_REQUIREMENTS != set(expected):
        raise WheelhouseError("requirement_sets_differ")
    try:
        root = wheelhouse.resolve(strict=True)
        root_metadata = os.lstat(root)
        entries = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError as error:
        raise WheelhouseError("wheelhouse_unreadable") from error
    if (
        stat.S_ISLNK(root_metadata.st_mode)
        or not stat.S_ISDIR(root_metadata.st_mode)
        or len(entries) != len(expected)
        or len(entries) > _MAX_WHEELS
    ):
        raise WheelhouseError("wheelhouse_shape_invalid")
    observed: dict[tuple[str, str], tuple[str, int, str]] = {}
    for path in entries:
        if path.suffix != ".whl" or path.name in {"", ".", ".."}:
            raise WheelhouseError("wheelhouse_shape_invalid")
        key = _wheel_identity(path)
        if key in observed:
            raise WheelhouseError("wheel_identity_duplicate")
        raw = path.read_bytes()
        observed[key] = (hashlib.sha256(raw).hexdigest(), len(raw), path.name)
    if set(observed) != set(expected):
        raise WheelhouseError("wheel_sets_differ")
    if any(observed[key][0] != expected[key] for key in expected):
        raise WheelhouseError("wheel_digest_mismatch")
    aggregate = hashlib.sha256(
        "".join(
            f"{key[0]}=={key[1]} {observed[key][0]} {observed[key][1]} {observed[key][2]}\n"
            for key in sorted(observed)
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema": "propertyquarry.python_wheelhouse.v1",
        "wheel_count": len(observed),
        "total_bytes": sum(item[1] for item in observed.values()),
        "aggregate_sha256": aggregate,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements-lock", required=True, type=Path)
    parser.add_argument("--hash-lock", required=True, type=Path)
    parser.add_argument("--wheelhouse", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        result = verify_wheelhouse(
            requirements_lock=arguments.requirements_lock,
            hash_lock=arguments.hash_lock,
            wheelhouse=arguments.wheelhouse,
        )
    except WheelhouseError as error:
        print(json.dumps({"status": "blocked", "reason": str(error)}, sort_keys=True))
        return 1
    print(json.dumps({"status": "pass", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
