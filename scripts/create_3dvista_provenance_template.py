#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

if __package__:
    from .property_tour_3dvista_provenance import (
        THREE_D_VISTA_PROVENANCE_FILENAMES,
        THREE_D_VISTA_TARGET_PROVENANCE_SCHEMA,
        export_tree_sha256,
        safe_relpath,
    )
else:
    from property_tour_3dvista_provenance import (
        THREE_D_VISTA_PROVENANCE_FILENAMES,
        THREE_D_VISTA_TARGET_PROVENANCE_SCHEMA,
        export_tree_sha256,
        safe_relpath,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create a fail-closed review template for an exact 3DVista export. "
            "The template remains non-importable until an authorized reviewer completes it."
        )
    )
    parser.add_argument("--slug", required=True, help="Exact existing PropertyQuarry tour slug.")
    parser.add_argument("--export-dir", required=True, help="Directory exported by 3DVista VT Pro.")
    parser.add_argument("--entry", default="index.html", help="Entry HTML relative to the export directory.")
    parser.add_argument("--write", default="", help="Defaults to <export-dir>/3dvista-target-provenance.json.")
    parser.add_argument("--force", action="store_true", help="Replace an existing template or receipt.")
    args = parser.parse_args()

    slug = str(args.slug or "").strip()
    if not slug or "/" in slug or "\\" in slug or ".." in slug:
        raise SystemExit("invalid_tour_slug")
    export_dir = Path(args.export_dir).expanduser().resolve()
    if not export_dir.is_dir():
        raise SystemExit("3dvista_export_dir_missing")
    entry_relpath = safe_relpath(args.entry)
    if not entry_relpath or not (export_dir / entry_relpath).is_file():
        raise SystemExit("3dvista_export_entry_missing")
    write_path = (
        Path(args.write).expanduser().resolve()
        if str(args.write or "").strip()
        else export_dir / "3dvista-target-provenance.json"
    )
    if export_dir in write_path.parents and not (
        write_path.parent == export_dir and write_path.name in THREE_D_VISTA_PROVENANCE_FILENAMES
    ):
        raise SystemExit("3dvista_target_provenance_write_path_invalid")
    if write_path.exists() and not args.force:
        raise SystemExit("3dvista_target_provenance_already_exists")

    artifact_sha256 = export_tree_sha256(export_dir)
    if not artifact_sha256:
        raise SystemExit("3dvista_export_unhashable")
    payload = {
        "schema": THREE_D_VISTA_TARGET_PROVENANCE_SCHEMA,
        "status": "pending_review",
        "provider": "3dvista",
        "target_slug": slug,
        "artifact": {
            "kind": "local_export",
            "sha256": artifact_sha256,
            "entry_relpath": entry_relpath,
        },
        "authorization": {
            "status": "pending",
            "reference": "",
        },
        "review": {
            "property_match": "pending",
            "visual_match": "pending",
            "reviewed_by": "",
            "reviewed_at": "",
        },
        "review_instructions": [
            "Confirm the export is licensed for reuse on PropertyQuarry and record the approval reference.",
            "Compare the complete tour against the exact target listing and mark property_match=pass only when it is the same property.",
            "Review desktop and mobile rendering, navigation, branding, and scene quality before marking visual_match=pass.",
            "Set status=pass only after all approval and review fields are complete; use an ISO-8601 timezone-aware reviewed_at value.",
        ],
    }
    write_path.parent.mkdir(parents=True, exist_ok=True)
    write_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_path.chmod(0o600)
    print(
        json.dumps(
            {
                "status": "pending_review_template_written",
                "slug": slug,
                "artifact_sha256": artifact_sha256,
                "write_path": str(write_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
