const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const https = require('https');

const ROOT = path.resolve(process.env.PROPERTYQUARRY_ROOT || path.resolve(__dirname, '..'));
const EA_ROOT = path.resolve(process.env.PROPERTYQUARRY_EA_ROOT || '/docker/EA');
const OUT_DIR = path.resolve(
  process.env.PROPERTYQUARRY_MAGICFIT_CLIPS_DIR ||
  path.join(ROOT, '_completion', 'propertyquarry_magicfit_promo_20260606', 'magicfit_clips')
);
const ENV_FILES = [
  path.join(ROOT, '.env'),
  path.join(EA_ROOT, '.env'),
];

function loadEnv(file) {
  if (!fs.existsSync(file)) return;
  for (const raw of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith('#') || !line.includes('=')) continue;
    const idx = line.indexOf('=');
    const key = line.slice(0, idx).trim();
    let value = line.slice(idx + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    if (!(key in process.env)) process.env[key] = value;
  }
}

for (const file of ENV_FILES) loadEnv(file);

const GLOBAL_NEGATIVE = [
  'no storyboard',
  'no slideshow',
  'no SVG',
  'no flat vector animation',
  'no generic office SaaS explainer',
  'no cartoon',
  'no toy figures',
  'no generated title text',
  'no readable UI labels',
  'no product logo text',
  'no warped hands',
  'no watermark',
  'no extra fingers',
  'no stock photo smile',
].join(', ');

const SCENES = [
  {
    id: '01_chaos_am_tisch',
    title: 'Chaos am Tisch',
    duration: 7,
    prompt: 'Photoreal premium real-estate decision trailer in Vienna at night. Mara, a credible woman in her mid 30s, sits at a warm apartment desk with laptop glow, city lights outside, too many blurred property tabs, vibrating phone, printed floorplan, sticky notes, coffee, mild fatigue, cinematic handheld macro inserts, warm and moody, no readable text.',
  },
  {
    id: '02_die_frage',
    title: 'Die Frage',
    duration: 8,
    prompt: 'Photoreal cinematic evening apartment interior in Vienna. Mara at laptop, Jonas enters gently and points to the property screen, intimate realistic dialogue moment, subtle frustration and uncertainty, warm tungsten light, believable couple energy, shallow depth of field, no readable screen text.',
  },
  {
    id: '03_search_brief',
    title: 'Search Brief',
    duration: 10,
    prompt: 'Photoreal premium SaaS product film. Close-up of Mara calmly entering property preferences into a clean modern app on laptop in a warm apartment. Structured search-brief workflow, Austrian rental search, floorplan requirement, gas heating avoided, lift and outdoor space preferred, but no readable UI text. Stable camera, calm controlled motion, editorial quality.',
  },
  {
    id: '04_market_scan',
    title: 'Market Scan',
    duration: 11,
    prompt: 'Photoreal abstract premium product visualization of a property market scan. Realistic listing cards, map fragments, floorplan thumbnails, risk chips and shortlist cards floating in a dark warm space, cards filtered and sorted with precision, restrained intelligence aesthetic, no sci-fi hologram excess, cinematic product trailer energy, no readable text.',
  },
  {
    id: '05_dossier',
    title: 'Dossier',
    duration: 12,
    prompt: 'Photoreal due-diligence desk scene in the same Vienna apartment. Laptop, printed floorplan, notebook and pen now arranged neatly. Mara reviews a structured property dossier with calm focus, Jonas nearby. Premium real-estate decision atmosphere, warm light, controlled confidence, no readable UI text.',
  },
  {
    id: '06_tour_tradeoff',
    title: 'Tour Tradeoff',
    duration: 12,
    prompt: 'Photoreal high-end modern apartment interior in Vienna, slow 360-tour style movement through bright living room with wood floors, clean walls, premium daylight real-estate viewing. Cinematic pause points where analysis overlays could later appear. Beautiful but grounded, no text.',
  },
  {
    id: '07_packet_share',
    title: 'Packet Share',
    duration: 10,
    prompt: 'Photoreal warm apartment review moment. Jonas holds a phone with a clean family review packet viewer area left blank for overlay, Mara and laptop in background, calm collaborative decision feeling, premium SaaS and real-estate trailer look, intimate but polished, no readable text.',
  },
  {
    id: '08_agent_brief',
    title: 'Agent Brief',
    duration: 10,
    prompt: 'Photoreal cinematic close-up of Mara recording a calm professional voice message at an organized evening workspace. Floorplan, notebook, laptop and phone arranged neatly, confident prepared tone, subtle Vienna city lights, premium commercial cinematography, no readable document text.',
  },
  {
    id: '09_cta',
    title: 'CTA',
    duration: 7,
    prompt: 'Photoreal warm final workspace in the same Vienna apartment. Only a few organized property cards remain on the laptop, Mara and Jonas relaxed in the background, city lights outside, calm confidence and clarity, premium product trailer ending, no readable text or title rendered in-scene.',
  },
];

function scenePath(scene, suffix) {
  return path.join(OUT_DIR, `${scene.id}${suffix}`);
}

function argValue(name) {
  const idx = process.argv.indexOf(name);
  return idx >= 0 ? process.argv[idx + 1] : null;
}

function magicfitDuration(seconds) {
  const allowed = [4, 6, 8, 10, 12, 15];
  return allowed.reduce((best, candidate) => {
    const currentGap = Math.abs(candidate - seconds);
    const bestGap = Math.abs(best - seconds);
    if (currentGap < bestGap) return candidate;
    if (currentGap === bestGap && candidate > best) return candidate;
    return best;
  }, allowed[0]);
}

async function login(page) {
  await page.goto('https://magicfit.pushowl.com/home', { waitUntil: 'domcontentloaded', timeout: 120000 });
  await page.waitForTimeout(4000);
  const body = await page.locator('body').innerText({ timeout: 10000 }).catch(() => '');
  if (!/login|sign in|email|password/i.test(body)) return;
  const email = process.env.PROPERTYQUARRY_MAGICFIT_EMAIL || process.env.MAGICFIT_EMAIL || '';
  const password = process.env.PROPERTYQUARRY_MAGICFIT_PASSWORD || process.env.MAGICFIT_PASSWORD || '';
  if (!email || !password) throw new Error('MagicFit credentials are missing from env.');
  const emailField = page.locator('input[type=email], input[name*=email i], input[placeholder*=email i]').first();
  if (await emailField.count()) await emailField.fill(email);
  const passwordField = page.locator('input[type=password]').first();
  if (await passwordField.count()) await passwordField.fill(password);
  const submit = page.getByRole('button', { name: /sign in|login|continue|submit/i }).first();
  if (await submit.count()) await submit.click();
  await page.waitForLoadState('domcontentloaded').catch(() => {});
  await page.waitForTimeout(8000);
}

async function selectPill(page, currentText, optionText) {
  const pill = page.getByRole('button', { name: currentText }).last();
  await pill.click({ timeout: 10000 });
  await page.waitForTimeout(500);
  const option = page.getByText(optionText, { exact: true }).last();
  await option.click({ timeout: 10000 });
  await page.waitForTimeout(500);
}

async function fillPrompt(page, prompt) {
  const box = page.locator('[contenteditable="true"][role="textbox"]').first();
  await box.waitFor({ timeout: 10000 });
  await box.evaluate((node) => {
    node.scrollIntoView({ block: 'center', inline: 'nearest' });
    node.focus();
    node.textContent = '';
  });
  await page.waitForTimeout(200);
  await box.click({ timeout: 10000, force: true }).catch(() => {});
  await page.keyboard.type(prompt, { delay: 1 });
  await page.waitForTimeout(800);
}

function download(url, file) {
  return new Promise((resolve, reject) => {
    const out = fs.createWriteStream(file);
    https.get(url, (res) => {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        out.close();
        fs.unlinkSync(file);
        return download(res.headers.location, file).then(resolve, reject);
      }
      if (res.statusCode !== 200) {
        out.close();
        fs.unlinkSync(file);
        reject(new Error(`download ${url} failed with ${res.statusCode}`));
        return;
      }
      res.pipe(out);
      out.on('finish', () => out.close(resolve));
    }).on('error', (error) => {
      out.close();
      try { fs.unlinkSync(file); } catch {}
      reject(error);
    });
  });
}

function collectCdnVideoUrlsFromText(text) {
  return [...new Set((text.match(/https:\/\/(?:cdn\.pushowl\.com|media\.powlcdn\.com)\/magicfit\/[^"'\s<>]+?\.(?:mp4|webm)(?:[^"'\s<>]*)?/g) || [])
    .map((url) => url.replace(/\\u0026/g, '&').replace(/[),\]]+$/, '')))];
}

function magicfitUrlTimestamp(url) {
  const match = url.match(/\/magicfit\/(\d+)-/);
  return match ? Number(match[1]) : 0;
}

async function collectVisibleMagicFitVideoUrls(page) {
  const urls = new Set();
  const html = await page.content().catch(() => '');
  for (const found of collectCdnVideoUrlsFromText(html)) urls.add(found);
  const videos = await page.locator('video').evaluateAll((nodes) => nodes.map((v) => v.currentSrc || v.src).filter(Boolean)).catch(() => []);
  for (const found of videos) {
    if (/(?:cdn\.pushowl\.com|media\.powlcdn\.com)\/magicfit\/.*\.(mp4|webm)/.test(found)) urls.add(found);
  }
  return urls;
}

function chooseNewestVideoUrl(urls, baseline, submittedAtMs) {
  const candidates = [...urls]
    .filter((url) => /\.(mp4|webm)(?:$|[?#/])/i.test(url))
    .filter((url) => !/\/ik-thumbnail\./i.test(url))
    .filter((url) => !baseline.has(url))
    .map((url) => ({ url, timestamp: magicfitUrlTimestamp(url) }))
    .filter((item) => item.timestamp === 0 || item.timestamp >= submittedAtMs - 120000)
    .sort((left, right) => right.timestamp - left.timestamp);
  return candidates[0]?.url || null;
}

async function renderScene(page, scene) {
  fs.mkdirSync(OUT_DIR, { recursive: true });
  const mp4Path = scenePath(scene, '.mp4');
  const sidecarPath = scenePath(scene, '.magicfit.json');
  if (fs.existsSync(mp4Path) && fs.existsSync(sidecarPath) && !process.argv.includes('--force')) {
    console.log(`skip ${scene.id}`);
    return;
  }

  const events = [];
  const seenVideoUrls = new Set();
  const responseHandler = async (response) => {
    const url = response.url();
    if (!url.includes('magicfit') && !url.includes('pushowl')) return;
    const item = { method: response.request().method(), status: response.status(), url, contentType: response.headers()['content-type'] || '' };
    events.push(item);
    if (/(?:cdn\.pushowl\.com|media\.powlcdn\.com)\/magicfit\/.*\.(mp4|webm)(?:$|\?)/.test(url)) seenVideoUrls.add(url);
    if (/json|script|text/.test(item.contentType)) {
      const text = await response.text().catch(() => '');
      for (const found of collectCdnVideoUrlsFromText(text)) seenVideoUrls.add(found);
      if (item.method !== 'GET' || /api|generation|render|session|job/i.test(url)) {
        item.bodyPreview = text.slice(0, 1200);
      }
    }
  };
  page.on('response', responseHandler);

  try {
    await page.goto('https://magicfit.pushowl.com/agents/generate?mode=video', { waitUntil: 'domcontentloaded', timeout: 120000 });
    await page.waitForTimeout(5000);
    const baselineVideoUrls = await collectVisibleMagicFitVideoUrls(page);
    await selectPill(page, '9:16', 'Landscape (16:9)').catch(async () => {
      await page.locator('select').nth(0).selectOption('16:9').catch(() => {});
    });
    const providerDuration = magicfitDuration(scene.duration);
    await selectPill(page, '4s', `${providerDuration}s`).catch(async () => {
      await page.locator('select').nth(1).selectOption(String(providerDuration)).catch(() => {});
    });
    const fullPrompt = `${scene.prompt} Global constraints: ${GLOBAL_NEGATIVE}.`;
    await fillPrompt(page, fullPrompt);
    await page.screenshot({ path: scenePath(scene, '.before-submit.png'), fullPage: true });
    const submit = page.locator('form button').last();
    const submittedAtMs = Date.now();
    await submit.click({ timeout: 30000 });
    console.log(`submitted ${scene.id}`);
    fs.writeFileSync(scenePath(scene, '.submitted.events.json'), JSON.stringify(events.slice(-120), null, 2));
    await page.waitForTimeout(3000);

    const timeoutMinutes = Number(argValue('--timeout-minutes') || '18');
    const deadline = Date.now() + timeoutMinutes * 60 * 1000;
    let videoUrl = null;
    let pollCount = 0;
    while (Date.now() < deadline && !videoUrl) {
      await page.waitForTimeout(10000);
      pollCount += 1;
      if (pollCount % 3 === 0) {
        await page.screenshot({ path: scenePath(scene, `.poll-${String(pollCount).padStart(2, '0')}.png`), fullPage: true }).catch(() => {});
      }
      const html = await page.content().catch(() => '');
      for (const found of collectCdnVideoUrlsFromText(html)) seenVideoUrls.add(found);
      const videos = await page.locator('video').evaluateAll((nodes) => nodes.map((v) => v.currentSrc || v.src).filter(Boolean)).catch(() => []);
      for (const found of videos) {
        if (/(?:cdn\.pushowl\.com|media\.powlcdn\.com)\/magicfit\/.*\.(mp4|webm)/.test(found)) seenVideoUrls.add(found);
      }
      videoUrl = chooseNewestVideoUrl(seenVideoUrls, baselineVideoUrls, submittedAtMs);
      if (!videoUrl) console.log(`poll ${scene.id}: waiting`);
    }
    await page.screenshot({ path: scenePath(scene, '.after-render.png'), fullPage: true }).catch(() => {});
    if (!videoUrl) {
      fs.writeFileSync(scenePath(scene, '.failed.json'), JSON.stringify({ scene, events: events.slice(-200), url: page.url() }, null, 2));
      throw new Error(`No MagicFit video URL found for ${scene.id}`);
    }
    await download(videoUrl, mp4Path);
    const sidecar = {
      provider: 'MagicFit',
      rendered_by: 'PropertyQuarry MagicFit browser automation',
      scene_id: scene.id,
      title: scene.title,
      duration_seconds_requested: scene.duration,
      duration_seconds_magicfit: providerDuration,
      aspect_ratio: '16:9',
      resolution: '720p',
      model: 'Seedance 2.0 Fast',
      video_output_url: videoUrl,
      source_prompt: fullPrompt,
      generated_at_utc: new Date().toISOString(),
      page_url_after_submit: page.url(),
      event_tail: events.slice(-80),
    };
    fs.writeFileSync(sidecarPath, JSON.stringify(sidecar, null, 2));
    console.log(`rendered ${scene.id} -> ${mp4Path}`);
  } finally {
    page.off('response', responseHandler);
  }
}

async function main() {
  const onlyIdx = process.argv.indexOf('--only');
  const only = onlyIdx >= 0 ? new Set(process.argv[onlyIdx + 1].split(',').map((s) => s.trim())) : null;
  const scenes = only ? SCENES.filter((scene) => only.has(scene.id) || only.has(scene.id.slice(0, 2))) : SCENES;
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1100 }, acceptDownloads: true });
  const page = await context.newPage();
  await login(page);
  for (const scene of scenes) {
    await renderScene(page, scene);
  }
  await browser.close();
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
