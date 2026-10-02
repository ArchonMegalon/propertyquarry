#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import json
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


DEFAULT_OUTPUT_DIR = Path("/docker/fleet/state/public_browseract_results")
DEFAULT_PUBLIC_BASE_URL = str(os.environ.get("EA_PUBLIC_RESULT_BASE_URL", "https://myexternalbrain.com/results")).strip().rstrip("/")


def default_output_dir() -> Path:
    configured = str(os.environ.get("EA_BROWSERACT_PUBLIC_RESULTS_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser()
    try:
        DEFAULT_OUTPUT_DIR.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return Path(os.environ.get("TMPDIR") or "/tmp") / "ea_public_browseract_results"
    return DEFAULT_OUTPUT_DIR


def slugify(value: str) -> str:
    lowered = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return lowered or "result"


def load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def coerce_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            loaded = json.loads(value)
        except Exception:
            return {}
        if isinstance(loaded, dict):
            return dict(loaded)
    return {}


def coerce_rows(paths: list[Path]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in paths:
        loaded = load_json(path)
        if isinstance(loaded, list):
            for entry in loaded:
                if isinstance(entry, dict):
                    rows.append(dict(entry))
            continue
        if isinstance(loaded, dict):
            rows.append(dict(loaded))
    return rows


def normalized_row(value: dict[str, object]) -> dict[str, object]:
    if isinstance(value.get("output_json"), dict):
        base = dict(value.get("output_json") or {})
        if isinstance(base.get("structured_output_json"), dict):
            merged = dict(base.get("structured_output_json") or {})
            merged.update(base)
            return merged
        return base
    if isinstance(value.get("structured_output_json"), dict):
        merged = dict(value.get("structured_output_json") or {})
        merged.update(value)
        return merged
    return dict(value)


def maybe_url(value: object) -> str:
    text = str(value or "").strip()
    if text.lower().startswith(("http://", "https://")):
        return text
    return ""


def maybe_local_path(value: object) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    candidate = Path(text).expanduser()
    if candidate.exists() and candidate.is_file():
        return candidate
    return None


def download_bytes(url: str) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "EA-Public-Result-Publisher/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.read(), str(response.headers.get("Content-Type") or "").strip()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"download_http_error:{exc.code}:{detail[:240]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"download_transport_error:{exc.reason}") from exc


def guess_ext(*, url: str, content_type: str) -> str:
    guessed = mimetypes.guess_extension((content_type or "").split(";", 1)[0].strip())
    if guessed:
        return guessed
    suffix = Path(urllib.parse.urlparse(url).path).suffix
    return suffix or ".bin"


def best_binary_url(row: dict[str, object]) -> str:
    for key in ("asset_url", "download_url"):
        url = maybe_url(row.get(key))
        if url:
            return url
    return ""


def best_local_asset_path(row: dict[str, object]) -> Path | None:
    for key in ("asset_path", "asset_file", "download_path", "download_file"):
        candidate = maybe_local_path(row.get(key))
        if candidate is not None:
            return candidate
    structured = coerce_dict(row.get("workflow_output_json"))
    if structured:
        for key in ("asset_path", "asset_file", "download_path", "download_file"):
            candidate = maybe_local_path(structured.get(key))
            if candidate is not None:
                return candidate
    return None


def best_text(row: dict[str, object]) -> str:
    for key in ("body_text", "raw_text", "normalized_text", "preview_text"):
        text = str(row.get(key) or "").strip()
        if text:
            return text
    structured = coerce_dict(row.get("workflow_output_json"))
    return str(structured or "").strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish browser-openable public result viewers for BrowserAct UI-service outputs.")
    parser.add_argument("--input", action="append", required=True, help="JSON file containing one result dict or a list of result dicts.")
    parser.add_argument("--output-dir", default=str(default_output_dir()))
    parser.add_argument("--public-base-url", default=DEFAULT_PUBLIC_BASE_URL)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = [normalized_row(row) for row in coerce_rows([Path(value) for value in args.input])]
    index_rows: list[dict[str, object]] = []
    for ordinal, row in enumerate(rows, start=1):
        title = str(row.get("result_title") or row.get("title") or row.get("service_key") or f"result-{ordinal}").strip()
        slug = slugify(title)
        target_dir = output_dir / slug
        target_dir.mkdir(parents=True, exist_ok=True)
        body_text = best_text(row)
        binary_url = best_binary_url(row)
        local_asset_path = best_local_asset_path(row)
        asset_relpath = ""
        mime_type = str(row.get("mime_type") or "").strip()
        notes: list[str] = []
        if local_asset_path is not None:
            target_path = target_dir / f"asset{local_asset_path.suffix or '.bin'}"
            shutil.copy2(local_asset_path, target_path)
            asset_relpath = target_path.name
            mime_type = mime_type or mimetypes.guess_type(str(local_asset_path))[0] or "application/octet-stream"
        elif binary_url:
            try:
                data, content_type = download_bytes(binary_url)
                suffix = guess_ext(url=binary_url, content_type=content_type)
                asset_path = target_dir / f"asset{suffix}"
                asset_path.write_bytes(data)
                asset_relpath = asset_path.name
                if content_type:
                    mime_type = content_type.split(";", 1)[0].strip()
            except Exception as exc:
                notes.append(str(exc))
        if not asset_relpath and body_text:
            text_path = target_dir / "content.txt"
            text_path.write_text(body_text + "\n", encoding="utf-8")
            asset_relpath = text_path.name
            mime_type = mime_type or "text/plain"
        if not mime_type and asset_relpath:
            mime_type = mimetypes.guess_type(str(target_dir / asset_relpath))[0] or "application/octet-stream"
        public_url = f"{str(args.public_base_url).rstrip('/')}/{slug}"
        manifest = {
            "slug": slug,
            "title": title,
            "service_key": str(row.get("service_key") or row.get("tool_name") or "").strip(),
            "summary": str(row.get("normalized_text") or row.get("preview_text") or body_text[:500]).strip(),
            "body_text": body_text,
            "mime_type": mime_type,
            "viewer_kind": (
                "video" if mime_type.startswith("video/") else
                "image" if mime_type.startswith("image/") else
                "pdf" if mime_type == "application/pdf" else
                "text"
            ),
            "asset_relpath": asset_relpath,
            "asset_path": str(local_asset_path) if local_asset_path is not None else "",
            "asset_url": maybe_url(row.get("asset_url")),
            "download_url": maybe_url(row.get("download_url")),
            "public_url": maybe_url(row.get("public_url")),
            "editor_url": maybe_url(row.get("editor_url")),
            "notes": " | ".join(note for note in notes if note),
            "hosted_url": public_url,
        }
        (target_dir / "result.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        index_rows.append({"slug": slug, "service_key": manifest["service_key"], "hosted_url": public_url, "path": str(target_dir / "result.json")})
    (output_dir / "index.json").write_text(json.dumps(index_rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": "ok", "count": len(index_rows), "index": str(output_dir / "index.json")}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
