import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, mkdir, rm, symlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { auditAndroidRelease } from "../scripts/verify-release-readiness.mjs";

const APP_ID = "com.myexternalbrain.propertyquarry";
const FINGERPRINT = "172772ee6f2a8f7f55d74b7653905b46d26389f8c17c000dd0e0877a3544e25e";
const GIT_HEAD = "97bf109919e0c3ec3ae52c759974ebe239176d1a";

function jsonResponse(payload) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function liveFetch({ linksReady }) {
  return async (url) => {
    if (url.endsWith("/mobile/runtime-contract")) {
      return jsonResponse({
        status: "ok",
        contract_version: "1",
        app_id: APP_ID,
        public_origin: "https://propertyquarry.com",
        minimum_android_build: 1,
        start_path: "/app/search",
        external_auth_path: "/sign-in/google",
        mobile_auth_return_to: "/mobile/auth/complete",
        mobile_auth_redeem_path: "/mobile/auth/redeem",
        share_import_path: "/app/api/mobile/property-links",
        app_link_paths: ["/app", "/app/*", "/shortlist", "/shortlist/*"],
        app_links_ready: linksReady,
        app_links_ready_by_app_id: { [APP_ID]: linksReady },
        walkthrough_default: "camera",
        spatial_tour_providers: ["3dvista", "matterport"],
        vr_mode: "optional",
      });
    }
    if (url.endsWith("/.well-known/assetlinks.json")) {
      return jsonResponse(linksReady ? [{
        relation: ["delegate_permission/common.handle_all_urls"],
        target: {
          namespace: "android_app",
          package_name: APP_ID,
          sha256_cert_fingerprints: [FINGERPRINT],
        },
      }] : []);
    }
    if (url.endsWith("/privacy")) return new Response("privacy", { status: 200 });
    throw new Error(`unexpected_url:${url}`);
  };
}

async function fixture({ withPlayEvidence = false } = {}) {
  const mobileRoot = await mkdtemp(path.join(os.tmpdir(), "propertyquarry-android-readiness-"));
  const artifactRelative = "android/app/build/outputs/bundle/release/app-release.aab";
  const artifactPath = path.join(mobileRoot, artifactRelative);
  await mkdir(path.dirname(artifactPath), { recursive: true });
  await mkdir(path.join(mobileRoot, "build"), { recursive: true });
  const artifact = Buffer.from("signed-propertyquarry-aab");
  await writeFile(artifactPath, artifact);
  const artifactSha256 = createHash("sha256").update(artifact).digest("hex");
  const localEvidencePath = path.join(mobileRoot, "build/propertyquarry-android-release-evidence.json");
  const playEvidencePath = path.join(mobileRoot, "build/propertyquarry-google-play-evidence.json");
  await writeFile(localEvidencePath, JSON.stringify({
    contract_name: "propertyquarry.android.release_evidence.v1",
    source_commit: GIT_HEAD,
    source_dirty: false,
    application_id: APP_ID,
    version_code: 1,
    artifact_path: artifactRelative,
    artifact_sha256: artifactSha256,
    bundletool_validate: true,
    web_contract_tests: true,
    release_unit_tests: true,
    release_lint: true,
    jar_signature_verified: true,
    embedded_signer_matches_upload_certificate: true,
    status: "upload_ready_local",
  }));
  if (withPlayEvidence) {
    await writeFile(playEvidencePath, JSON.stringify({
      contract_name: "propertyquarry.android.play_evidence.v1",
      application_id: APP_ID,
      app_created: true,
      play_app_signing_enabled: true,
      app_signing_certificate_sha256: FINGERPRINT,
      release_track: "internal",
      upload_status: "completed",
      artifact_sha256: artifactSha256,
      production_rollout_started: false,
    }));
  }
  return { mobileRoot, localEvidencePath, playEvidencePath, artifactPath };
}

test("release readiness reports only external Play and App Link blockers", async (context) => {
  const paths = await fixture();
  context.after(() => rm(paths.mobileRoot, { recursive: true, force: true }));
  const report = await auditAndroidRelease({
    ...paths,
    gitHead: GIT_HEAD,
    fetchImpl: liveFetch({ linksReady: false }),
  });
  assert.equal(report.status, "blocked");
  assert.equal(report.failed_count, 0);
  assert.deepEqual(
    report.checks.filter((row) => row.state === "blocked").map((row) => row.id).sort(),
    ["assetlinks", "play_evidence_present", "runtime_app_links"],
  );
});

test("release readiness passes with matching Play signing and internal-test evidence", async (context) => {
  const paths = await fixture({ withPlayEvidence: true });
  context.after(() => rm(paths.mobileRoot, { recursive: true, force: true }));
  const report = await auditAndroidRelease({
    ...paths,
    gitHead: GIT_HEAD,
    fetchImpl: liveFetch({ linksReady: true }),
  });
  assert.equal(report.status, "ready");
  assert.equal(report.failed_count, 0);
  assert.equal(report.blocked_count, 0);
  assert.equal(report.production_rollout_authorized, false);
});

test("release readiness fails when the signed AAB no longer matches its receipt", async (context) => {
  const paths = await fixture({ withPlayEvidence: true });
  context.after(() => rm(paths.mobileRoot, { recursive: true, force: true }));
  await writeFile(paths.artifactPath, "tampered-aab");
  const report = await auditAndroidRelease({
    ...paths,
    gitHead: GIT_HEAD,
    fetchImpl: liveFetch({ linksReady: true }),
  });
  assert.equal(report.status, "failed");
  assert.equal(report.checks.find((row) => row.id === "aab_sha256").state, "fail");
});

test("release readiness never fetches a caller-supplied origin", async (context) => {
  const paths = await fixture({ withPlayEvidence: true });
  context.after(() => rm(paths.mobileRoot, { recursive: true, force: true }));
  const seen = [];
  const productionFetch = liveFetch({ linksReady: true });
  const report = await auditAndroidRelease({
    ...paths,
    origin: "https://attacker.invalid",
    gitHead: GIT_HEAD,
    fetchImpl: async (url, options) => {
      seen.push(url);
      return productionFetch(url, options);
    },
  });
  assert.equal(report.status, "failed");
  assert.equal(report.checks.find((row) => row.id === "origin").state, "fail");
  assert.ok(seen.length > 0);
  assert.ok(seen.every((url) => url.startsWith("https://propertyquarry.com/")));
});

test("release readiness rejects an AAB reached through an escaping parent symlink", async (context) => {
  const paths = await fixture({ withPlayEvidence: true });
  const externalRoot = await mkdtemp(path.join(os.tmpdir(), "propertyquarry-aab-escape-"));
  context.after(async () => {
    await rm(paths.mobileRoot, { recursive: true, force: true });
    await rm(externalRoot, { recursive: true, force: true });
  });
  const releaseDirectory = path.dirname(paths.artifactPath);
  await rm(releaseDirectory, { recursive: true, force: true });
  await writeFile(path.join(externalRoot, "app-release.aab"), "signed-propertyquarry-aab");
  await symlink(externalRoot, releaseDirectory, "dir");
  const report = await auditAndroidRelease({
    ...paths,
    gitHead: GIT_HEAD,
    fetchImpl: liveFetch({ linksReady: true }),
  });
  assert.equal(report.status, "failed");
  assert.equal(
    report.checks.find((row) => row.id === "aab_sha256").detail,
    "artifact_path_outside_mobile_root",
  );
});
