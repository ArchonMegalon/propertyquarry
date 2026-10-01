#!/usr/bin/env python3
from __future__ import annotations

import os
import sys


def main() -> int:
    database_url = str(os.environ.get("DATABASE_URL") or "").strip()
    if not database_url:
        print("DATABASE_URL is not set; cannot migrate property search storage schema.", file=sys.stderr)
        return 1

    from app.product.property_search_schema import migrate_property_search_schema

    try:
        result = migrate_property_search_schema(
            database_url,
            applied_by=(
                str(os.environ.get("PROPERTYQUARRY_RELEASE_COMMIT_SHA") or "").strip()
                or str(os.environ.get("EA_ROLE") or "deploy").strip()
                or "deploy"
            ),
        )
    except Exception as exc:
        print(
            f"property search storage migration failed: {exc.__class__.__name__}",
            file=sys.stderr,
        )
        return 2
    print(
        "property search storage schema migrated "
        f"from v{result.previous_version} to v{result.current_version}; "
        f"applied={','.join(str(item) for item in result.applied_versions) or 'none'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
