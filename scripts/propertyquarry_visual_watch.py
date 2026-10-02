#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(os.getenv("PROPERTYQUARRY_ROOT") or "/docker/property")
DEFAULT_OUT = ROOT / "_completion" / "pixefy" / "propertyquarry_visual_watch"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _analysis_script() -> str:
    return r"""
    () => {
      const viewport = { width: window.innerWidth, height: window.innerHeight };
      const visible = (node) => {
        const style = window.getComputedStyle(node);
        const box = node.getBoundingClientRect();
        return style.visibility !== 'hidden'
          && style.display !== 'none'
          && box.width > 1
          && box.height > 1
          && box.bottom >= 0
          && box.right >= 0
          && box.top <= viewport.height
          && box.left <= viewport.width;
      };
      const labelFor = (node) => {
        const direct = node.getAttribute('data-pqx-visual-label')
          || node.getAttribute('aria-label')
          || node.getAttribute('data-testid')
          || node.getAttribute('class')
          || node.tagName.toLowerCase();
        return String(direct || node.tagName.toLowerCase()).slice(0, 120);
      };
      const rectOf = (node) => {
        const box = node.getBoundingClientRect();
        return {
          left: Math.round(box.left),
          top: Math.round(box.top),
          right: Math.round(box.right),
          bottom: Math.round(box.bottom),
          width: Math.round(box.width),
          height: Math.round(box.height),
        };
      };
      const elements = Array.from(document.querySelectorAll('body *')).filter(visible);
      const escaped = elements
        .filter((node) => {
          const parent = node.parentElement;
          if (!parent || !visible(parent)) return false;
          const box = node.getBoundingClientRect();
          const parentBox = parent.getBoundingClientRect();
          const style = window.getComputedStyle(parent);
          const clippingParent = ['hidden', 'clip', 'auto', 'scroll'].includes(style.overflowX);
          if (clippingParent) return false;
          return box.left < parentBox.left - 2 || box.right > parentBox.right + 2;
        })
        .slice(0, 20)
        .map((node) => ({ label: labelFor(node), rect: rectOf(node), text: String(node.textContent || '').trim().slice(0, 160) }));
      const offscreenMedia = Array.from(document.querySelectorAll('img, svg, canvas, video'))
        .filter(visible)
        .filter((node) => {
          const box = node.getBoundingClientRect();
          return box.left < -2 || box.right > viewport.width + 2 || box.top < -2 || box.bottom > viewport.height + 2;
        })
        .slice(0, 20)
        .map((node) => ({ label: labelFor(node), rect: rectOf(node), src: String(node.currentSrc || node.src || '').slice(0, 180) }));
      const screenFitTargets = Array.from(document.querySelectorAll('[data-pqx-screenfit-target]'))
        .filter(visible)
        .map((node) => {
          const rect = rectOf(node);
          return {
            label: String(node.getAttribute('data-pqx-screenfit-target') || labelFor(node)).slice(0, 120),
            rect,
            fitsViewport: rect.top >= -2 && rect.left >= -2 && rect.right <= viewport.width + 2 && rect.bottom <= viewport.height + 2,
          };
        });
      const media = Array.from(document.querySelectorAll('img, svg, canvas, video')).filter(visible);
      const duplicateGraphics = [];
      for (let i = 0; i < media.length; i += 1) {
        for (let j = i + 1; j < media.length; j += 1) {
          const a = media[i];
          const b = media[j];
          const ar = a.getBoundingClientRect();
          const br = b.getBoundingClientRect();
          const sameSrc = String(a.currentSrc || a.src || a.outerHTML.slice(0, 80)) === String(b.currentSrc || b.src || b.outerHTML.slice(0, 80));
          const overlapX = Math.max(0, Math.min(ar.right, br.right) - Math.max(ar.left, br.left));
          const overlapY = Math.max(0, Math.min(ar.bottom, br.bottom) - Math.max(ar.top, br.top));
          const overlapArea = overlapX * overlapY;
          const minArea = Math.max(1, Math.min(ar.width * ar.height, br.width * br.height));
          if (sameSrc && overlapArea / minArea > 0.75) {
            duplicateGraphics.push({ first: labelFor(a), second: labelFor(b), rect: rectOf(a) });
          }
          if (duplicateGraphics.length >= 20) break;
        }
        if (duplicateGraphics.length >= 20) break;
      }
      return {
        viewport,
        bodyScrollWidth: document.documentElement.scrollWidth,
        bodyClientWidth: document.documentElement.clientWidth,
        horizontalPageOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 2,
        escaped,
        offscreenMedia,
        screenFitTargets,
        duplicateGraphics,
      };
    }
    """


def _run_watch(url: str, *, output_dir: Path, interval_seconds: float, samples: int, viewport: str) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    width_text, height_text = viewport.lower().split("x", 1)
    width = int(width_text)
    height = int(height_text)
    frames: list[dict[str, Any]] = []
    output_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": width, "height": height})
        try:
            page.goto(url, wait_until="networkidle", timeout=60000)
            for index in range(samples):
                if index:
                    page.wait_for_timeout(int(interval_seconds * 1000))
                screenshot_path = output_dir / f"frame-{index + 1:03d}.png"
                page.screenshot(path=str(screenshot_path), full_page=True, animations="disabled", caret="hide")
                analysis = page.evaluate(_analysis_script())
                frames.append(
                    {
                        "sample": index + 1,
                        "captured_at": _now(),
                        "screenshot": str(screenshot_path),
                        "analysis": analysis,
                    }
                )
        finally:
            browser.close()
    issue_count = sum(
        int(bool(frame["analysis"].get("horizontalPageOverflow")))
        + len(frame["analysis"].get("escaped") or [])
        + len(frame["analysis"].get("offscreenMedia") or [])
        + sum(0 if bool(target.get("fitsViewport")) else 1 for target in (frame["analysis"].get("screenFitTargets") or []))
        + len(frame["analysis"].get("duplicateGraphics") or [])
        for frame in frames
    )
    return {
        "tool": "propertyquarry.pixefy_visual_watch.local",
        "status": "pass" if issue_count == 0 else "fail",
        "url": url,
        "viewport": viewport,
        "interval_seconds": interval_seconds,
        "samples": samples,
        "issue_count": issue_count,
        "frames": frames,
        "generated_at": _now(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture periodic PropertyQuarry screenshots and audit visible layout issues.")
    parser.add_argument("url")
    parser.add_argument("--interval-seconds", type=float, default=float(os.getenv("PROPERTYQUARRY_PIXEFY_INTERVAL_SECONDS") or "20"))
    parser.add_argument("--samples", type=int, default=int(os.getenv("PROPERTYQUARRY_PIXEFY_SAMPLES") or "3"))
    parser.add_argument("--viewport", default=os.getenv("PROPERTYQUARRY_PIXEFY_VIEWPORT") or "1440x1000")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUT))
    args = parser.parse_args()

    payload = _run_watch(
        str(args.url),
        output_dir=Path(args.output_dir),
        interval_seconds=max(0.2, float(args.interval_seconds)),
        samples=max(1, int(args.samples)),
        viewport=str(args.viewport),
    )
    report_path = Path(args.output_dir) / "visual-watch-report.json"
    _write_json(report_path, payload)
    print(report_path)
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
