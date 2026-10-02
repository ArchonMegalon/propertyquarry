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
        raise RuntimeError("template_worker_input_missing")
    loaded = json.loads(raw)
    if not isinstance(loaded, dict):
        raise RuntimeError("template_worker_input_invalid")
    return loaded


def _load_spec(packet: dict[str, object]) -> dict[str, object]:
    embedded = packet.get("workflow_spec_json")
    if isinstance(embedded, dict):
        return dict(embedded)
    path = str(packet.get("workflow_spec_path") or packet.get("template_path") or "").strip()
    if not path:
        raise RuntimeError("template_worker_spec_missing")
    loaded = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise RuntimeError("template_worker_spec_invalid")
    return loaded


def _slugify(value: object) -> str:
    lowered = "".join(char.lower() if char.isalnum() else "-" for char in str(value or "").strip())
    lowered = "-".join(part for part in lowered.split("-") if part)
    return lowered or f"template-{uuid.uuid4().hex[:12]}"


def _template_node_script() -> str:
    return r"""
const { chromium } = require('playwright');
const fs = require('fs');

async function main() {
  const packet = JSON.parse(fs.readFileSync(process.env.TEMPLATE_PACKET_PATH, 'utf8'));
  const spec = JSON.parse(fs.readFileSync(process.env.TEMPLATE_SPEC_PATH, 'utf8'));
  const screenshotPath = process.env.TEMPLATE_SCREENSHOT_PATH;
  const resultPath = String(process.env.TEMPLATE_RESULT_PATH || '').trim();
  const traceDir = String(process.env.TEMPLATE_TRACE_DIR || '').trim();
  const browserHeadless = String(process.env.TEMPLATE_BROWSER_HEADLESS || 'true').trim().toLowerCase() !== 'false';
  const initialRuntimeInputs = Object.assign({}, packet.runtime_inputs_json || {});
  const rawProxyServer = String(initialRuntimeInputs.browser_proxy_server || packet.browser_proxy_server || '').trim();
  const proxyServer = ['direct://', 'direct', 'none', 'off', 'disabled'].includes(rawProxyServer.toLowerCase())
    ? ''
    : rawProxyServer;
  const proxyUsername = String(initialRuntimeInputs.browser_proxy_username || packet.browser_proxy_username || '').trim();
  const proxyPassword = String(initialRuntimeInputs.browser_proxy_password || packet.browser_proxy_password || '').trim();
  const proxyBypass = String(initialRuntimeInputs.browser_proxy_bypass || packet.browser_proxy_bypass || '').trim();
  const launchOptions = {
    headless: browserHeadless,
    args: ['--disable-blink-features=AutomationControlled', '--no-sandbox', '--disable-dev-shm-usage'],
    ignoreDefaultArgs: ['--enable-automation'],
  };
  if (proxyServer) {
    launchOptions.proxy = { server: proxyServer };
    if (proxyUsername) launchOptions.proxy.username = proxyUsername;
    if (proxyPassword) launchOptions.proxy.password = proxyPassword;
    if (proxyBypass) launchOptions.proxy.bypass = proxyBypass;
  }
  const browser = await chromium.launch(launchOptions);
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1200 },
    locale: 'en-US',
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
  });
  await context.addInitScript(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
    Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
    Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
    Object.defineProperty(navigator, 'plugins', {
      get: () => [
        { name: 'Chrome PDF Plugin' },
        { name: 'Chrome PDF Viewer' },
        { name: 'Native Client' },
      ],
    });
    const originalQuery = window.navigator.permissions && window.navigator.permissions.query
      ? window.navigator.permissions.query.bind(window.navigator.permissions)
      : null;
    if (originalQuery) {
      window.navigator.permissions.query = (parameters) => (
        parameters && parameters.name === 'notifications'
          ? Promise.resolve({ state: Notification.permission })
          : originalQuery(parameters)
      );
    }
    if (!window.chrome) {
      Object.defineProperty(window, 'chrome', { value: { runtime: {} } });
    } else if (!window.chrome.runtime) {
      window.chrome.runtime = {};
    }
  });
  let page = await context.newPage();
  const runtimeInputs = Object.assign({}, initialRuntimeInputs);
  const authFlow = String((((spec || {}).meta || {}).auth_flow) || '').trim().toLowerCase();
  const runtimeTargetInputName = String((((spec || {}).meta || {}).runtime_input_name) || '').trim();
  const blockedUrlMarkers = Array.isArray((((spec || {}).meta || {}).blocked_url_markers))
    ? (((spec || {}).meta || {}).blocked_url_markers || []).map(value => String(value || '').trim()).filter(Boolean)
    : [];
  const googleAuthSequenceIds = new Set([
    'wait_google_email',
    'google_email',
    'google_email_next',
    'wait_google_password',
    'google_password',
    'google_password_next',
  ]);
  let googleAuthCompleted = false;
  let runtimeTargetVisited = false;
  const result = {
    url: '',
    title: '',
    bodyText: '',
    pageHtml: '',
    labels: [],
    buttons: [],
    links: [],
    extracts: {},
    outputText: '',
    warnings: [],
    errors: [],
    template_key: String(packet.template_key || ''),
    workflow_kind: String((((spec || {}).meta || {}).workflow_kind) || ''),
  };
  let authRequestFailure = '';
  let traceIndex = 0;

  if (blockedUrlMarkers.length) {
    await context.route('**/*', (route) => {
      try {
        const url = String(route.request().url() || '');
        if (blockedUrlMarkers.some(marker => marker && url.includes(marker))) {
          return route.abort();
        }
      } catch (_) {}
      return route.continue();
    });
  }

  function emitResultSummary(payload) {
    const summary = {
      result_path: resultPath || '',
      url: String((payload || {}).url || ''),
      title: String((payload || {}).title || ''),
      warnings: Array.isArray((payload || {}).warnings) ? payload.warnings.slice(0, 12) : [],
      errors: Array.isArray((payload || {}).errors) ? payload.errors.slice(0, 6) : [],
    };
    console.log(JSON.stringify(summary));
  }

  function persistResult(payload) {
    if (!resultPath) return;
    fs.writeFileSync(resultPath, JSON.stringify(payload), 'utf8');
  }

  function noteAuthRequestFailure(detail) {
    const normalized = String(detail || '').trim();
    if (!normalized || authRequestFailure) return;
    authRequestFailure = normalized;
  }

  function normalizeText(value) {
    return String(value || '').trim().toLowerCase().replace(/\s+/g, ' ');
  }

  async function detectAuthUiFailure(config) {
    const markers = Array.isArray(config && config.auth_failure_text_markers)
      ? config.auth_failure_text_markers.map(value => normalizeText(value)).filter(Boolean)
      : [];
    if (!markers.length) return '';
    const selectors = Array.isArray(config && config.auth_failure_selectors)
      ? config.auth_failure_selectors.map(value => String(value || '').trim()).filter(Boolean)
      : [];
    if (!selectors.length) return '';
    for (const selector of selectors) {
      try {
        const locator = page.locator(selector);
        const count = await locator.count().catch(() => 0);
        if (!count) continue;
        const texts = await locator.evaluateAll((nodes) => nodes.map((node) => {
          try {
            const style = window.getComputedStyle(node);
            if (style && (style.display === 'none' || style.visibility === 'hidden')) {
              return '';
            }
          } catch (_) {}
          try {
            if (typeof node.getBoundingClientRect === 'function') {
              const rect = node.getBoundingClientRect();
              if (rect && rect.width === 0 && rect.height === 0) {
                return '';
              }
            }
          } catch (_) {}
          return String(node.innerText || node.textContent || '').trim();
        }).filter(Boolean).slice(0, 16)).catch(() => []);
        for (const text of texts) {
          const normalized = normalizeText(text);
          if (!normalized) continue;
          for (const marker of markers) {
            if (normalized.includes(marker)) {
              const code = String((config && config.auth_failure_code) || 'invalid_credentials').trim() || 'invalid_credentials';
              return `${code}:${String(text || '').slice(0, 240)}`;
            }
          }
        }
      } catch (_) {}
    }
    return '';
  }

  async function trace(tag) {
    const safeTag = String(tag || 'trace').replace(/[^a-z0-9._-]+/gi, '-').replace(/-+/g, '-').replace(/^-|-$/g, '') || 'trace';
    if (traceDir) {
      const tracePath = `${traceDir}/${String(++traceIndex).padStart(2, '0')}-${safeTag}.png`;
      await page.screenshot({ path: tracePath, fullPage: true }).catch(() => {});
    }
    const url = String(page.url() || '');
    const title = String((await page.title().catch(() => '')) || '');
    console.log(JSON.stringify({ trace: safeTag, url, title }));
  }

  for (const [key, value] of Object.entries(packet)) {
    if (!(key in runtimeInputs)) runtimeInputs[key] = value;
  }

  function resolveValue(config) {
    if (!config || typeof config !== 'object') return '';
    const inputKey = String(config.value_from_input || '').trim();
    if (inputKey && runtimeInputs[inputKey] !== undefined && runtimeInputs[inputKey] !== null && String(runtimeInputs[inputKey]).trim()) {
      return String(runtimeInputs[inputKey]);
    }
    const secretKey = String(config.value_from_secret || '').trim();
    if (secretKey && runtimeInputs[secretKey] !== undefined && runtimeInputs[secretKey] !== null && String(runtimeInputs[secretKey]).trim()) {
      return String(runtimeInputs[secretKey]);
    }
    const explicitValue = String(config.value || '').trim();
    if (explicitValue) return explicitValue;
    return '';
  }

  function isOptional(config) {
    return Boolean(config && typeof config === 'object' && config.optional);
  }

  function isGoogleAccountsUrl(value) {
    return String(value || '').includes('accounts.google.com');
  }

  async function adoptContextPage(matchFn) {
    for (const candidate of context.pages()) {
      try {
        const url = String(candidate.url() || '');
        if (!matchFn(url)) continue;
        page = candidate;
        await page.bringToFront().catch(() => {});
        return true;
      } catch (_) {}
    }
    return false;
  }

  async function maybeAdoptGooglePage(timeoutMs) {
    const deadline = Date.now() + Math.max(1000, Number(timeoutMs || 10000));
    while (Date.now() < deadline) {
      if (isGoogleAccountsUrl(page.url())) return true;
      if (await adoptContextPage(url => isGoogleAccountsUrl(url))) return true;
      await page.waitForTimeout(500);
    }
    return isGoogleAccountsUrl(page.url());
  }

  async function maybeReturnToAppPage(timeoutMs) {
    const deadline = Date.now() + Math.max(1000, Number(timeoutMs || 10000));
    while (Date.now() < deadline) {
      if (!isGoogleAccountsUrl(page.url())) return true;
      if (await adoptContextPage(url => Boolean(url) && !isGoogleAccountsUrl(url) && !String(url).startsWith('about:blank'))) {
        return true;
      }
      await page.waitForTimeout(500);
    }
    return !isGoogleAccountsUrl(page.url());
  }

  async function awaitLocator(selector, config) {
    const normalized = String(selector || '').trim();
    if (!normalized) return null;
    const optional = isOptional(config);
    const waitMs = Math.max(250, Number((config && config.wait_timeout_ms) || (optional ? 1200 : 12000)));
    try {
      const locator = page.locator(normalized).first();
      const initialCount = await locator.count().catch(() => 0);
      if (initialCount) return locator;
      await locator.waitFor({ state: 'attached', timeout: waitMs }).catch(() => {});
      const finalCount = await locator.count().catch(() => 0);
      if (!finalCount) return null;
      return locator;
    } catch (_) {
      return null;
    }
  }

  async function maybeClick(selector, label, config = {}) {
    const normalized = String(selector || '').trim();
    if (!normalized) return false;
    try {
      const locator = await awaitLocator(normalized, config);
      if (!locator) return false;
      const waitMs = Math.max(250, Number((config && config.wait_timeout_ms) || (isOptional(config) ? 1200 : 12000)));
      const postClickWaitMs = Math.max(200, Number((config && config.post_click_wait_ms) || 1500));
      await locator.waitFor({ state: 'visible', timeout: waitMs }).catch(() => {});
      if (Boolean(config && config.react_click)) {
        const reacted = await locator.evaluate((node) => {
          const keys = Object.keys(node || {}).filter((key) => String(key || '').startsWith('__reactProps'));
          for (const key of keys) {
            try {
              const props = node[key];
              if (!props || typeof props.onClick !== 'function') continue;
              props.onClick({
                preventDefault() {},
                stopPropagation() {},
                currentTarget: node,
                target: node,
                nativeEvent: new MouseEvent('click', { bubbles: true, cancelable: true, view: window }),
              });
              return true;
            } catch (_) {}
          }
          return false;
        }).catch(() => false);
        if (reacted) {
          await page.waitForTimeout(postClickWaitMs);
          await trace(`react-click-${label || 'click'}`);
          return true;
        }
      }
      if (Boolean(config && config.dom_click)) {
        await locator.evaluate((node) => {
          if (node && typeof node.click === 'function') {
            node.click();
            return;
          }
          node.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
        });
      } else {
        await locator.click({ force: true, timeout: 10000 }).catch(async () => {
          await locator.click({ timeout: 10000 });
        });
      }
      await page.waitForTimeout(postClickWaitMs);
      return true;
    } catch (error) {
      result.warnings.push(`${label || 'click'}:${String(error && error.message ? error.message : error)}`);
      return false;
    }
  }

  async function maybeFill(selector, value, label, config = {}) {
    const normalized = String(selector || '').trim();
    const text = String(value || '');
    if (!normalized || !text.trim()) return false;
    try {
      const locator = await awaitLocator(normalized, config);
      if (!locator) return false;
      const waitMs = Math.max(250, Number((config && config.wait_timeout_ms) || (isOptional(config) ? 1200 : 15000)));
      await locator.waitFor({ state: 'visible', timeout: waitMs }).catch(() => {});
      await locator.fill(text, { timeout: 15000 }).catch(async () => {
        await locator.click({ force: true, timeout: 10000 }).catch(() => {});
        await locator.press('Control+A').catch(() => {});
        await locator.type(text, { delay: 10 });
      });
      await page.waitForTimeout(400);
      return true;
    } catch (error) {
      result.warnings.push(`${label || 'fill'}:${String(error && error.message ? error.message : error)}`);
      return false;
    }
  }

  async function maybePressEnter(selector, label, config = {}) {
    const normalized = String(selector || '').trim();
    if (!normalized) return false;
    try {
      const locator = await awaitLocator(normalized, config);
      if (!locator) return false;
      const waitMs = Math.max(250, Number((config && config.wait_timeout_ms) || (isOptional(config) ? 1200 : 12000)));
      await locator.waitFor({ state: 'visible', timeout: waitMs }).catch(() => {});
      await locator.focus({ timeout: 10000 }).catch(async () => {
        await locator.click({ force: true, timeout: 10000 });
      });
      await locator.press('Enter', { timeout: 10000 });
      await page.waitForTimeout(1800);
      return true;
    } catch (error) {
      result.warnings.push(`${label || 'press_enter'}:${String(error && error.message ? error.message : error)}`);
      return false;
    }
  }

  async function maybeRequestSubmit(selector, label, config = {}) {
    const normalized = String(selector || '').trim();
    if (!normalized) return false;
    try {
      const locator = await awaitLocator(normalized, config);
      if (!locator) return false;
      await locator.evaluate((form) => {
        if (form && typeof form.requestSubmit === 'function') {
          form.requestSubmit();
          return;
        }
        if (form && typeof form.dispatchEvent === 'function') {
          form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
        }
      });
      await page.waitForTimeout(Math.max(500, Number((config && config.post_click_wait_ms) || 1500)));
      await trace(`request-submit-${label || 'submit'}`);
      return true;
    } catch (error) {
      result.warnings.push(`${label || 'request_submit'}:${String(error && error.message ? error.message : error)}`);
      return false;
    }
  }

  async function waitForCookie(cookieName, timeoutMs) {
    const normalized = String(cookieName || '').trim();
    if (!normalized) return true;
    const deadline = Date.now() + Math.max(500, Number(timeoutMs || 10000));
    while (Date.now() < deadline) {
      try {
        const cookies = await context.cookies();
        if (cookies.some(cookie => String(cookie.name || '').trim() === normalized)) {
          return true;
        }
      } catch (_) {}
      await page.waitForTimeout(500);
    }
    return false;
  }

  async function submitLoginForm(config, label) {
    const passwordSelector = String(config.password_selector || "input[type=password], input[name=password], input[name=Passwd], input[autocomplete='current-password']").trim();
    const formSelector = String(config.form_selector || "form[name='login'], .ant-modal form, .ant-modal-root form, form").trim();
    const startUrl = String(page.url() || '');
    const authAdvanceTimeoutMs = Math.max(1500, Number(config.auth_advance_timeout_ms || 5000));
    const preSubmitCookieName = String(config.pre_submit_cookie_name || '').trim();
    const preSubmitCookieTimeoutMs = Math.max(1000, Number(config.pre_submit_cookie_timeout_ms || 15000));
    const preSubmitWaitMs = Math.max(0, Number(config.pre_submit_wait_ms || 0));
    const submitRetryCount = Math.max(0, Number(config.submit_retry_count || 0));
    const submitRetryBackoffMs = Math.max(500, Number(config.submit_retry_backoff_ms || 5000));

    function throwIfAuthRequestFailed() {
      if (!authRequestFailure) return;
      throw new Error(`${label}:auth_request_failed:${authRequestFailure}`);
    }

    async function throwIfAuthUiFailed() {
      const detected = await detectAuthUiFailure(config);
      if (!detected) return;
      throw new Error(`${label}:${detected}`);
    }

    async function authAdvanced() {
      const deadline = Date.now() + authAdvanceTimeoutMs;
      while (Date.now() < deadline) {
        throwIfAuthRequestFailed();
        await throwIfAuthUiFailed();
        const currentUrl = String(page.url() || '');
        if (currentUrl && currentUrl !== startUrl) return true;
        if (passwordSelector) {
          try {
            const locator = page.locator(passwordSelector).first();
            const count = await locator.count().catch(() => 0);
            if (!count) return true;
            const visible = await locator.isVisible().catch(() => false);
            if (!visible) return true;
          } catch (_) {}
        }
        await page.waitForTimeout(500);
      }
      throwIfAuthRequestFailed();
      await throwIfAuthUiFailed();
      return false;
    }

    const attemptSelectors = [
      String(config.selector || '').trim(),
      "form button[type=submit]",
      "form input[type=submit]",
      "button:has-text('Sign In')",
      "button:has-text('Log In')",
      "button:has-text('Login')",
      "button:has-text('Continue')",
      "button:has-text('Submit')",
      "button:has-text('Next')",
    ].filter(Boolean);

    for (let attempt = 0; attempt <= submitRetryCount; attempt += 1) {
      throwIfAuthRequestFailed();
      if (preSubmitCookieName) {
        const cookieReady = await waitForCookie(preSubmitCookieName, preSubmitCookieTimeoutMs);
        if (!cookieReady) {
          result.warnings.push(`${label}:cookie_wait_timeout:${preSubmitCookieName}`);
        }
      }
      if (preSubmitWaitMs > 0) {
        await page.waitForTimeout(preSubmitWaitMs);
      }
      if (formSelector && await maybeRequestSubmit(formSelector, `${label}:request_submit:${attempt + 1}`, config)) {
        if (await authAdvanced()) {
          return true;
        }
        throwIfAuthRequestFailed();
      }
      if (await maybePressEnter(passwordSelector, `${label}:enter:${attempt + 1}`, config)) {
        if (await authAdvanced()) {
          return true;
        }
        throwIfAuthRequestFailed();
      }
      for (const selector of attemptSelectors) {
        if (!(await maybeClick(selector, `${label}:click:${attempt + 1}`, config))) {
          continue;
        }
        if (await authAdvanced()) {
          return true;
        }
        throwIfAuthRequestFailed();
      }
      if (attempt < submitRetryCount) {
        await page.waitForTimeout(submitRetryBackoffMs);
      }
    }
    return false;
  }

  async function waitForUrlChange(previousUrl, timeoutMs) {
    const baseline = String(previousUrl || '').trim();
    if (!baseline) {
      await page.waitForTimeout(1500);
      return true;
    }
    try {
      await page.waitForFunction(
        value => String(window.location.href || '') !== value,
        baseline,
        { timeout: timeoutMs },
      );
      return true;
    } catch (_) {
      return false;
    }
  }

  async function completeGoogleAuth(config, label) {
    const emailValue = resolveValue({
      value_from_secret: String(config.email_secret || 'browseract_username'),
      value_from_input: String(config.email_input || ''),
    });
    const passwordValue = resolveValue({
      value_from_secret: String(config.password_secret || 'browseract_password'),
      value_from_input: String(config.password_input || ''),
    });
    const timeoutMs = Math.max(10000, Number(config.timeout_ms || 120000));
    const startUrl = String(page.url() || '');
    const onGoogle = () => String(page.url() || '').includes('accounts.google.com');

    if (!onGoogle()) {
      await page.waitForTimeout(1500);
      if (!(await maybeAdoptGooglePage(12000)) && !onGoogle()) {
        if (isOptional(config)) return false;
        throw new Error(`${label}:not_on_google`);
      }
    }

    const chooseAccountSelectors = [
      "[data-email]",
      "div[role='link'][data-identifier]",
      "li [data-email]",
      "div[data-identifier]",
    ];
    for (const selector of chooseAccountSelectors) {
      try {
        const locator = page.locator(selector);
        const count = await locator.count();
        if (!count) continue;
        if (emailValue) {
          for (let index = 0; index < count; index += 1) {
            const candidate = locator.nth(index);
            const text = String((await candidate.innerText().catch(() => '')) || '').trim().toLowerCase();
            const dataEmail = String((await candidate.getAttribute('data-email').catch(() => '')) || '').trim().toLowerCase();
            const dataIdentifier = String((await candidate.getAttribute('data-identifier').catch(() => '')) || '').trim().toLowerCase();
            if ([text, dataEmail, dataIdentifier].some(value => value && value.includes(emailValue.toLowerCase()))) {
              await candidate.click({ force: true, timeout: 10000 });
              await page.waitForTimeout(1800);
              break;
            }
          }
        } else {
          await locator.first().click({ force: true, timeout: 10000 });
          await page.waitForTimeout(1800);
        }
      } catch (_) {}
      if (!onGoogle()) return true;
    }

    if (emailValue) {
      await maybeFill("input[type=email], input[name=identifier], input[autocomplete='username']", emailValue, `${label}:email`, config);
      await maybeClick("#identifierNext button, button:has-text('Next')", `${label}:email_next`, config);
      await page.waitForTimeout(1800);
      await trace(`${label}-after-email-next`);
    }

    if (!onGoogle()) return true;

    if (passwordValue) {
      await maybeFill("input[type=password], input[name=Passwd], input[autocomplete='current-password']", passwordValue, `${label}:password`, config);
      await maybeClick("#passwordNext button, button:has-text('Next')", `${label}:password_next`, config);
      await page.waitForTimeout(2200);
      await trace(`${label}-after-password-next`);
    }

    const consentSelectors = [
      "button:has-text('Continue')",
      "button:has-text('Allow')",
      "button:has-text('Accept')",
      "button:has-text('Yes')",
      "button:has-text('I agree')",
      "[role='button']:has-text('Continue')",
      "[role='button']:has-text('Allow')",
    ];
    for (const selector of consentSelectors) {
      if (!onGoogle()) break;
      await maybeClick(selector, `${label}:consent`, { optional: true, wait_timeout_ms: 1200 });
      await page.waitForTimeout(1500);
    }

    if (!onGoogle()) return true;
    const changed = await waitForUrlChange(startUrl, timeoutMs);
    if (changed && !onGoogle()) return true;
    if (await maybeReturnToAppPage(timeoutMs)) return true;
    if (isOptional(config)) return false;
    throw new Error(`${label}:google_auth_incomplete`);
  }

  async function extractText(selector) {
    const normalized = String(selector || '').trim() || 'body';
    try {
      const locator = page.locator(normalized).first();
      if (await locator.count()) {
        return String((await locator.innerText().catch(() => '')) || '');
      }
    } catch (_) {}
    try {
      return String((await page.locator('body').innerText().catch(() => '')) || '');
    } catch (_) {
      return '';
    }
  }

  try {
    page.on('requestfailed', (request) => {
      try {
        const url = String(request.url() || '');
        const errorText = String((request.failure() && request.failure().errorText) || '');
        if (url.includes('api.1min.ai/auth/')) {
          const detail = `requestfailed:${url}:${errorText}`;
          noteAuthRequestFailure(detail);
          result.warnings.push(detail);
        }
      } catch (_) {}
    });
    page.on('console', (message) => {
      try {
        const text = String(message && message.text ? message.text() : '');
        if (text.includes('api.1min.ai/auth/login') || text.includes('CORS policy')) {
          const detail = `console:${text}`;
          if (text.includes('api.1min.ai/auth/login')) {
            noteAuthRequestFailure(detail);
          }
          result.warnings.push(detail);
        }
      } catch (_) {}
    });
    for (const node of (spec.nodes || [])) {
      if (!node || typeof node !== 'object') continue;
      const nodeType = String(node.type || '').trim().toLowerCase();
      const nodeId = String(node.id || '').trim();
      const label = String(node.label || node.id || nodeType || 'node');
      const config = (node.config && typeof node.config === 'object') ? node.config : {};
      if (authFlow === 'google_oauth' && googleAuthSequenceIds.has(nodeId)) {
        if (!googleAuthCompleted) {
          await completeGoogleAuth(config, 'google_auth');
          googleAuthCompleted = true;
        }
        continue;
      }
      if (nodeId === 'wait_content' && runtimeTargetInputName && !runtimeTargetVisited) {
        const runtimeTargetUrl = String(runtimeInputs[runtimeTargetInputName] || '').trim();
        if (runtimeTargetUrl) {
          const gotoError = await page.goto(runtimeTargetUrl, { waitUntil: 'domcontentloaded', timeout: 120000 }).then(
            () => '',
            (error) => String(error && error.message ? error.message : error),
          );
          if (gotoError) {
            if (isOptional(config)) {
              result.warnings.push(`runtime_target:${gotoError}`);
            } else {
              throw new Error(`runtime_target:${gotoError}`);
            }
          } else {
            runtimeTargetVisited = true;
            await page.waitForLoadState('networkidle', { timeout: 12000 }).catch(() => {});
            await page.waitForTimeout(2000);
          }
        }
      }
      if (nodeType === 'visit_page') {
        const targetUrl = resolveValue(config) || String(config.url || '').trim();
        if (!targetUrl) {
          if (config.skip_when_empty) continue;
          if (isOptional(config)) {
            result.warnings.push(`${label}:missing_url`);
            continue;
          }
          throw new Error(`${label}:missing_url`);
        }
        const gotoError = await page.goto(targetUrl, { waitUntil: 'domcontentloaded', timeout: 120000 }).then(
          () => '',
          (error) => String(error && error.message ? error.message : error),
        );
        if (gotoError) {
          if (isOptional(config)) {
            result.warnings.push(`${label}:${gotoError}`);
            continue;
          }
          throw new Error(`${label}:${gotoError}`);
        }
        await page.waitForLoadState('networkidle', { timeout: Number(config.networkidle_timeout_ms || 12000) }).catch(() => {});
        await page.waitForTimeout(Math.max(500, Number(config.post_load_wait_ms || 2500)));
        await trace(`visit-${nodeId || label}`);
        continue;
      }
      if (nodeType === 'input_text') {
        const value = resolveValue(config);
        if (!value) {
          if (isOptional(config)) {
            result.warnings.push(`${label}:missing_value`);
            continue;
          }
          throw new Error(`${label}:missing_value`);
        }
        const ok = await maybeFill(config.selector, value, label, config);
        if (!ok) {
          if (isOptional(config)) {
            result.warnings.push(`${label}:selector_not_found`);
            continue;
          }
          throw new Error(`${label}:selector_not_found`);
        }
        continue;
      }
      if (nodeType === 'click') {
        const clicked = await maybeClick(config.selector, label, config);
        if (!clicked) {
          if (isOptional(config)) {
            result.warnings.push(`${label}:selector_not_found`);
            continue;
          }
          throw new Error(`${label}:selector_not_found`);
        }
        continue;
      }
      if (nodeType === 'submit_login_form') {
        const submitted = await submitLoginForm(config, label);
        if (!submitted) {
          if (isOptional(config)) {
            result.warnings.push(`${label}:submit_not_found`);
            continue;
          }
          throw new Error(`${label}:submit_not_found`);
        }
        continue;
      }
      if (nodeType === 'google_auth') {
        try {
          await completeGoogleAuth(config, label);
        } catch (error) {
          const detail = `${label}:${String(error && error.message ? error.message : error)}`;
          if (isOptional(config)) {
            result.warnings.push(detail);
            continue;
          }
          throw new Error(detail);
        }
        continue;
      }
      if (nodeType === 'wait') {
        const selector = String(config.selector || '').trim();
        const timeoutMs = Math.max(1000, Number(config.timeout_ms || 45000));
        const state = String(config.state || 'visible').trim() || 'visible';
        if (!selector || selector === 'body') {
          await page.waitForTimeout(Math.min(timeoutMs, 2000));
          continue;
        }
        try {
          await page.locator(selector).first().waitFor({ state, timeout: timeoutMs });
        } catch (error) {
          const detail = `${label}:${String(error && error.message ? error.message : error)}`;
          if (isOptional(config)) {
            result.warnings.push(detail);
            continue;
          }
          throw new Error(detail);
        }
        continue;
      }
      if (nodeType === 'extract') {
        const fieldName = String(config.field_name || node.id || label).trim() || String(node.id || 'extract');
        const text = await extractText(config.selector);
        result.extracts[fieldName] = text.slice(0, 50000);
        if (!result.outputText && text.trim()) result.outputText = text.slice(0, 50000);
        continue;
      }
      if (nodeType === 'output') {
        const fieldName = String(config.field_name || '').trim();
        if (fieldName && result.extracts[fieldName]) {
          result.outputText = String(result.extracts[fieldName] || '').slice(0, 50000);
        }
      }
    }

    result.url = String(page.url() || '');
    result.title = String((await page.title().catch(() => '')) || '');
    if (!result.outputText) {
      result.outputText = (await extractText('body')).slice(0, 50000);
    }
    result.bodyText = String(result.outputText || '').slice(0, 50000);
    result.pageHtml = String((await page.content().catch(() => '')) || '');
    result.labels = await page.locator('label,h1,h2,h3,[role=heading]').evaluateAll(
      nodes => nodes.map(node => (node.innerText || node.textContent || '').trim()).filter(Boolean).slice(0, 120)
    ).catch(() => []);
    result.buttons = await page.locator('button,[role=button]').evaluateAll(
      nodes => nodes.map(node => (node.innerText || node.textContent || '').trim()).filter(Boolean).slice(0, 120)
    ).catch(() => []);
    result.links = await page.locator('a').evaluateAll(
      nodes => nodes.map(node => ({
        text: (node.innerText || node.textContent || '').trim(),
        href: String(node.href || '').trim(),
      })).filter(node => node.href).slice(0, 160)
    ).catch(() => []);
    await page.screenshot({ path: screenshotPath, fullPage: true }).catch((error) => {
      result.warnings.push(`screenshot:${String(error && error.message ? error.message : error)}`);
    });
    persistResult(result);
    emitResultSummary(result);
  } catch (error) {
    const errorText = String(error && error.stack ? error.stack : error);
    const normalizedError = normalizeText(errorText);
    if (normalizedError.includes('invalid_credentials')) {
      result.failure_code = 'invalid_credentials';
      result.ui_failure_code = 'invalid_credentials';
    } else if (normalizedError.includes('auth_request_failed')) {
      result.failure_code = 'auth_request_failed';
      result.ui_failure_code = 'auth_request_failed';
    } else if (
      normalizedError.includes('api.1min.ai/auth/login')
      && (
        normalizedError.includes('access to xmlhttprequest')
        || normalizedError.includes('access-control-allow-origin')
        || normalizedError.includes('cors policy')
        || normalizedError.includes('content security policy')
        || normalizedError.includes("connect-src 'none'")
        || normalizedError.includes('refused to connect')
      )
    ) {
      result.failure_code = 'auth_request_failed';
      result.ui_failure_code = 'auth_request_failed';
    }
    result.url = String(page.url() || '');
    result.title = String((await page.title().catch(() => '')) || '');
    if (!result.outputText) {
      result.outputText = (await extractText('body')).slice(0, 50000);
    }
    result.bodyText = String(result.outputText || '').slice(0, 50000);
    result.pageHtml = String((await page.content().catch(() => '')) || '');
    result.labels = await page.locator('label,h1,h2,h3,[role=heading]').evaluateAll(
      nodes => nodes.map(node => (node.innerText || node.textContent || '').trim()).filter(Boolean).slice(0, 120)
    ).catch(() => []);
    result.buttons = await page.locator('button,[role=button]').evaluateAll(
      nodes => nodes.map(node => (node.innerText || node.textContent || '').trim()).filter(Boolean).slice(0, 120)
    ).catch(() => []);
    result.links = await page.locator('a').evaluateAll(
      nodes => nodes.map(node => ({
        text: (node.innerText || node.textContent || '').trim(),
        href: String(node.href || '').trim(),
      })).filter(node => node.href).slice(0, 160)
    ).catch(() => []);
    await page.screenshot({ path: screenshotPath, fullPage: true }).catch((screenshotError) => {
      result.warnings.push(`screenshot:${String(screenshotError && screenshotError.message ? screenshotError.message : screenshotError)}`);
    });
    result.errors.push(errorText);
    persistResult(result);
    emitResultSummary(result);
    process.exit(1);
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.log(JSON.stringify({ url: '', title: '', bodyText: '', pageHtml: '', labels: [], buttons: [], links: [], extracts: {}, outputText: '', warnings: [], errors: [String(error && error.stack ? error.stack : error)] }));
  process.exit(1);
});
"""


def _run_browser(packet: dict[str, object], *, spec: dict[str, object], screenshot_path: Path, timeout_seconds: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="template-worker-", dir=str(SHARED_TEMP_ROOT)) as temp_dir_raw:
        temp_dir = Path(temp_dir_raw)
        packet_path = temp_dir / "packet.json"
        spec_path = temp_dir / "spec.json"
        script_path = temp_dir / "worker.js"
        result_path = temp_dir / "result.json"
        packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
        spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        script_path.write_text(_template_node_script(), encoding="utf-8")
        auth_flow = str(((spec.get("meta") or {}).get("auth_flow")) or "").strip().lower()
        google_headed = str(os.getenv("EA_UI_GOOGLE_HEADED") or "").strip().lower() in {"1", "true", "yes", "on"}
        run_args: list[str]
        env_pairs = [
            "-e",
            f"TEMPLATE_PACKET_PATH={packet_path}",
            "-e",
            f"TEMPLATE_SPEC_PATH={spec_path}",
            "-e",
            f"TEMPLATE_RESULT_PATH={temp_dir / 'result.json'}",
            "-e",
            f"TEMPLATE_SCREENSHOT_PATH={screenshot_path}",
            "-e",
            f"TEMPLATE_TRACE_DIR={screenshot_path.parent}",
        ]
        container_name = f"ea-ui-worker-{uuid.uuid4().hex[:12]}"
        if auth_flow == "google_oauth" and google_headed:
            env_pairs += ["-e", "TEMPLATE_BROWSER_HEADLESS=false"]
            run_args = ["bash", "-lc", f"xvfb-run -a node {script_path}"]
        else:
            run_args = ["node", str(script_path)]
        docker_network = str(os.getenv("EA_UI_SERVICE_DOCKER_NETWORK") or "").strip()
        command = [
            "docker",
            "run",
            "--name",
            container_name,
            "--rm",
            "-i",
            "-w",
            "/work",
            "-v",
            f"{temp_dir}:{temp_dir}",
            "-v",
            f"{screenshot_path.parent}:{screenshot_path.parent}",
            "-e",
            "NODE_PATH=/work/node_modules",
            *env_pairs,
        ]
        if docker_network:
            command.extend(["--network", docker_network])
        command.extend([PLAYWRIGHT_IMAGE, *run_args])
        try:
            completed = subprocess.run(
                command,
                text=True,
                capture_output=True,
                timeout=max(180, timeout_seconds + 60),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            subprocess.run(["docker", "kill", container_name], text=True, capture_output=True, check=False)
            raise RuntimeError(f"template_worker_timeout:{container_name}") from exc
        raw = str(completed.stdout or "").strip()
        loaded: dict[str, object] | None = None
        if result_path.exists():
            try:
                file_loaded = json.loads(result_path.read_text(encoding="utf-8"))
                if isinstance(file_loaded, dict):
                    loaded = file_loaded
            except Exception as exc:
                raise RuntimeError(f"template_worker_result_invalid:{type(exc).__name__}:{exc}") from exc
        if loaded is None and raw:
            for line in reversed([entry.strip() for entry in raw.splitlines() if entry.strip()]):
                try:
                    parsed = json.loads(line)
                except Exception:
                    continue
                if isinstance(parsed, dict):
                    loaded = parsed
                    break
        if loaded is None:
            raise RuntimeError(f"template_worker_empty_output:{str(completed.stderr or raw).strip()[:400]}")
        if completed.returncode != 0:
            raise RuntimeError(f"template_worker_failed:{str(loaded.get('errors') or completed.stderr or raw)[:500]}")
        if not isinstance(loaded, dict):
            raise RuntimeError("template_worker_output_invalid")
        return loaded


def _image_data_uri(path: Path) -> str:
    if not path.exists():
        return ""
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _auth_handoff_state(browser_output: dict[str, object]) -> dict[str, str]:
    url = str(browser_output.get("url") or "").strip().lower()
    title = str(browser_output.get("title") or "").strip().lower()
    body = str(browser_output.get("bodyText") or "").strip().lower()
    joined = "\n".join([url, title, body])
    if "accounts.google.com" in url and any(
        phrase in joined
        for phrase in (
            "2-step verification",
            "2-step-verifizierung",
            "2-factor",
            "bestatigen sie, dass sie es sind",
            "confirm it’s you",
            "try another way",
            "authenticator",
            "backup code",
            "passkey",
        )
    ):
        return {"state": "challenge_required", "provider": "google"}
    if "accounts.google.com" in url and ("sign in" in title or "sign in with google" in body):
        return {"state": "auth_handoff_required", "provider": "google"}
    if "login.microsoftonline.com" in url or "sign in to your account" in title:
        return {"state": "auth_handoff_required", "provider": "microsoft"}
    return {"state": "", "provider": ""}


def _failure_code_from_error_text(detail: object) -> str:
    lowered = str(detail or "").strip().lower()
    if not lowered:
        return ""
    if "invalid_credentials" in lowered or "email or password you entered is incorrect" in lowered:
        return "invalid_credentials"
    if "auth_request_failed" in lowered or "api.1min.ai/auth/login" in lowered:
        return "auth_request_failed"
    if "challenge_required" in lowered or "turnstile" in lowered or "cloudflare" in lowered:
        return "challenge_required"
    if "session_expired" in lowered or "login required" in lowered or "please sign in" in lowered:
        return "session_expired"
    if "timeout" in lowered:
        return "timeout"
    return ""


def _links_html(links: list[dict[str, object]]) -> str:
    parts: list[str] = []
    for row in links[:24]:
        href = html.escape(str(row.get("href") or "").strip())
        text = html.escape(str(row.get("text") or href).strip() or href)
        if not href:
            continue
        parts.append(f'<a class="chip" href="{href}" target="_blank" rel="noreferrer">{text}</a>')
    return "".join(parts)


def _extracts_html(extracts: dict[str, object]) -> str:
    if not extracts:
        return ""
    sections: list[str] = []
    for key, value in list(extracts.items())[:12]:
        label = html.escape(str(key or "").strip() or "extract")
        text = html.escape(str(value or "").strip())
        if not text:
            continue
        sections.append(f"<section><h2>{label}</h2><pre>{text}</pre></section>")
    return "".join(sections)


def _standalone_html(
    *,
    packet: dict[str, object],
    spec: dict[str, object],
    browser_output: dict[str, object],
    screenshot_data_uri: str,
) -> str:
    result_title = html.escape(str(packet.get("result_title") or packet.get("title") or spec.get("workflow_name") or "BrowserAct Result").strip())
    current_url = html.escape(str(browser_output.get("url") or "").strip())
    body_text = html.escape(str(browser_output.get("bodyText") or "").strip())
    template_key = html.escape(str(packet.get("template_key") or ((spec.get("meta") or {}).get("slug")) or "").strip())
    warning_text = html.escape("\n".join(str(item).strip() for item in (browser_output.get("warnings") or []) if str(item).strip()))
    extracts = browser_output.get("extracts") if isinstance(browser_output.get("extracts"), dict) else {}
    links = browser_output.get("links") if isinstance(browser_output.get("links"), list) else []
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{result_title}</title>
    <style>
      body {{
        margin: 0;
        font-family: "Iowan Old Style", Georgia, serif;
        color: #181714;
        background:
          radial-gradient(circle at top left, rgba(13,90,156,0.16), transparent 28%),
          linear-gradient(180deg, #f5f2eb 0%, #e9e2d6 100%);
      }}
      main {{ max-width: 1180px; margin: 0 auto; padding: 24px; }}
      .panel {{
        background: rgba(255,255,255,0.86);
        border: 1px solid rgba(24,23,20,0.10);
        border-radius: 28px;
        padding: 24px;
        box-shadow: 0 18px 54px rgba(24,23,20,0.08);
        margin-bottom: 18px;
      }}
      .chips {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 18px; }}
      .chip {{
        display: inline-flex;
        align-items: center;
        min-height: 42px;
        padding: 0 14px;
        border-radius: 999px;
        border: 1px solid rgba(24,23,20,0.10);
        background: rgba(255,255,255,0.75);
        text-decoration: none;
        color: inherit;
      }}
      h1 {{ margin: 0 0 10px; font-size: clamp(2rem, 4vw, 3.4rem); line-height: 0.94; }}
      p {{ margin: 0; line-height: 1.6; color: #5e584e; }}
      img {{ width: 100%; border-radius: 22px; border: 1px solid rgba(24,23,20,0.10); margin-top: 18px; }}
      pre {{
        white-space: pre-wrap;
        background: rgba(250,248,243,0.92);
        border: 1px solid rgba(24,23,20,0.10);
        border-radius: 20px;
        padding: 18px;
        line-height: 1.55;
        font-family: "SFMono-Regular", Consolas, monospace;
      }}
      h2 {{ margin-top: 0; }}
      section + section {{ margin-top: 18px; }}
    </style>
  </head>
  <body>
    <main>
      <section class="panel">
        <h1>{result_title}</h1>
        <p>Template-backed BrowserAct workspace capture republished by EA as a browser-openable artifact.</p>
        <div class="chips">
          <div class="chip">Template: {template_key or "n/a"}</div>
          {f'<a class="chip" href="{current_url}" target="_blank" rel="noreferrer">Open Captured Page</a>' if current_url else ''}
        </div>
        {f'<img src="{screenshot_data_uri}" alt="{result_title}">' if screenshot_data_uri else ''}
      </section>
      {f'<section class="panel"><h2>Captured Fields</h2>{_extracts_html(extracts)}</section>' if extracts else ''}
      <section class="panel">
        <h2>Visible Page Text</h2>
        <pre>{body_text}</pre>
      </section>
      {f'<section class="panel"><h2>Visible Links</h2><div class="chips">{_links_html(links)}</div></section>' if links else ''}
      {f'<section class="panel"><h2>Worker Warnings</h2><pre>{warning_text}</pre></section>' if warning_text else ''}
    </main>
  </body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a direct template-backed BrowserAct UI artifact.")
    parser.add_argument("--packet-path", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    packet = _load_packet(args.packet_path or None)
    spec = _load_spec(packet)
    packet.setdefault("login_email", DEFAULT_EMAIL)
    packet.setdefault("login_password", DEFAULT_PASSWORD)
    packet.setdefault("browseract_username", str(packet.get("login_email") or DEFAULT_EMAIL).strip())
    packet.setdefault("browseract_password", str(packet.get("login_password") or DEFAULT_PASSWORD).strip())
    timeout_seconds = max(120, int(packet.get("timeout_seconds") or 300))
    result_title = str(packet.get("result_title") or packet.get("title") or spec.get("workflow_name") or "BrowserAct Result").strip()
    run_slug = _slugify(result_title)
    service_key = str(packet.get("service_key") or packet.get("template_key") or "browseract_template").strip() or "browseract_template"
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    SHARED_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    service_root = OUTPUT_ROOT / service_key
    service_root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(service_root, 0o777)
    except Exception:
        pass
    run_dir = service_root / f"{time.strftime('%Y%m%d-%H%M%S')}-{run_slug}"
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(run_dir, 0o777)
    except Exception:
        pass
    screenshot_path = run_dir / "preview.png"
    try:
        browser_output = _run_browser(packet, spec=spec, screenshot_path=screenshot_path, timeout_seconds=timeout_seconds)
    except Exception as exc:
        detail = str(exc or "template_worker_failed").strip()
        failure_code = _failure_code_from_error_text(detail)
        response = {
            "service_key": service_key,
            "result_title": result_title or service_key,
            "render_status": "failed",
            "asset_path": "",
            "mime_type": "text/html",
            "editor_url": None,
            "body_text": "",
            "raw_text": "",
            "error": detail,
            "failure_code": failure_code,
            "ui_failure_code": failure_code,
            "structured_output_json": {
                "service": service_key,
                "template_key": str(packet.get("template_key") or ((spec.get("meta") or {}).get("slug")) or "").strip(),
                "warnings": [],
                "errors": [detail],
                "render_status": "failed",
                **({"failure_code": failure_code, "ui_failure_code": failure_code} if failure_code else {}),
            },
        }
        print(json.dumps(response, ensure_ascii=False))
        return 1
    screenshot_data_uri = _image_data_uri(screenshot_path)
    html_path = run_dir / "result.html"
    html_path.write_text(
        _standalone_html(packet=packet, spec=spec, browser_output=browser_output, screenshot_data_uri=screenshot_data_uri),
        encoding="utf-8",
    )
    auth_handoff = _auth_handoff_state(browser_output)
    render_status = "completed"
    if browser_output.get("warnings"):
        render_status = "completed_with_warnings"
    if auth_handoff["state"]:
        render_status = auth_handoff["state"]
    response = {
        "service_key": service_key,
        "result_title": result_title or service_key,
        "render_status": render_status,
        "asset_path": str(html_path),
        "mime_type": "text/html",
        "editor_url": str(browser_output.get("url") or "").strip() or None,
        "body_text": str(browser_output.get("bodyText") or "").strip(),
        "raw_text": str(browser_output.get("bodyText") or "").strip(),
        "structured_output_json": {
            "service": service_key,
            "template_key": str(packet.get("template_key") or ((spec.get("meta") or {}).get("slug")) or "").strip(),
            "url": str(browser_output.get("url") or "").strip(),
            "page_title": str(browser_output.get("title") or "").strip(),
            "labels": list(browser_output.get("labels") or []),
            "buttons": list(browser_output.get("buttons") or []),
            "links": list(browser_output.get("links") or []),
            "extracts": dict(browser_output.get("extracts") or {}),
            "warnings": list(browser_output.get("warnings") or []),
            "auth_handoff": auth_handoff,
            "workflow_kind": str((((spec.get("meta") or {}).get("workflow_kind")) or browser_output.get("workflow_kind") or "")).strip(),
            "screenshot_path": str(screenshot_path),
            "html_path": str(html_path),
            "render_status": render_status,
        },
    }
    print(json.dumps(response, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
