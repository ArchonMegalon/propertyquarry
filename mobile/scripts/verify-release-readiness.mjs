#!/usr/bin/env node

import { createHash } from "node:crypto";
import { constants as fsConstants } from "node:fs";
import { access, lstat, readFile, realpath } from "node:fs/promises";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const APP_ID = "com.myexternalbrain.propertyquarry";
const ORIGIN = "https://propertyquarry.com";
const LOCAL_CONTRACT = "propertyquarry.android.release_evidence.v1";
const PLAY_CONTRACT = "propertyquarry.android.play_evidence.v1";
const ALLOWED_TEST_TRACKS = new Set(["internal", "closed"]);

function check(id, state, detail) {
  return { id, state, detail };
}

function normalizeFingerprint(value) {
  return String(value || "").replaceAll(":", "").trim().toLowerCase();
}

function validFingerprint(value) {
  return /^[0-9a-f]{64}$/.test(normalizeFingerprint(value));
}

async function readJson(pathname) {
  return JSON.parse(await readFile(pathname, "utf8"));
}

async function sha256(pathname) {
  const digest = createHash("sha256");
  digest.update(await readFile(pathname));
  return digest.digest("hex");
}

async function safeRegularFile(root, pathname) {
  const absoluteRoot = await realpath(root);
  const absolutePath = path.resolve(root, pathname);
  const resolvedPath = await realpath(absolutePath);
  const relative = path.relative(absoluteRoot, resolvedPath);
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error("artifact_path_outside_mobile_root");
  }
  const metadata = await lstat(absolutePath);
  if (!metadata.isFile() || metadata.isSymbolicLink()) {
    throw new Error("artifact_not_regular_file");
  }
  return resolvedPath;
}

async function fetchJson(fetchImpl, url) {
  const response = await fetchImpl(url, {
    cache: "no-store",
    redirect: "error",
    signal: AbortSignal.timeout(10_000),
  });
  if (!response.ok) throw new Error(`http_${response.status}`);
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.toLowerCase().includes("application/json")) {
    throw new Error("unexpected_content_type");
  }
  return response.json();
}

function resolveGitHead(mobileRoot) {
  const result = spawnSync("git", ["-C", mobileRoot, "rev-parse", "HEAD"], {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
  });
  if (result.status !== 0) throw new Error("git_head_unavailable");
  return result.stdout.trim();
}

export async function auditAndroidRelease({
  mobileRoot,
  localEvidencePath = path.join(mobileRoot, "build/propertyquarry-android-release-evidence.json"),
  playEvidencePath = path.join(mobileRoot, "build/propertyquarry-google-play-evidence.json"),
  origin = ORIGIN,
  fetchImpl = globalThis.fetch,
  gitHead,
} = {}) {
  const checks = [];
  let localEvidence;
  let playEvidence;
  let artifactPath;

  if (origin !== ORIGIN) {
    checks.push(check("origin", "fail", "origin_must_be_exact_production_https_origin"));
  } else {
    checks.push(check("origin", "pass", ORIGIN));
  }

  try {
    localEvidence = await readJson(localEvidencePath);
    checks.push(check("local_evidence_present", "pass", LOCAL_CONTRACT));
  } catch (error) {
    checks.push(check("local_evidence_present", "fail", error.message));
  }

  if (localEvidence) {
    const exactLocalContract =
      localEvidence.contract_name === LOCAL_CONTRACT &&
      localEvidence.application_id === APP_ID &&
      localEvidence.status === "upload_ready_local" &&
      localEvidence.source_dirty === false &&
      localEvidence.bundletool_validate === true &&
      localEvidence.web_contract_tests === true &&
      localEvidence.release_unit_tests === true &&
      localEvidence.release_lint === true &&
      localEvidence.jar_signature_verified === true &&
      localEvidence.embedded_signer_matches_upload_certificate === true;
    checks.push(check(
      "local_evidence_contract",
      exactLocalContract ? "pass" : "fail",
      exactLocalContract ? "signed_release_contract_complete" : "signed_release_contract_incomplete",
    ));

    try {
      const currentHead = gitHead || resolveGitHead(mobileRoot);
      const matches = localEvidence.source_commit === currentHead;
      checks.push(check("source_commit", matches ? "pass" : "fail", matches ? currentHead : "source_commit_mismatch"));
    } catch (error) {
      checks.push(check("source_commit", "fail", error.message));
    }

    try {
      artifactPath = await safeRegularFile(mobileRoot, localEvidence.artifact_path);
      const actualSha256 = await sha256(artifactPath);
      const matches = actualSha256 === localEvidence.artifact_sha256;
      checks.push(check("aab_sha256", matches ? "pass" : "fail", matches ? actualSha256 : "artifact_digest_mismatch"));
    } catch (error) {
      checks.push(check("aab_sha256", "fail", error.message));
    }
  }

  try {
    playEvidence = await readJson(playEvidencePath);
    checks.push(check("play_evidence_present", "pass", PLAY_CONTRACT));
  } catch {
    checks.push(check("play_evidence_present", "blocked", "play_console_receipt_pending"));
  }

  let playFingerprint = "";
  if (playEvidence && localEvidence) {
    playFingerprint = normalizeFingerprint(playEvidence.app_signing_certificate_sha256);
    const exactPlayContract =
      playEvidence.contract_name === PLAY_CONTRACT &&
      playEvidence.application_id === APP_ID &&
      playEvidence.app_created === true &&
      playEvidence.play_app_signing_enabled === true &&
      validFingerprint(playFingerprint) &&
      ALLOWED_TEST_TRACKS.has(playEvidence.release_track) &&
      playEvidence.upload_status === "completed" &&
      playEvidence.artifact_sha256 === localEvidence.artifact_sha256 &&
      playEvidence.production_rollout_started === false;
    checks.push(check(
      "play_evidence_contract",
      exactPlayContract ? "pass" : "fail",
      exactPlayContract ? `test_track_${playEvidence.release_track}` : "play_console_receipt_incomplete",
    ));
  }

  try {
    const runtime = await fetchJson(fetchImpl, `${ORIGIN}/mobile/runtime-contract`);
    const runtimeCore =
      runtime.status === "ok" &&
      runtime.contract_version === "1" &&
      runtime.app_id === APP_ID &&
      runtime.public_origin === ORIGIN &&
      runtime.minimum_android_build <= Number(localEvidence?.version_code || 0) &&
      runtime.start_path === "/app/search" &&
      runtime.external_auth_path === "/sign-in/google" &&
      runtime.mobile_auth_return_to === "/mobile/auth/complete" &&
      runtime.mobile_auth_redeem_path === "/mobile/auth/redeem" &&
      runtime.share_import_path === "/app/api/mobile/property-links" &&
      JSON.stringify(runtime.app_link_paths) === JSON.stringify([
        "/app",
        "/app/*",
        "/shortlist",
        "/shortlist/*",
      ]) &&
      runtime.walkthrough_default === "camera" &&
      JSON.stringify(runtime.spatial_tour_providers) === JSON.stringify(["3dvista", "matterport"]) &&
      runtime.vr_mode === "optional";
    checks.push(check("runtime_contract", runtimeCore ? "pass" : "fail", runtimeCore ? "production_contract_exact" : "production_contract_mismatch"));

    const runtimeLinksReady =
      runtime.app_links_ready === true &&
      runtime.app_links_ready_by_app_id?.[APP_ID] === true;
    checks.push(check(
      "runtime_app_links",
      runtimeLinksReady ? "pass" : "blocked",
      runtimeLinksReady ? "production_app_links_ready" : "production_app_links_not_ready",
    ));
  } catch (error) {
    checks.push(check("runtime_contract", "fail", error.message));
  }

  try {
    const statements = await fetchJson(fetchImpl, `${ORIGIN}/.well-known/assetlinks.json`);
    const productionStatement = Array.isArray(statements)
      ? statements.find((statement) => statement?.target?.package_name === APP_ID)
      : undefined;
    if (!productionStatement) {
      checks.push(check("assetlinks", "blocked", "production_package_statement_pending"));
    } else if (!validFingerprint(playFingerprint)) {
      checks.push(check("assetlinks", "blocked", "play_signing_fingerprint_pending"));
    } else {
      const fingerprints = productionStatement.target.sha256_cert_fingerprints || [];
      const matches =
        productionStatement.target.namespace === "android_app" &&
        productionStatement.relation?.includes("delegate_permission/common.handle_all_urls") &&
        fingerprints.some((fingerprint) => normalizeFingerprint(fingerprint) === playFingerprint);
      checks.push(check("assetlinks", matches ? "pass" : "fail", matches ? "play_signer_published" : "play_signer_not_published"));
    }
  } catch (error) {
    checks.push(check("assetlinks", "fail", error.message));
  }

  try {
    const response = await fetchImpl(`${ORIGIN}/privacy`, {
      cache: "no-store",
      redirect: "error",
      signal: AbortSignal.timeout(10_000),
    });
    checks.push(check("privacy", response.ok ? "pass" : "fail", response.ok ? "public_privacy_available" : `http_${response.status}`));
  } catch (error) {
    checks.push(check("privacy", "fail", error.message));
  }

  const failed = checks.filter((row) => row.state === "fail");
  const blocked = checks.filter((row) => row.state === "blocked");
  const status = failed.length ? "failed" : blocked.length ? "blocked" : "ready";
  return {
    contract_name: "propertyquarry.android.release_readiness.v1",
    observed_at: new Date().toISOString(),
    status,
    application_id: APP_ID,
    origin: ORIGIN,
    failed_count: failed.length,
    blocked_count: blocked.length,
    production_rollout_authorized: false,
    checks,
  };
}

function parseArguments(argv) {
  const args = {};
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === "--local-evidence") args.localEvidencePath = argv[++index];
    else if (value === "--play-evidence") args.playEvidencePath = argv[++index];
    else if (value === "--origin") args.origin = argv[++index];
    else throw new Error(`unknown_argument:${value}`);
  }
  return args;
}

async function main() {
  const mobileRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
  await access(mobileRoot, fsConstants.R_OK);
  const report = await auditAndroidRelease({ mobileRoot, ...parseArguments(process.argv.slice(2)) });
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
  process.exitCode = report.status === "ready" ? 0 : report.status === "blocked" ? 2 : 1;
}

if (path.resolve(process.argv[1] || "") === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  });
}
