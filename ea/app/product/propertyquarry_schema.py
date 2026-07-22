from __future__ import annotations

import argparse
import json
import os
from typing import Sequence

from app.kernel_schema import (
    KernelSchemaError,
    inspect_kernel_schema,
    migrate_kernel_schema,
    require_kernel_schema_ready,
)
from app.product.property_search_schema import (
    PropertySearchSchemaError,
    inspect_property_search_schema,
    migrate_property_search_schema,
    require_property_search_schema_ready,
)
from app.product.propertyquarry_google_identity_schema import (
    PropertyQuarryGoogleIdentitySchemaError,
    inspect_propertyquarry_google_identity_schema,
    migrate_propertyquarry_google_identity_schema,
    require_propertyquarry_google_identity_schema_ready,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="PropertyQuarry deploy-time schema migration gate."
    )
    parser.add_argument("operation", choices=("migrate", "check"))
    parser.add_argument("--database-url", default="")
    parser.add_argument("--applied-by", default="")
    return parser


def _applied_by(explicit: str) -> str:
    return (
        str(explicit or "").strip()
        or str(os.environ.get("PROPERTYQUARRY_RELEASE_COMMIT_SHA") or "").strip()
        or str(os.environ.get("EA_ROLE") or "deploy").strip()
        or "deploy"
    )


def migrate_propertyquarry_schema(
    database_url: str,
    *,
    applied_by: str,
) -> dict[str, object]:
    kernel_result = migrate_kernel_schema(database_url, applied_by=applied_by)
    require_kernel_schema_ready(database_url)
    property_search_result = migrate_property_search_schema(
        database_url,
        applied_by=applied_by,
    )
    require_property_search_schema_ready(database_url)
    google_identity_result = migrate_propertyquarry_google_identity_schema(
        database_url,
        applied_by=applied_by,
    )
    require_propertyquarry_google_identity_schema_ready(database_url)
    return {
        "kernel": kernel_result.as_dict(),
        "property_search": property_search_result.as_dict(),
        "google_identity": google_identity_result.as_dict(),
    }


def inspect_propertyquarry_schema(database_url: str) -> dict[str, object]:
    kernel_status = inspect_kernel_schema(database_url)
    property_search_status = inspect_property_search_schema(database_url)
    google_identity_status = inspect_propertyquarry_google_identity_schema(
        database_url
    )
    return {
        "ready": (
            kernel_status.ready
            and property_search_status.ready
            and google_identity_status.ready
        ),
        "kernel": kernel_status.as_dict(),
        "property_search": property_search_status.as_dict(),
        "google_identity": google_identity_status.as_dict(),
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    database_url = str(
        args.database_url or os.environ.get("DATABASE_URL") or ""
    ).strip()
    if not database_url:
        print(json.dumps({"status": "failed", "reason": "database_url_missing"}))
        return 2
    if args.operation == "migrate":
        try:
            result = migrate_propertyquarry_schema(
                database_url,
                applied_by=_applied_by(args.applied_by),
            )
        except (
            KernelSchemaError,
            PropertySearchSchemaError,
            PropertyQuarryGoogleIdentitySchemaError,
        ) as exc:
            print(json.dumps({"status": "failed", "reason": str(exc)}))
            return 2
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "reason": f"migration_failed:{exc.__class__.__name__}",
                    }
                )
            )
            return 2
        print(json.dumps({"status": "migrated", **result}, sort_keys=True))
        return 0
    status = inspect_propertyquarry_schema(database_url)
    print(
        json.dumps(
            {"status": "ready" if status["ready"] else "not_ready", **status},
            sort_keys=True,
        )
    )
    return 0 if status["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
