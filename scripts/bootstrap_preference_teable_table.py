#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ea"))


PREFERENCE_REVIEW_QUEUE_FIELDS = [
    {"name": "projection_id", "type": "singleLineText", "notNull": True, "unique": True},
    {"name": "person_id", "type": "singleLineText"},
    {"name": "display_name", "type": "singleLineText"},
    {"name": "domain", "type": "singleLineText"},
    {"name": "category", "type": "singleLineText"},
    {"name": "key", "type": "singleLineText"},
    {"name": "confidence", "type": "number"},
    {"name": "source_mode", "type": "singleLineText"},
    {"name": "status", "type": "singleLineText"},
    {"name": "target_ref", "type": "singleLineText"},
    {"name": "projection_version", "type": "singleLineText"},
    {"name": "editable_fields_allowlist", "type": "longText"},
    {"name": "evidence_ref_count", "type": "number"},
    {"name": "last_updated_at", "type": "singleLineText"},
    {"name": "expiry_at", "type": "singleLineText"},
    {"name": "correlation_id", "type": "singleLineText"},
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bootstrap the Teable table used by EA preference-profile sync.")
    parser.add_argument("--base-id", required=True)
    parser.add_argument("--base-url", default=os.environ.get("TEABLE_BASE_URL") or "https://app.teable.ai")
    parser.add_argument("--table-name", default="preference_review_queue")
    parser.add_argument("--create-table", action="store_true")
    parser.add_argument("--write-config", action="store_true")
    parser.add_argument("--env-file", default="/docker/EA/.env")
    return parser.parse_args()


def _api_key() -> str:
    direct = str(os.environ.get("TEABLE_API_KEY") or "").strip()
    if direct:
        return direct
    env_file = Path("/docker/EA/.env")
    if env_file.is_file():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            if raw.startswith("TEABLE_API_KEY="):
                return raw.split("=", 1)[1].strip()
    return ""


def _request_json(*, method: str, url: str, api_key: str, body: dict[str, object] | None = None) -> object:
    data = None if body is None else json.dumps(body, ensure_ascii=True).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")[:1000]
        raise SystemExit(f"HTTP {exc.code} from Teable: {detail}") from exc
    except Exception as exc:
        raise SystemExit(f"Teable request failed: {exc}") from exc
    if not payload.strip():
        return {}
    try:
        return json.loads(payload)
    except Exception as exc:
        raise SystemExit(f"Teable returned invalid JSON: {exc}") from exc


def _write_env_mapping(*, env_file: Path, table_name: str, table_id: str) -> None:
    mapping = {
        table_name: {
            "table_id": table_id,
            "key_field": "projection_id",
            "field_key_type": "name",
        }
    }
    line = f"TEABLE_TABLE_SYNC_CONFIG_JSON={json.dumps(mapping, separators=(',', ':'))}"
    if not env_file.exists():
        env_file.write_text(line + "\n", encoding="utf-8")
        return
    lines = env_file.read_text(encoding="utf-8").splitlines()
    replaced = False
    updated: list[str] = []
    for raw in lines:
        if raw.startswith("TEABLE_TABLE_SYNC_CONFIG_JSON="):
            updated.append(line)
            replaced = True
        else:
            updated.append(raw)
    if not replaced:
        updated.append(line)
    env_file.write_text("\n".join(updated) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    api_key = _api_key()
    if not api_key:
        raise SystemExit("missing TEABLE_API_KEY")
    base_url = str(args.base_url or "https://app.teable.ai").strip().rstrip("/")
    table_name = str(args.table_name or "preference_review_queue").strip() or "preference_review_queue"

    if args.create_table:
        created = _request_json(
            method="POST",
            url=f"{base_url}/api/base/{urllib.parse.quote(str(args.base_id).strip())}/table/",
            api_key=api_key,
            body={
                "name": table_name,
                "fields": PREFERENCE_REVIEW_QUEUE_FIELDS,
                "fieldKeyType": "name",
            },
        )
        created_dict = dict(created if isinstance(created, dict) else {})
        table_id = str(created_dict.get("id") or "").strip()
        if not table_id:
            raise SystemExit("Teable create-table response did not include table id")
        mapping = {
            table_name: {
                "table_id": table_id,
                "key_field": "projection_id",
                "field_key_type": "name",
            }
        }
        if args.write_config:
            _write_env_mapping(env_file=Path(args.env_file), table_name=table_name, table_id=table_id)
        print(
            json.dumps(
                {
                    "status": "created",
                    "base_id": args.base_id,
                    "table_name": table_name,
                    "table_id": table_id,
                    "mapping": mapping,
                    "wrote_env_config": bool(args.write_config),
                },
                indent=2,
                ensure_ascii=True,
            )
        )
        return 0

    print(
        json.dumps(
            {
                "status": "preview",
                "base_id": args.base_id,
                "table_name": table_name,
                "base_url": base_url,
                "required_fields": PREFERENCE_REVIEW_QUEUE_FIELDS,
                "mapping_preview": {
                    table_name: {
                        "table_id": "<fill-after-create>",
                        "key_field": "projection_id",
                        "field_key_type": "name",
                    }
                },
            },
            indent=2,
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
