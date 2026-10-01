#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "ea") not in sys.path:
    sys.path.insert(0, str(ROOT / "ea"))


def _configure_runtime() -> None:
    os.environ["EA_RUNTIME_MODE"] = "dev"
    os.environ["EA_STORAGE_BACKEND"] = "memory"
    os.environ.pop("DATABASE_URL", None)
    os.environ.pop("EA_LEDGER_BACKEND", None)
    os.environ["EA_API_TOKEN"] = ""
    os.environ.setdefault("PROPERTYQUARRY_ENABLE_LEGACY_RUNTIME_SURFACES", "1")
    os.environ.setdefault("EA_PROPERTY_PROVIDER_REPAIR_RETRY_BUDGET_SECONDS", "60")


def _write_receipt(path: Path, body: str) -> None:
    target = path.expanduser()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent_metadata = os.stat(target.parent, follow_symlinks=False)
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(parent_metadata.st_mode) & 0o022
    ):
        raise ValueError(
            f"repair canary receipt parent must be trusted and non-peer-writable: {target.parent}"
        )

    temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(temporary, flags, 0o600)
    try:
        payload = (body + "\n").encode("utf-8")
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while materializing repair canary receipt")
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Exercise the provider-repair canary without calling live providers."
    )
    parser.add_argument("--write", default="", help="Optional JSON receipt output path.")
    args = parser.parse_args()
    _configure_runtime()

    from fastapi.testclient import TestClient

    from app.api.app import create_app
    from app.product import service as property_service
    from app.product.service import ProductService

    principal_id = f"property-repair-canary-{uuid.uuid4().hex[:12]}"
    client = TestClient(create_app(), base_url="https://propertyquarry.com")
    client.headers.update({"X-EA-Principal-ID": principal_id, "host": "propertyquarry.com"})
    started = client.post(
        "/v1/onboarding/start",
        json={
            "workspace_name": "PropertyQuarry repair canary",
            "mode": "personal",
            "workspace_mode": "personal",
            "timezone": "Europe/Vienna",
            "region": "AT",
            "language": "en",
            "selected_channels": ["google"],
        },
    )
    if started.status_code != 200:
        print(json.dumps({"status": "failed", "reason": "workspace_start_failed", "body": started.text}, sort_keys=True))
        return 1

    service = ProductService(client.app.state.container)
    run_id = f"repair-canary-{uuid.uuid4().hex}"
    source_url = "https://repair-canary.example.invalid/search"
    now = property_service._now_iso()
    with property_service._PROPERTY_SEARCH_RUN_LOCK:
        property_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = {
            "run_id": run_id,
            "principal_id": principal_id,
            "created_at": now,
            "updated_at": now,
            "status": "failed",
            "status_url": f"/app/api/signals/property/search/run/{run_id}",
            "selected_platforms": ["repair_canary", "willhaben"],
            "progress": 100,
            "message": "Canary source fetch stopped before completion.",
            "summary": {
                "status": "failed",
                "ranked_candidates": [{"candidate_ref": "canary-hit", "title": "Canary recovered hit"}],
                "sources": [
                    {
                        "source_url": source_url,
                        "source_label": "Repair Canary | Austria | Rent | 1010 Vienna",
                        "status": "failed",
                        "error": "canary fetch failed",
                    }
                ],
            },
        }

    opened = service._open_property_provider_repair_task(
        principal_id=principal_id,
        property_url=source_url,
        title="Repair canary source fetch failed",
        source_url=source_url,
        source_label="Repair Canary | Austria | Rent | 1010 Vienna",
        source_platform="repair_canary",
        source_family="canary",
        filter_key="source_fetch",
        diagnostics={"provider_host": "repair-canary.example.invalid", "error": "timeout", "repair_attempts": 3},
        source_ref="property-source:repair-canary",
        run_id=run_id,
    )
    if str(opened.get("status") or "") != "opened":
        print(json.dumps({"status": "failed", "reason": "repair_task_not_opened", "opened": opened}, sort_keys=True))
        return 1

    original_auto_resolve = ProductService._auto_resolve_property_provider_repair_task
    ProductService._auto_resolve_property_provider_repair_task = (  # type: ignore[method-assign]
        lambda self, *, principal_id, task, actor: {"status": "deferred", "reason": "manual_provider_patch_required"}
    )
    try:
        repair_summary = service.process_property_provider_repair_tasks(principal_id=principal_id, actor="repair_canary", limit=5)
    finally:
        ProductService._auto_resolve_property_provider_repair_task = original_auto_resolve  # type: ignore[method-assign]

    status = service.get_property_search_run_status(principal_id=principal_id, run_id=run_id) or {}
    summary = dict(status.get("summary") or {})
    receipts = [dict(row) for row in list(summary.get("repair_receipts") or []) if isinstance(row, dict)]
    sources = [dict(row) for row in list(summary.get("sources") or []) if isinstance(row, dict)]
    source = sources[0] if sources else {}
    receipt = receipts[-1] if receipts else {}
    ok = (
        str(status.get("status") or "") == "completed_partial"
        and int(repair_summary.get("resolved_total") or 0) >= 1
        and int(repair_summary.get("deferred_total") or 0) == 0
        and str(receipt.get("resolution") or "") == "provider_quarantined_retry_budget_exhausted"
        and str(source.get("repair_status") or "") == "returned"
    )
    output = {
        "generated_at": property_service._now_iso(),
        "status": "pass" if ok else "failed",
        "principal_id": principal_id,
        "run_id": run_id,
        "run_status": str(status.get("status") or ""),
        "repair_summary": repair_summary,
        "receipt_resolution": str(receipt.get("resolution") or ""),
        "source_repair_status": str(source.get("repair_status") or ""),
        "source_status": str(source.get("status") or ""),
    }
    body = json.dumps(output, indent=2, sort_keys=True)
    if str(args.write or "").strip():
        try:
            _write_receipt(Path(str(args.write)), body)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
    print(body)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
