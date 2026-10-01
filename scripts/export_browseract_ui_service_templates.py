#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


EA_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EA_ROOT / "ea"))

from app.services.browseract_ui_template_catalog import (  # noqa: E402
    browseract_ui_template_by_key,
    browseract_ui_template_definitions,
)


DEFAULT_OUTPUT_DIRS = (
    Path("/docker/EA/browseract_templates"),
    Path("/mnt/pcloud/EA/browseract_templates"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export versioned BrowserAct UI-service templates to JSON workflow files.")
    parser.add_argument(
        "--template",
        action="append",
        default=[],
        help="Template key to export. Repeat for multiple. Defaults to all template-backed UI services.",
    )
    parser.add_argument(
        "--output-dir",
        action="append",
        default=[],
        help="Extra output directory. Repeat for multiple locations.",
    )
    parser.add_argument(
        "--state-output-dir",
        default=os.environ.get("EA_BROWSERACT_TEMPLATE_OUTPUT_DIR", "/docker/fleet/state/browseract_bootstrap"),
        help="Workflow spec meta.output_dir value embedded into the generated spec.",
    )
    return parser.parse_args()


def selected_template_keys(values: list[str]) -> list[str]:
    requested = [str(value or "").strip() for value in values if str(value or "").strip()]
    if not requested:
        return [template.template_key for template in browseract_ui_template_definitions()]
    keys: list[str] = []
    seen: set[str] = set()
    for value in requested:
        template = browseract_ui_template_by_key(value)
        if template is None:
            raise SystemExit(f"unknown_template:{value}")
        if template.template_key in seen:
            continue
        seen.add(template.template_key)
        keys.append(template.template_key)
    return keys


def output_dirs(extra: list[str]) -> list[Path]:
    dirs: list[Path] = []
    seen: set[str] = set()
    for candidate in (*DEFAULT_OUTPUT_DIRS, *(Path(value).expanduser() for value in extra)):
        normalized = str(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        dirs.append(candidate)
    return dirs


def main() -> int:
    args = parse_args()
    template_keys = selected_template_keys(args.template)
    destinations = output_dirs(args.output_dir)
    summary: list[dict[str, object]] = []
    for template_key in template_keys:
        template = browseract_ui_template_by_key(template_key)
        if template is None:
            raise SystemExit(f"unknown_template:{template_key}")
        spec = template.workflow_spec(output_dir=str(args.state_output_dir))
        payload_text = json.dumps(spec, indent=2, ensure_ascii=False) + "\n"
        for target_dir in destinations:
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                continue
            path = target_dir / f"{template.template_key}.workflow.json"
            path.write_text(payload_text, encoding="utf-8")
            summary.append(
                {
                    "template_key": template.template_key,
                    "workflow_name": template.workflow_name,
                    "path": str(path),
                }
            )
    print(json.dumps({"status": "ok", "count": len(summary), "templates": summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
