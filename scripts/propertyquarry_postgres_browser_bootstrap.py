#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = ROOT if (ROOT / "app").is_dir() else ROOT / "ea"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from app.container import build_container
from app.product.service import build_product_service


PRINCIPAL_ID = "propertyquarry-postgres-browser"
PRINCIPAL_EMAIL = "postgres-browser-lane@example.com"


def _secure_write(path: Path, payload: dict[str, object]) -> None:
    if not path.is_absolute() or path.parent.resolve(strict=True) != Path("/tmp"):
        raise RuntimeError("postgres_browser_bootstrap_output_must_be_in_tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Provision one internal-only session for the PostgreSQL browser CI lane."
    )
    parser.add_argument(
        "--write",
        required=True,
    )
    args = parser.parse_args()

    if str(os.environ.get("PROPERTYQUARRY_POSTGRES_BROWSER_E2E") or "").strip() != "1":
        raise SystemExit("postgres_browser_bootstrap_requires_explicit_ci_gate")

    container = build_container()
    runtime_mode = str(container.settings.runtime.mode or "").strip().lower()
    storage_backend = str(container.runtime_profile.storage_backend or "").strip().lower()
    if runtime_mode != "prod" or storage_backend != "postgres":
        raise SystemExit(
            f"postgres_browser_bootstrap_requires_prod_postgres:{runtime_mode or 'empty'}:{storage_backend or 'empty'}"
        )

    container.onboarding.start_workspace(
        principal_id=PRINCIPAL_ID,
        workspace_name="PostgreSQL Browser Office",
        workspace_mode="personal",
        region="AT",
        language="en",
        timezone="Europe/Vienna",
        selected_channels=(),
    )
    access = build_product_service(container).issue_workspace_access_session(
        principal_id=PRINCIPAL_ID,
        email=PRINCIPAL_EMAIL,
        role="principal",
        display_name="PostgreSQL Browser Office",
        source_kind="postgres_browser_internal_ci_bootstrap",
        expires_in_hours=1,
        default_target="/app/search",
    )
    access_token = str(access.get("access_token") or "").strip()
    if not access_token:
        raise SystemExit("postgres_browser_bootstrap_session_missing")

    _secure_write(
        Path(args.write),
        {
            "contract_name": "propertyquarry.postgres_browser_internal_session",
            "version": 1,
            "status": "pass",
            "provisioning_scope": "internal_ci_only",
            "runtime_mode": runtime_mode,
            "storage_backend": storage_backend,
            "principal_id": PRINCIPAL_ID,
            "email": PRINCIPAL_EMAIL,
            "access_token": access_token,
            "expires_at": str(access.get("expires_at") or "").strip(),
        },
    )
    print(json.dumps({"status": "ok", "provisioning_scope": "internal_ci_only"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
