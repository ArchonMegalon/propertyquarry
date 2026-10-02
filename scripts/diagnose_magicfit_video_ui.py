#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

from property_magicfit_env import load_magicfit_env
from propertyquarry_playwright_runtime import playwright_chromium_launch_kwargs
from render_magicfit_property_flythrough import (
    goto_with_retries,
    maybe_login,
    select_extend_library_video,
    select_generator_mode,
    upload_extend_video,
)


def _load_env() -> None:
    load_magicfit_env()


def main() -> int:
    _load_env()
    out_json = Path(os.getenv("MAGICFIT_UI_DUMP_JSON") or "/tmp/magicfit-ui-dump.json")
    out_png = Path(os.getenv("MAGICFIT_UI_DUMP_PNG") or "/tmp/magicfit-ui-dump.png")
    target_mode = str(os.getenv("MAGICFIT_UI_MODE") or "video").strip().lower()
    if target_mode not in {"video", "extend"}:
        raise RuntimeError("magicfit_ui_mode_invalid")
    storage_state_path = Path(
        os.getenv("PROPERTYQUARRY_MAGICFIT_STORAGE_STATE")
        or "state/runtime/magicfit-browser-storage.json"
    ).expanduser().resolve()
    source_video_raw = str(os.getenv("MAGICFIT_UI_SOURCE_VIDEO") or "").strip()
    source_video_path = Path(source_video_raw).expanduser().resolve() if source_video_raw else None
    inspect_library_picker = str(os.getenv("MAGICFIT_UI_LIBRARY_PICKER") or "").strip() == "1"
    library_video_url = str(os.getenv("MAGICFIT_UI_LIBRARY_VIDEO_URL") or "").strip()
    inspect_model_options = str(os.getenv("MAGICFIT_UI_MODEL_OPTIONS") or "").strip() == "1"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            **playwright_chromium_launch_kwargs(playwright, args=["--no-sandbox"])
        )
        context_options: dict[str, object] = {"viewport": {"width": 1440, "height": 1100}}
        if storage_state_path.is_file():
            context_options["storage_state"] = str(storage_state_path)
        context = browser.new_context(**context_options)
        page = context.new_page()
        maybe_login(page, storage_state_path=storage_state_path)
        goto_with_retries(page, "https://magicfit.pushowl.com/agents/generate?mode=video")
        page.wait_for_timeout(6_000)
        select_generator_mode(page, target_mode)
        source_upload: dict[str, object] | None = None
        source_upload_error = ""
        library_selection: dict[str, object] | None = None
        library_selection_error = ""
        if source_video_path is not None:
            if target_mode != "extend":
                raise RuntimeError("magicfit_ui_source_video_requires_extend_mode")
            try:
                source_upload = upload_extend_video(page, source_video_path)
            except RuntimeError as exc:
                source_upload_error = str(exc)
        if inspect_library_picker:
            if target_mode != "extend" or source_video_path is not None:
                raise RuntimeError("magicfit_ui_library_picker_requires_empty_extend_mode")
            page.get_by_text("Upload", exact=True).first.click(timeout=10_000, force=True)
            library_tab = page.get_by_text("My Library", exact=True).last
            library_tab.wait_for(state="visible", timeout=20_000)
            library_tab.click(timeout=10_000, force=True)
            page.wait_for_timeout(5_000)
        if library_video_url:
            if target_mode != "extend" or source_video_path is not None or inspect_library_picker:
                raise RuntimeError("magicfit_ui_library_video_requires_empty_extend_mode")
            try:
                library_selection = select_extend_library_video(page, library_video_url)
            except RuntimeError as exc:
                library_selection_error = str(exc)
        if inspect_model_options:
            model_control = page.get_by_role("button", name="Seedance 2.0 Fast", exact=True).last
            model_control.wait_for(state="visible", timeout=20_000)
            model_control.click(timeout=10_000, force=True)
            page.wait_for_timeout(2_000)
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
        file_inputs = page.locator("input[type=file]").evaluate_all(
            """nodes => nodes.map((node, index) => ({
                index,
                accept: node.getAttribute('accept') || '',
                multiple: Boolean(node.multiple),
                disabled: Boolean(node.disabled)
            }))"""
        )
        images = page.locator("img").evaluate_all(
            """nodes => nodes.map((node, index) => {
                const rect = node.getBoundingClientRect();
                const clickable = node.closest('button,[role="button"],a,[tabindex]');
                let src = node.currentSrc || node.src || '';
                try {
                    const parsed = new URL(src, window.location.href);
                    src = `${parsed.origin}${parsed.pathname}`;
                } catch (_) {}
                return {
                    index,
                    alt: node.getAttribute('alt') || '',
                    src,
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                    x: Math.round(rect.x),
                    y: Math.round(rect.y),
                    clickable_tag: clickable ? clickable.tagName.toLowerCase() : '',
                    clickable_text: clickable ? (clickable.innerText || '').trim().slice(0, 160) : '',
                    clickable_class: clickable ? String(clickable.className || '').slice(0, 240) : ''
                };
            }).filter(item => item.width > 80 && item.height > 80)"""
        )
        videos = page.locator("video").evaluate_all(
            """nodes => nodes.map((node, index) => {
                const rect = node.getBoundingClientRect();
                const clickable = node.closest('button,[role="button"],a,[tabindex]');
                const sanitize = value => {
                    try {
                        const parsed = new URL(value || '', window.location.href);
                        return `${parsed.origin}${parsed.pathname}`;
                    } catch (_) {
                        return value || '';
                    }
                };
                return {
                    index,
                    src: sanitize(node.currentSrc || node.src || ''),
                    poster: sanitize(node.poster || ''),
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                    x: Math.round(rect.x),
                    y: Math.round(rect.y),
                    clickable_tag: clickable ? clickable.tagName.toLowerCase() : '',
                    clickable_text: clickable ? (clickable.innerText || '').trim().slice(0, 160) : '',
                    clickable_class: clickable ? String(clickable.className || '').slice(0, 240) : ''
                };
            }).filter(item => item.width > 80 && item.height > 80)"""
        )
        body = page.locator("body").inner_text(timeout=10_000)
        out_json.write_text(
            json.dumps(
                {
                    "url": page.url,
                    "target_mode": target_mode,
                    "duration_mentions": sorted(set(re.findall(r"\b(?:4|5|6|8|10|12|15)s\b", body))),
                    "storage_state_used": storage_state_path.is_file(),
                    "button_count": len(buttons),
                    "buttons": buttons,
                    "file_inputs": file_inputs,
                    "images": images,
                    "videos": videos,
                    "source_upload": source_upload,
                    "source_upload_error": source_upload_error,
                    "library_picker_inspected": inspect_library_picker,
                    "library_selection": library_selection,
                    "library_selection_error": library_selection_error,
                    "model_options_inspected": inspect_model_options,
                    "body": body,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        page.screenshot(path=str(out_png), full_page=True)
        context.close()
        browser.close()
    print(json.dumps({"dump_json": str(out_json), "dump_png": str(out_png)}))
    return 1 if source_upload_error or library_selection_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
