#!/usr/bin/env python3
"""Compatibility entrypoint for the retired two-repository mirror contract.

PropertyQuarry no longer treats ArchonMegalon/property as a release mirror.
It is a non-authoritative legacy repository governed by the shared repository
role policy. New callers should use check_property_repository_role.py.
"""

from __future__ import annotations

try:
    from scripts.check_property_repository_role import main
except ModuleNotFoundError:  # Direct execution places scripts/ on sys.path.
    from check_property_repository_role import main  # type: ignore[no-redef]


CANONICAL_REPOSITORY = "ArchonMegalon/propertyquarry"
MIRROR_REPOSITORY = "ArchonMegalon/property"
CANONICAL_URL = "https://github.com/ArchonMegalon/propertyquarry.git"
MIRROR_URL = "https://github.com/ArchonMegalon/property.git"


if __name__ == "__main__":
    raise SystemExit(main())
