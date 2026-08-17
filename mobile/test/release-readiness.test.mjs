import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, mkdir, rm, symlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { auditAndroidRelease } from "../scripts/verify-release-readiness.mjs";

const APP_ID = "com.myexternalbrain.propertyquarry";
const FINGERPRINT = "172772ee6f2a8f7f55d74b7653905b46d26389f8c17c000dd0e0877a3544e25e";
const UPLOAD_FINGERPRINT = "a8887d6641bf7135e374b0d8504c841af5d726090fb81eae5967075b608116cf";
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

async function fixture({
  withPlayEvidence = false,
  withCooldownEvidence = false,
  playEvidenceOverrides = {},
} = {}) {
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
  const uploadKeyActivationPath = path.join(mobileRoot, "build/propertyquarry-upload-key-activation-receipt.json");
  await writeFile(localEvidencePath, JSON.stringify({
    contract_name: "propertyquarry.android.release_evidence.v1",
    source_commit: GIT_HEAD,
    source_dirty: false,
    application_id: APP_ID,
    version_code: 1,
    version_name: "1.1.0",
    artifact_path: artifactRelative,
    artifact_sha256: artifactSha256,
    bundletool_validate: true,
    web_contract_tests: true,
    release_unit_tests: true,
    release_lint: true,
    jar_signature_verified: true,
    embedded_signer_matches_upload_certificate: true,
    upload_certificate_sha256: UPLOAD_FINGERPRINT,
    status: "upload_ready_local",
  }));
  if (withPlayEvidence) {
    await writeFile(playEvidencePath, JSON.stringify({
      contract_name: "propertyquarry.android.play_evidence.v1",
      application_id: APP_ID,
      developer_account_id: "9007890349240845326",
      play_app_id: "4976153363318887490",
      app_created: true,
      play_app_signing_enabled: true,
      app_signing_certificate_sha256: FINGERPRINT,
      release_track: "internal",
      release_track_name: "Internal testing",
      release_track_id: "4701487190338825843",
      release_name: "1 (1.1.0)",
      version_code: 1,
      version_name: "1.1.0",
      upload_status: "completed",
      submission_status: "available_to_selected_testers",
      artifact_sha256: withCooldownEvidence ? "historical-version-one-artifact" : artifactSha256,
      managed_publishing: false,
      production_status: "inactive",
      production_rollout_started: false,
      ...playEvidenceOverrides,
    }));
  }
  if (withCooldownEvidence) {
    const screenshotRelative = "build/propertyquarry-upload-key-cooldown.png";
    const screenshot = Buffer.from("verified-play-cooldown-screenshot");
    const screenshotSha256 = createHash("sha256").update(screenshot).digest("hex");
    await writeFile(path.join(mobileRoot, screenshotRelative), screenshot);
    await writeFile(uploadKeyActivationPath, JSON.stringify({
      contract_name: "propertyquarry.google_play.upload_key_activation.v1",
      developer_account_id: "9007890349240845326",
      play_app_id: "4976153363318887490",
      application_id: APP_ID,
      upload_key: {
        status: "active",
        certificate_sha256: UPLOAD_FINGERPRINT,
        verified_in_play_console: true,
      },
      security_cooldown: {
        status: "upload_blocked_until_eligible_at",
        eligible_at: "2026-08-08T12:09:53Z",
      },
      internal_release: {
        track: "internal",
        track_id: "4701487190338825843",
        release_id: "2",
        draft_url: "https://play.google.com/console/u/0/developers/9007890349240845326/app/4976153363318887490/tracks/4701487190338825843/releases/2/prepare",
        status: "draft_upload_blocked_by_security_cooldown",
        artifact_sha256: artifactSha256,
        version_code: 1,
        version_name: "1.1.0",
      },
      production_release_changed: false,
      evidence: {
        screenshot_path: screenshotRelative,
        screenshot_sha256: screenshotSha256,
      },
    }));
  }
  return {
    mobileRoot,
    localEvidencePath,
    playEvidencePath,
    uploadKeyActivationPath,
    artifactPath,
  };
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

test("release readiness reports a verified upload-key security cooldown as blocked", async (context) => {
  const paths = await fixture({ withPlayEvidence: true, withCooldownEvidence: true });
  context.after(() => rm(paths.mobileRoot, { recursive: true, force: true }));
  const report = await auditAndroidRelease({
    ...paths,
    gitHead: GIT_HEAD,
    now: new Date("2026-08-06T14:09:00Z"),
    fetchImpl: liveFetch({ linksReady: true }),
  });
  assert.equal(report.status, "blocked");
  assert.equal(report.failed_count, 0);
  assert.equal(report.blocked_count, 1);
  assert.match(
    report.checks.find((row) => row.id === "play_evidence_contract").detail,
    /^upload_key_cooldown_until_2026-08-08T12:09:53\.000Z$/,
  );
});

test("release readiness rejects an unverified upload-key cooldown receipt", async (context) => {
  const paths = await fixture({ withPlayEvidence: true, withCooldownEvidence: true });
  context.after(() => rm(paths.mobileRoot, { recursive: true, force: true }));
  await writeFile(
    paths.uploadKeyActivationPath,
    JSON.stringify({ contract_name: "propertyquarry.google_play.upload_key_activation.v1" }),
  );
  const report = await auditAndroidRelease({
    ...paths,
    gitHead: GIT_HEAD,
    fetchImpl: liveFetch({ linksReady: true }),
  });
  assert.equal(report.status, "failed");
  assert.equal(
    report.checks.find((row) => row.id === "play_evidence_contract").state,
    "fail",
  );
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

test("release readiness rejects Play evidence for a different app version", async (context) => {
  const paths = await fixture({
    withPlayEvidence: true,
    playEvidenceOverrides: {
      release_name: "2 (1.1.1)",
      version_code: 2,
      version_name: "1.1.1",
    },
  });
  context.after(() => rm(paths.mobileRoot, { recursive: true, force: true }));
  const report = await auditAndroidRelease({
    ...paths,
    gitHead: GIT_HEAD,
    fetchImpl: liveFetch({ linksReady: true }),
  });
  assert.equal(report.status, "failed");
  assert.equal(report.checks.find((row) => row.id === "play_evidence_contract").state, "fail");
});

test("release readiness rejects a closed test outside the governed Austria scope", async (context) => {
  const paths = await fixture({
    withPlayEvidence: true,
    playEvidenceOverrides: {
      release_track: "closed",
      release_track_name: "Closed testing - Alpha",
      release_track_id: "4701087863545965393",
      target_country_regions: ["Austria", "Germany"],
      tester_group: "propertyquarry-austria-testers@googlegroups.com",
      opted_in_testers: 6,
      required_opted_in_testers: 12,
      rollout_percentage: 100,
    },
  });
  context.after(() => rm(paths.mobileRoot, { recursive: true, force: true }));
  const report = await auditAndroidRelease({
    ...paths,
    gitHead: GIT_HEAD,
    fetchImpl: liveFetch({ linksReady: true }),
  });
  assert.equal(report.status, "failed");
  assert.equal(report.checks.find((row) => row.id === "play_evidence_contract").state, "fail");
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
