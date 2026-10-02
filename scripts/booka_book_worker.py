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
from pathlib import Path


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
        raise RuntimeError("booka_worker_input_missing")
    loaded = json.loads(raw)
    if not isinstance(loaded, dict):
        raise RuntimeError("booka_worker_input_invalid")
    return loaded


def _slugify(value: object) -> str:
    lowered = "".join(char.lower() if char.isalnum() else "-" for char in str(value or "").strip())
    lowered = "-".join(part for part in lowered.split("-") if part)
    return lowered or f"book-{uuid.uuid4().hex[:12]}"


def _booka_node_script() -> str:
    return r"""
const { chromium } = require('playwright');
const fs = require('fs');

async function main() {
  const packet = JSON.parse(fs.readFileSync(process.env.BOOKA_PACKET_PATH, 'utf8'));
  const screenshotPath = process.env.BOOKA_SCREENSHOT_PATH;
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1200 } });
  const page = await context.newPage();
  const result = { url: '', title: '', bodyText: '', labels: [], buttons: [], errors: [] };

  async function loginMaybe() {
    await page.goto('https://app.firstbook.ai/', { waitUntil: 'domcontentloaded', timeout: 120000 }).catch(() => {});
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

  try {
    await loginMaybe();
    await page.goto('https://app.firstbook.ai/', { waitUntil: 'domcontentloaded', timeout: 120000 }).catch(() => {});
    await page.waitForTimeout(4000);

    const startNewBookButton = page.getByRole('button', { name: /start a new book/i }).first();
    if (await startNewBookButton.count()) {
      await startNewBookButton.click({ force: true }).catch(() => {});
      await Promise.race([
        page.locator("input[placeholder*='Remote Manager']").first().waitFor({ timeout: 45000 }),
        page.locator("input[placeholder*='The Remote Manager']").first().waitFor({ timeout: 45000 }),
        page.getByText(/Define Your Book/i).waitFor({ timeout: 45000 }),
      ]).catch(() => {});
      await page.waitForTimeout(2000);
    }

    const titleField = page.locator("input[placeholder*='The Remote Manager']").first();
    if (await titleField.count()) await titleField.fill(String(packet.title || ''));
    const promptField = page.locator("textarea[placeholder*='Remote management']").first();
    if (await promptField.count()) await promptField.fill(String(packet.book_prompt || ''));
    const audienceField = page.locator("input[placeholder*='Series A Founders']").first();
    if (await audienceField.count()) await audienceField.fill(String(packet.audience || ''));
    const goalSelect = page.locator('select').first();
    if (await goalSelect.count()) {
      const desiredGoal = String(packet.goal || '').trim();
      if (desiredGoal) {
        const options = await goalSelect.locator('option').evaluateAll(nodes => nodes.map(n => ({ value: n.value, label: (n.textContent || '').trim() })));
        const match = options.find(row => row.label.toLowerCase().includes(desiredGoal.toLowerCase())) || options[0];
        if (match) {
          await goalSelect.selectOption(match.value).catch(() => {});
        }
      }
    }
    const backgroundField = page.locator("textarea[placeholder*='15 years leading distributed']").first();
    if (await backgroundField.count()) await backgroundField.fill(String(packet.professional_background || ''));
    const beliefsField = page.locator("textarea[placeholder*='Async is better']").first();
    if (await beliefsField.count()) await beliefsField.fill(String(packet.key_beliefs || ''));
    const anecdotesField = page.locator("textarea[placeholder*='The time I fired']").first();
    if (await anecdotesField.count()) await anecdotesField.fill(String(packet.anecdotes || ''));
    const toneField = page.locator("textarea[placeholder*='Short, punchy sentences']").first();
    if (await toneField.count()) await toneField.fill(String(packet.tone || ''));
    const writingSampleField = page.locator("textarea[placeholder*='Paste sample text here']").first();
    if (await writingSampleField.count()) await writingSampleField.fill(String(packet.writing_sample || ''));
    const referencesField = page.locator("input[placeholder*='Atomic Habits']").first();
    if (await referencesField.count()) await referencesField.fill(String(packet.style_references || ''));

    const generateButton = page.getByRole('button', { name: /generate book framework/i }).first();
    await generateButton.click({ force: true }).catch(() => {});
    await Promise.race([
      page.getByText(/PHASE 2: STRUCTURE/i).waitFor({ timeout: Math.max(45000, Number(packet.timeout_seconds || 240) * 1000) }),
      page.getByText(/Refine Your Outline/i).waitFor({ timeout: Math.max(45000, Number(packet.timeout_seconds || 240) * 1000) }),
    ]).catch(() => {});
    await page.waitForTimeout(4000);
    await page.screenshot({ path: screenshotPath, fullPage: true }).catch(() => {});

    result.url = String(page.url() || '');
    result.title = await page.title();
    result.bodyText = (await page.locator('body').innerText().catch(() => '')).slice(0, 50000);
    result.labels = await page.locator('label,h1,h2,h3').evaluateAll(nodes => nodes.map(node => (node.innerText || node.textContent || '').trim()).filter(Boolean).slice(0, 120)).catch(() => []);
    result.buttons = await page.locator('button').evaluateAll(nodes => nodes.map(node => (node.innerText || node.textContent || '').trim()).filter(Boolean).slice(0, 120)).catch(() => []);
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
  console.log(JSON.stringify({ url: '', title: '', bodyText: '', labels: [], buttons: [], errors: [String(error && error.stack ? error.stack : error)] }));
  process.exit(1);
});
"""


def _run_browser(packet: dict[str, object], *, screenshot_path: Path, timeout_seconds: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="booka-worker-", dir=str(SHARED_TEMP_ROOT)) as temp_dir_raw:
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
                "-e",
                f"BOOKA_PACKET_PATH={packet_path}",
                "-e",
                f"BOOKA_SCREENSHOT_PATH={screenshot_path}",
                PLAYWRIGHT_IMAGE,
                "node",
                "-e",
                _booka_node_script(),
            ],
            text=True,
            capture_output=True,
            timeout=max(180, timeout_seconds + 60),
            check=False,
        )
    raw = str(completed.stdout or "").strip()
    if not raw:
        raise RuntimeError(f"booka_worker_empty_output:{str(completed.stderr or '').strip()[:400]}")
    loaded = json.loads(raw.splitlines()[-1])
    if completed.returncode != 0:
        raise RuntimeError(f"booka_worker_failed:{str(loaded.get('errors') or completed.stderr or raw)[:500]}")
    if not isinstance(loaded, dict):
        raise RuntimeError("booka_worker_output_invalid")
    return loaded


def _image_data_uri(path: Path) -> str:
    if not path.exists():
        return ""
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _standalone_html(*, packet: dict[str, object], browser_output: dict[str, object], screenshot_data_uri: str) -> str:
    title = html.escape(str(packet.get("title") or browser_output.get("title") or "Booka Book").strip())
    body_text = html.escape(str(browser_output.get("bodyText") or "").strip())
    editor_url = html.escape(str(browser_output.get("url") or "").strip())
    audience = html.escape(str(packet.get("audience") or "").strip())
    goal = html.escape(str(packet.get("goal") or "").strip())
    tone = html.escape(str(packet.get("tone") or "").strip())
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
        color: #1f1b16;
        background: linear-gradient(180deg, #faf4ea 0%, #efe3d0 100%);
      }}
      main {{ max-width: 1100px; margin: 0 auto; padding: 24px; }}
      .panel {{
        background: rgba(255,255,255,0.82);
        border: 1px solid rgba(31,27,22,0.10);
        border-radius: 28px;
        padding: 24px;
        box-shadow: 0 18px 48px rgba(31,27,22,0.08);
      }}
      h1 {{ margin: 0 0 12px; font-size: clamp(2rem, 5vw, 4rem); line-height: 0.94; }}
      .meta {{ display: flex; flex-wrap: wrap; gap: 10px; margin: 18px 0; }}
      .chip {{ padding: 10px 14px; border-radius: 999px; background: #fbf7f1; border: 1px solid rgba(31,27,22,0.10); }}
      a {{ color: inherit; }}
      img {{ width: 100%; border-radius: 24px; border: 1px solid rgba(31,27,22,0.10); margin-top: 18px; }}
      pre {{
        white-space: pre-wrap;
        background: #fcfaf6;
        border: 1px solid rgba(31,27,22,0.10);
        border-radius: 24px;
        padding: 18px;
        line-height: 1.55;
        font-family: "SFMono-Regular", Consolas, monospace;
      }}
    </style>
  </head>
  <body>
    <main>
      <section class="panel">
        <h1>{title}</h1>
        <p>First Book AI output captured by EA and republished as a browser-openable artifact.</p>
        <div class="meta">
          <div class="chip">Audience: {audience or "n/a"}</div>
          <div class="chip">Goal: {goal or "n/a"}</div>
          <div class="chip">Tone: {tone or "n/a"}</div>
          {f'<div class="chip"><a href="{editor_url}" target="_blank" rel="noreferrer">Open First Book AI</a></div>' if editor_url else ''}
        </div>
        {f'<img src="{screenshot_data_uri}" alt="{title}">' if screenshot_data_uri else ''}
        <h2>Captured Outline</h2>
        <pre>{body_text}</pre>
      </section>
    </main>
  </body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a direct First Book AI artifact for the Booka UI-service.")
    parser.add_argument("--packet-path", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    packet = _load_packet(args.packet_path or None)
    packet.setdefault("login_email", DEFAULT_EMAIL)
    packet.setdefault("login_password", DEFAULT_PASSWORD)
    timeout_seconds = max(120, int(packet.get("timeout_seconds") or 240))
    run_slug = _slugify(packet.get("title") or packet.get("result_title") or f"booka-{uuid.uuid4().hex[:8]}")
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    SHARED_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    service_root = OUTPUT_ROOT / "booka_book"
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
    browser_output = _run_browser(packet, screenshot_path=screenshot_path, timeout_seconds=timeout_seconds)
    screenshot_data_uri = _image_data_uri(screenshot_path)
    html_path = run_dir / "result.html"
    html_path.write_text(
        _standalone_html(packet=packet, browser_output=browser_output, screenshot_data_uri=screenshot_data_uri),
        encoding="utf-8",
    )
    result_title = str(packet.get("result_title") or packet.get("title") or "First Book AI result").strip() or "First Book AI result"
    body_text = str(browser_output.get("bodyText") or "").strip()
    render_status = "completed" if "PHASE 2: STRUCTURE" in body_text or "Refine Your Outline" in body_text else "partial"
    result = {
        "service_key": "booka_book",
        "result_title": result_title,
        "render_status": render_status,
        "asset_path": str(html_path),
        "mime_type": "text/html",
        "editor_url": str(browser_output.get("url") or "").strip(),
        "raw_text": body_text,
        "structured_output_json": {
            "service": "First Book AI",
            "url": str(browser_output.get("url") or "").strip(),
            "page_title": str(browser_output.get("title") or "").strip(),
            "labels": list(browser_output.get("labels") or []),
            "buttons": list(browser_output.get("buttons") or []),
            "screenshot_path": str(screenshot_path) if screenshot_path.exists() else "",
            "html_path": str(html_path),
            "result_title": result_title,
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
