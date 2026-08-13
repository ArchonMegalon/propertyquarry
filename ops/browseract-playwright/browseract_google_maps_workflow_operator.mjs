import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { chromium } from "playwright";

const DEFAULT_TEMPLATE_ID = "77577579210625331";
const DEFAULT_TEMPLATE_URL = "https://www.browseract.com/template/google-maps-api";
const DEFAULT_WORKFLOW_NAME = "propertyquarry_google_maps_distance_research_v1";
const ALLOWED_HOSTS = new Set(["browseract.com", "www.browseract.com"]);
const RECEIPT_VERSION = "propertyquarry.browser_ooda_receipt.v1";
const GOOGLE_MAPS_EVIDENCE_EXTRACTION_PROMPT = `Extract the following fields from the current Google Maps place page:
place_name
place_category
place_id
destination_latitude
destination_longitude
final_surface_url
visible_text
address
plus_code

Return every key exactly once. Use the exact current Google Maps place URL as
final_surface_url. Read place_id and destination coordinates only from that URL
or the current Google Maps page state; never estimate or infer them. visible_text
must be a compact visible title, category, address, and plus-code summary. Return
null for any field that is not directly available.`;

function parseArgs(argv) {
  const options = {
    mode: "inspect",
    templateId: DEFAULT_TEMPLATE_ID,
    templateUrl: DEFAULT_TEMPLATE_URL,
    workflowId: "",
    stepName: "",
    workflowName: DEFAULT_WORKFLOW_NAME,
    envFile: "",
    evidenceDir: "/evidence",
    receipt: "/evidence/browseract-google-maps-workflow.json",
    profileDir: "/profile",
    headless: true,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const flag = argv[index];
    if (flag === "--headed") {
      options.headless = false;
      continue;
    }
    const value = argv[index + 1];
    if (!value || value.startsWith("--")) {
      throw new Error(`missing value for ${flag}`);
    }
    index += 1;
    if (flag === "--mode") options.mode = value;
    else if (flag === "--template-id") options.templateId = value;
    else if (flag === "--template-url") options.templateUrl = value;
    else if (flag === "--workflow-id") options.workflowId = value;
    else if (flag === "--step-name") options.stepName = value;
    else if (flag === "--workflow-name") options.workflowName = value;
    else if (flag === "--env-file") options.envFile = value;
    else if (flag === "--evidence-dir") options.evidenceDir = value;
    else if (flag === "--receipt") options.receipt = value;
    else if (flag === "--profile-dir") options.profileDir = value;
    else throw new Error(`unsupported argument: ${flag}`);
  }
  if (!new Set(["inspect", "clone", "publish", "configure-extract", "configure-loop"]).has(options.mode)) {
    throw new Error("--mode must be inspect, clone, publish, configure-extract, or configure-loop");
  }
  const template = new URL(options.templateUrl);
  if (template.protocol !== "https:" || !ALLOWED_HOSTS.has(template.hostname)) {
    throw new Error("template URL must use HTTPS on browseract.com");
  }
  if (!/^\d{8,24}$/.test(options.templateId)) {
    throw new Error("template ID must be numeric");
  }
  if (options.workflowId && !/^\d{8,24}$/.test(options.workflowId)) {
    throw new Error("workflow ID must be numeric");
  }
  if (new Set(["publish", "configure-extract", "configure-loop"]).has(options.mode) && !options.workflowId) {
    throw new Error(`--mode ${options.mode} requires --workflow-id`);
  }
  if (options.stepName && !/^[A-Za-z0-9 _-]{1,80}$/u.test(options.stepName)) {
    throw new Error("step name is outside the governed label format");
  }
  if (options.mode === "configure-extract" && options.stepName !== "Extract Data_1") {
    throw new Error("--mode configure-extract requires --step-name 'Extract Data_1'");
  }
  if (options.mode === "configure-loop" && options.stepName !== "Loop List_1") {
    throw new Error("--mode configure-loop requires --step-name 'Loop List_1'");
  }
  if (!/^[a-z0-9][a-z0-9_-]{7,79}$/i.test(options.workflowName)) {
    throw new Error("workflow name is outside the governed identifier format");
  }
  return options;
}

function parseEnvFile(filePath) {
  if (!filePath) return {};
  const content = fs.readFileSync(filePath, { encoding: "utf8", flag: "r" });
  const parsed = {};
  for (const rawLine of content.split(/\r?\n/u)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const normalized = line.startsWith("export ") ? line.slice(7) : line;
    const separator = normalized.indexOf("=");
    if (separator <= 0) continue;
    const name = normalized.slice(0, separator).trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/u.test(name)) continue;
    let value = normalized.slice(separator + 1).trim();
    if (
      value.length >= 2 &&
      ((value.startsWith('"') && value.endsWith('"')) ||
        (value.startsWith("'") && value.endsWith("'")))
    ) {
      value = value.slice(1, -1);
    }
    parsed[name] = value;
  }
  return parsed;
}

function sha256(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function safeAccountRef(environment) {
  const identity = [
    environment.BROWSERACT_USERNAME,
    environment.BROWSERACT_EMAIL,
    environment.BROWSERACT_LOGIN_EMAIL,
  ].find((value) => typeof value === "string" && value.trim());
  return identity ? `sha256:${sha256(identity.trim().toLowerCase())}` : "unavailable";
}

function credentialPair(environment) {
  const username = [
    environment.BROWSERACT_USERNAME,
    environment.BROWSERACT_EMAIL,
    environment.BROWSERACT_LOGIN_EMAIL,
  ].find((value) => typeof value === "string" && value.trim());
  const password = [
    environment.BROWSERACT_PASSWORD,
    environment.BROWSERACT_LOGIN_PASSWORD,
  ].find((value) => typeof value === "string" && value);
  return { username: username || "", password: password || "" };
}

function ensurePrivateDirectory(directory) {
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  fs.chmodSync(directory, 0o700);
}

function writePrivateJson(filePath, payload) {
  ensurePrivateDirectory(path.dirname(filePath));
  const temporary = `${filePath}.tmp-${process.pid}`;
  fs.writeFileSync(temporary, `${JSON.stringify(payload, null, 2)}\n`, {
    encoding: "utf8",
    mode: 0o600,
    flag: "wx",
  });
  fs.renameSync(temporary, filePath);
  fs.chmodSync(filePath, 0o600);
}

function workflowIdFromUrl(value) {
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    return "";
  }
  for (const segment of parsed.pathname.split("/").filter(Boolean).reverse()) {
    if (/^\d{8,24}$/u.test(segment)) return segment;
  }
  for (const name of ["workflow_id", "workflowId", "id"]) {
    const candidate = parsed.searchParams.get(name) || "";
    if (/^\d{8,24}$/u.test(candidate)) return candidate;
  }
  return "";
}

function safeUrl(value) {
  try {
    const parsed = new URL(value);
    if (parsed.protocol !== "https:" || !ALLOWED_HOSTS.has(parsed.hostname)) return "blocked";
    parsed.search = "";
    parsed.hash = "";
    return parsed.toString();
  } catch {
    return "invalid";
  }
}

async function capture(page, options, label, evidence) {
  const fileName = `${String(evidence.length + 1).padStart(2, "0")}-${label}.png`;
  const filePath = path.join(options.evidenceDir, fileName);
  await page.screenshot({ path: filePath, fullPage: true });
  fs.chmodSync(filePath, 0o600);
  evidence.push({
    kind: "screenshot",
    label,
    path: fileName,
    sha256: sha256(fs.readFileSync(filePath)),
  });
}

async function visibleInteractiveSummary(page) {
  return page.locator("a, button, input, textarea, [contenteditable=true], [role=button], [role=spinbutton]").evaluateAll((elements) =>
    elements
      .filter((element) => {
        const style = window.getComputedStyle(element);
        const rectangle = element.getBoundingClientRect();
        return style.visibility !== "hidden" && style.display !== "none" && rectangle.width > 0 && rectangle.height > 0;
      })
      .slice(0, 80)
      .map((element) => {
        const name = (element.getAttribute("aria-label") || element.textContent || element.getAttribute("placeholder") || "")
          .replace(/\s+/gu, " ")
          .trim()
          .slice(0, 120)
          .replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/giu, "[redacted-email]");
        return {
          tag: element.tagName.toLowerCase(),
          type: element.getAttribute("type") || "",
          role: element.getAttribute("role") || "",
          numeric_value: (() => {
            const value = element instanceof HTMLInputElement
              ? element.value
              : element.getAttribute("aria-valuenow") || element.textContent || "";
            return /^\d{1,3}$/u.test(value.trim()) ? value.trim() : "";
          })(),
          name,
        };
      }),
  );
}

async function firstVisible(locator) {
  const count = await locator.count();
  for (let index = 0; index < count; index += 1) {
    const candidate = locator.nth(index);
    if (await candidate.isVisible().catch(() => false)) return candidate;
  }
  return null;
}

async function clickNamed(page, pattern) {
  const candidate = await firstVisible(
    page.getByRole("button", { name: pattern }).or(page.getByRole("link", { name: pattern })),
  );
  if (!candidate) return false;
  await candidate.click();
  return true;
}

async function clickNamedWithPossiblePopup(context, page, pattern) {
  const knownPages = new Set(context.pages());
  const popupPromise = context.waitForEvent("page", { timeout: 5_000 }).catch(() => null);
  const clicked = await clickNamed(page, pattern);
  if (!clicked) return { clicked: false, page };
  const popup = await popupPromise;
  const destination = popup || context.pages().find((candidate) => !knownPages.has(candidate)) || page;
  if (destination !== page) {
    await destination.waitForLoadState("domcontentloaded", { timeout: 45_000 }).catch(() => undefined);
  }
  return { clicked: true, page: destination };
}

async function confirmPublishIfOffered(page) {
  for (const scope of [page.getByRole("dialog"), page.locator('[role="menu"]')]) {
    const visibleScope = await firstVisible(scope);
    if (!visibleScope) continue;
    const confirmation = await firstVisible(
      visibleScope.getByRole("button", { name: /^(publish|publish as new version|confirm|continue)$/iu }),
    );
    if (!confirmation) continue;
    await confirmation.click();
    return true;
  }
  return false;
}

async function verifyWorkflowViaApi(environment, workflowId) {
  const apiKey = [
    environment.BROWSERACT_API_KEY,
    environment.BROWSERACT_API_KEY_FALLBACK_1,
    environment.BROWSERACT_API_KEY_FALLBACK_2,
    environment.BROWSERACT_API_KEY_FALLBACK_3,
  ].find((value) => typeof value === "string" && value.trim());
  if (!apiKey) return { listed: false, status: "api_key_unavailable", name: "" };
  const response = await fetch("https://api.browseract.com/v2/workflow/list-workflows", {
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "User-Agent": "PropertyQuarry-BrowserAct-Operator/1.0",
    },
    signal: AbortSignal.timeout(30_000),
  });
  if (!response.ok) return { listed: false, status: `http_${response.status}`, name: "" };
  const body = await response.json();
  const rows = [body?.workflows, body?.data, body?.items, body?.rows].find(Array.isArray) || [];
  const match = rows.find((row) => {
    const candidate = String(row?.workflow_id || row?.id || row?._id || row?.workflowId || "").trim();
    return candidate === workflowId;
  });
  return {
    listed: Boolean(match),
    status: "ok",
    name: match ? String(match.name || match.title || match.workflow_name || "").slice(0, 120) : "",
  };
}

async function publishWorkflow(page, environment, workflowId, actions) {
  const publishClicked = await clickNamed(page, /^publish$/iu);
  if (!publishClicked) throw new Error("BrowserAct Publish control was not unambiguously found");
  actions.push("requested_workflow_publish");
  await page.waitForTimeout(1_500);
  const publishNewVersion = await firstVisible(
    page.getByRole("button", { name: /^publish as new version$/iu }),
  );
  if (publishNewVersion) {
    await publishNewVersion.click();
    actions.push("selected_publish_as_new_version");
    await page.waitForTimeout(1_500);
  }
  if (await confirmPublishIfOffered(page)) actions.push("confirmed_workflow_publish");
  await page.waitForLoadState("networkidle", { timeout: 45_000 }).catch(() => undefined);
  await page.waitForTimeout(5_000);
  const providerVerification = await verifyWorkflowViaApi(environment, workflowId);
  if (!providerVerification.listed) {
    throw new Error(`published BrowserAct workflow is absent from the provider API (${providerVerification.status})`);
  }
  actions.push("verified_workflow_in_provider_api");
  return providerVerification;
}

async function configureEvidenceExtraction(page, actions) {
  const editor = await firstVisible(
    page.locator('[contenteditable="true"][role="textbox"], [contenteditable="true"], textarea'),
  );
  if (!editor) throw new Error("BrowserAct extraction field editor was not unambiguously found");
  const before = await editor.evaluate((element) =>
    element instanceof HTMLTextAreaElement ? element.value : element.textContent || "",
  );
  if (!before.includes("Tittle Name") || !before.includes("plus_code")) {
    throw new Error("BrowserAct extraction field no longer matches the reviewed Google Maps template");
  }
  await editor.fill(GOOGLE_MAPS_EVIDENCE_EXTRACTION_PROMPT);
  await editor.press("Tab");
  await page.waitForTimeout(2_000);
  const after = await editor.evaluate((element) =>
    element instanceof HTMLTextAreaElement ? element.value : element.textContent || "",
  );
  if (after.trim() !== GOOGLE_MAPS_EVIDENCE_EXTRACTION_PROMPT.trim()) {
    throw new Error("BrowserAct extraction field did not retain the governed evidence contract");
  }
  actions.push("configured_google_maps_evidence_fields");
  return {
    before_sha256: `sha256:${sha256(before)}`,
    after_sha256: `sha256:${sha256(after)}`,
    required_fields: [
      "place_name",
      "place_category",
      "place_id",
      "destination_latitude",
      "destination_longitude",
      "final_surface_url",
      "visible_text",
      "address",
      "plus_code",
    ],
  };
}

async function configureNearestResultLoop(page, actions) {
  const inputs = page.locator('input, [role="spinbutton"], [contenteditable="true"]');
  let loopLimit = null;
  for (let index = 0; index < (await inputs.count()); index += 1) {
    const candidate = inputs.nth(index);
    if (!(await candidate.isVisible().catch(() => false))) continue;
    const value = await candidate.evaluate((element) =>
      element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement
        ? element.value
        : element.getAttribute("aria-valuenow") || element.textContent || "",
    );
    if (value.trim() === "10") {
      if (loopLimit) throw new Error("multiple BrowserAct loop-limit inputs matched the reviewed value");
      loopLimit = candidate;
    }
  }
  if (!loopLimit) throw new Error("BrowserAct reviewed loop limit of 10 was not unambiguously found");
  await loopLimit.fill("1");
  await loopLimit.press("Tab");
  await page.waitForTimeout(2_000);
  const retainedValue = await loopLimit.evaluate((element) =>
    element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement
      ? element.value
      : element.getAttribute("aria-valuenow") || element.textContent || "",
  );
  if (retainedValue.trim() !== "1") {
    throw new Error("BrowserAct loop limit did not retain the governed nearest-result bound");
  }
  actions.push("bounded_google_maps_result_loop_to_one");
  return {
    before_sha256: `sha256:${sha256("10")}`,
    after_sha256: `sha256:${sha256("1")}`,
    max_results: 1,
  };
}

async function loginIfNeeded(page, environment, actions) {
  let passwordInput = await firstVisible(page.locator('input[type="password"]'));
  if (!passwordInput) return false;
  const signUpHeading = await firstVisible(page.getByRole("heading", { name: /^sign up$/iu }));
  if (signUpHeading) {
    const switched = await clickNamed(page, /^log in$/iu);
    if (!switched) throw new Error("BrowserAct login tab control was not unambiguously found");
    await page.waitForTimeout(500);
    passwordInput = await firstVisible(page.locator('input[type="password"]'));
    if (!passwordInput) throw new Error("BrowserAct login password field was unavailable after switching tabs");
  }
  const credentials = credentialPair(environment);
  if (!credentials.username || !credentials.password) {
    throw new Error("BrowserAct login is required but governed login credentials are unavailable");
  }
  const usernameInput = await firstVisible(
    page.locator('input[type="email"], input[name*="email" i], input[name*="user" i], input[placeholder*="mail" i], input[autocomplete="username"]'),
  );
  if (!usernameInput) throw new Error("BrowserAct login identity field was not unambiguously found");
  await usernameInput.fill(credentials.username);
  await passwordInput.fill(credentials.password);
  const loginResponses = [];
  const observeLoginResponse = (response) => {
    try {
      const responseUrl = new URL(response.url());
      if (
        ALLOWED_HOSTS.has(responseUrl.hostname) &&
        response.request().method() === "POST" &&
        /(auth|login|session|user)/iu.test(responseUrl.pathname)
      ) {
        loginResponses.push({ path: responseUrl.pathname.slice(0, 160), status: response.status() });
      }
    } catch {
      return;
    }
  };
  page.on("response", observeLoginResponse);
  const submitted = await clickNamed(page, /^(log in|login|sign in)$/iu);
  if (!submitted) throw new Error("BrowserAct login submit control was not unambiguously found");
  actions.push("submitted_existing_account_login");
  await page
    .waitForURL((url) => !url.pathname.startsWith("/login"), { timeout: 20_000 })
    .catch(() => undefined);
  page.off("response", observeLoginResponse);
  const remainingPassword = await firstVisible(page.locator('input[type="password"]'));
  if (remainingPassword) {
    const feedback = await firstVisible(
      page.locator('[role="alert"], [class*="error-message" i], [class*="message-content" i], [class*="toast" i]'),
    );
    let safeFeedback = feedback ? (await feedback.textContent().catch(() => "")) || "" : "";
    for (const secret of [credentials.username, credentials.password]) {
      if (secret) safeFeedback = safeFeedback.replaceAll(secret, "[redacted]");
    }
    const statusSummary = loginResponses.map((row) => `${row.path}:${row.status}`).join(",");
    const detail = [statusSummary, safeFeedback.replace(/\s+/gu, " ").trim().slice(0, 160)]
      .filter(Boolean)
      .join("; ");
    throw new Error(`BrowserAct login remained on the authentication surface${detail ? ` (${detail})` : ""}`);
  }
  actions.push("authenticated_existing_browseract_account");
  return true;
}

async function setWorkflowNameIfOffered(page, workflowName, actions) {
  const input = await firstVisible(
    page.locator('input[name*="workflow" i], input[placeholder*="workflow" i], input[placeholder*="name" i]'),
  );
  if (!input) return false;
  const current = await input.inputValue().catch(() => "");
  if (current && !/google maps api|untitled|copy/iu.test(current)) return false;
  await input.fill(workflowName);
  actions.push("named_workflow");
  return true;
}

async function main() {
  const startedAt = new Date().toISOString();
  let options;
  try {
    options = parseArgs(process.argv.slice(2));
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    return 2;
  }
  ensurePrivateDirectory(options.evidenceDir);
  ensurePrivateDirectory(options.profileDir);
  const environment = { ...process.env, ...parseEnvFile(options.envFile) };
  const receipt = {
    schema: RECEIPT_VERSION,
    site: "browseract.com",
    account_ref: safeAccountRef(environment),
    work_type: "account_review_and_reversible_workflow_configuration",
    template_id: options.templateId,
    requested_actions: options.mode === "clone"
      ? ["authenticate_existing_account", "clone_template", "name_workflow", "save_workflow"]
      : options.mode === "publish"
        ? ["authenticate_existing_account", "publish_existing_workflow", "verify_api_listing"]
      : options.mode === "configure-extract"
        ? ["authenticate_existing_account", "configure_reviewed_extract_fields", "publish_new_version", "verify_api_listing"]
      : options.mode === "configure-loop"
        ? ["authenticate_existing_account", "bound_result_loop_to_one", "publish_new_version", "verify_api_listing"]
      : options.workflowId
        ? ["inspect_existing_workflow"]
        : ["inspect_template_clone_entrypoint"],
    completed_actions: [],
    quality_gate: "blocked",
    workflow_id: "",
    final_surface_url: "",
    irreversible_actions: [],
    blockers: [],
    evidence: [],
    started_at: startedAt,
    completed_at: "",
  };

  let context;
  let page;
  try {
    context = await chromium.launchPersistentContext(options.profileDir, {
      headless: options.headless,
      viewport: { width: 1440, height: 1000 },
      locale: "en-US",
      timezoneId: "Europe/Vienna",
    });
    page = context.pages()[0] || (await context.newPage());
    if (options.workflowId && new Set(["inspect", "publish", "configure-extract", "configure-loop"]).has(options.mode)) {
      const workflowUrl = `https://www.browseract.com/workflow/${options.workflowId}/orchestration`;
      await page.goto(workflowUrl, {
        waitUntil: "domcontentloaded",
        timeout: 60_000,
      });
      const loggedIn = await loginIfNeeded(page, environment, receipt.completed_actions);
      if (loggedIn) {
        await page.goto(workflowUrl, { waitUntil: "domcontentloaded", timeout: 60_000 });
      }
      await page.waitForLoadState("networkidle", { timeout: 45_000 }).catch(() => undefined);
      await page.waitForTimeout(8_000);
      if (new Set(["inspect", "configure-extract", "configure-loop"]).has(options.mode) && options.stepName) {
        const step = await firstVisible(page.getByText(options.stepName, { exact: true }));
        if (!step) throw new Error("requested BrowserAct workflow step was not unambiguously found");
        await step.click();
        receipt.completed_actions.push("opened_existing_workflow_step");
        await page.waitForTimeout(2_000);
      }
      receipt.workflow_id = workflowIdFromUrl(page.url());
      if (receipt.workflow_id !== options.workflowId) {
        throw new Error("requested BrowserAct workflow did not resolve to its governed account URL");
      }
      receipt.completed_actions.push("opened_existing_workflow");
      await capture(page, options, "workflow-inspect", receipt.evidence);
      if (options.mode === "publish") {
        const providerVerification = await publishWorkflow(
          page,
          environment,
          options.workflowId,
          receipt.completed_actions,
        );
        receipt.provider_verification = providerVerification;
        await capture(page, options, "workflow-published", receipt.evidence);
      } else if (options.mode === "configure-extract") {
        receipt.configuration_change = await configureEvidenceExtraction(
          page,
          receipt.completed_actions,
        );
        const providerVerification = await publishWorkflow(
          page,
          environment,
          options.workflowId,
          receipt.completed_actions,
        );
        receipt.provider_verification = providerVerification;
        await capture(page, options, "workflow-configured", receipt.evidence);
      } else if (options.mode === "configure-loop") {
        receipt.configuration_change = await configureNearestResultLoop(
          page,
          receipt.completed_actions,
        );
        const providerVerification = await publishWorkflow(
          page,
          environment,
          options.workflowId,
          receipt.completed_actions,
        );
        receipt.provider_verification = providerVerification;
        await capture(page, options, "workflow-configured", receipt.evidence);
      } else {
        receipt.completed_actions.push("inspected_existing_workflow");
      }
      receipt.quality_gate = "pass";
    } else {
      await page.goto(options.templateUrl, { waitUntil: "networkidle", timeout: 60_000 });
      await capture(page, options, "template", receipt.evidence);

      const initialClone = await clickNamedWithPossiblePopup(context, page, /^create from template$/iu);
      if (!initialClone.clicked) throw new Error("Create from Template control was not unambiguously found");
      page = initialClone.page;
      receipt.completed_actions.push("opened_template_clone_entrypoint");
      await page.waitForLoadState("networkidle", { timeout: 45_000 }).catch(() => undefined);
      await capture(page, options, "clone-entrypoint", receipt.evidence);

      if (options.mode === "inspect") {
        receipt.quality_gate = "pass";
      } else {
        const loggedIn = await loginIfNeeded(page, environment, receipt.completed_actions);
        if (loggedIn) {
          await page.goto(options.templateUrl, { waitUntil: "networkidle", timeout: 60_000 });
          const authenticatedClone = await clickNamedWithPossiblePopup(context, page, /^create from template$/iu);
          if (!authenticatedClone.clicked) {
            throw new Error("Create from Template was unavailable after authentication");
          }
          page = authenticatedClone.page;
          receipt.completed_actions.push("reopened_template_clone_after_authentication");
          await page.waitForLoadState("networkidle", { timeout: 45_000 }).catch(() => undefined);
        }
        await setWorkflowNameIfOffered(page, options.workflowName, receipt.completed_actions);
        const saveClicked = await clickNamed(page, /^(save|create|confirm|finish|complete setup)$/iu);
        if (saveClicked) {
          receipt.completed_actions.push("saved_workflow_configuration");
          await page.waitForLoadState("networkidle", { timeout: 45_000 }).catch(() => undefined);
        }
        receipt.workflow_id = workflowIdFromUrl(page.url());
        if (!receipt.workflow_id || receipt.workflow_id === options.templateId) {
          throw new Error("cloned BrowserAct workflow ID was not proven by the final account URL");
        }
        receipt.completed_actions.push("proved_cloned_workflow_id");
        receipt.quality_gate = "pass";
        await capture(page, options, "workflow", receipt.evidence);
      }
    }

    receipt.final_surface_url = safeUrl(page.url());
    receipt.interactive_summary = await visibleInteractiveSummary(page);
  } catch (error) {
    receipt.blockers.push(String(error?.message || error).slice(0, 500));
    if (page) {
      receipt.final_surface_url = safeUrl(page.url());
      receipt.interactive_summary = await visibleInteractiveSummary(page).catch(() => []);
      await capture(page, options, "blocked", receipt.evidence).catch(() => undefined);
    }
  } finally {
    if (context) await context.close().catch(() => undefined);
    receipt.completed_at = new Date().toISOString();
    writePrivateJson(options.receipt, receipt);
  }

  process.stdout.write(`${JSON.stringify({
    quality_gate: receipt.quality_gate,
    workflow_id: receipt.workflow_id,
    receipt: options.receipt,
    blockers: receipt.blockers,
  })}\n`);
  return receipt.quality_gate === "pass" ? 0 : 1;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  process.exitCode = await main();
}

export {
  credentialPair,
  parseArgs,
  parseEnvFile,
  safeAccountRef,
  safeUrl,
  workflowIdFromUrl,
};
