#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT / "ea", ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))


def _safe_reason(exc: BaseException) -> str:
    reason = str(exc or "").strip().splitlines()[0] if str(exc or "").strip() else ""
    return reason if reason.startswith("mootion_remote_asset_") else "mootion_remote_asset_worker_failed"


def main() -> int:
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise RuntimeError("mootion_remote_asset_worker_request_invalid")
        asset_url = str(request.get("asset_url") or "").strip()
        target_dir = Path(str(request.get("target_dir") or "").strip()).expanduser().resolve()
        if not asset_url or not target_dir.exists() or not target_dir.is_dir():
            raise RuntimeError("mootion_remote_asset_worker_request_invalid")

        from app.mootion_remote_asset_fetch import (  # noqa: PLC0415
            _materialize_mootion_remote_video_asset_in_process,
        )

        target_path = _materialize_mootion_remote_video_asset_in_process(
            asset_url,
            target_dir=target_dir,
        )
        print(
            json.dumps(
                {
                    "status": "completed",
                    "size_bytes": target_path.stat().st_size,
                },
                ensure_ascii=False,
            )
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "failed", "reason": _safe_reason(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
