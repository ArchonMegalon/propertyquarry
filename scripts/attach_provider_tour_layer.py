#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path, PurePosixPath

_SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(_SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIRECTORY))

from property_tour_governed_reservation import require_dynamic_tour_slug


_3DVISTA_EXPORT_MARKERS = ("tdvplayer", "tdvplayerapi", "tourviewer")
_TEXT_RUNTIME_SUFFIXES = {".html", ".htm", ".js", ".mjs", ".json", ".xml"}
_MAX_MARKER_SCAN_BYTES = 1_000_000
_MAX_MARKER_SCAN_FILES = 240


def _public_tour_dir() -> Path:
    return Path(os.getenv("EA_PUBLIC_TOUR_DIR") or "/data/public_property_tours").expanduser().resolve()


def _safe_relpath(value: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/").lstrip("/")
    parts = [part for part in normalized.split("/") if part and part not in {".", ".."}]
    return "/".join(parts)


def _safe_layer_id(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").strip().lower()).strip("-")
    if not normalized:
        raise SystemExit("layer_id_required")
    return normalized[:80]


def _safe_provider_url(value: str, *, allowed_root: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    parsed = urllib.parse.urlparse(normalized)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise SystemExit(f"{allowed_root}_url_invalid")
    host = str(parsed.hostname or "").strip().lower().rstrip(".")
    if host != allowed_root and not host.endswith(f".{allowed_root}"):
        raise SystemExit(f"{allowed_root}_url_not_allowlisted")
    return normalized


def _text_asset_has_markers(path: Path, markers: tuple[str, ...]) -> bool:
    if path.suffix.lower() not in _TEXT_RUNTIME_SUFFIXES:
        return False
    try:
        if path.stat().st_size > _MAX_MARKER_SCAN_BYTES:
            return False
        body = path.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False
    return any(marker in body for marker in markers)


def _local_entry_has_3dvista_markers(bundle_dir: Path, entry_relpath: str) -> bool:
    entry = (bundle_dir / entry_relpath).resolve()
    if bundle_dir.resolve() not in entry.parents or not entry.is_file():
        return False
    export_root = entry.parent.resolve()
    candidates = [entry]
    for candidate in sorted(export_root.rglob("*")):
        if len(candidates) >= _MAX_MARKER_SCAN_FILES:
            break
        if candidate.is_file() and candidate.suffix.lower() in _TEXT_RUNTIME_SUFFIXES and candidate.resolve() not in candidates:
            candidates.append(candidate.resolve())
    return any(_text_asset_has_markers(candidate, _3DVISTA_EXPORT_MARKERS) for candidate in candidates)


def _has_base_3dvista(payload: dict[str, object], bundle_dir: Path) -> bool:
    for key in ("three_d_vista_url", "threedvista_url", "3dvista_url", "source_virtual_tour_url", "crezlo_public_url"):
        try:
            if _safe_provider_url(str(payload.get(key) or ""), allowed_root="3dvista.com"):
                return True
        except SystemExit:
            pass
    for key in ("three_d_vista_entry_relpath", "threedvista_entry_relpath", "3dvista_entry_relpath"):
        relpath = _safe_relpath(str(payload.get(key) or ""))
        if relpath and _local_entry_has_3dvista_markers(bundle_dir, relpath):
            return True
    return False


def _upsert_layer(payload: dict[str, object], layer: dict[str, object]) -> None:
    layers = payload.get("tour_layers")
    if not isinstance(layers, list):
        layers = []
    layer_id = str(layer.get("id") or "")
    next_layers = [row for row in layers if not (isinstance(row, dict) and str(row.get("id") or "") == layer_id)]
    next_layers.append(layer)
    payload["tour_layers"] = next_layers


def build_layer(args: argparse.Namespace, *, payload: dict[str, object], bundle_dir: Path) -> dict[str, object]:
    provider = str(args.provider or "").strip().lower()
    layer_id = _safe_layer_id(args.layer_id)
    label = str(args.label or "").strip() or layer_id.replace("-", " ").replace("_", " ").title()
    disclosure = str(args.disclosure or "").strip()
    layer: dict[str, object] = {
        "id": layer_id,
        "label": label,
        "provider": provider,
    }
    if disclosure:
        layer["disclosure"] = disclosure

    if provider == "matterport":
        matterport_url = _safe_provider_url(str(args.matterport_url or args.url or ""), allowed_root="matterport.com")
        if not matterport_url:
            raise SystemExit("matterport_url_required")
        layer["matterport_url"] = matterport_url
        layer.setdefault(
            "disclosure",
            "Separate staged Matterport model. The original source tour remains unchanged.",
        )
        return layer

    if provider == "3dvista":
        three_d_vista_url = _safe_provider_url(str(args.three_d_vista_url or args.url or ""), allowed_root="3dvista.com")
        entry_relpath = _safe_relpath(str(args.three_d_vista_entry_relpath or ""))
        same_tour_layer = bool(args.same_tour_layer)
        if sum(bool(value) for value in (three_d_vista_url, entry_relpath, same_tour_layer)) != 1:
            raise SystemExit("3dvista_requires_exactly_one_url_entry_or_same_tour_layer")
        if three_d_vista_url:
            layer["three_d_vista_url"] = three_d_vista_url
        elif entry_relpath:
            if not _local_entry_has_3dvista_markers(bundle_dir, entry_relpath):
                raise SystemExit("3dvista_layer_entry_unverified")
            layer["three_d_vista_entry_relpath"] = entry_relpath
        else:
            if not _has_base_3dvista(payload, bundle_dir):
                raise SystemExit("3dvista_same_tour_layer_requires_base_3dvista")
            query = str(args.query or "").strip().lstrip("?")
            fragment = str(args.fragment or "").strip().lstrip("#")
            if not query and not fragment:
                raise SystemExit("3dvista_same_tour_layer_requires_query_or_fragment")
            layer["same_tour_layer"] = True
            if query:
                layer["query"] = query
            if fragment:
                layer["fragment"] = fragment
        layer.setdefault(
            "disclosure",
            "Staged 3DVista layer. This uses a declared provider layer or second export, not a fake overlay.",
        )
        return layer

    raise SystemExit("provider_must_be_matterport_or_3dvista")


def main() -> int:
    parser = argparse.ArgumentParser(description="Attach a provider-backed staged layer to an existing PropertyQuarry tour.")
    parser.add_argument("--slug", required=True)
    parser.add_argument("--provider", required=True, choices=("matterport", "3dvista"))
    parser.add_argument("--layer-id", required=True)
    parser.add_argument("--label", default="")
    parser.add_argument("--disclosure", default="")
    parser.add_argument("--url", default="", help="Provider URL. Alias for --matterport-url or --three-d-vista-url.")
    parser.add_argument("--matterport-url", default="")
    parser.add_argument("--three-d-vista-url", default="")
    parser.add_argument("--three-d-vista-entry-relpath", default="")
    parser.add_argument("--same-tour-layer", action="store_true")
    parser.add_argument("--query", default="")
    parser.add_argument("--fragment", "--hash", dest="fragment", default="")
    args = parser.parse_args()

    slug = _safe_relpath(args.slug)
    if "/" in slug or not slug:
        raise SystemExit("invalid_tour_slug")
    try:
        require_dynamic_tour_slug(slug)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    bundle_dir = _public_tour_dir() / slug
    manifest_path = bundle_dir / "tour.json"
    if not manifest_path.is_file():
        raise SystemExit("tour_manifest_missing")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit("invalid_tour_manifest")

    layer = build_layer(args, payload=payload, bundle_dir=bundle_dir)
    _upsert_layer(payload, layer)
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "attached",
                "slug": slug,
                "layer_id": layer["id"],
                "provider": layer["provider"],
                "control_url": f"/tours/{slug}/control/{'matterport' if layer['provider'] == 'matterport' else '3dvista'}",
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
