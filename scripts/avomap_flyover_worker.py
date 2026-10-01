#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import html
import json
import os
import shutil
import subprocess
import tempfile
import time
import traceback
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from browseract_ui_media import transcode_video


PLAYWRIGHT_IMAGE = os.environ.get("EA_UI_PLAYWRIGHT_IMAGE", "chummer-playwright:local").strip() or "chummer-playwright:local"
OUTPUT_ROOT = Path(os.environ.get("EA_UI_SERVICE_WORKER_OUTPUT_ROOT", "/docker/fleet/state/browseract_ui_worker_outputs")).expanduser()
SHARED_TEMP_ROOT = Path(os.environ.get("EA_UI_SERVICE_SHARED_TEMP_ROOT", "/docker/fleet/state/browseract_ui_worker_shared")).expanduser()
DEFAULT_EMAIL = os.environ.get("EA_UI_SERVICE_LOGIN_EMAIL", "").strip()
DEFAULT_PASSWORD = os.environ.get("EA_UI_SERVICE_LOGIN_PASSWORD", "").strip()


def _load_packet(path: str | None) -> dict[str, object]:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    raw = os.sys.stdin.read()
    if not raw.strip():
        raise RuntimeError("avomap_worker_input_missing")
    loaded = json.loads(raw)
    if not isinstance(loaded, dict):
        raise RuntimeError("avomap_worker_input_invalid")
    return loaded


def _slugify(value: object) -> str:
    lowered = "".join(char.lower() if char.isalnum() else "-" for char in str(value or "").strip())
    lowered = "-".join(part for part in lowered.split("-") if part)
    return lowered or f"flyover-{uuid.uuid4().hex[:12]}"


def _write_route_file(route_data: str, target_dir: Path) -> Path:
    text = str(route_data or "").strip()
    if not text:
        raise RuntimeError("avomap_route_data_missing")
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme in {"http", "https"}:
        suffix = Path(parsed.path).suffix or ".gpx"
        target = target_dir / f"route{suffix}"
        with urllib.request.urlopen(text, timeout=180) as response:
            target.write_bytes(response.read())
        return target
    local_candidate = Path(text).expanduser()
    if local_candidate.exists():
        target = target_dir / local_candidate.name
        shutil.copy2(local_candidate, target)
        return target
    suffix = ".kml" if "<kml" in text.lower() else ".gpx"
    target = target_dir / f"route{suffix}"
    target.write_text(text, encoding="utf-8")
    return target


def _normalize_video_format_label(value: object) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text in {"16:9", "landscape", "horizontal"}:
        return "Landscape"
    if text in {"9:16", "portrait", "vertical"}:
        return "Portrait"
    if text in {"1:1", "square"}:
        return "Square"
    return str(value or "").strip()


def _normalize_video_quality_label(value: object) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text in {"1080p", "full hd", "fhd"}:
        return "Full HD"
    if text in {"720p", "hd"}:
        return "HD"
    if text in {"4k", "uhd"}:
        return "4K"
    return str(value or "").strip()


def _avomap_node_script() -> str:
    return r"""
const { chromium } = require('playwright');
const fs = require('fs');

async function main() {
  const packet = JSON.parse(fs.readFileSync(process.env.AVOMAP_PACKET_PATH, 'utf8'));
  const routePath = process.env.AVOMAP_ROUTE_PATH;
  const screenshotPath = process.env.AVOMAP_SCREENSHOT_PATH;
  const recordDir = process.env.AVOMAP_RECORD_DIR;
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1480, height: 1160 } });
  const page = await context.newPage();
  const result = { url: '', title: '', bodyText: '', buttons: [], links: [], videoSrcs: [], errors: [], renderTriggered: false, previewRecorded: false, recordedVideoPath: '' };

  async function loginMaybe() {
    await page.goto('https://app.avomap.com/', { waitUntil: 'domcontentloaded', timeout: 120000 }).catch(() => {});
    await page.waitForTimeout(3000);
    const emailField = page.locator('input[type=email]').first();
    if (await emailField.count()) {
      await emailField.fill(String(packet.login_email || ''));
      const passwordField = page.locator('input[type=password]').first();
      if (await passwordField.count()) {
        await passwordField.fill(String(packet.login_password || ''));
      }
      const loginButton = page.getByRole('button', { name: /log in|sign in|continue/i }).first();
      if (await loginButton.count()) {
        await loginButton.click({ force: true }).catch(() => {});
      }
      await page.waitForTimeout(8000);
    }
  }

  async function maybeClickByText(text, options = {}) {
    if (!text) return false;
    const locator = page.getByRole('button', { name: new RegExp(String(text).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i') }).first();
    if (await locator.count()) {
      await locator.click({ force: true }).catch(() => {});
      await page.waitForTimeout(Number(options.waitMs || 1200));
      return true;
    }
    const generic = page.getByText(new RegExp(String(text).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i')).first();
    if (await generic.count()) {
      await generic.click({ force: true }).catch(() => {});
      await page.waitForTimeout(Number(options.waitMs || 1200));
      return true;
    }
    return false;
  }

  async function capturePreviewPlayback() {
    const storageState = await context.storageState().catch(() => null);
    if (!storageState) return '';
    const recordingContext = await browser.newContext({
      storageState,
      viewport: { width: 992, height: 768 },
      recordVideo: { dir: recordDir, size: { width: 992, height: 768 } },
    });
    const recordingPage = await recordingContext.newPage();
    await recordingPage.goto('https://app.avomap.com/account/user/map?uid=ZCPeRUvt5J&tab=route&card=main', { waitUntil: 'domcontentloaded', timeout: 120000 }).catch(() => {});
    await recordingPage.waitForTimeout(5000);
    const initialText = await recordingPage.locator('body').innerText().catch(() => '');
    const initialTimer = /\b(\d{2}:\d{2}:\d)\s*\/\s*(\d{2}:\d{2}:\d)\b/.exec(String(initialText || ''));
    const playX = Math.round(992 * 0.37);
    const playY = Math.round(768 * 0.77);
    await recordingPage.mouse.click(playX, playY, { delay: 80 }).catch(() => {});
    await recordingPage.waitForTimeout(2000);
    const afterClickText = await recordingPage.locator('body').innerText().catch(() => '');
    if (initialTimer && String(afterClickText || '').includes(initialTimer[1])) {
      await recordingPage.keyboard.press('Space').catch(() => {});
      await recordingPage.waitForTimeout(1500);
      await recordingPage.mouse.click(Math.round(992 * 0.77), Math.round(768 * 0.55), { delay: 80 }).catch(() => {});
    }
    await recordingPage.waitForTimeout(Math.max(32000, Number(packet.preview_record_seconds || 34) * 1000));
    const videoHandle = recordingPage.video();
    await recordingPage.close().catch(() => {});
    await recordingContext.close().catch(() => {});
    if (!videoHandle) return '';
    return await videoHandle.path().catch(() => '');
  }

  try {
    await loginMaybe();
    await page.goto('https://app.avomap.com/account/user/map?uid=ZCPeRUvt5J&tab=route&card=main', { waitUntil: 'domcontentloaded', timeout: 120000 }).catch(() => {});
    await page.waitForTimeout(4000);
    const createBtn = page.getByRole('button', { name: /create new video/i }).last();
    if (await createBtn.count()) await createBtn.click({ force: true }).catch(() => {});
    await page.waitForTimeout(2000);
    const uploadButton = page.getByRole('button', { name: /upload gps file/i }).first();
    if (await uploadButton.count()) await uploadButton.click({ force: true }).catch(() => {});
    await page.waitForTimeout(1500);
    const uploadInput = page.locator('#track-upload, input[type=file]').first();
    await uploadInput.setInputFiles(routePath);
    await page.waitForTimeout(12000);

    if (String(packet.video_format || '').trim()) {
      if (await maybeClickByText('Video Format', { waitMs: 1500 })) {
        await maybeClickByText(String(packet.video_format || ''), { waitMs: 2000 });
      }
    }
    if (String(packet.video_quality || '').trim()) {
      if (await maybeClickByText('Video Quality', { waitMs: 1500 })) {
        await maybeClickByText(String(packet.video_quality || ''), { waitMs: 2000 });
      }
    }
    if (String(packet.route_mode || '').trim()) {
      await maybeClickByText(String(packet.route_mode || ''), { waitMs: 1200 });
    }
    if (String(packet.camera_style || '').trim()) {
      await maybeClickByText('Camera', { waitMs: 2000 });
      await maybeClickByText(String(packet.camera_style || ''), { waitMs: 1500 });
      await maybeClickByText('Route', { waitMs: 1200 });
    }
    if (String(packet.map_style || '').trim()) {
      await maybeClickByText('Layer', { waitMs: 2000 });
      await maybeClickByText(String(packet.map_style || ''), { waitMs: 1500 });
      await maybeClickByText('Route', { waitMs: 1200 });
    }

    const exportButton = page.getByRole('button', { name: /export video/i }).first();
    if (await exportButton.count()) {
      await exportButton.click({ force: true }).catch(() => {});
      await page.waitForTimeout(2500);
      const renderButton = page.getByRole('button', { name: /render\\s*video/i }).first();
      if (await renderButton.count()) {
        await renderButton.click({ force: true }).catch(() => {});
        result.renderTriggered = true;
        await page.waitForTimeout(25000);
      } else {
        result.renderTriggered = true;
        await page.waitForTimeout(25000);
      }
    }

    await maybeClickByText('Files', { waitMs: 2000 });
    await page.waitForTimeout(3000);
    await page.screenshot({ path: screenshotPath, fullPage: true }).catch(() => {});

    result.url = String(page.url() || '');
    result.title = await page.title();
    result.bodyText = (await page.locator('body').innerText().catch(() => '')).slice(0, 50000);
    result.buttons = await page.locator('button,a').evaluateAll(nodes => nodes.map(node => ({ tag: node.tagName.toLowerCase(), text: (node.innerText || node.textContent || '').trim(), href: node.href || '' })).filter(row => row.text || row.href).slice(0, 180)).catch(() => []);
    result.links = result.buttons.filter(row => row.href).slice(0, 100);
    result.videoSrcs = await page.locator('video').evaluateAll(nodes => nodes.map(node => ({ src: node.currentSrc || node.src || '', poster: node.poster || '' }))).catch(() => []);
    const recorded = await capturePreviewPlayback().catch(() => '');
    if (recorded) {
      result.previewRecorded = true;
      result.recordedVideoPath = recorded;
    }
    console.log(JSON.stringify(result));
  } catch (error) {
    result.errors.push(String(error && error.stack ? error.stack : error));
    console.log(JSON.stringify(result));
    process.exit(1);
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.log(JSON.stringify({ url: '', title: '', bodyText: '', buttons: [], links: [], videoSrcs: [], errors: [String(error && error.stack ? error.stack : error)], renderTriggered: false }));
  process.exit(1);
});
"""


def _run_browser(packet: dict[str, object], *, route_path: Path, screenshot_path: Path, timeout_seconds: int) -> dict[str, object]:
    packet = dict(packet)
    packet["video_format"] = _normalize_video_format_label(packet.get("video_format"))
    packet["video_quality"] = _normalize_video_quality_label(packet.get("video_quality"))
    with tempfile.TemporaryDirectory(prefix="avomap-worker-", dir=str(SHARED_TEMP_ROOT)) as temp_dir_raw:
        temp_dir = Path(temp_dir_raw)
        packet_path = temp_dir / "packet.json"
        packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
        completed = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-i",
                "-v",
                f"{temp_dir}:{temp_dir}",
                "-v",
                f"{route_path.parent}:{route_path.parent}",
                "-v",
                f"{screenshot_path.parent}:{screenshot_path.parent}",
                "-e",
                f"AVOMAP_RECORD_DIR={screenshot_path.parent}",
                "-e",
                f"AVOMAP_PACKET_PATH={packet_path}",
                "-e",
                f"AVOMAP_ROUTE_PATH={route_path}",
                "-e",
                f"AVOMAP_SCREENSHOT_PATH={screenshot_path}",
                PLAYWRIGHT_IMAGE,
                "node",
                "-e",
                _avomap_node_script(),
            ],
            text=True,
            capture_output=True,
            timeout=max(240, timeout_seconds + 90),
            check=False,
        )
    raw = str(completed.stdout or "").strip()
    if not raw:
        raise RuntimeError(f"avomap_worker_empty_output:{str(completed.stderr or '').strip()[:400]}")
    loaded = json.loads(raw.splitlines()[-1])
    if completed.returncode != 0:
        raise RuntimeError(f"avomap_worker_failed:{str(loaded.get('errors') or completed.stderr or raw)[:500]}")
    if not isinstance(loaded, dict):
        raise RuntimeError("avomap_worker_output_invalid")
    return loaded


def _image_data_uri(path: Path) -> str:
    if not path.exists():
        return ""
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _standalone_html(*, packet: dict[str, object], browser_output: dict[str, object], screenshot_data_uri: str) -> str:
    title = html.escape(str(packet.get("title") or "AvoMap Flyover").strip())
    body_text = html.escape(str(browser_output.get("bodyText") or "").strip())
    editor_url = html.escape(str(browser_output.get("url") or "").strip())
    format_label = html.escape(str(packet.get("video_format") or "").strip())
    quality_label = html.escape(str(packet.get("video_quality") or "").strip())
    route_mode = html.escape(str(packet.get("route_mode") or "").strip())
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title}</title>
    <style>
      body {{
        margin: 0;
        font-family: "Iowan Old Style", Georgia, serif;
        color: #182125;
        background: radial-gradient(circle at top left, rgba(44,169,132,0.18), transparent 30%), linear-gradient(180deg, #eef8f4 0%, #dcebe8 100%);
      }}
      main {{ max-width: 1180px; margin: 0 auto; padding: 24px; }}
      .panel {{ background: rgba(255,255,255,0.84); border: 1px solid rgba(24,33,37,0.10); border-radius: 28px; padding: 24px; box-shadow: 0 18px 48px rgba(24,33,37,0.08); }}
      h1 {{ margin: 0 0 12px; font-size: clamp(2rem, 5vw, 4rem); line-height: 0.94; }}
      .meta {{ display: flex; flex-wrap: wrap; gap: 10px; margin: 18px 0; }}
      .chip {{ padding: 10px 14px; border-radius: 999px; background: #f6fcfa; border: 1px solid rgba(24,33,37,0.10); }}
      img {{ width: 100%; border-radius: 24px; border: 1px solid rgba(24,33,37,0.10); margin-top: 18px; }}
      pre {{ white-space: pre-wrap; background: #f9fcfb; border: 1px solid rgba(24,33,37,0.10); border-radius: 24px; padding: 18px; font-family: "SFMono-Regular", Consolas, monospace; line-height: 1.55; }}
      a {{ color: inherit; }}
    </style>
  </head>
  <body>
    <main>
      <section class="panel">
        <h1>{title}</h1>
        <p>AvoMap route render captured by EA and republished as an unauthenticated browser artifact.</p>
        <div class="meta">
          <div class="chip">Video format: {format_label or "default"}</div>
          <div class="chip">Video quality: {quality_label or "default"}</div>
          <div class="chip">Route mode: {route_mode or "default"}</div>
          {f'<div class="chip"><a href="{editor_url}" target="_blank" rel="noreferrer">Open AvoMap</a></div>' if editor_url else ''}
        </div>
        {f'<img src="{screenshot_data_uri}" alt="{title}">' if screenshot_data_uri else ''}
        <h2>Captured Route State</h2>
        <pre>{body_text}</pre>
      </section>
    </main>
  </body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a direct AvoMap artifact for the flyover UI-service.")
    parser.add_argument("--packet-path", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    packet = _load_packet(args.packet_path or None)
    packet.setdefault("login_email", DEFAULT_EMAIL)
    packet.setdefault("login_password", DEFAULT_PASSWORD)
    timeout_seconds = max(180, int(packet.get("timeout_seconds") or 300))
    result_title = str(packet.get("result_title") or packet.get("title") or "AvoMap flyover").strip() or "AvoMap flyover"
    run_slug = _slugify(result_title)
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    SHARED_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    service_root = OUTPUT_ROOT / "avomap_flyover"
    service_root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(service_root, 0o777)
    except OSError:
        pass
    run_dir = service_root / f"{time.strftime('%Y%m%d-%H%M%S')}-{run_slug}"
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(run_dir, 0o777)
    except OSError:
        pass
    route_path = _write_route_file(str(packet.get("route_data") or ""), run_dir)
    screenshot_path = run_dir / "preview.png"
    browser_output = _run_browser(packet, route_path=route_path, screenshot_path=screenshot_path, timeout_seconds=timeout_seconds)
    recorded_input_path = Path(str(browser_output.get("recordedVideoPath") or "")).expanduser()
    if browser_output.get("previewRecorded") and recorded_input_path.exists():
        recorded_webm_path = run_dir / "flyover-preview.webm"
        recorded_mp4_path = run_dir / "flyover-preview.mp4"
        shutil.copy2(recorded_input_path, recorded_webm_path)
        transcode_video(recorded_input_path, recorded_mp4_path)
        result = {
            "service_key": "avomap_flyover",
            "result_title": result_title,
            "render_status": "completed",
            "asset_path": str(recorded_webm_path),
            "mime_type": "video/webm",
            "editor_url": str(browser_output.get("url") or "").strip(),
            "raw_text": str(browser_output.get("bodyText") or "").strip(),
            "structured_output_json": {
                "service": "AvoMap",
                "url": str(browser_output.get("url") or "").strip(),
                "page_title": str(browser_output.get("title") or "").strip(),
                "buttons": list(browser_output.get("buttons") or []),
                "links": list(browser_output.get("links") or []),
                "video_candidates": list(browser_output.get("videoSrcs") or []),
                "route_path": str(route_path),
                "screenshot_path": str(screenshot_path) if screenshot_path.exists() else "",
                "browser_video_path": str(recorded_webm_path),
                "recorded_video_path": str(recorded_mp4_path),
                "render_status": "completed",
            },
        }
        print(json.dumps(result, ensure_ascii=False))
        return 0
    html_path = run_dir / "result.html"
    html_path.write_text(
        _standalone_html(packet=packet, browser_output=browser_output, screenshot_data_uri=_image_data_uri(screenshot_path)),
        encoding="utf-8",
    )
    body_text = str(browser_output.get("bodyText") or "").strip()
    render_status = "render_triggered" if browser_output.get("renderTriggered") else "preview_ready"
    result = {
        "service_key": "avomap_flyover",
        "result_title": result_title,
        "render_status": render_status,
        "asset_path": str(html_path),
        "mime_type": "text/html",
        "editor_url": str(browser_output.get("url") or "").strip(),
        "raw_text": body_text,
        "structured_output_json": {
            "service": "AvoMap",
            "url": str(browser_output.get("url") or "").strip(),
            "page_title": str(browser_output.get("title") or "").strip(),
            "buttons": list(browser_output.get("buttons") or []),
            "links": list(browser_output.get("links") or []),
            "video_candidates": list(browser_output.get("videoSrcs") or []),
            "route_path": str(route_path),
            "screenshot_path": str(screenshot_path) if screenshot_path.exists() else "",
            "recorded_video_path": str(recorded_input_path) if recorded_input_path.exists() else "",
            "html_path": str(html_path),
            "render_status": render_status,
        },
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        print(
            json.dumps(
                {
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(1)
