#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from property_magicfit_env import load_magicfit_env
def _load_env() -> None:
    load_magicfit_env()


def _login_if_needed(page) -> None:
    page.goto("https://magicfit.pushowl.com/home", wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(4_000)
    body = page.locator("body").inner_text(timeout=10_000)
    if not re.search(r"login|sign in|email|password", body, re.I):
        return
    email = (os.environ.get("PROPERTYQUARRY_MAGICFIT_EMAIL") or os.environ.get("MAGICFIT_EMAIL") or "").strip()
    password = (os.environ.get("PROPERTYQUARRY_MAGICFIT_PASSWORD") or os.environ.get("MAGICFIT_PASSWORD") or "").strip()
    if not email or not password:
        raise RuntimeError("magicfit_credentials_missing")
    page.locator("input[type=email], input[name*=email i], input[placeholder*=email i]").first.fill(email)
    page.locator("input[type=password]").first.fill(password)
    page.get_by_role("button", name=re.compile(r"sign in|login|continue|submit", re.I)).first.click()
    page.wait_for_timeout(8_000)


def _dump(page, out_json: Path, out_png: Path, *, label: str) -> None:
    buttons = page.locator("button").evaluate_all(
        """nodes => nodes.map((b, i) => ({
            index: i,
            text: (b.innerText || b.textContent || "").trim(),
            aria: b.getAttribute("aria-label"),
            title: b.getAttribute("title"),
            disabled: b.disabled,
            className: String(b.className || "")
        })).filter(x => x.text || x.aria || x.title)"""
    )
    inputs = page.locator("input").evaluate_all(
        """nodes => nodes.map((input, i) => ({
            index: i,
            type: input.getAttribute("type"),
            accept: input.getAttribute("accept"),
            name: input.getAttribute("name"),
            placeholder: input.getAttribute("placeholder"),
            className: String(input.className || "")
        }))"""
    )
    body = page.locator("body").inner_text(timeout=10_000)
    out_json.write_text(
        json.dumps(
            {
                "label": label,
                "url": page.url,
                "duration_mentions": sorted(set(re.findall(r"\b(?:4|5|6|7|8|9|10|11|12|13|14|15)s\b", body))),
                "button_count": len(buttons),
                "buttons": buttons,
                "inputs": inputs,
                "body": body,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    page.screenshot(path=str(out_png), full_page=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect MagicFit Extend-mode controls.")
    parser.add_argument("--video", default="", help="Optional local MP4 to try attaching in Extend mode.")
    parser.add_argument("--select-recent", action="store_true", help="Click the newest visible recent session before dumping.")
    parser.add_argument("--session-url", default="", help="Open a specific MagicFit session before selecting Extend.")
    parser.add_argument("--select-visible-video", action="store_true", help="Click the first visible video asset in the session.")
    parser.add_argument("--out-json", default="/tmp/magicfit-extend-ui-dump.json")
    parser.add_argument("--out-png", default="/tmp/magicfit-extend-ui-dump.png")
    args = parser.parse_args()
    _load_env()
    out_json = Path(args.out_json)
    out_png = Path(args.out_png)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        _login_if_needed(page)
        page.goto(args.session_url or "https://magicfit.pushowl.com/agents/generate?mode=video", wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_timeout(6_000)
        if args.select_visible_video:
            visible_video = page.locator("video").filter(has_not=page.locator("body")).last
            videos = page.locator("video")
            for index in range(videos.count()):
                candidate = videos.nth(index)
                box = candidate.bounding_box()
                if box and box.get("width", 0) > 50 and box.get("height", 0) > 50:
                    candidate.click(timeout=10_000, force=True)
                    page.wait_for_timeout(1_500)
                    break
        try:
            page.get_by_role("button", name=re.compile(r"^Extend$", re.I)).first.click(timeout=10_000)
            page.wait_for_timeout(2_000)
        except PlaywrightTimeoutError:
            pass
        video_path = Path(args.video).expanduser().resolve() if args.video else None
        if video_path and video_path.is_file():
            file_inputs = page.locator("input[type=file]")
            for index in range(file_inputs.count()):
                candidate = file_inputs.nth(index)
                accept = str(candidate.get_attribute("accept") or "")
                if "video" in accept or not accept:
                    candidate.set_input_files(str(video_path))
                    page.wait_for_timeout(5_000)
                    break
        if args.select_recent:
            cards = page.get_by_role("button", name=re.compile(r"\bitems?\b.*Vienna", re.I))
            if cards.count():
                cards.first.click(timeout=10_000, force=True)
                page.wait_for_timeout(5_000)
        _dump(page, out_json, out_png, label="extend")
        browser.close()
    print(json.dumps({"dump_json": str(out_json), "dump_png": str(out_png)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
