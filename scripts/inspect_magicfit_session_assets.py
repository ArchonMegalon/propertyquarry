#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

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


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect visible assets and controls in a MagicFit session.")
    parser.add_argument("--session-url", required=True)
    parser.add_argument("--out-json", default="/tmp/magicfit-session-elements.json")
    parser.add_argument("--out-png", default="/tmp/magicfit-session-elements.png")
    args = parser.parse_args()
    _load_env()
    out_json = Path(args.out_json)
    out_png = Path(args.out_png)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        _login_if_needed(page)
        page.goto(args.session_url, wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_timeout(6_000)
        payload = {
            "url": page.url,
            "videos": page.locator("video").evaluate_all(
                """nodes => nodes.map((v, i) => {
                    const r = v.getBoundingClientRect();
                    return {index: i, src: v.src, currentSrc: v.currentSrc, box: {x: r.x, y: r.y, w: r.width, h: r.height}};
                })"""
            ),
            "images": page.locator("img").evaluate_all(
                """nodes => nodes.map((img, i) => {
                    const r = img.getBoundingClientRect();
                    return {index: i, src: img.src, alt: img.alt, box: {x: r.x, y: r.y, w: r.width, h: r.height}};
                }).slice(0, 60)"""
            ),
            "buttons": page.locator("button").evaluate_all(
                """nodes => nodes.map((b, i) => {
                    const r = b.getBoundingClientRect();
                    return {
                        index: i,
                        text: (b.innerText || b.textContent || "").trim(),
                        aria: b.getAttribute("aria-label"),
                        title: b.getAttribute("title"),
                        disabled: b.disabled,
                        box: {x: r.x, y: r.y, w: r.width, h: r.height}
                    };
                }).filter(x => x.text || x.aria || x.title).slice(0, 80)"""
            ),
            "body": page.locator("body").inner_text(timeout=10_000),
        }
        out_json.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        page.screenshot(path=str(out_png), full_page=True)
        browser.close()
    print(json.dumps({"out_json": str(out_json), "out_png": str(out_png)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
