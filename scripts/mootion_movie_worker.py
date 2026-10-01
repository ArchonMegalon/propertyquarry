#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import html
import json
import os
import subprocess
import tempfile
import time
import traceback
import uuid
import urllib.parse
import urllib.request
from pathlib import Path

from browseract_ui_media import compose_slideshow_video, transcode_video_webm


PLAYWRIGHT_IMAGE = os.environ.get("EA_UI_PLAYWRIGHT_IMAGE", "chummer-playwright:local").strip() or "chummer-playwright:local"


def _default_worker_state_root() -> Path:
    if Path("/data/artifacts").exists():
        return Path("/data/artifacts")
    configured = str(os.environ.get("PROPERTYQUARRY_STATE_ROOT") or "").strip()
    if configured:
        return Path(configured).expanduser()
    property_root = Path(os.environ.get("PROPERTYQUARRY_ROOT") or "/docker/property").expanduser()
    return property_root / "state"


_WORKER_STATE_ROOT = _default_worker_state_root()
OUTPUT_ROOT = Path(
    os.environ.get("EA_UI_SERVICE_WORKER_OUTPUT_ROOT")
    or os.environ.get("PROPERTYQUARRY_BROWSERACT_OUTPUT_ROOT")
    or _WORKER_STATE_ROOT / "browseract_ui_worker_outputs"
).expanduser()
SHARED_TEMP_ROOT = Path(
    os.environ.get("EA_UI_SERVICE_SHARED_TEMP_ROOT")
    or os.environ.get("PROPERTYQUARRY_BROWSERACT_SHARED_TEMP_ROOT")
    or _WORKER_STATE_ROOT / "browseract_ui_worker_shared"
).expanduser()
DEFAULT_EMAIL = os.environ.get("EA_UI_SERVICE_LOGIN_EMAIL", "").strip()
DEFAULT_PASSWORD = os.environ.get("EA_UI_SERVICE_LOGIN_PASSWORD", "").strip()


def _load_packet(path: str | None) -> dict[str, object]:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    raw = os.sys.stdin.read()
    if not raw.strip():
        raise RuntimeError("mootion_worker_input_missing")
    loaded = json.loads(raw)
    if not isinstance(loaded, dict):
        raise RuntimeError("mootion_worker_input_invalid")
    return loaded


def _slugify(value: object) -> str:
    lowered = "".join(char.lower() if char.isalnum() else "-" for char in str(value or "").strip())
    lowered = "-".join(part for part in lowered.split("-") if part)
    return lowered or f"movie-{uuid.uuid4().hex[:12]}"


def _prompt_text(packet: dict[str, object]) -> str:
    parts = [str(packet.get("script_text") or "").strip()]
    steering = {
        "Title": packet.get("title"),
        "Audience": packet.get("audience"),
        "Hook": packet.get("hook_line"),
        "Closing line": packet.get("closing_line"),
        "Call to action": packet.get("cta"),
        "Language": packet.get("language"),
        "Aspect ratio": packet.get("aspect_ratio"),
        "Visual style": packet.get("visual_style"),
        "Camera style": packet.get("camera_style"),
        "Voiceover style": packet.get("voiceover_style"),
        "Music mood": packet.get("music_mood"),
        "Caption mode": packet.get("caption_mode"),
        "Shot pacing": packet.get("shot_pacing"),
        "Platform": packet.get("platform_target"),
    }
    for label, value in steering.items():
        text = str(value or "").strip()
        if text:
            parts.append(f"{label}: {text}")
    return "\n".join(part for part in parts if part).strip()


def _mootion_style_label(value: object) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    mappings = (
        (("photo", "real", "estate", "bright", "natural"), "Photorealistic"),
        (("cinematic", "film", "editorial"), "Cinematic"),
        (("anime",), "Anime"),
        (("comic", "retro"), "Retro comics"),
        (("pixel",), "Pixel art"),
        (("illustr", "sketch"), "Illustration"),
        (("cartoon", "3d"), "3D cartoon"),
        (("minimal",), "Minimalist"),
        (("horror",), "Horror"),
    )
    for needles, label in mappings:
        if any(needle in text for needle in needles):
            return label
    return ""


def _mootion_node_script() -> str:
    return r"""
const { chromium } = require('playwright');
const fs = require('fs');

async function main() {
  const packet = JSON.parse(fs.readFileSync(process.env.MOOTION_PACKET_PATH, 'utf8'));
  const screenshotPath = process.env.MOOTION_SCREENSHOT_PATH;
  const downloadPath = process.env.MOOTION_DOWNLOAD_PATH;
  const promptText = String(packet.prompt_text || '');
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1480, height: 1160 } });
  const page = await context.newPage();
  const result = { url: '', title: '', bodyText: '', links: [], buttons: [], videoSrcs: [], imageThumbs: [], errors: [], downloaded: false, projectJson: null, resolvedAssets: [] };

  function firstUrl(value) {
    if (!value) return '';
    if (typeof value === 'string') {
      const match = value.match(/https?:\/\/[^\s"'<>]+/i);
      return match ? match[0] : '';
    }
    if (Array.isArray(value)) {
      for (const entry of value) {
        const resolved = firstUrl(entry);
        if (resolved) return resolved;
      }
      return '';
    }
    if (typeof value === 'object') {
      for (const key of ['url', 'signedUrl', 'signed_url', 'path', 'data', 'result', 'value']) {
        const resolved = firstUrl(value[key]);
        if (resolved) return resolved;
      }
      for (const entry of Object.values(value)) {
        const resolved = firstUrl(entry);
        if (resolved) return resolved;
      }
    }
    return '';
  }

  async function resolveAssetUrl(assetType, assetId) {
    if (!assetType || !assetId) return '';
    const url = `https://ai-frontend.mootion.com/api/v1/assets/valid-path?type=${encodeURIComponent(assetType)}&id=${encodeURIComponent(assetId)}&target=raw`;
    const resp = await context.request.get(url).catch(() => null);
    if (!resp || !resp.ok()) return '';
    const contentType = String(resp.headers()['content-type'] || '');
    if (contentType.includes('application/json')) {
      const payload = await resp.json().catch(() => null);
      return firstUrl(payload);
    }
    const text = await resp.text().catch(() => '');
    return firstUrl(text);
  }

  async function fetchProjectJson(projectId) {
    if (!projectId) return null;
    const resp = await context.request.get(`https://api.mootion.com/story/projects/${projectId}`).catch(() => null);
    if (!resp || !resp.ok()) return null;
    const payload = await resp.json().catch(() => null);
    return payload && typeof payload === 'object' ? payload : null;
  }

  async function resolvedProjectAssets(projectPayload) {
    if (!projectPayload || typeof projectPayload !== 'object') return [];
    const data = projectPayload.data && typeof projectPayload.data === 'object' ? projectPayload.data : projectPayload;
    let content = data.projectJsonContent;
    if (typeof content === 'string') {
      try { content = JSON.parse(content); } catch { content = null; }
    }
    if (!content || typeof content !== 'object') return [];
    const storyboard = content.storyboard && typeof content.storyboard === 'object' ? content.storyboard : {};
    const scenes = Array.isArray(storyboard.scenes) ? storyboard.scenes : [];
    const rows = [];
    for (const scene of scenes) {
      if (!scene || typeof scene !== 'object') continue;
      const assets = Array.isArray(scene.assets) ? scene.assets : [];
      const currentAssetId = String(scene.current_asset || '');
      let selected = assets.find((entry) => entry && String(entry.id || '') === currentAssetId);
      if (!selected && assets.length) selected = assets[0];
      if (!selected || typeof selected !== 'object') continue;
      const assetType = String(selected.type || 'image').toLowerCase();
      const assetValue = String(selected.value || '');
      const resolvedUrl = await resolveAssetUrl(assetType, assetValue);
      rows.push({
        sceneId: String(scene.id || ''),
        subtitle: Array.isArray(scene.subtitle) && scene.subtitle.length ? String((scene.subtitle[0] || {}).original_text || '') : '',
        assetType,
        assetValue,
        resolvedUrl,
      });
    }
    return rows;
  }

  async function maybeClickText(text, options = {}) {
    const label = String(text || '').trim();
    if (!label) return false;
    const pattern = new RegExp(`^${label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`, 'i');
    const candidates = [
      page.getByRole('button', { name: pattern }).first(),
      page.getByText(pattern).first(),
    ];
    for (const locator of candidates) {
      if (await locator.count()) {
        await locator.click({ force: true }).catch(() => {});
        await page.waitForTimeout(Number(options.waitMs || 1000));
        return true;
      }
    }
    return false;
  }

  async function fillPromptEditor(text) {
    const editor = page.locator('[contenteditable="true"][role="textbox"], [contenteditable="true"]').first();
    await editor.waitFor({ timeout: 120000 });
    await editor.click({ force: true }).catch(() => {});
    await page.keyboard.press('Control+A').catch(() => {});
    await page.keyboard.press('Meta+A').catch(() => {});
    await page.keyboard.insertText(String(text || ''));
    await page.waitForTimeout(1200);
  }

  async function loginMaybe() {
    await page.goto('https://storyteller.mootion.com/auth/login', { waitUntil: 'domcontentloaded', timeout: 120000 }).catch(() => {});
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
      await page.waitForTimeout(10000);
    }
  }

  try {
    await loginMaybe();
    await page.goto('https://storyteller.mootion.com/project/new?type=short_video&prompt_type=idea&input=', { waitUntil: 'domcontentloaded', timeout: 120000 }).catch(() => {});
    await Promise.race([
      page.locator('[contenteditable="true"][role="textbox"]').first().waitFor({ timeout: 120000 }),
      page.locator('[contenteditable="true"]').first().waitFor({ timeout: 120000 }),
      page.locator('textarea').first().waitFor({ timeout: 120000 }),
    ]).catch(() => {});
    await page.waitForTimeout(4000);
    await fillPromptEditor(promptText);
    if (String(packet.aspect_ratio || '').trim()) {
      await maybeClickText(String(packet.aspect_ratio || '').trim(), { waitMs: 600 });
    }
    if (String(packet.style_label || '').trim()) {
      await maybeClickText(String(packet.style_label || '').trim(), { waitMs: 700 });
    }
    if (/quick|fast/i.test(String(packet.shot_pacing || ''))) {
      await maybeClickText('Quick pace', { waitMs: 600 });
    }
    const submitButton = page.getByRole('button', { name: /generate/i }).first();
    if (await submitButton.count()) {
      await submitButton.click({ force: true }).catch(() => {});
    }
    await page.waitForTimeout(5000);
    await page.waitForFunction(() => location.pathname.includes('/project/'), null, { timeout: 120000 }).catch(() => {});
    const projectId = String(page.url().split('/project/')[1] || '').split(/[?#]/, 1)[0];

    const startedAt = Date.now();
    const timeoutMs = Math.max(180000, Number(packet.timeout_seconds || 360) * 1000);
    let assetProbeStarted = false;
    while (Date.now() - startedAt < timeoutMs) {
      const videoNodes = await page.locator('video').evaluateAll(nodes => nodes.map(node => ({ src: node.currentSrc || node.src || '', poster: node.poster || '' }))).catch(() => []);
      if (Array.isArray(videoNodes) && videoNodes.some(row => row.src)) {
        result.videoSrcs = videoNodes;
        break;
      }
      const downloadButton = page.getByRole('button', { name: /download|export/i }).first();
      if (await downloadButton.count()) {
        result.buttons.push('download_or_export_visible');
      }
      if (projectId && Date.now() - startedAt >= 30000) {
        assetProbeStarted = true;
        const liveProjectJson = await fetchProjectJson(projectId);
        const liveResolvedAssets = await resolvedProjectAssets(liveProjectJson);
        if (liveProjectJson) {
          result.projectJson = liveProjectJson;
        }
        if (Array.isArray(liveResolvedAssets) && liveResolvedAssets.length) {
          result.resolvedAssets = liveResolvedAssets;
          if (liveResolvedAssets.some((row) => String((row || {}).resolvedUrl || '').trim())) {
            break;
          }
        }
      }
      await page.waitForTimeout(12000);
    }

    result.url = String(page.url() || '');
    result.title = await page.title();
    if (!result.projectJson) {
      result.projectJson = await fetchProjectJson(projectId);
    }
    if (!Array.isArray(result.resolvedAssets) || !result.resolvedAssets.length || !assetProbeStarted) {
      result.resolvedAssets = await resolvedProjectAssets(result.projectJson);
    }
    result.bodyText = (await page.locator('body').innerText().catch(() => '')).slice(0, 50000);
    result.imageThumbs = Array.from(
      new Set(
        await page
          .locator('img')
          .evaluateAll((nodes) =>
            nodes
              .map((node) => node.currentSrc || node.src || '')
              .filter((value) => typeof value === 'string' && value.trim().length > 0)
          )
          .catch(() => [])
      )
    );
    result.links = await page.locator('a[href]').evaluateAll(nodes => nodes.map(node => ({ text: (node.innerText || node.textContent || '').trim(), href: node.href })).slice(0, 120)).catch(() => []);
    result.buttons = Array.from(new Set(result.buttons.concat(await page.locator('button').evaluateAll(nodes => nodes.map(node => (node.innerText || node.textContent || '').trim()).filter(Boolean).slice(0, 120)).catch(() => []))));
    if (result.videoSrcs.length) {
      const src = String(result.videoSrcs[0].src || '');
      if (src) {
        const response = await context.request.get(src).catch(() => null);
        if (response && response.ok()) {
          fs.writeFileSync(downloadPath, await response.body());
          result.downloaded = true;
        }
      }
    }
    await page.screenshot({ path: screenshotPath, fullPage: true }).catch(() => {});
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
  console.log(JSON.stringify({ url: '', title: '', bodyText: '', links: [], buttons: [], videoSrcs: [], errors: [String(error && error.stack ? error.stack : error)], downloaded: false }));
  process.exit(1);
});
"""


def _run_browser(packet: dict[str, object], *, screenshot_path: Path, download_path: Path, timeout_seconds: int) -> dict[str, object]:
    packet = dict(packet)
    packet["prompt_text"] = _prompt_text(packet)
    packet["style_label"] = _mootion_style_label(packet.get("visual_style"))
    with tempfile.TemporaryDirectory(prefix="mootion-worker-", dir=str(SHARED_TEMP_ROOT)) as temp_dir_raw:
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
                f"{screenshot_path.parent}:{screenshot_path.parent}",
                "-v",
                f"{download_path.parent}:{download_path.parent}",
                "-e",
                f"MOOTION_PACKET_PATH={packet_path}",
                "-e",
                f"MOOTION_SCREENSHOT_PATH={screenshot_path}",
                "-e",
                f"MOOTION_DOWNLOAD_PATH={download_path}",
                PLAYWRIGHT_IMAGE,
                "node",
                "-e",
                _mootion_node_script(),
            ],
            text=True,
            capture_output=True,
            timeout=max(300, timeout_seconds + 90),
            check=False,
        )
    raw = str(completed.stdout or "").strip()
    if not raw:
        raise RuntimeError(f"mootion_worker_empty_output:{str(completed.stderr or '').strip()[:400]}")
    loaded = json.loads(raw.splitlines()[-1])
    if completed.returncode != 0:
        raise RuntimeError(f"mootion_worker_failed:{str(loaded.get('errors') or completed.stderr or raw)[:500]}")
    if not isinstance(loaded, dict):
        raise RuntimeError("mootion_worker_output_invalid")
    return loaded


def _image_data_uri(path: Path) -> str:
    if not path.exists():
        return ""
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _download_remote(url: str, target_path: Path) -> Path:
    request = urllib.request.Request(str(url), headers={"User-Agent": "EA-Mootion-Movie/1.0"})
    with urllib.request.urlopen(request, timeout=180) as response:
        target_path.write_bytes(response.read())
    return target_path


def _mootion_storyboard_video(
    browser_output: dict[str, object],
    *,
    run_dir: Path,
    result_title: str,
    aspect_ratio: str,
) -> Path | None:
    rows = [dict(entry) for entry in (browser_output.get("resolvedAssets") or []) if isinstance(entry, dict)]
    if not rows:
        return None
    fallback_image_urls = [str(value or "").strip() for value in (browser_output.get("imageThumbs") or []) if str(value or "").strip()]
    if fallback_image_urls and len(fallback_image_urls) >= len(rows):
        fallback_image_urls = fallback_image_urls[-len(rows) :]
    image_paths: list[Path] = []
    subtitle_lines: list[str] = []
    aspect = str(aspect_ratio or "").strip()
    width, height = (1080, 1920) if aspect == "9:16" else (1080, 1080) if aspect == "1:1" else (1280, 720)
    for index, row in enumerate(rows, start=1):
        if str(row.get("assetType") or "").strip().lower() != "image":
            continue
        resolved_url = str(row.get("resolvedUrl") or "").strip()
        if not resolved_url and index - 1 < len(fallback_image_urls):
            resolved_url = fallback_image_urls[index - 1]
        if not resolved_url:
            continue
        suffix = Path(urllib.parse.urlparse(resolved_url).path).suffix or ".jpg"
        target_path = run_dir / f"storyboard-{index:02d}{suffix}"
        _download_remote(resolved_url, target_path)
        image_paths.append(target_path)
        subtitle_lines.append(str(row.get("subtitle") or "").strip())
    if not image_paths:
        return None
    srt_path = run_dir / "storyboard-subtitles.srt"
    output_path = run_dir / "movie.mp4"
    compose_slideshow_video(
        image_paths,
        output_path,
        subtitle_lines=subtitle_lines,
        width=width,
        height=height,
        subtitle_srt_path=srt_path,
    )
    return output_path


def _standalone_html(*, packet: dict[str, object], browser_output: dict[str, object], screenshot_data_uri: str) -> str:
    title = html.escape(str(packet.get("title") or "Mootion Movie").strip())
    body_text = html.escape(str(browser_output.get("bodyText") or "").strip())
    editor_url = html.escape(str(browser_output.get("url") or "").strip())
    style = html.escape(str(packet.get("visual_style") or "").strip())
    aspect_ratio = html.escape(str(packet.get("aspect_ratio") or "").strip())
    language = html.escape(str(packet.get("language") or "").strip())
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
        color: #1c1722;
        background: radial-gradient(circle at top left, rgba(255,152,0,0.18), transparent 28%), linear-gradient(180deg, #f7f1e6 0%, #e9dccc 100%);
      }}
      main {{ max-width: 1180px; margin: 0 auto; padding: 24px; }}
      .panel {{ background: rgba(255,255,255,0.84); border: 1px solid rgba(28,23,34,0.10); border-radius: 28px; padding: 24px; box-shadow: 0 18px 48px rgba(28,23,34,0.08); }}
      h1 {{ margin: 0 0 12px; font-size: clamp(2rem, 5vw, 4rem); line-height: 0.94; }}
      .meta {{ display: flex; flex-wrap: wrap; gap: 10px; margin: 18px 0; }}
      .chip {{ padding: 10px 14px; border-radius: 999px; background: #fdf8f1; border: 1px solid rgba(28,23,34,0.10); }}
      img {{ width: 100%; border-radius: 24px; border: 1px solid rgba(28,23,34,0.10); margin-top: 18px; }}
      pre {{ white-space: pre-wrap; background: #fffaf4; border: 1px solid rgba(28,23,34,0.10); border-radius: 24px; padding: 18px; font-family: "SFMono-Regular", Consolas, monospace; line-height: 1.55; }}
      a {{ color: inherit; }}
    </style>
  </head>
  <body>
    <main>
      <section class="panel">
        <h1>{title}</h1>
        <p>Mootion project captured by EA and republished as an unauthenticated browser artifact.</p>
        <div class="meta">
          <div class="chip">Visual style: {style or "default"}</div>
          <div class="chip">Aspect ratio: {aspect_ratio or "default"}</div>
          <div class="chip">Language: {language or "default"}</div>
          {f'<div class="chip"><a href="{editor_url}" target="_blank" rel="noreferrer">Open Mootion Project</a></div>' if editor_url else ''}
        </div>
        {f'<img src="{screenshot_data_uri}" alt="{title}">' if screenshot_data_uri else ''}
        <h2>Captured Project State</h2>
        <pre>{body_text}</pre>
      </section>
    </main>
  </body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a direct Mootion artifact for the movie UI-service.")
    parser.add_argument("--packet-path", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    packet = _load_packet(args.packet_path or None)
    packet.setdefault("login_email", DEFAULT_EMAIL)
    packet.setdefault("login_password", DEFAULT_PASSWORD)
    timeout_seconds = max(240, int(packet.get("timeout_seconds") or 360))
    result_title = str(packet.get("result_title") or packet.get("title") or "Mootion movie").strip() or "Mootion movie"
    run_slug = _slugify(result_title)
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    SHARED_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    service_root = OUTPUT_ROOT / "mootion_movie"
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
    screenshot_path = run_dir / "preview.png"
    download_path = run_dir / "movie.mp4"
    browser_output = _run_browser(packet, screenshot_path=screenshot_path, download_path=download_path, timeout_seconds=timeout_seconds)
    if (not download_path.exists() or download_path.stat().st_size <= 0):
        composed_path = _mootion_storyboard_video(
            browser_output,
            run_dir=run_dir,
            result_title=result_title,
            aspect_ratio=str(packet.get("aspect_ratio") or "").strip(),
        )
        if composed_path is not None and composed_path.exists() and composed_path.stat().st_size > 0:
            download_path = composed_path
    body_text = str(browser_output.get("bodyText") or "").strip()
    if download_path.exists() and download_path.stat().st_size > 0:
        browser_video_path = run_dir / "movie.webm"
        transcode_video_webm(download_path, browser_video_path)
        result = {
            "service_key": "mootion_movie",
            "result_title": result_title,
            "render_status": "completed",
            "asset_path": str(browser_video_path),
            "mime_type": "video/webm",
            "editor_url": str(browser_output.get("url") or "").strip(),
            "raw_text": body_text,
            "structured_output_json": {
                "service": "Mootion",
                "url": str(browser_output.get("url") or "").strip(),
                "page_title": str(browser_output.get("title") or "").strip(),
                "buttons": list(browser_output.get("buttons") or []),
                "links": list(browser_output.get("links") or []),
                "video_candidates": list(browser_output.get("videoSrcs") or []),
                "project_json": dict(browser_output.get("projectJson") or {}) if isinstance(browser_output.get("projectJson"), dict) else {},
                "resolved_assets": list(browser_output.get("resolvedAssets") or []),
                "image_thumbs": list(browser_output.get("imageThumbs") or []),
                "browser_video_path": str(browser_video_path),
                "download_path": str(download_path),
                "screenshot_path": str(screenshot_path) if screenshot_path.exists() else "",
                "render_status": "completed",
            },
        }
    else:
        html_path = run_dir / "result.html"
        html_path.write_text(
            _standalone_html(packet=packet, browser_output=browser_output, screenshot_data_uri=_image_data_uri(screenshot_path)),
            encoding="utf-8",
        )
        result = {
            "service_key": "mootion_movie",
            "result_title": result_title,
            "render_status": "project_ready" if "/project/" in str(browser_output.get("url") or "") else "partial",
            "asset_path": str(html_path),
            "mime_type": "text/html",
            "editor_url": str(browser_output.get("url") or "").strip(),
            "raw_text": body_text,
            "structured_output_json": {
                "service": "Mootion",
                "url": str(browser_output.get("url") or "").strip(),
                "page_title": str(browser_output.get("title") or "").strip(),
                "buttons": list(browser_output.get("buttons") or []),
                "links": list(browser_output.get("links") or []),
                "video_candidates": list(browser_output.get("videoSrcs") or []),
                "project_json": dict(browser_output.get("projectJson") or {}) if isinstance(browser_output.get("projectJson"), dict) else {},
                "resolved_assets": list(browser_output.get("resolvedAssets") or []),
                "image_thumbs": list(browser_output.get("imageThumbs") or []),
                "screenshot_path": str(screenshot_path) if screenshot_path.exists() else "",
                "html_path": str(html_path),
                "render_status": "project_ready" if "/project/" in str(browser_output.get("url") or "") else "partial",
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
