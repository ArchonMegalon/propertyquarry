import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  credentialPair,
  parseArgs,
  parseEnvFile,
  safeAccountRef,
  safeUrl,
  workflowIdFromUrl,
} from "./browseract_google_maps_workflow_operator.mjs";

test("operator accepts only the governed BrowserAct template host", () => {
  assert.equal(parseArgs([]).templateId, "77577579210625331");
  assert.throws(
    () => parseArgs(["--template-url", "https://example.com/template"]),
    /browseract\.com/u,
  );
  assert.throws(
    () => parseArgs(["--mode", "publish"]),
    /requires --workflow-id/u,
  );
  assert.throws(
    () => parseArgs([
      "--mode",
      "configure-extract",
      "--workflow-id",
      "110788314941979001",
      "--step-name",
      "Other Step",
    ]),
    /Extract Data_1/u,
  );
  assert.throws(
    () => parseArgs([
      "--mode",
      "configure-loop",
      "--workflow-id",
      "110788314941979001",
      "--step-name",
      "Other Step",
    ]),
    /Loop List_1/u,
  );
});

test("environment file parser does not evaluate shell syntax", () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "pq-browseract-test-"));
  const envFile = path.join(directory, "operator.env");
  fs.writeFileSync(
    envFile,
    "BROWSERACT_USERNAME='operator@example.test'\nBROWSERACT_PASSWORD=literal-$(id)\n",
    { mode: 0o600 },
  );
  const parsed = parseEnvFile(envFile);
  assert.deepEqual(credentialPair(parsed), {
    username: "operator@example.test",
    password: "literal-$(id)",
  });
});

test("receipt identity is one-way and URLs discard query values", () => {
  const environment = { BROWSERACT_USERNAME: "operator@example.test" };
  const reference = safeAccountRef(environment);
  assert.match(reference, /^sha256:[0-9a-f]{64}$/u);
  assert.equal(reference.includes("operator"), false);
  assert.equal(
    safeUrl("https://www.browseract.com/workflow/12345678?token=secret#fragment"),
    "https://www.browseract.com/workflow/12345678",
  );
  assert.equal(safeUrl("https://evil.example/workflow/12345678"), "blocked");
});

test("workflow ID extraction ignores non-numeric account URLs", () => {
  assert.equal(
    workflowIdFromUrl("https://www.browseract.com/workflow/84575247514601092?tab=run"),
    "84575247514601092",
  );
  assert.equal(workflowIdFromUrl("https://www.browseract.com/reception/workflow"), "");
});
