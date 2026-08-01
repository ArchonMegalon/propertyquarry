#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


DEFAULT_OUTPUT_DIR = Path("/docker/property/state/public_property_tours/crezlo")
DEFAULT_PUBLIC_BASE_URL = str(os.environ.get("PROPERTYQUARRY_PUBLIC_TOUR_BASE_URL", "https://propertyquarry.com/tours")).strip().rstrip("/")

from property_tour_publication_gate import require_verified_crezlo_publication


def slugify(value: str) -> str:
    lowered = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return lowered or "tour"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish Crezlo property tours as PropertyQuarry-hosted browser-openable tour pages.")
    parser.add_argument("--input", action="append", required=True, help="Path to one or more property-tour run JSON files.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--public-base-url", default=DEFAULT_PUBLIC_BASE_URL)
    return parser.parse_args()


def download_bytes(url: str) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "PropertyQuarry-Crezlo-Tour-Publisher/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.read(), str(response.headers.get("Content-Type") or "").strip()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"download_http_error:{exc.code}:{detail[:240]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"download_transport_error:{exc.reason}") from exc


def load_run(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"tour_run_invalid:{path}")
    return payload


def _guess_ext(path_or_url: str, mime_type: str) -> str:
    guessed = mimetypes.guess_extension((mime_type or "").split(";", 1)[0].strip())
    if guessed:
        return guessed
    suffix = Path(urllib.parse.urlparse(path_or_url).path).suffix
    return suffix or ".bin"


def _scene_rows(run_payload: dict[str, object]) -> list[dict[str, object]]:
    structured = dict((run_payload.get("output_json") or {}).get("structured_output_json") or {})
    workflow_output = dict(structured.get("workflow_output_json") or {})
    detail = dict(workflow_output.get("tour_detail_json") or structured.get("tour_detail_json") or {})
    scenes = detail.get("scenes")
    if not isinstance(scenes, list):
        return []
    rows: list[dict[str, object]] = []
    for index, scene in enumerate(scenes, start=1):
        if not isinstance(scene, dict):
            continue
        file_payload = dict(scene.get("file") or {})
        url = str(file_payload.get("path") or "").strip()
        if not url:
            continue
        meta = dict(file_payload.get("meta") or {})
        rows.append(
            {
                "index": index,
                "name": str(scene.get("name") or file_payload.get("name") or f"Scene {index}").strip() or f"Scene {index}",
                "url": url,
                "role": str(meta.get("role") or "").strip(),
                "mime_type": str(file_payload.get("mime_type") or "").strip(),
                "source_url": str(meta.get("source_url") or "").strip(),
                "property_url": str(meta.get("property_url") or "").strip(),
            }
        )
    return rows


def _write_scene_assets(target_dir: Path, rows: list[dict[str, object]]) -> list[dict[str, object]]:
    published: list[dict[str, object]] = []
    for row in rows:
        content, mime_type = download_bytes(str(row["url"]))
        ext = _guess_ext(str(row["url"]), mime_type or str(row.get("mime_type") or ""))
        filename = f"scene-{int(row['index']):02d}{ext}"
        asset_path = target_dir / filename
        asset_path.write_bytes(content)
        published.append({**row, "asset_relpath": filename, "mime_type": mime_type or str(row.get("mime_type") or "")})
    return published


def _tour_html(*, slug: str, title: str, summary: str, notes: str, scenes: list[dict[str, object]], editor_url: str, property_url: str) -> str:
    cards = []
    for row in scenes:
        label = row["name"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        role = str(row.get("role") or "").strip().replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        asset_relpath = str(row["asset_relpath"]).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        source_url = str(row.get("source_url") or "").strip()
        source_link = (
            f'<a href="{source_url}" target="_blank" rel="noreferrer">Original image</a>'
            if source_url
            else ""
        )
        cards.append(
            f"""
            <article class="scene-card">
              <img src="/tours/files/{slug}/{asset_relpath}" alt="{label}">
              <div class="scene-meta">
                <div class="scene-index">{int(row["index"]):02d}</div>
                <div>
                  <h3>{label}</h3>
                  <p>{role or 'photo'}</p>
                </div>
                {source_link}
              </div>
            </article>
            """
        )
    summary_html = summary.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    notes_html = notes.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    title_html = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    editor_link = f'<a class="chip" href="{editor_url}" target="_blank" rel="noreferrer">Crezlo Editor</a>' if editor_url else ""
    property_link = f'<a class="chip" href="{property_url}" target="_blank" rel="noreferrer">Source Listing</a>' if property_url else ""
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title_html}</title>
    <style>
      :root {{
        --bg: #efe9de;
        --ink: #1b1713;
        --muted: #665e54;
        --panel: rgba(255,255,255,0.88);
        --edge: rgba(27,23,19,0.10);
        --accent: #0b5d66;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        color: var(--ink);
        background:
          radial-gradient(circle at top right, rgba(11,93,102,0.18), transparent 28%),
          linear-gradient(180deg, #f6f1e8 0%, #e7dfd0 100%);
        font-family: "Iowan Old Style", Georgia, serif;
      }}
      .shell {{ max-width: 1240px; margin: 0 auto; padding: 28px; }}
      .hero {{
        background: var(--panel);
        border: 1px solid var(--edge);
        border-radius: 30px;
        padding: 28px;
        box-shadow: 0 18px 54px rgba(27,23,19,0.08);
      }}
      .eyebrow {{ color: var(--muted); font-size: 12px; letter-spacing: 0.16em; text-transform: uppercase; }}
      h1 {{ margin: 10px 0 8px; font-size: clamp(2.2rem, 4.8vw, 4.4rem); line-height: 0.92; }}
      .summary {{ max-width: 76ch; color: var(--muted); line-height: 1.6; }}
      .actions {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 18px; }}
      .chip {{
        display: inline-flex;
        align-items: center;
        min-height: 42px;
        padding: 0 14px;
        border-radius: 999px;
        border: 1px solid var(--edge);
        background: rgba(255,255,255,0.72);
        text-decoration: none;
        color: inherit;
      }}
      .notes {{ margin-top: 14px; color: var(--muted); line-height: 1.6; }}
      .grid {{
        display: grid;
        gap: 18px;
        grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
        margin-top: 22px;
      }}
      .scene-card {{
        background: var(--panel);
        border: 1px solid var(--edge);
        border-radius: 24px;
        overflow: hidden;
        box-shadow: 0 12px 34px rgba(27,23,19,0.06);
      }}
      .scene-card img {{
        display: block;
        width: 100%;
        aspect-ratio: 16 / 10;
        object-fit: cover;
        background: #f3f0ea;
      }}
      .scene-meta {{
        display: grid;
        gap: 10px;
        grid-template-columns: auto 1fr;
        align-items: start;
        padding: 16px;
      }}
      .scene-index {{
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 42px;
        height: 42px;
        border-radius: 999px;
        background: rgba(11,93,102,0.10);
        color: var(--accent);
        font-weight: 700;
      }}
      .scene-meta h3 {{ margin: 0 0 6px; font-size: 1.15rem; }}
      .scene-meta p {{ margin: 0; color: var(--muted); }}
      .scene-meta a {{ grid-column: 1 / -1; color: var(--accent); text-decoration: none; }}
    </style>
  </head>
  <body>
    <main class="shell">
      <section class="hero">
        <div class="eyebrow">Property Tour</div>
        <h1>{title_html}</h1>
        <p class="summary">{summary_html}</p>
        <div class="actions">
          <a class="chip" href="/tours/{slug}.json">Tour JSON</a>
          {editor_link}
          {property_link}
        </div>
        <p class="notes">{notes_html}</p>
      </section>
      <section class="grid">
        {''.join(cards)}
      </section>
    </main>
  </body>
</html>"""


def _manifest_row(*, slug: str, title: str, summary: str, notes: str, editor_url: str, public_url: str, hosted_url: str) -> dict[str, object]:
    return {
        "slug": slug,
        "title": title,
        "service_key": "crezlo_property_tour",
        "summary": summary,
        "body_text": notes,
        "mime_type": "text/html",
        "viewer_kind": "html",
        "asset_relpath": "asset.html",
        "asset_path": "",
        "asset_url": "",
        "download_url": "",
        "public_url": hosted_url,
        "crezlo_public_url": public_url,
        "editor_url": editor_url,
        "notes": notes,
        "hosted_url": hosted_url,
    }


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    index_path = output_dir / "index.json"
    existing_index: list[dict[str, object]] = []
    if index_path.exists():
        try:
            loaded_index = json.loads(index_path.read_text(encoding="utf-8"))
            if isinstance(loaded_index, list):
                existing_index = [dict(row) for row in loaded_index if isinstance(row, dict)]
        except Exception:
            existing_index = []
    index_by_slug = {str(row.get("slug") or "").strip(): row for row in existing_index if str(row.get("slug") or "").strip()}

    for raw_input in args.input:
        run_path = Path(raw_input).expanduser()
        run_payload = load_run(run_path)
        output_json = dict(run_payload.get("output_json") or {})
        structured = dict(output_json.get("structured_output_json") or {})
        require_verified_crezlo_publication(structured)
        workflow_output = dict(structured.get("workflow_output_json") or {})
        detail = dict(workflow_output.get("tour_detail_json") or structured.get("tour_detail_json") or {})
        title = str(workflow_output.get("tour_title") or structured.get("tour_title") or output_json.get("result_title") or detail.get("title") or "Property Tour").strip() or "Property Tour"
        slug = slugify(str(workflow_output.get("slug") or structured.get("slug") or detail.get("slug") or title))
        target_dir = output_dir / slug
        target_dir.mkdir(parents=True, exist_ok=True)
        scene_rows = _scene_rows(run_payload)
        published_scenes = _write_scene_assets(target_dir, scene_rows)
        property_url = ""
        for row in published_scenes:
            property_url = str(row.get("property_url") or "").strip()
            if property_url:
                break
        summary = str(detail.get("display_title") or workflow_output.get("tour_title") or title).strip() or title
        notes = "Original Crezlo public link currently redirects to login. This PropertyQuarry-hosted tour mirrors the published scene order and media so the result opens directly in the browser."
        editor_url = str(workflow_output.get("editor_url") or structured.get("editor_url") or output_json.get("editor_url") or "").strip()
        public_url = str(workflow_output.get("public_url") or structured.get("public_url") or output_json.get("public_url") or "").strip()
        hosted_url = f"{str(args.public_base_url).rstrip('/')}/{slug}"
        html_path = target_dir / "asset.html"
        html_path.write_text(
            _tour_html(
                slug=slug,
                title=title,
                summary=summary,
                notes=notes,
                scenes=published_scenes,
                editor_url=editor_url,
                property_url=property_url,
            ),
            encoding="utf-8",
        )
        manifest = _manifest_row(
            slug=slug,
            title=title,
            summary=summary,
            notes=notes,
            editor_url=editor_url,
            public_url=public_url,
            hosted_url=hosted_url,
        )
        (target_dir / "result.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        index_by_slug[slug] = {
            "slug": slug,
            "service_key": "crezlo_property_tour",
            "hosted_url": hosted_url,
            "path": str(target_dir / "result.json"),
        }

    index_rows = [index_by_slug[key] for key in sorted(index_by_slug)]
    index_path.write_text(json.dumps(index_rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": "ok", "count": len(index_rows), "index": str(index_path)}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
