#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


USER_AGENT = "PropertyQuarry-Crezlo-Property-Tour/1.0"
PLAYWRIGHT_IMAGE = (
    os.environ.get("PROPERTYQUARRY_CREZLO_PLAYWRIGHT_IMAGE")
    or os.environ.get("EA_CREZLO_PLAYWRIGHT_IMAGE")
    or "propertyquarry-playwright:local"
).strip() or "propertyquarry-playwright:local"
SHARED_TEMP_ROOT = str(os.environ.get("PROPERTYQUARRY_CREZLO_SHARED_TEMP_ROOT") or os.environ.get("EA_CREZLO_SHARED_TEMP_ROOT") or "").strip()
DEFAULT_WORKSPACE_DOMAIN = (
    os.environ.get("PROPERTYQUARRY_CREZLO_WORKSPACE_DOMAIN", "").strip()
    or os.environ.get("EA_CREZLO_WORKSPACE_DOMAIN", "").strip()
    or os.environ.get("CREZLO_WORKSPACE_DOMAIN", "").strip()
    or "propertyquarry-tours.crezlotours.com"
)


def _load_packet(path: str | None) -> dict[str, object]:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    raw = os.sys.stdin.read()
    if not raw.strip():
        raise RuntimeError("crezlo_worker_input_missing")
    loaded = json.loads(raw)
    if not isinstance(loaded, dict):
        raise RuntimeError("crezlo_worker_input_invalid")
    return loaded


def _normalize_url_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(entry or "").strip() for entry in value if str(entry or "").strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _safe_filename(name: str) -> str:
    safe = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in str(name or "").strip())
    safe = safe.strip("._")
    return safe or f"asset_{uuid.uuid4().hex[:12]}"


def _guess_extension(*, source_url: str, content_type: str) -> str:
    guessed = mimetypes.guess_extension((content_type or "").split(";", 1)[0].strip())
    if guessed:
        return guessed
    suffix = Path(urllib.parse.urlparse(source_url).path).suffix
    if suffix:
        return suffix
    return ".bin"


def _download_remote(url: str, dest_dir: Path, *, prefix: str, ordinal: int) -> Path:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=180) as response:
        data = response.read()
        content_type = str(response.headers.get("Content-Type") or "").strip()
    suffix = _guess_extension(source_url=url, content_type=content_type)
    stem = _safe_filename(Path(urllib.parse.urlparse(url).path).stem or prefix)
    target = dest_dir / f"{ordinal:02d}_{prefix}_{stem}_{uuid.uuid4().hex[:8]}{suffix}"
    target.write_bytes(data)
    return target


def _materialize_asset(url: str, dest_dir: Path, *, prefix: str, ordinal: int) -> Path:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme in {"", "file"}:
        candidate = Path(parsed.path if parsed.scheme == "file" else url).expanduser()
        if not candidate.exists():
            raise RuntimeError(f"crezlo_asset_missing:{url}")
        target = dest_dir / f"{ordinal:02d}_{prefix}_{_safe_filename(candidate.name)}"
        shutil.copy2(candidate, target)
        return target
    if parsed.scheme not in {"http", "https"}:
        raise RuntimeError(f"crezlo_asset_scheme_unsupported:{url}")
    return _download_remote(url, dest_dir, prefix=prefix, ordinal=ordinal)


def _select_paths(
    *,
    media_paths: list[Path],
    floorplan_paths: list[Path],
    scene_strategy: str,
    scene_selection_json: dict[str, object],
) -> list[Path]:
    photos = list(media_paths)
    floorplans = list(floorplan_paths)
    if bool(scene_selection_json.get("reverse_photos")):
        photos.reverse()

    requested_indexes = scene_selection_json.get("photo_indexes")
    if isinstance(requested_indexes, list) and requested_indexes:
        selected: list[Path] = []
        for raw in requested_indexes:
            try:
                index = int(raw)
            except Exception:
                continue
            if 0 <= index < len(photos):
                selected.append(photos[index])
        if selected:
            photos = selected

    skipped = set()
    raw_skip = scene_selection_json.get("skip_photo_indexes")
    if isinstance(raw_skip, list):
        for raw in raw_skip:
            try:
                skipped.add(int(raw))
            except Exception:
                continue
    if skipped:
        photos = [path for index, path in enumerate(photos) if index not in skipped]

    max_photos = scene_selection_json.get("max_photos")
    try:
        max_photos_int = max(1, int(max_photos)) if max_photos is not None else 0
    except Exception:
        max_photos_int = 0
    if max_photos_int > 0:
        photos = photos[:max_photos_int]

    include_floorplans = scene_selection_json.get("include_floorplans")
    if include_floorplans is None:
        include_floorplans = scene_strategy not in {"photo_only", "compact_photo_only"}
    floorplan_position = str(scene_selection_json.get("floorplan_position") or "").strip().lower()
    if not floorplan_position:
        if scene_strategy == "layout_first":
            floorplan_position = "start"
        elif scene_strategy in {"photo_only", "compact_photo_only"}:
            floorplan_position = "omit"
        else:
            floorplan_position = "end"

    if scene_strategy == "compact" and not max_photos_int:
        photos = photos[: min(6, len(photos))]
    elif scene_strategy == "story_first" and len(photos) > 8:
        hero = photos[:1]
        body = photos[1:6]
        tail = photos[-2:]
        photos = hero + body + tail

    if not include_floorplans or floorplan_position == "omit":
        return photos
    if floorplan_position == "start":
        return floorplans + photos
    if floorplan_position == "alternate":
        combined: list[Path] = []
        paired = max(len(photos), len(floorplans))
        for index in range(paired):
            if index < len(photos):
                combined.append(photos[index])
            if index < len(floorplans):
                combined.append(floorplans[index])
        return combined
    return photos + floorplans


def _workspace_base_url(packet: dict[str, object]) -> str:
    workspace_domain = str(packet.get("workspace_domain") or DEFAULT_WORKSPACE_DOMAIN).strip()
    return str(packet.get("workspace_base_url") or f"https://{workspace_domain}").strip()


def _playwright_script() -> str:
    return r"""
const { chromium } = require('playwright');
const fs = require('fs');

async function main() {
  const packetPath = process.env.CREZLO_PACKET_PATH;
  const packet = JSON.parse(fs.readFileSync(packetPath, 'utf8'));
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1024 } });
  const page = await context.newPage();
  const result = {
    creation_mode: 'crezlo_ui_worker',
    workspace_base_url: packet.workspace_base_url,
    workspace_tours_url: packet.workspace_tours_url,
    uploaded_files: (packet.local_media_paths || []).map((entry) => String(entry)),
    logs: [],
  };

  const log = (message) => result.logs.push(String(message || ''));
  let transferWorkspaceId = String(packet.workspace_id || '').trim();

  // The workspace app keeps its active workspace in a cookie (not
  // localStorage).  The picker clears that cookie before issuing the signed
  // cross-host transfer, so restore it after authentication and before the
  // target app's first API request.  Without it the target page renders but
  // every tours request is rejected with `workspace_id is required`.
  async function setActiveWorkspaceCookie() {
    if (!transferWorkspaceId) return;
    await context.addCookies([{
      name: 'tours-active-workspace',
      value: transferWorkspaceId,
      domain: '.crezlotours.com',
      path: '/',
    }]).catch((error) => log(`workspace_cookie_set_failed:${error}`));
  }

  // Crezlo currently emits an internal workspace hostname that already ends
  // in `.crezlotours.com`, then appends that suffix again during the transfer
  // POST.  Keep the provider's signed transfer query intact but repair only
  // this deterministic host typo before Chromium requests it.
  await page.route('**/*', async (route) => {
    const requestUrl = String(route.request().url() || '');
    let repairedUrl = requestUrl.replace(
      '.crezlotours.com.crezlotours.com',
      '.crezlotours.com',
    );
    if (repairedUrl !== requestUrl && transferWorkspaceId) {
      try {
        const parsed = new URL(repairedUrl);
        if (parsed.searchParams.has('transfer') && !parsed.searchParams.has('workspace_id')) {
          parsed.searchParams.set('workspace_id', transferWorkspaceId);
          repairedUrl = parsed.toString();
        }
      } catch (error) {
        log(`workspace_transfer_url_parse_failed:${error}`);
      }
    }
    if (repairedUrl !== requestUrl) {
      await route.continue({ url: repairedUrl });
      return;
    }
    await route.continue();
  });

  function workspaceLabelCandidates(packet) {
    const labels = [];
    const push = (value) => {
      const text = String(value || '').trim();
      if (text && !labels.includes(text)) labels.push(text);
    };
    const configured = packet.workspace_label_candidates;
    if (Array.isArray(configured)) {
      for (const entry of configured) push(entry);
    }
    push(packet.workspace_label);
    push(packet.workspace_name);
    const workspaceDomain = String(packet.workspace_domain || '').trim();
    const workspaceSlug = workspaceDomain.split('.')[0] || '';
    if (workspaceSlug) {
      const normalized = workspaceSlug.replace(/-\d{6,}$/i, '');
      const derived = normalized
        .split('-')
        .filter(Boolean)
        .map((part) => {
          const cleaned = String(part || '').trim();
          if (!cleaned) return '';
          if (cleaned.length <= 2) return cleaned.toUpperCase();
          return cleaned.charAt(0).toUpperCase() + cleaned.slice(1);
        })
        .filter(Boolean)
        .join(' ');
      push(derived);
    }
    return labels;
  }

  async function enterWorkspace() {
    await page.goto('https://tours.crezlo.com/admin/workspaces', { waitUntil: 'domcontentloaded', timeout: 120000 }).catch(() => {});
    await page.waitForTimeout(4000);
    const desiredDomain = String(packet.workspace_domain || '').trim().toLowerCase();
    if (desiredDomain && String(page.url() || '').toLowerCase().includes(desiredDomain) && String(page.url() || '').includes('/admin/tours')) {
      return;
    }
    const candidates = workspaceLabelCandidates(packet);
    // The provider may rotate the workspace FQDN.  Resolve the current
    // workspace from the authenticated seller API before trying a label so a
    // stale env/default hostname cannot strand the worker on the picker.
    const workspaceResponse = await page.request.get('https://tours.crezlo.com/api/seller/tours/workspaces').catch(() => null);
    if (workspaceResponse && workspaceResponse.ok()) {
      try {
        const workspacePayload = await workspaceResponse.json();
        const workspaceRows = (((workspacePayload || {}).data || {}).data || []);
        const requestedId = String(packet.workspace_id || '').trim();
        const requestedName = String(packet.workspace_name || packet.workspace_label || '').trim().toLowerCase();
        const selected = workspaceRows.find((row) => {
          const id = String((row || {}).id || '').trim();
          const name = String((row || {}).name || '').trim().toLowerCase();
          return (requestedId && id === requestedId) || (requestedName && name === requestedName);
        }) || (workspaceRows.length === 1 ? workspaceRows[0] : null);
        if (selected) {
          transferWorkspaceId = String(selected.id || '').trim() || transferWorkspaceId;
          await setActiveWorkspaceCookie();
          result.workspace_id = String(selected.id || '');
          result.workspace_name = String(selected.name || '');
          result.workspace_domain = String(selected.internal_fqdn || selected.external_fqdn || '');
          result.workspace_base_url = result.workspace_domain ? `https://${result.workspace_domain}` : '';
          const workspaceCard = page
            .locator('[title="Use Workspace"]')
            .filter({ hasText: result.workspace_name })
            .first();
          if (await workspaceCard.count()) {
            await workspaceCard.click({ force: true }).catch(() => {});
            await page.waitForTimeout(5000);
            if (String(page.url() || '').includes('/admin/tours')) return;
          }
        }
      } catch (error) {
        log(`workspace_api_parse_failed:${error}`);
      }
    }
    await setActiveWorkspaceCookie();
    for (const label of candidates) {
      const candidate = page.getByText(new RegExp(label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i')).first();
      if (!await candidate.count()) {
        continue;
      }
      await candidate.click({ force: true }).catch(() => {});
      await page.waitForTimeout(6000);
      const currentUrl = String(page.url() || '').toLowerCase();
      if ((!desiredDomain || currentUrl.includes(desiredDomain)) && currentUrl.includes('/admin/tours')) {
        return;
      }
    }
    const addTourButton = page.getByRole('button', { name: /add tour/i }).first();
    if (await addTourButton.count()) {
      return;
    }
    throw new Error(`workspace_tours_unreachable:${page.url()}`);
  }

  async function maybeLogin() {
    await page.goto('https://tours.crezlo.com/admin/login', { waitUntil: 'domcontentloaded', timeout: 120000 }).catch(() => {});
    await page.waitForTimeout(2000);
    const emailField = page.locator('input[type="email"], input[placeholder*="email" i]').first();
    if (await emailField.count()) {
      await emailField.fill(String(packet.login_email || ''));
      const continueButton = page.getByRole('button', { name: /continue|next|submit/i }).first();
      if (await continueButton.count()) {
        await continueButton.click({ force: true }).catch(() => {});
      }
      await page.waitForTimeout(1500);
    }
    const passwordField = page.locator('input[type="password"]').first();
    if (await passwordField.count()) {
      await passwordField.fill(String(packet.login_password || ''));
      const loginButton = page.getByRole('button', { name: /log ?in|sign ?in|submit/i }).first();
      if (await loginButton.count()) {
        await loginButton.click({ force: true }).catch(() => {});
      }
      await page.waitForTimeout(6000);
    }
  }

  try {
    await maybeLogin();
    if (String(page.url() || '').includes('accounts.crezlo.com/login')) {
      await maybeLogin();
    }
    await enterWorkspace();
    await page.waitForTimeout(4000);

    const createTourResponse = page.waitForResponse(
      (response) => response.request().method() === 'POST' && response.url().includes('/api/seller/tours?product_type=tours'),
      { timeout: Math.max(60000, Number(packet.timeout_seconds || 240) * 1000) }
    ).catch(() => null);
    const createScenesResponse = page.waitForResponse(
      (response) => response.request().method() === 'POST' && /\/api\/seller\/tours\/.+\/scenes\?product_type=tours/.test(response.url()),
      { timeout: Math.max(60000, Number(packet.timeout_seconds || 240) * 1000) }
    ).catch(() => null);

    const addButton = page.getByRole('button', { name: /add tour/i }).first();
    if (!await addButton.count()) {
      // Keep this diagnostic bounded.  A provider workspace can render its
      // tour list while omitting the create control when the workspace
      // permission/domain context is missing; waiting for a click in that
      // state only burns the whole worker timeout and hides the real cause.
      throw new Error(`workspace_create_permission_unavailable:${page.url()}`);
    }
    await addButton.click({ force: true });
    await page.waitForTimeout(1500);

    const titleInput = page.locator('input[placeholder*="Tour Title" i], input[placeholder*="title" i]').first();
    await titleInput.fill(String(packet.tour_title || 'Untitled Tour'));
    await page.waitForTimeout(500);

    const fileInput = page.locator('input[type=file]').last();
    await fileInput.setInputFiles((packet.local_media_paths || []).map((entry) => String(entry)));
    await page.waitForTimeout(8000);

    const createButton = page.getByRole('button', { name: /create tour/i }).first();
    await page.waitForFunction(() => {
      const buttons = Array.from(document.querySelectorAll('button'));
      const target = buttons.find((entry) => (entry.textContent || '').toLowerCase().includes('create tour'));
      return Boolean(target && !target.disabled);
    }, null, { timeout: 60000 }).catch(() => {});
    await createButton.click({ force: true });

    const created = await createTourResponse;
    const scenes = await createScenesResponse;
    if (created) {
      try {
        result.create_response_json = await created.json();
      } catch (error) {
        log(`create_response_parse_failed:${error}`);
      }
    }
    if (scenes) {
      try {
        result.scenes_response_json = await scenes.json();
      } catch (error) {
        log(`scenes_response_parse_failed:${error}`);
      }
    }

    const createdData = (result.create_response_json || {}).data || {};
    result.tour_id = String(createdData.id || '');
    result.slug = String(createdData.slug || '');
    result.tour_status = String(createdData.status || 'created') || 'created';
    result.tour_title = String(createdData.title || packet.tour_title || '');

    if (result.tour_id) {
      const editorBaseUrl = String(result.workspace_base_url || packet.workspace_base_url || '').replace(/\/$/, '');
      result.editor_url = `${editorBaseUrl}/admin/tours/${result.tour_id}`;
      await page.goto(String(result.editor_url), { waitUntil: 'domcontentloaded', timeout: 120000 }).catch(() => {});
      await page.waitForTimeout(5000);
    } else {
      result.editor_url = String(page.url() || '');
    }
    result.page_url = String(page.url() || '');
    result.scene_count = Array.isArray(((result.scenes_response_json || {}).data || {}).data)
      ? (((result.scenes_response_json || {}).data || {}).data).length
      : 0;
    console.log(JSON.stringify(result));
    await browser.close();
  } catch (error) {
    result.error = String(error && error.stack ? error.stack : error);
    console.log(JSON.stringify(result));
    await browser.close();
    process.exit(1);
  }
}

main().catch((error) => {
  console.log(JSON.stringify({ creation_mode: 'crezlo_ui_worker', error: String(error && error.stack ? error.stack : error) }));
  process.exit(1);
});
"""


def _run_playwright(packet: dict[str, object], *, temp_dir: Path, timeout_seconds: int) -> dict[str, object]:
    packet_path = temp_dir / "packet.json"
    packet_path.write_text(json.dumps(packet, ensure_ascii=True), encoding="utf-8")
    command = [
        "docker",
        "run",
        "--rm",
        "-i",
        "-v",
        f"{temp_dir}:{temp_dir}",
        "-e",
        f"CREZLO_PACKET_PATH={packet_path}",
        PLAYWRIGHT_IMAGE,
        "node",
        "-",
    ]
    completed = subprocess.run(
        command,
        input=_playwright_script(),
        text=True,
        capture_output=True,
        timeout=max(120, timeout_seconds),
        check=False,
    )
    raw = str(completed.stdout or "").strip()
    if not raw:
        detail = str(completed.stderr or "").strip()
        raise RuntimeError(f"crezlo_worker_empty_output:{detail[:400]}")
    last_line = raw.splitlines()[-1].strip()
    try:
        loaded = json.loads(last_line)
    except Exception as exc:
        raise RuntimeError(f"crezlo_worker_non_json:{last_line[:400]}") from exc
    if completed.returncode != 0:
        detail = str(loaded.get("error") or completed.stderr or raw).strip()
        raise RuntimeError(f"crezlo_worker_failed:{detail[:400]}")
    if not isinstance(loaded, dict):
        raise RuntimeError("crezlo_worker_invalid_output")
    return loaded


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a Crezlo property tour through the live UI.")
    parser.add_argument("--input-json", default="")
    args = parser.parse_args()

    packet = _load_packet(args.input_json or None)
    login_email = str(packet.get("login_email") or os.environ.get("EA_CREZLO_LOGIN_EMAIL") or "").strip()
    login_password = str(packet.get("login_password") or os.environ.get("EA_CREZLO_LOGIN_PASSWORD") or "").strip()
    if not login_email:
        raise SystemExit("crezlo_login_email_missing")
    if not login_password:
        raise SystemExit("crezlo_login_password_missing")

    scene_strategy = str(packet.get("scene_strategy") or "balanced").strip().lower() or "balanced"
    scene_selection_json = dict(packet.get("scene_selection_json") or {})
    timeout_seconds = int(packet.get("timeout_seconds") or 240)

    temp_root_kwargs: dict[str, object] = {"prefix": "crezlo_property_tour_"}
    if SHARED_TEMP_ROOT:
        shared_root = Path(SHARED_TEMP_ROOT).expanduser()
        shared_root.mkdir(parents=True, exist_ok=True)
        temp_root_kwargs["dir"] = str(shared_root)

    with tempfile.TemporaryDirectory(**temp_root_kwargs) as temp_root:
        temp_dir = Path(temp_root)
        downloads_dir = temp_dir / "downloads"
        downloads_dir.mkdir(parents=True, exist_ok=True)

        media_urls = _normalize_url_list(packet.get("media_urls_json"))
        floorplan_urls = _normalize_url_list(packet.get("floorplan_urls_json"))

        media_paths = [
            _materialize_asset(url, downloads_dir, prefix="photo", ordinal=index)
            for index, url in enumerate(media_urls, start=1)
        ]
        floorplan_paths = [
            _materialize_asset(url, downloads_dir, prefix="floorplan", ordinal=index)
            for index, url in enumerate(floorplan_urls, start=1)
        ]
        selected_paths = _select_paths(
            media_paths=media_paths,
            floorplan_paths=floorplan_paths,
            scene_strategy=scene_strategy,
            scene_selection_json=scene_selection_json,
        )
        if not selected_paths:
            raise SystemExit("crezlo_media_missing")

        workspace_base_url = _workspace_base_url(packet)
        workspace_tours_url = str(packet.get("workspace_tours_url") or f"{workspace_base_url.rstrip('/')}/admin/tours").strip()
        worker_packet = {
            "login_email": login_email,
            "login_password": login_password,
            "tour_title": str(packet.get("tour_title") or "").strip(),
            "workspace_id": str(packet.get("workspace_id") or "").strip(),
            "workspace_domain": str(packet.get("workspace_domain") or DEFAULT_WORKSPACE_DOMAIN).strip(),
            "workspace_base_url": workspace_base_url,
            "workspace_tours_url": workspace_tours_url,
            "workspace_label": str(packet.get("workspace_label") or "").strip(),
            "workspace_name": str(packet.get("workspace_name") or "").strip(),
            "workspace_label_candidates": list(packet.get("workspace_label_candidates") or []),
            "floorplan_analysis_json": packet.get("floorplan_analysis_json") or packet.get("floorplan_analysis") or {},
            "floorplan_analysis_path": str(packet.get("floorplan_analysis_path") or "").strip(),
            "local_media_paths": [str(path) for path in selected_paths],
            "timeout_seconds": timeout_seconds,
        }
        result = _run_playwright(worker_packet, temp_dir=temp_dir, timeout_seconds=timeout_seconds)
        result["selected_media_count"] = len(selected_paths)
        result["source_media_count"] = len(media_paths)
        result["source_floorplan_count"] = len(floorplan_paths)
        result["scene_strategy"] = scene_strategy
        result["scene_selection_json"] = scene_selection_json
        result["workspace_domain"] = str(
            result.get("workspace_domain")
            or packet.get("workspace_domain")
            or DEFAULT_WORKSPACE_DOMAIN
        ).strip()
        result["workspace_base_url"] = str(
            result.get("workspace_base_url")
            or (f"https://{result['workspace_domain']}" if result["workspace_domain"] else "")
            or workspace_base_url
        ).strip()
        result["workspace_tours_url"] = str(
            result.get("workspace_tours_url")
            or (
                f"{result['workspace_base_url'].rstrip('/')}/admin/tours"
                if result["workspace_base_url"]
                else ""
            )
            or workspace_tours_url
        ).strip()
        print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
