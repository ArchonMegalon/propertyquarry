import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const read = (...parts) => readFileSync(join(root, ...parts), 'utf8');

test('the shell is local, accessible, and motion-safe', () => {
  const html = read('www', 'index.html');
  const css = read('www', 'shell.css');
  const script = read('www', 'shell.js');

  assert.match(html, /viewport-fit=cover/);
  assert.match(html, /aria-live="polite"/);
  assert.match(css, /prefers-reduced-motion:\s*reduce/);
  assert.match(script, /propertyquarry:runtime-error/);
  assert.doesNotMatch(`${html}\n${css}\n${script}`, /https?:\/\//);
});

test('Capacitor stays isolated and cannot navigate to an arbitrary origin', () => {
  const config = JSON.parse(read('capacitor.config.json'));
  const packageMetadata = JSON.parse(read('package.json'));

  assert.equal(config.appId, 'com.myexternalbrain.propertyquarry');
  assert.equal(config.server.url, undefined);
  assert.equal(config.server.cleartext, false);
  assert.equal(config.android.allowMixedContent, false);
  assert.deepEqual(config.server.allowNavigation, ['propertyquarry.com']);
  assert.equal(packageMetadata.name, 'propertyquarry-android');
  assert.doesNotMatch(JSON.stringify({ config, packageMetadata }), /memorial/i);
});

test('native runtime and signing contracts fail closed', () => {
  const appGradle = read('android', 'app', 'build.gradle');
  const lintConfig = read('android', 'app', 'lint.xml');
  const previewBuilder = read('scripts', 'build-preview-container.sh');
  const releaseBuilder = read('scripts', 'build-release-container.sh');
  const runtime = read(
    'android', 'app', 'src', 'main', 'java', 'com', 'myexternalbrain',
    'propertyquarry', 'PropertyQuarryRuntimeContract.java',
  );
  const nativePlugin = read(
    'android', 'app', 'src', 'main', 'java', 'com', 'myexternalbrain',
    'propertyquarry', 'PropertyQuarryNativePlugin.java',
  );
  const webViewClient = read(
    'android', 'app', 'src', 'main', 'java', 'com', 'myexternalbrain',
    'propertyquarry', 'PropertyQuarryWebViewClient.java',
  );
  const mainActivity = read(
    'android', 'app', 'src', 'main', 'java', 'com', 'myexternalbrain',
    'propertyquarry', 'MainActivity.java',
  );

  assert.match(appGradle, /verifyPropertyQuarryReleaseSigning/);
  assert.match(appGradle, /PROPERTYQUARRY_ANDROID_KEYSTORE_PATH/);
  assert.match(appGradle, /versionCode 4/);
  assert.match(appGradle, /versionName "1\.1\.2"/);
  assert.match(releaseBuilder, /propertyquarry_expected_version_code="4"/);
  assert.match(releaseBuilder, /propertyquarry_expected_version_name="1\.1\.2"/);
  assert.match(lintConfig, /src\/main\/res\/xml\/config\.xml/);
  assert.match(previewBuilder, /propertyquarry_release_bundle_backup="\$\(mktemp -d\)"/);
  assert.match(previewBuilder, /trap propertyquarry_restore_release_bundle EXIT/);
  assert.match(previewBuilder, /cp -a "\$\{propertyquarry_release_bundle_backup\}\/\."/);
  assert.match(runtime, /requireExact\(payload, "walkthrough_default", "camera"\)/);
  assert.match(runtime, /"3dvista"\.equals\(providers\.getString\(0\)\)/);
  assert.match(runtime, /"matterport"\.equals\(providers\.getString\(1\)\)/);
  assert.match(runtime, /runtime_app_links_unverified/);
  assert.match(nativePlugin, /native_bridge_path_not_allowed/);
  assert.match(nativePlugin, /Set\.of\("\/mobile\/auth\/bridge"\)/);
  assert.match(webViewClient, /isTrustedGoogleSignIn/);
  assert.match(webViewClient, /launchExternalLogin/);
  assert.match(mainActivity, /setOnCancelListener\(dialog -> resumePendingFlowOrStart\(\)\)/);
  assert.match(mainActivity, /activityResumed = true;/);
  assert.match(mainActivity, /pendingIntent = intent;/);
  assert.match(mainActivity, /if \(!runtimeReady \|\| !activityResumed\) return;/);
  assert.match(mainActivity, /WebViewFeature\.DOCUMENT_START_SCRIPT/);
  assert.match(mainActivity, /JSExport\.getBridgeJS\(this\)/);
  assert.match(mainActivity, /Collections\.singleton\(BuildConfig\.PROPERTYQUARRY_ORIGIN\)/);
  assert.match(mainActivity, /propertyquarry:native-auth-payload/);
  assert.match(mainActivity, /PropertyQuarryNativePlugin\.consumePendingAuth\(this\)/);
  assert.match(mainActivity, /PropertyQuarryNativePlugin\.consumePendingShare\(this\)/);
  assert.match(webViewClient, /deliverPendingBridgePayload\(view, url\)/);
  assert.doesNotMatch(mainActivity, /onNewIntent[\s\S]{0,240}handleIntent\(intent\)/);
  assert.match(nativePlugin, /remove\(PKCE_VERIFIER\)[\s\S]{0,80}\.commit\(\)/);
  assert.match(nativePlugin, /remove\(SHARED_IDEMPOTENCY\)[\s\S]{0,80}\.commit\(\)/);
  assert.match(nativePlugin, /native_auth_cleanup_failed/);
  assert.match(nativePlugin, /native_share_cleanup_failed/);
});

test('Play listing copy and graphics satisfy exact size limits', () => {
  for (const locale of ['en-US', 'de-DE']) {
    assert.ok(read('store', locale, 'title.txt').trim().length <= 30);
    assert.ok(read('store', locale, 'short-description.txt').trim().length <= 80);
    assert.ok(read('store', locale, 'full-description.txt').trim().length <= 4000);
  }

  const dimensions = (filename) => {
    const png = readFileSync(join(root, 'store', 'graphics', filename));
    assert.equal(png.subarray(1, 4).toString('ascii'), 'PNG');
    return [png.readUInt32BE(16), png.readUInt32BE(20)];
  };

  assert.deepEqual(dimensions('app-icon-512.png'), [512, 512]);
  assert.deepEqual(dimensions('feature-graphic.png'), [1024, 500]);
});
